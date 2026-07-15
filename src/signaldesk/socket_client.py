"""Multi-server Socket.IO transport, isolated from the Qt GUI thread.

The GUI thread never performs I/O. A single worker thread runs a maintenance
timer that services every configured server ``ServerLink``; the blocking
Socket.IO handshake itself runs on a short-lived per-attempt thread so no link
can stall the others (or queued GUI requests). All signals are tagged with the
server URL so the UI can present per-server state.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict

import socketio
from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, QTimer, Signal, Slot

LOGGER = logging.getLogger(__name__)

MAINTENANCE_INTERVAL_MS = 500
PING_INTERVAL_S = 5.0
CONNECT_TIMEOUT_S = 6.0
RETRY_INITIAL_S = 1.0
RETRY_MAX_S = 15.0
# A stuck handshake thread is bounded by CONNECT_TIMEOUT_S; allow a margin
# before the maintenance loop is permitted to start a fresh attempt.
CONNECT_STUCK_S = CONNECT_TIMEOUT_S + 3.0


class PingTracker:
    """Bounded, O(1)-amortized store of in-flight heartbeat nonces.

    Registering a nonce is amortized constant time regardless of how many
    heartbeats are outstanding: expired and over-capacity entries are evicted
    from the front of an ``OrderedDict``. This keeps round-trip measurement
    latency flat even under a flood of thousands of heartbeats.
    """

    def __init__(self, capacity: int = 1024, ttl_s: float = 30.0) -> None:
        self._pending: OrderedDict[str, float] = OrderedDict()
        self._capacity = max(1, capacity)
        self._ttl_s = ttl_s
        self._lock = threading.Lock()

    def register(self, nonce: str, now: float | None = None) -> None:
        moment = time.perf_counter() if now is None else now
        with self._lock:
            self._pending[nonce] = moment
            self._pending.move_to_end(nonce)
            self._prune_locked(moment)

    def resolve(self, nonce: str, now: float | None = None) -> float | None:
        """Return the round-trip time in milliseconds, or None if unknown."""
        moment = time.perf_counter() if now is None else now
        with self._lock:
            started = self._pending.pop(nonce, None)
        if started is None:
            return None
        return max(0.0, (moment - started) * 1000.0)

    def _prune_locked(self, now: float) -> None:
        pending = self._pending
        while pending:
            oldest = next(iter(pending))
            expired = now - pending[oldest] > self._ttl_s
            if not expired and len(pending) <= self._capacity:
                break
            pending.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)


class ServerLink:
    """One Socket.IO connection plus its reconnect/heartbeat state.

    State touched by both the maintenance thread and Socket.IO's own callback
    thread is guarded by ``_lock``.
    """

    def __init__(self, hub: SocketHub, url: str, subscriptions: set[str]) -> None:
        self._hub = hub
        self._url = url
        self._lock = threading.Lock()
        self._subscriptions = set(subscriptions)
        self._desired = True
        self._connected = False
        self._connecting = False
        self._connect_started = 0.0
        self._next_attempt_at = 0.0
        self._retry_delay = RETRY_INITIAL_S
        self._last_ping_at = 0.0
        self._pings = PingTracker()
        self._sio = socketio.Client(
            reconnection=False,
            logger=False,
            engineio_logger=False,
            handle_sigint=False,
        )
        self._register_handlers()

    @property
    def url(self) -> str:
        return self._url

    def _register_handlers(self) -> None:
        @self._sio.event
        def connect() -> None:
            with self._lock:
                self._connected = True
                self._connecting = False
                self._retry_delay = RETRY_INITIAL_S
                self._last_ping_at = 0.0
            self._hub.emit_state(self._url, "connected", self._transport_name())

        @self._sio.event
        def connect_error(data: object) -> None:
            detail = data.get("message", str(data)) if isinstance(data, dict) else str(data)
            with self._lock:
                self._connected = False
                self._connecting = False
            self._hub.emit_state(self._url, "disconnected", detail or "Connection refused")

        @self._sio.event
        def disconnect(reason: object = None) -> None:
            with self._lock:
                self._connected = False
                self._connecting = False
                self._next_attempt_at = time.monotonic() + self._retry_delay
                desired = self._desired
            state = "disconnected" if desired else "stopped"
            self._hub.emit_state(self._url, state, str(reason or "Connection closed"))

        @self._sio.on("alert")
        def on_alert(data: object) -> None:
            if isinstance(data, dict):
                self._hub.emit_alert(self._url, data)

        @self._sio.on("catalog")
        def on_catalog(data: object) -> None:
            self._hub.emit_catalog(self._url, data)

        @self._sio.on("subscriptions:confirmed")
        def on_subscriptions(data: object) -> None:
            if isinstance(data, dict):
                self._hub.emit_subscriptions(self._url, data.get("subscriptions", []))
            elif isinstance(data, list):
                self._hub.emit_subscriptions(self._url, data)

        @self._sio.on("health:pong")
        def on_health_pong(data: object) -> None:
            if not isinstance(data, dict):
                return
            rtt_ms = self._pings.resolve(str(data.get("nonce", "")))
            if rtt_ms is not None:
                self._hub.emit_health(self._url, round(rtt_ms), self._transport_name())

    def _transport_name(self) -> str:
        try:
            transport = self._sio.transport
            return str(transport() if callable(transport) else transport).title()
        except (AttributeError, TypeError, ValueError):
            return "Socket.IO"

    # --- called on the maintenance (worker) thread -----------------------

    def service(self, now: float) -> None:
        """Decide whether to (re)connect or heartbeat this link."""
        due_connect = False
        due_ping = False
        with self._lock:
            if not self._desired:
                return
            if self._connecting and now - self._connect_started > CONNECT_STUCK_S:
                self._connecting = False
            if not self._connected and not self._connecting and now >= self._next_attempt_at:
                self._connecting = True
                self._connect_started = now
                due_connect = True
            elif self._connected and now - self._last_ping_at >= PING_INTERVAL_S:
                self._last_ping_at = now
                due_ping = True
        if due_connect:
            self._spawn_connect()
        elif due_ping:
            self._send_ping()

    def set_subscriptions(self, subscriptions: set[str]) -> None:
        with self._lock:
            self._subscriptions = set(subscriptions)
            connected = self._connected
        if connected:
            self._emit_subscriptions()

    def request_reconnect(self) -> None:
        with self._lock:
            self._desired = True
            self._next_attempt_at = 0.0
            self._retry_delay = RETRY_INITIAL_S
            connected = self._connected
        if connected:
            self._safe_disconnect()

    def request_test(self) -> None:
        if not self._sio.connected:
            return
        with suppress_socketio():
            self._sio.emit("alert:test", {"requested_at": time.time()})

    def shutdown(self) -> None:
        with self._lock:
            self._desired = False
        self._safe_disconnect()

    # --- helpers ---------------------------------------------------------

    def _spawn_connect(self) -> None:
        with self._lock:
            subscriptions = sorted(self._subscriptions)
        self._hub.emit_state(self._url, "connecting", "Negotiating Socket.IO transport")
        thread = threading.Thread(
            target=self._connect_worker,
            args=(subscriptions,),
            name=f"connect-{self._url}",
            daemon=True,
        )
        thread.start()

    def _connect_worker(self, subscriptions: list[str]) -> None:
        if self._sio.connected:
            return
        try:
            self._sio.connect(
                self._url,
                auth={"subscriptions": subscriptions},
                wait=True,
                wait_timeout=CONNECT_TIMEOUT_S,
            )
        except Exception as exc:  # socket/HTTP backends raise several types
            with self._lock:
                self._connecting = False
                self._connected = False
                self._next_attempt_at = time.monotonic() + self._retry_delay
                self._retry_delay = min(self._retry_delay * 2, RETRY_MAX_S)
                desired = self._desired
            state = "disconnected" if desired else "stopped"
            self._hub.emit_state(self._url, state, str(exc) or "Server unavailable")

    def _emit_subscriptions(self) -> None:
        with self._lock:
            subscriptions = sorted(self._subscriptions)
        if not self._sio.connected:
            return
        with suppress_socketio():
            self._sio.emit("subscriptions:update", {"subscriptions": subscriptions})

    def _send_ping(self) -> None:
        nonce = uuid.uuid4().hex
        self._pings.register(nonce)
        try:
            self._sio.emit("health:ping", {"nonce": nonce})
        except socketio.exceptions.SocketIOError:
            self._pings.resolve(nonce)

    def _safe_disconnect(self) -> None:
        with suppress_socketio():
            if self._sio.connected:
                self._sio.disconnect()


class suppress_socketio:
    """Context manager swallowing Socket.IO emit/disconnect errors."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc_type is not None and issubclass(exc_type, socketio.exceptions.SocketIOError):
            LOGGER.debug("Socket.IO operation failed: %s", exc)
            return True
        return False


class SocketHub(QObject):
    """Owns every ``ServerLink`` and the shared maintenance timer."""

    state_changed = Signal(str, str, str)
    alert_received = Signal(str, object)
    catalog_received = Signal(str, object)
    subscriptions_confirmed = Signal(str, object)
    health_updated = Signal(str, int, str)

    def __init__(self) -> None:
        super().__init__()
        self._links: dict[str, ServerLink] = {}
        self._timer: QTimer | None = None

    @Slot()
    def initialize(self) -> None:
        if self._timer is not None:
            return
        self._timer = QTimer(self)
        self._timer.setInterval(MAINTENANCE_INTERVAL_MS)
        self._timer.timeout.connect(self._maintenance)
        self._timer.start()

    @Slot(object)
    def set_servers(self, servers: object) -> None:
        if not isinstance(servers, list):
            return
        desired: dict[str, set[str]] = {}
        for entry in servers:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url", ""))
            if not url:
                continue
            subs = entry.get("subscriptions", [])
            desired[url] = {str(item) for item in subs} if isinstance(subs, list) else set()

        for url in list(self._links):
            if url not in desired:
                self._links.pop(url).shutdown()

        for url, subs in desired.items():
            link = self._links.get(url)
            if link is None:
                self._links[url] = ServerLink(self, url, subs)
                self.state_changed.emit(url, "connecting", "Opening the event channel")
            else:
                link.set_subscriptions(subs)

    @Slot(str, object)
    def update_subscriptions(self, url: str, subscriptions: object) -> None:
        link = self._links.get(url)
        if link is None:
            return
        subs = {str(item) for item in subscriptions} if isinstance(subscriptions, list) else set()
        link.set_subscriptions(subs)

    @Slot(str)
    def request_reconnect(self, url: str) -> None:
        link = self._links.get(url)
        if link is not None:
            link.request_reconnect()

    @Slot(str)
    def request_test(self, url: str) -> None:
        link = self._links.get(url)
        if link is not None:
            link.request_test()

    @Slot()
    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        for link in self._links.values():
            link.shutdown()
        self._links.clear()

    @Slot()
    def _maintenance(self) -> None:
        now = time.monotonic()
        for link in list(self._links.values()):
            link.service(now)

    # Called from Socket.IO callback threads; Qt signal emission is thread-safe.
    def emit_state(self, url: str, state: str, detail: str) -> None:
        self.state_changed.emit(url, state, detail)

    def emit_alert(self, url: str, payload: object) -> None:
        self.alert_received.emit(url, payload)

    def emit_catalog(self, url: str, payload: object) -> None:
        self.catalog_received.emit(url, payload)

    def emit_subscriptions(self, url: str, payload: object) -> None:
        self.subscriptions_confirmed.emit(url, payload)

    def emit_health(self, url: str, rtt_ms: int, transport: str) -> None:
        self.health_updated.emit(url, rtt_ms, transport)


class SocketManager(QObject):
    """Main-thread facade for the hub living in a dedicated Qt thread."""

    state_changed = Signal(str, str, str)
    alert_received = Signal(str, object)
    catalog_received = Signal(str, object)
    subscriptions_confirmed = Signal(str, object)
    health_updated = Signal(str, int, str)

    _servers_requested = Signal(object)
    _subscriptions_requested = Signal(str, object)
    _reconnect_requested = Signal(str)
    _test_requested = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._thread.setObjectName("SignalDeskSocketThread")
        self._hub = SocketHub()
        self._hub.moveToThread(self._thread)

        self._thread.started.connect(self._hub.initialize)
        self._servers_requested.connect(self._hub.set_servers)
        self._subscriptions_requested.connect(self._hub.update_subscriptions)
        self._reconnect_requested.connect(self._hub.request_reconnect)
        self._test_requested.connect(self._hub.request_test)

        self._hub.state_changed.connect(self.state_changed)
        self._hub.alert_received.connect(self.alert_received)
        self._hub.catalog_received.connect(self.catalog_received)
        self._hub.subscriptions_confirmed.connect(self.subscriptions_confirmed)
        self._hub.health_updated.connect(self.health_updated)
        self._thread.finished.connect(self._hub.deleteLater)
        self._thread.start()

    def set_servers(self, servers: list[dict[str, object]]) -> None:
        self._servers_requested.emit(servers)

    def update_subscriptions(self, url: str, subscriptions: list[str]) -> None:
        self._subscriptions_requested.emit(url, subscriptions)

    def reconnect(self, url: str) -> None:
        self._reconnect_requested.emit(url)

    def request_test(self, url: str) -> None:
        self._test_requested.emit(url)

    def stop(self) -> None:
        if not self._thread.isRunning():
            return
        QMetaObject.invokeMethod(
            self._hub,
            "stop",
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        self._thread.quit()
        self._thread.wait(6000)
