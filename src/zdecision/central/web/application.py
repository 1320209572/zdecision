"""Application boundary for the Central Decision Web."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from zdecision.central.auth import Principal
from zdecision.central.web.contracts import CandidateInboxView, DraftItem, ReviewDraft
from zdecision.central.web.queries import CentralWebQueries, DashboardView
from zdecision.central.web.reviews import CentralReviewService
from zdecision.central.web.store import CentralWebStore


class CentralWebApplication:
    def __init__(
        self, *, store: CentralWebStore, queries: CentralWebQueries
    ) -> None:
        if not isinstance(store, CentralWebStore):
            raise TypeError("store must be a CentralWebStore")
        if not isinstance(queries, CentralWebQueries):
            raise TypeError("queries must be CentralWebQueries")
        self.store = store
        self.queries = queries
        self.reviews = CentralReviewService(store=store, queries=queries)

    def dashboard(self, principal: Principal) -> DashboardView:
        return self.queries.dashboard(principal)

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
        return self.reviews.list_candidates(
            principal,
            product_id,
            search=search,
            repository_id=repository_id,
            capture_request_id=capture_request_id,
            state=state,
            limit=limit,
            offset=offset,
        )

    def get_review_draft(
        self, principal: Principal, product_id: str
    ) -> ReviewDraft:
        return self.reviews.get_draft(principal, product_id)

    def save_review_draft(
        self,
        principal: Principal,
        product_id: str,
        expected_version: int,
        items: Sequence[DraftItem],
        now: str | datetime,
    ) -> ReviewDraft:
        return self.reviews.save_draft(
            principal, product_id, expected_version, items, now
        )
