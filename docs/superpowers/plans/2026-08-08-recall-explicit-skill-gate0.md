# Recall Explicit-Skill Gate 0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove, without changing ZDecision production behavior, that a test-only client can read the existing Codex Desktop host through its Unix control socket and distinguish an explicit bundled Recall Skill from three non-authorizing inputs.

**Architecture:** A test-only Python probe launches `codex app-server proxy`, which is a client proxy to the already-running Desktop control socket rather than a second app-server. It reuses the bounded JSON-RPC client and Gateway, rejects controlled-process fallback, and emits only sanitized identity, selection, and item metadata. Gate 0A proves the host route first; Gate 0B does not start unless 0A passes.

**Tech Stack:** Python 3.11 standard library, existing `ProcessJsonlTransport`, `JsonlAppServerClient`, `AppServerGateway`, SQLite read-only queries, `unittest`, Codex Desktop `app-server proxy`.

## Global Constraints

- Do not modify Plugin, Hook, MCP, Recall state, retrieval, Candidate, Central, or Registry production code.
- Do not launch `codex app-server` or accept `AppServerGateway` controlled-process fallback. `codex app-server proxy` is the only allowed live transport process.
- Never read or emit Prompt, transcript text, message text, Plugin URI text, tool arguments, tool output, source, diff, credentials, or full private paths.
- Gate 0A uses an exact known task ID. Gate 0B obtains an exact new Turn ID from the Hook ledger for that exact task; no recency, CWD, or transcript target guessing.
- Persist only bounded task/Turn IDs, route category, selection type/name, path category/equality, ordered item type/ID, timestamps, and pass/fail codes.
- A programmatic client that creates the same structured Skill item in a trusted user-visible root task is inside the accepted technical trust boundary; no physical-click claim is made.
- Any `agentMessage` before the activation MCP item fails ordering; message content is never inspected.
- Gate 0A failure stops before Gate 0B code or live cases. Gate 0B failure stops before production implementation or a Gate 1 claim.

---

### Task 1: Build and run the Gate 0A Desktop-host probe

**Files:**
- Create: `tests/recall_entry_protocol_probe.py`
- Create: `tests/test_recall_entry_protocol_probe.py`
- Create: `docs/superpowers/acceptance/2026-08-08-recall-entry-gate0.md`

**Interfaces:**
- Consumes: `ProcessJsonlTransport`, `AppServerGateway`, `AgentDatabase`, and an exact operator-supplied task ID.
- Produces:

```python
def launch_desktop_proxy(
    process_factory: Callable[[Sequence[str]], subprocess.Popen[str]],
) -> AppServerTransport: ...

def probe_known_thread(
    *, thread_id: str, transport: AppServerTransport
) -> dict[str, object]: ...

def forbid_controlled_process() -> AppServerTransport: ...
```

`launch_desktop_proxy()` invokes exactly `("codex", "app-server", "proxy")`.
`probe_known_thread()` returns only:

```python
{
    "gate": "0A",
    "route": "host_unix",
    "thread_match": True,
    "endpoint_category": "desktop_default_control_socket",
}
```

- [ ] **Step 1: Write failing tests for the proxy command and fallback denial**

Use a fake `Popen` factory and fake transports:

```python
def test_launches_only_the_existing_desktop_proxy(self):
    commands: list[tuple[str, ...]] = []
    launch_desktop_proxy(lambda command: self.fake_process(command, commands))
    self.assertEqual(commands, [("codex", "app-server", "proxy")])

def test_probe_never_falls_back_to_a_controlled_app_server(self):
    with self.assertRaises(AppServerUnavailable):
        probe_known_thread(
            thread_id="019fdf3f-2b42-79f1-b049-c8e464c330ab",
            transport=FailingTransport(),
        )
    self.assertEqual(self.controlled_process_launches, 0)
```

Also assert invalid task IDs, non-object replies, wrong returned Thread IDs,
EOF/timeouts, and peer values containing text fields produce bounded failures
without echoing peer data.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_entry_protocol_probe -v
```

Expected: FAIL because `tests.recall_entry_protocol_probe` does not exist.

- [ ] **Step 3: Implement the minimal test-only Gate 0A probe**

Use `subprocess.Popen` with UTF-8 text pipes and wrap the process in
`ProcessJsonlTransport`. Open `AgentDatabase` only inside
`tempfile.TemporaryDirectory()`. Construct the Gateway with the proxy as
`host_transport` and `process_factory=forbid_controlled_process`, call
`read_thread_identity()`, sanitize the result, close every resource, and expose:

```bash
.venv/bin/python -m tests.recall_entry_protocol_probe thread \
  --thread-id 019fdf3f-2b42-79f1-b049-c8e464c330ab
```

The command prints one canonical JSON object and never accepts or prints a
Prompt or host stderr/stdout.

- [ ] **Step 4: Run focused tests and static checks**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_entry_protocol_probe -v
.venv/bin/python -m compileall -q tests/recall_entry_protocol_probe.py tests/test_recall_entry_protocol_probe.py
git diff --check
```

Expected: all focused tests pass; compile and diff checks are clean.

- [ ] **Step 5: Run the one real Gate 0A probe**

Run the Step 3 command against the completed acceptance task. If sandboxing
prevents connecting to the user-owned Unix socket, rerun only this read-only
command with sandbox escalation.

Pass requires exactly `route=host_unix`, `thread_match=true`, and
`endpoint_category=desktop_default_control_socket`. Do not run Gate 0B after
any other result.

- [ ] **Step 6: Record bounded evidence and commit Task 1**

Write environment versions, UTC time, exact task ID, sanitized result, and
PASS/FAIL to the acceptance file. Never include the socket path or host reply.
On PASS:

```bash
git add tests/recall_entry_protocol_probe.py tests/test_recall_entry_protocol_probe.py docs/superpowers/acceptance/2026-08-08-recall-entry-gate0.md
git commit -m "test: prove Desktop host app-server route"
```

On FAIL, commit the harness and bounded failed evidence with message
`test: record unavailable Desktop host route`, mark Gate 0 blocked, and stop.

---

### Task 2: Add the Gate 0B exact-Turn watcher

**Precondition:** Task 1 evidence is PASS. Otherwise this task must not start.

**Files:**
- Modify: `tests/recall_entry_protocol_probe.py`
- Modify: `tests/test_recall_entry_protocol_probe.py`

**Interfaces:**
- Consumes: Gate 0A host transport; exact task ID; read-only Agent SQLite path from `database_path(os.environ)`; trusted `PLUGIN_ROOT` captured by the existing activation binding.
- Produces:

```python
def watch_next_observed_turn(
    *,
    thread_id: str,
    database_path: Path,
    transport: AppServerTransport,
    timeout_seconds: float,
) -> dict[str, object]: ...
```

The watcher snapshots existing `UserPromptSubmit` Turn IDs for the exact task,
polls SQLite through `mode=ro`, accepts exactly one new Turn for that task, and
reads it while `inProgress`. Its output contains only task/Turn IDs, selection
count, bounded type/name, path category/equality, ordered item type/ID, and the
boolean `agent_message_before_activation`.

- [ ] **Step 1: Write failing privacy, target-binding, and ordering tests**

Use temporary SQLite fixtures and fake app-server responses. Add exactly:

```python
def test_watcher_ignores_other_tasks_and_existing_turns(): ...
def test_watcher_requires_one_new_hook_observed_turn(): ...
def test_watcher_never_selects_prompt_or_message_text(): ...
def test_plain_text_and_plugin_attachment_have_no_skill_selection(): ...
def test_explicit_skill_matches_the_hook_bound_installed_path(): ...
def test_any_agent_message_before_activation_is_reported(): ...
def test_subagent_or_internal_thread_is_rejected(): ...
def test_sanitized_json_contains_no_private_path_or_payload_sentinel(): ...
```

For the explicit case, insert an activation binding whose `plugin_root` is a
temporary Plugin root containing `skills/decision-recall/SKILL.md`; compare
paths without returning either absolute path.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_entry_protocol_probe -v
```

Expected: FAIL because `watch_next_observed_turn()` does not exist.

- [ ] **Step 3: Implement the bounded watcher**

Use a read-only SQLite URI and select only `event_type`, `session_id`,
`turn_id`, `cwd`, and safe event metadata. Never select `safe_fact_json` or a
message field. Require `UserPromptSubmit` for the exact task, then call
`AppServerGateway.read_active_turn_evidence()` for that exact Turn.

For an explicit Skill, wait boundedly for the matching Session/Turn activation
binding and compare its trusted Plugin root to the selected path. For negative
cases, require no qualifying selection and no activation binding. Treat every
`agentMessage` before `activate_zdecision_recall` as failure without reading
text. Expose:

```bash
.venv/bin/python -m tests.recall_entry_protocol_probe watch \
  --thread-id 019f5f21-0d48-7501-9dd5-0219870232a1 \
  --timeout-seconds 30
```

- [ ] **Step 4: Run focused tests and checks, then commit Task 2**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_entry_protocol_probe -v
.venv/bin/python -m compileall -q tests/recall_entry_protocol_probe.py tests/test_recall_entry_protocol_probe.py
git diff --check
```

Expected: all pass.

Commit:

```bash
git add tests/recall_entry_protocol_probe.py tests/test_recall_entry_protocol_probe.py
git commit -m "test: observe explicit Recall Skill input"
```

---

### Task 3: Run the four Gate 0B Desktop cases and stop honestly

**Precondition:** Tasks 1 and 2 are committed and Gate 0A evidence is PASS.

**Files:**
- Modify: `docs/superpowers/acceptance/2026-08-08-recall-entry-gate0.md`

**Interfaces:**
- Consumes: `watch_next_observed_turn()` and four fresh native Turns in exact registered task `019f5f21-0d48-7501-9dd5-0219870232a1`, whose current Recall Session and activation-binding counts were both verified as zero before planning.
- Produces: one bounded four-row evidence table and a final PASS/FAIL decision.

- [ ] **Step 1: Arm and run the no-selection control**

Start the watcher for the exact task, then ask the user to send an ordinary
development Turn without ZDecision. Require zero structured Skill selections
and zero Recall activation binding.

- [ ] **Step 2: Arm and run the whole-Plugin control**

Start the watcher, then ask the user to attach the whole ZDecision Plugin and
send the Turn. Require zero qualifying structured Skill selections and zero
Recall activation binding.

- [ ] **Step 3: Arm and run the copied-text control**

Start the watcher, then ask the user to send literal copied text
`$decision-recall` without choosing the Skill. Require zero qualifying
structured Skill selections and zero Recall activation binding.

- [ ] **Step 4: Arm and run the explicit bundled-Skill case**

Start the watcher, then ask the user to choose **ZDecision Recall** from the
native Skill picker and send the normal request in that Turn. Require exactly
one `type=skill` selection, stable bounded name, exact Hook-root path match,
repeat-read stability, and no `agentMessage` before the activation MCP item.

- [ ] **Step 5: Record the result and apply the hard stop**

Append only exact task/Turn IDs, UTC timestamps, bounded names, result/path
categories, ordered item types/IDs, and PASS/FAIL codes. Gate 0B passes only if
all four rows pass. Otherwise record the first failed contract and stop. Do not
change production files or start Gate 1 in this plan.

- [ ] **Step 6: Verify and commit the evidence**

Run:

```bash
git diff --check
git status --short
```

Commit only the evidence file:

```bash
git add docs/superpowers/acceptance/2026-08-08-recall-entry-gate0.md
git commit -m "test: record explicit Recall Skill protocol gate"
```

## Final stop rule

This plan ends after the Gate 0A/0B evidence commit. PASS permits a new,
separately reviewed production implementation plan for the contingent
activation boundary. FAIL records the exact unavailable host capability and
returns to the user; it does not authorize marker parsing, a second app-server,
or broader review.
