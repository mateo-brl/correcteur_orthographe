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
from correcteur.platform.bridge import Bridge, Capture, WaylandBridge, WindowsBridge, X11Bridge

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


class FakeClock:
    """Horloge du pont simulée : les attentes avancent le temps sans attendre."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(bridge, "time", SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep,
                                                        monotonic_ns=time.monotonic_ns))
    return fake


@pytest.fixture
def wayland(monkeypatch, clock):
    tools = {"wl-copy", "wl-paste", "ydotool"}
    monkeypatch.setattr(bridge, "which", lambda *names: next((f"/usr/bin/{n}" for n in names if n in tools), None))
    monkeypatch.setattr(bridge, "_qt_focused", lambda: False)
    monkeypatch.setattr(bridge, "_qt_owns_clipboard", lambda: False)

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


def test_x11_delayed_restore_waits_for_the_capture(monkeypatch, clock):
    """Restauration différée de replace() (+1,2 s) arrivant pendant la capture suivante : reportée à sa fin, puis
    abandonnée, car la capture suivante a repris l'ancien presse-papiers d'origine (pas le texte corrigé encore en
    place) et le rendra elle-même. Avant, il était remis au milieu de la correction suivante."""
    deferred, written = [], []
    x11 = X11Bridge(lambda ms, fn: deferred.append(fn))
    clipboard = {"text": "ancien"}
    monkeypatch.setattr(x11, "_saved_text", lambda: clipboard["text"])
    monkeypatch.setattr(x11, "_write", lambda text, qt_fallback=True: written.append(text) or clipboard.update(text=text) or True)
    monkeypatch.setattr(x11, "_activate", lambda wid: True)
    monkeypatch.setattr(x11, "_send_keys", lambda action: None)
    ok, _ = x11.replace(Capture("Ces un test", target=1, from_selection=True), "C'est un test")
    assert ok and written == ["C'est un test"] and len(deferred) == 1
    x11.begin_capture()
    deferred[0]()  # pendant la capture : rien
    assert written == ["C'est un test"]
    # La capture a sauvé le texte corrigé encore en place : c'est l'ancien contenu qu'elle devra rendre.
    second = Capture("Ils sont parti", from_selection=True, saved_clipboard="C'est un test", clipboard_touched=True)
    x11.end_capture(second)
    assert written == ["C'est un test"]
    assert second.saved_clipboard == "ancien"
    x11.release(second)  # rien à corriger : la 2e capture rend le contenu d'origine
    assert written == ["C'est un test", "ancien"]


def test_x11_marks_only_a_text_clipboard(monkeypatch, clock):
    """Marqueur seulement sur un presse-papiers de texte lisible, posé sous un format à nous (sans format texte,
    les historiques du presse-papiers l'ignorent), et copie simulée par Ctrl+Inser, jamais Ctrl+C."""
    from correcteur.platform import x11_selection

    x11 = X11Bridge(lambda ms, fn: None)
    state, marked_with, keys = {}, [], []

    def write_marker(marker):
        marked_with.append(marker)
        state["clipboard"] = None  # le marqueur n'offre aucun texte
        x11._owner = SimpleNamespace(owns=lambda: True)
        return True

    monkeypatch.setattr(x11, "_active_window", lambda: None)
    monkeypatch.setattr(x11, "_read", lambda selection: None if selection == "PRIMARY" else state["clipboard"])
    monkeypatch.setattr(x11, "_write", lambda text, qt_fallback=True: pytest.fail("marqueur écrit en texte"))
    monkeypatch.setattr(x11, "_write_marker", write_marker)
    monkeypatch.setattr(x11, "_send_keys", keys.append)
    # (cibles TARGETS, texte lisible, marqué ?, copie envoyée ?)
    for targets, clipboard, marked, sent in ((["TARGETS", "image/png", "application/x-qt-image"], None, False, True),
                                             (["TARGETS", "text/html", "UTF8_STRING"], "gras", False, True),
                                             (["TARGETS", "UTF8_STRING", "text/plain;charset=utf-8"], "ancien", True,
                                              True),
                                             ([], None, True, True),       # presse-papiers vide
                                             (None, "ancien", False, True),  # pas de réponse à TARGETS
                                             (["UTF8_STRING"], None, False, False)):  # texte illisible
        marked_with.clear()
        keys.clear()
        state["clipboard"] = clipboard
        monkeypatch.setattr(x11_selection, "selection_targets", lambda selection, t=targets: t)
        capture = x11.capture()  # rien de sélectionné : la copie simulée ne copie rien
        assert capture.text == "" and keys == (["copy"] if sent else []), targets
        assert bool(marked_with) is marked and capture.clipboard_touched is marked, targets
        if marked:
            assert marked_with[0].startswith("\u2063correcteur")
            assert capture.saved_clipboard == (clipboard or "")


def test_x11_copy_is_ctrl_insert(monkeypatch):
    """Dans un terminal, Ctrl+C interrompait le programme en cours (« ^C ») : la copie simulée est Ctrl+Inser."""
    import contextlib
    import types

    pressed = []

    class Controller:
        def press(self, key):
            pressed.append(("appui", key))

        def release(self, key):
            pressed.append(("relâche", key))

        @contextlib.contextmanager
        def pressed(self, key):
            self.press(key)
            yield
            self.release(key)

    keys = SimpleNamespace(**{name: name for name in ("ctrl", "insert", "alt", "alt_r", "shift", "shift_r", "cmd")})
    module = types.ModuleType("pynput.keyboard")
    module.Controller, module.Key = Controller, keys
    monkeypatch.setitem(sys.modules, "pynput", types.ModuleType("pynput"))
    monkeypatch.setitem(sys.modules, "pynput.keyboard", module)
    X11Bridge._send_keys("copy")
    assert [p for p in pressed if p[0] == "appui"] == [("appui", "ctrl"), ("appui", "insert")]
    assert pressed[-2:] == [("relâche", "insert"), ("relâche", "ctrl")]


def test_x11_never_copies_over_a_copy_made_during_the_capture(monkeypatch, clock):
    """L'utilisateur copie autre chose pendant la capture : sa copie n'est pas écrasée par la copie simulée."""
    from correcteur.platform import x11_selection

    x11 = X11Bridge(lambda ms, fn: None)
    keys = []
    monkeypatch.setattr(x11, "_active_window", lambda: None)
    monkeypatch.setattr(x11, "_read", lambda selection: None if selection == "PRIMARY" else "T1")
    monkeypatch.setattr(x11_selection, "selection_targets", lambda selection: ["TARGETS", "UTF8_STRING"])
    monkeypatch.setattr(x11, "_write_marker",
                        lambda marker: setattr(x11, "_owner", SimpleNamespace(owns=lambda: False)) or True)
    monkeypatch.setattr(x11, "_send_keys", keys.append)
    capture = x11.capture()
    assert capture.text == "" and not capture.clipboard_touched and keys == []


def test_x11_uses_primary_only_from_the_active_application(monkeypatch, clock):
    """La sélection primaire reste celle de la dernière application où l'on a sélectionné : Ctrl+Alt+X dans
    Writer sans sélection corrigeait et collait le texte sélectionné dans Zen. Elle ne sert plus que si son
    propriétaire est le client X de la fenêtre active ; sinon, Ctrl+C simulé."""
    from correcteur.platform import x11_selection

    x11 = X11Bridge(lambda ms, fn: None)
    keys, asked = [], []
    monkeypatch.setattr(x11, "_active_window", lambda: 0x2400007)
    monkeypatch.setattr(x11, "_read", lambda selection: "Il pense a toi." if selection == "PRIMARY" else None)
    monkeypatch.setattr(x11, "_write", lambda text, qt_fallback=True: True)
    monkeypatch.setattr(x11, "_send_keys", keys.append)
    monkeypatch.setattr(x11, "_write_marker",
                        lambda marker: setattr(x11, "_owner", SimpleNamespace(owns=lambda: True)) or True)
    monkeypatch.setattr(x11_selection, "selection_targets", lambda selection: [])
    for same_client, expected in ((False, ""), (True, "Il pense a toi.")):
        keys.clear()
        monkeypatch.setattr(x11_selection, "selection_owned_by_client_of",
                            lambda selection, window, same=same_client: asked.append((selection, window)) or same)
        capture = x11.capture()
        assert capture.text == expected and keys == ([] if same_client else ["copy"])
    assert asked == [("PRIMARY", 0x2400007)] * 2
    monkeypatch.setattr(x11, "_active_window", lambda: None)  # fenêtre active inconnue : Ctrl+C
    assert x11.capture().text == "" and keys == ["copy"]


def test_x11_write_without_qt_fallback(monkeypatch):
    def forbidden(text):
        raise AssertionError("presse-papiers de Qt hors du fil de l'interface")

    monkeypatch.setattr(bridge, "_qt_set_clipboard", forbidden)
    x11 = X11Bridge(lambda ms, fn: None)
    x11._owner = SimpleNamespace(set_text=lambda text: False)
    assert x11._write("marqueur", qt_fallback=False) is False


def test_generic_bridge_never_restores_none():
    written = []
    generic = Bridge(lambda ms, fn: fn())
    generic._write_saved = written.append
    generic.release(Capture("", saved_clipboard=None, clipboard_touched=True))
    generic.release(Capture("", saved_clipboard="", clipboard_touched=False))
    assert written == []


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
    monkeypatch.setattr(x11, "_send_keys", pasted.append)
    capture = Capture("Ces un test", target=42, from_selection=True, saved_clipboard="mot de passe secret")
    ok, message = x11.replace(capture, "C'est un test")
    assert not ok and "rien n'a été collé" in message
    assert pasted == []
    assert x11.copy("texte") is False


class FakeWin:
    """Module win32 simulé : un presse-papiers (formats, numéro de séquence) et une application qui copie sa
    sélection quand on lui envoie la copie, puis colle ce qu'il contient."""

    CF_UNICODETEXT = 13
    HTML = 0xC0DE

    def __init__(self, selection: str = "Ces un test", html: bytes | None = None) -> None:
        self.clipboard: dict[int, bytes] = {13: self.text_bytes("témoin")}
        self.seq = 1
        self.selection, self.html = selection, html
        self.document: list[str] = []      # textes collés
        self.keys: list[str] = []
        self.set_ok, self.activate_ok = True, True
        self.foreground, self.focus = 100, 7
        self.classes = {100: "Notepad", 200: "Shell_TrayWnd"}
        self.elevated = False
        self.watch = None

    # presse-papiers
    @staticmethod
    def text_bytes(text, crlf=True):
        text = text.replace("\r\n", "\n")
        return (text.replace("\n", "\r\n") if crlf else text).encode("utf-16-le") + b"\0\0"

    def _write(self, formats):
        self.clipboard, self.seq = dict(formats), self.seq + 1

    def set_clipboard_formats(self, formats):
        if not self.set_ok:
            return False
        self._write(formats)
        return True

    def set_clipboard_text(self, text, crlf=True):
        return self.set_clipboard_formats({13: self.text_bytes(text, crlf)})

    def snapshot_clipboard(self):
        return dict(self.clipboard)

    def clipboard_sequence(self):
        return self.seq

    def get_clipboard_text(self):
        data = self.clipboard.get(13)
        return data[:-2].decode("utf-16-le") if data else None

    def get_clipboard_bytes(self, fmt):
        return self.clipboard.get(fmt)

    def html_format(self):
        return self.HTML

    # fenêtres et clavier
    def foreground_window(self):
        return self.foreground

    def window_class(self, hwnd):
        return self.classes.get(hwnd, "Notepad")

    def is_own_or_shell(self, hwnd):
        return self.window_class(hwnd) == "Shell_TrayWnd"

    def last_app_window(self):
        return 100

    def activate_window(self, hwnd):
        if self.activate_ok:
            self.foreground = hwnd
        return self.activate_ok

    def focused_control(self, hwnd):
        return self.focus

    def more_privileged(self, hwnd):
        return self.elevated

    def wait_modifiers_released(self, *args):
        pass

    def send_copy(self, hwnd):
        self.keys.append(f"copie:{self.window_class(hwnd)}")
        if self.selection and not self.elevated:
            formats = {13: self.text_bytes(self.selection)}
            if self.html:
                formats[self.HTML] = self.html
            self._write(formats)

    def send_ctrl(self, letter):
        self.keys.append(letter)
        if letter == "v":
            self.document.append(self.get_clipboard_text())

    def InputWatch(self):
        watch = SimpleNamespace(acted="", ok=True, stopped=False)
        watch.start = lambda: True
        watch.stop = lambda: setattr(watch, "stopped", True)
        self.watch = watch
        return watch

    def window_rect(self, hwnd):
        return (0, 0, 800, 600)

    def cursor_pos(self):
        return (1500, 300)

    def caret_point(self, hwnd):
        return None


def _windows(w: FakeWin):
    """Pont Windows sans charger win32 (Windows uniquement), restaurations différées notées dans `deferred`."""
    deferred = []
    win = WindowsBridge.__new__(WindowsBridge)
    Bridge.__init__(win, lambda ms, fn: deferred.append(fn))
    win.w, win._listener, win._watch = w, None, None
    return win, deferred


def _capture(win, express=True, from_menu=False):
    win.begin_capture()
    capture = win.capture(express=express, from_menu=from_menu)
    win.end_capture(capture)
    return capture


@pytest.mark.parametrize("activate_ok", [True, False], ids=["fenêtre trouvée", "fenêtre introuvable"])
def test_windows_never_pastes_when_copy_failed(clock, activate_ok):
    w = FakeWin()
    win, _ = _windows(w)
    capture = _capture(win)
    w.set_ok, w.activate_ok = False, activate_ok
    ok, message = win.replace(capture, "C'est un test")
    assert not ok and "rien n'a été collé" in message
    assert "v" not in w.keys
    assert win.copy("texte") is False


def test_windows_pastes_then_restores_the_clipboard(clock):
    w = FakeWin()
    win, deferred = _windows(w)
    capture = _capture(win)
    assert capture.text == "Ces un test" and w.keys == ["copie:Notepad"] and w.watch is not None
    ok, message = win.replace(capture, "C'est un test")
    assert ok and message == "" and w.document == ["C'est un test"] and w.watch.stopped
    for restore in deferred:
        restore()
    assert w.get_clipboard_text() == "témoin"


@pytest.mark.parametrize("acted, said", [("frappe", "tapé au clavier"), ("clic", "cliqué")])
def test_windows_does_not_paste_after_user_input(clock, acted, said):
    """B1 : l'utilisateur a tapé ou cliqué pendant la correction (« Ligne trois et la suiteQuel que soit... ») :
    rien n'est collé, le presse-papiers d'origine revient tout de suite."""
    w = FakeWin()
    win, _ = _windows(w)
    capture = _capture(win)
    w.watch.acted = acted
    ok, message = win.replace(capture, "C'est un test")
    assert not ok and said in message and "presse-papiers est intact" in message
    assert w.document == [] and "v" not in w.keys
    assert w.get_clipboard_text() == "témoin"


def test_windows_does_not_paste_when_the_focus_moved(clock):
    w = FakeWin()
    win, _ = _windows(w)
    capture = _capture(win)
    w.focus = 8  # autre zone de saisie de la même fenêtre
    ok, message = win.replace(capture, "C'est un test")
    assert not ok and w.document == [] and w.get_clipboard_text() == "témoin"


def test_windows_window_flow_does_not_compare_the_focus(clock):
    """Depuis la fenêtre de correction, l'application reprend le focus à son rythme : pas de comparaison."""
    w = FakeWin()
    win, _ = _windows(w)
    capture = _capture(win, express=False)
    w.focus = 0  # focus pas encore rendu au contrôle
    ok, _ = win.replace(capture, "C'est un test")
    assert ok and w.document == ["C'est un test"]


def test_windows_window_gone_leaves_the_corrected_text(clock):
    w = FakeWin()
    win, deferred = _windows(w)
    capture = _capture(win)
    w.activate_ok = False
    ok, message = win.replace(capture, "C'est un test")
    assert not ok and "Ctrl+V" in message and w.get_clipboard_text() == "C'est un test"
    win.release(capture)
    for restore in deferred:
        restore()
    assert w.get_clipboard_text() == "C'est un test"  # à coller par l'utilisateur : pas de restauration


def test_windows_burst_keeps_the_original_clipboard(clock):
    """B2 : corrections express en rafale. Chaque capture reprend le contenu d'origine (« témoin ») au lieu de
    sauver le texte laissé par la précédente, et les restaurations dépassées ne passent plus."""
    w = FakeWin()
    win, deferred = _windows(w)
    pasted = []
    for line in ("Il ces levé tôt.", "Mes parent son partis.", "Je les ai mit au frigo."):
        w.selection = line
        capture = _capture(win)
        assert capture.saved_clipboard == {13: w.text_bytes("témoin")}
        ok, _ = win.replace(capture, line.upper())
        assert ok
        pasted.append(line.upper())
    assert w.document == pasted
    for restore in deferred:
        restore()
    assert w.get_clipboard_text() == "témoin"


def test_windows_restore_skipped_when_the_user_copied_something(clock):
    w = FakeWin()
    win, deferred = _windows(w)
    capture = _capture(win)
    win.replace(capture, "C'est un test")
    w.set_clipboard_text("copié par l'utilisateur")
    for restore in deferred:
        restore()
    assert w.get_clipboard_text() == "copié par l'utilisateur"


def test_windows_tray_menu_goes_back_to_the_last_window(clock):
    """G6 : depuis le menu de l'icône, le premier plan est la barre des tâches : retour à la fenêtre de travail."""
    w = FakeWin()
    w.foreground = 200
    win, _ = _windows(w)
    capture = _capture(win, express=False, from_menu=True)
    assert capture.text == "Ces un test" and capture.target == 100 and w.keys == ["copie:Notepad"]


def test_windows_explains_elevated_windows(clock):
    """M4 : fenêtre lancée en administrateur, Windows bloque la copie simulée : on le dit."""
    w = FakeWin()
    w.elevated = True
    win, _ = _windows(w)
    capture = _capture(win)
    assert capture.text == "" and "administrateur" in capture.problem and w.watch.stopped
    win.release(capture)
    assert w.get_clipboard_text() == "témoin"


def test_windows_pastes_formatted_text_when_the_app_copied_some(clock):
    """G3 : Word et les navigateurs copient aussi du HTML : il est corrigé et recollé avec le texte, et les
    navigateurs reçoivent des fins de ligne \\n seules (Google Docs comptait \\r\\n deux fois)."""
    from correcteur.platform.cfhtml import correct_fragment

    fragment = "<p>Ces un <b>test</b></p><p>Il et&nbsp;sorti</p>"
    header = "Version:0.9\r\nStartHTML:0000000000\r\nEndHTML:0000000000\r\nStartFragment:0000000000\r\nEndFragment:0000000000\r\n"
    body = "<html><body><!--StartFragment-->" + fragment + "<!--EndFragment--></body></html>"
    start = len(header) + body.index(fragment)
    data = header.replace("StartHTML:0000000000", f"StartHTML:{len(header):010d}").replace(
        "EndHTML:0000000000", f"EndHTML:{len(header) + len(body.encode()):010d}").replace(
        "StartFragment:0000000000", f"StartFragment:{start:010d}").replace(
        "EndFragment:0000000000", f"EndFragment:{start + len(fragment.encode()):010d}").encode() + body.encode()
    w = FakeWin("Ces un test\r\nIl et sorti", data + b"\0")
    w.classes[100] = "Chrome_WidgetWin_1"
    win, _ = _windows(w)
    capture = _capture(win)
    assert capture.html is not None
    ok, _ = win.replace(capture, "C'est un test\nIl est sorti")
    assert ok
    assert w.clipboard[13] == w.text_bytes("C'est un test\nIl est sorti", crlf=False)
    html = w.clipboard[FakeWin.HTML].rstrip(b"\0").decode()
    assert "<p>C'est un <b>test</b></p><p>Il est&nbsp;sorti</p>" in html
    assert correct_fragment(fragment, "x", "y") is None  # texte introuvable : pas de HTML


def test_windows_window_opens_near_the_original_window(clock):
    """M1 : pointeur sur un autre écran : la fenêtre s'ouvre sur la fenêtre d'origine, pas sous le pointeur."""
    w = FakeWin()
    win, _ = _windows(w)
    capture = _capture(win, express=False)
    assert win.anchor(capture) == (400, 200)
    w.cursor_pos = lambda: (300, 300)  # pointeur sur la fenêtre d'origine : près de lui
    assert win.anchor(capture) is None


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


def test_java_gets_child_env(monkeypatch, package_env, tmp_path):
    """Le serveur LanguageTool local (Java) ne reçoit pas les chemins Qt du paquet."""
    from correcteur.engines import lt_server

    seen = []

    def popen(cmd, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(poll=lambda: None, terminate=lambda: None)

    monkeypatch.setattr(lt_server.subprocess, "Popen", popen)
    key = lt_server._Key(8081, 512, tmp_path / "languagetool-server.jar")
    lt_server._spawn(lt_server._Launch(key, ["java", "-jar", "x.jar"], tmp_path / "config.properties",
                                       tmp_path / "languagetool.log"))
    (kwargs,) = seen
    assert kwargs["env"]["PATH"] == os.environ["PATH"]
    assert not set(_QT_VARIABLES) & set(kwargs["env"])


def test_open_url_in_package_uses_xdg_open_with_child_env(monkeypatch, package_env):
    import correcteur.platform as platform

    launched = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append((cmd, kwargs))

        def wait(self):
            return 0

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "which", lambda *names: "/usr/bin/xdg-open")
    monkeypatch.setattr(platform.subprocess, "Popen", FakePopen)
    assert platform.open_url("https://languagetool.org/")
    (cmd, kwargs), = launched
    assert cmd == ["/usr/bin/xdg-open", "https://languagetool.org/"]
    assert not set(_QT_VARIABLES) & set(kwargs["env"])


def test_open_url_outside_package_uses_qt(monkeypatch, qapp):
    from PySide6.QtGui import QDesktopServices

    import correcteur.platform as platform

    opened = []
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(platform.subprocess, "Popen", lambda *a, **k: pytest.fail("xdg-open hors du paquet"))
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    assert platform.open_url("file:///tmp/dossier%20test")
    assert opened == ["file:///tmp/dossier test"]
