"""Immutable publication contracts for the Recall demonstration."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.demo.bundle import VerifiedDemoBundle
from zdecision.recall.demo.config import DemoProviderConfig, DemoPublisherConfig
from zdecision.recall.demo.contracts import DemoRetrievalProfile


class RecallDemoPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile = DemoRetrievalProfile.from_dict(
            json.loads(
                (Path(__file__).parents[1] / "src/zdecision/recall/demo/demo-profile.json").read_text()
            )
        )
        self.provider = DemoProviderConfig(
            repository_name="zstack-ui-next",
            product_name="third-party-services",
            decision_space_id="prod_3e6e73b8defbfee89ce7bf26e739b1dc",
            profile_path=self.root / "profile.json",
            model_state_root=self.root / "models",
            trust_root_path=self.root / "trust.pub",
            bundle_state_root=self.root / "state",
        )
        self.provider.profile_path.write_bytes(canonical_json_bytes(self.profile.to_dict()))
        self.config = DemoPublisherConfig(
            provider=self.provider,
            registry_product_root=self.root / "registry",
            signing_private_key_path=self.root / "private.key",
            signing_key_id="demo-leadership-v1",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _bundle(self, digest: str = "b" * 64) -> VerifiedDemoBundle:
        return VerifiedDemoBundle(
            decision_space_id=self.provider.decision_space_id,
            product_name=self.provider.product_name,
            repository=self.provider.repository_name,
            profile=self.profile,
            decisions=(),
            manifest_digest=digest,
        )

    def _publisher(self):
        from zdecision.recall.demo.publication import DemoBundlePublisher

        return DemoBundlePublisher(self.config)

    def _dependencies(self, order: list[str], digest: str = "b" * 64):
        def build(**arguments: object) -> Path:
            order.append("build")
            bundle = Path(arguments["output_root"])
            bundle.mkdir(parents=True)
            return bundle

        def verify(**_arguments: object) -> VerifiedDemoBundle:
            order.append("verify")
            return self._bundle(digest)

        return build, verify

    def test_completed_commit_builds_verifies_then_selects_generation(self) -> None:
        """Selection must happen only after a completed signed bundle verifies."""
        order: list[str] = []
        build, verify = self._dependencies(order)
        with (
            patch("zdecision.recall.demo.publication.build_signed_bundle", build),
            patch("zdecision.recall.demo.publication.load_verified_bundle", verify),
            patch("zdecision.recall.demo.publication._prepared_model_digest", return_value="d" * 64),
            patch("zdecision.recall.demo.publication.os.replace", side_effect=lambda source, destination: (order.append("replace"), Path(source).rename(destination))[1]),
        ):
            pointer = self._publisher().refresh("a" * 40)
        self.assertEqual(1, pointer.generation)
        self.assertEqual("bundles/" + "a" * 40 + "/bundle", pointer.bundle)
        self.assertEqual(["build", "verify", "replace"], order)

    def test_same_commit_is_idempotent_and_does_not_resign(self) -> None:
        """Refreshing the selected commit must reuse its immutable generation."""
        order: list[str] = []
        build, verify = self._dependencies(order)
        with (
            patch("zdecision.recall.demo.publication.build_signed_bundle", build),
            patch("zdecision.recall.demo.publication.load_verified_bundle", verify),
            patch("zdecision.recall.demo.publication._prepared_model_digest", return_value="d" * 64),
        ):
            publisher = self._publisher()
            first = publisher.refresh("a" * 40)
            second = publisher.refresh("a" * 40)
        self.assertEqual(first, second)
        self.assertEqual(1, order.count("build"))

    def test_existing_commit_with_different_bytes_fails_closed(self) -> None:
        """Tampered immutable metadata must never be selected or overwritten."""
        order: list[str] = []
        build, verify = self._dependencies(order)
        with (
            patch("zdecision.recall.demo.publication.build_signed_bundle", build),
            patch("zdecision.recall.demo.publication.load_verified_bundle", verify),
            patch("zdecision.recall.demo.publication._prepared_model_digest", return_value="d" * 64),
        ):
            publisher = self._publisher()
            pointer = publisher.refresh("a" * 40)
            current = self.provider.bundle_state_root / "current.json"
            before = current.read_bytes()
            metadata = self.provider.bundle_state_root / "bundles" / ("a" * 40) / "generation.json"
            value = json.loads(metadata.read_text())
            value["manifest_digest"] = "f" * 64
            metadata.write_bytes(canonical_json_bytes(value))
            from zdecision.recall.demo.publication import RecallDemoPublicationError
            with self.assertRaises(RecallDemoPublicationError) as captured:
                publisher.refresh("a" * 40)
        self.assertEqual("generation_conflict", captured.exception.code)
        self.assertEqual(before, current.read_bytes())
        self.assertEqual(1, pointer.generation)

    def test_build_or_verify_failure_preserves_previous_pointer_bytes(self) -> None:
        """A failed stage must leave the selected pointer byte-for-byte intact."""
        for failure in ("build", "verify"):
            with self.subTest(failure=failure):
                order: list[str] = []
                build, verify = self._dependencies(order)
                with (
                    patch("zdecision.recall.demo.publication.build_signed_bundle", build),
                    patch("zdecision.recall.demo.publication.load_verified_bundle", verify),
                    patch("zdecision.recall.demo.publication._prepared_model_digest", return_value="d" * 64),
                ):
                    publisher = self._publisher()
                    publisher.refresh("a" * 40)
                    current = self.provider.bundle_state_root / "current.json"
                    before = current.read_bytes()
                    if failure == "build":
                        patched = patch("zdecision.recall.demo.publication.build_signed_bundle", side_effect=RuntimeError())
                    else:
                        patched = patch(
                            "zdecision.recall.demo.publication.load_verified_bundle",
                            side_effect=(self._bundle(), RuntimeError()),
                        )
                    with patched:
                        from zdecision.recall.demo.publication import RecallDemoPublicationError
                        with self.assertRaises(RecallDemoPublicationError):
                            publisher.refresh("b" * 40)
                    self.assertEqual(before, current.read_bytes())

    def test_pointer_rejects_absolute_escape_symlink_and_unknown_fields(self) -> None:
        """Pointers are closed, relative, and cannot cross the state-root boundary."""
        from zdecision.recall.demo.publication import (
            DemoBundlePointer,
            RecallDemoPublicationError,
            load_demo_bundle_pointer,
        )
        pointer = {
            "schema_version": 1,
            "generation": 2,
            "publication_commit": "a" * 40,
            "bundle": "bundles/" + "a" * 40 + "/bundle",
            "manifest_digest": "b" * 64,
            "profile_digest": "c" * 64,
            "model_install_digest": "d" * 64,
            "generation_digest": "e" * 64,
        }
        for mutation in (
            lambda value: value.__setitem__("bundle", "/outside/bundle"),
            lambda value: value.__setitem__("bundle", "bundles/../outside/bundle"),
            lambda value: value.__setitem__("unexpected", True),
        ):
            value = dict(pointer)
            mutation(value)
            with self.assertRaises(RecallDemoPublicationError):
                DemoBundlePointer.from_dict(value)
        self.provider.bundle_state_root.mkdir()
        target = self.root / "outside.json"
        target.write_bytes(canonical_json_bytes(pointer))
        (self.provider.bundle_state_root / "current.json").symlink_to(target)
        with self.assertRaises(RecallDemoPublicationError):
            load_demo_bundle_pointer(self.provider)

    def test_new_active_head_changes_generation_and_manifest_digest(self) -> None:
        """A new completed publication selects a distinct immutable generation."""
        order: list[str] = []
        build, verify = self._dependencies(order, "b" * 64)
        with (
            patch("zdecision.recall.demo.publication.build_signed_bundle", build),
            patch("zdecision.recall.demo.publication.load_verified_bundle", side_effect=(self._bundle("b" * 64), self._bundle("b" * 64), self._bundle("c" * 64))),
            patch("zdecision.recall.demo.publication._prepared_model_digest", return_value="d" * 64),
        ):
            publisher = self._publisher()
            first = publisher.refresh("a" * 40)
            second = publisher.refresh("b" * 40)
        self.assertEqual(2, second.generation)
        self.assertNotEqual(first.manifest_digest, second.manifest_digest)
        self.assertNotEqual(first.generation_digest, second.generation_digest)
