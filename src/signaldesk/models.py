"""Shared, UI-independent alert protocol models."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

CHANNEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


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

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Alert:
        if not isinstance(payload, Mapping):
            raise TypeError("Alert payload must be a mapping")

        try:
            duration = int(payload.get("duration_ms", 7000))
        except (TypeError, ValueError):
            duration = 7000

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
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "message": self.message,
            "severity": self.severity.value,
            "channel": self.channel,
            "source": self.source,
            "created_at": self.created_at or utc_now_iso(),
            "duration_ms": self.duration_ms,
        }


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
