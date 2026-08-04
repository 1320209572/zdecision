"""Strict immutable values persisted by the central Decision Web."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast

from zdecision.capture.models import CandidateContent
from zdecision.capture.reviews import ApprovalRef
from zdecision.central.auth import require_id
from zdecision.ids import (
    candidate_revision_id,
    canonical_product_name,
    central_publication_id,
    product_id as derive_product_id,
    publication_candidate_id,
    review_item_id,
)


ReviewAction = Literal["accept", "edit_accept", "reject", "skip"]
PublicationState = Literal["confirmed", "committed_pending_push", "completed"]
ActionKind = Literal["review", "preview", "publish", "resume"]

_ACTIONS = frozenset(("accept", "edit_accept", "reject", "skip"))
_PUBLICATION_STATES = frozenset(
    ("confirmed", "committed_pending_push", "completed")
)
_ACTION_KINDS = frozenset(("review", "preview", "publish", "resume"))
_FAMILY_ID = re.compile(r"^cfm_[0-9a-f]{32}$")
_REPOSITORY_ID = re.compile(r"^repo_[0-9a-f]{32}$")
_REVISION_ID = re.compile(r"^crv_[0-9a-f]{32}$")
_PRODUCT_ID = re.compile(r"^prod_[0-9a-f]{32}$")
_REVIEW_BATCH_ID = re.compile(r"^rvb_[0-9a-f]{32}$")
_REVIEW_ID = re.compile(r"^rvi_[0-9a-f]{32}$")
_CANDIDATE_ID = re.compile(r"^cand_[0-9a-f]{32}_01$")
_PREVIEW_ID = re.compile(r"^pub_[0-9a-f]{32}$")
_PUBLICATION_ID = re.compile(r"^plb_[0-9a-f]{32}$")
_WEB_ACTION_ID = re.compile(r"^web_action_[A-Za-z0-9-]{1,96}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_RECOVERY_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _require_fields(
    value: Mapping[str, object], expected: frozenset[str], name: str
) -> None:
    if not isinstance(value, Mapping) or frozenset(value) != expected:
        raise ValueError(f"{name} has invalid fields")


def _id(value: object, pattern: re.Pattern[str], field_name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field_name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        raise ValueError(f"{field_name} is invalid") from None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} is invalid")
    return value


def _note(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("note is invalid")
    return value


def _content(value: object, field_name: str) -> CandidateContent:
    if not isinstance(value, CandidateContent):
        raise ValueError(f"{field_name} is invalid")
    return CandidateContent.from_dict(value.to_dict())


def _content_from_dict(value: object, field_name: str) -> CandidateContent:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} is invalid")
    return CandidateContent.from_dict(value)


def _draft_item(value: object) -> "DraftItem":
    if not isinstance(value, DraftItem):
        raise ValueError("draft items are invalid")
    return DraftItem.from_dict(value.to_dict())


def _review_item(value: object) -> "CentralReviewItem":
    if not isinstance(value, CentralReviewItem):
        raise ValueError("review items are invalid")
    return CentralReviewItem.from_dict(value.to_dict())


@dataclass(frozen=True)
class DraftItem:
    family_id: str
    repository_id: str
    revision_id: str
    revision: int
    content_digest: str
    action: ReviewAction
    effective_content: CandidateContent | None
    note: str | None = None

    def __post_init__(self) -> None:
        _id(self.family_id, _FAMILY_ID, "family_id")
        _id(self.repository_id, _REPOSITORY_ID, "repository_id")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise ValueError("revision is invalid")
        _id(self.content_digest, _DIGEST, "content_digest")
        if self.revision_id != candidate_revision_id(
            self.family_id, self.revision, self.content_digest
        ):
            raise ValueError("revision_id is invalid")
        _id(self.revision_id, _REVISION_ID, "revision_id")
        if self.action not in _ACTIONS:
            raise ValueError("action is invalid")
        if self.action == "edit_accept":
            object.__setattr__(
                self, "effective_content", _content(self.effective_content, "effective_content")
            )
        elif self.effective_content is not None:
            raise ValueError("Only edit_accept may contain effective_content")
        object.__setattr__(self, "note", _note(self.note))

    def to_dict(self) -> dict[str, object]:
        return {
            "family_id": self.family_id,
            "repository_id": self.repository_id,
            "revision_id": self.revision_id,
            "revision": self.revision,
            "content_digest": self.content_digest,
            "action": self.action,
            "effective_content": (
                self.effective_content.to_dict()
                if self.effective_content is not None else None
            ),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "DraftItem":
        _require_fields(value, frozenset((
            "family_id", "repository_id", "revision_id", "revision",
            "content_digest", "action", "effective_content", "note",
        )), "DraftItem")
        action = value["action"]
        return cls(
            family_id=_id(value["family_id"], _FAMILY_ID, "family_id"),
            repository_id=_id(value["repository_id"], _REPOSITORY_ID, "repository_id"),
            revision_id=_id(value["revision_id"], _REVISION_ID, "revision_id"),
            revision=value["revision"],
            content_digest=_id(value["content_digest"], _DIGEST, "content_digest"),
            action=cast(ReviewAction, action),
            effective_content=(
                _content_from_dict(value["effective_content"], "effective_content")
                if value["effective_content"] is not None else None
            ),
            note=_note(value["note"]),
        )


@dataclass(frozen=True)
class ReviewDraft:
    organization_id: str
    actor_id: str
    product_id: str
    version: int
    items: tuple[DraftItem, ...]
    updated_at: str | None

    def __post_init__(self) -> None:
        require_id(self.organization_id, "organization_id")
        require_id(self.actor_id, "actor_id")
        _id(self.product_id, _PRODUCT_ID, "product_id")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 0:
            raise ValueError("version is invalid")
        if not isinstance(self.items, tuple):
            raise ValueError("items are invalid")
        items = tuple(_draft_item(item) for item in self.items)
        if len({item.family_id for item in items}) != len(items):
            raise ValueError("items contain a duplicate family")
        if self.updated_at is not None:
            _timestamp(self.updated_at, "updated_at")
        object.__setattr__(self, "items", items)

    def to_dict(self) -> dict[str, object]:
        return {
            "organization_id": self.organization_id,
            "actor_id": self.actor_id,
            "product_id": self.product_id,
            "version": self.version,
            "items": [item.to_dict() for item in self.items],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ReviewDraft":
        _require_fields(value, frozenset((
            "organization_id", "actor_id", "product_id", "version", "items", "updated_at",
        )), "ReviewDraft")
        raw_items = value["items"]
        if not isinstance(raw_items, list) or any(
            not isinstance(item, Mapping) for item in raw_items
        ):
            raise ValueError("items are invalid")
        return cls(
            organization_id=require_id(value["organization_id"], "organization_id"),
            actor_id=require_id(value["actor_id"], "actor_id"),
            product_id=_id(value["product_id"], _PRODUCT_ID, "product_id"),
            version=value["version"],
            items=tuple(DraftItem.from_dict(item) for item in raw_items),
            updated_at=(
                _timestamp(value["updated_at"], "updated_at")
                if value["updated_at"] is not None else None
            ),
        )


@dataclass(frozen=True)
class CentralReviewItem:
    review_id: str
    family_id: str
    publication_candidate_id: str
    repository_id: str
    revision_id: str
    revision: int
    content_digest: str
    action: ReviewAction
    effective_content: CandidateContent | None
    note: str | None

    def __post_init__(self) -> None:
        _id(self.review_id, _REVIEW_ID, "review_id")
        _id(self.family_id, _FAMILY_ID, "family_id")
        if self.publication_candidate_id != publication_candidate_id(self.family_id):
            raise ValueError("publication_candidate_id is invalid")
        _id(self.publication_candidate_id, _CANDIDATE_ID, "publication_candidate_id")
        _id(self.repository_id, _REPOSITORY_ID, "repository_id")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise ValueError("revision is invalid")
        _id(self.content_digest, _DIGEST, "content_digest")
        if self.revision_id != candidate_revision_id(self.family_id, self.revision, self.content_digest):
            raise ValueError("revision_id is invalid")
        _id(self.revision_id, _REVISION_ID, "revision_id")
        if self.action not in _ACTIONS:
            raise ValueError("action is invalid")
        if self.action in ("accept", "edit_accept"):
            object.__setattr__(
                self, "effective_content", _content(self.effective_content, "effective_content")
            )
        elif self.effective_content is not None:
            raise ValueError("Rejected or skipped items cannot contain effective_content")
        object.__setattr__(self, "note", _note(self.note))

    def to_dict(self) -> dict[str, object]:
        return {
            "review_id": self.review_id,
            "family_id": self.family_id,
            "publication_candidate_id": self.publication_candidate_id,
            "repository_id": self.repository_id,
            "revision_id": self.revision_id,
            "revision": self.revision,
            "content_digest": self.content_digest,
            "action": self.action,
            "effective_content": self.effective_content.to_dict() if self.effective_content else None,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CentralReviewItem":
        _require_fields(value, frozenset((
            "review_id", "family_id", "publication_candidate_id", "repository_id",
            "revision_id", "revision", "content_digest", "action",
            "effective_content", "note",
        )), "CentralReviewItem")
        return cls(
            review_id=_id(value["review_id"], _REVIEW_ID, "review_id"),
            family_id=_id(value["family_id"], _FAMILY_ID, "family_id"),
            publication_candidate_id=_id(value["publication_candidate_id"], _CANDIDATE_ID, "publication_candidate_id"),
            repository_id=_id(value["repository_id"], _REPOSITORY_ID, "repository_id"),
            revision_id=_id(value["revision_id"], _REVISION_ID, "revision_id"),
            revision=value["revision"],
            content_digest=_id(value["content_digest"], _DIGEST, "content_digest"),
            action=cast(ReviewAction, value["action"]),
            effective_content=(
                _content_from_dict(value["effective_content"], "effective_content")
                if value["effective_content"] is not None else None
            ),
            note=_note(value["note"]),
        )


@dataclass(frozen=True)
class CentralReviewBatch:
    review_batch_id: str
    organization_id: str
    actor_id: str
    product_id: str
    product_name: str
    client_action_id: str
    request_digest: str
    approval: ApprovalRef
    items: tuple[CentralReviewItem, ...]
    created_at: str

    def __post_init__(self) -> None:
        _id(self.review_batch_id, _REVIEW_BATCH_ID, "review_batch_id")
        require_id(self.organization_id, "organization_id")
        require_id(self.actor_id, "actor_id")
        _id(self.product_id, _PRODUCT_ID, "product_id")
        if canonical_product_name(self.product_name) != self.product_name or derive_product_id(self.product_name) != self.product_id:
            raise ValueError("product identity is invalid")
        _id(self.client_action_id, _WEB_ACTION_ID, "client_action_id")
        _id(self.request_digest, _DIGEST, "request_digest")
        if not isinstance(self.approval, ApprovalRef):
            raise ValueError("approval is invalid")
        if not isinstance(self.items, tuple) or not 1 <= len(self.items) <= 20:
            raise ValueError("items must contain between 1 and 20 values")
        items = tuple(_review_item(item) for item in self.items)
        if len({item.family_id for item in items}) != len(items):
            raise ValueError("items contain a duplicate family")
        if any(
            item.review_id
            != review_item_id(self.review_batch_id, item.publication_candidate_id)
            for item in items
        ):
            raise ValueError("review_id is invalid")
        _timestamp(self.created_at, "created_at")
        object.__setattr__(self, "items", items)

    def to_dict(self) -> dict[str, object]:
        return {
            "review_batch_id": self.review_batch_id,
            "organization_id": self.organization_id,
            "actor_id": self.actor_id,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "client_action_id": self.client_action_id,
            "request_digest": self.request_digest,
            "approval": self.approval.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CentralReviewBatch":
        _require_fields(value, frozenset((
            "review_batch_id", "organization_id", "actor_id", "product_id",
            "product_name", "client_action_id", "request_digest", "approval",
            "items", "created_at",
        )), "CentralReviewBatch")
        items = value["items"]
        if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
            raise ValueError("items are invalid")
        if not isinstance(value["approval"], Mapping):
            raise ValueError("approval is invalid")
        return cls(
            review_batch_id=_id(value["review_batch_id"], _REVIEW_BATCH_ID, "review_batch_id"),
            organization_id=require_id(value["organization_id"], "organization_id"),
            actor_id=require_id(value["actor_id"], "actor_id"),
            product_id=_id(value["product_id"], _PRODUCT_ID, "product_id"),
            product_name=value["product_name"],
            client_action_id=_id(value["client_action_id"], _WEB_ACTION_ID, "client_action_id"),
            request_digest=_id(value["request_digest"], _DIGEST, "request_digest"),
            approval=ApprovalRef.from_dict(value["approval"]),
            items=tuple(CentralReviewItem.from_dict(item) for item in items),
            created_at=_timestamp(value["created_at"], "created_at"),
        )


@dataclass(frozen=True)
class CentralPublication:
    publication_id: str
    organization_id: str
    actor_id: str
    product_id: str
    preview_id: str
    confirm_action_id: str
    confirm_request_digest: str
    state: PublicationState
    approval: ApprovalRef
    commit_sha: str | None
    recovery_code: str | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _id(self.publication_id, _PUBLICATION_ID, "publication_id")
        _id(self.preview_id, _PREVIEW_ID, "preview_id")
        if self.publication_id != central_publication_id(self.preview_id):
            raise ValueError("publication_id is invalid")
        require_id(self.organization_id, "organization_id")
        require_id(self.actor_id, "actor_id")
        _id(self.product_id, _PRODUCT_ID, "product_id")
        _id(self.confirm_action_id, _WEB_ACTION_ID, "confirm_action_id")
        _id(self.confirm_request_digest, _DIGEST, "confirm_request_digest")
        if self.state not in _PUBLICATION_STATES:
            raise ValueError("state is invalid")
        if not isinstance(self.approval, ApprovalRef):
            raise ValueError("approval is invalid")
        if self.state == "confirmed":
            if self.commit_sha is not None:
                raise ValueError("commit_sha is invalid for state")
        else:
            _id(self.commit_sha, _GIT_COMMIT, "commit_sha")
        if self.recovery_code is not None:
            _id(self.recovery_code, _RECOVERY_CODE, "recovery_code")
        _timestamp(self.created_at, "created_at")
        _timestamp(self.updated_at, "updated_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "publication_id": self.publication_id,
            "organization_id": self.organization_id,
            "actor_id": self.actor_id,
            "product_id": self.product_id,
            "preview_id": self.preview_id,
            "confirm_action_id": self.confirm_action_id,
            "confirm_request_digest": self.confirm_request_digest,
            "state": self.state,
            "approval": self.approval.to_dict(),
            "commit_sha": self.commit_sha,
            "recovery_code": self.recovery_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CentralPublication":
        _require_fields(value, frozenset((
            "publication_id", "organization_id", "actor_id", "product_id",
            "preview_id", "confirm_action_id", "confirm_request_digest", "state",
            "approval", "commit_sha", "recovery_code", "created_at", "updated_at",
        )), "CentralPublication")
        if not isinstance(value["approval"], Mapping):
            raise ValueError("approval is invalid")
        return cls(
            publication_id=_id(value["publication_id"], _PUBLICATION_ID, "publication_id"),
            organization_id=require_id(value["organization_id"], "organization_id"),
            actor_id=require_id(value["actor_id"], "actor_id"),
            product_id=_id(value["product_id"], _PRODUCT_ID, "product_id"),
            preview_id=_id(value["preview_id"], _PREVIEW_ID, "preview_id"),
            confirm_action_id=_id(value["confirm_action_id"], _WEB_ACTION_ID, "confirm_action_id"),
            confirm_request_digest=_id(value["confirm_request_digest"], _DIGEST, "confirm_request_digest"),
            state=cast(PublicationState, value["state"]),
            approval=ApprovalRef.from_dict(value["approval"]),
            commit_sha=(
                _id(value["commit_sha"], _GIT_COMMIT, "commit_sha")
                if value["commit_sha"] is not None else None
            ),
            recovery_code=(
                _id(value["recovery_code"], _RECOVERY_CODE, "recovery_code")
                if value["recovery_code"] is not None else None
            ),
            created_at=_timestamp(value["created_at"], "created_at"),
            updated_at=_timestamp(value["updated_at"], "updated_at"),
        )


@dataclass(frozen=True)
class ActionResult:
    organization_id: str
    actor_id: str
    action_kind: ActionKind
    client_action_id: str
    request_digest: str
    result_id: str
    created_at: str

    def __post_init__(self) -> None:
        require_id(self.organization_id, "organization_id")
        require_id(self.actor_id, "actor_id")
        if self.action_kind not in _ACTION_KINDS:
            raise ValueError("action_kind is invalid")
        _id(self.client_action_id, _WEB_ACTION_ID, "client_action_id")
        _id(self.request_digest, _DIGEST, "request_digest")
        result_patterns = {
            "review": _REVIEW_BATCH_ID,
            "preview": _PREVIEW_ID,
            "publish": _PUBLICATION_ID,
            "resume": _PUBLICATION_ID,
        }
        _id(self.result_id, result_patterns[self.action_kind], "result_id")
        _timestamp(self.created_at, "created_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "organization_id": self.organization_id,
            "actor_id": self.actor_id,
            "action_kind": self.action_kind,
            "client_action_id": self.client_action_id,
            "request_digest": self.request_digest,
            "result_id": self.result_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ActionResult":
        _require_fields(value, frozenset((
            "organization_id", "actor_id", "action_kind", "client_action_id",
            "request_digest", "result_id", "created_at",
        )), "ActionResult")
        return cls(
            organization_id=require_id(value["organization_id"], "organization_id"),
            actor_id=require_id(value["actor_id"], "actor_id"),
            action_kind=cast(ActionKind, value["action_kind"]),
            client_action_id=_id(value["client_action_id"], _WEB_ACTION_ID, "client_action_id"),
            request_digest=_id(value["request_digest"], _DIGEST, "request_digest"),
            result_id=value["result_id"],
            created_at=_timestamp(value["created_at"], "created_at"),
        )
