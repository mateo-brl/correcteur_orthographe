"""Point d'entrée des exécutables PyInstaller."""

import sys

from correcteur.cli import main

if __name__ == "__main__":
    sys.exit(main())
