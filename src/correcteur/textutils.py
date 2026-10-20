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
    """Découpe en lignes (paragraphes) et renvoie (offset, contenu). Fins de ligne \\n, \\r\\n ou \\r seul
    (anciens fichiers Mac) : sans ce dernier, tout le fichier passait pour un seul paragraphe."""
    pos = 0
    for m in re.finditer(r"\r\n|\r|\n", text):
        yield pos, text[pos:m.start()]
        pos = m.end()
    yield pos, text[pos:]


def chunk_text(text: str, max_len: int) -> Iterator[tuple[int, str]]:
    """Découpe un long texte en morceaux d'au plus `max_len` caractères, sur des
    fins de paragraphe quand c'est possible, sinon sur des fins de phrase, sinon
    entre deux mots (jamais au milieu d'un mot, qui serait signalé à tort)."""
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_len, n)
        if end < n:
            cut = text.rfind("\n", start, end)
            if cut <= start:
                cut = max(text.rfind(". ", start, end), text.rfind("! ", start, end), text.rfind("? ", start, end))
                cut = cut + 1 if cut > start else -1
            if cut <= start:
                cut = max(text.rfind(" ", start, end), text.rfind("\t", start, end))
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


# Au-delà de cette taille, les deux versions sont d'abord appariées ligne à ligne, puis phrase à phrase : le
# diff mot à mot d'un long texte répétitif (mêmes mots des milliers de fois) prenait 45 s pour 300 lignes.
_DIFF_DIRECT_MAX = 4000
_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+")
_SENTENCE_RE = re.compile(r".*?(?:[.!?…]+\s+|\Z)", re.DOTALL)


def _pieces(text: str, pattern: re.Pattern) -> list[str]:
    return [m.group() for m in pattern.finditer(text) if m.group()]


def _paired(original: str, corrected: str, level: int = 0) -> Iterator[tuple[int, str, str]]:
    """(début dans `original`, morceau d'origine, morceau corrigé) : des morceaux courts, dont les concaténations
    redonnent les deux textes. Lignes (puis phrases) appariées une à une quand leur nombre est le même ; sinon
    par un diff sur les morceaux entiers, rapide puisqu'ils sont peu nombreux."""
    if len(original) + len(corrected) <= _DIFF_DIRECT_MAX or level > 1:
        yield 0, original, corrected
        return
    pattern = _LINE_RE if level == 0 else _SENTENCE_RE
    a, b = _pieces(original, pattern), _pieces(corrected, pattern)
    if len(a) == len(b):
        pairs = list(zip(a, b))
    else:
        pairs = []
        matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                pairs += list(zip(a[i1:i2], b[j1:j2]))
            else:
                pairs.append(("".join(a[i1:i2]), "".join(b[j1:j2])))
    if len(pairs) == 1:
        yield from _paired(original, corrected, level + 1)
        return
    offset = 0
    for old, new in pairs:
        if old != new:
            for start, sub_old, sub_new in _paired(old, new, level + 1):
                yield offset + start, sub_old, sub_new
        offset += len(old)


def diff_edits(original: str, corrected: str) -> list[tuple[int, int, str]]:
    """Compare deux versions d'un texte mot à mot et renvoie les modifications
    sous forme (debut, fin, remplacement) dans les coordonnées de `original`.

    Une modification qui ne touche que de la ponctuation ou des espaces est
    rattachée au mot voisin pour être visible et compréhensible
    ("Bonjour Marie" -> "Bonjour, Marie" donne "Bonjour" -> "Bonjour,")."""
    if original == corrected:
        return []
    edits: list[tuple[int, int, str]] = []
    for offset, old, new in _paired(original, corrected):
        if old != new:
            edits += [(offset + s, offset + e, rep) for s, e, rep in _diff_words(old, new)]
    return edits


def _diff_words(original: str, corrected: str) -> list[tuple[int, int, str]]:
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


def adapt_to_user_style(suggestion: str, text: str, original: str = "") -> str:
    """Dans les suggestions, garde les apostrophes et espaces du style de
    l'utilisateur. L'apostrophe suit celle du passage corrigé ; s'il n'en a pas,
    celle que le texte emploie le plus (la droite, celle du clavier, à égalité).
    Un seul « ’ » collé d'ailleurs ne change donc pas « c'est » en « s’est »."""
    if "’" in original or "'" in original:
        curly = "’" in original
    else:
        curly = text.count("’") > text.count("'")
    suggestion = suggestion.replace("'", "’") if curly else suggestion.replace("’", "'")
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


# « nous » ou « vous » sujets : en tête de phrase, après une ponctuation ou une conjonction (« Ils nous ont
# dit » : « nous » complément, le verbe s'accorde avec « ils »).
_SUBJECT_ENDINGS = {"nous": "ons", "vous": "ez"}
_SUBJECT_BEFORE_RE = re.compile(r"(?:(?P<start>\A)|[.!?…;:,(«\"]\s*|\b(?:et|mais|ou|donc|puis|alors|que|qu|quand|si|"
                                r"comme|lorsque|car)\s+)(nous|vous)\s+\Z", re.IGNORECASE)


def subject_ending(text: str, start: int) -> str | None:
    """Terminaison du verbe qui suit « nous » ou « vous » sujets juste avant `start` (« ons », « ez »), sinon
    None."""
    lo = max(0, start - 60)
    m = _SUBJECT_BEFORE_RE.search(text, lo, start)
    if m is None or (m.group("start") is not None and lo > 0):
        return None
    return _SUBJECT_ENDINGS[m.group(2).lower()]


def agree_with_subject(text: str, start: int, suggestions: Sequence[str]) -> tuple[str, ...]:
    """« Nous avont fini » : « avons », seule suggestion qui s'accorde avec « nous », passe en tête (Grammalecte
    la classait 4e, après « amont » et « avant »)."""
    ending = subject_ending(text, start)
    matching = [s for s in suggestions if ending and s.endswith(ending)]
    if len(matching) != 1:
        return tuple(suggestions)
    return (matching[0], *(s for s in suggestions if s != matching[0]))


_E_SOUND_RE = re.compile(r"(?:ées|ée|és|er|ez)$")
_SILENT_ENDINGS = ("ds", "ts", "es", "e", "s", "t", "x", "d")
_DOUBLE_END_RE = re.compile(r"([^aeiouyéèêàâîôûnm])\1$")  # « appell » comme « appel » ; « prenn » n'est pas « pren »


def _sound_keys(word: str) -> set[str]:
    word = _E_SOUND_RE.sub("é", word.lower().replace("’", "'"))
    ending = next((e for e in _SILENT_ENDINGS if word.endswith(e) and len(word) - len(e) >= 2), "")
    stems = {word[:len(word) - len(ending)]}
    if word.endswith("ent") and len(word) >= 6:
        stems.add(word[:-3])  # « mangent », « attendent » se prononcent « mange », « attende »
    return {_DOUBLE_END_RE.sub(r"\1", stem) for stem in stems}


def same_sound(a: str, b: str) -> bool:
    """Deux formes qui ne diffèrent que par des lettres muettes de fin : « fini / finis / finit », « allé /
    allez », « appel / appelle », « sui / suis ». Parmi les conjugaisons que propose Grammalecte (« Je fini » :
    « finis » ou « finissais » ?), c'est celle que la personne voulait écrire. Mots de 3 lettres au moins :
    « ni » n'est pas « nie »."""
    if min(len(a), len(b)) < 3:
        return a.lower() == b.lower()
    return bool(_sound_keys(a) & _sound_keys(b))


def one_edit(a: str, b: str) -> bool:
    """Une seule faute de frappe d'écart : une lettre en trop, en moins, remplacée, ou deux voisines échangées."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]]
                                  and a[diff[1]] == b[diff[0]])
    short, long_ = sorted((a, b), key=len)
    i = next((k for k in range(len(short)) if short[k] != long_[k]), len(short))
    return short[i:] == long_[i + 1:]


def same_letters(a: str, b: str) -> bool:
    """Vrai si deux mots ne diffèrent que par les accents ou la casse."""
    return a != b and strip_accents(a).casefold() == strip_accents(b).casefold()


def match_case(original: str, suggestion: str) -> str:
    """Recopie la casse de l'original sur la suggestion (majuscule initiale)."""
    if original[:1].isupper() and suggestion[:1].islower():
        return suggestion[:1].upper() + suggestion[1:]
    return suggestion


_JOINERS = "'’-"


def _word_group(text: str, start: int, end: int, reach: int = 30) -> tuple[int, int]:
    """Étend [start, end) aux mots entiers, apostrophes et traits d'union compris : « c'est → s'est » plutôt que
    « c → s », « fait t'il → fait-il » plutôt que « t' → - »."""
    def joined(i: int) -> bool:  # text[i] colle au mot qui le précède ou le suit
        return text[i].isalnum() or (text[i] in _JOINERS and 0 < i < len(text) - 1
                                     and text[i - 1].isalnum() and text[i + 1].isalnum())

    s, e = start, end
    while s > 0 and start - s < reach and joined(s - 1):
        s -= 1
    while e < len(text) and e - end < reach and joined(e):
        e += 1
    return s, e


def _with_punctuation(text: str, end: int) -> int:
    """Fin d'un groupe de mots étendue à la ponctuation qui le suit de près : « plage . → plage. » plutôt
    qu'une espace corrigée sans rien de lisible."""
    j = end
    while j < len(text) and j - end < 3 and not text[j].isalnum() and text[j] != "\n":
        j += 1
    run = text[end:j]
    return end + len(run.rstrip()) if run.strip() else end


def describe_changes(before: str, after: str, limit: int = 4) -> str:
    """« aller → allé, a → à, obtenu → obtenus… » : ce qu'une correction a changé, mot à mot. Une virgule
    déplacée se montre en un seul morceau (« heures ,Zorglub → heures, Zorglub », pas « heures → heures,,
    ,Zorglub → Zorglub »). Les espaces corrigées sont comptées à la fin (« a → à, 1 espace corrigée ») ; seules,
    elles se montrent avec la ponctuation qu'elles touchent (« plage . → plage. »)."""
    words, spaces = _changed_words(before, after, with_punctuation=False)
    if not words and spaces:
        words, spaces = _changed_words(before, after, with_punctuation=True)
    if not words:
        return ""
    if len(words) > limit:
        return ", ".join(words[:limit]) + "…"
    if spaces:
        words.append("1 espace corrigée" if spaces == 1 else f"{spaces} espaces corrigées")
    return ", ".join(words)


def _changed_words(before: str, after: str, with_punctuation: bool) -> tuple[list[str], int]:
    """(« ancien → nouveau » par groupe de mots modifié, nombre d'espaces corrigées sans autre changement)."""
    def shown(s2: int, e2: int, edits: list[tuple[int, int, str]]) -> tuple[str, str]:
        pieces, pos = [], s2
        for s, e, rep in edits:
            pieces += [before[pos:s], rep]
            pos = e
        pieces.append(before[pos:e2])
        return before[s2:e2].strip(), "".join(pieces).strip()

    groups: list[list] = []  # [début, fin, modifications] dans `before`
    for s, e, rep in diff_edits(before, after):
        s2, e2 = _word_group(before, s, e)
        old, new = shown(s2, e2, [(s, e, rep)])
        if with_punctuation and old.split() == new.split():
            e2 = _with_punctuation(before, e2)
        if groups:
            gap = before[groups[-1][1]:s2]
            punctuation = any(t and not t[-1].isalnum() for t in shown(*groups[-1])) or any(
                t and not t[0].isalnum() for t in shown(s2, e2, [(s, e, rep)]))
            if s2 <= groups[-1][1] or (len(gap) <= 2 and not gap.strip() and punctuation):
                groups[-1][1] = max(groups[-1][1], e2)
                groups[-1][2].append((s, e, rep))
                continue
        groups.append([s2, e2, [(s, e, rep)]])
    words, spaces = [], 0
    for group in groups:
        old, new = shown(*group)
        if old.split() != new.split():
            words.append(f"{old} → {new}")
        else:
            spaces += 1
    return words, spaces
