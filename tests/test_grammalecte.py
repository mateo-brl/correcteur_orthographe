from __future__ import annotations

import time

from conftest import needs_grammalecte

from correcteur.config import Settings
from correcteur.models import Category

pytestmark = needs_grammalecte


def engine(settings=None):
    from correcteur.engines.grammalecte_engine import GrammalecteEngine

    return GrammalecteEngine(settings or Settings())


def found(issues, text):
    return {text[i.start:i.end]: i for i in issues}


def test_detects_classic_mistakes():
    text = "Je suis allé a la plage, il fesait beau et les enfant jouait. Ces un beau jour."
    got = found(engine().check(text, "fr"), text)
    assert got["a"].replacements[0] == "à"
    assert got["fesait"].category is Category.SPELLING
    assert got["enfant"].replacements[0] == "enfants"
    assert "Ces" in got


def test_multiline_offsets():
    text = "Première ligne correcte.\r\nIl faut que tu viens.\nLes enfant jouent."
    got = found(engine().check(text, "fr"), text)
    assert "viens" in got and "enfant" in got


def test_standard_mode_hides_typography_pedantry():
    text = "C'est l'heure ! Il a dit \"bonjour\"... Voilà."
    std = found(engine().check(text, "fr"), text)
    assert not any(i.rule_id.startswith(("apostrophe_typographique", "nbsp_", "typo_guillemets_typographiques",
                                         "typo_points_suspension")) for i in std.values())
    strict_settings = Settings()
    strict_settings.general.typography = "stricte"
    strict = engine(strict_settings).check(text, "fr")
    assert any(i.rule_id.startswith("apostrophe_typographique") for i in strict)


def test_warmup_also_loads_the_corrector_rules():
    """Le préchargement passe par une vraie vérification : les règles du correcteur et leur lexique sont prêts
    avant la première correction, pas seulement Grammalecte."""
    e = engine()
    assert e._lexique is None
    e.warmup()
    assert e._checker is not None and e._lexique is not None


def test_paragraph_cache_makes_rechecks_fast():
    e = engine()
    e.warmup()
    text = "\n".join(f"Paragraphe {n} : les enfant joue dans le jardin avec leur ami." for n in range(40))
    t0 = time.perf_counter()
    e.check(text, "fr")
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    e.check(text.replace("Paragraphe 3 ", "Paragraphe trois "), "fr")
    second = time.perf_counter() - t0
    assert second < first


def test_supports_only_french():
    e = engine()
    assert e.supports("fr") and e.supports("fr-FR") and e.supports("auto")
    assert not e.supports("en-US")


def test_imperfect_subjunctive_left_out_in_standard_mode():
    text = "Il faut que tu viens demain."
    assert found(engine().check(text, "fr"), text)["viens"].replacements == ["viennes"]
    strict_settings = Settings()
    strict_settings.general.typography = "stricte"
    assert found(engine(strict_settings).check(text, "fr"), text)["viens"].replacements == ["viennes", "vinsses"]


def test_spelling_suggestions_skip_invented_capitals():
    """Grammalecte départage par ordre alphabétique, majuscules d'abord : « vien » proposait « VIe »."""
    text = "Il faut que tu vien demain."
    sugg = found(engine().check(text, "fr"), text)["vien"].replacements
    assert sugg[:2] == ["viens", "vient"]
    assert sugg.index("vie") < sugg.index("VIe") and sugg.index("vienne") < sugg.index("Vienne")


def test_spelling_ranking_keeps_grammalecte_distances():
    from correcteur.engines.grammalecte_engine import rank_spelling

    # Inversion de lettres (« chein ») ou lettre en trop (« bonjoure ») : l'ordre de Grammalecte reste.
    assert rank_spelling("bonjoure", ("bonjour", "bonjours", "conjure")) == ("bonjour", "bonjours", "conjure")
    assert rank_spelling("tres", ("très", "Dres", "ares")) == ("très", "ares", "Dres")
    assert rank_spelling("Vien", ("VIe", "Vie", "Viens")) == ("Viens", "Vie", "VIe")


def test_case_corrections_are_kept(home):
    """« Janvier » -> « janvier » : la suggestion ne doit pas reprendre la majuscule du mot tapé (elle serait
    alors identique à l'original, et la remarque disparaissait)."""
    from correcteur.checker import Checker
    from correcteur.config import PersonalDictionary

    settings = Settings()
    settings.languagetool.enabled = False
    checker = Checker(settings, PersonalDictionary())
    text = "Je suis né en Janvier."
    assert [(text[i.start:i.end], i.replacements[0]) for i in checker.check(text).issues] == [("Janvier", "janvier")]
    checker.close()


def test_emoji_do_not_hide_faults():
    """Grammalecte ne reconnaît pas les emoji et perdait les fautes qui suivent (« 😀 Les enfant joue. »)."""
    for text in ("😀 Les enfant joue.", "Bravo 👍🏽 les enfant joue.", "Merci ❤️ ! Les enfant joue."):
        got = found(engine().check(text, "fr"), text)
        assert "enfant" in got, text
        assert all(text[i.start:i.end].strip() for i in got.values())  # rien sur les espaces de remplacement


def test_standard_mode_skips_typographic_ordinals():
    """Les identifiants de Grammalecte 2.3 ont un préfixe de niveau (« g2__typo_ordinaux… ») : « 1er » ne doit
    pas devenir « 1ᵉʳ » en mode standard."""
    text = "Le 1er mai, il pleuvait."
    assert "1er" not in found(engine().check(text, "fr"), text)
