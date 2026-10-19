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
from correcteur.engines.regles import INDICATIVE_BEFORE_QUE, VETO
from correcteur.models import ENGINE_LABELS, ENGINE_PRIORITY, Category, CheckResult, EngineStatus, Issue
from correcteur.textutils import (
    adapt_to_user_style,
    apply_edits,
    match_case,
    narrow_edit,
    one_edit,
    only_typographic_difference,
    same_letters,
    same_sound,
    strip_accents,
    subject_ending,
    typo_normalize,
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


def _seconds(value: float) -> str:
    """« 8 s », « 8,1 s » : une décimale au plus, virgule française."""
    return f"{round(value, 1):g}".replace(".", ",") + " s"


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


def _slow_timeout(base: float | None, text: str) -> float | None:
    """Attente du moteur lent, plus longue pour un long texte : une page (3 800 caractères) prend souvent plus de
    8 s au serveur public de LanguageTool, et retombait sur Grammalecte seul. 2 s par millier de caractères, au
    plus 4 fois l'attente de base."""
    if base is None:
        return None
    return min(base * 4, base + 2.0 * len(text) / 1000)


def _spawn(target, *args, name: str) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread


class Checker:
    def __init__(self, settings: Settings, dictionary: PersonalDictionary | None = None,
                 engines: list[Engine] | None = None) -> None:
        self.settings = settings
        self.dictionary = dictionary
        self.session_ignored: set[tuple[str, ...]] = set()
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
                        statuses[name] = EngineStatus(name, False, f"{label} a mis plus de {_seconds(timeout)} à "
                                                      "répondre.", timeout * 1000)
            else:
                raw[engine.name] = issues
                statuses[engine.name] = status
            pending = tuple(n for n in names if n not in statuses)
            # Un seul moteur a pu vérifier (l'autre est désactivé ou hors ligne) : règle "solo".
            answered = [n for n, st in statuses.items() if st.ok]
            solo = allow_solo and not pending and len(answered) == 1
            recheck = self._recheck(answered[0], language) if solo and answered[0] in _LOCAL_ENGINES else None
            issues = self.post_process(text, raw, solo, recheck)
            if not pending and len(answered) > 1:
                self._settle_partial(text, raw, issues, language)
                self._confirm_remote(text, raw, issues, language)
            result = CheckResult(text, issues, dict(statuses), pending)
            if on_update:
                on_update(result)
        return result

    def _settle_partial(self, text: str, raw: dict[str, list[Issue]], issues: list[Issue], language: str) -> None:
        """Accord partiel : la correction de Grammalecte n'est que le 2e choix de LanguageTool (ou LanguageTool
        ne dit rien de ce passage). Un score ne
        peut pas trancher (« les enfant » : LanguageTool préfère « l'enfant », à tort ; « on joué » : il préfère
        « ont joué », à raison). On reprend donc la décision hors ligne, Grammalecte seul avec contre-épreuve :
        la correction en ligne n'en fait jamais moins que hors ligne quand l'autre moteur la propose aussi."""
        local = [name for name in raw if name in _LOCAL_ENGINES and raw[name]]
        if len(local) != 1:
            return
        # Aussi quand l'autre moteur ne dit rien de ce passage : il ne contredit pas, et la correction en ligne
        # ne doit pas en faire moins que hors ligne (« Tu à raison », que LanguageTool laisse passer).
        # Les règles du correcteur aussi quand LanguageTool propose autre chose au même endroit (« Je pense que ces
        # vrai » : « c'est » contre « ce vrai ») : elles ne se déclenchent que quand le contexte impose leur correction.
        partial = [i for i in issues if not i.confident and i.replacements and (
            i.partly_agreed or (i.sources == (local[0],) and (rule_family(i.rule_id).startswith("corr_") or not any(
                o is not i and o.overlaps(i) and local[0] not in o.sources for o in issues))))]
        # Désaccord franc (« Tu à raison » : « as » pour Grammalecte, « a » pour LanguageTool, dont la règle
        # d'accent ne regarde pas le sujet) : voir plus bas.
        contradicted = [i for i in issues if not i.confident and not i.agreed and not i.partly_agreed
                        and i.category is Category.GRAMMAR and i.replacements and local[0] in i.sources
                        and len(i.sources) > 1]
        if not partial and not contradicted:
            return
        recheck = self._recheck(local[0], language)
        offline = self.post_process(text, {local[0]: raw[local[0]]}, True, recheck)
        settled = {_corrected(text, o) for o in offline if o.confident and o.replacements}
        for issue in partial:
            # La suggestion retenue hors ligne n'est pas toujours la première de la remarque fusionnée (« Il et
            # sorti » : « ait, est, hait » pour Grammalecte, « est » pour LanguageTool) : elle passe en tête.
            hit = next((r for r in issue.replacements if text[:issue.start] + r + text[issue.end:] in settled), None)
            if hit is not None and hit != issue.replacements[0]:
                issue.replacements = [hit] + [r for r in issue.replacements if r != hit]
            issue.confident = hit is not None
        if not contradicted:
            return
        # La correction de Grammalecte l'emporte s'il en est sûr hors ligne et s'il resignale aussitôt celle de
        # LanguageTool appliquée (« Tu a raison » : « a » -> « as »). Si les deux passent sa contre-épreuve, ce
        # sont deux lectures et l'utilisateur choisit (« Les enfants on joué » : « on joue » pour Grammalecte,
        # « ont joué » pour LanguageTool ; la règle du correcteur tranche maintenant pour « ont »).
        trials = _Trials(text, issues, recheck)
        for issue in contradicted:
            remote = [o for name, found in raw.items() if name != local[0] for o in found
                      if o.replacements and o.category is Category.GRAMMAR and o.overlaps(issue)]
            if not solo_rule(issue):
                # Règle de Grammalecte peu fiable (« Les filles son belles » : « sa », puis « sont ») : la suggestion
                # de LanguageTool passe en tête, pour Alt+1. Rien n'est appliqué d'office.
                theirs = {typo_normalize(o.replacements[0]) for o in remote}
                first = next((r for r in issue.replacements if typo_normalize(r) in theirs), None)
                if first is not None:
                    issue.replacements = [first] + [r for r in issue.replacements if r != first]
                continue
            hit = next((r for r in issue.replacements if text[:issue.start] + r + text[issue.end:] in settled), None)
            if hit is None:
                continue
            if hit != issue.replacements[0]:
                issue.replacements = [hit] + [r for r in issue.replacements if r != hit]
            # Sûre si LanguageTool propose la même (« Il et sorti » : « est », 2e suggestion de Grammalecte, qu'il
            # retient hors ligne), ou si Grammalecte resignale aussitôt la sienne.
            mine = _corrected(text, issue)
            issue.confident = bool(remote) and all(
                _corrected(text, o) == mine or trials._clean_fix(o) is False for o in remote)

    def _confirm_remote(self, text: str, raw: dict[str, list[Issue]], issues: list[Issue], language: str) -> None:
        """LanguageTool seul propose une correction, et Grammalecte signale juste à côté une faute qu'il corrige
        mal (« Mes parents son partis » : « sont » pour LanguageTool, « parti » pour Grammalecte ; « Les enfants
        on faim » : « ont » contre « feint »). Les deux moteurs voient une faute à cet endroit : si la correction
        de LanguageTool fait taire Grammalecte sans lui faire signaler autre chose (contre-épreuve), elle est
        sûre. Pas quand la remarque voisine vient d'une règle fiable de Grammalecte : ce sont alors deux lectures
        (« les enfant joue »), et l'utilisateur choisit."""
        local = [name for name in raw if name in _LOCAL_ENGINES]  # même sans remarque : il a vérifié
        if len(local) != 1:
            return
        name = local[0]
        trials: _Trials | None = None
        for issue in issues:
            if (issue.confident or issue.category is not Category.GRAMMAR or not issue.replacements
                    or name in issue.sources or not _homophones(text[issue.start:issue.end], issue.replacements[0])):
                continue
            weak = [o for o in issues if o.sources == (name,) and not o.confident and not o.overlaps(issue)
                    and not solo_rule(o) and _next_to(text, issue, o)]
            trials = trials or _Trials(text, issues, self._recheck(name, language))
            if weak:
                issue.confident = bool(trials._clean_fix(issue)) and all(
                    trials._still_there(issue, o) is False for o in weak)
            elif not any(o is not issue and trials.same_sentence(o, issue) for o in issues):
                # Seule remarque de la phrase, et Grammalecte n'y trouve rien (« Je me suis fait mal a la main ») :
                # l'homophone de LanguageTool est sûr si Grammalecte ne signale rien une fois la correction
                # appliquée. Une autre faute dans la phrase rend la lecture incertaine (« Où sont Papa est Maman »
                # devenait « son »). Voir _remote_homophone_ok pour les tournures écartées.
                issue.confident = _remote_homophone_ok(text, issue) and bool(trials._clean_fix(issue))

    def _recheck(self, name: str, language: str) -> Recheck:
        """Revérification instantanée par un moteur local, pour la contre-épreuve de la règle "solo"."""
        engine = self.engine(name)
        assert engine is not None
        return lambda sentence: self.post_process(sentence, {name: engine.recheck(sentence, language)})

    def post_process(self, text: str, raw: dict[str, list[Issue]], solo: bool = False,
                     recheck: Recheck | None = None) -> list[Issue]:
        strict = self.settings.strict_typography
        ignored_rules = set(self.settings.ignored_rules)
        # Zones que les règles du correcteur déclarent justes (engines/regles.py) : aucun moteur n'y corrige.
        vetoes = [(i.start, i.end) for issues in raw.values() for i in issues if i.rule_id == VETO]
        cleaned: list[Issue] = []
        for issues in raw.values():
            for issue in issues:
                if issue.rule_id == VETO or any(s <= issue.start and issue.end <= e for s, e in vetoes):
                    continue
                fixed = self._clean(text, issue, strict, ignored_rules)
                if fixed is not None:
                    cleaned.append(fixed)
        merged = merge_issues(text, cleaned)
        if doubtful_text(text, merged, self.settings.general.language):
            return merged  # tout reste à vérifier : rien n'est sûr
        for issue in merged:
            # L'accord des deux moteurs n'exclut pas une autre lecture qu'ils manquent tous les deux
            # (« Son seul plaisir est manger » : tous deux veulent « mangé »).
            issue.confident = is_confident(text, issue) and (
                issue.category is not Category.GRAMMAR or not issue.replacements
                or solo_context_ok(text, issue, agreed=issue.agreed))
        if solo:
            mark_solo_confident(text, merged, recheck)
        for issue in merged:
            if issue.confident and rule_family(issue.rule_id).startswith("gn_") and any(
                    o is not issue and o.end <= issue.start and not text[o.end:issue.start].strip() for o in merged):
                # « Il ces levé » -> « levés » s'accorde avec « ces », que le même moteur déclare faux (« s'est ») :
                # un accord sur un mot douteux n'est pas sûr.
                issue.confident = False
        return merged

    def _clean(self, text: str, issue: Issue, strict: bool, ignored_rules: set[str]) -> Issue | None:
        if issue.start < 0 or issue.end > len(text) or issue.start > issue.end:
            return None
        original = text[issue.start:issue.end]
        if issue.rule_id in ignored_rules or self._ignored(text, issue, original):
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
                rep = adapt_to_user_style(rep, text, original)
            if issue.category is Category.SPELLING and rep.casefold() != original.casefold():
                # Pas pour une correction de la casse elle-même (« Janvier » -> « janvier »).
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
        """Ignore cette remarque (tous moteurs confondus) jusqu'à la fermeture : un mot inconnu partout ;
        une remarque de grammaire seulement à cet endroit, repéré par le passage et ses deux mots voisins.
        Ignorer « a » -> « à » une fois ne fait pas taire « sac a dos » ailleurs, et corriger une autre faute
        de la même phrase ne fait pas revenir la remarque."""
        original = issue.original(text)
        if issue.category is Category.SPELLING:
            self.session_ignored.add((issue.category.value, original))
            return
        anchor = _anchor(text, issue.start, issue.end)
        for rule in issue.rules:
            self.session_ignored.add((rule, anchor))

    def _ignored(self, text: str, issue: Issue, original: str) -> bool:
        if not self.session_ignored:
            return False
        if issue.category is Category.SPELLING:
            return (issue.category.value, original) in self.session_ignored
        # La zone d'un moteur peut être plus étroite que la remarque fusionnée qu'on a ignorée : il suffit
        # qu'elle tienne dans un passage ignoré, à sa place.
        for rule, anchor in self.session_ignored:
            if rule != issue.rule_id:
                continue
            pos = text.find(anchor, max(0, issue.end - len(anchor)))
            while pos != -1 and pos <= issue.start:
                if issue.end <= pos + len(anchor):
                    return True
                pos = text.find(anchor, pos + 1)
        return False

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
          suggestion unique (conséquence directe de la correction précédente), d'une
          famille de règles fiable (`_SOLO_RULES`) et sans autre lecture possible
          (`solo_context_ok`).
        Une correction que le moteur local proposait seul et que l'autre moteur
        n'a pas confirmée à la 1re passe n'est jamais appliquée.

        Si un moteur local peut vérifier seul, on n'attend pas les moteurs lents
        plus de `slow_timeout` secondes (réseau lent, serveur local qui démarre).

        Renvoie (texte corrigé, appliquées, restantes, dernier résultat)."""
        language = self.resolve_language(text, language)
        current = text
        local = [e.name for e in self.active_engines(current, language) if e.name in _LOCAL_ENGINES]
        can_go_alone = any(e.available()[0] for e in self.engines if e.name in local)
        result = self.check(current, language, timeout=_slow_timeout(slow_timeout, current) if can_go_alone else None)
        first_statuses = result.statuses
        # Remarques de la 1re passe, recalées après chaque correction : qui proposait quoi, et où. Une
        # correction proposée par le seul moteur lent puis confirmée par le moteur local devient sûre ; une
        # correction que le moteur local proposait déjà seul ne le devient jamais. Tout se juge à la même
        # place : le même « ce » -> « cette » sûr dans une phrase ne l'est pas pour autant dans la suivante.
        first_pass = [i for i in result.issues if i.replacements]
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
                    fixed = _corrected(current, i)
                    same = [j for j in first_pass if j.overlaps(i) and _corrected(current, j) == fixed]
                    if i.confident or any(j.confident for j in same):
                        return True
                    if same:
                        # Confirmation par le moteur local ; sans moteur local, personne ne peut confirmer.
                        return bool(local) and not {s for j in same for s in j.sources} & set(local)
                    return (i.category is Category.GRAMMAR and len(i.replacements) == 1
                            and sent(i.start) in touched and solo_rule(i) and solo_context_ok(current, i))

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
            first_pass = remap_issues(first_pass, edits)
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


def _distinct(replacements: list[str]) -> list[str]:
    """Sans doublons, à la typographie près (« C’est » et « C'est ») : la première forme est gardée."""
    seen: set[str] = set()
    return [r for r in replacements if not (typo_normalize(r) in seen or seen.add(typo_normalize(r)))]


# Confusions d'homophones grammaticaux : les seules corrections de LanguageTool confirmées par la contre-épreuve.
_HOMOPHONES = (("son", "sont"), ("on", "ont"), ("a", "à"), ("et", "est"), ("ce", "se"), ("ces", "ses"),
               ("ça", "sa"), ("ou", "où"), ("la", "là"), ("peu", "peut"), ("peu", "peux"), ("leur", "leurs"),
               ("ma", "m'a"), ("ta", "t'a"), ("mes", "mais"), ("quand", "quant"), ("ni", "n'y"), ("sans", "s'en"),
               ("c'est", "s'est"), ("dans", "d'en"), ("si", "s'y"), ("ses", "s'est"), ("ces", "c'est"))
_HOMOPHONE_PAIRS = frozenset(_HOMOPHONES) | frozenset((b, a) for a, b in _HOMOPHONES)


def _homophones(original: str, replacement: str) -> bool:
    key = (original.lower().replace("’", "'"), replacement.lower().replace("’", "'"))
    return key in _HOMOPHONE_PAIRS


# « Patrick a l'air sombre », « une volonté a toutes les chances », « celui qui a peine à » : « a » verbe, que
# LanguageTool prend pour « à ».
_A_VERB_NEXT_RE = re.compile(r"\s+(?:l['’]air\b|tou(?:t|te|s|tes)\b|peine\s+à\b)", re.IGNORECASE)


def _remote_homophone_ok(text: str, issue: Issue) -> bool:
    """Règles et tournures où LanguageTool se trompe trop souvent, mesurées sur deux échantillons du corpus :
    « Quand à mon tour je réalise » (QUAND_QUANT : 0 bonne sur 2), « Où sont Papa est Maman » -> « son »
    (SONT_SON : 2 sur 3), « à » -> « a » (A_ACCENT_A : 4 sur 5), « a » verbe suivi de « l'air », « tout »,
    « peine à ». Les autres règles : environ 100 bonnes pour 1 dégât."""
    if "QUAND_QUANT" in issue.rule_id or issue.rule_id in ("SONT_SON", "A_ACCENT_A"):
        return False
    if text[issue.start:issue.end].lower() == "a" and _A_VERB_NEXT_RE.match(text, issue.end):
        return False
    return True


_PARTICIPLE_AFTER_RE = re.compile(r"\s+\w+(?:é|ée|és|ées|i|ie|is|ies|it|u|ue|us|ues)\b", re.IGNORECASE)
_DETERMINER_AFTER_RE = re.compile(r"\s+(?:[ld]['’]|(?:le|la|les|un|une|des|du|mon|ma|mes|ton|ta|tes|son|sa|ses|notre|nos|"
                                  r"votre|vos|leur|leurs|ce|cet|cette|ces)\b)", re.IGNORECASE)
# « Quelque soit le temps » : le déterminant qui suit dit le genre (« Quel que soit », « Quelle que soit la météo »).
_QUEL_QUE_NEXT_RE = re.compile(r"\s*(?:soi(?:t|ent)\s+)?(\w+)\b", re.IGNORECASE)
_MASCULINE_DETERMINERS = frozenset("le un ce cet mon ton son notre votre leur".split())
_FEMININE_DETERMINERS = frozenset("la une cette ma ta sa".split())


def _solo_pick(text: str, issue: Issue) -> str | None:
    """Parmi plusieurs suggestions, celle que le contexte impose, quand il l'impose : l'auxiliaire devant un
    participe (« Je vous est envoyé » -> « ai », pas « aie » ni « suis »), le genre du déterminant qui suit
    (« Quelque soit le temps » -> « Quel que soit »)."""
    family = rule_family(issue.rule_id)
    first = issue.replacements[0]
    if family in ("conj_je", "conj_tu") and first in ("ai", "as") and _PARTICIPLE_AFTER_RE.match(text, issue.end):
        return first
    if family.endswith("_non_verbe") and text[issue.start:issue.end].lower() == "et" and "est" in issue.replacements:
        return "est"  # « Il et sorti » : « est », pas « ait » ni « hait »
    if family.startswith("conj_") or family.endswith("_non_verbe"):
        # « Je fini » : « finis » ou « finissais » ? Celle qui se prononce comme ce qui est écrit.
        same = [r for r in issue.replacements if same_sound(text[issue.start:issue.end], r)]
        if len(same) == 1:
            return same[0]
    if family == "conf_quel_que_soit":
        m = _QUEL_QUE_NEXT_RE.match(text, issue.end)
        det = m.group(1).lower() if m else ""
        want = "quel que" if det in _MASCULINE_DETERMINERS else "quelle que" if det in _FEMININE_DETERMINERS else None
        if want:
            return next((r for r in issue.replacements if r.lower() == want or r.lower().startswith(want + " ")), None)
    return None


_NEXT_TO_RE = re.compile(r"[^\w.!?…\n]*(?:[\w'’-]+[^\w.!?…\n]+)?")


def _next_to(text: str, a: Issue, b: Issue) -> bool:
    """Au plus un mot entre les deux remarques, dans la même phrase."""
    gap = text[a.end:b.start] if a.end <= b.start else text[b.end:a.start]
    return _NEXT_TO_RE.fullmatch(gap) is not None


def _corrected(text: str, issue: Issue) -> str:
    return text[:issue.start] + issue.replacements[0] + text[issue.end:]


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
    partie des suggestions de l'autre, 1 = une suggestion en commun ou le même
    passage, 0 = rien à voir."""
    same_span = (a.start, a.end) == (b.start, b.end)
    if same_span and not a.replacements and not b.replacements:
        return 1 if a.category == b.category else 0
    if not (same_span or a.overlaps(b)):
        return 0
    if not a.replacements or not b.replacements:
        return 1 if same_span and a.category == b.category else 0
    lo, hi = min(a.start, b.start), max(a.end, b.end)
    # Comparées sans la typographie : « C’est » de l'un et « C'est » de l'autre sont la même correction.
    eff_a = [typo_normalize(e) for e in _effects(text, a, lo, hi, 8)]
    eff_b = [typo_normalize(e) for e in _effects(text, b, lo, hi, 8)]
    if eff_a[0] == eff_b[0]:
        return 3
    if eff_a[0] in eff_b or eff_b[0] in eff_a:
        return 2
    if set(eff_a) & set(eff_b):
        return 1
    if same_span:
        # Même passage, corrections différentes (« tu à » : « as » pour l'un, « a » pour l'autre) : une seule
        # remarque qui propose les deux, plutôt que deux remarques contradictoires. Jamais un accord.
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
    l'accord des moteurs est mémorisé (il sert à la correction express). Il faut
    pour cela la même correction principale : « on joue » en tête chez l'un et
    seulement en 2e choix chez l'autre (qui préfère « ont joué ») n'est pas un accord."""
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
        effects = _effects(text, other, lo, hi, limit=8)
        translated = [_translate(text, eff, base, lo, hi) for eff in effects]
        if translated and translated[0] is None:
            # La correction principale de l'autre moteur déborde de la zone étroite (« on joué » -> « ont joué »
            # change « on », hors de « joué ») : on passe à la zone qui couvre les deux pour ne pas la perdre.
            base = replace(base, start=lo, end=hi,
                           replacements=_effects(text, base, lo, hi, limit=len(base.replacements)))
            translated = effects
        extra = [t for t in translated if t is not None]
        lead = cand if ENGINE_PRIORITY.get(cand.source, 9) <= ENGINE_PRIORITY.get(issue.source, 9) else issue
        # La correction du moteur prioritaire reste en tête, celle de l'autre moteur la suit : en cas de
        # désaccord (« on joue » / « ont joué »), les deux choix principaux sont les premiers proposés.
        replacements = _distinct(base.replacements + extra)
        follower = issue if lead is cand else cand
        lead_effect = [typo_normalize(e) for e in _effects(text, lead, lo, hi, 1)]
        partly = bool(lead_effect) and lead_effect[0] in [typo_normalize(e) for e in _effects(text, follower, lo, hi, 8)]
        for position, engine_issue in ((0, lead), (1, follower)):
            effect = _effects(text, engine_issue, lo, hi, 1)
            first = _translate(text, effect[0], base, lo, hi) if effect else None
            keys = [typo_normalize(r) for r in replacements]
            if first is not None and typo_normalize(first) in keys and keys.index(typo_normalize(first)) > position:
                replacements.insert(position, replacements.pop(keys.index(typo_normalize(first))))
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
            agreed=cand.agreed or best_score == 3,
            partly_agreed=cand.partly_agreed or partly,
        )
    merged.sort(key=lambda i: (i.start, i.end))
    return merged


_ENGLISH_NEIGHBOURS = frozenset("""the of and for with to in on at by from is are was were be an or not this that it its
    you your we our they their my his her new great general""".split())


def _capitalized_mid_sentence(text: str, start: int) -> bool:
    """Majuscule au milieu d'une phrase (« les Trail Blazers », « Force ouvrière », « Monster Hunter
    Generations ») : un nom propre ou un titre, qu'on ne corrige pas d'office."""
    if start >= len(text) or not text[start].isupper():
        return False
    before = text[:start].rstrip()
    return bool(before) and not re.search(r"[.!?…:;\n«“\"(\[—–-]$", before)


def _foreign_neighbour(text: str, issue: Issue) -> bool:
    """Un mot voisin est anglais (« Dawn of the Dead » : « the » n'est pas « thé »)."""
    before = re.findall(r"[\w']+", text[max(0, issue.start - 30):issue.start])
    after = re.findall(r"[\w']+", text[issue.end:issue.end + 30])
    near = [w.lower() for w in before[-1:] + after[:1]]
    return text[issue.start:issue.end].lower() in _ENGLISH_NEIGHBOURS or any(w in _ENGLISH_NEIGHBOURS for w in near)


def _next_to_number(text: str, issue: Issue) -> bool:
    return bool(re.search(r"\d\W*$", text[max(0, issue.start - 12):issue.start])
                or re.match(r"\W*\d", text[issue.end:issue.end + 12]))


def doubtful_text(text: str, issues: list[Issue], language: str) -> bool:
    """Texte où rien ne doit être corrigé d'office : réglé en français mais qui n'en a pas l'air (« at the
    cafe » deviendrait « at thé café »), ou plein de mots inconnus (autre langue, jargon, noms propres)."""
    if language.startswith("fr") and guess_language(text) != "fr":
        return True
    words = _WORD_RE.findall(text)
    unknown = sum(1 for i in issues if i.category is Category.SPELLING)
    return len(words) >= 4 and unknown > 0.3 * len(words)


def _accent_count(word: str) -> int:
    return sum(1 for ch in word if strip_accents(ch) != ch)


# Graphies sans trait d'union aussi justes : « Pierre Louis » (un prénom s'écrit comme la personne l'écrit),
# « château fort ».
_LOOSE_HYPHENS = frozenset({"château fort", "châteaux forts"})


def _letters_only(s: str) -> str:
    return re.sub(r"[\s'’-]+", "", strip_accents(s)).casefold()


# Consonnes doublées que l'on oublie (ou ajoute) souvent, à une place connue. Pas n'importe quel doublement :
# « fesait » -> « fessait » (de « fesser ») en serait un, alors que la faute est « faisait ».
_DOUBLINGS = (
    # Pas devant un « e » final : « appele » est « appelle » ou « appelé », « questione » « questionne » ou
    # « questionné ».
    (re.compile(r"ion(?=(?:el|elle|els|elles|aire|aires|er|é|ée|és|ées|ant|ement|ons|ez|ais|ait)$)"), "ionn"),
    (re.compile(r"(?<=[^aeiouyél])el(?=e(?:rai|ras|ra|rons|rez|ront|rais|rait|rions|riez|raient)$)"), "ell"),
    (re.compile(r"(?<=[^aeiouyét])et(?=e(?:rai|ras|ra|rons|rez|ront|rais|rait|rions|riez|raient)$)"), "ett"),
    (re.compile(r"addr"), "adr"),
    (re.compile(r"rrir$"), "rir"),
)


def _known_doublings(word: str) -> set[str]:
    """« professionel » -> « professionnel », « appelerai » -> « appellerai », « jeterai » -> « jetterai »,
    « addresse » -> « adresse », « courrir » -> « courir »."""
    found = set()
    for pattern, repl in _DOUBLINGS:
        for m in pattern.finditer(word):
            found.add(word[:m.start()] + repl + word[m.end():])
    return found


def is_confident(text: str, issue: Issue) -> bool:
    """Une correction est "sûre" (appliquée par la correction express) si :
    - c'est une faute d'accent ou de majuscule avec une suggestion évidente ;
    - les deux moteurs proposent la même correction de grammaire ;
    - les deux moteurs proposent le même mot et il y a très peu de candidats
      (sinon, "fesait" deviendrait "fessait" au lieu de "faisait") ;
    - c'est une correction de ponctuation/espaces à suggestion unique."""
    if (issue.replacements and rule_family(issue.rule_id) == "eleu_élisions_manquantes"
            and text[issue.start:issue.end].strip().lower() == "si" and re.match(r"ils?\b", text[issue.end:issue.end + 4], re.I)):
        return issue.replacements[0].lower() in ("s'", "s’")  # « si il » -> « s'il » : élision obligatoire, pas de style
    if not issue.replacements or issue.category is Category.STYLE or never_sure(issue):
        return False
    if issue.rule_id.startswith("corr_") and issue.category is not Category.GRAMMAR:
        return True  # règle du correcteur (engines/regles.py) : orthographe fréquente, « t » de liaison
    original = text[issue.start:issue.end]
    first = issue.replacements[0]
    if (rule_family(issue.rule_id).startswith("tu_") and len(issue.replacements) == 1
            and rule_family(issue.rule_id) != "tu_prénoms" and original.lower() not in _LOOSE_HYPHENS):
        # Locution figée à trait d'union : « Est ce que » -> « Est-ce que », « la bas » -> « là-bas »,
        # « au dessus » -> « au-dessus », « eux même » -> « eux-mêmes ». Seuls les espaces, les traits d'union
        # et les accents changent (et le « s » de « mêmes » après un pluriel).
        return _letters_only(original) == _letters_only(first) or (
            original.lower().endswith("même") and _letters_only(first) == _letters_only(original) + "s")
    if issue.category is Category.SPELLING and first in _known_doublings(original):
        return not (_capitalized_mid_sentence(text, issue.start) or _foreign_neighbour(text, issue))
    if issue.category is Category.SPELLING:
        if first.casefold() == original.casefold() and _next_to_number(text, issue):
            return False  # « place du 8 Mai 1945 », « rue du 14 Juillet » : un nom propre, pas une date
        if _accent_count(first) < _accent_count(original) and (
                original[:1].isupper() or re.search(r"(?:é|ée|és|ées)$", original.lower())):
            # « Sîn en akkadien » -> « Sin » : un nom écrit avec ses accents, exprès ; « avoir semblés » ->
            # « sembles » : la faute est le « s », pas l'accent.
            return False
        if first.lower() == original.lower() and any(ch.isupper() for ch in first[1:]):
            return False  # « el » -> « EL », « nt » -> « nT » : une majuscule inventée
        # "ecole" -> "école" est sûr ; "tache" -> "tâche"/"tachè" ne l'est pas.
        rivals = [r for r in issue.replacements[1:] if same_letters(original, r) and r.casefold() != first.casefold()]
        if same_letters(original, first) and not rivals:
            # Pas pour un nom propre ou un titre (« College », « Ramón »), ni au milieu d'anglais (« of the Dead »).
            return not (_capitalized_mid_sentence(text, issue.start) or _foreign_neighbour(text, issue))
        # « Nous avont fini » -> « avons » : une faute de frappe, et la seule suggestion à la terminaison
        # qu'impose le sujet.
        ending = subject_ending(text, issue.start)
        if (ending and first.endswith(ending) and one_edit(original, first)
                and sum(r.endswith(ending) for r in issue.replacements) == 1):
            return True
        return issue.agreed and len(issue.replacements) <= 3
    if issue.agreed:
        return True
    if issue.category is Category.TYPOGRAPHY:
        return len(issue.replacements) == 1 and (not _changes_words(original, first) or same_letters(original, first))
    return False


_SOLO_WINDOW = 12
# Contre-épreuves de la règle solo : 3 par phrase (120 au moins), et 10 s au plus pour un texte énorme.
# Un plafond fixe laissait la fin d'un long texte sans correction (400 lignes : seules les 120 premières).
_SOLO_MAX_TRIALS = 120
_SOLO_TRIALS_PER_SENTENCE = 3
_SOLO_TRIALS_SECONDS = 10.0
# Familles de règles Grammalecte dont la correction est fiable sans un second moteur pour la confirmer.
# Mesurées en injectant des fautes typiques (a/à, son/sont, on/ont, é/er, pluriels…) dans 2 500 phrases
# justes de la suite de tests de Grammalecte : chacune y a rétabli la phrase d'origine au moins 95 fois
# sur 100, sans rien toucher dans les phrases justes. Les autres corrigent trop souvent le mauvais mot :
# « Mes parents son partis » -> « son parti » (gn_mon_ton_son : 0 sur 43), « Nous les avons finis » -> « fini »
# (ppas_nous_vous_avoir : 0 sur 3). Certaines n'entrent qu'avec un garde-fou (voir solo_context_ok) :
# « c'étais » -> « s'étais » (conf_c_s_verbe : 5 sur 12 sans lui, 4 sur 4 avec).
_SOLO_RULES = frozenset({
    # à / a
    "conf_à_a", "conf_à_a_après_verbes", "conf_à_a_infinitif", "conf_à_a_locutions", "conf_à_a_verbes_locutions",
    "conf_a_à_verbe", "conf_a_à_substantifs",
    # homophones
    "conf_ces_ses_c_est", "conf_ce_ceux_se", "conf_se_ceux_ce", "conf_ce_se_verbe", "conf_ont", "conf_on_ont",
    "conf_ils_non_verbe", "conf_tu_non_verbe", "conf_son_sont", "conf_peu_peut_peux", "conf_la_là", "conf_est_et",
    "conf_et_est2", "conf_m_a_ma", "conf_c_s_verbe", "conf_mai_mais_mes", "conf_où_ou", "conf_quand_quant_qu_en", "conf_quant_quand_qu_en",
    "conf_dans_dan_d_en", "conf_nôtre_notre", "conf_ça_çà_sa", "conf_déterminant_fem_sing_verbe", "conf_leurs_verbe",
    "conf_c_est_était", "conf_près_prêt_pré", "conf_peut_être", "conf_peut_être2",
    # déterminant + nom
    "gn_les_1m", "gn_det_epi_plur_1m", "gn_ces_aux_pluriel_1m", "gn_quel_1m", "gn_quelle_1m", "gn_ce_1m", "gn_tout_det",
    "gn_toute_det", "gn_tous_det_nom", "gn_plusieurs_1m", "gn_quelque_1m", "gn_certains_1m", "gn_certaines_1m",
    "gn_nombre_chiffres_1m",
    # conjugaison, mode, infinitif
    "conj_je", "conj_tu", "conj_1s_2s_sans_sujet_xxxs", "conj_les_nom", "vmode_verbes_que_subjonctif",
    "vmode_j_aimerais_vinfi", "infi_falloir", "infi_vouloir", "infi_devoir", "infi_ne_pas_jamais_etc", "infi_qqch_de",
    "infi_par_pour_sans", "infi_verbe_de_verbe", "infi_verbes", "imp_verbe_groupe3_d",
    # participes passés (voir aussi solo_context_ok)
    "ppas_avoir", "ppas_être_confusion", "ppas_sujet_être_accord_pluriel", "ppas_ne_me_te_se_être",
    "ppas_3pl_mas_verbe_état", "ppas_3pl_fem_verbe_état", "ppas_3sg_fem_verbe_état", "ppas_nous_verbe_état",
    "ppas_pronom_avoir", "ppas_pronom_sujet_verbe_état_verbe", "ppas_n_m_t_l_avoir", "conf_sûr_sur",
    "conf_ont_on", "ppas_nous_vous_avoir", "conf_quel_que_soit",
    # Banc d'essai de la 0.1.11 (tests/banc_fautes.py) : verbe mal écrit après « je », « il », « on » (« Je
    # m'appel », voir solo_context_ok), locutions (« entrain », « sensé », « comme même », « en faite »,
    # « d'avantage »), « J'ai du partir », « Il y a peut de monde », « Le jour ou il est parti ». La conjugaison
    # après « il », « elle », « on », « ils », « vous » reste à la règle du correcteur (corr_conj_sujet) :
    # Grammalecte proposait « pensa » pour « il pensai » (80 bonnes sur 100), « Il » pour « Ils mange ».
    "conf_je_non_verbe", "conf_il_non_verbe", "conf_on_non_verbe", "conf_quelle_qu_elle", "conf_en_train_entrain",
    "conf_censé_sensé", "conf_quand_même", "conf_en_fait", "conf_davantage_d_avantages", "ppas_avoir_dû_vinfi",
    "conf_peut_peux_peu2", "conf_det_nom_où_pronom",
    # Vérifiées sur leurs phrases voisines justes (« C'est difficile, voir impossible » oui, « Il faut le voir,
    # voir le croire » non) : « Temps pis », « tout a fait », « voir » pour « voire », « Elle s'est faite
    # avoir », « C'est moi qui a », « La plupart des gens pense », « un espèce », « Mes amis m'ont aidés ».
    "conf_tant_temps_tan_1", "conf_tout_à_fait", "conf_voir_voire", "ppas_s_être_fait_infi", "conj_moi_qui",
    "conj_toi_qui", "conj_d_aucuns_la_plupart", "gn_un_1m", "ppas_m_t_avoir", "conf_tout_à_coup", "conf_tout_à_l_heure",
    # Mesurées sur les injections (ppas_3sg_mas_verbe_état : 28 sur 28) ou sans phrase voisine à risque : « Il si
    # connaît », « Ils se sont rendus compte », « deux milles euros », « dés demain » (voir solo_context_ok).
    "conf_s_y_si", "ppas_s_être_rendu_compte", "gn_mille", "ppas_3sg_mas_verbe_état", "conf_dès_des_dés",
    # « d'y allé » -> « aller », « Est-se » -> « Est-ce », « ne peut pas allé » -> « aller », « deux heure » ->
    # « heures » (voir solo_context_ok).
    "infi_de_verbe", "inte_verbes_composés_interrogatifs", "infi_pouvoir", "gn_nombre_lettres_1m",
})
# Jamais sûres, même avec l'accord des deux moteurs : le participe d'un verbe pronominal s'accorde selon
# son complément (« nous nous sommes lavés », mais « nous nous sommes téléphoné »), ce qu'aucun ne sait voir.
# Un nom propre (« Helene ») n'est pas accentué d'office : la personne l'écrit peut-être ainsi.
_NEVER_SURE_RULES = ("ppas_nous_nous_être", "ppas_vous_vous_être", "grammalecte_nom_propre")
_RULE_FAMILY_RE = re.compile(r"^gv?\d+__(.+?)(?:__b\d+_a\d+(?:_\d+)?)?$")
# Participe imposé après un « a » ou un « est » qui sont peut-être « à » ou « et » : « Il y a manger »
# (« à manger »), « Son seul plaisir est manger », « Il a appris a nager ». Sauf juste après un sujet.
_PARTICIPLE_FAMILIES = frozenset({"ppas_avoir", "ppas_être_confusion", "ppas_ne_me_te_se_être"})
_CONFUSABLE_AUXILIARIES = frozenset({"a", "est"})
_OBJECT_PRONOUNS = frozenset({"les", "le", "la", "lui", "leur", "nous", "vous", "en"})
_SUBJECTS = frozenset({"il", "elle", "on", "ça", "cela", "qui", "lui", "tout", "rien", "personne"})
_WORD_BEFORE_RE = re.compile(r"([\w'’-]+)\W*$")
# Verbes pronominaux dont le pronom est complément indirect : leur participe ne s'accorde pas (« Ils se sont
# téléphoné », « elles se sont plu », « ils se sont succédé »), ou il est suivi d'un infinitif (« elles se sont
# fait avoir »). Les deux moteurs voudraient « téléphonés » : jamais d'office.
_INDIRECT_PRONOMINAL = frozenset("""téléphoné parlé souri plu déplu complu succédé ressemblé menti nui suffi survécu
    ri convenu écrit dit demandé donné promis permis rendu répondu voulu fait laissé""".split())
_REFLEXIVE = ("se", "me", "te", "nous", "vous", "s'", "m'", "t'")
# Après « que » et un pronom sujet, le verbe est peut-être au subjonctif : « Il faut que je fait » n'est pas
# « je fais » (les deux moteurs le proposent) mais « je fasse », « à ce qu'il ne sais point » est « sache ».
# La conjugaison proposée, à l'indicatif, n'est sûre qu'après les mots qui appellent l'indicatif.
_QUE_SUBJECT_RE = re.compile(
    r"(?:^|[^\w'’])(?:([\w'’-]+)\s+)?(quoi|lors|puis)?qu(?:e\s+|['’]\s*)(?:je\s+|j['’]|tu\s+|il\s+|elle\s+|on\s+|"
    r"nous\s+|vous\s+|ils\s+|elles\s+)(?:(?:ne|me|te|se|le|la|les|lui|leur|y|en|nous|vous)\s+|[nmtsl]['’])*$",
    re.IGNORECASE)
# « Ils on l'air contents » : « on » est « ont », pas le sujet d'un verbe mal écrit (« aire »).
_ON_NOT_SUBJECT_RE = re.compile(r"\b(?:ils|elles|eux|nous|vous|qui|(?:les|des|mes|tes|ses|nos|vos|leurs|ces)\s+[\w-]+)"
                                r"\s+on\s+(?:\S+\s+|\S*['’])?$", re.IGNORECASE)
# « Je voudrais d'avantage de temps » -> « davantage » ; mais « pas d'avantage fiscal », « toutes sortes
# d'avantages » : le nom « avantage ».
_NOUN_AVANTAGE_BEFORE = frozenset("""pas point plus jamais guère aucun sans peu beaucoup trop assez autant moins tant
    sorte sortes genre type plein manque""".split())
_AFTER_DAVANTAGE_RE = re.compile(r"\s*(?:[.!?,;:)…]|$)|\s+(?:de\b|d['’]|que\b|qu['’])", re.IGNORECASE)
# « Je vais acheté du pain » : après aller ou venir, un participe en -é est l'infinitif mal écrit seulement si un
# complément suit (« Je vais fatigué au travail », « Ils vont pressés au marché » sont justes).
_GO_COME = frozenset("""vais vas va allons allez vont allais allait allions alliez allaient irai iras ira irons irez
    iront irais irait irions iriez iraient aille ailles aillent allé allée allés allées aller allant viens vient venons
    venez viennent venais venait venions veniez venaient venu venue venus venues venir venant""".split())
_OBJECT_START_RE = re.compile(r"\s*(?:[.!?…]|$)|\s+(?:[ldLD]['’]|(?:du|des|un|une|le|la|les|mon|ma|mes|ton|ta|tes|son|"
                              r"sa|ses|notre|nos|votre|vos|leur|leurs|ce|cet|cette|ces|quelques|plusieurs)\b)")


def rule_family(rule_id: str) -> str:
    """« gv1__ppas_avoir__b1_a1_1 » -> « ppas_avoir » (famille de la règle, sans niveau ni variante)."""
    m = _RULE_FAMILY_RE.match(rule_id)
    return m.group(1) if m else rule_id


def solo_rule(issue: Issue) -> bool:
    """Moteur seul : Grammalecte n'est cru que pour les familles mesurées ; LanguageTool seul (texte anglais,
    Grammalecte désactivé) garde la règle d'avant, une suggestion unique sans voisine."""
    family = rule_family(issue.rule_id)
    # imp_union_* : trait d'union de l'impératif (« Dis moi » -> « Dis-moi », « Va y » -> « Vas-y ») ; Grammalecte
    # ne le propose qu'en tête de phrase, jamais devant un article (« Prends la porte »).
    # vmode_…_subjonctif : subjonctif après un déclencheur que Grammalecte reconnaît (« veiller à ce que », « bien
    # que », « non que ») ; toutes à 100 % sur les injections, peu nombreuses chacune.
    return (issue.source != "grammalecte" or family in _SOLO_RULES or family.startswith(("corr_", "imp_union_"))
            or (family.startswith("vmode_") and family.endswith("_subjonctif")))


def never_sure(issue: Issue) -> bool:
    return rule_family(issue.rule_id).startswith(_NEVER_SURE_RULES)


def _words_before(text: str, pos: int, count: int) -> list[str]:
    words = []
    for _ in range(count):
        m = _WORD_BEFORE_RE.search(text, 0, pos)
        if not m:
            break
        words.append(m.group(1).lower().replace("’", "'"))
        pos = m.start()
    return words


def solo_context_ok(text: str, issue: Issue, agreed: bool = False) -> bool:
    """Ce qui entoure la faute laisse-t-il une autre lecture, que Grammalecte seul ne peut pas exclure ?
    `agreed` : les deux moteurs proposent cette correction (le genre choisi n'est alors plus un hasard)."""
    family = rule_family(issue.rule_id)
    if family.startswith("corr_"):
        return True  # règles du correcteur : leurs garde-fous sont dans engines/regles.py
    before = _words_before(text, issue.start, 2)
    if (family.startswith("gn_") or family == "conj_les_nom") and _capitalized_mid_sentence(text, issue.start):
        return False  # « les Trail Blazers » -> « Trails », « les syndicats Force ouvrière » -> « Forcent »
    if family.startswith(("conf_à_a", "conf_a_à")):
        prev = re.search(r"([\w'’-]+)\W*$", text[max(0, issue.start - 40):issue.start])
        if prev and _capitalized_mid_sentence(text, issue.start - (len(text[max(0, issue.start - 40):issue.start]) - prev.start(1))):
            return False  # « La Gères a une station » : « Gères » est un nom de rivière, pas « tu gères »
    if family == "gn_nombre_chiffres_1m" and re.search(r"\b(?:1[0-9]|20)\d\d(?:\s*[-–]\s*\d{2,4})?\s*$",
                                                        text[max(0, issue.start - 12):issue.start]):
        return False  # « 1943 débarquement de Salerne » : une année, pas une quantité
    word = text[issue.start:issue.end].lower()
    first = issue.replacements[0] if issue.replacements else ""
    if (word in _INDIRECT_PRONOMINAL and first.lower().startswith(word) and len(first) > len(word)
            and any(w in _REFLEXIVE or w.startswith(_REFLEXIVE[5:]) for w in _words_before(text, issue.start, 3))):
        return False
    if family.startswith("conj_") and re.search(r"[\"“«]\s*\w+\s*[\"”»]\s*$", text[max(0, issue.start - 20):issue.start]):
        return False  # « les pronoms “je” est “tu” » : « je » cité, pas sujet
    if family.startswith("conj_"):
        m = _QUE_SUBJECT_RE.search(text, max(0, issue.start - 80), issue.start)
        if m:
            prefix, before_que = (m.group(2) or "").lower(), (m.group(1) or "").lower()
            # « ce que tu en penses » : indicatif ; pas « à ce que » (« veiller à ce que tu viennes »).
            what = before_que == "ce" and not re.search(r"\bà\s+ce\s+qu", text[max(0, m.start() - 4):issue.start], re.I)
            if not (prefix in ("lors", "puis") or (not prefix and (before_que in INDICATIVE_BEFORE_QUE or what))):
                return False
    if family.endswith("_non_verbe") and not (same_sound(word, first) or word == "à" or (word, first) == ("et", "est")):
        # « Je m'appel » -> « appelle », « Tu à raison » -> « as » ; mais « je ni prendrai » n'est pas « je nie »
        # (« je n'y »).
        return False
    if family in ("conf_on_non_verbe", "conj_on") and _ON_NOT_SUBJECT_RE.search(text, max(0, issue.start - 60), issue.start):
        return False
    if family == "conf_davantage_d_avantages":
        if (text[issue.start:issue.end].lower().replace("’", "'") != "d'avantage"
                or (before and before[0] in _NOUN_AVANTAGE_BEFORE) or not _AFTER_DAVANTAGE_RE.match(text, issue.end)):
            return False
    if family in _PARTICIPLE_FAMILIES and before and before[0] in _CONFUSABLE_AUXILIARIES:
        # Sauf juste après un sujet (« qu'il est partit ») ou un pronom complément (« Il les a prit ») ; pas
        # « y » : « Il y a manger ».
        prev = before[1].replace("’", "'").split("'")[-1] if len(before) > 1 else ""
        if (prev not in _SUBJECTS and prev not in _OBJECT_PRONOUNS
                and not (len(before) > 1 and before[1] in ("l'", "m'", "t'"))):
            return False
    if family == "ppas_ne_me_te_se_être":
        # « Se serait attaquer », « s'est être » : « se » mis pour « ce » en tête de phrase, sans sujet.
        if text[issue.start:issue.end].lower() == "être" or re.match(r"[Ss]e\b|[Ss]['’]", _sentence_at(text, issue.start, issue.end)):
            return False
    if family == "conj_les_nom":
        # « Les Misérables est un roman » : un titre ; « Les contes est légendes » : « est » mis pour « et ».
        # « Les femmes porte toutes » : « portent » (verbe), pas « portes » (nom).
        if text[issue.start:issue.end].lower() == "est" or not first.endswith("nt"):
            return False
        m = re.search(r"\b[Ll]es\s+(\w)", text[max(0, issue.start - 80):issue.start])
        if m and m.group(1).isupper():
            return False
    if family == "infi_de_verbe" and (not word.endswith("é") or re.search(
            r"\b(?:rien|ça|sa|cela|quoi|personne|chose|aucun|aucune|quelqu['’]un|autre|y\s+[aà]-t-il|il\s+y\s+a)\s+"
            r"(?:[\w'’-]+,?\s+){0,4}(?:de|d['’])\s*(?:\w+\s+)?$", text[max(0, issue.start - 60):issue.start], re.I)):
        # « rien de changé », « qu'y a-t-il de changé ? », « je n'ai rien mangé de souillé » : « de » + participe,
        # juste. Et seulement le participe en -é mis pour l'infinitif (pas « de m'a part » -> « avoir »).
        return False
    if family == "infi_pouvoir" and (not (word.endswith("é") and first.lower() == word[:-1] + "er") or re.search(
            r"\b(?:un|très|trop|si|assez|bien|fort|soit)\s+peu[tx]\s+(?:\w+\s+)?$", text[max(0, issue.start - 30):issue.start],
            re.IGNORECASE)):
        return False  # « Il est un peut fatigué » : « peut » est « peu », « fatigué » est juste
    if family == "gn_nombre_lettres_1m" and (
            re.search(r"\b(?:les|tous|toutes|à)\s+\w+\s+$", text[max(0, issue.start - 25):issue.start], re.IGNORECASE)
            or word in ("besoin", "envie", "peur", "faim", "soif", "raison", "tort", "honte") or first.endswith(("ées", "és"))):
        # « nous avions tous les deux besoin », « le basket-ball à trois créée par » : le nombre ne compte pas ce nom
        return False
    if family == "inte_verbes_composés_interrogatifs" and word != "se" and not same_sound(
            re.sub(r"[-‐]t?$", "", word), re.sub(r"[-‐]t?$", "", first.lower())):
        return False  # « Serai-il » : « Serait-il » plutôt que « Sera-t-il » ; seulement la forme qui sonne pareil
    if family.startswith("imp_union_"):
        after = text[issue.end:issue.end + 30]
        if ((before and before[0] in ("je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles"))
                or re.match(r"\s+\w+é\b", after) or (re.search(r"\b(?:le|la|les)$", word)
                                                       and re.match(r"\s+(?:le|la|les|un|une|des|du|mon|ma|mes|ton|ta|tes|son"
                                                                    r"|sa|ses|notre|nos|votre|vos|leur|leurs|ce|cet|cette|ces)\b", after))):
            # « Nous convenons en », « Sachons nous conformé » (« conformer » : le pronom va avec l'infinitif),
            # « Laisse la son nom » (« là »).
            return False
    if family == "gn_mille" and re.match(r"\s+(?:nautiques?|marins?|anglais|terrestres?|romains?)\b", text[issue.end:issue.end + 12]):
        return False  # « dix milles nautiques » : le mille, unité de longueur, prend un « s »
    if family == "conf_dès_des_dés" and not (word == "dés" or re.match(
            r"\s+(?:demain|maintenant|aujourd['’]hui|lors|que\b|qu['’]|hier)", text[issue.end:issue.end + 14], re.I)):
        return False  # « dès demain », « dès que » ; pas « des 20 heures » (une autre faute à côté, « aux lieu »)
    if family == "conf_son_sont" and word == "sont" and re.search(r"\b(?:que|qu['’]|qui|dont)\s+(?:[\w'’-]+\s+){0,4}$",
                                                                text[max(0, issue.start - 60):issue.start], re.IGNORECASE):
        return False  # « Les livres que tu m'as prêtés sont super » : fin de la relative, « sont » est le verbe
    if family == "conf_ont_on" and not _PARTICIPLE_AFTER_RE.match(text, issue.end):
        # « C'est eux qui on gagné » -> « ont » ; pas « à Paris on ce les procure » (« se » mal écrit).
        return False
    if family == "ppas_nous_vous_avoir":
        # « Vous avez finis vos devoirs » -> « fini » : un complément d'objet suit, le participe est invariable.
        # Sans lui, l'accord peut être juste (« la galette que nous avons mangée », « nous avons partie gagnée »).
        word = text[issue.start:issue.end].lower()
        if not (len(first) < len(word) and word.startswith(first.lower()) and _DETERMINER_AFTER_RE.match(text, issue.end)):
            return False
    if family == "ppas_n_m_t_l_avoir":
        # « dont je t'ai parler » -> « parlé » ; mais « m'a poche » (pour « ma poche ») n'est pas « m'a poché » :
        # seulement l'infinitif en -er mis pour le participe (25 bonnes sur 35 sans ce garde-fou, et les 10
        # dégâts étaient tous de ce genre).
        # Ou une autre forme qui se prononce comme le participe : « Je l'ai fais » -> « fait ».
        word = text[issue.start:issue.end]
        if not same_sound(word, first):
            return False
    if family == "ppas_m_t_avoir" and (first.lower() != word[:-1] or re.search(
            r"\bqu(?:e\s|['’])", text[max(0, issue.start - 30):issue.start], re.IGNORECASE)):
        # « Mes amis m'ont aidés » -> « aidé » : seulement le pluriel retiré, le genre écrit reste. Pas après
        # « que » : « Les livres que tu m'as prêtés » s'accorde avec « livres ».
        return False
    if family == "conf_sûr_sur":
        # « Je suis sur que tu vas réussir », « sur de moi », « Bien sur, je viens » -> « sûr » ; jamais « sur le
        # toit », ni « posé bien sur la table ».
        if not (re.match(r"\s+(?:que\b|qu['’]|de\b|d['’])", text[issue.end:issue.end + 6], re.IGNORECASE)
                or (before and before[0] == "bien" and re.match(r"\s*(?:[,.!?;:…]|$)", text[issue.end:issue.end + 2]))):
            return False
    if family == "conf_c_s_verbe":
        # « Il c'est trompé » -> « s'est » ; mais « c'étais eux » (pour « c'était ») n'est pas « s'étais ».
        m = re.search(r"\b(\w+)\s+$", text[max(0, issue.start - 20):issue.start])
        if not m or m.group(1).lower() not in ("il", "elle", "on", "ils", "elles"):
            return False
    if family in ("ppas_pronom_avoir", "ppas_pronom_sujet_verbe_état_verbe"):
        # Seule la fin du participe change (« put » -> « pu », « partit » -> « partie ») : pas « Elle n'a pas
        # sont mot de passe » -> « été », ni « a elle seule » -> « eu ».
        word = text[issue.start:issue.end]
        if not first or word.lower().rstrip("tse") != first.lower().rstrip("es") or len(word.rstrip("tse")) < 2:
            return False
    if family == "infi_verbes":
        word = text[issue.start:issue.end]
        if (not before or before[0] not in _GO_COME or not word.endswith("é") or first != word[:-1] + "er"
                or not _OBJECT_START_RE.match(text, issue.end)):
            return False
    if not agreed and len(issue.replacements) > 1 and first.endswith("s") and issue.replacements[1] == first[:-1] + "es":
        # Masculin pluriel proposé au hasard (« Ma mère et ma sœur sont partit » : « partis » ou « parties » ?) :
        # seulement quand le sujet le dit (« ils »).
        if "ils" not in _words_before(text, issue.start, 3):
            return False
    return True


def solo_candidate(issue: Issue, text: str | None = None) -> bool:
    """Correction de grammaire qu'un moteur seul peut appliquer : une seule suggestion, la même au masculin
    et au féminin (« allé / allée » : la première, comme le ferait un correcteur humain qui ne connaît pas
    l'auteur), ou celle que le contexte impose (voir _solo_pick, qui la met en tête)."""
    if issue.confident or issue.category is not Category.GRAMMAR or not issue.replacements:
        return False
    if not solo_rule(issue):
        return False
    if rule_family(issue.rule_id).startswith("corr_"):
        return True  # règle du correcteur (engines/regles.py) : sa correction est en tête, le reste vient de Grammalecte
    first = issue.replacements[0]
    if text is not None and len(issue.replacements) > 1:
        pick = _solo_pick(text, issue)
        if pick is not None:
            if pick != first:
                issue.replacements = [pick] + [r for r in issue.replacements if r != pick]
            return True
    feminine = first[:-1] + "es" if first.endswith("s") else first + "e"
    return all(r in (feminine, first + "e") for r in issue.replacements[1:])  # « allés / allées », « permis / permise »


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
        if not solo_candidate(issue, text) or not solo_context_ok(text, issue):
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
        self._ends = [m.end() for m in _SENTENCE_END_RE.finditer(text)]
        self.budget = max(_SOLO_MAX_TRIALS, _SOLO_TRIALS_PER_SENTENCE * (len(self._ends) + 1))
        self.deadline = time.monotonic() + _SOLO_TRIALS_SECONDS
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
            if self.budget <= 0 or time.monotonic() > self.deadline:
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


def _anchor(text: str, start: int, end: int) -> str:
    """Le passage [start, end] avec le mot qui le précède et celui qui le suit."""
    before = re.search(r"[\w'’-]+\W*$", text[max(0, start - 40):start])
    after = re.match(r"\W*[\w'’-]+", text[end:end + 40])
    return text[start - (len(before.group(0)) if before else 0):end + (len(after.group(0)) if after else 0)]


def _sentence_at(text: str, start: int, end: int) -> str:
    """La phrase qui contient [start, end], sans les espaces autour."""
    lo = max((m.end() for m in _SENTENCE_END_RE.finditer(text, 0, start)), default=0)
    hi = next((m.end() for m in _SENTENCE_END_RE.finditer(text, end)), len(text))
    return text[lo:hi].strip()


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
