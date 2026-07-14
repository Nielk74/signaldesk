from signaldesk.models import Alert, AlertChannel, Severity


def test_alert_payload_is_normalized() -> None:
    alert = Alert.from_payload(
        {
            "id": "event-1",
            "title": "  Queue   pressure  ",
            "message": " Jobs are waiting. ",
            "severity": "warn",
            "channel": "Infrastructure",
            "source": "Monitor",
            "timestamp": "2026-07-15T12:00:00Z",
            "duration_ms": 100,
        }
    )

    assert alert.title == "Queue pressure"
    assert alert.message == "Jobs are waiting."
    assert alert.severity is Severity.WARNING
    assert alert.channel == "infrastructure"
    assert alert.created_at == "2026-07-15T12:00:00Z"
    assert alert.duration_ms == 2500


def test_unknown_severity_and_channel_have_safe_fallbacks() -> None:
    alert = Alert.from_payload(
        {
            "title": "Test",
            "message": "Test",
            "severity": "extreme",
            "channel": "../../invalid",
            "duration_ms": 99_999,
        }
    )

    assert alert.severity is Severity.INFO
    assert alert.channel == "general"
    assert alert.duration_ms == 30_000


def test_alert_round_trip_payload() -> None:
    first = Alert.from_payload({"title": "Release", "message": "Complete", "severity": "ok"})
    second = Alert.from_payload(first.to_payload())

    assert second == first


def test_channel_payload_defaults_description() -> None:
    channel = AlertChannel.from_payload({"key": "audit-events", "name": "Audit events"})

    assert channel.key == "audit-events"
    assert channel.name == "Audit events"
    assert channel.description == "Real-time alert channel"
