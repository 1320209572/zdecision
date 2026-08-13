"""Provider boundary tests for the local, consent-time Recall demo."""

from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from zdecision.recall.demo.bundle import VerifiedDemoBundle
from zdecision.recall.demo.config import DemoProviderConfig, DemoRecallConfig, DemoPublisherConfig
from zdecision.recall.demo.model_store import InstalledModels
from zdecision.recall.demo.publication import DemoBundlePointer
from zdecision.recall.demo.retrieval import DemoRecallResult, RankedDemoDecision
from zdecision.recall.demo.runtime import ModelRuntimeBundle
from zdecision.recall.handoff import (
    RecallPreflightClarification,
    RecallPreflightReady,
    RecallPreflightUnavailable,
)
from zdecision.recall.provider import RecallProviderUnavailable, UnavailableRecallProvider
from zdecision.recall.session import RecallIntent
from zdecision.registry.models import DecisionRevision

from zdecision.recall.demo.provider import (
    DemoRecallProvider,
    InstalledModelMetadata,
    VerifiedDemoBundleMetadata,
    configured_recall_provider,
)


ROOT = Path(__file__).parents[1]
PRODUCT_ID = "prod_3e6e73b8defbfee89ce7bf26e739b1dc"
PROFILE_PATH = ROOT / "src/zdecision/recall/demo/demo-profile.json"
DECISION_ID = "dec_aac76c0a67bc535766c741f80066c706"


def _intent(*, targets: tuple[str, ...] = (PRODUCT_ID,)) -> RecallIntent:
    return RecallIntent(
        target_decision_space_ids=targets,
        explicit_multi_space=len(targets) > 1,
        feature_goal="show an authorized cleanup entry",
        domain_objects=("SecurityServiceInstance",),
        repository_relative_paths=(
            "packages/products/third-party-services/apps/security-services",
        ),
        constraints=("follow backend authorization",),
        exclusions=("do not alter retry behavior",),
    )


class RecallDemoProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config = DemoProviderConfig(
            repository_name="zstack-ui-next",
            product_name="third-party-services",
            decision_space_id=PRODUCT_ID,
            profile_path=root / "profile.json",
            model_state_root=root / "models",
            trust_root_path=root / "trust.pub",
            bundle_state_root=root / "bundles",
        )
        from zdecision.recall.demo.contracts import DemoRetrievalProfile

        self.profile = DemoRetrievalProfile.from_dict(json.loads(PROFILE_PATH.read_text()))
        self.pointer = self._pointer()
        self.bundle_metadata = VerifiedDemoBundleMetadata(
            decision_space_id=PRODUCT_ID,
            product_name="third-party-services",
            repository="zstack-ui-next",
            profile=self.profile,
            manifest_digest=self.pointer.manifest_digest,
            decision_count=1,
            decision_leaves=((DECISION_ID, 1),),
        )
        self.model_metadata = InstalledModelMetadata(
            profile_digest=self.profile.digest,
            install_manifest_digest=self.pointer.model_install_digest,
        )
        self.revision = DecisionRevision.from_dict(
            json.loads(
                (
                    ROOT
                    / "decision-registry/products"
                    / PRODUCT_ID
                    / "decisions"
                    / DECISION_ID
                    / "r0001.json"
                ).read_text()
            )
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _pointer(self, **changes: object) -> DemoBundlePointer:
        base = {
            "schema_version": 1,
            "generation": 7,
            "publication_commit": "a" * 40,
            "bundle": "bundles/" + "a" * 40 + "/bundle",
            "manifest_digest": "b" * 64,
            "profile_digest": self.profile.digest if hasattr(self, "profile") else "c" * 64,
            "model_install_digest": hashlib.sha256(b"manifest").hexdigest(),
            "generation_digest": "e" * 64,
        }
        base.update(changes)
        unsigned = DemoBundlePointer(**base)
        digest_value = unsigned.to_dict()
        digest_value.pop("generation_digest")
        from zdecision.jsonio import canonical_json_bytes

        return DemoBundlePointer(
            **{**unsigned.__dict__, "generation_digest": hashlib.sha256(canonical_json_bytes(digest_value)).hexdigest()}
        )

    def _provider(self) -> DemoRecallProvider:
        return DemoRecallProvider(self.config)

    def _preflight(self) -> RecallPreflightReady:
        with self._metadata_ready():
            result = self._provider().preflight(
                repository_id="repo_1",
                repository_display_name="zstack-ui-next",
                intent=_intent(),
                now=datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
            )
        self.assertIsInstance(result, RecallPreflightReady)
        return result

    def _metadata_ready(self):
        return patch.object(
            DemoRecallProvider,
            "_current_metadata",
            return_value=(self.pointer, self.bundle_metadata, self.model_metadata),
        )

    def test_exact_product_preflight_freezes_selected_generation(self) -> None:
        """A ready result copies every public generation binding exactly."""
        preflight = self._preflight()

        self.assertEqual(self.pointer.manifest_digest, preflight.catalog_digest)
        self.assertEqual(self.pointer.generation, preflight.generation)
        self.assertEqual(self.pointer.generation_digest, preflight.generation_digest)
        self.assertEqual(self.pointer.profile_digest, preflight.retrieval_profile_digest)
        self.assertEqual(self.pointer.generation, preflight.index_generation)
        self.assertEqual("2026-08-13T10:15:00Z", preflight.expires_at)

    def test_ambiguous_intent_returns_only_third_party_services_display_name(self) -> None:
        """Unselected intent is bounded before any private provider lookup."""
        result = self._provider().preflight(
            repository_id="repo_1",
            repository_display_name="zstack-ui-next",
            intent=_intent(targets=(PRODUCT_ID, "prod_other")),
            now=datetime.now(UTC),
        )

        self.assertEqual(
            RecallPreflightClarification(
                code="recall_product_selection_required",
                candidate_display_names=("third-party-services",),
            ),
            result,
        )
        self.assertNotIn("provider", json.dumps(result.to_dict()))
        self.assertNotIn("正式决策", json.dumps(result.to_dict()))

    def test_wrong_repository_or_product_is_unavailable_or_clarification(self) -> None:
        """Only this repository and exact selected Decision Space are eligible."""
        wrong_repository = self._provider().preflight(
            repository_id="repo_1",
            repository_display_name="other-repository",
            intent=_intent(),
            now=datetime.now(UTC),
        )
        missing_product = self._provider().preflight(
            repository_id="repo_1",
            repository_display_name="zstack-ui-next",
            intent=_intent(targets=(PRODUCT_ID, "prod_other")),
            now=datetime.now(UTC),
        )
        other_product = self._provider().preflight(
            repository_id="repo_1",
            repository_display_name="zstack-ui-next",
            intent=_intent(targets=("prod_other",)),
            now=datetime.now(UTC),
        )

        self.assertIsInstance(wrong_repository, RecallPreflightUnavailable)
        self.assertIsInstance(missing_product, RecallPreflightClarification)
        self.assertIsInstance(other_product, RecallPreflightUnavailable)

    def test_preflight_does_not_import_torch_load_runtime_or_retrieve(self) -> None:
        """The Hook-safe side never crosses the model/retrieval boundary."""
        with (
            self._metadata_ready(),
            patch("zdecision.recall.demo.provider.load_verified_bundle", side_effect=AssertionError),
            patch("zdecision.recall.demo.provider.load_installed_models", side_effect=AssertionError),
            patch("zdecision.recall.demo.provider.load_transformers_runtime", side_effect=AssertionError),
            patch("zdecision.recall.demo.provider.HybridDemoRetriever.retrieve", side_effect=AssertionError),
        ):
            result = self._provider().preflight(
                repository_id="repo_1",
                repository_display_name="zstack-ui-next",
                intent=_intent(),
                now=datetime.now(UTC),
            )
        self.assertIsInstance(result, RecallPreflightReady)

    def test_preflight_emits_no_decision_text_or_private_path(self) -> None:
        """Preflight serialization exposes only formal public metadata."""
        preflight = self._preflight()
        serialized = json.dumps(preflight.to_dict())

        for sentinel in ("正式决策", str(self.config.bundle_state_root), "private.key"):
            self.assertNotIn(sentinel, serialized)

    def test_retrieve_maps_ranked_items_to_complete_recalled_decisions(self) -> None:
        """Consent-time ranked revisions become formal RecalledDecision values."""
        preflight = self._preflight()
        item = RankedDemoDecision(
            revision=self.revision,
            digest=self._revision_digest(),
            reranker_score=1.0,
            fused_score=1.0,
            match_reason="semantic+lexical+path",
        )
        result = DemoRecallResult(
            intent_digest=_intent().digest,
            profile_digest=self.profile.digest,
            manifest_digest=self.pointer.manifest_digest,
            items=(item,),
        )
        with self._retrieve_ready(result):
            shortlist = self._provider().retrieve(preflight)

        self.assertEqual((self.revision,), tuple(item.revision for item in shortlist.items))
        self.assertEqual((self._revision_digest(),), tuple(item.digest for item in shortlist.items))
        self.assertEqual(("semantic+lexical+path",), tuple(item.match_reason for item in shortlist.items))

    def test_retrieve_rejects_pointer_bundle_profile_or_model_generation_change(self) -> None:
        """Any changed generation binding invalidates an approved preflight."""
        preflight = self._preflight()
        result = DemoRecallResult(_intent().digest, self.profile.digest, self.pointer.manifest_digest, ())
        variants = (
            replace(self.pointer, generation=self.pointer.generation + 1),
            replace(self.bundle_metadata, manifest_digest="f" * 64),
            replace(self.bundle_metadata, profile=replace(self.profile, profile_id="other")),
            replace(self.model_metadata, install_manifest_digest="a" * 64),
        )
        for changed in variants:
            with self.subTest(changed=type(changed).__name__), self.assertRaisesRegex(
                RecallProviderUnavailable, "^Recall provider is unavailable$"
            ):
                with self._retrieve_ready(
                    result,
                    pointer=changed if isinstance(changed, DemoBundlePointer) else self.pointer,
                    bundle=changed if isinstance(changed, VerifiedDemoBundleMetadata) else self.bundle_metadata,
                    model=changed if isinstance(changed, InstalledModelMetadata) else self.model_metadata,
                ):
                    self._provider().retrieve(preflight)

    def test_empty_ranked_result_returns_a_valid_empty_shortlist(self) -> None:
        """No matching Decision remains a successful, bound empty shortlist."""
        preflight = self._preflight()
        result = DemoRecallResult(_intent().digest, self.profile.digest, self.pointer.manifest_digest, ())
        with self._retrieve_ready(result):
            shortlist = self._provider().retrieve(preflight)

        self.assertEqual((), shortlist.items)
        self.assertEqual(preflight.digest, shortlist.preflight_digest)

    def test_runtime_index_cache_is_keyed_by_exact_generation_digest(self) -> None:
        """Only identical immutable generation bindings reuse a runtime/index."""
        preflight = self._preflight()
        result = DemoRecallResult(_intent().digest, self.profile.digest, self.pointer.manifest_digest, ())
        builds: list[str] = []
        changed = self._pointer(generation=8, manifest_digest="f" * 64)
        changed_preflight = replace(
            preflight,
            catalog_digest=changed.manifest_digest,
            generation=changed.generation,
            generation_digest=changed.generation_digest,
            index_generation=changed.generation,
        )
        with self._retrieve_ready(result, build_calls=builds):
            provider = self._provider()
            provider.retrieve(preflight)
            provider.retrieve(preflight)
        changed_result = replace(result, manifest_digest=changed.manifest_digest)
        with self._retrieve_ready(
            changed_result,
            pointer=changed,
            bundle=replace(self.bundle_metadata, manifest_digest=changed.manifest_digest),
            build_calls=builds,
        ):
            provider.retrieve(changed_preflight)

        self.assertEqual([self.pointer.generation_digest, changed.generation_digest], builds)

    def test_invalid_or_missing_config_returns_unavailable_provider(self) -> None:
        """Configuration is a fail-closed construction boundary."""
        missing = configured_recall_provider(self.config.profile_path)
        self.config.profile_path.write_text("not valid json")
        invalid = configured_recall_provider(self.config.profile_path)

        self.assertIs(type(missing), UnavailableRecallProvider)
        self.assertIs(type(invalid), UnavailableRecallProvider)

    def _revision_digest(self) -> str:
        import hashlib

        from zdecision.jsonio import canonical_json_bytes

        return hashlib.sha256(canonical_json_bytes(self.revision.to_dict())).hexdigest()

    def _retrieve_ready(
        self,
        result: DemoRecallResult,
        *,
        pointer: DemoBundlePointer | None = None,
        bundle: VerifiedDemoBundleMetadata | None = None,
        model: InstalledModelMetadata | None = None,
        build_calls: list[str] | None = None,
    ):
        pointer = pointer or self.pointer
        bundle = bundle or self.bundle_metadata
        model = model or self.model_metadata
        verified_bundle = VerifiedDemoBundle(
            decision_space_id=PRODUCT_ID,
            product_name="third-party-services",
            repository="zstack-ui-next",
            profile=self.profile,
            decisions=(self.revision,),
            manifest_digest=pointer.manifest_digest,
        )
        runtime = ModelRuntimeBundle(self.profile.digest, object(), object())
        index = object()

        def build_index(*_args: object) -> object:
            if build_calls is not None:
                build_calls.append(pointer.generation_digest)
            return index

        class Retriever:
            def retrieve(self, *_args: object) -> DemoRecallResult:
                return result

        manifest = self.config.model_state_root / "manifest"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_bytes(b"manifest")
        installed = InstalledModels(
            self.profile.digest, Path("/embedding"), Path("/reranker"), manifest
        )
        patches = patch.multiple(
            "zdecision.recall.demo.provider",
            load_verified_bundle=lambda **_kwargs: verified_bundle,
            load_installed_models=lambda *_args: installed,
            load_transformers_runtime=lambda *_args: runtime,
            build_demo_index=build_index,
            HybridDemoRetriever=Retriever,
            _selected_bundle_root=lambda *_args: Path("/bundle"),
        )
        metadata = patch.object(
            DemoRecallProvider,
            "_current_metadata",
            return_value=(pointer, bundle, model),
        )
        return _PatchPair(patches, metadata)


class _PatchPair:
    def __init__(self, first, second) -> None:
        self._first = first
        self._second = second

    def __enter__(self):
        self._first.__enter__()
        self._second.__enter__()
        return self

    def __exit__(self, *arguments: object) -> None:
        self._second.__exit__(*arguments)
        self._first.__exit__(*arguments)
