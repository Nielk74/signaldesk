"""Compact multi-server alert management window."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from PySide6.QtCore import QSize, Qt, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent, QMouseEvent, QPainter, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from signaldesk.config import SEVERITIES, AppConfig, ServerConfig, normalize_server_url
from signaldesk.icons import SeverityIcon, make_app_icon, make_line_icon
from signaldesk.models import Alert, AlertChannel, normalize_channel
from signaldesk.richtext import apply_rich_text, make_selectable
from signaldesk.sounds import BUILTIN_SOUNDS, NONE_ID, display_name
from signaldesk.theme import COLORS, color

_STATE_TEXT = {
    "connected": ("Connected and listening", "LIVE"),
    "connecting": ("Connecting to server", "CONNECTING"),
    "disconnected": ("Server offline", "OFFLINE"),
    "stopped": ("Connection paused", "PAUSED"),
}

DELIVERY_MODES = {
    "toast_sound": "Toast + sound",
    "toast_only": "Toast only",
    "history_only": "History only",
    "muted": "Muted",
}

LIFECYCLE_STATES = ("unread", "snoozed")
HISTORY_PAGE_SIZE = 50
HISTORY_FILTER_DEBOUNCE_MS = 140


def _string_value(value: object, fallback: str = "") -> str:
    """Return a stable string for plain values and enum-like model fields."""
    if value is None:
        return fallback
    enum_value = getattr(value, "value", value)
    text = str(enum_value).strip()
    return text or fallback


def _record_value(record: object, key: str, default: object = None) -> object:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _record_alert(record: object) -> Alert:
    """Coerce persisted alert records without depending on their concrete class."""
    nested = _record_value(record, "alert")
    candidate = nested if nested is not None else record
    if isinstance(candidate, Alert):
        return candidate
    if isinstance(candidate, Mapping):
        return Alert.from_payload(candidate)
    payload = {
        name: getattr(candidate, name, None)
        for name in (
            "id",
            "title",
            "message",
            "severity",
            "channel",
            "source",
            "created_at",
            "duration_ms",
            "requires_attention",
            "lifecycle",
            "actions",
        )
    }
    return Alert.from_payload(payload)


def _record_origin(record: object, fallback: str = "") -> str:
    for source in (record, _record_value(record, "alert")):
        for key in ("server_url", "origin", "server"):
            value = _record_value(source, key)
            if value:
                return str(value)
    return fallback


def _record_status(record: object) -> str:
    if not _record_requires_attention(record):
        return "informational"
    for source in (record, _record_value(record, "alert")):
        for key in ("status", "lifecycle"):
            value = _record_value(source, key)
            status = _string_value(value).lower()
            if status == "snoozed":
                return status
            if status in {"unread", "acknowledged", "resolved"}:
                return "unread"
    return "unread"


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return str(value or "").strip().lower() in {"true", "yes", "on", "1"}


def _record_requires_attention(record: object) -> bool:
    """Support current records plus legacy mappings that explicitly carried lifecycle."""
    nested = _record_value(record, "alert")
    for source in (record, nested):
        if isinstance(source, Mapping):
            if "requires_attention" in source:
                return _bool_value(source.get("requires_attention"))
            if "lifecycle" in source or "status" in source:
                supplied = source.get("lifecycle", source.get("status"))
                return _string_value(supplied).lower() != "informational"
        elif source is not None and hasattr(source, "requires_attention"):
            return bool(source.requires_attention)
    return _record_alert(record).requires_attention


def _action_mapping(action: object) -> dict[str, str]:
    return {
        "label": str(_record_value(action, "label", "Open") or "Open")[:80],
        "url": str(_record_value(action, "url", "") or ""),
        "kind": _string_value(_record_value(action, "kind"), "link").lower(),
    }


def _record_actions(record: object) -> list[dict[str, str]]:
    for source in (_record_value(record, "alert"), record):
        actions = _record_value(source, "actions")
        if isinstance(actions, (list, tuple)):
            return [_action_mapping(action) for action in actions]
    return []


def _safe_action_url(url: str) -> bool:
    parsed = QUrl(url)
    return parsed.isValid() and parsed.scheme().lower() in {"http", "https", "mailto"}


def _refresh_style(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def _set_button_icon(
    button: QPushButton,
    name: str,
    *,
    icon_color: str = COLORS["text_secondary"],
) -> None:
    """Apply one consistent, scalable line icon to a compact action button."""
    button.setIcon(make_line_icon(name, icon_color, 20))
    button.setIconSize(QSize(20, 20))
    button.setProperty("iconName", name)


def _icon_button(
    name: str,
    accessible_name: str,
    *,
    checkable: bool = False,
    primary: bool = False,
    danger: bool = False,
) -> QPushButton:
    """Build a keyboard-operable icon action whose meaning is available on hover and to AT."""
    button = QPushButton()
    button.setObjectName("IconButton")
    button.setCheckable(checkable)
    button.setFixedSize(44, 44)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setToolTip(accessible_name)
    button.setAccessibleName(accessible_name)
    button.setProperty("primary", primary)
    button.setProperty("danger", danger)
    icon_color = (
        COLORS["on_primary"]
        if primary
        else COLORS["critical"]
        if danger
        else COLORS["text_secondary"]
    )
    _set_button_icon(button, name, icon_color=icon_color)
    return button


def _alert_time(value: str) -> str:
    """Return a compact local time for ledger metadata."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return "NOW"


def _inbox_summary(total: int) -> str:
    if total <= 0:
        return "No alerts retained"
    noun = "alert" if total == 1 else "alerts"
    return f"{total:,} {noun} retained"


def _server_label(url: str) -> str:
    """Human-friendly host[:port] extracted from a normalized URL."""
    remainder = url.split("://", 1)[-1]
    return remainder.split("/", 1)[0] or url


class ElidedLabel(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setToolTip(text)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._elide()

    def _elide(self) -> None:
        self.setText(
            self.fontMetrics().elidedText(
                self._full_text,
                Qt.TextElideMode.ElideRight,
                max(0, self.contentsRect().width()),
            )
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()


class StatusDot(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = "disconnected"
        self.setFixedSize(QSize(14, 14))
        self.setAccessibleName("Connection status: offline")

    def set_state(self, state: str) -> None:
        self._state = state
        spoken = {
            "connected": "online",
            "connecting": "connecting",
            "disconnected": "offline",
            "stopped": "paused",
        }.get(state, "offline")
        self.setAccessibleName(f"Connection status: {spoken}")
        self.update()

    def paintEvent(self, event: object) -> None:
        del event
        painter = QPainter(self)
        token = {
            "connected": "success",
            "connecting": "warning",
            "disconnected": "critical",
            "stopped": "critical",
        }.get(self._state, "critical")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color(token))
        painter.drawRect(3, 3, 8, 8)
        painter.end()


class Metric(QFrame):
    def __init__(self, label: str, value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MetricCell")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 11)
        layout.setSpacing(3)
        caption = QLabel(label)
        caption.setProperty("role", "eyebrow")
        self.value = QLabel(value)
        self.value.setObjectName("MetricValue")
        layout.addWidget(caption)
        layout.addWidget(self.value)


class ChannelRow(QFrame):
    toggled = Signal(str, bool)

    def __init__(
        self, channel: AlertChannel, selected: bool, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.channel = channel
        self.setObjectName("ChannelRow")
        self.setProperty("selected", selected)
        self.setMinimumHeight(76)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.marker = QFrame()
        self.marker.setObjectName("ChannelMarker")
        self.marker.setProperty("selected", selected)
        self.marker.setFixedWidth(4)
        layout.addWidget(self.marker)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(14, 10, 12, 10)
        body_layout.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(3)
        heading = QHBoxLayout()
        heading.setSpacing(8)
        name = QLabel(channel.name)
        name.setObjectName("SectionTitle")
        name.setWordWrap(True)
        name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        heading.addWidget(name, 1)
        description = QLabel(channel.description)
        description.setProperty("role", "muted")
        description.setWordWrap(True)
        description.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addLayout(heading)
        text.addWidget(description)
        body_layout.addLayout(text, 1)

        self.checkbox = _icon_button(
            "check",
            f"Subscribe to {channel.name}",
            checkable=True,
        )
        self.checkbox.setChecked(selected)
        self.checkbox.toggled.connect(self._changed)
        body_layout.addWidget(self.checkbox, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(body, 1)

    def _changed(self, selected: bool) -> None:
        self._set_visual_state(selected)
        self.toggled.emit(self.channel.key, selected)

    def _set_visual_state(self, selected: bool) -> None:
        action = "Unsubscribe from" if selected else "Subscribe to"
        label = f"{action} {self.channel.name}"
        self.checkbox.setToolTip(label)
        self.checkbox.setAccessibleName(label)
        _set_button_icon(
            self.checkbox,
            "check" if selected else "plus",
            icon_color=COLORS["primary"] if selected else COLORS["text_secondary"],
        )
        self.setProperty("selected", selected)
        self.marker.setProperty("selected", selected)
        _refresh_style(self)
        _refresh_style(self.marker)

    def set_selected(self, selected: bool) -> None:
        previous = self.checkbox.blockSignals(True)
        self.checkbox.setChecked(selected)
        self._set_visual_state(selected)
        self.checkbox.blockSignals(previous)


class AlertHistoryRow(QFrame):
    """Keyboard-operable history entry that opens the full alert detail."""

    activated = Signal(object)

    def __init__(self, record: object, origin: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.record = record
        alert = _record_alert(record)
        origin = _record_origin(record, origin)
        self.setObjectName("AlertHistoryRow")
        self.setMinimumHeight(104)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(
            f"{_string_value(alert.severity).title()} alert, {alert.title}, "
            f"from {alert.source}. Open details"
        )
        self.setToolTip("Open alert details (Enter or Space)")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        accent = QFrame()
        accent.setObjectName("HistoryAccent")
        accent.setProperty("severity", _string_value(alert.severity, "info"))
        accent.setFixedWidth(4)
        layout.addWidget(accent)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(13, 11, 14, 11)
        body_layout.setSpacing(11)
        body_layout.addWidget(SeverityIcon(alert.severity, 30), 0, Qt.AlignmentFlag.AlignTop)

        content = QVBoxLayout()
        content.setSpacing(4)
        metadata = QHBoxLayout()
        metadata.setSpacing(8)
        severity_name = _string_value(alert.severity, "info")
        severity = QLabel(severity_name.upper())
        severity.setObjectName("SeverityCode")
        severity.setProperty("severity", severity_name)
        tag = f"{alert.source.upper()} / {alert.channel.upper()}"
        if origin:
            tag = f"{tag} / {origin.upper()}"
        origin_label = ElidedLabel(tag)
        origin_label.setProperty("role", "eyebrow")
        metadata.addWidget(severity)
        metadata.addWidget(origin_label, 1)
        timestamp = QLabel(_alert_time(alert.created_at))
        timestamp.setObjectName("HistoryTime")
        metadata.addWidget(timestamp)
        content.addLayout(metadata)

        title = QLabel(alert.title)
        title.setObjectName("SectionTitle")
        title.setWordWrap(True)
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        make_selectable(title)
        content.addWidget(title)

        message = QLabel()
        message.setProperty("role", "muted")
        message.setWordWrap(True)
        message.setMaximumHeight(36)
        message.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        apply_rich_text(message, alert.message, COLORS["primary"])
        content.addWidget(message)
        body_layout.addLayout(content, 1)

        open_button = _icon_button(
            "chevron_right",
            f"Open details for {alert.title}",
        )
        open_button.clicked.connect(lambda: self.activated.emit(self.record))
        body_layout.addWidget(open_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(body, 1)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.record)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.record)
            event.accept()
            return
        super().keyPressEvent(event)


class AlertDetailDialog(QDialog):
    """Full, selectable alert detail and optional reminder controls."""

    lifecycle_requested = Signal(str, str, str, object, str)
    action_requested = Signal(str, str, object)

    def __init__(self, record: object, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.record = record
        self.alert = _record_alert(record)
        self.server_url = _record_origin(record)
        self.requires_attention = _record_requires_attention(record)
        self.status = _record_status(record)
        self.snoozed_until = str(
            _record_value(record, "snoozed_until")
            or _record_value(_record_value(record, "alert"), "snoozed_until")
            or ""
        )
        self._undo_status: str | None = None
        self._undo_snoozed_until: str | None = None
        self.setObjectName("AlertDetailDialog")
        self.setWindowTitle(f"Alert details — {self.alert.title}")
        self.setModal(False)
        self.resize(620, 660)
        self.setMinimumSize(500, 520)
        self.setAccessibleName(f"Alert details for {self.alert.title}")
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(14)

        heading = QHBoxLayout()
        heading.setSpacing(12)
        heading.addWidget(SeverityIcon(self.alert.severity, 40), 0, Qt.AlignmentFlag.AlignTop)
        heading_text = QVBoxLayout()
        heading_text.setSpacing(3)
        kicker = QLabel(
            f"{_string_value(self.alert.severity, 'info').upper()} / {self.alert.channel.upper()}"
        )
        kicker.setProperty("role", "eyebrow")
        title = QLabel(self.alert.title)
        title.setObjectName("DialogTitle")
        title.setWordWrap(True)
        make_selectable(title)
        heading_text.addWidget(kicker)
        heading_text.addWidget(title)
        heading.addLayout(heading_text, 1)
        outer.addLayout(heading)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 2, 0)
        content_layout.setSpacing(14)

        message_card = QFrame()
        message_card.setObjectName("Card")
        message_layout = QVBoxLayout(message_card)
        message_layout.setContentsMargins(16, 14, 16, 16)
        message_layout.setSpacing(6)
        message_heading = QLabel("MESSAGE")
        message_heading.setProperty("role", "eyebrow")
        message = QLabel()
        message.setWordWrap(True)
        apply_rich_text(message, self.alert.message, COLORS["primary"])
        message.setAccessibleName("Full alert message")
        message_layout.addWidget(message_heading)
        message_layout.addWidget(message)
        content_layout.addWidget(message_card)

        metadata_card = QGroupBox("Alert metadata")
        metadata_card.setAccessibleName("Alert metadata")
        metadata = QFormLayout(metadata_card)
        metadata.setContentsMargins(16, 16, 16, 16)
        metadata.setHorizontalSpacing(16)
        metadata.setVerticalSpacing(8)
        fields = [
            ("Alert ID", self.alert.id),
            ("Source", self.alert.source),
            ("Channel", self.alert.channel),
            ("Server", self.server_url or "Local / unknown"),
            ("Created", self.alert.created_at or "Unknown"),
        ]
        received_at = _record_value(self.record, "received_at")
        sequence = _record_value(self.record, "sequence")
        if received_at:
            fields.append(("Received", str(received_at)))
        if sequence is not None:
            fields.append(("Sequence", str(sequence)))
        for label_text, value in fields:
            value_label = QLabel(str(value))
            value_label.setWordWrap(True)
            make_selectable(value_label)
            metadata.addRow(label_text, value_label)
        content_layout.addWidget(metadata_card)

        actions = _record_actions(self.record)
        if actions:
            actions_card = QGroupBox("Alert actions")
            actions_layout = QVBoxLayout(actions_card)
            actions_layout.setContentsMargins(14, 16, 14, 14)
            actions_layout.setSpacing(8)
            for action in actions:
                button = QPushButton(action["label"])
                button.setObjectName(
                    "PrimaryButton"
                    if action["kind"] in {"primary", "runbook"}
                    else "SecondaryButton"
                )
                button.setMinimumHeight(44)
                button.setAccessibleName(f"{action['label']}, opens an external alert action")
                if _safe_action_url(action["url"]):
                    button.setToolTip(action["url"])
                    button.clicked.connect(
                        lambda _checked=False, item=action: self.action_requested.emit(
                            self.server_url, self.alert.id, dict(item)
                        )
                    )
                else:
                    button.setDisabled(True)
                    button.setToolTip("This action URL is not supported")
                actions_layout.addWidget(button)
            content_layout.insertWidget(1, actions_card)

        if self.requires_attention:
            self.reminder_card = QGroupBox("Reminder")
            reminder_layout = QVBoxLayout(self.reminder_card)
            reminder_layout.setContentsMargins(14, 16, 14, 14)
            reminder_layout.setSpacing(10)
            self.reminder_explanation = QLabel()
            self.reminder_explanation.setProperty("role", "muted")
            self.reminder_explanation.setWordWrap(True)
            self.reminder_explanation.setAccessibleName("Current reminder time")
            reminder_layout.addWidget(self.reminder_explanation)

            self.reminder_controls = QWidget()
            reminder_controls_layout = QVBoxLayout(self.reminder_controls)
            reminder_controls_layout.setContentsMargins(0, 0, 0, 0)
            reminder_controls_layout.setSpacing(8)
            reminder_row = QHBoxLayout()
            reminder_row.setSpacing(8)
            self.wake_button = QPushButton("Wake now")
            self.wake_button.setObjectName("SecondaryButton")
            self.wake_button.setMinimumHeight(44)
            self.wake_button.setAccessibleName("Clear the current reminder")
            self.wake_button.setIcon(make_line_icon("bell", COLORS["text_secondary"], 18))
            self.wake_button.setIconSize(QSize(18, 18))
            self.wake_button.clicked.connect(lambda: self._request_lifecycle("unread"))
            reminder_row.addWidget(self.wake_button)
            self.snooze_preset = QComboBox()
            self.snooze_preset.setAccessibleName("Reminder duration")
            self.snooze_preset.addItem("15 minutes", 15 * 60)
            self.snooze_preset.addItem("1 hour", 60 * 60)
            self.snooze_preset.addItem("4 hours", 4 * 60 * 60)
            self.snooze_preset.addItem("Until tomorrow", 24 * 60 * 60)
            self.snooze_preset.setMinimumHeight(44)
            reminder_row.addWidget(self.snooze_preset, 1)
            self.snooze_button = QPushButton("Remind me")
            self.snooze_button.setObjectName("SecondaryButton")
            self.snooze_button.setMinimumHeight(44)
            self.snooze_button.setAccessibleName("Set a reminder for this alert")
            self.snooze_button.setIcon(make_line_icon("clock", COLORS["text_secondary"], 18))
            self.snooze_button.setIconSize(QSize(18, 18))
            self.snooze_button.clicked.connect(self._request_snooze)
            reminder_row.addWidget(self.snooze_button)
            reminder_controls_layout.addLayout(reminder_row)
            reminder_layout.addWidget(self.reminder_controls)

            self.status_feedback = QFrame()
            self.status_feedback.setObjectName("StatusFeedback")
            feedback_layout = QHBoxLayout(self.status_feedback)
            feedback_layout.setContentsMargins(10, 6, 6, 6)
            feedback_layout.setSpacing(8)
            self.status_feedback_label = QLabel()
            self.status_feedback_label.setWordWrap(True)
            self.status_feedback_label.setAccessibleName("Reminder update confirmation")
            feedback_layout.addWidget(self.status_feedback_label, 1)
            self.undo_button = QPushButton("Undo")
            self.undo_button.setObjectName("GhostButton")
            self.undo_button.setMinimumHeight(44)
            self.undo_button.setAccessibleName("Undo the last reminder change")
            self.undo_button.clicked.connect(self._undo_last_lifecycle_change)
            feedback_layout.addWidget(self.undo_button)
            self.status_feedback.hide()
            reminder_layout.addWidget(self.status_feedback)

            self._update_reminder_surface()
            content_layout.insertWidget(1, self.reminder_card)
        content_layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.setAccessibleName("Close alert details")
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _reminder_description(self) -> str:
        if self.status != "snoozed":
            return ""
        deadline = self.snoozed_until
        try:
            parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            local = parsed.astimezone()
            deadline = local.strftime("%a %d %b at %H:%M")
        except (TypeError, ValueError):
            deadline = "the selected time"
        return f"Scheduled for {deadline} · notifications are paused until then."

    def _update_reminder_surface(self) -> None:
        description = self._reminder_description()
        self.reminder_explanation.setText(description)
        self.reminder_explanation.setVisible(bool(description))
        self.wake_button.setVisible(self.status == "snoozed")
        reminder_label = "Change reminder" if self.status == "snoozed" else "Remind me"
        self.snooze_button.setText(reminder_label)
        self.snooze_button.setAccessibleName(
            "Change this alert reminder"
            if self.status == "snoozed"
            else "Set a reminder for this alert"
        )

    def _request_lifecycle(
        self,
        state: str,
        snooze_until: str | None = None,
        *,
        offer_undo: bool = True,
    ) -> None:
        if not self.requires_attention or state not in LIFECYCLE_STATES:
            return
        previous_status = self.status
        previous_snooze = self.snoozed_until or None
        self.lifecycle_requested.emit(
            self.server_url,
            self.alert.id,
            state,
            snooze_until,
            "",
        )
        self.status = state
        self.snoozed_until = snooze_until or ""
        self._update_reminder_surface()
        self.status_feedback.show()
        feedback = "Reminder set" if state == "snoozed" else "Reminder cleared"
        self.status_feedback_label.setText(feedback)
        if offer_undo and previous_status != state:
            self._undo_status = previous_status
            self._undo_snoozed_until = previous_snooze
            self.undo_button.show()
        else:
            self._undo_status = None
            self._undo_snoozed_until = None
            self.undo_button.hide()

    def _undo_last_lifecycle_change(self) -> None:
        if self._undo_status is None:
            return
        status = self._undo_status
        snoozed_until = self._undo_snoozed_until
        self._request_lifecycle(
            status,
            snoozed_until,
            offer_undo=False,
        )
        feedback = "Previous reminder restored" if status == "snoozed" else "Reminder cleared"
        self.status_feedback_label.setText(feedback)

    def _request_snooze(self) -> None:
        seconds = int(self.snooze_preset.currentData() or 15 * 60)
        until = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
        self._request_lifecycle("snoozed", until)


class _Clickable(QWidget):
    """A widget that emits ``clicked`` on a left mouse press."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ServerStatusRow(QFrame):
    """A compact, expandable per-server status row on the Overview tab.

    Collapsed it is a single line (dot + name + status). Clicking the header
    reveals the connection detail, health metrics, and a reconnect button.
    """

    reconnect_requested = Signal(str)

    def __init__(self, url: str, alias: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.url = url
        self._state = "disconnected"
        self._last_health_at: float | None = None
        self._last_connected_text = "Never connected"
        self._offline_since: float | None = time.monotonic()
        self._rtt_text = ""
        self.setObjectName("ChannelRow")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = _Clickable()
        self.header = header
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        header.setAccessibleName(f"Show connection details for {alias or _server_label(url)}")
        header.clicked.connect(self.toggle)
        head_layout = QHBoxLayout(header)
        head_layout.setContentsMargins(12, 9, 12, 9)
        head_layout.setSpacing(9)
        self.status_dot = StatusDot()
        head_layout.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.title_label = ElidedLabel(alias or _server_label(url))
        self.title_label.setObjectName("SectionTitle")
        head_layout.addWidget(self.title_label, 1)
        self.summary_label = QLabel("OFFLINE")
        self.summary_label.setObjectName("RowStatus")
        self.summary_label.setProperty("connectionState", "disconnected")
        head_layout.addWidget(self.summary_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.chevron = QLabel("+")
        self.chevron.setProperty("role", "muted")
        head_layout.addWidget(self.chevron, 0, Qt.AlignmentFlag.AlignVCenter)
        # Let clicks fall through the passive children to the header.
        for passive in (self.status_dot, self.title_label, self.summary_label, self.chevron):
            passive.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        outer.addWidget(header)

        self.detail = QWidget()
        detail_layout = QVBoxLayout(self.detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(0)
        rule = QFrame()
        rule.setObjectName("HorizontalRule")
        detail_layout.addWidget(rule)

        detail_body = QWidget()
        body_layout = QHBoxLayout(detail_body)
        body_layout.setContentsMargins(12, 9, 12, 9)
        body_layout.setSpacing(9)
        self.connection_detail = QLabel("Trying to reach the configured endpoint")
        self.connection_detail.setProperty("role", "muted")
        self.connection_detail.setWordWrap(True)
        self.connection_detail.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        detail_text = QVBoxLayout()
        detail_text.setSpacing(3)
        detail_text.addWidget(self.connection_detail)
        self.reliability_label = QLabel("Last connected: never · Offline for: just now")
        self.reliability_label.setProperty("role", "muted")
        self.reliability_label.setWordWrap(True)
        self.reliability_label.setAccessibleName("Connection reliability status")
        detail_text.addWidget(self.reliability_label)
        body_layout.addLayout(detail_text, 1)
        self.reconnect_button = _icon_button("refresh", f"Reconnect to {url}")
        self.reconnect_button.clicked.connect(lambda: self.reconnect_requested.emit(self.url))
        body_layout.addWidget(self.reconnect_button, 0, Qt.AlignmentFlag.AlignTop)
        detail_layout.addWidget(detail_body)

        metric_strip = QFrame()
        metric_strip.setObjectName("MetricStrip")
        metrics = QHBoxLayout(metric_strip)
        metrics.setContentsMargins(0, 0, 0, 0)
        metrics.setSpacing(0)
        self.latency_metric = Metric("ROUND TRIP", "—")
        self.transport_metric = Metric("TRANSPORT", "—")
        self.heartbeat_metric = Metric("LAST HEARTBEAT", "Waiting")
        self.heartbeat_metric.setProperty("last", True)
        metrics.addWidget(self.latency_metric, 1)
        metrics.addWidget(self.transport_metric, 1)
        metrics.addWidget(self.heartbeat_metric, 1)
        detail_layout.addWidget(metric_strip)

        self.detail.setVisible(False)
        outer.addWidget(self.detail)

    def toggle(self) -> None:
        self.set_expanded(not self.detail.isVisible())

    def set_expanded(self, expanded: bool) -> None:
        self.detail.setVisible(expanded)
        self.chevron.setText("–" if expanded else "+")
        action = "Hide" if expanded else "Show"
        self.header.setAccessibleDescription(f"{action} server connection details")

    def set_alias(self, alias: str) -> None:
        label = alias or _server_label(self.url)
        self.title_label.set_full_text(label)
        self.header.setAccessibleName(f"Show connection details for {label}")

    def _update_summary(self) -> None:
        _title, pill = _STATE_TEXT.get(self._state, _STATE_TEXT["disconnected"])
        text = (
            f"{pill} · {self._rtt_text}" if self._state == "connected" and self._rtt_text else pill
        )
        self.summary_label.setText(text)
        self.summary_label.setProperty("connectionState", self._state)
        _refresh_style(self.summary_label)

    def set_state(self, state: str, detail: str) -> None:
        previous = self._state
        self._state = state
        self.status_dot.set_state(state)
        self.connection_detail.setText(detail)
        self.reconnect_button.setDisabled(state == "connecting")
        if state == "connected":
            if previous != "connected":
                self._last_connected_text = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
            self._offline_since = None
        elif previous == "connected" or self._offline_since is None:
            self._offline_since = time.monotonic()
        if state != "connected":
            self._rtt_text = ""
            self.latency_metric.value.setText("—")
            self.transport_metric.value.setText("—")
            self.heartbeat_metric.value.setText("Waiting")
            self._last_health_at = None
        self._update_summary()
        self._update_reliability()

    def set_health(self, rtt_ms: int, transport: str) -> None:
        self._last_health_at = time.monotonic()
        self._rtt_text = f"{rtt_ms} ms"
        self.latency_metric.value.setText(f"{rtt_ms} ms")
        self.transport_metric.value.setText(transport)
        self.heartbeat_metric.value.setText("Now")
        self._update_summary()

    def refresh_age(self) -> None:
        if self._last_health_at is not None and self._state == "connected":
            age = max(0, int(time.monotonic() - self._last_health_at))
            if age < 2:
                value = "Now"
            elif age < 60:
                value = f"{age}s ago"
            else:
                value = f"{age // 60}m ago"
            self.heartbeat_metric.value.setText(value)
        self._update_reliability()

    def set_reliability(
        self, *, last_connected: str | None = None, offline_since: str | float | None = None
    ) -> None:
        """Set controller-supplied last-connected and offline timing information."""
        if last_connected:
            self._last_connected_text = str(last_connected)
        if offline_since is None:
            self._offline_since = None if self._state == "connected" else time.monotonic()
        elif isinstance(offline_since, (int, float)):
            # Numeric values are treated as elapsed seconds for a stable UI contract.
            self._offline_since = time.monotonic() - max(0.0, float(offline_since))
        else:
            try:
                parsed = datetime.fromisoformat(str(offline_since).replace("Z", "+00:00"))
                elapsed = max(0.0, (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds())
                self._offline_since = time.monotonic() - elapsed
            except (TypeError, ValueError):
                self._offline_since = time.monotonic()
        self._update_reliability()

    def _update_reliability(self) -> None:
        if self._state == "connected":
            text = f"Last connected: {self._last_connected_text} · Currently online"
        else:
            elapsed = (
                max(0, int(time.monotonic() - self._offline_since))
                if self._offline_since is not None
                else 0
            )
            if elapsed < 60:
                duration = f"{elapsed}s"
            elif elapsed < 3600:
                duration = f"{elapsed // 60}m"
            else:
                duration = f"{elapsed // 3600}h {(elapsed % 3600) // 60}m"
            text = f"Last connected: {self._last_connected_text} · Offline for: {duration}"
        self.reliability_label.setText(text)


class ServerPanel(QFrame):
    """Per-server endpoint header plus its channel subscription toggles."""

    toggled = Signal(str, str, bool)
    remove_requested = Signal(str)
    renamed = Signal(str, str)  # url, alias
    auth_save_requested = Signal(str, str)
    auth_clear_requested = Signal(str)

    def __init__(
        self,
        url: str,
        channels: list[AlertChannel],
        selected: set[str],
        alias: str = "",
        catalog_loaded: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.url = url
        self._rows: dict[str, ChannelRow] = {}
        self._catalog_loaded = catalog_loaded
        self.channel_state_label: QLabel | None = None
        self.setObjectName("EndpointCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        accent = QFrame()
        accent.setObjectName("PanelAccent")
        layout.addWidget(accent)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(15, 12, 12, 10)
        header_layout.setSpacing(10)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        labels = QVBoxLayout()
        labels.setSpacing(1)
        display_name = alias.strip() or _server_label(url)
        self.server_name = ElidedLabel(display_name)
        self.server_name.setObjectName("SectionTitle")
        endpoint = ElidedLabel(url)
        endpoint.setProperty("role", "muted")
        labels.addWidget(self.server_name)
        labels.addWidget(endpoint)
        title_row.addLayout(labels, 1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("CounterLabel")
        title_row.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.alias_button = _icon_button(
            "edit",
            f"Edit the name of {display_name}",
            checkable=True,
        )
        self.alias_button.toggled.connect(self._toggle_alias_editor)
        title_row.addWidget(self.alias_button)
        self.auth_button = _icon_button(
            "key",
            f"Set an optional authentication token for {display_name}",
            checkable=True,
        )
        self.auth_button.toggled.connect(self._toggle_auth_editor)
        title_row.addWidget(self.auth_button)
        self.remove_button = _icon_button(
            "trash",
            f"Remove {display_name}",
            danger=True,
        )
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.url))
        title_row.addWidget(self.remove_button)
        header_layout.addLayout(title_row)

        self.alias_editor = QFrame()
        self.alias_editor.setObjectName("InlineEditor")
        alias_row = QHBoxLayout(self.alias_editor)
        alias_row.setContentsMargins(10, 8, 8, 8)
        alias_row.setSpacing(8)
        self.alias_input = QLineEdit(alias)
        self.alias_input.setPlaceholderText("Friendly name (optional)")
        self.alias_input.setAccessibleName(f"Alias for {url}")
        self.alias_input.editingFinished.connect(self._alias_committed)
        alias_row.addWidget(self.alias_input, 1)
        self.alias_save_button = _icon_button(
            "check",
            f"Save the name of {display_name}",
            primary=True,
        )
        self.alias_save_button.clicked.connect(self._finish_alias_edit)
        alias_row.addWidget(self.alias_save_button)
        self.alias_editor.hide()
        header_layout.addWidget(self.alias_editor)

        self.auth_group = QFrame()
        self.auth_group.setObjectName("InlineEditor")
        self.auth_group.setAccessibleName(f"Optional authentication for {url}")
        auth_layout = QVBoxLayout(self.auth_group)
        auth_layout.setContentsMargins(10, 8, 8, 8)
        auth_layout.setSpacing(7)
        auth_row = QHBoxLayout()
        auth_row.setSpacing(8)
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("Bearer token (optional)")
        self.token_input.setAccessibleName(f"New authentication token for {url}")
        self.token_input.setClearButtonEnabled(True)
        self.token_input.returnPressed.connect(self._save_token)
        auth_row.addWidget(self.token_input, 1)
        self.save_token_button = _icon_button(
            "check",
            f"Store the authentication token for {display_name}",
            primary=True,
        )
        self.save_token_button.setDisabled(True)
        self.save_token_button.clicked.connect(self._save_token)
        auth_row.addWidget(self.save_token_button)
        self.clear_token_button = _icon_button(
            "trash",
            f"Clear the stored authentication token for {display_name}",
            danger=True,
        )
        self.clear_token_button.clicked.connect(self._clear_token)
        auth_row.addWidget(self.clear_token_button)
        auth_layout.addLayout(auth_row)
        self.auth_status = QLabel("Optional token · contents are never displayed")
        self.auth_status.setProperty("role", "muted")
        self.auth_status.setWordWrap(True)
        auth_layout.addWidget(self.auth_status)
        self.token_input.textChanged.connect(
            lambda text: self.save_token_button.setEnabled(bool(text.strip()))
        )
        self.auth_group.hide()
        header_layout.addWidget(self.auth_group)
        layout.addWidget(header)

        self.channel_ledger = QFrame()
        self.channel_ledger.setObjectName("Ledger")
        self.channel_layout = QVBoxLayout(self.channel_ledger)
        self.channel_layout.setContentsMargins(0, 0, 0, 0)
        self.channel_layout.setSpacing(0)
        layout.addWidget(self.channel_ledger)

        self.set_channels(channels, selected, catalog_loaded=catalog_loaded)

    def set_channels(
        self,
        channels: list[AlertChannel],
        selected: set[str],
        *,
        catalog_loaded: bool = True,
    ) -> None:
        self._catalog_loaded = catalog_loaded
        while self.channel_layout.count():
            item = self.channel_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._rows.clear()
        self.channel_state_label = None
        for channel in channels:
            row = ChannelRow(channel, channel.key in selected)
            row.toggled.connect(lambda key, state, url=self.url: self.toggled.emit(url, key, state))
            self._rows[channel.key] = row
            self.channel_layout.addWidget(row)
        if not channels:
            state = QFrame()
            state.setObjectName("ChannelCatalogState")
            state.setMinimumHeight(64)
            state_layout = QHBoxLayout(state)
            state_layout.setContentsMargins(14, 10, 14, 10)
            state_layout.setSpacing(10)
            glyph = QLabel()
            glyph.setObjectName("ChannelCatalogIcon")
            glyph.setFixedSize(32, 32)
            glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
            glyph.setPixmap(
                make_line_icon(
                    "inbox" if catalog_loaded else "refresh",
                    COLORS["muted"],
                    20,
                ).pixmap(20, 20)
            )
            state_layout.addWidget(glyph)
            message = (
                "This server advertised no channels."
                if catalog_loaded
                else "Waiting for the server channel catalog…"
            )
            self.channel_state_label = QLabel(message)
            self.channel_state_label.setProperty("role", "muted")
            self.channel_state_label.setWordWrap(True)
            self.channel_state_label.setAccessibleName(message)
            state_layout.addWidget(self.channel_state_label, 1)
            self.channel_layout.addWidget(state)
        self.set_selected(selected)

    def set_selected(self, selected: set[str]) -> None:
        for key, row in self._rows.items():
            row.set_selected(key in selected)
        if not self._catalog_loaded:
            self.count_label.setText("Waiting")
        elif not self._rows:
            self.count_label.setText("No channels")
        else:
            visible_selected = len(set(self._rows).intersection(selected))
            self.count_label.setText(f"{visible_selected} selected")

    def _alias_committed(self) -> None:
        alias = self.alias_input.text().strip()
        self.server_name.set_full_text(alias or _server_label(self.url))
        self.renamed.emit(self.url, alias)

    def _finish_alias_edit(self) -> None:
        self._alias_committed()
        self.alias_button.setChecked(False)

    def _toggle_alias_editor(self, visible: bool) -> None:
        self.alias_editor.setVisible(visible)
        self.alias_button.setToolTip("Hide server name editor" if visible else "Edit server name")
        if visible:
            self.auth_button.setChecked(False)
            self.alias_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.alias_input.selectAll()

    def _toggle_auth_editor(self, visible: bool) -> None:
        self.auth_group.setVisible(visible)
        self.auth_button.setToolTip(
            "Hide authentication settings" if visible else "Set optional authentication token"
        )
        if visible:
            self.alias_button.setChecked(False)
            self.token_input.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _save_token(self) -> None:
        token = self.token_input.text().strip()
        if not token:
            return
        self.auth_save_requested.emit(self.url, token)
        # The view never becomes an in-memory secret viewer after hand-off.
        self.token_input.clear()
        self.auth_status.setText("Token save requested. Stored token contents remain hidden.")

    def _clear_token(self) -> None:
        self.token_input.clear()
        self.auth_clear_requested.emit(self.url)
        self.auth_status.setText("Stored-token removal requested.")

    def set_auth_status(self, stored: bool, secure_available: bool = True) -> None:
        if stored and secure_available:
            text = "A token is stored securely. Its contents are never displayed."
        elif stored:
            text = "A token is configured, but secure storage is unavailable on this system."
        elif secure_available:
            text = "No token is stored. Authentication is optional."
        else:
            text = "Secure storage is unavailable; no token is retained."
        self.token_input.clear()
        self.auth_status.setText(text)
        self.clear_token_button.setEnabled(stored)
        self.auth_button.setProperty("configured", stored)
        self.auth_button.setAccessibleDescription(text)
        _set_button_icon(
            self.auth_button,
            "key",
            icon_color=COLORS["primary"] if stored else COLORS["text_secondary"],
        )
        _refresh_style(self.auth_button)


class SoundRow(QFrame):
    """One severity's alert-sound selection with a preview button."""

    changed = Signal(str, str)  # severity, sound_id
    preview = Signal(str)  # sound_id

    def __init__(self, severity: str, sound_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.severity = severity
        self._sound_id = sound_id
        self.setObjectName("ChannelRow")
        self.setMinimumHeight(64)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        accent = QFrame()
        accent.setObjectName("HistoryAccent")
        accent.setProperty("severity", severity)
        accent.setFixedWidth(4)
        layout.addWidget(accent)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(14, 8, 12, 8)
        body_layout.setSpacing(10)
        code = QLabel(severity.upper())
        code.setObjectName("SeverityCode")
        code.setProperty("severity", severity)
        name = QLabel(severity.title())
        name.setObjectName("SectionTitle")
        labels = QVBoxLayout()
        labels.setSpacing(1)
        labels.addWidget(code)
        labels.addWidget(name)
        body_layout.addLayout(labels, 1)

        self.choose_button = QPushButton(display_name(sound_id))
        self.choose_button.setObjectName("SecondaryButton")
        self.choose_button.setMinimumWidth(120)
        self.choose_button.clicked.connect(self._open_menu)
        body_layout.addWidget(self.choose_button)

        self.play_button = _icon_button("play", f"Preview {severity} sound")
        _set_button_icon(self.play_button, "play", icon_color=COLORS["primary"])
        self.play_button.clicked.connect(lambda: self.preview.emit(self._sound_id))
        body_layout.addWidget(self.play_button)
        layout.addWidget(body, 1)

    def _open_menu(self) -> None:
        menu = QMenu(self)
        for sound_id, label in BUILTIN_SOUNDS.items():
            menu.addAction(label, lambda s=sound_id: self._select(s))
        menu.addSeparator()
        menu.addAction("None (silent)", lambda: self._select(NONE_ID))
        menu.addAction("Custom .wav…", self._choose_file)
        menu.exec(self.choose_button.mapToGlobal(self.choose_button.rect().bottomLeft()))

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an alert sound", "", "WAV audio (*.wav)"
        )
        if path:
            self._select(path)

    def _select(self, sound_id: str) -> None:
        self._sound_id = sound_id
        self.choose_button.setText(display_name(sound_id))
        self.changed.emit(self.severity, sound_id)
        if sound_id != NONE_ID:
            self.preview.emit(sound_id)


class DeliveryPolicyRow(QFrame):
    """A compact severity-to-delivery-mode mapping row."""

    changed = Signal(str, str)

    def __init__(self, severity: str, mode: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.severity = severity
        self.setObjectName("ChannelRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 12, 8)
        layout.setSpacing(12)
        label = QLabel(severity.title())
        label.setObjectName("SectionTitle")
        code = QLabel(severity.upper())
        code.setObjectName("SeverityCode")
        code.setProperty("severity", severity)
        labels = QVBoxLayout()
        labels.setSpacing(1)
        labels.addWidget(code)
        labels.addWidget(label)
        layout.addLayout(labels, 1)
        self.mode_combo = QComboBox()
        self.mode_combo.setAccessibleName(f"Delivery mode for {severity} alerts")
        self.mode_combo.setMinimumHeight(44)
        for key, text in DELIVERY_MODES.items():
            self.mode_combo.addItem(text, key)
        selected = self.mode_combo.findData(mode)
        self.mode_combo.setCurrentIndex(max(0, selected))
        self.mode_combo.currentIndexChanged.connect(
            lambda: self.changed.emit(self.severity, str(self.mode_combo.currentData()))
        )
        layout.addWidget(self.mode_combo)

    def mode(self) -> str:
        return str(self.mode_combo.currentData())

    def set_mode(self, mode: str) -> None:
        index = self.mode_combo.findData(mode)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)


class PolicyOverrideRow(QFrame):
    removed = Signal(str, str)

    def __init__(self, scope: str, value: str, mode: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scope = scope
        self.value = value
        self.mode = mode
        self.setObjectName("ChannelRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 8, 6)
        layout.setSpacing(8)
        scope_label = QLabel(scope.upper())
        scope_label.setProperty("role", "eyebrow")
        layout.addWidget(scope_label)
        value_label = ElidedLabel(value)
        value_label.setObjectName("SectionTitle")
        layout.addWidget(value_label, 1)
        mode_label = QLabel(DELIVERY_MODES.get(mode, mode))
        mode_label.setObjectName("CounterLabel")
        layout.addWidget(mode_label)
        remove = _icon_button(
            "trash",
            f"Remove {scope} delivery override for {value}",
            danger=True,
        )
        remove.clicked.connect(lambda: self.removed.emit(self.scope, self.value))
        layout.addWidget(remove)


class ManagementWindow(QMainWindow):
    servers_changed = Signal(object)
    sound_enabled_changed = Signal(bool)
    sound_changed = Signal(str, str)
    sound_preview_requested = Signal(str)
    subscriptions_changed = Signal(str, object)
    reconnect_requested = Signal(str)
    lifecycle_requested = Signal(str, str, str, object, str)
    alert_action_requested = Signal(str, str, object)
    history_filters_changed = Signal(object)
    history_export_requested = Signal(object)
    retention_changed = Signal(int)
    clear_history_requested = Signal()
    policy_changed = Signal(object)
    auth_save_requested = Signal(str, str)
    auth_clear_requested = Signal(str)
    launch_at_login_changed = Signal(bool)
    watchdog_threshold_changed = Signal(int)
    test_requested = Signal()
    quit_requested = Signal()
    hidden_to_tray = Signal()

    def __init__(self, config: AppConfig, *, tray_available: bool) -> None:
        super().__init__()
        self._tray_available = tray_available
        self._allow_close = False
        self._history_rows: list[AlertHistoryRow] = []
        self._history_records: list[object] = []
        self._history_page = 0
        self._history_dirty = True
        self._detail_dialogs: list[AlertDetailDialog] = []
        self._updating_policy = False
        self._policy_overrides: list[dict[str, str]] = []

        # Per-server model, keyed by URL.
        self._order: list[str] = [s.url for s in config.servers]
        self._subs: dict[str, set[str]] = {s.url: set(s.subscriptions) for s in config.servers}
        self._channels: dict[str, list[AlertChannel]] = {s.url: [] for s in config.servers}
        self._catalog_loaded: set[str] = set()
        self._states: dict[str, str] = {s.url: "connecting" for s in config.servers}
        self._aliases: dict[str, str] = {s.url: s.name for s in config.servers}
        self._auth_enabled: dict[str, bool] = {
            s.url: bool(getattr(s, "auth_enabled", False)) for s in config.servers
        }
        self._status_cards: dict[str, ServerStatusRow] = {}
        self._panels: dict[str, ServerPanel] = {}

        self._sound_enabled = config.sound_enabled
        self._sounds = dict(config.sounds)
        policy = getattr(config, "noise_policy", {})
        self._initial_policy = (
            policy.to_mapping() if hasattr(policy, "to_mapping") else dict(policy or {})
        )
        self._retention_days = int(getattr(config, "retention_days", 30))
        self._launch_at_login = bool(getattr(config, "launch_at_login", False))
        self._watchdog_seconds = int(getattr(config, "disconnect_warning_seconds", 30))

        self._history_render_timer = QTimer(self)
        self._history_render_timer.setSingleShot(True)
        self._history_render_timer.timeout.connect(self._render_history_if_needed)
        self._history_filter_timer = QTimer(self)
        self._history_filter_timer.setSingleShot(True)
        self._history_filter_timer.setInterval(HISTORY_FILTER_DEBOUNCE_MS)
        self._history_filter_timer.timeout.connect(self._apply_history_filter)

        self.setObjectName("RootWindow")
        self.setWindowTitle("SignalDesk — Alert center")
        self.setWindowIcon(make_app_icon())
        self.resize(600, 780)
        self.setMinimumSize(500, 640)
        self._build_ui()

        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._refresh_health_age)
        self._clock.start()

    # --- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("RootContent")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header_frame = QFrame()
        header_frame.setObjectName("CommandHeader")
        header = QHBoxLayout(header_frame)
        header.setContentsMargins(22, 16, 22, 16)
        header.setSpacing(11)
        brand_icon = QLabel()
        brand_icon.setPixmap(make_app_icon().pixmap(36, 36))
        brand_icon.setFixedSize(38, 38)
        brand_icon.setAccessibleName("SignalDesk logo")
        header.addWidget(brand_icon)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(1)
        title = QLabel("SignalDesk")
        title.setObjectName("BrandTitle")
        subtitle = QLabel("REAL-TIME ALERT CENTER / DESKTOP")
        subtitle.setObjectName("BrandSubtitle")
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)
        header.addLayout(brand_text)
        header.addStretch()

        status_block = QVBoxLayout()
        status_block.setSpacing(2)
        status_block.setAlignment(Qt.AlignmentFlag.AlignRight)
        status_caption = QLabel("SYSTEM STATUS")
        status_caption.setProperty("role", "headerEyebrow")
        status_caption.setAlignment(Qt.AlignmentFlag.AlignRight)
        status_block.addWidget(status_caption)
        status_row = QHBoxLayout()
        status_row.setSpacing(5)
        self.header_dot = StatusDot()
        status_row.addWidget(self.header_dot)
        self.header_status = QLabel("OFFLINE")
        self.header_status.setObjectName("HeaderStatus")
        self.header_status.setProperty("connectionState", "disconnected")
        status_row.addWidget(self.header_status)
        status_block.addLayout(status_row)
        header.addLayout(status_block)
        self.hide_button = _icon_button(
            "tray" if self._tray_available else "power",
            "Hide SignalDesk to the system tray" if self._tray_available else "Quit SignalDesk",
        )
        self.hide_button.setProperty("chrome", True)
        _set_button_icon(
            self.hide_button,
            "tray" if self._tray_available else "power",
            icon_color=COLORS["chrome_muted"],
        )
        self.hide_button.clicked.connect(self._hide_or_quit)
        header.addWidget(self.hide_button)
        outer.addWidget(header_frame)

        self.recovery_banner = QFrame()
        self.recovery_banner.setObjectName("RecoveryBanner")
        self.recovery_banner.setProperty("severity", "warning")
        recovery_layout = QHBoxLayout(self.recovery_banner)
        recovery_layout.setContentsMargins(22, 10, 14, 10)
        recovery_layout.setSpacing(10)
        recovery_text = QVBoxLayout()
        recovery_text.setSpacing(2)
        self.recovery_title = QLabel("Alert delivery gap detected")
        self.recovery_title.setObjectName("SectionTitle")
        self.recovery_detail = QLabel("")
        self.recovery_detail.setWordWrap(True)
        self.recovery_detail.setProperty("role", "muted")
        self.recovery_detail.setAccessibleName("Alert recovery status")
        recovery_text.addWidget(self.recovery_title)
        recovery_text.addWidget(self.recovery_detail)
        recovery_layout.addLayout(recovery_text, 1)
        dismiss_recovery = _icon_button("close", "Dismiss alert delivery gap notice")
        dismiss_recovery.clicked.connect(self.clear_recovery_banner)
        recovery_layout.addWidget(dismiss_recovery)
        self.recovery_banner.hide()
        outer.addWidget(self.recovery_banner)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(22, 14, 22, 0)
        body_layout.setSpacing(0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setAccessibleName("Alert management sections")
        self.tabs.addTab(self._build_overview_tab(), "OVERVIEW")
        self._history_tab = self._build_history_tab()
        self._history_tab_index = self.tabs.addTab(self._history_tab, "HISTORY")
        self.tabs.addTab(self._build_servers_tab(), "SERVERS")
        self.tabs.addTab(self._build_policies_tab(), "POLICIES")
        self.tabs.addTab(self._build_sounds_tab(), "SOUNDS")
        self.tabs.currentChanged.connect(self._tab_changed)
        body_layout.addWidget(self.tabs, 1)
        outer.addWidget(body, 1)

        self._rebuild_status_cards()
        self._rebuild_panels()
        self._recompute_header()

    def _build_overview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        servers_header = QHBoxLayout()
        servers_labels = QVBoxLayout()
        servers_labels.setSpacing(1)
        servers_kicker = QLabel("CONNECTIONS / FLEET")
        servers_kicker.setProperty("role", "eyebrow")
        servers_title = QLabel("Servers")
        servers_title.setObjectName("SectionTitle")
        servers_labels.addWidget(servers_kicker)
        servers_labels.addWidget(servers_title)
        servers_header.addLayout(servers_labels)
        servers_header.addStretch()
        self.test_button = _icon_button(
            "bell",
            "Preview an alert locally or through a connected server",
            primary=True,
        )
        self.test_button.clicked.connect(self.test_requested)
        servers_header.addWidget(self.test_button)
        layout.addLayout(servers_header)

        servers_scroll = QScrollArea()
        servers_scroll.setWidgetResizable(True)
        servers_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        servers_scroll.setMaximumHeight(220)
        servers_content = QWidget()
        servers_content_layout = QVBoxLayout(servers_content)
        servers_content_layout.setContentsMargins(0, 0, 2, 0)
        servers_content_layout.setSpacing(0)
        self.status_ledger = QFrame()
        self.status_ledger.setObjectName("Ledger")
        self.status_ledger.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.status_layout = QVBoxLayout(self.status_ledger)
        self.status_layout.setContentsMargins(0, 0, 0, 0)
        self.status_layout.setSpacing(0)
        servers_content_layout.addWidget(self.status_ledger)
        servers_content_layout.addStretch()
        servers_scroll.setWidget(servers_content)
        layout.addWidget(servers_scroll)

        inbox_card = QFrame()
        inbox_card.setObjectName("Card")
        inbox_layout = QHBoxLayout(inbox_card)
        inbox_layout.setContentsMargins(15, 12, 12, 12)
        inbox_layout.setSpacing(12)
        inbox_icon = QLabel()
        inbox_icon.setObjectName("InboxIconTile")
        inbox_icon.setFixedSize(44, 44)
        inbox_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inbox_icon.setPixmap(make_line_icon("inbox", COLORS["primary"], 24).pixmap(24, 24))
        inbox_layout.addWidget(inbox_icon)
        inbox_labels = QVBoxLayout()
        inbox_labels.setSpacing(2)
        self.overview_inbox_title = QLabel("Alert inbox")
        self.overview_inbox_title.setObjectName("SectionTitle")
        self.overview_inbox_summary = QLabel(_inbox_summary(0))
        self.overview_inbox_summary.setProperty("role", "muted")
        inbox_labels.addWidget(self.overview_inbox_title)
        inbox_labels.addWidget(self.overview_inbox_summary)
        inbox_layout.addLayout(inbox_labels, 1)
        self.overview_history_button = _icon_button(
            "chevron_right",
            "Open the searchable alert inbox",
        )
        self.overview_history_button.clicked.connect(self._open_history_tab)
        inbox_layout.addWidget(self.overview_history_button)
        layout.addWidget(inbox_card)
        layout.addStretch()
        return tab

    def _build_history_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        labels = QVBoxLayout()
        labels.setSpacing(1)
        kicker = QLabel("DURABLE INBOX / ALL ALERTS")
        kicker.setProperty("role", "eyebrow")
        title = QLabel("Alert history")
        title.setObjectName("SectionTitle")
        labels.addWidget(kicker)
        labels.addWidget(title)
        heading.addLayout(labels)
        heading.addStretch()
        self.history_count = QLabel("0 received")
        self.history_count.setObjectName("CounterLabel")
        heading.addWidget(self.history_count)
        layout.addLayout(heading)

        filters = QFrame()
        filters.setObjectName("FilterBar")
        filter_layout = QVBoxLayout(filters)
        filter_layout.setContentsMargins(12, 10, 12, 10)
        filter_layout.setSpacing(8)
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("Search title, message, source, channel, or server")
        self.history_search.setClearButtonEnabled(True)
        self.history_search.setAccessibleName("Search alert history")
        self.history_search.textChanged.connect(self._history_filter_changed)
        search_row.addWidget(self.history_search, 1)
        self.history_filter_button = _icon_button(
            "filter",
            "Show severity, server, and channel filters",
            checkable=True,
        )
        search_row.addWidget(self.history_filter_button)
        self.clear_filters_button = _icon_button(
            "filter_clear",
            "Clear all alert history filters",
        )
        self.clear_filters_button.setDisabled(True)
        self.clear_filters_button.clicked.connect(self._clear_history_filters)
        search_row.addWidget(self.clear_filters_button)
        self.export_history_button = _icon_button(
            "export",
            "Export the filtered alert history",
        )
        self.export_history_button.clicked.connect(
            lambda: self.history_export_requested.emit(self.history_filters())
        )
        search_row.addWidget(self.export_history_button)
        self.history_maintenance_button = _icon_button(
            "settings",
            "Show history retention and storage controls",
            checkable=True,
        )
        search_row.addWidget(self.history_maintenance_button)
        filter_layout.addLayout(search_row)

        self.history_filter_panel = QWidget()
        combo_row = QHBoxLayout(self.history_filter_panel)
        combo_row.setContentsMargins(0, 0, 0, 0)
        combo_row.setSpacing(8)
        self.severity_filter = self._history_combo(
            "Filter alert history by severity",
            [("All severities", "")] + [(severity.title(), severity) for severity in SEVERITIES],
        )
        self.server_filter = self._history_combo(
            "Filter alert history by server", [("All servers", "")]
        )
        self.channel_filter = self._history_combo(
            "Filter alert history by channel", [("All channels", "")]
        )
        for combo in (
            self.severity_filter,
            self.server_filter,
            self.channel_filter,
        ):
            combo.currentIndexChanged.connect(self._history_filter_changed)
            combo_row.addWidget(combo, 1)
        self.history_filter_panel.hide()
        self.history_filter_button.toggled.connect(self._toggle_history_filter_panel)
        filter_layout.addWidget(self.history_filter_panel)

        self.history_maintenance_panel = QFrame()
        self.history_maintenance_panel.setObjectName("InlineEditor")
        controls = QHBoxLayout(self.history_maintenance_panel)
        controls.setContentsMargins(10, 8, 8, 8)
        controls.setSpacing(8)
        retention_label = QLabel("Retention")
        retention_label.setProperty("role", "eyebrow")
        controls.addWidget(retention_label)
        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(1, 3650)
        self.retention_spin.setSuffix(" days")
        self.retention_spin.setValue(self._retention_days)
        self.retention_spin.setAccessibleName("Alert history retention in days")
        self.retention_spin.setMinimumHeight(44)
        self.retention_spin.valueChanged.connect(self.retention_changed)
        controls.addWidget(self.retention_spin)
        controls.addStretch()
        self.clear_history_button = _icon_button(
            "trash",
            "Clear all stored alert history",
            danger=True,
        )
        self.clear_history_button.clicked.connect(self._confirm_clear_history)
        controls.addWidget(self.clear_history_button)
        self.history_maintenance_panel.hide()
        self.history_maintenance_button.toggled.connect(self._toggle_history_maintenance_panel)
        filter_layout.addWidget(self.history_maintenance_panel)
        layout.addWidget(filters)

        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        history_content = QWidget()
        history_content_layout = QVBoxLayout(history_content)
        history_content_layout.setContentsMargins(0, 0, 2, 0)
        history_content_layout.setSpacing(0)
        self.history_ledger = QFrame()
        self.history_ledger.setObjectName("Ledger")
        self.history_ledger.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.history_layout = QVBoxLayout(self.history_ledger)
        self.history_layout.setContentsMargins(0, 0, 0, 0)
        self.history_layout.setSpacing(0)
        self.empty_history = QFrame()
        empty_layout = QVBoxLayout(self.empty_history)
        empty_layout.setContentsMargins(18, 24, 18, 26)
        empty_layout.setSpacing(4)
        empty_code = QLabel("00")
        empty_code.setObjectName("EmptyCode")
        self.empty_history_title = QLabel("No events recorded")
        self.empty_history_title.setObjectName("SectionTitle")
        self.empty_history_detail = QLabel(
            "Incoming events are persisted here, including alerts received while this window is hidden."
        )
        self.empty_history_detail.setProperty("role", "muted")
        self.empty_history_detail.setWordWrap(True)
        empty_layout.addWidget(empty_code)
        empty_layout.addWidget(self.empty_history_title)
        empty_layout.addWidget(self.empty_history_detail)
        self.history_layout.addWidget(self.empty_history)
        history_content_layout.addWidget(self.history_ledger)

        self.history_page_bar = QFrame()
        self.history_page_bar.setObjectName("HistoryPager")
        page_layout = QHBoxLayout(self.history_page_bar)
        page_layout.setContentsMargins(10, 8, 8, 8)
        page_layout.setSpacing(8)
        self.history_previous_button = _icon_button(
            "chevron_left",
            "Show newer alert history results",
        )
        self.history_previous_button.clicked.connect(lambda: self._change_history_page(-1))
        page_layout.addWidget(self.history_previous_button)
        self.history_page_label = QLabel("")
        self.history_page_label.setProperty("role", "muted")
        self.history_page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.history_page_label.setAccessibleName("Visible alert history result range")
        page_layout.addWidget(self.history_page_label, 1)
        self.history_next_button = _icon_button(
            "chevron_right",
            "Show older alert history results",
        )
        self.history_next_button.clicked.connect(lambda: self._change_history_page(1))
        page_layout.addWidget(self.history_next_button)
        self.history_page_bar.hide()
        history_content_layout.addStretch()
        self.history_scroll.setWidget(history_content)
        layout.addWidget(self.history_scroll, 1)
        layout.addWidget(self.history_page_bar)
        return tab

    def _history_combo(self, accessible_name: str, items: list[tuple[str, str]]) -> QComboBox:
        combo = QComboBox()
        combo.setAccessibleName(accessible_name)
        combo.setMinimumHeight(44)
        for label, value in items:
            combo.addItem(label, value)
        return combo

    def _build_servers_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        panels_header = QHBoxLayout()
        panels_labels = QVBoxLayout()
        panels_labels.setSpacing(1)
        panels_kicker = QLabel("ROUTING MATRIX / PER SERVER")
        panels_kicker.setProperty("role", "eyebrow")
        panels_title = QLabel("Servers & subscriptions")
        panels_title.setObjectName("SectionTitle")
        panels_labels.addWidget(panels_kicker)
        panels_labels.addWidget(panels_title)
        panels_header.addLayout(panels_labels)
        panels_header.addStretch()
        self.server_count = QLabel("")
        self.server_count.setObjectName("CounterLabel")
        panels_header.addWidget(self.server_count)
        self.add_server_button = _icon_button(
            "plus",
            "Show the add-server form",
            checkable=True,
        )
        panels_header.addWidget(self.add_server_button)
        layout.addLayout(panels_header)

        self.add_server_panel = QFrame()
        self.add_server_panel.setObjectName("EndpointCard")
        add_outer = QVBoxLayout(self.add_server_panel)
        add_outer.setContentsMargins(0, 0, 0, 0)
        add_outer.setSpacing(0)
        add_accent = QFrame()
        add_accent.setObjectName("PanelAccent")
        add_outer.addWidget(add_accent)
        add_body = QWidget()
        add_layout = QVBoxLayout(add_body)
        add_layout.setContentsMargins(15, 12, 15, 14)
        add_layout.setSpacing(7)
        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self.endpoint_input = QLineEdit()
        self.endpoint_input.setPlaceholderText("http://host:port")
        self.endpoint_input.setClearButtonEnabled(True)
        self.endpoint_input.setAccessibleName("New Socket.IO server URL")
        self.endpoint_input.returnPressed.connect(self._add_server)
        add_row.addWidget(self.endpoint_input, 1)
        self.add_button = _icon_button(
            "check",
            "Connect the server",
            primary=True,
        )
        self.add_button.clicked.connect(self._add_server)
        add_row.addWidget(self.add_button)
        add_layout.addLayout(add_row)
        self.endpoint_error = QLabel("")
        self.endpoint_error.setObjectName("EndpointError")
        self.endpoint_error.setWordWrap(True)
        self.endpoint_error.hide()
        add_layout.addWidget(self.endpoint_error)
        add_outer.addWidget(add_body)
        self.add_server_panel.hide()
        self.add_server_button.toggled.connect(self._toggle_add_server_panel)
        layout.addWidget(self.add_server_panel)

        panels_scroll = QScrollArea()
        panels_scroll.setWidgetResizable(True)
        panels_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        panels_content = QWidget()
        panels_content_layout = QVBoxLayout(panels_content)
        panels_content_layout.setContentsMargins(0, 0, 2, 0)
        panels_content_layout.setSpacing(12)
        self.panels_container = QVBoxLayout()
        self.panels_container.setSpacing(12)
        panels_content_layout.addLayout(self.panels_container)
        panels_content_layout.addStretch()
        panels_scroll.setWidget(panels_content)
        layout.addWidget(panels_scroll, 1)
        return tab

    def _build_policies_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 16, 0, 0)
        outer.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 2, 16)
        layout.setSpacing(14)

        heading = QHBoxLayout()
        heading.setSpacing(2)
        heading_text = QVBoxLayout()
        heading_text.setSpacing(2)
        kicker = QLabel("DELIVERY POLICY / NOISE CONTROL")
        kicker.setProperty("role", "eyebrow")
        title = QLabel("How alerts interrupt you")
        title.setObjectName("SectionTitle")
        helper = QLabel(
            "Every alert remains in history. These policies only control toast and sound delivery."
        )
        helper.setProperty("role", "muted")
        helper.setWordWrap(True)
        heading_text.addWidget(kicker)
        heading_text.addWidget(title)
        heading_text.addWidget(helper)
        heading.addLayout(heading_text, 1)
        layout.addLayout(heading)

        severity_ledger = QFrame()
        severity_ledger.setObjectName("Ledger")
        severity_layout = QVBoxLayout(severity_ledger)
        severity_layout.setContentsMargins(0, 0, 0, 0)
        severity_layout.setSpacing(0)
        initial_modes = self._initial_policy.get("severity_modes", {})
        self.delivery_rows: dict[str, DeliveryPolicyRow] = {}
        for severity in reversed(SEVERITIES):
            row = DeliveryPolicyRow(
                severity,
                str(initial_modes.get(severity, "toast_sound")),
            )
            row.changed.connect(lambda _severity, _mode: self._emit_policy_changed())
            severity_layout.addWidget(row)
            self.delivery_rows[severity] = row
        layout.addWidget(severity_ledger)

        self.advanced_policy = QWidget()
        advanced_layout = QVBoxLayout(self.advanced_policy)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.setSpacing(12)

        quiet = QGroupBox("Quiet hours")
        quiet_layout = QVBoxLayout(quiet)
        quiet_layout.setContentsMargins(14, 14, 14, 14)
        quiet_layout.setSpacing(8)
        self.quiet_enabled = QCheckBox("Enable quiet hours")
        self.quiet_enabled.setAccessibleName("Enable quiet hours for alert interruptions")
        self.quiet_enabled.setChecked(bool(self._initial_policy.get("quiet_enabled", False)))
        quiet_layout.addWidget(self.quiet_enabled)
        time_row = QHBoxLayout()
        time_row.setSpacing(8)
        time_row.addWidget(QLabel("From"))
        self.quiet_start = QTimeEdit()
        self.quiet_start.setDisplayFormat("HH:mm")
        self.quiet_start.setAccessibleName("Quiet hours start time")
        self.quiet_start.setTime(
            QTime.fromString(str(self._initial_policy.get("quiet_start", "22:00")), "HH:mm")
        )
        time_row.addWidget(self.quiet_start)
        time_row.addWidget(QLabel("until"))
        self.quiet_end = QTimeEdit()
        self.quiet_end.setDisplayFormat("HH:mm")
        self.quiet_end.setAccessibleName("Quiet hours end time")
        self.quiet_end.setTime(
            QTime.fromString(str(self._initial_policy.get("quiet_end", "07:00")), "HH:mm")
        )
        time_row.addWidget(self.quiet_end)
        time_row.addStretch()
        quiet_layout.addLayout(time_row)
        self.critical_bypass = QCheckBox("Critical alerts bypass quiet hours")
        self.critical_bypass.setAccessibleName("Allow critical alerts during quiet hours")
        self.critical_bypass.setChecked(bool(self._initial_policy.get("critical_bypass", True)))
        quiet_layout.addWidget(self.critical_bypass)
        advanced_layout.addWidget(quiet)

        repeats = QGroupBox("Repeated alerts")
        repeat_layout = QVBoxLayout(repeats)
        repeat_layout.setContentsMargins(14, 14, 14, 14)
        repeat_layout.setSpacing(8)
        self.group_repeats = QCheckBox("Group matching repeats during the cooldown")
        self.group_repeats.setChecked(bool(self._initial_policy.get("group_repeats", True)))
        self.group_repeats.setAccessibleName("Group repeated matching alerts")
        repeat_layout.addWidget(self.group_repeats)
        cooldown_row = QHBoxLayout()
        cooldown_row.addWidget(QLabel("Repeat cooldown"))
        self.cooldown_spin = QSpinBox()
        self.cooldown_spin.setRange(0, 3600)
        self.cooldown_spin.setSuffix(" seconds")
        self.cooldown_spin.setValue(int(self._initial_policy.get("cooldown_seconds", 30) or 0))
        self.cooldown_spin.setAccessibleName("Repeated alert cooldown in seconds")
        self.cooldown_spin.setMinimumHeight(44)
        cooldown_row.addWidget(self.cooldown_spin)
        cooldown_row.addStretch()
        repeat_layout.addLayout(cooldown_row)
        repeat_note = QLabel(
            "During the cooldown, matching alerts are counted in history without another interruption."
        )
        repeat_note.setProperty("role", "muted")
        repeat_note.setWordWrap(True)
        repeat_layout.addWidget(repeat_note)
        advanced_layout.addWidget(repeats)

        overrides = QGroupBox("Server and channel overrides")
        overrides_layout = QVBoxLayout(overrides)
        overrides_layout.setContentsMargins(14, 14, 14, 14)
        overrides_layout.setSpacing(8)
        override_helper = QLabel(
            "An exact server override wins over a channel override, which wins over severity."
        )
        override_helper.setProperty("role", "muted")
        override_helper.setWordWrap(True)
        overrides_layout.addWidget(override_helper)
        override_form = QHBoxLayout()
        override_form.setSpacing(8)
        self.override_scope = QComboBox()
        self.override_scope.addItem("Server", "server")
        self.override_scope.addItem("Channel", "channel")
        self.override_scope.setAccessibleName("Delivery override scope")
        self.override_scope.currentIndexChanged.connect(self._refresh_override_values)
        override_form.addWidget(self.override_scope)
        self.override_value = QComboBox()
        self.override_value.setEditable(True)
        self.override_value.setAccessibleName("Server or channel for delivery override")
        override_form.addWidget(self.override_value, 1)
        self.override_mode = QComboBox()
        self.override_mode.setAccessibleName("Delivery mode for override")
        for key, label in DELIVERY_MODES.items():
            self.override_mode.addItem(label, key)
        override_form.addWidget(self.override_mode)
        add_override = _icon_button(
            "plus",
            "Add server or channel delivery override",
            primary=True,
        )
        add_override.clicked.connect(self._add_policy_override)
        override_form.addWidget(add_override)
        overrides_layout.addLayout(override_form)
        self.override_ledger = QFrame()
        self.override_ledger.setObjectName("Ledger")
        self.override_layout = QVBoxLayout(self.override_ledger)
        self.override_layout.setContentsMargins(0, 0, 0, 0)
        self.override_layout.setSpacing(0)
        overrides_layout.addWidget(self.override_ledger)
        advanced_layout.addWidget(overrides)

        reliability = QGroupBox("Always-on reliability")
        reliability_layout = QVBoxLayout(reliability)
        reliability_layout.setContentsMargins(14, 14, 14, 14)
        reliability_layout.setSpacing(8)
        self.launch_toggle = QCheckBox("Launch SignalDesk when I sign in")
        self.launch_toggle.setChecked(self._launch_at_login)
        self.launch_toggle.setAccessibleName("Launch SignalDesk at login")
        self.launch_toggle.toggled.connect(self.launch_at_login_changed)
        reliability_layout.addWidget(self.launch_toggle)
        watchdog_row = QHBoxLayout()
        watchdog_row.addWidget(QLabel("Warn after a server is offline for"))
        self.watchdog_spin = QSpinBox()
        self.watchdog_spin.setRange(10, 3600)
        self.watchdog_spin.setSuffix(" seconds")
        self.watchdog_spin.setValue(self._watchdog_seconds)
        self.watchdog_spin.setAccessibleName("Disconnected server warning threshold")
        self.watchdog_spin.setMinimumHeight(44)
        self.watchdog_spin.valueChanged.connect(self.watchdog_threshold_changed)
        watchdog_row.addWidget(self.watchdog_spin)
        watchdog_row.addStretch()
        reliability_layout.addLayout(watchdog_row)
        reliability_note = QLabel(
            "Offline time and the last successful connection remain visible in each server row."
        )
        reliability_note.setProperty("role", "muted")
        reliability_note.setWordWrap(True)
        reliability_layout.addWidget(reliability_note)
        advanced_layout.addWidget(reliability)

        self.advanced_policy.hide()
        layout.addWidget(self.advanced_policy)
        self.advanced_policy_button = QPushButton("More settings...")
        self.advanced_policy_button.setObjectName("DisclosureButton")
        self.advanced_policy_button.setCheckable(True)
        self.advanced_policy_button.setMinimumHeight(44)
        self.advanced_policy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.advanced_policy_button.setToolTip(
            "Show advanced noise-control and reliability settings"
        )
        self.advanced_policy_button.setAccessibleName(
            "Show advanced noise-control and reliability settings"
        )
        self.advanced_policy_button.toggled.connect(self._toggle_advanced_policy)
        layout.addWidget(
            self.advanced_policy_button,
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        layout.addStretch()

        for control in (
            self.quiet_enabled,
            self.quiet_start,
            self.quiet_end,
            self.critical_bypass,
            self.group_repeats,
            self.cooldown_spin,
        ):
            signal = (
                control.timeChanged
                if isinstance(control, QTimeEdit)
                else control.valueChanged
                if isinstance(control, QSpinBox)
                else control.toggled
            )
            signal.connect(self._emit_policy_changed)

        server_modes = self._initial_policy.get("server_modes", {})
        channel_modes = self._initial_policy.get("channel_modes", {})
        if isinstance(server_modes, Mapping):
            self._policy_overrides.extend(
                {"scope": "server", "value": str(key), "mode": str(mode)}
                for key, mode in server_modes.items()
                if str(mode) in DELIVERY_MODES
            )
        if isinstance(channel_modes, Mapping):
            self._policy_overrides.extend(
                {"scope": "channel", "value": str(key), "mode": str(mode)}
                for key, mode in channel_modes.items()
                if str(mode) in DELIVERY_MODES
            )
        self._refresh_override_values()
        self._rebuild_policy_overrides()
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        return tab

    def _build_sounds_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        master_card = QFrame()
        master_card.setObjectName("EndpointCard")
        master_outer = QVBoxLayout(master_card)
        master_outer.setContentsMargins(0, 0, 0, 0)
        master_outer.setSpacing(0)
        master_accent = QFrame()
        master_accent.setObjectName("PanelAccent")
        master_outer.addWidget(master_accent)
        master_body = QWidget()
        master_layout = QHBoxLayout(master_body)
        master_layout.setContentsMargins(15, 12, 15, 12)
        master_layout.setSpacing(10)
        master_labels = QVBoxLayout()
        master_labels.setSpacing(1)
        master_kicker = QLabel("AUDIO / NOTIFICATIONS")
        master_kicker.setProperty("role", "eyebrow")
        master_title = QLabel("Alert sounds")
        master_title.setObjectName("SectionTitle")
        master_labels.addWidget(master_kicker)
        master_labels.addWidget(master_title)
        master_layout.addLayout(master_labels, 1)
        self.sound_toggle = _icon_button(
            "volume" if self._sound_enabled else "volume_off",
            "Mute alert sounds" if self._sound_enabled else "Enable alert sounds",
            checkable=True,
        )
        self.sound_toggle.setChecked(self._sound_enabled)
        self.sound_toggle.toggled.connect(self._sound_enabled_toggled)
        master_layout.addWidget(self.sound_toggle, 0, Qt.AlignmentFlag.AlignVCenter)
        master_outer.addWidget(master_body)
        layout.addWidget(master_card)

        rows_header = QHBoxLayout()
        rows_labels = QVBoxLayout()
        rows_labels.setSpacing(1)
        rows_kicker = QLabel("SOUND MATRIX / PER SEVERITY")
        rows_kicker.setProperty("role", "eyebrow")
        rows_title = QLabel("Sound per severity")
        rows_title.setObjectName("SectionTitle")
        rows_labels.addWidget(rows_kicker)
        rows_labels.addWidget(rows_title)
        rows_header.addLayout(rows_labels)
        rows_header.addStretch()
        layout.addLayout(rows_header)

        ledger = QFrame()
        ledger.setObjectName("Ledger")
        ledger.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        ledger_layout = QVBoxLayout(ledger)
        ledger_layout.setContentsMargins(0, 0, 0, 0)
        ledger_layout.setSpacing(0)
        # Most-severe first, matching the activity log's accent ordering.
        for severity in reversed(SEVERITIES):
            row = SoundRow(severity, self._sounds.get(severity, NONE_ID))
            row.changed.connect(self._sound_changed)
            row.preview.connect(self.sound_preview_requested)
            ledger_layout.addWidget(row)
        layout.addWidget(ledger)
        layout.addStretch()
        return tab

    def _sound_enabled_toggled(self, enabled: bool) -> None:
        self._sound_enabled = enabled
        label = "Mute alert sounds" if enabled else "Enable alert sounds"
        self.sound_toggle.setToolTip(label)
        self.sound_toggle.setAccessibleName(label)
        _set_button_icon(
            self.sound_toggle,
            "volume" if enabled else "volume_off",
            icon_color=COLORS["primary"] if enabled else COLORS["muted"],
        )
        self.sound_enabled_changed.emit(enabled)

    def _sound_changed(self, severity: str, sound_id: str) -> None:
        self._sounds[severity] = sound_id
        self.sound_changed.emit(severity, sound_id)

    # --- history, detail, and policy surfaces --------------------------

    def _open_history_tab(self) -> None:
        self.tabs.setCurrentIndex(self._history_tab_index)
        self.history_search.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def open_history(self) -> None:
        """Bring the persistent inbox forward from an aggregated notification."""
        self.show_and_activate()
        self._open_history_tab()

    def _tab_changed(self, index: int) -> None:
        if index == self._history_tab_index:
            self._schedule_history_render()

    def _schedule_history_render(self, delay_ms: int = 0) -> None:
        if self.tabs.currentIndex() != self._history_tab_index:
            return
        self._history_render_timer.start(max(0, int(delay_ms)))

    def _render_history_if_needed(self) -> None:
        if self.tabs.currentIndex() == self._history_tab_index and self._history_dirty:
            self._render_history_page()

    def history_filters(self) -> dict[str, str]:
        return {
            "search": self.history_search.text().strip(),
            "severity": str(self.severity_filter.currentData() or ""),
            "server": str(self.server_filter.currentData() or ""),
            "channel": str(self.channel_filter.currentData() or ""),
        }

    def _history_filter_changed(self, _value: object = None) -> None:
        del _value
        active = any(self.history_filters().values())
        self.history_filter_button.setProperty("configured", active)
        _refresh_style(self.history_filter_button)
        self.clear_filters_button.setEnabled(active)
        self._history_page = 0
        self._history_dirty = True
        self._history_filter_timer.start()

    def _apply_history_filter(self) -> None:
        self._render_history_if_needed()
        self.history_filters_changed.emit(self.history_filters())

    def _toggle_history_filter_panel(self, visible: bool) -> None:
        self.history_filter_panel.setVisible(visible)
        label = (
            "Hide severity, server, and channel filters"
            if visible
            else "Show severity, server, and channel filters"
        )
        self.history_filter_button.setToolTip(label)
        self.history_filter_button.setAccessibleName(label)
        if visible:
            self.history_maintenance_button.setChecked(False)
            self.severity_filter.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _toggle_history_maintenance_panel(self, visible: bool) -> None:
        self.history_maintenance_panel.setVisible(visible)
        label = (
            "Hide history retention and storage controls"
            if visible
            else "Show history retention and storage controls"
        )
        self.history_maintenance_button.setToolTip(label)
        self.history_maintenance_button.setAccessibleName(label)
        if visible:
            self.history_filter_button.setChecked(False)
            self.retention_spin.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _clear_history_filters(self) -> None:
        previous = self.history_search.blockSignals(True)
        self.history_search.clear()
        self.history_search.blockSignals(previous)
        for combo in (
            self.severity_filter,
            self.server_filter,
            self.channel_filter,
        ):
            previous = combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(previous)
        self._history_filter_timer.stop()
        self._history_page = 0
        self._history_dirty = True
        self._render_history_if_needed()
        self.history_filters_changed.emit(self.history_filters())
        self.history_filter_button.setProperty("configured", False)
        _refresh_style(self.history_filter_button)
        self.clear_filters_button.setDisabled(True)

    def _set_filter_options(self, combo: QComboBox, values: set[str], all_label: str) -> None:
        selected = str(combo.currentData() or "")
        previous = combo.blockSignals(True)
        combo.clear()
        combo.addItem(all_label, "")
        for value in sorted(item for item in values if item):
            combo.addItem(value, value)
        index = combo.findData(selected)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(previous)

    def _matching_history_records(self) -> list[object]:
        filters = self.history_filters()
        query = filters["search"].casefold()
        matches: list[object] = []
        for record in self._history_records:
            alert = _record_alert(record)
            origin = _record_origin(record)
            severity = _string_value(alert.severity, "info").lower()
            if filters["severity"] and severity != filters["severity"]:
                continue
            if filters["server"] and origin != filters["server"]:
                continue
            if filters["channel"] and alert.channel != filters["channel"]:
                continue
            if query:
                search_text = " ".join(
                    (
                        alert.id,
                        alert.title,
                        alert.message,
                        alert.source,
                        alert.channel,
                        origin,
                    )
                ).casefold()
                if query not in search_text:
                    continue
            matches.append(record)
        return matches

    def _render_history_page(self) -> None:
        for row in self._history_rows:
            self.history_layout.removeWidget(row)
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self._history_rows.clear()
        matches = self._matching_history_records()
        active = any(self.history_filters().values())
        page_count = max(1, (len(matches) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
        self._history_page = min(self._history_page, page_count - 1)
        start = self._history_page * HISTORY_PAGE_SIZE
        end = min(start + HISTORY_PAGE_SIZE, len(matches))
        visible_records = matches[start:end]
        if visible_records:
            self.empty_history.hide()
            for record in visible_records:
                row = AlertHistoryRow(record)
                row.activated.connect(self.open_alert_detail)
                self._history_rows.append(row)
                self.history_layout.insertWidget(self.history_layout.count() - 1, row)
                row.show()
        else:
            self.empty_history.show()
            if active:
                self.empty_history_title.setText("No alerts match these filters")
                self.empty_history_detail.setText(
                    "Try clearing a filter or searching for a different title, message, source, "
                    "channel, or server."
                )
            else:
                self.empty_history_title.setText("No events recorded")
                self.empty_history_detail.setText(
                    "Incoming events are persisted here, including alerts received while this "
                    "window is hidden."
                )
        total = len(self._history_records)
        self.history_count.setText(f"{len(matches)} of {total}" if active else f"{total} received")
        self.overview_inbox_summary.setText(_inbox_summary(total))
        self.export_history_button.setDisabled(not matches)
        if len(matches) > HISTORY_PAGE_SIZE:
            self.history_page_label.setText(f"{start + 1}–{end} of {len(matches)}")
            self.history_previous_button.setDisabled(self._history_page == 0)
            self.history_next_button.setDisabled(end >= len(matches))
            self.history_page_bar.show()
        else:
            self.history_page_bar.hide()
        self.history_scroll.verticalScrollBar().setValue(0)
        self._history_dirty = False

    def _change_history_page(self, delta: int) -> None:
        self._history_page = max(0, self._history_page + int(delta))
        self._history_dirty = True
        self._render_history_if_needed()

    def set_history_records(self, records: list[object] | tuple[object, ...]) -> None:
        """Replace the visible inbox from controller-owned persistent records."""
        self._history_records = list(records)
        servers = {_record_origin(record) for record in self._history_records}
        channels = {_record_alert(record).channel for record in self._history_records}
        self._set_filter_options(self.server_filter, servers, "All servers")
        self._set_filter_options(self.channel_filter, channels, "All channels")
        total = len(self._history_records)
        if not any(self.history_filters().values()):
            self.history_count.setText(f"{total} received")
        self.overview_inbox_summary.setText(_inbox_summary(total))
        self.export_history_button.setDisabled(not self._history_records)
        self._history_dirty = True
        self._schedule_history_render()

    def replace_history(self, records: list[object] | tuple[object, ...]) -> None:
        """Compatibility alias for controllers that call a replace operation."""
        self.set_history_records(records)

    def open_alert_detail(self, record: object) -> AlertDetailDialog:
        dialog = AlertDetailDialog(record, self)
        dialog.lifecycle_requested.connect(self.lifecycle_requested)
        dialog.action_requested.connect(self.alert_action_requested)
        dialog.finished.connect(lambda _result, item=dialog: self._forget_dialog(item))
        self._detail_dialogs.append(dialog)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def show_alert_detail(self, server_url: str, alert_id: str) -> bool:
        for record in self._history_records:
            alert = _record_alert(record)
            if alert.id == alert_id and (not server_url or _record_origin(record) == server_url):
                self.open_alert_detail(record)
                return True
        return False

    def _forget_dialog(self, dialog: AlertDetailDialog) -> None:
        if dialog in self._detail_dialogs:
            self._detail_dialogs.remove(dialog)

    def _confirm_clear_history(self) -> None:
        result = QMessageBox.question(
            self,
            "Clear alert history?",
            "This requests permanent removal of every stored alert. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if result == QMessageBox.StandardButton.Yes:
            self.clear_history_requested.emit()

    def _toggle_advanced_policy(self, visible: bool) -> None:
        self.advanced_policy.setVisible(visible)
        label = (
            "Hide advanced noise-control and reliability settings"
            if visible
            else "Show advanced noise-control and reliability settings"
        )
        self.advanced_policy_button.setText("Fewer settings..." if visible else "More settings...")
        self.advanced_policy_button.setToolTip(label)
        self.advanced_policy_button.setAccessibleName(label)

    def policy_mapping(self) -> dict[str, object]:
        server_modes: dict[str, str] = {}
        channel_modes: dict[str, str] = {}
        for override in self._policy_overrides:
            target = server_modes if override["scope"] == "server" else channel_modes
            target[override["value"]] = override["mode"]
        return {
            "severity_modes": {
                severity: row.mode() for severity, row in self.delivery_rows.items()
            },
            "server_modes": server_modes,
            "channel_modes": channel_modes,
            "quiet_enabled": self.quiet_enabled.isChecked(),
            "quiet_start": self.quiet_start.time().toString("HH:mm"),
            "quiet_end": self.quiet_end.time().toString("HH:mm"),
            "critical_bypass": self.critical_bypass.isChecked(),
            "cooldown_seconds": self.cooldown_spin.value() if self.group_repeats.isChecked() else 0,
            "group_repeats": self.group_repeats.isChecked(),
        }

    def _emit_policy_changed(self, _value: object = None) -> None:
        del _value
        if not self._updating_policy:
            self.policy_changed.emit(self.policy_mapping())

    def set_policy_mapping(self, policy: Mapping[str, object]) -> None:
        self._updating_policy = True
        try:
            modes = policy.get("severity_modes", {})
            if isinstance(modes, Mapping):
                for severity, row in self.delivery_rows.items():
                    row.set_mode(str(modes.get(severity, row.mode())))
            self.quiet_enabled.setChecked(bool(policy.get("quiet_enabled", False)))
            start = QTime.fromString(str(policy.get("quiet_start", "22:00")), "HH:mm")
            end = QTime.fromString(str(policy.get("quiet_end", "07:00")), "HH:mm")
            if start.isValid():
                self.quiet_start.setTime(start)
            if end.isValid():
                self.quiet_end.setTime(end)
            self.critical_bypass.setChecked(bool(policy.get("critical_bypass", True)))
            cooldown = int(policy.get("cooldown_seconds", 30) or 0)
            self.cooldown_spin.setValue(max(0, min(cooldown, 3600)))
            self.group_repeats.setChecked(bool(policy.get("group_repeats", cooldown > 0)))
            self._policy_overrides.clear()
            for scope, key in (("server", "server_modes"), ("channel", "channel_modes")):
                values = policy.get(key, {})
                if isinstance(values, Mapping):
                    self._policy_overrides.extend(
                        {"scope": scope, "value": str(value), "mode": str(mode)}
                        for value, mode in values.items()
                        if str(mode) in DELIVERY_MODES
                    )
            self._rebuild_policy_overrides()
        finally:
            self._updating_policy = False

    def _refresh_override_values(self, _index: int = -1) -> None:
        del _index
        scope = str(self.override_scope.currentData())
        current = self.override_value.currentText().strip()
        previous = self.override_value.blockSignals(True)
        self.override_value.clear()
        if scope == "server":
            values = list(self._order)
        else:
            values = sorted(
                {channel.key for channels in self._channels.values() for channel in channels}
            )
        self.override_value.addItems(values)
        if current and self.override_value.findText(current) < 0:
            self.override_value.addItem(current)
        if current:
            self.override_value.setCurrentText(current)
        self.override_value.blockSignals(previous)

    def _add_policy_override(self) -> None:
        scope = str(self.override_scope.currentData())
        value = self.override_value.currentText().strip()
        mode = str(self.override_mode.currentData())
        if not value or mode not in DELIVERY_MODES:
            self.override_value.setFocus()
            return
        self._policy_overrides = [
            item
            for item in self._policy_overrides
            if not (item["scope"] == scope and item["value"] == value)
        ]
        self._policy_overrides.append({"scope": scope, "value": value, "mode": mode})
        self._rebuild_policy_overrides()
        self._emit_policy_changed()

    def _remove_policy_override(self, scope: str, value: str) -> None:
        self._policy_overrides = [
            item
            for item in self._policy_overrides
            if not (item["scope"] == scope and item["value"] == value)
        ]
        self._rebuild_policy_overrides()
        self._emit_policy_changed()

    def _rebuild_policy_overrides(self) -> None:
        while self.override_layout.count():
            item = self.override_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        if not self._policy_overrides:
            empty = QLabel("No server or channel overrides")
            empty.setProperty("role", "muted")
            empty.setContentsMargins(12, 10, 12, 10)
            self.override_layout.addWidget(empty)
            return
        for item in self._policy_overrides:
            row = PolicyOverrideRow(item["scope"], item["value"], item["mode"])
            row.removed.connect(self._remove_policy_override)
            self.override_layout.addWidget(row)

    def set_retention_days(self, days: int) -> None:
        previous = self.retention_spin.blockSignals(True)
        self.retention_spin.setValue(max(1, min(int(days), 3650)))
        self.retention_spin.blockSignals(previous)

    def set_launch_at_login(self, enabled: bool) -> None:
        previous = self.launch_toggle.blockSignals(True)
        self.launch_toggle.setChecked(bool(enabled))
        self.launch_toggle.blockSignals(previous)

    # --- per-server rebuild ---------------------------------------------

    def _rebuild_status_cards(self) -> None:
        for card in self._status_cards.values():
            self.status_layout.removeWidget(card)
            card.deleteLater()
        self._status_cards.clear()
        for url in self._order:
            card = ServerStatusRow(url, self._aliases.get(url, ""))
            card.reconnect_requested.connect(self.reconnect_requested)
            card.set_state(self._states.get(url, "connecting"), "Opening the event channel")
            self.status_layout.addWidget(card)
            self._status_cards[url] = card

    def _rebuild_panels(self) -> None:
        for panel in self._panels.values():
            self.panels_container.removeWidget(panel)
            panel.deleteLater()
        self._panels.clear()
        for url in self._order:
            panel = ServerPanel(
                url,
                self._channels[url],
                self._subs[url],
                self._aliases.get(url, ""),
                catalog_loaded=url in self._catalog_loaded,
            )
            panel.toggled.connect(self._channel_toggled)
            panel.remove_requested.connect(self._confirm_remove_server)
            panel.renamed.connect(self._server_renamed)
            panel.auth_save_requested.connect(self._auth_save)
            panel.auth_clear_requested.connect(self._auth_clear)
            panel.set_auth_status(self._auth_enabled.get(url, False))
            self.panels_container.addWidget(panel)
            self._panels[url] = panel
        self.server_count.setText(f"{len(self._order)} configured")

    def _server_renamed(self, url: str, alias: str) -> None:
        if url not in self._aliases or self._aliases[url] == alias:
            return
        self._aliases[url] = alias
        card = self._status_cards.get(url)
        if card is not None:
            card.set_alias(alias)
        self._emit_servers_changed()

    def _recompute_header(self) -> None:
        total = len(self._order)
        states = [self._states.get(url, "disconnected") for url in self._order]
        connected = sum(1 for s in states if s == "connected")
        if total == 0:
            state, text = "disconnected", "NO SERVERS"
        elif connected:
            state, text = "connected", f"{connected}/{total} LIVE"
        elif any(s == "connecting" for s in states):
            state, text = "connecting", "CONNECTING"
        elif any(s == "stopped" for s in states):
            state, text = "stopped", "PAUSED"
        else:
            state, text = "disconnected", "OFFLINE"
        self.header_dot.set_state(state)
        self.header_status.setText(text)
        self.header_status.setProperty("connectionState", state)
        _refresh_style(self.header_status)

    # --- controller-facing API ------------------------------------------

    def set_server_state(self, url: str, state: str, detail: str) -> None:
        if url not in self._states:
            return
        self._states[url] = state
        card = self._status_cards.get(url)
        if card is not None:
            card.set_state(state, detail)
        self._recompute_header()
        self._update_test_button()

    def set_server_health(self, url: str, rtt_ms: int, transport: str) -> None:
        card = self._status_cards.get(url)
        if card is not None:
            card.set_health(rtt_ms, transport)

    def set_server_reliability(
        self,
        url: str,
        *,
        last_connected: str | None = None,
        offline_since: str | float | None = None,
    ) -> None:
        card = self._status_cards.get(url)
        if card is not None:
            card.set_reliability(
                last_connected=last_connected,
                offline_since=offline_since,
            )

    def set_server_auth_status(self, url: str, stored: bool, secure_available: bool = True) -> None:
        if url not in self._auth_enabled:
            return
        self._auth_enabled[url] = bool(stored)
        panel = self._panels.get(url)
        if panel is not None:
            panel.set_auth_status(bool(stored), secure_available)

    def set_recovery_banner(
        self,
        message: str,
        *,
        title: str = "Alert delivery gap detected",
        severity: str = "warning",
    ) -> None:
        self.recovery_title.setText(title)
        self.recovery_detail.setText(message)
        self.recovery_banner.setProperty("severity", severity)
        self.recovery_banner.setAccessibleName(f"{title}. {message}")
        _refresh_style(self.recovery_banner)
        self.recovery_banner.setVisible(bool(message))

    def show_recovery_gap(
        self,
        server_url: str,
        start: str = "",
        end: str = "",
        detail: str = "",
    ) -> None:
        interval = f" from {start} to {end}" if start and end else ""
        message = detail or (
            f"{server_url} may have missed alerts{interval}. SignalDesk is requesting catch-up "
            "delivery; review history before treating the connection as fully recovered."
        )
        self.set_recovery_banner(message)

    def clear_recovery_banner(self) -> None:
        self.recovery_banner.hide()

    def set_server_catalog(self, url: str, payload: Any) -> None:
        raw_channels = payload.get("channels", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_channels, list):
            return
        if url not in self._channels:
            return
        channels: list[AlertChannel] = []
        seen: set[str] = set()
        for raw in raw_channels:
            if not isinstance(raw, dict):
                continue
            key = normalize_channel(raw.get("key") or raw.get("id"), fallback="")
            if not key or key in seen:
                continue
            channels.append(AlertChannel.from_payload({**raw, "key": key}))
            seen.add(key)
        self._channels[url] = channels
        self._catalog_loaded.add(url)
        panel = self._panels.get(url)
        if panel is not None:
            panel.set_channels(channels, self._subs[url], catalog_loaded=True)
        self._refresh_override_values()

    def set_confirmed_subscriptions(self, url: str, subscriptions: Any) -> None:
        if not isinstance(subscriptions, list) or url not in self._subs:
            return
        self._subs[url] = {str(item) for item in subscriptions}
        panel = self._panels.get(url)
        if panel is not None:
            panel.set_selected(self._subs[url])

    def set_test_pending(self, pending: bool) -> None:
        self._test_pending = pending
        self._update_test_button()

    def add_alert(self, alert: Alert, origin: str = "") -> None:
        record: object = alert
        if origin:
            record = {"alert": alert, "server_url": origin}
        self._history_records.insert(0, record)
        self.set_history_records(self._history_records)

    def any_connected(self) -> bool:
        return any(state == "connected" for state in self._states.values())

    def status_card(self, url: str) -> ServerStatusRow | None:
        return self._status_cards.get(url)

    # --- internals -------------------------------------------------------

    _test_pending = False

    def _update_test_button(self) -> None:
        self.test_button.setDisabled(self._test_pending)
        if self._test_pending:
            label = "Waiting for the server test alert"
        elif self.any_connected():
            label = "Send a test alert through a connected server"
        else:
            label = "Preview a local alert"
        _set_button_icon(
            self.test_button,
            "clock" if self._test_pending else "bell",
            icon_color=COLORS["muted"] if self._test_pending else COLORS["on_primary"],
        )
        self.test_button.setToolTip(label)
        self.test_button.setAccessibleName(label)

    def _refresh_health_age(self) -> None:
        for card in self._status_cards.values():
            card.refresh_age()

    def _channel_toggled(self, url: str, key: str, selected: bool) -> None:
        subs = self._subs.get(url)
        if subs is None:
            return
        if selected:
            subs.add(key)
        else:
            subs.discard(key)
        panel = self._panels.get(url)
        if panel is not None:
            panel.count_label.setText(f"{len(subs)} selected")
        self.subscriptions_changed.emit(url, sorted(subs))

    def _auth_save(self, url: str, token: str) -> None:
        self._auth_enabled[url] = True
        self.auth_save_requested.emit(url, token)
        self._emit_servers_changed()

    def _auth_clear(self, url: str) -> None:
        self._auth_enabled[url] = False
        self.auth_clear_requested.emit(url)
        self._emit_servers_changed()

    def _toggle_add_server_panel(self, visible: bool) -> None:
        self.add_server_panel.setVisible(visible)
        label = "Hide the add-server form" if visible else "Show the add-server form"
        self.add_server_button.setToolTip(label)
        self.add_server_button.setAccessibleName(label)
        if visible:
            self.endpoint_input.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _add_server(self) -> None:
        try:
            url = normalize_server_url(self.endpoint_input.text())
        except ValueError as exc:
            self.endpoint_input.setProperty("invalid", True)
            _refresh_style(self.endpoint_input)
            self.endpoint_error.setText(str(exc))
            self.endpoint_error.show()
            self.endpoint_input.setFocus()
            return
        self.endpoint_input.setProperty("invalid", False)
        _refresh_style(self.endpoint_input)
        self.endpoint_error.hide()
        if url in self._subs:
            self.endpoint_error.setText("That server is already configured")
            self.endpoint_error.show()
            return
        self.endpoint_input.clear()
        self._order.append(url)
        self._subs[url] = set()
        self._channels[url] = []
        self._states[url] = "connecting"
        self._aliases[url] = ""
        self._auth_enabled[url] = False
        self._rebuild_status_cards()
        self._rebuild_panels()
        self._recompute_header()
        self._emit_servers_changed()
        self.add_server_button.setChecked(False)

    def _confirm_remove_server(self, url: str) -> None:
        if url not in self._subs:
            return
        result = QMessageBox.question(
            self,
            "Remove server?",
            f"Remove {url}, its subscriptions, and any stored token from SignalDesk? "
            "Stored alert history is kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if result == QMessageBox.StandardButton.Yes:
            self._remove_server(url)

    def _remove_server(self, url: str) -> None:
        if url not in self._subs:
            return
        self._order = [item for item in self._order if item != url]
        self._subs.pop(url, None)
        self._channels.pop(url, None)
        self._catalog_loaded.discard(url)
        self._states.pop(url, None)
        self._aliases.pop(url, None)
        self._auth_enabled.pop(url, None)
        self._rebuild_status_cards()
        self._rebuild_panels()
        self._recompute_header()
        self._update_test_button()
        self._emit_servers_changed()

    def _emit_servers_changed(self) -> None:
        self.servers_changed.emit(
            [
                ServerConfig(
                    url=url,
                    subscriptions=sorted(self._subs[url]),
                    name=self._aliases.get(url, ""),
                    auth_enabled=self._auth_enabled.get(url, False),
                )
                for url in self._order
            ]
        )

    def show_and_activate(self) -> None:
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def prepare_to_quit(self) -> None:
        self._allow_close = True

    def _hide_or_quit(self) -> None:
        if self._tray_available:
            self.hide()
            self.hidden_to_tray.emit()
        else:
            self.quit_requested.emit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
        elif self._tray_available:
            event.ignore()
            self.hide()
            self.hidden_to_tray.emit()
        else:
            event.ignore()
            self.quit_requested.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self._tray_available:
            self.hide()
            self.hidden_to_tray.emit()
            return
        super().keyPressEvent(event)
