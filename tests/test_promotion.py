from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.test_inventory import VALID_INVENTORY
from zdecision.capture.models import CandidateContent
from zdecision.capture.review_service import ReviewService
from zdecision.capture.reviews import ApprovalRef, ReviewSelection
from zdecision.ids import (
    PUBLISHER_FORMAT_VERSION,
    decision_id,
    product_id,
    publication_preview_id,
)
from zdecision.jsonio import atomic_write_json, canonical_json_bytes
from zdecision.private_store.filesystem import (
    FilePrivateStore,
    PrivateStateConflict,
    PrivateStateCorrupt,
)
from zdecision.registry.catalog import (
    DecisionUpdateNotSupported,
    RegistryCatalog,
)
from zdecision.registry.git import (
    GitRegistryAdapter,
    PublicationGitAmbiguous,
    RegistryOutOfSync,
)
from zdecision.registry.publication import (
    CandidatePublicationReceipt,
    PublicationFile,
    PublicationRecord,
    content_digest_for_files,
)
from zdecision.registry.service import (
    NoPublishableItems,
    PublicationApprovalConflict,
    PublicationConfirmationRequired,
    PublicationStale,
    PromotionService,
    ReviewSuperseded,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "decision-templates"
ENVELOPE_ROOT = (
    REPOSITORY_ROOT / "src" / "zdecision" / "capture" / "prompt_contracts"
)
PRODUCT_NAME = "安恒"
PRODUCT_ID = product_id(PRODUCT_NAME)
RAW_PRIVATE_SENTINEL = "RAW_CONVERSATION_SECRET_MUST_NOT_PUBLISH"


class _SequenceClock:
    def __init__(self, minute: int) -> None:
        self.minute = minute
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"2026-07-29T00:{self.minute:02d}:{self.calls:02d}Z"


def _approval() -> ApprovalRef:
    return ApprovalRef(
        actor="user",
        thread_id="thread-approval",
        turn_id="turn-approval",
        recorded_at="2026-07-29T00:00:00Z",
    )


def _private_record(
    *,
    state: str = "previewed",
    approval: ApprovalRef | None = None,
    commit_sha: str | None = None,
) -> PublicationRecord:
    candidate_id = "cand_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_01"
    decision_id_value = decision_id(candidate_id, PRODUCT_ID)
    display = (
        PublicationFile.from_bytes(
            "decision-registry/registry.json",
            canonical_json_bytes(
                {
                    "format": "zdecision-registry/v1",
                    "schema_version": 1,
                    "products": {},
                }
            ),
        ),
    )
    base_commit = "b" * 40
    base_registry_digests = {
        "decision-registry/registry.json": "c" * 64
    }
    review_ids = ("rvi_22222222222222222222222222222222",)
    preview_id = publication_preview_id(
        {
            "base_commit": base_commit,
            "base_registry_digests": base_registry_digests,
            "decision_ids": (decision_id_value,),
            "publisher_format": PUBLISHER_FORMAT_VERSION,
            "review_ids": review_ids,
            "target_paths": tuple(file.path for file in display),
        }
    )
    return PublicationRecord(
        record_version=1,
        preview_id=preview_id,
        content_digest=content_digest_for_files(display),
        state=state,
        created_at="2026-07-29T00:01:00Z",
        review_batch_id="rvb_11111111111111111111111111111111",
        review_ids=review_ids,
        candidate_ids=(candidate_id,),
        decision_ids=(decision_id_value,),
        product_id=PRODUCT_ID,
        product_name=PRODUCT_NAME,
        base_commit=base_commit,
        base_registry_digests=base_registry_digests,
        display_documents=display,
        changed_files=display,
        commit_message=(
            f"decision({PRODUCT_ID}): publish 1 decisions\n\n"
            f"ZDecision-Preview: {preview_id}\n"
        ),
        publication_approval=approval,
        commit_sha=commit_sha,
    )


class PublicationModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = FilePrivateStore(Path(self.temp_dir.name))

    def test_publication_file_and_previewed_record_round_trip_exact_bytes(self) -> None:
        record = _private_record()

        restored = PublicationRecord.from_dict(record.to_dict())

        self.assertEqual(record, restored)
        self.assertEqual(
            b'{"format":"zdecision-registry/v1","products":{},"schema_version":1}\n',
            restored.display_file_bytes()["decision-registry/registry.json"],
        )
        self.assertEqual(restored.display_file_bytes(), restored.changed_file_bytes())

    def test_content_digest_binds_sorted_path_and_every_content_byte(self) -> None:
        first = PublicationFile.from_bytes(
            "decision-registry/products/prod_" + "a" * 32 + "/product.json",
            b'{"a":1}\n',
        )
        second = PublicationFile.from_bytes(
            "decision-registry/registry.json",
            b'{"b":2}\n',
        )
        payload = {
            "documents": [
                {
                    "content": '{"a":1}\n',
                    "path": "decision-registry/products/prod_"
                    + "a" * 32
                    + "/product.json",
                },
                {"content": '{"b":2}\n', "path": "decision-registry/registry.json"},
            ]
        }
        expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

        self.assertEqual(expected, content_digest_for_files((first, second)))
        changed = replace(second, content='{"b":3}\n', sha256=hashlib.sha256(b'{"b":3}\n').hexdigest())
        self.assertNotEqual(expected, content_digest_for_files((first, changed)))

    def test_state_shape_requires_approval_and_commit_at_exact_boundaries(self) -> None:
        confirmation = _approval()
        valid = (
            _private_record(),
            _private_record(state="confirmed", approval=confirmation),
            _private_record(
                state="committed_pending_push",
                approval=confirmation,
                commit_sha="d" * 40,
            ),
            _private_record(
                state="completed",
                approval=confirmation,
                commit_sha="d" * 40,
            ),
        )
        for record in valid:
            with self.subTest(state=record.state):
                self.assertEqual(record, PublicationRecord.from_dict(record.to_dict()))

        invalid = (
            {**_private_record().to_dict(), "publication_approval": _approval().to_dict()},
            {**_private_record().to_dict(), "state": "confirmed"},
            {
                **_private_record().to_dict(),
                "state": "committed_pending_push",
                "publication_approval": _approval().to_dict(),
            },
            {**_private_record().to_dict(), "state": "unknown"},
            {**_private_record().to_dict(), "extra": True},
        )
        for value in invalid:
            with self.subTest(state=value.get("state")):
                with self.assertRaises(ValueError):
                    PublicationRecord.from_dict(value)

    def test_private_store_create_replay_compare_and_swap_are_strict(self) -> None:
        previewed = _private_record()
        confirmed = _private_record(state="confirmed", approval=_approval())

        self.assertEqual(previewed, self.store.create_publication(previewed))
        self.assertEqual(previewed, self.store.create_publication(previewed))
        self.assertEqual(previewed, self.store.get_publication(previewed.preview_id))
        self.assertEqual(
            confirmed,
            self.store.replace_publication(previewed, confirmed),
        )
        self.assertEqual(
            confirmed,
            self.store.replace_publication(previewed, confirmed),
        )
        pending = _private_record(
            state="committed_pending_push",
            approval=_approval(),
            commit_sha="d" * 40,
        )
        with self.assertRaises(PrivateStateConflict):
            self.store.replace_publication(previewed, pending)

    def test_private_store_rejects_collision_corruption_and_bad_receipt(self) -> None:
        record = _private_record()
        self.store.create_publication(record)
        path = self.store.root / "publications" / f"{record.preview_id}.json"
        path.write_text(json.dumps(record.to_dict(), ensure_ascii=False), "utf-8")
        with self.assertRaises(PrivateStateConflict):
            self.store.create_publication(record)
        path.write_text("{", "utf-8")
        with self.assertRaises(PrivateStateCorrupt):
            self.store.get_publication(record.preview_id)

        with self.assertRaises(ValueError):
            CandidatePublicationReceipt(
                candidate_id=record.candidate_ids[0],
                decision_id="dec_" + "f" * 32,
                product_id=record.product_id,
                preview_id=record.preview_id,
                commit_sha="d" * 40,
                recorded_at="2026-07-29T00:00:00Z",
            )

    def test_candidate_receipt_is_immutable_and_publication_scan_is_exact(self) -> None:
        record = _private_record()
        self.store.create_publication(record)
        receipt = CandidatePublicationReceipt(
            candidate_id=record.candidate_ids[0],
            decision_id=record.decision_ids[0],
            product_id=record.product_id,
            preview_id=record.preview_id,
            commit_sha="d" * 40,
            recorded_at="2026-07-29T00:00:00Z",
        )

        self.store.put_candidate_receipt(receipt)
        self.store.put_candidate_receipt(receipt)

        self.assertEqual(receipt, self.store.get_candidate_receipt(receipt.candidate_id))
        self.assertEqual(
            (record.preview_id,),
            self.store.publication_ids_for_candidates(record.candidate_ids),
        )
        self.assertEqual(
            (),
            self.store.publication_ids_for_candidates(
                ("cand_ffffffffffffffffffffffffffffffff_01",)
            ),
        )


class PublicationPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        from zdecision.capture.service import CaptureService
        from zdecision.capture.templates import TemplateCatalog

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.origin = self.root / "origin.git"
        self.repository = self.root / "repository"
        self.state_dir = self.root / "state"
        self.template_root = self.root / "templates"
        shutil.copytree(TEMPLATE_ROOT, self.template_root)

        self.git(self.root, "git", "init", "--bare", "--initial-branch=main", str(self.origin))
        self.git(self.root, "git", "init", "--initial-branch=main", str(self.repository))
        self.git(self.repository, "git", "config", "user.name", "ZDecision Test")
        self.git(self.repository, "git", "config", "user.email", "test@example.invalid")
        registry = self.repository / "decision-registry"
        registry.mkdir()
        atomic_write_json(
            registry / "registry.json",
            {
                "format": "zdecision-registry/v1",
                "schema_version": 1,
                "products": {},
            },
        )
        (registry / "README.md").write_text("formal only\n", "utf-8")
        self.git(self.repository, "git", "add", ".")
        self.git(self.repository, "git", "commit", "-m", "initial registry")
        self.git(self.repository, "git", "remote", "add", "origin", str(self.origin))
        self.git(self.repository, "git", "push", "-u", "origin", "main")

        self.store = FilePrivateStore(self.state_dir)
        self.capture_service = CaptureService(
            self.store,
            TemplateCatalog(self.template_root, ENVELOPE_ROOT),
        )
        self.review_clock = _SequenceClock(2)
        self.review_service = ReviewService(self.store, clock=self.review_clock)
        self.publication_clock = _SequenceClock(3)
        self.catalog = RegistryCatalog(self.repository)
        self.git_adapter = GitRegistryAdapter(
            self.repository,
            expected_origin=str(self.origin),
        )
        self.service = PromotionService(
            self.store,
            self.review_service,
            self.catalog,
            self.git_adapter,
            clock=self.publication_clock,
        )
        self.capture_id = self.complete_capture()
        record = self.store.get_capture(self.capture_id)
        assert record is not None
        self.candidate_ids = record.candidate_ids

    def git(self, cwd: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            args,
            cwd=cwd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def complete_capture(self) -> str:
        plan = self.capture_service.prepare(
            "thread-source",
            "turn-source",
            PRODUCT_NAME,
            "business",
        )
        operation_id = plan.record.operation_id
        self.capture_service.attach_fork(operation_id, "thread-fork")
        self.capture_service.attach_stage_turn(operation_id, "inventory", "turn-inventory")
        self.capture_service.complete_inventory(operation_id, VALID_INVENTORY)
        self.capture_service.attach_stage_turn(operation_id, "extraction", "turn-extraction")
        candidates = []
        for ordinal, claim in enumerate(
            (
                "正式决策按产品隔离保存。",
                "发布前必须展示完整正式内容。",
                RAW_PRIVATE_SENTINEL,
                "暂不处理的候选规则。",
            ),
            start=1,
        ):
            candidates.append(
                {
                    "product": PRODUCT_NAME,
                    "claim": claim,
                    "future_action": f"执行候选规则 {ordinal}。",
                    "scope": {
                        "summary": "ZDecision Publish",
                        "repositories": [
                            "https://github.com/1320209572/zdecision.git"
                        ],
                        "paths": ["decision-registry/"],
                    },
                    "invalidation_conditions": ["新的正式决策替代当前规则"],
                }
            )
        self.capture_service.complete_extraction(
            operation_id,
            {"candidates": candidates},
        )
        return operation_id

    def mixed_review(self):
        second = self.store.get_candidate(self.candidate_ids[1])
        assert second is not None
        edited = replace(second.content, claim="发布前展示完整内容与目标路径。")
        return self.review_service.record(
            self.capture_id,
            (
                ReviewSelection(self.candidate_ids[0], "accept"),
                ReviewSelection(self.candidate_ids[1], "edit_accept", edited),
                ReviewSelection(self.candidate_ids[2], "reject"),
                ReviewSelection(self.candidate_ids[3], "skip"),
            ),
            "thread-review",
            "turn-review-1",
        )

    def status(self) -> bytes:
        return self.git(
            self.repository,
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout

    def test_mixed_review_creates_exact_read_only_preview_for_accepted_items(self) -> None:
        batch = self.mixed_review()
        before = self.status()

        record = self.service.preview(batch.review_batch_id)

        self.assertEqual("previewed", record.state)
        self.assertEqual(before, self.status())
        self.assertEqual(2, len(record.decision_ids))
        self.assertEqual(
            (self.candidate_ids[0], self.candidate_ids[1]),
            record.candidate_ids,
        )
        self.assertEqual(PRODUCT_ID, record.product_id)
        self.assertIn(f"publish 2 decisions", record.commit_message)
        encoded = "".join(file.content for file in record.display_documents)
        for forbidden in (
            RAW_PRIVATE_SENTINEL,
            self.capture_id,
            batch.review_batch_id,
            *record.candidate_ids,
            *record.review_ids,
            "publication_approval",
        ):
            self.assertNotIn(forbidden, encoded)
        decision_documents = [
            json.loads(file.content)
            for file in record.display_documents
            if file.path.endswith("r0001.json")
        ]
        self.assertEqual(2, len(decision_documents))
        self.assertEqual(
            ["正式决策按产品隔离保存。", "发布前展示完整内容与目标路径。"],
            [value["claim"] for value in decision_documents],
        )
        for value in decision_documents:
            self.assertEqual(record.preview_id, value["publication_preview_id"])
            self.assertEqual(
                batch.approval.to_dict(),
                value["review_approval"],
            )
            self.assertEqual(
                {"thread_id": "thread-source", "turn_id": "turn-source"},
                value["source"],
            )

    def test_preview_replay_reuses_original_record_and_timestamp(self) -> None:
        batch = self.mixed_review()

        first = self.service.preview(batch.review_batch_id)
        replay = self.service.preview(batch.review_batch_id)

        self.assertEqual(first, replay)
        self.assertEqual("2026-07-29T00:03:01Z", replay.created_at)
        self.assertEqual(1, self.publication_clock.calls)
        self.assertEqual(
            canonical_json_bytes(first.to_dict()),
            (
                self.state_dir / "publications" / f"{first.preview_id}.json"
            ).read_bytes(),
        )

    def test_reject_and_skip_only_batch_has_no_publishable_items(self) -> None:
        batch = self.review_service.record(
            self.capture_id,
            (
                ReviewSelection(self.candidate_ids[0], "reject"),
                ReviewSelection(self.candidate_ids[1], "skip"),
            ),
            "thread-review",
            "turn-review-1",
        )

        with self.assertRaises(NoPublishableItems):
            self.service.preview(batch.review_batch_id)
        self.assertFalse((self.state_dir / "publications").exists())

    def test_newer_review_invalidates_an_older_unpublished_preview(self) -> None:
        batch = self.mixed_review()
        self.service.preview(batch.review_batch_id)
        self.review_service.record(
            self.capture_id,
            (ReviewSelection(self.candidate_ids[0], "reject"),),
            "thread-review",
            "turn-review-2",
        )

        with self.assertRaises(ReviewSuperseded):
            self.service.preview(batch.review_batch_id)

    def test_candidate_receipt_blocks_a_second_decision_before_git(self) -> None:
        batch = self.mixed_review()
        candidate_id = self.candidate_ids[0]
        self.store.put_candidate_receipt(
            CandidatePublicationReceipt(
                candidate_id=candidate_id,
                decision_id=decision_id(candidate_id, PRODUCT_ID),
                product_id=PRODUCT_ID,
                preview_id="pub_" + "f" * 32,
                commit_sha="d" * 40,
                recorded_at="2026-07-29T00:00:00Z",
            )
        )

        with self.assertRaises(DecisionUpdateNotSupported):
            self.service.preview(batch.review_batch_id)

    def test_existing_registry_head_blocks_duplicate_even_without_receipt(self) -> None:
        batch = self.mixed_review()
        preview = self.service.preview(batch.review_batch_id)
        self.catalog.write_exact(preview.changed_file_bytes())
        self.git(self.repository, "git", "add", "decision-registry")
        self.git(self.repository, "git", "commit", "-m", "seed formal decisions")
        self.git(self.repository, "git", "push", "origin", "main")

        with self.assertRaises(DecisionUpdateNotSupported):
            self.service.preview(batch.review_batch_id)

    def test_out_of_sync_main_stops_before_private_preview_write(self) -> None:
        batch = self.mixed_review()
        (self.repository / "source.txt").write_text("ahead\n", "utf-8")
        self.git(self.repository, "git", "add", "source.txt")
        self.git(self.repository, "git", "commit", "-m", "ahead")

        with self.assertRaises(RegistryOutOfSync):
            self.service.preview(batch.review_batch_id)
        self.assertFalse((self.state_dir / "publications").exists())


class _InjectedCrash(RuntimeError):
    pass


class PublicationConfirmationTests(unittest.TestCase):
    git = PublicationPreviewTests.git
    complete_capture = PublicationPreviewTests.complete_capture
    mixed_review = PublicationPreviewTests.mixed_review
    status = PublicationPreviewTests.status

    def setUp(self) -> None:
        PublicationPreviewTests.setUp(self)

    def preview(self) -> PublicationRecord:
        return self.service.preview(self.mixed_review().review_batch_id)

    def confirm(self, preview_id: str):
        return self.service.confirm(
            preview_id,
            "thread-publish",
            "turn-publish",
        )

    def test_confirmation_persists_approval_before_exact_batch_commit_and_push(self) -> None:
        preview = self.preview()
        exact_files = preview.changed_file_bytes()
        original_write = self.catalog.write_exact
        observed_states: list[str] = []

        def guarded_write(files):
            observed_states.append(self.service.get(preview.preview_id).state)
            original_write(files)

        self.catalog.write_exact = guarded_write
        result = self.confirm(preview.preview_id)

        self.assertEqual("completed", result.status)
        self.assertEqual(preview.decision_ids, result.decision_ids)
        completed = self.service.get(preview.preview_id)
        self.assertEqual("completed", completed.state)
        self.assertEqual(result.commit_sha, completed.commit_sha)
        self.assertEqual(["confirmed"], observed_states)
        self.assertEqual("thread-publish", completed.publication_approval.thread_id)
        self.assertEqual("turn-publish", completed.publication_approval.turn_id)
        self.assertEqual("2026-07-29T00:03:02Z", completed.publication_approval.recorded_at)
        self.assertEqual(b"", self.status())
        for path, expected in exact_files.items():
            self.assertEqual(expected, (self.repository / path).read_bytes())
        remote = self.git(
            self.root,
            "git",
            "--git-dir",
            str(self.origin),
            "rev-parse",
            "refs/heads/main",
        ).stdout.decode("ascii").strip()
        self.assertEqual(result.commit_sha, remote)
        for candidate_id, decision_id_value in zip(
            preview.candidate_ids,
            preview.decision_ids,
            strict=True,
        ):
            receipt = self.store.get_candidate_receipt(candidate_id)
            self.assertIsNotNone(receipt)
            self.assertEqual(decision_id_value, receipt.decision_id)
            self.assertEqual(result.commit_sha, receipt.commit_sha)

    def test_newer_review_changed_main_and_changed_registry_are_stale_before_approval(self) -> None:
        scenarios = ("review", "main", "registry")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.tearDown()
                self.setUp()
                preview = self.preview()
                if scenario == "review":
                    self.review_service.record(
                        self.capture_id,
                        (ReviewSelection(self.candidate_ids[0], "reject"),),
                        "thread-review",
                        "turn-review-2",
                    )
                elif scenario == "main":
                    (self.repository / "source.txt").write_text("ahead\n", "utf-8")
                    self.git(self.repository, "git", "add", "source.txt")
                    self.git(self.repository, "git", "commit", "-m", "ahead")
                else:
                    (self.repository / "decision-registry" / "registry.json").write_text(
                        "{}\n", "utf-8"
                    )

                with self.assertRaises(PublicationStale):
                    self.confirm(preview.preview_id)
                stored = self.service.get(preview.preview_id)
                self.assertEqual("previewed", stored.state)
                self.assertIsNone(stored.publication_approval)

    def test_retry_requires_original_confirmation_identity(self) -> None:
        preview = self.preview()
        first = self.confirm(preview.preview_id)

        replay = self.confirm(preview.preview_id)
        self.assertEqual(first, replay)
        with self.assertRaises(PublicationApprovalConflict):
            self.service.confirm(
                preview.preview_id,
                "thread-publish",
                "turn-publish-other",
            )
        approval = self.service.get(preview.preview_id).publication_approval
        self.assertEqual("turn-publish", approval.turn_id)

    def test_resume_rejects_an_unconfirmed_preview(self) -> None:
        preview = self.preview()

        with self.assertRaises(PublicationConfirmationRequired):
            self.service.resume(preview.preview_id)
        self.assertEqual("previewed", self.service.get(preview.preview_id).state)

    def test_crash_after_confirmed_retries_without_new_approval(self) -> None:
        preview = self.preview()
        original_write = self.catalog.write_exact

        def crash_before_write(files):
            raise _InjectedCrash("after confirmed")

        self.catalog.write_exact = crash_before_write
        with self.assertRaises(_InjectedCrash):
            self.confirm(preview.preview_id)
        confirmed = self.service.get(preview.preview_id)
        self.assertEqual("confirmed", confirmed.state)
        self.assertEqual(b"", self.status())

        self.catalog.write_exact = original_write
        result = self.service.resume(preview.preview_id)
        self.assertEqual("completed", result.status)
        self.assertEqual(
            confirmed.publication_approval,
            self.service.get(preview.preview_id).publication_approval,
        )
        self.assertEqual(2, self.publication_clock.calls)

    def test_crash_after_file_writes_reuses_only_exact_leftovers(self) -> None:
        preview = self.preview()
        original_commit = self.git_adapter.commit_exact

        def crash_before_commit(base_commit, message, changed_files):
            raise _InjectedCrash("after files")

        self.git_adapter.commit_exact = crash_before_commit
        with self.assertRaises(_InjectedCrash):
            self.confirm(preview.preview_id)
        self.assertEqual("confirmed", self.service.get(preview.preview_id).state)
        self.assertNotEqual(b"", self.status())

        self.git_adapter.commit_exact = original_commit
        result = self.service.resume(preview.preview_id)
        self.assertEqual("completed", result.status)
        self.assertEqual(b"", self.status())

    def test_crash_after_commit_adopts_the_unique_exact_child(self) -> None:
        preview = self.preview()
        original_receipt = self.store.put_candidate_receipt

        def crash_before_receipt(receipt):
            raise _InjectedCrash("after commit")

        self.store.put_candidate_receipt = crash_before_receipt
        with self.assertRaises(_InjectedCrash):
            self.confirm(preview.preview_id)
        confirmed = self.service.get(preview.preview_id)
        commit_sha = self.git(
            self.repository, "git", "rev-parse", "HEAD"
        ).stdout.decode("ascii").strip()
        self.assertEqual("confirmed", confirmed.state)
        self.assertNotEqual(preview.base_commit, commit_sha)

        self.store.put_candidate_receipt = original_receipt
        result = self.service.resume(preview.preview_id)
        self.assertEqual(commit_sha, result.commit_sha)
        self.assertEqual("completed", result.status)

    def test_pending_retry_handles_failure_before_and_after_remote_push(self) -> None:
        for push_succeeded in (False, True):
            with self.subTest(push_succeeded=push_succeeded):
                self.tearDown()
                self.setUp()
                preview = self.preview()
                original_push = self.git_adapter.push_exact

                def crash_push(commit_sha, base_commit):
                    if push_succeeded:
                        original_push(commit_sha, base_commit)
                    raise _InjectedCrash("at push boundary")

                self.git_adapter.push_exact = crash_push
                with self.assertRaises(_InjectedCrash):
                    self.confirm(preview.preview_id)
                pending = self.service.get(preview.preview_id)
                self.assertEqual("committed_pending_push", pending.state)
                self.assertIsNotNone(pending.commit_sha)

                self.git_adapter.push_exact = original_push
                result = self.service.resume(preview.preview_id)
                self.assertEqual("completed", result.status)
                self.assertEqual(pending.commit_sha, result.commit_sha)

    def test_wrong_child_commit_is_ambiguous_and_never_replaced(self) -> None:
        preview = self.preview()
        original_write = self.catalog.write_exact

        def crash_before_write(files):
            raise _InjectedCrash("after confirmed")

        self.catalog.write_exact = crash_before_write
        with self.assertRaises(_InjectedCrash):
            self.confirm(preview.preview_id)
        self.catalog.write_exact = original_write
        original_write(preview.changed_file_bytes())
        self.git(self.repository, "git", "add", "decision-registry")
        self.git(self.repository, "git", "commit", "-m", "wrong publication")
        wrong_head = self.git(
            self.repository, "git", "rev-parse", "HEAD"
        ).stdout.decode("ascii").strip()

        with self.assertRaises(PublicationGitAmbiguous):
            self.service.resume(preview.preview_id)
        self.assertEqual("confirmed", self.service.get(preview.preview_id).state)
        current_head = self.git(
            self.repository, "git", "rev-parse", "HEAD"
        ).stdout.decode("ascii").strip()
        self.assertEqual(wrong_head, current_head)

    def test_preview_reconciles_approved_publication_before_receipt_check(self) -> None:
        first_preview = self.preview()
        original_receipt = self.store.put_candidate_receipt

        def crash_before_receipt(receipt):
            raise _InjectedCrash("commit identified before receipt")

        self.store.put_candidate_receipt = crash_before_receipt
        with self.assertRaises(_InjectedCrash):
            self.confirm(first_preview.preview_id)
        self.assertEqual(
            "confirmed", self.service.get(first_preview.preview_id).state
        )
        self.assertIsNone(
            self.store.get_candidate_receipt(first_preview.candidate_ids[0])
        )
        self.store.put_candidate_receipt = original_receipt
        newer = self.review_service.record(
            self.capture_id,
            (ReviewSelection(self.candidate_ids[0], "accept"),),
            "thread-review",
            "turn-review-2",
        )

        with self.assertRaises(DecisionUpdateNotSupported):
            self.service.preview(newer.review_batch_id)
        self.assertEqual(
            "completed", self.service.get(first_preview.preview_id).state
        )

    def test_unconfirmed_older_preview_does_not_mutate_git(self) -> None:
        first_preview = self.preview()
        candidate = self.store.get_candidate(self.candidate_ids[0])
        edited = replace(candidate.content, claim="使用新的正式业务规则。")
        newer = self.review_service.record(
            self.capture_id,
            (ReviewSelection(self.candidate_ids[0], "edit_accept", edited),),
            "thread-review",
            "turn-review-2",
        )
        before_head = self.git(
            self.repository, "git", "rev-parse", "HEAD"
        ).stdout

        second_preview = self.service.preview(newer.review_batch_id)

        self.assertNotEqual(first_preview.preview_id, second_preview.preview_id)
        self.assertEqual(before_head, self.git(
            self.repository, "git", "rev-parse", "HEAD"
        ).stdout)
        self.assertEqual("previewed", self.service.get(first_preview.preview_id).state)


if __name__ == "__main__":
    unittest.main()
