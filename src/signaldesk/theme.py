"""SignalDesk semantic color tokens and Qt stylesheet."""

from __future__ import annotations

from PySide6.QtGui import QColor

COLORS = {
    "background": "#F0F3EF",
    "surface": "#FBFCFA",
    "surface_raised": "#F4F7F3",
    "surface_hover": "#EAF1EC",
    "border": "#D6DED8",
    "border_strong": "#AEBAB1",
    "text": "#111A15",
    "text_secondary": "#3E4E45",
    "muted": "#647269",
    "chrome": "#17231C",
    "chrome_text": "#F6FAF7",
    "chrome_muted": "#B5C2B9",
    "primary": "#007A4B",
    "primary_hover": "#008653",
    "primary_pressed": "#00673F",
    "on_primary": "#FFFFFF",
    "shadow": "#101812",
    "info": "#0968A6",
    "success": "#007A4B",
    "warning": "#955800",
    "critical": "#C53243",
    "success_bright": "#73DDA6",
    "warning_bright": "#F2BE6B",
    "critical_bright": "#FF8A96",
    "warning_surface": "#FFF4DD",
    "critical_surface": "#FFF0F2",
    "focus_ring": "#005E3A",
}

SEVERITY_COLORS = {
    "info": COLORS["info"],
    "success": COLORS["success"],
    "warning": COLORS["warning"],
    "critical": COLORS["critical"],
}


def color(name: str, alpha: int | None = None) -> QColor:
    value = QColor(COLORS[name])
    if alpha is not None:
        value.setAlpha(alpha)
    return value


APP_STYLESHEET = f"""
* {{
    font-family: "Segoe UI Variable", "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
    color: {COLORS["text"]};
}}

QMainWindow#RootWindow, QWidget#RootContent {{
    background-color: {COLORS["background"]};
}}

QDialog#AlertDetailDialog {{ background-color: {COLORS["background"]}; }}

QFrame#CommandHeader {{
    background-color: {COLORS["chrome"]};
    border: none;
    border-bottom: 4px solid {COLORS["primary"]};
}}

QFrame#FooterRail {{
    background-color: {COLORS["surface_raised"]};
    border: none;
    border-top: 1px solid {COLORS["border"]};
}}

QFrame#RecoveryBanner {{
    background-color: {COLORS["warning_surface"]};
    border: none;
    border-bottom: 1px solid {COLORS["warning"]};
}}

QFrame#RecoveryBanner[severity="critical"] {{
    background-color: {COLORS["critical_surface"]};
    border-bottom-color: {COLORS["critical"]};
}}

QLabel#BrandTitle {{
    color: {COLORS["chrome_text"]};
    font-size: 21px;
    font-weight: 750;
}}

QLabel#BrandSubtitle {{
    color: {COLORS["chrome_muted"]};
    font-family: "Cascadia Mono", "Cascadia Code", "Consolas", monospace;
    font-size: 10px;
    font-weight: 600;
}}

QLabel[role="muted"] {{ color: {COLORS["muted"]}; }}

QLabel[role="mono"], QLabel[role="eyebrow"], QLabel#CounterLabel,
QLabel#HistoryTime, QLabel#ToastTime {{
    font-family: "Cascadia Mono", "Cascadia Code", "Consolas", monospace;
}}

QLabel[role="eyebrow"] {{
    color: {COLORS["muted"]};
    font-size: 10px;
    font-weight: 700;
}}

QLabel[role="headerEyebrow"] {{
    color: {COLORS["chrome_muted"]};
    font-family: "Cascadia Mono", "Cascadia Code", "Consolas", monospace;
    font-size: 9px;
    font-weight: 700;
}}

QLabel#SectionTitle {{
    font-size: 15px;
    font-weight: 700;
}}

QLabel#DialogTitle {{
    font-size: 20px;
    font-weight: 750;
}}

QLabel#ConnectionTitle {{
    font-size: 20px;
    font-weight: 720;
}}

QLabel#MetricValue {{
    font-family: "Cascadia Mono", "Cascadia Code", "Consolas", monospace;
    font-size: 15px;
    font-weight: 650;
}}

QLabel#CounterLabel, QLabel#HistoryTime, QLabel#ToastTime {{
    color: {COLORS["muted"]};
    font-size: 10px;
    font-weight: 650;
}}

QLabel#EmptyCode {{
    color: {COLORS["border_strong"]};
    font-family: "Cascadia Mono", "Cascadia Code", "Consolas", monospace;
    font-size: 28px;
    font-weight: 700;
}}

QLabel#EndpointError {{ color: {COLORS["critical"]}; }}

QFrame#ConnectionCard, QFrame#EndpointCard, QFrame#Ledger, QFrame#Card {{
    background-color: {COLORS["surface"]};
    border: 1px solid {COLORS["border_strong"]};
    border-radius: 0px;
}}

QLabel#InboxIconTile {{
    background-color: {COLORS["surface_hover"]};
    border: 1px solid {COLORS["border"]};
}}

QFrame#ChannelCatalogState {{
    background-color: {COLORS["surface_raised"]};
    border: none;
}}

QLabel#ChannelCatalogIcon {{
    background-color: {COLORS["surface"]};
    border: 1px solid {COLORS["border"]};
}}

QFrame#FilterBar, QFrame#HistoryPager {{
    background-color: {COLORS["surface_raised"]};
    border: 1px solid {COLORS["border"]};
}}

QFrame#InlineEditor {{
    background-color: {COLORS["surface_raised"]};
    border: 1px solid {COLORS["border"]};
}}

QGroupBox {{
    background-color: {COLORS["surface"]};
    border: 1px solid {COLORS["border_strong"]};
    margin-top: 10px;
    padding-top: 8px;
    font-weight: 700;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 5px;
    color: {COLORS["text_secondary"]};
}}

QFrame#ConnectionAccent, QFrame#PanelAccent {{
    min-height: 4px;
    max-height: 4px;
    border: none;
    background-color: {COLORS["primary"]};
}}

QFrame#ConnectionAccent[connectionState="connecting"] {{ background: {COLORS["warning"]}; }}
QFrame#ConnectionAccent[connectionState="disconnected"],
QFrame#ConnectionAccent[connectionState="stopped"] {{ background: {COLORS["critical"]}; }}

QFrame#MetricStrip {{
    background-color: {COLORS["surface_raised"]};
    border: none;
}}

QFrame#MetricCell {{
    background: transparent;
    border: none;
    border-right: 1px solid {COLORS["border"]};
}}

QFrame#MetricCell[last="true"] {{ border-right: none; }}

QFrame#HorizontalRule {{
    min-height: 1px;
    max-height: 1px;
    background-color: {COLORS["border"]};
    border: none;
}}

QFrame#ChannelRow, QFrame#AlertHistoryRow {{
    background-color: {COLORS["surface"]};
    border: none;
    border-bottom: 1px solid {COLORS["border"]};
    border-radius: 0px;
}}

QFrame#ChannelRow:hover, QFrame#AlertHistoryRow:hover {{
    background-color: {COLORS["surface_hover"]};
}}

QFrame#AlertHistoryRow:focus {{
    border: 2px solid {COLORS["focus_ring"]};
}}

QFrame#ChannelRow[selected="true"] {{ background-color: {COLORS["surface_raised"]}; }}

QFrame#ChannelMarker {{
    background-color: {COLORS["border_strong"]};
    border: none;
}}

QFrame#ChannelMarker[selected="true"] {{ background-color: {COLORS["primary"]}; }}

QFrame#HistoryAccent[severity="info"] {{ background: {COLORS["info"]}; border: none; }}
QFrame#HistoryAccent[severity="success"] {{ background: {COLORS["success"]}; border: none; }}
QFrame#HistoryAccent[severity="warning"] {{ background: {COLORS["warning"]}; border: none; }}
QFrame#HistoryAccent[severity="critical"] {{ background: {COLORS["critical"]}; border: none; }}

QLabel#SeverityCode {{
    font-family: "Cascadia Mono", "Cascadia Code", "Consolas", monospace;
    font-size: 9px;
    font-weight: 750;
}}

QLabel#SeverityCode[severity="info"] {{ color: {COLORS["info"]}; }}
QLabel#SeverityCode[severity="success"] {{ color: {COLORS["success"]}; }}
QLabel#SeverityCode[severity="warning"] {{ color: {COLORS["warning"]}; }}
QLabel#SeverityCode[severity="critical"] {{ color: {COLORS["critical"]}; }}

QPushButton {{
    min-height: 40px;
    padding: 0 16px;
    border-radius: 0px;
    border: 1px solid transparent;
    font-weight: 650;
}}

QPushButton#PrimaryButton {{
    background-color: {COLORS["primary"]};
    color: {COLORS["on_primary"]};
    border-color: {COLORS["primary"]};
}}

QPushButton#PrimaryButton:hover {{
    background-color: {COLORS["primary_hover"]};
    border-color: {COLORS["primary_hover"]};
}}

QPushButton#PrimaryButton:pressed {{
    background-color: {COLORS["primary_pressed"]};
    border-color: {COLORS["primary_pressed"]};
}}

QPushButton#SecondaryButton {{
    background-color: {COLORS["surface"]};
    border-color: {COLORS["border_strong"]};
}}

QPushButton#SecondaryButton:hover {{
    background-color: {COLORS["surface_hover"]};
    border-color: {COLORS["primary"]};
}}

QPushButton#GhostButton {{
    background-color: transparent;
    color: {COLORS["text_secondary"]};
    border-color: {COLORS["border"]};
}}

QPushButton#GhostButton:hover {{
    background-color: {COLORS["surface"]};
    color: {COLORS["text"]};
    border-color: {COLORS["border_strong"]};
}}

QPushButton#DisclosureButton {{
    min-height: 44px;
    padding: 0 4px;
    background-color: transparent;
    color: {COLORS["primary"]};
    border: 1px solid transparent;
    text-align: left;
}}

QPushButton#DisclosureButton:hover {{
    color: {COLORS["primary_hover"]};
    background-color: transparent;
    border-bottom-color: {COLORS["primary"]};
}}

QPushButton#DisclosureButton:focus {{
    border: 2px solid {COLORS["focus_ring"]};
}}

QPushButton#DangerButton {{
    background-color: {COLORS["surface"]};
    color: {COLORS["critical"]};
    border-color: {COLORS["critical"]};
}}

QFrame#StatusFeedback {{
    background-color: {COLORS["surface_hover"]};
    border: 1px solid {COLORS["primary"]};
}}

QPushButton#DangerButton:hover {{
    background-color: {COLORS["critical_surface"]};
}}

QPushButton#ToastActionButton {{
    min-height: 38px;
    padding: 0 12px;
    background-color: {COLORS["surface"]};
    color: {COLORS["text_secondary"]};
    border-color: {COLORS["border_strong"]};
}}

QPushButton#ToastActionButton:hover {{
    color: {COLORS["primary"]};
    border-color: {COLORS["primary"]};
    background-color: {COLORS["surface_hover"]};
}}

QPushButton#IconButton {{
    min-width: 44px;
    max-width: 44px;
    min-height: 44px;
    max-height: 44px;
    padding: 0;
    border-radius: 0px;
    background-color: transparent;
    border-color: transparent;
}}

QPushButton#IconButton:hover {{
    background-color: {COLORS["surface_hover"]};
    border-color: {COLORS["border"]};
}}

QPushButton#IconButton:checked, QPushButton#IconButton[configured="true"] {{
    background-color: {COLORS["surface_hover"]};
    border-color: {COLORS["primary"]};
}}

QPushButton#IconButton[primary="true"] {{
    background-color: {COLORS["primary"]};
    border-color: {COLORS["primary"]};
}}

QPushButton#IconButton[primary="true"]:hover {{
    background-color: {COLORS["primary_hover"]};
    border-color: {COLORS["primary_hover"]};
}}

QPushButton#IconButton[danger="true"]:hover {{
    background-color: {COLORS["critical_surface"]};
    border-color: {COLORS["critical"]};
}}

QFrame#CommandHeader QPushButton#IconButton[chrome="true"]:hover {{
    background-color: {COLORS["primary_pressed"]};
    border-color: {COLORS["primary"]};
}}

QFrame#CommandHeader QPushButton#IconButton[chrome="true"]:focus {{
    border: 2px solid {COLORS["chrome_text"]};
}}

QPushButton:focus, QLineEdit:focus, QComboBox:focus,
QSpinBox:focus, QTimeEdit:focus, QPlainTextEdit:focus {{
    border: 2px solid {COLORS["focus_ring"]};
}}

QPushButton:disabled {{
    color: {COLORS["muted"]};
    background-color: {COLORS["surface_hover"]};
    border-color: {COLORS["border"]};
}}

QLineEdit {{
    min-height: 42px;
    padding: 0 12px;
    background-color: {COLORS["surface_raised"]};
    border: 1px solid {COLORS["border_strong"]};
    border-radius: 0px;
    selection-background-color: {COLORS["primary"]};
    selection-color: {COLORS["on_primary"]};
}}

QLineEdit:hover {{ border-color: {COLORS["muted"]}; }}
QLineEdit[invalid="true"] {{ border: 2px solid {COLORS["critical"]}; }}

QComboBox, QSpinBox, QTimeEdit, QPlainTextEdit {{
    min-height: 42px;
    padding: 0 10px;
    background-color: {COLORS["surface_raised"]};
    border: 1px solid {COLORS["border_strong"]};
    border-radius: 0px;
    selection-background-color: {COLORS["primary"]};
    selection-color: {COLORS["on_primary"]};
}}

QComboBox:hover, QSpinBox:hover, QTimeEdit:hover {{ border-color: {COLORS["muted"]}; }}
QComboBox::drop-down {{
    width: 28px;
    border: none;
    border-left: 1px solid {COLORS["border"]};
}}
QComboBox QAbstractItemView {{
    background-color: {COLORS["surface"]};
    border: 1px solid {COLORS["border_strong"]};
    selection-background-color: {COLORS["surface_hover"]};
    selection-color: {COLORS["text"]};
}}

QCheckBox {{
    spacing: 9px;
    min-height: 44px;
    color: {COLORS["text_secondary"]};
    font-family: "Cascadia Mono", "Cascadia Code", "Consolas", monospace;
    font-size: 10px;
    font-weight: 700;
}}

QCheckBox::indicator {{
    width: 20px;
    height: 20px;
    border-radius: 0px;
    border: 2px solid {COLORS["border_strong"]};
    background-color: {COLORS["surface"]};
}}

QCheckBox::indicator:hover {{ border-color: {COLORS["primary"]}; }}
QCheckBox:focus {{ color: {COLORS["primary"]}; }}
QCheckBox::indicator:focus {{
    border: 2px solid {COLORS["focus_ring"]};
}}
QCheckBox::indicator:checked {{
    background-color: {COLORS["primary"]};
    border-color: {COLORS["primary"]};
    image: url(none);
}}

QTabWidget::pane {{
    border: none;
    background: transparent;
    top: -1px;
}}

QTabBar {{
    qproperty-drawBase: 0;
    background: transparent;
    border: none;
    border-bottom: 1px solid {COLORS["border_strong"]};
}}

QTabBar::tab {{
    min-width: 72px;
    min-height: 42px;
    padding: 0 8px;
    margin: 0;
    border: none;
    border-bottom: 3px solid transparent;
    border-radius: 0px;
    background-color: transparent;
    color: {COLORS["muted"]};
    font-family: "Cascadia Mono", "Cascadia Code", "Consolas", monospace;
    font-size: 10px;
    font-weight: 700;
}}

QTabBar::tab:hover {{
    background-color: {COLORS["surface_hover"]};
    color: {COLORS["text"]};
    border-bottom: 3px solid {COLORS["border_strong"]};
}}

QTabBar::tab:selected {{
    background-color: transparent;
    color: {COLORS["text"]};
    border-bottom: 3px solid {COLORS["primary"]};
}}

QTabBar::tab:selected:hover {{
    background-color: {COLORS["surface_hover"]};
    border-bottom: 3px solid {COLORS["primary"]};
}}

QScrollArea, QScrollArea > QWidget > QWidget, QAbstractScrollArea::viewport {{
    background: transparent;
    border: none;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}

QScrollBar::handle:vertical {{
    background: {COLORS["border_strong"]};
    min-height: 30px;
    border-radius: 0px;
}}

QScrollBar::handle:vertical:hover {{ background: {COLORS["muted"]}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QLabel#HeaderStatus {{
    font-family: "Cascadia Mono", "Cascadia Code", "Consolas", monospace;
    font-size: 11px;
    font-weight: 750;
}}

QLabel#HeaderStatus[connectionState="connected"] {{ color: {COLORS["success_bright"]}; }}
QLabel#HeaderStatus[connectionState="connecting"] {{ color: {COLORS["warning_bright"]}; }}
QLabel#HeaderStatus[connectionState="disconnected"],
QLabel#HeaderStatus[connectionState="stopped"] {{ color: {COLORS["critical_bright"]}; }}

QLabel#RowStatus {{
    font-family: "Cascadia Mono", "Cascadia Code", "Consolas", monospace;
    font-size: 10px;
    font-weight: 700;
}}
QLabel#RowStatus[connectionState="connected"] {{ color: {COLORS["success"]}; }}
QLabel#RowStatus[connectionState="connecting"] {{ color: {COLORS["warning"]}; }}
QLabel#RowStatus[connectionState="disconnected"],
QLabel#RowStatus[connectionState="stopped"] {{ color: {COLORS["critical"]}; }}

QFrame#AlertToast {{ background: transparent; border: none; }}
QFrame#ToastOpenSurface {{
    background-color: transparent;
    border: 1px solid transparent;
}}
QFrame#ToastOpenSurface:hover {{ background-color: {COLORS["surface_hover"]}; }}
QFrame#ToastOpenSurface:focus {{ border-color: {COLORS["primary"]}; }}
QFrame#ToastOpenSurface[pressed="true"] {{
    background-color: {COLORS["border"]};
    border-color: {COLORS["primary"]};
}}
QFrame#ToastMetadata {{
    background-color: {COLORS["surface_raised"]};
    border: none;
    border-bottom: 1px solid {COLORS["border"]};
}}

QFrame#ToastAccent[severity="info"] {{ background: {COLORS["info"]}; border: none; }}
QFrame#ToastAccent[severity="success"] {{ background: {COLORS["success"]}; border: none; }}
QFrame#ToastAccent[severity="warning"] {{ background: {COLORS["warning"]}; border: none; }}
QFrame#ToastAccent[severity="critical"] {{ background: {COLORS["critical"]}; border: none; }}

QMenu {{
    background-color: {COLORS["surface"]};
    border: 1px solid {COLORS["border_strong"]};
    border-radius: 0px;
    padding: 6px;
}}

QMenu::item {{
    min-height: 32px;
    padding: 4px 24px 4px 12px;
    border-radius: 0px;
}}

QMenu::item:selected {{ background-color: {COLORS["surface_hover"]}; }}
QMenu::item:disabled {{ color: {COLORS["muted"]}; }}
QMenu::separator {{ height: 1px; background: {COLORS["border"]}; margin: 5px 8px; }}

QToolTip {{
    color: {COLORS["text_secondary"]};
    background-color: {COLORS["surface_raised"]};
    border: 1px solid {COLORS["border"]};
    padding: 4px 7px;
    font-size: 11px;
}}

QPushButton#NotificationOverflowIndicator {{
    color: {COLORS["text_secondary"]};
    background-color: {COLORS["surface_raised"]};
    border: 1px solid {COLORS["border"]};
    padding: 0px 10px;
    font-size: 11px;
    font-weight: 600;
}}
QPushButton#NotificationOverflowIndicator:hover {{
    color: {COLORS["text"]};
    border-color: {COLORS["border_strong"]};
    background-color: {COLORS["surface_hover"]};
}}
"""
