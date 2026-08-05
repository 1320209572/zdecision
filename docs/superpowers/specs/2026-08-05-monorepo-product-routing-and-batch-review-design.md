# Monorepo Product Routing and Batch Candidate Review Design

**Status:** Approved for implementation planning.

**Scope:** Correct the Central Web product model for repositories that contain
multiple products, define honest handling for shared code, and replace the
current high-friction Candidate cards with an efficient batch Review workflow.

**Authority:** This design supersedes the one-repository-to-one-product routing
and per-Candidate Review-action UI described in
`2026-08-04-central-decision-web-design.md`. Existing Capture privacy,
Candidate validation, Review evidence, publication Preview, explicit publish,
and product-isolated Registry contracts remain authoritative unless this
document explicitly changes them.

## 1. Confirmed corrections

The current Demo made two incorrect assumptions:

1. It treats an enabled Git repository as one product. In reality,
   `zstack-ui-next` is a single code repository containing multiple products,
   including Cloud, ZNS, ZMetis, ZIAM, Portal, IDP, Lifecycle, and other
   explicitly registered product areas.
2. It renders every Candidate as a large always-expanded card and asks the user
   to choose a Review action from a select element. That interaction is slow to
   scan and unnecessarily expensive for repeated approval work.

The corrected rules are:

- a Git repository is a source-code container, not a product identity;
- one repository may expose many server-registered Decision routes;
- a route belongs either to one product or to the explicit **Shared** Decision
  space;
- shared code is described honestly as Shared and is not silently copied to
  every product;
- Candidate Review is a compact selectable list with direct actions and batch
  operations; and
- Review submission and publication remain separate explicit boundaries.

The previously observed Dashboard loading delay is a separate performance
defect. It is not solved by, and must not distort, this functional correction.

## 2. Repository, product, and Shared model

### 2.1 Decision spaces

The Central Web presents two kinds of Decision space:

- `product`: a real company product such as Cloud, ZNS, or ZMetis;
- `shared`: common code and policy that intentionally applies as shared
  infrastructure rather than belonging to a single product.

Every Decision space has a stable server-owned ID, canonical display name,
kind, enabled state, and Registry partition. The existing V1 Registry
`product_id` remains the compatibility partition key for this Demo. A Shared
space therefore receives its own stable partition key, but every user-facing
surface labels it **Shared / 共享**, never as a sellable product.

Company metrics count only spaces whose kind is `product` as products. Shared
appears in a distinct section and has its own Candidate, Decision, and
publication counts.

### 2.2 Repository routes

An enabled repository owns one or more immutable-versioned route records:

```text
RepositoryDecisionRoute
├── route_id
├── repository_id
├── decision_space_id
├── path_prefixes
├── excluded_prefixes
├── enabled
└── configuration_version
```

For `zstack-ui-next`, representative explicit routes are:

```text
packages/products/cloud/**    -> Cloud
packages/products/zns/**      -> ZNS
packages/products/zmetis/**   -> ZMetis
packages/products/ziam/**     -> ZIAM
packages/products/portal/**   -> Portal
packages/products/idp/**      -> IDP
packages/products/lifecycle/** -> Lifecycle
packages/products/zstone/**   -> ZStone, when explicitly registered
packages/products/shared/**   -> Shared
packages/shared/**            -> Shared
```

Directory names are not a universal source of truth. During repository
onboarding the system may inspect tracked workspace manifests and Nx
`product:*` tags to propose routes, but an administrator-owned persisted route
configuration is authoritative. Untracked build output, ignored directories,
package names, and model-generated text never create a product automatically.

Explicit registration may add a known company product even when the current
checkout lacks a tracked product manifest. Such a route simply produces no
Candidate until trusted tracked changes match it.

### 2.3 Shared and multi-product code

Paths registered as Shared always route to the Shared Decision space. They do
not fan out into Cloud, Portal, or every package that imports them.
Only a trusted Git path match against a registered Shared route may create a
Shared Capture slice. The extraction model cannot promote product-routed work
to Shared merely because its conclusion sounds broadly applicable.

One development task may touch several product routes and Shared at the same
time. The system does not force that task into one repository-default product
and does not ask the extraction model to guess. It creates one product-scoped
Capture slice for every trusted route matched by the task:

```text
one Update action
  -> repository Capture group
     -> Cloud Capture slice
     -> Third-party Services Capture slice
     -> ZMetis Capture slice
     -> Shared Capture slice, if shared paths were touched
```

Each slice retains the existing fixed-product extraction contract. A formal
Candidate and Decision belong to exactly one Decision space. A genuinely
cross-product conclusion is expressed as separate product conclusions or as
one Shared conclusion; V1 does not publish one Decision into multiple Registry
partitions.

## 3. Trusted routing and Capture flow

### 3.1 Routing authority

Product routing uses locally observed, repository-relative Git path evidence:

- verified changed paths associated with the frozen task boundary;
- committed paths when a matching commit exists; and
- the registered route configuration version used for the Capture.

The model-generated `CandidateContent.paths` field is useful for display,
search, and consistency checks after routing. It may be empty, edited during
Review, or incorrect, so it never selects a product.

The central service continues to derive organization, actor, repository,
Decision space, and product name from authenticated server state. The client
may send a server-issued route or Capture identity, but cannot authoritatively
set those fields.

### 3.2 Capture outcomes

A single user Update action remains repository-scoped and produces one durable
Capture group.

- One matched route creates one Capture slice.
- Several matched routes create several independent slices under the same
  group and run without another user prompt.
- Shared-only changes create a Shared slice.
- No matched route produces a successful `no_routable_product_changes` result
  and no Candidate.
- A route configuration that is missing, disabled, malformed, or ambiguous
  fails closed before extraction.

The local Agent partitions eligible source work before extraction. It may use
the same frozen Session boundary as evidence for more than one slice, but each
slice receives only one fixed Decision-space instruction and produces only
Candidates for that space. Raw Session, Prompt, Diff, and source files remain
local as before.

### 3.3 Frozen Candidate ownership

Every Capture slice and synchronized Candidate revision freezes:

- `repository_id`;
- `route_id` and route configuration version;
- Decision-space ID, kind, and canonical name; and
- the source boundary used for extraction.

Candidate-family reconciliation is partitioned by repository and Decision
space. Changing a repository route later never moves an existing Candidate to
another product. Historical Review and publication evidence continues to point
to the frozen route.

## 4. Central Web information architecture

### 4.1 Company overview

The default Dashboard treats products as its primary rows. Repository identity
is secondary provenance.

Each product row displays:

- product name;
- pending Candidate count;
- active Decision count;
- latest activity; and
- links to Candidate Review, formal Decisions, and publication history.

An expandable provenance line may show the source repository and registered
path prefixes, for example `zstack-ui-next · packages/products/cloud/**`.
Shared is rendered in a separate **共享决策空间** section with the same three
destination links.

The Dashboard must never show `zstack-ui-next` itself as a product merely
because it is an enabled repository.

### 4.2 Repository deep links

A repository deep link may resolve to several Decision spaces. Therefore
`?repository_id=...` no longer guesses one product.

- If a trusted Capture group is known, the page opens the corresponding Review
  index filtered to that group and groups results by Decision space.
- If only the repository is known, the page opens a repository-scoped Review
  index showing its enabled product and Shared workspaces.
- A product page is opened directly only when the URL already contains a
  server-resolved Decision-space identity.

## 5. Candidate Review interaction

### 5.1 Compact Review list

The approved Candidate page is a compact product-scoped list, not a stack of
always-expanded cards. Every row shows only the information needed to make a
first-pass decision:

- selection Checkbox;
- Decision claim;
- short future-action or scope summary;
- registered product or Shared label and relevant path summary;
- Capture time or batch;
- current draft/Review state; and
- direct `接受`, `拒绝`, and `编辑` actions.

Revision IDs, content digests, Capture Request IDs, full invalidation
conditions, and other technical provenance live behind **查看证据**. Editing one
Candidate opens an inline detail area or side panel; it never expands every row
at once. The Decision space and repository remain locked during editing.

### 5.2 Selection is not approval

Checkbox state and Review state are separate:

- checking a row only selects it for a batch action;
- `接受` or `批量接受` writes an `accept` draft action;
- `拒绝` or `批量拒绝` writes a `reject` draft action;
- `编辑` creates `edit_accept` only after the edited content is valid;
- an unclassified row is simply unprocessed.

The current `skip` value may remain readable for compatibility, but the primary
UI does not expose a Skip command. Leaving a Candidate unclassified is the
lower-effort and clearer equivalent.

### 5.3 Batch toolbar and feedback

When at least one visible row is selected, a contextual toolbar appears:

```text
已选 N 条    批量接受    批量拒绝    清除选择
```

Batch actions update the private draft immediately; they do not submit Review
or publish. The page gives exact feedback and one-click undo, for example
`已将 6 条标记为接受 · 撤销`. Product navigation or a material filter change
clears transient selection but never discards saved or local draft actions.

The page follows the server's bounded Review-batch limit. If the current limit
is 20, selection and submission explain that limit rather than silently
truncating the user's choices.

### 5.4 Review submission

A persistent summary shows the product-wide draft state:

```text
已接受 8 · 已拒绝 3 · 未处理 3    保存草稿    提交审核（11）
```

Submission contains only explicitly classified, current Candidate revisions.
Accepted items become eligible for an immutable publication Preview; rejected
items record Review evidence; unprocessed items remain in the Inbox. Review
submission never publishes. The existing independent Preview page and explicit
publish click remain required.

If a selected or classified Candidate has a newer revision, it is marked stale
and excluded from submission until the user loads and reviews the current
revision. Draft version conflicts preserve both sides and offer an explicit
reload/reconcile action.

## 6. Required states

The Company and Candidate pages define and test:

- initial loading with a bounded recovery path;
- no registered product routes;
- no routable changes;
- empty and filtered-empty Candidate results;
- normal, selected, accepted, rejected, edited, and stale rows;
- partially classified drafts;
- batch action success with undo;
- draft save conflict;
- Review submission in progress, success, and retryable failure; and
- product, Shared, repository, and Capture-group navigation.

Checkbox labels are clickable, keyboard focus is visible, selection is
announced accessibly, and direct Review actions remain usable without a mouse.

## 7. Migration

The migration is additive and does not guess historical ownership:

1. Introduce Decision spaces and repository routes while preserving stable
   existing product IDs.
2. Convert a legacy one-product repository to one root route only when it truly
   remains a single-product repository.
3. Replace the generic `ZStack UI Next` mapping with explicit product and Shared
   routes.
4. Add frozen route/Decision-space ownership to Capture and Candidate state.
5. Existing unpublished `ZStack UI Next` Candidates are archived from the new
   product Inboxes and recaptured under trusted routes; their model-generated
   paths are not used for automatic migration.
6. Existing formal Decisions remain readable under their original Registry
   partition until a separate explicit migration is approved.

## 8. Non-goals

This slice does not add:

- an administrator UI for repository-route discovery or editing;
- model-based product classification;
- automatic broadcast of Shared decisions to products;
- one formal Decision owned by several Registry partitions;
- SSO, Git-role authorization, comments, or multi-level approval;
- a redesign of publication Preview or formal Decision pages; or
- the separate Dashboard Git-fetch performance fix.

Demo route configuration may be maintained in trusted server configuration.
A later administration surface can manage the same contract without changing
Capture or Review semantics.

## 9. Acceptance criteria

The design is implemented only when all of the following are demonstrated:

1. One registered repository can expose Cloud, ZNS, another product, and Shared
   as separate Decision spaces without duplicate repository identities.
2. The Company overview counts and lists products rather than repositories and
   presents Shared separately.
3. Trusted Cloud, ZNS, and Shared path changes in one completed task create the
   corresponding independent Capture slices from one Update action.
4. No route match creates no Candidate; client or model product claims cannot
   override the server route.
5. Route changes never move an existing Candidate or Review to another product.
6. Repository-only deep links never guess one product in a multi-product repo.
7. Every Candidate row has a Checkbox and direct Accept, Reject, and Edit
   actions; no Review-action select/dropdown is rendered.
8. Selecting several rows and applying Accept or Reject changes exactly those
   draft items, reports the result, and supports undo before submission.
9. Technical provenance is available on demand but does not dominate the
   default list.
10. Review submits the classified subset exactly once, publication remains a
    separate explicit action, and stale versions cannot be submitted.
11. Existing single-product repositories continue to route and review
    Candidates through a root route.
12. Contract, domain, API, migration, frontend interaction, accessibility, and
    one real `zstack-ui-next` vertical test pass without exposing raw Session
    data centrally.
