import json

import pytest

from signaldesk.config import (
    DEFAULT_SERVER_URL,
    AppConfig,
    ConfigStore,
    ServerConfig,
    normalize_server_url,
)


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
