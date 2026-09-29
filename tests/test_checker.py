from __future__ import annotations

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
        return self.found(text)


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


def issue(text, word, repl, source, category=Category.GRAMMAR, rule="R", occurrence=0, message="msg"):
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


def test_solo_engine_accepts_isolated_single_grammar_fix(home):
    s = Settings()
    text = "Je vais a la plage demain. Les résultats sont bon et sont là."
    found = lambda t: [
        issue(t, "a", ["à"], "grammalecte", rule="CONF_A"),
        issue(t, "sont", ["son"], "grammalecte", rule="G_SON"),
        issue(t, "bon", ["bons"], "grammalecte", rule="G_ACCORD"),
    ]
    # LanguageTool en panne : Grammalecte est seul.
    engines = [FakeEngine(s, "grammalecte", found), FakeEngine(s, "languagetool", error="hors ligne")]
    c = Checker(s, PersonalDictionary(), engines=engines)
    result = c.check(text)
    got = by_text(result.issues, text)
    assert got["a"].confident
    # Deux corrections voisines : ambigu, rien d'automatique.
    assert not got["sont"].confident and not got["bon"].confident
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
