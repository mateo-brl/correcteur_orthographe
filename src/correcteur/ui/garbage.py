"""Ramasse-miettes de Python lancé dans le fil de l'interface uniquement."""

from __future__ import annotations

import gc

from PySide6.QtCore import QObject, QTimer


class MainThreadGarbageCollector(QObject):
    """Le ramasse-miettes automatique de Python se déclenche dans le fil qui alloue de la mémoire à ce moment-là,
    souvent un fil de vérification. Il y détruisait des objets Qt devenus inaccessibles (ancien menu de l'icône,
    fenêtre fermée), et Qt plante quand ses objets sont détruits hors du fil de l'interface (violation d'accès
    sous Windows). On le coupe, et on le lance nous-mêmes dans ce fil, aux mêmes seuils."""

    def __init__(self, parent: QObject | None = None, interval_ms: int = 500) -> None:
        super().__init__(parent)
        self.thresholds = gc.get_threshold()
        gc.disable()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.collect)
        self._timer.start(interval_ms)

    def collect(self) -> None:
        """Comme le ramasse-miettes automatique : la génération 0 quand elle dépasse son seuil, et les plus
        anciennes quand elles dépassent aussi le leur."""
        counts, limits = gc.get_count(), self.thresholds
        if counts[0] <= limits[0]:
            return
        generation = 0
        while (generation + 1 < min(len(counts), len(limits)) and limits[generation + 1]
               and counts[generation + 1] > limits[generation + 1]):
            generation += 1
        gc.collect(generation)

    def stop(self) -> None:
        self._timer.stop()
        gc.enable()
