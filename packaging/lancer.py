"""Point d'entrée des exécutables PyInstaller."""

import os
import sys

if sys.platform.startswith("linux"):
    # Le lanceur de PyInstaller place les bibliothèques du paquet en tête de LD_LIBRARY_PATH (l'original est
    # dans LD_LIBRARY_PATH_ORIG). Les programmes du système lancés ensuite (gsettings, wl-copy, ydotool, java,
    # le navigateur) chargeraient alors notre vieille libglib au lieu de la leur, et gsettings échoue sous
    # Debian 13. On leur rend l'environnement d'origine ; ce processus garde ses bibliothèques, car le chargeur
    # dynamique a lu LD_LIBRARY_PATH à son démarrage.
    original = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        os.environ["LD_LIBRARY_PATH"] = original
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)

from correcteur.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
