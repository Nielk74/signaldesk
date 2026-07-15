from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from signaldesk.config import AppConfig
from signaldesk.models import Alert
from signaldesk.notifications import AlertToast
from signaldesk.window import ManagementWindow


def test_management_window_renders_connection_and_alert_state() -> None:
    app = QApplication.instance() or QApplication([])
    config = AppConfig()
    window = ManagementWindow(config, tray_available=False)
    url = config.servers[0].url
    window.set_server_state(url, "connected", "Listening for events")
    window.set_server_health(url, 12, "Websocket")
    window.add_alert(
        Alert.from_payload(
            {
                "title": "Release complete",
                "message": "All production checks passed.",
                "severity": "success",
                "channel": "deployments",
            }
        )
    )
    window.show()
    app.processEvents()

    card = window.status_card(url)
    assert card is not None
    assert window.header_status.text() == "1/1 LIVE"
    assert card.latency_metric.value.text() == "12 ms"
    assert card.transport_metric.value.text() == "Websocket"
    assert window.history_count.text() == "1 received"

    window.prepare_to_quit()
    window.close()


def test_management_window_supports_multiple_servers() -> None:
    app = QApplication.instance() or QApplication([])
    config = AppConfig.from_mapping(
        {
            "servers": [
                {"url": "http://a:1", "subscriptions": ["security"]},
                {"url": "http://b:2", "subscriptions": ["billing"]},
            ]
        }
    )
    window = ManagementWindow(config, tray_available=False)
    window.set_server_state("http://a:1", "connected", "up")
    window.set_server_state("http://b:2", "disconnected", "down")
    app.processEvents()

    assert window.status_card("http://a:1") is not None
    assert window.status_card("http://b:2") is not None
    assert window.header_status.text() == "1/2 LIVE"

    window.prepare_to_quit()
    window.close()


def test_alert_toast_renders_precision_surface() -> None:
    app = QApplication.instance() or QApplication([])
    alert = Alert.from_payload(
        {
            "title": "Database connection pressure",
            "message": "The primary pool is at 92% utilization.",
            "severity": "critical",
            "channel": "infrastructure",
            "source": "Database monitor",
            "duration_ms": 30_000,
        }
    )
    toast = AlertToast(alert)
    toast.show_at(QPoint(0, 0))
    app.processEvents()

    assert toast.width() == AlertToast.WIDTH
    assert toast.expiry_rail.height() == 3
    assert toast.accessibleName() == "Critical alert: Database connection pressure"

    toast.close()
