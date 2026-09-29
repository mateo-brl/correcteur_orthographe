"""Orchestration : lance les moteurs en parallèle, filtre et fusionne leurs résultats."""

from __future__ import annotations

import bisect
import logging
import re
import threading
import time
import queue
from dataclasses import replace
from typing import Callable, Iterable

from correcteur.config import PersonalDictionary, Settings
from correcteur.engines.base import Engine, EngineError
from correcteur.models import ENGINE_PRIORITY, Category, CheckResult, EngineStatus, Issue
from correcteur.textutils import (
    adapt_to_user_style,
    apply_edits,
    match_case,
    narrow_edit,
    only_typographic_difference,
    same_letters,
)

log = logging.getLogger(__name__)

_FR_WORDS = frozenset(
    "le la les de des du un une et est je tu il elle nous vous ils elles que qui pas pour dans sur avec ce cette "
    "mais ou au aux à ça ne se son sa ses mon ma mes ton ta tes on y été être avoir fait très bien merci bonjour "
    "salut c'est j'ai d'un d'une l'on qu'il n'est".split()
)
_EN_WORDS = frozenset(
    "the and is are was were you he she we they that this with for not of to in on it be have has my your "
    "hello thanks please would could should will can i'm it's don't".split()
)
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)

# Abréviations courantes à l'écrit (messages, mails) acceptées par défaut.
COMMON_ABBREVIATIONS = frozenset(
    "stp svp bcp rdv mdr ptdr lol tkt dsl jsp cc slt bjr bsr pk pq pcq ptet qqn qqch qqc tjrs tjs cad "
    "ok okay mail mails email emails wifi pb pbs dac tt tte ts att cdt cdlt pj nb ps asap fyi vs etc".split()
)
_SKIP_SPELLING_RE = re.compile(r"(://|@|^#|^www\.|\.(com|fr|org|net|io)$|^\d)", re.IGNORECASE)


def guess_language(text: str) -> str:
    """Détection minimale français / autre, pour ne pas lancer Grammalecte sur un
    texte anglais en mode "auto"."""
    words = [w.lower().replace("’", "'") for w in _WORD_RE.findall(text[:2000])]
    fr = sum(w in _FR_WORDS for w in words)
    en = sum(w in _EN_WORDS for w in words)
    return "fr" if fr >= en else "autre"


Callback = Callable[[CheckResult], None]


def _spawn(target, *args, name: str) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread


class Checker:
    def __init__(self, settings: Settings, dictionary: PersonalDictionary | None = None,
                 engines: list[Engine] | None = None) -> None:
        self.settings = settings
        self.dictionary = dictionary
        self.session_ignored: set[tuple[str, str]] = set()
        self.server = None
        if engines is None:
            from correcteur.engines.grammalecte_engine import GrammalecteEngine
            from correcteur.engines.languagetool_engine import LanguageToolEngine
            from correcteur.engines.lt_server import LocalLanguageToolServer

            self.server = LocalLanguageToolServer(lambda: self.settings)
            engines = [GrammalecteEngine(settings), LanguageToolEngine(settings, self.server)]
        self.engines = engines
        self._closed = threading.Event()
        self._lock = threading.Lock()

    # -- configuration -------------------------------------------------

    def configure(self, settings: Settings) -> None:
        self.settings = settings
        for engine in self.engines:
            engine.configure(settings)
        if self.server is not None and settings.languagetool.mode != "local":
            self.server.stop()

    def engine(self, name: str) -> Engine | None:
        return next((e for e in self.engines if e.name == name), None)

    def resolve_language(self, text: str, language: str | None = None) -> str:
        return language or self.settings.general.language or "fr"

    def active_engines(self, text: str, language: str, only: Iterable[str] | None = None) -> list[Engine]:
        wanted = set(only) if only is not None else None
        result = []
        for engine in self.engines:
            if not engine.is_enabled() or not engine.supports(language):
                continue
            if wanted is not None and engine.name not in wanted:
                continue
            if engine.name == "grammalecte" and language == "auto" and guess_language(text) != "fr":
                continue
            result.append(engine)
        return result

    def warmup(self) -> None:
        """Précharge les moteurs en arrière-plan (sans bloquer)."""
        for engine in self.engines:
            if engine.is_enabled():
                _spawn(self._safe_warmup, engine, name=f"prechargement-{engine.name}")

    @staticmethod
    def _safe_warmup(engine: Engine) -> None:
        try:
            engine.warmup()
        except Exception as exc:  # le moteur signalera l'erreur à la première vérification
            log.info("Préchargement de %s impossible : %s", engine.name, exc)

    def close(self) -> None:
        for engine in self.engines:
            try:
                engine.close()
            except Exception:
                pass
        if self.server is not None:
            self.server.stop()
        self._closed.set()

    # -- vérification --------------------------------------------------

    def _run(self, engine: Engine, text: str, language: str) -> tuple[list[Issue], EngineStatus]:
        t0 = time.perf_counter()
        try:
            ok, why = engine.available()
            if not ok:
                return [], EngineStatus(engine.name, False, why)
            issues = engine.check(text, language)
            return issues, EngineStatus(engine.name, True, "", (time.perf_counter() - t0) * 1000)
        except EngineError as exc:
            return [], EngineStatus(engine.name, False, str(exc), (time.perf_counter() - t0) * 1000)
        except Exception as exc:  # pragma: no cover - filet de sécurité
            log.exception("Erreur du moteur %s", engine.name)
            return [], EngineStatus(engine.name, False, f"Erreur interne : {exc}", (time.perf_counter() - t0) * 1000)

    def check(self, text: str, language: str | None = None, on_update: Callback | None = None,
              only: Iterable[str] | None = None, allow_solo: bool = True) -> CheckResult:
        """Vérifie `text` avec les moteurs actifs (ou seulement ceux de `only`).
        `on_update` est appelé à chaque moteur terminé (résultats partiels),
        depuis un thread de travail. Renvoie le résultat final."""
        language = self.resolve_language(text, language)
        engines = self.active_engines(text, language, only)
        raw: dict[str, list[Issue]] = {}
        statuses: dict[str, EngineStatus] = {}
        names = [e.name for e in engines]
        result = CheckResult(text, self.post_process(text, raw), statuses, tuple(names))
        if not engines:
            result.pending = ()
            if on_update:
                on_update(result)
            return result
        # Un fil "démon" par moteur : rien ne retient la fermeture du programme,
        # même si un moteur en ligne tarde à répondre.
        answers: queue.Queue = queue.Queue()
        for engine in engines:
            _spawn(lambda e=engine: answers.put((e, self._run(e, text, language))), name=f"moteur-{engine.name}")
        for _ in engines:
            while True:
                try:
                    engine, (issues, status) = answers.get(timeout=0.25)
                    break
                except queue.Empty:
                    if self._closed.is_set():
                        return result
            raw[engine.name] = issues
            statuses[engine.name] = status
            pending = tuple(n for n in names if n not in statuses)
            # Un seul moteur a pu vérifier (l'autre est désactivé ou hors ligne) : règle "solo".
            solo = allow_solo and not pending and sum(1 for st in statuses.values() if st.ok) == 1
            result = CheckResult(text, self.post_process(text, raw, solo), dict(statuses), pending)
            if on_update:
                on_update(result)
        return result

    # -- post-traitement -----------------------------------------------

    def post_process(self, text: str, raw: dict[str, list[Issue]], solo: bool = False) -> list[Issue]:
        strict = self.settings.strict_typography
        ignored_rules = set(self.settings.ignored_rules)
        cleaned: list[Issue] = []
        for issues in raw.values():
            for issue in issues:
                fixed = self._clean(text, issue, strict, ignored_rules)
                if fixed is not None:
                    cleaned.append(fixed)
        merged = merge_issues(text, cleaned)
        for issue in merged:
            issue.confident = is_confident(text, issue)
        if solo:
            mark_solo_confident(text, merged)
        return merged

    def _clean(self, text: str, issue: Issue, strict: bool, ignored_rules: set[str]) -> Issue | None:
        if issue.start < 0 or issue.end > len(text) or issue.start > issue.end:
            return None
        original = text[issue.start:issue.end]
        if issue.rule_id in ignored_rules or (issue.category.value, original) in self.session_ignored:
            return None
        if issue.category is Category.SPELLING:
            word = original.strip()
            if self.dictionary is not None and word in self.dictionary:
                return None
            if _SKIP_SPELLING_RE.search(word):
                return None
            if self.settings.general.accept_abbreviations and word.lower() in COMMON_ABBREVIATIONS:
                return None
        had = bool(issue.replacements)
        seen: set[str] = set()
        replacements = []
        for rep in issue.replacements:
            if not strict:
                rep = adapt_to_user_style(rep, text)
            if issue.category is Category.SPELLING:
                rep = match_case(original, rep)
            if rep == original or rep in seen:
                continue
            if not strict and only_typographic_difference(original, rep):
                continue
            seen.add(rep)
            replacements.append(rep)
        if had and not replacements:
            return None  # remarque purement typographique en mode standard
        start, end, replacements = narrow_edit(text, issue.start, issue.end, replacements)
        return replace(issue, start=start, end=end, replacements=replacements)

    # -- actions -------------------------------------------------------

    def ignore_once(self, text: str, issue: Issue) -> None:
        """Ignore cette remarque sur ce texte (tous moteurs confondus) jusqu'à la fermeture."""
        self.session_ignored.add((issue.category.value, issue.original(text)))

    def autocorrect(self, text: str, language: str | None = None,
                    max_passes: int = 6) -> tuple[str, list[Issue], list[Issue], CheckResult]:
        """Correction express : n'applique que des corrections sûres, en plusieurs passes.

        Dans une phrase, deux corrections de grammaire proches dépendent souvent
        l'une de l'autre ("les enfant était ravis" : corriger "enfant" change
        l'accord attendu pour "était" et "ravis"). On n'applique que la première,
        puis on revérifie. La première passe utilise tous les moteurs ; les
        suivantes seulement les moteurs locaux (instantanés) et n'acceptent que :
        - les corrections déjà confirmées par les deux moteurs à la 1re passe ;
        - les corrections évidentes (accents, ponctuation) ;
        - les corrections proposées par le seul moteur en ligne à la 1re passe et
          confirmées ensuite par le moteur local ;
        - dans une phrase déjà corrigée, une nouvelle correction de grammaire à
          suggestion unique (conséquence directe de la correction précédente).
        Une correction que le moteur local proposait seul et que l'autre moteur
        n'a pas confirmée à la 1re passe n'est jamais appliquée.

        Renvoie (texte corrigé, appliquées, restantes, dernier résultat)."""
        language = self.resolve_language(text, language)
        current = text
        result = self.check(current, language)
        confirmed = {(i.original(current), i.replacements[0]) for i in result.issues if i.confident}
        # Qui proposait quoi à la 1re passe : une correction proposée par le seul
        # moteur lent puis confirmée par le moteur local devient sûre ; une
        # correction que le moteur local proposait déjà seul ne le devient jamais.
        first_sources = {(i.original(current), i.replacements[0]): set(i.sources)
                         for i in result.issues if i.replacements}
        # Remarques de la 1re passe venant uniquement des moteurs lents : gardées pour le bilan.
        local = [e.name for e in self.active_engines(current, language) if e.name in _LOCAL_ENGINES]
        slow_only = [i for i in result.issues if not set(i.sources) & set(local)] if local else []
        applied_all: list[Issue] = []
        touched: set[int] = set()
        done: list[tuple[int, int]] = []  # passages déjà corrigés : jamais retouchés
        directions: dict[int, int] = {}   # phrase -> +1 (vers le pluriel) / -1 (vers le singulier)
        for pass_no in range(max_passes):
            if pass_no == 0:
                candidates = split_confident(result.issues)[0]
            else:
                sent = _sentence_finder(current)

                def acceptable(i: Issue) -> bool:
                    key = (i.original(current), i.replacements[0])
                    before = first_sources.get(key)
                    if i.confident or key in confirmed:
                        return True
                    if before is not None:
                        return not before & set(local)  # confirmation par l'autre moteur
                    return (i.category is Category.GRAMMAR and len(i.replacements) == 1
                            and sent(i.start) in touched)

                candidates = [i for i in result.issues if i.replacements and acceptable(i)]
                candidates = split_confident([replace(i, confident=True) for i in candidates])[0]
            candidates = [i for i in candidates if not any(i.start < e and s < i.end for s, e in done)]
            # Cohérence du nombre : dans une phrase, pas de correction vers le singulier
            # après une correction vers le pluriel (ou l'inverse) ; elle reste à vérifier.
            sent = _sentence_finder(current)
            coherent = []
            for i in sorted(candidates, key=lambda x: x.start):
                way = number_direction(i.original(current), i.replacements[0])
                if way and directions.get(sent(i.start), way) != way:
                    continue
                coherent.append(i)
            chosen = pick_independent(current, coherent)
            if not chosen:
                break
            for i in chosen:
                way = number_direction(i.original(current), i.replacements[0])
                if way:
                    directions.setdefault(sent(i.start), way)
            edits = sorted(((i.start, i.end, i.replacements[0]) for i in chosen), key=lambda e: e[0])
            current = apply_edits(current, edits)
            slow_only = remap_issues(slow_only, edits)
            done = _remap_spans(done, edits)
            sent = _sentence_finder(current)
            delta = 0
            touched = set()
            for start, end, rep in edits:
                touched.add(sent(start + delta))
                done.append((start + delta, start + delta + len(rep)))
                delta += len(rep) - (end - start)
            applied_all.extend(chosen)
            result = self.check(current, language, only=local or None, allow_solo=not local)
        remaining = [i for i in result.issues if not i.confident]
        remaining += [i for i in slow_only if not i.confident and not any(i.overlaps(r) for r in remaining)]
        remaining.sort(key=lambda i: i.start)
        return current, applied_all, remaining, result


def _effects(text: str, issue: Issue, lo: int, hi: int, limit: int = 4) -> list[str]:
    head, tail = text[lo:issue.start], text[issue.end:hi]
    return [head + r + tail for r in issue.replacements[:limit]]


def _translate(text: str, rep_effect: str, target: Issue, lo: int, hi: int) -> str | None:
    """Exprime un remplacement de la zone [lo, hi] en remplacement de la zone de `target`."""
    head, tail = text[lo:target.start], text[target.end:hi]
    if rep_effect.startswith(head) and rep_effect.endswith(tail) and len(rep_effect) >= len(head) + len(tail):
        return rep_effect[len(head):len(rep_effect) - len(tail)]
    return None


def _match_score(text: str, a: Issue, b: Issue) -> int:
    """À quel point deux signalements décrivent la même faute :
    3 = même correction principale, 2 = la correction principale de l'un fait
    partie des suggestions de l'autre, 1 = une suggestion en commun ou même mot
    inconnu, 0 = rien à voir."""
    same_span = (a.start, a.end) == (b.start, b.end)
    if same_span and not a.replacements and not b.replacements:
        return 1 if a.category == b.category else 0
    if not (same_span or a.overlaps(b)):
        return 0
    if not a.replacements or not b.replacements:
        return 1 if same_span and a.category == b.category else 0
    lo, hi = min(a.start, b.start), max(a.end, b.end)
    eff_a, eff_b = _effects(text, a, lo, hi, 8), _effects(text, b, lo, hi, 8)
    if eff_a[0] == eff_b[0]:
        return 3
    if eff_a[0] in eff_b or eff_b[0] in eff_a:
        return 2
    if set(eff_a) & set(eff_b):
        return 1
    if same_span and a.category is Category.SPELLING and b.category is Category.SPELLING:
        return 1
    return 0


def merge_issues(text: str, issues: Iterable[Issue]) -> list[Issue]:
    """Fusionne les fautes signalées par plusieurs moteurs.

    Chaque signalement d'un moteur secondaire est rattaché au signalement le
    plus compatible du moteur prioritaire. La zone la plus précise est gardée,
    le message vient du moteur prioritaire, les suggestions sont réunies et
    l'accord des moteurs est mémorisé (il sert à la correction express)."""
    ordered = sorted(issues, key=lambda i: (ENGINE_PRIORITY.get(i.source, 9), i.start, i.end))
    merged: list[Issue] = []
    for issue in ordered:
        best, best_score = None, 0
        for idx, cand in enumerate(merged):
            if issue.source in cand.sources or abs(cand.start - issue.start) > 300:
                continue
            score = _match_score(text, cand, issue)
            if score > best_score:
                best, best_score = idx, score
        if best is None:
            merged.append(issue)
            continue
        cand = merged[best]
        base, other = (cand, issue) if cand.length <= issue.length else (issue, cand)
        lo, hi = min(cand.start, issue.start), max(cand.end, issue.end)
        extra = []
        for eff in _effects(text, other, lo, hi, limit=8):
            translated = _translate(text, eff, base, lo, hi)
            if translated is not None:
                extra.append(translated)
        lead = cand if ENGINE_PRIORITY.get(cand.source, 9) <= ENGINE_PRIORITY.get(issue.source, 9) else issue
        # La correction du moteur prioritaire reste en tête, suivie de celles de l'autre.
        lead_first = _effects(text, lead, lo, hi, 1)
        replacements = list(dict.fromkeys(base.replacements + extra))
        if lead_first:
            first = _translate(text, lead_first[0], base, lo, hi)
            if first in replacements:
                replacements.remove(first)
                replacements.insert(0, first)
        merged[best] = replace(
            base,
            message=lead.message,
            url=lead.url or other.url,
            replacements=replacements,
            sources=tuple(dict.fromkeys(cand.sources + issue.sources)),
            rule_id=lead.rule_id,
            source=lead.source,
            category=lead.category,
            agreed=cand.agreed or best_score >= 2,
        )
    merged.sort(key=lambda i: (i.start, i.end))
    return merged


def is_confident(text: str, issue: Issue) -> bool:
    """Une correction est "sûre" (appliquée par la correction express) si :
    - c'est une faute d'accent ou de majuscule avec une suggestion évidente ;
    - les deux moteurs proposent la même correction de grammaire ;
    - les deux moteurs proposent le même mot et il y a très peu de candidats
      (sinon, "fesait" deviendrait "fessait" au lieu de "faisait") ;
    - c'est une correction de ponctuation/espaces à suggestion unique."""
    if not issue.replacements or issue.category is Category.STYLE:
        return False
    original = text[issue.start:issue.end]
    first = issue.replacements[0]
    if issue.category is Category.SPELLING:
        # "ecole" -> "école" est sûr ; "tache" -> "tâche"/"tachè" ne l'est pas.
        rivals = [r for r in issue.replacements[1:] if same_letters(original, r) and r.casefold() != first.casefold()]
        if same_letters(original, first) and not rivals:
            return True
        return issue.agreed and len(issue.replacements) <= 3
    if issue.agreed:
        return True
    if issue.category is Category.TYPOGRAPHY:
        return len(issue.replacements) == 1 and (not _changes_words(original, first) or same_letters(original, first))
    return False


_SOLO_WINDOW = 12


def mark_solo_confident(text: str, issues: list[Issue]) -> None:
    """Quand un seul moteur a pu vérifier, personne ne peut confirmer ses
    corrections. On accepte alors une correction de grammaire à suggestion
    unique ("a la plage" -> "à la plage"), sauf si une autre remarque est
    toute proche : deux corrections voisines signalent souvent une phrase
    ambiguë ("sont bon" -> "son bon" ou "sont bons" ?)."""
    for issue in issues:
        if issue.confident or issue.category is not Category.GRAMMAR or len(issue.replacements) != 1:
            continue
        crowded = any(
            other is not issue and other.category in (Category.GRAMMAR, Category.SPELLING)
            and other.start - _SOLO_WINDOW < issue.end and issue.start - _SOLO_WINDOW < other.end
            for other in issues
        )
        if not crowded:
            issue.confident = True


def _changes_words(a: str, b: str) -> bool:
    return _WORD_RE.findall(a) != _WORD_RE.findall(b)


_SENTENCE_END_RE = re.compile(r"[.!?…]+(?=\s|$)|\n")
_DEPENDENT_WINDOW = 30
_LOCAL_ENGINES = ("grammalecte",)


def number_direction(original: str, replacement: str) -> int:
    """+1 si la correction met au pluriel ("enfant" -> "enfants", "était" ->
    "étaient"), -1 si elle met au singulier, 0 sinon."""
    a, b = original.lower().strip(), replacement.lower().strip()
    if not a or not b or a == b:
        return 0

    def plural_of(sing: str, plur: str) -> bool:
        if plur in (sing + "s", sing + "x", sing + "es", sing + "nt"):
            return True
        return sing.endswith("t") and plur == sing[:-1] + "ent"  # était -> étaient, finit -> finissent exclus

    if plural_of(a, b):
        return 1
    if plural_of(b, a):
        return -1
    return 0


def _sentence_finder(text: str):
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(text)]
    return lambda pos: bisect.bisect_right(ends, pos)


def _remap_spans(spans: list[tuple[int, int]], edits: list[tuple[int, int, str]]) -> list[tuple[int, int]]:
    result = []
    for lo, hi in spans:
        delta = sum(len(rep) - (end - start) for start, end, rep in edits if end <= lo)
        result.append((lo + delta, hi + delta))
    return result


def remap_issues(issues: list[Issue], edits: list[tuple[int, int, str]]) -> list[Issue]:
    """Recale des fautes après des remplacements (triés) ; celles touchées disparaissent."""
    result = []
    for issue in issues:
        delta = 0
        keep = True
        for start, end, rep in edits:
            if end <= issue.start and not (start == end == issue.start):
                delta += len(rep) - (end - start)
            elif start >= issue.end:
                break
            else:
                keep = False
                break
        if keep:
            result.append(issue.shifted(delta))
    return result


def pick_independent(text: str, issues: list[Issue]) -> list[Issue]:
    """Parmi des corrections sûres, garde celles qui ne risquent pas de dépendre
    d'une autre : les corrections d'orthographe et de ponctuation (locales), et
    pour la grammaire, pas deux corrections à moins de 30 caractères dans la
    même phrase (la suivante sera revue à la passe suivante)."""
    sentence = _sentence_finder(text)
    chosen: list[Issue] = []
    last_grammar: Issue | None = None
    for issue in sorted(issues, key=lambda i: i.start):
        if issue.category is Category.GRAMMAR:
            if last_grammar is not None and sentence(last_grammar.start) == sentence(issue.start) \
                    and issue.start - last_grammar.end < _DEPENDENT_WINDOW:
                continue
            last_grammar = issue
        chosen.append(issue)
    return chosen


def split_confident(issues: list[Issue]) -> tuple[list[Issue], list[Issue]]:
    applied, remaining = [], []
    taken: list[Issue] = []
    for issue in issues:
        if issue.confident and not any(issue.overlaps(t) for t in taken):
            applied.append(issue)
            taken.append(issue)
        else:
            remaining.append(issue)
    return applied, [i for i in remaining if not any(i.overlaps(t) for t in taken)]
