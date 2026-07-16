import pytest

from signaldesk.models import (
    MAX_ALERT_ACTIONS,
    Alert,
    AlertAction,
    AlertChannel,
    AlertLifecycle,
    Severity,
)


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
    assert alert.requires_attention is False


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


def test_alert_structured_fields_are_validated_and_round_trip() -> None:
    alert = Alert.from_payload(
        {
            "id": "event-structured",
            "title": "Database unavailable",
            "message": "Open the runbook before escalating.",
            "sequence": "42",
            "status": "snoozed",
            "actions": [
                {
                    "label": "  Open   runbook ",
                    "url": "https://docs.example.com/runbooks/db?region=eu",
                    "kind": "Primary",
                },
                {"label": "Dashboard", "url": "http://status.example.com/db"},
            ],
        }
    )

    assert alert.sequence == 42
    assert alert.requires_attention is True
    assert alert.lifecycle is AlertLifecycle.SNOOZED
    assert alert.actions == (
        AlertAction(
            "Open runbook",
            "https://docs.example.com/runbooks/db?region=eu",
            "primary",
        ),
        AlertAction("Dashboard", "http://status.example.com/db"),
    )
    payload = alert.to_payload()
    assert payload["lifecycle"] == "snoozed"
    assert payload["sequence"] == 42
    assert Alert.from_payload(payload) == alert


def test_attention_lifecycle_is_server_opt_in_and_explicit_false_wins() -> None:
    informational = Alert.from_payload({"title": "Release complete"})
    attention = Alert.from_payload({"title": "Database unavailable", "requires_attention": True})
    explicitly_informational = Alert.from_payload(
        {
            "title": "For reference",
            "requires_attention": False,
            "status": "resolved",
        }
    )

    assert informational.requires_attention is False
    assert informational.lifecycle is AlertLifecycle.UNREAD
    assert informational.to_payload()["requires_attention"] is False
    assert attention.requires_attention is True
    assert attention.lifecycle is AlertLifecycle.UNREAD
    assert explicitly_informational.requires_attention is False
    assert explicitly_informational.lifecycle is AlertLifecycle.UNREAD


@pytest.mark.parametrize("legacy", ["ack", "acked", "acknowledged", "closed", "resolved"])
def test_removed_lifecycle_values_are_treated_as_unread(legacy: str) -> None:
    alert = Alert.from_payload({"title": "Legacy alert", "status": legacy})

    assert alert.requires_attention is True
    assert alert.lifecycle is AlertLifecycle.UNREAD
    assert "lifecycle" not in alert.to_payload()


def test_malformed_optional_actions_are_ignored_and_bounded() -> None:
    valid = [
        {"label": f"Action {index}", "url": f"https://example.com/{index}"}
        for index in range(MAX_ALERT_ACTIONS + 3)
    ]
    alert = Alert.from_payload(
        {
            "title": "Actions",
            "message": "Only safe links should survive.",
            "sequence": -1,
            "lifecycle": "invented",
            "actions": [
                None,
                {"label": "Script", "url": "javascript:alert(1)"},
                {"label": "Credentials", "url": "https://user:pass@example.com"},
                {"label": "Bad kind", "url": "https://example.com", "kind": "not valid"},
                {"url": "https://example.com/missing-label"},
                *valid,
            ],
        }
    )

    assert alert.sequence is None
    assert alert.lifecycle is AlertLifecycle.UNREAD
    assert len(alert.actions) == MAX_ALERT_ACTIONS
    assert all(action.url.startswith("https://") for action in alert.actions)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/runbook",
        "javascript:alert(1)",
        "https://user:secret@example.com/path",
        "https://example.com\\@malicious.example/path",
        "https://example.com/a b",
    ],
)
def test_alert_action_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError):
        AlertAction("Open", url)


def test_direct_alert_sequence_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        Alert(
            id="invalid-sequence",
            title="Invalid",
            message="Invalid",
            sequence=-1,
        )


def test_channel_payload_defaults_description() -> None:
    channel = AlertChannel.from_payload({"key": "audit-events", "name": "Audit events"})

    assert channel.key == "audit-events"
    assert channel.name == "Audit events"
    assert channel.description == "Real-time alert channel"
