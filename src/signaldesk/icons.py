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


def _chamfered_path(rect: QRectF, cut: float, *, cut_bottom_left: bool = False) -> QPainterPath:
    path = QPainterPath(QPointF(rect.left(), rect.top()))
    path.lineTo(rect.right() - cut, rect.top())
    path.lineTo(rect.right(), rect.top() + cut)
    path.lineTo(rect.right(), rect.bottom())
    if cut_bottom_left:
        path.lineTo(rect.left() + cut, rect.bottom())
        path.lineTo(rect.left(), rect.bottom() - cut)
    else:
        path.lineTo(rect.left(), rect.bottom())
    path.closeSubpath()
    return path


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
        painter.drawPath(
            _chamfered_path(
                QRectF(inset, inset, size - inset * 2, size - inset * 2),
                size * 0.14,
                cut_bottom_left=True,
            )
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
    elif name == "user":
        painter.drawEllipse(QRectF(size * 0.38, size * 0.20, size * 0.24, size * 0.24))
        shoulders = QPainterPath(QPointF(size * 0.24, size * 0.78))
        shoulders.cubicTo(
            QPointF(size * 0.27, size * 0.57),
            QPointF(size * 0.39, size * 0.51),
            QPointF(size * 0.50, size * 0.51),
        )
        shoulders.cubicTo(
            QPointF(size * 0.61, size * 0.51),
            QPointF(size * 0.73, size * 0.57),
            QPointF(size * 0.76, size * 0.78),
        )
        painter.drawPath(shoulders)
    elif name == "play":
        triangle = QPainterPath(QPointF(size * 0.34, size * 0.26))
        triangle.lineTo(size * 0.34, size * 0.74)
        triangle.lineTo(size * 0.74, size * 0.50)
        triangle.closeSubpath()
        painter.setBrush(QColor(color))
        painter.drawPath(triangle)
    elif name == "plus":
        painter.drawLine(QPointF(size * 0.50, size * 0.22), QPointF(size * 0.50, size * 0.78))
        painter.drawLine(QPointF(size * 0.22, size * 0.50), QPointF(size * 0.78, size * 0.50))
    elif name == "chevron_right":
        painter.drawLine(QPointF(size * 0.36, size * 0.24), QPointF(size * 0.64, size * 0.50))
        painter.drawLine(QPointF(size * 0.64, size * 0.50), QPointF(size * 0.36, size * 0.76))
    elif name == "chevron_left":
        painter.drawLine(QPointF(size * 0.64, size * 0.24), QPointF(size * 0.36, size * 0.50))
        painter.drawLine(QPointF(size * 0.36, size * 0.50), QPointF(size * 0.64, size * 0.76))
    elif name == "edit":
        painter.drawLine(QPointF(size * 0.27, size * 0.70), QPointF(size * 0.67, size * 0.30))
        painter.drawLine(QPointF(size * 0.33, size * 0.76), QPointF(size * 0.73, size * 0.36))
        painter.drawLine(QPointF(size * 0.27, size * 0.70), QPointF(size * 0.23, size * 0.79))
        painter.drawLine(QPointF(size * 0.23, size * 0.79), QPointF(size * 0.33, size * 0.76))
        painter.drawLine(QPointF(size * 0.67, size * 0.30), QPointF(size * 0.73, size * 0.36))
    elif name == "key":
        painter.drawEllipse(QRectF(size * 0.20, size * 0.22, size * 0.33, size * 0.33))
        painter.drawLine(QPointF(size * 0.46, size * 0.48), QPointF(size * 0.79, size * 0.78))
        painter.drawLine(QPointF(size * 0.66, size * 0.66), QPointF(size * 0.75, size * 0.57))
        painter.drawLine(QPointF(size * 0.72, size * 0.72), QPointF(size * 0.80, size * 0.64))
    elif name == "trash":
        painter.drawLine(QPointF(size * 0.25, size * 0.31), QPointF(size * 0.75, size * 0.31))
        painter.drawLine(QPointF(size * 0.40, size * 0.23), QPointF(size * 0.60, size * 0.23))
        painter.drawRect(QRectF(size * 0.31, size * 0.36, size * 0.38, size * 0.43))
        painter.drawLine(QPointF(size * 0.43, size * 0.44), QPointF(size * 0.43, size * 0.70))
        painter.drawLine(QPointF(size * 0.57, size * 0.44), QPointF(size * 0.57, size * 0.70))
    elif name == "refresh":
        rect = QRectF(size * 0.22, size * 0.22, size * 0.56, size * 0.56)
        painter.drawArc(rect, 35 * 16, 260 * 16)
        painter.drawLine(QPointF(size * 0.67, size * 0.22), QPointF(size * 0.79, size * 0.24))
        painter.drawLine(QPointF(size * 0.79, size * 0.24), QPointF(size * 0.75, size * 0.36))
    elif name == "bell":
        path = QPainterPath(QPointF(size * 0.27, size * 0.67))
        path.cubicTo(
            QPointF(size * 0.36, size * 0.58),
            QPointF(size * 0.31, size * 0.40),
            QPointF(size * 0.40, size * 0.31),
        )
        path.cubicTo(
            QPointF(size * 0.45, size * 0.25),
            QPointF(size * 0.55, size * 0.25),
            QPointF(size * 0.60, size * 0.31),
        )
        path.cubicTo(
            QPointF(size * 0.69, size * 0.40),
            QPointF(size * 0.64, size * 0.58),
            QPointF(size * 0.73, size * 0.67),
        )
        path.lineTo(size * 0.27, size * 0.67)
        painter.drawPath(path)
        painter.drawLine(QPointF(size * 0.44, size * 0.75), QPointF(size * 0.56, size * 0.75))
    elif name == "history":
        painter.drawEllipse(QRectF(size * 0.24, size * 0.24, size * 0.54, size * 0.54))
        painter.drawLine(QPointF(size * 0.51, size * 0.34), QPointF(size * 0.51, size * 0.52))
        painter.drawLine(QPointF(size * 0.51, size * 0.52), QPointF(size * 0.64, size * 0.60))
        painter.drawLine(QPointF(size * 0.22, size * 0.22), QPointF(size * 0.22, size * 0.40))
        painter.drawLine(QPointF(size * 0.22, size * 0.22), QPointF(size * 0.40, size * 0.22))
    elif name == "inbox":
        tray = QPainterPath(QPointF(size * 0.22, size * 0.30))
        tray.lineTo(size * 0.78, size * 0.30)
        tray.lineTo(size * 0.83, size * 0.72)
        tray.lineTo(size * 0.17, size * 0.72)
        tray.closeSubpath()
        painter.drawPath(tray)
        divider = QPainterPath(QPointF(size * 0.19, size * 0.54))
        divider.lineTo(size * 0.36, size * 0.54)
        divider.lineTo(size * 0.43, size * 0.63)
        divider.lineTo(size * 0.57, size * 0.63)
        divider.lineTo(size * 0.64, size * 0.54)
        divider.lineTo(size * 0.81, size * 0.54)
        painter.drawPath(divider)
    elif name == "filter":
        path = QPainterPath(QPointF(size * 0.20, size * 0.24))
        path.lineTo(size * 0.80, size * 0.24)
        path.lineTo(size * 0.59, size * 0.49)
        path.lineTo(size * 0.59, size * 0.73)
        path.lineTo(size * 0.41, size * 0.80)
        path.lineTo(size * 0.41, size * 0.49)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "filter_clear":
        path = QPainterPath(QPointF(size * 0.17, size * 0.22))
        path.lineTo(size * 0.76, size * 0.22)
        path.lineTo(size * 0.56, size * 0.47)
        path.lineTo(size * 0.56, size * 0.61)
        path.lineTo(size * 0.40, size * 0.69)
        path.lineTo(size * 0.40, size * 0.47)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(size * 0.61, size * 0.60), QPointF(size * 0.82, size * 0.81))
        painter.drawLine(QPointF(size * 0.82, size * 0.60), QPointF(size * 0.61, size * 0.81))
    elif name == "reset":
        painter.drawArc(QRectF(size * 0.23, size * 0.23, size * 0.54, size * 0.54), 40 * 16, 275 * 16)
        painter.drawLine(QPointF(size * 0.23, size * 0.23), QPointF(size * 0.40, size * 0.23))
        painter.drawLine(QPointF(size * 0.23, size * 0.23), QPointF(size * 0.23, size * 0.40))
    elif name == "export":
        painter.drawLine(QPointF(size * 0.50, size * 0.20), QPointF(size * 0.50, size * 0.61))
        painter.drawLine(QPointF(size * 0.36, size * 0.35), QPointF(size * 0.50, size * 0.20))
        painter.drawLine(QPointF(size * 0.64, size * 0.35), QPointF(size * 0.50, size * 0.20))
        path = QPainterPath(QPointF(size * 0.25, size * 0.56))
        path.lineTo(size * 0.25, size * 0.78)
        path.lineTo(size * 0.75, size * 0.78)
        path.lineTo(size * 0.75, size * 0.56)
        painter.drawPath(path)
    elif name == "settings":
        for y, x in ((0.31, 0.40), (0.50, 0.62), (0.69, 0.46)):
            painter.drawLine(QPointF(size * 0.22, size * y), QPointF(size * 0.78, size * y))
            painter.setBrush(QColor(COLORS["surface"]))
            painter.drawEllipse(QRectF(size * (x - 0.07), size * (y - 0.07), size * 0.14, size * 0.14))
            painter.setBrush(Qt.BrushStyle.NoBrush)
    elif name in {"volume", "volume_off"}:
        speaker = QPainterPath(QPointF(size * 0.22, size * 0.43))
        speaker.lineTo(size * 0.36, size * 0.43)
        speaker.lineTo(size * 0.52, size * 0.29)
        speaker.lineTo(size * 0.52, size * 0.71)
        speaker.lineTo(size * 0.36, size * 0.57)
        speaker.lineTo(size * 0.22, size * 0.57)
        speaker.closeSubpath()
        painter.drawPath(speaker)
        if name == "volume":
            painter.drawArc(QRectF(size * 0.43, size * 0.31, size * 0.32, size * 0.38), -55 * 16, 110 * 16)
        else:
            painter.drawLine(QPointF(size * 0.62, size * 0.36), QPointF(size * 0.80, size * 0.64))
            painter.drawLine(QPointF(size * 0.80, size * 0.36), QPointF(size * 0.62, size * 0.64))
    elif name == "tray":
        path = QPainterPath(QPointF(size * 0.22, size * 0.56))
        path.lineTo(size * 0.28, size * 0.77)
        path.lineTo(size * 0.72, size * 0.77)
        path.lineTo(size * 0.78, size * 0.56)
        painter.drawPath(path)
        painter.drawLine(QPointF(size * 0.50, size * 0.20), QPointF(size * 0.50, size * 0.57))
        painter.drawLine(QPointF(size * 0.36, size * 0.44), QPointF(size * 0.50, size * 0.58))
        painter.drawLine(QPointF(size * 0.64, size * 0.44), QPointF(size * 0.50, size * 0.58))
    elif name == "power":
        painter.drawLine(QPointF(size * 0.50, size * 0.18), QPointF(size * 0.50, size * 0.51))
        painter.drawArc(QRectF(size * 0.23, size * 0.24, size * 0.54, size * 0.56), -45 * 16, -270 * 16)
    elif name == "clock":
        painter.drawEllipse(QRectF(size * 0.22, size * 0.22, size * 0.56, size * 0.56))
        painter.drawLine(QPointF(size * 0.50, size * 0.32), QPointF(size * 0.50, size * 0.52))
        painter.drawLine(QPointF(size * 0.50, size * 0.52), QPointF(size * 0.64, size * 0.60))
    elif name == "check_circle":
        painter.drawEllipse(QRectF(size * 0.21, size * 0.21, size * 0.58, size * 0.58))
        painter.drawLine(QPointF(size * 0.34, size * 0.51), QPointF(size * 0.46, size * 0.63))
        painter.drawLine(QPointF(size * 0.46, size * 0.63), QPointF(size * 0.68, size * 0.38))
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
        backdrop.setAlpha(30)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(backdrop)
        painter.drawPath(
            _chamfered_path(
                QRectF(0, 0, self._size, self._size),
                max(4.0, self._size * 0.16),
            )
        )
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
