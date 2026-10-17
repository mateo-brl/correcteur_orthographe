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


def address() -> tuple[str, str]:
    if sys.platform == "win32":
        return rf"\\.\pipe\correcteur-{_user_tag()}-{_profile_tag()}", "AF_PIPE"
    name = f"correcteur-{_profile_tag()}.sock"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    bases = [Path(runtime)] if runtime and os.path.isdir(runtime) else []
    bases += [Path(tempfile.gettempdir()) / f"correcteur-{os.getuid()}", Path("/tmp") / f"correcteur-{os.getuid()}"]
    # Un dossier au chemin trop long ferait échouer l'écoute (« AF_UNIX path too long ») : on prend le suivant.
    base = next((b for b in bases if len(os.fsencode(str(b / name))) <= _UNIX_PATH_MAX), bases[-1])
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    return str(base / name), "AF_UNIX"


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
    """Envoie une commande à l'instance en cours. False s'il n'y en a pas."""
    key = _authkey(create=False)
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
    def __init__(self, handler: Callable[[str, str], None]) -> None:
        self._handler = handler
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
                self._handler(command, str(message.get("donnees", "")))
        except Exception as exc:
            log.debug("Connexion de commande abandonnée : %s", exc)
        finally:
            conn.close()

    def stop(self) -> None:
        self._stopping = True
        if self._listener is not None:
            addr = self._listener.address
            try:
                self._listener.close()
            except Exception:
                pass
            if sys.platform == "win32":
                send_command("ping", timeout=0.3)  # débloque accept()
            elif isinstance(addr, str) and os.path.exists(addr):
                try:
                    os.unlink(addr)
                except OSError:
                    pass
