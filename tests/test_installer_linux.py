"""scripts/installer-linux.sh avec un faux paquet (exécutable `correcteur` simulé), dans un PREFIX temporaire."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("bash"),
                                reason="script bash : Linux")

FAKE_EXE = '#!/bin/sh\necho "$*" >> "$FAUX_JOURNAL"\n[ "$1" = quitter ] && exit 1\nexit 0\n'


def make_package(folder: Path, version: str) -> Path:
    (folder / "_internal" / "correcteur" / "resources").mkdir(parents=True)
    (folder / "_internal" / "correcteur" / "resources" / "icone.svg").write_text("<svg/>", encoding="utf-8")
    (folder / "correcteur").write_text(FAKE_EXE, encoding="utf-8")
    (folder / "correcteur").chmod(0o755)
    (folder / "VERSION").write_text(version, encoding="utf-8")
    shutil.copy(ROOT / "scripts" / "installer-linux.sh", folder / "installer-linux.sh")
    return folder / "installer-linux.sh"


def run_installer(script: Path, prefix: Path, journal: Path, *args: str, **extra_env: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "CORRECTEUR_HOME")}
    env.update(PREFIX=str(prefix), SANS_DEMARRAGE="1", FAUX_JOURNAL=str(journal), **extra_env)
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=env, timeout=60)


def test_installer_rerun_from_installed_copy_keeps_installation(tmp_path):
    prefix, journal = tmp_path / "prefix", tmp_path / "journal.txt"
    dest = prefix / "opt" / "correcteur"
    out = run_installer(make_package(tmp_path / "paquet1", "1"), prefix, journal)
    assert out.returncode == 0, out.stderr
    assert (dest / "VERSION").read_text() == "1"
    assert (prefix / "bin" / "correcteur").resolve() == (dest / "correcteur").resolve()
    assert (prefix / "share" / "icons" / "hicolor" / "scalable" / "apps" / "correcteur.svg").exists()

    # Relancé depuis la copie installée : il supprimait son propre dossier (installation détruite).
    (dest / "marqueur").write_text("gardé", encoding="utf-8")
    out = run_installer(dest / "installer-linux.sh", prefix, journal)
    assert out.returncode == 0, out.stderr
    assert (dest / "marqueur").exists() and (dest / "_internal" / "correcteur" / "resources" / "icone.svg").exists()
    assert (prefix / "bin" / "correcteur").resolve().exists()

    # Mise à jour depuis un nouveau paquet : remplacement complet, sans dossier temporaire oublié.
    out = run_installer(make_package(tmp_path / "paquet2", "2"), prefix, journal)
    assert out.returncode == 0, out.stderr
    assert (dest / "VERSION").read_text() == "2" and not (dest / "marqueur").exists()
    assert sorted(p.name for p in (prefix / "opt").iterdir()) == ["correcteur"]
    assert "quitter" in journal.read_text().split("\n")


    # Mise à jour interrompue entre les deux déplacements : l'ancienne copie mise de côté est retirée.
    (prefix / "opt" / "correcteur.ancien").mkdir()
    out = run_installer(make_package(tmp_path / "paquet3", "3"), prefix, journal)
    assert out.returncode == 0, out.stderr
    assert sorted(p.name for p in (prefix / "opt").iterdir()) == ["correcteur"]


# Faux exécutable compilé : /proc/PID/exe désigne alors bien le fichier installé (un script désignerait sh).
# « quitter » échoue, comme avec une instance figée (réussit avec FAUX_QUITTER_OK) ; avec FAUX_ATTENTE, il reste
# lancé quels que soient ses arguments.
_FAKE_C = r"""
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc > 1 && strcmp(argv[1], "quitter") == 0) return getenv("FAUX_QUITTER_OK") ? 0 : 1;
    if (getenv("FAUX_ATTENTE")) for (;;) pause();
    return 0;
}
"""


@pytest.fixture
def compiled_exe(tmp_path):
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        pytest.skip("compilateur C requis")
    (tmp_path / "faux.c").write_text(_FAKE_C, encoding="utf-8")
    subprocess.run([cc, "-o", str(tmp_path / "faux"), str(tmp_path / "faux.c")], check=True)

    def make(folder: Path, version: str) -> Path:
        script = make_package(folder, version)
        shutil.copy(tmp_path / "faux", folder / "correcteur")
        return script

    return make


def test_installer_counts_only_the_application_instance(tmp_path, compiled_exe):
    """Faux avertissement « L'ancienne instance ne s'est pas arrêtée » : les commandes lancées par les raccourcis
    GNOME (« correcteur selection »...) comptaient comme l'application. Et une instance figée, qui ne répond pas
    à « correcteur quitter », était remplacée sans un mot."""
    prefix, journal = tmp_path / "prefix", tmp_path / "journal.txt"
    exe = prefix / "opt" / "correcteur" / "correcteur"
    assert run_installer(compiled_exe(tmp_path / "paquet1", "1"), prefix, journal).returncode == 0
    env = dict(os.environ, FAUX_ATTENTE="1")
    procs = []
    try:
        procs += [subprocess.Popen([str(exe), arg], env=env) for arg in ("selection", "express")]
        time.sleep(0.2)
        out = run_installer(compiled_exe(tmp_path / "paquet2", "2"), prefix, journal, FAUX_QUITTER_OK="1")
        assert out.returncode == 0, out.stderr
        assert "ne s'est pas arrêtée" not in out.stdout and "ne répond pas" not in out.stdout
        for n, args in enumerate(([], ["--demarrage"], ["--debug"])):
            app = subprocess.Popen([str(exe), *args], env=env)
            procs.append(app)
            time.sleep(0.2)
            out = run_installer(compiled_exe(tmp_path / f"paquet-{n}", "3"), prefix, journal)
            assert out.returncode == 0, out.stderr
            assert f"PID {app.pid}" in out.stdout and "ne répond pas à « correcteur quitter »" in out.stdout
            app.kill()
            app.wait()
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()


_FAKE_GSETTINGS = r"""#!{python}
import json, os, sys
path = os.environ["FAUX_DCONF"]
state = json.load(open(path))
if os.environ.get("FAUX_GSETTINGS_MEMOIRE"):  # gsettings sans accès à dconf : base vide, rien n'est gardé
    sys.stderr.write("GLib-GIO-Message: Using the 'memory' GSettings backend.\n")
    state = {{}}
cmd, schema, key = sys.argv[1:4]
name = schema + " " + key
if cmd == "get":
    print(state.get(name, "@as []" if key == "custom-keybindings" else "''"))
    sys.exit(0)
if cmd == "set":
    state[name] = sys.argv[4]
elif cmd == "reset":
    state.pop(name, None)
if not os.environ.get("FAUX_GSETTINGS_MEMOIRE"):
    json.dump(state, open(path, "w"))
"""
_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


@pytest.mark.parametrize("memory", [False, True], ids=["dconf", "gsettings sans dconf"])
def test_uninstaller(tmp_path, memory):
    """installer-linux.sh --desinstaller : retire le programme, ses entrées de menu et de démarrage, son icône
    et ses raccourcis GNOME (pas ceux de l'utilisateur), garde les paramètres et le dictionnaire."""
    prefix, journal = tmp_path / "prefix", tmp_path / "journal.txt"
    assert run_installer(make_package(tmp_path / "paquet", "1"), prefix, journal).returncode == 0
    xdg_config, xdg_data = tmp_path / "config", tmp_path / "data"
    (xdg_config / "autostart").mkdir(parents=True)
    (xdg_config / "autostart" / "correcteur.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    (xdg_config / "autostart" / "autre.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    (xdg_config / "Correcteur").mkdir()
    (xdg_config / "Correcteur" / "dictionnaire.txt").write_text("Zorglub\n", encoding="utf-8")
    (xdg_data / "Correcteur" / "moteurs").mkdir(parents=True)
    (prefix / "bin" / "autre").write_text("#!/bin/sh\n", encoding="utf-8")
    tools = tmp_path / "outils"
    tools.mkdir()
    (tools / "gsettings").write_text(_FAKE_GSETTINGS.format(python=sys.executable), encoding="utf-8")
    (tools / "gsettings").chmod(0o755)
    dconf = tmp_path / "dconf.json"
    paths = [f"{_BASE}win-e/", f"{_BASE}correcteur-verifier/", f"{_BASE}correcteur-express/"]
    state = {f"{_SCHEMA} custom-keybindings": str(paths)}
    for path in paths:
        state[f"{_SCHEMA}.custom-keybinding:{path} binding"] = "'<Control><Alt>c'"
    dconf.write_text(json.dumps(state), encoding="utf-8")
    extra = dict(XDG_CONFIG_HOME=str(xdg_config), XDG_DATA_HOME=str(xdg_data), FAUX_DCONF=str(dconf),
                 PATH=f"{tools}:{os.environ['PATH']}")
    if memory:
        extra["FAUX_GSETTINGS_MEMOIRE"] = "1"

    out = run_installer(prefix / "opt" / "correcteur" / "installer-linux.sh", prefix, journal, "--desinstaller", **extra)
    assert out.returncode == 0, out.stderr
    assert not (prefix / "opt" / "correcteur").exists() and not (prefix / "bin" / "correcteur").exists()
    assert not (prefix / "share" / "applications" / "correcteur.desktop").exists()
    assert not (prefix / "share" / "icons" / "hicolor" / "scalable" / "apps" / "correcteur.svg").exists()
    assert not (xdg_config / "autostart" / "correcteur.desktop").exists()
    assert (xdg_config / "autostart" / "autre.desktop").exists() and (prefix / "bin" / "autre").exists()
    assert (xdg_config / "Correcteur" / "dictionnaire.txt").read_text(encoding="utf-8") == "Zorglub\n"
    assert (xdg_data / "Correcteur" / "moteurs").is_dir()
    assert str(xdg_config / "Correcteur") in out.stdout and str(xdg_data / "Correcteur") in out.stdout
    assert journal.read_text().split("\n").count("quitter") == 1  # instance arrêtée
    after = json.loads(dconf.read_text(encoding="utf-8"))
    if memory:
        # Liste illisible : surtout ne pas la réécrire (les raccourcis de l'utilisateur seraient effacés).
        assert after == state and "non retirés" in out.stdout
    else:
        assert eval(after[f"{_SCHEMA} custom-keybindings"]) == [f"{_BASE}win-e/"]
        assert f"{_SCHEMA}.custom-keybinding:{_BASE}win-e/ binding" in after
        assert not any("correcteur-" in key for key in after)


def test_package_check_runs_the_application_as_x11(monkeypatch):
    """packaging/verifier_paquet_linux.py, lancé depuis une session Wayland : WAYLAND_DISPLAY restait, et
    l'application se croyait sous Wayland malgré XDG_SESSION_TYPE=x11."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("verifier_paquet_linux",
                                                  ROOT / "packaging" / "verifier_paquet_linux.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    env = module.app_environment("/tmp/profil-essai")
    assert "WAYLAND_DISPLAY" not in env
    assert (env["XDG_SESSION_TYPE"], env["QT_QPA_PLATFORM"], env["CORRECTEUR_HOME"]) == ("x11", "xcb", "/tmp/profil-essai")
