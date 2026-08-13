"""Application boundary for the Central Decision Web."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from zdecision.central.auth import Principal
from zdecision.central.registry_projection import (
    RegistryProjectionError,
    RegistryProjectionSynchronizer,
)
from zdecision.central.web.contracts import (
    CandidateInboxView,
    CentralPublication,
    DraftItem,
    ReviewDraft,
)
from zdecision.central.web.queries import (
    CentralWebQueries,
    DashboardView,
    DecisionDetailView,
    DecisionListView,
    DecisionRegistryUnavailable,
    RepositorySpacesView,
)
from zdecision.central.web.previews import (
    CentralPreviewService,
    PublicationPreviewView,
)
from zdecision.central.web.publications import (
    CentralPublicationService,
    PublicationHistory,
    PublicationView,
    PublicHistoryState,
)
from zdecision.central.web.reviews import (
    CentralReviewService,
    DecisionSpaceNotFound,
    DecisionSpaceNotLeaf,
    ReviewSubmissionResult,
)
from zdecision.central.web.store import CentralWebStore
from zdecision.registry.catalog import RegistryCatalog
from zdecision.registry.git import GitRegistryAdapter
from zdecision.recall.demo.publication import (
    DemoBundlePublisher,
    RecallDemoPublicationError,
)


class RecallDemoRefreshFailed(RuntimeError):
    pass


class CentralWebApplication:
    def __init__(
        self,
        *,
        store: CentralWebStore,
        queries: CentralWebQueries,
        catalog: RegistryCatalog | None = None,
        git: GitRegistryAdapter | None = None,
        registry_synchronizer: RegistryProjectionSynchronizer | None = None,
        recall_demo_publisher: DemoBundlePublisher | None = None,
    ) -> None:
        if not isinstance(store, CentralWebStore):
            raise TypeError("store must be a CentralWebStore")
        if not isinstance(queries, CentralWebQueries):
            raise TypeError("queries must be CentralWebQueries")
        self.store = store
        self.queries = queries
        self.reviews = CentralReviewService(store=store, queries=queries)
        configured = (
            catalog is not None,
            git is not None,
            registry_synchronizer is not None,
        )
        if any(configured) and not all(configured):
            raise ValueError("Preview Registry dependencies must be configured together")
        self.registry_synchronizer = registry_synchronizer
        self.recall_demo_publisher = recall_demo_publisher
        self.previews = (
            CentralPreviewService(
                store=store, queries=queries, catalog=catalog, git=git
            )
            if catalog is not None and git is not None
            else None
        )
        self.publications = (
            CentralPublicationService(
                store=store,
                previews=self.previews,
                catalog=catalog,
                git=git,
            )
            if self.previews is not None and catalog is not None and git is not None
            else None
        )

    def dashboard(self, principal: Principal) -> DashboardView:
        return self.queries.dashboard(principal)

    def list_decisions(
        self,
        principal: Principal,
        *,
        product_id: str | None = None,
        decision_space_id: str | None = None,
        search: str = "",
        repository: str = "",
        published_after: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> DecisionListView:
        view = self.queries.list_decisions(
            principal,
            product_id=product_id,
            decision_space_id=decision_space_id,
            search=search,
            repository=repository,
            published_after=published_after,
            limit=limit,
            offset=offset,
        )
        if view.registry_state == "unavailable":
            raise DecisionRegistryUnavailable("registry_unavailable")
        return view

    def get_decision(
        self, principal: Principal, decision_space_id: str, decision_id: str
    ) -> DecisionDetailView:
        return self.queries.get_decision(
            principal, decision_space_id, decision_id
        )

    def repository_spaces(
        self, principal: Principal, repository_id: str
    ) -> RepositorySpacesView:
        return self.queries.repository_spaces(principal, repository_id)

    def require_canonical_leaf(
        self, principal: Principal, decision_space_id: str
    ) -> None:
        space = self.queries.decision_space(principal, decision_space_id)
        if space is not None and space.decision_space_id == decision_space_id:
            return
        if self.queries.catalog_group_exists(principal, decision_space_id):
            raise DecisionSpaceNotLeaf()
        raise DecisionSpaceNotFound()

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
        return self.reviews.list_candidates(
            principal,
            decision_space_id,
            search=search,
            repository_id=repository_id,
            capture_request_id=capture_request_id,
            state=state,
            limit=limit,
            offset=offset,
        )

    def get_review_draft(
        self, principal: Principal, decision_space_id: str
    ) -> ReviewDraft:
        return self.reviews.get_draft(principal, decision_space_id)

    def save_review_draft(
        self,
        principal: Principal,
        decision_space_id: str,
        expected_version: int,
        items: Sequence[DraftItem],
        now: str | datetime,
    ) -> ReviewDraft:
        return self.reviews.save_draft(
            principal, decision_space_id, expected_version, items, now
        )

    def submit_review(
        self,
        principal: Principal,
        decision_space_id: str,
        client_action_id: str,
        expected_draft_version: int,
        items: Sequence[DraftItem],
        now: str | datetime,
    ) -> ReviewSubmissionResult:
        return self.reviews.submit(
            principal,
            decision_space_id,
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

    def publish(
        self,
        principal: Principal,
        preview_id: str,
        client_action_id: str,
        now: str | datetime,
    ) -> PublicationView:
        publication = self._publication_service().confirm(
            principal, preview_id, client_action_id, now
        )
        self._synchronize_completed_publication(publication)
        return self._publication_service().get(
            principal, publication.publication_id
        )

    def resume_publication(
        self,
        principal: Principal,
        publication_id: str,
        client_action_id: str,
        now: str | datetime,
    ) -> PublicationView:
        publication = self._publication_service().resume(
            principal, publication_id, client_action_id, now
        )
        self._synchronize_completed_publication(publication)
        return self._publication_service().get(
            principal, publication.publication_id
        )

    def get_publication(
        self, principal: Principal, publication_id: str
    ) -> PublicationView:
        return self._publication_service().get(principal, publication_id)

    def list_publications(
        self,
        principal: Principal,
        *,
        product_id: str | None,
        decision_space_id: str | None,
        state: PublicHistoryState | None,
        limit: int,
        offset: int,
    ) -> PublicationHistory:
        return self._publication_service().list(
            principal,
            product_id=product_id,
            decision_space_id=decision_space_id,
            state=state,
            limit=limit,
            offset=offset,
        )

    def _preview_service(self) -> CentralPreviewService:
        if self.previews is None:
            raise RuntimeError("Central Preview service is not configured")
        return self.previews

    def _publication_service(self) -> CentralPublicationService:
        if self.publications is None:
            raise RuntimeError("Central Publication service is not configured")
        return self.publications

    def _synchronize_completed_publication(
        self,
        publication: CentralPublication,
    ) -> None:
        if publication.state != "completed" or publication.commit_sha is None:
            return
        if self.registry_synchronizer is None:
            raise RuntimeError("Registry synchronizer is not configured")
        try:
            self.registry_synchronizer.synchronize(
                publication.organization_id,
                publication.commit_sha,
                publication.updated_at,
            )
        except RegistryProjectionError:
            return
        if self.recall_demo_publisher is not None:
            try:
                self.recall_demo_publisher.refresh(publication.commit_sha)
            except RecallDemoPublicationError:
                raise RecallDemoRefreshFailed(
                    "recall_demo_refresh_failed"
                ) from None
