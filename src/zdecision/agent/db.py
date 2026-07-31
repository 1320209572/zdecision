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


class AgentEventConflict(Exception):
    """Raised when one event identity is replayed with different content."""


class FeasibilityModelProfileConflict(Exception):
    """Raised when model discovery changes after the feasibility profile freezes."""


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

                CREATE INDEX IF NOT EXISTS events_work_queue
                    ON events(state, retry_at, processing_expires_at);

                INSERT INTO worker_state (
                    singleton_id, owner_pid, lease_expires_at
                ) VALUES (1, NULL, NULL)
                ON CONFLICT(singleton_id) DO NOTHING;

                INSERT INTO sync_probe (singleton_id, cursor, updated_at)
                VALUES (1, 0, NULL)
                ON CONFLICT(singleton_id) DO NOTHING;

                PRAGMA user_version = 5;
                """
            )
            _retire_legacy_automatic_capture(connection)
        return cls(database_path, connection)

    def retire_legacy_automatic_capture(self) -> bool:
        """Drop the obsolete journal after the on-demand stores exist."""

        with self._connection:
            return _retire_legacy_automatic_capture(self._connection)

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


def _retire_legacy_automatic_capture(connection: sqlite3.Connection) -> bool:
    """Remove the superseded journal only after every replacement store exists."""

    table_names = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    required_replacements = {
        "session_checkpoints",
        "native_attempts",
        "reconciliation_results",
        "candidate_outbox",
    }
    if not required_replacements.issubset(table_names):
        return False
    legacy_present = bool(
        {"automated_capture_runs", "boundary_assessments"} & table_names
    )
    connection.execute("DROP TABLE IF EXISTS boundary_assessments")
    connection.execute("DROP INDEX IF EXISTS automated_capture_boundary")
    connection.execute("DROP TABLE IF EXISTS automated_capture_runs")
    return legacy_present


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
