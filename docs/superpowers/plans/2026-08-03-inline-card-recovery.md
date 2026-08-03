# Inline Candidate Card Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an authorized inline Candidate refresh survive a lost initial
Central submission, card remount, and cold Plugin Hook startup without changing
the selected Capture scope or creating a second request.

**Architecture:** Keep the existing durable Control Binding as the authority.
The mutating start tool replays a pending submission with the same
`client_action_id`; the read-only status tool never creates a request. The
render tool returns private remount metadata so the widget locks the persisted
scope and resumes the correct app-only tool. The Hook CLI cold path avoids
loading MCP, HTTP, service, and worker modules that PreToolUse does not need.

**Tech Stack:** Python 3.14, SQLite, MCP Apps HTML/JavaScript, stdlib
`unittest`, Node.js widget harness.

## Global Constraints

- The user's first click remains the only Capture authorization.
- A selected Control Binding can replay only its persisted scope and action ID.
- `get_zdecision_candidate_refresh` remains read-only and never submits.
- Raw Session, Turn, Prompt, source, diff, and control identifiers never reach
  the Central service.
- Candidate extraction and reconciliation are outside this fix.

---

### Task 1: Recover the durable card state

**Files:**
- Modify: `tests/test_mcp_inline_refresh.py`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `src/zdecision/agent/static/update-candidates-v1.html`

**Interfaces:**
- Consumes: existing `ControlBinding.chosen_scope`, `client_action_id`, and
  `central_request_id`.
- Produces: transient safe state `submitting`; private render metadata
  `zdecision/chosen_scope` and `zdecision/request_attached`.

- [ ] **Step 1: Write failing domain tests**

Cover a lost create response returning `submitting`, then replay the same scope
through `start_zdecision_candidate_refresh` and assert one Central request is
adopted with the original action ID. Assert the read-only status tool does not
submit while no request is attached.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_lost_response_stays_submitting_until_same_action_is_adopted \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_pending_submission_status_tool_remains_read_only
```

Expected: failures because the current start path returns `unavailable` and
the current status path cannot represent a pending submission.

- [ ] **Step 3: Write a failing remount widget test**

Mount a render result whose private metadata contains a persisted scope and no
attached request. Assert both buttons remain disabled and the next app call
replays `start_zdecision_candidate_refresh` with that exact scope. Also cover
an attached request remount using only the read-only status tool.

- [ ] **Step 4: Run the widget test and verify RED**

Run the named widget regression with `unittest`; expect failure because the
current widget discards selected binding state on remount.

- [ ] **Step 5: Implement the minimal recovery state machine**

Refactor the existing create-and-attach block into a private method used only
by `start_zdecision_candidate_refresh`. Map transient create errors to
`submitting`. Extend render `_meta` for selected bindings. Teach the widget to
store the persisted scope, lock both actions, and dispatch start replay only
while `safe_state === "submitting"`; all later polling remains status-only.

- [ ] **Step 6: Run all inline MCP tests and commit**

```bash
.venv/bin/python -m unittest tests.test_mcp_inline_refresh
git add src/zdecision/agent/mcp_server.py \
  src/zdecision/agent/static/update-candidates-v1.html \
  tests/test_mcp_inline_refresh.py
git commit -m "fix: recover pending inline capture submissions"
```

### Task 2: Keep the binding Hook inside its host deadline

**Files:**
- Modify: `tests/test_control_binding_hook.py`
- Modify: `src/zdecision/agent/cli.py`
- Modify: `src/zdecision/agent/hooks.py`

**Interfaces:**
- Consumes: unchanged `zdecision-agent hook` JSON stdin/stdout contract.
- Produces: a PreToolUse path that does not import MCP, HTTP, service, or worker
  runtime modules.

- [ ] **Step 1: Write a failing isolated CLI test**

Run a real PreToolUse subprocess with a `sitecustomize` import guard that
rejects `httpx`, `mcp`, and `zdecision.agent.worker`. Assert it still creates a
trusted Control Binding and returns the rewritten `control_id`.

- [ ] **Step 2: Run the isolated test and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_control_binding_hook.ControlBindingHookTests.test_cli_pre_tool_hook_does_not_load_unrelated_runtime_stacks
```

Expected: failure because `zdecision.agent.cli` eagerly imports HTTP/MCP and
`zdecision.agent.hooks` eagerly imports the worker.

- [ ] **Step 3: Lazily import command-specific dependencies**

Move MCP, HTTP, service, status, test-repository, and worker imports into the
branches that use them. Preserve every CLI command and Hook output contract.

- [ ] **Step 4: Verify Hook behavior and cold subprocess latency**

```bash
.venv/bin/python -m unittest \
  tests.test_control_binding_hook tests.test_hook_latency tests.test_plugin_contract
```

Run five real `zdecision-agent hook` subprocess samples and confirm each stays
inside the configured three-second deadline.

- [ ] **Step 5: Run full verification and commit**

```bash
.venv/bin/python -m unittest discover -s tests
git diff --check
git add src/zdecision/agent/cli.py src/zdecision/agent/hooks.py \
  tests/test_control_binding_hook.py
git commit -m "fix: keep control binding hook lightweight"
```

After both tasks, restart Codex once and repeat the real inline acceptance from
a newly rendered card. Stop after this acceptance; the separately documented
parent-directory repository-binding gap remains deferred.
