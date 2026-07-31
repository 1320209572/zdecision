"""Cross-Session Candidate reconciliation in one fresh native Thread."""

from __future__ import annotations

import hashlib
from importlib.resources import files
from pathlib import Path

from zdecision.agent.request_state import (
    NativeCallCoordinator,
    RequestStateStore,
)
from zdecision.app_server.gateway import AppServerGateway
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


class ReconciliationRunnerError(Exception):
    """A native reconciliation result violates its frozen host boundary."""


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
        self.native_calls = NativeCallCoordinator(request_state)

    def run(
        self,
        *,
        request_id: str,
        repository_id: str,
        cwd: str,
        observations: tuple[Candidate, ...],
        current: tuple[CandidateFamilyRevision, ...],
        profile: FeasibilityModelProfile,
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

        persisted = self.request_state.get_reconciliation(request_id)
        if persisted is not None:
            if persisted.repository_id != repository_id:
                raise ReconciliationRunnerError(
                    "Persisted reconciliation repository conflicts"
                )
            return persisted

        ordered = tuple(sorted(
            observations, key=lambda item: item.candidate_id
        ))
        observation_ids = tuple(
            item.candidate_id for item in ordered
        )
        if len(set(observation_ids)) != len(observation_ids):
            raise ValueError("observations contain duplicate ids")
        if not ordered:
            result = ReconciliationResult.empty(repository_id)
            self.request_state.save_reconciliation(
                request_id, result
            )
            return result

        proposed_family_ids = tuple(
            candidate_family_id(repository_id, item.candidate_id)
            for item in ordered
        )
        thread_source = f"zdecision/reconciliation/{request_id}"
        thread_id = self.native_calls.resolve_thread(
            request_id=request_id,
            operation_key=request_id,
            stage="reconciliation_thread",
            stable_tag=thread_source,
            find=lambda tag: self.gateway.find_thread_by_source(
                tag, cwd=cwd
            ),
            create=lambda: self.gateway.start_ephemeral_thread(
                cwd, profile, thread_source
            ),
        )
        client_message_id = (
            f"zdecision/{request_id}/reconciliation"
        )
        receipt = self.native_calls.resolve_structured_turn(
            request_id=request_id,
            operation_key=request_id,
            stage="reconciliation_turn",
            stable_tag=client_message_id,
            read=lambda tag: (
                self.gateway.read_structured_turn_by_client_id(
                    thread_id, tag, profile
                )
            ),
            create=lambda: self.gateway.run_structured_turn(
                thread_id=thread_id,
                prompt=self.render_prompt(
                    repository_id,
                    ordered,
                    proposed_family_ids,
                    current,
                ),
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
                client_user_message_id=client_message_id,
            ),
        )
        _verify_receipt(receipt, thread_id, profile)
        decisions = validate_reconciliation(
            receipt.structured_output, ordered, current
        )
        result = apply_reconciliation(
            repository_id, ordered, current, decisions
        )
        self.request_state.save_reconciliation(
            request_id, result
        )
        return result

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
