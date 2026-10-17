"""Intégration au système : raccourcis globaux, sélection, presse-papiers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading


def session_type() -> str:
    """"windows", "x11", "wayland" ou "autre"."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux") or "bsd" in sys.platform:
        kind = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if kind == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
            return "wayland"
        if kind == "x11" or os.environ.get("DISPLAY"):
            return "x11"
    return "autre"


def desktop_name() -> str:
    return (os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "").lower()


def which(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


# Posées par PyInstaller pour les plugins Qt du paquet : utiles au correcteur, mais un programme Qt du système
# lancé par lui chargerait ces plugins au lieu des siens.
_PACKAGE_QT_VARIABLES = ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH")


def child_env() -> dict[str, str]:
    """Environnement des programmes du système lancés par le correcteur (copie de os.environ)."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        for name in _PACKAGE_QT_VARIABLES:
            env.pop(name, None)
    return env


def open_url(url: str) -> bool:
    """Ouvre une adresse (page web, ou dossier en file://) avec le programme du système, sans attendre. False
    si c'est impossible.

    Dans le paquet Linux, QDesktopServices lancerait xdg-open, puis le navigateur ou le gestionnaire de
    fichiers, avec les chemins Qt du paquet (voir child_env) : on lance xdg-open nous-mêmes."""
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        tool = which("xdg-open")
        if tool:
            try:
                proc = subprocess.Popen([tool, url], env=child_env(), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                pass
            else:
                # Récupère son code de sortie quand il a fini : pas de processus zombie.
                threading.Thread(target=proc.wait, name="xdg-open", daemon=True).start()
                return True
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    return bool(QDesktopServices.openUrl(QUrl(url)))
