# Central Decision Web Application Design

**Status:** Approved for implementation planning.

**Scope:** Define Packet 2 of the active Plugin architecture: the shared,
product-isolated Web application from synchronized Candidate revisions through
explicit Review and publication to browsable formal Decisions.

**Authority:** `docs/architecture.md` remains the product architecture authority.
This design implements its Packet 2 boundary and the Candidate Review and
publication contract in
`docs/superpowers/specs/2026-07-30-on-demand-candidate-refresh-design.md`.
Specifications marked Superseded remain historical evidence and do not govern
this implementation.

## 1. Product goal

ZDecision Central is the company-wide place to discover, review, publish, and
browse durable product Decisions. It is not merely a Candidate-refresh page and
it is not a general-purpose document editor.

The complete product direction contains three distinct experiences:

1. **Company overview:** every company member can discover products, formal
   Decisions, pending Review volume, and recent publications.
2. **Product workspace:** authorized product members review current Candidate
   revisions, inspect exact publication previews, and publish accepted items.
3. **Decision catalog:** company members search and read product-scoped formal
   Decisions without receiving their source conversations or private Review
   content.

The current technical Demo proves the functional path before company identity
is connected. It uses one fixed trusted browser principal. A later authentication
adapter will allow all authenticated company members to read formal Decisions
and will derive a product member's Review/publication authority from Developer
membership in a registered Git repository for that product. Deferring that
adapter must not weaken product isolation or allow the client to choose an
organization, actor, or publication target.

## 2. Current state and migration boundary

The current loopback page is Packet 1's technical Demo. It lists registered
repositories, starts Candidate refresh, follows request progress, and renders
current Candidate content. It intentionally has no accept, reject, preview,
publish, Decision-board, or publication-history behavior.

Packet 2 replaces that single-page product surface with a real multi-route Web
application while preserving the existing Packet 1 APIs used by the Plugin and
persistent Agent. The existing Candidate synchronization tables and request
state remain inputs to the new Candidate Inbox.

The product Candidate page retains Packet 1's explicit **更新候选决策** action.
For a product with multiple registered repositories, the user first selects one
repository; a trusted repository deep link preselects it. One click creates the
existing durable `all_valid_sessions` request for that repository. The page
restores request progress after navigation or restart and never accepts a
Session ID. This Capture control and Review remain separate actions.

The new Web does not call the historical Review/Publish CLI. It reuses the
validated domain values and invariants behind Review, Promotion, Registry,
stable IDs, exact-byte previews, Git recovery, and formal Decision V1, but gives
them central persistence and browser-facing application services.

## 3. Confirmed product decisions

The following decisions are fixed for this Demo:

- The default route is a company overview, not a reviewer-only Inbox.
- The application uses one shared shell with company-level navigation and
  product workspaces.
- Every product workspace has Candidate Review, formal Decisions, and
  publication history.
- Every Candidate supports `accept`, `edit_accept`, `reject`, and `skip`.
- A Review may classify only part of the current Inbox.
- An accepted subset may proceed while unclassified and skipped Candidates
  remain available for later Review.
- A Review batch and publication batch belong to exactly one product.
- Accepting a Candidate never publishes it.
- Publication requires an immutable, independent preview page followed by one
  explicit publish click. The preview page itself is the confirmation step; no
  additional confirmation modal is used.
- Formal Decisions are read-only in this Demo. Direct edit and delete are not
  supported.
- Formal Decision files are physically partitioned by stable product ID.
- Company SSO, Git-role authorization, Decision update/retirement, comments,
  notifications, multi-level approval, and automatic Decision recall are not
  part of this Demo.

## 4. Information architecture

### 4.1 Shared application shell

The persistent navigation contains:

- **公司总览** — organization-level product summary and recent publications;
- **候选审核** — cross-product entry to pending Candidate work;
- **正式决策** — cross-product searchable formal Decision catalog; and
- **发布历史** — global publication batches and their current state.

The shell uses the approved ZStack company mark followed by the product name
`ZDecision`. Production implementation must use an approved vector or
high-resolution brand asset; the small bitmap supplied for the concept mockup
is not a shippable source asset.

The visual language follows current ZStack enterprise products: a restrained
dark navigation rail, light content surfaces, cobalt-blue primary actions,
one-pixel separators, dense but readable information, and clear Chinese
labels. Generated concept mockups communicate hierarchy only and are not
pixel-perfect acceptance artifacts.

### 4.2 Company overview

The default route `/` shows:

- product count;
- pending current Candidate count;
- active formal Decision count;
- publications completed in the current week;
- one card or row per enabled product;
- per-product pending Candidate and active Decision counts;
- per-product last activity; and
- a recent-publication feed.

Products are derived from server-side registered repository mappings. Folder
names from `zstack-ui-next/packages/products` such as ZStack Cloud, ZStack AI
Studio, ZMetis, ZNS, ZIAM, ZStone, ZSV, ZCF Installer, Portal, and Lifecycle are
representative Demo fixtures, not a hard-coded product catalog.

Selecting a product opens `/products/{product_id}/candidates`. When the Plugin
opens the existing `?repository_id=...` route, the server resolves the registered
repository to its product and routes to that product's Candidate page with the
repository filter applied. An unknown or disabled repository never selects a
product supplied by the browser.

### 4.3 Global indexes and product workspaces

Global indexes aggregate only read models. They never create a cross-product
Review or publication:

- `/reviews` groups current Candidate revisions by product;
- `/decisions` searches active Decisions across products; and
- `/publications` lists publication batches across products.

Product routes are:

- `/products/{product_id}/candidates`;
- `/products/{product_id}/decisions`;
- `/products/{product_id}/decisions/{decision_id}`; and
- `/products/{product_id}/publications`.

Publication preview and batch-detail routes use their stable identity:

- `/publication-previews/{preview_id}`; and
- `/publications/{publication_id}`.

The product identity is always a path-safe `prod_<32-hex>` value. Human product
names are display data and never raw filesystem path components.

## 5. Candidate Inbox and Review

### 5.1 Inbox content

The Inbox displays only validated, non-ambiguous current Candidate family heads
for the selected product. It supports search and filters for repository,
Capture batch, and Review state. Candidate content is rendered as text, never
as HTML.

The page header contains the product's registered-repository selector and
**更新候选决策**. While one repository request is active, the page shows its
durable progress and prevents a second active request for that repository. A
completed request refreshes the Inbox from synchronized current heads; it does
not automatically classify or publish them.

Each row exposes the safe decision fields needed for Review:

- claim;
- future action;
- scope summary, repositories, and paths;
- invalidation conditions;
- current Candidate identity, revision, and digest; and
- safe repository/Capture metadata without native Session or Turn identity.

The central Web never receives raw Sessions, Prompts, tool output, code, diffs,
or local paths.

### 5.2 Review actions

Every current Candidate has exactly one optional action:

- `accept` freezes the current Candidate content unchanged;
- `edit_accept` freezes complete edited content;
- `reject` records that the current revision must not be promoted; and
- `skip` explicitly leaves the current revision for later Review.

`edit_accept` cannot change product. Edited repository scope may reference only
repositories already registered to the same product. Candidate scope paths are
semantic Decision content and never control Registry target paths. All edited
content passes the existing strict Candidate-content validation before Review
is recorded.

Reject may be performed in one click. An optional note may be retained in the
central Review record, but it is not part of the formal Decision or Registry.

### 5.3 Partial Review and drafts

A user does not need to classify the entire Inbox. The Web stores a mutable
Review draft so navigation or a central-service restart does not lose selected
actions. Saving a draft is not Review approval and cannot create a preview.

Submitting classified items creates one immutable Review batch for exactly one
product:

- accepted and edit-accepted items become eligible for preview;
- rejected items move to the processed view and remain auditable;
- skipped and unclassified items remain available in the pending Inbox; and
- published Candidate families remain visible only through their formal
  Decision and history links.

If the submitted batch has accepted items, the primary action is **生成发布预览**.
If it contains only reject/skip actions, the action is **提交审核结果** and no
preview is created.

### 5.4 Submission consistency

The browser submits an opaque action ID plus ordered Candidate identity,
revision, digest, action, and complete effective content where required. The
server derives the actor and product, reloads every current family head, and
validates the complete batch before writing any immutable Review item.

If any Candidate is missing, no longer current, belongs to another product, has
a different digest, or contains invalid edited content, the immutable Review
submission is all-or-nothing. The draft remains available so the page can mark
the stale item and let the user reconcile it.

Replaying an identical action returns the original Review result. Reusing an
action ID with different bytes is a conflict.

## 6. Publication preview

### 6.1 Independent page

An accepted Review does not publish. The Preview Service creates one immutable
preview for the accepted subset, then the browser navigates to an independent
page. A drawer or modal is insufficient because every complete Decision and
publication target must remain inspectable.

The page contains:

- product and accepted-item count;
- every complete formal Decision in readable field form;
- an expandable canonical JSON representation for exact-byte inspection;
- Decision IDs and revisions;
- exact Registry target paths;
- resulting product/root Registry indexes;
- base commit and pre-publication Registry digests;
- preview ID and batch content digest;
- exact commit message and changed-file list;
- **返回修改审核**; and
- **确认发布 N 条决策**.

Rejected, skipped, and unclassified Candidate content is absent from the
preview. The preview stores final Decision bytes before the publish action; a
later confirmation never changes them.

### 6.2 Staleness

A preview is publishable only while all accepted Candidate revisions, their
latest Review results, and the Registry base remain equal to its frozen input.
A newer Candidate Review invalidates older unpublished previews containing that
Candidate. Changed Registry state also makes a preview stale.

A stale preview is immutable evidence. The page disables publication and links
back to Review; it never silently refreshes under the same preview ID.

## 7. Publication execution and history

### 7.1 Explicit action

The independent preview is the human confirmation surface. One click on
**确认发布 N 条决策** submits only the preview ID and an opaque stable Web action
ID. There is no additional modal and the browser supplies no Decision payload,
product, path, commit message, organization, or actor.

The Publication Service persists confirmation before Git mutation and consumes
only the frozen preview files. One product batch creates one commit containing
independent Decision revisions and required product/root indexes.

### 7.2 Durable states

Publication uses the proven monotonic states:

```text
previewed
  -> confirmed
  -> committed_pending_push
  -> completed
```

The UI renders these as:

- **准备提交** for confirmed work before a commit is proven;
- **已提交，等待推送** for `committed_pending_push`;
- **发布完成** for `completed`; and
- **需要人工处理** when Git state is ambiguous.

The server may retry a known-safe push of the same commit. It never creates a
second Decision, preview, or commit to resolve an unknown outcome. Recovery may
adopt only the exact one-parent commit whose parent, message, changed path set,
and blobs equal the preview. Every other shape stops as ambiguous.

The browser does not claim success until the server proves the publication
commit is present on `origin/main`.

### 7.3 History

Global and product-scoped history are two views of the same durable publication
records. Each row shows product, batch identity, Decision count, actor, approval
time, state, and commit SHA when known. Batch detail links to every formal
Decision and shows safe recovery status.

The Demo actor is a fixed trusted principal. Later identity integration replaces
that principal without changing publication records or action contracts.

Review history and publication history remain distinct. Rejected and skipped
Candidate content is available only in authorized product Review history and
never appears in company-wide formal Decision or publication views.

## 8. Formal Decision catalog

### 8.1 Registry ownership

Git remains the formal source of truth. The physical layout is:

```text
decision-registry/
├── registry.json
└── products/
    └── prod_<32-hex>/
        ├── product.json
        ├── registry.json
        └── decisions/
            └── dec_<32-hex>/
                └── r0001.json
```

Every new publication writes only its own product directory plus the root
product index when needed. A Review or publication cannot contain Candidate
items from two products. Product names never become filesystem directories.

Central database rows for Candidate, Review, preview, publication, and derived
Decision indexes all carry `product_id`; uniqueness and foreign-key ownership
include it. Global pages aggregate explicit product-scoped queries and do not
erase ownership.

### 8.2 Browsing

The Decision index defaults to `active` formal revisions and supports keyword,
repository, and publication-time filters. Each row shows claim, future action,
scope, revision, product, and publication time.

Decision detail shows the complete formal document, product, revision,
lifecycle, scope, invalidation conditions, safe provenance, preview ID,
publication batch, and commit. Formal content is read-only.

This Demo creates only initial `revision: 1`, `lifecycle: active` Decisions.
Direct Web edit/delete, revision creation, supersede, and retirement are deferred
to a later explicit Decision-lifecycle design.

Registry unavailable is not rendered as an empty catalog. A read model is tied
to a proven Registry commit; stale or unavailable state is visible.

## 9. Component architecture

### 9.1 Frontend

Create a dedicated React/TypeScript application in the ZDecision repository.
FastAPI serves its compiled static assets and browser-route fallback. The Web is
an independent deployment unit from `zstack-ui-next`; it may later consume
published ZStack components or tokens without depending on that entire monorepo
during the Demo.

Suggested source ownership is by shared feature, not by copying code once per
product:

```text
web/
├── src/app/
├── src/pages/
│   ├── company-overview/
│   ├── candidate-review/
│   ├── publication-preview/
│   ├── decision-catalog/
│   └── publication-history/
├── src/features/
│   ├── products/
│   ├── candidates/
│   ├── reviews/
│   ├── publications/
│   └── decisions/
└── src/shared/
```

Products are data, not duplicated frontend implementations. Physical
product-directory isolation applies to formal Registry data.

### 9.2 Central application services

FastAPI remains the browser boundary and composes:

- **Product Catalog** from registered repository mappings;
- **Candidate Inbox** from synchronized current family heads;
- **Review Service** for mutable drafts and immutable product Review batches;
- **Preview Service** for exact formal bytes and stale checks;
- **Publication Service** for confirmation, Git recovery, and history;
- **Decision Query** for product-scoped formal Registry reads; and
- **Dashboard Query** for explicit cross-product read models.

The browser API never exposes the Agent lease channel and never accepts a raw
Registry file map.

### 9.3 Persistence

The existing central SQLite store is extended with product-owned Review draft,
immutable Review batch/item, publication preview/file, publication state, and
Candidate-to-Decision receipt records. Exact schema names are implementation
details, but the following ownership is not:

- drafts are mutable and actor/product scoped;
- submitted Review batches are append-only;
- previews are immutable;
- publication state advances monotonically;
- Candidate publication receipts prevent a second initial Decision; and
- formal Decision bytes live only in Git.

No transaction pretends SQLite and Git are atomic. Durable publication states
and exact commit adoption bridge the boundary.

## 10. Browser API behavior

Packet 1's existing repository, Candidate, and Capture Request APIs remain
compatible. New browser operations live under `/api/v1/web` so they cannot be
confused with the authenticated Agent lease channel:

- `GET /api/v1/web/dashboard` reads company and product summaries;
- `GET /api/v1/web/products/{product_id}/candidates` reads current product
  Candidate revisions with bounded filters;
- `GET /api/v1/web/products/{product_id}/review-draft` reads the fixed
  principal's current product draft;
- `PUT /api/v1/web/products/{product_id}/review-draft` saves a draft with an
  expected draft-version CAS;
- `POST /api/v1/web/products/{product_id}/reviews` submits one immutable Review
  batch using a stable action ID;
- `POST /api/v1/web/reviews/{review_batch_id}/previews` creates or returns one
  exact preview using a stable action ID;
- `GET /api/v1/web/publication-previews/{preview_id}` reads the frozen preview
  and current publishability;
- `POST /api/v1/web/publication-previews/{preview_id}/publish` confirms one
  frozen preview using a stable action ID;
- `POST /api/v1/web/publications/{publication_id}/resume` retries only the
  known-safe frozen publication;
- `GET /api/v1/web/publications` reads global or product-filtered history;
- `GET /api/v1/web/decisions` reads the global or product-filtered catalog; and
- `GET /api/v1/web/products/{product_id}/decisions/{decision_id}` reads one
  product-owned formal Decision.

The existing `POST /api/v1/capture-requests` remains the browser's explicit
repository refresh action. It accepts the server-validated repository ID,
`business` template, `all_valid_sessions` scope, and stable Web action ID. It
does not accept product, organization, actor, or Session identity.

Every mutating operation uses an opaque client action ID with identical-replay
semantics. Every response exposes a bounded stable state and safe identifiers.
Candidate text, edited Review text, and Decision text remain untrusted response
data.

The Demo uses a fixed server-derived organization and actor. Requests containing
client-writable organization, actor, product-name authority, Git path, commit
message, or formal Decision bytes are rejected. A product ID in a route scopes a
read or intent; the service still proves every referenced object's product
ownership.

## 11. Failure behavior

The Web maps stable server outcomes to explicit recovery:

| Condition | UI behavior | Mutation rule |
| --- | --- | --- |
| Candidate revision or digest changed | Mark item **已有新版本** and retain the draft for reconciliation | Write no immutable Review batch |
| Cross-product Candidate selection | Reject the submission and identify offending safe IDs | Write nothing |
| No accepted items | Submit reject/skip Review result without a preview | Write no publication state |
| Registry unavailable during preview | Preserve the Review and offer preview retry | Write no Registry files |
| Preview input or base changed | Mark preview **已过期** and disable publish | Create a new preview only after new user action |
| Commit created; push interrupted | Show **已提交，等待推送** | Retry only the same commit |
| Commit or remote outcome ambiguous | Show **需要人工处理** | Never auto-create another commit |
| Central service restarts | Restore draft, preview, and publication state | Continue from durable state |
| Formal Registry unavailable | Show unavailable/stale catalog state | Never report an empty Registry |

Normal empty states—no Candidates, no accepted items, no Decisions, or no
publication history—are distinct from service failures.

## 12. Security and privacy

- Raw Sessions, Prompts, tool output, code, diffs, credentials, and local paths
  never enter the central service.
- Candidate, edited Review, and formal Decision text is rendered as inert text.
- Product and Registry paths are server-derived and validated.
- The client cannot publish arbitrary JSON or choose a Git command.
- Git is invoked with argument arrays and exact owned paths; no shell command is
  constructed from browser data.
- Rejected Candidate content and Review notes never enter Git.
- The Demo service remains loopback-only with a fixed trusted principal.
- Later SSO and repository Developer-role authorization is an adapter at the
  browser principal and product-authorization boundary, not a rewrite of Review
  or Publication.

## 13. Technical approach alternatives

Three approaches were considered:

1. **Independent React application in ZDecision — selected.** It supports the
   required multi-page workflow and future authentication while keeping the
   product independently deployable.
2. **Extend the existing single HTML file — rejected.** It is fast for one
   control but cannot keep Review, routing, preview, and recovery state legible.
3. **Build directly inside `zstack-ui-next/packages/products` — deferred.** It
   maximizes immediate monorepo reuse but couples this functional Demo to a much
   larger build, release, and authentication system before the workflow is
   proven.

The selected application may adopt published ZStack design packages later. It
does not copy the `zstack-ui-next` product source tree.

## 14. Demo scope and stopping rule

### 14.1 Included

- company overview and product navigation;
- global and product Candidate Inbox;
- partial batch Review and saved drafts;
- exact independent publication preview;
- explicit publication and crash-safe recovery;
- product-isolated formal Registry writes;
- global and product Decision catalog/detail;
- global and product publication history; and
- the real Codex-card-to-browser-to-Registry vertical path.

### 14.2 Deferred

- company email SSO and Git Developer-role synchronization;
- multiple organizations;
- Decision update, supersede, retirement, and relation editing;
- comments, discussions, notifications, and multi-level approval;
- general administration and analytics;
- production deployment topology; and
- Packet 3 automatic Decision recall and Codex injection.

Implementation stops when the included vertical path and acceptance gates pass.
Deferred items do not become blockers or opportunistic additions.

## 15. Acceptance gates

### Gate 1: Application shell and product routing

- The company overview lists server-derived enabled products and accurate
  summary counts.
- Real representative products render without hard-coded per-product pages.
- A trusted `repository_id` deep link resolves to its product Candidate page.
- Unknown and disabled repositories cannot select another product.
- A selected registered repository can start and restore exactly one existing
  durable Candidate refresh without supplying a Session ID.

### Gate 2: Product-isolated Review

- The Inbox shows only current Candidate revisions for one product.
- Accept, edit-accept, reject, skip, saved draft, and partial submission work.
- A product-changing edit and any cross-product selection fail without an
  immutable Review write.
- A changed Candidate revision invalidates submission without partially
  recording the batch.

### Gate 3: Exact preview

- Only accepted effective content enters the preview.
- Every complete Decision, target path, Registry index, digest, base commit,
  and commit message is inspectable.
- Preview creation changes no Registry file.
- New Review or Registry state makes the preview stale and unpublishable.

### Gate 4: Publication and recovery

- One explicit click publishes the frozen preview as exactly one product-correct
  commit.
- Identical retry returns or resumes the same publication.
- Conflicting action reuse fails.
- Crash points before commit, after commit, and before/after push recover the
  exact publication or stop ambiguous without a second Decision or commit.
- Formal files round-trip through existing V1 Registry readers byte-for-byte.

### Gate 5: Decision and history views

- Published Decisions appear under the correct product directory and nowhere
  else.
- Product and global indexes return the same formal revision under explicit
  product ownership.
- Decision detail displays the complete read-only formal document.
- Publication history exposes durable state and links to the exact Decision and
  commit.
- Registry unavailable is visibly distinct from an empty Registry.

### Gate 6: Privacy and negative boundaries

- Browser requests cannot set organization, actor, product authority, Registry
  path, commit message, or formal Decision bytes.
- Candidate content cannot execute markup or browser instructions.
- Central persistence and HTTP fixtures contain no raw Session, Prompt, source
  code, diff, credential, or local path.
- Rejected and skipped content never enters Git.

### Gate 7: Real functional Demo

From an enabled repository:

```text
Codex Candidate refresh
  -> central product Candidate Inbox
  -> partial Review
  -> exact preview
  -> one explicit publication
  -> product-isolated Git Decision
  -> Decision catalog/detail
  -> publication history
```

The browser is restarted during one in-progress Review and one publication
recovery scenario to prove state restoration. Identity is the fixed Demo
principal; SSO and Git-role checks are not simulated or claimed complete.
