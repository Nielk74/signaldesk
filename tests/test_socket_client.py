import time

import pytest

from signaldesk.socket_client import (
    PingTracker,
    ServerLink,
    build_auth_payload,
    build_lifecycle_payload,
)


def test_auth_payload_omits_absent_optional_fields() -> None:
    assert build_auth_payload({"security", "billing"}) == {"subscriptions": ["billing", "security"]}


def test_auth_payload_includes_recovery_token_and_client_identity() -> None:
    assert build_auth_payload(
        ["security"],
        token="secret-value",
        resume_after=0,
        client_id="desktop-a",
    ) == {
        "subscriptions": ["security"],
        "token": "secret-value",
        "resume_after": 0,
        "client_id": "desktop-a",
    }


def test_lifecycle_payload_validation() -> None:
    assert build_lifecycle_payload("alert-1", "unread") == {
        "id": "alert-1",
        "status": "unread",
    }
    assert build_lifecycle_payload(
        "alert-1",
        "SNOOZED",
        snoozed_until="2026-07-15T14:00:00Z",
        note="Investigating",
    ) == {
        "id": "alert-1",
        "status": "snoozed",
        "snoozed_until": "2026-07-15T14:00:00Z",
        "note": "Investigating",
    }
    with pytest.raises(ValueError, match="Unsupported lifecycle status"):
        build_lifecycle_payload("alert-1", "deleted")
    with pytest.raises(ValueError, match="ISO-8601"):
        build_lifecycle_payload("alert-1", "snoozed", snoozed_until="tomorrow")
    with pytest.raises(ValueError, match="required"):
        build_lifecycle_payload("alert-1", "snoozed")
    with pytest.raises(ValueError, match="80"):
        build_lifecycle_payload("x" * 81, "unread")
    for removed_status in ("acknowledged", "resolved"):
        with pytest.raises(ValueError, match="Unsupported lifecycle status"):
            build_lifecycle_payload("alert-1", removed_status)


def test_reminder_confirmation_is_forwarded_with_request_identity() -> None:
    confirmations: list[tuple[str, object]] = []

    class Hub:
        def emit_lifecycle(self, url: str, payload: object) -> None:
            confirmations.append((url, payload))

    class Client:
        connected = True

        def emit(self, event: str, payload: object, *, callback: object) -> None:
            assert event == "alert:lifecycle"
            assert payload == {"id": "alert-1", "status": "unread"}
            callback({"ok": False, "error": "Alert was not found"})

    link = ServerLink(Hub(), "https://alerts.example.com", {"security"})
    link._sio = Client()
    link._connected = True
    link.request_lifecycle({"id": "alert-1", "status": "unread"})

    assert confirmations == [
        (
            "https://alerts.example.com",
            {"ok": False, "error": "Alert was not found", "id": "alert-1"},
        )
    ]


def test_ping_tracker_measures_round_trip() -> None:
    tracker = PingTracker()
    tracker.register("abc", now=10.0)
    rtt = tracker.resolve("abc", now=10.05)  # 50 ms later
    assert rtt is not None
    assert 49.0 <= rtt <= 51.0


def test_ping_tracker_unknown_nonce_returns_none() -> None:
    tracker = PingTracker()
    assert tracker.resolve("missing") is None
    # A nonce can only be resolved once.
    tracker.register("used", now=1.0)
    assert tracker.resolve("used", now=1.01) is not None
    assert tracker.resolve("used", now=1.02) is None


def test_ping_tracker_evicts_expired_entries() -> None:
    tracker = PingTracker(capacity=1024, ttl_s=1.0)
    tracker.register("old", now=0.0)
    tracker.register("new", now=5.0)  # registering prunes the expired "old"
    assert tracker.resolve("old") is None
    assert tracker.resolve("new", now=5.01) is not None


def test_ping_tracker_stays_bounded_under_heartbeat_flood() -> None:
    """5000+ outstanding heartbeats must not grow the tracker without bound."""
    tracker = PingTracker(capacity=1024, ttl_s=30.0)
    base = 1000.0
    for index in range(5000):
        tracker.register(f"nonce-{index}", now=base + index * 0.001)

    assert len(tracker) <= 1024
    # The most recent heartbeats are still resolvable with correct timing.
    latest = "nonce-4999"
    rtt = tracker.resolve(latest, now=base + 4999 * 0.001 + 0.02)
    assert rtt is not None
    assert 19.0 <= rtt <= 21.0


def test_ping_tracker_register_is_fast_under_flood() -> None:
    """Amortized O(1) registration keeps flood latency flat, not quadratic."""
    tracker = PingTracker(capacity=1024, ttl_s=30.0)
    start = time.perf_counter()
    for index in range(20000):
        tracker.register(f"n-{index}", now=1000.0 + index * 0.0001)
    elapsed = time.perf_counter() - start
    # Generous ceiling; a quadratic implementation would blow well past this.
    assert elapsed < 1.0
    assert len(tracker) <= 1024
