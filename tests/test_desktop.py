"""Tests d'intégration sur un vrai bureau : une application cible (processus
séparé) contient du texte sélectionné ; le correcteur le capture, le corrige
et le remplace, exactement comme en utilisation réelle.

- Linux X11 : exécutés en CI sous Xvfb (QT_QPA_PLATFORM=xcb).
- Windows : nécessitent un bureau interactif (CORRECTEUR_TESTS_BUREAU=1), ce
  que les machines de CI GitHub ne garantissent pas.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import needs_grammalecte

HELPER = Path(__file__).resolve().parent / "helpers" / "target_app.py"

on_desktop = pytest.mark.skipif(
    not (
        (sys.platform.startswith("linux") and os.environ.get("QT_QPA_PLATFORM") == "xcb" and os.environ.get("DISPLAY")
         and os.environ.get("XDG_SESSION_TYPE", "x11") == "x11")
        or (sys.platform == "win32" and os.environ.get("CORRECTEUR_TESTS_BUREAU"))
    ),
    reason="bureau réel requis (Xvfb + QT_QPA_PLATFORM=xcb, ou Windows avec CORRECTEUR_TESTS_BUREAU=1)",
)
pytestmark = on_desktop


class Target:
    def __init__(self, text: str, duration_ms: int = 6000):
        self.proc = subprocess.Popen([sys.executable, str(HELPER), text, str(duration_ms)],
                                     stdout=subprocess.PIPE, text=True, encoding="utf-8")
        assert self.proc.stdout.readline().strip() == "PRET"
        time.sleep(0.8)  # le temps que la fenêtre ait le focus
        if sys.platform == "win32":
            from correcteur.platform import win32

            deadline = time.monotonic() + 3
            while win32.window_title(win32.foreground_window()) != "Cible du correcteur":
                if time.monotonic() > deadline:
                    self.proc.kill()
                    pytest.skip("la fenêtre cible n'obtient pas le premier plan (pas de bureau interactif)")
                time.sleep(0.1)

    def final_text(self) -> str:
        out = self.proc.communicate(timeout=20)[0]
        line = next(l for l in out.splitlines() if l.startswith("FINAL:"))
        return line[len("FINAL:"):].replace("\\n", "\n")


def pump(qapp, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.02)


def test_capture_and_replace_selection(qapp, home):
    from correcteur.platform.bridge import create_bridge

    bridge = create_bridge(lambda ms, fn: None)
    target = Target("Ces un test", 4000)
    capture = bridge.capture()
    assert capture.text == "Ces un test"
    assert capture.from_selection
    ok, message = bridge.replace(capture, "C'est un test")
    assert ok, message
    pump(qapp, 1.0)
    assert target.final_text() == "C'est un test"


def test_release_restores_clipboard(qapp, home):
    from correcteur.platform.bridge import create_bridge

    bridge = create_bridge(lambda ms, fn: fn())
    bridge.copy("contenu d'origine")
    pump(qapp, 0.2)
    target = Target("texte sélectionné", 3000)
    capture = bridge.capture()
    bridge.release(capture)
    pump(qapp, 0.3)
    assert bridge.clipboard_text() == "contenu d'origine"
    target.final_text()


def test_replace_after_another_app_copied(qapp, home):
    """Cas réel : l'utilisateur a copié quelque chose dans une autre application,
    et le correcteur tourne depuis longtemps avec des fenêtres ouvertes."""
    from PySide6.QtWidgets import QWidget

    from correcteur.platform.bridge import create_bridge

    idle_window = QWidget()
    idle_window.show()  # donne à Qt un horodatage X11 qui va vieillir
    pump(qapp, 0.3)
    other = subprocess.Popen(
        [sys.executable, "-c",
         "from PySide6.QtWidgets import QApplication\nfrom PySide6.QtCore import QTimer\n"
         "a = QApplication([])\na.clipboard().setText('copié ailleurs')\nprint('ok', flush=True)\n"
         "QTimer.singleShot(4000, a.quit)\na.exec()"],
        stdout=subprocess.PIPE, text=True)
    assert other.stdout.readline().strip() == "ok"
    time.sleep(0.3)
    bridge = create_bridge(lambda ms, fn: None)
    for before, after in (("Ces un test", "C'est un test"), ("les enfant jouent", "les enfants jouent")):
        target = Target(before, 3500)
        capture = bridge.capture()
        assert capture.text == before
        ok, message = bridge.replace(capture, after)
        assert ok, message
        pump(qapp, 0.8)
        assert target.final_text() == after
    idle_window.close()
    other.communicate(timeout=10)


@pytest.mark.skipif(sys.platform == "win32", reason="pynput : X11")
def test_global_hotkey_x11(home):
    from pynput.keyboard import Controller, Key

    from correcteur.platform.bridge import create_bridge

    bridge = create_bridge()
    fired = threading.Event()
    failed = bridge.start_hotkeys({"<ctrl>+<alt>+<shift>+<f12>": fired.set})
    assert failed == []
    time.sleep(0.5)
    kb = Controller()
    with kb.pressed(Key.ctrl), kb.pressed(Key.alt), kb.pressed(Key.shift):
        kb.press(Key.f12)
        kb.release(Key.f12)
    try:
        assert fired.wait(3), "le raccourci global n'a pas été reçu"
    finally:
        bridge.stop_hotkeys()


@needs_grammalecte
def test_express_correction_end_to_end(qapp, home):
    """Raccourci « correction express » : l'application cible reçoit le texte corrigé."""
    from correcteur.config import SettingsStore
    from correcteur.ui.app import Controller

    store = SettingsStore()
    settings = store.load()
    settings.general.first_run_done = True
    settings.general.hotkeys_enabled = False
    settings.languagetool.enabled = False  # hors ligne, résultat déterministe
    store.save(settings)

    controller = Controller(qapp, startup=True)
    try:
        target = Target("Je vais a la plage , et ecole est fermée.", 8000)
        controller.command.emit("correction-express", "")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (controller._busy_express or not controller.checker.engines):
            qapp.processEvents()
            time.sleep(0.02)
        pump(qapp, 1.5)
        assert target.final_text() == "Je vais à la plage, et école est fermée."
    finally:
        controller.quit()
