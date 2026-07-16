"""Shared, UI-independent alert protocol models."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

CHANNEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
ACTION_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_ALERT_ACTIONS = 4
MAX_SEQUENCE = (1 << 63) - 1


class Severity(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    CRITICAL = "critical"

    @classmethod
    def parse(cls, value: object) -> Severity:
        normalized = str(value or "info").strip().lower()
        aliases = {
            "warn": cls.WARNING,
            "error": cls.CRITICAL,
            "danger": cls.CRITICAL,
            "ok": cls.SUCCESS,
        }
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError:
            return cls.INFO


class AlertLifecycle(StrEnum):
    """Reminder states plus legacy values retained for stored-data compatibility."""

    UNREAD = "unread"
    # Old databases may still contain these values. Parsing normalizes them to
    # unread, and current UI/protocol paths never create them.
    ACKNOWLEDGED = "acknowledged"
    SNOOZED = "snoozed"
    RESOLVED = "resolved"

    @classmethod
    def parse(cls, value: object) -> AlertLifecycle:
        """Parse protocol values, using ``unread`` for unknown values."""
        normalized = str(value or cls.UNREAD.value).strip().lower()
        aliases = {
            "new": cls.UNREAD,
            "ack": cls.UNREAD,
            "acked": cls.UNREAD,
            "acknowledged": cls.UNREAD,
            "snooze": cls.SNOOZED,
            "closed": cls.UNREAD,
            "resolved": cls.UNREAD,
        }
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError:
            return cls.UNREAD


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clean_text(value: object, *, fallback: str, maximum: int) -> str:
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        cleaned = fallback
    if len(cleaned) <= maximum:
        return cleaned
    return f"{cleaned[: maximum - 1].rstrip()}…"


def normalize_channel(value: object, *, fallback: str = "general") -> str:
    channel = str(value or fallback).strip().lower().replace(" ", "-")
    if not CHANNEL_PATTERN.fullmatch(channel):
        return fallback
    return channel


def _parse_sequence(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        sequence = value
    elif isinstance(value, str) and value.strip().isdigit():
        sequence = int(value.strip())
    else:
        return None
    if 0 <= sequence <= MAX_SEQUENCE:
        return sequence
    return None


def _parse_bool(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "yes", "on", "1"}:
        return True
    if normalized in {"false", "no", "off", "0"}:
        return False
    return default


def _safe_http_url(value: object) -> str:
    url = str(value or "").strip()
    if not url or len(url) > 2048:
        raise ValueError("Alert action URL must be between 1 and 2048 characters")
    if "\\" in url or any(character.isspace() or ord(character) < 32 for character in url):
        raise ValueError("Alert action URL contains unsafe characters")

    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Alert action URL must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("Alert action URL must not contain credentials")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Alert action URL contains an invalid port") from exc
    return url


@dataclass(frozen=True, slots=True)
class AlertAction:
    """A validated link that may be rendered as an alert action button."""

    label: str
    url: str
    kind: str | None = None

    def __post_init__(self) -> None:
        label = _clean_text(self.label, fallback="", maximum=60)
        if not label:
            raise ValueError("Alert action label is required")

        kind = str(self.kind or "").strip().lower() or None
        if kind is not None and not ACTION_KIND_PATTERN.fullmatch(kind):
            raise ValueError("Alert action kind is invalid")

        object.__setattr__(self, "label", label)
        object.__setattr__(self, "url", _safe_http_url(self.url))
        object.__setattr__(self, "kind", kind)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AlertAction:
        if not isinstance(payload, Mapping):
            raise TypeError("Alert action payload must be a mapping")
        return cls(
            label=str(payload.get("label") or ""),
            url=str(payload.get("url") or ""),
            kind=str(payload["kind"]) if payload.get("kind") is not None else None,
        )

    def to_payload(self) -> dict[str, str]:
        payload = {"label": self.label, "url": self.url}
        if self.kind is not None:
            payload["kind"] = self.kind
        return payload


def _parse_actions(value: object) -> tuple[AlertAction, ...]:
    if not isinstance(value, (list, tuple)):
        return ()

    actions: list[AlertAction] = []
    # Bound both accepted actions and work spent on a malformed network payload.
    for item in value[: MAX_ALERT_ACTIONS * 4]:
        if not isinstance(item, Mapping):
            continue
        try:
            actions.append(AlertAction.from_payload(item))
        except (TypeError, ValueError):
            continue
        if len(actions) == MAX_ALERT_ACTIONS:
            break
    return tuple(actions)


@dataclass(frozen=True, slots=True)
class Alert:
    id: str
    title: str
    message: str
    severity: Severity = Severity.INFO
    channel: str = "general"
    source: str = "SignalDesk"
    created_at: str = ""
    duration_ms: int = 7000
    sequence: int | None = None
    requires_attention: bool = False
    lifecycle: AlertLifecycle = AlertLifecycle.UNREAD
    actions: tuple[AlertAction, ...] = ()

    def __post_init__(self) -> None:
        if not self.created_at:
            object.__setattr__(self, "created_at", utc_now_iso())
        if self.sequence is not None and _parse_sequence(self.sequence) != self.sequence:
            raise ValueError("Alert sequence must be a non-negative 64-bit integer")
        if not isinstance(self.lifecycle, AlertLifecycle):
            object.__setattr__(self, "lifecycle", AlertLifecycle.parse(self.lifecycle))
        elif self.lifecycle in {AlertLifecycle.ACKNOWLEDGED, AlertLifecycle.RESOLVED}:
            object.__setattr__(self, "lifecycle", AlertLifecycle.UNREAD)
        if not isinstance(self.requires_attention, bool):
            object.__setattr__(
                self,
                "requires_attention",
                _parse_bool(self.requires_attention),
            )
        if not self.requires_attention and self.lifecycle is not AlertLifecycle.UNREAD:
            object.__setattr__(self, "lifecycle", AlertLifecycle.UNREAD)
        if len(self.actions) > MAX_ALERT_ACTIONS or any(
            not isinstance(action, AlertAction) for action in self.actions
        ):
            raise ValueError(f"Alert actions must contain at most {MAX_ALERT_ACTIONS} actions")
        if not isinstance(self.actions, tuple):
            object.__setattr__(self, "actions", tuple(self.actions))

    @property
    def status(self) -> AlertLifecycle:
        """Protocol-friendly alias for :attr:`lifecycle`."""
        return self.lifecycle

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Alert:
        if not isinstance(payload, Mapping):
            raise TypeError("Alert payload must be a mapping")

        try:
            duration = int(payload.get("duration_ms", 7000))
        except (TypeError, ValueError):
            duration = 7000

        has_explicit_lifecycle = "lifecycle" in payload or "status" in payload
        requires_attention = (
            _parse_bool(payload.get("requires_attention"))
            if "requires_attention" in payload
            else has_explicit_lifecycle
        )

        return cls(
            id=_clean_text(payload.get("id"), fallback=str(uuid.uuid4()), maximum=80),
            title=_clean_text(payload.get("title"), fallback="New alert", maximum=120),
            message=_clean_text(
                payload.get("message"), fallback="No additional details were provided.", maximum=500
            ),
            severity=Severity.parse(payload.get("severity")),
            channel=normalize_channel(payload.get("channel")),
            source=_clean_text(payload.get("source"), fallback="SignalDesk", maximum=80),
            created_at=_clean_text(
                payload.get("created_at") or payload.get("timestamp"),
                fallback=utc_now_iso(),
                maximum=64,
            ),
            duration_ms=max(2500, min(duration, 30_000)),
            sequence=_parse_sequence(payload.get("sequence")),
            requires_attention=requires_attention,
            lifecycle=(
                AlertLifecycle.parse(payload.get("lifecycle") or payload.get("status"))
                if requires_attention
                else AlertLifecycle.UNREAD
            ),
            actions=_parse_actions(payload.get("actions")),
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "message": self.message,
            "severity": self.severity.value,
            "channel": self.channel,
            "source": self.source,
            "created_at": self.created_at or utc_now_iso(),
            "duration_ms": self.duration_ms,
            "requires_attention": self.requires_attention,
        }
        if self.sequence is not None:
            payload["sequence"] = self.sequence
        if self.lifecycle is not AlertLifecycle.UNREAD:
            payload["lifecycle"] = self.lifecycle.value
        if self.actions:
            payload["actions"] = [action.to_payload() for action in self.actions]
        return payload


@dataclass(frozen=True, slots=True)
class AlertChannel:
    key: str
    name: str
    description: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> AlertChannel:
        key = normalize_channel(payload.get("key") or payload.get("id"))
        return cls(
            key=key,
            name=_clean_text(
                payload.get("name"), fallback=key.replace("-", " ").title(), maximum=60
            ),
            description=_clean_text(
                payload.get("description"), fallback="Real-time alert channel", maximum=140
            ),
        )
