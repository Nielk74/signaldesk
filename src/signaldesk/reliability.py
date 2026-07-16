"""Pure connection-watchdog state used by the desktop controller."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WatchdogEvent:
    server_url: str
    kind: str
    offline_seconds: int


@dataclass(slots=True)
class _ServerState:
    state: str
    changed_at: float
    warned: bool = False


class ConnectionWatchdog:
    """Detect prolonged outages and a later recovery without repeated warnings."""

    def __init__(self, threshold_seconds: int = 30) -> None:
        self.threshold_seconds = self._clean_threshold(threshold_seconds)
        self._servers: dict[str, _ServerState] = {}

    @staticmethod
    def _clean_threshold(value: object) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 30
        return max(10, min(parsed, 3600))

    def set_threshold(self, seconds: int) -> None:
        self.threshold_seconds = self._clean_threshold(seconds)

    def set_servers(self, urls: set[str], *, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        self._servers = {
            url: self._servers.get(url, _ServerState("connecting", moment)) for url in urls
        }

    def change(self, url: str, state: str, *, now: float | None = None) -> WatchdogEvent | None:
        moment = time.monotonic() if now is None else now
        previous = self._servers.get(url)
        if previous is not None and previous.state == state:
            return None

        if state == "connected":
            event = None
            if previous is not None and previous.warned:
                event = WatchdogEvent(
                    server_url=url,
                    kind="restored",
                    offline_seconds=max(0, round(moment - previous.changed_at)),
                )
            self._servers[url] = _ServerState(state, moment)
            return event

        if previous is not None and previous.state != "connected":
            # Connecting -> disconnected is one continuous outage window.
            self._servers[url] = _ServerState(state, previous.changed_at, previous.warned)
        else:
            self._servers[url] = _ServerState(state, moment)
        return None

    def poll(self, *, now: float | None = None) -> list[WatchdogEvent]:
        moment = time.monotonic() if now is None else now
        events: list[WatchdogEvent] = []
        for url, current in self._servers.items():
            if current.state == "connected" or current.warned:
                continue
            elapsed = moment - current.changed_at
            if elapsed < self.threshold_seconds:
                continue
            current.warned = True
            events.append(
                WatchdogEvent(
                    server_url=url,
                    kind="offline",
                    offline_seconds=max(0, round(elapsed)),
                )
            )
        return events

    def offline_seconds(self, url: str, *, now: float | None = None) -> int:
        current = self._servers.get(url)
        if current is None or current.state == "connected":
            return 0
        moment = time.monotonic() if now is None else now
        return max(0, round(moment - current.changed_at))
