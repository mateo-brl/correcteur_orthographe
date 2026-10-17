"""Démarrage et arrêt d'un serveur LanguageTool local (mode "local").

Le serveur Java est lancé en priorité basse, avec une mémoire plafonnée, et il
est rattaché au processus du correcteur : s'il se ferme (même brutalement), le
serveur s'arrête aussi et ne reste pas à consommer de la RAM.

Sous Linux, ce rattachement (PR_SET_PDEATHSIG) vise le *fil* qui a créé le
processus : Java est donc toujours lancé par un fil durable propre au serveur,
jamais par les fils éphémères des vérifications."""

from __future__ import annotations

import functools
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Callable, NamedTuple

from correcteur.config import Settings, data_dir
from correcteur.engines.base import EngineError
from correcteur.installer import find_java, languagetool_dir
from correcteur.platform import child_env

log = logging.getLogger(__name__)

_SERVER_CONFIG = """\
cacheSize=500
pipelineCaching=true
maxPipelinePoolSize=50
pipelineExpireTimeInSeconds=1800
maxTextLength=100000
"""

_NOT_INSTALLED = "LanguageTool local n'est pas installé (Paramètres > Moteurs, ou `correcteur installer languagetool`)."
_NO_JAVA = "Java est introuvable : installez Java 17 ou plus récent pour le mode local."


def _find_server_jar() -> Path | None:
    candidates = [languagetool_dir()]
    candidates += sorted(languagetool_dir().parent.glob("LanguageTool-*"), reverse=True)
    for folder in candidates:
        jar = folder / "languagetool-server.jar"
        if jar.is_file():
            return jar
    return None


class _Key(NamedTuple):
    """Paramètres d'un lancement : si l'un change, le serveur est relancé."""
    port: int
    max_ram_mb: int
    jar: Path | None


class _Launch:
    """Un processus Java du serveur, de la demande de lancement à son arrêt."""

    def __init__(self, key: _Key, cmd: list[str], config: Path, log_path: Path) -> None:
        self.key = key
        self.cmd = cmd
        self.config = config
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.error = ""  # échec de Popen (droits, fichier introuvable...)
        self.job = None  # objet Job Windows
        self.ready = threading.Event()      # le serveur a répondu au moins une fois
        self.cancelled = threading.Event()  # arrêt demandé, ou remplacé par un autre lancement
        self.lock = threading.Lock()        # entre la création du processus et son annulation

    def exited(self) -> bool:
        return bool(self.error) or (self.proc is not None and self.proc.poll() is not None)

    def cancel(self, worker: _Worker) -> None:
        """Arrête le processus sans attendre : sa fin (ou kill) est attendue par le fil durable."""
        with self.lock:
            self.cancelled.set()
            proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            worker.submit(functools.partial(_reap, proc))


class _Worker:
    """Fil durable du serveur : crée les processus Java et attend leur fin, une demande à la fois."""

    def __init__(self) -> None:
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, job: Callable[[], None]) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=_run_jobs, args=(self._jobs,),
                                                name="serveur-languagetool", daemon=True)
                self._thread.start()
        self._jobs.put(job)

    def close(self) -> None:
        self._jobs.put(None)


def _run_jobs(jobs: queue.SimpleQueue) -> None:
    while True:
        job = jobs.get()
        if job is None:
            return  # serveur détruit : Java s'arrête avec ce fil (PR_SET_PDEATHSIG)
        try:
            job()
        except Exception:  # pragma: no cover - filet de sécurité, le fil doit survivre
            log.exception("Serveur LanguageTool local : tâche en échec")
        job = None  # ne retient pas le lancement entre deux demandes


class LocalLanguageToolServer:
    def __init__(self, get_settings: Callable[[], Settings]) -> None:
        self._get_settings = get_settings
        # Verrou jamais tenu pendant une attente : stop() rend toujours la main tout de suite.
        self._lock = threading.Lock()
        self._launch: _Launch | None = None
        self._generation = 0  # augmente à chaque stop() : les démarrages en cours abandonnent
        self._worker = _Worker()
        weakref.finalize(self, self._worker.close)  # le fil durable vit tant que l'objet existe

    @property
    def port(self) -> int:
        return self._get_settings().languagetool.local_port

    def can_run(self) -> tuple[bool, str]:
        if self.ping():
            return True, ""
        if _find_server_jar() is None:
            return False, _NOT_INSTALLED
        if find_java() is None:
            return False, _NO_JAVA
        return True, ""

    def ping(self, timeout: float = 0.5, port: int | None = None) -> bool:
        import httpx

        try:
            resp = httpx.get(f"http://127.0.0.1:{port or self.port}/v2/languages", timeout=timeout)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def running(self) -> bool:
        launch = self._launch
        return launch is not None and launch.proc is not None and launch.proc.poll() is None

    def ensure_started(self, timeout: float = 90.0) -> None:
        """Lance le serveur si besoin (ou le relance si ses paramètres ont changé) et attend qu'il réponde."""
        deadline = time.monotonic() + timeout
        with self._lock:
            generation = self._generation
        while True:
            launch = self._current_launch(generation)
            if launch is None or self._wait_ready(launch, generation, deadline):
                return
            # Lancement remplacé pendant l'attente (paramètres changés) : on attend le nouveau.

    def _wanted(self) -> _Key:
        lt = self._get_settings().languagetool
        return _Key(lt.local_port, max(256, lt.local_max_ram_mb), _find_server_jar())

    def _check_not_stopped(self, generation: int) -> None:
        if self._generation != generation:
            raise EngineError("Démarrage du serveur LanguageTool local interrompu.")

    def _current_launch(self, generation: int) -> _Launch | None:
        """Lancement à attendre, demandé au fil durable si besoin ; None si un autre serveur répond déjà."""
        key = self._wanted()
        port_busy = False
        with self._lock:
            self._check_not_stopped(generation)
            launch = self._launch
            if launch is not None:
                if key.jar is None:  # LanguageTool en cours de réinstallation : on garde l'actuel
                    key = key._replace(jar=launch.key.jar)
                if launch.key == key and not launch.exited():
                    return launch
                # Port, mémoire ou dossier de LanguageTool changés, ou Java arrêté tout seul : on relance.
                port_busy = launch.key.port == key.port and not launch.exited()
                self._launch = None
                launch.cancel(self._worker)
        # Serveur déjà lancé par ailleurs ? (pas si c'est le nôtre, en train de s'arrêter sur ce port)
        if not port_busy and self.ping(port=key.port):
            return None
        java = find_java()
        if key.jar is None:
            raise EngineError(_NOT_INSTALLED)
        if java is None:
            raise EngineError(_NO_JAVA)
        config = data_dir() / "languagetool-serveur.properties"
        cmd = [
            java,
            "-Xms32m", f"-Xmx{key.max_ram_mb}m",
            "-XX:+UseSerialGC",          # un seul thread de ramasse-miettes : moins de CPU et de RAM
            "-XX:TieredStopAtLevel=1",   # compilation JIT légère : démarrage plus rapide
            "-Djava.awt.headless=true",
            "-cp", str(key.jar), "org.languagetool.server.HTTPServer",
            "--port", str(key.port), "--config", str(config),
        ]
        with self._lock:
            self._check_not_stopped(generation)
            if self._launch is None:  # sinon un autre fil vient de le lancer : on attend le sien
                self._launch = _Launch(key, cmd, config, self._log_path())
                self._worker.submit(functools.partial(_spawn, self._launch))
            return self._launch

    def _wait_ready(self, launch: _Launch, generation: int, deadline: float) -> bool:
        """Attend, sans verrou, que le serveur réponde ; False s'il a été remplacé entre-temps."""
        while not launch.ready.is_set():
            if launch.cancelled.is_set():
                self._check_not_stopped(generation)
                return False
            if launch.error:
                raise EngineError(f"Impossible de lancer le serveur LanguageTool : {launch.error}")
            proc = launch.proc
            if proc is not None:
                if proc.poll() is not None:
                    raise EngineError(f"Le serveur LanguageTool s'est arrêté (voir {launch.log_path}).")
                if self.ping(port=launch.key.port):
                    launch.ready.set()
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EngineError("Le serveur LanguageTool local met trop de temps à démarrer.")
            launch.cancelled.wait(min(0.3, remaining))  # stop() réveille l'attente aussitôt
        return True

    def _log_path(self) -> Path:
        return data_dir() / "languagetool.log"

    def stop(self) -> None:
        """Arrête le serveur, ou interrompt son démarrage, sans attendre la fin de Java."""
        with self._lock:
            self._generation += 1
            launch, self._launch = self._launch, None
        if launch is not None:
            launch.cancel(self._worker)


def _spawn(launch: _Launch) -> None:
    """Crée le processus Java. Exécuté par le fil durable, qui en devient le « parent »."""
    if launch.cancelled.is_set():
        return  # arrêté avant même d'être lancé
    kwargs: dict = {"stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": str(launch.key.jar.parent)}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000 | 0x00004000  # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
    else:
        kwargs["preexec_fn"] = _child_setup_posix
    try:
        launch.config.write_text(_SERVER_CONFIG, encoding="utf-8")
        with open(launch.log_path, "ab") as log_file:
            proc = subprocess.Popen(launch.cmd, stdout=log_file, env=child_env(), **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        launch.error = str(exc) or type(exc).__name__
        return
    if sys.platform == "win32":
        launch.job = _kill_with_parent_windows(proc)
    with launch.lock:
        launch.proc = proc
        cancelled = launch.cancelled.is_set()
    if cancelled:  # stop() est arrivé pendant Popen
        proc.terminate()
        _reap(proc)


def _reap(proc: subprocess.Popen) -> None:
    """Attend la fin de Java après terminate(), et le tue s'il traîne."""
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _child_setup_posix() -> None:  # pragma: no cover - exécuté dans le processus fils
    try:
        os.nice(10)
    except OSError:
        pass
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            import signal

            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG : arrêt si le correcteur (ou le fil durable) meurt
        except Exception:
            pass


def _kill_with_parent_windows(proc: subprocess.Popen):  # pragma: no cover - Windows uniquement
    """Place le serveur dans un Job Windows fermé à la mort du correcteur."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # JobObjectExtendedLimitInformation
        kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))
        return job
    except Exception:
        return None
