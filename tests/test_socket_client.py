import time

from signaldesk.socket_client import PingTracker


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
