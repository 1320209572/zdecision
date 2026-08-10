# Recall MCP App Host-Capability Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, install, run once, and remove an isolated Codex Plugin that proves the current Desktop host's app-only tool, authoritative recovery, model-context update, and follow-up-message behavior without touching production Recall.

**Architecture:** A temporary `zdecision-host-probe` Plugin starts a dedicated FastMCP stdio server through a lazy `zdecision-agent host-probe-mcp` command. The server owns one separate SQLite file, one inline MCP App resource, one model-visible render tool, and two app-only tools. The card executes the exact standards-first sequence `tools/call -> ui/update-model-context -> ui/message`; a single real Desktop run produces a bounded report, after which the temporary Plugin, cache, private database, and diagnostic source are removed.

**Tech Stack:** Python 3.14.4, standard-library `sqlite3`, `secrets`, SHA-256 and UTC timestamps, `mcp==1.29.0` FastMCP, static HTML/CSS/JavaScript using MCP Apps protocol `2026-01-26`, `unittest`, Codex CLI `0.147.0`, Codex Desktop `26.803.41515` build `6321`.

## Global Constraints

- Work directly on the existing `main` branch. Do not create a worktree or another Registry branch.
- The approved authority is `docs/superpowers/specs/2026-08-10-recall-mcp-app-host-capability-probe-design.md`.
- The diagnostic Plugin identity is exactly `zdecision-host-probe`; its MCP server name is exactly `zdecision-host-probe`.
- Do not modify `plugins/zdecision/`, its installed cache, its manifest version, its Skills, its Hooks, or its MCP server map.
- The temporary Plugin contains no Skill, Hook, app-server client, Central client, repository resolver, Capture path, Candidate path, or Decision provider.
- The diagnostic store is a separate owner-readable SQLite file at `private_state_root(environ) / "host-probe" / "zdecision-host-probe.sqlite3"`; it never opens the production `agent/zdecision.sqlite3`.
- Persist only bounded random probe coordinates, state, timestamps, version, and expiry. Never persist or emit Prompt, transcript, rollout, source, diff, tool output, PRD, repository, product, Session, Turn, Decision, Candidate, credentials, or local business paths.
- The render tool has no model-authored input. `run_zdecision_recall_host_probe` and `get_zdecision_recall_host_probe` have exact app-only visibility.
- `probe_id` and `marker` are app-private `_meta` values. The marker must be absent from model-visible tool content and the `ui/message` payload.
- A mutating response timeout is an unknown result. Do not retry the action automatically. Remount may issue one read-only recovery call.
- Capability truth requires both advertisement and one real request. Read exact paths `hostCapabilities.serverTools`, `hostCapabilities.updateModelContext.text`, and `hostCapabilities.message.text`.
- Await one complete `ui/update-model-context` request before sending one bounded `ui/message`. If context update fails, do not send the message. If message result is unknown, do not repeat it.
- The real acceptance runs once and ends with `PASS`, `PARTIAL`, or `FAIL`. Do not rerun a failed mutation to obtain a preferred result.
- Preserve the user's unrelated untracked files `docs/superpowers/acceptance/2026-08-06-recall-host-gate.md` and `tests/integration/test_recall_host_gate.py`; never stage or edit them.
- Do not launch a new broad design review or Skill blind test. Each implementation task gets one RED, one GREEN, a local self-review, and one commit.

## File Map

- Create `src/zdecision/agent/host_capability_probe.py`: immutable probe record, separate SQLite store, validation, atomic commit, read-only recovery, expiry, and FastMCP tool result shaping.
- Create `src/zdecision/agent/host_capability_probe_mcp.py`: isolated FastMCP resource/tool registration and stdio runner.
- Create `src/zdecision/agent/static/recall-host-capability-probe-v1.html`: capability display, one explicit action, exact MCP Apps call ordering, terminal status, and remount recovery.
- Modify `src/zdecision/agent/cli.py`: lazy `host-probe-mcp` command and separate private database path; no production database open on that branch.
- Create `plugins/zdecision-host-probe/.codex-plugin/plugin.json`: temporary manifest with only the diagnostic MCP component.
- Create `plugins/zdecision-host-probe/.mcp.json`: `zdecision-agent host-probe-mcp` stdio server.
- Modify `.agents/plugins/marketplace.json`: append the temporary available Plugin during the probe, then remove it during cleanup.
- Create `tests/test_recall_host_capability_probe.py`: state-store, FastMCP visibility/result, privacy, and JavaScript bridge tests.
- Modify `tests/test_agent_config_locator.py`: prove the diagnostic command uses only the separate database path.
- Modify `tests/test_plugin_contract.py`: prove the second marketplace entry is isolated and the production Plugin remains byte-for-byte contract-compatible.
- Create `docs/superpowers/acceptance/2026-08-10-recall-mcp-app-host-capability-probe.md`: sanitized automated and real Desktop evidence.

---

### Task 1: Implement the isolated authoritative probe store

**Files:**
- Create: `src/zdecision/agent/host_capability_probe.py`
- Create: `tests/test_recall_host_capability_probe.py`

**Interfaces:**
- Consumes: a dedicated `Path`, UTC-aware clock, and injectable token source.
- Produces:

```python
PROBE_VERSION = 1
PROBE_TTL = timedelta(hours=24)

@dataclass(frozen=True)
class HostCapabilityProbe:
    probe_id: str
    probe_version: int
    state: Literal["ready", "committed", "failed", "expired"]
    marker: str
    receipt: str
    created_at: str
    committed_at: str | None
    expires_at: str
```

`HostCapabilityProbeStore` exposes exactly these signatures:

```text
open(path: Path, *, clock: Callable[[], datetime] | None = None,
     token: Callable[[], str] | None = None) -> HostCapabilityProbeStore
create() -> HostCapabilityProbe
commit(probe_id: str) -> HostCapabilityProbe | None
get(probe_id: str) -> HostCapabilityProbe | None
close() -> None
```

The store creates exactly one table:

```sql
CREATE TABLE IF NOT EXISTS recall_host_capability_probes (
    probe_id TEXT PRIMARY KEY,
    probe_version INTEGER NOT NULL CHECK(probe_version = 1),
    state TEXT NOT NULL CHECK(state IN ('ready', 'committed', 'failed', 'expired')),
    marker TEXT NOT NULL UNIQUE,
    receipt TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    committed_at TEXT,
    expires_at TEXT NOT NULL
)
```

- [ ] **Step 1: Write the failing state and privacy tests**

Add `HostCapabilityProbeStoreTests` with a temporary SQLite path and deterministic UTC clock/token source:

```python
def test_create_commit_and_replay_return_one_authoritative_receipt(self):
    created = self.store.create()
    self.assertEqual("ready", created.state)
    committed = self.store.commit(created.probe_id)
    replay = self.store.commit(created.probe_id)
    self.assertEqual("committed", committed.state)
    self.assertEqual(committed, replay)
    self.assertEqual(created.marker, committed.marker)
    self.assertEqual(created.receipt, committed.receipt)

def test_reopen_recovers_committed_probe(self):
    created = self.store.create()
    committed = self.store.commit(created.probe_id)
    self.store.close()
    reopened = HostCapabilityProbeStore.open(
        self.database_path, clock=lambda: NOW, token=self.tokens
    )
    self.assertEqual(committed, reopened.get(created.probe_id))

def test_unknown_malformed_and_expired_ids_do_not_commit(self):
    self.assertIsNone(self.store.commit("not-a-probe"))
    created = self.store.create()
    self.clock.now = datetime.fromisoformat(
        created.expires_at.replace("Z", "+00:00")
    ) + timedelta(seconds=1)
    self.assertEqual("expired", self.store.get(created.probe_id).state)
    self.assertIsNone(self.store.commit(created.probe_id))

def test_store_bytes_exclude_business_sentinels(self):
    self.store.create()
    serialized = self.database_path.read_bytes().lower()
    for sentinel in (
        b"private_prompt_sentinel",
        b"private_transcript_sentinel",
        b"private_decision_sentinel",
        b"private_repository_sentinel",
    ):
        self.assertNotIn(sentinel, serialized)
```

Also test concurrent commits through two store connections, invalid/non-UTC clocks, duplicate token generation, closed-store use, parent directory mode, exact ID/marker/receipt length bounds, and `committed_at` remaining unchanged on replay.

- [ ] **Step 2: Run the store test and confirm RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_host_capability_probe.HostCapabilityProbeStoreTests -v
```

Expected: import failure because `zdecision.agent.host_capability_probe` does not exist. This is the required RED.

- [ ] **Step 3: Implement the minimal store**

Implement the exact interfaces above. Use `secrets.token_urlsafe(24)` by default, `BEGIN IMMEDIATE` around the first `ready -> committed` transition, five-second SQLite busy timeout, UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ` serialization, and `chmod 0o600` after creation. Generate bounded values as:

```python
probe_id = f"probe_{token()}"
marker = f"ZDECISION_HOST_PROBE_{token()}"
receipt = f"receipt_{token()}"
```

`get()` atomically changes an expired `ready` record to `expired`. `commit()` returns `None` for unknown, malformed, or expired IDs; it returns the same immutable committed record for a replay.

- [ ] **Step 4: Run the store test and confirm GREEN**

Run the Step 2 command again.

Expected: all `HostCapabilityProbeStoreTests` pass with no warning or leaked SQLite connection.

- [ ] **Step 5: Commit Task 1**

```bash
git add \
  src/zdecision/agent/host_capability_probe.py \
  tests/test_recall_host_capability_probe.py
git commit -m "test: add isolated Recall host probe state"
```

---

### Task 2: Add the isolated MCP tools and standards-first card

**Files:**
- Create: `src/zdecision/agent/host_capability_probe_mcp.py`
- Create: `src/zdecision/agent/static/recall-host-capability-probe-v1.html`
- Modify: `tests/test_recall_host_capability_probe.py`

**Interfaces:**
- Consumes: `HostCapabilityProbeStore` from Task 1.
- Produces:

```python
HOST_PROBE_URI = "ui://zdecision/recall-host-capability-probe-v1.html"
HOST_PROBE_MIME_TYPE = "text/html;profile=mcp-app"
```

The module exposes exactly:

```text
create_host_probe_mcp_server(store: HostCapabilityProbeStore) -> FastMCP
run_host_probe_mcp(*, database_path: Path) -> None
```

The test module defines one real in-memory protocol helper:

```text
call_tool(server: FastMCP, name: str,
          arguments: dict[str, object]) -> CallToolResult
```

The server registers exactly:

```text
show_zdecision_recall_host_probe   visibility = [model, app], no input
run_zdecision_recall_host_probe    visibility = [app], probe_id only
get_zdecision_recall_host_probe    visibility = [app], probe_id only
```

Tool-result boundaries are exact:

```python
# Render
structuredContent = {"probe_version": 1, "state": "ready"}
_meta = {"zdecision/probe_id": probe.probe_id}

# Run/get committed snapshot
structuredContent = {
    "probe_version": 1,
    "state": "committed",
    "receipt": probe.receipt,
    "committed_at": probe.committed_at,
}
_meta = {
    "zdecision/probe_id": probe.probe_id,
    "zdecision/probe_marker": probe.marker,
}

# Unknown/malformed/expired input
structuredContent = {
    "probe_version": 1,
    "state": "failed",
    "code": "invalid_probe",
}
_meta = {}
isError = True
```

- [ ] **Step 1: Write failing MCP visibility and result tests**

Add asynchronous `IsolatedHostProbeMcpTests`:

```python
async def test_server_registers_one_resource_and_exact_tool_visibility(self):
    server = create_host_probe_mcp_server(self.store)
    resources = await server.list_resources()
    tools = {tool.name: tool for tool in await server.list_tools()}
    self.assertEqual([HOST_PROBE_URI], [str(item.uri) for item in resources])
    self.assertEqual(
        {
            "show_zdecision_recall_host_probe",
            "run_zdecision_recall_host_probe",
            "get_zdecision_recall_host_probe",
        },
        set(tools),
    )
    self.assertEqual(
        ["model", "app"],
        tools["show_zdecision_recall_host_probe"].meta["ui"]["visibility"],
    )
    for name in (
        "run_zdecision_recall_host_probe",
        "get_zdecision_recall_host_probe",
    ):
        self.assertEqual(["app"], tools[name].meta["ui"]["visibility"])

async def test_marker_is_app_private_and_commit_replays_same_receipt(self):
    render = await call_tool(self.server, "show_zdecision_recall_host_probe", {})
    probe_id = render._meta["zdecision/probe_id"]
    self.assertNotIn("ZDECISION_HOST_PROBE_", json.dumps(render.structuredContent))
    first = await call_tool(
        self.server, "run_zdecision_recall_host_probe", {"probe_id": probe_id}
    )
    second = await call_tool(
        self.server, "run_zdecision_recall_host_probe", {"probe_id": probe_id}
    )
    self.assertEqual(first.structuredContent, second.structuredContent)
    self.assertEqual(first._meta, second._meta)
    self.assertNotIn(first._meta["zdecision/probe_marker"], first.content[0].text)
```

Also prove empty render schema, closed-world action schemas, exact annotations, blocked/unknown result shape, and that no tool result contains a Session, repository, product, Prompt, Decision, or local path key.

- [ ] **Step 2: Write failing card protocol tests**

Add `HostProbeCardProtocolTests` with a local JavaScript harness that records outbound JSON-RPC messages, resolves requests by ID, exposes deterministic timers, and delivers the render tool-result notification. Fix its public helpers to:

```javascript
mount({ hostCapabilities, renderResult })
outbound(method)
respond(request, result)
reject(request, error)
clickRun()
remount({ hostCapabilities, renderResult })
```

The required assertions are:

```javascript
const probeId = "probe_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const marker = "ZDECISION_HOST_PROBE_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const readyResult = result({ probe_version: 1, state: "ready" }, {
  "zdecision/probe_id": probeId,
});
const committedResult = result({
  probe_version: 1,
  state: "committed",
  receipt: "receipt_cccccccccccccccccccccccccccccccc",
  committed_at: "2026-08-10T00:00:00.000000Z",
}, {
  "zdecision/probe_id": probeId,
  "zdecision/probe_marker": marker,
});

const widget = await mount({
  hostCapabilities: {
    serverTools: {},
    updateModelContext: { text: {} },
    message: { text: {} },
  },
  renderResult: readyResult,
});
check(outbound("tools/call").length === 1, "mount must make only read-only get");
check(
  outbound("tools/call")[0].params.name === "get_zdecision_recall_host_probe",
  "mount used a mutating tool",
);
respond(outbound("tools/call")[0], readyResult);

const click = widget.clickRun();
const run = outbound("tools/call").at(-1);
check(run.params.name === "run_zdecision_recall_host_probe", "wrong action");
respond(run, committedResult);
await flush();
const update = outbound("ui/update-model-context")[0];
check(update.params.content.length === 1, "context was fragmented");
check(update.params.content[0].text.includes(marker), "marker not staged");
respond(update, {});
await flush();
const message = outbound("ui/message")[0];
check(message.params.role === "user", "wrong message role");
check(Array.isArray(message.params.content), "message content is not an array");
check(!message.params.content[0].text.includes(marker), "message leaked marker");
respond(message, { isError: false });
await click;
```

Add separate cases for:

- `serverTools` absent: no tool call and visible `unsupported`;
- malformed capability values such as `serverTools: true` or
  `updateModelContext: {text: true}`: treated as unsupported;
- `updateModelContext.text` absent: action commits, no context/message, visible `partial`;
- `message.text` absent: context succeeds, no message, visible `partial`;
- context error/timeout: no message and no retry;
- message `{isError: true}`/timeout: no retry and visible `message_failed`;
- action timeout: no second mutation;
- duplicate click: one mutation;
- remount: one read-only get, restored receipt, no action/context/message; and
- mismatched probe ID/receipt/marker: terminal `failed`.

- [ ] **Step 3: Run both new test classes and confirm RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_host_capability_probe.IsolatedHostProbeMcpTests \
  tests.test_recall_host_capability_probe.HostProbeCardProtocolTests -v
```

Expected: import/resource failures because the MCP module and HTML resource do not exist.

- [ ] **Step 4: Implement the minimal server and card**

Create the isolated FastMCP server without importing `mcp_server.py`, `RecallMcpTools`, `AgentDatabase`, or any Central module. Use accurate annotations:

```python
render_annotations = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
action_annotations = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
recovery_annotations = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
```

The static card must:

1. send `ui/initialize` with protocol `2026-01-26`;
2. read only `result.hostCapabilities`;
3. send `ui/notifications/initialized`;
4. accept the app-private probe ID from `ui/notifications/tool-result`;
5. issue one read-only `get` after initialize and render are both ready;
6. enable one **运行宿主能力验证** button only after a valid `ready` snapshot;
7. on click, call the app-only mutation once;
8. after committed result, call one complete `ui/update-model-context` request;
9. only after `{}` success, call one array-shaped `ui/message` request;
10. never retry a mutating or message request; and
11. show advertisement and actual-call outcomes separately.

- [ ] **Step 5: Run Task 2 tests and confirm GREEN**

Run the Step 3 command again, then:

```bash
.venv/bin/python -m unittest tests.test_recall_host_capability_probe -v
.venv/bin/python -m compileall -q \
  src/zdecision/agent/host_capability_probe.py \
  src/zdecision/agent/host_capability_probe_mcp.py
```

Expected: all probe tests pass; compile exits 0; no ResourceWarning or pending timer remains.

- [ ] **Step 6: Commit Task 2**

```bash
git add \
  src/zdecision/agent/host_capability_probe_mcp.py \
  src/zdecision/agent/static/recall-host-capability-probe-v1.html \
  tests/test_recall_host_capability_probe.py
git commit -m "feat: add isolated Recall host capability card"
```

---

### Task 3: Package the temporary Plugin and lazy CLI boundary

**Files:**
- Modify: `src/zdecision/agent/cli.py`
- Modify: `tests/test_agent_config_locator.py`
- Create: `plugins/zdecision-host-probe/.codex-plugin/plugin.json`
- Create: `plugins/zdecision-host-probe/.mcp.json`
- Modify: `.agents/plugins/marketplace.json`
- Modify: `tests/test_plugin_contract.py`

**Interfaces:**
- Consumes: `run_host_probe_mcp(database_path=Path)` from Task 2.
- Produces:

```python
def host_probe_database_path(environ: Mapping[str, str]) -> Path:
    return (
        private_state_root(environ)
        / "host-probe"
        / "zdecision-host-probe.sqlite3"
    )
```

The lazy wrapper signature is:

```text
run_host_probe_mcp(**arguments: object) -> None
```

- [ ] **Step 1: Write failing CLI and packaging tests**

In `tests/test_agent_config_locator.py`, add:

```python
def test_host_probe_mcp_uses_separate_state_without_opening_agent_database(self):
    with (
        patch("zdecision.agent.cli.private_state_root", return_value=self.root),
        patch("zdecision.agent.cli.run_host_probe_mcp") as run_probe,
        patch("zdecision.agent.db.AgentDatabase.open") as open_agent,
    ):
        self.assertEqual(0, main(["host-probe-mcp"]))
    run_probe.assert_called_once_with(
        database_path=self.root / "host-probe" / "zdecision-host-probe.sqlite3"
    )
    open_agent.assert_not_called()
```

In `tests/test_plugin_contract.py`, keep all existing assertions for
`plugins/zdecision` and add exact temporary assertions:

```python
probe = next(
    item for item in marketplace["plugins"]
    if item["name"] == "zdecision-host-probe"
)
self.assertEqual(
    {"source": "local", "path": "./plugins/zdecision-host-probe"},
    probe["source"],
)
self.assertEqual(
    {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    probe["policy"],
)

manifest = load_json(PROBE_PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
self.assertEqual("zdecision-host-probe", manifest["name"])
self.assertEqual("./.mcp.json", manifest["mcpServers"])
self.assertNotIn("skills", manifest)
self.assertNotIn("hooks", manifest)

server = load_json(PROBE_PLUGIN_ROOT / ".mcp.json")["mcpServers"]
self.assertEqual({"zdecision-host-probe"}, set(server))
self.assertEqual("zdecision-agent", server["zdecision-host-probe"]["command"])
self.assertEqual(["host-probe-mcp"], server["zdecision-host-probe"]["args"])
```

Also assert that the production `zdecision` marketplace entry remains first and unchanged, and the temporary manifest/MCP JSON contains none of the privacy sentinel keys.

- [ ] **Step 2: Run packaging tests and confirm RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_agent_config_locator \
  tests.test_plugin_contract -v
```

Expected: missing `host-probe-mcp` command and missing Plugin/marketplace entry.

- [ ] **Step 3: Scaffold the separate Plugin with the required helper**

Run exactly:

```bash
python3 /Users/zhaohuiying/.codex/skills/.system/plugin-creator/scripts/create_basic_plugin.py \
  zdecision-host-probe \
  --path /Users/zhaohuiying/Desktop/Zstack-repos/zdecision/plugins \
  --marketplace-path /Users/zhaohuiying/Desktop/Zstack-repos/zdecision/.agents/plugins/marketplace.json \
  --with-mcp \
  --with-marketplace \
  --install-policy AVAILABLE \
  --auth-policy ON_INSTALL \
  --category Productivity
```

Then use `apply_patch` to set the manifest exactly to:

```json
{
  "name": "zdecision-host-probe",
  "version": "0.1.0+codex.host-probe-20260810",
  "description": "Disposable Codex Desktop MCP Apps host capability probe",
  "author": {"name": "ZDecision"},
  "mcpServers": "./.mcp.json",
  "interface": {
    "displayName": "ZDecision Host Probe",
    "shortDescription": "Temporary MCP Apps capability verification",
    "longDescription": "A disposable local probe for app-only tools, context handoff, follow-up messaging, and remount recovery. It reads no project or Decision data.",
    "developerName": "ZDecision",
    "category": "Developer Tools",
    "capabilities": ["Interactive"],
    "defaultPrompt": ["运行 ZDecision 宿主能力验证"]
  }
}
```

Set `.mcp.json` exactly to:

```json
{
  "mcpServers": {
    "zdecision-host-probe": {
      "command": "zdecision-agent",
      "args": ["host-probe-mcp"]
    }
  }
}
```

- [ ] **Step 4: Add the lazy CLI branch**

Add the parser command and dispatch before any `AgentDatabase.open()`:

```python
subparsers.add_parser(
    "host-probe-mcp",
    help="serve the disposable Recall MCP Apps host probe over stdio",
)

if arguments.command == "host-probe-mcp":
    run_host_probe_mcp(
        database_path=host_probe_database_path(os.environ),
    )
    return 0
```

`run_host_probe_mcp()` must import `zdecision.agent.host_capability_probe_mcp` lazily. Do not import the diagnostic module on `hook`, `mcp`, `worker`, `service`, `status`, or repository commands.

- [ ] **Step 5: Validate the Plugin and confirm GREEN**

Run:

```bash
uv run --with pyyaml python \
  /Users/zhaohuiying/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py \
  /Users/zhaohuiying/Desktop/Zstack-repos/zdecision/plugins/zdecision-host-probe

.venv/bin/python -m unittest \
  tests.test_recall_host_capability_probe \
  tests.test_agent_config_locator \
  tests.test_plugin_contract -v
```

Expected: validator exits 0 and all focused tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add \
  src/zdecision/agent/cli.py \
  tests/test_agent_config_locator.py \
  plugins/zdecision-host-probe \
  .agents/plugins/marketplace.json \
  tests/test_plugin_contract.py
git commit -m "feat: package disposable Recall host probe"
```

---

### Task 4: Verify, install, and execute the single Desktop acceptance

**Files:**
- Create: `docs/superpowers/acceptance/2026-08-10-recall-mcp-app-host-capability-probe.md`

**Interfaces:**
- Consumes: the installed `zdecision-host-probe@zdecision-local` Plugin and one user click.
- Produces: one sanitized `PASS`, `PARTIAL`, or `FAIL` evidence report.

- [ ] **Step 1: Run automated preflight once**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_host_capability_probe \
  tests.test_agent_config_locator \
  tests.test_plugin_contract \
  tests.test_mcp_recall_confirmation \
  tests.test_mcp_inline_refresh -v

.venv/bin/python -m compileall -q src/zdecision tests
git diff --check
```

Expected: all listed tests pass and the production Recall/Candidate suites show no changed behavior.

- [ ] **Step 2: Install only the temporary Plugin**

First prove the production Plugin source is unchanged:

```bash
git diff 19813a1 -- plugins/zdecision
```

Expected: no output.

Then install:

```bash
codex plugin add zdecision-host-probe@zdecision-local --json
codex plugin list
```

Expected: the JSON result reports the exact temporary selector installed; `zdecision@zdecision-local` remains installed and enabled with its prior version.

- [ ] **Step 3: Restart once and run the visible acceptance**

Ask the user to restart Codex and open a dedicated new task. In that task invoke `show_zdecision_recall_host_probe`, then perform exactly:

1. verify the card displays the three advertised capability observations;
2. click **运行宿主能力验证** once;
3. if the host presents **发送后续提示?**, click **发送** once;
4. verify the next assistant response repeats the marker without a ZDecision probe-read tool call;
5. switch to another task and back; and
6. verify the same committed receipt is restored and no second action/context/message is sent.

Stop after the first completed classification. Do not click the action again.

- [ ] **Step 4: Record the bounded result**

Create the acceptance report with exactly these sections:

```markdown
# Recall MCP App Host-Capability Probe

## Result

**PASS | PARTIAL | FAIL**

## Environment

- Codex Desktop version/build
- Codex CLI version
- Python and MCP SDK versions
- Git source commit
- temporary Plugin manifest digest

## Capability and operation matrix

| Capability | Advertised | Actual request | Outcome |
| --- | --- | --- | --- |
| `serverTools` | yes/no | `tools/call` | pass/fail |
| `updateModelContext.text` | yes/no | `ui/update-model-context` | pass/fail/not-run |
| `message.text` | yes/no | `ui/message` | direct/host_confirmed/unsupported/failed/not-run |
| authoritative recovery | n/a | app-only `get` after remount | pass/fail |

## Safety evidence

- one mutating action call
- one stable receipt
- no automatic retry
- no production ZDecision Plugin change
- no App Server/transcript/business-data access
- privacy scan result

## Route selected

- PASS -> standards-first `enable-and-recall`
- PARTIAL context -> bounded UserPromptSubmit handoff design
- PARTIAL message -> next-native-message UX
- FAIL -> stop Plugin-based Recall for this host
```

Record only capability booleans/categories, a redacted marker prefix, and receipt digest prefix. Do not copy raw tool results, private database rows, task text, or full marker/receipt.

- [ ] **Step 5: Commit the acceptance report**

```bash
git add docs/superpowers/acceptance/2026-08-10-recall-mcp-app-host-capability-probe.md
git commit -m "docs: record Recall host capability result"
```

---

### Task 5: Remove the temporary Plugin and diagnostic implementation

**Files:**
- Delete: `src/zdecision/agent/host_capability_probe.py`
- Delete: `src/zdecision/agent/host_capability_probe_mcp.py`
- Delete: `src/zdecision/agent/static/recall-host-capability-probe-v1.html`
- Modify: `src/zdecision/agent/cli.py`
- Delete: `plugins/zdecision-host-probe/`
- Modify: `.agents/plugins/marketplace.json`
- Delete: `tests/test_recall_host_capability_probe.py`
- Modify: `tests/test_agent_config_locator.py`
- Modify: `tests/test_plugin_contract.py`

**Interfaces:**
- Consumes: the committed acceptance report from Task 4.
- Produces: a clean production repository and Codex installation with the report retained.

- [ ] **Step 1: Remove the installed Plugin and verify absence**

```bash
codex plugin remove zdecision-host-probe@zdecision-local --json
codex plugin list
```

Expected: the temporary selector is absent; `zdecision@zdecision-local` remains installed and unchanged.

- [ ] **Step 2: Delete only the exact private probe database**

Resolve `host_probe_database_path(os.environ)`, assert its final components are exactly `host-probe/zdecision-host-probe.sqlite3`, close every probe process, then remove that file and its empty parent directory. Do not delete `private_state_root`, `agent/`, or any other SQLite file.

- [ ] **Step 3: Remove the diagnostic source and restore packaging contracts**

Use `apply_patch` to remove the diagnostic modules/resource/plugin/test, delete only the temporary marketplace entry, remove only the `host-probe-mcp` CLI branch/test, and restore the original one-entry marketplace assertion. Do not change `plugins/zdecision/`.

- [ ] **Step 4: Run post-cleanup verification once**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_agent_config_locator \
  tests.test_plugin_contract \
  tests.test_mcp_recall_confirmation \
  tests.test_mcp_inline_refresh -v

.venv/bin/python -m compileall -q src/zdecision tests
git diff --check
git status --short
```

Expected: tests pass; no diagnostic source or marketplace entry remains; only the approved design, implementation plan, and acceptance report remain in history/current tree; the two protected untracked files are untouched.

- [ ] **Step 5: Commit cleanup**

```bash
git add -A \
  src/zdecision/agent/host_capability_probe.py \
  src/zdecision/agent/host_capability_probe_mcp.py \
  src/zdecision/agent/static/recall-host-capability-probe-v1.html \
  src/zdecision/agent/cli.py \
  plugins/zdecision-host-probe \
  .agents/plugins/marketplace.json \
  tests/test_recall_host_capability_probe.py \
  tests/test_agent_config_locator.py \
  tests/test_plugin_contract.py
git commit -m "chore: remove disposable Recall host probe"
```

Do not stage the protected unrelated untracked acceptance/test files with an unscoped `git add -A`.

## Final verification and handoff

After Task 5, run:

```bash
git show --check --stat HEAD
git status --short --branch
```

Report the exact host outcome and the single route it selected. Do not begin the production Recall amendment until the user accepts that result.
