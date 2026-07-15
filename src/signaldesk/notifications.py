"""Stacked, animated top-right desktop alert windows."""

from __future__ import annotations

import ctypes
import os
import sys
from contextlib import suppress

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QCursor, QGuiApplication, QMouseEvent, QResizeEvent
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


class AlertToast(QWidget):
    closed = Signal(object)
    activated = Signal()

    def __init__(self, alert: Alert) -> None:
        super().__init__(None)
        self.alert = alert
        self._target = QPoint()
        self._dismissing = False
        self._animate = animations_enabled()
        self._animation: QParallelAnimationGroup | QPropertyAnimation | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFixedWidth(410)
        self.setAccessibleName(f"{alert.severity.value.title()} alert: {alert.title}")
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        card = QFrame()
        card.setObjectName("AlertToast")
        card.setProperty("severity", self.alert.severity.value)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(26)
        shadow.setOffset(0, 7)
        shadow.setColor(color("shadow", 55))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        layout = QHBoxLayout(card)
        layout.setContentsMargins(10, 14, 12, 14)
        layout.setSpacing(12)

        accent = QFrame()
        accent.setObjectName("ToastAccent")
        accent.setProperty("severity", self.alert.severity.value)
        accent.setFixedWidth(4)
        layout.addWidget(accent)

        icon = SeverityIcon(self.alert.severity, 38)
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(5)

        metadata = QHBoxLayout()
        metadata.setContentsMargins(0, 0, 0, 0)
        metadata.setSpacing(8)
        badge = QLabel(self.alert.severity.value.upper())
        badge.setObjectName("SeverityBadge")
        badge.setProperty("severity", self.alert.severity.value)
        badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        metadata.addWidget(badge)
        source = ElidedLabel(f"{self.alert.source}  ·  {self.alert.channel}")
        source.setProperty("role", "muted")
        source.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        metadata.addWidget(source)
        content.addLayout(metadata)

        title = QLabel(self.alert.title)
        title.setObjectName("SectionTitle")
        title.setWordWrap(True)
        content.addWidget(title)

        message = QLabel(self.alert.message)
        message.setProperty("role", "muted")
        message.setWordWrap(True)
        message.setMaximumHeight(64)
        content.addWidget(message)
        layout.addLayout(content, 1)

        close_button = QPushButton()
        close_button.setObjectName("IconButton")
        close_button.setIcon(make_line_icon("close"))
        close_button.setIconSize(close_button.sizeHint())
        close_button.setToolTip("Dismiss alert")
        close_button.setAccessibleName("Dismiss alert")
        close_button.clicked.connect(self.dismiss)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignTop)

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
            self._timer.start(self.alert.duration_ms)
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
        self._timer.start(self.alert.duration_ms)

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
        self._timer.stop()
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
        self.closed.emit(self)
        self.close()

    def enterEvent(self, event: QEvent) -> None:
        self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        if not self._dismissing:
            self._timer.start(2500)
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
        x = area.right() - 410 - 12
        y = area.top() + 12
        targets: list[QPoint] = []
        for toast in self._toasts:
            toast.ensurePolished()
            toast.adjustSize()
            targets.append(QPoint(x, y))
            y += toast.height() + 2
        return targets
