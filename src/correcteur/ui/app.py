"""Application résidente : icône dans la zone de notification, raccourcis
globaux, fenêtre de correction et correction express."""

from __future__ import annotations

import logging
import sys
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QMessageBox, QSystemTrayIcon

from correcteur import APP_NAME, __version__
from correcteur.checker import Checker
from correcteur.config import PersonalDictionary, Settings, SettingsStore, resource_path
from correcteur.models import ENGINE_LABELS, EngineStatus
from correcteur.platform import autostart, session_type
from correcteur.platform.bridge import Capture, create_bridge
from correcteur.platform.ipc import IpcServer
from correcteur.platform.keys import display as hotkey_display
from correcteur.textutils import diff_edits
from correcteur.ui.popup import CorrectionWindow
from correcteur.ui.theme import apply_theme, current_theme

log = logging.getLogger(__name__)


def make_icon() -> QIcon:
    path = resource_path("icone.svg")
    if path.exists():
        icon = QIcon(str(path))
        if not icon.isNull():
            return icon
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#2f6fed"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QColor("white"))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(34)
    p.setFont(font)
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "C")
    p.end()
    return QIcon(pix)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} " + " ".join(w + "s" for w in word.split())


def express_message(before: str, after: str, applied: int, remaining: int,
                    statuses: dict[str, EngineStatus], check_hotkey: str) -> tuple[str, bool]:
    """Bilan d'une correction express : (texte de la notification, erreur ?).

    On montre les premières corrections ("aller → allé") pour qu'on sache ce qui
    a changé dans l'application sans avoir à relire tout le texte."""
    failures = []
    for status in statuses.values():
        if not status.ok:
            label = ENGINE_LABELS.get(status.name, status.name)
            detail = status.detail.strip() or "échec de la vérification."
            failures.append(detail if label in detail else f"{label} : {detail}")
    if not applied:
        if remaining:
            return (f"{_plural(remaining, 'remarque')} à vérifier, aucune correction sûre : "
                    f"{check_hotkey} pour les voir."), False
        if failures:
            return "Vérification incomplète : " + failures[0], True
        return "Aucune faute trouvée ✓", False
    changes = [(before[s:e].strip(), rep.strip()) for s, e, rep in diff_edits(before, after)]
    changes = [f"{old} → {new}" for old, new in changes if old.split() != new.split()]
    summary = _plural(applied, "correction appliquée")
    if changes:
        summary += " : " + ", ".join(changes[:3]) + ("…" if len(changes) > 3 else ".")
    lines = [summary if changes else summary + "."]
    if remaining:
        lines.append(f"{_plural(remaining, 'remarque')} à vérifier ({check_hotkey}).")
    lines += failures
    return "\n".join(lines), False


class Toast(QLabel):
    """Petite notification quand la zone de notification n'existe pas (GNOME sans extension)."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)  # le texte cite des extraits de l'utilisateur
        self.setMargin(12)
        self.setMaximumWidth(420)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, ms: int = 3500) -> None:
        self.setText(text)
        self.adjustSize()
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.right() - self.width() - 20, area.bottom() - self.height() - 20)
        self.show()
        self._timer.start(ms)


class Controller(QObject):
    command = Signal(str, str)            # commandes venant des raccourcis ou de l'IPC (autres threads)
    expressDone = Signal(object, object)  # (capture, (texte, appliquées, restantes, résultat) ou exception)

    def __init__(self, app: QApplication, startup: bool = False) -> None:
        super().__init__()
        self.app = app
        self.store = SettingsStore()
        self.settings: Settings = self.store.load()
        self.dictionary = PersonalDictionary()
        self.checker = Checker(self.settings, self.dictionary)
        self.bridge = create_bridge(self._defer)
        self.theme = current_theme(self.settings.general.theme)
        apply_theme(app, self.theme)
        self.icon = make_icon()
        app.setWindowIcon(self.icon)
        self._busy_express = False
        self._settings_dialog = None
        self._toast: Toast | None = None

        self.window = CorrectionWindow(self.checker, self, self.theme)
        self.window.setWindowIcon(self.icon)
        self.window.replaceRequested.connect(self._replace)
        self.window.finished.connect(self._window_finished)
        self.window.settingsRequested.connect(self.open_settings)
        self.window.ignoreRulesRequested.connect(self._ignore_rules)

        # Thème "comme le système" : suit le passage clair / sombre sans redémarrer (Qt 6.5 et plus).
        scheme_changed = getattr(QGuiApplication.styleHints(), "colorSchemeChanged", None)
        if scheme_changed is not None:
            scheme_changed.connect(lambda *_: self._follow_theme())

        self.command.connect(self._on_command, Qt.ConnectionType.QueuedConnection)
        self.expressDone.connect(self._express_done, Qt.ConnectionType.QueuedConnection)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.icon, self)
            self.tray.setToolTip(f"{APP_NAME} {__version__}")
            self.tray.activated.connect(self._tray_activated)
            self._build_tray_menu()
            self.tray.show()

        self.ipc = IpcServer(lambda cmd, data: self.command.emit(cmd, data))
        self.ipc.start()
        self._hotkey_failures: list[str] = []
        self._start_hotkeys()
        self.checker.warmup()

        if not self.settings.general.first_run_done:
            QTimer.singleShot(600, self._first_run)
        elif not startup:
            QTimer.singleShot(300, lambda: self.notify(self._ready_message()))

    def _defer(self, ms: int, fn) -> None:
        QTimer.singleShot(ms, fn)

    def notify(self, text: str, error: bool = False) -> None:
        if self.tray is not None and self.tray.supportsMessages():
            icon = QSystemTrayIcon.MessageIcon.Warning if error else QSystemTrayIcon.MessageIcon.Information
            self.tray.showMessage(APP_NAME, text, icon, 3500)
            return
        if self._toast is None:
            self._toast = Toast()
        self._toast.show_message(text)

    def copy_text(self, text: str) -> None:
        self.bridge.copy(text)

    def bring_to_front(self, widget) -> None:
        self.bridge.bring_to_front(widget)

    def release_capture(self, capture: Capture | None) -> None:
        if capture is not None:
            self.bridge.release(capture)

    def autostart_enabled(self) -> bool:
        try:
            return autostart.autostart_enabled()
        except Exception:
            return False

    def _ready_message(self) -> str:
        if session_type() == "wayland":
            return "Correcteur prêt. Utilisez votre raccourci du bureau (commande « correcteur selection »)."
        return (f"Correcteur prêt : sélectionnez du texte puis {hotkey_display(self.settings.general.hotkey_check)} "
                f"(fenêtre) ou {hotkey_display(self.settings.general.hotkey_autocorrect)} (correction express).")

    def _build_tray_menu(self) -> None:
        g = self.settings.general
        menu = QMenu()
        check = QAction(f"Vérifier la sélection\t{hotkey_display(g.hotkey_check)}", menu)
        check.triggered.connect(lambda: self.command.emit("verifier-selection", ""))
        menu.addAction(check)
        express = QAction(f"Correction express\t{hotkey_display(g.hotkey_autocorrect)}", menu)
        express.triggered.connect(lambda: self.command.emit("correction-express", ""))
        menu.addAction(express)
        menu.addAction("Vérifier le presse-papiers", lambda: self.command.emit("presse-papiers", ""))
        menu.addAction("Ouvrir le correcteur", lambda: self.command.emit("ouvrir", ""))
        menu.addSeparator()
        menu.addAction("Paramètres…", self.open_settings)
        menu.addAction("Quitter", self.quit)
        old = self.tray.contextMenu()
        self.tray.setContextMenu(menu)
        self._tray_menu = menu
        if old is not None:
            old.deleteLater()

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.command.emit("ouvrir", "")

    def _start_hotkeys(self) -> None:
        self.bridge.stop_hotkeys()
        g = self.settings.general
        supported, _why = self.bridge.hotkeys_supported()
        if not g.hotkeys_enabled or not supported:
            return
        bindings = {
            g.hotkey_check: lambda: self.command.emit("verifier-selection", ""),
            g.hotkey_autocorrect: lambda: self.command.emit("correction-express", ""),
        }
        failed = self.bridge.start_hotkeys(bindings)
        self._hotkey_failures = failed
        if failed:
            self.notify("Raccourci déjà utilisé par une autre application : " + ", ".join(hotkey_display(f) for f in failed)
                        + ". Changez-le dans les paramètres.", error=True)

    def _on_command(self, command: str, data: str) -> None:
        if command == "verifier-selection":
            capture = self.bridge.capture()
            self.window.open_with(capture.text, capture)
            if not capture.text:
                self.release_capture(capture)
                self.window.capture = None
        elif command == "correction-express":
            self._express()
        elif command == "presse-papiers":
            self.window.open_with(self.bridge.clipboard_text(), None)
        elif command == "ouvrir":
            if self.window.isVisible():
                self.bring_to_front(self.window)
            else:
                self.window.open_with(data, None)
        elif command == "parametres":
            self.open_settings()
        elif command == "quitter":
            self.quit()

    def _express(self) -> None:
        if self._busy_express:
            return
        capture = self.bridge.capture()
        if not capture.text.strip():
            self.release_capture(capture)
            self.notify("Aucun texte sélectionné.")
            return
        self._busy_express = True

        def work() -> None:
            try:
                self.expressDone.emit(capture, self.checker.autocorrect(capture.text))
            except Exception as exc:  # pragma: no cover
                self.expressDone.emit(capture, exc)

        threading.Thread(target=work, name="express", daemon=True).start()

    def _express_done(self, capture: Capture, outcome) -> None:
        self._busy_express = False
        if isinstance(outcome, Exception):
            self.release_capture(capture)
            self.notify(f"Correction impossible : {outcome}", error=True)
            return
        new_text, applied, remaining, result = outcome
        text, error = express_message(capture.text, new_text, len(applied), len(remaining), result.statuses,
                                      hotkey_display(self.settings.general.hotkey_check))
        if not applied:
            self.release_capture(capture)
            self.notify(text, error=error)
            return
        ok, message = self.bridge.replace(capture, new_text)
        self.notify(text if ok else message)

    def _replace(self, capture: Capture, text: str) -> None:
        # Laisse le temps à la fenêtre de disparaître avant de rendre la main à l'application.
        def do() -> None:
            ok, message = self.bridge.replace(capture, text)
            if not ok:
                self.notify(message)

        QTimer.singleShot(80, do)

    def _window_finished(self, capture: Capture | None, replaced: bool) -> None:
        if capture is not None and not replaced:
            self.bridge.release(capture)

    def _ignore_rules(self, rule_ids: list[str]) -> None:
        added = [r for r in dict.fromkeys(rule_ids) if r and r not in self.settings.ignored_rules]
        if added:
            self.settings.ignored_rules.extend(added)
            self.store.save(self.settings)

    def open_settings(self) -> None:
        from correcteur.ui.settings import SettingsDialog

        if self._settings_dialog is not None and self._settings_dialog.isVisible():
            self.bring_to_front(self._settings_dialog)
            return
        dialog = SettingsDialog(self.settings, self)
        dialog.setWindowIcon(self.icon)
        self._settings_dialog = dialog
        dialog.finished.connect(lambda _: setattr(self, "_settings_dialog", None))
        dialog.show()
        self.bring_to_front(dialog)

    def apply_settings(self, settings: Settings, words: list[str] | None = None) -> None:
        settings.general.first_run_done = True
        self.settings = settings
        self.store.save(settings)
        if words is not None:
            self.dictionary.set_words(words)
        self.checker.configure(settings)
        self._follow_theme()
        if self.window.isVisible():
            self.window.recheck()  # langue, typographie, dictionnaire ou règles ont pu changer
        if settings.general.autostart != self.autostart_enabled():
            try:
                autostart.set_autostart(settings.general.autostart)
            except Exception as exc:
                self.notify(f"Démarrage automatique impossible : {exc}", error=True)
        self._start_hotkeys()
        if self.tray is not None:
            self._build_tray_menu()
        self.checker.warmup()

    def _follow_theme(self) -> None:
        theme = current_theme(self.settings.general.theme)
        if theme is self.theme:
            return
        self.theme = theme
        apply_theme(self.app, theme)
        self.window.set_theme(theme)

    def _first_run(self) -> None:
        from correcteur.engines.grammalecte_engine import import_grammalecte

        self.settings.general.first_run_done = True
        self.store.save(self.settings)
        if import_grammalecte() is None:
            answer = QMessageBox.question(
                None, APP_NAME,
                "Bienvenue ! Le moteur Grammalecte (correcteur français hors ligne, 6 Mo) n'est pas encore "
                "installé. Le télécharger maintenant ?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                from correcteur.installer import install_grammalecte
                from correcteur.ui.settings import run_install

                run_install(None, "Grammalecte", install_grammalecte)
                self.checker.warmup()
        if session_type() == "wayland" or self.tray is None:
            self.open_settings()
        self.notify(self._ready_message())

    def quit(self) -> None:
        self.bridge.stop_hotkeys()
        self.ipc.stop()
        self.window.runner.shutdown()
        self.checker.close()
        self.app.quit()


def run_app(initial_command: str | None = None, startup: bool = False) -> int:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationVersion(__version__)
    QApplication.setQuitOnLastWindowClosed(False)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Correcteur.App")
        except Exception:
            pass
    app = QApplication(sys.argv[:1])
    controller = Controller(app, startup=startup)
    app.aboutToQuit.connect(controller.checker.close)
    if initial_command:
        QTimer.singleShot(200, lambda: controller.command.emit(initial_command, ""))
    return app.exec()
