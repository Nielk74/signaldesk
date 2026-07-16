"""Small JSON-backed configuration store for SignalDesk.

The configuration is multi-server: the app can hold connections to several
Socket.IO endpoints at once, each with its own channel subscriptions. Older
single-server config files (``server_url`` + ``subscriptions``) are still read.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from signaldesk.models import normalize_channel
from signaldesk.policies import NoisePolicy

DEFAULT_SERVER_URL = "http://127.0.0.1:8765"
DEFAULT_SUBSCRIPTIONS = ["infrastructure", "security", "deployments"]

# Per-severity alert sound ids (a built-in name, "none", or a .wav path).
SEVERITIES = ("info", "success", "warning", "critical")
DEFAULT_SOUNDS = {
    "info": "ping",
    "success": "chime",
    "warning": "alert",
    "critical": "siren",
}


def normalize_server_url(value: object) -> str:
    raw = str(value or DEFAULT_SERVER_URL).strip()
    if any(character.isspace() for character in raw):
        raise ValueError("Enter a valid HTTP or HTTPS server URL")
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlsplit(raw)
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme.lower(), parsed.scheme.lower())
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a valid HTTP or HTTPS server URL")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid HTTP or HTTPS server URL") from exc
    if parsed.username or parsed.password:
        raise ValueError("Credentials are not supported in the server URL")
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, path, parsed.query, ""))


def clean_subscriptions(values: Any) -> list[str]:
    """Return a sorted, de-duplicated list of valid channel keys."""
    if not isinstance(values, list):
        return list(DEFAULT_SUBSCRIPTIONS)
    cleaned = {
        normalize_channel(item, fallback="")
        for item in values
        if normalize_channel(item, fallback="")
    }
    return sorted(cleaned)


def app_data_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "SignalDesk"


def _default_config_path() -> Path:
    return app_data_dir() / "config.json"


def clean_sounds(values: Any) -> dict[str, str]:
    """Return a per-severity sound map, filling missing entries with defaults."""
    result = dict(DEFAULT_SOUNDS)
    if isinstance(values, dict):
        for severity in SEVERITIES:
            chosen = values.get(severity)
            if isinstance(chosen, str) and chosen:
                result[severity] = chosen
    return result


@dataclass(slots=True)
class ServerConfig:
    """A single Socket.IO endpoint and the channels subscribed on it."""

    url: str = DEFAULT_SERVER_URL
    subscriptions: list[str] = field(default_factory=lambda: list(DEFAULT_SUBSCRIPTIONS))
    name: str = ""
    auth_enabled: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ServerConfig:
        if not isinstance(value, Mapping):
            raise ValueError("Server entry must be an object")
        url = normalize_server_url(value.get("url"))
        name = " ".join(str(value.get("name", "") or "").split())[:60]
        return cls(
            url=url,
            subscriptions=clean_subscriptions(value.get("subscriptions", DEFAULT_SUBSCRIPTIONS)),
            name=name,
            auth_enabled=bool(value.get("auth_enabled", False)),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "subscriptions": list(self.subscriptions),
            "name": self.name,
            "auth_enabled": self.auth_enabled,
        }


def _default_servers() -> list[ServerConfig]:
    return [ServerConfig()]


@dataclass(slots=True)
class AppConfig:
    servers: list[ServerConfig] = field(default_factory=_default_servers)
    sound_enabled: bool = True
    sounds: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SOUNDS))
    noise_policy: NoisePolicy = field(default_factory=NoisePolicy)
    retention_days: int = 30
    max_history: int = 5000
    launch_at_login: bool = False
    disconnect_warning_seconds: int = 30
    client_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AppConfig:
        if not isinstance(value, Mapping):
            return cls()

        entries: list[ServerConfig] = []
        raw_servers = value.get("servers")
        if isinstance(raw_servers, list):
            for item in raw_servers:
                try:
                    entries.append(ServerConfig.from_mapping(item))
                except (ValueError, TypeError):
                    continue
        else:
            # Legacy single-server layout: {"server_url": ..., "subscriptions": [...]}.
            try:
                url = normalize_server_url(value.get("server_url"))
            except ValueError:
                url = DEFAULT_SERVER_URL
            entries.append(
                ServerConfig(
                    url=url,
                    subscriptions=clean_subscriptions(
                        value.get("subscriptions", DEFAULT_SUBSCRIPTIONS)
                    ),
                )
            )

        # De-duplicate by URL, preserving first occurrence; never end up empty.
        by_url: dict[str, ServerConfig] = {}
        for entry in entries:
            by_url.setdefault(entry.url, entry)
        return cls(
            servers=list(by_url.values()) or _default_servers(),
            sound_enabled=bool(value.get("sound_enabled", True)),
            sounds=clean_sounds(value.get("sounds")),
            noise_policy=NoisePolicy.from_mapping(value.get("noise_policy")),
            retention_days=_bounded_int(value.get("retention_days"), 30, 1, 3650),
            max_history=_bounded_int(value.get("max_history"), 5000, 100, 100_000),
            launch_at_login=bool(value.get("launch_at_login", False)),
            disconnect_warning_seconds=_bounded_int(
                value.get("disconnect_warning_seconds"), 30, 10, 3600
            ),
            client_id=_clean_client_id(value.get("client_id")),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "servers": [server.to_mapping() for server in self.servers],
            "sound_enabled": self.sound_enabled,
            "sounds": dict(self.sounds),
            "noise_policy": self.noise_policy.to_mapping(),
            "retention_days": self.retention_days,
            "max_history": self.max_history,
            "launch_at_login": self.launch_at_login,
            "disconnect_warning_seconds": self.disconnect_warning_seconds,
            "client_id": self.client_id,
        }


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _clean_client_id(value: object) -> str:
    text = str(value or "").strip()
    if 8 <= len(text) <= 80 and all(character.isalnum() or character in "-_" for character in text):
        return text
    return uuid.uuid4().hex


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_config_path()

    def load(self) -> AppConfig:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return AppConfig()
            return AppConfig.from_mapping(raw)
        except (OSError, json.JSONDecodeError, TypeError):
            return AppConfig()

    def save(self, config: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(config.to_mapping(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
