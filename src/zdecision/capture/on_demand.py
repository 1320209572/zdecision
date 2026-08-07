"""Immutable extractor-v3 Capture inputs, attempts, and committed results."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from zdecision.capture.inventory import (
    InventoryResult,
    validate_inventory,
    validate_inventory_v5,
)
from zdecision.capture.models import Candidate, SourceCheckpoint
from zdecision.capture.provenance import (
    CandidateProvenance,
    CaptureEvidenceManifest,
    SignalProvenance,
)
from zdecision.capture.service import (
    validate_extraction_output,
    validate_extraction_output_v5,
)
from zdecision.capture.templates import TemplateSnapshot
from zdecision.ids import (
    ON_DEMAND_CAPTURE_PROTOCOL,
    capture_candidate_id,
    on_demand_capture_operation_id,
)
from zdecision.jsonio import canonical_json_bytes


CaptureOperationStatus = Literal["open", "committed", "failed_terminal"]
AttemptState = Literal[
    "prepared",
    "creating_thread",
    "running",
    "validated",
    "accepted",
    "superseded",
    "abandoned",
]
ArchiveState = Literal["not_applicable", "pending", "archived"]

_CAPTURE_ID = re.compile(r"^cap_[0-9a-f]{32}$")
_REQUEST_ID = re.compile(r"^crq_[0-9a-f]{32}$")
_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_DECISION_SPACE_ID = re.compile(r"^dsp_[0-9a-f]{32}$")
_ROUTE_ID = re.compile(r"^drr_[0-9a-f]{32}$")
_PRODUCT_ID = re.compile(r"^prod_[0-9a-f]{32}$")
_MODEL_PROFILE_ID = re.compile(r"^fmp_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_FROZEN_V3_FIELDS = frozenset(
    (
        "record_version",
        "protocol_revision",
        "operation_id",
        "request_id",
        "repository_id",
        "source_key",
        "session_id",
        "cwd",
        "lineage",
        "previous_handled_turn_id",
        "upper_turn_id",
        "source_fingerprint",
        "product",
        "template",
        "model_profile_id",
        "model_id",
        "reasoning_effort",
        "model_discovery_digest",
        "model_discovered_at",
    )
)
_FROZEN_V4_FIELDS = _FROZEN_V3_FIELDS | frozenset(("route_context",))
_FROZEN_V5_FIELDS = _FROZEN_V4_FIELDS | frozenset(("evidence_manifest",))
_RESULT_V1_FIELDS = frozenset(
    (
        "operation_id",
        "inventory",
        "inventory_sha256",
        "extraction_sha256",
        "observations",
        "result_digest",
    )
)
_RESULT_V2_FIELDS = _RESULT_V1_FIELDS | frozenset(
    (
        "result_version",
        "evidence_manifest",
        "signal_provenance",
        "candidate_provenance",
    )
)
_OPERATION_STATUSES = frozenset(("open", "committed", "failed_terminal"))
_ATTEMPT_STATES = frozenset(
    (
        "prepared",
        "creating_thread",
        "running",
        "validated",
        "accepted",
        "superseded",
        "abandoned",
    )
)
_ARCHIVE_STATES = frozenset(("not_applicable", "pending", "archived"))


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    record_name: str,
) -> None:
    if frozenset(value) != expected:
        raise ValueError(f"{record_name} fields are invalid")


def _candidate_as_extraction_item(candidate: Candidate) -> dict[str, object]:
    return {
        "product": candidate.content.product,
        "claim": candidate.content.claim,
        "future_action": candidate.content.future_action,
        "scope": {
            "summary": candidate.content.scope_summary,
            "repositories": list(candidate.content.repositories),
            "paths": list(candidate.content.paths),
        },
        "invalidation_conditions": list(
            candidate.content.invalidation_conditions
        ),
    }


@dataclass(frozen=True)
class FrozenCaptureRouteContext:
    decision_space_id: str
    decision_space_kind: Literal["product", "shared_unit"]
    decision_space_name: str
    route_id: str
    route_configuration_version: int
    compatibility_product_id: str
    matched_path_digest: str

    def __post_init__(self) -> None:
        if _DECISION_SPACE_ID.fullmatch(self.decision_space_id) is None:
            raise ValueError("decision_space_id is invalid")
        if self.decision_space_kind not in ("product", "shared_unit"):
            raise ValueError("decision_space_kind is invalid")
        _nonempty(self.decision_space_name, "decision_space_name")
        if _ROUTE_ID.fullmatch(self.route_id) is None:
            raise ValueError("route_id is invalid")
        if (
            not isinstance(self.route_configuration_version, int)
            or isinstance(self.route_configuration_version, bool)
            or self.route_configuration_version < 1
        ):
            raise ValueError("route_configuration_version is invalid")
        if _PRODUCT_ID.fullmatch(self.compatibility_product_id) is None:
            raise ValueError("compatibility_product_id is invalid")
        _digest(self.matched_path_digest, "matched_path_digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_space_id": self.decision_space_id,
            "decision_space_kind": self.decision_space_kind,
            "decision_space_name": self.decision_space_name,
            "route_id": self.route_id,
            "route_configuration_version": self.route_configuration_version,
            "compatibility_product_id": self.compatibility_product_id,
            "matched_path_digest": self.matched_path_digest,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> "FrozenCaptureRouteContext":
        _require_exact_fields(
            value,
            frozenset(
                (
                    "decision_space_id",
                    "decision_space_kind",
                    "decision_space_name",
                    "route_id",
                    "route_configuration_version",
                    "compatibility_product_id",
                    "matched_path_digest",
                )
            ),
            "FrozenCaptureRouteContext",
        )
        return cls(**value)


@dataclass(frozen=True)
class FrozenCaptureInput:
    record_version: Literal[3, 4, 5]
    protocol_revision: str
    operation_id: str
    request_id: str
    repository_id: str
    source_key: str
    session_id: str
    cwd: str
    lineage: str
    previous_handled_turn_id: str | None
    upper_turn_id: str
    source_fingerprint: str
    product: str
    template: TemplateSnapshot
    model_profile_id: str
    model_id: str
    reasoning_effort: str
    model_discovery_digest: str
    model_discovered_at: str
    route_context: FrozenCaptureRouteContext | None
    evidence_manifest: CaptureEvidenceManifest | None = None

    def __post_init__(self) -> None:
        if self.record_version not in (3, 4, 5) or isinstance(
            self.record_version, bool
        ):
            raise ValueError("FrozenCaptureInput record_version is invalid")
        if _CAPTURE_ID.fullmatch(self.operation_id) is None:
            raise ValueError("FrozenCaptureInput operation_id is invalid")
        if _REQUEST_ID.fullmatch(self.request_id) is None:
            raise ValueError("FrozenCaptureInput request_id is invalid")
        if _REPOSITORY_ID.fullmatch(self.repository_id) is None:
            raise ValueError("FrozenCaptureInput repository_id is invalid")
        if _MODEL_PROFILE_ID.fullmatch(self.model_profile_id) is None:
            raise ValueError("FrozenCaptureInput model_profile_id is invalid")
        for field_name in (
            "protocol_revision",
            "source_key",
            "session_id",
            "lineage",
            "upper_turn_id",
            "product",
            "model_id",
            "reasoning_effort",
            "model_discovered_at",
        ):
            _nonempty(getattr(self, field_name), field_name)
        if self.record_version == 3:
            if (
                not self.protocol_revision.startswith("extractor-v3")
                or self.route_context is not None
                or self.evidence_manifest is not None
            ):
                raise ValueError("FrozenCaptureInput v3 fields are invalid")
        elif self.record_version == 4 and (
            not self.protocol_revision.startswith("extractor-v4")
            or not isinstance(self.route_context, FrozenCaptureRouteContext)
            or self.evidence_manifest is not None
        ):
            raise ValueError("FrozenCaptureInput protocol_revision is invalid")
        elif self.record_version == 5 and (
            not self.protocol_revision.startswith("extractor-v5")
            or not isinstance(self.route_context, FrozenCaptureRouteContext)
            or not isinstance(self.evidence_manifest, CaptureEvidenceManifest)
        ):
            raise ValueError("FrozenCaptureInput v5 fields are invalid")
        if not Path(self.cwd).is_absolute():
            raise ValueError("FrozenCaptureInput cwd must be absolute")
        if self.previous_handled_turn_id is not None:
            _nonempty(
                self.previous_handled_turn_id,
                "previous_handled_turn_id",
            )
        _digest(self.source_fingerprint, "source_fingerprint")
        _digest(self.model_discovery_digest, "model_discovery_digest")
        if not isinstance(self.template, TemplateSnapshot):
            raise TypeError("template must be a TemplateSnapshot")
        self.template.verify_integrity()
        if self.operation_id != on_demand_capture_operation_id(
            self._identity_payload()
        ):
            raise ValueError("FrozenCaptureInput operation identity mismatch")

    @classmethod
    def create(
        cls,
        request_id: str,
        repository_id: str,
        source_key: str,
        session_id: str,
        cwd: str,
        lineage: str,
        previous_handled_turn_id: str | None,
        upper_turn_id: str,
        source_fingerprint: str,
        product: str,
        template: TemplateSnapshot,
        model_profile_id: str,
        model_id: str,
        reasoning_effort: str,
        model_discovery_digest: str,
        model_discovered_at: str,
        route_context: FrozenCaptureRouteContext,
        evidence_manifest: CaptureEvidenceManifest,
        protocol_revision: str = ON_DEMAND_CAPTURE_PROTOCOL,
    ) -> "FrozenCaptureInput":
        identity = {
            "protocol": protocol_revision,
            "request_id": request_id,
            "repository_id": repository_id,
            "source_key": source_key,
            "session_id": session_id,
            "cwd": cwd,
            "lineage": lineage,
            "previous_handled_turn_id": previous_handled_turn_id,
            "upper_turn_id": upper_turn_id,
            "source_fingerprint": source_fingerprint,
            "product": product,
            "template": template.to_dict(),
            "model_profile_id": model_profile_id,
            "model_id": model_id,
            "reasoning_effort": reasoning_effort,
            "model_discovery_digest": model_discovery_digest,
            "model_discovered_at": model_discovered_at,
            "route_context": route_context.to_dict(),
            "evidence_manifest": evidence_manifest.to_dict(),
        }
        return cls(
            record_version=5,
            operation_id=on_demand_capture_operation_id(identity),
            protocol_revision=protocol_revision,
            request_id=request_id,
            repository_id=repository_id,
            source_key=source_key,
            session_id=session_id,
            cwd=cwd,
            lineage=lineage,
            previous_handled_turn_id=previous_handled_turn_id,
            upper_turn_id=upper_turn_id,
            source_fingerprint=source_fingerprint,
            product=product,
            template=template,
            model_profile_id=model_profile_id,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
            model_discovery_digest=model_discovery_digest,
            model_discovered_at=model_discovered_at,
            route_context=route_context,
            evidence_manifest=evidence_manifest,
        )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "protocol": self.protocol_revision,
            "request_id": self.request_id,
            "repository_id": self.repository_id,
            "source_key": self.source_key,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "lineage": self.lineage,
            "previous_handled_turn_id": self.previous_handled_turn_id,
            "upper_turn_id": self.upper_turn_id,
            "source_fingerprint": self.source_fingerprint,
            "product": self.product,
            "template": self.template.to_dict(),
            "model_profile_id": self.model_profile_id,
            "model_id": self.model_id,
            "reasoning_effort": self.reasoning_effort,
            "model_discovery_digest": self.model_discovery_digest,
            "model_discovered_at": self.model_discovered_at,
            **(
                {}
                if self.route_context is None
                else {"route_context": self.route_context.to_dict()}
            ),
            **(
                {}
                if self.evidence_manifest is None
                else {"evidence_manifest": self.evidence_manifest.to_dict()}
            ),
        }

    @property
    def frozen_digest(self) -> str:
        return _sha256(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "record_version": self.record_version,
            "protocol_revision": self.protocol_revision,
            "operation_id": self.operation_id,
            "request_id": self.request_id,
            "repository_id": self.repository_id,
            "source_key": self.source_key,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "lineage": self.lineage,
            "previous_handled_turn_id": self.previous_handled_turn_id,
            "upper_turn_id": self.upper_turn_id,
            "source_fingerprint": self.source_fingerprint,
            "product": self.product,
            "template": self.template.to_dict(),
            "model_profile_id": self.model_profile_id,
            "model_id": self.model_id,
            "reasoning_effort": self.reasoning_effort,
            "model_discovery_digest": self.model_discovery_digest,
            "model_discovered_at": self.model_discovered_at,
            **(
                {}
                if self.route_context is None
                else {"route_context": self.route_context.to_dict()}
            ),
            **(
                {}
                if self.evidence_manifest is None
                else {"evidence_manifest": self.evidence_manifest.to_dict()}
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> "FrozenCaptureInput":
        version = value.get("record_version")
        expected = (
            _FROZEN_V3_FIELDS
            if version == 3
            else _FROZEN_V4_FIELDS if version == 4 else _FROZEN_V5_FIELDS
        )
        _require_exact_fields(value, expected, "FrozenCaptureInput")
        template = value["template"]
        if not isinstance(template, Mapping):
            raise ValueError("FrozenCaptureInput template must be an object")
        raw_context = value.get("route_context")
        if raw_context is not None and not isinstance(raw_context, Mapping):
            raise ValueError("FrozenCaptureInput route_context is invalid")
        raw_manifest = value.get("evidence_manifest")
        if raw_manifest is not None and not isinstance(raw_manifest, dict):
            raise ValueError("FrozenCaptureInput evidence_manifest is invalid")
        return cls(
            record_version=value["record_version"],
            protocol_revision=value["protocol_revision"],
            operation_id=value["operation_id"],
            request_id=value["request_id"],
            repository_id=value["repository_id"],
            source_key=value["source_key"],
            session_id=value["session_id"],
            cwd=value["cwd"],
            lineage=value["lineage"],
            previous_handled_turn_id=value["previous_handled_turn_id"],
            upper_turn_id=value["upper_turn_id"],
            source_fingerprint=value["source_fingerprint"],
            product=value["product"],
            template=TemplateSnapshot.from_dict(template),
            model_profile_id=value["model_profile_id"],
            model_id=value["model_id"],
            reasoning_effort=value["reasoning_effort"],
            model_discovery_digest=value["model_discovery_digest"],
            model_discovered_at=value["model_discovered_at"],
            route_context=(
                None
                if raw_context is None
                else FrozenCaptureRouteContext.from_dict(raw_context)
            ),
            evidence_manifest=(
                None
                if raw_manifest is None
                else CaptureEvidenceManifest.from_dict(raw_manifest)
            ),
        )


@dataclass(frozen=True)
class ValidatedCaptureResult:
    result_version: Literal[1, 2]
    operation_id: str
    inventory: InventoryResult
    inventory_sha256: str
    extraction_sha256: str
    observations: tuple[Candidate, ...]
    evidence_manifest: CaptureEvidenceManifest | None
    signal_provenance: tuple[SignalProvenance, ...]
    candidate_provenance: tuple[CandidateProvenance, ...]
    result_digest: str

    @classmethod
    def create(
        cls,
        frozen: FrozenCaptureInput,
        inventory_output: object,
        extraction_output: object,
    ) -> "ValidatedCaptureResult":
        if not isinstance(frozen, FrozenCaptureInput):
            raise TypeError("frozen must be a FrozenCaptureInput")
        source = SourceCheckpoint(frozen.session_id, frozen.upper_turn_id)
        if frozen.record_version in (3, 4):
            inventory = validate_inventory(inventory_output)
            observations = validate_extraction_output(
                frozen.operation_id, source, frozen.product, extraction_output
            )
            inventory_sha256 = _sha256(inventory.to_dict())
            extraction_sha256 = _sha256(extraction_output)
            core = _result_v1_core(
                frozen.operation_id,
                inventory,
                inventory_sha256,
                extraction_sha256,
                observations,
            )
            return cls(
                result_version=1,
                operation_id=frozen.operation_id,
                inventory=inventory,
                inventory_sha256=inventory_sha256,
                extraction_sha256=extraction_sha256,
                observations=observations,
                evidence_manifest=None,
                signal_provenance=(),
                candidate_provenance=(),
                result_digest=_sha256(core),
            )
        if frozen.record_version != 5 or frozen.evidence_manifest is None:
            raise ValueError("FrozenCaptureInput result protocol is invalid")
        inventory, signal_provenance = validate_inventory_v5(
            inventory_output, frozen.evidence_manifest
        )
        observations, candidate_provenance = validate_extraction_output_v5(
            frozen.operation_id,
            source,
            frozen.product,
            extraction_output,
            inventory,
            signal_provenance,
            frozen.evidence_manifest,
        )
        inventory_sha256 = _sha256(inventory.to_dict())
        extraction_sha256 = _sha256(extraction_output)
        core = _result_v2_core(
            frozen.operation_id,
            inventory,
            inventory_sha256,
            extraction_sha256,
            observations,
            frozen.evidence_manifest,
            signal_provenance,
            candidate_provenance,
        )
        return cls(
            result_version=2,
            operation_id=frozen.operation_id,
            inventory=inventory,
            inventory_sha256=inventory_sha256,
            extraction_sha256=extraction_sha256,
            observations=observations,
            evidence_manifest=frozen.evidence_manifest,
            signal_provenance=signal_provenance,
            candidate_provenance=candidate_provenance,
            result_digest=_sha256(core),
        )

    def to_dict(self) -> dict[str, object]:
        legacy = {
            "operation_id": self.operation_id,
            "inventory": self.inventory.to_dict(),
            "inventory_sha256": self.inventory_sha256,
            "extraction_sha256": self.extraction_sha256,
            "observations": [
                observation.to_dict() for observation in self.observations
            ],
            "result_digest": self.result_digest,
        }
        if self.result_version == 1:
            return legacy
        return {
            "result_version": 2,
            **legacy,
            "evidence_manifest": self.evidence_manifest.to_dict()
            if self.evidence_manifest is not None
            else None,
            "signal_provenance": [
                item.to_dict() for item in self.signal_provenance
            ],
            "candidate_provenance": [
                item.to_dict() for item in self.candidate_provenance
            ],
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        frozen: FrozenCaptureInput | None = None,
    ) -> "ValidatedCaptureResult":
        version = 2 if value.get("result_version") == 2 else 1
        _require_exact_fields(
            value,
            _RESULT_V2_FIELDS if version == 2 else _RESULT_V1_FIELDS,
            "ValidatedCaptureResult",
        )
        if frozen is not None:
            if not isinstance(frozen, FrozenCaptureInput) or (
                (frozen.record_version == 5) != (version == 2)
            ):
                raise ValueError("ValidatedCaptureResult protocol pairing is invalid")
        operation_id = value["operation_id"]
        inventory_value = value["inventory"]
        observation_values = value["observations"]
        if (
            not isinstance(operation_id, str)
            or _CAPTURE_ID.fullmatch(operation_id) is None
            or not isinstance(inventory_value, Mapping)
            or not isinstance(observation_values, list)
        ):
            raise ValueError("ValidatedCaptureResult fields are invalid")
        inventory = validate_inventory(inventory_value)
        observations: list[Candidate] = []
        for raw_observation in observation_values:
            if not isinstance(raw_observation, Mapping):
                raise ValueError(
                    "ValidatedCaptureResult observation is invalid"
                )
            observations.append(Candidate.from_dict(raw_observation))
        typed_observations = tuple(observations)
        expected_ids = tuple(
            capture_candidate_id(operation_id, ordinal)
            for ordinal in range(1, len(typed_observations) + 1)
        )
        if (
            tuple(item.candidate_id for item in typed_observations)
            != expected_ids
            or any(
                item.capture_id != operation_id
                or item.ordinal != ordinal
                for ordinal, item in enumerate(typed_observations, start=1)
            )
        ):
            raise ValueError(
                "ValidatedCaptureResult observation identity mismatch"
            )
        inventory_sha256 = _digest(
            value["inventory_sha256"], "inventory_sha256"
        )
        extraction_sha256 = _digest(
            value["extraction_sha256"], "extraction_sha256"
        )
        result_digest = _digest(value["result_digest"], "result_digest")
        if inventory_sha256 != _sha256(inventory.to_dict()):
            raise ValueError("ValidatedCaptureResult inventory digest mismatch")
        evidence_manifest: CaptureEvidenceManifest | None = None
        signal_provenance: tuple[SignalProvenance, ...] = ()
        candidate_provenance: tuple[CandidateProvenance, ...] = ()
        if version == 2:
            raw_manifest = value["evidence_manifest"]
            raw_signal_provenance = value["signal_provenance"]
            raw_candidate_provenance = value["candidate_provenance"]
            if (
                not isinstance(raw_manifest, dict)
                or not isinstance(raw_signal_provenance, list)
                or not isinstance(raw_candidate_provenance, list)
            ):
                raise ValueError("ValidatedCaptureResult v2 fields are invalid")
            evidence_manifest = CaptureEvidenceManifest.from_dict(raw_manifest)
            signal_provenance = tuple(
                SignalProvenance.from_dict(item)
                for item in raw_signal_provenance
            )
            candidate_provenance = tuple(
                CandidateProvenance.from_dict(item)
                for item in raw_candidate_provenance
            )
            _validate_v2_sidecars(
                inventory,
                typed_observations,
                evidence_manifest,
                signal_provenance,
                candidate_provenance,
            )
        if frozen is not None and (
            frozen.operation_id != operation_id
            or (
                version == 2
                and evidence_manifest != frozen.evidence_manifest
            )
        ):
            raise ValueError("ValidatedCaptureResult frozen input mismatch")
        reconstructed_extraction = {
            "candidates": [
                {
                    **_candidate_as_extraction_item(item),
                    **(
                        {}
                        if version == 1
                        else {
                            "source_signal_ordinal": candidate_provenance[
                                ordinal - 1
                            ].source_signal_ordinal
                        }
                    ),
                }
                for ordinal, item in enumerate(typed_observations, start=1)
            ]
        }
        if extraction_sha256 != _sha256(reconstructed_extraction):
            raise ValueError(
                "ValidatedCaptureResult extraction digest mismatch"
            )
        core = (
            _result_v1_core(
                operation_id,
                inventory,
                inventory_sha256,
                extraction_sha256,
                typed_observations,
            )
            if version == 1
            else _result_v2_core(
                operation_id,
                inventory,
                inventory_sha256,
                extraction_sha256,
                typed_observations,
                evidence_manifest,
                signal_provenance,
                candidate_provenance,
            )
        )
        if result_digest != _sha256(core):
            raise ValueError("ValidatedCaptureResult digest mismatch")
        return cls(
            result_version=version,
            operation_id=operation_id,
            inventory=inventory,
            inventory_sha256=inventory_sha256,
            extraction_sha256=extraction_sha256,
            observations=typed_observations,
            evidence_manifest=evidence_manifest,
            signal_provenance=signal_provenance,
            candidate_provenance=candidate_provenance,
            result_digest=result_digest,
        )


def _result_v1_core(
    operation_id: str,
    inventory: InventoryResult,
    inventory_sha256: str,
    extraction_sha256: str,
    observations: tuple[Candidate, ...],
) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "inventory": inventory.to_dict(),
        "inventory_sha256": inventory_sha256,
        "extraction_sha256": extraction_sha256,
        "observations": [observation.to_dict() for observation in observations],
    }


def _result_v2_core(
    operation_id: str,
    inventory: InventoryResult,
    inventory_sha256: str,
    extraction_sha256: str,
    observations: tuple[Candidate, ...],
    evidence_manifest: CaptureEvidenceManifest | None,
    signal_provenance: tuple[SignalProvenance, ...],
    candidate_provenance: tuple[CandidateProvenance, ...],
) -> dict[str, object]:
    if evidence_manifest is None:
        raise ValueError("ValidatedCaptureResult v2 manifest is missing")
    return {
        "result_version": 2,
        **_result_v1_core(
            operation_id,
            inventory,
            inventory_sha256,
            extraction_sha256,
            observations,
        ),
        "evidence_manifest": evidence_manifest.to_dict(),
        "signal_provenance": [item.to_dict() for item in signal_provenance],
        "candidate_provenance": [item.to_dict() for item in candidate_provenance],
    }


def _validate_v2_sidecars(
    inventory: InventoryResult,
    observations: tuple[Candidate, ...],
    manifest: CaptureEvidenceManifest,
    signal_provenance: tuple[SignalProvenance, ...],
    candidate_provenance: tuple[CandidateProvenance, ...],
) -> None:
    if (
        len(signal_provenance) != len(inventory.signals)
        or tuple(item.signal_ordinal for item in signal_provenance)
        != tuple(range(1, len(inventory.signals) + 1))
        or len(candidate_provenance) != len(observations)
        or tuple(item.candidate_id for item in candidate_provenance)
        != tuple(item.candidate_id for item in observations)
    ):
        raise ValueError("ValidatedCaptureResult v2 sidecars are invalid")
    _, expected_signal_provenance = validate_inventory_v5(
        {
            "signals": [
                {
                    **signal.to_dict(),
                    "signal_ordinal": sidecar.signal_ordinal,
                    "evidence_receipt_ids": list(
                        sidecar.evidence_receipt_ids
                    ),
                }
                for signal, sidecar in zip(
                    inventory.signals, signal_provenance, strict=True
                )
            ],
            "coverage": inventory.coverage.to_dict(),
        },
        manifest,
    )
    if signal_provenance != expected_signal_provenance:
        raise ValueError("ValidatedCaptureResult v2 signal dispositions mismatch")
    by_ordinal = {item.signal_ordinal: item for item in signal_provenance}
    receipt_positions = {
        anchor.receipt_id: ordinal
        for ordinal, anchor in enumerate(manifest.anchors)
    }
    anchors_by_receipt = {
        anchor.receipt_id: anchor for anchor in manifest.anchors
    }
    for item in signal_provenance:
        if (
            any(receipt not in receipt_positions for receipt in item.evidence_receipt_ids)
            or tuple(
                sorted(item.evidence_receipt_ids, key=receipt_positions.__getitem__)
            )
            != item.evidence_receipt_ids
            or item.active_reference_set_digests
            != tuple(
                dict.fromkeys(
                    anchor.active_reference_set_digest
                    for receipt in item.evidence_receipt_ids
                    if (
                        anchor := anchors_by_receipt[receipt]
                    ).active_reference_set_digest
                    is not None
                )
            )
        ):
            raise ValueError("ValidatedCaptureResult v2 signal provenance mismatch")
    source_ordinals = tuple(
        item.source_signal_ordinal for item in candidate_provenance
    )
    if len(set(source_ordinals)) != len(source_ordinals):
        raise ValueError("ValidatedCaptureResult v2 source signals repeat")
    for item in candidate_provenance:
        source = by_ordinal.get(item.source_signal_ordinal)
        if (
            source is None
            or source.disposition != "candidate_eligible"
            or item.manifest_digest != manifest.manifest_digest
            or item.evidence_receipt_ids != source.evidence_receipt_ids
            or item.active_reference_set_digests
            != source.active_reference_set_digests
            or item.reference_decision_ids
        ):
            raise ValueError("ValidatedCaptureResult v2 provenance mismatch")


@dataclass(frozen=True)
class CaptureOperation:
    operation_id: str
    frozen: FrozenCaptureInput
    frozen_digest: str
    status: CaptureOperationStatus
    active_generation: int
    winner_generation: int | None
    committed_result_digest: str | None
    failure_code: str | None

    def __post_init__(self) -> None:
        if self.operation_id != self.frozen.operation_id:
            raise ValueError("CaptureOperation frozen identity mismatch")
        if self.frozen_digest != self.frozen.frozen_digest:
            raise ValueError("CaptureOperation frozen digest mismatch")
        if self.status not in _OPERATION_STATUSES:
            raise ValueError("CaptureOperation status is invalid")
        if (
            not isinstance(self.active_generation, int)
            or isinstance(self.active_generation, bool)
            or self.active_generation < 0
        ):
            raise ValueError("CaptureOperation active_generation is invalid")
        if self.winner_generation is not None and (
            not isinstance(self.winner_generation, int)
            or isinstance(self.winner_generation, bool)
            or self.winner_generation < 1
        ):
            raise ValueError("CaptureOperation winner_generation is invalid")
        if self.committed_result_digest is not None:
            _digest(
                self.committed_result_digest,
                "committed_result_digest",
            )
        if self.status == "committed":
            if (
                self.winner_generation is None
                or self.committed_result_digest is None
                or self.failure_code is not None
            ):
                raise ValueError("Committed CaptureOperation shape is invalid")
        elif self.status == "failed_terminal":
            _nonempty(self.failure_code, "failure_code")
            if (
                self.winner_generation is not None
                or self.committed_result_digest is not None
            ):
                raise ValueError(
                    "Terminal CaptureOperation shape is invalid"
                )
        elif (
            self.winner_generation is not None
            or self.committed_result_digest is not None
            or self.failure_code is not None
        ):
            raise ValueError("Open CaptureOperation shape is invalid")


@dataclass(frozen=True)
class ExecutionAttempt:
    attempt_id: str
    operation_id: str
    generation: int
    state: AttemptState
    thread_id: str | None
    inventory_turn_id: str | None
    extraction_turn_id: str | None
    failure_code: str | None
    validated_result_digest: str | None
    archive_state: ArchiveState
    started_at: str
    finished_at: str | None

    def __post_init__(self) -> None:
        _nonempty(self.attempt_id, "attempt_id")
        if _CAPTURE_ID.fullmatch(self.operation_id) is None:
            raise ValueError("ExecutionAttempt operation_id is invalid")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 1
        ):
            raise ValueError("ExecutionAttempt generation is invalid")
        if self.state not in _ATTEMPT_STATES:
            raise ValueError("ExecutionAttempt state is invalid")
        if self.archive_state not in _ARCHIVE_STATES:
            raise ValueError("ExecutionAttempt archive_state is invalid")
        _nonempty(self.started_at, "started_at")
        for field_name in (
            "thread_id",
            "inventory_turn_id",
            "extraction_turn_id",
            "failure_code",
            "finished_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _nonempty(value, field_name)
        if self.validated_result_digest is not None:
            _digest(
                self.validated_result_digest,
                "validated_result_digest",
            )
        if self.thread_id is None and self.archive_state != "not_applicable":
            raise ValueError("Unknown attempt Thread cannot require archive")
        if self.thread_id is not None and self.archive_state == "not_applicable":
            raise ValueError("Known attempt Thread must track archive state")


@dataclass(frozen=True)
class CaptureCommit:
    operation: CaptureOperation
    attempt: ExecutionAttempt
    result: ValidatedCaptureResult | None
