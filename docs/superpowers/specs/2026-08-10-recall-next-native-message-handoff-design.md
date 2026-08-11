# ZDecision Recall Next-Native-Message Handoff Design

**Status:** Approved for Gate A implementation planning on 2026-08-10. The
Gate A disposable-identity amendment in section 14 was approved on 2026-08-11.
Gates B, C, and D retain their separate planning and acceptance boundaries.

**Scope:** Replace the unsupported Recall authorization and context-handoff hot
path with the MCP Apps capabilities proved by the current Codex Desktop:
app-only tool calls, authoritative local state, `ui/update-model-context`, and
the next native user message. Preserve the approved signed Decision
distribution, local hybrid retrieval, applicability, active-set, lifecycle,
privacy, Capture, Review, publication, and Registry contracts.

## 1. Authority and amendment map

`docs/architecture.md` remains the product architecture authority. This
specification is a bounded amendment to the active Recall specifications and
must be reflected in `docs/architecture.md`, `AGENTS.md`, the installed Plugin
Skill, and their contract tests before the amended Recall slice is claimed
implemented.

This specification amends:

- `2026-08-06-session-opt-in-intelligent-decision-recall-design.md` sections 1,
  6, 8, 13, 15, 16, and the Recall-specific host requirements in sections 18
  and 19;
- `2026-08-09-recall-user-confirmation-entry-design.md` sections 3.1 and 5.3,
  while retaining the app-only confirmation click as the sole authorization;
  and
- `2026-08-10-recall-turn-gate-intent-design.md` only where that document places
  intent derivation after confirmation. Its strict seven-field `RecallIntent`
  and trusted Hook rewrite remain authoritative.

The following Recall-specific requirements are superseded:

- proving authorization or ordering by launching a second App Server;
- reading the active Desktop task with `thread/read`;
- requiring `additionalContext` to appear later as a `hookPrompt` item;
- treating App Server item order as a Recall security proof;
- requiring `ui/message` to start the follow-up Turn; and
- claiming that a Plugin can prevent every assistant text item before Recall.

The amendment does not remove the typed App Server Gateway globally. Capture
still uses the approved App Server task-reading, forking, and Turn-start flow.
No Candidate, Review, publication, Registry, or source-provenance behavior is
changed.

## 2. Evidence and product decision

The real-host result in
`docs/superpowers/acceptance/2026-08-10-recall-mcp-app-host-capability-probe.md`
is authoritative for this slice:

- app-only `tools/call` works;
- authoritative server state survives App remount;
- `ui/update-model-context` works and its private marker was consumed by the
  next native user message without a marker-read tool;
- `message.text` is not advertised and `ui/message` is unsupported; and
- the accepted product route is therefore **next-native-message UX**.

The user explicitly selects ZDecision and writes the normal development
request. Selection may render the Recall workflow, but it is not authorization.
Only the trusted confirmation card's app-only **启用本任务决策召回** action
authorizes Recall for the current Codex task.

The accepted consent is task-scoped inside the same enabled repository, not
permanently bound to the first product. The first attempt and delivery are
frozen to the current target set, but a later validated product change in the
same task and repository creates a new Intent Epoch without another consent
card. It clears the old active set before routing the new product. Moving the
task to another repository is a new authorization boundary and requires a new
confirmation.

Before the card is rendered, ZDecision must resolve a valid registered and
enabled repository, a ready signed Decision generation, a bounded typed
`RecallIntent`, and exactly one default target Decision-space leaf. When the
user explicitly names several products or Shared leaves,
`explicit_multi_space = true` may authorize that exact set under the existing
shared byte budget. Repository membership alone never implies multi-product
recall.

If the target is ambiguous, ZDecision returns bounded candidate display names
to Codex and asks the user to clarify in the conversation. It does not render
the confirmation card, retrieve Decisions, choose the repository as a product,
or load all candidates. The next native user clarification creates a new
preflight attempt. Product selection is not added to the confirmation card.

## 3. Goals and non-goals

### 3.1 Goals

- Preserve one explicit human authorization boundary per Codex task.
- Bind task authorization to one trusted repository and installed Plugin
  bundle, while binding each delivery to an exact target leaf set, typed
  intent, and signed Decision generation.
- Perform local retrieval at the confirmation click and freeze a bounded
  formal-Decision shortlist before context delivery.
- Deliver that shortlist through the proved `ui/update-model-context` path.
- Use the next native user message to classify applicability and atomically
  commit the set that actually governs the task.
- Keep ordinary same-intent Turns free of repeated retrieval or injection.
- Fail closed for product ambiguity, invalid distribution state, delivery
  uncertainty, conflict, and unproven applicability.
- Preserve task privacy and keep Central out of the synchronous Recall path.

### 3.2 Non-goals

- Non-code organizational or personal-memory tasks.
- Automatic Candidate Capture or feature-completion inference.
- Central semantic search or uploading task-derived queries.
- A new conversation runtime, private Desktop IPC, or App Server broker.
- Automatic follow-up messages from an MCP App on the current Desktop.
- Making plain assistant text fully enforceable through Plugin Hooks.
- Central Web, SSO, Git-role authorization, Registry V2, or unrelated UI work.
- Treating a monorepo such as `zstack-ui-next` as one product.

### 3.3 Approaches considered

**Mark Recall active when the user clicks enable** was rejected. The click
proves consent, not that Decision context reached Codex or that any Decision is
applicable to the current feature.

**Record consent on click, then wait until the next message to start retrieval**
was rejected. It adds another model-driven round trip before the system has
frozen a Decision set and leaves more restart and generation-change ambiguity.

**Enable and prepare a frozen delivery on click, then apply it in the next
native message** is selected. It uses only host capabilities proved in the
current Desktop, keeps retrieval local, makes delivery recoverable, and
separates consent from application.

## 4. Chosen flow

The production flow is:

    native ZDecision selection + development request
      -> local repository, catalog, intent, product, and cache preflight
      -> ambiguous: ask in chat; render no card
      -> unique or explicitly named target set
      -> freeze ConfirmationAttempt
      -> render two-button confirmation card
      -> app-only enable click
      -> local hybrid retrieval and frozen RecallDelivery
      -> App calls ui/update-model-context with the complete snapshot
      -> user sends the next native message with the App View attached
      -> Codex classifies the frozen shortlist
      -> trusted application commit
      -> active_injected_set becomes authoritative
      -> affected development proceeds

The card continues to display exactly **启用本任务决策召回** and
**暂不启用**. It may display the verified repository, resolved product or
Shared-leaf display names, and signed-cache freshness. It contains no raw
Prompt, PRD, source, diff, tool output, absolute path, ranking score, or
Decision text before the user enables Recall.

The next-message requirement is explicit UI, not a hidden retry. After a
successful context update the card tells the user to keep the ZDecision App
attachment and send the next normal message. Removing the App attachment may
remove the view-scoped model context; the system then remains unapplied and
must not claim success.

## 5. Trusted inputs and frozen records

### 5.1 RecallIntent preflight

The model-visible render request carries the existing closed seven-field
`RecallIntent`:

- `target_decision_space_ids`
- `explicit_multi_space`
- `feature_goal`
- `domain_objects`
- `repository_relative_paths`
- `constraints`
- `exclusions`

The `PreToolUse` Hook discards model-authored host coordinates and injects the
trusted Session, Turn, CWD, repository, Plugin-root, and operation identity. It
preserves only the schema-valid semantic intent. Raw Prompt or transcript bytes
are not persisted. The local resolver validates target IDs against the signed
catalog and repository mapping before any card is returned.

### 5.2 ConfirmationAttempt

One immutable attempt is frozen to:

- trusted task and originating Turn identity;
- normalized repository and allowed Decision-space mapping;
- validated normalized `RecallIntent` and digest;
- exact resolved target leaf set;
- signed catalog and Decision generation/digests plus freshness state;
- installed Plugin bundle and card-byte digests;
- opaque attempt ID, `recall-handoff-v1` protocol version, expiry, and
  timestamps.

The attempt stores no raw conversation, Prompt, PRD, source, diff, tool output,
or absolute local path. A changed repository, target, intent, signed generation,
Plugin bundle, or expired attempt invalidates the card. It cannot silently
adopt new bytes; a new card and confirmation are required.

### 5.3 RecallDelivery

An accepted attempt produces at most one stable delivery. The delivery freezes:

- one opaque `delivery_id` and attempt ID;
- the exact signed catalog, Decision generation, retrieval profile, and index
  generation;
- the normalized intent digest and target leaf set;
- the ordered shortlist of complete Decision ID, revision, digest, leaf, and
  typed envelope bytes;
- the aggregate snapshot digest, freshness state, and byte count; and
- delivery, application, and terminal receipts.

The existing maximum of eight complete Decisions and 10,000 UTF-8 bytes remains
unchanged. Decisions are never truncated. Retry may reuse only these exact
bytes while their signed authority remains valid.

## 6. State and transaction boundaries

Authorization, host delivery, and application are separate transactions.

### 6.1 Confirmation state

    pending_confirmation
      -> declined
      -> accepted
      -> invalidated

Only `pending_confirmation` may be claimed by the app-only action. Double click,
MCP retry, App remount, and process restart return the same committed receipt.
They never change the first terminal choice. Decline, dismissal, expiry,
malformed input, or transport failure grants no Recall authority.

### 6.2 Delivery state

    preparing
      -> context_prepared
      -> delivery_claimed
      -> host_delivered
      -> application_committed

    preparing / delivery_claimed
      -> blocked / invalidated / delivery_unknown

The app-only enable call starts one idempotent operation; it does not hold a
database transaction open while retrieval runs. A first short transaction
accepts the attempt and records `preparing`. Retrieval runs locally against the
already frozen generation. A second short transaction revalidates that
generation, freezes the exact delivery as `context_prepared`, and grants one
bounded delivery claim. Concurrent callers receive the same in-progress or
terminal operation and never start a second retrieval. A crash resumes from
the last durable state. No stage marks a Decision active.

The App receives snapshot bytes only after owning the delivery claim. It calls
`ui/update-model-context` once with one complete typed handoff snapshot, never
once per Decision. It awaits the request result before calling an app-only
delivery-ack tool. A successful ack changes `delivery_claimed` to
`host_delivered`. The ack proves only that the host accepted the context-update
request; it does not prove that the model consumed or applied the Decisions.

If the context update fails, no ack is sent and Recall remains unapplied. If the
update may have succeeded but the ack is lost, state is `delivery_unknown`.
There is no automatic retry. The card may offer an explicit retry using the
same `delivery_id`, digest, and bytes. A later valid application commit that
names the exact frozen delivery may atomically reconcile an unknown delivery.

### 6.3 Application state

The next native message receives the typed handoff snapshot. Codex classifies
every frozen shortlist item as exactly one of:

- `applicable`: governs the current work;
- `not_applicable`: a retrieval false positive or outside the current feature;
- `conflicting`: conflicts with a native requirement or another formal
  Decision; or
- `uncertain`: potentially relevant, but scope or invalidation conditions
  cannot be established.

Codex calls one model-visible application tool with the opaque delivery ID,
the exact Decision ID/revision/digest tuples, categories, and bounded local
reasons. Its `PreToolUse` Hook injects the trusted current Session and Turn
binding and discards model-authored host coordinates. The server validates the
complete result against the frozen delivery. It rejects missing, extra,
duplicate, substituted, stale, cross-task, or cross-delivery items.

The atomic commit stores the classification, blocked items, intent epoch,
receipt, and `active_injected_set`. Only `applicable` revisions enter that set.
`not_applicable` items do not block. `conflicting` or `uncertain` items pause the
affected work and require focused user resolution under the existing override
contract. A valid zero-item delivery or all-`not_applicable` result commits an
active empty set.

No UI acknowledgement, Hook invocation, retrieval result, or assistant claim
alone marks a Decision active.

## 7. Turn guard and later Turns

After confirmation but before application commit, covered command-executing,
file-mutating, and delegated tools are denied for the affected task. The Hook
returns a non-empty bounded reason and directs Codex to complete or recover the
handoff. The Plugin cannot absolutely prevent arbitrary assistant text before a
tool call; the product and acceptance language must state this limitation.

Once active, every native Turn uses a cheap local intent gate, not a full
retrieval:

- same normalized intent returns `reuse` and injects nothing;
- a meaningful feature, constraint, path, or target change creates a new Intent
  Epoch and performs local retrieval;
- ambiguous routing asks the user and retrieves nothing;
- an explicit recheck performs fresh local retrieval; and
- invalid cache, model, security, or lifecycle state blocks affected work.

The later-Turn gate retains the strict `RecallIntent` schema and trusted Hook
binding but no longer reads App Server state or requires `hookPrompt` evidence.
When retrieval is required, its model-visible tool result supplies the bounded
frozen shortlist directly; Codex performs the same validated applicability
commit before affected mutation. Ordinary “继续,” testing, and fixes within the
same intent reuse the current active set without reranking.

## 8. Active set and lifecycle

`active_injected_set` contains only complete revisions both delivered to model
context and classified `applicable`, keyed by Decision ID, revision, digest,
leaf, generation, intent epoch, context epoch, delivery, and application
receipt.

- Empty-match Prompts do not clear it.
- The same revision is not injected twice in one `context_epoch`.
- A product change retires the old set before new routing.
- A newly published Decision waits for a new Intent Epoch or explicit recheck.
- A revision removed or invalidated by a newer verified signed active-head
  generation loses authority immediately and blocks affected work.
- `SessionStart(source=compact|clear)` increments `context_epoch`, revalidates
  the current set, and restores surviving typed envelopes once through the
  documented Hook additional-context contract. It does not use `thread/read`
  to prove a later `hookPrompt` item.
- Normal Session end moves an authorized task to `dormant`; native resume of
  the same task revalidates before reuse.
- A Fork or subagent task starts disabled and never inherits authorization or
  an active receipt.
- Explicit Session bypass and per-Decision override retain the existing
  bounded user-authority and retirement-marker rules.

## 9. Distribution and retrieval dependencies

The synchronous handoff never queries Central. It uses only published,
lifecycle-active formal Decisions from a fully verified
local `recall_ready` generation or a signed last-known-good generation still
inside its safety lease. New state does not become active until signature,
canonical bytes, completeness, monotonic generation, freshness, model, index,
and coverage validation all pass.

The approved independent BM25, dense embedding, exact path/scope, bounded
union, deduplication, local cross-encoder reranker, threshold, and complete-item
budget remain mandatory. There is no keyword-only fallback and Codex never
receives the whole product corpus.

Central sees only authenticated organization/device sync state, allowed leaf
identities, snapshot cursors, and distribution metadata. Prompt, conversation,
PRD, source, diff, local absolute path, normalized intent, vectors, candidates,
scores, applicability, and active-set state remain local.

Formal Decision text is typed non-executable input. It cannot invoke tools,
authorize mutation, alter Plugin state, or override the latest native user
instruction merely because it contains instruction-like prose.

## 10. Failure and recovery contract

| Condition | Required behavior |
|---|---|
| Repository unregistered, disabled, or unresolved | Render no card; ordinary development remains unaffected. |
| Product ambiguous | Show bounded chat choices; retrieve nothing. |
| Signed generation or model/index not ready | Return a bounded unavailable result with no actionable confirmation card; do not use partial or keyword-only data. |
| Valid signed LKG inside its lease | Permit degraded Recall and display freshness. |
| LKG expired, corrupt, or rolled back | Stop Recall; do not synchronously query Central. |
| Card declined or dismissed | Create no active Recall Session or delivery. |
| Double click, retry, restart, or remount | Return the same terminal receipt and, where one exists, the same frozen delivery; perform no duplicate mutation. |
| Context update rejected | Keep the delivery unapplied and allow explicit retry. |
| Context update result or ack is unknown | Mark `delivery_unknown`; never blindly resend. |
| App attachment removed before next message | Commit no application; instruct the user to reopen the same delivery. |
| Classification missing, malformed, or cross-boundary | Reject and keep covered mutation blocked. |
| Conflict or uncertainty | Pause affected work and ask one focused question. |
| Retrieval returns zero | Commit an active empty epoch and continue. |
| Generation invalidates before application | Invalidate the delivery; create a new preflight/card against the new generation. |
| Crash between any stages | Recover the last durable stage; never infer the next stage from elapsed time. |
| Fork identity cannot be proven | Treat it as a new disabled task. |

An unavailable service never blocks a task that did not opt in. Once the user
has authorized Recall, failure blocks only affected development until retry,
resolution, or explicit Session bypass.

## 11. Compatibility and migration

Existing Candidate, Review, publication, Registry, formal Decision bytes, and
Capture provenance are unchanged.

Recall attempts, deliveries, sessions, gates, or receipts produced by the old
App-Server/hookPrompt protocol do not establish authorization under this
protocol. A protocol-version boundary retires pending states and requires a new
confirmation on first use. Old active or dormant states are not silently
upgraded because their delivery and application proof is absent.

The implementation removes only Recall-specific active-Turn evidence parsing,
second-App-Server construction, native-selection proof, `hookPrompt` ordering,
and `ui/message` continuation. Shared App Server types or adapters used by
Capture remain.

The old `2026-08-06-recall-host-gate.md` and
`2026-08-06-recall-real-session-integration.md` plans are superseded for Recall
and must not be executed as written. The 2026-08-09 inline-confirmation plan is
retained only for the card and app-only consent boundary; its `ui/message` and
post-click Gate path are superseded. The trusted-distribution and
hybrid-retrieval plans remain separate inputs but must implement the provider
and delivery boundaries in this specification.

## 12. Implementation gates

### Gate A: host-native handoff and authoritative state

- Implement the frozen preflight, confirmation, delivery, ack, application,
  idempotency, remount, restart, and mutation-guard contracts.
- Use a deterministic formal-Decision fixture through an explicit provider
  boundary only for automated and Desktop protocol tests.
- Never package or expose that fixture in the production Plugin. Production
  remains bounded unavailable until Gates B and C provide `recall_ready` data.
- Prove no Recall path starts a second App Server or reads the live task.
- Gate A completion does not claim production Decision Recall.

### Gate B: trusted formal-Decision distribution

- Complete clean-device signed catalog and Decision snapshot prefetch.
- Prove canonical bytes, signatures, completeness, high-water monotonicity,
  rollback/freeze protection, LKG, expiry, and atomic activation.
- End in `trusted_data_ready`, not `recall_ready`.

### Gate C: production local hybrid retrieval

- Select and freeze embedding and reranker runtimes on the private bilingual
  benchmark.
- Build and validate full enabled-leaf indexes atomically.
- Prove independent lexical, dense, and path channels, bounded fusion,
  reranking, thresholds, complete-item budgeting, and lifecycle filtering.
- Meet the existing routing, precision, recall, latency, and conflict-safety
  launch gates, extending applicability evaluation to the four categories in
  this amendment.

### Gate D: integrated real Codex development

Real Desktop acceptance covers at least:

1. no ZDecision selection: no Recall state or work;
2. unregistered/unresolved repository: no card;
3. ambiguous product: chat clarification and no card;
4. unique first-Turn and later-Turn product routing;
5. decline, enable, double-click, retry, remount, restart, and expiry;
6. successful context update followed by one native user message;
7. attachment removed, update rejected, ack lost, and explicit exact retry;
8. applicable, not-applicable, conflict, uncertainty, and valid zero match;
9. covered mutation denied until the application receipt commits;
10. same-intent reuse and intent-change replacement;
11. empty “继续” followed by one compact restoration;
12. product change, signed removal, degraded LKG, expiry, corruption, and clock
    rollback;
13. normal Session end/resume and default-disabled Fork/subagent behavior;
14. real formal Decisions from at least one enabled monorepo product leaf; and
15. network proof that no task content or private Recall state reached Central.

The feature is not demo-complete after Gate A or Gate B. It is claimed as real
formal-Decision Recall only after Gates A through D pass.

Each Gate receives its own implementation plan and completion decision. The
first plan written from this specification covers Gate A only; Gates B and C
are reconciled with their existing plans after Gate A fixes the provider and
handoff interfaces. There is no single all-Gates implementation run.

## 13. Test and stop rule

Each Gate uses focused contract tests, one complete suite at its completion,
and no broader suite repetition unless that run itself exposes a confirmed
regression. Gate D performs one bounded real Desktop acceptance and records the
exact host version and capability matrix.

Confirmed Critical or Important defects within the Gate are corrected and
retested. Minor improvements that are not explicit acceptance requirements are
recorded for later work. After the focused suite, one complete suite, and the
bounded real acceptance pass, stop. Do not begin another broad architecture
audit, Skill blind test, or unbounded “final review” loop.

Implementation must proceed one Gate at a time. Failure of app-only tools,
authoritative remount recovery, `ui/update-model-context`, trusted local
distribution, or the four-category quality gate stops the current Gate. It
does not authorize a second App Server, private IPC, transcript parsing,
Central task queries, whole-corpus injection, or fabricated Decision data as a
fallback.

## 14. Gate A disposable Plugin identity amendment

### 14.1 Problem and selected approach

The Gate A automated vertical and real Desktop acceptance must exercise the
production Hook, Plugin-bundle, Store, MCP, App, and application boundaries
without replacing or disabling the installed production Plugin. A disposable
Plugin cannot honestly do that while those boundaries hard-code the production
Plugin name, MCP server key, command, arguments, and Recall Skill path. Giving
the disposable bundle the production identity would collide with the installed
Plugin. Rewriting `PLUGIN_ROOT`, tool names, or bundle bytes in the harness
would test an adapter rather than the production trust boundary.

Three approaches were considered:

- **Reuse the production Plugin identity for the disposable bundle** was
  rejected because the installed production and disposable Plugins could not
  remain distinct stable components.
- **Keep production constants and translate disposable values inside the test
  harness** was rejected because a passing result would bypass the trust
  relationship that Gate A is intended to prove.
- **Inject one immutable, closed Plugin-identity value at code composition
  time** is selected. Production uses an exact constant. The disposable
  harness supplies its own exact value to the same production validators and
  composition functions.

This amendment changes no user-facing production identity and adds no runtime
configuration surface.

### 14.2 Closed identity value

`RecallPluginIdentity` is an immutable value containing exactly:

- `plugin_name` — the manifest and marketplace-entry name;
- `mcp_server_key` — the exact `.mcp.json` server-map key;
- `mcp_command` — the exact configured command string;
- `mcp_args` — the exact ordered tuple of configured argument strings;
- `hook_command` — the exact command string used by every required Hook; and
- `recall_skill_relative_path` — the normalized POSIX path to the Recall
  `SKILL.md` below the Plugin root.

The production constant is exactly:

```text
plugin_name = zdecision
mcp_server_key = zdecision-local
mcp_command = zdecision-agent
mcp_args = [mcp]
hook_command = zdecision-agent hook
recall_skill_relative_path = skills/zdecision/SKILL.md
```

The value is accepted only as an explicit in-process dependency of the Store,
Hook handler, and MCP server composition. It must never be selected or
overridden by an environment variable, CLI flag, model-visible tool input,
Hook input, Plugin manifest extension, database value, or remote response.
Production entry points always construct or use the production constant.

Plugin names and MCP server keys are 1–128 ASCII characters and use the closed
grammar `[a-z][a-z0-9]*(?:-[a-z0-9]+)*`. Underscores, consecutive separators,
leading or trailing separators, Unicode, and case folding are rejected. Within
this grammar the Codex tool namespace is the server key with each hyphen
replaced by one underscore. Because underscores are excluded from source keys,
the mapping is collision-free. Every full Plugin MCP tool name that the Hook
handler references individually for Candidate control or Recall is derived
from this same namespace and one member of the following closed basename sets;
no independent tool-prefix constant is permitted. Other MCP tools remain
covered by the generic `mcp__.*` matcher and are not identity-sensitive Hook
branches.

```text
Candidate:
  show_zdecision_update

Recall:
  show_zdecision_recall_confirmation
  decide_zdecision_recall
  get_zdecision_recall_handoff
  ack_zdecision_recall_delivery
  apply_zdecision_recall_delivery
  gate_zdecision_turn
```

The Recall Skill path must end in `SKILL.md`, contain at most 512 UTF-8 bytes,
be relative and normalized, contain no empty, `.` or `..` segment, and resolve
beneath the verified Plugin root. The command contains 1–4,096 UTF-8 bytes.
The ordered argument tuple contains at most 16 entries, each containing
1–4,096 UTF-8 bytes and no NUL, with at most 16,384 aggregate UTF-8 bytes. The
Hook command also contains 1–4,096 UTF-8 bytes. Both commands contain no NUL.
Bundle validation compares the manifest name, exact MCP key, exact command and
ordered arguments, and exact Recall Skill path to the injected identity before
computing or accepting the existing bundle digest. The validator returns the
verified Plugin root explicitly; callers must not reconstruct it from a fixed
number of Skill-path parents.

The manifest `hooks` field must be absent so the fixed default
`hooks/hooks.json` path is authoritative. That file's security structure is
exactly:

| Event | Matcher | Timeout | Additional-context limit |
|---|---|---:|---:|
| `SessionStart` | `startup|resume|clear|compact` | 3 | 0 |
| `UserPromptSubmit` | absent | 3 | 4000 |
| `PreCompact` | `manual|auto` | 3 | absent |
| `PostCompact` | `manual|auto` | 3 | absent |
| `PreToolUse` | derived rule below | 3 | absent |
| `PostToolUse` | absent | 3 | absent |
| `Stop` | absent | 3 | absent |
| `SessionEnd` | `other` | 3 | absent |

Each event has exactly one entry containing exactly one `type = command` Hook
with the injected `hook_command`. The `PreToolUse` matcher contains, in this
order, the injected namespace's Candidate show, Recall confirmation show,
Recall application, and Recall turn-gate names, followed by
`Bash|apply_patch|Edit|Write|Agent|mcp__.*`. The app-only Recall actions are
covered by the final MCP matcher while their Hook-handler names still derive
from the same identity. Missing events, commands, names, mutation coverage,
changed order or limits, or additional Hook entries fail validation.
Non-security display copy may vary, but it grants no authority.

Hook security fields are parsed and validated before every initial or later
Plugin-root or bundle acceptance. They are not added to the existing content
digest, so existing production rows and the digest algorithm remain unchanged;
durable authorization still fails after any Hook security-field change because
the root cannot pass the mandatory identity validation that precedes digest
comparison.

### 14.3 Production and persistence behavior

The Store owns one immutable identity for its lifetime. Initial attempt
binding, later delivery/application transitions, restart/replay, active-set
reuse, compact restoration, and bundle/root revalidation all use that same
identity. A Store reopened with an identity that does not validate the frozen
Plugin root and bytes fails closed. Cross-identity attempt, delivery, gate,
receipt, or active-set replay never adopts the caller's identity.

No identity field is added to SQLite and the existing bundle-digest algorithm
and protocol version remain unchanged. The frozen Plugin root plus bundle
digest continue to be durable state; every later check recomputes that digest
only after the root has passed the current Store identity's exact manifest,
MCP, command, argument, Hook-security, and Skill-path validation. Thus existing
production rows remain valid under the production constant while a differently
composed Store cannot authorize them.

The production `run_mcp` and Hook CLI accept no identity option. The production
Plugin manifest, `.mcp.json`, Hook command and matcher security fields, command,
arguments, Skill path, and user-visible name remain governed by the production
constant. The identity seam exists to compose verified local code, not to make
the production Plugin dynamically configurable.

### 14.4 Disposable Gate A composition

Task 9 generates one test-only disposable identity that is unique relative to
the production Plugin and uses the same closed grammar. `create` writes one
minimal launcher module below the disposable Plugin root containing the exact
immutable identity fields. The Plugin folder, manifest name, marketplace entry
name, selector, MCP server key, Hook full tool names, Hook command, MCP command,
arguments, and Recall Skill path must all agree with that value. Variable
version, database, and lease values are not identity fields. The marketplace
source resolves inside the fresh disposable root, and all harness processes use
one isolated database below that root.

The root's immutable ownership marker records the launcher's relative path,
byte digest, root device/inode, and generation. Each separate Hook or MCP
process derives the root from the launcher's own resolved path, revalidates the
marker and launcher bytes before importing the identity, and then refuses any
identity mutation. No database row, generic CLI option, environment option, or
manifest extension may construct, override, or substitute identity fields.
The generated command and arguments identify only that launcher and its closed
`hook` or `mcp` subcommand; they accept no root or identity argument.

The harness may inject the validated launcher identity only while directly
composing the production `RecallHostStore`, Hook handler, and
`create_mcp_server` functions. It must not call production `run_mcp`, modify the
production identity constant, rewrite host-provided `PLUGIN_ROOT`, translate
tool names after receipt, copy the trust validator, or monkeypatch bundle
bytes. The deterministic provider remains test-only and performs no network,
Git, Central, Registry, or App Server operation. Inspection and cleanup
revalidate the same root generation and launcher digest; mismatch is a hard
FAIL and cleanup preserves the uncertain root.

Automated tests must prove at least:

1. the production constant preserves every current production name, path, and
   generated full Hook tool name;
2. a valid unique disposable identity accepts only its matching generated
   bundle and Hook/MCP namespace;
3. manifest-name, server-key, command, ordered-argument, Skill-path, and
   namespace substitutions each fail closed;
4. Hook-command, required-event, matcher, timeout, additional-context-limit,
   missing-mutation-coverage, and added-Hook substitutions each fail closed;
5. two server keys cannot normalize to the same accepted tool namespace;
6. a state frozen under one identity cannot be replayed or revalidated through
   another identity;
7. initial binding and restart, delivery, application, reuse, and compact
   revalidation all use the Store's same immutable identity; and
8. the Task 9 automated vertical and Task 10 Desktop run leave the installed
   production Plugin, its marketplace entry, bundle bytes, and production
   database unchanged.

Task 9 stops rather than weakening this contract if Codex does not expose the
exact disposable MCP client, if the host supplies a mismatched Plugin root or
tool namespace, or if acceptance would require a second App Server, private
IPC, manual database mutation, identity translation, or disabling the
production Plugin.

### 14.5 Implementation-plan boundary

Before the existing Task 9 vertical is implemented, its plan must add one
reviewable Task 9A that introduces the production identity seam and its focused
contract tests. Task 9A may add one focused identity module and modify only the
production Store, Hook, MCP composition, and exact tests needed to prove
sections 14.2 and 14.3. It changes no Plugin bytes, CLI surface, SQLite schema,
bundle-digest algorithm, provider behavior, or Gate B/C code.

This amendment supersedes Task 9's earlier statement that generated Hook and
MCP commands invoke the repository harness file directly. Both commands invoke
the repository Python executable with the generated launcher path as the first
argument and the closed `hook` or `mcp` subcommand as the second. The launcher
performs its root, marker, generation, and self-digest verification before it
imports and delegates to the tracked repository harness.

The original Task 9 then composes its deterministic provider and disposable
Plugin through that reviewed seam. It must not absorb Task 9A into a test-only
adapter. Task 10 begins only after Task 9A and Task 9 are independently GREEN
and approved.
