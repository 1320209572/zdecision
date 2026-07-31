"""Cross-Session Candidate reconciliation through disposable attempts."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from zdecision.agent.request_state import RequestStateStore
from zdecision.app_server.gateway import (
    AppServerGateway,
    AppServerGatewayError,
)
from zdecision.app_server.jsonl import (
    AppServerEOF,
    AppServerError,
    AppServerProtocolError,
    AppServerRequestError,
    AppServerTimeout,
)
from zdecision.app_server.models import (
    AppServerTurnReceipt,
    FeasibilityModelProfile,
)
from zdecision.capture.models import Candidate
from zdecision.capture.reconciliation import (
    CandidateFamilyRevision,
    ReconciliationResult,
    apply_reconciliation,
    reconciliation_output_schema,
    validate_reconciliation,
)
from zdecision.ids import candidate_family_id
from zdecision.jsonio import canonical_json_bytes


_PROMPT_REVISION = "candidate-reconciliation-v1"


class ReconciliationRunnerError(Exception):
    """A reconciliation attempt violates its frozen host boundary."""


class ReconciliationAttemptRetryable(ReconciliationRunnerError):
    """One disposable reconciliation generation must be replaced."""


class ReconciliationRunner:
    """Reconcile typed observations without inheriting a source Session."""

    def __init__(
        self,
        *,
        gateway: AppServerGateway,
        request_state: RequestStateStore,
    ) -> None:
        self.gateway = gateway
        if not isinstance(request_state, RequestStateStore):
            raise TypeError("request_state must be a RequestStateStore")
        self.request_state = request_state

    def run(
        self,
        *,
        request_id: str,
        repository_id: str,
        cwd: str,
        observations: tuple[Candidate, ...],
        current: tuple[CandidateFamilyRevision, ...],
        profile: FeasibilityModelProfile,
        heartbeat: Callable[[], None] | None = None,
    ) -> ReconciliationResult:
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise ValueError("cwd must be an absolute path")
        if (
            not isinstance(observations, tuple)
            or any(
                not isinstance(item, Candidate)
                for item in observations
            )
        ):
            raise TypeError("observations must be Candidate values")
        if (
            not isinstance(current, tuple)
            or any(
                not isinstance(item, CandidateFamilyRevision)
                for item in current
            )
        ):
            raise TypeError(
                "current must be CandidateFamilyRevision values"
            )
        if not isinstance(profile, FeasibilityModelProfile):
            raise TypeError(
                "profile must be a FeasibilityModelProfile"
            )
        self.sweep_archives()

        ordered = tuple(sorted(
            observations, key=lambda item: item.candidate_id
        ))
        observation_ids = tuple(
            item.candidate_id for item in ordered
        )
        if len(set(observation_ids)) != len(observation_ids):
            raise ValueError("observations contain duplicate ids")
        if not ordered:
            persisted = self.request_state.get_reconciliation(request_id)
            empty = ReconciliationResult.empty(repository_id)
            if persisted is not None and persisted != empty:
                raise ReconciliationRunnerError(
                    "Empty reconciliation conflicts with persisted result"
                )
            return empty

        proposed_family_ids = tuple(
            candidate_family_id(repository_id, item.candidate_id)
            for item in ordered
        )
        prompt = self.render_prompt(
            repository_id,
            ordered,
            proposed_family_ids,
            current,
        )
        input_digest = _input_digest(
            repository_id=repository_id,
            observations=ordered,
            current=current,
            prompt=prompt,
            profile=profile,
        )
        attempt = self.request_state.begin_reconciliation_attempt(
            request_id, input_digest, _now()
        )
        if attempt.state in ("validated", "accepted"):
            return self.request_state.commit_reconciliation_attempt(
                attempt.attempt_id
            )

        try:
            thread_id = self.gateway.start_disposable_thread(
                cwd, profile
            )
        except (AppServerError, AppServerGatewayError) as error:
            self._abandon(
                attempt.attempt_id,
                _attempt_failure_code("thread", error),
            )
            raise ReconciliationAttemptRetryable(
                "Disposable reconciliation Thread must be retried"
            ) from error
        self.request_state.attach_reconciliation_thread(
            attempt.attempt_id, thread_id
        )

        try:
            _heartbeat(heartbeat)
            receipt = self.gateway.run_structured_turn(
                thread_id=thread_id,
                prompt=prompt,
                output_schema=reconciliation_output_schema(
                    observation_ids=observation_ids,
                    family_ids=(
                        tuple(
                            item.family_id for item in current
                        )
                        + proposed_family_ids
                    ),
                ),
                profile=profile,
                cwd=cwd,
            )
            _heartbeat(heartbeat)
            _verify_receipt(receipt, thread_id, profile)
            self.request_state.attach_reconciliation_turn(
                attempt.attempt_id, receipt.turn_id
            )
            decisions = validate_reconciliation(
                receipt.structured_output, ordered, current
            )
            result = apply_reconciliation(
                repository_id, ordered, current, decisions
            )
        except (
            AppServerError,
            AppServerGatewayError,
            ReconciliationRunnerError,
            ValueError,
        ) as error:
            self._abandon(
                attempt.attempt_id,
                _attempt_failure_code("turn", error),
            )
            raise ReconciliationAttemptRetryable(
                "Disposable reconciliation Turn must be retried"
            ) from error

        self.request_state.store_validated_reconciliation(
            attempt.attempt_id, result, _now()
        )
        winner = self.request_state.commit_reconciliation_attempt(
            attempt.attempt_id
        )
        self.sweep_archives()
        return winner

    def sweep_archives(self) -> None:
        for attempt in (
            self.request_state.pending_reconciliation_archives()
        ):
            assert attempt.thread_id is not None
            try:
                self.gateway.archive_thread(attempt.thread_id)
            except (AppServerError, AppServerGatewayError):
                continue
            self.request_state.mark_reconciliation_archived(
                attempt.attempt_id
            )

    def _abandon(self, attempt_id: str, failure_code: str) -> None:
        self.request_state.abandon_reconciliation_attempt(
            attempt_id, failure_code, _now()
        )
        self.sweep_archives()

    def render_prompt(
        self,
        repository_id: str,
        observations: tuple[Candidate, ...],
        proposed_family_ids: tuple[str, ...],
        current: tuple[CandidateFamilyRevision, ...],
    ) -> str:
        if len(observations) != len(proposed_family_ids):
            raise ValueError(
                "Every observation requires a proposed family"
            )
        contract = (
            files("zdecision.capture")
            .joinpath(
                "prompt_contracts",
                "candidate-reconciliation-v1.md",
            )
            .read_text(encoding="utf-8")
            .rstrip()
        )
        data = {
            "repository_id": repository_id,
            "current_families": [
                item.to_dict() for item in current
            ],
            "observations": [
                {
                    "observation_id": observation.candidate_id,
                    "proposed_family_id": proposed_family_id,
                    "content": observation.content.to_dict(),
                    "evidence_digest": hashlib.sha256(
                        canonical_json_bytes(
                            {"observation": observation.to_dict()}
                        )
                    ).hexdigest(),
                }
                for observation, proposed_family_id in zip(
                    observations,
                    proposed_family_ids,
                    strict=True,
                )
            ],
        }
        payload = canonical_json_bytes(data).decode("utf-8")
        return (
            f"{contract}\n\n"
            "BEGIN_UNTRUSTED_RECONCILIATION_DATA\n"
            f"{payload}\n"
            "END_UNTRUSTED_RECONCILIATION_DATA"
        )


def _verify_receipt(
    receipt: AppServerTurnReceipt,
    thread_id: str,
    profile: FeasibilityModelProfile,
) -> None:
    if receipt.thread_id != thread_id:
        raise ReconciliationRunnerError(
            "Structured Turn returned the wrong Thread"
        )
    if receipt.model_profile_id != profile.profile_id:
        raise ReconciliationRunnerError(
            "Structured Turn returned the wrong model profile"
        )


def _input_digest(
    *,
    repository_id: str,
    observations: tuple[Candidate, ...],
    current: tuple[CandidateFamilyRevision, ...],
    prompt: str,
    profile: FeasibilityModelProfile,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "current_families": [
                    item.to_dict() for item in current
                ],
                "model_profile": {
                    "discovery_digest": profile.discovery_digest,
                    "discovered_at": profile.discovered_at,
                    "model_id": profile.model_id,
                    "profile_id": profile.profile_id,
                    "reasoning_effort": profile.reasoning_effort,
                },
                "observations": [
                    {
                        "content_digest": hashlib.sha256(
                            canonical_json_bytes(
                                item.content.to_dict()
                            )
                        ).hexdigest(),
                        "observation_id": item.candidate_id,
                    }
                    for item in observations
                ],
                "prompt_revision": _PROMPT_REVISION,
                "prompt_sha256": hashlib.sha256(
                    prompt.encode("utf-8")
                ).hexdigest(),
                "repository_id": repository_id,
            }
        )
    ).hexdigest()


def _attempt_failure_code(stage: str, error: Exception) -> str:
    if isinstance(
        error,
        (AppServerTimeout, AppServerEOF, AppServerProtocolError),
    ):
        return f"{stage}_result_unknown"
    if isinstance(error, AppServerRequestError):
        return f"{stage}_request_rejected"
    return f"{stage}_result_invalid"


def _heartbeat(callback: Callable[[], None] | None) -> None:
    if callback is not None:
        callback()


def _now() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
