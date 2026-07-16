from pathlib import Path

import signaldesk.startup as startup


def test_linux_startup_entry_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(startup.sys, "platform", "linux")
    command = ["/opt/Signal Desk/signaldesk", "--hidden"]

    startup.set_launch_at_login(True, command=command, config_home=tmp_path)
    path = tmp_path / "autostart" / "signaldesk.desktop"
    assert path.is_file()
    assert "'/opt/Signal Desk/signaldesk' --hidden" in path.read_text(encoding="utf-8")
    assert startup.is_launch_at_login_enabled(config_home=tmp_path)

    startup.set_launch_at_login(False, command=command, config_home=tmp_path)
    assert not path.exists()


def test_macos_startup_entry_is_valid_plist(tmp_path, monkeypatch) -> None:
    import plistlib

    monkeypatch.setattr(startup.sys, "platform", "darwin")
    startup.set_launch_at_login(
        True,
        command=["/Applications/SignalDesk", "--hidden"],
        config_home=tmp_path,
    )
    path = tmp_path / f"{startup.MAC_LABEL}.plist"
    with path.open("rb") as stream:
        payload = plistlib.load(stream)
    assert payload["RunAtLoad"] is True
    assert payload["ProgramArguments"][-1] == "--hidden"


def test_default_launch_command_starts_hidden() -> None:
    command = startup.default_launch_command()
    assert command[-1] == "--hidden"
    assert Path(command[0]).is_absolute()
