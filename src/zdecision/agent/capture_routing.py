"""Deterministic local route matching and Capture-group planning."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from zdecision.agent.git_path_evidence import FrozenGitPathEvidence
from zdecision.agent.repository_routes import RepositoryRouteSnapshot
from zdecision.agent.session_index import FrozenSessionSource
from zdecision.central.decision_spaces import RepositoryDecisionRoute
from zdecision.ids import capture_slice_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import ClaimedCaptureGroup, RouteSelection


@dataclass(frozen=True)
class MatchedRoute:
    route: RepositoryDecisionRoute
    matched_paths: tuple[str, ...]
    matched_path_digest: str


@dataclass(frozen=True)
class CaptureSlicePlan:
    slice_id: str
    route_id: str
    route_configuration_version: int
    decision_space_id: str
    matched_paths: tuple[str, ...]
    matched_path_digest: str
    source_boundary_digest: str
    source_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "slice_id": self.slice_id,
            "route_id": self.route_id,
            "route_configuration_version": self.route_configuration_version,
            "decision_space_id": self.decision_space_id,
            "matched_paths": list(self.matched_paths),
            "matched_path_digest": self.matched_path_digest,
            "source_boundary_digest": self.source_boundary_digest,
            "source_keys": list(self.source_keys),
        }


@dataclass(frozen=True)
class CaptureGroupPlan:
    request_id: str
    repository_id: str
    route_snapshot_digest: str
    evidence_digest: str
    source_boundary_digest: str
    slices: tuple[CaptureSlicePlan, ...]

    def route_selections(self) -> tuple[RouteSelection, ...]:
        return tuple(
            RouteSelection(
                route_id=item.route_id,
                configuration_version=item.route_configuration_version,
                matched_path_digest=item.matched_path_digest,
                source_boundary_digest=item.source_boundary_digest,
            )
            for item in self.slices
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "repository_id": self.repository_id,
            "route_snapshot_digest": self.route_snapshot_digest,
            "evidence_digest": self.evidence_digest,
            "source_boundary_digest": self.source_boundary_digest,
            "slices": [item.to_dict() for item in self.slices],
        }


class RepositoryRouteMatcher:
    def match(
        self,
        paths: tuple[str, ...],
        snapshot: RepositoryRouteSnapshot,
    ) -> tuple[MatchedRoute, ...]:
        grouped: dict[str, list[str]] = {}
        route_by_id = {route.route_id: route for route in snapshot.routes}
        for path in paths:
            matches = tuple(
                route
                for route in snapshot.routes
                if route.enabled and route.matches(path)
            )
            if len(matches) > 1:
                raise ValueError("decision_space_route_ambiguous")
            if matches:
                grouped.setdefault(matches[0].route_id, []).append(path)
        return tuple(
            MatchedRoute(
                route=route_by_id[route_id],
                matched_paths=tuple(sorted(values)),
                matched_path_digest=hashlib.sha256(
                    canonical_json_bytes({"paths": sorted(values)})
                ).hexdigest(),
            )
            for route_id, values in sorted(grouped.items())
        )


class CaptureRoutingStore:
    """Persist one immutable local path-bearing plan per Capture group."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> "CaptureRoutingStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS capture_group_plans (
                    request_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    route_snapshot_json TEXT NOT NULL,
                    route_snapshot_digest TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    source_boundary_digest TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    plan_digest TEXT NOT NULL
                )
                """
            )
        return cls(database_path, connection)

    def close(self) -> None:
        self._connection.close()

    def load_plan(
        self,
        group: ClaimedCaptureGroup,
        snapshot: RepositoryRouteSnapshot,
    ) -> CaptureGroupPlan | None:
        row = self._connection.execute(
            "SELECT * FROM capture_group_plans WHERE request_id = ?",
            (group.request_id,),
        ).fetchone()
        if row is None:
            return None
        return self._stored_plan(row, group, snapshot)

    def get_or_create_plan(
        self,
        group: ClaimedCaptureGroup,
        snapshot: RepositoryRouteSnapshot,
        sources: tuple[FrozenSessionSource, ...],
        evidence: FrozenGitPathEvidence,
    ) -> CaptureGroupPlan:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT * FROM capture_group_plans WHERE request_id = ?",
                (group.request_id,),
            ).fetchone()
            if row is not None:
                plan = self._stored_plan(row, group, snapshot)
                self._connection.commit()
                return plan

            plan = plan_capture_group(group, snapshot, evidence, sources)
            snapshot_json = canonical_json_bytes(
                {"routes": [route.to_dict() for route in snapshot.routes]}
            ).decode("utf-8")
            plan_json = canonical_json_bytes(plan.to_dict()).decode("utf-8")
            plan_digest = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
            self._connection.execute(
                """
                INSERT INTO capture_group_plans(
                    request_id, repository_id, route_snapshot_json,
                    route_snapshot_digest, evidence_digest,
                    source_boundary_digest, plan_json, plan_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group.request_id,
                    group.repository_id,
                    snapshot_json,
                    snapshot.digest,
                    evidence.evidence_digest,
                    plan.source_boundary_digest,
                    plan_json,
                    plan_digest,
                ),
            )
            self._connection.commit()
            return plan
        except Exception:
            self._connection.rollback()
            raise

    @staticmethod
    def _stored_plan(
        row: sqlite3.Row,
        group: ClaimedCaptureGroup,
        snapshot: RepositoryRouteSnapshot,
    ) -> CaptureGroupPlan:
        if (
            row["repository_id"] != group.repository_id
            or row["route_snapshot_digest"] != group.route_snapshot_digest
            or snapshot.digest != group.route_snapshot_digest
        ):
            raise ValueError("capture_group_plan_conflict")
        stored_snapshot = _read_json(
            row["route_snapshot_json"],
            row["route_snapshot_digest"],
            snapshot_record=True,
        )
        if stored_snapshot != {
            "routes": [route.to_dict() for route in group.route_snapshot]
        }:
            raise ValueError("capture_group_plan_corrupt")
        plan = _plan_from_dict(
            _read_json(row["plan_json"], row["plan_digest"])
        )
        if (
            plan.request_id != group.request_id
            or plan.repository_id != group.repository_id
            or plan.route_snapshot_digest != group.route_snapshot_digest
            or plan.evidence_digest != row["evidence_digest"]
            or plan.source_boundary_digest != row["source_boundary_digest"]
        ):
            raise ValueError("capture_group_plan_corrupt")
        return plan


def plan_capture_group(
    group: ClaimedCaptureGroup,
    snapshot: RepositoryRouteSnapshot,
    evidence: FrozenGitPathEvidence,
    sources: tuple[FrozenSessionSource, ...],
) -> CaptureGroupPlan:
    if group.repository_id != snapshot.repository_id:
        raise ValueError("route_snapshot_repository_mismatch")
    if group.repository_id != evidence.repository_id:
        raise ValueError("git_evidence_repository_mismatch")
    if group.route_snapshot_digest != snapshot.digest:
        raise ValueError("route_snapshot_mismatch")
    matched = RepositoryRouteMatcher().match(evidence.paths, snapshot)
    source_keys = tuple(source.source_key for source in sources)
    source_boundary_digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "sources": [
                    {
                        "source_key": source.source_key,
                        "source_fingerprint": source.source_fingerprint,
                        "previous_handled_head_commit": (
                            source.previous_handled_head_commit
                        ),
                        "upper_head_commit": source.upper_head_commit,
                    }
                    for source in sources
                ]
            }
        )
    ).hexdigest()
    return CaptureGroupPlan(
        request_id=group.request_id,
        repository_id=group.repository_id,
        route_snapshot_digest=snapshot.digest,
        evidence_digest=evidence.evidence_digest,
        source_boundary_digest=source_boundary_digest,
        slices=tuple(
            CaptureSlicePlan(
                slice_id=capture_slice_id(
                    group.request_id,
                    item.route.route_id,
                    item.route.configuration_version,
                ),
                route_id=item.route.route_id,
                route_configuration_version=item.route.configuration_version,
                decision_space_id=item.route.decision_space_id,
                matched_paths=item.matched_paths,
                matched_path_digest=item.matched_path_digest,
                source_boundary_digest=source_boundary_digest,
                source_keys=source_keys,
            )
            for item in matched
        ),
    )


def _read_json(
    encoded: object,
    digest: object,
    *,
    snapshot_record: bool = False,
) -> object:
    if not isinstance(encoded, str) or not isinstance(digest, str):
        raise ValueError("capture_group_plan_corrupt")
    expected = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if expected != digest:
        raise ValueError("capture_group_plan_corrupt")
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError:
        raise ValueError("capture_group_plan_corrupt") from None
    if canonical_json_bytes(value).decode("utf-8") != encoded:
        raise ValueError("capture_group_plan_corrupt")
    return value


def _plan_from_dict(value: object) -> CaptureGroupPlan:
    if not isinstance(value, dict) or set(value) != {
        "request_id",
        "repository_id",
        "route_snapshot_digest",
        "evidence_digest",
        "source_boundary_digest",
        "slices",
    }:
        raise ValueError("capture_group_plan_corrupt")
    raw_slices = value["slices"]
    if not isinstance(raw_slices, list):
        raise ValueError("capture_group_plan_corrupt")
    slices: list[CaptureSlicePlan] = []
    for item in raw_slices:
        if not isinstance(item, dict) or set(item) != {
            "slice_id",
            "route_id",
            "route_configuration_version",
            "decision_space_id",
            "matched_paths",
            "matched_path_digest",
            "source_boundary_digest",
            "source_keys",
        }:
            raise ValueError("capture_group_plan_corrupt")
        paths = item["matched_paths"]
        source_keys = item["source_keys"]
        if not isinstance(paths, list) or not isinstance(source_keys, list):
            raise ValueError("capture_group_plan_corrupt")
        slices.append(
            CaptureSlicePlan(
                slice_id=item["slice_id"],
                route_id=item["route_id"],
                route_configuration_version=item[
                    "route_configuration_version"
                ],
                decision_space_id=item["decision_space_id"],
                matched_paths=tuple(paths),
                matched_path_digest=item["matched_path_digest"],
                source_boundary_digest=item["source_boundary_digest"],
                source_keys=tuple(source_keys),
            )
        )
    return CaptureGroupPlan(
        request_id=value["request_id"],
        repository_id=value["repository_id"],
        route_snapshot_digest=value["route_snapshot_digest"],
        evidence_digest=value["evidence_digest"],
        source_boundary_digest=value["source_boundary_digest"],
        slices=tuple(slices),
    )
