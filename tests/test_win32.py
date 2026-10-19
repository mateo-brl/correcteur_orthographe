"""Tests Windows (exécutés sur la machine Windows de la CI)."""

from __future__ import annotations

import ctypes
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.windows

if sys.platform != "win32":
    pytest.skip("Windows uniquement", allow_module_level=True)

from correcteur.platform import win32  # noqa: E402
from correcteur.platform.keys import parse  # noqa: E402


def test_input_structure_size():
    # SendInput refuse silencieusement une structure de mauvaise taille.
    assert ctypes.sizeof(win32.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


@pytest.mark.parametrize("combo, mods, vk", [
    ("<ctrl>+<alt>+c", win32.MOD_CONTROL | win32.MOD_ALT, 0x43),
    ("<ctrl>+<shift>+<f8>", win32.MOD_CONTROL | win32.MOD_SHIFT, 0x77),
    ("<cmd>+<space>", win32.MOD_WIN, 0x20),
    ("<alt>+9", win32.MOD_ALT, 0x39),
])
def test_native_hotkey_mapping(combo, mods, vk):
    native_mods, native_vk = win32._to_native(parse(combo))
    assert native_mods == mods | win32.MOD_NOREPEAT
    assert native_vk == vk


def _clipboard_or_skip():
    if not win32._open_clipboard(retries=5):
        pytest.skip("presse-papiers inaccessible (session sans bureau)")
    win32.user32.CloseClipboard()


def test_clipboard_roundtrip_unicode():
    _clipboard_or_skip()
    assert win32.set_clipboard_text("Élève à l'école\nligne 2 😀")
    assert win32.get_clipboard_text() == "Élève à l'école\r\nligne 2 😀"


def test_clipboard_snapshot_restore():
    _clipboard_or_skip()
    win32.set_clipboard_text("sauvegarde")
    snap = win32.snapshot_clipboard()
    assert snap
    win32.set_clipboard_text("autre chose")
    assert win32.set_clipboard_formats(snap)
    assert win32.get_clipboard_text() == "sauvegarde"


def test_clipboard_sequence_changes():
    _clipboard_or_skip()
    before = win32.clipboard_sequence()
    win32.set_clipboard_text("x")
    assert win32.clipboard_sequence() != before


def test_activate_invalid_window():
    assert win32.activate_window(0) is False
    assert win32.activate_window(0x7FFFFFF0) is False


def test_hotkey_listener_registers_and_stops():
    fired = threading.Event()
    listener = win32.HotkeyListener({"<ctrl>+<alt>+<shift>+<f11>": fired.set, "ctrl+nimportequoi": fired.set})
    failed = listener.start()
    assert "ctrl+nimportequoi" in failed
    try:
        if "<ctrl>+<alt>+<shift>+<f11>" in failed:
            pytest.skip("raccourci déjà pris sur cette machine")
        # Frappe simulée du raccourci.
        keys = [win32.VK_CONTROL, win32.VK_MENU, win32.VK_SHIFT, 0x7A]
        win32._send([win32._key_input(k, False) for k in keys] + [win32._key_input(k, True) for k in reversed(keys)])
        if not fired.wait(3):
            pytest.skip("pas de bureau interactif pour recevoir la frappe simulée")
    finally:
        listener.stop()
    # Après arrêt, le raccourci est libéré : un nouvel enregistrement doit réussir.
    again = win32.HotkeyListener({"<ctrl>+<alt>+<shift>+<f11>": lambda: None})
    assert again.start() == []
    again.stop()


def test_wait_modifiers_released_returns_quickly():
    t0 = time.monotonic()
    win32.wait_modifiers_released(0.5)
    assert time.monotonic() - t0 < 1.0


def test_autostart_registry(monkeypatch):
    from correcteur import APP_NAME
    from correcteur.platform import autostart

    monkeypatch.setattr(autostart, "APP_NAME", APP_NAME + "-test")
    autostart.set_autostart(False)  # jamais activé : ne doit pas échouer
    assert not autostart.autostart_enabled()
    try:
        autostart.set_autostart(True)  # crée la clé "Run" si elle n'existe pas
        assert autostart.autostart_enabled()
    finally:
        autostart.set_autostart(False)
    assert not autostart.autostart_enabled()


def test_text_bytes_line_endings():
    assert win32.text_bytes("a\nb") == "a\r\nb".encode("utf-16-le") + b"\0\0"
    assert win32.text_bytes("a\r\nb", crlf=False) == "a\nb".encode("utf-16-le") + b"\0\0"


def test_html_clipboard_roundtrip():
    """Format CF_HTML enregistré, écrit puis relu tel quel (la mémoire allouée peut être plus grande)."""
    _clipboard_or_skip()
    data = b"Version:0.9\r\nStartFragment:0000000040\r\nEndFragment:0000000044\r\n<b>x</b>\0"
    assert win32.html_format() >= 0xC000
    assert win32.set_clipboard_formats({win32.CF_UNICODETEXT: win32.text_bytes("x"), win32.html_format(): data})
    read = win32.get_clipboard_bytes(win32.html_format())
    assert read is not None and read.split(b"\0", 1)[0] == data[:-1]
    assert win32.get_clipboard_text() == "x"


def test_hotkey_availability():
    combo = "<ctrl>+<alt>+<shift>+<f12>"
    if not win32.hotkey_available(combo):
        pytest.skip("raccourci déjà pris sur cette machine")
    listener = win32.HotkeyListener({combo: lambda: None})
    try:
        assert listener.start() == []
        assert win32.hotkey_available(combo) is False  # pris (ici, par le correcteur lui-même)
    finally:
        listener.stop()
    assert win32.hotkey_available(combo) is True
    assert win32.hotkey_available("ctrl+nimportequoi") is None


def test_input_watch_ignores_simulated_keys():
    """Surveillance Raw Input : en place puis retirée ; les frappes simulées (celles du correcteur) ne comptent pas."""
    watch = win32.InputWatch()
    if not watch.start():
        watch.stop()
        pytest.skip("Raw Input indisponible (session sans bureau)")
    try:
        win32._send([win32._key_input(0x41, False), win32._key_input(0x41, True)])  # A, simulée
        time.sleep(0.3)
        assert watch.acted == ""
    finally:
        watch.stop()
    assert not watch._thread.is_alive()
    again = win32.InputWatch()  # une seule surveillance à la fois : la précédente est bien retirée
    try:
        assert again.start()
    finally:
        again.stop()


def test_window_helpers_never_fail():
    hwnd = win32.foreground_window()
    assert isinstance(win32.focused_control(hwnd), int)
    assert win32.window_rect(0) is None
    assert win32.caret_point(0) is None
    assert isinstance(win32.last_app_window(), int)
    assert win32.more_privileged(hwnd) is False or win32.more_privileged(hwnd) is True
    if hwnd:
        assert isinstance(win32.is_own_or_shell(hwnd), bool)
        assert win32.window_rect(hwnd) is None or len(win32.window_rect(hwnd)) == 4
