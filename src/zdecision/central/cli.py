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
from zdecision.central.decision_spaces import (
    CatalogGroup,
    EnabledRepository,
    LeafDecisionSpace,
    RepositoryDecisionRoute,
)
from zdecision.central.service import CaptureRequestService
from zdecision.central.store import CentralStore, _validate_route_set
from zdecision.central.web.application import CentralWebApplication
from zdecision.central.web.queries import CentralWebQueries
from zdecision.central.web.store import CentralWebStore
from zdecision.ids import (
    canonical_product_name,
    catalog_group_id,
    decision_space_id,
    product_id,
    repository_route_id,
)
from zdecision.jsonio import atomic_create_json
from zdecision.registry.git import GitRegistryAdapter
from zdecision.registry.catalog import RegistryCatalog
from zdecision.registry.query import RegistryQuery


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
    initialize.add_argument("--product-name")
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
    if arguments.product_name is not None:
        try:
            canonical_product_name(arguments.product_name)
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
    catalog_groups, spaces, routes = _demo_catalog(snapshot.repository_id)
    repository = EnabledRepository(snapshot.repository_id, True).to_dict()
    central = {
        "organization_id": organization_id,
        "user_id": user_id,
        "device_id": device_id,
        "device_token_sha256": device_token_digest,
        "repositories": [repository],
        "catalog_groups": [item.to_dict() for item in catalog_groups],
        "decision_spaces": [item.to_dict() for item in spaces],
        "repository_routes": [item.to_dict() for item in routes],
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
            store.put_repository(config["organization_id"], repository)
        for group in config["catalog_groups"]:
            store.put_catalog_group(config["organization_id"], group)
        for space in config["decision_spaces"]:
            store.put_decision_space(config["organization_id"], space)
        for repository in config["repositories"]:
            routes = tuple(
                route for route in config["repository_routes"]
                if route.repository_id == repository.repository_id
            )
            store.replace_trusted_route_heads(
                config["organization_id"], repository.repository_id, routes
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
            catalog=RegistryCatalog(registry_root),
            git=git,
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
            "catalog_groups",
            "decision_spaces",
            "repository_routes",
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
        repositories = tuple(EnabledRepository.from_dict(item) for item in raw_repositories)
        raw_groups = value["catalog_groups"]
        raw_spaces = value["decision_spaces"]
        raw_routes = value["repository_routes"]
        if not isinstance(raw_groups, list) or not isinstance(raw_spaces, list) or not isinstance(raw_routes, list):
            raise ValueError("catalog is invalid")
        groups = tuple(CatalogGroup.from_dict(item) for item in raw_groups)
        spaces = tuple(LeafDecisionSpace.from_dict(item) for item in raw_spaces)
        routes = tuple(RepositoryDecisionRoute.from_dict(item) for item in raw_routes)
        _validate_trusted_catalog(repositories, groups, spaces, routes)
    except (TypeError, ValueError) as error:
        raise CentralCliError("central_config_invalid") from error
    return {
        "organization_id": organization_id,
        "user_id": user_id,
        "device_id": device_id,
        "device_token_sha256": digest,
        "repositories": repositories,
        "catalog_groups": groups,
        "decision_spaces": spaces,
        "repository_routes": routes,
    }


def _validate_trusted_catalog(
    repositories: tuple[EnabledRepository, ...],
    groups: tuple[CatalogGroup, ...],
    spaces: tuple[LeafDecisionSpace, ...],
    routes: tuple[RepositoryDecisionRoute, ...],
) -> None:
    if len({item.repository_id for item in repositories}) != len(repositories):
        raise ValueError("repositories contain duplicates")
    group_by_id = {item.catalog_group_id: item for item in groups}
    if len(group_by_id) != len(groups):
        raise ValueError("catalog groups contain duplicates")
    for group in groups:
        if group.parent_group_id is not None:
            parent = group_by_id.get(group.parent_group_id)
            if parent is None or parent.breadcrumb + (group.display_name,) != group.breadcrumb:
                raise ValueError("catalog parent is invalid")
        seen: set[str] = set()
        cursor = group
        while cursor.parent_group_id is not None:
            if cursor.catalog_group_id in seen:
                raise ValueError("catalog cycle")
            seen.add(cursor.catalog_group_id)
            cursor = group_by_id.get(cursor.parent_group_id)
            if cursor is None:
                raise ValueError("catalog parent is invalid")
    space_by_id = {item.decision_space_id: item for item in spaces}
    if len(space_by_id) != len(spaces) or len(
        {item.compatibility_product_id for item in spaces}
    ) != len(spaces):
        raise ValueError("decision spaces contain duplicates")
    for space in spaces:
        if space.catalog_group_id is not None:
            group = group_by_id.get(space.catalog_group_id)
            if group is None or group.breadcrumb != space.catalog_breadcrumb:
                raise ValueError("decision space catalog is invalid")
    repository_ids = {item.repository_id for item in repositories}
    if not routes or {item.repository_id for item in routes} != repository_ids:
        raise ValueError("routes do not cover repositories")
    route_ids = set()
    for route in routes:
        target = space_by_id.get(route.decision_space_id)
        if (
            route.route_id in route_ids
            or target is None
            or not target.enabled
        ):
            raise ValueError("route target is invalid")
        route_ids.add(route.route_id)
    for repository_id in repository_ids:
        repository_routes = tuple(
            item for item in routes
            if item.repository_id == repository_id
        )
        _validate_route_set(repository_routes, space_by_id)


def _demo_catalog(repository_id: str) -> tuple[
    tuple[CatalogGroup, ...],
    tuple[LeafDecisionSpace, ...],
    tuple[RepositoryDecisionRoute, ...],
]:
    shared = CatalogGroup(
        catalog_group_id=catalog_group_id(("Shared",)),
        parent_group_id=None,
        display_name="Shared",
        breadcrumb=("Shared",),
        source_prefix=None,
        sort_order=20,
    )
    shared_groups = tuple(
        CatalogGroup(
            catalog_group_id=catalog_group_id(("Shared", label)),
            parent_group_id=shared.catalog_group_id,
            display_name=label,
            breadcrumb=("Shared", label),
            source_prefix=prefix,
            sort_order=order,
        )
        for label, prefix, order in (
            ("packages/products/shared", "packages/products/shared", 10),
            ("packages/shared", "packages/shared", 20),
            ("packages", "packages", 30),
        )
    )
    product_specs = (
        "cloud",
        "idp",
        "lifecycle",
        "portal",
        "redis",
        "third-party-services",
        "zcf-installer",
        "ziam",
        "zmetis",
        "zns",
        "zstack-ai-studio",
        "zstone",
        "zsv",
    )
    product_spaces = tuple(
        LeafDecisionSpace(
            decision_space_id=decision_space_id("product", product_id(name)),
            kind="product",
            display_name=name,
            compatibility_product_id=product_id(name),
            compatibility_product_name=name,
            catalog_group_id=None,
            catalog_breadcrumb=(),
            source_root=f"packages/products/{name}",
            package_name=None,
            asset_type=None,
            enabled=True,
        )
        for name in product_specs
    )
    group_by_prefix = {group.source_prefix: group for group in shared_groups}
    shared_specs = (
        ("zcf-audit", "packages/products/shared/zcf-audit", "@zstack/zcf-audit", "cross_product_module"),
        ("zcf-license", "packages/products/shared/zcf-license", "@zstack/zcf-license", "cross_product_module"),
        ("design-x", "packages/shared/design-x", "@zstack/design-x", "library"),
        ("theme", "packages/shared/theme", "@zstack/theme", "library"),
        ("design", "packages/design", "@zstack/design", "component_library"),
        ("form", "packages/form", "@zstack/form", "library"),
        ("table", "packages/table", "@zstack/table", "component_library"),
        ("hooks", "packages/hooks", "@zstack/hooks", "library"),
        ("auth", "packages/auth", "@zstack/auth", "library"),
        ("i18n", "packages/i18n", "@zstack/i18n", "library"),
        ("utils", "packages/utils", "@zstack/utils", "library"),
        ("zephyr", "packages/zephyr", "@zstack/zephyr", "component_library"),
    )
    shared_spaces: list[LeafDecisionSpace] = []
    for name, source_root, package_name, asset_type in shared_specs:
        group = max(
            (candidate for prefix, candidate in group_by_prefix.items() if source_root.startswith(prefix + "/")),
            key=lambda candidate: len(candidate.source_prefix or ""),
        )
        compatibility_name = f"Shared / {source_root}"
        compatibility_id = product_id(compatibility_name)
        shared_spaces.append(
            LeafDecisionSpace(
                decision_space_id=decision_space_id("shared_unit", compatibility_id),
                kind="shared_unit",
                display_name=name,
                compatibility_product_id=compatibility_id,
                compatibility_product_name=compatibility_name,
                catalog_group_id=group.catalog_group_id,
                catalog_breadcrumb=group.breadcrumb,
                source_root=source_root,
                package_name=package_name,
                asset_type=asset_type,
                enabled=True,
            )
        )
    spaces = (*product_spaces, *shared_spaces)
    routes = tuple(
        RepositoryDecisionRoute(
            route_id=repository_route_id(repository_id, space.decision_space_id),
            repository_id=repository_id,
            decision_space_id=space.decision_space_id,
            path_prefixes=(space.source_root,),
            excluded_prefixes=(),
            enabled=True,
            configuration_version=1,
        )
        for space in spaces
    )
    return (shared, *shared_groups), spaces, routes


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
