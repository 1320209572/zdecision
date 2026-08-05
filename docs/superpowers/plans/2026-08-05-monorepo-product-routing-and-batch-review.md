# Monorepo Product Routing and Batch Candidate Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Approved design:** `docs/superpowers/specs/2026-08-05-monorepo-product-routing-and-batch-review-design.md`

**Goal:** Make one enabled monorepo expose multiple product and concrete Shared-package Decision spaces, route one Update action into trusted leaf Capture slices, and replace the Candidate card/select UI with leaf-scoped batch Review.

**Architecture:** The central service owns a versioned Decision catalog and repository routes. One user action creates a repository-scoped Capture group; the local Agent freezes Git path evidence, selects only server-issued leaf routes, and runs one crash-safe Capture/reconciliation/upload slice per matched product or Shared package. The Web uses neutral `decision_space_id` routes, renders Shared as a non-publishable directory/package tree, and continues publishing each leaf through an isolated V1 compatibility Registry partition.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLite, the existing local Codex app-server Gateway and V1 Git Registry modules, React 19.2.8, React Router 7.18.2, TypeScript 7.0.2, Vite 8.2.0, Vitest 4.1.10, Testing Library 16.3.2, plain CSS design tokens.

## Global Constraints

- Work directly on the existing `main` branch; do not create a worktree, feature branch, or Registry branch.
- A Git repository is a source container, never a product identity.
- `Shared` and its intermediate directory nodes are navigation/aggregate nodes only. They cannot own Capture output, Candidate state, Review drafts, previews, publications, or Registry documents.
- Every routable Shared target is one explicitly registered tracked component, module, or standalone library such as `packages/products/shared/zcf-audit`, `packages/shared/theme`, or `packages/design`.
- The central trusted configuration owns organization, repository, route, Decision-space, catalog, package, asset-type, and V1 compatibility-partition metadata. The browser, model, and local upload payload cannot override them.
- Product/Shared routing uses normalized repository-relative Git paths frozen locally. `CandidateContent.paths`, package names, ignored output, and model text never choose ownership.
- A broad `packages/products/shared/** -> Shared` or `packages/shared/** -> Shared` fallback is invalid.
- Raw Sessions, Prompts, tool output, source code, diffs, credentials, and local absolute paths never enter central persistence or HTTP payloads.
- One Candidate family, Review batch, preview, publication, and formal Decision belongs to exactly one leaf Decision space.
- The V1 Registry JSON shape and `decision-registry/products/prod_<id>/...` paths remain unchanged. Every Shared leaf receives a distinct internal compatibility `product_id`; no user-facing surface calls it a product.
- Checkbox selection is transient and never means approval. Review actions are direct `accept`, `reject`, or `edit_accept`; unclassified means unprocessed and the primary UI does not expose `skip`.
- Selection, Review drafts, and Review submission never cross leaf Decision spaces. The existing submission limit remains 20 and must be explained instead of silently truncating.
- Review submission and publication remain separate explicit actions.
- Do not include the Dashboard Git-fetch performance fix, Registry V2, SSO, Git-role authorization, route-administration UI, comments, notifications, or recall changes.
- Stop after the seven tasks and the listed acceptance commands pass. Do not start another broad review cycle; record any non-blocking follow-up separately.

## File Map

### Central catalog, Capture, and migration

- Create `src/zdecision/central/decision_spaces.py` — strict catalog-group, leaf-space, route-version, and safe tree contracts.
- Create `src/zdecision/central/migrations.py` — the single idempotent migration from one-repository/one-product Candidate ownership to trusted leaf ownership or archive-and-recapture.
- Modify `src/zdecision/ids.py` — stable `dsp_`, `dsg_`, `drr_`, and `csl_` identities while retaining existing V1 `prod_` identities.
- Modify `src/zdecision/sync/contracts.py` — neutral repository catalog, Capture-group, slice, frozen ownership, and leaf-batch transport values.
- Modify `src/zdecision/central/store.py` — Decision-space/route tables, Capture groups/slices, frozen Candidate ownership, and archive state.
- Modify `src/zdecision/central/service.py` — create/claim/plan/complete groups, derive slice ownership, and atomically accept leaf Candidate batches.
- Modify `src/zdecision/central/api.py` — strict group/slice endpoints and neutral repository-space responses.
- Modify `src/zdecision/central/cli.py` — load the trusted catalog/routes and write neutral Agent repository config.
- Modify `src/zdecision/central/web/schema.py` — associate Web Candidate evidence with frozen leaf ownership.

### Local Agent routing and Capture

- Create `src/zdecision/agent/repository_routes.py` — validate and match versioned server-issued routes.
- Create `src/zdecision/agent/git_path_evidence.py` — freeze normalized tracked Git paths and their digest without reading or transmitting diff content.
- Create `src/zdecision/agent/capture_routing.py` — persist one group plan with deterministic leaf slices.
- Modify `src/zdecision/agent/db.py` — neutral enabled-repository state, route snapshots, Git evidence, and group/slice recovery tables.
- Modify `src/zdecision/agent/service.py` — load neutral config, synchronize routes, and process claimed groups.
- Modify `src/zdecision/agent/central_client.py` — group claim/plan, leaf batch upload, slice receipt, and group completion calls.
- Modify `src/zdecision/agent/capture_processor.py` — freeze sources once, plan slices, process each leaf, resume incomplete slices, then acknowledge the group.
- Modify `src/zdecision/agent/request_state.py` — key reconciliation, staged batches, and receipts by `(request_id, slice_id)`.
- Modify `src/zdecision/agent/session_index.py` — retain the group source boundary until all slices complete.
- Modify `src/zdecision/app_server/requested_capture.py` and `src/zdecision/capture/on_demand.py` — freeze the exact leaf instruction and local path-evidence digest into every extraction operation.
- Modify `src/zdecision/capture/reconciliation.py` and `src/zdecision/ids.py` — partition family reconciliation and Candidate identity by `decision_space_id`.

### Leaf Review, Registry compatibility, and Web

- Modify `src/zdecision/central/web/contracts.py`, `store.py`, `queries.py`, `reviews.py`, `previews.py`, `publications.py`, `application.py`, and `api.py` — replace current-mapping product ownership with frozen leaf ownership and neutral `/spaces` routes.
- Preserve `src/zdecision/registry/models.py`, `catalog.py`, `query.py`, and `publication.py` V1 bytes and paths; add compatibility tests rather than a new Registry schema.
- Modify `web/src/api/types.ts`, `web/src/app/router.tsx`, and `web/src/app/AppShell.tsx` — neutral Decision-space types and canonical routes.
- Create `web/src/features/decision-spaces/DecisionSpaceTree.tsx` — non-publishable Shared groups and actionable leaves.
- Create `web/src/pages/repository-entry/RepositoryEntryPage.tsx` — repository deep link that shows all matched spaces instead of guessing one product.
- Modify `web/src/pages/company-overview/CompanyOverviewPage.tsx` and `web/src/pages/review-index/ReviewIndexPage.tsx` — product rows plus the real Shared directory/package tree.
- Create `web/src/features/reviews/CandidateReviewRow.tsx` — compact row, Checkbox, direct actions, edit/evidence disclosure, and stale state.
- Modify `web/src/pages/candidate-review/CandidateReviewPage.tsx` and `web/src/styles/app.css` — leaf-scoped selection, batch actions, undo, counts, and submission boundary.
- Delete `web/src/features/reviews/ReviewEditor.tsx` after its edit controls are moved into `CandidateReviewRow.tsx`.
- Mechanically rebuild `src/zdecision/central/static/` with Vite; never hand-edit hashed assets.

### Acceptance tests and docs

- Add focused Python tests named in Tasks 1–4 and update existing central/local tests.
- Add `web/src/features/decision-spaces/DecisionSpaceTree.test.tsx` and `web/src/features/reviews/CandidateReviewRow.test.tsx`; update page tests.
- Update `tests/integration/test_on_demand_capture_core.py`, `tests/integration/test_inline_candidate_refresh.py`, and `tests/integration/test_central_web_vertical.py` for one real multi-leaf flow.
- Update `README.md`, `docs/architecture.md`, and `docs/demo-central-web.md` with the implemented UI-first workflow and actual Demo configuration shape.

## Acceptance Traceability

| Approved requirement | Implemented and proven by |
| --- | --- |
| One repository exposes multiple products and concrete Shared leaves | Tasks 1, 2, and 7 |
| Shared root/groups aggregate only; real packages own Decisions | Tasks 1, 5, and 7 |
| One Update action creates trusted product/Shared leaf slices | Tasks 2, 3, and 7 |
| Candidate ownership is frozen and migration never guesses from model paths | Tasks 2, 4, and 7 |
| Shared leaves publish through isolated V1 partitions | Task 4 and Task 7 |
| Neutral `/spaces` APIs and repository links never guess a product | Task 5 and Task 7 |
| Compact rows, Checkbox selection, direct/batch actions, undo | Task 6 and Task 7 |
| Review and publication stay separate; raw Session data stays local | Tasks 3, 4, 6, and 7 |
| Nx consumer tags never copy one Shared leaf into consuming products | Task 1 and Task 7 |
| Route-head changes never move an existing Candidate or Review | Tasks 2 and 4 |
| Existing true single-product repositories retain one trusted root route | Tasks 1, 2, and 7 |
| Technical provenance is available on demand without dominating the list | Task 6 |
| Review submits exact current revisions once; stale revisions are blocked | Tasks 4, 6, and 7 |

---

### Task 1: Central Decision Catalog and Versioned Repository Routes

**Files:**
- Create: `src/zdecision/central/decision_spaces.py`
- Modify: `src/zdecision/ids.py`
- Modify: `src/zdecision/sync/contracts.py`
- Modify: `src/zdecision/central/store.py`
- Modify: `src/zdecision/central/cli.py`
- Modify: `src/zdecision/central/service.py`
- Modify: `src/zdecision/central/api.py`
- Test: `tests/test_central_decision_spaces.py`
- Test: `tests/test_sync_contracts.py`
- Test: `tests/test_demo_config.py`
- Test: `tests/test_central_api.py`

**Interfaces:**
- Consumes: existing `canonical_product_name()`, `product_id()`, `CentralStore.connection`, and trusted `central.json` loading.
- Produces: `CatalogGroup`, `LeafDecisionSpace`, `RepositoryDecisionRoute`, neutral `EnabledRepository`/`RepositoryCatalogView`, and `CentralStore` lookup methods consumed by every later task.

```python
DecisionSpaceKind = Literal["product", "shared_unit"]

@dataclass(frozen=True)
class CatalogGroup:
    catalog_group_id: str
    parent_group_id: str | None
    display_name: str
    breadcrumb: tuple[str, ...]
    source_prefix: str | None
    sort_order: int

@dataclass(frozen=True)
class LeafDecisionSpace:
    decision_space_id: str
    kind: DecisionSpaceKind
    display_name: str
    compatibility_product_id: str
    compatibility_product_name: str
    catalog_group_id: str | None
    catalog_breadcrumb: tuple[str, ...]
    source_root: str
    package_name: str | None
    asset_type: str | None
    enabled: bool

@dataclass(frozen=True)
class RepositoryDecisionRoute:
    route_id: str
    repository_id: str
    decision_space_id: str
    path_prefixes: tuple[str, ...]
    excluded_prefixes: tuple[str, ...]
    enabled: bool
    configuration_version: int

    def matches(self, path: str) -> bool:
        included = any(
            prefix == "."
            or path == prefix
            or path.startswith(prefix + "/")
            for prefix in self.path_prefixes
        )
        excluded = any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in self.excluded_prefixes
        )
        return included and not excluded

@dataclass(frozen=True)
class EnabledRepository:
    repository_id: str
    enabled: bool

@dataclass(frozen=True)
class RepositoryCatalogView:
    repository_id: str
    enabled: bool
    spaces: tuple[LeafDecisionSpace, ...]
    routes: tuple[RepositoryDecisionRoute, ...]
    shared_tree: CatalogGroup | None
```

- [ ] **Step 1: Write failing catalog, contract, and trusted-config tests**

```python
def test_shared_group_cannot_be_a_route_target(self) -> None:
    shared = CatalogGroup(
        catalog_group_id="dsg_" + "1" * 32,
        parent_group_id=None,
        display_name="Shared",
        breadcrumb=("Shared",),
        source_prefix=None,
        sort_order=20,
    )
    self.store.put_catalog_group("org_demo", shared)
    with self.assertRaisesRegex(ValueError, "route_target_must_be_leaf"):
        self.store.put_route_version(
            "org_demo",
            RepositoryDecisionRoute(
                route_id="drr_" + "2" * 32,
                repository_id=REPOSITORY_ID,
                decision_space_id=shared.catalog_group_id,
                path_prefixes=("packages/products/shared",),
                excluded_prefixes=(),
                enabled=True,
                configuration_version=1,
            ),
        )

def test_one_repository_returns_product_and_shared_leaf_routes(self) -> None:
    routes = self.service.list_repository_spaces(self.user, REPOSITORY_ID)
    self.assertEqual(
        {"Cloud", "zcf-audit", "theme", "design"},
        {item.display_name for item in routes.spaces},
    )
    self.assertEqual("Shared", routes.shared_tree.display_name)

def test_route_version_append_does_not_overwrite_v1(self) -> None:
    self.store.put_route_version("org_demo", self.theme_route(version=1))
    self.store.put_route_version("org_demo", self.theme_route(version=2))
    self.assertEqual(
        (1, 2),
        tuple(item.configuration_version for item in self.store.route_history(
            "org_demo", self.theme_route().route_id
        )),
    )

def test_true_single_product_repository_accepts_one_root_route(self) -> None:
    route = self.product_route(
        path_prefixes=(".",),
        excluded_prefixes=(),
    )
    self.store.replace_trusted_route_heads(
        "org_demo", SINGLE_PRODUCT_REPOSITORY_ID, (route,)
    )
    self.assertTrue(route.matches("src/main.py"))
```

- [ ] **Step 2: Run the focused tests and verify they fail on the singular product contract**

Run:

```bash
.venv/bin/python -m unittest tests.test_central_decision_spaces tests.test_sync_contracts tests.test_demo_config tests.test_central_api -v
```

Expected: failures because `CatalogGroup`, `LeafDecisionSpace`, route-version storage, and repository-space responses do not exist and the current `RepositoryView` still requires one product.

- [ ] **Step 3: Add stable catalog, Decision-space, and route IDs plus strict contracts**

```python
def decision_space_id(kind: str, compatibility_product_id: str) -> str:
    return _stable_id("dsp", {
        "kind": kind,
        "compatibility_product_id": compatibility_product_id,
    })

def catalog_group_id(breadcrumb: Sequence[str]) -> str:
    return _stable_id("dsg", {"breadcrumb": list(breadcrumb)})

def repository_route_id(repository_id: str, decision_space_id_value: str) -> str:
    return _stable_id("drr", {
        "repository_id": repository_id,
        "decision_space_id": decision_space_id_value,
    })
```

Validate repository-relative POSIX prefixes; reject empty, absolute, `..`, and
duplicate prefixes, non-positive versions, invalid IDs, and a Shared leaf
whose source root is only `packages/products/shared` or `packages/shared`.
The literal `.` is the only root-route sentinel and is valid only when that
repository has exactly one enabled route to one `product` leaf. Reject a root
route in a multi-space repository and reject enabled route sets whose prefix
and exclusion rules can match the same path to more than one leaf. Keep
`product_id()` unchanged for V1 compatibility.
Every transport value implements strict canonical `to_dict()`/`from_dict()`
round trips; unknown fields and non-canonical digests are rejected.

- [ ] **Step 4: Add normalized catalog/route persistence and load the actual Demo tree**

```sql
CREATE TABLE IF NOT EXISTS catalog_groups (
  organization_id TEXT NOT NULL,
  catalog_group_id TEXT NOT NULL,
  parent_group_id TEXT,
  display_name TEXT NOT NULL,
  breadcrumb_json TEXT NOT NULL,
  source_prefix TEXT,
  sort_order INTEGER NOT NULL,
  PRIMARY KEY(organization_id, catalog_group_id)
);

CREATE TABLE IF NOT EXISTS decision_spaces (
  organization_id TEXT NOT NULL,
  decision_space_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('product','shared_unit')),
  display_name TEXT NOT NULL,
  compatibility_product_id TEXT NOT NULL,
  compatibility_product_name TEXT NOT NULL,
  catalog_group_id TEXT,
  catalog_breadcrumb_json TEXT NOT NULL,
  source_root TEXT NOT NULL,
  package_name TEXT,
  asset_type TEXT,
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
  PRIMARY KEY(organization_id, decision_space_id),
  UNIQUE(organization_id, compatibility_product_id)
);

CREATE TABLE IF NOT EXISTS repositories (
  organization_id TEXT NOT NULL,
  repository_id TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
  PRIMARY KEY(organization_id, repository_id)
);

CREATE TABLE IF NOT EXISTS repository_route_versions (
  organization_id TEXT NOT NULL,
  route_id TEXT NOT NULL,
  configuration_version INTEGER NOT NULL CHECK(configuration_version > 0),
  repository_id TEXT NOT NULL,
  decision_space_id TEXT NOT NULL,
  path_prefixes_json TEXT NOT NULL,
  excluded_prefixes_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
  PRIMARY KEY(organization_id, route_id, configuration_version)
);

CREATE TABLE IF NOT EXISTS repository_route_heads (
  organization_id TEXT NOT NULL,
  route_id TEXT NOT NULL,
  configuration_version INTEGER NOT NULL,
  PRIMARY KEY(organization_id, route_id)
);
```

`central.json` becomes the sole authority for catalog groups, leaf spaces,
repositories, and routes. `agent.json` retains only neutral enabled repository
IDs and credentials; it does not duplicate product or Shared ownership.

- [ ] **Step 5: Make repository APIs return the exact enabled route catalog**

```python
def list_repository_spaces(
    self, user: Principal, repository_id: str
) -> RepositoryCatalogView:
    principal = _require_user(user)
    return self.store.repository_catalog(
        principal.organization_id, repository_id
    )
```

Reject missing/disabled repositories, routes pointing to disabled leaves,
route heads whose canonical record is corrupt, and catalog cycles. A package
with several Nx consumer tags remains the single Shared leaf configured by its
source root.

- [ ] **Step 6: Re-run focused tests and commit the catalog foundation**

Run:

```bash
.venv/bin/python -m unittest tests.test_central_decision_spaces tests.test_sync_contracts tests.test_demo_config tests.test_central_api -v
```

Expected: all listed tests pass.

Commit:

```bash
git add src/zdecision/central/decision_spaces.py src/zdecision/ids.py src/zdecision/sync/contracts.py src/zdecision/central/store.py src/zdecision/central/cli.py src/zdecision/central/service.py src/zdecision/central/api.py tests/test_central_decision_spaces.py tests/test_sync_contracts.py tests/test_demo_config.py tests/test_central_api.py
git commit -m "feat: add decision space catalog routes"
```

### Task 2: Capture Groups, Leaf Slices, and Frozen Candidate Ownership

**Files:**
- Create: `src/zdecision/central/migrations.py`
- Modify: `src/zdecision/ids.py`
- Modify: `src/zdecision/sync/contracts.py`
- Modify: `src/zdecision/central/store.py`
- Modify: `src/zdecision/central/service.py`
- Modify: `src/zdecision/central/api.py`
- Modify: `src/zdecision/central/web/schema.py`
- Test: `tests/test_central_candidate_ownership.py`
- Test: `tests/test_central_migrations.py`
- Test: `tests/test_central_requests.py`
- Test: `tests/test_central_api.py`
- Test: `tests/test_central_web_store.py`

**Interfaces:**
- Consumes: Task 1 `LeafDecisionSpace`, `RepositoryDecisionRoute`, and `CentralStore.list_enabled_routes()`.
- Produces: repository-scoped `CaptureGroupView`, `ClaimedCaptureGroup`, deterministic `CaptureSliceView`, `CandidateOwnershipSnapshot`, leaf-batch receipt, and an archive-only migration for ambiguous historical Candidates.

```python
@dataclass(frozen=True)
class CaptureGroupCreate:
    repository_id: str
    template_id: str
    capture_scope: CaptureScope
    client_action_id: str

@dataclass(frozen=True)
class CaptureGroupView:
    request_id: str
    repository_id: str
    template_id: str
    capture_scope: CaptureScope
    client_action_id: str
    state: str
    last_sequence: int

@dataclass(frozen=True)
class ClaimedCaptureGroup:
    request_id: str
    repository_id: str
    template_id: str
    capture_scope: CaptureScope
    client_action_id: str
    route_snapshot: tuple[RepositoryDecisionRoute, ...]
    route_snapshot_digest: str
    lease_token: str
    lease_expires_at: str

@dataclass(frozen=True)
class RouteSelection:
    route_id: str
    configuration_version: int
    matched_path_digest: str
    source_boundary_digest: str

@dataclass(frozen=True)
class CandidateOwnershipSnapshot:
    repository_id: str
    route_id: str
    route_configuration_version: int
    decision_space_id: str
    decision_space_kind: DecisionSpaceKind
    display_name: str
    catalog_breadcrumb: tuple[str, ...]
    source_root: str
    compatibility_product_id: str
    compatibility_product_name: str
    source_boundary_digest: str

@dataclass(frozen=True)
class CaptureSliceView:
    request_id: str
    slice_id: str
    slice_order: int
    ownership: CandidateOwnershipSnapshot
    state: str

@dataclass(frozen=True)
class CandidateSliceBatchUpload:
    request_id: str
    slice_id: str
    route_id: str
    route_configuration_version: int
    decision_space_id: str
    items: tuple[CandidateRevisionUpload, ...]
    batch_digest: str

@dataclass(frozen=True)
class SliceUploadReceipt:
    request_id: str
    slice_id: str
    candidate_count: int
    receipt_digest: str
```

- [ ] **Step 1: Write failing group, ownership, and migration tests**

```python
def test_one_action_plans_three_frozen_leaf_slices(self) -> None:
    group = self.service.create_group(self.user, self.command(), NOW)
    claimed = self.service.claim_next_group(self.device, NOW)
    slices = self.service.plan_slices(
        self.device,
        claimed.request_id,
        claimed.lease_token,
        (
            self.selection(CLOUD_ROUTE_ID, "1" * 64),
            self.selection(ZCF_LICENSE_ROUTE_ID, "2" * 64),
            self.selection(THEME_ROUTE_ID, "3" * 64),
        ),
        NOW,
    )
    self.assertEqual(group.request_id, claimed.request_id)
    self.assertEqual(3, len(slices))
    self.assertEqual(
        {CLOUD_SPACE_ID, ZCF_LICENSE_SPACE_ID, THEME_SPACE_ID},
        {item.ownership.decision_space_id for item in slices},
    )

def test_upload_cannot_override_frozen_leaf(self) -> None:
    slice_view = self.plan_theme_slice()
    with self.assertRaisesRegex(RequestConflict, "slice_ownership_conflict"):
        self.service.accept_slice_batch(
            self.device,
            slice_view.request_id,
            slice_view.slice_id,
            self.batch(decision_space_id=CLOUD_SPACE_ID),
            NOW,
        )

def test_new_route_head_does_not_move_existing_candidate(self) -> None:
    first = self.accept_theme_candidate(route_version=1)
    self.store.put_route_version("org_demo", self.theme_route(version=2))
    current = self.store.candidate_ownership(
        "org_demo", REPOSITORY_ID, first.family_id, first.revision
    )
    self.assertEqual(THEME_SPACE_ID, current.decision_space_id)
    self.assertEqual(1, current.route_configuration_version)

def test_generic_monorepo_candidates_are_archived_not_guessed(self) -> None:
    report = migrate_legacy_repository_candidates(
        self.connection,
        "org_demo",
        REPOSITORY_ID,
        policy="archive_and_recapture",
        root_route=None,
        archived_at=NOW,
    )
    self.assertEqual(2, report.archived_family_count)
    self.assertEqual(0, report.backfilled_family_count)
```

- [ ] **Step 2: Run focused tests and verify the old singular request schema fails**

Run:

```bash
.venv/bin/python -m unittest tests.test_central_candidate_ownership tests.test_central_migrations tests.test_central_requests tests.test_central_api tests.test_central_web_store -v
```

Expected: failures because Capture groups/slices, ownership snapshots, and the explicit migration do not exist.

- [ ] **Step 3: Add the additive group/slice schema and deterministic IDs**

```python
def capture_slice_id(
    request_id: str,
    route_id: str,
    configuration_version: int,
) -> str:
    return _stable_id("csl", {
        "request_id": request_id,
        "route_id": route_id,
        "configuration_version": configuration_version,
    })
```

```sql
CREATE TABLE IF NOT EXISTS capture_groups (
  request_id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  repository_id TEXT NOT NULL,
  template_id TEXT NOT NULL,
  capture_scope TEXT NOT NULL,
  client_action_id TEXT NOT NULL,
  route_snapshot_json TEXT NOT NULL,
  route_snapshot_digest TEXT NOT NULL,
  state TEXT NOT NULL,
  attempt_count INTEGER NOT NULL,
  claimed_device_id TEXT,
  lease_token_digest TEXT,
  lease_expires_at TEXT,
  last_sequence INTEGER NOT NULL,
  terminal_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capture_slices (
  request_id TEXT NOT NULL,
  slice_id TEXT NOT NULL,
  slice_order INTEGER NOT NULL,
  route_id TEXT NOT NULL,
  route_configuration_version INTEGER NOT NULL,
  decision_space_id TEXT NOT NULL,
  ownership_json TEXT NOT NULL,
  ownership_digest TEXT NOT NULL,
  matched_path_digest TEXT NOT NULL,
  source_boundary_digest TEXT NOT NULL,
  state TEXT NOT NULL,
  receipt_json TEXT,
  receipt_digest TEXT,
  PRIMARY KEY(request_id, slice_id),
  UNIQUE(request_id, route_id)
);

CREATE TABLE IF NOT EXISTS candidate_revision_ownership (
  organization_id TEXT NOT NULL,
  repository_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  decision_space_id TEXT NOT NULL,
  route_id TEXT NOT NULL,
  route_configuration_version INTEGER NOT NULL,
  ownership_json TEXT NOT NULL,
  ownership_digest TEXT NOT NULL,
  PRIMARY KEY(organization_id, repository_id, family_id, revision)
);

CREATE TABLE IF NOT EXISTS candidate_family_archives (
  organization_id TEXT NOT NULL,
  repository_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  archived_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, repository_id, family_id)
);
```

Keep legacy `capture_requests`/`candidate_batches` readable for historical
technical evidence. All new Update actions use `capture_groups` and
`capture_slices`; do not keep two active creation paths.

- [ ] **Step 4: Implement group creation, lease, and server-derived slice planning**

Keep the installed plugin's V1 Capture-request URLs as compatibility names;
their implementation now creates and returns Capture groups:

```text
POST /api/v1/capture-requests
GET  /api/v1/capture-requests/{request_id}/events
POST /api/v1/agent/capture-requests/claim
POST /api/v1/agent/capture-requests/{request_id}/start
POST /api/v1/agent/capture-requests/{request_id}/heartbeat
POST /api/v1/agent/capture-requests/{request_id}/slices
PUT  /api/v1/agent/capture-requests/{request_id}/slices/{slice_id}/batch
POST /api/v1/agent/capture-requests/{request_id}/complete
POST /api/v1/agent/capture-requests/{request_id}/fail
```

The browser continues creating one request and following one event stream; it
does not know how many internal slices will result. The existing
`/api/v1/plugin/capture-requests` create/read aliases continue delegating to the
same group service for installed-card compatibility.

```python
def plan_slices(
    self,
    device: Principal,
    request_id: str,
    lease_token: str,
    selections: Sequence[RouteSelection],
    now: datetime,
) -> tuple[CaptureSliceView, ...]:
    """Validate selections against the frozen route snapshot and return immutable slices."""
```

An empty selection completes the group as `succeeded_no_candidates` with
`no_routable_decision_space_changes`. Duplicate route IDs, stale versions,
group nodes, missing routes, disabled leaves, or selection replay with different
digests fail closed. Slice IDs are deterministic from `(request_id, route_id,
configuration_version)`; retries return byte-identical slices.
Completion verifies that every planned slice has one accepted receipt and that
the supplied aggregate receipt digest is canonical. Empty-plan completion and
replayed completion are idempotent; partial completion is rejected.

- [ ] **Step 5: Freeze ownership during leaf upload and add the one-purpose migration**

```python
def accept_slice_batch(
    self,
    device: Principal,
    request_id: str,
    slice_id: str,
    lease_token: str,
    batch: CandidateSliceBatchUpload,
    now: datetime,
) -> SliceUploadReceipt:
    """Atomically persist revisions, frozen ownership, Web associations, and one replay-stable receipt."""
```

The upload contains only IDs/digests and Candidate records. The service derives
all names, catalog data, and V1 partition data from `capture_slices`. Save
Candidate heads by `(organization_id, repository_id, decision_space_id,
family_id)` and exclude archived families from the new Inbox.
Because V1 `CandidateContent` still contains a field named `product`, require
its value to equal the slice's frozen compatibility product name; reject a
different client/model value as `slice_content_ownership_conflict`. Web views
use the neutral Decision-space identity and breadcrumb instead of treating
that compatibility field as a user-facing product classification.

`migrate_legacy_repository_candidates()` has exactly two policies:

```python
MigrationPolicy = Literal["trusted_root_backfill", "archive_and_recapture"]
```

`trusted_root_backfill` requires one explicit true single-product root route.
`archive_and_recapture` preserves revision bytes, adds archive rows, and never
reads `CandidateContent.paths` to infer ownership.

- [ ] **Step 6: Re-run focused tests and commit the central Capture boundary**

Run:

```bash
.venv/bin/python -m unittest tests.test_central_candidate_ownership tests.test_central_migrations tests.test_central_requests tests.test_central_api tests.test_central_web_store -v
```

Expected: all listed tests pass.

Commit:

```bash
git add src/zdecision/central/migrations.py src/zdecision/ids.py src/zdecision/sync/contracts.py src/zdecision/central/store.py src/zdecision/central/service.py src/zdecision/central/api.py src/zdecision/central/web/schema.py tests/test_central_candidate_ownership.py tests/test_central_migrations.py tests/test_central_requests.py tests/test_central_api.py tests/test_central_web_store.py
git commit -m "feat: freeze capture slice ownership"
```

### Task 3: Local Trusted Git Routing and Multi-Slice Capture

**Files:**
- Create: `src/zdecision/agent/repository_routes.py`
- Create: `src/zdecision/agent/git_path_evidence.py`
- Create: `src/zdecision/agent/capture_routing.py`
- Modify: `src/zdecision/agent/db.py`
- Modify: `src/zdecision/agent/service.py`
- Modify: `src/zdecision/agent/central_client.py`
- Modify: `src/zdecision/agent/capture_processor.py`
- Modify: `src/zdecision/agent/request_state.py`
- Modify: `src/zdecision/agent/session_index.py`
- Modify: `src/zdecision/app_server/requested_capture.py`
- Modify: `src/zdecision/capture/on_demand.py`
- Modify: `src/zdecision/capture/reconciliation.py`
- Modify: `src/zdecision/ids.py`
- Test: `tests/test_repository_routes.py`
- Test: `tests/test_git_path_evidence.py`
- Test: `tests/test_capture_routing.py`
- Test: `tests/test_capture_request_processor.py`
- Test: `tests/test_requested_capture.py`
- Test: `tests/test_candidate_reconciliation.py`
- Test: `tests/test_central_client.py`
- Test: `tests/test_agent_service.py`
- Test: `tests/integration/test_on_demand_capture_core.py`
- Test: `tests/integration/test_inline_candidate_refresh.py`

**Interfaces:**
- Consumes: Task 2 `ClaimedCaptureGroup`, `RouteSelection`, group lease, and slice upload endpoints; existing `SessionIndex.freeze_sources()` and local Codex app-server runner.
- Produces: replay-stable `FrozenGitPathEvidence`, `CaptureGroupPlan`, per-leaf `FrozenCaptureInput`, slice-partitioned reconciliation state, and exact slice receipts.

```python
@dataclass(frozen=True)
class FrozenGitPathEvidence:
    repository_id: str
    head_commit: str
    commit_ranges: tuple["FrozenCommitRange", ...]
    paths: tuple[str, ...]
    evidence_digest: str

@dataclass(frozen=True)
class FrozenCommitRange:
    source_key: str
    base_exclusive: str
    head_inclusive: str

@dataclass(frozen=True)
class RepositoryRouteSnapshot:
    repository_id: str
    routes: tuple[RepositoryDecisionRoute, ...]
    digest: str

    @classmethod
    def create(
        cls,
        repository_id: str,
        routes: tuple[RepositoryDecisionRoute, ...],
    ) -> "RepositoryRouteSnapshot":
        ordered = tuple(sorted(
            routes,
            key=lambda item: (item.route_id, item.configuration_version),
        ))
        digest = hashlib.sha256(canonical_json_bytes({
            "repository_id": repository_id,
            "routes": [route.to_dict() for route in ordered],
        })).hexdigest()
        return cls(repository_id=repository_id, routes=ordered, digest=digest)

@dataclass(frozen=True)
class MatchedRoute:
    route: RepositoryDecisionRoute
    matched_paths: tuple[str, ...]
    matched_path_digest: str

@dataclass(frozen=True)
class CaptureSlicePlan:
    slice_id: str
    route_id: str
    route_configuration_version: int
    decision_space_id: str
    matched_paths: tuple[str, ...]
    matched_path_digest: str
    source_boundary_digest: str
    source_keys: tuple[str, ...]

@dataclass(frozen=True)
class CaptureGroupPlan:
    request_id: str
    repository_id: str
    route_snapshot_digest: str
    evidence_digest: str
    source_boundary_digest: str
    slices: tuple[CaptureSlicePlan, ...]

    def route_selections(self) -> tuple[RouteSelection, ...]:
        return tuple(
            RouteSelection(
                route_id=item.route_id,
                configuration_version=item.route_configuration_version,
                matched_path_digest=item.matched_path_digest,
                source_boundary_digest=item.source_boundary_digest,
            )
            for item in self.slices
        )
```

- [ ] **Step 1: Write failing trusted-path and route-planner tests**

```python
def test_cloud_license_and_theme_paths_make_three_leaf_slices(self) -> None:
    evidence = self.evidence(
        "packages/products/cloud/apps/core-shell/src/app.tsx",
        "packages/products/shared/zcf-license/src/App.tsx",
        "packages/shared/theme/src/index.ts",
    )
    plan = plan_capture_group(
        self.claimed_group(), self.route_snapshot(), evidence, self.sources()
    )
    self.assertEqual(
        (CLOUD_SPACE_ID, ZCF_LICENSE_SPACE_ID, THEME_SPACE_ID),
        tuple(item.decision_space_id for item in plan.slices),
    )

def test_generic_shared_route_is_rejected_before_matching(self) -> None:
    with self.assertRaisesRegex(ValueError, "generic_shared_route_forbidden"):
        RepositoryRouteSnapshot.create(
            REPOSITORY_ID,
            (self.route("packages/products/shared", SHARED_GROUP_ID),),
        )

def test_shared_leaf_does_not_fan_out_to_consuming_products(self) -> None:
    plan = plan_capture_group(
        self.claimed_group_with_cloud_portal_and_license_routes(),
        self.route_snapshot_with_cloud_portal_and_license_routes(),
        self.evidence("packages/products/shared/zcf-license/src/App.tsx"),
        self.sources(),
    )
    self.assertEqual(
        (ZCF_LICENSE_SPACE_ID,),
        tuple(item.decision_space_id for item in plan.slices),
    )

def test_git_evidence_never_contains_diff_content(self) -> None:
    frozen = self.reader.freeze(self.repository_snapshot(), self.sources())
    encoded = canonical_json_bytes(frozen.to_dict())
    self.assertIn(b"packages/shared/theme/src/index.ts", encoded)
    self.assertNotIn(b"PRIVATE_SOURCE_SENTINEL", encoded)
    self.assertNotIn(b"session_id", encoded)
```

- [ ] **Step 2: Run the routing tests and verify they fail before model execution**

Run:

```bash
.venv/bin/python -m unittest tests.test_repository_routes tests.test_git_path_evidence tests.test_capture_routing tests.test_agent_service tests.test_central_client -v
```

Expected: failures because route snapshots, path evidence, and group planning are absent and the Agent still mirrors one product per repository.

- [ ] **Step 3: Implement strict route snapshots and local Git evidence**

```python
class GitPathEvidenceReader:
    def freeze(
        self,
        repository: RepositorySnapshot,
        sources: tuple[FrozenSessionSource, ...],
    ) -> FrozenGitPathEvidence:
        """Freeze only normalized path names from committed, index, worktree, and untracked tracked-eligible changes."""
```

Run bounded, non-network Git commands from the resolved repository root:

```text
git diff --name-only --diff-filter=ACMRTUXB HEAD
git diff --cached --name-only --diff-filter=ACMRTUXB
git ls-files --others --exclude-standard
```

When a frozen Session boundary records an earlier trusted HEAD, also include
`git diff --name-only --diff-filter=ACMRTUXB <base>..<head>`. Normalize to POSIX
repository-relative tracked paths, sort/deduplicate, reject NUL/absolute/`..`,
and store only the paths, HEAD coordinates, and canonical digest locally. Do
not run `fetch`, read file bytes, or serialize command output other than path
names.

Extend `FrozenSessionSource` and the local checkpoint migration with optional
`previous_handled_head_commit` and `upper_head_commit` fields obtained only
from repository-resolved Hook events. A commit range is usable only when both
coordinates are valid commits in the same frozen worktree; otherwise omit the
range and retain the index/worktree evidence. This is how a task that already
committed or pushed its code still contributes trusted changed paths.

- [ ] **Step 4: Persist one immutable plan from only server-issued claimed routes**

```python
class RepositoryRouteMatcher:
    def match(
        self,
        paths: tuple[str, ...],
        snapshot: RepositoryRouteSnapshot,
    ) -> tuple[MatchedRoute, ...]:
        grouped: dict[str, list[str]] = {}
        route_by_id = {route.route_id: route for route in snapshot.routes}
        for path in paths:
            matches = tuple(
                route for route in snapshot.routes
                if route.enabled and route.matches(path)
            )
            if len(matches) > 1:
                raise ValueError("decision_space_route_ambiguous")
            if matches:
                grouped.setdefault(matches[0].route_id, []).append(path)
        return tuple(
            MatchedRoute(
                route=route_by_id[route_id],
                matched_paths=tuple(sorted(values)),
                matched_path_digest=hashlib.sha256(canonical_json_bytes({
                    "paths": sorted(values),
                })).hexdigest(),
            )
            for route_id, values in sorted(grouped.items())
        )

def plan_capture_group(
    group: ClaimedCaptureGroup,
    snapshot: RepositoryRouteSnapshot,
    evidence: FrozenGitPathEvidence,
    sources: tuple[FrozenSessionSource, ...],
) -> CaptureGroupPlan:
    if group.route_snapshot_digest != snapshot.digest:
        raise ValueError("route_snapshot_mismatch")
    matched = RepositoryRouteMatcher().match(evidence.paths, snapshot)
    source_keys = tuple(source.source_key for source in sources)
    source_boundary_digest = hashlib.sha256(canonical_json_bytes({
        "sources": [
            {
                "source_key": source.source_key,
                "source_fingerprint": source.source_fingerprint,
                "previous_handled_head_commit": (
                    source.previous_handled_head_commit
                ),
                "upper_head_commit": source.upper_head_commit,
            }
            for source in sources
        ],
    })).hexdigest()
    return CaptureGroupPlan(
        request_id=group.request_id,
        repository_id=group.repository_id,
        route_snapshot_digest=snapshot.digest,
        evidence_digest=evidence.evidence_digest,
        source_boundary_digest=source_boundary_digest,
        slices=tuple(
            CaptureSlicePlan(
                slice_id=capture_slice_id(
                    group.request_id,
                    item.route.route_id,
                    item.route.configuration_version,
                ),
                route_id=item.route.route_id,
                route_configuration_version=(
                    item.route.configuration_version
                ),
                decision_space_id=item.route.decision_space_id,
                matched_paths=item.matched_paths,
                matched_path_digest=item.matched_path_digest,
                source_boundary_digest=source_boundary_digest,
                source_keys=source_keys,
            )
            for item in matched
        ),
    )
```

Every path must match zero or one enabled leaf after exclusions. More than one
match raises `decision_space_route_ambiguous`; no matches returns an empty plan
and never calls the model. Persist route snapshot digest, evidence digest,
ordered slice identities, matched local paths, and source keys before sending
only `RouteSelection` IDs/digests to central.

Build `RepositoryRouteSnapshot` only from the claimed group's
`route_snapshot`; its digest must equal the server-frozen digest before any Git
command or model call. Persist that exact snapshot with the Capture-group plan
for retry, rather than consulting a newer route head. Replace
`feasibility_repository_mappings` product fields with neutral enabled
repository state. Current/All-valid buttons remain repository-scoped and do
not add a product selector.

`AgentDatabase.get_repository_snapshot(repository_id)` selects the newest
repository-resolved local event with a non-null trusted worktree root. Before
reading paths, `GitPathEvidenceReader` reruns `RepositoryResolver` on that root
and requires the same repository ID, so the frozen HEAD and path evidence come
from the current verified Git worktree rather than stale event coordinates.

- [ ] **Step 5: Make extraction, reconciliation, and outbox state leaf-scoped**

```python
@dataclass(frozen=True)
class FrozenCaptureRouteContext:
    decision_space_id: str
    decision_space_kind: Literal["product", "shared_unit"]
    decision_space_name: str
    route_id: str
    route_configuration_version: int
    compatibility_product_id: str
    matched_path_digest: str
```

Extend the existing `FrozenCaptureInput` record-version contract without
removing its current Session-source, template, model-profile, and Capture
boundary fields. Add one required `route_context: FrozenCaptureRouteContext`
field; bump the record version and keep the old reader only for archive/retry
of requests created before this migration.

Include `decision_space_id` in `candidate_family_id()` and every local
reconciliation/outbox primary key. The extractor receives one fixed leaf name
and the matched local path set for that slice; it cannot emit another leaf.
`RequestStateStore` keys committed reconciliation, staged batch, and receipt by
`(request_id, slice_id)`. Change `apply_reconciliation()` to require
`repository_id`, `decision_space_id`, `observations`, and `current`, and return
a `ReconciliationResult` carrying the same repository and Decision-space IDs.

- [ ] **Step 6: Change the processor to resume incomplete slices and acknowledge once**

```python
def _process(self, group: ClaimedCaptureGroup, client: CentralClient) -> None:
    route_snapshot = RepositoryRouteSnapshot.create(
        group.repository_id,
        group.route_snapshot,
    )
    if route_snapshot.digest != group.route_snapshot_digest:
        raise TerminalCaptureRequestError("route_snapshot_mismatch")
    sources = self.session_index.freeze_sources(
        group.request_id,
        group.repository_id,
        self._now(),
        capture_scope=group.capture_scope,
        selected_session_id=self._selected_session_id(group),
    )
    evidence = self.git_paths.freeze(
        self.database.get_repository_snapshot(group.repository_id),
        sources,
    )
    plan = self.routing_store.get_or_create_plan(
        group,
        route_snapshot,
        sources,
        evidence,
    )
    slices = client.plan_slices(group, plan.route_selections())
    if tuple(item.slice_id for item in slices) != tuple(
        item.slice_id for item in plan.slices
    ):
        raise TerminalCaptureRequestError("capture_slice_plan_mismatch")
    for slice_view in slices:
        if not self.request_state.has_receipt(group.request_id, slice_view.slice_id):
            self._process_slice(group, slice_view, plan, sources, client)
    client.complete_group(group.request_id, self.request_state.receipts_digest(group.request_id))
    self.session_index.acknowledge(group.request_id, self.request_state.receipts_digest(group.request_id), self._now())
```

A crash after one receipt reuses the frozen group plan and skips that slice;
it never reruns or rebinds its Candidate batch. A slice failure leaves the
group retryable without acknowledging the Session boundary. Raw app-server
content remains in the existing local runner only.

Before `_process_slice()`, bind the returned `CaptureSliceView` to the local
plan by exact slice/route/version/Decision-space IDs and freeze the server
ownership snapshot into `FrozenCaptureRouteContext`. Any mismatch is terminal
and occurs before extraction.

- [ ] **Step 7: Run focused local tests and the two Capture integrations**

Run:

```bash
.venv/bin/python -m unittest tests.test_repository_routes tests.test_git_path_evidence tests.test_capture_routing tests.test_capture_request_processor tests.test_requested_capture tests.test_candidate_reconciliation tests.test_central_client tests.test_agent_service tests.integration.test_on_demand_capture_core tests.integration.test_inline_candidate_refresh -v
```

Expected: all listed tests pass, including one action/three slices, no-route
short circuit, ambiguous-route fail-closed, and restart after one uploaded
slice.

- [ ] **Step 8: Commit the complete local multi-slice pipeline**

```bash
git add src/zdecision/agent/repository_routes.py src/zdecision/agent/git_path_evidence.py src/zdecision/agent/capture_routing.py src/zdecision/agent/db.py src/zdecision/agent/service.py src/zdecision/agent/central_client.py src/zdecision/agent/capture_processor.py src/zdecision/agent/request_state.py src/zdecision/agent/session_index.py src/zdecision/app_server/requested_capture.py src/zdecision/capture/on_demand.py src/zdecision/capture/reconciliation.py src/zdecision/ids.py tests/test_repository_routes.py tests/test_git_path_evidence.py tests/test_capture_routing.py tests/test_capture_request_processor.py tests/test_requested_capture.py tests/test_candidate_reconciliation.py tests/test_central_client.py tests/test_agent_service.py tests/integration/test_on_demand_capture_core.py tests/integration/test_inline_candidate_refresh.py
git commit -m "feat: capture candidates by decision space"
```

### Task 4: Leaf Review and V1 Registry Compatibility Publication

**Files:**
- Modify: `src/zdecision/central/web/contracts.py`
- Modify: `src/zdecision/central/web/schema.py`
- Modify: `src/zdecision/central/web/store.py`
- Modify: `src/zdecision/central/web/queries.py`
- Modify: `src/zdecision/central/web/reviews.py`
- Modify: `src/zdecision/central/web/previews.py`
- Modify: `src/zdecision/central/web/publications.py`
- Test: `tests/test_central_web_store.py`
- Test: `tests/test_central_web_review.py`
- Test: `tests/test_central_web_preview.py`
- Test: `tests/test_central_web_publication.py`
- Test: `tests/test_registry.py`
- Test: `tests/integration/test_central_web_vertical.py`

**Interfaces:**
- Consumes: Task 2 frozen `CandidateOwnershipSnapshot` and each leaf's immutable V1 compatibility `product_id`/name.
- Produces: leaf-owned `ReviewDraft`, `CentralReviewBatch`, Preview, Publication, and formal V1 Decision without changing Registry canonical bytes.

- [ ] **Step 1: Write failing cross-leaf and V1 compatibility tests**

```python
def test_review_rejects_items_from_another_leaf(self) -> None:
    with self.assertRaises(DecisionSpaceOwnershipConflict):
        self.reviews.submit(
            self.user,
            THEME_SPACE_ID,
            "web_action_cross_leaf",
            0,
            (self.draft_item(family_id=DESIGN_FAMILY_ID),),
            NOW,
        )

def test_shared_root_cannot_create_a_preview(self) -> None:
    with self.assertRaisesRegex(NoAcceptedItems, "decision_space_not_leaf"):
        self.previews.create(self.user, SHARED_GROUP_REVIEW_ID, "web_action_preview", NOW)

def test_two_shared_leaves_write_two_v1_partitions(self) -> None:
    self.publish_leaf(THEME_SPACE_ID)
    self.publish_leaf(DESIGN_SPACE_ID)
    self.assertTrue((self.registry_root / "products" / THEME_PRODUCT_ID / "registry.json").is_file())
    self.assertTrue((self.registry_root / "products" / DESIGN_PRODUCT_ID / "registry.json").is_file())
    self.assertNotEqual(THEME_PRODUCT_ID, DESIGN_PRODUCT_ID)
```

- [ ] **Step 2: Run Review/Registry tests and verify current mapping-based ownership fails**

Run:

```bash
.venv/bin/python -m unittest tests.test_central_web_store tests.test_central_web_review tests.test_central_web_preview tests.test_central_web_publication tests.test_registry -v
```

Expected: failures because Web contracts/drafts are keyed by `product_id` and Preview still derives ownership from the current repository mapping.

- [ ] **Step 3: Key Web state by `decision_space_id` and freeze ownership in immutable records**

```python
@dataclass(frozen=True)
class ReviewDraft:
    organization_id: str
    actor_id: str
    decision_space_id: str
    version: int
    items: tuple[DraftItem, ...]
    updated_at: str | None

@dataclass(frozen=True)
class CentralReviewBatch:
    review_batch_id: str
    organization_id: str
    actor_id: str
    decision_space_id: str
    compatibility_product_id: str
    compatibility_product_name: str
    items: tuple[CentralReviewItem, ...]
    created_at: str
```

Migrate Web primary keys and immutable records from `product_id` ownership to
`decision_space_id`, while retaining the frozen compatibility ID/name needed
for Preview rendering. Verify every Draft item against
`candidate_revision_ownership`, not current repository routes.

- [ ] **Step 4: Render Preview seeds only through a leaf compatibility partition**

```python
def registry_partition(space: LeafDecisionSpace) -> tuple[str, str]:
    if space.kind not in ("product", "shared_unit") or not space.enabled:
        raise ValueError("decision_space_not_publishable")
    return space.compatibility_product_id, space.compatibility_product_name
```

`CentralPreviewService._require_latest_and_unpublished()` validates the frozen
route/space snapshot for every accepted family. `_seed()` passes only the
compatibility ID/name into the unchanged V1 `DecisionSeed`. Never write
`decision_space_id`, breadcrumb, source root, or asset type into V1 Registry
JSON.

- [ ] **Step 5: Preserve exact publication recovery while authorizing by leaf**

Publication confirmation, commit adoption, push retry, ambiguous-state stop,
and receipts keep their existing state machine. Replace
`product_repositories(product_id)` authorization with frozen
`decision_space_id` ownership, and expose the leaf identity only in private
central records and safe Web views.

```python
if publication.decision_space_id != preview.decision_space_id:
    raise WebRecordConflict("publication_decision_space_conflict")
```

- [ ] **Step 6: Re-run exact-shape, publication, and vertical tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_central_web_store tests.test_central_web_review tests.test_central_web_preview tests.test_central_web_publication tests.test_registry -v
.venv/bin/python -m unittest tests.integration.test_central_web_vertical -v
```

Expected: all tests pass; existing product Decision bytes are unchanged, two
Shared leaves publish into two distinct `prod_` partitions, and cross-leaf
Review/Preview fails before Git mutation.

- [ ] **Step 7: Commit leaf-owned Review and publication**

```bash
git add src/zdecision/central/web/contracts.py src/zdecision/central/web/schema.py src/zdecision/central/web/store.py src/zdecision/central/web/queries.py src/zdecision/central/web/reviews.py src/zdecision/central/web/previews.py src/zdecision/central/web/publications.py tests/test_central_web_store.py tests/test_central_web_review.py tests/test_central_web_preview.py tests/test_central_web_publication.py tests/test_registry.py tests/integration/test_central_web_vertical.py
git commit -m "feat: publish leaf decision spaces"
```

### Task 5: Neutral Decision-Space API and Shared Catalog Tree

**Files:**
- Modify: `src/zdecision/central/web/contracts.py`
- Modify: `src/zdecision/central/web/queries.py`
- Modify: `src/zdecision/central/web/reviews.py`
- Modify: `src/zdecision/central/web/publications.py`
- Modify: `src/zdecision/central/web/application.py`
- Modify: `src/zdecision/central/web/api.py`
- Modify: `web/src/api/types.ts`
- Modify: `web/src/app/router.tsx`
- Modify: `web/src/app/AppShell.tsx`
- Create: `web/src/features/decision-spaces/DecisionSpaceTree.tsx`
- Create: `web/src/features/decision-spaces/DecisionSpaceTree.test.tsx`
- Create: `web/src/pages/repository-entry/RepositoryEntryPage.tsx`
- Modify: `web/src/pages/company-overview/CompanyOverviewPage.tsx`
- Modify: `web/src/pages/company-overview/CompanyOverviewPage.test.tsx`
- Modify: `web/src/pages/review-index/ReviewIndexPage.tsx`
- Modify: `web/src/pages/decision-catalog/DecisionCatalogPage.tsx`
- Modify: `web/src/pages/decision-catalog/DecisionDetailPage.tsx`
- Modify: `web/src/pages/publication-history/PublicationHistoryPage.tsx`
- Modify: `web/src/pages/publication-history/PublicationDetailPage.tsx`
- Modify: `web/src/styles/app.css`
- Test: `tests/test_central_web_queries.py`
- Test: `tests/test_central_web_api.py`
- Test: `tests/test_central_web_review.py`

**Interfaces:**
- Consumes: Tasks 1 and 4 leaf catalog/query services.
- Produces: safe `DecisionSpaceRef`, `CatalogNode`, `Dashboard.shared_tree`, repository-space index, and canonical `/spaces/{decision_space_id}` Web/API routes.

```typescript
export type DecisionSpaceKind = "product" | "shared_unit";

export interface DecisionSpaceRef {
  decision_space_id: string;
  kind: DecisionSpaceKind;
  display_name: string;
  breadcrumb: string[];
  source_root: string;
  package_name: string | null;
  asset_type: string | null;
}

export interface DecisionSpaceSummary extends DecisionSpaceRef {
  repository_ids: string[];
  pending_candidate_count: number;
  active_decision_count: number | null;
  last_activity_at: string | null;
}

export interface CatalogNode {
  node_id: string;
  kind: "catalog_group" | DecisionSpaceKind;
  display_name: string;
  breadcrumb: string[];
  pending_candidate_count: number;
  active_decision_count: number | null;
  last_activity_at: string | null;
  space: DecisionSpaceSummary | null;
  children: CatalogNode[];
}
```

- [ ] **Step 1: Write failing API and Shared-tree tests**

```python
def test_dashboard_counts_products_and_nests_shared_leaves(self) -> None:
    response = self.client.get("/api/v1/web/dashboard")
    self.assertEqual(200, response.status_code)
    body = response.json()
    self.assertEqual(2, body["metrics"]["product_count"])
    self.assertEqual("Shared", body["shared_tree"]["display_name"])
    self.assertEqual(
        ["design", "theme", "zcf-audit"],
        sorted(self._leaf_names(body["shared_tree"])),
    )

def test_catalog_group_cannot_open_candidate_inbox(self) -> None:
    response = self.client.get(
        f"/api/v1/web/spaces/{SHARED_GROUP_ID}/candidates"
    )
    self.assertEqual(404, response.status_code)
    self.assertEqual("decision_space_not_leaf", response.json()["error"])
```

```tsx
it("renders Shared groups without actions and package leaves with space links", () => {
  render(<DecisionSpaceTree root={sharedTreeFixture} />);
  expect(screen.getByText("packages/shared")).toBeVisible();
  expect(screen.getByText("theme")).toBeVisible();
  expect(screen.getByRole("link", { name: "theme 候选" })).toHaveAttribute(
    "href",
    `/spaces/${THEME_SPACE_ID}/candidates`,
  );
  expect(screen.queryByRole("link", { name: "Shared 候选" })).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run focused backend and frontend tests and verify product-only routes fail**

Run from the repository root:

```bash
.venv/bin/python -m unittest tests.test_central_web_queries tests.test_central_web_api tests.test_central_web_review -v
```

Run from `web/`:

```bash
npm test -- src/pages/company-overview/CompanyOverviewPage.test.tsx src/features/decision-spaces/DecisionSpaceTree.test.tsx
```

Expected: failures because Dashboard has only `products`, shared nodes do not
exist, and the router/API exposes only `/products/{product_id}`.

- [ ] **Step 3: Add neutral Web contracts and canonical API routes**

```text
GET  /api/v1/web/repositories/{repository_id}/spaces
GET  /api/v1/web/spaces/{decision_space_id}/candidates
GET  /api/v1/web/spaces/{decision_space_id}/review-draft
PUT  /api/v1/web/spaces/{decision_space_id}/review-draft
POST /api/v1/web/spaces/{decision_space_id}/reviews
GET  /api/v1/web/spaces/{decision_space_id}/decisions
GET  /api/v1/web/spaces/{decision_space_id}/decisions/{decision_id}
GET  /api/v1/web/spaces/{decision_space_id}/publications
```

```python
def list_candidates(
    self,
    principal: Principal,
    decision_space_id: str,
    *,
    search: str = "",
    repository_id: str | None = None,
    capture_request_id: str | None = None,
    state: str = "pending",
    limit: int = 50,
    offset: int = 0,
) -> CandidateInboxView:
    return self.reviews.list_candidates(
        principal,
        decision_space_id,
        search=search,
        repository_id=repository_id,
        capture_request_id=capture_request_id,
        state=state,
        limit=limit,
        offset=offset,
    )

def get_decision(
    self, principal: Principal, decision_space_id: str, decision_id: str
) -> DecisionDetailView:
    return self.queries.get_decision(
        principal, decision_space_id, decision_id
    )
```

Resolve the neutral ID to one enabled leaf and its compatibility partition.
Return `decision_space_not_leaf` for groups. Product-only URLs may redirect only
when the target resolves to a `product` leaf; React never generates them.

- [ ] **Step 4: Render the company product list and real Shared tree**

`Dashboard.metrics.product_count` counts only `product` leaves.
`Dashboard.shared_tree` is `null` when no Shared leaf is registered; otherwise
it contains `Shared`, exact directory groups, and package leaves. Aggregate
counts are calculated from descendants; only leaves render Candidate,
Decision, and Publication links.

```tsx
export function DecisionSpaceTree({ root }: { root: CatalogNode }) {
  return (
    <ul className="space-tree">
      <DecisionSpaceNode node={root} depth={0} />
    </ul>
  );
}
```

Use semantic buttons for expand/collapse, lists for hierarchy, visible focus,
and the complete breadcrumb/source root on each leaf detail row.

- [ ] **Step 5: Stop repository deep links from guessing a product**

Move `RepositoryEntryPage` out of `CandidateReviewPage.tsx`. A URL containing
only `?repository_id=...` loads
`/api/v1/web/repositories/{repository_id}/spaces` and routes to
`/reviews?repository_id=...`, where product and Shared leaves are grouped. It
opens a leaf directly only when `decision_space_id` is already present and was
returned by the server.

- [ ] **Step 6: Run neutral route, tree, and type tests**

Run from the repository root:

```bash
.venv/bin/python -m unittest tests.test_central_web_queries tests.test_central_web_api tests.test_central_web_review -v
```

Run from `web/`:

```bash
npm test -- src/pages/company-overview/CompanyOverviewPage.test.tsx src/pages/review-index/ReviewIndexPage.test.tsx src/features/decision-spaces/DecisionSpaceTree.test.tsx
npm run typecheck
```

Expected: all listed tests pass; Shared groups have no action URLs, leaves use
different canonical `/spaces/{decision_space_id}` URLs, and repository-only
links do not redirect to an arbitrary product.

- [ ] **Step 7: Commit the neutral catalog UI/API**

```bash
git add src/zdecision/central/web/contracts.py src/zdecision/central/web/queries.py src/zdecision/central/web/reviews.py src/zdecision/central/web/publications.py src/zdecision/central/web/application.py src/zdecision/central/web/api.py web/src/api/types.ts web/src/app/router.tsx web/src/app/AppShell.tsx web/src/features/decision-spaces/DecisionSpaceTree.tsx web/src/features/decision-spaces/DecisionSpaceTree.test.tsx web/src/pages/repository-entry/RepositoryEntryPage.tsx web/src/pages/company-overview/CompanyOverviewPage.tsx web/src/pages/company-overview/CompanyOverviewPage.test.tsx web/src/pages/review-index/ReviewIndexPage.tsx web/src/pages/decision-catalog/DecisionCatalogPage.tsx web/src/pages/decision-catalog/DecisionDetailPage.tsx web/src/pages/publication-history/PublicationHistoryPage.tsx web/src/pages/publication-history/PublicationDetailPage.tsx web/src/styles/app.css tests/test_central_web_queries.py tests/test_central_web_api.py tests/test_central_web_review.py
git commit -m "feat: browse decision spaces and shared packages"
```

### Task 6: Compact Leaf Candidate List and Batch Review

**Files:**
- Create: `web/src/features/reviews/CandidateReviewRow.tsx`
- Create: `web/src/features/reviews/CandidateReviewRow.test.tsx`
- Modify: `web/src/pages/candidate-review/CandidateReviewPage.tsx`
- Modify: `web/src/pages/candidate-review/CandidateReviewPage.test.tsx`
- Modify: `web/src/api/types.ts`
- Modify: `web/src/styles/app.css`
- Delete: `web/src/features/reviews/ReviewEditor.tsx`

**Interfaces:**
- Consumes: Task 5 `DecisionSpaceRef`, leaf-scoped Candidate Inbox, and existing 20-item Review endpoint.
- Produces: compact accessible row, transient selection, direct actions, one-level batch undo, and exact classified-subset submission.

```typescript
interface CandidateReviewRowProps {
  item: CandidateInboxItem;
  space: DecisionSpaceRef;
  action: ReviewDraftItem | undefined;
  selected: boolean;
  stale: boolean;
  onSelectedChange(familyId: string, selected: boolean): void;
  onDirectAction(familyId: string, action: "accept" | "reject"): void;
  onEditAccept(familyId: string, content: CandidateContent): void;
  onLoadLatest?(): void;
}

interface BatchUndo {
  message: string;
  previousByFamily: Map<string, ReviewDraftItem | undefined>;
}
```

- [ ] **Step 1: Write failing row, batch, undo, and limit tests**

```tsx
it("does not treat Checkbox selection as acceptance", async () => {
  const user = userEvent.setup();
  renderCandidatePage(threeCandidateInbox);
  await user.click(screen.getByRole("checkbox", { name: /选择决策 A/ }));
  expect(screen.getByText("已选 1 条")).toBeVisible();
  expect(
    within(screen.getByRole("article", { name: "候选 决策 A" }))
      .getByText("未处理"),
  ).toBeVisible();
});

it("batch accepts exactly selected rows and undo restores mixed actions", async () => {
  const user = userEvent.setup();
  renderCandidatePage(mixedDraftInbox);
  await user.click(screen.getByRole("checkbox", { name: /选择决策 A/ }));
  await user.click(screen.getByRole("checkbox", { name: /选择决策 C/ }));
  await user.click(screen.getByRole("button", { name: "批量接受" }));
  expect(within(screen.getByRole("article", { name: "候选 决策 A" })).getByText("已接受")).toBeVisible();
  expect(within(screen.getByRole("article", { name: "候选 决策 C" })).getByText("已接受")).toBeVisible();
  expect(within(screen.getByRole("article", { name: "候选 决策 B" })).getByText("已拒绝")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "撤销" }));
  expect(within(screen.getByRole("article", { name: "候选 决策 A" })).getByText("未处理")).toBeVisible();
  expect(within(screen.getByRole("article", { name: "候选 决策 C" })).getByText("编辑后接受")).toBeVisible();
});

it("blocks a 21-row batch instead of truncating it", async () => {
  const user = userEvent.setup();
  renderCandidatePage(twentyOneCandidateInbox);
  await user.click(screen.getByRole("checkbox", { name: "选择当前页 21 条" }));
  expect(screen.getByText("单次最多审核 20 条")).toBeVisible();
  expect(screen.getByRole("button", { name: "批量接受" })).toBeDisabled();
  expect(screen.getAllByText("未处理")).toHaveLength(21);
});

it("submits only explicitly classified current revisions", async () => {
  const user = userEvent.setup();
  const requests = renderCandidatePage(threeCandidateInbox);
  await user.click(screen.getByRole("button", { name: "接受决策 A" }));
  await user.click(screen.getByRole("button", { name: "拒绝决策 B" }));
  await user.click(screen.getByRole("button", { name: "提交审核" }));
  await waitFor(() => expect(requests.reviewPosts).toHaveLength(1));
  expect(requests.reviewPosts[0].items.map((item) => item.family_id)).toEqual([
    FAMILY_A,
    FAMILY_B,
  ]);
  expect(requests.previewPosts).toHaveLength(0);
});
```

In the same test file, implement `renderCandidatePage()` with the test router
and a mocked Web API that returns captured `reviewPosts` and `previewPosts`.
Define the three Inbox fixtures in the test file with stable family IDs and
current revision digests; do not depend on manual browser state or an external
server.

- [ ] **Step 2: Run Candidate tests and verify the card/select UI fails**

Run from `web/`:

```bash
npm test -- src/pages/candidate-review/CandidateReviewPage.test.tsx src/features/reviews/CandidateReviewRow.test.tsx
```

Expected: failures because there is no row Checkbox, batch toolbar, undo, or
direct action and `ReviewEditor` still renders a select.

- [ ] **Step 3: Implement compact rows and progressive evidence disclosure**

The list header renders a select-current-page Checkbox whose accessible name
includes the visible row count; it changes only transient selection. Each row
renders, in order: Checkbox, claim, short action/scope, leaf
breadcrumb/path, current draft state, and direct `接受`/`拒绝`/`编辑` buttons.
Give each row the accessible name `候选 {claim}` and each direct action the
accessible name `{动作}{claim}` so identical visible button labels remain
unambiguous to keyboard and assistive-technology users.
Revision ID, digest, repository, Capture Request IDs, and full invalidation
conditions stay inside a native `<details>` labelled `查看证据`. Render all text
as React text nodes; never use `dangerouslySetInnerHTML`.

`编辑` opens only that row's edit panel. Decision space and repository remain
read-only. Do not render the old Review-action `<select>` or a primary Skip
command.

- [ ] **Step 4: Add transient selection, exact batch actions, and one-step undo**

```typescript
const [selectedFamilyIds, setSelectedFamilyIds] = useState<Set<string>>(
  () => new Set(),
);
const [lastBatchUndo, setLastBatchUndo] = useState<BatchUndo | null>(null);

function applyBatch(action: "accept" | "reject") {
  const previousByFamily = new Map<string, ReviewDraftItem | undefined>();
  for (const familyId of selectedFamilyIds) {
    previousByFamily.set(familyId, draftByFamily.get(familyId));
  }
  setLastBatchUndo({
    message: `已将 ${selectedFamilyIds.size} 条标记为${action === "accept" ? "接受" : "拒绝"}`,
    previousByFamily,
  });
  replaceSelectedDraftActions(action);
  setSelectedFamilyIds(new Set());
}
```

Clear selection on leaf change or material filter change, but preserve the
draft map. Undo restores each touched family to its exact prior value. Starting
another batch action replaces the previous undo snapshot.

- [ ] **Step 5: Enforce leaf/stale/20-item submission boundaries and persistent counts**

The sticky summary shows accepted, rejected, unprocessed, and stale counts.
Submit only explicitly classified current revisions, never selected-only rows
or stale rows. If classifications would exceed 20, disable the new action and
explain the limit before state changes. POST only to
`/api/v1/web/spaces/{decision_space_id}/reviews`; Preview creation remains a
separate request after Review succeeds.

- [ ] **Step 6: Run Candidate interaction, type, and build tests**

Run from `web/`:

```bash
npm test -- src/pages/candidate-review/CandidateReviewPage.test.tsx src/features/reviews/CandidateReviewRow.test.tsx
npm run typecheck
npm run build
```

Expected: all tests pass; no Review select is rendered, Checkbox does not
classify, batch undo is exact, evidence is initially hidden, stale revisions
cannot submit, and Vite writes the central static bundle.

- [ ] **Step 7: Remove the old editor and commit the batch Review UI**

```bash
git rm web/src/features/reviews/ReviewEditor.tsx
git add web/src/features/reviews/CandidateReviewRow.tsx web/src/features/reviews/CandidateReviewRow.test.tsx web/src/pages/candidate-review/CandidateReviewPage.tsx web/src/pages/candidate-review/CandidateReviewPage.test.tsx web/src/api/types.ts web/src/styles/app.css src/zdecision/central/static
git commit -m "feat: add batch candidate review"
```

### Task 7: Real Monorepo Vertical Acceptance, Documentation, and Stop

**Files:**
- Modify: `tests/integration/test_on_demand_capture_core.py`
- Modify: `tests/integration/test_inline_candidate_refresh.py`
- Modify: `tests/integration/test_central_web_vertical.py`
- Modify: `tests/test_skill_contract.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/demo-central-web.md`
- Modify: `src/zdecision/central/static/` through `npm run build`

**Interfaces:**
- Consumes: Tasks 1–6 complete data flow.
- Produces: one reproducible technical Demo proving repository registration, trusted multi-leaf Capture, Shared-tree Review, V1 publication, restart recovery, and privacy boundaries.

- [ ] **Step 1: Add the end-to-end monorepo acceptance fixture**

```python
def test_one_update_routes_cloud_zns_audit_and_theme_without_generic_shared(self) -> None:
    for relative_path, content in (
        ("packages/products/cloud/apps/core-shell/src/app.tsx", "cloud change\n"),
        ("packages/products/zns/src/app.tsx", "zns change\n"),
        ("packages/products/shared/zcf-audit/src/App.tsx", "audit change\n"),
        ("packages/shared/theme/src/index.ts", "theme change\n"),
    ):
        target = self.repository / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    group = self.click_all_valid_sessions_update()
    self.run_agent_until_idle()

    slices = self.central.capture_slices(group.request_id)
    self.assertEqual(1, self.central.capture_group_count(group.client_action_id))
    self.assertEqual(
        {CLOUD_SPACE_ID, ZNS_SPACE_ID, ZCF_AUDIT_SPACE_ID, THEME_SPACE_ID},
        {item.decision_space_id for item in slices},
    )
    self.assertNotIn("Shared", {item.display_name for item in slices})
    self.assert_central_has_no_raw_session_or_source()
```

Add a restart case after the first slice receipt and assert that the first
batch is not uploaded twice. Complete leaf Review and publish `theme`; assert
the formal file is under the theme compatibility partition and the Shared root
has no Registry entry.

- [ ] **Step 2: Run the three integrations and establish the vertical baseline**

Run:

```bash
.venv/bin/python -m unittest tests.integration.test_on_demand_capture_core tests.integration.test_inline_candidate_refresh tests.integration.test_central_web_vertical -v
```

Expected after Tasks 1–6: the multi-slice, restart, Web navigation, and privacy
assertions pass. A failure is blocking and must be fixed at the responsible
earlier task; do not weaken an assertion. If the only failure is that the
committed Demo/Skill fixture has not yet listed a real route, complete Step 3
and rerun this exact command.

- [ ] **Step 3: Wire the Demo config and Skill contract to the real package tree**

The committed Demo fixture registers these real `packages/products/*` product
roots as independent product leaves:

```text
cloud
idp
lifecycle
portal
redis
third-party-services
zcf-installer
ziam
zmetis
zns
zstack-ai-studio
zstone
zsv
```

It also registers these selected concrete Shared leaves for the first Demo:

```text
packages/products/shared/zcf-audit
packages/products/shared/zcf-license
packages/shared/design-x
packages/shared/theme
packages/design
packages/form
packages/table
packages/hooks
packages/auth
packages/i18n
packages/utils
packages/zephyr
```

The installed Skill keeps the same two Update scope buttons. It never asks the
user to choose a product or Shared package; routing occurs after the repository
and Session authorization gates from frozen local Git evidence.

Re-run:

```bash
.venv/bin/python -m unittest tests.integration.test_on_demand_capture_core tests.integration.test_inline_candidate_refresh tests.integration.test_central_web_vertical tests.test_skill_contract -v
```

Expected: all listed tests pass with the committed Demo catalog and neutral
two-button Skill contract.

- [ ] **Step 4: Update architecture and operator documentation to match implemented behavior**

Document:

```text
Update action -> Capture group -> trusted Git route plan -> leaf slices
leaf slice -> local extraction/reconciliation -> frozen Candidate ownership
leaf Candidate Inbox -> Review -> Preview -> explicit publish -> V1 partition
```

Replace all one-repository/one-product examples, show `Shared` as a tree whose
groups are non-publishable, document canonical `/spaces` URLs, and retain the
explicit privacy boundary. Do not document commands or fields that the tests
do not exercise.

- [ ] **Step 5: Run the complete backend and frontend suites once**

Run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests
```

Run from `web/`:

```bash
npm test
npm run typecheck
npm run build
```

Expected: every command exits 0. Record the exact Python test count, Vitest
test/file count, and build result in the implementation handoff.

- [ ] **Step 6: Perform the bounded real `zstack-ui-next` smoke acceptance**

With the local central service and Agent running from the committed Demo
configuration:

1. Open the registered `zstack-ui-next` repository in Codex.
2. Click `所有有效 Session` once after tracked changes touch at least one
   product and two Shared leaves.
3. Verify one Capture group reaches terminal success and the Central Web shows
   separate leaf Inboxes.
4. Verify `Shared` expands to exact directory/package leaves and has no Review
   or Publish control of its own.
5. Select several Candidates inside one leaf, batch Accept/Reject, Undo once,
   submit the classified subset, and verify publication still requires the
   separate explicit action.
6. Inspect central persistence/log fixtures for absence of raw Session,
   Prompt, diff, and source content.

If this smoke test exposes a blocking defect, fix only that defect and rerun
the focused test plus this smoke. Do not initiate a new architecture audit.

- [ ] **Step 7: Commit acceptance/docs, report evidence, and stop**

```bash
git add tests/integration/test_on_demand_capture_core.py tests/integration/test_inline_candidate_refresh.py tests/integration/test_central_web_vertical.py tests/test_skill_contract.py README.md docs/architecture.md docs/demo-central-web.md src/zdecision/central/static
git commit -m "test: prove monorepo decision workflow"
```

Handoff must report:

- the seven commit SHAs;
- focused and full-suite results;
- the real smoke result and exact leaf names observed;
- whether the working tree is clean;
- whether local `main` is ahead of `origin/main`; and
- remaining non-blocking risks, limited to the explicitly excluded scope.

Do not push unless the user explicitly asks.
