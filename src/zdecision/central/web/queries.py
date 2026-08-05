"""Organization-scoped read models for the Central Decision Web."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from zdecision.central.auth import Principal
from zdecision.central.decision_spaces import LeafDecisionSpace
from zdecision.central.web.contracts import (
    CatalogNode,
    CandidateInboxItem,
    CandidateInboxView,
    CandidateReviewState,
    CentralPublication,
    DecisionSpaceRef,
    DecisionSpaceSummary,
    ReviewDraft,
)
from zdecision.central.web.store import WebRecordCorrupt
from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.publication import PublicationRecord
from zdecision.registry.models import DecisionRevision
from zdecision.registry.query import RegistryQueryUnavailable, RegistrySnapshot
from zdecision.sync.contracts import (
    CandidateOwnershipSnapshot,
    CandidateRevisionUpload,
    RepositoryView,
)


_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


@dataclass(frozen=True)
class ProductSummary:
    product_id: str
    product_name: str
    repository_ids: tuple[str, ...]
    pending_candidate_count: int
    active_decision_count: int | None
    last_activity_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "repository_ids": list(self.repository_ids),
            "pending_candidate_count": self.pending_candidate_count,
            "active_decision_count": self.active_decision_count,
            "last_activity_at": self.last_activity_at,
        }


@dataclass(frozen=True)
class DashboardMetrics:
    product_count: int
    pending_candidate_count: int
    active_decision_count: int | None
    completed_this_week: int

    def to_dict(self) -> dict[str, object]:
        return {
            "product_count": self.product_count,
            "pending_candidate_count": self.pending_candidate_count,
            "active_decision_count": self.active_decision_count,
            "completed_this_week": self.completed_this_week,
        }


@dataclass(frozen=True)
class RegistryStatus:
    state: Literal["available", "unavailable"]
    commit_sha: str | None

    def to_dict(self) -> dict[str, object]:
        return {"state": self.state, "commit_sha": self.commit_sha}


@dataclass(frozen=True)
class PublicationSummary:
    publication_id: str
    preview_id: str
    product_id: str
    product_name: str
    decision_count: int
    actor_id: str
    approved_at: str
    state: Literal[
        "confirmed", "committed_pending_push", "completed", "ambiguous"
    ]
    recovery_code: str | None
    commit_sha: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "preview_id": self.preview_id,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "decision_count": self.decision_count,
            "actor_id": self.actor_id,
            "approved_at": self.approved_at,
            "state": self.state,
            "recovery_code": self.recovery_code,
            "commit_sha": self.commit_sha,
        }


@dataclass(frozen=True)
class DashboardView:
    metrics: DashboardMetrics
    registry: RegistryStatus
    products: tuple[DecisionSpaceSummary, ...]
    shared_tree: CatalogNode | None
    recent_publications: tuple[PublicationSummary, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "metrics": self.metrics.to_dict(),
            "registry": self.registry.to_dict(),
            "products": [item.to_dict() for item in self.products],
            "shared_tree": (
                self.shared_tree.to_dict() if self.shared_tree is not None else None
            ),
            "recent_publications": [
                item.to_dict() for item in self.recent_publications
            ],
        }


class DecisionReadError(Exception):
    code = "decision_read_error"


class DecisionNotFound(DecisionReadError):
    code = "not_found"


class DecisionRegistryUnavailable(DecisionReadError):
    code = "registry_unavailable"


@dataclass(frozen=True)
class DecisionListItem:
    decision_space_id: str
    space: DecisionSpaceRef
    product_id: str
    product_name: str
    decision_id: str
    revision: int
    lifecycle: str
    claim: str
    future_action: str
    scope_summary: str
    repositories: tuple[str, ...]
    paths: tuple[str, ...]
    published_at: str | None
    publication_id: str | None
    commit_sha: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_space_id": self.decision_space_id,
            "space": self.space.to_dict(),
            "product_id": self.product_id,
            "product_name": self.product_name,
            "decision_id": self.decision_id,
            "revision": self.revision,
            "lifecycle": self.lifecycle,
            "claim": self.claim,
            "future_action": self.future_action,
            "scope_summary": self.scope_summary,
            "repositories": list(self.repositories),
            "paths": list(self.paths),
            "published_at": self.published_at,
            "publication_id": self.publication_id,
            "commit_sha": self.commit_sha,
        }

    def to_safe_dict(self) -> dict[str, object]:
        value = self.to_dict()
        value.pop("product_id")
        value.pop("product_name")
        return value


@dataclass(frozen=True)
class DecisionListView:
    registry_state: Literal["available", "unavailable"]
    registry_commit: str | None
    items: tuple[DecisionListItem, ...] | None
    total: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "registry_state": self.registry_state,
            "registry_commit": self.registry_commit,
            "items": (
                [item.to_dict() for item in self.items]
                if self.items is not None
                else None
            ),
            "total": self.total,
        }

    def to_safe_dict(self) -> dict[str, object]:
        value = self.to_dict()
        if self.items is not None:
            value["items"] = [item.to_safe_dict() for item in self.items]
        return value


@dataclass(frozen=True)
class DecisionDetailView:
    decision_space_id: str
    space: DecisionSpaceRef
    registry_commit: str
    decision: DecisionRevision
    publication_id: str | None
    published_at: str | None
    commit_sha: str | None

    def to_dict(self) -> dict[str, object]:
        canonical = canonical_json_bytes(self.decision.to_dict()).decode(
            "utf-8"
        )
        return {
            **self.decision.to_dict(),
            "decision_space_id": self.decision_space_id,
            "space": self.space.to_dict(),
            "canonical_json": canonical,
            "registry_commit": self.registry_commit,
            "publication_id": self.publication_id,
            "published_at": self.published_at,
            "commit_sha": self.commit_sha,
        }

    def to_safe_dict(self) -> dict[str, object]:
        value = self.to_dict()
        value.pop("product_id")
        value.pop("product_name")
        return value


@dataclass(frozen=True)
class _DecisionPublication:
    publication_id: str
    preview_id: str
    published_at: str
    commit_sha: str


@dataclass(frozen=True)
class RepositorySpacesView:
    repository_id: str
    spaces: tuple[DecisionSpaceSummary, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "spaces": [space.to_dict() for space in self.spaces],
        }


class CentralWebQueries:
    def __init__(self, connection: sqlite3.Connection, registry_query: object) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        if not callable(getattr(registry_query, "snapshot", None)):
            raise TypeError("registry_query must expose snapshot()")
        self.connection = connection
        self.registry_query = registry_query

    def list_products(self, principal: Principal) -> tuple[ProductSummary, ...]:
        self._require_user(principal)
        snapshot = self._registry_snapshot()
        return self._list_products(principal, snapshot)

    def list_decisions(
        self,
        principal: Principal,
        *,
        product_id: str | None = None,
        decision_space_id: str | None = None,
        search: str = "",
        repository: str = "",
        published_after: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> DecisionListView:
        self._require_user(principal)
        self._validate_decision_filters(
            search, repository, published_after, limit, offset
        )
        snapshot = self._registry_snapshot()
        if snapshot is None:
            return DecisionListView("unavailable", None, None, None)
        summaries = self._space_summaries(principal, snapshot)
        by_id = {space.decision_space_id: space for space in summaries}
        if decision_space_id is not None:
            selected = by_id.get(decision_space_id)
            if selected is None:
                raise DecisionNotFound("not_found")
            selected_spaces = (selected,)
        elif product_id is not None:
            leaf = self.decision_space(principal, product_id)
            selected = by_id.get(leaf.decision_space_id) if leaf is not None else None
            if selected is None or selected.kind != "product":
                raise DecisionNotFound("not_found")
            selected_spaces = (selected,)
        else:
            selected_spaces = summaries
        selected_products: dict[str, tuple[str, DecisionSpaceRef]] = {}
        for summary in selected_spaces:
            leaf = self.decision_space(principal, summary.decision_space_id)
            if leaf is None:
                raise DecisionNotFound("not_found")
            selected_products[leaf.compatibility_product_id] = (
                leaf.compatibility_product_name,
                self._space_ref(leaf),
            )
        publications = self._decision_publications(
            principal, frozenset(selected_products)
        )
        threshold = (
            self._rfc3339(published_after)
            if published_after is not None
            else None
        )
        folded_search = search.casefold()
        items: list[DecisionListItem] = []
        for revision in snapshot.active_decisions():
            mapped = selected_products.get(revision.product_id)
            if mapped is None or mapped[0] != revision.product_name:
                continue
            if folded_search and folded_search not in "\n".join(
                (
                    revision.claim,
                    revision.future_action,
                    revision.scope_summary,
                )
            ).casefold():
                continue
            if repository and repository not in revision.repositories:
                continue
            publication = publications.get(
                (
                    revision.product_id,
                    revision.decision_id,
                    revision.publication_preview_id,
                )
            )
            if threshold is not None and (
                publication is None
                or self._rfc3339(publication.published_at) < threshold
            ):
                continue
            items.append(
                self._decision_list_item(revision, publication, mapped[1])
            )
        items.sort(
            key=lambda item: (
                item.product_name.casefold(),
                item.decision_id,
            )
        )
        items.sort(key=lambda item: item.published_at or "", reverse=True)
        total = len(items)
        return DecisionListView(
            "available",
            snapshot.commit_sha,
            tuple(items[offset : offset + limit]),
            total,
        )

    def get_decision(
        self, principal: Principal, decision_space_id: str, decision_id: str
    ) -> DecisionDetailView:
        self._require_user(principal)
        space = self.decision_space(principal, decision_space_id)
        if space is None:
            raise DecisionNotFound("not_found")
        if decision_space_id.startswith("prod_") and space.kind != "product":
            raise DecisionNotFound("not_found")
        snapshot = self._registry_snapshot()
        if snapshot is None:
            raise DecisionRegistryUnavailable("registry_unavailable")
        revision = snapshot.decisions.get(
            (space.compatibility_product_id, decision_id)
        )
        if (
            revision is None
            or revision.lifecycle != "active"
            or revision.product_name != space.compatibility_product_name
        ):
            raise DecisionNotFound("not_found")
        publication = self._decision_publications(
            principal, frozenset((space.compatibility_product_id,))
        ).get((space.compatibility_product_id, decision_id, revision.publication_preview_id))
        return DecisionDetailView(
            decision_space_id=space.decision_space_id,
            space=self._space_ref(space),
            registry_commit=snapshot.commit_sha,
            decision=revision,
            publication_id=(
                publication.publication_id if publication is not None else None
            ),
            published_at=(
                publication.published_at if publication is not None else None
            ),
            commit_sha=(
                publication.commit_sha if publication is not None else None
            ),
        )

    def resolve_repository(
        self, principal: Principal, repository_id: str
    ) -> RepositoryView | None:
        self._require_user(principal)
        row = self.connection.execute(
            """
            SELECT repository_id, product_id, product_name, enabled
            FROM repository_mappings
            WHERE organization_id = ? AND repository_id = ? AND enabled = 1
            """,
            (principal.organization_id, repository_id),
        ).fetchone()
        if row is None:
            return None
        return RepositoryView(
            row["repository_id"],
            row["product_id"],
            row["product_name"],
            bool(row["enabled"]),
        )

    def product_repositories(
        self, principal: Principal, product_id: str
    ) -> tuple[RepositoryView, ...]:
        self._require_user(principal)
        rows = self.connection.execute(
            """
            SELECT repository_id, product_id, product_name, enabled
            FROM repository_mappings
            WHERE organization_id = ? AND product_id = ? AND enabled = 1
            ORDER BY repository_id
            """,
            (principal.organization_id, product_id),
        ).fetchall()
        return tuple(
            RepositoryView(
                row["repository_id"],
                row["product_id"],
                row["product_name"],
                bool(row["enabled"]),
            )
            for row in rows
        )

    def decision_space(
        self, principal: Principal, identifier: str
    ) -> LeafDecisionSpace | None:
        """Resolve an enabled leaf by canonical ID or a product-only V1 alias."""

        self._require_user(principal)
        row = self.connection.execute(
            """
            SELECT * FROM decision_spaces
            WHERE organization_id = ? AND enabled = 1
              AND (
                decision_space_id = ? OR
                (kind = 'product' AND compatibility_product_id = ?)
              )
            """,
            (principal.organization_id, identifier, identifier),
        ).fetchone()
        if row is None:
            return None
        return LeafDecisionSpace(
            decision_space_id=row["decision_space_id"],
            kind=row["kind"],
            display_name=row["display_name"],
            compatibility_product_id=row["compatibility_product_id"],
            compatibility_product_name=row["compatibility_product_name"],
            catalog_group_id=row["catalog_group_id"],
            catalog_breadcrumb=tuple(
                json.loads(row["catalog_breadcrumb_json"])
            ),
            source_root=row["source_root"],
            package_name=row["package_name"],
            asset_type=row["asset_type"],
            enabled=bool(row["enabled"]),
        )

    def decision_space_ref(
        self, principal: Principal, identifier: str
    ) -> DecisionSpaceRef | None:
        space = self.decision_space(principal, identifier)
        if space is None and identifier.startswith("dsp_"):
            self._require_user(principal)
            row = self.connection.execute(
                """SELECT * FROM decision_spaces
                WHERE organization_id = ? AND decision_space_id = ?""",
                (principal.organization_id, identifier),
            ).fetchone()
            if row is not None:
                space = LeafDecisionSpace(
                    decision_space_id=row["decision_space_id"],
                    kind=row["kind"],
                    display_name=row["display_name"],
                    compatibility_product_id=row["compatibility_product_id"],
                    compatibility_product_name=row[
                        "compatibility_product_name"
                    ],
                    catalog_group_id=row["catalog_group_id"],
                    catalog_breadcrumb=tuple(
                        json.loads(row["catalog_breadcrumb_json"])
                    ),
                    source_root=row["source_root"],
                    package_name=row["package_name"],
                    asset_type=row["asset_type"],
                    enabled=bool(row["enabled"]),
                )
        return self._space_ref(space) if space is not None else None

    def catalog_group_exists(
        self, principal: Principal, catalog_group_id: str
    ) -> bool:
        self._require_user(principal)
        return self.connection.execute(
            """SELECT 1 FROM catalog_groups
            WHERE organization_id = ? AND catalog_group_id = ?""",
            (principal.organization_id, catalog_group_id),
        ).fetchone() is not None

    def repository_spaces(
        self, principal: Principal, repository_id: str
    ) -> RepositorySpacesView:
        self._require_user(principal)
        enabled = self.connection.execute(
            """SELECT 1 FROM repositories
            WHERE organization_id = ? AND repository_id = ? AND enabled = 1
            UNION
            SELECT 1 FROM repository_mappings
            WHERE organization_id = ? AND repository_id = ? AND enabled = 1""",
            (
                principal.organization_id,
                repository_id,
                principal.organization_id,
                repository_id,
            ),
        ).fetchone()
        if enabled is None:
            raise DecisionNotFound("not_found")
        identifiers = {
            row["decision_space_id"]
            for row in self.connection.execute(
                """SELECT version.decision_space_id
                FROM repository_route_heads AS head
                JOIN repository_route_versions AS version
                  ON version.organization_id = head.organization_id
                 AND version.route_id = head.route_id
                 AND version.configuration_version = head.configuration_version
                JOIN decision_spaces AS space
                  ON space.organization_id = version.organization_id
                 AND space.decision_space_id = version.decision_space_id
                WHERE head.organization_id = ?
                  AND version.repository_id = ?
                  AND version.enabled = 1 AND space.enabled = 1""",
                (principal.organization_id, repository_id),
            ).fetchall()
        }
        mapping = self.resolve_repository(principal, repository_id)
        if mapping is not None:
            product = self.decision_space(principal, mapping.product_id)
            if product is not None and product.kind == "product":
                identifiers.add(product.decision_space_id)
        summaries = self._space_summaries(principal, self._registry_snapshot())
        return RepositorySpacesView(
            repository_id,
            tuple(
                summary
                for summary in summaries
                if summary.decision_space_id in identifiers
            ),
        )

    def decision_space_repositories(
        self, principal: Principal, decision_space_id: str
    ) -> tuple[RepositoryView, ...]:
        self._require_user(principal)
        space = self.decision_space(principal, decision_space_id)
        if space is None or space.decision_space_id != decision_space_id:
            return ()
        rows = self.connection.execute(
            """
            SELECT DISTINCT repository_id FROM (
              SELECT ownership.repository_id AS repository_id
              FROM candidate_revision_ownership AS ownership
              WHERE ownership.organization_id = ?
                AND ownership.decision_space_id = ?
              UNION
              SELECT version.repository_id AS repository_id
              FROM repository_route_heads AS head
              JOIN repository_route_versions AS version
                ON version.organization_id = head.organization_id
               AND version.route_id = head.route_id
               AND version.configuration_version = head.configuration_version
              WHERE head.organization_id = ?
                AND version.decision_space_id = ? AND version.enabled = 1
            )
            ORDER BY repository_id
            """,
            (
                principal.organization_id,
                decision_space_id,
                principal.organization_id,
                decision_space_id,
            ),
        ).fetchall()
        repositories = {
            row["repository_id"]
            for row in rows
        }
        if space.kind == "product":
            repositories.update(
                row["repository_id"]
                for row in self.connection.execute(
                    """SELECT repository_id FROM repository_mappings
                    WHERE organization_id = ? AND product_id = ?
                      AND enabled = 1""",
                    (
                        principal.organization_id,
                        space.compatibility_product_id,
                    ),
                ).fetchall()
            )
        return tuple(
            RepositoryView(
                repository_id,
                space.compatibility_product_id,
                space.compatibility_product_name,
                True,
            )
            for repository_id in sorted(repositories)
        )

    def candidate_revision_ownership(
        self,
        principal: Principal,
        repository_id: str,
        family_id: str,
        revision: int,
    ) -> CandidateOwnershipSnapshot | None:
        self._require_user(principal)
        row = self.connection.execute(
            """
            SELECT ownership_json, ownership_digest
            FROM candidate_revision_ownership
            WHERE organization_id = ? AND repository_id = ?
              AND family_id = ? AND revision = ?
            """,
            (
                principal.organization_id,
                repository_id,
                family_id,
                revision,
            ),
        ).fetchone()
        if row is None:
            return None
        return cast(
            CandidateOwnershipSnapshot,
            self._read_record(
                row["ownership_json"], row["ownership_digest"],
                CandidateOwnershipSnapshot, "candidate_ownership",
            ),
        )

    def repository_mapping(
        self, principal: Principal, repository_id: str
    ) -> RepositoryView | None:
        self._require_user(principal)
        row = self.connection.execute(
            """
            SELECT repository_id, product_id, product_name, enabled
            FROM repository_mappings
            WHERE organization_id = ? AND repository_id = ?
            """,
            (principal.organization_id, repository_id),
        ).fetchone()
        if row is None:
            return None
        return RepositoryView(
            row["repository_id"],
            row["product_id"],
            row["product_name"],
            bool(row["enabled"]),
        )

    def capture_request_route(
        self, principal: Principal, request_id: str
    ) -> tuple[str, str] | None:
        self._require_user(principal)
        row = self.connection.execute(
            """
            SELECT repository_id, product_id
            FROM capture_requests
            WHERE organization_id = ? AND request_id = ?
            """,
            (principal.organization_id, request_id),
        ).fetchone()
        if row is None:
            return None
        return row["repository_id"], row["product_id"]

    def candidate_revision(
        self,
        principal: Principal,
        repository_id: str,
        family_id: str,
        revision_id: str,
    ) -> CandidateRevisionUpload | None:
        self._require_user(principal)
        row = self.connection.execute(
            """
            SELECT revision.family_id, revision.revision,
                   revision.revision_id, revision.record_json,
                   revision.record_digest
            FROM candidate_revisions AS revision
            WHERE revision.organization_id = ?
              AND revision.repository_id = ?
              AND revision.family_id = ?
              AND revision.revision_id = ?
            """,
            (
                principal.organization_id,
                repository_id,
                family_id,
                revision_id,
            ),
        ).fetchone()
        if row is None:
            return None
        candidate = cast(
            CandidateRevisionUpload,
            self._read_record(
                row["record_json"],
                row["record_digest"],
                CandidateRevisionUpload,
                "candidate_revision",
            ),
        )
        if (
            candidate.family_id != row["family_id"]
            or candidate.revision != row["revision"]
            or candidate.revision_id != row["revision_id"]
        ):
            raise WebRecordCorrupt("candidate_revision")
        return candidate

    def current_candidate_revision(
        self,
        principal: Principal,
        repository_id: str,
        family_id: str,
    ) -> CandidateRevisionUpload | None:
        """Return the exact current head for one owned Candidate family."""

        self._require_user(principal)
        row = self.connection.execute(
            """
            SELECT revision.family_id, revision.revision,
                   revision.revision_id, revision.record_json,
                   revision.record_digest
            FROM candidate_family_heads AS head
            JOIN candidate_revisions AS revision
              ON revision.organization_id = head.organization_id
             AND revision.repository_id = head.repository_id
             AND revision.family_id = head.family_id
             AND revision.revision = head.revision
             AND revision.revision_id = head.revision_id
            WHERE head.organization_id = ?
              AND head.repository_id = ?
              AND head.family_id = ?
            """,
            (principal.organization_id, repository_id, family_id),
        ).fetchone()
        if row is None:
            return None
        candidate = cast(
            CandidateRevisionUpload,
            self._read_record(
                row["record_json"],
                row["record_digest"],
                CandidateRevisionUpload,
                "candidate_revision",
            ),
        )
        if (
            candidate.family_id != row["family_id"]
            or candidate.revision != row["revision"]
            or candidate.revision_id != row["revision_id"]
        ):
            raise WebRecordCorrupt("candidate_revision")
        return candidate

    def candidate_inbox(
        self,
        principal: Principal,
        decision_space_id: str,
        draft: ReviewDraft,
        *,
        search: str = "",
        repository_id: str | None = None,
        capture_request_id: str | None = None,
        state: str = "pending",
        limit: int = 50,
        offset: int = 0,
    ) -> CandidateInboxView:
        self._require_user(principal)
        space = self.decision_space(principal, decision_space_id)
        if space is None or space.decision_space_id != decision_space_id:
            raise ValueError("decision space is unavailable")
        repositories = self.decision_space_repositories(
            principal, decision_space_id
        )
        if draft.decision_space_id != decision_space_id:
            raise ValueError("draft decision space is invalid")

        parameters: list[object] = [
            principal.organization_id, decision_space_id
        ]
        conditions = [
            "head.organization_id = ?",
            "ownership.decision_space_id = ?",
        ]
        if repository_id is not None:
            conditions.append("head.repository_id = ?")
            parameters.append(repository_id)
        if capture_request_id is not None:
            conditions.append(
                """EXISTS (
                    SELECT 1
                    FROM web_candidate_revision_batches AS association
                    JOIN capture_requests AS request
                      ON request.request_id = association.request_id
                     AND request.organization_id = association.organization_id
                     AND request.repository_id = association.repository_id
                    WHERE association.organization_id = head.organization_id
                      AND association.repository_id = head.repository_id
                      AND association.family_id = head.family_id
                      AND association.revision_id = head.revision_id
                      AND association.request_id = ?
                )"""
            )
            parameters.append(capture_request_id)
        rows = self.connection.execute(
            f"""
            SELECT head.repository_id,
                   head.family_id, head.revision, head.revision_id,
                   revision.record_json, revision.record_digest
            FROM candidate_family_heads AS head
            JOIN candidate_revisions AS revision
              ON revision.organization_id = head.organization_id
             AND revision.repository_id = head.repository_id
             AND revision.family_id = head.family_id
             AND revision.revision = head.revision
             AND revision.revision_id = head.revision_id
            JOIN candidate_revision_ownership AS ownership
              ON ownership.organization_id = head.organization_id
             AND ownership.repository_id = head.repository_id
             AND ownership.family_id = head.family_id
             AND ownership.revision = head.revision
            WHERE {' AND '.join(conditions)}
            ORDER BY head.family_id, head.repository_id
            """,
            tuple(parameters),
        ).fetchall()

        draft_by_family = {item.family_id: item for item in draft.items}
        selected: list[CandidateInboxItem] = []
        normalized_search = search.casefold()
        for row in rows:
            candidate = cast(
                CandidateRevisionUpload,
                self._read_record(
                    row["record_json"],
                    row["record_digest"],
                    CandidateRevisionUpload,
                    "candidate_revision",
                ),
            )
            if (
                candidate.family_id != row["family_id"]
                or candidate.revision != row["revision"]
                or candidate.revision_id != row["revision_id"]
            ):
                raise WebRecordCorrupt("candidate_revision")
            if normalized_search and normalized_search not in self._candidate_text(
                candidate
            ).casefold():
                continue
            review_state = self._candidate_review_state(
                principal.organization_id,
                decision_space_id,
                row["repository_id"],
                candidate,
            )
            if state != "all" and review_state != state:
                continue
            draft_item = draft_by_family.get(candidate.family_id)
            selected.append(
                CandidateInboxItem(
                    family_id=candidate.family_id,
                    repository_id=row["repository_id"],
                    capture_request_ids=self._capture_request_ids(
                        principal.organization_id,
                        decision_space_id,
                        row["repository_id"],
                        candidate,
                    ),
                    revision_id=candidate.revision_id,
                    revision=candidate.revision,
                    content_digest=candidate.content_digest,
                    content=candidate.content,
                    review_state=review_state,
                    draft_action=(draft_item.action if draft_item else None),
                    stale_draft=(
                        draft_item is not None
                        and draft_item.revision_id != candidate.revision_id
                    ),
                )
            )
        return CandidateInboxView(
            space.compatibility_product_id,
            space.compatibility_product_name,
            repositories,
            tuple(selected[offset : offset + limit]),
            draft,
            self._space_ref(space),
        )

    def _capture_request_ids(
        self,
        organization_id: str,
        decision_space_id: str,
        repository_id: str,
        candidate: CandidateRevisionUpload,
    ) -> tuple[str, ...]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT association.request_id
            FROM web_candidate_revision_batches AS association
            JOIN capture_requests AS request
              ON request.request_id = association.request_id
             AND request.organization_id = association.organization_id
             AND request.repository_id = association.repository_id
            WHERE association.organization_id = ?
              AND association.repository_id = ?
              AND association.family_id = ?
              AND association.revision_id = ?
              AND association.decision_space_id = ?
            ORDER BY association.request_id
            """,
            (
                organization_id,
                repository_id,
                candidate.family_id,
                candidate.revision_id,
                decision_space_id,
            ),
        ).fetchall()
        return tuple(row["request_id"] for row in rows)

    def _candidate_review_state(
        self,
        organization_id: str,
        decision_space_id: str,
        repository_id: str,
        candidate: CandidateRevisionUpload,
    ) -> CandidateReviewState:
        receipt = self.connection.execute(
            """
            SELECT 1 FROM web_candidate_receipts
            WHERE organization_id = ? AND decision_space_id = ? AND family_id = ?
            """,
            (organization_id, decision_space_id, candidate.family_id),
        ).fetchone()
        if receipt is not None:
            return "published"
        row = self.connection.execute(
            """
            SELECT item.action
            FROM web_review_items AS item
            JOIN web_review_batches AS batch
              ON batch.organization_id = item.organization_id
             AND batch.decision_space_id = item.decision_space_id
             AND batch.review_batch_id = item.review_batch_id
            WHERE item.organization_id = ? AND item.decision_space_id = ?
              AND item.repository_id = ? AND item.family_id = ?
              AND item.revision_id = ?
            ORDER BY batch.submission_order DESC, batch.rowid DESC,
                     item.item_order DESC
            LIMIT 1
            """,
            (
                organization_id,
                decision_space_id,
                repository_id,
                candidate.family_id,
                candidate.revision_id,
            ),
        ).fetchone()
        if row is None or row["action"] == "skip":
            return "pending"
        if row["action"] == "reject":
            return "rejected"
        return "accepted"

    @staticmethod
    def _candidate_text(candidate: CandidateRevisionUpload) -> str:
        content = candidate.content
        return "\n".join(
            (
                content.product,
                content.claim,
                content.future_action,
                content.scope_summary,
                *content.repositories,
                *content.paths,
                *content.invalidation_conditions,
            )
        )

    def dashboard(self, principal: Principal) -> DashboardView:
        self._require_user(principal)
        snapshot = self._registry_snapshot()
        spaces = self._space_summaries(principal, snapshot)
        products = tuple(space for space in spaces if space.kind == "product")
        shared_tree = self._shared_tree(principal, spaces)
        registry = RegistryStatus(
            "available" if snapshot is not None else "unavailable",
            snapshot.commit_sha if snapshot is not None else None,
        )
        active_count = (
            sum(item.active_decision_count or 0 for item in products)
            if snapshot is not None
            else None
        )
        allowed_products = {
            self.decision_space(principal, space.decision_space_id).compatibility_product_id
            for space in spaces
        }
        publications = tuple(
            publication
            for publication in self._recent_publications(principal)
            if publication.product_id in allowed_products
        )
        return DashboardView(
            DashboardMetrics(
                product_count=len(products),
                pending_candidate_count=sum(
                    item.pending_candidate_count for item in products
                ),
                active_decision_count=active_count,
                completed_this_week=self._completed_this_week(principal),
            ),
            registry,
            products,
            shared_tree,
            publications,
        )

    def _space_summaries(
        self, principal: Principal, snapshot: RegistrySnapshot | None
    ) -> tuple[DecisionSpaceSummary, ...]:
        rows = self.connection.execute(
            """SELECT * FROM decision_spaces
            WHERE organization_id = ? AND enabled = 1
            ORDER BY kind, display_name, decision_space_id""",
            (principal.organization_id,),
        ).fetchall()
        summaries: list[DecisionSpaceSummary] = []
        for row in rows:
            space = self.decision_space(principal, row["decision_space_id"])
            if space is None:
                continue
            repositories = self.decision_space_repositories(
                principal, space.decision_space_id
            )
            if not repositories:
                continue
            summaries.append(
                DecisionSpaceSummary(
                    decision_space_id=space.decision_space_id,
                    kind=space.kind,
                    display_name=space.display_name,
                    breadcrumb=(
                        *space.catalog_breadcrumb,
                        space.display_name,
                    ),
                    source_root=space.source_root,
                    package_name=space.package_name,
                    asset_type=space.asset_type,
                    repository_ids=tuple(
                        repository.repository_id for repository in repositories
                    ),
                    pending_candidate_count=self._pending_count_space(
                        principal.organization_id, space
                    ),
                    active_decision_count=(
                        sum(
                            revision.product_name
                            == space.compatibility_product_name
                            for revision in snapshot.active_decisions(
                                space.compatibility_product_id
                            )
                        )
                        if snapshot is not None
                        else None
                    ),
                    last_activity_at=self._last_activity_space(
                        principal.organization_id, space
                    ),
                )
            )
        return tuple(summaries)

    def _shared_tree(
        self,
        principal: Principal,
        spaces: tuple[DecisionSpaceSummary, ...],
    ) -> CatalogNode | None:
        shared_spaces = tuple(space for space in spaces if space.kind == "shared_unit")
        if not shared_spaces:
            return None
        rows = self.connection.execute(
            """SELECT * FROM catalog_groups WHERE organization_id = ?
            ORDER BY sort_order, display_name, catalog_group_id""",
            (principal.organization_id,),
        ).fetchall()
        groups = {row["catalog_group_id"]: row for row in rows}
        root = next(
            (
                row for row in rows
                if row["parent_group_id"] is None and row["display_name"] == "Shared"
            ),
            None,
        )
        if root is None:
            return None
        leaves_by_group: dict[str, list[DecisionSpaceSummary]] = {}
        for summary in shared_spaces:
            row = self.connection.execute(
                """SELECT catalog_group_id FROM decision_spaces
                WHERE organization_id = ? AND decision_space_id = ?""",
                (principal.organization_id, summary.decision_space_id),
            ).fetchone()
            if row is not None and row["catalog_group_id"] in groups:
                leaves_by_group.setdefault(row["catalog_group_id"], []).append(summary)
        children_by_group: dict[str, list[str]] = {}
        for row in rows:
            if row["parent_group_id"] is not None:
                children_by_group.setdefault(row["parent_group_id"], []).append(
                    row["catalog_group_id"]
                )

        def leaf_node(space: DecisionSpaceSummary) -> CatalogNode:
            return CatalogNode(
                space.decision_space_id,
                space.kind,
                space.display_name,
                space.breadcrumb,
                space.pending_candidate_count,
                space.active_decision_count,
                space.last_activity_at,
                space,
                (),
            )

        def group_node(group_id: str) -> CatalogNode | None:
            row = groups[group_id]
            children = [
                node
                for child_id in children_by_group.get(group_id, [])
                if (node := group_node(child_id)) is not None
            ]
            children.extend(
                leaf_node(space)
                for space in sorted(
                    leaves_by_group.get(group_id, []),
                    key=lambda item: (item.display_name.casefold(), item.decision_space_id),
                )
            )
            if not children:
                return None
            active = (
                None
                if any(child.active_decision_count is None for child in children)
                else sum(child.active_decision_count or 0 for child in children)
            )
            activities = [
                child.last_activity_at
                for child in children
                if child.last_activity_at is not None
            ]
            return CatalogNode(
                group_id,
                "catalog_group",
                row["display_name"],
                tuple(json.loads(row["breadcrumb_json"])),
                sum(child.pending_candidate_count for child in children),
                active,
                max(activities) if activities else None,
                None,
                tuple(children),
            )

        return group_node(root["catalog_group_id"])

    def _owned_products(
        self, principal: Principal
    ) -> dict[str, tuple[str, tuple[str, ...]]]:
        rows = self.connection.execute(
            """
            SELECT product_id, product_name, repository_id
            FROM repository_mappings
            WHERE organization_id = ? AND enabled = 1
            ORDER BY product_name, product_id, repository_id
            """,
            (principal.organization_id,),
        ).fetchall()
        grouped: dict[str, tuple[str, list[str]]] = {}
        for row in rows:
            product_id = row["product_id"]
            product_name = row["product_name"]
            current = grouped.get(product_id)
            if current is None:
                grouped[product_id] = (product_name, [row["repository_id"]])
            elif current[0] != product_name:
                raise WebRecordCorrupt("repository_mapping")
            else:
                current[1].append(row["repository_id"])
        return {
            product_id: (name, tuple(repositories))
            for product_id, (name, repositories) in grouped.items()
        }

    def _decision_publications(
        self, principal: Principal, product_ids: frozenset[str]
    ) -> dict[tuple[str, str, str], _DecisionPublication]:
        if not product_ids:
            return {}
        placeholders = ", ".join("?" for _ in product_ids)
        rows = self.connection.execute(
            f"""
            SELECT publication.compatibility_product_id AS product_id,
                   publication.publication_id,
                   publication.preview_id, publication.commit_sha,
                   publication.updated_at, publication.record_json,
                   publication.record_digest, receipt.decision_id,
                   receipt.preview_id AS receipt_preview_id,
                   receipt.commit_sha AS receipt_commit_sha
            FROM web_publications AS publication
            JOIN web_candidate_receipts AS receipt
              ON receipt.organization_id = publication.organization_id
             AND receipt.decision_space_id = publication.decision_space_id
             AND receipt.compatibility_product_id =
                 publication.compatibility_product_id
             AND receipt.preview_id = publication.preview_id
             AND receipt.commit_sha = publication.commit_sha
            WHERE publication.organization_id = ?
              AND publication.state = 'completed'
              AND publication.compatibility_product_id IN ({placeholders})
            ORDER BY publication.updated_at DESC,
                     publication.publication_id DESC,
                     receipt.decision_id
            """,
            (principal.organization_id, *sorted(product_ids)),
        ).fetchall()
        joined: dict[tuple[str, str, str], _DecisionPublication] = {}
        for row in rows:
            publication = cast(
                CentralPublication,
                self._read_record(
                    row["record_json"],
                    row["record_digest"],
                    CentralPublication,
                    "publication",
                ),
            )
            if (
                publication.organization_id != principal.organization_id
                or publication.product_id != row["product_id"]
                or publication.publication_id != row["publication_id"]
                or publication.preview_id != row["preview_id"]
                or publication.preview_id != row["receipt_preview_id"]
                or publication.state != "completed"
                or publication.commit_sha != row["commit_sha"]
                or publication.commit_sha != row["receipt_commit_sha"]
                or publication.updated_at != row["updated_at"]
                or publication.commit_sha is None
            ):
                raise WebRecordCorrupt("publication")
            key = (
                publication.product_id,
                row["decision_id"],
                publication.preview_id,
            )
            if key in joined:
                raise WebRecordCorrupt("candidate_receipt")
            joined[key] = _DecisionPublication(
                publication.publication_id,
                publication.preview_id,
                publication.updated_at,
                publication.commit_sha,
            )
        return joined

    @staticmethod
    def _decision_list_item(
        revision: DecisionRevision,
        publication: _DecisionPublication | None,
        space: DecisionSpaceRef,
    ) -> DecisionListItem:
        return DecisionListItem(
            decision_space_id=space.decision_space_id,
            space=space,
            product_id=revision.product_id,
            product_name=revision.product_name,
            decision_id=revision.decision_id,
            revision=revision.revision,
            lifecycle=revision.lifecycle,
            claim=revision.claim,
            future_action=revision.future_action,
            scope_summary=revision.scope_summary,
            repositories=revision.repositories,
            paths=revision.paths,
            published_at=(
                publication.published_at if publication is not None else None
            ),
            publication_id=(
                publication.publication_id if publication is not None else None
            ),
            commit_sha=(
                publication.commit_sha if publication is not None else None
            ),
        )

    @staticmethod
    def _space_ref(space: LeafDecisionSpace) -> DecisionSpaceRef:
        return DecisionSpaceRef(
            space.decision_space_id,
            space.kind,
            space.display_name,
            (*space.catalog_breadcrumb, space.display_name),
            space.source_root,
            space.package_name,
            space.asset_type,
        )

    @classmethod
    def _validate_decision_filters(
        cls,
        search: str,
        repository: str,
        published_after: str | None,
        limit: int,
        offset: int,
    ) -> None:
        for value, name in (
            (search, "search"),
            (repository, "repository"),
        ):
            try:
                encoded = value.encode("utf-8")
            except (AttributeError, UnicodeError):
                raise ValueError(f"{name} is invalid") from None
            if len(encoded) > 200:
                raise ValueError(f"{name} is too long")
        if published_after is not None:
            cls._rfc3339(published_after)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            raise ValueError("Decision pagination is invalid")

    @staticmethod
    def _rfc3339(value: str) -> datetime:
        if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
            raise ValueError("published_after is invalid")
        try:
            parsed = datetime.fromisoformat(
                value.removesuffix("Z")
                + ("+00:00" if value.endswith("Z") else "")
            )
        except ValueError:
            raise ValueError("published_after is invalid") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("published_after is invalid")
        return parsed.astimezone(UTC)

    def _list_products(
        self, principal: Principal, snapshot: RegistrySnapshot | None
    ) -> tuple[ProductSummary, ...]:
        products: list[ProductSummary] = []
        for product_id, (
            product_name,
            repository_ids,
        ) in self._owned_products(principal).items():
            products.append(
                ProductSummary(
                    product_id=product_id,
                    product_name=product_name,
                    repository_ids=repository_ids,
                    pending_candidate_count=self._pending_count(
                        principal.organization_id, product_id
                    ),
                    active_decision_count=(
                        sum(
                            revision.product_name == product_name
                            for revision in snapshot.active_decisions(
                                product_id
                            )
                        )
                        if snapshot is not None
                        else None
                    ),
                    last_activity_at=self._last_activity(
                        principal.organization_id, product_id
                    ),
                )
            )
        return tuple(products)

    def _pending_count(self, organization_id: str, product_id: str) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM candidate_family_heads AS head
            JOIN repository_mappings AS mapping
              ON mapping.organization_id = head.organization_id
             AND mapping.repository_id = head.repository_id
             AND mapping.enabled = 1
            LEFT JOIN web_candidate_receipts AS receipt
              ON receipt.organization_id = head.organization_id
             AND receipt.compatibility_product_id = mapping.product_id
             AND receipt.family_id = head.family_id
            WHERE head.organization_id = ?
              AND mapping.product_id = ?
              AND receipt.family_id IS NULL
              AND COALESCE((
                SELECT item.action
                FROM web_review_items AS item
                JOIN web_review_batches AS batch
                  ON batch.organization_id = item.organization_id
                 AND batch.decision_space_id = item.decision_space_id
                 AND batch.review_batch_id = item.review_batch_id
                WHERE item.organization_id = head.organization_id
                  AND batch.compatibility_product_id = mapping.product_id
                  AND item.repository_id = head.repository_id
                  AND item.family_id = head.family_id
                  AND item.revision_id = head.revision_id
                ORDER BY batch.submission_order DESC,
                         batch.rowid DESC,
                         item.item_order DESC
                LIMIT 1
              ), '') NOT IN ('accept', 'edit_accept', 'reject')
            """,
            (organization_id, product_id),
        ).fetchone()
        return int(row["count"])

    def _pending_count_space(
        self, organization_id: str, space: LeafDecisionSpace
    ) -> int:
        row = self.connection.execute(
            """SELECT COUNT(*) AS count
            FROM candidate_family_heads AS head
            JOIN candidate_revision_ownership AS ownership
              ON ownership.organization_id = head.organization_id
             AND ownership.repository_id = head.repository_id
             AND ownership.family_id = head.family_id
             AND ownership.revision = head.revision
            LEFT JOIN web_candidate_receipts AS receipt
              ON receipt.organization_id = head.organization_id
             AND receipt.decision_space_id = ownership.decision_space_id
             AND receipt.family_id = head.family_id
            WHERE head.organization_id = ?
              AND ownership.decision_space_id = ?
              AND receipt.family_id IS NULL
              AND COALESCE((
                SELECT item.action
                FROM web_review_items AS item
                JOIN web_review_batches AS batch
                  ON batch.organization_id = item.organization_id
                 AND batch.decision_space_id = item.decision_space_id
                 AND batch.review_batch_id = item.review_batch_id
                WHERE item.organization_id = head.organization_id
                  AND item.decision_space_id = ownership.decision_space_id
                  AND item.repository_id = head.repository_id
                  AND item.family_id = head.family_id
                  AND item.revision_id = head.revision_id
                ORDER BY batch.submission_order DESC, batch.rowid DESC,
                         item.item_order DESC
                LIMIT 1
              ), '') NOT IN ('accept', 'edit_accept', 'reject')""",
            (organization_id, space.decision_space_id),
        ).fetchone()
        count = int(row["count"])
        if count == 0 and space.kind == "product":
            return self._pending_count(
                organization_id, space.compatibility_product_id
            )
        return count

    def _last_activity_space(
        self, organization_id: str, space: LeafDecisionSpace
    ) -> str | None:
        row = self.connection.execute(
            """SELECT MAX(activity_at) AS activity_at FROM (
              SELECT batch.observed_at AS activity_at
              FROM web_candidate_revision_batches AS batch
              WHERE batch.organization_id = ? AND batch.decision_space_id = ?
              UNION ALL
              SELECT publication.updated_at AS activity_at
              FROM web_publications AS publication
              WHERE publication.organization_id = ?
                AND publication.decision_space_id = ?
            )""",
            (
                organization_id,
                space.decision_space_id,
                organization_id,
                space.decision_space_id,
            ),
        ).fetchone()
        if row is not None and row["activity_at"] is not None:
            return row["activity_at"]
        if space.kind == "product":
            return self._last_activity(
                organization_id, space.compatibility_product_id
            )
        return None

    def _last_activity(
        self, organization_id: str, product_id: str
    ) -> str | None:
        row = self.connection.execute(
            """
            SELECT MAX(activity_at) AS activity_at FROM (
              SELECT request.updated_at AS activity_at
              FROM capture_requests AS request
              WHERE request.organization_id = ? AND request.product_id = ?
              UNION ALL
              SELECT revision.observed_at AS activity_at
              FROM web_candidate_revision_batches AS revision
              JOIN repository_mappings AS mapping
                ON mapping.organization_id = revision.organization_id
               AND mapping.repository_id = revision.repository_id
               AND mapping.enabled = 1
              WHERE revision.organization_id = ? AND mapping.product_id = ?
              UNION ALL
              SELECT publication.updated_at AS activity_at
              FROM web_publications AS publication
              WHERE publication.organization_id = ?
                AND publication.compatibility_product_id = ?
            )
            """,
            (
                organization_id,
                product_id,
                organization_id,
                product_id,
                organization_id,
                product_id,
            ),
        ).fetchone()
        return row["activity_at"] if row is not None else None

    def _recent_publications(
        self, principal: Principal
    ) -> tuple[PublicationSummary, ...]:
        rows = self.connection.execute(
            """
            SELECT publication.record_json AS publication_json,
                   publication.record_digest AS publication_digest,
                   preview.record_json AS preview_json,
                   preview.record_digest AS preview_digest,
                   space.compatibility_product_name AS product_name
            FROM web_publications AS publication
            JOIN web_publication_previews AS preview
              ON preview.organization_id = publication.organization_id
             AND preview.decision_space_id = publication.decision_space_id
             AND preview.preview_id = publication.preview_id
            JOIN decision_spaces AS space
              ON space.organization_id = publication.organization_id
             AND space.decision_space_id = publication.decision_space_id
             AND space.enabled = 1
            WHERE publication.organization_id = ?
            ORDER BY publication.updated_at DESC, publication.publication_id DESC
            LIMIT 20
            """,
            (principal.organization_id,),
        ).fetchall()
        summaries: list[PublicationSummary] = []
        for row in rows:
            publication = self._read_record(
                row["publication_json"],
                row["publication_digest"],
                CentralPublication,
                "publication",
            )
            preview = self._read_record(
                row["preview_json"],
                row["preview_digest"],
                PublicationRecord,
                "publication_preview",
            )
            if (
                publication.organization_id != principal.organization_id
                or publication.preview_id != preview.preview_id
                or publication.product_id != preview.product_id
            ):
                raise WebRecordCorrupt("publication")
            summaries.append(
                PublicationSummary(
                    publication_id=publication.publication_id,
                    preview_id=publication.preview_id,
                    product_id=publication.product_id,
                    product_name=row["product_name"],
                    decision_count=len(preview.decision_ids),
                    actor_id=publication.actor_id,
                    approved_at=publication.approval.recorded_at,
                    state=(
                        "ambiguous"
                        if publication.recovery_code == "ambiguous"
                        else publication.state
                    ),
                    recovery_code=publication.recovery_code,
                    commit_sha=publication.commit_sha,
                )
            )
        return tuple(summaries)

    def _completed_this_week(self, principal: Principal) -> int:
        now = datetime.now(UTC)
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM web_publications AS publication
            WHERE publication.organization_id = ?
              AND publication.state = 'completed'
              AND publication.updated_at >= ?
              AND EXISTS (
                SELECT 1 FROM decision_spaces AS space
                WHERE space.organization_id = publication.organization_id
                  AND space.decision_space_id = publication.decision_space_id
                  AND space.enabled = 1
              )
            """,
            (
                principal.organization_id,
                monday.isoformat().replace("+00:00", "Z"),
            ),
        ).fetchone()
        return int(row["count"])

    def _registry_snapshot(self) -> RegistrySnapshot | None:
        try:
            snapshot = self.registry_query.snapshot()
        except RegistryQueryUnavailable:
            return None
        if not isinstance(snapshot, RegistrySnapshot):
            raise TypeError("registry_query returned an invalid snapshot")
        return snapshot

    @staticmethod
    def _require_user(principal: Principal) -> None:
        if not isinstance(principal, Principal) or principal.kind != "user":
            raise ValueError("A browser user Principal is required")

    @staticmethod
    def _read_record(
        record_json: object,
        record_digest: object,
        record_type: type[object],
        label: str,
    ) -> object:
        try:
            if not isinstance(record_json, str) or not isinstance(
                record_digest, str
            ):
                raise ValueError
            value = json.loads(record_json)
            record = record_type.from_dict(value)
            encoded = canonical_json_bytes(record.to_dict())
            if (
                encoded != record_json.encode("utf-8")
                or hashlib.sha256(encoded).hexdigest() != record_digest
            ):
                raise ValueError
            return record
        except (AttributeError, TypeError, json.JSONDecodeError, ValueError):
            raise WebRecordCorrupt(label) from None
