from datetime import datetime

from signaldesk.models import Alert
from signaldesk.policies import (
    DeliveryMode,
    NoisePolicy,
    NotificationPolicyEngine,
)


def make_alert(**overrides):
    payload = {
        "id": "a-1",
        "title": "Database pressure",
        "message": "Pool at 92%",
        "severity": "warning",
        "channel": "infrastructure",
        "source": "db",
    }
    payload.update(overrides)
    return Alert.from_payload(payload)


def test_override_precedence_server_channel_severity() -> None:
    policy = NoisePolicy(
        severity_modes={"warning": "history_only"},
        channel_modes={"infrastructure": "toast_only"},
        server_modes={"https://prod": "toast_sound"},
        cooldown_seconds=0,
    )
    engine = NotificationPolicyEngine(policy)

    decision = engine.decide(make_alert(), "https://prod")
    assert decision.mode is DeliveryMode.TOAST_SOUND
    assert decision.show_toast and decision.play_sound

    decision = engine.decide(make_alert(), "https://staging")
    assert decision.mode is DeliveryMode.TOAST_ONLY
    assert decision.show_toast and not decision.play_sound


def test_quiet_hours_suppress_interruptions_but_always_record() -> None:
    policy = NoisePolicy(
        quiet_enabled=True,
        quiet_start="22:00",
        quiet_end="07:00",
        critical_bypass=True,
        cooldown_seconds=0,
    )
    engine = NotificationPolicyEngine(policy)
    decision = engine.decide(make_alert(), "server", now=datetime(2026, 1, 1, 23, 30))
    assert decision.record
    assert not decision.show_toast
    assert not decision.play_sound
    assert decision.reason == "quiet_hours"

    critical = engine.decide(
        make_alert(severity="critical"),
        "server",
        now=datetime(2026, 1, 1, 23, 30),
    )
    assert critical.show_toast and critical.play_sound


def test_repeat_cooldown_groups_matching_interruptions() -> None:
    engine = NotificationPolicyEngine(NoisePolicy(cooldown_seconds=60))
    first = engine.decide(make_alert(), "server", monotonic_now=100.0)
    repeated = engine.decide(make_alert(id="a-2"), "server", monotonic_now=110.0)
    later = engine.decide(make_alert(id="a-3"), "server", monotonic_now=170.0)

    assert first.show_toast
    assert not repeated.show_toast
    assert repeated.grouped_count == 2
    assert repeated.reason == "repeat_cooldown"
    assert later.show_toast


def test_policy_mapping_is_clean_and_bounded() -> None:
    policy = NoisePolicy.from_mapping(
        {
            "severity_modes": {"critical": "toast_only", "bad": "unknown"},
            "quiet_start": "99:00",
            "cooldown_seconds": 99_999,
        }
    )
    assert policy.severity_modes["critical"] == "toast_only"
    assert "bad" not in policy.severity_modes
    assert policy.quiet_start == "22:00"
    assert policy.cooldown_seconds == 3600
