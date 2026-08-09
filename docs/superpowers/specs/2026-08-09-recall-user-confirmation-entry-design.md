# ZDecision Recall User-Confirmation Entry Design

Status: Approved for implementation planning and execution on 2026-08-09.
Native Elicitation Gate E0 failed; the user explicitly approved the MCP Apps
inline confirmation card as the production entry mechanism.

Date: 2026-08-09

## 1. Decision

ZDecision Recall no longer treats Plugin attachment, Skill selection, Prompt
text, or model intent as authorization. Those signals may render the workflow,
but only the app-only action emitted by ZDecision's trusted inline confirmation
card after the user clicks **启用本任务决策召回** may authorize Recall for the
current Codex Session.

The user-facing flow is:

1. The user selects or attaches ZDecision on the first or any later Turn and
   writes the normal development request.
2. ZDecision's first workflow action renders one inline MCP App with exactly
   **启用本任务决策召回** and **暂不启用**.
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
fact.

The follow-up native form-Elicitation Gate E0 also failed on Codex Desktop
`26.803.41515`: the first requested human `accept` case completed as `cancel`
in about 14 milliseconds without displaying a human confirmation surface. The
bounded evidence is recorded in
`docs/superpowers/acceptance/2026-08-09-recall-elicitation-e0.md`. This result
rejects native form Elicitation for this slice; it does not test or reject MCP
Apps inline UI, which the existing Candidate control has already rendered and
called successfully in the same host.

This document supersedes the entry authority, Gate 0A/0B, Skill-selection
proof, and activation-order requirements in
`2026-08-08-recall-plugin-entry-boundary-design.md`. It does not change the
approved Recall Intent, trusted Decision distribution, hybrid retrieval,
reranking, applicability, active-set, context restoration, Fork, Capture,
Candidate Review, publication, Registry, or Central contracts.

## 3. Chosen mechanism

### 3.1 Chosen: MCP Apps app-only confirmation card

`show_zdecision_recall_confirmation` is the model-visible render tool. It is
read-only and returns `ui://zdecision/recall-confirmation-v1.html`. Its trusted
`PreToolUse` Hook creates one opaque activation attempt and replaces any
model-authored attempt coordinate before the render result is produced.

The card contains no Prompt or Decision content. It displays exactly:

- the action: enable published-Decision Recall for this task;
- the already verified repository display name;
- the lifetime: current Codex Session; and
- the two buttons **启用本任务决策召回** and **暂不启用**.

The UI receives the opaque attempt ID only through tool-result `_meta`, not
model-visible content. A click calls `decide_zdecision_recall`, whose MCP tool
visibility is exactly `app`. Its closed input contains only that opaque attempt
ID and `action = enable | decline`. The model cannot call this action tool or
supply Session, Turn, CWD, repository, product, actor, confirmation, or intent
coordinates to it.

The render call carries the bounded typed `RecallIntent` needed after an
asynchronous click. The local server validates and freezes its canonical bytes
inside the private activation attempt before enabling either button. Raw
Prompt, PRD, transcript, source, diff, tool output, and absolute local paths
are not stored. The card never sends the intent back from the browser.

Only the app-only `enable` transition grants consent. `decline`, dismissal,
expiry, malformed input, missing UI capability, timeout, MCP restart, or
transport loss grant no authority. The card never calls `enable` on load,
retry, remount, restoration, or polling.

After a committed enable result, the app may use the MCP Apps `ui/message`
method to request that Codex continue the current development request. That
message carries only a bounded activation outcome and cannot authorize Recall;
the already committed app-only action remains the sole authority.

This is an explicit trust-boundary decision, not a claim that the host
cryptographically attests a physical click. Safety comes from the trusted
installed UI resource, exact app-only tool visibility, an unguessable
single-attempt binding, server-side Session/repository/bundle validation, and
the prohibition on automatic enable calls. If the host cannot render the MCP
App, Recall stays disabled; there is no automatic fallback.

### 3.2 Rejected native confirmation mechanism

Native MCP form Elicitation is rejected for the current Desktop slice because
Gate E0 did not present a usable human confirmation. It may be reconsidered
only after a later documented host contract and a new explicit product
decision; the production path must not probe or retry it.

### 3.3 Rejected

The following remain non-authoritative:

- whole-Plugin attachment alone;
- a structured or textual Skill mention;
- `$decision-recall`, `@zdecision`, Plugin URI text, or Prompt markers;
- a model-authored confirmation value or direct call to an activation action;
- a Hook inference that the user probably intended Recall;
- a second controlled App Server process; and
- post-hoc transcript or rollout parsing.

## 4. Plugin and Skill interaction

ZDecision remains one installed Plugin with independent Recall and Candidate
capabilities.

For the product flow, selecting ZDecision for an ordinary development request
must route to the Recall entry instructions. Their first workflow action is
`show_zdecision_recall_confirmation`. No Decision retrieval, injection,
command, file change, delegation, Candidate operation, or substantive
assistant answer may precede the card. The model may then emit only a bounded
instruction to use the card while the attempt is pending.

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

Real acceptance must prove that selecting the user-visible ZDecision entry
loads `skills/zdecision` and causes the render tool call before substantive
work. If the current host does not route that selection reliably, stop and
present an explicit Recall-Skill interaction for user approval; do not infer
the selection from Prompt text. Selection is only a trigger for displaying
confirmation; the later app-only click is the authority.

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

The render tool validates and freezes one canonical `RecallIntent` against the
exact pending attempt. A conflicting render, intent replacement, repository
change, expired attempt, or bundle change disables the card and cannot create
a second current attempt for the same Session and Turn.

The Hook must stop calling the current `bind_activation()` behavior that marks
the Session active before the MCP operation succeeds.

### 5.2 Confirmation card action

The app-only action tool claims the exact pending operation. The operation
prevents concurrent or repeated decisions. A retry may read the same terminal
receipt, but it may never replace the first choice or turn an unknown result
into acceptance.

Result mapping is exact:

| Card/action result | State and behavior |
| --- | --- |
| app-only `enable` | Record consent, continue the same operation into routing and Recall preparation |
| `decline` | Commit a declined receipt, create no Recall Session, release the Turn for ordinary work |
| dismiss/no click until expiry or SessionEnd | Retire as cancelled; create no Recall Session |
| unavailable, timeout, malformed, transport loss | Commit or recover a non-authorizing failure; create no active Session |

Decline and cancellation are not permanent opt-outs. A later native user Turn may
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

- No result other than the app-only `enable` transition may be interpreted as
  consent.
- No active state may exist before acceptance and successful post-acceptance
  validation.
- A failure before the user responds must not block ordinary development
  forever; it returns a bounded unavailable outcome and leaves Recall disabled.
- A failure after acceptance must not silently continue as if Decisions were
  applied. It follows the existing fail-closed Recall/bypass contract.
- Confirmation receipts contain only opaque IDs, bounded state, timestamps,
  schema/version, repository binding, and digests. The private pending attempt
  may contain the normalized typed Recall Intent required for the asynchronous
  action, but receipts contain only its digest. Neither contains a raw Prompt,
  transcript, PRD, source, diff, tool arguments/output, Decision text, or
  absolute Plugin path.
- `recall-confirmation-v1.html` is part of the frozen installed Recall bundle.
  A different UI byte sequence, resource URI, or action-tool contract cannot
  reuse an existing attempt.
- Central receives no confirmation Prompt, current task content, or Recall
  query. Identity and authorization continue to be derived server-side.

## 8. Native Elicitation Gate E0 result

Gate E0 is complete and failed. The first Desktop `accept` case returned
`cancel` without a usable human confirmation surface. Its hard stop prevented
the remaining native Elicitation cases and production code was not changed.

The failure permanently blocks native Elicitation for this implementation
slice. It does not block the explicitly approved MCP Apps card path, and it
must not be rerun or reinterpreted as an inline-card result.

There is no replacement feasibility Gate. Implementation reuses the already
proven Candidate MCP App transport and proceeds directly to one focused real
Desktop acceptance after automated tests.

## 9. Production verification

Focused automated tests must prove:

- Hook binding creates only a pending activation attempt;
- the render tool freezes one validated local Recall Intent and exposes the
  attempt ID only through `_meta`;
- the app-only action is absent from model-visible tools and accepts no
  model-authored Session, repository, intent, or confirmation field;
- exact enable/decline/dismiss/failure mappings and idempotent recovery;
- load, retry, remount, restoration, and polling never call `enable`;
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

`select ZDecision -> inline confirmation card -> click enable -> relevant published Decisions -> ordinary development`

and the negative flows:

- no ZDecision selection -> no confirmation and no Recall;
- Candidate refresh -> Candidate controls only;
- decline/dismiss -> ordinary development without Recall;
- later explicit retry -> one new confirmation; and
- accepted but ambiguous product -> visible clarification before Decision
  application.

## 10. Stop rule

This design authorizes one production implementation plan scoped to the
inline confirmation entry, the accepted activation handoff, and their direct
Desktop acceptance. Reuse the existing Candidate MCP App transport and Recall
retrieval/lifecycle architecture unchanged.

Do not create another temporary confirmation Gate, re-run native Elicitation,
expand the retrieval algorithm, redesign Central, or start a new broad review.
If the inline card cannot render, its app-only action is model-visible, or the
real enable click cannot be bound to the current Session and enabled
repository, stop and report that exact failure.
