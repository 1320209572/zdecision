# Central Decision Web Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the approved company-level ZDecision Web application from current synchronized Candidate revisions through product-isolated Review, exact preview, crash-safe Git publication, and read-only Decision/history views.

**Architecture:** Keep the existing Capture/Agent channel unchanged and add a separate central-Web application layer over the same SQLite connection. A dedicated React/TypeScript application calls transport-only `/api/v1/web` routes; central services derive identity and product ownership, persist Review/publication state, and reuse the existing V1 Registry renderer and Git recovery adapter. Git remains the formal Decision source of truth, while SQLite owns drafts, immutable Review evidence, immutable previews, publication state, and Candidate-to-Decision receipts.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, SQLite, existing ZDecision Registry/Git modules, React 19.2.8, React Router 7.18.2, TypeScript 7.0.2, Vite 8.2.0, Vitest 4.1.10, Testing Library 16.3.2, plain CSS design tokens.

## Global Constraints

- Work directly on the existing `main` branch; do not create a worktree, feature branch, or Registry branch.
- Preserve every Packet 1 repository, Capture Request, Agent lease, Plugin action, and Candidate synchronization API.
- The Demo binds only to loopback and derives organization and actor from the fixed trusted browser principal.
- Raw Sessions, Prompts, tool output, code, diffs, credentials, local paths, and native Session/Turn IDs never enter central persistence or HTTP fixtures.
- Product identity always uses `prod_<32-hex>`; human product names are display data and never filesystem path components.
- One Review, preview, publication, and Git commit belongs to exactly one product.
- Formal Decision files remain `zdecision-decision/v1`, `schema_version: 1`, `revision: 1`, and `lifecycle: active`.
- Formal files remain under `decision-registry/products/<product_id>/...`; no formal Decision is stored in SQLite.
- Candidate, Review, and Decision text is untrusted text and must never be rendered as HTML.
- The browser cannot set organization, actor, authoritative product name, Registry path, commit message, or formal Decision bytes.
- Publication starts only from an immutable preview and is successful only after the exact commit is proven on `origin/main`.
- Company SSO, Git Developer-role authorization, multiple organizations, Decision update/supersede/retire, comments, notifications, administration, analytics, production deployment, and Packet 3 recall are excluded.
- Stop after the seven approved acceptance gates pass. Do not add deferred scope and do not start a new wide review cycle.

## File Map

### Backend

- `src/zdecision/ids.py` — add stable Central-Web Review/publication IDs and the deterministic `family_id` to V1 Candidate-ID adapter.
- `src/zdecision/central/store.py` — keep ownership of the SQLite connection and call the Web schema initializer.
- `src/zdecision/central/service.py` — record the safe Capture Request-to-Candidate revision association during the existing atomic batch acceptance.
- `src/zdecision/central/web/contracts.py` — immutable Web draft, Review, preview, publication, and response values with strict JSON round trips.
- `src/zdecision/central/web/schema.py` — create product-owned Web tables and indexes without changing Packet 1 tables.
- `src/zdecision/central/web/store.py` — canonical-JSON persistence, CAS, action replay, immutable-record checks, and monotonic publication updates.
- `src/zdecision/central/web/queries.py` — enabled-product, current-Candidate, dashboard, Decision, and history read models.
- `src/zdecision/central/web/reviews.py` — draft validation and all-or-nothing immutable Review submission.
- `src/zdecision/central/web/previews.py` — accepted-item conversion to exact V1 Registry preview and staleness checks.
- `src/zdecision/central/web/publications.py` — confirmation, exact commit adoption, push recovery, receipts, and safe resume.
- `src/zdecision/central/web/application.py` — dependency facade consumed by the HTTP router.
- `src/zdecision/central/web/api.py` — strict `/api/v1/web` request bodies, bounded query parameters, status mapping, and response serialization.
- `src/zdecision/registry/query.py` — product-owned, commit-bound formal Registry reader.
- `src/zdecision/central/api.py` — include the Web router and serve the built SPA without changing Agent routes.
- `src/zdecision/central/cli.py` — construct the Web services with an explicit Registry repository root.
- `pyproject.toml` — package the Vite HTML and asset directory.

### Frontend

- `web/package.json`, `web/package-lock.json`, `web/tsconfig.json`, `web/vite.config.ts`, `web/index.html` — isolated reproducible React build.
- `web/src/main.tsx`, `web/src/app/router.tsx`, `web/src/app/AppShell.tsx` — browser entry, route table, global navigation, and product context.
- `web/src/api/client.ts`, `web/src/api/types.ts` — one strict fetch boundary and server response types.
- `web/src/assets/zstack-logo.svg` — reviewed white ZStack vector from `zstack-ui-next/packages/products/cloud/bff/public/theme/default/zh-CN/logo.svg`.
- `web/src/styles/tokens.css`, `web/src/styles/app.css` — dark rail, light surfaces, cobalt action, typography, spacing, states, and responsive layout.
- `web/src/pages/company-overview/CompanyOverviewPage.tsx` — company metrics, enabled products, and recent publication feed.
- `web/src/pages/review-index/ReviewIndexPage.tsx` — cross-product pending-work grouping only; no cross-product mutation.
- `web/src/pages/candidate-review/CandidateReviewPage.tsx` — refresh control, current heads, draft actions, partial submit, and stale reconciliation.
- `web/src/pages/publication-preview/PublicationPreviewPage.tsx` — exact readable/JSON preview and the only publish control.
- `web/src/pages/decision-catalog/DecisionCatalogPage.tsx`, `DecisionDetailPage.tsx` — global/product read-only formal Decisions.
- `web/src/pages/publication-history/PublicationHistoryPage.tsx`, `PublicationDetailPage.tsx` — global/product durable publication states and recovery.
- `web/src/features/candidate-refresh/useCandidateRefresh.ts` — existing `all_valid_sessions` request, event cursor, restart restoration, and Candidate reload.
- `web/src/features/reviews/ReviewEditor.tsx` — `accept`, `edit_accept`, `reject`, and `skip` controls with inert text rendering.
- `web/src/shared/AsyncState.tsx`, `web/src/shared/StatusBadge.tsx` — explicit loading, empty, unavailable, stale, pending-push, and ambiguous states.
- `web/src/test/setup.ts` plus colocated `*.test.tsx` files — Vitest/Testing Library coverage.
- `src/zdecision/central/static/` — generated Vite output committed for Python packaging; never hand-edit these files.

### Tests and docs

- `tests/test_central_web_store.py` — schema ownership, canonical records, CAS, replay, and restart.
- `tests/test_central_web_queries.py` — product isolation, counts, Registry availability, and deep-link resolution.
- `tests/test_central_web_review.py` — four actions, partial Review, staleness, cross-product rejection, and idempotency.
- `tests/test_central_web_preview.py` — exact bytes, accepted subset, no write, and stale bases.
- `tests/test_central_web_publication.py` — state machine, crash points, adoption, push retry, ambiguity, and receipts.
- `tests/test_central_web_api.py` — strict HTTP bodies, safe errors, identity derivation, bounded filters, and SPA fallback.
- `tests/integration/test_central_web_vertical.py` — one real temporary-Git end-to-end publication path.
- `tests/test_update_candidates_page.py` — replace obsolete single-HTML assertions with SPA/build and Capture-control compatibility assertions.
- `docs/demo-central-web.md` — exact Demo startup, browser flow, restart checks, and stopping rule.

## Acceptance Traceability

| Approved gate | Implemented and proven by |
| --- | --- |
| Gate 1 — shell, products, repository routing, durable refresh | Tasks 2 and 3 |
| Gate 2 — product-isolated four-action partial Review | Tasks 3 and 4 |
| Gate 3 — exact immutable preview and staleness | Task 5 |
| Gate 4 — one-click publication, retry, crash adoption, ambiguity | Task 6 |
| Gate 5 — product/global Decision and publication views | Tasks 6 and 7 |
| Gate 6 — authority, privacy, inert text, rejected-content boundaries | Tasks 3–8, consolidated in Task 8 |
| Gate 7 — real Codex-card-to-Git functional Demo with restarts | Task 8 |

---

### Task 1: Central-Web identities, contracts, and durable schema

**Files:**
- Modify: `src/zdecision/ids.py`
- Modify: `src/zdecision/central/store.py`
- Modify: `src/zdecision/central/service.py`
- Create: `src/zdecision/central/web/__init__.py`
- Create: `src/zdecision/central/web/contracts.py`
- Create: `src/zdecision/central/web/schema.py`
- Create: `src/zdecision/central/web/store.py`
- Test: `tests/test_central_web_store.py`

**Interfaces:**
- Consumes: `CandidateContent`, `ApprovalRef`, `PublicationRecord`, `CandidatePublicationReceipt`, `canonical_json_bytes`, and `CentralStore.connection`.
- Produces: `publication_candidate_id(family_id: str) -> str`, `central_review_batch_id(organization_id, actor_id, product_id_value, client_action_id, ordered_items) -> str`, `central_publication_id(preview_id: str) -> str`, `ReviewDraft`, `CentralReviewBatch`, `CentralPublication`, and `CentralWebStore` methods used by every later task.

- [ ] **Step 1: Write failing identity and canonical-persistence tests**

```python
class CentralWebStoreTest(unittest.TestCase):
    def test_family_maps_deterministically_to_v1_candidate(self) -> None:
        self.assertEqual(
            "cand_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_01",
            publication_candidate_id("cfm_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        )

    def test_draft_compare_and_swap_survives_reopen(self) -> None:
        empty = self.web_store.get_draft("org_demo", "user_demo", PRODUCT_ID)
        saved = self.web_store.replace_draft(empty, (self.draft_item(),), NOW)
        self.assertEqual(1, saved.version)
        self.store.close()
        self.store = CentralStore.open(self.database_path)
        self.web_store = CentralWebStore(self.store.connection)
        self.assertEqual(saved, self.web_store.get_draft("org_demo", "user_demo", PRODUCT_ID))

    def test_action_replay_rejects_different_request_digest(self) -> None:
        self.web_store.record_action(
            "org_demo", "user_demo", "review", "web_action_1", "a" * 64,
            "rvb_" + "1" * 32, NOW,
        )
        with self.assertRaises(WebActionConflict):
            self.web_store.record_action(
                "org_demo", "user_demo", "review", "web_action_1", "b" * 64,
                "rvb_" + "2" * 32, NOW,
            )
```

- [ ] **Step 2: Run the focused test and verify the missing modules fail**

Run: `python -m unittest tests.test_central_web_store -v`

Expected: `ModuleNotFoundError: No module named 'zdecision.central.web'`.

- [ ] **Step 3: Add exact stable-ID adapters**

```python
_WEB_ACTION_ID = re.compile(r"^web_action_[A-Za-z0-9-]{1,96}$")
_PREVIEW_ID = re.compile(r"^pub_[0-9a-f]{32}$")

def publication_candidate_id(family_id: str) -> str:
    if not isinstance(family_id, str) or _CANDIDATE_FAMILY_ID.fullmatch(family_id) is None:
        raise ValueError("family_id is invalid")
    return f"cand_{family_id.removeprefix('cfm_')}_01"

def central_review_batch_id(
    organization_id: str,
    actor_id: str,
    product_id_value: str,
    client_action_id: str,
    ordered_items: Sequence[Mapping[str, object]],
) -> str:
    # Validate all five inputs, preserve item order, and hash canonical JSON.
    return _stable_id("rvb", {
        "actor_id": actor_id,
        "client_action_id": client_action_id,
        "items": list(ordered_items),
        "organization_id": organization_id,
        "product_id": product_id_value,
    })

def central_publication_id(preview_id: str) -> str:
    if not isinstance(preview_id, str) or _PREVIEW_ID.fullmatch(preview_id) is None:
        raise ValueError("preview_id is invalid")
    return _stable_id("plb", {"preview_id": preview_id})
```

`central_review_batch_id` must reject duplicate families, invalid actions, invalid revisions/digests, non-`prod_` product IDs, and action IDs outside the shown `web_action_` pattern before hashing.

- [ ] **Step 4: Define strict immutable Web records**

```python
ReviewAction = Literal["accept", "edit_accept", "reject", "skip"]
PublicationState = Literal[
    "confirmed", "committed_pending_push", "completed"
]

@dataclass(frozen=True)
class DraftItem:
    family_id: str
    repository_id: str
    revision_id: str
    revision: int
    content_digest: str
    action: ReviewAction
    effective_content: CandidateContent | None
    note: str | None = None

@dataclass(frozen=True)
class ReviewDraft:
    organization_id: str
    actor_id: str
    product_id: str
    version: int
    items: tuple[DraftItem, ...]
    updated_at: str | None

@dataclass(frozen=True)
class CentralReviewItem:
    review_id: str
    family_id: str
    publication_candidate_id: str
    repository_id: str
    revision_id: str
    revision: int
    content_digest: str
    action: ReviewAction
    effective_content: CandidateContent | None
    note: str | None

@dataclass(frozen=True)
class CentralReviewBatch:
    review_batch_id: str
    organization_id: str
    actor_id: str
    product_id: str
    product_name: str
    client_action_id: str
    request_digest: str
    approval: ApprovalRef
    items: tuple[CentralReviewItem, ...]
    created_at: str

@dataclass(frozen=True)
class CentralPublication:
    publication_id: str
    organization_id: str
    actor_id: str
    product_id: str
    preview_id: str
    confirm_action_id: str
    confirm_request_digest: str
    state: PublicationState
    approval: ApprovalRef
    commit_sha: str | None
    recovery_code: str | None
    created_at: str
    updated_at: str

@dataclass(frozen=True)
class ActionResult:
    organization_id: str
    actor_id: str
    action_kind: Literal["review", "preview", "publish", "resume"]
    client_action_id: str
    request_digest: str
    result_id: str
    created_at: str
```

Every record must implement exact-field `to_dict()`/`from_dict()` methods, validate its ID prefixes, normalize tuples, limit Review batches to 1–20 items, and validate state-dependent `commit_sha` shape.

- [ ] **Step 5: Create the product-owned SQLite schema**

```sql
CREATE TABLE IF NOT EXISTS web_review_drafts (
  organization_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version >= 0),
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, actor_id, product_id)
);

CREATE TABLE IF NOT EXISTS web_candidate_revision_batches (
  organization_id TEXT NOT NULL,
  repository_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, repository_id, family_id, revision_id, request_id),
  FOREIGN KEY(request_id) REFERENCES capture_requests(request_id)
);

CREATE TABLE IF NOT EXISTS web_review_batches (
  organization_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  review_batch_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  client_action_id TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, product_id, review_batch_id),
  UNIQUE(organization_id, actor_id, client_action_id)
);

CREATE TABLE IF NOT EXISTS web_review_items (
  organization_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  review_batch_id TEXT NOT NULL,
  item_order INTEGER NOT NULL CHECK(item_order >= 0),
  review_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  publication_candidate_id TEXT NOT NULL,
  repository_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision > 0),
  content_digest TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('accept','edit_accept','reject','skip')),
  effective_content_json TEXT,
  effective_content_digest TEXT,
  note TEXT,
  PRIMARY KEY(organization_id, product_id, review_batch_id, item_order),
  UNIQUE(organization_id, product_id, review_id),
  FOREIGN KEY(organization_id, product_id, review_batch_id)
    REFERENCES web_review_batches(organization_id, product_id, review_batch_id)
);

CREATE TABLE IF NOT EXISTS web_action_results (
  organization_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  action_kind TEXT NOT NULL CHECK(action_kind IN ('review','preview','publish','resume')),
  client_action_id TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  result_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, actor_id, action_kind, client_action_id)
);

CREATE TABLE IF NOT EXISTS web_publication_previews (
  organization_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  preview_id TEXT NOT NULL,
  review_batch_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, product_id, preview_id),
  FOREIGN KEY(organization_id, product_id, review_batch_id)
    REFERENCES web_review_batches(organization_id, product_id, review_batch_id)
);

CREATE TABLE IF NOT EXISTS web_publications (
  organization_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  publication_id TEXT NOT NULL,
  preview_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('confirmed','committed_pending_push','completed')),
  recovery_code TEXT,
  commit_sha TEXT,
  record_json TEXT NOT NULL,
  record_digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, product_id, publication_id),
  UNIQUE(organization_id, product_id, preview_id),
  FOREIGN KEY(organization_id, product_id, preview_id)
    REFERENCES web_publication_previews(organization_id, product_id, preview_id)
);

CREATE TABLE IF NOT EXISTS web_publication_families (
  organization_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  publication_id TEXT NOT NULL,
  PRIMARY KEY(organization_id, product_id, family_id),
  FOREIGN KEY(organization_id, product_id, publication_id)
    REFERENCES web_publications(organization_id, product_id, publication_id)
);

CREATE TABLE IF NOT EXISTS web_candidate_receipts (
  organization_id TEXT NOT NULL,
  product_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  publication_candidate_id TEXT NOT NULL,
  decision_id TEXT NOT NULL,
  preview_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(organization_id, product_id, family_id),
  UNIQUE(organization_id, product_id, decision_id)
);
```

Add indexes for Candidate batch filtering by `(organization_id, request_id, revision_id)`, Review batches by `(organization_id, product_id, created_at, review_batch_id)`, Review-family lookup by `(organization_id, product_id, family_id, review_batch_id)`, publication history by `(organization_id, product_id, created_at)`, and receipt lookup by Decision ID. Latest-Review queries join items to batches and order by batch `created_at DESC, review_batch_id DESC`; `item_order` is only order within one batch.

- [ ] **Step 6: Implement canonical store operations and CAS**

`CentralWebStore` exposes these exact methods:

```text
get_draft(organization_id, actor_id, product_id) -> ReviewDraft
replace_draft(expected, items, now) -> ReviewDraft
put_review_batch(batch) -> CentralReviewBatch
get_review_batch(organization_id, product_id, review_batch_id) -> CentralReviewBatch | None
put_preview(organization_id, product_id, record) -> PublicationRecord
get_preview(organization_id, preview_id) -> PublicationRecord | None
put_publication(publication) -> CentralPublication
claim_publication_families(publication, family_ids) -> None
replace_publication(expected, replacement) -> CentralPublication
put_family_receipts(publication, preview, commit_sha) -> None
action_result(organization_id, actor_id, action_kind, client_action_id) -> ActionResult | None
record_action(organization_id, actor_id, action_kind, client_action_id, request_digest, result_id, now) -> str
```

All JSON writes use `canonical_json_bytes`; all reads verify the stored SHA-256 digest and canonical encoding. `replace_draft` uses `BEGIN IMMEDIATE` plus `WHERE version = ?`; `replace_publication` accepts only `confirmed -> committed_pending_push -> completed` and same-state exact replay.

- [ ] **Step 7: Initialize the Web schema from `CentralStore.open`**

```python
with connection:
    connection.executescript(PACKET_1_SCHEMA)
    _migrate_capture_requests(connection)
    initialize_web_schema(connection)
```

Do not alter or rename any Packet 1 table, index, or column.

In `CaptureRequestService.accept_candidate_batch`, pass the current `batch.request_id` and server timestamp beside every accepted revision and insert `web_candidate_revision_batches` in the same transaction as the head update. On startup, idempotently backfill older rows by validating each stored canonical `candidate_batches.batch_json` with `CandidateBatchUpload.from_dict` and using `acknowledged_at`; malformed historical rows fail startup as `central_candidate_state_corrupt` rather than guessing.

- [ ] **Step 8: Run focused and existing persistence tests**

Run: `python -m unittest tests.test_central_web_store tests.test_central_requests -v`

Expected: all tests pass; reopening the SQLite file preserves exact drafts and immutable records.

- [ ] **Step 9: Commit Task 1**

```bash
git add src/zdecision/ids.py src/zdecision/central/store.py src/zdecision/central/service.py src/zdecision/central/web tests/test_central_web_store.py
git commit -m "feat: add central web persistence contracts"
```

### Task 2: Commit-bound product queries, browser API shell, and React application shell

**Files:**
- Create: `src/zdecision/registry/query.py`
- Create: `src/zdecision/central/web/queries.py`
- Create: `src/zdecision/central/web/application.py`
- Create: `src/zdecision/central/web/api.py`
- Modify: `src/zdecision/central/api.py`
- Modify: `src/zdecision/central/cli.py`
- Modify: `pyproject.toml`
- Create: `web/package.json`
- Create: `web/package-lock.json`
- Create: `web/tsconfig.json`
- Create: `web/vite.config.ts`
- Create: `web/index.html`
- Create: `web/src/main.tsx`
- Create: `web/src/app/router.tsx`
- Create: `web/src/app/AppShell.tsx`
- Create: `web/src/api/client.ts`
- Create: `web/src/api/types.ts`
- Create: `web/src/assets/zstack-logo.svg`
- Create: `web/src/styles/tokens.css`
- Create: `web/src/styles/app.css`
- Create: `web/src/pages/company-overview/CompanyOverviewPage.tsx`
- Create: `web/src/pages/review-index/ReviewIndexPage.tsx`
- Create: `web/src/shared/AsyncState.tsx`
- Create: `web/src/shared/StatusBadge.tsx`
- Create: `web/src/test/setup.ts`
- Test: `tests/test_central_web_queries.py`
- Test: `tests/test_central_web_api.py`
- Test: `web/src/pages/company-overview/CompanyOverviewPage.test.tsx`

**Interfaces:**
- Consumes: `CentralStore`, `CentralWebStore`, `DemoIdentityProvider.browser_principal()`, `GitRegistryAdapter.fetch_and_require_exact_main()`, and strict Registry V1 value classes.
- Produces: `RegistryQuery.snapshot()`, `CentralWebQueries.dashboard(principal)`, `CentralWebApplication`, `GET /api/v1/web/dashboard`, built SPA fallback, and stable frontend `api<T>()`.

- [ ] **Step 1: Write failing product-isolation and dashboard tests**

```python
def test_dashboard_derives_products_and_counts_from_owned_sources(self) -> None:
    dashboard = self.queries.dashboard(self.user)
    self.assertEqual([PRODUCT_ID], [item.product_id for item in dashboard.products])
    self.assertEqual(1, dashboard.metrics.product_count)
    self.assertEqual(1, dashboard.metrics.pending_candidate_count)
    self.assertEqual("available", dashboard.registry.state)

def test_unknown_repository_has_no_product_route(self) -> None:
    self.assertIsNone(self.queries.resolve_repository(self.user, "repo_" + "f" * 32))
```

Add a second-organization fixture and assert it never contributes products, Candidate counts, Registry rows, or publication rows.

- [ ] **Step 2: Run the focused backend tests and verify they fail**

Run: `python -m unittest tests.test_central_web_queries tests.test_central_web_api -v`

Expected: import failures for `RegistryQuery` and `CentralWebQueries`.

- [ ] **Step 3: Implement a commit-bound Registry reader**

```python
@dataclass(frozen=True)
class RegistrySnapshot:
    commit_sha: str
    products: Mapping[str, ProductMetadata]
    registries: Mapping[str, ProductRegistry]
    decisions: Mapping[tuple[str, str], DecisionRevision]

class RegistryQueryUnavailable(Exception):
    code = "registry_unavailable"

class RegistryQuery:
    def __init__(self, repository_root: Path, git: GitRegistryAdapter) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.git = git

    def snapshot(self) -> RegistrySnapshot:
        commit_sha = self.git.fetch_and_require_exact_main()
        root = RootRegistry.from_dict(self._read(ROOT_PATH))
        # Read only root-declared product metadata, product registries, and heads.
        # Parse every r0001 file with DecisionRevision.from_dict and prove
        # product_id, decision_id, head path, and revision ownership.
        return RegistrySnapshot(commit_sha, products, registries, decisions)
```

If Git synchronization, canonical JSON, a declared path, or a V1 object is invalid, raise `RegistryQueryUnavailable("registry_unavailable")`; never return an empty snapshot for a failure.

- [ ] **Step 4: Implement server-derived product/dashboard queries**

```python
@dataclass(frozen=True)
class ProductSummary:
    product_id: str
    product_name: str
    repository_ids: tuple[str, ...]
    pending_candidate_count: int
    active_decision_count: int | None
    last_activity_at: str | None

@dataclass(frozen=True)
class DashboardMetrics:
    product_count: int
    pending_candidate_count: int
    active_decision_count: int | None
    completed_this_week: int

@dataclass(frozen=True)
class RegistryStatus:
    state: Literal["available", "unavailable"]
    commit_sha: str | None

@dataclass(frozen=True)
class PublicationSummary:
    publication_id: str
    preview_id: str
    product_id: str
    product_name: str
    decision_count: int
    actor_id: str
    approved_at: str
    state: Literal["confirmed", "committed_pending_push", "completed"]
    recovery_code: str | None
    commit_sha: str | None

@dataclass(frozen=True)
class DashboardView:
    metrics: DashboardMetrics
    registry: RegistryStatus
    products: tuple[ProductSummary, ...]
    recent_publications: tuple[PublicationSummary, ...]
```

`CentralWebQueries` exposes `list_products(principal) -> tuple[ProductSummary, ...]`, `resolve_repository(principal, repository_id) -> RepositoryView | None`, and `dashboard(principal) -> DashboardView`. Its pending-count SQL joins current heads to enabled repository mappings, left-joins receipts, and excludes only a latest matching `accept`, `edit_accept`, or `reject`; `skip` and missing Review remain pending.

Dashboard response shape is fixed to:

```json
{
  "metrics": {
    "product_count": 1,
    "pending_candidate_count": 12,
    "active_decision_count": 14,
    "completed_this_week": 2
  },
  "registry": {"state": "available", "commit_sha": "<40-hex>"},
  "products": [{
    "product_id": "prod_<32-hex>",
    "product_name": "ZDecision",
    "repository_ids": ["repo_<32-hex>"],
    "pending_candidate_count": 12,
    "active_decision_count": 14,
    "last_activity_at": "2026-08-04T00:00:00Z"
  }],
  "recent_publications": []
}
```

- [ ] **Step 5: Add the transport-only Web router and SPA fallback**

```python
router = APIRouter(prefix="/api/v1/web")

@router.get("/dashboard")
async def dashboard() -> dict[str, object]:
    return application.dashboard(browser()).to_dict()
```

`create_app` gains optional keyword-only `web_application` and `static_root` arguments, includes the router only when configured, mounts `/assets`, and returns the same built `index.html` for every non-API browser route. Any `/api/...` miss remains JSON 404 and is never answered by the SPA. Keep all existing route bodies byte-for-byte compatible.

- [ ] **Step 6: Add explicit Registry-root composition to the Demo CLI**

```python
run.add_argument("--registry-repository-root", required=True)

registry_root = Path(arguments.registry_repository_root).expanduser().resolve()
git = GitRegistryAdapter(registry_root)
registry_query = RegistryQuery(registry_root, git)
web_application = CentralWebApplication(
    store=CentralWebStore(store.connection),
    queries=CentralWebQueries(store.connection, registry_query),
)
app = create_app(
    CaptureRequestService(store),
    provider,
    web_application=web_application,
)
```

Reject non-absolute, missing, non-directory, or non-Git roots before starting Uvicorn. Keep loopback enforcement.

- [ ] **Step 7: Create the reproducible frontend package**

```json
{
  "name": "zdecision-web",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "engines": {"node": ">=22.12 <23"},
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "typecheck": "tsc --noEmit --pretty false",
    "test": "vitest run"
  },
  "dependencies": {
    "react": "19.2.8",
    "react-dom": "19.2.8",
    "react-router-dom": "7.18.2"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "7.0.0",
    "@testing-library/react": "16.3.2",
    "@testing-library/user-event": "14.6.3",
    "@types/react": "19.2.18",
    "@types/react-dom": "19.2.4",
    "@vitejs/plugin-react": "6.0.5",
    "jsdom": "30.0.1",
    "typescript": "7.0.2",
    "vite": "8.2.0",
    "vitest": "4.1.10"
  }
}
```

Configure Vite output as `../src/zdecision/central/static`, `assetsDir: "assets"`, `emptyOutDir: true`, and dev proxy `/api` to `http://127.0.0.1:8765`. Generate and commit `package-lock.json` with `npm install --package-lock-only`.

- [ ] **Step 8: Add the company shell and overview test first**

```tsx
it("renders server products and routes without hard-coded product pages", async () => {
  mockDashboard({ product_id: PRODUCT_ID, product_name: "ZStack Cloud" });
  render(<RouterProvider router={router} />);
  expect(await screen.findByText("ZStack Cloud")).toBeVisible();
  expect(screen.getByRole("link", { name: "候选审核" })).toHaveAttribute(
    "href", "/reviews",
  );
  expect(screen.queryByText(/session_id/i)).not.toBeInTheDocument();
});
```

Run: `cd web && npm test -- CompanyOverviewPage.test.tsx`

Expected: fail because the router and page do not exist.

- [ ] **Step 9: Implement the shell, overview, and safe fetch boundary**

```ts
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  const value: unknown = await response.json();
  if (!response.ok) throw new ApiError(response.status, readErrorCode(value));
  return value as T;
}
```

Create routes `/`, `/reviews`, `/decisions`, `/publications`, all product routes, preview, and publication detail. Unimplemented route components at this task render a neutral `功能将在后续切片启用` panel without exposing a working action. The root page fetches only the dashboard; `/reviews` groups `dashboard.products` by pending count and links into one product workspace.

Use the reviewed white ZStack vector in the dark rail next to `ZDecision`; use CSS variables `--rail: #0b1d36`, `--primary: #1769e0`, `--surface: #ffffff`, `--canvas: #f4f7fb`, and `--danger: #c83f32`. Do not inline the user-supplied low-resolution bitmap.

- [ ] **Step 10: Build, package, and verify the shell**

Run: `cd web && npm run typecheck && npm test && npm run build`

Expected: typecheck passes, frontend tests pass, and `src/zdecision/central/static/index.html` plus `static/assets/*` are generated.

Update package data to:

```toml
"zdecision.central" = ["static/*.html", "static/assets/*"]
```

Run: `python -m unittest tests.test_central_web_queries tests.test_central_web_api tests.test_central_api -v`

Expected: all tests pass and existing Capture/Agent APIs remain compatible.

- [ ] **Step 11: Commit Task 2**

```bash
git add pyproject.toml src/zdecision/central src/zdecision/registry/query.py web tests/test_central_web_queries.py tests/test_central_web_api.py
git commit -m "feat: add central decision web shell"
```

### Task 3: Product Candidate Inbox, explicit refresh, and durable Review drafts

**Files:**
- Modify: `src/zdecision/central/web/contracts.py`
- Modify: `src/zdecision/central/web/queries.py`
- Create: `src/zdecision/central/web/reviews.py`
- Modify: `src/zdecision/central/web/application.py`
- Modify: `src/zdecision/central/web/api.py`
- Create: `web/src/pages/candidate-review/CandidateReviewPage.tsx`
- Create: `web/src/features/candidate-refresh/useCandidateRefresh.ts`
- Create: `web/src/features/reviews/ReviewEditor.tsx`
- Test: `tests/test_central_web_review.py`
- Test: `tests/test_central_web_api.py`
- Modify: `tests/test_update_candidates_page.py`
- Test: `web/src/pages/candidate-review/CandidateReviewPage.test.tsx`

**Interfaces:**
- Consumes: current `candidate_family_heads`, immutable `candidate_revisions`, registered repository mappings, Packet 1 Capture endpoints, `ReviewDraft`, and `CentralWebStore.replace_draft`.
- Produces: `CandidateInboxView`, `CentralReviewService.list_candidates`, `get_draft`, `save_draft`, the product Candidate API, and a browser page that restores both Capture progress and Review draft after navigation/restart.

- [ ] **Step 1: Write failing product Candidate and draft-CAS tests**

```python
def test_inbox_contains_only_current_heads_for_route_product(self) -> None:
    view = self.service.list_candidates(self.user, PRODUCT_ID)
    self.assertEqual((FAMILY_ID,), tuple(item.family_id for item in view.items))
    self.assertEqual(REVISION_ID, view.items[0].revision_id)
    self.assertNotIn("session", json.dumps(view.to_dict()).lower())

def test_save_draft_rejects_wrong_version_without_losing_existing_actions(self) -> None:
    first = self.service.save_draft(self.user, PRODUCT_ID, 0, (self.accept_item(),), NOW)
    with self.assertRaises(DraftConflict):
        self.service.save_draft(self.user, PRODUCT_ID, 0, (self.reject_item(),), NOW)
    self.assertEqual(first, self.service.get_draft(self.user, PRODUCT_ID))

def test_draft_cannot_reference_candidate_from_another_product(self) -> None:
    with self.assertRaises(ProductOwnershipConflict):
        self.service.save_draft(self.user, PRODUCT_ID, 0, (self.other_product_item(),), NOW)

def test_capture_batch_filter_uses_safe_request_association(self) -> None:
    view = self.service.list_candidates(
        self.user, PRODUCT_ID, capture_request_id=CAPTURE_REQUEST_ID
    )
    self.assertEqual((FAMILY_ID,), tuple(item.family_id for item in view.items))
```

- [ ] **Step 2: Run the focused backend test and verify it fails**

Run: `python -m unittest tests.test_central_web_review -v`

Expected: import failure for `CentralReviewService`.

- [ ] **Step 3: Add safe Candidate Inbox contracts and queries**

```python
@dataclass(frozen=True)
class CandidateInboxItem:
    family_id: str
    repository_id: str
    capture_request_ids: tuple[str, ...]
    revision_id: str
    revision: int
    content_digest: str
    content: CandidateContent
    review_state: Literal["pending", "accepted", "rejected", "published"]
    draft_action: ReviewAction | None
    stale_draft: bool

@dataclass(frozen=True)
class CandidateInboxView:
    product_id: str
    product_name: str
    repositories: tuple[RepositoryView, ...]
    items: tuple[CandidateInboxItem, ...]
    draft: ReviewDraft
```

The SQL query must join `repository_mappings -> candidate_family_heads -> candidate_revisions`, constrain organization, enabled mapping, and route `product_id`, verify every canonical record digest, then add the safe Capture Request associations plus latest matching Review/receipt state. It accepts `search` up to 200 UTF-8 bytes, an owned `repository_id`, an owned `capture_request_id`, `state` in `pending|accepted|rejected|published|all`, `limit` 1–100, and non-negative `offset`.

- [ ] **Step 4: Implement draft validation and CAS service methods**

`CentralReviewService` exposes these exact calls:

```text
list_candidates(principal, product_id, *, search="", repository_id=None, capture_request_id=None, state="pending", limit=50, offset=0) -> CandidateInboxView
get_draft(principal, product_id) -> ReviewDraft
save_draft(principal, product_id, expected_version, items, now) -> ReviewDraft
```

For every draft item, prove organization/product ownership and that the exact immutable revision exists. Allow a stale older revision to remain in a saved draft so the UI can reconcile it, but mark `stale_draft: true` when it is not the current head. Product and repository lists are read-only in the Demo editor: `edit_accept` must keep `content.product` and `content.repositories` byte-for-byte equal to the current Candidate, while claim, future action, scope summary, paths, and invalidation conditions may change. Limit a draft to 100 items, each note to 1,000 UTF-8 bytes, and each effective content payload to the existing Candidate limits.

- [ ] **Step 5: Add strict Candidate/draft routes**

```python
class _DraftItemBody(_StrictBody):
    family_id: str
    repository_id: str
    revision_id: str
    revision: int = Field(ge=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["accept", "edit_accept", "reject", "skip"]
    effective_content: dict[str, object] | None = None
    note: str | None = Field(default=None, max_length=1000)

class _SaveDraftBody(_StrictBody):
    expected_version: int = Field(ge=0)
    items: list[_DraftItemBody] = Field(max_length=100)

@router.get("/products/{product_id}/candidates")
async def candidates(
    product_id: str,
    search: str = Query(default="", max_length=200),
    repository_id: str | None = None,
    capture_request_id: str | None = None,
    state: Literal["pending", "accepted", "rejected", "published", "all"] = "pending",
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    return application.list_candidates(
        browser(), product_id, search=search, repository_id=repository_id,
        capture_request_id=capture_request_id, state=state,
        limit=limit, offset=offset,
    ).to_dict()

@router.get("/products/{product_id}/review-draft")
async def get_review_draft(product_id: str) -> dict[str, object]:
    return application.get_review_draft(browser(), product_id).to_dict()

@router.put("/products/{product_id}/review-draft")
async def save_review_draft(product_id: str, body: _SaveDraftBody) -> dict[str, object]:
    return application.save_review_draft(
        browser(), product_id, body.expected_version,
        tuple(item.to_contract() for item in body.items), current_time(),
    ).to_dict()
```

Map unknown/disabled product or repository to 404, cross-product ownership to 409 `product_ownership_conflict`, CAS mismatch to 409 `review_draft_conflict`, and malformed edited content to 422 `invalid_request`. Never echo an exception string.

- [ ] **Step 6: Write the failing browser interaction test**

```tsx
it("refreshes one owned repository and restores a partial draft", async () => {
  mockCandidateInbox({ draftVersion: 2, action: "accept" });
  renderCandidatePage(`/products/${PRODUCT_ID}/candidates?repository_id=${REPOSITORY_ID}`);
  expect(await screen.findByDisplayValue("接受")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "更新候选决策" }));
  expect(lastCaptureBody()).toEqual({
    repository_id: REPOSITORY_ID,
    template_id: "business",
    capture_scope: "all_valid_sessions",
    client_action_id: expect.stringMatching(/^web_action_/),
  });
});
```

Run: `cd web && npm test -- CandidateReviewPage.test.tsx`

Expected: fail because the Candidate page does not exist.

- [ ] **Step 7: Implement Candidate Review UI and Packet 1 refresh restoration**

`CandidateReviewPage` must:

```tsx
<ReviewEditor
  item={item}
  action={draftByFamily.get(item.family_id)}
  onChange={updateLocalDraft}
/>
```

- render Candidate fields through React text nodes only;
- filter by safe Capture Request ID without displaying Session or Turn identity;
- expose `accept`, `edit_accept`, `reject`, and `skip` for each row;
- lock product and repository fields during edit;
- save the whole draft with the last server version;
- retain local edits after a 409 and show `审核草稿已在其他页面更新`;
- show stale draft rows as `已有新版本` without silently moving the action;
- choose one of the product's registered repositories before refresh;
- create only `all_valid_sessions` requests;
- store `{request_id, repository_id, last_sequence}` under `zdecision:capture:<repository_id>`;
- reconnect through `GET /api/v1/capture-requests/{id}/events?after_sequence=N`;
- reload the Candidate Inbox on `succeeded` or `succeeded_no_candidates`; and
- distinguish empty Candidate results from Capture failure.

For `/?repository_id=...`, fetch the server repository mapping and navigate only when its enabled product is returned. Unknown/disabled repository renders `仓库未登记或未启用`; it never falls back to a client-selected product.

- [ ] **Step 8: Replace obsolete single-HTML assertions**

```python
def test_spa_build_and_capture_api_keep_the_explicit_boundary(self) -> None:
    html = self.client.get("/").text
    self.assertIn('<div id="root"></div>', html)
    self.assertNotIn("session_id", html.lower())
    response = self.client.post("/api/v1/capture-requests", json=self.capture_body())
    self.assertEqual(200, response.status_code)
    self.assertEqual("all_valid_sessions", response.json()["capture_scope"])
```

Delete assertions that Review/Publish strings must not exist; those assertions described the superseded technical page rather than Packet 2.

- [ ] **Step 9: Run focused backend/frontend compatibility tests**

Run: `python -m unittest tests.test_central_web_review tests.test_central_web_api tests.test_update_candidates_page tests.test_central_api -v`

Expected: all tests pass.

Run: `cd web && npm run typecheck && npm test -- CandidateReviewPage.test.tsx`

Expected: typecheck and Candidate page tests pass.

- [ ] **Step 10: Commit Task 3**

```bash
git add src/zdecision/central/web web/src tests/test_central_web_review.py tests/test_central_web_api.py tests/test_update_candidates_page.py
git commit -m "feat: add product candidate review drafts"
```

### Task 4: All-or-nothing immutable Review submission

**Files:**
- Modify: `src/zdecision/central/web/store.py`
- Modify: `src/zdecision/central/web/reviews.py`
- Modify: `src/zdecision/central/web/application.py`
- Modify: `src/zdecision/central/web/api.py`
- Modify: `web/src/pages/candidate-review/CandidateReviewPage.tsx`
- Modify: `web/src/features/reviews/ReviewEditor.tsx`
- Test: `tests/test_central_web_review.py`
- Test: `tests/test_central_web_api.py`
- Test: `web/src/pages/candidate-review/CandidateReviewPage.test.tsx`

**Interfaces:**
- Consumes: exact current heads, saved draft version, stable Web action ID, `central_review_batch_id`, `review_item_id`, and `CentralWebStore.put_review_batch`.
- Produces: `CentralReviewService.submit`, immutable one-product Review batches, latest Review state, `POST /api/v1/web/products/{product_id}/reviews`, and the UI transition to preview eligibility or completed reject/skip Review.

- [ ] **Step 1: Write failing atomicity, action replay, and partial-Review tests**

```python
def test_partial_review_records_only_classified_items(self) -> None:
    result = self.service.submit(
        self.user, PRODUCT_ID, "web_action_review_1", 1,
        (self.accept_item(FAMILY_A), self.reject_item(FAMILY_B)), NOW,
    )
    self.assertEqual((FAMILY_A, FAMILY_B), tuple(i.family_id for i in result.batch.items))
    self.assertEqual((FAMILY_C,), tuple(i.family_id for i in result.remaining_pending))
    self.assertTrue(result.preview_eligible)

def test_one_changed_revision_writes_no_batch(self) -> None:
    self.advance_family_b()
    with self.assertRaises(ReviewStale):
        self.service.submit(
            self.user, PRODUCT_ID, "web_action_review_2", 1,
            (self.accept_item(FAMILY_A), self.accept_item(FAMILY_B)), NOW,
        )
    self.assertEqual(0, self.count_rows("web_review_batches"))

def test_identical_action_replays_and_changed_bytes_conflict(self) -> None:
    first = self.submit("web_action_review_3", self.accept_item(FAMILY_A))
    self.assertEqual(first, self.submit("web_action_review_3", self.accept_item(FAMILY_A)))
    with self.assertRaises(WebActionConflict):
        self.submit("web_action_review_3", self.reject_item(FAMILY_A))
```

- [ ] **Step 2: Run the focused test and verify the submit path fails**

Run: `python -m unittest tests.test_central_web_review.CentralReviewSubmissionTest -v`

Expected: `AttributeError` for missing `submit`.

- [ ] **Step 3: Implement one immediate transaction for validation and immutable write**

```python
@dataclass(frozen=True)
class ReviewSubmissionResult:
    batch: CentralReviewBatch
    preview_eligible: bool
    remaining_pending: tuple[str, ...]
    draft_version: int

def submit(
    self,
    principal: Principal,
    product_id: str,
    client_action_id: str,
    expected_draft_version: int,
    items: Sequence[DraftItem],
    now: str,
) -> ReviewSubmissionResult:
    actor = require_browser_user(principal)
    ordered = tuple(items)
    request_digest = sha256(canonical_json_bytes({
        "draft_version": expected_draft_version,
        "items": [item.to_dict() for item in ordered],
        "product_id": product_id,
    })).hexdigest()
    with immediate(self.store.connection):
        replay = self.store.action_result(
            actor.organization_id, actor.actor_id, "review", client_action_id
        )
        if replay is not None:
            return self._require_identical_replay(replay, request_digest)
        draft = self.store.get_draft(actor.organization_id, actor.actor_id, product_id)
        if draft.version != expected_draft_version:
            raise DraftConflict("review_draft_conflict")
        current = self._load_and_validate_all_current(actor, product_id, ordered)
        batch = self._freeze_batch(actor, product_id, client_action_id, request_digest, current, now)
        self.store.put_review_batch(batch)
        self.store.clear_submitted_draft_items(draft, ordered, now)
        self.store.record_action(actor.organization_id, actor.actor_id, "review", client_action_id, request_digest, batch.review_batch_id, now)
    return self._submission_result(batch)
```

Validation must complete before the first immutable insert. For `accept`, freeze current content. For `edit_accept`, validate complete content, unchanged product/repositories, and same-product route ownership. For `reject`/`skip`, require no effective content. Derive:

```python
approval = ApprovalRef(
    actor="user",
    thread_id=f"web_review_{batch_id.removeprefix('rvb_')}",
    turn_id=client_action_id,
    recorded_at=now,
)
```

Each item uses `publication_candidate_id(family_id)` plus existing `review_item_id(batch_id, publication_candidate_id)`. A `skip` remains pending; `reject` is processed; accepted items are eligible for one exact preview. A Review containing only reject/skip items creates no preview record.

- [ ] **Step 4: Add the strict Review endpoint**

```python
class _SubmitReviewBody(_StrictBody):
    client_action_id: str = Field(pattern=r"^web_action_[A-Za-z0-9-]{1,96}$")
    expected_draft_version: int = Field(ge=0)
    items: list[_DraftItemBody] = Field(min_length=1, max_length=20)

@router.post("/products/{product_id}/reviews")
async def submit_review(product_id: str, body: _SubmitReviewBody) -> dict[str, object]:
    return application.submit_review(browser(), product_id, body, current_time()).to_dict()
```

Response includes `review_batch_id`, ordered safe item results, `preview_eligible`, `remaining_pending_count`, and the incremented draft version. It contains no Review notes on company-wide pages.

- [ ] **Step 5: Complete the partial Review UI flow**

```tsx
const result = await api<ReviewSubmissionResult>(
  `/api/v1/web/products/${productId}/reviews`,
  { method: "POST", body: JSON.stringify({
      client_action_id: makeWebActionId(),
      expected_draft_version: draft.version,
      items: orderedClassifiedItems,
  }) },
);
```

Disable submit when no row has an action. Label the primary action `生成发布预览` when any item is accepted/edit-accepted, otherwise `提交审核结果`. On `review_stale`, retain local/durable draft selections, mark safe offending family IDs `已有新版本`, and offer `载入最新版本`; never resubmit automatically.

- [ ] **Step 6: Run focused Review and UI tests**

Run: `python -m unittest tests.test_central_web_review tests.test_central_web_api -v`

Expected: all Review atomicity, replay, product, and stale tests pass.

Run: `cd web && npm test -- CandidateReviewPage.test.tsx`

Expected: partial accept/reject, reject-only, stale reconciliation, and no-HTML-render tests pass.

- [ ] **Step 7: Commit Task 4**

```bash
git add src/zdecision/central/web web/src/pages/candidate-review web/src/features/reviews tests/test_central_web_review.py tests/test_central_web_api.py
git commit -m "feat: add immutable product review submission"
```

### Task 5: Immutable exact publication preview

**Files:**
- Create: `src/zdecision/central/web/previews.py`
- Modify: `src/zdecision/central/web/store.py`
- Modify: `src/zdecision/central/web/application.py`
- Modify: `src/zdecision/central/web/api.py`
- Create: `web/src/pages/publication-preview/PublicationPreviewPage.tsx`
- Modify: `web/src/pages/candidate-review/CandidateReviewPage.tsx`
- Test: `tests/test_central_web_preview.py`
- Test: `tests/test_central_web_api.py`
- Test: `web/src/pages/publication-preview/PublicationPreviewPage.test.tsx`

**Interfaces:**
- Consumes: immutable accepted Review items, `RegistryCatalog.inspect/render`, `GitRegistryAdapter.fetch_and_require_exact_main`, existing `publication_preview_id`, V1 `DecisionSeed`/`DecisionRevision`, and immutable preview storage.
- Produces: `CentralPreviewService.create/get/check_publishability`, preview POST/GET APIs, and the independent exact-preview page.

- [ ] **Step 1: Write failing accepted-subset and exact-byte tests**

```python
def test_preview_contains_only_accepted_effective_content_and_writes_nothing(self) -> None:
    before = self.registry_tree_bytes()
    preview = self.service.create(
        self.user, REVIEW_BATCH_ID, "web_action_preview_1", NOW,
    )
    self.assertEqual((ACCEPTED_CANDIDATE_ID,), preview.candidate_ids)
    self.assertNotIn("rejected claim", "".join(f.content for f in preview.display_documents))
    self.assertEqual(before, self.registry_tree_bytes())

def test_preview_replay_is_exact_and_action_conflict_is_rejected(self) -> None:
    first = self.create("web_action_preview_2", REVIEW_BATCH_ID)
    self.assertEqual(first, self.create("web_action_preview_2", REVIEW_BATCH_ID))
    with self.assertRaises(WebActionConflict):
        self.create("web_action_preview_2", OTHER_REVIEW_BATCH_ID)

def test_new_review_or_registry_base_makes_preview_stale(self) -> None:
    preview = self.create("web_action_preview_3", REVIEW_BATCH_ID)
    self.record_new_review_for_accepted_family()
    self.assertEqual("stale", self.service.get(self.user, preview.preview_id).publishability)
```

- [ ] **Step 2: Run the focused test and verify the preview service is missing**

Run: `python -m unittest tests.test_central_web_preview -v`

Expected: import failure for `CentralPreviewService`.

- [ ] **Step 3: Convert accepted central Review items to V1 seeds**

```python
def _seed(batch: CentralReviewBatch, item: CentralReviewItem) -> DecisionSeed:
    assert item.action in ("accept", "edit_accept")
    assert item.effective_content is not None
    candidate_id = publication_candidate_id(item.family_id)
    return DecisionSeed(
        candidate_id=candidate_id,
        decision_id=decision_id(candidate_id, batch.product_id),
        product_id=batch.product_id,
        product_name=batch.product_name,
        content=item.effective_content,
        source=SourceCheckpoint(
            thread_id=f"candidate_family_{item.family_id.removeprefix('cfm_')}",
            turn_id=f"candidate_revision_{item.revision_id.removeprefix('crv_')}",
        ),
        review_approval=batch.approval,
    )
```

These source values are opaque central Candidate coordinates, not native Codex IDs. Persist the original `family_id`/`revision_id` only in central Review/receipt rows; do not add fields to Decision V1.

- [ ] **Step 4: Implement exact preview creation and immutable storage**

```python
def create(
    self, principal: Principal, review_batch_id: str,
    client_action_id: str, now: str,
) -> PublicationRecord:
    batch = self._owned_batch(principal, review_batch_id)
    accepted = tuple(i for i in batch.items if i.action in ("accept", "edit_accept"))
    if not accepted:
        raise NoAcceptedItems("no_accepted_items")
    self._require_latest_and_unpublished(batch, accepted)
    seeds = tuple(self._seed(batch, item) for item in accepted)
    base_commit = self.git.fetch_and_require_exact_main()
    self.git.require_clean_registry()
    plan = self.catalog.inspect(seeds)
    preview_id = publication_preview_id({
        "base_commit": base_commit,
        "base_registry_digests": plan.base_registry_digests,
        "decision_ids": plan.decision_ids,
        "publisher_format": PUBLISHER_FORMAT_VERSION,
        "review_ids": tuple(item.review_id for item in accepted),
        "target_paths": plan.changed_paths,
    })
    draft = self.catalog.render(plan, preview_id)
    record = PublicationRecord(
        record_version=1,
        preview_id=preview_id,
        content_digest=content_digest_for_files(tuple(
            PublicationFile.from_bytes(path, data)
            for path, data in sorted(draft.display_documents.items())
        )),
        state="previewed",
        created_at=now,
        review_batch_id=batch.review_batch_id,
        review_ids=tuple(item.review_id for item in accepted),
        candidate_ids=tuple(item.publication_candidate_id for item in accepted),
        decision_ids=plan.decision_ids,
        product_id=batch.product_id,
        product_name=batch.product_name,
        base_commit=base_commit,
        base_registry_digests=plan.base_registry_digests,
        display_documents=display_files,
        changed_files=changed_files,
        commit_message=(
            f"decision({batch.product_id}): publish {len(accepted)} decisions\n\n"
            f"ZDecision-Preview: {preview_id}\n"
        ),
    )
    return self.store.put_preview(batch.organization_id, batch.product_id, record)
```

Complete `display_files` and `changed_files` from sorted `RegistryDraft` entries before record construction. Record the preview action replay in the same SQLite transaction as the immutable preview. If Registry access fails, retain the Review and write neither preview nor Registry files.

- [ ] **Step 5: Recompute publishability without mutating the preview**

```python
@dataclass(frozen=True)
class PublicationPreviewView:
    record: PublicationRecord
    publishability: Literal["publishable", "stale", "registry_unavailable"]
    publication_id: str | None
```

`check_publishability` must prove:

```python
def _require_fresh(self, batch: CentralReviewBatch, record: PublicationRecord) -> None:
    accepted = self._accepted(batch)
    self._require_latest_and_unpublished(batch, accepted)
    seeds = tuple(self._seed(batch, item) for item in accepted)
    self.git.fetch_and_require_exact_main(record.base_commit)
    self.git.require_clean_registry()
    plan = self.catalog.inspect(seeds)
    draft = self.catalog.render(plan, record.preview_id)
    if (
        tuple(item.review_id for item in accepted) != record.review_ids
        or plan.decision_ids != record.decision_ids
        or dict(plan.base_registry_digests) != dict(record.base_registry_digests)
        or plan.changed_paths != tuple(file.path for file in record.changed_files)
        or dict(draft.display_documents) != record.display_file_bytes()
        or dict(draft.changed_files) != record.changed_file_bytes()
    ):
        raise PreviewStale("preview_stale")
```

`get` returns `publishability: publishable|stale|registry_unavailable` plus the unchanged frozen record. It never creates a replacement under the same ID.

- [ ] **Step 6: Add preview create/read routes**

```python
class _ActionBody(_StrictBody):
    client_action_id: str = Field(pattern=r"^web_action_[A-Za-z0-9-]{1,96}$")

@router.post("/reviews/{review_batch_id}/previews")
async def create_preview(review_batch_id: str, body: _ActionBody) -> dict[str, object]:
    return application.create_preview(
        browser(), review_batch_id, body.client_action_id, current_time()
    ).to_dict()

@router.get("/publication-previews/{preview_id}")
async def get_preview(preview_id: str) -> dict[str, object]:
    return application.get_preview(browser(), preview_id).to_dict()
```

The read response includes full readable Decision fields, canonical JSON strings, file digests, exact paths, base commit, base Registry digests, preview/content digests, commit message, changed-file list, and publishability. It excludes rejected/skipped text and native source IDs.

- [ ] **Step 7: Write the failing independent-page UI test**

```tsx
it("shows exact files and uses the preview page as the only confirmation", async () => {
  mockPreview({ publishability: "publishable", decisionCount: 2 });
  renderPreviewPage(`/publication-previews/${PREVIEW_ID}`);
  expect(await screen.findByText("确认发布 2 条决策")).toBeEnabled();
  expect(screen.getByText(/decision-registry\/products\/prod_/)).toBeVisible();
  expect(screen.getByText("完整 JSON")).toBeVisible();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
```

- [ ] **Step 8: Implement the exact preview page**

Render complete Decision field panels, expandable canonical JSON in `<pre>`, target paths, root/product Registry JSON, base/digests, commit message, and changed files. `返回修改审核` links to the product Candidate page. Disable publish for `stale` and `registry_unavailable`; label stale `预览已过期` and require an explicit new preview action after Review reconciliation. The page contains one publish button and no modal.

- [ ] **Step 9: Run preview domain, API, Registry, and frontend tests**

Run: `python -m unittest tests.test_central_web_preview tests.test_central_web_api tests.test_registry -v`

Expected: exact-byte, no-write, staleness, API, and existing Registry tests pass.

Run: `cd web && npm test -- PublicationPreviewPage.test.tsx`

Expected: preview rendering, stale disablement, inert JSON, and no-modal tests pass.

- [ ] **Step 10: Commit Task 5**

```bash
git add src/zdecision/central/web web/src/pages/publication-preview web/src/pages/candidate-review tests/test_central_web_preview.py tests/test_central_web_api.py
git commit -m "feat: add exact central publication previews"
```

### Task 6: Explicit publication, exact Git recovery, and publication history

**Files:**
- Create: `src/zdecision/central/web/publications.py`
- Modify: `src/zdecision/central/web/store.py`
- Modify: `src/zdecision/central/web/queries.py`
- Modify: `src/zdecision/central/web/application.py`
- Modify: `src/zdecision/central/web/api.py`
- Modify: `src/zdecision/central/cli.py`
- Create: `web/src/pages/publication-history/PublicationHistoryPage.tsx`
- Create: `web/src/pages/publication-history/PublicationDetailPage.tsx`
- Modify: `web/src/pages/publication-preview/PublicationPreviewPage.tsx`
- Test: `tests/test_central_web_publication.py`
- Test: `tests/test_central_web_api.py`
- Test: `web/src/pages/publication-history/PublicationHistoryPage.test.tsx`

**Interfaces:**
- Consumes: immutable `PublicationRecord(state="previewed")`, `central_publication_id`, `GitRegistryAdapter.reconcile_exact_commit/commit_exact/publication_remote_state/push_exact`, `RegistryCatalog.write_exact`, and `CentralWebStore` monotonic state/receipt methods.
- Produces: `CentralPublicationService.confirm/resume/get/list`, publication POST/resume/history/detail APIs, exact crash recovery, and global/product publication-history UI.

- [ ] **Step 1: Write failing confirmation, crash-adoption, push, and ambiguity tests**

```python
def test_confirmation_is_durable_before_git_mutation(self) -> None:
    self.service.checkpoint = raise_at("after_confirmation")
    with self.assertRaises(InjectedCrash):
        self.confirm("web_action_publish_1")
    stored = self.web_store.get_publication_by_preview("org_demo", PREVIEW_ID)
    self.assertEqual("confirmed", stored.state)
    self.assertIsNone(stored.commit_sha)

def test_commit_success_before_state_write_is_adopted_once(self) -> None:
    self.service.checkpoint = raise_at("after_commit")
    with self.assertRaises(InjectedCrash):
        self.confirm("web_action_publish_2")
    commit_count = self.commit_count()
    result = self.service.resume(self.user, PUBLICATION_ID, "web_action_resume_1", NOW)
    self.assertEqual(commit_count, self.commit_count())
    self.assertEqual("completed", result.state)

def test_interrupted_push_stays_pending_and_retries_same_commit(self) -> None:
    self.git.fail_push_verification_once = True
    pending = self.confirm("web_action_publish_3")
    self.assertEqual("committed_pending_push", pending.state)
    completed = self.service.resume(self.user, pending.publication_id, "web_action_resume_2", NOW)
    self.assertEqual(pending.commit_sha, completed.commit_sha)

def test_unrelated_remote_state_stops_ambiguous_without_second_commit(self) -> None:
    self.create_unrelated_remote_commit()
    with self.assertRaises(PublicationAmbiguous):
        self.service.resume(self.user, PUBLICATION_ID, "web_action_resume_3", NOW)
    self.assertEqual("ambiguous", self.stored_publication().recovery_code)

def test_confirmation_cannot_reclaim_a_family_owned_by_concurrent_winner(self) -> None:
    second = self.publishable_preview()
    self.insert_concurrent_publication_claim(FAMILY_ID)
    with self.assertRaises(CandidateAlreadyPublishing):
        self.confirm_preview(second, "web_action_publish_second")
```

Also cover identical publish/resume action replay, conflicting bytes, crash before commit, crash after remote push but before `completed`, and one receipt per family.

- [ ] **Step 2: Run the focused test and verify the publication service is missing**

Run: `python -m unittest tests.test_central_web_publication -v`

Expected: import failure for `CentralPublicationService`.

- [ ] **Step 3: Persist confirmation before any Git call**

```python
def confirm(
    self, principal: Principal, preview_id: str,
    client_action_id: str, now: str,
) -> CentralPublication:
    actor = require_browser_user(principal)
    preview = self.previews.require_publishable(actor, preview_id)
    request_digest = sha256(canonical_json_bytes({"preview_id": preview_id})).hexdigest()
    with immediate(self.store.connection):
        replay = self.store.action_result(
            actor.organization_id, actor.actor_id, "publish", client_action_id
        )
        if replay is not None:
            publication = self._require_identical_replay(replay, request_digest)
        else:
            publication_id = central_publication_id(preview_id)
            publication = CentralPublication(
                publication_id=publication_id,
                organization_id=actor.organization_id,
                actor_id=actor.actor_id,
                product_id=preview.product_id,
                preview_id=preview_id,
                confirm_action_id=client_action_id,
                confirm_request_digest=request_digest,
                state="confirmed",
                approval=ApprovalRef(
                    actor="user",
                    thread_id=f"web_publication_{publication_id.removeprefix('plb_')}",
                    turn_id=client_action_id,
                    recorded_at=now,
                ),
                commit_sha=None,
                recovery_code=None,
                created_at=now,
                updated_at=now,
            )
            self.store.put_publication(publication)
            self.store.claim_publication_families(
                publication, self.previews.family_ids(preview_id)
            )
            self.store.record_action(actor.organization_id, actor.actor_id, "publish", client_action_id, request_digest, publication_id, now)
    self.checkpoint("after_confirmation")
    return self._resume(publication)
```

If the preview is stale/unavailable, create no publication row. A second publish action for the same preview returns the existing publication only when it refers to the same immutable preview; it never creates a second publication identity.

- [ ] **Step 4: Implement exact commit adoption and monotonic recovery**

```python
def _execution_record(
    preview: PublicationRecord, publication: CentralPublication,
) -> PublicationRecord:
    return replace(
        preview,
        state=publication.state,
        publication_approval=publication.approval,
        commit_sha=publication.commit_sha,
    )

def _resume_confirmed(self, publication: CentralPublication) -> CentralPublication:
    preview = self._preview(publication.preview_id)
    reconciled = self.git.reconcile_exact_commit(
        preview.base_commit, preview.commit_message, preview.changed_file_bytes()
    )
    if reconciled is None:
        self.git.require_clean_registry(preview.changed_file_bytes())
        self.catalog.write_exact(preview.changed_file_bytes())
        commit_sha = self.git.commit_exact(
            preview.base_commit, preview.commit_message, preview.changed_file_bytes()
        )
        remote_contains = False
    else:
        commit_sha = reconciled.commit_sha
        remote_contains = reconciled.remote_contains_commit
    self.checkpoint("after_commit")
    pending = replace(
        publication,
        state="committed_pending_push",
        commit_sha=commit_sha,
        updated_at=self.clock(),
    )
    with immediate(self.store.connection):
        self.store.put_family_receipts(publication, preview, commit_sha)
        pending = self.store.replace_publication(publication, pending)
    return self._complete_or_push(pending, remote_contains)
```

For `committed_pending_push`, call `publication_remote_state`. If it returns `base`, call `push_exact` with the same SHA/base. If it returns `contains`, advance to `completed`. On `RegistryPushFailed`, keep `committed_pending_push` and return it. On `PublicationGitAmbiguous`, persist `recovery_code="ambiguous"` without changing the monotonic state, then reject every automatic resume. A successful remote push followed by a crash is recovered by proving `contains` and advancing the same row.

- [ ] **Step 5: Add publish, safe resume, history, and detail APIs**

```python
@router.post("/publication-previews/{preview_id}/publish")
async def publish(preview_id: str, body: _ActionBody) -> dict[str, object]:
    return application.publish(
        browser(), preview_id, body.client_action_id, current_time()
    ).to_dict()

@router.post("/publications/{publication_id}/resume")
async def resume(publication_id: str, body: _ActionBody) -> dict[str, object]:
    return application.resume_publication(
        browser(), publication_id, body.client_action_id, current_time()
    ).to_dict()

@router.get("/publications")
async def publications(
    product_id: str | None = None,
    state: Literal["confirmed", "committed_pending_push", "completed", "ambiguous"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    return application.list_publications(
        browser(), product_id=product_id, state=state, limit=limit, offset=offset
    ).to_dict()

@router.get("/publications/{publication_id}")
async def publication_detail(publication_id: str) -> dict[str, object]:
    return application.get_publication(browser(), publication_id).to_dict()
```

History rows include product ID/name, publication/preview IDs, Decision count, fixed Demo actor ID, approval time, durable state, recovery code, and commit SHA. They never include rejected/skipped Candidate content or Review notes. `resume` only retries a frozen known-safe publication and returns 409 `publication_ambiguous` for ambiguous state.

- [ ] **Step 6: Complete Preview publish and history UI tests first**

```tsx
it("publishes once and shows pending push without claiming success", async () => {
  mockPublishResponse({ state: "committed_pending_push", commit_sha: COMMIT });
  renderPreviewPage(`/publication-previews/${PREVIEW_ID}`);
  await user.click(await screen.findByRole("button", { name: "确认发布 2 条决策" }));
  expect(await screen.findByText("已提交，等待推送")).toBeVisible();
  expect(screen.queryByText("发布完成")).not.toBeInTheDocument();
});

it("groups global history without permitting cross-product mutation", async () => {
  mockPublicationHistory([cloudPublication, zmetisPublication]);
  renderHistoryPage("/publications");
  expect(await screen.findByText("ZStack Cloud")).toBeVisible();
  expect(screen.getByText("ZMetis")).toBeVisible();
  expect(screen.queryByRole("button", { name: /批量发布/ })).not.toBeInTheDocument();
});
```

- [ ] **Step 7: Implement publication status, recovery, and history pages**

After the one preview-page click, navigate to `/publications/{publication_id}`. Map states exactly:

```ts
export const publicationLabels = {
  confirmed: "准备提交",
  committed_pending_push: "已提交，等待推送",
  completed: "发布完成",
  ambiguous: "需要人工处理",
} as const;
```

Show `继续安全推送` only for `committed_pending_push`; show no retry button for ambiguous. Global and product pages call the same endpoint with or without `product_id`. Detail links to exact Decisions when available and shows commit SHA as text; it does not construct a shell command.

- [ ] **Step 8: Run focused publication, Git, API, and frontend tests**

Run: `python -m unittest tests.test_central_web_publication tests.test_central_web_api tests.test_git_registry tests.test_registry -v`

Expected: all state, crash, exact-commit, product, and existing Registry/Git tests pass.

Run: `cd web && npm test -- PublicationPreviewPage.test.tsx PublicationHistoryPage.test.tsx`

Expected: one-click confirmation, pending-push, completed, ambiguous, and product grouping tests pass.

- [ ] **Step 9: Commit Task 6**

```bash
git add src/zdecision/central/web src/zdecision/central/cli.py web/src/pages/publication-preview web/src/pages/publication-history tests/test_central_web_publication.py tests/test_central_web_api.py
git commit -m "feat: publish reviewed decisions with recovery"
```

### Task 7: Formal Decision catalog, detail, and complete dashboard read models

**Files:**
- Modify: `src/zdecision/registry/query.py`
- Modify: `src/zdecision/central/web/queries.py`
- Modify: `src/zdecision/central/web/application.py`
- Modify: `src/zdecision/central/web/api.py`
- Modify: `web/src/pages/company-overview/CompanyOverviewPage.tsx`
- Create: `web/src/pages/decision-catalog/DecisionCatalogPage.tsx`
- Create: `web/src/pages/decision-catalog/DecisionDetailPage.tsx`
- Modify: `web/src/pages/publication-history/PublicationDetailPage.tsx`
- Test: `tests/test_central_web_queries.py`
- Test: `tests/test_central_web_api.py`
- Test: `web/src/pages/decision-catalog/DecisionCatalogPage.test.tsx`
- Test: `web/src/pages/decision-catalog/DecisionDetailPage.test.tsx`

**Interfaces:**
- Consumes: `RegistrySnapshot`, V1 `DecisionRevision`, completed central publication rows keyed by preview ID, and server-owned product mapping.
- Produces: global/product Decision search, product-owned detail, accurate company/product metrics, explicit unavailable state, and read-only catalog/detail pages.

- [ ] **Step 1: Write failing Registry/read-model tests**

```python
def test_global_and_product_catalog_return_same_owned_revision(self) -> None:
    global_items = self.queries.list_decisions(self.user, product_id=None, search="隔离")
    product_items = self.queries.list_decisions(self.user, product_id=PRODUCT_ID, search="隔离")
    self.assertEqual(global_items.items, product_items.items)
    self.assertEqual(PRODUCT_ID, global_items.items[0].product_id)

def test_product_detail_rejects_decision_owned_by_another_product(self) -> None:
    with self.assertRaises(DecisionNotFound):
        self.queries.get_decision(self.user, OTHER_PRODUCT_ID, DECISION_ID)

def test_invalid_registry_is_unavailable_not_empty(self) -> None:
    self.corrupt_product_registry()
    result = self.queries.list_decisions(self.user)
    self.assertEqual("unavailable", result.registry_state)
    self.assertIsNone(result.items)
```

- [ ] **Step 2: Run the focused query tests and verify missing methods fail**

Run: `python -m unittest tests.test_central_web_queries -v`

Expected: `AttributeError` for `list_decisions` and `get_decision`.

- [ ] **Step 3: Add bounded product-owned Registry query methods**

```python
@dataclass(frozen=True)
class DecisionListItem:
    product_id: str
    product_name: str
    decision_id: str
    revision: int
    lifecycle: str
    claim: str
    future_action: str
    scope_summary: str
    repositories: tuple[str, ...]
    paths: tuple[str, ...]
    published_at: str | None
    publication_id: str | None
    commit_sha: str | None

@dataclass(frozen=True)
class DecisionListView:
    registry_state: Literal["available", "unavailable"]
    registry_commit: str | None
    items: tuple[DecisionListItem, ...] | None
    total: int | None

@dataclass(frozen=True)
class DecisionDetailView:
    registry_commit: str
    decision: DecisionRevision
    publication_id: str | None
    published_at: str | None
    commit_sha: str | None

list_decisions(principal, *, product_id=None, search="", repository="", published_after=None, limit=50, offset=0) -> DecisionListView
get_decision(principal, product_id, decision_id) -> DecisionDetailView
```

Load one commit-bound snapshot per request. Filter only `lifecycle == "active"`; case-fold search over claim, future action, and scope summary; match repository against the formal repository list; join central publication by `publication_preview_id` for published time, publication ID, and commit SHA. Validate `search`/`repository` at 200 UTF-8 bytes, `limit` 1–100, non-negative offset, and RFC 3339 `published_after`.

Detail response contains the complete formal V1 document, product, revision/lifecycle, scope, invalidation conditions, safe opaque source checkpoint, Review approval coordinate, preview/publication IDs, and commit. It is read-only and contains no update/delete relation.

- [ ] **Step 4: Add Decision list/detail routes and finish dashboard metrics**

```python
@router.get("/decisions")
async def decisions(
    product_id: str | None = None,
    search: str = Query(default="", max_length=200),
    repository: str = Query(default="", max_length=200),
    published_after: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    return application.list_decisions(
        browser(), product_id=product_id, search=search, repository=repository,
        published_after=published_after, limit=limit, offset=offset,
    ).to_dict()

@router.get("/products/{product_id}/decisions/{decision_id}")
async def decision_detail(product_id: str, decision_id: str) -> dict[str, object]:
    return application.get_decision(browser(), product_id, decision_id).to_dict()
```

Return HTTP 503 with `registry_unavailable` for an invalid/unreachable Registry, not `items: []`. Return 404 for unknown/mismatched product Decision. Update dashboard metrics from the same snapshot and completed publication rows; a Registry failure sets dashboard `registry.state="unavailable"` and `active_decision_count=null`, while Candidate/product counts remain visible.

- [ ] **Step 5: Write failing catalog/detail UI tests**

```tsx
it("keeps global ownership visible and product routes isolated", async () => {
  mockDecisionList([cloudDecision, zmetisDecision]);
  renderCatalogPage("/decisions");
  expect(await screen.findByText("ZStack Cloud")).toBeVisible();
  expect(screen.getByText("ZMetis")).toBeVisible();
  expect(screen.getAllByRole("link", { name: "查看决策" })[0]).toHaveAttribute(
    "href", `/products/${CLOUD_PRODUCT_ID}/decisions/${CLOUD_DECISION_ID}`,
  );
});

it("renders Decision text inert and exposes no mutation controls", async () => {
  mockDecisionDetail({ claim: '<img src=x onerror="alert(1)">' });
  renderDecisionPage(`/products/${PRODUCT_ID}/decisions/${DECISION_ID}`);
  expect(await screen.findByText('<img src=x onerror="alert(1)">')).toBeVisible();
  expect(document.querySelector("img[src='x']")).toBeNull();
  expect(screen.queryByRole("button", { name: /编辑|删除|退休/ })).not.toBeInTheDocument();
});
```

- [ ] **Step 6: Implement global/product catalog and read-only detail UI**

Use the same `DecisionCatalogPage` for `/decisions` and `/products/:productId/decisions`; the route parameter supplies only the filter. Show keyword, repository, and publication-time filters, product ownership, revision, scope, and publication time. Use `AsyncState` to distinguish `暂无正式决策` from `正式决策仓库暂不可用`. Detail renders lists as text, links back to product catalog, and links to its publication/commit metadata without a mutation button.

- [ ] **Step 7: Complete company overview and publication links**

Company overview cards link to each product's Candidate, Decision, and publication pages and display accurate pending/active counts. Recent publications link to `/publications/{publication_id}`. Representative products appear only when present in server mappings; no source file contains a fixed ZStack product array.

- [ ] **Step 8: Run query, API, Registry, and frontend tests**

Run: `python -m unittest tests.test_central_web_queries tests.test_central_web_api tests.test_registry -v`

Expected: available, empty, unavailable, product ownership, filters, and complete-detail tests pass.

Run: `cd web && npm test -- DecisionCatalogPage.test.tsx DecisionDetailPage.test.tsx CompanyOverviewPage.test.tsx`

Expected: global/product catalog, inert content, unavailable state, and overview tests pass.

- [ ] **Step 9: Commit Task 7**

```bash
git add src/zdecision/registry/query.py src/zdecision/central/web web/src/pages/company-overview web/src/pages/decision-catalog web/src/pages/publication-history tests/test_central_web_queries.py tests/test_central_web_api.py
git commit -m "feat: add formal decision catalog and history links"
```

### Task 8: Real vertical acceptance, privacy boundaries, packaging, and Demo runbook

**Files:**
- Create: `tests/integration/test_central_web_vertical.py`
- Modify: `tests/test_central_web_api.py`
- Modify: `web/src/pages/candidate-review/CandidateReviewPage.test.tsx`
- Modify: `web/src/pages/publication-preview/PublicationPreviewPage.test.tsx`
- Create: `docs/demo-central-web.md`
- Regenerate: `src/zdecision/central/static/index.html`
- Regenerate: `src/zdecision/central/static/assets/*`

**Interfaces:**
- Consumes: complete Packet 1 Capture path, all `/api/v1/web` operations, temporary bare Git origin, built SPA, persistent SQLite file, and fixed Demo principal.
- Produces: one automated Gate 1–6 vertical proof, one exact Gate 7 manual runbook, packaged frontend assets, and the final stop decision.

- [ ] **Step 1: Write the failing real temporary-Git vertical test**

```python
def test_candidate_to_product_registry_decision_and_history(self) -> None:
    request_id = self.create_capture_request(REPOSITORY_ID)
    self.agent_upload_and_complete(request_id, candidates=(self.candidate(),))

    draft = self.client.get(f"/api/v1/web/products/{PRODUCT_ID}/review-draft").json()
    saved = self.save_accept_draft(draft, FAMILY_ID)
    review = self.submit_review(saved, "web_action_review_vertical")
    preview = self.create_preview(review["review_batch_id"], "web_action_preview_vertical")

    self.restart_central_service()
    published = self.publish(preview["preview_id"], "web_action_publish_vertical")
    self.assertEqual("completed", published["state"])

    decision_id = published["decision_ids"][0]
    detail = self.client.get(
        f"/api/v1/web/products/{PRODUCT_ID}/decisions/{decision_id}"
    )
    self.assertEqual(200, detail.status_code)
    self.assertEqual(PRODUCT_ID, detail.json()["product_id"])
    self.assertTrue(self.remote_contains(published["commit_sha"]))
    self.assertTrue(self.registry_path(PRODUCT_ID, decision_id).is_file())
```

The fixture creates a temporary working repository, a temporary bare `origin`, exact `main`, Registry root, central SQLite file, registered product repository, and deterministic clock. It must exercise HTTP bodies rather than call Review/Preview/Publication service methods directly.

- [ ] **Step 2: Add restart and negative-boundary assertions**

Extend the integration test to:

```python
self.save_partial_draft_then_restart()
self.assertEqual("accept", self.reloaded_draft_action(FAMILY_ID))
self.inject_crash_after_commit_then_restart()
self.assertEqual(1, self.publication_commit_count(PREVIEW_ID))
self.assertEqual(1, self.receipt_count(FAMILY_ID))
```

Send forbidden `organization_id`, `actor_id`, `product_name`, `registry_path`, `commit_message`, `decision_bytes`, `session_id`, and `prompt` keys to every relevant mutation body and assert 422 without those values in the response. Scan the SQLite file, HTTP fixture JSON, and committed Registry blobs for unique sentinel strings representing a raw Prompt, source code, diff, credential, and local path; all must be absent. Assert rejected/skipped Candidate claims are absent from every Git blob.

- [ ] **Step 3: Run the vertical test and fix only concrete failures**

Run: `python -m unittest tests.integration.test_central_web_vertical -v`

Expected: one complete enabled-repository path passes, including both service restarts, exact remote commit proof, product folder, Decision detail, and publication history.

If this test fails, change only code necessary for the observed Gate 1–6 failure and rerun this single test. Do not start a general code-review loop.

- [ ] **Step 4: Finish frontend security and state tests**

```tsx
it("never executes Candidate markup", async () => {
  mockCandidate({ claim: '<button onclick="fetch(\'/secret\')">run</button>' });
  renderCandidatePage(PRODUCT_ROUTE);
  expect(await screen.findByText(/onclick=/)).toBeVisible();
  expect(document.querySelector("button[onclick]")).toBeNull();
});

it("restores the same preview after browser remount", async () => {
  const first = renderPreviewPage(PREVIEW_ROUTE);
  expect(await screen.findByText(PREVIEW_ID)).toBeVisible();
  first.unmount();
  renderPreviewPage(PREVIEW_ROUTE);
  expect(await screen.findByText(PREVIEW_ID)).toBeVisible();
});
```

Run: `cd web && npm run typecheck && npm test`

Expected: all frontend tests pass with no React unsafe-HTML API in `web/src`.

Run: `rg -n "dangerouslySetInnerHTML|innerHTML|eval\\(|new Function" web/src`

Expected: no output.

- [ ] **Step 5: Build and verify packaged browser routes**

Run: `cd web && npm run build`

Expected: Vite recreates `src/zdecision/central/static/index.html` and hashed assets.

Run: `python -m unittest tests.test_update_candidates_page tests.test_central_web_api -v`

Expected: root and every product/preview/publication/Decision browser route return the same SPA entry; `/api/...` misses remain JSON 404; Capture Request compatibility still passes.

- [ ] **Step 6: Write the exact Demo runbook**

`docs/demo-central-web.md` must contain these executable commands for the current Demo checkout:

```bash
export ZDECISION_REPO=/Users/zhaohuiying/Desktop/Zstack-repos/zdecision
export ZDECISION_DEMO_DIR=/Users/zhaohuiying/.zdecision/demo
cd "$ZDECISION_REPO"
python -m zdecision.central.cli run \
  --database "$ZDECISION_DEMO_DIR/central.sqlite3" \
  --config "$ZDECISION_DEMO_DIR/central.json" \
  --registry-repository-root "$ZDECISION_REPO" \
  --host 127.0.0.1 \
  --port 8765
```

Then document the exact visible sequence: Codex card **更新候选决策** -> default browser product Inbox -> partial accept/reject/skip -> save/restart/restore -> submit -> exact preview -> one confirmation click -> publication state -> product-isolated Decision detail -> history. State plainly that SSO, Git-role authorization, Decision updates, and automatic recall are not demonstrated.

- [ ] **Step 7: Run one focused full backend suite and one frontend suite**

Run: `python -m unittest discover -s tests -v`

Expected: all tests pass; existing live-environment skips remain skips, not failures.

Run: `cd web && npm run typecheck && npm test && npm run build`

Expected: typecheck, all Vitest tests, and production build pass.

- [ ] **Step 8: Perform one bounded real Demo and visual pass**

Start the loopback service from the runbook. In an enabled Git repository, ask Codex to show **更新候选决策**, click **所有有效 Session**, and verify the default browser opens that repository's product Inbox without a Session ID. Complete one partial Review, close/reopen the browser before submission, create the exact preview, click the single publish action, and follow the resulting Decision and publication-history links. This is the Gate 7 functional acceptance; record the request, Review, preview, publication, Decision, and commit IDs in the handoff.

During that one flow, inspect exactly these routes at desktop width and one narrow width: `/`, `/reviews`, the product Candidate page, the preview, `/decisions`, the Decision detail, `/publications`, and the publication detail. Check ZStack mark fidelity, navigation selection, text wrapping, keyboard focus, disabled/stale/pending/ambiguous contrast, and absence of horizontal overflow. Fix only visible defects on those routes, rerun the affected frontend test and `npm run build`, then stop visual iteration.

- [ ] **Step 9: Verify diff scope and commit Task 8**

Run: `git diff --check`

Expected: no output.

Run: `git status --short`

Expected: only the integration test, frontend test/build outputs, runbook, and concrete Gate fixes from this task are modified.

```bash
git add docs/demo-central-web.md tests/integration/test_central_web_vertical.py tests/test_central_web_api.py web/src src/zdecision/central/static
git commit -m "test: verify central decision web demo"
```

## Execution Checkpoints and Stop Rule

- After Tasks 2, 4, 6, and 8, report the focused test evidence and wait for the next execution checkpoint if the user is supervising interactively.
- Do not dispatch a wide architecture or code review after each task. A task's focused tests are its gate.
- Run the complete Python and frontend suites only in Task 8, unless a concrete cross-cutting regression requires an earlier run.
- Treat `--help` copy, SSO, authorization, automatic recall, production deployment, and Decision lifecycle as non-blocking follow-up work.
- After Task 8 passes, report remaining deferred risks once and stop. Push only when the user explicitly asks.
