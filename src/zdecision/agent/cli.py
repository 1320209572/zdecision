"""Command boundary for Plugin Hooks, local MCP, and feasibility setup."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx

from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import TestRepositoryMapping
from zdecision.agent.hooks import handle_hook
from zdecision.agent.mcp_server import LocalMcpTools, run_mcp
from zdecision.agent.repository import RepositoryResolver
from zdecision.ids import canonical_product_name, product_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.private_store.filesystem import private_state_root


def database_path(environ: Mapping[str, str]) -> Path:
    return private_state_root(environ) / "agent" / "zdecision.sqlite3"


def config_locator_path(environ: Mapping[str, str]) -> Path:
    return private_state_root(environ) / "agent" / "config-locator.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zdecision-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hook", help="record one Codex Hook JSON object from stdin")
    subparsers.add_parser("mcp", help="serve the local ZDecision MCP tools over stdio")
    subparsers.add_parser("worker", help="run the singleton local Agent worker")
    subparsers.add_parser("status", help="show bounded local Agent status")
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
        help="configure a feasibility-only local repository mapping",
    )
    actions = repository.add_subparsers(dest="repository_action", required=True)
    enable = actions.add_parser("enable", help="enable one feasibility repository")
    enable.add_argument("--cwd", required=True)
    enable.add_argument("--product-name", required=True)
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
            raw = sys.stdin.buffer.read()
            response = handle_hook(
                raw,
                database=database,
                clock=lambda: datetime.now(UTC),
            )
            sys.stdout.buffer.write(canonical_json_bytes(dict(response.output)))
            return 0
        if arguments.command == "status":
            tools = LocalMcpTools(
                database=database,
                cwd=os.getcwd(),
            )
            sys.stdout.buffer.write(canonical_json_bytes(tools.zdecision_status()))
            return 0
        return _configure_test_repository(arguments, database)
    finally:
        database.close()


def _configure_test_repository(
    arguments: argparse.Namespace,
    database: AgentDatabase,
) -> int:
    snapshot = RepositoryResolver().resolve(Path(arguments.cwd).expanduser().resolve())
    if snapshot is None:
        _write_error("repository_not_resolved")
        return 1
    existing = database.get_repository_mapping(snapshot.repository_id)
    if arguments.repository_action == "enable":
        product_name = canonical_product_name(arguments.product_name)
        mapping = TestRepositoryMapping(
            repository_id=snapshot.repository_id,
            product_id=product_id(product_name),
            product_name=product_name,
            enabled=True,
        )
    else:
        if existing is None:
            _write_error("repository_not_registered")
            return 1
        mapping = TestRepositoryMapping(
            repository_id=existing.repository_id,
            product_id=existing.product_id,
            product_name=existing.product_name,
            enabled=False,
        )
    database.put_test_repository_mapping(mapping)
    sys.stdout.buffer.write(
        canonical_json_bytes(
            {
                "enabled": mapping.enabled,
                "product_id": mapping.product_id,
                "product_name": mapping.product_name,
                "repository_id": mapping.repository_id,
            }
        )
    )
    return 0


def _write_error(code: str) -> None:
    sys.stderr.buffer.write(canonical_json_bytes({"error": code}))


def _run_service_command(
    arguments: argparse.Namespace,
    state_path: Path,
) -> int:
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
        mirror_repository_mappings,
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
            mirror_repository_mappings(database, config)
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
