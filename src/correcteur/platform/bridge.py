"""Récupérer le texte sélectionné dans n'importe quelle application et le
remplacer par le texte corrigé.

Toutes les méthodes (sauf les rappels des raccourcis) s'exécutent dans le
thread de l'interface. `defer(ms, fonction)` programme une action différée
dans ce même thread (fourni par l'application Qt)."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from correcteur.platform import desktop_name, session_type, which

log = logging.getLogger(__name__)

Defer = Callable[[int, Callable[[], None]], None]
_RESTORE_DELAY_MS = 1200


@dataclass
class Capture:
    text: str
    target: Any = None             # fenêtre d'origine (HWND Windows, identifiant X11)
    from_selection: bool = False   # le texte vient bien d'une sélection
    saved_clipboard: Any = None    # contenu du presse-papiers à restaurer
    clipboard_touched: bool = False


class Bridge:
    name = "aucun"

    def __init__(self, defer: Defer | None = None) -> None:
        self._defer = defer or (lambda ms, fn: threading.Timer(ms / 1000, fn).start())

    # raccourcis
    def hotkeys_supported(self) -> tuple[bool, str]:
        return False, "Raccourcis globaux indisponibles sur ce système."

    def start_hotkeys(self, bindings: dict[str, Callable[[], None]]) -> list[str]:
        return list(bindings)

    def stop_hotkeys(self) -> None:
        pass

    # sélection
    def capture(self) -> Capture:
        return Capture("")

    def release(self, capture: Capture) -> None:
        """Fenêtre fermée sans remplacement : on rend le presse-papiers d'origine."""

    def replace(self, capture: Capture, text: str) -> tuple[bool, str]:
        self.copy(text)
        return False, "Texte corrigé copié : collez-le avec Ctrl+V."

    def copy(self, text: str) -> None:
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.clipboard().setText(text)

    def clipboard_text(self) -> str:
        from PySide6.QtGui import QGuiApplication

        return QGuiApplication.clipboard().text()

    def bring_to_front(self, widget) -> None:
        widget.show()
        widget.raise_()
        widget.activateWindow()

    def diagnostics(self) -> list[str]:
        return [f"Session : {session_type()}"]


# -- Windows ---------------------------------------------------------------

class WindowsBridge(Bridge):
    name = "windows"

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
                       saved_clipboard=saved, clipboard_touched=changed)

    def _restore_later(self, capture: Capture) -> None:
        saved = capture.saved_clipboard
        if saved is None or not capture.clipboard_touched:
            return
        self._defer(_RESTORE_DELAY_MS, lambda: self.w.set_clipboard_formats(saved))

    def release(self, capture: Capture) -> None:
        if capture.clipboard_touched and capture.saved_clipboard is not None:
            self.w.set_clipboard_formats(capture.saved_clipboard)

    def replace(self, capture: Capture, text: str) -> tuple[bool, str]:
        w = self.w
        if not capture.target or not w.activate_window(capture.target):
            w.set_clipboard_text(text)
            return False, "Fenêtre d'origine introuvable : texte corrigé copié, collez-le avec Ctrl+V."
        time.sleep(0.06)
        w.set_clipboard_text(text)
        w.wait_modifiers_released(0.5)
        w.send_ctrl("v")
        capture.clipboard_touched = True
        self._restore_later(capture)
        return True, ""

    def copy(self, text: str) -> None:
        self.w.set_clipboard_text(text)

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


# -- Linux X11 -------------------------------------------------------------

class X11Bridge(Bridge):
    name = "x11"

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
            out = subprocess.run([tool, "getactivewindow"], capture_output=True, text=True, timeout=1)
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
            subprocess.run([tool, "windowactivate", "--sync", str(wid)], capture_output=True, timeout=2)
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            if self._active_window() == wid:
                return True
            time.sleep(0.03)
        return False

    @staticmethod
    def _send_ctrl(letter: str) -> None:
        from pynput.keyboard import Controller, Key

        kb = Controller()
        for key in (Key.alt, Key.alt_r, Key.shift, Key.shift_r, Key.cmd):
            try:
                kb.release(key)
            except Exception:
                pass
        with kb.pressed(Key.ctrl):
            kb.press(letter)
            kb.release(letter)

    # sélections : python-xlib (voir x11_selection), Qt en dernier recours
    def _read(self, selection: str) -> str | None:
        try:
            from correcteur.platform.x11_selection import read_selection
        except ImportError:
            from PySide6.QtGui import QClipboard, QGuiApplication

            mode = QClipboard.Mode.Selection if selection == "PRIMARY" else QClipboard.Mode.Clipboard
            return QGuiApplication.clipboard().text(mode)
        return read_selection(selection)

    def _write(self, text: str) -> None:
        try:
            if self._owner is None:
                from correcteur.platform.x11_selection import SelectionOwner

                self._owner = SelectionOwner()
            if self._owner.set_text(text):
                return
        except Exception as exc:
            log.debug("Presse-papiers X11 via python-xlib impossible : %s", exc)
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.clipboard().setText(text)

    def capture(self) -> Capture:
        target = self._active_window()
        text = self._read("PRIMARY") or ""
        if text.strip():
            return Capture(text, target=target, from_selection=True)
        # Rien dans la sélection primaire : Ctrl+C simulé.
        saved = self._read("CLIPBOARD")
        time.sleep(0.2)
        try:
            self._send_ctrl("c")
        except Exception:
            return Capture("", target=target)
        deadline = time.monotonic() + 0.8
        text = saved
        while time.monotonic() < deadline:
            time.sleep(0.04)
            text = self._read("CLIPBOARD")
            if text != saved:
                break
        changed = bool(text) and text != saved
        return Capture(text if changed else "", target=target, from_selection=changed,
                       saved_clipboard=saved, clipboard_touched=changed)

    def release(self, capture: Capture) -> None:
        if capture.clipboard_touched and capture.saved_clipboard is not None:
            self._write(capture.saved_clipboard)

    def replace(self, capture: Capture, text: str) -> tuple[bool, str]:
        if capture.saved_clipboard is None:
            capture.saved_clipboard = self._read("CLIPBOARD")
        self._write(text)
        capture.clipboard_touched = True
        if capture.target and not self._activate(capture.target):
            return False, "Fenêtre d'origine introuvable : texte corrigé copié, collez-le avec Ctrl+V."
        time.sleep(0.05)
        try:
            self._send_ctrl("v")
        except Exception as exc:
            return False, f"Collage automatique impossible ({exc}) : texte copié, faites Ctrl+V."
        saved = capture.saved_clipboard
        if saved is not None:
            self._defer(_RESTORE_DELAY_MS, lambda: self._write(saved))
        return True, ""

    def copy(self, text: str) -> None:
        self._write(text)

    def clipboard_text(self) -> str:
        return self._read("CLIPBOARD") or ""

    def diagnostics(self) -> list[str]:
        ok, why = self.hotkeys_supported()
        return [
            "Session : X11",
            "Raccourcis : " + ("pynput" if ok else why),
            "Sélection : sélection primaire X11 (repli : Ctrl+C simulé)",
            "xdotool : " + ("oui" if which("xdotool") else "non (facultatif)"),
        ]


# -- Linux Wayland -----------------------------------------------------------

class WaylandBridge(Bridge):
    """Wayland interdit aux applications d'espionner le clavier ou les autres
    fenêtres : le raccourci se déclare dans les paramètres du bureau (commande
    `correcteur selection`), la sélection se lit avec wl-paste et le collage se
    fait avec wtype ou ydotool s'ils sont installés."""

    name = "wayland"

    def hotkeys_supported(self) -> tuple[bool, str]:
        return False, ("Sous Wayland, le raccourci se crée dans les paramètres du bureau avec la commande "
                       "« correcteur selection » (voir Paramètres > Général).")

    @staticmethod
    def _run(cmd: list[str], data: str | None = None, timeout: float = 1.5) -> str | None:
        try:
            out = subprocess.run(cmd, input=data, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout if out.returncode == 0 else None

    def _paste(self, primary: bool) -> str:
        tool = which("wl-paste")
        if not tool:
            return ""
        cmd = [tool, "--no-newline", "--type", "text"]
        if primary:
            cmd.insert(1, "--primary")
        return self._run(cmd) or ""

    def _set_clipboard(self, text: str) -> bool:
        tool = which("wl-copy")
        if tool:
            return self._run([tool], data=text) is not None
        super().copy(text)
        return True

    def _send_ctrl(self, letter: str) -> bool:
        wtype = which("wtype")
        if wtype and "gnome" not in desktop_name():
            if self._run([wtype, "-M", "ctrl", letter, "-m", "ctrl"]) is not None:
                return True
        ydotool = which("ydotool")
        if ydotool:
            code = {"c": 46, "v": 47}[letter]
            if self._run([ydotool, "key", "29:1", f"{code}:1", f"{code}:0", "29:0"]) is not None:
                return True
        return False

    def capture(self) -> Capture:
        text = self._paste(primary=True)
        if text.strip():
            return Capture(text, from_selection=True)
        saved = self._paste(primary=False)
        time.sleep(0.2)
        if self._send_ctrl("c"):
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                time.sleep(0.05)
                text = self._paste(primary=False)
                if text != saved:
                    return Capture(text, from_selection=True, saved_clipboard=saved, clipboard_touched=True)
        return Capture("")

    def release(self, capture: Capture) -> None:
        if capture.clipboard_touched and capture.saved_clipboard is not None:
            self._set_clipboard(capture.saved_clipboard)

    def replace(self, capture: Capture, text: str) -> tuple[bool, str]:
        if capture.saved_clipboard is None:
            capture.saved_clipboard = self._paste(primary=False)
        self._set_clipboard(text)
        capture.clipboard_touched = True
        time.sleep(0.25)  # le temps que le bureau rende le focus à la fenêtre d'origine
        if not self._send_ctrl("v"):
            return False, "Texte corrigé copié : collez-le avec Ctrl+V (installez wtype ou ydotool pour le collage automatique)."
        saved = capture.saved_clipboard
        self._defer(_RESTORE_DELAY_MS, lambda: self._set_clipboard(saved))
        return True, ""

    def copy(self, text: str) -> None:
        self._set_clipboard(text)

    def clipboard_text(self) -> str:
        return self._paste(primary=False)

    def diagnostics(self) -> list[str]:
        return [
            f"Session : Wayland ({desktop_name() or 'bureau inconnu'})",
            "Raccourcis : à créer dans les paramètres du bureau (commande « correcteur selection »)",
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

