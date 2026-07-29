from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.test_inventory import VALID_INVENTORY
from zdecision.capture.models import CandidateContent, LegacyCaptureRecord, SourceCheckpoint
from zdecision.capture.review_service import (
    CaptureNotReviewable,
    InvalidReview,
    ReviewConflict,
    ReviewNotFound,
    ReviewService,
)
from zdecision.capture.reviews import (
    ApprovalRef,
    ReviewBatch,
    ReviewItem,
    ReviewSelection,
)
from zdecision.ids import (
    canonical_product_name,
    decision_id,
    product_id,
    publication_preview_id,
    review_batch_id,
    review_item_id,
)
from zdecision.jsonio import atomic_write_json, canonical_json_bytes
from zdecision.private_store.filesystem import (
    FilePrivateStore,
    InvalidPrivateObjectId,
    PrivateStateConflict,
    PrivateStateCorrupt,
)


CAPTURE_ID = "cap_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CANDIDATE_ID = "cand_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_01"
REVIEW_BATCH_ID = "rvb_39a385f527e697a9e40ce1105a7dd8b0"
REVIEW_ID = "rvi_a36b19360a8986f487bca806900ac678"
PRODUCT_ID = "prod_4d7b16e1616dd4cd1aeb2411836fd687"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "decision-templates"
ENVELOPE_ROOT = (
    REPOSITORY_ROOT / "src" / "zdecision" / "capture" / "prompt_contracts"
)


def _content_dict() -> dict[str, object]:
    return {
        "product": "安恒",
        "claim": "正式决策按产品隔离保存。",
        "future_action": "新增决策时写入对应产品目录。",
        "scope_summary": "ZDecision Registry 的正式存储布局",
        "repositories": ["https://github.com/1320209572/zdecision.git"],
        "paths": ["decision-registry/"],
        "invalidation_conditions": ["产品隔离策略被新的正式决策替代"],
    }


def _content() -> CandidateContent:
    return CandidateContent.from_dict(_content_dict())


def _approval(recorded_at: str = "2026-07-29T00:00:00Z") -> ApprovalRef:
    return ApprovalRef(
        actor="user",
        thread_id="thread-review",
        turn_id="turn-review",
        recorded_at=recorded_at,
    )


def _accepted_item() -> ReviewItem:
    return ReviewItem(
        review_id=REVIEW_ID,
        candidate_id=CANDIDATE_ID,
        action="accept",
        content=_content(),
    )


class ReviewValueTests(unittest.TestCase):
    def test_product_identity_normalizes_nfc_and_surrounding_whitespace(self) -> None:
        decomposed = "  安\u6052\u0301  "
        composed = "安恒\u0301"

        self.assertEqual(composed, canonical_product_name(decomposed))
        self.assertEqual(product_id(composed), product_id(decomposed))
        self.assertEqual(PRODUCT_ID, product_id("安恒"))

    def test_product_identity_is_case_sensitive_and_rejects_controls(self) -> None:
        self.assertNotEqual(product_id("ZDecision"), product_id("zdecision"))
        for invalid in ("", "  ", "安\n恒", "安\u0000恒"):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(ValueError):
                    canonical_product_name(invalid)

    def test_stable_ids_match_hand_checked_canonical_fixtures(self) -> None:
        identity_item = {
            "candidate_id": CANDIDATE_ID,
            "action": "accept",
            "effective_content": _content_dict(),
        }
        batch_id = review_batch_id(
            CAPTURE_ID,
            (identity_item,),
            "thread-review",
            "turn-review",
        )

        self.assertEqual(REVIEW_BATCH_ID, batch_id)
        self.assertEqual(REVIEW_ID, review_item_id(batch_id, CANDIDATE_ID))
        self.assertEqual(
            "dec_feb59baaa4c45aa371d0d07606f61dae",
            decision_id(CANDIDATE_ID, PRODUCT_ID),
        )
        self.assertEqual(
            "pub_430c44758d880af3455dd5eaacc0bedf",
            publication_preview_id(
                {
                    "base_commit": "a" * 40,
                    "base_registry_digests": {
                        "decision-registry/registry.json": "b" * 64
                    },
                    "decision_ids": [
                        "dec_1688901ff46d9f556b9fe6c4d3283d81"
                    ],
                    "publisher_format": "zdecision-publisher/v1",
                    "review_ids": [
                        "rvi_44444444444444444444444444444444"
                    ],
                    "target_paths": ["decision-registry/registry.json"],
                }
            ),
        )

    def test_review_batch_identity_is_order_sensitive_but_not_time_sensitive(
        self,
    ) -> None:
        first = {
            "candidate_id": CANDIDATE_ID,
            "action": "accept",
            "effective_content": _content_dict(),
        }
        second = {
            "candidate_id": "cand_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_02",
            "action": "skip",
            "effective_content": None,
        }

        forward = review_batch_id(
            CAPTURE_ID,
            (first, second),
            "thread-review",
            "turn-review",
        )
        reverse = review_batch_id(
            CAPTURE_ID,
            (second, first),
            "thread-review",
            "turn-review",
        )

        self.assertNotEqual(forward, reverse)
        self.assertEqual(
            REVIEW_BATCH_ID,
            review_batch_id(
                CAPTURE_ID,
                (first,),
                _approval("2026-07-29T00:00:00Z").thread_id,
                _approval("2027-01-01T00:00:00Z").turn_id,
            ),
        )

    def test_stable_id_functions_reject_malformed_inputs(self) -> None:
        valid_item = {
            "candidate_id": CANDIDATE_ID,
            "action": "accept",
            "effective_content": _content_dict(),
        }
        invalid_items = (
            {},
            {**valid_item, "extra": True},
            {**valid_item, "action": "approve"},
            {**valid_item, "effective_content": None},
            {
                "candidate_id": CANDIDATE_ID,
                "action": "skip",
                "effective_content": _content_dict(),
            },
        )
        for item in invalid_items:
            with self.subTest(item=item):
                with self.assertRaises(ValueError):
                    review_batch_id(
                        CAPTURE_ID,
                        (item,),
                        "thread-review",
                        "turn-review",
                    )

        with self.assertRaises(ValueError):
            review_item_id("rvb_bad", CANDIDATE_ID)
        with self.assertRaises(ValueError):
            review_item_id(REVIEW_BATCH_ID, "candidate_bad")
        with self.assertRaises(ValueError):
            decision_id(CANDIDATE_ID, "prod_bad")
        with self.assertRaises(ValueError):
            decision_id("candidate_bad", PRODUCT_ID)
        with self.assertRaises(ValueError):
            publication_preview_id({"base_commit": "a" * 40})

    def test_approval_ref_is_strict_and_round_trips(self) -> None:
        value = {
            "actor": "user",
            "thread_id": "thread-review",
            "turn_id": "turn-review",
            "recorded_at": "2026-07-29T00:00:00.123456Z",
        }

        self.assertEqual(value, ApprovalRef.from_dict(value).to_dict())

        invalid_values = (
            {**value, "actor": "assistant"},
            {**value, "thread_id": ""},
            {**value, "recorded_at": "2026-07-29T00:00:00+00:00"},
            {**value, "recorded_at": "2026-02-30T00:00:00Z"},
            {**value, "extra": "field"},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    ApprovalRef.from_dict(invalid)

    def test_review_selection_enforces_action_specific_content(self) -> None:
        accepted = ReviewSelection.from_dict(
            {"candidate_id": CANDIDATE_ID, "action": "accept"}
        )
        edited = ReviewSelection.from_dict(
            {
                "candidate_id": CANDIDATE_ID,
                "action": "edit_accept",
                "content": _content_dict(),
            }
        )

        self.assertIsNone(accepted.content)
        self.assertEqual(_content(), edited.content)
        self.assertEqual(
            {"candidate_id": CANDIDATE_ID, "action": "accept"},
            accepted.to_dict(),
        )

        invalid_values = (
            {
                "candidate_id": CANDIDATE_ID,
                "action": "accept",
                "content": _content_dict(),
            },
            {"candidate_id": CANDIDATE_ID, "action": "edit_accept"},
            {
                "candidate_id": CANDIDATE_ID,
                "action": "reject",
                "content": _content_dict(),
            },
            {"candidate_id": CANDIDATE_ID, "action": "approve"},
            {"candidate_id": "", "action": "skip"},
            {"candidate_id": "candidate_bad", "action": "skip"},
            {"candidate_id": CANDIDATE_ID, "action": "skip", "extra": True},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    ReviewSelection.from_dict(invalid)

    def test_review_item_freezes_content_only_for_accepted_actions(self) -> None:
        accepted = _accepted_item()
        rejected = ReviewItem(
            review_id="rvi_44444444444444444444444444444444",
            candidate_id="cand_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_02",
            action="reject",
            content=None,
        )

        self.assertEqual(accepted, ReviewItem.from_dict(accepted.to_dict()))
        self.assertNotIn("content", rejected.to_dict())

        with self.assertRaises(ValueError):
            ReviewItem(
                review_id=REVIEW_ID,
                candidate_id=CANDIDATE_ID,
                action="accept",
                content=None,
            )
        with self.assertRaises(ValueError):
            ReviewItem(
                review_id=REVIEW_ID,
                candidate_id=CANDIDATE_ID,
                action="skip",
                content=_content(),
            )

    def test_review_batch_validates_identity_membership_and_round_trips(self) -> None:
        batch = ReviewBatch(
            review_batch_id=REVIEW_BATCH_ID,
            capture_id=CAPTURE_ID,
            sequence=1,
            approval=_approval(),
            items=(_accepted_item(),),
        )

        self.assertEqual(batch, ReviewBatch.from_dict(batch.to_dict()))
        self.assertEqual(REVIEW_BATCH_ID, batch.review_batch_id)
        self.assertEqual(REVIEW_ID, batch.items[0].review_id)

        with self.assertRaises(ValueError):
            ReviewBatch(
                review_batch_id=REVIEW_BATCH_ID,
                capture_id=CAPTURE_ID,
                sequence=0,
                approval=_approval(),
                items=(_accepted_item(),),
            )
        with self.assertRaises(ValueError):
            ReviewBatch(
                review_batch_id=REVIEW_BATCH_ID,
                capture_id=CAPTURE_ID,
                sequence=1,
                approval=_approval(),
                items=(_accepted_item(), _accepted_item()),
            )
        with self.assertRaises(ValueError):
            ReviewBatch(
                review_batch_id=REVIEW_BATCH_ID,
                capture_id=CAPTURE_ID,
                sequence=1,
                approval=_approval(),
                items=(
                    ReviewItem(
                        review_id="rvi_44444444444444444444444444444444",
                        candidate_id=CANDIDATE_ID,
                        action="accept",
                        content=_content(),
                    ),
                ),
            )

    def test_review_batch_rejects_unknown_fields_and_more_than_twenty_items(
        self,
    ) -> None:
        valid = ReviewBatch(
            review_batch_id=REVIEW_BATCH_ID,
            capture_id=CAPTURE_ID,
            sequence=1,
            approval=_approval(),
            items=(_accepted_item(),),
        ).to_dict()

        with self.assertRaises(ValueError):
            ReviewBatch.from_dict({**valid, "extra": True})

        items = tuple(
            ReviewItem(
                review_id=f"rvi_{ordinal:032x}",
                candidate_id=f"cand_{ordinal:032x}_01",
                action="skip",
                content=None,
            )
            for ordinal in range(1, 22)
        )
        with self.assertRaises(ValueError):
            ReviewBatch(
                review_batch_id="rvb_44444444444444444444444444444444",
                capture_id=CAPTURE_ID,
                sequence=1,
                approval=_approval(),
                items=items,
            )


class _SequenceClock:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"2026-07-29T00:00:{self.calls:02d}Z"


class ReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        from zdecision.capture.service import CaptureService
        from zdecision.capture.templates import TemplateCatalog

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.state_dir = self.root / "state"
        self.template_root = self.root / "templates"
        shutil.copytree(TEMPLATE_ROOT, self.template_root)
        self.store = FilePrivateStore(self.state_dir)
        self.capture_service = CaptureService(
            self.store,
            TemplateCatalog(self.template_root, ENVELOPE_ROOT),
        )
        self.clock = _SequenceClock()
        self.service = ReviewService(self.store, clock=self.clock)
        self.capture_counter = 0

    def complete_capture(self, product: str = "安恒") -> str:
        self.capture_counter += 1
        suffix = self.capture_counter
        plan = self.capture_service.prepare(
            "thread-source",
            f"turn-source-{suffix}",
            product,
            "business",
        )
        operation_id = plan.record.operation_id
        self.capture_service.attach_fork(operation_id, f"thread-fork-{suffix}")
        self.capture_service.attach_stage_turn(
            operation_id,
            "inventory",
            f"turn-inventory-{suffix}",
        )
        self.capture_service.complete_inventory(operation_id, VALID_INVENTORY)
        self.capture_service.attach_stage_turn(
            operation_id,
            "extraction",
            f"turn-extraction-{suffix}",
        )
        self.capture_service.complete_extraction(
            operation_id,
            {
                "candidates": [
                    {
                        "product": product,
                        "claim": "正式决策按产品隔离保存。",
                        "future_action": "新增决策时写入对应产品目录。",
                        "scope": {
                            "summary": "ZDecision Registry 的正式存储布局",
                            "repositories": [
                                "https://github.com/1320209572/zdecision.git"
                            ],
                            "paths": ["decision-registry/"],
                        },
                        "invalidation_conditions": [
                            "产品隔离策略被新的正式决策替代"
                        ],
                    },
                    {
                        "product": product,
                        "claim": "候选决策在发布前保持私有。",
                        "future_action": "只把审核后的正式决策写入 Git。",
                        "scope": {
                            "summary": "ZDecision 的私有与正式存储边界",
                            "repositories": [],
                            "paths": ["decision-registry/"],
                        },
                        "invalidation_conditions": [
                            "正式存储边界被新的产品决策替代"
                        ],
                    },
                ]
            },
        )
        return operation_id

    def candidate_ids(self, capture_id: str) -> tuple[str, str]:
        record = self.store.get_capture(capture_id)
        assert record is not None
        first, second = record.candidate_ids
        return first, second

    def test_records_one_atomic_mixed_batch_with_frozen_effective_content(
        self,
    ) -> None:
        capture_id = self.complete_capture()
        first, second = self.candidate_ids(capture_id)

        batch = self.service.record(
            capture_id,
            (
                ReviewSelection(first, "accept"),
                ReviewSelection(second, "reject"),
            ),
            "thread-review",
            "turn-review-1",
        )

        self.assertEqual(1, batch.sequence)
        self.assertEqual((first, second), tuple(item.candidate_id for item in batch.items))
        self.assertEqual("安恒", batch.items[0].content.product)
        self.assertIsNone(batch.items[1].content)
        self.assertEqual(batch, self.store.get_review_batch(batch.review_batch_id))
        self.assertEqual((batch.review_batch_id,), self.store.review_batch_ids_for_capture(capture_id))

    def test_edit_accept_freezes_complete_content_but_cannot_move_product(
        self,
    ) -> None:
        capture_id = self.complete_capture()
        first, _ = self.candidate_ids(capture_id)
        edited = CandidateContent.from_dict(
            {
                **_content_dict(),
                "product": "  安恒  ",
                "claim": "正式决策必须按稳定产品 ID 隔离保存。",
            }
        )

        batch = self.service.record(
            capture_id,
            (ReviewSelection(first, "edit_accept", edited),),
            "thread-review",
            "turn-review-1",
        )

        self.assertEqual("安恒", batch.items[0].content.product)
        self.assertEqual(
            "正式决策必须按稳定产品 ID 隔离保存。",
            batch.items[0].content.claim,
        )

        other_capture = self.complete_capture()
        other_first, _ = self.candidate_ids(other_capture)
        wrong_product = CandidateContent.from_dict(
            {**_content_dict(), "product": "其他产品"}
        )
        with self.assertRaises(InvalidReview):
            self.service.record(
                other_capture,
                (ReviewSelection(other_first, "edit_accept", wrong_product),),
                "thread-review",
                "turn-review-2",
            )

    def test_batch_rejects_duplicates_missing_and_foreign_candidates_before_write(
        self,
    ) -> None:
        capture_id = self.complete_capture()
        first, _ = self.candidate_ids(capture_id)
        other_capture = self.complete_capture()
        foreign, _ = self.candidate_ids(other_capture)

        invalid_selections = (
            (ReviewSelection(first, "accept"), ReviewSelection(first, "skip")),
            (
                ReviewSelection(
                    "cand_ffffffffffffffffffffffffffffffff_01",
                    "accept",
                ),
            ),
            (ReviewSelection(foreign, "accept"),),
        )
        for ordinal, selections in enumerate(invalid_selections, start=1):
            with self.subTest(ordinal=ordinal):
                with self.assertRaises(InvalidReview):
                    self.service.record(
                        capture_id,
                        selections,
                        "thread-review",
                        f"turn-invalid-{ordinal}",
                    )

        self.assertEqual((), self.store.review_batch_ids_for_capture(capture_id))

    def test_corrupt_completed_candidate_set_never_produces_a_review(self) -> None:
        capture_id = self.complete_capture()
        first, _ = self.candidate_ids(capture_id)
        candidate_path = self.state_dir / "candidates" / f"{first}.json"
        value = json.loads(candidate_path.read_text("utf-8"))
        value["content"]["claim"] = "tampered but structurally valid"
        atomic_write_json(candidate_path, value)

        with self.assertRaises(PrivateStateCorrupt):
            self.service.record(
                capture_id,
                (ReviewSelection(first, "accept"),),
                "thread-review",
                "turn-review-corrupt",
            )
        self.assertEqual((), self.store.review_batch_ids_for_capture(capture_id))

    def test_only_completed_v2_capture_is_reviewable(self) -> None:
        prepared = self.capture_service.prepare(
            "thread-source",
            "turn-prepared",
            "安恒",
            "business",
        ).record.operation_id
        selection = ReviewSelection(
            "cand_ffffffffffffffffffffffffffffffff_01",
            "skip",
        )

        with self.assertRaises(CaptureNotReviewable):
            self.service.record(
                prepared,
                (selection,),
                "thread-review",
                "turn-review-prepared",
            )

        legacy = LegacyCaptureRecord(
            operation_id="legacy-capture",
            source=SourceCheckpoint("thread-source", "turn-legacy"),
            product="安恒",
            status="completed",
            fork_thread_id="thread-fork",
            candidate_ids=(),
            created_at="2026-07-29T00:00:00Z",
            updated_at="2026-07-29T00:00:00Z",
        )
        atomic_write_json(
            self.state_dir / "captures" / "legacy-capture.json",
            legacy.to_dict(),
        )
        with self.assertRaises(CaptureNotReviewable):
            self.service.record(
                "legacy-capture",
                (selection,),
                "thread-review",
                "turn-review-legacy",
            )

    def test_identical_retry_reuses_batch_timestamp_and_clock_call(self) -> None:
        capture_id = self.complete_capture()
        first, _ = self.candidate_ids(capture_id)
        selections = (ReviewSelection(first, "accept"),)

        first_result = self.service.record(
            capture_id,
            selections,
            "thread-review",
            "turn-review-1",
        )
        replay = self.service.record(
            capture_id,
            selections,
            "thread-review",
            "turn-review-1",
        )

        self.assertEqual(first_result, replay)
        self.assertEqual("2026-07-29T00:00:01Z", replay.approval.recorded_at)
        self.assertEqual(1, self.clock.calls)

    def test_one_approval_turn_cannot_authorize_different_review_bytes_or_capture(
        self,
    ) -> None:
        capture_id = self.complete_capture()
        first, _ = self.candidate_ids(capture_id)
        self.service.record(
            capture_id,
            (ReviewSelection(first, "accept"),),
            "thread-review",
            "turn-review-1",
        )

        with self.assertRaises(ReviewConflict):
            self.service.record(
                capture_id,
                (ReviewSelection(first, "reject"),),
                "thread-review",
                "turn-review-1",
            )

        other_capture = self.complete_capture()
        other_first, _ = self.candidate_ids(other_capture)
        with self.assertRaises(ReviewConflict):
            self.service.record(
                other_capture,
                (ReviewSelection(other_first, "accept"),),
                "thread-review",
                "turn-review-1",
            )
        self.assertEqual(1, self.clock.calls)

    def test_later_batch_gets_next_sequence_and_latest_item_wins(self) -> None:
        capture_id = self.complete_capture()
        first, second = self.candidate_ids(capture_id)
        initial = self.service.record(
            capture_id,
            (ReviewSelection(first, "accept"), ReviewSelection(second, "skip")),
            "thread-review",
            "turn-review-1",
        )
        later = self.service.record(
            capture_id,
            (ReviewSelection(first, "reject"),),
            "thread-review",
            "turn-review-2",
        )

        latest = self.service.latest_items(capture_id)

        self.assertEqual(1, initial.sequence)
        self.assertEqual(2, later.sequence)
        self.assertEqual("reject", latest[first].action)
        self.assertEqual("skip", latest[second].action)
        self.assertEqual(
            tuple(sorted((initial.review_batch_id, later.review_batch_id))),
            self.store.review_batch_ids_for_capture(capture_id),
        )

    def test_missing_review_and_corrupt_private_review_are_sanitized(self) -> None:
        with self.assertRaises(ReviewNotFound):
            self.service.get("rvb_ffffffffffffffffffffffffffffffff")
        with self.assertRaises(InvalidPrivateObjectId):
            self.store.get_review_batch("../outside")

        capture_id = self.complete_capture()
        first, _ = self.candidate_ids(capture_id)
        batch = self.service.record(
            capture_id,
            (ReviewSelection(first, "accept"),),
            "thread-review",
            "turn-review-1",
        )
        path = self.state_dir / "review_batches" / f"{batch.review_batch_id}.json"
        path.write_text("{", "utf-8")

        with self.assertRaises(PrivateStateCorrupt) as raised:
            self.store.get_review_batch(batch.review_batch_id)
        self.assertNotIn("正式决策", str(raised.exception))

    def test_review_store_replays_only_identical_canonical_bytes(self) -> None:
        capture_id = self.complete_capture()
        first, _ = self.candidate_ids(capture_id)
        batch = self.service.record(
            capture_id,
            (ReviewSelection(first, "accept"),),
            "thread-review",
            "turn-review-1",
        )

        self.store.put_review_batch(batch)
        path = self.state_dir / "review_batches" / f"{batch.review_batch_id}.json"
        self.assertEqual(canonical_json_bytes(batch.to_dict()), path.read_bytes())
        path.write_text(json.dumps(batch.to_dict(), ensure_ascii=False), "utf-8")

        with self.assertRaises(PrivateStateConflict):
            self.store.put_review_batch(batch)


if __name__ == "__main__":
    unittest.main()
