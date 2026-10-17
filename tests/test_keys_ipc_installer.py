from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

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
        second = IpcServer(lambda c, d: None)
        assert second.start() is False and second.other_instance
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


@pytest.mark.parametrize("frozen, workers", [(False, 0), (True, 1)], ids=["sources", "paquet"])
def test_grammalecte_precompiled_without_subprocesses_in_package(tmp_path, monkeypatch, frozen, workers):
    """Dans le paquet PyInstaller, compileall en parallèle relançait l'exécutable avec des arguments de Python
    (« invalid choice: 'from multiprocessing.forkserver import main…' »)."""
    import compileall

    from correcteur import installer

    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(installer, "download", lambda url, dest, *args, **kwargs: dest)
    monkeypatch.setattr(installer, "_extract", lambda archive, target, **kwargs: target.mkdir(parents=True))
    calls = []
    monkeypatch.setattr(compileall, "compile_dir", lambda path, **kwargs: calls.append(kwargs))
    installer.install_grammalecte(target=tmp_path / "grammalecte")
    assert [c["workers"] for c in calls] == [workers]


def test_package_launcher_calls_freeze_support_first():
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "packaging" / "lancer.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    statements = [ast.unparse(node) for node in tree.body
                  if not isinstance(node, ast.Import)
                  and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    # Première instruction exécutée, avant le bloc LD_LIBRARY_PATH et l'import du correcteur.
    assert statements[0] == "multiprocessing.freeze_support()"
    assert any(s.startswith("if sys.platform.startswith('linux')") for s in statements)
    assert statements.index("from correcteur.cli import main") > 0


@pytest.mark.skipif(sys.platform == "win32", reason="socket Unix")
def test_ipc_silent_client_does_not_block_commands(home, monkeypatch):
    """Un client qui se connecte sans rien envoyer bloquait la poignée de main, donc toutes les commandes
    suivantes : la ligne de commande lançait alors une deuxième instance."""
    import socket

    from correcteur.platform import ipc

    monkeypatch.setattr(ipc, "CLIENT_TIMEOUT", 0.5)
    received, done = [], threading.Event()
    server = ipc.IpcServer(lambda cmd, data: (received.append(cmd), done.set()))
    assert server.start(), server.error
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        silent.connect(ipc.address()[0])
        start = time.monotonic()
        assert ipc.send_command("ouvrir", timeout=2.0)
        assert time.monotonic() - start < 1.0
        assert done.wait(3)
        # Passé le délai, le client muet est déconnecté.
        silent.settimeout(5)
        while silent.recv(1024):
            pass
    finally:
        silent.close()
        server.stop()
    assert received == ["ouvrir"]


@pytest.mark.skipif(sys.platform == "win32", reason="socket Unix")
def test_ipc_wrong_key_is_refused(home):
    from multiprocessing.connection import Client

    from correcteur.platform import ipc

    received, done = [], threading.Event()
    server = ipc.IpcServer(lambda cmd, data: (received.append(cmd), done.set()))
    assert server.start()
    try:
        with pytest.raises(Exception):
            with Client(ipc.address()[0], family="AF_UNIX", authkey=b"mauvaise cle") as conn:
                conn.send({"commande": "quitter"})
        assert ipc.send_command("ouvrir")
        assert done.wait(3)
    finally:
        server.stop()
    assert received == ["ouvrir"]


def test_ipc_start_reports_listen_failure(home, monkeypatch):
    """Écoute impossible : start() renvoie False avec la raison (avant : True, l'application tournait sans
    canal, « correcteur quitter » échouait et chaque raccourci lançait une nouvelle instance)."""
    from correcteur.platform import ipc

    def refuse(*args, **kwargs):
        raise OSError("AF_UNIX path too long")

    monkeypatch.setattr(ipc, "Listener", refuse)
    server = ipc.IpcServer(lambda cmd, data: None)
    assert server.start() is False
    assert "AF_UNIX path too long" in server.error and not server.other_instance


@pytest.mark.skipif(sys.platform == "win32", reason="socket Unix")
def test_ipc_long_runtime_dir_falls_back_to_short_path(home, tmp_path, monkeypatch):
    from correcteur.platform import ipc

    long_dir = tmp_path / ("r" * 60) / ("u" * 60)
    long_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(long_dir))
    path, family = ipc.address()
    assert len(os.fsencode(path)) <= 107 and not path.startswith(str(long_dir))
    received, done = [], threading.Event()
    server = ipc.IpcServer(lambda cmd, data: (received.append(cmd), done.set()))
    assert server.start(), server.error
    try:
        assert ipc.send_command("parametres")
        assert done.wait(3)
    finally:
        server.stop()
    assert received == ["parametres"]


@pytest.mark.skipif(sys.platform == "win32", reason="socket Unix")
def test_ipc_address_never_fails_and_avoids_foreign_folders(home, request, monkeypatch):
    """/tmp/correcteur-<uid> déjà pris (fichier : FileExistsError, plantage de toute la ligne de commande ; ou
    dossier d'un autre utilisateur, qui pourrait y placer une fausse socket) : un autre dossier est pris."""
    import tempfile

    from correcteur.platform import ipc

    shared = Path(tempfile.mkdtemp(prefix="ipc"))  # chemin court : une socket Unix fait 107 octets au plus
    request.addfinalizer(lambda: shutil.rmtree(shared, ignore_errors=True))
    monkeypatch.setattr(ipc, "_temp_dirs", lambda: [shared])
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    uid = os.getuid()
    (shared / f"correcteur-{uid}").write_text("pas un dossier")
    path, family = ipc.address()
    assert family == "AF_UNIX" and Path(path).parent == shared / f"correcteur-{uid}-1"
    assert (os.stat(Path(path).parent).st_mode & 0o777) == 0o700

    # Dossier ouvert à tous : corrigé s'il est à l'utilisateur.
    os.chmod(shared / f"correcteur-{uid}-1", 0o777)
    assert ipc.address() == (path, family)
    assert (os.stat(Path(path).parent).st_mode & 0o777) == 0o700

    # Dossiers créés avant lui par un autre utilisateur (propriétaire simulé) : ni corrigés ni utilisés.
    shutil.rmtree(shared / f"correcteur-{uid}-1")
    (shared / f"correcteur-{uid}").unlink()
    foreign = {shared / f"correcteur-{uid}", shared / f"correcteur-{uid}-1"}
    for folder in foreign:
        folder.mkdir()
        os.chmod(folder, 0o777)
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=uid + 1) if Path(path) in foreign else info

    monkeypatch.setattr(ipc.os, "lstat", lstat)
    path, _ = ipc.address()
    assert Path(path).parent == shared / f"correcteur-{uid}-2"
    assert all((os.stat(folder).st_mode & 0o777) == 0o777 for folder in foreign)
    # Lien symbolique, même vers un dossier valable : refusé.
    (shared / "lien").symlink_to(shared / f"correcteur-{uid}-1")
    assert not ipc._private_dir(shared / "lien", create=False)


@pytest.mark.skipif(sys.platform == "win32", reason="socket Unix")
def test_ipc_address_never_raises(home, monkeypatch):
    """La ligne de commande appelle address() sans filet (send_command) : jamais d'exception."""
    from correcteur.config import config_dir
    from correcteur.platform import ipc

    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(ipc, "_temp_dirs", lambda: [Path("/nulle/part")])
    path, family = ipc.address()  # aucun dossier possible : repli sur le dossier des paramètres
    assert family == "AF_UNIX" and Path(path).parent == config_dir()
    assert ipc.send_command("ouvrir", timeout=0.5) is False

    def broken(*args, **kwargs):
        raise OSError("disque en lecture seule")

    monkeypatch.setenv("CORRECTEUR_HOME", "/nulle/part/profil")
    monkeypatch.setattr(ipc.os, "mkdir", broken)
    assert ipc.address()[1] == "AF_UNIX"
    assert ipc.send_command("ouvrir", timeout=0.5) is False
    monkeypatch.setattr(ipc, "_profile_tag", lambda: (_ for _ in ()).throw(RuntimeError("inattendu")))
    assert ipc.address()[1] == "AF_UNIX"


@pytest.mark.skipif(sys.platform == "win32", reason="socket Unix")
def test_ipc_runtime_dir_open_to_others_is_not_used(home, tmp_path, monkeypatch):
    from correcteur.platform import ipc

    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o755)
    os.chmod(runtime, 0o755)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(ipc, "_temp_dirs", lambda: [tmp_path])
    path, _ = ipc.address()
    assert not path.startswith(str(runtime)) and (os.stat(runtime).st_mode & 0o777) == 0o755
    os.chmod(runtime, 0o700)
    assert ipc.address()[0].startswith(str(runtime))
