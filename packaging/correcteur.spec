# -*- mode: python ; coding: utf-8 -*-
# Construction : pyinstaller packaging/correcteur.spec  (depuis la racine du dépôt)
# Prérequis : Grammalecte dans vendor/ (python -m correcteur installer grammalecte --dossier vendor)

import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "vendor"))

if not (ROOT / "vendor" / "grammalecte" / "__init__.py").exists():
    raise SystemExit("Grammalecte absent de vendor/ : python -m correcteur installer grammalecte --dossier vendor")

hidden = collect_submodules("correcteur")
hidden += collect_submodules("grammalecte", filter=lambda name: "test" not in name and "bottle" not in name)
# pynput choisit son module X11 au lancement. Sans écran, l'import de pynput échoue pendant le build et
# collect_submodules("pynput") ne trouve rien : ces modules sont donc nommés un par un.
PYNPUT_X11 = ["pynput.keyboard._xorg", "pynput.mouse._xorg", "pynput._util.xorg", "pynput._util.xorg_keysyms"]
if sys.platform.startswith("linux"):
    hidden += PYNPUT_X11 + collect_submodules("Xlib")

datas = collect_data_files("grammalecte", excludes=["**/gc_test.txt", "**/perf.txt"])
datas += [(str(ROOT / "src" / "correcteur" / "resources"), "correcteur/resources")]

icon = ROOT / "packaging" / ("icone.ico" if sys.platform == "win32" else "icone.png")

a = Analysis(
    [str(ROOT / "packaging" / "lancer.py")],
    pathex=[str(ROOT / "src"), str(ROOT / "vendor")],
    hiddenimports=hidden,
    datas=datas,
    excludes=["tkinter", "unittest", "pydoc_data", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtOpenGL", "PySide6.QtPdf", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtMultimedia"],
    noarchive=False,
)

if sys.platform.startswith("linux"):
    missing = set(PYNPUT_X11) - {entry[0] for entry in a.pure}
    if missing:
        raise SystemExit(f"Modules absents du paquet (raccourcis X11 inutilisables) : {sorted(missing)}")

# Le thème GTK de Qt n'est pas utilisé (style Fusion) : on évite d'embarquer GTK (~15 Mo).
_unused = ("libqgtk3", "libgtk-3", "libgdk-3", "libatk-1.0", "libatk-bridge-2.0")
a.binaries = [b for b in a.binaries if not any(part in b[0] for part in _unused)]

# Le crochet d'exécution de PyInstaller pour PySide6 importe QtCore à chaque lancement (0,1 s), pour un qt.conf que
# PySide6 crée déjà lui-même : la ligne de commande de chaque raccourci sous Wayland le payait sans jamais ouvrir de
# fenêtre. lancer.py pose ses chemins (plugins de Qt). Sous Windows, où il règle aussi la recherche des DLL de Qt et
# où chaque raccourci passe par RegisterHotKey, il reste.
if sys.platform.startswith("linux"):
    a.scripts = [script for script in a.scripts if script[0] != "pyi_rth_pyside6"]

pyz = PYZ(a.pure)

common = dict(debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
              icon=str(icon) if icon.exists() else None)
# Mode UTF-8 de Python : fichiers, entrée et sortie standard en UTF-8 même sous Windows.
options = [("X utf8_mode=1", None, "OPTION")]
if sys.platform == "win32":
    # Windows : une application sans console + un outil en ligne de commande.
    exes = [
        EXE(pyz, a.scripts, options, exclude_binaries=True, name="Correcteur", console=False, **common),
        EXE(pyz, a.scripts, options, exclude_binaries=True, name="correcteur-cli", console=True, **common),
    ]
else:
    exes = [EXE(pyz, a.scripts, options, exclude_binaries=True, name="correcteur", console=True, **common)]

coll = COLLECT(*exes, a.binaries, a.datas, strip=False, upx=False, name="Correcteur")

# Licence GPL et adresse des sources, à côté de l'exécutable (obligatoire pour diffuser Grammalecte).
for name in ("LICENSE", "packaging/SOURCES.txt"):
    shutil.copy(ROOT / name, Path(DISTPATH) / "Correcteur")
