"""Durable reconciliation generations and atomic Candidate delivery state."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from zdecision.capture.reconciliation import (
    CandidateFamilyRevision,
    ReconciliationResult,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CandidateBatchUpload,
    CandidateRevisionUpload,
    UploadReceipt,
    CandidateSliceBatchUpload,
    SliceUploadReceipt,
)


ReconciliationAttemptState = Literal[
    "creating_thread",
    "running",
    "validated",
    "accepted",
    "superseded",
    "abandoned",
]
ArchiveState = Literal["not_applicable", "pending", "archived"]

_REQUEST_ID = re.compile(r"^crq_[0-9a-f]{32}$")
_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_DECISION_SPACE_ID = re.compile(r"^dsp_[0-9a-f]{32}$")
_SLICE_ID = re.compile(r"^csl_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ATTEMPT_STATES = frozenset(
    (
        "creating_thread",
        "running",
        "validated",
        "accepted",
        "superseded",
        "abandoned",
    )
)
_TERMINAL_ATTEMPT_STATES = frozenset(
    ("accepted", "superseded", "abandoned")
)
_ARCHIVE_STATES = frozenset(
    ("not_applicable", "pending", "archived")
)


class RequestStateError(Exception):
    """Base class for private reconciliation and delivery failures."""


class ReconciliationConflict(RequestStateError):
    """A reconciliation replay conflicts with durable local state."""


class BatchConflict(RequestStateError):
    """A Candidate batch or upload receipt conflicts with durable state."""


class RequestStateCorrupt(RequestStateError):
    """Persisted canonical data failed its digest or typed contract."""


@dataclass(frozen=True)
class ReconciliationAttempt:
    attempt_id: str
    request_id: str
    generation: int
    state: ReconciliationAttemptState
    thread_id: str | None
    turn_id: str | None
    failure_code: str | None
    validated_result_digest: str | None
    archive_state: ArchiveState
    started_at: str
    finished_at: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise ValueError("attempt_id is invalid")
        _request_id(self.request_id)
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 1
        ):
            raise ValueError("generation is invalid")
        if self.state not in _ATTEMPT_STATES:
            raise ValueError("state is invalid")
        if self.archive_state not in _ARCHIVE_STATES:
            raise ValueError("archive_state is invalid")
        _nonempty(self.started_at, "started_at")
        for field_name in (
            "thread_id",
            "turn_id",
            "failure_code",
            "finished_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _nonempty(value, field_name)
        if self.validated_result_digest is not None:
            _digest(self.validated_result_digest)
        if self.thread_id is None and self.archive_state != "not_applicable":
            raise ValueError(
                "Unknown reconciliation Thread cannot require archive"
            )
        if self.thread_id is not None and self.archive_state == "not_applicable":
            raise ValueError(
                "Known reconciliation Thread must track archive state"
            )


class RequestStateStore:
    """Own reconciliation fencing, family heads, and the Candidate outbox."""

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
                CREATE TABLE IF NOT EXISTS reconciliation_operations (
                    request_id TEXT PRIMARY KEY,
                    input_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'open', 'committed'
                    )),
                    active_generation INTEGER NOT NULL
                        CHECK(active_generation >= 0),
                    winner_generation INTEGER,
                    committed_result_json TEXT,
                    committed_result_digest TEXT
                );

                CREATE TABLE IF NOT EXISTS reconciliation_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation > 0),
                    state TEXT NOT NULL CHECK(state IN (
                        'creating_thread', 'running', 'validated',
                        'accepted', 'superseded', 'abandoned'
                    )),
                    thread_id TEXT,
                    turn_id TEXT,
                    failure_code TEXT,
                    validated_result_json TEXT,
                    validated_result_digest TEXT,
                    archive_state TEXT NOT NULL CHECK(archive_state IN (
                        'not_applicable', 'pending', 'archived'
                    )),
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(request_id, generation),
                    FOREIGN KEY(request_id)
                        REFERENCES reconciliation_operations(request_id)
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
                        'pending', 'uploaded'
                    )),
                    receipt_json TEXT,
                    receipt_digest TEXT
                );

                CREATE TABLE IF NOT EXISTS slice_reconciliation_results (
                    request_id TEXT NOT NULL,
                    slice_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    decision_space_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    PRIMARY KEY(request_id, slice_id)
                );

                CREATE TABLE IF NOT EXISTS slice_reconciliation_archives (
                    request_id TEXT NOT NULL,
                    slice_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending', 'archived')),
                    PRIMARY KEY(request_id, slice_id)
                );

                CREATE TABLE IF NOT EXISTS slice_candidate_family_revisions (
                    repository_id TEXT NOT NULL,
                    decision_space_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    revision_id TEXT NOT NULL UNIQUE,
                    record_json TEXT NOT NULL,
                    record_digest TEXT NOT NULL,
                    PRIMARY KEY(
                        repository_id, decision_space_id, family_id, revision
                    )
                );

                CREATE TABLE IF NOT EXISTS slice_candidate_family_heads (
                    repository_id TEXT NOT NULL,
                    decision_space_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    PRIMARY KEY(repository_id, decision_space_id, family_id)
                );

                CREATE TABLE IF NOT EXISTS slice_candidate_outbox (
                    request_id TEXT NOT NULL,
                    slice_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    decision_space_id TEXT NOT NULL,
                    batch_json TEXT NOT NULL,
                    batch_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending', 'uploaded')),
                    receipt_json TEXT,
                    receipt_digest TEXT,
                    PRIMARY KEY(request_id, slice_id)
                );

                CREATE INDEX IF NOT EXISTS reconciliation_archive_queue
                    ON reconciliation_attempts(archive_state, state);
                """
            )
            _retire_native_attempts(connection)
        return cls(database_path, connection)

    def close(self) -> None:
        self._connection.close()

    def begin_reconciliation_attempt(
        self,
        request_id: str,
        input_digest: str,
        started_at: str,
    ) -> ReconciliationAttempt:
        request = _request_id(request_id)
        digest = _digest(input_digest)
        started = _nonempty(started_at, "started_at")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            operation = self._reconciliation_operation(request)
            if operation is None:
                self._connection.execute(
                    """
                    INSERT INTO reconciliation_operations(
                        request_id, input_digest, status,
                        active_generation, winner_generation,
                        committed_result_json, committed_result_digest
                    ) VALUES (?, ?, 'open', 0, NULL, NULL, NULL)
                    """,
                    (request, digest),
                )
                operation = self._required_reconciliation_operation(
                    request
                )
            elif operation["input_digest"] != digest:
                raise ReconciliationConflict(
                    "Reconciliation input digest conflicts"
                )

            if operation["status"] == "committed":
                winner = self._winner_attempt(operation)
                self._connection.commit()
                return winner

            active_generation = operation["active_generation"]
            if active_generation:
                active_row = self._attempt_for_generation(
                    request, active_generation
                )
                if active_row is None:
                    raise RequestStateCorrupt(
                        "Active reconciliation attempt is missing"
                    )
                active = self._attempt(active_row)
                if active.state == "validated":
                    self._connection.commit()
                    return active
                if active.state in ("creating_thread", "running"):
                    self._connection.execute(
                        """
                        UPDATE reconciliation_attempts
                        SET state = 'abandoned',
                            failure_code = 'restart_result_unknown',
                            finished_at = ?
                        WHERE attempt_id = ?
                        """,
                        (started, active.attempt_id),
                    )
                elif active.state == "accepted":
                    raise RequestStateCorrupt(
                        "Open reconciliation has an accepted attempt"
                    )

            generation = active_generation + 1
            attempt_id = _attempt_id(request, generation)
            updated = self._connection.execute(
                """
                UPDATE reconciliation_operations
                SET active_generation = ?
                WHERE request_id = ? AND status = 'open'
                  AND active_generation = ?
                """,
                (generation, request, active_generation),
            )
            if updated.rowcount != 1:
                raise ReconciliationConflict(
                    "Reconciliation generation changed concurrently"
                )
            self._connection.execute(
                """
                INSERT INTO reconciliation_attempts(
                    attempt_id, request_id, generation, state,
                    thread_id, turn_id, failure_code,
                    validated_result_json, validated_result_digest,
                    archive_state, started_at, finished_at
                ) VALUES (
                    ?, ?, ?, 'creating_thread',
                    NULL, NULL, NULL, NULL, NULL,
                    'not_applicable', ?, NULL
                )
                """,
                (attempt_id, request, generation, started),
            )
            attempt = self._attempt(
                self._required_attempt(attempt_id)
            )
            self._connection.commit()
            return attempt
        except Exception:
            self._connection.rollback()
            raise

    def attach_reconciliation_thread(
        self, attempt_id: str, thread_id: str
    ) -> ReconciliationAttempt:
        thread = _nonempty(thread_id, "thread_id")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._attempt(self._required_attempt(attempt_id))
            if current.thread_id is not None:
                if current.thread_id != thread:
                    raise ReconciliationConflict(
                        "Reconciliation Thread conflicts"
                    )
                self._connection.commit()
                return current
            if current.state != "creating_thread":
                raise ReconciliationConflict(
                    "Reconciliation cannot attach a Thread"
                )
            self._connection.execute(
                """
                UPDATE reconciliation_attempts
                SET state = 'running', thread_id = ?,
                    archive_state = 'pending'
                WHERE attempt_id = ?
                """,
                (thread, attempt_id),
            )
            updated = self._attempt(
                self._required_attempt(attempt_id)
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    def attach_reconciliation_turn(
        self, attempt_id: str, turn_id: str
    ) -> ReconciliationAttempt:
        turn = _nonempty(turn_id, "turn_id")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._attempt(self._required_attempt(attempt_id))
            if current.turn_id is not None:
                if current.turn_id != turn:
                    raise ReconciliationConflict(
                        "Reconciliation Turn conflicts"
                    )
                self._connection.commit()
                return current
            if current.state != "running" or current.thread_id is None:
                raise ReconciliationConflict(
                    "Reconciliation attempt is not running"
                )
            self._connection.execute(
                """
                UPDATE reconciliation_attempts
                SET turn_id = ?
                WHERE attempt_id = ?
                """,
                (turn, attempt_id),
            )
            updated = self._attempt(
                self._required_attempt(attempt_id)
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    def abandon_reconciliation_attempt(
        self,
        attempt_id: str,
        failure_code: str,
        finished_at: str,
    ) -> ReconciliationAttempt:
        failure = _failure_code(failure_code)
        finished = _nonempty(finished_at, "finished_at")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._attempt(self._required_attempt(attempt_id))
            if current.state == "abandoned":
                if current.failure_code != failure:
                    raise ReconciliationConflict(
                        "Reconciliation failure conflicts"
                    )
                self._connection.commit()
                return current
            if current.state in ("accepted", "superseded"):
                raise ReconciliationConflict(
                    "Terminal reconciliation cannot be abandoned"
                )
            self._connection.execute(
                """
                UPDATE reconciliation_attempts
                SET state = 'abandoned', failure_code = ?,
                    finished_at = ?
                WHERE attempt_id = ?
                """,
                (failure, finished, attempt_id),
            )
            updated = self._attempt(
                self._required_attempt(attempt_id)
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    def store_validated_reconciliation(
        self,
        attempt_id: str,
        result: ReconciliationResult,
        finished_at: str,
    ) -> ReconciliationAttempt:
        if not isinstance(result, ReconciliationResult):
            raise TypeError("result must be a ReconciliationResult")
        canonical = ReconciliationResult.from_dict(result.to_dict())
        if canonical != result:
            raise ReconciliationConflict(
                "Reconciliation result is not canonical"
            )
        result_json, result_digest = _canonical_record(result.to_dict())
        finished = _nonempty(finished_at, "finished_at")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._required_attempt(attempt_id)
            current = self._attempt(row)
            existing_json = row["validated_result_json"]
            existing_digest = row["validated_result_digest"]
            if existing_json is not None or existing_digest is not None:
                if (
                    existing_json != result_json
                    or existing_digest != result_digest
                ):
                    raise ReconciliationConflict(
                        "Validated reconciliation conflicts"
                    )
                self._connection.commit()
                return current
            next_state = (
                current.state
                if current.state in _TERMINAL_ATTEMPT_STATES
                else "validated"
            )
            self._connection.execute(
                """
                UPDATE reconciliation_attempts
                SET state = ?, validated_result_json = ?,
                    validated_result_digest = ?,
                    finished_at = COALESCE(finished_at, ?)
                WHERE attempt_id = ?
                """,
                (
                    next_state,
                    result_json,
                    result_digest,
                    finished,
                    attempt_id,
                ),
            )
            updated = self._attempt(
                self._required_attempt(attempt_id)
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    def commit_reconciliation_attempt(
        self, attempt_id: str
    ) -> ReconciliationResult:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            attempt_row = self._required_attempt(attempt_id)
            attempt = self._attempt(attempt_row)
            attempted_result = self._attempt_result(attempt_row)
            if attempted_result is None:
                raise ReconciliationConflict(
                    "Reconciliation attempt has no validated result"
                )
            operation = self._required_reconciliation_operation(
                attempt.request_id
            )
            if operation["status"] == "committed":
                winner_result = self._operation_result(operation)
                if winner_result is None:
                    raise RequestStateCorrupt(
                        "Committed reconciliation has no result"
                    )
                if operation["winner_generation"] == attempt.generation:
                    if winner_result != attempted_result:
                        raise RequestStateCorrupt(
                            "Winning reconciliation result conflicts"
                        )
                    target_state = "accepted"
                else:
                    target_state = "superseded"
                self._set_attempt_state(attempt_id, target_state)
                self._connection.commit()
                return winner_result

            can_win = (
                operation["active_generation"] == attempt.generation
                and attempt.state == "validated"
            )
            if not can_win:
                raise ReconciliationConflict(
                    "Stale reconciliation has no committed winner"
                )
            result_json, result_digest = _canonical_record(
                attempted_result.to_dict()
            )
            updated = self._connection.execute(
                """
                UPDATE reconciliation_operations
                SET status = 'committed', winner_generation = ?,
                    committed_result_json = ?,
                    committed_result_digest = ?
                WHERE request_id = ? AND status = 'open'
                  AND active_generation = ?
                """,
                (
                    attempt.generation,
                    result_json,
                    result_digest,
                    attempt.request_id,
                    attempt.generation,
                ),
            )
            if updated.rowcount != 1:
                raise ReconciliationConflict(
                    "Reconciliation winner CAS lost"
                )
            self._set_attempt_state(attempt_id, "accepted")
            self._connection.commit()
            return attempted_result
        except Exception:
            self._connection.rollback()
            raise

    def pending_reconciliation_archives(
        self,
    ) -> tuple[ReconciliationAttempt, ...]:
        rows = self._connection.execute(
            """
            SELECT *
            FROM reconciliation_attempts
            WHERE archive_state = 'pending'
              AND state IN ('accepted', 'superseded', 'abandoned')
            ORDER BY request_id, generation
            """
        ).fetchall()
        return tuple(self._attempt(row) for row in rows)

    def mark_reconciliation_archived(
        self, attempt_id: str
    ) -> ReconciliationAttempt:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._attempt(self._required_attempt(attempt_id))
            if current.archive_state == "archived":
                self._connection.commit()
                return current
            if (
                current.thread_id is None
                or current.archive_state != "pending"
                or current.state not in _TERMINAL_ATTEMPT_STATES
            ):
                raise ReconciliationConflict(
                    "Reconciliation is not ready to archive"
                )
            self._connection.execute(
                """
                UPDATE reconciliation_attempts
                SET archive_state = 'archived'
                WHERE attempt_id = ?
                """,
                (attempt_id,),
            )
            updated = self._attempt(
                self._required_attempt(attempt_id)
            )
            self._connection.commit()
            return updated
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
        try:
            return ReconciliationResult.from_dict(
                _read_canonical(
                    row["result_json"],
                    row["result_digest"],
                    "Reconciliation result",
                )
            )
        except ValueError as error:
            raise RequestStateCorrupt(
                "Reconciliation result is invalid"
            ) from error

    def commit_candidate_result(
        self,
        request_id: str,
        result: ReconciliationResult,
        batch: CandidateBatchUpload,
    ) -> CandidateBatchUpload:
        request = _request_id(request_id)
        if not isinstance(result, ReconciliationResult):
            raise TypeError("result must be a ReconciliationResult")
        if not isinstance(batch, CandidateBatchUpload):
            raise TypeError("batch must be a CandidateBatchUpload")
        if batch.request_id != request:
            raise BatchConflict("Candidate batch request conflicts")
        if batch.repository_id != result.repository_id:
            raise BatchConflict("Candidate batch repository conflicts")
        expected_items = tuple(
            CandidateRevisionUpload(
                family_id=revision.family_id,
                revision_id=revision.revision_id,
                revision=revision.revision,
                content=revision.content,
                content_digest=revision.content_digest,
                evidence_digest=revision.evidence_digest,
                provenance=revision.provenance,
            )
            for revision in result.uploadable_revisions
        )
        if batch.items != expected_items:
            raise BatchConflict(
                "Candidate batch revisions conflict"
            )
        result_json, result_digest = _canonical_record(result.to_dict())
        revisions_json, revisions_digest = _canonical_record(
            [item.to_dict() for item in result.uploadable_revisions]
        )
        batch_json, stored_batch_digest = _canonical_record(
            batch.to_dict()
        )

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            operation = self._reconciliation_operation(request)
            if operation is not None:
                if operation["status"] != "committed":
                    raise BatchConflict(
                        "Candidate commit has no reconciliation winner"
                    )
                winner = self._operation_result(operation)
                if winner != result:
                    raise BatchConflict(
                        "Candidate result conflicts with winner"
                    )

            existing_result = self._connection.execute(
                """
                SELECT repository_id, result_json, result_digest
                FROM reconciliation_results
                WHERE request_id = ?
                """,
                (request,),
            ).fetchone()
            if existing_result is not None and (
                existing_result["repository_id"] != result.repository_id
                or existing_result["result_json"] != result_json
                or existing_result["result_digest"] != result_digest
            ):
                raise BatchConflict(
                    "Candidate reconciliation result conflicts"
                )
            existing_outbox = self._connection.execute(
                """
                SELECT repository_id, revisions_json, revisions_digest,
                       batch_json, batch_digest
                FROM candidate_outbox
                WHERE request_id = ?
                """,
                (request,),
            ).fetchone()
            if existing_outbox is not None:
                if existing_result is None or (
                    existing_outbox["repository_id"]
                    != batch.repository_id
                    or existing_outbox["revisions_json"]
                    != revisions_json
                    or existing_outbox["revisions_digest"]
                    != revisions_digest
                    or existing_outbox["batch_json"] != batch_json
                    or existing_outbox["batch_digest"]
                    != stored_batch_digest
                ):
                    raise BatchConflict("Candidate batch conflicts")
                stored = self._batch_from_row(existing_outbox)
                self._connection.commit()
                return stored

            revisions: dict[str, CandidateFamilyRevision] = {}
            for revision in (
                *result.new_revisions,
                *result.current_revisions,
            ):
                prior = revisions.get(revision.revision_id)
                if prior is not None and prior != revision:
                    raise BatchConflict(
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
            if existing_result is None:
                self._connection.execute(
                    """
                    INSERT INTO reconciliation_results(
                        request_id, repository_id,
                        result_json, result_digest
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        request,
                        result.repository_id,
                        result_json,
                        result_digest,
                    ),
                )
            self._insert_candidate_outbox(
                request=request,
                repository_id=batch.repository_id,
                revisions_json=revisions_json,
                revisions_digest=revisions_digest,
                batch_json=batch_json,
                batch_digest=stored_batch_digest,
            )
            self._connection.commit()
            return batch
        except sqlite3.IntegrityError as error:
            self._connection.rollback()
            raise BatchConflict(
                "Candidate persistence conflicts"
            ) from error
        except ReconciliationConflict as error:
            self._connection.rollback()
            raise BatchConflict(
                "Candidate family state conflicts"
            ) from error
        except Exception:
            self._connection.rollback()
            raise

    def pending_batch(
        self, request_id: str
    ) -> CandidateBatchUpload | None:
        request = _request_id(request_id)
        row = self._connection.execute(
            """
            SELECT *
            FROM candidate_outbox
            WHERE request_id = ? AND state = 'pending'
            """,
            (request,),
        ).fetchone()
        return None if row is None else self._batch_from_row(row)

    def staged_batch(
        self, request_id: str
    ) -> CandidateBatchUpload | None:
        request = _request_id(request_id)
        row = self._connection.execute(
            """
            SELECT *
            FROM candidate_outbox
            WHERE request_id = ?
            """,
            (request,),
        ).fetchone()
        return None if row is None else self._batch_from_row(row)

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
        try:
            return UploadReceipt.from_dict(
                _read_canonical(
                    row["receipt_json"],
                    row["receipt_digest"],
                    "Upload receipt",
                )
            )
        except ValueError as error:
            raise RequestStateCorrupt(
                "Upload receipt is invalid"
            ) from error

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
                SELECT *, state AS outbox_state
                FROM candidate_outbox
                WHERE request_id = ?
                """,
                (request,),
            ).fetchone()
            if row is None:
                raise BatchConflict(
                    "Upload receipt has no Candidate batch"
                )
            batch = self._batch_from_row(row)
            if receipt.batch_digest != batch.batch_digest:
                raise BatchConflict("Upload receipt digest conflicts")
            if row["outbox_state"] == "uploaded":
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

    def _insert_candidate_outbox(
        self,
        *,
        request: str,
        repository_id: str,
        revisions_json: str,
        revisions_digest: str,
        batch_json: str,
        batch_digest: str,
    ) -> None:
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
                repository_id,
                revisions_json,
                revisions_digest,
                batch_json,
                batch_digest,
            ),
        )

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

    def _batch_from_row(
        self, row: sqlite3.Row
    ) -> CandidateBatchUpload:
        try:
            return CandidateBatchUpload.from_dict(
                _read_canonical(
                    row["batch_json"],
                    row["batch_digest"],
                    "Candidate batch",
                )
            )
        except ValueError as error:
            raise RequestStateCorrupt(
                "Candidate batch is invalid"
            ) from error

    def _reconciliation_operation(
        self, request_id: str
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT *
            FROM reconciliation_operations
            WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()

    def _required_reconciliation_operation(
        self, request_id: str
    ) -> sqlite3.Row:
        row = self._reconciliation_operation(request_id)
        if row is None:
            raise ReconciliationConflict(
                "Reconciliation operation does not exist"
            )
        return row

    def _attempt_for_generation(
        self, request_id: str, generation: int
    ) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT *
            FROM reconciliation_attempts
            WHERE request_id = ? AND generation = ?
            """,
            (request_id, generation),
        ).fetchone()

    def _required_attempt(self, attempt_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            """
            SELECT *
            FROM reconciliation_attempts
            WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise ReconciliationConflict(
                "Reconciliation attempt does not exist"
            )
        return row

    def _winner_attempt(
        self, operation: sqlite3.Row
    ) -> ReconciliationAttempt:
        generation = operation["winner_generation"]
        if not isinstance(generation, int):
            raise RequestStateCorrupt(
                "Committed reconciliation winner is invalid"
            )
        row = self._attempt_for_generation(
            operation["request_id"], generation
        )
        if row is None:
            raise RequestStateCorrupt(
                "Committed reconciliation winner is missing"
            )
        attempt = self._attempt(row)
        if attempt.state != "accepted":
            raise RequestStateCorrupt(
                "Committed reconciliation winner is not accepted"
            )
        return attempt

    def _operation_result(
        self, operation: sqlite3.Row
    ) -> ReconciliationResult | None:
        raw = operation["committed_result_json"]
        digest = operation["committed_result_digest"]
        if raw is None and digest is None:
            return None
        if raw is None or digest is None:
            raise RequestStateCorrupt(
                "Committed reconciliation result is incomplete"
            )
        try:
            return ReconciliationResult.from_dict(
                _read_canonical(
                    raw, digest, "Committed reconciliation result"
                )
            )
        except ValueError as error:
            raise RequestStateCorrupt(
                "Committed reconciliation result is invalid"
            ) from error

    def _attempt_result(
        self, attempt: sqlite3.Row
    ) -> ReconciliationResult | None:
        raw = attempt["validated_result_json"]
        digest = attempt["validated_result_digest"]
        if raw is None and digest is None:
            return None
        if raw is None or digest is None:
            raise RequestStateCorrupt(
                "Validated reconciliation result is incomplete"
            )
        try:
            return ReconciliationResult.from_dict(
                _read_canonical(
                    raw, digest, "Validated reconciliation result"
                )
            )
        except ValueError as error:
            raise RequestStateCorrupt(
                "Validated reconciliation result is invalid"
            ) from error

    def _attempt(self, row: sqlite3.Row) -> ReconciliationAttempt:
        try:
            return ReconciliationAttempt(
                attempt_id=row["attempt_id"],
                request_id=row["request_id"],
                generation=row["generation"],
                state=row["state"],
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                failure_code=row["failure_code"],
                validated_result_digest=row[
                    "validated_result_digest"
                ],
                archive_state=row["archive_state"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
            )
        except (TypeError, ValueError, KeyError):
            raise RequestStateCorrupt(
                "Reconciliation attempt is invalid"
            ) from None

    def _set_attempt_state(self, attempt_id: str, state: str) -> None:
        self._connection.execute(
            """
            UPDATE reconciliation_attempts
            SET state = ?
            WHERE attempt_id = ?
            """,
            (state, attempt_id),
        )

    def slice_reconciliation(
        self, request_id: str, slice_id: str
    ) -> ReconciliationResult | None:
        request = _request_id(request_id)
        slice_value = _slice_id(slice_id)
        row = self._connection.execute(
            """
            SELECT result_json, result_digest
            FROM slice_reconciliation_results
            WHERE request_id = ? AND slice_id = ?
            """,
            (request, slice_value),
        ).fetchone()
        if row is None:
            return None
        try:
            return ReconciliationResult.from_dict(
                _read_canonical(
                    row["result_json"],
                    row["result_digest"],
                    "Slice reconciliation result",
                )
            )
        except ValueError as error:
            raise RequestStateCorrupt(
                "Slice reconciliation result is invalid"
            ) from error

    def store_slice_reconciliation(
        self,
        request_id: str,
        slice_id: str,
        result: ReconciliationResult,
        *,
        archive_thread_id: str | None = None,
    ) -> ReconciliationResult:
        request = _request_id(request_id)
        slice_value = _slice_id(slice_id)
        if not isinstance(result, ReconciliationResult):
            raise TypeError("result must be a ReconciliationResult")
        if archive_thread_id is not None:
            _nonempty(archive_thread_id, "archive_thread_id")
        result_json, result_digest = _canonical_record(result.to_dict())
        with self._connection:
            existing = self._connection.execute(
                """
                SELECT repository_id, decision_space_id,
                       result_json, result_digest
                FROM slice_reconciliation_results
                WHERE request_id = ? AND slice_id = ?
                """,
                (request, slice_value),
            ).fetchone()
            if existing is not None:
                if (
                    existing["repository_id"] != result.repository_id
                    or existing["decision_space_id"]
                    != result.decision_space_id
                    or existing["result_json"] != result_json
                    or existing["result_digest"] != result_digest
                ):
                    raise ReconciliationConflict(
                        "Slice reconciliation conflicts"
                    )
            else:
                self._connection.execute(
                    """
                    INSERT INTO slice_reconciliation_results(
                        request_id, slice_id, repository_id,
                        decision_space_id, result_json, result_digest
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request,
                        slice_value,
                        result.repository_id,
                        result.decision_space_id,
                        result_json,
                        result_digest,
                    ),
                )
            if archive_thread_id is not None:
                archive = self._connection.execute(
                    """
                    SELECT thread_id FROM slice_reconciliation_archives
                    WHERE request_id = ? AND slice_id = ?
                    """,
                    (request, slice_value),
                ).fetchone()
                if archive is not None:
                    if archive["thread_id"] != archive_thread_id:
                        raise ReconciliationConflict(
                            "Slice reconciliation archive conflicts"
                        )
                else:
                    self._connection.execute(
                        """
                        INSERT INTO slice_reconciliation_archives(
                            request_id, slice_id, thread_id, state
                        ) VALUES (?, ?, ?, 'pending')
                        """,
                        (request, slice_value, archive_thread_id),
                    )
        return result

    def pending_slice_reconciliation_archives(
        self,
    ) -> tuple[tuple[str, str, str], ...]:
        rows = self._connection.execute(
            """
            SELECT request_id, slice_id, thread_id
            FROM slice_reconciliation_archives
            WHERE state = 'pending'
            ORDER BY request_id, slice_id
            """
        ).fetchall()
        return tuple(
            (row["request_id"], row["slice_id"], row["thread_id"])
            for row in rows
        )

    def mark_slice_reconciliation_archived(
        self, request_id: str, slice_id: str, thread_id: str
    ) -> None:
        request = _request_id(request_id)
        slice_value = _slice_id(slice_id)
        thread = _nonempty(thread_id, "thread_id")
        with self._connection:
            row = self._connection.execute(
                """
                SELECT thread_id FROM slice_reconciliation_archives
                WHERE request_id = ? AND slice_id = ?
                """,
                (request, slice_value),
            ).fetchone()
            if row is None or row["thread_id"] != thread:
                raise ReconciliationConflict(
                    "Slice reconciliation archive conflicts"
                )
            self._connection.execute(
                """
                UPDATE slice_reconciliation_archives SET state = 'archived'
                WHERE request_id = ? AND slice_id = ?
                """,
                (request, slice_value),
            )

    def slice_current_families(
        self, repository_id: str, decision_space_id: str
    ) -> tuple[CandidateFamilyRevision, ...]:
        repository = _repository_id(repository_id)
        decision_space = _decision_space_id(decision_space_id)
        rows = self._connection.execute(
            """
            SELECT revisions.record_json, revisions.record_digest
            FROM slice_candidate_family_heads AS heads
            JOIN slice_candidate_family_revisions AS revisions
              ON revisions.repository_id = heads.repository_id
             AND revisions.decision_space_id = heads.decision_space_id
             AND revisions.family_id = heads.family_id
             AND revisions.revision_id = heads.revision_id
            WHERE heads.repository_id = ? AND heads.decision_space_id = ?
            ORDER BY heads.family_id
            """,
            (repository, decision_space),
        ).fetchall()
        return tuple(
            CandidateFamilyRevision.from_dict(
                _read_canonical(
                    row["record_json"],
                    row["record_digest"],
                    "Slice Candidate family revision",
                )
            )
            for row in rows
        )

    def commit_slice_result(
        self,
        request_id: str,
        slice_id: str,
        result: ReconciliationResult,
        batch: CandidateSliceBatchUpload,
    ) -> CandidateSliceBatchUpload:
        request = _request_id(request_id)
        slice_value = _slice_id(slice_id)
        if not isinstance(result, ReconciliationResult):
            raise TypeError("result must be a ReconciliationResult")
        if not isinstance(batch, CandidateSliceBatchUpload):
            raise TypeError("batch must be a CandidateSliceBatchUpload")
        if (
            batch.request_id != request
            or batch.slice_id != slice_value
            or batch.decision_space_id != result.decision_space_id
        ):
            raise BatchConflict("Candidate slice batch identity conflicts")
        expected_items = tuple(
            CandidateRevisionUpload(
                family_id=revision.family_id,
                revision_id=revision.revision_id,
                revision=revision.revision,
                content=revision.content,
                content_digest=revision.content_digest,
                evidence_digest=revision.evidence_digest,
                provenance=revision.provenance,
            )
            for revision in result.uploadable_revisions
        )
        if batch.items != expected_items:
            raise BatchConflict("Candidate slice revisions conflict")
        batch_json, batch_record_digest = _canonical_record(batch.to_dict())
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            persisted = self.slice_reconciliation(request, slice_value)
            if persisted is not None and persisted != result:
                raise BatchConflict("Candidate slice result conflicts")
            existing = self._connection.execute(
                """
                SELECT batch_json, batch_digest
                FROM slice_candidate_outbox
                WHERE request_id = ? AND slice_id = ?
                """,
                (request, slice_value),
            ).fetchone()
            if existing is not None:
                if (
                    existing["batch_json"] != batch_json
                    or existing["batch_digest"] != batch_record_digest
                ):
                    raise BatchConflict("Candidate slice batch conflicts")
                stored = CandidateSliceBatchUpload.from_dict(
                    _read_canonical(
                        existing["batch_json"],
                        existing["batch_digest"],
                        "Candidate slice batch",
                    )
                )
                self._connection.commit()
                return stored
            if persisted is None:
                result_json, result_digest = _canonical_record(result.to_dict())
                self._connection.execute(
                    """
                    INSERT INTO slice_reconciliation_results(
                        request_id, slice_id, repository_id,
                        decision_space_id, result_json, result_digest
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request,
                        slice_value,
                        result.repository_id,
                        result.decision_space_id,
                        result_json,
                        result_digest,
                    ),
                )
            revisions = {
                revision.revision_id: revision
                for revision in (*result.new_revisions, *result.current_revisions)
            }
            for revision in sorted(
                revisions.values(),
                key=lambda item: (item.family_id, item.revision),
            ):
                record_json, record_digest = _canonical_record(
                    revision.to_dict()
                )
                self._connection.execute(
                    """
                    INSERT INTO slice_candidate_family_revisions(
                        repository_id, decision_space_id, family_id,
                        revision, revision_id, record_json, record_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        repository_id, decision_space_id, family_id, revision
                    ) DO UPDATE SET
                        revision_id = excluded.revision_id,
                        record_json = excluded.record_json,
                        record_digest = excluded.record_digest
                    """,
                    (
                        result.repository_id,
                        result.decision_space_id,
                        revision.family_id,
                        revision.revision,
                        revision.revision_id,
                        record_json,
                        record_digest,
                    ),
                )
            for revision in result.current_revisions:
                self._connection.execute(
                    """
                    INSERT INTO slice_candidate_family_heads(
                        repository_id, decision_space_id,
                        family_id, revision_id
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(
                        repository_id, decision_space_id, family_id
                    ) DO UPDATE SET revision_id = excluded.revision_id
                    """,
                    (
                        result.repository_id,
                        result.decision_space_id,
                        revision.family_id,
                        revision.revision_id,
                    ),
                )
            self._connection.execute(
                """
                INSERT INTO slice_candidate_outbox(
                    request_id, slice_id, repository_id,
                    decision_space_id, batch_json, batch_digest,
                    state, receipt_json, receipt_digest
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL)
                """,
                (
                    request,
                    slice_value,
                    result.repository_id,
                    result.decision_space_id,
                    batch_json,
                    batch_record_digest,
                ),
            )
            self._connection.commit()
            return batch
        except sqlite3.IntegrityError as error:
            self._connection.rollback()
            raise BatchConflict("Candidate slice persistence conflicts") from error
        except Exception:
            self._connection.rollback()
            raise

    def staged_slice_batch(
        self, request_id: str, slice_id: str
    ) -> CandidateSliceBatchUpload | None:
        row = self._connection.execute(
            """
            SELECT batch_json, batch_digest
            FROM slice_candidate_outbox
            WHERE request_id = ? AND slice_id = ?
            """,
            (_request_id(request_id), _slice_id(slice_id)),
        ).fetchone()
        if row is None:
            return None
        return CandidateSliceBatchUpload.from_dict(
            _read_canonical(
                row["batch_json"], row["batch_digest"],
                "Candidate slice batch"
            )
        )

    def slice_receipt(
        self, request_id: str, slice_id: str
    ) -> SliceUploadReceipt | None:
        row = self._connection.execute(
            """
            SELECT receipt_json, receipt_digest
            FROM slice_candidate_outbox
            WHERE request_id = ? AND slice_id = ? AND state = 'uploaded'
            """,
            (_request_id(request_id), _slice_id(slice_id)),
        ).fetchone()
        if row is None:
            return None
        return SliceUploadReceipt.from_dict(
            _read_canonical(
                row["receipt_json"], row["receipt_digest"],
                "Slice upload receipt"
            )
        )

    def mark_slice_uploaded(self, receipt: SliceUploadReceipt) -> None:
        if not isinstance(receipt, SliceUploadReceipt):
            raise TypeError("receipt must be a SliceUploadReceipt")
        receipt_json, receipt_digest = _canonical_record(receipt.to_dict())
        with self._connection:
            row = self._connection.execute(
                """
                SELECT state, batch_json, batch_digest,
                       receipt_json, receipt_digest
                FROM slice_candidate_outbox
                WHERE request_id = ? AND slice_id = ?
                """,
                (receipt.request_id, receipt.slice_id),
            ).fetchone()
            if row is None:
                raise BatchConflict("Candidate slice outbox is missing")
            batch = CandidateSliceBatchUpload.from_dict(
                _read_canonical(
                    row["batch_json"],
                    row["batch_digest"],
                    "Candidate slice batch",
                )
            )
            expected_receipt_digest = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "request_id": batch.request_id,
                        "slice_id": batch.slice_id,
                        "candidate_count": len(batch.items),
                        "batch_digest": batch.batch_digest,
                    }
                )
            ).hexdigest()
            if (
                receipt.candidate_count != len(batch.items)
                or receipt.receipt_digest != expected_receipt_digest
            ):
                raise BatchConflict("Slice upload receipt conflicts")
            if row["state"] == "uploaded":
                if (
                    row["receipt_json"] != receipt_json
                    or row["receipt_digest"] != receipt_digest
                ):
                    raise BatchConflict("Slice upload receipt conflicts")
                return
            self._connection.execute(
                """
                UPDATE slice_candidate_outbox
                SET state = 'uploaded', receipt_json = ?, receipt_digest = ?
                WHERE request_id = ? AND slice_id = ?
                """,
                (
                    receipt_json,
                    receipt_digest,
                    receipt.request_id,
                    receipt.slice_id,
                ),
            )

    def has_receipt(self, request_id: str, slice_id: str) -> bool:
        return self.slice_receipt(request_id, slice_id) is not None

    def receipts_digest(
        self, request_id: str, slice_ids: tuple[str, ...]
    ) -> str:
        receipts: list[SliceUploadReceipt] = []
        for slice_id in slice_ids:
            receipt = self.slice_receipt(request_id, slice_id)
            if receipt is None:
                raise BatchConflict("Slice upload receipt is missing")
            receipts.append(receipt)
        return hashlib.sha256(
            canonical_json_bytes(
                {"receipts": [receipt.to_dict() for receipt in receipts]}
            )
        ).hexdigest()


def _retire_native_attempts(connection: sqlite3.Connection) -> bool:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    required = {
        "capture_operations",
        "capture_execution_attempts",
        "reconciliation_operations",
        "reconciliation_attempts",
        "candidate_outbox",
    }
    if not required.issubset(tables):
        return False
    existed = "native_attempts" in tables
    connection.execute("DROP TABLE IF EXISTS native_attempts")
    return existed


def _attempt_id(request_id: str, generation: int) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {"generation": generation, "request_id": request_id}
        )
    ).hexdigest()[:32]
    return f"rat_{digest}"


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


def _decision_space_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or _DECISION_SPACE_ID.fullmatch(value) is None
    ):
        raise ValueError("decision_space_id is invalid")
    return value


def _slice_id(value: object) -> str:
    if not isinstance(value, str) or _SLICE_ID.fullmatch(value) is None:
        raise ValueError("slice_id is invalid")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("digest is invalid")
    return value


def _failure_code(value: object) -> str:
    if (
        not isinstance(value, str)
        or _FAILURE_CODE.fullmatch(value) is None
    ):
        raise ValueError("failure_code is invalid")
    return value


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} is invalid")
    return value


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
    if hashlib.sha256(encoded).hexdigest() != expected_digest:
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
