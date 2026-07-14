from PySide6.QtWidgets import QApplication

from signaldesk.config import AppConfig
from signaldesk.models import Alert
from signaldesk.window import ManagementWindow


def test_management_window_renders_connection_and_alert_state() -> None:
    app = QApplication.instance() or QApplication([])
    window = ManagementWindow(AppConfig(), tray_available=False)
    window.set_connection_state("connected", "Listening for events")
    window.set_health(12, "Websocket")
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

    assert window.header_status.text() == "LIVE"
    assert window.latency_metric.value.text() == "12 ms"
    assert window.transport_metric.value.text() == "Websocket"
    assert window.history_count.text() == "1 received"

    window.prepare_to_quit()
    window.close()
