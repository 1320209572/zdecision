"""SQLite Event Ledger for the device-local ZDecision Agent."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zdecision.agent.events import (
    EVENT_STATES,
    VALIDATION_STATES,
    WORK_STATES,
    AgentEvent,
    EventState,
    HookInvocation,
    TestRepositoryMapping,
    event_id_for,
)
from zdecision.jsonio import canonical_json_bytes


_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_PRODUCT_ID = re.compile(r"^prod_[0-9a-f]{32}$")
_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MODEL_PROFILE_ID = re.compile(r"^fmp_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_AUTOMATED_CAPTURE_ID = re.compile(r"^acp_[0-9a-f]{32}$")
_CAPTURE_OPERATION_ID = re.compile(r"^cap_[0-9a-f]{32}$")
_AUTOMATED_CAPTURE_STATES = frozenset(
    (
        "prepared",
        "assessment_fork_pending",
        "assessment_fork_attached",
        "assessment_completed",
        "completed_ineligible",
        "capture_fork_pending",
        "capture_fork_attached",
        "inventory_completed",
        "completed",
        "ambiguous",
        "failed",
    )
)
_TERMINAL_AUTOMATED_CAPTURE_STATES = frozenset(
    ("completed_ineligible", "completed", "ambiguous", "failed")
)
_AUTOMATED_CAPTURE_TRANSITIONS = {
    "prepared": frozenset(("assessment_fork_pending", "failed")),
    "assessment_fork_pending": frozenset(
        ("assessment_fork_attached", "ambiguous", "failed")
    ),
    "assessment_fork_attached": frozenset(("assessment_completed", "failed")),
    "assessment_completed": frozenset(
        ("completed_ineligible", "capture_fork_pending", "failed")
    ),
    "capture_fork_pending": frozenset(
        ("capture_fork_attached", "ambiguous", "failed")
    ),
    "capture_fork_attached": frozenset(("inventory_completed", "failed")),
    "inventory_completed": frozenset(("completed", "failed")),
}


@dataclass(frozen=True)
class SessionLease:
    session_id: str
    cwd: str
    renewed_at: datetime
    expires_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True)
class StoredFeasibilityModelProfile:
    profile_id: str
    model_id: str
    reasoning_effort: str
    discovery_digest: str
    discovered_at: str


@dataclass(frozen=True)
class AppServerRouteRecord:
    route: str
    failure_code: str | None
    recorded_at: datetime


@dataclass(frozen=True)
class AutomatedCaptureRunRecord:
    automated_capture_id: str
    session_id: str
    source_turn_id: str
    repository_id: str
    product_id: str
    product_name: str
    template_id: str
    template_snapshot_digest: str
    eligibility_prompt_digest: str
    model_profile_id: str
    state: str
    assessment_thread_id: str | None
    assessment_turn_id: str | None
    capture_operation_id: str | None
    capture_thread_id: str | None
    inventory_turn_id: str | None
    extraction_turn_id: str | None
    candidate_ids: tuple[str, ...]
    failure_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class BoundaryAssessmentRecord:
    automated_capture_id: str
    source_thread_id: str
    source_turn_id: str
    prompt_version: str
    prompt_digest: str
    input_fact_digest: str
    assessment_thread_id: str
    assessment_turn_id: str
    model_profile_id: str
    phase: str
    has_durable_decision_signal: bool
    validation: str
    unresolved_blockers: tuple[str, ...]
    recorded_at: str


class AgentEventConflict(Exception):
    """Raised when one event identity is replayed with different content."""


class FeasibilityModelProfileConflict(Exception):
    """Raised when model discovery changes after the feasibility profile freezes."""


class AutomatedCaptureConflict(Exception):
    """Raised when a private automated-Capture CAS or immutable write conflicts."""


class AgentDatabase:
    """Small transactional store shared by Hooks and the local MCP process."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> "AgentDatabase":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=0.25)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 250")
        connection.execute("PRAGMA journal_mode = WAL")
        with connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_id TEXT,
                    cwd TEXT NOT NULL,
                    repository_id TEXT,
                    worktree_root TEXT,
                    branch TEXT,
                    head_commit TEXT,
                    safe_fact_json BLOB NOT NULL,
                    input_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('recorded','processing','consumed','deferred',
                                  'failed_retryable','failed_terminal')
                    ),
                    failure_code TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    processing_expires_at TEXT,
                    retry_at TEXT
                );

                CREATE TABLE IF NOT EXISTS feasibility_repository_mappings (
                    repository_id TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
                );

                CREATE INDEX IF NOT EXISTS events_session
                    ON events(session_id);
                CREATE INDEX IF NOT EXISTS events_cwd
                    ON events(cwd);
                """
            )
            event_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            if "attempt_count" not in event_columns:
                connection.execute(
                    "ALTER TABLE events ADD COLUMN "
                    "attempt_count INTEGER NOT NULL DEFAULT 0"
                )
            if "processing_expires_at" not in event_columns:
                connection.execute(
                    "ALTER TABLE events ADD COLUMN processing_expires_at TEXT"
                )
            if "retry_at" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN retry_at TEXT")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_leases (
                    session_id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    renewed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    ended_at TEXT
                );

                CREATE TABLE IF NOT EXISTS worker_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    owner_pid INTEGER,
                    lease_expires_at TEXT
                );

                CREATE TABLE IF NOT EXISTS sync_probe (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    cursor INTEGER NOT NULL,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS feasibility_model_profile (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    profile_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL,
                    discovery_digest TEXT NOT NULL,
                    discovered_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_server_route_events (
                    route_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    route TEXT NOT NULL,
                    failure_code TEXT,
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS automated_capture_runs (
                    automated_capture_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    source_turn_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    template_snapshot_digest TEXT NOT NULL,
                    eligibility_prompt_digest TEXT NOT NULL,
                    model_profile_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'prepared','assessment_fork_pending',
                            'assessment_fork_attached','assessment_completed',
                            'completed_ineligible','capture_fork_pending',
                            'capture_fork_attached','inventory_completed',
                            'completed','ambiguous','failed'
                        )
                    ),
                    assessment_thread_id TEXT,
                    assessment_turn_id TEXT,
                    capture_operation_id TEXT,
                    capture_thread_id TEXT,
                    inventory_turn_id TEXT,
                    extraction_turn_id TEXT,
                    candidate_ids_json BLOB NOT NULL,
                    failure_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS automated_capture_boundary
                    ON automated_capture_runs(session_id, source_turn_id);

                CREATE TABLE IF NOT EXISTS boundary_assessments (
                    automated_capture_id TEXT PRIMARY KEY,
                    source_thread_id TEXT NOT NULL,
                    source_turn_id TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    prompt_digest TEXT NOT NULL,
                    input_fact_digest TEXT NOT NULL,
                    assessment_thread_id TEXT NOT NULL,
                    assessment_turn_id TEXT NOT NULL,
                    model_profile_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    has_durable_decision_signal INTEGER NOT NULL
                        CHECK (has_durable_decision_signal IN (0, 1)),
                    validation TEXT NOT NULL,
                    unresolved_blockers_json BLOB NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(automated_capture_id)
                        REFERENCES automated_capture_runs(automated_capture_id)
                );

                CREATE INDEX IF NOT EXISTS events_work_queue
                    ON events(state, retry_at, processing_expires_at);

                INSERT INTO worker_state (
                    singleton_id, owner_pid, lease_expires_at
                ) VALUES (1, NULL, NULL)
                ON CONFLICT(singleton_id) DO NOTHING;

                INSERT INTO sync_probe (singleton_id, cursor, updated_at)
                VALUES (1, 0, NULL)
                ON CONFLICT(singleton_id) DO NOTHING;

                PRAGMA user_version = 4;
                """
            )
        return cls(database_path, connection)

    def record_hook(self, invocation: HookInvocation) -> AgentEvent:
        if not isinstance(invocation, HookInvocation):
            raise TypeError("record_hook requires a HookInvocation")
        event_id = event_id_for(invocation)
        values = self._invocation_values(event_id, invocation)
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO events (
                    event_id, event_type, occurred_at, session_id, turn_id, cwd,
                    repository_id, worktree_root, branch, head_commit,
                    safe_fact_json, input_digest, state, failure_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                values,
            )
        if cursor.rowcount == 0:
            row = self._connection.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None or not self._is_same_replay(row, values):
                raise AgentEventConflict(
                    f"Event {event_id!r} already has different normalized content"
                )
        event = self.get_event(event_id)
        if event is None:
            raise RuntimeError("Recorded event could not be read back")
        return event

    def get_event(self, event_id: str) -> AgentEvent | None:
        row = self._connection.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return None if row is None else self._event_from_row(row)

    def list_events(self, session_id: str) -> tuple[AgentEvent, ...]:
        rows = self._connection.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY rowid", (session_id,)
        ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def latest_open_boundary(self, cwd: str) -> tuple[str, str] | None:
        event = self.latest_turn_event(cwd)
        if event is None or event.invocation.turn_id is None:
            return None
        return event.invocation.session_id, event.invocation.turn_id

    def latest_turn_event(self, cwd: str) -> AgentEvent | None:
        rows = self._connection.execute(
            """
            SELECT DISTINCT candidate.session_id
            FROM events AS candidate
            WHERE candidate.cwd = ?
              AND candidate.turn_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM events AS ended
                  WHERE ended.session_id = candidate.session_id
                    AND ended.event_type = 'SessionEnd'
                    AND ended.rowid > candidate.rowid
              )
            """,
            (cwd,),
        ).fetchall()
        if len(rows) != 1:
            return None
        row = self._connection.execute(
            """
            SELECT * FROM events
            WHERE cwd = ? AND session_id = ? AND turn_id IS NOT NULL
            ORDER BY rowid DESC LIMIT 1
            """,
            (cwd, rows[0]["session_id"]),
        ).fetchone()
        return None if row is None else self._event_from_row(row)

    def put_test_repository_mapping(self, mapping: TestRepositoryMapping) -> None:
        if not isinstance(mapping, TestRepositoryMapping):
            raise TypeError("mapping must be a TestRepositoryMapping")
        if _REPOSITORY_ID.fullmatch(mapping.repository_id) is None:
            raise ValueError("repository_id is invalid")
        if _PRODUCT_ID.fullmatch(mapping.product_id) is None:
            raise ValueError("product_id is invalid")
        if not isinstance(mapping.product_name, str) or not mapping.product_name:
            raise ValueError("product_name is invalid")
        if not isinstance(mapping.enabled, bool):
            raise ValueError("enabled must be a boolean")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO feasibility_repository_mappings (
                    repository_id, product_id, product_name, enabled
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(repository_id) DO UPDATE SET
                    product_id = excluded.product_id,
                    product_name = excluded.product_name,
                    enabled = excluded.enabled
                """,
                (
                    mapping.repository_id,
                    mapping.product_id,
                    mapping.product_name,
                    int(mapping.enabled),
                ),
            )

    def get_repository_mapping(
        self, repository_id: str
    ) -> TestRepositoryMapping | None:
        row = self._connection.execute(
            """
            SELECT repository_id, product_id, product_name, enabled
            FROM feasibility_repository_mappings WHERE repository_id = ?
            """,
            (repository_id,),
        ).fetchone()
        if row is None:
            return None
        return TestRepositoryMapping(
            repository_id=row["repository_id"],
            product_id=row["product_id"],
            product_name=row["product_name"],
            enabled=bool(row["enabled"]),
        )

    def count_events(self, *, cwd: str | None = None) -> int:
        if cwd is None:
            row = self._connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM events WHERE cwd = ?", (cwd,)
            ).fetchone()
        return int(row["count"])

    def renew_session(
        self,
        session_id: str,
        cwd: str,
        *,
        renewed_at: datetime,
        expires_at: datetime,
        create: bool,
    ) -> bool:
        renewed = _format_datetime(renewed_at)
        expires = _format_datetime(expires_at)
        if expires_at <= renewed_at:
            raise ValueError("Session lease expiry must follow renewal")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is invalid")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("cwd is invalid")
        if not isinstance(create, bool):
            raise TypeError("create must be a boolean")
        with self._connection:
            if create:
                cursor = self._connection.execute(
                    """
                    INSERT INTO session_leases (
                        session_id, cwd, renewed_at, expires_at, ended_at
                    ) VALUES (?, ?, ?, ?, NULL)
                    ON CONFLICT(session_id) DO UPDATE SET
                        cwd = excluded.cwd,
                        renewed_at = excluded.renewed_at,
                        expires_at = excluded.expires_at,
                        ended_at = NULL
                    """,
                    (session_id, cwd, renewed, expires),
                )
            else:
                cursor = self._connection.execute(
                    """
                    UPDATE session_leases
                    SET cwd = ?, renewed_at = ?, expires_at = ?
                    WHERE session_id = ? AND ended_at IS NULL
                    """,
                    (cwd, renewed, expires, session_id),
                )
        return cursor.rowcount > 0

    def end_session(self, session_id: str, *, ended_at: datetime) -> bool:
        ended = _format_datetime(ended_at)
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE session_leases
                SET ended_at = ?, expires_at = ?
                WHERE session_id = ? AND ended_at IS NULL
                """,
                (ended, ended, session_id),
            )
        return cursor.rowcount > 0

    def active_session_leases(self, now: datetime) -> tuple[SessionLease, ...]:
        current = _format_datetime(now)
        rows = self._connection.execute(
            """
            SELECT session_id, cwd, renewed_at, expires_at, ended_at
            FROM session_leases
            WHERE ended_at IS NULL AND expires_at > ?
            ORDER BY session_id
            """,
            (current,),
        ).fetchall()
        return tuple(
            SessionLease(
                session_id=row["session_id"],
                cwd=row["cwd"],
                renewed_at=_parse_datetime(row["renewed_at"]),
                expires_at=_parse_datetime(row["expires_at"]),
                ended_at=(
                    None
                    if row["ended_at"] is None
                    else _parse_datetime(row["ended_at"])
                ),
            )
            for row in rows
        )

    def claim_events(
        self,
        now: datetime,
        *,
        limit: int,
        processing_lease_seconds: float,
    ) -> tuple[AgentEvent, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("claim limit must be positive")
        if processing_lease_seconds <= 0:
            raise ValueError("processing lease must be positive")
        current = _format_datetime(now)
        expires = _format_datetime(
            now + timedelta(seconds=processing_lease_seconds)
        )
        cursor = self._connection.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            rows = cursor.execute(
                """
                SELECT event_id
                FROM events
                WHERE state = 'recorded'
                   OR (
                       state = 'failed_retryable'
                       AND retry_at IS NOT NULL
                       AND retry_at <= ?
                   )
                ORDER BY rowid
                LIMIT ?
                """,
                (current, limit),
            ).fetchall()
            event_ids = tuple(row["event_id"] for row in rows)
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                cursor.execute(
                    f"""
                    UPDATE events
                    SET state = 'processing',
                        failure_code = NULL,
                        attempt_count = attempt_count + 1,
                        processing_expires_at = ?,
                        retry_at = NULL
                    WHERE event_id IN ({placeholders})
                    """,
                    (expires, *event_ids),
                )
                claimed_rows = cursor.execute(
                    f"""
                    SELECT * FROM events
                    WHERE event_id IN ({placeholders})
                    ORDER BY rowid
                    """,
                    event_ids,
                ).fetchall()
            else:
                claimed_rows = []
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise
        finally:
            cursor.close()
        return tuple(self._event_from_row(row) for row in claimed_rows)

    def consume_event(self, event_id: str) -> bool:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE events
                SET state = 'consumed',
                    failure_code = NULL,
                    processing_expires_at = NULL,
                    retry_at = NULL
                WHERE event_id = ? AND state = 'processing'
                """,
                (event_id,),
            )
        return cursor.rowcount == 1

    def fail_event(
        self,
        event_id: str,
        *,
        failure_code: str,
        retry_at: datetime | None,
        terminal: bool = False,
    ) -> bool:
        if _FAILURE_CODE.fullmatch(failure_code) is None:
            raise ValueError("failure_code is invalid")
        if not isinstance(terminal, bool):
            raise TypeError("terminal must be a boolean")
        if not terminal and retry_at is None:
            raise ValueError("retryable failures require retry_at")
        retry = None if retry_at is None else _format_datetime(retry_at)
        state = "failed_terminal" if terminal else "failed_retryable"
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE events
                SET state = ?, failure_code = ?,
                    processing_expires_at = NULL, retry_at = ?
                WHERE event_id = ? AND state = 'processing'
                """,
                (state, failure_code, retry, event_id),
            )
        return cursor.rowcount == 1

    def requeue_expired_claims(self, now: datetime) -> int:
        current = _format_datetime(now)
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE events
                SET state = 'failed_retryable',
                    failure_code = 'processing_lease_expired',
                    processing_expires_at = NULL,
                    retry_at = ?
                WHERE state = 'processing'
                  AND processing_expires_at IS NOT NULL
                  AND processing_expires_at <= ?
                """,
                (current, current),
            )
        return cursor.rowcount

    def pending_event_count(self) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS count FROM events
            WHERE state IN ('recorded', 'processing', 'failed_retryable')
            """
        ).fetchone()
        return int(row["count"])

    def next_event_due_at(self, now: datetime) -> datetime | None:
        if self._connection.execute(
            "SELECT 1 FROM events WHERE state = 'recorded' LIMIT 1"
        ).fetchone() is not None:
            return now
        row = self._connection.execute(
            """
            SELECT MIN(due_at) AS due_at
            FROM (
                SELECT processing_expires_at AS due_at
                FROM events WHERE state = 'processing'
                UNION ALL
                SELECT retry_at AS due_at
                FROM events WHERE state = 'failed_retryable'
            )
            WHERE due_at IS NOT NULL
            """
        ).fetchone()
        return None if row["due_at"] is None else _parse_datetime(row["due_at"])

    def sync_probe(self) -> tuple[int, datetime | None]:
        row = self._connection.execute(
            "SELECT cursor, updated_at FROM sync_probe WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("Sync probe row is missing")
        return (
            int(row["cursor"]),
            None if row["updated_at"] is None else _parse_datetime(row["updated_at"]),
        )

    def update_sync_probe(
        self,
        *,
        expected_cursor: int,
        new_cursor: int,
        updated_at: datetime,
    ) -> int:
        if not isinstance(new_cursor, int) or isinstance(new_cursor, bool):
            raise ValueError("Sync cursor must be an integer")
        if new_cursor < expected_cursor:
            raise ValueError("Sync cursor cannot move backwards")
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE sync_probe SET cursor = ?, updated_at = ?
                WHERE singleton_id = 1 AND cursor = ?
                """,
                (new_cursor, _format_datetime(updated_at), expected_cursor),
            )
        if cursor.rowcount == 0:
            return self.sync_probe()[0]
        return new_cursor

    def get_feasibility_model_profile(
        self,
    ) -> StoredFeasibilityModelProfile | None:
        row = self._connection.execute(
            """
            SELECT profile_id, model_id, reasoning_effort,
                   discovery_digest, discovered_at
            FROM feasibility_model_profile WHERE singleton_id = 1
            """
        ).fetchone()
        if row is None:
            return None
        return StoredFeasibilityModelProfile(
            profile_id=row["profile_id"],
            model_id=row["model_id"],
            reasoning_effort=row["reasoning_effort"],
            discovery_digest=row["discovery_digest"],
            discovered_at=row["discovered_at"],
        )

    def freeze_feasibility_model_profile(
        self,
        *,
        profile_id: str,
        model_id: str,
        reasoning_effort: str,
        discovery_digest: str,
        discovered_at: str,
    ) -> StoredFeasibilityModelProfile:
        if _MODEL_PROFILE_ID.fullmatch(profile_id) is None:
            raise ValueError("profile_id is invalid")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("model_id is invalid")
        if not isinstance(reasoning_effort, str) or not reasoning_effort:
            raise ValueError("reasoning_effort is invalid")
        if _DIGEST.fullmatch(discovery_digest) is None:
            raise ValueError("discovery_digest is invalid")
        _parse_datetime(discovered_at)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO feasibility_model_profile (
                    singleton_id, profile_id, model_id, reasoning_effort,
                    discovery_digest, discovered_at
                ) VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO NOTHING
                """,
                (
                    profile_id,
                    model_id,
                    reasoning_effort,
                    discovery_digest,
                    discovered_at,
                ),
            )
        stored = self.get_feasibility_model_profile()
        if stored is None:
            raise RuntimeError("Frozen model profile could not be read back")
        if stored.discovery_digest != discovery_digest:
            raise FeasibilityModelProfileConflict(
                "Model discovery changed after the feasibility profile froze"
            )
        return stored

    def record_app_server_route(
        self,
        *,
        route: str,
        failure_code: str | None,
        recorded_at: datetime,
    ) -> None:
        if route not in {"host", "controlled_process"}:
            raise ValueError("app-server route is invalid")
        if failure_code is not None and _FAILURE_CODE.fullmatch(failure_code) is None:
            raise ValueError("failure_code is invalid")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO app_server_route_events (
                    route, failure_code, recorded_at
                ) VALUES (?, ?, ?)
                """,
                (route, failure_code, _format_datetime(recorded_at)),
            )

    def list_app_server_route_events(self) -> tuple[AppServerRouteRecord, ...]:
        rows = self._connection.execute(
            """
            SELECT route, failure_code, recorded_at
            FROM app_server_route_events ORDER BY route_event_id
            """
        ).fetchall()
        return tuple(
            AppServerRouteRecord(
                route=row["route"],
                failure_code=row["failure_code"],
                recorded_at=_parse_datetime(row["recorded_at"]),
            )
            for row in rows
        )

    def create_automated_capture_run(
        self, record: AutomatedCaptureRunRecord
    ) -> AutomatedCaptureRunRecord:
        _validate_automated_capture_run(record)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO automated_capture_runs (
                    automated_capture_id, session_id, source_turn_id,
                    repository_id, product_id, product_name, template_id,
                    template_snapshot_digest, eligibility_prompt_digest,
                    model_profile_id, state, assessment_thread_id,
                    assessment_turn_id, capture_operation_id,
                    capture_thread_id, inventory_turn_id, extraction_turn_id,
                    candidate_ids_json, failure_code, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(automated_capture_id) DO NOTHING
                """,
                _automated_capture_values(record),
            )
        stored = self.get_automated_capture_run(record.automated_capture_id)
        if stored != record:
            raise AutomatedCaptureConflict(
                "Automated Capture identity already has different state"
            )
        return stored

    def get_automated_capture_run(
        self, automated_capture_id: str
    ) -> AutomatedCaptureRunRecord | None:
        if _AUTOMATED_CAPTURE_ID.fullmatch(automated_capture_id) is None:
            raise ValueError("automated_capture_id is invalid")
        row = self._connection.execute(
            """
            SELECT * FROM automated_capture_runs
            WHERE automated_capture_id = ?
            """,
            (automated_capture_id,),
        ).fetchone()
        return None if row is None else _automated_capture_from_row(row)

    def replace_automated_capture_run(
        self,
        expected: AutomatedCaptureRunRecord,
        replacement: AutomatedCaptureRunRecord,
    ) -> AutomatedCaptureRunRecord:
        _validate_automated_capture_run(expected)
        _validate_automated_capture_run(replacement)
        immutable_fields = (
            "automated_capture_id",
            "session_id",
            "source_turn_id",
            "repository_id",
            "product_id",
            "product_name",
            "template_id",
            "template_snapshot_digest",
            "eligibility_prompt_digest",
            "model_profile_id",
            "created_at",
        )
        if any(
            getattr(expected, field_name) != getattr(replacement, field_name)
            for field_name in immutable_fields
        ):
            raise AutomatedCaptureConflict(
                "Automated Capture identity fields cannot change"
            )
        if replacement != expected and replacement.state not in (
            _AUTOMATED_CAPTURE_TRANSITIONS.get(expected.state, frozenset())
        ):
            raise AutomatedCaptureConflict(
                "Automated Capture state transition is invalid"
            )
        current = self.get_automated_capture_run(expected.automated_capture_id)
        if current is None:
            raise AutomatedCaptureConflict("Automated Capture run does not exist")
        if current == replacement:
            return current
        if current != expected:
            raise AutomatedCaptureConflict("Automated Capture run changed")
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE automated_capture_runs SET
                    state = ?, assessment_thread_id = ?, assessment_turn_id = ?,
                    capture_operation_id = ?, capture_thread_id = ?,
                    inventory_turn_id = ?, extraction_turn_id = ?,
                    candidate_ids_json = ?, failure_code = ?, updated_at = ?
                WHERE automated_capture_id = ? AND state = ? AND updated_at = ?
                """,
                (
                    replacement.state,
                    replacement.assessment_thread_id,
                    replacement.assessment_turn_id,
                    replacement.capture_operation_id,
                    replacement.capture_thread_id,
                    replacement.inventory_turn_id,
                    replacement.extraction_turn_id,
                    canonical_json_bytes(list(replacement.candidate_ids)),
                    replacement.failure_code,
                    replacement.updated_at,
                    expected.automated_capture_id,
                    expected.state,
                    expected.updated_at,
                ),
            )
        if cursor.rowcount != 1:
            raise AutomatedCaptureConflict("Automated Capture CAS failed")
        stored = self.get_automated_capture_run(replacement.automated_capture_id)
        if stored != replacement:
            raise AutomatedCaptureConflict(
                "Automated Capture replacement did not persist exactly"
            )
        return stored

    def automated_capture_id_for_boundary(
        self, session_id: str, source_turn_id: str
    ) -> str | None:
        rows = self._connection.execute(
            """
            SELECT automated_capture_id FROM automated_capture_runs
            WHERE session_id = ? AND source_turn_id = ?
            ORDER BY rowid
            """,
            (session_id, source_turn_id),
        ).fetchall()
        return None if not rows else rows[-1]["automated_capture_id"]

    def boundary_has_assessment(self, session_id: str, source_turn_id: str) -> bool:
        row = self._connection.execute(
            """
            SELECT 1 FROM boundary_assessments
            WHERE source_thread_id = ? AND source_turn_id = ? LIMIT 1
            """,
            (session_id, source_turn_id),
        ).fetchone()
        return row is not None

    def automated_capture_active(
        self,
        session_id: str,
        source_turn_id: str,
        *,
        excluding_id: str | None = None,
    ) -> bool:
        placeholders = ",".join("?" for _ in _TERMINAL_AUTOMATED_CAPTURE_STATES)
        parameters: list[object] = [
            session_id,
            source_turn_id,
            *_TERMINAL_AUTOMATED_CAPTURE_STATES,
        ]
        exclusion = ""
        if excluding_id is not None:
            exclusion = " AND automated_capture_id != ?"
            parameters.append(excluding_id)
        row = self._connection.execute(
            f"""
            SELECT 1 FROM automated_capture_runs
            WHERE session_id = ? AND source_turn_id = ?
              AND state NOT IN ({placeholders}){exclusion}
            LIMIT 1
            """,
            tuple(parameters),
        ).fetchone()
        return row is not None

    def put_boundary_assessment(
        self, record: BoundaryAssessmentRecord
    ) -> BoundaryAssessmentRecord:
        _validate_boundary_assessment_record(record)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO boundary_assessments (
                    automated_capture_id, source_thread_id, source_turn_id,
                    prompt_version, prompt_digest, input_fact_digest,
                    assessment_thread_id, assessment_turn_id,
                    model_profile_id, phase, has_durable_decision_signal,
                    validation, unresolved_blockers_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(automated_capture_id) DO NOTHING
                """,
                (
                    record.automated_capture_id,
                    record.source_thread_id,
                    record.source_turn_id,
                    record.prompt_version,
                    record.prompt_digest,
                    record.input_fact_digest,
                    record.assessment_thread_id,
                    record.assessment_turn_id,
                    record.model_profile_id,
                    record.phase,
                    int(record.has_durable_decision_signal),
                    record.validation,
                    canonical_json_bytes(list(record.unresolved_blockers)),
                    record.recorded_at,
                ),
            )
        stored = self.get_boundary_assessment(record.automated_capture_id)
        if stored != record:
            raise AutomatedCaptureConflict(
                "Boundary assessment already has different content"
            )
        return stored

    def get_boundary_assessment(
        self, automated_capture_id: str
    ) -> BoundaryAssessmentRecord | None:
        if _AUTOMATED_CAPTURE_ID.fullmatch(automated_capture_id) is None:
            raise ValueError("automated_capture_id is invalid")
        row = self._connection.execute(
            """
            SELECT * FROM boundary_assessments
            WHERE automated_capture_id = ?
            """,
            (automated_capture_id,),
        ).fetchone()
        return None if row is None else _boundary_assessment_from_row(row)

    def set_worker_owner(self, owner_pid: int, *, lease_expires_at: datetime) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE worker_state
                SET owner_pid = ?, lease_expires_at = ?
                WHERE singleton_id = 1
                """,
                (owner_pid, _format_datetime(lease_expires_at)),
            )

    def clear_worker_owner(self, owner_pid: int) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE worker_state
                SET owner_pid = NULL, lease_expires_at = NULL
                WHERE singleton_id = 1 AND owner_pid = ?
                """,
                (owner_pid,),
            )

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _invocation_values(
        event_id: str, invocation: HookInvocation
    ) -> tuple[object, ...]:
        stored_safe_fact = dict(invocation.safe_fact)
        if invocation.tool_name is not None:
            stored_safe_fact["_tool_name"] = invocation.tool_name
        return (
            event_id,
            invocation.event_name,
            invocation.occurred_at,
            invocation.session_id,
            invocation.turn_id,
            invocation.cwd,
            invocation.repository_id,
            invocation.worktree_root,
            invocation.branch,
            invocation.head_commit,
            canonical_json_bytes(stored_safe_fact),
            invocation.input_digest,
            "recorded",
            None,
        )

    @staticmethod
    def _row_canonical_values(row: sqlite3.Row) -> tuple[object, ...]:
        return tuple(
            row[field]
            for field in (
                "event_id",
                "event_type",
                "occurred_at",
                "session_id",
                "turn_id",
                "cwd",
                "repository_id",
                "worktree_root",
                "branch",
                "head_commit",
                "safe_fact_json",
                "input_digest",
                "state",
                "failure_code",
            )
        )

    @classmethod
    def _is_same_replay(
        cls, row: sqlite3.Row, incoming: tuple[object, ...]
    ) -> bool:
        stored = cls._row_canonical_values(row)
        return stored[:2] == incoming[:2] and stored[3:12] == incoming[3:12]

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> AgentEvent:
        safe_fact = json.loads(bytes(row["safe_fact_json"]))
        if not isinstance(safe_fact, dict):
            raise ValueError("Stored Event safe fact is invalid")
        tool_name = safe_fact.pop("_tool_name", None)
        if tool_name is not None and not isinstance(tool_name, str):
            raise ValueError("Stored Event tool name is invalid")
        state = row["state"]
        if state not in EVENT_STATES:
            raise ValueError("Stored Event state is invalid")
        invocation = HookInvocation(
            event_name=row["event_type"],
            occurred_at=row["occurred_at"],
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            cwd=row["cwd"],
            repository_id=row["repository_id"],
            worktree_root=row["worktree_root"],
            branch=row["branch"],
            head_commit=row["head_commit"],
            source=(safe_fact.get("source") if row["event_type"] == "SessionStart" else None),
            tool_name=(tool_name if row["event_type"] == "PostToolUse" else None),
            safe_fact=safe_fact,
            input_digest=row["input_digest"],
        )
        return AgentEvent(
            event_id=row["event_id"],
            invocation=invocation,
            state=state,
            failure_code=row["failure_code"],
        )


def _validate_automated_capture_run(record: AutomatedCaptureRunRecord) -> None:
    if not isinstance(record, AutomatedCaptureRunRecord):
        raise TypeError("record must be an AutomatedCaptureRunRecord")
    if _AUTOMATED_CAPTURE_ID.fullmatch(record.automated_capture_id) is None:
        raise ValueError("automated_capture_id is invalid")
    for field_name in (
        "session_id",
        "source_turn_id",
        "product_name",
        "template_id",
    ):
        value = getattr(record, field_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field_name} is invalid")
    if _REPOSITORY_ID.fullmatch(record.repository_id) is None:
        raise ValueError("repository_id is invalid")
    if _PRODUCT_ID.fullmatch(record.product_id) is None:
        raise ValueError("product_id is invalid")
    if _DIGEST.fullmatch(record.template_snapshot_digest) is None:
        raise ValueError("template_snapshot_digest is invalid")
    if _DIGEST.fullmatch(record.eligibility_prompt_digest) is None:
        raise ValueError("eligibility_prompt_digest is invalid")
    if _MODEL_PROFILE_ID.fullmatch(record.model_profile_id) is None:
        raise ValueError("model_profile_id is invalid")
    if record.state not in _AUTOMATED_CAPTURE_STATES:
        raise ValueError("Automated Capture state is invalid")
    for field_name in (
        "assessment_thread_id",
        "assessment_turn_id",
        "capture_thread_id",
        "inventory_turn_id",
        "extraction_turn_id",
    ):
        value = getattr(record, field_name)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{field_name} is invalid")
    if record.capture_operation_id is not None and _CAPTURE_OPERATION_ID.fullmatch(
        record.capture_operation_id
    ) is None:
        raise ValueError("capture_operation_id is invalid")
    if not isinstance(record.candidate_ids, tuple) or any(
        not isinstance(value, str) or not value for value in record.candidate_ids
    ):
        raise ValueError("candidate_ids is invalid")
    if len(set(record.candidate_ids)) != len(record.candidate_ids):
        raise ValueError("candidate_ids contains a duplicate")
    if record.failure_code is not None and _FAILURE_CODE.fullmatch(
        record.failure_code
    ) is None:
        raise ValueError("failure_code is invalid")
    _parse_datetime(record.created_at)
    _parse_datetime(record.updated_at)


def _automated_capture_values(
    record: AutomatedCaptureRunRecord,
) -> tuple[object, ...]:
    return (
        record.automated_capture_id,
        record.session_id,
        record.source_turn_id,
        record.repository_id,
        record.product_id,
        record.product_name,
        record.template_id,
        record.template_snapshot_digest,
        record.eligibility_prompt_digest,
        record.model_profile_id,
        record.state,
        record.assessment_thread_id,
        record.assessment_turn_id,
        record.capture_operation_id,
        record.capture_thread_id,
        record.inventory_turn_id,
        record.extraction_turn_id,
        canonical_json_bytes(list(record.candidate_ids)),
        record.failure_code,
        record.created_at,
        record.updated_at,
    )


def _automated_capture_from_row(row: sqlite3.Row) -> AutomatedCaptureRunRecord:
    record = AutomatedCaptureRunRecord(
        automated_capture_id=row["automated_capture_id"],
        session_id=row["session_id"],
        source_turn_id=row["source_turn_id"],
        repository_id=row["repository_id"],
        product_id=row["product_id"],
        product_name=row["product_name"],
        template_id=row["template_id"],
        template_snapshot_digest=row["template_snapshot_digest"],
        eligibility_prompt_digest=row["eligibility_prompt_digest"],
        model_profile_id=row["model_profile_id"],
        state=row["state"],
        assessment_thread_id=row["assessment_thread_id"],
        assessment_turn_id=row["assessment_turn_id"],
        capture_operation_id=row["capture_operation_id"],
        capture_thread_id=row["capture_thread_id"],
        inventory_turn_id=row["inventory_turn_id"],
        extraction_turn_id=row["extraction_turn_id"],
        candidate_ids=_stored_string_tuple(
            row["candidate_ids_json"], "candidate_ids"
        ),
        failure_code=row["failure_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
    _validate_automated_capture_run(record)
    return record


def _validate_boundary_assessment_record(record: BoundaryAssessmentRecord) -> None:
    if not isinstance(record, BoundaryAssessmentRecord):
        raise TypeError("record must be a BoundaryAssessmentRecord")
    if _AUTOMATED_CAPTURE_ID.fullmatch(record.automated_capture_id) is None:
        raise ValueError("automated_capture_id is invalid")
    for field_name in (
        "source_thread_id",
        "source_turn_id",
        "prompt_version",
        "assessment_thread_id",
        "assessment_turn_id",
    ):
        value = getattr(record, field_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field_name} is invalid")
    for field_name in ("prompt_digest", "input_fact_digest"):
        if _DIGEST.fullmatch(getattr(record, field_name)) is None:
            raise ValueError(f"{field_name} is invalid")
    if _MODEL_PROFILE_ID.fullmatch(record.model_profile_id) is None:
        raise ValueError("model_profile_id is invalid")
    if record.phase not in WORK_STATES:
        raise ValueError("phase is invalid")
    if not isinstance(record.has_durable_decision_signal, bool):
        raise ValueError("has_durable_decision_signal is invalid")
    if record.validation not in VALIDATION_STATES:
        raise ValueError("validation is invalid")
    if (
        not isinstance(record.unresolved_blockers, tuple)
        or len(record.unresolved_blockers) > 20
        or any(
            not isinstance(value, str) or not value or len(value) > 256
            for value in record.unresolved_blockers
        )
        or len(set(record.unresolved_blockers)) != len(record.unresolved_blockers)
    ):
        raise ValueError("unresolved_blockers is invalid")
    _parse_datetime(record.recorded_at)


def _boundary_assessment_from_row(row: sqlite3.Row) -> BoundaryAssessmentRecord:
    record = BoundaryAssessmentRecord(
        automated_capture_id=row["automated_capture_id"],
        source_thread_id=row["source_thread_id"],
        source_turn_id=row["source_turn_id"],
        prompt_version=row["prompt_version"],
        prompt_digest=row["prompt_digest"],
        input_fact_digest=row["input_fact_digest"],
        assessment_thread_id=row["assessment_thread_id"],
        assessment_turn_id=row["assessment_turn_id"],
        model_profile_id=row["model_profile_id"],
        phase=row["phase"],
        has_durable_decision_signal=bool(row["has_durable_decision_signal"]),
        validation=row["validation"],
        unresolved_blockers=_stored_string_tuple(
            row["unresolved_blockers_json"], "unresolved_blockers"
        ),
        recorded_at=row["recorded_at"],
    )
    _validate_boundary_assessment_record(record)
    return record


def _stored_string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(bytes(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError(f"Stored {field_name} is invalid") from None
    if not isinstance(decoded, list) or any(
        not isinstance(member, str) for member in decoded
    ):
        raise ValueError(f"Stored {field_name} is invalid")
    return tuple(decoded)


def _format_datetime(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_datetime(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Stored datetime is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Stored datetime is invalid") from None
    if parsed.tzinfo is None:
        raise ValueError("Stored datetime is invalid")
    return parsed.astimezone(UTC)
