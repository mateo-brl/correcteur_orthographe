"""scripts/installer-linux.sh avec un faux paquet (exécutable `correcteur` simulé), dans un PREFIX temporaire."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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


def run_installer(script: Path, prefix: Path, journal: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP")}
    env.update(PREFIX=str(prefix), SANS_DEMARRAGE="1", FAUX_JOURNAL=str(journal))
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, timeout=60)


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
