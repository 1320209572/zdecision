"""Idempotent enable-and-prepare orchestration for Recall handoffs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from zdecision.agent.recall_host_state import (
    RecallActivationAttempt,
    RecallDelivery,
    RecallHostStore,
)
from zdecision.recall.handoff import (
    RECALL_HANDOFF_PROTOCOL,
    RecallPreflightReady,
    build_handoff_context,
)
from zdecision.recall.provider import RecallProvider


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


def _claim_expired(delivery: RecallDelivery, now: datetime) -> bool:
    if delivery.claim_expires_at is None:
        return False
    return now >= datetime.fromisoformat(delivery.claim_expires_at)
