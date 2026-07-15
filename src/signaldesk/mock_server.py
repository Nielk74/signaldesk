"""Development Socket.IO server used to exercise SignalDesk end to end."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import itertools
import json
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import socketio
import uvicorn

from signaldesk.models import Alert, AlertChannel, Severity, utc_now_iso

LOGGER = logging.getLogger("signaldesk.mock")

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
        "message": "The p95 response time is back below 180 ms in the EU region.",
        "severity": "success",
        "channel": "infrastructure",
        "source": "Edge monitor",
    },
    {
        "title": "Unusual sign-in blocked",
        "message": "A sign-in from a new location was blocked pending verification.",
        "severity": "warning",
        "channel": "security",
        "source": "Identity guard",
    },
    {
        "title": "Release completed",
        "message": "Version 2.4.0 is healthy on all production instances.",
        "severity": "success",
        "channel": "deployments",
        "source": "Release pipeline",
    },
    {
        "title": "Database connection pressure",
        "message": "The primary pool is at 92% utilization. Scaling is in progress.",
        "severity": "critical",
        "channel": "infrastructure",
        "source": "Database monitor",
    },
    {
        "title": "Usage threshold reached",
        "message": "This workspace has consumed 80% of its monthly event allowance.",
        "severity": "info",
        "channel": "billing",
        "source": "Usage service",
    },
]


class MockAlertServer:
    def __init__(self, *, demo: bool = False, demo_interval: float = 8.0) -> None:
        self.sio = socketio.AsyncServer(async_mode="asgi", logger=False, engineio_logger=False)
        # sid -> subscribed channels (source of truth for /health and cleanup).
        self.clients: dict[str, set[str]] = {}
        # channel -> subscribed sids (reverse index for O(1) delivery counts).
        self._members: dict[str, set[str]] = {}
        self.demo = demo
        self.demo_interval = max(2.0, demo_interval)
        self._demo_task: asyncio.Task[None] | None = None
        self._register_events()
        self.app = socketio.ASGIApp(self.sio, other_asgi_app=self._control_app)

    def _register_events(self) -> None:
        @self.sio.event
        async def connect(sid: str, environ: dict[str, Any], auth: Any = None) -> None:
            del environ
            supplied = auth.get("subscriptions") if isinstance(auth, dict) else None
            requested = (
                supplied
                if isinstance(supplied, list)
                else ["infrastructure", "security", "deployments"]
            )
            subscriptions = await self._apply_subscriptions(sid, requested)
            LOGGER.info("Client connected: %s", sid)
            await self.sio.emit("catalog", self.catalog_payload(), to=sid)
            await self.sio.emit(
                "subscriptions:confirmed",
                {"subscriptions": sorted(subscriptions)},
                to=sid,
            )
            if self.demo and self._demo_task is None:
                self._demo_task = asyncio.create_task(
                    self._demo_loop(), name="signaldesk-demo-alerts"
                )

        @self.sio.event
        async def disconnect(sid: str, reason: Any = None) -> None:
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
                message="The complete real-time alert path is working correctly.",
                severity=Severity.SUCCESS,
                channel=channel,
                source="Mock server",
                created_at=utc_now_iso(),
                duration_ms=7000,
            )
            await self.sio.emit("alert", alert.to_payload(), to=sid)
            return {"ok": True, "id": alert.id}

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
        if delivered:
            await self.sio.emit("alert", alert.to_payload(), room=alert.channel)
        LOGGER.info("Published %s alert to %d client(s)", alert.channel, delivered)
        return delivered

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
                await self._json_response(
                    send,
                    202,
                    {"accepted": True, "delivered": delivered, "alert": alert.to_payload()},
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
    parser.add_argument("--verbose", action="store_true", help="Enable verbose server logs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )
    server = MockAlertServer(demo=args.demo, demo_interval=args.interval)
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
