"""Promotion boundary from private accepted Reviews to the formal Registry."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from zdecision.capture.models import CaptureRecord
from zdecision.capture.review_service import ReviewService
from zdecision.capture.reviews import ReviewBatch, ReviewItem
from zdecision.ids import (
    PUBLISHER_FORMAT_VERSION,
    canonical_product_name,
    decision_id,
    product_id,
    publication_preview_id,
)
from zdecision.private_store.filesystem import FilePrivateStore
from zdecision.registry.catalog import (
    DecisionUpdateNotSupported,
    RegistryCatalog,
)
from zdecision.registry.git import GitRegistryAdapter
from zdecision.registry.models import DecisionSeed
from zdecision.registry.publication import (
    PublicationFile,
    PublicationRecord,
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


class PublicationInProgress(PublicationError):
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

    def _guard_existing_publications(
        self,
        accepted: tuple[ReviewItem, ...],
    ) -> None:
        candidate_ids = tuple(item.candidate_id for item in accepted)
        for preview_id in self.store.publication_ids_for_candidates(candidate_ids):
            record = self.get(preview_id)
            if record.state in ("confirmed", "committed_pending_push"):
                raise PublicationInProgress(
                    "An earlier publication must be reconciled before a new preview"
                )

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
