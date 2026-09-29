"""Contrastes WCAG des deux thèmes, pour qu'un changement de couleur ne casse pas la lisibilité."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("PySide6")

from correcteur.models import Category  # noqa: E402
from correcteur.ui.theme import DARK, LIGHT, Theme, stylesheet  # noqa: E402

TEXT_MIN = 4.5       # texte (WCAG 1.4.3)
NON_TEXT_MIN = 3.0   # contours, soulignements, indicateur de focus (WCAG 1.4.11)
SELECTION_ALPHA = 55  # fond teinté de la faute sélectionnée dans l'éditeur (editor.py)

THEMES = [pytest.param(LIGHT, id="clair"), pytest.param(DARK, id="sombre")]
CATEGORIES = ("spelling", "grammar", "typography", "style")


def rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def luminance(color: str) -> float:
    channels = []
    for value in rgb(color):
        c = value / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def blend(color: str, background: str, alpha: int) -> str:
    a = alpha / 255
    mixed = (round(c * a + d * (1 - a)) for c, d in zip(rgb(color), rgb(background)))
    return "#" + "".join(f"{v:02x}" for v in mixed)


def failures(theme: Theme, pairs: list[tuple[str, str]], minimum: float) -> list[str]:
    bad = []
    for fg, bg in pairs:
        ratio = contrast(getattr(theme, fg), getattr(theme, bg))
        if ratio < minimum:
            bad.append(f"{fg} sur {bg} : {ratio:.2f}:1 (minimum {minimum}:1)")
    return bad


def test_contrast_reference_values():
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#667085", "#eef0f4") == pytest.approx(4.36, abs=0.01)  # valeur relevée par l'audit


@pytest.mark.parametrize("theme", THEMES)
def test_text_contrast(theme):
    pairs = [(fg, bg) for fg in ("text", "muted") for bg in ("bg", "surface", "surface2")]
    # Libellés de catégorie des cartes, posés sur la carte.
    pairs += [(cat, "surface") for cat in CATEGORIES]
    # Triangle d'état (en-tête) et ligne d'avertissement, sur le fond de la fenêtre.
    pairs += [("warn", "bg"), ("warn", "surface")]
    # Boutons principaux, première suggestion, texte sélectionné.
    pairs += [("accent_text", "accent")]
    assert failures(theme, pairs, TEXT_MIN) == []


@pytest.mark.parametrize("theme", THEMES)
def test_non_text_contrast(theme):
    # Contours des cartes, de l'éditeur et des boutons, posés sur le fond ou dans une carte.
    pairs = [("outline", bg) for bg in ("bg", "surface", "surface2")]
    # Anneau de focus, carte sélectionnée, bordure de l'éditeur actif.
    pairs += [("accent", bg) for bg in ("bg", "surface", "surface2")]
    # Anneau de focus des boutons remplis d'encre.
    pairs += [("outline", "accent")]
    # Soulignements ondulés dans l'éditeur.
    pairs += [(cat, "surface") for cat in CATEGORIES]
    # Pastille « moteur OK » de l'en-tête.
    pairs += [("ok", "bg")]
    assert failures(theme, pairs, NON_TEXT_MIN) == []


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("category", list(Category))
def test_selected_issue_keeps_text_readable(theme, category):
    tint = blend(theme.category_color(category), theme.surface, SELECTION_ALPHA)
    assert contrast(theme.text, tint) >= TEXT_MIN


@pytest.mark.parametrize("theme", THEMES)
def test_accent_is_neutral(theme):
    # Les actions ne doivent pas se confondre avec une catégorie (le bleu reste à la grammaire).
    r, g, b = rgb(theme.accent)
    assert max(r, g, b) - min(r, g, b) <= 25
    assert theme.accent.lower() not in {getattr(theme, cat).lower() for cat in CATEGORIES}


def rules(css: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        for selector in selectors.split(","):
            found[selector.strip()] = found.get(selector.strip(), "") + body
    return found


@pytest.mark.parametrize("theme", THEMES)
def test_stylesheet_uses_accessible_tokens(theme):
    css = rules(stylesheet(theme))
    for selector in ("QPlainTextEdit#editor", "QFrame#card", "QPushButton", "QToolButton"):
        assert f"border: 1px solid {theme.outline}" in css[selector], selector
    # Focus visible sur tous les boutons, y compris les boutons-liens sans bordure.
    for selector in ("QPushButton:focus", "QPushButton#link:focus", "QToolButton:focus"):
        assert f"border: 2px solid {theme.accent}" in css[selector], selector
    for selector in ("QPushButton#primary:focus", 'QPushButton#suggestion[first="true"]:focus'):
        assert f"border: 2px solid {theme.outline}" in css[selector], selector
    assert f"color: {theme.muted}" in css["QLabel#tag"]
