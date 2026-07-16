# SignalDesk

SignalDesk is a cross-platform desktop alert center. It maintains Socket.IO connections in the
background, persists every alert in a searchable inbox, and presents focused toast and sound
notifications according to operator-defined policies.

The repository includes a replay-capable mock server for testing the complete protocol locally.

## Features

- Durable SQLite inbox with full detail views, search, exact filters, fixed-size
  paged rendering, JSON/CSV export, configurable retention, and a bounded row limit
- Catch-up delivery using per-server sequence cursors, duplicate-safe persistence, quiet replay,
  and an explicit warning when a server's replay window contains a gap
- Optional per-alert reminders: servers decide which alerts expose Remind me; all other alerts
  remain equally durable without showing a classification
- Structured, validated HTTP/HTTPS alert actions for runbooks, dashboards, and incident tools
- Noise controls by severity, server, and channel, plus quiet hours, critical bypass, repeat
  cooldown with a temporary grouped-alert count, history-only delivery, and mute modes; alerts
  are always retained
- Automatic reconnect with exponential backoff, heartbeat health, disconnect watchdog alerts,
  recovery notices, and optional launch at login
- Optional per-server bearer tokens stored in the operating system's secure credential store;
  tokens never enter the JSON configuration or return to the UI
- Accessible, keyboard-operable PySide6 interface, selectable link-aware alert text, and
  non-focus-stealing stacked notifications with subtle floating hover guidance, action
  confirmations, and burst aggregation; activating a notification opens that alert's retained
  detail view
- Single-instance ownership prevents concurrent SignalDesk processes; launching it again activates
  the existing window
- Multi-server channel subscriptions, system-tray controls, and per-severity alert sounds

## Quick start

SignalDesk requires Python 3.11 or newer.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Start the mock server:

```powershell
signaldesk-mock --demo --interval 6
```

Start the desktop client in another terminal:

```powershell
signaldesk
```

The default endpoint is `http://127.0.0.1:8765`. Closing the management window keeps SignalDesk
in the system tray; `signaldesk --hidden` starts there directly. Notification motion follows the
Windows “Show animations” preference. On other platforms, set `SIGNALDESK_REDUCE_MOTION=1` to
disable slide and fade transitions.

### Optional authenticated mock server

Prefer the environment variable so a token is not left in shell history:

```powershell
$env:SIGNALDESK_MOCK_TOKEN = "development-secret"
signaldesk-mock --demo
```

In SignalDesk, open **Servers**, enter the token under that server, and select **Save token**. If
the OS has no secure keyring backend, SignalDesk refuses to retain the token and reports the
problem. Authentication is optional for servers that do not require it.

## Publish an alert

The mock server accepts `POST /alert`:

```powershell
$alert = @{
    id = "queue-pressure-42"
    title = "Production queue growing"
    message = "The jobs queue has remained above 2,000 items for five minutes."
    severity = "warning"
    channel = "infrastructure"
    source = "Queue monitor"
    actions = @(
        @{
            label = "Open runbook"
            url = "https://docs.example.com/runbooks/queue-pressure"
            kind = "runbook"
        }
    )
} | ConvertTo-Json -Depth 4

Invoke-RestMethod `
    -Uri http://127.0.0.1:8765/alert `
    -Method Post `
    -ContentType application/json `
    -Body $alert
```

Only clients subscribed to the alert's channel receive it. The mock server assigns the monotonic
`sequence` used for replay.

## Socket.IO protocol

SignalDesk uses the default namespace and `/socket.io` path.

| Direction | Event | Purpose |
| --- | --- | --- |
| Server → client | `alert` | Deliver a live or replayed alert |
| Server → client | `catalog` | Advertise available channels |
| Server → client | `subscriptions:confirmed` | Confirm active subscriptions |
| Client → server | `subscriptions:update` | Replace active subscriptions |
| Client → server | `health:ping` | Start a heartbeat round trip |
| Server → client | `health:pong` | Return the heartbeat nonce |
| Client → server | `alert:test` | Request a server-routed test alert |
| Server → client | `recovery:complete` | Close a replay batch and describe any gap |
| Client → server | `alert:lifecycle` | Set, change, or clear an alert reminder |
| Server → client | `alert:lifecycle:confirmed` | Confirm a reminder update to relevant clients |

The channel picker is populated exclusively from each server's latest `catalog`; SignalDesk does
not synthesize a fallback list. Until a server responds, its panel shows a waiting state, and an
empty catalog is shown as an explicit empty state. Catalogs are isolated per server and should use
this shape:

```json
{
  "channels": [
    {
      "key": "service-health",
      "name": "Service health",
      "description": "Availability and capacity signals"
    }
  ]
}
```

`key` is the stable subscription identifier. `name` and `description` are presentation metadata
owned by that server. Previously saved subscription keys are retained, but a key is only rendered
as an option when the current server advertises it.

On connection, SignalDesk sends an auth payload. Optional fields are omitted when unused:

```json
{
  "subscriptions": ["deployments", "infrastructure", "security"],
  "resume_after": 1842,
  "client_id": "60e912cbe9824d5aa6a06434c1bf0352",
  "token": "server-specific-secret"
}
```

The server should emit replayed alerts in ascending sequence order, setting `"replayed": true`,
then emit a recovery boundary:

```json
{
  "recovered_count": 3,
  "latest_sequence": 1847,
  "oldest_available_sequence": 1200,
  "gap": false,
  "truncated": false
}
```

SignalDesk advances its durable cursor only after an alert is committed. `gap`/`truncated` must be
true when events after the requested cursor have fallen out of the server's replay window.

An alert payload can opt into reminders and include up to four validated actions:

```json
{
  "id": "89d02e76-b7af-4975-9a46-68e1cda932dc",
  "title": "Database connection pressure",
  "message": "The primary pool is at 92% utilization.",
  "severity": "critical",
  "channel": "infrastructure",
  "source": "Database monitor",
  "created_at": "2026-07-15T12:00:00.000Z",
  "duration_ms": 7000,
  "sequence": 1847,
  "requires_attention": true,
  "lifecycle": "unread",
  "actions": [
    {
      "label": "Open dashboard",
      "url": "https://monitoring.example.com/pools/primary",
      "kind": "primary"
    }
  ]
}
```

Supported severities are `info`, `success`, `warning`, and `critical`. Reminders are
server-controlled per alert: `requires_attention: true` enables reminder controls. When the field
is absent or `false`, the alert is still persisted, searchable, exportable, and eligible for
configured toast/sound delivery, but it has no reminder controls or status label. For
compatibility, explicitly supplying
`lifecycle` or `status` opts in when `requires_attention` is omitted; an explicit `false` wins.

Lifecycle values are `unread` and `snoozed`. An outbound snooze requires an ISO-8601
`snoozed_until` value; `unread` clears a reminder. Older `acknowledged` and `resolved` values are
accepted as legacy input and treated as `unread`:

```json
{
  "id": "89d02e76-b7af-4975-9a46-68e1cda932dc",
  "status": "snoozed",
  "snoozed_until": "2026-07-15T14:00:00Z"
}
```

Servers must authenticate the handshake when a token is required, reject reminder changes for
alerts that do not allow them, authorize subscriptions and reminder changes, scope confirmations to
relevant channel members, bound their replay log, use TLS outside local development, and validate
alert producers.

## Mock server controls

```text
GET  /          Server information
GET  /health    Health and connected-client count
POST /alert     Validate, sequence, retain, and publish an alert
```

Useful options include `--token`, `--event-log-size`, `--demo`, `--interval`, and `--verbose`.
The server binds only to `127.0.0.1` by default.

## Project structure

```text
src/signaldesk/
  app.py             Controller, tray, routing, and feature integration
  history.py         SQLite inbox, replay cursors, filtering, retention, export
  socket_client.py   Threaded multi-server Socket.IO client and reconnect logic
  credentials.py     Fail-closed OS keyring integration
  single_instance.py Cross-process ownership and activation handoff
  policies.py        Noise-control policy evaluation
  reliability.py     Disconnect watchdog state machine
  startup.py         Per-user launch-at-login integration
  richtext.py        Safe URL linkification and selectable alert text
  sounds.py          Cached built-in sound synthesis and playback
  notifications.py   Stacked alert toasts and quick actions
  window.py          Management, history, policy, auth, and detail UI
  mock_server.py     Replay/auth/reminder-capable ASGI development server
  models.py          Validated protocol models
  config.py          Atomic non-secret preference storage
tests/               Unit, UI smoke, protocol, and controller integration tests
```

## Development

```powershell
pytest
ruff check .
```

To temporarily override the first saved endpoint:

```powershell
signaldesk --server https://alerts.example.com
```

## License

MIT
