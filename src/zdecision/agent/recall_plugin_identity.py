"""Closed, injectable identity for the trusted Recall Plugin bundle."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_HOOK_TOOL_BASENAMES = frozenset(
    {
        "show_zdecision_update",
        "show_zdecision_recall_confirmation",
        "decide_zdecision_recall",
        "get_zdecision_recall_handoff",
        "ack_zdecision_recall_delivery",
        "apply_zdecision_recall_delivery",
        "gate_zdecision_turn",
    }
)
_PRE_TOOL_PREFIX_BASENAMES = (
    "show_zdecision_update",
    "show_zdecision_recall_confirmation",
    "apply_zdecision_recall_delivery",
    "gate_zdecision_turn",
)
_HOOK_PROJECTION = (
    ("SessionStart", "startup|resume|clear|compact", 3, 0),
    ("UserPromptSubmit", None, 3, 4000),
    ("PreCompact", "manual|auto", 3, None),
    ("PostCompact", "manual|auto", 3, None),
    ("PreToolUse", "identity", 3, None),
    ("PostToolUse", None, 3, None),
    ("Stop", None, 3, None),
    ("SessionEnd", "other", 3, None),
)


@dataclass(frozen=True)
class RecallPluginIdentity:
    plugin_name: str
    mcp_server_key: str
    mcp_command: str
    mcp_args: tuple[str, ...]
    hook_command: str
    recall_skill_relative_path: str

    def __post_init__(self) -> None:
        _identifier(self.plugin_name, "plugin_name")
        _identifier(self.mcp_server_key, "mcp_server_key")
        _command(self.mcp_command, "mcp_command")
        _arguments(self.mcp_args)
        _command(self.hook_command, "hook_command")
        _skill_path(self.recall_skill_relative_path)

    @property
    def tool_namespace(self) -> str:
        return self.mcp_server_key.replace("-", "_")

    def tool_name(self, basename: str) -> str:
        if basename not in _HOOK_TOOL_BASENAMES:
            raise ValueError("unsupported identity-sensitive MCP tool")
        return f"mcp__{self.tool_namespace}__{basename}"

    @property
    def pre_tool_matcher(self) -> str:
        return "|".join(
            (
                *(self.tool_name(name) for name in _PRE_TOOL_PREFIX_BASENAMES),
                "Bash",
                "apply_patch",
                "Edit",
                "Write",
                "Agent",
                "mcp__.*",
            )
        )


@dataclass(frozen=True)
class VerifiedRecallPluginBundle:
    root: Path
    skill_path: Path
    bundle_digest: str


def verify_recall_plugin_bundle(
    plugin_root: object, identity: RecallPluginIdentity
) -> VerifiedRecallPluginBundle | None:
    """Accept only the exact on-disk Plugin bundle for *identity*."""

    if not isinstance(identity, RecallPluginIdentity):
        return None
    if (
        not isinstance(plugin_root, (str, Path))
        or not str(plugin_root)
        or len(str(plugin_root).encode("utf-8")) > 4096
        or "\x00" in str(plugin_root)
    ):
        return None
    try:
        supplied_root = Path(plugin_root)
        if not supplied_root.is_absolute():
            return None
        root = supplied_root.resolve(strict=True)
        manifest_path = _contained_file(root, ".codex-plugin/plugin.json")
        mcp_path = _contained_file(root, ".mcp.json")
        skill_path = _contained_file(root, identity.recall_skill_relative_path)
        hooks_path = _contained_file(root, "hooks/hooks.json")
        if (
            manifest_path is None
            or mcp_path is None
            or skill_path is None
            or hooks_path is None
            or manifest_path.stat().st_size > 65_536
            or mcp_path.stat().st_size > 65_536
            or skill_path.stat().st_size > 262_144
            or hooks_path.stat().st_size > 65_536
        ):
            return None
        manifest_bytes = manifest_path.read_bytes()
        mcp_bytes = mcp_path.read_bytes()
        skill_bytes = skill_path.read_bytes()
        hooks_bytes = hooks_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        mcp = json.loads(mcp_bytes)
        hooks_document = json.loads(hooks_bytes)
        if not _valid_manifest(manifest, identity):
            return None
        if not _valid_mcp(mcp, identity):
            return None
        if not _valid_hooks(hooks_document, identity):
            return None
        return VerifiedRecallPluginBundle(
            root=root,
            skill_path=skill_path,
            bundle_digest=hashlib.sha256(
                manifest_bytes + b"\0" + mcp_bytes + b"\0" + skill_bytes
            ).hexdigest(),
        )
    except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _identifier(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not value.isascii()
        or _IDENTIFIER.fullmatch(value) is None
    ):
        raise ValueError(f"{name} is invalid")


def _command(value: object, name: str) -> None:
    if not isinstance(value, str) or "\x00" in value or not 1 <= len(value.encode("utf-8")) <= 4096:
        raise ValueError(f"{name} is invalid")


def _arguments(value: object) -> None:
    if not isinstance(value, tuple) or len(value) > 16:
        raise ValueError("mcp_args is invalid")
    total = 0
    for argument in value:
        if not isinstance(argument, str) or "\x00" in argument:
            raise ValueError("mcp_args is invalid")
        length = len(argument.encode("utf-8"))
        if not 1 <= length <= 4096:
            raise ValueError("mcp_args is invalid")
        total += length
    if total > 16_384:
        raise ValueError("mcp_args is invalid")


def _skill_path(value: object) -> None:
    if not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > 512:
        raise ValueError("recall_skill_relative_path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.name != "SKILL.md"
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
        or str(path) != value
    ):
        raise ValueError("recall_skill_relative_path is invalid")


def _contained_file(root: Path, relative: str) -> Path | None:
    path = (root / relative).resolve(strict=True)
    if root not in path.parents or not path.is_file():
        return None
    return path


def _valid_manifest(value: object, identity: RecallPluginIdentity) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("name") == identity.plugin_name
        and value.get("skills") == "./skills/"
        and value.get("mcpServers") == "./.mcp.json"
        and "hooks" not in value
    )


def _valid_mcp(value: object, identity: RecallPluginIdentity) -> bool:
    if not isinstance(value, dict) or set(value) != {"mcpServers"}:
        return False
    servers = value["mcpServers"]
    if not isinstance(servers, dict) or set(servers) != {identity.mcp_server_key}:
        return False
    server = servers[identity.mcp_server_key]
    return bool(
        isinstance(server, dict)
        and set(server) == {"command", "args"}
        and server.get("command") == identity.mcp_command
        and server.get("args") == list(identity.mcp_args)
    )


def _valid_hooks(value: object, identity: RecallPluginIdentity) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("hooks"), dict):
        return False
    hooks = value["hooks"]
    if not isinstance(hooks, dict) or set(hooks) != {event for event, *_ in _HOOK_PROJECTION}:
        return False
    for event, matcher, timeout, context_limit in _HOOK_PROJECTION:
        entries = hooks[event]
        expected_matcher = identity.pre_tool_matcher if matcher == "identity" else matcher
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            return False
        entry = entries[0]
        expected_entry_keys = {"hooks"}
        if expected_matcher is not None:
            expected_entry_keys.add("matcher")
        if set(entry) != expected_entry_keys or (
            expected_matcher is not None
            and entry.get("matcher") != expected_matcher
        ):
            return False
        handlers = entry.get("hooks")
        if not isinstance(handlers, list) or len(handlers) != 1 or not isinstance(handlers[0], dict):
            return False
        handler = handlers[0]
        expected = {"type": "command", "command": identity.hook_command, "timeout": timeout}
        if context_limit is not None:
            expected["additionalContextLimit"] = context_limit
        if handler != expected:
            return False
    return True


PRODUCTION_RECALL_PLUGIN_IDENTITY = RecallPluginIdentity(
    plugin_name="zdecision",
    mcp_server_key="zdecision-local",
    mcp_command="zdecision-agent",
    mcp_args=("mcp",),
    hook_command="zdecision-agent hook",
    recall_skill_relative_path="skills/zdecision/SKILL.md",
)
