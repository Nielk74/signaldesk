import asyncio

from signaldesk.mock_server import MockAlertServer
from signaldesk.models import Alert


def test_catalog_has_unique_channel_keys() -> None:
    channels = MockAlertServer.catalog_payload()["channels"]
    keys = [channel["key"] for channel in channels]

    assert len(keys) == len(set(keys))
    assert {"infrastructure", "security", "deployments"}.issubset(keys)


def test_publish_only_targets_subscribed_clients(monkeypatch) -> None:
    server = MockAlertServer()
    server.clients = {
        "security-client": {"security"},
        "infra-client": {"infrastructure"},
        "mixed-client": {"security", "infrastructure"},
    }
    calls = []

    async def fake_emit(event, payload, *, to):
        calls.append((event, payload, to))

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
    assert {call[2] for call in calls} == {"security-client", "mixed-client"}
    assert all(call[0] == "alert" for call in calls)


def test_filter_subscriptions_rejects_unknown_channels() -> None:
    assert MockAlertServer._filter_subscriptions(["security", "unknown", "deployments"]) == {
        "security",
        "deployments",
    }
