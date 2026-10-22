"""Application résidente : icône dans la zone de notification, raccourcis
globaux, fenêtre de correction et correction express."""

from __future__ import annotations

import faulthandler
import logging
import os
import re
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QObject, Qt, QTimer, QTranslator, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QMessageBox, QSystemTrayIcon

from correcteur import APP_NAME, __version__
from correcteur.checker import Checker
from correcteur.config import PersonalDictionary, Settings, SettingsStore, data_dir, log_path, resource_path
from correcteur.models import ENGINE_LABELS, EngineStatus
from correcteur.platform import autostart, desktop_name, session_type
from correcteur.platform.bridge import Capture, create_bridge
from correcteur.platform.ipc import IpcServer
from correcteur.platform.keys import display as hotkey_display
from correcteur.textutils import describe_changes
from correcteur.ui.garbage import MainThreadGarbageCollector
from correcteur.ui.popup import CorrectionWindow
from correcteur.ui.theme import apply_theme, current_theme

log = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)s %(threadName)s %(name)s : %(message)s"
_GNOME_MISSING = ("Raccourcis clavier inactifs : sous Wayland, ils passent par GNOME, et ceux du correcteur n'y "
                  "sont pas. Créez-les dans Paramètres > Général (« Créer les raccourcis GNOME automatiquement »).")
# Après une correction express réussie, la sélection a été remplacée : une 2e frappe rapide du raccourci ne trouve
# plus rien, et « Aucun texte sélectionné » ferait croire à un échec.
_QUIET_AFTER_EXPRESS_S = 3.0
# Correction express plus longue que ça (long texte, LanguageTool lent) : « Correction en cours… ».
_EXPRESS_PROGRESS_MS = 1000
_crash_file = None  # plantages.log, ouvert pour toute la vie du processus (voir install_crash_log)


def crash_log_path() -> Path:
    """Fichier des plantages (erreur fatale de Python, segfault), à côté du journal."""
    return data_dir() / "plantages.log"


# Sous Windows, faulthandler écrit aussi les exceptions que le système rattrape lui-même : un client
# d'accessibilité (UI Automation) en provoque une à chaque lancement (RPC_E_CANTCALLOUT_ININPUTSYNCCALL), sans
# aucune conséquence. Elles faisaient croire à des plantages : retirées du fichier.
_HARMLESS_EXCEPTIONS = re.compile(r"^Windows fatal exception: code 0x(?:8001010d|8001010e|80010108)\b", re.I)


def clean_crash_log(path: Path) -> None:
    """Retire de plantages.log les blocs d'exceptions rattrapées sans conséquence (voir _HARMLESS_EXCEPTIONS)."""
    try:
        if not path.exists() or path.stat().st_size > 5_000_000:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return
    kept, skipping = [], False
    for line in lines:
        if _HARMLESS_EXCEPTIONS.match(line):
            skipping = True
            continue
        if skipping and line.startswith(("--- ", "Windows fatal exception", "Fatal Python error")):
            skipping = False
        if not skipping:
            kept.append(line)
    if len(kept) != len(lines):
        try:
            path.write_text("".join(kept), encoding="utf-8")
        except OSError:
            pass


def close_crash_log() -> None:
    """Arrêt normal : faulthandler n'a plus rien à écrire, le fichier est nettoyé (voir clean_crash_log)."""
    global _crash_file
    if _crash_file is None:
        return
    try:
        faulthandler.disable()
        _crash_file.close()
    except (OSError, ValueError):
        pass
    _crash_file = None
    if sys.platform == "win32":
        clean_crash_log(crash_log_path())


def install_crash_log() -> Path | None:
    """faulthandler écrit la pile de tous les fils dans plantages.log quand le processus plante (segfault d'une
    bibliothèque, par exemple) : le journal n'en garderait aucune trace. Fichier à part, ouvert en ajout : le
    journal tournant change de fichier sous le descripteur que faulthandler garde."""
    global _crash_file
    try:
        path = crash_log_path()
        if sys.platform == "win32" and (_crash_file is None or _crash_file.closed):
            clean_crash_log(path)  # fausses alertes des lancements précédents (arrêtés sans passer par quit)
        if _crash_file is None or _crash_file.closed:
            _crash_file = open(path, "a", encoding="utf-8")
        _crash_file.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} : {APP_NAME} {__version__} démarré "
                          f"(processus {os.getpid()})\n")
        _crash_file.flush()
        faulthandler.enable(file=_crash_file, all_threads=True)
    except (OSError, RuntimeError, ValueError) as exc:
        log.warning("Fichier des plantages impossible à ouvrir : %s", exc)
        return None
    return path


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
    changes = describe_changes(before, after, limit=3)  # espaces corrigées comptées : la liste suit le nombre
    summary = _plural(applied, "correction appliquée")
    if changes:
        summary += " : " + changes + ("" if changes.endswith(("…", ".", "!", "?")) else ".")
    lines = [summary if changes else summary + ".", "Ctrl+Z dans l'application pour annuler."]
    if remaining:
        lines.append(f"{_plural(remaining, 'remarque')} à vérifier ({check_hotkey}).")
    lines += failures
    return "\n".join(lines), False


class Toast(QLabel):
    """Bulle du correcteur, en bas à droite de l'écran où se trouve le pointeur. Sous Windows, elle remplace les
    notifications du système, que « Ne pas déranger » et les applications en plein écran rendent invisibles :
    « Aucun texte sélectionné » doit se voir au moment où l'on appuie sur le raccourci. Ne prend jamais le
    focus ; un clic la ferme."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)  # le texte cite des extraits de l'utilisateur
        self.setMargin(12)
        self.setMaximumWidth(440)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, ms: int = 3500, theme=None, error: bool = False, title: str = "") -> None:
        if theme is not None:
            border = theme.spelling if error else theme.outline
            self.setStyleSheet(f"QLabel {{ background: {theme.surface}; color: {theme.text}; "
                               f"border: 2px solid {border}; border-radius: 8px; }}")
        self.setText(f"{title}\n{text}" if title else text)
        self.setMinimumWidth(0)
        self.adjustSize()
        screen = QGuiApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.right() - self.width() - 20, area.bottom() - self.height() - 20)
        self.show()
        self.raise_()
        self._timer.start(ms)

    def mousePressEvent(self, event) -> None:
        self.hide()


def toast_duration(text: str) -> int:
    """Temps d'affichage : de quoi lire le message (3,5 s au moins, 10 s au plus)."""
    return max(3500, min(10_000, 2000 + 45 * len(text)))


class Controller(QObject):
    command = Signal(str, str)            # commandes venant des raccourcis ou de l'IPC (autres threads)
    expressDone = Signal(object, object)  # (capture, (texte corrigé, nombre appliqué, bilan, erreur ?) ou exception)
    captured = Signal(str, object)        # (commande, capture) : sélection lue dans un fil de travail
    notifyRequested = Signal(str, bool)   # notification demandée depuis un fil de travail
    gnomeShortcutsRead = Signal(object, bool)  # (raccourcis GNOME du correcteur ou None, prévenir s'il en manque)

    def __init__(self, app: QApplication, startup: bool = False, ipc: IpcServer | None = None) -> None:
        super().__init__()
        self.app = app
        # Canal de commande ouvert avant les travaux lents (fenêtres, icône), ou dès le lancement par la ligne de
        # commande (voir cli.main) : les commandes reçues attendent la boucle d'événements.
        self.command.connect(self._on_command, Qt.ConnectionType.QueuedConnection)
        if ipc is None:
            ipc = IpcServer()
            ipc.start()
        self.ipc = ipc
        ipc.set_handler(lambda cmd, data: self.command.emit(cmd, data))
        self.store = SettingsStore()
        self.settings: Settings = self.store.load()
        self._sync_autostart()
        self.dictionary = PersonalDictionary()
        self.checker = Checker(self.settings, self.dictionary)
        self.bridge = create_bridge(self._defer)
        self.theme = current_theme(self.settings.general.theme)
        apply_theme(app, self.theme)
        self.icon = make_icon()
        app.setWindowIcon(self.icon)
        self._busy_express = False
        self._stopped = False  # quit() appelé : plus de notification (messages différés encore en route)
        self._settings_dialog = None
        self._toast: Toast | None = None
        self._express_progress = QTimer(self)
        self._express_progress.setSingleShot(True)
        self._express_progress.timeout.connect(self._show_express_progress)

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

        self.expressDone.connect(self._express_done, Qt.ConnectionType.QueuedConnection)
        self.captured.connect(self._on_captured, Qt.ConnectionType.QueuedConnection)
        self.notifyRequested.connect(self.notify, Qt.ConnectionType.QueuedConnection)
        self.gnomeShortcutsRead.connect(self._gnome_shortcuts_read, Qt.ConnectionType.QueuedConnection)
        self._capturing = False
        self._express_replaced_at = float("-inf")  # dernière correction express collée (time.monotonic())
        self._ready_pending = False  # message « Correcteur prêt » en attente des raccourcis GNOME
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
            self._create_tray_menu()
            self.tray.show()

        if not self.ipc.listening:
            if self.ipc.other_instance:
                QTimer.singleShot(0, self.quit)  # lancée en même temps qu'une autre : celle-là répond déjà
            else:
                QTimer.singleShot(1500, lambda: self.notify(
                    f"{self.ipc.error} Les raccourcis du bureau et « correcteur quitter » ne fonctionneront pas.",
                    error=True))
        # Hors ouverture de session (et au premier lancement), « Correcteur prêt » suit et dit quels raccourcis
        # sont pris.
        self._start_hotkeys(ready_follows=not startup or not self.settings.general.first_run_done)
        self.checker.warmup()

        if not self.settings.general.first_run_done:
            QTimer.singleShot(600, self._first_run)
        elif not startup:
            if self._gnome_checked:
                # Les raccourcis GNOME sont en cours de lecture : le message les nommera (3 s au plus).
                self._ready_pending = True
                QTimer.singleShot(3000, self._show_ready)
            else:
                QTimer.singleShot(300, self._announce_ready)

    def _defer(self, ms: int, fn) -> None:
        QTimer.singleShot(ms, fn)

    def _sync_autostart(self) -> None:
        """Paramètre « lancer à l'ouverture de session » aligné sur l'état réel : l'installateur crée la clé
        Run de Windows, et une désinstallation l'avait noté désactivé."""
        actual = self.autostart_enabled()
        if self.settings.general.autostart != actual:
            self.settings.general.autostart = actual
            try:
                self.store.save(self.settings)
            except Exception:
                log.exception("Paramètres non enregistrés")

    def _own_toast(self) -> bool:
        """Messages dans la bulle du correcteur : sous Windows (toujours visibles), ou sans zone de notification."""
        return sys.platform == "win32" or self.tray is None or not self.tray.supportsMessages()

    def notify(self, text: str, error: bool = False) -> None:
        if self._stopped:
            return
        # Au journal : les messages d'erreur ; les bilans (extraits du texte de l'utilisateur) seulement avec --debug.
        log.log(logging.INFO if error else logging.DEBUG, "Message : %s", text.replace("\n", " / "))
        self._express_progress.stop()
        if not self._own_toast():
            icon = QSystemTrayIcon.MessageIcon.Warning if error else QSystemTrayIcon.MessageIcon.Information
            self.tray.showMessage(APP_NAME, text, icon, 3500)
            return
        if self._toast is None:
            self._toast = Toast()
        self._toast.show_message(text, toast_duration(text), self.theme, error, APP_NAME)

    def _show_express_progress(self) -> None:
        if self._busy_express and self._own_toast() and not self._stopped:
            if self._toast is None:
                self._toast = Toast()
            self._toast.show_message("Correction en cours…", 60_000, self.theme, False, APP_NAME)

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
        g = self.settings.general
        wayland = session_type() == "wayland"
        if not g.hotkeys_enabled and (not wayland or "gnome" in desktop_name()):
            # Sous Wayland hors GNOME, les raccourcis sont ceux du bureau : la case ne les coupe pas.
            return ("Correcteur prêt, sans ses raccourcis clavier (désactivés dans les paramètres) : utilisez le "
                    "menu de son icône.")
        verify_id, express_id = autostart.GNOME_SHORTCUT_IDS
        keys = ((g.hotkey_check, verify_id, "fenêtre"), (g.hotkey_autocorrect, express_id, "correction express"))
        working = [f"{hotkey_display(combo)} ({what})" for combo, gnome_id, what in keys
                   if self._hotkey_works(combo, gnome_id)]
        if len(working) == len(keys):
            return f"Correcteur prêt : sélectionnez du texte puis {working[0]} ou {working[1]}."
        if wayland:
            return "Correcteur prêt. Utilisez votre raccourci du bureau (commande « correcteur selection »)."
        taken = [hotkey_display(combo) for combo, _, _ in keys if combo in self._hotkey_failures]
        if not taken:
            return ("Correcteur prêt, sans ses raccourcis clavier (indisponibles sur ce système) : utilisez le menu "
                    "de son icône.")
        # Un seul message pour « prêt » et les raccourcis pris : le second remplaçait le premier dans la bulle.
        lead = (f"Correcteur prêt : sélectionnez du texte puis {working[0]}." if working
                else "Correcteur prêt, sans ses raccourcis clavier : utilisez le menu de son icône.")
        other = "" if working else " (un autre Correcteur, installé ou portable, est peut-être déjà lancé)"
        verb, pronoun = ("est", "le") if len(taken) == 1 else ("sont", "les")
        return (f"{lead}\n{' et '.join(taken)} {verb} déjà pris par une autre application{other} : "
                f"changez-{pronoun} dans les paramètres.")

    def _announce_ready(self) -> None:
        # Raccourcis pris : le message est une erreur (et va au journal).
        self.notify(self._ready_message(), error=bool(self._hotkey_failures))

    def _show_ready(self) -> None:
        if self._ready_pending:
            self._ready_pending = False
            self._announce_ready()

    def _hotkey_works(self, combo: str, gnome_id: str) -> bool:
        """Le raccourci fonctionne réellement : raccourcis globaux actifs, ou raccourci GNOME du correcteur
        présent avec ces touches."""
        if self._hotkeys_active:
            return combo not in self._hotkey_failures
        try:
            return bool(self._gnome_bindings) and self._gnome_bindings.get(gnome_id) == autostart.gnome_binding(combo)
        except ValueError:
            return False

    def _menu_label(self, label: str, combo: str, gnome_id: str) -> str:
        """Libellé d'une entrée du menu, avec son raccourci seulement s'il fonctionne réellement."""
        return f"{label}\t{hotkey_display(combo)}" if self._hotkey_works(combo, gnome_id) else label

    def _create_tray_menu(self) -> None:
        """Menu de l'icône, créé une seule fois. Le remplacer (setContextMenu puis destruction de l'ancien)
        pendant que le bureau le lit par D-Bus (icône AppIndicator de GNOME) faisait planter l'application au
        démarrage, où il changeait deux fois en une seconde : on ne change plus que ses libellés."""
        menu = QMenu()
        # « menu » : la sélection est dans la fenêtre où l'on travaillait, que le clic a fait passer au second plan.
        self._check_action = menu.addAction("Vérifier la sélection",
                                            lambda: self.command.emit("verifier-selection", "menu"))
        self._express_action = menu.addAction("Correction express",
                                              lambda: self.command.emit("correction-express", "menu"))
        menu.addAction("Vérifier le presse-papiers", lambda: self.command.emit("presse-papiers", ""))
        menu.addAction("Ouvrir le correcteur", lambda: self.command.emit("ouvrir", ""))
        menu.addSeparator()
        menu.addAction("Paramètres…", self.open_settings)
        menu.addAction("Quitter", self.quit)
        self._tray_menu = menu
        self._build_tray_menu()
        self.tray.setContextMenu(menu)

    def _build_tray_menu(self) -> None:
        """Libellés des raccourcis dans le menu de l'icône (voir _menu_label)."""
        g = self.settings.general
        verify_id, express_id = autostart.GNOME_SHORTCUT_IDS
        for action, label, combo, gnome_id in ((self._check_action, "Vérifier la sélection", g.hotkey_check, verify_id),
                                               (self._express_action, "Correction express", g.hotkey_autocorrect,
                                                express_id)):
            text = self._menu_label(label, combo, gnome_id)
            if action.text() != text:
                action.setText(text)

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.command.emit("ouvrir", "")

    def _start_hotkeys(self, ready_follows: bool = False) -> None:
        """`ready_follows` : le message « Correcteur prêt » suit, et dira lui-même quels raccourcis sont pris."""
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
            if failed and not ready_follows:
                # Tous pris : souvent un autre Correcteur (installé et portable lancés ensemble).
                other = (" (un autre Correcteur, installé ou portable, est peut-être déjà lancé)"
                         if len(failed) == len(bindings) else "")
                self.notify("Raccourci déjà utilisé par une autre application" + other + " : "
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
                self._read_gnome_shortcuts(warn_missing=warn)
            return
        if warn and not self._hotkeys_explained:
            self._hotkeys_explained = True
            text = why if why.startswith("Raccourcis") else f"Raccourcis globaux inactifs : {why}"
            QTimer.singleShot(2000, lambda: self.notify(text, error=True))

    def _read_gnome_shortcuts(self, warn_missing: bool, install: tuple[str, str] | None = None,
                              remove: bool = False) -> None:
        """Lit les raccourcis GNOME du correcteur, après les avoir créés ou mis à jour avec les touches
        `install`, ou retirés (`remove`), dans un fil de travail : gsettings peut mettre plusieurs secondes à
        répondre."""
        def work() -> None:
            if install is not None:
                try:
                    autostart.install_gnome_shortcuts(*install)
                    self.notifyRequested.emit("Raccourcis GNOME mis à jour : "
                                              + " et ".join(hotkey_display(c) for c in install) + ".", False)
                except Exception as exc:
                    self.notifyRequested.emit(f"Raccourcis GNOME non mis à jour : {exc}", True)
            if remove:
                try:
                    message = autostart.remove_gnome_shortcuts()
                    if message:
                        self.notifyRequested.emit(message, False)
                except Exception as exc:
                    self.notifyRequested.emit(f"Raccourcis GNOME non retirés : {exc}", True)
            try:
                bindings = autostart.gnome_shortcuts()
            except Exception as exc:
                log.info("Raccourcis GNOME illisibles : %s", exc)
                bindings = None
            self.gnomeShortcutsRead.emit(bindings, warn_missing)

        threading.Thread(target=work, name="raccourcis-gnome", daemon=True).start()

    def _gnome_shortcuts_read(self, bindings: dict[str, str] | None, warn_missing: bool) -> None:
        self._gnome_bindings = bindings
        self.bridge.desktop_shortcuts = bindings  # pour l'onglet Diagnostic
        if self.tray is not None:
            self._build_tray_menu()
        if warn_missing and bindings is not None and len(bindings) < len(autostart.GNOME_SHORTCUT_IDS):
            self._ready_pending = False  # ce message-ci dit déjà quoi faire
            self.notify(_GNOME_MISSING, error=True)
        else:
            self._show_ready()

    def _on_command(self, command: str, data: str) -> None:
        if self._capturing and command in ("verifier-selection", "correction-express", "presse-papiers"):
            # Souvent une 2e frappe du raccourci, parce que rien n'apparaît encore. La rejouer ensuite lancerait
            # un 2e Ctrl+C sur une sélection qui a pu changer, ou sur la fenêtre qui vient de s'ouvrir.
            self.notify("Lecture de la sélection en cours, un instant…")
            return
        if command == "verifier-selection":
            self._capture_for(command, from_menu=data == "menu")
        elif command == "correction-express":
            if self._busy_express:
                self.notify("Correction express déjà en cours, un instant…")
            else:
                self._capture_for(command, from_menu=data == "menu")
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

    def _capture_for(self, command: str, from_menu: bool = False) -> None:
        """Lit la sélection hors du fil de l'interface quand c'est possible (plusieurs secondes sous Wayland
        quand rien n'est sélectionné), puis continue dans ce fil (_on_captured). Jusque-là, le pont reporte
        les restaurations du presse-papiers (Bridge.begin_capture)."""
        if _app_focused() and not from_menu:
            capture = self.bridge.focused_capture()
            if capture is not None:
                self._on_captured(command, capture)
                return
        self._capturing = True
        self.bridge.begin_capture()
        express = command == "correction-express"
        if not self.bridge.capture_in_thread:
            self._on_captured(command, self._safe_capture(express, from_menu))
            return
        threading.Thread(target=lambda: self.captured.emit(command, self._safe_capture(express, from_menu)),
                         name="capture", daemon=True).start()

    def _safe_capture(self, express: bool = False, from_menu: bool = False) -> Capture:
        try:
            return self.bridge.capture(express=express, from_menu=from_menu)
        except Exception:
            log.exception("Lecture de la sélection impossible")
            return Capture("")

    def _on_captured(self, command: str, capture: Capture) -> None:
        if self._capturing:
            self._capturing = False
            self.bridge.end_capture(capture)
        if not capture.text.strip():
            self.release_capture(capture)
            if capture.problem:
                self.notify(capture.problem, error=True)
                return
            if time.monotonic() - self._express_replaced_at < _QUIET_AFTER_EXPRESS_S:
                log.info("Aucun texte sélectionné juste après une correction express : pas de message.")
                return
            # Pas de fenêtre vide sans explication : l'utilisateur sait tout de suite quoi faire.
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
        self._express_progress.start(_EXPRESS_PROGRESS_MS)
        check_hotkey = hotkey_display(self.settings.general.hotkey_check)

        def work() -> None:
            # Bilan calculé ici aussi : sur un long texte, la comparaison mot à mot prend du temps.
            try:
                outcome = self._express_outcome(capture.text, self.checker.autocorrect(capture.text), check_hotkey)
                self.expressDone.emit(capture, outcome)
            except Exception as exc:  # pragma: no cover
                log.exception("Correction express impossible")
                self.expressDone.emit(capture, exc)

        threading.Thread(target=work, name="express", daemon=True).start()

    def _express_outcome(self, text: str, corrected, check_hotkey: str | None = None) -> tuple[str, int, str, bool]:
        """(texte corrigé, nombre de corrections, bilan, erreur ?) d'après le résultat de Checker.autocorrect."""
        new_text, applied, remaining, result = corrected
        no_engine = "" if result.statuses else self._no_engine_reason(text)
        if check_hotkey is None:
            check_hotkey = hotkey_display(self.settings.general.hotkey_check)
        message, error = express_message(text, new_text, len(applied), len(remaining), result.statuses,
                                         check_hotkey, no_engine)
        return new_text, len(applied), message, error

    def _express_done(self, capture: Capture, outcome) -> None:
        self._busy_express = False
        self._express_progress.stop()
        if isinstance(outcome, Exception):
            self.release_capture(capture)
            self.notify(f"Correction impossible : {outcome}", error=True)
            return
        new_text, applied, text, error = outcome
        if not applied:
            self.release_capture(capture)
            self.notify(text, error=error)
            return
        ok, message = self.bridge.replace(capture, new_text)
        if ok:
            self._express_replaced_at = time.monotonic()
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

    def _ignore_rules(self, rule_ids: list[str], label: str = "") -> None:
        added = [r for r in dict.fromkeys(rule_ids) if r and r not in self.settings.ignored_rules]
        if added:
            self.settings.ignored_rules.extend(added)
            if label:
                self.settings.rule_labels.update({r: label for r in added})
            self.store.save(self.settings)

    def open_settings(self) -> None:
        from correcteur.ui.settings import SettingsDialog

        if self._settings_dialog is not None and self._settings_dialog.isVisible():
            self.bring_to_front(self._settings_dialog)
            return
        dialog = SettingsDialog(self.settings, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # sinon chaque ouverture laissait un dialogue caché
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
        if "gnome" in desktop_name():
            if session_type() == "wayland":
                # Sous Wayland, les raccourcis sont ceux de GNOME : ils prennent les nouvelles touches, disparaissent
                # quand les raccourcis globaux sont désactivés et reviennent quand ils sont réactivés.
                keys = (new.hotkey_check, new.hotkey_autocorrect)
                if old.hotkeys_enabled and not new.hotkeys_enabled:
                    self._read_gnome_shortcuts(warn_missing=False, remove=True)
                elif new.hotkeys_enabled and (hotkeys_changed or not old.hotkeys_enabled):
                    self._read_gnome_shortcuts(warn_missing=False, install=keys)
            elif old.hotkeys_enabled and not new.hotkeys_enabled:
                # GNOME sur Xorg : ceux créés sous Wayland (réglages communs aux deux sessions) marcheraient encore.
                self._read_gnome_shortcuts(warn_missing=False, remove=True)
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
        self._announce_ready()

    def quit(self) -> None:
        if self._stopped:
            return  # déjà arrêté (« quitter » reçu deux fois, par exemple)
        self._stopped = True
        self.bridge.stop_hotkeys()
        self.ipc.stop()
        self.window.runner.shutdown()
        self.checker.close()
        close_crash_log()
        self.app.quit()


def run_app(initial_command: str | None = None, startup: bool = False, debug: bool | None = None,
            ipc: IpcServer | None = None) -> int:
    """`debug` : journal détaillé ; par défaut, celui de la ligne de commande (option --debug). `ipc` : canal de
    commande déjà ouvert (voir cli.main)."""
    if debug is None:
        debug = logging.getLogger().isEnabledFor(logging.DEBUG)
    install_log(debug)
    install_crash_log()
    log.info("%s %s démarré (Python %s, %s, session %s)", APP_NAME, __version__, sys.version.split()[0],
             sys.platform, " ".join(part for part in (session_type(), desktop_name()) if part))
    if ipc is not None and not ipc.listening and not ipc.other_instance and ipc.error:
        log.warning("%s", ipc.error)  # canal ouvert avant le journal : son échec n'y était pas
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
    collector = MainThreadGarbageCollector(app)  # noqa: F841 (actif jusqu'à la fin de app.exec())
    load_qt_translation(app)
    controller = Controller(app, startup=startup, ipc=ipc)
    app.aboutToQuit.connect(controller.checker.close)
    # Arrêt normal noté au journal : sans cette ligne, une fin brutale (plantage, session fermée) se repère.
    app.aboutToQuit.connect(lambda: log.info("%s arrêté normalement.", APP_NAME))
    if initial_command:
        QTimer.singleShot(200, lambda: controller.command.emit(initial_command, ""))
    return app.exec()
