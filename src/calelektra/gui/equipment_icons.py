"""Pictograms of the equipment families, for the hardware setup diagrams.

ELEKTRA-like: simple line drawings in a single colour, drawn with QPainter on
a 48 × 48 grid (no image files to ship or license). ``glyph_for`` chooses the
pictogram of a category and, where it matters, of its type (antennas,
positioners, loads, adapters, enclosures); ``paint_icon`` draws it on its
tile: light grey with a thin border and the glyph in dark slate; a selected
node is a blue tile with a white glyph, as in ELEKTRA.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
)

GRID = 48.0
SLATE = QColor("#2c3e50")
MUTED = QColor("#8a94a3")
ACCENT = QColor("#2b7bb9")
TILE_TOP, TILE_BOTTOM, TILE_BORDER = QColor("#f6f8fb"), QColor("#dde4ec"), QColor("#b6c2d0")
SELECTED_TOP = QColor("#4a9ad6")


class _Draw:
    """Line drawing on the 48 × 48 grid of a glyph."""

    def __init__(self, painter: QPainter, color: QColor, width: float):
        self.p, self.color, self.width = painter, color, width
        self.pen()

    def pen(self, width: float | None = None, dashed: bool = False) -> None:
        pen = QPen(self.color, width if width is not None else self.width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if dashed:
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            pen.setDashPattern([1.6, 1.6])
        self.p.setPen(pen)
        self.p.setBrush(Qt.BrushStyle.NoBrush)

    def line(self, *points: tuple[float, float]) -> None:
        path = QPainterPath(QPointF(*points[0]))
        for point in points[1:]:
            path.lineTo(QPointF(*point))
        self.p.drawPath(path)

    def shape(self, *points: tuple[float, float]) -> None:
        path = QPainterPath(QPointF(*points[0]))
        for point in points[1:]:
            path.lineTo(QPointF(*point))
        path.closeSubpath()
        self.p.drawPath(path)

    def rect(self, x: float, y: float, w: float, h: float, radius: float = 0.0) -> None:
        if radius:
            self.p.drawRoundedRect(QRectF(x, y, w, h), radius, radius)
        else:
            self.p.drawRect(QRectF(x, y, w, h))

    def circle(self, x: float, y: float, r: float) -> None:
        self.p.drawEllipse(QPointF(x, y), r, r)

    def ellipse(self, x: float, y: float, w: float, h: float) -> None:
        self.p.drawEllipse(QRectF(x, y, w, h))

    def arc(self, x: float, y: float, w: float, h: float, start: float, span: float) -> None:
        """Arc of the ellipse in (x, y, w, h); angles in degrees, counter-clockwise from 3 o'clock."""
        self.p.drawArc(QRectF(x, y, w, h), round(start * 16), round(span * 16))

    def dot(self, x: float, y: float, r: float) -> None:
        self.p.save()
        self.p.setPen(Qt.PenStyle.NoPen)
        self.p.setBrush(self.color)
        self.p.drawEllipse(QPointF(x, y), r, r)
        self.p.restore()

    def path(self, path: QPainterPath) -> None:
        self.p.drawPath(path)

    def tube(self, path: QPainterPath, width: float) -> None:
        """Outline of a thick line (a cable)."""
        stroker = QPainterPathStroker()
        stroker.setWidth(width)
        stroker.setCapStyle(Qt.PenCapStyle.FlatCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self.p.drawPath(stroker.createStroke(path).simplified())

    def head(self, x: float, y: float, angle: float, size: float = 4.0) -> None:
        """Arrow head with its tip at (x, y), pointing to ``angle`` (degrees, y down)."""
        a = math.radians(angle)
        for side in (-1, 1):
            b = a + math.pi + side * math.radians(30)
            self.line((x + size * math.cos(b), y + size * math.sin(b)), (x, y))

    def zigzag(self, x0: float, x1: float, y: float, amplitude: float, teeth: int) -> None:
        step = (x1 - x0) / (2 * teeth)
        points = [(x0, y)]
        for k in range(2 * teeth - 1):
            points.append((x0 + step * (k + 1), y + (-amplitude if k % 2 == 0 else amplitude)))
        points.append((x1, y))
        self.line(*points)


Glyph = Callable[[_Draw], None]
GLYPHS: dict[str, Glyph] = {}
GLYPH_NAMES: dict[str, str] = {}


def _glyph(key: str, name: str) -> Callable[[Glyph], Glyph]:
    def decorator(function: Glyph) -> Glyph:
        GLYPHS[key] = function
        GLYPH_NAMES[key] = name
        return function

    return decorator


def _nut(d: _Draw, x: float, y: float, w: float = 6.0, h: float = 16.0) -> None:
    """Coupling nut of a coaxial connector, seen from the side (x, y: top left)."""
    d.rect(x, y, w, h, 1.0)
    d.line((x, y + h * 0.33), (x + w, y + h * 0.33))
    d.line((x, y + h * 0.67), (x + w, y + h * 0.67))


def _coax_end(d: _Draw, face: float, y: float, direction: int) -> float:
    """Connector at the end of a cable: nut at ``face``, body behind it; returns where the cable starts."""
    nut_w, body_w = 5.0, 5.0
    nut_x = face if direction < 0 else face - nut_w
    _nut(d, nut_x, y - 6, nut_w, 12)
    body_x = nut_x + nut_w if direction < 0 else nut_x - body_w
    d.rect(body_x, y - 3.5, body_w, 7)
    return body_x + body_w if direction < 0 else body_x


def _ground(d: _Draw, x: float, y: float) -> None:
    d.line((x - 5, y), (x + 5, y))
    d.line((x - 3, y + 2.6), (x + 3, y + 2.6))
    d.line((x - 1, y + 5.2), (x + 1, y + 5.2))


# — instruments and diagram nodes —


@_glyph("vna", "VNA")
def _vna(d: _Draw) -> None:
    d.rect(4, 10, 40, 27, 3)
    d.rect(8, 14, 18, 16, 1.5)
    trace = QPainterPath(QPointF(9.5, 18.5))
    trace.cubicTo(QPointF(14, 18.5), QPointF(15, 27), QPointF(17, 27))
    trace.cubicTo(QPointF(19, 27), QPointF(20, 18.5), QPointF(24.5, 18.5))
    d.path(trace)
    d.circle(35.5, 18.5, 3.2)
    d.circle(31.5, 30.5, 2.3)
    d.circle(39.5, 30.5, 2.3)
    d.line((9, 37), (9, 40))
    d.line((39, 37), (39, 40))


@_glyph("puerto", "Puerto del VNA")
def _port(d: _Draw) -> None:
    d.shape(*((24 + 14 * math.cos(math.radians(a)), 24 + 14 * math.sin(math.radians(a))) for a in range(0, 360, 60)))
    d.circle(24, 24, 8)
    d.dot(24, 24, 2.4)


@_glyph("plano", "Plano de referencia")
def _plane(d: _Draw) -> None:
    d.shape((19, 10), (29, 5), (29, 38), (19, 43))
    d.line((4, 24), (24, 24))
    d.pen(dashed=True)
    d.line((26.5, 24), (45, 24))
    d.pen()
    d.dot(24, 24, 2.6)


@_glyph("sensor_ambiental", "Sensor ambiental (TPH)")
def _ambient(d: _Draw) -> None:
    tube = QPainterPath(QPointF(13, 30.4))
    tube.lineTo(QPointF(13, 9))
    tube.arcTo(QRectF(13, 6, 6, 6), 180, -180)
    tube.lineTo(QPointF(19, 30.4))
    tube.arcTo(QRectF(10.3, 29.3, 11.4, 11.4), 57.7, -295.4)
    d.path(tube)
    d.line((16, 13), (16, 33))
    d.dot(16, 35, 2.8)
    for y in (12, 17, 22):
        d.line((22, y), (25, y))
    drop = QPainterPath(QPointF(35, 15))
    drop.cubicTo(QPointF(37.5, 20), QPointF(41, 23.5), QPointF(41, 28))
    drop.arcTo(QRectF(29, 22, 12, 12), 0, -180)
    drop.cubicTo(QPointF(29, 23.5), QPointF(32.5, 20), QPointF(35, 15))
    d.path(drop)


# — cables, adapters, loads —


@_glyph("cable", "Cable (EUT)")
def _cable(d: _Draw) -> None:
    a = _coax_end(d, 3, 33, -1)
    b = _coax_end(d, 45, 15, 1)
    path = QPainterPath(QPointF(a, 33))
    path.cubicTo(QPointF(a + 13, 33), QPointF(b - 13, 15), QPointF(b, 15))
    d.tube(path, 5)


@_glyph("cable_medida", "Cable de medida")
def _test_cable(d: _Draw) -> None:
    a = _coax_end(d, 3, 33, -1)
    b = _coax_end(d, 45, 15, 1)
    path = QPainterPath(QPointF(a, 33))
    path.cubicTo(QPointF(a + 13, 33), QPointF(b - 13, 15), QPointF(b, 15))
    d.tube(path, 6)
    d.pen(d.width * 0.7)
    for k in range(1, 8):  # armour
        t = k / 8
        point, angle = path.pointAtPercent(t), math.radians(-path.angleAtPercent(t) + 90)
        dx, dy = 3 * math.cos(angle), 3 * math.sin(angle)
        d.line((point.x() - dx, point.y() - dy), (point.x() + dx, point.y() + dy))
    d.pen()


@_glyph("adaptador", "Adaptador / transición")
def _adapter(d: _Draw) -> None:
    _nut(d, 5, 15, 7, 18)
    d.rect(12, 18.5, 13, 11)
    _nut(d, 25, 19, 5, 10)
    d.rect(30, 21, 8, 6)
    d.line((38, 24), (43, 24))


@_glyph("through", "Through (barril)")
def _through(d: _Draw) -> None:
    _nut(d, 5, 15, 7, 18)
    d.rect(12, 19, 24, 10)
    _nut(d, 36, 15, 7, 18)
    d.line((16.5, 24), (31.5, 24))
    d.head(16, 24, 180, 3.2)
    d.head(32, 24, 0, 3.2)


@_glyph("adaptador_lisn", "Adaptador de la LISN")
def _lisn_adapter(d: _Draw) -> None:
    _nut(d, 4, 16, 6, 16)
    d.rect(10, 19.5, 10, 9)
    d.rect(20, 7, 6, 34, 1)
    for y in (15, 33):
        d.line((26, y), (36, y))
        d.rect(36, y - 2.2, 6, 4.4, 2.2)


def _termination(d: _Draw) -> float:
    _nut(d, 4, 16, 6, 16)
    d.rect(10, 19, 9, 10)
    return 19.0


@_glyph("carga_50", "Carga 50 Ω")
def _load(d: _Draw) -> None:
    x = _termination(d)
    d.line((x, 24), (22, 24))
    d.zigzag(22, 37, 24, 4, 3)
    d.line((37, 24), (41, 24), (41, 30))
    _ground(d, 41, 30)


@_glyph("cortocircuito", "Cortocircuito")
def _short(d: _Draw) -> None:
    x = _termination(d)
    d.line((x, 24), (36, 24), (36, 30))
    _ground(d, 36, 30)


@_glyph("abierto", "Abierto")
def _open(d: _Draw) -> None:
    x = _termination(d)
    d.line((x, 24), (31, 24))
    d.circle(34, 24, 3)


@_glyph("kit", "Kit de calibración")
def _kit(d: _Draw) -> None:
    d.rect(4, 14, 40, 26, 3)
    d.line((18, 14), (18, 9), (30, 9), (30, 14))
    for x in (9.5, 20.5, 31.5):
        d.rect(x, 20, 7, 14, 2)
        d.line((x, 24.5), (x + 7, 24.5))


# — components (EUT) —


@_glyph("acoplador", "Acoplador direccional")
def _coupler(d: _Draw) -> None:
    d.rect(9, 9, 30, 26, 2)
    d.line((2, 16), (46, 16))  # main line: In → Out
    d.line((16, 35), (16, 43))  # Coupl. Fwd
    d.line((32, 35), (32, 43))  # Coupl. Rev
    # as in ELEKTRA: the forward wave goes to Fwd, the reflected one to Rev
    forward = QPainterPath(QPointF(30, 16))
    forward.cubicTo(QPointF(30, 25), QPointF(16, 23), QPointF(16, 31.5))
    reverse = QPainterPath(QPointF(18, 16))
    reverse.cubicTo(QPointF(18, 25), QPointF(32, 23), QPointF(32, 31.5))
    d.path(forward)
    d.path(reverse)
    d.head(16, 32, 90, 3)
    d.head(32, 32, 90, 3)


@_glyph("lisn", "LISN")
def _lisn(d: _Draw) -> None:
    d.rect(8, 11, 32, 27, 3)
    d.line((3, 21), (13, 21))
    for k in range(3):
        d.arc(13 + 6 * k, 18, 6, 6, 180, -180)
    d.line((31, 21), (45, 21))
    d.circle(24, 31, 2.8)
    d.dot(24, 31, 1.0)


@_glyph("path", "Path (camino compuesto)")
def _signal_path(d: _Draw) -> None:
    d.line((3, 24), (10, 24))
    d.rect(10, 17, 11, 14, 1.5)
    d.zigzag(11.5, 19.5, 24, 3, 2)
    d.line((21, 24), (27, 24))
    d.rect(27, 17, 11, 14, 1.5)
    d.shape((30, 20), (30, 28), (35.5, 24))
    d.line((38, 24), (45, 24))


@_glyph("preamplificador", "Preamplificador")
def _amplifier(d: _Draw) -> None:
    d.rect(7, 10, 34, 28, 3)
    d.shape((17, 15), (17, 33), (32, 24))
    d.line((2, 24), (17, 24))
    d.line((32, 24), (46, 24))
    d.dot(36.5, 33, 1.3)


@_glyph("generico", "Componente / equipo")
def _generic(d: _Draw) -> None:
    d.rect(14, 12, 20, 24, 2.5)
    for x in (4, 39):  # a coaxial connector on each side
        _nut(d, x, 18, 5, 12)
    d.rect(9, 20.5, 5, 7)
    d.rect(34, 20.5, 5, 7)


# — antennas —


@_glyph("log_periodica", "Antena log-periódica")
def _lpda(d: _Draw) -> None:
    d.line((6, 24), (43, 24))
    for x, half in ((10, 17), (15.5, 14.5), (21, 12), (26.5, 9.5), (31.5, 7.5), (36, 5.5), (40, 4)):
        d.line((x, 24 - half), (x, 24 + half))
    d.dot(5, 24, 1.8)


@_glyph("biconica", "Antena bicónica")
def _biconical(d: _Draw) -> None:
    d.shape((4, 12), (21, 22), (21, 26), (4, 36))
    d.shape((44, 12), (27, 22), (27, 26), (44, 36))
    d.line((4, 20), (21, 23))
    d.line((4, 28), (21, 25))
    d.line((44, 20), (27, 23))
    d.line((44, 28), (27, 25))
    d.line((24, 22), (24, 42))
    d.line((19, 42), (29, 42))


@_glyph("hibrida", "Antena híbrida (bilog)")
def _hybrid(d: _Draw) -> None:
    d.line((10, 24), (43, 24))
    d.shape((10, 22), (5, 5), (15, 5))
    d.shape((10, 26), (5, 43), (15, 43))
    for x, half in ((20, 11), (25.5, 9), (30.5, 7), (35, 5.5), (39.5, 4)):
        d.line((x, 24 - half), (x, 24 + half))


@_glyph("bocina", "Antena de bocina")
def _horn(d: _Draw) -> None:
    d.rect(4, 20, 5, 8, 1)
    d.rect(9, 17, 9, 14, 1)
    d.shape((18, 17), (44, 7), (44, 41), (18, 31))
    d.line((18, 22.5), (44, 21))
    d.line((18, 25.5), (44, 27))


@_glyph("omnidireccional_svswr", "Antena omnidireccional")
def _omni(d: _Draw) -> None:
    d.line((24, 6), (24, 21))
    d.line((24, 27), (24, 42))
    for r in (8, 14):
        d.arc(24 - r, 24 - r, 2 * r, 2 * r, -35, 70)
        d.arc(24 - r, 24 - r, 2 * r, 2 * r, 145, 70)


# — positioners and enclosures —


@_glyph("mastil", "Mástil")
def _mast(d: _Draw) -> None:
    d.rect(21, 5, 6, 34, 1)
    for y in (13, 21, 29):
        d.line((21, y), (27, y))
    d.line((14, 39), (34, 39))
    d.line((16, 39), (13, 44))
    d.line((32, 39), (35, 44))
    d.line((27, 16), (40, 16))
    d.rect(40, 12, 3, 8, 1)
    d.line((11, 9), (11, 30))
    d.head(11, 8.5, -90, 3.4)
    d.head(11, 30.5, 90, 3.4)


@_glyph("tripode", "Trípode")
def _tripod(d: _Draw) -> None:
    d.line((15, 8), (33, 8))
    d.line((24, 8), (24, 25))
    d.rect(20.5, 23, 7, 5, 1)
    d.line((21.5, 28), (11, 43))
    d.line((26.5, 28), (37, 43))
    d.line((24, 28), (24, 43))


@_glyph("mesa_giratoria", "Mesa giratoria")
def _turntable(d: _Draw) -> None:
    d.ellipse(5, 21, 38, 12)
    d.line((5, 27), (5, 32))
    d.line((43, 27), (43, 32))
    d.arc(5, 26, 38, 12, 180, 180)
    d.arc(9, 7, 30, 12, 160, -140)
    d.head(37.1, 10.9, 40, 3.6)


@_glyph("carril", "Carril")
def _rail(d: _Draw) -> None:
    d.line((4, 34), (44, 34))
    d.line((4, 39), (44, 39))
    for x in (9, 17, 25, 33, 41):
        d.line((x, 34), (x, 39))
    d.rect(16, 22, 16, 10, 2)
    d.line((11, 14), (37, 14))
    d.head(10.5, 14, 180, 3.4)
    d.head(37.5, 14, 0, 3.4)


def _absorbers(d: _Draw, start: tuple[float, float], end: tuple[float, float], depth: float, teeth: int) -> None:
    (x0, y0), (x1, y1) = start, end
    length = math.hypot(x1 - x0, y1 - y0)
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    nx, ny = -uy * depth, ux * depth  # into the room when going round it clockwise (on screen, y down)
    points = []
    for k in range(2 * teeth + 1):
        t = k / (2 * teeth)
        x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        points.append((x + nx, y + ny) if k % 2 else (x, y))
    d.line(*points)


@_glyph("sac", "Cámara semianecoica (SAC)")
def _sac(d: _Draw) -> None:
    d.line((5, 39), (5, 6), (43, 6), (43, 39))
    _absorbers(d, (5, 6), (43, 6), 5.5, 4)
    _absorbers(d, (43, 6), (43, 39), 5.5, 3)
    _absorbers(d, (5, 39), (5, 6), 5.5, 3)
    d.pen(d.width * 1.5)
    d.line((3, 39.5), (45, 39.5))  # ground plane
    d.pen()
    for x in range(7, 45, 6):
        d.line((x, 41.5), (x - 3, 45))


@_glyph("far", "Cámara anecoica (FAR)")
def _far(d: _Draw) -> None:
    d.rect(5, 6, 38, 36)
    _absorbers(d, (5, 6), (43, 6), 5.5, 4)
    _absorbers(d, (43, 6), (43, 42), 5.5, 3)
    _absorbers(d, (43, 42), (5, 42), 5.5, 4)
    _absorbers(d, (5, 42), (5, 6), 5.5, 3)


@_glyph("oats", "Emplazamiento abierto (OATS)")
def _oats(d: _Draw) -> None:
    d.shape((4, 40), (15, 29), (44, 29), (33, 40))
    d.circle(34, 13, 4)
    for k in range(8):
        a = math.radians(45 * k)
        d.line((34 + 6.5 * math.cos(a), 13 + 6.5 * math.sin(a)), (34 + 8.5 * math.cos(a), 13 + 8.5 * math.sin(a)))
    d.line((13, 33), (13, 15))
    d.line((9, 15), (17, 15))


# — choice of a glyph and painting —

_TYPED = {
    "antena": ({"biconica", "log_periodica", "hibrida", "bocina", "omnidireccional_svswr"}, "log_periodica"),
    "posicionador": ({"mastil", "tripode", "mesa_giratoria", "carril"}, "mastil"),
    "carga": ({"carga_50", "cortocircuito", "abierto"}, "carga_50"),
}
_ADAPTERS = {"coaxial": "adaptador", "through": "through", "lisn": "adaptador_lisn"}
_ENCLOSURES = {"SAC": "sac", "FAR": "far", "OATS": "oats"}
_CATEGORIES = {
    "vna": "vna",
    "cable": "cable",
    "cable_medida": "cable_medida",
    "kit_calibracion": "kit",
    "acoplador": "acoplador",
    "lisn": "lisn",
    "path": "path",
    "preamplificador": "preamplificador",
    "sensor_ambiental": "sensor_ambiental",
}


def glyph_for(category: str | None, kind: str | None = None) -> str:
    """Pictogram of a category of equipment and, where it changes the picture, of its type (``tipo``)."""
    if category in _TYPED:
        types, default = _TYPED[category]
        return kind if kind in types else default
    if category == "adaptador":
        return _ADAPTERS.get(kind or "", "adaptador")
    if category == "recinto":
        return _ENCLOSURES.get(kind or "", "sac")
    return _CATEGORIES.get(category or "", "generico")


def paint_glyph(painter: QPainter, rect: QRectF, glyph: str, color: QColor, width: float = 2.0) -> None:
    """The glyph alone, scaled into ``rect`` (``width``: line width on the 48 × 48 grid)."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    side = min(rect.width(), rect.height())
    painter.translate(rect.center().x() - side / 2, rect.center().y() - side / 2)
    painter.scale(side / GRID, side / GRID)
    GLYPHS.get(glyph, _generic)(_Draw(painter, color, width))
    painter.restore()


def paint_tile(
    painter: QPainter,
    rect: QRectF,
    selected: bool = False,
    border: QColor | None = None,
    dashed: bool = False,
    radius: float | None = None,
) -> QColor:
    """The tile of a pictogram (or the box of a coupler); returns the colour of the drawing that goes on it.

    ``border``: colour of the state of the role (warning, error…); ``dashed``: not assigned.
    """
    radius = rect.width() * 0.16 if radius is None else radius
    top, bottom = (SELECTED_TOP, ACCENT) if selected else (TILE_TOP, TILE_BOTTOM)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    gradient.setColorAt(0, top)
    gradient.setColorAt(1, bottom)
    edge = border if border is not None else (ACCENT.darker(115) if selected else TILE_BORDER)
    painter.setPen(QPen(edge, 2.0 if border is not None else 1.0, Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine))
    painter.setBrush(gradient)
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
    return QColor("#ffffff") if selected else SLATE


def paint_icon(
    painter: QPainter,
    rect: QRectF,
    glyph: str,
    selected: bool = False,
    muted: bool = False,
    border: QColor | None = None,
    dashed: bool = False,
) -> None:
    """Tile and glyph of a pictogram (``muted``: the role has no equipment yet)."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = paint_tile(painter, rect, selected, border, dashed)
    if muted and not selected:
        color = MUTED
    margin = rect.width() * 0.12
    paint_glyph(painter, rect.adjusted(margin, margin, -margin, -margin), glyph, color)
    painter.restore()


@lru_cache(maxsize=256)
def icon_pixmap(glyph: str, size: int = 32) -> QPixmap:
    """Cached pixmap of a pictogram with its tile (lists, menus)."""
    ratio = 2
    image = QPixmap(size * ratio, size * ratio)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.scale(ratio, ratio)
    paint_icon(painter, QRectF(0, 0, size, size), glyph)
    painter.end()
    image.setDevicePixelRatio(ratio)
    return image
