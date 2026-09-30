from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from correcteur.config import Settings
from correcteur.engines.base import EngineError
from correcteur.engines.languagetool_engine import LanguageToolEngine
from correcteur.models import Category


def u16(s):
    return len(s.encode("utf-16-le")) // 2


def lt_match(text, word, replacements, rule_id="RULE", issue_type="grammar", category="CAT_GRAMMAIRE", shift=0):
    """Construit une réponse comme l'API LanguageTool (offsets UTF-16, contexte)."""
    start = text.index(word)
    offset = u16(text[:start])
    length = u16(word)
    ctx_start = max(0, start - 10)
    context = text[ctx_start:start + len(word) + 10]
    return {
        "message": f"Message {rule_id}",
        "offset": offset + shift,
        "length": length,
        "context": {"text": context, "offset": u16(text[ctx_start:start]), "length": length},
        "replacements": [{"value": r} for r in replacements],
        "rule": {"id": rule_id, "issueType": issue_type, "category": {"id": category},
                 "urls": [{"value": "https://exemple.org/regle"}]},
    }


class FakeServer:
    def __init__(self, responder=None, status=200):
        self.requests = []
        self.responder = responder or (lambda text: [])
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append(form)
        if self.status != 200:
            return httpx.Response(self.status, text="erreur")
        return httpx.Response(200, json={"matches": self.responder(form["text"])})


def make_engine(settings: Settings, server: FakeServer) -> LanguageToolEngine:
    engine = LanguageToolEngine(settings)
    engine._client = httpx.Client(transport=httpx.MockTransport(server))
    return engine


def test_offsets_are_converted_from_utf16(settings):
    text = "Super 😀 journée , les enfant jouent."
    server = FakeServer(lambda t: [lt_match(t, "enfant", ["enfants"], "D_N"),
                                   lt_match(t, " ,", [","], "COMMA", "whitespace", "TYPOGRAPHY")])
    engine = make_engine(settings, server)
    issues = engine.check(text, "fr")
    got = {text[i.start:i.end]: i for i in issues}
    assert set(got) == {"enfant", " ,"}
    assert got["enfant"].replacements == ["enfants"]
    assert got["enfant"].category is Category.GRAMMAR
    assert got[" ,"].category is Category.TYPOGRAPHY
    assert got["enfant"].url == "https://exemple.org/regle"


def test_inconsistent_positions_are_dropped(settings):
    """Le serveur public renvoie parfois des positions relatives au paragraphe
    (remarques "grammalecte_*") : elles ne doivent jamais souligner le mauvais mot."""
    text = "Bonjour Julie,\nJe suis aller a la réunion."
    server = FakeServer(lambda t: [
        lt_match(t, "aller", ["allé"], "grammalecte_gv1__ppas", shift=-15),
        lt_match(t, "aller", ["allé"], "AUTRE_REGLE", shift=-15),
        lt_match(t, " a ", [" à "], "A_ACCENT"),
    ])
    engine = make_engine(settings, server)
    issues = engine.check(text, "fr")
    assert [text[i.start:i.end] for i in issues] == [" a "]


@pytest.mark.parametrize("issue_type, category, expected", [
    ("misspelling", "TYPOS", Category.SPELLING),
    ("typographical", "CAT_TYPOGRAPHIE", Category.TYPOGRAPHY),
    ("style", "STYLE", Category.STYLE),
    ("uncategorized", "CAT_HOMONYMES_PARONYMES", Category.GRAMMAR),
])
def test_categories(settings, issue_type, category, expected):
    text = "mot"
    engine = make_engine(settings, FakeServer(lambda t: [lt_match(t, "mot", ["mots"], "R", issue_type, category)]))
    assert engine.check(text, "fr")[0].category is expected


def test_standard_mode_disables_typographic_rules(settings):
    server = FakeServer()
    engine = make_engine(settings, server)
    engine.check("Bonjour.", "fr")
    form = server.requests[-1]
    assert form["language"] == "fr"
    assert form["level"] == "picky"
    assert "CAT_TYPOGRAPHIE" in form["disabledCategories"]
    settings.general.typography = "stricte"
    engine.check("Bonsoir.", "fr")
    assert "disabledCategories" not in server.requests[-1]


def test_results_are_cached(settings):
    server = FakeServer()
    engine = make_engine(settings, server)
    engine.check("Un texte.", "fr")
    engine.check("Un texte.", "fr")
    assert len(server.requests) == 1


def test_long_text_is_split_in_chunks(settings):
    paragraph = "Les enfant jouent dans le jardin pendant que les parents discutent. " * 20 + "\n"
    text = paragraph * 20  # ~27 000 caractères > limite de l'API publique
    server = FakeServer(lambda t: [lt_match(t, "enfant", ["enfants"])] if "enfant" in t else [])
    engine = make_engine(settings, server)
    issues = engine.check(text, "fr")
    assert len(server.requests) >= 2
    assert all(len(r["text"]) <= 15_000 for r in server.requests)
    assert all(text[i.start:i.end] == "enfant" for i in issues)


def test_premium_credentials_sent(settings):
    settings.languagetool.mode = "premium"
    settings.languagetool.username = "moi@exemple.fr"
    settings.languagetool.api_key = "cle"
    server = FakeServer()
    engine = make_engine(settings, server)
    assert engine.base_url().startswith("https://api.languagetoolplus.com")
    engine.check("Test.", "fr")
    assert server.requests[-1]["username"] == "moi@exemple.fr"
    assert server.requests[-1]["apiKey"] == "cle"


def test_premium_without_credentials_unavailable(settings):
    settings.languagetool.mode = "premium"
    ok, why = LanguageToolEngine(settings).available()
    assert not ok and "Premium" in why


@pytest.mark.parametrize("status, fragment", [(429, "Limite"), (403, "refuse"), (500, "Erreur LanguageTool 500")])
def test_http_errors_are_readable(settings, status, fragment):
    engine = make_engine(settings, FakeServer(status=status))
    with pytest.raises(EngineError, match=fragment):
        engine.check("Texte.", "fr")


def test_network_error(settings):
    def boom(request):
        raise httpx.ConnectError("pas de réseau")

    engine = LanguageToolEngine(settings)
    engine._client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(EngineError, match="injoignable"):
        engine.check("Texte.", "fr")


def test_auto_language(settings):
    server = FakeServer()
    engine = make_engine(settings, server)
    engine.check("Hello world.", "auto")
    assert server.requests[-1]["language"] == "auto"
    assert "preferredVariants" in server.requests[-1]


@pytest.mark.reseau
def test_public_api_really_works(settings):
    engine = LanguageToolEngine(settings)
    text = "Bonjour Julie,\nIl faut que tu viens demain avec les enfant.\nJe suis aller a la réunion."
    issues = engine.check(text, "fr")
    flagged = {text[i.start:i.end] for i in issues}
    assert flagged & {"viens", "les enfant", "enfant"}
    assert "Julie" not in flagged and "Bonjour" not in flagged
    json.dumps([i.to_dict(text) for i in issues], ensure_ascii=False)


@pytest.mark.parametrize("typed, root", [
    ("http://localhost:8081", "http://localhost:8081"),
    ("localhost:8081/", "http://localhost:8081"),
    ("https://lt.example.org/v2/check", "https://lt.example.org"),
    (" http://192.168.1.20:8010/v2 ", "http://192.168.1.20:8010"),
])
def test_personal_server_address_is_forgiving(settings, typed, root):
    settings.languagetool.mode = "perso"
    settings.languagetool.url = typed
    assert LanguageToolEngine(settings).base_url() == root
