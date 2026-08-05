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
- a route belongs either to one product or to one concrete Shared component or
  library;
- **Shared** is a non-publishable catalog root whose descendants mirror real
  tracked code directories; it is not one generic Decision bucket;
- shared code is described honestly as Shared, separated by its concrete
  component or library, and never silently copied to every product;
- Candidate Review is a compact selectable list with direct actions and batch
  operations; and
- Review submission and publication remain separate explicit boundaries.

The previously observed Dashboard loading delay is a separate performance
defect. It is not solved by, and must not distort, this functional correction.

## 2. Repository, product, and Shared model

### 2.1 Catalog groups and leaf Decision spaces

The Central Web distinguishes navigation hierarchy from Decision ownership.
It presents:

- `product`: a publishable leaf Decision space for a real company product such
  as Cloud, ZNS, or ZMetis;
- `shared_unit`: a publishable leaf Decision space for one concrete shared
  component, module, or standalone library; and
- `catalog_group`: a non-publishable navigation and aggregation node.

**Shared / 共享** is a top-level `catalog_group`. Directory groups beneath it are
also `catalog_group` nodes. Neither can own Candidates, Reviews, publications,
or Registry documents directly. Only a product or `shared_unit` leaf can own
them.

Every leaf Decision space has a stable server-owned ID, canonical display
name, kind, enabled state, repository provenance, catalog breadcrumb, source
root, optional package name, asset type, and Registry partition. Representative
asset types are `cross_product_module`, `component_library`, `library`, and
`service`; they are descriptive metadata, not routing authority.

The existing V1 Registry `product_id` remains the compatibility partition key
for this Demo. Each Shared leaf therefore receives its own stable compatibility
partition ID even though the Central Web never presents it as a sellable
product. The Shared root does not receive such an ID. A future Registry format
may mirror catalog paths physically, but that migration is not required to
keep Shared leaves logically and cryptographically isolated in V1.

Company metrics count only `product` leaves as products. Shared appears in a
distinct tree. Counts on Shared and its directory groups are aggregates of
their descendant leaves; every leaf has its own Candidate, Decision, and
publication counts.

### 2.2 Shared hierarchy from real code

For `zstack-ui-next`, the initial registered Shared tree mirrors its tracked
package layout:

```text
Shared
├── packages/products/shared        # cross-product functional modules
│   ├── audit-nest                  # @zstack/audit-nest
│   ├── zcf-alert                   # @zstack/zcf-alert
│   ├── zcf-audit                   # @zstack/zcf-audit
│   ├── zcf-license                 # @zstack/zcf-license
│   └── zcf-region-management       # @zstack/zcf-region-management
├── packages/shared                 # shared foundation layer
│   ├── design-x                    # @zstack/design-x
│   └── theme                       # @zstack/theme
└── packages                        # root-level reusable components/libraries
    ├── design                      # @zstack/design · component library
    ├── form                        # @zstack/form · library
    ├── table                       # @zstack/table · component library
    ├── hooks                       # @zstack/hooks · library
    ├── auth                        # @zstack/auth · library
    ├── i18n                        # @zstack/i18n · library
    ├── utils                       # @zstack/utils · library
    ├── zephyr                      # @zstack/zephyr · component/icon library
    └── other explicitly registered reusable packages
```

This tree is not a hard-coded universal taxonomy. Repository onboarding may
inspect tracked directories, package manifests, workspace metadata, and Nx
tags to propose leaf registrations. The persisted server configuration is
authoritative and records both the exact source root and the friendly asset
type. Tooling or generated directories are not included merely because they
live below `packages/`.

Directory location and package identity are both visible. For example,
`zcf-region-management` is shown as
`Shared / packages/products/shared / zcf-region-management` even though its Nx
metadata names several consuming products. Consumers do not change ownership
and do not cause its Decisions to be copied into Cloud or Portal.

### 2.3 Repository routes

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
packages/products/shared/zcf-audit/**   -> Shared / packages/products/shared / zcf-audit
packages/products/shared/zcf-license/** -> Shared / packages/products/shared / zcf-license
packages/shared/design-x/**              -> Shared / packages/shared / design-x
packages/shared/theme/**                 -> Shared / packages/shared / theme
packages/design/**                       -> Shared / packages / design
packages/form/**                         -> Shared / packages / form
```

Directory names are not a universal source of truth. During repository
onboarding the system may inspect tracked workspace manifests and Nx
`product:*` tags to propose routes, but an administrator-owned persisted route
configuration is authoritative. Untracked build output, ignored directories,
package names, and model-generated text never create a product automatically.

Explicit registration may add a known company product even when the current
checkout lacks a tracked product manifest. Such a route simply produces no
Candidate until trusted tracked changes match it.

### 2.4 Shared and multi-space code

Paths registered to a Shared leaf always route to that exact component or
library. They do not route to the Shared root and do not fan out into Cloud,
Portal, sibling Shared packages, or every package that imports them. A broad
`packages/products/shared/** -> Shared` fallback route is forbidden.

Only a trusted Git path match against a registered Shared-leaf route may create
a Shared-leaf Capture slice. The extraction model cannot promote product-routed
work to Shared or move work between Shared leaves merely because its
conclusion sounds broadly applicable.

One development task may touch several products and Shared leaves at the same
time. The system does not force that task into one repository-default product
and does not ask the extraction model to guess. It creates one leaf-scoped
Capture slice for every trusted route matched by the task:

```text
one Update action
  -> repository Capture group
     -> Cloud Capture slice
     -> Third-party Services Capture slice
     -> ZMetis Capture slice
     -> Shared / design Capture slice
     -> Shared / theme Capture slice
```

Each slice retains the existing fixed-Decision-space extraction contract. A
formal Candidate and Decision belong to exactly one product or Shared leaf. A
conclusion spanning several matched leaves is extracted into leaf-specific
conclusions; V1 does not publish one Decision into several Registry partitions
and does not offer a generic Shared publication target.

## 3. Trusted routing and Capture flow

### 3.1 Routing authority

Decision-space routing uses locally observed, repository-relative Git path
evidence:

- verified changed paths associated with the frozen task boundary;
- committed paths when a matching commit exists; and
- the registered route configuration version used for the Capture.

The model-generated `CandidateContent.paths` field is useful for display,
search, and consistency checks after routing. It may be empty, edited during
Review, or incorrect, so it never selects a product or Shared leaf.

The central service continues to derive organization, actor, repository,
Decision-space identity, kind, catalog breadcrumb, source root, and display
name from authenticated server state. The client may send a server-issued
route or Capture identity, but cannot authoritatively set those fields.

### 3.2 Capture outcomes

A single user Update action remains repository-scoped and produces one durable
Capture group.

- One matched route creates one Capture slice.
- Several matched routes create several independent slices under the same
  group and run without another user prompt.
- Shared-only changes create one slice per matched Shared leaf; they never
  create a slice for the Shared root.
- No matched route produces a successful `no_routable_decision_space_changes`
  result and no Candidate.
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
- Decision-space ID, kind, canonical name, catalog breadcrumb, and source root;
- the V1 compatibility partition ID; and
- the source boundary used for extraction.

Candidate-family reconciliation is partitioned by repository and Decision
space. Changing a repository route later never moves an existing Candidate to
another product or Shared leaf. Historical Review and publication evidence
continues to point to the frozen route.

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

Shared is rendered as a separate expandable tree that mirrors the registered
code hierarchy. `Shared` and intermediate directory groups show descendant
aggregate counts only. Each leaf row displays:

- the component or library name and package name;
- its full catalog breadcrumb and repository source root;
- asset type;
- pending Candidate, active Decision, and latest-activity counts; and
- links to that leaf's Candidate Review, formal Decisions, and publication
  history.

For example, the user navigates to
`Shared / packages/shared / theme`, not to one generic Shared Inbox. Directory
groups may collapse visually, but filtering or sorting never flattens ownership
or mixes draft state between leaves.

The Dashboard must never show `zstack-ui-next` itself as a product merely
because it is an enabled repository.

### 4.2 Repository deep links

A repository deep link may resolve to several Decision spaces. Therefore
`?repository_id=...` no longer guesses one product.

- If a trusted Capture group is known, the page opens the corresponding Review
  index filtered to that group and groups results by leaf Decision space.
- If only the repository is known, the page opens a repository-scoped Review
  index showing its enabled products and Shared tree.
- A Review page is opened directly only when the URL already contains a
  server-resolved leaf Decision-space identity.

Canonical Central Web and API routes use the neutral leaf identity, for example
`/spaces/{decision_space_id}/candidates`, `/spaces/{decision_space_id}/decisions`,
and `/spaces/{decision_space_id}/publications`. Existing product-only URLs may
remain as compatibility redirects for product leaves, but Shared leaves are
never exposed through a user-facing `/products/...` route merely because V1
uses a compatibility `product_id` internally.

## 5. Candidate Review interaction

### 5.1 Compact Review list

The approved Candidate page is a compact leaf-Decision-space-scoped list, not
a stack of always-expanded cards. The Shared root and directory groups cannot
host a Review draft. Every row shows only the information needed to make a
first-pass decision:

- selection Checkbox;
- Decision claim;
- short future-action or scope summary;
- registered product name or full Shared leaf breadcrumb and relevant path
  summary;
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
`已将 6 条标记为接受 · 撤销`. Decision-space navigation or a material filter
change clears transient selection but never discards saved or local draft
actions. Selection and batch Review never cross leaf Decision spaces.

The page follows the server's bounded Review-batch limit. If the current limit
is 20, selection and submission explain that limit rather than silently
truncating the user's choices.

### 5.4 Review submission

A persistent summary shows the current leaf Decision-space draft state:

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
- no registered leaf routes;
- no routable changes;
- empty and filtered-empty Candidate results;
- normal, selected, accepted, rejected, edited, and stale rows;
- partially classified drafts;
- batch action success with undo;
- draft save conflict;
- Review submission in progress, success, and retryable failure; and
- product, Shared tree, Shared-leaf, repository, and Capture-group navigation.

Checkbox labels are clickable, keyboard focus is visible, selection is
announced accessibly, and direct Review actions remain usable without a mouse.

## 7. Migration

The migration is additive and does not guess historical ownership:

1. Introduce Decision spaces and repository routes while preserving stable
   existing product IDs.
2. Convert a legacy one-product repository to one root route only when it truly
   remains a single-product repository.
3. Introduce the non-publishable Shared catalog root, directory groups, and one
   Shared leaf Decision space per explicitly registered tracked component or
   library.
4. Replace the generic `ZStack UI Next` mapping with explicit product and
   Shared-leaf routes. Do not retain a broad Shared fallback route.
5. Add frozen route/Decision-space ownership to Capture and Candidate state.
6. Existing unpublished generic `ZStack UI Next` or `Shared` Candidates are
   archived from the new leaf Inboxes and recaptured under trusted routes;
   their model-generated paths are not used for automatic migration.
7. Existing formal Decisions remain readable under their original Registry
   partition until a separate explicit migration is approved.

## 8. Non-goals

This slice does not add:

- an administrator UI for repository-route discovery or editing;
- model-based product classification;
- automatic broadcast of Shared-leaf decisions to products or sibling Shared
  packages;
- a generic Shared Inbox, Review draft, publication target, or Registry
  partition;
- automatic registration of every directory below `packages/`;
- one formal Decision owned by several Registry partitions;
- a V2 physical Registry tree that mirrors source directory names;
- SSO, Git-role authorization, comments, or multi-level approval;
- a redesign of publication Preview or formal Decision pages; or
- the separate Dashboard Git-fetch performance fix.

Demo route configuration may be maintained in trusted server configuration.
A later administration surface can manage the same contract without changing
Capture or Review semantics.

## 9. Acceptance criteria

The design is implemented only when all of the following are demonstrated:

1. One registered repository can expose Cloud, ZNS, another product, and
   multiple Shared-leaf Decision spaces without duplicate repository
   identities.
2. The Company overview counts and lists products rather than repositories and
   presents Shared as a real directory/package tree whose root and intermediate
   nodes aggregate but do not own Decisions.
3. `packages/products/shared/zcf-audit`, `packages/shared/theme`, and
   `packages/design` resolve to three distinct Shared leaves with independent
   Candidate, Review, publication, and V1 Registry partitions.
4. Trusted Cloud, ZNS, `zcf-audit`, and `theme` path changes in one completed
   task create the corresponding independent Capture slices from one Update
   action; no generic Shared slice is created.
5. A Shared package consumed by several products remains owned by its real
   Shared leaf and is not copied into those products.
6. No route match creates no Candidate; client or model ownership claims cannot
   override the server route or move work between Shared leaves.
7. Route changes never move an existing Candidate or Review to another product
   or Shared leaf.
8. Repository-only deep links never guess one product or Shared leaf in a
   multi-space repository.
9. Shared root and directory groups cannot open or save a Review draft; a leaf
   breadcrumb and exact source root remain visible throughout Review.
10. Every Candidate row has a Checkbox and direct Accept, Reject, and Edit
    actions; no Review-action select/dropdown is rendered.
11. Selecting several rows within one leaf and applying Accept or Reject
    changes exactly those draft items, reports the result, and supports undo
    before submission.
12. Technical provenance is available on demand but does not dominate the
    default list.
13. Review submits the classified subset exactly once, publication remains a
    separate explicit action, and stale versions cannot be submitted.
14. Existing single-product repositories continue to route and review
    Candidates through a root route.
15. Contract, domain, API, migration, frontend interaction, accessibility, and
    one real `zstack-ui-next` vertical test pass without exposing raw Session
    data centrally.
