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
from PySide6.QtCore import Qt  # noqa: E402


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
    """make_window(grammalecte={...}, languagetool={...}) : arguments de FakeEngine pour chaque moteur.
    La correction automatique à l'ouverture est coupée, sauf avec auto_correct=True."""
    from correcteur.ui.popup import CorrectionWindow
    from correcteur.ui.theme import LIGHT

    made = []

    def make(auto_correct=False, **engines):
        settings = Settings()
        settings.general.auto_correct = auto_correct
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
    """Texte inchangé avec des corrections sûres : « Corriger et copier » les applique d'abord."""
    open_and_wait(qapp, window)
    assert window.replace_btn.text() == "Corriger et copier"
    finished = []
    window.finished.connect(lambda cap, replaced: finished.append(replaced))
    window.replace_text()
    assert wait_until(qapp, lambda: finished == [False])
    assert window.controller.copied[-1] == "Les résultats que j'ai obtenus sont bons et école ferme, fesait-il."
    assert not window.isVisible()


def test_replace_with_capture_emits_request(qapp, window):
    from correcteur.platform.bridge import Capture

    capture = Capture("Ces un test.", target=123, from_selection=True)
    open_and_wait(qapp, window, capture.text, capture)
    requested = []
    window.replaceRequested.connect(lambda cap, text: requested.append((cap, text)))
    window.editor.setPlainText("C'est un test.")  # corrigé à la main
    window.replace_text()
    assert requested == [(capture, "C'est un test.")]


def test_unchanged_text_is_never_pasted_back(qapp, window):
    """Rien de corrigé et rien de sûr à corriger : « Remplacer » ne recolle pas le texte d'origine (l'utilisateur
    croyait que le remplacement ne marchait pas) ; il est désactivé et la fenêtre dit pourquoi."""
    from correcteur.platform.bridge import Capture

    capture = Capture("Ces un test.", target=123, from_selection=True)
    open_and_wait(qapp, window, capture.text, capture)
    requested = []
    window.replaceRequested.connect(lambda cap, text: requested.append((cap, text)))
    assert not window.replace_btn.isEnabled()
    window.replace_text()  # Ctrl+Entrée
    assert requested == [] and window.isVisible()
    assert "Aucune correction" in window.auto_note.text()


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
        "Ctrl+Z dans l'application pour annuler.",
        "1 remarque à vérifier (Ctrl+Alt+C).",
        "LanguageTool a mis plus de 8 s à répondre.",
    ]
    # Une espace en trop n'a rien de lisible à montrer : seulement le nombre.
    assert express_message("le  mail", "le mail", 1, 0, {}, "X") == (
        "1 correction appliquée.\nCtrl+Z dans l'application pour annuler.", False)
    # Aucun moteur n'a vérifié le texte : ce n'est pas « aucune faute ».
    assert express_message("ok", "ok", 0, 0, {}, "X") == (
        "Vérification impossible : aucun moteur actif n'a vérifié le texte.", True)
    offline = {"grammalecte": EngineStatus("grammalecte", False, "non installé.")}
    assert express_message("ok", "ok", 0, 0, offline, "X") == ("Vérification impossible : Grammalecte : non installé.", True)
    both = {"grammalecte": EngineStatus("grammalecte", True, ""), "languagetool": EngineStatus("languagetool", True, "")}
    assert express_message("ok", "ok", 0, 0, both, "X") == ("Aucune faute trouvée ✓", False)


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


# Correction automatique à l'ouverture (réglage « Corriger automatiquement », actif par défaut).
FIXED = "Les résultats que j'ai obtenus sont bons et école ferme, fesait-il."


def auto_window(make_window, lt_delay=0.05):
    return make_window(auto_correct=True, grammalecte=dict(found=g_issues),
                       languagetool=dict(found=lt_issues, delay=lt_delay))


def test_sure_corrections_are_applied_when_the_window_opens(qapp, make_window):
    window = auto_window(make_window)
    window.open_with(TEXT)
    assert wait_until(qapp, lambda: window.editor.plain_text() == FIXED)
    assert "automatiquement" in window.auto_note.text() and window.auto_note.isVisible()
    marked = {FIXED[a:b] for a, b in window.editor.marked_ranges()}
    assert marked and all(m in ("s", "bons", "é", ",") or m in FIXED for m in marked)  # passages surlignés
    remaining = {window._text[i.start:i.end] for i in window._issues}
    assert "fesait" in remaining  # douteuse : reste une suggestion
    window.editor.undo()  # un seul Ctrl+Z annule la correction automatique
    assert window.editor.plain_text() == TEXT
    assert window.editor.marked_ranges() == []


def test_without_the_setting_the_text_is_left_alone(qapp, window):
    open_and_wait(qapp, window)
    qapp.processEvents()
    assert window.editor.plain_text() == TEXT and not window.auto_note.isVisible()


def test_replace_before_the_checks_end_waits_then_pastes_the_corrected_text(qapp, make_window):
    from correcteur.platform.bridge import Capture

    window = auto_window(make_window, lt_delay=0.6)
    capture = Capture(TEXT, target=1, from_selection=True)
    requested = []
    window.replaceRequested.connect(lambda cap, text: requested.append(text))
    window.open_with(TEXT, capture)
    window.replace_text()  # Entrée tout de suite, LanguageTool n'a pas répondu
    assert requested == [] and window.replace_btn.text() == "Vérification…"
    assert wait_until(qapp, lambda: requested == [FIXED])


def test_typing_before_the_checks_end_cancels_the_automatic_correction(qapp, make_window):
    window = auto_window(make_window, lt_delay=0.4)
    window.open_with(TEXT)
    cursor = window.editor.textCursor()
    cursor.insertText(" Merci.")
    assert wait_until(qapp, lambda: not window._running and window._ready)
    assert window.editor.plain_text() == TEXT + " Merci."


def test_a_suggestion_applied_twice_is_applied_once(qapp, window):
    """Alt+1 répété (ou double-clic) sur « enfant » -> « enfants » donnait « enfantss »."""
    open_and_wait(qapp, window)
    obtenu = next(i for i in window._issues if window._text[i.start:i.end] == "obtenu")
    window.apply_uid(obtenu.uid, "obtenus")
    window.apply_uid(obtenu.uid, "obtenus")
    assert "obtenus sont" in window.editor.plain_text() and "obtenuss" not in window.editor.plain_text()
    assert all(i.uid != obtenu.uid for i in window._issues)


def test_non_breaking_spaces_survive_a_replacement(qapp, window):
    """toPlainText() changeait les espaces insécables en espaces ordinaires dans le texte rendu."""
    from correcteur.platform.bridge import Capture

    text = "Il fesait beau\u00a0: on est sorti\u00a0! «\u00a0Super\u00a0»"
    capture = Capture(text, target=1, from_selection=True)
    open_and_wait(qapp, window, text, capture)
    requested = []
    window.replaceRequested.connect(lambda cap, new: requested.append(new))
    window.editor.setPlainText(text.replace("fesait", "faisait"))
    window.replace_text()
    assert requested and requested[0].count("\u00a0") == 4


def test_local_engine_underlines_without_waiting_for_languagetool(qapp, make_window):
    window = make_window(grammalecte=dict(found=g_issues), languagetool=dict(found=lt_issues, delay=3))
    window.open_with(TEXT)
    start = time.monotonic()
    assert wait_until(qapp, lambda: any("grammalecte" in i.sources for i in window._issues), 2)
    assert time.monotonic() - start < 1.5


def test_reopening_forgets_a_check_still_running(qapp, make_window):
    """Fenêtre fermée avant la réponse de LanguageTool, puis rouverte : l'indicateur ne tourne pas sans fin."""
    window = make_window(grammalecte=dict(found=g_issues), languagetool=dict(found=lt_issues, delay=0.5))
    window.open_with(TEXT)
    window.close()
    window.open_with("Texte correct.")
    assert wait_until(qapp, lambda: not window._running, 3)


def test_settings_save_keeps_rules_and_words_added_meanwhile(qapp, home):
    """« Enregistrer » réécrivait la copie faite à l'ouverture : une règle coupée ou un mot ajouté depuis la
    fenêtre de correction pendant que les Paramètres étaient ouverts disparaissait."""
    from correcteur.ui.settings import SettingsDialog

    settings = Settings()
    settings.ignored_rules = ["ANCIENNE", "A_REACTIVER"]
    dictionary = PersonalDictionary()
    dictionary.add("bidule")
    controller = FakeController(settings, dictionary)
    dialog = SettingsDialog(settings, controller)
    # Pendant ce temps, depuis la fenêtre de correction :
    controller.settings.ignored_rules.append("NOUVELLE")
    dictionary.add("machin")
    # Dans les Paramètres, l'utilisateur réactive une règle.
    item = dialog.rules.findItems("A_REACTIVER", Qt.MatchFlag.MatchExactly)[0]
    dialog.rules.takeItem(dialog.rules.row(item))
    dialog._accept()
    assert controller.settings.ignored_rules == ["ANCIENNE", "NOUVELLE"]
    assert sorted(controller.applied_words) == ["bidule", "machin"]


def test_closing_while_a_replacement_waits_pastes_nothing_later(qapp, make_window):
    """Entrée puis Échap avant la fin des vérifications : rien ne doit être collé après coup, et la fenêtre
    rouverte n'est ni figée ni en attente."""
    from correcteur.platform.bridge import Capture

    window = auto_window(make_window, lt_delay=0.4)
    capture = Capture(TEXT, target=1, from_selection=True)
    requested = []
    window.replaceRequested.connect(lambda cap, text: requested.append(text))
    window.open_with(TEXT, capture)
    window.replace_text()
    assert window.editor.isReadOnly()  # figé : une frappe annulerait la correction attendue
    window.close()
    assert not window.editor.isReadOnly()
    time.sleep(0.6)
    qapp.processEvents()
    assert requested == []
    window.open_with("Texte correct.")
    assert wait_until(qapp, lambda: window._ready)
    assert not window._replace_pending and not window.editor.isReadOnly()
    assert requested == []


def test_without_live_check_typing_leaves_the_window_ready(qapp, make_window):
    """Revérification coupée : après une frappe, « Remplacer » n'attend pas une vérification qui ne vient pas."""
    from correcteur.platform.bridge import Capture

    window = make_window(grammalecte=dict(found=g_issues), languagetool=dict(found=lt_issues, delay=0.4))
    window.controller.settings.general.live_check = False
    capture = Capture(TEXT, target=1, from_selection=True)
    requested = []
    window.replaceRequested.connect(lambda cap, text: requested.append(text))
    window.open_with(TEXT, capture)
    window.editor.textCursor().insertText(" Merci.")
    assert window._ready and window.replace_btn.text() == "Remplacer"
    window.replace_text()
    assert requested == [TEXT + " Merci."]


def test_without_auto_correct_the_edited_text_is_pasted_as_is(qapp, window):
    """Correction automatique décochée : le texte retouché à la main est collé tel quel, sans corrections
    ajoutées en douce."""
    from correcteur.platform.bridge import Capture

    capture = Capture(TEXT, target=1, from_selection=True)
    open_and_wait(qapp, window, TEXT, capture)
    requested = []
    window.replaceRequested.connect(lambda cap, text: requested.append(text))
    edited = TEXT.replace("fesait", "faisait")
    window.editor.setPlainText(edited)
    assert wait_until(qapp, lambda: window._ready and not window._running)
    window.replace_text()
    assert requested == [edited]
