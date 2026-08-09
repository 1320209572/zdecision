# ZDecision Recall Explicit-Skill Entry Boundary Design

Status: Superseded on 2026-08-09 by
`2026-08-09-recall-user-confirmation-entry-design.md`. Gate 0A remains useful
historical failure evidence, but its App Server and Skill-selection proof are
no longer an implementation prerequisite.

Date: 2026-08-08

## 1. Problem and observed host facts

The first real Codex Desktop Host Gate failed after the user attached the
ZDecision Plugin on the first Turn. Codex loaded the broad `zdecision` Skill,
produced development commentary, and completed a file mutation without calling
`activate_zdecision_recall`. The local Recall store consequently contained no
Session, activation binding, or Turn gate.

The first entry-boundary proposal assumed that `thread/read` would preserve a
whole-Plugin selection as a structured `skill` or `mention` input containing a
trusted Plugin path. That assumption is false for the observed Desktop build:

- a privacy-safe read of the completed acceptance Turn returned one Turn and
  zero structured `skill` or `mention` selections;
- the local rollout contained a canonical Plugin URI marker only as text, not
  as a structured selection; and
- the official App Server contract defines explicit `skill` input and App
  `mention` input, but does not define a whole-Plugin mention path, a primary
  Plugin Skill, picker provenance, or guaranteed structured read-back of a
  whole-Plugin attachment.

The design must therefore stop treating whole-Plugin attachment as a proven
authorization primitive. It first tests the narrower official input: explicit
selection of the bundled **ZDecision Recall** Skill.

## 2. Scope and authority

This document amends only the Recall entry and native-selection assumptions in:

- sections 1, 6.1, 6.2, 18, and 19 of
  `2026-08-06-session-opt-in-intelligent-decision-recall-design.md`;
- the global constraints and the scoped activation work in Tasks 2 through 6
  and Task 8 of
  `2026-08-06-recall-host-gate.md`; and
- the failed Task 8 acceptance path that treated whole-Plugin attachment as a
  structured selection.

References in those documents to proving a whole-Plugin `mention`, a
Plugin-root selection path, or a deterministic Hook-side pre-activation
selection backstop are replaced for this slice. Gate 0 does not restore that
backstop. This document does not amend
Recall Intent, retrieval, freshness, active-set, context restoration, Fork,
Capture provenance, Candidate Review, publication, Registry, or Central
contracts.

Gate 0 does not claim Packet 3 complete and does not yet change
`docs/architecture.md`. If both probes pass, the scoped implementation plan and
architecture authority must be aligned before Gate 1 is claimed complete.

The authority this design can test is deliberately precise: a structured
explicit-Skill input inside the trusted Codex Desktop task. It is not a
cryptographic proof that a physical person clicked the picker; the current
official host protocol exposes no such gesture signature. A programmatic
client that can place the same exact structured Skill item into that trusted
user-visible root task is indistinguishable and is inside this technical trust
boundary. Internal Threads and subagents remain excluded by their host-owned
identity, not by attempting to infer who clicked the picker.

## 3. Product interaction

ZDecision remains one installed Plugin. Recall is enabled for a task only when
the user explicitly selects the bundled **ZDecision Recall** Skill on a native
user Turn. The selection may occur on the first Turn or any later Turn, and the
same Session-lifecycle rules continue after successful activation.

The user selects the Skill and writes the normal development request in the
same Turn. There is no separate Enable Recall button.

The following do not activate Recall:

- merely installing or attaching the whole Plugin;
- implicit Skill consideration;
- repository enablement;
- plain text such as `@zdecision`, `$decision-recall`, or a copied Plugin URI;
- assistant initiative, a delegated message without a qualifying structured
  Skill item, tool output, recalled Decision text, or lifecycle events; and
- any subagent or internal Thread, even if inherited text contains the Skill
  name or path.

Candidate refresh remains independent. The exact native request
**更新候选决策**, the existing inline controls, and the verified-completion
presentation boundary retain their current repository gate. They neither
activate Recall nor authorize Capture. If a Turn explicitly selects
**ZDecision Recall** and also requests Candidate refresh, Recall activation is
the first visible workflow action; the read-only Candidate controls may render
only after activation succeeds.

## 4. Skill topology

The smallest compatible Plugin structure keeps the two existing focused
entries instead of inventing a primary Plugin Skill:

```text
plugins/zdecision/
├── skills/
│   ├── decision-recall/
│   │   ├── SKILL.md
│   │   └── agents/openai.yaml
│   └── zdecision/
│       ├── SKILL.md
│       └── agents/openai.yaml
├── hooks/hooks.json
└── .mcp.json
```

`decision-recall` is the only Recall workflow. Its metadata remains explicit
only:

```yaml
interface:
  display_name: "ZDecision Recall"
policy:
  allow_implicit_invocation: false
```

Its instructions require `activate_zdecision_recall` to be the model's first
visible action. No assistant status message, shell command, file mutation,
delegation, Candidate tool, or other MCP call may precede it. Codex's native
tool-invocation UI may show progress without creating an `agentMessage`.

The existing `zdecision` Skill becomes Candidate-refresh/status only. Its
description and default Prompt must not claim Recall activation, completed
development as automatic Capture authority, or a primary-Plugin role. It may
remain implicitly invocable only for the already approved Candidate status and
refresh presentation contract.

After Gate 0 passes and before Task 8 is rerun, the Plugin manifest and user
copy must state that Recall requires selecting **ZDecision Recall**. The
text-only Recall entry is removed from `interface.defaultPrompt`; a default
Prompt cannot manufacture the structured Skill authority required here.

No file is moved or renamed merely to make the directory names symmetrical.
The Plugin manifest has no assumed primary-Skill behavior.

## 5. Gate 0A/0B: prove the actual Desktop protocol first

Gate 0 is a read-only feasibility gate and precedes production changes.
Adding a test-only probe harness is permitted; changing Plugin, Hook, MCP, or
Recall production behavior is not.

### 5.1 Gate 0A — connect to the existing Desktop host

The probe owner is one bounded, test-only local harness started before the
target user Turn. It receives the exact acceptance task ID from the operator
and the exact Turn ID from the existing local Hook event ledger. It never
discovers a target through recency, CWD, transcript, or Prompt matching.

The first supported transport candidate is the already-running Desktop
app-server Unix control socket. The official protocol defines this as WebSocket
messages over a Unix socket using the standard HTTP Upgrade handshake. The
harness must:

1. connect to the existing Desktop-owned endpoint;
2. complete the WebSocket upgrade and app-server `initialize` handshake;
3. record only `route = host_unix` plus a bounded endpoint category, never the
   absolute socket path;
4. read the exact known Thread without resuming it; and
5. close without starting, resuming, steering, forking, or mutating a Thread.

An equivalent host-injected `AppServerTransport` may be used only if Desktop
actually supplies it. The harness and later activation must not launch a
second `codex app-server` process. Current controlled-process fallback is not
evidence for this gate.

Gate 0A passes only if the existing Desktop endpoint completes the handshake
and can read the exact known Thread. If no host endpoint is exposed, the
WebSocket-over-Unix handshake is rejected, authentication cannot be obtained,
or the exact Thread is unavailable, record the bounded failure and stop. Do not
begin Gate 0B or production implementation.

### 5.2 Gate 0B preconditions

- the current local Plugin bundle is installed;
- the bundled **ZDecision Recall** Skill is visible in the native picker;
- the Plugin Hook is enabled and its current definition is trusted;
- the test task is in a registered and enabled Git repository; and
- Gate 0A's already-proven host route is armed before each target Turn.

Hook-disabled, untrusted, or managed-policy-excluded runs cannot claim a
fail-closed Recall boundary and fail this gate explicitly.

### 5.3 Gate 0B probe cases

Run exactly four bounded native Desktop cases:

1. a control Turn without a ZDecision selection; and
2. a Turn that attaches the whole ZDecision Plugin;
3. a Turn containing copied plain text `$decision-recall` without selecting the
   Skill; and
4. a Turn that explicitly selects the bundled **ZDecision Recall**
   Skill.

For each Turn, call `thread/read(includeTurns=true)` while the exact Turn is in
progress. Record only:

- Thread ID equality and active Turn ID equality;
- selection `type` and bounded `name`;
- whether the path is absolute and resolves to the exact installed
  `decision-recall/SKILL.md` under the Hook-supplied `PLUGIN_ROOT`; and
- ordered item types and bounded IDs needed to check activation ordering.

Do not persist Prompt text, transcript, message text, Plugin URI text, tool
arguments, tool output, source, diff, or the full absolute installed path.

### 5.4 Gate 0B pass criteria

Gate 0B passes only when all of the following are true:

- the no-selection, whole-Plugin, and copied-text Turns contain no qualifying
  structured Skill selection;
- the selected Turn contains exactly one qualifying `type = skill` input;
- its bounded name has the stable observed Recall Skill value;
- its normalized absolute path equals the installed
  `decision-recall/SKILL.md` path under `PLUGIN_ROOT`;
- the exact in-progress Turn is readable before any `agentMessage`;
  and
- repeated reads of that Turn return the same bounded selection identity.

The observed bounded name is frozen into the follow-up contract; it is not
guessed in advance. `type = mention`, a Plugin-root path, a canonical textual
marker, and post-hoc rollout parsing cannot satisfy this gate.

### 5.5 Stop rule

If the explicit bundled Skill is absent, loses its structured identity, or
cannot be read while the Turn is in progress, stop. Do not implement the
selection verifier, do not parse Prompt or Plugin-marker text, and do not claim
Task 8 or Gate 1 readiness.

The next product decision would then be explicit: either accept the weaker
`UserPromptSubmit` canonical-marker boundary for read-only Recall, or wait for
a supported host selection signal. That trust reduction is not authorized by
this document.

## 6. Contingent activation boundary after Gate 0

This section becomes implementable only after Gate 0 evidence is recorded and
the exact observed Skill name/path contract is added to the implementation
plan.

### 6.1 No-selection fast path

An enabled repository with no Recall Session keeps the current fail-open path:

- `PreToolUse` does not call app-server to search for a selection;
- no Recall row, negative-probe cache, Turn gate, injection, Central request,
  or recall-specific app-server call is created; and
- Candidate status, Candidate refresh, and ordinary development remain
  unaffected.

This preserves the existing no-selection contract and Hook latency budget.

### 6.2 Explicit activation path

Only an actual `activate_zdecision_recall` MCP call enters the activation path:

1. `PreToolUse` validates the enabled repository, rejects subagents, replaces
   all model-supplied identity with Hook-owned Session/Turn/CWD and
   `PLUGIN_ROOT`, and creates an idempotent short-lived `activating` binding.
2. The MCP reads that exact active Turn through the supported App Server route.
3. It validates the Gate 0-frozen structured Skill identity, installed bundle,
   exact Thread/Turn/CWD, and item ordering.
4. It performs the bounded Gate 1 provider operation.
5. It maps the provider result to the exact state below and atomically commits
   the corresponding receipt before returning.

The Hook does not mark the Session `active` before steps 2 through 4 succeed
with a valid `retrieve` result.
An `activating` binding denies later sensitive tools for its short lease, is
idempotently reclaimable by the same activation operation, and never supplies
Decision context by itself.

Gate 1 freezes provider outcomes as follows:

| Provider result | Gate 1 transition |
| --- | --- |
| `retrieve` with the valid one-shot host probe | atomically commit `active`, probe receipt, and bounded fixture response |
| `blocked` | commit exact-Turn `activation_unproven`; no active Session or Decision context |
| `clarify_product` or any other disposition | treat as unsupported in Gate 1, commit exact-Turn `activation_unproven`, record the bounded failure, and stop acceptance |

The later retrieval packet may implement the already approved
`awaiting_product_clarification` flow. Gate 1 must not simulate it or silently
turn clarification into `active`.

### 6.3 Failure and recovery

`activation_unproven` is a durable state of the exact activation binding, not a
new active `RecallSessionState`. It is keyed by Session and Turn, supplies no
Decision context, and is consulted by the sensitive-tool guard even when no
active Recall Session row exists.

- No qualifying structured Skill or an unavailable active-Turn read replaces
  the provisional binding with terminal exact-Turn `activation_unproven`,
  returns bounded `native_selection_unproven`, and creates no active Recall
  Session. Supported sensitive tools remain denied for that Turn.
- A qualifying installed Skill followed by bundle tamper, wrong
  Thread/Turn/CWD, replay, or an activation-order violation moves the bound
  Session to `blocked`; supported sensitive tools remain denied.
- A foreign Skill with the same display name but a different installed path is
  treated as non-qualifying, not as a reason to block an unrelated task.
- A crash after `activating` but before the receipt leaves no `active` state.
  Same-operation retry may resume idempotently. Otherwise the exact Turn stays
  denied until a later trusted native `UserPromptSubmit`, `SessionEnd`, or
  explicit user disable retires `activating` or `activation_unproven`; wall
  clock expiry alone never reopens the same in-progress Turn.
- Hook-disabled or Hook-untrusted execution is outside the enforcement
  boundary and must be reported as unavailable, never described as
  fail-closed.

This slice permanently removes the proposed Hook-side pre-activation selection
backstop: before the activation MCP call, the Hook has no proven, low-latency
selection signal.
Therefore "activation is the first visible action" remains a real Desktop
acceptance gate. If Codex again emits development text or invokes another tool
first, Task 8 fails and implementation stops instead of adding Prompt parsing
or a synchronous Hook-side app-server probe. Gate 2 must not start until every
Gate 1 Desktop case passes.

## 7. Installed-bundle integrity

The Recall activation digest covers the raw bytes and Plugin-relative paths of
every behavior-bearing Recall entry:

- `.codex-plugin/plugin.json`;
- `.mcp.json`;
- `hooks/hooks.json`;
- `skills/decision-recall/SKILL.md`;
- `skills/decision-recall/agents/openai.yaml`; and
- each exact Recall reference named by that Skill, if references are added.

The digest algorithm is exactly:

1. initialize SHA-256 with UTF-8 domain tag
   `zdecision-recall-bundle-v2` followed by one NUL byte;
2. process the fixed entries above in lexicographic POSIX-relative-path order;
3. for each entry, append its UTF-8 relative-path byte length as an unsigned
   eight-byte big-endian integer, the path bytes, the raw file byte length in
   the same encoding, and the raw file bytes; and
4. use the lowercase hexadecimal SHA-256 result.

Absolute cache paths never enter the content digest. The resolved absolute
paths are checked separately for exact root containment and identity against
the Hook-supplied `PLUGIN_ROOT`.

Candidate-only files are not silently pulled into the Recall digest. Adding or
removing a Recall reference requires an explicit digest-contract update and
tests; recursive directory trust is not allowed.

## 8. Verification

After Gate 0 passes, the focused implementation tests must prove:

- Recall Skill metadata is explicit-only and Candidate Skill metadata retains
  only the approved Candidate behavior;
- no-selection tasks make zero recall-specific app-server or Central calls and
  create no Recall state;
- an activation call validates the exact Gate 0-frozen `skill` identity and
  rejects text, `mention`, Plugin-root, foreign-path, implicit, replay, wrong
  Thread/Turn/CWD, internal-Thread, Fork, and bundle-tamper cases;
- `activating -> active` occurs only with a valid Gate 1 `retrieve` result and
  an atomically committed receipt;
- absent evidence commits exact-Turn `activation_unproven`, proven
  tamper/order violations block, and crash/retry never exposes premature active
  state or reopens the same failed Turn;
- Candidate refresh without Recall remains usable;
- explicit Recall plus Candidate refresh activates first and renders the
  existing controls only afterward;
- no persisted or returned value contains Prompt, transcript, Plugin URI,
  source, diff, tool arguments, or tool-output sentinels; and
- the no-selection Hook path still meets its existing latency budget.

Real Desktop acceptance then reruns the existing seven Task 8 cases with the
explicit bundled Recall Skill. The activation item and receipt must precede
any `agentMessage`, command, file change, delegation, or other MCP item. A
wrong first action is a failed gate, not a recoverable
same-Turn warning, because the host does not expose trustworthy denied-versus-
executed provenance for every item.

The evidence report must also include control cases for whole-Plugin
attachment, Hook disabled/untrusted behavior, a foreign same-name Skill, and
the combined Recall-plus-Candidate request. Passing automated tests without
passing the real first-Turn and later-Turn Desktop cases does not complete
Task 8.

## 9. Alternatives rejected for this slice

- **Whole-Plugin attachment as structured authority:** contradicted by the
  observed Desktop read and not guaranteed by the official App Server schema.
- **Synchronous app-server search in every first sensitive `PreToolUse`:** adds
  work to unselected tasks, violates the zero-call contract, and cannot satisfy
  the current Hook latency budget reliably.
- **Parse the textual Plugin marker in `UserPromptSubmit`:** preserves the
  desired whole-Plugin UX but cannot prove picker provenance; it remains an
  explicit future trust-boundary choice if Gate 0 fails.
- **Add an Enable Recall button:** adds a second user action and still depends
  on model-routed UI/tool behavior.
- **Treat a renamed `zdecision` directory as the primary Plugin Skill:** the
  Plugin manifest defines no primary-Skill routing guarantee.
- **Build a broad activation verdict/state framework before Gate 0:** creates
  recovery and consistency machinery around an input the host may not expose.
