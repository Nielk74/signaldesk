from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QToolTip,
)

from signaldesk.config import AppConfig
from signaldesk.models import Alert
from signaldesk.notifications import (
    HOVER_HINT_DELAY_MS,
    MAX_VISIBLE_TOASTS,
    OVERFLOW_DISPLAY_MS,
    AlertToast,
    NotificationManager,
)
from signaldesk.richtext import apply_rich_text, make_selectable
from signaldesk.theme import APP_STYLESHEET
from signaldesk.window import (
    HISTORY_FILTER_DEBOUNCE_MS,
    HISTORY_PAGE_SIZE,
    AlertDetailDialog,
    AlertHistoryRow,
    ManagementWindow,
    SoundRow,
)


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


def test_server_channel_rows_come_only_from_each_server_catalog() -> None:
    app = QApplication.instance() or QApplication([])
    config = AppConfig.from_mapping(
        {
            "servers": [
                {"url": "http://a:1", "subscriptions": ["service-health"]},
                {"url": "http://b:2", "subscriptions": ["legacy-channel"]},
            ]
        }
    )
    window = ManagementWindow(config, tray_available=False)
    first = window._panels["http://a:1"]
    second = window._panels["http://b:2"]

    assert first._rows == {}
    assert first.count_label.text() == "Waiting"
    assert first.channel_state_label is not None
    assert "waiting" in first.channel_state_label.text().lower()

    window.set_server_catalog(
        "http://a:1",
        {
            "channels": [
                {
                    "key": "service-health",
                    "name": "Service health",
                    "description": "Availability and capacity signals",
                },
                {
                    "key": "release-notes",
                    "name": "Release notes",
                    "description": "Deployment lifecycle updates",
                },
            ]
        },
    )
    app.processEvents()

    assert list(first._rows) == ["service-health", "release-notes"]
    assert "infrastructure" not in first._rows
    assert first._rows["service-health"].channel.name == "Service health"
    row_text = {label.text() for label in first._rows["service-health"].findChildren(QLabel)}
    assert {"Service health", "Availability and capacity signals"}.issubset(row_text)
    assert first.count_label.text() == "1 selected"
    assert second._rows == {}
    assert second.count_label.text() == "Waiting"

    window.set_server_catalog("http://b:2", {"channels": []})
    app.processEvents()
    assert second._rows == {}
    assert second.count_label.text() == "No channels"
    assert second.channel_state_label is not None
    assert "advertised no channels" in second.channel_state_label.text().lower()

    window.prepare_to_quit()
    window.close()


def test_alert_text_is_selectable_and_links_clickable() -> None:
    # A QApplication must exist before constructing widgets.
    QApplication.instance() or QApplication([])

    plain = QLabel()
    make_selectable(plain)
    assert plain.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse

    linked = QLabel()
    apply_rich_text(linked, "visit https://example.com for details", "#00915A")
    flags = linked.textInteractionFlags()
    assert linked.openExternalLinks()
    assert flags & Qt.TextInteractionFlag.LinksAccessibleByMouse
    assert flags & Qt.TextInteractionFlag.TextSelectableByMouse
    assert "example.com" in linked.text()


def test_sounds_tab_exposes_per_severity_rows_and_emits() -> None:
    QApplication.instance() or QApplication([])
    window = ManagementWindow(AppConfig(), tray_available=False)
    assert window.tabs.count() == 5  # Overview, History, Servers, Policies, Sounds

    rows = window.findChildren(SoundRow)
    assert {row.severity for row in rows} == {"info", "success", "warning", "critical"}

    captured: dict[str, str] = {}
    window.sound_changed.connect(lambda sev, sid: captured.__setitem__(sev, sid))
    rows[0]._select("bell")
    assert captured[rows[0].severity] == "bell"

    toggles: list[bool] = []
    window.sound_enabled_changed.connect(toggles.append)
    window.sound_toggle.setChecked(not window.sound_toggle.isChecked())
    assert toggles

    window.prepare_to_quit()
    window.close()


def test_compact_icon_actions_and_progressive_disclosure() -> None:
    QApplication.instance() or QApplication([])
    window = ManagementWindow(AppConfig(), tray_available=False)
    panel = window._panels[window._order[0]]

    assert window.sound_toggle.text() == ""
    assert not window.sound_toggle.icon().isNull()
    assert window.sound_toggle.toolTip()
    assert window.sound_toggle.accessibleName()
    window.set_test_pending(True)
    assert window.test_button.property("iconName") == "clock"
    assert "waiting" in window.test_button.accessibleName().lower()
    window.set_test_pending(False)

    assert panel.alias_editor.isHidden()
    assert panel.auth_group.isHidden()
    panel.auth_button.click()
    assert not panel.auth_group.isHidden()
    assert panel.alias_editor.isHidden()
    panel.alias_button.click()
    assert not panel.alias_editor.isHidden()
    assert panel.auth_group.isHidden()

    assert window.history_filter_panel.isHidden()
    assert window.history_maintenance_panel.isHidden()
    assert window.clear_filters_button.property("iconName") == "filter_clear"
    assert not window.clear_filters_button.isEnabled()
    window.history_search.setText("database")
    assert window.clear_filters_button.isEnabled()
    window.clear_filters_button.click()
    assert not window.clear_filters_button.isEnabled()
    window.history_filter_button.click()
    assert not window.history_filter_panel.isHidden()
    window.history_maintenance_button.click()
    assert window.history_filter_panel.isHidden()
    assert not window.history_maintenance_panel.isHidden()

    assert window.add_server_panel.isHidden()
    window.add_server_button.click()
    assert not window.add_server_panel.isHidden()
    assert window.advanced_policy.isHidden()
    assert window.advanced_policy_button.text() == "More settings..."
    assert window.advanced_policy_button.objectName() == "DisclosureButton"
    assert window.advanced_policy_button.icon().isNull()
    assert window.advanced_policy_button.accessibleName()
    window.advanced_policy_button.click()
    assert not window.advanced_policy.isHidden()
    assert window.advanced_policy_button.text() == "Fewer settings..."
    window.advanced_policy_button.click()
    assert window.advanced_policy.isHidden()
    assert window.advanced_policy_button.text() == "More settings..."

    icon_buttons = [
        button for button in window.findChildren(QPushButton) if button.objectName() == "IconButton"
    ]
    assert icon_buttons
    assert all(button.text() == "" for button in icon_buttons)
    assert all(button.minimumWidth() >= 44 for button in icon_buttons)
    assert all(button.minimumHeight() >= 44 for button in icon_buttons)
    assert all(button.toolTip() and button.accessibleName() for button in icon_buttons)

    window.prepare_to_quit()
    window.close()


def test_checkbox_focus_is_scoped_without_a_widget_sized_green_box() -> None:
    assert "QPushButton:focus, QLineEdit:focus, QCheckBox:focus" not in APP_STYLESHEET
    assert "QCheckBox::indicator:focus" in APP_STYLESHEET
    assert "QCheckBox:focus { color:" in APP_STYLESHEET


def test_server_status_row_is_collapsed_and_expandable() -> None:
    QApplication.instance() or QApplication([])
    config = AppConfig()
    window = ManagementWindow(config, tray_available=False)
    row = window.status_card(config.servers[0].url)
    assert row is not None
    assert row.detail.isHidden()  # compact by default
    row.toggle()
    assert not row.detail.isHidden()  # click reveals details
    window.prepare_to_quit()
    window.close()


def test_server_alias_persists_through_servers_changed() -> None:
    QApplication.instance() or QApplication([])
    config = AppConfig()
    window = ManagementWindow(config, tray_available=False)
    url = config.servers[0].url

    captured: list[object] = []
    window.servers_changed.connect(captured.append)
    window._server_renamed(url, "Prod EU")

    assert window._aliases[url] == "Prod EU"
    assert window.status_card(url).title_label.toolTip() == "Prod EU"
    assert captured and captured[-1][0].name == "Prod EU"

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
            "requires_attention": True,
        }
    )
    toast = AlertToast(alert)
    toast.show_at(QPoint(0, 0))
    app.processEvents()

    assert toast.width() == AlertToast.WIDTH
    assert toast.expiry_rail.height() == 2
    assert toast.accessibleName() == "Critical alert: Database connection pressure"

    toast.close()


def test_history_filters_keyboard_detail_and_lifecycle_signals() -> None:
    app = QApplication.instance() or QApplication([])
    window = ManagementWindow(AppConfig(), tray_available=False)
    record = {
        "server_url": "https://alerts.example.com",
        "lifecycle": "unread",
        "note": "Existing response note",
        "sequence": 42,
        "received_at": "2026-07-15T12:01:00Z",
        "alert": Alert.from_payload(
            {
                "id": "incident-1",
                "title": "Database saturation",
                "message": "Open https://example.com/runbook and inspect the primary pool.",
                "severity": "critical",
                "channel": "infrastructure",
                "actions": [
                    {
                        "label": "Open runbook",
                        "url": "https://example.com/runbook",
                        "kind": "runbook",
                    }
                ],
            }
        ),
    }
    window.set_history_records([record])
    window.show()
    window.tabs.setCurrentIndex(window._history_tab_index)
    app.processEvents()

    assert window.history_search.accessibleName() == "Search alert history"
    assert not hasattr(window, "status_filter")
    assert set(window.history_filters()) == {"search", "severity", "server", "channel"}
    assert window.severity_filter.accessibleName()
    assert window.server_filter.accessibleName()
    assert window.channel_filter.accessibleName()

    window.history_search.setText("not present")
    QTest.qWait(HISTORY_FILTER_DEBOUNCE_MS + 20)
    assert window.empty_history_title.text() == "No alerts match these filters"
    window._clear_history_filters()
    row = window.findChild(AlertHistoryRow)
    assert row is not None
    QTest.keyClick(row, Qt.Key.Key_Return)
    assert window._detail_dialogs
    window._detail_dialogs[-1].close()

    dialog = AlertDetailDialog(record, window)
    lifecycle: list[tuple[object, ...]] = []
    actions: list[tuple[object, ...]] = []
    dialog.lifecycle_requested.connect(lambda *args: lifecycle.append(args))
    dialog.action_requested.connect(lambda *args: actions.append(args))
    assert not hasattr(dialog, "note_input")
    assert not hasattr(dialog, "acknowledge_button")
    assert not hasattr(dialog, "resolve_button")
    assert any(label.text() == "42" for label in dialog.findChildren(QLabel))
    dialog.snooze_button.click()
    assert lifecycle[-1][:3] == (
        "https://alerts.example.com",
        "incident-1",
        "snoozed",
    )
    assert lifecycle[-1][4] == ""
    assert not hasattr(dialog, "status_label")
    assert not hasattr(dialog, "status_explanation")
    assert "notifications are paused" in dialog.reminder_explanation.text().lower()
    assert not dialog.status_feedback.isHidden()
    action_button = next(
        button for button in dialog.findChildren(QPushButton) if button.text() == "Open runbook"
    )
    action_button.click()
    assert actions[-1][2]["url"] == "https://example.com/runbook"
    dialog.close()
    window.prepare_to_quit()
    window.close()


def test_alerts_without_reminders_have_no_classification_or_controls() -> None:
    QApplication.instance() or QApplication([])
    alert = Alert.from_payload(
        {
            "id": "info-only",
            "title": "Release completed",
            "message": "Version 3 is now available.",
            "severity": "success",
        }
    )
    record = {"server_url": "https://alerts.example.com", "alert": alert}

    row = AlertHistoryRow(record)
    assert not any(label.objectName() == "StatusBadge" for label in row.findChildren(QLabel))
    assert "informational" not in row.accessibleName().lower()
    assert "attention" not in row.accessibleName().lower()

    dialog = AlertDetailDialog(record)
    assert dialog.requires_attention is False
    assert not hasattr(dialog, "status_label")
    assert not hasattr(dialog, "status_explanation")
    assert not hasattr(dialog, "status_card")
    assert not hasattr(dialog, "reminder_card")
    assert not hasattr(dialog, "reminder_controls")
    assert not hasattr(dialog, "note_input")
    visible_text = " ".join(label.text() for label in dialog.findChildren(QLabel)).lower()
    assert "informational" not in visible_text
    assert "needs attention" not in visible_text

    toast = AlertToast(alert)
    assert not hasattr(toast, "status")
    assert toast.accessibleName() == "Success alert: Release completed"
    assert not hasattr(toast, "acknowledge_button")
    assert not hasattr(toast, "snooze_button")
    assert not hasattr(toast, "resolve_button")

    toast.close()
    dialog.close()
    row.close()


def test_reminder_controls_support_schedule_wake_and_undo_without_status_labels() -> None:
    QApplication.instance() or QApplication([])
    record = {
        "server_url": "https://alerts.example.com",
        "lifecycle": "unread",
        "alert": Alert.from_payload(
            {
                "id": "attention-flow",
                "title": "Capacity pressure",
                "requires_attention": True,
            }
        ),
    }
    dialog = AlertDetailDialog(record)
    changes: list[tuple[object, ...]] = []
    dialog.lifecycle_requested.connect(lambda *args: changes.append(args))

    assert not hasattr(dialog, "status_label")
    assert not hasattr(dialog, "status_explanation")
    assert dialog.reminder_explanation.isHidden()
    assert not hasattr(dialog, "acknowledge_button")
    assert not hasattr(dialog, "resolve_button")
    assert not hasattr(dialog, "note_input")
    dialog.snooze_button.click()
    assert changes[-1][2] == "snoozed"
    assert not dialog.reminder_explanation.isHidden()
    assert "scheduled for" in dialog.reminder_explanation.text().lower()
    assert not dialog.wake_button.isHidden()
    assert dialog.snooze_button.text() == "Change reminder"
    assert dialog.status_feedback_label.text() == "Reminder set"
    dialog.undo_button.click()
    assert changes[-1][2] == "unread"
    assert dialog.reminder_explanation.isHidden()
    assert dialog.status_feedback_label.text() == "Reminder cleared"
    dialog.snooze_button.click()
    dialog.wake_button.click()
    assert changes[-1][2] == "unread"
    assert dialog.reminder_explanation.isHidden()
    dialog.undo_button.click()
    assert changes[-1][2] == "snoozed"
    assert not dialog.reminder_explanation.isHidden()
    assert dialog.status_feedback_label.text() == "Previous reminder restored"

    dialog.close()

    legacy_dialog = AlertDetailDialog({**record, "lifecycle": "resolved"})
    assert not hasattr(legacy_dialog, "status_label")
    assert not hasattr(legacy_dialog, "resolve_button")
    legacy_dialog.close()


def test_policy_serialization_and_reliability_controls_emit() -> None:
    QApplication.instance() or QApplication([])
    window = ManagementWindow(AppConfig(), tray_available=False)
    policies: list[object] = []
    launch: list[bool] = []
    watchdog: list[int] = []
    window.policy_changed.connect(policies.append)
    window.launch_at_login_changed.connect(launch.append)
    window.watchdog_threshold_changed.connect(watchdog.append)

    warning = window.delivery_rows["warning"].mode_combo
    warning.setCurrentIndex(warning.findData("history_only"))
    window.quiet_enabled.setChecked(True)
    window.override_scope.setCurrentIndex(window.override_scope.findData("channel"))
    window.override_value.setCurrentText("security")
    window.override_mode.setCurrentIndex(window.override_mode.findData("muted"))
    window._add_policy_override()
    mapping = window.policy_mapping()

    assert mapping["severity_modes"]["warning"] == "history_only"
    assert mapping["channel_modes"]["security"] == "muted"
    assert mapping["quiet_enabled"] is True
    assert policies

    window.launch_toggle.setChecked(not window.launch_toggle.isChecked())
    window.watchdog_spin.setValue(window.watchdog_spin.value() + 1)
    assert launch and watchdog
    window.prepare_to_quit()
    window.close()


def test_auth_token_is_password_only_cleared_and_never_reloaded() -> None:
    QApplication.instance() or QApplication([])
    config = AppConfig()
    window = ManagementWindow(config, tray_available=False)
    url = config.servers[0].url
    panel = window._panels[url]
    captured: list[tuple[str, str]] = []
    window.auth_save_requested.connect(lambda server, token: captured.append((server, token)))

    assert panel.token_input.echoMode() == QLineEdit.EchoMode.Password
    assert panel.token_input.text() == ""
    panel.token_input.setText("super-secret")
    panel.save_token_button.click()
    assert captured == [(url, "super-secret")]
    assert panel.token_input.text() == ""
    window.set_server_auth_status(url, True, secure_available=True)
    assert panel.token_input.text() == ""
    assert "never displayed" in panel.auth_status.text().lower()
    window.prepare_to_quit()
    window.close()


def test_recovery_banner_and_history_controls_have_no_attention_count(monkeypatch) -> None:
    QApplication.instance() or QApplication([])
    window = ManagementWindow(AppConfig(), tray_available=False)
    exported: list[object] = []
    cleared: list[bool] = []
    window.history_export_requested.connect(exported.append)
    window.clear_history_requested.connect(lambda: cleared.append(True))

    window.show_recovery_gap(
        "https://alerts.example.com",
        "10:00",
        "10:05",
    )
    assert not window.recovery_banner.isHidden()
    assert "10:00" in window.recovery_detail.text()
    assert window.recovery_detail.accessibleName()
    assert window.tabs.tabText(window._history_tab_index) == "HISTORY"
    assert not hasattr(window, "history_unread_label")
    assert window.overview_inbox_title.text() == "Alert inbox"
    assert window.overview_inbox_summary.text() == "No alerts retained"
    assert window.overview_history_button.property("iconName") == "chevron_right"
    assert window.overview_history_button.accessibleName()

    window.history_export_requested.emit(window.history_filters())
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    window._confirm_clear_history()
    assert exported and cleared
    window.clear_recovery_banner()
    assert window.recovery_banner.isHidden()
    window.prepare_to_quit()
    window.close()


def test_toast_and_manager_forward_actionable_identity(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("SIGNALDESK_REDUCE_MOTION", "1")
    alert = Alert.from_payload(
        {
            "id": "actionable-1",
            "title": "Service degraded",
            "message": "Inspect the service.",
            "severity": "warning",
            "requires_attention": True,
            "actions": [
                {
                    "label": "Open dashboard",
                    "url": "https://example.com/dashboard",
                }
            ],
        }
    )
    manager = NotificationManager()
    activated: list[tuple[object, ...]] = []
    lifecycle: list[tuple[object, ...]] = []
    actions: list[tuple[object, ...]] = []
    manager.activated.connect(lambda *args: activated.append(args))
    manager.lifecycle_requested.connect(lambda *args: lifecycle.append(args))
    manager.action_requested.connect(lambda *args: actions.append(args))
    manager.show_alert(alert, "https://alerts.example.com")
    app.processEvents()
    toast = manager._toasts[0]
    original_height = toast.height()

    assert toast.snooze_button.text() == ""
    assert not toast.snooze_button.icon().isNull()
    assert toast.snooze_button.toolTip() == "Remind me in 15 minutes"
    assert not hasattr(toast, "acknowledge_button")
    assert not hasattr(toast, "resolve_button")
    assert toast.lifecycle_row.indexOf(toast.snooze_button) == toast.lifecycle_row.count() - 1

    QToolTip.hideText()
    app.processEvents()
    QApplication.sendEvent(toast.snooze_button, QEvent(QEvent.Type.Enter))
    app.processEvents()
    assert QToolTip.text() == ""
    assert toast._hint_timer.isActive()
    QTest.qWait(HOVER_HINT_DELAY_MS + 20)
    assert QToolTip.text() == "Remind me in 15 minutes"
    assert not hasattr(toast, "action_feedback")
    assert toast.height() == original_height

    action_button = next(
        button for button in toast.findChildren(QPushButton) if button.text() == "Open dashboard"
    )
    action_button.click()
    assert actions[-1][:2] == ("https://alerts.example.com", "actionable-1")
    assert activated == []
    toast.snooze_button.click()
    app.processEvents()
    assert lifecycle[-1][:3] == (
        "https://alerts.example.com",
        "actionable-1",
        "snoozed",
    )
    assert QToolTip.text() == "Reminder set for 15 minutes."
    assert not toast._dismissing
    assert toast.height() == original_height
    manager.dismiss_all()


def test_clicking_toast_opens_its_alert_identity(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("SIGNALDESK_REDUCE_MOTION", "1")
    alert = Alert.from_payload(
        {
            "id": "open-detail-1",
            "title": "Open the matching detail",
            "message": "Click this notification body.",
            "severity": "info",
        }
    )
    manager = NotificationManager()
    activated: list[tuple[str, str]] = []
    manager.activated.connect(lambda server, alert_id: activated.append((server, alert_id)))
    manager.show_alert(alert, "https://alerts.example.com")
    app.processEvents()
    toast = manager._toasts[0]

    assert toast.open_surface.toolTip() == "Open alert details"
    assert toast.open_surface.focusPolicy() == Qt.FocusPolicy.StrongFocus
    QTest.mouseClick(toast.open_surface, Qt.MouseButton.LeftButton)
    app.processEvents()

    assert activated == [("https://alerts.example.com", "open-detail-1")]
    assert manager._toasts == []


def test_toast_quick_reminder_snoozes_for_fifteen_minutes(monkeypatch) -> None:
    QApplication.instance() or QApplication([])
    monkeypatch.setenv("SIGNALDESK_REDUCE_MOTION", "1")
    alert = Alert.from_payload(
        {
            "id": "quick-reminder-1",
            "title": "Check capacity again",
            "requires_attention": True,
        }
    )
    toast = AlertToast(alert, "https://alerts.example.com")
    changes: list[tuple[object, ...]] = []
    toast.lifecycle_requested.connect(lambda *args: changes.append(args))
    before = datetime.now(UTC)

    toast.snooze_button.click()

    assert changes[-1][:3] == (
        "https://alerts.example.com",
        "quick-reminder-1",
        "snoozed",
    )
    remind_at = datetime.fromisoformat(str(changes[-1][3]).replace("Z", "+00:00"))
    assert (
        timedelta(minutes=14, seconds=59) <= remind_at - before <= timedelta(minutes=15, seconds=1)
    )
    assert QToolTip.text() == "Reminder set for 15 minutes."
    assert not toast._dismissing
    assert not hasattr(toast, "acknowledge_button")
    assert not hasattr(toast, "resolve_button")
    toast.dismiss()


def test_notification_burst_bounds_visible_stack_and_counts_overflow(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("SIGNALDESK_REDUCE_MOTION", "1")
    manager = NotificationManager()
    opened: list[bool] = []
    manager.overflow_activated.connect(lambda: opened.append(True))
    total = MAX_VISIBLE_TOASTS + 7

    for index in range(total):
        manager.show_alert(
            Alert.from_payload(
                {
                    "id": f"burst-{index}",
                    "title": f"Burst alert {index}",
                    "message": "Representative burst notification.",
                    "requires_attention": False,
                }
            ),
            "https://alerts.example.com",
        )
    app.processEvents()

    assert len(manager._toasts) == MAX_VISIBLE_TOASTS
    assert [toast.alert.id for toast in manager._toasts] == [
        f"burst-{index}" for index in range(MAX_VISIBLE_TOASTS - 1, -1, -1)
    ]
    assert manager._overflow_count == total - MAX_VISIBLE_TOASTS
    assert manager._overflow_indicator is not None
    assert manager._overflow_indicator.text() == "+7 more alerts"
    assert manager._overflow_indicator.isVisible()
    assert manager._overflow_timer.interval() == OVERFLOW_DISPLAY_MS
    assert manager._overflow_timer.isActive()
    area = manager._screen_area()
    assert area is not None
    assert area.contains(manager._overflow_indicator.geometry())
    assert all(area.contains(toast.geometry()) for toast in manager._toasts)

    manager._overflow_indicator.click()
    assert opened == [True]
    assert manager._overflow_count == 0
    assert manager._overflow_indicator.isHidden()

    manager.show_alert(
        Alert.from_payload(
            {
                "id": "burst-after-open",
                "title": "Another burst alert",
                "requires_attention": False,
            }
        ),
        "https://alerts.example.com",
    )
    assert manager._overflow_count == 1
    manager._overflow_timer.timeout.emit()
    assert manager._overflow_count == 0
    assert manager._overflow_indicator.isHidden()
    manager.dismiss_all()


def test_grouped_alert_counter_is_visible_without_an_active_toast(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("SIGNALDESK_REDUCE_MOTION", "1")
    manager = NotificationManager()

    manager.aggregate_alerts(2)
    app.processEvents()

    assert manager._toasts == []
    assert manager._overflow_indicator is not None
    assert manager._overflow_indicator.isVisible()
    assert manager._overflow_indicator.text() == "+2 more alerts"
    manager.dismiss_all()


def test_large_history_uses_bounded_pages_instead_of_eager_widgets() -> None:
    app = QApplication.instance() or QApplication([])
    window = ManagementWindow(AppConfig(), tray_available=False)
    records = [
        {
            "server_url": "https://alerts.example.com",
            "alert": Alert.from_payload(
                {
                    "id": f"paged-{index}",
                    "title": f"Paged alert {index}",
                    "message": "Representative retained alert.",
                }
            ),
        }
        for index in range(HISTORY_PAGE_SIZE * 2 + 25)
    ]

    window.set_history_records(records)
    assert window._history_rows == []
    window.tabs.setCurrentIndex(window._history_tab_index)
    app.processEvents()

    assert len(window._history_rows) == HISTORY_PAGE_SIZE
    assert window.history_page_label.text() == (f"1–{HISTORY_PAGE_SIZE} of {len(records)}")
    assert window._history_rows[0].record is records[0]

    window.history_next_button.click()
    assert len(window._history_rows) == HISTORY_PAGE_SIZE
    assert window._history_rows[0].record is records[HISTORY_PAGE_SIZE]
    assert window.history_previous_button.isEnabled()

    window.prepare_to_quit()
    window.close()
