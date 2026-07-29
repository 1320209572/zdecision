"""Promotion boundary from private accepted Reviews to the formal Registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from zdecision.capture.models import CaptureRecord
from zdecision.capture.review_service import ReviewError, ReviewService
from zdecision.capture.reviews import ApprovalRef, ReviewBatch, ReviewItem
from zdecision.ids import (
    PUBLISHER_FORMAT_VERSION,
    canonical_product_name,
    decision_id,
    product_id,
    publication_preview_id,
)
from zdecision.private_store.filesystem import (
    FilePrivateStore,
    PrivateStateConflict,
)
from zdecision.registry.catalog import (
    DecisionUpdateNotSupported,
    RegistryCatalog,
    RegistryError,
)
from zdecision.registry.git import (
    GitRegistryAdapter,
    GitRegistryError,
    PublicationGitAmbiguous,
)
from zdecision.registry.models import DecisionSeed
from zdecision.registry.publication import (
    CandidatePublicationReceipt,
    PublicationFile,
    PublicationRecord,
    PublicationResult,
    content_digest_for_files,
)


class PublicationError(Exception):
    """Base class for sanitized publication failures."""


class PublicationNotFound(PublicationError):
    pass


class NoPublishableItems(PublicationError):
    pass


class ReviewSuperseded(PublicationError):
    pass


class PublicationStale(PublicationError):
    pass


class PublicationApprovalConflict(PublicationError):
    pass


class PublicationConfirmationRequired(PublicationError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class PromotionService:
    def __init__(
        self,
        store: FilePrivateStore,
        review_service: ReviewService,
        registry_catalog: RegistryCatalog,
        git_adapter: GitRegistryAdapter,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.store = store
        self.review_service = review_service
        self.registry_catalog = registry_catalog
        self.git_adapter = git_adapter
        self.clock = clock

    def get(self, preview_id: str) -> PublicationRecord:
        record = self.store.get_publication(preview_id)
        if record is None:
            raise PublicationNotFound(
                f"Publication preview {preview_id!r} does not exist"
            )
        return record

    def preview(self, review_batch_id: str) -> PublicationRecord:
        batch = self.review_service.get(review_batch_id)
        accepted = tuple(
            item
            for item in batch.items
            if item.action in ("accept", "edit_accept")
        )
        if not accepted:
            raise NoPublishableItems("Review batch has no accepted items")

        self._guard_existing_publications(accepted)
        self._require_latest(batch, accepted)
        for item in accepted:
            if self.store.get_candidate_receipt(item.candidate_id) is not None:
                raise DecisionUpdateNotSupported(
                    "A selected Candidate already owns a published Decision"
                )

        capture = self.store.get_capture(batch.capture_id)
        if not isinstance(capture, CaptureRecord) or capture.status != "completed":
            raise ReviewSuperseded("Review Capture is no longer publishable")

        first_content = accepted[0].content
        assert first_content is not None
        product_name = canonical_product_name(first_content.product)
        product_id_value = product_id(product_name)
        seeds = self._decision_seeds(
            accepted,
            product_name,
            product_id_value,
            capture,
            batch,
        )

        base_commit = self.git_adapter.fetch_and_require_exact_main()
        self.git_adapter.require_clean_registry()
        plan = self.registry_catalog.inspect(seeds)
        review_ids = tuple(item.review_id for item in accepted)
        decision_ids = tuple(seed.decision_id for seed in seeds)
        preview_id = publication_preview_id(
            {
                "base_commit": base_commit,
                "base_registry_digests": plan.base_registry_digests,
                "decision_ids": decision_ids,
                "publisher_format": PUBLISHER_FORMAT_VERSION,
                "review_ids": review_ids,
                "target_paths": plan.changed_paths,
            }
        )
        draft = self.registry_catalog.render(plan, preview_id)
        display_documents = tuple(
            PublicationFile.from_bytes(path, content)
            for path, content in draft.display_documents.items()
        )
        changed_files = tuple(
            PublicationFile.from_bytes(path, content)
            for path, content in draft.changed_files.items()
        )
        existing = self.store.get_publication(preview_id)
        created_at = existing.created_at if existing is not None else self.clock()
        record = PublicationRecord(
            record_version=1,
            preview_id=preview_id,
            content_digest=content_digest_for_files(display_documents),
            state="previewed",
            created_at=created_at,
            review_batch_id=batch.review_batch_id,
            review_ids=review_ids,
            candidate_ids=tuple(item.candidate_id for item in accepted),
            decision_ids=decision_ids,
            product_id=product_id_value,
            product_name=product_name,
            base_commit=base_commit,
            base_registry_digests=plan.base_registry_digests,
            display_documents=display_documents,
            changed_files=changed_files,
            commit_message=(
                f"decision({product_id_value}): publish "
                f"{len(decision_ids)} decisions\n\n"
                f"ZDecision-Preview: {preview_id}\n"
            ),
        )
        return self.store.create_publication(record)

    def confirm(
        self,
        preview_id: str,
        approval_thread_id: str,
        approval_turn_id: str,
    ) -> PublicationResult:
        self._validate_approval_identity(
            approval_thread_id,
            approval_turn_id,
        )
        record = self.get(preview_id)
        if record.state != "previewed":
            self._require_same_approval(
                record,
                approval_thread_id,
                approval_turn_id,
            )
            return self.resume(preview_id)

        try:
            self._require_preview_fresh(record)
        except (
            GitRegistryError,
            RegistryError,
            ReviewError,
            ReviewSuperseded,
        ) as exc:
            raise PublicationStale(
                f"Publication preview {preview_id!r} is stale"
            ) from exc

        approval = ApprovalRef(
            actor="user",
            thread_id=approval_thread_id,
            turn_id=approval_turn_id,
            recorded_at=self.clock(),
        )
        confirmed = replace(
            record,
            state="confirmed",
            publication_approval=approval,
        )
        try:
            self.store.replace_publication(record, confirmed)
        except PrivateStateConflict:
            current = self.get(preview_id)
            self._require_same_approval(
                current,
                approval_thread_id,
                approval_turn_id,
            )
        return self.resume(preview_id)

    def resume(self, preview_id: str) -> PublicationResult:
        record = self.get(preview_id)
        if record.state == "previewed":
            raise PublicationConfirmationRequired(
                f"Publication preview {preview_id!r} has not been confirmed"
            )
        if record.state == "confirmed":
            return self._resume_confirmed(record)
        if record.state == "committed_pending_push":
            return self._resume_pending(record)
        assert record.state == "completed"
        assert record.commit_sha is not None
        self._ensure_receipts(record, record.commit_sha)
        remote_state = self.git_adapter.publication_remote_state(
            record.commit_sha,
            record.base_commit,
        )
        if remote_state != "contains":
            raise PublicationGitAmbiguous(
                "Completed publication is no longer present on origin/main"
            )
        return self._result(record)

    def _guard_existing_publications(
        self,
        accepted: tuple[ReviewItem, ...],
    ) -> None:
        candidate_ids = tuple(item.candidate_id for item in accepted)
        for preview_id in self.store.publication_ids_for_candidates(candidate_ids):
            record = self.get(preview_id)
            if record.state in ("confirmed", "committed_pending_push"):
                self.resume(preview_id)

    def _require_latest(
        self,
        batch: ReviewBatch,
        accepted: tuple[ReviewItem, ...],
    ) -> None:
        latest = self.review_service.latest_items(batch.capture_id)
        if any(
            latest.get(item.candidate_id) != item
            for item in accepted
        ):
            raise ReviewSuperseded(
                "One or more accepted Review items have been superseded"
            )

    def _require_preview_fresh(self, record: PublicationRecord) -> None:
        batch = self.review_service.get(record.review_batch_id)
        accepted = tuple(
            item
            for item in batch.items
            if item.action in ("accept", "edit_accept")
        )
        if (
            tuple(item.review_id for item in accepted) != record.review_ids
            or tuple(item.candidate_id for item in accepted)
            != record.candidate_ids
        ):
            raise ReviewSuperseded("Publication Review batch changed")
        self._require_latest(batch, accepted)
        for candidate_id in record.candidate_ids:
            if self.store.get_candidate_receipt(candidate_id) is not None:
                raise ReviewSuperseded(
                    "A publication Candidate already has a receipt"
                )

        capture = self.store.get_capture(batch.capture_id)
        if not isinstance(capture, CaptureRecord) or capture.status != "completed":
            raise ReviewSuperseded("Publication Capture is unavailable")
        seeds = self._decision_seeds(
            accepted,
            record.product_name,
            record.product_id,
            capture,
            batch,
        )
        if tuple(seed.decision_id for seed in seeds) != record.decision_ids:
            raise ReviewSuperseded("Publication Decision identities changed")

        self.git_adapter.fetch_and_require_exact_main(record.base_commit)
        self.git_adapter.require_clean_registry()
        plan = self.registry_catalog.inspect(seeds)
        if (
            plan.product_id != record.product_id
            or plan.product_name != record.product_name
            or plan.decision_ids != record.decision_ids
            or dict(plan.base_registry_digests)
            != dict(record.base_registry_digests)
            or plan.changed_paths
            != tuple(file.path for file in record.changed_files)
        ):
            raise PublicationStale("Publication Registry plan changed")
        draft = self.registry_catalog.render(plan, record.preview_id)
        if (
            dict(draft.display_documents) != record.display_file_bytes()
            or dict(draft.changed_files) != record.changed_file_bytes()
        ):
            raise PublicationStale("Publication formal bytes changed")

    def _resume_confirmed(
        self,
        record: PublicationRecord,
    ) -> PublicationResult:
        reconciled = self.git_adapter.reconcile_exact_commit(
            record.base_commit,
            record.commit_message,
            record.changed_file_bytes(),
        )
        if reconciled is not None:
            return self._adopt_commit(
                record,
                reconciled.commit_sha,
                remote_contains=reconciled.remote_contains_commit,
            )

        exact_files = record.changed_file_bytes()
        self.git_adapter.require_clean_registry(exact_files)
        self.registry_catalog.write_exact(exact_files)
        commit_sha = self.git_adapter.commit_exact(
            record.base_commit,
            record.commit_message,
            exact_files,
        )
        return self._adopt_commit(
            record,
            commit_sha,
            remote_contains=False,
        )

    def _adopt_commit(
        self,
        confirmed: PublicationRecord,
        commit_sha: str,
        *,
        remote_contains: bool,
    ) -> PublicationResult:
        self._ensure_receipts(confirmed, commit_sha)
        pending = replace(
            confirmed,
            state="committed_pending_push",
            commit_sha=commit_sha,
        )
        pending = self.store.replace_publication(confirmed, pending)
        if remote_contains:
            return self._complete(pending)
        return self._resume_pending(pending)

    def _resume_pending(
        self,
        pending: PublicationRecord,
    ) -> PublicationResult:
        assert pending.commit_sha is not None
        self._ensure_receipts(pending, pending.commit_sha)
        remote_state = self.git_adapter.publication_remote_state(
            pending.commit_sha,
            pending.base_commit,
        )
        if remote_state == "base":
            self.git_adapter.push_exact(
                pending.commit_sha,
                pending.base_commit,
            )
        return self._complete(pending)

    def _complete(self, pending: PublicationRecord) -> PublicationResult:
        completed = replace(pending, state="completed")
        completed = self.store.replace_publication(pending, completed)
        return self._result(completed)

    def _ensure_receipts(
        self,
        record: PublicationRecord,
        commit_sha: str,
    ) -> None:
        approval = record.publication_approval
        assert approval is not None
        for candidate_id, decision_id_value in zip(
            record.candidate_ids,
            record.decision_ids,
            strict=True,
        ):
            self.store.put_candidate_receipt(
                CandidatePublicationReceipt(
                    candidate_id=candidate_id,
                    decision_id=decision_id_value,
                    product_id=record.product_id,
                    preview_id=record.preview_id,
                    commit_sha=commit_sha,
                    recorded_at=approval.recorded_at,
                )
            )

    @staticmethod
    def _result(record: PublicationRecord) -> PublicationResult:
        assert record.state in ("committed_pending_push", "completed")
        assert record.commit_sha is not None
        return PublicationResult(
            preview_id=record.preview_id,
            decision_ids=record.decision_ids,
            status=record.state,
            commit_sha=record.commit_sha,
        )

    @staticmethod
    def _validate_approval_identity(thread_id: str, turn_id: str) -> None:
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("Publication approval thread id must not be empty")
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("Publication approval turn id must not be empty")

    @staticmethod
    def _require_same_approval(
        record: PublicationRecord,
        thread_id: str,
        turn_id: str,
    ) -> None:
        approval = record.publication_approval
        if (
            approval is None
            or approval.thread_id != thread_id
            or approval.turn_id != turn_id
        ):
            raise PublicationApprovalConflict(
                "Publication was confirmed by a different native Turn"
            )

    @staticmethod
    def _decision_seeds(
        accepted: tuple[ReviewItem, ...],
        product_name: str,
        product_id_value: str,
        capture: CaptureRecord,
        batch: ReviewBatch,
    ) -> tuple[DecisionSeed, ...]:
        seeds: list[DecisionSeed] = []
        for item in accepted:
            content = item.content
            assert content is not None
            if canonical_product_name(content.product) != product_name:
                raise ReviewSuperseded(
                    "Accepted Review items no longer share one product"
                )
            seeds.append(
                DecisionSeed(
                    candidate_id=item.candidate_id,
                    decision_id=decision_id(item.candidate_id, product_id_value),
                    product_id=product_id_value,
                    product_name=product_name,
                    content=content,
                    source=capture.source,
                    review_approval=batch.approval,
                )
            )
        return tuple(seeds)
