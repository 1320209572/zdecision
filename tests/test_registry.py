from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from zdecision.capture.models import CandidateContent, SourceCheckpoint
from zdecision.capture.reviews import ApprovalRef
from zdecision.ids import decision_id, product_id
from zdecision.jsonio import atomic_write_bytes, atomic_write_json, canonical_json_bytes
from zdecision.registry.catalog import (
    DecisionUpdateNotSupported,
    RegistryCatalog,
    RegistryConflict,
    RegistryInvalid,
    RegistryStale,
)
from zdecision.registry.models import (
    DecisionHead,
    DecisionRevision,
    DecisionSeed,
    ProductMetadata,
    ProductRegistry,
    RootProductEntry,
    RootRegistry,
)


PRODUCT_NAME = "安恒"
PRODUCT_ID = "prod_4d7b16e1616dd4cd1aeb2411836fd687"
PREVIEW_ID = "pub_33333333333333333333333333333333"
ROOT_PATH = "decision-registry/registry.json"


def _content(claim: str = "正式决策按产品隔离保存。") -> CandidateContent:
    return CandidateContent(
        product=PRODUCT_NAME,
        claim=claim,
        future_action="新增决策时写入对应产品目录。",
        scope_summary="ZDecision Registry 的正式存储布局",
        repositories=("https://github.com/1320209572/zdecision.git",),
        paths=("decision-registry/",),
        invalidation_conditions=("产品隔离策略被新的正式决策替代",),
    )


def _approval() -> ApprovalRef:
    return ApprovalRef(
        actor="user",
        thread_id="thread-review",
        turn_id="turn-review",
        recorded_at="2026-07-29T00:00:00Z",
    )


def _seed(ordinal: int, claim: str | None = None) -> DecisionSeed:
    candidate_id = f"cand_{ordinal:032x}_01"
    return DecisionSeed(
        candidate_id=candidate_id,
        decision_id=decision_id(candidate_id, PRODUCT_ID),
        product_id=PRODUCT_ID,
        product_name=PRODUCT_NAME,
        content=_content(claim or f"正式产品决策 {ordinal}"),
        source=SourceCheckpoint("thread-source", "turn-source"),
        review_approval=_approval(),
    )


def _decision_dict(seed: DecisionSeed) -> dict[str, object]:
    return {
        "format": "zdecision-decision/v1",
        "schema_version": 1,
        "decision_id": seed.decision_id,
        "product_id": PRODUCT_ID,
        "product_name": PRODUCT_NAME,
        "revision": 1,
        "lifecycle": "active",
        "claim": seed.content.claim,
        "future_action": seed.content.future_action,
        "scope": {
            "summary": seed.content.scope_summary,
            "repositories": list(seed.content.repositories),
            "paths": list(seed.content.paths),
        },
        "invalidation_conditions": list(seed.content.invalidation_conditions),
        "supersedes": [],
        "variant_of": [],
        "source": seed.source.to_dict(),
        "review_approval": seed.review_approval.to_dict(),
        "publication_preview_id": PREVIEW_ID,
    }


class RegistryModelTests(unittest.TestCase):
    def test_all_formal_models_round_trip_the_fixed_exact_shapes(self) -> None:
        seed = _seed(1)
        decision_value = _decision_dict(seed)
        product_path = f"products/{PRODUCT_ID}/product.json"
        registry_path = f"products/{PRODUCT_ID}/registry.json"
        head_path = f"decisions/{seed.decision_id}/r0001.json"
        root_value = {
            "format": "zdecision-registry/v1",
            "schema_version": 1,
            "products": {
                PRODUCT_ID: {
                    "name": PRODUCT_NAME,
                    "product_path": product_path,
                    "registry_path": registry_path,
                }
            },
        }
        product_value = {
            "format": "zdecision-product/v1",
            "schema_version": 1,
            "product_id": PRODUCT_ID,
            "name": PRODUCT_NAME,
        }
        product_registry_value = {
            "format": "zdecision-product-registry/v1",
            "schema_version": 1,
            "product_id": PRODUCT_ID,
            "decisions": {
                seed.decision_id: {
                    "head_revision": 1,
                    "lifecycle": "active",
                    "head_path": head_path,
                }
            },
        }

        self.assertEqual(root_value, RootRegistry.from_dict(root_value).to_dict())
        self.assertEqual(
            product_value,
            ProductMetadata.from_dict(product_value).to_dict(),
        )
        self.assertEqual(
            product_registry_value,
            ProductRegistry.from_dict(product_registry_value).to_dict(),
        )
        self.assertEqual(
            decision_value,
            DecisionRevision.from_dict(decision_value).to_dict(),
        )
        self.assertEqual(
            canonical_json_bytes(decision_value),
            canonical_json_bytes(DecisionRevision.from_dict(decision_value).to_dict()),
        )

    def test_formal_models_reject_unknown_fields_and_wrong_constants(self) -> None:
        seed = _seed(1)
        decision = _decision_dict(seed)
        invalid_decisions = (
            {**decision, "candidate_id": seed.candidate_id},
            {**decision, "schema_version": 2},
            {**decision, "revision": 2},
            {**decision, "lifecycle": "retired"},
            {**decision, "supersedes": ["dec_" + "f" * 32]},
            {**decision, "publication_confirmation": {"turn_id": "secret"}},
        )
        for invalid in invalid_decisions:
            with self.subTest(keys=tuple(invalid)):
                with self.assertRaises(ValueError):
                    DecisionRevision.from_dict(invalid)

        root = {
            "format": "zdecision-registry/v1",
            "schema_version": 1,
            "products": {},
        }
        for invalid in (
            {**root, "extra": True},
            {"format": "zdecision-registry/v1", "schema_version": 1},
            {**root, "format": "legacy-registry/v0"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    RootRegistry.from_dict(invalid)

    def test_id_and_path_ownership_are_validated_at_each_model_boundary(self) -> None:
        seed = _seed(1)
        with self.assertRaises(ValueError):
            ProductMetadata(
                product_id="prod_" + "f" * 32,
                name=PRODUCT_NAME,
            )
        with self.assertRaises(ValueError):
            RootRegistry(
                products={
                    PRODUCT_ID: RootProductEntry(
                        name=PRODUCT_NAME,
                        product_path="../outside.json",
                        registry_path=f"products/{PRODUCT_ID}/registry.json",
                    )
                }
            )
        with self.assertRaises(ValueError):
            ProductRegistry(
                product_id=PRODUCT_ID,
                decisions={
                    seed.decision_id: DecisionHead(
                        head_revision=1,
                        lifecycle="active",
                        head_path="../other/r0001.json",
                    )
                },
            )
        wrong_product = {**_decision_dict(seed), "product_id": "prod_" + "f" * 32}
        with self.assertRaises(ValueError):
            DecisionRevision.from_dict(wrong_product)

    def test_atomic_write_bytes_replaces_exact_content_without_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "value.json"

            atomic_write_bytes(path, b"first\n")
            atomic_write_bytes(path, b"second\n")

            self.assertEqual(b"second\n", path.read_bytes())
            self.assertEqual(["value.json"], [item.name for item in path.parent.iterdir()])


class RegistryCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repository_root = Path(self.temp_dir.name)
        self.registry_root = self.repository_root / "decision-registry"
        self.registry_root.mkdir()
        atomic_write_json(
            self.registry_root / "registry.json",
            {
                "format": "zdecision-registry/v1",
                "schema_version": 1,
                "products": {},
            },
        )
        (self.registry_root / "README.md").write_text("formal only\n", "utf-8")
        self.catalog = RegistryCatalog(self.repository_root)

    def test_empty_registry_plans_multiple_decisions_without_writing(self) -> None:
        seeds = (_seed(2), _seed(1))
        before = tuple(sorted(str(path.relative_to(self.repository_root)) for path in self.repository_root.rglob("*")))

        plan = self.catalog.inspect(seeds)
        draft = self.catalog.render(plan, PREVIEW_ID)

        after = tuple(sorted(str(path.relative_to(self.repository_root)) for path in self.repository_root.rglob("*")))
        self.assertEqual(before, after)
        self.assertEqual(tuple(seed.decision_id for seed in seeds), plan.decision_ids)
        self.assertEqual(PRODUCT_ID, plan.product_id)
        self.assertEqual(5, len(draft.changed_files))
        self.assertEqual(5, len(draft.display_documents))
        self.assertEqual(tuple(sorted(draft.changed_files)), plan.changed_paths)
        for path in draft.changed_files:
            self.assertTrue(path.startswith(f"decision-registry/products/{PRODUCT_ID}/") or path == ROOT_PATH)
            self.assertNotIn(PRODUCT_NAME, path)
            if path != ROOT_PATH:
                self.assertFalse((self.repository_root / path).exists())

        root = json.loads(draft.display_documents[ROOT_PATH])
        product_registry_path = f"decision-registry/products/{PRODUCT_ID}/registry.json"
        product_registry = json.loads(draft.display_documents[product_registry_path])
        self.assertEqual([PRODUCT_ID], list(root["products"]))
        self.assertEqual(
            sorted(seed.decision_id for seed in seeds),
            list(product_registry["decisions"]),
        )
        for seed in seeds:
            path = f"decision-registry/products/{PRODUCT_ID}/decisions/{seed.decision_id}/r0001.json"
            value = json.loads(draft.display_documents[path])
            self.assertEqual(PREVIEW_ID, value["publication_preview_id"])
            self.assertNotIn("candidate_id", value)
            self.assertNotIn("review_id", value)
            self.assertNotIn("publication_confirmation", value)

    def test_existing_product_adds_only_new_decision_and_changed_index(self) -> None:
        first = _seed(1)
        initial_plan = self.catalog.inspect((first,))
        initial = self.catalog.render(initial_plan, PREVIEW_ID)
        self.catalog.write_exact(initial.changed_files)
        root_before = (self.registry_root / "registry.json").read_bytes()
        product_before = (
            self.registry_root / "products" / PRODUCT_ID / "product.json"
        ).read_bytes()

        second = _seed(2)
        plan = self.catalog.inspect((second,))
        draft = self.catalog.render(
            plan,
            "pub_44444444444444444444444444444444",
        )

        self.assertEqual(
            (
                f"decision-registry/products/{PRODUCT_ID}/decisions/{second.decision_id}/r0001.json",
                f"decision-registry/products/{PRODUCT_ID}/registry.json",
            ),
            tuple(sorted(draft.changed_files)),
        )
        self.assertEqual(root_before, draft.display_documents[ROOT_PATH])
        self.assertEqual(
            product_before,
            draft.display_documents[
                f"decision-registry/products/{PRODUCT_ID}/product.json"
            ],
        )
        with self.assertRaises(DecisionUpdateNotSupported):
            self.catalog.inspect((first,))

    def test_assert_base_detects_any_relevant_registry_change(self) -> None:
        plan = self.catalog.inspect((_seed(1),))
        atomic_write_json(
            self.registry_root / "registry.json",
            {
                "format": "zdecision-registry/v1",
                "schema_version": 1,
                "products": {PRODUCT_ID: {}},
            },
        )

        with self.assertRaises(RegistryStale):
            self.catalog.assert_base(plan)

    def test_invalid_missing_legacy_or_flat_registry_never_looks_empty(self) -> None:
        invalid_roots: tuple[object, ...] = (
            {"format": "zdecision-registry/v1", "schema_version": 1},
            {
                "format": "legacy-registry/v0",
                "schema_version": 1,
                "products": {},
            },
            [],
        )
        for ordinal, value in enumerate(invalid_roots):
            with self.subTest(ordinal=ordinal):
                atomic_write_json(self.registry_root / "registry.json", value)
                with self.assertRaises(RegistryInvalid):
                    self.catalog.inspect((_seed(1),))

        (self.registry_root / "registry.json").unlink()
        with self.assertRaises(RegistryInvalid):
            self.catalog.inspect((_seed(1),))

        atomic_write_json(
            self.registry_root / "registry.json",
            {
                "format": "zdecision-registry/v1",
                "schema_version": 1,
                "products": {},
            },
        )
        (self.registry_root / "decisions").mkdir()
        with self.assertRaises(RegistryInvalid):
            self.catalog.inspect((_seed(1),))

    def test_symlinks_and_cross_product_documents_are_rejected(self) -> None:
        plan = self.catalog.inspect((_seed(1),))
        draft = self.catalog.render(plan, PREVIEW_ID)
        self.catalog.write_exact(draft.changed_files)
        decision_path = self.repository_root / next(
            path for path in draft.changed_files if path.endswith("r0001.json")
        )
        outside = self.repository_root / "outside.json"
        outside.write_bytes(decision_path.read_bytes())
        decision_path.unlink()
        decision_path.symlink_to(outside)

        with self.assertRaises(RegistryInvalid):
            self.catalog.inspect((_seed(2),))

        decision_path.unlink()
        value = json.loads(outside.read_text("utf-8"))
        value["product_id"] = "prod_" + "f" * 32
        atomic_write_json(decision_path, value)
        with self.assertRaises(RegistryInvalid):
            self.catalog.inspect((_seed(2),))

    def test_write_exact_rejects_escape_symlink_and_unplanned_bytes(self) -> None:
        with self.assertRaises(RegistryConflict):
            self.catalog.write_exact({"../outside.json": b"{}\n"})
        with self.assertRaises(RegistryConflict):
            self.catalog.write_exact({"decision-registry/README.md": b"changed\n"})
        original_root = (self.registry_root / "registry.json").read_bytes()
        with self.assertRaises(RegistryConflict):
            self.catalog.write_exact({ROOT_PATH: b"{}\n"})
        self.assertEqual(original_root, (self.registry_root / "registry.json").read_bytes())

        plan = self.catalog.inspect((_seed(1),))
        draft = self.catalog.render(plan, PREVIEW_ID)
        products = self.registry_root / "products"
        outside = self.repository_root / "outside"
        outside.mkdir()
        products.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RegistryConflict):
            self.catalog.write_exact(draft.changed_files)


if __name__ == "__main__":
    unittest.main()
