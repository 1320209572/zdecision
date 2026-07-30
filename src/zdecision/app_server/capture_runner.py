"""Automatic eligibility assessment and isolated two-stage Capture orchestration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from zdecision.agent.db import (
    AgentDatabase,
    AutomatedCaptureRunRecord,
    BoundaryAssessmentRecord,
)
from zdecision.app_server.gateway import AppServerGateway
from zdecision.app_server.models import FeasibilityModelProfile
from zdecision.capture.eligibility import (
    ELIGIBILITY_PROMPT_VERSION,
    BoundaryAssessment,
    SourceBoundaryFacts,
    capture_eligible,
    eligibility_output_schema,
    eligibility_prompt,
    validate_boundary_assessment,
)
from zdecision.capture.service import CaptureService
from zdecision.jsonio import canonical_json_bytes


class AutomatedCaptureError(Exception):
    """Base class for bounded automatic-Capture failures."""


class AutomatedCapturePreconditionError(AutomatedCaptureError):
    """Local trusted facts do not identify one enabled repository/product."""


class AutomatedCaptureAmbiguous(AutomatedCaptureError):
    """An external fork may exist without a durably attached native id."""

    def __init__(self, automated_capture_id: str) -> None:
        self.automated_capture_id = automated_capture_id
        super().__init__(
            f"Automated Capture {automated_capture_id} is ambiguous; "
            "a replacement fork is forbidden"
        )


class AutomatedCaptureFailed(AutomatedCaptureError):
    """A prior attempt reached a bounded terminal failure."""


@dataclass(frozen=True)
class AutomatedCaptureResult:
    automated_capture_id: str
    source_thread_id: str
    source_turn_id: str
    assessment_turn_id: str
    assessment: BoundaryAssessment
    capture_operation_id: str | None
    capture_thread_id: str | None
    inventory_turn_id: str | None
    extraction_turn_id: str | None
    candidate_ids: tuple[str, ...]
    model_profile_id: str


@dataclass(frozen=True)
class _LocalBoundaryContext:
    repository_id: str
    product_id: str
    product_name: str
    cwd: str
    head_commit: str | None
    reported_work_state: str | None
    validation: str
    unresolved_blockers: tuple[str, ...]


class AutomatedCaptureRunner:
    def __init__(
        self,
        *,
        gateway: AppServerGateway,
        database: AgentDatabase,
        capture_service: CaptureService,
        clock=None,
    ) -> None:
        self.gateway = gateway
        self.database = database
        self.capture_service = capture_service
        self.clock = clock or (lambda: datetime.now(UTC))

    def run(
        self,
        session_id: str,
        source_turn_id: str,
        template_id: str = "business",
    ) -> AutomatedCaptureResult:
        source_thread_id = _nonempty(session_id, "session_id")
        source_turn = _nonempty(source_turn_id, "source_turn_id")
        requested_template = _nonempty(template_id, "template_id")
        context = self._local_context(source_thread_id, source_turn)

        existing_id = self.database.automated_capture_id_for_boundary(
            source_thread_id, source_turn
        )
        if existing_id is not None:
            existing = self.database.get_automated_capture_run(existing_id)
            assert existing is not None
            if existing.template_id != requested_template:
                raise AutomatedCapturePreconditionError(
                    "The source boundary was already assessed with another template"
                )
            return self._terminal_replay(existing)

        snapshot = self.capture_service.catalog.render(
            requested_template, context.product_name
        )
        boundary = self.gateway.read_completed_boundary(
            source_thread_id, source_turn
        )
        if Path(boundary.cwd) != Path(context.cwd):
            raise AutomatedCapturePreconditionError(
                "Hook and app-server source directories do not match"
            )
        profile = self.gateway.discover_and_freeze_profile(boundary)
        facts = SourceBoundaryFacts(
            source_thread_id=source_thread_id,
            source_turn_id=source_turn,
            repository_id=context.repository_id,
            head_commit=context.head_commit,
            work_kind="code",
            source_turn_completed=True,
            source_turn_assessed=self.database.boundary_has_assessment(
                source_thread_id, source_turn
            ),
            capture_active=self.database.automated_capture_active(
                source_thread_id, source_turn
            ),
            repository_mapping_valid=True,
            local_runtime_valid=True,
            reported_work_state=context.reported_work_state,
            validation=context.validation,
            unresolved_blockers=context.unresolved_blockers,
        )
        prompt = eligibility_prompt(facts)
        prompt_digest = _digest(
            {
                "prompt": prompt,
                "prompt_version": ELIGIBILITY_PROMPT_VERSION,
            }
        )
        input_fact_digest = _digest(facts.to_dict())
        template_snapshot_digest = _digest(snapshot.to_dict())
        automated_capture_id = _automated_capture_id(
            session_id=source_thread_id,
            source_turn_id=source_turn,
            product_id=context.product_id,
            prompt_digest=prompt_digest,
            template_snapshot_digest=template_snapshot_digest,
            model_profile_id=profile.profile_id,
        )
        now = _format_datetime(self.clock())
        run = self.database.create_automated_capture_run(
            AutomatedCaptureRunRecord(
                automated_capture_id=automated_capture_id,
                session_id=source_thread_id,
                source_turn_id=source_turn,
                repository_id=context.repository_id,
                product_id=context.product_id,
                product_name=context.product_name,
                template_id=requested_template,
                template_snapshot_digest=template_snapshot_digest,
                eligibility_prompt_digest=prompt_digest,
                model_profile_id=profile.profile_id,
                state="prepared",
                assessment_thread_id=None,
                assessment_turn_id=None,
                capture_operation_id=None,
                capture_thread_id=None,
                inventory_turn_id=None,
                extraction_turn_id=None,
                candidate_ids=(),
                failure_code=None,
                created_at=now,
                updated_at=now,
            )
        )

        run = self._transition(run, state="assessment_fork_pending")
        try:
            assessment_thread_id = self.gateway.fork_ephemeral(
                source_thread_id, source_turn
            )
        except Exception:
            self._transition(run, state="ambiguous", failure_code="assessment_fork_ambiguous")
            raise AutomatedCaptureAmbiguous(automated_capture_id) from None
        run = self._transition(
            run,
            state="assessment_fork_attached",
            assessment_thread_id=assessment_thread_id,
        )
        try:
            assessment_receipt = self.gateway.run_structured_turn(
                thread_id=assessment_thread_id,
                prompt=prompt,
                output_schema=eligibility_output_schema(),
                profile=profile,
                cwd=boundary.cwd,
            )
            _require_profile(assessment_receipt.model_profile_id, profile)
            assessment = validate_boundary_assessment(
                assessment_receipt.structured_output
            )
        except Exception:
            self._transition(
                run, state="failed", failure_code="assessment_turn_failed"
            )
            raise AutomatedCaptureFailed(
                f"Automated Capture {automated_capture_id} assessment failed"
            ) from None
        self.database.put_boundary_assessment(
            BoundaryAssessmentRecord(
                automated_capture_id=automated_capture_id,
                source_thread_id=source_thread_id,
                source_turn_id=source_turn,
                prompt_version=ELIGIBILITY_PROMPT_VERSION,
                prompt_digest=prompt_digest,
                input_fact_digest=input_fact_digest,
                assessment_thread_id=assessment_thread_id,
                assessment_turn_id=assessment_receipt.turn_id,
                model_profile_id=profile.profile_id,
                phase=assessment.phase,
                has_durable_decision_signal=(
                    assessment.has_durable_decision_signal
                ),
                validation=assessment.validation,
                unresolved_blockers=assessment.unresolved_blockers,
                recorded_at=_format_datetime(self.clock()),
            )
        )
        run = self._transition(
            run,
            state="assessment_completed",
            assessment_turn_id=assessment_receipt.turn_id,
        )
        if not capture_eligible(assessment, facts):
            run = self._transition(run, state="completed_ineligible")
            return self._result(run, assessment)

        plan = self.capture_service.prepare(
            source_thread_id,
            source_turn,
            context.product_name,
            requested_template,
        )
        if plan.record.template != snapshot:
            self._transition(run, state="failed", failure_code="template_changed")
            raise AutomatedCaptureFailed(
                f"Automated Capture {automated_capture_id} template changed"
            )
        run = self._transition(
            run,
            state="capture_fork_pending",
            capture_operation_id=plan.record.operation_id,
        )
        try:
            capture_thread_id = self.gateway.fork_ephemeral(
                source_thread_id, source_turn
            )
        except Exception:
            self._transition(
                run, state="ambiguous", failure_code="capture_fork_ambiguous"
            )
            raise AutomatedCaptureAmbiguous(automated_capture_id) from None
        if capture_thread_id == assessment_thread_id:
            self._transition(
                run, state="ambiguous", failure_code="capture_fork_not_fresh"
            )
            raise AutomatedCaptureAmbiguous(automated_capture_id)
        self.capture_service.attach_fork(
            plan.record.operation_id, capture_thread_id
        )
        run = self._transition(
            run,
            state="capture_fork_attached",
            capture_thread_id=capture_thread_id,
        )

        try:
            inventory_receipt = self.gateway.run_structured_turn(
                thread_id=capture_thread_id,
                prompt=plan.inventory_prompt,
                output_schema=_inventory_output_schema(),
                profile=profile,
                cwd=boundary.cwd,
            )
            _require_profile(inventory_receipt.model_profile_id, profile)
            self.capture_service.attach_stage_turn(
                plan.record.operation_id,
                "inventory",
                inventory_receipt.turn_id,
            )
            self.capture_service.complete_inventory(
                plan.record.operation_id,
                inventory_receipt.structured_output,
                raw_output_sha256=inventory_receipt.output_sha256,
            )
        except Exception:
            self._transition(run, state="failed", failure_code="inventory_failed")
            raise AutomatedCaptureFailed(
                f"Automated Capture {automated_capture_id} Inventory failed"
            ) from None
        run = self._transition(
            run,
            state="inventory_completed",
            inventory_turn_id=inventory_receipt.turn_id,
        )

        try:
            extraction_receipt = self.gateway.run_structured_turn(
                thread_id=capture_thread_id,
                prompt=plan.extraction_prompt,
                output_schema=_extraction_output_schema(context.product_name),
                profile=profile,
                cwd=boundary.cwd,
            )
            _require_profile(extraction_receipt.model_profile_id, profile)
            self.capture_service.attach_stage_turn(
                plan.record.operation_id,
                "extraction",
                extraction_receipt.turn_id,
            )
            candidate_set = self.capture_service.complete_extraction(
                plan.record.operation_id,
                extraction_receipt.structured_output,
                raw_output_sha256=extraction_receipt.output_sha256,
            )
        except Exception:
            self._transition(run, state="failed", failure_code="extraction_failed")
            raise AutomatedCaptureFailed(
                f"Automated Capture {automated_capture_id} Extraction failed"
            ) from None
        run = self._transition(
            run,
            state="completed",
            extraction_turn_id=extraction_receipt.turn_id,
            candidate_ids=candidate_set.candidate_ids,
        )
        return self._result(run, assessment)

    def _local_context(
        self, session_id: str, source_turn_id: str
    ) -> _LocalBoundaryContext:
        events = tuple(
            event
            for event in self.database.list_events(session_id)
            if event.invocation.turn_id == source_turn_id
        )
        if not events:
            raise AutomatedCapturePreconditionError(
                "The Hook Event Ledger does not contain the source Turn"
            )
        repository_ids = {
            event.invocation.repository_id
            for event in events
            if event.invocation.repository_id is not None
        }
        if len(repository_ids) != 1:
            raise AutomatedCapturePreconditionError(
                "The source Turn does not resolve to one repository"
            )
        repository_id = next(iter(repository_ids))
        mapping = self.database.get_repository_mapping(repository_id)
        if mapping is None or not mapping.enabled:
            raise AutomatedCapturePreconditionError(
                "The source repository is not enabled for ZDecision"
            )
        cwds = {event.invocation.cwd for event in events}
        if len(cwds) != 1:
            raise AutomatedCapturePreconditionError(
                "The source Turn has conflicting working directories"
            )
        head_commit = next(
            (
                event.invocation.head_commit
                for event in reversed(events)
                if event.invocation.head_commit is not None
            ),
            None,
        )
        reported_work_state: str | None = None
        validation = "unknown"
        blocker_count = 0
        for event in events:
            facts = event.invocation.safe_fact
            if facts.get("report_kind") == "work_state":
                reported_work_state = facts.get("status")
                validation = facts.get("validation")
                blocker_count = facts.get("unresolved_blocker_count", 0)
        if reported_work_state is None:
            validation_events = [
                event
                for event in events
                if event.invocation.safe_fact.get("classification") == "validation"
            ]
            if validation_events:
                exit_status = validation_events[-1].invocation.safe_fact.get(
                    "exit_status"
                )
                if exit_status == 0:
                    validation = "passed"
                elif isinstance(exit_status, int):
                    validation = "failed"
        if (
            reported_work_state is not None
            and reported_work_state
            not in {
                "exploring",
                "implementing",
                "awaiting_user",
                "validation_failed",
                "milestone_complete",
            }
        ):
            raise AutomatedCapturePreconditionError(
                "The reported work state is invalid"
            )
        if validation not in {"passed", "failed", "not_applicable", "unknown"}:
            raise AutomatedCapturePreconditionError(
                "The reported validation state is invalid"
            )
        if (
            not isinstance(blocker_count, int)
            or isinstance(blocker_count, bool)
            or not 0 <= blocker_count <= 20
        ):
            raise AutomatedCapturePreconditionError(
                "The reported blocker count is invalid"
            )
        blockers = tuple(
            f"reported_blocker_{index:02d}"
            for index in range(1, blocker_count + 1)
        )
        return _LocalBoundaryContext(
            repository_id=repository_id,
            product_id=mapping.product_id,
            product_name=mapping.product_name,
            cwd=next(iter(cwds)),
            head_commit=head_commit,
            reported_work_state=reported_work_state,
            validation=validation,
            unresolved_blockers=blockers,
        )

    def _transition(
        self,
        run: AutomatedCaptureRunRecord,
        *,
        state: str,
        assessment_thread_id: str | None = None,
        assessment_turn_id: str | None = None,
        capture_operation_id: str | None = None,
        capture_thread_id: str | None = None,
        inventory_turn_id: str | None = None,
        extraction_turn_id: str | None = None,
        candidate_ids: tuple[str, ...] | None = None,
        failure_code: str | None = None,
    ) -> AutomatedCaptureRunRecord:
        replacement = replace(
            run,
            state=state,
            assessment_thread_id=(
                run.assessment_thread_id
                if assessment_thread_id is None
                else assessment_thread_id
            ),
            assessment_turn_id=(
                run.assessment_turn_id
                if assessment_turn_id is None
                else assessment_turn_id
            ),
            capture_operation_id=(
                run.capture_operation_id
                if capture_operation_id is None
                else capture_operation_id
            ),
            capture_thread_id=(
                run.capture_thread_id
                if capture_thread_id is None
                else capture_thread_id
            ),
            inventory_turn_id=(
                run.inventory_turn_id
                if inventory_turn_id is None
                else inventory_turn_id
            ),
            extraction_turn_id=(
                run.extraction_turn_id
                if extraction_turn_id is None
                else extraction_turn_id
            ),
            candidate_ids=(run.candidate_ids if candidate_ids is None else candidate_ids),
            failure_code=failure_code,
            updated_at=_format_datetime(self.clock()),
        )
        return self.database.replace_automated_capture_run(run, replacement)

    def _terminal_replay(
        self, run: AutomatedCaptureRunRecord
    ) -> AutomatedCaptureResult:
        if run.state == "ambiguous":
            raise AutomatedCaptureAmbiguous(run.automated_capture_id)
        if run.state == "failed":
            raise AutomatedCaptureFailed(
                f"Automated Capture {run.automated_capture_id} previously failed"
            )
        if run.state not in {"completed", "completed_ineligible"}:
            raise AutomatedCaptureAmbiguous(run.automated_capture_id)
        assessment_record = self.database.get_boundary_assessment(
            run.automated_capture_id
        )
        if assessment_record is None:
            raise AutomatedCaptureFailed(
                f"Automated Capture {run.automated_capture_id} has no assessment"
            )
        assessment = BoundaryAssessment(
            phase=assessment_record.phase,
            has_durable_decision_signal=(
                assessment_record.has_durable_decision_signal
            ),
            validation=assessment_record.validation,
            unresolved_blockers=assessment_record.unresolved_blockers,
        )
        return self._result(run, assessment)

    @staticmethod
    def _result(
        run: AutomatedCaptureRunRecord, assessment: BoundaryAssessment
    ) -> AutomatedCaptureResult:
        if run.assessment_turn_id is None:
            raise AutomatedCaptureFailed(
                f"Automated Capture {run.automated_capture_id} has no assessment Turn"
            )
        return AutomatedCaptureResult(
            automated_capture_id=run.automated_capture_id,
            source_thread_id=run.session_id,
            source_turn_id=run.source_turn_id,
            assessment_turn_id=run.assessment_turn_id,
            assessment=assessment,
            capture_operation_id=run.capture_operation_id,
            capture_thread_id=run.capture_thread_id,
            inventory_turn_id=run.inventory_turn_id,
            extraction_turn_id=run.extraction_turn_id,
            candidate_ids=run.candidate_ids,
            model_profile_id=run.model_profile_id,
        )


def _automated_capture_id(
    *,
    session_id: str,
    source_turn_id: str,
    product_id: str,
    prompt_digest: str,
    template_snapshot_digest: str,
    model_profile_id: str,
) -> str:
    identity = _digest(
        {
            "eligibility_prompt_digest": prompt_digest,
            "model_profile_id": model_profile_id,
            "product_id": product_id,
            "session_id": session_id,
            "source_turn_id": source_turn_id,
            "template_snapshot_digest": template_snapshot_digest,
        }
    )
    return f"acp_{identity[:32]}"


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _format_datetime(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _require_profile(
    receipt_profile_id: str, profile: FeasibilityModelProfile
) -> None:
    if receipt_profile_id != profile.profile_id:
        raise AutomatedCaptureFailed("Structured Turn used another model profile")


def _inventory_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "signals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "rule": {"type": "string"},
                        "future_effect": {"type": "string"},
                        "scope": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "current_confirmed",
                                "unresolved",
                                "superseded",
                            ],
                        },
                        "confirmation_basis": {
                            "type": "string",
                            "enum": [
                                "explicit_user_confirmation",
                                "explicit_user_direction",
                                "adopted_decision_contract",
                                "uncertain",
                            ],
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                    },
                    "required": [
                        "topic",
                        "rule",
                        "future_effect",
                        "scope",
                        "status",
                        "confirmation_basis",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
            "coverage": {
                "type": "object",
                "properties": {
                    "reviewed_retained_context": {
                        "type": "string",
                        "enum": ["earliest_to_latest"],
                    },
                    "known_gaps": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["reviewed_retained_context", "known_gaps"],
                "additionalProperties": False,
            },
        },
        "required": ["signals", "coverage"],
        "additionalProperties": False,
    }


def _extraction_output_schema(product: str) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product": {"type": "string", "enum": [product]},
                        "claim": {"type": "string"},
                        "future_action": {"type": "string"},
                        "scope": {
                            "type": "object",
                            "properties": {
                                "summary": {"type": "string"},
                                "repositories": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "paths": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["summary", "repositories", "paths"],
                            "additionalProperties": False,
                        },
                        "invalidation_conditions": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": [
                        "product",
                        "claim",
                        "future_action",
                        "scope",
                        "invalidation_conditions",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["candidates"],
        "additionalProperties": False,
    }
