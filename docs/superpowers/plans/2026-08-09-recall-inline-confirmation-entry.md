# Recall Inline Confirmation Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unusable native MCP Elicitation entry with a trusted
inline MCP App card whose app-only click is the sole authority to start Recall
for the current Codex task.

**Architecture:** The trusted `PreToolUse` Hook creates a private pending
activation attempt bound to the current Session, Turn, enabled repository, and
installed Plugin. The model-visible render tool freezes the exact card digest
and returns one inline card; the card alone can call the app-visible decision
tool. `enable` records Session consent with no task intent or injected set, and
the card requests a bounded follow-up Turn where the existing gate derives the
`RecallIntent`; `decline`, dismissal, expiry, and failures create no Recall
Session.

**Tech Stack:** Python 3.14, SQLite, MCP/FastMCP, MCP Apps JSON-RPC bridge,
static HTML/CSS/JavaScript, `unittest`.

## Global Constraints

- Work directly on the existing `main` checkout; the user explicitly rejected
  a separate worktree for this V1 repository.
- Do not touch or stage the user-owned untracked files
  `docs/superpowers/acceptance/2026-08-06-recall-host-gate.md` and
  `tests/integration/test_recall_host_gate.py`.
- Plugin or Skill selection renders the card but is never Recall authority.
- Only an app-only `enable` action from the trusted card authorizes Recall.
- Before `enable`, no `recall_sessions` row exists and ordinary development is
  not permanently blocked.
- Only a registered and enabled Git repository may receive an activation
  attempt.
- The card appears inside the Codex conversation, not in Central Web or the
  default browser.
- The card has exactly **启用本任务决策召回** and **暂不启用** actions.
- Card load, remount, restoration, polling, timeout, and retry must never call
  `enable` automatically.
- Raw Prompt, PRD, transcript, source, diff, tool output, credentials, Decision
  text, and absolute Plugin path never enter the attempt receipt, card payload,
  logs, Central traffic, or acceptance report.
- Do not rerun native Elicitation Gate E0, add another feasibility Gate, change
  hybrid retrieval/reranking, redesign Central, or broaden Candidate behavior.
- A production provider returning `blocked` remains a truthful blocked Recall
  outcome; this entry slice must not claim formal Decisions were applied.

---

### Task 1: Durable user-confirmation attempt and consent Session

**Files:**

- Modify: `src/zdecision/agent/recall_host_state.py`
- Modify: `src/zdecision/agent/hooks.py`
- Modify: `src/zdecision/agent/recall_mcp.py`
- Test: `tests/test_recall_host_state.py`
- Test: `tests/test_recall_hook_gate.py`
- Test: `tests/test_mcp_recall_host_gate.py`

**Interfaces:**

- Consumes: enabled-repository resolution and Hook-owned Session/Turn/CWD
  coordinates. It does not consume or persist `RecallIntent`.
- Produces: `RecallActivationAttempt`,
  `RecallHostStore.create_activation_attempt(...)`,
  `RecallHostStore.attach_activation_card(...)`,
  `RecallHostStore.decide_activation_attempt(...)`,
  `RecallHostStore.retire_activation_attempts(...)`,
  `RecallMcpTools.show_recall_confirmation(...)`, and
  `RecallMcpTools.decide_recall_confirmation(...)`.

- [ ] **Step 1: Write failing durable-state tests**

Add tests that exercise this public shape before production code exists:

```python
attempt = store.create_activation_attempt(
    session_id="session-1",
    turn_id="turn-1",
    cwd="/tmp/recall",
    repository_id="repo_" + "1" * 32,
    repository_display_name="recall",
    attempt_id="activation_" + "2" * 32,
    now=NOW,
    expires_at=NOW + timedelta(minutes=15),
    plugin_root=None,
)
self.assertEqual("pending_confirmation", attempt.state)
self.assertIsNone(store.get_session("session-1"))

attached = store.attach_activation_card(
    attempt.attempt_id,
    ui_digest="a" * 64,
)
self.assertEqual("a" * 64, attached.ui_digest)

declined = store.decide_activation_attempt(
    attempt.attempt_id, action="decline", now=NOW
)
self.assertEqual("declined", declined.state)
self.assertIsNone(store.get_session("session-1"))
```

Cover one current attempt per Session/Turn, exact replay, conflicting card
digest, wrong repository/CWD/bundle, expiry, `SessionEnd` retirement, restart
recovery, and privacy sentinels. Add an `enable` case proving one transaction
commits the attempt and creates `state="active"`, `intent_epoch=0`,
`active_intent_digest=None`, and `active_set_digest=None`. A conflicting replay
must not replace the first choice.

- [ ] **Step 2: Run the state tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_host_state -v
```

Expected: FAIL because `RecallActivationAttempt` and the five attempt methods
do not exist and the old `bind_activation()` still creates an active Session.

- [ ] **Step 3: Implement the minimal durable attempt schema and transactions**

Add a `recall_activation_attempts` table with one row per trusted attempt and
an exact state check over:

```text
pending_confirmation
declined
cancelled
failed
committed
```

Persist only bounded trusted coordinates, repository display name, timestamps,
bundle digest, UI digest, and result digest. Use `BEGIN IMMEDIATE` for every
transition. `attach_activation_card` accepts one UI digest once. `enable` must
atomically commit the attempt and create one active consent Session with no
Intent Epoch result or active set. `decline` and retirement never create a
Session. A retry returns the frozen terminal row and a conflicting replay
raises `RecallGateConflict`.

- [ ] **Step 4: Write failing Hook binding tests**

Replace activation-tool fixtures with
`mcp__zdecision_local__show_zdecision_recall_confirmation` and assert:

```python
response = handle_pre_tool_hook(
    trusted_render_event,
    database=database,
    clock=lambda: NOW,
    repository_resolver=resolver,
    recall_store=store,
    activation_attempt_id_factory=lambda: "activation_" + "2" * 32,
)
self.assertEqual("allow", permission_decision(response))
self.assertEqual(
    {"activation_attempt_id": "activation_" + "2" * 32},
    updated_input(response),
)
self.assertIsNone(store.get_session(SESSION_ID))
```

Also assert unregistered/disabled repositories, subagents, unobserved Turns,
relative CWDs, invalid Plugin roots, and model-authored attempt IDs are denied;
pending/declined/cancelled attempts do not gate ordinary tools; only a
committed active consent Session does.

- [ ] **Step 5: Run the Hook tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_hook_gate -v
```

Expected: FAIL because the Hook still binds
`activate_zdecision_recall` through `bind_activation()`.

- [ ] **Step 6: Replace the premature activation binding**

Rename the Hook constant to the render-tool name. In the exact render-tool
branch, resolve the repository snapshot, derive the bounded display name from
the verified worktree root basename, create the durable pending attempt, and
replace tool input with only `activation_attempt_id`. Keep Candidate control
binding independent. On `SessionEnd`, call
`retire_activation_attempts(session_id, now)` before or with the existing
dormant transition.

- [ ] **Step 7: Write failing Recall MCP domain tests**

Add domain tests that call:

```python
rendered = tools.show_recall_confirmation(
    activation_attempt_id=ATTEMPT_ID,
    ui_digest="a" * 64,
)
declined = tools.decide_recall_confirmation(
    activation_attempt_id=ATTEMPT_ID,
    action="decline",
    current_ui_digest="a" * 64,
)
```

Assert render returns only bounded state plus `_meta` attempt/repository data;
neither render nor decision accepts an intent; enable creates consent but does
not call the provider; replay does not transition twice; conflicting action,
UI digest, CWD, repository, timeout, or SQLite failure never authorizes. Remove
activation-path expectations for App Server selected-Skill evidence; retain
the existing later-Turn gate behavior unchanged.

- [ ] **Step 8: Run the Recall MCP tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_mcp_recall_host_gate -v
```

Expected: FAIL because the new render/decision methods do not exist and the old
activation path still creates state before a user click.

- [ ] **Step 9: Implement the app-only decision and idempotent receipt**

Implement the two domain methods. Render freezes only the current UI digest.
Decision validates that digest and the exact attempt. `decline` commits
directly. `enable` commits consent and the empty Session state in one
transaction; it performs no routing, retrieval, or provider call. Delete the
obsolete model-visible activation path and its App Server selection proof; do
not weaken the ordinary later-Turn gate, which derives the first
post-confirmation `RecallIntent`.

- [ ] **Step 10: Run Task 1 GREEN and commit**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_host_state \
  tests.test_recall_hook_gate \
  tests.test_mcp_recall_host_gate -v
```

Expected: all three modules pass with no hang.

Commit only Task 1 files:

```bash
git add src/zdecision/agent/recall_host_state.py \
  src/zdecision/agent/hooks.py \
  src/zdecision/agent/recall_mcp.py \
  tests/test_recall_host_state.py \
  tests/test_recall_hook_gate.py \
  tests/test_mcp_recall_host_gate.py
git commit -m "feat: add trusted Recall confirmation attempts"
```

### Task 2: Inline card, app-only action, Skill routing, and direct acceptance

**Files:**

- Modify: `src/zdecision/agent/mcp_server.py`
- Create: `src/zdecision/agent/static/recall-confirmation-v1.html`
- Create: `tests/test_mcp_recall_confirmation.py`
- Modify: `tests/test_plugin_contract.py`
- Modify: `tests/test_recall_skill_contract.py`
- Modify: `plugins/zdecision/.codex-plugin/plugin.json`
- Modify: `plugins/zdecision/hooks/hooks.json`
- Modify: `plugins/zdecision/skills/zdecision/SKILL.md`
- Modify: `plugins/zdecision/skills/zdecision/agents/openai.yaml`
- Create: `plugins/zdecision/skills/candidate-refresh/SKILL.md`
- Create: `plugins/zdecision/skills/candidate-refresh/agents/openai.yaml`
- Delete: `plugins/zdecision/skills/decision-recall/SKILL.md`
- Delete: `plugins/zdecision/skills/decision-recall/agents/openai.yaml`
- Modify: `AGENTS.md`
- Modify: `docs/architecture.md`
- Create after real acceptance:
  `docs/superpowers/acceptance/2026-08-09-recall-inline-confirmation.md`

**Interfaces:**

- Consumes: Task 1's pending-attempt and decision methods, FastMCP tool/resource
  metadata, and the existing Candidate card's MCP Apps request helper pattern.
- Produces: `RECALL_CONFIRMATION_URI`, one model/app-visible render tool, one
  app-only decision tool, an explicit-only Recall Skill at `skills/zdecision`,
  and an independently implicit Candidate Skill at `skills/candidate-refresh`.

- [ ] **Step 1: Write failing MCP contract tests**

Create `tests/test_mcp_recall_confirmation.py` and assert:

```python
resources = {str(item.uri): item for item in await server.list_resources()}
tools = {item.name: item for item in await server.list_tools()}

self.assertIn("ui://zdecision/recall-confirmation-v1.html", resources)
self.assertEqual(
    ["model", "app"],
    tools["show_zdecision_recall_confirmation"].meta["ui"]["visibility"],
)
self.assertEqual(
    ["app"],
    tools["decide_zdecision_recall"].meta["ui"]["visibility"],
)
self.assertNotIn("activate_zdecision_recall", tools)
```

Assert the render schema contains only `activation_attempt_id`;
the decision schema contains only `activation_attempt_id` and
`action=enable|decline`; extra Session/CWD/repository/confirmed fields are
rejected. Verify `_meta` carries the opaque attempt and repository display name
while model-visible content does not.

- [ ] **Step 2: Write failing static-card protocol tests**

Read the HTML as text and exercise it with the existing Node MCP Apps harness.
Prove both Chinese buttons render, initialization makes no `tools/call`, and
only a synthetic click emits:

```json
{
  "method": "tools/call",
  "params": {
    "name": "decide_zdecision_recall",
    "arguments": {
      "activation_attempt_id": "activation_...",
      "action": "enable"
    }
  }
}
```

Remount, duplicate tool-result notifications, timeout, and retry must emit no
automatic `enable`. After a committed enable result, permit exactly one bounded
`ui/message`. If the host does not continue, render a bounded instruction that
the next native user message will run Recall. Decline emits no enable or Recall
continuation.

- [ ] **Step 3: Run the new module and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_mcp_recall_confirmation -v
```

Expected: FAIL because the resource, tools, and HTML do not exist.

- [ ] **Step 4: Implement the resource and two MCP tools**

Register `ui://zdecision/recall-confirmation-v1.html` with
`text/html;profile=mcp-app`, empty CSP domains, and a bordered inline card.
The render tool calls Task 1's render method with the SHA-256 of the exact HTML
bytes. The decision tool recomputes the same digest and calls Task 1's decision
method. Use `meta={"ui": {"visibility": ["app"]}}` for the decision action and
the existing closed-world argument-model helper for both tools.

Build a compact ZStack-aligned card, not a new dashboard: repository name,
current-task lifetime, one primary enable button, one quiet decline button,
and bounded pending/active/declined/blocked text. Reuse the Candidate card's
MCP Apps JSON-RPC framing, origin checks, pending-request map, and timeout
cleanup; do not copy its Candidate state machine.

- [ ] **Step 5: Write failing Plugin and Skill topology tests**

Update contract tests to require:

```text
skills/zdecision            -> explicit-only Recall entry
skills/candidate-refresh    -> Candidate status/refresh instructions
skills/decision-recall      -> absent
```

The Recall Skill's first workflow tool is
`show_zdecision_recall_confirmation`; it says selection only renders and the
card click authorizes. Candidate instructions retain the exact
**更新候选决策** gates and never render Recall confirmation. The Hook matcher
names the new render tool and no longer names `activate_zdecision_recall`.

- [ ] **Step 6: Run Plugin contracts and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_plugin_contract tests.test_recall_skill_contract -v
```

Expected: FAIL because the current `zdecision` Skill is Candidate-only and the
old `decision-recall` Skill and activation matcher still exist.

- [ ] **Step 7: Implement the Skill split and documentation alignment**

Move the existing Candidate instructions and implicit metadata byte-for-byte
in meaning to `skills/candidate-refresh`. Make `skills/zdecision` the
explicit-only Recall entry with the approved card-first ordering. Remove the
old competing `decision-recall` entry. Update manifest copy, Hook matcher,
`AGENTS.md`, and the Recall sentence in `docs/architecture.md`; do not change
Candidate authority or claim that full hybrid retrieval is complete.

- [ ] **Step 8: Run focused GREEN and privacy checks**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_mcp_recall_confirmation \
  tests.test_recall_host_state \
  tests.test_recall_hook_gate \
  tests.test_mcp_recall_host_gate \
  tests.test_plugin_contract \
  tests.test_recall_skill_contract \
  tests.test_mcp_inline_refresh -v
```

Then scan only changed production/card bytes for the test privacy sentinels and
run `git diff --check`. Expected: focused modules pass, the privacy scan is
empty, and the diff has no whitespace errors.

- [ ] **Step 9: Commit the automated implementation**

Stage the explicit Task 2 paths only and verify the two protected untracked
files remain untracked:

```bash
git status --short
git commit -m "feat: confirm Recall from an inline card"
```

- [ ] **Step 10: Run the complete suite once**

Run exactly once:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected baseline: at least 893 tests pass, with only the four previously
recorded skips unless the new tests increase the count. Record any existing
Starlette or SQLite warnings without turning them into new scope.

- [ ] **Step 11: Refresh the installed local Plugin and run one real Desktop acceptance**

After automated GREEN, refresh `zdecision@zdecision-local`, restart Codex once,
and use a newly created or existing enabled-repository task. Select ZDecision,
send an ordinary development request, and verify the card appears before
substantive work. Click **启用本任务决策召回** once and inspect only bounded
receipts to prove the exact current Session/repository attempt transitioned.
Record whether `ui/message` starts a follow-up Turn. If it does not, send one
ordinary native user continuation and verify the existing Turn gate is
requested; do not store the prior Prompt or fabricate a Turn to mask the host
behavior. The production readiness provider may truthfully end in `blocked`
until the retrieval packet is ready.

Also verify one **暂不启用** case creates no Recall Session. Do not rerun native
Elicitation, use the central Web page, or manually call the app-only tool.

- [ ] **Step 12: Record bounded evidence and stop**

Write the acceptance report with Codex/Desktop version, commit, card render,
button outcome, bounded attempt/Session states, test commands/counts, and
privacy scan result. Include no Session ID, path, Prompt, Decision content,
credentials, tool payload, or raw database row. If the card fails to render,
the action appears model-visible, or the click is not bound to the current
enabled repository task, record FAIL and stop without another workaround.
