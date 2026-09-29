"""Lancement automatique à l'ouverture de session et raccourcis du bureau (GNOME)."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from correcteur import APP_NAME
from correcteur.platform.keys import parse

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_DESKTOP_FILE = "correcteur.desktop"


def launch_command(*args: str) -> list[str]:
    """Commande qui lance le correcteur (exécutable empaqueté ou module Python)."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        if sys.platform == "win32":
            # Toujours l'application sans console, même appelée depuis correcteur-cli.exe.
            gui = exe.with_name("Correcteur.exe")
            if gui.exists():
                exe = gui
        return [str(exe), *args]
    python = sys.executable
    if sys.platform == "win32":
        pythonw = Path(python).with_name("pythonw.exe")
        if pythonw.exists() and not args:
            python = str(pythonw)
    return [python, "-m", "correcteur", *args]


def _command_line(cmd: list[str]) -> str:
    if sys.platform == "win32":
        return subprocess.list2cmdline(cmd)
    return shlex.join(cmd)


def _autostart_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / _DESKTOP_FILE


def autostart_enabled() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, APP_NAME)
                return True
        except OSError:
            return False
    return _autostart_file().exists()


def set_autostart(enabled: bool) -> None:
    cmd = launch_command("--demarrage")
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _command_line(cmd))
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return
    path = _autostart_file()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desktop_entry(cmd, autostart=True), encoding="utf-8")


def desktop_entry(cmd: list[str], autostart: bool = False) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "Comment=Correcteur d'orthographe et de grammaire",
        f"Exec={_command_line(cmd)}",
        "Icon=accessories-text-editor",
        "Terminal=false",
        "Categories=Utility;TextTools;",
        "StartupNotify=false",
    ]
    if autostart:
        lines += ["X-GNOME-Autostart-enabled=true", "X-GNOME-Autostart-Delay=3"]
    return "\n".join(lines) + "\n"


# -- raccourcis GNOME (Wayland) --------------------------------------------

_GNOME_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
_GNOME_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


def gnome_binding(combo: str) -> str:
    hk = parse(combo)
    names = {"ctrl": "<Control>", "alt": "<Alt>", "shift": "<Shift>", "cmd": "<Super>"}
    key = hk.key if len(hk.key) == 1 else {"page_up": "Page_Up", "page_down": "Page_Down", "enter": "Return",
                                            "esc": "Escape", "space": "space"}.get(hk.key, hk.key.upper() if hk.key.startswith("f") else hk.key)
    return "".join(names[m] for m in ("ctrl", "alt", "shift", "cmd") if m in hk.modifiers) + key


def install_gnome_shortcuts(check_combo: str, express_combo: str) -> str:
    """Crée (ou met à jour) deux raccourcis personnalisés GNOME. Renvoie un message."""
    import ast

    def gset(*args: str) -> None:
        subprocess.run(["gsettings", "set", *args], check=True, capture_output=True, timeout=5)

    out = subprocess.run(["gsettings", "get", _GNOME_SCHEMA, "custom-keybindings"],
                         capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    current = [] if out.startswith("@as") else list(ast.literal_eval(out or "[]"))
    entries = [
        ("correcteur-verifier", "Correcteur : vérifier la sélection", "selection", check_combo),
        ("correcteur-express", "Correcteur : correction express", "express", express_combo),
    ]
    for ident, name, arg, combo in entries:
        path = f"{_GNOME_BASE}{ident}/"
        schema = f"{_GNOME_SCHEMA}.custom-keybinding:{path}"
        gset(schema, "name", name)
        gset(schema, "command", _command_line(launch_command(arg)))
        gset(schema, "binding", gnome_binding(combo))
        if path not in current:
            current.append(path)
    gset(_GNOME_SCHEMA, "custom-keybindings", str(current))
    return "Raccourcis GNOME créés (Paramètres > Clavier > Raccourcis personnalisés)."
