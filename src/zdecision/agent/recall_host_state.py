"""Durable trusted host state for opt-in decision recall."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.session import RecallSessionState, TurnGateResult


TurnGateState = Literal["pending", "committed", "blocked"]
ActivationAttemptState = Literal[
    "pending_confirmation", "declined", "cancelled", "failed", "committed"
]
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
class RecallActivationAttempt:
    attempt_id: str
    session_id: str
    turn_id: str
    cwd: str
    repository_id: str
    repository_display_name: str
    state: ActivationAttemptState
    created_at: str
    expires_at: str
    plugin_root: str | None
    plugin_bundle_digest: str | None
    ui_digest: str | None
    result_digest: str | None


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
    active_set_digest: str | None = None
    reference_state_version: int | None = None


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
    def open(
        cls, path: Path, *, timeout_seconds: float = 5.0
    ) -> "RecallHostStore":
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0 < timeout_seconds <= 5.0
        ):
            raise ValueError("timeout_seconds is invalid")
        database_path = Path(path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=timeout_seconds)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
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
                    created_at TEXT NOT NULL,
                    plugin_root TEXT,
                    plugin_bundle_digest TEXT
                );

                CREATE TABLE IF NOT EXISTS recall_activation_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    repository_display_name TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'pending_confirmation', 'declined', 'cancelled',
                        'failed', 'committed'
                    )),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    plugin_root TEXT,
                    plugin_bundle_digest TEXT,
                    ui_digest TEXT,
                    result_digest TEXT,
                    UNIQUE(session_id, turn_id)
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
                    commit_fingerprint TEXT,
                    active_set_digest TEXT,
                    reference_state_version INTEGER,
                    plugin_root TEXT,
                    plugin_bundle_digest TEXT,
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
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(recall_turn_gates)"
                ).fetchall()
            }
            if "commit_fingerprint" not in columns:
                connection.execute(
                    "ALTER TABLE recall_turn_gates ADD COLUMN commit_fingerprint TEXT"
                )
            if "active_set_digest" not in columns:
                connection.execute(
                    "ALTER TABLE recall_turn_gates ADD COLUMN "
                    "active_set_digest TEXT"
                )
            if "reference_state_version" not in columns:
                connection.execute(
                    "ALTER TABLE recall_turn_gates ADD COLUMN "
                    "reference_state_version INTEGER"
                )
            if "plugin_root" not in columns:
                connection.execute(
                    "ALTER TABLE recall_turn_gates ADD COLUMN plugin_root TEXT"
                )
            if "plugin_bundle_digest" not in columns:
                connection.execute(
                    "ALTER TABLE recall_turn_gates "
                    "ADD COLUMN plugin_bundle_digest TEXT"
                )
            activation_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(recall_activation_bindings)"
                ).fetchall()
            }
            if "plugin_root" not in activation_columns:
                connection.execute(
                    "ALTER TABLE recall_activation_bindings "
                    "ADD COLUMN plugin_root TEXT"
                )
            if "plugin_bundle_digest" not in activation_columns:
                connection.execute(
                    "ALTER TABLE recall_activation_bindings "
                    "ADD COLUMN plugin_bundle_digest TEXT"
                )
        return cls(database_path, connection)

    def close(self) -> None:
        self._connection.close()

    def get_session(self, session_id: str) -> RecallSession | None:
        session = _text(session_id, "session_id")
        if self._is_internal_thread(session):
            return None
        row = self._session_row(session)
        return None if row is None else _session(row)

    def get_turn_gate(self, session_id: str, turn_id: str) -> TurnGate | None:
        session = _text(session_id, "session_id")
        turn = _text(turn_id, "turn_id")
        if self._is_internal_thread(session):
            return None
        row = self._gate_for_turn(session, turn)
        return None if row is None else _gate(row)

    def get_activation_attempt(
        self, attempt_id: str
    ) -> RecallActivationAttempt | None:
        attempt = _text(attempt_id, "attempt_id")
        row = self._connection.execute(
            "SELECT * FROM recall_activation_attempts WHERE attempt_id = ?",
            (attempt,),
        ).fetchone()
        return None if row is None else _activation_attempt(row)

    def create_activation_attempt(
        self,
        *,
        session_id: str,
        turn_id: str,
        cwd: str,
        repository_id: str,
        repository_display_name: str,
        attempt_id: str,
        now: datetime,
        expires_at: datetime,
        plugin_root: str | None,
    ) -> RecallActivationAttempt:
        """Freeze one host-rendered confirmation request without activating Recall."""

        session = _text(session_id, "session_id")
        turn = _text(turn_id, "turn_id")
        working_directory = _absolute_path(cwd)
        repository = _repository_id(repository_id)
        display_name = _display_name(repository_display_name)
        attempt = _text(attempt_id, "attempt_id")
        created = _aware_utc(now, "now")
        expiry = _aware_utc(expires_at, "expires_at")
        if expiry <= created:
            raise ValueError("expires_at is invalid")
        installed_root, bundle_digest = _optional_installed_plugin_binding(plugin_root)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if self._is_internal_thread(session):
                raise RecallGateConflict("internal threads are recall-disabled")
            existing = self._connection.execute(
                "SELECT * FROM recall_activation_attempts WHERE attempt_id = ?",
                (attempt,),
            ).fetchone()
            if existing is not None:
                if not _matching_activation_attempt(
                    existing,
                    session_id=session,
                    turn_id=turn,
                    cwd=working_directory,
                    repository_id=repository,
                    repository_display_name=display_name,
                    plugin_root=installed_root,
                    plugin_bundle_digest=bundle_digest,
                ):
                    raise RecallGateConflict("activation attempt is already frozen")
                result = _activation_attempt(existing)
                self._connection.commit()
                return result
            if self._session_row(session) is not None:
                raise RecallGateConflict("session already has recall consent")
            current = self._connection.execute(
                """
                SELECT * FROM recall_activation_attempts
                WHERE session_id = ? AND turn_id = ?
                """,
                (session, turn),
            ).fetchone()
            if current is not None:
                if not _matching_activation_attempt(
                    current,
                    session_id=session,
                    turn_id=turn,
                    cwd=working_directory,
                    repository_id=repository,
                    repository_display_name=display_name,
                    plugin_root=installed_root,
                    plugin_bundle_digest=bundle_digest,
                ):
                    raise RecallGateConflict("turn already has an activation attempt")
                result = _activation_attempt(current)
                self._connection.commit()
                return result
            self._connection.execute(
                """
                INSERT INTO recall_activation_attempts(
                    attempt_id, session_id, turn_id, cwd, repository_id,
                    repository_display_name, state, created_at, expires_at,
                    plugin_root, plugin_bundle_digest, ui_digest, result_digest
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending_confirmation', ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    attempt, session, turn, working_directory, repository, display_name,
                    _timestamp(created), _timestamp(expiry), installed_root, bundle_digest,
                ),
            )
            result = _activation_attempt(
                self._connection.execute(
                    "SELECT * FROM recall_activation_attempts WHERE attempt_id = ?",
                    (attempt,),
                ).fetchone()
            )
            self._connection.commit()
            return result
        except Exception:
            self._connection.rollback()
            raise

    def attach_activation_card(
        self, attempt_id: str, *, ui_digest: str
    ) -> RecallActivationAttempt:
        """Bind a pending trusted attempt to exactly one rendered card."""

        attempt = _text(attempt_id, "attempt_id")
        digest = _digest(ui_digest, "ui_digest")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._required_activation_attempt(attempt)
            if row["ui_digest"] is None:
                if row["state"] != "pending_confirmation":
                    raise RecallGateConflict("activation attempt is terminal")
                self._connection.execute(
                    "UPDATE recall_activation_attempts SET ui_digest = ? WHERE attempt_id = ?",
                    (digest, attempt),
                )
            elif row["ui_digest"] != digest:
                raise RecallGateConflict("activation card is already frozen")
            result = _activation_attempt(self._required_activation_attempt(attempt))
            self._connection.commit()
            return result
        except Exception:
            self._connection.rollback()
            raise

    def decide_activation_attempt(
        self, attempt_id: str, *, action: str, now: datetime
    ) -> RecallActivationAttempt:
        """Atomically record an app decision and, for enable, create consent."""

        attempt = _text(attempt_id, "attempt_id")
        if action not in ("enable", "decline"):
            raise ValueError("action is invalid")
        decided = _aware_utc(now, "now")
        result_digest = hashlib.sha256(
            canonical_json_bytes({"action": action})
        ).hexdigest()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._required_activation_attempt(attempt)
            expected_state = "committed" if action == "enable" else "declined"
            if row["state"] != "pending_confirmation":
                if row["state"] == expected_state and row["result_digest"] == result_digest:
                    result = _activation_attempt(row)
                    self._connection.commit()
                    return result
                raise RecallGateConflict("activation attempt decision is already frozen")
            if row["ui_digest"] is None:
                raise RecallGateConflict("activation card was not rendered")
            if decided >= _parse_timestamp(row["expires_at"]):
                self._connection.execute(
                    "UPDATE recall_activation_attempts SET state = 'failed', result_digest = ? WHERE attempt_id = ?",
                    (hashlib.sha256(canonical_json_bytes({"action": "expired"})).hexdigest(), attempt),
                )
                self._connection.commit()
                raise RecallGateConflict("activation attempt expired")
            if action == "enable":
                if self._session_row(row["session_id"]) is not None:
                    raise RecallGateConflict("session already has recall consent")
                self._connection.execute(
                    """
                    INSERT INTO recall_sessions(
                        session_id, state, authorization_turn_id, cwd,
                        context_epoch, intent_epoch, active_intent_digest,
                        active_set_digest, last_gate_turn_id, ended_at, resumed_at
                    ) VALUES (?, 'active', ?, ?, 0, 0, NULL, NULL, NULL, NULL, NULL)
                    """,
                    (row["session_id"], row["turn_id"], row["cwd"]),
                )
            self._connection.execute(
                "UPDATE recall_activation_attempts SET state = ?, result_digest = ? WHERE attempt_id = ?",
                (expected_state, result_digest, attempt),
            )
            result = _activation_attempt(self._required_activation_attempt(attempt))
            self._connection.commit()
            return result
        except Exception:
            self._connection.rollback()
            raise

    def retire_activation_attempts(
        self, session_id: str, *, now: datetime
    ) -> tuple[RecallActivationAttempt, ...]:
        """Cancel outstanding confirmation cards when their native Session ends."""

        session = _text(session_id, "session_id")
        _aware_utc(now, "now")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                """
                UPDATE recall_activation_attempts
                SET state = 'cancelled', result_digest = ?
                WHERE session_id = ? AND state = 'pending_confirmation'
                """,
                (hashlib.sha256(canonical_json_bytes({"action": "cancelled"})).hexdigest(), session),
            )
            rows = self._connection.execute(
                "SELECT * FROM recall_activation_attempts WHERE session_id = ? ORDER BY created_at",
                (session,),
            ).fetchall()
            self._connection.commit()
            return tuple(_activation_attempt(row) for row in rows)
        except Exception:
            self._connection.rollback()
            raise

    def bind_activation(
        self,
        *,
        session_id: str,
        turn_id: str,
        cwd: str,
        binding_id: str,
        now: datetime,
        plugin_root: str | None = None,
    ) -> RecallSession:
        session = _text(session_id, "session_id")
        turn = _text(turn_id, "turn_id")
        binding = _text(binding_id, "binding_id")
        working_directory = _absolute_path(cwd)
        created_at = _timestamp(_aware_utc(now, "now"))
        installed_root, bundle_digest = _optional_installed_plugin_binding(
            plugin_root
        )
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
                    or bound["plugin_root"] != installed_root
                    or bound["plugin_bundle_digest"] != bundle_digest
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
                    SET state = 'active', cwd = ?, ended_at = NULL
                    WHERE session_id = ?
                    """,
                    (working_directory, session),
                )
            else:
                raise RecallGateConflict("session already has an activation")
            self._connection.execute(
                """
                INSERT INTO recall_activation_bindings(
                    binding_id, session_id, turn_id, cwd, created_at,
                    plugin_root, plugin_bundle_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding,
                    session,
                    turn,
                    working_directory,
                    created_at,
                    installed_root,
                    bundle_digest,
                ),
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
        plugin_root: str | None = None,
    ) -> TurnGate:
        session = _text(session_id, "session_id")
        turn = _text(turn_id, "turn_id")
        gate = _text(gate_id, "gate_id")
        context = _epoch(context_epoch, "context_epoch")
        intent = _epoch(intent_epoch, "intent_epoch")
        generation = _generation(active_generation)
        installed_root, bundle_digest = _optional_installed_plugin_binding(
            plugin_root
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if self.is_internal_thread(session):
                raise RecallGateConflict("internal threads are recall-disabled")
            existing = self._gate_for_turn(session, turn)
            if existing is not None:
                if (
                    existing["plugin_root"] != installed_root
                    or existing["plugin_bundle_digest"] != bundle_digest
                    or _gate(existing)
                    != TurnGate(
                        gate_id=gate,
                        session_id=session,
                        turn_id=turn,
                        context_epoch=context,
                        intent_epoch=intent,
                        active_generation=generation,
                        state=existing["state"],
                        result_digest=existing["result_digest"],
                    )
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
                    active_generation, state, result_digest, plugin_root,
                    plugin_bundle_digest
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                """,
                (
                    gate,
                    session,
                    turn,
                    context,
                    intent,
                    generation,
                    installed_root,
                    bundle_digest,
                ),
            )
            result = _gate(self._gate_by_id(gate))
            self._connection.commit()
            return result
        except Exception:
            self._connection.rollback()
            raise

    def bound_recall_skill_path(
        self, binding_kind: str, binding_id: str
    ) -> Path | None:
        """Revalidate the exact plugin root frozen with one Hook binding."""

        binding = _text(binding_id, "binding_id")
        if binding_kind == "activation":
            table = "recall_activation_bindings"
            id_column = "binding_id"
        elif binding_kind == "attempt":
            table = "recall_activation_attempts"
            id_column = "attempt_id"
        elif binding_kind == "turn":
            table = "recall_turn_gates"
            id_column = "gate_id"
        else:
            raise ValueError("binding_kind is invalid")
        row = self._connection.execute(
            f"SELECT plugin_root, plugin_bundle_digest FROM {table} "
            f"WHERE {id_column} = ?",
            (binding,),
        ).fetchone()
        if row is None or row["plugin_root"] is None:
            return None
        bundle = _installed_recall_bundle(row["plugin_root"])
        if bundle is None or bundle[1] != row["plugin_bundle_digest"]:
            return None
        return bundle[0]

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
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            gate = self._gate_for_turn(session, turn)
            if gate is None or gate["gate_id"] != gate_id:
                raise RecallGateConflict("turn gate does not match trusted binding")
            if self._is_internal_thread(session):
                raise RecallGateConflict("internal threads are recall-disabled")
            try:
                active_set = _optional_text(active_set_digest, "active_set_digest")
                _validate_result(result, gate)
                fingerprint = _commit_fingerprint(result, active_set)
            except (TypeError, ValueError, RecallGateConflict) as error:
                if gate["state"] == "pending":
                    self._block_gate(gate)
                    self._connection.commit()
                else:
                    self._connection.rollback()
                raise RecallGateConflict("turn gate result is invalid") from error
            if gate["state"] in ("committed", "blocked"):
                if gate["commit_fingerprint"] != fingerprint:
                    raise RecallGateConflict("turn gate result is already frozen")
                terminal = _gate(gate)
                self._connection.commit()
                return terminal
            if gate["state"] != "pending":
                raise RecallGateConflict("turn gate is invalid")
            try:
                _validate_session_epoch(gate, self._required_session(session))
            except RecallGateConflict as error:
                self._block_gate(gate)
                self._connection.commit()
                raise RecallGateConflict("turn gate result is invalid") from error
            if result.disposition == "blocked":
                self._connection.execute(
                    """
                    UPDATE recall_turn_gates
                    SET state = 'blocked', result_digest = ?, commit_fingerprint = ?
                    WHERE gate_id = ?
                    """,
                    (_result_digest(result), fingerprint, gate["gate_id"]),
                )
                blocked = _gate(self._gate_by_id(gate["gate_id"]))
                self._connection.commit()
                return blocked
            digest = _result_digest(result)
            self._connection.execute(
                """
                UPDATE recall_turn_gates
                SET state = 'committed', result_digest = ?, commit_fingerprint = ?,
                    active_set_digest = ?, reference_state_version = 1
                WHERE gate_id = ?
                """,
                (digest, fingerprint, active_set, gate["gate_id"]),
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
            committed = _gate(self._gate_by_id(gate["gate_id"]))
            self._connection.commit()
            return committed
        except Exception:
            self._connection.rollback()
            raise

    def require_committed_gate(self, session_id: str, turn_id: str) -> TurnGate:
        session = _text(session_id, "session_id")
        turn = _text(turn_id, "turn_id")
        if self._is_internal_thread(session):
            raise RecallGateConflict("internal threads are recall-disabled")
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
        pending_turn_id: str | None = None,
        pending_gate_id: str | None = None,
        rebased_gate_id: str | None = None,
    ) -> ContextRestoration:
        session = _text(session_id, "session_id")
        if source not in ("compact", "clear"):
            raise ValueError("source is invalid")
        latest_turn = _text(latest_observed_turn_id, "latest_observed_turn_id")
        active_set = _optional_text(active_set_digest, "active_set_digest")
        key = _text(compaction_key, "compaction_key")
        active_set_key = active_set or ""
        rebase_values = (pending_turn_id, pending_gate_id, rebased_gate_id)
        if any(value is not None for value in rebase_values):
            if source != "compact" or not all(
                value is not None for value in rebase_values
            ):
                raise ValueError("pending gate rebase is invalid")
            pending_turn = _text(pending_turn_id, "pending_turn_id")
            pending_gate = _text(pending_gate_id, "pending_gate_id")
            rebased_gate = _text(rebased_gate_id, "rebased_gate_id")
        else:
            pending_turn = None
            pending_gate = None
            rebased_gate = None
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if self._is_internal_thread(session):
                raise RecallGateConflict("internal threads are recall-disabled")
            current = self._session_row(session)
            if current is None or current["state"] != "active":
                raise RecallGateConflict("session is not active for restoration")
            replay_row = self._connection.execute(
                """
                SELECT * FROM recall_context_restorations
                WHERE compaction_key = ?
                """,
                (key,),
            ).fetchone()
            if replay_row is not None:
                if (
                    replay_row["session_id"] != session
                    or replay_row["source"] != source
                ):
                    raise RecallGateConflict(
                        "compaction key already belongs to another event"
                    )
                replay = _restoration(replay_row)
                self._connection.commit()
                return replay
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
            if pending_turn is not None:
                gate = self._gate_for_turn(session, pending_turn)
                if (
                    gate is None
                    or gate["gate_id"] != pending_gate
                    or gate["state"] != "pending"
                    or gate["context_epoch"] != current["context_epoch"]
                    or gate["intent_epoch"] != current["intent_epoch"]
                    or self._gate_by_id(rebased_gate) is not None
                ):
                    raise RecallGateConflict("pending gate rebase is invalid")
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
            if pending_turn is not None:
                updated = self._connection.execute(
                    """
                    UPDATE recall_turn_gates
                    SET gate_id = ?, context_epoch = ?
                    WHERE session_id = ? AND turn_id = ?
                      AND gate_id = ? AND state = 'pending'
                    """,
                    (
                        rebased_gate,
                        epoch,
                        session,
                        pending_turn,
                        pending_gate,
                    ),
                )
                if updated.rowcount != 1:
                    raise RecallGateConflict("pending gate rebase is invalid")
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
            if self._is_internal_thread(session):
                raise RecallGateConflict("internal threads are recall-disabled")
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
            if self._is_internal_thread(session):
                raise RecallGateConflict("internal threads are recall-disabled")
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
            self._connection.execute(
                """
                UPDATE recall_turn_gates
                SET state = 'blocked'
                WHERE session_id = ? AND state = 'pending'
                """,
                (thread,),
            )
            self._connection.execute(
                "UPDATE recall_sessions SET state = 'blocked' WHERE session_id = ?",
                (thread,),
            )
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
        return self._is_internal_thread(thread)

    def _is_internal_thread(self, thread_id: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM recall_internal_threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            is not None
        )

    def _block_gate(self, gate: sqlite3.Row) -> None:
        self._connection.execute(
            "UPDATE recall_turn_gates SET state = 'blocked' WHERE gate_id = ?",
            (gate["gate_id"],),
        )

    def _session_row(self, session_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM recall_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()

    def _required_activation_attempt(self, attempt_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM recall_activation_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise RecallGateConflict("activation attempt does not exist")
        return row

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


def _activation_attempt(row: sqlite3.Row) -> RecallActivationAttempt:
    return RecallActivationAttempt(
        attempt_id=row["attempt_id"],
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        cwd=row["cwd"],
        repository_id=row["repository_id"],
        repository_display_name=row["repository_display_name"],
        state=row["state"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        plugin_root=row["plugin_root"],
        plugin_bundle_digest=row["plugin_bundle_digest"],
        ui_digest=row["ui_digest"],
        result_digest=row["result_digest"],
    )


def _matching_activation_attempt(
    row: sqlite3.Row,
    *,
    session_id: str,
    turn_id: str,
    cwd: str,
    repository_id: str,
    repository_display_name: str,
    plugin_root: str | None,
    plugin_bundle_digest: str | None,
) -> bool:
    return (
        row["session_id"] == session_id
        and row["turn_id"] == turn_id
        and row["cwd"] == cwd
        and row["repository_id"] == repository_id
        and row["repository_display_name"] == repository_display_name
        and row["plugin_root"] == plugin_root
        and row["plugin_bundle_digest"] == plugin_bundle_digest
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
        active_set_digest=row["active_set_digest"],
        reference_state_version=row["reference_state_version"],
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


def _validate_result(result: TurnGateResult, gate: sqlite3.Row) -> None:
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


def _validate_session_epoch(gate: sqlite3.Row, session: RecallSession) -> None:
    if session.context_epoch != gate["context_epoch"]:
        raise RecallGateConflict("session context epoch is stale")
    if session.intent_epoch != gate["intent_epoch"]:
        raise RecallGateConflict("session intent epoch is stale")


def _result_digest(result: TurnGateResult) -> str:
    return hashlib.sha256(canonical_json_bytes(asdict(result))).hexdigest()


def _commit_fingerprint(
    result: TurnGateResult, active_set_digest: str | None
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "result": asdict(result),
                "active_set_digest": active_set_digest,
            }
        )
    ).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} is invalid")
    return value


def _repository_id(value: object) -> str:
    repository = _text(value, "repository_id")
    if len(repository) != 37 or not repository.startswith("repo_"):
        raise ValueError("repository_id is invalid")
    if any(character not in "0123456789abcdef" for character in repository[5:]):
        raise ValueError("repository_id is invalid")
    return repository


def _display_name(value: object) -> str:
    display_name = _text(value, "repository_display_name")
    if len(display_name) > 255 or "/" in display_name or "\\" in display_name:
        raise ValueError("repository_display_name is invalid")
    return display_name


def _digest(value: object, name: str) -> str:
    digest = _text(value, name)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{name} is invalid")
    return digest


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _absolute_path(value: object) -> str:
    path = _text(value, "cwd")
    if not os.path.isabs(path):
        raise ValueError("cwd must be absolute")
    return os.path.normpath(path)


def installed_recall_skill_path(plugin_root: object) -> Path | None:
    """Validate one Hook-provided installed plugin root and exact Recall Skill."""

    bundle = _installed_recall_bundle(plugin_root)
    return None if bundle is None else bundle[0]


def _installed_recall_bundle(
    plugin_root: object,
) -> tuple[Path, str] | None:
    if (
        not isinstance(plugin_root, str)
        or not plugin_root
        or len(plugin_root) > 4096
        or "\x00" in plugin_root
    ):
        return None
    try:
        supplied_root = Path(plugin_root)
        if not supplied_root.is_absolute():
            return None
        root = supplied_root.resolve(strict=True)
        manifest_path = (root / ".codex-plugin/plugin.json").resolve(
            strict=True
        )
        mcp_path = (root / ".mcp.json").resolve(strict=True)
        skill_path = (root / "skills/decision-recall/SKILL.md").resolve(
            strict=True
        )
        if (
            root not in manifest_path.parents
            or root not in mcp_path.parents
            or root not in skill_path.parents
            or not manifest_path.is_file()
            or not mcp_path.is_file()
            or not skill_path.is_file()
            or manifest_path.stat().st_size > 65_536
            or mcp_path.stat().st_size > 65_536
            or skill_path.stat().st_size > 262_144
        ):
            return None
        manifest_bytes = manifest_path.read_bytes()
        mcp_bytes = mcp_path.read_bytes()
        skill_bytes = skill_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        mcp_config = json.loads(mcp_bytes)
        if (
            not isinstance(manifest, dict)
            or manifest.get("name") != "zdecision"
            or manifest.get("skills") != "./skills/"
            or manifest.get("mcpServers") != "./.mcp.json"
            or not isinstance(mcp_config, dict)
        ):
            return None
        servers = mcp_config.get("mcpServers")
        if not isinstance(servers, dict):
            return None
        server = servers.get("zdecision-local")
        if (
            not isinstance(server, dict)
            or server.get("command") != "zdecision-agent"
            or server.get("args") != ["mcp"]
        ):
            return None
        bundle_digest = hashlib.sha256(
            manifest_bytes + b"\0" + mcp_bytes + b"\0" + skill_bytes
        ).hexdigest()
        return skill_path, bundle_digest
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError):
        return None


def _optional_installed_plugin_binding(
    value: object,
) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    bundle = _installed_recall_bundle(value)
    if bundle is None:
        raise ValueError("plugin_root is not a verified ZDecision plugin")
    return str(bundle[0].parents[2]), bundle[1]


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


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RecallGateConflict("stored timestamp is invalid") from error
    return _aware_utc(parsed, "stored timestamp")
