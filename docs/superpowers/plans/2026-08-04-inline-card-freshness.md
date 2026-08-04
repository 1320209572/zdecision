# Inline Card Freshness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make current and expired ZDecision inline cards unambiguous and force Codex to load the corrected widget resource.

**Architecture:** Keep the existing Control Binding and Capture contracts unchanged. Interpret only the status tool's unbound `unavailable` envelope as an expired historical card, render a persistent freshness label in the widget, and change the host-visible MCP Apps resource URI from v1 to v2.

**Tech Stack:** Python 3.14, FastMCP, HTML/CSS/vanilla JavaScript, `unittest`, Node `vm` widget harnesses.

## Global Constraints

- Do not change Capture, Review, publication, repository eligibility, or Control Binding authorization.
- An unregistered, disabled, or unresolved repository remains disabled without an explanation.
- Historical cards never retry, create a replacement binding, or redirect an action.
- A current bound central failure continues to display `暂时无法更新`.
- Preserve the already implemented but uncommitted local-page anchor probe until real acceptance decides whether to keep it.
- Stop after one focused suite, one complete suite, and one real Codex Desktop acceptance; do not start another broad review.

---

### Task 1: Version and distinguish the inline card

**Files:**
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `src/zdecision/agent/static/update-candidates-v1.html`
- Modify: `tests/test_mcp_inline_refresh.py`

**Interfaces:**
- Consumes: `get_zdecision_candidate_refresh(control_id)` results. A missing/expired binding returns `safe_state: "unavailable"` without `submission_state` or `chosen_scope`; a current binding includes both persisted fields.
- Produces: MCP Apps resource URI `ui://zdecision/update-candidates-v2.html`; widget freshness labels `当前卡片` and `历史卡片`; expired-card copy `此更新卡已失效`.

- [ ] **Step 1: Write failing URI and freshness tests**

Change the test constant to the required URI:

```python
WIDGET_URI = "ui://zdecision/update-candidates-v2.html"
```

Extend the widget harness element maps with `card-state`, then add a scenario that proves all three state distinctions:

```javascript
const current = await mount();
const currentRestore = current.latestToolCall(
  "get_zdecision_candidate_refresh",
);
await current.respond(currentRestore, state("ready", "ready"));
check(current.elements["card-state"].textContent === "当前卡片");
check(!current.elements.current.disabled && !current.elements.all.disabled);

const historical = await mount();
const historicalRestore = historical.latestToolCall(
  "get_zdecision_candidate_refresh",
);
await historical.respond(historicalRestore, {
  content: [],
  structuredContent: {
    safe_state: "unavailable",
    candidate_revision_count: null,
    candidate_page_url: null,
  },
});
check(historical.elements["card-state"].textContent === "历史卡片");
check(historical.elements.status.textContent === "此更新卡已失效");
check(historical.elements.current.disabled && historical.elements.all.disabled);

const currentFailure = await mount();
const failureRestore = currentFailure.latestToolCall(
  "get_zdecision_candidate_refresh",
);
await currentFailure.respond(
  failureRestore,
  state("unavailable", "attached", "current_session"),
);
check(currentFailure.elements["card-state"].textContent === "当前卡片");
check(currentFailure.elements.status.textContent === "暂时无法更新");
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_mcp_contract_registers_only_the_real_card_and_tools \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_widget_distinguishes_current_and_historical_cards
```

Expected: FAIL because the registered URI is still v1 and the widget does not expose or update `card-state`.

- [ ] **Step 3: Implement the minimal production behavior**

In `mcp_server.py`, change only the host-visible URI:

```python
UPDATE_CANDIDATES_URI = "ui://zdecision/update-candidates-v2.html"
```

Keep `UPDATE_CANDIDATES_PATH` pointed at the existing packaged HTML file. In the HTML, give the existing decorative index the `card-state` ID and default it to `当前卡片`:

```html
<span id="card-state" class="index" aria-live="polite">当前卡片</span>
```

Add `expired: "此更新卡已失效"` to the bounded labels. In `boundedResult`, recognize only this exact unbound envelope before normal consistency validation:

```javascript
const isHistorical = value.safe_state === "unavailable"
  && value.submission_state === undefined
  && value.chosen_scope === undefined;
if (isHistorical) {
  return {
    valid: true,
    safe_state: "expired",
    candidate_revision_count: null,
    candidate_page_url: null,
    submission_state: null,
    chosen_scope: null,
  };
}
```

Handle that value before normal binding-state assignment:

```javascript
if (value.safe_state === "expired") {
  submissionState = null;
  selected = true;
  cardState.textContent = "历史卡片";
  renderResult(value);
  lockActions();
  return;
}
cardState.textContent = "当前卡片";
```

Do not reinterpret a bound `submission_state: "attached"` plus `safe_state: "unavailable"`; it remains a current-card temporary failure.

- [ ] **Step 4: Run the focused test and inline module**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_mcp_contract_registers_only_the_real_card_and_tools \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_widget_distinguishes_current_and_historical_cards
.venv/bin/python -m unittest tests.test_mcp_inline_refresh
git diff --check
```

Expected: the focused tests pass, all inline-card tests pass, and `git diff --check` emits no output.

- [ ] **Step 5: Run the complete automated suite**

Run:

```bash
.venv/bin/python -m unittest discover -s tests
```

Expected: all tests pass with only the repository's two known skips.

- [ ] **Step 6: Perform one real Codex Desktop acceptance**

After one Codex reload, render exactly one new card in the enabled `zstack-ui-next` task. Verify it visibly says `当前卡片`, enables both actions, and an older expired card says `历史卡片` / `此更新卡已失效`. Continue the existing local-link click probe only from the new current card.

- [ ] **Step 7: Commit only after real acceptance**

If freshness acceptance passes, review the local-link probe outcome. Keep and commit the anchor changes only if the click reaches the intended Codex browser surface; otherwise revert only that probe before committing the freshness fix.

```bash
git add src/zdecision/agent/mcp_server.py \
  src/zdecision/agent/static/update-candidates-v1.html \
  tests/test_mcp_inline_refresh.py
git commit -m "fix: distinguish current candidate refresh cards"
```

Do not push without a separate user request.
