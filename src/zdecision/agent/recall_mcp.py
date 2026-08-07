"""Explicit model tools behind trusted Recall host bindings."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from zdecision.agent.recall_host_state import (
    RecallGateConflict,
    RecallHostStore,
    RecallSession,
)
from zdecision.jsonio import atomic_write_json, canonical_json_bytes
from zdecision.recall.session import HostProbeEnvelope, RecallIntent, TurnGateResult


_FIXTURE_MARKER = "host_gate_fixture_not_formal"
_FIXTURE_INSTRUCTION = "Use only this bounded host-gate fixture."


class RecallGateProvider(Protocol):
    def activate(self, intent: RecallIntent) -> TurnGateResult: ...

    def gate(
        self, previous: RecallSession, intent: RecallIntent
    ) -> TurnGateResult: ...


class ReadinessRecallGateProvider:
    """Gate 1 production provider: readiness only, never retrieval evidence."""

    def activate(self, intent: RecallIntent) -> TurnGateResult:
        return TurnGateResult("blocked", intent.digest, 0, 0, None)

    def gate(
        self, previous: RecallSession, intent: RecallIntent
    ) -> TurnGateResult:
        return TurnGateResult(
            "blocked",
            intent.digest,
            previous.context_epoch,
            previous.intent_epoch,
            None,
        )


class LiveHostProbeProvider:
    """One-shot, explicitly prepared acceptance fixture provider."""

    def __init__(self, database_path: Path, cwd: str) -> None:
        self.path = host_probe_path(database_path, cwd)

    def activate(self, intent: RecallIntent) -> TurnGateResult:
        probe = self._take_probe()
        return TurnGateResult(
            "retrieve" if probe is not None else "blocked",
            intent.digest,
            0,
            1 if probe is not None else 0,
            probe,
        )

    def gate(
        self, previous: RecallSession, intent: RecallIntent
    ) -> TurnGateResult:
        probe = self._take_probe()
        return TurnGateResult(
            "retrieve" if probe is not None else "blocked",
            intent.digest,
            previous.context_epoch,
            previous.intent_epoch + (1 if probe is not None else 0),
            probe,
        )

    def _take_probe(self) -> HostProbeEnvelope | None:
        try:
            value = json.loads(self.path.read_text("utf-8"))
            if not isinstance(value, dict) or frozenset(value) != frozenset(
                ("probe_id", "marker", "instruction")
            ):
                return None
            probe_id = value["probe_id"]
            instruction = value["instruction"]
            if (
                not isinstance(probe_id, str)
                or not 1 <= len(probe_id) <= 128
                or not isinstance(instruction, str)
                or not 1 <= len(instruction) <= 512
            ):
                return None
            probe = HostProbeEnvelope(
                probe_id=probe_id,
                marker=value["marker"],
                instruction=instruction,
            )
            self.path.unlink()
            return probe
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None


class RecallMcpTools:
    """Recall-only MCP methods composed beside Candidate MCP state."""

    def __init__(
        self,
        *,
        host_store: RecallHostStore,
        provider: RecallGateProvider,
        cwd: str,
        live_acceptance: bool = False,
    ) -> None:
        self.host_store = host_store
        self.provider = provider
        self.cwd = os.path.normpath(cwd)
        self.live_acceptance = live_acceptance
        self._ensure_receipt_schema()

    def activate_zdecision_recall(
        self, *, activation_binding_id: str, intent: object
    ) -> dict[str, object]:
        parsed = _parse_intent(intent)
        if parsed is None:
            return _blocked("invalid_intent")
        binding = self._activation_binding(activation_binding_id)
        if binding is None:
            return _blocked("invalid_binding")
        session, turn_id = binding
        receipt = self._receipt("activation", activation_binding_id)
        if receipt is not None:
            if receipt["intent_digest"] != parsed.digest:
                return _blocked("binding_replayed")
            if self._has_later_gate(session.session_id, receipt["gate_id"]):
                return _blocked("invalid_binding")
            return self._reconcile_receipt(receipt, session, turn_id)
        if not self.live_acceptance:
            return _blocked("native_selection_unproven")
        try:
            result = self.provider.activate(parsed)
        except Exception:
            return _blocked("host_gate_unavailable")
        if not _valid_result(result, parsed, session, activation=True):
            return _blocked("host_gate_unavailable")
        if result.disposition == "blocked":
            return _blocked("host_gate_only")
        if result.disposition == "clarify_product":
            return _clarify()
        if result.probe is None:
            return _blocked("host_gate_unavailable")
        gate_id = _activation_gate_id(activation_binding_id)
        response = _active(result.probe)
        active_set_digest = _probe_digest(result.probe)
        try:
            self.host_store.begin_turn_gate(
                session_id=session.session_id,
                turn_id=turn_id,
                context_epoch=session.context_epoch,
                intent_epoch=session.intent_epoch,
                active_generation=None,
                gate_id=gate_id,
            )
            self._prepare_receipt(
                "activation",
                activation_binding_id,
                parsed.digest,
                gate_id,
                result,
                active_set_digest,
                response,
            )
            self.host_store.commit_turn_gate(
                session_id=session.session_id,
                turn_id=turn_id,
                gate_id=gate_id,
                result=result,
                active_set_digest=active_set_digest,
            )
            self._apply_receipt("activation", activation_binding_id)
            return response
        except (OSError, sqlite3.Error, RecallGateConflict, ValueError):
            return _blocked("host_gate_unavailable")

    def gate_zdecision_turn(
        self, *, turn_gate_id: str, intent: object
    ) -> dict[str, object]:
        parsed = _parse_intent(intent)
        if parsed is None:
            return _blocked("invalid_intent")
        binding = self._turn_binding(turn_gate_id)
        if binding is None:
            return _blocked("invalid_binding")
        session, turn_id = binding
        receipt = self._receipt("turn", turn_gate_id)
        if receipt is not None:
            if receipt["intent_digest"] != parsed.digest:
                return _blocked("binding_replayed")
            return self._reconcile_receipt(receipt, session, turn_id)
        try:
            result = self.provider.gate(session, parsed)
        except Exception:
            return _blocked("host_gate_unavailable")
        if not _valid_result(result, parsed, session, activation=False):
            return _blocked("host_gate_unavailable")
        if result.disposition == "clarify_product":
            return _clarify()
        if result.disposition == "blocked":
            response = _blocked("host_gate_only")
            active_set_digest = session.active_set_digest
        elif result.probe is not None:
            response = _active(result.probe)
            active_set_digest = _probe_digest(result.probe)
        else:
            return _blocked("host_gate_unavailable")
        try:
            self._prepare_receipt(
                "turn",
                turn_gate_id,
                parsed.digest,
                turn_gate_id,
                result,
                active_set_digest,
                response,
            )
            self.host_store.commit_turn_gate(
                session_id=session.session_id,
                turn_id=turn_id,
                gate_id=turn_gate_id,
                result=result,
                active_set_digest=active_set_digest,
            )
            self._apply_receipt("turn", turn_gate_id)
            return response
        except (OSError, sqlite3.Error, RecallGateConflict, ValueError):
            return _blocked("host_gate_unavailable")

    def _activation_binding(
        self, binding_id: object
    ) -> tuple[RecallSession, str] | None:
        if not _valid_binding_id(binding_id):
            return None
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT session_id, turn_id, cwd
                    FROM recall_activation_bindings WHERE binding_id = ?
                    """,
                    (binding_id,),
                ).fetchone()
            if row is None or os.path.normpath(row["cwd"]) != self.cwd:
                return None
            session = self.host_store.get_session(row["session_id"])
            if (
                session is None
                or session.state != "active"
                or session.authorization_turn_id != row["turn_id"]
                or session.cwd != self.cwd
            ):
                return None
            return session, row["turn_id"]
        except (OSError, sqlite3.Error, ValueError):
            return None

    def _turn_binding(
        self, gate_id: object
    ) -> tuple[RecallSession, str] | None:
        if not _valid_binding_id(gate_id):
            return None
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT rowid, session_id, turn_id, state
                    FROM recall_turn_gates WHERE gate_id = ?
                    """,
                    (gate_id,),
                ).fetchone()
                newer = None
                if row is not None:
                    newer = connection.execute(
                        """
                        SELECT 1 FROM recall_turn_gates
                        WHERE session_id = ? AND rowid > ? LIMIT 1
                        """,
                        (row["session_id"], row["rowid"]),
                    ).fetchone()
            if row is None or newer is not None:
                return None
            session = self.host_store.get_session(row["session_id"])
            if session is None or session.state != "active" or session.cwd != self.cwd:
                return None
            if row["state"] != "pending" and self._receipt("turn", gate_id) is None:
                return None
            return session, row["turn_id"]
        except (OSError, sqlite3.Error, ValueError):
            return None

    def _has_later_gate(self, session_id: str, gate_id: str) -> bool:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT rowid FROM recall_turn_gates WHERE gate_id = ?",
                    (gate_id,),
                ).fetchone()
                if row is None:
                    return False
                return connection.execute(
                    """
                    SELECT 1 FROM recall_turn_gates
                    WHERE session_id = ? AND rowid > ? LIMIT 1
                    """,
                    (session_id, row["rowid"]),
                ).fetchone() is not None
        except sqlite3.Error:
            return True

    def _reconcile_receipt(
        self, receipt: sqlite3.Row, session: RecallSession, turn_id: str
    ) -> dict[str, object]:
        try:
            result = _result_from_json(receipt["result_json"])
            response = json.loads(receipt["response_json"])
            if response != _response_for_result(result):
                raise ValueError("response is invalid")
            if receipt["state"] == "prepared":
                self.host_store.commit_turn_gate(
                    session_id=session.session_id,
                    turn_id=turn_id,
                    gate_id=receipt["gate_id"],
                    result=result,
                    active_set_digest=receipt["active_set_digest"],
                )
                self._apply_receipt(receipt["binding_kind"], receipt["binding_id"])
            return response
        except (json.JSONDecodeError, OSError, sqlite3.Error, RecallGateConflict, ValueError):
            return _blocked("host_gate_unavailable")

    def _ensure_receipt_schema(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recall_mcp_receipts (
                    binding_kind TEXT NOT NULL CHECK(binding_kind IN ('activation','turn')),
                    binding_id TEXT NOT NULL,
                    intent_digest TEXT NOT NULL,
                    gate_id TEXT NOT NULL,
                    result_json BLOB NOT NULL,
                    active_set_digest TEXT,
                    response_json BLOB NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('prepared','applied')),
                    PRIMARY KEY(binding_kind, binding_id)
                )
                """
            )

    def _receipt(self, kind: str, binding_id: str) -> sqlite3.Row | None:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT * FROM recall_mcp_receipts
                WHERE binding_kind = ? AND binding_id = ?
                """,
                (kind, binding_id),
            ).fetchone()

    def _prepare_receipt(
        self,
        kind: str,
        binding_id: str,
        intent_digest: str,
        gate_id: str,
        result: TurnGateResult,
        active_set_digest: str | None,
        response: dict[str, object],
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO recall_mcp_receipts(
                    binding_kind, binding_id, intent_digest, gate_id,
                    result_json, active_set_digest, response_json, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared')
                """,
                (
                    kind,
                    binding_id,
                    intent_digest,
                    gate_id,
                    canonical_json_bytes(asdict(result)),
                    active_set_digest,
                    canonical_json_bytes(response),
                ),
            )

    def _apply_receipt(self, kind: str, binding_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE recall_mcp_receipts SET state = 'applied'
                WHERE binding_kind = ? AND binding_id = ?
                """,
                (kind, binding_id),
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.host_store.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def prepare_host_probe(database_path: Path, cwd: str, probe_id: str) -> None:
    probe = HostProbeEnvelope(
        probe_id=probe_id,
        marker=_FIXTURE_MARKER,
        instruction=_FIXTURE_INSTRUCTION,
    )
    atomic_write_json(host_probe_path(database_path, cwd), asdict(probe))


def clear_host_probes(database_path: Path) -> bool:
    directory = _host_probe_directory(database_path)
    existed = False
    if directory.exists():
        for path in directory.glob("*.json"):
            existed = True
            path.unlink(missing_ok=True)
    return existed


def host_probe_path(database_path: Path, cwd: str) -> Path:
    normalized = os.path.normpath(cwd)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return _host_probe_directory(database_path) / f"{digest}.json"


def _host_probe_directory(database_path: Path) -> Path:
    return Path(database_path).parent / "recall-host-probes"


def _parse_intent(value: object) -> RecallIntent | None:
    try:
        return RecallIntent.from_dict(value)
    except (TypeError, ValueError):
        return None


def _valid_binding_id(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and "\x00" not in value
    )


def _valid_result(
    result: object,
    intent: RecallIntent,
    session: RecallSession,
    *,
    activation: bool,
) -> bool:
    if not isinstance(result, TurnGateResult) or result.intent_digest != intent.digest:
        return False
    if result.context_epoch != session.context_epoch:
        return False
    if not session.intent_epoch <= result.intent_epoch <= session.intent_epoch + 1:
        return False
    if result.probe is not None and not _valid_probe(result.probe):
        return False
    if activation and result.disposition not in (
        "retrieve",
        "clarify_product",
        "blocked",
    ):
        return False
    return result.disposition in (
        "reuse",
        "retrieve",
        "clarify_product",
        "refresh_required",
        "blocked",
    )


def _valid_probe(probe: object) -> bool:
    return bool(
        isinstance(probe, HostProbeEnvelope)
        and isinstance(probe.probe_id, str)
        and 1 <= len(probe.probe_id) <= 128
        and probe.marker == _FIXTURE_MARKER
        and isinstance(probe.instruction, str)
        and 1 <= len(probe.instruction) <= 512
    )


def _response_for_result(result: TurnGateResult) -> dict[str, object]:
    if result.disposition == "blocked" and result.probe is None:
        return _blocked("host_gate_only")
    if result.disposition != "blocked" and _valid_probe(result.probe):
        return _active(result.probe)
    raise ValueError("result response is invalid")


def _active(probe: HostProbeEnvelope) -> dict[str, object]:
    return {
        "state": "active",
        "receipt": "host_probe_applied",
        "probe": asdict(probe),
    }


def _blocked(code: str) -> dict[str, object]:
    return {"state": "blocked", "code": code}


def _clarify() -> dict[str, object]:
    return {
        "state": "clarify_product",
        "question": "Which Decision space should be used?",
    }


def _probe_digest(probe: HostProbeEnvelope) -> str:
    return hashlib.sha256(canonical_json_bytes(asdict(probe))).hexdigest()


def _activation_gate_id(binding_id: str) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {"domain": "zdecision-recall-activation-gate-v1", "binding": binding_id}
        )
    ).hexdigest()
    return f"activation_gate_{digest[:32]}"


def _result_from_json(value: bytes) -> TurnGateResult:
    item = json.loads(value)
    if not isinstance(item, dict) or frozenset(item) != frozenset(
        ("disposition", "intent_digest", "context_epoch", "intent_epoch", "probe")
    ):
        raise ValueError("result is invalid")
    probe_value = item["probe"]
    probe = None
    if probe_value is not None:
        if not isinstance(probe_value, dict) or frozenset(probe_value) != frozenset(
            ("probe_id", "marker", "instruction")
        ):
            raise ValueError("probe is invalid")
        probe = HostProbeEnvelope(**probe_value)
    return TurnGateResult(
        item["disposition"],
        item["intent_digest"],
        item["context_epoch"],
        item["intent_epoch"],
        probe,
    )
