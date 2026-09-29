"""Interface commune des moteurs de correction."""

from __future__ import annotations

from abc import ABC, abstractmethod

from correcteur.config import Settings
from correcteur.models import Issue


class EngineError(Exception):
    """Erreur affichable telle quelle à l'utilisateur."""


class Engine(ABC):
    name: str = ""
    label: str = ""
    # Un moteur "live" est relancé pendant la saisie ; sinon seulement à la demande.
    live: bool = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def configure(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def is_enabled(self) -> bool: ...

    def available(self) -> tuple[bool, str]:
        """(disponible, explication si indisponible)."""
        return True, ""

    def supports(self, language: str) -> bool:
        return True

    def warmup(self) -> None:
        """Préchargement en arrière-plan pour que la première vérification soit instantanée."""

    @abstractmethod
    def check(self, text: str, language: str) -> list[Issue]: ...

    def close(self) -> None:
        pass
