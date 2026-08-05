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
    central_review_batch_id,
    product_id as derive_product_id,
    publication_candidate_id,
    review_item_id,
)
from zdecision.sync.contracts import RepositoryView


ReviewAction = Literal["accept", "edit_accept", "reject", "skip"]
CandidateReviewState = Literal["pending", "accepted", "rejected", "published"]
DecisionSpaceKind = Literal["product", "shared_unit"]
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
_DECISION_SPACE_ID = re.compile(r"^dsp_[0-9a-f]{32}$")
_REVIEW_BATCH_ID = re.compile(r"^rvb_[0-9a-f]{32}$")
_REVIEW_ID = re.compile(r"^rvi_[0-9a-f]{32}$")
_CANDIDATE_ID = re.compile(r"^cand_[0-9a-f]{32}_01$")
_PREVIEW_ID = re.compile(r"^pub_[0-9a-f]{32}$")
_PUBLICATION_ID = re.compile(r"^plb_[0-9a-f]{32}$")
_WEB_ACTION_ID = re.compile(r"^web_action_[A-Za-z0-9_-]{1,96}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_RECOVERY_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CAPTURE_REQUEST_ID = re.compile(r"^crq_[0-9a-f]{32}$")
_REVIEW_STATES = frozenset(("pending", "accepted", "rejected", "published"))


@dataclass(frozen=True)
class DecisionSpaceRef:
    decision_space_id: str
    kind: DecisionSpaceKind
    display_name: str
    breadcrumb: tuple[str, ...]
    source_root: str
    package_name: str | None
    asset_type: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_space_id": self.decision_space_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "breadcrumb": list(self.breadcrumb),
            "source_root": self.source_root,
            "package_name": self.package_name,
            "asset_type": self.asset_type,
        }


@dataclass(frozen=True)
class DecisionSpaceSummary(DecisionSpaceRef):
    repository_ids: tuple[str, ...]
    pending_candidate_count: int
    active_decision_count: int | None
    last_activity_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            **super().to_dict(),
            "repository_ids": list(self.repository_ids),
            "pending_candidate_count": self.pending_candidate_count,
            "active_decision_count": self.active_decision_count,
            "last_activity_at": self.last_activity_at,
        }


@dataclass(frozen=True)
class CatalogNode:
    node_id: str
    kind: Literal["catalog_group", "product", "shared_unit"]
    display_name: str
    breadcrumb: tuple[str, ...]
    pending_candidate_count: int
    active_decision_count: int | None
    last_activity_at: str | None
    space: DecisionSpaceSummary | None
    children: tuple["CatalogNode", ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "breadcrumb": list(self.breadcrumb),
            "pending_candidate_count": self.pending_candidate_count,
            "active_decision_count": self.active_decision_count,
            "last_activity_at": self.last_activity_at,
            "space": self.space.to_dict() if self.space is not None else None,
            "children": [child.to_dict() for child in self.children],
        }


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
    decision_space_id: str
    version: int
    items: tuple[DraftItem, ...]
    updated_at: str | None

    def __post_init__(self) -> None:
        require_id(self.organization_id, "organization_id")
        require_id(self.actor_id, "actor_id")
        _id(self.decision_space_id, _DECISION_SPACE_ID, "decision_space_id")
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
            "decision_space_id": self.decision_space_id,
            "version": self.version,
            "items": [item.to_dict() for item in self.items],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ReviewDraft":
        _require_fields(value, frozenset((
            "organization_id", "actor_id", "decision_space_id", "version", "items", "updated_at",
        )), "ReviewDraft")
        raw_items = value["items"]
        if not isinstance(raw_items, list) or any(
            not isinstance(item, Mapping) for item in raw_items
        ):
            raise ValueError("items are invalid")
        return cls(
            organization_id=require_id(value["organization_id"], "organization_id"),
            actor_id=require_id(value["actor_id"], "actor_id"),
            decision_space_id=_id(
                value["decision_space_id"], _DECISION_SPACE_ID,
                "decision_space_id",
            ),
            version=value["version"],
            items=tuple(DraftItem.from_dict(item) for item in raw_items),
            updated_at=(
                _timestamp(value["updated_at"], "updated_at")
                if value["updated_at"] is not None else None
            ),
        )


@dataclass(frozen=True)
class CandidateInboxItem:
    family_id: str
    repository_id: str
    capture_request_ids: tuple[str, ...]
    revision_id: str
    revision: int
    content_digest: str
    content: CandidateContent
    review_state: CandidateReviewState
    draft_action: ReviewAction | None
    stale_draft: bool

    def __post_init__(self) -> None:
        _id(self.family_id, _FAMILY_ID, "family_id")
        _id(self.repository_id, _REPOSITORY_ID, "repository_id")
        if not isinstance(self.capture_request_ids, tuple):
            raise ValueError("capture_request_ids are invalid")
        capture_request_ids = tuple(
            _id(value, _CAPTURE_REQUEST_ID, "capture_request_id")
            for value in self.capture_request_ids
        )
        if len(set(capture_request_ids)) != len(capture_request_ids):
            raise ValueError("capture_request_ids contain duplicates")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 1
        ):
            raise ValueError("revision is invalid")
        _id(self.content_digest, _DIGEST, "content_digest")
        if self.revision_id != candidate_revision_id(
            self.family_id, self.revision, self.content_digest
        ):
            raise ValueError("revision_id is invalid")
        _id(self.revision_id, _REVISION_ID, "revision_id")
        object.__setattr__(self, "content", _content(self.content, "content"))
        if self.review_state not in _REVIEW_STATES:
            raise ValueError("review_state is invalid")
        if self.draft_action is not None and self.draft_action not in _ACTIONS:
            raise ValueError("draft_action is invalid")
        if not isinstance(self.stale_draft, bool):
            raise ValueError("stale_draft is invalid")
        object.__setattr__(self, "capture_request_ids", capture_request_ids)

    def to_dict(self) -> dict[str, object]:
        return {
            "family_id": self.family_id,
            "repository_id": self.repository_id,
            "capture_request_ids": list(self.capture_request_ids),
            "revision_id": self.revision_id,
            "revision": self.revision,
            "content_digest": self.content_digest,
            "content": self.content.to_dict(),
            "review_state": self.review_state,
            "draft_action": self.draft_action,
            "stale_draft": self.stale_draft,
        }


@dataclass(frozen=True)
class CandidateInboxView:
    product_id: str
    product_name: str
    repositories: tuple[RepositoryView, ...]
    items: tuple[CandidateInboxItem, ...]
    draft: ReviewDraft
    space: DecisionSpaceRef | None = None

    def __post_init__(self) -> None:
        _id(self.product_id, _PRODUCT_ID, "product_id")
        if canonical_product_name(self.product_name) != self.product_name:
            raise ValueError("product_name is invalid")
        if derive_product_id(self.product_name) != self.product_id:
            raise ValueError("product identity is invalid")
        if not isinstance(self.repositories, tuple) or any(
            not isinstance(item, RepositoryView) for item in self.repositories
        ):
            raise ValueError("repositories are invalid")
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, CandidateInboxItem) for item in self.items
        ):
            raise ValueError("items are invalid")
        if not isinstance(self.draft, ReviewDraft):
            raise ValueError("draft is invalid")
        if any(item.product_id != self.product_id for item in self.repositories):
            raise ValueError("repository product is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "product_id": self.product_id,
            "product_name": self.product_name,
            "space": self.space.to_dict() if self.space is not None else None,
            "repositories": [item.to_dict() for item in self.repositories],
            "items": [item.to_dict() for item in self.items],
            "draft": self.draft.to_dict(),
        }

    def to_safe_dict(self) -> dict[str, object]:
        value = self.to_dict()
        value.pop("product_id")
        value.pop("product_name")
        value["repositories"] = [
            {
                "repository_id": item.repository_id,
                "enabled": item.enabled,
            }
            for item in self.repositories
        ]
        return value


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
    decision_space_id: str
    compatibility_product_id: str
    compatibility_product_name: str
    client_action_id: str
    request_digest: str
    approval: ApprovalRef
    items: tuple[CentralReviewItem, ...]
    created_at: str

    def __post_init__(self) -> None:
        _id(self.review_batch_id, _REVIEW_BATCH_ID, "review_batch_id")
        require_id(self.organization_id, "organization_id")
        require_id(self.actor_id, "actor_id")
        _id(self.decision_space_id, _DECISION_SPACE_ID, "decision_space_id")
        _id(
            self.compatibility_product_id, _PRODUCT_ID,
            "compatibility_product_id",
        )
        if (
            canonical_product_name(self.compatibility_product_name)
            != self.compatibility_product_name
            or derive_product_id(self.compatibility_product_name)
            != self.compatibility_product_id
        ):
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
        identity_items = tuple(
            {
                "family_id": item.family_id,
                "repository_id": item.repository_id,
                "revision_id": item.revision_id,
                "revision": item.revision,
                "content_digest": item.content_digest,
                "action": item.action,
                "effective_content": (
                    item.effective_content.to_dict()
                    if item.action == "edit_accept" else None
                ),
                "note": item.note,
            }
            for item in items
        )
        if self.review_batch_id != central_review_batch_id(
            self.organization_id,
            self.actor_id,
            self.compatibility_product_id,
            self.client_action_id,
            identity_items,
        ):
            raise ValueError("review_batch_id is invalid")
        _timestamp(self.created_at, "created_at")
        object.__setattr__(self, "items", items)

    def to_dict(self) -> dict[str, object]:
        return {
            "review_batch_id": self.review_batch_id,
            "organization_id": self.organization_id,
            "actor_id": self.actor_id,
            "decision_space_id": self.decision_space_id,
            "compatibility_product_id": self.compatibility_product_id,
            "compatibility_product_name": self.compatibility_product_name,
            "client_action_id": self.client_action_id,
            "request_digest": self.request_digest,
            "approval": self.approval.to_dict(),
            "items": [item.to_dict() for item in self.items],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CentralReviewBatch":
        _require_fields(value, frozenset((
            "review_batch_id", "organization_id", "actor_id", "decision_space_id",
            "compatibility_product_id", "compatibility_product_name",
            "client_action_id", "request_digest", "approval",
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
            decision_space_id=_id(
                value["decision_space_id"], _DECISION_SPACE_ID,
                "decision_space_id",
            ),
            compatibility_product_id=_id(
                value["compatibility_product_id"], _PRODUCT_ID,
                "compatibility_product_id",
            ),
            compatibility_product_name=value["compatibility_product_name"],
            client_action_id=_id(value["client_action_id"], _WEB_ACTION_ID, "client_action_id"),
            request_digest=_id(value["request_digest"], _DIGEST, "request_digest"),
            approval=ApprovalRef.from_dict(value["approval"]),
            items=tuple(CentralReviewItem.from_dict(item) for item in items),
            created_at=_timestamp(value["created_at"], "created_at"),
        )

    @property
    def product_id(self) -> str:
        return self.compatibility_product_id

    @property
    def product_name(self) -> str:
        return self.compatibility_product_name


@dataclass(frozen=True)
class ReviewSubmissionSnapshot:
    organization_id: str
    actor_id: str
    decision_space_id: str
    review_batch_id: str
    preview_eligible: bool
    remaining_pending: tuple[str, ...]
    draft_version: int

    def __post_init__(self) -> None:
        require_id(self.organization_id, "organization_id")
        require_id(self.actor_id, "actor_id")
        _id(self.decision_space_id, _DECISION_SPACE_ID, "decision_space_id")
        _id(self.review_batch_id, _REVIEW_BATCH_ID, "review_batch_id")
        if not isinstance(self.preview_eligible, bool):
            raise ValueError("preview_eligible is invalid")
        if not isinstance(self.remaining_pending, tuple):
            raise ValueError("remaining_pending is invalid")
        pending = tuple(
            _id(value, _FAMILY_ID, "family_id")
            for value in self.remaining_pending
        )
        if len(set(pending)) != len(pending):
            raise ValueError("remaining_pending contains duplicates")
        if (
            not isinstance(self.draft_version, int)
            or isinstance(self.draft_version, bool)
            or self.draft_version < 1
        ):
            raise ValueError("draft_version is invalid")
        object.__setattr__(self, "remaining_pending", pending)

    def to_dict(self) -> dict[str, object]:
        return {
            "organization_id": self.organization_id,
            "actor_id": self.actor_id,
            "decision_space_id": self.decision_space_id,
            "review_batch_id": self.review_batch_id,
            "preview_eligible": self.preview_eligible,
            "remaining_pending": list(self.remaining_pending),
            "draft_version": self.draft_version,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> "ReviewSubmissionSnapshot":
        _require_fields(value, frozenset((
            "organization_id", "actor_id", "decision_space_id", "review_batch_id",
            "preview_eligible", "remaining_pending", "draft_version",
        )), "ReviewSubmissionSnapshot")
        pending = value["remaining_pending"]
        if not isinstance(pending, list):
            raise ValueError("remaining_pending is invalid")
        return cls(
            organization_id=require_id(
                value["organization_id"], "organization_id"
            ),
            actor_id=require_id(value["actor_id"], "actor_id"),
            decision_space_id=_id(
                value["decision_space_id"], _DECISION_SPACE_ID,
                "decision_space_id",
            ),
            review_batch_id=_id(
                value["review_batch_id"], _REVIEW_BATCH_ID, "review_batch_id"
            ),
            preview_eligible=value["preview_eligible"],
            remaining_pending=tuple(pending),
            draft_version=value["draft_version"],
        )


@dataclass(frozen=True)
class CentralPublication:
    publication_id: str
    organization_id: str
    actor_id: str
    decision_space_id: str
    compatibility_product_id: str
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
        _id(self.decision_space_id, _DECISION_SPACE_ID, "decision_space_id")
        _id(
            self.compatibility_product_id, _PRODUCT_ID,
            "compatibility_product_id",
        )
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
            "decision_space_id": self.decision_space_id,
            "compatibility_product_id": self.compatibility_product_id,
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
            "publication_id", "organization_id", "actor_id", "decision_space_id",
            "compatibility_product_id",
            "preview_id", "confirm_action_id", "confirm_request_digest", "state",
            "approval", "commit_sha", "recovery_code", "created_at", "updated_at",
        )), "CentralPublication")
        if not isinstance(value["approval"], Mapping):
            raise ValueError("approval is invalid")
        return cls(
            publication_id=_id(value["publication_id"], _PUBLICATION_ID, "publication_id"),
            organization_id=require_id(value["organization_id"], "organization_id"),
            actor_id=require_id(value["actor_id"], "actor_id"),
            decision_space_id=_id(
                value["decision_space_id"], _DECISION_SPACE_ID,
                "decision_space_id",
            ),
            compatibility_product_id=_id(
                value["compatibility_product_id"], _PRODUCT_ID,
                "compatibility_product_id",
            ),
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

    @property
    def product_id(self) -> str:
        return self.compatibility_product_id


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
