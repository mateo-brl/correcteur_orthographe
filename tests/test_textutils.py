import pytest

from correcteur.textutils import (
    adapt_to_user_style,
    apply_edits,
    chunk_text,
    diff_edits,
    levenshtein,
    match_case,
    narrow_edit,
    only_typographic_difference,
    same_letters,
    split_paragraphs,
    utf16_len,
    utf16_to_index,
)


@pytest.mark.parametrize(
    "original, corrected, expected",
    [
        ("Bonjour Marie", "Bonjour, Marie", [("Bonjour", "Bonjour,")]),
        ("peut être", "peut-être", [("peut être", "peut-être")]),
        ("mes amis , il fesait", "mes amis, il faisait", [("amis ", "amis"), ("fesait", "faisait")]),
        ("le le chat", "le chat", [("le ", "")]),
        ("Ces un beau jour.", "C'est un beau jour.", [("Ces", "C'est")]),
        ("les enfant jouait", "les enfants jouaient", [("enfant", "enfants"), ("jouait", "jouaient")]),
        ("Salut", "Salut.", [("Salut", "Salut.")]),
        ("identique", "identique", []),
    ],
)
def test_diff_edits(original, corrected, expected):
    edits = diff_edits(original, corrected)
    assert [(original[s:e], r) for s, e, r in edits] == expected
    assert apply_edits(original, edits) == corrected


def test_utf16_offsets_with_emoji():
    text = "a😀b c"
    to_index = utf16_to_index(text)
    assert utf16_len(text) == 6
    # "b" est à l'offset UTF-16 3 mais à l'indice Python 2.
    assert to_index(3) == 2
    assert to_index(6) == len(text)
    assert text[to_index(5)] == "c"


def test_utf16_identity_fast_path():
    text = "Élève à l'école"
    to_index = utf16_to_index(text)
    assert all(to_index(i) == i for i in range(len(text) + 1))


def test_split_paragraphs_offsets_and_crlf():
    text = "un\r\ndeux\n\ntrois"
    parts = list(split_paragraphs(text))
    assert parts == [(0, "un"), (4, "deux"), (9, ""), (10, "trois")]
    for offset, para in parts:
        assert text[offset:offset + len(para)] == para


def test_chunk_text_respects_limit_and_covers_everything():
    text = ("Une phrase assez longue. " * 30 + "\n") * 5
    chunks = list(chunk_text(text, 400))
    assert "".join(c for _, c in chunks) == text
    assert all(len(c) <= 400 for _, c in chunks)
    for offset, chunk in chunks:
        assert text[offset:offset + len(chunk)] == chunk


def test_apply_edits_skips_overlaps():
    text = "abcdef"
    assert apply_edits(text, [(0, 3, "X"), (2, 4, "Y"), (4, 6, "Z")]) == "XdZ"


def test_narrow_edit():
    text = "les enfant jouent"
    assert narrow_edit(text, 0, 10, ["les enfants"]) == (4, 10, ["enfants"])
    # Suggestions incompatibles : pas de réduction possible.
    assert narrow_edit(text, 0, 10, ["l'enfant", "les enfants"]) == (0, 10, ["l'enfant", "les enfants"])


def test_typographic_equivalence():
    assert only_typographic_difference("l'", "l’")
    assert only_typographic_difference(" !", "\u00a0!")
    assert only_typographic_difference("...", "…")
    assert only_typographic_difference(" - ", " \u2013 ")
    assert only_typographic_difference('"', "«\u00a0")
    assert not only_typographic_difference(" ,", ",")
    assert not only_typographic_difference("a", "à")


def test_adapt_to_user_style():
    assert adapt_to_user_style("C’est", "Je dis que c'est") == "C'est"
    assert adapt_to_user_style("C’est", "Je dis que c’est") == "C’est"
    assert adapt_to_user_style("C’est", "Ces un film.") == "C'est"  # aucune apostrophe : celle du clavier
    assert adapt_to_user_style("10\u00a0h", "à 10h") == "10 h"


def test_word_helpers():
    assert same_letters("ecole", "école")
    assert not same_letters("école", "école")
    assert not same_letters("fesait", "faisait")
    assert match_case("Ecole", "école") == "École"
    assert levenshtein("fesait", "faisait") == 2
    assert levenshtein("abc", "xyzuvw", limit=2) == 3
