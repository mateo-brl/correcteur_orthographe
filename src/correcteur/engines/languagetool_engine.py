"""Moteur LanguageTool (API HTTP v2) : service public, serveur local ou perso.

LanguageTool est à base de règles (pas d'IA). Les offsets renvoyés sont en
unités UTF-16 (Java) : ils sont convertis en indices Python pour que les
emojis ne décalent pas les soulignements."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque

from correcteur.config import Settings
from correcteur.engines.base import Engine, EngineError
from correcteur.models import Category, Issue
from correcteur.textutils import chunk_text, utf16_to_index

log = logging.getLogger(__name__)

PUBLIC_URL = "https://api.languagetool.org"
PREMIUM_URL = "https://api.languagetoolplus.com"

# Limites de l'API publique : 20 requêtes/min et 20 Ko par requête.
_PUBLIC_MAX_CHARS = 15_000
_OTHER_MAX_CHARS = 60_000
_PUBLIC_REQ_PER_MIN = 18

# Catégories purement typographiques coupées en mode "standard".
_STANDARD_DISABLED_CATEGORIES = "CAT_TYPOGRAPHIE"
_STANDARD_DISABLED_RULES = "FRENCH_WHITESPACE,APOS_TYP,TIRET,POINTS_SUSPENSION,ESPACE_UNITES,HEURES,GUILLEMETS"

_SPELLING_CATEGORIES = {"TYPOS", "CASING", "COMPOUNDING"}
_TYPO_CATEGORIES = {"TYPOGRAPHY", "PUNCTUATION", "CAT_TYPOGRAPHIE"}
_STYLE_CATEGORIES = {"STYLE", "REDUNDANCY", "REPETITIONS", "REPETITIONS_STYLE", "PLAIN_ENGLISH", "CAT_REGISTRE",
                     "SEMANTICS", "MISC"}


def _matches_context(match: dict, actual: str) -> bool:
    """Vérifie que le passage signalé correspond bien au contexte renvoyé par le serveur."""
    context = match.get("context") or {}
    ctx_text = context.get("text")
    if not isinstance(ctx_text, str) or "offset" not in context or "length" not in context:
        return True
    to_index = utf16_to_index(ctx_text)
    start = to_index(int(context["offset"]))
    end = to_index(int(context["offset"]) + int(context["length"]))
    expected = ctx_text[start:end]
    return not expected or expected == actual


def _category(match: dict) -> Category:
    rule = match.get("rule") or {}
    issue_type = rule.get("issueType", "")
    cat_id = (rule.get("category") or {}).get("id", "")
    if issue_type == "misspelling" or cat_id in _SPELLING_CATEGORIES:
        return Category.SPELLING
    if issue_type in ("typographical", "whitespace") or cat_id in _TYPO_CATEGORIES:
        return Category.TYPOGRAPHY
    if issue_type in ("style", "register", "locale-violation") or cat_id in _STYLE_CATEGORIES:
        return Category.STYLE
    return Category.GRAMMAR


class LanguageToolEngine(Engine):
    name = "languagetool"
    label = "LanguageTool"
    live = True

    def __init__(self, settings: Settings, server=None) -> None:
        super().__init__(settings)
        self._client = None
        self._client_lock = threading.Lock()
        self._cache: OrderedDict[tuple, list[tuple]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._requests: deque[float] = deque()
        self._rate_lock = threading.Lock()
        self.server = server  # LocalLanguageToolServer, pour le mode "local"

    def is_enabled(self) -> bool:
        return self.settings.languagetool.enabled

    @property
    def mode(self) -> str:
        return self.settings.languagetool.mode

    def base_url(self) -> str:
        lt = self.settings.languagetool
        if lt.mode == "public":
            return PUBLIC_URL
        if lt.mode == "premium":
            return PREMIUM_URL
        if lt.mode == "local":
            return f"http://127.0.0.1:{lt.local_port}"
        return lt.url.rstrip("/")

    def available(self) -> tuple[bool, str]:
        lt = self.settings.languagetool
        if lt.mode == "premium" and not (lt.username and lt.api_key):
            return False, "Identifiants LanguageTool Premium manquants."
        if lt.mode == "local" and self.server is not None:
            ok, why = self.server.can_run()
            if not ok:
                return False, why
        return True, ""

    def _http(self):
        with self._client_lock:
            if self._client is None:
                import httpx

                # Connexion gardée ouverte : évite une poignée de main TLS à chaque vérification.
                self._client = httpx.Client(
                    timeout=httpx.Timeout(20.0, connect=5.0),
                    headers={"User-Agent": "Correcteur (https://github.com/mateo-brl/correcteur_orthographe)"},
                    http2=False,
                )
            return self._client

    def warmup(self) -> None:
        if self.mode == "local" and self.server is not None:
            self.server.ensure_started()

    def close(self) -> None:
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _throttle(self) -> None:
        if self.mode != "public":
            return
        with self._rate_lock:
            now = time.monotonic()
            while self._requests and now - self._requests[0] > 60:
                self._requests.popleft()
            if len(self._requests) >= _PUBLIC_REQ_PER_MIN:
                wait = 60 - (now - self._requests[0]) + 0.1
                if wait > 8:
                    raise EngineError("Limite de l'API publique LanguageTool atteinte, réessayez dans une minute.")
                time.sleep(max(wait, 0))
            self._requests.append(time.monotonic())

    def _params(self, language: str) -> dict:
        lt = self.settings.languagetool
        params = {
            "language": "auto" if language == "auto" else language,
            "motherTongue": lt.mother_tongue,
        }
        if language == "auto":
            params["preferredVariants"] = "fr-FR,en-US,de-DE,pt-PT,ca-ES"
        if lt.picky:
            params["level"] = "picky"
        if not self.settings.strict_typography:
            params["disabledCategories"] = _STANDARD_DISABLED_CATEGORIES
            params["disabledRules"] = _STANDARD_DISABLED_RULES
        if lt.mode == "premium":
            params["username"] = lt.username
            params["apiKey"] = lt.api_key
        return params

    def _request(self, text: str, language: str) -> list[tuple]:
        import httpx

        if self.mode == "local" and self.server is not None:
            self.server.ensure_started()
        self._throttle()
        data = {"text": text, **self._params(language)}
        try:
            resp = self._http().post(self.base_url() + "/v2/check", data=data)
        except httpx.TimeoutException as exc:
            raise EngineError("LanguageTool ne répond pas (délai dépassé).") from exc
        except httpx.HTTPError as exc:
            if self.mode == "local":
                raise EngineError("Le serveur LanguageTool local ne répond pas.") from exc
            raise EngineError("LanguageTool injoignable (pas de connexion Internet ?).") from exc
        if resp.status_code == 429:
            raise EngineError("Limite de l'API publique LanguageTool atteinte, réessayez dans une minute.")
        if resp.status_code in (401, 403):
            raise EngineError("LanguageTool refuse l'accès (identifiants Premium incorrects ?).")
        if resp.status_code >= 400:
            detail = resp.text.strip().splitlines()[0][:200] if resp.text else ""
            raise EngineError(f"Erreur LanguageTool {resp.status_code} : {detail}")
        payload = resp.json()
        to_index = utf16_to_index(text)
        found = []
        for match in payload.get("matches", []):
            rule = match.get("rule") or {}
            # Le serveur public intègre Grammalecte ; ces remarques font doublon avec
            # notre Grammalecte local et leurs positions sont fausses sur plusieurs paragraphes.
            if str(rule.get("id", "")).startswith("grammalecte_"):
                continue
            offset = int(match.get("offset", 0))
            length = int(match.get("length", 0))
            start, end = to_index(offset), to_index(offset + length)
            if not _matches_context(match, text[start:end]):
                log.debug("Remarque LanguageTool ignorée (position incohérente) : %s", rule.get("id"))
                continue
            replacements = tuple(r["value"] for r in match.get("replacements", [])[:8] if "value" in r)
            urls = rule.get("urls") or []
            found.append((
                start, end, match.get("message", ""), replacements, _category(match),
                rule.get("id", "LT"), urls[0].get("value", "") if urls else "",
            ))
        return found

    def check(self, text: str, language: str) -> list[Issue]:
        if not text.strip():
            return []
        max_chars = _PUBLIC_MAX_CHARS if self.mode == "public" else _OTHER_MAX_CHARS
        key_base = (self.base_url(), language, self.settings.strict_typography, self.settings.languagetool.picky)
        issues: list[Issue] = []
        for offset, chunk in chunk_text(text, max_chars):
            if not chunk.strip():
                continue
            key = key_base + (chunk,)
            with self._cache_lock:
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
            if cached is None:
                cached = self._request(chunk, language)
                with self._cache_lock:
                    self._cache[key] = cached
                    while len(self._cache) > 256:
                        self._cache.popitem(last=False)
            for start, end, message, replacements, category, rule, url in cached:
                issues.append(Issue(
                    start=offset + start, end=offset + end, message=message, replacements=list(replacements),
                    category=category, rule_id=rule, source=self.name, url=url,
                ))
        return issues
