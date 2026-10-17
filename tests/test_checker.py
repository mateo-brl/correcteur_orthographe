from __future__ import annotations

import re
import threading
import time

import pytest

from correcteur.checker import Checker, guess_language, is_confident, merge_issues, split_confident
from correcteur.config import PersonalDictionary, Settings
from correcteur.engines.base import Engine, EngineError
from correcteur.models import Category, Issue


class FakeEngine(Engine):
    def __init__(self, settings, name, found=None, delay=0.0, error=None, languages=("fr",)):
        super().__init__(settings)
        self.name = name
        self.found = found or (lambda text: [])
        self.delay = delay
        self.error = error
        self.languages = languages
        self.calls = 0

    def is_enabled(self):
        return True

    def supports(self, language):
        return language == "auto" or any(language.startswith(l) for l in self.languages)

    def check(self, text, language):
        self.calls += 1
        time.sleep(self.delay)
        if self.error:
            raise EngineError(self.error)
        return present(self.found(text))  # un mot absent du texte (déjà corrigé) : pas de remarque


def find(text, word, occurrence=0):
    """Position d'un mot entier (comme un vrai moteur), ou d'un signe."""
    import re

    pattern = re.escape(word)
    if word[:1].isalnum():
        pattern = r"(?<!\w)" + pattern
    if word[-1:].isalnum():
        pattern += r"(?!\w)"
    matches = list(re.finditer(pattern, text))
    return matches[occurrence].start() if len(matches) > occurrence else None


# Règle par défaut des faux moteurs : une famille que la règle « solo » accepte (voir _SOLO_RULES).
RULE = "g3__gn_les_1m__b1_a1_1"


def issue(text, word, repl, source, category=Category.GRAMMAR, rule=RULE, occurrence=0, message="msg"):
    start = find(text, word, occurrence)
    if start is None:
        return None
    return Issue(start, start + len(word), message, list(repl), category, rule, source)


def present(items):
    return [i for i in items if i is not None]


TEXT = "Les résultats que j'ai obtenu sont bon et ecole ferme , fesait-il."


def g_issues(text):
    return present([
        issue(text, "obtenu", ["obtenus"], "grammalecte", rule="G_PPAS"),
        issue(text, "sont", ["son"], "grammalecte", rule="G_SON"),
        issue(text, "bon", ["bons"], "grammalecte", rule="G_ACCORD"),
        issue(text, "ecole", ["école", "Écoles"], "grammalecte", Category.SPELLING, "G_ORTHO"),
        issue(text, " ,", [","], "grammalecte", Category.TYPOGRAPHY, "G_VIRG"),
        issue(text, "fesait", ["fessait", "vessait", "cessait"], "grammalecte", Category.SPELLING, "G_ORTHO"),
    ])


def lt_issues(text):
    return present([
        issue(text, "obtenu", ["obtenus"], "languagetool", rule="QUE_AVOIR"),
        issue(text, "sont bon", ["sont bons", "sont bonnes", "son bon"], "languagetool", rule="SONT_SON"),
        issue(text, "ecole", ["école"], "languagetool", Category.SPELLING, "FR_SPELLING_RULE"),
        issue(text, " ,", [","], "languagetool", Category.TYPOGRAPHY, "COMMA_WS"),
        issue(text, "fesait", ["fessait", "ferait"], "languagetool", Category.SPELLING, "FR_SPELLING_RULE"),
    ])


@pytest.fixture
def checker(home):
    s = Settings()
    engines = [FakeEngine(s, "grammalecte", g_issues), FakeEngine(s, "languagetool", lt_issues)]
    c = Checker(s, PersonalDictionary(), engines=engines)
    yield c
    c.close()


def by_text(result_issues, text):
    return {text[i.start:i.end]: i for i in result_issues}


def test_merge_and_confidence(checker):
    result = checker.check(TEXT)
    found = by_text(result.issues, TEXT)
    assert set(found) == {"obtenu", "sont", "bon", "ecole", " ,", "fesait"}
    # Même correction par les deux moteurs : fusion + sûre.
    assert found["obtenu"].sources == ("grammalecte", "languagetool") and found["obtenu"].confident
    # "sont bon" de LanguageTool rejoint "bon" -> "bons" (sa 1re suggestion), pas "sont" -> "son".
    assert "languagetool" in found["bon"].sources and found["bon"].confident
    assert found["bon"].replacements[:2] == ["bons", "bonnes"]
    assert found["sont"].sources == ("grammalecte",) and not found["sont"].confident
    # Accent seul : sûr. Ponctuation confirmée : sûre.
    assert found["ecole"].confident and found["ecole"].replacements[0] == "école"
    assert found[" ,"].confident
    # Orthographe ambiguë : jamais appliquée automatiquement.
    assert not found["fesait"].confident


def test_merged_issue_keeps_every_rule(checker):
    """Pour ne plus signaler une faute, il faut couper la règle de chaque moteur :
    sinon l'autre moteur la resignale à la vérification suivante."""
    found = by_text(checker.check(TEXT).issues, TEXT)
    assert found["obtenu"].rule_id == "G_PPAS"
    assert found["obtenu"].rules == ("G_PPAS", "QUE_AVOIR")
    checker.settings.ignored_rules = list(found["obtenu"].rules)
    assert "obtenu" not in by_text(checker.check(TEXT).issues, TEXT)


def test_spelling_rule_never_joins_a_grammar_fault():
    text = "Il a mangé la tarte a la crème."
    g = issue(text, "a", ["à"], "grammalecte", rule="CONF_A", occurrence=1)
    lt = issue(text, "a", ["à"], "languagetool", Category.SPELLING, "MORFOLOGIK_RULE_FR", occurrence=1)
    merged = merge_issues(text, [g, lt])
    assert len(merged) == 1 and merged[0].category is Category.GRAMMAR
    assert merged[0].rules == ("CONF_A",)


ON_JOUE = "Les enfants on joué ensemble."


def on_joue_engines(s):
    """Signalements réels des deux moteurs pour « on joué » (rapport d'un utilisateur, Debian 13)."""
    g = lambda t: present([issue(t, "joué", ["joue", "jouait", "joua"], "grammalecte", rule="g2__conj_on")])
    lt = lambda t: present([issue(t, "on joué", ["ont joué", "on joue"], "languagetool", rule="ON_ONT")])
    return [FakeEngine(s, "grammalecte", g), FakeEngine(s, "languagetool", lt)]


def test_second_choice_is_not_an_agreement():
    """Grammalecte veut « on joue », LanguageTool « ont joué » et n'a « on joue » qu'en 2e choix :
    ce n'est pas un accord. La correction n'est pas sûre, et « ont joué » n'est pas perdue."""
    [merged] = merge_issues(ON_JOUE, [e.check(ON_JOUE, "fr")[0] for e in on_joue_engines(Settings())])
    assert merged.original(ON_JOUE) == "on joué"  # zone élargie : « ont joué » change aussi « on »
    assert merged.replacements[:2] == ["on joue", "ont joué"]  # désaccord : les deux choix en tête
    assert not merged.agreed and not is_confident(ON_JOUE, merged)


def test_autocorrect_does_not_apply_a_second_choice(home):
    s = Settings()
    c = Checker(s, PersonalDictionary(), engines=on_joue_engines(s))
    new_text, applied, remaining, _ = c.autocorrect(ON_JOUE)
    assert new_text == ON_JOUE and not applied
    assert [ON_JOUE[i.start:i.end] for i in remaining] == ["on joué"]
    c.close()


def word_issue(text, word, repl, source, rule=RULE):
    """Comme `issue`, mais seulement pour le mot entier (« enfant » n'est pas trouvé dans « enfants »)."""
    m = re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text)
    return Issue(m.start(), m.end(), "msg", list(repl), Category.GRAMMAR, rule, source) if m else None


def test_partial_agreement_falls_back_on_the_offline_decision(home):
    """« les enfant » : Grammalecte veut « enfants », LanguageTool préfère « l'enfant » et n'a « les enfants »
    qu'en 2e choix. Même forme que « on joué », où LanguageTool a raison : un score ne tranche pas. On
    reprend la décision hors ligne (Grammalecte seul, contre-épreuve), qui corrige « enfant » mais pas
    « joué », où Grammalecte hésite. La correction en ligne n'en fait pas moins que hors ligne (0.1.2)."""
    text = "Hier, les enfant on joué ensemble."

    def g(t):
        return present([word_issue(t, "enfant", ["enfants"], "grammalecte"),
                        word_issue(t, "joué", ["joue", "jouait", "joua"], "grammalecte", "g2__conj_on")])

    def lt(t):
        return present([word_issue(t, "les enfant", ["l'enfant", "les enfants"], "languagetool"),
                        word_issue(t, "on joué", ["ont joué", "on joue"], "languagetool", "ON_ONT")])

    s = Settings()
    for engines in ([FakeEngine(s, "grammalecte", g), FakeEngine(s, "languagetool", lt)],
                    [FakeEngine(s, "grammalecte", g)]):
        c = Checker(s, PersonalDictionary(), engines=engines)
        new_text, applied, remaining, _ = c.autocorrect(text)
        assert new_text == "Hier, les enfants on joué ensemble."
        assert [new_text[i.start:i.end] for i in remaining] == ["joué"]  # vu à la dernière passe (locale)
        c.close()


def test_languagetool_fix_confirmed_when_it_silences_a_weak_local_remark(home):
    """« Mes parents son partis » : LanguageTool seul propose « sont », Grammalecte signale « partis » à côté
    avec une règle peu fiable (« parti »). Corriger « son » fait taire Grammalecte : la correction est sûre.
    Pas avec une règle fiable de Grammalecte (deux lectures), ni pour autre chose qu'un homophone."""
    text = "Mes parents son partis."

    def engines(s, rule, fix="sont"):
        g = lambda t: present([word_issue(t, "partis", ["parti"], "grammalecte", rule)] if "son partis" in t else [])
        lt = lambda t: present([word_issue(t, "son", [fix], "languagetool", "SON_SONT")])
        return [FakeEngine(s, "grammalecte", g), FakeEngine(s, "languagetool", lt)]

    s = Settings()
    for rule, fix, expected in (("g3__gn_mon_ton_son_1m__b1_a4_1", "sont", "Mes parents sont partis."),
                                (RULE, "sont", None), ("g3__gn_mon_ton_son_1m__b1_a4_1", "sont là", text)):
        c = Checker(s, PersonalDictionary(), engines=engines(s, rule, fix))
        new_text = c.autocorrect(text)[0]
        if expected is None:
            assert "sont" not in new_text
        else:
            assert new_text == expected
        c.close()


def test_solo_only_trusts_reliable_rule_families(home):
    """Seul, Grammalecte n'est cru que pour des familles de règles vérifiées : « Mes parents son partis »
    -> « son parti » (gn_mon_ton_son prend « son » pour un déterminant) n'est jamais appliqué."""
    s = Settings()
    text = "Mes parents son partis en vacances."
    g = lambda t: present([word_issue(t, "partis", ["parti"], "grammalecte", "g3__gn_mon_ton_son_1m__b1_a4_1")])
    c = Checker(s, PersonalDictionary(), engines=[FakeEngine(s, "grammalecte", g)])
    assert c.autocorrect(text)[0] == text
    c.close()


def test_pronominal_participle_is_never_sure():
    """« Nous nous sommes téléphoné » : les deux moteurs veulent « téléphonés », à tort (téléphoner à)."""
    text = "Nous nous sommes téléphoné."
    rule = "gv1__ppas_nous_nous_être_verbe__b1_a1_1"
    found = [issue(text, "téléphoné", ["téléphonés", "téléphonées"], "grammalecte", rule=rule),
             issue(text, "téléphoné", ["téléphonés"], "languagetool", rule="AGREEMENT")]
    [merged] = merge_issues(text, found)
    assert merged.agreed and not is_confident(text, merged)


def test_curly_apostrophe_elsewhere_does_not_split_a_fault(home):
    """Un « ’ » ailleurs dans le texte : Grammalecte propose « C’est », LanguageTool « C'est ». C'est la même
    correction (une seule remarque, sûre), écrite avec l'apostrophe du passage corrigé ou du texte."""
    s = Settings()
    text = "Ses une très bonne idée. Aujourd’hui il pleut."
    g = lambda t: present([word_issue(t, "Ses", ["C’est"], "grammalecte", "g2__conf_ces_ses_c_est__b1_a1_1")])
    lt = lambda t: present([word_issue(t, "Ses", ["C'est"], "languagetool", "SES_CEST")])
    c = Checker(s, PersonalDictionary(), engines=[FakeEngine(s, "grammalecte", g), FakeEngine(s, "languagetool", lt)])
    [found] = c.check(text).issues
    assert found.agreed and found.confident and found.replacements == ["C’est"]
    c.close()


def test_autocorrect_only_applies_confident(checker):
    new_text, applied, remaining, _ = checker.autocorrect(TEXT)
    assert new_text == "Les résultats que j'ai obtenus sont bons et école ferme, fesait-il."
    assert {new_text[i.start:i.end] for i in remaining} == {"sont", "fesait"}  # douteuses : à vérifier
    assert len(applied) == 4


def test_autocorrect_resolves_cascades_left_to_right(home):
    """"les enfant était ravis" : les deux moteurs veulent "ravi" (accord avec
    "était"), mais la bonne correction est "enfants ... étaient ravis"."""
    s = Settings()

    def g(t):
        found = [issue(t, "enfant", ["enfants"], "grammalecte")]
        if find(t, "enfants") is not None:
            found.append(issue(t, "était", ["étaient"], "grammalecte"))
        else:
            found.append(issue(t, "ravis", ["ravi"], "grammalecte"))
        return present(found)

    def lt(t):
        return present([issue(t, "enfant", ["enfants"], "languagetool"), issue(t, "ravis", ["ravi"], "languagetool")])

    lt_engine = FakeEngine(s, "languagetool", lt)
    c = Checker(s, PersonalDictionary(), engines=[FakeEngine(s, "grammalecte", g), lt_engine])
    new_text, applied, remaining, _ = c.autocorrect("Hier, les enfant était ravis de venir.")
    assert new_text == "Hier, les enfants étaient ravis de venir."
    assert lt_engine.calls == 1  # passes suivantes : moteur local seulement
    c.close()


def test_autocorrect_never_touches_a_fixed_passage_twice(home):
    s = Settings()
    # Un moteur "têtu" qui signale toujours le même passage.
    stubborn = lambda t: [Issue(0, 3, "", [t[:3] + "x"], Category.GRAMMAR, "R", "grammalecte", agreed=True)]
    c = Checker(s, PersonalDictionary(), engines=[FakeEngine(s, "grammalecte", stubborn)])
    new_text, applied, _, _ = c.autocorrect("abc def")
    assert new_text == "abcx def" and len(applied) == 1
    c.close()


def test_autocorrect_without_local_engine_stays_cautious(home):
    """Sans moteur local (texte anglais, Grammalecte désactivé), les passes
    suivantes n'ont personne pour confirmer : une correction douteuse de la
    1re passe ne devient jamais sûre."""
    s = Settings()

    def lt(t):
        return present([
            issue(t, "teh", ["the", "ten", "tea"], "languagetool", Category.SPELLING, "SPELL"),
            issue(t, " ,", [","], "languagetool", Category.TYPOGRAPHY, "COMMA_WS"),
        ])

    c = Checker(s, PersonalDictionary(), engines=[FakeEngine(s, "languagetool", lt, languages=("en",))])
    new_text, applied, remaining, _ = c.autocorrect("I saw teh car , then left.", "en-US")
    c.close()
    assert new_text == "I saw teh car, then left."
    assert len(applied) == 1
    assert [new_text[i.start:i.end] for i in remaining] == ["teh"]


def test_autocorrect_does_not_wait_forever_for_a_slow_engine(home):
    """Réseau lent : la correction express part avec Grammalecte seul (règle
    "solo") au lieu de laisser l'utilisateur attendre sans rien voir."""
    s = Settings()
    text = "Je vais a la plage demain."
    g = FakeEngine(s, "grammalecte", lambda t: present([issue(t, "a", ["à"], "grammalecte", rule="g3__conf_à_a_après_verbes__b3_a1_1")]))
    slow = FakeEngine(s, "languagetool", lambda t: [], delay=3)
    c = Checker(s, PersonalDictionary(), engines=[g, slow])
    t0 = time.monotonic()
    new_text, _, _, final = c.autocorrect(text, slow_timeout=0.3)
    assert time.monotonic() - t0 < 2
    assert new_text == "Je vais à la plage demain."
    assert not final.statuses["languagetool"].ok  # le bilan dit pourquoi LanguageTool manque

    t0 = time.monotonic()
    result = c.check(text, timeout=0.3)
    assert time.monotonic() - t0 < 2
    c.close()
    assert result.done and result.statuses["grammalecte"].ok
    assert not result.statuses["languagetool"].ok
    assert "0.3 s" in result.statuses["languagetool"].detail


def test_partial_updates_are_streamed(home):
    s = Settings()
    engines = [FakeEngine(s, "grammalecte", g_issues), FakeEngine(s, "languagetool", lt_issues, delay=0.2)]
    c = Checker(s, PersonalDictionary(), engines=engines)
    updates = []
    c.check(TEXT, on_update=lambda r: updates.append((r.pending, len(r.issues))))
    c.close()
    assert updates[0][0] == ("languagetool",)
    assert updates[-1][0] == ()


def test_engine_error_is_reported_not_raised(home):
    s = Settings()
    engines = [FakeEngine(s, "grammalecte", g_issues), FakeEngine(s, "languagetool", error="hors ligne")]
    c = Checker(s, PersonalDictionary(), engines=engines)
    result = c.check(TEXT)
    c.close()
    assert not result.statuses["languagetool"].ok
    assert result.statuses["languagetool"].detail == "hors ligne"
    assert result.statuses["grammalecte"].ok
    assert result.issues


def test_only_selects_engines(checker):
    result = checker.check(TEXT, only=["grammalecte"])
    assert set(result.statuses) == {"grammalecte"}


def test_personal_dictionary_and_abbreviations(home):
    s = Settings()
    text = "Salut stp, Kubernetes et Zorglub sont là."
    found = lambda t: [issue(t, w, [], "grammalecte", Category.SPELLING, "G_ORTHO") for w in ("stp", "Kubernetes", "Zorglub")]
    d = PersonalDictionary()
    d.add("kubernetes")
    c = Checker(s, d, engines=[FakeEngine(s, "grammalecte", found)])
    words = {text[i.start:i.end] for i in c.check(text).issues}
    assert words == {"Zorglub"}
    s.general.accept_abbreviations = False
    words = {text[i.start:i.end] for i in c.check(text).issues}
    assert words == {"stp", "Zorglub"}
    c.close()


def test_urls_and_emails_are_not_spellchecked(home):
    s = Settings()
    text = "Écris à jean@exemple.fr ou va sur https://exemple.org"
    found = lambda t: [issue(t, w, [], "grammalecte", Category.SPELLING, "G_ORTHO")
                       for w in ("jean@exemple.fr", "https://exemple.org")]
    c = Checker(s, PersonalDictionary(), engines=[FakeEngine(s, "grammalecte", found)])
    assert c.check(text).issues == []
    c.close()


def test_typographic_only_remarks_hidden_in_standard_mode(home):
    s = Settings()
    text = "C'est l'école !"
    found = lambda t: [
        issue(t, "C'", ["C’"], "grammalecte", Category.TYPOGRAPHY, "apostrophe_typographique"),
        issue(t, " !", ["\u00a0!"], "grammalecte", Category.TYPOGRAPHY, "nbsp"),
    ]
    c = Checker(s, PersonalDictionary(), engines=[FakeEngine(s, "grammalecte", found)])
    assert c.check(text).issues == []
    s.general.typography = "stricte"
    assert len(c.check(text).issues) == 2
    c.close()


def test_suggestions_follow_user_apostrophe_style(home):
    s = Settings()
    text = "Ces un jour qu'on aime."
    found = lambda t: [issue(t, "Ces", ["C’est"], "grammalecte")]
    c = Checker(s, PersonalDictionary(), engines=[FakeEngine(s, "grammalecte", found)])
    assert c.check(text).issues[0].replacements == ["C'est"]
    c.close()


def test_ignored_rules_and_ignore_once(checker):
    checker.settings.ignored_rules = ["G_SON"]
    result = checker.check(TEXT)
    assert "sont" not in by_text(result.issues, TEXT)
    target = by_text(result.issues, TEXT)["fesait"]
    checker.ignore_once(TEXT, target)
    assert "fesait" not in by_text(checker.check(TEXT).issues, TEXT)


def test_ignore_once_keeps_a_grammar_remark_to_its_sentence(home):
    """Ignorer « a » -> « à » une fois ne doit pas faire taire « sac a dos » ailleurs (avant : toute la session)."""
    s = Settings()
    rule = "g3__conf_à_a_après_verbes__b3_a1_1"
    g = lambda t: [Issue(m.start(), m.end(), "msg", ["à"], Category.GRAMMAR, rule, "grammalecte")
                   for m in re.finditer(r"(?<= )a(?= )", t)]
    c = Checker(s, PersonalDictionary(), engines=[FakeEngine(s, "grammalecte", g)])
    text = "Je vais a la plage. Le sac a dos est lourd."
    first = c.check(text).issues[0]
    c.ignore_once(text, first)
    assert [i.start for i in c.check(text).issues] == [text.index("sac a") + 4]
    assert len(c.check("Je vais a la plage.").issues) == 0  # même phrase : toujours ignorée
    assert len(c.check("Il range son sac a dos.").issues) == 1
    # Corriger une autre faute de la même phrase ne fait pas revenir la remarque ignorée.
    assert [i.start for i in c.check("Je vais a la plage. Le sac à dos est lourd.").issues] == []
    c.close()


def test_ignore_once_holds_when_the_engines_zones_differ(home):
    """La remarque ignorée est la fusion des deux moteurs (« on joué ») ; la zone de Grammalecte seul est
    « joué » : elle doit rester ignorée."""
    s = Settings()
    c = Checker(s, PersonalDictionary(), engines=on_joue_engines(s))
    [merged] = c.check(ON_JOUE).issues
    c.ignore_once(ON_JOUE, merged)
    assert c.check(ON_JOUE).issues == []
    c.close()


def test_grammalecte_skipped_for_english_in_auto_mode(home):
    s = Settings()
    s.general.language = "auto"
    g = FakeEngine(s, "grammalecte")
    lt = FakeEngine(s, "languagetool", languages=("fr", "en"))
    c = Checker(s, PersonalDictionary(), engines=[g, lt])
    c.check("Hello, this is a simple English sentence that you can read.")
    assert g.calls == 0 and lt.calls == 1
    c.check("Bonjour, c'est une phrase en français que tu peux lire.")
    assert g.calls == 1
    c.close()


def test_guess_language():
    assert guess_language("Je pense que c'est une bonne idée pour nous") == "fr"
    assert guess_language("I think this is a good idea for you and me") == "autre"


def test_split_confident_skips_overlaps():
    a = Issue(0, 5, "", ["x"], Category.GRAMMAR, "A", "grammalecte", confident=True)
    b = Issue(3, 8, "", ["y"], Category.GRAMMAR, "B", "grammalecte", confident=True)
    c = Issue(10, 12, "", ["z"], Category.GRAMMAR, "C", "grammalecte")
    applied, remaining = split_confident([a, b, c])
    assert applied == [a] and remaining == [c]


def test_merge_does_not_merge_same_engine():
    text = "abc def"
    a = Issue(0, 3, "", ["x"], Category.GRAMMAR, "A", "grammalecte")
    b = Issue(0, 3, "", ["x"], Category.GRAMMAR, "B", "grammalecte")
    assert len(merge_issues(text, [a, b])) == 2


def test_is_confident_rejects_style():
    text = "au jour d'aujourd'hui"
    i = Issue(0, len(text), "", ["aujourd'hui"], Category.STYLE, "PLEO", "grammalecte", agreed=True)
    assert not is_confident(text, i)


def test_checker_is_thread_safe(checker):
    errors = []

    def run():
        try:
            for _ in range(5):
                checker.check(TEXT)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def when(context, word, repl, rule=RULE, category=Category.GRAMMAR, source="grammalecte"):
    """Remarque sur `word` tant que le texte contient `context` : comme un vrai moteur,
    qui juge un mot d'après ses voisins (corriger l'un peut faire disparaître l'autre)."""
    import re

    def found(t):
        match = re.search(r"(?<!\w)" + re.escape(context) + r"(?!\w)", t)
        if match is None:
            return None
        start = match.start() + context.index(word)
        return Issue(start, start + len(word), "msg", list(repl), category, rule, source)
    return found


def solo_checker(s, *rules, name="grammalecte"):
    """Un seul moteur répond (l'autre est hors ligne)."""
    other = "languagetool" if name == "grammalecte" else "grammalecte"
    engines = [FakeEngine(s, name, lambda t: [r(t) for r in rules]), FakeEngine(s, other, error="hors ligne")]
    return Checker(s, PersonalDictionary(), engines=engines)


def sure(checker, text):
    return {w: i.confident for w, i in by_text(checker.check(text).issues, text).items()}


def test_solo_engine_accepts_isolated_single_grammar_fix(home):
    c = solo_checker(Settings(), when("vais a la", "a", ["à"]), when("sont bon", "sont", ["son"]),
                     when("sont bon", "bon", ["bons"]))
    got = sure(c, "Je vais a la plage demain. Les résultats sont bon et sont là.")
    assert got["a"]
    # Chaque correction fait disparaître l'autre : deux lectures possibles, rien d'automatique.
    assert not got["sont"] and not got["bon"]
    c.close()


def test_solo_recheck_applies_the_cause_not_the_consequence(home):
    """"Ils on mangé" : "ont" règle aussi "mangé" ; corriger "mangé" laisse "on"."""
    c = solo_checker(Settings(), when("Ils on", "on", ["ont"]), when("on mangé", "mangé", ["mange", "mangeait"]))
    text = "Ils on mangé au restaurant."
    got = sure(c, text)
    assert got["on"] and not got["mangé"]
    assert c.autocorrect(text)[0] == "Ils ont mangé au restaurant."
    c.close()


def test_solo_recheck_applies_independent_neighbours(home):
    c = solo_checker(Settings(), when("avons manger", "manger", ["mangé"]), when("des pomme", "pomme", ["pommes"]))
    text = "Nous avons manger des pomme hier."
    assert sure(c, text) == {"manger": True, "pomme": True}
    assert c.autocorrect(text)[0] == "Nous avons mangé des pommes hier."
    c.close()


def test_solo_recheck_ignores_an_alternative_that_breaks_something_else(home):
    """"Quel belle journée" : "beau" ferait disparaître "Quel" mais rendrait "journée" fautive."""
    c = solo_checker(Settings(), when("Quel belle", "Quel", ["Quelle"]), when("Quel belle", "belle", ["beau"]),
                     when("beau journée", "journée", []))
    assert sure(c, "Quel belle journée !") == {"Quel": True, "belle": False}
    c.close()


def test_solo_recheck_rejects_a_fix_the_engine_flags_again(home):
    c = solo_checker(Settings(), when("faut mangé", "mangé", ["mangez"]), when("faut mangez", "mangez", ["manger"]))
    assert sure(c, "Il faut mangé.") == {"mangé": False}
    c.close()


def test_solo_accepts_masculine_among_gender_variants(home):
    from correcteur.checker import solo_candidate

    c = solo_checker(Settings(), when("suis aller", "aller", ["allé", "allée"]))
    assert c.autocorrect("Je suis aller au cinéma.")[0] == "Je suis allé au cinéma."
    c.close()
    grammar = lambda *repl: Issue(0, 1, "", list(repl), Category.GRAMMAR, RULE, "grammalecte")
    assert solo_candidate(grammar("allés", "allées"))
    assert not solo_candidate(grammar("mange", "manges"))  # personne du verbe : pas un simple accord
    assert not solo_candidate(grammar("ait", "est"))


def test_solo_never_trusts_agreement_guessed_from_the_verb(home):
    """"Il a pris sont manteau" : l'accord avec "sont" donnerait "sont manteaux"."""
    c = solo_checker(Settings(), when("sont manteau", "manteau", ["manteaux"], rule="gv1__ppas_être_accord_plur__b1"))
    text = "Il a pris sont manteau."
    assert sure(c, text) == {"manteau": False}
    assert c.autocorrect(text)[0] == text
    c.close()


def test_solo_recheck_failure_leaves_the_fix_to_the_user(home):
    rules = [when("Ils on", "on", ["ont"]), when("on mangé", "mangé", ["mange"])]
    c = solo_checker(Settings(), *rules)
    engine = c.engine("grammalecte")
    engine.recheck = lambda text, language: (_ for _ in ()).throw(RuntimeError("moteur planté"))
    assert sure(c, "Ils on mangé au restaurant.") == {"on": False, "mangé": False}
    c.close()


def test_solo_without_local_engine_keeps_the_distance_rule(home):
    """LanguageTool seul : pas de revérification instantanée, deux remarques voisines restent à vérifier."""
    c = solo_checker(Settings(), when("Ils on", "on", ["ont"], source="languagetool"),
                     when("on mangé", "mangé", ["mange"], source="languagetool"), name="languagetool")
    assert sure(c, "Ils on mangé au restaurant.") == {"on": False, "mangé": False}
    c.close()


def test_solo_rule_not_used_when_both_engines_answer(checker):
    found = by_text(checker.check(TEXT).issues, TEXT)
    assert not found["sont"].confident


@pytest.mark.parametrize("original, replacement, way", [
    ("enfant", "enfants", 1), ("était", "étaient", 1), ("fatigué", "fatiguées", 1), ("parle", "parlent", 1),
    ("ils", "il", -1), ("ravis", "ravi", -1), ("aller", "allé", 0), ("a", "à", 0),
])
def test_number_direction(original, replacement, way):
    from correcteur.checker import number_direction

    assert number_direction(original, replacement) == way


def test_autocorrect_keeps_number_coherent_in_a_sentence(home):
    """"les enfant, ils était ravis" : après "enfants" (pluriel), les moteurs
    proposent "ils" -> "il" (singulier) : refusé, laissé à vérifier."""
    s = Settings()

    def g(t):
        found = [issue(t, "enfant", ["enfants"], "grammalecte"), issue(t, "ils", ["il"], "grammalecte"),
                 issue(t, "ravis", ["ravi"], "grammalecte")]
        return present(found)

    def lt(t):
        return present([issue(t, "enfant", ["enfants"], "languagetool"), issue(t, "ils", ["il"], "languagetool"),
                        issue(t, "ravis", ["ravi"], "languagetool")])

    c = Checker(s, PersonalDictionary(), engines=[FakeEngine(s, "grammalecte", g), FakeEngine(s, "languagetool", lt)])
    new_text, applied, remaining, _ = c.autocorrect("Va voir les enfant, ils était ravis.")
    assert new_text == "Va voir les enfants, ils était ravis."
    assert {new_text[i.start:i.end] for i in remaining} == {"ils", "ravis"}
    c.close()


def test_close_never_blocks_program_exit(tmp_path):
    """Un moteur qui ne répond pas (réseau lent) ne doit jamais empêcher le
    programme de se fermer."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    code = (
        "import threading, time\n"
        "from test_checker import FakeEngine\n"
        "from correcteur.checker import Checker\n"
        "from correcteur.config import Settings\n"
        "s = Settings()\n"
        "c = Checker(s, None, engines=[FakeEngine(s, 'languagetool', delay=60)])\n"
        "threading.Thread(target=c.check, args=('texte',)).start()\n"
        "time.sleep(0.3)\n"
        "c.close()\n"
    )
    env = dict(os.environ, CORRECTEUR_HOME=str(tmp_path),
               PYTHONPATH=os.pathsep.join([str(root / "src"), str(root / "tests")]))
    proc = subprocess.run([sys.executable, "-c", code], env=env, timeout=15, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
