"""Durable, verified SQLite projection of the Git Decision Registry."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from zdecision.central.auth import require_id
from zdecision.central.web.store import immediate
from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.models import (
    DecisionHead,
    DecisionRevision,
    ProductMetadata,
    ProductRegistry,
)
from zdecision.registry.git import GitRegistryError
from zdecision.registry.query import (
    RegistryQueryUnavailable,
    RegistrySnapshot,
)


ProjectionState = Literal["available", "syncing", "unavailable"]

_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_ERROR_CODES = frozenset(
    ("git_proof_failed", "registry_invalid", "projection_install_failed")
)

_PRODUCT_INSERT = """
INSERT INTO registry_product_projection(
  organization_id, registry_tree_oid, product_id, product_name,
  product_path, registry_path, product_json, product_digest
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

_DECISION_INSERT = """
INSERT INTO registry_decision_projection(
  organization_id, registry_tree_oid, product_id, decision_id, revision,
  lifecycle, head_path, claim, future_action, scope_summary,
  repositories_json, paths_json, invalidation_conditions_json,
  publication_preview_id, decision_json, decision_digest
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class RegistryProjectionConflict(Exception):
    code = "projection_state_conflict"


@dataclass(frozen=True)
class RegistryProjectionState:
    organization_id: str
    state: ProjectionState
    active_commit: str | None
    active_tree_oid: str | None
    desired_commit: str | None
    desired_tree_oid: str | None
    verified_at: str | None
    updated_at: str
    product_count: int | None
    decision_count: int | None
    projection_digest: str | None
    error_code: str | None


@dataclass(frozen=True)
class ActiveRegistryProjection:
    commit_sha: str
    tree_oid: str
    verified_at: str
    snapshot: RegistrySnapshot


def _timestamp(value: str | datetime) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("now is invalid")
    datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    return value


def _object_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _OBJECT_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _optional_object_id(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _object_id(value, field_name)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _projection_digest(
    products: list[tuple[str, str]],
    decisions: list[tuple[str, str, int, str]],
) -> str:
    return _digest(
        {
            "products": [list(item) for item in products],
            "decisions": [list(item) for item in decisions],
        }
    )


def _state(row: sqlite3.Row) -> RegistryProjectionState:
    return RegistryProjectionState(
        organization_id=row["organization_id"],
        state=row["state"],
        active_commit=row["active_commit"],
        active_tree_oid=row["active_tree_oid"],
        desired_commit=row["desired_commit"],
        desired_tree_oid=row["desired_tree_oid"],
        verified_at=row["verified_at"],
        updated_at=row["updated_at"],
        product_count=row["product_count"],
        decision_count=row["decision_count"],
        projection_digest=row["projection_digest"],
        error_code=row["error_code"],
    )


def _snapshot_rows(
    organization_id: str,
    tree_oid: str,
    snapshot: RegistrySnapshot,
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]], str]:
    if not isinstance(snapshot, RegistrySnapshot):
        raise TypeError("snapshot must be a RegistrySnapshot")
    _object_id(snapshot.commit_sha, "commit_sha")
    product_rows: list[tuple[object, ...]] = []
    decision_rows: list[tuple[object, ...]] = []
    product_manifest: list[tuple[str, str]] = []
    decision_manifest: list[tuple[str, str, int, str]] = []
    if set(snapshot.products) != set(snapshot.registries):
        raise ValueError("Registry products and registries do not match")

    expected_decision_keys: set[tuple[str, str]] = set()
    for product_id, product in sorted(snapshot.products.items()):
        registry = snapshot.registries[product_id]
        if (
            not isinstance(product, ProductMetadata)
            or product.product_id != product_id
            or not isinstance(registry, ProductRegistry)
            or registry.product_id != product_id
        ):
            raise ValueError("Registry product ownership mismatch")
        product_json = _json(product.to_dict())
        product_digest = hashlib.sha256(product_json.encode("utf-8")).hexdigest()
        product_rows.append(
            (
                organization_id,
                tree_oid,
                product_id,
                product.name,
                f"products/{product_id}/product.json",
                f"products/{product_id}/registry.json",
                product_json,
                product_digest,
            )
        )
        product_manifest.append((product_id, product_digest))
        for decision_id, head in sorted(registry.decisions.items()):
            expected_decision_keys.add((product_id, decision_id))
            revision = snapshot.decisions.get((product_id, decision_id))
            expected_path = f"decisions/{decision_id}/r{head.head_revision:04d}.json"
            if (
                not isinstance(head, DecisionHead)
                or head.head_path != expected_path
                or not isinstance(revision, DecisionRevision)
                or revision.product_id != product_id
                or revision.product_name != product.name
                or revision.decision_id != decision_id
                or revision.revision != head.head_revision
                or revision.lifecycle != head.lifecycle
            ):
                raise ValueError("Registry Decision ownership mismatch")
            decision_json = _json(revision.to_dict())
            decision_digest = hashlib.sha256(
                decision_json.encode("utf-8")
            ).hexdigest()
            decision_rows.append(
                (
                    organization_id,
                    tree_oid,
                    product_id,
                    decision_id,
                    revision.revision,
                    revision.lifecycle,
                    expected_path,
                    revision.claim,
                    revision.future_action,
                    revision.scope_summary,
                    _json(list(revision.repositories)),
                    _json(list(revision.paths)),
                    _json(list(revision.invalidation_conditions)),
                    revision.publication_preview_id,
                    decision_json,
                    decision_digest,
                )
            )
            decision_manifest.append(
                (product_id, decision_id, revision.revision, decision_digest)
            )
    if set(snapshot.decisions) != expected_decision_keys:
        raise ValueError("Registry Decision keys do not match heads")
    return (
        product_rows,
        decision_rows,
        _projection_digest(product_manifest, decision_manifest),
    )


class RegistryProjectionStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        self.connection = connection

    def get_state(self, organization_id: str) -> RegistryProjectionState | None:
        organization = require_id(organization_id, "organization_id")
        row = self.connection.execute(
            """SELECT organization_id, state, active_commit, active_tree_oid,
                      desired_commit, desired_tree_oid, verified_at, updated_at,
                      product_count, decision_count, projection_digest, error_code
               FROM registry_projection_state WHERE organization_id = ?""",
            (organization,),
        ).fetchone()
        return None if row is None else _state(row)

    def mark_syncing(
        self,
        organization_id: str,
        desired_commit: str,
        desired_tree_oid: str,
        verified_at: str,
        updated_at: str,
    ) -> RegistryProjectionState:
        organization = require_id(organization_id, "organization_id")
        commit = _object_id(desired_commit, "desired_commit")
        tree = _object_id(desired_tree_oid, "desired_tree_oid")
        verified = _timestamp(verified_at)
        updated = _timestamp(updated_at)
        with immediate(self.connection):
            self.connection.execute(
                """INSERT INTO registry_projection_state(
                     organization_id, state, desired_commit, desired_tree_oid,
                     verified_at, updated_at
                   ) VALUES (?, 'syncing', ?, ?, ?, ?)
                   ON CONFLICT(organization_id) DO UPDATE SET
                     state = 'syncing', desired_commit = excluded.desired_commit,
                     desired_tree_oid = excluded.desired_tree_oid,
                     verified_at = excluded.verified_at,
                     updated_at = excluded.updated_at, error_code = NULL""",
                (organization, commit, tree, verified, updated),
            )
        state = self.get_state(organization)
        assert state is not None
        return state

    def mark_unavailable(
        self,
        organization_id: str,
        desired_commit: str | None,
        desired_tree_oid: str | None,
        verified_at: str | None,
        updated_at: str,
        error_code: str,
    ) -> RegistryProjectionState:
        organization = require_id(organization_id, "organization_id")
        commit = _optional_object_id(desired_commit, "desired_commit")
        tree = _optional_object_id(desired_tree_oid, "desired_tree_oid")
        verified = None if verified_at is None else _timestamp(verified_at)
        updated = _timestamp(updated_at)
        if error_code not in _ERROR_CODES:
            raise ValueError("error_code is invalid")
        with immediate(self.connection):
            self.connection.execute(
                """INSERT INTO registry_projection_state(
                     organization_id, state, desired_commit, desired_tree_oid,
                     verified_at, updated_at, error_code
                   ) VALUES (?, 'unavailable', ?, ?, ?, ?, ?)
                   ON CONFLICT(organization_id) DO UPDATE SET
                     state = 'unavailable', desired_commit = excluded.desired_commit,
                     desired_tree_oid = excluded.desired_tree_oid,
                     verified_at = excluded.verified_at,
                     updated_at = excluded.updated_at,
                     error_code = excluded.error_code""",
                (organization, commit, tree, verified, updated, error_code),
            )
        state = self.get_state(organization)
        assert state is not None
        return state

    def matches(
        self,
        organization_id: str,
        tree_oid: str,
        snapshot: RegistrySnapshot,
    ) -> bool:
        organization = require_id(organization_id, "organization_id")
        tree = _object_id(tree_oid, "tree_oid")
        product_rows, decision_rows, projection_digest = _snapshot_rows(
            organization, tree, snapshot
        )
        state = self.get_state(organization)
        if (
            state is None
            or state.active_tree_oid != tree
            or state.product_count != len(product_rows)
            or state.decision_count != len(decision_rows)
            or state.projection_digest != projection_digest
        ):
            return False
        expected_products = {tuple(row[2:]) for row in product_rows}
        expected_decisions = {tuple(row[2:]) for row in decision_rows}
        products = self.connection.execute(
            """SELECT product_id, product_name, product_path, registry_path,
                      product_json, product_digest
               FROM registry_product_projection
               WHERE organization_id = ? AND registry_tree_oid = ?""",
            (organization, tree),
        ).fetchall()
        decisions = self.connection.execute(
            """SELECT product_id, decision_id, revision, lifecycle, head_path,
                      claim, future_action, scope_summary, repositories_json,
                      paths_json, invalidation_conditions_json,
                      publication_preview_id, decision_json, decision_digest
               FROM registry_decision_projection
               WHERE organization_id = ? AND registry_tree_oid = ?""",
            (organization, tree),
        ).fetchall()
        return expected_products == {
            tuple(row) for row in products
        } and expected_decisions == {tuple(row) for row in decisions}

    def install(
        self,
        organization_id: str,
        tree_oid: str,
        snapshot: RegistrySnapshot,
        verified_at: str,
        updated_at: str,
    ) -> RegistryProjectionState:
        organization = require_id(organization_id, "organization_id")
        tree = _object_id(tree_oid, "tree_oid")
        verified = _timestamp(verified_at)
        updated = _timestamp(updated_at)
        product_rows, decision_rows, projection_digest = _snapshot_rows(
            organization, tree, snapshot
        )
        with immediate(self.connection):
            state = self.get_state(organization)
            if (
                state is None
                or state.state != "syncing"
                or state.desired_commit != snapshot.commit_sha
                or state.desired_tree_oid != tree
            ):
                raise RegistryProjectionConflict("projection_state_conflict")
            self.connection.execute(
                """DELETE FROM registry_decision_projection
                   WHERE organization_id = ? AND registry_tree_oid = ?""",
                (organization, tree),
            )
            self.connection.execute(
                """DELETE FROM registry_product_projection
                   WHERE organization_id = ? AND registry_tree_oid = ?""",
                (organization, tree),
            )
            self.connection.executemany(_PRODUCT_INSERT, product_rows)
            self.connection.executemany(_DECISION_INSERT, decision_rows)
            self.connection.execute(
                """UPDATE registry_projection_state
                   SET state = 'available', active_commit = ?, active_tree_oid = ?,
                       desired_commit = NULL, desired_tree_oid = NULL,
                       verified_at = ?, updated_at = ?, product_count = ?,
                       decision_count = ?, projection_digest = ?, error_code = NULL
                   WHERE organization_id = ?""",
                (
                    snapshot.commit_sha,
                    tree,
                    verified,
                    updated,
                    len(product_rows),
                    len(decision_rows),
                    projection_digest,
                    organization,
                ),
            )
            self.connection.execute(
                """DELETE FROM registry_decision_projection
                   WHERE organization_id = ? AND registry_tree_oid != ?""",
                (organization, tree),
            )
            self.connection.execute(
                """DELETE FROM registry_product_projection
                   WHERE organization_id = ? AND registry_tree_oid != ?""",
                (organization, tree),
            )
        state = self.get_state(organization)
        assert state is not None
        return state

    def update_provenance(
        self,
        organization_id: str,
        commit_sha: str,
        tree_oid: str,
        verified_at: str,
        updated_at: str,
    ) -> RegistryProjectionState:
        organization = require_id(organization_id, "organization_id")
        commit = _object_id(commit_sha, "commit_sha")
        tree = _object_id(tree_oid, "tree_oid")
        verified = _timestamp(verified_at)
        updated = _timestamp(updated_at)
        with immediate(self.connection):
            state = self.get_state(organization)
            if (
                state is None
                or state.state != "syncing"
                or state.desired_commit != commit
                or state.desired_tree_oid != tree
                or state.active_tree_oid != tree
            ):
                raise RegistryProjectionConflict("projection_state_conflict")
            self.connection.execute(
                """UPDATE registry_projection_state
                   SET state = 'available', active_commit = ?,
                       verified_at = ?, updated_at = ?, desired_commit = NULL,
                       desired_tree_oid = NULL, error_code = NULL
                   WHERE organization_id = ?""",
                (commit, verified, updated, organization),
            )
        state = self.get_state(organization)
        assert state is not None
        return state

    def load_active(
        self, organization_id: str
    ) -> ActiveRegistryProjection | None:
        organization = require_id(organization_id, "organization_id")
        state = self.get_state(organization)
        if state is None or state.state != "available":
            return None
        try:
            commit = _object_id(state.active_commit, "active_commit")
            tree = _object_id(state.active_tree_oid, "active_tree_oid")
            verified = _timestamp(state.verified_at)
            if (
                not isinstance(state.product_count, int)
                or isinstance(state.product_count, bool)
                or state.product_count < 0
                or not isinstance(state.decision_count, int)
                or isinstance(state.decision_count, bool)
                or state.decision_count < 0
                or not isinstance(state.projection_digest, str)
                or state.error_code is not None
            ):
                return None
            product_rows = self.connection.execute(
                """SELECT product_id, product_name, product_path, registry_path,
                          product_json, product_digest
                   FROM registry_product_projection
                   WHERE organization_id = ? AND registry_tree_oid = ?
                   ORDER BY product_id""",
                (organization, tree),
            ).fetchall()
            decision_rows = self.connection.execute(
                """SELECT product_id, decision_id, revision, lifecycle, head_path,
                          claim, future_action, scope_summary, repositories_json,
                          paths_json, invalidation_conditions_json,
                          publication_preview_id, decision_json, decision_digest
                   FROM registry_decision_projection
                   WHERE organization_id = ? AND registry_tree_oid = ?
                   ORDER BY product_id, decision_id, revision""",
                (organization, tree),
            ).fetchall()
            if (
                len(product_rows) != state.product_count
                or len(decision_rows) != state.decision_count
            ):
                return None
            products: dict[str, ProductMetadata] = {}
            product_manifest: list[tuple[str, str]] = []
            for row in product_rows:
                product_json = row["product_json"]
                product_digest = row["product_digest"]
                product = ProductMetadata.from_dict(json.loads(product_json))
                encoded = canonical_json_bytes(product.to_dict())
                if (
                    encoded != product_json.encode("utf-8")
                    or hashlib.sha256(encoded).hexdigest() != product_digest
                    or product.product_id != row["product_id"]
                    or product.name != row["product_name"]
                    or row["product_path"]
                    != f"products/{product.product_id}/product.json"
                    or row["registry_path"]
                    != f"products/{product.product_id}/registry.json"
                    or product.product_id in products
                ):
                    return None
                products[product.product_id] = product
                product_manifest.append((product.product_id, product_digest))

            heads: dict[str, dict[str, DecisionHead]] = {
                product_id: {} for product_id in products
            }
            decisions: dict[tuple[str, str], DecisionRevision] = {}
            decision_manifest: list[tuple[str, str, int, str]] = []
            for row in decision_rows:
                product_id = row["product_id"]
                decision_id = row["decision_id"]
                decision_json = row["decision_json"]
                decision_digest = row["decision_digest"]
                revision = DecisionRevision.from_dict(json.loads(decision_json))
                encoded = canonical_json_bytes(revision.to_dict())
                expected_path = (
                    f"decisions/{decision_id}/r{row['revision']:04d}.json"
                )
                if (
                    product_id not in products
                    or decision_id in heads[product_id]
                    or encoded != decision_json.encode("utf-8")
                    or hashlib.sha256(encoded).hexdigest() != decision_digest
                    or revision.product_id != product_id
                    or revision.product_name != products[product_id].name
                    or revision.decision_id != decision_id
                    or revision.revision != row["revision"]
                    or revision.lifecycle != row["lifecycle"]
                    or row["lifecycle"] != "active"
                    or row["head_path"] != expected_path
                    or revision.claim != row["claim"]
                    or revision.future_action != row["future_action"]
                    or revision.scope_summary != row["scope_summary"]
                    or _json(list(revision.repositories)) != row["repositories_json"]
                    or _json(list(revision.paths)) != row["paths_json"]
                    or _json(list(revision.invalidation_conditions))
                    != row["invalidation_conditions_json"]
                    or revision.publication_preview_id
                    != row["publication_preview_id"]
                ):
                    return None
                head = DecisionHead(
                    row["revision"], row["lifecycle"], row["head_path"]
                )
                heads[product_id][decision_id] = head
                decisions[(product_id, decision_id)] = revision
                decision_manifest.append(
                    (product_id, decision_id, row["revision"], decision_digest)
                )
            if _projection_digest(product_manifest, decision_manifest) != (
                state.projection_digest
            ):
                return None
            registries = {
                product_id: ProductRegistry(product_id, product_heads)
                for product_id, product_heads in heads.items()
            }
            snapshot = RegistrySnapshot(commit, products, registries, decisions)
            return ActiveRegistryProjection(commit, tree, verified, snapshot)
        except (
            AttributeError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ):
            return None


class RegistryProjectionError(Exception):
    code = "registry_projection_error"


class RegistryProjectionSynchronizer:
    def __init__(self, *, git, query, store, clock=None) -> None:
        if not callable(getattr(git, "require_exact_main", None)):
            raise TypeError("git must expose require_exact_main()")
        if not callable(getattr(git, "registry_tree_oid", None)):
            raise TypeError("git must expose registry_tree_oid()")
        if not callable(getattr(query, "snapshot_at_commit", None)):
            raise TypeError("query must expose snapshot_at_commit()")
        if not isinstance(store, RegistryProjectionStore):
            raise TypeError("store must be a RegistryProjectionStore")
        self.git = git
        self.query = query
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))

    def synchronize(
        self, organization_id: str, verified_commit: str, verified_at: str,
    ) -> RegistryProjectionState:
        desired_tree_oid: str | None = None
        updated_at = _timestamp(self.clock())
        try:
            self.git.require_exact_main(verified_commit)
            desired_tree_oid = self.git.registry_tree_oid(verified_commit)
            self.store.mark_syncing(
                organization_id, verified_commit, desired_tree_oid,
                verified_at, updated_at,
            )
            snapshot = self.query.snapshot_at_commit(verified_commit)
            if snapshot.commit_sha != verified_commit:
                raise RegistryQueryUnavailable("registry_unavailable")
            if self.store.matches(organization_id, desired_tree_oid, snapshot):
                return self.store.update_provenance(
                    organization_id, verified_commit, desired_tree_oid,
                    verified_at, updated_at,
                )
            return self.store.install(
                organization_id, desired_tree_oid, snapshot,
                verified_at, updated_at,
            )
        except GitRegistryError:
            error_code = "git_proof_failed"
        except RegistryQueryUnavailable:
            error_code = "registry_invalid"
        except (RegistryProjectionConflict, sqlite3.Error, ValueError):
            error_code = "projection_install_failed"
        try:
            return self.store.mark_unavailable(
                organization_id, verified_commit, desired_tree_oid,
                verified_at, updated_at, error_code,
            )
        except (sqlite3.Error, ValueError) as error:
            raise RegistryProjectionError("registry_projection_error") from error
