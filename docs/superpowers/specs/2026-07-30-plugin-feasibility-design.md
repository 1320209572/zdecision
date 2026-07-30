# ZDecision Plugin Technical-Loop Feasibility Design

**Status:** Approved conversational design for the post-V1 plugin feasibility
stage.

**Authority:** `docs/architecture.md` remains the authority for the existing
manual V1. This document defines a bounded experiment for the installable,
automatic Demo architecture. It does not silently change existing V1 storage,
Review, or publication contracts.

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

## 3. Deferred from this feasibility stage

- Real company-email OIDC/SSO integration
- Production web visual design and design-system work
- Windows, mobile, and IDE Extension support
- Multi-level approval and organization-wide RBAC beyond one test identity
- Automatic mutation of an already published Decision
- Large-scale performance and reliability testing
- A distributed scheduler or generalized workflow engine
- Backward compatibility for the manual V1 CLI as an end-user interface

The central contracts are real even where the identity and presentation are
temporary. Test identity is replaced by company OIDC later without changing
Candidate, Review, publication, or Decision-query contracts.

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
└── MCP server connection
```

### 5.1 Skills

Skills describe first-time setup, status and diagnostics, and a manual
rescan/submit fallback. They do not contain domain persistence logic.

### 5.2 Hooks

- `Stop` records a completed Turn boundary.
- `SessionEnd` records a strong settling signal. It is not the only Capture
  trigger and does not itself execute Capture.
- `UserPromptSubmit` resolves the current repository, reads the local Decision
  cache, evaluates applicability locally, and returns bounded
  `additionalContext`.
- `SessionStart` may perform a bounded health check or Worker wake-up, but it
  does not make a remote request on the critical path.

Plugin Hooks follow Codex's normal review-and-trust flow. An enterprise-managed
deployment may later mark them trusted through managed policy.

### 5.3 MCP and OAuth

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

The event lifecycle is:

```text
observed -> eligible -> scheduled -> processed
               |            |
               +-> deferred +-> failed_retryable
                            +-> failed_terminal
```

Re-delivery of the same Hook input resolves to the same event identity. The
Ledger stores neither complete prompts nor complete transcript messages.

## 8. Work Units and delayed Capture

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

On each meaningful event, the Scheduler resets a short settling timer. It runs
Capture only when the repository is registered, the source Turn is complete,
the boundary has not already been processed, no Capture is active for the Work
Unit, and the local runtime is healthy. `SessionEnd`, commit, successful
validation, and an explicit completion marker strengthen readiness but none is
universally required.

The feasibility implementation measures rather than productizes the settling
duration. Automated tests use a zero-duration injected clock; the live Demo
uses a configurable 60-second settling duration. Production tuning is outside
this stage.

## 9. App Server Capture

The App Server Gateway is the only component that speaks the Codex app-server
protocol. For an eligible completed boundary it performs:

```text
thread/read -> thread/fork -> turn/start Inventory -> turn/start Extraction
```

It returns typed source, fork, Turn, and structured-output records to the
Capture domain. The Agent does not rebuild a raw transcript from Hook files.

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

The lifecycle is:

```text
observing -> review_ready -> reviewing -> accepted/rejected -> published
    |              |
    |              +-> stale when a newer revision arrives
    +-> refined / replaced / withdrawn
```

A Candidate becomes `review_ready` after at least one of these conditions:

- its source Session ends without a newer contradictory observation;
- materially the same decision is observed at two distinct completed
  boundaries;
- an explicit completion signal is followed by a successful Capture;
- the user invokes the manual submit fallback.

A settling timer alone never proves stability.

If a newer revision arrives during Review, the central service marks the older
revision stale and refuses publication. If an earlier revision is already a
formal Decision, the new revision becomes a separately reviewed change that
may later revise or supersede it; published history is never silently edited.

## 11. Central data contracts

The Candidate synchronization allowlist is:

- organization, product, and repository identifiers;
- Candidate ID, revision, state, and content digest;
- claim, future action, scope, and invalidation conditions;
- replacement relation;
- creation and update timestamps.

The central API rejects unknown fields. In particular it rejects transcript,
prompt, raw Thread, model-context, source-diff, source-code, and local-event
payloads.

Writes are idempotent on Candidate ID, revision, and content digest. A lower
revision cannot overwrite a higher revision, and the same revision with a
different digest is a conflict.

The organization centrally controls normalized Git-remote-to-product mapping.
An unregistered repository is a no-op. A Plugin-provided product claim cannot
override the server mapping.

After test-identity authorization, the Agent fetches a versioned enabled-repo
mapping at `SessionStart` and refreshes it at most once every 15 minutes. Hooks
read only that local mapping cache. A missing or older-than-15-minutes mapping
fails closed: no event is recorded, no Capture or Candidate sync starts, and no
Decision is injected, while the ordinary Codex Turn continues unchanged. An
administrator disabling a repository takes effect no later than the next
successful refresh or cache expiry.

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

The click on **Publish** is the explicit publication action. Its request
contains the exact displayed Candidate IDs, revisions, content digests, and
accept/reject choices. The server transaction records the Review batch and
freezes the accepted formal bytes before asking the Registry adapter to commit.
If any displayed revision or digest is no longer current, the whole request is
stale and writes neither Review nor Registry state.

Central web publication uses Decision schema version 2 rather than forging a
Codex Turn approval compatible with manual V1. Its approval provenance contains
the stable organization actor ID, stable web Review action ID, and server UTC
approval time. The formal document also contains its publication batch ID.
Email addresses and authentication tokens are not copied into the Registry.
Existing V1 Decision revisions remain readable and immutable under their
original schema.

## 13. Recall and injection

The Local Agent incrementally caches current formal Decision revisions by
product. `UserPromptSubmit` performs no blocking central request. It resolves
the repository, reads the local product cache, uses the current prompt only for
local applicability, and returns complete applicable Decisions as bounded
`additionalContext`.

The central service receives a product-scoped Decision synchronization request,
not the user's prompt. Injected entries include Decision ID and revision. No
applicable Decision produces no filler context. When the central service is
offline, a cache entry may still be used with its recorded version and update
time; failure to load a safe cache produces no injection and never blocks the
prompt.

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

Pass when a locally installed Plugin contributes its Skill, receives the four
configured Hook events in a new conversation, completes Hook trust, works in an
arbitrary test repository without repository `AGENTS.md`, and stops all
behavior when disabled.

### Gate 2: Hook latency and Agent lifecycle

The recommended runtime is an on-demand singleton Worker. A Hook appends an
event, wakes or starts the Worker, and exits. The Worker drains work and exits
when idle.

Pass when Hook p95 duration is at most 150 ms, 100 rapid test events are neither
lost nor processed twice, concurrent wake-ups leave one active Worker, a crash
leaves unfinished work retryable, and a central outage does not affect Codex.

The sole fallback is an OS-managed user service. If both runtimes fail, stop
with a local-background-runtime feasibility failure.

### Gate 3: Automated app-server Capture

Pass when the Agent starts from a real Hook Session ID, reads the completed
boundary, forks without modifying the source Session, completes both existing
Capture stages, records native Turn identities, and returns one validated
Candidate set without transcript-format parsing or duplicate retry results.

Failure of both the host-capability and controlled-process routes is a blocking
result. Later gates do not proceed.

### Gate 4: Cross-Session convergence

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
must return `ambiguous`, not a silent merge or deletion.

### Gate 5: Central service and identity lanes

Pass when unauthorized requests fail, repository mapping controls product,
Candidate synchronization is idempotent and monotonic, forbidden raw-content
fields are rejected, central persistence contains no raw conversation content,
Codex reaches the central MCP server through development OAuth/OIDC, and the
Agent uses either a verified safe Plugin channel or an independent test
credential without reading Codex private credential storage.

### Gate 6: Minimal Review and publication

Pass when the page shows product-isolated current revisions, accepts batch
accept/reject choices, invalidates stale Review, publishes accepted items as
formal Decisions, and causes the central service to create exactly one
product-correct Registry commit for the batch.

### Gate 7: Recall and `UserPromptSubmit`

Pass when injection p95 is at most 200 ms without a synchronous central call,
only applicable current-product Decisions are injected, Decision IDs and
revisions are visible, no prompt is sent centrally, a safe cache works offline,
no applicable result produces no context, and fixed English and Chinese test
prompts rank the expected seeded Decision inside the bounded result.

### Gate 8: Real end-to-end acceptance

Use one centrally registered test repository and three Sessions:

1. Session A performs development work.
2. Session B continues or reverses one choice.
3. The Plugin produces reconciled Candidates without a manual compression
   conversation.
4. The user reviews and publishes from the minimal page.
5. Session C starts a new task and receives the relevant formal Decisions on
   its first prompt.

Pass only when the central data audit finds no raw Session content, Candidate
state has no unresolved duplicate current conclusions, publication targets the
correct product, Session C identifies the supplied Decision revisions, and
failure of any ZDecision component does not block normal Codex work.

## 16. Execution and stopping rules

Run gates in order because Gates 2 and 3 can invalidate the architecture. Each
gate permits one recommended implementation, one predeclared fallback where
specified, and one focused correction of a confirmed defect. A failed gate
produces evidence and a bounded architecture decision; it does not trigger a
new broad review.

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
