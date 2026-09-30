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
from correcteur.models import ENGINE_LABELS, ENGINE_PRIORITY, Category, CheckResult, EngineStatus, Issue
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
Recheck = Callable[[str], list[Issue]]  # phrase -> remarques trouvées (moteur local, instantané)

# Correction express : attente maximale d'un moteur lent quand un moteur local peut suffire.
EXPRESS_SLOW_TIMEOUT = 8.0


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
              only: Iterable[str] | None = None, allow_solo: bool = True,
              timeout: float | None = None) -> CheckResult:
        """Vérifie `text` avec les moteurs actifs (ou seulement ceux de `only`).
        `on_update` est appelé à chaque moteur terminé (résultats partiels),
        depuis un thread de travail. Renvoie le résultat final.

        Passé `timeout` secondes, les moteurs qui n'ont pas répondu sont comptés
        en échec pour cette vérification. Ils finissent quand même en arrière-plan
        et gardent leur réponse en cache pour la fois suivante."""
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
        deadline = None if timeout is None else time.monotonic() + timeout
        while len(statuses) < len(names):
            wait = 0.25 if deadline is None else max(0.0, min(0.25, deadline - time.monotonic()))
            try:
                engine, (issues, status) = answers.get(timeout=wait)
            except queue.Empty:
                if self._closed.is_set():
                    return result
                if deadline is None or time.monotonic() < deadline:
                    continue
                for name in names:
                    if name not in statuses:
                        label = ENGINE_LABELS.get(name, name)
                        raw[name] = []
                        statuses[name] = EngineStatus(name, False, f"{label} a mis plus de {timeout:g} s à répondre.",
                                                      timeout * 1000)
            else:
                raw[engine.name] = issues
                statuses[engine.name] = status
            pending = tuple(n for n in names if n not in statuses)
            # Un seul moteur a pu vérifier (l'autre est désactivé ou hors ligne) : règle "solo".
            answered = [n for n, st in statuses.items() if st.ok]
            solo = allow_solo and not pending and len(answered) == 1
            recheck = self._recheck(answered[0], language) if solo and answered[0] in _LOCAL_ENGINES else None
            result = CheckResult(text, self.post_process(text, raw, solo, recheck), dict(statuses), pending)
            if on_update:
                on_update(result)
        return result

    def _recheck(self, name: str, language: str) -> Recheck:
        """Revérification instantanée par un moteur local, pour la contre-épreuve de la règle "solo"."""
        engine = self.engine(name)
        assert engine is not None
        return lambda sentence: self.post_process(sentence, {name: engine.recheck(sentence, language)})

    def post_process(self, text: str, raw: dict[str, list[Issue]], solo: bool = False,
                     recheck: Recheck | None = None) -> list[Issue]:
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
            mark_solo_confident(text, merged, recheck)
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

    def ignore_once(self, text: str, issue: Issue) -> None:
        """Ignore cette remarque sur ce texte (tous moteurs confondus) jusqu'à la fermeture."""
        self.session_ignored.add((issue.category.value, issue.original(text)))

    def autocorrect(self, text: str, language: str | None = None, max_passes: int = 6,
                    slow_timeout: float | None = EXPRESS_SLOW_TIMEOUT) -> tuple[str, list[Issue], list[Issue], CheckResult]:
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

        Si un moteur local peut vérifier seul, on n'attend pas les moteurs lents
        plus de `slow_timeout` secondes (réseau lent, serveur local qui démarre).

        Renvoie (texte corrigé, appliquées, restantes, dernier résultat)."""
        language = self.resolve_language(text, language)
        current = text
        local = [e.name for e in self.active_engines(current, language) if e.name in _LOCAL_ENGINES]
        can_go_alone = any(e.available()[0] for e in self.engines if e.name in local)
        result = self.check(current, language, timeout=slow_timeout if can_go_alone else None)
        first_statuses = result.statuses
        confirmed = {(i.original(current), i.replacements[0]) for i in result.issues if i.confident}
        # Qui proposait quoi à la 1re passe : une correction proposée par le seul
        # moteur lent puis confirmée par le moteur local devient sûre ; une
        # correction que le moteur local proposait déjà seul ne le devient jamais.
        first_sources = {(i.original(current), i.replacements[0]): set(i.sources)
                         for i in result.issues if i.replacements}
        # Remarques de la 1re passe venant uniquement des moteurs lents : gardées pour le bilan.
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
                        # Confirmation par le moteur local ; sans moteur local, personne ne peut confirmer.
                        return bool(local) and not before & set(local)
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
        # Les moteurs lents n'ont tourné qu'à la 1re passe : leur état reste dans le bilan.
        result.statuses = {**first_statuses, **result.statuses}
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


def _rules_to_ignore(issue: Issue) -> tuple[str, ...]:
    # Une règle d'orthographe couvre tous les mots : on ne la coupe jamais pour une seule faute.
    return () if issue.category is Category.SPELLING else issue.rules


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
            rules=tuple(dict.fromkeys((lead.rule_id,) + _rules_to_ignore(cand) + _rules_to_ignore(issue))),
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
_SOLO_MAX_TRIALS = 120
# Accord déduit de la seule forme du verbe être ("sont bon" -> "bons") : si c'est le verbe qui est faux
# ("il a pris sont manteau" -> "sont manteaux"), la correction aggrave la phrase. Jamais sans confirmation.
_SOLO_WEAK_RULES = ("gv1__ppas_être_accord_",)


def solo_candidate(issue: Issue) -> bool:
    """Correction de grammaire qu'un moteur seul peut appliquer : une seule suggestion, ou
    la même au masculin et au féminin (« allé / allée » : la première, comme le ferait un
    correcteur humain qui ne connaît pas l'auteur)."""
    if issue.confident or issue.category is not Category.GRAMMAR or not issue.replacements:
        return False
    if issue.rule_id.startswith(_SOLO_WEAK_RULES):
        return False
    first = issue.replacements[0]
    feminine = first[:-1] + "es" if first.endswith("s") else first + "e"
    return all(r == feminine for r in issue.replacements[1:])


def mark_solo_confident(text: str, issues: list[Issue], recheck: Recheck | None = None) -> None:
    """Quand un seul moteur a pu vérifier, personne ne peut confirmer ses corrections.

    On accepte une correction de grammaire évidente ("a la plage" -> "à la plage", voir
    `solo_candidate`) si aucune autre remarque n'est toute proche : deux corrections
    voisines signalent souvent une phrase ambiguë ("sont bon" -> "son bon" ou "sont bons" ?).

    Avec un moteur local (`recheck`), on tranche les cas voisins par contre-épreuve : on
    applique la correction et on revérifie la phrase. Elle est sûre si le moteur ne la
    resignale pas et si, pour chaque remarque voisine :
    - la voisine est toujours là (fautes indépendantes : "avons manger des pomme") ;
    - ou la voisine disparaît, mais la corriger elle ne ferait pas disparaître celle-ci :
      c'était une conséquence ("Ils on mangé" : "ont" règle "mangé", l'inverse non).
    Si chacune des deux corrections fait disparaître l'autre sans créer de nouvelle faute,
    ce sont deux lectures possibles ("sont bon") : aucune n'est appliquée."""
    trials = _Trials(text, issues, recheck) if recheck is not None else None
    for issue in issues:
        if not solo_candidate(issue):
            continue
        neighbours = [
            other for other in issues
            if other is not issue and other.category in (Category.GRAMMAR, Category.SPELLING)
            and other.start - _SOLO_WINDOW < issue.end and issue.start - _SOLO_WINDOW < other.end
        ]
        if trials is None:
            issue.confident = not neighbours
        else:
            issue.confident = trials.stands(issue, [o for o in neighbours if trials.same_sentence(issue, o)])


class _Trials:
    """Contre-épreuves de la règle "solo" : chaque correction est appliquée seule à sa
    phrase, que le moteur local revérifie (une fois par correction, dans une limite).
    Une phrase seule suffit : les règles de grammaire ne dépassent pas la phrase, et
    l'analyse est cinq fois plus rapide que celle du paragraphe."""

    def __init__(self, text: str, issues: list[Issue], recheck: Recheck) -> None:
        self.text = text
        self.issues = issues
        self.recheck = recheck
        self.budget = _SOLO_MAX_TRIALS
        self._ends = [m.end() for m in _SENTENCE_END_RE.finditer(text)]
        self._done: dict[tuple[int, int, str], tuple[int, str, list[Issue]] | None] = {}

    def _bounds(self, issue: Issue) -> tuple[int, int]:
        """Début et fin de la phrase qui contient la remarque."""
        i = bisect.bisect_right(self._ends, issue.start)
        lo = self._ends[i - 1] if i else 0
        j = bisect.bisect_left(self._ends, issue.end)
        hi = self._ends[j] if j < len(self._ends) else len(self.text)
        while lo < issue.start and self.text[lo].isspace():
            lo += 1
        while hi > issue.end and self.text[hi - 1] == "\n":
            hi -= 1
        return lo, hi

    def same_sentence(self, a: Issue, b: Issue) -> bool:
        return self._bounds(a) == self._bounds(b)

    def after(self, issue: Issue) -> tuple[int, str, list[Issue]] | None:
        """(début de la phrase, phrase corrigée, remarques trouvées), ou None hors budget."""
        key = (issue.start, issue.end, issue.replacements[0])
        if key not in self._done:
            if self.budget <= 0:
                return None
            self.budget -= 1
            lo, hi = self._bounds(issue)
            sentence = self.text[lo:issue.start] + issue.replacements[0] + self.text[issue.end:hi]
            try:
                self._done[key] = (lo, sentence, self.recheck(sentence))
            except Exception as exc:  # contre-épreuve impossible : la correction reste à vérifier
                log.debug("Contre-épreuve impossible : %s", exc)
                self._done[key] = None
        return self._done[key]

    def _moved(self, fix: Issue, other: Issue, lo: int) -> tuple[int, int]:
        """Position de `other` dans la phrase où `fix` est appliquée."""
        delta = len(fix.replacements[0]) - fix.length if other.start >= fix.end else 0
        return other.start - lo + delta, other.end - lo + delta

    def _still_there(self, fix: Issue, other: Issue) -> bool | None:
        trial = self.after(fix)
        if trial is None:
            return None
        lo, _, found = trial
        start, end = self._moved(fix, other, lo)
        probe = replace(other, start=start, end=end)
        return any(f.category is other.category and f.overlaps(probe) for f in found)

    def _clean_fix(self, fix: Issue) -> bool | None:
        """La correction n'est pas resignalée et ne fait apparaître aucune nouvelle faute."""
        trial = self.after(fix)
        if trial is None:
            return None
        lo, _, found = trial
        start = fix.start - lo
        fixed = replace(fix, start=start, end=start + len(fix.replacements[0]))
        delta = len(fix.replacements[0]) - fix.length
        for f in found:
            if f.overlaps(fixed):
                return False
            back = f.shifted(lo - delta if f.start >= fixed.end else lo)
            if not any(o.category is f.category and o.overlaps(back) for o in self.issues):
                return False
        return True

    def stands(self, issue: Issue, neighbours: list[Issue]) -> bool:
        trial = self.after(issue)
        if trial is None:
            return False
        lo, _, found = trial
        start = issue.start - lo
        fixed = replace(issue, start=start, end=start + len(issue.replacements[0]))
        if any(f.overlaps(fixed) for f in found):
            return False  # le moteur n'est pas satisfait de sa propre correction
        for other in neighbours:
            present = self._still_there(issue, other)
            if present is None:
                return False
            if present or not other.replacements:
                continue  # indépendante, ou simple conséquence sans correction propre
            reverse = self._still_there(other, issue)
            if reverse is None:
                return False
            if not reverse and self._clean_fix(other):
                return False  # deux lectures possibles : on laisse l'utilisateur choisir
        return True


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
