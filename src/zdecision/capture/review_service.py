"""Application service for atomic private Candidate Review batches."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone

from zdecision.capture.models import (
    Candidate,
    CandidateContent,
    CaptureRecord,
    LegacyCaptureRecord,
)
from zdecision.capture.reviews import (
    ApprovalRef,
    ReviewBatch,
    ReviewItem,
    ReviewSelection,
)
from zdecision.ids import (
    canonical_product_name,
    review_batch_id,
    review_item_id,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.private_store.filesystem import (
    FilePrivateStore,
    PrivateStateConflict,
    PrivateStateCorrupt,
)


class ReviewError(Exception):
    """Base class for sanitized Review service failures."""


class ReviewNotFound(ReviewError):
    pass


class CaptureNotReviewable(ReviewError):
    pass


class InvalidReview(ReviewError):
    pass


class ReviewConflict(ReviewError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _persisted_candidate(candidate: Candidate) -> dict[str, object]:
    return {
        "product": candidate.content.product,
        "claim": candidate.content.claim,
        "future_action": candidate.content.future_action,
        "scope": {
            "summary": candidate.content.scope_summary,
            "repositories": list(candidate.content.repositories),
            "paths": list(candidate.content.paths),
        },
        "invalidation_conditions": list(candidate.content.invalidation_conditions),
    }


class ReviewService:
    def __init__(
        self,
        store: FilePrivateStore,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.store = store
        self.clock = clock

    def record(
        self,
        capture_id: str,
        selections: Sequence[ReviewSelection],
        approval_thread_id: str,
        approval_turn_id: str,
    ) -> ReviewBatch:
        previous_approval = self.store.review_batch_for_approval(
            approval_thread_id,
            approval_turn_id,
        )
        if (
            previous_approval is not None
            and previous_approval.capture_id != capture_id
        ):
            raise ReviewConflict(
                "The approval Turn already owns a different Review batch"
            )

        candidates = self._reviewable_candidates(capture_id)
        validated_selections = self._validated_selections(selections, candidates)
        item_inputs = self._effective_item_inputs(validated_selections, candidates)
        batch_id = review_batch_id(
            capture_id,
            tuple(identity for _, identity, _ in item_inputs),
            approval_thread_id,
            approval_turn_id,
        )
        if previous_approval is not None:
            if previous_approval.review_batch_id == batch_id:
                return previous_approval
            raise ReviewConflict(
                "The approval Turn already owns different Review content"
            )

        prior_batches = tuple(
            self._required_batch(batch_id_value)
            for batch_id_value in self.store.review_batch_ids_for_capture(capture_id)
        )
        sequence = max((batch.sequence for batch in prior_batches), default=0) + 1
        approval = ApprovalRef(
            actor="user",
            thread_id=approval_thread_id,
            turn_id=approval_turn_id,
            recorded_at=self.clock(),
        )
        items = tuple(
            ReviewItem(
                review_id=review_item_id(batch_id, selection.candidate_id),
                candidate_id=selection.candidate_id,
                action=selection.action,
                content=content,
            )
            for selection, _, content in item_inputs
        )
        batch = ReviewBatch(
            review_batch_id=batch_id,
            capture_id=capture_id,
            sequence=sequence,
            approval=approval,
            items=items,
        )
        try:
            self.store.put_review_batch(batch)
        except PrivateStateConflict as exc:
            raise ReviewConflict("Review batch private state conflicts") from exc
        return batch

    def get(self, batch_id: str) -> ReviewBatch:
        batch = self.store.get_review_batch(batch_id)
        if batch is None:
            raise ReviewNotFound(f"Review batch {batch_id!r} does not exist")
        return batch

    def latest_items(self, capture_id: str) -> Mapping[str, ReviewItem]:
        self._reviewable_candidates(capture_id)
        batches = sorted(
            (
                self._required_batch(batch_id)
                for batch_id in self.store.review_batch_ids_for_capture(capture_id)
            ),
            key=lambda batch: batch.sequence,
        )
        latest: dict[str, ReviewItem] = {}
        for batch in batches:
            for item in batch.items:
                latest[item.candidate_id] = item
        return latest

    def _required_batch(self, batch_id: str) -> ReviewBatch:
        batch = self.store.get_review_batch(batch_id)
        if batch is None:
            raise PrivateStateCorrupt("review_batches", batch_id)
        return batch

    def _reviewable_candidates(self, capture_id: str) -> dict[str, Candidate]:
        record = self.store.get_capture(capture_id)
        if (
            record is None
            or isinstance(record, LegacyCaptureRecord)
            or not isinstance(record, CaptureRecord)
            or record.status != "completed"
        ):
            raise CaptureNotReviewable(
                f"Capture {capture_id!r} is not a completed V2 Capture"
            )
        if record.extraction_turn_id is None or record.extraction_sha256 is None:
            raise PrivateStateCorrupt("captures", capture_id)
        manifest = self.store.get_extraction_manifest(capture_id)
        if (
            manifest is None
            or manifest.operation_id != capture_id
            or manifest.extraction_turn_id != record.extraction_turn_id
            or manifest.extraction_sha256 != record.extraction_sha256
            or manifest.candidate_ids != record.candidate_ids
        ):
            raise PrivateStateCorrupt("extraction_manifests", capture_id)
        if self.store.candidate_ids_for_capture(capture_id) != record.candidate_ids:
            raise PrivateStateCorrupt("candidates", capture_id)

        candidates: list[Candidate] = []
        for ordinal, candidate_id in enumerate(record.candidate_ids, start=1):
            candidate = self.store.get_candidate(candidate_id)
            if candidate is None:
                raise PrivateStateCorrupt("candidates", candidate_id)
            if (
                candidate.capture_id != capture_id
                or candidate.ordinal != ordinal
                or candidate.source != record.source
                or candidate.content.product != record.product
            ):
                raise PrivateStateCorrupt("candidates", candidate_id)
            candidates.append(candidate)
        extraction_digest = hashlib.sha256(
            canonical_json_bytes(
                {"candidates": [_persisted_candidate(item) for item in candidates]}
            )
        ).hexdigest()
        if extraction_digest != record.extraction_sha256:
            raise PrivateStateCorrupt("extraction_manifests", capture_id)
        return {candidate.candidate_id: candidate for candidate in candidates}

    def _validated_selections(
        self,
        selections: Sequence[ReviewSelection],
        candidates: Mapping[str, Candidate],
    ) -> tuple[ReviewSelection, ...]:
        if isinstance(selections, (str, bytes)) or not isinstance(
            selections, Sequence
        ):
            raise InvalidReview("Review selections must be a sequence")
        if not 1 <= len(selections) <= 20:
            raise InvalidReview("Review must contain between 1 and 20 selections")
        if any(not isinstance(selection, ReviewSelection) for selection in selections):
            raise InvalidReview("Review contains an invalid selection")
        candidate_ids = [selection.candidate_id for selection in selections]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise InvalidReview("Review contains a duplicate Candidate")
        if any(candidate_id not in candidates for candidate_id in candidate_ids):
            raise InvalidReview("Review references a Candidate outside its Capture")
        return tuple(selections)

    def _effective_item_inputs(
        self,
        selections: tuple[ReviewSelection, ...],
        candidates: Mapping[str, Candidate],
    ) -> tuple[
        tuple[ReviewSelection, dict[str, object], CandidateContent | None],
        ...,
    ]:
        result: list[
            tuple[ReviewSelection, dict[str, object], CandidateContent | None]
        ] = []
        accepted_products: set[str] = set()
        for selection in selections:
            candidate = candidates[selection.candidate_id]
            content = None
            if selection.action in ("accept", "edit_accept"):
                selected_content = (
                    candidate.content
                    if selection.action == "accept"
                    else selection.content
                )
                assert selected_content is not None
                candidate_product = canonical_product_name(candidate.content.product)
                selected_product = canonical_product_name(selected_content.product)
                if selected_product != candidate_product:
                    raise InvalidReview("Review cannot change a Candidate product")
                content = replace(selected_content, product=candidate_product)
                accepted_products.add(candidate_product)
            identity = {
                "candidate_id": selection.candidate_id,
                "action": selection.action,
                "effective_content": (
                    content.to_dict() if content is not None else None
                ),
            }
            result.append((selection, identity, content))
        if len(accepted_products) > 1:
            raise InvalidReview("Accepted Review items must use one product")
        return tuple(result)
