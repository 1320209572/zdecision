"""Explicit ownership disposition for ambiguous legacy repository Candidates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from zdecision.central.decision_spaces import LeafDecisionSpace, RepositoryDecisionRoute
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import CandidateOwnershipSnapshot


MigrationPolicy = Literal["trusted_root_backfill", "archive_and_recapture"]


@dataclass(frozen=True)
class LegacyCandidateMigrationReport:
    archived_family_count: int
    backfilled_family_count: int


def migrate_legacy_repository_candidates(
    connection: sqlite3.Connection,
    organization_id: str,
    repository_id: str,
    *,
    policy: MigrationPolicy,
    root_route: RepositoryDecisionRoute | None,
    archived_at: datetime,
) -> LegacyCandidateMigrationReport:
    """Archive ambiguity or backfill only an explicitly trusted root leaf."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection")
    if not isinstance(organization_id, str) or not organization_id:
        raise ValueError("organization_id is invalid")
    if not isinstance(repository_id, str) or not repository_id:
        raise ValueError("repository_id is invalid")
    if policy not in ("trusted_root_backfill", "archive_and_recapture"):
        raise ValueError("migration_policy_invalid")
    if (
        not isinstance(archived_at, datetime)
        or archived_at.tzinfo is None
        or archived_at.utcoffset() is None
    ):
        raise ValueError("archived_at is invalid")
    timestamp = archived_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    family_rows = connection.execute(
        """SELECT DISTINCT family_id FROM candidate_revisions
        WHERE organization_id = ? AND repository_id = ? ORDER BY family_id""",
        (organization_id, repository_id),
    ).fetchall()
    families = tuple(row["family_id"] for row in family_rows)

    if policy == "archive_and_recapture":
        if root_route is not None:
            raise ValueError("archive_policy_does_not_accept_root_route")
        with connection:
            connection.executemany(
                """INSERT OR IGNORE INTO candidate_family_archives(
                organization_id, repository_id, family_id, reason, archived_at
                ) VALUES (?, ?, ?, 'ambiguous_legacy_repository_ownership', ?)""",
                [(organization_id, repository_id, family_id, timestamp) for family_id in families],
            )
        return LegacyCandidateMigrationReport(len(families), 0)

    if (
        not isinstance(root_route, RepositoryDecisionRoute)
        or root_route.repository_id != repository_id
        or root_route.path_prefixes != (".",)
        or root_route.excluded_prefixes
        or not root_route.enabled
    ):
        raise ValueError("trusted_root_route_required")
    matching_routes = connection.execute(
        """SELECT version.route_id, version.configuration_version,
        version.decision_space_id, version.record_digest
        FROM repository_route_heads AS head
        JOIN repository_route_versions AS version
          ON version.organization_id = head.organization_id
         AND version.route_id = head.route_id
         AND version.configuration_version = head.configuration_version
        WHERE head.organization_id = ? AND version.repository_id = ?
          AND version.enabled = 1 ORDER BY version.route_id""",
        (organization_id, repository_id),
    ).fetchall()
    if len(matching_routes) != 1 or (
        matching_routes[0]["route_id"] != root_route.route_id
        or matching_routes[0]["configuration_version"]
        != root_route.configuration_version
        or matching_routes[0]["decision_space_id"]
        != root_route.decision_space_id
        or matching_routes[0]["record_digest"]
        != hashlib.sha256(canonical_json_bytes(root_route.to_dict())).hexdigest()
    ):
        raise ValueError("trusted_root_route_required")
    space_row = connection.execute(
        """SELECT * FROM decision_spaces WHERE organization_id = ?
        AND decision_space_id = ? AND kind = 'product' AND enabled = 1""",
        (organization_id, root_route.decision_space_id),
    ).fetchone()
    if space_row is None:
        raise ValueError("trusted_root_route_required")
    space = LeafDecisionSpace(
        decision_space_id=space_row["decision_space_id"],
        kind=space_row["kind"],
        display_name=space_row["display_name"],
        compatibility_product_id=space_row["compatibility_product_id"],
        compatibility_product_name=space_row["compatibility_product_name"],
        catalog_group_id=space_row["catalog_group_id"],
        catalog_breadcrumb=tuple(json.loads(space_row["catalog_breadcrumb_json"])),
        source_root=space_row["source_root"],
        package_name=space_row["package_name"],
        asset_type=space_row["asset_type"],
        enabled=bool(space_row["enabled"]),
    )
    boundary_digest = hashlib.sha256(
        canonical_json_bytes({"trusted_root_route": root_route.to_dict()})
    ).hexdigest()
    ownership = CandidateOwnershipSnapshot(
        repository_id=repository_id,
        route_id=root_route.route_id,
        route_configuration_version=root_route.configuration_version,
        decision_space_id=space.decision_space_id,
        decision_space_kind=space.kind,
        display_name=space.display_name,
        catalog_breadcrumb=space.catalog_breadcrumb,
        source_root=space.source_root,
        compatibility_product_id=space.compatibility_product_id,
        compatibility_product_name=space.compatibility_product_name,
        source_boundary_digest=boundary_digest,
    )
    ownership_json = canonical_json_bytes(ownership.to_dict()).decode("utf-8")
    ownership_digest = hashlib.sha256(ownership_json.encode("utf-8")).hexdigest()
    revisions = connection.execute(
        """SELECT family_id, revision FROM candidate_revisions
        WHERE organization_id = ? AND repository_id = ?
        ORDER BY family_id, revision""",
        (organization_id, repository_id),
    ).fetchall()
    with connection:
        connection.executemany(
            """INSERT OR IGNORE INTO candidate_revision_ownership(
            organization_id, repository_id, family_id, revision,
            decision_space_id, route_id, route_configuration_version,
            ownership_json, ownership_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (organization_id, repository_id, row["family_id"], row["revision"],
                 ownership.decision_space_id, ownership.route_id,
                 ownership.route_configuration_version, ownership_json,
                 ownership_digest)
                for row in revisions
            ],
        )
        connection.execute(
            """UPDATE candidate_family_heads SET decision_space_id = ?
            WHERE organization_id = ? AND repository_id = ?
              AND decision_space_id = 'legacy_unassigned'""",
            (ownership.decision_space_id, organization_id, repository_id),
        )
    return LegacyCandidateMigrationReport(0, len(families))
