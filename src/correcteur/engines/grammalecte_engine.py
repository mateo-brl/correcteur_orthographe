"""Moteur Grammalecte : correcteur grammatical français, 100 % hors ligne.

Grammalecte est chargé une seule fois et gardé en mémoire ; chaque paragraphe
vérifié est mis en cache, si bien qu'une revérification après une correction
ne réanalyse que le paragraphe modifié."""

from __future__ import annotations

import os
import sys
import threading
from collections import OrderedDict
from pathlib import Path

from correcteur.config import Settings
from correcteur.engines.base import Engine, EngineError
from correcteur.models import Category, Issue
from correcteur.textutils import split_paragraphs

# Options Grammalecte désactivées en mode typographique "standard" : ce sont
# les remarques qui n'ont pas de sens pour un texte tapé au clavier (mails,
# messages, formulaires...).
_STANDARD_OFF = {"apos": False, "nbsp": False, "unit": False, "num": False, "eepi": False}

# Règles typographiques ignorées en mode "standard" (préfixes d'identifiant).
_STANDARD_RULE_PREFIXES = (
    "apostrophe_typographique",
    "typo_guillemets_typographiques",
    "typo_points_suspension",
    "typo_tiret_incise",
    "typo_tiret_d",
    "typo_tiret_dans_dialogue",
    "typo_ordinaux_chiffres_exposants",
    "typo_ordinaux_chiffres_romains_exposants",
    "typo_signe_moins",
    "typo_signe_multiplication",
    "typo_math",
    "nbsp_",
)

_CATEGORY_BY_TYPE = {
    **dict.fromkeys(
        ["typo", "apos", "esp", "tab", "nbsp", "unit", "num", "virg", "poncfin", "nf", "liga", "mapos", "chim",
         "ocr", "eepi"],
        Category.TYPOGRAPHY,
    ),
    **dict.fromkeys(["maj", "minis", "tu", "mc"], Category.SPELLING),
    **dict.fromkeys(["conf", "loc", "gn", "infi", "conj", "ppas", "imp", "inte", "vmode", "date"], Category.GRAMMAR),
    **dict.fromkeys(["bs", "pleo", "eleu", "neg", "redon1", "redon2"], Category.STYLE),
}

_import_lock = threading.Lock()


def _candidate_paths() -> list[Path]:
    paths = []
    env = os.environ.get("CORRECTEUR_GRAMMALECTE")
    if env:
        paths.append(Path(env))
    try:
        from correcteur.installer import grammalecte_dir

        paths.append(grammalecte_dir())
    except Exception:
        pass
    # Dossier vendor/ à la racine du dépôt (développement) ou à côté de l'exécutable.
    here = Path(__file__).resolve()
    paths.append(here.parents[3] / "vendor")
    paths.append(Path(sys.executable).resolve().parent / "vendor")
    return paths


def import_grammalecte():
    """Importe le module grammalecte, en l'ajoutant au chemin si besoin."""
    with _import_lock:
        try:
            import grammalecte  # type: ignore

            return grammalecte
        except ImportError:
            pass
        for base in _candidate_paths():
            if (base / "grammalecte" / "__init__.py").is_file():
                sys.path.insert(0, str(base))
                try:
                    import grammalecte  # type: ignore

                    return grammalecte
                except ImportError:
                    sys.path.remove(str(base))
        return None


class GrammalecteEngine(Engine):
    name = "grammalecte"
    label = "Grammalecte"
    live = True

    def __init__(self, settings: Settings, cache_size: int = 4096) -> None:
        super().__init__(settings)
        self._lock = threading.Lock()
        self._checker = None
        self._load_error = ""
        self._options_cache: dict[bool, dict] = {}
        self._cache: OrderedDict[tuple[str, bool], list[tuple]] = OrderedDict()
        self._cache_size = cache_size

    def is_enabled(self) -> bool:
        return self.settings.grammalecte.enabled

    def supports(self, language: str) -> bool:
        return language.lower().startswith("fr") or language == "auto"

    def available(self) -> tuple[bool, str]:
        if self._checker is not None:
            return True, ""
        if import_grammalecte() is None:
            return False, "Grammalecte n'est pas installé (Paramètres > Moteurs > Installer, ou `correcteur installer grammalecte`)."
        return True, ""

    def _ensure_loaded(self):
        if self._checker is not None:
            return self._checker
        module = import_grammalecte()
        if module is None:
            raise EngineError("Grammalecte n'est pas installé.")
        try:
            self._checker = module.GrammarChecker("fr")
        except Exception as exc:  # pragma: no cover - dépend de l'installation
            self._load_error = str(exc)
            raise EngineError(f"Impossible de charger Grammalecte : {exc}") from exc
        return self._checker

    def warmup(self) -> None:
        with self._lock:
            checker = self._ensure_loaded()
            # La première analyse compile les règles (plusieurs secondes) : on la fait tout de suite.
            checker.getParagraphErrors("Les enfant joue dans le jardin, il fesait beau.", self._options(False), False, True)

    def _options(self, strict: bool) -> dict:
        opts = self._options_cache.get(strict)
        if opts is None:
            gce = self._ensure_loaded().getGCEngine()
            opts = gce.getDefaultOptions() if hasattr(gce, "getDefaultOptions") else gce.getOptions()
            opts = dict(opts)
            if not strict:
                opts.update(_STANDARD_OFF)
            self._options_cache[strict] = opts
        return opts

    def _check_paragraph(self, paragraph: str, strict: bool) -> list[tuple]:
        key = (paragraph, strict)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        checker = self._ensure_loaded()
        gram, spell = checker.getParagraphErrors(paragraph, self._options(strict), False, True)
        found: list[tuple] = []
        for err in gram:
            rule = err.get("sRuleId", "")
            if not strict and rule.startswith(_STANDARD_RULE_PREFIXES):
                continue
            found.append((
                err["nStart"], err["nEnd"], err.get("sMessage", ""), tuple(err.get("aSuggestions") or ()),
                _CATEGORY_BY_TYPE.get(err.get("sType", ""), Category.GRAMMAR), rule, err.get("URL", ""),
            ))
        for err in spell:
            word = err.get("sValue", "")
            found.append((
                err["nStart"], err["nEnd"], f"Mot inconnu du dictionnaire : « {word} ».",
                tuple(err.get("aSuggestions") or ()), Category.SPELLING, "grammalecte_orthographe", "",
            ))
        self._cache[key] = found
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return found

    def check(self, text: str, language: str) -> list[Issue]:
        strict = self.settings.strict_typography
        issues: list[Issue] = []
        with self._lock:
            for offset, paragraph in split_paragraphs(text):
                if not paragraph.strip():
                    continue
                for start, end, message, suggestions, category, rule, url in self._check_paragraph(paragraph, strict):
                    issues.append(Issue(
                        start=offset + start, end=offset + end, message=message,
                        replacements=list(suggestions), category=category, rule_id=rule,
                        source=self.name, url=url,
                    ))
        return issues
