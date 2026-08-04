from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zdecision.capture.models import CandidateContent, SourceCheckpoint
from zdecision.capture.reviews import ApprovalRef
from zdecision.ids import decision_id, product_id
from zdecision.jsonio import atomic_write_json
from zdecision.registry.catalog import RegistryCatalog
from zdecision.registry.git import (
    GitRegistryAdapter,
    PublicationGitAmbiguous,
    ReconciledCommit,
    RegistryGitConflict,
    RegistryOutOfSync,
    RegistryPushFailed,
)
from zdecision.registry.models import DecisionSeed


PRODUCT_NAME = "安恒"
PRODUCT_ID = product_id(PRODUCT_NAME)
PREVIEW_ID = "pub_33333333333333333333333333333333"


def _seed(ordinal: int = 1) -> DecisionSeed:
    candidate_id = f"cand_{ordinal:032x}_01"
    return DecisionSeed(
        candidate_id=candidate_id,
        decision_id=decision_id(candidate_id, PRODUCT_ID),
        product_id=PRODUCT_ID,
        product_name=PRODUCT_NAME,
        content=CandidateContent(
            product=PRODUCT_NAME,
            claim=f"正式产品决策 {ordinal}",
            future_action="新增决策时写入对应产品目录。",
            scope_summary="ZDecision Registry",
            repositories=("https://github.com/1320209572/zdecision.git",),
            paths=("decision-registry/",),
            invalidation_conditions=("新的正式决策替代当前规则",),
        ),
        source=SourceCheckpoint("thread-source", "turn-source"),
        review_approval=ApprovalRef(
            actor="user",
            thread_id="thread-review",
            turn_id="turn-review",
            recorded_at="2026-07-29T00:00:00Z",
        ),
    )


class GitRegistryAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.origin = self.root / "origin.git"
        self.seed_repo = self.root / "seed"
        self.local = self.root / "local"

        self.run_at(self.root, "git", "init", "--bare", "--initial-branch=main", str(self.origin))
        self.run_at(self.root, "git", "init", "--initial-branch=main", str(self.seed_repo))
        self.configure_identity(self.seed_repo)
        registry = self.seed_repo / "decision-registry"
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
        self.git("git", "add", ".", cwd=self.seed_repo)
        self.git("git", "commit", "-m", "initial registry", cwd=self.seed_repo)
        self.git("git", "remote", "add", "origin", str(self.origin), cwd=self.seed_repo)
        self.git("git", "push", "-u", "origin", "main", cwd=self.seed_repo)
        self.run_at(self.root, "git", "clone", str(self.origin), str(self.local))
        self.configure_identity(self.local)
        self.adapter = GitRegistryAdapter(self.local, expected_origin=str(self.origin))

    def run_at(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        return self.git(*args, cwd=cwd)

    def git(
        self,
        *args: str,
        cwd: Path,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            args,
            cwd=cwd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def configure_identity(self, repository: Path) -> None:
        self.git("git", "config", "user.name", "ZDecision Test", cwd=repository)
        self.git("git", "config", "user.email", "test@example.invalid", cwd=repository)

    def head(self, repository: Path | None = None) -> str:
        result = self.git("git", "rev-parse", "HEAD", cwd=repository or self.local)
        return result.stdout.decode("ascii").strip()

    def commit_local_file(self, name: str, text: str) -> str:
        (self.local / name).write_text(text, "utf-8")
        self.git("git", "add", name, cwd=self.local)
        self.git("git", "commit", "-m", f"local {name}", cwd=self.local)
        return self.head()

    def push_remote_descendant(self, name: str = "remote.txt") -> str:
        clone = self.root / f"remote-{name.replace('.', '-')}"
        self.run_at(self.root, "git", "clone", str(self.origin), str(clone))
        self.configure_identity(clone)
        (clone / name).write_text("remote change\n", "utf-8")
        self.git("git", "add", name, cwd=clone)
        self.git("git", "commit", "-m", f"remote {name}", cwd=clone)
        self.git("git", "push", "origin", "main", cwd=clone)
        return self.head(clone)

    def draft(self):
        catalog = RegistryCatalog(self.local)
        plan = catalog.inspect((_seed(),))
        draft = catalog.render(plan, PREVIEW_ID)
        return catalog, plan, draft

    def publication_message(self, count: int = 1) -> str:
        return (
            f"decision({PRODUCT_ID}): publish {count} decisions\n\n"
            f"ZDecision-Preview: {PREVIEW_ID}\n"
        )

    def test_fresh_fetch_requires_main_and_exact_local_remote_commit(self) -> None:
        base = self.head()

        self.assertEqual(base, self.adapter.fetch_and_require_exact_main())
        self.assertEqual(
            base,
            self.adapter.fetch_and_require_exact_main(expected_base=base),
        )

        self.git("git", "checkout", "--detach", "HEAD", cwd=self.local)
        with self.assertRaises(RegistryOutOfSync):
            self.adapter.fetch_and_require_exact_main()

    def test_wrong_origin_is_sanitized_before_any_fetch_or_push(self) -> None:
        secret_origin = "https://user:top-secret@example.invalid/repo.git"
        self.git("git", "remote", "set-url", "origin", secret_origin, cwd=self.local)

        with self.assertRaises(RegistryOutOfSync) as raised:
            self.adapter.fetch_and_require_exact_main()

        self.assertNotIn("top-secret", str(raised.exception))
        self.assertNotIn("user", str(raised.exception))

    def test_ahead_behind_and_diverged_main_are_never_synchronized_automatically(
        self,
    ) -> None:
        base = self.head()
        local_commit = self.commit_local_file("local.txt", "local\n")
        remote_commit = self.push_remote_descendant()

        with self.assertRaises(RegistryOutOfSync):
            self.adapter.fetch_and_require_exact_main()

        self.assertEqual(local_commit, self.head())
        self.assertEqual(base, self.git("git", "rev-parse", f"{local_commit}^", cwd=self.local).stdout.decode().strip())
        self.assertNotEqual(local_commit, remote_commit)

    def test_behind_main_stays_behind_after_rejection(self) -> None:
        local_before = self.head()
        self.push_remote_descendant()

        with self.assertRaises(RegistryOutOfSync):
            self.adapter.fetch_and_require_exact_main()

        self.assertEqual(local_before, self.head())

    def test_registry_clean_check_allows_only_exact_target_leftovers(self) -> None:
        self.adapter.fetch_and_require_exact_main()
        catalog, _, draft = self.draft()
        (self.local / "source.txt").write_text("unrelated\n", "utf-8")
        self.git("git", "add", "source.txt", cwd=self.local)
        (self.local / "untracked.txt").write_text("untracked\n", "utf-8")

        target = next(path for path in draft.changed_files if path.endswith("r0001.json"))
        target_path = self.local / target
        target_path.parent.mkdir(parents=True)
        target_path.write_bytes(draft.changed_files[target])
        self.adapter.require_clean_registry(draft.changed_files)

        target_path.write_bytes(b"different\n")
        with self.assertRaises(RegistryGitConflict):
            self.adapter.require_clean_registry(draft.changed_files)
        target_path.write_bytes(draft.changed_files[target])
        (self.local / "decision-registry" / "README.md").write_text("dirty\n", "utf-8")
        with self.assertRaises(RegistryGitConflict):
            self.adapter.require_clean_registry(draft.changed_files)

        self.assertFalse((self.local / "decision-registry" / "products" / PRODUCT_ID / "product.json").exists())
        self.assertIsInstance(catalog, RegistryCatalog)

    def test_commit_exact_uses_one_parent_and_preserves_unrelated_index(self) -> None:
        base = self.adapter.fetch_and_require_exact_main()
        catalog, _, draft = self.draft()
        catalog.write_exact(draft.changed_files)
        (self.local / "source.txt").write_text("staged user change\n", "utf-8")
        self.git("git", "add", "source.txt", cwd=self.local)
        (self.local / "untracked.txt").write_text("user file\n", "utf-8")

        commit_sha = self.adapter.commit_exact(
            base,
            self.publication_message(),
            draft.changed_files,
        )

        parents = self.git("git", "rev-list", "--parents", "-n", "1", commit_sha, cwd=self.local).stdout.decode().split()
        changed = self.git("git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha, cwd=self.local).stdout.decode().splitlines()
        raw_commit = self.git("git", "cat-file", "commit", commit_sha, cwd=self.local).stdout
        message = raw_commit.split(b"\n\n", 1)[1].decode("utf-8")
        staged = self.git("git", "diff", "--cached", "--name-only", cwd=self.local).stdout.decode().splitlines()

        self.assertEqual([commit_sha, base], parents)
        self.assertEqual(sorted(draft.changed_files), sorted(changed))
        self.assertEqual(self.publication_message(), message)
        self.assertEqual(["source.txt"], staged)
        self.assertTrue((self.local / "untracked.txt").exists())

    def test_reconcile_adopts_only_exact_child_and_tracks_remote_presence(self) -> None:
        base = self.adapter.fetch_and_require_exact_main()
        catalog, _, draft = self.draft()
        catalog.write_exact(draft.changed_files)
        commit_sha = self.adapter.commit_exact(
            base,
            self.publication_message(),
            draft.changed_files,
        )

        local = self.adapter.reconcile_exact_commit(
            base,
            self.publication_message(),
            draft.changed_files,
        )
        self.assertEqual(ReconciledCommit(commit_sha, False), local)

        self.adapter.push_exact(commit_sha, base)
        remote = self.adapter.reconcile_exact_commit(
            base,
            self.publication_message(),
            draft.changed_files,
        )
        self.assertEqual(ReconciledCommit(commit_sha, True), remote)
        self.adapter.push_exact(commit_sha, base)

    def test_exact_commit_ignores_inherited_alternate_index(self) -> None:
        base = self.adapter.fetch_and_require_exact_main()
        catalog, _, draft = self.draft()
        catalog.write_exact(draft.changed_files)
        alternate_index = self.root / "attacker.index"

        with patch.dict(
            "os.environ", {"GIT_INDEX_FILE": str(alternate_index)}, clear=False
        ):
            commit_sha = self.adapter.commit_exact(
                base, self.publication_message(), draft.changed_files
            )

        self.assertEqual(commit_sha, self.head())
        self.assertFalse(alternate_index.exists())

    def test_reconcile_ignores_replace_refs_when_proving_exact_commit(self) -> None:
        base = self.adapter.fetch_and_require_exact_main()
        catalog, _, draft = self.draft()
        catalog.write_exact(draft.changed_files)
        commit_sha = self.adapter.commit_exact(
            base, self.publication_message(), draft.changed_files
        )
        wrong = self.git(
            "git", "commit-tree", f"{base}^{{tree}}", "-p", base,
            cwd=self.local, input_bytes=b"wrong\n",
        ).stdout.decode("ascii").strip()
        self.git("git", "replace", commit_sha, wrong, cwd=self.local)

        result = self.adapter.reconcile_exact_commit(
            base, self.publication_message(), draft.changed_files
        )

        self.assertEqual(ReconciledCommit(commit_sha, False), result)

    def test_reconcile_rejects_wrong_message_without_replacing_commit(self) -> None:
        base = self.adapter.fetch_and_require_exact_main()
        catalog, _, draft = self.draft()
        catalog.write_exact(draft.changed_files)
        self.git("git", "add", "decision-registry", cwd=self.local)
        self.git("git", "commit", "-m", "wrong message", cwd=self.local)
        wrong = self.head()

        with self.assertRaises(PublicationGitAmbiguous):
            self.adapter.reconcile_exact_commit(
                base,
                self.publication_message(),
                draft.changed_files,
            )

        self.assertEqual(wrong, self.head())

    def test_reconcile_rejects_extra_path_and_non_child(self) -> None:
        base = self.adapter.fetch_and_require_exact_main()
        catalog, _, draft = self.draft()
        catalog.write_exact(draft.changed_files)
        (self.local / "decision-registry" / "README.md").write_text("extra\n", "utf-8")
        self.git("git", "add", "decision-registry", cwd=self.local)
        self.git(
            "git",
            "commit",
            "--cleanup=verbatim",
            "--file=-",
            cwd=self.local,
            input_bytes=self.publication_message().encode("utf-8"),
        )

        with self.assertRaises(PublicationGitAmbiguous):
            self.adapter.reconcile_exact_commit(
                base,
                self.publication_message(),
                draft.changed_files,
            )

    def test_reconcile_rejects_wrong_blob_with_exact_paths_and_message(self) -> None:
        base = self.adapter.fetch_and_require_exact_main()
        catalog, _, draft = self.draft()
        catalog.write_exact(draft.changed_files)
        decision_path = next(path for path in draft.changed_files if path.endswith("r0001.json"))
        (self.local / decision_path).write_bytes(b"wrong blob\n")
        self.git("git", "add", "--", *sorted(draft.changed_files), cwd=self.local)
        self.git(
            "git",
            "commit",
            "--only",
            "--cleanup=verbatim",
            "--file=-",
            "--",
            *sorted(draft.changed_files),
            cwd=self.local,
            input_bytes=self.publication_message().encode("utf-8"),
        )

        with self.assertRaises(PublicationGitAmbiguous):
            self.adapter.reconcile_exact_commit(
                base,
                self.publication_message(),
                draft.changed_files,
            )

    def test_reconcile_rejects_a_commit_missing_one_previewed_path(self) -> None:
        base = self.adapter.fetch_and_require_exact_main()
        catalog, _, draft = self.draft()
        catalog.write_exact(draft.changed_files)
        paths = sorted(draft.changed_files)
        committed_paths = paths[:-1]
        self.git("git", "add", "--", *committed_paths, cwd=self.local)
        self.git(
            "git",
            "commit",
            "--only",
            "--cleanup=verbatim",
            "--file=-",
            "--",
            *committed_paths,
            cwd=self.local,
            input_bytes=self.publication_message().encode("utf-8"),
        )

        with self.assertRaises(PublicationGitAmbiguous):
            self.adapter.reconcile_exact_commit(
                base,
                self.publication_message(),
                draft.changed_files,
            )

    def test_reconcile_rejects_a_merge_commit_even_with_expected_message(self) -> None:
        base = self.adapter.fetch_and_require_exact_main()
        self.git("git", "switch", "-c", "publication-side", cwd=self.local)
        catalog, _, draft = self.draft()
        catalog.write_exact(draft.changed_files)
        self.git("git", "add", "decision-registry", cwd=self.local)
        self.git("git", "commit", "-m", "side publication", cwd=self.local)
        self.git("git", "switch", "main", cwd=self.local)
        self.commit_local_file("main.txt", "main change\n")
        self.git(
            "git",
            "merge",
            "--no-ff",
            "--no-commit",
            "publication-side",
            cwd=self.local,
        )
        self.git(
            "git",
            "commit",
            "--cleanup=verbatim",
            "--file=-",
            cwd=self.local,
            input_bytes=self.publication_message().encode("utf-8"),
        )

        with self.assertRaises(PublicationGitAmbiguous):
            self.adapter.reconcile_exact_commit(
                base,
                self.publication_message(),
                draft.changed_files,
            )

        self.commit_local_file("later.txt", "later\n")
        with self.assertRaises(PublicationGitAmbiguous):
            self.adapter.reconcile_exact_commit(
                base,
                self.publication_message(),
                draft.changed_files,
            )

    def test_remote_descendant_containing_publication_is_completed(self) -> None:
        base = self.adapter.fetch_and_require_exact_main()
        catalog, _, draft = self.draft()
        catalog.write_exact(draft.changed_files)
        commit_sha = self.adapter.commit_exact(
            base,
            self.publication_message(),
            draft.changed_files,
        )
        self.adapter.push_exact(commit_sha, base)
        descendant = self.push_remote_descendant("after-publication.txt")

        result = self.adapter.reconcile_exact_commit(
            base,
            self.publication_message(),
            draft.changed_files,
        )

        self.assertEqual(ReconciledCommit(commit_sha, True), result)
        self.assertNotEqual(commit_sha, descendant)

    def test_non_fast_forward_push_keeps_same_local_commit_and_reports_failure(
        self,
    ) -> None:
        base = self.adapter.fetch_and_require_exact_main()
        catalog, _, draft = self.draft()
        catalog.write_exact(draft.changed_files)
        commit_sha = self.adapter.commit_exact(
            base,
            self.publication_message(),
            draft.changed_files,
        )
        self.push_remote_descendant("competing.txt")

        with self.assertRaises((PublicationGitAmbiguous, RegistryPushFailed)):
            self.adapter.push_exact(commit_sha, base)

        self.assertEqual(commit_sha, self.head())
        self.assertEqual(
            1,
            int(
                self.git(
                    "git",
                    "rev-list",
                    "--count",
                    f"{base}..{commit_sha}",
                    cwd=self.local,
                ).stdout
            ),
        )

    def test_successful_push_with_unknown_verification_remains_retryable(self) -> None:
        base = self.adapter.fetch_and_require_exact_main()
        catalog, _, draft = self.draft()
        catalog.write_exact(draft.changed_files)
        commit_sha = self.adapter.commit_exact(
            base,
            self.publication_message(),
            draft.changed_files,
        )

        class VerificationUnavailableAdapter(GitRegistryAdapter):
            def __init__(self, repository_root: Path, expected_origin: str) -> None:
                super().__init__(repository_root, expected_origin)
                self.fetch_calls = 0

            def _fetch_main(self) -> None:
                self.fetch_calls += 1
                if self.fetch_calls == 2:
                    raise RegistryOutOfSync("verification unavailable")
                super()._fetch_main()

        uncertain = VerificationUnavailableAdapter(
            self.local,
            expected_origin=str(self.origin),
        )
        with self.assertRaises(RegistryPushFailed):
            uncertain.push_exact(commit_sha, base)

        remote_head = self.git(
            "git",
            "ls-remote",
            str(self.origin),
            "refs/heads/main",
            cwd=self.local,
        ).stdout.decode("ascii").split()[0]
        self.assertEqual(commit_sha, remote_head)


if __name__ == "__main__":
    unittest.main()
