import asyncio

import pytest

from signaldesk.mock_server import (
    MockAlertServer,
    build_recovery_batch,
    token_is_authorized,
    validate_lifecycle_payload,
)
from signaldesk.models import Alert


def test_catalog_has_unique_channel_keys() -> None:
    channels = MockAlertServer.catalog_payload()["channels"]
    keys = [channel["key"] for channel in channels]

    assert len(keys) == len(set(keys))
    assert {"infrastructure", "security", "deployments"}.issubset(keys)


def test_publish_fans_out_to_channel_room_once(monkeypatch) -> None:
    server = MockAlertServer()
    # Reverse index is the delivery source of truth; two clients on "security".
    server.clients = {
        "security-client": {"security"},
        "infra-client": {"infrastructure"},
        "mixed-client": {"security", "infrastructure"},
    }
    server._members = {
        "security": {"security-client", "mixed-client"},
        "infrastructure": {"infra-client", "mixed-client"},
    }
    calls = []

    async def fake_emit(event, payload, *, room):
        calls.append((event, payload, room))

    monkeypatch.setattr(server.sio, "emit", fake_emit)
    alert = Alert.from_payload(
        {
            "title": "Blocked sign-in",
            "message": "A risky sign-in was blocked.",
            "severity": "warning",
            "channel": "security",
        }
    )

    delivered = asyncio.run(server.publish(alert))

    assert delivered == 2
    assert len(calls) == 1
    event, _payload, room = calls[0]
    assert event == "alert"
    assert room == "security"


def test_publish_with_no_subscribers_skips_emit(monkeypatch) -> None:
    server = MockAlertServer()
    calls = []

    async def fake_emit(event, payload, *, room):
        calls.append(room)

    monkeypatch.setattr(server.sio, "emit", fake_emit)
    alert = Alert.from_payload(
        {"title": "Nobody home", "message": "No subscribers.", "channel": "billing"}
    )

    delivered = asyncio.run(server.publish(alert))

    assert delivered == 0
    assert calls == []


def test_apply_subscriptions_updates_rooms_and_reverse_index(monkeypatch) -> None:
    server = MockAlertServer()
    entered: list[tuple[str, str]] = []
    left: list[tuple[str, str]] = []

    async def fake_enter(sid, channel):
        entered.append((sid, channel))

    async def fake_leave(sid, channel):
        left.append((sid, channel))

    monkeypatch.setattr(server, "_enter_room", fake_enter)
    monkeypatch.setattr(server, "_leave_room", fake_leave)

    first = asyncio.run(server._apply_subscriptions("sid", ["security", "unknown", "billing"]))
    assert first == {"security", "billing"}
    assert server._members["security"] == {"sid"}
    assert set(entered) == {("sid", "security"), ("sid", "billing")}

    second = asyncio.run(server._apply_subscriptions("sid", ["security"]))
    assert second == {"security"}
    assert "billing" not in server._members
    assert ("sid", "billing") in left


def test_filter_subscriptions_rejects_unknown_channels() -> None:
    assert MockAlertServer._filter_subscriptions(["security", "unknown", "deployments"]) == {
        "security",
        "deployments",
    }


def test_recovery_batch_filters_subscriptions_and_reports_truncation() -> None:
    events = [
        {"id": "3", "channel": "security", "sequence": 3},
        {"id": "4", "channel": "billing", "sequence": 4},
        {"id": "5", "channel": "security", "sequence": 5},
    ]

    recovered, metadata = build_recovery_batch(events, {"security"}, 1, 5)

    assert [event["sequence"] for event in recovered] == [3, 5]
    assert all(event["replayed"] is True for event in recovered)
    assert metadata == {
        "recovered_count": 2,
        "latest_sequence": 5,
        "oldest_available_sequence": 3,
        "gap": True,
        "truncated": True,
    }


def test_recovery_batch_without_cursor_establishes_boundary_without_replay() -> None:
    recovered, metadata = build_recovery_batch(
        [{"id": "1", "channel": "security", "sequence": 1}],
        {"security"},
        None,
        1,
    )

    assert recovered == []
    assert metadata["latest_sequence"] == 1
    assert metadata["gap"] is False


def test_publish_assigns_monotonic_sequences_and_bounds_replay_log(monkeypatch) -> None:
    server = MockAlertServer(event_log_size=2)
    server._members = {"security": {"sid"}}
    emitted: list[int] = []

    async def fake_emit(event, payload, *, room):
        assert event == "alert"
        assert room == "security"
        emitted.append(payload["sequence"])

    monkeypatch.setattr(server.sio, "emit", fake_emit)

    async def publish_three() -> None:
        for index in range(3):
            alert = Alert.from_payload(
                {"id": str(index), "title": f"Alert {index}", "channel": "security"}
            )
            await server.publish(alert)

    asyncio.run(publish_three())

    assert emitted == [1, 2, 3]
    assert [item["sequence"] for item in server._event_log] == [2, 3]


def test_optional_auth_allows_zero_config_and_rejects_wrong_token() -> None:
    assert token_is_authorized(None, None) is True
    assert token_is_authorized(None, "anything") is True
    assert token_is_authorized("expected", None) is False
    assert token_is_authorized("expected", "wrong") is False
    assert token_is_authorized("expected", "expected") is True


def test_lifecycle_validation_and_confirmation(monkeypatch) -> None:
    assert validate_lifecycle_payload({"id": "a-1", "status": "unread"}) == {
        "id": "a-1",
        "status": "unread",
    }
    for removed_status in ("acknowledged", "resolved", "muted"):
        with pytest.raises(ValueError, match="unread or snoozed"):
            validate_lifecycle_payload({"id": "a-1", "status": removed_status})

    server = MockAlertServer()
    server._event_log.append(
        {
            "id": "a-1",
            "channel": "security",
            "sequence": 1,
            "requires_attention": True,
        }
    )
    confirmations = []

    async def fake_emit(event, payload, **kwargs):
        confirmations.append((event, payload, kwargs))

    monkeypatch.setattr(server.sio, "emit", fake_emit)
    handler = server.sio.handlers["/"]["alert:lifecycle"]
    result = asyncio.run(
        handler(
            "sid",
            {
                "id": "a-1",
                "status": "snoozed",
                "snoozed_until": "2026-07-16T10:00:00Z",
            },
        )
    )

    assert result["ok"] is True
    assert result["status"] == "snoozed"
    assert server._event_log[0]["status"] == "snoozed"
    assert confirmations[0][0] == "alert:lifecycle:confirmed"
    assert confirmations[0][2] == {"to": "sid"}
    assert confirmations[1][2] == {"room": "security", "skip_sid": "sid"}

    server._event_log[0]["lifecycle"] = "snoozed"
    undone = asyncio.run(handler("sid", {"id": "a-1", "status": "unread"}))
    assert undone["status"] == "unread"
    assert server._event_log[0]["status"] == "unread"
    assert "lifecycle" not in server._event_log[0]

    unknown = asyncio.run(handler("sid", {"id": "missing", "status": "unread"}))
    assert unknown == {"ok": False, "error": "Alert was not found"}

    server._event_log.append({"id": "info-1", "channel": "security", "sequence": 2})
    informational = asyncio.run(handler("sid", {"id": "info-1", "status": "unread"}))
    assert informational == {
        "ok": False,
        "error": "This alert does not allow reminders",
    }


def test_lifecycle_rejects_invalid_snooze_timestamp() -> None:
    with pytest.raises(ValueError, match="ISO-8601"):
        validate_lifecycle_payload({"id": "a-1", "status": "snoozed", "snoozed_until": "later"})
    with pytest.raises(ValueError, match="required"):
        validate_lifecycle_payload({"id": "a-1", "status": "snoozed"})
