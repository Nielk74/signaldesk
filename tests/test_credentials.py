from types import SimpleNamespace

import pytest

from signaldesk import credentials


class MemoryBackend:
    priority = 10

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        del self.values[(service, account)]


def install_backend(monkeypatch, backend) -> None:
    monkeypatch.setattr(
        credentials,
        "_load_keyring",
        lambda: SimpleNamespace(get_keyring=lambda: backend),
    )


def test_account_is_stable_for_equivalent_normalized_urls() -> None:
    first = credentials.credential_account("HTTP://Example.COM:80/")
    second = credentials.credential_account("http://example.com")

    assert first == second
    assert first.startswith("server-")
    assert "example" not in first


def test_secure_token_round_trip_and_delete(monkeypatch) -> None:
    backend = MemoryBackend()
    install_backend(monkeypatch, backend)

    assert credentials.get_token("https://alerts.example") is None
    credentials.set_token("https://alerts.example", "do-not-log-this")
    assert credentials.get_token("https://alerts.example") == "do-not-log-this"
    assert credentials.delete_token("https://alerts.example") is True
    assert credentials.delete_token("https://alerts.example") is False


def test_unavailable_backend_fails_closed(monkeypatch) -> None:
    backend = MemoryBackend()
    backend.priority = 0
    install_backend(monkeypatch, backend)

    with pytest.raises(credentials.CredentialUnavailable, match="No secure"):
        credentials.set_token("https://alerts.example", "secret")


def test_backend_errors_do_not_expose_token(monkeypatch) -> None:
    class BrokenBackend(MemoryBackend):
        def set_password(self, service: str, account: str, value: str) -> None:
            raise RuntimeError(f"failed for {value}")

    install_backend(monkeypatch, BrokenBackend())

    with pytest.raises(credentials.CredentialUnavailable) as captured:
        credentials.set_token("https://alerts.example", "very-sensitive")
    assert "very-sensitive" not in str(captured.value)


def test_token_validation_rejects_blank_and_unbounded_values(monkeypatch) -> None:
    install_backend(monkeypatch, MemoryBackend())
    with pytest.raises(ValueError, match="empty"):
        credentials.set_token("https://alerts.example", "   ")
    with pytest.raises(ValueError, match="4096"):
        credentials.set_token("https://alerts.example", "x" * 4097)
