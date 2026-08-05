"""Durable Capture Request lifecycle and authorization rules."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from collections.abc import Sequence
from typing import Iterator

from zdecision.central.auth import Principal
from zdecision.central.decision_spaces import RepositoryCatalogView, RepositoryDecisionRoute
from zdecision.central.store import CentralStore
from zdecision.central.web.schema import record_candidate_revision_batch
from zdecision.ids import capture_request_id, capture_slice_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CandidateBatchUpload,
    CandidateOwnershipSnapshot,
    CandidateRevisionUpload,
    CandidateSliceBatchUpload,
    CaptureGroupCreate,
    CaptureGroupView,
    CaptureRequestCreate,
    CaptureRequestView,
    CaptureSliceView,
    ClaimedCaptureGroup,
    ClaimedCaptureRequest,
    ProgressEvent,
    RepositoryView,
    RouteSelection,
    SliceUploadReceipt,
    UploadReceipt,
)


_REQUEST_ID = re.compile(r"^crq_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ACTIVE_STATES = ("queued", "claimed", "running", "failed_retryable")
_SUCCESS_STATES = ("succeeded", "succeeded_no_candidates")
_MAX_SEQUENCE = 2_147_483_647
_RETRY_DELAYS = (5, 30, 120, 300)
class CentralRequestError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AccessDenied(CentralRequestError):
    pass


class RepositoryUnavailable(CentralRequestError):
    pass


class RequestNotFound(CentralRequestError):
    pass


class RequestConflict(CentralRequestError):
    pass


class InvalidLease(CentralRequestError):
    pass


class InvalidTransition(CentralRequestError):
    pass


class SequenceOverflow(CentralRequestError):
    pass


class CaptureRequestService:
    def __init__(self, store: CentralStore) -> None:
        if not isinstance(store, CentralStore):
            raise TypeError("store must be a CentralStore")
        self.store = store

    def list_repositories(
        self, user: Principal
    ) -> tuple[RepositoryView, ...]:
        principal = _require_user(user)
        rows = self.store.connection.execute(
            """
            SELECT repository_id, product_id, product_name, enabled
            FROM repository_mappings
            WHERE organization_id = ?
            ORDER BY product_name, repository_id
            """,
            (principal.organization_id,),
        ).fetchall()
        return tuple(
            RepositoryView(
                repository_id=row["repository_id"],
                product_id=row["product_id"],
                product_name=row["product_name"],
                enabled=bool(row["enabled"]),
            )
            for row in rows
        )

    def list_repository_spaces(
        self, user: Principal, repository_id: str
    ) -> RepositoryCatalogView:
        principal = _require_user(user)
        try:
            return self.store.repository_catalog(
                principal.organization_id, repository_id
            )
        except ValueError as error:
            raise RepositoryUnavailable(str(error)) from None

    def list_current_candidates(
        self,
        user: Principal,
        repository_id: str,
    ) -> tuple[CandidateRevisionUpload, ...]:
        principal = _require_user(user)
        mapping = self.store.connection.execute(
            """
            SELECT repository_id
            FROM repository_mappings
            WHERE organization_id = ? AND repository_id = ?
            """,
            (principal.organization_id, repository_id),
        ).fetchone()
        if mapping is None:
            raise RepositoryUnavailable("repository_unavailable")
        rows = self.store.connection.execute(
            """
            SELECT revisions.record_json, revisions.record_digest
            FROM candidate_family_heads AS heads
            JOIN candidate_revisions AS revisions
              ON revisions.organization_id = heads.organization_id
             AND revisions.repository_id = heads.repository_id
             AND revisions.family_id = heads.family_id
             AND revisions.revision_id = heads.revision_id
            WHERE heads.organization_id = ?
              AND heads.repository_id = ?
              AND NOT EXISTS (
                SELECT 1 FROM candidate_family_archives AS archives
                WHERE archives.organization_id = heads.organization_id
                  AND archives.repository_id = heads.repository_id
                  AND archives.family_id = heads.family_id
              )
            ORDER BY heads.family_id
            """,
            (principal.organization_id, repository_id),
        ).fetchall()
        return tuple(
            CandidateRevisionUpload.from_dict(
                _read_canonical(
                    row["record_json"],
                    row["record_digest"],
                    "Candidate revision",
                )
            )
            for row in rows
        )

    def create_group(
        self,
        user: Principal,
        command: CaptureGroupCreate,
        now: datetime,
    ) -> CaptureGroupView:
        """Create one public Capture action with a frozen server route snapshot."""

        principal = _require_user(user)
        if not isinstance(command, CaptureGroupCreate):
            raise TypeError("command must be a CaptureGroupCreate")
        timestamp = _timestamp(now)
        connection = self.store.connection
        with _immediate(connection):
            replay = connection.execute(
                """SELECT capture_groups.* FROM capture_group_actions AS action
                JOIN capture_groups ON capture_groups.request_id = action.request_id
                WHERE action.organization_id = ? AND action.actor_id = ?
                  AND action.client_action_id = ?""",
                (principal.organization_id, principal.actor_id, command.client_action_id),
            ).fetchone()
            if replay is not None:
                if (
                    replay["repository_id"] != command.repository_id
                    or replay["template_id"] != command.template_id
                    or replay["capture_scope"] != command.capture_scope
                ):
                    raise RequestConflict("capture_request_action_conflict")
                return _group_view(replay)

            active = connection.execute(
                """SELECT 1 FROM capture_groups
                WHERE organization_id = ? AND repository_id = ?
                  AND state IN ('queued','claimed','running','failed_retryable')""",
                (principal.organization_id, command.repository_id),
            ).fetchone()
            if active is not None:
                raise RequestConflict("repository_capture_busy")
            try:
                routes = self.store.list_enabled_routes(
                    principal.organization_id, command.repository_id
                )
            except ValueError as error:
                raise RepositoryUnavailable(str(error)) from None
            route_record = {"routes": [route.to_dict() for route in routes]}
            route_json, route_digest = _canonical_record(route_record)
            request_id = capture_request_id(
                principal.organization_id,
                command.repository_id,
                command.template_id,
                command.client_action_id,
            )
            try:
                connection.execute(
                    """INSERT INTO capture_groups(
                    request_id, organization_id, actor_id, repository_id,
                    template_id, capture_scope, client_action_id,
                    route_snapshot_json, route_snapshot_digest, state,
                    attempt_count, last_sequence, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, 1, ?, ?)""",
                    (request_id, principal.organization_id, principal.actor_id,
                     command.repository_id, command.template_id, command.capture_scope,
                     command.client_action_id, route_json, route_digest,
                     timestamp, timestamp),
                )
                connection.execute(
                    """INSERT INTO capture_group_actions(
                    organization_id, actor_id, client_action_id, request_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (principal.organization_id, principal.actor_id,
                     command.client_action_id, request_id, timestamp),
                )
                _insert_group_event(connection, request_id, 1, "queued", "request_queued", timestamp)
            except sqlite3.IntegrityError as error:
                raise RequestConflict("capture_request_conflict") from error
            return _group_view(_group_row(connection, request_id))

    def get_group(self, user: Principal, request_id: str) -> CaptureGroupView:
        principal = _require_user(user)
        row = _group_row(self.store.connection, request_id)
        _authorize_organization(principal, row)
        return _group_view(row)

    def claim_next_group(
        self,
        device: Principal,
        now: datetime,
        lease_seconds: int = 30,
    ) -> ClaimedCaptureGroup | None:
        principal = _require_device(device)
        duration = _lease_seconds(lease_seconds)
        timestamp = _timestamp(now)
        connection = self.store.connection
        with _immediate(connection):
            self._requeue_due_groups(connection, principal.organization_id, now)
            row = connection.execute(
                """SELECT capture_groups.* FROM capture_groups
                JOIN repositories ON repositories.organization_id = capture_groups.organization_id
                  AND repositories.repository_id = capture_groups.repository_id
                WHERE capture_groups.organization_id = ?
                  AND capture_groups.state = 'queued' AND repositories.enabled = 1
                ORDER BY capture_groups.created_at, capture_groups.request_id LIMIT 1""",
                (principal.organization_id,),
            ).fetchone()
            if row is None:
                return None
            token = f"lease_{secrets.token_urlsafe(24)}"
            token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            expires_at = _timestamp(now + timedelta(seconds=duration))
            sequence = _next_sequence(row["last_sequence"])
            connection.execute(
                """UPDATE capture_groups SET state = 'claimed',
                attempt_count = attempt_count + 1, claimed_device_id = ?,
                lease_token_digest = ?, lease_expires_at = ?, retry_at = NULL,
                last_sequence = ?, updated_at = ? WHERE request_id = ?""",
                (principal.device_id, token_digest, expires_at, sequence,
                 timestamp, row["request_id"]),
            )
            _insert_group_event(connection, row["request_id"], sequence, "claimed", "device_claimed", timestamp)
            return _claimed_group_view(_group_row(connection, row["request_id"]), token)

    def plan_slices(
        self,
        device: Principal,
        request_id: str,
        lease_token: str,
        selections: Sequence[RouteSelection],
        now: datetime,
    ) -> tuple[CaptureSliceView, ...]:
        """Validate selections against the frozen route snapshot and return immutable slices."""

        principal = _require_device(device)
        if isinstance(selections, (str, bytes)) or not isinstance(selections, Sequence):
            raise TypeError("selections must be a sequence")
        if any(not isinstance(item, RouteSelection) for item in selections):
            raise TypeError("selections must contain RouteSelection values")
        timestamp = _timestamp(now)
        connection = self.store.connection
        with _immediate(connection):
            row = _group_row(connection, request_id)
            _require_device_ownership(row, principal)
            _require_token(row, lease_token)
            ordered = tuple(sorted(selections, key=lambda item: item.route_id))
            if len({item.route_id for item in ordered}) != len(ordered):
                raise RequestConflict("slice_route_repeated")
            existing = connection.execute(
                "SELECT * FROM capture_slices WHERE request_id = ? ORDER BY slice_order",
                (request_id,),
            ).fetchall()
            if existing:
                if len(existing) != len(ordered) or any(
                    current["route_id"] != selection.route_id
                    or current["route_configuration_version"] != selection.configuration_version
                    or current["matched_path_digest"] != selection.matched_path_digest
                    or current["source_boundary_digest"] != selection.source_boundary_digest
                    for current, selection in zip(existing, ordered, strict=True)
                ):
                    raise RequestConflict("slice_plan_conflict")
                return tuple(_slice_view(item) for item in existing)
            if (
                not ordered
                and row["state"] == "succeeded_no_candidates"
                and row["terminal_code"] == "no_routable_decision_space_changes"
            ):
                return ()
            _require_live_lease(row, principal, lease_token, now)
            if row["state"] not in ("claimed", "running"):
                raise InvalidTransition("capture_request_not_active")
            if not ordered:
                empty_digest = _aggregate_receipt_digest(())
                sequence = _next_sequence(row["last_sequence"])
                connection.execute(
                    """UPDATE capture_groups SET state = 'succeeded_no_candidates',
                    result_receipt_digest = ?, result_candidate_count = 0,
                    terminal_code = 'no_routable_decision_space_changes',
                    lease_expires_at = NULL, last_sequence = ?, updated_at = ?
                    WHERE request_id = ?""",
                    (empty_digest, sequence, timestamp, request_id),
                )
                _insert_group_event(connection, request_id, sequence,
                                    "succeeded_no_candidates",
                                    "no_routable_decision_space_changes", timestamp)
                return ()
            snapshot = _route_snapshot(row)
            routes = {route.route_id: route for route in snapshot}
            views: list[CaptureSliceView] = []
            for index, selection in enumerate(ordered):
                route = routes.get(selection.route_id)
                if (
                    route is None
                    or route.configuration_version != selection.configuration_version
                    or route.repository_id != row["repository_id"]
                    or not route.enabled
                ):
                    raise RequestConflict("slice_route_not_in_snapshot")
                try:
                    space = self.store.decision_space(
                        row["organization_id"], route.decision_space_id
                    )
                except ValueError:
                    raise RequestConflict("slice_target_not_leaf") from None
                if not space.enabled:
                    raise RequestConflict("slice_target_disabled")
                ownership = CandidateOwnershipSnapshot(
                    repository_id=row["repository_id"], route_id=route.route_id,
                    route_configuration_version=route.configuration_version,
                    decision_space_id=space.decision_space_id,
                    decision_space_kind=space.kind, display_name=space.display_name,
                    catalog_breadcrumb=space.catalog_breadcrumb,
                    source_root=space.source_root,
                    compatibility_product_id=space.compatibility_product_id,
                    compatibility_product_name=space.compatibility_product_name,
                    source_boundary_digest=selection.source_boundary_digest,
                )
                ownership_json, ownership_digest = _canonical_record(ownership.to_dict())
                slice_id = capture_slice_id(request_id, route.route_id, route.configuration_version)
                connection.execute(
                    """INSERT INTO capture_slices(
                    request_id, slice_id, slice_order, route_id,
                    route_configuration_version, decision_space_id,
                    ownership_json, ownership_digest, matched_path_digest,
                    source_boundary_digest, state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned')""",
                    (request_id, slice_id, index, route.route_id,
                     route.configuration_version, route.decision_space_id,
                     ownership_json, ownership_digest, selection.matched_path_digest,
                     selection.source_boundary_digest),
                )
                views.append(CaptureSliceView(request_id, slice_id, index, ownership, "planned"))
            return tuple(views)

    def accept_slice_batch(
        self,
        device: Principal,
        request_id: str,
        slice_id: str,
        lease_token: str,
        batch: CandidateSliceBatchUpload,
        now: datetime,
    ) -> SliceUploadReceipt:
        """Atomically persist revisions, frozen ownership, Web associations, and one replay-stable receipt."""

        principal = _require_device(device)
        if not isinstance(batch, CandidateSliceBatchUpload):
            raise TypeError("batch must be a CandidateSliceBatchUpload")
        timestamp = _timestamp(now)
        batch_json, batch_record_digest = _canonical_record(batch.to_dict())
        connection = self.store.connection
        with _immediate(connection):
            group = _group_row(connection, request_id)
            _require_device_ownership(group, principal)
            _require_token(group, lease_token)
            slice_row = connection.execute(
                "SELECT * FROM capture_slices WHERE request_id = ? AND slice_id = ?",
                (request_id, slice_id),
            ).fetchone()
            if slice_row is None:
                raise RequestNotFound("capture_slice_not_found")
            if slice_row["receipt_json"] is not None:
                if slice_row["batch_json"] != batch_json or slice_row["batch_record_digest"] != batch_record_digest:
                    raise RequestConflict("slice_batch_conflict")
                return SliceUploadReceipt.from_dict(_read_canonical(
                    slice_row["receipt_json"], slice_row["receipt_digest"], "Slice receipt"
                ))
            _require_live_lease(group, principal, lease_token, now)
            if group["state"] not in ("claimed", "running"):
                raise InvalidTransition("capture_request_not_active")
            if (
                batch.request_id != request_id
                or batch.slice_id != slice_id
                or batch.route_id != slice_row["route_id"]
                or batch.route_configuration_version != slice_row["route_configuration_version"]
                or batch.decision_space_id != slice_row["decision_space_id"]
            ):
                raise RequestConflict("slice_ownership_conflict")
            ownership = _ownership_from_slice(slice_row)
            if any(item.content.product != ownership.compatibility_product_name for item in batch.items):
                raise RequestConflict("slice_content_ownership_conflict")
            family_ids = [item.family_id for item in batch.items]
            if len(set(family_ids)) != len(family_ids):
                raise RequestConflict("candidate_family_repeated")
            for item in batch.items:
                archived = connection.execute(
                    """SELECT 1 FROM candidate_family_archives WHERE organization_id = ?
                    AND repository_id = ? AND family_id = ?""",
                    (principal.organization_id, ownership.repository_id, item.family_id),
                ).fetchone()
                if archived is not None:
                    raise RequestConflict("candidate_family_archived")
                family_ownership = connection.execute(
                    """SELECT ownership_digest FROM candidate_revision_ownership
                    WHERE organization_id = ? AND repository_id = ? AND family_id = ?
                    LIMIT 1""",
                    (principal.organization_id, ownership.repository_id, item.family_id),
                ).fetchone()
                if family_ownership is not None and family_ownership["ownership_digest"] != slice_row["ownership_digest"]:
                    raise RequestConflict("candidate_family_ownership_conflict")
                _save_candidate_revision(connection, principal.organization_id,
                                         ownership.repository_id, request_id, item,
                                         timestamp, ownership.decision_space_id)
                record_candidate_revision_batch(
                    connection, principal.organization_id, ownership.repository_id,
                    request_id, item, timestamp, ownership
                )
                try:
                    connection.execute(
                        """INSERT INTO candidate_revision_ownership(
                        organization_id, repository_id, family_id, revision,
                        decision_space_id, route_id, route_configuration_version,
                        ownership_json, ownership_digest
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (principal.organization_id, ownership.repository_id,
                         item.family_id, item.revision, ownership.decision_space_id,
                         ownership.route_id, ownership.route_configuration_version,
                         slice_row["ownership_json"], slice_row["ownership_digest"]),
                    )
                except sqlite3.IntegrityError as error:
                    existing = connection.execute(
                        """SELECT ownership_digest FROM candidate_revision_ownership
                        WHERE organization_id = ? AND repository_id = ?
                        AND family_id = ? AND revision = ?""",
                        (principal.organization_id, ownership.repository_id,
                         item.family_id, item.revision),
                    ).fetchone()
                    if existing is None or existing["ownership_digest"] != slice_row["ownership_digest"]:
                        raise RequestConflict("candidate_revision_ownership_conflict") from error
            receipt_digest = hashlib.sha256(canonical_json_bytes({
                "request_id": request_id, "slice_id": slice_id,
                "candidate_count": len(batch.items), "batch_digest": batch.batch_digest,
            })).hexdigest()
            receipt = SliceUploadReceipt(request_id, slice_id, len(batch.items), receipt_digest)
            receipt_json = canonical_json_bytes(receipt.to_dict()).decode("utf-8")
            connection.execute(
                """UPDATE capture_slices SET state = 'accepted', batch_json = ?,
                batch_record_digest = ?, receipt_json = ?, receipt_digest = ?
                WHERE request_id = ? AND slice_id = ?""",
                (batch_json, batch_record_digest, receipt_json,
                 hashlib.sha256(receipt_json.encode("utf-8")).hexdigest(),
                 request_id, slice_id),
            )
            return receipt

    def complete_group(
        self,
        device: Principal,
        request_id: str,
        lease_token: str,
        receipt_digest: str,
        now: datetime,
    ) -> CaptureGroupView:
        principal = _require_device(device)
        digest = _require_digest(receipt_digest, "receipt_digest")
        timestamp = _timestamp(now)
        connection = self.store.connection
        with _immediate(connection):
            group = _group_row(connection, request_id)
            _require_device_ownership(group, principal)
            _require_token(group, lease_token)
            if group["state"] in _SUCCESS_STATES:
                if group["result_receipt_digest"] != digest:
                    raise InvalidTransition("completion_digest_conflict")
                return _group_view(group)
            _require_live_lease(group, principal, lease_token, now)
            rows = connection.execute(
                "SELECT * FROM capture_slices WHERE request_id = ? ORDER BY slice_order",
                (request_id,),
            ).fetchall()
            if not rows or any(row["receipt_json"] is None for row in rows):
                raise InvalidTransition("slice_receipts_required")
            receipts = tuple(SliceUploadReceipt.from_dict(_read_canonical(
                row["receipt_json"], row["receipt_digest"], "Slice receipt"
            )) for row in rows)
            expected = _aggregate_receipt_digest(receipts)
            if digest != expected:
                raise InvalidTransition("completion_digest_conflict")
            count = sum(receipt.candidate_count for receipt in receipts)
            state = "succeeded" if count else "succeeded_no_candidates"
            sequence = _next_sequence(group["last_sequence"])
            connection.execute(
                """UPDATE capture_groups SET state = ?, result_receipt_digest = ?,
                result_candidate_count = ?, terminal_code = ?, lease_expires_at = NULL,
                last_sequence = ?, updated_at = ? WHERE request_id = ?""",
                (state, digest, count,
                 "capture_succeeded" if count else "capture_succeeded_no_candidates",
                 sequence, timestamp, request_id),
            )
            _insert_group_event(connection, request_id, sequence, state,
                                "capture_succeeded" if count else "capture_succeeded_no_candidates",
                                timestamp)
            return _group_view(_group_row(connection, request_id))

    def group_request_view(
        self, principal: Principal, request_id: str
    ) -> CaptureRequestView:
        actor = principal
        if actor.kind == "device":
            actor = Principal("user", actor.organization_id, "device_compat", None)
        row = _group_row(self.store.connection, request_id)
        _authorize_organization(actor, row)
        mapping = self.store.connection.execute(
            """SELECT product_id, product_name FROM repository_mappings
            WHERE organization_id = ? AND repository_id = ?""",
            (row["organization_id"], row["repository_id"]),
        ).fetchone()
        if mapping is None:
            routes = _route_snapshot(row)
            if not routes:
                raise RepositoryUnavailable("repository_unavailable")
            space = self.store.decision_space(row["organization_id"], routes[0].decision_space_id)
            product_id = space.compatibility_product_id
            product_name = space.compatibility_product_name
        else:
            product_id = mapping["product_id"]
            product_name = mapping["product_name"]
        event = self.store.connection.execute(
            """SELECT code FROM capture_group_events WHERE request_id = ?
            ORDER BY sequence DESC LIMIT 1""", (request_id,)
        ).fetchone()
        return CaptureRequestView(
            request_id=request_id, repository_id=row["repository_id"],
            product_id=product_id, product_name=product_name,
            template_id=row["template_id"], state=row["state"],
            progress_code=event["code"],
            candidate_revision_count=row["result_candidate_count"],
            last_sequence=row["last_sequence"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def group_events_after(
        self, user: Principal, request_id: str, after_sequence: int
    ) -> tuple[ProgressEvent, ...]:
        principal = _require_user(user)
        group = _group_row(self.store.connection, request_id)
        _authorize_organization(principal, group)
        rows = self.store.connection.execute(
            """SELECT * FROM capture_group_events WHERE request_id = ?
            AND sequence > ? ORDER BY sequence""", (request_id, after_sequence)
        ).fetchall()
        return tuple(ProgressEvent(row["request_id"], row["sequence"], row["state"], row["code"], row["occurred_at"]) for row in rows)

    def start_group(
        self, device: Principal, request_id: str, lease_token: str, now: datetime
    ) -> ProgressEvent:
        principal = _require_device(device)
        timestamp = _timestamp(now)
        with _immediate(self.store.connection):
            row = _group_row(self.store.connection, request_id)
            _require_live_lease(row, principal, lease_token, now)
            if row["state"] != "claimed":
                raise InvalidTransition("capture_request_not_claimed")
            sequence = _next_sequence(row["last_sequence"])
            self.store.connection.execute(
                "UPDATE capture_groups SET state = 'running', last_sequence = ?, updated_at = ? WHERE request_id = ?",
                (sequence, timestamp, request_id),
            )
            _insert_group_event(self.store.connection, request_id, sequence, "running", "capture_started", timestamp)
            return ProgressEvent(request_id, sequence, "running", "capture_started", timestamp)

    def heartbeat_group(
        self, device: Principal, request_id: str, lease_token: str,
        now: datetime, lease_seconds: int = 30,
    ) -> ClaimedCaptureGroup:
        principal = _require_device(device)
        timestamp = _timestamp(now)
        expires_at = _timestamp(now + timedelta(seconds=_lease_seconds(lease_seconds)))
        with _immediate(self.store.connection):
            row = _group_row(self.store.connection, request_id)
            _require_live_lease(row, principal, lease_token, now)
            if row["state"] not in ("claimed", "running"):
                raise InvalidTransition("capture_request_not_active")
            self.store.connection.execute(
                "UPDATE capture_groups SET lease_expires_at = ?, updated_at = ? WHERE request_id = ?",
                (expires_at, timestamp, request_id),
            )
            return _claimed_group_view(_group_row(self.store.connection, request_id), lease_token)

    def record_group_progress(
        self, device: Principal, request_id: str, lease_token: str,
        code: str, now: datetime,
    ) -> ProgressEvent:
        principal = _require_device(device)
        progress_code = _require_code(code)
        timestamp = _timestamp(now)
        with _immediate(self.store.connection):
            row = _group_row(self.store.connection, request_id)
            _require_device_ownership(row, principal)
            _require_token(row, lease_token)
            if row["state"] != "running":
                raise InvalidTransition("capture_request_not_running")
            _require_live_lease(row, principal, lease_token, now)
            sequence = _next_sequence(row["last_sequence"])
            self.store.connection.execute(
                "UPDATE capture_groups SET last_sequence = ?, updated_at = ? WHERE request_id = ?",
                (sequence, timestamp, request_id),
            )
            _insert_group_event(self.store.connection, request_id, sequence, "running", progress_code, timestamp)
            return ProgressEvent(request_id, sequence, "running", progress_code, timestamp)

    def fail_group(
        self, device: Principal, request_id: str, lease_token: str,
        code: str, retryable: bool, now: datetime,
    ) -> CaptureGroupView:
        principal = _require_device(device)
        failure_code = _require_code(code)
        timestamp = _timestamp(now)
        with _immediate(self.store.connection):
            row = _group_row(self.store.connection, request_id)
            _require_live_lease(row, principal, lease_token, now)
            if row["state"] not in ("claimed", "running"):
                raise InvalidTransition("capture_request_not_active")
            can_retry = retryable and row["attempt_count"] <= len(_RETRY_DELAYS)
            state = "failed_retryable" if can_retry else "failed_terminal"
            terminal = None if can_retry else ("retry_exhausted" if retryable else failure_code)
            retry_at = None
            if can_retry:
                retry_at = _timestamp(now + timedelta(seconds=_RETRY_DELAYS[row["attempt_count"] - 1]))
            sequence = _next_sequence(row["last_sequence"])
            self.store.connection.execute(
                """UPDATE capture_groups SET state = ?, retry_at = ?, terminal_code = ?,
                claimed_device_id = NULL, lease_token_digest = NULL,
                lease_expires_at = NULL, last_sequence = ?, updated_at = ?
                WHERE request_id = ?""",
                (state, retry_at, terminal, sequence, timestamp, request_id),
            )
            _insert_group_event(self.store.connection, request_id, sequence, state,
                                "capture_failed_retryable" if can_retry else terminal,
                                timestamp)
            return _group_view(_group_row(self.store.connection, request_id))

    def accept_legacy_root_batch(
        self, device: Principal, lease_token: str,
        batch: CandidateBatchUpload, now: datetime,
    ) -> SliceUploadReceipt:
        group = _group_row(self.store.connection, batch.request_id)
        rows = self.store.connection.execute(
            "SELECT * FROM capture_slices WHERE request_id = ?", (batch.request_id,)
        ).fetchall()
        if not rows:
            routes = _route_snapshot(group)
            if len(routes) != 1 or routes[0].path_prefixes != (".",) or routes[0].excluded_prefixes:
                raise RequestConflict("legacy_batch_requires_trusted_root_route")
            marker = hashlib.sha256(b"v1-trusted-root-compatibility").hexdigest()
            slices = self.plan_slices(
                device, batch.request_id, lease_token,
                (RouteSelection(routes[0].route_id, routes[0].configuration_version, marker, marker),),
                now,
            )
            slice_view = slices[0]
        else:
            if len(rows) != 1:
                raise RequestConflict("legacy_batch_requires_trusted_root_route")
            slice_view = _slice_view(rows[0])
        adapted = CandidateSliceBatchUpload(
            request_id=batch.request_id, slice_id=slice_view.slice_id,
            route_id=slice_view.ownership.route_id,
            route_configuration_version=slice_view.ownership.route_configuration_version,
            decision_space_id=slice_view.ownership.decision_space_id,
            items=batch.items, batch_digest=batch.batch_digest,
        )
        try:
            return self.accept_slice_batch(
                device, batch.request_id, slice_view.slice_id, lease_token, adapted, now
            )
        except RequestConflict as error:
            translations = {
                "slice_batch_conflict": "batch_conflict",
                "slice_content_ownership_conflict": "candidate_product_mismatch",
            }
            raise RequestConflict(translations.get(error.code, error.code)) from None

    def complete_legacy_root_group(
        self, device: Principal, request_id: str, lease_token: str,
        batch_digest: str, now: datetime,
    ) -> CaptureGroupView:
        rows = self.store.connection.execute(
            "SELECT * FROM capture_slices WHERE request_id = ? ORDER BY slice_order",
            (request_id,),
        ).fetchall()
        if len(rows) != 1 or rows[0]["batch_json"] is None:
            raise InvalidTransition("candidate_batch_required")
        stored = CandidateSliceBatchUpload.from_dict(json.loads(rows[0]["batch_json"]))
        if stored.batch_digest != batch_digest:
            raise InvalidTransition("completion_digest_conflict")
        receipts = tuple(SliceUploadReceipt.from_dict(_read_canonical(
            row["receipt_json"], row["receipt_digest"], "Slice receipt"
        )) for row in rows)
        return self.complete_group(
            device, request_id, lease_token, _aggregate_receipt_digest(receipts), now
        )

    def _requeue_due_groups(
        self, connection: sqlite3.Connection, organization_id: str, now: datetime
    ) -> None:
        timestamp = _timestamp(now)
        rows = connection.execute(
            """SELECT * FROM capture_groups WHERE organization_id = ?
            AND ((state IN ('claimed','running') AND lease_expires_at <= ?)
              OR (state = 'failed_retryable' AND retry_at <= ?))""",
            (organization_id, timestamp, timestamp),
        ).fetchall()
        for row in rows:
            sequence = _next_sequence(row["last_sequence"])
            connection.execute(
                """UPDATE capture_groups SET state = 'queued', claimed_device_id = NULL,
                lease_token_digest = NULL, lease_expires_at = NULL,
                retry_at = NULL,
                last_sequence = ?, updated_at = ? WHERE request_id = ?""",
                (sequence, timestamp, row["request_id"]),
            )
            _insert_group_event(connection, row["request_id"], sequence,
                                "queued", "lease_expired_requeued", timestamp)

    def create_request(
        self,
        user: Principal,
        command: CaptureRequestCreate,
        now: datetime,
    ) -> CaptureRequestView:
        group = self.create_group(
            user,
            CaptureGroupCreate(
                repository_id=command.repository_id,
                template_id=command.template_id,
                capture_scope=command.capture_scope,
                client_action_id=command.client_action_id,
            ),
            now,
        )
        return self.group_request_view(user, group.request_id)

        # Legacy implementation retained below only as readable migration evidence.
        principal = _require_user(user)
        if not isinstance(command, CaptureRequestCreate):
            raise TypeError("command must be a CaptureRequestCreate")
        timestamp = _timestamp(now)
        connection = self.store.connection
        with _immediate(connection):
            action_row = connection.execute(
                """
                SELECT request.*
                FROM capture_request_actions AS action
                JOIN capture_requests AS request
                  ON request.request_id = action.request_id
                WHERE action.organization_id = ?
                  AND action.actor_id = ?
                  AND action.client_action_id = ?
                """,
                (
                    principal.organization_id,
                    principal.actor_id,
                    command.client_action_id,
                ),
            ).fetchone()
            if action_row is not None:
                if (
                    action_row["repository_id"] != command.repository_id
                    or action_row["template_id"] != command.template_id
                    or action_row["capture_scope"] != command.capture_scope
                ):
                    raise RequestConflict("capture_request_action_conflict")
                return _request_view(
                    _request_row(connection, action_row["request_id"])
                )

            active = connection.execute(
                """
                SELECT *
                FROM capture_requests
                WHERE organization_id = ?
                  AND repository_id = ?
                  AND state IN ('queued','claimed','running','failed_retryable')
                """,
                (principal.organization_id, command.repository_id),
            ).fetchone()
            if active is not None:
                raise RequestConflict("repository_capture_busy")

            mapping = connection.execute(
                """
                SELECT repository_id, product_id, product_name, enabled
                FROM repository_mappings
                WHERE organization_id = ? AND repository_id = ?
                """,
                (principal.organization_id, command.repository_id),
            ).fetchone()
            if mapping is None or not bool(mapping["enabled"]):
                raise RepositoryUnavailable("repository_unavailable")

            request_id = capture_request_id(
                principal.organization_id,
                command.repository_id,
                command.template_id,
                command.client_action_id,
            )
            try:
                connection.execute(
                    """
                    INSERT INTO capture_requests(
                        request_id, organization_id, actor_id, repository_id,
                        product_id, product_name, template_id, capture_scope,
                        client_action_id, state,
                        attempt_count, claimed_device_id, lease_token_digest,
                        lease_expires_at, retry_at, result_batch_digest,
                        terminal_code, last_sequence, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, NULL, NULL,
                              NULL, NULL, NULL, NULL, 1, ?, ?)
                    """,
                    (
                        request_id,
                        principal.organization_id,
                        principal.actor_id,
                        command.repository_id,
                        mapping["product_id"],
                        mapping["product_name"],
                        command.template_id,
                        command.capture_scope,
                        command.client_action_id,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO capture_request_actions(
                        organization_id, actor_id, client_action_id,
                        request_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        principal.organization_id,
                        principal.actor_id,
                        command.client_action_id,
                        request_id,
                        timestamp,
                    ),
                )
                _insert_event(
                    connection,
                    request_id=request_id,
                    sequence=1,
                    state="queued",
                    code="request_queued",
                    occurred_at=timestamp,
                )
            except sqlite3.IntegrityError as error:
                raise RequestConflict("capture_request_conflict") from error
            row = _request_row(connection, request_id)
            return _request_view(row)

    def get_request(
        self, user: Principal, request_id: str
    ) -> CaptureRequestView:
        if self.store.connection.execute(
            "SELECT 1 FROM capture_groups WHERE request_id = ?", (request_id,)
        ).fetchone() is not None:
            return self.group_request_view(user, request_id)
        principal = _require_user(user)
        _require_request_id(request_id)
        row = _request_row(self.store.connection, request_id)
        _authorize_organization(principal, row)
        return _request_view(row)

    def events_after(
        self,
        user: Principal,
        request_id: str,
        after_sequence: int,
    ) -> tuple[ProgressEvent, ...]:
        if self.store.connection.execute(
            "SELECT 1 FROM capture_groups WHERE request_id = ?", (request_id,)
        ).fetchone() is not None:
            return self.group_events_after(user, request_id, after_sequence)
        principal = _require_user(user)
        _require_request_id(request_id)
        if (
            not isinstance(after_sequence, int)
            or isinstance(after_sequence, bool)
            or not 0 <= after_sequence <= _MAX_SEQUENCE
        ):
            raise ValueError("after_sequence is invalid")
        request = _request_row(self.store.connection, request_id)
        _authorize_organization(principal, request)
        rows = self.store.connection.execute(
            """
            SELECT request_id, sequence, state, code, occurred_at
            FROM capture_request_events
            WHERE request_id = ? AND sequence > ?
            ORDER BY sequence
            """,
            (request_id, after_sequence),
        ).fetchall()
        return tuple(
            ProgressEvent(
                request_id=row["request_id"],
                sequence=row["sequence"],
                state=row["state"],
                code=row["code"],
                occurred_at=row["occurred_at"],
            )
            for row in rows
        )

    def claim_next(
        self,
        device: Principal,
        now: datetime,
        lease_seconds: int,
    ) -> ClaimedCaptureRequest | None:
        claimed_group = self.claim_next_group(device, now, lease_seconds)
        if claimed_group is not None:
            mapping = self.store.connection.execute(
                """SELECT product_id, product_name FROM repository_mappings
                WHERE organization_id = ? AND repository_id = ?""",
                (device.organization_id, claimed_group.repository_id),
            ).fetchone()
            if mapping is None:
                raise RepositoryUnavailable("repository_unavailable")
            return ClaimedCaptureRequest(
                request_id=claimed_group.request_id,
                repository_id=claimed_group.repository_id,
                product_id=mapping["product_id"],
                product_name=mapping["product_name"],
                template_id=claimed_group.template_id,
                capture_scope=claimed_group.capture_scope,
                client_action_id=claimed_group.client_action_id,
                lease_token=claimed_group.lease_token,
                lease_expires_at=claimed_group.lease_expires_at,
            )
        return None

        # Legacy queued rows are historical evidence and are never claimed.
        principal = _require_device(device)
        duration = _lease_seconds(lease_seconds)
        timestamp = _timestamp(now)
        connection = self.store.connection
        with _immediate(connection):
            self._requeue_due(connection, principal.organization_id, now)
            row = connection.execute(
                """
                SELECT request.*
                FROM capture_requests AS request
                JOIN repository_mappings AS mapping
                  ON mapping.organization_id = request.organization_id
                 AND mapping.repository_id = request.repository_id
                WHERE request.organization_id = ?
                  AND request.state = 'queued'
                  AND mapping.enabled = 1
                ORDER BY request.created_at, request.request_id
                LIMIT 1
                """,
                (principal.organization_id,),
            ).fetchone()
            if row is None:
                return None
            sequence = _next_sequence(row["last_sequence"])
            token = f"lease_{secrets.token_urlsafe(24)}"
            token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            expires_at = _timestamp(now + timedelta(seconds=duration))
            connection.execute(
                """
                UPDATE capture_requests
                SET state = 'claimed',
                    attempt_count = attempt_count + 1,
                    claimed_device_id = ?,
                    lease_token_digest = ?,
                    lease_expires_at = ?,
                    retry_at = NULL,
                    last_sequence = ?,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (
                    principal.device_id,
                    token_digest,
                    expires_at,
                    sequence,
                    timestamp,
                    row["request_id"],
                ),
            )
            _insert_event(
                connection,
                request_id=row["request_id"],
                sequence=sequence,
                state="claimed",
                code="device_claimed",
                occurred_at=timestamp,
            )
            updated = _request_row(connection, row["request_id"])
            return _claimed_view(updated, token)

    def start(
        self,
        device: Principal,
        request_id: str,
        lease_token: str,
        now: datetime,
    ) -> ProgressEvent:
        if self.store.connection.execute(
            "SELECT 1 FROM capture_groups WHERE request_id = ?", (request_id,)
        ).fetchone() is not None:
            return self.start_group(device, request_id, lease_token, now)
        raise RequestConflict("legacy_capture_read_only")
        principal = _require_device(device)
        timestamp = _timestamp(now)
        connection = self.store.connection
        with _immediate(connection):
            row = _request_row(connection, request_id)
            _require_live_lease(row, principal, lease_token, now)
            if row["state"] != "claimed":
                raise InvalidTransition("capture_request_not_claimed")
            sequence = _next_sequence(row["last_sequence"])
            connection.execute(
                """
                UPDATE capture_requests
                SET state = 'running', last_sequence = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (sequence, timestamp, request_id),
            )
            _insert_event(
                connection,
                request_id=request_id,
                sequence=sequence,
                state="running",
                code="capture_started",
                occurred_at=timestamp,
            )
            return ProgressEvent(
                request_id=request_id,
                sequence=sequence,
                state="running",
                code="capture_started",
                occurred_at=timestamp,
            )

    def heartbeat(
        self,
        device: Principal,
        request_id: str,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
    ) -> ClaimedCaptureRequest:
        if self.store.connection.execute(
            "SELECT 1 FROM capture_groups WHERE request_id = ?", (request_id,)
        ).fetchone() is not None:
            claimed = self.heartbeat_group(
                device, request_id, lease_token, now, lease_seconds
            )
            mapping = self.store.connection.execute(
                """SELECT product_id, product_name FROM repository_mappings
                WHERE organization_id = ? AND repository_id = ?""",
                (device.organization_id, claimed.repository_id),
            ).fetchone()
            return ClaimedCaptureRequest(
                claimed.request_id, claimed.repository_id, mapping["product_id"],
                mapping["product_name"], claimed.template_id,
                claimed.capture_scope, claimed.client_action_id,
                claimed.lease_token, claimed.lease_expires_at,
            )
        raise RequestConflict("legacy_capture_read_only")
        principal = _require_device(device)
        duration = _lease_seconds(lease_seconds)
        timestamp = _timestamp(now)
        connection = self.store.connection
        with _immediate(connection):
            row = _request_row(connection, request_id)
            _require_live_lease(row, principal, lease_token, now)
            if row["state"] not in ("claimed", "running"):
                raise InvalidTransition("capture_request_not_active")
            expires_at = _timestamp(now + timedelta(seconds=duration))
            connection.execute(
                """
                UPDATE capture_requests
                SET lease_expires_at = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (expires_at, timestamp, request_id),
            )
            updated = _request_row(connection, request_id)
            return _claimed_view(updated, lease_token)

    def record_progress(
        self,
        device: Principal,
        request_id: str,
        lease_token: str,
        code: str,
        now: datetime,
    ) -> ProgressEvent:
        if self.store.connection.execute(
            "SELECT 1 FROM capture_groups WHERE request_id = ?", (request_id,)
        ).fetchone() is not None:
            return self.record_group_progress(
                device, request_id, lease_token, code, now
            )
        raise RequestConflict("legacy_capture_read_only")
        principal = _require_device(device)
        progress_code = _require_code(code)
        timestamp = _timestamp(now)
        connection = self.store.connection
        with _immediate(connection):
            row = _request_row(connection, request_id)
            _require_device_ownership(row, principal)
            _require_token(row, lease_token)
            if row["state"] != "running":
                raise InvalidTransition("capture_request_not_running")
            _require_live_lease(row, principal, lease_token, now)
            sequence = _next_sequence(row["last_sequence"])
            connection.execute(
                """
                UPDATE capture_requests
                SET last_sequence = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (sequence, timestamp, request_id),
            )
            _insert_event(
                connection,
                request_id=request_id,
                sequence=sequence,
                state="running",
                code=progress_code,
                occurred_at=timestamp,
            )
            return ProgressEvent(
                request_id=request_id,
                sequence=sequence,
                state="running",
                code=progress_code,
                occurred_at=timestamp,
            )

    def accept_candidate_batch(
        self,
        device: Principal,
        lease_token: str,
        batch: CandidateBatchUpload,
        now: datetime,
    ) -> UploadReceipt:
        if self.store.connection.execute(
            "SELECT 1 FROM capture_groups WHERE request_id = ?", (batch.request_id,)
        ).fetchone() is not None:
            self.accept_legacy_root_batch(device, lease_token, batch, now)
            return UploadReceipt(
                request_id=batch.request_id,
                batch_digest=batch.batch_digest,
                acknowledged_at=_timestamp(now),
            )
        raise RequestConflict("legacy_capture_read_only")
        principal = _require_device(device)
        if not isinstance(batch, CandidateBatchUpload):
            raise TypeError("batch must be a CandidateBatchUpload")
        timestamp = _timestamp(now)
        batch_json, batch_record_digest = _canonical_record(
            batch.to_dict()
        )
        connection = self.store.connection
        with _immediate(connection):
            row = _request_row(connection, batch.request_id)
            _require_device_ownership(row, principal)
            _require_token(row, lease_token)
            existing = connection.execute(
                """
                SELECT batch_digest, batch_json, batch_record_digest,
                       receipt_json, receipt_digest
                FROM candidate_batches
                WHERE request_id = ?
                """,
                (batch.request_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["batch_digest"] != batch.batch_digest
                    or existing["batch_json"] != batch_json
                    or existing["batch_record_digest"]
                    != batch_record_digest
                ):
                    raise RequestConflict("batch_conflict")
                return UploadReceipt.from_dict(
                    _read_canonical(
                        existing["receipt_json"],
                        existing["receipt_digest"],
                        "Upload receipt",
                    )
                )

            _require_live_lease(
                row, principal, lease_token, now
            )
            if row["state"] != "running":
                raise InvalidTransition(
                    "capture_request_not_running"
                )
            if batch.repository_id != row["repository_id"]:
                raise RequestConflict("batch_repository_conflict")
            if any(
                item.content.product != row["product_name"]
                for item in batch.items
            ):
                raise RequestConflict(
                    "candidate_product_mismatch"
                )
            family_ids = [
                item.family_id for item in batch.items
            ]
            if len(set(family_ids)) != len(family_ids):
                raise RequestConflict(
                    "candidate_family_repeated"
                )
            for item in batch.items:
                _save_candidate_revision(
                    connection,
                    principal.organization_id,
                    batch.repository_id,
                    batch.request_id,
                    item,
                    timestamp,
                )
            receipt = UploadReceipt(
                request_id=batch.request_id,
                batch_digest=batch.batch_digest,
                acknowledged_at=timestamp,
            )
            receipt_json, receipt_digest = _canonical_record(
                receipt.to_dict()
            )
            connection.execute(
                """
                INSERT INTO candidate_batches(
                    request_id, organization_id, repository_id,
                    batch_digest, batch_json, batch_record_digest,
                    item_count, receipt_json, receipt_digest,
                    acknowledged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch.request_id,
                    principal.organization_id,
                    batch.repository_id,
                    batch.batch_digest,
                    batch_json,
                    batch_record_digest,
                    len(batch.items),
                    receipt_json,
                    receipt_digest,
                    timestamp,
                ),
            )
            return receipt

    def complete(
        self,
        device: Principal,
        request_id: str,
        lease_token: str,
        batch_digest: str,
        now: datetime,
    ) -> CaptureRequestView:
        if self.store.connection.execute(
            "SELECT 1 FROM capture_groups WHERE request_id = ?", (request_id,)
        ).fetchone() is not None:
            self.complete_legacy_root_group(
                device, request_id, lease_token, batch_digest, now
            )
            return self.group_request_view(device, request_id)
        raise RequestConflict("legacy_capture_read_only")
        principal = _require_device(device)
        digest = _require_digest(batch_digest, "batch_digest")
        timestamp = _timestamp(now)
        connection = self.store.connection
        with _immediate(connection):
            row = _request_row(connection, request_id)
            _require_device_ownership(row, principal)
            _require_token(row, lease_token)
            if row["state"] in _SUCCESS_STATES:
                if row["result_batch_digest"] != digest:
                    raise InvalidTransition("completion_digest_conflict")
                return _request_view(row)
            _require_live_lease(row, principal, lease_token, now)
            if row["state"] != "running":
                raise InvalidTransition("capture_request_not_running")
            stored_batch = connection.execute(
                """
                SELECT batch_digest, item_count
                FROM candidate_batches
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if stored_batch is None:
                raise InvalidTransition(
                    "candidate_batch_required"
                )
            if stored_batch["batch_digest"] != digest:
                raise InvalidTransition(
                    "completion_digest_conflict"
                )
            state = (
                "succeeded_no_candidates"
                if stored_batch["item_count"] == 0
                else "succeeded"
            )
            code = (
                "capture_succeeded_no_candidates"
                if state == "succeeded_no_candidates"
                else "capture_succeeded"
            )
            sequence = _next_sequence(row["last_sequence"])
            connection.execute(
                """
                UPDATE capture_requests
                SET state = ?,
                    lease_expires_at = NULL,
                    result_batch_digest = ?,
                    result_candidate_count = ?,
                    terminal_code = ?,
                    last_sequence = ?,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (
                    state, digest, stored_batch["item_count"], code, sequence,
                    timestamp, request_id,
                ),
            )
            _insert_event(
                connection,
                request_id=request_id,
                sequence=sequence,
                state=state,
                code=code,
                occurred_at=timestamp,
            )
            return _request_view(_request_row(connection, request_id))

    def fail(
        self,
        device: Principal,
        request_id: str,
        lease_token: str,
        code: str,
        retryable: bool,
        now: datetime,
    ) -> CaptureRequestView:
        if self.store.connection.execute(
            "SELECT 1 FROM capture_groups WHERE request_id = ?", (request_id,)
        ).fetchone() is not None:
            self.fail_group(
                device, request_id, lease_token, code, retryable, now
            )
            return self.group_request_view(device, request_id)
        raise RequestConflict("legacy_capture_read_only")
        principal = _require_device(device)
        failure_code = _require_code(code)
        if not isinstance(retryable, bool):
            raise ValueError("retryable is invalid")
        timestamp = _timestamp(now)
        connection = self.store.connection
        with _immediate(connection):
            row = _request_row(connection, request_id)
            _require_live_lease(row, principal, lease_token, now)
            if row["state"] not in ("claimed", "running"):
                raise InvalidTransition("capture_request_not_active")
            attempt_count = row["attempt_count"]
            if retryable and attempt_count < 5:
                state = "failed_retryable"
                event_code = failure_code
                retry_at = _timestamp(
                    now + timedelta(seconds=_RETRY_DELAYS[attempt_count - 1])
                )
                terminal_code = None
            else:
                state = "failed_terminal"
                event_code = "retry_exhausted" if retryable else failure_code
                retry_at = None
                terminal_code = event_code
            sequence = _next_sequence(row["last_sequence"])
            connection.execute(
                """
                UPDATE capture_requests
                SET state = ?,
                    claimed_device_id = NULL,
                    lease_token_digest = NULL,
                    lease_expires_at = NULL,
                    retry_at = ?,
                    terminal_code = ?,
                    last_sequence = ?,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (
                    state,
                    retry_at,
                    terminal_code,
                    sequence,
                    timestamp,
                    request_id,
                ),
            )
            _insert_event(
                connection,
                request_id=request_id,
                sequence=sequence,
                state=state,
                code=event_code,
                occurred_at=timestamp,
            )
            return _request_view(_request_row(connection, request_id))

    def _requeue_due(
        self,
        connection: sqlite3.Connection,
        organization_id: str,
        now: datetime,
    ) -> None:
        timestamp = _timestamp(now)
        expired = connection.execute(
            """
            SELECT *
            FROM capture_requests
            WHERE organization_id = ?
              AND state IN ('claimed','running')
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            ORDER BY created_at, request_id
            """,
            (organization_id, timestamp),
        ).fetchall()
        for row in expired:
            sequence = _next_sequence(row["last_sequence"])
            if row["attempt_count"] >= 5:
                state = "failed_terminal"
                code = "retry_exhausted"
                terminal_code = code
            else:
                state = "queued"
                code = "lease_expired_requeued"
                terminal_code = None
            connection.execute(
                """
                UPDATE capture_requests
                SET state = ?,
                    claimed_device_id = NULL,
                    lease_token_digest = NULL,
                    lease_expires_at = NULL,
                    retry_at = NULL,
                    terminal_code = ?,
                    last_sequence = ?,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (
                    state,
                    terminal_code,
                    sequence,
                    timestamp,
                    row["request_id"],
                ),
            )
            _insert_event(
                connection,
                request_id=row["request_id"],
                sequence=sequence,
                state=state,
                code=code,
                occurred_at=timestamp,
            )

        retryable = connection.execute(
            """
            SELECT *
            FROM capture_requests
            WHERE organization_id = ?
              AND state = 'failed_retryable'
              AND retry_at IS NOT NULL
              AND retry_at <= ?
            ORDER BY created_at, request_id
            """,
            (organization_id, timestamp),
        ).fetchall()
        for row in retryable:
            sequence = _next_sequence(row["last_sequence"])
            connection.execute(
                """
                UPDATE capture_requests
                SET state = 'queued',
                    retry_at = NULL,
                    last_sequence = ?,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (sequence, timestamp, row["request_id"]),
            )
            _insert_event(
                connection,
                request_id=row["request_id"],
                sequence=sequence,
                state="queued",
                code="retry_ready",
                occurred_at=timestamp,
            )


def _require_user(principal: Principal) -> Principal:
    if not isinstance(principal, Principal) or principal.kind != "user":
        raise AccessDenied("user_principal_required")
    return principal


def _require_device(principal: Principal) -> Principal:
    if not isinstance(principal, Principal) or principal.kind != "device":
        raise AccessDenied("device_principal_required")
    return principal


def _authorize_organization(principal: Principal, row: sqlite3.Row) -> None:
    if row["organization_id"] != principal.organization_id:
        raise AccessDenied("organization_access_denied")


def _require_device_ownership(row: sqlite3.Row, device: Principal) -> None:
    _authorize_organization(device, row)
    if row["claimed_device_id"] != device.device_id:
        raise AccessDenied("device_access_denied")


def _require_token(row: sqlite3.Row, lease_token: str) -> None:
    if not isinstance(lease_token, str) or not lease_token:
        raise InvalidLease("lease_invalid")
    expected = row["lease_token_digest"]
    supplied = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
    if not isinstance(expected, str) or not hmac.compare_digest(supplied, expected):
        raise InvalidLease("lease_invalid")


def _require_live_lease(
    row: sqlite3.Row,
    device: Principal,
    lease_token: str,
    now: datetime,
) -> None:
    _require_device_ownership(row, device)
    _require_token(row, lease_token)
    expires_at = row["lease_expires_at"]
    if expires_at is None or now >= _parse_timestamp(expires_at):
        raise InvalidLease("lease_expired")


def _request_row(
    connection: sqlite3.Connection, request_id: str
) -> sqlite3.Row:
    _require_request_id(request_id)
    row = connection.execute(
        """
        SELECT request.*, event.code AS progress_code
        FROM capture_requests AS request
        JOIN capture_request_events AS event
          ON event.request_id = request.request_id
         AND event.sequence = request.last_sequence
        WHERE request.request_id = ?
        """,
        (request_id,),
    ).fetchone()
    if row is None:
        raise RequestNotFound("capture_request_not_found")
    return row


def _group_row(connection: sqlite3.Connection, request_id: str) -> sqlite3.Row:
    _require_request_id(request_id)
    row = connection.execute(
        "SELECT * FROM capture_groups WHERE request_id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise RequestNotFound("capture_request_not_found")
    return row


def _group_view(row: sqlite3.Row) -> CaptureGroupView:
    return CaptureGroupView(
        request_id=row["request_id"],
        repository_id=row["repository_id"],
        template_id=row["template_id"],
        capture_scope=row["capture_scope"],
        client_action_id=row["client_action_id"],
        state=row["state"],
        last_sequence=row["last_sequence"],
    )


def _route_snapshot(row: sqlite3.Row) -> tuple[RepositoryDecisionRoute, ...]:
    value = _read_canonical(
        row["route_snapshot_json"], row["route_snapshot_digest"], "Route snapshot"
    )
    if not isinstance(value, dict) or frozenset(value) != frozenset(("routes",)):
        raise ValueError("route_snapshot_corrupt")
    routes = value["routes"]
    if not isinstance(routes, list):
        raise ValueError("route_snapshot_corrupt")
    return tuple(RepositoryDecisionRoute.from_dict(route) for route in routes)


def _claimed_group_view(row: sqlite3.Row, token: str) -> ClaimedCaptureGroup:
    return ClaimedCaptureGroup(
        request_id=row["request_id"],
        repository_id=row["repository_id"],
        template_id=row["template_id"],
        capture_scope=row["capture_scope"],
        client_action_id=row["client_action_id"],
        route_snapshot=_route_snapshot(row),
        route_snapshot_digest=row["route_snapshot_digest"],
        lease_token=token,
        lease_expires_at=row["lease_expires_at"],
    )


def _ownership_from_slice(row: sqlite3.Row) -> CandidateOwnershipSnapshot:
    return CandidateOwnershipSnapshot.from_dict(
        _read_canonical(row["ownership_json"], row["ownership_digest"], "Slice ownership")
    )


def _slice_view(row: sqlite3.Row) -> CaptureSliceView:
    return CaptureSliceView(
        request_id=row["request_id"],
        slice_id=row["slice_id"],
        slice_order=row["slice_order"],
        ownership=_ownership_from_slice(row),
        state=row["state"],
    )


def _insert_group_event(
    connection: sqlite3.Connection,
    request_id: str,
    sequence: int,
    state: str,
    code: str,
    occurred_at: str,
) -> None:
    connection.execute(
        """INSERT INTO capture_group_events(
        request_id, sequence, state, code, occurred_at
        ) VALUES (?, ?, ?, ?, ?)""",
        (request_id, sequence, state, code, occurred_at),
    )


def _aggregate_receipt_digest(
    receipts: Sequence[SliceUploadReceipt],
) -> str:
    return hashlib.sha256(canonical_json_bytes({
        "receipts": [receipt.to_dict() for receipt in receipts]
    })).hexdigest()


def _request_view(row: sqlite3.Row) -> CaptureRequestView:
    return CaptureRequestView(
        request_id=row["request_id"],
        repository_id=row["repository_id"],
        product_id=row["product_id"],
        product_name=row["product_name"],
        template_id=row["template_id"],
        state=row["state"],
        progress_code=row["progress_code"],
        candidate_revision_count=(
            row["result_candidate_count"]
            if row["state"] in _SUCCESS_STATES
            else None
        ),
        last_sequence=row["last_sequence"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _claimed_view(
    row: sqlite3.Row, lease_token: str
) -> ClaimedCaptureRequest:
    return ClaimedCaptureRequest(
        request_id=row["request_id"],
        repository_id=row["repository_id"],
        product_id=row["product_id"],
        product_name=row["product_name"],
        template_id=row["template_id"],
        capture_scope=row["capture_scope"],
        client_action_id=row["client_action_id"],
        lease_token=lease_token,
        lease_expires_at=row["lease_expires_at"],
    )


def _insert_event(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    sequence: int,
    state: str,
    code: str,
    occurred_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO capture_request_events(
            request_id, sequence, state, code, occurred_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (request_id, sequence, state, code, occurred_at),
    )


def _next_sequence(current: int) -> int:
    if (
        not isinstance(current, int)
        or isinstance(current, bool)
        or current < 0
        or current >= _MAX_SEQUENCE
    ):
        raise SequenceOverflow("capture_event_sequence_overflow")
    return current + 1


def _require_request_id(value: str) -> None:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise ValueError("request_id is invalid")


def _require_code(value: str) -> str:
    if not isinstance(value, str) or _CODE.fullmatch(value) is None:
        raise ValueError("code is invalid")
    return value


def _require_digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _save_candidate_revision(
    connection: sqlite3.Connection,
    organization_id: str,
    repository_id: str,
    request_id: str,
    item: CandidateRevisionUpload,
    observed_at: str,
    decision_space_id: str = "legacy_unassigned",
) -> None:
    record_json, record_digest = _canonical_record(
        item.to_dict()
    )
    rows = connection.execute(
        """
        SELECT organization_id, repository_id, family_id, revision,
               revision_id, record_json, record_digest
        FROM candidate_revisions
        WHERE (
            organization_id = ?
            AND repository_id = ?
            AND family_id = ?
            AND revision = ?
        ) OR (
            organization_id = ? AND revision_id = ?
        )
        """,
        (
            organization_id,
            repository_id,
            item.family_id,
            item.revision,
            organization_id,
            item.revision_id,
        ),
    ).fetchall()
    for row in rows:
        if (
            row["organization_id"] != organization_id
            or row["repository_id"] != repository_id
            or row["family_id"] != item.family_id
            or row["revision"] != item.revision
            or row["revision_id"] != item.revision_id
            or row["record_json"] != record_json
            or row["record_digest"] != record_digest
        ):
            raise RequestConflict(
                "candidate_revision_conflict"
            )
    if not rows:
        try:
            connection.execute(
                """
                INSERT INTO candidate_revisions(
                    organization_id, repository_id, family_id,
                    revision, revision_id, record_json, record_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    organization_id,
                    repository_id,
                    item.family_id,
                    item.revision,
                    item.revision_id,
                    record_json,
                    record_digest,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise RequestConflict(
                "candidate_revision_conflict"
            ) from error

    head = connection.execute(
        """
        SELECT revision, revision_id
        FROM candidate_family_heads
        WHERE organization_id = ?
          AND repository_id = ?
          AND decision_space_id = ?
          AND family_id = ?
        """,
        (organization_id, repository_id, decision_space_id, item.family_id),
    ).fetchone()
    if head is None:
        if item.revision != 1:
            raise RequestConflict(
                "candidate_revision_not_monotonic"
            )
        connection.execute(
            """
            INSERT INTO candidate_family_heads(
                organization_id, repository_id, decision_space_id, family_id,
                revision, revision_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                organization_id,
                repository_id,
                decision_space_id,
                item.family_id,
                item.revision,
                item.revision_id,
            ),
        )
        record_candidate_revision_batch(
            connection, organization_id, repository_id, request_id, item, observed_at
        )
        return
    if head["revision_id"] == item.revision_id:
        record_candidate_revision_batch(
            connection, organization_id, repository_id, request_id, item, observed_at
        )
        return
    if item.revision != head["revision"] + 1:
        raise RequestConflict(
            "candidate_revision_not_monotonic"
        )
    connection.execute(
        """
        UPDATE candidate_family_heads
        SET revision = ?, revision_id = ?
        WHERE organization_id = ?
          AND repository_id = ?
          AND decision_space_id = ?
          AND family_id = ?
        """,
        (
            item.revision,
            item.revision_id,
            organization_id,
            repository_id,
            decision_space_id,
            item.family_id,
        ),
    )
    record_candidate_revision_batch(
        connection, organization_id, repository_id, request_id, item, observed_at
    )


def _canonical_record(value: object) -> tuple[str, str]:
    payload = canonical_json_bytes(value)
    return payload.decode("utf-8"), hashlib.sha256(payload).hexdigest()


def _read_canonical(
    payload: object,
    expected_digest: object,
    record_name: str,
) -> object:
    if (
        not isinstance(payload, str)
        or not isinstance(expected_digest, str)
        or _DIGEST.fullmatch(expected_digest) is None
    ):
        raise RequestConflict("central_candidate_state_corrupt")
    encoded = payload.encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != expected_digest:
        raise RequestConflict("central_candidate_state_corrupt")
    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise RequestConflict(
            "central_candidate_state_corrupt"
        ) from error
    if canonical_json_bytes(value) != encoded:
        raise RequestConflict("central_candidate_state_corrupt")
    return value


def _lease_seconds(value: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 3600
    ):
        raise ValueError("lease_seconds is invalid")
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise RequestConflict("stored_timestamp_invalid") from error
    if parsed.tzinfo is None:
        raise RequestConflict("stored_timestamp_invalid")
    return parsed


@contextmanager
def _immediate(
    connection: sqlite3.Connection,
) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
