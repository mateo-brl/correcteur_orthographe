"""Correction express hors ligne, avec le vrai Grammalecte : ce qu'il corrige seul,
et surtout ce qu'il ne doit jamais toucher."""

from __future__ import annotations

import pytest

from conftest import needs_grammalecte

pytestmark = needs_grammalecte

FIXED = [
    ("Je vais a la plage demain.", "Je vais à la plage demain."),
    ("Ces un très bon film.", "C'est un très bon film."),
    ("Ils on mangé au restaurant.", "Ils ont mangé au restaurant."),
    ("Ils son partis hier soir.", "Ils sont partis hier soir."),
    ("Il faut que tu viens demain.", "Il faut que tu viennes demain."),
    ("Quel belle journée !", "Quelle belle journée !"),
    ("Je suis aller a la réunion hier.", "Je suis allé à la réunion hier."),
    ("Ils sont aller au cinéma.", "Ils sont allés au cinéma."),
    ("Les résultats sont bon.", "Les résultats sont bons."),
    ("Je me suis tromper de numéro.", "Je me suis trompé de numéro."),
    ("Nous avons manger des pomme et on a bien rigoler.", "Nous avons mangé des pommes et on a bien rigolé."),
    ("Il faut que tu viens voir ça, les enfant était ravis !",
     "Il faut que tu viennes voir ça, les enfants étaient ravis !"),
    ("Les filles sont parti tôt car elles étaient fatigué.",
     "Les filles sont parties tôt, car elles étaient fatiguées."),
]

UNTOUCHED = [
    # Grammalecte croit "sont" verbe et voudrait "sont manteaux" : sans confirmation, on n'y touche pas.
    "Il a pris sont manteau.",
    "Les enfants sont partis en vacances avec leurs parents.",
    "Elle a mangé les pommes qu'elle avait achetées.",
    "stp envoie-moi le rdv par mail, merci !",
]


@pytest.fixture(scope="module")
def offline():
    from correcteur.checker import Checker
    from correcteur.config import PersonalDictionary, Settings

    settings = Settings()
    settings.languagetool.enabled = False
    checker = Checker(settings, PersonalDictionary())
    yield checker
    checker.close()


@pytest.mark.parametrize("text, expected", FIXED)
def test_offline_express_fixes(offline, text, expected):
    assert offline.autocorrect(text)[0] == expected


@pytest.mark.parametrize("text", UNTOUCHED)
def test_offline_express_leaves_doubtful_text_alone(offline, text):
    assert offline.autocorrect(text)[0] == text


def test_offline_express_never_picks_a_reading_at_random(offline):
    """"obtenu sont bon" : "son bon" fait disparaître toutes les remarques, "sont bons" aussi.
    Grammalecte seul ne peut pas trancher : "son" ne doit jamais être choisi."""
    assert "son bon" not in offline.autocorrect("Les résultats que j'ai obtenu sont bon.")[0]
