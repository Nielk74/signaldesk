"""SignalDesk desktop application entry point."""

from __future__ import annotations

import argparse
import ctypes
import logging
import signal
import sqlite3
import sys
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox, QSystemTrayIcon

from signaldesk.config import AppConfig, ConfigStore, ServerConfig, normalize_server_url
from signaldesk.credentials import CredentialUnavailable, delete_token, get_token, set_token
from signaldesk.history import AlertStore, StoredAlert
from signaldesk.icons import make_app_icon
from signaldesk.models import Alert, AlertAction, AlertLifecycle, Severity, utc_now_iso
from signaldesk.notifications import NotificationManager
from signaldesk.policies import NoisePolicy, NotificationPolicyEngine
from signaldesk.reliability import ConnectionWatchdog, WatchdogEvent
from signaldesk.single_instance import SingleInstanceError, SingleInstanceGuard
from signaldesk.socket_client import SocketManager
from signaldesk.sounds import SoundPlayer
from signaldesk.startup import StartupIntegrationError, set_launch_at_login
from signaldesk.theme import APP_STYLESHEET
from signaldesk.window import ManagementWindow

LOGGER = logging.getLogger("signaldesk")
LOCAL_SERVER_URL = "local://signaldesk"
MAX_VISIBLE_HISTORY = 5000
HISTORY_REFRESH_INTERVAL_MS = 250


def _server_label(url: str) -> str:
    remainder = url.split("://", 1)[-1]
    return remainder.split("/", 1)[0] or url


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _utc_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class SignalDeskController(QObject):
    def __init__(
        self,
        app: QApplication,
        config: AppConfig,
        store: ConfigStore,
        *,
        start_hidden: bool,
        disable_tray: bool,
        history_store: AlertStore | None = None,
    ) -> None:
        super().__init__(app)
        self.app = app
        self.config = config
        self.store = store
        self.history = history_store or AlertStore()
        self._close_history_on_shutdown = history_store is None
        self.policy_engine = NotificationPolicyEngine(config.noise_policy)
        self.watchdog = ConnectionWatchdog(config.disconnect_warning_seconds)
        self._shutting_down = False
        self._test_pending = False
        self._states: dict[str, str] = {server.url: "connecting" for server in config.servers}
        self._last_connected: dict[str, str] = {}
        self._requested_cursors: dict[str, int | None] = {}
        self._pending_lifecycle: dict[
            tuple[str, str], tuple[str, str | None, str | None]
        ] = {}
        self._history_filters: dict[str, str] = {}
        self._last_snooze_check = 0.0

        self.tray_available = not disable_tray and QSystemTrayIcon.isSystemTrayAvailable()
        self.app.setQuitOnLastWindowClosed(False)
        self.window = ManagementWindow(config, tray_available=self.tray_available)
        self.notifications = NotificationManager(self)
        self.sounds = SoundPlayer(self)
        self.socket = SocketManager(self)
        self.tray: QSystemTrayIcon | None = None
        self.tray_menu: QMenu | None = None
        self.tray_status_action: QAction | None = None
        self._test_timeout = QTimer(self)
        self._test_timeout.setSingleShot(True)
        self._test_timeout.timeout.connect(self._test_timed_out)
        self._maintenance_timer = QTimer(self)
        self._maintenance_timer.setInterval(1000)
        self._maintenance_timer.timeout.connect(self._maintenance_tick)
        self._history_refresh_timer = QTimer(self)
        self._history_refresh_timer.setSingleShot(True)
        self._history_refresh_timer.setInterval(HISTORY_REFRESH_INTERVAL_MS)
        self._history_refresh_timer.timeout.connect(self._refresh_history)

        self._connect_signals()
        self.history.prune(
            retention_days=self.config.retention_days,
            max_rows=self.config.max_history,
        )
        self._refresh_history()
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
        self.window.sound_enabled_changed.connect(self._set_sound_enabled)
        self.window.sound_changed.connect(self._set_sound)
        self.window.sound_preview_requested.connect(self.sounds.play)
        self.window.lifecycle_requested.connect(self._request_lifecycle)
        self.window.alert_action_requested.connect(self._open_alert_action)
        self.window.history_filters_changed.connect(self._history_filters_changed)
        self.window.history_export_requested.connect(self._export_history)
        self.window.retention_changed.connect(self._set_retention)
        self.window.clear_history_requested.connect(self._clear_history)
        self.window.policy_changed.connect(self._set_policy)
        self.window.auth_save_requested.connect(self._save_auth_token)
        self.window.auth_clear_requested.connect(self._clear_auth_token)
        self.window.launch_at_login_changed.connect(self._set_launch_at_login)
        self.window.watchdog_threshold_changed.connect(self._set_watchdog_threshold)
        self.notifications.activated.connect(self._open_notification_detail)
        self.notifications.overflow_activated.connect(self.window.open_history)
        self.notifications.lifecycle_requested.connect(self._request_lifecycle)
        self.notifications.action_requested.connect(self._open_alert_action)

        self.socket.state_changed.connect(self._connection_changed)
        self.socket.health_updated.connect(self.window.set_server_health)
        self.socket.alert_received.connect(self._alert_received)
        self.socket.catalog_received.connect(self.window.set_server_catalog)
        self.socket.subscriptions_confirmed.connect(self.window.set_confirmed_subscriptions)
        self.socket.recovery_completed.connect(self._recovery_completed)
        self.socket.lifecycle_confirmed.connect(self._lifecycle_confirmed)

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
        urls = {server.url for server in self.config.servers}
        self.watchdog.set_servers(urls)
        for server in self.config.servers:
            self._states[server.url] = "connecting"
            self.window.set_server_state(server.url, "connecting", f"Connecting to {server.url}")
        self.socket.set_servers(self._server_payload())
        self._maintenance_timer.start()
        self._refresh_tray_status()

    def _server_payload(self) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        healed_config = False
        for server in self.config.servers:
            cursor = self.history.get_cursor(server.url)
            self._requested_cursors[server.url] = cursor
            entry: dict[str, object] = {
                "url": server.url,
                "subscriptions": list(server.subscriptions),
                "client_id": self.config.client_id,
            }
            if cursor is not None:
                entry["resume_after"] = cursor

            if server.auth_enabled:
                try:
                    token = get_token(server.url)
                except CredentialUnavailable as exc:
                    LOGGER.warning("Secure credentials unavailable for %s: %s", server.url, exc)
                    self.window.set_server_auth_status(server.url, True, False)
                else:
                    if token:
                        entry["token"] = token
                        self.window.set_server_auth_status(server.url, True, True)
                    else:
                        # Heal a stale non-secret flag when its keyring entry no longer exists.
                        server.auth_enabled = False
                        healed_config = True
                        self.window.set_server_auth_status(server.url, False, True)
            else:
                self.window.set_server_auth_status(server.url, False, True)
            payload.append(entry)

        if healed_config:
            self.store.save(self.config)
        return payload

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
        previous = {server.url: server for server in self.config.servers}
        self.config.servers = clean
        urls = {server.url for server in clean}
        for removed_url in previous.keys() - urls:
            self._pending_lifecycle = {
                key: value
                for key, value in self._pending_lifecycle.items()
                if key[0] != removed_url
            }
            if previous[removed_url].auth_enabled:
                try:
                    delete_token(removed_url)
                except CredentialUnavailable as exc:
                    LOGGER.warning(
                        "Could not remove secure credentials for %s: %s", removed_url, exc
                    )
                    self.window.set_recovery_banner(
                        f"The server was removed, but its secure token could not be deleted: {exc}",
                        title="Stored token unavailable",
                        severity="warning",
                    )
        self._states = {url: self._states.get(url, "connecting") for url in urls}
        self.watchdog.set_servers(urls)
        self.store.save(self.config)
        self.socket.set_servers(self._server_payload())
        if not self._any_connected() and self._test_pending:
            self._clear_test_pending()
        self._refresh_tray_status()

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
        watchdog_event = self.watchdog.change(url, state)
        if state == "connected":
            connected_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
            self._last_connected[url] = connected_at
            self.window.set_server_reliability(
                url,
                last_connected=connected_at,
                offline_since=None,
            )
            self._flush_pending_lifecycle(url)
        else:
            self.window.set_server_reliability(
                url,
                last_connected=self._last_connected.get(url),
                offline_since=self.watchdog.offline_seconds(url),
            )
        if watchdog_event is not None:
            self._handle_watchdog_event(watchdog_event)
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
        if not isinstance(payload, Mapping):
            LOGGER.warning("Ignored malformed alert payload from %s", url)
            return
        try:
            alert = Alert.from_payload(payload)
        except (TypeError, ValueError) as exc:
            LOGGER.warning("Ignored malformed alert payload: %s", exc)
            return
        if self._test_pending:
            self._clear_test_pending()
        self._record_alert(
            url,
            alert,
            raw_payload=payload,
            notify=not bool(payload.get("replayed", False)),
        )

    def _set_sound_enabled(self, enabled: bool) -> None:
        self.config.sound_enabled = bool(enabled)
        self.store.save(self.config)

    def _set_sound(self, severity: str, sound_id: str) -> None:
        if severity in self.config.sounds:
            self.config.sounds[severity] = sound_id
            self.store.save(self.config)

    def _display_alert(self, alert: Alert, origin: str = "") -> None:
        """Persist and route a local alert (kept for previews and watchdog events)."""
        self._record_alert(
            LOCAL_SERVER_URL,
            alert,
            raw_payload=alert.to_payload(),
            notify=True,
            origin=origin or "SignalDesk",
        )

    def _record_alert(
        self,
        server_url: str,
        alert: Alert,
        *,
        raw_payload: Mapping[str, Any] | None,
        notify: bool,
        origin: str | None = None,
    ) -> StoredAlert | None:
        """Commit an alert before advancing its replay cursor or interrupting the user."""
        try:
            existing = self.history.get(server_url, alert.id)
            stored = self.history.upsert(
                server_url,
                alert,
                origin=origin or self._server_display_name(server_url),
                raw_payload=raw_payload,
            )
            if stored.sequence is not None and server_url != LOCAL_SERVER_URL:
                self.socket.update_resume_after(server_url, stored.sequence)
            self.history.prune(
                retention_days=self.config.retention_days,
                max_rows=self.config.max_history,
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            LOGGER.exception("Could not persist alert %s from %s", alert.id, server_url)
            self.window.set_recovery_banner(
                f"SignalDesk could not safely store an incoming alert: {exc}",
                title="Persistent inbox error",
                severity="critical",
            )
            return None

        if notify and existing is None:
            self._route_alert(stored.alert, server_url)
        self._schedule_history_refresh()
        return stored

    def _route_alert(self, alert: Alert, server_url: str) -> None:
        decision = self.policy_engine.decide(alert, server_url)
        if decision.show_toast:
            # The full URL is retained as the lifecycle/action routing key.
            self.notifications.show_alert(alert, origin=server_url)
        elif decision.reason == "repeat_cooldown":
            # Repeat grouping is a noise control, not silent delivery. Keep a
            # transient on-screen count while every full alert remains in history.
            self.notifications.aggregate_alerts()
        if decision.play_sound and self.config.sound_enabled:
            self.sounds.play(self.config.sounds.get(alert.severity.value, ""))

    def _open_notification_detail(self, server_url: str, alert_id: str) -> None:
        """Bring the durable alert record forward when its toast is activated."""
        self.window.show_and_activate()
        try:
            record = self.history.get(server_url, alert_id)
        except (RuntimeError, ValueError, sqlite3.Error) as exc:
            LOGGER.exception("Could not open notification detail for %s", alert_id)
            self.window.set_recovery_banner(
                f"The alert detail could not be loaded from the inbox: {exc}",
                title="Alert detail unavailable",
                severity="warning",
            )
            return
        if record is not None:
            self.window.open_alert_detail(record)
            return
        self.window.set_recovery_banner(
            "This notification is no longer present in the retained alert history.",
            title="Alert detail unavailable",
            severity="warning",
        )

    def _server_display_name(self, server_url: str) -> str:
        if server_url == LOCAL_SERVER_URL:
            return "SignalDesk"
        for server in self.config.servers:
            if server.url == server_url:
                return server.name or _server_label(server_url)
        return _server_label(server_url)

    # --- persistent inbox -------------------------------------------------

    def _schedule_history_refresh(self) -> None:
        """Coalesce GUI history updates so alert delivery never waits on row rendering."""
        if not self._history_refresh_timer.isActive():
            self._history_refresh_timer.start()

    def _refresh_history(self) -> None:
        self._history_refresh_timer.stop()
        try:
            limit = max(1, min(int(self.config.max_history), MAX_VISIBLE_HISTORY))
            records = self.history.query(limit=limit)
        except (RuntimeError, ValueError, sqlite3.Error) as exc:
            LOGGER.exception("Could not refresh alert history")
            self.window.set_recovery_banner(
                f"Stored alerts could not be loaded: {exc}",
                title="Persistent inbox error",
                severity="critical",
            )
            return
        self.window.replace_history(records)
        self._refresh_tray_status()

    def _history_filters_changed(self, filters: object) -> None:
        if not isinstance(filters, Mapping):
            return
        self._history_filters = {
            key: str(filters.get(key, "") or "").strip()
            for key in ("search", "severity", "server", "channel")
        }

    @staticmethod
    def _history_query_args(filters: object) -> dict[str, str | None]:
        mapping = filters if isinstance(filters, Mapping) else {}
        return {
            "text": str(mapping.get("search", "") or "").strip() or None,
            "severity": str(mapping.get("severity", "") or "").strip() or None,
            "server": str(mapping.get("server", "") or "").strip() or None,
            "channel": str(mapping.get("channel", "") or "").strip() or None,
        }

    def _export_history(self, filters: object) -> None:
        path_text, selected_filter = QFileDialog.getSaveFileName(
            self.window,
            "Export alert history",
            "signaldesk-history.json",
            "JSON files (*.json);;CSV files (*.csv)",
        )
        if not path_text:
            return
        path = Path(path_text)
        csv_selected = path.suffix.lower() == ".csv" or "CSV" in selected_filter
        if not path.suffix:
            path = path.with_suffix(".csv" if csv_selected else ".json")
        try:
            query = self._history_query_args(filters)
            content = (
                self.history.export_csv(**query)
                if csv_selected
                else self.history.export_json(**query)
            )
            path.write_text(content, encoding="utf-8")
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(
                self.window,
                "History export failed",
                f"SignalDesk could not write the export:\n{exc}",
            )
            return
        self.window.set_recovery_banner(
            f"Filtered alert history was written to {path}.",
            title="History exported",
            severity="success",
        )

    def _set_retention(self, days: int) -> None:
        value = max(1, min(int(days), 3650))
        self.config.retention_days = value
        self.store.save(self.config)
        try:
            self.history.prune(retention_days=value, max_rows=self.config.max_history)
        except (RuntimeError, ValueError, sqlite3.Error) as exc:
            self.window.set_recovery_banner(
                f"The retention policy was saved, but history could not be pruned: {exc}",
                title="History retention warning",
                severity="warning",
            )
            return
        self._schedule_history_refresh()

    def _clear_history(self) -> None:
        try:
            removed = self.history.clear()
        except sqlite3.Error as exc:
            self.window.set_recovery_banner(
                f"Stored alerts could not be cleared: {exc}",
                title="Clear history failed",
                severity="critical",
            )
            return
        self._pending_lifecycle.clear()
        self._refresh_history()
        self.window.set_recovery_banner(
            f"Removed {removed} stored alert{'s' if removed != 1 else ''}. Replay cursors were kept.",
            title="Alert history cleared",
            severity="success",
        )

    # --- reminders and alert actions -------------------------------------

    def _request_lifecycle(
        self,
        server_url: str,
        alert_id: str,
        status: str,
        snoozed_until: object = None,
        note: str = "",
    ) -> None:
        try:
            lifecycle = AlertLifecycle(str(status).strip().lower())
        except ValueError:
            return
        if lifecycle not in {AlertLifecycle.UNREAD, AlertLifecycle.SNOOZED}:
            return
        until = str(snoozed_until).strip() if snoozed_until else None
        clean_note = str(note or "").strip()
        try:
            updated = self.history.update_lifecycle(
                server_url,
                alert_id,
                lifecycle,
                snoozed_until=until,
                note=clean_note,
            )
        except ValueError as exc:
            self.window.set_recovery_banner(
                str(exc),
                title="Reminder update rejected",
                severity="warning",
            )
            return
        if updated is None:
            return
        self._schedule_history_refresh()

        if server_url in self._states:
            payload = (lifecycle.value, until, clean_note or None)
            self._pending_lifecycle[(server_url, alert_id)] = payload
            if self._states.get(server_url) == "connected":
                self._send_lifecycle(server_url, alert_id, payload)
            else:
                self.window.set_recovery_banner(
                    "The reminder change is saved locally and will be sent when the server reconnects.",
                    title="Reminder update queued",
                    severity="info",
                )

    def _send_lifecycle(
        self,
        server_url: str,
        alert_id: str,
        payload: tuple[str, str | None, str | None],
    ) -> None:
        status, snoozed_until, note = payload
        try:
            self.socket.request_lifecycle(
                server_url,
                alert_id,
                status,
                snoozed_until=snoozed_until,
                note=note,
            )
        except ValueError as exc:
            self._pending_lifecycle.pop((server_url, alert_id), None)
            self.window.set_recovery_banner(
                str(exc),
                title="Reminder update rejected",
                severity="warning",
            )

    def _flush_pending_lifecycle(self, server_url: str) -> None:
        for (url, alert_id), payload in list(self._pending_lifecycle.items()):
            if url == server_url:
                self._send_lifecycle(url, alert_id, payload)

    def _lifecycle_confirmed(self, server_url: str, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        alert_id = str(payload.get("id", "") or "").strip()
        if payload.get("ok") is False:
            if alert_id:
                self._pending_lifecycle.pop((server_url, alert_id), None)
            error = str(payload.get("error", "The server rejected the reminder update"))
            self.window.set_recovery_banner(
                error,
                title="Reminder update was not accepted",
                severity="warning",
            )
            return
        try:
            supplied_lifecycle = AlertLifecycle(
                str(payload.get("status", "")).strip().lower()
            )
        except ValueError:
            return
        lifecycle = AlertLifecycle.parse(supplied_lifecycle)
        if not alert_id:
            return
        until = str(payload.get("snoozed_until", "") or "").strip() or None
        note = str(payload.get("note", "") or "").strip() if "note" in payload else None
        try:
            updated = self.history.update_lifecycle(
                server_url,
                alert_id,
                lifecycle,
                snoozed_until=until,
                note=note,
            )
        except ValueError as exc:
            LOGGER.warning("Ignored malformed reminder confirmation from %s: %s", server_url, exc)
            return
        if updated is not None:
            self._pending_lifecycle.pop((server_url, alert_id), None)
            self._schedule_history_refresh()

    def _open_alert_action(self, server_url: str, alert_id: str, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        try:
            action = AlertAction.from_payload(payload)
        except (TypeError, ValueError) as exc:
            LOGGER.warning("Rejected unsafe alert action: %s", exc)
            return
        record = self.history.get(server_url, alert_id)
        if record is None or not any(
            candidate.label == action.label and candidate.url == action.url
            for candidate in record.alert.actions
        ):
            LOGGER.warning("Rejected action not present on alert %s from %s", alert_id, server_url)
            return
        if not QDesktopServices.openUrl(QUrl(action.url)):
            QMessageBox.warning(
                self.window,
                "Could not open alert action",
                "The system could not open the validated action URL.",
            )

    # --- catch-up delivery ------------------------------------------------

    def _recovery_completed(self, server_url: str, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        latest = _non_negative_int(payload.get("latest_sequence"))
        if latest is not None:
            try:
                self.history.set_cursor(server_url, latest)
            except (RuntimeError, ValueError, sqlite3.Error) as exc:
                self.window.set_recovery_banner(
                    f"The recovery checkpoint could not be saved: {exc}",
                    title="Catch-up checkpoint failed",
                    severity="critical",
                )
                return
            self.socket.update_resume_after(server_url, latest)

        recovered = _non_negative_int(payload.get("recovered_count")) or 0
        gap = bool(payload.get("gap", False) or payload.get("truncated", False))
        if gap:
            start_cursor = self._requested_cursors.get(server_url)
            oldest = _non_negative_int(payload.get("oldest_available_sequence"))
            unavailable = "some earlier events"
            if start_cursor is not None and oldest is not None and oldest > start_cursor + 1:
                unavailable = f"sequences {start_cursor + 1}–{oldest - 1}"
            self.window.show_recovery_gap(
                server_url,
                detail=(
                    f"{self._server_display_name(server_url)} replayed {recovered} retained "
                    f"alert{'s' if recovered != 1 else ''}, but {unavailable} are no longer "
                    "available on the server. Review the retained alert history."
                ),
            )
        elif recovered:
            self.window.set_recovery_banner(
                f"Recovered {recovered} alert{'s' if recovered != 1 else ''} from "
                f"{self._server_display_name(server_url)} and added them to history.",
                title="Catch-up delivery complete",
                severity="success",
            )
        self._schedule_history_refresh()

    # --- policy, credentials, and reliability ----------------------------

    def _set_policy(self, payload: object) -> None:
        policy = NoisePolicy.from_mapping(dict(payload) if isinstance(payload, Mapping) else {})
        self.config.noise_policy = policy
        self.policy_engine.set_policy(policy)
        self.store.save(self.config)

    def _save_auth_token(self, server_url: str, token: str) -> None:
        server = next((item for item in self.config.servers if item.url == server_url), None)
        if server is None:
            return
        previous = server.auth_enabled
        try:
            set_token(server_url, token)
        except (CredentialUnavailable, ValueError) as exc:
            self.window.set_server_auth_status(server_url, previous, False)
            self.window.set_recovery_banner(
                str(exc),
                title="Token was not saved",
                severity="warning",
            )
            return
        server.auth_enabled = True
        self.store.save(self.config)
        self.window.set_server_auth_status(server_url, True, True)
        self.socket.set_servers(self._server_payload())

    def _clear_auth_token(self, server_url: str) -> None:
        server = next((item for item in self.config.servers if item.url == server_url), None)
        if server is None:
            return
        previous = server.auth_enabled
        try:
            delete_token(server_url)
        except CredentialUnavailable as exc:
            self.window.set_server_auth_status(server_url, previous, False)
            self.window.set_recovery_banner(
                str(exc),
                title="Token was not cleared",
                severity="warning",
            )
            return
        server.auth_enabled = False
        self.store.save(self.config)
        self.window.set_server_auth_status(server_url, False, True)
        self.socket.set_servers(self._server_payload())

    def _set_launch_at_login(self, enabled: bool) -> None:
        previous = self.config.launch_at_login
        try:
            set_launch_at_login(bool(enabled))
        except StartupIntegrationError as exc:
            self.window.set_launch_at_login(previous)
            self.window.set_recovery_banner(
                str(exc),
                title="Launch at login could not be changed",
                severity="warning",
            )
            return
        self.config.launch_at_login = bool(enabled)
        self.store.save(self.config)

    def _set_watchdog_threshold(self, seconds: int) -> None:
        self.watchdog.set_threshold(seconds)
        self.config.disconnect_warning_seconds = self.watchdog.threshold_seconds
        self.store.save(self.config)

    def _maintenance_tick(self) -> None:
        for event in self.watchdog.poll():
            self._handle_watchdog_event(event)
        for url, state in self._states.items():
            if state != "connected":
                self.window.set_server_reliability(
                    url,
                    last_connected=self._last_connected.get(url),
                    offline_since=self.watchdog.offline_seconds(url),
                )

        now = time.monotonic()
        if now - self._last_snooze_check >= 5:
            self._last_snooze_check = now
            self._wake_due_snoozes()

    def _handle_watchdog_event(self, event: WatchdogEvent) -> None:
        label = self._server_display_name(event.server_url)
        if event.kind == "offline":
            title = f"{label} has been offline"
            message = (
                f"No connection has been available for {event.offline_seconds} seconds. "
                "SignalDesk will keep retrying and request catch-up delivery after reconnecting."
            )
            severity = Severity.WARNING
            requires_attention = True
        elif event.kind == "restored":
            title = f"{label} connection restored"
            message = (
                f"The server is reachable again after about {event.offline_seconds} seconds. "
                "Catch-up delivery is being checked."
            )
            severity = Severity.SUCCESS
            requires_attention = False
        else:
            return
        self._display_alert(
            Alert(
                id=str(uuid.uuid4()),
                title=title,
                message=message,
                severity=severity,
                channel="connection",
                source="SignalDesk watchdog",
                created_at=utc_now_iso(),
                duration_ms=9000,
                requires_attention=requires_attention,
            ),
            origin=label,
        )

    def _wake_due_snoozes(self) -> None:
        now = datetime.now(UTC)
        try:
            snoozed = self.history.query(
                status=AlertLifecycle.SNOOZED,
                limit=MAX_VISIBLE_HISTORY,
            )
            due = [
                record
                for record in snoozed
                if record.snoozed_until
                and (parsed := _utc_datetime(record.snoozed_until)) is not None
                and parsed <= now
            ]
            woke = self.history.wake_snoozed(now)
        except (RuntimeError, ValueError, sqlite3.Error):
            LOGGER.exception("Could not wake snoozed alerts")
            return
        if not woke:
            return
        self._schedule_history_refresh()
        for record in due:
            self._pending_lifecycle.pop((record.server_url, record.alert.id), None)
            refreshed = self.history.get(record.server_url, record.alert.id)
            if refreshed is not None:
                self._route_alert(refreshed.alert, refreshed.server_url)

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._maintenance_timer.stop()
        self._history_refresh_timer.stop()
        self._test_timeout.stop()
        self.notifications.dismiss_all()
        if self.tray is not None:
            self.tray.hide()
        self.socket.stop()
        if self._close_history_on_shutdown:
            self.history.close()
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

    try:
        instance_guard = SingleInstanceGuard(parent=app)
        is_primary = instance_guard.acquire()
    except SingleInstanceError as exc:
        LOGGER.error("Could not establish single-instance ownership: %s", exc)
        QMessageBox.critical(
            None,
            "SignalDesk could not start",
            f"SignalDesk could not safely verify that it is the only running instance.\n\n{exc}",
        )
        return 1
    if not is_primary:
        return 0

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
    instance_guard.activation_requested.connect(controller.window.show_and_activate)
    if instance_guard.take_pending_activation():
        QTimer.singleShot(0, controller.window.show_and_activate)
    app.aboutToQuit.connect(controller.shutdown)
    app.aboutToQuit.connect(instance_guard.release)
    signal.signal(signal.SIGINT, lambda *_: controller.shutdown())
    keep_python_signals_alive = QTimer()
    keep_python_signals_alive.start(500)
    keep_python_signals_alive.timeout.connect(lambda: None)
    QTimer.singleShot(0, controller.start)
    try:
        return app.exec()
    finally:
        instance_guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
