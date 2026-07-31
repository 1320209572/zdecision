"""Durable Capture Request lifecycle and authorization rules."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Iterator

from zdecision.central.auth import Principal
from zdecision.central.store import CentralStore
from zdecision.ids import capture_request_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CaptureRequestCreate,
    CaptureRequestView,
    ClaimedCaptureRequest,
    ProgressEvent,
    RepositoryView,
)


_REQUEST_ID = re.compile(r"^crq_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ACTIVE_STATES = ("queued", "claimed", "running", "failed_retryable")
_SUCCESS_STATES = ("succeeded", "succeeded_no_candidates")
_MAX_SEQUENCE = 2_147_483_647
_RETRY_DELAYS = (5, 30, 120, 300)
_EMPTY_BATCH_DIGEST = hashlib.sha256(
    canonical_json_bytes({"items": []})
).hexdigest()


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

    def create_request(
        self,
        user: Principal,
        command: CaptureRequestCreate,
        now: datetime,
    ) -> CaptureRequestView:
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
                return _request_view(action_row)

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
                        active["request_id"],
                        timestamp,
                    ),
                )
                return _request_view(active)

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
                        product_id, product_name, template_id, state,
                        attempt_count, claimed_device_id, lease_token_digest,
                        lease_expires_at, retry_at, result_batch_digest,
                        terminal_code, last_sequence, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, NULL, NULL,
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

    def complete(
        self,
        device: Principal,
        request_id: str,
        lease_token: str,
        batch_digest: str,
        now: datetime,
    ) -> CaptureRequestView:
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
            state = (
                "succeeded_no_candidates"
                if digest == _EMPTY_BATCH_DIGEST
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
                    terminal_code = ?,
                    last_sequence = ?,
                    updated_at = ?
                WHERE request_id = ?
                """,
                (state, digest, code, sequence, timestamp, request_id),
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
        "SELECT * FROM capture_requests WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        raise RequestNotFound("capture_request_not_found")
    return row


def _request_view(row: sqlite3.Row) -> CaptureRequestView:
    return CaptureRequestView(
        request_id=row["request_id"],
        repository_id=row["repository_id"],
        product_id=row["product_id"],
        product_name=row["product_name"],
        template_id=row["template_id"],
        state=row["state"],
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
