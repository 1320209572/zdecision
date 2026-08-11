"""Canonical, bounded values for the formal Recall handoff."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias

from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.session import RecallIntent
from zdecision.registry.models import DecisionRevision


RECALL_HANDOFF_PROTOCOL = "recall-handoff-v1"
ApplicationDisposition = Literal[
    "applicable", "not_applicable", "conflicting", "uncertain"
]

_MAX_SHORTLIST_ITEMS = 8
_MAX_DECISION_BYTES = 10_000
_MAX_REASON_CHARACTERS = 2_000
_MAX_APPLICATION_ITEMS = 8
_APPLICATION_DISPOSITIONS = frozenset(
    ("applicable", "not_applicable", "conflicting", "uncertain")
)


def _exact_mapping(
    value: object, fields: frozenset[str], object_name: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        raise ValueError(f"{object_name} fields are invalid")
    return value


def _text(value: object, field_name: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is invalid")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{field_name} is invalid")
    return value


def _digest(value: object, field_name: str) -> str:
    text = _text(value, field_name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field_name} is invalid")
    return text


def _nonnegative_integer(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} is invalid")
    return value


def _delivery_id(value: object) -> str:
    text = _text(value, "delivery_id")
    if not text.startswith("delivery_") or len(text) != 41:
        raise ValueError("delivery_id is invalid")
    if any(character not in "0123456789abcdef" for character in text[9:]):
        raise ValueError("delivery_id is invalid")
    return text


@dataclass(frozen=True)
class RecallPreflightReady:
    repository_id: str
    repository_display_name: str
    intent: RecallIntent
    target_decision_space_ids: tuple[str, ...]
    target_display_names: tuple[str, ...]
    catalog_digest: str
    generation: int
    generation_digest: str
    retrieval_profile_digest: str
    index_generation: int
    freshness: Literal["ready", "degraded"]
    expires_at: str

    def __post_init__(self) -> None:
        _text(self.repository_id, "repository_id")
        _text(self.repository_display_name, "repository_display_name")
        if not isinstance(self.intent, RecallIntent):
            raise ValueError("intent is invalid")
        if (
            not isinstance(self.target_decision_space_ids, tuple)
            or not 1 <= len(self.target_decision_space_ids) <= _MAX_SHORTLIST_ITEMS
            or any(not isinstance(item, str) or not item.strip() for item in self.target_decision_space_ids)
            or len(set(self.target_decision_space_ids)) != len(self.target_decision_space_ids)
        ):
            raise ValueError("target_decision_space_ids is invalid")
        if (
            not isinstance(self.target_display_names, tuple)
            or len(self.target_display_names) != len(self.target_decision_space_ids)
            or any(not isinstance(item, str) or not item.strip() for item in self.target_display_names)
        ):
            raise ValueError("target_display_names is invalid")
        _digest(self.catalog_digest, "catalog_digest")
        _nonnegative_integer(self.generation, "generation")
        _digest(self.generation_digest, "generation_digest")
        _digest(self.retrieval_profile_digest, "retrieval_profile_digest")
        _nonnegative_integer(self.index_generation, "index_generation")
        if self.freshness not in ("ready", "degraded"):
            raise ValueError("freshness is invalid")
        _text(self.expires_at, "expires_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "repository_display_name": self.repository_display_name,
            "intent": self.intent.to_dict(),
            "target_decision_space_ids": list(self.target_decision_space_ids),
            "target_display_names": list(self.target_display_names),
            "catalog_digest": self.catalog_digest,
            "generation": self.generation,
            "generation_digest": self.generation_digest,
            "retrieval_profile_digest": self.retrieval_profile_digest,
            "index_generation": self.index_generation,
            "freshness": self.freshness,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> "RecallPreflightReady":
        item = _exact_mapping(
            value,
            frozenset(
                (
                    "repository_id",
                    "repository_display_name",
                    "intent",
                    "target_decision_space_ids",
                    "target_display_names",
                    "catalog_digest",
                    "generation",
                    "generation_digest",
                    "retrieval_profile_digest",
                    "index_generation",
                    "freshness",
                    "expires_at",
                )
            ),
            "RecallPreflightReady",
        )
        target_ids = item["target_decision_space_ids"]
        target_names = item["target_display_names"]
        if not isinstance(target_ids, list) or not isinstance(target_names, list):
            raise ValueError("RecallPreflightReady targets are invalid")
        return cls(
            repository_id=_text(item["repository_id"], "repository_id"),
            repository_display_name=_text(
                item["repository_display_name"], "repository_display_name"
            ),
            intent=RecallIntent.from_dict(item["intent"]),
            target_decision_space_ids=tuple(
                _text(member, "target_decision_space_ids") for member in target_ids
            ),
            target_display_names=tuple(
                _text(member, "target_display_names") for member in target_names
            ),
            catalog_digest=_digest(item["catalog_digest"], "catalog_digest"),
            generation=_nonnegative_integer(item["generation"], "generation"),
            generation_digest=_digest(item["generation_digest"], "generation_digest"),
            retrieval_profile_digest=_digest(
                item["retrieval_profile_digest"], "retrieval_profile_digest"
            ),
            index_generation=_nonnegative_integer(
                item["index_generation"], "index_generation"
            ),
            freshness=item["freshness"],  # type: ignore[arg-type]
            expires_at=_text(item["expires_at"], "expires_at"),
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()


@dataclass(frozen=True)
class RecallPreflightClarification:
    code: str
    candidate_display_names: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.code, "code")
        if (
            not isinstance(self.candidate_display_names, tuple)
            or not 1 <= len(self.candidate_display_names) <= _MAX_SHORTLIST_ITEMS
            or any(
                not isinstance(name, str)
                or not name.strip()
                or len(name) > _MAX_REASON_CHARACTERS
                for name in self.candidate_display_names
            )
            or len(set(self.candidate_display_names))
            != len(self.candidate_display_names)
        ):
            raise ValueError("candidate_display_names is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "candidate_display_names": list(self.candidate_display_names),
        }

    @classmethod
    def from_dict(cls, value: object) -> "RecallPreflightClarification":
        item = _exact_mapping(
            value,
            frozenset(("code", "candidate_display_names")),
            "RecallPreflightClarification",
        )
        candidate_display_names = item["candidate_display_names"]
        if not isinstance(candidate_display_names, list):
            raise ValueError("candidate_display_names is invalid")
        return cls(
            code=_text(item["code"], "code"),
            candidate_display_names=tuple(candidate_display_names),
        )


@dataclass(frozen=True)
class RecallPreflightUnavailable:
    code: str

    def __post_init__(self) -> None:
        _text(self.code, "code")

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code}

    @classmethod
    def from_dict(cls, value: object) -> "RecallPreflightUnavailable":
        item = _exact_mapping(
            value,
            frozenset(("code",)),
            "RecallPreflightUnavailable",
        )
        return cls(code=_text(item["code"], "code"))


RecallPreflightResult: TypeAlias = (
    RecallPreflightReady | RecallPreflightClarification | RecallPreflightUnavailable
)


@dataclass(frozen=True)
class RecalledDecision:
    decision_space_id: str
    revision: DecisionRevision
    digest: str
    match_reason: str

    def __post_init__(self) -> None:
        _text(self.decision_space_id, "decision_space_id")
        _text(self.match_reason, "match_reason", maximum=_MAX_REASON_CHARACTERS)
        _digest(self.digest, "digest")

    @classmethod
    def create(
        cls,
        *,
        decision_space_id: str,
        revision: DecisionRevision,
        match_reason: str,
    ) -> "RecalledDecision":
        revision_digest = hashlib.sha256(
            canonical_json_bytes(revision.to_dict())
        ).hexdigest()
        return cls(
            decision_space_id=decision_space_id,
            revision=revision,
            digest=revision_digest,
            match_reason=match_reason,
        )

    def to_dict(self) -> dict[str, object]:
        if not isinstance(self.revision, DecisionRevision):
            raise ValueError("revision is invalid")
        return {
            "decision_space_id": self.decision_space_id,
            "formal_decision": self.revision.to_dict(),
            "digest": self.digest,
            "match_reason": self.match_reason,
        }

    @classmethod
    def from_dict(cls, value: object) -> "RecalledDecision":
        item = _exact_mapping(
            value,
            frozenset(("decision_space_id", "formal_decision", "digest", "match_reason")),
            "RecalledDecision",
        )
        if not isinstance(item["formal_decision"], Mapping):
            raise ValueError("formal_decision is invalid")
        return cls(
            decision_space_id=_text(item["decision_space_id"], "decision_space_id"),
            revision=DecisionRevision.from_dict(item["formal_decision"]),
            digest=_digest(item["digest"], "digest"),
            match_reason=_text(item["match_reason"], "match_reason", maximum=_MAX_REASON_CHARACTERS),
        )


@dataclass(frozen=True)
class RecallShortlist:
    preflight_digest: str
    items: tuple[RecalledDecision, ...]

    def __post_init__(self) -> None:
        _digest(self.preflight_digest, "preflight_digest")
        if (
            not isinstance(self.items, tuple)
            or len(self.items) > _MAX_SHORTLIST_ITEMS
            or any(not isinstance(item, RecalledDecision) for item in self.items)
        ):
            raise ValueError("shortlist items are invalid")
        for item in self.items:
            if not isinstance(item.revision, DecisionRevision):
                raise ValueError("revision is invalid")
            expected = hashlib.sha256(
                canonical_json_bytes(item.revision.to_dict())
            ).hexdigest()
            if item.digest != expected:
                raise ValueError("digest does not match revision")
        identities = tuple(
            (item.decision_space_id, item.revision.decision_id, item.revision.revision)
            for item in self.items
        )
        if len(set(identities)) != len(identities):
            raise ValueError("shortlist Decision tuples contain duplicates")
        if sum(len(canonical_json_bytes(item.revision.to_dict())) for item in self.items) > _MAX_DECISION_BYTES:
            raise ValueError("shortlist Decision bytes exceed 10,000")

    @classmethod
    def create(
        cls,
        *,
        preflight: RecallPreflightReady,
        items: tuple[RecalledDecision, ...],
        preflight_digest: str | None = None,
    ) -> "RecallShortlist":
        if not isinstance(preflight, RecallPreflightReady):
            raise ValueError("preflight is invalid")
        if preflight_digest is not None and preflight_digest != preflight.digest:
            raise ValueError("preflight_digest does not match preflight")
        target_ids = frozenset(preflight.target_decision_space_ids)
        if any(item.decision_space_id not in target_ids for item in items):
            raise ValueError("shortlist Decision space is invalid")
        return cls(preflight_digest=preflight.digest, items=items)

    def to_dict(self) -> dict[str, object]:
        return {
            "preflight_digest": self.preflight_digest,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, value: object) -> "RecallShortlist":
        item = _exact_mapping(
            value,
            frozenset(("preflight_digest", "items")),
            "RecallShortlist",
        )
        raw_items = item["items"]
        if not isinstance(raw_items, list):
            raise ValueError("shortlist items are invalid")
        return cls(
            preflight_digest=_digest(item["preflight_digest"], "preflight_digest"),
            items=tuple(RecalledDecision.from_dict(member) for member in raw_items),
        )


@dataclass(frozen=True)
class RecallApplicationItem:
    decision_id: str
    revision: int
    digest: str
    disposition: ApplicationDisposition
    reason: str

    def __post_init__(self) -> None:
        _text(self.decision_id, "decision_id")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise ValueError("revision is invalid")
        _digest(self.digest, "digest")
        if self.disposition not in _APPLICATION_DISPOSITIONS:
            raise ValueError("disposition is invalid")
        _text(self.reason, "reason", maximum=_MAX_REASON_CHARACTERS)

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "revision": self.revision,
            "digest": self.digest,
            "disposition": self.disposition,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: object) -> "RecallApplicationItem":
        item = _exact_mapping(
            value,
            frozenset(("decision_id", "revision", "digest", "disposition", "reason")),
            "RecallApplicationItem",
        )
        return cls(
            decision_id=_text(item["decision_id"], "decision_id"),
            revision=item["revision"],  # type: ignore[arg-type]
            digest=_digest(item["digest"], "digest"),
            disposition=item["disposition"],  # type: ignore[arg-type]
            reason=_text(item["reason"], "reason", maximum=_MAX_REASON_CHARACTERS),
        )


@dataclass(frozen=True)
class RecallApplicationSubmission:
    delivery_id: str
    items: tuple[RecallApplicationItem, ...]

    def __post_init__(self) -> None:
        _delivery_id(self.delivery_id)
        if (
            not isinstance(self.items, tuple)
            or len(self.items) > _MAX_APPLICATION_ITEMS
            or any(not isinstance(item, RecallApplicationItem) for item in self.items)
        ):
            raise ValueError("application items are invalid")
        identities = tuple((item.decision_id, item.revision, item.digest) for item in self.items)
        if len(set(identities)) != len(identities):
            raise ValueError("application items contain duplicates")

    def to_dict(self) -> dict[str, object]:
        return {
            "delivery_id": self.delivery_id,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, value: object) -> "RecallApplicationSubmission":
        item = _exact_mapping(
            value,
            frozenset(("delivery_id", "items")),
            "RecallApplicationSubmission",
        )
        raw_items = item["items"]
        if not isinstance(raw_items, list):
            raise ValueError("application items are invalid")
        return cls(
            delivery_id=_delivery_id(item["delivery_id"]),
            items=tuple(RecallApplicationItem.from_dict(member) for member in raw_items),
        )


def build_handoff_context(
    delivery_id: str,
    preflight: RecallPreflightReady,
    shortlist: RecallShortlist,
) -> str:
    """Return only the canonical, non-executable next-message context."""

    delivery_id = _delivery_id(delivery_id)
    if not isinstance(preflight, RecallPreflightReady):
        raise ValueError("preflight is invalid")
    if not isinstance(shortlist, RecallShortlist) or shortlist.preflight_digest != preflight.digest:
        raise ValueError("shortlist does not match preflight")
    payload = {
        "marker": "ZDECISION_RECALL_HANDOFF",
        "protocol": RECALL_HANDOFF_PROTOCOL,
        "delivery_id": delivery_id,
        "preflight_digest": preflight.digest,
        "intent": preflight.intent.to_dict(),
        "target_decision_spaces": [
            {"decision_space_id": identifier, "display_name": name}
            for identifier, name in zip(
                preflight.target_decision_space_ids, preflight.target_display_names,
                strict=True,
            )
        ],
        "catalog_digest": preflight.catalog_digest,
        "generation": preflight.generation,
        "generation_digest": preflight.generation_digest,
        "retrieval_profile_digest": preflight.retrieval_profile_digest,
        "index_generation": preflight.index_generation,
        "freshness": preflight.freshness,
        "expires_at": preflight.expires_at,
        "decisions": [item.to_dict() for item in shortlist.items],
    }
    return canonical_json_bytes(payload).decode("utf-8")
