"""Instance unique et commandes à distance.

L'application résidente écoute sur un canal local (tube nommé Windows ou
socket Unix) protégé par une clé secrète lisible seulement par l'utilisateur.
`correcteur selection` (utilisé par les raccourcis du bureau sous Wayland)
envoie simplement une commande à l'instance déjà lancée : c'est instantané,
aucun moteur n'est rechargé."""

from __future__ import annotations

import getpass
import hashlib
import logging
import os
import secrets
import stat
import sys
import tempfile
import threading
import time
from multiprocessing.connection import Client, Listener, answer_challenge, deliver_challenge
from pathlib import Path
from typing import Callable

from correcteur.config import config_dir

log = logging.getLogger(__name__)

COMMANDS = ("verifier-selection", "correction-express", "ouvrir", "presse-papiers", "parametres", "quitter")

# Délai accordé à un client pour s'authentifier et envoyer sa commande.
CLIENT_TIMEOUT = 2.0
# Longueur maximale du chemin d'une socket Unix (108 octets sous Linux, zéro final compris).
_UNIX_PATH_MAX = 107


def _user_tag() -> str:
    try:
        return "".join(c for c in getpass.getuser() if c.isalnum()) or "user"
    except Exception:
        return "user"


def _profile_tag() -> str:
    """Un canal par dossier de paramètres : un profil portable ou de test ne
    parle jamais à l'instance principale."""
    return hashlib.sha1(str(config_dir().resolve()).encode("utf-8")).hexdigest()[:10]


def _temp_dirs() -> list[Path]:
    """Dossiers temporaires partagés où créer le dossier du canal."""
    return list(dict.fromkeys([Path(tempfile.gettempdir()), Path("/tmp")]))


def _private_dir(path: Path, create: bool) -> bool:
    """Vrai si `path` est un dossier (pas un lien) de l'utilisateur, fermé aux autres. Dans un dossier partagé
    comme /tmp, un autre utilisateur local pourrait sinon créer le dossier avant lui et y placer une fausse
    socket. Avec `create`, le dossier est créé s'il n'existe pas, et remis en mode 700 s'il est à lui."""
    try:
        if create and not os.path.lexists(path):
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass  # créé entre-temps : vérifié ci-dessous
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return False
        if stat.S_IMODE(info.st_mode) & 0o077:
            if not create:
                return False  # XDG_RUNTIME_DIR ouvert aux autres : on ne touche pas à ses droits
            os.chmod(path, 0o700)
        return True
    except OSError as exc:
        log.debug("Dossier du canal inutilisable (%s) : %s", path, exc)
        return False


def address() -> tuple[str, str]:
    """Adresse du canal de commande. Ne lève jamais d'erreur (la ligne de commande l'appelle sans filet) : si
    aucun dossier ne convient, l'écoute échouera et IpcServer.start() le dira."""
    if sys.platform == "win32":
        return rf"\\.\pipe\correcteur-{_user_tag()}-{_profile_tag()}", "AF_PIPE"
    try:
        name = f"correcteur-{_profile_tag()}.sock"
        uid = os.getuid()

        def fits(base: Path) -> bool:
            # Un chemin trop long ferait échouer l'écoute (« AF_UNIX path too long ») : on prend le suivant.
            return len(os.fsencode(str(base / name))) <= _UNIX_PATH_MAX

        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime and fits(Path(runtime)) and _private_dir(Path(runtime), create=False):
            return str(Path(runtime) / name), "AF_UNIX"
        # Dossier pris par un fichier, par un autre utilisateur ou illisible : dossier suffixé (-1, -2...).
        candidates = [tmp / f"correcteur-{uid}{suffix}" for suffix in ("", "-1", "-2", "-3") for tmp in _temp_dirs()]
        for base in candidates:
            if fits(base) and _private_dir(base, create=True):
                return str(base / name), "AF_UNIX"
        base = config_dir()  # dossier de l'utilisateur, en dernier recours
        return str(base / name), "AF_UNIX"
    except Exception as exc:
        log.warning("Adresse du canal de commande : %s", exc)
        return str(Path(tempfile.gettempdir()) / f"correcteur-{_user_tag()}.sock"), "AF_UNIX"


def _authkey(create: bool) -> bytes | None:
    path = config_dir() / "ipc.cle"
    try:
        return bytes.fromhex(path.read_text().strip())
    except (OSError, ValueError):
        if not create:
            return None
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.hex())
    return key


def send_command(command: str, payload: str = "", timeout: float = 2.0) -> bool:
    """Envoie une commande à l'instance en cours. False s'il n'y en a pas (ou si le dossier des paramètres est
    inaccessible : la ligne de commande l'appelle sans filet)."""
    try:
        key = _authkey(create=False)
    except OSError:
        return False
    if key is None:
        return False
    addr, family = address()
    result: list[bool] = [False]

    def attempt() -> None:
        try:
            with Client(addr, family=family, authkey=key) as conn:
                conn.send({"commande": command, "donnees": payload})
                result[0] = conn.recv() == "ok"
        except Exception:
            result[0] = False

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout)
    return result[0]


class _TimedConnection:
    """Connexion dont les lectures échouent passé l'échéance, poignée de main comprise : un client qui se
    connecte sans rien envoyer ne retient pas le serveur."""

    def __init__(self, conn, timeout: float) -> None:
        self._conn = conn
        self._deadline = time.monotonic() + timeout

    def _wait(self) -> None:
        if not self._conn.poll(max(0.0, self._deadline - time.monotonic())):
            raise TimeoutError("client muet")

    def send_bytes(self, data) -> None:
        self._conn.send_bytes(data)

    def recv_bytes(self, maxlength=None) -> bytes:
        self._wait()
        return self._conn.recv_bytes(maxlength)

    def recv(self):
        self._wait()
        return self._conn.recv()


class IpcServer:
    def __init__(self, handler: Callable[[str, str], None] | None = None) -> None:
        self._handler = handler
        # Sans destinataire (canal ouvert dès le lancement, avant l'interface : voir cli.main), les commandes
        # attendent ici et set_handler les transmet dans l'ordre.
        self._waiting: list[tuple[str, str]] = []
        self._handler_lock = threading.Lock()
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._key = b""
        self._stopping = False
        self.error = ""
        self.other_instance = False  # start() a échoué parce qu'une autre instance écoute déjà

    def _fail(self, reason: str) -> bool:
        self.error = reason
        log.warning("%s", reason)
        return False

    @property
    def listening(self) -> bool:
        return self._listener is not None

    def set_handler(self, handler: Callable[[str, str], None]) -> None:
        """Destinataire des commandes ; il reçoit d'abord celles arrivées avant lui."""
        with self._handler_lock:
            self._handler = handler
            waiting, self._waiting = self._waiting, []
            for command, data in waiting:
                handler(command, data)

    def _dispatch(self, command: str, data: str) -> None:
        with self._handler_lock:
            handler = self._handler
            if handler is None:
                self._waiting.append((command, data))
                return
        handler(command, data)

    def start(self) -> bool:
        """False si une autre instance écoute déjà (`other_instance`) ou si l'écoute est impossible ; la raison
        est alors dans `error`."""
        self.error, self.other_instance = "", False
        try:
            addr, family = address()
            self._key = _authkey(create=True)
        except OSError as exc:
            return self._fail(f"Canal de commande indisponible : {exc}")
        if send_command("ping", timeout=1.0):
            self.other_instance = True
            return self._fail("Une autre instance du correcteur est déjà lancée.")
        if family == "AF_UNIX" and os.path.exists(addr):
            try:
                os.unlink(addr)  # socket orpheline d'un arrêt brutal
            except OSError:
                pass
        listener = None
        try:
            # Sans clé ici : l'authentification se fait connexion par connexion, avec un délai (voir _handle).
            listener = Listener(addr, family=family)
            if family == "AF_UNIX":
                os.chmod(addr, 0o600)
        except OSError as exc:
            if listener is not None:
                listener.close()
            return self._fail(f"Canal de commande indisponible ({addr}) : {exc}")
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, name="ipc", daemon=True)
        self._thread.start()
        return True

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stopping:
            try:
                conn = self._listener.accept()
            except Exception:
                if self._stopping:
                    return
                continue
            threading.Thread(target=self._handle, args=(conn,), name="ipc-client", daemon=True).start()

    def _handle(self, conn) -> None:
        timed = _TimedConnection(conn, CLIENT_TIMEOUT)
        try:
            deliver_challenge(timed, self._key)
            answer_challenge(timed, self._key)
            message = timed.recv()
            command = message.get("commande", "") if isinstance(message, dict) else ""
            conn.send("ok")
            if command and command != "ping":
                self._dispatch(command, str(message.get("donnees", "")))
        except Exception as exc:
            log.debug("Connexion de commande abandonnée : %s", exc)
        finally:
            conn.close()

    def stop(self) -> None:
        self._stopping = True
        listener, self._listener = self._listener, None  # un 2e appel ne fait rien (Listener fermé : AttributeError)
        if listener is not None:
            addr = listener.address
            try:
                listener.close()
            except Exception:
                pass
            if sys.platform == "win32":
                send_command("ping", timeout=0.3)  # débloque accept()
            elif isinstance(addr, str) and os.path.exists(addr):
                try:
                    os.unlink(addr)
                except OSError:
                    pass
