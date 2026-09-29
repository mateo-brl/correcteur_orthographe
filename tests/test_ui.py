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
def window(qapp, home):
    from correcteur.ui.popup import CorrectionWindow
    from correcteur.ui.theme import LIGHT

    settings = Settings()
    engines = [FakeEngine(settings, "grammalecte", g_issues), FakeEngine(settings, "languagetool", lt_issues, delay=0.05)]
    dictionary = PersonalDictionary()
    checker = Checker(settings, dictionary, engines=engines)
    w = CorrectionWindow(checker, FakeController(settings, dictionary), LIGHT)
    yield w
    w.runner.shutdown()
    w.close()
    checker.close()


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
