# ZDecision Plugin Pre-Demo Technical Feasibility Design

**Status:** Draft under review.

**Authority:** `docs/architecture.md` remains the authority for the existing
manual V1. This document defines a bounded experiment for the installable,
automatic architecture intended for a later Demo. It does not silently change
existing V1 storage, Review, or publication contracts.

## 1. Goal

Prove that an installed ZDecision Plugin can form one complete technical loop
without asking the user to start a separate decision-compression conversation:

```text
Codex development activity
  -> local event collection
  -> delayed two-stage Capture
  -> cross-session Candidate reconciliation
  -> structured Candidate sync
  -> minimal web Review and publication
  -> formal Decision query
  -> local recall and UserPromptSubmit injection
```

This is a go/no-go experiment before the first demonstrable product version; a
passing result is not itself the first Demo. The first Demo specification is
written only after this experiment proves the Plugin, background Agent,
app-server Capture, central publication, cold-start cache, and recall loop. It
will add real company-email OIDC/SSO and production web visual design.

The feasibility stage targets Codex in the ChatGPT desktop app on macOS. Codex
CLI receives an installation and Hook compatibility smoke test. Production
hardening and additional platforms follow only after this loop passes.

## 2. Non-negotiable boundaries

- Raw Codex transcripts, prompts, model context, source-code diffs, and complete
  Thread data never enter the ZDecision central service.
- The central service accepts only structured Candidate revisions, formal
  Decisions, Review state, product/repository configuration, and operational
  metadata explicitly defined in this document.
- Only repositories registered and enabled by the organization are collected.
  The Plugin resolves the current Git remote; users do not toggle collection
  independently for each repository.
- A Candidate is not a formal Decision. A user must review a specific Candidate
  revision and explicitly publish it in the web workflow.
- Plugin, local Agent, network, or central-service failure must not block normal
  Codex development.
- Hook handlers record or retrieve bounded local state. They do not execute
  two-stage extraction or wait for a slow network request.
- The system does not assume that one Session equals one feature, that one
  feature stays in one Session, or that a Git commit marks feature completion.

## 3. Deferred to the first Demo specification

- Real company-email OIDC/SSO integration
- Production web visual design and design-system work
- Windows, mobile, and IDE Extension support
- Multi-level approval and organization-wide RBAC beyond one test identity
- Automatic mutation of an already published Decision
- Large-scale performance and reliability testing
- A distributed scheduler or generalized workflow engine
- Backward compatibility for the manual V1 CLI as an end-user interface

The central contracts are real even where the identity and presentation are
temporary. Test identity is replaced by company OIDC in the first Demo without
changing Candidate, Review, publication, or Decision-query contracts. This
feasibility document must not be renamed or marked as the first Demo after the
gates pass; the Demo receives its own specification and acceptance criteria.

## 4. Existing foundation and reuse

The repository already proves the manual domain path:

```text
selected Session boundary
  -> Inventory
  -> Extraction
  -> Candidate
  -> batch Review
  -> immutable preview
  -> explicit publication
  -> product-isolated Git Decision Registry
```

The Plugin feasibility stage reuses these behaviors:

- the two-stage Inventory and Extraction protocol;
- editable, versioned decision-compression templates;
- strict structured-output validation;
- Candidate content fields;
- stable identity and idempotency principles;
- the boundary between Candidate and formal Decision;
- product-isolated formal storage;
- immutable published history and explicit publication.

It does not preserve these manual-workflow assumptions:

- a user must name a Session before Capture;
- one completed Capture creates a permanently fixed Candidate set;
- Review state is only user-local;
- publication approval must be a Codex Turn containing `确认发布`;
- a user's local Plugin writes directly to the formal Registry.

The central service becomes the only Registry writer. It may initially use the
existing Git Registry implementation behind its service boundary. The Plugin
never clones, commits, or pushes the formal Registry.

## 5. Official Codex extension points

The installable bundle uses official Codex Plugin primitives rather than a
parallel extension mechanism:

```text
ZDecision Plugin
├── Skills
├── lifecycle Hooks
├── local ZDecision Agent executable
├── bundled local-Agent MCP server
└── remote central MCP connection
```

### 5.1 Skills

Skills describe first-time setup, status and diagnostics, and a manual
rescan/submit fallback. They also instruct Codex to call the Local Agent's
`report_work_state` tool when it can classify the current work as exploring,
implementing, awaiting user input, validation failed, or milestone complete.
That report is a local observed fact, not an authoritative completion decision.
The tool accepts only `status`, `validation`, and a list of unresolved blocker
labels; the Agent binds the current Session and Turn itself. It accepts no
Candidate text or raw transcript. Skills do not contain domain persistence
logic.

### 5.2 Hooks

- `Stop` records a completed Turn boundary only. A `Stop` event, with or
  without 60 seconds of silence, never makes work Capture-eligible.
- `SessionEnd` records that the Session ended or became idle. It may request a
  boundary assessment, but it never proves completion or Review readiness.
- `PostToolUse` records allowlisted local facts such as a validation command's
  exit status or a new Git commit. It stores no raw command output centrally.
- `UserPromptSubmit` resolves the current repository, reads the local Decision
  cache, evaluates applicability locally, and returns bounded
  `additionalContext`.
- `SessionStart` wakes the Worker and requests the asynchronous cache refresh
  defined in section 13. It does not wait for the network; onboarding readiness,
  not Session-start timing, is the first-prompt guarantee boundary.

Plugin Hooks follow Codex's normal review-and-trust flow. An enterprise-managed
deployment may later mark them trusted through managed policy.

### 5.3 MCP and OAuth

The Plugin's bundled MCP configuration names two separate servers: a local
stdio server exposing `report_work_state`, status, and manual fallback tools;
and the central streamable-HTTP MCP server. Their tools and credentials are not
interchangeable.

The central MCP server exposes authenticated tools for account status,
Candidate operations, Decision queries, and diagnostics. The production server
will use OAuth 2.1 against the company's OIDC provider and request the
`openid`, `email`, and `profile` identity scopes when advertised.

Codex-hosted MCP calls and background Local-Agent calls are separate credential
lanes. The feasibility stage must not assume that Codex exposes its stored MCP
token to a local process. It verifies whether the Plugin authorization channel
can safely serve the Agent; otherwise the Agent uses an independent test
credential in this stage and a separate authorization-code or device flow
against the same company IdP in the production design.

The central application has one domain layer with two transports. MCP serves
Codex and the Local Agent; an HTTPS application API serves the Review page.
Neither transport owns Candidate, Review, publication, or Decision semantics.

## 6. Component architecture

```text
Codex
│
├── ZDecision Plugin
│   ├── Skills
│   ├── Lifecycle Hook commands
│   └── Local ZDecision Agent
│       ├── local MCP tools / status
│       ├── Event Ledger
│       ├── Repo Resolver
│       ├── Work Unit Assembler
│       ├── Capture Scheduler
│       ├── App Server Gateway
│       ├── Candidate Reconciler
│       ├── Sync Client
│       └── Decision Cache / Recall
│
└── Central feasibility service
    ├── Test Identity boundary
    ├── Repo-to-Product configuration
    ├── Candidate Revision API
    ├── Review and Publish API
    ├── minimal Review page
    └── formal Decision Query API
```

### 6.1 Hook commands

A Hook command validates its input, appends one idempotent event, wakes the
Worker when appropriate, and exits. It never parses an entire transcript or
calls a model.

### 6.2 Local Agent

The Agent owns all device-local automation and private evidence. It resolves
registered repositories, forms provisional Work Units, schedules Capture,
talks to Codex app-server, reconciles Candidate observations, synchronizes
structured revisions, maintains the formal Decision cache, and assembles
recall context.

### 6.3 Central service

The central service owns organization configuration, shared Candidate Review
state, publication, and formal Decision truth. Its feasibility UI is functional
but intentionally plain.

## 7. Event Ledger

The Event Ledger is a user-local SQLite database. Each record contains:

- stable `event_id`;
- event type and occurrence time;
- local Session and Turn identifiers;
- current working directory;
- normalized repository identity;
- branch, worktree, and HEAD commit when available;
- an optional local transcript pointer treated only as an unstable hint;
- processing state and a bounded safe failure code.

Observed events remain facts. Their lifecycle is:

```text
observed -> recorded -> consumed
                |          |
                +-> deferred
                +-> failed_retryable / failed_terminal
```

Re-delivery of the same Hook input resolves to the same event identity. The
Ledger stores neither complete prompts nor complete transcript messages.
`capture_eligible` is not an Event state; it is the result of a separate,
versioned boundary assessment.

## 8. Work Units, Capture eligibility, and Review readiness

A Work Unit is a local scheduling envelope, not a permanent feature identity
and not an input to formal Decision identity.

It contains a repository/product identity, branch or worktree lineage, related
Sessions, completed Turn boundaries, a Git-state summary, and scheduling state.

The assembler prefers strong linkage:

- native resume/fork lineage;
- the same explicit issue or ticket identifier;
- the same branch and worktree;
- continuation of the same uncommitted state;
- overlapping primary paths within a continuous activity window.

Semantic similarity is a supporting signal and cannot by itself force two Work
Units together. Candidate reconciliation runs across active Candidate families
for the product, so a provisional Work Unit split does not prevent a later
cross-Session replacement from being detected.

The automatic pipeline has three separate gates:

```text
observed event
  -> boundary assessment
  -> capture_eligible or not_eligible
  -> Inventory and Extraction
  -> observing Candidate revision
  -> review_ready or remain observing
```

### 8.1 Observed facts and assessment triggers

`Stop`, `SessionEnd`, work-state reports, validation results, and commits are
facts. No individual fact means that a feature is complete.

The Scheduler requests a boundary assessment only after one of these strong
trigger combinations:

- `report_work_state(status=milestone_complete)` plus a completed Turn;
- a successful validation result or new commit after substantive repository
  change, followed by a completed Turn;
- `SessionEnd` when the Work Unit has at least one unassessed substantive
  completed Turn;
- the explicit manual submit fallback.

A standalone `Stop`, elapsed silence, a failed validation, or a Turn that asks
the user a question does not request an assessment.

### 8.2 Fixed boundary assessment

The Agent runs `capture-eligibility/v1`, a system-owned fixed prompt, in an
isolated app-server Turn. It receives the completed source boundary and only
local Work Unit facts. It returns this strict result:

```text
phase: exploring | implementing | awaiting_user | validation_failed |
       milestone_complete
has_durable_decision_signal: true | false
validation: passed | failed | not_applicable | unknown
unresolved_blockers: string[]
```

`not_applicable` is valid only for non-code design or product work. The output,
prompt revision, prompt digest, source Turn, and input-fact digest are stored
locally for replay.

A boundary is `capture_eligible` only when all of these are true:

- `phase` is `milestone_complete`;
- `has_durable_decision_signal` is true;
- validation is `passed`, or is `not_applicable` for non-code work;
- `unresolved_blockers` is empty;
- the source Turn is complete and has not already been assessed;
- no Capture is already active for the Work Unit;
- the repository mapping and local runtime are valid.

Every other valid result is `not_eligible` and creates no Inventory,
Extraction, Candidate, or central record. A failed assessment is retryable but
does not default to eligible.

### 8.3 Settling timer

The settling timer only debounces a strong trigger so related events can be
coalesced. It cannot move a boundary from `not_eligible` to
`capture_eligible`. Automated tests use a zero-duration injected clock; the
live feasibility run uses 60 seconds after a strong trigger. Production tuning
is outside this stage.

### 8.4 Review readiness

A successful Capture first creates an `observing` Candidate revision. It enters
the Review Inbox only when all of these are true:

- its source boundary was `capture_eligible`;
- it is a strictly validated Stage 2 Candidate originating from an Inventory
  signal that is `current_confirmed`, `high` confidence, and not `uncertain`;
- Stage 2 confirmed that no known gap intersects that Candidate's core rule or
  scope;
- reconciliation is not `ambiguous`;
- no later local event for the Work Unit exists before synchronization; and
- either the Work Unit supplied an explicit milestone-complete report, or the
  materially same Candidate was observed at two separately eligible completed
  boundaries.

`SessionEnd`, a commit, successful tests, or silence cannot independently make
a Candidate `review_ready`.

The negative acceptance set is mandatory: stopping to think, awaiting user
input, failed tests, implementation still in progress, and exploratory design
must produce no Review Inbox item. A commit with no durable decision may cause
an assessment, but it must produce no Capture Candidate.

## 9. App Server Capture

The App Server Gateway is the only component that speaks the Codex app-server
protocol. For a strong assessment trigger it performs:

```text
thread/read
  -> short-lived assessment thread/fork
     -> turn/start capture-eligibility/v1
  -> if capture_eligible: a separate fresh Capture thread/fork
     -> turn/start Inventory
     -> if Inventory is valid: turn/start Extraction
```

The assessment fork is discarded as assessment evidence and never becomes the
Capture fork. The fresh Capture fork contains exactly the existing two Turns,
Inventory then Extraction, so eligibility output cannot contaminate inherited
development context or the current Capture contract.

It returns typed source, fork, Turn, and structured-output records to the
Capture domain. The Agent does not rebuild a raw transcript from Hook files.

Gate 3 first calls app-server model discovery and freezes one
`FeasibilityModelProfile` containing an exact returned `model_id` and explicit
reasoning effort. It prefers the source Thread's model only when discovery
confirms that exact model and effort; otherwise it selects and records one
supported profile before any fixture runs. The specification deliberately does
not guess a model slug. Every eligibility, Inventory, Extraction, and
reconciliation `turn/start` passes the frozen profile explicitly and persists
it; no operation inherits an implicit Session default.

The preferred experiment connects to the current host's supported app-server
capability. If that is unavailable to a Plugin process, the only planned
fallback is a controlled app-server process using the same authenticated local
Codex state. If neither route can read, fork, and run a Turn for the Hook's
Session identity, app-server automation is a blocking feasibility failure.

## 10. Candidate reconciliation

Each valid Stage 2 item first becomes a local Candidate Observation. The
Reconciler compares it with active Candidate families for the product and
classifies the relationship as exactly one of:

- `same`: add an observation without changing current meaning;
- `refine`: create a new revision with narrowed or expanded current content;
- `replace`: replace a contradicted earlier revision;
- `unrelated`: create a new Candidate family;
- `ambiguous`: retain the observations locally without automatic merge.

A Candidate family contains a stable Candidate ID, monotonic revision, current
content, lifecycle state, source observations, stability state, and optional
replacement relation.

Reconciliation is performed by one fresh app-server Turn using the fixed,
system-owned `candidate-reconciliation/v1` prompt. This prompt is not selected
or edited by a Decision Compression Template. Its complete input is limited to
one new structured Candidate Observation and the structured current heads of
active Candidate families for the server-authoritative product. It receives no
raw Session context.

The strict output is exactly:

```json
{
  "relation": "same|refine|replace|unrelated|ambiguous",
  "target_family_id": "string|null"
}
```

For `same`, `refine`, or `replace`, the target is exactly one supplied family
ID. For `unrelated` or `ambiguous`, it is null. Schema failure, an invented ID,
or more than one plausible target becomes `ambiguous`. The model cannot
synthesize merged Candidate text. `same` retains current content and revision;
`refine` and `replace` use the new Observation's validated content exactly and
create revision `n+1`; `unrelated` creates a family; `ambiguous` creates and
changes nothing.

Each immutable Stage 2 Candidate ID is its Observation ID. The Agent assigns a
monotonic per-product Observation sequence in the same local transaction that
first persists a new Observation; duplicate Observation IDs reuse the original
sequence. Reconciliation is serialized per product, and the lowest unprocessed
sequence is always next.

A new family ID uses the existing V1-compatible Candidate shape
`cand_<32-hex>_01`. Its hash is the first 32 hexadecimal SHA-256 characters of
canonical JSON containing `kind: candidate-family/v1`, the
server-authoritative product ID, and the seed Observation ID. `same`, `refine`,
and `replace` retain that family ID.

The reconciliation operation ID is derived from the fixed prompt digest,
frozen model profile, new Observation digest, and ordered current-head tuples
of family ID, revision, and digest. Retry replays the stored result; the same
operation ID with different inputs conflicts. The Agent records operation ID,
prompt version/digest, input digest, model profile, and native Turn ID.

Lifecycle belongs to one Candidate revision; reconciliation relations are not
states:

```text
observing -> review_ready -> accepted -> published
    |              |          |
    |              +-> rejected
    +-------------------------> stale
```

`review_ready` and `accepted` may also transition to `stale`. `rejected`,
`stale`, and `published` are terminal for that exact revision. Opening the page
does not create a `reviewing` state. Only the exact current `accepted` revision
may publish.

If a newer revision arrives during Review, the central service marks the older
revision stale and refuses publication. If an earlier revision is already a
formal Decision, the new revision becomes a separately reviewed change that
may later revise or supersede it; published history is never silently edited.
A rejected revision is terminal and cannot publish. A later materially changed
Observation may create a new revision, which must satisfy Review readiness and
receive a new Review. When a new local revision makes a centrally visible
revision obsolete, the Agent immediately sends an authenticated invalidation
control containing only the family ID, old revision, old digest, and
`new_revision` reason. The central service marks the old revision stale even
when the new revision is still local `observing`; its new content is not sent
until it becomes `review_ready`.

## 11. Central data contracts

The authenticated Candidate synchronization request allowlist is:

- repository ID;
- Candidate ID, revision, state, and content digest;
- claim, future action, scope, and invalidation conditions;
- replacement relation;
- opaque local source checkpoint IDs needed by the V1 formal renderer.

Only current `review_ready` revisions and invalidation controls synchronize.
An invalidation carries the already-known family ID, old revision, old digest,
and reason, but no new `observing` content. Boundary-assessment, observing, and
ambiguous reconciliation states remain local.

The central API rejects unknown fields. In particular it rejects transcript,
prompt, raw Thread, model-context, source-diff, source-code, and local-event
payloads.

Writes are idempotent on Candidate ID, revision, and content digest. A lower
revision cannot overwrite a higher revision, and the same revision with a
different digest is a conflict.

The server derives `organization_id` and `actor_id` exclusively from the
verified login identity. It resolves `product_id` and canonical product name
from that organization's repository mapping. Organization, actor, and product
are not client-writable request fields; if Candidate content names a different
product, the server rejects it. An unregistered or disabled repository is a
no-op. The repository ID is a credential-free stable digest of the normalized
Git remote, and the server independently derives the same value from its
registered remote.

Source checkpoint IDs are locally salted, stable opaque references. Native
Session and Turn IDs and their content are not synchronized. The central
service cannot dereference the opaque values back to a local conversation.

Candidate fields retain the current 16 KiB per-Candidate encoded-size limit and
are treated as untrusted text by the Agent, server, page, and Registry renderer.
Neither Candidate nor Decision text is interpreted as a command.

## 12. Review and publication

The minimal page lists current `review_ready` Candidate revisions grouped by
product. A user can accept or reject several items and then publish the accepted
set.

Review is bound to exact Candidate revisions. A newer revision invalidates an
unpublished Review. Publication creates immutable formal Decision revisions;
the central service alone writes the Git Registry, with one batch publication
per commit. The feasibility stage does not implement formal Decision update or
retirement UI.

The click on **Publish** is the explicit publication action. Its request has a
stable Web action ID plus the exact displayed Candidate IDs, revisions, content
digests, and accept/reject choices. The server derives organization and actor
from login and product from repository mapping. The same scoped Web action ID
and identical payload returns the original result; reuse with different bytes
is a conflict.

The server transaction first verifies that every displayed revision and digest
is still current. It then atomically records the Web Review and a frozen
`confirmed` central Publication Record before any Git work starts. A stale item
writes neither Review nor publication state.

### 12.1 Formal Decision compatibility

This feasibility stage does not introduce Decision schema version 2. It writes
the existing exact `zdecision-decision/v1`, `schema_version: 1` format so the
existing Registry and readers continue to load old and new Decisions.

- Product ID uses the existing canonical NFC/trim product name and canonical
  JSON SHA-256 derivation.
- Decision ID uses the existing canonical `{candidate_id, product_id}` SHA-256
  derivation. The V1-compatible Candidate family ID is its Candidate input.
- Only initial `revision: 1`, `lifecycle: active` Decisions publish;
  `supersedes` and `variant_of` remain empty in this experiment.
- Paths remain
  `decision-registry/products/<product_id>/decisions/<decision_id>/r0001.json`.
- Formal bytes use the existing sorted-key, compact, UTF-8 canonical JSON with
  unescaped Unicode, no NaN, and one trailing newline.
- The formal `source.thread_id` and `source.turn_id` contain the Candidate's
  opaque local source checkpoint IDs, not native Codex IDs.
- The required V1 `review_approval` remains a minimal approval coordinate:
  `actor` is `user`, `thread_id` is `web_review_<stable-id>`, `turn_id` is
  `web_action_<stable-id>`, and `recorded_at` is server UTC. These explicit
  prefixes prevent the values from being mistaken for native Codex IDs.
- The required `publication_preview_id` uses the existing
  `pub_<32-hex>` derivation. Decision documents do not gain a digest field.

The complete organization identity, stable actor ID, email-independent login
subject, Web channel metadata, authentication evidence, Review payload, and
publication action remain only in the central Publication Record. Email
addresses and tokens never enter Git.

If channel-neutral values in the existing `review_approval.thread_id/turn_id`
fields are later rejected as a product contract, V1 compatibility is no longer
possible; a separate specification must define V2 and upgrade every Registry
reader before any V2 file is written.

### 12.2 Publication, retry, and crash recovery

The central Publication Record freezes:

- server-derived organization, actor, repository, and product;
- the Web Review/action identity and server approval time;
- every Candidate ID, revision, digest, and accept/reject result;
- accepted Decision IDs;
- base commit and exact pre-publication Registry digests;
- exact formal paths and bytes, per-file digests, and batch content digest;
- exact commit message, state, commit SHA when known, and verification time.

Its monotonic states remain:

```text
confirmed -> committed_pending_push -> completed
```

The Git Worker consumes only the frozen files; it never reloads Candidate text
and renders different bytes during retry. From `confirmed`, recovery adopts a
commit only when it is the unique one-parent child of the frozen base and its
message, changed paths, and blobs match exactly. From
`committed_pending_push`, retry proves or pushes the same commit. Any other Git
shape is ambiguous and stops. A Candidate-family publication receipt prevents
a second Decision from that family; publishing a later formal revision is
deferred to the first-Demo or later Decision-update specification.

## 13. Recall and injection

### 13.1 Onboarding and cache generations

Plugin installation is not recall readiness. After authorization, onboarding
must fetch a server-signed complete repository mapping and a complete Decision
snapshot, including valid empty snapshots, for every enabled Product. It
validates the complete response and atomically switches one active local cache
generation. Only then does Plugin status become `ready`.

A new computer performs the same onboarding. A newly registered repository is
not ready until one generation contains both its mapping and its complete
Product snapshot. Until readiness, ordinary Codex work continues but ZDecision
does not claim that an empty result means no applicable Decision.

Each generation records:

```text
organization_id
mapping_version
mapping_issued_at
mapping_fresh_until
mapping_recall_until
product_id -> decision_version
sync_cursor
last_successful_sync_at
active_generation_digest
signature_key_id
state
```

States are exactly:

```text
unauthorized | cold | warming | fresh | stale | expired | invalid
```

`empty` and `no_applicable` are content results, never cache states.

The central service signs the mapping and Decision manifest with Ed25519. The
signed object uses the repository's canonical JSON bytes and omits the
signature field; the Plugin pins the trusted public key and key ID. An unknown
key, bad signature, wrong organization, same version with a different digest,
lower version, invalid V1 Decision, or digest mismatch makes the incoming
generation `invalid` and leaves the previous active generation unchanged.

### 13.2 Freshness and offline behavior

The exact policy is:

| Cache state | Signed-cache age | Recall | Capture and upload |
|---|---:|---|---|
| `fresh` | at most 15 minutes | allowed | allowed |
| `stale` | over 15 minutes and at most 24 hours | allowed with stale status | forbidden |
| `expired` | over 24 hours | forbidden | forbidden |
| `cold`, `warming`, `invalid`, `unauthorized` | no safe generation | forbidden | forbidden |

A failed refresh never extends either deadline. A successful mapping refresh
atomically removes a repository disabled by an administrator; offline use may
retain the last signed route for at most 24 hours.

`UserPromptSubmit` reads only the active local generation and performs no
network call or synchronization wait. For `cold` or `warming`, it wakes the
Worker and returns promptly with no `additionalContext`; a one-time Hook
`systemMessage` exposes the cache state. `expired` and `invalid` likewise do not
masquerade as `no_applicable`. In `stale`, the Hook may inject and displays the
signed cache version and age as stale.

### 13.3 Background incremental synchronization

Every successful central publication atomically increments the Product's
`decision_version` and the organization `sync_cursor`. The Local Agent pulls by
cursor; a push or notification is only a wake-up hint and never the source of
correctness.

During active Codex desktop sessions, the singleton Agent tracks one bounded
sync lease per Session from `SessionStart` through `SessionEnd` and polls at
least once per 60 seconds while any lease is active. Hook activity renews that
Session's lease; a crash-expired lease can be reclaimed. The Worker exits only
after every active lease has ended or expired and its queue is drained. If this
process cannot reliably survive for the active sessions, Gate 2 uses its
already declared OS-managed user-service fallback.

An incremental response is fully validated, written as a new generation, and
then atomically made active with its cursor. A crash exposes either the old
generation and old cursor or the complete new pair, never a mixture. Replaying
the same cursor is idempotent. `SessionStart` and a cold/due Prompt also wake
the Worker without putting a remote request on the Prompt path.

### 13.4 Local ranking

The Local Agent caches current formal Decision revisions by product. The
central service receives product-scoped snapshot and cursor requests, not the
user's prompt. Injected entries include Decision ID and revision.

The feasibility ranker is deterministic and makes no model or network call. It
first excludes Decisions whose repository scope does not include the current
repository. It then scores exact repository/path references, normalized ASCII
word overlap, and normalized CJK two-character-shingle overlap across the user
prompt, Decision claim, future action, and scope. It returns at most eight
complete Decisions and at most 10,000 UTF-8 bytes, stopping before the first
item that would exceed the byte limit. It never truncates an individual
Decision. Excluded IDs and the exclusion reason remain in local diagnostics.

This deterministic ranker proves private, bounded injection; semantic-retrieval
quality optimization is a later measured product task rather than part of the
Hook critical path.

### 13.5 Task Usage and injection receipts

Task Usage is local-only and keyed by the Hook's stable `session_id`. It stores
the active repository and product, mapping version, Decision version, and each
injected `(decision_id, revision, canonical_content_digest)`. It stores no
Prompt and never synchronizes centrally.

For each Hook Turn, a Prompt Injection Receipt stores Session and Turn IDs,
repository and product, mapping and Decision versions, injected revision keys,
and the final `additionalContext` digest. Task Usage and its Receipt commit in
one SQLite transaction before Hook output. Re-delivery of the same Session and
Turn returns the same Receipt and output.

The ranker first selects its bounded result, then removes revisions already
injected for the current repository/product route. The same revision is never
injected twice in one Session. A new Decision may inject once. Task Usage keeps
the revision field for compatibility, but this experiment accepts only the
existing V1 `revision: 1` formal schema and does not manufacture a revision-2
fixture. A cache-version change with unchanged heads does not re-inject. A
product or repository change starts a new route epoch. A cold Prompt writes no
injected keys, so the next Prompt after readiness can inject. Corrupt Task
Usage fails closed for injection, wakes repair, and lets the ordinary Prompt
continue.

## 14. Failure behavior

- Hook validation or Ledger failure is reported safely but does not stop the
  Codex Turn.
- Agent crashes leave queued events retryable.
- A singleton lease prevents two Workers from processing one Work Unit at the
  same time; an expired lease can be reclaimed.
- Central-service outage leaves Candidate revisions in a local outbox and
  retries idempotently.
- App-server ambiguity is recorded and stops that Capture; it does not create a
  replacement fork without reconciliation.
- Candidate reconciliation ambiguity remains private and visible through
  diagnostics; it is not guessed away.
- Recall cache corruption or schema mismatch disables injection for that prompt
  and requests a later refresh.

## 15. Feasibility gates

### Gate 1: Plugin installation and extension discovery

Pass when a locally installed Plugin contributes its Skill and local Agent
tools, receives `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, and
`SessionEnd` in a new conversation, completes Hook trust, works in an arbitrary
test repository without repository `AGENTS.md`, and stops all behavior when
disabled.

### Gate 2: Hook latency and Agent lifecycle

The recommended runtime is an on-demand singleton Worker with an active-session
sync lease. A Hook appends an event, wakes or starts the Worker, and exits. The
Worker drains work, polls central changes while the Codex Session remains
active, and exits after all Session leases end or expire and its queue drains.

Pass when Hook p95 duration is at most 150 ms, 100 rapid test events are neither
lost nor processed twice, concurrent wake-ups leave one active Worker, a crash
leaves unfinished work retryable, a central outage does not affect Codex, and
the Worker advances a test sync cursor within 60 seconds without a new Prompt.

The sole fallback is an OS-managed user service. If both runtimes fail, stop
with a local-background-runtime feasibility failure.

### Gate 3: Automated app-server access

Pass when the Agent starts from a real Hook Session ID, reads the completed
boundary, forks without modifying the source Session, runs the fixed boundary
assessment and both existing Capture stages, records native Turn identities,
and returns one validated Candidate set without transcript-format parsing or
duplicate retry results. The Gate must first persist one exact model-discovery
result and frozen `FeasibilityModelProfile`; every Turn must prove it received
that profile explicitly.

Failure of both the host-capability and controlled-process routes is a blocking
result. Later gates do not proceed.

### Gate 4: Capture eligibility and Review readiness

The positive set contains completed code work with successful validation and a
durable confirmed decision, plus completed non-code product/design work whose
validation is correctly `not_applicable`. Both must become Capture-eligible;
only Candidates satisfying section 8.4 may reach the Review Inbox.

The negative set is:

| Scenario | Required result |
|---|---|
| Plain `Stop` followed by more than 60 seconds of silence | Event only; no assessment or Capture |
| Assistant is waiting for user input | `not_eligible`; no Review item |
| Validation failed | `not_eligible`; no Review item |
| Implementation is explicitly unfinished | `not_eligible`; no Review item |
| Work is exploratory and alternatives remain open | `not_eligible`; no Review item |
| Session ends while blocked or unfinished | assessment may run, but no Capture or Review item |
| Commit contains no durable decision | zero Candidates and no Review item |

Pass when the fixed eligibility result and stored prompt/input digests explain
every positive and negative outcome. Silence, `Stop`, commit, validation, or
`SessionEnd` alone must never be the reason for Review readiness. All fixtures
use the one Gate 3 model profile rather than an implicit current-Session model.

### Gate 5: Cross-Session convergence

The fixed scenarios and expected structural results are:

| Scenario | Expected result |
|---|---|
| One feature in one Session | One Candidate family |
| One feature across two Sessions | One family with added observations or a revision |
| Two features in one Session | Two independent families |
| Later Session reverses an earlier choice | Earlier revision replaced; later revision current |
| Duplicate Hook or Capture delivery | No content or revision-count change |

Pass when all five results have the expected family count, lifecycle, and
replacement relation. Text wording need not be byte-identical. Uncertain input
must return `ambiguous`, not a silent merge or deletion. Each result must record
`candidate-reconciliation/v1`, model, prompt/input digests, and native Turn ID;
replay must preserve family ID and revision.

### Gate 6: Central service and identity lanes

Pass when unauthorized requests fail; login derives organization and actor;
repository mapping derives product; client organization/actor/product fields
are rejected; Candidate synchronization is idempotent and monotonic; forbidden
raw-content fields are rejected; central persistence contains no raw
conversation content; signed mapping and Decision snapshots validate; Codex
reaches the central MCP server through development OAuth/OIDC; and the Agent
uses either a verified safe Plugin channel or an independent test credential
without reading Codex private credential storage.

### Gate 7: Minimal Review and publication

Pass when the page shows product-isolated current revisions, accepts batch
accept/reject choices, invalidates stale Review, publishes accepted items as
formal Decisions, and causes the central service to create exactly one
product-correct Registry commit for the batch. The new files must round-trip
through the existing V1 readers byte-for-byte. Replaying the same Web action is
idempotent; conflicting reuse fails. Injected crash points before commit, after
commit, and before/after push must resume the one frozen publication or stop as
ambiguous without creating another Decision or commit.

### Gate 8: Cold-start recall and `UserPromptSubmit`

This Gate starts from an empty local data directory and may not seed or manually
synchronize a Decision cache. It must cover:

1. authorization and onboarding move `cold -> warming -> fresh` only after a
   signed complete mapping and every enabled Product snapshot commit atomically;
2. a new computer follows the same path;
3. a newly registered repository becomes ready only with its Product snapshot;
4. publication advances server `decision_version` and `sync_cursor`, and the
   active Agent advances naturally within 60 seconds without a seed, manual
   sync, or new Prompt;
5. an interrupted incremental sync exposes no partial generation and retries
   idempotently;
6. from 15 minutes through 24 hours offline, Recall works with stale status
   while Capture/upload fail closed;
7. after 24 hours, or for bad signature, digest, schema, organization, or
   version monotonicity, Recall and Capture fail closed while Codex continues;
8. a warming Prompt returns within 200 ms, injects nothing, does not record
   `no_applicable`, and can inject on the next Prompt after readiness;
9. fixed English and Chinese Prompts rank the expected downloaded Decision;
10. the second Prompt in one Session does not re-inject the same revision;
11. a newly published Decision ID injects once, while a cache-version-only
    change with unchanged Decision heads does not;
12. a repository/product change starts a new route epoch and injects that
    product once;
13. re-delivery of one Hook Turn returns the same Prompt Injection Receipt;
14. a new Session can receive the same relevant Decision again; and
15. central request logs prove that synchronization never contains the user's
    Prompt.

Pass also requires steady-state injection p95 of at most 200 ms, complete
Decision IDs/revisions in context, and no filler context for a genuine
`no_applicable` result.

### Gate 9: Real end-to-end acceptance

Use one centrally registered test repository and three Sessions:

1. Plugin authorization and onboarding reach a naturally downloaded `fresh`
   cache with no test seed.
2. Session A performs development work.
3. Session B continues or reverses one choice.
4. The Plugin produces reconciled Candidates without a manual compression
   conversation.
5. The user reviews and publishes from the minimal page.
6. The test observes the Local Agent naturally advance to the publication's
   returned Decision version without manual synchronization.
7. Session C starts a new task and receives the relevant formal Decisions on
   its first Prompt; its second Prompt does not repeat them.

Pass only when the central data audit finds no raw Session content, Candidate
state has no unresolved duplicate current conclusions, publication targets the
correct product, Session C identifies the supplied Decision revisions, and
failure of any ZDecision component does not block normal Codex work.

## 16. Execution and stopping rules

Run gates in order because Gates 2 through 4 can invalidate the architecture.
Each gate permits one recommended implementation, one predeclared fallback
where specified, and one focused correction of a confirmed defect. A failed
gate produces evidence and a bounded architecture decision; it does not
trigger a new broad review.

After all gates pass, run one focused test set, one full repository test run,
and one real end-to-end acceptance. Do not start a new blind audit, wide code
review, or speculative hardening cycle. Remaining non-blocking risks become
explicit follow-up work.

## 17. Official capability references

- [Plugin architecture](https://developers.openai.com/plugins/concepts/plugins)
- [Plugin packaging](https://developers.openai.com/plugins/build/plugins)
- [Codex lifecycle Hooks](https://learn.chatgpt.com/docs/hooks)
- [MCP OAuth 2.1 authentication](https://developers.openai.com/plugins/build/auth)
- [Plugin installation and supported surfaces](https://learn.chatgpt.com/docs/plugins)
