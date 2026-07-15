"""Compact multi-server alert management window."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent, QPainter, QResizeEvent
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

from signaldesk.config import AppConfig, ServerConfig, normalize_server_url
from signaldesk.icons import SeverityIcon, make_app_icon
from signaldesk.models import Alert, AlertChannel
from signaldesk.theme import color

BUILTIN_CHANNELS = [
    AlertChannel("infrastructure", "Infrastructure", "Hosts, services, and capacity signals"),
    AlertChannel("security", "Security", "Access, policy, and threat detection events"),
    AlertChannel("deployments", "Deployments", "Build and release lifecycle updates"),
    AlertChannel("billing", "Billing", "Usage limits, invoices, and payment events"),
    AlertChannel("product", "Product", "Feature flags and product announcements"),
]

_STATE_TEXT = {
    "connected": ("Connected and listening", "LIVE"),
    "connecting": ("Connecting to server", "CONNECTING"),
    "disconnected": ("Server offline", "OFFLINE"),
    "stopped": ("Connection paused", "PAUSED"),
}


def _refresh_style(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def _alert_time(value: str) -> str:
    """Return a compact local time for ledger metadata."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return "NOW"


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
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        key = ElidedLabel(channel.key.upper())
        key.setProperty("role", "eyebrow")
        key.setMaximumWidth(150)
        heading.addWidget(name)
        heading.addStretch()
        heading.addWidget(key)
        description = QLabel(channel.description)
        description.setProperty("role", "muted")
        description.setWordWrap(True)
        description.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addLayout(heading)
        text.addWidget(description)
        body_layout.addLayout(text, 1)

        self.checkbox = QCheckBox()
        self.checkbox.setChecked(selected)
        self.checkbox.setText("ON" if selected else "OFF")
        self.checkbox.setAccessibleName(f"Subscribe to {channel.name}")
        self.checkbox.toggled.connect(self._changed)
        body_layout.addWidget(self.checkbox, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(body, 1)

    def _changed(self, selected: bool) -> None:
        self._set_visual_state(selected)
        self.toggled.emit(self.channel.key, selected)

    def _set_visual_state(self, selected: bool) -> None:
        self.checkbox.setText("ON" if selected else "OFF")
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
    def __init__(self, alert: Alert, origin: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AlertHistoryRow")
        self.setMinimumHeight(92)
        self.setAccessibleName(
            f"{alert.severity.value.title()} alert, {alert.title}, from {alert.source}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        accent = QFrame()
        accent.setObjectName("HistoryAccent")
        accent.setProperty("severity", alert.severity.value)
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
        severity = QLabel(alert.severity.value.upper())
        severity.setObjectName("SeverityCode")
        severity.setProperty("severity", alert.severity.value)
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
        content.addWidget(title)

        message = QLabel(alert.message)
        message.setProperty("role", "muted")
        message.setWordWrap(True)
        message.setMaximumHeight(36)
        message.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        content.addWidget(message)
        body_layout.addLayout(content, 1)
        layout.addWidget(body, 1)


class ServerStatusCard(QFrame):
    """Per-server connection state and health metrics on the Overview tab."""

    reconnect_requested = Signal(str)

    def __init__(self, url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.url = url
        self._state = "disconnected"
        self._last_health_at: float | None = None
        self.setObjectName("ConnectionCard")

        card_layout = QVBoxLayout(self)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        self.accent = QFrame()
        self.accent.setObjectName("ConnectionAccent")
        self.accent.setProperty("connectionState", "disconnected")
        card_layout.addWidget(self.accent)

        head = QWidget()
        head_layout = QVBoxLayout(head)
        head_layout.setContentsMargins(16, 12, 12, 12)
        head_layout.setSpacing(6)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        self.endpoint_label = ElidedLabel(_server_label(url))
        self.endpoint_label.setProperty("role", "eyebrow")
        top_row.addWidget(self.endpoint_label, 1)
        self.reconnect_button = QPushButton("Reconnect")
        self.reconnect_button.setObjectName("SecondaryButton")
        self.reconnect_button.clicked.connect(lambda: self.reconnect_requested.emit(self.url))
        top_row.addWidget(self.reconnect_button)
        head_layout.addLayout(top_row)

        state_row = QHBoxLayout()
        state_row.setSpacing(9)
        self.status_dot = StatusDot()
        state_row.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignTop)
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
        head_layout.addLayout(state_row)
        card_layout.addWidget(head)

        divider = QFrame()
        divider.setObjectName("HorizontalRule")
        card_layout.addWidget(divider)

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
        card_layout.addWidget(metric_strip)

    def set_state(self, state: str, detail: str) -> None:
        self._state = state
        self.status_dot.set_state(state)
        self.accent.setProperty("connectionState", state)
        _refresh_style(self.accent)
        title, _pill = _STATE_TEXT.get(state, _STATE_TEXT["disconnected"])
        self.connection_title.setText(title)
        self.connection_detail.setText(detail)
        self.reconnect_button.setDisabled(state == "connecting")
        if state != "connected":
            self.latency_metric.value.setText("—")
            self.transport_metric.value.setText("—")
            self.heartbeat_metric.value.setText("Waiting")
            self._last_health_at = None

    def set_health(self, rtt_ms: int, transport: str) -> None:
        self._last_health_at = time.monotonic()
        self.latency_metric.value.setText(f"{rtt_ms} ms")
        self.transport_metric.value.setText(transport)
        self.heartbeat_metric.value.setText("Now")

    def refresh_age(self) -> None:
        if self._last_health_at is None or self._state != "connected":
            return
        age = max(0, int(time.monotonic() - self._last_health_at))
        if age < 2:
            value = "Now"
        elif age < 60:
            value = f"{age}s ago"
        else:
            value = f"{age // 60}m ago"
        self.heartbeat_metric.value.setText(value)


class ServerPanel(QFrame):
    """Per-server endpoint header plus its channel subscription toggles."""

    toggled = Signal(str, str, bool)
    remove_requested = Signal(str)

    def __init__(
        self,
        url: str,
        channels: list[AlertChannel],
        selected: set[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.url = url
        self._rows: dict[str, ChannelRow] = {}
        self.setObjectName("EndpointCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        accent = QFrame()
        accent.setObjectName("PanelAccent")
        layout.addWidget(accent)

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(15, 12, 12, 8)
        header_layout.setSpacing(4)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        labels = QVBoxLayout()
        labels.setSpacing(1)
        kicker = QLabel("CONNECTION / SOCKET.IO")
        kicker.setProperty("role", "eyebrow")
        title = QLabel(_server_label(url))
        title.setObjectName("SectionTitle")
        labels.addWidget(kicker)
        labels.addWidget(title)
        title_row.addLayout(labels, 1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("CounterLabel")
        title_row.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignTop)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("GhostButton")
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.url))
        title_row.addWidget(self.remove_button, 0, Qt.AlignmentFlag.AlignTop)
        header_layout.addLayout(title_row)
        endpoint = ElidedLabel(url)
        endpoint.setProperty("role", "muted")
        header_layout.addWidget(endpoint)
        layout.addWidget(header)

        self.channel_ledger = QFrame()
        self.channel_ledger.setObjectName("Ledger")
        self.channel_layout = QVBoxLayout(self.channel_ledger)
        self.channel_layout.setContentsMargins(0, 0, 0, 0)
        self.channel_layout.setSpacing(0)
        layout.addWidget(self.channel_ledger)

        self.set_channels(channels, selected)

    def set_channels(self, channels: list[AlertChannel], selected: set[str]) -> None:
        while self.channel_layout.count():
            item = self.channel_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._rows.clear()
        for channel in channels:
            row = ChannelRow(channel, channel.key in selected)
            row.toggled.connect(lambda key, state, url=self.url: self.toggled.emit(url, key, state))
            self._rows[channel.key] = row
            self.channel_layout.addWidget(row)
        self.set_selected(selected)

    def set_selected(self, selected: set[str]) -> None:
        for key, row in self._rows.items():
            row.set_selected(key in selected)
        self.count_label.setText(f"{len(selected)} selected")


class ManagementWindow(QMainWindow):
    servers_changed = Signal(object)
    subscriptions_changed = Signal(str, object)
    reconnect_requested = Signal(str)
    test_requested = Signal()
    quit_requested = Signal()
    hidden_to_tray = Signal()

    def __init__(self, config: AppConfig, *, tray_available: bool) -> None:
        super().__init__()
        self._tray_available = tray_available
        self._allow_close = False
        self._history_rows: list[AlertHistoryRow] = []

        # Per-server model, keyed by URL.
        self._order: list[str] = [s.url for s in config.servers]
        self._subs: dict[str, set[str]] = {s.url: set(s.subscriptions) for s in config.servers}
        self._channels: dict[str, list[AlertChannel]] = {
            s.url: list(BUILTIN_CHANNELS) for s in config.servers
        }
        self._states: dict[str, str] = {s.url: "connecting" for s in config.servers}
        self._status_cards: dict[str, ServerStatusCard] = {}
        self._panels: dict[str, ServerPanel] = {}

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
        outer.addWidget(header_frame)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(22, 14, 22, 0)
        body_layout.setSpacing(0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setAccessibleName("Alert management sections")
        self.tabs.addTab(self._build_overview_tab(), "OVERVIEW")
        self.tabs.addTab(self._build_servers_tab(), "SERVERS")
        body_layout.addWidget(self.tabs, 1)
        outer.addWidget(body, 1)

        footer_frame = QFrame()
        footer_frame.setObjectName("FooterRail")
        footer = QHBoxLayout(footer_frame)
        footer.setContentsMargins(22, 10, 22, 10)
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
        outer.addWidget(footer_frame)

        self._rebuild_status_cards()
        self._rebuild_panels()
        self._recompute_header()

    def _build_overview_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        servers_scroll = QScrollArea()
        servers_scroll.setWidgetResizable(True)
        servers_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        servers_scroll.setMaximumHeight(300)
        servers_content = QWidget()
        self.status_layout = QVBoxLayout(servers_content)
        self.status_layout.setContentsMargins(0, 0, 2, 0)
        self.status_layout.setSpacing(10)
        self.status_layout.addStretch()
        servers_scroll.setWidget(servers_content)
        layout.addWidget(servers_scroll)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.test_button = QPushButton("Preview alert")
        self.test_button.setObjectName("PrimaryButton")
        self.test_button.setToolTip(
            "Test locally when offline, or through a live socket when connected"
        )
        self.test_button.clicked.connect(self.test_requested)
        actions.addWidget(self.test_button, 1)
        layout.addLayout(actions)

        history_header = QHBoxLayout()
        history_labels = QVBoxLayout()
        history_labels.setSpacing(1)
        history_kicker = QLabel("ACTIVITY LOG / LATEST")
        history_kicker.setProperty("role", "eyebrow")
        history_title = QLabel("Recent alerts")
        history_title.setObjectName("SectionTitle")
        history_labels.addWidget(history_kicker)
        history_labels.addWidget(history_title)
        history_header.addLayout(history_labels)
        history_header.addStretch()
        self.history_count = QLabel("0 received")
        self.history_count.setObjectName("CounterLabel")
        history_header.addWidget(self.history_count)
        layout.addLayout(history_header)

        history_scroll = QScrollArea()
        history_scroll.setWidgetResizable(True)
        history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
        empty_layout.setContentsMargins(18, 20, 18, 22)
        empty_layout.setSpacing(3)
        empty_code = QLabel("00")
        empty_code.setObjectName("EmptyCode")
        empty_title = QLabel("No events recorded")
        empty_title.setObjectName("SectionTitle")
        empty_detail = QLabel(
            "Incoming socket events will appear here and at the top-right of your screen."
        )
        empty_detail.setProperty("role", "muted")
        empty_detail.setWordWrap(True)
        empty_layout.addWidget(empty_code)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_detail)
        self.history_layout.addWidget(self.empty_history)
        history_content_layout.addWidget(self.history_ledger)
        history_content_layout.addStretch()
        history_scroll.setWidget(history_content)
        layout.addWidget(history_scroll, 1)
        return tab

    def _build_servers_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        add_card = QFrame()
        add_card.setObjectName("EndpointCard")
        add_outer = QVBoxLayout(add_card)
        add_outer.setContentsMargins(0, 0, 0, 0)
        add_outer.setSpacing(0)
        add_accent = QFrame()
        add_accent.setObjectName("PanelAccent")
        add_outer.addWidget(add_accent)
        add_body = QWidget()
        add_layout = QVBoxLayout(add_body)
        add_layout.setContentsMargins(15, 12, 15, 14)
        add_layout.setSpacing(7)
        add_kicker = QLabel("FLEET / ADD ENDPOINT")
        add_kicker.setProperty("role", "eyebrow")
        add_title = QLabel("Connect a server")
        add_title.setObjectName("SectionTitle")
        add_layout.addWidget(add_kicker)
        add_layout.addWidget(add_title)
        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self.endpoint_input = QLineEdit()
        self.endpoint_input.setPlaceholderText("http://host:port")
        self.endpoint_input.setClearButtonEnabled(True)
        self.endpoint_input.setAccessibleName("New Socket.IO server URL")
        self.endpoint_input.returnPressed.connect(self._add_server)
        add_row.addWidget(self.endpoint_input, 1)
        self.add_button = QPushButton("Add server")
        self.add_button.setObjectName("SecondaryButton")
        self.add_button.clicked.connect(self._add_server)
        add_row.addWidget(self.add_button)
        add_layout.addLayout(add_row)
        self.endpoint_error = QLabel("")
        self.endpoint_error.setObjectName("EndpointError")
        self.endpoint_error.setWordWrap(True)
        self.endpoint_error.hide()
        add_layout.addWidget(self.endpoint_error)
        add_outer.addWidget(add_body)
        layout.addWidget(add_card)

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
        layout.addLayout(panels_header)

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

    # --- per-server rebuild ---------------------------------------------

    def _rebuild_status_cards(self) -> None:
        for card in self._status_cards.values():
            self.status_layout.removeWidget(card)
            card.deleteLater()
        self._status_cards.clear()
        for index, url in enumerate(self._order):
            card = ServerStatusCard(url)
            card.reconnect_requested.connect(self.reconnect_requested)
            card.set_state(self._states.get(url, "connecting"), "Opening the event channel")
            self.status_layout.insertWidget(index, card)
            self._status_cards[url] = card

    def _rebuild_panels(self) -> None:
        for panel in self._panels.values():
            self.panels_container.removeWidget(panel)
            panel.deleteLater()
        self._panels.clear()
        for url in self._order:
            panel = ServerPanel(url, self._channels[url], self._subs[url])
            panel.toggled.connect(self._channel_toggled)
            panel.remove_requested.connect(self._remove_server)
            self.panels_container.addWidget(panel)
            self._panels[url] = panel
        self.server_count.setText(f"{len(self._order)} configured")

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

    def set_server_catalog(self, url: str, payload: Any) -> None:
        raw_channels = payload.get("channels", []) if isinstance(payload, dict) else payload
        if not isinstance(raw_channels, list):
            return
        channels = [AlertChannel.from_payload(raw) for raw in raw_channels if isinstance(raw, dict)]
        if not channels or url not in self._channels:
            return
        self._channels[url] = channels
        panel = self._panels.get(url)
        if panel is not None:
            panel.set_channels(channels, self._subs[url])

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
        if not self._history_rows:
            self.empty_history.hide()
        row = AlertHistoryRow(alert, origin)
        self._history_rows.insert(0, row)
        self.history_layout.insertWidget(0, row)
        while len(self._history_rows) > 30:
            old = self._history_rows.pop()
            self.history_layout.removeWidget(old)
            old.deleteLater()
        self.history_count.setText(f"{len(self._history_rows)} received")

    def any_connected(self) -> bool:
        return any(state == "connected" for state in self._states.values())

    def status_card(self, url: str) -> ServerStatusCard | None:
        return self._status_cards.get(url)

    # --- internals -------------------------------------------------------

    _test_pending = False

    def _update_test_button(self) -> None:
        self.test_button.setDisabled(self._test_pending)
        if self._test_pending:
            self.test_button.setText("Waiting for server…")
        elif self.any_connected():
            self.test_button.setText("Send socket test")
        else:
            self.test_button.setText("Preview alert")

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
        self._channels[url] = list(BUILTIN_CHANNELS)
        self._states[url] = "connecting"
        self._rebuild_status_cards()
        self._rebuild_panels()
        self._recompute_header()
        self._emit_servers_changed()

    def _remove_server(self, url: str) -> None:
        if url not in self._subs:
            return
        self._order = [item for item in self._order if item != url]
        self._subs.pop(url, None)
        self._channels.pop(url, None)
        self._states.pop(url, None)
        self._rebuild_status_cards()
        self._rebuild_panels()
        self._recompute_header()
        self._update_test_button()
        self._emit_servers_changed()

    def _emit_servers_changed(self) -> None:
        self.servers_changed.emit(
            [ServerConfig(url=url, subscriptions=sorted(self._subs[url])) for url in self._order]
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
