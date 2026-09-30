"""Tests de l'interface (Qt hors écran en local, Xvfb en CI)."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from correcteur.checker import Checker  # noqa: E402
from correcteur.config import PersonalDictionary, Settings  # noqa: E402
from correcteur.models import Category, Issue  # noqa: E402
from correcteur.ui.popup import shift_issues  # noqa: E402
from test_checker import TEXT, FakeEngine, g_issues, lt_issues  # noqa: E402


class FakeController:
    def __init__(self, settings, dictionary):
        self.settings = settings
        self.dictionary = dictionary
        self.copied = []
        self.notes = []
        self.released = []

        class _Bridge:
            @staticmethod
            def diagnostics():
                return ["Session : test"]

        self.bridge = _Bridge()

    def copy_text(self, text):
        self.copied.append(text)

    def notify(self, text, error=False):
        self.notes.append(text)

    def bring_to_front(self, widget):
        widget.show()

    def release_capture(self, capture):
        self.released.append(capture)

    def autostart_enabled(self):
        return False

    def apply_settings(self, settings, words=None):
        self.settings = settings
        self.applied_words = words


def wait_until(qapp, predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def make_window(qapp, home):
    """make_window(grammalecte={...}, languagetool={...}) : arguments de FakeEngine pour chaque moteur."""
    from correcteur.ui.popup import CorrectionWindow
    from correcteur.ui.theme import LIGHT

    made = []

    def make(**engines):
        settings = Settings()
        dictionary = PersonalDictionary()
        checker = Checker(settings, dictionary, engines=[FakeEngine(settings, name, **kw) for name, kw in engines.items()])
        w = CorrectionWindow(checker, FakeController(settings, dictionary), LIGHT)
        made.append((w, checker))
        return w

    yield make
    for w, checker in made:
        w.runner.shutdown()
        w.close()
        checker.close()


@pytest.fixture
def window(make_window):
    return make_window(grammalecte=dict(found=g_issues), languagetool=dict(found=lt_issues, delay=0.05))


def open_and_wait(qapp, w, text=TEXT, capture=None):
    w.open_with(text, capture)
    assert wait_until(qapp, lambda: not w._running and len(w._statuses) == 2)


def test_open_check_and_cards(qapp, window):
    open_and_wait(qapp, window)
    words = {window._text[i.start:i.end] for i in window._issues}
    assert words == {"obtenu", "sont", "bon", "ecole", " ,", "fesait"}
    assert len(window._cards) == len(window._issues)
    assert "Corrections sûres (4)" == window.fix_sure.text()


def test_apply_suggestion_updates_text_and_shifts(qapp, window):
    open_and_wait(qapp, window)
    target = next(i for i in window._issues if window._text[i.start:i.end] == "obtenu")
    window.apply(target, "obtenus")
    assert "obtenus sont" in window.editor.toPlainText()
    # Les fautes suivantes sont recalées sans attendre la revérification.
    ecole = next(i for i in window._issues if i.category is Category.SPELLING and i.replacements[0] == "école")
    assert window._text[ecole.start:ecole.end] == "ecole"


def test_keyboard_applies_suggestions(qapp, window):
    """F8 pour aller à une faute, Alt+1…5 pour choisir la suggestion (Alt+& … sur AZERTY)."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    open_and_wait(qapp, window)
    window.activateWindow()
    assert wait_until(qapp, lambda: qapp.activeWindow() is window)
    window._select(next(i for i in window._issues if window._text[i.start:i.end] == "obtenu"))
    QTest.keyClick(window.editor, Qt.Key.Key_1, Qt.KeyboardModifier.AltModifier)
    assert "obtenus sont" in window.editor.toPlainText()
    # La faute suivante est sélectionnée : on enchaîne sans la souris.
    assert window._text[window._selected.start:window._selected.end] == "sont"
    QTest.keyClick(window.editor, Qt.Key.Key_Ampersand, Qt.KeyboardModifier.AltModifier)
    assert "obtenus son bon" in window.editor.toPlainText()

    # Sans faute sélectionnée : celle où se trouve le curseur.
    window._select(None)
    cursor = window.editor.textCursor()
    cursor.setPosition(window.editor.toPlainText().index("fesait") + 2)
    window.editor.setTextCursor(cursor)
    window.apply_suggestion(2)
    assert "vessait-il" in window.editor.toPlainText()
    window.apply_suggestion(9)  # pas de 9e suggestion : rien ne se passe


def test_apply_confident_and_undo(qapp, window):
    open_and_wait(qapp, window)
    window.apply_confident()
    expected = "Les résultats que j'ai obtenus sont bons et école ferme, fesait-il."
    assert wait_until(qapp, lambda: window.editor.toPlainText() == expected, 5)
    assert window.controller.notes[-1].startswith("4 corrections sûres")
    window.editor.undo()  # une seule annulation pour toutes les corrections
    assert window.editor.toPlainText() == TEXT


def test_ignore_and_dictionary(qapp, window):
    open_and_wait(qapp, window)
    fesait = next(i for i in window._issues if window._text[i.start:i.end] == "fesait")
    window.add_to_dictionary(fesait)
    assert "fesait" in window.checker.dictionary
    assert all(window._text[i.start:i.end] != "fesait" for i in window._issues)
    sont = next(i for i in window._issues if window._text[i.start:i.end] == "sont")
    window.ignore(sont)
    assert all(window._text[i.start:i.end] != "sont" for i in window._issues)


def test_ignore_rule_covers_both_engines_and_spares_spelling(qapp, window):
    from PySide6.QtWidgets import QMenu

    open_and_wait(qapp, window)
    requested = []
    window.ignoreRulesRequested.connect(requested.append)
    obtenu = next(i for i in window._issues if window._text[i.start:i.end] == "obtenu")
    window.ignore_rule(obtenu)
    assert requested == [["G_PPAS", "QUE_AVOIR"]]
    assert all(window._text[i.start:i.end] != "obtenu" for i in window._issues)

    # Orthographe : une seule règle par moteur pour tous les mots, on propose le dictionnaire à la place.
    fesait = next(i for i in window._issues if window._text[i.start:i.end] == "fesait")
    menu = QMenu()
    window._fill_issue_menu(menu, fesait)
    labels = [a.text() for a in menu.actions()]
    assert "Ajouter au dictionnaire" in labels and "Ne plus signaler cette règle" not in labels
    menu.deleteLater()


def test_live_recheck_after_typing(qapp, window):
    open_and_wait(qapp, window)
    g, lt = window.checker.engines
    before = (g.calls, lt.calls)
    cursor = window.editor.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    window.editor.setTextCursor(cursor)
    window.editor.insertPlainText(" Encore.")
    # Revérification rapide (Grammalecte) puis complète (les deux moteurs).
    assert wait_until(qapp, lambda: g.calls > before[0], 3)
    assert wait_until(qapp, lambda: lt.calls > before[1], 5)
    assert window._text.endswith(" Encore.")
    assert {window._text[i.start:i.end] for i in window._issues} >= {"obtenu", "ecole", "fesait"}


def test_replace_without_capture_copies(qapp, window):
    open_and_wait(qapp, window)
    finished = []
    window.finished.connect(lambda cap, replaced: finished.append(replaced))
    window.replace_text()
    assert window.controller.copied[-1] == TEXT
    assert finished == [False]
    assert not window.isVisible()


def test_replace_with_capture_emits_request(qapp, window):
    from correcteur.platform.bridge import Capture

    capture = Capture("Ces un test.", target=123, from_selection=True)
    window.open_with(capture.text, capture)
    requested = []
    window.replaceRequested.connect(lambda cap, text: requested.append((cap, text)))
    window.replace_text()
    assert requested == [(capture, "Ces un test.")]


def test_escape_releases_capture(qapp, window):
    from correcteur.platform.bridge import Capture

    capture = Capture("Texte.", target=1, from_selection=True)
    window.open_with(capture.text, capture)
    finished = []
    window.finished.connect(lambda cap, replaced: finished.append((cap, replaced)))
    window.close()
    assert finished == [(capture, False)]


def test_emoji_positions_in_editor(qapp, window):
    text = "Super 😀 et ecole fermée"
    window.open_with(text, None)
    issue = Issue(text.index("ecole"), text.index("ecole") + 5, "m", ["école"], Category.SPELLING, "R", "grammalecte")
    window._issues = [issue]
    window._refresh_issues()
    window.apply(issue, "école")
    assert window.editor.toPlainText() == "Super 😀 et école fermée"


def test_express_message_lists_changes_and_missing_engines():
    from correcteur.models import EngineStatus
    from correcteur.ui.app import express_message

    before = "Je suis aller a la réunion, les résultats que j'ai obtenu sont bon et on a bien rigoler."
    after = "Je suis allé à la réunion, les résultats que j'ai obtenus sont bons et on a bien rigolé."
    slow = {"languagetool": EngineStatus("languagetool", False, "LanguageTool a mis plus de 8 s à répondre.")}
    text, error = express_message(before, after, 5, 1, slow, "Ctrl+Alt+C")
    assert not error
    assert text.splitlines() == [
        "5 corrections appliquées : aller → allé, a → à, obtenu → obtenus…",
        "1 remarque à vérifier (Ctrl+Alt+C).",
        "LanguageTool a mis plus de 8 s à répondre.",
    ]
    # Une espace en trop n'a rien de lisible à montrer : seulement le nombre.
    assert express_message("le  mail", "le mail", 1, 0, {}, "X") == ("1 correction appliquée.", False)
    assert express_message("ok", "ok", 0, 0, {}, "X") == ("Aucune faute trouvée ✓", False)
    offline = {"grammalecte": EngineStatus("grammalecte", False, "non installé.")}
    assert express_message("ok", "ok", 0, 0, offline, "X") == ("Vérification incomplète : Grammalecte : non installé.", True)


def test_shift_issues():
    text = "aaa bbb ccc"
    issues = [Issue(0, 3, "", [], Category.GRAMMAR, "A", "g"), Issue(4, 7, "", [], Category.GRAMMAR, "B", "g"),
              Issue(8, 11, "", [], Category.GRAMMAR, "C", "g")]
    new = "aaa XXXXX ccc"
    shifted = shift_issues(issues, text, new)
    assert [(new[i.start:i.end]) for i in shifted] == ["aaa", "ccc"]
    assert shifted[1].uid == issues[2].uid


def test_settings_dialog_saves(qapp, home):
    from correcteur.ui.settings import SettingsDialog

    settings = Settings()
    controller = FakeController(settings, PersonalDictionary())
    dialog = SettingsDialog(settings, controller)
    dialog.typo_strict.setChecked(True)
    dialog.new_word.setText("Zorglub")
    dialog._add_word()
    dialog._lt_radios["local"].setChecked(True)
    dialog._accept()
    assert controller.settings.general.typography == "stricte"
    assert controller.settings.languagetool.mode == "local"
    assert "Zorglub" in controller.applied_words
    assert settings.general.typography == "standard"  # l'original n'est pas modifié


def test_theme_switch(qapp, window):
    from correcteur.ui.theme import DARK

    open_and_wait(qapp, window)
    window.set_theme(DARK)
    assert len(window._cards) == len(window._issues)


def test_loading_text_until_first_issues(qapp, make_window):
    w = make_window(grammalecte=dict(delay=0.3), languagetool=dict(found=lt_issues, delay=1.0))
    w.open_with(TEXT, None)
    qapp.processEvents()
    assert not w.placeholder.isHidden()
    assert w.placeholder.text() == "Vérification en cours…"
    # Grammalecte ne trouve rien, LanguageTool travaille encore : le texte reste affiché.
    assert wait_until(qapp, lambda: "grammalecte" in w._statuses)
    assert w._running == {"languagetool"}
    assert w.placeholder.text() == "Vérification en cours…"
    assert wait_until(qapp, lambda: not w._running)
    assert w.placeholder.isHidden()
    assert len(w._cards) == len(w._issues) > 0


def test_loading_text_on_recheck(qapp, make_window):
    w = make_window(grammalecte=dict(delay=0.2), languagetool=dict(delay=0.2))
    open_and_wait(qapp, w, "Bonjour à tous.")
    assert w.placeholder.text() == "✓ Aucune faute trouvée."
    w._start_check()  # Ctrl+R : la liste vide doit annoncer la vérification
    assert w.placeholder.text() == "Vérification en cours…"
    assert wait_until(qapp, lambda: not w._running)
    assert w.placeholder.text() == "✓ Aucune faute trouvée."


def test_engine_error_is_shown_in_window(qapp, make_window):
    cause = "LanguageTool injoignable (pas de connexion Internet ?)."
    w = make_window(grammalecte=dict(found=g_issues), languagetool=dict(found=lt_issues, error=cause))
    open_and_wait(qapp, w)
    assert not w.notice.isHidden()
    assert cause in w.notice.text()
    assert "Texte vérifié par Grammalecte seul." in w.notice.text()
    assert w._cards  # les fautes de Grammalecte restent affichées
    # Le moteur répond de nouveau : l'avertissement disparaît.
    w.checker.engine("languagetool").error = None
    w._start_check()
    assert wait_until(qapp, lambda: not w._running)
    assert w._statuses["languagetool"].ok
    assert w.notice.isHidden()


def test_all_engines_failed_lists_causes(qapp, make_window):
    w = make_window(grammalecte=dict(error="Grammalecte n'est pas installé."),
                    languagetool=dict(error="Limite de l'API publique LanguageTool atteinte, réessayez dans une minute."))
    open_and_wait(qapp, w)
    assert w.placeholder.text() == (
        "Aucun moteur n'a pu vérifier le texte.\n"
        "Grammalecte n'est pas installé.\n"
        "Limite de l'API publique LanguageTool atteinte, réessayez dans une minute."
    )
    assert not w.placeholder.isHidden()
    assert w.notice.isHidden()  # les causes sont déjà dans la liste


def test_list_text_before_first_check(qapp, make_window):
    w = make_window(grammalecte=dict(), languagetool=dict())
    w.open_with("", None)
    w.editor.insertPlainText("Bonjour.")
    assert w.placeholder.isHidden()  # texte pas encore vérifié : la liste n'affirme rien
    assert wait_until(qapp, lambda: w.placeholder.text() == "✓ Aucune faute trouvée.")


def test_no_active_engine(qapp, make_window):
    w = make_window()
    w.open_with("Bonjour.", None)
    assert wait_until(qapp, lambda: w.placeholder.text() == "Aucun moteur actif pour ce texte (Paramètres, onglet Moteurs).")
    assert w.notice.isHidden()


def test_next_card_is_scrolled_into_view_after_a_fix(qapp, make_window):
    """Après une correction, la liste est reconstruite puis la faute suivante est
    sélectionnée : sa carte doit être visible, pas la liste remontée en haut."""
    import re

    from PySide6.QtCore import QPoint

    def all_ecole(text):
        return [Issue(m.start(), m.end(), "Mot inconnu.", ["école", "écoles"], Category.SPELLING, "ORTHO", "grammalecte")
                for m in re.finditer(r"\becole\b", text)]

    w = make_window(grammalecte=dict(found=all_ecole))
    text = "\n".join(f"Ligne {n} : ecole" for n in range(25))
    w.open_with(text, None)
    w.resize(640, 540)
    assert wait_until(qapp, lambda: not w._running and len(w._cards) == 25)
    w.apply(w._issues[18], "école")
    assert wait_until(qapp, lambda: len(w._cards) == 24, 3)
    for _ in range(5):
        qapp.processEvents()
    selected = next(c for c in w._cards if c.uid == w._selected.uid)
    top = selected.mapTo(w.scroll.viewport(), QPoint(0, 0)).y()
    assert 0 <= top < w.scroll.viewport().height()
