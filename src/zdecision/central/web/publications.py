"""Explicit, crash-recoverable publication of one frozen product Preview."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from zdecision.capture.reviews import ApprovalRef
from zdecision.central.auth import Principal
from zdecision.central.web.contracts import CentralPublication, DecisionSpaceRef
from zdecision.central.web.previews import (
    CentralPreviewService,
    PreviewStale,
    RegistryUnavailable,
)
from zdecision.central.web.store import (
    CentralWebStore,
    WebActionConflict,
    WebRecordConflict,
    WebRecordCorrupt,
    immediate,
)
from zdecision.ids import central_publication_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.catalog import RegistryCatalog
from zdecision.registry.git import (
    GitRegistryAdapter,
    PublicationGitAmbiguous,
    RegistryPushFailed,
)
from zdecision.registry.publication import PublicationRecord


PublicHistoryState = Literal[
    "confirmed", "committed_pending_push", "completed", "ambiguous"
]


class CentralPublicationError(Exception):
    code = "publication_error"


class PublicationNotFound(CentralPublicationError):
    code = "not_found"


class CandidateAlreadyPublishing(CentralPublicationError):
    code = "candidate_already_publishing"


class PublicationAmbiguous(CentralPublicationError):
    code = "publication_ambiguous"


@dataclass(frozen=True)
class PublicationView:
    decision_space_id: str
    space: DecisionSpaceRef
    publication_id: str
    preview_id: str
    product_id: str
    product_name: str
    decision_count: int
    decision_ids: tuple[str, ...]
    actor_id: str
    approved_at: str
    state: PublicHistoryState
    recovery_code: str | None
    commit_sha: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_space_id": self.decision_space_id,
            "space": self.space.to_dict(),
            "publication_id": self.publication_id,
            "preview_id": self.preview_id,
            "product_id": self.product_id,
            "product_name": self.product_name,
            "decision_count": self.decision_count,
            "decision_ids": list(self.decision_ids),
            "actor_id": self.actor_id,
            "approved_at": self.approved_at,
            "state": self.state,
            "recovery_code": self.recovery_code,
            "commit_sha": self.commit_sha,
        }

    def to_safe_dict(self) -> dict[str, object]:
        value = self.to_dict()
        value.pop("product_id")
        value.pop("product_name")
        return value


@dataclass(frozen=True)
class PublicationHistory:
    items: tuple[PublicationView, ...]
    total: int
    limit: int
    offset: int

    def to_dict(self) -> dict[str, object]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
        }

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "items": [item.to_safe_dict() for item in self.items],
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
        }


class CentralPublicationService:
    """The sole mutation boundary from immutable Preview to exact Git proof."""

    def __init__(
        self,
        *,
        store: CentralWebStore,
        previews: CentralPreviewService,
        catalog: RegistryCatalog,
        git: GitRegistryAdapter,
        clock: Callable[[], str | datetime] | None = None,
    ) -> None:
        if not isinstance(store, CentralWebStore):
            raise TypeError("store must be a CentralWebStore")
        if not isinstance(previews, CentralPreviewService):
            raise TypeError("previews must be a CentralPreviewService")
        if not isinstance(catalog, RegistryCatalog):
            raise TypeError("catalog must be a RegistryCatalog")
        if not isinstance(git, GitRegistryAdapter):
            raise TypeError("git must be a GitRegistryAdapter")
        if catalog.repository_root != git.repository_root:
            raise ValueError("Registry roots do not match")
        self.store = store
        self.previews = previews
        self.catalog = catalog
        self.git = git
        self.clock = clock or (lambda: datetime.now(UTC))
        self.checkpoint: Callable[[str], None] = lambda _: None

    def confirm(
        self,
        principal: Principal,
        preview_id: str,
        client_action_id: str,
        now: str | datetime,
    ) -> CentralPublication:
        self._require_user(principal)
        timestamp = self._timestamp(now)
        request_digest = hashlib.sha256(
            canonical_json_bytes({"preview_id": preview_id})
        ).hexdigest()

        replay = self.store.action_result(
            principal.organization_id, principal.actor_id,
            "publish", client_action_id,
        )
        if replay is not None:
            return self._replay(principal, replay, request_digest)

        existing = self.store.get_publication_by_preview(
            principal.organization_id, preview_id
        )
        if existing is not None:
            self._require_owned(principal, existing)
            with immediate(self.store.connection):
                self.store.record_action(
                    principal.organization_id, principal.actor_id, "publish",
                    client_action_id, request_digest, existing.publication_id,
                    timestamp,
                )
            return self._resume(existing)

        view = self.previews.get(principal, preview_id)
        if view.publishability == "stale":
            raise PreviewStale("preview_stale")
        if view.publishability == "registry_unavailable":
            raise RegistryUnavailable("registry_unavailable")
        preview = view.record
        batch = self.store.get_review_batch_by_id(
            principal.organization_id, preview.review_batch_id
        )
        if batch is None or batch.actor_id != principal.actor_id:
            raise WebRecordCorrupt("publication_review")
        publication = CentralPublication(
            publication_id=central_publication_id(preview_id),
            organization_id=principal.organization_id,
            actor_id=principal.actor_id,
            decision_space_id=batch.decision_space_id,
            compatibility_product_id=preview.product_id,
            preview_id=preview_id,
            confirm_action_id=client_action_id,
            confirm_request_digest=request_digest,
            state="confirmed",
            approval=ApprovalRef(
                actor="user",
                thread_id=(
                    "web_publication_"
                    + central_publication_id(preview_id).removeprefix("plb_")
                ),
                turn_id=client_action_id,
                recorded_at=timestamp,
            ),
            commit_sha=None,
            recovery_code=None,
            created_at=timestamp,
            updated_at=timestamp,
        )
        try:
            with immediate(self.store.connection):
                concurrent = self.store.get_publication_by_preview(
                    principal.organization_id, preview_id
                )
                if concurrent is not None:
                    self._require_same_preview(concurrent, preview)
                    self._require_owned(principal, concurrent)
                    publication = concurrent
                else:
                    self.previews.require_current_central_state(
                        principal, preview
                    )
                    self.store.put_publication(publication)
                    self.store.claim_publication_families(
                        publication, self._family_ids(publication, preview)
                    )
                self.store.record_action(
                    principal.organization_id, principal.actor_id, "publish",
                    client_action_id, request_digest,
                    publication.publication_id, timestamp,
                )
        except WebRecordConflict as error:
            if str(error) == "publication_family_conflict":
                raise CandidateAlreadyPublishing(
                    "candidate_already_publishing"
                ) from None
            raise
        self.checkpoint("after_confirmation")
        return self._resume(publication)

    def resume(
        self,
        principal: Principal,
        publication_id: str,
        client_action_id: str,
        now: str | datetime,
    ) -> CentralPublication:
        self._require_user(principal)
        timestamp = self._timestamp(now)
        request_digest = hashlib.sha256(
            canonical_json_bytes({"publication_id": publication_id})
        ).hexdigest()
        replay = self.store.action_result(
            principal.organization_id, principal.actor_id,
            "resume", client_action_id,
        )
        if replay is not None:
            if replay.request_digest != request_digest:
                raise WebActionConflict("web_action_conflict")
            if replay.result_id != publication_id:
                raise WebRecordCorrupt("resume_action_result")
        else:
            publication = self.store.get_publication(
                principal.organization_id, publication_id
            )
            if publication is None:
                raise PublicationNotFound("not_found")
            self._require_owned(principal, publication)
            with immediate(self.store.connection):
                self.store.record_action(
                    principal.organization_id, principal.actor_id, "resume",
                    client_action_id, request_digest, publication_id, timestamp,
                )
        publication = self.store.get_publication(
            principal.organization_id, publication_id
        )
        if publication is None:
            raise PublicationNotFound("not_found")
        self._require_owned(principal, publication)
        return self._resume(publication)

    def get(self, principal: Principal, publication_id: str) -> PublicationView:
        self._require_user(principal)
        publication = self.store.get_publication(
            principal.organization_id, publication_id
        )
        if publication is None:
            raise PublicationNotFound("not_found")
        if self.previews.queries.decision_space(
            principal, publication.decision_space_id
        ) is None:
            raise PublicationNotFound("not_found")
        return self._view(principal, publication)

    def list(
        self,
        principal: Principal,
        *,
        product_id: str | None = None,
        state: PublicHistoryState | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PublicationHistory:
        self._require_user(principal)
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("publication pagination is invalid")
        space = (
            self.previews.queries.decision_space(principal, product_id)
            if product_id is not None else None
        )
        if product_id is not None and (
            space is None
            or not self.previews.queries.decision_space_repositories(
                principal, space.decision_space_id
            )
        ):
            raise PublicationNotFound("not_found")
        publications, total = self.store.list_publications(
            principal.organization_id,
            decision_space_id=(space.decision_space_id if space else None),
            state=state,
            limit=limit,
            offset=offset,
        )
        visible = tuple(
            publication
            for publication in publications
            if self.previews.queries.decision_space_repositories(
                principal, publication.decision_space_id
            )
        )
        return PublicationHistory(
            tuple(self._view(principal, publication) for publication in visible),
            len(visible) if product_id is None else total,
            limit,
            offset,
        )

    def _replay(self, principal, replay, request_digest) -> CentralPublication:
        if replay.request_digest != request_digest:
            raise WebActionConflict("web_action_conflict")
        publication = self.store.get_publication(
            principal.organization_id, replay.result_id
        )
        if publication is None or hashlib.sha256(canonical_json_bytes(
            {"preview_id": publication.preview_id}
        )).hexdigest() != replay.request_digest:
            raise WebRecordCorrupt("publish_action_result")
        self._require_owned(principal, publication)
        return self._resume(publication)

    def _resume(self, publication: CentralPublication) -> CentralPublication:
        if publication.recovery_code == "ambiguous":
            raise PublicationAmbiguous("publication_ambiguous")
        if publication.state == "completed":
            return publication
        try:
            if publication.state == "confirmed":
                publication = self._resume_confirmed(publication)
            return self._complete_or_push(publication)
        except PublicationGitAmbiguous:
            self._latch_ambiguous(publication)
            raise PublicationAmbiguous("publication_ambiguous") from None

    def _resume_confirmed(
        self, publication: CentralPublication
    ) -> CentralPublication:
        preview = self._preview(publication)
        reconciled = self.git.reconcile_exact_commit(
            preview.base_commit, preview.commit_message,
            preview.changed_file_bytes(),
        )
        if reconciled is None:
            self.git.require_clean_registry(preview.changed_file_bytes())
            self.catalog.write_exact(preview.changed_file_bytes())
            self.checkpoint("before_commit")
            commit_sha = self.git.commit_exact(
                preview.base_commit, preview.commit_message,
                preview.changed_file_bytes(),
            )
        else:
            commit_sha = reconciled.commit_sha
        self.checkpoint("after_commit")
        pending = replace(
            publication,
            state="committed_pending_push",
            commit_sha=commit_sha,
            updated_at=self._timestamp(self.clock()),
        )
        with immediate(self.store.connection):
            self.store.put_family_receipts(publication, preview, commit_sha)
            pending = self.store.replace_publication(publication, pending)
        return pending

    def _complete_or_push(
        self, publication: CentralPublication
    ) -> CentralPublication:
        if publication.state != "committed_pending_push" or publication.commit_sha is None:
            raise WebRecordCorrupt("publication")
        preview = self._preview(publication)
        remote_state = self.git.publication_remote_state(
            publication.commit_sha, preview.base_commit
        )
        if remote_state == "base":
            try:
                self.git.push_exact(publication.commit_sha, preview.base_commit)
            except RegistryPushFailed:
                return publication
            self.checkpoint("after_push")
        elif remote_state != "contains":
            raise PublicationGitAmbiguous("Publication remote state is invalid")
        completed = replace(
            publication,
            state="completed",
            updated_at=self._timestamp(self.clock()),
        )
        with immediate(self.store.connection):
            return self.store.replace_publication(publication, completed)

    def _latch_ambiguous(self, publication: CentralPublication) -> None:
        current = self.store.get_publication(
            publication.organization_id, publication.publication_id
        )
        if current is None:
            raise WebRecordCorrupt("publication")
        if current.recovery_code == "ambiguous":
            return
        with immediate(self.store.connection):
            self.store.replace_publication(
                current,
                replace(
                    current,
                    recovery_code="ambiguous",
                    updated_at=self._timestamp(self.clock()),
                ),
            )

    def _preview(self, publication: CentralPublication) -> PublicationRecord:
        preview = self.store.get_preview(
            publication.organization_id, publication.preview_id
        )
        if (
            preview is None
            or preview.product_id != publication.compatibility_product_id
        ):
            raise WebRecordCorrupt("publication_preview")
        batch = self.store.get_review_batch(
            publication.organization_id,
            publication.decision_space_id,
            preview.review_batch_id,
        )
        if batch is None:
            raise WebRecordCorrupt("publication_preview")
        return preview

    def _family_ids(
        self, publication: CentralPublication, preview: PublicationRecord
    ) -> tuple[str, ...]:
        batch = self.store.get_review_batch(
            publication.organization_id, publication.decision_space_id,
            preview.review_batch_id,
        )
        if batch is None or batch.actor_id != publication.actor_id:
            raise WebRecordCorrupt("publication_review")
        by_candidate = {
            item.publication_candidate_id: item.family_id for item in batch.items
        }
        try:
            return tuple(by_candidate[value] for value in preview.candidate_ids)
        except KeyError:
            raise WebRecordCorrupt("publication_review") from None

    def _view(
        self, principal: Principal, publication: CentralPublication
    ) -> PublicationView:
        preview = self._preview(publication)
        space = self.previews.queries.decision_space_ref(
            principal, publication.decision_space_id
        )
        if space is None:
            raise PublicationNotFound("not_found")
        state: PublicHistoryState = (
            "ambiguous"
            if publication.recovery_code == "ambiguous"
            else publication.state
        )
        return PublicationView(
            publication.decision_space_id,
            space,
            publication.publication_id,
            publication.preview_id,
            publication.product_id,
            preview.product_name,
            len(preview.decision_ids),
            preview.decision_ids,
            publication.actor_id,
            publication.approval.recorded_at,
            state,
            publication.recovery_code,
            publication.commit_sha,
        )

    def _require_same_preview(
        self, publication: CentralPublication, preview: PublicationRecord
    ) -> None:
        batch = self.store.get_review_batch_by_id(
            publication.organization_id, preview.review_batch_id
        )
        if (
            publication.preview_id != preview.preview_id
            or publication.compatibility_product_id != preview.product_id
            or batch is None
            or publication.decision_space_id != batch.decision_space_id
        ):
            raise WebRecordConflict("publication_preview_conflict")

    @staticmethod
    def _require_owned(
        principal: Principal, publication: CentralPublication
    ) -> None:
        if (
            publication.organization_id != principal.organization_id
            or publication.actor_id != principal.actor_id
        ):
            raise PublicationNotFound("not_found")

    @staticmethod
    def _require_user(principal: Principal) -> None:
        if not isinstance(principal, Principal) or principal.kind != "user":
            raise ValueError("A browser user Principal is required")

    @staticmethod
    def _timestamp(value: str | datetime) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("now must be timezone-aware")
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError("now is invalid")
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
        return value
