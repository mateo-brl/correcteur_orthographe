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

        if enabled:
            # La clé "Run" n'existe pas toujours sur un profil Windows neuf : on la crée.
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _command_line(cmd))
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass  # rien à désactiver
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
        "Icon=correcteur",  # installée par scripts/installer-linux.sh, comme pour l'entrée du menu
        "Terminal=false",
        "Categories=Utility;TextTools;",
        "StartupNotify=false",
    ]
    if autostart:
        lines += ["X-GNOME-Autostart-enabled=true", "X-GNOME-Autostart-Delay=3"]
    return "\n".join(lines) + "\n"


_GNOME_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
_GNOME_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"


def gnome_binding(combo: str) -> str:
    hk = parse(combo)
    names = {"ctrl": "<Control>", "alt": "<Alt>", "shift": "<Shift>", "cmd": "<Super>"}
    key = hk.key if len(hk.key) == 1 else {"page_up": "Page_Up", "page_down": "Page_Down", "enter": "Return",
                                            "esc": "Escape", "space": "space"}.get(hk.key, hk.key.upper() if hk.key.startswith("f") else hk.key)
    return "".join(names[m] for m in ("ctrl", "alt", "shift", "cmd") if m in hk.modifiers) + key


# Messages de gsettings quand il n'atteint pas la base dconf de la session : il bascule alors sur une base
# vide en mémoire, lit une liste de raccourcis vide, et l'écriture finale effacerait ceux de l'utilisateur.
_GSETTINGS_UNRELIABLE = ("'memory' GSettings backend", "Failed to load module")


def _gsettings(*args: str) -> str:
    out = subprocess.run(["gsettings", *args], capture_output=True, text=True, timeout=5)
    if out.returncode != 0:
        raise RuntimeError(f"gsettings {args[0]} : {out.stderr.strip() or 'échec'}")
    if any(marker in out.stderr for marker in _GSETTINGS_UNRELIABLE):
        raise RuntimeError("gsettings n'accède pas aux réglages de la session : " + out.stderr.strip().splitlines()[0])
    return out.stdout.strip()


def _read(*args: str):
    """Valeur lue par gsettings (texte GVariant : chaînes et listes de chaînes)."""
    import ast

    out = _gsettings("get", *args)
    return [] if out.startswith("@as") else ast.literal_eval(out)


def _keybinding_list() -> list[str]:
    return list(_read(_GNOME_SCHEMA, "custom-keybindings"))


def _other_keybinding_dirs() -> list[str]:
    """Raccourcis personnalisés présents dans dconf, hors ceux du correcteur ([] sans l'outil dconf)."""
    from correcteur.platform import which

    if not which("dconf"):
        return []
    out = subprocess.run(["dconf", "list", _GNOME_BASE], capture_output=True, text=True, timeout=5)
    return [d for d in out.stdout.split() if d.endswith("/") and not d.startswith("correcteur-")]


def install_gnome_shortcuts(check_combo: str, express_combo: str) -> str:
    """Crée (ou met à jour) deux raccourcis personnalisés GNOME sans toucher aux autres. Renvoie un message.

    La liste des raccourcis est réécrite en entier : si la lecture est douteuse, on s'arrête sans rien
    écrire, et le résultat est relu avant d'annoncer quoi que ce soit."""
    current = _keybinding_list()
    if not current and _other_keybinding_dirs():
        raise RuntimeError("la liste des raccourcis GNOME lue est vide alors que d'autres raccourcis existent : "
                           "rien n'a été modifié.")
    entries = [
        ("correcteur-verifier", "Correcteur : vérifier la sélection", "selection", check_combo),
        ("correcteur-express", "Correcteur : correction express", "express", express_combo),
    ]
    wanted = list(current)
    expected = {}
    for ident, name, arg, combo in entries:
        path = f"{_GNOME_BASE}{ident}/"
        schema = f"{_GNOME_SCHEMA}.custom-keybinding:{path}"
        values = {"name": name, "command": _command_line(launch_command(arg)), "binding": gnome_binding(combo)}
        for key, value in values.items():
            _gsettings("set", schema, key, value)
            expected[(schema, key)] = value
        if path not in wanted:
            wanted.append(path)
    if wanted != current:
        _gsettings("set", _GNOME_SCHEMA, "custom-keybindings", str(wanted))
    if _keybinding_list() != wanted or any(
            _read(schema, key) != value for (schema, key), value in expected.items()):
        raise RuntimeError("les raccourcis GNOME relus ne correspondent pas à ceux enregistrés.")
    return "Raccourcis GNOME créés (Paramètres > Clavier > Raccourcis personnalisés)."
