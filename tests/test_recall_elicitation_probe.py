"""Tests for the durable, one-shot Recall elicitation probe state."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zdecision.jsonio import canonical_json_bytes

from tests.recall_elicitation_probe import ProbeConflict, ProbeReceiptStore


NOW = datetime(2026, 8, 9, 3, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)


class ProbeReceiptStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "probe.sqlite3"
        self.store = ProbeReceiptStore.open(self.database_path)
        self.addCleanup(self.store.close)

    def test_arm_claim_accept_is_one_shot(self) -> None:
        """This catches a claimed prompt that accepts more than once."""

        self.store.arm("accept", now=NOW)
        pending = self.store.claim_armed(request_digest="a" * 64, now=NOW)
        self.assertEqual((pending.state, pending.prompt_count), ("pending", 1))
        accepted = self.store.complete("accept", state="accept", now=NOW)
        self.assertEqual(
            (accepted.state, accepted.prompt_count, accepted.completion_count),
            ("accept", 1, 1),
        )
        self.assertEqual(
            self.store.current(),
            accepted,
        )

    def test_decline_and_cancel_are_terminal_non_accepting_results(self) -> None:
        """This catches declining or cancelling without completing the receipt."""

        for case_id, state in (("decline", "decline"), ("cancel", "cancel")):
            self.store.arm(case_id, now=NOW)
            self.store.claim_armed(request_digest="b" * 64, now=NOW)
            receipt = self.store.complete(case_id, state=state, now=NOW)
            self.assertEqual(receipt.state, state)
            self.assertEqual(receipt.completion_count, 1)

    def test_restart_recovers_pending_as_transport_lost_without_reprompt(self) -> None:
        """This catches a restart re-arming an already-shown prompt."""

        self.store.arm("restart", now=NOW)
        self.store.claim_armed(request_digest="c" * 64, now=NOW)
        self.store.close()
        reopened = ProbeReceiptStore.open(self.database_path)
        self.addCleanup(reopened.close)
        recovered = reopened.recover_pending(now=LATER)
        self.assertEqual([item.state for item in recovered], ["transport_lost"])
        self.assertEqual(reopened.receipt("restart").prompt_count, 1)
        with self.assertRaises(ProbeConflict):
            reopened.arm("restart", now=LATER)

    def test_report_and_database_exclude_private_sentinels(self) -> None:
        """This catches a report or receipt database retaining prompt-source data."""

        sentinel = "PRIVATE_PROMPT_SOURCE_DIFF_DECISION_SENTINEL"
        self.store.arm("accept", now=NOW)
        report = canonical_json_bytes(self.store.report())
        self.assertNotIn(sentinel.encode(), report)
        self.assertNotIn(sentinel.encode(), self.database_path.read_bytes())

    def test_rejects_invalid_case_state_digest_and_naive_time(self) -> None:
        """This catches malformed input entering durable probe state."""

        with self.assertRaises(ValueError):
            self.store.arm("wrong", now=NOW)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.store.arm("accept", now=NOW.replace(tzinfo=None))

        self.store.arm("accept", now=NOW)
        with self.assertRaises(ValueError):
            self.store.claim_armed(request_digest="A" * 64, now=NOW)
        with self.assertRaises(ValueError):
            self.store.claim_armed(request_digest="a" * 63, now=NOW)
        with self.assertRaises(ValueError):
            self.store.claim_armed(request_digest="a" * 64, now=NOW.replace(tzinfo=None))
        self.store.claim_armed(request_digest="a" * 64, now=NOW)
        with self.assertRaises(ValueError):
            self.store.complete("accept", state="pending", now=NOW)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.store.complete("accept", state="wrong", now=NOW)  # type: ignore[arg-type]

    def test_normalizes_aware_timestamps_to_utc_z(self) -> None:
        """This catches durable receipts retaining a non-UTC clock representation."""

        timestamp = datetime.fromisoformat("2026-08-09T11:00:00+08:00")

        receipt = self.store.arm("accept", now=timestamp)

        self.assertEqual(receipt.updated_at, "2026-08-09T03:00:00Z")

    def test_refuses_two_simultaneously_armed_cases(self) -> None:
        """This catches a second case being armed while another one is active."""

        armed = self.store.arm("accept", now=NOW)

        with self.assertRaises(ProbeConflict):
            self.store.arm("decline", now=LATER)

        self.assertEqual(self.store.current(), armed)
        self.assertIsNone(self.store.receipt("decline"))

    def test_refuses_different_request_replay_for_pending_case(self) -> None:
        """This catches a pending prompt being claimed by a different request."""

        self.store.arm("accept", now=NOW)
        first = self.store.claim_armed(request_digest="a" * 64, now=NOW)

        with self.assertRaises(ProbeConflict):
            self.store.claim_armed(request_digest="b" * 64, now=LATER)

        self.assertEqual(self.store.current(), first)

    def test_arm_moves_current_marker_without_changing_terminal_receipt(self) -> None:
        """This catches arming a new case mutating an earlier terminal receipt."""

        self.store.arm("accept", now=NOW)
        self.store.claim_armed(request_digest="a" * 64, now=NOW)
        accepted = self.store.complete("accept", state="accept", now=NOW)

        armed = self.store.arm("decline", now=LATER)

        self.assertEqual(self.store.receipt("accept"), accepted)
        self.assertEqual(self.store.current(), armed)

    def test_unavailable_finishes_armed_case_without_prompt(self) -> None:
        """This catches unavailable capability recording a displayed prompt."""

        self.store.arm("capability_unavailable", now=NOW)
        unavailable = self.store.mark_armed_unavailable(now=LATER)

        self.assertEqual(
            (unavailable.state, unavailable.prompt_count, unavailable.completion_count),
            ("unavailable", 0, 0),
        )
        self.assertEqual(self.store.current(), unavailable)

    def test_completion_requires_pending_and_cannot_repeat(self) -> None:
        """This catches completing before a claim or completing the same case twice."""

        self.store.arm("accept", now=NOW)
        with self.assertRaises(ProbeConflict):
            self.store.complete("accept", state="accept", now=NOW)

        self.store.claim_armed(request_digest="a" * 64, now=NOW)
        self.store.complete("accept", state="accept", now=LATER)
        with self.assertRaises(ProbeConflict):
            self.store.complete("accept", state="accept", now=LATER)

    def test_non_client_completion_states_do_not_count_as_actions(self) -> None:
        """This catches failure outcomes being counted as user completions."""

        for case_id, state in (
            ("accept", "unavailable"),
            ("decline", "failed"),
            ("cancel", "transport_lost"),
        ):
            self.store.arm(case_id, now=NOW)
            self.store.claim_armed(request_digest="d" * 64, now=NOW)
            receipt = self.store.complete(case_id, state=state, now=LATER)
            self.assertEqual(receipt.completion_count, 0)

    def test_recovered_transport_lost_case_cannot_later_accept(self) -> None:
        """This catches a recovered transport failure being treated as pending."""

        self.store.arm("restart", now=NOW)
        self.store.claim_armed(request_digest="c" * 64, now=NOW)
        self.store.recover_pending(now=LATER)

        with self.assertRaises(ProbeConflict):
            self.store.complete("restart", state="accept", now=LATER)


if __name__ == "__main__":
    unittest.main()
