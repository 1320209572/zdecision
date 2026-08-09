# ZDecision Recall User-Confirmation Entry Design

Status: Approved for Gate E0 planning and execution on 2026-08-09.
Production activation remains gated on E0 passing and a separately reviewed
implementation plan.

Date: 2026-08-09

## 1. Decision

ZDecision Recall no longer treats Plugin attachment, Skill selection, Prompt
text, or model intent as authorization. Those signals may start the workflow,
but only a native MCP user-confirmation result with `action = accept` may
authorize Recall for the current Codex Session.

The user-facing flow is:

1. The user selects or attaches ZDecision on the first or any later Turn and
   writes the normal development request.
2. ZDecision's first workflow action requests native confirmation from Codex:
   **启用本任务决策召回** or **暂不启用**.
3. Only **启用本任务决策召回** starts product routing, retrieval, Decision
   applicability, and injection.
4. One accepted confirmation remains effective for the current Session under
   the existing resume and context-compaction lifecycle.
5. Decline or dismissal leaves the Session recall-disabled. The user may
   explicitly select ZDecision again on a later Turn, but the system never
   re-prompts automatically.

Only published formal Decisions are recallable. Candidate, Review draft,
Capture input, Prompt, transcript, source, diff, and tool output are outside
the Recall corpus.

## 2. Why the previous entry gate is replaced

The 2026-08-08 explicit-Skill design attempted to prove authorization by
reading the in-progress Turn through the existing Desktop App Server. Gate 0A
failed because the current Desktop does not expose a supported App Server
transport to the local Plugin MCP process. A model-visible task-summary API
also flattens user input to text and does not retain the structured Skill
identity required by that design.

That failure does not mean the user lacked intent. It means ZDecision could
not convert Plugin or Skill selection into a host-verifiable authorization
fact. This design moves the authorization boundary to a user response that the
MCP client returns directly to the originating MCP tool call.

This document supersedes the entry authority, Gate 0A/0B, Skill-selection
proof, and activation-order requirements in
`2026-08-08-recall-plugin-entry-boundary-design.md`. It does not change the
approved Recall Intent, trusted Decision distribution, hybrid retrieval,
reranking, applicability, active-set, context restoration, Fork, Capture,
Candidate Review, publication, Registry, or Central contracts.

## 3. Chosen mechanism and fallback

### 3.1 Chosen: native MCP form elicitation

`activate_zdecision_recall` becomes an asynchronous MCP tool. During that tool
call, the server uses the request-scoped MCP context to issue a form-mode
elicitation with a bounded confirmation schema.

The confirmation contains no Prompt or Decision content. Its message contains
exactly:

- the action: enable published-Decision Recall for this task;
- the already verified repository display name;
- the lifetime: current Codex Session; and
- the two semantic outcomes: enable or do not enable.

The requested form schema is a closed empty object: the user is not asked to
type or supply any value. Codex owns the visible action labels and returns
`accept`, `decline`, or `cancel`; E0 records the actual Desktop presentation
without assuming that the host uses ZDecision's preferred Chinese button
labels.

The server consumes the MCP result directly. The model does not receive or
supply a `confirmed` boolean, confirmation token, Session ID, Turn ID, CWD,
repository ID, product ID, or actor identity as tool input.

Only `action = accept` grants consent. `decline`, `cancel`, malformed results,
missing client capability, timeout, MCP restart, or transport loss grant no
authority.

### 3.2 Fallback: MCP Apps app-only confirmation card

The existing Candidate-control pattern proves that Codex can render an MCP UI
resource whose action tool is visible to the app but not callable by the
model. That mechanism is the fallback only if the native Elicitation gate
fails.

Fallback is not automatic. It requires a new user decision and an amended
design because it changes the visible interaction and replay contract.

### 3.3 Rejected

The following remain non-authoritative:

- whole-Plugin attachment alone;
- a structured or textual Skill mention;
- `$decision-recall`, `@zdecision`, Plugin URI text, or Prompt markers;
- a model-authored confirmation value;
- a Hook inference that the user probably intended Recall;
- a second controlled App Server process; and
- post-hoc transcript or rollout parsing.

## 4. Plugin and Skill interaction

ZDecision remains one installed Plugin with independent Recall and Candidate
capabilities.

For the product flow, selecting ZDecision for an ordinary development request
must route to the Recall entry instructions. Their first visible action is the
activation MCP call. No Decision retrieval, injection, command, file change,
delegation, Candidate operation, or substantive assistant answer may precede
the confirmation result.

Candidate refresh remains independent:

- the exact **更新候选决策** workflow does not request Recall consent;
- Candidate controls remain usable while Recall is disabled; and
- a Candidate request never authorizes Recall or makes Candidate content
  recallable.

The installed Skill topology is fixed for this change:

- `skills/zdecision` becomes the explicit-only Recall entry associated with
  the user-visible ZDecision selection;
- the current Candidate instructions move to
  `skills/candidate-refresh`, whose implicit policy is limited to the exact
  approved Candidate status/refresh intent; and
- `skills/decision-recall` is removed after its Recall instructions and
  metadata are consolidated into `skills/zdecision`, so two Recall entries
  cannot compete.

Native acceptance must prove that selecting the user-visible ZDecision entry
loads `skills/zdecision` and causes the activation MCP call before substantive
work. If the current host does not route that selection reliably, stop and
present an explicit Recall-Skill interaction for user approval; do not infer
the selection from Prompt text. Selection is only a trigger for displaying
confirmation; the elicitation response is the authority.

## 5. Trusted activation flow

### 5.1 Before the MCP call

The trusted `PreToolUse` Hook continues to replace model-authored coordinates
with Hook-owned Session, Turn, CWD, enabled-repository, Plugin-root, and bundle
identity. It creates an idempotent activation attempt, not an active Recall
Session.

An activation attempt is bound to exactly:

- one Session ID;
- one originating Turn ID;
- one normalized CWD and enabled repository;
- one installed Plugin bundle digest;
- one opaque operation ID; and
- one request state/version.

The private activation-attempt state is exactly
`pending_confirmation`, `accepted_pending_activation`, `declined`,
`cancelled`, `failed`, or `committed`. It is separate from
`RecallSessionState`. Before acceptance there is no Recall Session row.

The Hook must stop calling the current `bind_activation()` behavior that marks
the Session active before the MCP operation succeeds.

### 5.2 Confirmation

The MCP tool claims the exact pending operation and requests one elicitation.
The operation prevents concurrent or repeated prompts. A retry may reclaim the
same operation only when it can prove the original result was not committed;
it may never turn an unknown result into acceptance.

Result mapping is exact:

| Elicitation result | State and behavior |
| --- | --- |
| `accept` | Record consent, continue the same operation into routing and Recall preparation |
| `decline` | Commit a declined receipt, create no Recall Session, release the Turn for ordinary work |
| `cancel` | Commit a cancelled receipt, create no Recall Session, release the Turn for ordinary work |
| unavailable, timeout, malformed, transport loss | Commit or recover a non-authorizing failure; create no active Session |

Decline and cancel are not permanent opt-outs. A later native user Turn may
create a new activation attempt only after the user explicitly selects
ZDecision again. Lifecycle Hooks and ordinary Prompts do not retry it.

### 5.3 After acceptance

Acceptance authorizes Recall but does not manufacture applicable Decisions.
In one transaction it first changes the attempt from `pending_confirmation`
to `accepted_pending_activation` and creates an `activating` Recall Session.
The same operation then:

1. validates that the repository remains registered and enabled;
2. derives the product; when routing is ambiguous, returns the bounded product
   display-name choices and keeps the Session `activating` until the user
   chooses or explicitly bypasses Recall;
3. resolves the trusted ready/LKG Decision bundle locally;
4. performs hybrid retrieval, reranking, and bounded applicability;
5. commits the active Recall Session, the `committed` activation attempt, and
   the first result receipt atomically in the Recall host store; and
6. returns only complete published-Decision envelopes that passed the existing
   gates.

An accepted operation that has not completed these checks is `activating` and
supplies no Decision context. Sensitive development tools remain gated until
the accepted operation reaches an allowed applied, empty, clarified, or
explicit-bypass result under the existing Recall contract.

An accepted operation with a valid empty applicable set becomes `active` with
an empty active set and releases ordinary work. A post-acceptance validation,
freshness, routing, or provider failure becomes `blocked` and requires the
existing explicit retry/bypass path; it never degrades silently to development
without Decisions.

## 6. Session lifecycle

- **First-Turn enable:** confirmation precedes Recall and substantive work.
- **Later-Turn enable:** the current bounded context may form Recall Intent;
  earlier Turns do not become persisted transcript data.
- **Ordinary later Turns:** no repeated confirmation.
- **Intent change:** use the existing Turn gate and retrieval policy; do not
  request consent again.
- **Context compaction/clear:** restore the active injected set once in the new
  context epoch without requesting consent again.
- **Resume:** revalidate repository, bundle, and freshness before reuse; do not
  request consent again solely because the app restarted.
- **Fork:** starts recall-disabled and requires its own accepted confirmation
  on a native user Turn.
- **Subagent or internal Thread:** remains recall-disabled and cannot inherit or
  obtain confirmation.
- **SessionEnd:** move an active Session to the existing `dormant` state and
  retire pending attempts. A trusted resume revalidates and reuses consent.
- **Terminal close:** move the Recall Session to `closed`; a later new Session
  requires new confirmation.
- **Explicit bypass:** preserve the approved bypass semantics. Re-enabling
  later requires a new explicit selection and confirmation.

## 7. Failure and privacy rules

- No result other than `accept` may be interpreted as consent.
- No active state may exist before acceptance and successful post-acceptance
  validation.
- A failure before the user responds must not block ordinary development
  forever; it returns a bounded unavailable outcome and leaves Recall disabled.
- A failure after acceptance must not silently continue as if Decisions were
  applied. It follows the existing fail-closed Recall/bypass contract.
- Confirmation receipts contain only opaque IDs, bounded state, timestamps,
  schema/version, repository binding, and digests. They contain no Prompt,
  transcript, PRD, source, diff, tool arguments/output, Decision text, or
  absolute Plugin path.
- Central receives no confirmation Prompt, current task content, or Recall
  query. Identity and authorization continue to be derived server-side.

## 8. Feasibility Gate E0

Production behavior must not change until one minimal Desktop probe proves the
actual native Elicitation contract for the installed Codex version.

The probe uses a test-only MCP tool and runs exactly these cases:

1. accept;
2. decline;
3. cancel/dismiss;
4. client capability unavailable; and
5. MCP process restart or transport loss while awaiting input.

The gate passes only if:

- Codex visibly asks the human user instead of letting the model synthesize a
  response;
- the server receives distinct `accept`, `decline`, and `cancel` actions tied
  to the exact originating tool request;
- the model cannot call the continuation with a fabricated answer;
- accept is returned at most once under replay/retry;
- decline, cancel, timeout, restart, and unavailable never produce active
  Recall state; and
- no Prompt, transcript, source, diff, Decision text, credentials, or raw UI
  response is persisted in the bounded acceptance report.

If E0 fails, stop. Do not implement production activation and do not silently
fall back to Plugin-selection proof, Prompt parsing, or a model-authored
boolean. Present the MCP Apps app-only card fallback for explicit approval.

## 9. Production verification after E0

Focused automated tests must prove:

- Hook binding creates only a pending activation attempt;
- the MCP tool accepts no model-authored confirmation field;
- exact accept/decline/cancel/failure mappings and idempotent recovery;
- no Session is active before accepted post-validation commits;
- no-selection and Candidate-only tasks display no confirmation and create no
  Recall state;
- first- and later-Turn confirmation, ordinary later-Turn reuse, compaction,
  resume, Fork isolation, SessionEnd, and bypass/re-enable;
- registered/enabled repository enforcement and monorepo product routing;
- only published Decision revisions enter retrieval or injection;
- no duplicate prompt or duplicate Decision injection; and
- privacy sentinels remain absent from SQLite, logs, reports, Central traffic,
  cache metadata, and receipts.

Real Desktop acceptance must then prove the complete visible flow:

`select ZDecision -> confirmation -> accept -> relevant published Decisions -> ordinary development`

and the negative flows:

- no ZDecision selection -> no confirmation and no Recall;
- Candidate refresh -> Candidate controls only;
- decline/cancel -> ordinary development without Recall;
- later explicit retry -> one new confirmation; and
- accepted but ambiguous product -> visible clarification before Decision
  application.

## 10. Stop rule

This design authorizes only Gate E0 planning and execution after user review.
It does not authorize production implementation before E0 passes. After E0,
write a replacement implementation plan scoped to the activation entry and
reuse the existing retrieval and lifecycle architecture unchanged.
