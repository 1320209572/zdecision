"""Owner-only pointer to the existing local Agent configuration."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from zdecision.jsonio import atomic_write_json


def publish_agent_config_locator(
    locator_path: Path, agent_config_path: Path
) -> Path:
    """Validate and atomically publish only the Agent config's absolute path."""

    locator = _absolute(locator_path, "agent_config_locator_path_not_absolute")
    config = _absolute(agent_config_path, "agent_config_path_not_absolute")
    _load_and_validate_agent_config(config)
    atomic_write_json(locator, {"agent_config_path": str(config)})
    _validate_private_file(locator, exact_mode=True, kind="locator")
    return config


def load_agent_config_path(locator_path: Path) -> Path:
    """Load the strict locator and revalidate its target Agent config."""

    locator = _absolute(locator_path, "agent_config_locator_path_not_absolute")
    _validate_private_file(locator, exact_mode=True, kind="locator")
    try:
        value = json.loads(locator.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _config_error("agent_config_locator_invalid") from error
    if (
        not isinstance(value, Mapping)
        or frozenset(value) != {"agent_config_path"}
        or not isinstance(value["agent_config_path"], str)
    ):
        raise _config_error("agent_config_locator_invalid")
    config = _absolute(
        Path(value["agent_config_path"]), "agent_config_path_not_absolute"
    )
    _load_and_validate_agent_config(config)
    return config


def _load_and_validate_agent_config(path: Path) -> None:
    _validate_private_file(path, exact_mode=False, kind="config")
    from zdecision.agent.service import load_agent_config

    load_agent_config(path)


def _absolute(path: Path, error_code: str) -> Path:
    value = Path(path)
    if not value.is_absolute():
        raise _config_error(error_code)
    return value


def _validate_private_file(
    path: Path, *, exact_mode: bool, kind: str
) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise _config_error(f"agent_{kind}_invalid") from error
    mode = stat.S_IMODE(metadata.st_mode)
    invalid_mode = mode != 0o600 if exact_mode else bool(mode & 0o077)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or invalid_mode
    ):
        raise _config_error(f"agent_{kind}_permissions_invalid")


def _config_error(code: str):
    from zdecision.agent.service import AgentServiceConfigError

    return AgentServiceConfigError(code)
