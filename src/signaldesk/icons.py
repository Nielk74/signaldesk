"""Small vector-painted icons, avoiding platform-dependent emoji glyphs."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from signaldesk.models import Severity
from signaldesk.theme import COLORS, SEVERITY_COLORS


def _rounded_pen(value: QColor | str, width: float) -> QPen:
    pen = QPen(QColor(value), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def make_app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["primary"]))
        inset = max(1.0, size * 0.04)
        painter.drawRoundedRect(
            QRectF(inset, inset, size - inset * 2, size - inset * 2),
            size * 0.25,
            size * 0.25,
        )

        path = QPainterPath(QPointF(size * 0.20, size * 0.54))
        path.lineTo(size * 0.35, size * 0.54)
        path.lineTo(size * 0.43, size * 0.34)
        path.lineTo(size * 0.56, size * 0.69)
        path.lineTo(size * 0.65, size * 0.46)
        path.lineTo(size * 0.80, size * 0.46)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(_rounded_pen(COLORS["on_primary"], max(1.6, size * 0.085)))
        painter.drawPath(path)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def make_line_icon(name: str, color: str = COLORS["text_secondary"], size: int = 18) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(_rounded_pen(color, max(1.5, size * 0.1)))
    margin = size * 0.28
    if name == "close":
        painter.drawLine(QPointF(margin, margin), QPointF(size - margin, size - margin))
        painter.drawLine(QPointF(size - margin, margin), QPointF(margin, size - margin))
    elif name == "check":
        painter.drawLine(QPointF(size * 0.22, size * 0.53), QPointF(size * 0.43, size * 0.72))
        painter.drawLine(QPointF(size * 0.43, size * 0.72), QPointF(size * 0.79, size * 0.30))
    painter.end()
    return QIcon(pixmap)


class SeverityIcon(QWidget):
    def __init__(
        self, severity: Severity | str, size: int = 36, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.severity = Severity.parse(severity)
        self._size = size
        self.setFixedSize(QSize(size, size))
        self.setAccessibleName(f"{self.severity.value.title()} severity")

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = QColor(SEVERITY_COLORS[self.severity.value])
        backdrop = QColor(accent)
        backdrop.setAlpha(35)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(backdrop)
        painter.drawEllipse(QRectF(0, 0, self._size, self._size))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(_rounded_pen(accent, max(1.8, self._size * 0.065)))

        center = self._size / 2
        if self.severity == Severity.SUCCESS:
            painter.drawLine(
                QPointF(self._size * 0.28, center), QPointF(self._size * 0.44, self._size * 0.66)
            )
            painter.drawLine(
                QPointF(self._size * 0.44, self._size * 0.66),
                QPointF(self._size * 0.73, self._size * 0.34),
            )
        elif self.severity == Severity.WARNING:
            path = QPainterPath(QPointF(center, self._size * 0.22))
            path.lineTo(self._size * 0.78, self._size * 0.75)
            path.lineTo(self._size * 0.22, self._size * 0.75)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawLine(QPointF(center, self._size * 0.39), QPointF(center, self._size * 0.56))
            painter.drawPoint(QPointF(center, self._size * 0.65))
        elif self.severity == Severity.CRITICAL:
            painter.drawLine(
                QPointF(self._size * 0.31, self._size * 0.31),
                QPointF(self._size * 0.69, self._size * 0.69),
            )
            painter.drawLine(
                QPointF(self._size * 0.69, self._size * 0.31),
                QPointF(self._size * 0.31, self._size * 0.69),
            )
        else:
            painter.drawLine(QPointF(center, self._size * 0.44), QPointF(center, self._size * 0.69))
            painter.drawPoint(QPointF(center, self._size * 0.31))
        painter.end()
