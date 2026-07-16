"""Development Socket.IO server used to exercise SignalDesk end to end."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import inspect
import itertools
import json
import logging
import os
import random
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import socketio
import uvicorn

from signaldesk.models import Alert, AlertChannel, Severity, utc_now_iso

LOGGER = logging.getLogger("signaldesk.mock")
DEFAULT_EVENT_LOG_SIZE = 500
LIFECYCLE_STATES = frozenset({"unread", "snoozed"})

CHANNELS = [
    AlertChannel("infrastructure", "Infrastructure", "Hosts, services, and capacity signals"),
    AlertChannel("security", "Security", "Access, policy, and threat detection events"),
    AlertChannel("deployments", "Deployments", "Build and release lifecycle updates"),
    AlertChannel("billing", "Billing", "Usage limits, invoices, and payment events"),
    AlertChannel("product", "Product", "Feature flags and product announcements"),
]

DEMO_ALERTS = [
    {
        "title": "API latency recovered",
        "message": (
            "The p95 response time is back below 180 ms in the EU region. "
            "Dashboard: https://status.example.com/latency"
        ),
        "severity": "success",
        "channel": "infrastructure",
        "source": "Edge monitor",
    },
    {
        "title": "Unusual sign-in blocked",
        "message": (
            "A sign-in from a new location was blocked pending verification. "
            "Review it at https://security.example.com/signins/9f2a"
        ),
        "severity": "warning",
        "channel": "security",
        "source": "Identity guard",
        "requires_attention": True,
    },
    {
        "title": "Release completed",
        "message": (
            "Version 2.4.0 is healthy on all production instances. "
            "Release notes: https://github.com/example/app/releases/tag/v2.4.0"
        ),
        "severity": "success",
        "channel": "deployments",
        "source": "Release pipeline",
    },
    {
        "title": "Database connection pressure",
        "message": (
            "The primary pool is at 92% utilization. Scaling is in progress. "
            "Runbook: https://wiki.example.com/runbooks/db-pool"
        ),
        "severity": "critical",
        "channel": "infrastructure",
        "source": "Database monitor",
        "requires_attention": True,
    },
    {
        "title": "Usage threshold reached",
        "message": (
            "This workspace has consumed 80% of its monthly event allowance. "
            "Manage limits: https://billing.example.com/usage"
        ),
        "severity": "info",
        "channel": "billing",
        "source": "Usage service",
    },
]


def normalize_resume_after(value: object) -> int | None:
    """Return a valid non-negative replay cursor, or ``None`` when absent."""
    if value is None or isinstance(value, bool):
        return None
    try:
        cursor = int(value)
    except (TypeError, ValueError):
        return None
    return cursor if cursor >= 0 else None


def token_is_authorized(required_token: str | None, supplied_token: object) -> bool:
    """Check optional token auth with a timing-safe comparison when enabled."""
    if required_token is None:
        return True
    return isinstance(supplied_token, str) and hmac.compare_digest(
        supplied_token, required_token
    )


def build_recovery_batch(
    events: list[dict[str, Any]],
    subscriptions: set[str],
    resume_after: int | None,
    latest_sequence: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select retained subscribed events after a cursor and describe any gap."""
    cursor = normalize_resume_after(resume_after)
    first_sequence = normalize_resume_after(events[0].get("sequence")) if events else None
    oldest_available = first_sequence if first_sequence is not None else latest_sequence + 1
    gap = (
        cursor is not None
        and latest_sequence > cursor
        and cursor < oldest_available - 1
    )
    recovered: list[dict[str, Any]] = []
    if cursor is not None:
        for event in events:
            sequence = normalize_resume_after(event.get("sequence"))
            if (
                sequence is not None
                and sequence > cursor
                and str(event.get("channel", "")) in subscriptions
            ):
                replay = dict(event)
                replay["replayed"] = True
                recovered.append(replay)

    metadata = {
        "recovered_count": len(recovered),
        "latest_sequence": latest_sequence,
        "oldest_available_sequence": oldest_available,
        "gap": gap,
        "truncated": gap,
    }
    return recovered, metadata


def validate_lifecycle_payload(data: object) -> dict[str, str]:
    """Validate an incoming lifecycle action without retaining arbitrary data."""
    if not isinstance(data, dict):
        raise ValueError("Lifecycle payload must be an object")
    alert_id = str(data.get("id", "")).strip()
    if not alert_id:
        raise ValueError("Alert id is required")
    status = str(data.get("status", "")).strip().lower()
    if status not in LIFECYCLE_STATES:
        raise ValueError("Status must be unread or snoozed")

    payload = {"id": alert_id[:80], "status": status}
    snoozed_until = str(data.get("snoozed_until", "")).strip()
    if status == "snoozed" and not snoozed_until:
        raise ValueError("Snooze time is required for a snoozed alert")
    if status == "snoozed":
        try:
            datetime.fromisoformat(snoozed_until.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Snooze time must be an ISO-8601 timestamp") from exc
        payload["snoozed_until"] = snoozed_until[:64]
    note = " ".join(str(data.get("note", "")).split())
    if note:
        payload["note"] = note[:500]
    return payload


class MockAlertServer:
    def __init__(
        self,
        *,
        demo: bool = False,
        demo_interval: float = 8.0,
        auth_token: str | None = None,
        event_log_size: int = DEFAULT_EVENT_LOG_SIZE,
    ) -> None:
        self.sio = socketio.AsyncServer(async_mode="asgi", logger=False, engineio_logger=False)
        # sid -> subscribed channels (source of truth for /health and cleanup).
        self.clients: dict[str, set[str]] = {}
        # channel -> subscribed sids (reverse index for O(1) delivery counts).
        self._members: dict[str, set[str]] = {}
        self._client_ids: dict[str, str] = {}
        self._auth_token = auth_token if auth_token else None
        self._event_log: deque[dict[str, Any]] = deque(maxlen=max(1, event_log_size))
        self._latest_sequence = 0
        self._event_lock = asyncio.Lock()
        self.demo = demo
        self.demo_interval = max(2.0, demo_interval)
        self._demo_task: asyncio.Task[None] | None = None
        self._register_events()
        self.app = socketio.ASGIApp(self.sio, other_asgi_app=self._control_app)

    def _register_events(self) -> None:
        @self.sio.event
        async def connect(sid: str, environ: dict[str, Any], auth: Any = None) -> None:
            del environ
            if self._auth_token is not None:
                supplied_token = auth.get("token") if isinstance(auth, dict) else None
                if not token_is_authorized(self._auth_token, supplied_token):
                    LOGGER.warning("Client authentication failed: %s", sid)
                    raise socketio.exceptions.ConnectionRefusedError("Authentication failed")
            supplied = auth.get("subscriptions") if isinstance(auth, dict) else None
            requested = (
                supplied
                if isinstance(supplied, list)
                else ["infrastructure", "security", "deployments"]
            )
            resume_after = normalize_resume_after(
                auth.get("resume_after") if isinstance(auth, dict) else None
            )
            client_id = str(auth.get("client_id", "")).strip() if isinstance(auth, dict) else ""
            if client_id:
                self._client_ids[sid] = client_id[:80]
            await self.sio.emit("catalog", self.catalog_payload(), to=sid)
            # Joining rooms, taking the replay snapshot, and sending the
            # recovery boundary are atomic relative to live publication.
            async with self._event_lock:
                subscriptions = await self._apply_subscriptions(sid, requested)
                await self.sio.emit(
                    "subscriptions:confirmed",
                    {"subscriptions": sorted(subscriptions)},
                    to=sid,
                )
                recovered, metadata = build_recovery_batch(
                    list(self._event_log),
                    subscriptions,
                    resume_after,
                    self._latest_sequence,
                )
                for payload in recovered:
                    await self.sio.emit("alert", payload, to=sid)
                await self.sio.emit("recovery:complete", metadata, to=sid)
            LOGGER.info("Client connected: %s", sid)
            if self.demo and self._demo_task is None:
                self._demo_task = asyncio.create_task(
                    self._demo_loop(), name="signaldesk-demo-alerts"
                )

        @self.sio.event
        async def disconnect(sid: str, reason: Any = None) -> None:
            self._client_ids.pop(sid, None)
            for channel in self.clients.pop(sid, set()):
                members = self._members.get(channel)
                if members is not None:
                    members.discard(sid)
                    if not members:
                        self._members.pop(channel, None)
            LOGGER.info("Client disconnected: %s (%s)", sid, reason or "unknown")

        @self.sio.on("subscriptions:update")
        async def update_subscriptions(sid: str, data: Any) -> dict[str, Any]:
            supplied = data.get("subscriptions", []) if isinstance(data, dict) else []
            subscriptions = await self._apply_subscriptions(sid, supplied)
            payload = {"subscriptions": sorted(subscriptions)}
            await self.sio.emit("subscriptions:confirmed", payload, to=sid)
            return payload

        @self.sio.on("health:ping")
        async def health_ping(sid: str, data: Any) -> None:
            nonce = data.get("nonce", "") if isinstance(data, dict) else ""
            await self.sio.emit(
                "health:pong",
                {"nonce": nonce, "server_time": utc_now_iso()},
                to=sid,
            )

        @self.sio.on("alert:test")
        async def test_alert(sid: str, data: Any = None) -> dict[str, Any]:
            del data
            subscriptions = self.clients.get(sid, set())
            channel = sorted(subscriptions)[0] if subscriptions else "infrastructure"
            alert = Alert(
                id=str(uuid.uuid4()),
                title="Socket test received",
                message=(
                    "The complete real-time alert path is working correctly. "
                    "Docs: https://github.com/Nielk74/signaldesk"
                ),
                severity=Severity.SUCCESS,
                channel=channel,
                source="Mock server",
                created_at=utc_now_iso(),
                duration_ms=7000,
            )
            _delivered, payload = await self._publish_alert(alert, to=sid, delivered=1)
            return {"ok": True, "id": alert.id, "sequence": payload["sequence"]}

        @self.sio.on("alert:lifecycle")
        async def alert_lifecycle(sid: str, data: Any) -> dict[str, Any]:
            try:
                lifecycle = validate_lifecycle_payload(data)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}

            lifecycle["updated_at"] = utc_now_iso()
            client_id = self._client_ids.get(sid)
            if client_id:
                lifecycle["client_id"] = client_id
            confirmation: dict[str, Any] = {"ok": True, **lifecycle}
            async with self._event_lock:
                channel = ""
                for event in reversed(self._event_log):
                    if str(event.get("id", "")) == lifecycle["id"]:
                        if not Alert.from_payload(event).requires_attention:
                            return {
                                "ok": False,
                                "error": "This alert does not allow reminders",
                            }
                        event.pop("snoozed_until", None)
                        event.pop("lifecycle", None)
                        event.update(lifecycle)
                        channel = str(event.get("channel", ""))
                        break
                if not channel:
                    return {"ok": False, "error": "Alert was not found"}
                # Always confirm to the requester, even if they unsubscribed
                # after receiving the alert. Other clients only see lifecycle
                # state for channels to which they are currently subscribed.
                await self.sio.emit("alert:lifecycle:confirmed", confirmation, to=sid)
                await self.sio.emit(
                    "alert:lifecycle:confirmed",
                    confirmation,
                    room=channel,
                    skip_sid=sid,
                )
            return confirmation

    async def _apply_subscriptions(self, sid: str, requested: Any) -> set[str]:
        """Reconcile a client's channel rooms with its requested subscriptions."""
        channels = self._filter_subscriptions(requested)
        previous = self.clients.get(sid, set())
        for channel in previous - channels:
            members = self._members.get(channel)
            if members is not None:
                members.discard(sid)
                if not members:
                    self._members.pop(channel, None)
            await self._leave_room(sid, channel)
        for channel in channels - previous:
            self._members.setdefault(channel, set()).add(sid)
            await self._enter_room(sid, channel)
        self.clients[sid] = channels
        return channels

    async def _enter_room(self, sid: str, channel: str) -> None:
        result = self.sio.enter_room(sid, channel)
        if inspect.isawaitable(result):
            await result

    async def _leave_room(self, sid: str, channel: str) -> None:
        result = self.sio.leave_room(sid, channel)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _filter_subscriptions(values: Any) -> set[str]:
        allowed = {channel.key for channel in CHANNELS}
        if not isinstance(values, list):
            return set()
        return {str(value) for value in values if str(value) in allowed}

    @staticmethod
    def catalog_payload() -> dict[str, Any]:
        return {
            "channels": [
                {
                    "key": channel.key,
                    "name": channel.name,
                    "description": channel.description,
                }
                for channel in CHANNELS
            ]
        }

    async def publish(self, alert: Alert) -> int:
        """Fan an alert out to every subscriber of its channel in one emit.

        Delivery uses the channel room, so cost is a single ``emit`` regardless
        of subscriber count rather than one round-trip per client.
        """
        members = self._members.get(alert.channel)
        delivered = len(members) if members else 0
        await self._publish_alert(
            alert,
            room=alert.channel,
            delivered=delivered,
        )
        LOGGER.info("Published %s alert to %d client(s)", alert.channel, delivered)
        return delivered

    async def _publish_alert(
        self,
        alert: Alert,
        *,
        room: str | None = None,
        to: str | None = None,
        delivered: int,
    ) -> tuple[int, dict[str, Any]]:
        """Sequence, retain, and emit one alert under the recovery boundary lock."""
        async with self._event_lock:
            self._latest_sequence += 1
            payload = alert.to_payload()
            payload["sequence"] = self._latest_sequence
            self._event_log.append(dict(payload))
            if delivered:
                if to is not None:
                    await self.sio.emit("alert", payload, to=to)
                elif room is not None:
                    await self.sio.emit("alert", payload, room=room)
        return delivered, payload

    async def _demo_loop(self) -> None:
        sequence = itertools.cycle(DEMO_ALERTS)
        try:
            while True:
                await asyncio.sleep(self.demo_interval)
                template = dict(next(sequence))
                template.update(
                    id=str(uuid.uuid4()),
                    created_at=utc_now_iso(),
                    duration_ms=random.choice([6000, 7000, 8000]),
                )
                await self.publish(Alert.from_payload(template))
        except asyncio.CancelledError:
            return

    async def _control_app(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self._json_response(send, 404, {"error": "Not found"})
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")
        if method == "GET" and path == "/":
            await self._json_response(
                send,
                200,
                {
                    "name": "SignalDesk mock server",
                    "socket_path": "/socket.io",
                    "health": "/health",
                    "publish": "POST /alert",
                },
            )
            return
        if method == "GET" and path == "/health":
            await self._json_response(send, 200, {"ok": True, "clients": len(self.clients)})
            return
        if method == "POST" and path == "/alert":
            try:
                body = await self._read_body(receive)
                payload = json.loads(body or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                payload.setdefault("channel", "infrastructure")
                payload.setdefault("source", "HTTP control")
                alert = Alert.from_payload(payload)
                delivered = await self.publish(alert)
                published_payload = next(
                    (
                        dict(item)
                        for item in reversed(self._event_log)
                        if item.get("id") == alert.id
                    ),
                    alert.to_payload(),
                )
                await self._json_response(
                    send,
                    202,
                    {"accepted": True, "delivered": delivered, "alert": published_payload},
                )
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                await self._json_response(send, 400, {"error": str(exc)})
            return
        await self._json_response(send, 404, {"error": "Not found"})

    @staticmethod
    async def _read_body(
        receive: Callable[[], Awaitable[dict[str, Any]]], maximum: int = 65_536
    ) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            event = await receive()
            chunk = event.get("body", b"")
            size += len(chunk)
            if size > maximum:
                raise ValueError("Request body is too large")
            chunks.append(chunk)
            if not event.get("more_body", False):
                return b"".join(chunks)

    @staticmethod
    async def _json_response(
        send: Callable[[dict[str, Any]], Awaitable[None]], status: int, payload: dict[str, Any]
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the SignalDesk mock Socket.IO server")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Interface to bind (default: 127.0.0.1)"
    )
    parser.add_argument("--port", default=8765, type=int, help="Port to bind (default: 8765)")
    parser.add_argument("--demo", action="store_true", help="Publish rotating demo alerts")
    parser.add_argument(
        "--interval", default=8.0, type=float, help="Seconds between demo alerts (minimum: 2)"
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("SIGNALDESK_MOCK_TOKEN"),
        help=(
            "Require this Socket.IO token (or set SIGNALDESK_MOCK_TOKEN; "
            "the environment variable avoids shell history)"
        ),
    )
    parser.add_argument(
        "--event-log-size",
        default=DEFAULT_EVENT_LOG_SIZE,
        type=int,
        help=f"Number of alerts retained for replay (default: {DEFAULT_EVENT_LOG_SIZE})",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose server logs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    server = MockAlertServer(
        demo=args.demo,
        demo_interval=args.interval,
        auth_token=args.token,
        event_log_size=args.event_log_size,
    )
    LOGGER.info("Mock server listening at http://%s:%d", args.host, args.port)
    LOGGER.info("Publish custom alerts with POST http://%s:%d/alert", args.host, args.port)
    uvicorn.run(
        server.app,
        host=args.host,
        port=args.port,
        log_level="debug" if args.verbose else "warning",
        access_log=args.verbose,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
