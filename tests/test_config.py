import json

import pytest

from signaldesk.config import AppConfig, ConfigStore, normalize_server_url


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


def test_config_store_round_trip(tmp_path) -> None:
    store = ConfigStore(tmp_path / "nested" / "config.json")
    expected = AppConfig(
        server_url="https://alerts.example.com",
        subscriptions=["billing", "security"],
    )

    store.save(expected)

    assert store.load() == expected
    assert json.loads(store.path.read_text(encoding="utf-8"))["subscriptions"] == [
        "billing",
        "security",
    ]


def test_config_store_recovers_from_invalid_json(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("not-json", encoding="utf-8")

    config = ConfigStore(path).load()

    assert config.server_url == "http://127.0.0.1:8765"
    assert "infrastructure" in config.subscriptions


def test_config_mapping_filters_invalid_subscriptions() -> None:
    config = AppConfig.from_mapping(
        {
            "server_url": "not a valid url",
            "subscriptions": ["security", "security", "../../bad", "deployments"],
        }
    )

    assert config.server_url == "http://127.0.0.1:8765"
    assert config.subscriptions == ["deployments", "security"]
