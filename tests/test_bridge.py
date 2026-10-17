"""Pont avec le système : presse-papiers et collage (programmes externes simulés), environnement des
programmes lancés."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from correcteur.platform import bridge, child_env
from correcteur.platform.bridge import Capture, WaylandBridge, WindowsBridge, X11Bridge

_QT_VARIABLES = ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH")


@pytest.fixture
def package_env(monkeypatch):
    """Comme dans le paquet PyInstaller : sys.frozen et les chemins Qt du paquet dans l'environnement."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    for name in _QT_VARIABLES:
        monkeypatch.setenv(name, f"/paquet/{name}")


def test_child_env_drops_package_qt_paths(package_env):
    env = child_env()
    assert not set(_QT_VARIABLES) & set(env)
    assert env["PATH"] == os.environ["PATH"]
    # Le correcteur garde ses propres chemins.
    assert os.environ["QT_PLUGIN_PATH"] == "/paquet/QT_PLUGIN_PATH"


def test_child_env_unchanged_outside_package(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("QT_PLUGIN_PATH", "/mes/plugins")
    env = child_env()
    assert env == dict(os.environ)
    env["AUTRE"] = "1"  # une copie : os.environ n'est pas touché
    assert "AUTRE" not in os.environ


class FakeRun:
    """subprocess.run simulé : wl-copy, wl-paste et ydotool répondent selon `results`."""

    def __init__(self, results: dict[str, object] | None = None) -> None:
        self.results = results or {}
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        outcome = self.results.get(os.path.basename(cmd[0]), 0)
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(cmd, outcome, stdout="", stderr="")

    def ran(self, name: str) -> list[list[str]]:
        return [cmd for cmd, _ in self.calls if os.path.basename(cmd[0]) == name]


@pytest.fixture
def wayland(monkeypatch):
    tools = {"wl-copy", "wl-paste", "ydotool"}
    monkeypatch.setattr(bridge, "which", lambda *names: next((f"/usr/bin/{n}" for n in names if n in tools), None))
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)

    def make(results=None):
        run = FakeRun(results)
        monkeypatch.setattr(subprocess, "run", run)
        deferred = []
        return WaylandBridge(lambda ms, fn: deferred.append(fn)), run, deferred

    return make


def _capture() -> Capture:
    return Capture("Ces un test", from_selection=True, saved_clipboard="mot de passe secret")


def test_wl_copy_does_not_wait_for_its_background_server(wayland, package_env):
    """wl-copy reste en arrière-plan pour servir le presse-papiers : ses sorties partent vers /dev/null
    (sinon subprocess.run attend leur fermeture jusqu'au délai), le texte passe par l'entrée standard."""
    wl, run, _ = wayland()
    assert wl.copy("C'est un test")
    (cmd, kwargs), = run.calls
    assert cmd == ["/usr/bin/wl-copy"]
    assert kwargs["input"] == "C'est un test"
    assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.DEVNULL
    assert "capture_output" not in kwargs
    assert not set(_QT_VARIABLES) & set(kwargs["env"])


@pytest.mark.parametrize("failure", [1, subprocess.TimeoutExpired("wl-copy", 1.5), OSError("introuvable")],
                         ids=["code d'erreur", "délai dépassé", "lancement impossible"])
def test_wayland_never_pastes_when_copy_failed(wayland, failure):
    """Ctrl+V après une copie ratée collerait l'ancien presse-papiers (un mot de passe, par exemple)."""
    wl, run, deferred = wayland({"wl-copy": failure})
    ok, message = wl.replace(_capture(), "C'est un test")
    assert not ok
    assert "wl-copy" in message and "rien n'a été collé" in message
    assert run.ran("ydotool") == []
    assert deferred == []
    assert wl.copy("texte") is False


def test_wayland_pastes_after_successful_copy(wayland):
    wl, run, deferred = wayland()
    ok, message = wl.replace(_capture(), "C'est un test")
    assert ok, message
    assert run.ran("ydotool") == [["/usr/bin/ydotool", "key", "29:1", "47:1", "47:0", "29:0"]]
    assert len(deferred) == 1  # restauration de l'ancien presse-papiers


@pytest.mark.skipif(sys.platform == "win32", reason="script shell : Linux")
def test_real_wl_copy_with_background_child_returns_at_once(tmp_path, monkeypatch):
    """Faux wl-copy qui, comme le vrai, laisse un processus en arrière-plan avec ses sorties ouvertes."""
    received = tmp_path / "presse-papiers.txt"
    script = tmp_path / "wl-copy"
    script.write_text(f'#!/bin/sh\ncat > "{received}"\n(sleep 4) &\nexit 0\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(bridge, "which", lambda *names: str(script) if "wl-copy" in names else None)
    start = time.monotonic()
    assert WaylandBridge(lambda ms, fn: None).copy("Texte corrigé, accentué")
    assert time.monotonic() - start < 1.0
    assert received.read_text(encoding="utf-8") == "Texte corrigé, accentué"


def test_x11_never_pastes_when_copy_failed(monkeypatch):
    x11 = X11Bridge(lambda ms, fn: None)
    pasted = []
    monkeypatch.setattr(x11, "_write", lambda text: False)
    monkeypatch.setattr(x11, "_activate", lambda wid: True)
    monkeypatch.setattr(x11, "_send_ctrl", pasted.append)
    capture = Capture("Ces un test", target=42, from_selection=True, saved_clipboard="mot de passe secret")
    ok, message = x11.replace(capture, "C'est un test")
    assert not ok and "rien n'a été collé" in message
    assert pasted == []
    assert x11.copy("texte") is False


def _windows_bridge(set_ok: bool, activate_ok: bool):
    w = SimpleNamespace(
        pasted=[],
        activate_window=lambda hwnd: activate_ok,
        set_clipboard_text=lambda text: set_ok,
        wait_modifiers_released=lambda *a: None,
        set_clipboard_formats=lambda formats: True,
    )
    w.send_ctrl = w.pasted.append
    win = WindowsBridge.__new__(WindowsBridge)  # sans charger win32 (Windows uniquement)
    win.w = w
    win._defer = lambda ms, fn: None
    win._listener = None
    return win, w


@pytest.mark.parametrize("activate_ok", [True, False], ids=["fenêtre trouvée", "fenêtre introuvable"])
def test_windows_never_pastes_when_copy_failed(monkeypatch, activate_ok):
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    win, w = _windows_bridge(set_ok=False, activate_ok=activate_ok)
    ok, message = win.replace(Capture("Ces un test", target=1, from_selection=True), "C'est un test")
    assert not ok and "rien n'a été collé" in message
    assert w.pasted == []
    assert win.copy("texte") is False


def test_windows_pastes_after_successful_copy(monkeypatch):
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    win, w = _windows_bridge(set_ok=True, activate_ok=True)
    ok, _ = win.replace(Capture("Ces un test", target=1, from_selection=True), "C'est un test")
    assert ok and w.pasted == ["v"]


def test_system_programs_get_child_env(monkeypatch, package_env):
    """gsettings, dconf et xdotool lancés depuis le paquet ne reçoivent pas les plugins Qt du paquet."""
    from correcteur.platform import autostart

    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(bridge, "which", lambda *names: "/usr/bin/xdotool")
    autostart._gsettings("get", "org.gnome.desktop.interface", "clock-format")
    monkeypatch.setattr("correcteur.platform.which", lambda *names: "/usr/bin/dconf")
    autostart._other_keybinding_dirs()
    x11 = X11Bridge(lambda ms, fn: None)
    monkeypatch.setitem(sys.modules, "Xlib", None)  # python-xlib indisponible : repli sur xdotool
    x11._active_window()
    names = [os.path.basename(cmd[0]) for cmd, _ in run.calls]
    assert names == ["gsettings", "dconf", "xdotool"]
    for _, kwargs in run.calls:
        assert not set(_QT_VARIABLES) & set(kwargs["env"])
