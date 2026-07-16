import json

import pytest

from signaldesk.config import (
    DEFAULT_SERVER_URL,
    AppConfig,
    ConfigStore,
    ServerConfig,
    normalize_server_url,
)
from signaldesk.policies import NoisePolicy


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("localhost:9000/", "http://localhost:9000"),
        ("ws://localhost:9000/socket/", "http://localhost:9000/socket"),
        ("wss://alerts.example.com", "https://alerts.example.com"),
    ],
)
def test_normalize_server_url(raw: str, expected: str) -> None:
    assert normalize_server_url(raw) == expected


def test_normalize_server_url_rejects_unsafe_values() -> None:
    with pytest.raises(ValueError):
        normalize_server_url("file:///tmp/server")
    with pytest.raises(ValueError):
        normalize_server_url("https://user:secret@example.com")


def test_config_store_round_trip_multiple_servers(tmp_path) -> None:
    store = ConfigStore(tmp_path / "nested" / "config.json")
    expected = AppConfig(
        servers=[
            ServerConfig(url="https://alerts.example.com", subscriptions=["billing", "security"]),
            ServerConfig(url="http://127.0.0.1:8765", subscriptions=["infrastructure"]),
        ]
    )

    store.save(expected)

    assert store.load() == expected
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert [server["url"] for server in raw["servers"]] == [
        "https://alerts.example.com",
        "http://127.0.0.1:8765",
    ]


def test_config_store_recovers_from_invalid_json(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("not-json", encoding="utf-8")

    config = ConfigStore(path).load()

    assert config.servers[0].url == DEFAULT_SERVER_URL
    assert "infrastructure" in config.servers[0].subscriptions


def test_config_migrates_legacy_single_server_layout() -> None:
    config = AppConfig.from_mapping(
        {
            "server_url": "wss://alerts.example.com",
            "subscriptions": ["security", "security", "../../bad", "deployments"],
        }
    )

    assert len(config.servers) == 1
    assert config.servers[0].url == "https://alerts.example.com"
    assert config.servers[0].subscriptions == ["deployments", "security"]


def test_config_round_trips_sound_settings(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig(
        servers=[ServerConfig(url="http://a:1", subscriptions=["security"])],
        sound_enabled=False,
        sounds={"info": "bell", "success": "chime", "warning": "alert", "critical": "none"},
    )
    store.save(config)
    assert store.load() == config


def test_config_sounds_default_and_filter() -> None:
    # Missing sounds -> defaults; unknown severities ignored; blank values dropped.
    config = AppConfig.from_mapping(
        {
            "servers": [{"url": "http://a:1"}],
            "sound_enabled": False,
            "sounds": {"critical": "bell", "bogus": "x", "info": ""},
        }
    )
    assert config.sound_enabled is False
    assert config.sounds["critical"] == "bell"
    assert config.sounds["info"] == "ping"  # default retained
    assert "bogus" not in config.sounds


def test_config_deduplicates_servers_by_url() -> None:
    config = AppConfig.from_mapping(
        {
            "servers": [
                {"url": "http://a:1", "subscriptions": ["security"]},
                {"url": "http://a:1", "subscriptions": ["billing"]},
                {"url": "not a url"},
            ]
        }
    )

    assert [server.url for server in config.servers] == ["http://a:1"]
    assert config.servers[0].subscriptions == ["security"]


def test_config_round_trips_reliability_policy_without_secrets(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig(
        servers=[
            ServerConfig(
                url="https://alerts.example.com",
                subscriptions=["security"],
                auth_enabled=True,
            )
        ],
        noise_policy=NoisePolicy(
            channel_modes={"security": "toast_only"},
            quiet_enabled=True,
            cooldown_seconds=90,
        ),
        retention_days=60,
        max_history=9000,
        launch_at_login=True,
        disconnect_warning_seconds=45,
        client_id="desktop-client-1",
    )
    store.save(config)

    loaded = store.load()
    assert loaded == config
    raw = store.path.read_text(encoding="utf-8")
    assert "auth_enabled" in raw
    assert "token" not in raw.lower()


def test_config_clamps_reliability_settings() -> None:
    config = AppConfig.from_mapping(
        {
            "retention_days": -2,
            "max_history": 999_999,
            "disconnect_warning_seconds": "bad",
            "client_id": "!",
        }
    )
    assert config.retention_days == 1
    assert config.max_history == 100_000
    assert config.disconnect_warning_seconds == 30
    assert len(config.client_id) >= 8
