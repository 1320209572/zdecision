"""Strict private values for user Review batches."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from zdecision.capture.models import CandidateContent
from zdecision.ids import review_batch_id, review_item_id


ReviewAction = Literal["accept", "edit_accept", "reject", "skip"]

_ACTIONS = frozenset(("accept", "edit_accept", "reject", "skip"))
_ACCEPTED_ACTIONS = frozenset(("accept", "edit_accept"))
_CAPTURE_ID = re.compile(r"^cap_[0-9a-f]{32}$")
_CANDIDATE_ID = re.compile(
    r"^cand_[0-9a-f]{32}_(?:0[1-9]|1[0-9]|20)$"
)
_REVIEW_BATCH_ID = re.compile(r"^rvb_[0-9a-f]{32}$")
_REVIEW_ID = re.compile(r"^rvi_[0-9a-f]{32}$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


def _require_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    object_name: str,
) -> None:
    if not isinstance(value, Mapping) or frozenset(value) != expected:
        raise ValueError(f"{object_name} has invalid fields")


def _nonempty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _validated_content(value: object) -> CandidateContent:
    if not isinstance(value, Mapping):
        raise ValueError("Review content must be an object")
    return CandidateContent.from_dict(value)


def _candidate_id(value: object) -> str:
    candidate_id = _nonempty_string(value, "Review Candidate id")
    if _CANDIDATE_ID.fullmatch(candidate_id) is None:
        raise ValueError("Review Candidate id is invalid")
    return candidate_id


def _validate_utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("recorded_at must be a UTC RFC 3339 timestamp")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ValueError("recorded_at must be a UTC RFC 3339 timestamp") from None
    return value


@dataclass(frozen=True)
class ApprovalRef:
    actor: Literal["user"]
    thread_id: str
    turn_id: str
    recorded_at: str

    def __post_init__(self) -> None:
        if self.actor != "user":
            raise ValueError("Approval actor must be user")
        _nonempty_string(self.thread_id, "Approval thread_id")
        _nonempty_string(self.turn_id, "Approval turn_id")
        _validate_utc_timestamp(self.recorded_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ApprovalRef:
        _require_fields(
            value,
            frozenset(("actor", "thread_id", "turn_id", "recorded_at")),
            "ApprovalRef",
        )
        actor = value["actor"]
        if actor != "user":
            raise ValueError("Approval actor must be user")
        return cls(
            actor="user",
            thread_id=_nonempty_string(value["thread_id"], "Approval thread_id"),
            turn_id=_nonempty_string(value["turn_id"], "Approval turn_id"),
            recorded_at=_validate_utc_timestamp(value["recorded_at"]),
        )


@dataclass(frozen=True)
class ReviewSelection:
    candidate_id: str
    action: ReviewAction
    content: CandidateContent | None = None

    def __post_init__(self) -> None:
        _candidate_id(self.candidate_id)
        if self.action not in _ACTIONS:
            raise ValueError("Review action is invalid")
        if self.action == "edit_accept":
            if not isinstance(self.content, CandidateContent):
                raise ValueError("edit_accept requires complete Candidate content")
            CandidateContent.from_dict(self.content.to_dict())
        elif self.content is not None:
            raise ValueError("Only edit_accept may supply Review content")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "candidate_id": self.candidate_id,
            "action": self.action,
        }
        if self.content is not None:
            value["content"] = self.content.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ReviewSelection:
        if not isinstance(value, Mapping):
            raise ValueError("ReviewSelection must be an object")
        action = value.get("action")
        expected = (
            frozenset(("candidate_id", "action", "content"))
            if action == "edit_accept"
            else frozenset(("candidate_id", "action"))
        )
        _require_fields(value, expected, "ReviewSelection")
        if action not in _ACTIONS:
            raise ValueError("Review action is invalid")
        content = (
            _validated_content(value["content"])
            if action == "edit_accept"
            else None
        )
        return cls(
            candidate_id=_candidate_id(value["candidate_id"]),
            action=action,
            content=content,
        )


@dataclass(frozen=True)
class ReviewItem:
    review_id: str
    candidate_id: str
    action: ReviewAction
    content: CandidateContent | None

    def __post_init__(self) -> None:
        if not isinstance(self.review_id, str) or _REVIEW_ID.fullmatch(
            self.review_id
        ) is None:
            raise ValueError("Review item id is invalid")
        _candidate_id(self.candidate_id)
        if self.action not in _ACTIONS:
            raise ValueError("Review action is invalid")
        if self.action in _ACCEPTED_ACTIONS:
            if not isinstance(self.content, CandidateContent):
                raise ValueError("Accepted Review item requires frozen content")
            CandidateContent.from_dict(self.content.to_dict())
        elif self.content is not None:
            raise ValueError("Rejected or skipped Review item cannot have content")

    def identity_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "action": self.action,
            "effective_content": (
                self.content.to_dict() if self.content is not None else None
            ),
        }

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "review_id": self.review_id,
            "candidate_id": self.candidate_id,
            "action": self.action,
        }
        if self.content is not None:
            value["content"] = self.content.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ReviewItem:
        if not isinstance(value, Mapping):
            raise ValueError("ReviewItem must be an object")
        action = value.get("action")
        expected = (
            frozenset(("review_id", "candidate_id", "action", "content"))
            if action in _ACCEPTED_ACTIONS
            else frozenset(("review_id", "candidate_id", "action"))
        )
        _require_fields(value, expected, "ReviewItem")
        if action not in _ACTIONS:
            raise ValueError("Review action is invalid")
        content = (
            _validated_content(value["content"])
            if action in _ACCEPTED_ACTIONS
            else None
        )
        review_id = value["review_id"]
        candidate_id = value["candidate_id"]
        if not isinstance(review_id, str) or not isinstance(candidate_id, str):
            raise ValueError("Review item ids must be strings")
        return cls(
            review_id=review_id,
            candidate_id=candidate_id,
            action=action,
            content=content,
        )


@dataclass(frozen=True)
class ReviewBatch:
    review_batch_id: str
    capture_id: str
    sequence: int
    approval: ApprovalRef
    items: tuple[ReviewItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.review_batch_id, str) or _REVIEW_BATCH_ID.fullmatch(
            self.review_batch_id
        ) is None:
            raise ValueError("Review batch id is invalid")
        if not isinstance(self.capture_id, str) or _CAPTURE_ID.fullmatch(
            self.capture_id
        ) is None:
            raise ValueError("Review Capture id is invalid")
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 1
        ):
            raise ValueError("Review sequence must be a positive integer")
        if not isinstance(self.approval, ApprovalRef):
            raise ValueError("Review approval is invalid")
        if not isinstance(self.items, tuple) or not 1 <= len(self.items) <= 20:
            raise ValueError("Review batch must contain between 1 and 20 items")
        if any(not isinstance(item, ReviewItem) for item in self.items):
            raise ValueError("Review batch items are invalid")
        candidate_ids = [item.candidate_id for item in self.items]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Review batch contains a duplicate Candidate")
        expected_batch_id = review_batch_id(
            self.capture_id,
            tuple(item.identity_dict() for item in self.items),
            self.approval.thread_id,
            self.approval.turn_id,
        )
        if self.review_batch_id != expected_batch_id:
            raise ValueError("Review batch identity mismatch")
        for item in self.items:
            if item.review_id != review_item_id(
                self.review_batch_id, item.candidate_id
            ):
                raise ValueError("Review item identity mismatch")

    def to_dict(self) -> dict[str, object]:
        return {
            "review_batch_id": self.review_batch_id,
            "capture_id": self.capture_id,
            "sequence": self.sequence,
            "approval": self.approval.to_dict(),
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ReviewBatch:
        _require_fields(
            value,
            frozenset(
                ("review_batch_id", "capture_id", "sequence", "approval", "items")
            ),
            "ReviewBatch",
        )
        approval = value["approval"]
        items = value["items"]
        if not isinstance(approval, Mapping) or not isinstance(items, list):
            raise ValueError("Review batch approval and items are invalid")
        batch_id = value["review_batch_id"]
        capture_id = value["capture_id"]
        sequence = value["sequence"]
        if not isinstance(batch_id, str) or not isinstance(capture_id, str):
            raise ValueError("Review batch ids must be strings")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ValueError("Review sequence must be an integer")
        parsed_items: list[ReviewItem] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError("Review batch item must be an object")
            parsed_items.append(ReviewItem.from_dict(item))
        return cls(
            review_batch_id=batch_id,
            capture_id=capture_id,
            sequence=sequence,
            approval=ApprovalRef.from_dict(approval),
            items=tuple(parsed_items),
        )
