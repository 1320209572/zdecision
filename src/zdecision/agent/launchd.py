"""Explicit ownership boundary for the macOS ZDecision LaunchAgent."""

from __future__ import annotations

import os
import plistlib
import subprocess
from collections.abc import Callable
from pathlib import Path

from zdecision.jsonio import atomic_write_bytes


LABEL = "com.zdecision.agent"
_PLIST_NAME = f"{LABEL}.plist"

CommandRunner = Callable[[list[str]], None]


def render_launch_agent(
    *,
    executable: str,
    state_dir: str,
    config_path: str,
) -> str:
    executable_path = _absolute(executable, "executable")
    state_path = _absolute(state_dir, "state_dir")
    config = _absolute(config_path, "config_path")
    value = {
        "Label": LABEL,
        "ProgramArguments": [
            executable_path,
            "service",
            "run",
            "--config",
            config,
        ],
        "EnvironmentVariables": {
            "ZDECISION_STATE_DIR": state_path,
            "PATH": os.environ.get("PATH") or os.defpath,
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
    }
    return plistlib.dumps(
        value,
        fmt=plistlib.FMT_XML,
        sort_keys=False,
    ).decode("utf-8")


def install_launch_agent(
    *,
    executable: str,
    state_dir: str,
    config_path: str,
    home: Path | None = None,
    uid: int | None = None,
    runner: CommandRunner | None = None,
) -> Path:
    target = _launch_agent_path(home)
    if target.exists():
        _require_owned_plist(target)
    rendered = render_launch_agent(
        executable=executable,
        state_dir=state_dir,
        config_path=config_path,
    )
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_bytes(target, rendered.encode("utf-8"))
    command_runner = runner or _run
    user_id = os.getuid() if uid is None else _uid(uid)
    command_runner(
        [
            "launchctl",
            "bootstrap",
            f"gui/{user_id}",
            str(target),
        ]
    )
    return target


def uninstall_launch_agent(
    *,
    home: Path | None = None,
    uid: int | None = None,
    runner: CommandRunner | None = None,
) -> bool:
    target = _launch_agent_path(home)
    if not target.exists():
        return False
    _require_owned_plist(target)
    command_runner = runner or _run
    user_id = os.getuid() if uid is None else _uid(uid)
    command_runner(
        [
            "launchctl",
            "bootout",
            f"gui/{user_id}",
            str(target),
        ]
    )
    target.unlink()
    return True


def launch_agent_status(
    *,
    home: Path | None = None,
    uid: int | None = None,
) -> dict[str, bool]:
    target = _launch_agent_path(home)
    installed = target.is_file()
    if installed:
        _require_owned_plist(target)
    user_id = os.getuid() if uid is None else _uid(uid)
    result = subprocess.run(
        ["launchctl", "print", f"gui/{user_id}/{LABEL}"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        "installed": installed,
        "loaded": result.returncode == 0,
    }


def _launch_agent_path(home: Path | None) -> Path:
    root = Path.home() if home is None else Path(home)
    if not root.is_absolute():
        raise ValueError("home must be absolute")
    return root / "Library" / "LaunchAgents" / _PLIST_NAME


def _require_owned_plist(path: Path) -> None:
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise ValueError("LaunchAgent plist is invalid") from error
    if not isinstance(value, dict) or value.get("Label") != LABEL:
        raise ValueError("LaunchAgent plist is not owned by ZDecision")


def _absolute(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or not Path(value).is_absolute()
    ):
        raise ValueError(f"{name} must be absolute")
    return str(Path(value))


def _uid(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("uid is invalid")
    return value


def _run(command: list[str]) -> None:
    subprocess.run(
        command,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
