"""Icon of the application: a Smith chart with the marker of a measurement.

The drawing is that of the hardware setup pictograms (``equipment_icons.py``): a single-tone line drawing
on the 48 × 48 grid, on a tile. Two tiles:

* ``marino``: the navy of the rail, with the drawing in light grey: the icon of the application (its
  windows, the taskbar and the ``.ico`` of the shortcuts);
* ``claro``: the light tile of the hardware setups, with the drawing in slate: inside the application
  (the rail, «Acerca de»).

Each size has its own drawing: the chart with r = 1 and x = ±1 from 21 px, only its circle, axis and
marker below; and thicker lines below 64 px, so that they stay about 1.3 px wide.
``write_icon_files`` writes the ``.ico`` and the PNG files (``tools/icono_app.py``).
"""

from __future__ import annotations

import math
import struct
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from calelektra.gui.equipment_icons import GRID, _Draw, paint_tile

STYLES = ("marino", "claro")
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)  # the pixmaps of the icon
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
RESOURCES = Path(__file__).resolve().parent / "recursos"
NAVY_TOP, NAVY_BOTTOM, NAVY_BORDER = QColor("#1f3a5f"), QColor("#16263d"), QColor("#3a5a82")
LIGHT_GLYPH = QColor("#eef3f9")
MARGIN = 0.03  # around the tile, of the side of the icon


def level_of(size: int) -> int:
    """Detail of the drawing at ``size`` px: 0, the circle, the axis and the marker; 1, also r = 1 and x = ±1."""
    return 0 if size <= 20 else 1


def line_width(size: int) -> float:
    """Line width on the 48 grid: that of the setups (2) from 64 px; thicker below, about 1.3 px."""
    return 2.0 if size >= 64 else max(2.0, 1.3 * GRID / (size * 0.76))


def _polyline(d: _Draw, points: list[tuple[float, float]]) -> None:
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    d.path(path)


def draw_smith(d: _Draw, level: int) -> None:
    """The Smith chart of the icon on the 48 grid, with the marker of a measurement where the grid leaves room."""
    cx, cy, radius = 24.0, 24.0, 19.0

    def point(z: complex) -> tuple[float, float]:
        gamma = (z - 1) / (z + 1)
        return cx + radius * gamma.real, cy - radius * gamma.imag

    d.circle(cx, cy, radius)
    d.line((cx - radius, cy), (cx + radius, cy))
    if level >= 1:
        steps = [k * math.pi / 120 - math.pi / 2 + 1e-6 for k in range(121)]
        _polyline(d, [point(complex(1.0, math.tan(t))) for t in steps])  # r = 1
        for x in (1.0, -1.0):  # x = ±1
            _polyline(d, [point(complex(math.tan(k * (math.pi / 2 - 1e-3) / 80), x)) for k in range(81)])
    angle, magnitude = math.radians(215), 0.55
    mx, my = cx + radius * magnitude * math.cos(angle), cy - radius * magnitude * math.sin(angle)
    d.dot(mx, my, 2.6 if level >= 1 else 3.6)
    if level >= 1:
        d.pen(d.width * 0.9)
        d.circle(mx, my, 5.2)
        d.pen()


def _navy_tile(painter: QPainter, rect: QRectF) -> QColor:
    gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    gradient.setColorAt(0, NAVY_TOP)
    gradient.setColorAt(1, NAVY_BOTTOM)
    painter.setPen(QPen(NAVY_BORDER, max(1.0, rect.width() / 48)))
    painter.setBrush(gradient)
    radius = rect.width() * 0.16
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    return LIGHT_GLYPH


def paint_app_icon(painter: QPainter, rect: QRectF, style: str = "marino", size: int | None = None) -> None:
    """The icon in ``rect``: the tile and the chart, with the detail of ``size`` px (by default, the width of ``rect``)."""
    size = size or round(rect.width())
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = _navy_tile(painter, rect) if style == "marino" else paint_tile(painter, rect)  # light: as the setups
    margin = rect.width() * 0.12
    inner = rect.adjusted(margin, margin, -margin, -margin)
    painter.translate(inner.left(), inner.top())
    painter.scale(inner.width() / GRID, inner.width() / GRID)
    draw_smith(_Draw(painter, color, line_width(size)), level_of(size))
    painter.restore()


def app_image(size: int, style: str = "marino", ratio: float = 1.0) -> QImage:
    """The icon as an image of ``size`` logical px (``ratio``: device pixels per logical pixel)."""
    image = QImage(round(size * ratio), round(size * ratio), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    image.setDevicePixelRatio(ratio)
    painter = QPainter(image)
    pad = size * MARGIN
    paint_app_icon(painter, QRectF(pad, pad, size - 2 * pad, size - 2 * pad), style, size)
    painter.end()
    return image


def app_pixmap(size: int, style: str = "marino", ratio: float = 2.0) -> QPixmap:
    """Pixmap of the icon for a label (sharp on high-density screens)."""
    return QPixmap.fromImage(app_image(size, style, ratio))


@lru_cache(maxsize=4)
def app_icon(style: str = "marino") -> QIcon:
    """The icon with a drawing for each size (``marino``: the application; ``claro``: inside it)."""
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(QPixmap.fromImage(app_image(size, style)))
    return icon


def png_bytes(size: int, style: str = "marino") -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    app_image(size, style).save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def write_ico(path: Path, sizes: tuple[int, ...] = ICO_SIZES, style: str = "marino") -> Path:
    """A Windows icon with an image (PNG) for each size."""
    images = [png_bytes(size, style) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    for size, image in zip(sizes, images):
        side = 0 if size >= 256 else size  # 0 means 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(image), offset)
        offset += len(image)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + b"".join(images))
    return path


def write_icon_files(folder: Path = RESOURCES) -> list[Path]:
    """``cal-elektra.ico`` and the PNG of 16 to 256 px (navy), and the light one of 256 px."""
    written = [write_ico(folder / "cal-elektra.ico")]
    for size in (16, 32, 48, 64, 128, 256):
        path = folder / f"cal-elektra-{size}.png"
        path.write_bytes(png_bytes(size))
        written.append(path)
    path = folder / "cal-elektra-claro-256.png"
    path.write_bytes(png_bytes(256, "claro"))
    written.append(path)
    return written
