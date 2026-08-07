from __future__ import annotations

import copy
import hashlib
import unittest

from zdecision.capture.provenance import (
    CaptureEvidenceManifest,
    PromptAnchor,
    prompt_anchor_receipt_id,
)


EVENT_ONE = "evt_" + "1" * 32
EVENT_TWO = "evt_" + "2" * 32
TURN_ONE = "turn-1"
TURN_TWO = "turn-2"


def anchor(event_id: str, turn_id: str, ordinal: int) -> PromptAnchor:
    return PromptAnchor(
        receipt_id=prompt_anchor_receipt_id(event_id),
        hook_event_id=event_id,
        turn_id=turn_id,
        anchor_ordinal=ordinal,
        active_reference_set_digest=None,
    )


class CaptureEvidenceManifestTests(unittest.TestCase):
    def test_manifest_round_trips_the_host_issued_anchor_receipts(self) -> None:
        """This catches a manifest omitting a receipt, ordinal, or null digest."""

        first = anchor(EVENT_ONE, TURN_ONE, 1)
        second = anchor(EVENT_TWO, TURN_TWO, 2)
        manifest = CaptureEvidenceManifest.create(
            source_session_id="session-1",
            previous_handled_event_id=None,
            upper_stop_event_id="evt_" + "3" * 32,
            anchors=(first, second),
        )

        decoded = CaptureEvidenceManifest.from_dict(manifest.to_dict())

        self.assertEqual(manifest, decoded)
        self.assertEqual(
            (first.receipt_id, second.receipt_id),
            tuple(item["receipt_id"] for item in manifest.to_dict()["anchors"]),
        )
        self.assertNotEqual(EVENT_ONE, first.receipt_id)

    def test_manifest_rejects_tampered_or_noncanonical_anchor_payloads(self) -> None:
        """This catches accepting forged, reordered, or noncanonical evidence."""

        manifest = CaptureEvidenceManifest.create(
            source_session_id="session-1",
            previous_handled_event_id=None,
            upper_stop_event_id="evt_" + "3" * 32,
            anchors=(anchor(EVENT_ONE, TURN_ONE, 1), anchor(EVENT_TWO, TURN_TWO, 2)),
        )
        invalid_payloads: list[dict[str, object]] = []

        unknown = copy.deepcopy(manifest.to_dict())
        unknown["unexpected"] = True
        invalid_payloads.append(unknown)

        duplicate = copy.deepcopy(manifest.to_dict())
        duplicate["anchors"] = [duplicate["anchors"][0], duplicate["anchors"][0]]
        invalid_payloads.append(duplicate)

        reordered = copy.deepcopy(manifest.to_dict())
        reordered["anchors"] = list(reversed(reordered["anchors"]))
        invalid_payloads.append(reordered)

        invalid_digest = copy.deepcopy(manifest.to_dict())
        invalid_digest["manifest_digest"] = "0" * 64
        invalid_payloads.append(invalid_digest)

        empty = copy.deepcopy(manifest.to_dict())
        empty["anchors"] = []
        invalid_payloads.append(empty)

        bad_ordinal = copy.deepcopy(manifest.to_dict())
        bad_ordinal["anchors"][1]["anchor_ordinal"] = 3
        invalid_payloads.append(bad_ordinal)

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    CaptureEvidenceManifest.from_dict(payload)

    def test_receipt_identifier_is_deterministic_and_opaque(self) -> None:
        """This catches receipts exposing or randomly changing the Hook event ID."""

        receipt = prompt_anchor_receipt_id(EVENT_ONE)

        self.assertEqual(receipt, prompt_anchor_receipt_id(EVENT_ONE))
        self.assertNotIn(EVENT_ONE, receipt)
        self.assertNotEqual(
            receipt, prompt_anchor_receipt_id(EVENT_TWO)
        )


if __name__ == "__main__":
    unittest.main()
