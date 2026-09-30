"""Fenêtre de correction : texte souligné, liste des fautes, remplacement dans
l'application d'origine."""

from __future__ import annotations

import html
import logging
import threading
from typing import TYPE_CHECKING, Iterable

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCursor, QGuiApplication, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from correcteur.checker import Checker, split_confident
from correcteur.models import ENGINE_LABELS, Category, CheckResult, EngineStatus, Issue
from correcteur.platform.keys import display as hotkey_display
from correcteur.textutils import diff_edits
from correcteur.ui.editor import CheckEditor, PositionMap
from correcteur.ui.theme import Theme

if TYPE_CHECKING:
    from correcteur.platform.bridge import Capture

log = logging.getLogger(__name__)

_FAST_ENGINES = ("grammalecte",)   # moteurs locaux instantanés : relancés pendant la frappe
_FAST_DELAY_MS = 350
_FULL_DELAY_MS = 1300
_MAX_CARDS = 150


def shift_issues(issues: list[Issue], old: str, new: str) -> list[Issue]:
    """Recale les fautes après une modification du texte (sans revérifier)."""
    if old == new:
        return issues
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]:
        suffix += 1
    old_end = len(old) - suffix
    delta = len(new) - len(old)
    kept = []
    for issue in issues:
        if issue.end <= prefix:
            kept.append(issue)
        elif issue.start >= old_end:
            kept.append(issue.shifted(delta))
    return kept


def can_ignore_rule(issue: Issue) -> bool:
    """L'orthographe tient en une seule règle par moteur : la désactiver couperait toute
    la vérification des mots. Pour un mot correct, c'est le dictionnaire qui convient."""
    return issue.category is not Category.SPELLING


class CheckRunner(QObject):
    """Exécute les vérifications en arrière-plan ; ne garde que la dernière demande en attente."""

    updated = Signal(int, object)  # (génération, CheckResult)

    def __init__(self, checker: Checker) -> None:
        super().__init__()
        self.checker = checker
        self._lock = threading.Lock()
        self._stopped = False
        self._busy = False
        self._pending: tuple | None = None

    def request(self, generation: int, text: str, only: Iterable[str] | None = None) -> None:
        job = (generation, text, tuple(only) if only is not None else None)
        with self._lock:
            if self._stopped:
                return
            if self._busy:
                self._pending = job
                return
            self._busy = True
        threading.Thread(target=self._loop, args=(job,), name="verification", daemon=True).start()

    def _loop(self, job: tuple) -> None:
        while job is not None:
            generation, text, only = job
            try:
                self.checker.check(text, on_update=lambda r, g=generation: self.updated.emit(g, r), only=only)
            except Exception:
                log.exception("Vérification impossible")
            with self._lock:
                job, self._pending = (None if self._stopped else self._pending), None
                self._busy = job is not None

    def shutdown(self) -> None:
        with self._lock:
            self._stopped = True
            self._pending = None


class IssueCard(QFrame):
    selected = Signal(int)

    def __init__(self, window: "CorrectionWindow", issue: Issue, text: str, theme: Theme) -> None:
        super().__init__()
        self.uid = issue.uid
        uid = issue.uid
        self.setObjectName("card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        color = theme.category_color(issue.category)
        self.setStyleSheet(f"QFrame#card {{ border-left: 4px solid {color}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        head = QHBoxLayout()
        cat = QLabel(issue.category.label.upper())
        cat.setStyleSheet(f"color: {color}; font-weight: 700; font-size: 8pt; letter-spacing: 0.5px;")
        head.addWidget(cat)
        if issue.confident:
            sure = QLabel("sûre")
            sure.setObjectName("tag")
            sure.setToolTip("Correction confirmée : appliquée par « Corrections sûres » et la correction express.")
            head.addWidget(sure)
        head.addStretch(1)
        sources = QLabel(" + ".join(ENGINE_LABELS.get(s, s) for s in issue.sources))
        sources.setObjectName("tag")
        head.addWidget(sources)
        layout.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(6)
        original = text[issue.start:issue.end]
        orig = QLabel(original.replace("\n", "⏎") if original.strip() else repr(original))
        orig.setObjectName("original")
        orig.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        row.addWidget(orig)
        if issue.replacements:
            row.addWidget(QLabel("→"))
            for i, rep in enumerate(issue.replacements[:5]):
                btn = QPushButton(rep if rep.strip() else "(supprimer)")
                btn.setObjectName("suggestion")
                btn.setProperty("first", i == 0)
                btn.setToolTip(f"Alt+{i + 1} quand la faute est sélectionnée (F8 : faute suivante)")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(lambda _=False, r=rep: window.apply_uid(uid, r))
                row.addWidget(btn)
        else:
            none = QLabel("pas de suggestion")
            none.setObjectName("muted")
            row.addWidget(none)
        row.addStretch(1)
        layout.addLayout(row)

        msg = QLabel(issue.message)
        msg.setObjectName("message")
        msg.setWordWrap(True)
        msg.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(msg)

        actions = QHBoxLayout()
        actions.addStretch(1)
        ignore = QPushButton("Ignorer")
        ignore.setObjectName("link")
        ignore.clicked.connect(lambda: window.ignore_uid(uid))
        actions.addWidget(ignore)
        if issue.category is Category.SPELLING:
            add = QPushButton("Ajouter au dictionnaire")
            add.setObjectName("link")
            add.clicked.connect(lambda: window.add_to_dictionary_uid(uid))
            actions.addWidget(add)
        more = QToolButton()
        more.setText("⋯")
        more.setAutoRaise(True)
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        if can_ignore_rule(issue):
            menu.addAction("Ne plus signaler cette règle", lambda: window.ignore_rule_uid(uid))
        if issue.url:
            menu.addAction("En savoir plus…", lambda: window.open_url(issue.url))
        rule = menu.addAction(f"Règle : {', '.join(issue.rules)}")
        rule.setEnabled(False)
        more.setMenu(menu)
        actions.addWidget(more)
        layout.addLayout(actions)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:
        self.selected.emit(self.uid)
        super().mousePressEvent(event)


class CorrectionWindow(QWidget):
    autoDone = Signal(str, str, int)         # (texte avant, texte corrigé, nombre de corrections)
    finished = Signal(object, bool)          # (capture, remplacé ?)
    replaceRequested = Signal(object, str)   # (capture, texte)
    settingsRequested = Signal()
    ignoreRulesRequested = Signal(list)     # identifiants des règles à ne plus signaler

    def __init__(self, checker: Checker, controller, theme: Theme) -> None:
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.checker = checker
        self.controller = controller
        self.theme = theme
        self.capture: Capture | None = None
        self._text = ""
        self._issues: list[Issue] = []
        self._statuses: dict[str, EngineStatus] = {}
        self._running: set[str] = set()
        self._no_engine = False  # dernière vérification complète lancée sans aucun moteur actif
        self._generation = 0
        self._selected: Issue | None = None
        self._cards: list[IssueCard] = []
        self._card_uids: list[tuple] = []
        self._closing_handled = False
        self._auto_running = False

        self.setWindowTitle("Correcteur")
        self.resize(640, 540)
        self.setMinimumSize(420, 360)

        self.runner = CheckRunner(checker)
        self.runner.updated.connect(self._on_result)
        self.autoDone.connect(self._on_auto_done)

        self._fast_timer = self._timer(_FAST_DELAY_MS, lambda: self._start_check(only=_FAST_ENGINES))
        self._full_timer = self._timer(_FULL_DELAY_MS, lambda: self._start_check())

        self._build()
        self._install_shortcuts()

    def _timer(self, delay: int, callback) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(delay)
        timer.timeout.connect(callback)
        return timer

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("Correcteur")
        title.setObjectName("title")
        head.addWidget(title)
        self.status = QLabel("")
        self.status.setObjectName("status")
        head.addWidget(self.status, 1)
        gear = QToolButton()
        gear.setText("⚙")
        gear.setToolTip("Paramètres")
        gear.setAutoRaise(True)
        gear.clicked.connect(self.settingsRequested.emit)
        head.addWidget(gear)
        root.addLayout(head)

        # Cause d'un échec de moteur, en toutes lettres (l'infobulle de l'état ne suffit pas).
        self.notice = QLabel("")
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.RichText)
        self.notice.hide()
        root.addWidget(self.notice)

        self.editor = CheckEditor(self.theme)
        self.editor.setPlaceholderText(
            "Sélectionnez du texte dans n'importe quelle application puis utilisez le raccourci, "
            "ou tapez / collez votre texte ici."
        )
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.issueClicked.connect(self._on_issue_clicked)
        self.editor.menu_builder = self._fill_issue_menu
        self.editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.editor, 3)

        info = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        info.addWidget(self.summary, 1)
        self.hint = QLabel("")
        self.hint.setObjectName("muted")
        info.addWidget(self.hint)
        root.addLayout(info)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 4, 0)
        self.list_layout.setSpacing(6)
        self.placeholder = QLabel("")
        self.placeholder.setObjectName("muted")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setTextFormat(Qt.TextFormat.PlainText)
        self.placeholder.hide()
        self.list_layout.addWidget(self.placeholder)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_host)
        root.addWidget(self.scroll, 4)

        foot = QHBoxLayout()
        self.fix_sure = QPushButton("Corrections sûres")
        self.fix_sure.setToolTip("Applique les corrections confirmées (accents, accords validés par les deux moteurs, ponctuation).")
        self.fix_sure.clicked.connect(self.apply_confident)
        foot.addWidget(self.fix_sure)
        more = QToolButton()
        more.setText("⋯")
        more.setToolTip("Plus d'actions")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_menu = QMenu(more)
        more_menu.addAction("Accepter toutes les premières suggestions", self.apply_all)
        more_menu.addAction("Revérifier", lambda: self._start_check())
        more.setMenu(more_menu)
        foot.addWidget(more)
        foot.addStretch(1)
        self.copy_btn = QPushButton("Copier")
        self.copy_btn.clicked.connect(self.copy_text)
        foot.addWidget(self.copy_btn)
        self.replace_btn = QPushButton("Remplacer")
        self.replace_btn.setObjectName("primary")
        self.replace_btn.setDefault(True)
        self.replace_btn.clicked.connect(self.replace_text)
        foot.addWidget(self.replace_btn)
        root.addLayout(foot)

    def _install_shortcuts(self) -> None:
        bindings = [
            ("Esc", self.close),
            ("Ctrl+Return", self.replace_text),
            ("Ctrl+Enter", self.replace_text),
            ("Ctrl+Shift+Return", self.apply_confident),
            ("Ctrl+R", lambda: self._start_check()),
            ("F8", lambda: self._select_relative(+1)),
            ("Shift+F8", lambda: self._select_relative(-1)),
        ]
        for keys, callback in bindings:
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(callback)
        # Alt+1…Alt+5 : suggestion n° 1 à 5. Sur un clavier AZERTY, la rangée du haut donne & é " ' ( sans Maj.
        for number, azerty in enumerate("&é\"'(", start=1):
            shortcut = QShortcut(self)
            shortcut.setKeys([QKeySequence(f"Alt+{number}"), QKeySequence(f"Alt+{azerty}")])
            # Les deux combinaisons peuvent correspondre à la même frappe : Qt la dit alors "ambiguë".
            shortcut.activated.connect(lambda n=number: self.apply_suggestion(n))
            shortcut.activatedAmbiguously.connect(lambda n=number: self.apply_suggestion(n))

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.editor.set_theme(theme)
        self._rebuild_cards(force=True)
        self._update_status()  # les pastilles d'état portent les couleurs du thème

    def open_with(self, text: str, capture: "Capture | None" = None) -> None:
        if self.isVisible() and self.capture is not None and self.capture is not capture:
            self.controller.release_capture(self.capture)
        self.capture = capture
        self._closing_handled = False
        self._issues = []
        self._statuses = {}
        self._no_engine = False
        self._selected = None
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self._text = self.editor.toPlainText()
        self._generation += 1
        self.editor.set_issues([], self._text)
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.editor.setTextCursor(cursor)
        has_target = capture is not None and capture.from_selection
        self.replace_btn.setText("Remplacer" if has_target else "Copier et fermer")
        self.replace_btn.setToolTip("Remplace le texte sélectionné dans l'application d'origine (Ctrl+Entrée)."
                                    if has_target else "Copie le texte corrigé et ferme la fenêtre (Ctrl+Entrée).")
        self.hint.setText("Ctrl+Entrée : " + ("remplacer" if has_target else "copier") + " · Échap : fermer")
        self._rebuild_cards(force=True)
        self._position_near_cursor()
        self.controller.bring_to_front(self)
        self.editor.setFocus()
        if text.strip():
            self._start_check()
        else:
            self._update_status()

    def _position_near_cursor(self) -> None:
        pos = QCursor.pos()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = min(max(pos.x() - self.width() // 2, area.left() + 8), area.right() - self.width() - 8)
        y = pos.y() + 24
        if y + self.height() > area.bottom() - 8:
            y = max(area.top() + 8, pos.y() - self.height() - 24)
        self.move(QPoint(x, y))

    def _start_check(self, only: Iterable[str] | None = None) -> None:
        self._fast_timer.stop()
        if only is None:
            self._full_timer.stop()
        if not self._text.strip():
            # Les réponses pour l'ancien texte seront ignorées : rien ne tourne plus pour celui-ci.
            self._running.clear()
            self._issues = []
            self._refresh_issues()
            return
        engines = [e.name for e in self.checker.active_engines(self._text, self.checker.resolve_language(self._text), only)]
        if only is None:
            self._no_engine = not engines
        self._running.update(engines)
        self._update_status()
        self.runner.request(self._generation, self._text, only)

    def recheck(self) -> None:
        """Revérifie tout le texte (après un changement de paramètres, par exemple)."""
        self._start_check()

    def _on_text_changed(self) -> None:
        new = self.editor.toPlainText()
        self._issues = shift_issues(self._issues, self._text, new)
        if self._selected is not None:
            self._selected = self._by_uid(self._selected.uid)
        self._text = new
        self._generation += 1
        self._refresh_issues()
        if self.controller.settings.general.live_check:
            self._fast_timer.start()
            self._full_timer.start()

    def _on_result(self, generation: int, result: CheckResult) -> None:
        if result.text != self._text:
            return  # texte modifié depuis : un résultat plus récent arrivera
        finished = set(result.statuses)
        self._running -= finished
        self._statuses.update(result.statuses)
        enabled = {e.name for e in self.checker.engines if e.is_enabled()}
        # Les fautes des moteurs qui n'ont pas (encore) répondu restent affichées,
        # recalées sur le texte : pas de clignotement pendant la frappe.
        kept = [i for i in self._issues if not set(i.sources) & finished and set(i.sources) <= enabled
                and not any(i.overlaps(n) for n in result.issues)]
        # Même faute qu'avant : on garde son identifiant (la liste n'est pas reconstruite).
        previous = {(i.start, i.end, i.rule_id): i.uid for i in self._issues}
        for issue in result.issues:
            issue.uid = previous.get((issue.start, issue.end, issue.rule_id), issue.uid)
        self._issues = sorted(result.issues + kept, key=lambda i: (i.start, i.end))
        if self._selected is not None:
            key = (self._selected.start, self._selected.end, self._selected.rule_id)
            self._selected = next((i for i in self._issues if (i.start, i.end, i.rule_id) == key), None)
        self._refresh_issues()

    def _by_uid(self, uid: int) -> Issue | None:
        return next((i for i in self._issues if i.uid == uid), None)

    def _refresh_issues(self) -> None:
        self.editor.set_issues(self._issues, self._text)
        self.editor.select_issue(self._selected, reveal=False)
        self._rebuild_cards()
        self._update_status()

    def _rebuild_cards(self, force: bool = False) -> None:
        uids = [(i.uid, i.sources, i.confident, tuple(i.replacements[:5]), i.message) for i in self._issues[:_MAX_CARDS]]
        if not force and uids and uids == self._card_uids:
            self._sync_card_selection()
            self._update_summary()
            return
        self._card_uids = uids
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards = []
        # Garde le texte d'état (premier élément) et le ressort (dernier).
        while self.list_layout.count() > 2:
            item = self.list_layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
        if not self._issues:
            self._update_summary()
            return
        self.list_host.setUpdatesEnabled(False)
        for issue in self._issues[:_MAX_CARDS]:
            card = IssueCard(self, issue, self._text, self.theme)
            card.selected.connect(lambda uid: self._select(self._by_uid(uid)))
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)
            # Visible tout de suite (sinon Qt attend le tour de boucle suivant) : la mise en page
            # et le défilement vers la faute sélectionnée peuvent se faire immédiatement.
            card.show()
            self._cards.append(card)
        if len(self._issues) > _MAX_CARDS:
            more = QLabel(f"… et {len(self._issues) - _MAX_CARDS} autres (corrigez les premières pour voir la suite).")
            more.setObjectName("muted")
            self.list_layout.insertWidget(self.list_layout.count() - 1, more)
            more.show()
        self.list_host.setUpdatesEnabled(True)
        self._sync_card_selection()
        self._update_summary()

    def _sync_card_selection(self) -> None:
        uid = self._selected.uid if self._selected is not None else None
        for card in self._cards:
            card.set_selected(card.uid == uid)

    def _update_summary(self) -> None:
        if self._auto_running:
            return
        if not self._issues:
            self.summary.setText("")
            self.fix_sure.setText("Corrections sûres")
            self.fix_sure.setEnabled(False)
            return
        counts: dict[Category, int] = {}
        for issue in self._issues:
            counts[issue.category] = counts.get(issue.category, 0) + 1
        parts = [f"{n} {cat.label.lower()}" for cat, n in counts.items()]
        total = len(self._issues)
        self.summary.setText(f"{total} remarque{'s' if total > 1 else ''} : " + " · ".join(parts))
        sure = len(split_confident(self._issues)[0])
        self.fix_sure.setText(f"Corrections sûres ({sure})")
        self.fix_sure.setEnabled(sure > 0)

    def _update_status(self) -> None:
        parts = []
        tips = []
        for engine in self.checker.engines:
            if not engine.is_enabled():
                continue
            label = ENGINE_LABELS.get(engine.name, engine.name)
            status = self._statuses.get(engine.name)
            if engine.name in self._running:
                parts.append(f"◌ {label}")
                tips.append(f"{label} : vérification en cours…")
            elif status is None:
                continue
            elif status.ok:
                parts.append(f"<span style='color:{self.theme.ok}'>●</span> {label}")
                tips.append(f"{label} : OK ({status.duration_ms:.0f} ms)")
            else:
                parts.append(f"<span style='color:{self.theme.warn}'>▲</span> {label}")
                tips.append(f"{label} : {status.detail}")
        self.status.setText("  ".join(parts))
        self.status.setToolTip("\n".join(tips))
        self._update_notice()
        self._update_placeholder()

    def _engine_report(self) -> tuple[list[str], list[str]]:
        """(moteurs qui ont vérifié le texte, causes des échecs) d'après les derniers états reçus."""
        ok: list[str] = []
        failed: list[str] = []
        for engine in self.checker.engines:
            status = self._statuses.get(engine.name)
            if status is None or not engine.is_enabled():
                continue
            label = ENGINE_LABELS.get(engine.name, engine.name)
            if status.ok:
                ok.append(label)
                continue
            cause = status.detail.strip() or "échec de la vérification."
            if label not in cause:
                cause = f"{label} : {cause}"
            if not cause.endswith((".", "!", "?", "…")):
                cause += "."
            failed.append(cause)
        return ok, failed

    def _nothing_checked(self, ok: list[str]) -> bool:
        """Vérification terminée sans aucun moteur valide : la liste affiche les causes."""
        return bool(self._text.strip()) and not self._running and not self._issues and not ok

    def _update_notice(self) -> None:
        ok, failed = self._engine_report()
        if not failed or self._nothing_checked(ok):
            self.notice.hide()
            self.notice.clear()
            return
        text = " ".join(failed)
        if ok:
            text += f" Texte vérifié par {' et '.join(ok)}{' seul' if len(ok) == 1 else ''}."
        self.notice.setText(f"<span style='color:{self.theme.warn}'>▲</span> {html.escape(text, quote=False)}")
        self.notice.show()

    def _update_placeholder(self) -> None:
        text = ""
        if self._text.strip() and not self._issues:
            ok, failed = self._engine_report()
            if self._running:
                text = "Vérification en cours…"
            elif ok:
                text = "✓ Aucune faute trouvée."
            elif failed:
                text = "\n".join(["Aucun moteur n'a pu vérifier le texte."] + failed)
            elif self._no_engine:
                text = "Aucun moteur actif pour ce texte (Paramètres, onglet Moteurs)."
        self.placeholder.setText(text)
        self.placeholder.setVisible(bool(text))

    def _select(self, issue: Issue | None, reveal: bool = True) -> None:
        self._selected = issue
        self.editor.select_issue(issue, reveal=reveal)
        self._sync_card_selection()
        card = next((c for c in self._cards if issue is not None and c.uid == issue.uid), None)
        if card is not None:
            self._fit_list()
            self.scroll.ensureWidgetVisible(card)

    def _fit_list(self) -> None:
        """Donne tout de suite à la liste sa hauteur réelle. Juste après une reconstruction, Qt ne
        la recalcule qu'aux tours de boucle suivants, et le défilement viserait des positions périmées."""
        width = self.scroll.viewport().width()
        layout = self.list_layout
        layout.activate()
        height = layout.heightForWidth(width) if layout.hasHeightForWidth() else layout.sizeHint().height()
        self.list_host.resize(width, max(height, self.scroll.viewport().height()))
        layout.activate()

    def _select_relative(self, step: int) -> None:
        if not self._issues:
            return
        uids = [i.uid for i in self._issues]
        if self._selected is not None and self._selected.uid in uids:
            idx = (uids.index(self._selected.uid) + step) % len(self._issues)
        else:
            idx = 0 if step > 0 else len(self._issues) - 1
        self._select(self._issues[idx])

    def _on_issue_clicked(self, issue: Issue, global_pos: QPoint) -> None:
        self._select(issue, reveal=False)
        menu = QMenu(self)
        self._fill_issue_menu(menu, issue)
        menu.exec(global_pos + QPoint(0, 12))
        menu.deleteLater()

    def _fill_issue_menu(self, menu: QMenu, issue: Issue) -> None:
        title = QAction(issue.message if len(issue.message) < 90 else issue.message[:87] + "…", menu)
        title.setEnabled(False)
        menu.addAction(title)
        for number, rep in enumerate(issue.replacements[:6], start=1):
            label = f"→ {rep}" if rep.strip() else "→ (supprimer)"
            action = menu.addAction(label + (f"\tAlt+{number}" if number <= 5 else ""))
            font = action.font()
            font.setBold(True)
            action.setFont(font)
            action.triggered.connect(lambda _=False, r=rep: self.apply(issue, r))
        menu.addSeparator()
        menu.addAction("Ignorer", lambda: self.ignore(issue))
        if issue.category is Category.SPELLING:
            menu.addAction("Ajouter au dictionnaire", lambda: self.add_to_dictionary(issue))
        if can_ignore_rule(issue):
            menu.addAction("Ne plus signaler cette règle", lambda: self.ignore_rule(issue))

    def _replace_range(self, edits: list[tuple[Issue, str]]) -> None:
        cursor = QTextCursor(self.editor.document())
        cursor.beginEditBlock()
        for issue, rep in sorted(edits, key=lambda e: e[0].start, reverse=True):
            start, end = self.editor.qt_range(issue)
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(rep)
        cursor.endEditBlock()

    def apply(self, issue: Issue, replacement: str) -> None:
        self.apply_uid(issue.uid, replacement)

    def apply_uid(self, uid: int, replacement: str) -> None:
        issue = self._by_uid(uid)
        if issue is None:
            return
        self._replace_range([(issue, replacement)])
        # Passe à la faute suivante pour enchaîner les corrections au clavier.
        after = issue.start + len(replacement)
        nxt = [i for i in self._issues if i.start >= after]
        self._select(nxt[0] if nxt else (self._issues[0] if self._issues else None))

    def apply_suggestion(self, number: int) -> None:
        """Alt+1…Alt+5 : applique la suggestion n° `number` de la faute sélectionnée
        (sinon de celle où se trouve le curseur), puis passe à la faute suivante."""
        issue = self._by_uid(self._selected.uid) if self._selected is not None else None
        issue = issue or self.editor.issue_at_cursor()
        if issue is None or not 0 < number <= len(issue.replacements):
            return
        self.apply_uid(issue.uid, issue.replacements[number - 1])

    def apply_confident(self) -> None:
        """Corrections sûres, en plusieurs passes (voir Checker.autocorrect), en arrière-plan."""
        if self._auto_running or not self._text.strip():
            return
        self._auto_running = True
        before = self._text
        self.fix_sure.setEnabled(False)
        self.fix_sure.setText("Correction…")

        def work() -> None:
            try:
                after, applied, _remaining, _result = self.checker.autocorrect(before)
            except Exception:
                log.exception("Corrections sûres impossibles")
                after, applied = before, []
            self.autoDone.emit(before, after, len(applied))

        threading.Thread(target=work, name="corrections-sures", daemon=True).start()

    def _on_auto_done(self, before: str, after: str, count: int) -> None:
        self._auto_running = False
        if before != self._text or after == before:
            self._update_summary()  # texte modifié entre-temps, ou rien à corriger
            return
        positions = PositionMap(before)
        cursor = QTextCursor(self.editor.document())
        cursor.beginEditBlock()  # un seul Ctrl+Z annule tout
        for start, end, rep in reversed(diff_edits(before, after)):
            cursor.setPosition(positions.to_qt(start))
            cursor.setPosition(positions.to_qt(end), QTextCursor.MoveMode.KeepAnchor)
            cursor.insertText(rep)
        cursor.endEditBlock()
        self._start_check()
        self.controller.notify(f"{count} correction{'s' if count > 1 else ''} sûre{'s' if count > 1 else ''} appliquée{'s' if count > 1 else ''}.")

    def apply_all(self) -> None:
        taken: list[Issue] = []
        for issue in self._issues:
            if issue.replacements and not any(issue.overlaps(t) for t in taken):
                taken.append(issue)
        if taken:
            self._replace_range([(i, i.replacements[0]) for i in taken])

    def _drop(self, predicate) -> None:
        self._issues = [i for i in self._issues if not predicate(i)]
        if self._selected is not None and self._by_uid(self._selected.uid) is None:
            self._selected = None
        self._refresh_issues()

    def ignore(self, issue: Issue) -> None:
        self.checker.ignore_once(self._text, issue)
        original = issue.original(self._text)
        self._drop(lambda i: i.uid == issue.uid or (i.category is issue.category and i.original(self._text) == original))

    def ignore_uid(self, uid: int) -> None:
        issue = self._by_uid(uid)
        if issue is not None:
            self.ignore(issue)

    def add_to_dictionary(self, issue: Issue) -> None:
        word = issue.original(self._text).strip()
        if self.checker.dictionary is not None and word:
            self.checker.dictionary.add(word)
        self._drop(lambda i: i.category is Category.SPELLING and i.original(self._text).strip() == word)

    def add_to_dictionary_uid(self, uid: int) -> None:
        issue = self._by_uid(uid)
        if issue is not None:
            self.add_to_dictionary(issue)

    def ignore_rule(self, issue: Issue) -> None:
        # Toutes les règles de la faute : sinon l'autre moteur la resignalerait à la vérification suivante.
        rules = set(issue.rules)
        self.ignoreRulesRequested.emit(list(issue.rules))
        self._drop(lambda i: bool(rules & set(i.rules)))

    def ignore_rule_uid(self, uid: int) -> None:
        issue = self._by_uid(uid)
        if issue is not None:
            self.ignore_rule(issue)

    def open_url(self, url: str) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(url))

    def copy_text(self) -> None:
        self.controller.copy_text(self.editor.toPlainText())
        self.controller.notify("Texte copié dans le presse-papiers.")

    def replace_text(self) -> None:
        text = self.editor.toPlainText()
        capture = self.capture
        self._closing_handled = True
        self.hide()
        if capture is not None and capture.from_selection:
            self.replaceRequested.emit(capture, text)
            self.finished.emit(capture, True)
        else:
            self.controller.copy_text(text)
            self.controller.notify("Texte corrigé copié : collez-le avec Ctrl+V.")
            self.finished.emit(capture, False)
        self.capture = None

    def closeEvent(self, event) -> None:
        self._fast_timer.stop()
        self._full_timer.stop()
        if not self._closing_handled:
            self._closing_handled = True
            self.finished.emit(self.capture, False)
            self.capture = None
        super().closeEvent(event)

    def shortcut_hint(self) -> str:
        return hotkey_display(self.controller.settings.general.hotkey_check)
