"""Command boundary for Plugin Hooks, local MCP, and feasibility setup."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from zdecision.jsonio import canonical_json_bytes
from zdecision.private_store.filesystem import private_state_root


def database_path(environ: Mapping[str, str]) -> Path:
    return private_state_root(environ) / "agent" / "zdecision.sqlite3"


def config_locator_path(environ: Mapping[str, str]) -> Path:
    return private_state_root(environ) / "agent" / "config-locator.json"


def run_mcp(**arguments: object) -> None:
    """Load the MCP runtime only when its command is invoked."""

    from zdecision.agent.mcp_server import run_mcp as run_mcp_server

    run_mcp_server(**arguments)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zdecision-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hook", help="record one Codex Hook JSON object from stdin")
    subparsers.add_parser("mcp", help="serve the local ZDecision MCP tools over stdio")
    subparsers.add_parser("worker", help="run the singleton local Agent worker")
    subparsers.add_parser("status", help="show bounded local Agent status")
    if os.environ.get("ZDECISION_LIVE_ACCEPTANCE") == "1":
        recall_gate = subparsers.add_parser(
            "recall-host-gate",
            help="manage the live-acceptance-only Recall host probe",
        )
        recall_actions = recall_gate.add_subparsers(
            dest="recall_host_gate_action", required=True
        )
        prepare = recall_actions.add_parser(
            "prepare", help="prepare one bounded Recall host probe"
        )
        prepare.add_argument("--cwd", required=True)
        recall_actions.add_parser("clear", help="clear prepared Recall host probes")
    service = subparsers.add_parser(
        "service", help="manage the persistent local delivery service"
    )
    service_actions = service.add_subparsers(
        dest="service_action", required=True
    )
    service_run = service_actions.add_parser(
        "run", help="run the persistent local delivery loop"
    )
    service_run.add_argument("--config", required=True)
    service_install = service_actions.add_parser(
        "install", help="install and start the macOS LaunchAgent"
    )
    service_install.add_argument("--config", required=True)
    service_actions.add_parser(
        "uninstall", help="stop and remove the macOS LaunchAgent"
    )
    service_actions.add_parser(
        "status", help="show LaunchAgent installation status"
    )
    repository = subparsers.add_parser(
        "test-repository",
        help="configure feasibility-only local repository enablement",
    )
    actions = repository.add_subparsers(dest="repository_action", required=True)
    enable = actions.add_parser("enable", help="enable one feasibility repository")
    enable.add_argument("--cwd", required=True)
    disable = actions.add_parser("disable", help="disable one feasibility repository")
    disable.add_argument("--cwd", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    state_path = database_path(os.environ)
    if arguments.command == "mcp":
        run_mcp(
            database_path=state_path,
            config_locator_path=config_locator_path(os.environ),
            cwd=os.getcwd(),
        )
        return 0
    if arguments.command == "service":
        return _run_service_command(arguments, state_path)
    from zdecision.agent.db import AgentDatabase

    database = AgentDatabase.open(state_path)
    try:
        if arguments.command == "worker":
            from zdecision.agent.session_index import (
                SessionIndex,
                SessionIndexEventProcessor,
            )
            from zdecision.agent.worker import (
                Worker,
            )

            session_index = SessionIndex.open(state_path)
            try:
                Worker(
                    database=database,
                    processor=SessionIndexEventProcessor(session_index),
                    sync_poller=None,
                    lock_path=state_path.parent / "worker.lock",
                ).run_until_idle()
            finally:
                session_index.close()
            return 0
        if arguments.command == "hook":
            from zdecision.agent.hooks import handle_hook

            raw = sys.stdin.buffer.read()
            response = handle_hook(
                raw,
                database=database,
                clock=lambda: datetime.now(UTC),
            )
            sys.stdout.buffer.write(canonical_json_bytes(dict(response.output)))
            return 0
        if arguments.command == "status":
            from zdecision.agent.mcp_server import LocalMcpTools

            tools = LocalMcpTools(
                database=database,
                cwd=os.getcwd(),
            )
            sys.stdout.buffer.write(canonical_json_bytes(tools.zdecision_status()))
            return 0
        if arguments.command == "recall-host-gate":
            return _run_recall_host_gate(arguments, database, state_path)
        return _configure_test_repository(arguments, database)
    finally:
        database.close()


def _configure_test_repository(
    arguments: argparse.Namespace,
    database: AgentDatabase,
) -> int:
    from zdecision.agent.repository import RepositoryResolver
    from zdecision.central.decision_spaces import EnabledRepository

    snapshot = RepositoryResolver().resolve(Path(arguments.cwd).expanduser().resolve())
    if snapshot is None:
        _write_error("repository_not_resolved")
        return 1
    existing = database.get_enabled_repository(snapshot.repository_id)
    enabled = arguments.repository_action == "enable"
    if not enabled:
        if existing is None:
            _write_error("repository_not_registered")
            return 1
    repository = EnabledRepository(snapshot.repository_id, enabled)
    database.put_enabled_repository(repository)
    sys.stdout.buffer.write(
        canonical_json_bytes(
            {
                "enabled": repository.enabled,
                "repository_id": repository.repository_id,
            }
        )
    )
    return 0


def _write_error(code: str) -> None:
    sys.stderr.buffer.write(canonical_json_bytes({"error": code}))


def _run_recall_host_gate(
    arguments: argparse.Namespace,
    database: AgentDatabase,
    state_path: Path,
) -> int:
    from zdecision.agent.recall_mcp import clear_host_probes, prepare_host_probe
    from zdecision.agent.repository import RepositoryResolver

    if arguments.recall_host_gate_action == "clear":
        clear_host_probes(state_path)
        sys.stdout.buffer.write(canonical_json_bytes({"cleared": True}))
        return 0
    requested = Path(arguments.cwd).expanduser()
    if not requested.is_absolute():
        _write_error("recall_host_gate_invalid_repository")
        return 1
    cwd = str(requested.resolve())
    snapshot = RepositoryResolver().resolve(cwd)
    repository = (
        None
        if snapshot is None
        else database.get_enabled_repository(snapshot.repository_id)
    )
    if repository is None or not repository.enabled:
        _write_error("recall_host_gate_invalid_repository")
        return 1
    prepare_host_probe(
        state_path,
        cwd,
        f"probe_{uuid4().hex}",
    )
    sys.stdout.buffer.write(canonical_json_bytes({"prepared": True}))
    return 0


def _run_service_command(
    arguments: argparse.Namespace,
    state_path: Path,
) -> int:
    import httpx
    import shutil
    import subprocess

    from zdecision.agent.db import AgentDatabase
    from zdecision.agent.launchd import (
        install_launch_agent,
        launch_agent_status,
        uninstall_launch_agent,
    )
    from zdecision.agent.service import (
        AgentService,
        AgentServiceConfigError,
        configured_processor,
        load_agent_config,
        mirror_enabled_repositories,
    )

    try:
        if arguments.service_action == "uninstall":
            removed = uninstall_launch_agent()
            sys.stdout.buffer.write(
                canonical_json_bytes({"removed": removed})
            )
            return 0
        if arguments.service_action == "status":
            sys.stdout.buffer.write(
                canonical_json_bytes(launch_agent_status())
            )
            return 0

        config_path = Path(arguments.config).expanduser()
        config = load_agent_config(config_path)
        from zdecision.agent.config_locator import publish_agent_config_locator

        publish_agent_config_locator(
            state_path.with_name("config-locator.json"), config_path
        )
        database = AgentDatabase.open(state_path)
        try:
            mirror_enabled_repositories(database, config)
            if arguments.service_action == "install":
                executable = shutil.which("zdecision-agent")
                if executable is None:
                    executable = str(Path(sys.argv[0]).resolve())
                installed = install_launch_agent(
                    executable=executable,
                    state_dir=str(state_path.parent.parent),
                    config_path=str(config_path),
                )
                sys.stdout.buffer.write(
                    canonical_json_bytes(
                        {"installed": True, "path": str(installed)}
                    )
                )
                return 0

            from zdecision.agent.central_client import CentralClient

            client = CentralClient(config.central_url, config.device_token)
            lease_timeout = httpx.Timeout(
                5.0,
                connect=3.0,
                write=5.0,
                pool=3.0,
            )
            try:
                AgentService(
                    client=client,
                    processor=configured_processor(
                        database, config, state_path
                    ),
                    lease_client_factory=lambda: CentralClient(
                        config.central_url,
                        config.device_token,
                        timeout=lease_timeout,
                    ),
                ).run_forever()
            finally:
                client.close()
            return 0
        finally:
            database.close()
    except (
        AgentServiceConfigError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ):
        _write_error("agent_service_command_failed")
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
