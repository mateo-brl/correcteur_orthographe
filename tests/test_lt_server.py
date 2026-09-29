"""Serveur LanguageTool local (lourd : nécessite Java et LanguageTool décompressé).

Lancer avec CORRECTEUR_TEST_LT_DIR=/chemin/vers/LanguageTool-6.6."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

LT_DIR = os.environ.get("CORRECTEUR_TEST_LT_DIR")
pytestmark = pytest.mark.skipif(not LT_DIR, reason="définir CORRECTEUR_TEST_LT_DIR pour tester le serveur local")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
        proc = checker.server._proc
    finally:
        checker.close()
    assert proc.poll() is not None  # le serveur Java est bien arrêté
