"""Génère packaging/icone.ico et icone.png à partir de l'icône SVG (via Qt).

Le fichier .ico est assemblé à la main (images PNG en plusieurs tailles, format
accepté par Windows depuis Vista) : pas de dépendance au greffon ICO de Qt."""

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(ROOT / "src" / "correcteur" / "resources" / "icone.svg")).render(painter)
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("encodage PNG impossible")
    buffer.close()
    return bytes(data)


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    directory, payload = b"", b""
    for size, png in images:
        side = 0 if size >= 256 else size  # 0 signifie 256 dans le format ICO
        directory += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset + len(payload))
        payload += png
    return header + directory + payload


def main() -> int:
    app = QGuiApplication(sys.argv[:1])  # noqa: F841
    out = ROOT / "packaging"
    (out / "icone.png").write_bytes(png_bytes(render(256)))
    (out / "icone.ico").write_bytes(build_ico([(size, png_bytes(render(size))) for size in ICO_SIZES]))
    print(f"icônes générées dans {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
