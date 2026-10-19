"""Frequency-coverage chart of a hardware setup.

One bar per equipment on a logarithmic frequency axis, plus the required
range and the effective range (intersection). Uncovered zones are shaded red
and the edges of the limiting equipment are marked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from calelektra.core.frequency import FrequencyRange, RangeContribution
from calelektra.core.units import format_frequency
from calelektra.gui import style
from calelektra.setups.analysis import SetupAnalysis


@dataclass
class _Row:
    kind: str  # segment | required | effective | equipment | empty
    text: str
    range: FrequencyRange | None = None
    color: QColor = field(default_factory=lambda: QColor(style.ACCENT))
    tooltip: str = ""
    mark_low: QColor | None = None
    mark_high: QColor | None = None
    hatched: bool = False
    uncovered: list[FrequencyRange] = field(default_factory=list)


class CoverageWidget(QWidget):
    LABEL_WIDTH = 300
    ROW_HEIGHT = 24
    AXIS_HEIGHT = 34
    RIGHT_MARGIN = 28

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._rows: list[_Row] = []
        self._lo, self._hi = 1.0, 1e11

    # ── data ───────────────────────────────────────────────────────────────
    def set_analysis(self, analysis: SetupAnalysis | None) -> None:
        self._rows = []
        frequencies: list[float] = []
        for segment_analysis in analysis.segments if analysis else []:
            coverage = segment_analysis.coverage
            segment = segment_analysis.segment
            title = segment.name if len(analysis.segments) > 1 or analysis.setup.segments else "Rango completo"
            block_start = len(self._rows)
            self._rows.append(_Row("segment", f"{title}: {segment.range}"))
            self._rows.append(_Row("required", "Rango requerido", segment.range, QColor("#9cc3e6"), f"Requerido: {segment.range}"))
            if coverage.effective is not None:
                color = style.OK if coverage.covered else style.ERROR
                state = "cubre el requerido" if coverage.covered else "NO cubre el requerido"
                self._rows.append(
                    _Row("effective", "Rango efectivo del setup", coverage.effective, color, f"Efectivo: {coverage.effective} — {state}")
                )
            else:
                self._rows.append(_Row("empty", "Rango efectivo del setup: sin rango común", color=style.ERROR))
            uncovered_low = any(u.start == segment.range.start for u in coverage.uncovered)
            uncovered_high = any(u.stop == segment.range.stop for u in coverage.uncovered)
            for contribution in coverage.contributions:
                self._rows.append(self._equipment_row(contribution, coverage, uncovered_low, uncovered_high))
            for row in self._rows[block_start:]:
                row.uncovered = list(coverage.uncovered)
            self._rows.append(_Row("spacer", ""))
            for r in [segment.range] + [c.range for c in coverage.contributions]:
                frequencies.extend(f for f in (r.start, r.stop) if f > 0)
        if frequencies:
            self._lo = 10 ** math.floor(math.log10(min(frequencies)))
            self._hi = 10 ** math.ceil(math.log10(max(frequencies)))
            if self._hi <= self._lo:
                self._hi = self._lo * 10
        self.setMinimumHeight(len(self._rows) * self.ROW_HEIGHT + self.AXIS_HEIGHT + 8)
        self.update()

    def _equipment_row(self, c: RangeContribution, coverage, uncovered_low: bool, uncovered_high: bool) -> _Row:
        tooltip = "\n".join([f"{c.label}: {c.range}", *c.sources])
        row = _Row("equipment", c.label, c.range, QColor("#4f8a8b"), tooltip)
        if c.is_dut:
            row.color, row.hatched = QColor(style.DUT), True
            row.text = f"{c.label} (DUT, no limita)"
            return row
        if c in coverage.lower_limiting:
            row.mark_low = style.ERROR if uncovered_low or coverage.effective is None else QColor("#5d6d7e")
        if c in coverage.upper_limiting:
            row.mark_high = style.ERROR if uncovered_high or coverage.effective is None else QColor("#5d6d7e")
        if (row.mark_low is not None and row.mark_low == style.ERROR) or (
            row.mark_high is not None and row.mark_high == style.ERROR
        ):
            row.color = QColor("#d35400")
            row.text = f"{c.label}  ⚠ limita"
        return row

    # ── geometry ───────────────────────────────────────────────────────────
    def _plot_left(self) -> float:
        return float(self.LABEL_WIDTH)

    def _plot_width(self) -> float:
        return max(50.0, self.width() - self.LABEL_WIDTH - self.RIGHT_MARGIN)

    def _x(self, hz: float) -> float:
        lo, hi = math.log10(self._lo), math.log10(self._hi)
        value = math.log10(hz) if hz > 0 else lo
        value = min(max(value, lo), hi)
        return self._plot_left() + (value - lo) / (hi - lo) * self._plot_width()

    # ── painting ───────────────────────────────────────────────────────────
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        if not self._rows:
            painter.setPen(QColor("#6b7785"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Asigne equipos para ver la cobertura en frecuencia")
            return
        plot_bottom = len(self._rows) * self.ROW_HEIGHT + 4
        self._paint_grid(painter, plot_bottom)
        for index, row in enumerate(self._rows):
            self._paint_row(painter, row, index * self.ROW_HEIGHT + 4)
        self._paint_axis(painter, plot_bottom)

    def _paint_grid(self, painter: QPainter, bottom: float) -> None:
        painter.setPen(QPen(QColor("#e3e8ef"), 1))
        decade = self._lo
        while decade <= self._hi * 1.0001:
            x = self._x(decade)
            painter.drawLine(QPointF(x, 0), QPointF(x, bottom))
            decade *= 10

    def _paint_axis(self, painter: QPainter, top: float) -> None:
        painter.setPen(QPen(QColor("#5d6d7e"), 1))
        painter.drawLine(QPointF(self._plot_left(), top), QPointF(self._plot_left() + self._plot_width(), top))
        font = QFont(painter.font())
        font.setPointSizeF(8)
        painter.setFont(font)
        decade = self._lo
        while decade <= self._hi * 1.0001:
            x = self._x(decade)
            painter.drawLine(QPointF(x, top), QPointF(x, top + 4))
            painter.drawText(QRectF(x - 40, top + 5, 80, 16), Qt.AlignmentFlag.AlignCenter, format_frequency(decade))
            decade *= 10
        painter.drawText(
            QRectF(self._plot_left(), top + 18, self._plot_width(), 14),
            Qt.AlignmentFlag.AlignCenter,
            "Frecuencia (escala logarítmica)",
        )

    def _paint_row(self, painter: QPainter, row: _Row, y: float) -> None:
        h = self.ROW_HEIGHT
        font = QFont(painter.font())
        font.setPointSizeF(9)
        font.setBold(row.kind in ("segment", "effective"))
        painter.setFont(font)
        for uncovered in row.uncovered:
            x1, x2 = self._x(uncovered.start), self._x(uncovered.stop)
            painter.fillRect(QRectF(x1, y, max(2.0, x2 - x1), h), QColor(192, 57, 43, 38))
        if row.kind == "segment":
            painter.setPen(style.PRIMARY)
            painter.drawText(QRectF(6, y, self.width() - 12, h), Qt.AlignmentFlag.AlignVCenter, row.text)
            return
        if row.kind in ("spacer",):
            return
        painter.setPen(row.color if row.kind in ("empty",) else QColor("#2c3e50"))
        painter.drawText(
            QRectF(14, y, self.LABEL_WIDTH - 20, h),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            painter.fontMetrics().elidedText(row.text, Qt.TextElideMode.ElideRight, self.LABEL_WIDTH - 24),
        )
        if row.range is None:
            return
        x1, x2 = self._x(row.range.start), self._x(row.range.stop)
        bar = QRectF(x1, y + 5, max(3.0, x2 - x1), h - 10)
        fill = QColor(row.color)
        fill.setAlpha(150 if row.kind == "equipment" else 200)
        brush = QBrush(fill, Qt.BrushStyle.BDiagPattern) if row.hatched else QBrush(fill)
        painter.setPen(QPen(row.color.darker(120), 1))
        painter.setBrush(brush)
        painter.drawRoundedRect(bar, 3, 3)
        small = QFont(font)
        small.setPointSizeF(8)
        small.setBold(False)
        painter.setFont(small)
        text = str(row.range)
        width = painter.fontMetrics().horizontalAdvance(text) + 8
        light_text = bar.width() > width and not row.hatched and row.kind != "required"
        painter.setPen(QColor("#ffffff") if light_text else QColor("#2c3e50"))
        if bar.width() > width:
            painter.drawText(bar, Qt.AlignmentFlag.AlignCenter, text)
        else:
            painter.drawText(QRectF(bar.right() + 4, y, 200, h), Qt.AlignmentFlag.AlignVCenter, text)
        for color, x in ((row.mark_low, x1), (row.mark_high, x2)):
            if color is not None:
                painter.setPen(QPen(color, 3))
                painter.drawLine(QPointF(x, y + 1), QPointF(x, y + h - 1))

    # ── tooltips ───────────────────────────────────────────────────────────
    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt API)
        index = int((event.position().y() - 4) // self.ROW_HEIGHT)
        if 0 <= index < len(self._rows) and self._rows[index].tooltip:
            QToolTip.showText(event.globalPosition().toPoint(), self._rows[index].tooltip, self)
        else:
            QToolTip.hideText()
