# Recall User-Confirmation Gate E0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove, without changing ZDecision production behavior, that the installed Codex Desktop presents a native MCP form-elicitation to the human user and returns non-forgeable, request-bound `accept`, `decline`, and `cancel` outcomes.

**Architecture:** A standalone test-only FastMCP stdio server owns one zero-argument probe tool and a private SQLite receipt store. It is temporarily registered with `codex mcp add`, never added to the ZDecision Plugin bundle, and asks Codex for a closed empty-object confirmation through request-scoped `Context.elicit()`. Automated in-memory protocol tests prove schema, capability, replay, crash, and privacy behavior; four bounded Desktop interactions prove the actual human UI before the temporary server is removed.

**Tech Stack:** Python 3.14, standard-library SQLite and SHA-256, `mcp==1.29.0` FastMCP/ClientSession in-memory transport, Pydantic v2, `unittest`, Codex Desktop `26.803.41515`, Codex CLI `0.147.0`.

## Global Constraints

- This plan is Gate E0 only. Do not modify `src/zdecision/`, `plugins/zdecision/`, Hook behavior, `RecallMcpTools`, `RecallHostStore`, Candidate refresh, Capture, Central, Registry, or Decision retrieval.
- Do not add the probe tool to the production ZDecision MCP server or Plugin manifest. Register one temporary global stdio MCP server named exactly `zdecision-elicitation-e0`, then remove it after the evidence is recorded.
- The probe tool has zero model-authored arguments. The currently armed case is private local state; the model cannot submit a confirmation value, case ID, Session ID, Turn ID, repository, actor, or continuation token.
- The Elicitation schema is a closed empty object. Only the client-returned `action = accept` authorizes the probe result; `decline`, `cancel`, unavailable capability, malformed response, exception, timeout, EOF, process restart, or transport loss are non-authorizing.
- Persist only fixed case IDs, one private current-case marker, opaque request-ID
  digests, bounded action/state enums, prompt/completion counts, UTC timestamps,
  schema/version, and the probe source digest.
- Never persist or emit Prompt, transcript, user message, UI message, raw Elicitation response, source, diff, Decision text, credentials, tool arguments/output, absolute repository/Plugin paths, or exception text.
- `pending` recovered after process restart becomes terminal `transport_lost`; it is never re-prompted or converted to `accept`.
- Any live failure is a hard stop. Do not begin production activation changes and do not fall back to Skill-selection proof, Prompt parsing, a model-authored boolean, or a second App Server.
- Preserve the user's unrelated untracked files: `docs/superpowers/acceptance/2026-08-06-recall-host-gate.md` and `tests/integration/test_recall_host_gate.py`. Never stage them in this plan.

## File Map

- Create `tests/recall_elicitation_probe.py`: test-only probe state, SQLite receipts, FastMCP server, and `arm`/`serve`/`report` CLI.
- Create `tests/test_recall_elicitation_probe.py`: pure store tests plus in-memory MCP protocol tests.
- Create `tests/integration/test_recall_elicitation_desktop.py`: opt-in assertion over the sanitized private Desktop receipt database.
- Create `docs/superpowers/acceptance/2026-08-09-recall-elicitation-e0.md`: bounded environment, automated evidence, live case table, cleanup evidence, and final PASS/FAIL.
- Modify `docs/superpowers/specs/2026-08-09-recall-user-confirmation-entry-design.md`: retain the already approved status and lifecycle correction in commit history.

---

### Task 1: Build the durable one-shot probe state

**Files:**
- Create: `tests/recall_elicitation_probe.py`
- Create: `tests/test_recall_elicitation_probe.py`

**Interfaces:**
- Consumes: one operator-created private SQLite path and a UTC-aware clock.
- Produces:

```python
ProbeCase = Literal[
    "accept", "decline", "cancel", "capability_unavailable", "restart",
]
ProbeState = Literal[
    "armed", "pending", "accept", "decline", "cancel",
    "unavailable", "failed", "transport_lost",
]

@dataclass(frozen=True)
class ProbeReceipt:
    case_id: ProbeCase
    state: ProbeState
    request_digest: str | None
    prompt_count: int
    completion_count: int
    updated_at: str

class ProbeReceiptStore:
    # Public signatures fixed by this task:
    # open(path: Path) -> ProbeReceiptStore
    # arm(case_id: ProbeCase, *, now: datetime) -> ProbeReceipt
    # claim_armed(*, request_digest: str, now: datetime) -> ProbeReceipt
    # mark_armed_unavailable(*, now: datetime) -> ProbeReceipt
    # complete(case_id: ProbeCase, *, state: ProbeState, now: datetime)
    #     -> ProbeReceipt
    # recover_pending(*, now: datetime) -> tuple[ProbeReceipt, ...]
    # current() -> ProbeReceipt | None
    # receipt(case_id: ProbeCase) -> ProbeReceipt | None
    # receipts() -> tuple[ProbeReceipt, ...]
    # report() -> dict[str, object]
    # close() -> None
```

`ProbeReceiptStore.open()` creates exactly one table and one partial unique
index:

```sql
CREATE TABLE IF NOT EXISTS elicitation_probe_receipts (
    case_id TEXT PRIMARY KEY CHECK(case_id IN (
        'accept', 'decline', 'cancel', 'capability_unavailable', 'restart'
    )),
    state TEXT NOT NULL CHECK(state IN (
        'armed', 'pending', 'accept', 'decline', 'cancel',
        'unavailable', 'failed', 'transport_lost'
    )),
    is_current INTEGER NOT NULL CHECK(is_current IN (0, 1)),
    request_digest TEXT,
    prompt_count INTEGER NOT NULL CHECK(prompt_count BETWEEN 0 AND 1),
    completion_count INTEGER NOT NULL CHECK(completion_count BETWEEN 0 AND 1),
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_current_elicitation_probe
ON elicitation_probe_receipts(is_current)
WHERE is_current = 1;
```

- [ ] **Step 1: Write the failing transition and privacy tests**

Add `ProbeReceiptStoreTest` with a temporary SQLite database and these exact behaviors:

```python
def test_arm_claim_accept_is_one_shot(self):
    self.store.arm("accept", now=NOW)
    pending = self.store.claim_armed(request_digest="a" * 64, now=NOW)
    self.assertEqual((pending.state, pending.prompt_count), ("pending", 1))
    accepted = self.store.complete("accept", state="accept", now=NOW)
    self.assertEqual(
        (accepted.state, accepted.prompt_count, accepted.completion_count),
        ("accept", 1, 1),
    )
    self.assertEqual(
        self.store.current(),
        accepted,
    )

def test_decline_and_cancel_are_terminal_non_accepting_results(self):
    for case_id, state in (("decline", "decline"), ("cancel", "cancel")):
        self.store.arm(case_id, now=NOW)
        self.store.claim_armed(request_digest="b" * 64, now=NOW)
        receipt = self.store.complete(case_id, state=state, now=NOW)
        self.assertEqual(receipt.state, state)
        self.assertEqual(receipt.completion_count, 1)

def test_restart_recovers_pending_as_transport_lost_without_reprompt(self):
    self.store.arm("restart", now=NOW)
    self.store.claim_armed(request_digest="c" * 64, now=NOW)
    self.store.close()
    reopened = ProbeReceiptStore.open(self.database_path)
    recovered = reopened.recover_pending(now=LATER)
    self.assertEqual([item.state for item in recovered], ["transport_lost"])
    self.assertEqual(reopened.receipt("restart").prompt_count, 1)
    with self.assertRaises(ProbeConflict):
        reopened.arm("restart", now=LATER)

def test_report_and_database_exclude_private_sentinels(self):
    sentinel = "PRIVATE_PROMPT_SOURCE_DIFF_DECISION_SENTINEL"
    self.store.arm("accept", now=NOW)
    report = canonical_json_bytes(self.store.report())
    self.assertNotIn(sentinel.encode(), report)
    self.assertNotIn(sentinel.encode(), self.database_path.read_bytes())
```

Also cover invalid case/state/digest/time, two simultaneously armed cases,
different-request replay, transactional movement of the private current marker
without changing an earlier terminal receipt, unavailable with zero prompt
count, completion before claim, second completion, and `accept` after recovered
`transport_lost`.

- [ ] **Step 2: Run the focused test and confirm RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_elicitation_probe.ProbeReceiptStoreTest -v
```

Expected: FAIL because `tests.recall_elicitation_probe` does not exist.

- [ ] **Step 3: Implement the minimal SQLite state owner**

Use `BEGIN IMMEDIATE` for every state transition. Normalize timestamps to UTC
`Z`; validate request digests as exactly 64 lowercase hexadecimal characters.
`arm()` refuses while any other case is `armed` or `pending`, clears the old
terminal row's private `is_current` marker, and inserts the newly armed case as
the sole current row in the same transaction. It also refuses to overwrite an
existing case ID. `claim_armed()` requires the sole current row to be `armed`
and changes it to `pending`.
`mark_armed_unavailable()` changes exactly one `armed` row to `unavailable`
without incrementing `prompt_count`. `complete()` requires `pending`, matches
the claimed case, and accepts only:

```python
_COMPLETION_STATES = frozenset(
    ("accept", "decline", "cancel", "unavailable", "failed", "transport_lost")
)
```

`completion_count` becomes `1` only for a valid client action in
`accept|decline|cancel`. It remains `0` for `unavailable`, `failed`, and
`transport_lost`.

`recover_pending()` changes every `pending` row to `transport_lost` in one
transaction and leaves all other states unchanged. `current()` reads only the
unique current row; the marker is never returned in a tool result or report.
`report()` returns only:

```python
{
    "gate": "E0",
    "schema_version": 1,
    "cases": [
        {
            "case_id": receipt.case_id,
            "state": receipt.state,
            "request_digest": receipt.request_digest,
            "prompt_count": receipt.prompt_count,
            "completion_count": receipt.completion_count,
            "updated_at": receipt.updated_at,
        }
        for receipt in receipts
    ],
}
```

Do not add a free-form message or error column.

- [ ] **Step 4: Run focused tests and checks**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_elicitation_probe.ProbeReceiptStoreTest -v
.venv/bin/python -m compileall -q tests/recall_elicitation_probe.py tests/test_recall_elicitation_probe.py
git diff --check
```

Expected: the store tests pass; compile and diff checks are clean.

- [ ] **Step 5: Commit Task 1**

```bash
git add tests/recall_elicitation_probe.py tests/test_recall_elicitation_probe.py
git commit -m "test: add durable Recall elicitation probe state"
```

---

### Task 2: Prove the MCP Elicitation wire contract in memory

**Files:**
- Modify: `tests/recall_elicitation_probe.py`
- Modify: `tests/test_recall_elicitation_probe.py`
- Create: `tests/integration/test_recall_elicitation_desktop.py`

**Interfaces:**
- Consumes: `ProbeReceiptStore`, FastMCP request `Context`, the initialized client's declared form-Elicitation capability, and the server request ID.
- Produces:

```python
class EmptyConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
```

The remaining public signatures are fixed as:

- `supports_form_elicitation(context: Context) -> bool`
- `request_digest(request_id: object) -> str`
- `build_probe_server(database_path: Path) -> FastMCP`
- `main(argv: Sequence[str] | None = None) -> int`

`main()` exposes exactly:

```text
recall_elicitation_probe arm --database PATH --case {accept,decline,cancel,capability_unavailable,restart}
recall_elicitation_probe serve --database PATH
recall_elicitation_probe report --database PATH
```

The FastMCP server exposes exactly one zero-input tool named `probe_zdecision_elicitation`.

- [ ] **Step 1: Write failing schema, capability, protocol, and replay tests**

Use `unittest.IsolatedAsyncioTestCase`, `mcp.shared.memory.create_connected_server_and_client_session`, and a real `ClientSession` callback:

```python
async def _call_server(self, elicitation_callback) -> dict[str, object]:
    server = build_probe_server(self.database_path)
    async with create_connected_server_and_client_session(
        server,
        elicitation_callback=elicitation_callback,
        raise_exceptions=True,
    ) as session:
        result = await session.call_tool("probe_zdecision_elicitation", {})
    return json.loads(result.content[0].text)

async def _call_with_action(self, action: str) -> dict[str, object]:
    seen: list[types.ElicitRequestParams] = []

    async def elicitation_callback(context, params):
        seen.append(params)
        return types.ElicitResult(
            action=action,
            content={} if action == "accept" else None,
        )

    result = await self._call_server(elicitation_callback)
    self.assertEqual(len(seen), 1)
    return result
```

Add these exact tests:

```python
def test_empty_confirmation_schema_is_closed_and_has_no_properties(self):
    schema = EmptyConfirmation.model_json_schema()
    self.assertEqual(schema["properties"], {})
    self.assertIs(schema["additionalProperties"], False)

async def test_accept_decline_and_cancel_remain_distinct(self):
    for case_id, action in (("accept", "accept"), ("decline", "decline"), ("cancel", "cancel")):
        self.store.arm(case_id, now=NOW)
        response = await self._call_with_action(action)
        self.assertEqual(response["action"], action)
        self.assertEqual(response["authorized"], action == "accept")

async def test_client_without_form_capability_returns_unavailable_without_eliciting(self):
    self.store.arm("capability_unavailable", now=NOW)
    server = build_probe_server(self.database_path)
    async with create_connected_server_and_client_session(
        server, raise_exceptions=True
    ) as session:
        result = await session.call_tool("probe_zdecision_elicitation", {})
    response = json.loads(result.content[0].text)
    self.assertEqual(response["action"], "unavailable")
    receipt = self.store.receipt("capability_unavailable")
    self.assertEqual((receipt.state, receipt.prompt_count), ("unavailable", 0))

async def test_terminal_replay_returns_one_receipt_without_second_elicitation(self):
    self.store.arm("accept", now=NOW)
    calls = 0

    async def accept_callback(context, params):
        nonlocal calls
        calls += 1
        return types.ElicitResult(action="accept", content={})

    first = await self._call_server(accept_callback)
    replay = await self._call_server(accept_callback)
    self.assertEqual((first["action"], replay["action"]), ("accept", "accept"))
    self.assertEqual((first["replayed"], replay["replayed"]), (False, True))
    self.assertEqual(calls, 1)
    receipt = self.store.receipt("accept")
    self.assertEqual((receipt.prompt_count, receipt.completion_count), (1, 1))

async def test_context_elicit_relates_response_to_originating_tool_request(self):
    session = SimpleNamespace(
        elicit_form=AsyncMock(
            return_value=types.ElicitResult(action="decline", content=None)
        )
    )
    request_context = RequestContext(
        request_id="tool-request-17",
        meta=None,
        session=session,
        lifespan_context=None,
    )
    context = Context(request_context=request_context)
    result = await context.elicit(
        message=ELICITATION_MESSAGE,
        schema=EmptyConfirmation,
    )
    self.assertEqual(result.action, "decline")
    self.assertEqual(
        session.elicit_form.await_args.kwargs["related_request_id"],
        "tool-request-17",
    )

async def test_callback_exception_is_non_authorizing_and_sanitized(self):
    sentinel = "PRIVATE_ELICITATION_EXCEPTION_SENTINEL"

    async def failing_callback(context, params):
        raise RuntimeError(sentinel)

    self.store.arm("accept", now=NOW)
    response = await self._call_server(failing_callback)
    self.assertEqual(response["action"], "failed")
    self.assertFalse(response["authorized"])
    self.assertNotIn(sentinel, json.dumps(response))
    self.assertNotIn(sentinel.encode(), self.database_path.read_bytes())

async def test_tool_schema_has_no_model_authored_fields(self):
    tools = await build_probe_server(self.database_path).list_tools()
    probe = next(item for item in tools if item.name == "probe_zdecision_elicitation")
    self.assertEqual(probe.parameters.get("properties"), {})
    self.assertEqual(probe.parameters.get("required", []), [])
```

Assert the callback receives the exact message and a closed empty-object schema, but never persist either. Scan the SQLite bytes, returned tool result, and report for Prompt/source/diff/Decision/exception sentinels.

Add one table-driven failure test that creates a fresh temporary store for each
of `TimeoutError`, `EOFError`, a malformed `ElicitResult`, and callback
`RuntimeError`; every case must finish as `failed`, return
`authorized=false`, keep `completion_count=0`, and exclude exception text from
the database, tool result, and report. Add a cancellation test whose callback
raises `asyncio.CancelledError`; `_run_probe()` must persist `failed` during
shielded cleanup and then re-raise cancellation.

Import `AsyncMock`, `SimpleNamespace`, and `RequestContext` for the
request-binding test. Define `ELICITATION_MESSAGE` once and reuse it in the
server and the exact-message assertion; do not duplicate mutable UI text.

- [ ] **Step 2: Run the protocol tests and confirm RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_elicitation_probe -v
```

Expected: the Task 1 store tests pass and the new tests fail because the MCP server and CLI are absent.

- [ ] **Step 3: Implement the one-tool FastMCP adapter**

Use this exact adapter shape:

```python
def build_probe_server(database_path: Path) -> FastMCP:
    server = FastMCP("ZDecision Recall E0 Probe")

    @server.tool(
        title="Probe ZDecision user confirmation",
        description="Run the test-only ZDecision E0 native confirmation probe.",
    )
    async def probe_zdecision_elicitation(
        context: Context,
    ) -> dict[str, object]:
        return await _run_probe(context=context, database_path=database_path)

    return server
```

`_run_probe()` reads `current()`. An `armed` row may enter the Elicitation
flow; a `pending` row returns a bounded non-authorizing `pending` result; a
terminal row replays its existing bounded receipt without eliciting; and no
current row returns `unavailable`. The unique index and transactional checks
make multiple current rows a corruption error that fails closed. It checks:

```python
params = context.session.client_params
elicitation = None if params is None else params.capabilities.elicitation
form_supported = bool(elicitation is not None and elicitation.form is not None)
```

When unavailable, complete the armed case as `unavailable` with zero prompts. Otherwise hash `str(context.request_id)` with the domain tag `zdecision-elicitation-e0-request-v1`, claim once, and call:

```python
result = await context.elicit(
    message=(
        "是否启用本任务的 ZDecision 正式决策召回？"
        "确认后仅对当前 Codex Session 生效。"
    ),
    schema=EmptyConfirmation,
)
```

Map only `result.action` in `{"accept", "decline", "cancel"}` to the same
terminal state. Catch protocol, EOF, timeout, and validation exceptions as
`failed` without storing or returning exception text. On
`asyncio.CancelledError`, shield the bounded `failed` transition and re-raise
the cancellation; a process death before that transition remains `pending`
and startup recovery converts it to `transport_lost`. Return only:

```python
{
    "gate": "E0",
    "action": receipt.state,
    "authorized": receipt.state == "accept",
    "replayed": replayed,
    "prompt_count": receipt.prompt_count,
    "completion_count": receipt.completion_count,
}
```

`authorized=true` on replay refers to the same immutable accepted operation;
it is not a second client action. `replayed=true`, the unchanged request
digest, and `completion_count=1` prove that the server received and recorded
`accept` exactly once.

At `serve` startup call `recover_pending()` before `server.run(transport="stdio")`. `arm` refuses an existing case. `report` writes one canonical JSON object to stdout. All diagnostics go to stderr as bounded codes only; stdio server startup writes nothing outside MCP framing.

- [ ] **Step 4: Add the opt-in Desktop evidence assertion**

Create a skipped-by-default integration test:

```python
@unittest.skipUnless(
    os.environ.get("ZDECISION_LIVE_ACCEPTANCE") == "1",
    "live Desktop acceptance is disabled",
)
class RecallElicitationDesktopAcceptanceTest(unittest.TestCase):
    def test_exact_e0_receipts(self):
        store = ProbeReceiptStore.open(
            Path(os.environ["ZDECISION_ELICITATION_E0_DB"])
        )
        try:
            receipts = {item.case_id: item for item in store.receipts()}
        finally:
            store.close()
        self.assertEqual(receipts["accept"].state, "accept")
        self.assertEqual(receipts["decline"].state, "decline")
        self.assertEqual(receipts["cancel"].state, "cancel")
        self.assertEqual(receipts["restart"].state, "transport_lost")
        self.assertTrue(all(item.prompt_count == 1 for item in receipts.values()))
        self.assertEqual(receipts["accept"].completion_count, 1)
        self.assertEqual(receipts["decline"].completion_count, 1)
        self.assertEqual(receipts["cancel"].completion_count, 1)
        self.assertEqual(receipts["restart"].completion_count, 0)
```

Expose `receipts() -> tuple[ProbeReceipt, ...]` from the store in stable case order.

- [ ] **Step 5: Run automated E0 and unchanged-production regressions**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_elicitation_probe -v
.venv/bin/python -m unittest tests.integration.test_recall_elicitation_desktop -v
.venv/bin/python -m unittest tests.test_mcp_recall_host_gate tests.test_recall_hook_gate tests.test_recall_skill_contract tests.test_plugin_contract -v
.venv/bin/python -m compileall -q tests/recall_elicitation_probe.py tests/test_recall_elicitation_probe.py tests/integration/test_recall_elicitation_desktop.py
git diff --check
```

Expected: all probe unit/protocol tests pass; the Desktop test is skipped because the live flag is absent; the existing 60 Recall/Plugin tests pass unchanged; compile and diff checks are clean.

- [ ] **Step 6: Commit Task 2**

```bash
git add tests/recall_elicitation_probe.py tests/test_recall_elicitation_probe.py tests/integration/test_recall_elicitation_desktop.py
git commit -m "test: prove Recall elicitation protocol contract"
```

---

### Task 3: Run the real Desktop Gate E0 and cleanly stop

**Precondition:** Tasks 1 and 2 are committed and every automated command in Task 2 Step 5 has the expected result.

**Files:**
- Create: `docs/superpowers/acceptance/2026-08-09-recall-elicitation-e0.md`

**Interfaces:**
- Consumes: the test-only probe module, one explicit private database path, current `codex mcp` configuration, four human actions in Codex Desktop, and the opt-in Desktop assertion.
- Produces: one sanitized E0 evidence report with an exact PASS/FAIL conclusion and no production changes.

- [ ] **Step 1: Record a collision-free preflight and register the temporary server**

First run:

```bash
codex mcp get zdecision-elicitation-e0 --json
```

Expected: non-zero because the temporary name is absent. If it exists, stop and inspect ownership; do not overwrite it.

Use the explicit private path:

```text
/private/tmp/zdecision-recall-elicitation-e0-20260809.sqlite3
```

Register the exact absolute command:

```bash
codex mcp add zdecision-elicitation-e0 -- \
  /Users/zhaohuiying/Desktop/Zstack-repos/zdecision/.venv/bin/python \
  /Users/zhaohuiying/Desktop/Zstack-repos/zdecision/tests/recall_elicitation_probe.py \
  serve --database /private/tmp/zdecision-recall-elicitation-e0-20260809.sqlite3
codex mcp get zdecision-elicitation-e0 --json
```

Expected: the second command identifies one enabled stdio server with the exact Python executable and probe script. Do not copy the full configuration into Git.

Ask the user to restart Codex Desktop once so the temporary MCP catalog is reloaded. Do not modify or restart the production ZDecision service.

- [ ] **Step 2: Run and verify the accept case plus replay**

Arm the case:

```bash
.venv/bin/python tests/recall_elicitation_probe.py arm \
  --database /private/tmp/zdecision-recall-elicitation-e0-20260809.sqlite3 \
  --case accept
```

In one fresh native Desktop task, ask the user to request the test-only E0 probe. The tool has no arguments. Require the Desktop to visibly wait for the user; the user clicks the affirmative action.

Run `report` and require `accept`, `prompt_count=1`, and `completion_count=1`. Ask for the same probe once more without re-arming; require the same bounded result and no second UI. If the model answers the confirmation, the tool completes before the user acts, or a second prompt appears, mark E0 FAIL, run the cleanup in Step 6, record Step 7, and do not run another live case.

- [ ] **Step 3: Run and verify decline and cancel**

Arm `decline`, invoke the same zero-input tool in a new native Turn, and ask the user to choose the negative action. Require `state=decline`, `prompt_count=1`, and `completion_count=1`.

Arm `cancel`, invoke in another new native Turn, and ask the user to dismiss the UI using the host's cancel/close behavior. Require `state=cancel`, `prompt_count=1`, and `completion_count=1`.

Neither case may return `authorized=true`, create ZDecision Recall state, or trigger any production ZDecision tool.

- [ ] **Step 4: Prove restart/transport loss cannot become acceptance**

Arm `restart`, invoke the zero-input probe, wait until the Desktop visibly shows the confirmation UI, and then ask the user to restart Codex Desktop without choosing an action.

After restart, the probe server's startup recovery must change the persisted `pending` row to `transport_lost`. Run `report`; require `prompt_count=1`, `completion_count=0`, and no `accept`. Requesting the probe again without re-arming must not show another confirmation.

- [ ] **Step 5: Run the executable live acceptance and privacy scan**

Run:

```bash
ZDECISION_LIVE_ACCEPTANCE=1 \
ZDECISION_ELICITATION_E0_DB=/private/tmp/zdecision-recall-elicitation-e0-20260809.sqlite3 \
.venv/bin/python -m unittest tests.integration.test_recall_elicitation_desktop -v
.venv/bin/python tests/recall_elicitation_probe.py report \
  --database /private/tmp/zdecision-recall-elicitation-e0-20260809.sqlite3
```

Expected: the live test passes and the live database report contains the four
fixed Desktop cases only. The fifth `capability_unavailable` scenario is proven
by the in-memory client-without-capability test from Task 2; it is not copied
into the Desktop database. Search the report and private database for unique
Prompt, transcript, source, diff, Decision, credential, and exception sentinels
used by the automated tests; every search must be empty.

- [ ] **Step 6: Remove the temporary server and verify cleanup**

Run:

```bash
codex mcp remove zdecision-elicitation-e0
codex mcp get zdecision-elicitation-e0 --json
```

Expected: remove succeeds and get returns non-zero. Ask the user to restart Codex Desktop once more and verify the probe tool is absent. Retain the private SQLite file only until the sanitized acceptance report is committed; it is not a product database or Git input.

- [ ] **Step 7: Record bounded evidence and apply the hard stop**

Write exactly:

- UTC time and current Codex Desktop, Codex CLI, Python, and MCP SDK versions;
- Git source base and SHA-256 of `tests/recall_elicitation_probe.py`;
- confirmation that `tool_call_mcp_elicitation` is reported stable and `mcp_2026_07_28` remains disabled;
- one row per required scenario containing only fixed case ID, evidence source
  (`desktop` or `automated`), request digest prefix or `not_sent`, action/state,
  prompt count, completion count, and PASS/FAIL; the four Desktop rows come from
  the private live database and the `capability_unavailable` row comes from the
  exact Task 2 in-memory test result;
- automated commands with counts/status;
- temporary MCP cleanup status; and
- final `PASS — production activation planning may begin` or `FAIL — production activation remains blocked`.

Do not include Prompt, UI copy/screenshot, raw response, absolute temporary/config paths, tool output, exception text, or private data.

Any failed case makes the final result FAIL even if later retry succeeds. On
the first failure, do not run later live cases; remove the temporary MCP server
using Step 6, record this report, and stop. Present the MCP Apps app-only card
only as a new design option.

- [ ] **Step 8: Verify and commit only Gate E0 artifacts**

Before the evidence commit, run:

```bash
git diff --check
git status --short
```

Stage only the new E0 acceptance report. The design-status update and plan are
committed before Task 1, and the probe files/tests are committed by Tasks 1 and
2. Never stage the two unrelated untracked files named in Global Constraints.

Commit the evidence:

```bash
git add docs/superpowers/acceptance/2026-08-09-recall-elicitation-e0.md
git commit -m "test: record native Recall confirmation gate"
```

After the commit, run:

```bash
git show --check HEAD
git status --short --branch
```

Expected: the evidence commit is format-clean; only the two pre-existing
untracked files named in Global Constraints remain unrelated to this plan.

## Final Stop Rule

This plan ends after the sanitized Gate E0 evidence commit and temporary MCP cleanup. PASS permits writing a separate production implementation plan for Plugin/Skill topology, pending activation attempts, asynchronous `activate_zdecision_recall`, and existing Recall lifecycle integration. PASS does not authorize implementing those changes inside this plan. FAIL keeps production Recall activation blocked and returns to the user without another review loop.
