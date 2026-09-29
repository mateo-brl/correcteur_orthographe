"""Outils texte : conversion d'offsets, découpage, diff mot à mot, remplacements."""

from __future__ import annotations

import difflib
import re
from typing import Callable, Iterator, Sequence

# Un mot (lettres/chiffres), une suite d'espaces, ou un signe isolé.
_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)
_WORD_RE = re.compile(r"\w", re.UNICODE)


def utf16_to_index(text: str) -> Callable[[int], int]:
    """Renvoie une fonction qui convertit un offset UTF-16 (LanguageTool, Java)
    en indice Python. Les emojis et autres caractères hors BMP occupent deux
    unités UTF-16 mais un seul caractère Python."""
    if len(text.encode("utf-16-le")) == 2 * len(text):
        return lambda offset: offset
    mapping: list[int] = []
    for i, ch in enumerate(text):
        mapping.append(i)
        if ord(ch) > 0xFFFF:
            mapping.append(i + 1)
    mapping.append(len(text))
    last = len(mapping) - 1
    return lambda offset: mapping[min(max(offset, 0), last)]


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_paragraphs(text: str) -> Iterator[tuple[int, str]]:
    """Découpe en lignes (paragraphes) et renvoie (offset, contenu sans \\r)."""
    pos = 0
    for line in text.split("\n"):
        yield pos, line[:-1] if line.endswith("\r") else line
        pos += len(line) + 1


def chunk_text(text: str, max_len: int) -> Iterator[tuple[int, str]]:
    """Découpe un long texte en morceaux d'au plus `max_len` caractères, sur des
    fins de paragraphe quand c'est possible, sinon sur des fins de phrase."""
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_len, n)
        if end < n:
            cut = text.rfind("\n", start, end)
            if cut <= start:
                cut = max(text.rfind(". ", start, end), text.rfind("! ", start, end), text.rfind("? ", start, end))
                cut = cut + 1 if cut > start else -1
            if cut > start:
                end = cut + 1 if text[cut:cut + 1] == "\n" else cut
        yield start, text[start:end]
        start = end


def apply_edits(text: str, edits: Sequence[tuple[int, int, str]]) -> str:
    """Applique des remplacements (debut, fin, texte). Les remplacements qui se
    chevauchent sont ignorés (le premier dans l'ordre fourni gagne)."""
    accepted: list[tuple[int, int, str]] = []
    for start, end, repl in edits:
        if any(start < e and s < end or (start == end and s < start < e) for s, e, _ in accepted):
            continue
        accepted.append((start, end, repl))
    for start, end, repl in sorted(accepted, key=lambda x: (x[0], x[1]), reverse=True):
        text = text[:start] + repl + text[end:]
    return text


def tokenize(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]


def _is_word(s: str) -> bool:
    return bool(_WORD_RE.search(s))


def diff_edits(original: str, corrected: str) -> list[tuple[int, int, str]]:
    """Compare deux versions d'un texte mot à mot et renvoie les modifications
    sous forme (debut, fin, remplacement) dans les coordonnées de `original`.

    Une modification qui ne touche que de la ponctuation ou des espaces est
    rattachée au mot voisin pour être visible et compréhensible
    ("Bonjour Marie" -> "Bonjour, Marie" donne "Bonjour" -> "Bonjour,")."""
    a_tok = tokenize(original)
    b_tok = tokenize(corrected)
    a_str = [original[s:e] for s, e in a_tok]
    b_str = [corrected[s:e] for s, e in b_tok]
    matcher = difflib.SequenceMatcher(None, a_str, b_str, autojunk=False)

    hunks: list[list[int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        seg = "".join(a_str[i1:i2])
        new = "".join(b_str[j1:j2])
        if not _is_word(seg):
            # Rattacher au mot précédent (même position dans les deux textes).
            if i1 > 0 and j1 > 0 and a_str[i1 - 1] == b_str[j1 - 1] and _is_word(a_str[i1 - 1]):
                i1 -= 1
                j1 -= 1
            elif i2 < len(a_str) and j2 < len(b_str) and a_str[i2] == b_str[j2] and _is_word(a_str[i2]):
                i2 += 1
                j2 += 1
            # "peut être" -> "peut-être" : englober aussi le mot suivant.
            if seg.isspace() and not _is_word(new) and i2 < len(a_str) and j2 < len(b_str) \
                    and a_str[i2] == b_str[j2] and _is_word(a_str[i2]):
                i2 += 1
                j2 += 1
        if hunks and i1 <= hunks[-1][1]:
            hunks[-1][1] = max(hunks[-1][1], i2)
            hunks[-1][3] = max(hunks[-1][3], j2)
        else:
            hunks.append([i1, i2, j1, j2])

    edits = []
    for i1, i2, j1, j2 in hunks:
        a_start = a_tok[i1][0] if i1 < len(a_tok) else len(original)
        a_end = a_tok[i2 - 1][1] if i2 > i1 else a_start
        b_start = b_tok[j1][0] if j1 < len(b_tok) else len(corrected)
        b_end = b_tok[j2 - 1][1] if j2 > j1 else b_start
        edits.append((a_start, a_end, corrected[b_start:b_end]))
    return edits


def narrow_edit(text: str, start: int, end: int, replacements: Sequence[str]) -> tuple[int, int, list[str]]:
    """Réduit une zone signalée à la partie réellement modifiée, mot à mot, par
    toutes les suggestions : "les enfant" -> ["les enfants"] devient
    "enfant" -> ["enfants"]."""
    if not replacements:
        return start, end, list(replacements)
    orig = text[start:end]
    o_tok = [orig[s:e] for s, e in tokenize(orig)]
    r_toks = [[r[s:e] for s, e in tokenize(r)] for r in replacements]
    if not o_tok:
        return start, end, list(replacements)

    def common(seqs: list[list[str]], reverse: bool) -> int:
        k = 0
        while True:
            vals = set()
            for seq in seqs:
                if k >= len(seq):
                    return k
                vals.add(seq[-1 - k] if reverse else seq[k])
            if len(vals) != 1:
                return k
            k += 1

    seqs = [o_tok] + r_toks
    pre = common(seqs, reverse=False)
    # Ne pas tout consommer : il doit rester au moins un token de l'original.
    pre = min(pre, len(o_tok) - 1, *(len(r) for r in r_toks))
    trimmed = [s[pre:] for s in seqs]
    suf = common(trimmed, reverse=True)
    suf = min(suf, len(trimmed[0]) - 1, *(len(r) for r in trimmed[1:]))
    if pre == 0 and suf == 0:
        return start, end, list(replacements)
    new_start = start + len("".join(o_tok[:pre]))
    new_end = end - len("".join(o_tok[len(o_tok) - suf:])) if suf else end
    new_repl = ["".join(t[pre:len(t) - suf] if suf else t[pre:]) for t in r_toks]
    if not text[new_start:new_end].strip():
        return start, end, list(replacements)
    return new_start, new_end, new_repl


_TYPO_TABLE = str.maketrans({
    "\u2019": "'", "\u2018": "'", "\u02bc": "'",
    "\u00a0": " ", "\u202f": " ", "\u2009": " ",
    "\u00ab": '"', "\u00bb": '"', "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
})


def typo_normalize(s: str) -> str:
    """Forme « tapée au clavier » d'un texte : apostrophes droites, guillemets
    droits, espaces normales, points de suspension en trois points."""
    s = s.translate(_TYPO_TABLE).replace("…", "...")
    s = re.sub(r"\s*\"\s*", '"', s)
    return s


def only_typographic_difference(a: str, b: str) -> bool:
    return a != b and typo_normalize(a) == typo_normalize(b)


def adapt_to_user_style(suggestion: str, text: str) -> str:
    """Dans les suggestions, garde les apostrophes et espaces du style de
    l'utilisateur (apostrophe droite si le texte n'utilise que celle-là)."""
    if "'" in text and "’" not in text:
        suggestion = suggestion.replace("’", "'")
    if "\u00a0" not in text and "\u202f" not in text:
        suggestion = suggestion.replace("\u00a0", " ").replace("\u202f", " ")
    return suggestion


def levenshtein(a: str, b: str, limit: int = 3) -> int:
    """Distance d'édition (arrêt anticipé au-delà de `limit`)."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            val = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(val)
            best = min(best, val)
        if best > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def strip_accents(s: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def same_letters(a: str, b: str) -> bool:
    """Vrai si deux mots ne diffèrent que par les accents ou la casse."""
    return a != b and strip_accents(a).casefold() == strip_accents(b).casefold()


def match_case(original: str, suggestion: str) -> str:
    """Recopie la casse de l'original sur la suggestion (majuscule initiale)."""
    if original[:1].isupper() and suggestion[:1].islower():
        return suggestion[:1].upper() + suggestion[1:]
    return suggestion
