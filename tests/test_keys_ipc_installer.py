from __future__ import annotations

import sys
import threading
import time
import zipfile

import pytest

from correcteur.platform import keys


@pytest.mark.parametrize("raw, internal, shown", [
    ("<ctrl>+<alt>+c", "<ctrl>+<alt>+c", "Ctrl+Alt+C"),
    ("Ctrl+Alt+C", "<ctrl>+<alt>+c", "Ctrl+Alt+C"),
    ("ctrl+maj+f8", "<ctrl>+<shift>+<f8>", "Ctrl+Maj+F8"),
    ("Win+Shift+Space", "<shift>+<cmd>+<space>", "Maj+Win+Espace"),
    ("<ctrl_l>+<alt_r>+x", "<ctrl>+<alt>+x", "Ctrl+Alt+X"),
    ("F9", "<f9>", "F9"),
])
def test_parse_and_display(raw, internal, shown):
    assert keys.normalize(raw) == internal
    assert keys.display(raw) == shown


@pytest.mark.parametrize("raw", ["", "ctrl+alt", "c", "ctrl+foo", "ctrl+a+b"])
def test_invalid_hotkeys(raw):
    with pytest.raises(ValueError):
        keys.parse(raw)


def test_qt_roundtrip():
    for combo in ("<ctrl>+<alt>+c", "<ctrl>+<shift>+<f8>", "<alt>+<cmd>+<page_up>"):
        assert keys.from_qt(keys.to_qt(combo)) == combo


def test_gnome_binding():
    from correcteur.platform.autostart import gnome_binding

    assert gnome_binding("<ctrl>+<alt>+c") == "<Control><Alt>c"
    assert gnome_binding("<ctrl>+<shift>+<f8>") == "<Control><Shift>F8"
    assert gnome_binding("<cmd>+<space>") == "<Super>space"


def test_desktop_entry_and_launch_command():
    from correcteur.platform.autostart import desktop_entry, launch_command

    cmd = launch_command("selection")
    assert cmd[-1] == "selection"
    entry = desktop_entry(cmd, autostart=True)
    assert entry.startswith("[Desktop Entry]") and "X-GNOME-Autostart-enabled=true" in entry


@pytest.mark.skipif(sys.platform == "win32", reason="fichier .desktop : Linux")
def test_autostart_linux(home, monkeypatch):
    from correcteur.platform import autostart

    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    assert not autostart.autostart_enabled()
    autostart.set_autostart(True)
    assert autostart.autostart_enabled()
    assert "--demarrage" in (home / "xdg" / "autostart" / "correcteur.desktop").read_text()
    autostart.set_autostart(False)
    assert not autostart.autostart_enabled()


def test_ipc_roundtrip(home):
    from correcteur.platform.ipc import IpcServer, send_command

    received = []
    event = threading.Event()

    def handler(cmd, data):
        received.append((cmd, data))
        event.set()

    assert send_command("ouvrir") is False  # personne n'écoute encore
    server = IpcServer(handler)
    assert server.start()
    try:
        # Une deuxième instance voit la première et ne démarre pas.
        assert IpcServer(lambda c, d: None).start() is False
        assert send_command("verifier-selection", "abc")
        assert event.wait(3)
        assert received == [("verifier-selection", "abc")]
    finally:
        server.stop()
    time.sleep(0.2)
    assert send_command("ouvrir", timeout=1) is False


def test_extract_rejects_path_traversal(tmp_path):
    from correcteur.installer import _extract

    archive = tmp_path / "mechant.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evasion.txt", "non")
    with pytest.raises(RuntimeError):
        _extract(archive, tmp_path / "cible")
    assert not (tmp_path / "evasion.txt").exists()


def test_extract_flattens_single_root(tmp_path):
    from correcteur.installer import _extract

    archive = tmp_path / "lt.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("LanguageTool-9.9/languagetool-server.jar", "jar")
        zf.writestr("LanguageTool-9.9/libs/a.jar", "a")
    _extract(archive, tmp_path / "LT")
    assert (tmp_path / "LT" / "languagetool-server.jar").read_text() == "jar"


def test_extract_member_prefix(tmp_path):
    from correcteur.installer import _extract

    archive = tmp_path / "g.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("grammalecte/__init__.py", "")
        zf.writestr("setup.py", "")
    _extract(archive, tmp_path / "g", member_prefix="grammalecte/")
    assert (tmp_path / "g" / "grammalecte" / "__init__.py").exists()
    assert not (tmp_path / "g" / "setup.py").exists()


def test_download_checks_sha256(tmp_path, monkeypatch):
    import httpx

    from correcteur import installer

    def fake_stream(method, url, **kwargs):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"contenu")))
        return client.stream(method, url)

    monkeypatch.setattr(httpx, "stream", fake_stream)
    ok = installer.download("https://exemple.org/f", tmp_path / "f", sha256="")
    assert ok.read_bytes() == b"contenu"
    with pytest.raises(RuntimeError, match="SHA-256"):
        installer.download("https://exemple.org/f", tmp_path / "g", sha256="0" * 64)
    assert not (tmp_path / "g").exists()
