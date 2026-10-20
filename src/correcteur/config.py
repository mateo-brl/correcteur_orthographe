"""Paramètres persistants (JSON) et emplacements des fichiers de l'application."""

from __future__ import annotations

import codecs
import dataclasses
import functools
import json
import logging
import os
import stat
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from correcteur import APP_NAME

log = logging.getLogger(__name__)


PORTABLE_MARKER = "portable.txt"
PORTABLE_DIR = "Données"


@functools.lru_cache(maxsize=1)
def portable_dir() -> Path | None:
    """Version portable (fichier portable.txt à côté de l'exécutable) : dossier « Données » à côté de lui, s'il
    est accessible en écriture. Sinon (zip décompressé dans Program Files, clé en lecture seule) : None."""
    if not getattr(sys, "frozen", False):
        return None
    folder = Path(sys.executable).resolve().parent
    if not (folder / PORTABLE_MARKER).is_file():
        return None
    target = folder / PORTABLE_DIR
    try:
        target.mkdir(exist_ok=True)
        probe = target / ".ecriture"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return None
    return target


def _base_dirs() -> tuple[Path, Path]:
    """(dossier de configuration, dossier de données).

    CORRECTEUR_HOME permet un mode isolé (tests, ou version portable choisie à la main) ; la version portable
    range tout dans son propre dossier (voir portable_dir)."""
    home = os.environ.get("CORRECTEUR_HOME")
    if home:
        base = Path(home)
        return base / "config", base / "data"
    portable = portable_dir()
    if portable is not None:
        return portable, portable
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


def log_path() -> Path:
    """Journal de l'application (voir correcteur.ui.app.install_log)."""
    return data_dir() / "correcteur.log"


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
    auto_correct: bool = True       # appliquer les corrections sûres à l'ouverture de la fenêtre
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
    # Description lisible des règles désactivées (identifiant -> « message : « exemple » »), notée au moment où
    # l'utilisateur les coupe : les Paramètres ne montrent plus « g2__pleo_locutions__b7_a1_1 ».
    rule_labels: dict[str, str] = field(default_factory=dict)

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
        elif isinstance(current, dict):
            if isinstance(value, dict):
                kwargs[f.name] = {str(k): str(v) for k, v in value.items()}
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
        self._unreadable = False
        self.reload()

    @staticmethod
    def _decode(line: bytes) -> str:
        """UTF-8, sinon Windows-1252 (fichier modifié avec un ancien éditeur) : aucun mot n'est perdu, et
        l'enregistrement suivant réécrit le fichier en UTF-8."""
        try:
            return line.decode("utf-8")
        except UnicodeDecodeError:
            return line.decode("cp1252", errors="replace")

    @classmethod
    def _lines(cls, data: bytes) -> list[str]:
        """Lignes du fichier. UTF-16 s'il commence par sa marque d'ordre des octets (« Unicode » du Bloc-notes
        de Windows) : lu octet par octet comme de l'UTF-8, il donnait des mots coupés de caractères nuls, que
        l'enregistrement suivant écrivait tels quels. Sinon UTF-8, ligne par ligne (voir _decode)."""
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return data.decode("utf-16", errors="replace").splitlines()
        return [cls._decode(line) for line in data.removeprefix(codecs.BOM_UTF8).splitlines()]

    def reload(self) -> None:
        words: set[str] = set()
        data, unreadable = b"", False
        try:
            data = self.path.read_bytes()
            self._protect()
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Illisible (droits, dossier à la place du fichier...) : dictionnaire vide pour cette session,
            # et on ne l'écrase pas en enregistrant.
            log.warning("Dictionnaire personnel illisible (%s) : %s", self.path, exc)
            unreadable = True
        self._unreadable = unreadable
        for line in self._lines(data):
            word = line.strip()
            if word and not word.startswith("#"):
                words.add(word)
        with self._lock:
            self._words = words
            self._folded = {w for w in words if w == w.lower()}

    def _protect(self) -> None:
        """Mots personnels (noms, adresses...) : un dictionnaire créé lisible par le groupe ou les autres (664 avec
        la 0.1.6) est remis en 600 dès le chargement, sans attendre le prochain mot ajouté. Seulement s'il
        appartient à l'utilisateur."""
        if os.name != "posix":
            return
        try:
            info = os.stat(self.path)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077:
                os.chmod(self.path, 0o600)
        except OSError as exc:
            log.debug("Droits du dictionnaire inchangés : %s", exc)

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
        if self._unreadable:
            log.warning("Dictionnaire personnel illisible : modification gardée pour cette session seulement.")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, "\n".join(self.words()) + "\n", private=True)  # mots personnels : noms, adresses...
