"""Règles du correcteur : les fautes que Grammalecte ne voit pas, ou qu'il corrige mal.

Pas d'intelligence artificielle : des règles écrites à la main, chacune précise et mesurée sur corpus, qui
s'appuient sur le dictionnaire de Grammalecte (lemme, nature, temps, personne, genre et nombre de chaque mot)
et sur son module de conjugaison. Elles tournent dans le même passage que Grammalecte (voir
GrammalecteEngine._check_paragraph) : une règle qui porte sur la même zone qu'une remarque de Grammalecte la
remplace, et elle peut écarter les remarques voisines qu'elle sait fausses (« Il ces levé » : Grammalecte
propose « sait » pour « ces » et « levés » pour « levé » ; la bonne correction est « s'est »).

Chaque règle ne propose qu'une correction, et ne se déclenche que quand le contexte l'impose : ses
familles (« corr_… ») sont sûres pour le checker."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from correcteur.models import Category


@dataclass
class Faute:
    start: int
    end: int
    replacement: str | None   # None : rien à corriger ici, seulement écarter la remarque de Grammalecte (veto)
    message: str
    rule: str
    category: Category = Category.GRAMMAR
    drop: tuple[tuple[int, int], ...] = ()   # remarques de Grammalecte à écarter (zones exactes)


@dataclass
class Mot:
    text: str
    start: int
    end: int
    low: str = field(init=False)

    def __post_init__(self) -> None:
        self.low = self.text.lower().replace("’", "'")


# Mots élidés reconnus en tête d'un mot (« j'ai », « qu'il », « s'est », « jusqu'à ») : coupés en deux.
_ELISION_RE = re.compile(r"(?i)^(jusqu|lorsqu|puisqu|quoiqu|qu|[cdjlmnst])['’](?=\w)")
_WORD_RE = re.compile(r"[\w]+(?:['’-][\w]+)*['’]?")


def tokenize(text: str, known: Callable[[str], bool] | None = None) -> list[Mot]:
    """Mots du texte avec leurs positions. Les élisions sont séparées (« j' » + « ai »), sauf pour les mots
    du dictionnaire qui les contiennent (« aujourd'hui », « quelqu'un »)."""
    words: list[Mot] = []
    for m in _WORD_RE.finditer(text):
        token, start = m.group(0), m.start()
        while True:
            e = _ELISION_RE.match(token)
            if not e or (known is not None and known(token)):
                break
            cut = e.end()
            words.append(Mot(token[:cut], start, start + cut))
            token, start = token[cut:], start + cut
        if token:
            words.append(Mot(token, start, start + len(token)))
    return words


class Lexique:
    """Analyse morphologique de Grammalecte, avec cache. Étiquettes (exemples) :
    « >cueillir/:V3_it____a:Q:A:m:s/* » (participe passé masculin singulier de « cueillir »),
    « >fleur/:N:f:p/* » (nom féminin pluriel), « >être/:V0ei_____a:Ip:3s/* » (indicatif présent, 3e sing.)."""

    def __init__(self, spellchecker) -> None:
        self._sp = spellchecker
        self._cache: dict[str, list[str]] = {}

    def morph(self, word: str) -> list[str]:
        cached = self._cache.get(word)
        if cached is None:
            try:
                cached = list(self._sp.getMorph(word))
            except Exception:
                cached = []
            if len(self._cache) > 50_000:
                self._cache.clear()
            self._cache[word] = cached
        return cached

    def known(self, word: str) -> bool:
        return bool(self.morph(word))

    def verbs(self, word: str) -> list[tuple[str, str, str]]:
        """[(lemme, type du verbe, étiquettes)] des lectures verbales du mot."""
        out = []
        for m in self.morph(word):
            mm = re.match(r">([^/]+)/:V(\w+)((?::[^/:]+)*)", m)
            if mm:
                out.append((mm.group(1), mm.group(2), mm.group(3)))
        return out

    def has(self, word: str, tag: str) -> bool:
        return any(tag in m for m in self.morph(word))

    def is_noun(self, word: str) -> bool:
        return self.has(word, "/:N")

    def participle(self, word: str) -> tuple[str, str] | None:
        """(lemme, type) si le mot est un participe passé d'un seul verbe."""
        found = {(lemma, vtyp) for lemma, vtyp, tags in self.verbs(word) if ":Q" in tags}
        return next(iter(found)) if len(found) == 1 else None

    def finite_verb(self, word: str) -> bool:
        return any(re.search(r":(?:Ip|Iq|Is|If|K|Sp|Sq|E)\b", tags) for _, _, tags in self.verbs(word))

    def noun_gender_number(self, word: str) -> tuple[str, str] | None:
        """(genre, nombre) du nom, s'il n'en a qu'un : « m », « f » ou « e » (épicène) ; « s », « p » ou « i »."""
        found = set()
        for m in self.morph(word):
            mm = re.search(r"/:N(?::A)?:([mfe]):([spi])", m)
            if mm:
                found.add((mm.group(1), mm.group(2)))
        return next(iter(found)) if len(found) == 1 else None


def conj(lemma: str, tense: str, who: str) -> str | None:
    try:
        from grammalecte.fr import conj as _conj

        return _conj.getConj(lemma, tense, who) or None
    except Exception:
        return None


def match_case(original: str, replacement: str) -> str:
    if original[:1].isupper() and replacement[:1].islower():
        return replacement[:1].upper() + replacement[1:]
    return replacement


# ---------------------------------------------------------------------------------------------------------
# Vocabulaire

SUBJECTS = {"je": ":1s", "j'": ":1s", "tu": ":2s", "il": ":3s", "elle": ":3s", "on": ":3s", "nous": ":1p",
            "vous": ":2p", "ils": ":3p", "elles": ":3p"}
CLITICS = frozenset("ne n' me m' te t' se s' le la l' les lui leur y en nous vous".split())
REFLEXIVE = frozenset("se s' me m' te t' nous vous".split())
ETRE = frozenset("""suis es est sommes êtes sont étais était étions étiez étaient serai seras sera serons serez
    seront serais serait serions seriez seraient fus fut fûmes fûtes furent sois soit soyons soyez soient fusse
    fusses fût fussions fussiez fussent""".split())
AVOIR = frozenset("""ai as a avons avez ont avais avait avions aviez avaient aurai auras aura aurons aurez auront
    aurais aurait aurions auriez auraient eus eut eûmes eûtes eurent aie aies ait ayons ayez aient""".split())
BETWEEN = frozenset("""pas plus jamais guère point bien beaucoup souvent longtemps toujours déjà vraiment enfin
    même presque tous toutes encore mal trop peu tant vite rarement parfois tellement alors aussi""".split())
DETERMINERS = frozenset("""le la l' les un une des du de d' mon ma mes ton ta tes son sa ses notre nos votre vos leur
    leurs ce cet cette ces quelques plusieurs chaque aucun aucune tout toute tous toutes deux trois quatre cinq
    six sept huit neuf dix""".split())
PLURAL_DETERMINERS = frozenset("les des mes tes ses nos vos leurs ces quelques plusieurs certains certaines".split())
PREPOSITIONS = frozenset("""avec pour dans sur sous de à par chez sans vers contre entre parmi malgré selon après
    avant pendant depuis""".split())
# « avoir » + nom : « Les enfants on faim » -> « ont faim », « Elle à peur » -> « a peur ».
AVOIR_NOUNS = frozenset("""faim soif peur raison tort besoin envie honte froid chaud mal sommeil lieu horreur hâte
    confiance""".split())


def _sentence_start(words: list[Mot], i: int, text: str) -> bool:
    """Le mot i commence-t-il une phrase (début du texte, ou après . ! ? : ;) ?"""
    if i == 0:
        return not text[:words[0].start].strip(" \t\"«“(")
    gap = text[words[i - 1].end:words[i].start]
    return bool(re.search(r"[.!?:;]", gap))


def _joined(text: str, a: Mot, b: Mot) -> bool:
    """Rien d'autre que des espaces entre deux mots (pas de virgule, de tiret, de parenthèse)."""
    return not text[a.end:b.start].strip()


# ---------------------------------------------------------------------------------------------------------
# Participes des verbes pronominaux

# Pronom complément indirect (« se parler » : parler à quelqu'un) : le participe ne s'accorde jamais.
INDIRECT_PRONOMINAL = frozenset("""parler téléphoner sourire rire succéder ressembler mentir nuire suffire survivre
    déplaire complaire plaire convenir demander répondre""".split())
# Pronom complément direct ou verbe essentiellement pronominal : accord avec le sujet, sauf si un complément
# d'objet suit (« Elles se sont lavé les mains »).
DIRECT_PRONOMINAL = frozenset("""regarder laver lever coucher habiller déshabiller maquiller coiffer raser blesser
    promener réveiller préparer installer battre aimer détester haïr voir rencontrer quitter embrasser marier
    séparer cacher perdre tromper arrêter asseoir endormir reposer amuser ennuyer inquiéter présenter retrouver
    revoir croiser disputer connaître réunir sauver enfuir évanouir souvenir absenter moquer écrier efforcer
    méfier repentir taire plaindre tuer noyer brûler couper appeler lancer jeter mettre engager inscrire
    intéresser occuper entraîner sentir trouver montrer comporter conduire défendre rendre décider dépêcher
    tenir servir baigner doucher énerver fâcher calmer garer approcher éloigner enfermer échapper évader
    écrouler effondrer envoler endormir réjouir accrocher agenouiller allonger étendre égarer""".split())


def _participle_after_etre(words: list[Mot], i: int, lex: Lexique) -> int | None:
    """Indice du participe passé qui suit l'auxiliaire être en position i (adverbes sautés), ou None."""
    j = i + 1
    while j < len(words) and words[j].low in BETWEEN and j - i <= 3:
        j += 1
    if j < len(words) and lex.participle(words[j].text):
        return j
    return None


def regle_pronominal_indirect(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Ils se sont parlés hier » -> « parlé » ; « Ils se sont téléphonés » -> « téléphoné »."""
    for i in range(len(words) - 2):
        if words[i].low not in REFLEXIVE or words[i + 1].low not in ETRE:
            continue
        j = _participle_after_etre(words, i + 1, lex)
        if j is None:
            continue
        word = words[j]
        lemma, _ = lex.participle(word.text)
        if lemma not in INDIRECT_PRONOMINAL or word.low in ("plus", "plu"):
            continue
        base = conj(lemma, ":Q", ":m:s")
        if base and base != word.low:
            yield Faute(word.start, word.end, base, f"« se {lemma} » : le pronom est complément indirect (à soi, l'un "
                        "à l'autre), le participe passé ne s'accorde pas.", "corr_ppas_pronominal_indirect")
        elif base == word.low:
            # Déjà juste : Grammalecte voudrait l'accorder (« Ils se sont téléphonés »), à tort.
            yield Faute(word.start, word.end, None, "", "corr_ppas_pronominal_indirect")


def regle_pronominal_sujet(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Elles se sont regardé dans le miroir » -> « regardées » ; « Elle s'est blessé au genou » -> « blessée ».
    Pas quand un complément d'objet suit (« Elles se sont lavé les mains ») ni devant un infinitif."""
    targets = {"elle": ":f:s", "elles": ":f:p", "ils": ":m:p"}
    for k, subject in enumerate(words):
        target = targets.get(subject.low)
        if target is None:
            continue
        i = k + 1
        if i < len(words) and words[i].low in ("ne", "n'"):
            i += 1
        if i + 1 >= len(words) or words[i].low not in ("se", "s'") or words[i + 1].low not in ETRE:
            continue
        j = _participle_after_etre(words, i + 1, lex)
        if j is None:
            continue
        word = words[j]
        lemma, vtyp = lex.participle(word.text)
        if lemma not in DIRECT_PRONOMINAL:
            continue
        nxt = words[j + 1] if j + 1 < len(words) else None
        if nxt is not None and _joined(text, word, nxt):
            if (nxt.low in DETERMINERS or nxt.text[:1].isdigit() or lex.has(nxt.text, ":Y")
                    or (lex.is_noun(nxt.text) and not lex.has(nxt.text, ":G") and not lex.has(nxt.text, ":W"))):
                continue  # complément d'objet ou infinitif après le participe : pas d'accord avec le sujet
        form = conj(lemma, ":Q", target)
        if form and form != word.low:
            yield Faute(word.start, word.end, form, f"Verbe pronominal : le participe passé s'accorde avec le sujet "
                        f"« {subject.text} ».", "corr_ppas_pronominal_sujet")


# ---------------------------------------------------------------------------------------------------------
# Modes : subjonctif après « que », imparfait après « si »

_SUBJUNCTIVE_TRIGGERS = re.compile(
    r"(?i)\b(?:(?:il\s+)?(?:faut|faudrait|faudra|fallait|fallut|faille)"
    r"|(?:il\s+(?:est|était|serait|sera)\s+)(?:important|nécessaire|essentiel|indispensable|urgent|temps|possible|"
    r"impossible|dommage|normal|préférable|souhaitable|regrettable|inadmissible|utile|inutile|juste|naturel)"
    r"|(?:c['’]est\s+)(?:dommage|important|nécessaire|normal|possible|impossible)"
    r"|veux|veut|voulons|voulez|veulent|voudrais|voudrait|voudrions|voudriez|voudraient|voulais|voulait"
    r"|souhaite|souhaites|souhaitons|souhaitez|souhaitent|souhaiterais|souhaiterait|aimerais|aimerait|préfère|"
    r"préfères|préférerais|préférerait|exige|exiges|exigeons|exigez|exigent|ordonne|attends|attend|doute|doutes|"
    r"crains|craint|regrette|regrettes|"
    r"pour|afin|avant|bien|sans|moins|pourvu|condition|crainte|peur|jusqu['’]à\s+ce|en\s+attendant)\s+qu(?:e\s+|['’])$")
_QUOIQUE_RE = re.compile(r"(?i)\bquoiqu(?:e\s+|['’])$")


def regle_subjonctif(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Il faut que je fait » -> « fasse », « Bien qu'il est malade » -> « soit », « Il faut que nous allons »
    -> « allions » : après un déclencheur du subjonctif, le verbe dont le sujet est un pronom."""
    for k, subject in enumerate(words):
        who = SUBJECTS.get(subject.low)
        if who is None or k == 0 or words[k - 1].low not in ("que", "qu'"):
            continue
        before = text[max(0, words[k - 1].start - 40):subject.start]
        if not (_SUBJUNCTIVE_TRIGGERS.search(before) or _QUOIQUE_RE.search(before)):
            continue
        if re.search(r"(?i)\bsi\s+bien\s+qu(?:e\s+|['’])$", before):
            continue  # « si bien que » : conséquence, à l'indicatif
        j = k + 1
        while j < len(words) and words[j].low in CLITICS and j - k <= 3:
            j += 1
        if j >= len(words) or not _joined(text, words[j - 1], words[j]):
            continue
        verb = words[j]
        readings = lex.verbs(verb.text)
        lemmas = {lemma for lemma, _, tags in readings if re.search(r":(?:Ip|Iq|Is|If|K|Sp|Sq)\b", tags)}
        if len(lemmas) != 1:
            continue  # « suis » : être ou suivre ? On ne choisit pas.
        if any(":Sp" in tags and who in tags for _, _, tags in readings):
            continue  # déjà au subjonctif à la bonne personne
        lemma = next(iter(lemmas))
        form = conj(lemma, ":Sp", who)
        if not form or form == verb.low:
            continue
        start, form = verb.start, match_case(verb.text, form)
        elided = words[j - 1]
        if elided.low.endswith("'") and elided.end == verb.start and not re.match(r"(?i)[aeiouyhéèêàâîôû]", form):
            if elided.low not in ("n'", "m'", "t'", "s'"):
                continue  # « l'a » : « le » ou « la » ? On ne choisit pas.
            start, form = elided.start, elided.text[0] + "e " + form  # « n'est » -> « ne soit »
        yield Faute(start, verb.end, form, f"Après « {before.strip()} », le verbe se met au subjonctif.",
                    "corr_subjonctif")


def regle_si_conditionnel(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Si j'aurais su » -> « Si j'avais su » : après le « si » d'une hypothèse, l'imparfait, pas le
    conditionnel. Seulement en tête de phrase : « Je me demande si tu pourrais » (interrogation) est juste."""
    for k, si in enumerate(words):
        if si.low not in ("si", "s'") or not _sentence_start(words, k, text):
            continue
        j = k + 1
        if j >= len(words) or words[j].low not in SUBJECTS or (si.low == "s'" and words[j].low not in ("il", "ils")):
            continue
        who = SUBJECTS[words[j].low]
        j += 1
        while j < len(words) and words[j].low in CLITICS and j - k <= 4:
            j += 1
        if j >= len(words):
            continue
        verb = words[j]
        readings = lex.verbs(verb.text)
        conditional = {lemma for lemma, _, tags in readings if ":K" in tags and who in tags}
        if len(conditional) != 1 or len({lemma for lemma, _, _ in readings}) != 1:
            continue
        form = conj(next(iter(conditional)), ":Iq", who)
        if form and form != verb.low:
            yield Faute(verb.start, verb.end, form, "Après le « si » d'une hypothèse, l'imparfait et non le conditionnel "
                        "(« Si j'avais su »).", "corr_si_imparfait")


# ---------------------------------------------------------------------------------------------------------
# Homophones

def regle_sest(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Il ces levé » -> « Il s'est levé », « Elle sait trompée » -> « Elle s'est trompée »."""
    for k in range(len(words) - 2):
        subject, word, nxt = words[k], words[k + 1], words[k + 2]
        if subject.low not in ("il", "elle", "on", "qui", "ça", "cela") or word.low not in ("ces", "ses", "sait", "sais"):
            continue
        if not (_joined(text, subject, word) and _joined(text, word, nxt)) or not lex.participle(nxt.text):
            continue
        if lex.has(nxt.text, "/:N") and not lex.has(nxt.text, ":Q"):
            continue
        yield Faute(word.start, word.end, "s'est", "« s'est » (verbe pronominal au passé composé) devant un participe "
                    "passé.", "corr_sest", drop=((nxt.start, nxt.end),))


def _plural_subject_before(words: list[Mot], i: int, lex: Lexique) -> bool:
    """Un groupe nominal pluriel finit juste avant le mot i : « Mes parents », « Les filles », « Les enfants
    du quartier » non (on s'en tient à déterminant pluriel + nom, éventuellement suivi d'un adjectif)."""
    j = i - 1
    if j >= 0 and lex.has(words[j].text, ":A") and not lex.is_noun(words[j].text) and j - 1 >= 0:
        j -= 1  # adjectif après le nom
    if j < 1:
        return False
    noun, det = words[j], words[j - 1]
    gn = lex.noun_gender_number(noun.text)
    return det.low in PLURAL_DETERMINERS and gn is not None and gn[1] in ("p", "i")


def regle_son_on_pluriel(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Mes parents son partis » -> « sont » ; « Les enfants on faim » -> « ont ». Grammalecte propose « parti »
    ou « feint » pour le mot d'après : ces remarques sont écartées."""
    for i in range(1, len(words) - 1):
        word, nxt = words[i], words[i + 1]
        if word.low not in ("son", "on") or not _joined(text, words[i - 1], word) or not _plural_subject_before(words, i, lex):
            continue
        k = i + 1
        while k < len(words) - 1 and words[k].low in ("très", "trop", "si", "bien", "tous", "toutes", "déjà", "pas"):
            k += 1
        after = words[k]
        if word.low == "son":
            ok = (lex.has(after.text, ":Q") or lex.has(after.text, ":A")) and not lex.has(after.text, ":G")
            fix, msg = "sont", "« sont » (verbe être) : le sujet est pluriel."
        else:
            ok = lex.has(after.text, ":Q") or after.low in AVOIR_NOUNS
            fix, msg = "ont", "« ont » (verbe avoir) : le sujet est pluriel."
        if ok:
            yield Faute(word.start, word.end, fix, msg, "corr_son_on_pluriel", drop=((after.start, after.end),))


def regle_qui_on(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Les gens qui on vu le film » -> « qui ont vu » : « qui » repris d'un pluriel, puis un participe."""
    for i in range(2, len(words) - 1):
        if words[i].low != "on" or words[i - 1].low != "qui" or not lex.participle(words[i + 1].text):
            continue
        if lex.finite_verb(words[i + 1].text):
            continue
        if not (_plural_subject_before(words, i - 1, lex) or words[i - 2].low in ("eux", "elles", "ceux", "celles")):
            continue
        yield Faute(words[i].start, words[i].end, "ont", "« ont » (verbe avoir) : « qui » reprend un pluriel.",
                    "corr_son_on_pluriel", drop=((words[i + 1].start, words[i + 1].end),))


def regle_ce_se(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Il ce lave » -> « Il se lave » : après un pronom sujet, « ce » ne peut être que « se »."""
    for i in range(1, len(words) - 1):
        if words[i].low != "ce" or words[i - 1].low not in ("il", "elle", "on", "ils", "elles"):
            continue
        if not _joined(text, words[i - 1], words[i]) or not lex.finite_verb(words[i + 1].text):
            continue
        yield Faute(words[i].start, words[i].end, "se", "« se » (pronom réfléchi) devant un verbe.", "corr_ce_se")


def regle_ces_cest(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Je pense que ces vrai » -> « que c'est vrai » : « ces » suivi d'un adjectif seul, en fin de groupe."""
    for i in range(len(words) - 1):
        word, adj = words[i], words[i + 1]
        if word.low != "ces":
            continue
        if not (i == 0 or words[i - 1].low in ("que", "qu'", "mais", "et", "car", "donc", "alors", "si")):
            continue
        if not lex.has(adj.text, ":A") or lex.is_noun(adj.text) and not lex.has(adj.text, "/:A") or re.search(
                r":A:[mfe]:p", " ".join(lex.morph(adj.text))):
            continue
        rest = text[adj.end:adj.end + 3]
        if i + 2 < len(words) and _joined(text, adj, words[i + 2]) and (lex.is_noun(words[i + 2].text) or lex.has(
                words[i + 2].text, ":A")):
            continue  # « ces bons amis » : déterminant
        if not re.match(r"\s*(?:[.!?,;:)]|$)", rest):
            continue
        yield Faute(word.start, word.end, match_case(word.text, "c'est"), "« c'est » devant un adjectif.",
                    "corr_ces_cest", drop=((adj.start, adj.end),))


def regle_cest_ces(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« C'est enfants sont sages » -> « Ces enfants » : « c'est » devant un nom pluriel sans déterminant, suivi
    d'un verbe."""
    for i in range(len(words) - 3):
        c, est, noun, verb = words[i], words[i + 1], words[i + 2], words[i + 3]
        if c.low != "c'" or est.low != "est" or est.start != c.end:
            continue
        gn = lex.noun_gender_number(noun.text)
        if gn is None or gn[1] != "p" or not lex.finite_verb(verb.text) or lex.has(noun.text, ":G"):
            continue
        yield Faute(c.start, est.end, match_case(c.text, "ces"), "« ces » (déterminant) devant un nom pluriel.",
                    "corr_cest_ces", drop=((noun.start, noun.end),))


def regle_mais_mes(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Mais amis sont venus » -> « Mes amis » : en tête de phrase, devant un nom pluriel puis un verbe."""
    for i in range(len(words) - 2):
        if words[i].low != "mais" or not _sentence_start(words, i, text) or not _joined(text, words[i], words[i + 1]):
            continue
        gn = lex.noun_gender_number(words[i + 1].text)
        if gn is None or gn[1] != "p" or lex.has(words[i + 1].text, ":G") or lex.has(words[i + 1].text, ":W"):
            continue
        if not lex.finite_verb(words[i + 2].text):
            continue
        yield Faute(words[i].start, words[i].end, match_case(words[i].text, "mes"),
                    "« mes » (déterminant) devant un nom pluriel.", "corr_mais_mes")


_MOVE_VERBS = frozenset("aller partir venir arriver rentrer retourner revenir monter descendre passer rester entrer sortir".split())


def regle_a_accent(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« a » mis pour « à » là où le verbe avoir est impossible : « Merci a tous », « mal a la main »,
    « partis a la mer »."""
    for i in range(1, len(words) - 1):
        word, prev, nxt = words[i], words[i - 1], words[i + 1]
        if word.low != "a" or not _joined(text, prev, word):
            continue
        fix = False
        if prev.low == "merci" and (nxt.low in ("tous", "toutes", "vous", "toi", "lui", "elle", "elles", "eux", "nous",
                                                  "moi", "tout", "chacun", "chacune") or nxt.text[:1].isupper()):
            fix = True
        elif prev.low == "mal" and nxt.low in ("la", "l'", "au", "aux", "mon", "ma", "mes", "ton", "ta", "tes", "son",
                                               "sa", "ses", "notre", "nos", "votre", "vos", "leur", "leurs"):
            fix = i < 2 or words[i - 2].low not in ("le", "un", "du", "ce")  # « le mal a la vie dure » : nom sujet
        else:
            part = lex.participle(prev.text)
            if part and part[0] in _MOVE_VERBS and (nxt.low in ("la", "l'", "les", "un", "une") or nxt.text[:1].isupper()
                                                   or nxt.text[:1].isdigit()):
                fix = True
        if fix:
            yield Faute(word.start, word.end, match_case(word.text, "à"), "« à » (préposition) : le verbe avoir est "
                        "impossible ici.", "corr_a_preposition")


def regle_a_verbe(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« à » mis pour « a » : « La réunion à lieu », « Elle à faim », « Le film à commencé »."""
    for i in range(1, len(words) - 1):
        word, prev, nxt = words[i], words[i - 1], words[i + 1]
        if word.low != "à" or not _joined(text, prev, word) or not _joined(text, word, nxt):
            continue
        if nxt.low == "lieu":
            if prev.low in ("au", "en"):
                continue
            yield Faute(word.start, word.end, "a", "« a lieu » (verbe avoir lieu).", "corr_a_verbe")
            continue
        subject = prev.low in ("il", "elle", "on", "qui") or (i >= 2 and words[i - 2].low in ("le", "la", "l'", "un",
                                                                                                "une", "ce", "cet",
                                                                                                "cette", "mon", "ma",
                                                                                                "ton", "ta", "son",
                                                                                                "sa") and lex.is_noun(prev.text))
        if not subject:
            continue
        if nxt.low in AVOIR_NOUNS or (lex.participle(nxt.text) and not lex.is_noun(nxt.text)
                                      and nxt.low not in ("découvert", "couvert")):
            yield Faute(word.start, word.end, "a", "« a » (verbe avoir) après le sujet.", "corr_a_verbe",
                        drop=((nxt.start, nxt.end),))


def veto_a_ete_adjectif(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« un climat tempéré à été chaud » (climat à été chaud) : Grammalecte voudrait « a été »."""
    for i in range(1, len(words) - 1):
        if words[i].low == "à" and words[i + 1].low == "été" and lex.has(words[i - 1].text, ":A") and not (
                lex.is_noun(words[i - 1].text) and not lex.has(words[i - 1].text, ":Q")):
            yield Faute(words[i].start, words[i].end, None, "", "corr_veto")


def regle_c_est_a_dire(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« c'est a dire » ou « c'est à dire » -> « c'est-à-dire »."""
    for m in re.finditer(r"(?i)\bc['’]est[\s-]+[aà][\s-]+dire\b", text):
        if m.group(0).lower().replace("’", "'") != "c'est-à-dire":
            yield Faute(m.start(), m.end(), match_case(m.group(0), "c'est-à-dire"), "Locution « c'est-à-dire », avec "
                        "des traits d'union.", "corr_c_est_a_dire")


def regle_ni_ny(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Il ni a rien » -> « Il n'y a rien »."""
    for i in range(1, len(words) - 1):
        if words[i].low == "ni" and words[i - 1].low in ("il", "elle", "on", "ils", "elles") and (
                words[i + 1].low in AVOIR or words[i + 1].low in ("en", "aura", "aurait", "avait")):
            yield Faute(words[i].start, words[i].end, "n'y", "« n'y » (négation et pronom « y »).", "corr_ni_ny")


def regle_prie(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Je vous pris de m'excuser » -> « Je vous prie », « Je vous en pris » -> « Je vous en prie »."""
    for m in re.finditer(r"(?i)\b(?:je|j['’])\s*(?:vous|te|t['’])\s*(?:en\s+)?(pris)\b(?=\s+(?:de\b|d['’])|\s*[.!,]|$)",
                         text):
        yield Faute(m.start(1), m.end(1), "prie", "« prier » au présent : « je vous prie ».", "corr_prie")


def regle_avoir_ete(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Il a était malade » -> « Il a été malade »."""
    for i in range(1, len(words)):
        if words[i].low in ("était", "étais", "étaient") and words[i - 1].low in AVOIR and _joined(text, words[i - 1], words[i]):
            yield Faute(words[i].start, words[i].end, "été", "Participe passé « été » après l'auxiliaire avoir.",
                        "corr_avoir_ete")


def regle_je_ai(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Je les aient vus » -> « Je les ai vus » : auxiliaire de « je » devant un participe."""
    for k, subject in enumerate(words):
        if subject.low not in ("je", "j'"):
            continue
        before = text[max(0, subject.start - 40):subject.start]
        if re.search(r"(?i)\bqu(?:e\s*|['’])$", before):
            continue  # « que j'aie » : subjonctif possible
        j = k + 1
        while j < len(words) and words[j].low in CLITICS - {"ne", "n'"} and j - k <= 3:
            j += 1
        if j + 1 >= len(words) or words[j].low not in ("aient", "ait", "aies", "aie", "est", "es", "et"):
            continue
        part = lex.participle(words[j + 1].text)
        if part is None or lex.finite_verb(words[j + 1].text):
            continue
        vtyp = part[1]
        if len(vtyp) > 8 and vtyp[7] == "e" and vtyp[8] != "a":
            fix = "suis"  # « Je est venu » -> « Je suis venu »
        else:
            fix = "ai"
        yield Faute(words[j].start, words[j].end, fix, f"Auxiliaire à la 1re personne : « je {fix} ».", "corr_je_ai")


def regle_quel_genre(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Quel heure est-il ? » -> « Quelle heure » : « quel » s'accorde avec le nom qui suit."""
    forms = {("m", "s"): "quel", ("m", "p"): "quels", ("f", "s"): "quelle", ("f", "p"): "quelles"}
    for i in range(len(words) - 1):
        word, noun = words[i], words[i + 1]
        if word.low not in ("quel", "quels", "quelle", "quelles") or not _joined(text, word, noun):
            continue
        gn = lex.noun_gender_number(noun.text)
        if gn is None or gn[0] not in ("m", "f") or gn[1] not in ("s", "p") or lex.has(noun.text, ":V"):
            continue
        form = forms[gn]
        if form != word.low:
            yield Faute(word.start, word.end, match_case(word.text, form), f"« {form} » s'accorde avec « {noun.text} ».",
                        "corr_quel_accord", drop=((noun.start, noun.end),))


def regle_sont_son(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Il a pris sont manteau » -> « son manteau » : « sont » devant un nom singulier, après un verbe ou une
    préposition (jamais après un sujet)."""
    for i in range(1, len(words) - 1):
        word, prev, noun = words[i], words[i - 1], words[i + 1]
        if word.low != "sont" or not _joined(text, prev, word) or not _joined(text, word, noun):
            continue
        gn = lex.noun_gender_number(noun.text)
        if gn is None or gn[1] != "s" or lex.has(noun.text, ":V") or lex.has(noun.text, ":A") or lex.has(noun.text, ":G"):
            continue
        after_verb = prev.low in PREPOSITIONS or (lex.participle(prev.text) and prev.low not in ETRE) or (
            lex.finite_verb(prev.text) and prev.low not in ETRE and not lex.is_noun(prev.text))
        if not after_verb or prev.low in SUBJECTS:
            continue
        det = "sa" if gn[0] == "f" and not re.match(r"(?i)[aeiouyhéèêàâîôû]", noun.text) else "son"
        yield Faute(word.start, word.end, det, f"« {det} » (déterminant possessif) devant un nom.", "corr_sont_son",
                    drop=((noun.start, noun.end),))


def regle_cod_que(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Les fleurs que j'ai cueilli » -> « cueillies » : complément d'objet placé avant (« que » reprend le
    nom), participe d'un verbe transitif direct après avoir."""
    gender_of_det = {"la": "f", "une": "f", "cette": "f", "ma": "f", "ta": "f", "sa": "f", "le": "m", "un": "m",
                     "ce": "m", "cet": "m", "mon": "m", "ton": "m", "son": "m"}
    for q in range(2, len(words) - 3):
        if words[q].low not in ("que", "qu'"):
            continue
        noun, det = words[q - 1], words[q - 2]
        gn = lex.noun_gender_number(noun.text)
        if gn is None or det.low not in DETERMINERS or not _joined(text, det, noun):
            continue
        gender, number = gn
        if gender == "e":
            gender = gender_of_det.get(det.low, "m")
        if number == "i":
            number = "p" if det.low in PLURAL_DETERMINERS else "s"
        if (gender, number) == ("m", "s"):
            continue
        k = q + 1
        if words[k].low not in SUBJECTS:
            continue
        j = k + 1
        while j < len(words) and words[j].low in ("ne", "n'", "lui", "leur", "y", "en", "me", "m'", "te", "t'", "nous",
                                                  "vous") and j - k <= 3:
            j += 1
        if j + 1 >= len(words) or words[j].low not in AVOIR:
            continue
        p = j + 1
        while p < len(words) and words[p].low in BETWEEN and p - j <= 3:
            p += 1
        if p >= len(words):
            continue
        part = lex.participle(words[p].text)
        if part is None:
            continue
        lemma, vtyp = part
        if len(vtyp) < 7 or vtyp[3] != "t" or vtyp[6] == "m" or lemma in ("faire", "laisser", "falloir", "valoir",
                                                                             "coûter", "peser", "mesurer", "vivre",
                                                                             "durer", "courir", "dormir", "régner"):
            continue
        if p + 1 < len(words) and _joined(text, words[p], words[p + 1]) and (
                lex.has(words[p + 1].text, ":Y") or words[p + 1].low in DETERMINERS):
            continue  # « que j'ai vu partir », « que j'ai donné à » ... : pas sûr
        form = conj(lemma, ":Q", f":{gender}:{number}")
        if form and form != words[p].low:
            yield Faute(words[p].start, words[p].end, form, f"Complément d'objet placé avant (« {noun.text} ») : le "
                        "participe passé s'accorde avec lui.", "corr_ppas_cod_avant")


# Fautes d'orthographe fréquentes que les suggestions de Grammalecte classent mal (« fesait » : « faisait »
# n'était pas proposé ; « malgrès » : « maigres » en premier).
FREQUENT = {
    "malgrès": "malgré", "parmis": "parmi", "ormis": "hormis", "notament": "notamment", "vraiement": "vraiment",
    "fesait": "faisait", "fesais": "faisais", "fesaient": "faisaient", "fesant": "faisant", "fesons": "faisons",
    "aparemment": "apparemment", "apparament": "apparemment", "apparement": "apparemment",
    "dailleurs": "d'ailleurs", "aujourdhui": "aujourd'hui", "biensur": "bien sûr", "parcontre": "par contre",
    "connection": "connexion", "dévelloper": "développer", "dévellopement": "développement",
    "language": "langage", "personellement": "personnellement", "rapellé": "rappelé", "apellé": "appelé",
    "baucoup": "beaucoup", "beacoup": "beaucoup",
}


def regle_orthographe_frequente(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    for w in words:
        fix = FREQUENT.get(w.low)
        if fix is not None and not (w.low == "language" and lex.known(w.text)):
            yield Faute(w.start, w.end, match_case(w.text, fix), f"Orthographe : « {fix} ».", "corr_orthographe",
                        Category.SPELLING)


RULES: tuple[Callable[[str, list[Mot], Lexique], Iterable[Faute]], ...] = (
    regle_pronominal_indirect, regle_pronominal_sujet, regle_subjonctif, regle_si_conditionnel, regle_sest,
    regle_son_on_pluriel, regle_qui_on, regle_ce_se, regle_ces_cest, regle_cest_ces, regle_mais_mes,
    regle_a_accent, regle_a_verbe, regle_c_est_a_dire, regle_ni_ny, regle_prie, regle_avoir_ete, regle_je_ai,
    regle_quel_genre, regle_sont_son, regle_cod_que, regle_orthographe_frequente, veto_a_ete_adjectif,
)


def check(text: str, lex: Lexique) -> list[Faute]:
    """Fautes trouvées par les règles du correcteur, sans doublon de zone (la première règle l'emporte)."""
    words = tokenize(text, lex.known)
    found: list[Faute] = []
    taken: list[tuple[int, int]] = []
    for rule in RULES:
        try:
            for faute in rule(text, words, lex):
                if any(faute.start < e and s < faute.end for s, e in taken):
                    continue
                found.append(faute)
                taken.append((faute.start, faute.end))
        except Exception:  # une règle en défaut ne doit jamais empêcher la vérification
            continue
    return found
