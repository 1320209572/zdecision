from __future__ import annotations

import copy
import unittest

from zdecision.capture.provenance import (
    CaptureEvidenceManifest,
    PromptAnchor,
    prompt_anchor_receipt_id,
)


VALID_INVENTORY = {
    "signals": [
        {
            "topic": "升级目标",
            "rule": "用户选择实际升级目标规格",
            "future_effect": "后续升级流程不得自动替用户选择",
            "scope": "安恒实例升级",
            "status": "current_confirmed",
            "confirmation_basis": "explicit_user_direction",
            "confidence": "high",
        }
    ],
    "coverage": {
        "reviewed_retained_context": "earliest_to_latest",
        "known_gaps": [],
    },
}


def evidence_manifest(*, active_reference_set_digest: str | None = None) -> CaptureEvidenceManifest:
    event_id = "evt_" + "1" * 32
    anchor = PromptAnchor(
        receipt_id=prompt_anchor_receipt_id(event_id),
        hook_event_id=event_id,
        turn_id="turn-1",
        anchor_ordinal=1,
        active_reference_set_digest=active_reference_set_digest,
    )
    return CaptureEvidenceManifest.create(
        source_session_id="session-1",
        previous_handled_event_id=None,
        upper_stop_event_id="evt_" + "2" * 32,
        anchors=(anchor,),
    )


def v5_inventory(manifest: CaptureEvidenceManifest) -> dict[str, object]:
    value = copy.deepcopy(VALID_INVENTORY)
    value["signals"][0].update(
        {
            "signal_ordinal": 1,
            "evidence_receipt_ids": [manifest.anchors[0].receipt_id],
        }
    )
    return value


def multi_receipt_manifest() -> CaptureEvidenceManifest:
    anchors = tuple(
        PromptAnchor(
            receipt_id=prompt_anchor_receipt_id("evt_" + digit * 32),
            hook_event_id="evt_" + digit * 32,
            turn_id=f"turn-{ordinal}",
            anchor_ordinal=ordinal,
            active_reference_set_digest=None,
        )
        for ordinal, digit in enumerate(("3", "4"), start=1)
    )
    return CaptureEvidenceManifest.create(
        source_session_id="session-1",
        previous_handled_event_id=None,
        upper_stop_event_id="evt_" + "5" * 32,
        anchors=anchors,
    )


class InventoryValidationTests(unittest.TestCase):
    def inventory_api(self):
        try:
            from zdecision.capture.inventory import (
                InventoryValidationError,
                validate_inventory,
            )
        except ModuleNotFoundError as exc:
            self.fail(f"Inventory API is missing: {exc}")
        return InventoryValidationError, validate_inventory

    def assert_invalid(self, value: object, code: str = "invalid_inventory") -> None:
        error_type, validate_inventory = self.inventory_api()
        with self.assertRaises(error_type) as raised:
            validate_inventory(value)
        self.assertEqual(code, raised.exception.code)

    def test_valid_inventory_round_trips(self) -> None:
        _, validate_inventory = self.inventory_api()

        result = validate_inventory(VALID_INVENTORY)

        self.assertEqual("升级目标", result.signals[0].topic)
        self.assertEqual((), result.coverage.known_gaps)
        self.assertEqual(VALID_INVENTORY, result.to_dict())

    def test_zero_signals_is_valid(self) -> None:
        _, validate_inventory = self.inventory_api()
        value = {
            "signals": [],
            "coverage": {
                "reviewed_retained_context": "earliest_to_latest",
                "known_gaps": [],
            },
        }

        self.assertEqual((), validate_inventory(value).signals)

    def test_all_enum_values_are_accepted_individually(self) -> None:
        _, validate_inventory = self.inventory_api()
        enum_cases = {
            "status": ("current_confirmed", "unresolved", "superseded"),
            "confirmation_basis": (
                "explicit_user_confirmation",
                "explicit_user_direction",
                "adopted_decision_contract",
                "uncertain",
            ),
            "confidence": ("high", "medium", "low"),
        }

        for field, values in enum_cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    inventory = copy.deepcopy(VALID_INVENTORY)
                    inventory["signals"][0][field] = value
                    self.assertEqual(
                        value,
                        getattr(validate_inventory(inventory).signals[0], field),
                    )

    def test_signal_limit_is_checked_before_item_shape(self) -> None:
        error_type, validate_inventory = self.inventory_api()
        value = {
            "signals": [{}] * 101,
            "coverage": {
                "reviewed_retained_context": "earliest_to_latest",
                "known_gaps": [],
            },
        }

        with self.assertRaisesRegex(
            error_type, "inventory_signal_limit_exceeded"
        ) as raised:
            validate_inventory(value)
        self.assertEqual("inventory_signal_limit_exceeded", raised.exception.code)

    def test_canonical_size_is_checked_before_nested_shape(self) -> None:
        error_type, validate_inventory = self.inventory_api()
        value = {
            "signals": [{"private_unknown": "界" * 100_000}],
            "coverage": {
                "reviewed_retained_context": "earliest_to_latest",
                "known_gaps": [],
            },
        }

        with self.assertRaises(error_type) as raised:
            validate_inventory(value)
        self.assertEqual("inventory_output_too_large", raised.exception.code)

    def test_exact_fields_are_required_at_every_level(self) -> None:
        cases: list[object] = []

        top_unknown = copy.deepcopy(VALID_INVENTORY)
        top_unknown["summary"] = "not allowed"
        cases.append(top_unknown)

        top_missing = copy.deepcopy(VALID_INVENTORY)
        del top_missing["coverage"]
        cases.append(top_missing)

        signal_unknown = copy.deepcopy(VALID_INVENTORY)
        signal_unknown["signals"][0]["evidence"] = "not allowed"
        cases.append(signal_unknown)

        signal_missing = copy.deepcopy(VALID_INVENTORY)
        del signal_missing["signals"][0]["scope"]
        cases.append(signal_missing)

        coverage_unknown = copy.deepcopy(VALID_INVENTORY)
        coverage_unknown["coverage"]["complete"] = True
        cases.append(coverage_unknown)

        coverage_missing = copy.deepcopy(VALID_INVENTORY)
        del coverage_missing["coverage"]["known_gaps"]
        cases.append(coverage_missing)

        for value in cases:
            with self.subTest(value=value):
                self.assert_invalid(value)

    def test_signal_text_fields_must_be_non_empty_strings(self) -> None:
        for field in ("topic", "rule", "future_effect", "scope"):
            for invalid in ("", "   ", 7, None):
                with self.subTest(field=field, invalid=invalid):
                    value = copy.deepcopy(VALID_INVENTORY)
                    value["signals"][0][field] = invalid
                    self.assert_invalid(value)

    def test_pipe_combined_or_unknown_enum_values_are_rejected(self) -> None:
        cases = (
            ("status", "current_confirmed|unresolved"),
            (
                "confirmation_basis",
                "explicit_user_confirmation|explicit_user_direction",
            ),
            ("confidence", "high|medium"),
            ("status", "confirmed"),
            ("confirmation_basis", "assistant_inference"),
            ("confidence", "certain"),
        )

        for field, invalid in cases:
            with self.subTest(field=field, invalid=invalid):
                value = copy.deepcopy(VALID_INVENTORY)
                value["signals"][0][field] = invalid
                self.assert_invalid(value)

    def test_unhashable_enum_values_use_the_sanitized_validation_boundary(self) -> None:
        for field in ("status", "confirmation_basis", "confidence"):
            with self.subTest(field=field):
                value = copy.deepcopy(VALID_INVENTORY)
                value["signals"][0][field] = []
                self.assert_invalid(value)

    def test_coverage_marker_and_gap_types_are_strict(self) -> None:
        invalid_marker = copy.deepcopy(VALID_INVENTORY)
        invalid_marker["coverage"]["reviewed_retained_context"] = "latest_to_earliest"
        self.assert_invalid(invalid_marker)

        invalid_gap = copy.deepcopy(VALID_INVENTORY)
        invalid_gap["coverage"]["known_gaps"] = ["missing authorization", 7]
        self.assert_invalid(invalid_gap)

    def test_invalid_second_signal_never_returns_a_partial_inventory(self) -> None:
        value = copy.deepcopy(VALID_INVENTORY)
        value["signals"].append({**value["signals"][0], "rule": ""})

        self.assert_invalid(value)

    def test_validation_messages_do_not_echo_model_authored_secrets(self) -> None:
        error_type, validate_inventory = self.inventory_api()
        secret = "MODEL_SECRET_8c2340e3"

        for value in (
            {
                **copy.deepcopy(VALID_INVENTORY),
                secret: "unknown private value",
            },
            {
                **copy.deepcopy(VALID_INVENTORY),
                "signals": [
                    {
                        **copy.deepcopy(VALID_INVENTORY)["signals"][0],
                        "status": secret,
                    }
                ],
            },
        ):
            with self.subTest(value=value):
                with self.assertRaises(error_type) as raised:
                    validate_inventory(value)
                self.assertEqual("invalid_inventory", raised.exception.code)
                self.assertNotIn(secret, raised.exception.message)
                self.assertNotIn(secret, str(raised.exception))

    def test_v5_uses_only_canonical_manifest_receipts_and_host_dispositions(self) -> None:
        """This catches model-selected receipts bypassing the frozen allowlist."""
        from zdecision.capture.inventory import (
            InventoryValidationError,
            validate_inventory_v5,
        )

        manifest = evidence_manifest(active_reference_set_digest="a" * 64)
        inventory, provenance = validate_inventory_v5(
            v5_inventory(manifest), manifest
        )

        self.assertEqual(VALID_INVENTORY, inventory.to_dict())
        self.assertEqual("candidate_eligible", provenance[0].disposition)
        self.assertEqual(("a" * 64,), provenance[0].active_reference_set_digests)

        for receipts in (
            ["rcpt_" + "f" * 64],
            [manifest.anchors[0].receipt_id, manifest.anchors[0].receipt_id],
            [],
        ):
            with self.subTest(receipts=receipts):
                invalid = v5_inventory(manifest)
                invalid["signals"][0]["evidence_receipt_ids"] = receipts
                with self.assertRaises(InventoryValidationError):
                    validate_inventory_v5(invalid, manifest)

    def test_v5_reference_influenced_short_confirmation_never_becomes_eligible(self) -> None:
        """This catches treating recall-active assent as an independent direction."""
        from zdecision.capture.inventory import validate_inventory_v5

        manifest = evidence_manifest(active_reference_set_digest="b" * 64)
        short_confirmation = v5_inventory(manifest)
        short_confirmation["signals"][0]["confirmation_basis"] = (
            "explicit_user_confirmation"
        )
        _, confirmation_provenance = validate_inventory_v5(
            short_confirmation, manifest
        )
        self.assertEqual("needs_evidence", confirmation_provenance[0].disposition)

        uncertain = v5_inventory(manifest)
        uncertain["signals"][0]["confirmation_basis"] = "uncertain"
        _, uncertain_provenance = validate_inventory_v5(uncertain, manifest)
        self.assertNotEqual(
            "candidate_eligible", uncertain_provenance[0].disposition
        )

    def test_v5_rejects_reordered_cross_manifest_and_noncanonical_ordinals(self) -> None:
        """This catches receipt borrowing or ordinal rewriting before Extraction."""
        from zdecision.capture.inventory import (
            InventoryValidationError,
            validate_inventory_v5,
        )

        manifest = multi_receipt_manifest()
        for mutation in (
            lambda value: value["signals"][0].update(
                {
                    "evidence_receipt_ids": [
                        manifest.anchors[1].receipt_id,
                        manifest.anchors[0].receipt_id,
                    ]
                }
            ),
            lambda value: value["signals"][0].update({"signal_ordinal": 2}),
            lambda value: value["signals"][0].update(
                {"evidence_receipt_ids": [evidence_manifest().anchors[0].receipt_id]}
            ),
        ):
            value = v5_inventory(manifest)
            mutation(value)
            with self.assertRaises(InventoryValidationError):
                validate_inventory_v5(value, manifest)

    def test_v5_only_direct_current_direction_is_eligible(self) -> None:
        """This catches non-current, adoption, reference-only, and code-only inputs."""
        from zdecision.capture.inventory import validate_inventory_v5

        manifest = evidence_manifest(active_reference_set_digest="c" * 64)
        cases = (
            {"status": "unresolved"},
            {"confirmation_basis": "uncertain"},
            {"confirmation_basis": "explicit_user_confirmation"},
            {"confirmation_basis": "adopted_decision_contract"},
            {"status": "superseded", "evidence_receipt_ids": []},
        )
        for change in cases:
            with self.subTest(change=change):
                value = v5_inventory(manifest)
                value["signals"][0].update(change)
                _, provenance = validate_inventory_v5(value, manifest)
                self.assertNotEqual("candidate_eligible", provenance[0].disposition)


if __name__ == "__main__":
    unittest.main()
