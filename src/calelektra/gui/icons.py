"""Vector icons drawn with QPainter (no image files to ship or license).

``icon("equipos", "#ffffff")`` returns a cached QIcon. Every icon is drawn on
a 24×24 grid with round caps, in the style of common line-icon sets.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

Painter = Callable[[QPainter, QColor], None]
_ICONS: dict[str, Painter] = {}


def _register(*names: str) -> Callable[[Painter], Painter]:
    def decorator(function: Painter) -> Painter:
        for name in names:
            _ICONS[name] = function
        return function

    return decorator


def _pen(painter: QPainter, color: QColor, width: float = 1.8) -> None:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)


def _poly(painter: QPainter, *points: tuple[float, float], close: bool = False) -> None:
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    if close:
        path.closeSubpath()
    painter.drawPath(path)


@_register("inicio")
def _home(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    _poly(p, (3, 11), (12, 3.5), (21, 11))
    _poly(p, (5.5, 9.5), (5.5, 20.5), (18.5, 20.5), (18.5, 9.5))
    _poly(p, (10, 20.5), (10, 14.5), (14, 14.5), (14, 20.5))


@_register("ensayos", "ejecutar")
def _play(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawEllipse(QRectF(3, 3, 18, 18))
    p.setBrush(c)
    _poly(p, (10, 8), (16.5, 12), (10, 16), close=True)


@_register("detener")
def _stop(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawEllipse(QRectF(3, 3, 18, 18))
    p.setBrush(c)
    p.drawRoundedRect(QRectF(8.5, 8.5, 7, 7), 1, 1)


@_register("procedimientos")
def _clipboard(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawRoundedRect(QRectF(5, 4.5, 14, 17), 2, 2)
    p.drawRoundedRect(QRectF(9, 2.5, 6, 4), 1, 1)
    for y in (11, 14.5, 18):
        _poly(p, (8.5, y), (9.5, y))
        _poly(p, (11.5, y), (16, y))


@_register("setups")
def _diagram(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawRoundedRect(QRectF(2.5, 4, 7, 5.5), 1.2, 1.2)
    p.drawRoundedRect(QRectF(14.5, 4, 7, 5.5), 1.2, 1.2)
    p.drawRoundedRect(QRectF(8.5, 15, 7, 5.5), 1.2, 1.2)
    _poly(p, (9.5, 6.75), (14.5, 6.75))
    _poly(p, (6, 9.5), (6, 12.2), (12, 12.2), (12, 15))
    _poly(p, (18, 9.5), (18, 12.2), (12, 12.2))


@_register("equipos", "vna")
def _instrument(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawRoundedRect(QRectF(2.5, 5, 19, 14), 2, 2)
    p.drawRect(QRectF(5, 7.5, 9, 7))
    path = QPainterPath(QPointF(5.5, 12.5))
    for k in range(1, 9):
        x = 5.5 + k
        path.lineTo(QPointF(x, 11 - 2 * math.sin(k * 0.9)))
    p.drawPath(path)
    p.drawEllipse(QRectF(16, 8, 3, 3))
    p.drawEllipse(QRectF(16, 13, 3, 3))
    _poly(p, (6, 19), (6, 21))
    _poly(p, (18, 19), (18, 21))


@_register("catalogo")
def _books(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawRoundedRect(QRectF(3.5, 4, 5, 16.5), 1, 1)
    p.drawRoundedRect(QRectF(9.5, 4, 5, 16.5), 1, 1)
    path = QPainterPath(QPointF(15.5, 5.5))
    path.lineTo(QPointF(19.5, 4.5))
    path.lineTo(QPointF(21.5, 19.5))
    path.lineTo(QPointF(17.5, 20.5))
    path.closeSubpath()
    p.drawPath(path)
    _poly(p, (3.5, 8), (8.5, 8))
    _poly(p, (9.5, 8), (14.5, 8))


@_register("nuevo")
def _plus(p: QPainter, c: QColor) -> None:
    _pen(p, c, 2.2)
    _poly(p, (12, 5), (12, 19))
    _poly(p, (5, 12), (19, 12))


@_register("editar")
def _pencil(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    _poly(p, (4, 20), (5, 15.5), (16, 4.5), (19.5, 8), (8.5, 19), (4, 20), close=True)
    _poly(p, (14, 6.5), (17.5, 10))


@_register("duplicar")
def _copy(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawRoundedRect(QRectF(8, 8, 12, 12), 2, 2)
    _poly(p, (16, 8), (16, 5.5), (14.5, 4), (5.5, 4), (4, 5.5), (4, 14.5), (5.5, 16), (8, 16))


@_register("eliminar")
def _trash(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    _poly(p, (4, 6.5), (20, 6.5))
    _poly(p, (9, 6.5), (9.5, 4), (14.5, 4), (15, 6.5))
    _poly(p, (6, 6.5), (7, 20.5), (17, 20.5), (18, 6.5))
    _poly(p, (10, 10), (10, 17))
    _poly(p, (14, 10), (14, 17))


@_register("importar")
def _import(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    _poly(p, (12, 3), (12, 14))
    _poly(p, (7.5, 9.5), (12, 14), (16.5, 9.5))
    _poly(p, (4, 14), (4, 20), (20, 20), (20, 14))


@_register("exportar")
def _export(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    _poly(p, (12, 14), (12, 3))
    _poly(p, (7.5, 7.5), (12, 3), (16.5, 7.5))
    _poly(p, (4, 14), (4, 20), (20, 20), (20, 14))


@_register("recargar")
def _reload(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawArc(QRectF(4.5, 4.5, 15, 15), 60 * 16, 290 * 16)
    _poly(p, (16.5, 2.8), (16.5, 7.2), (12.2, 7.2))


@_register("carpeta", "abrir")
def _folder(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    _poly(p, (3, 6), (3, 19), (21, 19), (21, 8), (11.5, 8), (9.5, 5), (3, 5), (3, 6))


@_register("guardar")
def _save(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    _poly(p, (4, 4), (4, 20), (20, 20), (20, 7.5), (16.5, 4), (4, 4), close=True)
    p.drawRect(QRectF(7.5, 4, 8, 5))
    p.drawRect(QRectF(7, 13, 10, 7))


@_register("ok")
def _check(p: QPainter, c: QColor) -> None:
    _pen(p, c, 2.2)
    p.drawEllipse(QRectF(2.5, 2.5, 19, 19))
    _poly(p, (7.5, 12.5), (10.8, 15.8), (16.8, 8.8))


@_register("aviso")
def _warning(p: QPainter, c: QColor) -> None:
    _pen(p, c, 2)
    _poly(p, (12, 3), (22, 20.5), (2, 20.5), close=True)
    _poly(p, (12, 9), (12, 14.5))
    p.setBrush(c)
    p.drawEllipse(QPointF(12, 17.6), 0.8, 0.8)


@_register("error")
def _error(p: QPainter, c: QColor) -> None:
    _pen(p, c, 2)
    p.drawEllipse(QRectF(2.5, 2.5, 19, 19))
    _poly(p, (8.5, 8.5), (15.5, 15.5))
    _poly(p, (15.5, 8.5), (8.5, 15.5))


@_register("info")
def _info(p: QPainter, c: QColor) -> None:
    _pen(p, c, 2)
    p.drawEllipse(QRectF(2.5, 2.5, 19, 19))
    _poly(p, (12, 11), (12, 17))
    p.setBrush(c)
    p.drawEllipse(QPointF(12, 7.8), 0.9, 0.9)


@_register("pendiente")
def _pending(p: QPainter, c: QColor) -> None:
    _pen(p, c, 1.8)
    p.drawEllipse(QRectF(3.5, 3.5, 17, 17))


@_register("en_curso")
def _running(p: QPainter, c: QColor) -> None:
    _pen(p, c, 2.2)
    p.drawArc(QRectF(3.5, 3.5, 17, 17), 90 * 16, -270 * 16)
    p.setBrush(c)
    p.drawEllipse(QPointF(12, 12), 3, 3)


@_register("omitido")
def _skipped(p: QPainter, c: QColor) -> None:
    _pen(p, c, 1.8)
    p.drawEllipse(QRectF(3.5, 3.5, 17, 17))
    _poly(p, (8, 12), (16, 12))


@_register("grafica")
def _chart(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    _poly(p, (3.5, 3.5), (3.5, 20.5), (20.5, 20.5))
    _poly(p, (6, 16), (10, 10.5), (13.5, 13.5), (19.5, 6))


@_register("tabla")
def _table(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawRoundedRect(QRectF(3.5, 4.5, 17, 15), 2, 2)
    _poly(p, (3.5, 9), (20.5, 9))
    _poly(p, (3.5, 14), (20.5, 14))
    _poly(p, (9.5, 9), (9.5, 19.5))


@_register("buscar")
def _search(p: QPainter, c: QColor) -> None:
    _pen(p, c, 2)
    p.drawEllipse(QRectF(4, 4, 11.5, 11.5))
    _poly(p, (14, 14), (20, 20))


@_register("cable")
def _cable(p: QPainter, c: QColor) -> None:
    _pen(p, c, 2)
    p.drawRect(QRectF(2.5, 4, 4, 5))
    p.drawRect(QRectF(17.5, 15, 4, 5))
    path = QPainterPath(QPointF(4.5, 9))
    path.cubicTo(QPointF(4.5, 22), QPointF(19.5, 2), QPointF(19.5, 15))
    p.drawPath(path)


@_register("ajustes")
def _settings(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    for y, x in ((6, 8), (12, 15), (18, 10)):
        _poly(p, (3.5, y), (20.5, y))
        p.setBrush(c)
        p.drawEllipse(QPointF(x, y), 2.2, 2.2)
        p.setBrush(Qt.BrushStyle.NoBrush)


@_register("biblioteca")
def _library(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawEllipse(QRectF(4, 3, 16, 5))
    _poly(p, (4, 5.5), (4, 18.5))
    _poly(p, (20, 5.5), (20, 18.5))
    p.drawArc(QRectF(4, 9, 16, 5), 180 * 16, 180 * 16)
    p.drawArc(QRectF(4, 16, 16, 5), 180 * 16, 180 * 16)


@_register("carpeta_nueva")
def _new_folder(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    _poly(p, (3, 6), (3, 19), (13, 19))
    _poly(p, (21, 12), (21, 8), (11.5, 8), (9.5, 5), (3, 5), (3, 6))
    _poly(p, (18, 14), (18, 21))
    _poly(p, (14.5, 17.5), (21.5, 17.5))


@_register("carpeta_externa")
def _external_folder(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    _poly(p, (3, 6), (3, 19), (12, 19))
    _poly(p, (21, 11), (21, 8), (11.5, 8), (9.5, 5), (3, 5), (3, 6))
    _poly(p, (15, 20), (21, 14))
    _poly(p, (16.5, 14), (21, 14), (21, 18.5))


@_register("veredicto")
def _verdict(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    _poly(p, (12, 3), (19, 6), (19, 12), (12, 21), (5, 12), (5, 6), (12, 3), close=True)
    _poly(p, (8.5, 11.5), (11, 14), (15.5, 9))



@_register("condiciones")
def _thermometer(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    p.drawRoundedRect(QRectF(10, 3, 4, 13), 2, 2)
    _poly(p, (16.5, 6), (19, 6))
    _poly(p, (16.5, 9), (19, 9))
    _poly(p, (16.5, 12), (19, 12))
    _poly(p, (12, 8), (12, 15))
    p.setBrush(c)
    p.drawEllipse(QRectF(8.5, 13.5, 7, 7))


@_register("certificado")
def _certificate(p: QPainter, c: QColor) -> None:
    _pen(p, c)
    _poly(p, (12.5, 21), (4, 21), (4, 3), (14.5, 3), (19, 7.5), (19, 11))
    _poly(p, (14.5, 3), (14.5, 7.5), (19, 7.5))
    _poly(p, (7.5, 9), (12.5, 9))
    _poly(p, (7.5, 12.5), (13, 12.5))
    p.drawEllipse(QRectF(14, 12.5, 6, 6))
    _poly(p, (15.2, 18), (14.6, 21.5), (17, 20.3), (19.4, 21.5), (18.8, 18))


def available() -> list[str]:
    return sorted(_ICONS)


@lru_cache(maxsize=512)
def pixmap(name: str, color: str = "#2c3e50", size: int = 24) -> QPixmap:
    ratio = 2
    image = QPixmap(size * ratio, size * ratio)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size * ratio / 24, size * ratio / 24)
    _ICONS.get(name, _pending)(painter, QColor(color))
    painter.end()
    image.setDevicePixelRatio(ratio)
    return image


@lru_cache(maxsize=512)
def icon(name: str, color: str = "#2c3e50", size: int = 24) -> QIcon:
    return QIcon(pixmap(name, color, size))
