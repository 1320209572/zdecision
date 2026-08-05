# Central Runtime Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the loopback central service without changing the frozen Candidate-refresh action identity, and replace unlimited 1.5-second Pending replays with one bounded recovery window per mounted card.

**Architecture:** Keep the existing local-first `pending` and central `client_action_id` idempotency contracts. Lock the executable Demo startup example to the central CLI's required Registry argument, repair only the owned live LaunchAgent, and add a widget-local retry scheduler with six exponential delays. Do not add a central service installer or change attached-request progress polling.

**Tech Stack:** Python 3.14, `unittest`, vanilla JavaScript, Node `vm` widget harness, macOS `launchd`, SQLite, FastMCP, HTTPX.

## Global Constraints

- Work directly on the existing `main` worktree; do not create a branch or worktree.
- Preserve `chosen_scope`, `client_action_id`, and the current `pending` binding.
- Retry delays are exactly `1500, 3000, 6000, 12000, 24000, 48000` milliseconds.
- One mount may have at most one Pending retry timer. After the sixth replay remains Pending, issue no more automatic start calls and show the existing generic unavailable copy.
- Remounting starts a fresh bounded window but reuses the persisted action identity.
- Do not change attached-request polling, central idempotency, extraction, Review, publication, or repository routing.
- Do not add a general central-service LaunchAgent API in this Demo repair.
- Run focused tests, one complete suite, and one live recovery acceptance. Do not start another broad review.

---

### Task 1: Lock the documented central startup contract

**Files:**
- Modify: `tests/test_skill_contract.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: the required CLI argument `--registry-repository-root <absolute Git root>`.
- Produces: one tested README startup block that cannot silently drift from the CLI.

- [ ] **Step 1: Write the failing README contract assertion**

Extend `test_readme_documents_page_trigger_and_installed_templates`:

```python
central_run = text.split("zdecision-central run", 1)[1].split(
    "zdecision-agent service run", 1
)[0]
self.assertIn(
    "--registry-repository-root /absolute/path/to/zdecision-checkout",
    central_run,
)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_skill_contract.ZDecisionSkillContractTests.test_readme_documents_page_trigger_and_installed_templates
```

Expected: FAIL because the current README command omits the Registry root.

- [ ] **Step 3: Add the missing argument to the README command**

The command must contain these arguments in order:

```text
--database /absolute/path/to/central.sqlite3
--config /absolute/path/to/new-config-directory/central.json
--registry-repository-root /absolute/path/to/zdecision-checkout
--host 127.0.0.1
--port 8765
```

- [ ] **Step 4: Verify GREEN and formatting**

Run:

```bash
.venv/bin/python -m unittest tests.test_skill_contract.ZDecisionSkillContractTests.test_readme_documents_page_trigger_and_installed_templates
git diff --check
```

Expected: PASS and no formatting output.

- [ ] **Step 5: Commit the startup contract**

```bash
git add README.md tests/test_skill_contract.py
git commit -m "fix: lock central registry startup argument"
```

---

### Task 2: Bound durable Pending replay in the inline card

**Files:**
- Modify: `tests/test_mcp_inline_refresh.py`
- Modify: `src/zdecision/agent/static/update-candidates-v1.html`

**Interfaces:**
- Consumes: `submission_state="pending"`, persisted `chosen_scope`, and `start_zdecision_candidate_refresh`.
- Produces: `schedulePendingRetry()` and `cancelPendingRetry()` with one exact six-delay budget per mount.

- [ ] **Step 1: Write a failing bounded-schedule widget test**

Drive an initial Pending result and all six replays:

```javascript
const delays = [1500, 3000, 6000, 12000, 24000, 48000];
for (const delay of delays) {
  check(widget.timers.length === 1, "pending scheduled more than one timer");
  const retry = widget.takeTimer(delay);
  check(retry, `missing pending retry at ${delay}ms`);
  retry();
  await flush();
  const replay = widget.latestToolCall("start_zdecision_candidate_refresh");
  await widget.respond(
    replay,
    state("submitting", "pending", "current_session"),
  );
}
check(
  widget.toolCalls("start_zdecision_candidate_refresh").length === 7,
  "pending replay exceeded or missed the six-attempt budget",
);
check(widget.timers.length === 0, "exhausted Pending scheduled another retry");
check(
  widget.elements.status.textContent === "暂时无法更新",
  "exhausted Pending did not show the generic unavailable state",
);
```

Extend the existing remount test: after its immediate replay returns Pending,
it must schedule `1500` again without changing the control or scope.

- [ ] **Step 2: Run both recovery tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_widget_bounds_same_mount_pending_retries tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_widget_real_remount_restores_pending_from_original_result
```

Expected: FAIL because the widget always schedules another 1500ms timer.

- [ ] **Step 3: Implement the per-mount scheduler**

Add the exact delay vector, an index, and one timer handle:

```javascript
const pendingRetryDelays = [1500, 3000, 6000, 12000, 24000, 48000];
let pendingRetryIndex = 0;
let pendingRetryTimer = null;

function cancelPendingRetry(resetBudget = false) {
  if (pendingRetryTimer !== null) {
    clearTimeout(pendingRetryTimer);
    pendingRetryTimer = null;
  }
  if (resetBudget) pendingRetryIndex = 0;
}

function schedulePendingRetry() {
  if (pendingRetryTimer !== null) return;
  if (pendingRetryIndex >= pendingRetryDelays.length) {
    safeState = "unavailable";
    status.textContent = labels.unavailable;
    return;
  }
  const delay = pendingRetryDelays[pendingRetryIndex++];
  pendingRetryTimer = setTimeout(() => {
    pendingRetryTimer = null;
    if (submissionState === "pending" && chosenScope !== null) {
      start(chosenScope, true);
    }
  }, delay);
}
```

Reset the budget only when `acceptInitialResult` binds another control. Replace
the unconditional 1500ms Pending retry in `applyBindingResult` with
`schedulePendingRetry()`. Cancel the Pending timer for every non-Pending state;
leave the existing attached polling branch unchanged.

- [ ] **Step 4: Verify the complete inline-card module**

Run:

```bash
.venv/bin/python -m unittest tests.test_mcp_inline_refresh
git diff --check
```

Expected: complete module PASS and clean formatting.

- [ ] **Step 5: Commit bounded recovery**

```bash
git add src/zdecision/agent/static/update-candidates-v1.html tests/test_mcp_inline_refresh.py
git commit -m "fix: bound pending candidate refresh retries"
```

---

### Task 3: Repair the live service and recover the frozen action

**Files:**
- Modify outside Git: `/Users/zhaohuiying/Library/LaunchAgents/com.zdecision.central.demo.plist`
- Read only: `/Users/zhaohuiying/Library/Application Support/ZDecision/agent/zdecision.sqlite3`
- Read only: `/Users/zhaohuiying/Library/Application Support/ZDecision/demo-central/central.sqlite3`

**Interfaces:**
- Consumes: the owned LaunchAgent, Registry checkout `/Users/zhaohuiying/Desktop/Zstack-repos/zdecision`, control `ctl_2927049c9ba8a2075e80c31e1f6ca132`, and action `codex_action_ccc3e593276f4012afe99c909713e7f4`.
- Produces: a healthy loopback central service and exactly one attached request for that action.

- [ ] **Step 1: Record the pre-repair invariant**

Read-only checks must show: port 8765 closed, owned LaunchAgent label, Pending
binding, null central request ID, and central request count 7. Never print the
Agent token.

- [ ] **Step 2: Patch only the owned LaunchAgent arguments**

Boot out the exact label, add these two arguments immediately after the config
path, and bootstrap the same plist:

```xml
<string>--registry-repository-root</string>
<string>/Users/zhaohuiying/Desktop/Zstack-repos/zdecision</string>
```

Do not change database, config, host, port, logs, `KeepAlive`, or throttle.

- [ ] **Step 3: Verify health before replay**

Require one loopback listener, HTTP 200 from `http://127.0.0.1:8765/`, a loaded
LaunchAgent with exit status 0, and no new parser failures in stderr.

- [ ] **Step 4: Refresh the installed Plugin and recover idempotently**

Refresh `zdecision@zdecision-local`, then allow the mounted or remounted card to
replay the same frozen action. Do not call the create endpoint with a new action
ID and do not edit either database.

- [ ] **Step 5: Prove exactly-once attachment**

Require:

```text
binding.submission_state = attached
binding.central_request_id = one non-null crq_... value
central request count = 8
request.client_action_id = codex_action_ccc3e593276f4012afe99c909713e7f4
request.repository_id = repo_8c2fb0e7f322b39a116f76a40058a08f
```

Re-read after one additional card poll and require the count remains 8.

- [ ] **Step 6: Run final verification once**

Run:

```bash
.venv/bin/python -m unittest tests.test_skill_contract tests.test_mcp_inline_refresh
.venv/bin/python -m unittest discover -s tests
git diff --check
git status --short --branch
```

Expected: focused and complete suites pass, formatting is clean, and no
uncommitted implementation files remain. Stop here; do not start another review
or push without explicit user authorization.
