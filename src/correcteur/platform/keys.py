"""Raccourcis clavier : format interne "<ctrl>+<alt>+c" (celui de pynput),
conversion vers/depuis l'affichage "Ctrl+Alt+C" et les séquences Qt."""

from __future__ import annotations

from dataclasses import dataclass

MODIFIERS = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Maj", "cmd": "Win"}
_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "meta": "cmd", "super": "cmd", "win": "cmd", "windows": "cmd",
    "maj": "shift", "altgr": "alt_gr", "espace": "space", "entrée": "enter", "entree": "enter", "return": "enter",
}
_NAMED = {
    "space": "Espace", "enter": "Entrée", "tab": "Tab", "esc": "Échap", "backspace": "Retour",
    "insert": "Inser", "delete": "Suppr", "home": "Début", "end": "Fin", "page_up": "PgPréc", "page_down": "PgSuiv",
    "pause": "Pause", "print_screen": "ImprÉcran", "scroll_lock": "ArrêtDéfil", "menu": "Menu",
    "up": "Haut", "down": "Bas", "left": "Gauche", "right": "Droite",
    **{f"f{i}": f"F{i}" for i in range(1, 25)},
}


@dataclass(frozen=True)
class Hotkey:
    modifiers: frozenset[str]
    key: str  # caractère unique en minuscule, ou nom ("f5", "space"...)

    def to_pynput(self) -> str:
        parts = [f"<{m}>" for m in ("ctrl", "alt", "shift", "cmd") if m in self.modifiers]
        parts.append(self.key if len(self.key) == 1 else f"<{self.key}>")
        return "+".join(parts)

    def display(self) -> str:
        parts = [MODIFIERS[m] for m in ("ctrl", "alt", "shift", "cmd") if m in self.modifiers]
        parts.append(self.key.upper() if len(self.key) == 1 else _NAMED.get(self.key, self.key.capitalize()))
        return "+".join(parts)


def parse(combo: str) -> Hotkey:
    """Accepte "<ctrl>+<alt>+c", "Ctrl+Alt+C", "ctrl+maj+f8"..."""
    if not combo or not combo.strip():
        raise ValueError("Raccourci vide")
    raw = combo.strip()
    tokens = []
    # "+" peut être la touche elle-même ("ctrl++").
    if raw.endswith("++"):
        tokens = [t for t in raw[:-2].split("+") if t] + ["+"]
    else:
        tokens = [t for t in raw.split("+") if t]
    mods: set[str] = set()
    key = None
    for token in tokens:
        name = token.strip().strip("<>").lower()
        name = _ALIASES.get(name, name)
        for suffix in ("_l", "_r"):
            if name.endswith(suffix) and name[:-2] in MODIFIERS:
                name = name[:-2]
        if name in MODIFIERS:
            mods.add(name)
        elif key is None:
            key = name if len(name) > 1 else token.strip().lower()
        else:
            raise ValueError(f"Raccourci invalide : {combo}")
    if key is None:
        raise ValueError(f"Il manque une touche (pas seulement Ctrl/Alt/Maj) : {combo}")
    if len(key) > 1 and key not in _NAMED:
        raise ValueError(f"Touche inconnue : {key}")
    if not mods and not (len(key) > 1 and key.startswith("f")):
        raise ValueError("Ajoutez au moins Ctrl, Alt ou Win pour ne pas gêner la saisie.")
    return Hotkey(frozenset(mods), key)


def normalize(combo: str) -> str:
    return parse(combo).to_pynput()


def display(combo: str) -> str:
    try:
        return parse(combo).display()
    except ValueError:
        return combo


def from_qt(sequence: str) -> str:
    """Convertit une QKeySequence en texte portable ("Ctrl+Alt+C") vers le format interne."""
    return normalize(sequence.replace("Meta", "Win").replace("PgUp", "page_up").replace("PgDown", "page_down")
                     .replace("Return", "enter").replace("Del", "delete").replace("Ins", "insert"))


def to_qt(combo: str) -> str:
    hk = parse(combo)
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Meta"}
    parts = [names[m] for m in ("ctrl", "alt", "shift", "cmd") if m in hk.modifiers]
    qt_keys = {"page_up": "PgUp", "page_down": "PgDown", "enter": "Return", "delete": "Del", "insert": "Ins",
               "esc": "Esc", "print_screen": "Print", "scroll_lock": "ScrollLock"}
    key = hk.key.upper() if len(hk.key) == 1 else qt_keys.get(hk.key, hk.key.capitalize())
    parts.append(key)
    return "+".join(parts)
