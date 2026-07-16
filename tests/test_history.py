import csv
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import StringIO

import pytest

import signaldesk.history as history_module
from signaldesk.history import SCHEMA_VERSION, AlertStore
from signaldesk.models import Alert, AlertLifecycle, Severity


def make_alert(
    alert_id: str,
    *,
    title: str | None = None,
    severity: str = "info",
    channel: str = "general",
    sequence: int | None = None,
    lifecycle: str = "unread",
    actions: list[dict[str, str]] | None = None,
) -> Alert:
    return Alert.from_payload(
        {
            "id": alert_id,
            "title": title or f"Alert {alert_id}",
            "message": f"Details for {alert_id}",
            "severity": severity,
            "channel": channel,
            "source": "Test monitor",
            "created_at": "2026-07-15T10:00:00Z",
            "sequence": sequence,
            "lifecycle": lifecycle,
            "actions": actions or [],
        }
    )


def test_database_initializes_and_persists_across_reopen(tmp_path) -> None:
    path = tmp_path / "nested" / "alerts.sqlite3"
    with AlertStore(path) as store:
        stored = store.add(
            "https://alerts.example.com",
            make_alert("persisted", sequence=7),
            origin="Production",
            received_at="2026-07-15T10:01:00Z",
            raw_payload={"vendor_extension": {"ticket": "OPS-7"}},
        )
        assert stored.payload["vendor_extension"] == {"ticket": "OPS-7"}

    connection = sqlite3.connect(path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()

    assert version == SCHEMA_VERSION
    assert {"alerts", "server_cursors"}.issubset(tables)

    with AlertStore(path) as reopened:
        stored = reopened.get("https://alerts.example.com", "persisted")
        assert stored is not None
        assert stored.origin == "Production"
        assert stored.sequence == 7
        assert stored.payload["vendor_extension"] == {"ticket": "OPS-7"}
        assert reopened.highest_sequence("https://alerts.example.com") == 7


def test_newer_database_schema_is_rejected(tmp_path) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(RuntimeError, match="newer than supported"):
        AlertStore(path)


def test_v1_migration_preserves_existing_alerts_as_attention_required(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(history_module._SCHEMA_V1)
    payload = {
        "id": "legacy",
        "title": "Legacy alert",
        "message": "Created before attention was optional.",
        "lifecycle": "unread",
    }
    connection.execute(
        """
        INSERT INTO alerts (
            server_url, alert_id, origin, payload, title, message, severity,
            channel, source, created_at, received_at, lifecycle,
            snoozed_until, note, sequence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', NULL)
        """,
        (
            "https://legacy.example.com",
            "legacy",
            "Legacy",
            json.dumps(payload),
            "Legacy alert",
            "Created before attention was optional.",
            "warning",
            "general",
            "Legacy server",
            "2026-07-15T10:00:00.000Z",
            "2026-07-15T10:00:00.000Z",
            "unread",
        ),
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    with AlertStore(path) as store:
        stored = store.get("https://legacy.example.com", "legacy")
        assert stored is not None
        assert stored.requires_attention is True
        assert store.unread_count() == 1


def test_informational_alerts_are_durable_but_not_unread_or_actionable(tmp_path) -> None:
    server = "https://optional.example.com"
    informational = Alert.from_payload(
        {"id": "info", "title": "Release complete", "message": "For reference."}
    )
    attention = Alert.from_payload(
        {
            "id": "attention",
            "title": "Database unavailable",
            "requires_attention": True,
        }
    )
    with AlertStore(tmp_path / "alerts.sqlite3") as store:
        stored_info = store.add(server, informational)
        store.add(server, attention)

        assert stored_info.requires_attention is False
        assert store.unread_count() == 1
        assert [item.alert_id for item in store.query(status="informational")] == ["info"]
        assert [item.alert_id for item in store.query(status="unread")] == ["attention"]
        with pytest.raises(ValueError, match="does not allow reminders"):
            store.update_lifecycle(server, "info", "unread")

        exported = json.loads(store.export_json(status="informational"))
        assert exported[0]["requires_attention"] is False


def test_upsert_deduplicates_and_preserves_local_lifecycle(tmp_path) -> None:
    server = "https://one.example.com"
    with AlertStore(tmp_path / "alerts.sqlite3") as store:
        first = make_alert("same-id", title="First payload", sequence=10)
        inserted = store.add(server, first, received_at="2026-07-15T10:00:00Z")
        store.update_lifecycle(
            server,
            first.id,
            AlertLifecycle.SNOOZED,
            snoozed_until="2026-07-16T10:00:00Z",
            note="Owned by Alice",
        )

        exact_retry = store.upsert(
            server,
            first,
            received_at="2026-07-15T10:01:00Z",
        )
        stale = store.upsert(
            server,
            make_alert("same-id", title="Stale payload", sequence=9),
            received_at="2026-07-15T10:02:00Z",
        )
        refreshed = store.upsert(
            server,
            make_alert("same-id", title="Refreshed payload", sequence=11),
            received_at="2026-07-15T10:03:00Z",
            raw_payload={"revision": 2},
        )

        assert inserted.received_at == "2026-07-15T10:00:00.000Z"
        assert exact_retry.received_at == inserted.received_at
        assert stale.alert.title == "First payload"
        assert refreshed.alert.title == "Refreshed payload"
        assert refreshed.payload["revision"] == 2
        assert refreshed.lifecycle is AlertLifecycle.SNOOZED
        assert refreshed.snoozed_until == "2026-07-16T10:00:00.000Z"
        assert refreshed.note == "Owned by Alice"
        assert refreshed.sequence == 11
        assert len(store.query()) == 1
        assert store.highest_sequence(server) == 11


def test_replayed_legacy_lifecycle_is_normalized_without_retry_regression(tmp_path) -> None:
    server = "https://one.example.com"
    with AlertStore(tmp_path / "alerts.sqlite3") as store:
        store.add(server, make_alert("lifecycle-replay", sequence=10))
        normalized = store.upsert(
            server,
            make_alert("lifecycle-replay", sequence=10, lifecycle="resolved"),
            raw_payload={"status": "resolved", "note": "Recovered remotely"},
        )
        assert normalized.lifecycle is AlertLifecycle.UNREAD
        assert normalized.payload["lifecycle"] == "unread"
        assert "status" not in normalized.payload
        assert normalized.note == "Recovered remotely"

        retried = store.upsert(server, make_alert("lifecycle-replay", sequence=11))
        assert retried.lifecycle is AlertLifecycle.UNREAD


def test_legacy_stored_states_are_read_and_counted_as_unread(tmp_path) -> None:
    server = "https://legacy.example.com"
    with AlertStore(tmp_path / "alerts.sqlite3") as store:
        store.add(server, make_alert("old-ack"))
        store.add(server, make_alert("old-resolved"))
        with store._connection:
            store._connection.execute(
                "UPDATE alerts SET lifecycle = 'acknowledged' WHERE alert_id = 'old-ack'"
            )
            store._connection.execute(
                "UPDATE alerts SET lifecycle = 'resolved', snoozed_until = ? "
                "WHERE alert_id = 'old-resolved'",
                ("2026-07-16T10:00:00.000Z",),
            )

        records = store.query(status="unread")
        assert {record.alert_id for record in records} == {"old-ack", "old-resolved"}
        assert all(record.lifecycle is AlertLifecycle.UNREAD for record in records)
        assert all(record.snoozed_until is None for record in records)
        assert store.unread_count() == 2


def test_cursor_checkpoint_is_monotonic_and_survives_clear(tmp_path) -> None:
    server = "https://cursor.example.com"
    with AlertStore(tmp_path / "alerts.sqlite3") as store:
        assert store.highest_sequence(server) is None
        assert store.set_cursor(server, 50) == 50
        assert store.set_cursor(server, 12) == 50
        store.add(server, make_alert("older", sequence=40))
        assert store.get_cursor(server) == 50

        assert store.clear(server) == 1
        assert store.highest_sequence(server) == 50
        assert store.clear(server, reset_cursor=True) == 0
        assert store.highest_sequence(server) is None


def test_query_supports_text_filters_and_pagination(tmp_path) -> None:
    with AlertStore(tmp_path / "alerts.sqlite3") as store:
        store.add(
            "https://a.example.com",
            make_alert(
                "cpu",
                title="CPU at 100%",
                severity="warning",
                channel="infrastructure",
            ),
            received_at="2026-07-15T10:00:00Z",
        )
        store.add(
            "https://a.example.com",
            make_alert("login", title="Risky sign-in", severity="critical", channel="security"),
            received_at="2026-07-15T10:01:00Z",
        )
        store.add(
            "https://b.example.com",
            make_alert(
                "release", title="Release complete", severity="success", channel="deployments"
            ),
            received_at="2026-07-15T10:02:00Z",
        )
        store.snooze(
            "https://a.example.com",
            "login",
            "2026-07-16T10:00:00Z",
        )

        assert [item.alert_id for item in store.query(text="cpu")] == ["cpu"]
        # LIKE wildcard characters are searched literally, not interpolated into SQL.
        assert [item.alert_id for item in store.query(text="100%")] == ["cpu"]
        assert [item.alert_id for item in store.query(text="%")] == ["cpu"]
        assert {
            item.alert_id for item in store.query(severity=[Severity.WARNING, Severity.CRITICAL])
        } == {"cpu", "login"}
        assert [item.alert_id for item in store.query(status="snoozed")] == ["login"]
        assert [item.alert_id for item in store.query(channel="deployments")] == ["release"]
        assert {item.alert_id for item in store.query(server_url="https://a.example.com")} == {
            "cpu",
            "login",
        }
        assert [item.alert_id for item in store.query(limit=1, offset=1)] == ["login"]
        assert [item.alert_id for item in store.list_alerts(limit=1)] == ["release"]

        assert store.query(server="' OR 1=1 --") == []
        assert len(store.query()) == 3


def test_lifecycle_snooze_notes_and_unread_count(tmp_path) -> None:
    server = "https://lifecycle.example.com"
    with AlertStore(tmp_path / "alerts.sqlite3") as store:
        store.add(server, make_alert("life"))
        assert store.unread_count() == 1
        assert store.unread_count(server) == 1

        snoozed = store.snooze(
            server,
            "life",
            "2026-07-15T15:00:00+02:00",
            note="  Waiting for maintenance  ",
        )
        assert snoozed is not None
        assert snoozed.lifecycle is AlertLifecycle.SNOOZED
        assert snoozed.alert.lifecycle is snoozed.lifecycle
        assert snoozed.snoozed_until == "2026-07-15T13:00:00.000Z"
        assert snoozed.note == "Waiting for maintenance"
        assert store.unread_count() == 0

        noted = store.update_note(server, "life", "Escalated\nto the database team")
        assert noted is not None
        assert noted.note == "Escalated\nto the database team"
        woken = store.update_lifecycle(server, "life", "unread")
        assert woken is not None
        assert woken.lifecycle is AlertLifecycle.UNREAD
        assert woken.alert.lifecycle is woken.lifecycle
        assert woken.snoozed_until is None
        assert woken.note == noted.note
        assert store.unread_count() == 1

        with pytest.raises(ValueError, match="unread or snoozed"):
            store.update_lifecycle(server, "life", "ack")
        with pytest.raises(ValueError, match="unread or snoozed"):
            store.update_lifecycle(server, "life", AlertLifecycle.RESOLVED)
        with pytest.raises(ValueError, match="snoozed_until is required"):
            store.update_lifecycle(server, "life", AlertLifecycle.SNOOZED)
        assert store.update_note(server, "missing", "note") is None


def test_wake_snoozed_restores_only_expired_alerts(tmp_path) -> None:
    server = "https://snooze.example.com"
    with AlertStore(tmp_path / "alerts.sqlite3") as store:
        store.add(server, make_alert("expired"))
        store.add(server, make_alert("future"))
        store.snooze(server, "expired", "2026-07-15T09:00:00Z")
        store.snooze(server, "future", "2026-07-15T11:00:00Z")

        assert store.wake_snoozed("2026-07-15T10:00:00Z") == 1
        assert store.get(server, "expired").lifecycle is AlertLifecycle.UNREAD
        future = store.get(server, "future")
        assert future.lifecycle is AlertLifecycle.SNOOZED
        assert future.snoozed_until == "2026-07-15T11:00:00.000Z"
        assert store.unread_count() == 1


def test_prune_and_clear_preserve_or_reset_cursors_explicitly(tmp_path) -> None:
    server_a = "https://a.example.com"
    server_b = "https://b.example.com"
    with AlertStore(tmp_path / "alerts.sqlite3") as store:
        store.add(
            server_a,
            make_alert("old", sequence=1),
            received_at="2026-07-01T00:00:00Z",
        )
        store.add(
            server_a,
            make_alert("middle", sequence=2),
            received_at="2026-07-10T00:00:00Z",
        )
        store.add(
            server_b,
            make_alert("new", sequence=3),
            received_at="2026-07-14T00:00:00Z",
        )

        assert store.prune(retention_days=7, now="2026-07-15T00:00:00Z") == 1
        assert {item.alert_id for item in store.query()} == {"middle", "new"}
        assert store.prune(max_rows=1) == 1
        assert [item.alert_id for item in store.query()] == ["new"]
        assert store.highest_sequence(server_a) == 2

        assert store.clear(server_b) == 1
        assert store.highest_sequence(server_b) == 3
        store.clear(server_b, reset_cursor=True)
        assert store.highest_sequence(server_b) is None

        with pytest.raises(ValueError):
            store.prune(retention_days=-1)
        with pytest.raises(ValueError):
            store.prune(max_rows=-1)


def test_json_and_csv_exports_include_payload_and_local_metadata(tmp_path) -> None:
    server = "https://export.example.com"
    with AlertStore(tmp_path / "alerts.sqlite3") as store:
        alert = make_alert(
            "exported",
            title="Café service recovered",
            severity="success",
            sequence=8,
            actions=[
                {
                    "label": "Open dashboard",
                    "url": "https://status.example.com/dashboard",
                    "kind": "primary",
                }
            ],
        )
        store.add(server, alert, raw_payload={"correlation_id": "corr-8"})
        store.update_lifecycle(
            server,
            alert.id,
            AlertLifecycle.SNOOZED,
            snoozed_until="2026-07-16T10:00:00Z",
            note="Verified",
        )

        exported_json = json.loads(store.export_json(status="snoozed"))
        assert len(exported_json) == 1
        assert exported_json[0]["lifecycle"] == "snoozed"
        assert exported_json[0]["note"] == "Verified"
        assert exported_json[0]["payload"]["correlation_id"] == "corr-8"
        assert exported_json[0]["payload"]["actions"][0]["label"] == "Open dashboard"

        rows = list(csv.DictReader(StringIO(store.export_csv(status="snoozed"))))
        assert len(rows) == 1
        assert rows[0]["title"] == "Café service recovered"
        assert rows[0]["sequence"] == "8"
        assert json.loads(rows[0]["payload"])["correlation_id"] == "corr-8"


def test_csv_export_neutralizes_spreadsheet_formulas(tmp_path) -> None:
    server = "https://export.example.com"
    with AlertStore(tmp_path / "alerts.sqlite3") as store:
        alert = make_alert("formula", title='=HYPERLINK("https://malicious.example")')
        store.add(server, alert, origin="@untrusted-origin")
        store.update_note(server, alert.id, "+SUM(1, 2)")

        row = next(csv.DictReader(StringIO(store.export_csv())))
        assert row["title"].startswith("'=")
        assert row["origin"].startswith("'@")
        assert row["note"].startswith("'+")


def test_store_serializes_shared_connection_access_across_threads(tmp_path) -> None:
    server = "https://threads.example.com"
    with AlertStore(tmp_path / "alerts.sqlite3") as store:

        def insert(index: int) -> None:
            store.add(server, make_alert(f"thread-{index}", sequence=index))

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(insert, range(40)))

        assert len(store.query(limit=100)) == 40
        assert store.highest_sequence(server) == 39


def test_received_at_accepts_datetime(tmp_path) -> None:
    with AlertStore(tmp_path / "alerts.sqlite3") as store:
        stored = store.add(
            "https://time.example.com",
            make_alert("dated"),
            received_at=datetime(2026, 7, 15, 12, 30, tzinfo=UTC),
        )

        assert stored.received_at == "2026-07-15T12:30:00.000Z"
