"""Durable trusted host state for opt-in decision recall."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.session import RecallSessionState, TurnGateResult


TurnGateState = Literal["pending", "committed", "blocked"]
ContextSource = Literal["compact", "clear"]
InternalThreadPurpose = Literal["capture", "reconciliation"]


@dataclass(frozen=True)
class RecallSession:
    session_id: str
    state: RecallSessionState
    authorization_turn_id: str
    cwd: str
    context_epoch: int
    intent_epoch: int
    active_intent_digest: str | None
    active_set_digest: str | None
    last_gate_turn_id: str | None


@dataclass(frozen=True)
class TurnGate:
    gate_id: str
    session_id: str
    turn_id: str
    context_epoch: int
    intent_epoch: int
    active_generation: int | None
    state: TurnGateState
    result_digest: str | None


@dataclass(frozen=True)
class ContextRestoration:
    session_id: str
    source: ContextSource
    latest_observed_turn_id: str
    active_set_digest: str | None
    context_epoch: int
    compaction_key: str


@dataclass(frozen=True)
class InternalThreadBinding:
    thread_id: str
    parent_thread_id: str
    purpose: InternalThreadPurpose
    operation_id: str
    created_at: str


class RecallGateConflict(ValueError):
    """A trusted recall state transition conflicts with durable state."""


class RecallHostStore:
    """SQLite owner of trusted activation, native turn, and context state."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> "RecallHostStore":
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        with connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS recall_sessions (
                    session_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK(state IN (
                        'activating', 'active', 'blocked', 'bypassed',
                        'dormant', 'closed'
                    )),
                    authorization_turn_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    context_epoch INTEGER NOT NULL CHECK(context_epoch >= 0),
                    intent_epoch INTEGER NOT NULL CHECK(intent_epoch >= 0),
                    active_intent_digest TEXT,
                    active_set_digest TEXT,
                    last_gate_turn_id TEXT,
                    ended_at TEXT,
                    resumed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS recall_activation_bindings (
                    binding_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recall_turn_gates (
                    gate_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    context_epoch INTEGER NOT NULL CHECK(context_epoch >= 0),
                    intent_epoch INTEGER NOT NULL CHECK(intent_epoch >= 0),
                    active_generation INTEGER CHECK(
                        active_generation IS NULL OR active_generation >= 0
                    ),
                    state TEXT NOT NULL CHECK(state IN (
                        'pending', 'committed', 'blocked'
                    )),
                    result_digest TEXT,
                    UNIQUE(session_id, turn_id)
                );

                CREATE TABLE IF NOT EXISTS recall_context_restorations (
                    session_id TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('compact', 'clear')),
                    latest_observed_turn_id TEXT NOT NULL,
                    active_set_digest_key TEXT NOT NULL,
                    active_set_digest TEXT,
                    context_epoch INTEGER NOT NULL CHECK(context_epoch >= 0),
                    compaction_key TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(
                        session_id, source, latest_observed_turn_id,
                        active_set_digest_key
                    )
                );

                CREATE TABLE IF NOT EXISTS recall_internal_threads (
                    thread_id TEXT PRIMARY KEY,
                    parent_thread_id TEXT NOT NULL,
                    purpose TEXT NOT NULL CHECK(
                        purpose IN ('capture', 'reconciliation')
                    ),
                    operation_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                """
            )
        return cls(database_path, connection)

    def close(self) -> None:
        self._connection.close()

    def get_session(self, session_id: str) -> RecallSession | None:
        session = _text(session_id, "session_id")
        row = self._session_row(session)
        return None if row is None else _session(row)

    def bind_activation(
        self,
        *,
        session_id: str,
        turn_id: str,
        cwd: str,
        binding_id: str,
        now: datetime,
    ) -> RecallSession:
        session = _text(session_id, "session_id")
        turn = _text(turn_id, "turn_id")
        binding = _text(binding_id, "binding_id")
        working_directory = _absolute_path(cwd)
        created_at = _timestamp(_aware_utc(now, "now"))
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if self.is_internal_thread(session):
                raise RecallGateConflict("internal threads are recall-disabled")
            bound = self._connection.execute(
                "SELECT * FROM recall_activation_bindings WHERE binding_id = ?",
                (binding,),
            ).fetchone()
            if bound is not None:
                if (
                    bound["session_id"] != session
                    or bound["turn_id"] != turn
                    or bound["cwd"] != working_directory
                ):
                    raise RecallGateConflict("activation binding is already frozen")
                result = self._required_session(session)
                self._connection.commit()
                return result
            current = self._session_row(session)
            if current is None:
                self._connection.execute(
                    """
                    INSERT INTO recall_sessions(
                        session_id, state, authorization_turn_id, cwd,
                        context_epoch, intent_epoch, active_intent_digest,
                        active_set_digest, last_gate_turn_id, ended_at, resumed_at
                    ) VALUES (?, 'active', ?, ?, 0, 0, NULL, NULL, NULL, NULL, NULL)
                    """,
                    (session, turn, working_directory),
                )
            elif current["state"] == "activating":
                self._connection.execute(
                    """
                    UPDATE recall_sessions
                    SET state = 'active', authorization_turn_id = ?, cwd = ?,
                        ended_at = NULL
                    WHERE session_id = ?
                    """,
                    (turn, working_directory, session),
                )
            else:
                raise RecallGateConflict("session already has an activation")
            self._connection.execute(
                """
                INSERT INTO recall_activation_bindings(
                    binding_id, session_id, turn_id, cwd, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (binding, session, turn, working_directory, created_at),
            )
            result = self._required_session(session)
            self._connection.commit()
            return result
        except Exception:
            self._connection.rollback()
            raise

    def begin_turn_gate(
        self,
        *,
        session_id: str,
        turn_id: str,
        context_epoch: int,
        intent_epoch: int,
        active_generation: int | None,
        gate_id: str,
    ) -> TurnGate:
        session = _text(session_id, "session_id")
        turn = _text(turn_id, "turn_id")
        gate = _text(gate_id, "gate_id")
        context = _epoch(context_epoch, "context_epoch")
        intent = _epoch(intent_epoch, "intent_epoch")
        generation = _generation(active_generation)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if self.is_internal_thread(session):
                raise RecallGateConflict("internal threads are recall-disabled")
            existing = self._gate_for_turn(session, turn)
            if existing is not None:
                if _gate(existing) != TurnGate(
                    gate_id=gate,
                    session_id=session,
                    turn_id=turn,
                    context_epoch=context,
                    intent_epoch=intent,
                    active_generation=generation,
                    state=existing["state"],
                    result_digest=existing["result_digest"],
                ):
                    raise RecallGateConflict("native turn gate is already frozen")
                result = _gate(existing)
                self._connection.commit()
                return result
            if self._gate_by_id(gate) is not None:
                raise RecallGateConflict("gate ID already belongs to another turn")
            current = self._session_row(session)
            if current is None or current["state"] != "active":
                raise RecallGateConflict("session is not active for recall")
            if current["context_epoch"] != context or current["intent_epoch"] != intent:
                raise RecallGateConflict("turn gate epoch is stale")
            self._connection.execute(
                """
                INSERT INTO recall_turn_gates(
                    gate_id, session_id, turn_id, context_epoch, intent_epoch,
                    active_generation, state, result_digest
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)
                """,
                (gate, session, turn, context, intent, generation),
            )
            result = _gate(self._gate_by_id(gate))
            self._connection.commit()
            return result
        except Exception:
            self._connection.rollback()
            raise

    def commit_turn_gate(
        self,
        *,
        session_id: str,
        turn_id: str,
        gate_id: str,
        result: TurnGateResult,
        active_set_digest: str | None,
    ) -> TurnGate:
        session = _text(session_id, "session_id")
        turn = _text(turn_id, "turn_id")
        gate_id = _text(gate_id, "gate_id")
        active_set = _optional_text(active_set_digest, "active_set_digest")
        gate: sqlite3.Row | None = None
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            gate = self._gate_for_turn(session, turn)
            if gate is None or gate["gate_id"] != gate_id:
                raise RecallGateConflict("turn gate does not match trusted binding")
            if gate["state"] == "committed":
                if gate["result_digest"] != _result_digest(result):
                    raise RecallGateConflict("turn gate result is already frozen")
                committed = _gate(gate)
                self._connection.commit()
                return committed
            if gate["state"] != "pending":
                raise RecallGateConflict("turn gate is blocked")
            current = self._required_session(session)
            _validate_result(result, gate, current)
            if result.disposition == "blocked":
                self._connection.execute(
                    """
                    UPDATE recall_turn_gates
                    SET state = 'blocked', result_digest = ?
                    WHERE gate_id = ?
                    """,
                    (_result_digest(result), gate_id),
                )
                blocked = _gate(self._gate_by_id(gate_id))
                self._connection.commit()
                return blocked
            digest = _result_digest(result)
            self._connection.execute(
                """
                UPDATE recall_turn_gates
                SET state = 'committed', result_digest = ?
                WHERE gate_id = ?
                """,
                (digest, gate_id),
            )
            self._connection.execute(
                """
                UPDATE recall_sessions
                SET context_epoch = ?, intent_epoch = ?,
                    active_intent_digest = ?, active_set_digest = ?,
                    last_gate_turn_id = ?
                WHERE session_id = ?
                """,
                (
                    result.context_epoch,
                    result.intent_epoch,
                    result.intent_digest,
                    active_set,
                    turn,
                    session,
                ),
            )
            committed = _gate(self._gate_by_id(gate_id))
            self._connection.commit()
            return committed
        except RecallGateConflict:
            if gate is not None and gate["state"] == "pending":
                self._connection.execute(
                    "UPDATE recall_turn_gates SET state = 'blocked' WHERE gate_id = ?",
                    (gate_id,),
                )
                self._connection.commit()
            else:
                self._connection.rollback()
            raise
        except Exception:
            self._connection.rollback()
            raise

    def require_committed_gate(self, session_id: str, turn_id: str) -> TurnGate:
        session = _text(session_id, "session_id")
        turn = _text(turn_id, "turn_id")
        row = self._gate_for_turn(session, turn)
        if row is None or row["state"] != "committed":
            raise RecallGateConflict("turn gate is not committed")
        return _gate(row)

    def begin_context_epoch(
        self,
        *,
        session_id: str,
        source: ContextSource,
        latest_observed_turn_id: str,
        active_set_digest: str | None,
        compaction_key: str,
    ) -> ContextRestoration:
        session = _text(session_id, "session_id")
        if source not in ("compact", "clear"):
            raise ValueError("source is invalid")
        latest_turn = _text(latest_observed_turn_id, "latest_observed_turn_id")
        active_set = _optional_text(active_set_digest, "active_set_digest")
        key = _text(compaction_key, "compaction_key")
        active_set_key = active_set or ""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._session_row(session)
            if current is None or current["state"] != "active":
                raise RecallGateConflict("session is not active for restoration")
            if (
                current["last_gate_turn_id"] != latest_turn
                or current["active_set_digest"] != active_set
            ):
                raise RecallGateConflict("context restoration is stale")
            row = self._connection.execute(
                """
                SELECT * FROM recall_context_restorations
                WHERE session_id = ? AND source = ?
                  AND latest_observed_turn_id = ? AND active_set_digest_key = ?
                """,
                (session, source, latest_turn, active_set_key),
            ).fetchone()
            if row is not None:
                if row["compaction_key"] != key:
                    raise RecallGateConflict("context restoration key is frozen")
                replay = _restoration(row)
                self._connection.commit()
                return replay
            if self._connection.execute(
                "SELECT 1 FROM recall_context_restorations WHERE compaction_key = ?",
                (key,),
            ).fetchone() is not None:
                raise RecallGateConflict("compaction key already belongs to another event")
            epoch = current["context_epoch"] + 1
            self._connection.execute(
                """
                INSERT INTO recall_context_restorations(
                    session_id, source, latest_observed_turn_id,
                    active_set_digest_key, active_set_digest, context_epoch,
                    compaction_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session, source, latest_turn, active_set_key, active_set, epoch, key),
            )
            self._connection.execute(
                "UPDATE recall_sessions SET context_epoch = ? WHERE session_id = ?",
                (epoch, session),
            )
            restoration = _restoration(
                self._connection.execute(
                    "SELECT * FROM recall_context_restorations WHERE compaction_key = ?",
                    (key,),
                ).fetchone()
            )
            self._connection.commit()
            return restoration
        except Exception:
            self._connection.rollback()
            raise

    def mark_dormant(
        self, session_id: str, ended_at: datetime
    ) -> RecallSession | None:
        session = _text(session_id, "session_id")
        ended = _timestamp(_aware_utc(ended_at, "ended_at"))
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._session_row(session)
            if current is None:
                self._connection.commit()
                return None
            if current["state"] == "active":
                self._connection.execute(
                    """
                    UPDATE recall_sessions
                    SET state = 'dormant', ended_at = ?
                    WHERE session_id = ?
                    """,
                    (ended, session),
                )
            result = self._required_session(session)
            self._connection.commit()
            return result
        except Exception:
            self._connection.rollback()
            raise

    def begin_resume(
        self, session_id: str, cwd: str, now: datetime
    ) -> RecallSession | None:
        session = _text(session_id, "session_id")
        working_directory = _absolute_path(cwd)
        resumed = _timestamp(_aware_utc(now, "now"))
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._session_row(session)
            if current is None:
                self._connection.commit()
                return None
            if current["state"] != "dormant":
                raise RecallGateConflict("session is not dormant")
            self._connection.execute(
                """
                UPDATE recall_sessions
                SET state = 'activating', cwd = ?, resumed_at = ?
                WHERE session_id = ?
                """,
                (working_directory, resumed, session),
            )
            result = self._required_session(session)
            self._connection.commit()
            return result
        except Exception:
            self._connection.rollback()
            raise

    def bind_internal_thread(
        self,
        *,
        thread_id: str,
        parent_thread_id: str,
        purpose: InternalThreadPurpose,
        operation_id: str,
        now: datetime,
    ) -> InternalThreadBinding:
        thread = _text(thread_id, "thread_id")
        parent = _text(parent_thread_id, "parent_thread_id")
        if purpose not in ("capture", "reconciliation"):
            raise ValueError("purpose is invalid")
        operation = _text(operation_id, "operation_id")
        created_at = _timestamp(_aware_utc(now, "now"))
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self._connection.execute(
                "SELECT * FROM recall_internal_threads WHERE thread_id = ?", (thread,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["parent_thread_id"] != parent
                    or existing["purpose"] != purpose
                    or existing["operation_id"] != operation
                ):
                    raise RecallGateConflict("internal thread binding is frozen")
                result = _internal_thread(existing)
                self._connection.commit()
                return result
            try:
                self._connection.execute(
                    """
                    INSERT INTO recall_internal_threads(
                        thread_id, parent_thread_id, purpose, operation_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (thread, parent, purpose, operation, created_at),
                )
            except sqlite3.IntegrityError as error:
                raise RecallGateConflict(
                    "operation ID already belongs to another internal thread"
                ) from error
            result = _internal_thread(
                self._connection.execute(
                    "SELECT * FROM recall_internal_threads WHERE thread_id = ?",
                    (thread,),
                ).fetchone()
            )
            self._connection.commit()
            return result
        except Exception:
            self._connection.rollback()
            raise

    def is_internal_thread(self, thread_id: str) -> bool:
        thread = _text(thread_id, "thread_id")
        return (
            self._connection.execute(
                "SELECT 1 FROM recall_internal_threads WHERE thread_id = ?", (thread,)
            ).fetchone()
            is not None
        )

    def _session_row(self, session_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM recall_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()

    def _required_session(self, session_id: str) -> RecallSession:
        row = self._session_row(session_id)
        if row is None:
            raise RecallGateConflict("recall session was not found")
        return _session(row)

    def _gate_for_turn(self, session_id: str, turn_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT * FROM recall_turn_gates
            WHERE session_id = ? AND turn_id = ?
            """,
            (session_id, turn_id),
        ).fetchone()

    def _gate_by_id(self, gate_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM recall_turn_gates WHERE gate_id = ?", (gate_id,)
        ).fetchone()


def _session(row: sqlite3.Row) -> RecallSession:
    return RecallSession(
        session_id=row["session_id"],
        state=row["state"],
        authorization_turn_id=row["authorization_turn_id"],
        cwd=row["cwd"],
        context_epoch=row["context_epoch"],
        intent_epoch=row["intent_epoch"],
        active_intent_digest=row["active_intent_digest"],
        active_set_digest=row["active_set_digest"],
        last_gate_turn_id=row["last_gate_turn_id"],
    )


def _gate(row: sqlite3.Row | None) -> TurnGate:
    if row is None:
        raise RecallGateConflict("turn gate was not found")
    return TurnGate(
        gate_id=row["gate_id"],
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        context_epoch=row["context_epoch"],
        intent_epoch=row["intent_epoch"],
        active_generation=row["active_generation"],
        state=row["state"],
        result_digest=row["result_digest"],
    )


def _restoration(row: sqlite3.Row | None) -> ContextRestoration:
    if row is None:
        raise RecallGateConflict("context restoration was not found")
    return ContextRestoration(
        session_id=row["session_id"],
        source=row["source"],
        latest_observed_turn_id=row["latest_observed_turn_id"],
        active_set_digest=row["active_set_digest"],
        context_epoch=row["context_epoch"],
        compaction_key=row["compaction_key"],
    )


def _internal_thread(row: sqlite3.Row | None) -> InternalThreadBinding:
    if row is None:
        raise RecallGateConflict("internal thread binding was not found")
    return InternalThreadBinding(
        thread_id=row["thread_id"],
        parent_thread_id=row["parent_thread_id"],
        purpose=row["purpose"],
        operation_id=row["operation_id"],
        created_at=row["created_at"],
    )


def _validate_result(
    result: TurnGateResult, gate: sqlite3.Row, session: RecallSession
) -> None:
    if not isinstance(result, TurnGateResult):
        raise RecallGateConflict("turn gate result is invalid")
    if result.disposition not in (
        "reuse",
        "retrieve",
        "clarify_product",
        "refresh_required",
        "blocked",
    ):
        raise RecallGateConflict("turn gate disposition is invalid")
    _text(result.intent_digest, "intent_digest")
    if result.context_epoch != gate["context_epoch"]:
        raise RecallGateConflict("turn gate context epoch is stale")
    if not isinstance(result.intent_epoch, int) or isinstance(result.intent_epoch, bool):
        raise RecallGateConflict("turn gate intent epoch is invalid")
    if not gate["intent_epoch"] <= result.intent_epoch <= gate["intent_epoch"] + 1:
        raise RecallGateConflict("turn gate intent epoch is stale")
    if session.context_epoch != gate["context_epoch"]:
        raise RecallGateConflict("session context epoch is stale")
    if session.intent_epoch != gate["intent_epoch"]:
        raise RecallGateConflict("session intent epoch is stale")


def _result_digest(result: TurnGateResult) -> str:
    return hashlib.sha256(canonical_json_bytes(asdict(result))).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} is invalid")
    return value


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _absolute_path(value: object) -> str:
    path = _text(value, "cwd")
    if not os.path.isabs(path):
        raise ValueError("cwd must be absolute")
    return os.path.normpath(path)


def _epoch(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} is invalid")
    return value


def _generation(value: object) -> int | None:
    return None if value is None else _epoch(value, "active_generation")


def _aware_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
