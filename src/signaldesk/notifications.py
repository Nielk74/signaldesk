"""Stacked, animated top-right desktop alert windows."""

from __future__ import annotations

import ctypes
import os
import sys
import time
from contextlib import suppress
from datetime import datetime

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QCursor,
    QGuiApplication,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPolygon,
    QRegion,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from signaldesk.icons import SeverityIcon, make_line_icon
from signaldesk.models import Alert
from signaldesk.theme import color


def animations_enabled() -> bool:
    if os.environ.get("SIGNALDESK_REDUCE_MOTION", "").lower() in {"1", "true", "yes"}:
        return False
    if sys.platform == "win32":
        setting = ctypes.c_int(1)
        with suppress(AttributeError, OSError):
            if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(setting), 0):
                return bool(setting.value)
    return True


class ElidedLabel(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self.setMinimumWidth(0)
        self.setToolTip(text)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        available = max(0, self.contentsRect().width())
        self.setText(
            self.fontMetrics().elidedText(
                self._full_text,
                Qt.TextElideMode.ElideRight,
                available,
            )
        )


class ChamferFrame(QFrame):
    """Surface with a single clipped corner for the alert silhouette."""

    def __init__(self, cut: int = 12, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cut = cut
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def _paint_path(self) -> QPainterPath:
        rect = QRectF(0.5, 0.5, max(0, self.width() - 1), max(0, self.height() - 1))
        path = QPainterPath()
        path.moveTo(rect.left(), rect.top())
        path.lineTo(rect.right() - self._cut, rect.top())
        path.lineTo(rect.right(), rect.top() + self._cut)
        path.lineTo(rect.right(), rect.bottom())
        path.lineTo(rect.left(), rect.bottom())
        path.closeSubpath()
        return path

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = self._paint_path()
        painter.fillPath(path, color("surface"))
        painter.setPen(QPen(color("border_strong"), 1))
        painter.drawPath(path)
        painter.end()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        width = self.width()
        height = self.height()
        polygon = QPolygon(
            [
                QPoint(0, 0),
                QPoint(max(0, width - self._cut), 0),
                QPoint(width, self._cut),
                QPoint(width, height),
                QPoint(0, height),
            ]
        )
        self.setMask(QRegion(polygon))


class ExpiryRail(QWidget):
    def __init__(self, severity: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._severity = severity
        self._progress = 1.0
        self.setFixedHeight(3)
        self.setAccessibleName("Alert time remaining")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_progress(self, value: float) -> None:
        progress = max(0.0, min(1.0, value))
        if progress == self._progress:
            return
        self._progress = progress
        self.update()

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), color("border"))
        width = round(self.width() * self._progress)
        if width > 0:
            painter.fillRect(0, 0, width, self.height(), color(self._severity))
        painter.end()


class AlertToast(QWidget):
    WIDTH = 424

    closed = Signal(object)
    activated = Signal()

    def __init__(self, alert: Alert) -> None:
        super().__init__(None)
        self.alert = alert
        self._target = QPoint()
        self._dismissing = False
        self._animate = animations_enabled()
        self._animation: QParallelAnimationGroup | QPropertyAnimation | None = None
        self._remaining_ms = float(alert.duration_ms)
        self._last_expiry_tick: float | None = None
        self._expiry_timer = QTimer(self)
        self._expiry_timer.setInterval(50 if self._animate else 250)
        self._expiry_timer.timeout.connect(self._tick_expiry)

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFixedWidth(self.WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"{alert.severity.value.title()} alert: {alert.title}")
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 10, 10)

        card = ChamferFrame(12)
        card.setObjectName("AlertToast")
        card.setProperty("severity", self.alert.severity.value)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(6)
        shadow.setOffset(4, 4)
        shadow.setColor(color("shadow", 60))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        layout = QHBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        accent = QFrame()
        accent.setObjectName("ToastAccent")
        accent.setProperty("severity", self.alert.severity.value)
        accent.setFixedWidth(5)
        layout.addWidget(accent)

        shell = QWidget()
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        metadata_frame = QFrame()
        metadata_frame.setObjectName("ToastMetadata")
        metadata = QHBoxLayout(metadata_frame)
        metadata.setContentsMargins(12, 2, 4, 2)
        metadata.setSpacing(8)
        severity = QLabel(self.alert.severity.value.upper())
        severity.setObjectName("SeverityCode")
        severity.setProperty("severity", self.alert.severity.value)
        severity.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        metadata.addWidget(severity)
        source = ElidedLabel(f"{self.alert.source.upper()} / {self.alert.channel.upper()}")
        source.setProperty("role", "eyebrow")
        source.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        metadata.addWidget(source, 1)
        timestamp = QLabel(datetime.now().strftime("%H:%M"))
        timestamp.setObjectName("ToastTime")
        metadata.addWidget(timestamp)

        close_button = QPushButton()
        close_button.setObjectName("IconButton")
        close_button.setIcon(make_line_icon("close"))
        close_button.setIconSize(QSize(18, 18))
        close_button.setToolTip("Dismiss alert")
        close_button.setAccessibleName("Dismiss alert")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self.dismiss)
        metadata.addWidget(close_button)
        shell_layout.addWidget(metadata_frame)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(12, 12, 14, 14)
        body_layout.setSpacing(12)

        icon = SeverityIcon(self.alert.severity, 36)
        body_layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(4)

        title = QLabel(self.alert.title)
        title.setObjectName("SectionTitle")
        title.setWordWrap(True)
        content.addWidget(title)

        message = QLabel(self.alert.message)
        message.setProperty("role", "muted")
        message.setWordWrap(True)
        message.setMaximumHeight(64)
        content.addWidget(message)
        body_layout.addLayout(content, 1)
        shell_layout.addWidget(body)

        self.expiry_rail = ExpiryRail(self.alert.severity.value)
        shell_layout.addWidget(self.expiry_rail)
        layout.addWidget(shell, 1)

    def show_at(self, target: QPoint) -> None:
        self._target = target
        self.ensurePolished()
        self.adjustSize()
        start = QPoint(target.x() + 44, target.y())
        self.move(start)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()

        if not self._animate:
            self.move(target)
            self.setWindowOpacity(1.0)
            self._start_expiry()
            return

        position = QPropertyAnimation(self, b"pos", self)
        position.setDuration(230)
        position.setStartValue(start)
        position.setEndValue(target)
        position.setEasingCurve(QEasingCurve.Type.OutCubic)
        opacity = QPropertyAnimation(self, b"windowOpacity", self)
        opacity.setDuration(180)
        opacity.setStartValue(0.0)
        opacity.setEndValue(1.0)
        opacity.setEasingCurve(QEasingCurve.Type.OutCubic)
        group = QParallelAnimationGroup(self)
        group.addAnimation(position)
        group.addAnimation(opacity)
        self._animation = group
        group.start()
        self._start_expiry()

    def _start_expiry(self) -> None:
        self._remaining_ms = float(self.alert.duration_ms)
        self._last_expiry_tick = time.monotonic()
        self.expiry_rail.set_progress(1.0)
        self._expiry_timer.start()

    def _tick_expiry(self) -> None:
        if self._dismissing:
            return
        now = time.monotonic()
        if self._last_expiry_tick is None:
            self._last_expiry_tick = now
            return
        elapsed_ms = (now - self._last_expiry_tick) * 1000
        self._last_expiry_tick = now
        self._remaining_ms = max(0.0, self._remaining_ms - elapsed_ms)
        self.expiry_rail.set_progress(self._remaining_ms / self.alert.duration_ms)
        if self._remaining_ms <= 0:
            self.dismiss()

    def move_to(self, target: QPoint) -> None:
        self._target = target
        if self._dismissing or self.pos() == target:
            return
        if not self._animate:
            self.move(target)
            return
        animation = QPropertyAnimation(self, b"pos", self)
        animation.setDuration(180)
        animation.setStartValue(self.pos())
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation = animation
        animation.start()

    def dismiss(self) -> None:
        if self._dismissing:
            return
        self._dismissing = True
        self._expiry_timer.stop()
        if not self._animate:
            self._finish_close()
            return
        position = QPropertyAnimation(self, b"pos", self)
        position.setDuration(150)
        position.setStartValue(self.pos())
        position.setEndValue(QPoint(self.pos().x() + 32, self.pos().y()))
        position.setEasingCurve(QEasingCurve.Type.InCubic)
        opacity = QPropertyAnimation(self, b"windowOpacity", self)
        opacity.setDuration(140)
        opacity.setStartValue(self.windowOpacity())
        opacity.setEndValue(0.0)
        group = QParallelAnimationGroup(self)
        group.addAnimation(position)
        group.addAnimation(opacity)
        group.finished.connect(self._finish_close)
        self._animation = group
        group.start()

    def _finish_close(self) -> None:
        self._expiry_timer.stop()
        self.closed.emit(self)
        self.close()

    def enterEvent(self, event: QEvent) -> None:
        if not self._dismissing:
            self._tick_expiry()
            self._expiry_timer.stop()
            self._last_expiry_tick = None
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        if not self._dismissing:
            self._last_expiry_tick = time.monotonic()
            self._expiry_timer.start()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit()
        super().mouseReleaseEvent(event)


class NotificationManager(QObject):
    activated = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._toasts: list[AlertToast] = []

    def show_alert(self, alert: Alert) -> None:
        toast = AlertToast(alert)
        toast.closed.connect(self._remove)
        toast.activated.connect(self.activated)
        self._toasts.insert(0, toast)
        if len(self._toasts) > 4:
            self._toasts[-1].dismiss()

        targets = self._targets()
        if targets:
            toast.show_at(targets[0])
        for current, target in zip(self._toasts[1:], targets[1:], strict=False):
            current.move_to(target)

    def dismiss_all(self) -> None:
        for toast in list(self._toasts):
            toast.dismiss()

    def _remove(self, toast: AlertToast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        for current, target in zip(self._toasts, self._targets(), strict=False):
            current.move_to(target)

    def _targets(self) -> list[QPoint]:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return []
        area = screen.availableGeometry()
        x = area.right() - AlertToast.WIDTH - 12
        y = area.top() + 12
        targets: list[QPoint] = []
        for toast in self._toasts:
            toast.ensurePolished()
            toast.adjustSize()
            targets.append(QPoint(x, y))
            y += toast.height() + 4
        return targets
