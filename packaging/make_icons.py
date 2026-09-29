"""Génère packaging/icone.ico et icone.png à partir de l'icône SVG (via Qt)."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    QSvgRenderer(str(ROOT / "src" / "correcteur" / "resources" / "icone.svg")).render(painter)
    painter.end()
    return image


def main() -> int:
    app = QGuiApplication(sys.argv[:1])  # noqa: F841
    out = ROOT / "packaging"
    ok = render(256).save(str(out / "icone.png"), "PNG")
    ok = render(256).save(str(out / "icone.ico"), "ICO") and ok
    print("icônes générées" if ok else "échec de la génération des icônes")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
