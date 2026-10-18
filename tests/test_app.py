"""Application résidente (Controller) : capture, remplacement, raccourcis, notifications, journal et
traduction. Le pont avec le système est simulé ; la fenêtre de correction est la vraie."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from correcteur.engines.base import Engine  # noqa: E402
from correcteur.models import CheckResult, EngineStatus  # noqa: E402
from correcteur.platform import bridge as bridge_module  # noqa: E402
from correcteur.platform.bridge import Bridge, Capture, WaylandBridge  # noqa: E402


class QuietEngine(Engine):
    """Moteur instantané qui ne trouve rien."""
    name = "grammalecte"

    def is_enabled(self) -> bool:
        return True

    def check(self, text, language):
        return []


class FakeBridge(Bridge):
    """Pont simulé : la capture attend `allow_capture`, et les étapes sont notées dans `calls`."""
    capture_in_thread = True

    def __init__(self, defer) -> None:
        super().__init__(defer)
        self.calls: list[str] = []
        self.allow_capture = threading.Event()
        self.allow_capture.set()
        self.next_capture = Capture("")
        self.copy_result = True

    def hotkeys_supported(self):
        return True, ""

    def start_hotkeys(self, bindings):
        return []

    def begin_capture(self) -> None:
        self.calls.append("begin")
        super().begin_capture()

    def capture(self) -> Capture:
        self.allow_capture.wait(5)
        self.calls.append("capture")
        return self.next_capture

    def end_capture(self, capture) -> None:
        self.calls.append("end")
        super().end_capture(capture)

    def copy(self, text: str) -> bool:
        return self.copy_result


def wait_until(qapp, predicate, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def make_controller(qapp, home, monkeypatch):
    """make_controller(fabrique du pont) : (contrôleur, notifications reçues)."""
    from correcteur.checker import Checker
    from correcteur.config import SettingsStore
    from correcteur.ui import app as app_module

    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    store = SettingsStore()
    settings = store.load()
    settings.general.first_run_done = True
    settings.general.auto_correct = False
    settings.languagetool.enabled = False
    store.save(settings)
    notes: list[tuple[str, bool]] = []
    monkeypatch.setattr(app_module.Controller, "notify",
                        lambda self, text, error=False: None if self._stopped else notes.append((text, error)))
    monkeypatch.setattr(app_module, "Checker", lambda s, d: Checker(s, d, engines=[QuietEngine(s)]))
    made = []

    def make(bridge_factory=FakeBridge, startup=True):
        monkeypatch.setattr(app_module, "create_bridge", lambda defer: bridge_factory(defer))
        controller = app_module.Controller(qapp, startup=startup)
        made.append(controller)
        return controller, notes

    yield make
    for controller in made:
        controller.window.close()
        controller.quit()


def test_copy_text_reports_failure(make_controller):
    """copy_text renvoyait None : la fenêtre ne pouvait pas signaler une copie ratée."""
    controller, _ = make_controller()
    controller.bridge.copy_result = False
    assert controller.copy_text("texte") is False
    controller.bridge.copy_result = True
    assert controller.copy_text("texte") is True


def test_restorations_and_commands_wait_for_the_capture(qapp, make_controller, monkeypatch):
    """Pendant une capture (fil de travail), une 2e commande est annoncée au lieu d'être ignorée en silence, et
    une restauration du presse-papiers (fenêtre fermée) attend la fin de la capture."""
    from correcteur.ui import app as app_module

    monkeypatch.setattr(app_module, "_app_focused", lambda: False)
    controller, notes = make_controller()
    bridge = controller.bridge
    bridge.allow_capture.clear()
    controller._on_command("verifier-selection", "")
    assert controller._capturing and bridge.calls == ["begin"]
    controller._on_command("correction-express", "")
    controller._on_command("verifier-selection", "")
    assert notes == [("Lecture de la sélection en cours, un instant…", False)] * 2
    restored = []
    bridge._write_saved = restored.append
    bridge.release(Capture("", saved_clipboard="ancien", clipboard_touched=True))
    assert restored == []
    bridge.allow_capture.set()
    assert wait_until(qapp, lambda: not controller._capturing)
    assert bridge.calls == ["begin", "capture", "end"]
    assert restored == ["ancien"]
    assert notes[-1][0].startswith("Aucun texte sélectionné")


def test_focused_window_capture_is_done_at_once(qapp, make_controller, monkeypatch):
    """Fenêtre du correcteur au premier plan : capture par le pont dans le fil de l'interface (Qt, sous
    Wayland), sans fil de travail."""
    from correcteur.ui import app as app_module

    monkeypatch.setattr(app_module, "_app_focused", lambda: True)
    controller, _ = make_controller()
    controller.bridge.focused_capture = lambda: Capture("Ces un test.", from_selection=True)
    controller._on_command("verifier-selection", "")
    assert controller.window.isVisible() and controller.window._original == "Ces un test."
    assert controller.bridge.calls == [] and not controller._capturing


def test_express_says_when_nothing_was_checked(make_controller):
    """« Aucune faute trouvée ✓ » s'affichait alors qu'aucun moteur n'avait vérifié le texte."""
    controller, notes = make_controller()
    capture = Capture("Hello world", from_selection=True)
    controller._express_done(capture, ("Hello world", [], [], CheckResult("Hello world", [], {}, ())))
    text, error = notes[-1]
    assert error and text.startswith("Vérification impossible : aucun moteur activé ne gère")
    failed = {"grammalecte": EngineStatus("grammalecte", False, "non installé.")}
    controller._express_done(capture, ("Hello world", [], [], CheckResult("Hello world", [], failed, ())))
    assert notes[-1] == ("Vérification impossible : Grammalecte : non installé.", True)


def test_express_message_when_nothing_was_checked():
    from correcteur.ui.app import express_message

    off = "tous les moteurs sont désactivés dans les paramètres."
    assert express_message("ok", "ok", 0, 0, {}, "X", off) == (f"Vérification impossible : {off}", True)
    assert express_message("ok", "ok", 0, 0, {}, "X")[1] is True
    g_off = EngineStatus("grammalecte", False, "non installé.")
    lt_slow = EngineStatus("languagetool", False, "LanguageTool a mis plus de 8 s à répondre.")
    assert express_message("ok", "ok", 0, 0, {"grammalecte": g_off}, "X") == (
        "Vérification impossible : Grammalecte : non installé.", True)
    assert express_message("ok", "ok", 0, 0, {"grammalecte": g_off, "languagetool": lt_slow}, "X") == (
        "Vérification impossible :\nGrammalecte : non installé.\nLanguageTool a mis plus de 8 s à répondre.", True)
    g_ok = EngineStatus("grammalecte", True)
    assert express_message("ok", "ok", 0, 0, {"grammalecte": g_ok, "languagetool": lt_slow}, "X") == (
        "Vérification incomplète : LanguageTool a mis plus de 8 s à répondre.", True)
    assert express_message("ok", "ok", 0, 0, {"grammalecte": g_ok}, "X") == ("Aucune faute trouvée ✓", False)


def test_express_notification_tells_about_the_clipboard(make_controller):
    controller, notes = make_controller()
    controller.bridge.replace = lambda capture, text: (True, "Le presse-papiers contient le texte corrigé.")
    issue = SimpleNamespace()
    result = CheckResult("Ces un test", [], {"grammalecte": EngineStatus("grammalecte", True)}, ())
    controller._express_done(Capture("Ces un test", from_selection=True), ("C'est un test", [issue], [], result))
    text, error = notes[-1]
    assert not error and text.splitlines()[0].startswith("1 correction appliquée")
    assert text.splitlines()[-1] == "Le presse-papiers contient le texte corrigé."


def test_tray_menu_shows_only_working_hotkeys(make_controller):
    controller, _ = make_controller()
    g = controller.settings.general
    label = lambda: controller._menu_label("Vérifier la sélection", g.hotkey_check, "correcteur-verifier")  # noqa: E731
    controller._hotkeys_active, controller._hotkey_failures = True, []
    assert label() == "Vérifier la sélection\tCtrl+Alt+C"
    controller._hotkey_failures = [g.hotkey_check]  # pris par une autre application
    assert label() == "Vérifier la sélection"
    # Wayland : rien d'actif dans le correcteur, seuls comptent les raccourcis GNOME.
    controller._hotkeys_active, controller._hotkey_failures = False, []
    controller._gnome_bindings = None
    assert label() == "Vérifier la sélection"
    controller._gnome_bindings = {"correcteur-verifier": "<Control><Alt>c"}
    assert label() == "Vérifier la sélection\tCtrl+Alt+C"
    controller._gnome_bindings = {"correcteur-verifier": "<Control><Alt>v"}
    assert label() == "Vérifier la sélection"


class _ImmediateTimer:
    @staticmethod
    def singleShot(ms, fn) -> None:
        fn()


class _WaylandFakeBridge(FakeBridge):
    def hotkeys_supported(self):
        return False, "Sous Wayland, le raccourci se crée dans les paramètres du bureau."


def test_missing_gnome_shortcuts_are_announced_once(qapp, make_controller, monkeypatch):
    """Sous Wayland, le message de hotkeys_supported() était ignoré : rien ne disait que les raccourcis GNOME
    du correcteur manquaient."""
    from correcteur.ui import app as app_module

    monkeypatch.setattr(app_module, "QTimer", _ImmediateTimer)
    monkeypatch.setattr(app_module, "session_type", lambda: "wayland")
    monkeypatch.setattr(app_module, "desktop_name", lambda: "gnome")
    monkeypatch.setattr(app_module.autostart, "gnome_shortcuts", lambda: {"correcteur-verifier": "<Control><Alt>c"})
    controller, notes = make_controller(_WaylandFakeBridge)
    assert wait_until(qapp, lambda: controller._gnome_bindings is not None)
    assert [n for n in notes if "GNOME" in n[0]] == [(app_module._GNOME_MISSING, True)]
    controller._start_hotkeys()  # paramètres enregistrés : pas de 2e message
    qapp.processEvents()
    time.sleep(0.2)
    qapp.processEvents()
    assert len([n for n in notes if "GNOME" in n[0]]) == 1


def test_settings_change_updates_gnome_shortcuts(qapp, make_controller, monkeypatch):
    """Sous Wayland avec GNOME, changer un raccourci dans les Paramètres ne changeait pas celui de GNOME."""
    from correcteur.ui import app as app_module

    controller, notes = make_controller()
    monkeypatch.setattr(app_module, "session_type", lambda: "wayland")
    monkeypatch.setattr(app_module, "desktop_name", lambda: "ubuntu:gnome")
    installed = []

    def install(check, express):
        installed.append((check, express))
        raise RuntimeError("gsettings introuvable")

    monkeypatch.setattr(app_module.autostart, "install_gnome_shortcuts", install)
    monkeypatch.setattr(app_module.autostart, "gnome_shortcuts", lambda: {})
    controller.apply_settings(controller.settings.copy())  # raccourcis inchangés : GNOME n'est pas touché
    changed = controller.settings.copy()
    changed.general.hotkey_check = "<ctrl>+<alt>+v"
    controller.apply_settings(changed)
    assert wait_until(qapp, lambda: any("GNOME" in text for text, _ in notes))
    assert installed == [("<ctrl>+<alt>+v", "<ctrl>+<alt>+x")]
    assert ("Raccourcis GNOME non mis à jour : gsettings introuvable", True) in notes


def _open_ready(qapp, window, capture: Capture) -> None:
    window.open_with(capture.text, capture)
    assert wait_until(qapp, lambda: not window._running and len(window._statuses) == 1)
    window.editor.setPlainText("C'est un test.")  # corrigé à la main


def test_wayland_window_replace_sets_clipboard_before_hiding(qapp, make_controller, monkeypatch):
    """Sous GNOME Wayland, « Remplacer » ne collait rien : wl-paste et wl-copy, lancés après la fermeture de la
    fenêtre, prenaient le focus à l'application d'origine. Le presse-papiers est lu et posé par Qt pendant que la
    fenêtre est visible, puis seul le Ctrl+V part, 400 ms plus tard ; l'ancien contenu est remis par wl-copy
    1,5 s après le collage, une fois relu le texte collé, et la notification ne promet rien."""
    ran = []

    def run(cmd, **kwargs):
        ran.append((time.monotonic(), cmd, kwargs.get("input")))
        out = "C'est un test." if os.path.basename(cmd[0]) == "wl-paste" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(bridge_module, "which", lambda *names: next(
        (f"/usr/bin/{n}" for n in names if n in ("wl-copy", "wl-paste", "ydotool")), None))
    monkeypatch.setattr(bridge_module, "desktop_name", lambda: "gnome")
    controller, notes = make_controller(WaylandBridge)
    events = []
    monkeypatch.setattr(bridge_module, "_qt_focused", lambda: controller.window.isVisible())
    monkeypatch.setattr(bridge_module, "_qt_owns_clipboard", lambda: True)
    monkeypatch.setattr(bridge_module, "_qt_saved_text",
                        lambda: events.append(("lu", controller.window.isVisible())) or "ancien")
    monkeypatch.setattr(bridge_module, "_qt_set_clipboard",
                        lambda text: events.append(("posé", text, controller.window.isVisible())) or True)
    capture = Capture("Ces un test.", from_selection=True)
    _open_ready(qapp, controller.window, capture)
    controller.window.replace_text()
    assert events == [("lu", True), ("posé", "C'est un test.", True)]
    assert not controller.window.isVisible() and ran == []
    hidden_at = time.monotonic()
    assert wait_until(qapp, lambda: ran, timeout=3)
    pasted_at, ctrl_v, _ = ran[0]
    assert pasted_at - hidden_at >= 0.3
    assert os.path.basename(ctrl_v[0]) == "ydotool" and ctrl_v[-4:] == ["29:1", "47:1", "47:0", "29:0"] and "56:0" in ctrl_v
    assert capture.saved_clipboard == "ancien"
    assert wait_until(qapp, lambda: any(os.path.basename(cmd[0]) == "wl-copy" for _, cmd, _ in ran), timeout=4)
    names = [(os.path.basename(cmd[0]), data) for _, cmd, data in ran]
    assert names == [("ydotool", None), ("wl-paste", None), ("wl-copy", "ancien")]
    assert ran[-1][0] - pasted_at >= 1.5
    assert not any("prochaine ouverture" in text for text, _ in notes)


def test_x11_window_replace_waits_for_the_window_to_hide(qapp, make_controller):
    """X11 et Windows : comportement inchangé, remplacement 80 ms après la fermeture de la fenêtre."""
    controller, notes = make_controller()
    seen = []
    controller.bridge.replace = lambda capture, text: seen.append((text, controller.window.isVisible())) or (True, "")
    _open_ready(qapp, controller.window, Capture("Ces un test.", from_selection=True))
    controller.window.replace_text()
    assert seen == []
    assert wait_until(qapp, lambda: seen)
    assert seen == [("C'est un test.", False)]


def test_application_log(home, monkeypatch):
    """Journal correcteur.log (1 Mo, 2 sauvegardes, INFO), exceptions non rattrapées comprises."""
    from logging.handlers import RotatingFileHandler

    from correcteur.config import log_path
    from correcteur.ui.app import install_log

    root = logging.getLogger()
    levels = {handler: handler.level for handler in root.handlers}
    root_level = root.level
    monkeypatch.setattr(sys, "excepthook", lambda *args: None)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    handler = install_log(debug=False)
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert (handler.maxBytes, handler.backupCount, handler.level) == (1_000_000, 2, logging.INFO)
        assert Path(handler.baseFilename) == log_path() and log_path().parent == home / "data"
        assert install_log() is handler  # une seule fois
        logging.getLogger("correcteur.essai").info("Démarrage")
        logging.getLogger("correcteur.essai").debug("détail")
        try:
            raise ValueError("boum")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        try:
            1 / 0
        except ZeroDivisionError as exc:
            threading.excepthook(SimpleNamespace(exc_type=type(exc), exc_value=exc, exc_traceback=exc.__traceback__,
                                                 thread=SimpleNamespace(name="express")))
        handler.flush()
        content = log_path().read_text(encoding="utf-8")
        assert "Démarrage" in content and "détail" not in content
        assert "ValueError: boum" in content and "ZeroDivisionError" in content and "« express »" in content
    finally:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(root_level)
        for other, level in levels.items():
            other.setLevel(level)


def test_qt_dialogs_in_french(qapp):
    """Les boutons Yes / No des boîtes de dialogue restaient en anglais."""
    from PySide6.QtWidgets import QMessageBox

    from correcteur.ui.app import load_qt_translation

    translator = load_qt_translation(qapp)
    assert translator is not None
    try:
        box = QMessageBox(QMessageBox.Icon.Question, "Correcteur", "Télécharger ?",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        assert box.button(QMessageBox.StandardButton.Yes).text() == "&Oui"
        assert box.button(QMessageBox.StandardButton.No).text() == "&Non"
    finally:
        qapp.removeTranslator(translator)


def test_no_empty_selection_message_right_after_an_express_correction(make_controller, monkeypatch):
    """Double Ctrl+Alt+X rapide : une seule correction, puis « Aucun texte sélectionné » (la sélection venait
    d'être remplacée). Plus de ce message dans les 3 s qui suivent une correction express collée."""
    from correcteur.ui import app as app_module

    controller, notes = make_controller()
    controller.bridge.replace = lambda capture, text: (True, "")
    result = CheckResult("Ces un test", [], {"grammalecte": EngineStatus("grammalecte", True)}, ())
    controller._express_done(Capture("Ces un test", from_selection=True), ("C'est un test", [object()], [], result))
    notes.clear()
    controller._on_captured("correction-express", Capture(""))
    assert notes == []
    now = time.monotonic()
    monkeypatch.setattr(app_module.time, "monotonic", lambda: now + 3.5)
    controller._on_captured("correction-express", Capture(""))
    assert notes and notes[-1][0].startswith("Aucun texte sélectionné")


def test_ready_message_names_the_gnome_shortcuts(qapp, make_controller, monkeypatch):
    """Sous GNOME, « Correcteur prêt » nommait la commande « correcteur selection » même quand les raccourcis
    GNOME du correcteur existaient : il attend leur lecture et nomme les vraies touches."""
    from correcteur.ui import app as app_module

    monkeypatch.setattr(app_module, "session_type", lambda: "wayland")
    monkeypatch.setattr(app_module, "desktop_name", lambda: "gnome")
    monkeypatch.setattr(app_module.autostart, "gnome_shortcuts",
                        lambda: {"correcteur-verifier": "<Control><Alt>c", "correcteur-express": "<Control><Alt>x"})
    controller, notes = make_controller(_WaylandFakeBridge, startup=False)
    assert wait_until(qapp, lambda: notes, timeout=4)
    assert notes == [("Correcteur prêt : sélectionnez du texte puis Ctrl+Alt+C (fenêtre) ou Ctrl+Alt+X "
                      "(correction express).", False)]
    assert controller.bridge.desktop_shortcuts["correcteur-express"] == "<Control><Alt>x"
    time.sleep(0.2)
    qapp.processEvents()
    assert len(notes) == 1

    # Raccourcis absents : le message dit quoi faire, et le message de démarrage ne vient pas le recouvrir.
    controller.quit()  # sinon la 2e instance verrait la 1re et s'arrêterait
    monkeypatch.setattr(app_module.autostart, "gnome_shortcuts", lambda: {})
    notes.clear()
    make_controller(_WaylandFakeBridge, startup=False)
    assert wait_until(qapp, lambda: notes, timeout=4)
    assert notes == [(app_module._GNOME_MISSING, True)]


def _run_python(code: str, home: Path, timeout: float = 90) -> subprocess.CompletedProcess:
    env = dict(os.environ, CORRECTEUR_HOME=str(home), QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    env.pop("WAYLAND_DISPLAY", None)
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=timeout)


def test_crash_log_keeps_fatal_errors(tmp_path):
    """Un segfault dans un fil ne laissait aucune trace (journal vide) : faulthandler écrit dans plantages.log."""
    out = _run_python("from correcteur.ui.app import crash_log_path, install_crash_log\n"
                      "import faulthandler, threading\n"
                      "assert install_crash_log() == crash_log_path()\n"
                      "t = threading.Thread(target=faulthandler._sigsegv, name='verification')\n"
                      "t.start(); t.join()\n", tmp_path)
    assert out.returncode != 0
    content = (tmp_path / "data" / "plantages.log").read_text(encoding="utf-8")
    assert "démarré (processus" in content and "Segmentation fault" in content
    assert "_sigsegv" in content or "File " in content


def test_application_logs_its_normal_end(tmp_path):
    """Arrêt normal noté au journal (aboutToQuit) : une fin sans cette ligne est un arrêt brutal."""
    from correcteur.config import Settings

    settings = Settings()
    settings.general.first_run_done = True
    settings.languagetool.enabled = False
    settings.general.hotkeys_enabled = False
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "parametres.json").write_text(json.dumps(settings.to_dict()), encoding="utf-8")
    out = _run_python("from correcteur.ui.app import run_app\n"
                      "raise SystemExit(run_app('quitter', startup=True))\n", tmp_path)
    assert out.returncode == 0, out.stderr
    journal = (tmp_path / "data" / "correcteur.log").read_text(encoding="utf-8")
    assert "démarré" in journal and journal.rstrip().endswith("arrêté normalement.")
    assert "démarré (processus" in (tmp_path / "data" / "plantages.log").read_text(encoding="utf-8")
