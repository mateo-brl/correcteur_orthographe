"""Fenêtre des paramètres."""

from __future__ import annotations

import sys
import threading

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from correcteur import __version__
from correcteur.config import Settings, config_dir, data_dir
from correcteur.platform import desktop_name, open_url, session_type
from correcteur.platform import keys as hotkeys

LANGUAGES = [
    ("fr", "Français"),
    ("auto", "Détection automatique"),
    ("en-US", "Anglais (États-Unis)"),
    ("en-GB", "Anglais (Royaume-Uni)"),
    ("es", "Espagnol"),
    ("de-DE", "Allemand"),
    ("it", "Italien"),
    ("pt-PT", "Portugais"),
]

LT_MODES = [
    ("public", "En ligne (gratuit, rien à installer, aucune charge pour le PC)"),
    ("local", "Sur ce PC (hors ligne, nécessite Java, ~400 Mo de RAM)"),
    ("perso", "Serveur LanguageTool personnel"),
    ("premium", "Compte LanguageTool Premium"),
]



class HotkeyEdit(QKeySequenceEdit):
    """Champ de raccourci où Tab et Maj+Tab passent au champ voisin au lieu d'être enregistrés : sans cela, on
    ne pouvait plus en sortir au clavier (un raccourci global sans Ctrl, Alt ou Win est de toute façon refusé).

    Une combinaison déjà réservée par une autre application n'arrive jamais jusqu'ici : Windows la lui donne. Le
    champ, vidé dès l'appui de Ctrl, restait vide, et « Enregistrer » gardait l'ancien raccourci sans rien dire.
    Ctrl, Alt, Maj et Win relâchées sans autre touche : l'ancien raccourci revient. Si une touche a été relâchée
    sans avoir été vue s'enfoncer (son appui a été pris ailleurs), `noKey` le signale."""

    noKey = Signal(str)  # modificateurs appuyés (« Ctrl+Alt »)
    _MODIFIER_KEYS = {Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Shift, Qt.Key.Key_Meta, Qt.Key.Key_AltGr}

    def __init__(self, sequence: QKeySequence) -> None:
        super().__init__(sequence)
        self.setMaximumSequenceLength(1)
        self._kept = sequence
        self._reset()

    def _reset(self) -> None:
        self._held = Qt.KeyboardModifier.NoModifier  # modificateurs vus pendant la saisie
        self._pressed: set[int] = set()              # autres touches vues s'enfoncer
        self._orphan = False                         # touche relâchée sans avoir été vue s'enfoncer

    def event(self, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.FocusIn:
            self._kept = self.keySequence()
            self._reset()
        elif kind == QEvent.Type.KeyPress and event.key() not in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self._held |= event.modifiers()
            if event.key() not in self._MODIFIER_KEYS:
                self._pressed.add(event.key())
        elif kind == QEvent.Type.KeyRelease:
            if event.key() not in self._MODIFIER_KEYS and event.key() not in self._pressed:
                self._orphan = True  # son appui a été pris par le raccourci d'une autre application
            handled = super().event(event)
            if not QGuiApplication.queryKeyboardModifiers():
                held, orphan = self._held, self._orphan
                self._reset()
                if self.keySequence().isEmpty() and held:
                    self.setKeySequence(self._kept)
                    names = [label for flag, label in ((Qt.KeyboardModifier.ControlModifier, "Ctrl"),
                                                       (Qt.KeyboardModifier.AltModifier, "Alt"),
                                                       (Qt.KeyboardModifier.ShiftModifier, "Maj"),
                                                       (Qt.KeyboardModifier.MetaModifier, "Win")) if held & flag]
                    if orphan:  # sans relâchement orphelin, l'utilisateur a seulement hésité : rien à dire
                        self.noKey.emit("+".join(names))
            return handled
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            free = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
            if not event.modifiers() & free:
                if self.keySequence().isEmpty():
                    # Maj, appuyée seule avant Tab, a lancé l'enregistrement et vidé le champ.
                    self.setKeySequence(self._kept)
                self.focusNextPrevChild(event.key() == Qt.Key.Key_Tab
                                        and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                return True
        return super().event(event)


class _Signals(QObject):
    progress = Signal(int, int)
    done = Signal(bool, str)


def run_install(parent: QWidget, label: str, func) -> bool:
    """Lance un téléchargement avec une barre de progression (sans figer l'interface)."""
    dialog = QProgressDialog(f"Téléchargement de {label}…", "Masquer", 0, 100, parent)
    dialog.setWindowTitle("Installation")
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumDuration(0)
    signals = _Signals()
    outcome = {"ok": False, "msg": ""}

    def on_progress(received: int, total: int) -> None:
        if total:
            dialog.setMaximum(100)
            dialog.setValue(min(99, received * 100 // total))
            dialog.setLabelText(f"Téléchargement de {label}… {received // 1_000_000} / {total // 1_000_000} Mo")
        else:
            dialog.setMaximum(0)

    def on_done(ok: bool, msg: str) -> None:
        outcome.update(ok=ok, msg=msg)
        dialog.setValue(dialog.maximum() or 100)
        dialog.close()

    signals.progress.connect(on_progress)
    signals.done.connect(on_done)

    def worker() -> None:
        try:
            func(lambda r, t: signals.progress.emit(r, t))
            signals.done.emit(True, "")
        except Exception as exc:
            signals.done.emit(False, str(exc))

    threading.Thread(target=worker, daemon=True).start()
    dialog.exec()
    while not outcome["ok"] and not outcome["msg"]:
        # Fenêtre masquée avant la fin : on attend quand même le résultat.
        from PySide6.QtCore import QCoreApplication, QEventLoop

        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.WaitForMoreEvents, 100)
    if outcome["ok"]:
        QMessageBox.information(parent, "Installation", f"{label} est installé.")
    else:
        QMessageBox.warning(parent, "Installation", f"Échec de l'installation de {label} :\n{outcome['msg']}")
    return outcome["ok"]


def _merged(opened: list[str], shown: list[str], current: list[str]) -> list[str]:
    """Liste à enregistrer : l'état actuel, moins ce que l'utilisateur a retiré dans la fenêtre (présent à
    l'ouverture, absent maintenant), plus ce qu'il y a ajouté."""
    removed = set(opened) - set(shown)
    kept = [item for item in current if item not in removed]
    return kept + [item for item in shown if item not in kept]


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, controller, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Paramètres du correcteur")
        self.setMinimumWidth(600)
        self.controller = controller
        self.settings = settings.copy()

        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), "Général")
        tabs.addTab(self._engines_tab(), "Moteurs")
        tabs.addTab(self._dictionary_tab(), "Dictionnaire")
        tabs.addTab(self._about_tab(), "Diagnostic")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Enregistrer")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annuler")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)
        self._refresh_engine_status()

    def _general_tab(self) -> QWidget:
        g = self.settings.general
        page = QWidget()
        form = QFormLayout(page)

        self.language = QComboBox()
        for code, name in LANGUAGES:
            self.language.addItem(name, code)
        idx = self.language.findData(g.language)
        self.language.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("Langue :", self.language)

        typo = QWidget()
        typo_layout = QVBoxLayout(typo)
        typo_layout.setContentsMargins(0, 0, 0, 0)
        self.typo_standard = QRadioButton("Standard (conseillé)")
        self.typo_strict = QRadioButton("Stricte")
        (self.typo_strict if g.typography == "stricte" else self.typo_standard).setChecked(True)
        typo_layout.addWidget(self.typo_standard)
        typo_layout.addWidget(self.typo_strict)
        typo_help = QLabel("Standard ignore les apostrophes courbes, espaces insécables, tirets longs, etc. "
                           "Stricte applique toute la typographie française (édition, documents officiels).")
        typo_help.setWordWrap(True)
        typo_help.setObjectName("muted")
        typo_layout.addWidget(typo_help)
        form.addRow("Typographie :", typo)

        self.abbrev = QCheckBox("Accepter les abréviations courantes (stp, svp, rdv, mdr…)")
        self.abbrev.setChecked(g.accept_abbreviations)
        form.addRow("", self.abbrev)
        self.live = QCheckBox("Revérifier pendant la frappe dans la fenêtre du correcteur")
        self.live.setChecked(g.live_check)
        form.addRow("", self.live)
        self.auto_correct = QCheckBox("Corriger automatiquement les fautes sûres à l'ouverture de la fenêtre")
        self.auto_correct.setToolTip("Les corrections douteuses restent proposées, à appliquer d'un clic.")
        self.auto_correct.setChecked(g.auto_correct)
        form.addRow("", self.auto_correct)
        self.autostart = QCheckBox("Lancer le correcteur à l'ouverture de session")
        self.autostart.setChecked(self.controller.autostart_enabled())
        form.addRow("", self.autostart)

        self.theme = QComboBox()
        for code, name in (("auto", "Comme le système"), ("clair", "Clair"), ("sombre", "Sombre")):
            self.theme.addItem(name, code)
        self.theme.setCurrentIndex(max(0, self.theme.findData(g.theme)))
        form.addRow("Thème :", self.theme)

        box = QGroupBox("Raccourcis clavier")
        box_form = QFormLayout(box)
        self.hotkeys_enabled = QCheckBox("Activer les raccourcis globaux")
        self.hotkeys_enabled.setChecked(g.hotkeys_enabled)
        box_form.addRow(self.hotkeys_enabled)
        self.hk_check = HotkeyEdit(QKeySequence(hotkeys.to_qt(g.hotkey_check)))
        self.hk_check_error = self._error_label()
        box_form.addRow("Vérifier la sélection :", self._with_error(self.hk_check, self.hk_check_error))
        self.hk_express = HotkeyEdit(QKeySequence(hotkeys.to_qt(g.hotkey_autocorrect)))
        self.hk_express_error = self._error_label()
        box_form.addRow("Correction express :", self._with_error(self.hk_express, self.hk_express_error))
        for edit, label in ((self.hk_check, self.hk_check_error), (self.hk_express, self.hk_express_error)):
            edit.noKey.connect(lambda mods, label=label: self._show_error(
                label, f"Aucune touche reçue après {mods} : cette combinaison est sans doute déjà prise par une "
                       "autre application. Essayez-en une autre."))
            edit.keySequenceChanged.connect(lambda _seq, label=label: label.hide())
        explain = QLabel(
            "<b>Vérifier la sélection</b> ouvre la fenêtre du correcteur sur le texte sélectionné.<br>"
            "<b>Correction express</b> corrige directement le texte sélectionné (seulement les corrections sûres)."
        )
        explain.setWordWrap(True)
        explain.setObjectName("muted")
        box_form.addRow(explain)
        if session_type() == "wayland":
            note = QLabel(
                "Sous Wayland, les applications ne peuvent pas écouter le clavier globalement : créez des "
                "raccourcis dans les paramètres du bureau avec les commandes <code>correcteur selection</code> "
                "et <code>correcteur express</code>."
            )
            note.setWordWrap(True)
            box_form.addRow(note)
            if "gnome" in desktop_name():
                gnome = QPushButton("Créer les raccourcis GNOME automatiquement")
                gnome.clicked.connect(self._install_gnome)
                box_form.addRow(gnome)
        form.addRow(box)
        return page

    def _error_label(self) -> QLabel:
        label = QLabel("")
        label.setWordWrap(True)
        theme = getattr(self.controller, "theme", None)
        if theme is not None:
            label.setStyleSheet(f"color: {theme.spelling};")
        label.hide()
        return label

    @staticmethod
    def _with_error(edit: QWidget, label: QLabel) -> QWidget:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(edit)
        column.addWidget(label)
        return box

    def _show_error(self, label: QLabel, text: str) -> None:
        label.setText(text)
        label.show()
        # Message sur deux lignes : la fenêtre grandit pour l'afficher en entier (sa 2e ligne était coupée).
        QTimer.singleShot(0, lambda: self._fit_label(label))

    def _fit_label(self, label: QLabel) -> None:
        if not label.isVisible():
            return
        need = label.heightForWidth(label.width())
        if need > label.height():
            label.setMinimumHeight(need)
            self.layout().activate()
            height = self.sizeHint().height()
            if height > self.height():
                self.resize(self.width(), height)

    def _engines_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        gbox = QGroupBox("Grammalecte (français, hors ligne, très léger)")
        g_layout = QVBoxLayout(gbox)
        self.g_enabled = QCheckBox("Activer Grammalecte")
        self.g_enabled.setChecked(self.settings.grammalecte.enabled)
        g_layout.addWidget(self.g_enabled)
        row = QHBoxLayout()
        self.g_status = QLabel("")
        row.addWidget(self.g_status, 1)
        self.g_install = QPushButton("Installer (6 Mo)")
        self.g_install.clicked.connect(self._install_grammalecte)
        row.addWidget(self.g_install)
        g_layout.addLayout(row)
        layout.addWidget(gbox)

        lt = self.settings.languagetool
        lbox = QGroupBox("LanguageTool (règles de grammaire et de style, sans IA)")
        l_layout = QVBoxLayout(lbox)
        self.lt_enabled = QCheckBox("Activer LanguageTool")
        self.lt_enabled.setChecked(lt.enabled)
        l_layout.addWidget(self.lt_enabled)
        self.lt_mode = QButtonGroup(self)
        self._lt_radios: dict[str, QRadioButton] = {}
        for code, label in LT_MODES:
            radio = QRadioButton(label)
            self.lt_mode.addButton(radio)
            self._lt_radios[code] = radio
            l_layout.addWidget(radio)
        self._lt_radios.get(lt.mode, self._lt_radios["public"]).setChecked(True)
        public_note = QLabel("En ligne : le texte vérifié est envoyé aux serveurs de LanguageTool (pas d'IA, "
                             "pas de compte). Choisissez « Sur ce PC » pour que rien ne sorte de l'ordinateur.")
        public_note.setWordWrap(True)
        public_note.setObjectName("muted")
        l_layout.addWidget(public_note)

        form = QFormLayout()
        local_row = QHBoxLayout()
        self.lt_status = QLabel("")
        local_row.addWidget(self.lt_status, 1)
        self.lt_install = QPushButton("Installer (250 Mo)")
        self.lt_install.clicked.connect(self._install_languagetool)
        local_row.addWidget(self.lt_install)
        form.addRow("Mode local :", local_row)
        self.lt_ram = QSpinBox()
        self.lt_ram.setRange(256, 4096)
        self.lt_ram.setSingleStep(128)
        self.lt_ram.setSuffix(" Mo")
        self.lt_ram.setValue(lt.local_max_ram_mb)
        form.addRow("Mémoire max (local) :", self.lt_ram)
        self.lt_url = QLineEdit(lt.url)
        form.addRow("Adresse du serveur perso :", self.lt_url)
        self.lt_user = QLineEdit(lt.username)
        form.addRow("Identifiant Premium :", self.lt_user)
        self.lt_key = QLineEdit(lt.api_key)
        self.lt_key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Clé API Premium :", self.lt_key)
        self.lt_picky = QCheckBox("Mode exigeant (plus de règles de style et de grammaire)")
        self.lt_picky.setChecked(lt.picky)
        form.addRow("", self.lt_picky)
        l_layout.addLayout(form)
        layout.addWidget(lbox)
        layout.addStretch(1)
        return page

    def _dictionary_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Mots acceptés (noms propres, jargon…) :"))
        self.words = QListWidget()
        dictionary = self.controller.dictionary
        self._opened_words = list(dictionary.words() if dictionary else [])
        self.words.addItems(self._opened_words)
        layout.addWidget(self.words, 2)
        row = QHBoxLayout()
        self.new_word = QLineEdit()
        self.new_word.setPlaceholderText("Nouveau mot")
        self.new_word.returnPressed.connect(self._add_word)
        row.addWidget(self.new_word, 1)
        add = QPushButton("Ajouter")
        add.clicked.connect(self._add_word)
        row.addWidget(add)
        remove = QPushButton("Supprimer")
        remove.clicked.connect(lambda: [self.words.takeItem(self.words.row(i)) for i in self.words.selectedItems()])
        row.addWidget(remove)
        layout.addLayout(row)

        layout.addWidget(QLabel("Règles désactivées :"))
        self.rules = QListWidget()
        for label, rules in self.settings.ignored_rule_groups():
            # Une ligne par faute coupée, toutes ses règles réactivées ensemble ; les identifiants en infobulle.
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, rules)
            item.setToolTip("\n".join(rules))
            self.rules.addItem(item)
        layout.addWidget(self.rules, 1)
        restore = QPushButton("Réactiver la règle sélectionnée")
        restore.clicked.connect(lambda: [self.rules.takeItem(self.rules.row(i)) for i in self.rules.selectedItems()])
        layout.addWidget(restore)
        return page

    @staticmethod
    def _items(widget: QListWidget) -> list[str]:
        return [widget.item(i).text() for i in range(widget.count())]

    def _about_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        lines = [f"<b>Correcteur {__version__}</b>", f"Python {sys.version.split()[0]}"]
        lines += self.controller.bridge.diagnostics()
        lines += [f"Paramètres : {config_dir()}", f"Données : {data_dir()}"]
        label = QLabel("<br>".join(lines))
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setWordWrap(True)
        layout.addWidget(label)
        open_dir = QPushButton("Ouvrir le dossier des paramètres")
        open_dir.clicked.connect(self._open_config_dir)
        layout.addWidget(open_dir)
        layout.addStretch(1)
        return page

    def _open_config_dir(self) -> None:
        if not open_url(config_dir().as_uri()):
            QMessageBox.information(self, "Dossier des paramètres", f"Ouverture impossible. Dossier : {config_dir()}")

    def _refresh_engine_status(self) -> None:
        from correcteur.engines.grammalecte_engine import import_grammalecte
        from correcteur.engines.lt_server import _find_server_jar
        from correcteur.installer import find_java

        has_g = import_grammalecte() is not None
        self.g_status.setText("✓ Installé" if has_g else "Non installé")
        self.g_install.setVisible(not has_g)
        jar = _find_server_jar() is not None
        java = find_java() is not None
        status = "✓ Installé" if jar else "Non installé"
        status += " · Java " + ("✓" if java else "introuvable (Java 17+ requis)")
        self.lt_status.setText(status)
        self.lt_install.setVisible(not jar)

    def _install_grammalecte(self) -> None:
        from correcteur.installer import install_grammalecte

        run_install(self, "Grammalecte", install_grammalecte)
        self._refresh_engine_status()

    def _install_languagetool(self) -> None:
        from correcteur.installer import install_languagetool

        run_install(self, "LanguageTool", install_languagetool)
        self._refresh_engine_status()

    def _install_gnome(self) -> None:
        from correcteur.platform.autostart import install_gnome_shortcuts

        try:
            check = hotkeys.from_qt(self.hk_check.keySequence().toString(QKeySequence.SequenceFormat.PortableText))
            express = hotkeys.from_qt(self.hk_express.keySequence().toString(QKeySequence.SequenceFormat.PortableText))
            QMessageBox.information(self, "Raccourcis", install_gnome_shortcuts(check, express))
        except Exception as exc:
            QMessageBox.warning(self, "Raccourcis", f"Impossible de créer les raccourcis : {exc}")

    def _add_word(self) -> None:
        word = self.new_word.text().strip()
        if word and not self.words.findItems(word, Qt.MatchFlag.MatchExactly):
            self.words.addItem(word)
        self.new_word.clear()

    def _hotkey(self, edit: QKeySequenceEdit, fallback: str) -> str:
        seq = edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        if not seq:
            return fallback
        return hotkeys.from_qt(seq)

    def _hotkeys_free(self, wanted) -> bool:
        """Les nouveaux raccourcis sont-ils libres ? Sinon, l'erreur s'affiche sous le champ et la fenêtre reste
        ouverte (avant, seule une bulle le disait, après la fermeture). Ceux que le correcteur tient déjà passent."""
        current = self.controller.settings.general
        mine = {current.hotkey_check, current.hotkey_autocorrect}
        free = getattr(getattr(self.controller, "bridge", None), "hotkey_free", None)
        for combo, edit, label in wanted:
            if combo in mine or free is None:
                continue
            if free(combo) is False:
                self._show_error(label, f"{hotkeys.display(combo)} est déjà pris par une autre application : "
                                        "choisissez-en un autre.")
                edit.setFocus()
                return False
        return True

    def _accept(self) -> None:
        s = self.settings
        g = s.general
        try:
            check = self._hotkey(self.hk_check, g.hotkey_check)
            express = self._hotkey(self.hk_express, g.hotkey_autocorrect)
        except ValueError as exc:
            QMessageBox.warning(self, "Raccourci invalide", str(exc))
            return
        if check == express:
            QMessageBox.warning(self, "Raccourcis", "Les deux raccourcis doivent être différents.")
            return
        if self.hotkeys_enabled.isChecked() and not self._hotkeys_free(((check, self.hk_check, self.hk_check_error),
                                                                        (express, self.hk_express, self.hk_express_error))):
            return
        g.language = self.language.currentData()
        g.typography = "stricte" if self.typo_strict.isChecked() else "standard"
        g.accept_abbreviations = self.abbrev.isChecked()
        g.live_check = self.live.isChecked()
        g.auto_correct = self.auto_correct.isChecked()
        g.theme = self.theme.currentData()
        g.hotkeys_enabled = self.hotkeys_enabled.isChecked()
        g.hotkey_check, g.hotkey_autocorrect = check, express
        g.autostart = self.autostart.isChecked()

        s.grammalecte.enabled = self.g_enabled.isChecked()
        lt = s.languagetool
        lt.enabled = self.lt_enabled.isChecked()
        lt.mode = next(code for code, radio in self._lt_radios.items() if radio.isChecked())
        lt.local_max_ram_mb = self.lt_ram.value()
        lt.url = self.lt_url.text().strip() or lt.url
        lt.username = self.lt_user.text().strip()
        lt.api_key = self.lt_key.text().strip()
        lt.picky = self.lt_picky.isChecked()
        # Règles coupées et mots ajoutés depuis la fenêtre de correction pendant que celle-ci était ouverte :
        # gardés. Seuls disparaissent ceux que l'utilisateur a retirés ici.
        rules = [rule for i in range(self.rules.count()) for rule in self.rules.item(i).data(Qt.ItemDataRole.UserRole)]
        s.ignored_rules = _merged(self.settings.ignored_rules, rules, self.controller.settings.ignored_rules)
        labels = {**self.settings.rule_labels, **self.controller.settings.rule_labels}
        s.rule_labels = {r: labels[r] for r in s.ignored_rules if r in labels}
        dictionary = self.controller.dictionary
        words = _merged(self._opened_words, self._items(self.words), dictionary.words() if dictionary else [])
        self.controller.apply_settings(s, words)
        self.accept()
