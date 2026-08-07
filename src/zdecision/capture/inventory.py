"""Typed private Stage 1 inventory contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from zdecision.capture.provenance import (
    CaptureEvidenceManifest,
    SignalDisposition,
    SignalProvenance,
)
from zdecision.jsonio import canonical_json_bytes


SignalStatus = Literal["current_confirmed", "unresolved", "superseded"]
ConfirmationBasis = Literal[
    "explicit_user_confirmation",
    "explicit_user_direction",
    "adopted_decision_contract",
    "uncertain",
]
SignalConfidence = Literal["high", "medium", "low"]

_MAX_SIGNALS = 100
_MAX_INVENTORY_BYTES = 256 * 1024
_INVENTORY_FIELDS = frozenset(("signals", "coverage"))
_SIGNAL_FIELDS = frozenset(
    (
        "topic",
        "rule",
        "future_effect",
        "scope",
        "status",
        "confirmation_basis",
        "confidence",
    )
)
_V5_SIGNAL_FIELDS = _SIGNAL_FIELDS | frozenset(
    ("signal_ordinal", "evidence_receipt_ids")
)
_COVERAGE_FIELDS = frozenset(("reviewed_retained_context", "known_gaps"))
_SIGNAL_STATUSES = frozenset(("current_confirmed", "unresolved", "superseded"))
_CONFIRMATION_BASES = frozenset(
    (
        "explicit_user_confirmation",
        "explicit_user_direction",
        "adopted_decision_contract",
        "uncertain",
    )
)
_SIGNAL_CONFIDENCES = frozenset(("high", "medium", "low"))


class InventoryValidationError(ValueError):
    """Raised when model-authored Stage 1 output violates its contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class DecisionSignal:
    topic: str
    rule: str
    future_effect: str
    scope: str
    status: SignalStatus
    confirmation_basis: ConfirmationBasis
    confidence: SignalConfidence

    def to_dict(self) -> dict[str, object]:
        return {
            "topic": self.topic,
            "rule": self.rule,
            "future_effect": self.future_effect,
            "scope": self.scope,
            "status": self.status,
            "confirmation_basis": self.confirmation_basis,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class InventoryCoverage:
    reviewed_retained_context: Literal["earliest_to_latest"]
    known_gaps: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "reviewed_retained_context": self.reviewed_retained_context,
            "known_gaps": list(self.known_gaps),
        }


@dataclass(frozen=True)
class InventoryResult:
    signals: tuple[DecisionSignal, ...]
    coverage: InventoryCoverage

    def to_dict(self) -> dict[str, object]:
        return {
            "signals": [signal.to_dict() for signal in self.signals],
            "coverage": self.coverage.to_dict(),
        }


def _invalid() -> InventoryValidationError:
    return InventoryValidationError(
        "invalid_inventory",
        "Inventory output does not match the required schema",
    )


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_inventory(value: object) -> InventoryResult:
    """Validate one complete model-authored inventory without echoing its data."""

    if isinstance(value, Mapping):
        raw_signals = value.get("signals")
        if isinstance(raw_signals, list) and len(raw_signals) > _MAX_SIGNALS:
            raise InventoryValidationError(
                "inventory_signal_limit_exceeded",
                "Inventory contains more than 100 signals",
            )

    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError):
        raise _invalid() from None
    if len(encoded) > _MAX_INVENTORY_BYTES:
        raise InventoryValidationError(
            "inventory_output_too_large",
            "Inventory output exceeds 256 KiB",
        )

    if not isinstance(value, Mapping) or frozenset(value) != _INVENTORY_FIELDS:
        raise _invalid()
    raw_signals = value["signals"]
    coverage = value["coverage"]
    if not isinstance(raw_signals, list) or not isinstance(coverage, Mapping):
        raise _invalid()
    if frozenset(coverage) != _COVERAGE_FIELDS:
        raise _invalid()
    if coverage["reviewed_retained_context"] != "earliest_to_latest":
        raise _invalid()
    known_gaps = coverage["known_gaps"]
    if not isinstance(known_gaps, list) or any(
        not isinstance(gap, str) for gap in known_gaps
    ):
        raise _invalid()

    signals: list[DecisionSignal] = []
    for raw_signal in raw_signals:
        if not isinstance(raw_signal, Mapping):
            raise _invalid()
        if frozenset(raw_signal) != _SIGNAL_FIELDS:
            raise _invalid()
        if any(
            not _nonempty_string(raw_signal[field])
            for field in ("topic", "rule", "future_effect", "scope")
        ):
            raise _invalid()
        status = raw_signal["status"]
        confirmation_basis = raw_signal["confirmation_basis"]
        confidence = raw_signal["confidence"]
        if (
            not isinstance(status, str)
            or status not in _SIGNAL_STATUSES
            or not isinstance(confirmation_basis, str)
            or confirmation_basis not in _CONFIRMATION_BASES
            or not isinstance(confidence, str)
            or confidence not in _SIGNAL_CONFIDENCES
        ):
            raise _invalid()
        signals.append(
            DecisionSignal(
                topic=raw_signal["topic"],
                rule=raw_signal["rule"],
                future_effect=raw_signal["future_effect"],
                scope=raw_signal["scope"],
                status=status,
                confirmation_basis=confirmation_basis,
                confidence=confidence,
            )
        )

    return InventoryResult(
        signals=tuple(signals),
        coverage=InventoryCoverage(
            reviewed_retained_context="earliest_to_latest",
            known_gaps=tuple(known_gaps),
        ),
    )


def validate_inventory_v5(
    value: object,
    manifest: CaptureEvidenceManifest,
) -> tuple[InventoryResult, tuple[SignalProvenance, ...]]:
    """Validate v5 receipt selections and derive host-owned dispositions."""

    if not isinstance(manifest, CaptureEvidenceManifest):
        raise TypeError("manifest must be a CaptureEvidenceManifest")
    if isinstance(value, Mapping):
        raw_signals = value.get("signals")
        if isinstance(raw_signals, list) and len(raw_signals) > _MAX_SIGNALS:
            raise InventoryValidationError(
                "inventory_signal_limit_exceeded",
                "Inventory contains more than 100 signals",
            )
    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError):
        raise _invalid() from None
    if len(encoded) > _MAX_INVENTORY_BYTES:
        raise InventoryValidationError(
            "inventory_output_too_large",
            "Inventory output exceeds 256 KiB",
        )
    if not isinstance(value, Mapping) or frozenset(value) != _INVENTORY_FIELDS:
        raise _invalid()
    raw_signals = value["signals"]
    if not isinstance(raw_signals, list):
        raise _invalid()

    receipt_positions = {
        anchor.receipt_id: position
        for position, anchor in enumerate(manifest.anchors)
    }
    anchors_by_receipt = {anchor.receipt_id: anchor for anchor in manifest.anchors}
    legacy_signals: list[dict[str, object]] = []
    selected_receipts: list[tuple[str, ...]] = []
    for ordinal, raw_signal in enumerate(raw_signals, start=1):
        if (
            not isinstance(raw_signal, Mapping)
            or frozenset(raw_signal) != _V5_SIGNAL_FIELDS
            or raw_signal["signal_ordinal"] != ordinal
            or isinstance(raw_signal["signal_ordinal"], bool)
        ):
            raise _invalid()
        raw_receipts = raw_signal["evidence_receipt_ids"]
        if (
            not isinstance(raw_receipts, list)
            or any(not isinstance(receipt, str) for receipt in raw_receipts)
            or len(set(raw_receipts)) != len(raw_receipts)
            or any(receipt not in receipt_positions for receipt in raw_receipts)
        ):
            raise _invalid()
        receipts = tuple(raw_receipts)
        if tuple(sorted(receipts, key=receipt_positions.__getitem__)) != receipts:
            raise _invalid()
        if (
            raw_signal["status"] == "current_confirmed"
            and not receipts
        ):
            raise _invalid()
        legacy_signals.append(
            {field: raw_signal[field] for field in _SIGNAL_FIELDS}
        )
        selected_receipts.append(receipts)

    inventory = validate_inventory(
        {"signals": legacy_signals, "coverage": value["coverage"]}
    )
    sidecars: list[SignalProvenance] = []
    for signal, receipts in zip(inventory.signals, selected_receipts, strict=True):
        reference_digests = tuple(
            dict.fromkeys(
                anchor.active_reference_set_digest
                for receipt in receipts
                if (anchor := anchors_by_receipt[receipt]).active_reference_set_digest
                is not None
            )
        )
        disposition = _signal_disposition(signal, receipts, reference_digests)
        sidecars.append(
            SignalProvenance.create(
                signal_ordinal=len(sidecars) + 1,
                evidence_receipt_ids=receipts,
                active_reference_set_digests=reference_digests,
                disposition=disposition,
            )
        )
    return inventory, tuple(sidecars)


def _signal_disposition(
    signal: DecisionSignal,
    receipts: tuple[str, ...],
    reference_digests: tuple[str, ...],
) -> SignalDisposition:
    if not receipts:
        return "excluded_unverified"
    if signal.status != "current_confirmed" or signal.confidence != "high":
        return "needs_evidence"
    if signal.confirmation_basis == "uncertain":
        return "excluded_unverified"
    if (
        reference_digests
        and signal.confirmation_basis
        in ("explicit_user_confirmation", "adopted_decision_contract")
    ):
        return "needs_evidence"
    return "candidate_eligible"
