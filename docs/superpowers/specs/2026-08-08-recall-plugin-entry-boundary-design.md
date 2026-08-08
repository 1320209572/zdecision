# ZDecision Recall Explicit-Skill Entry Boundary Design

Status: Proposed — revised after protocol self-review; awaiting written user
review before implementation planning.

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

- section 6.1 of
  `2026-08-06-session-opt-in-intelligent-decision-recall-design.md`;
- Tasks 5 and 6, and the corresponding real Desktop cases, in
  `2026-08-06-recall-host-gate.md`; and
- the failed Task 8 acceptance path that treated whole-Plugin attachment as a
  structured selection.

Until Gate 0 below passes, references in those documents to proving a
whole-Plugin `mention`, a Plugin-root selection path, or a deterministic
pre-answer selection backstop are suspended. This document does not amend
Recall Intent, retrieval, freshness, active-set, context restoration, Fork,
Capture provenance, Candidate Review, publication, Registry, or Central
contracts.

The authority this design can test is deliberately precise: a structured
explicit-Skill input inside the trusted Codex Desktop task. It is not a
cryptographic proof that a physical person clicked the picker; the current
official host protocol exposes no such gesture signature.

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
- assistant initiative, delegated messages, tool output, recalled Decision
  text, or lifecycle events.

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

No file is moved or renamed merely to make the directory names symmetrical.
The Plugin manifest has no assumed primary-Skill behavior.

## 5. Gate 0: prove the actual Desktop protocol first

Gate 0 is a read-only feasibility gate and precedes production changes.

### 5.1 Preconditions

- the current local Plugin bundle is installed;
- the bundled **ZDecision Recall** Skill is visible in the native picker;
- the Plugin Hook is enabled and its current definition is trusted;
- the test task is in a registered and enabled Git repository; and
- the probe uses the supported host App Server route while the selected Turn is
  still `inProgress`.

The probe and later activation must use the host-provided route. They must not
launch a second controlled app-server process to inspect another live Desktop
task.

Hook-disabled, untrusted, or managed-policy-excluded runs cannot claim a
fail-closed Recall boundary and fail this gate explicitly.

### 5.2 Probe cases

Run exactly two bounded cases:

1. a control Turn without a ZDecision selection; and
2. a native Turn that explicitly selects the bundled **ZDecision Recall**
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

### 5.3 Pass criteria

Gate 0 passes only when all of the following are true:

- the control Turn contains no qualifying structured selection;
- the selected Turn contains exactly one qualifying `type = skill` input;
- its bounded name has the stable observed Recall Skill value;
- its normalized absolute path equals the installed
  `decision-recall/SKILL.md` path under `PLUGIN_ROOT`;
- the exact in-progress Turn is readable before substantive assistant output;
  and
- repeated reads of that Turn return the same bounded selection identity.

The observed bounded name is frozen into the follow-up contract; it is not
guessed in advance. `type = mention`, a Plugin-root path, a canonical textual
marker, and post-hoc rollout parsing cannot satisfy this gate.

### 5.4 Stop rule

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
4. It performs the existing bounded provider operation.
5. It atomically commits the active Recall Session and activation receipt
   before returning a result.

The Hook does not mark the Session `active` before steps 2 through 4 succeed.
An `activating` binding denies later sensitive tools for its short lease, is
idempotently reclaimable by the same activation operation, and never supplies
Decision context by itself.

### 6.3 Failure and recovery

- No qualifying structured Skill or an unavailable active-Turn read discards
  the provisional binding, returns bounded `native_selection_unproven`, and
  creates no active Recall Session. The Skill must stop affected development
  and tell the user Recall was not activated.
- A qualifying installed Skill followed by bundle tamper, wrong
  Thread/Turn/CWD, replay, or an activation-order violation moves the bound
  Session to `blocked`; supported sensitive tools remain denied.
- A foreign Skill with the same display name but a different installed path is
  treated as non-qualifying, not as a reason to block an unrelated task.
- A crash after `activating` but before the receipt leaves no `active` state.
  Same-operation retry may resume idempotently; after lease expiry a new native
  explicit-Skill Turn may retry.
- Hook-disabled or Hook-untrusted execution is outside the enforcement
  boundary and must be reported as unavailable, never described as
  fail-closed.

There is intentionally no deterministic Hook backstop before the activation
MCP call: the Hook has no proven, low-latency selection signal at that point.
Therefore "activation is the first visible action" remains a real Desktop
acceptance gate. If Codex again emits development text or invokes another tool
first, Task 8 fails and implementation stops instead of adding Prompt parsing
or a synchronous Hook-side app-server probe.

## 7. Installed-bundle integrity

The Recall activation digest covers the normalized bytes and installed paths
of every behavior-bearing Recall entry:

- `.codex-plugin/plugin.json`;
- `.mcp.json`;
- `hooks/hooks.json`;
- `skills/decision-recall/SKILL.md`;
- `skills/decision-recall/agents/openai.yaml`; and
- each exact Recall reference named by that Skill, if references are added.

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
- `activating -> active` occurs only with provider success and an atomically
  committed receipt;
- absent evidence discards provisional state, proven tamper/order violations
  block, and crash/retry never exposes premature active state;
- Candidate refresh without Recall remains usable;
- explicit Recall plus Candidate refresh activates first and renders the
  existing controls only afterward;
- no persisted or returned value contains Prompt, transcript, Plugin URI,
  source, diff, tool arguments, or tool-output sentinels; and
- the no-selection Hook path still meets its existing latency budget.

Real Desktop acceptance then reruns the existing seven Task 8 cases with the
explicit bundled Recall Skill. The activation item and receipt must precede
the first substantive `agentMessage`, command, file change, delegation, or
other MCP item. A wrong first action is a failed gate, not a recoverable
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
