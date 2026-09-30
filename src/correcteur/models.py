"""Modèle de données partagé par les moteurs, le vérificateur et l'interface."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from enum import Enum

_uids = itertools.count(1)


class Category(str, Enum):
    SPELLING = "orthographe"
    GRAMMAR = "grammaire"
    TYPOGRAPHY = "typographie"
    STYLE = "style"

    @property
    def label(self) -> str:
        return {
            Category.SPELLING: "Orthographe",
            Category.GRAMMAR: "Grammaire",
            Category.TYPOGRAPHY: "Ponctuation",
            Category.STYLE: "Style",
        }[self]


# Ordre de priorité des moteurs : le premier l'emporte pour le message affiché
# quand plusieurs moteurs signalent la même faute.
ENGINE_PRIORITY = {"grammalecte": 0, "languagetool": 1}

ENGINE_LABELS = {"grammalecte": "Grammalecte", "languagetool": "LanguageTool"}


@dataclass
class Issue:
    """Une faute détectée dans le texte.

    `start` et `end` sont des indices Python (points de code) dans le texte
    complet vérifié. `replacements` est trié du plus probable au moins probable.
    """

    start: int
    end: int
    message: str
    replacements: list[str]
    category: Category
    rule_id: str
    source: str
    url: str = ""
    sources: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()  # règles à couper pour ne plus signaler cette faute (tous moteurs)
    agreed: bool = False      # plusieurs moteurs proposent la même correction
    confident: bool = False   # correction sûre, appliquée par la correction express
    # Identifiant stable : conservé quand la faute est décalée après une modification du texte.
    uid: int = field(default_factory=lambda: next(_uids), compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.sources:
            self.sources = (self.source,)
        if not self.rules:
            self.rules = (self.rule_id,)

    @property
    def length(self) -> int:
        return self.end - self.start

    def original(self, text: str) -> str:
        return text[self.start : self.end]

    def shifted(self, delta: int) -> "Issue":
        return replace(self, start=self.start + delta, end=self.end + delta)

    def overlaps(self, other: "Issue") -> bool:
        if self.start == self.end or other.start == other.end:
            return self.start <= other.end and other.start <= self.end
        return self.start < other.end and other.start < self.end

    def to_dict(self, text: str | None = None) -> dict:
        data = {
            "debut": self.start,
            "fin": self.end,
            "message": self.message,
            "suggestions": list(self.replacements),
            "categorie": self.category.value,
            "regle": self.rule_id,
            "sources": list(self.sources),
            "sur": self.confident,
        }
        if text is not None:
            data["texte"] = self.original(text)
        if self.url:
            data["url"] = self.url
        return data


@dataclass
class EngineStatus:
    name: str
    ok: bool
    detail: str = ""
    duration_ms: float = 0.0


@dataclass
class CheckResult:
    text: str
    issues: list[Issue] = field(default_factory=list)
    statuses: dict[str, EngineStatus] = field(default_factory=dict)
    pending: tuple[str, ...] = ()

    @property
    def done(self) -> bool:
        return not self.pending
