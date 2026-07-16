"""Cross-platform launch-at-login integration for the background client."""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

APP_NAME = "SignalDesk"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MAC_LABEL = "com.signaldesk.alerts"


class StartupIntegrationError(RuntimeError):
    """Raised when the operating-system startup entry cannot be changed."""


def default_launch_command() -> list[str]:
    """Return the command used by an OS startup entry."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "--hidden"]
    return [str(Path(sys.executable).resolve()), "-m", "signaldesk", "--hidden"]


def set_launch_at_login(
    enabled: bool,
    *,
    command: list[str] | None = None,
    config_home: Path | None = None,
) -> None:
    """Enable or remove the current user's startup entry.

    No administrator privileges are required. Errors are surfaced so the UI
    can explain the recovery path instead of claiming the setting was saved.
    """
    launch = command or default_launch_command()
    try:
        if sys.platform == "win32":
            _set_windows(enabled, launch)
        elif sys.platform == "darwin":
            _set_macos(enabled, launch, config_home)
        else:
            _set_linux(enabled, launch, config_home)
    except (OSError, ValueError) as exc:
        raise StartupIntegrationError(str(exc) or "Unable to update launch-at-login") from exc


def is_launch_at_login_enabled(*, config_home: Path | None = None) -> bool:
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
                winreg.QueryValueEx(key, APP_NAME)
            return True
        if sys.platform == "darwin":
            return _mac_path(config_home).is_file()
        return _linux_path(config_home).is_file()
    except OSError:
        return False


def _set_windows(enabled: bool, command: list[str]) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(
                key,
                APP_NAME,
                0,
                winreg.REG_SZ,
                subprocess.list2cmdline(command),
            )
        else:
            with suppress(FileNotFoundError):
                winreg.DeleteValue(key, APP_NAME)


def _mac_path(config_home: Path | None) -> Path:
    root = config_home or Path.home() / "Library" / "LaunchAgents"
    return root / f"{MAC_LABEL}.plist"


def _set_macos(enabled: bool, command: list[str], config_home: Path | None) -> None:
    path = _mac_path(config_home)
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": MAC_LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "ProcessType": "Background",
    }
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        plistlib.dump(payload, stream)
    os.replace(temporary, path)


def _linux_path(config_home: Path | None) -> Path:
    root = config_home or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "autostart" / "signaldesk.desktop"


def _set_linux(enabled: bool, command: list[str], config_home: Path | None) -> None:
    path = _linux_path(config_home)
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    executable = shlex.join(command)
    content = "\n".join(
        (
            "[Desktop Entry]",
            "Type=Application",
            f"Name={APP_NAME}",
            f"Exec={executable}",
            "Terminal=false",
            "X-GNOME-Autostart-enabled=true",
            "Comment=Real-time operational alerts",
            "",
        )
    )
    temporary = path.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
