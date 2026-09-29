from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def pytest_configure(config):
    import tempfile

    # Jamais les vrais paramètres de l'utilisateur : dossier isolé pour la session de tests.
    os.environ["CORRECTEUR_HOME"] = tempfile.mkdtemp(prefix="correcteur-tests-")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    config.addinivalue_line("markers", "windows: tests réservés à Windows")
    config.addinivalue_line("markers", "x11: tests nécessitant un serveur X11 (Xvfb en CI)")
    config.addinivalue_line("markers", "reseau: tests qui appellent un service en ligne")


@pytest.fixture
def settings():
    from correcteur.config import Settings

    return Settings()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Dossier de configuration vierge pour un test."""
    monkeypatch.setenv("CORRECTEUR_HOME", str(tmp_path))
    return tmp_path


def _has_grammalecte() -> bool:
    from correcteur.engines.grammalecte_engine import import_grammalecte

    return import_grammalecte() is not None


needs_grammalecte = pytest.mark.skipif(not _has_grammalecte(), reason="Grammalecte non installé "
                                       "(python -m correcteur installer grammalecte)")


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "windows" in item.keywords and sys.platform != "win32":
            item.add_marker(pytest.mark.skip(reason="Windows uniquement"))
        if "x11" in item.keywords:
            from correcteur.platform import session_type

            if not sys.platform.startswith("linux") or session_type() != "x11":
                item.add_marker(pytest.mark.skip(reason="serveur X11 requis"))
        if "reseau" in item.keywords and not os.environ.get("CORRECTEUR_TESTS_RESEAU"):
            item.add_marker(pytest.mark.skip(reason="définir CORRECTEUR_TESTS_RESEAU=1 pour les tests en ligne"))
