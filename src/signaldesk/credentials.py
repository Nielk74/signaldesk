"""Secure, per-server credential storage.

Tokens are deliberately kept out of SignalDesk's JSON configuration.  This
module talks directly to the active ``keyring`` backend and refuses backends
that are unavailable or known to store credentials as plain text.
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SERVICE_NAME = "io.github.nielk74.signaldesk.server-token.v1"


class CredentialUnavailable(RuntimeError):
    """Raised when the operating system has no usable secure credential store."""


def normalize_credential_url(server_url: str) -> str:
    """Return a stable URL representation used only to derive an account id."""
    raw = str(server_url or "").strip()
    if not raw or any(character.isspace() for character in raw):
        raise ValueError("A valid server URL is required")
    if "://" not in raw:
        raw = f"http://{raw}"

    parsed = urlsplit(raw)
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme.lower(), parsed.scheme.lower())
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A valid HTTP or HTTPS server URL is required")
    if parsed.username or parsed.password:
        raise ValueError("Credentials must not be embedded in the server URL")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("A valid HTTP or HTTPS server URL is required") from exc

    hostname = parsed.hostname.lower()
    if ":" in hostname:  # Preserve brackets for IPv6 URL authorities.
        hostname = f"[{hostname}]"
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    authority = hostname if port is None or default_port else f"{hostname}:{port}"
    path = parsed.path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((scheme, authority, path, query, ""))


def credential_account(server_url: str) -> str:
    """Return a non-secret, stable keyring account for ``server_url``."""
    normalized = normalize_credential_url(server_url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"server-{digest}"


def _load_keyring() -> Any:
    try:
        import keyring
    except ImportError:
        raise CredentialUnavailable(
            "Secure credential storage is unavailable; install the keyring dependency"
        ) from None
    return keyring


def _backend_priority(backend: Any) -> float:
    try:
        priority = backend.priority
        if callable(priority):
            priority = priority()
        return float(priority)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return 0.0


def _backend_is_secure(backend: Any) -> bool:
    """Conservatively reject unavailable and known plaintext backends."""
    if _backend_priority(backend) <= 0:
        return False
    identity = f"{type(backend).__module__}.{type(backend).__qualname__}".lower()
    insecure_markers = (
        "backends.fail",
        "nullkeyring",
        "plaintext",
        "uncrypted",
        "keyrings.alt",
    )
    return not any(marker in identity for marker in insecure_markers)


def _secure_backend() -> Any:
    keyring = _load_keyring()
    try:
        backend = keyring.get_keyring()
    except Exception:
        raise CredentialUnavailable("The system credential store could not be opened") from None

    # ChainerBackend can include an unsafe fallback. Pick a secure member
    # explicitly so credentials never fall through to a plaintext backend.
    identity = f"{type(backend).__module__}.{type(backend).__qualname__}".lower()
    if "chainer" in identity:
        candidates = getattr(backend, "backends", ())
        backend = next((candidate for candidate in candidates if _backend_is_secure(candidate)), None)
    if backend is None or not _backend_is_secure(backend):
        raise CredentialUnavailable("No secure system credential-store backend is available")
    return backend


def get_token(server_url: str) -> str | None:
    """Read a server token from the OS credential store, if one exists."""
    backend = _secure_backend()
    try:
        value = backend.get_password(SERVICE_NAME, credential_account(server_url))
    except Exception:
        raise CredentialUnavailable("The server token could not be read securely") from None
    return value if isinstance(value, str) and value else None


def set_token(server_url: str, token: str) -> None:
    """Store a non-empty server token in the OS credential store."""
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Token must not be empty")
    if len(token) > 4096:
        raise ValueError("Token must not exceed 4096 characters")
    value = token
    backend = _secure_backend()
    try:
        backend.set_password(SERVICE_NAME, credential_account(server_url), value)
    except Exception:
        raise CredentialUnavailable("The server token could not be stored securely") from None


def delete_token(server_url: str) -> bool:
    """Delete a stored token, returning whether one was present."""
    backend = _secure_backend()
    account = credential_account(server_url)
    try:
        if backend.get_password(SERVICE_NAME, account) is None:
            return False
        backend.delete_password(SERVICE_NAME, account)
    except Exception:
        raise CredentialUnavailable("The server token could not be deleted securely") from None
    return True
