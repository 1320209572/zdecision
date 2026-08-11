"""Idempotent enable-and-prepare orchestration for Recall handoffs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from zdecision.agent.recall_host_state import (
    RecallActivationAttempt,
    RecallDelivery,
    RecallHostStore,
)
from zdecision.recall.handoff import (
    RECALL_HANDOFF_PROTOCOL,
    RecallApplicationSubmission,
    RecallPreflightClarification,
    RecallPreflightReady,
    RecallPreflightUnavailable,
    build_handoff_context,
)
from zdecision.recall.provider import RecallProvider
from zdecision.recall.session import RecallIntent, TurnGateResult


_CLAIM_LEASE = timedelta(seconds=30)


class RecallHandoffService:
    """Coordinate one frozen Recall delivery after explicit consent."""

    def __init__(
        self,
        *,
        store: RecallHostStore,
        provider: RecallProvider,
        clock: Callable[[], datetime],
        delivery_id_factory: Callable[[str], str],
        claim_token_factory: Callable[[], str],
    ) -> None:
        self.store = store
        self.provider = provider
        self.clock = clock
        self.delivery_id_factory = delivery_id_factory
        self.claim_token_factory = claim_token_factory

    def enable(
        self,
        *,
        attempt_id: str,
        current_ui_digest: str,
    ) -> dict[str, object]:
        now = self.clock()
        if not self._attempt_is_current(
            attempt_id=attempt_id,
            current_ui_digest=current_ui_digest,
        ):
            return {"state": "blocked", "code": "invalid_confirmation"}
        claim_token = self.claim_token_factory()
        try:
            claim = self.store.begin_delivery(
                attempt_id=attempt_id,
                delivery_id=self.delivery_id_factory(attempt_id),
                claim_token=claim_token,
                current_ui_digest=current_ui_digest,
                now=now,
                claim_expires_at=now + _CLAIM_LEASE,
            )
        except Exception:
            return {"state": "blocked", "code": "invalid_confirmation"}
        if not claim.owned:
            delivery = claim.delivery
            if delivery.state == "delivery_claimed" and _claim_expired(
                delivery, now
            ):
                try:
                    delivery = self.store.mark_delivery_unknown(
                        delivery_id=delivery.delivery_id,
                        now=now,
                    )
                except Exception:
                    return self.status(attempt_id=attempt_id)
            if delivery.state == "delivery_unknown":
                try:
                    retry = self.store.claim_delivery_retry(
                        delivery_id=delivery.delivery_id,
                        claim_token=claim_token,
                        now=now,
                        claim_expires_at=now + _CLAIM_LEASE,
                    )
                except Exception:
                    return self.status(attempt_id=attempt_id)
                if retry.owned:
                    return _delivery_output(retry.delivery, include_context=True)
            return self.status(attempt_id=attempt_id)
        try:
            shortlist = self.provider.retrieve(claim.delivery.preflight)
            if not self._attempt_is_current(
                attempt_id=attempt_id,
                current_ui_digest=current_ui_digest,
                expected_preflight=claim.delivery.preflight,
            ):
                return {"state": "blocked", "code": "delivery_prepare_failed"}
            context_text = build_handoff_context(
                claim.delivery.delivery_id,
                claim.delivery.preflight,
                shortlist,
            )
            prepared = self.store.commit_prepared_delivery(
                delivery_id=claim.delivery.delivery_id,
                claim_token=claim.claim_token,
                shortlist=shortlist,
                context_text=context_text,
                now=self.clock(),
            )
        except Exception:
            return {"state": "blocked", "code": "delivery_prepare_failed"}
        return _delivery_output(prepared, include_context=True)

    def _attempt_is_current(
        self,
        *,
        attempt_id: str,
        current_ui_digest: str,
        expected_preflight: RecallPreflightReady | None = None,
    ) -> bool:
        try:
            attempt = self.store.get_activation_attempt(attempt_id)
            if (
                attempt is None
                or attempt.protocol_version != RECALL_HANDOFF_PROTOCOL
                or attempt.ui_digest != current_ui_digest
                or not isinstance(attempt.preflight, RecallPreflightReady)
                or self.clock()
                >= datetime.fromisoformat(attempt.preflight.expires_at)
                or (
                    expected_preflight is not None
                    and attempt.preflight != expected_preflight
                )
            ):
                return False
            return not (
                attempt.plugin_root is not None
                and self.store.bound_recall_skill_path("attempt", attempt_id) is None
            )
        except Exception:
            return False

    def decline(
        self,
        *,
        attempt_id: str,
        current_ui_digest: str,
    ) -> dict[str, object]:
        try:
            attempt = self.store.get_activation_attempt(attempt_id)
            if attempt is None or attempt.ui_digest != current_ui_digest:
                return {"state": "blocked", "code": "invalid_confirmation"}
            decided = self.store.decide_activation_attempt(
                attempt_id,
                action="decline",
                now=self.clock(),
            )
            return _attempt_output(decided)
        except Exception:
            return {"state": "blocked", "code": "invalid_confirmation"}

    def status(self, *, attempt_id: str) -> dict[str, object]:
        try:
            delivery = self.store.delivery_for_attempt(attempt_id)
        except Exception:
            return {"state": "blocked", "code": "delivery_unavailable"}
        if delivery is None:
            return {"state": "blocked", "code": "delivery_not_found"}
        now = self.clock()
        if delivery.state == "delivery_claimed":
            if _claim_expired(delivery, now):
                return _delivery_output(
                    delivery,
                    include_context=False,
                    state="delivery_unknown",
                    code="acknowledgement_expired",
                )
            return _delivery_output(
                delivery,
                include_context=False,
                code="delivery_in_progress",
            )
        return _delivery_output(delivery, include_context=False)

    def ack(
        self,
        *,
        attempt_id: str,
        delivery_id: str,
        context_digest: str,
    ) -> dict[str, object]:
        """Acknowledge only the exact frozen delivery owned by an attempt."""

        try:
            delivery = self.store.delivery_for_attempt(attempt_id)
            if delivery is None or delivery.delivery_id != delivery_id:
                return {"state": "blocked", "code": "invalid_delivery"}
            acknowledged = self.store.ack_delivery(
                delivery_id=delivery_id,
                context_digest=context_digest,
                now=self.clock(),
            )
        except Exception:
            return {"state": "blocked", "code": "invalid_delivery"}
        return _delivery_output(acknowledged, include_context=False)

    def apply(
        self,
        *,
        session_id: str,
        turn_id: str,
        gate_id: str,
        delivery_id: str,
        submission: RecallApplicationSubmission,
    ) -> dict[str, object]:
        delivery = self.store.commit_delivery_application(
            session_id=session_id,
            turn_id=turn_id,
            gate_id=gate_id,
            delivery_id=delivery_id,
            submission=submission,
            now=self.clock(),
        )
        return bounded_application_output(delivery)

    def gate_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        gate_id: str,
        intent: RecallIntent,
        explicit_refresh: bool = False,
    ) -> dict[str, object]:
        """Reuse one active intent or freeze one changed Intent Epoch."""

        session = self.store.get_session(session_id)
        if session is not None and session.state == "activating":
            try:
                delivery = self.store.intent_delivery_for_gate(
                    session_id, turn_id, gate_id
                )
            except Exception:
                delivery = None
            if delivery is not None and delivery.preflight.intent == intent:
                return _turn_delivery_output(
                    delivery, intent_epoch=session.intent_epoch + 1
                )
        if (
            session is None
            or session.state != "active"
            or not isinstance(intent, RecallIntent)
        ):
            return {"state": "blocked", "code": "intent_change_unavailable"}
        if not isinstance(explicit_refresh, bool):
            return {"state": "blocked", "code": "invalid_intent"}
        if session.active_intent_digest == intent.digest and not explicit_refresh:
            result = TurnGateResult(
                disposition="reuse",
                intent_digest=intent.digest,
                context_epoch=session.context_epoch,
                intent_epoch=session.intent_epoch,
                probe=None,
            )
            self.store.commit_turn_gate(
                session_id=session_id,
                turn_id=turn_id,
                gate_id=gate_id,
                result=result,
                active_set_digest=session.active_set_digest,
            )
            return {
                "state": "reuse",
                "context_epoch": session.context_epoch,
                "intent_epoch": session.intent_epoch,
            }
        try:
            preflight = self.provider.preflight(
                repository_id=session.repository_id,
                repository_display_name=Path(session.cwd).name,
                intent=intent,
                now=self.clock(),
            )
        except Exception:
            return {"state": "unavailable", "code": "recall_not_ready"}
        if isinstance(preflight, RecallPreflightClarification):
            return {
                "state": "clarify_product",
                "candidate_display_names": list(
                    preflight.candidate_display_names
                ),
            }
        if isinstance(preflight, RecallPreflightUnavailable):
            return {"state": "unavailable", "code": preflight.code}
        if (
            not isinstance(preflight, RecallPreflightReady)
            or preflight.intent != intent
            or preflight.repository_id != session.repository_id
            or preflight.target_decision_space_ids
            != intent.target_decision_space_ids
        ):
            return {"state": "unavailable", "code": "invalid_preflight"}
        attempt_id = _intent_attempt_id(gate_id, preflight.digest)
        delivery_id = _intent_delivery_id(attempt_id)
        try:
            claimed_at = self.clock()
            claim = self.store.begin_intent_delivery(
                session_id=session_id,
                turn_id=turn_id,
                gate_id=gate_id,
                attempt_id=attempt_id,
                delivery_id=delivery_id,
                claim_token=self.claim_token_factory(),
                preflight=preflight,
                now=claimed_at,
                claim_expires_at=claimed_at + _CLAIM_LEASE,
                retire_active_set=(
                    session.active_intent is not None
                    and session.active_intent.target_decision_space_ids
                    != preflight.target_decision_space_ids
                ),
            )
            if not claim.owned:
                return _turn_delivery_output(
                    claim.delivery, intent_epoch=session.intent_epoch + 1
                )
            shortlist = self.provider.retrieve(preflight)
            context_text = build_handoff_context(
                delivery_id, preflight, shortlist
            )
            delivered = self.store.commit_intent_delivery(
                delivery_id=delivery_id,
                claim_token=claim.claim_token,
                shortlist=shortlist,
                context_text=context_text,
                now=self.clock(),
            )
        except Exception:
            return {"state": "blocked", "code": "delivery_prepare_failed"}
        return _turn_delivery_output(
            delivered, intent_epoch=session.intent_epoch + 1
        )


def _delivery_output(
    delivery: RecallDelivery,
    *,
    include_context: bool,
    state: str | None = None,
    code: str | None = None,
) -> dict[str, object]:
    output: dict[str, object] = {"state": state or delivery.state}
    if code is not None:
        output["code"] = code
    if delivery.state == "preparing":
        output["code"] = "delivery_in_progress"
        return output
    if all(
        isinstance(value, str)
        for value in (
            delivery.context_text,
            delivery.snapshot_digest,
            delivery.context_digest,
        )
    ):
        output["_meta"] = {
            "zdecision/delivery_id": delivery.delivery_id,
            "zdecision/snapshot_digest": delivery.snapshot_digest,
            "zdecision/context_digest": delivery.context_digest,
        }
        if include_context:
            output["_meta"]["zdecision/context_text"] = delivery.context_text
    return output


def _attempt_output(attempt: RecallActivationAttempt) -> dict[str, object]:
    meta: dict[str, object] = {
        "zdecision/activation_attempt_id": attempt.attempt_id,
        "zdecision/repository_display_name": attempt.repository_display_name,
    }
    if attempt.preflight is not None:
        meta["zdecision/target_display_names"] = list(
            attempt.preflight.target_display_names
        )
        meta["zdecision/freshness"] = attempt.preflight.freshness
    return {"state": attempt.state, "_meta": meta}


def bounded_application_output(delivery: RecallDelivery) -> dict[str, object]:
    """Return only the safe model-visible result of an atomic application."""

    if (
        delivery.application is None
        or delivery.shortlist is None
        or delivery.application_receipt_id is None
    ):
        raise ValueError("delivery application is incomplete")
    counts = {
        "applicable": 0,
        "not_applicable": 0,
        "conflicting": 0,
        "uncertain": 0,
    }
    for item in delivery.application.items:
        counts[item.disposition] += 1
    return {
        "state": delivery.state,
        "disposition_counts": counts,
        "application_receipt_id": delivery.application_receipt_id,
        "intent_epoch": 1,
        "scope_titles": [
            item.revision.scope_summary for item in delivery.shortlist.items
        ],
    }


def _claim_expired(delivery: RecallDelivery, now: datetime) -> bool:
    if delivery.claim_expires_at is None:
        return False
    return now >= datetime.fromisoformat(delivery.claim_expires_at)


def _intent_attempt_id(gate_id: str, preflight_digest: str) -> str:
    digest = hashlib.sha256(
        f"zdecision-intent-attempt-v1\0{gate_id}\0{preflight_digest}".encode()
    ).hexdigest()
    return f"intent_attempt_{digest[:32]}"


def _intent_delivery_id(attempt_id: str) -> str:
    digest = hashlib.sha256(
        f"zdecision-intent-delivery-v1\0{attempt_id}".encode()
    ).hexdigest()
    return f"delivery_{digest[:32]}"


def _turn_delivery_output(
    delivery: RecallDelivery, *, intent_epoch: int
) -> dict[str, object]:
    if (
        delivery.state != "host_delivered"
        or delivery.shortlist is None
        or delivery.context_text is None
    ):
        return {"state": "blocked", "code": "delivery_in_progress"}
    payload = json.loads(delivery.context_text)
    return {
        "state": "retrieve",
        "delivery_id": delivery.delivery_id,
        "intent_epoch": intent_epoch,
        "decisions": [item.to_dict() for item in delivery.shortlist.items],
        "handoff": payload,
    }
