"""Opt-in assertion for the sanitized Desktop E0 probe receipts."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from tests.recall_elicitation_probe import ProbeReceiptStore


@unittest.skipUnless(
    os.environ.get("ZDECISION_LIVE_ACCEPTANCE") == "1",
    "live Desktop acceptance is disabled",
)
class RecallElicitationDesktopAcceptanceTest(unittest.TestCase):
    def test_exact_e0_receipts(self):
        store = ProbeReceiptStore.open(
            Path(os.environ["ZDECISION_ELICITATION_E0_DB"])
        )
        try:
            receipts = {item.case_id: item for item in store.receipts()}
        finally:
            store.close()
        self.assertEqual(receipts["accept"].state, "accept")
        self.assertEqual(receipts["decline"].state, "decline")
        self.assertEqual(receipts["cancel"].state, "cancel")
        self.assertEqual(receipts["restart"].state, "transport_lost")
        self.assertTrue(all(item.prompt_count == 1 for item in receipts.values()))
        self.assertEqual(receipts["accept"].completion_count, 1)
        self.assertEqual(receipts["decline"].completion_count, 1)
        self.assertEqual(receipts["cancel"].completion_count, 1)
        self.assertEqual(receipts["restart"].completion_count, 0)


if __name__ == "__main__":
    unittest.main()
