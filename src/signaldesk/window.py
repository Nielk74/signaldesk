"""Compact alert management window."""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from signaldesk.config import AppConfig, normalize_server_url
from signaldesk.icons import SeverityIcon, make_app_icon
from signaldesk.models import Alert, AlertChannel
from signaldesk.theme import COLORS, color

BUILTIN_CHANNELS = [
    AlertChannel("infrastructure", "Infrastructure", "Hosts, services, and capacity signals"),
    AlertChannel("security", "Security", "Access, policy, and threat detection events"),
    AlertChannel("deployments", "Deployments", "Build and release lifecycle updates"),
    AlertChannel("billing", "Billing", "Usage limits, invoices, and payment events"),
    AlertChannel("product", "Product", "Feature flags and product announcements"),
]


def _refresh_style(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


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
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        token = {
            "connected": "success",
            "connecting": "warning",
            "disconnected": "critical",
            "stopped": "critical",
        }.get(self._state, "critical")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color(token))
        painter.drawEllipse(3, 3, 8, 8)
        painter.end()


class Metric(QWidget):
    def __init__(self, label: str, value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        caption = QLabel(label)
        caption.setProperty("role", "muted")
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
        self.setMinimumHeight(70)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 12, 10)
        layout.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(3)
        name = QLabel(channel.name)
        name.setObjectName("SectionTitle")
        description = QLabel(channel.description)
        description.setProperty("role", "muted")
        description.setWordWrap(True)
        text.addWidget(name)
        text.addWidget(description)
        layout.addLayout(text, 1)

        self.checkbox = QCheckBox()
        self.checkbox.setChecked(selected)
        self.checkbox.setText("On" if selected else "Off")
        self.checkbox.setAccessibleName(f"Subscribe to {channel.name}")
        self.checkbox.toggled.connect(self._changed)
        layout.addWidget(self.checkbox, 0, Qt.AlignmentFlag.AlignVCenter)

    def _changed(self, selected: bool) -> None:
        self.checkbox.setText("On" if selected else "Off")
        self.toggled.emit(self.channel.key, selected)

    def set_selected(self, selected: bool) -> None:
        previous = self.checkbox.blockSignals(True)
        self.checkbox.setChecked(selected)
        self.checkbox.setText("On" if selected else "Off")
        self.checkbox.blockSignals(previous)


class AlertHistoryRow(QFrame):
    def __init__(self, alert: Alert, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AlertHistoryRow")
        self.setMinimumHeight(76)
        self.setAccessibleName(
            f"{alert.severity.value.title()} alert, {alert.title}, from {alert.source}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(11)
        layout.addWidget(SeverityIcon(alert.severity, 32), 0, Qt.AlignmentFlag.AlignTop)

        content = QVBoxLayout()
        content.setSpacing(3)
        heading = QHBoxLayout()
        heading.setSpacing(8)
        title = QLabel(alert.title)
        title.setObjectName("SectionTitle")
        title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        badge = QLabel(alert.severity.value.upper())
        badge.setObjectName("SeverityBadge")
        badge.setProperty("severity", alert.severity.value)
        heading.addWidget(title, 1)
        heading.addWidget(badge)
        content.addLayout(heading)

        message = QLabel(alert.message)
        message.setProperty("role", "muted")
        message.setWordWrap(True)
        message.setMaximumHeight(38)
        content.addWidget(message)
        metadata = QLabel(f"{alert.source}  ·  {alert.channel}  ·  Just now")
        metadata.setProperty("role", "muted")
        content.addWidget(metadata)
        layout.addLayout(content, 1)


class ManagementWindow(QMainWindow):
    reconnect_requested = Signal(str)
    subscriptions_changed = Signal(object)
    test_requested = Signal()
    quit_requested = Signal()
    hidden_to_tray = Signal()

    def __init__(self, config: AppConfig, *, tray_available: bool) -> None:
        super().__init__()
        self._tray_available = tray_available
        self._allow_close = False
        self._subscriptions = set(config.subscriptions)
        self._channel_rows: dict[str, ChannelRow] = {}
        self._history_rows: list[AlertHistoryRow] = []
        self._connection_state = "disconnected"
        self._test_pending = False
        self._last_health_at: float | None = None

        self.setObjectName("RootWindow")
        self.setWindowTitle("SignalDesk — Alert center")
        self.setWindowIcon(make_app_icon())
        self.resize(520, 720)
        self.setMinimumSize(470, 610)
        self._build_ui(config)

        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._refresh_health_age)
        self._clock.start()

    def _build_ui(self, config: AppConfig) -> None:
        root = QWidget()
        root.setObjectName("RootContent")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 18, 22, 16)
        outer.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(11)
        brand_icon = QLabel()
        brand_icon.setPixmap(make_app_icon().pixmap(38, 38))
        brand_icon.setFixedSize(40, 40)
        brand_icon.setAccessibleName("SignalDesk logo")
        header.addWidget(brand_icon)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        title = QLabel("SignalDesk")
        title.setObjectName("BrandTitle")
        subtitle = QLabel("Real-time alert center")
        subtitle.setObjectName("BrandSubtitle")
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)
        header.addLayout(brand_text)
        header.addStretch()
        self.header_status = QLabel("OFFLINE")
        self.header_status.setObjectName("StatusPill")
        self.header_status.setProperty("connectionState", "disconnected")
        header.addWidget(self.header_status)
        outer.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setAccessibleName("Alert management sections")
        self.tabs.addTab(self._build_overview_tab(), "Overview")
        self.tabs.addTab(self._build_channels_tab(config), "Subscriptions")
        outer.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        footer_text = QLabel(
            "Continues running in the system tray"
            if self._tray_available
            else "Closing exits SignalDesk"
        )
        footer_text.setProperty("role", "muted")
        footer.addWidget(footer_text)
        footer.addStretch()
        self.hide_button = QPushButton("Hide to tray" if self._tray_available else "Quit")
        self.hide_button.setObjectName("GhostButton")
        self.hide_button.clicked.connect(self._hide_or_quit)
        footer.addWidget(self.hide_button)
        outer.addLayout(footer)

    def _build_overview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(12)

        card = QFrame()
        card.setObjectName("ConnectionCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(17, 15, 17, 15)
        card_layout.setSpacing(12)

        state_row = QHBoxLayout()
        state_row.setSpacing(9)
        self.status_dot = StatusDot()
        state_row.addWidget(self.status_dot)
        state_text = QVBoxLayout()
        state_text.setSpacing(2)
        self.connection_title = QLabel("Server offline")
        self.connection_title.setObjectName("ConnectionTitle")
        self.connection_detail = QLabel("Trying to reach the configured endpoint")
        self.connection_detail.setProperty("role", "muted")
        self.connection_detail.setWordWrap(True)
        state_text.addWidget(self.connection_title)
        state_text.addWidget(self.connection_detail)
        state_row.addLayout(state_text, 1)
        card_layout.addLayout(state_row)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"background: {COLORS['border']}; max-height: 1px; border: none;")
        card_layout.addWidget(divider)

        metrics = QHBoxLayout()
        metrics.setSpacing(14)
        self.latency_metric = Metric("ROUND TRIP", "—")
        self.transport_metric = Metric("TRANSPORT", "—")
        self.heartbeat_metric = Metric("LAST HEARTBEAT", "Waiting")
        metrics.addWidget(self.latency_metric, 1)
        metrics.addWidget(self.transport_metric, 1)
        metrics.addWidget(self.heartbeat_metric, 1)
        card_layout.addLayout(metrics)
        layout.addWidget(card)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.test_button = QPushButton("Preview alert")
        self.test_button.setObjectName("PrimaryButton")
        self.test_button.setToolTip(
            "Test locally when offline, or through the socket when connected"
        )
        self.test_button.clicked.connect(self.test_requested)
        actions.addWidget(self.test_button, 1)
        self.quick_reconnect_button = QPushButton("Reconnect")
        self.quick_reconnect_button.setObjectName("SecondaryButton")
        self.quick_reconnect_button.clicked.connect(self._apply_endpoint)
        actions.addWidget(self.quick_reconnect_button)
        layout.addLayout(actions)

        history_header = QHBoxLayout()
        history_title = QLabel("Recent alerts")
        history_title.setObjectName("SectionTitle")
        history_header.addWidget(history_title)
        history_header.addStretch()
        self.history_count = QLabel("0 received")
        self.history_count.setProperty("role", "muted")
        history_header.addWidget(self.history_count)
        layout.addLayout(history_header)

        history_scroll = QScrollArea()
        history_scroll.setWidgetResizable(True)
        history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        history_content = QWidget()
        self.history_layout = QVBoxLayout(history_content)
        self.history_layout.setContentsMargins(0, 0, 2, 0)
        self.history_layout.setSpacing(8)
        self.empty_history = QFrame()
        self.empty_history.setObjectName("Card")
        empty_layout = QVBoxLayout(self.empty_history)
        empty_layout.setContentsMargins(16, 22, 16, 22)
        empty_title = QLabel("No alerts yet")
        empty_title.setObjectName("SectionTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_detail = QLabel(
            "Incoming socket events will appear here and at the top-right of your screen."
        )
        empty_detail.setProperty("role", "muted")
        empty_detail.setWordWrap(True)
        empty_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_detail)
        self.history_layout.addWidget(self.empty_history)
        self.history_layout.addStretch()
        history_scroll.setWidget(history_content)
        layout.addWidget(history_scroll, 1)
        return tab

    def _build_channels_tab(self, config: AppConfig) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(12)

        endpoint_card = QFrame()
        endpoint_card.setObjectName("EndpointCard")
        endpoint_layout = QVBoxLayout(endpoint_card)
        endpoint_layout.setContentsMargins(15, 13, 15, 14)
        endpoint_layout.setSpacing(7)
        endpoint_title = QLabel("Server endpoint")
        endpoint_title.setObjectName("SectionTitle")
        endpoint_layout.addWidget(endpoint_title)
        self.endpoint_input = QLineEdit(config.server_url)
        self.endpoint_input.setClearButtonEnabled(True)
        self.endpoint_input.setAccessibleName("Socket.IO server URL")
        self.endpoint_input.returnPressed.connect(self._apply_endpoint)
        endpoint_layout.addWidget(self.endpoint_input)
        self.endpoint_error = QLabel("")
        self.endpoint_error.setStyleSheet(f"color: {COLORS['critical']};")
        self.endpoint_error.setWordWrap(True)
        self.endpoint_error.hide()
        endpoint_layout.addWidget(self.endpoint_error)
        endpoint_actions = QHBoxLayout()
        endpoint_helper = QLabel("HTTP(S) URL; Socket.IO upgrades the transport automatically")
        endpoint_helper.setProperty("role", "muted")
        endpoint_helper.setWordWrap(True)
        endpoint_actions.addWidget(endpoint_helper, 1)
        self.apply_endpoint_button = QPushButton("Apply & reconnect")
        self.apply_endpoint_button.setObjectName("SecondaryButton")
        self.apply_endpoint_button.clicked.connect(self._apply_endpoint)
        endpoint_actions.addWidget(self.apply_endpoint_button)
        endpoint_layout.addLayout(endpoint_actions)
        layout.addWidget(endpoint_card)

        channel_header = QHBoxLayout()
        channel_title = QLabel("Alert channels")
        channel_title.setObjectName("SectionTitle")
        channel_header.addWidget(channel_title)
        channel_header.addStretch()
        self.subscription_count = QLabel("")
        self.subscription_count.setProperty("role", "muted")
        channel_header.addWidget(self.subscription_count)
        layout.addLayout(channel_header)

        channel_scroll = QScrollArea()
        channel_scroll.setWidgetResizable(True)
        channel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        channel_content = QWidget()
        self.channel_layout = QVBoxLayout(channel_content)
        self.channel_layout.setContentsMargins(0, 0, 2, 0)
        self.channel_layout.setSpacing(8)
        channel_scroll.setWidget(channel_content)
        layout.addWidget(channel_scroll, 1)
        self.set_channels(BUILTIN_CHANNELS)
        return tab

    def set_connection_state(self, state: str, detail: str) -> None:
        self._connection_state = state
        self.status_dot.set_state(state)
        connected = state == "connected"
        texts = {
            "connected": ("Connected and listening", "LIVE"),
            "connecting": ("Connecting to server", "CONNECTING"),
            "disconnected": ("Server offline", "OFFLINE"),
            "stopped": ("Connection paused", "PAUSED"),
        }
        title, pill = texts.get(state, texts["disconnected"])
        self.connection_title.setText(title)
        self.connection_detail.setText(detail)
        self.header_status.setText(pill)
        self.header_status.setProperty("connectionState", state)
        _refresh_style(self.header_status)
        self._update_test_button()
        self.quick_reconnect_button.setDisabled(state == "connecting")
        self.apply_endpoint_button.setDisabled(state == "connecting")
        if not connected:
            self.latency_metric.value.setText("—")
            self.transport_metric.value.setText("—")
            self.heartbeat_metric.value.setText("Waiting")
            self._last_health_at = None

    def set_test_pending(self, pending: bool) -> None:
        self._test_pending = pending
        self._update_test_button()

    def _update_test_button(self) -> None:
        self.test_button.setDisabled(self._test_pending)
        if self._test_pending:
            self.test_button.setText("Waiting for server…")
        elif self._connection_state == "connected":
            self.test_button.setText("Send socket test")
        else:
            self.test_button.setText("Preview alert")

    def set_health(self, rtt_ms: int, transport: str) -> None:
        self._last_health_at = time.monotonic()
        self.latency_metric.value.setText(f"{rtt_ms} ms")
        self.transport_metric.value.setText(transport)
        self.heartbeat_metric.value.setText("Now")

    def _refresh_health_age(self) -> None:
        if self._last_health_at is None or self._connection_state != "connected":
            return
        age = max(0, int(time.monotonic() - self._last_health_at))
        if age < 2:
            value = "Now"
        elif age < 60:
            value = f"{age}s ago"
        else:
            value = f"{age // 60}m ago"
        self.heartbeat_metric.value.setText(value)

    def set_catalog(self, payload: Any) -> None:
        raw_channels = payload.get("channels", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_channels, list):
            return
        channels: list[AlertChannel] = []
        for raw in raw_channels:
            if isinstance(raw, dict):
                channels.append(AlertChannel.from_payload(raw))
        if channels:
            self.set_channels(channels)

    def set_channels(self, channels: list[AlertChannel]) -> None:
        while self.channel_layout.count():
            item = self.channel_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._channel_rows.clear()
        for channel in channels:
            row = ChannelRow(channel, channel.key in self._subscriptions)
            row.toggled.connect(self._channel_toggled)
            self._channel_rows[channel.key] = row
            self.channel_layout.addWidget(row)
        self.channel_layout.addStretch()
        self._update_subscription_count()

    def set_confirmed_subscriptions(self, subscriptions: Any) -> None:
        if not isinstance(subscriptions, list):
            return
        self._subscriptions = {str(item) for item in subscriptions}
        for key, row in self._channel_rows.items():
            row.set_selected(key in self._subscriptions)
        self._update_subscription_count()

    def _channel_toggled(self, key: str, selected: bool) -> None:
        if selected:
            self._subscriptions.add(key)
        else:
            self._subscriptions.discard(key)
        self._update_subscription_count()
        self.subscriptions_changed.emit(sorted(self._subscriptions))

    def _update_subscription_count(self) -> None:
        count = len(self._subscriptions)
        self.subscription_count.setText(f"{count} selected")

    def add_alert(self, alert: Alert) -> None:
        if not self._history_rows:
            self.empty_history.hide()
        row = AlertHistoryRow(alert)
        self._history_rows.insert(0, row)
        self.history_layout.insertWidget(0, row)
        while len(self._history_rows) > 30:
            old = self._history_rows.pop()
            self.history_layout.removeWidget(old)
            old.deleteLater()
        count = len(self._history_rows)
        self.history_count.setText(f"{count} received")

    def show_and_activate(self) -> None:
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def prepare_to_quit(self) -> None:
        self._allow_close = True

    def _apply_endpoint(self) -> None:
        try:
            endpoint = normalize_server_url(self.endpoint_input.text())
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
        self.endpoint_input.setText(endpoint)
        self.reconnect_requested.emit(endpoint)

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
