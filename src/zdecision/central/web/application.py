"""Application boundary for the Central Decision Web."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from zdecision.central.auth import Principal
from zdecision.central.web.contracts import CandidateInboxView, DraftItem, ReviewDraft
from zdecision.central.web.queries import CentralWebQueries, DashboardView
from zdecision.central.web.previews import (
    CentralPreviewService,
    PublicationPreviewView,
)
from zdecision.central.web.reviews import (
    CentralReviewService,
    ReviewSubmissionResult,
)
from zdecision.central.web.store import CentralWebStore
from zdecision.registry.catalog import RegistryCatalog
from zdecision.registry.git import GitRegistryAdapter


class CentralWebApplication:
    def __init__(
        self,
        *,
        store: CentralWebStore,
        queries: CentralWebQueries,
        catalog: RegistryCatalog | None = None,
        git: GitRegistryAdapter | None = None,
    ) -> None:
        if not isinstance(store, CentralWebStore):
            raise TypeError("store must be a CentralWebStore")
        if not isinstance(queries, CentralWebQueries):
            raise TypeError("queries must be CentralWebQueries")
        self.store = store
        self.queries = queries
        self.reviews = CentralReviewService(store=store, queries=queries)
        if (catalog is None) != (git is None):
            raise ValueError("Preview Registry dependencies must be configured together")
        self.previews = (
            CentralPreviewService(
                store=store, queries=queries, catalog=catalog, git=git
            )
            if catalog is not None and git is not None
            else None
        )

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

    def submit_review(
        self,
        principal: Principal,
        product_id: str,
        client_action_id: str,
        expected_draft_version: int,
        items: Sequence[DraftItem],
        now: str | datetime,
    ) -> ReviewSubmissionResult:
        return self.reviews.submit(
            principal,
            product_id,
            client_action_id,
            expected_draft_version,
            items,
            now,
        )

    def create_preview(
        self,
        principal: Principal,
        review_batch_id: str,
        client_action_id: str,
        now: str | datetime,
    ) -> PublicationPreviewView:
        return self._preview_service().create(
            principal, review_batch_id, client_action_id, now
        )

    def get_preview(
        self, principal: Principal, preview_id: str
    ) -> PublicationPreviewView:
        return self._preview_service().get(principal, preview_id)

    def _preview_service(self) -> CentralPreviewService:
        if self.previews is None:
            raise RuntimeError("Central Preview service is not configured")
        return self.previews
