"""Paramètres persistants (JSON) et emplacements des fichiers de l'application."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from correcteur import APP_NAME


def _base_dirs() -> tuple[Path, Path]:
    """(dossier de configuration, dossier de données).

    CORRECTEUR_HOME permet un mode portable (ou isolé pour les tests)."""
    home = os.environ.get("CORRECTEUR_HOME")
    if home:
        base = Path(home)
        return base / "config", base / "data"
    from platformdirs import user_config_dir, user_data_dir

    return Path(user_config_dir(APP_NAME, appauthor=False)), Path(user_data_dir(APP_NAME, appauthor=False))


def config_dir() -> Path:
    path = _base_dirs()[0]
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    path = _base_dirs()[1]
    path.mkdir(parents=True, exist_ok=True)
    return path


def engines_dir() -> Path:
    path = data_dir() / "moteurs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_path(name: str) -> Path:
    """Fichier fourni avec le programme (icône...), en source ou empaqueté."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / "correcteur" / "resources" / name
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent / "resources" / name


# Raccourcis au format pynput : <ctrl>, <alt>, <shift>, <cmd> (touche Windows).
DEFAULT_HOTKEY_CHECK = "<ctrl>+<alt>+c"
DEFAULT_HOTKEY_AUTOCORRECT = "<ctrl>+<alt>+x"


@dataclass
class GeneralSettings:
    language: str = "fr"            # "fr", "auto", ou un code LanguageTool ("en-US", "de-DE"...)
    typography: str = "standard"    # "standard" : pas de pinaillage typographique ; "stricte" : tout
    hotkey_check: str = DEFAULT_HOTKEY_CHECK
    hotkey_autocorrect: str = DEFAULT_HOTKEY_AUTOCORRECT
    hotkeys_enabled: bool = True
    live_check: bool = True         # revérifier pendant la saisie dans la fenêtre
    accept_abbreviations: bool = True  # ne pas signaler stp, svp, rdv, mdr...
    autostart: bool = False
    theme: str = "auto"             # "auto", "clair", "sombre"
    first_run_done: bool = False


@dataclass
class GrammalecteSettings:
    enabled: bool = True


@dataclass
class LanguageToolSettings:
    enabled: bool = True
    # "public" : api.languagetool.org (gratuit, aucune charge sur le PC, ~20 requêtes/min)
    # "local" : serveur LanguageTool sur ce PC (Java requis, ~400 Mo de RAM), démarré automatiquement
    # "perso" : serveur LanguageTool à l'adresse `url` (par ex. un autre PC du réseau)
    # "premium" : compte LanguageTool Premium (username + api_key)
    mode: str = "public"
    url: str = "http://localhost:8081"
    local_port: int = 8081
    local_max_ram_mb: int = 512
    username: str = ""
    api_key: str = ""
    picky: bool = True
    mother_tongue: str = "fr"


@dataclass
class Settings:
    general: GeneralSettings = field(default_factory=GeneralSettings)
    grammalecte: GrammalecteSettings = field(default_factory=GrammalecteSettings)
    languagetool: LanguageToolSettings = field(default_factory=LanguageToolSettings)
    ignored_rules: list[str] = field(default_factory=list)

    @property
    def strict_typography(self) -> bool:
        return self.general.typography == "stricte"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        return _from_dict(cls, data)

    def copy(self) -> "Settings":
        return Settings.from_dict(json.loads(json.dumps(self.to_dict())))


def _from_dict(cls, data: Any):
    """Construit une dataclass en ignorant les clés inconnues et en gardant les
    valeurs par défaut pour les clés absentes ou de mauvais type."""
    if not isinstance(data, dict):
        return cls()
    kwargs = {}
    defaults = cls()
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        current = getattr(defaults, f.name)
        if dataclasses.is_dataclass(current):
            kwargs[f.name] = _from_dict(type(current), value)
        elif isinstance(current, bool):
            if isinstance(value, bool):
                kwargs[f.name] = value
        elif isinstance(current, (int, float)) and not isinstance(current, bool):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                kwargs[f.name] = type(current)(value)
        elif isinstance(current, list):
            if isinstance(value, list):
                kwargs[f.name] = [str(v) for v in value]
        elif isinstance(current, str):
            if isinstance(value, str):
                kwargs[f.name] = value
    return cls(**kwargs)


def _atomic_write(path: Path, content: str, private: bool = False) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    if private and os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class SettingsStore:
    """Charge et sauvegarde les paramètres (parametres.json)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "parametres.json"

    def load(self) -> Settings:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return Settings.from_dict(json.load(fh))
        except FileNotFoundError:
            return Settings()
        except (OSError, ValueError):
            # Fichier corrompu : on le garde de côté et on repart des valeurs par défaut.
            try:
                os.replace(self.path, self.path.with_suffix(".json.corrompu"))
            except OSError:
                pass
            return Settings()

    def save(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, json.dumps(settings.to_dict(), ensure_ascii=False, indent=2), private=True)


class PersonalDictionary:
    """Dictionnaire personnel : un mot par ligne dans dictionnaire.txt.

    Un mot enregistré en minuscules accepte toutes les casses ("stp" accepte
    "Stp" et "STP") ; un mot avec majuscules n'accepte que sa forme exacte."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "dictionnaire.txt"
        self._lock = threading.Lock()
        self._words: set[str] = set()
        self._folded: set[str] = set()
        self.reload()

    def reload(self) -> None:
        words: set[str] = set()
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    word = line.strip()
                    if word and not word.startswith("#"):
                        words.add(word)
        except FileNotFoundError:
            pass
        with self._lock:
            self._words = words
            self._folded = {w for w in words if w == w.lower()}

    def __contains__(self, word: str) -> bool:
        word = word.strip().replace("’", "'")
        with self._lock:
            return word in self._words or word.lower() in self._folded

    def words(self) -> list[str]:
        with self._lock:
            return sorted(self._words, key=str.casefold)

    def add(self, word: str) -> None:
        word = word.strip().replace("’", "'")
        if not word:
            return
        with self._lock:
            self._words.add(word)
            if word == word.lower():
                self._folded.add(word)
        self._save()

    def remove(self, word: str) -> None:
        with self._lock:
            self._words.discard(word)
            self._folded.discard(word)
        self._save()

    def set_words(self, words: list[str]) -> None:
        clean = {w.strip().replace("’", "'") for w in words if w.strip()}
        with self._lock:
            self._words = clean
            self._folded = {w for w in clean if w == w.lower()}
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, "\n".join(self.words()) + "\n")
