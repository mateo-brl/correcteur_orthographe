"""Fonctions Windows (ctypes, sans dépendance) : raccourcis globaux via
RegisterHotKey, simulation de touches via SendInput, presse-papiers, fenêtres.

RegisterHotKey est la méthode officielle : le raccourci est intercepté par le
système (l'application au premier plan ne le reçoit pas) et ne coûte rien
tant qu'il n'est pas utilisé (pas de hook clavier)."""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable

from correcteur.platform.keys import Hotkey, parse

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SW_RESTORE = 9

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU = 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5
_MODIFIER_VKS = (VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN)

_NAMED_VK = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "page_up": 0x21, "page_down": 0x22, "pause": 0x13, "print_screen": 0x2C,
    "scroll_lock": 0x91, "menu": 0x5D, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}

ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL


def _to_native(hotkey: Hotkey) -> tuple[int, int]:
    mods = MOD_NOREPEAT
    mods |= MOD_CONTROL if "ctrl" in hotkey.modifiers else 0
    mods |= MOD_ALT if "alt" in hotkey.modifiers else 0
    mods |= MOD_SHIFT if "shift" in hotkey.modifiers else 0
    mods |= MOD_WIN if "cmd" in hotkey.modifiers else 0
    key = hotkey.key
    if key in _NAMED_VK:
        return mods, _NAMED_VK[key]
    if key.isascii() and key.isalnum():
        return mods, ord(key.upper())
    scan = user32.VkKeyScanW(key)
    if scan == -1:
        raise ValueError(f"Touche introuvable sur ce clavier : {key}")
    return mods, scan & 0xFF


class HotkeyListener:
    """Thread qui enregistre les raccourcis et reçoit WM_HOTKEY."""

    def __init__(self, bindings: dict[str, Callable[[], None]]) -> None:
        self._bindings = bindings
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.failed: list[str] = []

    def start(self) -> list[str]:
        self._thread = threading.Thread(target=self._run, name="raccourcis", daemon=True)
        self._thread.start()
        self._ready.wait(3)
        return self.failed

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # crée la file de messages du thread
        callbacks: dict[int, Callable[[], None]] = {}
        for ident, (combo, callback) in enumerate(self._bindings.items(), start=1):
            try:
                mods, vk = _to_native(parse(combo))
            except ValueError:
                self.failed.append(combo)
                continue
            if user32.RegisterHotKey(None, ident, mods, vk):
                callbacks[ident] = callback
            else:
                self.failed.append(combo)  # déjà pris par une autre application
        self._ready.set()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    callback = callbacks.get(int(msg.wParam))
                    if callback:
                        try:
                            callback()
                        except Exception:
                            pass
        finally:
            for ident in callbacks:
                user32.UnregisterHotKey(None, ident)

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(1)


def _key_input(vk: int, up: bool) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    return inp


def _send(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def pressed_modifiers() -> list[int]:
    return [vk for vk in _MODIFIER_VKS if user32.GetAsyncKeyState(vk) & 0x8000]


def wait_modifiers_released(timeout: float = 1.2) -> None:
    """Attend que l'utilisateur relâche Ctrl/Alt/Maj/Win du raccourci ; sinon les
    relâche artificiellement (un Ctrl+C envoyé avec Alt enfoncé deviendrait Ctrl+Alt+C)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pressed_modifiers():
            return
        time.sleep(0.015)
    held = pressed_modifiers()
    if held:
        # Une touche neutre avant de relâcher Alt évite l'ouverture du menu de l'application.
        _send([_key_input(0xE8, False), _key_input(0xE8, True)] + [_key_input(vk, True) for vk in held])
        time.sleep(0.02)


def send_ctrl(letter: str) -> None:
    vk = ord(letter.upper())
    _send([_key_input(VK_CONTROL, False), _key_input(vk, False), _key_input(vk, True), _key_input(VK_CONTROL, True)])


def foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buf, 512)
    return buf.value


def activate_window(hwnd: int) -> bool:
    """Ramène une fenêtre au premier plan (astuce AttachThreadInput)."""
    if not hwnd or not user32.IsWindow(wintypes.HWND(hwnd)):
        return False
    target = wintypes.HWND(hwnd)
    if user32.IsIconic(target):
        user32.ShowWindow(target, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    this_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fg_tid and fg_tid != this_tid:
        attached = bool(user32.AttachThreadInput(fg_tid, this_tid, True))
    try:
        user32.BringWindowToTop(target)
        user32.SetForegroundWindow(target)
    finally:
        if attached:
            user32.AttachThreadInput(fg_tid, this_tid, False)
    return int(user32.GetForegroundWindow() or 0) == hwnd


def clipboard_sequence() -> int:
    return int(user32.GetClipboardSequenceNumber())


def _open_clipboard(retries: int = 40) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)  # une autre application le tient ouvert
    return False


def get_clipboard_text() -> str | None:
    if not _open_clipboard():
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _global_from_bytes(data: bytes):
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(len(data), 1))
    if not handle:
        return None
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return None
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(handle)
    return handle


def set_clipboard_text(text: str) -> bool:
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return set_clipboard_formats({CF_UNICODETEXT: text.encode("utf-16-le") + b"\x00\x00"})


def set_clipboard_formats(formats: dict[int, bytes]) -> bool:
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        ok = True
        for fmt, data in formats.items():
            handle = _global_from_bytes(data)
            if handle is None or not user32.SetClipboardData(fmt, handle):
                if handle:
                    kernel32.GlobalFree(handle)
                ok = False
        return ok
    finally:
        user32.CloseClipboard()


# Formats dont les données ne sont pas un bloc mémoire (GDI...) : non sauvegardables tels quels.
_NON_HGLOBAL = {2, 3, 9, 14, 0x0080, 0x0082, 0x0083, 0x008E}
_MAX_SNAPSHOT = 64 * 1024 * 1024


def snapshot_clipboard() -> dict[int, bytes] | None:
    """Copie tout le contenu du presse-papiers (texte, images, fichiers...) pour le
    restaurer après usage. None si impossible."""
    if not _open_clipboard():
        return None
    try:
        formats: dict[int, bytes] = {}
        total = 0
        fmt = user32.EnumClipboardFormats(0)
        while fmt:
            if fmt not in _NON_HGLOBAL:
                handle = user32.GetClipboardData(fmt)
                if handle:
                    size = kernel32.GlobalSize(handle)
                    ptr = kernel32.GlobalLock(handle) if size else None
                    if ptr:
                        try:
                            total += size
                            if total > _MAX_SNAPSHOT:
                                return None
                            formats[fmt] = ctypes.string_at(ptr, size)
                        finally:
                            kernel32.GlobalUnlock(handle)
            fmt = user32.EnumClipboardFormats(fmt)
        return formats
    finally:
        user32.CloseClipboard()
