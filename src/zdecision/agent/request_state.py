"""Durable intent journal for native app-server mutations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from zdecision.app_server.jsonl import AppServerRequestError
from zdecision.app_server.models import AppServerTurnReceipt
from zdecision.capture.reconciliation import (
    CandidateFamilyRevision,
    ReconciliationResult,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CandidateBatchUpload,
    CandidateRevisionUpload,
    UploadReceipt,
)


NativeStage = Literal[
    "capture_fork",
    "inventory",
    "extraction",
    "reconciliation_thread",
    "reconciliation_turn",
]
NativeAttemptState = Literal["prepared", "pending", "attached", "completed"]

_REQUEST_ID = re.compile(r"^crq_[0-9a-f]{32}$")
_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STAGES = frozenset(
    (
        "capture_fork",
        "inventory",
        "extraction",
        "reconciliation_thread",
        "reconciliation_turn",
    )
)


class RequestStateError(Exception):
    """Base class for native-attempt state failures."""


class NativeAttemptNotFound(RequestStateError):
    """A requested native attempt has not been prepared."""


class NativeAttemptConflict(RequestStateError):
    """A replay disagrees with the durably recorded native attempt."""


class CaptureResultUnknown(RequestStateError):
    """An external mutation may have succeeded but cannot yet be adopted."""


class ReconciliationConflict(RequestStateError):
    """A reconciliation replay disagrees with durable local state."""


class BatchConflict(RequestStateError):
    """A Candidate batch or upload receipt conflicts with durable state."""


class RequestStateCorrupt(RequestStateError):
    """Persisted canonical data failed its digest or typed contract."""


@dataclass(frozen=True)
class NativeAttempt:
    request_id: str
    operation_key: str
    stage: NativeStage
    stable_tag: str
    state: NativeAttemptState
    native_id: str | None
    output_digest: str | None


class RequestStateStore:
    """Persist app-server intent before any non-idempotent native call."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> "RequestStateStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS native_attempts (
                    request_id TEXT NOT NULL,
                    operation_key TEXT NOT NULL,
                    stage TEXT NOT NULL CHECK(stage IN (
                        'capture_fork',
                        'inventory',
                        'extraction',
                        'reconciliation_thread',
                        'reconciliation_turn'
                    )),
                    stable_tag TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'prepared',
                        'pending',
                        'attached',
                        'completed'
                    )),
                    native_id TEXT,
                    output_digest TEXT,
                    PRIMARY KEY(request_id, operation_key, stage)
                );

                CREATE TABLE IF NOT EXISTS candidate_family_revisions (
                    repository_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    revision_id TEXT NOT NULL UNIQUE,
                    record_json TEXT NOT NULL,
                    record_digest TEXT NOT NULL,
                    PRIMARY KEY(repository_id, family_id, revision)
                );

                CREATE TABLE IF NOT EXISTS candidate_family_heads (
                    repository_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    PRIMARY KEY(repository_id, family_id)
                );

                CREATE TABLE IF NOT EXISTS reconciliation_results (
                    request_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    result_digest TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS candidate_outbox (
                    request_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    revisions_json TEXT NOT NULL,
                    revisions_digest TEXT NOT NULL,
                    batch_json TEXT NOT NULL,
                    batch_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'pending',
                        'uploaded'
                    )),
                    receipt_json TEXT,
                    receipt_digest TEXT
                );
                """
            )
        return cls(database_path, connection)

    def close(self) -> None:
        self._connection.close()

    def get_native_attempt(
        self,
        request_id: str,
        operation_key: str,
        stage: str,
    ) -> NativeAttempt | None:
        identity = _identity(request_id, operation_key, stage)
        row = self._connection.execute(
            """
            SELECT request_id, operation_key, stage, stable_tag, state,
                   native_id, output_digest
            FROM native_attempts
            WHERE request_id = ? AND operation_key = ? AND stage = ?
            """,
            identity,
        ).fetchone()
        return None if row is None else _attempt(row)

    def get_or_create_native_attempt(
        self,
        request_id: str,
        operation_key: str,
        stage: str,
        stable_tag: str,
    ) -> NativeAttempt:
        identity = _identity(request_id, operation_key, stage)
        tag = _safe_value(stable_tag, "stable_tag")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._row(identity)
            if existing is not None:
                attempt = _attempt(existing)
                if attempt.stable_tag != tag:
                    raise NativeAttemptConflict(
                        "Native attempt stable tag conflicts"
                    )
                self._connection.commit()
                return attempt
            self._connection.execute(
                """
                INSERT INTO native_attempts(
                    request_id, operation_key, stage, stable_tag, state,
                    native_id, output_digest
                ) VALUES (?, ?, ?, ?, 'prepared', NULL, NULL)
                """,
                (*identity, tag),
            )
            attempt = _attempt(self._required_row(identity))
            self._connection.commit()
            return attempt
        except Exception:
            self._connection.rollback()
            raise

    def mark_native_pending(
        self,
        request_id: str,
        operation_key: str,
        stage: str,
    ) -> NativeAttempt:
        identity = _identity(request_id, operation_key, stage)
        return self._transition(
            identity,
            allowed=frozenset(("prepared", "pending")),
            target="pending",
        )

    def attach_native_result(
        self,
        request_id: str,
        operation_key: str,
        stage: str,
        native_id: str,
    ) -> NativeAttempt:
        identity = _identity(request_id, operation_key, stage)
        result_id = _safe_value(native_id, "native_id")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = _attempt(self._required_row(identity))
            if current.state == "prepared":
                raise NativeAttemptConflict(
                    "Native result cannot attach before pending"
                )
            if current.native_id is not None:
                if current.native_id != result_id:
                    raise NativeAttemptConflict(
                        "Native attempt result conflicts"
                    )
                self._connection.commit()
                return current
            if current.state != "pending":
                raise NativeAttemptConflict("Native attempt state conflicts")
            self._connection.execute(
                """
                UPDATE native_attempts
                SET state = 'attached', native_id = ?
                WHERE request_id = ? AND operation_key = ? AND stage = ?
                """,
                (result_id, *identity),
            )
            attached = _attempt(self._required_row(identity))
            self._connection.commit()
            return attached
        except Exception:
            self._connection.rollback()
            raise

    def complete_native_attempt(
        self,
        request_id: str,
        operation_key: str,
        stage: str,
        output_digest: str,
    ) -> NativeAttempt:
        identity = _identity(request_id, operation_key, stage)
        digest = _digest(output_digest)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = _attempt(self._required_row(identity))
            if current.native_id is None or current.state not in {
                "attached",
                "completed",
            }:
                raise NativeAttemptConflict(
                    "Native attempt cannot complete before attachment"
                )
            if current.output_digest is not None:
                if current.output_digest != digest:
                    raise NativeAttemptConflict(
                        "Native attempt output digest conflicts"
                    )
                self._connection.commit()
                return current
            self._connection.execute(
                """
                UPDATE native_attempts
                SET state = 'completed', output_digest = ?
                WHERE request_id = ? AND operation_key = ? AND stage = ?
                """,
                (digest, *identity),
            )
            completed = _attempt(self._required_row(identity))
            self._connection.commit()
            return completed
        except Exception:
            self._connection.rollback()
            raise

    def reset_native_after_rejection(
        self,
        request_id: str,
        operation_key: str,
        stage: str,
    ) -> NativeAttempt:
        identity = _identity(request_id, operation_key, stage)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = _attempt(self._required_row(identity))
            if current.state == "prepared":
                self._connection.commit()
                return current
            if current.state != "pending" or current.native_id is not None:
                raise NativeAttemptConflict(
                    "Attached native attempts cannot be reset"
                )
            self._connection.execute(
                """
                UPDATE native_attempts
                SET state = 'prepared', native_id = NULL, output_digest = NULL
                WHERE request_id = ? AND operation_key = ? AND stage = ?
                """,
                identity,
            )
            reset = _attempt(self._required_row(identity))
            self._connection.commit()
            return reset
        except Exception:
            self._connection.rollback()
            raise

    def current_families(
        self, repository_id: str
    ) -> tuple[CandidateFamilyRevision, ...]:
        repository = _repository_id(repository_id)
        rows = self._connection.execute(
            """
            SELECT revisions.record_json, revisions.record_digest
            FROM candidate_family_heads AS heads
            JOIN candidate_family_revisions AS revisions
              ON revisions.repository_id = heads.repository_id
             AND revisions.family_id = heads.family_id
             AND revisions.revision_id = heads.revision_id
            WHERE heads.repository_id = ?
            ORDER BY heads.family_id
            """,
            (repository,),
        ).fetchall()
        return tuple(
            CandidateFamilyRevision.from_dict(
                _read_canonical(
                    row["record_json"],
                    row["record_digest"],
                    "Candidate family revision",
                )
            )
            for row in rows
        )

    def get_reconciliation(
        self, request_id: str
    ) -> ReconciliationResult | None:
        request = _request_id(request_id)
        row = self._connection.execute(
            """
            SELECT result_json, result_digest
            FROM reconciliation_results
            WHERE request_id = ?
            """,
            (request,),
        ).fetchone()
        if row is None:
            return None
        return ReconciliationResult.from_dict(
            _read_canonical(
                row["result_json"],
                row["result_digest"],
                "Reconciliation result",
            )
        )

    def save_reconciliation(
        self,
        request_id: str,
        result: ReconciliationResult,
    ) -> None:
        request = _request_id(request_id)
        if not isinstance(result, ReconciliationResult):
            raise TypeError("result must be a ReconciliationResult")
        result_json, result_digest = _canonical_record(
            result.to_dict()
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing_result = self._connection.execute(
                """
                SELECT repository_id, result_json, result_digest
                FROM reconciliation_results
                WHERE request_id = ?
                """,
                (request,),
            ).fetchone()
            if existing_result is not None:
                if (
                    existing_result["repository_id"]
                    != result.repository_id
                    or existing_result["result_json"] != result_json
                    or existing_result["result_digest"] != result_digest
                ):
                    raise ReconciliationConflict(
                        "Reconciliation result conflicts"
                    )
                self._connection.commit()
                return

            revisions: dict[str, CandidateFamilyRevision] = {}
            for revision in (
                *result.new_revisions,
                *result.current_revisions,
            ):
                prior = revisions.get(revision.revision_id)
                if prior is not None and prior != revision:
                    raise ReconciliationConflict(
                        "Revision identity conflicts"
                    )
                revisions[revision.revision_id] = revision
            for revision in sorted(
                revisions.values(),
                key=lambda item: (item.family_id, item.revision),
            ):
                self._save_family_revision(
                    result.repository_id, revision
                )
            for revision in result.current_revisions:
                self._save_family_head(
                    result.repository_id, revision
                )
            self._connection.execute(
                """
                INSERT INTO reconciliation_results(
                    request_id, repository_id, result_json, result_digest
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    request,
                    result.repository_id,
                    result_json,
                    result_digest,
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            self._connection.rollback()
            raise ReconciliationConflict(
                "Reconciliation persistence conflicts"
            ) from error
        except Exception:
            self._connection.rollback()
            raise

    def stage_batch(
        self,
        request_id: str,
        revisions: tuple[CandidateFamilyRevision, ...],
        batch: CandidateBatchUpload,
    ) -> None:
        request = _request_id(request_id)
        if (
            not isinstance(revisions, tuple)
            or any(
                not isinstance(item, CandidateFamilyRevision)
                for item in revisions
            )
        ):
            raise TypeError(
                "revisions must be CandidateFamilyRevision values"
            )
        if not isinstance(batch, CandidateBatchUpload):
            raise TypeError("batch must be a CandidateBatchUpload")
        if batch.request_id != request:
            raise BatchConflict("Candidate batch request conflicts")
        expected_items = tuple(
            CandidateRevisionUpload(
                family_id=revision.family_id,
                revision_id=revision.revision_id,
                revision=revision.revision,
                content=revision.content,
                content_digest=revision.content_digest,
                evidence_digest=revision.evidence_digest,
            )
            for revision in revisions
        )
        if batch.items != expected_items:
            raise BatchConflict(
                "Candidate batch revisions conflict"
            )
        revisions_json, revisions_digest = _canonical_record(
            [item.to_dict() for item in revisions]
        )
        batch_json, stored_batch_digest = _canonical_record(
            batch.to_dict()
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            result_row = self._connection.execute(
                """
                SELECT repository_id, result_json, result_digest
                FROM reconciliation_results
                WHERE request_id = ?
                """,
                (request,),
            ).fetchone()
            if result_row is None:
                raise BatchConflict(
                    "Candidate batch has no reconciliation result"
                )
            result = ReconciliationResult.from_dict(
                _read_canonical(
                    result_row["result_json"],
                    result_row["result_digest"],
                    "Reconciliation result",
                )
            )
            if (
                batch.repository_id != result.repository_id
                or result_row["repository_id"] != result.repository_id
                or revisions != result.uploadable_revisions
            ):
                raise BatchConflict(
                    "Candidate batch does not match reconciliation"
                )
            existing = self._connection.execute(
                """
                SELECT repository_id, revisions_json, revisions_digest,
                       batch_json, batch_digest
                FROM candidate_outbox
                WHERE request_id = ?
                """,
                (request,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["repository_id"] != batch.repository_id
                    or existing["revisions_json"] != revisions_json
                    or existing["revisions_digest"]
                    != revisions_digest
                    or existing["batch_json"] != batch_json
                    or existing["batch_digest"]
                    != stored_batch_digest
                ):
                    raise BatchConflict("Candidate batch conflicts")
                self._connection.commit()
                return
            self._connection.execute(
                """
                INSERT INTO candidate_outbox(
                    request_id, repository_id,
                    revisions_json, revisions_digest,
                    batch_json, batch_digest,
                    state, receipt_json, receipt_digest
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL)
                """,
                (
                    request,
                    batch.repository_id,
                    revisions_json,
                    revisions_digest,
                    batch_json,
                    stored_batch_digest,
                ),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def pending_batch(
        self, request_id: str
    ) -> CandidateBatchUpload | None:
        request = _request_id(request_id)
        row = self._connection.execute(
            """
            SELECT batch_json, batch_digest
            FROM candidate_outbox
            WHERE request_id = ? AND state = 'pending'
            """,
            (request,),
        ).fetchone()
        if row is None:
            return None
        return CandidateBatchUpload.from_dict(
            _read_canonical(
                row["batch_json"],
                row["batch_digest"],
                "Candidate batch",
            )
        )

    def staged_batch(
        self, request_id: str
    ) -> CandidateBatchUpload | None:
        request = _request_id(request_id)
        row = self._connection.execute(
            """
            SELECT batch_json, batch_digest
            FROM candidate_outbox
            WHERE request_id = ?
            """,
            (request,),
        ).fetchone()
        if row is None:
            return None
        return CandidateBatchUpload.from_dict(
            _read_canonical(
                row["batch_json"],
                row["batch_digest"],
                "Candidate batch",
            )
        )

    def upload_receipt(
        self, request_id: str
    ) -> UploadReceipt | None:
        request = _request_id(request_id)
        row = self._connection.execute(
            """
            SELECT receipt_json, receipt_digest
            FROM candidate_outbox
            WHERE request_id = ? AND state = 'uploaded'
            """,
            (request,),
        ).fetchone()
        if row is None:
            return None
        return UploadReceipt.from_dict(
            _read_canonical(
                row["receipt_json"],
                row["receipt_digest"],
                "Upload receipt",
            )
        )

    def mark_uploaded(self, receipt: UploadReceipt) -> None:
        if not isinstance(receipt, UploadReceipt):
            raise TypeError("receipt must be an UploadReceipt")
        request = _request_id(receipt.request_id)
        receipt_json, receipt_digest = _canonical_record(
            receipt.to_dict()
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                """
                SELECT batch_json, batch_digest, state,
                       receipt_json, receipt_digest
                FROM candidate_outbox
                WHERE request_id = ?
                """,
                (request,),
            ).fetchone()
            if row is None:
                raise BatchConflict(
                    "Upload receipt has no Candidate batch"
                )
            batch = CandidateBatchUpload.from_dict(
                _read_canonical(
                    row["batch_json"],
                    row["batch_digest"],
                    "Candidate batch",
                )
            )
            if receipt.batch_digest != batch.batch_digest:
                raise BatchConflict("Upload receipt digest conflicts")
            if row["state"] == "uploaded":
                if (
                    row["receipt_json"] != receipt_json
                    or row["receipt_digest"] != receipt_digest
                ):
                    raise BatchConflict("Upload receipt conflicts")
                self._connection.commit()
                return
            self._connection.execute(
                """
                UPDATE candidate_outbox
                SET state = 'uploaded',
                    receipt_json = ?,
                    receipt_digest = ?
                WHERE request_id = ?
                """,
                (receipt_json, receipt_digest, request),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def _save_family_revision(
        self,
        repository_id: str,
        revision: CandidateFamilyRevision,
    ) -> None:
        record_json, record_digest = _canonical_record(
            revision.to_dict()
        )
        existing = self._connection.execute(
            """
            SELECT repository_id, family_id, revision,
                   revision_id, record_json, record_digest
            FROM candidate_family_revisions
            WHERE (
                repository_id = ? AND family_id = ? AND revision = ?
            ) OR revision_id = ?
            """,
            (
                repository_id,
                revision.family_id,
                revision.revision,
                revision.revision_id,
            ),
        ).fetchall()
        for row in existing:
            if (
                row["repository_id"] != repository_id
                or row["family_id"] != revision.family_id
                or row["revision"] != revision.revision
                or row["revision_id"] != revision.revision_id
                or row["record_json"] != record_json
                or row["record_digest"] != record_digest
            ):
                raise ReconciliationConflict(
                    "Candidate family revision conflicts"
                )
        if existing:
            return
        self._connection.execute(
            """
            INSERT INTO candidate_family_revisions(
                repository_id, family_id, revision, revision_id,
                record_json, record_digest
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                repository_id,
                revision.family_id,
                revision.revision,
                revision.revision_id,
                record_json,
                record_digest,
            ),
        )

    def _save_family_head(
        self,
        repository_id: str,
        revision: CandidateFamilyRevision,
    ) -> None:
        row = self._connection.execute(
            """
            SELECT heads.revision_id, revisions.revision
            FROM candidate_family_heads AS heads
            JOIN candidate_family_revisions AS revisions
              ON revisions.repository_id = heads.repository_id
             AND revisions.family_id = heads.family_id
             AND revisions.revision_id = heads.revision_id
            WHERE heads.repository_id = ? AND heads.family_id = ?
            """,
            (repository_id, revision.family_id),
        ).fetchone()
        if row is None:
            self._connection.execute(
                """
                INSERT INTO candidate_family_heads(
                    repository_id, family_id, revision_id
                ) VALUES (?, ?, ?)
                """,
                (
                    repository_id,
                    revision.family_id,
                    revision.revision_id,
                ),
            )
            return
        if row["revision_id"] == revision.revision_id:
            return
        if row["revision"] >= revision.revision:
            raise ReconciliationConflict(
                "Candidate family head would move backward"
            )
        self._connection.execute(
            """
            UPDATE candidate_family_heads
            SET revision_id = ?
            WHERE repository_id = ? AND family_id = ?
            """,
            (
                revision.revision_id,
                repository_id,
                revision.family_id,
            ),
        )

    def _transition(
        self,
        identity: tuple[str, str, str],
        *,
        allowed: frozenset[str],
        target: NativeAttemptState,
    ) -> NativeAttempt:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = _attempt(self._required_row(identity))
            if current.state not in allowed:
                raise NativeAttemptConflict("Native attempt state conflicts")
            if current.state != target:
                self._connection.execute(
                    """
                    UPDATE native_attempts
                    SET state = ?
                    WHERE request_id = ? AND operation_key = ? AND stage = ?
                    """,
                    (target, *identity),
                )
            transitioned = _attempt(self._required_row(identity))
            self._connection.commit()
            return transitioned
        except Exception:
            self._connection.rollback()
            raise

    def _row(
        self, identity: tuple[str, str, str]
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT request_id, operation_key, stage, stable_tag, state,
                   native_id, output_digest
            FROM native_attempts
            WHERE request_id = ? AND operation_key = ? AND stage = ?
            """,
            identity,
        ).fetchone()

    def _required_row(
        self, identity: tuple[str, str, str]
    ) -> sqlite3.Row:
        row = self._row(identity)
        if row is None:
            raise NativeAttemptNotFound("Native attempt does not exist")
        return row


class NativeCallCoordinator:
    """Combine durable intent with unique native-object recovery."""

    def __init__(self, store: RequestStateStore) -> None:
        if not isinstance(store, RequestStateStore):
            raise TypeError("store must be a RequestStateStore")
        self.store = store

    def resolve_thread(
        self,
        *,
        request_id: str,
        operation_key: str,
        stage: str,
        stable_tag: str,
        find: Callable[[str], str | None],
        create: Callable[[], str],
    ) -> str:
        attempt = self.store.get_or_create_native_attempt(
            request_id, operation_key, stage, stable_tag
        )
        if attempt.state == "completed":
            assert attempt.native_id is not None
            return attempt.native_id
        if attempt.state == "attached":
            assert attempt.native_id is not None
            self.store.complete_native_attempt(
                request_id,
                operation_key,
                stage,
                _thread_digest(attempt.native_id, stage, stable_tag),
            )
            return attempt.native_id
        if attempt.state == "pending":
            native_id = self._find_thread(find, stable_tag)
            if native_id is None:
                raise CaptureResultUnknown(
                    "Native Thread result is not yet observable"
                )
            attached = self.store.attach_native_result(
                request_id, operation_key, stage, native_id
            )
            self.store.complete_native_attempt(
                request_id,
                operation_key,
                stage,
                _thread_digest(native_id, stage, stable_tag),
            )
            assert attached.native_id is not None
            return attached.native_id

        self.store.mark_native_pending(request_id, operation_key, stage)
        try:
            native_id = create()
            _safe_value(native_id, "native_id")
        except AppServerRequestError:
            self.store.reset_native_after_rejection(
                request_id, operation_key, stage
            )
            raise
        except Exception:
            raise CaptureResultUnknown(
                "Native Thread result is unknown"
            ) from None
        self.store.attach_native_result(
            request_id, operation_key, stage, native_id
        )
        self.store.complete_native_attempt(
            request_id,
            operation_key,
            stage,
            _thread_digest(native_id, stage, stable_tag),
        )
        return native_id

    def resolve_structured_turn(
        self,
        *,
        request_id: str,
        operation_key: str,
        stage: str,
        stable_tag: str,
        read: Callable[[str], AppServerTurnReceipt | None],
        create: Callable[[], AppServerTurnReceipt],
    ) -> AppServerTurnReceipt:
        attempt = self.store.get_or_create_native_attempt(
            request_id, operation_key, stage, stable_tag
        )
        if attempt.state in {"pending", "attached", "completed"}:
            receipt = self._read_turn(read, stable_tag)
            if receipt is None:
                raise CaptureResultUnknown(
                    "Native Turn result is not yet observable"
                )
            self._verify_receipt(attempt, receipt)
            if attempt.native_id is None:
                self.store.attach_native_result(
                    request_id,
                    operation_key,
                    stage,
                    receipt.turn_id,
                )
            self.store.complete_native_attempt(
                request_id,
                operation_key,
                stage,
                receipt.output_sha256,
            )
            return receipt

        self.store.mark_native_pending(request_id, operation_key, stage)
        try:
            receipt = create()
            if not isinstance(receipt, AppServerTurnReceipt):
                raise TypeError("create did not return an app-server receipt")
        except AppServerRequestError:
            self.store.reset_native_after_rejection(
                request_id, operation_key, stage
            )
            raise
        except Exception:
            raise CaptureResultUnknown(
                "Native Turn result is unknown"
            ) from None
        self.store.attach_native_result(
            request_id, operation_key, stage, receipt.turn_id
        )
        self.store.complete_native_attempt(
            request_id,
            operation_key,
            stage,
            receipt.output_sha256,
        )
        return receipt

    @staticmethod
    def _find_thread(
        find: Callable[[str], str | None], stable_tag: str
    ) -> str | None:
        try:
            native_id = find(stable_tag)
            if native_id is not None:
                return _safe_value(native_id, "native_id")
            return None
        except Exception:
            raise CaptureResultUnknown(
                "Native Thread recovery is unavailable"
            ) from None

    @staticmethod
    def _read_turn(
        read: Callable[[str], AppServerTurnReceipt | None],
        stable_tag: str,
    ) -> AppServerTurnReceipt | None:
        try:
            receipt = read(stable_tag)
            if receipt is not None and not isinstance(
                receipt, AppServerTurnReceipt
            ):
                raise TypeError("read did not return an app-server receipt")
            return receipt
        except Exception:
            raise CaptureResultUnknown(
                "Native Turn recovery is unavailable"
            ) from None

    @staticmethod
    def _verify_receipt(
        attempt: NativeAttempt, receipt: AppServerTurnReceipt
    ) -> None:
        if (
            attempt.native_id is not None
            and attempt.native_id != receipt.turn_id
        ):
            raise NativeAttemptConflict("Recovered native Turn id conflicts")
        if (
            attempt.output_digest is not None
            and attempt.output_digest != receipt.output_sha256
        ):
            raise NativeAttemptConflict(
                "Recovered native Turn output conflicts"
            )


def _identity(
    request_id: str, operation_key: str, stage: str
) -> tuple[str, str, str]:
    request = _request_id(request_id)
    operation = _safe_value(operation_key, "operation_key")
    if not isinstance(stage, str) or stage not in _STAGES:
        raise ValueError("stage is invalid")
    return request, operation, stage


def _request_id(value: object) -> str:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise ValueError("request_id is invalid")
    return value


def _repository_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or _REPOSITORY_ID.fullmatch(value) is None
    ):
        raise ValueError("repository_id is invalid")
    return value


def _safe_value(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_KEY.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("output_digest is invalid")
    return value


def _thread_digest(native_id: str, stage: str, stable_tag: str) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "native_id": native_id,
                "stable_tag": stable_tag,
                "stage": stage,
            }
        )
    ).hexdigest()


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
        raise RequestStateCorrupt(
            f"{record_name} persistence fields are invalid"
        )
    encoded = payload.encode("utf-8")
    actual_digest = hashlib.sha256(encoded).hexdigest()
    if actual_digest != expected_digest:
        raise RequestStateCorrupt(
            f"{record_name} digest does not match stored bytes"
        )
    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise RequestStateCorrupt(
            f"{record_name} is not valid JSON"
        ) from error
    if canonical_json_bytes(value) != encoded:
        raise RequestStateCorrupt(
            f"{record_name} bytes are not canonical JSON"
        )
    return value


def _attempt(row: sqlite3.Row) -> NativeAttempt:
    return NativeAttempt(
        request_id=row["request_id"],
        operation_key=row["operation_key"],
        stage=row["stage"],
        stable_tag=row["stable_tag"],
        state=row["state"],
        native_id=row["native_id"],
        output_digest=row["output_digest"],
    )
