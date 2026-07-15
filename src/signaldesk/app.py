"""SignalDesk desktop application entry point."""

from __future__ import annotations

import argparse
import ctypes
import logging
import signal
import sys
import uuid
from contextlib import suppress
from typing import Any

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from signaldesk.config import AppConfig, ConfigStore, ServerConfig, normalize_server_url
from signaldesk.icons import make_app_icon
from signaldesk.models import Alert, Severity, utc_now_iso
from signaldesk.notifications import NotificationManager
from signaldesk.socket_client import SocketManager
from signaldesk.theme import APP_STYLESHEET
from signaldesk.window import ManagementWindow

LOGGER = logging.getLogger("signaldesk")


def _server_label(url: str) -> str:
    remainder = url.split("://", 1)[-1]
    return remainder.split("/", 1)[0] or url


class SignalDeskController(QObject):
    def __init__(
        self,
        app: QApplication,
        config: AppConfig,
        store: ConfigStore,
        *,
        start_hidden: bool,
        disable_tray: bool,
    ) -> None:
        super().__init__(app)
        self.app = app
        self.config = config
        self.store = store
        self._shutting_down = False
        self._test_pending = False
        self._states: dict[str, str] = {server.url: "connecting" for server in config.servers}

        self.tray_available = not disable_tray and QSystemTrayIcon.isSystemTrayAvailable()
        self.app.setQuitOnLastWindowClosed(False)
        self.window = ManagementWindow(config, tray_available=self.tray_available)
        self.notifications = NotificationManager(self)
        self.socket = SocketManager(self)
        self.tray: QSystemTrayIcon | None = None
        self.tray_menu: QMenu | None = None
        self.tray_status_action: QAction | None = None
        self._test_timeout = QTimer(self)
        self._test_timeout.setSingleShot(True)
        self._test_timeout.timeout.connect(self._test_timed_out)

        self._connect_signals()
        if self.tray_available:
            self._create_tray()
        if not start_hidden or not self.tray_available:
            self.window.show()

    def _connect_signals(self) -> None:
        self.window.reconnect_requested.connect(self.socket.reconnect)
        self.window.subscriptions_changed.connect(self._update_subscriptions)
        self.window.servers_changed.connect(self._servers_changed)
        self.window.test_requested.connect(self._request_test)
        self.window.quit_requested.connect(self.shutdown)
        self.notifications.activated.connect(self.window.show_and_activate)

        self.socket.state_changed.connect(self._connection_changed)
        self.socket.health_updated.connect(self.window.set_server_health)
        self.socket.alert_received.connect(self._alert_received)
        self.socket.catalog_received.connect(self.window.set_server_catalog)
        self.socket.subscriptions_confirmed.connect(self.window.set_confirmed_subscriptions)

    def _create_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_app_icon(), self)
        self.tray.setToolTip("SignalDesk — connecting")
        menu = QMenu()
        show_action = QAction("Open alert center", menu)
        show_action.triggered.connect(self.window.show_and_activate)
        menu.addAction(show_action)
        self.tray_status_action = QAction("Status: Connecting", menu)
        self.tray_status_action.setEnabled(False)
        menu.addAction(self.tray_status_action)
        menu.addSeparator()
        test_action = QAction("Send test alert", menu)
        test_action.triggered.connect(self._request_test)
        menu.addAction(test_action)
        reconnect_action = QAction("Reconnect all", menu)
        reconnect_action.triggered.connect(self._reconnect_all)
        menu.addAction(reconnect_action)
        menu.addSeparator()
        quit_action = QAction("Quit SignalDesk", menu)
        quit_action.triggered.connect(self.shutdown)
        menu.addAction(quit_action)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def start(self) -> None:
        for server in self.config.servers:
            self._states[server.url] = "connecting"
            self.window.set_server_state(server.url, "connecting", f"Connecting to {server.url}")
        self.socket.set_servers(self._server_payload())

    def _server_payload(self) -> list[dict[str, object]]:
        return [
            {"url": server.url, "subscriptions": list(server.subscriptions)}
            for server in self.config.servers
        ]

    def _any_connected(self) -> bool:
        return any(state == "connected" for state in self._states.values())

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.window.show_and_activate()

    def _reconnect_all(self) -> None:
        for url in list(self._states):
            self.socket.reconnect(url)

    def _servers_changed(self, servers: Any) -> None:
        if not isinstance(servers, list):
            return
        clean = [item for item in servers if isinstance(item, ServerConfig)]
        self.config.servers = clean
        urls = {server.url for server in clean}
        self._states = {url: self._states.get(url, "connecting") for url in urls}
        self.store.save(self.config)
        self.socket.set_servers(self._server_payload())
        if not self._any_connected() and self._test_pending:
            self._clear_test_pending()

    def _update_subscriptions(self, url: str, subscriptions: Any) -> None:
        if not isinstance(subscriptions, list):
            return
        cleaned = sorted({str(item) for item in subscriptions})
        for server in self.config.servers:
            if server.url == url:
                server.subscriptions = cleaned
                break
        self.store.save(self.config)
        self.socket.update_subscriptions(url, cleaned)

    def _request_test(self) -> None:
        connected = [url for url, state in self._states.items() if state == "connected"]
        if connected:
            if self._test_pending:
                return
            self._test_pending = True
            self.window.set_test_pending(True)
            self._test_timeout.start(4500)
            for url in connected:
                self.socket.request_test(url)
            return
        self._display_alert(
            Alert(
                id=str(uuid.uuid4()),
                title="Local alert preview",
                message="The notification UI is ready. Connect a server to test the socket path.",
                severity=Severity.INFO,
                channel="local-preview",
                source="SignalDesk",
                created_at=utc_now_iso(),
                duration_ms=7000,
            )
        )

    def _test_timed_out(self) -> None:
        if not self._test_pending:
            return
        self._clear_test_pending()
        self._display_alert(
            Alert(
                id=str(uuid.uuid4()),
                title="Socket test timed out",
                message="No server answered alert:test. Check its event handlers, then try again.",
                severity=Severity.WARNING,
                channel="connection",
                source="SignalDesk",
                created_at=utc_now_iso(),
                duration_ms=8000,
            )
        )

    def _clear_test_pending(self) -> None:
        self._test_pending = False
        self._test_timeout.stop()
        self.window.set_test_pending(False)

    def _connection_changed(self, url: str, state: str, detail: str) -> None:
        self._states[url] = state
        clean_detail = " ".join(detail.split()) if detail else "No connection details"
        if len(clean_detail) > 180:
            clean_detail = f"{clean_detail[:179]}…"
        self.window.set_server_state(url, state, clean_detail)
        if not self._any_connected() and self._test_pending:
            self._clear_test_pending()
        self._refresh_tray_status()

    def _refresh_tray_status(self) -> None:
        total = len(self._states)
        online = sum(1 for state in self._states.values() if state == "connected")
        summary = f"{online}/{total} online" if total else "no servers"
        if self.tray is not None:
            self.tray.setToolTip(f"SignalDesk — {summary}")
        if self.tray_status_action is not None:
            self.tray_status_action.setText(f"Status: {summary}")

    def _alert_received(self, url: str, payload: Any) -> None:
        try:
            alert = Alert.from_payload(payload)
        except (TypeError, ValueError) as exc:
            LOGGER.warning("Ignored malformed alert payload: %s", exc)
            return
        if self._test_pending:
            self._clear_test_pending()
        self._display_alert(alert, origin=_server_label(url))

    def _display_alert(self, alert: Alert, origin: str = "") -> None:
        self.window.add_alert(alert, origin)
        self.notifications.show_alert(alert)

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.notifications.dismiss_all()
        if self.tray is not None:
            self.tray.hide()
        self.socket.stop()
        self.window.prepare_to_quit()
        self.window.close()
        self.app.quit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SignalDesk real-time desktop alerts")
    parser.add_argument("--server", help="Override the saved Socket.IO server URL")
    parser.add_argument("--hidden", action="store_true", help="Start directly in the system tray")
    parser.add_argument("--no-tray", action="store_true", help="Disable system tray integration")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser


def _set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    with suppress(AttributeError, OSError):
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SignalDesk.Alerts.0.1")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    _set_windows_app_id()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("SignalDesk")
    app.setApplicationDisplayName("SignalDesk")
    app.setOrganizationName("SignalDesk")
    app.setWindowIcon(make_app_icon())
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)

    store = ConfigStore()
    config = store.load()
    if args.server:
        try:
            url = normalize_server_url(args.server)
        except ValueError as exc:
            build_parser().error(str(exc))
        # Override the endpoint but keep the previously subscribed channels.
        prior_subs = list(config.servers[0].subscriptions) if config.servers else []
        config.servers = [ServerConfig(url=url, subscriptions=prior_subs)]
        store.save(config)

    controller = SignalDeskController(
        app,
        config,
        store,
        start_hidden=args.hidden,
        disable_tray=args.no_tray,
    )
    app.aboutToQuit.connect(controller.shutdown)
    signal.signal(signal.SIGINT, lambda *_: controller.shutdown())
    keep_python_signals_alive = QTimer()
    keep_python_signals_alive.start(500)
    keep_python_signals_alive.timeout.connect(lambda: None)
    QTimer.singleShot(0, controller.start)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
