"""Application résidente : icône dans la zone de notification, raccourcis
globaux, fenêtre de correction et correction express."""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QObject, Qt, QTimer, QTranslator, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QMessageBox, QSystemTrayIcon

from correcteur import APP_NAME, __version__
from correcteur.checker import Checker
from correcteur.config import PersonalDictionary, Settings, SettingsStore, log_path, resource_path
from correcteur.models import ENGINE_LABELS, EngineStatus
from correcteur.platform import autostart, desktop_name, session_type
from correcteur.platform.bridge import Capture, create_bridge
from correcteur.platform.ipc import IpcServer
from correcteur.platform.keys import display as hotkey_display
from correcteur.textutils import diff_edits
from correcteur.ui.popup import CorrectionWindow
from correcteur.ui.theme import apply_theme, current_theme

log = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(threadName)s %(name)s : %(message)s"
_GNOME_MISSING = ("Raccourcis clavier inactifs : sous Wayland, ils passent par GNOME, et ceux du correcteur n'y "
                  "sont pas. Créez-les dans Paramètres > Général (« Créer les raccourcis GNOME automatiquement »).")


def install_log(debug: bool = False) -> logging.Handler | None:
    """Journal de l'application : correcteur.log dans le dossier des données (1 Mo, 2 sauvegardes), niveau INFO
    (DEBUG avec --debug), exceptions non rattrapées comprises. L'application est lancée sans terminal (sorties
    vers /dev/null) : sans lui, une erreur ne laisserait aucune trace. Renvoie le gestionnaire installé."""
    root = logging.getLogger()
    try:
        path = log_path()
        for handler in root.handlers:
            if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == path.absolute():
                return handler  # déjà installé
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    except OSError as exc:
        log.warning("Journal impossible à ouvrir : %s", exc)
        return None
    level = logging.DEBUG if debug else logging.INFO
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    # Le terminal garde son niveau (WARNING, ou DEBUG avec --debug) : seul le fichier reçoit les messages INFO.
    for other in root.handlers:
        if other.level == logging.NOTSET:
            other.setLevel(root.level)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    if not debug:
        # Une ligne par requête vers LanguageTool : du bruit, pas une information.
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)
    root.addHandler(handler)

    previous_hook, previous_thread_hook = sys.excepthook, threading.excepthook

    def excepthook(kind, value, tb) -> None:
        if not issubclass(kind, KeyboardInterrupt):
            log.critical("Exception non rattrapée", exc_info=(kind, value, tb))
        previous_hook(kind, value, tb)

    def thread_excepthook(args) -> None:
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread is not None else "?"
            log.critical("Exception non rattrapée dans le fil « %s »", name,
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        previous_thread_hook(args)

    # Les exceptions levées dans les fonctions appelées par Qt passent aussi par sys.excepthook.
    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    return handler


def load_qt_translation(app: QApplication) -> QTranslator | None:
    """Boutons standard de Qt en français (Oui / Non, Annuler...) quelle que soit la langue du système, comme
    le reste de l'interface. qtbase_fr.qm vient de PySide6 ; PyInstaller l'embarque avec QtCore."""
    folders = [QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        folders.append(str(Path(bundle) / "PySide6" / "Qt" / "translations"))
    translator = QTranslator(app)
    for folder in folders:
        if translator.load("qtbase_fr", folder):
            app.installTranslator(translator)
            return translator
    log.info("Traduction française de Qt introuvable (%s)", ", ".join(folders))
    return None


def _app_focused() -> bool:
    """Une fenêtre du correcteur a le focus clavier."""
    return QGuiApplication.focusWindow() is not None


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
                    statuses: dict[str, EngineStatus], check_hotkey: str, no_engine: str = "") -> tuple[str, bool]:
    """Bilan d'une correction express : (texte de la notification, erreur ?).

    On montre les premières corrections ("aller → allé") pour qu'on sache ce qui
    a changé dans l'application sans avoir à relire tout le texte. Si aucun
    moteur n'a vérifié le texte (tous en échec, ou aucun actif pour sa langue :
    `no_engine` dit alors pourquoi), ce n'est pas « aucune faute »."""
    failures = []
    for status in statuses.values():
        if not status.ok:
            label = ENGINE_LABELS.get(status.name, status.name)
            detail = status.detail.strip() or "échec de la vérification."
            failures.append(detail if label in detail else f"{label} : {detail}")
    if not applied:
        if not any(status.ok for status in statuses.values()):
            reasons = failures or [no_engine or "aucun moteur actif n'a vérifié le texte."]
            if len(reasons) == 1:
                return f"Vérification impossible : {reasons[0]}", True
            return "Vérification impossible :\n" + "\n".join(reasons), True
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
    lines = [summary if changes else summary + ".", "Ctrl+Z dans l'application pour annuler."]
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
    captured = Signal(str, object)        # (commande, capture) : sélection lue dans un fil de travail
    notifyRequested = Signal(str, bool)   # notification demandée depuis un fil de travail
    gnomeShortcutsRead = Signal(object, bool)  # (raccourcis GNOME du correcteur ou None, prévenir s'il en manque)

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
        # Connexions directes : la fenêtre émet ces signaux avant de se cacher, et le pont doit agir pendant
        # qu'elle a encore le focus (sous Wayland, presse-papiers posé par Qt : voir Bridge.stage_replace).
        self.window.replaceRequested.connect(self._replace, Qt.ConnectionType.DirectConnection)
        self.window.finished.connect(self._window_finished, Qt.ConnectionType.DirectConnection)
        self.window.settingsRequested.connect(self.open_settings)
        self.window.ignoreRulesRequested.connect(self._ignore_rules)

        # Thème "comme le système" : suit le passage clair / sombre sans redémarrer (Qt 6.5 et plus).
        scheme_changed = getattr(QGuiApplication.styleHints(), "colorSchemeChanged", None)
        if scheme_changed is not None:
            scheme_changed.connect(lambda *_: self._follow_theme())

        self.command.connect(self._on_command, Qt.ConnectionType.QueuedConnection)
        self.expressDone.connect(self._express_done, Qt.ConnectionType.QueuedConnection)
        self.captured.connect(self._on_captured, Qt.ConnectionType.QueuedConnection)
        self.notifyRequested.connect(self.notify, Qt.ConnectionType.QueuedConnection)
        self.gnomeShortcutsRead.connect(self._gnome_shortcuts_read, Qt.ConnectionType.QueuedConnection)
        app.applicationStateChanged.connect(self._application_state_changed)
        self._capturing = False
        # Raccourcis qui fonctionnent réellement, pour le menu de l'icône (voir _menu_label).
        self._hotkeys_active = False
        self._hotkey_failures: list[str] = []
        self._gnome_bindings: dict[str, str] | None = None
        self._gnome_checked = self._hotkeys_explained = False

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.icon, self)
            self.tray.setToolTip(f"{APP_NAME} {__version__}")
            self.tray.activated.connect(self._tray_activated)
            self._build_tray_menu()
            self.tray.show()

        self.ipc = IpcServer(lambda cmd, data: self.command.emit(cmd, data))
        if not self.ipc.start():
            if self.ipc.other_instance:
                QTimer.singleShot(0, self.quit)  # lancée en même temps qu'une autre : celle-là répond déjà
            else:
                QTimer.singleShot(1500, lambda: self.notify(
                    f"{self.ipc.error} Les raccourcis du bureau et « correcteur quitter » ne fonctionneront pas.",
                    error=True))
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

    def copy_text(self, text: str) -> bool:
        """False si le texte n'a pas pu être mis dans le presse-papiers (la fenêtre le signale)."""
        return bool(self.bridge.copy(text))

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

    def _menu_label(self, label: str, combo: str, gnome_id: str) -> str:
        """Libellé d'une entrée du menu, avec son raccourci seulement s'il fonctionne réellement : raccourcis
        globaux actifs, ou raccourci GNOME du correcteur présent avec ces touches."""
        if self._hotkeys_active:
            works = combo not in self._hotkey_failures
        else:
            try:
                works = bool(self._gnome_bindings) and self._gnome_bindings.get(gnome_id) == autostart.gnome_binding(combo)
            except ValueError:
                works = False
        return f"{label}\t{hotkey_display(combo)}" if works else label

    def _build_tray_menu(self) -> None:
        g = self.settings.general
        verify_id, express_id = autostart.GNOME_SHORTCUT_IDS
        menu = QMenu()
        check = QAction(self._menu_label("Vérifier la sélection", g.hotkey_check, verify_id), menu)
        check.triggered.connect(lambda: self.command.emit("verifier-selection", ""))
        menu.addAction(check)
        express = QAction(self._menu_label("Correction express", g.hotkey_autocorrect, express_id), menu)
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
        self._hotkeys_active, self._hotkey_failures = False, []
        g = self.settings.general
        supported, why = self.bridge.hotkeys_supported()
        if supported and g.hotkeys_enabled:
            bindings = {
                g.hotkey_check: lambda: self.command.emit("verifier-selection", ""),
                g.hotkey_autocorrect: lambda: self.command.emit("correction-express", ""),
            }
            failed = self.bridge.start_hotkeys(bindings)
            self._hotkey_failures = failed
            self._hotkeys_active = len(failed) < len(bindings)
            if failed:
                self.notify("Raccourci déjà utilisé par une autre application : "
                            + ", ".join(hotkey_display(f) for f in failed) + ". Changez-le dans les paramètres.",
                            error=True)
        elif not supported:
            self._hotkeys_unavailable(why, warn=g.hotkeys_enabled)
        if self.tray is not None:
            self._build_tray_menu()

    def _hotkeys_unavailable(self, why: str, warn: bool) -> None:
        """Pas de raccourcis globaux. Sous Wayland, ils passent par le bureau : sous GNOME, on vérifie une fois
        que ceux du correcteur existent (et on les montre dans le menu). Ailleurs, on dit pourquoi, une fois."""
        if session_type() == "wayland":
            if "gnome" in desktop_name() and not self._gnome_checked:
                self._gnome_checked = True
                QTimer.singleShot(2000, lambda: self._read_gnome_shortcuts(warn_missing=warn))
            return
        if warn and not self._hotkeys_explained:
            self._hotkeys_explained = True
            text = why if why.startswith("Raccourcis") else f"Raccourcis globaux inactifs : {why}"
            QTimer.singleShot(2000, lambda: self.notify(text, error=True))

    def _read_gnome_shortcuts(self, warn_missing: bool, install: tuple[str, str] | None = None) -> None:
        """Lit les raccourcis GNOME du correcteur, après les avoir créés ou mis à jour avec les touches
        `install`, dans un fil de travail : gsettings peut mettre plusieurs secondes à répondre."""
        def work() -> None:
            if install is not None:
                try:
                    autostart.install_gnome_shortcuts(*install)
                    self.notifyRequested.emit("Raccourcis GNOME mis à jour : "
                                              + " et ".join(hotkey_display(c) for c in install) + ".", False)
                except Exception as exc:
                    self.notifyRequested.emit(f"Raccourcis GNOME non mis à jour : {exc}", True)
            try:
                bindings = autostart.gnome_shortcuts()
            except Exception as exc:
                log.info("Raccourcis GNOME illisibles : %s", exc)
                bindings = None
            self.gnomeShortcutsRead.emit(bindings, warn_missing)

        threading.Thread(target=work, name="raccourcis-gnome", daemon=True).start()

    def _gnome_shortcuts_read(self, bindings: dict[str, str] | None, warn_missing: bool) -> None:
        self._gnome_bindings = bindings
        if self.tray is not None:
            self._build_tray_menu()
        if warn_missing and bindings is not None and len(bindings) < len(autostart.GNOME_SHORTCUT_IDS):
            self.notify(_GNOME_MISSING, error=True)

    def _application_state_changed(self, state) -> None:
        if state == Qt.ApplicationState.ApplicationActive:
            # Un peu plus tard : le bureau envoie le contenu du presse-papiers à la fenêtre qui reçoit le focus.
            QTimer.singleShot(150, self.bridge.on_focus)

    def _on_command(self, command: str, data: str) -> None:
        if self._capturing and command in ("verifier-selection", "correction-express", "presse-papiers"):
            # Souvent une 2e frappe du raccourci, parce que rien n'apparaît encore. La rejouer ensuite lancerait
            # un 2e Ctrl+C sur une sélection qui a pu changer, ou sur la fenêtre qui vient de s'ouvrir.
            self.notify("Lecture de la sélection en cours, un instant…")
            return
        if command == "verifier-selection":
            self._capture_for(command)
        elif command == "correction-express":
            if self._busy_express:
                self.notify("Correction express déjà en cours, un instant…")
            else:
                self._capture_for(command)
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

    def _capture_for(self, command: str) -> None:
        """Lit la sélection hors du fil de l'interface quand c'est possible (plusieurs secondes sous Wayland
        quand rien n'est sélectionné), puis continue dans ce fil (_on_captured). Jusque-là, le pont reporte
        les restaurations du presse-papiers (Bridge.begin_capture)."""
        if _app_focused():
            capture = self.bridge.focused_capture()
            if capture is not None:
                self._on_captured(command, capture)
                return
        self._capturing = True
        self.bridge.begin_capture()
        if not self.bridge.capture_in_thread:
            self._on_captured(command, self._safe_capture())
            return
        threading.Thread(target=lambda: self.captured.emit(command, self._safe_capture()),
                         name="capture", daemon=True).start()

    def _safe_capture(self) -> Capture:
        try:
            return self.bridge.capture()
        except Exception:
            log.exception("Lecture de la sélection impossible")
            return Capture("")

    def _on_captured(self, command: str, capture: Capture) -> None:
        if self._capturing:
            self._capturing = False
            self.bridge.end_capture(capture)
        if not capture.text.strip():
            # Pas de fenêtre vide sans explication : l'utilisateur sait tout de suite quoi faire.
            self.release_capture(capture)
            self.notify("Aucun texte sélectionné : sélectionnez un texte, puis recommencez.")
            return
        if command == "verifier-selection":
            self.window.open_with(capture.text, capture)
        else:
            self._express(capture)

    def _express(self, capture: Capture) -> None:
        if self._busy_express:
            self.release_capture(capture)
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
        no_engine = "" if result.statuses else self._no_engine_reason(capture.text)
        text, error = express_message(capture.text, new_text, len(applied), len(remaining), result.statuses,
                                      hotkey_display(self.settings.general.hotkey_check), no_engine)
        if not applied:
            self.release_capture(capture)
            self.notify(text, error=error)
            return
        ok, message = self.bridge.replace(capture, new_text)
        if ok:
            self.notify(f"{text}\n{message}" if message else text)
        else:
            self.notify(message, error=True)

    def _no_engine_reason(self, text: str) -> str:
        """Pourquoi aucun moteur n'a même été lancé sur ce texte."""
        try:
            if not any(engine.is_enabled() for engine in self.checker.engines):
                return "tous les moteurs sont désactivés dans les paramètres."
            language = self.checker.resolve_language(text)
        except Exception:
            return ""
        return "aucun moteur activé ne gère " + ("la langue de ce texte." if language == "auto"
                                                 else f"cette langue ({language}).")

    def _replace(self, capture: Capture, text: str) -> None:
        """Remplacement demandé par la fenêtre de correction, juste avant qu'elle se cache : elle a encore le
        focus (connexion directe)."""
        staged = self.bridge.stage_replace(capture, text)
        if staged is None:
            # Laisse le temps à la fenêtre de disparaître avant de rendre la main à l'application.
            QTimer.singleShot(80, lambda: self._replaced(*self.bridge.replace(capture, text)))
            return
        ok, message = staged
        if not ok:
            self.notify(message, error=True)
            return
        # Texte corrigé déjà dans le presse-papiers : Ctrl+V quand l'application d'origine a repris le focus.
        QTimer.singleShot(self.bridge.staged_paste_delay_ms, lambda: self._replaced(*self.bridge.paste_staged(capture)))

    def _replaced(self, ok: bool, message: str) -> None:
        if message:
            self.notify(message, error=not ok)

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
        old, new = self.settings.general, settings.general
        hotkeys_changed = (old.hotkey_check, old.hotkey_autocorrect) != (new.hotkey_check, new.hotkey_autocorrect)
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
        if hotkeys_changed and session_type() == "wayland" and "gnome" in desktop_name():
            # Sous Wayland, les raccourcis sont ceux de GNOME : ils prennent les nouvelles touches.
            self._read_gnome_shortcuts(warn_missing=False, install=(new.hotkey_check, new.hotkey_autocorrect))
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
        try:
            self.app.applicationStateChanged.disconnect(self._application_state_changed)
        except (RuntimeError, TypeError):
            pass
        self.bridge.stop_hotkeys()
        self.ipc.stop()
        self.window.runner.shutdown()
        self.checker.close()
        self.app.quit()


def run_app(initial_command: str | None = None, startup: bool = False, debug: bool | None = None) -> int:
    """`debug` : journal détaillé ; par défaut, celui de la ligne de commande (option --debug)."""
    if debug is None:
        debug = logging.getLogger().isEnabledFor(logging.DEBUG)
    install_log(debug)
    log.info("%s %s démarré (Python %s, %s, session %s %s)", APP_NAME, __version__, sys.version.split()[0],
             sys.platform, session_type(), desktop_name())
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
    load_qt_translation(app)
    controller = Controller(app, startup=startup)
    app.aboutToQuit.connect(controller.checker.close)
    if initial_command:
        QTimer.singleShot(200, lambda: controller.command.emit(initial_command, ""))
    return app.exec()
