import asyncio

from signaldesk.mock_server import MockAlertServer
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
