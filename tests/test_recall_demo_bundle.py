"""Contracts for the frozen third-party-services recall demonstration."""

from __future__ import annotations

import copy
import base64
import hashlib
import json
import math
import shutil
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zdecision.jsonio import canonical_json_bytes

from zdecision.recall.demo.bundle import (
    DemoBundleError,
    build_signed_bundle,
    load_verified_bundle,
    load_verified_bundle_metadata,
)
from zdecision.recall.demo import bundle as bundle_module
from zdecision.recall.demo.contracts import DemoRetrievalProfile


PROFILE_PATH = Path(__file__).parents[1] / "src/zdecision/recall/demo/demo-profile.json"
PRODUCT_ID = "prod_3e6e73b8defbfee89ce7bf26e739b1dc"
PRODUCT_ROOT = (
    Path(__file__).parents[1] / "decision-registry/products" / PRODUCT_ID
)
FOREIGN_DECISION_PATH = (
    Path(__file__).parents[1]
    / "decision-registry/products/prod_4d7b16e1616dd4cd1aeb2411836fd687"
    / "decisions/dec_822344cef4ce51de20c351af31ab356e/r0001.json"
)
EXPECTED_MODEL_FILES = {
    "embedding": {
        "config.json": {
            "sha256": "69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959",
            "size": 655,
        },
        "model.safetensors": {
            "sha256": "1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477",
            "size": 470641600,
        },
        "sentencepiece.bpe.model": {
            "sha256": "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
            "size": 5069051,
        },
        "special_tokens_map.json": {
            "sha256": "d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7",
            "size": 167,
        },
        "tokenizer.json": {
            "sha256": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
            "size": 17082730,
        },
        "tokenizer_config.json": {
            "sha256": "a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b",
            "size": 443,
        },
    },
    "reranker": {
        "config.json": {
            "sha256": "289adf7ada1eb6b4afa7589a48a032d45a076cf2e46dcdb3b4cabc33be14f708",
            "size": 799,
        },
        "model.safetensors": {
            "sha256": "ced967c45fd1902eb92716c9ceeca7c95a936770ea9db611f5a841b926e33fbd",
            "size": 1112206140,
        },
        "sentencepiece.bpe.model": {
            "sha256": "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
            "size": 5069051,
        },
        "special_tokens_map.json": {
            "sha256": "d5469a60db23249c7f8945013d78df30b44b6bf686c6bb4740f4223f77b1b535",
            "size": 279,
        },
        "tokenizer.json": {
            "sha256": "9eb652ac4e40cc093272bbbe0f55d521cf67570060227109b5cdc20945a4489e",
            "size": 17098107,
        },
        "tokenizer_config.json": {
            "sha256": "a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b",
            "size": 443,
        },
    },
}


class DemoProfileTests(unittest.TestCase):
    """The committed profile is an exact, finite retrieval contract."""

    def setUp(self) -> None:
        self.value = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))

    def test_committed_profile_has_exact_demo_values(self) -> None:
        """Changing an approved frozen profile value must fail this contract."""
        self.assertIn("files", self.value["embedding"])
        self.assertIn("files", self.value["reranker"])
        profile = DemoRetrievalProfile.from_dict(self.value)

        self.assertEqual("recall-demo-third-party-services-v1", profile.profile_id)
        self.assertEqual(
            "intfloat/multilingual-e5-small", profile.embedding.model_id
        )
        self.assertEqual(
            "614241f622f53c4eeff9890bdc4f31cfecc418b3",
            profile.embedding.revision,
        )
        self.assertEqual("BAAI/bge-reranker-base", profile.reranker.model_id)
        self.assertEqual(
            "2cfc18c9415c912f9d8155881c133215df768a70",
            profile.reranker.revision,
        )
        self.assertEqual(512, profile.embedding.max_tokens)
        self.assertEqual(512, profile.reranker.max_tokens)
        self.assertEqual(384, profile.embedding_dimension)
        self.assertEqual(10, profile.bm25_depth)
        self.assertEqual(10, profile.dense_depth)
        self.assertEqual(10, profile.path_depth)
        self.assertEqual(10, profile.union_depth)
        self.assertEqual(10, profile.rerank_depth)
        self.assertEqual(60, profile.reciprocal_rank_constant)
        self.assertEqual(1.0, profile.bm25_weight)
        self.assertEqual(1.0, profile.dense_weight)
        self.assertEqual(1.5, profile.path_weight)
        self.assertEqual(8, profile.max_shortlist_items)
        self.assertEqual(10_000, profile.max_shortlist_utf8_bytes)
        self.assertEqual(3.0, profile.reranker_threshold)
        self.assertEqual(
            EXPECTED_MODEL_FILES["embedding"],
            profile.embedding.to_dict()["files"],
        )
        self.assertEqual(
            EXPECTED_MODEL_FILES["reranker"],
            profile.reranker.to_dict()["files"],
        )
        self.assertEqual(
            "d24fd982285f8c454b5be4f56d1a4fd16a0eab047f3e7bf466133c847c50771a",
            profile.digest,
        )

    def test_model_file_bindings_are_sorted_and_immutable(self) -> None:
        """A mutable or nondeterministically ordered allowlist weakens the signature."""
        profile = DemoRetrievalProfile.from_dict(self.value)

        for role, spec in (
            ("embedding", profile.embedding),
            ("reranker", profile.reranker),
        ):
            self.assertTrue(hasattr(spec, "files"))
            self.assertIsInstance(spec.files, tuple)
            self.assertEqual(
                tuple(sorted(EXPECTED_MODEL_FILES[role])),
                tuple(binding.name for binding in spec.files),
            )
            with self.assertRaises(FrozenInstanceError):
                spec.files[0].size = 0

    def test_model_file_schema_requires_exact_six_names_and_binding_fields(self) -> None:
        """Missing, extra, or partially bound model files must not be signed."""
        mutations = (
            lambda files: files.pop("config.json"),
            lambda files: files.__setitem__(
                "unexpected.json", copy.deepcopy(files["config.json"])
            ),
            lambda files: files["config.json"].pop("size"),
            lambda files: files["config.json"].__setitem__("unexpected", 1),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                value = copy.deepcopy(self.value)
                self.assertIn("files", value["embedding"])
                mutation(value["embedding"]["files"])
                with self.assertRaises(ValueError):
                    DemoRetrievalProfile.from_dict(value)

    def test_model_file_schema_rejects_non_exact_sizes(self) -> None:
        """Only non-negative JSON integers may bind a model file size."""
        for invalid_size in (True, 1.0, -1):
            with self.subTest(invalid_size=invalid_size):
                value = copy.deepcopy(self.value)
                self.assertIn("files", value["embedding"])
                value["embedding"]["files"]["config.json"]["size"] = invalid_size
                with self.assertRaises(ValueError):
                    DemoRetrievalProfile.from_dict(value)

    def test_model_file_schema_rejects_malformed_sha256(self) -> None:
        """A digest that is short or not lowercase hexadecimal is not exact."""
        for invalid_digest in ("A" * 64, "0" * 63):
            with self.subTest(invalid_digest=invalid_digest):
                value = copy.deepcopy(self.value)
                self.assertIn("files", value["embedding"])
                value["embedding"]["files"]["config.json"][
                    "sha256"
                ] = invalid_digest
                with self.assertRaises(ValueError):
                    DemoRetrievalProfile.from_dict(value)

    def test_rejects_extra_profile_field(self) -> None:
        """Accepting an unbound profile field would weaken the signed contract."""
        value = copy.deepcopy(self.value)
        value["unexpected"] = "value"

        with self.assertRaises(ValueError):
            DemoRetrievalProfile.from_dict(value)

    def test_rejects_non_sha_model_revision(self) -> None:
        """A mutable model reference must not enter the frozen profile."""
        value = copy.deepcopy(self.value)
        value["embedding"]["revision"] = "main"

        with self.assertRaises(ValueError):
            DemoRetrievalProfile.from_dict(value)

    def test_rejects_non_positive_retrieval_depth(self) -> None:
        """A zero retrieval depth would make the demo profile non-functional."""
        value = copy.deepcopy(self.value)
        value["dense_depth"] = 0

        with self.assertRaises(ValueError):
            DemoRetrievalProfile.from_dict(value)

    def test_rejects_boolean_profile_scalar(self) -> None:
        """JSON booleans must not be accepted as numeric profile fields."""
        value = copy.deepcopy(self.value)
        value["schema_version"] = True

        with self.assertRaises(ValueError):
            DemoRetrievalProfile.from_dict(value)

    def test_rejects_non_finite_fusion_weight(self) -> None:
        """NaN fusion weights would break canonical signing and ranking."""
        value = copy.deepcopy(self.value)
        value["bm25_weight"] = math.nan

        with self.assertRaises(ValueError):
            DemoRetrievalProfile.from_dict(value)

    def test_rejects_non_finite_reranker_threshold(self) -> None:
        """Infinite thresholds are not valid deterministic retrieval inputs."""
        value = copy.deepcopy(self.value)
        value["reranker_threshold"] = math.inf

        with self.assertRaises(ValueError):
            DemoRetrievalProfile.from_dict(value)

    def test_rejects_out_of_range_reranker_threshold(self) -> None:
        """Thresholds outside the approved bounded range must not be accepted."""
        value = copy.deepcopy(self.value)
        value["reranker_threshold"] = 20.1

        with self.assertRaises(ValueError):
            DemoRetrievalProfile.from_dict(value)

    def test_profile_round_trips_through_canonical_json(self) -> None:
        """A changed serialization shape must fail the frozen profile contract."""
        profile = DemoRetrievalProfile.from_dict(self.value)
        restored = DemoRetrievalProfile.from_dict(
            json.loads(canonical_json_bytes(profile.to_dict()))
        )

        self.assertEqual(profile, restored)
        self.assertEqual(
            canonical_json_bytes(self.value), canonical_json_bytes(profile.to_dict())
        )

    def test_rejects_profile_that_would_change_canonical_json_on_round_trip(self) -> None:
        """Numeric normalization must not silently change signed profile bytes."""
        value = copy.deepcopy(self.value)
        value["bm25_weight"] = 1

        with self.assertRaises(ValueError):
            DemoRetrievalProfile.from_dict(value)


class DemoBundleTests(unittest.TestCase):
    """The distributable demo contains only signed public material."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private_key = Ed25519PrivateKey.generate()
        self.private_key_path = self.root / "signing.key"
        self.private_key_path.write_bytes(
            self.private_key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        )
        self.trust_root_path = self.root / "trust-root.pub"
        self.trust_root_path.write_bytes(
            self.private_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build(self) -> Path:
        return build_signed_bundle(
            product_root=PRODUCT_ROOT,
            profile_path=PROFILE_PATH,
            private_key_path=self.private_key_path,
            key_id="recall-demo-test-key",
            output_root=self.root / "bundle",
        )

    def _product_copy(self, name: str = "product-copy") -> Path:
        product_root = self.root / name
        shutil.copytree(PRODUCT_ROOT, product_root)
        return product_root

    def _write_head(
        self,
        product_root: Path,
        *,
        decision_id: str,
        revision: int,
        lifecycle: str = "active",
    ) -> None:
        registry_path = product_root / "registry.json"
        registry = json.loads(registry_path.read_text())
        source_path = next((product_root / "decisions").glob("*/r0001.json"))
        decision = json.loads(source_path.read_text())
        decision["decision_id"] = decision_id
        decision["revision"] = revision
        destination = product_root / "decisions" / decision_id / f"r{revision:04d}.json"
        destination.parent.mkdir()
        destination.write_bytes(canonical_json_bytes(decision))
        registry["decisions"][decision_id] = {
            "head_path": str(destination.relative_to(product_root)),
            "head_revision": revision,
            "lifecycle": lifecycle,
        }
        registry_path.write_bytes(canonical_json_bytes(registry))

    def _assert_rejected(self, bundle: Path | None = None) -> None:
        with self.assertRaises(DemoBundleError) as captured:
            load_verified_bundle(
                bundle_root=bundle or self.root / "bundle",
                trust_root_path=self.trust_root_path,
            )
        self.assertRegex(captured.exception.code, r"^[a-z_]+$")
        self.assertEqual(captured.exception.code, str(captured.exception))
        self.assertNotIn("cleanup", str(captured.exception).lower())
        self.assertNotIn("用户应获得", str(captured.exception))
        self.assertNotIn(self.private_key_path.read_bytes().hex(), str(captured.exception))

    def _assert_rejected_code(self, code: str) -> None:
        with self.assertRaises(DemoBundleError) as captured:
            load_verified_bundle(
                bundle_root=self.root / "bundle",
                trust_root_path=self.trust_root_path,
            )
        self.assertEqual(code, captured.exception.code)
        self.assertNotEqual("payload_invalid", captured.exception.code)

    def _rewrite_snapshot_and_resign(self, snapshot: dict[str, object]) -> None:
        """Bind a test-mutated snapshot with the trusted test signing key."""
        bundle = self.root / "bundle"
        snapshot_bytes = canonical_json_bytes(snapshot)
        (bundle / "snapshot.json").write_bytes(snapshot_bytes)
        signed = json.loads((bundle / "signed-manifest.json").read_text())
        manifest = signed["manifest"]
        manifest["files"]["snapshot.json"] = {
            "sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
            "byte_length": len(snapshot_bytes),
        }
        manifest["decision_count"] = len(snapshot["decisions"])
        manifest["decision_leaves"] = [
            {"decision_id": item["decision_id"], "revision": item["revision"]}
            for item in snapshot["decisions"]
        ]
        signed["signature"] = base64.b64encode(
            self.private_key.sign(canonical_json_bytes(manifest))
        ).decode("ascii")
        (bundle / "signed-manifest.json").write_bytes(canonical_json_bytes(signed))

    def _rewrite_manifest_and_resign(self, signed: dict[str, object]) -> None:
        signed["signature"] = base64.b64encode(
            self.private_key.sign(canonical_json_bytes(signed["manifest"]))
        ).decode("ascii")
        (self.root / "bundle/signed-manifest.json").write_bytes(
            canonical_json_bytes(signed)
        )

    def test_builds_verifiable_active_sorted_bundle(self) -> None:
        """A changed source leaf or bundle composition must fail acceptance."""
        bundle = self._build()

        verified = load_verified_bundle(
            bundle_root=bundle, trust_root_path=self.trust_root_path
        )

        self.assertEqual(
            {"snapshot.json", "retrieval-profile.json", "signed-manifest.json"},
            {path.name for path in bundle.iterdir()},
        )
        self.assertEqual(PRODUCT_ID, verified.decision_space_id)
        self.assertEqual("third-party-services", verified.product_name)
        self.assertEqual(10, len(verified.decisions))
        self.assertEqual(
            tuple(sorted(decision.decision_id for decision in verified.decisions)),
            tuple(decision.decision_id for decision in verified.decisions),
        )
        self.assertTrue(all(item.lifecycle == "active" for item in verified.decisions))
        self.assertTrue(
            all(item.repositories == ("zstack-ui-next",) for item in verified.decisions)
        )

    def test_metadata_verification_never_materializes_snapshot_decisions(self) -> None:
        """Preflight reads only bounded manifest/profile metadata, never snapshot prose."""
        bundle = self._build()
        original = bundle_module._read_bound_payload

        def read_bound(path, manifest, name):
            if name == "snapshot.json":
                raise AssertionError("snapshot decisions were materialized")
            return original(path, manifest, name)

        with patch.object(bundle_module, "_read_bound_payload", side_effect=read_bound):
            metadata = load_verified_bundle_metadata(
                bundle_root=bundle, trust_root_path=self.trust_root_path
            )

        self.assertEqual(10, metadata.decision_count)
        self.assertEqual(10, len(metadata.decision_leaves))

    def test_bundle_accepts_an_eleventh_active_head(self) -> None:
        """An additional complete active formal head must be signed and returned."""
        product_root = self._product_copy()
        self._write_head(
            product_root,
            decision_id="dec_0123456789abcdef0123456789abcdef",
            revision=1,
        )

        bundle = build_signed_bundle(
            product_root=product_root,
            profile_path=PROFILE_PATH,
            private_key_path=self.private_key_path,
            key_id="recall-demo-test-key",
            output_root=self.root / "bundle",
        )

        verified = load_verified_bundle(
            bundle_root=bundle, trust_root_path=self.trust_root_path
        )
        self.assertEqual(11, len(verified.decisions))

    def test_bundle_accepts_one_and_thirty_two_active_heads(self) -> None:
        """The valid formal active corpus may range from one through thirty-two."""
        for count in (1, 32):
            with self.subTest(count=count):
                product_root = self._product_copy(f"product-copy-{count}")
                registry_path = product_root / "registry.json"
                registry = json.loads(registry_path.read_text())
                registry["decisions"] = {}
                registry_path.write_bytes(canonical_json_bytes(registry))
                for number in range(count):
                    self._write_head(
                        product_root,
                        decision_id=f"dec_{number:032x}",
                        revision=1,
                    )
                bundle = build_signed_bundle(
                    product_root=product_root,
                    profile_path=PROFILE_PATH,
                    private_key_path=self.private_key_path,
                    key_id="recall-demo-test-key",
                    output_root=self.root / f"bundle-{count}",
                )
                verified = load_verified_bundle(
                    bundle_root=bundle, trust_root_path=self.trust_root_path
                )
                self.assertEqual(count, len(verified.decisions))

    def test_bundle_rejects_zero_or_more_than_thirty_two_active_heads(self) -> None:
        """The publisher must bound the active corpus before it signs any bundle."""
        for count in (0, 33):
            with self.subTest(count=count):
                product_root = self._product_copy(f"invalid-product-copy-{count}")
                registry_path = product_root / "registry.json"
                registry = json.loads(registry_path.read_text())
                registry["decisions"] = {}
                registry_path.write_bytes(canonical_json_bytes(registry))
                for number in range(count):
                    self._write_head(
                        product_root,
                        decision_id=f"dec_{number:032x}",
                        revision=1,
                    )
                with self.assertRaises(DemoBundleError) as captured:
                    build_signed_bundle(
                        product_root=product_root,
                        profile_path=PROFILE_PATH,
                        private_key_path=self.private_key_path,
                        key_id="recall-demo-test-key",
                        output_root=self.root / f"bundle-{count}",
                    )
                self.assertEqual("source_invalid", captured.exception.code)

    def test_rejects_equal_length_snapshot_tampering(self) -> None:
        """A changed snapshot byte must not survive its signed digest check."""
        bundle = self._build()
        snapshot_path = bundle / "snapshot.json"
        content = snapshot_path.read_bytes()
        position = content.index(b"active")
        snapshot_path.write_bytes(content[:position] + b"x" + content[position + 1 :])

        self._assert_rejected(bundle)

    def test_rejects_retrieval_profile_tampering(self) -> None:
        """A changed retrieval configuration must fail its signed payload binding."""
        bundle = self._build()
        profile_path = bundle / "retrieval-profile.json"
        profile_path.write_bytes(profile_path.read_bytes() + b" ")

        self._assert_rejected(bundle)

    def test_rejects_signed_model_file_binding_tampering(self) -> None:
        """A changed model-byte allowlist must fail the existing bundle signature chain."""
        bundle = self._build()
        profile_path = bundle / "retrieval-profile.json"
        value = json.loads(profile_path.read_text())
        self.assertIn("files", value["embedding"])
        value["embedding"]["files"]["config.json"]["sha256"] = "0" * 64
        profile_path.write_bytes(canonical_json_bytes(value))

        with self.assertRaises(DemoBundleError) as captured:
            load_verified_bundle(
                bundle_root=bundle,
                trust_root_path=self.trust_root_path,
            )
        self.assertEqual("payload_invalid", captured.exception.code)

    def test_rejects_altered_signed_manifest(self) -> None:
        """A manifest alteration must fail before the bound payloads are opened."""
        bundle = self._build()
        signed = json.loads((bundle / "signed-manifest.json").read_text())
        signed["manifest"]["decision_count"] = 9
        (bundle / "signed-manifest.json").write_bytes(canonical_json_bytes(signed))

        self._assert_rejected(bundle)

    def test_rejects_bundle_with_another_public_key(self) -> None:
        """A bundle signed by an untrusted private key must not load."""
        bundle = self._build()
        other_key = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        wrong_trust_root = self.root / "other.pub"
        wrong_trust_root.write_bytes(other_key)

        with self.assertRaises(DemoBundleError) as captured:
            load_verified_bundle(bundle_root=bundle, trust_root_path=wrong_trust_root)
        self.assertEqual(captured.exception.code, str(captured.exception))

    def test_accepts_resigned_bundle_within_active_bounds(self) -> None:
        """A trusted snapshot may contain any signed active count within bounds."""
        self._build()
        snapshot_path = self.root / "bundle/snapshot.json"
        snapshot = json.loads(snapshot_path.read_text())
        snapshot["decisions"].pop()
        self._rewrite_snapshot_and_resign(snapshot)

        verified = load_verified_bundle(
            bundle_root=self.root / "bundle", trust_root_path=self.trust_root_path
        )
        self.assertEqual(9, len(verified.decisions))

    def test_rejects_duplicate_decision(self) -> None:
        """Duplicating a leaf must fail the exact unique-identity contract."""
        self._build()
        snapshot = json.loads((self.root / "bundle/snapshot.json").read_text())
        snapshot["decisions"].append(copy.deepcopy(snapshot["decisions"][0]))
        self._rewrite_snapshot_and_resign(snapshot)

        self._assert_rejected_code("manifest_invalid")

    def test_rejects_foreign_product_decision(self) -> None:
        """A formal revision from another product must not enter this demo."""
        self._build()
        snapshot = json.loads((self.root / "bundle/snapshot.json").read_text())
        snapshot["decisions"][0] = json.loads(FOREIGN_DECISION_PATH.read_text())
        snapshot["decisions"].sort(key=lambda item: item["decision_id"])
        self._rewrite_snapshot_and_resign(snapshot)

        self._assert_rejected_code("snapshot_invalid")

    def test_rejects_inactive_revision(self) -> None:
        """Inactive formal leaves must never be returned to retrieval."""
        self._build()
        snapshot = json.loads((self.root / "bundle/snapshot.json").read_text())
        snapshot["decisions"][0]["lifecycle"] = "inactive"
        self._rewrite_snapshot_and_resign(snapshot)

        self._assert_rejected_code("snapshot_invalid")

    def test_rejects_malformed_revision(self) -> None:
        """Malformed formal leaves must never be returned to retrieval."""
        self._build()
        snapshot = json.loads((self.root / "bundle/snapshot.json").read_text())
        snapshot["decisions"][0]["claim"] = ""
        self._rewrite_snapshot_and_resign(snapshot)

        self._assert_rejected_code("snapshot_invalid")

    def test_rejects_private_key_below_distributable_root(self) -> None:
        """A signing key path inside a distributable root is forbidden."""
        private_path = self.root / "key-in-bundle/private.key"
        private_path.parent.mkdir()
        private_path.write_bytes(self.private_key_path.read_bytes())

        with self.assertRaises(DemoBundleError) as captured:
            build_signed_bundle(
                product_root=PRODUCT_ROOT,
                profile_path=PROFILE_PATH,
                private_key_path=private_path,
                key_id="recall-demo-test-key",
                output_root=self.root / "key-in-bundle",
            )
        self.assertEqual("private_key_location", captured.exception.code)

    def test_build_accepts_registry_with_nine_heads(self) -> None:
        """A valid active source set below ten must still be publishable."""
        product_root = self.root / "product-copy"
        shutil.copytree(PRODUCT_ROOT, product_root)
        registry_path = product_root / "registry.json"
        registry = json.loads(registry_path.read_text())
        registry["decisions"].pop(next(iter(registry["decisions"])))
        registry_path.write_bytes(canonical_json_bytes(registry))

        bundle = build_signed_bundle(
            product_root=product_root,
            profile_path=PROFILE_PATH,
            private_key_path=self.private_key_path,
            key_id="recall-demo-test-key",
            output_root=self.root / "bundle",
        )
        self.assertEqual(
            9,
            len(load_verified_bundle(bundle_root=bundle, trust_root_path=self.trust_root_path).decisions),
        )

    def test_verification_rejects_resigned_bundle_without_active_decisions(self) -> None:
        """A trusted signature still cannot select an empty active corpus."""
        self._build()
        snapshot = json.loads((self.root / "bundle/snapshot.json").read_text())
        snapshot["decisions"] = []
        self._rewrite_snapshot_and_resign(snapshot)

        self._assert_rejected()

    def test_rejects_boolean_manifest_schema_version_after_resigning(self) -> None:
        """The manifest schema version must be an integer, not JSON true."""
        self._build()
        signed = json.loads((self.root / "bundle/signed-manifest.json").read_text())
        signed["manifest"]["schema_version"] = True
        self._rewrite_manifest_and_resign(signed)

        self._assert_rejected()

    def test_rejects_boolean_manifest_leaf_revision_after_resigning(self) -> None:
        """A manifest leaf revision must be an integer, not JSON true."""
        self._build()
        signed = json.loads((self.root / "bundle/signed-manifest.json").read_text())
        signed["manifest"]["decision_leaves"][0]["revision"] = True
        self._rewrite_manifest_and_resign(signed)

        self._assert_rejected()

    def test_rejects_boolean_snapshot_schema_version_after_resigning(self) -> None:
        """The snapshot schema version must be an integer, not JSON true."""
        self._build()
        snapshot = json.loads((self.root / "bundle/snapshot.json").read_text())
        snapshot["schema_version"] = True
        self._rewrite_snapshot_and_resign(snapshot)

        self._assert_rejected()

    def test_atomic_publish_never_replaces_an_existing_empty_destination(self) -> None:
        """The low-level publication primitive must provide no-replace semantics."""
        source = self.root / "source"
        destination = self.root / "destination"
        source.mkdir()
        destination.mkdir()

        with self.assertRaises(FileExistsError):
            bundle_module._rename_no_replace(source, destination)

        self.assertTrue(source.exists())
        self.assertTrue(destination.exists())
