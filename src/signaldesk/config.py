"""Small JSON-backed configuration store for SignalDesk."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from signaldesk.models import normalize_channel

DEFAULT_SERVER_URL = "http://127.0.0.1:8765"
DEFAULT_SUBSCRIPTIONS = ["infrastructure", "security", "deployments"]


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


def _default_config_path() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "SignalDesk" / "config.json"


@dataclass(slots=True)
class AppConfig:
    server_url: str = DEFAULT_SERVER_URL
    subscriptions: list[str] = field(default_factory=lambda: list(DEFAULT_SUBSCRIPTIONS))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AppConfig:
        try:
            server_url = normalize_server_url(value.get("server_url"))
        except ValueError:
            server_url = DEFAULT_SERVER_URL

        raw_subscriptions = value.get("subscriptions", DEFAULT_SUBSCRIPTIONS)
        if not isinstance(raw_subscriptions, list):
            raw_subscriptions = DEFAULT_SUBSCRIPTIONS
        subscriptions = sorted(
            {
                normalize_channel(item, fallback="")
                for item in raw_subscriptions
                if normalize_channel(item, fallback="")
            }
        )
        return cls(server_url=server_url, subscriptions=subscriptions)


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
            json.dumps(asdict(config), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
