"""Command boundary for Plugin Hooks, local MCP, and feasibility setup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from zdecision.jsonio import canonical_json_bytes
from zdecision.private_store.filesystem import private_state_root


_RECALL_DEMO_NONCE = b"zdecision-recall-demo-setup-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _RecallDemoArgumentError(Exception):
    """A Recall Demo parse error whose details must remain private."""


class _AgentArgumentParser(argparse.ArgumentParser):
    def __init__(
        self, *args: object, sanitize_errors: bool = False, **kwargs: object
    ) -> None:
        self._sanitize_errors = sanitize_errors
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        if self._sanitize_errors:
            raise _RecallDemoArgumentError()
        super().error(message)


def database_path(environ: Mapping[str, str]) -> Path:
    return private_state_root(environ) / "agent" / "zdecision.sqlite3"


def config_locator_path(environ: Mapping[str, str]) -> Path:
    return private_state_root(environ) / "agent" / "config-locator.json"


def run_mcp(**arguments: object) -> None:
    """Load the MCP runtime only when its command is invoked."""

    from zdecision.agent.mcp_server import run_mcp as run_mcp_server

    run_mcp_server(**arguments)


def build_parser(
    *, sanitize_recall_demo_errors: bool = False
) -> argparse.ArgumentParser:
    parser_factory = partial(
        _AgentArgumentParser, sanitize_errors=sanitize_recall_demo_errors
    )
    parser = _AgentArgumentParser(
        prog="zdecision-agent", sanitize_errors=sanitize_recall_demo_errors
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=parser_factory
    )
    subparsers.add_parser("hook", help="record one Codex Hook JSON object from stdin")
    subparsers.add_parser("mcp", help="serve the local ZDecision MCP tools over stdio")
    subparsers.add_parser("worker", help="run the singleton local Agent worker")
    subparsers.add_parser("status", help="show bounded local Agent status")
    service = subparsers.add_parser(
        "service", help="manage the persistent local delivery service"
    )
    service_actions = service.add_subparsers(
        dest="service_action", required=True, parser_class=parser_factory
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
    actions = repository.add_subparsers(
        dest="repository_action", required=True, parser_class=parser_factory
    )
    enable = actions.add_parser("enable", help="enable one feasibility repository")
    enable.add_argument("--cwd", required=True)
    disable = actions.add_parser("disable", help="disable one feasibility repository")
    disable.add_argument("--cwd", required=True)
    recall_demo = subparsers.add_parser(
        "recall-demo", help="configure the bounded local Recall demonstration"
    )
    recall_demo_actions = recall_demo.add_subparsers(
        dest="recall_demo_action", required=True, parser_class=parser_factory
    )
    configure = recall_demo_actions.add_parser("configure")
    for name in (
        "registry-product-root",
        "profile",
        "model-state-root",
        "trust-root",
        "bundle-state-root",
        "signing-private-key",
    ):
        configure.add_argument(f"--{name}", required=True, type=_absolute_path)
    configure.add_argument("--signing-key-id", required=True)
    recall_demo_actions.add_parser("status")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argument_values = tuple(sys.argv[1:] if argv is None else argv)
    try:
        arguments = build_parser(
            sanitize_recall_demo_errors=(
                bool(argument_values) and argument_values[0] == "recall-demo"
            )
        ).parse_args(argument_values)
    except _RecallDemoArgumentError:
        _write_recall_demo_error()
        return 2
    state_path = database_path(os.environ)
    if arguments.command == "recall-demo":
        return _run_recall_demo_command(arguments, os.environ)
    if arguments.command == "mcp":
        from zdecision.recall.demo.config import recall_demo_config_path

        run_mcp(
            database_path=state_path,
            config_locator_path=config_locator_path(os.environ),
            recall_demo_config_path=recall_demo_config_path(os.environ),
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
            from zdecision.recall.demo.config import recall_demo_config_path
            from zdecision.recall.demo.factory import configured_recall_provider

            raw = sys.stdin.buffer.read()
            provider = configured_recall_provider(recall_demo_config_path(os.environ))
            response = handle_hook(
                raw,
                database=database,
                clock=lambda: datetime.now(UTC),
                recall_provider=provider,
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
        return _configure_test_repository(arguments, database)
    finally:
        database.close()


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("filesystem path must be absolute")
    return path


def _run_recall_demo_command(
    arguments: argparse.Namespace, environ: Mapping[str, str]
) -> int:
    from zdecision.recall.demo.config import (
        DemoRecallConfig,
        load_demo_recall_config,
        recall_demo_config_path,
        write_demo_recall_config,
    )

    path = recall_demo_config_path(environ)
    if arguments.recall_demo_action == "configure":
        try:
            config = DemoRecallConfig.from_dict(
                {
                    "schema_version": 1,
                    "repository_name": "zstack-ui-next",
                    "product_name": "third-party-services",
                    "decision_space_id": "prod_3e6e73b8defbfee89ce7bf26e739b1dc",
                    "registry_product_root": str(arguments.registry_product_root),
                    "profile_path": str(arguments.profile),
                    "model_state_root": str(arguments.model_state_root),
                    "trust_root_path": str(arguments.trust_root),
                    "bundle_state_root": str(arguments.bundle_state_root),
                    "signing_private_key_path": str(arguments.signing_private_key),
                    "signing_key_id": arguments.signing_key_id,
                }
            )
            profile_digest, model_digest = _validate_recall_demo_setup(config)
            _prepare_bundle_state_root(config.provider.bundle_state_root)
            write_demo_recall_config(path, config)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            _write_recall_demo_error()
            return 1
        _write_recall_demo_status("configured", profile_digest, model_digest)
        return 0

    try:
        config = load_demo_recall_config(path)
    except FileNotFoundError:
        _write_recall_demo_status("not-configured", None, None)
        return 0
    except (OSError, RuntimeError, TypeError, ValueError):
        _write_recall_demo_error()
        return 1
    try:
        profile_digest, model_digest = _validate_recall_demo_setup(config)
        generation, digest = _current_recall_demo_generation(
            config.provider.bundle_state_root
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        _write_recall_demo_status("invalid", None, None)
        return 0
    _write_recall_demo_status(
        "configured", profile_digest, model_digest, generation, digest
    )
    return 0


def _validate_recall_demo_setup(config: object) -> tuple[str, str]:
    """Validate all owner-supplied setup material before config creation."""

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    from zdecision.recall.demo.config import DemoRecallConfig
    from zdecision.recall.demo.contracts import DemoRetrievalProfile
    from zdecision.recall.demo.model_store import load_installed_models
    from zdecision.registry.models import ProductMetadata, ProductRegistry

    if not isinstance(config, DemoRecallConfig):
        raise ValueError("recall_demo_config_invalid")
    profile = DemoRetrievalProfile.from_dict(
        json.loads(config.provider.profile_path.read_bytes())
    )
    if (
        profile.repository != config.provider.repository_name
        or profile.product_name != config.provider.product_name
        or profile.decision_space_id != config.provider.decision_space_id
    ):
        raise ValueError("recall_demo_config_invalid")
    installed = load_installed_models(profile, config.provider.model_state_root)
    model_digest = hashlib.sha256(
        installed.install_manifest_path.read_bytes()
    ).hexdigest()
    private_key = Ed25519PrivateKey.from_private_bytes(
        config.publisher.signing_private_key_path.read_bytes()
    )
    public_key = Ed25519PublicKey.from_public_bytes(
        config.provider.trust_root_path.read_bytes()
    )
    try:
        public_key.verify(private_key.sign(_RECALL_DEMO_NONCE), _RECALL_DEMO_NONCE)
    except InvalidSignature:
        raise ValueError("recall_demo_config_invalid") from None
    product = ProductMetadata.from_dict(
        json.loads(
            (config.publisher.registry_product_root / "product.json").read_bytes()
        )
    )
    registry = ProductRegistry.from_dict(
        json.loads(
            (config.publisher.registry_product_root / "registry.json").read_bytes()
        )
    )
    if (
        product.product_id != config.provider.decision_space_id
        or product.name != config.provider.product_name
        or registry.product_id != product.product_id
    ):
        raise ValueError("recall_demo_config_invalid")
    return profile.digest, model_digest


def _prepare_bundle_state_root(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("recall_demo_config_invalid")
    path.chmod(0o700)


def _current_recall_demo_generation(
    bundle_state_root: Path,
) -> tuple[int | None, str | None]:
    path = bundle_state_root / "current.json"
    if not path.exists():
        return None, None
    if path.is_symlink():
        raise ValueError("recall_demo_config_invalid")
    value = json.loads(path.read_bytes())
    if not isinstance(value, Mapping):
        raise ValueError("recall_demo_config_invalid")
    generation = value.get("generation")
    digest = value.get("generation_digest")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
    ):
        raise ValueError("recall_demo_config_invalid")
    return generation, digest[:12]


def _write_recall_demo_status(
    status: str,
    profile_digest: str | None,
    model_digest: str | None,
    generation: int | None = None,
    current_digest: str | None = None,
) -> None:
    sys.stdout.buffer.write(
        canonical_json_bytes(
            {
                "status": status,
                "profile_digest_prefix": (
                    profile_digest[:12] if profile_digest is not None else None
                ),
                "model_install_digest_prefix": (
                    model_digest[:12] if model_digest is not None else None
                ),
                "current_generation": generation,
                "current_digest_prefix": current_digest,
            }
        )
    )


def _write_recall_demo_error() -> None:
    sys.stderr.buffer.write(b'{"error":"recall_demo_config_invalid"}')


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
