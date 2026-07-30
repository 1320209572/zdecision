"""Command boundary for Plugin Hooks, local MCP, and feasibility setup."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zdecision-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hook", help="record one Codex Hook JSON object from stdin")
    subparsers.add_parser("mcp", help="serve the local ZDecision MCP tools over stdio")
    subparsers.add_parser("worker", help="run the singleton local Agent worker")
    subparsers.add_parser("status", help="show bounded local Agent status")

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
        run_mcp(database_path=state_path, cwd=os.getcwd())
        return 0
    database = AgentDatabase.open(state_path)
    try:
        if arguments.command == "worker":
            from zdecision.agent.worker import (
                LocalEventProcessor,
                ProbeSyncPoller,
                Worker,
            )

            Worker(
                database=database,
                processor=LocalEventProcessor(),
                sync_poller=ProbeSyncPoller(),
                lock_path=state_path.parent / "worker.lock",
            ).run_until_idle()
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
                clock=lambda: datetime.now(UTC),
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


if __name__ == "__main__":
    raise SystemExit(main())
