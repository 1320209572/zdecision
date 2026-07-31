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
    gate3 = subparsers.add_parser(
        "gate3", help="run the diagnostic app-server Capture acceptance"
    )
    gate3.add_argument("--session-id", required=True)
    gate3.add_argument("--turn-id", required=True)

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
                clock=lambda: datetime.now(UTC),
            )
            sys.stdout.buffer.write(canonical_json_bytes(tools.zdecision_status()))
            return 0
        if arguments.command == "gate3":
            return _run_gate3(arguments, database, state_path)
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
            try:
                AgentService(
                    client=client,
                    processor=configured_processor(
                        database, config, state_path
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


def _run_gate3(
    arguments: argparse.Namespace,
    database: AgentDatabase,
    state_path: Path,
) -> int:
    from zdecision.app_server.capture_runner import (
        AutomatedCaptureError,
        AutomatedCaptureRunner,
    )
    from zdecision.app_server.gateway import AppServerGateway, AppServerGatewayError
    from zdecision.app_server.jsonl import AppServerError
    from zdecision.capture.service import CaptureService
    from zdecision.capture.templates import TemplateCatalog
    from zdecision.private_store.filesystem import FilePrivateStore

    package_root = Path(__file__).resolve().parents[1]
    repository_root = package_root.parents[1]
    store = FilePrivateStore(state_path.parents[1])
    catalog = TemplateCatalog(
        repository_root / "decision-templates",
        package_root / "capture" / "prompt_contracts",
    )
    gateway: AppServerGateway | None = None
    route = "replay"
    try:
        existing_id = database.automated_capture_id_for_boundary(
            arguments.session_id, arguments.turn_id
        )
        if existing_id is None:
            gateway = AppServerGateway.connect(database=database)
            route = gateway.route
        result = AutomatedCaptureRunner(
            gateway=gateway,
            database=database,
            capture_service=CaptureService(store, catalog),
            clock=lambda: datetime.now(UTC),
        ).run(arguments.session_id, arguments.turn_id)
    except (
        AppServerError,
        AutomatedCaptureError,
        AppServerGatewayError,
        OSError,
        ValueError,
    ):
        _write_error("gate3_failed")
        return 1
    finally:
        if gateway is not None:
            gateway.close()
    run = database.get_automated_capture_run(result.automated_capture_id)
    profile = database.get_feasibility_model_profile()
    assessment_record = database.get_boundary_assessment(
        result.automated_capture_id
    )
    if run is None or profile is None or assessment_record is None:
        _write_error("gate3_receipt_missing")
        return 1
    sys.stdout.buffer.write(
        canonical_json_bytes(
            {
                "assessment": {
                    "blocker_count": len(result.assessment.unresolved_blockers),
                    "durable_decision_signal": (
                        result.assessment.has_durable_decision_signal
                    ),
                    "phase": result.assessment.phase,
                    "validation": result.assessment.validation,
                },
                "assessment_thread_id": run.assessment_thread_id,
                "assessment_turn_id": result.assessment_turn_id,
                "automated_capture_id": result.automated_capture_id,
                "candidate_count": len(result.candidate_ids),
                "capture_operation_id": result.capture_operation_id,
                "capture_thread_id": result.capture_thread_id,
                "eligibility_input_digest": assessment_record.input_fact_digest,
                "eligibility_prompt_digest": assessment_record.prompt_digest,
                "extraction_turn_id": result.extraction_turn_id,
                "inventory_turn_id": result.inventory_turn_id,
                "model": {
                    "discovery_digest": profile.discovery_digest,
                    "model_id": profile.model_id,
                    "profile_id": profile.profile_id,
                    "reasoning_effort": profile.reasoning_effort,
                },
                "route": route,
                "source_thread_id": result.source_thread_id,
                "source_turn_id": result.source_turn_id,
                "state": run.state,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
