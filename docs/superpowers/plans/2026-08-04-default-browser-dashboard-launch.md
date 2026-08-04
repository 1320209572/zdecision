# Default-Browser Dashboard Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a successful ZDecision inline card open the repository-filtered central decision system in the operating system's default browser with one explicit click.

**Architecture:** Add a small injectable local Browser Launcher, expose one app-only MCP action that validates the private Control Binding and derives the dashboard URL server-side, then move the versioned widget from the failed link path to that tool. The central Web application remains independent and unchanged.

**Tech Stack:** Python 3.14, FastMCP, HTML/CSS/vanilla JavaScript, Python `webbrowser`, `unittest`, Node `vm` widget harnesses.

## Global Constraints

- Work directly on the existing `main` worktree; do not create a worktree or another branch.
- Preserve the current uncommitted card-freshness changes and unrelated user work.
- The disposable real-host probe has already been removed and must not reappear.
- The final card resource URI is exactly `ui://zdecision/update-candidates-v3.html`.
- The widget sends only `control_id`; it never supplies a URL, repository ID, product ID, path, command, or browser name.
- The launch tool is app-only, non-read-only, non-destructive, non-idempotent, and open-world.
- Do not use `ui/open-link`, `window.open`, a clickable HTML anchor, another model Turn, or a second local Web server.
- Keep Candidate content, Decision content, Session identity, Turn identity, local paths, and credentials out of the card and launch contract.
- Do not redesign the central Web application, Decision boards, Review, publication, or SSO in this slice.
- Use one focused test module, one complete suite, and one real default-browser acceptance; do not start another broad review.

---

### Task 1: Add the testable default-browser boundary

**Files:**
- Create: `src/zdecision/agent/browser_launcher.py`
- Create: `tests/test_browser_launcher.py`

**Interfaces:**
- Consumes: one locally derived HTTP or HTTPS URL string.
- Produces: `BrowserLauncher.open(url: str) -> bool` and `SystemDefaultBrowserLauncher`.

- [ ] **Step 1: Write the failing launcher tests**

Create `tests/test_browser_launcher.py`:

```python
from __future__ import annotations

import unittest

from zdecision.agent.browser_launcher import SystemDefaultBrowserLauncher


class SystemDefaultBrowserLauncherTests(unittest.TestCase):
    def test_open_delegates_the_exact_url_to_the_default_browser(self) -> None:
        calls: list[str] = []

        def opener(url: str) -> bool:
            calls.append(url)
            return True

        launcher = SystemDefaultBrowserLauncher(opener=opener)

        self.assertTrue(launcher.open("http://127.0.0.1:8765/?repository_id=repo_2"))
        self.assertEqual(
            ["http://127.0.0.1:8765/?repository_id=repo_2"],
            calls,
        )

    def test_open_returns_false_when_the_platform_rejects_the_request(self) -> None:
        launcher = SystemDefaultBrowserLauncher(opener=lambda _url: False)

        self.assertFalse(launcher.open("https://decisions.example.test/"))

    def test_open_contains_platform_exceptions(self) -> None:
        def failing_opener(_url: str) -> bool:
            raise OSError("browser unavailable")

        launcher = SystemDefaultBrowserLauncher(opener=failing_opener)

        self.assertFalse(launcher.open("https://decisions.example.test/"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the launcher test and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_browser_launcher
```

Expected: FAIL with `ModuleNotFoundError: No module named 'zdecision.agent.browser_launcher'`.

- [ ] **Step 3: Implement the minimal launcher boundary**

Create `src/zdecision/agent/browser_launcher.py`:

```python
"""Local operating-system browser launch boundary."""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from typing import Protocol


class BrowserLauncher(Protocol):
    def open(self, url: str) -> bool:
        """Request one URL in the operating system's default browser."""

        ...


class SystemDefaultBrowserLauncher:
    def __init__(
        self,
        *,
        opener: Callable[[str], bool] | None = None,
    ) -> None:
        self._opener = opener or webbrowser.open_new_tab

    def open(self, url: str) -> bool:
        try:
            return bool(self._opener(url))
        except Exception:
            return False
```

The local MCP tool, not this transport adapter, remains responsible for URL validation and derivation. The adapter never constructs a shell command.

- [ ] **Step 4: Run the launcher tests and verify GREEN**

Run:

```bash
.venv/bin/python -m unittest tests.test_browser_launcher
git diff --check
```

Expected: 3 tests pass and `git diff --check` emits no output.

- [ ] **Step 5: Commit the launcher boundary**

```bash
git add src/zdecision/agent/browser_launcher.py tests/test_browser_launcher.py
git commit -m "feat: add default browser launcher"
```

---

### Task 2: Add the trusted app-only dashboard action

**Files:**
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `tests/test_mcp_inline_refresh.py`

**Interfaces:**
- Consumes: `BrowserLauncher.open(url: str) -> bool`, one opaque `control_id`, the existing private `ControlBindingStore`, repository mapping, and configured central base URL.
- Produces: `LocalMcpTools.open_zdecision_dashboard(control_id: str) -> dict[str, object]` and the app-only MCP tool `open_zdecision_dashboard`.

- [ ] **Step 1: Add the recording launcher and dependency to the test fixture**

Add this helper beside `RecordingCentralClient` in `tests/test_mcp_inline_refresh.py`:

```python
class RecordingBrowserLauncher:
    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.urls: list[str] = []

    def open(self, url: str) -> bool:
        self.urls.append(url)
        return self.accepted
```

In `asyncSetUp`, add:

```python
self.browser_launcher = RecordingBrowserLauncher()
```

Add `"browser_launcher"` to the `required` constructor-parameter set in `domain()`, and pass:

```python
browser_launcher=self.browser_launcher,
```

Also add a keyword argument to `domain()` so URL validation can be exercised
without changing process configuration:

```python
central_base_url: str = CENTRAL_BASE_URL,
```

and replace the fixed constructor value with:

```python
central_base_url=central_base_url,
```

- [ ] **Step 2: Write failing authorization and exact-target tests**

Add these tests to `McpInlineRefreshTests`:

```python
async def test_dashboard_launch_uses_only_the_bound_repository_url(self) -> None:
    domain = self.domain()
    domain.start_zdecision_candidate_refresh(CONTROL_ID, "current_session")

    result = domain.open_zdecision_dashboard(CONTROL_ID)

    expected = (
        "http://127.0.0.1:8765/"
        "?repository_id=repo_22222222222222222222222222222222"
    )
    self.assertEqual(
        {"safe_state": "launch_requested", "dashboard_url": expected},
        result,
    )
    self.assertEqual([expected], self.browser_launcher.urls)

async def test_dashboard_launch_rejects_unattached_or_invalid_controls(self) -> None:
    domain = self.domain()

    for control_id in (CONTROL_ID, "ctl_" + "9" * 32):
        with self.subTest(control_id=control_id):
            self.assertEqual(
                {"safe_state": "unavailable", "dashboard_url": None},
                domain.open_zdecision_dashboard(control_id),
            )

    self.assertEqual([], self.browser_launcher.urls)

async def test_dashboard_launch_rechecks_the_repository_mapping(self) -> None:
    domain = self.domain()
    domain.start_zdecision_candidate_refresh(CONTROL_ID, "current_session")
    self.database.put_test_repository_mapping(
        TestRepositoryMapping(
            repository_id=REPOSITORY_ID,
            product_id=PRODUCT_ID,
            product_name="ZDecision",
            enabled=False,
        )
    )

    self.assertEqual(
        {"safe_state": "unavailable", "dashboard_url": None},
        domain.open_zdecision_dashboard(CONTROL_ID),
    )
    self.assertEqual([], self.browser_launcher.urls)

async def test_dashboard_launch_exposes_safe_fallback_when_launcher_rejects(
    self,
) -> None:
    self.browser_launcher.accepted = False
    domain = self.domain()
    domain.start_zdecision_candidate_refresh(CONTROL_ID, "current_session")

    result = domain.open_zdecision_dashboard(CONTROL_ID)

    self.assertEqual("unavailable", result["safe_state"])
    self.assertEqual(
        "http://127.0.0.1:8765/"
        "?repository_id=repo_22222222222222222222222222222222",
        result["dashboard_url"],
    )
    self.assertEqual([result["dashboard_url"]], self.browser_launcher.urls)

async def test_dashboard_launch_derives_https_and_rejects_invalid_base_urls(
    self,
) -> None:
    domain = self.domain(central_base_url="https://decisions.example.test")
    domain.start_zdecision_candidate_refresh(CONTROL_ID, "current_session")

    result = domain.open_zdecision_dashboard(CONTROL_ID)

    self.assertEqual(
        "https://decisions.example.test/"
        "?repository_id=repo_22222222222222222222222222222222",
        result["dashboard_url"],
    )
    self.assertEqual([result["dashboard_url"]], self.browser_launcher.urls)

    invalid = self.domain(central_base_url="https://user:pass@example.test")
    self.assertEqual(
        {"safe_state": "unavailable", "dashboard_url": None},
        invalid.open_zdecision_dashboard(CONTROL_ID),
    )
    self.assertEqual(1, len(self.browser_launcher.urls))
```

- [ ] **Step 3: Extend the MCP contract test and verify RED**

In `test_mcp_contract_registers_only_the_real_card_and_tools`, add `open_zdecision_dashboard` to the expected tool set and assert:

```python
open_dashboard = tools["open_zdecision_dashboard"]
self.assertEqual(["app"], open_dashboard.meta["ui"]["visibility"])
self.assertFalse(open_dashboard.annotations.readOnlyHint)
self.assertFalse(open_dashboard.annotations.destructiveHint)
self.assertFalse(open_dashboard.annotations.idempotentHint)
self.assertTrue(open_dashboard.annotations.openWorldHint)
```

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_dashboard_launch_uses_only_the_bound_repository_url \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_dashboard_launch_rejects_unattached_or_invalid_controls \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_dashboard_launch_rechecks_the_repository_mapping \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_dashboard_launch_exposes_safe_fallback_when_launcher_rejects \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_dashboard_launch_derives_https_and_rejects_invalid_base_urls \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_mcp_contract_registers_only_the_real_card_and_tools
```

Expected: FAIL because `LocalMcpTools` has no `browser_launcher` dependency or `open_zdecision_dashboard` method and the tool is not registered.

- [ ] **Step 4: Implement the dependency and domain action**

Import the boundary in `src/zdecision/agent/mcp_server.py`:

```python
from zdecision.agent.browser_launcher import (
    BrowserLauncher,
    SystemDefaultBrowserLauncher,
)
```

Add the optional dependency to `LocalMcpTools.__init__` and store it:

```python
browser_launcher: BrowserLauncher | None = None,
```

```python
self.browser_launcher = browser_launcher
```

Rename the private URL constructor from `_candidate_page_url` to `_dashboard_url`, preserving its existing validation and `repository_id` query encoding. Update `_safe_request_output` to call `_dashboard_url` while retaining the existing `candidate_page_url` status key for this slice.

Add the action:

```python
def open_zdecision_dashboard(self, control_id: str) -> dict[str, object]:
    binding = self._valid_binding(control_id, require_current_cwd=False)
    if (
        binding is None
        or binding.submission_state != "attached"
        or binding.chosen_scope is None
        or binding.central_request_id is None
        or self.browser_launcher is None
    ):
        return {"safe_state": "unavailable", "dashboard_url": None}

    dashboard_url = _dashboard_url(
        self.central_base_url,
        binding.repository_id,
    )
    if dashboard_url is None:
        return {"safe_state": "unavailable", "dashboard_url": None}

    try:
        accepted = self.browser_launcher.open(dashboard_url)
    except Exception:
        accepted = False
    return {
        "safe_state": "launch_requested" if accepted else "unavailable",
        "dashboard_url": dashboard_url,
    }
```

Do not query central Candidate content and do not accept a target URL from the caller.

- [ ] **Step 5: Register the app-only tool and production launcher**

Define a distinct annotation next to `app_action`:

```python
browser_action = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
```

Register:

```python
@server.tool(
    title="Open ZDecision dashboard",
    description="Open the trusted product dashboard in the default browser.",
    annotations=browser_action,
    meta={"ui": {"visibility": ["app"]}},
)
def open_zdecision_dashboard(control_id: str) -> dict[str, object]:
    return tools.open_zdecision_dashboard(control_id)
```

In `run_mcp`, pass the real boundary when constructing `LocalMcpTools`:

```python
browser_launcher=SystemDefaultBrowserLauncher(),
```

- [ ] **Step 6: Run the focused domain tests and verify GREEN**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_browser_launcher \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_dashboard_launch_uses_only_the_bound_repository_url \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_dashboard_launch_rejects_unattached_or_invalid_controls \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_dashboard_launch_rechecks_the_repository_mapping \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_dashboard_launch_exposes_safe_fallback_when_launcher_rejects \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_dashboard_launch_derives_https_and_rejects_invalid_base_urls \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_mcp_contract_registers_only_the_real_card_and_tools
git diff --check
```

Expected: all selected tests pass and `git diff --check` emits no output.

- [ ] **Step 7: Commit the trusted launch action**

```bash
git add src/zdecision/agent/mcp_server.py tests/test_mcp_inline_refresh.py
git commit -m "feat: open trusted decision dashboard"
```

---

### Task 3: Route the v3 card through the trusted action

**Files:**
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `src/zdecision/agent/static/update-candidates-v1.html`
- Modify: `tests/test_mcp_inline_refresh.py`

**Interfaces:**
- Consumes: `open_zdecision_dashboard(control_id)` returning `safe_state` and `dashboard_url`.
- Produces: immutable resource `ui://zdecision/update-candidates-v3.html`, button **打开决策中心**, bounded launch status, and selectable non-clickable fallback text.

- [ ] **Step 1: Write the failing v3 resource and markup assertions**

Change the test constant:

```python
WIDGET_URI = "ui://zdecision/update-candidates-v3.html"
```

Replace the existing clickable-anchor parser assertions with:

```python
self.assertNotIn('target="_blank"', html)
self.assertNotIn('rel="noopener noreferrer"', html)
self.assertNotIn('"ui/open-link"', html)
self.assertNotIn("window.open(", html)
self.assertIn('id="page-address"', html)
self.assertIn("打开决策中心", html)
self.assertIn('name: "open_zdecision_dashboard"', html)
```

In `test_widget_uses_portable_bridge_and_contains_no_candidate_payload`, require `"open_zdecision_dashboard"` and explicitly reject `"ui/open-link"`.

- [ ] **Step 2: Replace the old open-link harness scenario**

Rename `test_widget_only_requests_https_page_and_never_claims_navigation` to `test_widget_requests_trusted_dashboard_launch_and_handles_uncertainty`.

Keep its existing Node harness setup through the terminal successful refresh, then replace the open-link assertions with this sequence:

```javascript
const successfulClick = elements["open-page"].dispatch("click");
const successfulOpen = latestCall("tools/call");
check(
  successfulOpen.params.name === "open_zdecision_dashboard",
  "open action called the wrong tool",
);
check(
  JSON.stringify(successfulOpen.params.arguments)
    === JSON.stringify({control_id: "ctl_11111111111111111111111111111111"}),
  "open action sent anything other than the trusted control",
);
check(elements["open-page"].disabled, "open action allowed a duplicate call");
deliver({
  jsonrpc: "2.0",
  id: successfulOpen.id,
  result: {
    structuredContent: {
      safe_state: "launch_requested",
      dashboard_url: candidateUrl,
    },
  },
});
await successfulClick;
check(
  elements.status.textContent === "已请求使用默认浏览器打开决策中心",
  "accepted launch used false navigation copy",
);
check(!elements["open-page"].disabled, "accepted launch was not retryable");

const rejectedClick = elements["open-page"].dispatch("click");
const rejectedOpen = latestCall("tools/call");
deliver({
  jsonrpc: "2.0",
  id: rejectedOpen.id,
  result: {
    structuredContent: {
      safe_state: "unavailable",
      dashboard_url: candidateUrl,
    },
  },
});
await rejectedClick;
check(
  elements.status.textContent === "无法自动打开，请使用下方地址",
  "rejected launch hid the fallback state",
);
check(!elements["page-address"].hidden, "fallback address stayed hidden");
check(
  elements["page-address"].textContent === candidateUrl,
  "fallback address changed",
);

const callCount = outbound.filter(
  (message) => message.method === "tools/call"
    && message.params?.name === "open_zdecision_dashboard",
).length;
const timedOutClick = elements["open-page"].dispatch("click");
takeTimer(5000)();
await timedOutClick;
check(
  outbound.filter(
    (message) => message.method === "tools/call"
      && message.params?.name === "open_zdecision_dashboard",
  ).length === callCount + 1,
  "lost response triggered an automatic second launch",
);
```

Expected final harness marker: `default-browser-launch-ok`.

- [ ] **Step 3: Run the widget tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_mcp_contract_registers_only_the_real_card_and_tools \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_widget_uses_portable_bridge_and_contains_no_candidate_payload \
  tests.test_mcp_inline_refresh.McpInlineRefreshTests.test_widget_requests_trusted_dashboard_launch_and_handles_uncertainty
```

Expected: FAIL because the resource is still v2 and the widget still uses the failed link path.

- [ ] **Step 4: Implement the v3 widget behavior**

In `mcp_server.py`, set:

```python
UPDATE_CANDIDATES_URI = "ui://zdecision/update-candidates-v3.html"
```

In the widget markup, keep the existing button ID but change its copy and restore the fallback to plain text:

```html
<button id="open-page" type="button" hidden>打开决策中心</button>
<p id="page-address" aria-label="决策中心地址" hidden></p>
```

Add a bounded launch-result parser:

```javascript
function boundedLaunchResult(result) {
  const toolResult = result?.structuredContent ? result : result?.toolResult;
  const value = toolResult?.structuredContent;
  const safeState = value?.safe_state;
  return {
    valid: toolResult?.isError !== true
      && (safeState === "launch_requested" || safeState === "unavailable"),
    safe_state: safeState,
    dashboard_url: typeof value?.dashboard_url === "string"
      ? value.dashboard_url
      : null,
  };
}
```

Replace the current `openPageButton` handler with:

```javascript
openPageButton.addEventListener("click", async () => {
  if (typeof controlId !== "string") return;
  openPageButton.disabled = true;
  pageAddress.hidden = true;
  status.textContent = "正在请求默认浏览器";
  try {
    const result = await request("tools/call", {
      name: "open_zdecision_dashboard",
      arguments: {control_id: controlId},
    }, 5000);
    const launch = boundedLaunchResult(result);
    if (!launch.valid) throw new Error("invalid_dashboard_launch_result");
    const fallbackUrl = launch.dashboard_url || pageUrl;
    if (launch.safe_state === "launch_requested") {
      status.textContent = "已请求使用默认浏览器打开决策中心";
      openPageButton.textContent = "再次打开决策中心";
    } else {
      status.textContent = "无法自动打开，请使用下方地址";
      openPageButton.textContent = "重试打开决策中心";
      if (typeof fallbackUrl === "string") {
        pageAddress.textContent = fallbackUrl;
        pageAddress.hidden = false;
      }
    }
  } catch {
    status.textContent = "无法确认是否已打开；如未出现请重试";
    openPageButton.textContent = "重试打开决策中心";
    if (typeof pageUrl === "string") {
      pageAddress.textContent = pageUrl;
      pageAddress.hidden = false;
    }
  } finally {
    openPageButton.disabled = false;
  }
});
```

Do not add an automatic retry timer to this handler.

- [ ] **Step 5: Run the complete focused module**

Run:

```bash
.venv/bin/python -m unittest tests.test_browser_launcher tests.test_mcp_inline_refresh
git diff --check
```

Expected: the launcher and complete inline-card modules pass, including card freshness, crash recovery, scope selection, privacy, and default-browser launch tests.

- [ ] **Step 6: Run the complete repository suite**

Run:

```bash
.venv/bin/python -m unittest discover -s tests
```

Expected: all tests pass with only the repository's two known skips.

- [ ] **Step 7: Commit the v3 card and current freshness changes**

Review `git diff --stat` and confirm only the planned launcher/card files and existing freshness files are included, then commit:

```bash
git add \
  src/zdecision/agent/mcp_server.py \
  src/zdecision/agent/static/update-candidates-v1.html \
  tests/test_mcp_inline_refresh.py
git commit -m "feat: launch decision dashboard from Codex card"
```

Do not push without a separate user request.

---

### Task 4: Perform the single real Desktop acceptance and stop

**Files:**
- No production file changes unless this acceptance exposes one confirmed blocker.

**Interfaces:**
- Consumes: the committed v3 card, app-only launch tool, local Agent configuration, running central service, and an enabled repository.
- Produces: one binary acceptance result for the complete click-to-default-browser chain.

- [ ] **Step 1: Verify runtime prerequisites without exposing credentials**

Run:

```bash
curl --silent --show-error --fail --max-time 2 \
  http://127.0.0.1:8765/ >/dev/null
ps -ax -o command= | rg '[z]decision-agent service run|[z]decision-central run'
```

Expected: the central page responds and exactly one persistent Agent and one central service are visible. Do not print Agent configuration contents.

- [ ] **Step 2: Reload Codex once and render one new card**

Fully restart Codex so it loads the new MCP tool and immutable v3 resource. In one enabled-repository task, send **更新候选决策** exactly once and verify the card says **当前卡片**.

- [ ] **Step 3: Complete one refresh and launch the dashboard**

Choose either approved scope, wait for `本次同步 N 条候选决策` or `没有发现新的候选决策`, then click **打开决策中心** exactly once.

Expected:

- the operating system's default browser opens
  `http://127.0.0.1:8765/?repository_id=<bound repository id>`;
- Codex creates no empty in-app Browser tab;
- the card says **已请求使用默认浏览器打开决策中心**; and
- the central page is filtered to the expected product and shows synchronized Candidate revisions.

- [ ] **Step 4: Apply the stopping rule**

If the acceptance passes, stop. If it exposes one confirmed blocker, write one failing regression test, make one focused correction, run the focused modules and full suite once, and repeat this acceptance once. Record non-blocking improvements separately; do not start another architecture or code-review loop.
