"""Pont Wayland (GNOME surtout) sur un faux bureau : presse-papiers à plusieurs formats, wl-paste, wl-copy (y
compris --foreground), ydotool, gdbus, et une application qui copie sa sélection au Ctrl+C et reçoit le Ctrl+V.
Le temps est simulé : les attentes du pont et les actions différées avancent l'horloge sans attendre."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from correcteur.platform import bridge
from correcteur.platform.bridge import Capture, SavedClipboard, WaylandBridge

TEXT_TYPES = ["text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT"]
RELEASES = ["29:0", "97:0", "56:0", "100:0", "42:0", "54:0", "125:0", "126:0"]
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


class FakeClock:
    """Horloge simulée, avec des actions programmées (actions différées du pont, gestes de l'utilisateur)."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.events: list[list] = []

    def monotonic(self) -> float:
        return self.now

    def at(self, when: float, fn) -> None:
        self.events.append([when, fn])

    def sleep(self, seconds: float) -> None:
        end = self.now + max(0.0, seconds)
        while True:
            due = [e for e in self.events if e[0] <= end]
            if not due:
                break
            event = min(due, key=lambda e: e[0])
            self.events.remove(event)
            self.now = max(self.now, event[0])
            event[1]()
        self.now = max(self.now, end)

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)


class QtOwner:
    """Le presse-papiers posé par Qt (fenêtre de correction)."""
    returncode = None


class FakeProc:
    """wl-copy --foreground (vivant tant qu'il possède le presse-papiers) ou wl-paste --type (lecture binaire)."""

    def __init__(self, desktop: "Desktop", cmd: list[str]) -> None:
        self.desktop, self.cmd, self.returncode = desktop, cmd, None
        self._input = b""
        if os.path.basename(cmd[0]) == "wl-paste":
            mime = cmd[cmd.index("--type") + 1]
            data = desktop.formats.get(mime)
            read, write = os.pipe()
            os.write(write, data or b"")
            os.close(write)
            self.stdout = os.fdopen(read, "rb")
            self.returncode = 0 if data is not None else 1
        else:
            self.stdin = SimpleNamespace(write=self._write, close=self._close)

    def _write(self, data: bytes) -> None:
        self._input += data

    def _close(self) -> None:
        self.desktop.calls.append((self.desktop.clock.now, self.cmd, {"input": self._input}))
        if self.desktop.failures.get("wl-copy"):
            self.returncode = 1
            return
        self.desktop.set_text(self._input.decode("utf-8"), owner=self)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class Desktop:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.formats: dict[str, bytes] = {}
        self.owner = None           # wl-copy --foreground ou Qt, s'ils possèdent le presse-papiers
        self.primary = ""
        self.selected: str | None = None  # sélection de l'application active, copiée au Ctrl+C
        self.idle_ms = 60_000       # temps depuis la dernière frappe (gdbus)
        self.pasted: list[object] = []
        self.calls: list[tuple[float, list[str], dict]] = []
        self.failures: dict[str, object] = {}

    # presse-papiers
    def set_text(self, text: str, owner=None) -> None:
        self._take(owner)
        self.formats = {t: text.encode("utf-8") for t in TEXT_TYPES}

    def set_formats(self, formats: dict[str, bytes], owner=None) -> None:
        self._take(owner)
        self.formats = dict(formats)

    def _take(self, owner) -> None:
        if self.owner is not None and self.owner is not owner:
            self.owner.returncode = 0  # sélection reprise par un autre : wl-copy --foreground s'arrête
        self.owner = owner

    def text(self) -> str | None:
        data = self.formats.get("text/plain;charset=utf-8")
        return data.decode("utf-8") if data is not None else None

    # programmes
    def run(self, cmd, **kwargs):
        name = os.path.basename(cmd[0])
        self.calls.append((self.clock.now, cmd, kwargs))
        self.clock.now += 0.05
        failure = self.failures.get(name)
        if isinstance(failure, BaseException):
            raise failure

        def done(code=0, out="", err=""):
            return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=err)

        args = cmd[1:]
        if name == "wl-paste":
            if "--primary" in args:
                return done(0, self.primary) if self.primary else done(1, err="Nothing is copied\n")
            if not self.formats:
                return done(1, err="Nothing is copied\n")
            if "--list-types" in args:
                return done(0, "".join(f"{t}\n" for t in self.formats))
            if failure == "lecture":
                raise subprocess.TimeoutExpired(cmd, 1.5)
            text = self.text()
            return done(0, text) if text is not None else done(1, err="No suitable type of content copied\n")
        if name == "wl-copy":
            if failure:
                return done(1)
            if args[:1] == ["--type"]:
                self.set_formats({args[1]: kwargs["input"]})
            elif args == ["--clear"]:
                self.set_formats({})
            else:
                self.set_text(kwargs["input"])
            return done(0, None, None)
        if name == "ydotool":
            if "46:1" in args and self.selected is not None:
                self.set_text(self.selected)
            if "47:1" in args:
                self.pasted.append(self.text() if self.text() is not None else dict(self.formats))
            return done()
        if name == "gdbus":
            return done(0, f"(uint64 {self.idle_ms},)\n")
        return done()

    def popen(self, cmd, **kwargs):
        return FakeProc(self, cmd)

    # constats
    def ran(self, name: str, *args: str) -> list[list[str]]:
        return [cmd for _, cmd, _ in self.calls
                if os.path.basename(cmd[0]) == name and all(a in cmd for a in args)]

    def times(self, name: str, *args: str) -> list[float]:
        return [at for at, cmd, _ in self.calls if os.path.basename(cmd[0]) == name and all(a in cmd for a in args)]

    def restores(self) -> list:
        """Ce que les restaurations (wl-copy hors --foreground) ont écrit."""
        out = []
        for _, cmd, kwargs in self.calls:
            if os.path.basename(cmd[0]) == "wl-copy" and "--foreground" not in cmd:
                data = kwargs.get("input")
                if isinstance(data, str) and data.startswith("⁣correcteur"):
                    continue  # marqueur de la capture
                out.append("<vide>" if "--clear" in cmd else data)
        return out


@pytest.fixture
def setup(monkeypatch):
    """setup(gnome=True) : (pont Wayland, faux bureau, horloge). Les actions différées passent par l'horloge, la
    fin des restaurations s'exécute sur place."""
    clock = FakeClock()
    monkeypatch.setattr(bridge, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep,
                                                        monotonic_ns=time.monotonic_ns, strftime=time.strftime))
    tools = {"wl-copy", "wl-paste", "ydotool", "gdbus"}
    monkeypatch.setattr(bridge, "which", lambda *names: next((f"/usr/bin/{n}" for n in names if n in tools), None))
    monkeypatch.setattr(bridge, "_qt_focused", lambda: False)
    monkeypatch.setattr(bridge, "_qt_owns_clipboard", lambda: False)

    def make(gnome: bool = True):
        desktop = Desktop(clock)
        monkeypatch.setattr(bridge, "desktop_name", lambda: "gnome" if gnome else "sway")
        monkeypatch.setattr(subprocess, "run", desktop.run)
        monkeypatch.setattr(subprocess, "Popen", desktop.popen)
        wl = WaylandBridge(lambda ms, fn: clock.at(clock.now + ms / 1000, fn))
        wl._spawn = lambda fn: fn()
        return wl, desktop, clock

    return make


def _qt(monkeypatch, desktop: Desktop, focused: bool = True, owns=None) -> list[str]:
    """Presse-papiers de Qt branché sur le faux bureau ; renvoie la liste des textes posés par Qt."""
    posed = []

    def qt_set(text):
        posed.append(text)
        desktop.set_text(text, owner=QtOwner())
        return True

    monkeypatch.setattr(bridge, "_qt_set_clipboard", qt_set)
    monkeypatch.setattr(bridge, "_qt_saved_text", lambda: desktop.text())
    monkeypatch.setattr(bridge, "_qt_focused", lambda: focused)
    monkeypatch.setattr(bridge, "_qt_owns_clipboard",
                        owns if owns is not None else (lambda: isinstance(desktop.owner, QtOwner)))
    return posed


def express(wl: WaylandBridge, corrected: str):
    """Correction express complète : capture, puis remplacement si du texte a été copié."""
    wl.begin_capture()
    capture = wl.capture()
    wl.end_capture(capture)
    if not capture.text:
        wl.release(capture)
        return capture, None
    return capture, wl.replace(capture, corrected)


# P1 : jamais la sélection primaire d'une autre application

def test_capture_ignores_the_primary_selection_of_another_window(setup):
    """Ctrl+A dans Zen sur « Il pense a toi. », Alt+Tab vers Writer sans rien sélectionner, Ctrl+Alt+X : le texte
    de Zen était corrigé et inséré dans Writer. La sélection primaire n'est plus lue : Ctrl+C simulé, et sans
    sélection dans l'application active, le marqueur reste."""
    wl, desktop, clock = setup()
    desktop.set_text("témoin")
    desktop.primary = "Il pense a toi."
    capture, outcome = express(wl, "Il pense à toi.")
    assert capture.text == "" and outcome is None
    assert desktop.pasted == [] and desktop.ran("wl-paste", "--primary") == []
    clock.advance(2)
    assert desktop.text() == "témoin" and desktop.restores() == ["témoin"]
    # Avec une sélection dans l'application active : c'est elle qui est prise.
    desktop.selected = "Il pense a toi."
    capture, (ok, _) = express(wl, "Il pense à toi.")
    assert capture.text == "Il pense a toi." and ok and desktop.pasted == ["Il pense à toi."]


def test_marker_finds_a_selection_already_copied(setup):
    """Texte copié juste avant d'être sélectionné : sans marqueur, le Ctrl+C ne changeait rien."""
    wl, desktop, _ = setup()
    desktop.set_text("Ces un test")
    desktop.selected = "Ces un test"
    capture = wl.capture()
    assert capture.text == "Ces un test" and capture.saved_clipboard == "Ces un test"


def test_capture_never_uses_qt_clipboard(setup, monkeypatch):
    """Capture hors du fil de l'interface : jamais le presse-papiers de Qt, même sans wl-copy."""
    wl, desktop, _ = setup()

    def forbidden(*args):
        raise AssertionError("presse-papiers de Qt hors du fil de l'interface")

    monkeypatch.setattr(bridge, "_qt_set_clipboard", forbidden)
    monkeypatch.setattr(bridge, "_qt_saved_text", forbidden)
    monkeypatch.setattr(bridge, "which", lambda *names: next((f"/usr/bin/{n}" for n in names
                                                                if n in ("wl-paste", "ydotool")), None))
    desktop.set_text("ancien")
    desktop.selected = "Ces un test"
    assert wl.capture().text == "Ces un test"  # comparaison avec le contenu d'avant


def test_unreadable_clipboard_is_never_captured_nor_restored(setup):
    """wl-paste qui dépasse son délai renvoyait "" : un presse-papiers vide était restauré. Et sans contenu d'avant
    pour comparer, l'ancien presse-papiers (un mot de passe) passerait pour la sélection : pas de Ctrl+C du tout."""
    wl, desktop, clock = setup()
    desktop.set_text("mot de passe")
    desktop.selected = "Ces un test"
    desktop.failures["wl-paste"] = "lecture"
    assert wl._paste(primary=False) is None
    capture = wl.capture()
    assert capture.text == "" and not capture.clipboard_touched and desktop.ran("ydotool") == []
    wl.release(capture)
    clock.advance(10)
    assert desktop.ran("wl-copy") == []
    del desktop.failures["wl-paste"]
    desktop.set_formats({})
    assert wl._paste(primary=False) == "" and wl._clipboard_before() == (True, "", "")


def test_waits_for_the_shortcut_keys_before_ctrl_c(setup):
    """Le raccourci GNOME part à l'appui : un Ctrl+C simulé aussitôt devenait Ctrl+Alt+C. On attend que
    l'utilisateur ait relâché ses touches, et sous GNOME que le focus soit revenu après wl-paste et wl-copy."""
    for gnome, focus_wait in ((False, 0.25), (True, 0.5)):
        wl, desktop, clock = setup(gnome=gnome)
        desktop.set_text("ancien")
        desktop.selected = "Ces un test"
        start = clock.now
        capture = wl.capture()
        assert capture.text == "Ces un test" and capture.started == start
        (ctrl_c,), (at,) = desktop.ran("ydotool"), desktop.times("ydotool")
        assert ctrl_c[2:] == [*RELEASES, "29:1", "46:1", "46:0", "29:0"]
        last_tool = max(t for t, cmd, _ in desktop.calls if os.path.basename(cmd[0]).startswith("wl-") and t < at)
        assert at >= start + 0.3 and at >= last_tool + 0.05 + focus_wait


def test_express_waits_for_focus_after_wl_copy(setup):
    """Correction express sous GNOME : la surface de wl-copy prend le focus ; Ctrl+V seulement 0,5 s après."""
    wl, desktop, _ = setup()
    desktop.set_text("ancien")
    desktop.selected = "Ces un test"
    _, (ok, _) = express(wl, "C'est un test")
    assert ok and desktop.pasted == ["C'est un test"]
    (copy_at,), (paste_at,) = desktop.times("wl-copy", "--foreground"), desktop.times("ydotool", "47:1")
    assert paste_at >= copy_at + 0.5


def test_express_never_pastes_when_copy_failed(setup):
    """Ctrl+V après une copie ratée collerait l'ancien presse-papiers (un mot de passe, par exemple)."""
    wl, desktop, _ = setup()
    desktop.set_text("mot de passe")
    desktop.selected = "Ces un test"
    desktop.failures["wl-copy"] = True
    wl.begin_capture()
    capture = wl.capture()
    wl.end_capture(capture)
    ok, message = wl.replace(capture, "C'est un test")
    assert not ok and "rien n'a été collé" in message and desktop.pasted == []


# Fenêtre de correction

def test_window_replace_uses_only_qt_clipboard(setup, monkeypatch):
    """Remplacement depuis la fenêtre : texte corrigé posé par Qt pendant qu'elle a le focus ; seul le Ctrl+V
    part ensuite (wl-paste et wl-copy, lancés après sa fermeture, prenaient le focus à l'application)."""
    wl, desktop, clock = setup()
    desktop.set_text("témoin")
    desktop.selected = "Ces un test"
    capture = wl.capture()  # la fenêtre s'ouvre (fil de travail), puis l'utilisateur corrige et remplace
    posed = _qt(monkeypatch, desktop)
    calls = len(desktop.calls)
    assert wl.stage_replace(capture, "C'est un test") == (True, "")
    assert posed == ["C'est un test"] and len(desktop.calls) == calls
    ok, message = wl.paste_staged(capture)
    assert ok and message == ""  # plus de promesse dans la notification
    assert [os.path.basename(cmd[0]) for _, cmd, _ in desktop.calls[calls:]] == ["ydotool"]
    assert desktop.pasted == ["C'est un test"]
    paste_at = clock.now
    clock.advance(1.4)
    assert desktop.restores() == []
    clock.advance(0.3)
    assert desktop.restores() == ["témoin"] and desktop.text() == "témoin"
    assert desktop.times("wl-copy")[-1] >= paste_at + 1.5


def test_window_replace_without_focus_goes_through_wl_copy(setup, monkeypatch):
    """Ctrl+Entrée avant la fin des vérifications : le remplacement part plus tard, sans fenêtre qui ait le
    focus ; Qt ne peut pas poser le presse-papiers (l'ancien était collé). stage_replace renvoie None."""
    wl, desktop, _ = setup()
    _qt(monkeypatch, desktop, focused=False)
    assert wl.stage_replace(Capture("Ces un test", from_selection=True), "C'est un test") is None


# P2 : restauration après le collage

def test_restore_once_after_paste_and_never_promised(setup):
    wl, desktop, clock = setup(gnome=False)
    desktop.set_text("témoin")
    desktop.selected = "Ces un test"
    _, (ok, message) = express(wl, "C'est un test")
    assert ok and message == ""
    paste_at = desktop.times("ydotool", "47:1")[0]
    clock.advance(10)
    assert desktop.restores() == ["témoin"] and desktop.text() == "témoin"
    assert paste_at + 0.5 <= desktop.times("wl-copy")[-1] < paste_at + 1.0  # 0,5 s hors de GNOME
    assert desktop.ran("gdbus") == []  # pause de saisie : seulement sous GNOME


def test_restore_waits_for_the_capture_and_a_typing_pause(setup):
    """Pendant une capture, la restauration remplacerait le marqueur ; pendant la frappe, la surface de wl-copy
    prendrait une touche. Elle attend la fin des deux."""
    wl, desktop, clock = setup()
    desktop.set_text("témoin")
    desktop.selected = "Ces un test"
    _, (ok, _) = express(wl, "C'est un test")
    paste_at = clock.now
    desktop.idle_ms = 50  # l'utilisateur tape
    wl.begin_capture()  # une capture commence, sans encore toucher au presse-papiers
    clock.at(paste_at + 2.0, lambda: wl.end_capture(Capture("")))
    clock.at(paste_at + 3.0, lambda: setattr(desktop, "idle_ms", 600))
    clock.advance(5)  # le fil de restauration simulé va jusqu'au bout : on regarde l'heure de l'écriture
    assert desktop.restores() == ["témoin"] and desktop.times("wl-copy")[-1] >= paste_at + 3.0
    assert len(desktop.ran("gdbus")) > 1


def test_no_restore_when_the_user_copied_something_else(setup, monkeypatch):
    wl, desktop, clock = setup()
    desktop.set_text("témoin")
    desktop.selected = "Ces un test"
    express(wl, "C'est un test")
    desktop.set_text("copie plus récente")  # wl-copy --foreground s'arrête : il n'a plus le presse-papiers
    clock.advance(10)
    assert desktop.restores() == [] and desktop.text() == "copie plus récente"

    # Fenêtre : Qt se croit encore propriétaire (indice périmé) ; le contenu relu tranche.
    wl, desktop, clock = setup()
    desktop.set_text("témoin")
    desktop.selected = "Ces un test"
    capture = wl.capture()
    _qt(monkeypatch, desktop, owns=lambda: True)
    wl.stage_replace(capture, "C'est un test")
    wl.paste_staged(capture)
    desktop.set_text("copie plus récente")
    clock.advance(10)
    assert desktop.restores() == [] and desktop.text() == "copie plus récente"


def test_restore_never_leaves_late(setup, caplog):
    """Pas de pause de saisie pendant 5 s : abandon, noté au journal. Plus jamais de restauration qui traîne."""
    wl, desktop, clock = setup()
    desktop.set_text("témoin")
    desktop.selected = "Ces un test"
    express(wl, "C'est un test")
    desktop.idle_ms = 10
    with caplog.at_level(logging.INFO, logger="correcteur.platform.bridge"):
        clock.advance(30)
    assert desktop.restores() == [] and desktop.text() == "C'est un test"
    assert any("abandonné après 5 s" in r.getMessage() for r in caplog.records)

    # Action différée exécutée très en retard (interface bloquée) : jamais.
    wl, desktop, clock = setup()
    desktop.set_text("témoin")
    desktop.selected = "Ces un test"
    late = []
    wl._defer = lambda ms, fn: late.append(fn)
    express(wl, "C'est un test")
    clock.now += 8
    late[0]()
    assert desktop.restores() == []


def test_newer_replace_cancels_the_older_restore(setup):
    """Deux corrections express coup sur coup : une seule restauration, celle du contenu d'origine."""
    wl, desktop, clock = setup()
    desktop.set_text("témoin")
    desktop.selected = "Ces un test"
    express(wl, "C'est un test")
    desktop.selected = "Il pense a toi."
    express(wl, "Il pense à toi.")
    assert desktop.pasted == ["C'est un test", "Il pense à toi."]
    clock.advance(10)
    assert desktop.restores() == ["témoin"]


# P3 : contenus qui ne sont pas du texte

def test_an_image_is_kept(setup):
    wl, desktop, clock = setup()
    desktop.set_formats({"image/png": PNG, "image/jpeg": b"jpeg"})
    desktop.selected = "Ces un test"
    _, (ok, message) = express(wl, "C'est un test")
    assert ok and message == ""
    clock.advance(5)
    assert desktop.formats == {"image/png": PNG}
    assert desktop.ran("wl-copy", "--type", "image/png")
    # Rien de sélectionné : marqueur posé puis image remise.
    desktop.selected = None
    capture, _ = express(wl, "x")
    clock.advance(5)
    assert capture.text == "" and desktop.formats == {"image/png": PNG}


def test_preferred_formats():
    assert bridge._preferred_type(["image/jpeg", "image/png", "text/plain"]) == "image/png"
    assert bridge._preferred_type(["text/plain", "image/webp"]) == "image/webp"
    assert bridge._preferred_type(["x-special/gnome-copied-files", "text/uri-list", "text/plain"]) \
        == "x-special/gnome-copied-files"
    assert bridge._preferred_type(["text/uri-list", "UTF8_STRING"]) == "text/uri-list"
    assert bridge._preferred_type(["text/html", "text/plain;charset=utf-8"]) is None  # le texte suffit
    assert bridge._preferred_type(["TARGETS", "application/x-tableur"]) == "application/x-tableur"


def test_too_big_content_is_said_lost(setup, monkeypatch):
    monkeypatch.setattr(bridge, "_MAX_SAVED", 100)
    wl, desktop, clock = setup()
    desktop.set_formats({"image/png": PNG})
    desktop.selected = "Ces un test"
    capture, (ok, message) = express(wl, "C'est un test")
    assert ok and "n'a pas pu être gardé" in message and capture.saved_clipboard is None
    clock.advance(10)
    assert desktop.restores() == []


def test_sequence_seen_on_gnome_never_brings_back_an_old_text(setup, monkeypatch):
    """Témoin copié, remplacement depuis la fenêtre, image copiée, Ctrl+Alt+X sur une sélection, puis
    Ctrl+Alt+X sans sélection : l'ancien témoin revenait par-dessus l'image (Qt se croyait encore propriétaire
    du presse-papiers, et son texte servait de sauvegarde)."""
    wl, desktop, clock = setup()
    desktop.set_text("témoin")
    desktop.selected = "Ces un test"
    capture = wl.capture()
    _qt(monkeypatch, desktop, owns=lambda: True)  # indice de Qt toujours vrai, même après une copie ailleurs
    wl.stage_replace(capture, "C'est un test")
    wl.paste_staged(capture)
    clock.advance(3)
    assert desktop.text() == "témoin"
    desktop.set_formats({"image/png": PNG})  # l'utilisateur copie une image
    monkeypatch.setattr(bridge, "_qt_focused", lambda: False)
    desktop.selected = "Il pense a toi."
    express(wl, "Il pense à toi.")
    clock.advance(3)
    desktop.selected = None
    express(wl, "x")
    clock.advance(10)
    assert desktop.formats == {"image/png": PNG}
    assert desktop.pasted == ["C'est un test", "Il pense à toi."]


def test_read_limited_with_a_real_program(tmp_path, monkeypatch):
    """Lecture binaire de wl-paste par un vrai processus : contenu exact, ou rien au-delà de la limite."""
    script = tmp_path / "wl-paste"
    script.write_text('#!/bin/sh\nhead -c "$TAILLE" /dev/zero\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(bridge, "which", lambda *names: str(script) if "wl-paste" in names else None)
    monkeypatch.setattr(bridge, "_MAX_SAVED", 300_000)
    wl = WaylandBridge(lambda ms, fn: None)
    monkeypatch.setenv("TAILLE", "200000")
    assert wl._paste_bytes("image/png") == bytes(200_000)
    monkeypatch.setenv("TAILLE", "400000")
    assert wl._paste_bytes("image/png") is None


# Divers

def test_focused_capture_reads_with_qt(setup, monkeypatch):
    """Fenêtre du correcteur au premier plan : la sélection primaire est lue par Qt, sans wl-paste."""
    from PySide6.QtGui import QClipboard

    wl, desktop, _ = setup()
    modes = []
    fake_clipboard = SimpleNamespace(supportsSelection=lambda: True,
                                     text=lambda mode: modes.append(mode) or "Texte choisi")
    monkeypatch.setattr(bridge, "_qt_clipboard", lambda: fake_clipboard)
    capture = wl.focused_capture()
    assert capture.text == "Texte choisi" and capture.from_selection
    assert modes == [QClipboard.Mode.Selection] and desktop.calls == []
    fake_clipboard.supportsSelection = lambda: False
    assert wl.focused_capture() is None


def test_diagnostics_show_the_gnome_shortcuts(setup, monkeypatch):
    wl, _, _ = setup()
    wl.desktop_shortcuts = {"correcteur-verifier": "<Control><Alt>c", "correcteur-express": "<Control><Alt>x"}
    assert "Raccourcis : GNOME, Ctrl+Alt+C (vérifier), Ctrl+Alt+X (correction express)" in wl.diagnostics()
    from correcteur.platform import autostart

    wl.desktop_shortcuts = None
    monkeypatch.setattr(autostart, "gnome_shortcuts", lambda: {})
    assert any(line.startswith("Raccourcis : à créer") for line in wl.diagnostics())


def test_saved_clipboard_is_never_equal_to_text():
    assert SavedClipboard("image/png", b"x") != "x"
