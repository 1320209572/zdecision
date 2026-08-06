# Registry Read Model Design

**Status:** Approved for implementation planning.

**Scope:** Keep the Git Decision Registry as the only authoritative formal
Decision source while projecting its verified contents into Central SQLite
for Dashboard, formal Decision queries, and future recall. Preserve every
existing publication, recovery, exact-main, canonical-JSON, and ownership
invariant.

## 1. Problem and evidence

The Central Web currently builds a complete formal Registry snapshot inside
each Dashboard, Decision catalog, Decision detail, and repository-space
request. Each snapshot performs `git fetch origin main` before and after
reading the Registry so the request can prove that the commit did not change
mid-read.

That safety check belongs at synchronization and publication boundaries, not
on every page read:

- one fetch takes about 3.9 seconds in the current environment;
- Dashboard takes about 8 seconds and formal Decisions about 10.4 seconds;
- Candidate and Publication list APIs take about 7 milliseconds in isolation;
  and
- a concurrent Dashboard request delays the 7-millisecond Candidate request
  to about 9.56 seconds because synchronous Git subprocesses block the single
  Uvicorn event loop.

The response bodies are small. JavaScript size, CSS, React rendering, and the
current amount of Decision data are not the primary cause.

## 2. Goals

- Ordinary Central Web reads perform no Git network, subprocess, or Registry
  filesystem work.
- Every projected row is bound to one verified `decision-registry` Git tree,
  its proving repository commit, and a canonical document digest.
- Projection installation is atomic: readers observe either the complete old
  commit or the complete new commit, never a partial mixture.
- A completed publication becomes visible immediately without a Central
  restart.
- Central recovers automatically after a crash between Git publication and
  projection installation.
- Preview creation, publication confirmation, publication recovery, and push
  retain their current fresh-fetch and exact-main checks.
- Candidate and Publication operational APIs do not wait behind Dashboard or
  formal Decision Registry reads.
- The solution remains in-process and V1-sized: no Redis, queue, webhook,
  distributed lease, second Registry writer, or additional service.

## 3. Considered approaches

### A. Durable SQLite read model — selected

Git remains the write model and sole source of truth. A synchronizer reads one
already verified Git commit, validates the same Registry models used today,
and transactionally installs query-oriented product and Decision rows in the
existing Central SQLite database. Web APIs query only those rows.

The projection is derived data. It never authorizes publication and can be
deleted and rebuilt entirely from Git.

This adds a bounded synchronization contract but gives Central durable,
restart-safe, queryable data for Dashboard, formal Decision pages, search,
pagination, and future plugin recall.

### B. Process-local immutable snapshot

Keep one verified Registry object in memory and replace it after publication.
This is simpler, but it must rebuild after every restart, is unavailable to
other workers, has no durable sync state, and does not naturally support
query indexing or future recall.

### C. Dedicated Registry reader service

Move Git synchronization and query serving into a separate process or remote
service. This provides strong operational isolation and multi-instance
coordination, but adds authentication, deployment, monitoring, and failure
modes that are not justified for the first Demo.

Approach A is selected because Central already owns SQLite and all query-side
product metadata. It solves the current latency without introducing another
runtime component.

## 4. Authority and consistency model

The following authority order is fixed:

1. Git `origin/main` contains authoritative formal Decision bytes.
2. A verified Git commit proves one immutable `decision-registry` tree OID.
3. The tree OID, rather than an unrelated code-only commit, identifies the
   Registry content version.
4. SQLite contains only a derived projection of that exact tree.
5. Web pages and recall read the projection only when its state is
   `available` and both its active commit and active tree OID are non-null.

SQLite never becomes a second Decision Registry. No browser, plugin, Candidate,
Review, or Publication payload may write projection rows directly. Only the
server-owned Registry synchronizer accepts a commit SHA obtained from either
startup verification or a completed publication/recovery record, then derives
and verifies the Registry tree OID itself.

## 5. Projection schema

The Central schema gains three server-owned tables.

### 5.1 `registry_projection_state`

One row per organization:

- `organization_id` — primary key;
- `state` — `available`, `syncing`, or `unavailable`;
- `active_commit` — repository commit currently proving the read model;
- `active_tree_oid` — `decision-registry` tree represented by projected rows;
- `desired_commit` — commit being installed or requiring recovery;
- `desired_tree_oid` — Registry tree being installed or requiring recovery;
- `verified_at` — time the desired commit was proven against Git;
- `updated_at` — last projection state change; and
- `error_code` — bounded internal synchronization classification or null;
- `product_count` and `decision_count` — expected active row counts; and
- `projection_digest` — SHA-256 of the ordered product/Decision identity and
  canonical-document-digest manifest.

The last three integrity fields distinguish a valid empty Registry from a
missing or partially deleted projection without consulting Git on a Web read.

### 5.2 `registry_product_projection`

Rows are keyed by `(organization_id, registry_tree_oid, product_id)` and store:

- canonical product JSON and SHA-256 digest;
- product name; and
- the Registry paths needed for ownership verification.

### 5.3 `registry_decision_projection`

Rows are keyed by
`(organization_id, registry_tree_oid, product_id, decision_id, revision)` and
store:

- canonical Decision JSON and SHA-256 digest;
- lifecycle and revision;
- indexed claim, future action, and scope summary;
- JSON-encoded repository, path, and invalidation-condition collections; and
- publication preview identity and source/review proof already present in the
  canonical V1 Decision.

Decision-space ownership remains server-derived from the existing Central
catalog mapping. Client-supplied product or Decision-space identity is never
trusted.

All projection tables are migration-managed with the existing Central schema.
Foreign keys and indexes cover active-tree, product, Decision ID, lifecycle,
and the current search/filter paths. No FTS engine is introduced in this
slice.

## 6. Projection build and atomic installation

The synchronizer exposes one operation:

`synchronize(organization_id, verified_commit, verified_at)`.

It performs the following sequence:

1. Validate the commit format and prove local `HEAD`, `refs/heads/main`, and
   the last fetched `refs/remotes/origin/main` equal `verified_commit`.
2. Resolve and validate the immutable `verified_commit:decision-registry`
   tree OID.
3. Persist `state=syncing`, `desired_commit=verified_commit`, and the desired
   tree OID.
4. Read the immutable commit using the current commit-bound Registry parser.
5. Validate canonical JSON, root/product ownership, Decision ownership, head
   revision, and lifecycle exactly as today.
6. Prepare product and Decision projection rows without changing the active
   projection.
7. In one SQLite transaction, insert the complete verified-tree projection,
   set the active commit and tree OID to their desired values, set
   `state=available`, clear the error, and remove rows for superseded trees.

If parsing, validation, or installation fails, the transaction does not expose
partial rows. State becomes `unavailable`, `desired_commit` remains available
for recovery, and formal Registry APIs fail closed instead of returning empty
or silently stale data.

The synchronizer is idempotent. When a new verified repository commit has the
same Registry tree OID as the active projection, it updates only the proving
commit and verification time; code-only commits do not duplicate or rebuild
formal Decision rows.

## 7. Startup synchronization

Before Central begins serving formal Registry reads:

1. perform one existing fresh fetch of `origin/main`;
2. require exact equality of `HEAD`, local `main`, and fetched `origin/main`;
3. resolve the verified commit's `decision-registry` tree OID;
4. reuse the durable projection when that tree matches the active tree and its
   digests are valid, updating only the proving commit and verification time;
   or
5. synchronize the verified tree when it differs, the state is incomplete, or
   projection rows are missing.

Startup may therefore spend several seconds performing one remote verification,
but page navigation never pays that cost. If startup verification fails,
Candidate review and Publication history may remain operational while formal
Registry APIs report unavailable. They do not serve a projection whose remote
freshness could not be proven.

V1 retains the explicit single-Central-writer rule. Code-only commits in the
same repository are supported through tree-OID identity. External manual
changes under `decision-registry/` and multiple independent Central writers
remain outside the supported contract. A later version may add a webhook or
dedicated sync worker without changing the projection schema.

## 8. Publication and recovery flow

Publication confirmation and recovery preserve their exact current sequence:
fresh fetch, exact-main validation, preview freshness proof, exact commit
creation or reconciliation, push, and remote containment proof.

Only after the durable Publication record reaches `completed`, the application
calls the synchronizer with `publication.commit_sha`. The synchronizer derives
the resulting Registry tree OID. The publication path has already proved that
local and remote `main` contain that commit, so projection installation
requires no extra network fetch.

If Central crashes after the Git push but before projection installation, the
completed Publication record and Git commit remain authoritative. On restart,
startup compares the verified commit and Registry tree with projection state,
then rebuilds the changed tree or updates code-only provenance as required.

If post-publication projection installation fails:

- Publication remains `completed` and is never rolled back;
- the projection state becomes `unavailable` with the desired commit recorded;
- formal Decision APIs fail closed;
- Publication history continues to show the completed Git proof; and
- startup synchronization retries deterministically.

This prevents both duplicate publication and silently stale formal reads.

## 9. Read path and API behavior

Dashboard, Decision list/detail, and repository-space queries read the active
SQLite projection and existing Central catalog data. They never call Git or
construct a Registry snapshot.

The public Registry state remains compatible:

- `available` — the active projection is complete and commit-bound;
- `unavailable` — no projection may be served.

The Dashboard additionally exposes `verified_at` and uses the copy
**Registry 已验证** rather than **Registry 已同步**. Internal `syncing` state is
reported as externally unavailable until the atomic switch completes.
The existing Registry commit remains the user-facing Git proof; the tree OID
is stored and tested as the Registry content identity so unrelated code-only
commits do not force a rebuild.

No general React query cache is added in this slice. Once Web reads are SQLite
only, page responses should already be millisecond-scale. A frontend cache
would add invalidation behavior and could hide a newly published Decision.

## 10. Concurrency and trust boundaries

- Read requests use only SQLite and never acquire a Git synchronization lock.
- Projection builds parse outside the installation transaction; the atomic
  transaction is limited to projection rows and state switching.
- Prior projection rows remain intact while a new tree is built, but formal
  APIs map internal `syncing` to unavailable and do not serve them. After the
  installation transaction commits, every reader observes the new active tree.
- Projection rows are server-owned and accept no client write API.
- Candidate, Review, Preview, Publication, and Decision text remains untrusted
  data and cannot trigger synchronization.
- Only startup verification and a native completed publication/recovery event
  may select a desired commit.
- The projection never authorizes commit creation, push, or publication.

## 11. Verification and acceptance

Automated tests must prove:

1. schema migration creates the projection tables, constraints, and indexes;
2. synchronizing a verified commit installs canonical rows, an active tree,
   and its proving commit atomically;
3. a code-only commit with the same Registry tree updates provenance without
   rebuilding rows, and an identical replay is idempotent;
4. parse, digest, ownership, or transaction failure exposes no partial or
   stale projection;
5. Dashboard and formal Decision queries use no Git adapter or subprocess;
6. startup reuses a matching durable projection and rebuilds a mismatched or
   interrupted projection;
7. completed publish and resume install their exact completed commit;
8. a crash after Git publication but before projection installation recovers
   on startup;
9. preview and publication tests still perform fresh exact-main checks; and
10. existing Registry corruption, stale preview, ambiguous publication, and
    crash-recovery tests remain green.

Real acceptance on the running Demo must prove:

- Dashboard and formal Decisions perform no `git fetch` in their request path;
- repeated requests complete below 200 milliseconds on the current machine;
- a concurrent Dashboard request does not delay Candidate beyond 200
  milliseconds;
- restarting Central preserves the active projected commit; and
- the previously published Decision remains visible, its Publication receipt
  retains commit `02ae3c37e388b74004d771d18468bba06f90a1f6`, and the active read-model
  commit exactly matches the then-current `origin/main`.

## 12. Deferred boundaries

The following are explicitly deferred:

- periodic polling or Git-provider webhooks;
- multiple independent Central writers or multi-region synchronization;
- PostgreSQL migration and cross-host projection distribution;
- full-text search engines;
- generic frontend query caching;
- SQL N+1 cleanup outside queries touched by this projection; and
- projection history browsing across multiple Registry commits.

## 13. Stop rule

Stop after the migration-managed SQLite projection, verified synchronizer,
startup reuse/rebuild, completed-publication refresh, SQLite-only read path,
semantic Dashboard copy, focused tests, one full test run, and one real latency
check pass. Do not add a new service, Redis, queue, background polling, webhook,
generic React data library, unrelated SQL optimization, or a new wide review
cycle in this slice.
