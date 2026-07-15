"""SignalDesk semantic color tokens and Qt stylesheet."""

from __future__ import annotations

from PySide6.QtGui import QColor

COLORS = {
    "background": "#F4F6F3",
    "surface": "#FFFFFF",
    "surface_raised": "#F7F9F5",
    "surface_hover": "#EEF3EA",
    "border": "#E3E8E0",
    "border_strong": "#CBD4C6",
    "text": "#14231B",
    "text_secondary": "#4B5A52",
    "muted": "#7A897F",
    "primary": "#00915A",
    "primary_hover": "#0AA268",
    "primary_pressed": "#007A4B",
    "on_primary": "#FFFFFF",
    "shadow": "#14231B",
    "info": "#0B74C4",
    "success": "#00915A",
    "warning": "#C77700",
    "critical": "#D9414E",
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


def tint(name: str, alpha: int) -> str:
    """Return a translucent ``rgba(...)`` string for the named token."""
    c = QColor(COLORS[name])
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


APP_STYLESHEET = f"""
* {{
    font-family: "Segoe UI Variable", "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
    color: {COLORS["text"]};
}}

QMainWindow#RootWindow, QWidget#RootContent {{
    background-color: {COLORS["background"]};
}}

QLabel#BrandTitle {{
    font-size: 20px;
    font-weight: 750;
}}

QLabel#BrandSubtitle, QLabel[role="muted"] {{
    color: {COLORS["muted"]};
}}

QLabel#SectionTitle {{
    font-size: 15px;
    font-weight: 650;
}}

QLabel#ConnectionTitle {{
    font-size: 21px;
    font-weight: 700;
}}

QLabel#MetricValue {{
    font-size: 15px;
    font-weight: 650;
}}

QFrame#Card, QFrame#ConnectionCard, QFrame#EndpointCard, QFrame#ChannelRow,
QFrame#AlertHistoryRow {{
    background-color: {COLORS["surface"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 14px;
}}

QFrame#ChannelRow:hover, QFrame#AlertHistoryRow:hover {{
    background-color: {COLORS["surface_raised"]};
    border-color: {COLORS["border_strong"]};
}}

QFrame#VerticalDivider {{
    background-color: {COLORS["border"]};
    border: none;
}}

QPushButton {{
    min-height: 40px;
    padding: 0 16px;
    border-radius: 9px;
    border: 1px solid transparent;
    font-weight: 600;
}}

QPushButton#PrimaryButton {{
    background-color: {COLORS["primary"]};
    color: {COLORS["on_primary"]};
}}

QPushButton#PrimaryButton:hover {{
    background-color: {COLORS["primary_hover"]};
}}

QPushButton#PrimaryButton:pressed {{
    background-color: {COLORS["primary_pressed"]};
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
    background-color: {COLORS["surface_raised"]};
    color: {COLORS["text"]};
}}

QPushButton#IconButton {{
    min-width: 32px;
    max-width: 32px;
    min-height: 32px;
    max-height: 32px;
    padding: 0;
    border-radius: 8px;
    background-color: transparent;
}}

QPushButton#IconButton:hover {{
    background-color: {COLORS["surface_hover"]};
}}

QPushButton:focus, QLineEdit:focus, QCheckBox:focus {{
    border: 2px solid {COLORS["primary"]};
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
    border-radius: 9px;
    selection-background-color: {COLORS["primary"]};
    selection-color: {COLORS["on_primary"]};
}}

QLineEdit:hover {{
    border-color: {COLORS["muted"]};
}}

QLineEdit[invalid="true"] {{
    border: 2px solid {COLORS["critical"]};
}}

QCheckBox {{
    spacing: 10px;
    min-height: 44px;
}}

QCheckBox::indicator {{
    width: 22px;
    height: 22px;
    border-radius: 7px;
    border: 2px solid {COLORS["border_strong"]};
    background-color: {COLORS["surface"]};
}}

QCheckBox::indicator:hover {{
    border-color: {COLORS["primary"]};
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
}}

QTabBar::tab {{
    min-width: 120px;
    min-height: 38px;
    padding: 0 18px;
    margin-right: 6px;
    border-radius: 9px;
    background-color: transparent;
    color: {COLORS["muted"]};
    font-weight: 600;
}}

QTabBar::tab:hover {{
    background-color: {COLORS["surface"]};
    color: {COLORS["text_secondary"]};
}}

QTabBar::tab:selected {{
    background-color: {COLORS["surface"]};
    color: {COLORS["primary"]};
    border: 1px solid {COLORS["border"]};
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
    border-radius: 4px;
}}

QScrollBar::handle:vertical:hover {{
    background: {COLORS["muted"]};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QLabel[connectionState="connected"] {{ color: {COLORS["success"]}; }}
QLabel[connectionState="connecting"] {{ color: {COLORS["warning"]}; }}
QLabel[connectionState="disconnected"], QLabel[connectionState="stopped"] {{
    color: {COLORS["critical"]};
}}

QLabel#StatusPill {{
    padding: 5px 10px;
    border-radius: 9px;
    font-size: 11px;
    font-weight: 700;
}}

QLabel#StatusPill[connectionState="connected"] {{
    color: {COLORS["success"]};
    background-color: {tint("success", 28)};
    border: 1px solid {tint("success", 90)};
}}

QLabel#StatusPill[connectionState="connecting"] {{
    color: {COLORS["warning"]};
    background-color: {tint("warning", 28)};
    border: 1px solid {tint("warning", 90)};
}}

QLabel#StatusPill[connectionState="disconnected"], QLabel#StatusPill[connectionState="stopped"] {{
    color: {COLORS["critical"]};
    background-color: {tint("critical", 28)};
    border: 1px solid {tint("critical", 90)};
}}

QLabel#SeverityBadge {{
    padding: 3px 7px;
    border-radius: 6px;
    font-size: 10px;
    font-weight: 700;
}}

QLabel#SeverityBadge[severity="info"] {{ color: {COLORS["info"]}; background: {tint("info", 30)}; }}
QLabel#SeverityBadge[severity="success"] {{ color: {COLORS["success"]}; background: {tint("success", 30)}; }}
QLabel#SeverityBadge[severity="warning"] {{ color: {COLORS["warning"]}; background: {tint("warning", 30)}; }}
QLabel#SeverityBadge[severity="critical"] {{ color: {COLORS["critical"]}; background: {tint("critical", 30)}; }}

QFrame#AlertToast {{
    background-color: {COLORS["surface"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 14px;
}}

QFrame#ToastAccent[severity="info"] {{ background: {COLORS["info"]}; border-radius: 2px; }}
QFrame#ToastAccent[severity="success"] {{ background: {COLORS["success"]}; border-radius: 2px; }}
QFrame#ToastAccent[severity="warning"] {{ background: {COLORS["warning"]}; border-radius: 2px; }}
QFrame#ToastAccent[severity="critical"] {{ background: {COLORS["critical"]}; border-radius: 2px; }}

QMenu {{
    background-color: {COLORS["surface"]};
    border: 1px solid {COLORS["border_strong"]};
    border-radius: 9px;
    padding: 6px;
}}

QMenu::item {{
    min-height: 32px;
    padding: 4px 24px 4px 12px;
    border-radius: 6px;
}}

QMenu::item:selected {{ background-color: {COLORS["surface_hover"]}; }}
QMenu::item:disabled {{ color: {COLORS["muted"]}; }}
QMenu::separator {{ height: 1px; background: {COLORS["border"]}; margin: 5px 8px; }}

QToolTip {{
    color: {COLORS["text"]};
    background-color: {COLORS["surface"]};
    border: 1px solid {COLORS["border_strong"]};
    padding: 6px;
}}
"""
