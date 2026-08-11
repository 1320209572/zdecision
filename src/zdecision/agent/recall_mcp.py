"""Explicit model tools behind trusted Recall host bindings."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from zdecision.agent.recall_handoff import RecallHandoffService
from zdecision.agent.recall_host_state import (
    RecallGateConflict,
    RecallHostStore,
    RecallSession,
)
from zdecision.app_server.models import ActiveTurnEvidence
from zdecision.jsonio import atomic_write_json, canonical_json_bytes
from zdecision.recall.handoff import RECALL_HANDOFF_PROTOCOL, RecallPreflightReady
from zdecision.recall.provider import UnavailableRecallProvider
from zdecision.recall.session import HostProbeEnvelope, RecallIntent, TurnGateResult


_FIXTURE_MARKER = "host_gate_fixture_not_formal"
_FIXTURE_INSTRUCTION = "Use only this bounded host-gate fixture."
_CLAIM_TIMEOUT_SECONDS = 0.05


class _ClaimBusy(Exception):
    pass


class RecallGateProvider(Protocol):
    def activate(self, intent: RecallIntent) -> TurnGateResult: ...

    def gate(
        self, previous: RecallSession, intent: RecallIntent
    ) -> TurnGateResult: ...


class ActiveTurnEvidenceGateway(Protocol):
    def read_active_turn_evidence(
        self, thread_id: str, turn_id: str
    ) -> ActiveTurnEvidence: ...

    def close(self) -> None: ...


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
        probe = self._read_probe()
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
        probe = self._read_probe()
        return TurnGateResult(
            "retrieve" if probe is not None else "blocked",
            intent.digest,
            previous.context_epoch,
            previous.intent_epoch + (1 if probe is not None else 0),
            probe,
        )

    def acknowledge(self, probe: HostProbeEnvelope) -> None:
        if self._read_probe() == probe:
            self.path.unlink(missing_ok=True)

    def _read_probe(self) -> HostProbeEnvelope | None:
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
        handoff_service: RecallHandoffService | None = None,
        cwd: str,
        live_acceptance: bool = False,
        evidence_gateway_factory: Callable[[], ActiveTurnEvidenceGateway]
        | None = None,
        recall_skill_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.host_store = host_store
        self.provider = provider
        self.cwd = os.path.normpath(cwd)
        self.live_acceptance = live_acceptance
        self.evidence_gateway_factory = evidence_gateway_factory
        self.recall_skill_path = (
            None
            if recall_skill_path is None
            else Path(recall_skill_path).resolve(strict=False)
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.handoff_service = handoff_service or RecallHandoffService(
            store=host_store,
            provider=UnavailableRecallProvider(),
            clock=self.clock,
            delivery_id_factory=_delivery_id_for_attempt,
            claim_token_factory=lambda: f"claim_{uuid4().hex}",
        )
        self._ensure_receipt_schema()

    def show_recall_confirmation(
        self, *, activation_attempt_id: str, intent: object, ui_digest: str
    ) -> dict[str, object]:
        """Freeze the card shown for one Hook-owned confirmation attempt."""

        parsed = _parse_intent(intent)
        attempt = self._confirmation_attempt(activation_attempt_id)
        if (
            parsed is None
            or attempt is None
            or attempt.protocol_version != RECALL_HANDOFF_PROTOCOL
            or not isinstance(attempt.preflight, RecallPreflightReady)
            or attempt.preflight.intent.digest != parsed.digest
            or attempt.preflight.intent != parsed
            or not _valid_ui_digest(ui_digest)
        ):
            return _blocked("invalid_confirmation")
        if attempt.state != "pending_confirmation":
            return _confirmation_output(attempt)
        try:
            attached = self.host_store.attach_activation_card(
                attempt.attempt_id, ui_digest=ui_digest
            )
        except (OSError, sqlite3.Error, RecallGateConflict, ValueError):
            return _blocked("invalid_confirmation")
        return _confirmation_output(attached)

    def decide_recall_confirmation(
        self,
        *,
        activation_attempt_id: str,
        action: str,
        current_ui_digest: str,
    ) -> dict[str, object]:
        """Commit a user confirmation without routing, retrieval, or provider work."""

        attempt = self._confirmation_attempt(activation_attempt_id)
        if (
            attempt is None
            or action not in ("enable", "decline")
            or not _valid_ui_digest(current_ui_digest)
            or attempt.ui_digest != current_ui_digest
        ):
            return _blocked("invalid_confirmation")
        if attempt.protocol_version == RECALL_HANDOFF_PROTOCOL:
            if action == "enable":
                result = self.handoff_service.enable(
                    attempt_id=attempt.attempt_id,
                    current_ui_digest=current_ui_digest,
                )
            else:
                result = self.handoff_service.decline(
                    attempt_id=attempt.attempt_id,
                    current_ui_digest=current_ui_digest,
                )
            return _merge_confirmation_meta(result, attempt)
        try:
            decided = self.host_store.decide_activation_attempt(
                attempt.attempt_id, action=action, now=self.clock()
            )
        except (OSError, sqlite3.Error, RecallGateConflict, ValueError):
            return _blocked("invalid_confirmation")
        return _confirmation_output(decided)

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
        if not self._active_turn_barrier_proven(
            session,
            turn_id,
            tool_name="activate_zdecision_recall",
            operation_id=activation_binding_id,
            binding_kind="activation",
            binding_id=activation_binding_id,
            require_native_selection=True,
        ):
            return _blocked("native_selection_unproven")
        return self._claim_provider_result(
            kind="activation",
            binding_id=activation_binding_id,
            gate_id=_activation_gate_id(activation_binding_id),
            parsed=parsed,
            session=session,
            turn_id=turn_id,
            activation=True,
            invoke=lambda: self.provider.activate(parsed),
        )

    def _confirmation_attempt(self, attempt_id: object):
        if not _valid_binding_id(attempt_id):
            return None
        try:
            attempt = self.host_store.get_activation_attempt(attempt_id)
            if attempt is None or os.path.normpath(attempt.cwd) != self.cwd:
                return None
            if (
                attempt.plugin_root is not None
                and self.host_store.bound_recall_skill_path("attempt", attempt.attempt_id)
                is None
            ):
                return None
            return attempt
        except (OSError, sqlite3.Error, RecallGateConflict, ValueError):
            return None

    def _active_turn_barrier_proven(
        self,
        session: RecallSession,
        turn_id: str,
        *,
        tool_name: str,
        operation_id: str,
        binding_kind: str,
        binding_id: str,
        require_native_selection: bool,
    ) -> bool:
        factory = self.evidence_gateway_factory
        recall_skill_path = self.recall_skill_path
        if recall_skill_path is None:
            try:
                recall_skill_path = self.host_store.bound_recall_skill_path(
                    binding_kind, binding_id
                )
            except Exception:
                recall_skill_path = None
        if (
            factory is None
            or recall_skill_path is None
            or not recall_skill_path.is_file()
        ):
            return False
        gateway: ActiveTurnEvidenceGateway | None = None
        evidence: ActiveTurnEvidence | None = None
        connection_closed = False
        try:
            gateway = factory()
            evidence = gateway.read_active_turn_evidence(
                session.session_id, turn_id
            )
        except Exception:
            return False
        finally:
            if gateway is not None:
                try:
                    gateway.close()
                    connection_closed = True
                except Exception:
                    connection_closed = False
        if evidence is None or not connection_closed:
            return False
        if (
            evidence.thread.thread_id != session.session_id
            or evidence.turn_id != turn_id
            or evidence.thread.cwd != session.cwd
            or evidence.thread.cwd != self.cwd
        ):
            return False
        if require_native_selection and not any(
            Path(selected.path).resolve(strict=False) == recall_skill_path
            for selected in evidence.selected_skills
        ):
            return False
        matching_items = [
            (index, item)
            for index, item in enumerate(evidence.ordered_items)
            if item.item_type == "mcpToolCall"
            and item.tool_name == tool_name
            and item.operation_id == operation_id
        ]
        if len(matching_items) != 1:
            return False
        target_index = matching_items[0][0]
        if not any(
            item.item_type == "hookPrompt"
            for item in evidence.ordered_items[:target_index]
        ):
            return False
        substantive_types = frozenset(
            ("agentMessage", "commandExecution", "fileChange")
        )
        return not any(
            item.item_type in substantive_types
            for item in evidence.ordered_items[:target_index]
        )

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
        if not self._active_turn_barrier_proven(
            session,
            turn_id,
            tool_name="gate_zdecision_turn",
            operation_id=turn_gate_id,
            binding_kind="turn",
            binding_id=turn_gate_id,
            require_native_selection=False,
        ):
            return _blocked("host_gate_unavailable")
        return self._claim_provider_result(
            kind="turn",
            binding_id=turn_gate_id,
            gate_id=turn_gate_id,
            parsed=parsed,
            session=session,
            turn_id=turn_id,
            activation=False,
            invoke=lambda: self.provider.gate(session, parsed),
        )

    def _claim_provider_result(
        self,
        *,
        kind: str,
        binding_id: str,
        gate_id: str,
        parsed: RecallIntent,
        session: RecallSession,
        turn_id: str,
        activation: bool,
        invoke: Callable[[], TurnGateResult],
    ) -> dict[str, object]:
        try:
            with self._connection(
                timeout_seconds=_CLAIM_TIMEOUT_SECONDS
            ) as connection:
                receipt = self._receipt_from(connection, kind, binding_id)
                if receipt is None:
                    claim = connection.execute(
                        """
                        SELECT intent_digest FROM recall_mcp_claims
                        WHERE binding_kind = ? AND binding_id = ?
                        """,
                        (kind, binding_id),
                    ).fetchone()
                    if claim is not None:
                        if claim["intent_digest"] != parsed.digest:
                            return _blocked("binding_replayed")
                        raise _ClaimBusy
                    connection.execute(
                        """
                        INSERT INTO recall_mcp_claims(
                            binding_kind, binding_id, intent_digest
                        ) VALUES (?, ?, ?)
                        """,
                        (kind, binding_id, parsed.digest),
                    )
                    result = invoke()
                    if not _valid_result(
                        result, parsed, session, activation=activation
                    ):
                        raise ValueError("provider result is invalid")
                    response = _response_for_result(result)
                    active_set_digest = (
                        _probe_digest(result.probe)
                        if result.probe is not None
                        else session.active_set_digest
                    )
                    if result.probe is not None:
                        owner = connection.execute(
                            """
                            SELECT binding_kind, binding_id
                            FROM recall_host_probe_claims
                            WHERE probe_digest = ?
                            """,
                            (active_set_digest,),
                        ).fetchone()
                        if owner is None:
                            connection.execute(
                                """
                                INSERT INTO recall_host_probe_claims(
                                    probe_digest, binding_kind, binding_id
                                ) VALUES (?, ?, ?)
                                """,
                                (active_set_digest, kind, binding_id),
                            )
                        elif (
                            owner["binding_kind"] != kind
                            or owner["binding_id"] != binding_id
                        ):
                            raise _ClaimBusy
                    requires_gate_commit = result.disposition != "clarify_product" and (
                        kind == "turn" or result.disposition != "blocked"
                    )
                    self._insert_receipt(
                        connection,
                        kind,
                        binding_id,
                        parsed.digest,
                        gate_id,
                        result,
                        active_set_digest,
                        response,
                        state=("prepared" if requires_gate_commit else "applied"),
                    )
                    receipt = self._receipt_from(connection, kind, binding_id)
                elif receipt["intent_digest"] != parsed.digest:
                    return _blocked("binding_replayed")
            if receipt is None:
                return _blocked("host_gate_unavailable")
            return self._reconcile_receipt(receipt, session, turn_id)
        except _ClaimBusy:
            return _blocked("host_gate_busy")
        except sqlite3.OperationalError as error:
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                return _blocked("host_gate_busy")
            return _blocked("host_gate_unavailable")
        except Exception:
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
                if receipt["binding_kind"] == "activation":
                    self.host_store.begin_turn_gate(
                        session_id=session.session_id,
                        turn_id=turn_id,
                        context_epoch=session.context_epoch,
                        intent_epoch=session.intent_epoch,
                        active_generation=None,
                        gate_id=receipt["gate_id"],
                    )
                self.host_store.commit_turn_gate(
                    session_id=session.session_id,
                    turn_id=turn_id,
                    gate_id=receipt["gate_id"],
                    result=result,
                    active_set_digest=receipt["active_set_digest"],
                )
                self._apply_receipt(receipt["binding_kind"], receipt["binding_id"])
        except (json.JSONDecodeError, OSError, sqlite3.Error, RecallGateConflict, ValueError):
            return _blocked("host_gate_unavailable")
        self._acknowledge_probe(result.probe)
        return response

    def _ensure_receipt_schema(self) -> None:
        with self._connection() as connection:
            connection.executescript(
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
                );

                CREATE TABLE IF NOT EXISTS recall_mcp_claims (
                    binding_kind TEXT NOT NULL CHECK(binding_kind IN ('activation','turn')),
                    binding_id TEXT NOT NULL,
                    intent_digest TEXT NOT NULL,
                    PRIMARY KEY(binding_kind, binding_id)
                );

                CREATE TABLE IF NOT EXISTS recall_host_probe_claims (
                    probe_digest TEXT PRIMARY KEY,
                    binding_kind TEXT NOT NULL CHECK(binding_kind IN ('activation','turn')),
                    binding_id TEXT NOT NULL,
                    UNIQUE(binding_kind, binding_id)
                );
                """
            )

    def _receipt(self, kind: str, binding_id: str) -> sqlite3.Row | None:
        with self._connection() as connection:
            return self._receipt_from(connection, kind, binding_id)

    def _receipt_from(
        self, connection: sqlite3.Connection, kind: str, binding_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM recall_mcp_receipts
            WHERE binding_kind = ? AND binding_id = ?
            """,
            (kind, binding_id),
        ).fetchone()

    def _insert_receipt(
        self,
        connection: sqlite3.Connection,
        kind: str,
        binding_id: str,
        intent_digest: str,
        gate_id: str,
        result: TurnGateResult,
        active_set_digest: str | None,
        response: dict[str, object],
        *,
        state: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO recall_mcp_receipts(
                binding_kind, binding_id, intent_digest, gate_id,
                result_json, active_set_digest, response_json, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                binding_id,
                intent_digest,
                gate_id,
                canonical_json_bytes(asdict(result)),
                active_set_digest,
                canonical_json_bytes(response),
                state,
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

    def _acknowledge_probe(self, probe: HostProbeEnvelope | None) -> None:
        if probe is None or not isinstance(self.provider, LiveHostProbeProvider):
            return
        try:
            self.provider.acknowledge(probe)
        except OSError:
            return

    @contextmanager
    def _connection(
        self, *, timeout_seconds: float = 5.0
    ) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.host_store.path, timeout=timeout_seconds
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
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


def _valid_ui_digest(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _confirmation_output(attempt: object) -> dict[str, object]:
    state = getattr(attempt, "state", None)
    attempt_id = getattr(attempt, "attempt_id", None)
    repository_display_name = getattr(attempt, "repository_display_name", None)
    if not all(
        isinstance(value, str)
        for value in (state, attempt_id, repository_display_name)
    ):
        raise ValueError("confirmation attempt is invalid")
    meta: dict[str, object] = {
        "zdecision/activation_attempt_id": attempt_id,
        "zdecision/repository_display_name": repository_display_name,
    }
    preflight = getattr(attempt, "preflight", None)
    if isinstance(preflight, RecallPreflightReady):
        meta["zdecision/target_display_names"] = list(
            preflight.target_display_names
        )
        meta["zdecision/freshness"] = preflight.freshness
    return {
        "state": state,
        "_meta": meta,
    }


def _merge_confirmation_meta(
    result: dict[str, object], attempt: object
) -> dict[str, object]:
    confirmation = _confirmation_output(attempt)
    merged = dict(confirmation["_meta"])
    result_meta = result.get("_meta")
    if isinstance(result_meta, dict):
        merged.update(result_meta)
    return {**result, "_meta": merged}


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
    if result.disposition == "clarify_product" and result.probe is None:
        return _clarify()
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


def _delivery_id_for_attempt(attempt_id: str) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "domain": "zdecision-recall-delivery-v1",
                "attempt_id": attempt_id,
            }
        )
    ).hexdigest()
    return f"delivery_{digest[:32]}"


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
