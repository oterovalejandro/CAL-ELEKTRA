"""Pictograms shown with the operator requests (connect a standard, the through, the DUT…)."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap

WIDTH, HEIGHT = 300, 140
METAL = QColor("#9aa7b6")
DARK = QColor("#3b4b5f")
ACCENT = QColor("#2b7bb9")
GOLD = QColor("#c9a227")


def _port(p: QPainter, x: float, label: str, facing_right: bool = True) -> float:
    """Instrument front panel with a connector; returns the x of the connector face."""
    panel = QRectF(x, 30, 46, 80)
    gradient = QLinearGradient(panel.topLeft(), panel.bottomRight())
    gradient.setColorAt(0, QColor("#dfe5ec"))
    gradient.setColorAt(1, QColor("#b9c3cf"))
    p.setPen(QPen(DARK, 1.4))
    p.setBrush(gradient)
    p.drawRoundedRect(panel, 6, 6)
    p.setPen(DARK)
    font = QFont()
    font.setPointSizeF(8)
    font.setBold(True)
    p.setFont(font)
    p.drawText(QRectF(x, 92, 46, 16), Qt.AlignmentFlag.AlignCenter, label)
    body = QRectF(x + 46 if facing_right else x - 22, 58, 22, 24)
    p.setBrush(METAL)
    p.setPen(QPen(DARK, 1.2))
    p.drawRect(body)
    return body.right() if facing_right else body.left()


def _text(p: QPainter, rect: QRectF, text: str, color: QColor = DARK, size: float = 9) -> None:
    font = QFont()
    font.setPointSizeF(size)
    font.setBold(True)
    p.setFont(font)
    p.setPen(color)
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


def _standard(p: QPainter, x: float, kind: str) -> None:
    body = QRectF(x + 4, 56, 54, 28)
    p.setPen(QPen(DARK, 1.4))
    p.setBrush(GOLD.lighter(130))
    p.drawRoundedRect(body, 5, 5)
    p.setBrush(METAL)
    p.drawRect(QRectF(x, 60, 8, 20))
    end = body.right()
    p.setPen(QPen(DARK, 2.2))
    if kind == "short":
        p.drawLine(QPointF(end + 6, 54), QPointF(end + 6, 86))
        p.drawLine(QPointF(end, 70), QPointF(end + 6, 70))
    elif kind == "open":
        p.drawLine(QPointF(end, 70), QPointF(end + 8, 70))
        p.drawLine(QPointF(end + 8, 62), QPointF(end + 8, 78))
    else:  # match: resistor to ground
        path = QPainterPath(QPointF(end, 70))
        x0 = end
        for k in range(6):
            path.lineTo(QPointF(x0 + 3 + 4 * k, 62 if k % 2 == 0 else 78))
        path.lineTo(QPointF(x0 + 28, 70))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.drawLine(QPointF(x0 + 28, 70), QPointF(x0 + 28, 82))
        for k, half in enumerate((8, 5, 2)):
            p.drawLine(QPointF(x0 + 28 - half, 84 + 3 * k), QPointF(x0 + 28 + half, 84 + 3 * k))
    _text(p, QRectF(x - 4, 26, 110, 22), {"open": "OPEN", "short": "SHORT", "match": "MATCH"}[kind], ACCENT, 10)


def _arrow(p: QPainter, x1: float, x2: float, y: float = 70) -> None:
    p.setPen(QPen(ACCENT, 2, Qt.PenStyle.DashLine))
    p.drawLine(QPointF(x1, y), QPointF(x2 - 6, y))
    p.setPen(QPen(ACCENT, 2))
    p.drawLine(QPointF(x2 - 10, y - 5), QPointF(x2, y))
    p.drawLine(QPointF(x2 - 10, y + 5), QPointF(x2, y))


@lru_cache(maxsize=16)
def illustration(kind: str) -> QPixmap:
    ratio = 2
    image = QPixmap(WIDTH * ratio, HEIGHT * ratio)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(ratio, ratio)
    if kind in ("open", "short", "match"):
        face = _port(p, 20, "PLANO")
        _arrow(p, face + 6, face + 60)
        _standard(p, face + 70, kind)
    elif kind == "through":
        left = _port(p, 20, "PR1")
        right = _port(p, WIDTH - 66, "PR2", facing_right=False)
        p.setPen(QPen(DARK, 1.4))
        p.setBrush(GOLD.lighter(130))
        p.drawRoundedRect(QRectF(left + 8, 60, right - left - 16, 20), 5, 5)
        _text(p, QRectF(left, 26, right - left, 22), "THROUGH", ACCENT, 10)
    elif kind == "dut":
        left = _port(p, 20, "PR1")
        right = _port(p, WIDTH - 66, "PR2", facing_right=False)
        path = QPainterPath(QPointF(left, 70))
        path.cubicTo(QPointF(left + 70, 140), QPointF(right - 70, 0), QPointF(right, 70))
        p.setPen(QPen(DARK, 6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.setPen(QPen(QColor("#5c6f86"), 3))
        p.drawPath(path)
        _text(p, QRectF(left, 18, right - left, 20), "DUT", ACCENT, 10)
        _text(p, QRectF(left - 6, 92, 40, 16), "A", DARK, 8)
        _text(p, QRectF(right - 34, 92, 40, 16), "B", DARK, 8)
    elif kind == "lisn":
        face = _port(p, 20, "PR1")
        box = QRectF(WIDTH - 130, 34, 104, 74)
        p.setPen(QPen(DARK, 1.4))
        p.setBrush(QColor("#e9eef4"))
        p.drawRoundedRect(box, 6, 6)
        _text(p, QRectF(box.left(), box.top() + 4, box.width(), 18), "LISN", ACCENT, 10)
        for k, name in enumerate(("L1", "N")):
            cx = box.left() + 30 + 44 * k
            p.setBrush(QColor("#c0392b") if k == 0 else QColor("#2b7bb9"))
            p.setPen(QPen(DARK, 1.2))
            p.drawEllipse(QPointF(cx, box.top() + 44), 7, 7)
            _text(p, QRectF(cx - 16, box.top() + 54, 32, 14), name, DARK, 8)
        path = QPainterPath(QPointF(face, 70))
        path.cubicTo(QPointF(face + 50, 70), QPointF(box.left() - 10, 120), QPointF(box.left() + 30, box.top() + 51))
        p.setPen(QPen(DARK, 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
    else:
        _text(p, QRectF(0, 0, WIDTH, HEIGHT), "", DARK)
    p.end()
    image.setDevicePixelRatio(ratio)
    return image
