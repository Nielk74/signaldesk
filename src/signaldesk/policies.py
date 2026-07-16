"""Notification routing and noise-control policy evaluation.

Every alert is still written to the durable inbox.  Policies only decide
whether an event should interrupt the operator with a toast and/or sound.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class DeliveryMode(StrEnum):
    """How an alert should be surfaced after it is recorded."""

    TOAST_SOUND = "toast_sound"
    TOAST_ONLY = "toast_only"
    HISTORY_ONLY = "history_only"
    MUTED = "muted"

    @classmethod
    def parse(cls, value: object, default: DeliveryMode | None = None) -> DeliveryMode:
        fallback = default or cls.TOAST_SOUND
        try:
            return cls(str(value))
        except ValueError:
            return fallback


DEFAULT_SEVERITY_MODES = {
    "info": DeliveryMode.TOAST_SOUND.value,
    "success": DeliveryMode.TOAST_SOUND.value,
    "warning": DeliveryMode.TOAST_SOUND.value,
    "critical": DeliveryMode.TOAST_SOUND.value,
}


def _clean_modes(value: object, *, maximum: int = 256) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for raw_key, raw_mode in list(value.items())[:maximum]:
        key = str(raw_key).strip()
        if not key or len(key) > 256:
            continue
        try:
            result[key] = DeliveryMode(str(raw_mode)).value
        except ValueError:
            continue
    return result


def _clean_clock(value: object, default: str) -> str:
    text = str(value or default).strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError:
        return default
    return parsed.strftime("%H:%M")


@dataclass(slots=True)
class NoisePolicy:
    """Serializable notification overrides with clear precedence.

    Server overrides win over channel overrides, which win over severity
    defaults. Quiet hours and repeat cooldown are applied afterwards.
    """

    severity_modes: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SEVERITY_MODES))
    server_modes: dict[str, str] = field(default_factory=dict)
    channel_modes: dict[str, str] = field(default_factory=dict)
    quiet_enabled: bool = False
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"
    critical_bypass: bool = True
    cooldown_seconds: int = 30

    @classmethod
    def from_mapping(cls, value: object) -> NoisePolicy:
        if not isinstance(value, dict):
            return cls()
        severities = dict(DEFAULT_SEVERITY_MODES)
        severities.update(_clean_modes(value.get("severity_modes"), maximum=16))
        return cls(
            severity_modes=severities,
            server_modes=_clean_modes(value.get("server_modes")),
            channel_modes=_clean_modes(value.get("channel_modes")),
            quiet_enabled=bool(value.get("quiet_enabled", False)),
            quiet_start=_clean_clock(value.get("quiet_start"), "22:00"),
            quiet_end=_clean_clock(value.get("quiet_end"), "07:00"),
            critical_bypass=bool(value.get("critical_bypass", True)),
            cooldown_seconds=max(0, min(_as_int(value.get("cooldown_seconds"), 30), 3600)),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "severity_modes": dict(self.severity_modes),
            "server_modes": dict(self.server_modes),
            "channel_modes": dict(self.channel_modes),
            "quiet_enabled": self.quiet_enabled,
            "quiet_start": self.quiet_start,
            "quiet_end": self.quiet_end,
            "critical_bypass": self.critical_bypass,
            "cooldown_seconds": self.cooldown_seconds,
        }

    def mode_for(self, *, server_url: str, channel: str, severity: str) -> DeliveryMode:
        if server_url in self.server_modes:
            return DeliveryMode.parse(self.server_modes[server_url])
        if channel in self.channel_modes:
            return DeliveryMode.parse(self.channel_modes[channel])
        return DeliveryMode.parse(self.severity_modes.get(severity))

    def in_quiet_hours(self, moment: datetime) -> bool:
        if not self.quiet_enabled:
            return False
        current = moment.hour * 60 + moment.minute
        start_hour, start_minute = (int(part) for part in self.quiet_start.split(":"))
        end_hour, end_minute = (int(part) for part in self.quiet_end.split(":"))
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class NotificationDecision:
    """Result of evaluating an alert. Recording is intentionally unconditional."""

    record: bool
    show_toast: bool
    play_sound: bool
    mode: DeliveryMode
    reason: str = ""
    grouped_count: int = 1


class NotificationPolicyEngine:
    """Stateful evaluator implementing quiet hours and repeat cooldowns."""

    def __init__(self, policy: NoisePolicy | None = None) -> None:
        self.policy = policy or NoisePolicy()
        self._recent: dict[str, tuple[float, int]] = {}

    def set_policy(self, policy: NoisePolicy) -> None:
        self.policy = policy
        self._recent.clear()

    def decide(
        self,
        alert: object,
        server_url: str,
        *,
        now: datetime | None = None,
        monotonic_now: float | None = None,
    ) -> NotificationDecision:
        severity_value = getattr(getattr(alert, "severity", "info"), "value", None)
        severity = str(severity_value or getattr(alert, "severity", "info"))
        channel = str(getattr(alert, "channel", "general"))
        mode = self.policy.mode_for(
            server_url=server_url,
            channel=channel,
            severity=severity,
        )
        show_toast = mode in {DeliveryMode.TOAST_SOUND, DeliveryMode.TOAST_ONLY}
        play_sound = mode is DeliveryMode.TOAST_SOUND
        reason = ""

        moment = now or datetime.now().astimezone()
        if self.policy.in_quiet_hours(moment) and not (
            severity == "critical" and self.policy.critical_bypass
        ):
            show_toast = False
            play_sound = False
            reason = "quiet_hours"

        lifecycle = getattr(alert, "lifecycle", getattr(alert, "status", "unread"))
        status_value = getattr(lifecycle, "value", None)
        status = str(status_value or lifecycle)
        if status == "snoozed":
            show_toast = False
            play_sound = False
            reason = "lifecycle_update"

        grouped_count = 1
        cooldown = self.policy.cooldown_seconds
        if cooldown > 0 and (show_toast or play_sound):
            clock = time.monotonic() if monotonic_now is None else monotonic_now
            fingerprint = "\x1f".join(
                (
                    server_url,
                    channel,
                    severity,
                    str(getattr(alert, "source", "")),
                    str(getattr(alert, "title", "")),
                )
            )
            previous = self._recent.get(fingerprint)
            if previous is not None and clock - previous[0] < cooldown:
                grouped_count = previous[1] + 1
                self._recent[fingerprint] = (previous[0], grouped_count)
                show_toast = False
                play_sound = False
                reason = "repeat_cooldown"
            else:
                self._recent[fingerprint] = (clock, 1)
            cutoff = clock - max(cooldown * 2, 60)
            self._recent = {
                key: value for key, value in self._recent.items() if value[0] >= cutoff
            }

        return NotificationDecision(
            record=True,
            show_toast=show_toast,
            play_sound=play_sound,
            mode=mode,
            reason=reason,
            grouped_count=grouped_count,
        )
