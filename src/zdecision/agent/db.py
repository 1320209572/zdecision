"""SQLite Event Ledger for the device-local ZDecision Agent."""

from __future__ import annotations

import json
import re
import sqlite3
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


class AgentEventConflict(Exception):
    """Raised when one event identity is replayed with different content."""


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
                    failure_code TEXT
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
                PRAGMA user_version = 1;
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
        return stored[:2] == incoming[:2] and stored[3:] == incoming[3:]

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
