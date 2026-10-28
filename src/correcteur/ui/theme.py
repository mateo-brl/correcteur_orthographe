"""Couleurs et feuille de style (clair / sombre, selon le système par défaut)."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleFactory

from correcteur.models import Category


@dataclass(frozen=True)
class Theme:
    dark: bool
    bg: str
    surface: str
    surface2: str
    border: str       # séparateurs discrets (onglets, groupes, infobulles)
    outline: str      # contour des composants : au moins 3:1 contre bg, surface et surface2
    text: str
    muted: str
    accent: str
    accent_text: str
    spelling: str
    grammar: str
    typography: str
    style: str
    ok: str
    warn: str

    def category_color(self, category: Category) -> str:
        return {
            Category.SPELLING: self.spelling,
            Category.GRAMMAR: self.grammar,
            Category.TYPOGRAPHY: self.typography,
            Category.STYLE: self.style,
        }[category]

    def qcolor(self, category: Category) -> QColor:
        return QColor(self.category_color(category))


# Actions en couleur neutre (encre) : les couleurs vives restent réservées aux catégories de fautes.
LIGHT = Theme(
    dark=False, bg="#f6f7f9", surface="#ffffff", surface2="#eef0f4", border="#d9dde5", outline="#7d8696",
    text="#1d2330", muted="#5f687c", accent="#232a38", accent_text="#ffffff", spelling="#d92d20",
    grammar="#2f6fed", typography="#a65f00", style="#7a5af8", ok="#12a150", warn="#a65f00",
)
DARK = Theme(
    dark=True, bg="#16181d", surface="#1f2229", surface2="#2a2e37", border="#373c47", outline="#737b8c",
    text="#e7e9ee", muted="#98a2b3", accent="#dde1e8", accent_text="#16181d", spelling="#ff6b61",
    grammar="#6b9bff", typography="#f5b441", style="#a48afb", ok="#3ccf7d", warn="#f5b441",
)


def system_is_dark() -> bool:
    hints = QGuiApplication.styleHints()
    scheme = getattr(hints, "colorScheme", None)
    if scheme is not None:
        try:
            return scheme() == Qt.ColorScheme.Dark
        except Exception:
            pass
    return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


def current_theme(choice: str = "auto") -> Theme:
    if choice == "sombre":
        return DARK
    if choice == "clair":
        return LIGHT
    return DARK if system_is_dark() else LIGHT


class ThemedStyle(QProxyStyle):
    """Fusion, avec les cases à cocher et les boutons radio aux couleurs du thème. Fusion tire leur contour
    du fond de la fenêtre assombri : sur le thème sombre, une case vide ou un bouton radio non choisi
    disparaissaient presque (contour sous 3:1, WCAG 1.4.11)."""

    def __init__(self, theme: Theme) -> None:
        super().__init__(QStyleFactory.create("Fusion"))
        self.theme = theme

    def drawPrimitive(self, element, option, painter, widget=None) -> None:
        radio = element == QStyle.PrimitiveElement.PE_IndicatorRadioButton
        if not radio and element != QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            super().drawPrimitive(element, option, painter, widget)
            return
        t = self.theme
        state = option.state
        on = bool(state & QStyle.StateFlag.State_On)
        enabled = bool(state & QStyle.StateFlag.State_Enabled)
        hover = enabled and bool(state & QStyle.StateFlag.State_MouseOver)
        # Coché : rempli d'encre, comme les boutons principaux. Désactivé : en retrait, comme les champs grisés.
        if not enabled:
            border, fill, mark = t.outline, t.surface2, t.muted
        elif on:
            border, fill, mark = t.accent, t.accent, t.accent_text
        else:
            border, fill, mark = (t.accent if hover else t.outline), t.surface, t.accent_text
        side = min(option.rect.width(), option.rect.height())
        box = QRectF(option.rect.x(), option.rect.y(), side, side).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(border), 1.25))  # 1 px : un cercle anticrénelé passait sous 3:1
        painter.setBrush(QColor(fill))
        if radio:
            painter.drawEllipse(box)
            if on:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(mark))
                painter.drawEllipse(box.center(), side * 0.2, side * 0.2)
        else:
            painter.drawRoundedRect(box, 3, 3)
            if on:
                check = QPainterPath(QPointF(box.left() + side * 0.24, box.top() + side * 0.52))
                check.lineTo(QPointF(box.left() + side * 0.42, box.top() + side * 0.70))
                check.lineTo(QPointF(box.left() + side * 0.76, box.top() + side * 0.32))
                pen = QPen(QColor(mark), max(1.5, side / 8))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(check)
        painter.restore()


def apply_theme(app: QApplication, theme: Theme) -> None:
    app.setStyle(ThemedStyle(theme))
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(theme.bg))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(theme.text))
    pal.setColor(QPalette.ColorRole.Base, QColor(theme.surface))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(theme.surface2))
    pal.setColor(QPalette.ColorRole.Text, QColor(theme.text))
    pal.setColor(QPalette.ColorRole.Button, QColor(theme.surface2))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(theme.text))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(theme.surface))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(theme.text))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(theme.accent))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(theme.accent_text))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(theme.muted))
    pal.setColor(QPalette.ColorRole.Link, QColor(theme.accent))
    # setColor(rôle, couleur) vaut pour tous les états : sans ces lignes, un champ désactivé gardait le texte
    # normal et ne se distinguait pas d'un champ actif.
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(theme.muted))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor(theme.surface2))  # champ en retrait
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(theme))


def stylesheet(t: Theme) -> str:
    # Le focus passe le contour à 2 px ; la marge intérieure perd 1 px pour que rien ne bouge.
    # Sur les boutons remplis d'encre, l'anneau prend la couleur de contour : il tranche avec le remplissage et le fond.
    # Appui : le fond change aussitôt, sans animation (MOTION 1), pour que le clic se voie.
    # Bouton principal désactivé : il reprend l'aspect des autres boutons désactivés (le sélecteur #primary
    # l'emportait sur :disabled, et « Remplacer » grisé restait rempli d'encre comme s'il marchait).
    # Les « ⋯ » ouvrent un menu à eux seuls : la flèche que Fusion y ajoutait chevauchait les points.
    return f"""
    QWidget {{ font-size: 10pt; }}
    QToolTip {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border}; padding: 6px; }}
    QPlainTextEdit#editor {{
        background: {t.surface}; border: 1px solid {t.outline}; border-radius: 8px; padding: 6px;
        font-size: 11pt; selection-background-color: {t.accent}; selection-color: {t.accent_text};
    }}
    QPlainTextEdit#editor:focus {{ border-color: {t.accent}; }}
    QFrame#card {{ background: {t.surface}; border: 1px solid {t.outline}; border-radius: 8px; padding: 1px; }}
    QFrame#card[selected="true"] {{ border: 2px solid {t.accent}; padding: 0px 0px 0px 1px; }}
    QLabel#muted, QLabel#status {{ color: {t.muted}; }}
    QLabel#notice {{ color: {t.text}; }}
    QLabel#title {{ font-weight: 600; font-size: 11pt; }}
    QLabel#message {{ color: {t.text}; }}
    QLabel#original {{ color: {t.muted}; text-decoration: line-through; }}
    QLabel#tag {{ color: {t.muted}; background: {t.surface2}; border-radius: 4px; padding: 1px 6px; font-size: 8pt; }}
    QPushButton {{
        background: {t.surface2}; color: {t.text}; border: 1px solid {t.outline}; border-radius: 6px; padding: 5px 12px;
    }}
    QPushButton:hover {{ border-color: {t.accent}; }}
    QPushButton:focus {{ border: 2px solid {t.accent}; padding: 4px 11px; }}
    QPushButton:pressed {{ background: {t.border}; }}
    QPushButton:disabled {{ color: {t.muted}; }}
    QPushButton#primary {{ background: {t.accent}; color: {t.accent_text}; border-color: {t.accent}; font-weight: 600; }}
    QPushButton#primary:focus {{ border: 2px solid {t.outline}; padding: 4px 11px; }}
    QPushButton#primary:pressed {{ background: {t.muted}; border-color: {t.muted}; }}
    QPushButton#primary:disabled {{ background: {t.surface2}; color: {t.muted}; border-color: {t.outline}; }}
    QPushButton#suggestion {{ border-radius: 5px; padding: 3px 10px; font-weight: 600; }}
    QPushButton#suggestion:focus {{ padding: 2px 9px; }}
    QPushButton#suggestion[first="true"] {{ background: {t.accent}; color: {t.accent_text}; border-color: {t.accent}; }}
    QPushButton#suggestion[first="true"]:focus {{ border: 2px solid {t.outline}; }}
    QPushButton#suggestion[first="true"]:pressed {{ background: {t.muted}; border-color: {t.muted}; }}
    QPushButton#link {{ background: transparent; border: 1px solid transparent; color: {t.muted}; padding: 2px 4px; }}
    QPushButton#link:hover {{ color: {t.accent}; }}
    QPushButton#link:focus {{ border: 2px solid {t.accent}; color: {t.text}; padding: 1px 3px; }}
    QPushButton#link:pressed {{ background: {t.surface2}; color: {t.text}; }}
    QToolButton {{
        background: {t.surface2}; color: {t.text}; border: 1px solid {t.outline}; border-radius: 6px; padding: 3px 6px;
    }}
    QToolButton[autoRaise="true"] {{ background: transparent; border-color: transparent; }}
    QToolButton:hover {{ border-color: {t.accent}; }}
    QToolButton:focus {{ border: 2px solid {t.accent}; padding: 2px 5px; }}
    QToolButton:pressed {{ background: {t.border}; }}
    QToolButton::menu-indicator {{ image: none; width: 0px; }}
    QScrollArea {{ border: none; background: transparent; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QTabWidget::pane {{ border: 1px solid {t.border}; border-radius: 6px; top: -1px; }}
    QGroupBox {{ border: 1px solid {t.border}; border-radius: 6px; margin-top: 12px; padding-top: 8px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; font-weight: 600; }}
    """
