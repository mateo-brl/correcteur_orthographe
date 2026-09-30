"""Zone de texte avec soulignements ondulés colorés, façon traitement de texte."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QMenu, QPlainTextEdit, QTextEdit, QToolTip

from correcteur.models import Issue
from correcteur.textutils import utf16_to_index
from correcteur.ui.theme import Theme


class PositionMap:
    """Conversion indices Python <-> positions Qt (UTF-16 : un emoji compte double)."""

    def __init__(self, text: str) -> None:
        self.identity = len(text.encode("utf-16-le")) == 2 * len(text)
        if self.identity:
            return
        self._to_qt = [0] * (len(text) + 1)
        pos = 0
        for i, ch in enumerate(text):
            self._to_qt[i] = pos
            pos += 2 if ord(ch) > 0xFFFF else 1
        self._to_qt[len(text)] = pos
        self._to_py = utf16_to_index(text)

    def to_qt(self, index: int) -> int:
        if self.identity:
            return index
        return self._to_qt[max(0, min(index, len(self._to_qt) - 1))]

    def to_py(self, position: int) -> int:
        return position if self.identity else self._to_py(position)


class CheckEditor(QPlainTextEdit):
    issueClicked = Signal(object, QPoint)

    def __init__(self, theme: Theme, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("editor")
        self.setMouseTracking(True)
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._theme = theme
        self._issues: list[Issue] = []
        self._selected: Issue | None = None
        self._map = PositionMap("")
        self.menu_builder: Callable[[QMenu, Issue], None] | None = None

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self._refresh()

    def set_issues(self, issues: list[Issue], text: str) -> None:
        self._issues = issues
        self._map = PositionMap(text)
        if self._selected is not None:
            self._selected = next((i for i in issues if i.uid == self._selected.uid), None)
        self._refresh()

    def select_issue(self, issue: Issue | None, reveal: bool = True) -> None:
        self._selected = issue
        self._refresh()
        if issue is not None and reveal:
            cursor = self.textCursor()
            cursor.setPosition(self._map.to_qt(issue.start))
            self.setTextCursor(cursor)
            self.ensureCursorVisible()

    def qt_range(self, issue: Issue) -> tuple[int, int]:
        start, end = self._map.to_qt(issue.start), self._map.to_qt(issue.end)
        if end <= start:
            end = min(start + 1, self.document().characterCount() - 1)
        return start, end

    def _refresh(self) -> None:
        selections = []
        doc = self.document()
        for issue in self._issues:
            start, end = self.qt_range(issue)
            sel = QTextEdit.ExtraSelection()
            cursor = QTextCursor(doc)
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            sel.cursor = cursor
            fmt = QTextCharFormat()
            color = self._theme.qcolor(issue.category)
            fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
            fmt.setUnderlineColor(color)
            if self._selected is not None and issue.uid == self._selected.uid:
                bg = QColor(color)
                bg.setAlpha(55)
                fmt.setBackground(bg)
            sel.format = fmt
            selections.append(sel)
        self.setExtraSelections(selections)

    def issue_at(self, pos: QPoint) -> Issue | None:
        cursor = self.cursorForPosition(pos)
        qt_pos = cursor.position()
        # Le curseur tombe entre deux caractères : on regarde celui sous la souris.
        if pos.x() < self.cursorRect(cursor).x() and qt_pos > 0:
            qt_pos -= 1
        index = self._map.to_py(qt_pos)
        for issue in self._issues:
            if issue.start <= index < max(issue.end, issue.start + 1):
                return issue
        return None

    def issue_at_cursor(self) -> Issue | None:
        """Faute sous le curseur de saisie (juste avant, au milieu ou juste après le mot)."""
        index = self._map.to_py(self.textCursor().position())
        return next((i for i in self._issues if i.start <= index <= max(i.end, i.start + 1)), None)

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        issue = self.issue_at(event.position().toPoint())
        if issue is None:
            QToolTip.hideText()
            return
        hint = issue.message
        if issue.replacements:
            hint += "\n→ " + " / ".join(issue.replacements[:3])
        QToolTip.showText(event.globalPosition().toPoint(), hint, self)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton or self.textCursor().hasSelection():
            return
        issue = self.issue_at(event.position().toPoint())
        if issue is not None:
            self.issueClicked.emit(issue, event.globalPosition().toPoint())

    def contextMenuEvent(self, event) -> None:
        menu = self.createStandardContextMenu()
        issue = self.issue_at(event.pos())
        if issue is not None and self.menu_builder is not None:
            first = menu.actions()[0] if menu.actions() else None
            extra = QMenu(self)
            self.menu_builder(extra, issue)
            for action in extra.actions():
                menu.insertAction(first, action)
            menu.insertSeparator(first)
            menu.aboutToHide.connect(extra.deleteLater)
        menu.exec(event.globalPos())
        menu.deleteLater()
