"""Vérifie le paquet Linux construit comme le ferait un utilisateur, sur un écran X11 (Xvfb en CI).

L'application empaquetée est lancée, une fenêtre contient un texte sélectionné, on appuie sur
Ctrl+Alt+X et la fenêtre doit contenir le texte corrigé. Avant, on vérifie que les programmes du
système lancés par le paquet n'héritent pas de ses bibliothèques. Ce test attrape ce que les tests
du code source ne voient pas : ce qui tient à l'empaquetage.

    xvfb-run -a python packaging/verifier_paquet_linux.py dist/Correcteur/correcteur
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests" / "helpers" / "target_app.py"
TEXT = "Je vais a la plage , et ecole est fermée."
EXPECTED = "Je vais à la plage, et école est fermée."


def child_environment_is_clean(exe: Path, env: dict[str, str]) -> bool:
    """Les programmes du système lancés par le paquet (ici gsettings, remplacé par un faux qui note son
    environnement) ne doivent pas hériter des bibliothèques du paquet : sous Debian 13, gsettings échouait."""
    tools = Path(tempfile.mkdtemp(prefix="correcteur-outils-"))
    log = tools / "gsettings.log"
    fake = tools / "gsettings"
    fake.write_text('#!/bin/sh\necho "LD_LIBRARY_PATH=${LD_LIBRARY_PATH-}" >> "%s"\n'
                    '[ "$1" = get ] && echo "@as []"\nexit 0\n' % log, encoding="utf-8")
    fake.chmod(0o755)
    out = subprocess.run([str(exe), "raccourcis-gnome"], env=dict(env, PATH=f"{tools}:{env['PATH']}"),
                         capture_output=True, text=True)
    seen = log.read_text(encoding="utf-8") if log.exists() else ""
    print(f"raccourcis-gnome : {out.stdout.strip() or out.stderr.strip()}")
    if out.returncode != 0 or not seen:
        print("gsettings n'a pas été appelé correctement.")
        return False
    if "_internal" in seen:
        print("gsettings a hérité des bibliothèques du paquet :\n" + seen)
        return False
    return True


def main(exe: Path) -> int:
    home = tempfile.mkdtemp(prefix="correcteur-paquet-")
    env = dict(os.environ, CORRECTEUR_HOME=home, XDG_SESSION_TYPE="x11", QT_QPA_PLATFORM="xcb")
    if not child_environment_is_clean(exe, env):
        return 1
    os.environ["CORRECTEUR_HOME"] = home
    sys.path.insert(0, str(ROOT / "src"))
    from correcteur.config import SettingsStore

    # Hors ligne pour un résultat reproductible, et sans la fenêtre de premier lancement.
    store = SettingsStore()
    settings = store.load()
    settings.general.first_run_done = True
    settings.languagetool.enabled = False
    store.save(settings)

    app = subprocess.Popen([str(exe)], env=env)
    try:
        deadline = time.monotonic() + 30
        while "Instance en cours : oui" not in subprocess.run(
                [str(exe), "diagnostic"], env=env, capture_output=True, text=True).stdout:
            if app.poll() is not None or time.monotonic() > deadline:
                print("L'application empaquetée n'a pas démarré.")
                return 1
            time.sleep(0.5)
        print(subprocess.run([str(exe), "diagnostic"], env=env, capture_output=True, text=True).stdout)
        time.sleep(2)  # chargement de Grammalecte

        target = subprocess.Popen([sys.executable, str(TARGET), TEXT, "10000"], env=env,
                                  stdout=subprocess.PIPE, text=True, encoding="utf-8")
        assert target.stdout.readline().strip() == "PRET"
        time.sleep(1)
        from pynput.keyboard import Controller, Key

        from correcteur.platform.bridge import xtest_key

        keyboard, x = Controller(), xtest_key("x")  # vraie frappe : le raccourci global doit la voir
        with keyboard.pressed(Key.ctrl), keyboard.pressed(Key.alt):
            keyboard.press(x)
            keyboard.release(x)
        out = target.communicate(timeout=30)[0]
        final = next(line for line in out.splitlines() if line.startswith("FINAL:"))[len("FINAL:"):]
        print(f"Avant : {TEXT}\nAprès : {final}")
        if final != EXPECTED:
            print(f"Attendu : {EXPECTED}")
            return 1
        print("Paquet Linux : correction express par Ctrl+Alt+X vérifiée.")
        return 0
    finally:
        subprocess.run([str(exe), "quitter"], env=env, capture_output=True)
        try:
            app.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill()


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]).resolve()))
