"""SignalDesk semantic color tokens and Qt stylesheet."""

from __future__ import annotations

from PySide6.QtGui import QColor

COLORS = {
    "background": "#080D18",
    "surface": "#101827",
    "surface_raised": "#162033",
    "surface_hover": "#1C2940",
    "border": "#28364D",
    "border_strong": "#3A4B67",
    "text": "#F8FAFC",
    "text_secondary": "#B6C2D4",
    "muted": "#8492A8",
    "primary": "#5B8CFF",
    "primary_hover": "#73A0FF",
    "info": "#38BDF8",
    "success": "#34D399",
    "warning": "#FBBF24",
    "critical": "#FB7185",
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

QLabel#BrandTitle {{
    font-size: 20px;
    font-weight: 700;
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
    border-radius: 12px;
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
    color: #07101F;
}}

QPushButton#PrimaryButton:hover {{
    background-color: {COLORS["primary_hover"]};
}}

QPushButton#PrimaryButton:pressed {{
    background-color: #4B7CEB;
}}

QPushButton#SecondaryButton {{
    background-color: {COLORS["surface_raised"]};
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
    color: #718096;
    background-color: #182235;
    border-color: #263248;
}}

QLineEdit {{
    min-height: 42px;
    padding: 0 12px;
    background-color: #0B1220;
    border: 1px solid {COLORS["border_strong"]};
    border-radius: 9px;
    selection-background-color: {COLORS["primary"]};
}}

QLineEdit:hover {{
    border-color: #526784;
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
    border: 2px solid #52627B;
    background-color: #0B1220;
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
    background-color: {COLORS["surface_raised"]};
    color: {COLORS["text"]};
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
    background: #34445E;
    min-height: 30px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical:hover {{
    background: #465A78;
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
    background-color: rgba(52, 211, 153, 28);
    border: 1px solid rgba(52, 211, 153, 75);
}}

QLabel#StatusPill[connectionState="connecting"] {{
    color: {COLORS["warning"]};
    background-color: rgba(251, 191, 36, 25);
    border: 1px solid rgba(251, 191, 36, 68);
}}

QLabel#StatusPill[connectionState="disconnected"], QLabel#StatusPill[connectionState="stopped"] {{
    color: {COLORS["critical"]};
    background-color: rgba(251, 113, 133, 25);
    border: 1px solid rgba(251, 113, 133, 68);
}}

QLabel#SeverityBadge {{
    padding: 3px 7px;
    border-radius: 6px;
    font-size: 10px;
    font-weight: 700;
}}

QLabel#SeverityBadge[severity="info"] {{ color: {COLORS["info"]}; background: rgba(56, 189, 248, 25); }}
QLabel#SeverityBadge[severity="success"] {{ color: {COLORS["success"]}; background: rgba(52, 211, 153, 25); }}
QLabel#SeverityBadge[severity="warning"] {{ color: {COLORS["warning"]}; background: rgba(251, 191, 36, 25); }}
QLabel#SeverityBadge[severity="critical"] {{ color: {COLORS["critical"]}; background: rgba(251, 113, 133, 25); }}

QFrame#AlertToast {{
    background-color: #111A2A;
    border: 1px solid #34445E;
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
    background-color: {COLORS["surface_raised"]};
    border: 1px solid {COLORS["border_strong"]};
    padding: 6px;
}}
"""
