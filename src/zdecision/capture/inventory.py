"""Typed private Stage 1 inventory contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

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
