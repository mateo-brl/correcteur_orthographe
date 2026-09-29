"""Téléchargement et installation des moteurs (Grammalecte, LanguageTool)."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

from correcteur.config import engines_dir

Progress = Callable[[int, int], None]  # (octets reçus, total ou 0 si inconnu)

GRAMMALECTE_VERSION = "2.3.0"
GRAMMALECTE_URL = f"https://grammalecte.net/zip/Grammalecte-fr-v{GRAMMALECTE_VERSION}.zip"
GRAMMALECTE_SHA256 = "aaa4219704857778038ecc1db18f6a907994c0125b8e762dcecc69130280684f"

LANGUAGETOOL_VERSION = "6.6"
LANGUAGETOOL_URL = f"https://languagetool.org/download/LanguageTool-{LANGUAGETOOL_VERSION}.zip"
LANGUAGETOOL_SHA256 = "53600506b399bb5ffe1e4c8dec794fd378212f14aaf38ccef9b6f89314d11631"


def grammalecte_dir() -> Path:
    return engines_dir() / f"grammalecte-{GRAMMALECTE_VERSION}"


def languagetool_dir() -> Path:
    return engines_dir() / f"LanguageTool-{LANGUAGETOOL_VERSION}"


def download(url: str, dest: Path, progress: Progress | None = None, sha256: str = "") -> Path:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh, httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            received = 0
            for chunk in resp.iter_bytes(1 << 16):
                fh.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if progress:
                    progress(received, total)
        if sha256 and digest.hexdigest() != sha256:
            raise RuntimeError(f"Empreinte SHA-256 inattendue pour {url} : fichier corrompu ou modifié.")
        os.replace(tmp_name, dest)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return dest


def _extract(zip_path: Path, target: Path, member_prefix: str = "") -> None:
    """Extraction sûre (refuse les chemins qui sortent du dossier cible)."""
    staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=".extract-"))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            root = staging.resolve()
            for info in zf.infolist():
                if member_prefix and not info.filename.startswith(member_prefix):
                    continue
                out = (staging / info.filename).resolve()
                if root not in out.parents and out != root:
                    raise RuntimeError(f"Archive invalide : {info.filename}")
                zf.extract(info, staging)
        if target.exists():
            shutil.rmtree(target)
        entries = list(staging.iterdir())
        # Archive avec un seul dossier racine (cas de LanguageTool) : on le remonte.
        if len(entries) == 1 and entries[0].is_dir() and not member_prefix:
            shutil.move(str(entries[0]), str(target))
        else:
            target.mkdir(parents=True)
            for entry in entries:
                shutil.move(str(entry), str(target / entry.name))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install_grammalecte(progress: Progress | None = None, target: Path | None = None) -> Path:
    """Installe Grammalecte dans le dossier des moteurs (ou dans `target`, par
    exemple vendor/ pour le développement et l'empaquetage)."""
    target = Path(target) if target else grammalecte_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        archive = download(GRAMMALECTE_URL, Path(tmp) / "grammalecte.zip", progress, GRAMMALECTE_SHA256)
        _extract(archive, target, member_prefix="grammalecte/")
    # Précompilation : sinon le premier lancement compile 4 Mo de règles (plusieurs secondes).
    import compileall

    compileall.compile_dir(str(target), quiet=1, workers=0)
    return target


def install_languagetool(progress: Progress | None = None) -> Path:
    target = languagetool_dir()
    with tempfile.TemporaryDirectory(dir=engines_dir()) as tmp:
        archive = download(LANGUAGETOOL_URL, Path(tmp) / "languagetool.zip", progress, LANGUAGETOOL_SHA256)
        _extract(archive, target)
    return target


def find_java() -> str | None:
    """Cherche un Java utilisable (JAVA_HOME, PATH, Java embarqué dans le dossier moteurs)."""
    exe = "java.exe" if sys.platform == "win32" else "java"
    candidates = []
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(Path(java_home) / "bin" / exe)
    for jre in sorted(engines_dir().glob("jre*"), reverse=True):
        candidates.append(jre / "bin" / exe)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("java")


def console_progress(label: str) -> Progress:
    def show(received: int, total: int) -> None:
        if total:
            pct = received * 100 // total
            sys.stderr.write(f"\r{label} : {pct:3d} % ({received // 1_000_000} / {total // 1_000_000} Mo)")
        else:
            sys.stderr.write(f"\r{label} : {received // 1_000_000} Mo")
        sys.stderr.flush()
        if total and received >= total:
            sys.stderr.write("\n")

    return show
