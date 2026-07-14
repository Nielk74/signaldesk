"""Socket.IO client isolated from the Qt GUI thread."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

import socketio
from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, QTimer, Signal, Slot

LOGGER = logging.getLogger(__name__)


class SocketWorker(QObject):
    state_changed = Signal(str, str)
    alert_received = Signal(object)
    catalog_received = Signal(object)
    subscriptions_confirmed = Signal(object)
    health_updated = Signal(int, str)

    def __init__(self) -> None:
        super().__init__()
        self._sio = socketio.Client(
            reconnection=False,
            logger=False,
            engineio_logger=False,
            handle_sigint=False,
        )
        self._timer: QTimer | None = None
        self._desired_connection = False
        self._connecting = False
        self._connected = False
        self._url = ""
        self._subscriptions: set[str] = set()
        self._next_attempt_at = 0.0
        self._retry_delay = 1.0
        self._last_ping_at = 0.0
        self._pending_pings: dict[str, float] = {}
        self._ping_lock = threading.Lock()
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self._sio.event
        def connect() -> None:
            self._connected = True
            self._connecting = False
            self._retry_delay = 1.0
            self._last_ping_at = 0.0
            self.state_changed.emit("connected", self._transport_name())

        @self._sio.event
        def connect_error(data: Any) -> None:
            self._connected = False
            self._connecting = False
            detail = data.get("message", str(data)) if isinstance(data, dict) else str(data)
            self.state_changed.emit("disconnected", detail or "Connection refused")

        @self._sio.event
        def disconnect(reason: Any = None) -> None:
            self._connected = False
            self._connecting = False
            self._next_attempt_at = time.monotonic() + self._retry_delay
            state = "disconnected" if self._desired_connection else "stopped"
            self.state_changed.emit(state, str(reason or "Connection closed"))

        @self._sio.on("alert")
        def on_alert(data: Any) -> None:
            if isinstance(data, dict):
                self.alert_received.emit(data)

        @self._sio.on("catalog")
        def on_catalog(data: Any) -> None:
            self.catalog_received.emit(data)

        @self._sio.on("subscriptions:confirmed")
        def on_subscriptions(data: Any) -> None:
            if isinstance(data, dict):
                self.subscriptions_confirmed.emit(data.get("subscriptions", []))
            elif isinstance(data, list):
                self.subscriptions_confirmed.emit(data)

        @self._sio.on("health:pong")
        def on_health_pong(data: Any) -> None:
            if not isinstance(data, dict):
                return
            nonce = str(data.get("nonce", ""))
            with self._ping_lock:
                started_at = self._pending_pings.pop(nonce, None)
            if started_at is not None:
                rtt_ms = max(0, round((time.perf_counter() - started_at) * 1000))
                self.health_updated.emit(rtt_ms, self._transport_name())

    def _transport_name(self) -> str:
        try:
            transport = self._sio.transport
            return str(transport() if callable(transport) else transport).title()
        except (AttributeError, TypeError, ValueError):
            return "Socket.IO"

    @Slot()
    def initialize(self) -> None:
        if self._timer is not None:
            return
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._maintenance)
        self._timer.start()

    @Slot(str, object)
    def connect_to_server(self, url: str, subscriptions: object) -> None:
        requested = (
            {str(item) for item in subscriptions} if isinstance(subscriptions, list) else set()
        )
        endpoint_changed = url != self._url
        self._url = url
        self._subscriptions = requested
        self._desired_connection = True
        self._next_attempt_at = 0.0
        self._retry_delay = 1.0

        if endpoint_changed and self._sio.connected:
            self._sio.disconnect()
        elif self._sio.connected:
            self._emit_subscriptions()
            self.state_changed.emit("connected", self._transport_name())
        else:
            self.state_changed.emit("connecting", "Opening the event channel")

    @Slot(object)
    def update_subscriptions(self, subscriptions: object) -> None:
        if isinstance(subscriptions, list):
            self._subscriptions = {str(item) for item in subscriptions}
        self._emit_subscriptions()

    @Slot()
    def request_test_alert(self) -> None:
        if not self._sio.connected:
            return
        try:
            self._sio.emit("alert:test", {"requested_at": time.time()})
        except socketio.exceptions.SocketIOError as exc:
            LOGGER.debug("Unable to request test alert: %s", exc)

    @Slot()
    def disconnect_from_server(self) -> None:
        self._desired_connection = False
        if self._sio.connected:
            self._sio.disconnect()
        else:
            self.state_changed.emit("stopped", "Connection paused")

    @Slot()
    def stop(self) -> None:
        self._desired_connection = False
        if self._timer is not None:
            self._timer.stop()
        if self._sio.connected:
            self._sio.disconnect()

    @Slot()
    def _maintenance(self) -> None:
        now = time.monotonic()
        if (
            self._desired_connection
            and not self._sio.connected
            and not self._connecting
            and now >= self._next_attempt_at
        ):
            self._connect_once()
            return
        if self._sio.connected and now - self._last_ping_at >= 5.0:
            self._send_health_ping()

    def _connect_once(self) -> None:
        if not self._url:
            return
        self._connecting = True
        self.state_changed.emit("connecting", "Negotiating Socket.IO transport")
        try:
            self._sio.connect(
                self._url,
                auth={"subscriptions": sorted(self._subscriptions)},
                wait=True,
                wait_timeout=4,
            )
        except Exception as exc:  # socket/HTTP backends raise several exception types
            self._connecting = False
            self._connected = False
            self.state_changed.emit("disconnected", str(exc) or "Server unavailable")
            self._next_attempt_at = time.monotonic() + self._retry_delay
            self._retry_delay = min(self._retry_delay * 2, 15.0)

    def _emit_subscriptions(self) -> None:
        if not self._sio.connected:
            return
        try:
            self._sio.emit(
                "subscriptions:update",
                {"subscriptions": sorted(self._subscriptions)},
            )
        except socketio.exceptions.SocketIOError as exc:
            LOGGER.debug("Unable to update subscriptions: %s", exc)

    def _send_health_ping(self) -> None:
        nonce = uuid.uuid4().hex
        started_at = time.perf_counter()
        with self._ping_lock:
            self._pending_pings = {
                key: value for key, value in self._pending_pings.items() if started_at - value < 30
            }
            self._pending_pings[nonce] = started_at
        try:
            self._sio.emit("health:ping", {"nonce": nonce})
            self._last_ping_at = time.monotonic()
        except socketio.exceptions.SocketIOError:
            with self._ping_lock:
                self._pending_pings.pop(nonce, None)


class SocketBridge(QObject):
    """Main-thread facade for the worker living in a dedicated Qt thread."""

    state_changed = Signal(str, str)
    alert_received = Signal(object)
    catalog_received = Signal(object)
    subscriptions_confirmed = Signal(object)
    health_updated = Signal(int, str)

    _connect_requested = Signal(str, object)
    _subscriptions_requested = Signal(object)
    _test_requested = Signal()
    _disconnect_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._thread.setObjectName("SignalDeskSocketThread")
        self._worker = SocketWorker()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.initialize)
        self._connect_requested.connect(self._worker.connect_to_server)
        self._subscriptions_requested.connect(self._worker.update_subscriptions)
        self._test_requested.connect(self._worker.request_test_alert)
        self._disconnect_requested.connect(self._worker.disconnect_from_server)

        self._worker.state_changed.connect(self.state_changed)
        self._worker.alert_received.connect(self.alert_received)
        self._worker.catalog_received.connect(self.catalog_received)
        self._worker.subscriptions_confirmed.connect(self.subscriptions_confirmed)
        self._worker.health_updated.connect(self.health_updated)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def connect_server(self, url: str, subscriptions: list[str]) -> None:
        self._connect_requested.emit(url, subscriptions)

    def update_subscriptions(self, subscriptions: list[str]) -> None:
        self._subscriptions_requested.emit(subscriptions)

    def request_test_alert(self) -> None:
        self._test_requested.emit()

    def disconnect_server(self) -> None:
        self._disconnect_requested.emit()

    def stop(self) -> None:
        if not self._thread.isRunning():
            return
        QMetaObject.invokeMethod(
            self._worker,
            "stop",
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        self._thread.quit()
        self._thread.wait(6000)
