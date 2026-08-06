from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from zdecision.capture.models import CandidateContent
from zdecision.capture.reviews import ApprovalRef
from zdecision.central.auth import Principal
from zdecision.central.decision_spaces import LeafDecisionSpace
from zdecision.central.registry_projection import RegistryProjectionStore
from zdecision.central.store import CentralStore
from zdecision.central.web.contracts import CentralReviewBatch, CentralReviewItem
from zdecision.central.web.previews import (
    CentralPreviewService,
    NoAcceptedItems,
    PreviewStale,
    RegistryUnavailable,
)
from zdecision.central.web.queries import CentralWebQueries
from zdecision.central.web.store import CentralWebStore, WebActionConflict
from zdecision.ids import (
    candidate_revision_id,
    central_review_batch_id,
    decision_space_id,
    product_id,
    publication_candidate_id,
    repository_route_id,
    review_item_id,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.registry.catalog import RegistryCatalog
from zdecision.registry.git import GitRegistryAdapter, RegistryOutOfSync
from zdecision.registry.models import RootRegistry
from zdecision.registry.query import RegistryQuery
from zdecision.sync.contracts import (
    CandidateOwnershipSnapshot,
    CandidateRevisionUpload,
    RepositoryView,
)


NOW = "2026-08-04T08:00:00Z"
LATER = "2026-08-04T08:01:00Z"
PRODUCT_NAME = "ZDecision"
PRODUCT_ID = product_id(PRODUCT_NAME)
REPOSITORY_ID = "repo_" + "1" * 32
FAMILY_ID = "cfm_" + "a" * 32
REJECTED_FAMILY_ID = "cfm_" + "b" * 32
PRODUCT_SPACE_ID = decision_space_id("product", PRODUCT_ID)


def content(claim: str) -> CandidateContent:
    return CandidateContent(
        product=PRODUCT_NAME,
        claim=claim,
        future_action="Inspect exact formal bytes before publication.",
        scope_summary="Central publication preview",
        repositories=("zdecision",),
        paths=("src/zdecision/central/web/",),
        invalidation_conditions=("The reviewed Candidate changes.",),
    )


class CentralPreviewServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.remote = root / "remote.git"
        self.repository = root / "repository"
        self._git("init", "--bare", str(self.remote), repository=root)
        self._git("init", "-b", "main", str(self.repository), repository=root)
        self._git("config", "user.email", "tests@example.com")
        self._git("config", "user.name", "ZDecision Tests")
        registry = self.repository / "decision-registry"
        registry.mkdir()
        (registry / "registry.json").write_bytes(
            canonical_json_bytes(RootRegistry({}).to_dict())
        )
        self._git("add", "decision-registry")
        self._git("commit", "-m", "initial registry")
        self._git("remote", "add", "origin", str(self.remote.resolve()))
        self._git("push", "-u", "origin", "main")

        self.central = CentralStore.open(root / "central.sqlite3")
        self.addCleanup(self.central.close)
        self.store = CentralWebStore(self.central.connection)
        self.user = Principal("user", "org_demo", "user_demo", None)
        self.central.put_repository_mapping(
            "org_demo",
            RepositoryView(REPOSITORY_ID, PRODUCT_ID, PRODUCT_NAME, True),
        )
        self.central.put_decision_space(
            "org_demo",
            LeafDecisionSpace(
                decision_space_id=PRODUCT_SPACE_ID,
                kind="product",
                display_name=PRODUCT_NAME,
                compatibility_product_id=PRODUCT_ID,
                compatibility_product_name=PRODUCT_NAME,
                catalog_group_id=None,
                catalog_breadcrumb=(),
                source_root=".",
                package_name=None,
                asset_type=None,
                enabled=True,
            ),
        )
        self.accepted_revision = self._insert_revision(
            FAMILY_ID, content("accepted claim")
        )
        self.rejected_revision = self._insert_revision(
            REJECTED_FAMILY_ID, content("rejected claim")
        )
        self.batch = self._review_batch(
            "web_action_review-1",
            (
                (self.accepted_revision, "accept"),
                (self.rejected_revision, "reject"),
            ),
        )
        self.store.put_review_batch(self.batch)
        git = GitRegistryAdapter(
            self.repository, expected_origin=str(self.remote.resolve())
        )
        registry_projection = RegistryProjectionStore(self.central.connection)
        snapshot = RegistryQuery(self.repository, git).snapshot()
        registry_projection.mark_syncing(
            "org_demo", snapshot.commit_sha, "1" * 40,
            "2026-08-06T10:00:00Z", "2026-08-06T10:00:00Z",
        )
        registry_projection.install(
            "org_demo", "1" * 40, snapshot,
            "2026-08-06T10:00:00Z", "2026-08-06T10:00:00Z",
        )
        self.service = CentralPreviewService(
            store=self.store,
            queries=CentralWebQueries(
                self.central.connection, registry_projection
            ),
            catalog=RegistryCatalog(self.repository),
            git=git,
        )

    def _git(
        self,
        *arguments: str,
        repository: Path | None = None,
        input_bytes: bytes | None = None,
    ) -> bytes:
        return subprocess.run(
            ("git", "-C", str(repository or self.repository), *arguments),
            input=input_bytes,
            check=True,
            capture_output=True,
        ).stdout

    def _insert_revision(
        self,
        family_id: str,
        candidate_content: CandidateContent,
        *,
        space: LeafDecisionSpace | None = None,
    ) -> CandidateRevisionUpload:
        owner = space or self.central.decision_space(
            "org_demo", PRODUCT_SPACE_ID
        )
        digest = hashlib.sha256(
            canonical_json_bytes(candidate_content.to_dict())
        ).hexdigest()
        revision = CandidateRevisionUpload(
            family_id=family_id,
            revision_id=candidate_revision_id(family_id, 1, digest),
            revision=1,
            content=candidate_content,
            content_digest=digest,
            evidence_digest="e" * 64,
        )
        payload = canonical_json_bytes(revision.to_dict())
        ownership = CandidateOwnershipSnapshot(
            repository_id=REPOSITORY_ID,
            route_id=repository_route_id(
                REPOSITORY_ID, owner.decision_space_id
            ),
            route_configuration_version=1,
            decision_space_id=owner.decision_space_id,
            decision_space_kind=owner.kind,
            display_name=owner.display_name,
            catalog_breadcrumb=owner.catalog_breadcrumb,
            source_root=owner.source_root,
            compatibility_product_id=owner.compatibility_product_id,
            compatibility_product_name=owner.compatibility_product_name,
            source_boundary_digest="9" * 64,
        )
        ownership_payload = canonical_json_bytes(ownership.to_dict())
        with self.central.connection:
            self.central.connection.execute(
                """
                INSERT INTO candidate_revisions(
                    organization_id, repository_id, family_id, revision,
                    revision_id, record_json, record_digest
                ) VALUES ('org_demo', ?, ?, 1, ?, ?, ?)
                """,
                (
                    REPOSITORY_ID,
                    family_id,
                    revision.revision_id,
                    payload.decode("utf-8"),
                    hashlib.sha256(payload).hexdigest(),
                ),
            )
            self.central.connection.execute(
                """
                INSERT INTO candidate_family_heads(
                    organization_id, repository_id, family_id, revision,
                    revision_id
                ) VALUES ('org_demo', ?, ?, 1, ?)
                """,
                (REPOSITORY_ID, family_id, revision.revision_id),
            )
            self.central.connection.execute(
                """
                INSERT INTO candidate_revision_ownership(
                    organization_id, repository_id, family_id, revision,
                    decision_space_id, route_id, route_configuration_version,
                    ownership_json, ownership_digest
                ) VALUES ('org_demo', ?, ?, 1, ?, ?, 1, ?, ?)
                """,
                (
                    REPOSITORY_ID,
                    family_id,
                    owner.decision_space_id,
                    ownership.route_id,
                    ownership_payload.decode("utf-8"),
                    hashlib.sha256(ownership_payload).hexdigest(),
                ),
            )
        return revision

    def _review_batch(
        self,
        action_id: str,
        selected: tuple[tuple[CandidateRevisionUpload, str], ...],
        *,
        space: LeafDecisionSpace | None = None,
    ) -> CentralReviewBatch:
        owner = space or self.central.decision_space(
            "org_demo", PRODUCT_SPACE_ID
        )
        identity_items = tuple(
            {
                "family_id": revision.family_id,
                "repository_id": REPOSITORY_ID,
                "revision_id": revision.revision_id,
                "revision": revision.revision,
                "content_digest": revision.content_digest,
                "action": action,
                "effective_content": None,
                "note": None,
            }
            for revision, action in selected
        )
        batch_id = central_review_batch_id(
            "org_demo", "user_demo", owner.compatibility_product_id,
            action_id, identity_items
        )
        items = tuple(
            CentralReviewItem(
                review_id=review_item_id(
                    batch_id, publication_candidate_id(revision.family_id)
                ),
                family_id=revision.family_id,
                publication_candidate_id=publication_candidate_id(
                    revision.family_id
                ),
                repository_id=REPOSITORY_ID,
                revision_id=revision.revision_id,
                revision=revision.revision,
                content_digest=revision.content_digest,
                action=action,
                effective_content=(
                    revision.content if action in ("accept", "edit_accept") else None
                ),
                note=None,
            )
            for revision, action in selected
        )
        return CentralReviewBatch(
            review_batch_id=batch_id,
            organization_id="org_demo",
            actor_id="user_demo",
            decision_space_id=owner.decision_space_id,
            compatibility_product_id=owner.compatibility_product_id,
            compatibility_product_name=owner.compatibility_product_name,
            client_action_id=action_id,
            request_digest=hashlib.sha256(action_id.encode()).hexdigest(),
            approval=ApprovalRef("user", "web_review", action_id, NOW),
            items=items,
            created_at=NOW,
        )

    def _registry_tree_bytes(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.repository).as_posix(): path.read_bytes()
            for path in (self.repository / "decision-registry").rglob("*")
            if path.is_file()
        }

    def test_preview_contains_only_accepted_effective_content_and_writes_nothing(
        self,
    ) -> None:
        before = self._registry_tree_bytes()

        view = self.service.create(
            self.user,
            self.batch.review_batch_id,
            "web_action_preview-1",
            NOW,
        )

        self.assertEqual(
            (publication_candidate_id(FAMILY_ID),), view.record.candidate_ids
        )
        rendered = "".join(file.content for file in view.record.display_documents)
        self.assertIn("accepted claim", rendered)
        self.assertNotIn("rejected claim", rendered)
        self.assertEqual(before, self._registry_tree_bytes())
        self.assertEqual("publishable", view.publishability)
        self.assertIsNone(view.publication_id)

        for document in view.record.display_documents:
            if not document.path.endswith(".json"):
                continue
            decoded = document.content
            self.assertNotIn("decision_space_id", decoded)
            self.assertNotIn("catalog_breadcrumb", decoded)
            self.assertNotIn("source_root", decoded)
            self.assertNotIn("asset_type", decoded)

    def test_repository_remap_after_review_keeps_frozen_v1_partition(self) -> None:
        remapped_name = "Other Product"
        self.central.put_repository_mapping(
            "org_demo",
            RepositoryView(
                REPOSITORY_ID,
                product_id(remapped_name),
                remapped_name,
                True,
            ),
        )

        try:
            view = self.service.create(
                self.user,
                self.batch.review_batch_id,
                "web_action_preview-remapped",
                NOW,
            )
        except PreviewStale:
            self.fail("preview ignored the frozen candidate ownership")

        self.assertEqual(PRODUCT_ID, view.record.product_id)
        self.assertEqual(PRODUCT_NAME, view.record.product_name)
        self.assertTrue(
            any(
                f"decision-registry/products/{PRODUCT_ID}/" in item.path
                for item in view.record.changed_files
            )
        )

    def test_preview_replay_is_exact_and_action_conflict_is_rejected(self) -> None:
        first = self.service.create(
            self.user, self.batch.review_batch_id, "web_action_preview-2", NOW
        )
        replay = self.service.create(
            self.user, self.batch.review_batch_id, "web_action_preview-2", NOW
        )
        other = self._review_batch(
            "web_action_review-2", ((self.accepted_revision, "accept"),)
        )
        self.store.put_review_batch(other)

        self.assertEqual(first, replay)
        with self.assertRaises(WebActionConflict):
            self.service.create(
                self.user,
                other.review_batch_id,
                "web_action_preview-2",
                NOW,
            )

    def test_different_action_for_same_preview_reuses_original_frozen_record(
        self,
    ) -> None:
        first = self.service.create(
            self.user, self.batch.review_batch_id, "web_action_preview-first", NOW
        )

        second = self.service.create(
            self.user,
            self.batch.review_batch_id,
            "web_action_preview-second",
            LATER,
        )

        self.assertEqual(first, second)
        self.assertEqual(NOW, second.record.created_at)
        self.assertEqual(
            (1, 2),
            (
                self.central.connection.execute(
                    "SELECT COUNT(*) FROM web_publication_previews"
                ).fetchone()[0],
                self.central.connection.execute(
                    "SELECT COUNT(*) FROM web_action_results WHERE action_kind='preview'"
                ).fetchone()[0],
            ),
        )

    def test_concurrent_action_replay_reports_current_stale_status(self) -> None:
        first = self.service.create(
            self.user, self.batch.review_batch_id, "web_action_preview-raced", NOW
        )
        original_action_result = self.store.action_result
        calls = 0

        def race_action_result(*args: object) -> object:
            nonlocal calls
            calls += 1
            result = original_action_result(*args)
            if calls == 1:
                (self.repository / "decision-registry" / "README.md").write_text(
                    "Registry base advanced during replay.\n", "utf-8"
                )
                self._git("add", "decision-registry/README.md")
                self._git("commit", "-m", "advance registry during replay")
                self._git("push", "origin", "main")
                return None
            return result

        with patch.object(self.store, "action_result", side_effect=race_action_result):
            raced = self.service.create(
                self.user,
                self.batch.review_batch_id,
                "web_action_preview-raced",
                LATER,
            )
        loaded = self.service.get(self.user, first.record.preview_id)

        self.assertEqual(first.record, raced.record)
        self.assertEqual("stale", raced.publishability)
        self.assertIsNone(raced.publication_id)
        self.assertEqual(loaded, raced)

    def test_base_change_before_atomic_freeze_writes_no_preview_or_action(
        self,
    ) -> None:
        class ChangingGit(GitRegistryAdapter):
            calls = 0

            def fetch_and_require_exact_main(
                self, expected_base: str | None = None
            ) -> str:
                self.calls += 1
                if self.calls == 2:
                    raise RegistryOutOfSync("base changed before freeze")
                return super().fetch_and_require_exact_main(expected_base)

        git = ChangingGit(
            self.repository, expected_origin=str(self.remote.resolve())
        )
        service = CentralPreviewService(
            store=self.store,
            queries=self.service.queries,
            catalog=RegistryCatalog(self.repository),
            git=git,
        )

        with self.assertRaises(RegistryUnavailable):
            service.create(
                self.user,
                self.batch.review_batch_id,
                "web_action_preview-race",
                NOW,
            )
        self.assertEqual(
            (0, 0),
            (
                self.central.connection.execute(
                    "SELECT COUNT(*) FROM web_publication_previews"
                ).fetchone()[0],
                self.central.connection.execute(
                    "SELECT COUNT(*) FROM web_action_results WHERE action_kind='preview'"
                ).fetchone()[0],
            ),
        )

    def test_new_review_or_registry_base_makes_preview_stale(self) -> None:
        preview = self.service.create(
            self.user, self.batch.review_batch_id, "web_action_preview-3", NOW
        )
        newer = self._review_batch(
            "web_action_review-3", ((self.accepted_revision, "accept"),)
        )
        self.store.put_review_batch(newer)

        loaded = self.service.get(self.user, preview.record.preview_id)

        self.assertEqual("stale", loaded.publishability)
        self.assertEqual(preview.record, loaded.record)

    def test_new_registry_base_makes_preview_stale(self) -> None:
        preview = self.service.create(
            self.user, self.batch.review_batch_id, "web_action_preview-base", NOW
        )
        (self.repository / "decision-registry" / "README.md").write_text(
            "Registry base changed.\n", "utf-8"
        )
        self._git("add", "decision-registry/README.md")
        self._git("commit", "-m", "change registry base")
        self._git("push", "origin", "main")

        loaded = self.service.get(self.user, preview.record.preview_id)

        self.assertEqual("stale", loaded.publishability)
        self.assertEqual(preview.record, loaded.record)

    def test_unreachable_registry_keeps_frozen_preview_readable(self) -> None:
        preview = self.service.create(
            self.user,
            self.batch.review_batch_id,
            "web_action_preview-unavailable",
            NOW,
        )
        unavailable = self.remote.with_name("remote-unavailable.git")
        self.remote.rename(unavailable)

        loaded = self.service.get(self.user, preview.record.preview_id)

        self.assertEqual("registry_unavailable", loaded.publishability)
        self.assertEqual(preview.record, loaded.record)

    def test_preview_reads_proven_commit_not_assume_unchanged_or_replacement_bytes(
        self,
    ) -> None:
        registry_path = self.repository / "decision-registry" / "registry.json"
        committed = registry_path.read_bytes()
        registry_path.write_text("not canonical registry bytes", "utf-8")
        self._git("update-index", "--assume-unchanged", "decision-registry/registry.json")
        original_blob = self._git(
            "rev-parse", "HEAD:decision-registry/registry.json"
        ).decode().strip()
        replacement_blob = self._git(
            "hash-object", "-w", "--stdin", input_bytes=b'{"replacement":true}\n'
        ).decode().strip()
        self._git("replace", original_blob, replacement_blob)

        view = self.service.create(
            self.user, self.batch.review_batch_id, "web_action_preview-4", NOW
        )

        root_document = next(
            file
            for file in view.record.display_documents
            if file.path == "decision-registry/registry.json"
        )
        self.assertNotEqual(committed.decode(), root_document.content)
        self.assertIn(PRODUCT_ID, root_document.content)
        self.assertNotIn("replacement", root_document.content)
        self.assertEqual(
            "not canonical registry bytes", registry_path.read_text("utf-8")
        )

    def test_rejected_only_review_has_no_preview(self) -> None:
        batch = self._review_batch(
            "web_action_review-rejected", ((self.rejected_revision, "reject"),)
        )
        self.store.put_review_batch(batch)

        with self.assertRaises(NoAcceptedItems):
            self.service.create(
                self.user, batch.review_batch_id, "web_action_preview-5", NOW
            )
        self.assertEqual(
            0,
            self.central.connection.execute(
                "SELECT COUNT(*) FROM web_action_results WHERE action_kind='preview'"
            ).fetchone()[0],
        )

    def test_shared_catalog_root_cannot_create_a_preview(self) -> None:
        before = self._registry_tree_bytes()

        with self.assertRaisesRegex(
            NoAcceptedItems, "decision_space_not_leaf"
        ):
            self.service.create(
                self.user,
                "dsg_" + "f" * 32,
                "web_action_preview-shared-root",
                NOW,
            )

        self.assertEqual(before, self._registry_tree_bytes())


if __name__ == "__main__":
    unittest.main()
