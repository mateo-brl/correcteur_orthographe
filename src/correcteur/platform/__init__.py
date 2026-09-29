"""Intégration au système : raccourcis globaux, sélection, presse-papiers."""

from __future__ import annotations

import os
import shutil
import sys


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
