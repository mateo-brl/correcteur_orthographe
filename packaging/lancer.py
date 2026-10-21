"""Point d'entrée des exécutables PyInstaller."""

import multiprocessing
import os
import sys

# Avant tout le reste : un processus auxiliaire de multiprocessing (serveur forkserver, suivi des ressources,
# processus de travail) relance cet exécutable avec des arguments de Python ; freeze_support exécute alors ce
# qu'il demande et termine le processus, au lieu de passer ces arguments à notre ligne de commande.
multiprocessing.freeze_support()

# Chemins de Qt dans le paquet, que posait le crochet de PyInstaller pour PySide6. Ce crochet, retiré (voir
# correcteur.spec), importait aussi Qt à chaque lancement (0,1 s), même pour la ligne de commande qui n'en a pas
# besoin : « correcteur selection » de chaque raccourci sous Wayland, « correcteur corriger ».
if sys.platform == "win32":
    _qt = os.path.join(sys._MEIPASS, "PySide6")
    os.environ["PATH"] = sys._MEIPASS + os.pathsep + os.environ.get("PATH", "")
else:
    _qt = os.path.join(sys._MEIPASS, "PySide6", "Qt")
os.environ["QT_PLUGIN_PATH"] = os.path.join(_qt, "plugins")
os.environ["QML2_IMPORT_PATH"] = os.path.join(_qt, "qml")

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
