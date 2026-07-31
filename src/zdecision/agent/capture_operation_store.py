"""SQLite persistence for extractor-v3 Capture operations and attempts."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from zdecision.capture.on_demand import (
    CaptureCommit,
    CaptureOperation,
    ExecutionAttempt,
    FrozenCaptureInput,
    ValidatedCaptureResult,
)
from zdecision.ids import capture_attempt_id
from zdecision.jsonio import canonical_json_bytes


_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TERMINAL_ATTEMPT_STATES = frozenset(
    ("accepted", "superseded", "abandoned")
)


class CaptureOperationStoreError(Exception):
    """Base class for durable Capture operation failures."""


class CaptureOperationConflict(CaptureOperationStoreError, ValueError):
    """A replay conflicts with the already frozen business operation."""


class CaptureAttemptConflict(CaptureOperationStoreError, ValueError):
    """An attempt transition conflicts with durable state."""


class CaptureOperationCorrupt(CaptureOperationStoreError):
    """Persisted operation state failed its canonical typed contract."""


def _canonical_text(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _json_object(value: object, record_name: str) -> Mapping[str, object]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        raise CaptureOperationCorrupt(
            f"{record_name} is not valid JSON"
        ) from None
    if not isinstance(decoded, Mapping):
        raise CaptureOperationCorrupt(f"{record_name} must be an object")
    return decoded


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CaptureAttemptConflict(f"{field_name} must be non-empty")
    return value


class CaptureOperationStore:
    """Own generation fencing and the one extractor-v3 result CAS."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> "CaptureOperationStore":
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
                CREATE TABLE IF NOT EXISTS capture_operations (
                    operation_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    frozen_json TEXT NOT NULL,
                    frozen_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'open', 'committed', 'failed_terminal'
                    )),
                    active_generation INTEGER NOT NULL
                        CHECK(active_generation >= 0),
                    winner_generation INTEGER,
                    committed_result_json TEXT,
                    committed_result_digest TEXT,
                    failure_code TEXT,
                    UNIQUE(request_id, source_key)
                );

                CREATE TABLE IF NOT EXISTS capture_execution_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation > 0),
                    state TEXT NOT NULL CHECK(state IN (
                        'prepared', 'creating_thread', 'running', 'validated',
                        'accepted', 'superseded', 'abandoned'
                    )),
                    thread_id TEXT,
                    inventory_turn_id TEXT,
                    extraction_turn_id TEXT,
                    failure_code TEXT,
                    validated_result_json TEXT,
                    validated_result_digest TEXT,
                    archive_state TEXT NOT NULL CHECK(archive_state IN (
                        'not_applicable', 'pending', 'archived'
                    )),
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(operation_id, generation),
                    FOREIGN KEY(operation_id)
                        REFERENCES capture_operations(operation_id)
                );

                CREATE INDEX IF NOT EXISTS capture_attempt_archives
                    ON capture_execution_attempts(archive_state, state);
                """
            )
        return cls(database_path, connection)

    def close(self) -> None:
        self._connection.close()

    def ensure_operation(
        self, frozen: FrozenCaptureInput
    ) -> CaptureOperation:
        if not isinstance(frozen, FrozenCaptureInput):
            raise TypeError("frozen must be a FrozenCaptureInput")
        frozen_json = _canonical_text(frozen.to_dict())
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._operation_row(frozen.operation_id)
            if existing is not None:
                operation = self._operation(existing)
                if (
                    operation.frozen_digest != frozen.frozen_digest
                    or existing["frozen_json"] != frozen_json
                ):
                    raise CaptureOperationConflict(
                        "Capture operation frozen input conflicts"
                    )
                self._connection.commit()
                return operation

            source_row = self._connection.execute(
                """
                SELECT operation_id
                FROM capture_operations
                WHERE request_id = ? AND source_key = ?
                """,
                (frozen.request_id, frozen.source_key),
            ).fetchone()
            if source_row is not None:
                raise CaptureOperationConflict(
                    "Capture request source is already frozen differently"
                )
            self._connection.execute(
                """
                INSERT INTO capture_operations(
                    operation_id, request_id, source_key, frozen_json,
                    frozen_digest, status, active_generation,
                    winner_generation, committed_result_json,
                    committed_result_digest, failure_code
                ) VALUES (?, ?, ?, ?, ?, 'open', 0, NULL, NULL, NULL, NULL)
                """,
                (
                    frozen.operation_id,
                    frozen.request_id,
                    frozen.source_key,
                    frozen_json,
                    frozen.frozen_digest,
                ),
            )
            operation = self._operation(
                self._required_operation_row(frozen.operation_id)
            )
            self._connection.commit()
            return operation
        except Exception:
            self._connection.rollback()
            raise

    def operation_for_source(
        self, request_id: str, source_key: str
    ) -> CaptureOperation | None:
        request = _nonempty(request_id, "request_id")
        source = _nonempty(source_key, "source_key")
        rows = self._connection.execute(
            """
            SELECT *
            FROM capture_operations
            WHERE request_id = ? AND source_key = ?
            """,
            (request, source),
        ).fetchall()
        if len(rows) > 1:
            raise CaptureOperationCorrupt(
                "Capture request source has multiple operations"
            )
        return None if not rows else self._operation(rows[0])

    def begin_attempt(
        self, operation_id: str, started_at: str
    ) -> ExecutionAttempt:
        started = _nonempty(started_at, "started_at")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            operation = self._operation(
                self._required_operation_row(operation_id)
            )
            if operation.status != "open":
                raise CaptureAttemptConflict(
                    "Capture operation is not open"
                )
            generation = operation.active_generation + 1
            attempt_id = capture_attempt_id(operation.operation_id, generation)
            updated = self._connection.execute(
                """
                UPDATE capture_operations
                SET active_generation = ?
                WHERE operation_id = ? AND status = 'open'
                  AND active_generation = ?
                """,
                (
                    generation,
                    operation.operation_id,
                    operation.active_generation,
                ),
            )
            if updated.rowcount != 1:
                raise CaptureAttemptConflict(
                    "Capture generation changed concurrently"
                )
            self._connection.execute(
                """
                INSERT INTO capture_execution_attempts(
                    attempt_id, operation_id, generation, state, thread_id,
                    inventory_turn_id, extraction_turn_id, failure_code,
                    validated_result_json, validated_result_digest,
                    archive_state, started_at, finished_at
                ) VALUES (
                    ?, ?, ?, 'creating_thread', NULL, NULL, NULL, NULL,
                    NULL, NULL, 'not_applicable', ?, NULL
                )
                """,
                (
                    attempt_id,
                    operation.operation_id,
                    generation,
                    started,
                ),
            )
            attempt = self._attempt(
                self._required_attempt_row(attempt_id)
            )
            self._connection.commit()
            return attempt
        except Exception:
            self._connection.rollback()
            raise

    def attach_thread(
        self, attempt_id: str, thread_id: str
    ) -> ExecutionAttempt:
        native_thread = _nonempty(thread_id, "thread_id")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._attempt(self._required_attempt_row(attempt_id))
            if current.thread_id is not None:
                if current.thread_id != native_thread:
                    raise CaptureAttemptConflict(
                        "Capture attempt Thread conflicts"
                    )
                self._connection.commit()
                return current
            if current.state not in ("prepared", "creating_thread"):
                raise CaptureAttemptConflict(
                    "Capture attempt cannot attach a Thread"
                )
            self._connection.execute(
                """
                UPDATE capture_execution_attempts
                SET thread_id = ?, state = 'running',
                    archive_state = 'pending'
                WHERE attempt_id = ?
                """,
                (native_thread, attempt_id),
            )
            updated = self._attempt(
                self._required_attempt_row(attempt_id)
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    def attach_turn(
        self,
        attempt_id: str,
        stage: str,
        turn_id: str,
    ) -> ExecutionAttempt:
        if stage not in ("inventory", "extraction"):
            raise CaptureAttemptConflict("Capture stage is invalid")
        native_turn = _nonempty(turn_id, "turn_id")
        column = (
            "inventory_turn_id"
            if stage == "inventory"
            else "extraction_turn_id"
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._attempt(self._required_attempt_row(attempt_id))
            existing = getattr(current, column)
            if existing is not None:
                if existing != native_turn:
                    raise CaptureAttemptConflict(
                        f"Capture attempt {stage} Turn conflicts"
                    )
                self._connection.commit()
                return current
            if current.thread_id is None or current.state != "running":
                raise CaptureAttemptConflict(
                    "Capture attempt is not running"
                )
            if (
                stage == "extraction"
                and current.inventory_turn_id is None
            ):
                raise CaptureAttemptConflict(
                    "Extraction cannot precede Inventory"
                )
            self._connection.execute(
                f"""
                UPDATE capture_execution_attempts
                SET {column} = ?
                WHERE attempt_id = ?
                """,
                (native_turn, attempt_id),
            )
            updated = self._attempt(
                self._required_attempt_row(attempt_id)
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    def abandon_attempt(
        self,
        attempt_id: str,
        failure_code: str,
        finished_at: str,
    ) -> ExecutionAttempt:
        failure = self._failure_code(failure_code)
        finished = _nonempty(finished_at, "finished_at")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._attempt(self._required_attempt_row(attempt_id))
            if current.state == "abandoned":
                if current.failure_code != failure:
                    raise CaptureAttemptConflict(
                        "Capture attempt failure conflicts"
                    )
                self._connection.commit()
                return current
            if current.state in ("accepted", "superseded"):
                raise CaptureAttemptConflict(
                    "Terminal Capture attempt cannot be abandoned"
                )
            self._connection.execute(
                """
                UPDATE capture_execution_attempts
                SET state = 'abandoned', failure_code = ?, finished_at = ?
                WHERE attempt_id = ?
                """,
                (failure, finished, attempt_id),
            )
            updated = self._attempt(
                self._required_attempt_row(attempt_id)
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    def store_validated_attempt(
        self,
        attempt_id: str,
        result: ValidatedCaptureResult,
        finished_at: str,
    ) -> ExecutionAttempt:
        if not isinstance(result, ValidatedCaptureResult):
            raise TypeError("result must be a ValidatedCaptureResult")
        validated_result = ValidatedCaptureResult.from_dict(result.to_dict())
        if validated_result != result:
            raise CaptureAttemptConflict(
                "Validated result is not canonical"
            )
        finished = _nonempty(finished_at, "finished_at")
        result_json = _canonical_text(result.to_dict())
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._required_attempt_row(attempt_id)
            current = self._attempt(row)
            if current.operation_id != result.operation_id:
                raise CaptureAttemptConflict(
                    "Validated result operation conflicts"
                )
            if row["validated_result_json"] is not None:
                if (
                    row["validated_result_digest"] != result.result_digest
                    or row["validated_result_json"] != result_json
                ):
                    raise CaptureAttemptConflict(
                        "Validated result replay conflicts"
                    )
                self._connection.commit()
                return current
            next_state = (
                current.state
                if current.state
                in ("accepted", "superseded", "abandoned")
                else "validated"
            )
            self._connection.execute(
                """
                UPDATE capture_execution_attempts
                SET state = ?, validated_result_json = ?,
                    validated_result_digest = ?,
                    finished_at = COALESCE(finished_at, ?)
                WHERE attempt_id = ?
                """,
                (
                    next_state,
                    result_json,
                    result.result_digest,
                    finished,
                    attempt_id,
                ),
            )
            updated = self._attempt(
                self._required_attempt_row(attempt_id)
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    def commit_attempt(self, attempt_id: str) -> CaptureCommit:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            attempt_row = self._required_attempt_row(attempt_id)
            attempt = self._attempt(attempt_row)
            attempted_result = self._validated_result(attempt_row)
            if attempted_result is None:
                raise CaptureAttemptConflict(
                    "Capture attempt has no validated result"
                )
            operation_row = self._required_operation_row(
                attempt.operation_id
            )
            operation = self._operation(operation_row)

            if operation.status == "committed":
                winner = self._committed_result(operation_row)
                if winner is None:
                    raise CaptureOperationCorrupt(
                        "Committed Capture operation has no result"
                    )
                if operation.winner_generation == attempt.generation:
                    if (
                        winner.result_digest
                        != attempted_result.result_digest
                    ):
                        raise CaptureOperationCorrupt(
                            "Winning Capture attempt conflicts with its commit"
                        )
                    target_state = "accepted"
                else:
                    target_state = "superseded"
                self._set_attempt_state(attempt_id, target_state)
                commit = CaptureCommit(
                    operation=self._operation(
                        self._required_operation_row(
                            attempt.operation_id
                        )
                    ),
                    attempt=self._attempt(
                        self._required_attempt_row(attempt_id)
                    ),
                    result=winner,
                )
                self._connection.commit()
                return commit

            can_win = (
                operation.status == "open"
                and operation.active_generation == attempt.generation
                and attempt.state == "validated"
            )
            if not can_win:
                self._set_attempt_state(attempt_id, "superseded")
                result = (
                    self._committed_result(operation_row)
                    if operation.status == "committed"
                    else None
                )
                commit = CaptureCommit(
                    operation=self._operation(
                        self._required_operation_row(
                            attempt.operation_id
                        )
                    ),
                    attempt=self._attempt(
                        self._required_attempt_row(attempt_id)
                    ),
                    result=result,
                )
                self._connection.commit()
                return commit

            result_json = _canonical_text(attempted_result.to_dict())
            updated = self._connection.execute(
                """
                UPDATE capture_operations
                SET status = 'committed', winner_generation = ?,
                    committed_result_json = ?,
                    committed_result_digest = ?
                WHERE operation_id = ? AND status = 'open'
                  AND active_generation = ?
                """,
                (
                    attempt.generation,
                    result_json,
                    attempted_result.result_digest,
                    attempt.operation_id,
                    attempt.generation,
                ),
            )
            if updated.rowcount != 1:
                raise CaptureAttemptConflict(
                    "Capture operation CAS lost concurrently"
                )
            self._set_attempt_state(attempt_id, "accepted")
            commit = CaptureCommit(
                operation=self._operation(
                    self._required_operation_row(attempt.operation_id)
                ),
                attempt=self._attempt(
                    self._required_attempt_row(attempt_id)
                ),
                result=attempted_result,
            )
            self._connection.commit()
            return commit
        except Exception:
            self._connection.rollback()
            raise

    def fail_operation_terminal(
        self, operation_id: str, failure_code: str
    ) -> CaptureOperation:
        failure = self._failure_code(failure_code)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._operation(
                self._required_operation_row(operation_id)
            )
            if current.status == "failed_terminal":
                if current.failure_code != failure:
                    raise CaptureOperationConflict(
                        "Terminal Capture failure conflicts"
                    )
                self._connection.commit()
                return current
            if current.status == "committed":
                raise CaptureOperationConflict(
                    "Committed Capture operation cannot fail"
                )
            self._connection.execute(
                """
                UPDATE capture_operations
                SET status = 'failed_terminal', failure_code = ?
                WHERE operation_id = ? AND status = 'open'
                """,
                (failure, operation_id),
            )
            updated = self._operation(
                self._required_operation_row(operation_id)
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    def committed_result(
        self, operation_id: str
    ) -> ValidatedCaptureResult | None:
        row = self._required_operation_row(operation_id)
        operation = self._operation(row)
        if operation.status != "committed":
            return None
        result = self._committed_result(row)
        if result is None:
            raise CaptureOperationCorrupt(
                "Committed Capture operation has no result"
            )
        return result

    def pending_archives(self) -> tuple[ExecutionAttempt, ...]:
        rows = self._connection.execute(
            """
            SELECT *
            FROM capture_execution_attempts
            WHERE archive_state = 'pending'
              AND state IN ('accepted', 'superseded', 'abandoned')
            ORDER BY operation_id, generation
            """
        ).fetchall()
        return tuple(self._attempt(row) for row in rows)

    def mark_archived(self, attempt_id: str) -> ExecutionAttempt:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._attempt(self._required_attempt_row(attempt_id))
            if current.archive_state == "archived":
                self._connection.commit()
                return current
            if (
                current.thread_id is None
                or current.archive_state != "pending"
                or current.state not in _TERMINAL_ATTEMPT_STATES
            ):
                raise CaptureAttemptConflict(
                    "Capture attempt is not ready to archive"
                )
            self._connection.execute(
                """
                UPDATE capture_execution_attempts
                SET archive_state = 'archived'
                WHERE attempt_id = ?
                """,
                (attempt_id,),
            )
            updated = self._attempt(
                self._required_attempt_row(attempt_id)
            )
            self._connection.commit()
            return updated
        except Exception:
            self._connection.rollback()
            raise

    def _operation_row(self, operation_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM capture_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()

    def _required_operation_row(self, operation_id: str) -> sqlite3.Row:
        row = self._operation_row(operation_id)
        if row is None:
            raise CaptureOperationConflict(
                f"Capture operation {operation_id!r} does not exist"
            )
        return row

    def _required_attempt_row(self, attempt_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            """
            SELECT *
            FROM capture_execution_attempts
            WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise CaptureAttemptConflict(
                f"Capture attempt {attempt_id!r} does not exist"
            )
        return row

    def _operation(self, row: sqlite3.Row) -> CaptureOperation:
        try:
            raw_frozen = _json_object(
                row["frozen_json"], "FrozenCaptureInput"
            )
            frozen = FrozenCaptureInput.from_dict(raw_frozen)
            if (
                row["frozen_json"] != _canonical_text(frozen.to_dict())
                or row["frozen_digest"] != frozen.frozen_digest
                or row["operation_id"] != frozen.operation_id
                or row["request_id"] != frozen.request_id
                or row["source_key"] != frozen.source_key
            ):
                raise CaptureOperationCorrupt(
                    "Capture operation frozen state is not canonical"
                )
            return CaptureOperation(
                operation_id=row["operation_id"],
                frozen=frozen,
                frozen_digest=row["frozen_digest"],
                status=row["status"],
                active_generation=row["active_generation"],
                winner_generation=row["winner_generation"],
                committed_result_digest=row[
                    "committed_result_digest"
                ],
                failure_code=row["failure_code"],
            )
        except CaptureOperationCorrupt:
            raise
        except (TypeError, ValueError, KeyError):
            raise CaptureOperationCorrupt(
                "Capture operation state is invalid"
            ) from None

    def _attempt(self, row: sqlite3.Row) -> ExecutionAttempt:
        try:
            return ExecutionAttempt(
                attempt_id=row["attempt_id"],
                operation_id=row["operation_id"],
                generation=row["generation"],
                state=row["state"],
                thread_id=row["thread_id"],
                inventory_turn_id=row["inventory_turn_id"],
                extraction_turn_id=row["extraction_turn_id"],
                failure_code=row["failure_code"],
                validated_result_digest=row[
                    "validated_result_digest"
                ],
                archive_state=row["archive_state"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
            )
        except (TypeError, ValueError, KeyError):
            raise CaptureOperationCorrupt(
                "Capture attempt state is invalid"
            ) from None

    def _validated_result(
        self, row: sqlite3.Row
    ) -> ValidatedCaptureResult | None:
        raw = row["validated_result_json"]
        digest = row["validated_result_digest"]
        if raw is None and digest is None:
            return None
        if raw is None or digest is None:
            raise CaptureOperationCorrupt(
                "Capture attempt result is incomplete"
            )
        try:
            result = ValidatedCaptureResult.from_dict(
                _json_object(raw, "ValidatedCaptureResult")
            )
        except (TypeError, ValueError) as error:
            raise CaptureOperationCorrupt(
                "Capture attempt result is invalid"
            ) from error
        if (
            result.result_digest != digest
            or raw != _canonical_text(result.to_dict())
        ):
            raise CaptureOperationCorrupt(
                "Capture attempt result is not canonical"
            )
        return result

    def _committed_result(
        self, row: sqlite3.Row
    ) -> ValidatedCaptureResult | None:
        raw = row["committed_result_json"]
        digest = row["committed_result_digest"]
        if raw is None and digest is None:
            return None
        if raw is None or digest is None:
            raise CaptureOperationCorrupt(
                "Committed Capture result is incomplete"
            )
        try:
            result = ValidatedCaptureResult.from_dict(
                _json_object(raw, "ValidatedCaptureResult")
            )
        except (TypeError, ValueError) as error:
            raise CaptureOperationCorrupt(
                "Committed Capture result is invalid"
            ) from error
        if (
            result.result_digest != digest
            or raw != _canonical_text(result.to_dict())
        ):
            raise CaptureOperationCorrupt(
                "Committed Capture result is not canonical"
            )
        return result

    def _set_attempt_state(self, attempt_id: str, state: str) -> None:
        self._connection.execute(
            """
            UPDATE capture_execution_attempts
            SET state = ?
            WHERE attempt_id = ?
            """,
            (state, attempt_id),
        )

    @staticmethod
    def _failure_code(value: str) -> str:
        if not isinstance(value, str) or _FAILURE_CODE.fullmatch(value) is None:
            raise CaptureAttemptConflict("failure_code is invalid")
        return value
