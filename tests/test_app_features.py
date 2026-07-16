from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QFileDialog

import signaldesk.app as app_module
from signaldesk.app import SignalDeskController
from signaldesk.config import AppConfig, ConfigStore, ServerConfig
from signaldesk.history import AlertStore
from signaldesk.models import AlertLifecycle
from signaldesk.policies import NoisePolicy


class FakeSocketManager(QObject):
    state_changed = Signal(str, str, str)
    alert_received = Signal(str, object)
    catalog_received = Signal(str, object)
    subscriptions_confirmed = Signal(str, object)
    health_updated = Signal(str, int, str)
    recovery_completed = Signal(str, object)
    lifecycle_confirmed = Signal(str, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.servers: list[dict[str, object]] = []
        self.resume_calls: list[tuple[str, int]] = []
        self.lifecycle_calls: list[tuple[object, ...]] = []

    def set_servers(self, servers: list[dict[str, object]]) -> None:
        self.servers = servers

    def update_subscriptions(self, _url: str, _subscriptions: list[str]) -> None:
        return

    def reconnect(self, _url: str) -> None:
        return

    def update_resume_after(self, url: str, sequence: int) -> None:
        self.resume_calls.append((url, sequence))

    def request_test(self, _url: str) -> None:
        return

    def request_lifecycle(
        self,
        url: str,
        alert_id: str,
        status: str,
        snoozed_until: str | None = None,
        note: str | None = None,
    ) -> None:
        self.lifecycle_calls.append((url, alert_id, status, snoozed_until, note))

    def stop(self) -> None:
        return


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(app_module, "SocketManager", FakeSocketManager)
    config = AppConfig(
        servers=[ServerConfig(url="https://alerts.example.com", subscriptions=["security"])],
        noise_policy=NoisePolicy(cooldown_seconds=0),
    )
    history = AlertStore(":memory:")
    instance = SignalDeskController(
        QApplication.instance(),
        config,
        ConfigStore(tmp_path / "config.json"),
        start_hidden=True,
        disable_tray=True,
        history_store=history,
    )
    yield instance
    instance._history_refresh_timer.stop()
    instance.notifications.dismiss_all()
    instance.window.prepare_to_quit()
    instance.window.close()
    history.close()


def alert_payload(
    alert_id: str,
    *,
    sequence: int,
    replayed: bool = False,
    requires_attention: bool = True,
) -> dict[str, object]:
    return {
        "id": alert_id,
        "title": f"Alert {alert_id}",
        "message": "Investigate the affected service.",
        "severity": "warning",
        "channel": "security",
        "source": "Test server",
        "sequence": sequence,
        "replayed": replayed,
        "requires_attention": requires_attention,
        "actions": [
            {
                "label": "Open runbook",
                "url": "https://docs.example.com/runbook",
                "kind": "runbook",
            }
        ],
    }


def test_alert_is_committed_before_cursor_and_replay_is_quiet(
    controller: SignalDeskController, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[tuple[str, str]] = []
    played: list[str] = []
    monkeypatch.setattr(
        controller.notifications,
        "show_alert",
        lambda alert, origin="": shown.append((alert.id, origin)),
    )
    monkeypatch.setattr(controller.sounds, "play", played.append)
    url = controller.config.servers[0].url

    controller._alert_received(url, alert_payload("replayed", sequence=4, replayed=True))
    controller._alert_received(url, alert_payload("live", sequence=5))
    controller._alert_received(url, alert_payload("live", sequence=5))

    assert controller.history.get(url, "replayed") is not None
    assert controller.history.get_cursor(url) == 5
    assert controller.socket.resume_calls[-1] == (url, 5)
    assert shown == [("live", url)]
    assert len(played) == 1
    assert controller._history_refresh_timer.isActive()


def test_offline_reminder_is_queued_flushed_and_confirmed(
    controller: SignalDeskController,
) -> None:
    url = controller.config.servers[0].url
    controller._alert_received(url, alert_payload("incident", sequence=1, replayed=True))
    controller._states[url] = "disconnected"

    remind_at = "2026-07-16T10:00:00Z"
    controller._request_lifecycle(url, "incident", "snoozed", remind_at)

    stored = controller.history.get(url, "incident")
    assert stored is not None
    assert stored.lifecycle is AlertLifecycle.SNOOZED
    assert stored.snoozed_until == "2026-07-16T10:00:00.000Z"
    assert (url, "incident") in controller._pending_lifecycle
    assert controller.socket.lifecycle_calls == []

    controller._connection_changed(url, "connected", "Websocket")
    assert controller.socket.lifecycle_calls == [(url, "incident", "snoozed", remind_at, None)]

    controller._lifecycle_confirmed(
        url,
        {
            "ok": True,
            "id": "incident",
            "status": "snoozed",
            "snoozed_until": remind_at,
        },
    )
    assert (url, "incident") not in controller._pending_lifecycle
    assert controller.history.get(url, "incident").lifecycle is AlertLifecycle.SNOOZED


def test_alert_without_reminders_is_delivered_without_reminder_controls(
    controller: SignalDeskController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = controller.config.servers[0].url
    shown: list[str] = []
    monkeypatch.setattr(
        controller.notifications,
        "show_alert",
        lambda alert, origin="": shown.append(alert.id),
    )

    controller._alert_received(
        url,
        alert_payload(
            "information",
            sequence=1,
            requires_attention=False,
        ),
    )

    stored = controller.history.get(url, "information")
    assert stored is not None
    assert stored.requires_attention is False
    assert controller.history.unread_count() == 0
    assert shown == ["information"]
    controller._request_lifecycle(url, "information", "unread")
    assert stored.lifecycle is AlertLifecycle.UNREAD
    assert (url, "information") not in controller._pending_lifecycle
    assert "does not allow reminders" in controller.window.recovery_detail.text().lower()


def test_removed_lifecycle_actions_are_ignored(controller: SignalDeskController) -> None:
    url = controller.config.servers[0].url
    controller._alert_received(url, alert_payload("legacy-action", sequence=1, replayed=True))

    controller._request_lifecycle(url, "legacy-action", "acknowledged")
    controller._request_lifecycle(url, "legacy-action", "resolved")

    stored = controller.history.get(url, "legacy-action")
    assert stored is not None
    assert stored.lifecycle is AlertLifecycle.UNREAD
    assert (url, "legacy-action") not in controller._pending_lifecycle
    assert controller.socket.lifecycle_calls == []


def test_tray_status_only_reports_connection_state(controller: SignalDeskController) -> None:
    class TraySurface:
        tooltip = ""

        def setToolTip(self, value: str) -> None:
            self.tooltip = value

    class StatusSurface:
        text = ""

        def setText(self, value: str) -> None:
            self.text = value

    tray = TraySurface()
    status = StatusSurface()
    controller.tray = tray  # type: ignore[assignment]
    controller.tray_status_action = status  # type: ignore[assignment]

    controller._refresh_tray_status()

    assert tray.tooltip == "SignalDesk — 0/1 online"
    assert status.text == "Status: 0/1 online"


def test_repeat_cooldown_surfaces_grouped_alert_count(
    controller: SignalDeskController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller.policy_engine.set_policy(NoisePolicy(cooldown_seconds=30))
    url = controller.config.servers[0].url
    shown: list[str] = []
    grouped: list[int] = []
    monkeypatch.setattr(
        controller.notifications,
        "show_alert",
        lambda alert, origin="": shown.append(alert.id),
    )
    monkeypatch.setattr(
        controller.notifications,
        "aggregate_alerts",
        lambda count=1: grouped.append(count),
    )
    monkeypatch.setattr(controller.sounds, "play", lambda _sound: None)

    first = alert_payload("repeat-1", sequence=1)
    second = alert_payload("repeat-2", sequence=2)
    first["title"] = second["title"] = "Repeated service alert"
    controller._alert_received(url, first)
    controller._alert_received(url, second)

    assert shown == ["repeat-1"]
    assert grouped == [1]
    assert controller.history.get(url, "repeat-1") is not None
    assert controller.history.get(url, "repeat-2") is not None


def test_notification_activation_opens_matching_durable_detail(
    controller: SignalDeskController,
) -> None:
    url = controller.config.servers[0].url
    controller._alert_received(
        url,
        alert_payload("open-from-toast", sequence=2, replayed=True),
    )

    controller.notifications.activated.emit(url, "open-from-toast")

    assert controller.window._detail_dialogs
    detail = controller.window._detail_dialogs[-1]
    assert detail.alert.id == "open-from-toast"
    assert detail.isVisible()


def test_expired_snooze_returns_to_unread_and_resurfaces(
    controller: SignalDeskController, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = controller.config.servers[0].url
    controller._alert_received(url, alert_payload("wake-me", sequence=2, replayed=True))
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    controller._request_lifecycle(url, "wake-me", "snoozed", past, "Waiting")
    routed: list[str] = []
    monkeypatch.setattr(controller, "_route_alert", lambda alert, _url: routed.append(alert.id))

    controller._wake_due_snoozes()

    stored = controller.history.get(url, "wake-me")
    assert stored is not None
    assert stored.lifecycle is AlertLifecycle.UNREAD
    assert routed == ["wake-me"]
    assert (url, "wake-me") not in controller._pending_lifecycle


def test_recovery_gap_advances_durable_boundary_and_warns(
    controller: SignalDeskController,
) -> None:
    url = controller.config.servers[0].url
    controller._requested_cursors[url] = 3

    controller._recovery_completed(
        url,
        {
            "recovered_count": 2,
            "latest_sequence": 9,
            "oldest_available_sequence": 6,
            "gap": True,
            "truncated": True,
        },
    )

    assert controller.history.get_cursor(url) == 9
    assert controller.socket.resume_calls[-1] == (url, 9)
    assert not controller.window.recovery_banner.isHidden()
    assert "4–5" in controller.window.recovery_detail.text()


def test_filtered_history_export_writes_json(
    controller: SignalDeskController,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    url = controller.config.servers[0].url
    controller._alert_received(url, alert_payload("export-me", sequence=7, replayed=True))
    destination = tmp_path / "filtered.json"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(destination), "JSON files (*.json)"),
    )

    controller._export_history({"severity": "warning", "server": url})

    exported = json.loads(destination.read_text(encoding="utf-8"))
    assert [item["alert_id"] for item in exported] == ["export-me"]
