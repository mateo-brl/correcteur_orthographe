"""Texte mis en forme du presse-papiers Windows (format CF_HTML, « HTML Format ») : y reporter les corrections
faites sur le texte brut.

Word, Outlook, les navigateurs et LibreOffice copient la sélection en texte brut et en HTML. Recoller le HTML
corrigé, avec le texte brut corrigé, garde le gras, l'italique, les liens et les espaces insécables. Seul le
texte entre les balises change, jamais les balises. Une correction qu'on ne retrouve pas exactement dans le HTML,
ou qui touche un retour à la ligne, fait renoncer au HTML (None) : le texte brut corrigé est alors collé seul.

En-tête CF_HTML : des lignes « Clé:valeur » (Version, StartHTML, EndHTML, StartFragment, EndFragment...), où
les valeurs sont des positions en octets dans les données, encodées en UTF-8. La sélection est entre
StartFragment et EndFragment."""

from __future__ import annotations

import difflib
import html
import re

from correcteur.textutils import diff_edits

_OFFSET_KEYS = ("StartHTML", "EndHTML", "StartFragment", "EndFragment", "StartSelection", "EndSelection")
_TOKEN_RE = re.compile(r"<!--.*?-->|<!\[.*?\]>|<![^>]*>|<\?[^>]*>|</?[A-Za-z][^>]*>|<|[^<]+", re.S)
_TAG_NAME_RE = re.compile(r"</?\s*([A-Za-z][A-Za-z0-9:-]*)")
_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);?")
_BLOCKS = {"p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table", "tbody", "thead", "tfoot", "caption",
           "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr", "section", "article", "header", "footer",
           "dl", "dt", "dd", "address", "figure", "figcaption", "nav", "aside", "main", "form", "center"}
_HIDDEN = {"style", "script", "title", "head", "template"}
_SPACES = " \t\n\r\f"
_MAX_ALIGN = 8000  # au-delà, l'alignement caractère par caractère serait trop lent (fil de l'interface) : texte brut seul


def correct_cf_html(data: bytes, before: str, after: str) -> bytes | None:
    """Données CF_HTML où le texte `before` (texte brut copié en même temps) devient `after` ; None si les
    corrections ne se reportent pas avec certitude."""
    data = data.split(b"\0", 1)[0]  # texte terminé par un octet nul ; la mémoire allouée peut être plus grande
    parsed = _parse(data)
    if parsed is None:
        return None
    fields, order, header_end, start, end = parsed
    try:
        fragment = data[start:end].decode("utf-8")
    except UnicodeDecodeError:
        return None
    new_fragment = correct_fragment(fragment, before, after)
    if new_fragment is None:
        return None
    return _rebuild(data, fields, order, header_end, start, end, new_fragment.encode("utf-8")) + b"\0"


def _parse(data: bytes):
    fields: dict[str, str] = {}
    order: list[str] = []
    pos = 0
    while pos < len(data) and data[pos:pos + 1] != b"<" and len(order) < 20:
        stop = data.find(b"\n", pos)
        if stop < 0:
            return None
        line = data[pos:stop].rstrip(b"\r").decode("ascii", "replace")
        if ":" not in line:
            break
        key, value = line.split(":", 1)
        fields[key], pos = value.strip(), stop + 1
        order.append(key)
    try:
        start, end = int(fields["StartFragment"]), int(fields["EndFragment"])
    except (KeyError, ValueError):
        return None
    if not pos <= start <= end <= len(data):
        return None
    return fields, order, pos, start, end


def _rebuild(data: bytes, fields: dict[str, str], order: list[str], header_end: int, start: int, end: int,
             fragment: bytes) -> bytes:
    """Nouvel en-tête (positions sur 10 chiffres, toujours de la même longueur) et nouvelles positions."""
    delta = len(fragment) - (end - start)

    def line(key: str, value: str) -> str:
        return f"{key}:{value}\r\n"

    def number(value: int) -> str:
        return "-1" if value < 0 else f"{value:010d}"

    offsets: dict[str, int] = {}
    for key in order:
        if key in _OFFSET_KEYS:
            try:
                offsets[key] = int(fields[key])
            except ValueError:
                offsets[key] = -1
    header_len = len("".join(line(k, number(offsets[k]) if k in offsets else fields[k]) for k in order).encode())

    def moved(old: int, key: str) -> int:
        if old < 0:
            return old
        if key in ("StartSelection", "EndSelection") and start < old < end:
            old = start if key == "StartSelection" else end  # dans la sélection : on la recouvre entière
        shift = delta if old >= end and not (old == start == end and key.startswith("Start")) else 0
        return header_len + (old - header_end) + shift

    header = "".join(line(k, number(moved(offsets[k], k)) if k in offsets else fields[k]) for k in order)
    return header.encode() + data[header_end:start] + fragment + data[end:]


def _tokens(fragment: str) -> list[tuple[bool, str]]:
    """(texte ?, morceau) : balises, commentaires et texte, dans l'ordre."""
    return [(not m.group().startswith("<") or m.group() == "<", m.group()) for m in _TOKEN_RE.finditer(fragment)]


class _Visible:
    """Texte affiché d'un fragment HTML, avec pour chaque caractère sa place dans le HTML : (n° du morceau,
    début, fin) ; morceau -1 pour les retours à la ligne des blocs (paragraphes, <br>)."""

    def __init__(self, tokens: list[tuple[bool, str]]) -> None:
        self.chars: list[str] = []
        self.where: list[tuple[int, int, int]] = []
        hidden = ""
        space = True  # début de bloc : les blancs de tête ne s'affichent pas
        for n, (is_text, piece) in enumerate(tokens):
            if not is_text:
                name = _TAG_NAME_RE.match(piece)
                tag = name.group(1).lower() if name else ""
                if hidden:
                    if piece.startswith("</") and tag == hidden:
                        hidden = ""
                    continue
                if tag in _HIDDEN and not piece.startswith("</") and not piece.endswith("/>"):
                    hidden = tag
                elif tag in _BLOCKS and (tag == "br" or not self.chars or self.chars[-1] != "\n"):
                    if self.chars or tag == "br":
                        self._add("\n", -1, 0, 0)
                    space = True
                continue
            if hidden:
                continue
            for char, a, b in _decoded(piece):
                if char in _SPACES:
                    if space:
                        continue
                    char, space = " ", True
                else:
                    space = False
                self._add(char, n, a, b)

    def _add(self, char: str, token: int, a: int, b: int) -> None:
        self.chars.append(char)
        self.where.append((token, a, b))


def _decoded(piece: str):
    """Caractères affichés d'un morceau de texte HTML (entités décodées), avec leur place dans le morceau."""
    pos = 0
    for m in _ENTITY_RE.finditer(piece):
        for i in range(pos, m.start()):
            yield piece[i], i, i + 1
        for char in html.unescape(m.group()):
            yield char, m.start(), m.end()
        pos = m.end()
    for i in range(pos, len(piece)):
        yield piece[i], i, i + 1


def _key(char: str) -> str:
    return " " if char in _SPACES or char == "\xa0" else char


def _normalized(chars) -> tuple[str, list[int]]:
    """Texte réduit pour l'alignement (tout blanc, insécable comprise, devient une espace ; une seule de suite),
    avec la position d'origine de chacun de ses caractères."""
    out: list[str] = []
    index: list[int] = []
    for i, char in enumerate(chars):
        key = _key(char)
        if key == " " and (not out or out[-1] == " "):
            continue
        out.append(key)
        index.append(i)
    return "".join(out), index


def _alignment(plain: str, visible: list[str]) -> dict[int, int] | None:
    """Position dans le texte affiché de chaque caractère du texte brut qu'on y retrouve."""
    norm_p, index_p = _normalized(plain)
    norm_v, index_v = _normalized(visible)
    if norm_p.strip() == norm_v.strip():
        lead_p, lead_v = len(norm_p) - len(norm_p.lstrip()), len(norm_v) - len(norm_v.lstrip())
        size = len(norm_p.strip())
        return {index_p[lead_p + k]: index_v[lead_v + k] for k in range(size)}
    if len(norm_p) + len(norm_v) > _MAX_ALIGN:
        return None
    mapping: dict[int, int] = {}
    matcher = difflib.SequenceMatcher(None, norm_p, norm_v, autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            mapping[index_p[a + k]] = index_v[b + k]
    return mapping


def _keep_nbsp(old_plain: str, old_visible: str, new: str) -> str:
    """Le nouveau texte reprend les espaces insécables de l'ancien là où il ne change rien."""
    if "\xa0" not in old_visible or len(old_plain) != len(old_visible):
        return new
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_plain, new, autojunk=False).get_opcodes():
        out.append(old_visible[i1:i2] if tag == "equal" else new[j1:j2])
    return "".join(out)


def _escape(text: str) -> str:
    return html.escape(text, quote=False).replace("\xa0", "&nbsp;")


def correct_fragment(fragment: str, before: str, after: str) -> str | None:
    """Fragment HTML où le texte affiché `before` devient `after` ; None si c'est incertain."""
    if before == after:
        return fragment
    tokens = _tokens(fragment)
    if any(is_text and piece == "<" for is_text, piece in tokens):
        return None  # HTML mal formé
    visible = _Visible(tokens)
    mapping = _alignment(before, visible.chars)
    if mapping is None:
        return None
    exact = _normalized(before)[0].strip() == _normalized(visible.chars)[0].strip()
    changes: dict[int, list[tuple[int, int, str]]] = {}  # morceau -> remplacements (début, fin, HTML)
    for s, e, rep in diff_edits(before, after):
        old = before[s:e]
        if "\n" in old or "\n" in rep:
            return None
        if s == e:
            if s in mapping:
                token, a, _b = visible.where[mapping[s]]
            elif s - 1 in mapping:
                token, _a, a = visible.where[mapping[s - 1]]
            else:
                return None
            if token < 0:
                return None
            changes.setdefault(token, []).append((a, a, _escape(rep)))
            continue
        if any(i not in mapping for i in range(s, e) if before[i] not in _SPACES):
            return None
        found = [i for i in range(s, e) if i in mapping]
        if not found:
            continue  # blancs seuls, que le HTML regroupe de toute façon
        first, last = found[0], found[-1]
        vs, ve = mapping[first], mapping[last] + 1
        if vs >= ve or any(visible.where[v][0] < 0 for v in range(vs, ve)):
            return None
        shown = "".join(visible.chars[vs:ve])
        core = before[first:last + 1]
        if sum(_key(c) != " " for c in shown) != sum(_key(c) != " " for c in core):
            return None  # le HTML montre autre chose à cet endroit (puce, texte caché...)
        new = rep
        head, tail = before[s:first], before[last + 1:e]
        if head and new.startswith(head):
            new = new[len(head):]
        if tail and new.endswith(tail):
            new = new[:len(new) - len(tail)]
        new = _keep_nbsp(core, shown, new)
        by_token: dict[int, list[int]] = {}
        for v in range(vs, ve):
            by_token.setdefault(visible.where[v][0], []).append(v)
        for rank, (token, places) in enumerate(sorted(by_token.items())):
            a, b = visible.where[places[0]][1], visible.where[places[-1]][2]
            changes.setdefault(token, []).append((a, b, _escape(new) if rank == 0 else ""))
    pieces = [piece for _, piece in tokens]
    for token, edits in changes.items():
        piece = pieces[token]
        last_start = len(piece) + 1
        for a, b, text in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
            if b > last_start:
                return None  # remplacements qui se chevauchent
            piece = piece[:a] + text + piece[b:]
            last_start = a
        pieces[token] = piece
    result = "".join(pieces)
    if exact and _normalized(_Visible(_tokens(result)).chars)[0].strip() != _normalized(after)[0].strip():
        return None  # vérification : le HTML corrigé doit afficher exactement le texte corrigé
    return result
