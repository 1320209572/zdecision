from __future__ import annotations

import unittest
from unittest.mock import patch

from tests import test_central_web_preview as preview_fixtures
from zdecision.central.web.publications import (
    CandidateAlreadyPublishing,
    CentralPublicationService,
    PublicationAmbiguous,
)
from zdecision.central.web.store import WebActionConflict, WebRecordConflict
from zdecision.ids import central_publication_id
from zdecision.registry.catalog import RegistryCatalog
from zdecision.registry.git import GitRegistryAdapter, RegistryPushFailed


class InjectedCrash(Exception):
    pass


def raise_at(name: str):
    def checkpoint(actual: str) -> None:
        if actual == name:
            raise InjectedCrash(actual)

    return checkpoint


class _UnknownPushOnce(GitRegistryAdapter):
    def __init__(self, wrapped: GitRegistryAdapter) -> None:
        super().__init__(wrapped.repository_root, wrapped.expected_origin)
        self.failed = False

    def push_exact(self, commit_sha: str, base_commit: str) -> None:
        super().push_exact(commit_sha, base_commit)
        if not self.failed:
            self.failed = True
            raise RegistryPushFailed("verification unavailable")


class CentralPublicationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = preview_fixtures.CentralPreviewServiceTest(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.preview = self.fixture.service.create(
            self.fixture.user, self.fixture.batch.review_batch_id,
            "web_action_preview-publication", preview_fixtures.NOW,
        ).record
        self.git = self.fixture.service.git
        self.service = CentralPublicationService(
            store=self.fixture.store,
            previews=self.fixture.service,
            catalog=RegistryCatalog(self.fixture.repository),
            git=self.git,
            clock=lambda: preview_fixtures.LATER,
        )

    def confirm(self, action_id: str = "web_action_publish-1"):
        return self.service.confirm(
            self.fixture.user, self.preview.preview_id, action_id,
            preview_fixtures.NOW,
        )

    def stored(self):
        return self.fixture.store.get_publication_by_preview(
            "org_demo", self.preview.preview_id
        )

    def commit_count(self) -> int:
        return int(self.fixture._git("rev-list", "--count", "HEAD").strip())

    def remote_head(self) -> str:
        return self.fixture._git(
            "rev-parse", "refs/remotes/origin/main"
        ).decode().strip()

    def test_confirmation_is_durable_before_git_mutation(self) -> None:
        self.service.checkpoint = raise_at("after_confirmation")

        with self.assertRaises(InjectedCrash):
            self.confirm()

        stored = self.stored()
        self.assertIsNotNone(stored)
        self.assertEqual("confirmed", stored.state)
        self.assertIsNone(stored.commit_sha)
        self.assertEqual(1, self.commit_count())

    def test_commit_success_before_state_write_is_adopted_once(self) -> None:
        self.service.checkpoint = raise_at("after_commit")
        with self.assertRaises(InjectedCrash):
            self.confirm()
        commit_count = self.commit_count()
        self.service.checkpoint = lambda _: None

        result = self.service.resume(
            self.fixture.user,
            central_publication_id(self.preview.preview_id),
            "web_action_resume-1",
            preview_fixtures.LATER,
        )

        self.assertEqual(commit_count, self.commit_count())
        self.assertEqual("completed", result.state)
        self.assertEqual(result.commit_sha, self.remote_head())

    def test_crash_before_commit_reuses_frozen_worktree_bytes(self) -> None:
        self.service.checkpoint = raise_at("before_commit")
        with self.assertRaises(InjectedCrash):
            self.confirm()
        self.assertEqual("confirmed", self.stored().state)
        self.assertEqual(1, self.commit_count())
        self.service.checkpoint = lambda _: None

        completed = self.service.resume(
            self.fixture.user,
            central_publication_id(self.preview.preview_id),
            "web_action_resume-before-commit",
            preview_fixtures.LATER,
        )

        self.assertEqual("completed", completed.state)
        self.assertEqual(2, self.commit_count())

    def test_interrupted_push_stays_pending_and_retries_same_commit(self) -> None:
        uncertain = _UnknownPushOnce(self.git)
        self.service.git = uncertain

        pending = self.confirm()
        completed = self.service.resume(
            self.fixture.user,
            pending.publication_id,
            "web_action_resume-2",
            preview_fixtures.LATER,
        )

        self.assertEqual("committed_pending_push", pending.state)
        self.assertEqual("completed", completed.state)
        self.assertEqual(pending.commit_sha, completed.commit_sha)
        self.assertEqual(2, self.commit_count())

    def test_crash_after_remote_push_recovers_without_second_commit(self) -> None:
        self.service.checkpoint = raise_at("after_push")
        with self.assertRaises(InjectedCrash):
            self.confirm()
        pending = self.stored()
        commit_count = self.commit_count()
        self.service.checkpoint = lambda _: None

        completed = self.service.resume(
            self.fixture.user,
            pending.publication_id,
            "web_action_resume-3",
            preview_fixtures.LATER,
        )

        self.assertEqual("completed", completed.state)
        self.assertEqual(pending.commit_sha, completed.commit_sha)
        self.assertEqual(commit_count, self.commit_count())

    def test_unrelated_remote_state_latches_ambiguous_without_second_commit(self) -> None:
        self.service.checkpoint = raise_at("after_confirmation")
        with self.assertRaises(InjectedCrash):
            self.confirm()
        self.service.checkpoint = lambda _: None
        clone = self.fixture.repository.parent / "competitor"
        self.fixture._git(
            "clone", str(self.fixture.remote), str(clone),
            repository=self.fixture.repository.parent,
        )
        self.fixture._git(
            "checkout", "-b", "main", "origin/main", repository=clone
        )
        self.fixture._git("config", "user.email", "tests@example.com", repository=clone)
        self.fixture._git("config", "user.name", "Competitor", repository=clone)
        (clone / "unrelated.txt").write_text("unrelated\n", "utf-8")
        self.fixture._git("add", "unrelated.txt", repository=clone)
        self.fixture._git("commit", "-m", "unrelated", repository=clone)
        self.fixture._git("push", "origin", "main", repository=clone)

        with self.assertRaises(PublicationAmbiguous):
            self.service.resume(
                self.fixture.user,
                central_publication_id(self.preview.preview_id),
                "web_action_resume-4",
                preview_fixtures.LATER,
            )
        with self.assertRaises(PublicationAmbiguous):
            self.service.resume(
                self.fixture.user,
                central_publication_id(self.preview.preview_id),
                "web_action_resume-5",
                preview_fixtures.LATER,
            )

        self.assertEqual("ambiguous", self.stored().recovery_code)
        self.assertEqual(1, self.commit_count())
        history = self.service.list(
            self.fixture.user, state="ambiguous", limit=50, offset=0
        )
        self.assertEqual(1, history.total)
        self.assertEqual("ambiguous", history.items[0].state)

    def test_publish_and_resume_action_replays_are_idempotent(self) -> None:
        first = self.confirm("web_action_publish-replay")
        replay = self.confirm("web_action_publish-replay")
        second_action = self.confirm("web_action_publish-second")
        resumed = self.service.resume(
            self.fixture.user, first.publication_id,
            "web_action_resume-replay", preview_fixtures.LATER,
        )
        resumed_replay = self.service.resume(
            self.fixture.user, first.publication_id,
            "web_action_resume-replay", preview_fixtures.LATER,
        )

        self.assertEqual(first, replay)
        self.assertEqual(first, second_action)
        self.assertEqual(resumed, resumed_replay)
        self.assertEqual(2, self.commit_count())
        self.assertEqual(
            1,
            self.fixture.central.connection.execute(
                "SELECT COUNT(*) FROM web_candidate_receipts WHERE family_id = ?",
                (preview_fixtures.FAMILY_ID,),
            ).fetchone()[0],
        )

    def test_action_replay_with_conflicting_bytes_is_rejected(self) -> None:
        self.service.checkpoint = raise_at("after_confirmation")
        with self.assertRaises(InjectedCrash):
            self.confirm("web_action_publish-conflict")

        with self.assertRaises(WebActionConflict) as raised:
            self.service.confirm(
                self.fixture.user, "pub_" + "f" * 32,
                "web_action_publish-conflict", preview_fixtures.NOW,
            )
        self.assertEqual("web_action_conflict", str(raised.exception))

    def test_family_owned_by_concurrent_winner_is_not_reclaimed(self) -> None:
        original = self.fixture.store.claim_publication_families

        def lose_race(publication, family_ids):
            original(publication, family_ids)
            raise WebRecordConflict("publication_family_conflict")

        with patch.object(
            self.fixture.store, "claim_publication_families", side_effect=lose_race
        ):
            with self.assertRaises(CandidateAlreadyPublishing):
                self.confirm()

        self.assertIsNone(self.stored())


if __name__ == "__main__":
    unittest.main()
