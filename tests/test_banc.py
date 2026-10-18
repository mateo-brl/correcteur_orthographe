"""Banc d'essai (banc_fautes.py) en correction express hors ligne : chaque faute courante est corrigée
d'office, chaque piège reste intact."""

from __future__ import annotations

import pytest

from banc_fautes import FAUTES, PIEGES
from conftest import needs_grammalecte

pytestmark = needs_grammalecte


@pytest.fixture(scope="module")
def offline():
    from correcteur.checker import Checker
    from correcteur.config import PersonalDictionary, Settings

    settings = Settings()
    settings.languagetool.enabled = False
    checker = Checker(settings, PersonalDictionary())
    yield checker
    checker.close()


def _same(a: str, b: str) -> bool:
    return a.replace("’", "'") == b.replace("’", "'")


@pytest.mark.parametrize("text, expected", FAUTES)
def test_bench_mistake_is_fixed(offline, text, expected):
    assert _same(offline.autocorrect(text)[0], expected)


@pytest.mark.parametrize("text", PIEGES)
def test_bench_trap_is_left_alone(offline, text):
    assert _same(offline.autocorrect(text)[0], text)
