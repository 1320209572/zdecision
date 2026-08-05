"""Product-scoped Candidate Inbox and durable Review draft operations."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from zdecision.capture.reviews import ApprovalRef
from zdecision.central.auth import Principal
from zdecision.central.decision_spaces import LeafDecisionSpace
from zdecision.central.web.contracts import (
    CandidateInboxView,
    CentralReviewBatch,
    CentralReviewItem,
    DraftItem,
    ReviewDraft,
    ReviewSubmissionSnapshot,
)
from zdecision.central.web.queries import CentralWebQueries
from zdecision.central.web.store import (
    CentralWebStore,
    DraftConflict,
    WebActionConflict,
    WebRecordCorrupt,
    immediate,
)
from zdecision.ids import (
    candidate_revision_id,
    central_review_batch_id,
    publication_candidate_id,
    review_item_id,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import CandidateRevisionUpload


_CANDIDATE_ITEM_LIMIT = 16 * 1024
_STATES = frozenset(("pending", "accepted", "rejected", "published", "all"))


class CentralReviewError(Exception):
    """Base class for stable, detail-free Review service failures."""

    code = "review_error"


class DecisionSpaceNotFound(CentralReviewError):
    code = "not_found"


class DecisionSpaceNotLeaf(CentralReviewError):
    code = "decision_space_not_leaf"


class DecisionSpaceOwnershipConflict(CentralReviewError):
    code = "decision_space_ownership_conflict"


ProductNotFound = DecisionSpaceNotFound
ProductOwnershipConflict = DecisionSpaceOwnershipConflict


class ReviewStale(CentralReviewError):
    code = "review_stale"

    def __init__(self, family_ids: Sequence[str]) -> None:
        self.family_ids = tuple(family_ids)
        super().__init__(self.code)


@dataclass(frozen=True)
class ReviewSubmissionResult:
    batch: CentralReviewBatch
    preview_eligible: bool
    remaining_pending: tuple[str, ...]
    draft_version: int

    def to_dict(self) -> dict[str, object]:
        return {
            "review_batch_id": self.batch.review_batch_id,
            "items": [
                {
                    "review_id": item.review_id,
                    "family_id": item.family_id,
                    "publication_candidate_id": item.publication_candidate_id,
                    "repository_id": item.repository_id,
                    "revision_id": item.revision_id,
                    "revision": item.revision,
                    "content_digest": item.content_digest,
                    "action": item.action,
                }
                for item in self.batch.items
            ],
            "preview_eligible": self.preview_eligible,
            "remaining_pending_count": len(self.remaining_pending),
            "draft_version": self.draft_version,
        }


class CentralReviewService:
    def __init__(
        self, *, store: CentralWebStore, queries: CentralWebQueries
    ) -> None:
        if not isinstance(store, CentralWebStore):
            raise TypeError("store must be a CentralWebStore")
        if not isinstance(queries, CentralWebQueries):
            raise TypeError("queries must be CentralWebQueries")
        self.store = store
        self.queries = queries

    def list_candidates(
        self,
        principal: Principal,
        decision_space_id: str,
        *,
        search: str = "",
        repository_id: str | None = None,
        capture_request_id: str | None = None,
        state: str = "pending",
        limit: int = 50,
        offset: int = 0,
    ) -> CandidateInboxView:
        self._require_user(principal)
        space = self._require_space(principal, decision_space_id)
        if not isinstance(search, str) or len(search.encode("utf-8")) > 200:
            raise ValueError("search is invalid")
        if state not in _STATES:
            raise ValueError("state is invalid")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise ValueError("limit is invalid")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            raise ValueError("offset is invalid")
        if repository_id is not None:
            self._require_repository(
                principal, space.decision_space_id, repository_id
            )
        draft = self.store.get_draft(
            principal.organization_id, principal.actor_id,
            space.decision_space_id,
        )
        return self.queries.candidate_inbox(
            principal,
            space.decision_space_id,
            draft,
            search=search,
            repository_id=repository_id,
            capture_request_id=capture_request_id,
            state=state,
            limit=limit,
            offset=offset,
        )

    def get_draft(
        self, principal: Principal, decision_space_id: str
    ) -> ReviewDraft:
        self._require_user(principal)
        space = self._require_space(principal, decision_space_id)
        return self.store.get_draft(
            principal.organization_id, principal.actor_id,
            space.decision_space_id,
        )

    def save_draft(
        self,
        principal: Principal,
        decision_space_id: str,
        expected_version: int,
        items: Sequence[DraftItem],
        now: str | datetime,
    ) -> ReviewDraft:
        self._require_user(principal)
        space = self._require_space(principal, decision_space_id)
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 0
        ):
            raise ValueError("expected_version is invalid")
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
            raise ValueError("items are invalid")
        draft_items = tuple(items)
        if len(draft_items) > 100 or any(
            not isinstance(item, DraftItem) for item in draft_items
        ):
            raise ValueError("items are invalid")
        current = self.store.get_draft(
            principal.organization_id, principal.actor_id,
            space.decision_space_id,
        )
        if current.version != expected_version:
            raise DraftConflict("review_draft_conflict")
        for item in draft_items:
            self._validate_draft_item(principal, space, item)
        return self.store.replace_draft(
            current, draft_items, self._timestamp(now)
        )

    def submit(
        self,
        principal: Principal,
        decision_space_id: str,
        client_action_id: str,
        expected_draft_version: int,
        items: Sequence[DraftItem],
        now: str | datetime,
    ) -> ReviewSubmissionResult:
        self._require_user(principal)
        space = self._require_space(principal, decision_space_id)
        if (
            not isinstance(expected_draft_version, int)
            or isinstance(expected_draft_version, bool)
            or expected_draft_version < 0
        ):
            raise ValueError("expected_draft_version is invalid")
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
            raise ValueError("items are invalid")
        ordered = tuple(items)
        if not 1 <= len(ordered) <= 20 or any(
            not isinstance(item, DraftItem) for item in ordered
        ):
            raise ValueError("items are invalid")
        if len({item.family_id for item in ordered}) != len(ordered):
            raise ValueError("items contain a duplicate family")
        timestamp = self._timestamp(now)
        request_digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "draft_version": expected_draft_version,
                    "items": [item.to_dict() for item in ordered],
                    "decision_space_id": space.decision_space_id,
                }
            )
        ).hexdigest()

        with immediate(self.store.connection):
            replay = self.store.action_result(
                principal.organization_id,
                principal.actor_id,
                "review",
                client_action_id,
            )
            if replay is not None:
                if replay.request_digest != request_digest:
                    raise WebActionConflict("web_action_conflict")
                batch = self.store.get_review_batch(
                    principal.organization_id, space.decision_space_id,
                    replay.result_id,
                )
                if batch is None:
                    raise WebRecordCorrupt("review_action_result")
                snapshot = self.store.get_review_submission_result(
                    principal.organization_id,
                    principal.actor_id,
                    space.decision_space_id,
                    batch.review_batch_id,
                )
                if snapshot is None:
                    raise WebRecordCorrupt("review_submission_result")
                return ReviewSubmissionResult(
                    batch=batch,
                    preview_eligible=snapshot.preview_eligible,
                    remaining_pending=snapshot.remaining_pending,
                    draft_version=snapshot.draft_version,
                )

            draft = self.store.get_draft(
                principal.organization_id, principal.actor_id,
                space.decision_space_id,
            )
            if draft.version != expected_draft_version:
                raise DraftConflict("review_draft_conflict")
            draft_by_family = {item.family_id: item for item in draft.items}
            if any(
                draft_by_family.get(item.family_id) != item for item in ordered
            ):
                raise DraftConflict("review_draft_conflict")

            current_items: list[tuple[DraftItem, CandidateRevisionUpload]] = []
            stale_families: list[str] = []
            for item in ordered:
                current = self.queries.current_candidate_revision(
                    principal, item.repository_id, item.family_id
                )
                if current is None or (
                    current.revision_id != item.revision_id
                    or current.revision != item.revision
                    or current.content_digest != item.content_digest
                ):
                    stale_families.append(item.family_id)
                    continue
                self._validate_draft_item(principal, space, item)
                current_items.append((item, current))
            if stale_families:
                raise ReviewStale(stale_families)

            identity_items = tuple(item.to_dict() for item in ordered)
            batch_id = central_review_batch_id(
                principal.organization_id,
                principal.actor_id,
                space.compatibility_product_id,
                client_action_id,
                identity_items,
            )
            frozen_items = tuple(
                self._freeze_item(batch_id, item, current)
                for item, current in current_items
            )
            batch = CentralReviewBatch(
                review_batch_id=batch_id,
                organization_id=principal.organization_id,
                actor_id=principal.actor_id,
                decision_space_id=space.decision_space_id,
                compatibility_product_id=space.compatibility_product_id,
                compatibility_product_name=space.compatibility_product_name,
                client_action_id=client_action_id,
                request_digest=request_digest,
                approval=ApprovalRef(
                    actor="user",
                    thread_id=f"web_review_{batch_id.removeprefix('rvb_')}",
                    turn_id=client_action_id,
                    recorded_at=timestamp,
                ),
                items=frozen_items,
                created_at=timestamp,
            )
            self.store.put_review_batch(batch)
            updated_draft = self.store.clear_submitted_draft_items(
                draft, ordered, timestamp
            )
            result = self._submission_result(
                principal, batch, draft_version=updated_draft.version
            )
            self.store.put_review_submission_result(
                ReviewSubmissionSnapshot(
                    organization_id=principal.organization_id,
                    actor_id=principal.actor_id,
                    decision_space_id=space.decision_space_id,
                    review_batch_id=batch.review_batch_id,
                    preview_eligible=result.preview_eligible,
                    remaining_pending=result.remaining_pending,
                    draft_version=result.draft_version,
                )
            )
            self.store.record_action(
                principal.organization_id,
                principal.actor_id,
                "review",
                client_action_id,
                request_digest,
                batch.review_batch_id,
                timestamp,
            )
            return result

    @staticmethod
    def _freeze_item(
        batch_id: str,
        item: DraftItem,
        current: CandidateRevisionUpload,
    ) -> CentralReviewItem:
        candidate_id = publication_candidate_id(item.family_id)
        effective_content = (
            item.effective_content
            if item.action == "edit_accept"
            else current.content if item.action == "accept" else None
        )
        return CentralReviewItem(
            review_id=review_item_id(batch_id, candidate_id),
            family_id=item.family_id,
            publication_candidate_id=candidate_id,
            repository_id=item.repository_id,
            revision_id=item.revision_id,
            revision=item.revision,
            content_digest=item.content_digest,
            action=item.action,
            effective_content=effective_content,
            note=item.note,
        )

    def _submission_result(
        self,
        principal: Principal,
        batch: CentralReviewBatch,
        *,
        draft_version: int | None = None,
    ) -> ReviewSubmissionResult:
        pending: list[str] = []
        offset = 0
        while True:
            page = self.list_candidates(
                principal,
                batch.decision_space_id,
                state="pending",
                limit=100,
                offset=offset,
            )
            pending.extend(item.family_id for item in page.items)
            if len(page.items) < 100:
                break
            offset += 100
        version = (
            draft_version
            if draft_version is not None
            else self.store.get_draft(
                principal.organization_id,
                principal.actor_id,
                batch.decision_space_id,
            ).version
        )
        return ReviewSubmissionResult(
            batch=batch,
            preview_eligible=any(
                item.action in ("accept", "edit_accept")
                for item in batch.items
            ),
            remaining_pending=tuple(pending),
            draft_version=version,
        )

    def _validate_draft_item(
        self, principal: Principal, space: LeafDecisionSpace, item: DraftItem
    ) -> None:
        self._require_repository(
            principal, space.decision_space_id, item.repository_id
        )
        candidate = self.queries.candidate_revision(
            principal,
            item.repository_id,
            item.family_id,
            item.revision_id,
        )
        if candidate is None:
            raise DecisionSpaceOwnershipConflict()
        ownership = self.queries.candidate_revision_ownership(
            principal,
            item.repository_id,
            item.family_id,
            item.revision,
        )
        if ownership is None or (
            ownership.decision_space_id != space.decision_space_id
            or ownership.repository_id != item.repository_id
            or ownership.compatibility_product_id
            != space.compatibility_product_id
            or ownership.compatibility_product_name
            != space.compatibility_product_name
        ):
            raise DecisionSpaceOwnershipConflict()
        if (
            item.revision != candidate.revision
            or item.content_digest != candidate.content_digest
            or item.revision_id
            != candidate_revision_id(
                item.family_id, item.revision, item.content_digest
            )
        ):
            raise DecisionSpaceOwnershipConflict()
        if item.note is not None and len(item.note.encode("utf-8")) > 1000:
            raise ValueError("note is invalid")
        if item.action != "edit_accept":
            return
        effective = item.effective_content
        if effective is None:
            raise ValueError("effective_content is invalid")
        if (
            effective.product != candidate.content.product
            or effective.repositories != candidate.content.repositories
        ):
            raise ValueError("edited product scope is invalid")
        digest = hashlib.sha256(
            canonical_json_bytes(effective.to_dict())
        ).hexdigest()
        effective_revision = CandidateRevisionUpload(
            family_id=item.family_id,
            revision_id=candidate_revision_id(
                item.family_id, item.revision, digest
            ),
            revision=item.revision,
            content=effective,
            content_digest=digest,
            evidence_digest="0" * 64,
        )
        if (
            len(canonical_json_bytes(effective_revision.to_dict()))
            > _CANDIDATE_ITEM_LIMIT
        ):
            raise ValueError("effective_content is invalid")

    def _require_space(
        self, principal: Principal, identifier: str
    ) -> LeafDecisionSpace:
        space = self.queries.decision_space(principal, identifier)
        if space is None or not space.enabled:
            if self.queries.catalog_group_exists(principal, identifier):
                raise DecisionSpaceNotLeaf()
            raise DecisionSpaceNotFound()
        return space

    def _require_repository(
        self, principal: Principal, decision_space_id: str,
        repository_id: str
    ) -> None:
        repositories = self.queries.decision_space_repositories(
            principal, decision_space_id
        )
        if repository_id not in {
            item.repository_id for item in repositories
        }:
            raise DecisionSpaceOwnershipConflict()

    @staticmethod
    def _require_user(principal: Principal) -> None:
        if not isinstance(principal, Principal) or principal.kind != "user":
            raise ValueError("A browser user Principal is required")

    @staticmethod
    def _timestamp(value: str | datetime) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("now is invalid")
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError("now is invalid")
        datetime.fromisoformat(value[:-1] + "+00:00")
        return value
