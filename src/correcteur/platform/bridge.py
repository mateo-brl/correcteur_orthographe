"""Récupérer le texte sélectionné dans n'importe quelle application et le
remplacer par le texte corrigé.

Toutes les méthodes s'exécutent dans le thread de l'interface, sauf capture()
quand `capture_in_thread` est vrai (et les rappels des raccourcis).
`defer(ms, fonction)` programme une action différée dans ce même thread
(fourni par l'application Qt).

Presse-papiers emprunté : pour copier la sélection (copie simulée) ou coller
le texte corrigé, le correcteur écrit dans le presse-papiers, puis y remet ce
qu'il contenait. Sous X11 et Windows, il ne sait remettre que du texte : un
presse-papiers qui contient autre chose (image, fichiers copiés...) n'y est
jamais marqué. Sous Wayland, tout format est gardé (voir SavedClipboard).
Pendant une capture (begin_capture, end_capture), aucune restauration n'a lieu."""

from __future__ import annotations

import logging
import os
import re
import select
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from correcteur.platform import child_env, desktop_name, session_type, which

log = logging.getLogger(__name__)

Defer = Callable[[int, Callable[[], None]], None]
_RESTORE_DELAY_MS = 1200
# Jamais de Ctrl+V si le texte corrigé n'est pas dans le presse-papiers : on collerait son ancien contenu.
_COPY_FAILED = "Copie du texte corrigé impossible (presse-papiers inaccessible) : rien n'a été collé."
_NO_PASTE_TOOL = ("Texte corrigé copié : collez-le avec Ctrl+V (installez wtype ou ydotool pour le collage "
                  "automatique).")
# Phrase ajoutée à la notification quand l'ancien presse-papiers ne pourra pas être remis.
_CLIPBOARD_LOST = ("L'ancien contenu du presse-papiers (trop gros ou illisible) n'a pas pu être gardé : il contient "
                   "maintenant le texte corrigé.")
# Wayland : contenu sauvé avant une copie simulée (une image peut peser lourd), et restauration après le collage :
# délai pour que l'application lise le collage (et sous GNOME, que le focus soit revenu), pause de saisie à
# attendre, abandon passé ce délai après l'heure prévue.
_MAX_SAVED = 50_000_000
_RESTORE_AFTER_PASTE_GNOME_S = 1.5
_RESTORE_AFTER_PASTE_S = 0.5
_IDLE_BEFORE_RESTORE_MS = 400
_RESTORE_GIVE_UP_S = 5.0
# Le raccourci du bureau part à l'appui des touches : quand la commande arrive, l'utilisateur tient peut-être
# encore Ctrl+Alt, et une copie simulée deviendrait Ctrl+Alt+Inser. Sous Wayland, impossible de savoir quand il les
# relâche (un relâchement envoyé par ydotool n'agit pas sur les touches du vrai clavier) : on attend.
_KEYS_RELEASE_S = 0.3
# Sous GNOME, Mutter n'a pas le protocole data-control : wl-copy et wl-paste créent une surface pour obtenir le
# focus clavier, puis le rendent. Une frappe simulée trop tôt arriverait à cette surface, pas à l'application.
_FOCUS_RETURN_S = 0.5
# Fenêtre de correction cachée : le temps que le bureau rende le focus à l'application d'origine (impossible de
# le lui demander sous Wayland ; environ 400 ms sous GNOME).
_WINDOW_FOCUS_RETURN_MS = 400
# Relâchement de Ctrl, Alt, Maj et Super (gauche et droite) avant chaque combinaison envoyée par ydotool. Il
# n'agit que sur son clavier virtuel (touche restée enfoncée par une commande interrompue) : le noyau ignore le
# relâchement d'une touche que ce clavier n'a pas enfoncée, d'où aussi l'attente de _KEYS_RELEASE_S.
_YDOTOOL_RELEASE = ("29:0", "97:0", "56:0", "100:0", "42:0", "54:0", "125:0", "126:0")
# Copie simulée sous Linux : Ctrl+Inser, jamais Ctrl+C, qui interrompt le programme en cours dans un terminal
# (« ^C »). GTK, Qt, LibreOffice, Firefox, Chromium et les terminaux VTE copient avec Ctrl+Inser ; un terminal qui ne
# le connaît pas reçoit une séquence d'échappement, sans conséquence. Le collage reste Ctrl+V : Maj+Inser collerait
# la sélection primaire dans les terminaux VTE, c'est-à-dire le texte d'origine.
_YDOTOOL_KEYS = {"copy": "110", "paste": "47"}  # KEY_INSERT, KEY_V
_WTYPE_KEYS = {"copy": ["-k", "Insert"], "paste": ["v"]}
# Marqueur posé sous un format à nous, sans aucun format texte : les historiques du presse-papiers (Clipboard
# Indicator, GPaste, Klipper), qui ne gardent que du texte, l'ignorent.
_MARKER_TYPE = "application/x-correcteur-marqueur"
# Formats de texte brut et cibles de service X11 : un presse-papiers qui n'offre que ceux-là ne contient que du
# texte, qu'on sait remettre. Tout autre format (image, page web, fichiers copiés...) serait perdu.
_TEXT_FORMATS = {"UTF8_STRING", "STRING", "TEXT", "COMPOUND_TEXT", "TARGETS", "MULTIPLE", "TIMESTAMP",
                 "SAVE_TARGETS", "DELETE", "INSERT_PROPERTY", "INSERT_SELECTION"}


_PLAIN_TEXT = {"UTF8_STRING", "STRING", "TEXT", "COMPOUND_TEXT"}


_UNREAD = object()  # texte du presse-papiers non lu (voir WaylandBridge._clipboard_before)


def _text_only(formats) -> bool:
    return all(f in _TEXT_FORMATS or f.lower().startswith("text/plain") for f in formats)


def _has_text(formats) -> bool:
    return any(f in _PLAIN_TEXT or f.lower().startswith("text/plain") for f in formats)


def _clipboard_marker() -> str:
    """Contenu unique mis dans le presse-papiers juste avant une copie simulée. Comparer au contenu d'avant ne
    suffit pas : si l'utilisateur vient de copier le texte qu'il a sélectionné, la copie ne change rien et la
    sélection passerait pour vide (« Aucun texte sélectionné »)."""
    return f"⁣correcteur {time.monotonic_ns()}"


def _sleep_until(deadline: float) -> None:
    time.sleep(max(0.0, deadline - time.monotonic()))


# Presse-papiers de Qt : dans le fil de l'interface uniquement.
def _qt_clipboard():
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.clipboard()


def _qt_set_clipboard(text: str) -> bool:
    """Presse-papiers via Qt, relu pour vérifier que l'écriture a pris."""
    clipboard = _qt_clipboard()
    clipboard.setText(text)
    return clipboard.text() == text


def _qt_saved_text() -> str | None:
    """Texte du presse-papiers lu par Qt, à remettre plus tard ; None s'il contient autre chose que du texte."""
    clipboard = _qt_clipboard()
    data = clipboard.mimeData()
    if data is not None and not _text_only(data.formats()):
        return None
    return clipboard.text()


def _qt_focused() -> bool:
    """Une fenêtre du correcteur a le focus clavier."""
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() is not None and QGuiApplication.focusWindow() is not None


def _qt_owns_clipboard() -> bool:
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.instance() is not None and QGuiApplication.clipboard().ownsClipboard()


@dataclass
class Capture:
    text: str
    target: Any = None             # fenêtre d'origine (HWND Windows, identifiant X11)
    from_selection: bool = False   # le texte vient bien d'une sélection
    saved_clipboard: Any = None    # contenu du presse-papiers à restaurer (None : rien à restaurer)
    clipboard_touched: bool = False
    started: float = 0.0           # time.monotonic() au début de la capture, juste après la frappe du raccourci
    left: Any = None               # ce que le correcteur a laissé dans le presse-papiers (marqueur, copie, texte collé)
    owner: Any = None              # wl-copy --foreground qui possède ce qu'il a laissé (vivant : rien n'a changé)


class Bridge:
    name = "aucun"

    def __init__(self, defer: Defer | None = None) -> None:
        self._defer = defer or (lambda ms, fn: threading.Timer(ms / 1000, fn).start())
        self._lock = threading.Lock()
        self._capturing = False
        self._postponed: list[Callable[[], None]] = []  # restaurations demandées pendant une capture
        # Presse-papiers occupé par le correcteur : (contenu d'origine à remettre, contenu qu'il y a laissé).
        self._borrowed: tuple[Any, Any] | None = None
        self._generation = 0  # augmente à chaque emprunt : une restauration plus ancienne est dépassée
        # Raccourcis du bureau trouvés (GNOME : identifiant -> touches), fournis par l'application.
        self.desktop_shortcuts: dict[str, str] | None = None

    # raccourcis
    def hotkeys_supported(self) -> tuple[bool, str]:
        return False, "Raccourcis globaux indisponibles sur ce système."

    def start_hotkeys(self, bindings: dict[str, Callable[[], None]]) -> list[str]:
        return list(bindings)

    def stop_hotkeys(self) -> None:
        pass

    # sélection
    # La capture peut tourner hors du fil de l'interface (elle dure jusqu'à quelques secondes sous Wayland) :
    # vrai tant qu'elle n'utilise pas le presse-papiers de Qt, réservé à ce fil.
    capture_in_thread = False

    def capture(self) -> Capture:
        return Capture("")

    def focused_capture(self) -> Capture | None:
        """Capture faite tout de suite dans le fil de l'interface, quand une fenêtre du correcteur a le focus.
        None : capture ordinaire."""
        return None

    def begin_capture(self) -> None:
        """Avant une capture (fil de l'interface). Jusqu'à end_capture, les restaurations du presse-papiers
        attendent : pendant la copie simulée, l'ancien contenu remplacerait le marqueur et passerait pour la
        copie du texte sélectionné (en correction express, il serait corrigé puis collé à sa place)."""
        with self._lock:
            self._capturing = True

    def end_capture(self, capture: Capture) -> None:
        """Après la capture (fil de l'interface) : note l'emprunt du presse-papiers, puis rejoue les
        restaurations reportées."""
        with self._lock:
            self._capturing = False
            postponed, self._postponed = self._postponed, []
        if capture.clipboard_touched:
            self._borrow(capture, capture.left)
        for restore in postponed:
            restore()

    def _borrow(self, capture: Capture, left: Any) -> None:
        """Le correcteur laisse `left` dans le presse-papiers et devra y remettre capture.saved_clipboard. S'il
        l'occupait déjà (restauration encore due) et que la capture a sauvé ce qu'il y avait laissé, c'est le
        contenu d'origine qu'il faudra remettre : sinon l'utilisateur garderait le texte de la fois d'avant. Toute
        restauration plus ancienne est dépassée."""
        with self._lock:
            if self._borrowed is not None and capture.saved_clipboard == self._borrowed[1]:
                capture.saved_clipboard = self._borrowed[0]
            capture.left = left
            self._borrowed = None if capture.saved_clipboard is None else (capture.saved_clipboard, left)
            self._generation += 1

    def _give_back(self, capture: Capture) -> None:
        """Remet le presse-papiers d'origine de `capture`, ou le fera à la fin de la capture en cours. Rien si
        elle n'y a pas touché ou si sa sauvegarde a échoué (None) : écrire un presse-papiers vide perdrait ce
        que l'utilisateur avait copié."""
        if not capture.clipboard_touched or capture.saved_clipboard is None:
            return
        with self._lock:
            if self._capturing:
                self._postponed.append(lambda: self._give_back(capture))
                return
            self._borrowed = None
        capture.clipboard_touched = False  # remis une fois pour toutes
        self._write_saved(capture.saved_clipboard)

    def _write_saved(self, saved: Any) -> None:
        _qt_set_clipboard(saved)

    # presse-papiers lu sans Qt (fil de la capture) : formats offerts ([] s'il est vide, None si on ne sait
    # pas) et texte (None si illisible)
    def _clipboard_formats(self) -> list[str] | None:
        return None

    def _clipboard_read(self) -> str | None:
        return None

    def _saved_text(self) -> str | None:
        """Texte du presse-papiers à remettre après coup : "" s'il est vide, None s'il contient autre chose que
        du texte (on ne sait remettre que du texte) ou s'il est illisible."""
        formats = self._clipboard_formats()
        if formats is None or not _text_only(formats):
            return None
        return self._clipboard_read() if formats else ""

    def _clipboard_before(self) -> tuple[bool, str | None, str | None]:
        """Presse-papiers avant une copie simulée : (utilisable, texte pour la comparaison, texte à remettre).
        Inutilisable s'il contient du texte qu'on n'a pas pu lire : sans contenu d'avant, l'ancien presse-papiers
        (un mot de passe, peut-être) passerait pour la copie de la sélection."""
        formats = self._clipboard_formats()
        if formats == []:
            return True, "", ""
        readable = formats is None or _has_text(formats)
        text = self._clipboard_read() if readable else None
        if readable and text is None:
            return False, None, None
        return True, text, text if formats is not None and _text_only(formats) else None

    def release(self, capture: Capture) -> None:
        """Fenêtre fermée sans remplacement : on rend le presse-papiers d'origine."""
        self._give_back(capture)

    # Remplacement depuis la fenêtre de correction : stage_replace est appelé pendant qu'elle est encore
    # visible et a le focus, paste_staged `staged_paste_delay_ms` plus tard. None (par défaut) : replace(),
    # une fois la fenêtre cachée.
    staged_paste_delay_ms = _WINDOW_FOCUS_RETURN_MS

    def stage_replace(self, capture: Capture, text: str) -> tuple[bool, str] | None:
        return None

    def paste_staged(self, capture: Capture) -> tuple[bool, str]:
        return False, "Texte corrigé copié : collez-le avec Ctrl+V."

    def replace(self, capture: Capture, text: str) -> tuple[bool, str]:
        """(collé ?, message). Un collage réussi peut aussi avoir un message : l'ancien presse-papiers n'est
        pas rendu tout de suite, ou ne le sera pas."""
        if not self.copy(text):
            return False, _COPY_FAILED
        return False, "Texte corrigé copié : collez-le avec Ctrl+V."

    def copy(self, text: str) -> bool:
        """Met `text` dans le presse-papiers. False si c'est impossible."""
        return _qt_set_clipboard(text)

    def clipboard_text(self) -> str:
        return _qt_clipboard().text()

    def bring_to_front(self, widget) -> None:
        widget.show()
        widget.raise_()
        widget.activateWindow()

    def diagnostics(self) -> list[str]:
        return [f"Session : {session_type()}"]


class WindowsBridge(Bridge):
    name = "windows"
    capture_in_thread = True

    def __init__(self, defer: Defer | None = None) -> None:
        super().__init__(defer)
        from correcteur.platform import win32

        self.w = win32
        self._listener = None

    def hotkeys_supported(self) -> tuple[bool, str]:
        return True, ""

    def start_hotkeys(self, bindings):
        self.stop_hotkeys()
        self._listener = self.w.HotkeyListener(bindings)
        return self._listener.start()

    def stop_hotkeys(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def capture(self) -> Capture:
        w = self.w
        started = time.monotonic()
        hwnd = w.foreground_window()
        w.wait_modifiers_released()
        saved = w.snapshot_clipboard()
        seq = w.clipboard_sequence()
        w.send_ctrl("c")
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline and w.clipboard_sequence() == seq:
            time.sleep(0.015)
        changed = w.clipboard_sequence() != seq
        text = ""
        if changed:
            time.sleep(0.02)  # certaines applications écrivent en plusieurs fois
            text = w.get_clipboard_text() or ""
        return Capture(text.replace("\r\n", "\n"), target=hwnd, from_selection=bool(text),
                       saved_clipboard=saved, clipboard_touched=changed, started=started)

    def _write_saved(self, saved: Any) -> None:
        self.w.set_clipboard_formats(saved)

    def replace(self, capture: Capture, text: str) -> tuple[bool, str]:
        w = self.w
        if not capture.target or not w.activate_window(capture.target):
            if not w.set_clipboard_text(text):
                return False, _COPY_FAILED
            return False, "Fenêtre d'origine introuvable : texte corrigé copié, collez-le avec Ctrl+V."
        time.sleep(0.06)
        if not w.set_clipboard_text(text):
            return False, _COPY_FAILED
        w.wait_modifiers_released(0.5)
        w.send_ctrl("v")
        capture.clipboard_touched = True
        self._defer(_RESTORE_DELAY_MS, lambda: self._give_back(capture))
        return True, ""

    def copy(self, text: str) -> bool:
        return self.w.set_clipboard_text(text)

    def clipboard_text(self) -> str:
        return (self.w.get_clipboard_text() or "").replace("\r\n", "\n")

    def bring_to_front(self, widget) -> None:
        super().bring_to_front(widget)
        try:
            self.w.activate_window(int(widget.winId()))
        except Exception:
            pass

    def diagnostics(self) -> list[str]:
        return ["Session : Windows", "Raccourcis : RegisterHotKey", "Sélection : Ctrl+C simulé (presse-papiers restauré)"]


def xtest_key(letter: str):
    """Touche pynput qui tape ``letter`` comme une vraie frappe sous X11.

    pynput envoie un caractère par XSendEvent : un évènement « synthétique » que certaines applications
    ignorent (xterm, par exemple) et que les raccourcis globaux ne voient pas. Donnée par son keysym, la
    touche passe par XTEST, comme le clavier. Si la disposition n'a pas cette touche, on garde le caractère.
    """
    from pynput.keyboard import KeyCode
    from Xlib import XK, display

    keysym = XK.string_to_keysym(letter)
    try:
        d = display.Display()
        try:
            if keysym and d.keysym_to_keycode(keysym):
                return KeyCode.from_vk(keysym)
        finally:
            d.close()
    except Exception:
        pass
    return letter


class X11Bridge(Bridge):
    name = "x11"

    @property
    def capture_in_thread(self) -> bool:  # type: ignore[override]
        try:
            import correcteur.platform.x11_selection  # noqa: F401  (sinon : repli sur le presse-papiers de Qt)
        except ImportError:
            return False
        return True

    def __init__(self, defer: Defer | None = None) -> None:
        super().__init__(defer)
        self._hotkeys = None
        self._owner = None

    def hotkeys_supported(self) -> tuple[bool, str]:
        try:
            import pynput  # noqa: F401
        except Exception as exc:
            return False, f"Module pynput indisponible ({exc})."
        return True, ""

    def start_hotkeys(self, bindings):
        self.stop_hotkeys()
        try:
            from pynput import keyboard

            self._hotkeys = keyboard.GlobalHotKeys(bindings)
            self._hotkeys.daemon = True
            self._hotkeys.start()
            return []
        except Exception as exc:
            log.warning("Raccourcis X11 indisponibles : %s", exc)
            self._hotkeys = None
            return list(bindings)

    def stop_hotkeys(self) -> None:
        if self._hotkeys is not None:
            try:
                self._hotkeys.stop()
            except Exception:
                pass
            self._hotkeys = None

    # fenêtres
    def _active_window(self) -> int | None:
        """Fenêtre active : _NET_ACTIVE_WINDOW (gestionnaire de fenêtres EWMH),
        sinon la fenêtre qui a le focus clavier (X11 de base)."""
        try:
            from Xlib import X, display

            d = display.Display()
            try:
                root = d.screen().root
                prop = root.get_full_property(d.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
                if prop is not None and len(prop.value) and int(prop.value[0]):
                    return int(prop.value[0])
                focus = d.get_input_focus().focus
                if hasattr(focus, "id") and focus.id not in (0, 1, root.id):
                    return int(focus.id)
            finally:
                d.close()
        except Exception:
            pass
        tool = which("xdotool")
        if tool:
            out = subprocess.run([tool, "getactivewindow"], capture_output=True, text=True, timeout=1, env=child_env())
            if out.returncode == 0 and out.stdout.strip().isdigit():
                return int(out.stdout.strip())
        return None

    def _activate(self, wid: int) -> bool:
        if self._active_window() == wid:
            return True
        try:
            from Xlib import X, display, protocol

            d = display.Display()
            try:
                root = d.screen().root
                atom = d.intern_atom("_NET_ACTIVE_WINDOW")
                window = d.create_resource_object("window", wid)
                if root.get_full_property(atom, X.AnyPropertyType) is not None:
                    event = protocol.event.ClientMessage(
                        window=window, client_type=atom,
                        data=(32, [2, X.CurrentTime, 0, 0, 0]),  # 2 = demande "pager", honorée par les gestionnaires de fenêtres
                    )
                    root.send_event(event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
                else:
                    # Pas de gestionnaire de fenêtres : on donne le focus directement.
                    window.set_input_focus(X.RevertToParent, X.CurrentTime)
                d.flush()
            finally:
                d.close()
        except Exception:
            tool = which("xdotool")
            if not tool:
                return False
            subprocess.run([tool, "windowactivate", "--sync", str(wid)], capture_output=True, timeout=2, env=child_env())
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            if self._active_window() == wid:
                return True
            time.sleep(0.03)
        return False

    @staticmethod
    def _send_keys(action: str) -> None:
        """Copie (Ctrl+Inser) ou collage (Ctrl+V) simulés par XTEST, voir _YDOTOOL_KEYS."""
        from pynput.keyboard import Controller, Key

        kb = Controller()
        for key in (Key.alt, Key.alt_r, Key.shift, Key.shift_r, Key.cmd):
            try:
                kb.release(key)
            except Exception:
                pass
        key = Key.insert if action == "copy" else xtest_key("v")
        with kb.pressed(Key.ctrl):
            kb.press(key)
            kb.release(key)

    # sélections : python-xlib (voir x11_selection), Qt en dernier recours
    def _read(self, selection: str) -> str | None:
        try:
            from correcteur.platform.x11_selection import read_selection
        except ImportError:
            from PySide6.QtGui import QClipboard, QGuiApplication

            mode = QClipboard.Mode.Selection if selection == "PRIMARY" else QClipboard.Mode.Clipboard
            return QGuiApplication.clipboard().text(mode)
        return read_selection(selection)

    def _write(self, text: str, qt_fallback: bool = True) -> bool:
        """Sans `qt_fallback`, jamais le presse-papiers de Qt (capture hors du fil de l'interface)."""
        try:
            if self._owner is None:
                from correcteur.platform.x11_selection import SelectionOwner

                self._owner = SelectionOwner()
            if self._owner.set_text(text):
                return True
        except Exception as exc:
            log.debug("Presse-papiers X11 via python-xlib impossible : %s", exc)
        return qt_fallback and _qt_set_clipboard(text)

    def _clipboard_formats(self) -> list[str] | None:
        try:
            from correcteur.platform.x11_selection import selection_targets
        except ImportError:
            # Sans python-xlib, la capture se fait dans le fil de l'interface : Qt est permis.
            data = _qt_clipboard().mimeData()
            return list(data.formats()) if data is not None else None
        return selection_targets("CLIPBOARD")  # cible TARGETS

    def _clipboard_read(self) -> str | None:
        return self._read("CLIPBOARD")

    def _primary_from(self, window: int | None) -> bool:
        """La sélection primaire appartient-elle à l'application de `window` ? Elle reste celle de la dernière
        application où l'on a sélectionné du texte, même après être passé à une autre fenêtre."""
        if window is None:
            return False
        try:
            from correcteur.platform.x11_selection import selection_owned_by_client_of
        except ImportError:
            return False  # sans python-xlib, impossible de savoir : copie simulée
        return selection_owned_by_client_of("PRIMARY", window)

    def capture(self) -> Capture:
        started = time.monotonic()
        target = self._active_window()
        if self._primary_from(target):
            text = self._read("PRIMARY") or ""
            if text.strip():
                return Capture(text, target=target, from_selection=True, started=started)
        # Pas de sélection primaire de l'application active : copie simulée (Ctrl+Inser). Le presse-papiers n'est
        # marqué (voir _clipboard_marker) que s'il ne contient que du texte qu'on a pu lire ; sinon, on compare avec
        # son contenu d'avant.
        usable, before, saved = self._clipboard_before()
        if not usable:
            return Capture("", target=target, started=started)
        marker = _clipboard_marker()
        marked = saved is not None and self._write_marker(marker)
        if marked:
            before = None  # le marqueur n'offre aucun texte : tout texte lu ensuite vient de la copie
        time.sleep(0.2)
        if marked and not self._owner.owns():
            # Un autre programme a pris le presse-papiers depuis le marqueur : l'utilisateur vient de copier autre
            # chose. On ne copie pas par-dessus, et il n'y a rien à remettre.
            log.info("Presse-papiers changé pendant la capture : copie simulée annulée.")
            return Capture("", target=target, started=started)
        try:
            self._send_keys("copy")
        except Exception:
            return Capture("", target=target, saved_clipboard=saved, clipboard_touched=marked, started=started,
                           left=marker if marked else None)
        deadline = time.monotonic() + 0.8
        text = before
        while time.monotonic() < deadline:
            time.sleep(0.04)
            text = self._read("CLIPBOARD")
            if text and text != before:
                break
        changed = bool(text) and text != before
        return Capture(text if changed else "", target=target, from_selection=changed,
                       saved_clipboard=saved, clipboard_touched=changed or marked, started=started,
                       left=text if changed else (marker if marked else None))

    def _write_marker(self, marker: str) -> bool:
        """Marqueur sous un format à nous (_MARKER_TYPE), par python-xlib seulement (capture hors du fil de
        l'interface) ; False sans python-xlib : on comparera avec le contenu d'avant."""
        try:
            if self._owner is None:
                from correcteur.platform.x11_selection import SelectionOwner

                self._owner = SelectionOwner()
            return self._owner.set_data(marker.encode("utf-8"), _MARKER_TYPE)
        except Exception as exc:
            log.debug("Marqueur X11 impossible : %s", exc)
            return False

    def _write_saved(self, saved: Any) -> None:
        self._write(saved)

    def replace(self, capture: Capture, text: str) -> tuple[bool, str]:
        if capture.saved_clipboard is None and not capture.clipboard_touched:
            capture.saved_clipboard = self._saved_text()
        if not self._write(text):
            return False, _COPY_FAILED
        capture.clipboard_touched = True
        self._borrow(capture, text)
        if capture.target and not self._activate(capture.target):
            return False, "Fenêtre d'origine introuvable : texte corrigé copié, collez-le avec Ctrl+V."
        time.sleep(0.05)
        try:
            self._send_keys("paste")
        except Exception as exc:
            return False, f"Collage automatique impossible ({exc}) : texte copié, faites Ctrl+V."
        if capture.saved_clipboard is None:
            return True, _CLIPBOARD_LOST
        self._defer(_RESTORE_DELAY_MS, lambda: self._give_back(capture))
        return True, ""

    def copy(self, text: str) -> bool:
        return self._write(text)

    def clipboard_text(self) -> str:
        return self._read("CLIPBOARD") or ""

    def diagnostics(self) -> list[str]:
        ok, why = self.hotkeys_supported()
        return [
            "Session : X11",
            "Raccourcis : " + ("pynput" if ok else why),
            "Sélection : sélection primaire X11 de l'application active (sinon Ctrl+Inser simulé)",
            "xdotool : " + ("oui" if which("xdotool") else "non (facultatif)"),
        ]


def _nothing_copied(stderr: str | None) -> bool:
    """Message de wl-paste quand le presse-papiers est vide (selon sa version)."""
    return any(message in (stderr or "") for message in ("Nothing is copied", "No selection"))


def _preferred_type(formats: list[str]) -> str | None:
    """Format à garder quand le presse-papiers n'est pas du texte simple : une image (PNG d'abord) ou des
    fichiers copiés, même s'ils offrent aussi du texte (leur nom, leur chemin) ; à défaut de texte, le premier
    format offert. None : le texte suffit."""
    if "image/png" in formats:
        return "image/png"
    for wanted in (lambda f: f.startswith("image/"), lambda f: f == "x-special/gnome-copied-files",
                   lambda f: f == "text/uri-list"):
        found = next((f for f in formats if wanted(f)), None)
        if found:
            return found
    if _has_text(formats):
        return None
    return next((f for f in formats if f not in _TEXT_FORMATS), None)


def _read_limited(stream, limit: int, timeout: float) -> bytes | None:
    """Lit `stream` jusqu'au bout ; None au-delà de `limit` octets ou de `timeout` secondes."""
    fd, chunks, size = stream.fileno(), [], 0
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            return None
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)


def _gnome_display(binding: str) -> str:
    """"<Control><Alt>c" (GNOME) -> "Ctrl+Alt+C"."""
    names = {"control": "Ctrl", "primary": "Ctrl", "alt": "Alt", "shift": "Maj", "super": "Win"}
    parts = [names.get(m.lower(), m) for m in re.findall(r"<([^>]+)>", binding)]
    key = re.sub(r"<[^>]+>", "", binding)
    return "+".join(parts + [key.upper() if len(key) == 1 else key])


@dataclass
class SavedClipboard:
    """Contenu du presse-papiers qui n'est pas du texte simple (image, fichiers copiés...), gardé tel quel."""
    mime: str
    data: bytes


@dataclass
class _Restore:
    """Restauration du presse-papiers après un collage ou une capture (voir WaylandBridge._restore_later)."""
    saved: Any                  # contenu d'origine : texte ou SavedClipboard
    left: Any                   # ce que le correcteur a laissé, qui doit encore y être
    generation: int             # dépassée par un remplacement ou une capture plus récents : annulée
    due: float                  # time.monotonic() à partir duquel restaurer
    main_check: Callable[[], bool] | None = None   # indice lu dans le fil de l'interface (Qt propriétaire ?)
    sure_check: Callable[[], bool] | None = None   # preuve, lisible de tout fil (processus wl-copy vivant)


class WaylandBridge(Bridge):
    """Wayland interdit aux applications d'espionner le clavier ou les autres
    fenêtres : le raccourci se déclare dans les paramètres du bureau (commande
    `correcteur selection`), la sélection se copie par un Ctrl+Inser simulé (wl-paste,
    wl-copy) et le collage se fait avec wtype ou ydotool s'ils sont installés.

    Depuis la fenêtre de correction, qui a le focus, le texte corrigé est posé par
    Qt (stage_replace) ; en correction express, par wl-copy. Le presse-papiers
    d'origine, image comprise, est remis par wl-copy peu après le collage, si le
    correcteur possède encore ce qu'il y a laissé (_restore_later)."""

    name = "wayland"
    capture_in_thread = True  # wl-paste, wtype, ydotool : des programmes externes

    def __init__(self, defer: Defer | None = None) -> None:
        super().__init__(defer)
        self._restoring = False  # wl-copy d'une restauration en cours (voir _wait_restoring)
        self._copies: list[subprocess.Popen] = []  # wl-copy --foreground de la correction express
        # Lance la fin d'une restauration (attente, relecture, wl-copy) hors du fil de l'interface.
        self._spawn: Callable[[Callable[[], None]], None] = (
            lambda fn: threading.Thread(target=fn, name="restauration", daemon=True).start())

    def hotkeys_supported(self) -> tuple[bool, str]:
        return False, ("Sous Wayland, le raccourci se crée dans les paramètres du bureau avec la commande "
                       "« correcteur selection » (voir Paramètres > Général).")

    @staticmethod
    def _tools_take_focus() -> bool:
        """wl-copy et wl-paste prennent le focus un instant (GNOME : pas de protocole data-control)."""
        return "gnome" in desktop_name()

    @staticmethod
    def _exec(cmd: list[str], data: str | bytes | None = None, timeout: float = 1.5,
              output: bool = True) -> subprocess.CompletedProcess | None:
        """Lance le programme ; None s'il ne démarre pas ou dépasse le délai. `data` en octets : échange binaire.

        Sans `output`, ses sorties vont vers /dev/null : wl-copy laisse en arrière-plan un processus qui sert le
        presse-papiers en gardant ses sorties ouvertes, et attendre leur fin bloquerait jusqu'au délai."""
        streams = {"capture_output": True} if output else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        text = {} if isinstance(data, bytes) else {"encoding": "utf-8", "errors": "replace"}
        try:
            return subprocess.run(cmd, input=data, timeout=timeout, env=child_env(), **text, **streams)
        except (OSError, subprocess.TimeoutExpired):
            return None

    @classmethod
    def _run(cls, cmd: list[str], data: str | bytes | None = None, timeout: float = 1.5,
             output: bool = True) -> str | None:
        """Sortie du programme (chaîne vide sans `output`), None s'il échoue."""
        out = cls._exec(cmd, data, timeout, output)
        if out is None or out.returncode != 0:
            return None
        return out.stdout if output else ""

    def _paste(self, primary: bool) -> str | None:
        """Texte du presse-papiers (ou de la sélection primaire) : "" s'il est vide, None si la lecture a échoué
        (wl-paste absent, délai dépassé, contenu qui n'est pas du texte). Une sauvegarde None n'est jamais
        restaurée : remettre "" effacerait ce que l'utilisateur avait copié."""
        tool = which("wl-paste")
        if not tool:
            return None
        cmd = [tool, "--no-newline", "--type", "text"]
        if primary:
            cmd.insert(1, "--primary")
        out = self._exec(cmd)
        if out is None:
            return None
        if out.returncode == 0:
            return out.stdout
        return "" if _nothing_copied(out.stderr) else None

    def _paste_bytes(self, mime: str) -> bytes | None:
        """Contenu du presse-papiers dans le format `mime`, tel quel ; None s'il est illisible ou dépasse
        _MAX_SAVED octets (rien ne sera restauré)."""
        tool = which("wl-paste")
        if not tool:
            return None
        try:
            proc = subprocess.Popen([tool, "--type", mime], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=child_env())
        except OSError:
            return None
        try:
            data = _read_limited(proc.stdout, _MAX_SAVED, 3.0)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
        return data if proc.returncode == 0 else None

    def _clipboard_types(self) -> list[str] | None:
        """Formats offerts par le presse-papiers ([] s'il est vide), None si on ne sait pas."""
        tool = which("wl-paste")
        if not tool:
            return None
        out = self._exec([tool, "--list-types"])
        if out is None:
            return None
        if out.returncode == 0:
            return [line.strip() for line in out.stdout.splitlines() if line.strip()]
        return [] if _nothing_copied(out.stderr) else None

    def _clipboard_before(self) -> tuple[bool, Any, Any]:
        """Presse-papiers avant une copie simulée : (utilisable, texte pour la comparaison, contenu à remettre :
        texte, SavedClipboard, ou None s'il n'a pas pu être lu). Inutilisable si on ne sait pas ce qu'il contient,
        ou s'il contient du texte illisible : l'ancien presse-papiers (un mot de passe, peut-être) passerait pour
        la copie de la sélection. Texte de comparaison _UNREAD : une image ou des fichiers, sauvés tels quels,
        dont le texte n'a pas été lu (le marqueur le rend inutile, et une lecture de moins fait gagner du temps)."""
        formats = self._clipboard_types()
        if formats is None:
            return False, None, None
        if not formats:
            return True, "", ""
        mime = _preferred_type(formats)
        if mime is not None:
            data = self._paste_bytes(mime)
            if data is not None:
                return True, _UNREAD if _has_text(formats) else None, SavedClipboard(mime, data)
        if not _has_text(formats):
            return True, None, None
        text = self._paste(primary=False)
        if text is None:
            return False, None, None
        return True, text, text if mime is None else None

    def _still_left(self, left: Any) -> bool:
        """Le presse-papiers contient-il encore ce que le correcteur y a laissé ? (relu par wl-paste)"""
        if isinstance(left, SavedClipboard):
            formats = self._clipboard_types() or []
            return left.mime in formats and (left.mime == _MARKER_TYPE or self._paste_bytes(left.mime) == left.data)
        return self._paste(primary=False) == left

    def _set_clipboard(self, text: str, qt_fallback: bool = True) -> bool:
        """Sans `qt_fallback`, jamais le presse-papiers de Qt (hors du fil de l'interface)."""
        tool = which("wl-copy")
        if tool:
            return self._run([tool], data=text, output=False) is not None
        return qt_fallback and _qt_set_clipboard(text)

    def _write_clipboard(self, saved: Any) -> bool:
        """Remet un contenu sauvegardé, par wl-copy (texte, presse-papiers vide, ou format d'origine)."""
        tool = which("wl-copy")
        if not tool:
            return False
        if isinstance(saved, SavedClipboard):
            return self._run([tool, "--type", saved.mime], data=saved.data, timeout=3.0, output=False) is not None
        if saved == "":
            return self._run([tool, "--clear"], output=False) is not None
        return self._run([tool], data=saved, output=False) is not None

    def _keep_copy(self, data: bytes, mime: str | None = None) -> subprocess.Popen | None:
        """wl-copy --foreground : le processus reste là tant qu'il possède le presse-papiers et s'arrête dès
        qu'un autre programme copie quelque chose. Vivant, il prouve que ce qu'on y a mis y est encore."""
        tool = which("wl-copy")
        if not tool:
            return None
        self._copies = [proc for proc in self._copies if proc.poll() is None]
        cmd = [tool, "--foreground"] + (["--type", mime] if mime else [])
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=child_env())
            proc.stdin.write(data)
            proc.stdin.close()
        except OSError:
            return None
        self._copies.append(proc)
        return proc

    def _send_keys(self, action: str) -> bool:
        """Copie (Ctrl+Inser) ou collage (Ctrl+V) simulés, voir _YDOTOOL_KEYS."""
        wtype = which("wtype")
        if wtype and "gnome" not in desktop_name():
            if self._run([wtype, "-M", "ctrl", *_WTYPE_KEYS[action], "-m", "ctrl"]) is not None:
                return True
        ydotool = which("ydotool")
        if ydotool:
            code = _YDOTOOL_KEYS[action]
            if self._run([ydotool, "key", *_YDOTOOL_RELEASE, "29:1", f"{code}:1", f"{code}:0", "29:0"]) is not None:
                return True
        return False

    def _wait_for_keys(self, started: float, copy: subprocess.Popen | None = None) -> bool:
        """Avant une frappe simulée : l'utilisateur doit avoir relâché les touches du raccourci, et le focus être
        revenu à l'application après wl-paste ou wl-copy (voir _FOCUS_RETURN_S). False si le wl-copy `copy`
        s'est arrêté entre-temps (échec, ou autre copie : coller ne collerait pas le texte corrigé)."""
        focus = _FOCUS_RETURN_S if self._tools_take_focus() else 0.25
        deadline = max(started + _KEYS_RELEASE_S, time.monotonic() + focus)
        while True:
            if copy is not None and copy.poll() is not None:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.05, remaining))

    def _wait_restoring(self) -> None:
        """Attend la fin d'un wl-copy de restauration déjà parti (2 s au plus) : écrire en même temps, c'est
        risquer que l'ancien contenu passe après."""
        end = time.monotonic() + 2.0
        while self._restoring and time.monotonic() < end:
            time.sleep(0.02)

    def capture(self) -> Capture:
        started = time.monotonic()
        self._wait_restoring()
        # Jamais la sélection primaire : c'est celle de la dernière application où l'on a sélectionné, pas
        # forcément celle qui est active (Ctrl+Alt+X collait dans Writer un texte sélectionné dans Zen). Toujours
        # une copie simulée (Ctrl+Inser), après avoir marqué le presse-papiers (voir _clipboard_marker) : sans
        # sélection dans l'application active, le marqueur reste et rien n'est capturé. Marqué seulement si son
        # contenu a pu être sauvé, et seulement par wl-copy : le presse-papiers de Qt est réservé au fil de
        # l'interface.
        usable, before, saved = self._clipboard_before()
        if not usable:
            return Capture("", started=started)
        marker = _clipboard_marker().encode("utf-8")
        owner = self._keep_copy(marker, _MARKER_TYPE) if saved is not None else None
        if owner is None and before is _UNREAD:
            return Capture("", started=started)  # ni marqueur ni contenu d'avant pour comparer
        if not self._wait_for_keys(started, owner):
            # Le marqueur a perdu le presse-papiers avant la copie simulée : l'utilisateur vient de copier autre
            # chose (ou wl-copy a échoué). Rien n'est copié par-dessus, et il n'y a rien à remettre.
            log.info("Presse-papiers changé pendant la capture : copie simulée annulée.")
            return Capture("", started=started)
        left = SavedClipboard(_MARKER_TYPE, marker) if owner is not None else None
        if not self._send_keys("copy"):
            return Capture("", saved_clipboard=saved, clipboard_touched=owner is not None, started=started, left=left,
                           owner=owner)
        limit = 1.2 if self._tools_take_focus() else 0.6
        deadline = time.monotonic() + limit
        if owner is not None:
            # L'application copie : le wl-copy du marqueur perd le presse-papiers et s'arrête. Rien à relire
            # avant (sous GNOME, chaque wl-paste prendrait le focus un instant).
            while owner.poll() is None:
                if time.monotonic() >= deadline:
                    return Capture("", saved_clipboard=saved, clipboard_touched=True, started=started, left=left,
                                   owner=owner)
                time.sleep(0.02)
            text = self._paste(primary=False)
            return Capture(text or "", from_selection=bool(text), saved_clipboard=saved, clipboard_touched=True,
                           started=started, left=text)
        # Sans marqueur : comparaison avec le contenu d'avant. Sous GNOME, on lit moins souvent.
        first, step = (0.25, 0.15) if self._tools_take_focus() else (0.05, 0.05)
        time.sleep(first)
        while True:
            text = self._paste(primary=False)
            if text and text != before:
                return Capture(text, from_selection=True, saved_clipboard=saved, clipboard_touched=True,
                               started=started, left=text)
            if time.monotonic() >= deadline:
                return Capture("", started=started)
            time.sleep(step)

    def focused_capture(self) -> Capture | None:
        """Le correcteur a le focus : sélection primaire lue par Qt. Sous GNOME, wl-paste prendrait le focus à
        la fenêtre le temps de la lecture ; et Qt, qui a le focus, lit sans détour."""
        from PySide6.QtGui import QClipboard

        clipboard = _qt_clipboard()
        if not clipboard.supportsSelection():
            return None
        text = clipboard.text(QClipboard.Mode.Selection)
        return Capture(text, from_selection=bool(text.strip()), started=time.monotonic())

    def _give_back(self, capture: Capture) -> None:
        self._restore_later(capture, 0.0)

    def _settle_delay(self) -> float:
        """Après le Ctrl+V, le temps que l'application lise le collage (et sous GNOME, que le focus soit bien
        revenu) avant de lui reprendre le presse-papiers."""
        return _RESTORE_AFTER_PASTE_GNOME_S if self._tools_take_focus() else _RESTORE_AFTER_PASTE_S

    def _restore_later(self, capture: Capture, delay: float, main_check: Callable[[], bool] | None = None,
                       sure_check: Callable[[], bool] | None = None) -> None:
        """Remet le presse-papiers d'origine de `capture` par wl-copy, `delay` secondes plus tard (fil de
        l'interface). La restauration n'a lieu que si elle est toujours la plus récente, qu'aucune capture n'est en
        cours, que le correcteur possède encore ce qu'il a laissé et, sous GNOME, après une pause de saisie (la
        surface de wl-copy ne doit pas prendre une frappe). Sinon, au plus tard _RESTORE_GIVE_UP_S secondes après
        l'heure prévue, elle est abandonnée : jamais de restauration qui traîne."""
        saved = capture.saved_clipboard
        if not capture.clipboard_touched or saved is None:
            return
        capture.clipboard_touched = False  # une seule restauration par capture
        owner = capture.owner
        if sure_check is None and owner is not None:
            sure_check = lambda: owner.poll() is None  # noqa: E731  (wl-copy vivant : il possède encore ce qu'il a mis)
        with self._lock:
            job = _Restore(saved, capture.left, self._generation, time.monotonic() + delay, main_check, sure_check)
        self._defer(int(delay * 1000), lambda: self._start_restore(job))

    def _is_current(self, job: _Restore) -> bool:
        with self._lock:
            return job.generation == self._generation

    def _forget(self, job: _Restore, why: str = "") -> None:
        """Restauration finie ou abandonnée : plus rien n'est dû (une capture suivante n'en hérite pas)."""
        if why:
            log.info("Presse-papiers d'origine non remis : %s.", why)
        with self._lock:
            if job.generation == self._generation:
                self._borrowed = None

    def _start_restore(self, job: _Restore) -> None:
        """Heure prévue (fil de l'interface) : indice de Qt, puis la suite dans un fil de travail (attente de la
        pause de saisie, relecture, wl-copy)."""
        if not self._is_current(job):
            return
        if time.monotonic() > job.due + _RESTORE_GIVE_UP_S:
            self._forget(job, "restauration en retard")
            return
        if job.main_check is not None and not job.main_check():
            self._forget(job, "un autre programme a copié quelque chose depuis")
            return
        self._spawn(lambda: self._restore_worker(job))

    def _restore_worker(self, job: _Restore) -> None:
        _sleep_until(job.due)  # le minuteur de Qt peut partir un peu en avance
        deadline = job.due + _RESTORE_GIVE_UP_S
        why = ""
        while True:
            if not self._is_current(job):
                return  # dépassée par un remplacement ou une capture plus récents
            if time.monotonic() > deadline:
                self._forget(job, f"abandonné après {_RESTORE_GIVE_UP_S:g} s ({why or 'délai dépassé'})")
                return
            with self._lock:
                capturing = self._capturing
            if capturing:
                why = "capture en cours"
                time.sleep(0.1)
                continue
            idle = self._idle_ms()
            if idle is not None and idle < _IDLE_BEFORE_RESTORE_MS:
                why = f"pas de pause de saisie de {_IDLE_BEFORE_RESTORE_MS} ms"
                time.sleep(max(0.05, (_IDLE_BEFORE_RESTORE_MS - idle) / 1000))
                continue
            if job.sure_check is not None:
                if not job.sure_check():
                    self._forget(job, "un autre programme a copié quelque chose depuis")
                    return
            elif not self._still_left(job.left):
                self._forget(job, "le presse-papiers a changé depuis")
                return
            with self._lock:
                if self._capturing or job.generation != self._generation:
                    continue  # une capture vient de commencer : on revérifie tout
                self._restoring = True
            try:
                if not self._write_clipboard(job.saved):
                    log.warning("Presse-papiers d'origine non remis : wl-copy a échoué.")
            finally:
                self._restoring = False
            self._forget(job)
            return

    def _idle_ms(self) -> int | None:
        """Temps depuis la dernière frappe ou le dernier mouvement de souris (GNOME), None si inconnu."""
        if not self._tools_take_focus():
            return None
        tool = which("gdbus")
        if not tool:
            return None
        out = self._run([tool, "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
                         "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                         "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"], timeout=1.0)
        match = re.search(r"uint64\s+(\d+)", out or "")  # réponse : « (uint64 1234,) »
        return int(match.group(1)) if match else None

    def stage_replace(self, capture: Capture, text: str) -> tuple[bool, str] | None:
        """Remplacement depuis la fenêtre de correction, appelé pendant qu'elle a encore le focus : texte corrigé
        posé par Qt. Sous GNOME, wl-copy créerait une surface pour obtenir le focus, et l'application d'origine ne
        l'aurait pas retrouvé au moment du Ctrl+V. Aucune fenêtre du correcteur n'a le focus (remplacement qui
        attendait les moteurs) : None, et replace() passe par wl-copy."""
        if not _qt_focused():
            return None
        if capture.saved_clipboard is None and not capture.clipboard_touched:
            capture.saved_clipboard = _qt_saved_text()  # le correcteur a le focus : Qt voit le presse-papiers actuel
        self._wait_restoring()
        if not _qt_set_clipboard(text):
            return False, _COPY_FAILED
        capture.clipboard_touched = True
        capture.owner = None  # posé par Qt : la restauration relira le contenu
        self._borrow(capture, text)
        return True, ""

    def paste_staged(self, capture: Capture) -> tuple[bool, str]:
        """Ctrl+V, `staged_paste_delay_ms` après stage_replace, quand l'application d'origine a repris le focus.
        Le correcteur possède le presse-papiers : c'est le fil de l'interface, resté libre, qui sert le collage."""
        if not self._send_keys("paste"):
            return False, _NO_PASTE_TOOL
        # Qt dit-il encore posséder le presse-papiers ? Simple indice : le contenu est relu avant d'écrire.
        self._restore_later(capture, self._settle_delay(), main_check=_qt_owns_clipboard)
        return True, _CLIPBOARD_LOST if capture.saved_clipboard is None else ""

    def replace(self, capture: Capture, text: str) -> tuple[bool, str]:
        """Correction express : aucune fenêtre du correcteur n'a le focus, le texte passe par wl-copy."""
        if capture.saved_clipboard is None and not capture.clipboard_touched:
            # Le correcteur a le focus : Qt voit le presse-papiers actuel. Sinon wl-paste, jamais l'avis de Qt sur
            # sa propriété, qui peut rester vrai après une copie faite ailleurs.
            capture.saved_clipboard = _qt_saved_text() if _qt_focused() else self._clipboard_before()[2]
        self._wait_restoring()
        copy = self._keep_copy(text.encode("utf-8"))
        if copy is None and not self._set_clipboard(text):
            return False, "Copie du texte corrigé impossible (wl-copy a échoué) : rien n'a été collé."
        capture.clipboard_touched = True
        capture.owner = copy
        self._borrow(capture, text)
        if not self._wait_for_keys(capture.started, copy):
            return False, "Copie du texte corrigé impossible (wl-copy a échoué) : rien n'a été collé."
        if not self._send_keys("paste"):
            return False, _NO_PASTE_TOOL
        self._restore_later(capture, self._settle_delay())
        return True, _CLIPBOARD_LOST if capture.saved_clipboard is None else ""

    def copy(self, text: str) -> bool:
        # Fenêtre du correcteur au premier plan (bouton Copier) : par Qt, sans surface de wl-copy.
        if _qt_focused():
            return _qt_set_clipboard(text)
        return self._set_clipboard(text)

    def clipboard_text(self) -> str:
        if _qt_focused():
            return _qt_clipboard().text()
        return self._paste(primary=False) or ""

    def _shortcuts_line(self) -> str:
        shortcuts = self.desktop_shortcuts
        if shortcuts is None and "gnome" in desktop_name():
            try:
                from correcteur.platform.autostart import gnome_shortcuts

                shortcuts = gnome_shortcuts()
            except Exception:
                shortcuts = None
        labels = {"correcteur-verifier": "vérifier", "correcteur-express": "correction express"}
        found = [f"{_gnome_display(binding)} ({labels.get(ident, ident)})" for ident, binding in (shortcuts or {}).items()]
        if found:
            return "Raccourcis : GNOME, " + ", ".join(found)
        return "Raccourcis : à créer dans les paramètres du bureau (commande « correcteur selection »)"

    def diagnostics(self) -> list[str]:
        return [
            f"Session : Wayland ({desktop_name() or 'bureau inconnu'})",
            self._shortcuts_line(),
            "wl-clipboard : " + ("oui" if which("wl-paste") else "NON (paquet wl-clipboard requis)"),
            "wtype : " + ("oui" if which("wtype") else "non"),
            "ydotool : " + ("oui" if which("ydotool") else "non"),
        ]


def create_bridge(defer: Defer | None = None) -> Bridge:
    kind = session_type()
    try:
        if kind == "windows":
            return WindowsBridge(defer)
        if kind == "x11":
            return X11Bridge(defer)
        if kind == "wayland":
            return WaylandBridge(defer)
    except Exception as exc:  # pragma: no cover
        log.warning("Intégration système indisponible : %s", exc)
    return Bridge(defer)
