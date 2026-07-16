"""Persistent, UI-independent alert inbox storage."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from threading import RLock
from typing import Any

from signaldesk.models import Alert, AlertLifecycle, Severity

SCHEMA_VERSION = 2
MAX_NOTE_LENGTH = 4000
MAX_QUERY_LIMIT = 5000

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS alerts (
    server_url TEXT NOT NULL,
    alert_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    payload TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    severity TEXT NOT NULL,
    channel TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    lifecycle TEXT NOT NULL DEFAULT 'unread'
        CHECK (lifecycle IN ('unread', 'acknowledged', 'snoozed', 'resolved')),
    snoozed_until TEXT,
    note TEXT NOT NULL DEFAULT '',
    sequence INTEGER,
    PRIMARY KEY (server_url, alert_id)
);

CREATE INDEX IF NOT EXISTS alerts_received_at_idx
    ON alerts (received_at DESC);
CREATE INDEX IF NOT EXISTS alerts_lifecycle_idx
    ON alerts (lifecycle, received_at DESC);
CREATE INDEX IF NOT EXISTS alerts_server_sequence_idx
    ON alerts (server_url, sequence);
CREATE INDEX IF NOT EXISTS alerts_filter_idx
    ON alerts (severity, channel, received_at DESC);

CREATE TABLE IF NOT EXISTS server_cursors (
    server_url TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    updated_at TEXT NOT NULL
);
"""

_SCHEMA_V2 = """
ALTER TABLE alerts ADD COLUMN requires_attention INTEGER NOT NULL DEFAULT 1
    CHECK (requires_attention IN (0, 1));
CREATE INDEX IF NOT EXISTS alerts_attention_lifecycle_idx
    ON alerts (requires_attention, lifecycle, received_at DESC);
"""


def default_history_path() -> Path:
    """Return the default database location in SignalDesk's application-data directory."""
    # Import lazily so the protocol/storage layers do not create a module cycle.
    from signaldesk.config import app_data_dir

    return app_data_dir() / "alerts.sqlite3"


def _as_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("Timestamp must not be empty")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid timestamp: {value!r}") from exc
    else:
        raise TypeError("Timestamp must be a datetime or ISO-8601 string")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp(value: datetime | str | None = None) -> str:
    parsed = datetime.now(UTC) if value is None else _as_datetime(value)
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _required_text(value: object, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    return cleaned


def _note(value: object) -> str:
    return str(value or "").strip()[:MAX_NOTE_LENGTH]


def _strict_lifecycle(value: AlertLifecycle | str) -> AlertLifecycle:
    try:
        lifecycle = (
            value
            if isinstance(value, AlertLifecycle)
            else AlertLifecycle(str(value).strip().lower())
        )
    except ValueError as exc:
        raise ValueError("Lifecycle must be unread or snoozed") from exc
    if lifecycle not in {AlertLifecycle.UNREAD, AlertLifecycle.SNOOZED}:
        raise ValueError("Lifecycle must be unread or snoozed")
    return lifecycle


def _sequence(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Sequence must be a non-negative integer")
    if value > (1 << 63) - 1:
        raise ValueError("Sequence exceeds SQLite's signed 64-bit integer range")
    return value


def _filter_items(
    value: str | Severity | AlertLifecycle | Iterable[str | Severity | AlertLifecycle] | None,
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [str(value)]
    return [str(item) for item in value]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _csv_cell(value: object) -> object:
    """Prevent user-controlled CSV text from being interpreted as a spreadsheet formula."""
    if isinstance(value, str) and value.lstrip()[:1] in {"=", "+", "-", "@"}:
        return f"'{value}"
    return value


@dataclass(frozen=True, slots=True)
class StoredAlert:
    """An alert plus its persistent inbox metadata."""

    server_url: str
    alert: Alert
    origin: str
    payload: Mapping[str, Any]
    received_at: str
    lifecycle: AlertLifecycle
    snoozed_until: str | None
    note: str
    sequence: int | None

    @property
    def alert_id(self) -> str:
        return self.alert.id

    @property
    def status(self) -> AlertLifecycle:
        """Alias useful to callers that call the lifecycle field ``status``."""
        return self.lifecycle

    @property
    def requires_attention(self) -> bool:
        return self.alert.requires_attention

    def to_mapping(self) -> dict[str, Any]:
        """Return a JSON-serializable representation, including the full payload."""
        return {
            "server_url": self.server_url,
            "alert_id": self.alert.id,
            "origin": self.origin,
            "payload": dict(self.payload),
            "received_at": self.received_at,
            "requires_attention": self.alert.requires_attention,
            "lifecycle": self.lifecycle.value,
            "snoozed_until": self.snoozed_until,
            "note": self.note,
            "sequence": self.sequence,
        }


class AlertStore:
    """Thread-safe SQLite store for the durable SignalDesk inbox."""

    def __init__(self, path: str | Path | None = None) -> None:
        requested_path = default_history_path() if path is None else path
        self.path: str | Path = requested_path
        if str(requested_path) != ":memory:":
            database_path = Path(requested_path).expanduser()
            database_path.parent.mkdir(parents=True, exist_ok=True)
            self.path = database_path
            database = str(database_path)
        else:
            database = ":memory:"

        self._lock = RLock()
        self._connection = sqlite3.connect(database, timeout=10, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA busy_timeout = 10000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._migrate()
        except Exception:
            self._connection.close()
            raise

    def _migrate(self) -> None:
        with self._lock:
            row = self._connection.execute("PRAGMA user_version").fetchone()
            version = int(row[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Alert database schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            if version < 1:
                self._connection.executescript(_SCHEMA_V1)
                version = 1
            if version < 2:
                self._connection.executescript(_SCHEMA_V2)
                version = 2
            self._connection.execute(f"PRAGMA user_version = {version}")
            self._connection.commit()

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._connection.close()

    def __enter__(self) -> AlertStore:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    @staticmethod
    def _payload_json(alert: Alert, raw_payload: Mapping[str, Any] | None) -> str:
        payload = dict(raw_payload or {})
        payload.pop("status", None)
        payload.pop("lifecycle", None)
        # Canonical model values win while unknown protocol fields remain intact.
        payload.update(alert.to_payload())
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _is_fresher(
        existing: sqlite3.Row,
        *,
        payload_json: str,
        received_at: str,
        sequence: int | None,
    ) -> bool:
        previous_sequence = existing["sequence"]
        if sequence is not None:
            if previous_sequence is None or sequence > previous_sequence:
                return True
            if sequence < previous_sequence:
                return False
        elif previous_sequence is not None:
            return False
        return payload_json != existing["payload"] and received_at >= existing["received_at"]

    def _set_cursor_locked(self, server_url: str, sequence: int) -> None:
        self._connection.execute(
            """
            INSERT INTO server_cursors (server_url, sequence, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (server_url) DO UPDATE SET
                sequence = excluded.sequence,
                updated_at = excluded.updated_at
            WHERE excluded.sequence > server_cursors.sequence
            """,
            (server_url, sequence, _timestamp()),
        )

    def upsert(
        self,
        server_url: str,
        alert: Alert,
        *,
        origin: str | None = None,
        received_at: datetime | str | None = None,
        raw_payload: Mapping[str, Any] | None = None,
    ) -> StoredAlert:
        """Insert an alert or refresh an existing key without resetting local lifecycle."""
        server = _required_text(server_url, "server_url")
        record_origin = _required_text(origin or alert.source, "origin")[:160]
        received = _timestamp(received_at)
        payload_json = self._payload_json(alert, raw_payload)

        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT payload, received_at, sequence, requires_attention FROM alerts "
                "WHERE server_url = ? AND alert_id = ?",
                (server, alert.id),
            ).fetchone()
            accepted_payload = existing is None
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO alerts (
                        server_url, alert_id, origin, payload, title, message, severity,
                        channel, source, created_at, received_at, requires_attention, lifecycle,
                        snoozed_until, note, sequence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', ?)
                    """,
                    (
                        server,
                        alert.id,
                        record_origin,
                        payload_json,
                        alert.title,
                        alert.message,
                        alert.severity.value,
                        alert.channel,
                        alert.source,
                        alert.created_at,
                        received,
                        int(alert.requires_attention),
                        alert.lifecycle.value,
                        alert.sequence,
                    ),
                )
            elif self._is_fresher(
                existing,
                payload_json=payload_json,
                received_at=received,
                sequence=alert.sequence,
            ):
                accepted_payload = True
                self._connection.execute(
                    """
                    UPDATE alerts SET
                        origin = ?, payload = ?, title = ?, message = ?, severity = ?,
                        channel = ?, source = ?, created_at = ?, received_at = ?,
                        requires_attention = ?, sequence = ?
                    WHERE server_url = ? AND alert_id = ?
                    """,
                    (
                        record_origin,
                        payload_json,
                        alert.title,
                        alert.message,
                        alert.severity.value,
                        alert.channel,
                        alert.source,
                        alert.created_at,
                        received,
                        int(alert.requires_attention),
                        alert.sequence,
                        server,
                        alert.id,
                    ),
                )

            explicit_lifecycle = raw_payload is not None and (
                "lifecycle" in raw_payload or "status" in raw_payload
            )
            if accepted_payload and not alert.requires_attention:
                # Disabling response handling makes the record informational;
                # stale lifecycle metadata must not leak into the UI.
                self._connection.execute(
                    "UPDATE alerts SET lifecycle = ?, snoozed_until = NULL "
                    "WHERE server_url = ? AND alert_id = ?",
                    (AlertLifecycle.UNREAD.value, server, alert.id),
                )
            # Explicit server lifecycle values survive replay, including an
            # explicit unread used to undo or wake an alert. A plain retry
            # that omits lifecycle never resets a local response action.
            elif alert.requires_attention and (
                alert.lifecycle is not AlertLifecycle.UNREAD or explicit_lifecycle
            ):
                snoozed_until: str | None = None
                if alert.lifecycle is AlertLifecycle.SNOOZED and raw_payload is not None:
                    supplied_snooze = raw_payload.get("snoozed_until")
                    if supplied_snooze:
                        snoozed_until = _timestamp(str(supplied_snooze))
                assignments = ["lifecycle = ?", "snoozed_until = ?"]
                lifecycle_parameters: list[Any] = [
                    alert.lifecycle.value,
                    snoozed_until,
                ]
                if raw_payload is not None and "note" in raw_payload:
                    assignments.append("note = ?")
                    lifecycle_parameters.append(_note(raw_payload.get("note")))
                lifecycle_parameters.extend((server, alert.id))
                self._connection.execute(
                    f"UPDATE alerts SET {', '.join(assignments)} "
                    "WHERE server_url = ? AND alert_id = ?",
                    lifecycle_parameters,
                )

            if alert.sequence is not None:
                self._set_cursor_locked(server, alert.sequence)
            row = self._get_row_locked(server, alert.id)

        if row is None:  # Defensive: the transaction above always inserts or finds the key.
            raise RuntimeError("Alert upsert did not produce a stored record")
        return self._row_to_stored(row)

    def add(
        self,
        server_url: str,
        alert: Alert,
        *,
        origin: str | None = None,
        received_at: datetime | str | None = None,
        raw_payload: Mapping[str, Any] | None = None,
    ) -> StoredAlert:
        """Alias for :meth:`upsert`."""
        return self.upsert(
            server_url,
            alert,
            origin=origin,
            received_at=received_at,
            raw_payload=raw_payload,
        )

    def _get_row_locked(self, server_url: str, alert_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM alerts WHERE server_url = ? AND alert_id = ?",
            (server_url, alert_id),
        ).fetchone()

    @staticmethod
    def _row_to_stored(row: sqlite3.Row) -> StoredAlert:
        try:
            loaded = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            loaded = {}
        payload: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
        lifecycle = AlertLifecycle.parse(row["lifecycle"])
        snoozed_until = row["snoozed_until"] if lifecycle is AlertLifecycle.SNOOZED else None
        if snoozed_until is None:
            payload.pop("snoozed_until", None)
        payload.update(
            {
                "id": row["alert_id"],
                "title": row["title"],
                "message": row["message"],
                "severity": row["severity"],
                "channel": row["channel"],
                "source": row["source"],
                "created_at": row["created_at"],
                "requires_attention": bool(row["requires_attention"]),
                "lifecycle": lifecycle.value,
            }
        )
        if row["sequence"] is not None:
            payload["sequence"] = row["sequence"]
        alert = Alert.from_payload(payload)
        return StoredAlert(
            server_url=row["server_url"],
            alert=alert,
            origin=row["origin"],
            payload=payload,
            received_at=row["received_at"],
            lifecycle=lifecycle,
            snoozed_until=snoozed_until,
            note=row["note"],
            sequence=row["sequence"],
        )

    def get(self, server_url: str, alert_id: str) -> StoredAlert | None:
        """Return one alert by its composite server/id key."""
        with self._lock:
            row = self._get_row_locked(server_url, alert_id)
        return self._row_to_stored(row) if row is not None else None

    def query(
        self,
        *,
        text: str | None = None,
        severity: str | Severity | Iterable[str | Severity] | None = None,
        status: str | AlertLifecycle | Iterable[str | AlertLifecycle] | None = None,
        channel: str | Iterable[str] | None = None,
        server: str | Iterable[str] | None = None,
        server_url: str | Iterable[str] | None = None,
        limit: int | None = 100,
        offset: int = 0,
    ) -> list[StoredAlert]:
        """Search newest-first with optional text and exact-value filters."""
        if server is not None and server_url is not None:
            raise ValueError("Use either server or server_url, not both")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_QUERY_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}, or None")

        clauses: list[str] = []
        parameters: list[Any] = []
        if text and text.strip():
            pattern = f"%{_escape_like(text.strip())}%"
            searchable = ("alert_id", "title", "message", "source", "origin", "payload")
            clauses.append(
                "(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in searchable) + ")"
            )
            parameters.extend(pattern for _ in searchable)

        selected_statuses = _filter_items(status)
        if selected_statuses:
            include_informational = "informational" in selected_statuses
            lifecycle_statuses = [
                item for item in selected_statuses if item != "informational"
            ]
            if AlertLifecycle.UNREAD.value in lifecycle_statuses:
                lifecycle_statuses.extend(
                    [AlertLifecycle.ACKNOWLEDGED.value, AlertLifecycle.RESOLVED.value]
                )
            lifecycle_statuses = list(dict.fromkeys(lifecycle_statuses))
            status_clauses: list[str] = []
            if include_informational:
                status_clauses.append("requires_attention = 0")
            if lifecycle_statuses:
                placeholders = ", ".join("?" for _ in lifecycle_statuses)
                status_clauses.append(
                    f"(requires_attention = 1 AND lifecycle IN ({placeholders}))"
                )
                parameters.extend(lifecycle_statuses)
            clauses.append("(" + " OR ".join(status_clauses) + ")")

        for column, selected in (
            ("severity", _filter_items(severity)),
            ("channel", _filter_items(channel)),
            ("server_url", _filter_items(server if server is not None else server_url)),
        ):
            if selected:
                placeholders = ", ".join("?" for _ in selected)
                clauses.append(f"{column} IN ({placeholders})")
                parameters.extend(selected)

        sql = "SELECT * FROM alerts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY received_at DESC, rowid DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            parameters.extend((limit, offset))
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            parameters.append(offset)

        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [self._row_to_stored(row) for row in rows]

    def list_alerts(
        self,
        *,
        text: str | None = None,
        severity: str | Severity | Iterable[str | Severity] | None = None,
        status: str | AlertLifecycle | Iterable[str | AlertLifecycle] | None = None,
        channel: str | Iterable[str] | None = None,
        server: str | Iterable[str] | None = None,
        server_url: str | Iterable[str] | None = None,
        limit: int | None = 100,
        offset: int = 0,
    ) -> list[StoredAlert]:
        """Convenience alias for :meth:`query`."""
        return self.query(
            text=text,
            severity=severity,
            status=status,
            channel=channel,
            server=server,
            server_url=server_url,
            limit=limit,
            offset=offset,
        )

    def update_lifecycle(
        self,
        server_url: str,
        alert_id: str,
        lifecycle: AlertLifecycle | str,
        *,
        snoozed_until: datetime | str | None = None,
        note: str | None = None,
    ) -> StoredAlert | None:
        """Set local lifecycle metadata, preserving the server payload."""
        status = _strict_lifecycle(lifecycle)
        with self._lock, self._connection:
            existing = self._get_row_locked(server_url, alert_id)
            if existing is None:
                return None
            if not bool(existing["requires_attention"]):
                raise ValueError("This alert does not allow reminders")
            if status is AlertLifecycle.SNOOZED:
                if snoozed_until is None:
                    raise ValueError("snoozed_until is required for a snoozed alert")
                snooze_value = _timestamp(snoozed_until)
            else:
                snooze_value = None

            assignments = ["lifecycle = ?", "snoozed_until = ?"]
            parameters: list[Any] = [status.value, snooze_value]
            if note is not None:
                assignments.append("note = ?")
                parameters.append(_note(note))
            parameters.extend((server_url, alert_id))
            self._connection.execute(
                f"UPDATE alerts SET {', '.join(assignments)} WHERE server_url = ? AND alert_id = ?",
                parameters,
            )
            row = self._get_row_locked(server_url, alert_id)
        return self._row_to_stored(row) if row is not None else None

    def snooze(
        self,
        server_url: str,
        alert_id: str,
        until: datetime | str,
        *,
        note: str | None = None,
    ) -> StoredAlert | None:
        """Put an alert into the snoozed lifecycle until an ISO-8601 timestamp."""
        return self.update_lifecycle(
            server_url,
            alert_id,
            AlertLifecycle.SNOOZED,
            snoozed_until=until,
            note=note,
        )

    def update_note(self, server_url: str, alert_id: str, note: str) -> StoredAlert | None:
        """Replace (or clear with an empty string) an alert's local note."""
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE alerts SET note = ? WHERE server_url = ? AND alert_id = ?",
                (_note(note), server_url, alert_id),
            )
            row = self._get_row_locked(server_url, alert_id)
        return self._row_to_stored(row) if row is not None else None

    def unread_count(self, server_url: str | None = None) -> int:
        """Count locally unread alerts, optionally for one server."""
        active_values = (
            AlertLifecycle.UNREAD.value,
            AlertLifecycle.ACKNOWLEDGED.value,
            AlertLifecycle.RESOLVED.value,
        )
        sql = (
            "SELECT COUNT(*) FROM alerts WHERE requires_attention = 1 "
            "AND lifecycle IN (?, ?, ?)"
        )
        parameters: list[Any] = list(active_values)
        if server_url is not None:
            sql += " AND server_url = ?"
            parameters.append(server_url)
        with self._lock:
            row = self._connection.execute(sql, parameters).fetchone()
        return int(row[0])

    def wake_snoozed(self, now: datetime | str | None = None) -> int:
        """Atomically return expired snoozed alerts to the unread lifecycle."""
        checkpoint = _timestamp(now)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE alerts
                SET lifecycle = ?, snoozed_until = NULL
                WHERE requires_attention = 1
                  AND lifecycle = ?
                  AND snoozed_until IS NOT NULL
                  AND snoozed_until <= ?
                """,
                (
                    AlertLifecycle.UNREAD.value,
                    AlertLifecycle.SNOOZED.value,
                    checkpoint,
                ),
            )
            return max(cursor.rowcount, 0)

    def set_cursor(self, server_url: str, sequence: int) -> int:
        """Advance a server checkpoint monotonically and return the persisted cursor."""
        server = _required_text(server_url, "server_url")
        checkpoint = _sequence(sequence)
        with self._lock, self._connection:
            self._set_cursor_locked(server, checkpoint)
            row = self._connection.execute(
                "SELECT sequence FROM server_cursors WHERE server_url = ?", (server,)
            ).fetchone()
        if row is None:
            raise RuntimeError("Cursor update did not produce a checkpoint")
        return int(row[0])

    def highest_sequence(self, server_url: str) -> int | None:
        """Return the durable replay cursor for a server, if one has been seen."""
        with self._lock:
            row = self._connection.execute(
                "SELECT sequence FROM server_cursors WHERE server_url = ?", (server_url,)
            ).fetchone()
        return int(row[0]) if row is not None else None

    def get_cursor(self, server_url: str) -> int | None:
        """Alias for :meth:`highest_sequence`."""
        return self.highest_sequence(server_url)

    def prune(
        self,
        *,
        retention_days: int | None = None,
        max_rows: int | None = None,
        now: datetime | str | None = None,
    ) -> int:
        """Delete expired/overflow rows while retaining replay cursors."""
        if retention_days is not None and (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or retention_days < 0
        ):
            raise ValueError("retention_days must be a non-negative integer or None")
        if max_rows is not None and (
            isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 0
        ):
            raise ValueError("max_rows must be a non-negative integer or None")
        if retention_days is None and max_rows is None:
            return 0

        with self._lock, self._connection:
            changes_before = self._connection.total_changes
            if retention_days is not None:
                reference = datetime.now(UTC) if now is None else _as_datetime(now)
                cutoff = _timestamp(reference - timedelta(days=retention_days))
                self._connection.execute("DELETE FROM alerts WHERE received_at < ?", (cutoff,))
            if max_rows is not None:
                self._connection.execute(
                    """
                    DELETE FROM alerts WHERE rowid IN (
                        SELECT rowid FROM alerts
                        ORDER BY received_at DESC, rowid DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (max_rows,),
                )
            return self._connection.total_changes - changes_before

    def clear(self, server_url: str | None = None, *, reset_cursor: bool = False) -> int:
        """Clear inbox rows; replay cursors are preserved unless explicitly reset."""
        with self._lock, self._connection:
            if server_url is None:
                cursor = self._connection.execute("DELETE FROM alerts")
                if reset_cursor:
                    self._connection.execute("DELETE FROM server_cursors")
            else:
                cursor = self._connection.execute(
                    "DELETE FROM alerts WHERE server_url = ?", (server_url,)
                )
                if reset_cursor:
                    self._connection.execute(
                        "DELETE FROM server_cursors WHERE server_url = ?", (server_url,)
                    )
            return max(cursor.rowcount, 0)

    def export_json(
        self,
        *,
        text: str | None = None,
        severity: str | Severity | Iterable[str | Severity] | None = None,
        status: str | AlertLifecycle | Iterable[str | AlertLifecycle] | None = None,
        channel: str | Iterable[str] | None = None,
        server: str | Iterable[str] | None = None,
        server_url: str | Iterable[str] | None = None,
    ) -> str:
        """Export matching inbox rows as an indented JSON array."""
        records = self.query(
            text=text,
            severity=severity,
            status=status,
            channel=channel,
            server=server,
            server_url=server_url,
            limit=None,
        )
        return (
            json.dumps(
                [record.to_mapping() for record in records],
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def export_csv(
        self,
        *,
        text: str | None = None,
        severity: str | Severity | Iterable[str | Severity] | None = None,
        status: str | AlertLifecycle | Iterable[str | AlertLifecycle] | None = None,
        channel: str | Iterable[str] | None = None,
        server: str | Iterable[str] | None = None,
        server_url: str | Iterable[str] | None = None,
    ) -> str:
        """Export matching inbox rows as UTF-8-compatible CSV text."""
        records = self.query(
            text=text,
            severity=severity,
            status=status,
            channel=channel,
            server=server,
            server_url=server_url,
            limit=None,
        )
        fields = (
            "server_url",
            "alert_id",
            "origin",
            "received_at",
            "requires_attention",
            "lifecycle",
            "snoozed_until",
            "note",
            "sequence",
            "title",
            "message",
            "severity",
            "channel",
            "source",
            "created_at",
            "actions",
            "payload",
        )
        output = StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for record in records:
            alert = record.alert
            row: dict[str, object] = {
                "server_url": record.server_url,
                "alert_id": alert.id,
                "origin": record.origin,
                "received_at": record.received_at,
                "requires_attention": record.alert.requires_attention,
                "lifecycle": record.lifecycle.value,
                "snoozed_until": record.snoozed_until or "",
                "note": record.note,
                "sequence": "" if record.sequence is None else record.sequence,
                "title": alert.title,
                "message": alert.message,
                "severity": alert.severity.value,
                "channel": alert.channel,
                "source": alert.source,
                "created_at": alert.created_at,
                "actions": json.dumps(
                    [action.to_payload() for action in alert.actions], ensure_ascii=False
                ),
                "payload": json.dumps(dict(record.payload), ensure_ascii=False, sort_keys=True),
            }
            writer.writerow({key: _csv_cell(value) for key, value in row.items()})
        return output.getvalue()
