"""Application cible pour les tests d'intégration : une zone de texte dont tout
le contenu est sélectionné. Écrit son contenu final sur la sortie standard."""

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QPlainTextEdit

app = QApplication(sys.argv[:1])
edit = QPlainTextEdit()
edit.setWindowTitle("Cible du correcteur")
edit.setPlainText(sys.argv[1])
edit.resize(400, 120)
edit.show()
edit.selectAll()
edit.activateWindow()
edit.raise_()
edit.setFocus()
print("PRET", flush=True)
duration = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
QTimer.singleShot(duration, app.quit)
app.exec()
print("FINAL:" + edit.toPlainText().replace("\n", "\\n"), flush=True)
