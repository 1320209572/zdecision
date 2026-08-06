# Registry Read Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace request-time Git Registry reads with a durable, commit-proven SQLite projection so Central Dashboard and formal Decision navigation stay below 200 milliseconds without weakening publication safety.

**Architecture:** Git `origin/main` remains the only formal Decision authority. A server-owned synchronizer verifies one exact repository commit, resolves its immutable `decision-registry` tree OID, parses the existing canonical V1 Registry models, and atomically installs derived product and Decision rows in Central SQLite; Web queries read only the active projection. Startup performs one fresh verification, while completed publication and recovery reuse their already-proven commit and refresh the projection without another fetch.

**Tech Stack:** Python 3.11, standard-library `sqlite3` and `subprocess`, FastAPI, existing ZDecision Registry V1 models, React 19, TypeScript 7, Vitest 4, Vite 8.

## Global Constraints

- Git `origin/main` remains authoritative; SQLite is deletable, rebuildable derived data and never authorizes publication.
- The `decision-registry` Git tree OID identifies Registry content; the repository commit records the exact proof and may advance on a code-only commit without rebuilding rows.
- Dashboard, formal Decision list/detail, and repository-space reads perform no Git fetch, Git subprocess, or Registry filesystem read.
- Projection installation exposes either one complete active tree or none; parsing, digest, ownership, or transaction failure must never serve partial or silently stale data.
- Internal `syncing` maps to public `unavailable`; an unavailable Registry is never represented as an empty Registry.
- Preview creation, confirmation, recovery, commit reconciliation, push, and remote containment retain their current fresh-fetch and exact-main rules.
- A completed Publication is never rolled back because projection installation failed; Publication history remains available while formal Registry APIs fail closed.
- Startup performs exactly one fresh `origin/main` fetch, requires `HEAD == refs/heads/main == refs/remotes/origin/main`, and then reuses or rebuilds the durable projection.
- Keep the single-Central-writer V1 contract and the existing one-branch `main` plus `decision-registry/` layout.
- Add no service, Redis, queue, webhook, background polling, PostgreSQL migration, FTS engine, generic React query cache, or unrelated SQL optimization.
- Warm Dashboard and formal Decision requests must complete below 200 milliseconds on the current machine; a concurrent Dashboard request must not delay Candidate APIs beyond 200 milliseconds.
- Preserve the published receipt commit `02ae3c37e388b74004d771d18468bba06f90a1f6` during real acceptance.
- Real acceptance requires local `main` to equal fetched `origin/main`; pushing implementation commits remains a separate user-authorized action and is not implied by this plan.

---

### Task 1: Migration-managed projection schema and atomic SQLite store

**Files:**
- Modify: `src/zdecision/central/web/schema.py:19-194`
- Create: `src/zdecision/central/registry_projection.py`
- Create: `tests/test_registry_projection.py`
- Test: `tests/test_central_web_store.py:219-243`

**Interfaces:**
- Consumes: existing `RegistrySnapshot`, `ProductMetadata`, `ProductRegistry`, `DecisionHead`, `DecisionRevision`, `canonical_json_bytes()`, and `immediate(connection)`.
- Produces: `RegistryProjectionConflict`, `RegistryProjectionState`, `ActiveRegistryProjection`, and `RegistryProjectionStore` with exact methods `get_state()`, `mark_syncing()`, `mark_unavailable()`, `matches()`, `install()`, `update_provenance()`, and `load_active()`.

- [ ] **Step 1: Write failing schema and round-trip tests**

Add this compact fixture and the first store contract to `tests/test_registry_projection.py`:

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from zdecision.central.registry_projection import RegistryProjectionStore
from zdecision.central.store import CentralStore
from zdecision.ids import product_id
from zdecision.registry.models import ProductMetadata, ProductRegistry
from zdecision.registry.query import RegistrySnapshot


PRODUCT_NAME = "ZDecision"
PRODUCT_ID = product_id(PRODUCT_NAME)
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
TREE_A = "1" * 40
TREE_B = "2" * 40
VERIFIED_AT = "2026-08-06T10:00:00Z"


def _snapshot(commit_sha: str = COMMIT_A) -> RegistrySnapshot:
    return RegistrySnapshot(
        commit_sha=commit_sha,
        products={PRODUCT_ID: ProductMetadata(PRODUCT_ID, PRODUCT_NAME)},
        registries={PRODUCT_ID: ProductRegistry(PRODUCT_ID, {})},
        decisions={},
    )


class RegistryProjectionStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.central = CentralStore.open(
            Path(self.temporary_directory.name) / "central.sqlite3"
        )
        self.addCleanup(self.central.close)
        self.projection = RegistryProjectionStore(self.central.connection)

    def test_schema_and_install_round_trip_are_commit_and_tree_bound(self) -> None:
        tables = {
            row["name"]
            for row in self.central.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {
                "registry_projection_state",
                "registry_product_projection",
                "registry_decision_projection",
            }.issubset(tables)
        )

        self.projection.mark_syncing(
            "org_demo", COMMIT_A, TREE_A, VERIFIED_AT, VERIFIED_AT
        )
        state = self.projection.install(
            "org_demo", TREE_A, _snapshot(), VERIFIED_AT, VERIFIED_AT
        )
        active = self.projection.load_active("org_demo")

        self.assertEqual("available", state.state)
        self.assertEqual(COMMIT_A, active.commit_sha)
        self.assertEqual(TREE_A, active.tree_oid)
        self.assertEqual(VERIFIED_AT, active.verified_at)
        self.assertEqual(_snapshot(), active.snapshot)
```

Add assertions for the three primary keys, the Decision-to-Product foreign key, the state check constraint, and indexes named `registry_product_projection_name`, `registry_decision_projection_lifecycle`, and `registry_decision_projection_identity`. Add a second test that directly corrupts `product_json` or `product_digest` and asserts `load_active("org_demo") is None`; derived corruption must fail closed.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_registry_projection.RegistryProjectionStoreTest.test_schema_and_install_round_trip_are_commit_and_tree_bound -v
```

Expected: FAIL because `zdecision.central.registry_projection` and the three projection tables do not exist.

- [ ] **Step 3: Add the three projection tables to the existing Web schema**

Append these migration-managed definitions inside `WEB_SCHEMA` in `src/zdecision/central/web/schema.py`; do not create a separate migration runner:

```sql
CREATE TABLE IF NOT EXISTS registry_projection_state (
  organization_id TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK(state IN ('available','syncing','unavailable')),
  active_commit TEXT,
  active_tree_oid TEXT,
  desired_commit TEXT,
  desired_tree_oid TEXT,
  verified_at TEXT,
  updated_at TEXT NOT NULL,
  product_count INTEGER CHECK(product_count IS NULL OR product_count >= 0),
  decision_count INTEGER CHECK(decision_count IS NULL OR decision_count >= 0),
  projection_digest TEXT,
  error_code TEXT CHECK(
    error_code IS NULL OR error_code IN (
      'git_proof_failed','registry_invalid','projection_install_failed'
    )
  ),
  CHECK(
    state != 'available' OR (
      active_commit IS NOT NULL AND active_tree_oid IS NOT NULL
      AND verified_at IS NOT NULL AND product_count IS NOT NULL
      AND decision_count IS NOT NULL AND projection_digest IS NOT NULL
      AND error_code IS NULL
    )
  )
);

CREATE TABLE IF NOT EXISTS registry_product_projection (
  organization_id TEXT NOT NULL,
  registry_tree_oid TEXT NOT NULL,
  product_id TEXT NOT NULL,
  product_name TEXT NOT NULL,
  product_path TEXT NOT NULL,
  registry_path TEXT NOT NULL,
  product_json TEXT NOT NULL,
  product_digest TEXT NOT NULL,
  PRIMARY KEY(organization_id, registry_tree_oid, product_id)
);

CREATE TABLE IF NOT EXISTS registry_decision_projection (
  organization_id TEXT NOT NULL,
  registry_tree_oid TEXT NOT NULL,
  product_id TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision > 0),
  lifecycle TEXT NOT NULL CHECK(lifecycle = 'active'),
  head_path TEXT NOT NULL,
  claim TEXT NOT NULL,
  future_action TEXT NOT NULL,
  scope_summary TEXT NOT NULL,
  repositories_json TEXT NOT NULL,
  paths_json TEXT NOT NULL,
  invalidation_conditions_json TEXT NOT NULL,
  publication_preview_id TEXT NOT NULL,
  decision_json TEXT NOT NULL,
  decision_digest TEXT NOT NULL,
  PRIMARY KEY(
    organization_id, registry_tree_oid, product_id, decision_id, revision
  ),
  FOREIGN KEY(organization_id, registry_tree_oid, product_id)
    REFERENCES registry_product_projection(
      organization_id, registry_tree_oid, product_id
    )
);

CREATE INDEX IF NOT EXISTS registry_product_projection_name
ON registry_product_projection(
  organization_id, registry_tree_oid, product_name, product_id
);

CREATE INDEX IF NOT EXISTS registry_decision_projection_lifecycle
ON registry_decision_projection(
  organization_id, registry_tree_oid, lifecycle, product_id
);

CREATE INDEX IF NOT EXISTS registry_decision_projection_identity
ON registry_decision_projection(
  organization_id, registry_tree_oid, product_id, decision_id, revision
);
```

The current `CentralStore.open()` call to `initialize_web_schema()` must remain the only schema entry point.

- [ ] **Step 4: Implement the projection value types and canonical row builder**

Create `src/zdecision/central/registry_projection.py` with these public values and method signatures:

```python
ProjectionState = Literal["available", "syncing", "unavailable"]


class RegistryProjectionConflict(Exception):
    code = "projection_state_conflict"


@dataclass(frozen=True)
class RegistryProjectionState:
    organization_id: str
    state: ProjectionState
    active_commit: str | None
    active_tree_oid: str | None
    desired_commit: str | None
    desired_tree_oid: str | None
    verified_at: str | None
    updated_at: str
    product_count: int | None
    decision_count: int | None
    projection_digest: str | None
    error_code: str | None


@dataclass(frozen=True)
class ActiveRegistryProjection:
    commit_sha: str
    tree_oid: str
    verified_at: str
    snapshot: RegistrySnapshot


class RegistryProjectionStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        self.connection = connection
```

Add these exact public methods to the class:

- `get_state(organization_id: str) -> RegistryProjectionState | None`
- `mark_syncing(organization_id: str, desired_commit: str, desired_tree_oid: str, verified_at: str, updated_at: str) -> RegistryProjectionState`
- `mark_unavailable(organization_id: str, desired_commit: str | None, desired_tree_oid: str | None, verified_at: str | None, updated_at: str, error_code: str) -> RegistryProjectionState`
- `matches(organization_id: str, tree_oid: str, snapshot: RegistrySnapshot) -> bool`
- `install(organization_id: str, tree_oid: str, snapshot: RegistrySnapshot, verified_at: str, updated_at: str) -> RegistryProjectionState`
- `update_provenance(organization_id: str, commit_sha: str, tree_oid: str, verified_at: str, updated_at: str) -> RegistryProjectionState`
- `load_active(organization_id: str) -> ActiveRegistryProjection | None`

Their complete behavior is:

- validate organization IDs, 40-character lowercase commit/tree hashes, and RFC 3339 UTC timestamps before SQL;
- add `_timestamp(value: str | datetime) -> str` using the same timezone-aware datetime/RFC 3339 normalization as `CentralPublicationService._timestamp()`; the synchronizer and every store mutation use this helper;
- derive canonical JSON and SHA-256 for every `ProductMetadata` and `DecisionRevision` using `canonical_json_bytes()`;
- derive product and registry paths as `products/{product_id}/product.json` and `products/{product_id}/registry.json`;
- derive each head path as `decisions/{decision_id}/r{revision:04d}.json`;
- use canonical JSON arrays for repositories, paths, and invalidation conditions;
- compute `projection_digest` as SHA-256 of canonical JSON containing ordered `[product_id, product_digest]` entries and ordered `[product_id, decision_id, revision, decision_digest]` entries;
- compare exact product/Decision key-and-digest sets in `matches()`, so missing or additional rows force a rebuild;
- reconstruct `ProductRegistry` and `DecisionHead` values in `load_active()`, then re-check every denormalized column against the canonical `DecisionRevision`;
- compare loaded row counts and the recomputed manifest digest with state, so an available zero-row projection is valid only when both expected counts are zero;
- return `None` for any unavailable/syncing state, count/digest mismatch, canonical mismatch, or ownership mismatch.

- [ ] **Step 5: Implement short state transactions and one atomic installation transaction**

Use `immediate(self.connection)` for every state mutation. `mark_syncing()` must preserve the old active commit/tree while setting the desired proof; `mark_unavailable()` must preserve those old rows but make them unservable. `install()` must use this exact transaction order:

```python
with immediate(self.connection):
    state = self.get_state(organization_id)
    if (
        state is None
        or state.state != "syncing"
        or state.desired_commit != snapshot.commit_sha
        or state.desired_tree_oid != tree_oid
    ):
        raise RegistryProjectionConflict("projection_state_conflict")
    self.connection.execute(
        """DELETE FROM registry_decision_projection
           WHERE organization_id = ? AND registry_tree_oid = ?""",
        (organization_id, tree_oid),
    )
    self.connection.execute(
        """DELETE FROM registry_product_projection
           WHERE organization_id = ? AND registry_tree_oid = ?""",
        (organization_id, tree_oid),
    )
    self.connection.executemany(PRODUCT_INSERT, product_rows)
    self.connection.executemany(DECISION_INSERT, decision_rows)
    self.connection.execute(
        """UPDATE registry_projection_state
           SET state = 'available', active_commit = ?, active_tree_oid = ?,
               desired_commit = NULL, desired_tree_oid = NULL,
               verified_at = ?, updated_at = ?, product_count = ?,
               decision_count = ?, projection_digest = ?, error_code = NULL
           WHERE organization_id = ?""",
        (
            snapshot.commit_sha, tree_oid, verified_at, updated_at,
            len(product_rows), len(decision_rows), projection_digest,
            organization_id,
        ),
    )
    self.connection.execute(
        """DELETE FROM registry_decision_projection
           WHERE organization_id = ? AND registry_tree_oid != ?""",
        (organization_id, tree_oid),
    )
    self.connection.execute(
        """DELETE FROM registry_product_projection
           WHERE organization_id = ? AND registry_tree_oid != ?""",
        (organization_id, tree_oid),
    )
```

Use plain `INSERT` after deleting only the desired tree inside the same transaction; do not use SQLite `INSERT OR REPLACE`, whose delete-and-insert behavior can violate the Decision-to-Product foreign key. `update_provenance()` runs after `mark_syncing()`: it must require `state='syncing'`, exact desired commit/tree, and `active_tree_oid == desired_tree_oid`; then it atomically restores `available`, advances `active_commit`/`verified_at`, clears desired/error fields, and leaves product/Decision rows byte-for-byte untouched.

- [ ] **Step 6: Add rollback, same-tree, idempotency, and corruption tests**

Add tests with these exact assertions:

```python
def test_same_tree_updates_only_commit_provenance(self) -> None:
    self.projection.mark_syncing(
        "org_demo", COMMIT_A, TREE_A, VERIFIED_AT, VERIFIED_AT
    )
    self.projection.install(
        "org_demo", TREE_A, _snapshot(), VERIFIED_AT, VERIFIED_AT
    )
    before = self.central.connection.execute(
        "SELECT rowid, * FROM registry_product_projection"
    ).fetchall()

    self.projection.mark_syncing(
        "org_demo", COMMIT_B, TREE_A,
        "2026-08-06T11:00:00Z", "2026-08-06T11:00:00Z",
    )
    state = self.projection.update_provenance(
        "org_demo", COMMIT_B, TREE_A,
        "2026-08-06T11:00:00Z", "2026-08-06T11:00:00Z",
    )
    after = self.central.connection.execute(
        "SELECT rowid, * FROM registry_product_projection"
    ).fetchall()

    self.assertEqual(COMMIT_B, state.active_commit)
    self.assertEqual(before, after)


def test_failed_install_never_switches_or_partially_replaces_active_tree(self) -> None:
    self.projection.mark_syncing(
        "org_demo", COMMIT_A, TREE_A, VERIFIED_AT, VERIFIED_AT
    )
    self.projection.install(
        "org_demo", TREE_A, _snapshot(), VERIFIED_AT, VERIFIED_AT
    )
    self.projection.mark_syncing(
        "org_demo", COMMIT_B, TREE_B, VERIFIED_AT, VERIFIED_AT
    )
    self.central.connection.execute(
        f"""CREATE TRIGGER reject_tree_b BEFORE INSERT
            ON registry_product_projection
            WHEN NEW.registry_tree_oid = '{TREE_B}'
            BEGIN SELECT RAISE(ABORT, 'fixture rejection'); END"""
    )

    with self.assertRaises(sqlite3.IntegrityError):
        self.projection.install(
            "org_demo", TREE_B, _snapshot(COMMIT_B),
            VERIFIED_AT, VERIFIED_AT,
        )

    rows = self.central.connection.execute(
        "SELECT DISTINCT registry_tree_oid FROM registry_product_projection"
    ).fetchall()
    self.assertEqual([TREE_A], [row[0] for row in rows])
    self.assertIsNone(self.projection.load_active("org_demo"))
```

Also prove exact replay does not create duplicate rows, missing rows make `matches()` false, and malformed canonical JSON/digests make `load_active()` fail closed.
Install `RegistrySnapshot(COMMIT_A, {}, {}, {})` in a separate test and assert
`load_active()` returns an available empty snapshot with zero counts; this
prevents the integrity check from confusing a valid empty Registry with row
loss.

- [ ] **Step 7: Run the store tests and commit**

Run:

```bash
.venv/bin/python -m unittest tests.test_registry_projection tests.test_central_web_store -v
```

Expected: PASS.

Commit:

```bash
git add src/zdecision/central/web/schema.py src/zdecision/central/registry_projection.py tests/test_registry_projection.py tests/test_central_web_store.py
git commit -m "feat: add durable registry projection store"
```

---

### Task 2: Commit-bound Registry parser and verified projection synchronizer

**Files:**
- Modify: `src/zdecision/registry/git.py:44-232`
- Modify: `src/zdecision/registry/query.py:66-129`
- Modify: `src/zdecision/central/registry_projection.py`
- Modify: `tests/test_git_registry.py:56-210`
- Modify: `tests/test_central_web_queries.py:135-289`
- Modify: `tests/test_registry_projection.py`

**Interfaces:**
- Consumes: Task 1 `RegistryProjectionStore` and current Git/Registry V1 validation.
- Produces: `GitRegistryAdapter.require_exact_main(expected_commit)`, `GitRegistryAdapter.registry_tree_oid(commit_sha)`, `RegistryQuery.snapshot_at_commit(commit_sha)`, `RegistryProjectionSynchronizer.synchronize(organization_id, verified_commit, verified_at)`, and `RegistryProjectionError`.

- [ ] **Step 1: Write failing Git and commit-bound parser tests**

Extend the existing real-Git fixtures with these assertions:

```python
def test_require_exact_main_and_tree_oid_do_not_fetch(self) -> None:
    commit = self.adapter.fetch_and_require_exact_main()
    with mock.patch.object(
        self.adapter, "_fetch_main", side_effect=AssertionError("unexpected fetch")
    ):
        self.assertEqual(commit, self.adapter.require_exact_main(commit))
        tree_oid = self.adapter.registry_tree_oid(commit)
    self.assertRegex(tree_oid, r"^[0-9a-f]{40}$")
    self.assertEqual(
        self.git(
            "git", "rev-parse", f"{commit}:decision-registry",
            cwd=self.local,
        ).stdout.decode("ascii").strip(),
        tree_oid,
    )


def test_snapshot_at_commit_parses_without_remote_verification(self) -> None:
    commit = self.query.git.fetch_and_require_exact_main()
    with mock.patch.object(
        self.query.git,
        "fetch_and_require_exact_main",
        side_effect=AssertionError("unexpected fetch"),
    ):
        snapshot = self.query.snapshot_at_commit(commit)
    self.assertEqual(commit, snapshot.commit_sha)
    self.assertEqual("committed formal decision", next(iter(snapshot.decisions.values())).claim)
```

Import `unittest.mock` as `mock`. Keep existing `snapshot()` tests unchanged so the legacy strict entry point continues to prove fresh verification before and after a read.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_git_registry.GitRegistryAdapterTests.test_require_exact_main_and_tree_oid_do_not_fetch \
  tests.test_central_web_queries.RegistryQueryTest.test_snapshot_at_commit_parses_without_remote_verification -v
```

Expected: FAIL because the three public methods do not exist.

- [ ] **Step 3: Split fresh verification from immutable commit parsing**

Refactor `GitRegistryAdapter` so the existing method delegates to one exact-ref helper:

```python
def fetch_and_require_exact_main(self, expected_base: str | None = None) -> str:
    return self._require_exact_main(expected_base=expected_base, fetch=True)

def require_exact_main(self, expected_commit: str) -> str:
    self._validated_commit(expected_commit, "Expected commit")
    return self._require_exact_main(expected_base=expected_commit, fetch=False)

def _require_exact_main(
    self, *, expected_base: str | None, fetch: bool,
) -> str:
    self._require_origin_and_main(fetch=fetch)
    head = self._revision("HEAD", RegistryOutOfSync)
    local_main = self._revision("refs/heads/main", RegistryOutOfSync)
    remote_main = self._revision("refs/remotes/origin/main", RegistryOutOfSync)
    if head != local_main or head != remote_main:
        raise RegistryOutOfSync("Local main is not exactly synchronized")
    if expected_base is not None and head != expected_base:
        raise RegistryOutOfSync("Local main no longer matches the expected commit")
    return head
```

Implement `registry_tree_oid()` through `_run()` using `git rev-parse --verify <commit>:decision-registry`, then verify `git cat-file -t <oid>` returns exactly `tree`; reject any non-40-character lowercase result with `RegistryOutOfSync`.

Move the parse loop currently inside `RegistryQuery.snapshot()` into:

```python
def snapshot_at_commit(self, commit_sha: str) -> RegistrySnapshot:
    try:
        if not isinstance(commit_sha, str) or _COMMIT.fullmatch(commit_sha) is None:
            raise ValueError("Registry commit is invalid")
        return self._parse_snapshot(commit_sha)
    except RegistryQueryUnavailable:
        raise
    except (GitRegistryError, OSError, UnicodeError, TypeError, ValueError):
        raise RegistryQueryUnavailable("registry_unavailable") from None

def snapshot(self) -> RegistrySnapshot:
    try:
        commit_sha = self.git.fetch_and_require_exact_main()
        snapshot = self.snapshot_at_commit(commit_sha)
        self.git.fetch_and_require_exact_main(expected_base=commit_sha)
        return snapshot
    except RegistryQueryUnavailable:
        raise
    except (GitRegistryError, OSError, UnicodeError, TypeError, ValueError):
        raise RegistryQueryUnavailable("registry_unavailable") from None
```

Do not expose or call `GitRegistryAdapter._validated_commit` from `RegistryQuery`; keep the local `_COMMIT` check shown above. `snapshot_at_commit()` must perform no network request, while `_read()` must keep `--no-replace-objects`, exact path, blob type, canonical bytes, ownership, head revision, and lifecycle checks.

- [ ] **Step 4: Write failing synchronizer state-machine tests**

Add `from unittest import mock`, then add these test doubles and scenarios to `tests/test_registry_projection.py`:

```python
class _VerifiedGit:
    def __init__(self, tree_oid: str = TREE_A) -> None:
        self.tree_oid = tree_oid
        self.fetch_count = 0

    def require_exact_main(self, expected_commit: str) -> str:
        return expected_commit

    def registry_tree_oid(self, commit_sha: str) -> str:
        return self.tree_oid


class _CommitQuery:
    def __init__(self, snapshot: RegistrySnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[str] = []

    def snapshot_at_commit(self, commit_sha: str) -> RegistrySnapshot:
        self.calls.append(commit_sha)
        return self.snapshot


def test_synchronize_installs_then_reuses_same_tree_without_rewriting_rows(self) -> None:
    git = _VerifiedGit()
    query = _CommitQuery(_snapshot())
    synchronizer = RegistryProjectionSynchronizer(
        git=git, query=query, store=self.projection,
        clock=lambda: "2026-08-06T10:00:01Z",
    )

    first = synchronizer.synchronize("org_demo", COMMIT_A, VERIFIED_AT)
    before = self.central.connection.execute(
        "SELECT rowid, * FROM registry_product_projection"
    ).fetchall()
    query.snapshot = _snapshot(COMMIT_B)
    second = synchronizer.synchronize(
        "org_demo", COMMIT_B, "2026-08-06T11:00:00Z"
    )
    after = self.central.connection.execute(
        "SELECT rowid, * FROM registry_product_projection"
    ).fetchall()

    self.assertEqual("available", first.state)
    self.assertEqual(COMMIT_B, second.active_commit)
    self.assertEqual(before, after)
    self.assertEqual([COMMIT_A, COMMIT_B], query.calls)


def test_parse_failure_marks_projection_unavailable_without_serving_old_rows(self) -> None:
    synchronizer = RegistryProjectionSynchronizer(
        git=_VerifiedGit(), query=_CommitQuery(_snapshot()),
        store=self.projection, clock=lambda: VERIFIED_AT,
    )
    synchronizer.synchronize("org_demo", COMMIT_A, VERIFIED_AT)
    synchronizer.query.snapshot_at_commit = mock.Mock(
        side_effect=RegistryQueryUnavailable("registry_unavailable")
    )

    state = synchronizer.synchronize("org_demo", COMMIT_A, VERIFIED_AT)

    self.assertEqual("unavailable", state.state)
    self.assertEqual("registry_invalid", state.error_code)
    self.assertIsNone(self.projection.load_active("org_demo"))
```

Add an installation-trigger failure test that expects `projection_install_failed`, old-tree rows retained, and no active snapshot served. Add a mismatched exact-main test that expects `git_proof_failed`.

- [ ] **Step 5: Implement `RegistryProjectionSynchronizer`**

Add the following control flow after the Task 1 store in `registry_projection.py`:

```python
class RegistryProjectionError(Exception):
    code = "registry_projection_error"


class RegistryProjectionSynchronizer:
    def __init__(self, *, git, query, store, clock=None) -> None:
        if not callable(getattr(git, "require_exact_main", None)):
            raise TypeError("git must expose require_exact_main()")
        if not callable(getattr(git, "registry_tree_oid", None)):
            raise TypeError("git must expose registry_tree_oid()")
        if not callable(getattr(query, "snapshot_at_commit", None)):
            raise TypeError("query must expose snapshot_at_commit()")
        if not isinstance(store, RegistryProjectionStore):
            raise TypeError("store must be a RegistryProjectionStore")
        self.git = git
        self.query = query
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))

    def synchronize(
        self, organization_id: str, verified_commit: str, verified_at: str,
    ) -> RegistryProjectionState:
        desired_tree_oid: str | None = None
        updated_at = _timestamp(self.clock())
        try:
            self.git.require_exact_main(verified_commit)
            desired_tree_oid = self.git.registry_tree_oid(verified_commit)
            self.store.mark_syncing(
                organization_id, verified_commit, desired_tree_oid,
                verified_at, updated_at,
            )
            snapshot = self.query.snapshot_at_commit(verified_commit)
            if snapshot.commit_sha != verified_commit:
                raise RegistryQueryUnavailable("registry_unavailable")
            if self.store.matches(organization_id, desired_tree_oid, snapshot):
                return self.store.update_provenance(
                    organization_id, verified_commit, desired_tree_oid,
                    verified_at, updated_at,
                )
            return self.store.install(
                organization_id, desired_tree_oid, snapshot,
                verified_at, updated_at,
            )
        except GitRegistryError:
            error_code = "git_proof_failed"
        except RegistryQueryUnavailable:
            error_code = "registry_invalid"
        except (RegistryProjectionConflict, sqlite3.Error, ValueError):
            error_code = "projection_install_failed"
        try:
            return self.store.mark_unavailable(
                organization_id, verified_commit, desired_tree_oid,
                verified_at, updated_at, error_code,
            )
        except (sqlite3.Error, ValueError) as error:
            raise RegistryProjectionError("registry_projection_error") from error
```

`matches()` must run after parsing even when the tree OID is unchanged; this is what detects missing or corrupt durable rows on startup without rebuilding valid code-only commits.

- [ ] **Step 6: Run focused Registry and projection tests and commit**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_git_registry \
  tests.test_central_web_queries.RegistryQueryTest \
  tests.test_registry_projection -v
```

Expected: PASS, including the existing replacement-object and worktree-isolation tests.

Commit:

```bash
git add src/zdecision/registry/git.py src/zdecision/registry/query.py src/zdecision/central/registry_projection.py tests/test_git_registry.py tests/test_central_web_queries.py tests/test_registry_projection.py
git commit -m "feat: synchronize verified registry projections"
```

---

### Task 3: SQLite-only Dashboard and formal Decision query path

**Files:**
- Modify: `src/zdecision/central/web/queries.py:1-1703`
- Modify: `tests/test_central_web_queries.py:86-625`
- Modify: `tests/test_central_web_api.py:66-325`
- Modify: `tests/test_central_web_review.py:56-162`
- Modify: `tests/test_central_web_preview.py:127-136`

**Interfaces:**
- Consumes: Task 1 `RegistryProjectionStore.load_active(organization_id)` and `ActiveRegistryProjection`.
- Produces: `CentralWebQueries(connection, registry_projection)`, `RegistryStatus(state, commit_sha, verified_at)`, and the unchanged public unavailable/503 behavior.

- [ ] **Step 1: Replace the fake live Registry query in tests with a seeded projection**

Add `import sqlite3` and `from unittest import mock`, then add this helper to `tests/test_central_web_queries.py` and use it in `setUp()` plus each test that currently constructs `_RegistryQuery`:

```python
def _projection(
    connection: sqlite3.Connection,
    *, decisions: tuple[DecisionRevision, ...] = (),
    available: bool = True,
) -> RegistryProjectionStore:
    store = RegistryProjectionStore(connection)
    if not available:
        store.mark_unavailable(
            "org_demo", None, None, None,
            "2026-08-06T10:00:00Z", "git_proof_failed",
        )
        return store
    snapshot = _registry_snapshot(decisions)
    store.mark_syncing(
        "org_demo", COMMIT_SHA, "1" * 40,
        "2026-08-06T10:00:00Z", "2026-08-06T10:00:00Z",
    )
    store.install(
        "org_demo", "1" * 40, snapshot,
        "2026-08-06T10:00:00Z", "2026-08-06T10:00:00Z",
    )
    return store
```

Rename the existing fake's pure snapshot construction to `_registry_snapshot(decisions)`; remove its mutable unavailable and call-count behavior. Update the global/product list test to assert identical results without asserting two live snapshot calls.

Add this hot-path regression test:

```python
def test_dashboard_catalog_detail_and_repository_spaces_never_spawn_git(self) -> None:
    revision = _decision()
    projection = _projection(self.store.connection, decisions=(revision,))
    queries = CentralWebQueries(self.store.connection, projection)

    with mock.patch(
        "subprocess.run", side_effect=AssertionError("Git entered Web read path")
    ):
        dashboard = queries.dashboard(self.user)
        catalog = queries.list_decisions(self.user)
        detail = queries.get_decision(
            self.user, PRODUCT_SPACE_ID, revision.decision_id
        )
        spaces = queries.repository_spaces(self.user, PRODUCT_REPOSITORY_ID)

    self.assertEqual(COMMIT_SHA, dashboard.registry.commit_sha)
    self.assertEqual("2026-08-06T10:00:00Z", dashboard.registry.verified_at)
    self.assertEqual(1, catalog.total)
    self.assertEqual(revision.decision_id, detail.decision.decision_id)
    self.assertEqual((PRODUCT_SPACE_ID,), tuple(
        item.decision_space_id for item in spaces.spaces
    ))
```

Keep the existing unavailable-not-empty tests and change only their fixture to `_projection(self.store.connection, available=False)`.

- [ ] **Step 2: Run the query regression test and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_central_web_queries.CentralWebQueriesTest.test_dashboard_catalog_detail_and_repository_spaces_never_spawn_git -v
```

Expected: FAIL because `CentralWebQueries` still expects a provider exposing `snapshot()` and still reaches the live Registry abstraction.

- [ ] **Step 3: Replace the query dependency and expose verification time**

Change the constructor and status model to:

```python
@dataclass(frozen=True)
class RegistryStatus:
    state: Literal["available", "unavailable"]
    commit_sha: str | None
    verified_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "commit_sha": self.commit_sha,
            "verified_at": self.verified_at,
        }


class CentralWebQueries:
    def __init__(
        self, connection: sqlite3.Connection,
        registry_projection: RegistryProjectionStore,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        if not isinstance(registry_projection, RegistryProjectionStore):
            raise TypeError(
                "registry_projection must be a RegistryProjectionStore"
            )
        self.connection = connection
        self.registry_projection = registry_projection

    def _active_registry(
        self, organization_id: str,
    ) -> ActiveRegistryProjection | None:
        return self.registry_projection.load_active(organization_id)
```

At `list_products`, `list_decisions`, `get_decision`, `repository_spaces`, and `dashboard`, call `_active_registry(principal.organization_id)` once and pass `active.snapshot if active is not None else None` into the existing business logic. Use `active.commit_sha` for `DecisionListView.registry_commit` and Decision detail proof. Build Dashboard status as:

```python
registry = RegistryStatus(
    "available" if active is not None else "unavailable",
    active.commit_sha if active is not None else None,
    active.verified_at if active is not None else None,
)
```

Delete the imports of `RegistryQueryUnavailable` and the old `_registry_snapshot()` method. Do not move search, publication receipt joins, Candidate counts, or decision-space ownership into a new abstraction in this task.

- [ ] **Step 4: Update API fixtures and all constructor sites**

Update these exact construction sites to seed and pass one `RegistryProjectionStore`: `tests/test_central_web_api.py`, `tests/test_central_web_preview.py`, `tests/test_central_web_review.py`, and the query tests. The API Dashboard expected payload must include:

```python
"registry": {
    "state": "available",
    "commit_sha": COMMIT_SHA,
    "verified_at": "2026-08-06T10:00:00Z",
},
```

Keep Decision list/detail status codes and response fields unchanged. Keep the existing 503 `registry_unavailable` assertion for formal reads; Candidate and Publication operational endpoints must still return normally when the projection is unavailable.

- [ ] **Step 5: Run focused query/API/review/preview tests and commit**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_central_web_queries \
  tests.test_central_web_api \
  tests.test_central_web_review \
  tests.test_central_web_preview -v
```

Expected: PASS. Preview stale/exact-main tests must still exercise live Git, while Web read tests must not.

Commit:

```bash
git add src/zdecision/central/web/queries.py tests/test_central_web_queries.py tests/test_central_web_api.py tests/test_central_web_review.py tests/test_central_web_preview.py
git commit -m "feat: serve formal decisions from sqlite projection"
```

---

### Task 4: Startup recovery and post-publication projection refresh

**Files:**
- Modify: `src/zdecision/central/cli.py:175-235`
- Modify: `src/zdecision/central/web/application.py:43-230`
- Verify: `tests/test_central_web_publication.py:42-275`
- Modify: `tests/test_central_web_api.py:276-288`
- Create: `tests/test_central_cli.py`
- Modify: `tests/integration/test_central_web_vertical.py:199-228`
- Modify: `tests/integration/test_central_web_vertical.py:587-621`

**Interfaces:**
- Consumes: Task 2 `RegistryProjectionSynchronizer` and Task 3 SQLite-only queries.
- Produces: `_synchronize_registry_on_startup()`, the `registry_synchronizer` keyword on `CentralWebApplication`, and `_synchronize_completed_publication()`.

- [ ] **Step 1: Write failing startup behavior tests**

Create `tests/test_central_cli.py` with narrow doubles and these two cases:

```python
class _StartupGit:
    def __init__(self, commit: str = "a" * 40, error: Exception | None = None):
        self.commit = commit
        self.error = error
        self.fetch_count = 0

    def fetch_and_require_exact_main(self) -> str:
        self.fetch_count += 1
        if self.error is not None:
            raise self.error
        return self.commit


def test_startup_fetches_once_and_synchronizes_the_exact_commit(self) -> None:
    git = _StartupGit()
    synchronizer = mock.Mock()
    projection = mock.Mock()

    _synchronize_registry_on_startup(
        "org_demo", git, synchronizer, projection,
        "2026-08-06T10:00:00Z",
    )

    self.assertEqual(1, git.fetch_count)
    synchronizer.synchronize.assert_called_once_with(
        "org_demo", "a" * 40, "2026-08-06T10:00:00Z"
    )
    projection.mark_unavailable.assert_not_called()


def test_startup_verification_failure_disables_only_formal_reads(self) -> None:
    git = _StartupGit(error=RegistryOutOfSync("offline"))
    synchronizer = mock.Mock()
    projection = mock.Mock()

    _synchronize_registry_on_startup(
        "org_demo", git, synchronizer, projection,
        "2026-08-06T10:00:00Z",
    )

    synchronizer.synchronize.assert_not_called()
    projection.mark_unavailable.assert_called_once_with(
        "org_demo", None, None, None,
        "2026-08-06T10:00:00Z", "git_proof_failed",
    )
```

- [ ] **Step 2: Write failing completed-publication refresh tests**

In the `tests/test_central_web_api.py` application fixture, retain `self.web`, create the real synchronizer around its Git/query/projection dependencies, and wrap its method with `mock.Mock(wraps=synchronizer.synchronize)` before injecting it. Extend the existing `test_publication_routes_require_one_explicit_action_and_return_safe_history` after publish, replay, and resume:

```python
completed_record = CentralWebStore(
    self.store.connection
).get_publication_by_preview(
    "org_demo", preview["preview_id"]
)

self.assertIsNotNone(completed_record)
self.assertEqual("completed", completed_record.state)
self.assertEqual(3, self.synchronizer.synchronize.call_count)
self.assertEqual(
    [
        mock.call(
            "org_demo", completed_record.commit_sha,
            completed_record.updated_at,
        )
    ] * 3,
    self.synchronizer.synchronize.call_args_list,
)
formal = self.client.get(
    f"/api/v1/web/spaces/{PRODUCT_SPACE_ID}/decisions/"
    f"{preview['decisions'][0]['decision_id']}"
)
self.assertEqual(200, formal.status_code, formal.text)
```

The three calls are intentional: initial confirmation, idempotent publish replay, and resume replay all repair a missing projection without repeating Git publication. Import `mock` from `unittest`.

Add a second API test whose synchronizer first calls `projection.mark_unavailable(...)` and then raises `RegistryProjectionError`; assert the publish response still returns `completed`, Publication detail/history still return the commit proof, and formal Decision list/detail return `503 registry_unavailable` rather than exposing the old tree. Keep `tests/test_central_web_publication.py` unchanged as the lower-level before-commit/after-commit/after-push regression suite.

- [ ] **Step 3: Run the startup and publication tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_central_cli \
  tests.test_central_web_api.CentralWebApiTest.test_publication_routes_require_one_explicit_action_and_return_safe_history -v
```

Expected: FAIL because startup has no synchronization helper and `CentralWebApplication` does not refresh a completed commit.

- [ ] **Step 4: Add testable startup synchronization and production wiring**

Add this helper to `central/cli.py`:

```python
def _synchronize_registry_on_startup(
    organization_id: str,
    git: GitRegistryAdapter,
    synchronizer: RegistryProjectionSynchronizer,
    projection: RegistryProjectionStore,
    verified_at: str,
) -> None:
    try:
        verified_commit = git.fetch_and_require_exact_main()
    except GitRegistryError:
        projection.mark_unavailable(
            organization_id, None, None, None,
            verified_at, "git_proof_failed",
        )
        return
    synchronizer.synchronize(
        organization_id, verified_commit, verified_at
    )
```

In `_run_server()`, after catalog rows are loaded and before `create_app()`:

```python
git = GitRegistryAdapter(registry_root)
projection = RegistryProjectionStore(store.connection)
registry_query = RegistryQuery(registry_root, git)
synchronizer = RegistryProjectionSynchronizer(
    git=git, query=registry_query, store=projection,
)
verified_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
_synchronize_registry_on_startup(
    config["organization_id"], git, synchronizer,
    projection, verified_at,
)
web_application = CentralWebApplication(
    store=CentralWebStore(store.connection),
    queries=CentralWebQueries(store.connection, projection),
    catalog=RegistryCatalog(registry_root),
    git=git,
    registry_synchronizer=synchronizer,
)
```

Do not abort Uvicorn for a normal verification/unavailability result. A broken Central SQLite connection may still abort startup because Candidate and Publication persistence would also be unsafe.

- [ ] **Step 5: Refresh only after a durable completed Publication**

Add `registry_synchronizer: RegistryProjectionSynchronizer | None = None` to `CentralWebApplication.__init__`. Require `catalog`, `git`, and synchronizer to be either all configured or all absent. Update the configured application fixture in `tests/test_central_web_api.py` to pass the same projection synchronizer used by its query fixture. After both `confirm()` and `resume()` return, invoke:

```python
def _synchronize_completed_publication(
    self, publication: CentralPublication,
) -> None:
    if publication.state != "completed" or publication.commit_sha is None:
        return
    if self.registry_synchronizer is None:
        raise RuntimeError("Registry synchronizer is not configured")
    try:
        self.registry_synchronizer.synchronize(
            publication.organization_id,
            publication.commit_sha,
            publication.updated_at,
        )
    except RegistryProjectionError:
        return
```

Call this before returning the publication view. This placement is deliberately outside `CentralPublicationService._complete_or_push()` and its SQLite transaction: the completed Git proof is durable first, then the derived read model catches up. Calling it for a completed idempotent replay is required and safe.

- [ ] **Step 6: Add the crash-after-push recovery integration test**

Update the integration app factory to run the same startup helper before constructing `CentralWebQueries`. Add one end-to-end test with this sequence:

```python
published = browser.publish(preview.preview_id)
self.assertEqual("completed", published["state"])
published_commit = published["commit_sha"]

# Simulate process loss after durable Git/publication success by removing
# the derived projection before rebuilding the app around the same database.
with self.store.connection:
    self.store.connection.execute("DELETE FROM registry_decision_projection")
    self.store.connection.execute("DELETE FROM registry_product_projection")
    self.store.connection.execute(
        "UPDATE registry_projection_state SET state = 'syncing'"
    )

restarted = self.restart_application()
detail = restarted.get_decision(
    self.user, self.product_space_id, self.formal_decision_id
)
state = self.projection.get_state("org_demo")

self.assertEqual(self.formal_decision_id, detail.decision.decision_id)
self.assertEqual(published_commit, state.active_commit)
self.assertEqual(
    1, self.git_commit_count_with_subject("decision("),
)
```

Also assert the existing after-commit, before-commit, push-retry, and after-push recovery tests remain unchanged and green. Add a code-only commit fixture with unchanged Registry tree and assert restart updates only `active_commit`/`verified_at`.

- [ ] **Step 7: Run startup/publication/integration tests and commit**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_central_cli \
  tests.test_central_web_publication \
  tests.test_central_web_api \
  tests.integration.test_central_web_vertical -v
```

Expected: PASS, with no duplicate commit or push in the recovery case.

Commit:

```bash
git add src/zdecision/central/cli.py src/zdecision/central/web/application.py tests/test_central_cli.py tests/test_central_web_api.py tests/integration/test_central_web_vertical.py
git commit -m "feat: recover registry projection after publication"
```

---

### Task 5: Dashboard verification semantics and bounded real acceptance

**Files:**
- Modify: `web/src/api/types.ts:15-18`
- Modify: `web/src/pages/company-overview/CompanyOverviewPage.tsx:8-77`
- Modify: `web/src/pages/company-overview/CompanyOverviewPage.test.tsx`
- Rebuild: `src/zdecision/central/static/index.html`
- Rebuild: `src/zdecision/central/static/assets/*`
- Verify: `tests/test_central_web_api.py`
- Verify: `tests/integration/test_central_web_vertical.py`

**Interfaces:**
- Consumes: Task 3 Dashboard JSON field `registry.verified_at`.
- Produces: frontend `RegistryStatus.verified_at: string | null`, visible copy **Registry 已验证**, production static assets, and measured acceptance evidence.

- [ ] **Step 1: Write the failing Dashboard semantic test**

Update the Dashboard fixture to include `verified_at` and add:

```tsx
it("shows a verified Registry proof instead of a synchronization claim", async () => {
  vi.stubGlobal("fetch", vi.fn(() => json({
    ...dashboard,
    registry: {
      state: "available",
      commit_sha: "a".repeat(40),
      verified_at: "2026-08-06T10:00:00Z",
    },
  })));

  render(<RouterProvider router={router} />);

  expect(await screen.findByText(/Registry 已验证/)).toBeVisible();
  expect(screen.queryByText(/Registry 已同步/)).not.toBeInTheDocument();
  expect(screen.getByText(/08\/06/)).toBeVisible();
});
```

- [ ] **Step 2: Run the frontend test and verify RED**

Run:

```bash
cd web
npm test -- CompanyOverviewPage.test.tsx -t "shows a verified Registry proof"
```

Expected: FAIL because the type lacks `verified_at` and the page still says **Registry 已同步**.

- [ ] **Step 3: Add the field and render the exact verification copy**

Change the API type to:

```ts
export interface RegistryStatus {
  state: RegistryState;
  commit_sha: string | null;
  verified_at: string | null;
}
```

Render the header proof without introducing cache, new request state, or new CSS:

```tsx
<StatusBadge
  tone={dashboard.registry.state === "available" ? "success" : "danger"}
>
  Registry {dashboard.registry.state === "available"
    ? `已验证 · ${formatDate(dashboard.registry.verified_at)}`
    : "不可用"}
</StatusBadge>
```

Reuse the existing `formatDate()` function. Do not add React caching, prefetching, polling, or stale-data retention in this slice.

- [ ] **Step 4: Run all automated verification and rebuild packaged Web assets**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
(cd web && npm run typecheck && npm test && npm run build)
```

Expected: all Python and frontend tests PASS; the Vite build replaces the packaged `src/zdecision/central/static` assets.

- [ ] **Step 5: Perform one bounded real latency and durability acceptance**

Start the Demo Central with the existing absolute database/config/Registry arguments. In a second terminal run each request five times and record `time_total`:

```bash
for path in dashboard decisions; do
  for run in 1 2 3 4 5; do
    curl --silent --output /dev/null \
      --write-out "${path} ${run} %{time_total}\n" \
      "http://127.0.0.1:8765/api/v1/web/${path}"
  done
done
```

Resolve the first enabled Decision space from Dashboard, then run Dashboard and Candidate concurrently:

```bash
zdecision_space_id=$(curl --silent \
  http://127.0.0.1:8765/api/v1/web/dashboard \
  | .venv/bin/python -c \
  'import json,sys; print(json.load(sys.stdin)["products"][0]["decision_space_id"])')
curl --silent --output /dev/null \
  --write-out "dashboard %{time_total}\n" \
  http://127.0.0.1:8765/api/v1/web/dashboard &
curl --silent --output /dev/null \
  --write-out "candidate %{time_total}\n" \
  "http://127.0.0.1:8765/api/v1/web/spaces/${zdecision_space_id}/candidates"
wait
```

Acceptance requires every warm formal read and the concurrent Candidate request to be below `0.200` seconds. Inspect access logs during these requests and confirm there is no `git fetch`; then restart Central and verify:

```bash
curl --silent http://127.0.0.1:8765/api/v1/web/dashboard
curl --silent http://127.0.0.1:8765/api/v1/web/decisions
zdecision_publication_id=$(curl --silent \
  http://127.0.0.1:8765/api/v1/web/publications \
  | .venv/bin/python -c \
  'import json,sys; target="02ae3c37e388b74004d771d18468bba06f90a1f6"; print(next(item["publication_id"] for item in json.load(sys.stdin)["items"] if item["commit_sha"] == target))')
curl --silent \
  "http://127.0.0.1:8765/api/v1/web/publications/${zdecision_publication_id}"
```

The Dashboard active projection commit must equal the freshly fetched current `origin/main`; the previously published Decision must remain visible; its Publication receipt must still contain commit `02ae3c37e388b74004d771d18468bba06f90a1f6`. If the current code commits moved `origin/main` without changing `decision-registry`, the active commit may advance while the active tree OID and projection rows remain unchanged.

- [ ] **Step 6: Commit the semantic UI and packaged assets**

Run `git diff --check`, then commit only the intended frontend/source asset changes:

```bash
git add web/src/api/types.ts web/src/pages/company-overview/CompanyOverviewPage.tsx web/src/pages/company-overview/CompanyOverviewPage.test.tsx src/zdecision/central/static
git commit -m "feat: show verified registry read model"
```

Stop here. Record any deferred frontend caching, webhook, polling, multi-writer, or database scaling work as future work; do not start another broad architecture review in this slice.
