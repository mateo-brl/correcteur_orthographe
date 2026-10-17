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
    ("J'aime ce voiture rouge.", "J'aime cette voiture rouge."),
    # Une correction sûre à un endroit ne l'est pas ailleurs pour le même mot (« Il ce lave » : ce -> se).
    ("J'aime ce voiture rouge. Il ce lave les mains.", "J'aime cette voiture rouge. Il ce lave les mains."),
    ("Les manteau sont rangés. Il a pris sont manteau.", "Les manteaux sont rangés. Il a pris sont manteau."),
    # Familles de règles ajoutées après mesure sur corpus (rapport de test de la 0.1.5 : couverture trop faible).
    ("Elle à mangé.", "Elle a mangé."),
    ("Tu à raison.", "Tu as raison."),
    ("Ont va au cinéma.", "On va au cinéma."),
    ("Il peu le faire.", "Il peut le faire."),
    ("Je leurs parle.", "Je leur parle."),
    ("Il faut mangé.", "Il faut manger."),
    ("Sa va ?", "Ça va ?"),
    ("Quand à lui, il reste.", "Quant à lui, il reste."),
    ("Il viendra peut être demain.", "Il viendra peut-être demain."),
    ("Il pense a toi.", "Il pense à toi."),
    ("Merci a vous.", "Merci à vous."),
    ("Il a appris a nager.", "Il a appris à nager."),
]

UNTOUCHED = [
    # Grammalecte croit "sont" verbe et voudrait "sont manteaux" : sans confirmation, on n'y touche pas.
    "Il a pris sont manteau.",
    # Corrections de Grammalecte qui tombent sur le mauvais mot (rapport de test, 0.1.2) : la faute
    # est « son » -> « sont », « on » -> « ont », « c'est » -> « s'est », ou la phrase est juste.
    "Mes parents son partis en vacances.",
    "Les enfants on faim.",
    "Les filles son belles.",
    "Ses parents son très gentils.",
    "Nous nous sommes téléphoné.",
    # Autre lecture possible, que Grammalecte seul ne peut pas exclure (rapport de test de la 0.1.5).
    "Il y a manger et manger.",           # « à manger »
    "Son seul plaisir est manger.",
    "Les Misérables est un roman de Hugo.",
    "Les Échos a publié un article.",
    "Rendez-vous place du 8 Mai 1945.",
    "Ma mère et ma sœur sont partit.",    # « parties » : le masculin par défaut serait faux
    "Helene est arrivée.",                 # un nom propre n'est pas accentué d'office
    "I have à cat.",                       # pas du français
    "Il peut être fatigué.",
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
