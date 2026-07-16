"""Turn plain alert text into selectable, link-aware rich text for QLabel.

Alert bodies arrive as plain strings. These helpers make that text selectable
by mouse and turn any embedded ``http(s)`` URLs into clickable anchors, while
escaping everything else so untrusted alert content cannot inject markup.
"""

from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
# Punctuation that commonly follows a URL in prose but is not part of it.
_TRAILING = ".,;:!?)]}\"'"


def has_link(text: str) -> bool:
    return _URL_RE.search(text) is not None


def linkify(text: str, link_color: str) -> str:
    """Return HTML with URLs wrapped in styled anchors; all else escaped."""
    parts: list[str] = []
    last = 0
    for match in _URL_RE.finditer(text):
        parts.append(html.escape(text[last : match.start()]))
        url = match.group(0)
        trailing = ""
        while url and url[-1] in _TRAILING:
            trailing = url[-1] + trailing
            url = url[:-1]
        href = html.escape(url, quote=True)
        parts.append(f'<a href="{href}" style="color:{link_color};">{html.escape(url)}</a>')
        parts.append(html.escape(trailing))
        last = match.end()
    parts.append(html.escape(text[last:]))
    return "".join(parts)


def make_selectable(label: QLabel) -> None:
    """Allow mouse text selection on an otherwise plain label."""
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)


def apply_rich_text(label: QLabel, text: str, link_color: str) -> None:
    """Render alert body text: clickable links when present, always selectable."""
    if has_link(text):
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setText(linkify(text, link_color))
        label.setOpenExternalLinks(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
    else:
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setText(text)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
