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
from correcteur.textutils import same_sound


# Règle des vetos : une zone juste, où aucun moteur ne doit corriger (voir Checker.post_process).
VETO = "corr_veto"


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

        form = _conj.getConj(lemma, tense, who) or None
        if form is None and tense == ":Q" and who == ":m:p":
            # « mis », « pris » : Grammalecte ne donne pas le masculin pluriel quand il est celui du singulier.
            single = _conj.getConj(lemma, ":Q", ":m:s") or ""
            form = single if single.endswith(("s", "x")) else None
        return form
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
    déplaire complaire plaire convenir demander répondre permettre écrire""".split())
# Pronom complément direct ou verbe essentiellement pronominal : accord avec le sujet, sauf si un complément
# d'objet suit (« Elles se sont lavé les mains »).
DIRECT_PRONOMINAL = frozenset("""regarder laver lever coucher habiller déshabiller maquiller coiffer raser blesser
    promener réveiller préparer installer battre aimer détester haïr voir rencontrer quitter embrasser marier surprendre
    séparer cacher perdre tromper arrêter asseoir assoir endormir reposer amuser ennuyer inquiéter présenter retrouver
    revoir croiser disputer connaître réunir sauver enfuir évanouir souvenir absenter moquer écrier efforcer
    méfier repentir taire plaindre tuer noyer brûler couper appeler lancer jeter mettre engager inscrire
    intéresser occuper entraîner sentir trouver montrer comporter conduire défendre rendre décider dépêcher
    tenir servir baigner doucher énerver fâcher calmer garer approcher éloigner enfermer échapper évader
    écrouler effondrer envoler endormir réjouir accrocher agenouiller allonger étendre égarer""".split())


# Verbes toujours pronominaux : le participe s'accorde avec le sujet, quoi qui suive (« Elle s'est souvenue de
# tout », « Ils se sont emparés du ballon »).
ESSENTIALLY_PRONOMINAL = frozenset("""souvenir enfuir évanouir absenter moquer écrier efforcer méfier repentir abstenir
    emparer envoler écrouler évader agenouiller blottir""".split())


def participle_like(word: str, lex: Lexique) -> tuple[str, str] | None:
    """(lemme, type) du participe passé, ou du verbe conjugué qui se prononce comme lui : « Elle s'est mit »,
    « assit », « permit » (passé simple) mis pour « mise », « assise », « permis » ; « s'est lever »."""
    found = lex.participle(word)
    if found:
        return found
    if not lex.known(word):
        # « Ils se sont succédés » : forme qui n'existe pas (le participe de « succéder » est invariable).
        for ending in ("es", "s", "e"):
            if word.lower().endswith(ending) and lex.participle(word[:-len(ending)]):
                return lex.participle(word[:-len(ending)])
        return None
    # Plusieurs lemmes possibles s'ils mènent au même participe (« assit » : asseoir et assoir).
    readings = {}
    for lemma, vtyp, _ in lex.verbs(word):
        base = conj(lemma, ":Q", ":m:s")
        if base and base != word.lower() and same_sound(word, base):
            readings.setdefault(base, (lemma, vtyp))
    return next(iter(readings.values())) if len(readings) == 1 else None


def _participle_after_etre(words: list[Mot], i: int, lex: Lexique) -> int | None:
    """Indice du participe passé qui suit l'auxiliaire être en position i (adverbes sautés), ou None."""
    j = i + 1
    while j < len(words) and words[j].low in BETWEEN and j - i <= 3:
        j += 1
    if j < len(words) and participle_like(words[j].text, lex):
        return j
    return None


def _object_before(words: list[Mot], i: int, lex: Lexique) -> bool:
    """Un complément d'objet placé avant, repris par « que » : « Les questions qu'ils se sont posées », « Les
    libertés qu'elle s'est permises ». Le participe s'accorde alors avec lui : pas de règle sur le sujet."""
    for k in range(max(1, i - 3), i):
        if words[k].low in ("que", "qu'"):
            prev = words[k - 1]
            return lex.is_noun(prev.text) or prev.low in ("ce", "celle", "celles", "ceux", "celui")
    return False


def regle_pronominal_indirect(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Ils se sont parlés hier » -> « parlé » ; « Ils se sont téléphonés » -> « téléphoné »."""
    for i in range(len(words) - 2):
        if words[i].low not in REFLEXIVE or words[i + 1].low not in ETRE:
            continue
        j = _participle_after_etre(words, i + 1, lex)
        if j is None or _object_before(words, i, lex):
            continue
        word = words[j]
        lemma, _ = participle_like(word.text, lex)
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
    targets = {"elle": ":f:s", "elles": ":f:p", "ils": ":m:p", "nous": ""}
    for k, subject in enumerate(words):
        target = targets.get(subject.low)
        if target is None:
            continue
        i = k + 1
        if i < len(words) and words[i].low in ("ne", "n'"):
            i += 1
        reflexive = ("nous",) if subject.low == "nous" else ("se", "s'")
        if i + 1 >= len(words) or words[i].low not in reflexive or words[i + 1].low not in ETRE:
            continue
        j = _participle_after_etre(words, i + 1, lex)
        if j is None or _object_before(words, k, lex):
            continue
        word = words[j]
        lemma, vtyp = participle_like(word.text, lex)
        if lemma not in DIRECT_PRONOMINAL:
            continue
        nxt = words[j + 1] if j + 1 < len(words) else None
        if nxt is not None and _joined(text, word, nxt) and lemma not in ESSENTIALLY_PRONOMINAL:
            if (nxt.low in DETERMINERS or nxt.text[:1].isdigit() or lex.has(nxt.text, ":Y")
                    or (lex.is_noun(nxt.text) and not lex.has(nxt.text, ":G") and not lex.has(nxt.text, ":W"))):
                continue  # complément d'objet ou infinitif après le participe : pas d'accord avec le sujet
        if not target:
            # « Nous nous sommes promené » -> « promenés » : le pluriel, et le genre que la personne a écrit
            # (« promenée » -> « promenées »).
            if word.low.endswith("s"):
                continue
            target = ":f:p" if word.low.endswith("e") else ":m:p"
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
    r"pour|afin|avant|(?:^|[,;:]\s*|\b(?:et|mais|or|car|donc)\s+)bien|sans|à\s+moins|pourvu|condition|crainte|peur|"
    r"jusqu['’]à\s+ce|en\s+attendant)\s+qu(?:e\s+|['’])$")
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
        if re.search(r"(?i)\b(?:si\s+bien|sans\s+doute)\s+qu(?:e\s+|['’])$", before):
            continue  # « si bien que » (conséquence), « sans doute que » : à l'indicatif
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
        if any(re.search(r":S[pq]\b", tags) and who in tags for _, _, tags in readings):
            continue  # déjà au subjonctif à la bonne personne (« qu'il commençât », « qu'il y eût »)
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


# Après « que », le verbe est peut-être au subjonctif (« à ce qu'il ne sais point » -> « sache ») : la
# conjugaison à l'indicatif n'est sûre qu'après les mots qui appellent l'indicatif.
INDICATIVE_BEFORE_QUE = frozenset("""parce pense penses pensons pensez pensent pensais pensait crois croit croyons
    croyez croient croyais croyait sais sait savons savez savent savais savait dis dit disons dites disent disais
    disait vois voit voyons voyez voient voyais voyait trouve trouves trouvons trouvez trouvent trouvais trouvait
    espère espères espérons espérez espèrent sûr sûre sûrs sûres certain certaine évident vrai clair dès pendant
    alors tandis depuis maintenant ainsi vu étant tellement tant remarque constate apprends apprend est-ce""".split())
# Temps voisins à l'oreille : « il pensai » (passé simple, 1re pers.) est « pensait » ; « elle saurai » (futur)
# est « saurait ».
_SOUND_ALIKE_TENSES = {"Is": ("Is", "Iq"), "If": ("If", "K"), "Q": ("Ip",), "Y": ("Ip",)}


def _after_que_ok(words: list[Mot], k: int) -> bool:
    """Le sujet en position k ne suit pas un « que » qui appellerait le subjonctif. « ce que tu en penses » est
    à l'indicatif, pas « à ce que » (« veiller à ce que tu viennes »)."""
    if k == 0 or words[k - 1].low not in ("que", "qu'"):
        return True
    if k >= 2 and words[k - 2].low == "ce":
        return k < 3 or words[k - 3].low != "à"
    return k >= 2 and words[k - 2].low in INDICATIVE_BEFORE_QUE


def _plural_subject_start(text: str, words: list[Mot], k: int, lex: Lexique) -> bool:
    """« Les femmes | porte » : groupe nominal pluriel en tête de proposition, juste avant le mot k."""
    return (2 <= k < len(words) and words[k - 2].low in PLURAL_DETERMINERS and _plural_subject_before(words, k, lex)
            and _joined(text, words[k - 2], words[k - 1]) and _joined(text, words[k - 1], words[k])
            and (k == 2 and not text[:words[0].start].strip() or k > 2 and re.search(
                r"[.!?;:,]\s*$", text[words[k - 3].end:words[k - 2].start]) is not None
                 or k > 2 and words[k - 3].low in ("et", "mais", "car", "puis", "donc", "alors")))


_QUANTITIES = frozenset("beaucoup peu trop assez plein tant combien".split())


def _antecedent_person(words: list[Mot], k: int, lex: Lexique) -> str | None:
    """Personne du verbe après « qui » en position k : « Les gens qui pense » (:3p), « beaucoup de gens qui ».
    Seulement quand l'antécédent est sans ambiguïté : pas « le chef des employés qui » (le chef ou les
    employés ?)."""
    if k < 2:
        return None
    noun, det = words[k - 1], words[k - 2]
    gn = lex.noun_gender_number(noun.text)
    if gn is None or gn[1] not in ("s", "p") or lex.has(noun.text, ":V"):
        return None
    if det.low in ("de", "d'"):
        if k < 3 or words[k - 3].low not in _QUANTITIES:
            return None
    elif det.low not in DETERMINERS or det.low in ("du", "des") and k >= 3 and lex.is_noun(words[k - 3].text):
        return None
    elif k >= 3 and (lex.is_noun(words[k - 3].text) or words[k - 3].low in ("de", "d'")):
        return None  # « le frère de mon ami qui » : le frère ou l'ami ?
    return ":3p" if gn[1] == "p" else ":3s"


def regle_conj_sujet(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Ils mange » -> « mangent », « Il finis » -> « finit », « Tu parle » -> « parles », « il pensai » ->
    « pensait », « Les femmes porte » -> « portent » : le verbe ne s'accorde pas avec le pronom sujet. Une seule
    correction possible : la forme de la bonne personne qui se prononce comme ce qui est écrit (Grammalecte
    propose « pensa » pour « il pensai », « Il » pour « Ils mange »). « nous » et « vous » sont souvent
    compléments (« Cela vous dégoûtait ») : « vous » seulement en tête d'une question (« Vous allé bien ? »)."""
    for k, subject in enumerate(words):
        who = SUBJECTS.get(subject.low) or (":3s" if subject.low in ("ça", "cela") else None)
        if who is None and _plural_subject_start(text, words, k + 1, lex) and subject.low not in CLITICS:
            who = ":3p"
        if who is None and subject.low == "qui":
            who = _antecedent_person(words, k, lex)
        if who is None or who == ":1p":
            continue
        if who == ":2p" and not (_sentence_start(words, k, text) and re.match(r"[^.!?]*\?", text[subject.end:])):
            continue
        if not _after_que_ok(words, k) or (k > 0 and words[k - 1].low in ("et", "ou", "ni")):
            continue  # « Il ou elle ne conçoivent » : deux sujets, le pluriel est juste
        if subject.low == "on" and k > 0 and (words[k - 1].low in ("ils", "elles", "eux", "nous", "vous", "qui")
                                              or _plural_subject_before(words, k, lex)):
            continue  # « Elles on accompli », « Les enfants on recours » : « on » est « ont »
        j = k + 1
        while j < len(words) and words[j].low in CLITICS and j - k <= 3:
            j += 1
        if j >= len(words) or not _joined(text, words[j - 1], words[j]):
            continue
        verb = words[j]
        if verb.low in SUBJECTS or verb.low in DETERMINERS:
            continue
        if subject.low not in SUBJECTS and (lex.is_noun(verb.text) or lex.has(verb.text, ":A")):
            continue  # « les valeurs limites », « des petites zones » : nom ou adjectif, pas un verbe
        readings = lex.verbs(verb.text)
        if not readings and (subject.low in SUBJECTS or subject.low in ("ça", "cela")) and not lex.known(verb.text):
            # « il se sentai », « tu croyai », « J'avai » : mot inconnu, une lettre muette de moins qu'une forme de
            # la bonne personne.
            found = {c for c in (verb.text + e for e in ("s", "t", "x", "e", "es", "nt", "ent"))
                     if any(who in tags and re.search(r":(?:Ip|Iq|Is|If|K)\b", tags) for _, _, tags in lex.verbs(c))}
            if len(found) == 1:
                yield Faute(verb.start, verb.end, found.pop(), f"Le verbe s'accorde avec son sujet « {subject.text} ».",
                            "corr_conj_sujet")
            continue
        if who == ":3s" and lex.has(verb.text, ":3p"):
            if subject.low in ("il", "elle") and verb.low.endswith("nt") and all(
                    ":3p" in tags for _, _, tags in readings if re.search(r":(?:Ip|Iq|Is|If|K|Sp|Sq)\b", tags)):
                # « Elle se querellent », « Il se dédient » : le verbe est au pluriel, avec sa terminaison en
                # « -nt » écrite exprès ; c'est le « s » du pronom qui manque (Grammalecte hésite).
                yield Faute(subject.start, subject.end, match_case(subject.text, subject.low + "s"), "Le pronom "
                            "s'accorde avec le verbe au pluriel.", "corr_conj_sujet", drop=((verb.start, verb.end),))
            continue
        if not readings or any(who in tags and re.search(r":(?:Ip|Iq|Is|If|K|Sp|Sq)\b", tags) for _, _, tags in readings):
            continue
        tenses = set()
        for lemma, _, tags in readings:
            for tense in re.findall(r":(Ip|Iq|Is|If|K|Sp|Sq|Q|Y)\b", tags):
                tenses.update((lemma, t) for t in _SOUND_ALIKE_TENSES.get(tense, (tense,)))
        forms = {conj(lemma, ":" + tense, who) for lemma, tense in tenses}
        forms = {f for f in forms if f and same_sound(verb.text, f)}
        if len(forms) == 1:
            drop = ((subject.start, subject.end),) if subject.low in SUBJECTS else ()
            yield Faute(verb.start, verb.end, match_case(verb.text, forms.pop()), f"Le verbe s'accorde avec son sujet "
                        f"« {subject.text} ».", "corr_conj_sujet", drop=drop)


# ---------------------------------------------------------------------------------------------------------
# Homophones

def regle_sest(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Il ces levé » -> « Il s'est levé », « Elle sait trompée » -> « Elle s'est trompée », « tout c'est bien
    passé » -> « tout s'est bien passé »."""
    for k in range(len(words) - 3):
        subject, c, est = words[k], words[k + 1], words[k + 2]
        if (subject.low in ("tout", "rien", "personne", "chacun", "cela", "ça") and c.low == "c'" and est.low == "est"
                and c.end == est.start and _joined(text, subject, c)):
            j = _participle_after_etre(words, k + 2, lex)
            if j is not None and lex.participle(words[j].text):
                yield Faute(c.start, est.end, "s'est", "« s'est » (verbe pronominal au passé composé) devant un "
                            "participe passé.", "corr_sest", drop=((words[j].start, words[j].end),))
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


def _coordinated_subject_before(words: list[Mot], i: int, lex: Lexique) -> bool:
    """« Sa mère et son père | », « Paul et sa sœur | » : deux groupes coordonnés par « et » juste avant le mot i."""
    if i < 4 or words[i - 2].low not in DETERMINERS or not lex.is_noun(words[i - 1].text):
        return False
    k = i - 3
    if words[k].low != "et":
        return False
    first = words[k - 1]
    return (lex.is_noun(first.text) and k >= 2 and words[k - 2].low in DETERMINERS) or (
        first.text[:1].isupper() and lex.has(first.text, ":M"))


_OBJECT_DETERMINERS = frozenset("""un une des du deux trois quatre cinq six sept huit neuf dix plusieurs aucun aucune
    mon ma mes ton ta tes son sa ses notre nos votre vos""".split())


def regle_son_on_pluriel(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Mes parents son partis » -> « sont » ; « Les enfants on faim » -> « ont ». Grammalecte propose « parti »
    ou « feint » pour le mot d'après : ces remarques sont écartées."""
    for i in range(1, len(words) - 1):
        word, nxt = words[i], words[i + 1]
        if (word.low == "son" and words[i - 1].low in ("se", "s'") and _joined(text, words[i - 1], word)
                and participle_like(nxt.text, lex) and not lex.is_noun(nxt.text)):
            # « Elles se son succédé » : après « se », « son » ne peut être que « sont ».
            yield Faute(word.start, word.end, "sont", "« sont » (verbe être) après « se ».", "corr_son_on_pluriel",
                        drop=((nxt.start, nxt.end),))
            continue
        if word.low not in ("son", "on") or not _joined(text, words[i - 1], word) or not (
                _plural_subject_before(words, i, lex) or _coordinated_subject_before(words, i, lex)):
            continue
        k = i + 1
        while k < len(words) - 1 and words[k].low in ("très", "trop", "si", "bien", "tous", "toutes", "déjà", "pas"):
            k += 1
        after = words[k]
        if word.low == "son":
            ok = (lex.has(after.text, ":Q") or lex.has(after.text, ":A")) and not lex.has(after.text, ":G")
            fix, msg = "sont", "« sont » (verbe être) : le sujet est pluriel."
        else:
            # « Mes voisins on un chien » : « on » sujet ne peut pas précéder un déterminant (« leur » mis à part :
            # « Les enfants on leur donne… » est une virgule oubliée).
            ok = lex.has(after.text, ":Q") or after.low in AVOIR_NOUNS or after.low in _OBJECT_DETERMINERS
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
# Ce qui suit un « à » préposition : « à la fête », « à mon frère », « à Paris », « à 8 heures ».
_AFTER_PREPOSITION_A = frozenset("""la l' le les un une mon ma mes ton ta tes son sa ses notre nos votre vos leur leurs
    ce cet cette ces chaque""".split())
_QUANTITY_DE = frozenset("beaucoup peu plein trop assez énormément tant pas point plus moins".split())
_SUBJECT_WORDS = frozenset(SUBJECTS) | {"c'", "ce", "ça", "cela", "qui"}


def _verb_and_object_before(text: str, words: list[Mot], i: int, lex: Lexique) -> bool:
    """Le mot i suit-il « sujet + verbe + complément d'objet » : « Il y a du monde | », « Elle mange une
    pomme | », « Je n'ai pas de voiture | », « Il a vu le chat | » ? Le complément est déjà là : le « a » qui
    suit ne peut pas être le verbe avoir."""
    j = i - 1
    if j >= 1 and lex.has(words[j].text, ":A") and not lex.is_noun(words[j].text):
        j -= 1  # adjectif après le nom
    if j < 2 or not lex.is_noun(words[j].text) or words[j].text[:1].isupper():
        return False
    k = j - 1
    if words[k].low not in DETERMINERS | {"du", "de", "d'", "des"}:
        return False
    if words[k].low in ("de", "d'"):
        if words[k - 1].low not in _QUANTITY_DE:
            return False
        k -= 1
    v = k - 1
    if v < 1 or not all(_joined(text, words[n], words[n + 1]) for n in range(v, i)):
        return False
    verb = words[v]
    if lex.participle(verb.text) and not lex.finite_verb(verb.text):
        v -= 1  # « a vu le chat » : l'auxiliaire avant le participe
        if v < 1 or (words[v].low not in AVOIR and words[v].low not in ETRE) or not _joined(text, words[v], verb):
            return False
    elif not lex.finite_verb(verb.text) or verb.low in DETERMINERS or verb.low in CLITICS:
        return False
    s = v - 1
    while s >= 0 and words[s].low in CLITICS and v - s <= 3:
        s -= 1
    return s >= 0 and words[s].low in _SUBJECT_WORDS and _joined(text, words[s], words[s + 1])


_HOUR_UNITS = frozenset("heure heures h".split())


def _hour_at(words: list[Mot], j: int) -> bool:
    """Une heure commence au mot `j` (« 21 heures », « 8 h », « 21h30 ») et n'est pas une durée (« 21 heures de
    retard »)."""
    if re.fullmatch(r"\d{1,2}h(?:\d{2})?", words[j].low):
        k = j + 1
    elif j + 1 < len(words) and words[j].low.isdigit() and words[j + 1].low in _HOUR_UNITS:
        k = j + 2
    else:
        return False
    return k >= len(words) or words[k].low not in ("de", "d'", "d’")


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
            elif (nxt.text[:1].isdigit() and _joined(text, word, nxt) and lex.finite_verb(prev.text)
                  and prev.low not in AVOIR and prev.low not in ETRE and _hour_at(words, i + 1)):
                fix = True  # « Le chien qui aboie a 21 heures » (mais « Le train a 21 heures de retard »)
            elif (nxt.low in _AFTER_PREPOSITION_A or nxt.text[:1].isdigit()) and _joined(text, word, nxt):
                # « Il y a du monde a la fête » ; « nuire a un résultat » (après un infinitif, « a » n'a pas de sujet)
                fix = _verb_and_object_before(text, words, i, lex) or (
                    lex.has(prev.text, ":Y") and prev.low not in ("avoir", "être") and (not lex.is_noun(prev.text) or (
                        i >= 2 and (lex.finite_verb(words[i - 2].text) or words[i - 2].low in ("pour", "sans", "de", "d'")))))
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
        pronoun = prev.low in ("il", "elle", "on", "qui")
        # Nom sujet : seulement en tête de proposition (« Le film à commencé ») ; « l'incapacité à trouvé un
        # partenaire » est « à trouver », « un moteur à cylindré fixe » est « à cylindrée ».
        noun = (i >= 2 and words[i - 2].low in ("le", "la", "l'", "un", "une", "ce", "cet", "cette", "mon", "ma", "ton",
                                                 "ta", "son", "sa") and lex.is_noun(prev.text)
                and (i == 2 or re.search(r"[.!?;:,]\s*$|^\s*$", text[:words[i - 2].start]) is not None
                     or words[i - 3].low in ("et", "mais", "car", "puis", "donc")))
        if not (pronoun or noun):
            continue
        after = words[i + 2] if i + 2 < len(words) and _joined(text, nxt, words[i + 2]) else None
        if noun and after is not None and (lex.has(after.text, ":A") or lex.is_noun(after.text)) and not (
                after.low in DETERMINERS or after.low in PREPOSITIONS):
            continue  # « à cylindré fixe » : un groupe nominal suit, pas un complément du verbe
        part = lex.participle(nxt.text)
        if noun and part and len(part[1]) > 2 and part[1][2] != "i" and (
                after is None or after.low not in DETERMINERS and after.low not in CLITICS):
            continue  # « la tâche à effectué, il s'en foutait » : « à effectuer » (verbe sans emploi intransitif)
        if nxt.low in AVOIR_NOUNS or (part and (pronoun or not lex.is_noun(nxt.text))
                                      and nxt.low not in ("découvert", "couvert")):
            yield Faute(word.start, word.end, "a", "« a » (verbe avoir) après le sujet.", "corr_a_verbe",
                        drop=((nxt.start, nxt.end),))


_SUBORDINATORS = frozenset("que qu' qui dont où quand si lorsque lorsqu' puisque puisqu' parce comme et mais ou car".split())


def _verb_in(words: list[Mot], lex: Lexique) -> bool:
    """Un verbe conjugué parmi ces mots : pas un nom qui a aussi une lecture verbale (« la tâche », « le linge »),
    sauf après un pronom sujet ou complément (« il tâche », « nous le linge » non : « le » est un déterminant)."""
    pronouns = (set(SUBJECTS) | CLITICS) - DETERMINERS
    for n, w in enumerate(words):
        prev = words[n - 1].low if n > 0 else ""
        if lex.finite_verb(w.text) and (not lex.is_noun(w.text) or prev in pronouns or (
                (w.low in ETRE or w.low in AVOIR) and prev not in DETERMINERS)):  # « est » : verbe, sauf « l'est »
            return True
    return False


def regle_a_participe(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« à » + participe en -é. Sans verbe conjugué dans la proposition, « à » est le verbe avoir : « Le ministre de
    l'Intérieur à estimé que… » -> « a estimé ». Après un verbe conjugué, le participe est l'infinitif mal écrit :
    « nous avons à cherché des issues » -> « à chercher », « Il ne reste qu'à démontré » -> « démontrer »."""
    for i in range(1, len(words) - 1):
        a, part = words[i], words[i + 1]
        if a.low != "à" or not _joined(text, words[i - 1], a) or not _joined(text, a, part) or not part.low.endswith("é"):
            continue
        reading = lex.participle(part.text)
        if not reading or not reading[1].startswith("1") or lex.is_noun(part.text):
            continue
        k = i - 1  # début de la proposition
        while k > 0 and words[k].low not in _SUBORDINATORS and not re.search(r"[.!?;:,()«»]", text[words[k - 1].end:words[k].start]):
            k -= 1
        before = words[k:i]
        m = i + 2  # fin de la proposition principale, après le participe
        while m < len(words) and words[m].low not in _SUBORDINATORS and not re.search(r"[.!?;:,()«»]", text[words[m - 1].end:words[m].start]):
            m += 1
        after = words[i + 2:m]
        nxt = words[i + 2] if i + 2 < len(words) and _joined(text, part, words[i + 2]) else None
        sentence_end = re.match(r"\s*(?:[.!?…]|$)", text[part.end:part.end + 3]) is not None
        if words[i - 1].low == "qu'" or _verb_in(before, lex):
            # « Il ne nous reste qu'à démontré », « nous avons à cherché » : l'infinitif
            inf = part.text[:-1] + "er"
            if lex.has(inf, ":Y"):
                yield Faute(part.start, part.end, inf, "Infinitif après « à ».", "corr_a_participe",
                            drop=((a.start, a.end),))
        elif (before and lex.is_noun(words[i - 1].text) and not _verb_in(after, lex)
              and words[i - 1].low not in ("rien", "tout", "personne", "chose", "beaucoup", "peu", "trop", "assez") and (
                (nxt is None and sentence_end and len(reading[1]) > 2 and reading[1][2] == "i")
                or (nxt is not None and (nxt.low in DETERMINERS or nxt.low in PREPOSITIONS or nxt.low in ("que", "qu'"))))):
            # « Le film à commencé. », « Le ministre à estimé que » ; pas « la tâche à effectué, il… » (« à
            # effectuer »), ni « un moteur à cylindré fixe ».
            yield Faute(a.start, a.end, "a", "« a » (verbe avoir) : la phrase n'a pas d'autre verbe.", "corr_a_verbe",
                        drop=((part.start, part.end),))


def veto_a_verbe(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Je pense que le chat a la grippe », « Paul a la clé » : un sujet en tête de proposition, puis « a » et un
    déterminant, sans autre verbe dans la proposition : « a » est le verbe avoir (LanguageTool proposait « à »).
    Pas « Le voyage a la mer était long » (« à la mer » : « était » est le verbe)."""
    for i in range(1, len(words) - 1):
        a, nxt = words[i], words[i + 1]
        if a.low != "a" or nxt.low not in _AFTER_PREPOSITION_A or not (_joined(text, words[i - 1], a) and _joined(text, a, nxt)):
            continue
        prev = words[i - 1]
        if prev.low in ("il", "elle", "on", "qui") or (prev.text[:1].isupper() and lex.has(prev.text, ":M")):
            s = i - 1
        elif i >= 2 and words[i - 2].low in DETERMINERS and lex.is_noun(prev.text):
            s = i - 2
        else:
            continue
        if not (s == 0 or _sentence_start(words, s, text) or words[s - 1].low in _CLAUSE_START_WORDS):
            continue
        m = i + 2
        while m < len(words) and words[m].low not in _SUBORDINATORS and not re.search(r"[.!?;:,()«»]", text[words[m - 1].end:words[m].start]):
            m += 1
        if not _verb_in(words[i + 2:m], lex):
            yield Faute(a.start, a.end, None, "", "corr_veto_a_verbe")


def veto_sujets_coordonnes(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Ni lui ni elle ne viendront », « Paul et elle partent » : deux sujets coordonnés, le verbe au pluriel est
    juste (les deux moteurs voulaient « elles »)."""
    for k in range(1, len(words) - 1):
        pron = words[k]
        if words[k - 1].low not in ("et", "ou", "ni") or pron.low not in ("il", "elle", "lui", "eux", "elles", "ils",
                                                                          "moi", "toi", "nous", "vous"):
            continue
        j = k + 1
        while j < len(words) and words[j].low in CLITICS and j - k <= 3:
            j += 1
        if j < len(words) and re.search(r":[123]p", " ".join(t for _, _, t in lex.verbs(words[j].text))):
            yield Faute(pron.start, pron.end, None, "", "corr_veto_sujets")
            yield Faute(words[j].start, words[j].end, None, "", "corr_veto_sujets")


def veto_a_ete_adjectif(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« un climat tempéré à été chaud » (climat à été chaud) : Grammalecte voudrait « a été »."""
    for i in range(1, len(words) - 1):
        if words[i].low == "à" and words[i + 1].low == "été" and lex.has(words[i - 1].text, ":A") and not (
                lex.is_noun(words[i - 1].text) and not lex.has(words[i - 1].text, ":Q")):
            yield Faute(words[i].start, words[i].end, None, "", "corr_veto")


# « Je ne sais pas ou il est » : savoir, ignorer, oublier, se demander… puis « ou » et une proposition.
_WHERE_VERBS = frozenset("savoir ignorer oublier demander indiquer expliquer deviner".split())
_WHERE_IMPERATIVE_RE = re.compile(r"(?i)^(?:dis|dites|montre|montrez|explique|expliquez|indique|indiquez|rappelle|"
                                  r"rappelez|demande|demandez)-(?:moi|nous|lui|leur)$")
# Verbes qui disent un lieu : « Je ne sais plus ou j'ai mis mes clés » (même sujet que « sais »).
_LOCATION_VERBS = frozenset("""être aller habiter vivre mettre ranger poser laisser garer trouver cacher partir venir
    dormir travailler passer naître rester""".split())
_SKIPPED_BEFORE_OU = frozenset("pas plus jamais vraiment bien toujours même exactement trop point".split())


def _clause_after(text: str, words: list[Mot], i: int, lex: Lexique) -> tuple[str, str] | None:
    """(sujet, lemme du verbe) de la proposition qui commence au mot i : pronom sujet, pronoms compléments
    (pas de négation), verbe conjugué ; ou verbe être, aller, se trouver directement (« ou est la gare »)."""
    if i >= len(words):
        return None
    if words[i].low in SUBJECTS:
        subject, j = words[i].low.replace("j'", "je"), i + 1
        while j < len(words) and words[j].low in CLITICS - {"ne", "n'"} and j - i <= 3:
            j += 1
        if j >= len(words) or not _joined(text, words[j - 1], words[j]):
            return None
    else:
        subject, j = "", i
    verb = words[j]
    readings = [(lemma, tags) for lemma, _, tags in lex.verbs(verb.text) if re.search(r":(?:Ip|Iq|If|K|Is|Sp)\b", tags)]
    if not readings:
        return None
    lemma = readings[0][0]
    if lemma in ("avoir", "être") and j + 1 < len(words) and lex.participle(words[j + 1].text):
        lemma = lex.participle(words[j + 1].text)[0]  # « j'ai mis » : mettre
    if not subject and lemma not in ("être", "aller", "trouver"):
        return None
    return subject, lemma


def regle_ou_lieu(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Je ne sais pas ou il est » -> « où », « Je me demande ou il habite », « Dis-moi ou tu es », « dans la
    mesure ou », « partout ou il va ». Pas « Je ne sais pas ou je ne veux pas » (négation : conjonction), ni
    « Tu sais ou tu crois savoir » (même sujet, verbe sans idée de lieu)."""
    for i in range(1, len(words) - 1):
        ou = words[i]
        if ou.low != "ou" or not _joined(text, words[i - 1], ou):
            continue
        clause = _clause_after(text, words, i + 1, lex)
        if clause is None:
            continue
        before = " ".join(w.low for w in words[max(0, i - 3):i])
        if before.endswith(("dans la mesure", "partout")):
            yield Faute(ou.start, ou.end, "où", "« où » (lieu, moment) : « ou » sans accent veut dire « ou bien ».",
                        "corr_ou_lieu")
            continue
        j = i - 1
        while j > 0 and words[j].low in _SKIPPED_BEFORE_OU:
            j -= 1
        main = words[j]
        if _WHERE_IMPERATIVE_RE.match(main.text):
            main_subject = "tu"
        else:
            if not any(lemma in _WHERE_VERBS for lemma, _, _ in lex.verbs(main.text)):
                continue
            s = j - 1
            while s >= 0 and (words[s].low in CLITICS or words[s].low in AVOIR) and j - s <= 4:
                s -= 1
            main_subject = words[s].low.replace("j'", "je") if s >= 0 and words[s].low in SUBJECTS else ""
        subject, lemma = clause
        if subject and subject == main_subject and lemma not in _LOCATION_VERBS:
            continue  # « Je sais ou je crois savoir » : « ou bien »
        yield Faute(ou.start, ou.end, "où", "« où » (lieu) : « ou » sans accent veut dire « ou bien ».", "corr_ou_lieu")


def regle_peu(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Il en reste peut. » -> « peu », « sous peut », « depuis peut », « il y a peut. » : « peut » (verbe pouvoir)
    est impossible après un autre verbe conjugué ou une préposition, en fin de groupe."""
    for i in range(1, len(words)):
        word = words[i]
        if word.low not in ("peut", "peux") or not _joined(text, words[i - 1], word):
            continue
        end = re.match(r"\s*(?:[.!?,;:…)]|$)", text[word.end:word.end + 3]) is not None
        de_after = re.match(r"\s+(?:de\b|d['’])", text[word.end:word.end + 4], re.I) is not None
        prev = words[i - 1]
        before = " ".join(w.low for w in words[max(0, i - 3):i])
        if prev.low == "sous" or (prev.low == "ici" and before.endswith("d' ici")):
            fix = end or i + 1 >= len(words)
        elif prev.low == "depuis" or before.endswith("il y a"):
            fix = end or de_after
        else:
            # « Le chat peut. » : un nom sujet ; « Il en reste peut » : « reste » verbe, après un pronom.
            verb = lex.finite_verb(prev.text) and (not lex.is_noun(prev.text) or (
                i >= 2 and (words[i - 2].low in CLITICS or words[i - 2].low in SUBJECTS)))
            fix = (end and verb and prev.low not in SUBJECTS and prev.low not in CLITICS
                   and prev.low not in ("qui", "veut", "que", "qu'"))
        if fix:
            yield Faute(word.start, word.end, match_case(word.text, "peu"), "« peu » (petite quantité) : « peut » est le "
                        "verbe pouvoir.", "corr_peu")


def regle_pret(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Je suis près à partir » -> « prêt », « Elle est près à tout » -> « prête » : « près à » suivi d'un
    infinitif. L'accord suit le sujet (Grammalecte propose toujours « prêt » ou « prêts »)."""
    forms = {("m", "s"): "prêt", ("f", "s"): "prête", ("m", "p"): "prêts", ("f", "p"): "prêtes"}
    pronouns = {"je": ("m", "s"), "j'": ("m", "s"), "tu": ("m", "s"), "il": ("m", "s"), "on": ("m", "s"),
                "elle": ("f", "s"), "ils": ("m", "p"), "elles": ("f", "p")}
    for i in range(2, len(words) - 2):
        word = words[i]
        if word.low != "près" or words[i + 1].low != "à" or not _joined(text, word, words[i + 1]):
            continue
        nxt = words[i + 2]
        if not (lex.has(nxt.text, ":Y") or nxt.low == "tout"):
            continue
        j = i - 1
        while j > 0 and words[j].low in BETWEEN:
            j -= 1
        if words[j].low not in ETRE:
            continue
        s = j - 1
        if s >= 0 and words[s].low in ("ne", "n'"):
            s -= 1
        if s < 0:
            continue
        agreement = pronouns.get(words[s].low)
        if agreement is None and s >= 1 and words[s - 1].low in DETERMINERS:
            gn = lex.noun_gender_number(words[s].text)
            if gn is not None and gn[1] in ("s", "p"):
                # Nom épicène (« les enfants », « l'élève ») : le déterminant dit le genre, sinon le masculin.
                gender = gn[0] if gn[0] != "e" else ("f" if words[s - 1].low in ("la", "une", "cette", "ma", "ta", "sa")
                                                    else "m")
                agreement = (gender, gn[1])
        if agreement is None:
            continue
        yield Faute(word.start, word.end, match_case(word.text, forms[agreement]), "« prêt à » (disposé à) : « près » "
                    "veut dire « à côté ».", "corr_pret")


def regle_ca(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Sa fait longtemps » -> « Ça fait », « sa me plaît » -> « ça » : « sa » (déterminant féminin) devant un
    verbe qui n'est pas un nom féminin, ou devant un pronom. Grammalecte proposait « Son fait » ou « fée ».
    Pas « sa marche » (nom féminin), ni devant être (« Sa était » est plutôt « C'était »)."""
    for i in range(len(words) - 1):
        w, nxt = words[i], words[i + 1]
        if w.low != "sa" or not _joined(text, w, nxt) or nxt.low in ETRE or w.text.isupper():
            continue  # « la SA ne joue plus » : un sigle
        if i > 0 and words[i - 1].low in DETERMINERS:
            continue
        if nxt.low in ("me", "m'", "te", "t'", "nous", "vous", "lui", "leur", "y", "en", "ne", "n'"):
            ok = True
        else:
            feminine = any(re.search(r"/:(?:N|A)(?::A)?:[fe]:[si]", m) for m in lex.morph(nxt.text))
            ok = lex.finite_verb(nxt.text) and not feminine and not lex.has(nxt.text, ":W")
        if ok:
            yield Faute(w.start, w.end, match_case(w.text, "ça"), "« ça » (cela) devant un verbe : « sa » est le "
                        "déterminant (« sa maison »).", "corr_ca", drop=((nxt.start, nxt.end),))


def regle_la_demonstratif(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Ces gens la sont bizarres » -> « gens-là », « ce jour la, il pleuvait » -> « ce jour-là » : après
    « ce, cet, cette, ces » et un nom, « la » devant une ponctuation ou devant être et avoir (le pronom « la »
    s'élide : « l'a »)."""
    for i in range(2, len(words)):
        la, noun, det = words[i], words[i - 1], words[i - 2]
        if la.low != "la" or det.low not in ("ce", "cet", "cette", "ces") or not lex.is_noun(noun.text):
            continue
        if not (_joined(text, det, noun) and _joined(text, noun, la)):
            continue
        nxt = words[i + 1] if i + 1 < len(words) else None
        end = re.match(r"\s*(?:[.!?,;:…)]|$)", text[la.end:la.end + 3]) is not None
        if end or (nxt is not None and _joined(text, la, nxt) and (nxt.low in ETRE or nxt.low in AVOIR)):
            yield Faute(noun.end, la.end, "-là", "« -là » (démonstratif) : « ces gens-là », « ce jour-là ».",
                        "corr_demonstratif_la")


def regle_leurs(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Leur enfants sont grands » -> « Leurs enfants » : « leur » devant un nom pluriel, qui n'est pas un verbe.
    Grammalecte hésitait entre « Leurs enfants » et « Leur enfant »."""
    for i in range(len(words) - 1):
        w, noun = words[i], words[i + 1]
        if w.low != "leur" or not _joined(text, w, noun) or lex.has(noun.text, ":V"):
            continue
        gn = lex.noun_gender_number(noun.text)
        if gn is not None and gn[1] == "p":
            yield Faute(w.start, w.end, match_case(w.text, "leurs"), "« leurs » devant un nom pluriel.", "corr_leurs",
                        drop=((noun.start, noun.end),))


# Locutions figées mal écrites, que Grammalecte ne signale pas (« en vacance ») ou corrige mal. Le groupe
# « f » de chaque motif est la partie remplacée.
LOCUTIONS = (
    # « Je suis en vacance », « partis en vacance », « bonnes vacance » ; pas « Cette place est en vacance »
    # (un poste libre).
    (re.compile(r"(?i)(?:\b(?:je|j['’]|tu|il|elle|on|nous|vous|ils|elles)\s+(?:\w+\s+){0,2}en|\b(?:partir|pars|part|partons"
                r"|partez|partent|parti|partis|partie|parties|aller|vais|vas|va|allons|allez|vont|allé|allés|allée|allées"
                r"|bonnes|grandes|petites|les|des|mes|tes|ses|nos|vos|leurs)\s+(?:en\s+)?)\s*(?P<f>vacance)\b(?!s)"),
     lambda f: f + "s", "« vacances » (congés) s'écrit au pluriel."),
    (re.compile(r"(?i)\bsans\s+(?P<f>doutes)\b"), lambda f: f[:-1], "Locution « sans doute »."),
    (re.compile(r"(?i)\b(?P<f>dores)\s+et\s+déjà\b"), lambda f: f[0] + "'" + f[1:], "Locution « d'ores et déjà »."),
    (re.compile(r"(?i)\bau\s+(?P<f>fur\s+à)\s+mesure\b"), lambda f: f[:3] + " et à", "Locution « au fur et à mesure »."),
    (re.compile(r"(?i)\b(?P<f>tout\s+suite)\b"), lambda f: f[:4] + " de suite", "Locution « tout de suite »."),
    (re.compile(r"(?i)\b(?P<f>enfin)\s+de\s+compte\b"), lambda f: f[:2] + " fin", "Locution « en fin de compte »."),
    (re.compile(r"(?i)\best-ce(?P<f>-)qu(?:e\b|['’])"), lambda f: " ", "« est-ce que » : pas de trait d'union avant "
     "« que »."),
    (re.compile(r"(?i)\b(?P<f>bonne)\s+app[ée]tit\b"), lambda f: f[:3], "« Bon appétit » : « appétit » est masculin."),
    (re.compile(r"(?i)\bjusqu['’](?P<f>a)\b"), lambda f: "à", "« jusqu'à » (préposition)."),
    (re.compile(r"(?i)\b(?P<f>tous)\s+(?:ce|ceci|cela|ça)\b"), lambda f: f[:3] + "t", "« tout ce que » : « tout » "
     "(neutre) devant « ce »."),
    (re.compile(r"(?i)\b(?P<f>et)-(?:ce|il|elle|on|ils|elles)\b"), lambda f: f[0] + "st", "Inversion : « est-ce », "
     "« est-il » (verbe être)."),
    # « Cela lui a fait du tord » : après un article, seul le nom « tort » a sa place (« tord » vient de tordre).
    # Les autres « avoir tord », « à tord », « donner tord » : règle de Grammalecte, sûre (voir checker._SOLO_RULES).
    (re.compile(r"(?i)\b(?:du|des)\s+(?P<f>tords?)\b"), lambda f: f[:3] + "t" + f[4:], "Le nom « tort » (« faire du "
     "tort ») ; « tord » vient du verbe tordre."),
)


def regle_locutions(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    for pattern, fix, message in LOCUTIONS:
        for m in pattern.finditer(text):
            yield Faute(m.start("f"), m.end("f"), fix(m.group("f")), message, "corr_locution")


def regle_s_en(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Il sans va » -> « Il s'en va », « Elle sans fiche » -> « s'en » : « sans » entre un pronom sujet et un
    verbe. Grammalecte proposait « sent »."""
    for i in range(1, len(words) - 1):
        w, nxt = words[i], words[i + 1]
        if (w.low == "sans" and words[i - 1].low in ("il", "elle", "on", "ils", "elles")
                and _joined(text, words[i - 1], w) and _joined(text, w, nxt) and lex.finite_verb(nxt.text)
                and not lex.is_noun(nxt.text) and nxt.low not in DETERMINERS):
            yield Faute(w.start, w.end, "s'en", "« s'en » (se + en) devant un verbe.", "corr_s_en")


def regle_t_euphonique(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Y a t'il » -> « Y a-t-il », « Que fait t'il » -> « Que fait-il » : le « t » de liaison s'écrit entre
    deux traits d'union, et seulement si le verbe finit par une voyelle (Grammalecte proposait « fait-t-il »)."""
    for m in re.finditer(r"(?i)(?<=\w)(\s+t['’]|\s*-t['’]|\s+-t-|\s*-\s*t\s*-?)(?=(?:il|elle|on|ils|elles)\b)", text):
        verb = text[:m.start()].rsplit(None, 1)[-1] if text[:m.start()].strip() else ""
        if not verb or not lex.finite_verb(re.sub(r"\W+$", "", verb)):
            continue
        fix = "-" if verb[-1:].lower() in "td" else "-t-"
        if m.group(0) != fix:
            yield Faute(m.start(), m.end(), fix, "Inversion du sujet : « a-t-il », « fait-il » (le « t » de liaison "
                        "seulement après une voyelle).", "corr_t_euphonique", Category.TYPOGRAPHY)


def regle_il_faut(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Ils faut partir » -> « Il faut » : falloir ne se conjugue qu'avec « il »."""
    for i in range(len(words) - 1):
        if words[i].low == "ils" and words[i + 1].low in ("faut", "fallait", "faudra", "faudrait", "fallut"):
            yield Faute(words[i].start, words[i].end, match_case(words[i].text, "il"), "« il faut » : falloir ne se "
                        "conjugue qu'avec « il ».", "corr_il_faut", drop=((words[i + 1].start, words[i + 1].end),))


def regle_m_a(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Elle mes donné un livre » -> « Elle m'a donné », « Il tes appelé » -> « Il t'a appelé » : « mes » ou
    « tes » entre un pronom sujet et un participe passé (Grammalecte proposait « donnés »)."""
    aux = {"il": "a", "elle": "a", "on": "a", "qui": "a", "tu": "as", "ils": "ont", "elles": "ont"}
    for i in range(1, len(words) - 1):
        w, nxt, subject = words[i], words[i + 1], words[i - 1]
        if w.low not in ("mes", "tes") or subject.low not in aux or not (_joined(text, subject, w) and _joined(text, w, nxt)):
            continue
        if lex.participle(nxt.text) and not lex.is_noun(nxt.text) and not lex.finite_verb(nxt.text):
            if w.low == "tes" and subject.low == "tu":
                continue
            yield Faute(w.start, w.end, w.low[0] + "'" + aux[subject.low], f"« {w.low[0]}'{aux[subject.low]} » (pronom "
                        "et auxiliaire avoir) devant un participe passé.", "corr_m_a", drop=((nxt.start, nxt.end),))


_INFINITIVE_VERBS = frozenset("""vais vas va allons allez vont allais allait allions alliez allaient irai iras ira irons
    irez iront irais irait aller peux peut pouvons pouvez peuvent pouvais pouvait pourrai pourra pourrait pouvoir veux
    veut voulons voulez veulent voulais voulait voudrais voudrait vouloir dois doit devons devez doivent devais devait
    devrais devrait devoir faut fallait faudra faudrait sais sait savons savez savent""".split())


def regle_infinitif(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« vous allez la lui donné » -> « donner », « Tu peux le laissé » -> « laisser » : après aller, pouvoir,
    vouloir, devoir, falloir, savoir et au moins un pronom complément, le participe en -é est l'infinitif mal écrit
    (sans pronom, « Je vais fatigué au travail » est juste)."""
    for i in range(len(words) - 2):
        if words[i].low not in _INFINITIVE_VERBS:
            continue
        j = i + 1
        while j < len(words) and words[j].low in CLITICS - {"ne", "n'"} and j - i <= 3:
            j += 1
        if j == i + 1 or j >= len(words) or not all(_joined(text, words[n], words[n + 1]) for n in range(i, j)):
            continue
        w = words[j]
        part = lex.participle(w.text)
        if part and w.low.endswith("é") and part[1].startswith("1") and not lex.is_noun(w.text):
            inf = w.text[:-1] + "er"
            if lex.has(inf, ":Y"):
                yield Faute(w.start, w.end, inf, "Infinitif après « " + words[i].text + " ».", "corr_infinitif")


def regle_avoir_participe(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« je les ai mit au frigo » -> « mis », « Il a prit le train » -> « pris » : après l'auxiliaire avoir, un
    passé simple qui se prononce comme le participe (Grammalecte ne signale pas toujours « les ai mit »).
    Accord avec « les » ou « la » placés avant."""
    for i in range(1, len(words) - 1):
        aux, word = words[i], words[i + 1]
        if aux.low not in AVOIR or not _joined(text, aux, word) or lex.participle(word.text) or not lex.known(word.text):
            continue
        prev = words[i - 1].low
        if prev not in SUBJECTS and prev not in CLITICS and prev not in ("qui", "j'"):
            continue
        if lex.is_noun(word.text) or lex.has(word.text, ":A"):
            continue
        reading = participle_like(word.text, lex)
        if reading is None:
            continue
        target = {"les": ":m:p", "la": ":f:s"}.get(prev, ":m:s")
        form = conj(reading[0], ":Q", target)
        if form and form != word.low:
            yield Faute(word.start, word.end, form, "Participe passé après l'auxiliaire avoir.", "corr_avoir_participe")


def regle_la_article(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« vingt mille lieues sous là mer » -> « la mer », « dans là maison » -> « la maison » : « là » devant un nom
    féminin singulier qui n'est pas aussi un verbe (« Là commence la forêt »). Pas après être ou rester (« Il est là
    maman » : un appel)."""
    for i in range(len(words) - 1):
        w, noun = words[i], words[i + 1]
        if w.low != "là" or not _joined(text, w, noun) or noun.text[:1].isupper():
            continue
        if i > 0 and (words[i - 1].low in ETRE or words[i - 1].low.startswith("rest")):
            continue
        gn = lex.noun_gender_number(noun.text)
        if gn is not None and gn[0] in ("f", "e") and gn[1] == "s" and not lex.has(noun.text, ":V") and not re.match(
                r"(?i)[aeiouyhéèêàâîôû]", noun.text):
            yield Faute(w.start, w.end, match_case(w.text, "la"), "« la » (article) devant un nom : « là » indique le "
                        "lieu.", "corr_la_article")


_CLAUSE_START_WORDS = frozenset("et mais car donc or puis alors que qu' parce".split())


def regle_c_est(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« S'est une vertu rare » -> « C'est », « ce que s'est » -> « ce que c'est », « s'est-à-dire » ; « c'étais
    eux » -> « c'était ». « s'est » devant un déterminant, en tête de proposition (pas « Il s'est un peu calmé »)."""
    for i in range(len(words) - 1):
        a, b = words[i], words[i + 1]
        if a.low == "c'" and b.start == a.end and b.low in ("étais", "étai", "étaie"):
            yield Faute(b.start, b.end, "était", "« c'était » : le verbe s'accorde avec « ce ».", "corr_c_est")
            continue
        if a.low != "s'" or b.start != a.end or b.low not in ("est", "est-à-dire") or i + 2 > len(words):
            continue
        start = i == 0 or _sentence_start(words, i, text) or words[i - 1].low in _CLAUSE_START_WORDS
        nxt = words[i + 2] if i + 2 < len(words) else None
        end = re.match(r"\s*(?:[.!?,;:…)]|$)", text[b.end:b.end + 3]) is not None
        if b.low == "est-à-dire" or (start and end) or (start and nxt is not None and nxt.low in DETERMINERS
                                     and nxt.low not in ("de", "d'", "tout", "toute", "tous", "toutes")
                                     and not (nxt.low == "un" and i + 3 < len(words) and words[i + 3].low == "peu")):
            yield Faute(a.start, a.end, match_case(a.text, "c'"), "« c'est » (cela est) : « s'est » est le verbe "
                        "pronominal (« il s'est levé »).", "corr_c_est")


def regle_et(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Est j'ai des gens en main » -> « Et j'ai » : « est » en tête de phrase devant « je » (pas une question :
    « Est-ce que je » prend un trait d'union)."""
    for i in range(len(words) - 1):
        if words[i].low == "est" and words[i + 1].low in ("je", "j'") and (i == 0 or _sentence_start(words, i, text)):
            yield Faute(words[i].start, words[i].end, match_case(words[i].text, "et"), "« et » (conjonction).",
                        "corr_et")


# Adjectifs qui se placent avant le nom : « les petites zone », pas « plusieurs arrivées demain » (« arrivées »
# est le nom) ni « Les rieurs son pour vous ».
_BEFORE_NOUN = frozenset("""petit grand beau bel bon mauvais jeune vieux vieil nouveau nouvel gros long haut joli premier
    dernier autre même seul vrai faux meilleur pire double ancien cher pauvre propre brave certain différent divers
    nombreux prochain gentil vaste énorme immense large court bref""".split())


def _lemma_of_adjective(word: str, lex: Lexique) -> str:
    m = next((re.match(r">([^/]+)/", mm) for mm in lex.morph(word) if ":A" in mm), None)
    return m.group(1) if m else ""


def regle_pluriel_nom(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Des petites zone favorables » -> « zones » : déterminant pluriel, adjectif au pluriel, puis un nom au
    singulier. Seulement quand le pluriel du nom existe et que le mot n'est pas aussi un verbe."""
    for i in range(2, len(words)):
        det, adj, noun = words[i - 2], words[i - 1], words[i]
        if det.low not in PLURAL_DETERMINERS or not (_joined(text, det, adj) and _joined(text, adj, noun)):
            continue
        if not re.search(r":A:[mfe]:p", " ".join(lex.morph(adj.text))) or _lemma_of_adjective(adj.text, lex) not in _BEFORE_NOUN:
            continue
        gn = lex.noun_gender_number(noun.text)
        if gn is None or gn[1] != "s" or lex.has(noun.text, ":A") or lex.has(noun.text, ":W") or noun.text[:1].isupper():
            continue
        plural = noun.text + ("x" if noun.low.endswith(("au", "eau", "eu")) else "s")
        if lex.noun_gender_number(plural) is not None and lex.noun_gender_number(plural)[1] == "p":
            yield Faute(noun.start, noun.end, plural, "Le nom s'accorde avec le déterminant pluriel.", "corr_pluriel_nom")


def regle_la_lieu(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Je serai la demain » -> « là », « Il est la, » -> « là » : « la » après être, devant un mot de temps, une
    préposition ou une ponctuation (pas « Elle est la meilleure »)."""
    after_re = re.compile(r"(?i)\s*(?:[.!?,;:…)]|$)|\s+(?:demain|aujourd['’]hui|maintenant|hier|ce\s+soir|ce\s+matin|"
                          r"cet\s+après-midi|tout\s+de\s+suite|bientôt|à\b|avant\b|après\b|pour\b|depuis\b|"
                          r"dans\b|ou\b|et\b|vers\b)")
    for i in range(1, len(words)):
        w = words[i]
        if w.low == "la" and words[i - 1].low in ETRE and _joined(text, words[i - 1], w) and after_re.match(text, w.end):
            yield Faute(w.start, w.end, "là", "« là » (lieu) : « la » est l'article ou le pronom.", "corr_la_lieu")


def regle_c_est_a_dire(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« c'est a dire » ou « c'est à dire » -> « c'est-à-dire »."""
    for m in re.finditer(r"(?i)\bc['’]est[\s-]+[aà][\s-]+dire\b", text):
        if m.group(0).lower().replace("’", "'") != "c'est-à-dire":
            yield Faute(m.start(), m.end(), match_case(m.group(0), "c'est-à-dire"), "Locution « c'est-à-dire », avec "
                        "des traits d'union.", "corr_c_est_a_dire")


def regle_ni_ny(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Il ni a rien » -> « Il n'y a rien », « je ni prendrai pas le train » -> « je n'y prendrai » : « ni »
    entre un pronom sujet et un verbe."""
    for i in range(1, len(words) - 1):
        if words[i].low != "ni" or words[i - 1].low not in SUBJECTS or not _joined(text, words[i - 1], words[i]):
            continue
        nxt = words[i + 1]
        if nxt.low in AVOIR or nxt.low == "en" or (lex.finite_verb(nxt.text) and not lex.is_noun(nxt.text)
                                                   and nxt.low not in SUBJECTS and nxt.low not in CLITICS):
            yield Faute(words[i].start, words[i].end, "n'y", "« n'y » (négation et pronom « y »).", "corr_ni_ny")


def regle_prie(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Je vous pris de m'excuser » -> « Je vous prie », « Je vous en pris » -> « Je vous en prie »."""
    for m in re.finditer(r"(?i)\b(?:je|j['’])\s*(?:vous|te|t['’])\s*(?:en\s+)?(pris)\b(?=\s+(?:de\b|d['’])|\s*[.!,]|$)",
                         text):
        yield Faute(m.start(1), m.end(1), "prie", "« prier » au présent : « je vous prie ».", "corr_prie")


def regle_avoir_ete(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Il a était malade » -> « Il a été malade » ; « Ils ont étés » -> « été » (participe invariable)."""
    for i in range(1, len(words)):
        if (words[i].low in ("était", "étais", "étaient", "étés", "étée", "étées") and words[i - 1].low in AVOIR
                and _joined(text, words[i - 1], words[i])):
            yield Faute(words[i].start, words[i].end, "été", "Participe passé « été » après l'auxiliaire avoir.",
                        "corr_avoir_ete")


_CLAUSE_OPENERS = frozenset("et mais car donc or puis alors que qu' quand lorsque lorsqu' si comme parce où".split())


_SPACE_BEFORE_FINAL_DOT = re.compile(r"(?<=[\w»)\]])[ \t\u00a0\u202f]+\.(?!\.)(?=[ \t]*(?:\r?\n|$))")


def regle_espace_point_final(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Je vais à la plage . » en fin de texte ou de ligne : Grammalecte ne signale l'espace avant le point qu'au
    milieu du texte (« ... » mis à part)."""
    for m in _SPACE_BEFORE_FINAL_DOT.finditer(text):
        yield Faute(m.start(), m.end(), ".", "Pas d'espace avant un point.", "corr_espace_point", Category.TYPOGRAPHY)


def regle_nous_sommes(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Nous somme arrivés » -> « Nous sommes » : « nous » sujet, en tête de proposition (pas « Il nous somme de
    partir », où « somme » est le verbe sommer)."""
    for i in range(1, len(words) - 1):
        word = words[i]
        if word.low != "somme":
            continue
        k = i - 1
        if words[k].low in ("ne", "n'") and k > 0:
            k -= 1
        nous, nxt = words[k], words[i + 1]
        if nous.low != "nous" or not _joined(text, nous, words[k + 1]) or not _joined(text, word, nxt):
            continue
        if nxt.low in ("toute", "totale", "nulle"):
            continue  # « somme toute »
        if k == 0 or re.search(r"[.!?;:,(«]\s*$", text[:nous.start]) or words[k - 1].low in _CLAUSE_OPENERS:
            yield Faute(word.start, word.end, match_case(word.text, "sommes"), "« sommes » (verbe être) avec "
                        "« nous ».", "corr_nous_sommes")


def regle_pluriel_aux(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« des chevals » -> « chevaux » : mot inconnu en « -als » dont le pluriel en « -aux » existe (Grammalecte
    proposait « cheval » en premier)."""
    for word in words:
        if len(word.low) < 5 or not word.low.endswith("als") or lex.known(word.text):
            continue
        plural = word.text[:-3] + ("AUX" if word.text.isupper() else "aux")
        found = lex.noun_gender_number(plural.lower())
        if found is not None and found[1] in ("p", "i"):
            yield Faute(word.start, word.end, plural, f"Pluriel en « -aux » : « {plural.lower()} ».", "corr_pluriel_aux",
                        Category.SPELLING)


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
        nxt = words[j + 1]
        part = lex.participle(nxt.text)
        if part is None or lex.finite_verb(nxt.text) or nxt.low in SUBJECTS or nxt.low in CLITICS or not (
                _joined(text, subject, words[k + 1]) and _joined(text, words[j], nxt)):
            continue  # « les pronoms “je” et “tu” » : « tu » n'est pas le participe de « taire »
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
        if lex.has(noun.text, ":A") or noun.low in DETERMINERS:
            continue  # « quelles grande idiotes » : l'adjectif a le genre du nom qui suit, pas forcément le sien
        number = "p" if word.low.endswith("s") else "s"
        if gn[1] != number:
            continue  # « Quels changement » : « Quel changement » ou « Quels changements » ? On ne choisit pas.
        form = forms[gn]
        if form != word.low:
            yield Faute(word.start, word.end, match_case(word.text, form), f"« {form} » s'accorde avec « {noun.text} ».",
                        "corr_quel_accord", drop=((noun.start, noun.end),))


def regle_sont_son(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Il a pris sont manteau » -> « son manteau » : « sont » devant un nom singulier, après un verbe ou une
    préposition (jamais après un sujet)."""
    for i in range(len(words) - 1):
        word, noun = words[i], words[i + 1]
        if word.low != "sont" or not _joined(text, word, noun):
            continue
        gn = lex.noun_gender_number(noun.text)
        if gn is None or gn[1] != "s" or lex.has(noun.text, ":V") or lex.has(noun.text, ":A") or lex.has(noun.text, ":G"):
            continue
        if i == 0 or _sentence_start(words, i, text):
            # « Sont élégance est rare » : en tête de phrase, devant un nom singulier, « sont » n'a pas de sujet.
            if i + 2 < len(words) and lex.finite_verb(words[i + 2].text):
                det = "sa" if gn[0] == "f" and not re.match(r"(?i)[aeiouyhéèêàâîôû]", noun.text) else "son"
                yield Faute(word.start, word.end, match_case(word.text, det), f"« {det} » (déterminant possessif) "
                            "devant un nom.", "corr_sont_son", drop=((noun.start, noun.end),))
            continue
        prev = words[i - 1]
        if not _joined(text, prev, word):
            continue
        after_verb = prev.low in PREPOSITIONS or (lex.participle(prev.text) and prev.low not in ETRE) or (
            lex.finite_verb(prev.text) and prev.low not in ETRE and not lex.is_noun(prev.text)) or (
            lex.has(prev.text, ":Y") and not lex.is_noun(prev.text))
        if not after_verb or prev.low in SUBJECTS or (i >= 2 and words[i - 2].low in DETERMINERS and lex.is_noun(prev.text)):
            continue  # « les abrutis sont légion » : un nom sujet, « sont » est le verbe
        if any(w.low in ("que", "qu'", "qui", "dont", "où") for w in words[max(0, i - 6):i - 1]):
            continue  # « Les livres que tu m'as prêtés sont super » : fin de la relative, puis le verbe
        det = "sa" if gn[0] == "f" and not re.match(r"(?i)[aeiouyhéèêàâîôû]", noun.text) else "son"
        yield Faute(word.start, word.end, det, f"« {det} » (déterminant possessif) devant un nom.", "corr_sont_son",
                    drop=((noun.start, noun.end),))


# « l'impression qu'il a réussi », « l'idée que tu as eue » : après ces noms, « que » est souvent une
# conjonction (le contenu de l'impression), pas un pronom qui reprend le nom.
_CONTENT_NOUNS = frozenset("""impression idée sentiment fait espoir peur crainte certitude preuve nouvelle pensée opinion
    avis conviction hypothèse raison sensation illusion assurance garantie promesse annonce rumeur""".split())
# Verbes après lesquels « que » est une conjonction (« il explique que… ») : le nom d'avant n'est pas repris.
_SAYING = frozenset("""dit dis disent explique expliquent écrit écrivent raconte racontent annonce annoncent affirme
    affirment répond répondent promet promettent assure assurent montre montrent rappelle rappellent pense pensent
    crois croit croient sait sais savent vois voit voient trouve trouvent espère espèrent sent sens prétend prétendent
    jure jurent avoue avouent""".split())


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
        if gn is None or det.low not in DETERMINERS or not _joined(text, det, noun) or noun.low in _CONTENT_NOUNS:
            continue
        if q >= 3 and (words[q - 3].low in ("et", "ou", "ni") or text[words[q - 3].end:det.start].strip() == ","):
            continue  # « La vision et la philosophie que j'ai élaborées » : l'antécédent est double
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
        if any(lex.has(w.text, ":V") and w.low in _SAYING for w in words[max(0, q - 8):q - 2]):
            continue  # « Marina m'explique dans ses lettres qu'elle a échoué » : « que » conjonction
        j = k + 1
        # « que tu m'as contacté » : « m' » est le complément d'objet, pas le nom d'avant.
        while j < len(words) and words[j].low in ("ne", "n'", "lui", "leur", "y") and j - k <= 3:
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
                                                                             "durer", "courir", "dormir", "régner",
                                                                             "pouvoir", "devoir"):
            continue
        nxt = words[p + 1] if p + 1 < len(words) and _joined(text, words[p], words[p + 1]) else None
        if nxt is not None and (nxt.low in CLITICS or nxt.low in DETERMINERS or nxt.low in ("à", "de", "d'", "que", "qu'")
                                or lex.has(nxt.text, ":Y")):
            continue  # « que j'ai vu partir », « qu'il a voulu me passer », « qu'il a appris à » : pas sûr
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
    "croivent": "croient", "croyent": "croient", "voyent": "voient", "envoyent": "envoient", "faisez": "faites",
    "disez": "dites", "parceque": "parce que", "apeller": "appeler", "apeler": "appeler", "acceuil": "accueil",
    "acceuillir": "accueillir", "acceuilli": "accueilli", "dilemne": "dilemme", "plusieur": "plusieurs",
    "aujour'dhui": "aujourd'hui", "aujourd'hui'": "aujourd'hui", "quelqu'un'": "quelqu'un",
    "aurevoir": "au revoir", "apétit": "appétit", "envoit": "envoie", "rapelle": "rappelle", "rapelles": "rappelles",
    "rapellent": "rappellent", "rapeler": "rappeler", "rapeller": "rappeler", "rappeller": "rappeler",
    "occurence": "occurrence", "occurences": "occurrences",
    # « contant » existe (verbe conter), ses formes accordées non : c'est l'adjectif « content ».
    "contante": "contente", "contantes": "contentes", "contants": "contents",
}


# Adverbes de degré : « très contant », « si contant » -> « content ».
_DEGREE = frozenset("très si trop tellement assez aussi vraiment super plus moins fort bien".split())


def regle_content(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    """« Je suis contant de te voir » -> « content », « très contant » -> « content » : « contant » (du verbe conter)
    existe, Grammalecte ne dit rien ; après « être » ou un adverbe de degré, seul l'adjectif a sa place. « En
    contant une histoire » reste intact."""
    for i, w in enumerate(words):
        if w.low != "contant" or i == 0:
            continue
        j = i - 1
        while j > 0 and words[j].low in BETWEEN | _DEGREE and _joined(text, words[j], words[j + 1]):
            j -= 1
        if (_joined(text, words[i - 1], w) and (words[i - 1].low in _DEGREE
                                                or (words[j].low in ETRE and _joined(text, words[j], words[j + 1])))):
            yield Faute(w.start, w.end, match_case(w.text, "content"), "Adjectif « content » ; « contant » vient du "
                        "verbe conter.", "corr_content", Category.SPELLING)


def regle_orthographe_frequente(text: str, words: list[Mot], lex: Lexique) -> Iterable[Faute]:
    for w in words:
        fix = FREQUENT.get(w.low)
        if fix is None and w.low.endswith("ié") and len(w.low) > 4 and not lex.known(w.text):
            # « particulié » -> « particulier », « papié » -> « papier » : nom ou adjectif en -ier (pas un verbe,
            # où « -ié » serait peut-être un participe : « envié »).
            candidate = w.low[:-1] + "er"
            if lex.known(candidate) and not lex.has(candidate, ":V"):
                fix = candidate
        if fix is not None and not (w.low == "language" and lex.known(w.text)):
            yield Faute(w.start, w.end, match_case(w.text, fix), f"Orthographe : « {fix} ».", "corr_orthographe",
                        Category.SPELLING)


RULES: tuple[Callable[[str, list[Mot], Lexique], Iterable[Faute]], ...] = (
    # Participes, modes, conjugaison
    regle_pronominal_indirect, regle_pronominal_sujet, regle_subjonctif, regle_si_conditionnel, regle_conj_sujet,
    # Homophones
    regle_sest, regle_son_on_pluriel, regle_qui_on, regle_ce_se, regle_ces_cest, regle_cest_ces, regle_mais_mes,
    regle_a_accent, regle_a_verbe, regle_a_participe, regle_ou_lieu, regle_peu, regle_pret, regle_ca,
    regle_la_demonstratif, regle_leurs, regle_locutions, regle_s_en, regle_t_euphonique, regle_il_faut, regle_m_a,
    regle_infinitif, regle_avoir_participe, regle_la_article, regle_c_est, regle_et, regle_pluriel_nom, regle_la_lieu,
    regle_c_est_a_dire, regle_ni_ny, regle_prie, regle_avoir_ete, regle_nous_sommes, regle_pluriel_aux,
    regle_espace_point_final, regle_je_ai,
    regle_quel_genre, regle_sont_son,
    regle_cod_que, regle_content, regle_orthographe_frequente,
    # Vetos, en dernier : une zone déjà corrigée par une règle n'est pas déclarée juste
    veto_a_ete_adjectif, veto_a_verbe, veto_sujets_coordonnes,
)


def check(text: str, lex: Lexique, strict: bool = False) -> list[Faute]:
    """Fautes trouvées par les règles du correcteur, sans doublon de zone (la première règle l'emporte).
    `strict` : une règle en défaut lève son exception (tests) au lieu d'être ignorée."""
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
            if strict:
                raise
            continue
    return found
