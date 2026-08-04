"""Product-scoped Candidate Inbox and durable Review draft operations."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

from zdecision.central.auth import Principal
from zdecision.central.web.contracts import (
    CandidateInboxView,
    DraftItem,
    ReviewDraft,
)
from zdecision.central.web.queries import CentralWebQueries
from zdecision.central.web.store import CentralWebStore, DraftConflict
from zdecision.ids import candidate_revision_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import CandidateRevisionUpload


_CANDIDATE_ITEM_LIMIT = 16 * 1024
_STATES = frozenset(("pending", "accepted", "rejected", "published", "all"))


class CentralReviewError(Exception):
    """Base class for stable, detail-free Review service failures."""

    code = "review_error"


class ProductNotFound(CentralReviewError):
    code = "not_found"


class ProductOwnershipConflict(CentralReviewError):
    code = "product_ownership_conflict"


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
        product_id: str,
        *,
        search: str = "",
        repository_id: str | None = None,
        capture_request_id: str | None = None,
        state: str = "pending",
        limit: int = 50,
        offset: int = 0,
    ) -> CandidateInboxView:
        self._require_user(principal)
        self._require_product(principal, product_id)
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
            self._require_repository(principal, product_id, repository_id)
        if capture_request_id is not None:
            route = self.queries.capture_request_route(
                principal, capture_request_id
            )
            if route is None:
                raise ProductNotFound()
            request_repository_id, request_product_id = route
            if request_product_id != product_id:
                raise ProductOwnershipConflict()
            self._require_repository(
                principal, product_id, request_repository_id
            )
        draft = self.store.get_draft(
            principal.organization_id, principal.actor_id, product_id
        )
        return self.queries.candidate_inbox(
            principal,
            product_id,
            draft,
            search=search,
            repository_id=repository_id,
            capture_request_id=capture_request_id,
            state=state,
            limit=limit,
            offset=offset,
        )

    def get_draft(
        self, principal: Principal, product_id: str
    ) -> ReviewDraft:
        self._require_user(principal)
        self._require_product(principal, product_id)
        return self.store.get_draft(
            principal.organization_id, principal.actor_id, product_id
        )

    def save_draft(
        self,
        principal: Principal,
        product_id: str,
        expected_version: int,
        items: Sequence[DraftItem],
        now: str | datetime,
    ) -> ReviewDraft:
        self._require_user(principal)
        self._require_product(principal, product_id)
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
            principal.organization_id, principal.actor_id, product_id
        )
        if current.version != expected_version:
            raise DraftConflict("review_draft_conflict")
        for item in draft_items:
            self._validate_draft_item(principal, product_id, item)
        return self.store.replace_draft(
            current, draft_items, self._timestamp(now)
        )

    def _validate_draft_item(
        self, principal: Principal, product_id: str, item: DraftItem
    ) -> None:
        self._require_repository(principal, product_id, item.repository_id)
        candidate = self.queries.candidate_revision(
            principal,
            item.repository_id,
            item.family_id,
            item.revision_id,
        )
        if candidate is None:
            raise ProductOwnershipConflict()
        if (
            item.revision != candidate.revision
            or item.content_digest != candidate.content_digest
            or item.revision_id
            != candidate_revision_id(
                item.family_id, item.revision, item.content_digest
            )
        ):
            raise ProductOwnershipConflict()
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

    def _require_product(
        self, principal: Principal, product_id: str
    ) -> None:
        if not self.queries.product_repositories(principal, product_id):
            raise ProductNotFound()

    def _require_repository(
        self, principal: Principal, product_id: str, repository_id: str
    ) -> None:
        mapping = self.queries.repository_mapping(principal, repository_id)
        if mapping is None or not mapping.enabled:
            raise ProductNotFound()
        if mapping.product_id != product_id:
            raise ProductOwnershipConflict()

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
