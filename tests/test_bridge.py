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


_TEXT_TYPES = ["text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT"]


class FakeDesktop:
    """wl-paste, wl-copy et ydotool simulés autour d'un presse-papiers : son texte, ses formats, la sélection
    primaire, et ce que l'application copie quand elle reçoit Ctrl+C. Chaque appel est noté avec l'heure."""

    def __init__(self, clock: FakeClock, clipboard: str = "", types=None, primary: str = "",
                 selected: str | None = None) -> None:
        self.clock = clock
        self.clipboard = clipboard
        self.types = list(_TEXT_TYPES if types is None else types) if (clipboard or types) else []
        self.primary = primary
        self.selected = selected  # copié par l'application au Ctrl+C (None : rien de sélectionné)
        self.failures: dict[str, object] = {}  # outil -> exception levée, ou "lecture" pour wl-paste du texte
        self.calls: list[tuple[float, list[str], dict]] = []

    def __call__(self, cmd, **kwargs):
        name = os.path.basename(cmd[0])
        self.calls.append((self.clock.now, cmd, kwargs))
        self.clock.now += 0.05  # chaque programme prend un peu de temps
        failure = self.failures.get(name)
        if isinstance(failure, BaseException):
            raise failure

        def done(code=0, out="", err=""):
            return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=err)

        if name == "wl-paste":
            if "--primary" in cmd:
                return done(0, self.primary) if self.primary else done(1, err="Nothing is copied\n")
            if not self.types:
                return done(1, err="Nothing is copied\n")
            if "--list-types" in cmd:
                return done(0, "\n".join(self.types) + "\n")
            if failure == "lecture":
                raise subprocess.TimeoutExpired(cmd, 1.5)
            if not bridge._text_only(self.types):
                return done(1, err="No suitable type of content copied\n")
            return done(0, self.clipboard)
        if name == "wl-copy":
            self.clipboard, self.types = kwargs["input"], list(_TEXT_TYPES)
            return done(0, None, None)
        if name == "ydotool" and "46:1" in cmd and self.selected is not None:
            self.clipboard, self.types = self.selected, list(_TEXT_TYPES)
        return done()

    def ran(self, name: str) -> list[list[str]]:
        return [cmd for _, cmd, _ in self.calls if os.path.basename(cmd[0]) == name]

    def times(self, name: str) -> list[float]:
        return [at for at, cmd, _ in self.calls if os.path.basename(cmd[0]) == name]

    def copies(self) -> list[str]:
        return [kwargs["input"] for _, cmd, kwargs in self.calls if os.path.basename(cmd[0]) == "wl-copy"]


@pytest.fixture
def desktop(wayland, monkeypatch, clock):
    """desktop(**état) : (pont Wayland, faux bureau, restaurations différées)."""

    def make(**state):
        wl, _, deferred = wayland()
        fake = FakeDesktop(clock, **state)
        monkeypatch.setattr(subprocess, "run", fake)
        return wl, fake, deferred

    return make


_RELEASES = ["29:0", "97:0", "56:0", "100:0", "42:0", "54:0", "125:0", "126:0"]


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
    assert ok
    # Modificateurs relâchés d'abord : Ctrl+V, pas Ctrl+Alt+V.
    assert run.ran("ydotool") == [["/usr/bin/ydotool", "key", *_RELEASES, "29:1", "47:1", "47:0", "29:0"]]
    # Pas de wl-copy de restauration après le collage : l'ancien presse-papiers sera remis par Qt (on_focus).
    assert deferred == [] and run.ran("wl-copy") == [["/usr/bin/wl-copy"]]
    assert "prochaine ouverture du correcteur" in message


def test_wayland_marks_only_a_text_clipboard(desktop):
    """Le marqueur remplaçait tout le presse-papiers, puis seul l'ancien texte était rendu : une image copiée
    était perdue (régression de la 0.1.5)."""
    wl, fake, _ = desktop(types=["image/png", "image/jpeg"])
    capture = wl.capture()  # rien de sélectionné : le Ctrl+C ne copie rien
    assert capture.text == "" and not capture.clipboard_touched
    assert fake.ran("wl-copy") == [] and fake.types == ["image/png", "image/jpeg"]
    wl.release(capture)
    assert fake.ran("wl-copy") == []

    # Texte mis en forme (page web) : pas de marqueur non plus, et son texte n'est pas pris pour la sélection.
    wl, fake, _ = desktop(clipboard="gras", types=["text/html", "text/plain;charset=utf-8"])
    capture = wl.capture()
    assert capture.text == "" and fake.ran("wl-copy") == []

    # Image, puis texte sélectionné copié par le Ctrl+C : rien à rendre (l'image ne se rend pas).
    wl, fake, _ = desktop(types=["image/png"], selected="Ces un test")
    capture = wl.capture()
    assert capture.text == "Ces un test" and capture.saved_clipboard is None
    wl.release(capture)
    assert fake.ran("wl-copy") == []

    # Texte : marqué avant le Ctrl+C, puis rendu.
    wl, fake, _ = desktop(clipboard="ancien")
    capture = wl.capture()
    assert capture.text == "" and capture.saved_clipboard == "ancien" and capture.clipboard_touched
    assert fake.copies()[0].startswith("\u2063correcteur")
    wl.release(capture)
    assert fake.copies()[-1] == "ancien" and fake.clipboard == "ancien"


def test_wayland_marker_finds_a_selection_already_copied(desktop):
    """Texte copié juste avant d'être sélectionné : sans marqueur, le Ctrl+C ne changeait rien."""
    wl, fake, _ = desktop(clipboard="Ces un test", selected="Ces un test")
    capture = wl.capture()
    assert capture.text == "Ces un test" and capture.from_selection
    assert capture.saved_clipboard == "Ces un test"


def test_wayland_capture_never_uses_qt_clipboard(desktop, monkeypatch):
    """Sans wl-copy, le marqueur était écrit par Qt depuis le fil de la capture."""
    monkeypatch.setattr(bridge, "which", lambda *names: next((f"/usr/bin/{n}" for n in names
                                                                if n in ("wl-paste", "ydotool")), None))

    def forbidden(*args):
        raise AssertionError("presse-papiers de Qt hors du fil de l'interface")

    monkeypatch.setattr(bridge, "_qt_set_clipboard", forbidden)
    monkeypatch.setattr(bridge, "_qt_saved_text", forbidden)
    wl, fake, _ = desktop(clipboard="ancien", selected="Ces un test")
    capture = wl.capture()
    assert capture.text == "Ces un test"  # comparaison avec le contenu d'avant


def test_wayland_paste_failure_is_none_and_never_restored(desktop):
    """wl-paste qui dépasse son délai renvoyait "" : la restauration écrivait alors un presse-papiers vide et
    ce que l'utilisateur avait copié était perdu."""
    wl, fake, _ = desktop(clipboard="mot de passe", selected="Ces un test")
    fake.failures["wl-paste"] = "lecture"
    assert wl._paste(primary=False) is None
    capture = wl.capture()
    # Pas de contenu d'avant pour comparer : pas de Ctrl+C du tout, plutôt que de prendre l'ancien presse-papiers
    # (un mot de passe) pour la sélection et l'envoyer à LanguageTool.
    assert capture.text == "" and not capture.clipboard_touched
    assert fake.ran("wl-copy") == [] and fake.ran("ydotool") == []
    wl.release(capture)
    assert fake.ran("wl-copy") == []
    ok, message = wl.replace(Capture("Ces un test", from_selection=True), "C'est un test")
    assert ok and "n'a pas pu être gardé" in message
    assert fake.copies() == ["C'est un test"]
    # Presse-papiers vide : "" (rien d'autre à rendre), pas None.
    wl, fake, _ = desktop()
    assert wl._paste(primary=False) == "" and wl._saved_text() == ""


def test_wayland_waits_for_the_shortcut_keys_before_ctrl_c(desktop, monkeypatch, clock):
    """Le raccourci GNOME part à l'appui : un Ctrl+C simulé aussitôt devenait Ctrl+Alt+C (qui relance le
    raccourci). On attend que l'utilisateur ait relâché ses touches, et sous GNOME que le focus soit revenu à
    l'application après wl-paste et wl-copy."""
    for desktop_name, focus_wait in (("sway", 0.0), ("gnome", 0.5)):
        monkeypatch.setattr(bridge, "desktop_name", lambda: desktop_name)
        wl, fake, _ = desktop(clipboard="ancien", selected="Ces un test")
        start = clock.now
        capture = wl.capture()
        assert capture.text == "Ces un test" and capture.started == start
        (ctrl_c,), (at,) = fake.ran("ydotool"), fake.times("ydotool")
        assert ctrl_c[2:] == [*_RELEASES, "29:1", "46:1", "46:0", "29:0"]
        assert at >= start + 0.3
        last_tool = max(t for t, cmd, _ in fake.calls if "wl-" in os.path.basename(cmd[0]) and t < at)
        assert at >= last_tool + 0.05 + focus_wait


def test_wayland_express_waits_for_focus_after_wl_copy(desktop, monkeypatch):
    """Correction express sous GNOME : la surface de wl-copy prend le focus ; Ctrl+V seulement 0,5 s après."""
    monkeypatch.setattr(bridge, "desktop_name", lambda: "gnome")
    wl, fake, _ = desktop(clipboard="ancien")
    ok, _ = wl.replace(Capture("Ces un test", from_selection=True), "C'est un test")
    assert ok
    (copy_at,), (paste_at,) = [fake.times("wl-copy")[-1]], fake.times("ydotool")
    assert paste_at >= copy_at + 0.05 + 0.5


def test_wayland_window_replace_uses_only_qt_clipboard(desktop, monkeypatch):
    """Remplacement depuis la fenêtre : wl-paste et wl-copy, lancés juste après sa fermeture, prenaient le
    focus à l'application d'origine sous GNOME, et le Ctrl+V tombait à côté. Le presse-papiers est préparé par
    Qt pendant que la fenêtre a le focus, puis seul le Ctrl+V part."""
    qt = []
    monkeypatch.setattr(bridge, "_qt_saved_text", lambda: "ancien")
    monkeypatch.setattr(bridge, "_qt_set_clipboard", lambda text: qt.append(text) or True)
    wl, fake, _ = desktop(clipboard="ancien")
    capture = Capture("Ces un test", from_selection=True)
    assert wl.stage_replace(capture, "C'est un test") == (True, "")
    assert qt == ["C'est un test"] and fake.calls == []
    ok, message = wl.paste_staged(capture)
    assert ok and "prochaine ouverture" in message
    assert [os.path.basename(cmd[0]) for _, cmd, _ in fake.calls] == ["ydotool"]
    assert capture.saved_clipboard == "ancien"

    # Copie impossible : rien n'est collé.
    monkeypatch.setattr(bridge, "_qt_set_clipboard", lambda text: False)
    ok, message = wl.stage_replace(Capture("Ces un test", from_selection=True), "C'est un test")
    assert not ok and "rien n'a été collé" in message


def test_wayland_restores_by_qt_when_the_correcteur_gets_focus(desktop, monkeypatch, clock):
    """Ancien presse-papiers remis par Qt à la prochaine ouverture du correcteur, seulement s'il contient encore
    le texte collé (sinon l'utilisateur a copié autre chose depuis), jamais pendant une capture."""
    clipboard = {"text": "ancien"}
    writes = []
    monkeypatch.setattr(bridge, "_qt_saved_text", lambda: clipboard["text"])
    monkeypatch.setattr(bridge, "_qt_clipboard", lambda: SimpleNamespace(text=lambda: clipboard["text"]))

    def qt_set(text):
        clipboard["text"] = text
        writes.append(text)
        return True

    monkeypatch.setattr(bridge, "_qt_set_clipboard", qt_set)
    wl, fake, _ = desktop()
    capture = Capture("Ces un test", from_selection=True)
    wl.stage_replace(capture, "C'est un test")
    wl.paste_staged(capture)
    monkeypatch.setattr(bridge, "_qt_focused", lambda: True)
    wl.on_focus()  # trop tôt : l'application lit peut-être encore le collage
    assert writes == ["C'est un test"]
    clock.now += 2
    wl.begin_capture()
    wl.on_focus()  # pendant une capture : jamais
    assert writes == ["C'est un test"]
    wl.end_capture(Capture(""))
    wl.on_focus()
    assert writes == ["C'est un test", "ancien"] and fake.calls[-1][1][0].endswith("ydotool")
    wl.on_focus()  # une seule fois
    assert writes == ["C'est un test", "ancien"]

    # L'utilisateur a copié autre chose depuis le collage : on le garde.
    capture = Capture("Ces un test", from_selection=True)
    wl.stage_replace(capture, "C'est un test")
    wl.paste_staged(capture)
    clipboard["text"] = "copié ensuite"
    clock.now += 2
    wl.on_focus()
    assert clipboard["text"] == "copié ensuite"


def test_wayland_focused_capture_reads_with_qt(desktop, monkeypatch):
    """Fenêtre du correcteur au premier plan : la sélection primaire est lue par Qt, sans wl-paste."""
    from PySide6.QtGui import QClipboard

    modes = []
    fake_clipboard = SimpleNamespace(supportsSelection=lambda: True,
                                     text=lambda mode: modes.append(mode) or "Texte choisi")
    monkeypatch.setattr(bridge, "_qt_clipboard", lambda: fake_clipboard)
    wl, fake, _ = desktop(primary="autre")
    capture = wl.focused_capture()
    assert capture.text == "Texte choisi" and capture.from_selection
    assert modes == [QClipboard.Mode.Selection] and fake.calls == []
    fake_clipboard.supportsSelection = lambda: False
    assert wl.focused_capture() is None  # capture ordinaire


def test_restorations_wait_for_the_end_of_a_capture(desktop):
    """Fenêtre fermée (release) pendant une capture : l'ancien contenu remplaçait le marqueur et passait pour la
    copie du texte sélectionné. La restauration attend la fin de la capture."""
    wl, fake, _ = desktop(clipboard="ancien")
    first = wl.capture()  # rien de sélectionné : marqueur laissé, à effacer
    copies = len(fake.copies())
    wl.begin_capture()
    wl.release(first)
    assert len(fake.copies()) == copies
    wl.end_capture(Capture(""))
    assert fake.copies()[-1] == "ancien"
    wl.release(first)  # déjà rendu : rien de plus
    assert fake.copies()[-1] == "ancien" and len(fake.copies()) == copies + 1


def test_x11_delayed_restore_waits_for_the_capture(monkeypatch, clock):
    """Restauration différée de replace() (+1,2 s) arrivant pendant la capture suivante : reportée à sa fin, et
    la capture garde l'ancien presse-papiers d'origine, pas le texte corrigé encore en place."""
    deferred, written = [], []
    x11 = X11Bridge(lambda ms, fn: deferred.append(fn))
    clipboard = {"text": "ancien"}
    monkeypatch.setattr(x11, "_saved_text", lambda: clipboard["text"])
    monkeypatch.setattr(x11, "_write", lambda text, qt_fallback=True: written.append(text) or clipboard.update(text=text) or True)
    monkeypatch.setattr(x11, "_activate", lambda wid: True)
    monkeypatch.setattr(x11, "_send_ctrl", lambda letter: None)
    ok, _ = x11.replace(Capture("Ces un test", target=1, from_selection=True), "C'est un test")
    assert ok and written == ["C'est un test"] and len(deferred) == 1
    x11.begin_capture()
    deferred[0]()  # pendant la capture : rien
    assert written == ["C'est un test"]
    # La capture a sauvé le texte corrigé encore en place : c'est l'ancien contenu qu'elle devra rendre.
    second = Capture("Ils sont parti", from_selection=True, saved_clipboard="C'est un test", clipboard_touched=True)
    x11.end_capture(second)
    assert written == ["C'est un test", "ancien"]
    assert second.saved_clipboard == "ancien"


def test_x11_marks_only_a_text_clipboard(monkeypatch, clock):
    from correcteur.platform import x11_selection

    x11 = X11Bridge(lambda ms, fn: None)
    state, written, keys = {}, [], []

    def write(text, qt_fallback=True):
        written.append((text, qt_fallback))
        state["clipboard"] = text
        return True

    monkeypatch.setattr(x11, "_active_window", lambda: None)
    monkeypatch.setattr(x11, "_read", lambda selection: None if selection == "PRIMARY" else state["clipboard"])
    monkeypatch.setattr(x11, "_write", write)
    monkeypatch.setattr(x11, "_send_ctrl", keys.append)
    # (cibles TARGETS, texte lisible, marqué ?, Ctrl+C envoyé ?)
    for targets, clipboard, marked, sent in ((["TARGETS", "image/png", "application/x-qt-image"], None, False, True),
                                             (["TARGETS", "text/html", "UTF8_STRING"], "gras", False, True),
                                             (["TARGETS", "UTF8_STRING", "text/plain;charset=utf-8"], "ancien", True,
                                              True),
                                             ([], None, True, True),       # presse-papiers vide
                                             (None, "ancien", False, True),  # pas de réponse à TARGETS
                                             (["UTF8_STRING"], None, False, False)):  # texte illisible
        written.clear()
        keys.clear()
        state["clipboard"] = clipboard
        monkeypatch.setattr(x11_selection, "selection_targets", lambda selection, t=targets: t)
        capture = x11.capture()  # rien de sélectionné : le Ctrl+C ne copie rien
        assert capture.text == "" and keys == (["c"] if sent else []), targets
        assert bool(written) is marked and capture.clipboard_touched is marked, targets
        if marked:
            # Capture hors du fil de l'interface : jamais le presse-papiers de Qt pour le marqueur.
            assert written[0][0].startswith("\u2063correcteur") and written[0][1] is False
            assert capture.saved_clipboard == (clipboard or "")


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
    Bridge.__init__(win, lambda ms, fn: None)
    win.w = w
    win._listener = None
    return win, w


@pytest.mark.parametrize("activate_ok", [True, False], ids=["fenêtre trouvée", "fenêtre introuvable"])
def test_windows_never_pastes_when_copy_failed(clock, activate_ok):
    win, w = _windows_bridge(set_ok=False, activate_ok=activate_ok)
    ok, message = win.replace(Capture("Ces un test", target=1, from_selection=True), "C'est un test")
    assert not ok and "rien n'a été collé" in message
    assert w.pasted == []
    assert win.copy("texte") is False


def test_windows_pastes_after_successful_copy(clock):
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
