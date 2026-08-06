"""Contract tests for bounded host-side recall session values."""

from __future__ import annotations

import dataclasses
import hashlib
import unittest

from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.session import HostProbeEnvelope, RecallIntent


def _intent_value(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "target_decision_space_ids": ["space-product"],
        "explicit_multi_space": False,
        "feature_goal": "Add a bounded recall gate",
        "domain_objects": ["RecallIntent"],
        "repository_relative_paths": ["src/zdecision/recall/session.py"],
        "constraints": ["host only"],
        "exclusions": ["central writes"],
    }
    value.update(overrides)
    return value


class RecallIntentContractTests(unittest.TestCase):
    def test_from_dict_requires_exact_fields_and_normalizes_sequences(self) -> None:
        """This catches accidental acceptance of omitted or unknown intent data."""

        parsed = RecallIntent.from_dict(
            _intent_value(
                target_decision_space_ids=[" space-product "],
                domain_objects=[" RecallIntent "],
                repository_relative_paths=[" ./src//zdecision/recall/session.py "],
                constraints=[" host only "],
                exclusions=[" central writes "],
            )
        )

        self.assertEqual(parsed.target_decision_space_ids, ("space-product",))
        self.assertEqual(parsed.domain_objects, ("RecallIntent",))
        self.assertEqual(
            parsed.repository_relative_paths,
            ("src/zdecision/recall/session.py",),
        )
        self.assertEqual(parsed.constraints, ("host only",))
        self.assertEqual(parsed.exclusions, ("central writes",))
        self.assertEqual(
            parsed.to_dict(),
            {
                "target_decision_space_ids": ["space-product"],
                "explicit_multi_space": False,
                "feature_goal": "Add a bounded recall gate",
                "domain_objects": ["RecallIntent"],
                "repository_relative_paths": ["src/zdecision/recall/session.py"],
                "constraints": ["host only"],
                "exclusions": ["central writes"],
            },
        )
        with self.assertRaises(ValueError):
            RecallIntent.from_dict({"feature_goal": "missing fields"})
        with self.assertRaises(ValueError):
            RecallIntent.from_dict(_intent_value(unexpected="not allowed"))

    def test_digest_is_stable_for_canonical_intent_data(self) -> None:
        """This catches digesting a noncanonical or incomplete representation."""

        first = RecallIntent.from_dict(_intent_value())
        second = RecallIntent.from_dict(
            {
                "exclusions": ["central writes"],
                "constraints": ["host only"],
                "repository_relative_paths": ["src/zdecision/recall/session.py"],
                "domain_objects": ["RecallIntent"],
                "feature_goal": "Add a bounded recall gate",
                "explicit_multi_space": False,
                "target_decision_space_ids": ["space-product"],
            }
        )

        expected = hashlib.sha256(canonical_json_bytes(first.to_dict())).hexdigest()
        self.assertEqual(first.digest, expected)
        self.assertEqual(second.digest, expected)

    def test_decision_space_count_and_multi_space_flag_must_agree(self) -> None:
        """This catches ambiguous or duplicate target Decision-space selection."""

        with self.assertRaises(ValueError):
            RecallIntent.from_dict(
                _intent_value(target_decision_space_ids=["space-product", "space-product"])
            )
        with self.assertRaises(ValueError):
            RecallIntent.from_dict(
                _intent_value(target_decision_space_ids=["space-product", "space-shared"])
            )
        with self.assertRaises(ValueError):
            RecallIntent.from_dict(
                _intent_value(target_decision_space_ids=[], explicit_multi_space=True)
            )
        parsed = RecallIntent.from_dict(
            _intent_value(
                target_decision_space_ids=["space-product", "space-shared"],
                explicit_multi_space=True,
            )
        )
        self.assertEqual(
            parsed.target_decision_space_ids, ("space-product", "space-shared")
        )
        one_explicit = RecallIntent.from_dict(
            _intent_value(explicit_multi_space=True)
        )
        self.assertEqual(one_explicit.target_decision_space_ids, ("space-product",))
        eight_explicit = RecallIntent.from_dict(
            _intent_value(
                target_decision_space_ids=[f"space-{index}" for index in range(8)],
                explicit_multi_space=True,
            )
        )
        self.assertEqual(len(eight_explicit.target_decision_space_ids), 8)
        with self.assertRaises(ValueError):
            RecallIntent.from_dict(
                _intent_value(
                    target_decision_space_ids=[f"space-{index}" for index in range(9)],
                    explicit_multi_space=True,
                )
            )

    def test_relative_paths_are_normalized_and_unsafe_paths_are_rejected(self) -> None:
        """This catches host traversal or absolute filesystem path acceptance."""

        parsed = RecallIntent.from_dict(
            _intent_value(repository_relative_paths=["./src//zdecision/recall/session.py"])
        )
        self.assertEqual(
            parsed.repository_relative_paths,
            ("src/zdecision/recall/session.py",),
        )
        for unsafe_path in ("/etc/passwd", "src/../secrets.py", "C:\\secrets.py"):
            with self.subTest(unsafe_path=unsafe_path):
                with self.assertRaises(ValueError):
                    RecallIntent.from_dict(
                        _intent_value(repository_relative_paths=[unsafe_path])
                    )

    def test_rejects_blank_or_oversized_intent_content(self) -> None:
        """This catches unbounded host state and blank normalized list members."""

        for field_name in (
            "target_decision_space_ids",
            "domain_objects",
            "repository_relative_paths",
            "constraints",
            "exclusions",
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValueError):
                    RecallIntent.from_dict(_intent_value(**{field_name: ["   "]}))
                with self.assertRaises(ValueError):
                    RecallIntent.from_dict(
                        _intent_value(**{field_name: ["x"] * 33})
                    )
                with self.assertRaises(ValueError):
                    RecallIntent.from_dict(
                        _intent_value(**{field_name: ["x" * 513]})
                    )
        with self.assertRaises(ValueError):
            RecallIntent.from_dict(_intent_value(feature_goal="x" * 2_001))
        with self.assertRaises(ValueError):
            RecallIntent.from_dict(_intent_value(constraints=["x" * 512] * 32))


class HostProbeEnvelopeContractTests(unittest.TestCase):
    def test_probe_marker_is_fixture_only_and_frozen(self) -> None:
        """This catches a host probe being represented as a formal decision."""

        probe = HostProbeEnvelope(
            probe_id="probe-1",
            marker="host_gate_fixture_not_formal",
            instruction="Use this fixture only for the host gate.",
        )

        with self.assertRaises(ValueError):
            HostProbeEnvelope(
                probe_id="probe-1",
                marker="formal_decision",  # type: ignore[arg-type]
                instruction="This must not be a formal decision.",
            )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            probe.marker = "formal_decision"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
