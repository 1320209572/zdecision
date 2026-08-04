"""CLI for the loopback-only ZDecision central technical demo."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import secrets
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from zdecision.agent.repository import RepositoryResolver
from zdecision.central.auth import DemoIdentityProvider, require_id, require_sha256
from zdecision.central.service import CaptureRequestService
from zdecision.central.store import CentralStore
from zdecision.central.web.application import CentralWebApplication
from zdecision.central.web.queries import CentralWebQueries
from zdecision.central.web.store import CentralWebStore
from zdecision.ids import canonical_product_name, product_id
from zdecision.jsonio import atomic_create_json
from zdecision.registry.git import GitRegistryAdapter
from zdecision.registry.query import RegistryQuery
from zdecision.sync.contracts import RepositoryView


_DEFAULT_CENTRAL_URL = "http://127.0.0.1:8765"


class CentralCliError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zdecision-central")
    commands = parser.add_subparsers(dest="command", required=True)

    demo_config = commands.add_parser(
        "demo-config", help="manage technical-demo configuration"
    )
    config_commands = demo_config.add_subparsers(
        dest="config_command", required=True
    )
    initialize = config_commands.add_parser(
        "init", help="create one central and local Agent configuration pair"
    )
    initialize.add_argument("--repository-cwd", required=True)
    initialize.add_argument("--product-name", required=True)
    initialize.add_argument("--output-dir", required=True)

    run = commands.add_parser("run", help="run the loopback central service")
    run.add_argument("--database", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--registry-repository-root", required=True)
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "demo-config":
            return _initialize_demo_config(arguments)
        return _run_server(arguments)
    except CentralCliError as error:
        sys.stderr.write(
            json.dumps(
                {"error": error.code},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return 1


def _initialize_demo_config(arguments: argparse.Namespace) -> int:
    repository_cwd = Path(arguments.repository_cwd).expanduser()
    if not repository_cwd.is_absolute():
        raise CentralCliError("repository_cwd_not_absolute")
    snapshot = RepositoryResolver(timeout_seconds=2.0).resolve(repository_cwd)
    if snapshot is None:
        raise CentralCliError("repository_not_resolved")
    try:
        product_name = canonical_product_name(arguments.product_name)
    except ValueError as error:
        raise CentralCliError("product_name_invalid") from error

    output_directory = Path(arguments.output_dir).expanduser()
    if not output_directory.is_absolute():
        raise CentralCliError("output_dir_not_absolute")
    if output_directory.exists():
        if not output_directory.is_dir():
            raise CentralCliError("output_dir_invalid")
        try:
            if next(output_directory.iterdir(), None) is not None:
                raise CentralCliError("output_dir_not_empty")
        except OSError as error:
            raise CentralCliError("output_dir_unreadable") from error
    else:
        try:
            output_directory.mkdir(mode=0o700, parents=True)
        except OSError as error:
            raise CentralCliError("output_dir_create_failed") from error

    organization_id = "org_demo"
    user_id = "user_demo"
    device_id = "device_demo"
    device_token = f"zdt_{secrets.token_urlsafe(32)}"
    device_token_digest = hashlib.sha256(
        device_token.encode("utf-8")
    ).hexdigest()
    repository = RepositoryView(
        repository_id=snapshot.repository_id,
        product_id=product_id(product_name),
        product_name=product_name,
        enabled=True,
    ).to_dict()
    central = {
        "organization_id": organization_id,
        "user_id": user_id,
        "device_id": device_id,
        "device_token_sha256": device_token_digest,
        "repositories": [repository],
    }
    agent = {
        "central_url": _DEFAULT_CENTRAL_URL,
        "organization_id": organization_id,
        "device_id": device_id,
        "device_token": device_token,
        "repositories": [repository],
    }
    central_path = output_directory / "central.json"
    agent_path = output_directory / "agent.json"
    created: list[Path] = []
    try:
        if not atomic_create_json(central_path, central):
            raise CentralCliError("config_already_exists")
        created.append(central_path)
        if not atomic_create_json(agent_path, agent):
            raise CentralCliError("config_already_exists")
        created.append(agent_path)
        os.chmod(central_path, 0o600)
        os.chmod(agent_path, 0o600)
    except (OSError, CentralCliError) as error:
        for path in created:
            path.unlink(missing_ok=True)
        if isinstance(error, CentralCliError):
            raise
        raise CentralCliError("config_write_failed") from error

    print(f"Created demo configuration in {output_directory}")
    return 0


def _run_server(arguments: argparse.Namespace) -> int:
    if not _is_loopback(arguments.host):
        raise CentralCliError("non_loopback_bind_forbidden")
    if not isinstance(arguments.port, int) or not 1 <= arguments.port <= 65535:
        raise CentralCliError("port_invalid")
    config_path = Path(arguments.config).expanduser()
    database_path = Path(arguments.database).expanduser()
    if not config_path.is_absolute() or not database_path.is_absolute():
        raise CentralCliError("server_path_not_absolute")
    registry_root = _registry_repository_root(
        arguments.registry_repository_root
    )
    config = _load_central_config(config_path)

    from zdecision.central.api import create_app

    store = CentralStore.open(database_path)
    try:
        for repository in config["repositories"]:
            store.put_repository_mapping(
                config["organization_id"],
                repository,
            )
        provider = DemoIdentityProvider(
            organization_id=config["organization_id"],
            user_id=config["user_id"],
            device_id=config["device_id"],
            device_token_sha256=config["device_token_sha256"],
        )
        git = GitRegistryAdapter(registry_root)
        registry_query = RegistryQuery(registry_root, git)
        web_application = CentralWebApplication(
            store=CentralWebStore(store.connection),
            queries=CentralWebQueries(store.connection, registry_query),
        )
        app = create_app(
            CaptureRequestService(store),
            provider,
            web_application=web_application,
        )
        import uvicorn

        uvicorn.run(
            app,
            host=arguments.host,
            port=arguments.port,
            access_log=True,
        )
    finally:
        store.close()
    return 0


def _registry_repository_root(value: str) -> Path:
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise CentralCliError("registry_repository_root_not_absolute")
    if not root.is_dir():
        raise CentralCliError("registry_repository_root_invalid")
    resolved = root.resolve()
    try:
        result = subprocess.run(
            ("git", "-C", str(resolved), "rev-parse", "--show-toplevel"),
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        raise CentralCliError("registry_repository_root_not_git") from None
    if result.returncode != 0:
        raise CentralCliError("registry_repository_root_not_git")
    try:
        top_level = Path(result.stdout.strip()).resolve()
    except (OSError, ValueError):
        raise CentralCliError("registry_repository_root_not_git") from None
    if top_level != resolved:
        raise CentralCliError("registry_repository_root_not_git")
    return resolved


def _load_central_config(path: Path) -> dict[str, object]:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise CentralCliError("central_config_permissions_invalid")
        value = json.loads(path.read_text("utf-8"))
    except CentralCliError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CentralCliError("central_config_invalid") from error
    expected = frozenset(
        (
            "organization_id",
            "user_id",
            "device_id",
            "device_token_sha256",
            "repositories",
        )
    )
    if not isinstance(value, Mapping) or frozenset(value) != expected:
        raise CentralCliError("central_config_invalid")
    try:
        organization_id = require_id(value["organization_id"], "organization_id")
        user_id = require_id(value["user_id"], "user_id")
        device_id = require_id(value["device_id"], "device_id")
        digest = require_sha256(
            value["device_token_sha256"], "device_token_sha256"
        )
        raw_repositories = value["repositories"]
        if (
            not isinstance(raw_repositories, list)
            or not 1 <= len(raw_repositories) <= 100
        ):
            raise ValueError("repositories are invalid")
        repositories = tuple(
            RepositoryView.from_dict(item) for item in raw_repositories
        )
    except (TypeError, ValueError) as error:
        raise CentralCliError("central_config_invalid") from error
    return {
        "organization_id": organization_id,
        "user_id": user_id,
        "device_id": device_id,
        "device_token_sha256": digest,
        "repositories": repositories,
    }


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
