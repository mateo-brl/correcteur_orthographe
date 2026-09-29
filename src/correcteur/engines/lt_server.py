"""Démarrage et arrêt d'un serveur LanguageTool local (mode "local").

Le serveur Java est lancé en priorité basse, avec une mémoire plafonnée, et il
est rattaché au processus du correcteur : s'il se ferme (même brutalement), le
serveur s'arrête aussi et ne reste pas à consommer de la RAM."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from correcteur.config import Settings, data_dir
from correcteur.engines.base import EngineError
from correcteur.installer import find_java, languagetool_dir

_SERVER_CONFIG = """\
cacheSize=500
pipelineCaching=true
maxPipelinePoolSize=50
pipelineExpireTimeInSeconds=1800
maxTextLength=100000
"""


def _find_server_jar() -> Path | None:
    candidates = [languagetool_dir()]
    candidates += sorted(languagetool_dir().parent.glob("LanguageTool-*"), reverse=True)
    for folder in candidates:
        jar = folder / "languagetool-server.jar"
        if jar.is_file():
            return jar
    return None


class LocalLanguageToolServer:
    def __init__(self, get_settings: Callable[[], Settings]) -> None:
        self._get_settings = get_settings
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._job = None  # objet Job Windows

    @property
    def port(self) -> int:
        return self._get_settings().languagetool.local_port

    def can_run(self) -> tuple[bool, str]:
        if self.ping():
            return True, ""
        if _find_server_jar() is None:
            return False, "LanguageTool local n'est pas installé (Paramètres > Moteurs, ou `correcteur installer languagetool`)."
        if find_java() is None:
            return False, "Java est introuvable : installez Java 17 ou plus récent pour le mode local."
        return True, ""

    def ping(self, timeout: float = 0.5) -> bool:
        import httpx

        try:
            resp = httpx.get(f"http://127.0.0.1:{self.port}/v2/languages", timeout=timeout)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ensure_started(self, timeout: float = 90.0) -> None:
        with self._lock:
            if self.running() or self.ping():
                return
            ok, why = self.can_run()
            if not ok:
                raise EngineError(why)
            self._start()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._proc is not None and self._proc.poll() is not None:
                    raise EngineError(f"Le serveur LanguageTool s'est arrêté (voir {self._log_path()}).")
                if self.ping():
                    return
                time.sleep(0.3)
            raise EngineError("Le serveur LanguageTool local met trop de temps à démarrer.")

    def _log_path(self) -> Path:
        return data_dir() / "languagetool.log"

    def _start(self) -> None:
        jar = _find_server_jar()
        java = find_java()
        assert jar is not None and java is not None
        settings = self._get_settings().languagetool
        config = data_dir() / "languagetool-serveur.properties"
        config.write_text(_SERVER_CONFIG, encoding="utf-8")
        cmd = [
            java,
            "-Xms32m", f"-Xmx{max(256, settings.local_max_ram_mb)}m",
            "-XX:+UseSerialGC",          # un seul thread de ramasse-miettes : moins de CPU et de RAM
            "-XX:TieredStopAtLevel=1",   # compilation JIT légère : démarrage plus rapide
            "-Djava.awt.headless=true",
            "-cp", str(jar), "org.languagetool.server.HTTPServer",
            "--port", str(settings.local_port), "--config", str(config),
        ]
        log = open(self._log_path(), "ab")
        kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "cwd": str(jar.parent)}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000 | 0x00004000  # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
        else:
            kwargs["preexec_fn"] = _child_setup_posix
        try:
            self._proc = subprocess.Popen(cmd, **kwargs)
        finally:
            log.close()
        if sys.platform == "win32":
            self._job = _kill_with_parent_windows(self._proc)

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            if proc is None or proc.poll() is not None:
                return
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


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
            libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG : arrêt si le correcteur meurt
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
