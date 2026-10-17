from __future__ import annotations

import os
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


@pytest.mark.skipif(sys.platform == "win32", reason="fichier .desktop : Linux")
def test_autostart_command_keeps_settings_in_sync(home, monkeypatch):
    """« correcteur demarrage oui » (lancé par l'installateur) : le réglage enregistré suit le disque."""
    from correcteur.cli import main
    from correcteur.config import SettingsStore

    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    assert main(["demarrage", "oui"]) == 0
    assert SettingsStore().load().general.autostart
    assert "Icon=correcteur\n" in (home / "xdg" / "autostart" / "correcteur.desktop").read_text()
    assert main(["demarrage", "non"]) == 0
    assert not SettingsStore().load().general.autostart


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


# Faux gsettings et dconf : une base de réglages dans un fichier JSON (FAUX_DCONF).
_FAKE_GSETTINGS = r'''#!{python}
import json, os, sys
path = os.environ["FAUX_DCONF"]
state = json.load(open(path))
memory = bool(os.environ.get("FAUX_GSETTINGS_MEMOIRE"))
if memory:  # comme gsettings quand il n'atteint pas dconf : base vide en mémoire, rien n'est gardé
    sys.stderr.write("GLib-GIO-Message: Using the 'memory' GSettings backend.  Your settings will not be saved.\n")
    state = {{}}
cmd, schema, key = sys.argv[1:4]
name = schema + " " + key
if cmd == "get":
    print(state.get(name, "@as []" if key == "custom-keybindings" else "''"))
else:
    state[name] = sys.argv[4] if key == "custom-keybindings" else repr(sys.argv[4])
    if not memory:
        json.dump(state, open(path, "w"))
'''
_FAKE_DCONF = r'''#!{python}
import json, os, sys
state, base = json.load(open(os.environ["FAUX_DCONF"])), sys.argv[2]
print("\n".join(sorted({{k.split(" ")[0].split(":", 1)[1][len(base):] for k in state if ":" + base in k}})))
'''
_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
_LIST = "org.gnome.settings-daemon.plugins.media-keys custom-keybindings"


def _user_shortcuts(listed: bool) -> dict:
    state = {f"org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:{_BASE}{d}/ name": f"'{d}'"
             for d in ("win-e", "terminator")}
    if listed:
        state[_LIST] = str([f"{_BASE}win-e/", f"{_BASE}terminator/"])
    return state


@pytest.fixture
def fake_dconf(tmp_path, monkeypatch):
    import json

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("gsettings", _FAKE_GSETTINGS), ("dconf", _FAKE_DCONF)):
        (bin_dir / name).write_text(script.format(python=sys.executable), encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    db = tmp_path / "dconf.json"
    monkeypatch.setenv("FAUX_DCONF", str(db))

    def setup(state: dict) -> callable:
        db.write_text(json.dumps(state), encoding="utf-8")
        return lambda: json.loads(db.read_text(encoding="utf-8"))

    return setup


@pytest.mark.skipif(sys.platform == "win32", reason="GNOME : Linux")
def test_gnome_shortcuts_keep_the_user_ones(fake_dconf):
    from correcteur.platform.autostart import install_gnome_shortcuts

    read = fake_dconf(_user_shortcuts(listed=True))
    assert "créés" in install_gnome_shortcuts("<ctrl>+<alt>+c", "<ctrl>+<alt>+x")
    install_gnome_shortcuts("<ctrl>+<alt>+c", "<ctrl>+<alt>+x")  # deux fois : pas de doublon
    listed = eval(read()[_LIST])
    assert listed == [f"{_BASE}win-e/", f"{_BASE}terminator/", f"{_BASE}correcteur-verifier/",
                      f"{_BASE}correcteur-express/"]


@pytest.mark.skipif(sys.platform == "win32", reason="GNOME : Linux")
@pytest.mark.parametrize("memory", [True, False], ids=["base en mémoire", "liste lue vide"])
def test_gnome_shortcuts_never_overwrite_an_unread_list(fake_dconf, monkeypatch, memory):
    """gsettings qui n'atteint pas dconf lit une liste vide : la réécrire effacerait les raccourcis de
    l'utilisateur (vu sous Debian 13 avec la 0.1.1). On s'arrête sans rien écrire."""
    from correcteur.platform.autostart import install_gnome_shortcuts

    state = _user_shortcuts(listed=memory)
    read = fake_dconf(state)
    if memory:
        monkeypatch.setenv("FAUX_GSETTINGS_MEMOIRE", "1")
    with pytest.raises(RuntimeError):
        install_gnome_shortcuts("<ctrl>+<alt>+c", "<ctrl>+<alt>+x")
    assert read() == state
