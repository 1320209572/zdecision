"""Strict host-owned provenance for Capture prompt anchors."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal, cast

from zdecision.jsonio import canonical_json_bytes


EvidenceKind = Literal["hook_observed_user_prompt_anchor"]

_EVENT_ID = re.compile(r"^evt_[0-9a-f]{32}$")
_RECEIPT_ID = re.compile(r"^rcpt_[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_ANCHORS = 100
_EVIDENCE_KIND: EvidenceKind = "hook_observed_user_prompt_anchor"


def prompt_anchor_receipt_id(hook_event_id: str) -> str:
    """Return an opaque, deterministic receipt for one local Hook event."""

    _require_event_id(hook_event_id, "hook_event_id")
    digest = hashlib.sha256(
        canonical_json_bytes({"kind": _EVIDENCE_KIND, "event_id": hook_event_id})
    ).hexdigest()
    return f"rcpt_{digest}"


@dataclass(frozen=True)
class PromptAnchor:
    receipt_id: str
    hook_event_id: str
    turn_id: str
    anchor_ordinal: int
    active_reference_set_digest: str | None

    def __post_init__(self) -> None:
        _require_receipt_id(self.receipt_id)
        _require_event_id(self.hook_event_id, "hook_event_id")
        _require_identifier(self.turn_id, "turn_id")
        if (
            not isinstance(self.anchor_ordinal, int)
            or isinstance(self.anchor_ordinal, bool)
            or not 1 <= self.anchor_ordinal <= _MAX_ANCHORS
        ):
            raise ValueError("anchor_ordinal is invalid")
        _require_optional_digest(
            self.active_reference_set_digest, "active_reference_set_digest"
        )
        if self.receipt_id != prompt_anchor_receipt_id(self.hook_event_id):
            raise ValueError("receipt_id does not match hook_event_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "hook_event_id": self.hook_event_id,
            "turn_id": self.turn_id,
            "anchor_ordinal": self.anchor_ordinal,
            "active_reference_set_digest": self.active_reference_set_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> "PromptAnchor":
        fields = {
            "receipt_id",
            "hook_event_id",
            "turn_id",
            "anchor_ordinal",
            "active_reference_set_digest",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("prompt anchor fields are invalid")
        return cls(
            receipt_id=cast(str, value["receipt_id"]),
            hook_event_id=cast(str, value["hook_event_id"]),
            turn_id=cast(str, value["turn_id"]),
            anchor_ordinal=cast(int, value["anchor_ordinal"]),
            active_reference_set_digest=cast(
                str | None, value["active_reference_set_digest"]
            ),
        )


@dataclass(frozen=True)
class CaptureEvidenceManifest:
    version: Literal[1]
    kind: EvidenceKind
    source_session_id: str
    previous_handled_event_id: str | None
    upper_stop_event_id: str
    anchors: tuple[PromptAnchor, ...]
    manifest_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version != 1
        ):
            raise ValueError("manifest version is invalid")
        if self.kind != _EVIDENCE_KIND:
            raise ValueError("manifest kind is invalid")
        _require_identifier(self.source_session_id, "source_session_id")
        _require_optional_event_id(
            self.previous_handled_event_id, "previous_handled_event_id"
        )
        _require_event_id(self.upper_stop_event_id, "upper_stop_event_id")
        _require_digest(self.manifest_digest, "manifest_digest")
        _validate_anchors(self.anchors)
        if self.manifest_digest != _manifest_digest(
            source_session_id=self.source_session_id,
            previous_handled_event_id=self.previous_handled_event_id,
            upper_stop_event_id=self.upper_stop_event_id,
            anchors=self.anchors,
        ):
            raise ValueError("manifest_digest does not match manifest")

    @classmethod
    def create(
        cls,
        *,
        source_session_id: str,
        previous_handled_event_id: str | None,
        upper_stop_event_id: str,
        anchors: tuple[PromptAnchor, ...],
    ) -> "CaptureEvidenceManifest":
        payload = {
            "version": 1,
            "kind": _EVIDENCE_KIND,
            "source_session_id": source_session_id,
            "previous_handled_event_id": previous_handled_event_id,
            "upper_stop_event_id": upper_stop_event_id,
            "anchors": [anchor.to_dict() for anchor in anchors],
        }
        return cls(
            version=1,
            kind=_EVIDENCE_KIND,
            source_session_id=source_session_id,
            previous_handled_event_id=previous_handled_event_id,
            upper_stop_event_id=upper_stop_event_id,
            anchors=anchors,
            manifest_digest=hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "kind": self.kind,
            "source_session_id": self.source_session_id,
            "previous_handled_event_id": self.previous_handled_event_id,
            "upper_stop_event_id": self.upper_stop_event_id,
            "anchors": [anchor.to_dict() for anchor in self.anchors],
            "manifest_digest": self.manifest_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> "CaptureEvidenceManifest":
        fields = {
            "version",
            "kind",
            "source_session_id",
            "previous_handled_event_id",
            "upper_stop_event_id",
            "anchors",
            "manifest_digest",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("manifest fields are invalid")
        anchors_value = value["anchors"]
        if not isinstance(anchors_value, list):
            raise ValueError("manifest anchors are invalid")
        manifest = cls(
            version=cast(Literal[1], value["version"]),
            kind=cast(EvidenceKind, value["kind"]),
            source_session_id=cast(str, value["source_session_id"]),
            previous_handled_event_id=cast(
                str | None, value["previous_handled_event_id"]
            ),
            upper_stop_event_id=cast(str, value["upper_stop_event_id"]),
            anchors=tuple(PromptAnchor.from_dict(anchor) for anchor in anchors_value),
            manifest_digest=cast(str, value["manifest_digest"]),
        )
        if manifest.to_dict() != value:
            raise ValueError("manifest is not canonical")
        return manifest


def _manifest_digest(
    *,
    source_session_id: str,
    previous_handled_event_id: str | None,
    upper_stop_event_id: str,
    anchors: tuple[PromptAnchor, ...],
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "version": 1,
                "kind": _EVIDENCE_KIND,
                "source_session_id": source_session_id,
                "previous_handled_event_id": previous_handled_event_id,
                "upper_stop_event_id": upper_stop_event_id,
                "anchors": [anchor.to_dict() for anchor in anchors],
            }
        )
    ).hexdigest()


def _validate_anchors(anchors: tuple[PromptAnchor, ...]) -> None:
    if not isinstance(anchors, tuple) or not 1 <= len(anchors) <= _MAX_ANCHORS:
        raise ValueError("manifest anchors are invalid")
    if any(not isinstance(anchor, PromptAnchor) for anchor in anchors):
        raise ValueError("manifest anchors are invalid")
    if tuple(anchor.anchor_ordinal for anchor in anchors) != tuple(
        range(1, len(anchors) + 1)
    ):
        raise ValueError("anchor ordinals are not canonical")
    if len({anchor.receipt_id for anchor in anchors}) != len(anchors):
        raise ValueError("manifest receipts are not unique")
    if len({anchor.hook_event_id for anchor in anchors}) != len(anchors):
        raise ValueError("manifest Hook events are not unique")
    if len({anchor.turn_id for anchor in anchors}) != len(anchors):
        raise ValueError("manifest Turns are not unique")


def _require_identifier(value: object, name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _require_event_id(value: object, name: str) -> None:
    if not isinstance(value, str) or _EVENT_ID.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _require_optional_event_id(value: object, name: str) -> None:
    if value is not None:
        _require_event_id(value, name)


def _require_receipt_id(value: object) -> None:
    if not isinstance(value, str) or _RECEIPT_ID.fullmatch(value) is None:
        raise ValueError("receipt_id is invalid")


def _require_digest(value: object, name: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")


def _require_optional_digest(value: object, name: str) -> None:
    if value is not None:
        _require_digest(value, name)
