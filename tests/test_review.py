from __future__ import annotations

import unittest

from zdecision.capture.models import CandidateContent
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


CAPTURE_ID = "cap_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CANDIDATE_ID = "cand_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_01"
REVIEW_BATCH_ID = "rvb_39a385f527e697a9e40ce1105a7dd8b0"
REVIEW_ID = "rvi_a36b19360a8986f487bca806900ac678"
PRODUCT_ID = "prod_4d7b16e1616dd4cd1aeb2411836fd687"


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


if __name__ == "__main__":
    unittest.main()
