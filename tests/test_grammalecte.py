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
