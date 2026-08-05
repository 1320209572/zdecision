"""SQLite persistence owned by the central coordination service."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from zdecision.central.auth import require_id
from zdecision.central.decision_spaces import (
    CatalogGroup,
    EnabledRepository,
    LeafDecisionSpace,
    RepositoryCatalogView,
    RepositoryDecisionRoute,
)
from zdecision.central.web.schema import initialize_web_schema
from zdecision.ids import capture_request_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import RepositoryView
from zdecision.sync.contracts import CandidateOwnershipSnapshot


@dataclass(frozen=True)
class CaptureRequestRecord:
    request_id: str
    organization_id: str
    actor_id: str
    repository_id: str
    product_id: str
    product_name: str
    template_id: str
    capture_scope: str
    client_action_id: str
    state: str
    attempt_count: int
    claimed_device_id: str | None
    lease_token_digest: str | None
    lease_expires_at: str | None
    retry_at: str | None
    result_batch_digest: str | None
    result_candidate_count: int | None
    terminal_code: str | None
    last_sequence: int
    created_at: str
    updated_at: str


class CentralStore:
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self.connection = connection

    @classmethod
    def open(cls, path: Path) -> "CentralStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            database_path,
            timeout=5.0,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        with connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS repository_mappings (
                    organization_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    PRIMARY KEY(organization_id, repository_id)
                );

                CREATE TABLE IF NOT EXISTS catalog_groups (
                    organization_id TEXT NOT NULL,
                    catalog_group_id TEXT NOT NULL,
                    parent_group_id TEXT,
                    display_name TEXT NOT NULL,
                    breadcrumb_json TEXT NOT NULL,
                    source_prefix TEXT,
                    sort_order INTEGER NOT NULL,
                    PRIMARY KEY(organization_id, catalog_group_id)
                );

                CREATE TABLE IF NOT EXISTS decision_spaces (
                    organization_id TEXT NOT NULL,
                    decision_space_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('product','shared_unit')),
                    display_name TEXT NOT NULL,
                    compatibility_product_id TEXT NOT NULL,
                    compatibility_product_name TEXT NOT NULL,
                    catalog_group_id TEXT,
                    catalog_breadcrumb_json TEXT NOT NULL,
                    source_root TEXT NOT NULL,
                    package_name TEXT,
                    asset_type TEXT,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    PRIMARY KEY(organization_id, decision_space_id),
                    UNIQUE(organization_id, compatibility_product_id)
                );

                CREATE TABLE IF NOT EXISTS repositories (
                    organization_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    PRIMARY KEY(organization_id, repository_id)
                );

                CREATE TABLE IF NOT EXISTS repository_route_versions (
                    organization_id TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    configuration_version INTEGER NOT NULL CHECK(configuration_version > 0),
                    repository_id TEXT NOT NULL,
                    decision_space_id TEXT NOT NULL,
                    path_prefixes_json TEXT NOT NULL,
                    excluded_prefixes_json TEXT NOT NULL,
                    record_digest TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    PRIMARY KEY(organization_id, route_id, configuration_version)
                );

                CREATE TABLE IF NOT EXISTS repository_route_heads (
                    organization_id TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    configuration_version INTEGER NOT NULL,
                    PRIMARY KEY(organization_id, route_id)
                );

                CREATE TABLE IF NOT EXISTS capture_requests (
                    request_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    capture_scope TEXT NOT NULL CHECK(
                        capture_scope IN ('current_session', 'all_valid_sessions')
                    ),
                    client_action_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'queued','claimed','running','succeeded',
                        'succeeded_no_candidates','failed_retryable',
                        'failed_terminal','cancelled'
                    )),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    claimed_device_id TEXT,
                    lease_token_digest TEXT,
                    lease_expires_at TEXT,
                    retry_at TEXT,
                    result_batch_digest TEXT,
                    result_candidate_count INTEGER CHECK(
                        result_candidate_count IS NULL
                        OR result_candidate_count >= 0
                    ),
                    terminal_code TEXT,
                    last_sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS capture_groups (
                    request_id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    capture_scope TEXT NOT NULL,
                    client_action_id TEXT NOT NULL,
                    route_snapshot_json TEXT NOT NULL,
                    route_snapshot_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    claimed_device_id TEXT,
                    lease_token_digest TEXT,
                    lease_expires_at TEXT,
                    retry_at TEXT,
                    result_receipt_digest TEXT,
                    result_candidate_count INTEGER,
                    last_sequence INTEGER NOT NULL,
                    terminal_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS capture_group_actions (
                    organization_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    client_action_id TEXT NOT NULL,
                    request_id TEXT NOT NULL REFERENCES capture_groups(request_id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(organization_id, actor_id, client_action_id)
                );

                CREATE TABLE IF NOT EXISTS capture_group_events (
                    request_id TEXT NOT NULL REFERENCES capture_groups(request_id),
                    sequence INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    code TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY(request_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS capture_slices (
                    request_id TEXT NOT NULL REFERENCES capture_groups(request_id),
                    slice_id TEXT NOT NULL,
                    slice_order INTEGER NOT NULL,
                    route_id TEXT NOT NULL,
                    route_configuration_version INTEGER NOT NULL,
                    decision_space_id TEXT NOT NULL,
                    ownership_json TEXT NOT NULL,
                    ownership_digest TEXT NOT NULL,
                    matched_path_digest TEXT NOT NULL,
                    source_boundary_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    batch_json TEXT,
                    batch_record_digest TEXT,
                    receipt_json TEXT,
                    receipt_digest TEXT,
                    PRIMARY KEY(request_id, slice_id),
                    UNIQUE(request_id, route_id)
                );

                CREATE TABLE IF NOT EXISTS candidate_revision_ownership (
                    organization_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    decision_space_id TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    route_configuration_version INTEGER NOT NULL,
                    ownership_json TEXT NOT NULL,
                    ownership_digest TEXT NOT NULL,
                    PRIMARY KEY(organization_id, repository_id, family_id, revision)
                );

                CREATE TABLE IF NOT EXISTS candidate_family_archives (
                    organization_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    archived_at TEXT NOT NULL,
                    PRIMARY KEY(organization_id, repository_id, family_id)
                );

                CREATE TABLE IF NOT EXISTS capture_request_actions (
                    organization_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    client_action_id TEXT NOT NULL,
                    request_id TEXT NOT NULL
                        REFERENCES capture_requests(request_id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(
                        organization_id, actor_id, client_action_id
                    )
                );

                CREATE TABLE IF NOT EXISTS capture_request_events (
                    request_id TEXT NOT NULL
                        REFERENCES capture_requests(request_id),
                    sequence INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    code TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY(request_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS candidate_batches (
                    request_id TEXT PRIMARY KEY
                        REFERENCES capture_requests(request_id),
                    organization_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    batch_digest TEXT NOT NULL,
                    batch_json TEXT NOT NULL,
                    batch_record_digest TEXT NOT NULL,
                    item_count INTEGER NOT NULL CHECK(item_count >= 0),
                    receipt_json TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    acknowledged_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS candidate_revisions (
                    organization_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    revision_id TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    record_digest TEXT NOT NULL,
                    PRIMARY KEY(
                        organization_id, repository_id,
                        family_id, revision
                    ),
                    UNIQUE(organization_id, revision_id)
                );

                CREATE TABLE IF NOT EXISTS candidate_family_heads (
                    organization_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    decision_space_id TEXT NOT NULL DEFAULT 'legacy_unassigned',
                    family_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    revision_id TEXT NOT NULL,
                    PRIMARY KEY(
                        organization_id, repository_id, decision_space_id,
                        family_id
                    )
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    one_active_capture_per_repository
                ON capture_requests(organization_id, repository_id)
                WHERE state IN (
                    'queued','claimed','running','failed_retryable'
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    capture_event_sequence_once
                ON capture_request_events(request_id, sequence);

                CREATE INDEX IF NOT EXISTS capture_requests_claim_order
                ON capture_requests(
                    organization_id, state, retry_at, created_at, request_id
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    one_active_capture_group_per_repository
                ON capture_groups(organization_id, repository_id)
                WHERE state IN ('queued','claimed','running','failed_retryable');

                CREATE INDEX IF NOT EXISTS capture_groups_claim_order
                ON capture_groups(
                    organization_id, state, retry_at, created_at, request_id
                );
                """
            )
            _migrate_capture_requests(connection)
            _migrate_candidate_family_heads(connection)
            initialize_web_schema(connection)
        return cls(database_path, connection)

    def close(self) -> None:
        self.connection.close()

    def put_repository_mapping(
        self,
        organization_id: str,
        repository: RepositoryView,
    ) -> None:
        organization = require_id(organization_id, "organization_id")
        if not isinstance(repository, RepositoryView):
            raise TypeError("repository must be a RepositoryView")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO repository_mappings(
                    organization_id, repository_id, product_id,
                    product_name, enabled
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(organization_id, repository_id) DO UPDATE SET
                    product_id = excluded.product_id,
                    product_name = excluded.product_name,
                    enabled = excluded.enabled
                """,
                (
                    organization,
                    repository.repository_id,
                    repository.product_id,
                    repository.product_name,
                    int(repository.enabled),
                ),
            )

    def put_catalog_group(
        self, organization_id: str, group: CatalogGroup
    ) -> None:
        organization = require_id(organization_id, "organization_id")
        if not isinstance(group, CatalogGroup):
            raise TypeError("group must be a CatalogGroup")
        with self.connection:
            if group.parent_group_id is not None:
                parent = self.connection.execute(
                    """
                    SELECT breadcrumb_json FROM catalog_groups
                    WHERE organization_id = ? AND catalog_group_id = ?
                    """,
                    (organization, group.parent_group_id),
                ).fetchone()
                if parent is None:
                    raise ValueError("catalog_parent_not_found")
                if tuple(json.loads(parent["breadcrumb_json"])) + (
                    group.display_name,
                ) != group.breadcrumb:
                    raise ValueError("catalog_breadcrumb_invalid")
            self.connection.execute(
                """
                INSERT INTO catalog_groups(
                    organization_id, catalog_group_id, parent_group_id,
                    display_name, breadcrumb_json, source_prefix, sort_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(organization_id, catalog_group_id) DO UPDATE SET
                    parent_group_id = excluded.parent_group_id,
                    display_name = excluded.display_name,
                    breadcrumb_json = excluded.breadcrumb_json,
                    source_prefix = excluded.source_prefix,
                    sort_order = excluded.sort_order
                """,
                (
                    organization, group.catalog_group_id, group.parent_group_id,
                    group.display_name, _json_text(list(group.breadcrumb)),
                    group.source_prefix, group.sort_order,
                ),
            )

    def put_decision_space(
        self, organization_id: str, space: LeafDecisionSpace
    ) -> None:
        organization = require_id(organization_id, "organization_id")
        if not isinstance(space, LeafDecisionSpace):
            raise TypeError("space must be a LeafDecisionSpace")
        with self.connection:
            if space.catalog_group_id is not None:
                group = self.connection.execute(
                    """SELECT breadcrumb_json FROM catalog_groups
                    WHERE organization_id = ? AND catalog_group_id = ?""",
                    (organization, space.catalog_group_id),
                ).fetchone()
                if group is None:
                    raise ValueError("catalog_group_not_found")
                if tuple(json.loads(group["breadcrumb_json"])) != space.catalog_breadcrumb:
                    raise ValueError("catalog_breadcrumb_invalid")
            self.connection.execute(
                """
                INSERT INTO decision_spaces(
                    organization_id, decision_space_id, kind, display_name,
                    compatibility_product_id, compatibility_product_name,
                    catalog_group_id, catalog_breadcrumb_json, source_root,
                    package_name, asset_type, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(organization_id, decision_space_id) DO UPDATE SET
                    kind = excluded.kind, display_name = excluded.display_name,
                    compatibility_product_id = excluded.compatibility_product_id,
                    compatibility_product_name = excluded.compatibility_product_name,
                    catalog_group_id = excluded.catalog_group_id,
                    catalog_breadcrumb_json = excluded.catalog_breadcrumb_json,
                    source_root = excluded.source_root,
                    package_name = excluded.package_name,
                    asset_type = excluded.asset_type, enabled = excluded.enabled
                """,
                (
                    organization, space.decision_space_id, space.kind,
                    space.display_name, space.compatibility_product_id,
                    space.compatibility_product_name, space.catalog_group_id,
                    _json_text(list(space.catalog_breadcrumb)), space.source_root,
                    space.package_name, space.asset_type, int(space.enabled),
                ),
            )

    def put_repository(
        self, organization_id: str, repository: EnabledRepository
    ) -> None:
        organization = require_id(organization_id, "organization_id")
        if not isinstance(repository, EnabledRepository):
            raise TypeError("repository must be an EnabledRepository")
        with self.connection:
            self.connection.execute(
                """INSERT INTO repositories(organization_id, repository_id, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT(organization_id, repository_id) DO UPDATE SET
                    enabled = excluded.enabled""",
                (organization, repository.repository_id, int(repository.enabled)),
            )

    def put_route_version(
        self, organization_id: str, route: RepositoryDecisionRoute
    ) -> None:
        organization = require_id(organization_id, "organization_id")
        if not isinstance(route, RepositoryDecisionRoute):
            raise TypeError("route must be a RepositoryDecisionRoute")
        with self.connection:
            self._validate_route_target(organization, route)
            self._insert_route_version(organization, route)
            current = self._head_routes(organization, route.repository_id)
            previous = next(
                (item for item in current if item.route_id == route.route_id), None
            )
            if previous is None or route.configuration_version > previous.configuration_version:
                current = tuple(
                    route if item.route_id == route.route_id else item
                    for item in current
                )
                if previous is None:
                    current += (route,)
                _validate_route_set(
                    current, self._spaces_for_ids(organization, current)
                )
                self.connection.execute(
                    """INSERT INTO repository_route_heads(
                        organization_id, route_id, configuration_version
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(organization_id, route_id) DO UPDATE SET
                        configuration_version = excluded.configuration_version""",
                    (organization, route.route_id, route.configuration_version),
                )

    def replace_trusted_route_heads(
        self,
        organization_id: str,
        repository_id: str,
        routes: tuple[RepositoryDecisionRoute, ...],
    ) -> None:
        organization = require_id(organization_id, "organization_id")
        if not isinstance(routes, tuple) or not routes:
            raise ValueError("routes are invalid")
        if any(
            not isinstance(route, RepositoryDecisionRoute)
            or route.repository_id != repository_id
            for route in routes
        ):
            raise ValueError("routes are invalid")
        if len({route.route_id for route in routes}) != len(routes):
            raise ValueError("routes contain duplicates")
        with self.connection:
            repository = self.connection.execute(
                """SELECT enabled FROM repositories
                WHERE organization_id = ? AND repository_id = ?""",
                (organization, repository_id),
            ).fetchone()
            if repository is None:
                raise ValueError("repository_unavailable")
            for route in routes:
                self._validate_route_target(organization, route)
            _validate_route_set(routes, self._spaces_for_ids(organization, routes))
            for route in routes:
                self._insert_route_version(organization, route)
            self.connection.execute(
                """DELETE FROM repository_route_heads WHERE organization_id = ?
                AND route_id IN (
                    SELECT route_id FROM repository_route_versions
                    WHERE organization_id = ? AND repository_id = ?
                )""",
                (organization, organization, repository_id),
            )
            self.connection.executemany(
                """INSERT INTO repository_route_heads(
                    organization_id, route_id, configuration_version
                ) VALUES (?, ?, ?)""",
                [(organization, route.route_id, route.configuration_version) for route in routes],
            )

    def route_history(
        self, organization_id: str, route_id: str
    ) -> tuple[RepositoryDecisionRoute, ...]:
        organization = require_id(organization_id, "organization_id")
        rows = self.connection.execute(
            """SELECT * FROM repository_route_versions
            WHERE organization_id = ? AND route_id = ?
            ORDER BY configuration_version""",
            (organization, route_id),
        ).fetchall()
        return tuple(_route_from_row(row) for row in rows)

    def repository_catalog(
        self, organization_id: str, repository_id: str
    ) -> RepositoryCatalogView:
        organization = require_id(organization_id, "organization_id")
        repository = self.connection.execute(
            """SELECT enabled FROM repositories
            WHERE organization_id = ? AND repository_id = ?""",
            (organization, repository_id),
        ).fetchone()
        if repository is None or not bool(repository["enabled"]):
            raise ValueError("repository_unavailable")
        routes = tuple(
            route for route in self._head_routes(organization, repository_id)
            if route.enabled
        )
        spaces = self._spaces_for_ids(organization, routes)
        if any(not space.enabled for space in spaces.values()):
            raise ValueError("route_target_disabled")
        _validate_route_set(routes, spaces)
        ordered_spaces = tuple(
            sorted(spaces.values(), key=lambda item: (item.display_name, item.decision_space_id))
        )
        groups = self._catalog_groups(organization)
        _validate_catalog_cycles(groups)
        _validate_space_catalog_breadcrumbs(spaces.values(), groups)
        shared_tree = next(
            (group for group in groups.values() if group.parent_group_id is None and group.display_name == "Shared"),
            None,
        )
        return RepositoryCatalogView(
            repository_id, True, ordered_spaces,
            tuple(sorted(routes, key=lambda item: item.route_id)), shared_tree,
        )

    def list_enabled_routes(
        self, organization_id: str, repository_id: str
    ) -> tuple[RepositoryDecisionRoute, ...]:
        """Return the validated current leaf route heads for a repository."""

        return self.repository_catalog(organization_id, repository_id).routes

    def decision_space(
        self, organization_id: str, decision_space_id: str
    ) -> LeafDecisionSpace:
        organization = require_id(organization_id, "organization_id")
        row = self.connection.execute(
            """SELECT * FROM decision_spaces
            WHERE organization_id = ? AND decision_space_id = ?""",
            (organization, decision_space_id),
        ).fetchone()
        if row is None:
            raise ValueError("decision_space_not_found")
        return _space_from_row(row)

    def candidate_ownership(
        self,
        organization_id: str,
        repository_id: str,
        family_id: str,
        revision: int,
    ) -> CandidateOwnershipSnapshot:
        organization = require_id(organization_id, "organization_id")
        row = self.connection.execute(
            """SELECT ownership_json, ownership_digest
            FROM candidate_revision_ownership
            WHERE organization_id = ? AND repository_id = ?
              AND family_id = ? AND revision = ?""",
            (organization, repository_id, family_id, revision),
        ).fetchone()
        if row is None:
            raise ValueError("candidate_ownership_not_found")
        payload = row["ownership_json"]
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != row["ownership_digest"]:
            raise ValueError("candidate_ownership_corrupt")
        return CandidateOwnershipSnapshot.from_dict(json.loads(payload))

    def _validate_route_target(
        self, organization: str, route: RepositoryDecisionRoute
    ) -> None:
        repository = self.connection.execute(
            """SELECT 1 FROM repositories
            WHERE organization_id = ? AND repository_id = ?""",
            (organization, route.repository_id),
        ).fetchone()
        if repository is None:
            raise ValueError("repository_unavailable")
        row = self.connection.execute(
            """SELECT enabled FROM decision_spaces
            WHERE organization_id = ? AND decision_space_id = ?""",
            (organization, route.decision_space_id),
        ).fetchone()
        if row is None:
            raise ValueError("route_target_must_be_leaf")
        if not bool(row["enabled"]):
            raise ValueError("route_target_disabled")

    def _insert_route_version(
        self, organization: str, route: RepositoryDecisionRoute
    ) -> None:
        record = route.to_dict()
        digest = hashlib.sha256(canonical_json_bytes(record)).hexdigest()
        existing = self.connection.execute(
            """SELECT record_digest FROM repository_route_versions
            WHERE organization_id = ? AND route_id = ? AND configuration_version = ?""",
            (organization, route.route_id, route.configuration_version),
        ).fetchone()
        if existing is not None:
            if existing["record_digest"] != digest:
                raise ValueError("route_version_conflict")
            return
        self.connection.execute(
            """INSERT INTO repository_route_versions(
                organization_id, route_id, configuration_version, repository_id,
                decision_space_id, path_prefixes_json, excluded_prefixes_json,
                record_digest, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (organization, route.route_id, route.configuration_version,
             route.repository_id, route.decision_space_id,
             _json_text(list(route.path_prefixes)),
             _json_text(list(route.excluded_prefixes)), digest, int(route.enabled)),
        )

    def _head_routes(
        self, organization: str, repository_id: str
    ) -> tuple[RepositoryDecisionRoute, ...]:
        rows = self.connection.execute(
            """SELECT version.* FROM repository_route_heads AS head
            JOIN repository_route_versions AS version
              ON version.organization_id = head.organization_id
             AND version.route_id = head.route_id
             AND version.configuration_version = head.configuration_version
            WHERE head.organization_id = ? AND version.repository_id = ?
            ORDER BY version.route_id""",
            (organization, repository_id),
        ).fetchall()
        return tuple(_route_from_row(row) for row in rows)

    def _spaces_for_ids(
        self, organization: str, routes: tuple[RepositoryDecisionRoute, ...]
    ) -> dict[str, LeafDecisionSpace]:
        identifiers = sorted({route.decision_space_id for route in routes})
        if not identifiers:
            return {}
        marks = ",".join("?" for _ in identifiers)
        rows = self.connection.execute(
            f"SELECT * FROM decision_spaces WHERE organization_id = ? AND decision_space_id IN ({marks})",
            (organization, *identifiers),
        ).fetchall()
        spaces = {row["decision_space_id"]: _space_from_row(row) for row in rows}
        if len(spaces) != len(identifiers):
            raise ValueError("route_target_must_be_leaf")
        return spaces

    def _catalog_groups(self, organization: str) -> dict[str, CatalogGroup]:
        rows = self.connection.execute(
            "SELECT * FROM catalog_groups WHERE organization_id = ?",
            (organization,),
        ).fetchall()
        return {row["catalog_group_id"]: _group_from_row(row) for row in rows}

    def get_request_record(
        self, request_id: str
    ) -> CaptureRequestRecord | None:
        row = self.connection.execute(
            "SELECT * FROM capture_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is not None:
            return request_record_from_row(row)
        group = self.connection.execute(
            "SELECT * FROM capture_groups WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if group is None:
            return None
        mapping = self.connection.execute(
            """SELECT product_id, product_name FROM repository_mappings
            WHERE organization_id = ? AND repository_id = ?""",
            (group["organization_id"], group["repository_id"]),
        ).fetchone()
        return CaptureRequestRecord(
            request_id=group["request_id"],
            organization_id=group["organization_id"],
            actor_id=group["actor_id"],
            repository_id=group["repository_id"],
            product_id=mapping["product_id"],
            product_name=mapping["product_name"],
            template_id=group["template_id"],
            capture_scope=group["capture_scope"],
            client_action_id=group["client_action_id"],
            state=group["state"],
            attempt_count=group["attempt_count"],
            claimed_device_id=group["claimed_device_id"],
            lease_token_digest=group["lease_token_digest"],
            lease_expires_at=group["lease_expires_at"],
            retry_at=group["retry_at"],
            result_batch_digest=group["result_receipt_digest"],
            result_candidate_count=group["result_candidate_count"],
            terminal_code=group["terminal_code"],
            last_sequence=group["last_sequence"],
            created_at=group["created_at"],
            updated_at=group["updated_at"],
        )


def request_record_from_row(row: sqlite3.Row) -> CaptureRequestRecord:
    return CaptureRequestRecord(
        request_id=row["request_id"],
        organization_id=row["organization_id"],
        actor_id=row["actor_id"],
        repository_id=row["repository_id"],
        product_id=row["product_id"],
        product_name=row["product_name"],
        template_id=row["template_id"],
        capture_scope=row["capture_scope"],
        client_action_id=row["client_action_id"],
        state=row["state"],
        attempt_count=row["attempt_count"],
        claimed_device_id=row["claimed_device_id"],
        lease_token_digest=row["lease_token_digest"],
        lease_expires_at=row["lease_expires_at"],
        retry_at=row["retry_at"],
        result_batch_digest=row["result_batch_digest"],
        result_candidate_count=row["result_candidate_count"],
        terminal_code=row["terminal_code"],
        last_sequence=row["last_sequence"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _json_text(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8").rstrip("\n")


def _group_from_row(row: sqlite3.Row) -> CatalogGroup:
    return CatalogGroup(
        catalog_group_id=row["catalog_group_id"],
        parent_group_id=row["parent_group_id"],
        display_name=row["display_name"],
        breadcrumb=tuple(json.loads(row["breadcrumb_json"])),
        source_prefix=row["source_prefix"],
        sort_order=row["sort_order"],
    )


def _space_from_row(row: sqlite3.Row) -> LeafDecisionSpace:
    return LeafDecisionSpace(
        decision_space_id=row["decision_space_id"],
        kind=row["kind"],
        display_name=row["display_name"],
        compatibility_product_id=row["compatibility_product_id"],
        compatibility_product_name=row["compatibility_product_name"],
        catalog_group_id=row["catalog_group_id"],
        catalog_breadcrumb=tuple(json.loads(row["catalog_breadcrumb_json"])),
        source_root=row["source_root"],
        package_name=row["package_name"],
        asset_type=row["asset_type"],
        enabled=bool(row["enabled"]),
    )


def _route_from_row(row: sqlite3.Row) -> RepositoryDecisionRoute:
    record = {
        "route_id": row["route_id"],
        "repository_id": row["repository_id"],
        "decision_space_id": row["decision_space_id"],
        "path_prefixes": json.loads(row["path_prefixes_json"]),
        "excluded_prefixes": json.loads(row["excluded_prefixes_json"]),
        "enabled": bool(row["enabled"]),
        "configuration_version": row["configuration_version"],
    }
    digest = hashlib.sha256(canonical_json_bytes(record)).hexdigest()
    if row["record_digest"] != digest:
        raise ValueError("route_head_record_corrupt")
    return RepositoryDecisionRoute.from_dict(record)


def _validate_catalog_cycles(groups: dict[str, CatalogGroup]) -> None:
    for group in groups.values():
        seen: set[str] = set()
        current = group
        while current.parent_group_id is not None:
            if current.catalog_group_id in seen:
                raise ValueError("catalog_cycle")
            seen.add(current.catalog_group_id)
            parent = groups.get(current.parent_group_id)
            if parent is None:
                raise ValueError("catalog_parent_not_found")
            current = parent


def _validate_space_catalog_breadcrumbs(
    spaces: object,
    groups: dict[str, CatalogGroup],
) -> None:
    for space in spaces:
        if not isinstance(space, LeafDecisionSpace):
            raise TypeError("spaces must contain LeafDecisionSpace values")
        if space.catalog_group_id is None:
            continue
        group = groups.get(space.catalog_group_id)
        if group is None or group.breadcrumb != space.catalog_breadcrumb:
            raise ValueError("catalog_breadcrumb_invalid")


def _validate_route_set(
    routes: tuple[RepositoryDecisionRoute, ...],
    spaces: dict[str, LeafDecisionSpace],
) -> None:
    enabled = tuple(route for route in routes if route.enabled)
    root_routes = tuple(route for route in enabled if "." in route.path_prefixes)
    if root_routes and (
        len(enabled) != 1
        or spaces[root_routes[0].decision_space_id].kind != "product"
    ):
        raise ValueError("root_route_requires_single_product_leaf")
    for index, route in enumerate(enabled):
        for other in enabled[index + 1 :]:
            if route.decision_space_id == other.decision_space_id:
                continue
            if _routes_overlap(route, other):
                raise ValueError("route_targets_overlap")


def _routes_overlap(
    first: RepositoryDecisionRoute, second: RepositoryDecisionRoute
) -> bool:
    for left in first.path_prefixes:
        for right in second.path_prefixes:
            if left == ".":
                overlap = right
            elif right == ".":
                overlap = left
            elif left == right or left.startswith(right + "/"):
                overlap = left
            elif right.startswith(left + "/"):
                overlap = right
            else:
                continue
            if not _fully_excluded(overlap, first.excluded_prefixes) and not _fully_excluded(
                overlap, second.excluded_prefixes
            ):
                return True
    return False


def _fully_excluded(prefix: str, exclusions: tuple[str, ...]) -> bool:
    return any(
        excluded == prefix or prefix.startswith(excluded + "/")
        for excluded in exclusions
    )


def _migrate_capture_requests(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(capture_requests)"
        ).fetchall()
    }
    if "capture_scope" not in columns:
        connection.execute(
            """
            ALTER TABLE capture_requests ADD COLUMN capture_scope TEXT
            NOT NULL DEFAULT 'all_valid_sessions' CHECK(
                capture_scope IN ('current_session', 'all_valid_sessions')
            )
            """
        )
    if "client_action_id" not in columns:
        connection.execute(
            "ALTER TABLE capture_requests ADD COLUMN client_action_id TEXT"
        )
    rows = connection.execute(
        """
        SELECT request_id, organization_id, repository_id, template_id
        FROM capture_requests WHERE client_action_id IS NULL
        """
    ).fetchall()
    for row in rows:
        actions = connection.execute(
            """
            SELECT client_action_id FROM capture_request_actions
            WHERE request_id = ?
            """,
            (row["request_id"],),
        ).fetchall()
        matching = {
            action["client_action_id"] for action in actions
            if capture_request_id(
                row["organization_id"], row["repository_id"],
                row["template_id"], action["client_action_id"],
            ) == row["request_id"]
        }
        if len(matching) != 1:
            raise ValueError("capture_request_original_action_unrecoverable")
        connection.execute(
            """
            UPDATE capture_requests SET client_action_id = ?
            WHERE request_id = ?
            """,
            (matching.pop(), row["request_id"]),
        )
    if "result_candidate_count" not in columns:
        connection.execute(
            """
            ALTER TABLE capture_requests ADD COLUMN result_candidate_count
            INTEGER CHECK(
                result_candidate_count IS NULL
                OR result_candidate_count >= 0
            )
            """
        )
        connection.execute(
            """
            UPDATE capture_requests
            SET result_candidate_count = (
                SELECT item_count FROM candidate_batches
                WHERE candidate_batches.request_id = capture_requests.request_id
            )
            WHERE state IN ('succeeded', 'succeeded_no_candidates')
            """
        )


def _migrate_candidate_family_heads(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(candidate_family_heads)"
        ).fetchall()
    }
    if "decision_space_id" in columns:
        return
    connection.executescript(
        """
        ALTER TABLE candidate_family_heads RENAME TO legacy_candidate_family_heads;
        CREATE TABLE candidate_family_heads (
            organization_id TEXT NOT NULL,
            repository_id TEXT NOT NULL,
            decision_space_id TEXT NOT NULL DEFAULT 'legacy_unassigned',
            family_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision > 0),
            revision_id TEXT NOT NULL,
            PRIMARY KEY(
                organization_id, repository_id, decision_space_id, family_id
            )
        );
        INSERT INTO candidate_family_heads(
            organization_id, repository_id, family_id, revision, revision_id
        ) SELECT organization_id, repository_id, family_id, revision, revision_id
          FROM legacy_candidate_family_heads;
        DROP TABLE legacy_candidate_family_heads;
        """
    )
