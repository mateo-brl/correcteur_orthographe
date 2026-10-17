"""Serveur LanguageTool local.

Les premiers tests remplacent Java par un faux serveur (petit script Python qui
répond à /v2/languages). Le dernier lance le vrai LanguageTool (lourd : Java et
LanguageTool décompressé), avec CORRECTEUR_TEST_LT_DIR=/chemin/vers/LanguageTool-6.6."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

LT_DIR = os.environ.get("CORRECTEUR_TEST_LT_DIR")

# Faux "java" : lit --port, attend FAUX_LT_DELAI secondes (démarrage lent), puis répond comme LanguageTool.
FAKE_JAVA = r'''
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

time.sleep(float(os.environ.get("FAUX_LT_DELAI", "0")))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path.startswith("/v2/languages")
        body = b'[{"name": "French", "code": "fr", "longCode": "fr"}]' if ok else b""
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


port = int(sys.argv[sys.argv.index("--port") + 1])
HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

needs_fake_java = pytest.mark.skipif(sys.platform == "win32", reason="faux Java lancé comme script exécutable (POSIX)")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def in_thread(target) -> tuple[threading.Thread, list[BaseException]]:
    """Lance `target` dans un fil éphémère (comme le Checker) et garde ses exceptions."""
    errors: list[BaseException] = []

    def run() -> None:
        try:
            target()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def wait_until(condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "délai dépassé"
        time.sleep(0.05)


@pytest.fixture
def fake(home, tmp_path, monkeypatch):
    """Serveur local dont Java est remplacé par FAKE_JAVA ; `procs` liste les processus lancés."""
    from correcteur.config import Settings
    from correcteur.engines import lt_server

    java = tmp_path / "faux-java"
    java.write_text(f"#!{sys.executable}\n{FAKE_JAVA}", encoding="utf-8")
    java.chmod(0o755)
    jar = tmp_path / "LanguageTool" / "languagetool-server.jar"
    jar.parent.mkdir()
    jar.write_bytes(b"")
    monkeypatch.setattr(lt_server, "find_java", lambda: str(java))
    monkeypatch.setattr(lt_server, "_find_server_jar", lambda: jar)
    monkeypatch.delenv("FAUX_LT_DELAI", raising=False)

    procs: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(lt_server.subprocess, "Popen", popen)
    settings = Settings()
    settings.languagetool.mode = "local"
    settings.languagetool.local_port = free_port()
    server = lt_server.LocalLanguageToolServer(lambda: settings)
    yield SimpleNamespace(server=server, settings=settings, procs=procs, jar=jar)
    server.stop()
    for proc in procs:  # aucun faux serveur ne doit survivre au test
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@needs_fake_java
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG est propre à Linux")
def test_server_survives_the_thread_that_asked_for_it(fake):
    # Le Checker vérifie dans un fil éphémère : la fin de ce fil ne doit pas tuer Java (PR_SET_PDEATHSIG).
    thread, errors = in_thread(lambda: fake.server.ensure_started(timeout=20))
    thread.join(30)
    assert not errors
    assert len(fake.procs) == 1
    proc = fake.procs[0]
    time.sleep(0.5)  # le signal de PR_SET_PDEATHSIG serait déjà arrivé
    assert proc.poll() is None
    assert fake.server.running() and fake.server.ping()

    thread, errors = in_thread(lambda: fake.server.ensure_started(timeout=20))
    thread.join(30)
    assert not errors
    assert len(fake.procs) == 1  # même processus, pas de relance


@needs_fake_java
def test_concurrent_checks_share_one_server(fake, monkeypatch):
    monkeypatch.setenv("FAUX_LT_DELAI", "1")
    started = [in_thread(lambda: fake.server.ensure_started(timeout=20)) for _ in range(3)]
    for thread, errors in started:
        thread.join(30)
        assert not errors
    assert len(fake.procs) == 1


@needs_fake_java
@pytest.mark.parametrize("change", ["port", "memoire", "dossier"])
def test_settings_change_restarts_server(fake, monkeypatch, change):
    from correcteur.engines import lt_server

    lt = fake.settings.languagetool
    fake.server.ensure_started(timeout=20)
    old = fake.procs[-1]
    if change == "port":
        lt.local_port = free_port()
        expected = str(lt.local_port)
    elif change == "memoire":
        lt.local_max_ram_mb = 1024
        expected = "-Xmx1024m"
    else:
        jar = fake.jar.parent.parent / "LanguageTool-autre" / "languagetool-server.jar"
        jar.parent.mkdir()
        jar.write_bytes(b"")
        monkeypatch.setattr(lt_server, "_find_server_jar", lambda: jar)
        expected = str(jar)

    fake.server.ensure_started(timeout=20)
    assert len(fake.procs) == 2
    new = fake.procs[-1]
    assert expected in new.args
    assert old.wait(timeout=10) is not None  # l'ancien serveur est arrêté
    assert new.poll() is None and fake.server.ping()


@needs_fake_java
def test_stop_interrupts_a_slow_start_at_once(fake, monkeypatch):
    from correcteur.engines.base import EngineError

    monkeypatch.setenv("FAUX_LT_DELAI", "60")  # Java qui ne répond pas avant une minute
    thread, errors = in_thread(lambda: fake.server.ensure_started(timeout=20))
    wait_until(lambda: fake.procs)
    time.sleep(0.3)  # le fil attend maintenant que le serveur réponde

    t0 = time.monotonic()
    fake.server.stop()  # paramètres appliqués ou fermeture du correcteur
    assert time.monotonic() - t0 < 1.0
    thread.join(2)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], EngineError)
    assert fake.procs[0].wait(timeout=10) is not None

    # Le serveur redémarre ensuite normalement.
    monkeypatch.setenv("FAUX_LT_DELAI", "0")
    fake.server.ensure_started(timeout=20)
    assert len(fake.procs) == 2 and fake.server.ping()


@needs_fake_java
def test_check_waiting_during_start_follows_the_restart(fake, monkeypatch):
    # Port changé pendant un démarrage lent : la vérification en cours attend le nouveau serveur.
    monkeypatch.setenv("FAUX_LT_DELAI", "60")
    thread, errors = in_thread(lambda: fake.server.ensure_started(timeout=20))
    wait_until(lambda: fake.procs)
    monkeypatch.setenv("FAUX_LT_DELAI", "0")
    fake.settings.languagetool.local_port = free_port()

    fake.server.ensure_started(timeout=20)
    thread.join(10)
    assert not thread.is_alive()
    assert not errors
    assert len(fake.procs) == 2
    assert fake.procs[0].wait(timeout=10) is not None


@pytest.mark.skipif(not LT_DIR, reason="définir CORRECTEUR_TEST_LT_DIR pour tester le vrai serveur local")
def test_local_server_lifecycle(home, monkeypatch):
    from correcteur.checker import Checker
    from correcteur.config import PersonalDictionary, Settings
    from correcteur.engines import lt_server

    monkeypatch.setattr(lt_server, "_find_server_jar", lambda: Path(LT_DIR) / "languagetool-server.jar")
    settings = Settings()
    settings.grammalecte.enabled = False
    settings.languagetool.mode = "local"
    settings.languagetool.local_port = free_port()
    checker = Checker(settings, PersonalDictionary())
    try:
        text = "Il faut que tu viens demain avec les enfant."
        result = checker.check(text)
        assert result.statuses["languagetool"].ok, result.statuses["languagetool"].detail
        assert {text[i.start:i.end] for i in result.issues} & {"viens", "enfant", "les enfant"}
        assert checker.server.running()
        proc = checker.server._launch.proc
        time.sleep(1)
        assert proc.poll() is None  # la fin du fil de vérification n'a pas tué Java
    finally:
        checker.close()
    assert proc.wait(timeout=15) is not None  # le serveur Java est bien arrêté
