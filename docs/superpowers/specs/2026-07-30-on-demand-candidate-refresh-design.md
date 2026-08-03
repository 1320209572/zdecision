# ZDecision On-Demand Candidate Refresh Design

**Status:** Packet 1 Gates A–C implemented and accepted on 2026-07-31;
runtime model-profile lifecycle amendment approved for implementation planning
on 2026-08-03.

**Scope:** Pre-Demo technical loop for the installable Codex Plugin.

**Supersedes:** The automatic eligibility, `report_work_state`, and zero-touch
Candidate-generation contracts in
`2026-07-30-plugin-feasibility-design.md` and its two implementation plans.

The existing manual V1 domain remains the proven foundation for Capture,
Review, publication, Registry storage, and Decision use. This design changes
how the Plugin selects source work and starts Candidate generation.

## 1. Product decision

The first Plugin loop does not attempt to infer that a feature is complete.
The Plugin observes eligible repository activity automatically, but Candidate
generation starts only when the user clicks **更新候选决策 (Update Candidates)**
on the ZDecision page.

That click is an intentional semantic boundary. It replaces all of these user
steps:

- finding one or more Codex Session IDs;
- opening a separate decision-compression conversation;
- choosing source boundaries manually;
- running a CLI command;
- merging results from multiple Sessions;
- retrying individual extraction stages.

The user still reviews, accepts, rejects, and publishes Candidate decisions on
the page. Published Decision recall remains automatic in later Codex work.

The complete loop is:

```text
development in an enabled repository
  -> Plugin records bounded local activity and Session checkpoints
  -> user clicks Update Candidates for that repository
  -> central service creates a durable Capture Request
  -> local Agent claims the request
  -> app-server reads eligible changed Sessions and runs two-stage Capture
  -> local reconciliation produces current Candidate revisions
  -> only structured Candidates synchronize to the Review page
  -> user accepts/rejects and explicitly publishes
  -> formal Decisions enter the product Registry
  -> local cache recalls applicable Decisions into later Codex tasks
```

No click means no model-based Candidate extraction. Hooks may continue to
record local facts, refresh Decision caches, and serve recall without creating
Candidates.

## 2. Non-negotiable boundaries

- Only organization-registered and enabled Git repositories are observed.
- Raw Sessions, Prompts, model context, tool output, source code, diffs, and
  complete app-server Thread data never enter the central service.
- A Capture Request contains repository and operation metadata, not Session
  content or Session selection supplied by the browser.
- The local Agent derives Session membership from trusted local facts and
  app-server identities.
- The central service derives organization and actor from authenticated
  identity and product from the server-side repository mapping.
- Candidate content is private review material, not formal project memory.
- Only explicitly reviewed and published Decisions enter the Registry.
- Plugin, Agent, network, Capture, or central-service failure never blocks
  ordinary Codex development.
- One Session need not equal one feature, and one feature may span Sessions.
- A commit, push, test result, Stop, silence, or SessionEnd is never a required
  Capture trigger.
- Non-code task capture is outside this first technical loop.

## 3. Scope and deferrals

The technical loop supports Codex Desktop on macOS, one test organization, one
test identity, and one active device per user. The implementation must keep
the identity and device boundaries replaceable.

Deferred until the loop succeeds:

- company-email OIDC/SSO;
- production visual design;
- multiple active devices competing for one request;
- Windows, mobile, and IDE Extension support;
- scheduled or confidence-based automatic Candidate refresh;
- organization-wide approval hierarchies;
- automatic mutation of a published Decision;
- non-code Sessions and repositories without a Git identity;
- generalized workflow engines or distributed schedulers.

## 4. Component architecture

```text
Codex Desktop
  |
  +-- ZDecision Plugin
  |     +-- lifecycle Hooks
  |     +-- setup/status Skill
  |     +-- local Agent executable
  |           +-- Event Ledger
  |           +-- Repository Resolver
  |           +-- Session Index
  |           +-- Capture Request Client
  |           +-- app-server Gateway
  |           +-- Candidate Reconciler
  |           +-- durable Candidate Outbox
  |           +-- Decision Cache and Recall
  |
  +-- Codex app-server

ZDecision central service
  +-- identity and repository configuration
  +-- Capture Request API and progress stream
  +-- Candidate Inbox
  +-- Review and Publication service
  +-- formal Decision query and version stream
  +-- minimal web page
```

The Plugin is not a replacement conversation runtime. Codex app-server remains
the authority for Thread and Turn content. The local Agent owns private
automation and evidence. The central service owns shared requests, Candidate
Review, publication, and formal Decision truth.

## 5. Repository and Session observation

### 5.1 Repository scope

Hooks and app-server records provide the current Session ID and working
directory. The local Agent resolves:

```text
Session ID -> cwd -> normalized Git remote -> repository ID
           -> signed organization mapping -> enabled product
```

The repository ID is a credential-free digest of the normalized remote. A
missing, unregistered, disabled, stale, or conflicting mapping prevents
Capture and upload while leaving Codex unaffected.

The Agent does not parse arbitrary shell syntax to determine repository scope.
Commit and validation facts may be retained as local diagnostics, but they do
not select Sessions or authorize Candidate generation.

### 5.2 Event Ledger

Hooks append bounded, idempotent facts to the existing local SQLite Event
Ledger. A Hook does not call a model, parse a transcript, or wait for the
network.

Event consumption and Session handling are separate concepts. Consuming a Hook
event means its facts have been indexed; it does not mean the Session has been
captured.

### 5.3 Session Index and checkpoints

For every eligible Session, the local Session Index records at least:

```text
session_id
repository_id
branch/worktree lineage when available
latest durable completed Turn checkpoint
last successfully handled checkpoint
source fingerprint
last observed time
```

Subagent Sessions are excluded unless a later specification explicitly makes
them first-class sources. A changed Session remains eligible even when an
earlier checkpoint from the same Session was handled.

The correctness key is therefore not Session ID alone. It includes Session,
lineage, upper checkpoint, and source fingerprint. A failed or unacknowledged
Capture never advances `last successfully handled checkpoint`.

## 6. Update Candidates request

### 6.1 Page action

The Review page exposes **更新候选决策 (Update Candidates)** after the user
chooses a product or repository. The browser sends only:

```text
repository_id
template_id
client_action_id
```

The server supplies organization, actor, request ID, and creation time. It
rejects unknown fields, especially raw Session or Prompt data.

### 6.2 Durable request lifecycle

The central request lifecycle is:

```text
queued -> claimed -> running -> succeeded
                  |          +-> succeeded_no_candidates
                  +----------> failed_retryable -> queued
                  +----------> failed_terminal
                  +----------> cancelled
```

`client_action_id` makes a retried button action idempotent. The first Demo
allows one active request per repository and one active device for the user.
Another click while that request is active returns the same request.

The page may close and reconnect. Request status and progress therefore live
in durable central storage; browser local storage is only a convenience.
Progress events have stable monotonically increasing sequence numbers.

### 6.3 Local delivery

A central page cannot directly read or wake local Codex state. The installed
Agent must therefore maintain an authenticated background request channel.
For this technical loop, installation registers an OS-managed user service or
an equally persistent official Plugin background facility. Hook-only wakeups
are insufficient because a user may click the page after a Codex Session has
ended.

The Agent long-polls or incrementally polls by durable cursor, claims a request
with a renewable lease, and heartbeats while running. If the Agent is offline,
the request remains queued and the page says it is waiting for the device.

## 7. Frozen local Capture boundary

When the Agent claims a request, it snapshots the eligible local upper
checkpoints for that repository. Activity arriving after the snapshot belongs
to the next click.

For each Session whose upper checkpoint differs from its last successfully
handled checkpoint, the Agent records an immutable source operation:

```text
request_id
repository_id
session_id
lineage
previous_handled_checkpoint
requested_upper_checkpoint
source_fingerprint
template snapshot
model profile
```

The first technical loop uses the app-server-retained context at the requested
upper checkpoint. `previous_handled_checkpoint` decides whether the Session
changed and supplies provenance; the system does not reconstruct or upload raw
JSONL deltas. If repeated full retained context later proves too expensive, a
separate measured optimization may introduce a lower-bound focus contract.

## 8. Two-stage Capture and reconciliation

For every changed Session operation, the existing app-server Gateway performs:

```text
thread/read at durable upper checkpoint
  -> fresh persisted read-only fork for one disposable attempt
  -> turn/start Inventory with frozen template and model
  -> validate complete Inventory
  -> turn/start Extraction
  -> validate zero or more Candidate Observations
```

There is no eligibility assessment Turn. The user's page action is the Capture
boundary. Zero Candidates is a successful result.

### 8.1 Runtime model-profile lifecycle

The feasibility-only rule that permanently froze the first complete
`model/list` digest does not apply to the long-running Plugin. Codex may add,
remove, or reorder model catalog entries without invalidating an otherwise
supported extraction profile.

The local Agent maintains one active extraction profile containing an exact
`model_id`, reasoning effort, profile ID, discovery digest, and discovery time.
When a new Capture Request with at least one changed source is first processed,
the Agent discovers the current app-server catalog and resolves the request
profile exactly once. A request with no changed source performs no model
discovery and completes with zero Candidates:

1. If the active profile's exact model and reasoning effort are still
   supported, the Agent reuses that profile even when the complete catalog
   digest changed.
2. If no active profile exists, or its exact model and effort are no longer
   supported, the Agent selects the default model and explicit default effort
   returned by app-server discovery and atomically rotates the active profile
   to a new profile ID. It never guesses a model slug.
3. The selected profile is persisted in the private request freeze before any
   source operation is created. Every new source operation in that request
   copies the same complete profile into its immutable input.

Rotation uses compare-and-swap against the previously active profile. A
concurrent loser rereads the winner and may continue only when that exact
winner is supported by its observed catalog. A catalog change by itself is
never a `ModelDiscoveryConflict` and must not surface as an
`unexpected_processor_error`.

An existing source operation never consults or adopts a later active profile.
All retries and disposable generations use the profile frozen in that
operation. If app-server no longer accepts that frozen model, the operation
stops with an explicit `frozen_model_unavailable` terminal result; it does not
silently change model mid-operation. Because the handled Session checkpoint
has not advanced, a later user refresh can create a new request using the then
active profile.

The existing singleton profile row is reinterpreted in place as the initial
active-profile record. Rotation replaces that record through compare-and-swap;
immutable source operations retain every historical profile needed for replay
and audit. No Capture artifact, Candidate, or handled checkpoint is deleted
during migration. A pre-amendment request that already froze Session sources
but has not created a source operation fills its missing request profile once
on its next retry; a request with any existing source operation derives the
request profile from that operation and rejects a mixed-profile replay.

Strict existing limits, frozen prompts, output digests, and native Turn IDs
remain in force. The durable business operation is separate from native model
execution. If a fork or Turn result is unknown, the whole native attempt is
abandoned and a higher generation reruns Inventory and Extraction in a fresh
fork. Only the active generation may win the operation CAS, so model execution
is at-least-once while Candidate effects remain exactly-once. Reconciliation
uses the same disposable-attempt and generation-fencing rule, then commits its
result, family heads, and immutable outbox in one SQLite transaction.

Candidate Observations from all Sessions in the request are reconciled against
current product Candidate families as exactly one of:

- `same`: retain the current revision and add provenance;
- `refine`: create a new narrowed or expanded Candidate revision;
- `replace`: create a new revision that supersedes a contradicted revision;
- `unrelated`: create a new Candidate family;
- `ambiguous`: retain the observation locally and publish no guess.

A family has a stable `family_id`; revisions are monotonic. Every observation
records its source checkpoint and digest. A later Session may refine or replace
an earlier Session's result.

Only validated, non-ambiguous current Candidate revisions synchronize to the
central Inbox. The local handled checkpoint advances only after the central
service idempotently acknowledges the complete request result, including a
valid zero-Candidate result.

## 9. Candidate Review and publication

The request itself supplies the user-selected readiness boundary, so the
automatic `observing -> review_ready` stability gate is removed. A validated
current Candidate becomes visible after synchronization.

The page supports batch accept, edit-and-accept, reject, and skip. Product is
not editable; a wrong repository or product requires another refresh from the
correct route. A newer Candidate revision makes an older un-published Review
stale.

Accepting a Candidate does not publish it. Publication still freezes an exact
preview, requires a separate explicit page action, and produces one
product-isolated Registry commit for the accepted batch. The central service
is the only Registry writer. Existing exact-byte preview, idempotency, commit
adoption, push recovery, and ambiguity rules remain unchanged.

Published Decisions are immutable revisions. A later Candidate that changes a
published rule requires a new explicit Review and a later Decision revision or
superseding operation; Capture never mutates a Decision silently.

## 10. Automatic Decision recall

Candidate refresh is user-triggered; Decision recall is automatic.

After onboarding, the Agent maintains a signed, versioned repository mapping
and complete formal Decision snapshot for each enabled product. A publication
increments the product Decision version and organization sync cursor. The
Agent advances by cursor; notifications are wakeup hints, not correctness.

`UserPromptSubmit` ranks locally and never sends the Prompt to the central
service. It injects at most eight complete Decisions and at most 10,000 UTF-8
bytes. It never truncates a Decision.

Task Usage records an `active_injected_set` of Decision ID, revision, and
content digest for the current repository route and context epoch:

- an ordinary empty-match Prompt does not clear the set;
- an already active revision is not injected again;
- repository/product change clears it;
- invalidated Decision revisions are removed;
- `SessionStart(source=compact|clear)` opens a new context epoch and restores
  the still-valid active set exactly once;
- `startup` and `resume` do not create a context epoch;
- `SessionEnd` closes the Session's injection state.

Cold or invalid cache state injects nothing. A stale signed cache may remain
usable for the separately specified offline grace period, with visible stale
status. Normal Codex work always continues.

## 11. DeepTutor reuse policy

The design was informed by DeepTutor commit
`731410e45dd455c34707ad28e001e2b3545c2945`, licensed under Apache-2.0.
ZDecision does not install DeepTutor as a runtime dependency.

### 11.1 Near-direct reuse candidates

- the pure entity/fingerprint snapshot types and `added/modified/removed` diff
  behavior from `services/memory/snapshot/entity.py` and `diff.py`, extended
  with ZDecision Session checkpoints;
- the browser start/status/reconnect interaction from
  `web/components/memory/useMemoryRun.ts`, rewritten against durable Capture
  Request APIs and monotonic server event cursors;
- small Codex metadata parsing fixtures for `session_meta.cwd`, stable Session
  ID, and subagent exclusion, only as a local diagnostic fallback if the
  official app-server route cannot provide the required metadata.

### 11.2 Behavior and tests to port, not runtime code

- advance a checkpoint only after successful persistence and acknowledgement;
- keep an old state and retry from the same source after failure;
- invalidate or separate state across conversation branches;
- rebuild from authoritative retained context rather than repeatedly folding
  a summary when feasible;
- bind trusted repository, Session, Turn, template, model, and idempotency
  values in the host rather than accepting them from model or browser fields;
- cite only allowlisted source identities;
- return an explicit empty result rather than manufacturing content;
- use stable IDs for edit/replace and prevent repeated append duplicates;
- reconnect progress streams from a cursor and reject a second active run for
  the same key.

### 11.3 Code not transplanted

- the generic L1/L2/L3 Markdown memory store;
- DeepTutor's in-memory RunManager, whose active execution is lost on restart;
- the full-session import API and storage path, which uploads transcripts and
  does not correctly refresh a growing imported Session;
- the Notebook-specific `write_note` implementation;
- the DeepTutor Agent loop, tool registry, event bus, provider stack, and
  authentication subsystem;
- tolerant JSON repair, because ZDecision model outputs remain fail-closed.

Before distributing any copied or closely adapted file, the repository must:

1. establish ZDecision's outbound license;
2. add `THIRD_PARTY_NOTICES.md` with the Apache-2.0 source and fixed upstream
   commit;
3. retain applicable copyright and license text;
4. mark adapted files as modified;
5. port the relevant upstream tests and add ZDecision privacy, durability, and
   cursor tests.

## 12. Existing implementation impact

Keep and adapt:

- Plugin packaging and lifecycle Hooks;
- repository normalization and enabled-repository gate;
- SQLite Event Ledger, event idempotency, leases, and singleton locking;
- typed app-server JSONL transport and Gateway;
- two-stage Capture, templates, strict validation, and private artifacts;
- Candidate Review, publication, canonical JSON, and Git recovery;
- product-isolated Decision Registry.

Retire or replace:

- `capture/eligibility.py` and `capture-eligibility-v1.md`;
- required `report_work_state` and `submit_current_boundary` tools;
- automatic boundary-assessment states and database records;
- automatic assessment portions of `capture_runner.py`;
- command-pattern classification as a Capture trigger;
- the active-Session-only `ProbeSyncPoller`;
- zero-touch Candidate acceptance tests and their Skill instructions.

Add:

- central Capture Request contracts and durable store;
- persistent local request client and device status;
- local Session Index and successful-handled checkpoints;
- durable local Capture Request mirror and Candidate outbox;
- central Candidate Inbox, Review API, publication API, and minimal page;
- durable progress events and reconnect endpoint;
- signed Decision cache, cursor synchronization, ranking, Task Usage, and
  context-epoch restoration.

The old automatic implementation plans are historical evidence only. A new
implementation plan must begin from this design after written-spec approval.

## 13. Acceptance gates

### Gate A: Observation and repository scope

- The installed Plugin observes a normal development Session in a registered
  repository without user ZDecision instructions.
- The Session Index records its latest durable completed Turn.
- An unregistered repository and a subagent Session produce no Capture source.
- No raw Prompt, message, diff, code, or tool output enters central storage.

### Gate B: Durable page request

- One click creates one idempotent request for the chosen repository.
- A repeated click while active returns that request.
- If the device is offline, the page shows queued/waiting and later proceeds
  without another click.
- Refreshing or reopening the page reconnects from a monotonic event cursor.
- A service restart preserves the request and its terminal result.

### Gate C: Local multi-Session Capture

- One request snapshots every changed eligible Session upper checkpoint.
- One Session, multiple Sessions, two features in one Session, and a later
  reversal produce the expected family and revision structure.
- Zero durable decisions returns `succeeded_no_candidates`.
- Activity after the frozen upper boundary waits for the next click.
- A crash resumes a durable validated or committed operation result; an
  unknown app-server result starts a higher disposable generation without
  duplicate Candidate effects.
- A late abandoned generation cannot alter the winning Candidate family or
  outbox batch.
- Handled checkpoints advance only after central acknowledgement.
- An unrelated model-catalog change reuses the still-supported active profile
  and does not fail the request.
- Removal of the active model before request freezing rotates once to a new
  supported profile; every source operation in that request freezes the same
  profile ID.
- Replaying an existing operation after active-profile rotation continues to
  use its original frozen profile, while an unavailable frozen model produces
  `frozen_model_unavailable` rather than an implicit substitution or an
  unexpected processor failure.
- Concurrent profile resolution produces one active winner and no request
  containing mixed source-operation profiles.

### Gate D: Web Review and publication

- The page shows only current product-isolated Candidate revisions.
- Batch accept/edit/reject works and stale revisions cannot publish.
- Explicit publication creates exactly one product-correct Registry commit.
- Commit/push crash points recover the exact frozen publication or stop as
  ambiguous without creating another Decision.

### Gate E: Cold-start and automatic recall

- A new device reaches a valid signed cache without test seeding.
- A newly published Decision reaches the Agent by cursor.
- A relevant first Prompt receives complete Decision revisions locally.
- A second Prompt does not repeat them.
- An empty-match continuation preserves `active_injected_set`.
- Context compaction restores that set once; the next Prompt does not repeat
  it.
- Central request logs prove that no user Prompt was sent for ranking.

### Gate F: Real end-to-end acceptance

1. The user develops normally in one or more Sessions in an enabled repository.
2. The user opens the page and clicks **更新候选决策** once.
3. The system selects changed Sessions, extracts, reconciles, and displays
   current Candidates without a Session ID, CLI command, or extra Codex task.
4. The user accepts/rejects and explicitly publishes.
5. A new Codex Session receives the relevant formal Decisions automatically.

The Gate fails if Candidate generation begins without a page request, requires
manual Session selection, leaks source content centrally, duplicates a current
family, or needs another manual synchronization step before recall.

## 14. Stopping rule

Implement one Gate at a time. Each Gate permits one focused correction of a
confirmed defect. After Gate F, run one focused suite, one full suite, and one
real end-to-end run. Do not start another broad architecture audit or unbounded
hardening cycle. Record non-blocking improvements as later work.
