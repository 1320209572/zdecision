"""Disposable, production-composed harness for the Recall Gate A vertical.

This module is test-only.  Its generated launcher validates an immutable
on-disk bundle before directly composing the production Store, Hook and MCP
server with a deterministic provider.
"""

from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator, Sequence

from zdecision.agent.db import AgentDatabase
from zdecision.agent.hooks import handle_hook
from zdecision.agent.mcp_server import LocalMcpTools, create_mcp_server
from zdecision.agent.recall_handoff import RecallHandoffService
from zdecision.agent.recall_host_state import RecallHostStore
from zdecision.agent.recall_mcp import RecallMcpTools, delivery_id_for_attempt
from zdecision.agent.recall_plugin_identity import (
    RecallPluginIdentity,
    verify_recall_plugin_bundle,
)
from zdecision.agent.repository import RepositoryResolver
from zdecision.capture.models import CandidateContent, SourceCheckpoint
from zdecision.capture.reviews import ApprovalRef
from zdecision.central.decision_spaces import EnabledRepository
from zdecision.ids import decision_id, product_id
from zdecision.recall.handoff import (
    RecallPreflightReady,
    RecallShortlist,
    RecalledDecision,
)
from zdecision.recall.session import RecallIntent
from zdecision.registry.models import DecisionRevision, DecisionSeed


_MARKER = ".recall-gate-a-marker.json"
_CONFIGURATION = ".recall-gate-a-runtime.json"
_LOCK = ".recall-gate-a-lifecycle.lock"
_LEASES = ".recall-gate-a-leases"
_STATE = "state"
_LAUNCHER_NAME = "recall_gate_a_launcher.py"


def gate_id_for_turn(turn_id: object) -> str:
    return "gate_" + hashlib.sha256(str(turn_id).encode("utf-8")).hexdigest()[:32]


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8") if isinstance(value, str) else value)
    path.chmod(0o600)


def _write_json(path: Path, value: object) -> None:
    _write(path, _canonical(value))


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _source_repository() -> Path:
    repository = Path(__file__).resolve(strict=True).parents[2]
    if not (repository / ".venv" / "bin" / "python").is_file():
        raise RuntimeError("source repository Python is unavailable")
    return repository


def _repository_commitment(repository: Path) -> str:
    resolved = repository.resolve(strict=True)
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:32]


def _plugin_identity_components(plugin_name: object) -> tuple[str, str] | None:
    prefix = "zdecision-gatea-"
    if not isinstance(plugin_name, str) or not plugin_name.startswith(prefix):
        return None
    components = plugin_name.removeprefix(prefix).split("-")
    if (
        len(components) != 2
        or len(components[0]) != 16
        or len(components[1]) != 32
        or any(
            character not in "0123456789abcdef"
            for component in components
            for character in component
        )
    ):
        return None
    return components[0], components[1]


def _mcp_server_key(plugin_name: object) -> str:
    components = _plugin_identity_components(plugin_name)
    if components is None:
        raise RuntimeError("disposable Plugin identity is invalid")
    return f"zgatea-{components[0]}"


def _plugin_commits_to_target(
    plugin_name: str,
    target_repository: Path,
) -> bool:
    components = _plugin_identity_components(plugin_name)
    if components is None:
        return False
    try:
        expected_target = _repository_commitment(target_repository)
    except OSError:
        return False
    return secrets.compare_digest(components[1], expected_target)


def _fresh_root(
    root: Path,
    source_repository: Path,
    target_repository: Path,
) -> Path:
    if not root.is_absolute() or root.exists():
        raise RuntimeError("disposable root must be a fresh absolute path")
    resolved_parent = root.parent.resolve(strict=True)
    candidate = resolved_parent / root.name
    home = Path.home().resolve(strict=True)
    repositories = (
        source_repository.resolve(strict=True),
        target_repository.resolve(strict=True),
    )
    if any(_inside(candidate, repository) for repository in repositories) or _inside(
        candidate, home
    ):
        raise RuntimeError("disposable root must not be below repository or home")
    production_cache = home / ".codex"
    if _inside(candidate, production_cache):
        raise RuntimeError("disposable root must not be below production cache")
    return candidate


def _identity(
    plugin_name: str,
    launcher: Path,
    source_repository: Path,
) -> RecallPluginIdentity:
    python = source_repository / ".venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError("source repository Python is unavailable")
    command = str(python)
    hook_command = " ".join(
        __import__("shlex").quote(part) for part in (command, str(launcher), "hook")
    )
    return RecallPluginIdentity(
        plugin_name=plugin_name,
        mcp_server_key=_mcp_server_key(plugin_name),
        mcp_command=command,
        mcp_args=(str(launcher), "mcp"),
        hook_command=hook_command,
        recall_skill_relative_path="skills/recall/SKILL.md",
    )


def _hooks(identity: RecallPluginIdentity) -> dict[str, object]:
    entries = (
        ("SessionStart", "startup|resume|clear|compact", 0),
        ("UserPromptSubmit", None, 4000),
        ("PreCompact", "manual|auto", None),
        ("PostCompact", "manual|auto", None),
        ("PreToolUse", identity.pre_tool_matcher, None),
        ("PostToolUse", None, None),
        ("Stop", None, None),
        ("SessionEnd", "other", None),
    )
    document: dict[str, object] = {"hooks": {}}
    hooks = document["hooks"]
    assert isinstance(hooks, dict)
    for event, matcher, limit in entries:
        handler: dict[str, object] = {
            "type": "command",
            "command": identity.hook_command,
            "timeout": 3,
        }
        if limit is not None:
            handler["additionalContextLimit"] = limit
        entry: dict[str, object] = {"hooks": [handler]}
        if matcher is not None:
            entry["matcher"] = matcher
        hooks[event] = [entry]
    return document


def _launcher_source(
    *,
    root_relative_launcher: str,
    source_repository: Path,
    target_repository: Path,
    identity: RecallPluginIdentity,
    generation: str,
) -> str:
    fields = repr(
        (
            identity.plugin_name,
            identity.mcp_server_key,
            identity.mcp_command,
            identity.mcp_args,
            identity.hook_command,
            identity.recall_skill_relative_path,
        )
    )
    return f'''from __future__ import annotations
import hashlib
import json
import os
import sys
from pathlib import Path

IDENTITY = {fields}
SOURCE_REPOSITORY = {str(source_repository)!r}
TARGET_REPOSITORY = {str(target_repository)!r}
LAUNCHER_RELATIVE_PATH = {root_relative_launcher!r}
GENERATION = {generation!r}

def _fail(message: str) -> None:
    raise SystemExit(message)

def _root() -> tuple[Path, Path]:
    launcher = Path(__file__).resolve()
    root = launcher.parents[3]
    marker_path = root / {_MARKER!r}
    try:
        marker = json.loads(marker_path.read_text("utf-8"))
        root_stat = root.stat()
        digest = hashlib.sha256(launcher.read_bytes()).hexdigest()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _fail("invalid disposable Recall launcher")
    if (
        marker.get("launcher_relative_path") != LAUNCHER_RELATIVE_PATH
        or marker.get("launcher_digest") != digest
        or marker.get("generation") != GENERATION
        or marker.get("root_device") != root_stat.st_dev
        or marker.get("root_inode") != root_stat.st_ino
        or not isinstance(marker.get("generation"), str)
    ):
        _fail("disposable Recall launcher ownership mismatch")
    source_plugin_root = launcher.parent
    plugin_root = source_plugin_root
    supplied = os.environ.get("PLUGIN_ROOT")
    if supplied is not None:
        try:
            plugin_root = Path(supplied).resolve(strict=True)
        except OSError:
            _fail("PLUGIN_ROOT is invalid")
    prefix = "zdecision-gatea-"
    components = IDENTITY[0].removeprefix(prefix).split("-")
    try:
        target_commitment = hashlib.sha256(
            str(Path(TARGET_REPOSITORY).resolve(strict=True)).encode("utf-8")
        ).hexdigest()[:32]
    except OSError:
        _fail("disposable Recall repository binding is invalid")
    if (
        source_plugin_root.name != IDENTITY[0]
        or not IDENTITY[0].startswith(prefix)
        or len(components) != 2
        or IDENTITY[2] != str(Path(SOURCE_REPOSITORY) / ".venv" / "bin" / "python")
        or components[1] != target_commitment
    ):
        _fail("disposable Recall repository binding mismatch")
    return root, plugin_root

def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("hook", "mcp"):
        _fail("expected exactly one of hook or mcp")
    root, plugin_root = _root()
    if SOURCE_REPOSITORY not in sys.path:
        sys.path.insert(0, SOURCE_REPOSITORY)
    from tests.integration.recall_gate_a_desktop_harness import run_launcher
    return run_launcher(
        root=root,
        plugin_root=plugin_root,
        identity_fields=IDENTITY,
        source_repository=SOURCE_REPOSITORY,
        target_repository=TARGET_REPOSITORY,
        command=sys.argv[1],
    )

if __name__ == "__main__":
    raise SystemExit(main())
'''


def _marker(root: Path) -> dict[str, object]:
    try:
        marker = json.loads((root / _MARKER).read_text("utf-8"))
        root_stat = root.stat()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("disposable root marker is invalid") from error
    required = {
        "generation",
        "root_device",
        "root_inode",
        "launcher_relative_path",
        "launcher_digest",
    }
    if set(marker) != required or not isinstance(marker["generation"], str):
        raise RuntimeError("disposable root marker is invalid")
    if marker["root_device"] != root_stat.st_dev or marker["root_inode"] != root_stat.st_ino:
        raise RuntimeError("disposable root generation changed")
    launcher = root / marker["launcher_relative_path"]
    try:
        digest = hashlib.sha256(launcher.read_bytes()).hexdigest()
    except OSError as error:
        raise RuntimeError("disposable launcher is unavailable") from error
    if digest != marker["launcher_digest"]:
        raise RuntimeError("disposable launcher changed")
    frozen = _frozen_launcher_configuration(launcher)
    if frozen["generation"] != marker["generation"]:
        raise RuntimeError("disposable launcher generation changed")
    configuration = _read_configuration(root)
    if configuration["generation"] != marker["generation"]:
        raise RuntimeError("disposable root generation changed")
    if (
        frozen["source_repository"] != str(_source_repository())
        or configuration["source_repository"] != frozen["source_repository"]
        or configuration["target_repository"] != frozen["target_repository"]
    ):
        raise RuntimeError("disposable repository binding changed")
    identity = _identity_from_fields(configuration["identity"])
    plugin = launcher.parent
    if (
        plugin.name != identity.plugin_name
        or identity.mcp_command
        != str(Path(frozen["source_repository"]) / ".venv" / "bin" / "python")
        or not _plugin_commits_to_target(
            identity.plugin_name,
            Path(frozen["target_repository"]),
        )
        or verify_recall_plugin_bundle(plugin, identity) is None
    ):
        raise RuntimeError("disposable target commitment changed")
    return marker


def _frozen_launcher_configuration(launcher: Path) -> dict[str, str]:
    """Read closed generated literals only after the marker verified launcher bytes."""

    try:
        document = ast.parse(launcher.read_text("utf-8"), filename=str(launcher))
    except (OSError, SyntaxError, UnicodeDecodeError) as error:
        raise RuntimeError("disposable launcher configuration is invalid") from error
    values: dict[str, str] = {}
    for name, key in (
        ("GENERATION", "generation"),
        ("SOURCE_REPOSITORY", "source_repository"),
        ("TARGET_REPOSITORY", "target_repository"),
    ):
        assignments = [
            statement
            for statement in document.body
            if isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == name
        ]
        if (
            len(assignments) != 1
            or not isinstance(assignments[0].value, ast.Constant)
            or not isinstance(assignments[0].value.value, str)
        ):
            raise RuntimeError("disposable launcher configuration is invalid")
        values[key] = assignments[0].value.value
    generation = values["generation"]
    if len(generation) != 32 or any(
        character not in "0123456789abcdef" for character in generation
    ):
        raise RuntimeError("disposable launcher generation is invalid")
    if any(
        "\x00" in values[key] or not Path(values[key]).is_absolute()
        for key in ("source_repository", "target_repository")
    ):
        raise RuntimeError("disposable launcher repository binding is invalid")
    return values


def _read_configuration(root: Path) -> dict[str, object]:
    try:
        value = json.loads((root / _CONFIGURATION).read_text("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("disposable configuration is invalid") from error
    if (
        not isinstance(value, dict)
        or set(value)
        != {"source_repository", "target_repository", "identity", "generation"}
        or not isinstance(value["source_repository"], str)
        or not isinstance(value["target_repository"], str)
        or not isinstance(value["generation"], str)
        or "\x00" in value["source_repository"]
        or "\x00" in value["target_repository"]
        or not Path(value["source_repository"]).is_absolute()
        or not Path(value["target_repository"]).is_absolute()
    ):
        raise RuntimeError("disposable configuration is invalid")
    return value


def _identity_from_fields(fields: object) -> RecallPluginIdentity:
    if not isinstance(fields, (list, tuple)) or len(fields) != 6:
        raise RuntimeError("launcher identity is invalid")
    try:
        identity = RecallPluginIdentity(
            plugin_name=fields[0],
            mcp_server_key=fields[1],
            mcp_command=fields[2],
            mcp_args=tuple(fields[3]),
            hook_command=fields[4],
            recall_skill_relative_path=fields[5],
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError("launcher identity is invalid") from error
    if identity.mcp_server_key != _mcp_server_key(identity.plugin_name):
        raise RuntimeError("disposable Plugin identity is invalid")
    return identity


@contextmanager
def _lifecycle_lock(root: Path) -> Iterator[None]:
    path = root / _LOCK
    stream = path.open("a+b")
    path.chmod(0o600)
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class _McpLease:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path: Path | None = None

    def __enter__(self) -> "_McpLease":
        with _lifecycle_lock(self.root):
            directory = self.root / _LEASES
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
            self.path = directory / f"{os.getpid()}-{secrets.token_hex(12)}"
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.close(descriptor)
        return self

    def __exit__(self, *_: object) -> None:
        if self.path is not None:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def _live_leases(root: Path) -> int:
    directory = root / _LEASES
    if not directory.exists():
        return 0
    count = 0
    for path in directory.iterdir():
        try:
            pid = int(path.read_text("ascii"))
            os.kill(pid, 0)
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
        else:
            count += 1
    return count


class DeterministicGateAProvider:
    """Fixed formal Decisions; it intentionally has no I/O side effects."""

    def __init__(self) -> None:
        self.preflight_calls = 0
        self.retrieve_calls = 0

    def preflight(
        self, *, repository_id: str, repository_display_name: str, intent: RecallIntent, now: datetime
    ) -> RecallPreflightReady:
        self.preflight_calls += 1
        return RecallPreflightReady(
            repository_id=repository_id,
            repository_display_name=repository_display_name,
            intent=intent,
            target_decision_space_ids=intent.target_decision_space_ids,
            target_display_names=("Gate A test decisions",),
            catalog_digest="a" * 64,
            generation=1,
            generation_digest="b" * 64,
            retrieval_profile_digest="c" * 64,
            index_generation=1,
            freshness="ready",
            expires_at=(now + timedelta(hours=1)).astimezone(UTC).isoformat(),
        )

    def retrieve(self, preflight: RecallPreflightReady) -> RecallShortlist:
        self.retrieve_calls += 1
        product = "Gate A"
        identifier = product_id(product)
        def revision(seed: str, claim: str, path: str) -> DecisionRevision:
            candidate = f"cand_{seed * 32}_01"
            return DecisionRevision.from_seed(
                DecisionSeed(
                    candidate_id=candidate,
                    decision_id=decision_id(candidate, identifier),
                    product_id=identifier,
                    product_name=product,
                    content=CandidateContent(
                        product=product,
                        claim=claim,
                        future_action="Apply only through the complete Gate A handoff.",
                        scope_summary="Gate A disposable vertical",
                        repositories=("https://example.invalid/gate-a.git",),
                        paths=(path,),
                        invalidation_conditions=("The Gate A contract changes.",),
                    ),
                    source=SourceCheckpoint("fixture-source", f"fixture-turn-{seed}"),
                    review_approval=ApprovalRef(
                        actor="user", thread_id="fixture-review", turn_id=f"fixture-review-{seed}", recorded_at="2026-08-12T00:00:00Z"
                    ),
                ),
                f"pub_{seed * 32}",
            )
        applicable = RecalledDecision.create(
            decision_space_id=preflight.target_decision_space_ids[0],
            revision=revision("1", "The designated Gate A target is applicable.", "src/gate-a"),
            match_reason="designated target",
        )
        excluded = RecalledDecision.create(
            decision_space_id=preflight.target_decision_space_ids[0],
            revision=revision("2", "A different target is explicitly excluded.", "src/not-gate-a"),
            match_reason="negative control",
        )
        return RecallShortlist.create(preflight=preflight, items=(applicable, excluded))


class GateARuntime:
    """One in-process production composition used by hooks and the launcher."""

    def __init__(
        self,
        *,
        root: Path,
        repository: Path,
        identity: RecallPluginIdentity,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = root
        self.repository = repository
        self.identity = identity
        self.clock = clock if clock is not None else lambda: datetime.now(UTC)
        self.database = AgentDatabase.open(root / _STATE / "agent.sqlite3")
        self.store = RecallHostStore.open(root / _STATE / "agent.sqlite3", identity=identity)
        resolved = RepositoryResolver(timeout_seconds=1.0).resolve(repository)
        if resolved is None:
            self.close()
            raise RuntimeError("enabled repository could not be resolved")
        self.resolver = RepositoryResolver(timeout_seconds=1.0)
        self.database.put_enabled_repository(EnabledRepository(resolved.repository_id, True))
        self.provider = DeterministicGateAProvider()
        self.service = RecallHandoffService(
            store=self.store,
            provider=self.provider,
            clock=self.clock,
            delivery_id_factory=delivery_id_for_attempt,
            claim_token_factory=lambda: "claim_" + secrets.token_hex(16),
        )
        self.recall_tools = RecallMcpTools(
            host_store=self.store,
            handoff_service=self.service,
            cwd=str(repository),
            clock=self.clock,
        )
        self.server = create_mcp_server(
            LocalMcpTools(database=self.database, cwd=str(repository)),
            self.recall_tools,
            recall_identity=identity,
        )
        self._secure_state_files()

    def _secure_state_files(self) -> None:
        state = self.root / _STATE
        if not state.is_dir():
            return
        state.chmod(0o700)
        for path in state.glob("agent.sqlite3*"):
            if path.is_file():
                path.chmod(0o600)

    def close(self) -> None:
        self.store.close()
        self.database.close()
        self._secure_state_files()

    def hook(self, raw: object) -> dict[str, object]:
        response = handle_hook(
            raw,
            database=self.database,
            clock=self.clock,
            repository_resolver=self.resolver,
            worker_waker=lambda _: None,
            recall_store=self.store,
            recall_provider=self.provider,
            activation_attempt_id_factory=lambda: "activation_" + "a" * 32,
            turn_gate_id_factory=lambda _session, turn, *_: gate_id_for_turn(turn),
            recall_identity=self.identity,
        )
        return response.output


def create(*, root: Path, target_repository: Path) -> dict[str, object]:
    """Write a fresh self-contained marketplace and exactly one Plugin."""

    source_repository = _source_repository()
    target_repository = target_repository.resolve(strict=True)
    root = _fresh_root(root, source_repository, target_repository)
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    instance_id = secrets.token_hex(8)
    plugin_name = "-".join(
        (
            "zdecision-gatea",
            instance_id,
            _repository_commitment(target_repository),
        )
    )
    plugin = root / "marketplace" / "plugins" / plugin_name
    launcher = plugin / _LAUNCHER_NAME
    relative_launcher = launcher.relative_to(root).as_posix()
    identity = _identity(
        plugin_name,
        launcher,
        source_repository,
    )
    generation = secrets.token_hex(16)
    for directory in (plugin, plugin / "skills" / "recall", plugin / "hooks", root / _STATE, root / _LEASES):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    _write_json(plugin / ".codex-plugin" / "plugin.json", {
        "name": identity.plugin_name,
        "version": "0.1.0",
        "description": "Disposable ZDecision Recall Gate A acceptance Plugin",
        "author": {"name": "ZDecision"},
        "skills": "./skills/",
        "mcpServers": "./.mcp.json",
        "interface": {
            "displayName": "ZDecision Gate A",
            "shortDescription": "Disposable Recall Gate A acceptance",
            "longDescription": (
                "Runs the isolated Recall Gate A Desktop acceptance without "
                "replacing the production ZDecision Plugin."
            ),
            "developerName": "ZDecision",
            "category": "Productivity",
            "capabilities": ["Read"],
            "defaultPrompt": ["Run ZDecision Gate A"],
        },
    })
    _write_json(plugin / ".mcp.json", {"mcpServers": {identity.mcp_server_key: {
        "command": identity.mcp_command, "args": list(identity.mcp_args)}}})
    _write(
        plugin / identity.recall_skill_relative_path,
        """---
name: zdecision-gate-a
description: Run the isolated ZDecision Recall Gate A acceptance for the current task.
---

# ZDecision Gate A

Use this disposable Skill only for the bounded Recall Gate A acceptance.
""",
    )
    _write_json(plugin / "hooks" / "hooks.json", _hooks(identity))
    _write_json(root / "marketplace" / ".agents" / "plugins" / "marketplace.json", {
        "name": "recall-gate-a-disposable",
        "interface": {"displayName": "ZDecision Gate A Disposable"},
        "plugins": [{
            "name": identity.plugin_name,
            "source": {
                "source": "local",
                "path": f"./plugins/{identity.plugin_name}",
            },
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
            "category": "Productivity",
        }],
    })
    launcher_source = _launcher_source(
        root_relative_launcher=relative_launcher,
        source_repository=source_repository,
        target_repository=target_repository,
        identity=identity,
        generation=generation,
    )
    _write(launcher, launcher_source)
    marker = {
        "generation": generation,
        "root_device": root.stat().st_dev,
        "root_inode": root.stat().st_ino,
        "launcher_relative_path": relative_launcher,
        "launcher_digest": hashlib.sha256(launcher.read_bytes()).hexdigest(),
    }
    _write_json(root / _MARKER, marker)
    _write_json(root / _CONFIGURATION, {
        "source_repository": str(source_repository),
        "target_repository": str(target_repository),
        "generation": generation,
        "identity": list((identity.plugin_name, identity.mcp_server_key, identity.mcp_command, list(identity.mcp_args), identity.hook_command, identity.recall_skill_relative_path)),
    })
    if verify_recall_plugin_bundle(plugin, identity) is None:
        raise RuntimeError("generated disposable Plugin did not verify")
    return {"plugin_name": identity.plugin_name, "mcp_server_key": identity.mcp_server_key, "state": "ready"}


def inspect(*, root: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    _marker(root)
    configuration = _read_configuration(root)
    identity = _identity_from_fields(configuration["identity"])
    plugin = root / "marketplace" / "plugins" / identity.plugin_name
    if verify_recall_plugin_bundle(plugin, identity) is None:
        raise RuntimeError("generated Plugin no longer verifies")
    counts = {"attempt_count": 0, "delivery_count": 0, "active_item_count": 0}
    database = root / _STATE / "agent.sqlite3"
    if database.exists():
        with sqlite3.connect(database) as connection:
            for key, table in (("attempt_count", "recall_activation_attempts"), ("delivery_count", "recall_deliveries"), ("active_item_count", "recall_active_injected_items")):
                try:
                    counts[key] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    counts[key] = 0
    return {"state": "ready", "live_mcp_leases": _live_leases(root), **counts}


def cleanup(*, root: Path) -> dict[str, object]:
    root = root.resolve(strict=True)
    with _lifecycle_lock(root):
        _marker(root)
        if _live_leases(root):
            raise RuntimeError("every exact disposable MCP client must exit before cleanup")
        quarantined = root.with_name(f".{root.name}.cleanup-{secrets.token_hex(8)}")
        root.rename(quarantined)
        try:
            marker = _marker(quarantined)
            _delete_validated_quarantine(quarantined, marker)
        except Exception:
            if quarantined.exists() and not root.exists():
                quarantined.rename(root)
            raise
    return {"state": "removed"}


def _delete_validated_quarantine(quarantined: Path, marker: dict[str, object]) -> None:
    """Delete only the inode just validated by ``_marker``; never its replacement."""

    expected = (marker["root_device"], marker["root_inode"])
    parent_fd = os.open(quarantined.parent, os.O_RDONLY)
    directory_fd: int | None = None
    try:
        directory_fd = os.open(
            quarantined.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            dir_fd=parent_fd,
        )
        actual = os.fstat(directory_fd)
        if (actual.st_dev, actual.st_ino) != expected:
            raise RuntimeError("quarantined disposable root was replaced")
        _require_quarantine_name_binding(parent_fd, quarantined.name, expected)
        _remove_tree_at_fd(directory_fd)
        _require_quarantine_name_binding(parent_fd, quarantined.name, expected)
        os.rmdir(quarantined.name, dir_fd=parent_fd)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(parent_fd)


def _require_quarantine_name_binding(
    parent_fd: int, name: str, expected: tuple[object, object]
) -> None:
    try:
        bound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise RuntimeError("quarantined disposable root is unavailable") from error
    if (bound.st_dev, bound.st_ino) != expected:
        raise RuntimeError("quarantined disposable root was replaced")


def _remove_tree_at_fd(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode):
            expected = (entry.st_dev, entry.st_ino)
            try:
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise RuntimeError("disposable child directory is unavailable") from error
            try:
                opened = os.fstat(child_fd)
                if (
                    (opened.st_dev, opened.st_ino) != expected
                    or not stat.S_ISDIR(opened.st_mode)
                ):
                    raise RuntimeError("disposable child directory was replaced")
                _remove_tree_at_fd(child_fd)
            finally:
                os.close(child_fd)
            _require_child_name_binding(directory_fd, name, expected)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _require_child_name_binding(
    parent_fd: int, name: str, expected: tuple[int, int]
) -> None:
    try:
        bound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise RuntimeError("disposable child directory is unavailable") from error
    if (
        (bound.st_dev, bound.st_ino) != expected
        or not stat.S_ISDIR(bound.st_mode)
    ):
        raise RuntimeError("disposable child directory was replaced")


def _runtime(
    root: Path,
    plugin_root: Path,
    identity: RecallPluginIdentity,
    source_repository: str,
    target_repository: str,
) -> GateARuntime:
    _marker(root)
    if verify_recall_plugin_bundle(plugin_root, identity) is None:
        raise RuntimeError("launcher Plugin identity did not verify")
    configuration = _read_configuration(root)
    if tuple(configuration["identity"]) != (
        identity.plugin_name, identity.mcp_server_key, identity.mcp_command,
        list(identity.mcp_args), identity.hook_command, identity.recall_skill_relative_path,
    ):
        raise RuntimeError("launcher identity differs from generated configuration")
    if configuration["target_repository"] != target_repository:
        raise RuntimeError("launcher target differs from generated configuration")
    if configuration["source_repository"] != source_repository:
        raise RuntimeError("launcher source differs from generated configuration")
    return GateARuntime(
        root=root,
        repository=Path(target_repository),
        identity=identity,
    )


def run_launcher(
    *,
    root: Path,
    plugin_root: Path,
    identity_fields: object,
    source_repository: str,
    target_repository: str,
    command: str,
) -> int:
    if command not in ("hook", "mcp"):
        raise RuntimeError("launcher command is invalid")
    identity = _identity_from_fields(identity_fields)
    imported_source = Path(__file__).resolve(strict=True).parents[2]
    if imported_source != Path(source_repository).resolve(strict=True):
        raise RuntimeError("launcher imported the harness from a different source")
    runtime = _runtime(
        root,
        plugin_root,
        identity,
        source_repository,
        target_repository,
    )
    try:
        if command == "hook":
            raw = json.loads(sys.stdin.buffer.read(65_537).decode("utf-8"))
            print(json.dumps(runtime.hook(raw), separators=(",", ":"), sort_keys=True))
            return 0
        with _McpLease(root):
            runtime.server.run(transport="stdio")
        return 0
    finally:
        runtime.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recall-gate-a-desktop-harness")
    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create")
    create_parser.add_argument("--root", required=True)
    create_parser.add_argument("--target-repository", required=True)
    for name in ("inspect", "cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    root = Path(arguments.root)
    if arguments.command == "create":
        result = create(
            root=root,
            target_repository=Path(arguments.target_repository),
        )
    elif arguments.command == "inspect":
        result = inspect(root=root)
    else:
        result = cleanup(root=root)
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
