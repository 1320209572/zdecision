# ZDecision Recall Plugin Entry Boundary Design

Status: Proposed — conversational direction approved; written specification
awaiting user review.

Date: 2026-08-08

## 1. Problem

The first real Codex Desktop Host Gate failed after the user attached the
ZDecision Plugin on the first Turn. Codex loaded the broad `zdecision` Skill,
produced development commentary, and completed a file mutation without calling
`activate_zdecision_recall`. The local Recall store consequently contained no
Session, activation binding, or Turn gate.

The Plugin currently exposes two competing workflow entries:

- `zdecision`, whose broad description covers Candidate refresh and completed
  code-development work; and
- `decision-recall`, whose instructions require Recall activation.

At the same time, native-selection validation accepts only the exact installed
`decision-recall/SKILL.md` path. This makes whole-Plugin selection and the
trusted Recall entry disagree about which Skill represents user authority.

This is an entry-boundary defect. Existing Recall intent, state, app-server
identity, Turn-gate, restoration, Fork, and Capture-provenance domains remain
valid and are not redesigned here.

## 2. Product contract

Attaching ZDecision to a native user Turn means: enable Decision Recall for
that exact Codex task. This applies on the first or any later Turn and remains
effective for the Session until bypass, close, or another defined lifecycle
transition.

Not attaching ZDecision means Recall remains disabled. Installing the Plugin,
implicit Skill consideration, repository enablement, ordinary Prompt text,
assistant initiative, delegated messages, and lifecycle observations do not
activate Recall.

Candidate refresh stays independent:

- the exact native request **更新候选决策** may render the existing refresh
  controls after its repository gate; and
- a completed and verified code-development boundary may render those controls
  under the existing Candidate-refresh contract.

Neither action activates Recall or authorizes Capture. Combining Plugin
attachment with a Candidate-refresh request first completes Recall activation,
then may render the read-only refresh controls; the later scope click remains
the only Capture authorization.

## 3. Plugin structure

Keep one installable ZDecision Plugin with two focused workflows, but remove
the competing broad entry:

```text
plugins/zdecision/
├── skills/
│   ├── zdecision/              # primary Plugin-selection Recall entry
│   │   ├── SKILL.md
│   │   └── references/recall.md
│   └── candidate-refresh/      # narrow Candidate-refresh workflow
│       └── SKILL.md
├── hooks/hooks.json
└── .mcp.json
```

The primary `zdecision` Skill is Recall-only. Its first workflow action after a
native Plugin selection is `activate_zdecision_recall`; it permits only a
short activation-status message before that call and forbids substantive
development, shell execution, file mutation, delegation, or another MCP
action first.

The existing Candidate-refresh instructions move to `candidate-refresh`. Its
description is limited to the exact refresh request and the completed,
verified code-development boundary. It must explicitly exclude Plugin
selection as Recall authority and must not call a Recall tool.

`decision-recall` stops being a separately discoverable Skill. Its detailed
instructions become the primary Skill's reference so Plugin selection cannot
route to a competing entry.

## 4. Native-selection proof

Prompt matching remains forbidden. The activation MCP verifies the exact
in-progress app-server Turn and accepts only a structured user-input selection
belonging to the trusted installed Plugin.

The accepted forms are deliberately narrow:

- `type = skill`: `name = zdecision` and the normalized path equals the
  installed primary `skills/zdecision/SKILL.md`; or
- `type = mention`: `name = zdecision` and the normalized path is either that
  exact primary Skill path or the exact installed Plugin root.

For either form, the installed root must be the Hook-supplied `PLUGIN_ROOT`,
the manifest and primary Recall Skill bytes must match the frozen bundle
digest, and app-server Thread ID, active Turn ID, and CWD must equal the
Hook-owned binding. Any other path, including an arbitrary descendant of the
Plugin root, is rejected. If the real host emits another structured path shape,
the focused live protocol test stops for an explicit contract update rather
than broadening the path check heuristically.

Implicit Skill invocation does not create a qualifying structured user-input
selection. Plain text such as `@zdecision`, a copied plugin URI, quoted text,
tool output, a delegated message, or a model-authored activation call is not
authority.

The first implementation test records only the live structured selection's
`type`, bounded `name`, and normalized path category. It never records Prompt
or transcript text. Any host shape outside the two forms above stops the fix
for a focused contract update rather than widening trust heuristically.

## 5. Host-enforced mutation guard

Skill instructions improve routing but are not an enforcement boundary. The
Hook adds a first-sensitive-tool backstop for enabled repositories.

When `PreToolUse` observes a command-executing or code-mutating tool and the
Session has no Recall state, it reads the exact active app-server Turn once:

1. no trusted ZDecision selection: retain the current fail-open behavior and
   create no Recall row;
2. trusted selection and activation tool: bind exact Session, Turn, CWD, Plugin
   bundle, and an opaque activation ID, then enter `activating`;
3. trusted selection and any other sensitive tool: persist a bounded pending
   selection observation and deny the tool with
   `recall_activation_required`.

The pending observation contains only Session ID, Turn ID, normalized CWD,
selection kind, installed-bundle digest, and observation time. It contains no
Prompt, URI text, transcript, source, tool input, or tool output. Repeated
checks for the same Turn are idempotent.

The guard covers the Hook-supported Bash, unified exec, `apply_patch`, MCP,
Agent, and other local function-tool paths already named in `hooks.json`.
Unsupported hosted or specialized paths are not claimed as covered. Plain
assistant text cannot be deterministically intercepted by this Hook and
therefore remains a real Desktop acceptance condition.

## 6. Activation outcomes

Replace the current boolean active-Turn barrier result with bounded verdicts:

- `selection_absent`: no qualifying structured current-Turn selection;
- `selection_invalid`: a selection exists but fails installed-entry or bundle
  validation;
- `activation_required`: a selected Turn attempted another sensitive tool
  first;
- `barrier_violated`: substantive agent text, command execution, or file change
  preceded the activation item;
- `host_gate_unavailable`: exact active-Turn evidence could not be read or
  validated; and
- `proven`: native selection and first-answer ordering both pass.

`selection_absent` removes the provisional activation binding and returns the
Session to disabled, so an erroneous model activation does not poison an
ordinary Candidate-refresh task. `selection_invalid`, `barrier_violated`, and
a host failure after a trusted pending selection move the Session to blocked
and keep sensitive tools denied. Only `proven` may invoke the Recall provider
and commit the activation receipt.

Existing wrong-Turn, cross-Session, CWD, internal-thread, Fork, replay, and
bundle-tamper failures remain fail-closed.

## 7. Ordered flow

The required first-Turn sequence is:

```text
native user selects ZDecision
  -> UserPromptSubmit lifecycle observation
  -> primary zdecision Recall Skill
  -> activate_zdecision_recall
  -> PreToolUse freezes host binding and activating state
  -> MCP reads exact active Turn and proves structured selection/order
  -> provider result and activation receipt commit
  -> substantive development answer or mutation
```

Later active Turns retain the existing `UserPromptSubmit` Turn-gate flow.
Candidate refresh, Central synchronization, retrieval, ranking, formal
Decision application, and Capture provenance are unchanged.

## 8. Verification and stop rule

Focused automated tests must prove:

- whole-Plugin `mention` and explicit primary-Skill selection activate Recall;
- implicit Skill use and ordinary text do not activate it;
- Candidate refresh without Plugin selection creates no Recall state;
- a selected Turn's first non-activation mutation is denied;
- activation then committed gating permits the exact later mutation;
- absent selection rolls back provisional state, while an ordering violation
  remains blocked;
- installed path, bundle digest, Session, Turn, CWD, replay, internal Thread,
  and Fork mismatches fail closed; and
- no persisted value contains Prompt, transcript, plugin URI text, source, or
  tool payload sentinels.

After focused and full automated tests pass, reinstall the bumped local Plugin,
restart Codex only when required, and rerun Task 8 from Case 1. Gate 1 passes
only if all seven real Desktop cases pass. In particular, the activation item
and receipt must precede the first substantive `agentMessage`, command, or file
change. If the current host cannot expose a qualifying structured selection or
cannot maintain that ordering, retain the failed acceptance result and do not
replace this interaction with Prompt parsing or claim Gate 2 readiness.

## 9. Alternatives rejected

- **Only edit Skill wording:** faster, but still permits a skipped activation
  to mutate before any Recall state exists.
- **Extra “Enable Recall” button:** adds a user step and still needs a
  model-called MCP UI tool before the component can appear; Codex does not
  expose a Plugin-owned button inside its native attachment picker.
- **Parse the textual plugin token in `UserPromptSubmit`:** cannot distinguish
  native UI selection from copied, delegated, or programmatic text and violates
  the approved native-selection trust boundary.
- **Split Recall and Candidate refresh into two separately installed Plugins:**
  avoids Skill competition but adds installation and product-surface overhead
  without first testing the narrower single-Plugin repair.
