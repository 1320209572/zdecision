# Codex Inline Candidate Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the accepted no-side-effect Codex UI probe with a real
two-button control that starts either current-Session or all-valid-Session
Candidate refresh, follows safe progress, and leaves Candidate Review and
publication on the central page.

**Architecture:** A narrowly matched Codex `PreToolUse` Hook binds the
model-visible render call to host-owned Session, Turn, and repository facts in
private local SQLite state. The widget's app-only action persists one immutable
scope intent before using a device-authenticated central request API. The
persistent local Agent claims only safe scope/action fields, resolves
current-Session identity locally, and feeds the existing two-stage Capture and
reconciliation pipeline. The central service remains authoritative for request
state, concurrency, Candidate synchronization, and safe result count.

**Tech Stack:** Python 3.11+, SQLite, FastAPI, `httpx`, `mcp>=1.28,<2`,
FastMCP/MCP Apps, vanilla HTML/CSS/JavaScript, `unittest`.

## Global Constraints

- Work directly on `main`; do not create a worktree or a Registry branch.
- Preserve the existing Capture, reconciliation, Review, publication, and
  Decision Registry algorithms.
- A render-tool call never starts Capture. Only clicking **当前 Session** or
  **所有有效 Session** creates a request.
- Never send Session ID, Turn ID, `cwd`, local path, Prompt, transcript, code,
  diff, tool input/output, or `control_id` to the central service.
- The central service derives organization, actor, product, and device
  identity; clients cannot submit them.
- `current_session` without an exact private intent fails closed. It never
  falls back to repository-wide Capture or recent-Session guessing.
- Keep one active request per repository. Exact action replay is idempotent;
  another action receives `repository_capture_busy`; do not add a queue.
- The widget never receives Candidate content and never performs Review or
  publication.
- Do not add OIDC/SSO, Decision recall, non-code sources, scheduled Capture,
  React/Node, or production visual polish in this slice.
- Use test-driven changes and one commit per task. Do not start another broad
  architecture or code-review loop.
- Final stopping rule: one focused suite, one complete suite, one package
  inspection, and one real Codex Desktop acceptance. A confirmed blocker may
  receive one focused correction.

---

### Task 1: Make Capture Request scope, identity, progress, and result explicit

**Files:**

- Modify: `src/zdecision/sync/contracts.py`
- Modify: `src/zdecision/central/store.py`
- Modify: `src/zdecision/central/service.py`
- Test: `tests/test_sync_contracts.py`
- Test: `tests/test_central_requests.py`
- Update fixtures in:
  `tests/test_agent_service.py`,
  `tests/test_capture_request_processor.py`,
  `tests/test_central_client.py`,
  `tests/test_central_api.py`,
  `tests/integration/test_on_demand_capture_core.py`

**Interfaces:**

```python
CaptureScope = Literal["current_session", "all_valid_sessions"]

@dataclass(frozen=True)
class CaptureRequestCreate:
    repository_id: str
    template_id: str
    capture_scope: CaptureScope
    client_action_id: str

@dataclass(frozen=True)
class CaptureRequestView:
    request_id: str
    repository_id: str
    product_id: str
    product_name: str
    template_id: str
    state: CaptureRequestState
    progress_code: str
    candidate_revision_count: int | None
    last_sequence: int
    created_at: str
    updated_at: str

@dataclass(frozen=True)
class ClaimedCaptureRequest:
    request_id: str
    repository_id: str
    product_id: str
    product_name: str
    template_id: str
    capture_scope: CaptureScope
    client_action_id: str
    lease_token: str
    lease_expires_at: str
```

- [ ] **Step 1: Write strict transport and lifecycle tests**

  Add exact-field round-trip cases:

  ```python
  command = api.CaptureRequestCreate.from_dict(
      {
          "repository_id": REPOSITORY_ID,
          "template_id": "business",
          "capture_scope": "current_session",
          "client_action_id": "codex_action_001",
      }
  )
  self.assertEqual("current_session", command.capture_scope)

  with self.assertRaises(ValueError):
      api.CaptureRequestCreate.from_dict(
          {
              "repository_id": REPOSITORY_ID,
              "template_id": "business",
              "capture_scope": "recent_session",
              "client_action_id": "codex_action_001",
          }
      )
  ```

  Replace the old “second action attaches to active request” expectation with:

  ```python
  first = self.create("web_action_001")
  replay = self.create("web_action_001")
  self.assertEqual(first.request_id, replay.request_id)

  with self.assertRaisesRegex(
      RequestConflict, "repository_capture_busy"
  ):
      self.create("web_action_002")

  action_count = self.store.connection.execute(
      "SELECT COUNT(*) FROM capture_request_actions"
  ).fetchone()[0]
  self.assertEqual(1, action_count)
  ```

  Add cases proving that:

  - replaying one action with another repository, template, or scope conflicts;
  - `claim_next()` returns the stored scope and original action ID;
  - active and failed requests expose
    `candidate_revision_count is None`;
  - a completed empty batch exposes `0`;
  - a completed non-empty batch exposes that request batch's item count; and
  - a previously busy action creates a new request only after the old request
    is terminal.

- [ ] **Step 2: Run the focused tests and verify RED**

  ```bash
  .venv/bin/python -m unittest \
    tests.test_sync_contracts \
    tests.test_central_requests -v
  ```

  Expected: failures because scope, progress, result count, and busy semantics
  are absent.

- [ ] **Step 3: Implement strict shared contracts**

  Add a closed validator:

  ```python
  CaptureScope = Literal["current_session", "all_valid_sessions"]
  _CAPTURE_SCOPES = frozenset(("current_session", "all_valid_sessions"))

  def _capture_scope(value: object) -> CaptureScope:
      if value not in _CAPTURE_SCOPES:
          raise ValueError("capture_scope is invalid")
      return cast(CaptureScope, value)

  def _optional_nonnegative_integer(
      value: object, field_name: str
  ) -> int | None:
      if value is None:
          return None
      if (
          not isinstance(value, int)
          or isinstance(value, bool)
          or value < 0
      ):
          raise ValueError(f"{field_name} is invalid")
      return value
  ```

  Make all three dataclasses serialize and parse only their exact new fields.
  Update every existing request fixture explicitly; do not give
  `CaptureRequestCreate` a default scope.

- [ ] **Step 4: Persist the request's original action and scope**

  Extend fresh `capture_requests` schema with:

  ```sql
  capture_scope TEXT NOT NULL CHECK(
      capture_scope IN ('current_session', 'all_valid_sessions')
  ),
  client_action_id TEXT NOT NULL,
  result_candidate_count INTEGER CHECK(
      result_candidate_count IS NULL OR result_candidate_count >= 0
  ),
  ```

  Add an idempotent `PRAGMA table_info(capture_requests)` migration for the
  existing technical-Demo database:

  - old requests receive `capture_scope = 'all_valid_sessions'`;
  - recover the original action by recomputing `capture_request_id` with each
    stored action and selecting the unique value equal to the existing request
    ID;
  - fail closed if the original action cannot be identified uniquely;
  - backfill successful request counts from `candidate_batches.item_count`;
  - leave non-success counts null.

  Do not use `PRAGMA user_version`; the local stores share the same database
  file and already create their own bounded tables.

- [ ] **Step 5: Enforce exact replay and repository busy**

  Keep the current `BEGIN IMMEDIATE` ordering. The service branch must become:

  ```python
  if action_row is not None:
      if (
          action_row["repository_id"] != command.repository_id
          or action_row["template_id"] != command.template_id
          or action_row["capture_scope"] != command.capture_scope
      ):
          raise RequestConflict("capture_request_action_conflict")
      return _request_view(
          _request_row(connection, action_row["request_id"])
      )

  if active is not None:
      raise RequestConflict("repository_capture_busy")
  ```

  Insert `capture_scope` and `client_action_id` into the new request row.
  Include both in `_claimed_view()`. Make `_request_row()` join the event whose
  sequence equals `capture_requests.last_sequence` and expose that event's
  fixed `code` as `progress_code`; every path returning a public view must
  re-read through `_request_row()`. During `complete()`, copy
  `candidate_batches.item_count` into `result_candidate_count` in the same
  transaction that marks success.

- [ ] **Step 6: Verify migration and GREEN**

  Add a test that creates the prior schema, inserts an old page request and its
  action row, closes it, then opens it through the new `CentralStore`. Assert
  the row was recovered as `all_valid_sessions` with the original action ID.

  ```bash
  .venv/bin/python -m unittest \
    tests.test_sync_contracts \
    tests.test_central_requests \
    tests.test_agent_service \
    tests.test_capture_request_processor \
    tests.test_central_client \
    tests.test_central_api \
    tests.integration.test_on_demand_capture_core -v
  ```

  Expected: all listed tests pass with explicit fixture fields.

- [ ] **Step 7: Commit**

  ```bash
  git add src/zdecision/sync/contracts.py \
    src/zdecision/central/store.py \
    src/zdecision/central/service.py \
    tests/test_sync_contracts.py \
    tests/test_central_requests.py \
    tests/test_agent_service.py \
    tests/test_capture_request_processor.py \
    tests/test_central_client.py \
    tests/test_central_api.py \
    tests/integration/test_on_demand_capture_core.py
  git commit -m "feat: define scoped candidate refresh requests"
  ```

---

### Task 2: Add the authenticated Plugin request edge and preserve page behavior

**Files:**

- Modify: `src/zdecision/central/auth.py`
- Modify: `src/zdecision/central/api.py`
- Modify: `src/zdecision/agent/central_client.py`
- Modify: `src/zdecision/central/static/index.html`
- Test: `tests/test_central_api.py`
- Test: `tests/test_central_client.py`
- Test: `tests/test_update_candidates_page.py`

**Interfaces:**

```python
def authenticate_plugin_action(
    self, authorization: str | None
) -> Principal:
    self.authenticate_device(authorization)
    return self.browser_principal()
```

- `CentralClient.create_capture_request(command: CaptureRequestCreate) ->
  CaptureRequestView`
- `CentralClient.get_capture_request(request_id: str) -> CaptureRequestView`

- [ ] **Step 1: Write failing authentication and privacy tests**

  Cover the dedicated endpoints:

  ```python
  response = self.client.post(
      "/api/v1/plugin/capture-requests",
      headers={"Authorization": f"Bearer {DEVICE_TOKEN}"},
      json={
          "repository_id": REPOSITORY_ID,
          "template_id": "business",
          "capture_scope": "current_session",
          "client_action_id": "codex_action_001",
      },
  )
  self.assertEqual(200, response.status_code)
  self.assertEqual("user_demo", self.store.get_request_record(
      response.json()["request_id"]
  ).actor_id)
  ```

  Assert missing/wrong Bearer tokens return 401. For each forbidden field
  `organization_id`, `actor_id`, `product_id`, `device_id`, `session_id`,
  `turn_id`, `cwd`, and `control_id`, add it to the POST body and assert 422.
  Assert busy returns exactly:

  ```python
  self.assertEqual(
      {"error": "repository_capture_busy"},
      response.json(),
  )
  ```

  In the HTTP client test, inspect request bytes and prove the POST body has
  exactly:

  ```python
  {
      "repository_id": REPOSITORY_ID,
      "template_id": "business",
      "capture_scope": "current_session",
      "client_action_id": "codex_action_001",
  }
  ```

- [ ] **Step 2: Run the focused tests and verify RED**

  ```bash
  .venv/bin/python -m unittest \
    tests.test_central_api \
    tests.test_central_client \
    tests.test_update_candidates_page -v
  ```

  Expected: failures because the Plugin routes and four-field page request do
  not exist.

- [ ] **Step 3: Add the Demo Plugin identity adapter and routes**

  Register:

  ```text
  POST /api/v1/plugin/capture-requests
  GET  /api/v1/plugin/capture-requests/{request_id}
  ```

  Both routes require the existing device Bearer token, then convert it to the
  server-configured Demo user principal through
  `authenticate_plugin_action()`. The Pydantic body contains only
  `repository_id`, `template_id`, `capture_scope`, and `client_action_id`.
  Keep existing browser and Agent routes unchanged.

- [ ] **Step 4: Add bounded client create/get methods**

  Refactor the private transport helper to support GET and POST without
  changing the existing retry limit. Only the Plugin create path may preserve
  the allowlisted 409 code `repository_capture_busy`; every other unexpected
  status remains a sanitized `central_request_rejected`.

  ```python
  def create_capture_request(
      self, command: CaptureRequestCreate
  ) -> CaptureRequestView:
      _, value = self._request(
          "POST",
          "/api/v1/plugin/capture-requests",
          payload=command.to_dict(),
          allowed_statuses=(200,),
          allowed_error_codes=("repository_capture_busy",),
      )
      return CaptureRequestView.from_dict(value)

  def get_capture_request(
      self, request_id: str
  ) -> CaptureRequestView:
      _, value = self._request(
          "GET",
          f"/api/v1/plugin/capture-requests/{_request_id(request_id)}",
          payload=None,
          allowed_statuses=(200,),
      )
      return CaptureRequestView.from_dict(value)
  ```

  Keep connection/transient retries at three attempts with the existing
  bounded backoff.

- [ ] **Step 5: Keep the web page explicitly repository-wide**

  Change its request body to:

  ```javascript
  body: JSON.stringify({
    repository_id,
    template_id: "business",
    capture_scope: "all_valid_sessions",
    client_action_id
  })
  ```

  Add optional validated `repository_id` query filtering after repository
  retrieval so the inline card can open the corresponding Candidate section.
  Invalid or unknown query values show the normal page without trusting them
  as identity.

- [ ] **Step 6: Verify GREEN and commit**

  ```bash
  .venv/bin/python -m unittest \
    tests.test_central_api \
    tests.test_central_client \
    tests.test_update_candidates_page -v
  git add src/zdecision/central/auth.py \
    src/zdecision/central/api.py \
    src/zdecision/agent/central_client.py \
    src/zdecision/central/static/index.html \
    tests/test_central_api.py \
    tests/test_central_client.py \
    tests/test_update_candidates_page.py
  git commit -m "feat: add authenticated plugin capture requests"
  ```

---

### Task 3: Persist private Control Bindings and locate the existing Agent config

**Files:**

- Create: `src/zdecision/agent/control_bindings.py`
- Create: `src/zdecision/agent/config_locator.py`
- Modify: `src/zdecision/agent/cli.py`
- Modify: `src/zdecision/agent/service.py`
- Test: `tests/test_control_bindings.py`
- Test: `tests/test_agent_config_locator.py`
- Test: `tests/test_agent_service.py`
- Test: `tests/test_plugin_contract.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ControlBinding:
    control_id: str
    session_id: str
    render_turn_id: str
    cwd: str
    repository_id: str
    product_id: str
    created_at: str
    expires_at: str
    chosen_scope: CaptureScope | None
    client_action_id: str | None
    central_request_id: str | None
```

- `ControlBindingStore.open(path: Path) -> ControlBindingStore`
- `ControlBindingStore.create_binding(*, session_id: str, render_turn_id: str,
  cwd: str, repository_id: str, product_id: str, created_at: datetime,
  expires_at: datetime, control_id: str) -> ControlBinding`
- `ControlBindingStore.choose_scope(control_id: str, *,
  expected_repository_id: str, scope: CaptureScope,
  proposed_client_action_id: str, now: datetime) -> ControlBinding`
- `ControlBindingStore.attach_request(control_id: str, *,
  client_action_id: str, central_request_id: str) -> ControlBinding`
- `ControlBindingStore.get(control_id: str) -> ControlBinding | None`
- `ControlBindingStore.get_by_client_action_id(client_action_id: str) ->
  ControlBinding | None`
- `ControlBindingStore.close() -> None`

- [ ] **Step 1: Write failing Control Binding durability tests**

  Prove these invariants:

  ```python
  first = store.choose_scope(
      control.control_id,
      expected_repository_id=REPOSITORY_ID,
      scope="current_session",
      proposed_client_action_id="codex_action_first",
      now=NOW,
  )
  replay = store.choose_scope(
      control.control_id,
      expected_repository_id=REPOSITORY_ID,
      scope="current_session",
      proposed_client_action_id="codex_action_ignored",
      now=NOW + timedelta(minutes=30),
  )
  self.assertEqual(first.client_action_id, replay.client_action_id)

  with self.assertRaises(ControlScopeConflict):
      store.choose_scope(
          control.control_id,
          expected_repository_id=REPOSITORY_ID,
          scope="all_valid_sessions",
          proposed_client_action_id="codex_action_second",
          now=NOW,
      )
  ```

  Also test restart persistence, concurrent double-click behavior, expired
  unused control rejection, selected-control replay after initial expiry,
  fabricated ID rejection, repository mismatch, and conflicting request
  attachment. Scan SQLite bytes for a sentinel supplied as discarded Prompt,
  diff, tool input, and transcript path and prove it is absent.

- [ ] **Step 2: Write failing config-locator tests**

  The locator must contain exactly:

  ```json
  {"agent_config_path":"/absolute/owner-only/agent.json"}
  ```

  Test absolute path, current owner, `0600`, canonical JSON, atomic replacement,
  and target-config permission revalidation. Reject relative, malformed,
  group/world-readable, non-owner, and non-file paths. Assert the raw device
  token does not occur in the locator, Plugin manifest, MCP arguments, or
  command output.

- [ ] **Step 3: Run tests and verify RED**

  ```bash
  .venv/bin/python -m unittest \
    tests.test_control_bindings \
    tests.test_agent_config_locator \
    tests.test_agent_service \
    tests.test_plugin_contract -v
  ```

- [ ] **Step 4: Implement the Control Binding store**

  Use a separate SQLite connection to the existing private Agent database:

  ```sql
  CREATE TABLE IF NOT EXISTS control_bindings (
      control_id TEXT PRIMARY KEY,
      session_id TEXT NOT NULL,
      render_turn_id TEXT NOT NULL,
      cwd TEXT NOT NULL,
      repository_id TEXT NOT NULL,
      product_id TEXT NOT NULL,
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      chosen_scope TEXT CHECK(
          chosen_scope IS NULL OR
          chosen_scope IN ('current_session', 'all_valid_sessions')
      ),
      client_action_id TEXT UNIQUE,
      central_request_id TEXT UNIQUE,
      CHECK(
          (chosen_scope IS NULL AND client_action_id IS NULL) OR
          (chosen_scope IS NOT NULL AND client_action_id IS NOT NULL)
      )
  );
  ```

  Validate `ctl_` plus 32 lowercase hex for controls and a distinct
  `codex_action_` safe identifier for actions. `choose_scope()` uses
  `BEGIN IMMEDIATE`; it commits the first scope/action pair before returning,
  returns the stored action for same-scope replay, and raises on a different
  scope. Only an unselected control is rejected after its 15-minute expiry.

- [ ] **Step 5: Implement the owner-only config locator**

  Use `atomic_write_json()` so the temporary file and final locator remain
  `0600`. `service install` and `service run` publish the validated absolute
  config path after `load_agent_config()` succeeds. `mcp` loads the locator
  from the fixed private state location; `.mcp.json` remains exactly
  `["mcp"]`, and the locator never contains the token itself.

- [ ] **Step 6: Verify GREEN and commit**

  ```bash
  .venv/bin/python -m unittest \
    tests.test_control_bindings \
    tests.test_agent_config_locator \
    tests.test_agent_service \
    tests.test_plugin_contract -v
  git add src/zdecision/agent/control_bindings.py \
    src/zdecision/agent/config_locator.py \
    src/zdecision/agent/cli.py \
    src/zdecision/agent/service.py \
    tests/test_control_bindings.py \
    tests/test_agent_config_locator.py \
    tests/test_agent_service.py \
    tests/test_plugin_contract.py
  git commit -m "feat: persist trusted candidate refresh controls"
  ```

---

### Task 4: Bind the render tool through the trusted `PreToolUse` Hook

**Files:**

- Modify: `plugins/zdecision/hooks/hooks.json`
- Modify: `src/zdecision/agent/hooks.py`
- Test: `tests/test_control_binding_hook.py`
- Test: `tests/test_event_ledger.py`
- Test: `tests/test_hook_latency.py`
- Test: `tests/test_plugin_contract.py`

**Interface:** `handle_control_binding_hook(value: Mapping[str, object], *,
database: AgentDatabase, clock: Callable[[], datetime | str],
repository_resolver: RepositoryResolver | None = None, control_store:
ControlBindingStore | None = None, control_id_factory: Callable[[], str] | None
= None) -> HookResponse`

- [ ] **Step 1: Write failing trusted-envelope tests**

  A valid host envelope:

  ```python
  response = handle_hook(
      {
          "hook_event_name": "PreToolUse",
          "session_id": "session_a",
          "turn_id": "turn_a",
          "cwd": str(self.repository),
          "tool_name": (
              "mcp__zdecision_local__show_zdecision_update"
          ),
          "tool_input": {
              "control_id": "ctl_ffffffffffffffffffffffffffffffff"
          },
      },
      database=self.database,
      clock=lambda: NOW,
      repository_resolver=self.repository_resolver,
      worker_waker=lambda _: self.fail("must not wake worker"),
  )
  ```

  must produce only:

  ```python
  {
      "hookSpecificOutput": {
          "hookEventName": "PreToolUse",
          "permissionDecision": "allow",
          "updatedInput": {
              "control_id": "ctl_0123456789abcdef0123456789abcdef"
          },
      }
  }
  ```

  Inject a deterministic factory in the test. Assert the model-supplied ID and
  all raw `tool_input` fields are discarded. For unresolved, unregistered,
  disabled, subagent (`agent_id` present), malformed, or persistence-failure
  input, assert the Hook still allows the render call but replaces all input:

  ```python
  {
      "hookSpecificOutput": {
          "hookEventName": "PreToolUse",
          "permissionDecision": "allow",
          "updatedInput": {},
      }
  }
  ```

  Assert `AgentDatabase.count_events()` stays unchanged and the worker is never
  woken.

- [ ] **Step 2: Update the Plugin Hook contract test**

  Keep the five existing lifecycle Hooks and add exactly one `PreToolUse`
  matcher for the canonical ZDecision render tool. It must still invoke
  `zdecision-agent hook`, have a three-second maximum, and must not include
  `additionalContextLimit`.

- [ ] **Step 3: Run tests and verify RED**

  ```bash
  .venv/bin/python -m unittest \
    tests.test_control_binding_hook \
    tests.test_event_ledger \
    tests.test_hook_latency \
    tests.test_plugin_contract -v
  ```

- [ ] **Step 4: Implement a separate PreToolUse path**

  Decode once in `handle_hook()`. Dispatch `PreToolUse` before
  `HookInvocation.from_dict()` so it never enters the lifecycle event ledger.
  Validate exact tool name, safe Session/Turn identifiers, absolute `cwd`, no
  `agent_id`, normalized Git repository, and enabled local mapping. Create a
  random `ctl_` plus 32-hex binding expiring in 15 minutes.

  Return a complete `updatedInput` replacement in both success and disabled
  paths. Do not return a reason string. Keep the existing five lifecycle paths
  byte-for-byte compatible.

- [ ] **Step 5: Verify no latency or privacy regression and commit**

  ```bash
  .venv/bin/python -m unittest \
    tests.test_control_binding_hook \
    tests.test_event_ledger \
    tests.test_hook_latency \
    tests.test_plugin_contract -v
  git add plugins/zdecision/hooks/hooks.json \
    src/zdecision/agent/hooks.py \
    tests/test_control_binding_hook.py \
    tests/test_event_ledger.py \
    tests/test_hook_latency.py \
    tests/test_plugin_contract.py
  git commit -m "feat: bind inline refresh to the current Codex session"
  ```

---

### Task 5: Replace the probe with the real two-button MCP Apps card

**Files:**

- Create: `src/zdecision/agent/static/update-candidates-v1.html`
- Delete: `src/zdecision/agent/static/update-probe-v1.html`
- Modify: `src/zdecision/agent/mcp_server.py`
- Delete: `tests/test_mcp_ui_probe.py`
- Create: `tests/test_mcp_inline_refresh.py`
- Modify: `src/zdecision/agent/cli.py`
- Test: `tests/test_agent_config_locator.py`

**MCP tools:**

- `show_zdecision_update(control_id: str | None = None) -> CallToolResult`
- `start_zdecision_candidate_refresh(control_id: str, scope: CaptureScope) ->
  dict[str, object]`
- `get_zdecision_candidate_refresh(control_id: str) -> dict[str, object]`

- [ ] **Step 1: Write failing MCP contract tests**

  Assert:

  - resource URI is `ui://zdecision/update-candidates-v1.html`;
  - MIME is `text/html;profile=mcp-app`;
  - render visibility is `["model", "app"]` and read-only;
  - start/status visibility is `["app"]`;
  - start is non-destructive, idempotent, and not read-only;
  - status is read-only and idempotent;
  - the deleted acknowledgement probe no longer exists;
  - no binding returns `actions_enabled: false` with no reason;
  - valid render exposes `control_id` only in result `_meta`, not model-visible
    content or `structuredContent`;
  - a fabricated, expired, or cross-repository control is rejected;
  - local scope/action is persisted before the fake network client is called;
  - same-scope replay uses one action and one request;
  - scope conflict creates no request;
  - lost response replay adopts the central request with the same action ID;
  - `repository_capture_busy` is returned as safe state `busy`, without
    attaching the unrelated request; and
  - status returns only `safe_state`, optional count, and optional safe page
    URL.

- [ ] **Step 2: Write failing widget contract tests**

  Read the HTML resource and assert it contains:

  ```text
  当前 Session
  所有有效 Session
  ui/initialize
  ui/notifications/initialized
  ui/notifications/tool-input
  ui/notifications/tool-result
  tools/call
  start_zdecision_candidate_refresh
  get_zdecision_candidate_refresh
  ui/open-link
  ```

  Assert it does not contain `window.openai`, `Candidate` payload rendering,
  Session/Turn labels, local paths, or the old probe text.

- [ ] **Step 3: Run tests and verify RED**

  ```bash
  .venv/bin/python -m unittest \
    tests.test_mcp_inline_refresh \
    tests.test_agent_config_locator -v
  ```

- [ ] **Step 4: Build the MCP domain methods**

  `run_mcp()` loads the owner-only locator, existing Agent config, private
  `ControlBindingStore`, and authenticated `CentralClient`. If config is
  unavailable, the MCP server still starts and renders a disabled card.

  For render, use `CallToolResult` so the model does not see the control:

  ```python
  CallToolResult(
      content=[
          TextContent(
              type="text",
              text="ZDecision Candidate refresh control is ready.",
          )
      ],
      structuredContent={
          "actions_enabled": binding is not None,
          "safe_state": "ready" if binding is not None else "disabled",
      },
      _meta=(
          {"zdecision/control_id": binding.control_id}
          if binding is not None
          else {}
      ),
  )
  ```

  For start:

  1. validate the private binding and current repository;
  2. atomically choose scope and persist an independent
     `codex_action_` identifier;
  3. if a request is already attached, read it;
  4. otherwise POST the safe four-field command;
  5. attach the returned request ID with conflict checking; and
  6. map only allowlisted central state/progress codes to safe card state.

  Construct the Candidate-page URL locally from the validated central base URL
  and encoded repository ID. Do not accept a page URL from widget input.
  Map request state and progress explicitly: queued/claimed becomes `queued`;
  capture/reconciliation progress becomes `capturing`; upload progress becomes
  `syncing`; zero-count success becomes `empty`; positive-count success becomes
  `succeeded`; retryable/terminal failures become `failed`.

- [ ] **Step 5: Implement the card state machine**

  The widget initializes the portable bridge, reads the initial tool result
  `_meta`, and enables both buttons only when a control exists. First click
  immediately disables both buttons and calls start with the chosen scope.
  Poll status every 1500 ms only while the safe state is active. Stop on
  success, empty success, busy, or failure.

  Map only these bounded labels:

  ```javascript
  const labels = {
    creating: "正在创建更新请求",
    queued: "等待本地设备",
    capturing: "正在整理候选决策",
    syncing: "正在同步候选决策",
    busy: "已有更新正在进行",
    unavailable: "暂时无法更新",
    failed: "本次更新未完成",
    empty: "没有发现新的候选决策"
  };
  ```

  A positive terminal count renders `本次同步 N 条候选决策`. Successful state
  renders **打开候选决策页面** and uses `ui/open-link` only when the host
  advertises link support.

- [ ] **Step 6: Verify focused behavior and commit**

  ```bash
  .venv/bin/python -m unittest \
    tests.test_mcp_inline_refresh \
    tests.test_central_client \
    tests.test_agent_config_locator \
    tests.test_plugin_contract -v
  git add src/zdecision/agent/static/update-candidates-v1.html \
    src/zdecision/agent/static/update-probe-v1.html \
    src/zdecision/agent/mcp_server.py \
    src/zdecision/agent/cli.py \
    tests/test_mcp_ui_probe.py \
    tests/test_mcp_inline_refresh.py \
    tests/test_agent_config_locator.py
  git commit -m "feat: add inline candidate refresh controls"
  ```

---

### Task 6: Freeze the trusted current Session or all remaining valid Sessions

**Files:**

- Modify: `src/zdecision/agent/session_index.py`
- Modify: `src/zdecision/agent/capture_processor.py`
- Modify: `src/zdecision/agent/service.py`
- Test: `tests/test_session_index.py`
- Test: `tests/test_capture_request_processor.py`
- Test: `tests/test_agent_service.py`

**Interface:** `SessionIndex.freeze_sources(request_id: str, repository_id:
str, frozen_at: datetime, *, capture_scope: CaptureScope,
selected_session_id: str | None = None) -> tuple[FrozenSessionSource, ...]`

- [ ] **Step 1: Write failing scope and replay tests**

  With changed Sessions A and B:

  ```python
  current = self.index.freeze_sources(
      REQUEST_A,
      REPOSITORY_ID,
      NOW,
      capture_scope="current_session",
      selected_session_id="session_a",
  )
  self.assertEqual(["session_a"], [item.session_id for item in current])

  self.index.acknowledge(REQUEST_A, BATCH_DIGEST, NOW)
  remaining = self.index.freeze_sources(
      REQUEST_B,
      REPOSITORY_ID,
      NOW,
      capture_scope="all_valid_sessions",
  )
  self.assertEqual(["session_b"], [item.session_id for item in remaining])
  ```

  Also prove:

  - current scope requires a selected Session;
  - all-valid scope rejects a selected Session;
  - the current Session's latest lineage checkpoint is selected once;
  - current scope returns empty when only another Session changed;
  - activity after freeze waits for a later request;
  - replay with a different repository/scope/Session conflicts;
  - unchanged acknowledged sources stay excluded; and
  - subagent sources retain the existing final exclusion behavior.

- [ ] **Step 2: Write failing processor fail-closed tests**

  For `current_session`, assert missing action binding, repository mismatch, or
  scope mismatch raises `TerminalCaptureRequestError` with
  `current_session_intent_missing` or `current_session_intent_mismatch` before
  any Capture model work. Assert all-valid page requests need no private
  binding and retain current behavior.

- [ ] **Step 3: Run tests and verify RED**

  ```bash
  .venv/bin/python -m unittest \
    tests.test_session_index \
    tests.test_capture_request_processor \
    tests.test_agent_service -v
  ```

- [ ] **Step 4: Migrate freeze identity and implement selection**

  Add `capture_scope` and nullable `selected_session_id` to
  `capture_request_freezes`; migrate old rows to
  `all_valid_sessions`/null. On replay, compare repository, scope, and selected
  Session before returning frozen sources.

  For current scope, select the bound Session's newest changed checkpoint by
  `(latest_observed_at DESC, latest_event_id DESC)` with `LIMIT 1`. For
  all-valid scope, preserve the existing repository query. Keep acknowledgement
  and post-freeze activity semantics unchanged.

- [ ] **Step 5: Resolve private intent before Capture**

  Inject `ControlBindingStore` into `OnDemandCaptureProcessor`. Before
  `client.start()` or source freeze:

  ```python
  selected_session_id = None
  if request.capture_scope == "current_session":
      binding = self.control_store.get_by_client_action_id(
          request.client_action_id
      )
      if binding is None:
          raise TerminalCaptureRequestError(
              "current_session_intent_missing"
          )
      if (
          binding.repository_id != request.repository_id
          or binding.chosen_scope != request.capture_scope
      ):
          raise TerminalCaptureRequestError(
              "current_session_intent_mismatch"
          )
      selected_session_id = binding.session_id
  ```

  Call the new freeze interface with the resolved Session. The page's
  `all_valid_sessions` path never looks up a control. Wire and close the store
  through `configured_processor()` with the other local stores.

- [ ] **Step 6: Verify GREEN and commit**

  ```bash
  .venv/bin/python -m unittest \
    tests.test_session_index \
    tests.test_capture_request_processor \
    tests.test_agent_service -v
  git add src/zdecision/agent/session_index.py \
    src/zdecision/agent/capture_processor.py \
    src/zdecision/agent/service.py \
    tests/test_session_index.py \
    tests/test_capture_request_processor.py \
    tests/test_agent_service.py
  git commit -m "feat: honor candidate refresh source scope"
  ```

---

### Task 7: Present the card at the approved boundary and prove the full slice

**Files:**

- Modify: `plugins/zdecision/skills/zdecision/SKILL.md`
- Modify: `plugins/zdecision/skills/zdecision/agents/openai.yaml`
- Modify: `plugins/zdecision/.codex-plugin/plugin.json`
- Modify: `tests/test_plugin_contract.py`
- Modify: `tests/integration/test_on_demand_capture_core.py`
- Create: `tests/integration/test_inline_candidate_refresh.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing Plugin Skill contract tests**

  Assert the packaged Plugin Skill says:

  - after a normal code-development task reaches a completed and verified
    boundary in an enabled repository, render `show_zdecision_update` once;
  - an explicit same-task phrase **更新候选决策** renders it immediately;
  - rendering is not Capture authorization;
  - Session start, intermediate Turns, and non-code work do not proactively
    render it;
  - duplicate render has no domain side effect;
  - the user is never asked for a Session ID or capture CLI; and
  - Review/publication remain on the central page.

  Keep `allow_implicit_invocation: true`. Update the Plugin's default prompt to
  `更新候选决策`.

- [ ] **Step 2: Add a deterministic end-to-end integration test**

  Build two changed interactive Sessions plus one subagent Session in one
  enabled repository. Drive the real local Hook/control, MCP action, central
  Plugin endpoint, Agent claim, existing two-stage Capture/reconciliation,
  upload, completion, and status read.

  Assert:

  1. Session A's current-scope request reaches success and advances only A;
  2. Session B remains changed;
  3. a later all-valid request processes B but not acknowledged A;
  4. the subagent is not uploaded as a source;
  5. each terminal count equals that request's acknowledged batch item count;
  6. exact action replay creates no duplicate request or Candidate revision;
  7. a simultaneous different action receives busy; and
  8. Candidate revisions appear on the central page API, not in MCP output.

  Scan central database cells, recorded HTTP bodies, progress events, and MCP
  structured output for sentinel Session IDs, Turn IDs, `cwd`, Prompt, source,
  diff, tool output, and control ID. Every sentinel must be absent.

- [ ] **Step 3: Run the focused vertical-slice suite**

  ```bash
  .venv/bin/python -m unittest \
    tests.test_control_bindings \
    tests.test_agent_config_locator \
    tests.test_control_binding_hook \
    tests.test_mcp_inline_refresh \
    tests.test_session_index \
    tests.test_capture_request_processor \
    tests.test_sync_contracts \
    tests.test_central_requests \
    tests.test_central_api \
    tests.test_central_client \
    tests.test_plugin_contract \
    tests.test_update_candidates_page \
    tests.integration.test_inline_candidate_refresh \
    tests.integration.test_on_demand_capture_core -v
  ```

  Expected: all focused tests pass. Fix only failures caused by this slice.

- [ ] **Step 4: Run the complete suite and package inspection once**

  ```bash
  .venv/bin/python -m unittest discover -s tests -v
  build_dir="$(mktemp -d)"
  .venv/bin/python -m build --outdir "$build_dir"
  .venv/bin/python -c \
    'import sys,zipfile; p=sys.argv[1]; z=zipfile.ZipFile(p); names=z.namelist(); assert any(n.endswith("zdecision/agent/static/update-candidates-v1.html") for n in names); assert not any(n.endswith("update-probe-v1.html") for n in names)' \
    "$(find "$build_dir" -name '*.whl' -print -quit)"
  git diff --check
  ```

  Expected: complete suite passes; wheel contains only the real versioned
  widget; diff check is clean.

- [ ] **Step 5: Commit the completed automated slice**

  ```bash
  git add plugins/zdecision/skills/zdecision/SKILL.md \
    plugins/zdecision/skills/zdecision/agents/openai.yaml \
    plugins/zdecision/.codex-plugin/plugin.json \
    tests/test_plugin_contract.py \
    tests/integration/test_on_demand_capture_core.py \
    tests/integration/test_inline_candidate_refresh.py \
    README.md
  git commit -m "feat: complete inline candidate refresh flow"
  ```

- [ ] **Step 6: Perform one real Codex Desktop acceptance**

  1. Reinstall or refresh the local package and Plugin, then restart Codex once.
  2. Run `zdecision-agent service install --config` with the existing
     owner-only Demo config so the runtime locator is present.
  3. In the same enabled Git repository, open two ordinary Codex tasks and
     complete one verified code boundary in each.
  4. In task A, render the card and click **当前 Session**.
  5. Observe queued/running/terminal state and the request's revision count.
  6. Verify only task A's checkpoint advanced.
  7. Render another card and click **所有有效 Session**.
  8. Verify task B is processed and task A is not repeated.
  9. Click **打开候选决策页面** and see the corresponding current Candidate
     revisions.
  10. Inspect central records for the privacy sentinel set.

  Pass only if no Session ID is supplied by the user, each button selects the
  correct scope, the card shows no Candidate content, and no private local
  source identity reaches central storage.

  If one blocking defect appears, make one focused correction, rerun the
  directly affected tests plus the complete suite, and repeat this acceptance
  once. Record non-blocking improvements for later work and stop.
