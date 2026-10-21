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
    # Une correction sûre à un endroit ne l'est pas ailleurs pour le même mot : « ce » -> « cette » d'un
    # côté, « Il ce lave » -> « se » de l'autre (règle du correcteur, engines/regles.py).
    ("J'aime ce voiture rouge. Il ce lave les mains.", "J'aime cette voiture rouge. Il se lave les mains."),
    ("Les manteau sont rangés. Il a pris sont manteau.", "Les manteaux sont rangés. Il a pris son manteau."),
    # Familles de règles ajoutées après mesure sur corpus (rapport de test de la 0.1.5 : couverture trop faible).
    ("Elle à mangé.", "Elle a mangé."),
    ("Tu à raison.", "Tu as raison."),
    ("Ont va au cinéma.", "On va au cinéma."),
    ("Il peu le faire.", "Il peut le faire."),
    ("Je leurs parle.", "Je leur parle."),
    ("Il faut mangé.", "Il faut manger."),
    ("Sa va ?", "Ça va ?"),
    # Espace après une virgule devant une élision : la suggestion « j » précédée d'une espace ne coupe aucun mot.
    ("Oui,j'arrive tout de suite.", "Oui, j'arrive tout de suite."),
    ("Quand à lui, il reste.", "Quant à lui, il reste."),
    ("Il viendra peut être demain.", "Il viendra peut-être demain."),
    ("Il pense a toi.", "Il pense à toi."),
    ("Merci a vous.", "Merci à vous."),
    ("Je vais acheté du pain.", "Je vais acheter du pain."),
    ("Il est allé acheté le journal.", "Il est allé acheter le journal."),
    ("Il ma donné un livre.", "Il m'a donné un livre."),
    ("Tu ma manqué.", "Tu m'as manqué."),
    ("Il a appris a nager.", "Il a appris à nager."),
    # Rapport de test de la 0.1.6 : fautes signalées mais pas corrigées d'office.
    ("Elle est partit très tôt ce matin.", "Elle est partie très tôt ce matin."),
    ("Je n'ai pas put venir.", "Je n'ai pas pu venir."),
    ("Il c'est trompé de route.", "Il s'est trompé de route."),
    ("Nous avont fini le travail.", "Nous avons fini le travail."),
    # Rapport de test de la 0.1.8.
    ("Je suis sur que tu vas réussir.", "Je suis sûr que tu vas réussir."),
    ("C'est la maison dont je t'ai parler.", "C'est la maison dont je t'ai parlé."),
    # Rapport de test de la 0.1.9.
    ("Je ne sais pas si il viendra.", "Je ne sais pas s'il viendra."),
    ("Si il pleut, on reste.", "S'il pleut, on reste."),
    ("Je vous est envoyé le dossier.", "Je vous ai envoyé le dossier."),
    ("Quelque soit le temps, nous partirons.", "Quel que soit le temps, nous partirons."),
    ("Quelque soit la météo, nous partirons.", "Quelle que soit la météo, nous partirons."),
    ("C'est eux qui on gagné.", "C'est eux qui ont gagné."),
    ("Vous avez finis vos devoirs ?", "Vous avez fini vos devoirs ?"),
    # Règles du correcteur (engines/regles.py) : Grammalecte corrigeait ici le mauvais mot (« son parti »,
    # « on faims », « levés ») ou proposait une forme fautive (« fais »).
    ("Il a pris sont manteau.", "Il a pris son manteau."),
    ("Mes parents son partis en vacances.", "Mes parents sont partis en vacances."),
    ("Les enfants on faim.", "Les enfants ont faim."),
    ("Les filles son belles.", "Les filles sont belles."),
    ("Ses parents son très gentils.", "Ses parents sont très gentils."),
    ("Il faut que je fait mes courses.", "Il faut que je fasse mes courses."),
    ("Il ces levé tôt ce matin.", "Il s'est levé tôt ce matin."),
    # Grammalecte proposait « S'étais » : c'est « C'était » (règle du correcteur).
    ("C'étais lui le plus jeune.", "C'était lui le plus jeune."),
    # Rapport de test de la 0.1.15 : « a » après le verbe d'un sujet nominal (après « on » -> « ont » pour le
    # premier), auxiliaire de « je » derrière la négation, participe de « se tromper de ».
    ("Mes cousins on une maison a la campagne depuis 21 ans.", "Mes cousins ont une maison à la campagne depuis 21 ans."),
    ("Les promeneurs ont soif a la fontaine.", "Les promeneurs ont soif à la fontaine."),
    ("Je n'est pas fini mon travail.", "Je n'ai pas fini mon travail."),
    ("Je n'est pas allé à l'école.", "Je ne suis pas allé à l'école."),
    ("Ils se sont trompé de chemin hier soir.", "Ils se sont trompés de chemin hier soir."),
]

UNTOUCHED = [
    # Phrase juste : « se » complément indirect, pas d'accord.
    "Nous nous sommes téléphoné.",
    # Autre lecture possible, que Grammalecte seul ne peut pas exclure (rapport de test de la 0.1.5).
    "Il y a manger et manger.",           # « à manger »
    "Son seul plaisir est manger.",
    "Les Misérables est un roman de Hugo.",
    "Je vais fatigué au travail.",
    "Ils vont pressés au marché.",
    "Voici ma donnée la plus fiable.",
    "Ils nous avont dit.",                 # « nous » complément : « ont », pas « avons »
    "Elle n'a pas sont mot de passe.",     # « son », pas « été »
    "Ils se sont téléphoné hier soir.",    # « se » complément indirect : pas d'accord (test de la 0.1.7)
    "Patrick a l'air sombre.",
    "Je suis sur la route.",
    "Il est sur le point de partir.",
    "Nous les avons finis hier.",          # complément placé avant : l'accord est juste
    "La galette que nous avons mangée.",
    "Les Échos a publié un article.",
    "Rendez-vous place du 8 Mai 1945.",
    "Ma mère et ma sœur sont partit.",    # « parties » : le masculin par défaut serait faux
    "Helene est arrivée.",                 # un nom propre n'est pas accentué d'office
    "I have à cat.",                       # pas du français
    "Il peut être fatigué.",
    "Les enfants sont partis en vacances avec leurs parents.",
    "Elle a mangé les pommes qu'elle avait achetées.",
    "stp envoie-moi le rdv par mail, merci !",
    # « a » verbe d'un autre sujet que celui du verbe d'avant (rapport de test de la 0.1.15).
    "Le chien qui mange un os a la queue coupée.",
    "Le livre que tu lis le soir a la couverture rouge.",
    # « ai » ou « suis » ? Grammalecte propose les deux : rien d'office.
    "Je n'est pas content.",
    "J'est fatigué.",
    "J'est passé te voir.",
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
