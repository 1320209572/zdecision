"""Explicit model tools behind trusted Recall host bindings."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from zdecision.agent.recall_handoff import RecallHandoffService
from zdecision.agent.recall_host_state import (
    RecallGateConflict,
    RecallHostStore,
    RecallSession,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.handoff import (
    RECALL_HANDOFF_PROTOCOL,
    RecallApplicationSubmission,
    RecallPreflightReady,
)
from zdecision.recall.provider import UnavailableRecallProvider
from zdecision.recall.session import RecallIntent


class RecallMcpTools:
    """Recall-only MCP methods composed beside Candidate MCP state."""

    def __init__(
        self,
        *,
        host_store: RecallHostStore,
        handoff_service: RecallHandoffService | None = None,
        cwd: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.host_store = host_store
        self.cwd = os.path.normpath(cwd)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.handoff_service = handoff_service or RecallHandoffService(
            store=host_store,
            provider=UnavailableRecallProvider(),
            clock=self.clock,
            delivery_id_factory=delivery_id_for_attempt,
            claim_token_factory=lambda: f"claim_{uuid4().hex}",
        )

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
        return _blocked("invalid_confirmation")

    def get_recall_handoff(
        self, *, activation_attempt_id: str
    ) -> dict[str, object]:
        """Return app-only authoritative state without claiming delivery bytes."""

        attempt = self._confirmation_attempt(activation_attempt_id)
        if (
            attempt is None
            or attempt.protocol_version != RECALL_HANDOFF_PROTOCOL
            or not isinstance(attempt.preflight, RecallPreflightReady)
        ):
            return _blocked("invalid_confirmation")
        try:
            delivery = self.host_store.delivery_for_attempt(attempt.attempt_id)
        except Exception:
            return _blocked("delivery_unavailable")
        if delivery is None:
            return _confirmation_output(attempt)
        return _merge_confirmation_meta(
            self.handoff_service.status(attempt_id=attempt.attempt_id),
            attempt,
        )

    def ack_recall_delivery(
        self,
        *,
        activation_attempt_id: str,
        delivery_id: str,
        context_digest: str,
    ) -> dict[str, object]:
        """Acknowledge the exact app-private delivery tuple."""

        attempt = self._confirmation_attempt(activation_attempt_id)
        if (
            attempt is None
            or attempt.protocol_version != RECALL_HANDOFF_PROTOCOL
            or not isinstance(attempt.preflight, RecallPreflightReady)
        ):
            return _blocked("invalid_delivery")
        result = self.handoff_service.ack(
            attempt_id=attempt.attempt_id,
            delivery_id=delivery_id,
            context_digest=context_digest,
        )
        return _merge_confirmation_meta(result, attempt)

    def apply_recall_delivery(
        self,
        *,
        turn_gate_id: str,
        delivery_id: str,
        items: object,
    ) -> dict[str, object]:
        """Commit classifications only through one trusted pending gate."""

        binding = self._application_binding(turn_gate_id, delivery_id)
        if binding is None:
            return _blocked("invalid_application")
        session, turn_id = binding
        try:
            submission = RecallApplicationSubmission.from_dict(
                {"delivery_id": delivery_id, "items": items}
            )
            return self.handoff_service.apply(
                session_id=session.session_id,
                turn_id=turn_id,
                gate_id=turn_gate_id,
                delivery_id=delivery_id,
                submission=submission,
            )
        except Exception:
            return _blocked("invalid_application")

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

    def gate_zdecision_turn(
        self,
        *,
        turn_gate_id: str,
        intent: object,
        explicit_refresh: bool = False,
    ) -> dict[str, object]:
        parsed = _parse_intent(intent)
        if parsed is None or not isinstance(explicit_refresh, bool):
            return _blocked("invalid_intent")
        binding = self._turn_binding(turn_gate_id, intent=parsed)
        if binding is None:
            return _blocked("invalid_binding")
        session, turn_id = binding
        try:
            return self.handoff_service.gate_turn(
                session_id=session.session_id,
                turn_id=turn_id,
                gate_id=turn_gate_id,
                intent=parsed,
                explicit_refresh=explicit_refresh,
            )
        except Exception:
            return _blocked("host_gate_unavailable")

    def _turn_binding(
        self, gate_id: object, *, intent: RecallIntent | None = None
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
            recovering_intent_delivery = (
                session is not None
                and session.state == "activating"
                and row["state"] == "pending"
                and self.host_store.intent_delivery_for_gate(
                    session.session_id, row["turn_id"], gate_id
                )
                is not None
            )
            if (
                session is None
                or session.cwd != self.cwd
                or (session.state != "active" and not recovering_intent_delivery)
            ):
                return None
            terminal_reuse = (
                row["state"] == "committed"
                and intent is not None
                and self.host_store.replayable_reuse_gate(
                    session_id=session.session_id,
                    turn_id=row["turn_id"],
                    gate_id=gate_id,
                    intent=intent,
                )
            )
            if (
                row["state"] != "pending"
                and not terminal_reuse
            ):
                return None
            return session, row["turn_id"]
        except (OSError, sqlite3.Error, ValueError):
            return None

    def _application_binding(
        self, gate_id: object, delivery_id: object
    ) -> tuple[RecallSession, str] | None:
        if not _valid_binding_id(gate_id) or not _valid_binding_id(delivery_id):
            return None
        try:
            delivery = self.host_store.get_delivery(delivery_id)
            if delivery is None:
                return None
            eligible = self.host_store.eligible_delivery_for_session(
                delivery.session_id
            )
            if eligible is None or eligible.delivery_id != delivery.delivery_id:
                return None
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT rowid, session_id, turn_id, active_generation, state
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
            session = self.host_store.get_session(delivery.session_id)
            if (
                row is None
                or newer is not None
                or row["session_id"] != delivery.session_id
                or row["state"] != "pending"
                or row["active_generation"] != delivery.preflight.generation
                or session is None
                or session.state != "activating"
                or session.cwd != self.cwd
            ):
                return None
            return session, row["turn_id"]
        except (OSError, sqlite3.Error, RecallGateConflict, ValueError):
            return None

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


def _blocked(code: str) -> dict[str, object]:
    return {"state": "blocked", "code": code}


def delivery_id_for_attempt(attempt_id: str) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "domain": "zdecision-recall-delivery-v1",
                "attempt_id": attempt_id,
            }
        )
    ).hexdigest()
    return f"delivery_{digest[:32]}"
