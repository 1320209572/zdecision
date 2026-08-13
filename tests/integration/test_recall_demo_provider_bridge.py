"""Publication-to-Recall proof for the bounded leadership Demo."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests import test_central_web_api
from tests.test_recall_demo_retrieval import (
    RecordingEmbedding,
    RecordingReranker,
    _cleanup_intent,
)
from zdecision.agent.control_bindings import ControlBindingStore
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import TestRepositoryMapping
from zdecision.agent.hooks import handle_hook
from zdecision.agent.recall_handoff import RecallHandoffService
from zdecision.agent.recall_host_state import RecallHostStore
from zdecision.agent.recall_mcp import RecallMcpTools
from zdecision.agent.recall_plugin_identity import PRODUCTION_RECALL_PLUGIN_IDENTITY
from zdecision.agent.repository import RepositoryResolver
from zdecision.central.auth import Principal
from zdecision.central.decision_spaces import (
    EnabledRepository,
    LeafDecisionSpace,
    RepositoryDecisionRoute,
)
from zdecision.central.web.application import CentralWebApplication
from zdecision.central.web.contracts import DraftItem
from zdecision.capture.models import CandidateContent
from zdecision.ids import (
    candidate_revision_id,
    decision_id,
    decision_space_id,
    publication_candidate_id,
    repository_route_id,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.recall.demo.bundle import load_verified_bundle
from zdecision.recall.demo.config import DemoProviderConfig, DemoPublisherConfig
from zdecision.recall.demo.contracts import DemoRetrievalProfile
from zdecision.recall.demo.model_store import InstalledModels, prepare_models
from zdecision.recall.demo.provider import DemoRecallProvider
from zdecision.recall.demo.publication import (
    DemoBundlePublisher,
    load_demo_bundle_pointer,
)
from zdecision.recall.demo.runtime import ModelRuntimeBundle
from zdecision.registry.models import ProductRegistry, RootProductEntry, RootRegistry
from zdecision.sync.contracts import (
    CandidateOwnershipSnapshot,
    CandidateRevisionUpload,
    RepositoryView,
)


ROOT = Path(__file__).resolve().parents[2]
PRODUCT_ID = "prod_3e6e73b8defbfee89ce7bf26e739b1dc"
PRODUCT_NAME = "third-party-services"
PRODUCT_SPACE_ID = decision_space_id("product", PRODUCT_ID)
REPOSITORY_ID = "repo_" + "7" * 32
FAMILY_ID = "cfm_" + "7" * 32
NOW = datetime(2026, 8, 13, 11, 0, tzinfo=UTC)
PROFILE_PATH = ROOT / "src/zdecision/recall/demo/demo-profile.json"
BASELINE_DECISIONS = (
    "dec_85e57f21d3a72fddb86749ccee0f8cbf",
    "dec_aac76c0a67bc535766c741f80066c706",
    "dec_d62aad7c1b160beaf4e31fa1a387d7e3",
)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _table_snapshot(path: Path, prefixes: tuple[str, ...]) -> tuple[tuple[str, int, str], ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        return tuple(
            (name, int(connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]), hashlib.sha256(sql.encode()).hexdigest())
            for name, sql in rows
            if name.startswith(prefixes)
        )


def _tree_snapshot(root: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


class RecallDemoProviderBridgeIntegrationTests(unittest.TestCase):
    def test_unpublished_candidate_is_absent_then_published_decision_is_recalled(self) -> None:
        """Removing any Central, provider, Hook, MCP, or application bridge breaks this."""

        production_registry = ROOT / "decision-registry"
        production_registry_before = _tree_snapshot(production_registry)
        preexisting_pointer = self._preexisting_pointer()

        central = test_central_web_api.CentralWebApiTest(methodName="runTest")
        central.setUp()
        self.addCleanup(central.doCleanups)
        assert central.web.previews is not None
        registry_repository = central.web.previews.catalog.repository_root
        self._install_registry_fixture(registry_repository)
        baseline_commit = _git(registry_repository, "rev-parse", "HEAD")
        central.synchronizer.synchronize(
            "org_demo", baseline_commit, "2026-08-13T10:59:00Z"
        )
        candidate_decision_id = self._install_candidate(central)
        central_candidate_before = self._central_domain_snapshot(
            central.store.connection
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider_config, publisher_config, installed = self._demo_state(
                root, registry_repository
            )
            publisher = DemoBundlePublisher(publisher_config)
            with self.subTest(boundary="publish-recall-apply"):
                pointer_n = publisher.refresh(baseline_commit)
                bundle_n = load_verified_bundle(
                    bundle_root=provider_config.bundle_state_root / pointer_n.bundle,
                    trust_root_path=provider_config.trust_root_path,
                )
                self.assertNotIn(
                    candidate_decision_id,
                    {item.decision_id for item in bundle_n.decisions},
                )

                current = central.web
                assert current.previews is not None
                central.web = CentralWebApplication(
                    store=current.store,
                    queries=current.queries,
                    catalog=current.previews.catalog,
                    git=current.previews.git,
                    registry_synchronizer=central.synchronizer,
                    recall_demo_publisher=publisher,
                )
                publication = self._publish_candidate(central)
                self.assertEqual("completed", publication.state)
                self.assertIsNotNone(publication.commit_sha)
                publication_commit = publication.commit_sha
                assert publication_commit is not None

                pointer_after = load_demo_bundle_pointer(provider_config)
                self.assertEqual(pointer_n.generation + 1, pointer_after.generation)
                self.assertEqual(publication_commit, pointer_after.publication_commit)
                self.assertNotEqual(pointer_n.generation_digest, pointer_after.generation_digest)

                agent_repository = root / "zstack-ui-next"
                self._create_agent_repository(agent_repository)
                agent_database_path = root / "agent.sqlite3"
                agent_database = AgentDatabase.open(agent_database_path)
                self.addCleanup(agent_database.close)
                recall_store = RecallHostStore.open(agent_database_path)
                self.addCleanup(recall_store.close)
                control_store = ControlBindingStore.open(agent_database_path)
                self.addCleanup(control_store.close)
                capture_database = root / "capture.sqlite3"
                sqlite3.connect(capture_database).close()
                state_before = _table_snapshot(
                    agent_database_path, ("candidate_", "slice_candidate_", "capture_")
                )
                capture_before = capture_database.read_bytes()
                resolver = RepositoryResolver(timeout_seconds=0.5)
                repository = resolver.resolve(agent_repository)
                self.assertIsNotNone(repository)
                assert repository is not None
                agent_database.put_test_repository_mapping(
                    TestRepositoryMapping(
                        repository_id=repository.repository_id,
                        product_id=PRODUCT_ID,
                        product_name=PRODUCT_NAME,
                        enabled=True,
                    )
                )
                agent_database.put_enabled_repository(
                    EnabledRepository(repository.repository_id, True)
                )
                plugin_root = root / "plugin-cache/zdecision/0.1.0"
                shutil.copytree(ROOT / "plugins/zdecision", plugin_root)

                provider = DemoRecallProvider(provider_config)
                provider.retrieve = mock.Mock(wraps=provider.retrieve)
                runtime = ModelRuntimeBundle(
                    profile_digest=bundle_n.profile.digest,
                    embedding=RecordingEmbedding(),
                    reranker=RecordingReranker((9.0, 8.0, 7.0, 6.0)),
                )
                intent = _cleanup_intent()
                with (
                    mock.patch.dict(os.environ, {"PLUGIN_ROOT": str(plugin_root)}),
                    mock.patch(
                        "zdecision.recall.demo.provider.load_transformers_runtime",
                        return_value=runtime,
                    ),
                ):
                    preflight = provider.preflight(
                        repository_id=repository.repository_id,
                        repository_display_name="zstack-ui-next",
                        intent=intent,
                        now=NOW,
                    )
                    self.assertEqual(pointer_after.generation, preflight.generation)
                    self.assertEqual(
                        pointer_after.generation_digest,
                        preflight.generation_digest,
                    )
                    self._observe_native_turn(
                        agent_database, recall_store, control_store, resolver,
                        provider, agent_repository, "turn-confirm",
                    )
                    bound = self._hook(
                        agent_database, recall_store, control_store, resolver,
                        provider, agent_repository,
                        {
                            "hook_event_name": "PreToolUse",
                            "session_id": "session-demo",
                            "turn_id": "turn-confirm",
                            "cwd": str(agent_repository),
                            "tool_name": PRODUCTION_RECALL_PLUGIN_IDENTITY.tool_name(
                                "show_zdecision_recall_confirmation"
                            ),
                            "tool_input": {"intent": intent.to_dict()},
                        },
                    )
                    binding = bound.output["hookSpecificOutput"]["updatedInput"]
                    attempt_id = binding["activation_attempt_id"]
                    ui_digest = "a" * 64
                    tools = RecallMcpTools(
                        host_store=recall_store,
                        handoff_service=RecallHandoffService(
                            store=recall_store,
                            provider=provider,
                            clock=lambda: NOW,
                            delivery_id_factory=lambda _: "delivery_" + "d" * 32,
                            claim_token_factory=lambda: "claim_" + "c" * 32,
                        ),
                        cwd=str(agent_repository),
                        clock=lambda: NOW,
                    )
                    card = tools.show_recall_confirmation(
                        activation_attempt_id=attempt_id,
                        intent=intent.to_dict(),
                        ui_digest=ui_digest,
                    )
                    self.assertEqual("pending_confirmation", card["state"])
                    enabled = tools.decide_recall_confirmation(
                        activation_attempt_id=attempt_id,
                        action="enable",
                        current_ui_digest=ui_digest,
                    )
                    self.assertEqual("delivery_claimed", enabled["state"])
                    self.assertEqual(1, provider.retrieve.call_count)
                    delivery = recall_store.delivery_for_attempt(attempt_id)
                    self.assertIsNotNone(delivery)
                    assert delivery is not None and delivery.shortlist is not None
                    recalled_ids = {
                        item.revision.decision_id for item in delivery.shortlist.items
                    }
                    self.assertIn(candidate_decision_id, recalled_ids)
                    tools.ack_recall_delivery(
                        activation_attempt_id=attempt_id,
                        delivery_id=delivery.delivery_id,
                        context_digest=delivery.context_digest,
                    )

                    self._observe_native_turn(
                        agent_database, recall_store, control_store, resolver,
                        provider, agent_repository, "turn-apply",
                    )
                    classifications = [
                        {
                            "decision_id": item.revision.decision_id,
                            "revision": item.revision.revision,
                            "digest": item.digest,
                            "disposition": (
                                "applicable"
                                if item.revision.decision_id == candidate_decision_id
                                else "not_applicable"
                            ),
                            "reason": "Bounded leadership Demo classification.",
                        }
                        for item in delivery.shortlist.items
                    ]
                    application_hook = self._hook(
                        agent_database, recall_store, control_store, resolver,
                        provider, agent_repository,
                        {
                            "hook_event_name": "PreToolUse",
                            "session_id": "session-demo",
                            "turn_id": "turn-apply",
                            "cwd": str(agent_repository),
                            "tool_name": PRODUCTION_RECALL_PLUGIN_IDENTITY.tool_name(
                                "apply_zdecision_recall_delivery"
                            ),
                            "tool_input": {"items": classifications},
                        },
                    )
                    application_binding = application_hook.output[
                        "hookSpecificOutput"
                    ]["updatedInput"]
                    applied = tools.apply_recall_delivery(**application_binding)
                    self.assertEqual("application_committed", applied["state"])
                    self.assertEqual(1, applied["disposition_counts"]["applicable"])
                    self.assertGreater(
                        applied["disposition_counts"]["not_applicable"], 0
                    )
                    released = self._hook(
                        agent_database, recall_store, control_store, resolver,
                        provider, agent_repository,
                        {
                            "hook_event_name": "PreToolUse",
                            "session_id": "session-demo",
                            "turn_id": "turn-apply",
                            "cwd": str(agent_repository),
                            "tool_name": "apply_patch",
                            "tool_input": {},
                        },
                    )
                    self.assertEqual({}, released.output)

                    recall_store.close()
                    restarted_store = RecallHostStore.open(agent_database_path)
                    self.addCleanup(restarted_store.close)
                    restarted_service = RecallHandoffService(
                        store=restarted_store,
                        provider=provider,
                        clock=lambda: NOW + timedelta(seconds=1),
                        delivery_id_factory=lambda _: "delivery_" + "d" * 32,
                        claim_token_factory=lambda: "claim_" + "e" * 32,
                    )
                    restarted_delivery = restarted_store.get_delivery(
                        delivery.delivery_id
                    )
                    self.assertIsNotNone(restarted_delivery)
                    assert restarted_delivery is not None
                    replay = restarted_service.apply(
                        session_id="session-demo",
                        turn_id="turn-apply",
                        gate_id=application_binding["turn_gate_id"],
                        delivery_id=delivery.delivery_id,
                        submission=restarted_delivery.application,
                    )
                    self.assertEqual(applied, replay)
                    self.assertEqual(1, provider.retrieve.call_count)

                self.assertEqual(
                    state_before,
                    _table_snapshot(
                        agent_database_path,
                        ("candidate_", "slice_candidate_", "capture_"),
                    ),
                )
                self.assertEqual(capture_before, capture_database.read_bytes())

        self.assertEqual(
            central_candidate_before,
            self._central_domain_snapshot(central.store.connection),
        )
        self.assertEqual(production_registry_before, _tree_snapshot(production_registry))
        self.assertEqual(preexisting_pointer, self._preexisting_pointer())

    def _install_registry_fixture(self, repository: Path) -> None:
        registry = repository / "decision-registry"
        shutil.rmtree(registry)
        product = registry / "products" / PRODUCT_ID
        (product / "decisions").mkdir(parents=True)
        root = RootRegistry(
            {
                PRODUCT_ID: RootProductEntry(
                    PRODUCT_NAME,
                    f"products/{PRODUCT_ID}/product.json",
                    f"products/{PRODUCT_ID}/registry.json",
                )
            }
        )
        (registry / "registry.json").write_bytes(canonical_json_bytes(root.to_dict()))
        shutil.copy2(
            ROOT / "decision-registry/products" / PRODUCT_ID / "product.json",
            product / "product.json",
        )
        heads = {}
        source_product = ROOT / "decision-registry/products" / PRODUCT_ID
        for decision_identifier in BASELINE_DECISIONS:
            source = source_product / "decisions" / decision_identifier
            shutil.copytree(source, product / "decisions" / decision_identifier)
            heads[decision_identifier] = json.loads(
                (source_product / "registry.json").read_text()
            )["decisions"][decision_identifier]
        (product / "registry.json").write_bytes(
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "format": "zdecision-product-registry/v1",
                    "product_id": PRODUCT_ID,
                    "decisions": heads,
                }
            )
        )
        _git(repository, "add", "decision-registry")
        _git(repository, "commit", "-m", "seed third-party-services registry")
        _git(repository, "push", "origin", "main")

    @staticmethod
    def _central_domain_snapshot(
        connection: sqlite3.Connection,
    ) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if row[0].startswith(
                ("candidate_", "capture_", "slice_candidate_")
            )
        )
        return tuple(
            (
                table,
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    )
                ),
            )
            for table in tables
        )

    def _install_candidate(self, central: test_central_web_api.CentralWebApiTest) -> str:
        content = CandidateContent(
            product=PRODUCT_NAME,
            claim="清理任务失败后的重试属于重试清理任务，不属于重新部署。",
            future_action="清理失败场景必须保持清理语义，不得复用会让用户理解为重新部署的操作名称或流程。",
            scope_summary="安全服务清理任务失败重试",
            repositories=("zstack-ui-next",),
            paths=(
                "packages/products/third-party-services/apps/security-services/src/domain/security-services/task-flow-definitions.ts",
            ),
            invalidation_conditions=(
                "清理失败不再支持重试",
                "清理重试被产品明确并入重新部署流程",
            ),
        )
        digest = hashlib.sha256(canonical_json_bytes(content.to_dict())).hexdigest()
        revision = CandidateRevisionUpload(
            family_id=FAMILY_ID,
            revision_id=candidate_revision_id(FAMILY_ID, 1, digest),
            revision=1,
            content=content,
            content_digest=digest,
            evidence_digest="7" * 64,
        )
        ownership = CandidateOwnershipSnapshot(
            repository_id=REPOSITORY_ID,
            route_id=repository_route_id(REPOSITORY_ID, PRODUCT_SPACE_ID),
            route_configuration_version=1,
            decision_space_id=PRODUCT_SPACE_ID,
            decision_space_kind="product",
            display_name=PRODUCT_NAME,
            catalog_breadcrumb=(),
            source_root="packages/products/third-party-services",
            compatibility_product_id=PRODUCT_ID,
            compatibility_product_name=PRODUCT_NAME,
            source_boundary_digest="7" * 64,
        )
        central.store.put_repository_mapping(
            "org_demo", RepositoryView(REPOSITORY_ID, PRODUCT_ID, PRODUCT_NAME, True)
        )
        central.store.put_repository("org_demo", EnabledRepository(REPOSITORY_ID, True))
        central.store.put_decision_space(
            "org_demo",
            LeafDecisionSpace(
                PRODUCT_SPACE_ID, "product", PRODUCT_NAME, PRODUCT_ID,
                PRODUCT_NAME, None, (),
                "packages/products/third-party-services", None, None, True,
            ),
        )
        central.store.replace_trusted_route_heads(
            "org_demo", REPOSITORY_ID,
            (
                RepositoryDecisionRoute(
                    ownership.route_id, REPOSITORY_ID, PRODUCT_SPACE_ID,
                    (ownership.source_root,), (), True, 1,
                ),
            ),
        )
        record = canonical_json_bytes(revision.to_dict())
        ownership_bytes = canonical_json_bytes(ownership.to_dict())
        with central.store.connection:
            central.store.connection.execute(
                "INSERT INTO candidate_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("org_demo", REPOSITORY_ID, FAMILY_ID, 1, revision.revision_id,
                 record.decode(), hashlib.sha256(record).hexdigest()),
            )
            central.store.connection.execute(
                """INSERT INTO candidate_family_heads(
                    organization_id, repository_id, family_id, revision, revision_id
                ) VALUES (?, ?, ?, ?, ?)""",
                ("org_demo", REPOSITORY_ID, FAMILY_ID, 1, revision.revision_id),
            )
            central.store.connection.execute(
                """INSERT INTO candidate_revision_ownership(
                    organization_id, repository_id, family_id, revision,
                    decision_space_id, route_id, route_configuration_version,
                    ownership_json, ownership_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("org_demo", REPOSITORY_ID, FAMILY_ID, 1, PRODUCT_SPACE_ID,
                 ownership.route_id, 1, ownership_bytes.decode(),
                 hashlib.sha256(ownership_bytes).hexdigest()),
            )
        return decision_id(publication_candidate_id(FAMILY_ID), PRODUCT_ID)

    def _publish_candidate(self, central: test_central_web_api.CentralWebApiTest):
        principal = Principal("user", "org_demo", "user_demo", None)
        inbox = central.web.list_candidates(principal, PRODUCT_SPACE_ID)
        item = inbox.items[0]
        draft_item = DraftItem(
            family_id=item.family_id,
            repository_id=item.repository_id,
            revision_id=item.revision_id,
            revision=item.revision,
            content_digest=item.content_digest,
            action="accept",
            effective_content=None,
        )
        draft = central.web.save_review_draft(
            principal, PRODUCT_SPACE_ID, 0, (draft_item,), NOW
        )
        review = central.web.submit_review(
            principal, PRODUCT_SPACE_ID, "web_action_demo-review",
            draft.version, draft.items, NOW,
        )
        preview = central.web.create_preview(
            principal, review.batch.review_batch_id,
            "web_action_demo-preview", NOW,
        )
        return central.web.publish(
            principal, preview.record.preview_id, "web_action_demo-publish", NOW
        )

    def _demo_state(
        self, root: Path, registry_repository: Path
    ) -> tuple[DemoProviderConfig, DemoPublisherConfig, InstalledModels]:
        private_key = Ed25519PrivateKey.generate()
        private_key_path = root / "signing.key"
        private_key_path.write_bytes(
            private_key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        )
        trust_path = root / "trust.pub"
        trust_path.write_bytes(
            private_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )
        os.chmod(private_key_path, 0o600)
        os.chmod(trust_path, 0o600)
        profile_value = json.loads(PROFILE_PATH.read_text())
        snapshots: dict[str, Path] = {}
        for role in ("embedding", "reranker"):
            snapshot = root / "model-cache" / role
            snapshot.mkdir(parents=True)
            snapshots[profile_value[role]["model_id"]] = snapshot
            for name in profile_value[role]["files"]:
                path = snapshot / name
                path.write_bytes(f"{role}:{name}\n".encode())
                profile_value[role]["files"][name] = {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size": path.stat().st_size,
                }
        profile = DemoRetrievalProfile.from_dict(profile_value)
        profile_path = root / "profile.json"
        profile_path.write_bytes(canonical_json_bytes(profile.to_dict()))
        model_state_root = root / "models"
        installed = prepare_models(
            profile=profile,
            state_root=model_state_root,
            snapshot_resolver=lambda model_id, _revision: snapshots[model_id],
        )
        provider = DemoProviderConfig(
            repository_name="zstack-ui-next",
            product_name=PRODUCT_NAME,
            decision_space_id=PRODUCT_ID,
            profile_path=profile_path,
            model_state_root=model_state_root,
            trust_root_path=trust_path,
            bundle_state_root=root / "bundles",
        )
        return provider, DemoPublisherConfig(
            provider=provider,
            registry_product_root=(
                registry_repository / "decision-registry/products" / PRODUCT_ID
            ),
            signing_private_key_path=private_key_path,
            signing_key_id="demo-leadership-v1",
        ), installed

    def _create_agent_repository(self, repository: Path) -> None:
        repository.mkdir()
        _git(repository, "init", "-b", "main")
        _git(repository, "config", "user.email", "demo@example.com")
        _git(repository, "config", "user.name", "Recall Demo")
        (repository / "README.md").write_text("demo\n")
        _git(repository, "add", "README.md")
        _git(repository, "commit", "-m", "fixture")
        _git(repository, "remote", "add", "origin", "https://example.invalid/zstack-ui-next.git")

    def _observe_native_turn(
        self, database, recall_store, control_store, resolver, provider,
        repository: Path, turn_id: str,
    ) -> None:
        self._hook(
            database, recall_store, control_store, resolver, provider, repository,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session-demo",
                "turn_id": turn_id,
                "cwd": str(repository),
                "prompt": "bounded native leadership turn",
            },
        )

    def _hook(
        self, database, recall_store, control_store, resolver, provider,
        repository: Path, value: dict[str, object],
    ):
        return handle_hook(
            value,
            database=database,
            clock=lambda: NOW,
            repository_resolver=resolver,
            worker_waker=lambda _: None,
            control_store=control_store,
            recall_store=recall_store,
            recall_provider=provider,
        )

    @staticmethod
    def _preexisting_pointer() -> tuple[str, bytes] | None:
        from zdecision.recall.demo.config import load_demo_recall_config, recall_demo_config_path

        config_path = recall_demo_config_path(os.environ)
        if not config_path.exists():
            return None
        try:
            config = load_demo_recall_config(config_path)
            pointer = config.provider.bundle_state_root / "current.json"
            return (str(pointer), pointer.read_bytes()) if pointer.exists() else None
        except (OSError, ValueError):
            return None


if __name__ == "__main__":
    unittest.main()
