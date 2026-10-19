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
    # Le style du passage corrigé prime : « c'est » tapé au clavier reste droit dans un texte qui a un « ’ ».
    mixed = "Elle c'est trompée. Aujourd’hui il pleut."
    assert adapt_to_user_style("s’est", mixed, "c'est") == "s'est"
    assert adapt_to_user_style("C'est", "Ses une idée. Aujourd’hui il pleut.", "Ses") == "C’est"


def test_word_helpers():
    assert same_letters("ecole", "école")
    assert not same_letters("école", "école")
    assert not same_letters("fesait", "faisait")
    assert match_case("Ecole", "école") == "École"
    assert levenshtein("fesait", "faisait") == 2
    assert levenshtein("abc", "xyzuvw", limit=2) == 3


def test_chunks_never_split_a_word():
    text = " ".join(["mot"] * 2000)  # ni fin de ligne ni point : on coupe entre deux mots
    chunks = list(chunk_text(text, 1000))
    assert "".join(c for _, c in chunks) == text
    assert all(c.strip(" ").split(" ")[0] == "mot" and c.rstrip(" ").endswith("mot") for _, c in chunks)


def test_subject_agreement_for_nous_and_vous():
    from correcteur.textutils import agree_with_subject, one_edit, subject_ending

    text = "Nous avont fini. Ils nous avont dit. Et vous avé raison."
    assert subject_ending(text, text.index("avont")) == "ons"
    assert subject_ending(text, text.index("avont dit")) is None  # « nous » complément de « Ils »
    assert subject_ending(text, text.index("avé")) == "ez"
    assert agree_with_subject(text, 5, ("amont", "avant", "avons")) == ("avons", "amont", "avant")
    assert agree_with_subject(text, 5, ("avions", "avons")) == ("avions", "avons")  # deux candidats : on n'y touche pas
    assert one_edit("avont", "avons") and one_edit("put", "pu") and one_edit("ecrie", "ecire")
    assert not one_edit("avont", "amont ") and not one_edit("abc", "abc")


def test_describe_changes_counts_spaces():
    from correcteur.textutils import describe_changes

    before, after = "Je vais a la plage , et ecole est fermée.", "Je vais à la plage, et école est fermée."
    assert describe_changes(before, after) == "a → à, ecole → école, 1 espace corrigée"
    assert describe_changes("le  mail", "le mail") == ""  # seules, les espaces n'ont rien à montrer


@pytest.mark.parametrize("before, after, expected", [
    ("Sa fait longtemps qu'on ne c'est pas vu !", "Ça fait longtemps qu'on ne s'est pas vu !", "Sa → Ça, c'est → s'est"),
    ("Que fait t'il ?", "Que fait-il ?", "fait t'il → fait-il"),
    ("Dit moi ce que tu en pense.", "Dis-moi ce que tu en penses.", "Dit moi → Dis-moi, pense → penses"),
    ("c'est-a-dire", "c'est-à-dire", "c'est-a-dire → c'est-à-dire"),
    ("Il est arrivé a l'heure", "Il est arrivé à l'heure", "a → à"),
], ids=["apostrophe", "trait d'union", "impératif", "locution", "mot seul"])
def test_describe_changes_shows_whole_words(before, after, expected):
    """« c → s » et « t' → - » ne disaient rien : le bilan montre les mots entiers."""
    from correcteur.textutils import describe_changes

    assert describe_changes(before, after) == expected


def test_diff_edits_on_a_long_repetitive_text_is_fast_and_exact():
    """300 lignes du même message : 46 s de diff (interface figée pendant la correction express), 0,1 s au plus
    maintenant. Les modifications refont exactement le texte corrigé, même si des lignes changent."""
    import random
    import time

    from correcteur.textutils import apply_edits

    message = ("Bonjour Marie,\nje t'écrit pour te dire que je serai la demain a 8 heures. Les enfants on hâte de te "
               "voir, ils parle de toi tout les jours.\nBisous, Paul")
    fixed = message.replace("je t'écrit", "Je t'écris").replace(" la demain a", " là demain à") \
        .replace(" on hâte", " ont hâte").replace("ils parle", "ils parlent").replace("tout les", "tous les")
    before, after = "\n".join([message] * 100), "\n".join([fixed] * 100)
    start = time.monotonic()
    edits = diff_edits(before, after)
    assert time.monotonic() - start < 2
    assert apply_edits(before, edits) == after and len(edits) == 700  # 7 mots corrigés par message
    random.seed(3)
    words = "le la les de un et à a est sont ces ses c'est s'est mais mes on ont Paul. Marie ! ?".split()
    for _ in range(20):
        lines = [" ".join(random.choice(words) for _ in range(random.randint(0, 30))) for _ in range(150)]
        changed = list(lines)
        for _ in range(10):
            i = random.randrange(len(changed))
            changed[i] = changed[i].replace(" a ", " à ", 1) + random.choice(["", " fin"])
        if random.random() < 0.5:
            changed.insert(random.randrange(len(changed)), "ligne ajoutée")
        a, b = "\n".join(lines), "\n".join(changed)
        if random.random() < 0.3:
            a, b = a.replace("\n", " "), b.replace("\n", " ")  # un seul long paragraphe : découpé en phrases
        assert apply_edits(a, diff_edits(a, b)) == b
