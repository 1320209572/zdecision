"""Organization-scoped read models for the Central Decision Web."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from zdecision.central.auth import Principal
from zdecision.central.web.contracts import CentralPublication
from zdecision.central.web.store import WebRecordCorrupt
from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.publication import PublicationRecord
from zdecision.registry.query import RegistryQueryUnavailable, RegistrySnapshot
from zdecision.sync.contracts import RepositoryView


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
    state: Literal["confirmed", "committed_pending_push", "completed"]
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
    products: tuple[ProductSummary, ...]
    recent_publications: tuple[PublicationSummary, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "metrics": self.metrics.to_dict(),
            "registry": self.registry.to_dict(),
            "products": [item.to_dict() for item in self.products],
            "recent_publications": [
                item.to_dict() for item in self.recent_publications
            ],
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

    def dashboard(self, principal: Principal) -> DashboardView:
        self._require_user(principal)
        snapshot = self._registry_snapshot()
        products = self._list_products(principal, snapshot)
        registry = RegistryStatus(
            "available" if snapshot is not None else "unavailable",
            snapshot.commit_sha if snapshot is not None else None,
        )
        active_count = (
            sum(item.active_decision_count or 0 for item in products)
            if snapshot is not None
            else None
        )
        publications = self._recent_publications(principal)
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
            publications,
        )

    def _list_products(
        self, principal: Principal, snapshot: RegistrySnapshot | None
    ) -> tuple[ProductSummary, ...]:
        rows = self.connection.execute(
            """
            SELECT product_id, product_name, repository_id
            FROM repository_mappings
            WHERE organization_id = ? AND enabled = 1
            ORDER BY product_name, product_id, repository_id
            """,
            (principal.organization_id,),
        ).fetchall()
        grouped: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            grouped.setdefault(
                (row["product_id"], row["product_name"]), []
            ).append(row["repository_id"])
        products: list[ProductSummary] = []
        for (product_id, product_name), repository_ids in grouped.items():
            registry = (
                snapshot.registries.get(product_id)
                if snapshot is not None
                else None
            )
            products.append(
                ProductSummary(
                    product_id=product_id,
                    product_name=product_name,
                    repository_ids=tuple(repository_ids),
                    pending_candidate_count=self._pending_count(
                        principal.organization_id, product_id
                    ),
                    active_decision_count=(
                        len(registry.decisions)
                        if snapshot is not None and registry is not None
                        else (0 if snapshot is not None else None)
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
             AND receipt.product_id = mapping.product_id
             AND receipt.family_id = head.family_id
            WHERE head.organization_id = ?
              AND mapping.product_id = ?
              AND receipt.family_id IS NULL
              AND COALESCE((
                SELECT item.action
                FROM web_review_items AS item
                JOIN web_review_batches AS batch
                  ON batch.organization_id = item.organization_id
                 AND batch.product_id = item.product_id
                 AND batch.review_batch_id = item.review_batch_id
                WHERE item.organization_id = head.organization_id
                  AND item.product_id = mapping.product_id
                  AND item.repository_id = head.repository_id
                  AND item.family_id = head.family_id
                  AND item.revision_id = head.revision_id
                ORDER BY batch.created_at DESC,
                         batch.review_batch_id DESC,
                         item.item_order DESC
                LIMIT 1
              ), '') NOT IN ('accept', 'edit_accept', 'reject')
            """,
            (organization_id, product_id),
        ).fetchone()
        return int(row["count"])

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
                AND publication.product_id = ?
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
                   mapping.product_name AS product_name
            FROM web_publications AS publication
            JOIN web_publication_previews AS preview
              ON preview.organization_id = publication.organization_id
             AND preview.product_id = publication.product_id
             AND preview.preview_id = publication.preview_id
            JOIN (
              SELECT organization_id, product_id, MIN(product_name) AS product_name
              FROM repository_mappings
              WHERE enabled = 1
              GROUP BY organization_id, product_id
            ) AS mapping
              ON mapping.organization_id = publication.organization_id
             AND mapping.product_id = publication.product_id
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
                    state=publication.state,
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
                SELECT 1 FROM repository_mappings AS mapping
                WHERE mapping.organization_id = publication.organization_id
                  AND mapping.product_id = publication.product_id
                  AND mapping.enabled = 1
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
