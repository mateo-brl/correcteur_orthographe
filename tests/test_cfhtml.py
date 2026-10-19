"""Corrections reportées dans le texte mis en forme du presse-papiers Windows (CF_HTML) : Word, navigateurs."""

from __future__ import annotations

import pytest

from correcteur.platform.cfhtml import _parse, correct_cf_html, correct_fragment


def cf_html(document: str) -> bytes:
    """Données CF_HTML d'un document HTML marqué <!--StartFragment--> ... <!--EndFragment-->."""
    header = "Version:0.9\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\nStartFragment:{:010d}\r\nEndFragment:{:010d}\r\n" \
             "SourceURL:https://exemple.fr/doc\r\n"
    size = len(header.format(0, 0, 0, 0))
    body = document.encode("utf-8")
    start = size + body.index(b"<!--StartFragment-->") + len(b"<!--StartFragment-->")
    end = size + body.index(b"<!--EndFragment-->")
    return header.format(size, size + len(body), start, end).encode() + body + b"\0"


def fragment(data: bytes) -> str:
    """Fragment désigné par l'en-tête, après vérification des positions de début et de fin du HTML."""
    fields, _order, header_end, start, end = _parse(data.rstrip(b"\0"))
    assert int(fields["StartHTML"]) == header_end
    assert int(fields["EndHTML"]) == len(data.rstrip(b"\0"))
    assert fields["SourceURL"] == "https://exemple.fr/doc"
    return data[start:end].decode("utf-8")


WORD = ("<html xmlns:o=\"urn:schemas-microsoft-com:office:office\">\r\n<head>\r\n<meta charset=utf-8>\r\n<style>\r\n"
        "<!--\r\n p.MsoNormal {margin:0cm;}\r\n-->\r\n</style>\r\n</head>\r\n<body lang=FR>\r\n<!--StartFragment-->\r\n\r\n"
        "<p class=MsoNormal>Je les ai <b>mit</b> au <a href=\"https://frigo.fr\">frigo</a>&nbsp;! Il ces lev&eacute;\r\n"
        "t&ocirc;t, <i>a</i> 8 heures&nbsp;; ok<o:p></o:p></p>\r\n\r\n"
        "<p class=MsoNormal>Mes parents son partis en vacance.<o:p></o:p></p>\r\n\r\n<!--EndFragment-->\r\n</body>\r\n</html>")


def test_word_formatting_and_nbsp_survive():
    """G3 : gras, italique, lien et espaces insécables avant ! et ; restent en place."""
    before = "Je les ai mit au frigo ! Il ces levé tôt, a 8 heures ; ok\nMes parents son partis en vacance.\n"
    after = "Je les ai mis au frigo ! Il s'est levé tôt, à 8 heures ; ok\nMes parents sont partis en vacances.\n"
    out = correct_cf_html(cf_html(WORD), before, after)
    assert out is not None and out.endswith(b"\0")
    assert fragment(out) == (
        "\r\n\r\n<p class=MsoNormal>Je les ai <b>mis</b> au <a href=\"https://frigo.fr\">frigo</a>&nbsp;! Il s'est "
        "lev&eacute;\r\nt&ocirc;t, <i>à</i> 8 heures&nbsp;; ok<o:p></o:p></p>\r\n\r\n"
        "<p class=MsoNormal>Mes parents sont partis en vacances.<o:p></o:p></p>\r\n\r\n")


def test_browser_fragment():
    page = ("<html><body>\r\n<!--StartFragment--><span style=\"color: rgb(0, 0, 0);\">Sa fait longtemps qu'on ne </span>"
            "<b>c'est</b> pas vu&nbsp;! <a href=\"x\">J'ai acheter</a> des g&acirc;teaux.<!--EndFragment-->\r\n</body>\r\n"
            "</html>")
    out = correct_cf_html(cf_html(page), "Sa fait longtemps qu'on ne c'est pas vu ! J'ai acheter des gâteaux.",
                          "Ça fait longtemps qu'on ne s'est pas vu ! J'ai acheté des gâteaux.")
    assert fragment(out) == ("<span style=\"color: rgb(0, 0, 0);\">Ça fait longtemps qu'on ne </span><b>s'est</b> pas "
                             "vu&nbsp;! <a href=\"x\">J'ai acheté</a> des g&acirc;teaux.")


@pytest.mark.parametrize("html, before, after, expected", [
    # Puce de liste Word, absente du texte brut sous cette forme.
    ("<p><![if !supportLists]><span style='mso-list:Ignore'>·<span>&nbsp;&nbsp;&nbsp; </span></span><![endif]>"
     "Il et sorti<o:p></o:p></p>", "·\tIl et sorti\n", "·\tIl est sorti\n",
     "<p><![if !supportLists]><span style='mso-list:Ignore'>·<span>&nbsp;&nbsp;&nbsp; </span></span><![endif]>"
     "Il est sorti<o:p></o:p></p>"),
    ("<p>Bonjour Marie ça va?</p>", "Bonjour Marie ça va?", "Bonjour, Marie ça va ?", "<p>Bonjour, Marie ça va ?</p>"),
    # Mot coupé par la mise en forme : le mot corrigé prend celle de son début.
    ("<p>Ils <b>mang</b>e des pommes</p>", "Ils mange des pommes", "Ils mangent des pommes",
     "<p>Ils <b>mangent</b> des pommes</p>"),
    ("<p>Tom &amp; Jerry son l&agrave;</p>", "Tom & Jerry son là", "Tom & Jerry sont là",
     "<p>Tom &amp; Jerry sont l&agrave;</p>"),
    ("<p>a &lt; b et c a d</p>", "a < b et c a d", "a < b et c à d", "<p>a &lt; b et c à d</p>"),
    ("<p>x &lt; y</p>", "x < y", "x < y", "<p>x &lt; y</p>"),  # rien à changer
], ids=["puce Word", "ponctuation", "mot coupé", "entités", "caractère à échapper", "inchangé"])
def test_fragment_corrections(html, before, after, expected):
    assert correct_fragment(html, before, after) == expected


@pytest.mark.parametrize("html, before, after", [
    ("<p>a</p><p>b</p>", "a\nb", "a b"),                      # retour à la ligne supprimé
    ("<p>Je vais a la plage</p>", "Autre chose", "Autre choses"),  # texte introuvable
    ("<p>a < b</p>", "a < b", "a < c"),                       # HTML mal formé
], ids=["retour à la ligne", "texte introuvable", "HTML mal formé"])
def test_uncertain_corrections_give_up(html, before, after):
    """Dans le doute, pas de HTML : le texte brut corrigé est collé seul."""
    assert correct_fragment(html, before, after) is None


def test_header_with_short_numbers_and_no_context():
    """En-tête sans zéros de remplissage, StartHTML à -1 : positions recalculées, toujours sur 10 chiffres."""
    body = "<!--StartFragment-->Il et sorti<!--EndFragment-->"
    head = "Version:1.0\r\nStartHTML:-1\r\nEndHTML:-1\r\nStartFragment:{}\r\nEndFragment:{}\r\n"
    start = end = 0
    for _ in range(3):  # la longueur de l'en-tête dépend des nombres qu'il contient
        start = len(head.format(start, end)) + body.index("Il")
        end = start + len("Il et sorti")
    data = head.format(start, end).encode() + body.encode()
    assert data[start:end] == b"Il et sorti"
    out = correct_cf_html(data, "Il et sorti", "Il est sorti")
    fields, _order, _end, s, e = _parse(out.rstrip(b"\0"))
    assert fields["StartHTML"] == "-1" and len(fields["StartFragment"]) == 10
    assert out[s:e] == b"Il est sorti"


def test_long_text_is_fast_enough():
    import time

    line = "Les enfants on hâte de te voir, ils parle de toi tout les jours."
    fixed = "Les enfants ont hâte de te voir, ils parlent de toi tous les jours."
    before, after = "\n".join([line] * 300), "\n".join([fixed] * 300)
    html = "".join(f"<p>{line}</p>" for line in before.split("\n"))
    start = time.monotonic()
    out = correct_fragment(html, before, after)
    assert out == "".join(f"<p>{line}</p>" for line in after.split("\n"))
    assert time.monotonic() - start < 3
