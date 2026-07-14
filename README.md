# SignalDesk

SignalDesk is a small Python desktop app for real-time operational alerts. It stays available in the system tray, listens for named Socket.IO events in the background, and presents polished, stacked notifications at the top-right of the active screen.

The repository includes a mock server, so the complete socket path can be tested locally in a couple of minutes.

## What it includes

- Native Windows, macOS, and Linux interface built with PySide6
- Background Socket.IO client with automatic exponential-backoff reconnects
- Animated, non-focus-stealing notifications with info, success, warning, and critical states
- Multiple alerts stacked safely inside the current screen's working area
- Compact management window for connection health, heartbeat latency, transport, recent history, and channel subscriptions
- System-tray controls for opening the manager, reconnecting, testing, and quitting
- Persistent server URL and subscription preferences
- Mock ASGI Socket.IO server with demo alerts and an HTTP publish endpoint
- Unit tests and GitHub Actions checks

## Quick start

SignalDesk requires Python 3.11 or newer.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Start the mock server in one terminal:

```powershell
signaldesk-mock --demo --interval 6
```

Start the desktop client in another:

```powershell
signaldesk
```

The client connects to `http://127.0.0.1:8765` by default. Close the management window to keep SignalDesk running in the tray, or launch it quietly with `signaldesk --hidden`.

Notification motion follows the Windows “Show animations” accessibility preference. On other platforms, or for testing, set `SIGNALDESK_REDUCE_MOTION=1` to disable slide and fade transitions.

## Trigger a custom alert

The mock server accepts `POST /alert`. From PowerShell:

```powershell
$alert = @{
    title = "Production queue growing"
    message = "The jobs queue has remained above 2,000 items for five minutes."
    severity = "warning"
    channel = "infrastructure"
    source = "Queue monitor"
} | ConvertTo-Json

Invoke-RestMethod `
    -Uri http://127.0.0.1:8765/alert `
    -Method Post `
    -ContentType application/json `
    -Body $alert
```

Or with `curl`:

```bash
curl -X POST http://127.0.0.1:8765/alert \
  -H "Content-Type: application/json" \
  -d '{"title":"Release ready","message":"All checks passed.","severity":"success","channel":"deployments","source":"CI"}'
```

Only clients subscribed to the alert's channel receive it. The management window can enable or disable channels while the socket is live.

## Socket.IO protocol

SignalDesk uses the default Socket.IO namespace and path (`/socket.io`). A compatible server should implement these events:

| Direction | Event | Purpose |
| --- | --- | --- |
| Server → client | `alert` | Deliver an alert payload |
| Server → client | `catalog` | Advertise available subscription channels |
| Server → client | `subscriptions:confirmed` | Confirm the active channel set |
| Client → server | `subscriptions:update` | Replace the client's subscriptions |
| Client → server | `health:ping` | Start a heartbeat round-trip measurement |
| Server → client | `health:pong` | Return the heartbeat nonce |
| Client → server | `alert:test` | Request a server-routed test alert |

An `alert` payload looks like this:

```json
{
  "id": "89d02e76-b7af-4975-9a46-68e1cda932dc",
  "title": "Database connection pressure",
  "message": "The primary pool is at 92% utilization.",
  "severity": "critical",
  "channel": "infrastructure",
  "source": "Database monitor",
  "created_at": "2026-07-15T12:00:00.000Z",
  "duration_ms": 7000
}
```

Supported severities are `info`, `success`, `warning`, and `critical`. SignalDesk validates incoming data, limits oversized text, and clamps display duration between 2.5 and 30 seconds.

On connection, the client sends its current subscriptions in the Socket.IO auth payload:

```json
{
  "subscriptions": ["deployments", "infrastructure", "security"]
}
```

## Mock server controls

```text
GET  /          Server information
GET  /health    Health and connected-client count
POST /alert     Validate and publish an alert
```

Run `signaldesk-mock --help` for bind address, port, demo interval, and logging options. The mock server binds only to `127.0.0.1` by default.

## Project structure

```text
src/signaldesk/
  app.py             Application lifecycle and tray integration
  socket_client.py   Threaded Socket.IO client and reconnect logic
  notifications.py  Animated top-right alert stack
  window.py          Connection, history, and subscription interface
  mock_server.py     Socket.IO/ASGI development server
  models.py          Validated protocol models
  config.py          Atomic JSON preference storage
tests/               UI-independent unit tests
```

## Development

```powershell
pytest
ruff check .
```

To point the app at another endpoint without opening the settings tab:

```powershell
signaldesk --server https://alerts.example.com
```

The mock server intentionally has no authentication and should not be exposed to an untrusted network. A production deployment should use TLS, authenticate the Socket.IO connection, authorize subscriptions server-side, apply rate limits, and validate alert producers.

## License

MIT
