"""Stacked, animated top-right desktop alert windows."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QCursor,
    QGuiApplication,
    QKeyEvent,
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
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from signaldesk.icons import SeverityIcon, make_line_icon
from signaldesk.models import Alert
from signaldesk.richtext import apply_rich_text, has_link
from signaldesk.theme import COLORS, color

HOVER_HINT_DELAY_MS = 320
MAX_VISIBLE_TOASTS = 4
NOTIFICATION_GAP = 4
NOTIFICATION_MARGIN = 12
OVERFLOW_DISPLAY_MS = 4500


def _value(source: object, key: str, default: object = None) -> object:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _text(value: object, fallback: str = "") -> str:
    resolved = getattr(value, "value", value)
    result = str(resolved).strip() if resolved is not None else ""
    return result or fallback


def _alert_actions(alert: object) -> list[dict[str, str]]:
    raw = _value(alert, "actions", [])
    if not isinstance(raw, (list, tuple)):
        return []
    return [
        {
            "label": str(_value(action, "label", "Open") or "Open")[:80],
            "url": str(_value(action, "url", "") or ""),
            "kind": _text(_value(action, "kind"), "link").lower(),
        }
        for action in raw
    ]


def _safe_action_url(url: str) -> bool:
    parsed = QUrl(url)
    return parsed.isValid() and parsed.scheme().lower() in {"http", "https", "mailto"}


def animations_enabled() -> bool:
    # Animate by default; SignalDesk's toast motion is intentional and not tied
    # to the Windows "Animation effects" system setting. Opt out explicitly with
    # SIGNALDESK_REDUCE_MOTION=1 (respected for accessibility).
    return os.environ.get("SIGNALDESK_REDUCE_MOTION", "").lower() not in {"1", "true", "yes"}


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
    HEIGHT = 2

    def __init__(self, severity: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._severity = severity
        self._progress = 1.0
        self.setFixedHeight(self.HEIGHT)
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
        # A single solid severity-colored bar that shrinks (no track behind it),
        # antialiased so the leading edge glides sub-pixel instead of stepping.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        fill_width = rect.width() * self._progress
        if fill_width > 0:
            painter.fillRect(QRectF(0.0, 0.0, fill_width, rect.height()), color(self._severity))
        painter.end()


class ToastOpenSurface(QFrame):
    """Keyboard- and pointer-activatable surface for opening alert detail."""

    activated = Signal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ToastOpenSurface")
        self.setProperty("pressed", False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Open alert details")
        self.setAccessibleName(f"Open alert details for {title}")

    def _set_pressed(self, pressed: bool) -> None:
        if bool(self.property("pressed")) == pressed:
            return
        self.setProperty("pressed", pressed)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._set_pressed(True)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        was_pressed = bool(self.property("pressed"))
        self._set_pressed(False)
        if (
            was_pressed
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.activated.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._set_pressed(True)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._set_pressed(False)
            self.activated.emit()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._set_pressed(False)
        super().leaveEvent(event)


class AlertToast(QWidget):
    WIDTH = 424

    closed = Signal(object)
    activated = Signal(str, str)
    lifecycle_requested = Signal(str, str, str, object, str)
    action_requested = Signal(str, str, object)

    def __init__(self, alert: Alert, origin: str = "") -> None:
        super().__init__(None)
        self.alert = alert
        self.origin = origin or str(getattr(alert, "server_url", "") or "")
        self.requires_attention = bool(alert.requires_attention)
        self._target = QPoint()
        self._dismissing = False
        self._action_committed = False
        self._action_hints: dict[QObject, str] = {}
        self._quick_action_buttons: list[QPushButton] = []
        self._animate = animations_enabled()
        self._animation: QParallelAnimationGroup | QPropertyAnimation | None = None
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.setInterval(1200)
        self._feedback_timer.timeout.connect(self.dismiss)
        self._pending_hint_anchor: QPushButton | None = None
        self._pending_hint_text = ""
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.setInterval(HOVER_HINT_DELAY_MS)
        self._hint_timer.timeout.connect(self._show_pending_hint)
        # A single vsync-driven animation is the countdown: it drives the rail
        # smoothly and fires dismissal on completion. Hovering pauses it.
        self._expiry_anim = QVariantAnimation(self)
        self._expiry_anim.setStartValue(1.0)
        self._expiry_anim.setEndValue(0.0)
        self._expiry_anim.setDuration(max(1, int(alert.duration_ms)))
        self._expiry_anim.setEasingCurve(QEasingCurve.Type.Linear)
        self._expiry_anim.valueChanged.connect(self._on_expiry_progress)
        self._expiry_anim.finished.connect(self.dismiss)

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

        self.open_surface = ToastOpenSurface(self.alert.title)
        self.open_surface.activated.connect(self._activate)
        body_layout = QHBoxLayout(self.open_surface)
        body_layout.setContentsMargins(12, 12, 14, 14)
        body_layout.setSpacing(12)

        icon = SeverityIcon(self.alert.severity, 36)
        icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        body_layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(4)

        title = QLabel(self.alert.title)
        title.setObjectName("SectionTitle")
        title.setWordWrap(True)
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        content.addWidget(title)

        message = QLabel()
        message.setProperty("role", "muted")
        message.setWordWrap(True)
        message.setMaximumHeight(64)
        apply_rich_text(message, self.alert.message, COLORS["primary"])
        if not has_link(self.alert.message):
            message.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            message.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        content.addWidget(message)

        structured_actions = [
            action for action in _alert_actions(self.alert) if _safe_action_url(action["url"])
        ][:2]
        if structured_actions:
            structured_row = QHBoxLayout()
            structured_row.setSpacing(6)
            for action in structured_actions:
                button = QPushButton(action["label"])
                button.setObjectName("ToastActionButton")
                button.setMinimumHeight(44)
                button.setAccessibleName(f"{action['label']}, alert action")
                button.setToolTip(action["url"])
                button.clicked.connect(
                    lambda _checked=False, item=action: self.action_requested.emit(
                        self.origin, self.alert.id, dict(item)
                    )
                )
                structured_row.addWidget(button)
            structured_row.addStretch()
            content.addLayout(structured_row)

        if self.requires_attention:
            self.lifecycle_row = QHBoxLayout()
            self.lifecycle_row.setSpacing(6)
            self.lifecycle_row.addStretch()
            self.snooze_button = QPushButton()
            self.snooze_button.setObjectName("IconButton")
            self.snooze_button.setIcon(make_line_icon("clock", COLORS["text_secondary"], 20))
            self.snooze_button.setIconSize(QSize(20, 20))
            self.snooze_button.setFixedSize(44, 44)
            self.snooze_button.setAccessibleName("Remind me about this alert in 15 minutes")
            self.snooze_button.setToolTip("Remind me in 15 minutes")
            self.snooze_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.snooze_button.clicked.connect(self._request_snooze)
            self.lifecycle_row.addWidget(self.snooze_button)
            content.addLayout(self.lifecycle_row)

            self._quick_action_buttons = [self.snooze_button]
            self._action_hints = {
                self.snooze_button: "Remind me in 15 minutes",
            }
            for button in self._quick_action_buttons:
                button.installEventFilter(self)
        body_layout.addLayout(content, 1)
        shell_layout.addWidget(self.open_surface)

        self.expiry_rail = ExpiryRail(self.alert.severity.value)
        shell_layout.addWidget(self.expiry_rail)
        layout.addWidget(shell, 1)

    def _request_snooze(self) -> None:
        if not self._commit_quick_action(
            "Reminder set for 15 minutes.", self.snooze_button
        ):
            return
        remind_at = datetime.now(UTC) + timedelta(minutes=15)
        snoozed_until = remind_at.isoformat().replace("+00:00", "Z")
        self.lifecycle_requested.emit(
            self.origin,
            self.alert.id,
            "snoozed",
            snoozed_until,
            "",
        )

    def _commit_quick_action(self, feedback: str, anchor: QPushButton) -> bool:
        if self._action_committed or self._dismissing:
            return False
        self._action_committed = True
        self._expiry_anim.stop()
        self._hint_timer.stop()
        self._pending_hint_anchor = None
        self._pending_hint_text = ""
        QToolTip.hideText()
        for button in self._quick_action_buttons:
            button.setDisabled(True)
        self._show_action_popup(anchor, feedback)
        self._feedback_timer.start()
        return True

    @staticmethod
    def _show_action_popup(anchor: QPushButton, text: str) -> None:
        popup_at = anchor.mapToGlobal(QPoint(anchor.width() // 2, -6))
        QToolTip.showText(popup_at, text, anchor)

    def _queue_action_popup(self, anchor: QPushButton, text: str, *, delay_ms: int) -> None:
        self._pending_hint_anchor = anchor
        self._pending_hint_text = text
        if delay_ms <= 0:
            self._show_pending_hint()
        else:
            self._hint_timer.start(delay_ms)

    def _show_pending_hint(self) -> None:
        anchor = self._pending_hint_anchor
        if anchor is None or self._action_committed or self._dismissing:
            return
        self._show_action_popup(anchor, self._pending_hint_text)

    def _hide_action_popup(self) -> None:
        self._hint_timer.stop()
        self._pending_hint_anchor = None
        self._pending_hint_text = ""
        QToolTip.hideText()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        hint = self._action_hints.get(watched)
        if hint is not None and not self._action_committed:
            if event.type() == QEvent.Type.Enter:
                self._queue_action_popup(watched, hint, delay_ms=HOVER_HINT_DELAY_MS)
            elif event.type() == QEvent.Type.FocusIn:
                self._queue_action_popup(watched, hint, delay_ms=0)
            elif event.type() in {
                QEvent.Type.Leave,
                QEvent.Type.FocusOut,
                QEvent.Type.Hide,
            }:
                self._hide_action_popup()
            elif event.type() == QEvent.Type.ToolTip:
                # The app-owned delayed tooltip replaces Qt's second native popup.
                return True
        return super().eventFilter(watched, event)

    def _activate(self) -> None:
        if self._dismissing:
            return
        self.activated.emit(self.origin, self.alert.id)
        self.dismiss()

    def show_at(self, target: QPoint) -> None:
        self._target = target
        self.ensurePolished()
        self.adjustSize()
        start = QPoint(target.x() + 56, target.y())
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
        position.setDuration(320)
        position.setStartValue(start)
        position.setEndValue(target)
        position.setEasingCurve(QEasingCurve.Type.OutQuint)
        opacity = QPropertyAnimation(self, b"windowOpacity", self)
        opacity.setDuration(240)
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
        self.expiry_rail.set_progress(1.0)
        self._expiry_anim.start()

    def _on_expiry_progress(self, value: object) -> None:
        # Respect reduce-motion: keep the bar static but still count down.
        if self._animate:
            self.expiry_rail.set_progress(float(value))

    def move_to(self, target: QPoint) -> None:
        self._target = target
        if self._dismissing or self.pos() == target:
            return
        if not self._animate:
            self.move(target)
            return
        animation = QPropertyAnimation(self, b"pos", self)
        animation.setDuration(300)
        animation.setStartValue(self.pos())
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation = animation
        animation.start()

    def dismiss(self) -> None:
        if self._dismissing:
            return
        self._dismissing = True
        self._hide_action_popup()
        self._feedback_timer.stop()
        self._expiry_anim.stop()
        if not self._animate:
            self._finish_close()
            return
        position = QPropertyAnimation(self, b"pos", self)
        position.setDuration(220)
        position.setStartValue(self.pos())
        position.setEndValue(QPoint(self.pos().x() + 48, self.pos().y()))
        position.setEasingCurve(QEasingCurve.Type.InCubic)
        opacity = QPropertyAnimation(self, b"windowOpacity", self)
        opacity.setDuration(200)
        opacity.setStartValue(self.windowOpacity())
        opacity.setEndValue(0.0)
        opacity.setEasingCurve(QEasingCurve.Type.InCubic)
        group = QParallelAnimationGroup(self)
        group.addAnimation(position)
        group.addAnimation(opacity)
        group.finished.connect(self._finish_close)
        self._animation = group
        group.start()

    def _finish_close(self) -> None:
        self._expiry_anim.stop()
        self.closed.emit(self)
        self.close()

    def enterEvent(self, event: QEvent) -> None:
        if not self._dismissing and self._expiry_anim.state() == QAbstractAnimation.State.Running:
            self._expiry_anim.pause()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        if not self._dismissing and self._expiry_anim.state() == QAbstractAnimation.State.Paused:
            self._expiry_anim.resume()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._activate()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class NotificationOverflowIndicator(QPushButton):
    """Compact transient counter for alerts rolled out of the visible stack."""

    WIDTH = 152
    HEIGHT = 40

    def __init__(self) -> None:
        super().__init__(None)
        self.setObjectName("NotificationOverflowIndicator")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setToolTip("Open alert history")

    def set_count(self, count: int) -> None:
        count = max(1, int(count))
        suffix = "alert" if count == 1 else "alerts"
        label = f"+{count:,} more {suffix}" if count <= 9999 else "9,999+ more alerts"
        self.setText(label)
        self.setAccessibleName(
            f"{count} additional {suffix} received; open alert history"
        )

    def show_at(self, target: QPoint) -> None:
        self.move(target)
        self.show()
        self.raise_()


class NotificationManager(QObject):
    activated = Signal(str, str)
    lifecycle_requested = Signal(str, str, str, object, str)
    action_requested = Signal(str, str, object)
    overflow_activated = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._toasts: list[AlertToast] = []
        self._overflow_count = 0
        self._overflow_indicator: NotificationOverflowIndicator | None = None
        self._overflow_timer = QTimer(self)
        self._overflow_timer.setSingleShot(True)
        self._overflow_timer.setInterval(OVERFLOW_DISPLAY_MS)
        self._overflow_timer.timeout.connect(self._hide_overflow)

    def show_alert(self, alert: Alert, origin: str = "") -> None:
        # A burst should never allocate windows that cannot be shown. Alerts are
        # already durable in history before this method is called, so extras can
        # be represented by the transient counter without losing their details.
        self._toasts = [current for current in self._toasts if not current._dismissing]
        area = self._screen_area()
        if area is None or len(self._toasts) >= MAX_VISIBLE_TOASTS:
            self.aggregate_alerts()
            return

        toast = AlertToast(alert, origin)
        toast.closed.connect(self._remove)
        toast.activated.connect(self.activated)
        toast.lifecycle_requested.connect(self.lifecycle_requested)
        toast.action_requested.connect(self.action_requested)
        toast.ensurePolished()
        toast.adjustSize()

        self._toasts.insert(0, toast)
        if not self._stack_fits(area):
            self._toasts.remove(toast)
            toast.deleteLater()
            self.aggregate_alerts()
            return

        targets = self._targets(area)
        if targets:
            toast.show_at(targets[0])
        for current, target in zip(self._toasts[1:], targets[1:], strict=False):
            current.move_to(target)
        self._position_overflow(area)

    def dismiss_all(self) -> None:
        self._hide_overflow()
        for toast in list(self._toasts):
            toast.dismiss()

    def _remove(self, toast: AlertToast) -> None:
        if toast in self._toasts:
            self._toasts.remove(toast)
        area = self._screen_area()
        for current, target in zip(self._toasts, self._targets(area), strict=False):
            current.move_to(target)
        self._position_overflow(area)

    @staticmethod
    def _screen_area() -> QRect | None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return None
        return screen.availableGeometry()

    def _stack_fits(self, area: QRect) -> bool:
        if len(self._toasts) <= 1:
            return True
        height = sum(toast.height() for toast in self._toasts)
        height += NOTIFICATION_GAP * (len(self._toasts) - 1)
        reserved = NotificationOverflowIndicator.HEIGHT + NOTIFICATION_GAP
        return height <= area.height() - (2 * NOTIFICATION_MARGIN) - reserved

    def _targets(self, area: QRect | None = None) -> list[QPoint]:
        area = area or self._screen_area()
        if area is None:
            return []
        x = area.right() - AlertToast.WIDTH - NOTIFICATION_MARGIN
        y = area.top() + NOTIFICATION_MARGIN
        targets: list[QPoint] = []
        for toast in self._toasts:
            toast.ensurePolished()
            toast.adjustSize()
            targets.append(QPoint(x, y))
            y += toast.height() + NOTIFICATION_GAP
        return targets

    def _ensure_overflow_indicator(self) -> NotificationOverflowIndicator:
        if self._overflow_indicator is None:
            self._overflow_indicator = NotificationOverflowIndicator()
            self._overflow_indicator.clicked.connect(self._overflow_clicked)
        return self._overflow_indicator

    def _increment_overflow(self, count: int) -> None:
        self._overflow_count += max(0, int(count))
        indicator = self._ensure_overflow_indicator()
        indicator.set_count(self._overflow_count)
        self._overflow_timer.start()

    def aggregate_alerts(self, count: int = 1) -> None:
        """Surface alerts intentionally grouped before a full toast is created."""
        count = max(0, int(count))
        if count == 0:
            return
        self._increment_overflow(count)
        self._position_overflow()

    def _position_overflow(self, area: QRect | None = None) -> None:
        if self._overflow_count <= 0:
            return
        area = area or self._screen_area()
        if area is None:
            return
        indicator = self._ensure_overflow_indicator()
        y = area.top() + NOTIFICATION_MARGIN
        for toast in self._toasts:
            y += toast.height() + NOTIFICATION_GAP
        maximum_y = area.bottom() - indicator.height() - NOTIFICATION_MARGIN + 1
        y = min(y, maximum_y)
        x = area.right() - indicator.width() - NOTIFICATION_MARGIN + 1
        target = QPoint(x, y)
        if indicator.isVisible():
            indicator.move(target)
        else:
            indicator.show_at(target)

    def _hide_overflow(self) -> None:
        self._overflow_timer.stop()
        self._overflow_count = 0
        if self._overflow_indicator is not None:
            self._overflow_indicator.hide()

    def _overflow_clicked(self) -> None:
        self._hide_overflow()
        self.overflow_activated.emit()
