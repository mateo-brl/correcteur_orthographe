"""Fonctions Windows (ctypes, sans dépendance) : raccourcis globaux via
RegisterHotKey, simulation de touches via SendInput, presse-papiers, fenêtres.

RegisterHotKey est la méthode officielle : le raccourci est intercepté par le
système (l'application au premier plan ne le reçoit pas) et ne coûte rien
tant qu'il n'est pas utilisé. Aucun crochet clavier permanent : un seul, posé
au plus 2 s après le raccourci (voir HotkeyListener), et une surveillance
Raw Input le temps d'une correction express (voir InputWatch)."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from typing import Callable

from correcteur.platform.keys import Hotkey, parse

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_TIMER = 0x0113
WM_INPUT = 0x00FF
WM_KEYUP, WM_SYSKEYUP = 0x0101, 0x0105
WM_APP_UNHOOK = 0x8001  # WM_APP + 1 : retirer le crochet clavier du fil des raccourcis
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SW_RESTORE = 9
WH_KEYBOARD_LL = 13
LLKHF_INJECTED = 0x10
GW_HWNDNEXT = 2
GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000008, 0x00000080, 0x08000000
RIDEV_REMOVE, RIDEV_INPUTSINK = 0x00000001, 0x00000100
RID_INPUT = 0x10000003
RIM_TYPEMOUSE, RIM_TYPEKEYBOARD = 0, 1
RI_KEY_BREAK = 0x01
# Appuis des boutons de la souris (gauche, droit, milieu, X1, X2) ; la molette ne déplace pas la sélection.
_RI_MOUSE_DOWNS = 0x0001 | 0x0004 | 0x0010 | 0x0040 | 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25
SECURITY_MANDATORY_HIGH_RID = 0x3000

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_INSERT = 0x2D
VK_ESCAPE = 0x1B
VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU = 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5
_MODIFIER_VKS = (VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN)
# Touche « neutre » (sans fonction) envoyée pendant qu'Alt ou Win est enfoncée : relâchée seule, Alt ouvrirait la
# barre de menus de l'application, Win le menu Démarrer. AutoHotkey fait de même (MenuMaskKey).
VK_MASK = 0xE8
# Touches dont l'appui ne déplace pas la sélection : modificateurs et verrouillages (Raw Input donne VK_SHIFT,
# VK_CONTROL et VK_MENU sans côté ; 0xFF : touche « fantôme » de certains claviers).
_NEUTRAL_VKS = {VK_SHIFT, VK_CONTROL, VK_MENU, 0x14, 0x90, 0x91, 0xFF, VK_MASK, *_MODIFIER_VKS}
# Terminaux (fenêtres de console, ou programmes qui en contiennent un, comme VS Code) : Ctrl+C y interrompt le
# programme en cours quand rien n'est sélectionné. La copie simulée n'y envoie jamais que Ctrl+Inser.
_CONSOLE_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "mintty", "PuTTY", "VirtualConsoleClass"}
_TERMINAL_PROCESSES = {"windowsterminal.exe", "openconsole.exe", "conhost.exe", "cmd.exe", "powershell.exe",
                       "pwsh.exe", "code.exe", "code - insiders.exe", "vscodium.exe", "cursor.exe", "windsurf.exe",
                       "mintty.exe", "putty.exe", "kitty.exe", "conemu.exe", "conemu64.exe", "alacritty.exe",
                       "wezterm-gui.exe", "hyper.exe", "tabby.exe", "wsl.exe", "wslhost.exe", "bash.exe"}
# Touches étendues (préfixe E0) : sans ce drapeau, Inser devient celle du pavé numérique.
_EXTENDED_VKS = {0x2D, 0x5B, 0x5C, 0xA3, 0xA5}  # Inser, Windows gauche et droite, Ctrl et Alt droites
# Barre de menus active (touche Alt, F10) ou menu ouvert : la copie simulée y tombe et ne copie rien.
GUI_INMENUMODE, GUI_SYSTEMMENUMODE, GUI_POPUPMENUMODE = 0x4, 0x8, 0x10
# Barre des tâches, bureau, menus : jamais la fenêtre où l'on travaillait.
_SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "NotifyIconOverflowWindow",
                  "TopLevelWindowForOverflowXamlIsland", "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow",
                  "TaskListThumbnailWnd", "#32768", "tooltips_class32", "SysShadow"}

_NAMED_VK = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "page_up": 0x21, "page_down": 0x22, "pause": 0x13, "print_screen": 0x2C,
    "scroll_lock": 0x91, "menu": 0x5D, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t


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


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT), ("dwFlags", wintypes.DWORD),
                ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD), ("hDevice", wintypes.HANDLE),
                ("wParam", wintypes.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT), ("Reserved", wintypes.USHORT),
                ("VKey", wintypes.USHORT), ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]


class _RAWMOUSEBUTTONS(ctypes.Structure):
    _fields_ = [("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT)]


class _RAWMOUSEUNION(ctypes.Union):
    _fields_ = [("ulButtons", wintypes.ULONG), ("buttons", _RAWMOUSEBUTTONS)]


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("usFlags", wintypes.USHORT), ("u", _RAWMOUSEUNION), ("ulRawButtons", wintypes.ULONG),
                ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]


class _RAWDATA(ctypes.Union):
    _fields_ = [("mouse", RAWMOUSE), ("keyboard", RAWKEYBOARD)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("data", _RAWDATA)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


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
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
user32.GetClipboardFormatNameW.restype = ctypes.c_int
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ULONG_PTR, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ULONG_PTR
user32.KillTimer.argtypes = [wintypes.HWND, ULONG_PTR]
user32.KillTimer.restype = wintypes.BOOL
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetTopWindow.argtypes = [wintypes.HWND]
user32.GetTopWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
user32.RegisterRawInputDevices.restype = wintypes.BOOL
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT),
                                   wintypes.UINT]
user32.GetRawInputData.restype = wintypes.UINT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetCurrentProcessId.restype = wintypes.DWORD
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
advapi32.GetTokenInformation.restype = wintypes.BOOL
advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
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
    """Thread qui enregistre les raccourcis et reçoit WM_HOTKEY.

    Juste après WM_HOTKEY, deux précautions pour l'application au premier plan, qui voit encore Ctrl et Alt
    enfoncées (l'appui de la lettre lui a été caché par Windows, pas son relâchement) :
    - Alt ou Win enfoncée : touche neutre (VK_MASK), sinon Alt relâchée seule ouvre la barre de menus ;
    - lettre encore enfoncée : son relâchement est avalé s'il arrive avant celui de Ctrl et Alt. Google Docs
      ouvre son panneau « Grammaire et orthographe » au relâchement de Ctrl+Alt+X. Crochet clavier posé
      pour ce seul relâchement (2 s au plus). Dès que Ctrl et Alt sont relâchées, un relâchement simulé de la
      lettre suit : sans lui, Windows croirait la touche toujours enfoncée, et le raccourci suivant passerait
      pour une répétition (MOD_NOREPEAT : ignoré)."""

    def __init__(self, bindings: dict[str, Callable[[], None]]) -> None:
        self._bindings = bindings
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.failed: list[str] = []
        self._hook = None
        self._hook_proc = HOOKPROC(self._on_key)  # gardé vivant tant que le crochet peut servir
        self._swallow_vk = 0
        self._timer = 0
        self._release_timer = 0  # attend le relâchement de Ctrl et Alt pour renvoyer la lettre avalée

    def start(self) -> list[str]:
        self._thread = threading.Thread(target=self._run, name="raccourcis", daemon=True)
        self._thread.start()
        self._ready.wait(3)
        return self.failed

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # crée la file de messages du thread
        callbacks: dict[int, tuple[Callable[[], None], int]] = {}
        for ident, (combo, callback) in enumerate(self._bindings.items(), start=1):
            try:
                mods, vk = _to_native(parse(combo))
            except ValueError as exc:
                log.warning("Raccourci %s invalide : %s", combo, exc)
                self.failed.append(combo)
                continue
            if user32.RegisterHotKey(None, ident, mods, vk):
                callbacks[ident] = (callback, vk)
            else:
                log.warning("Raccourci %s refusé par Windows (erreur %d) : déjà pris par une autre application.",
                            combo, ctypes.get_last_error())
                self.failed.append(combo)
        self._ready.set()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    entry = callbacks.get(int(msg.wParam))
                    if entry:
                        callback, vk = entry
                        self._after_hotkey(vk)
                        try:
                            callback()
                        except Exception:
                            log.exception("Erreur après le raccourci")
                elif msg.message == WM_APP_UNHOOK or (msg.message == WM_TIMER and msg.wParam == self._timer):
                    self._unhook()
                elif msg.message == WM_TIMER and msg.wParam == self._release_timer and not pressed_modifiers():
                    user32.KillTimer(None, self._release_timer)
                    self._release_timer = 0
                    _send_swallowed()
        finally:
            self._unhook()
            if self._release_timer:
                user32.KillTimer(None, self._release_timer)
            _send_swallowed()
            for ident in callbacks:
                user32.UnregisterHotKey(None, ident)

    def _after_hotkey(self, vk: int) -> None:
        try:
            if any(user32.GetAsyncKeyState(k) & 0x8000 for k in (VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN)):
                _send([_key_input(VK_MASK, False), _key_input(VK_MASK, True)])
            if self._hook is None and user32.GetAsyncKeyState(vk) & 0x8000:
                self._swallow_vk = vk
                self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_proc,
                                                      kernel32.GetModuleHandleW(None), 0)
                if self._hook:
                    self._timer = user32.SetTimer(None, 0, 2000, None)
                else:
                    log.info("Crochet clavier refusé (erreur %d)", ctypes.get_last_error())
        except Exception:
            log.exception("Précautions après le raccourci impossibles")

    def _on_key(self, code: int, wparam: int, lparam: int) -> int:
        try:
            if code == 0 and wparam in (WM_KEYUP, WM_SYSKEYUP) and self._swallow_vk:
                info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if info.vkCode == self._swallow_vk and not info.flags & LLKHF_INJECTED:
                    vk, self._swallow_vk = self._swallow_vk, 0
                    user32.PostThreadMessageW(self._thread_id, WM_APP_UNHOOK, 0, 0)
                    if pressed_modifiers():
                        with _lock:
                            _swallowed.add(vk)
                        if not self._release_timer:
                            self._release_timer = user32.SetTimer(None, 0, 15, None)
                        return 1
        except Exception:
            pass
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _unhook(self) -> None:
        if self._timer:
            user32.KillTimer(None, self._timer)
            self._timer = 0
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        self._swallow_vk = 0

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(1)


def hotkey_available(combo: str) -> bool | None:
    """Le raccourci est-il libre ? Essai d'enregistrement aussitôt annulé (fil appelant). None : touche
    inconnue sur ce clavier. Un raccourci que le correcteur tient déjà passe pour pris."""
    try:
        mods, vk = _to_native(parse(combo))
    except ValueError:
        return None
    ident = 0xBFF0
    if not user32.RegisterHotKey(None, ident, mods, vk):
        return False
    user32.UnregisterHotKey(None, ident)
    return True


_lock = threading.Lock()
_swallowed: set[int] = set()  # relâchements avalés par le crochet, à renvoyer une fois Ctrl et Alt relâchées


def _key_input(vk: int, up: bool, extended: bool = False) -> INPUT:
    """Frappe simulée avec le code de touche (scan code) du clavier réel : sans lui, Windows Terminal ignorait le
    Ctrl+Inser de la copie simulée."""
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    extended = extended or vk in _EXTENDED_VKS
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    inp.ki = KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0) & 0xFF, flags, 0, 0)  # 0 : MAPVK_VK_TO_VSC
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
    while True:
        held = pressed_modifiers()
        if not held:
            break
        if time.monotonic() >= deadline:
            # Une touche neutre avant de relâcher Alt évite l'ouverture du menu de l'application.
            _send([_key_input(VK_MASK, False), _key_input(VK_MASK, True)] + [_key_input(vk, True) for vk in held])
            time.sleep(0.02)
            break
        time.sleep(0.015)
    _send_swallowed()


def _send_swallowed() -> None:
    """Relâchements avalés après le raccourci (voir HotkeyListener) : renvoyés maintenant que Ctrl et Alt sont
    relâchées. L'application voit relâcher une touche qu'elle n'a pas vue s'enfoncer, sans conséquence."""
    with _lock:
        keys = sorted(_swallowed)
        _swallowed.clear()
    if keys:
        _send([_key_input(vk, True) for vk in keys])


def send_ctrl(letter: str) -> None:
    vk = ord(letter.upper())
    _send([_key_input(VK_CONTROL, False), _key_input(vk, False), _key_input(vk, True), _key_input(VK_CONTROL, True)])


def send_copy(keys: str = "insert") -> None:
    """Copie simulée : Ctrl+Inser (« insert ») ou Ctrl+C (« c »)."""
    if keys == "insert":
        _send([_key_input(VK_CONTROL, False), _key_input(VK_INSERT, False), _key_input(VK_INSERT, True),
               _key_input(VK_CONTROL, True)])
    else:
        send_ctrl(keys)


def send_escape() -> None:
    _send([_key_input(VK_ESCAPE, False), _key_input(VK_ESCAPE, True)])


def process_name(hwnd: int) -> str:
    """Nom du programme de la fenêtre, en minuscules (« code.exe ») ; "" s'il est illisible."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, _window_pid(hwnd)) if hwnd else None
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(handle)


def is_terminal(hwnd: int) -> bool:
    """Fenêtre de terminal, ou programme qui en contient un (VS Code) : jamais de Ctrl+C simulé."""
    return window_class(hwnd) in _CONSOLE_CLASSES or process_name(hwnd) in _TERMINAL_PROCESSES


def keyboard_in_menu(hwnd: int) -> bool:
    """Le clavier est dans un menu de la fenêtre plutôt que dans son texte : barre de menus activée par Alt (menus
    classiques, ou barre XAML du Bloc-notes de Windows 11, qui garde alors le focus), menu ouvert."""
    info = _thread_info(hwnd)
    if info is None:
        return False
    if info.flags & (GUI_INMENUMODE | GUI_SYSTEMMENUMODE | GUI_POPUPMENUMODE):
        return True
    return (window_class(hwnd) == "Notepad" and bool(info.hwndFocus)
            and window_class(int(info.hwndFocus)) == "InputSiteWindowClass")


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


def _thread_info(hwnd: int) -> GUITHREADINFO | None:
    tid = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None) if hwnd else 0
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if not tid or not user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
        return None
    return info


def focused_control(hwnd: int) -> int:
    """Contrôle qui a le focus clavier dans la fenêtre `hwnd` (0 si inconnu)."""
    info = _thread_info(hwnd)
    return int(info.hwndFocus or 0) if info is not None else 0


def caret_point(hwnd: int) -> tuple[int, int] | None:
    """Bas du curseur de saisie de la fenêtre, en pixels de l'écran ; None si l'application n'a pas de curseur
    système (navigateurs, applications modernes)."""
    info = _thread_info(hwnd)
    if info is None or not info.hwndCaret:
        return None
    point = wintypes.POINT(info.rcCaret.left, info.rcCaret.bottom)
    if not user32.ClientToScreen(info.hwndCaret, ctypes.byref(point)):
        return None
    return point.x, point.y


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """(gauche, haut, droite, bas) en pixels de l'écran."""
    rect = wintypes.RECT()
    if not hwnd or not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def cursor_pos() -> tuple[int, int] | None:
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return None
    return point.x, point.y


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value


def is_own_or_shell(hwnd: int) -> bool:
    """Fenêtre du correcteur (menu de l'icône compris), ou de la barre des tâches et du bureau."""
    return _window_pid(hwnd) == kernel32.GetCurrentProcessId() or window_class(hwnd) in _SHELL_CLASSES


def _cloaked(hwnd) -> bool:
    """Fenêtre « masquée » par le gestionnaire de fenêtres (autre bureau virtuel, application en veille)."""
    try:
        value = wintypes.DWORD()
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(value), ctypes.sizeof(value))  # DWMWA_CLOAKED
        return bool(value.value)
    except Exception:
        return False


def last_app_window() -> int:
    """Dernière fenêtre d'application utilisée : la première de l'ordre d'empilement, hors fenêtres du correcteur,
    de la barre des tâches, invisibles ou sans titre. Le clic sur l'icône du correcteur donne le premier plan à la
    barre des tâches, mais la fenêtre où l'on travaillait reste en tête des autres. Les fenêtres « toujours au
    premier plan » ne viennent qu'à défaut (incrustations, mini-lecteurs)."""
    own = kernel32.GetCurrentProcessId()
    topmost = 0
    hwnd = user32.GetTopWindow(None)
    for _ in range(2000):
        if not hwnd:
            break
        value = int(hwnd)
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if (user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and not ex & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
                and user32.GetWindowTextLengthW(hwnd) > 0 and _window_pid(value) != own
                and window_class(value) not in _SHELL_CLASSES and not _cloaked(wintypes.HWND(value))):
            if not ex & WS_EX_TOPMOST:
                return value
            topmost = topmost or value
        hwnd = user32.GetWindow(hwnd, GW_HWNDNEXT)
    return topmost


def _integrity(process) -> int | None:
    """Niveau d'intégrité du processus (0x2000 : normal, 0x3000 : administrateur), None si illisible."""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buf, size, ctypes.byref(size)):
            return None
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_MANDATORY_LABEL.Label.Sid
        count = advapi32.GetSidSubAuthorityCount(sid)[0]
        return int(advapi32.GetSidSubAuthority(sid, count - 1)[0])
    finally:
        kernel32.CloseHandle(token)


def more_privileged(hwnd: int) -> bool:
    """La fenêtre appartient à un programme lancé en administrateur, pas le correcteur : Windows bloque alors
    les frappes simulées vers elle (UIPI), sans le signaler. Jeton illisible : programme élevé, sauf si le
    correcteur l'est aussi."""
    ours = _integrity(kernel32.GetCurrentProcess())
    if ours is None:
        return False
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, _window_pid(hwnd))
    if not handle:
        return False
    try:
        theirs = _integrity(handle)
    finally:
        kernel32.CloseHandle(handle)
    if theirs is None:
        return ours < SECURITY_MANDATORY_HIGH_RID
    return theirs > ours


class InputWatch:
    """Frappes et clics de l'utilisateur pendant une correction express (Raw Input, dans un fil à part).

    Le texte corrigé est collé à l'endroit du curseur au moment du collage : si l'utilisateur a tapé ou cliqué
    entre-temps, la sélection n'est plus là, et le collage écrirait ailleurs. Raw Input reçoit une copie des
    évènements sans jamais les retarder (contrairement à un crochet clavier ou souris), et seulement pendant la
    correction. Frappes simulées (hDevice nul : celles du correcteur), répétitions d'une touche déjà enfoncée au
    départ, modificateurs seuls et molette ne comptent pas."""

    _class_atom = 0
    _wndproc = None
    _watches: dict[int, "InputWatch"] = {}

    def __init__(self) -> None:
        self.acted = ""   # ce que l'utilisateur a fait (« frappe », « clic »), "" s'il n'a rien fait
        self.ok = False   # surveillance réellement en place
        self._held: set[int] = set()
        self._thread_id = 0
        self._ready = threading.Event()

    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, name="surveillance", daemon=True)
        self._thread.start()
        self._ready.wait(1.0)
        return self.ok

    def stop(self) -> None:
        """Arrête la surveillance et attend qu'elle soit retirée : une seule à la fois par processus (Raw Input)."""
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread_id = 0
        thread = getattr(self, "_thread", None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(0.5)

    @classmethod
    def _register_class(cls) -> int:
        if not cls._class_atom:
            cls._wndproc = WNDPROC(cls._window_proc)
            wc = WNDCLASSW(lpfnWndProc=cls._wndproc, hInstance=kernel32.GetModuleHandleW(None),
                           lpszClassName="CorrecteurSurveillance")
            cls._class_atom = user32.RegisterClassW(ctypes.byref(wc))
        return cls._class_atom

    @classmethod
    def _window_proc(cls, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            watch = cls._watches.get(int(hwnd or 0))
            if watch is not None:
                try:
                    watch._on_input(lparam)
                except Exception:
                    pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self) -> None:
        hwnd = None
        try:
            self._thread_id = kernel32.GetCurrentThreadId()
            if self._register_class():
                hwnd = user32.CreateWindowExW(0, "CorrecteurSurveillance", None, 0, 0, 0, 0, 0, wintypes.HWND(-3),
                                              None, kernel32.GetModuleHandleW(None), None)  # HWND_MESSAGE : invisible
            if hwnd:
                self._watches[int(hwnd)] = self
                self._held = {vk for vk in range(1, 255) if user32.GetAsyncKeyState(vk) & 0x8000}
                devices = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(1, 6, RIDEV_INPUTSINK, hwnd),   # clavier
                                               RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, hwnd))   # souris
                self.ok = bool(user32.RegisterRawInputDevices(devices, 2, ctypes.sizeof(RAWINPUTDEVICE)))
                if not self.ok:
                    log.info("Surveillance des frappes impossible (erreur %d)", ctypes.get_last_error())
        except Exception:
            log.exception("Surveillance des frappes impossible")
        finally:
            self._ready.set()
        if not hwnd:
            return
        msg = wintypes.MSG()
        # Arrêt d'office au bout de 2 minutes, si personne ne l'a demandé.
        timer = user32.SetTimer(None, 0, 120_000, None) if self.ok else 0
        try:
            while self.ok and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_TIMER and msg.wParam == timer:
                    break
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if timer:
                user32.KillTimer(None, timer)
            if self.ok:
                removed = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(1, 6, RIDEV_REMOVE, None),
                                               RAWINPUTDEVICE(1, 2, RIDEV_REMOVE, None))
                user32.RegisterRawInputDevices(removed, 2, ctypes.sizeof(RAWINPUTDEVICE))
            self._watches.pop(int(hwnd), None)
            user32.DestroyWindow(hwnd)

    def _on_input(self, handle: int) -> None:
        data = RAWINPUT()
        size = wintypes.UINT(ctypes.sizeof(RAWINPUT))
        if user32.GetRawInputData(handle, RID_INPUT, ctypes.byref(data), ctypes.byref(size),
                                  ctypes.sizeof(RAWINPUTHEADER)) in (0, 0xFFFFFFFF):
            return
        if not data.header.hDevice:
            return  # frappe ou clic simulés (SendInput) : ceux du correcteur
        if data.header.dwType == RIM_TYPEKEYBOARD:
            vk = data.data.keyboard.VKey
            if data.data.keyboard.Flags & RI_KEY_BREAK:
                self._held.discard(vk)
            elif vk not in self._held:
                self._held.add(vk)
                if vk not in _NEUTRAL_VKS and not self.acted:
                    self.acted = "frappe"
        elif data.header.dwType == RIM_TYPEMOUSE and data.data.mouse.buttons.usButtonFlags & _RI_MOUSE_DOWNS:
            self.acted = self.acted or "clic"


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


_html_format = 0


def html_format() -> int:
    """Format « HTML Format » (CF_HTML) : texte mis en forme copié par Word, les navigateurs, Outlook..."""
    global _html_format
    if not _html_format:
        _html_format = int(user32.RegisterClipboardFormatW("HTML Format"))
    return _html_format


# Ligne entière copiée faute de sélection : Scintilla (Notepad++) et Visual Studio la signalent par un format à
# eux ; VS Code, dans les données personnalisées de Chromium (« isFromEmptySelection », en UTF-16).
_LINE_COPY_FORMATS = ("MSDEVLineSelect", "VisualStudioEditorOperationsLineCutCopyClipboardTag")
_WEB_CUSTOM_DATA = "Chromium Web Custom MIME Data Format"
_EMPTY_SELECTION = '"isFromEmptySelection":true'.encode("utf-16-le")


def copied_cells() -> bool:
    """Le presse-papiers contient-il des cellules de tableur copiées hors édition (format « Biff8 » d'Excel et de
    LibreOffice Calc) ?"""
    return bool(user32.IsClipboardFormatAvailable(user32.RegisterClipboardFormatW("Biff8")))


def copied_line() -> bool:
    """Le presse-papiers contient-il une ligne copiée faute de sélection (VS Code, Notepad++, Visual Studio) ?"""
    if any(user32.IsClipboardFormatAvailable(user32.RegisterClipboardFormatW(name)) for name in _LINE_COPY_FORMATS):
        return True
    data = get_clipboard_bytes(int(user32.RegisterClipboardFormatW(_WEB_CUSTOM_DATA)))
    return bool(data) and _EMPTY_SELECTION in data


def get_clipboard_bytes(fmt: int, limit: int = 8 * 1024 * 1024) -> bytes | None:
    """Données brutes d'un format du presse-papiers (None s'il est absent, illisible ou trop gros)."""
    if not _open_clipboard():
        return None
    try:
        if not user32.IsClipboardFormatAvailable(fmt):
            return None
        handle = user32.GetClipboardData(fmt)
        size = kernel32.GlobalSize(handle) if handle else 0
        if not size or size > limit:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def format_name(fmt: int) -> str:
    buf = ctypes.create_unicode_buffer(128)
    if fmt >= 0xC000 and user32.GetClipboardFormatNameW(fmt, buf, 128):
        return buf.value
    return str(fmt)


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


def text_bytes(text: str, crlf: bool = True) -> bytes:
    """CF_UNICODETEXT : fins de ligne \\r\\n (convention Windows), ou \\n seul (`crlf` faux)."""
    text = text.replace("\r\n", "\n")
    if crlf:
        text = text.replace("\n", "\r\n")
    return text.encode("utf-16-le") + b"\x00\x00"


def set_clipboard_text(text: str, crlf: bool = True) -> bool:
    return set_clipboard_formats({CF_UNICODETEXT: text_bytes(text, crlf)})


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
        lost: list[int] = []
        total = 0
        fmt = user32.EnumClipboardFormats(0)
        while fmt:
            if fmt not in _NON_HGLOBAL:
                handle = user32.GetClipboardData(fmt)
                size = kernel32.GlobalSize(handle) if handle else 0
                ptr = kernel32.GlobalLock(handle) if size else None
                if ptr:
                    try:
                        total += size
                        if total > _MAX_SNAPSHOT:
                            log.info("Presse-papiers trop gros pour être gardé (plus de %d Mo)", _MAX_SNAPSHOT >> 20)
                            return None
                        formats[fmt] = ctypes.string_at(ptr, size)
                    finally:
                        kernel32.GlobalUnlock(handle)
                else:
                    lost.append(fmt)  # données OLE seulement (FileContents d'une capture d'écran, par exemple)
            fmt = user32.EnumClipboardFormats(fmt)
        if lost:
            log.info("Formats du presse-papiers non gardés : %s", ", ".join(format_name(f) for f in lost))
        return formats
    finally:
        user32.CloseClipboard()
