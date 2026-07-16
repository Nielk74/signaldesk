from signaldesk.reliability import ConnectionWatchdog


def test_watchdog_warns_once_then_reports_recovery() -> None:
    watchdog = ConnectionWatchdog(30)
    watchdog.set_servers({"https://prod"}, now=100.0)

    assert watchdog.poll(now=129.0) == []
    events = watchdog.poll(now=130.0)
    assert len(events) == 1
    assert events[0].kind == "offline"
    assert watchdog.poll(now=200.0) == []

    restored = watchdog.change("https://prod", "connected", now=205.0)
    assert restored is not None
    assert restored.kind == "restored"
    assert restored.offline_seconds == 105


def test_watchdog_treats_connecting_and_disconnected_as_one_outage() -> None:
    watchdog = ConnectionWatchdog(10)
    watchdog.set_servers({"server"}, now=0.0)
    watchdog.change("server", "disconnected", now=5.0)
    assert watchdog.offline_seconds("server", now=9.0) == 9
    assert watchdog.poll(now=10.0)[0].offline_seconds == 10


def test_watchdog_server_set_removes_stale_entries() -> None:
    watchdog = ConnectionWatchdog(10)
    watchdog.set_servers({"old"}, now=0.0)
    watchdog.set_servers({"new"}, now=20.0)
    assert watchdog.offline_seconds("old", now=40.0) == 0
    assert watchdog.poll(now=29.0) == []
    assert watchdog.poll(now=30.0)[0].server_url == "new"


def test_watchdog_threshold_is_bounded() -> None:
    watchdog = ConnectionWatchdog(0)
    assert watchdog.threshold_seconds == 10
    watchdog.set_threshold(99999)
    assert watchdog.threshold_seconds == 3600
