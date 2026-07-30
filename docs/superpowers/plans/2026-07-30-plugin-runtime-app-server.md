# ZDecision Plugin Runtime and App-Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pass feasibility Gates 1 through 3 by installing one real ZDecision
Plugin, recording its Hooks through a bounded local Agent, keeping one
on-demand Worker alive for active Sessions, and running the existing two-stage
Capture through Codex app-server from a real Hook Session ID.

**Architecture:** A repo-local Plugin package invokes one globally installed
`zdecision-agent` entry point. Hook ingestion and local MCP tools share an
SQLite database under the existing user-local private state root; Hooks only
record bounded facts and wake a singleton Worker. A typed JSONL gateway first
tries an explicitly supplied supported host transport, then uses the one
predeclared fallback, `codex app-server --listen stdio://`, to read and fork the
source Thread and run a separate eligibility Turn followed by Inventory and
Extraction in a fresh Capture fork.

**Tech Stack:** Python 3.11+, Python standard library, SQLite, `fcntl` on the
macOS feasibility target, `mcp>=1.28,<2`, Codex Plugin manifests and Hooks,
Codex app-server JSONL protocol, existing `CaptureService` and
`TemplateCatalog`, and `unittest`.

## Global Constraints

- The authority is
  `docs/superpowers/specs/2026-07-30-plugin-feasibility-design.md`, especially
  Gates 1 through 3 and section 16 stopping rules.
- Work directly on `main`. Do not create a worktree or feature branch.
- The Plugin lives at `plugins/zdecision/`; it is exposed by the repository
  marketplace at `.agents/plugins/marketplace.json`.
- The feasibility install uses `.venv/bin/pip install -e .` for tests and
  `pipx install --editable . --force` before live Plugin installation so the
  desktop host can resolve `zdecision-agent`. Producing a self-contained
  distributable executable belongs to the later first-Demo specification.
- Hook commands may import only standard-library ZDecision modules on their
  latency path. Import the MCP SDK lazily only for the `mcp` subcommand.
- Store Agent state at
  `private_state_root(environ) / "agent" / "zdecision.sqlite3"`; tests pass an
  explicit temporary path and never use real user state.
- Never persist Prompt text, transcript contents, tool output, source diffs,
  source code, credentials, tokens, or complete app-server Thread data in the
  Event Ledger.
- Hook failure reports a bounded local error and returns control to Codex. It
  must not block an ordinary Turn.
- Every app-server eligibility, Inventory, and Extraction Turn passes one
  persisted exact `model_id` and reasoning effort explicitly.
- The eligibility fork and Capture fork are different ephemeral Threads.
  Eligibility output never enters the two-Turn Capture fork.
- The controlled app-server fallback inherits the user's existing Codex login;
  it never reads or copies credential files and never logs authentication data.
- Gate 3 is blocking. If both routes fail to read and fork the Hook Session,
  stop this plan and do not start Gates 4 through 9.
- Run only the focused tests named in each task. After Gate 3, run one combined
  focused set and one full repository suite; do not start another audit loop.

## File structure

```text
.agents/plugins/marketplace.json
plugins/zdecision/
  .codex-plugin/plugin.json
  .mcp.json
  hooks/hooks.json
  skills/zdecision/SKILL.md
  skills/zdecision/agents/openai.yaml
src/zdecision/
  agent/
    __init__.py
    cli.py
    db.py
    events.py
    hooks.py
    mcp_server.py
    repository.py
    worker.py
  app_server/
    __init__.py
    models.py
    jsonl.py
    gateway.py
    capture_runner.py
  capture/
    eligibility.py
    prompt_contracts/capture-eligibility-v1.md
tests/
  test_plugin_contract.py
  test_event_ledger.py
  test_repository.py
  test_worker.py
  test_hook_latency.py
  test_app_server_gateway.py
  test_automated_capture.py
  integration/
    __init__.py
    test_gate1_plugin_smoke.py
    test_gate3_live_app_server.py
```

The current `.agents/skills/zdecision/` remains the manual V1 repository Skill
and is not copied into the Plugin. The new Plugin Skill describes automatic
status, `report_work_state`, and manual fallback only.

## Gate coverage

| Approved requirement | Owning task and evidence |
|---|---|
| Plugin Skill, local tools, five trusted Hooks, arbitrary repo, disable behavior | Task 1 focused contract tests and live Gate 1 acceptance |
| Hook p95 <= 150 ms, 100 events, singleton, crash retry, outage isolation, 60-second active-session poll | Task 2 deterministic clock/concurrency tests and live Gate 2 acceptance |
| Real Hook Session read, source-preserving fork, isolated eligibility, fresh two-Turn Capture, native receipts | Tasks 3-4 fake-protocol tests and live Gate 3 acceptance |
| Exact discovered model and effort on every Turn | Task 3 frozen profile tests plus Task 4 receipt assertions |
| No transcript parsing or duplicate retry results | Task 1 privacy scan and Task 4 immutable operation/replay tests |
| Host path first, controlled process only fallback, both failing is blocking | Task 3 route selection and Task 4 stopping rule |

---

### Task 1: Installable Plugin, local MCP tools, and bounded Event Ledger

**Files:**

- Modify: `pyproject.toml`
- Create: `.agents/plugins/marketplace.json`
- Create: `plugins/zdecision/.codex-plugin/plugin.json`
- Create: `plugins/zdecision/.mcp.json`
- Create: `plugins/zdecision/hooks/hooks.json`
- Create: `plugins/zdecision/skills/zdecision/SKILL.md`
- Create: `plugins/zdecision/skills/zdecision/agents/openai.yaml`
- Create: `src/zdecision/agent/__init__.py`
- Create: `src/zdecision/agent/events.py`
- Create: `src/zdecision/agent/db.py`
- Create: `src/zdecision/agent/hooks.py`
- Create: `src/zdecision/agent/mcp_server.py`
- Create: `src/zdecision/agent/repository.py`
- Create: `src/zdecision/agent/cli.py`
- Create: `tests/test_plugin_contract.py`
- Create: `tests/test_event_ledger.py`
- Create: `tests/test_repository.py`
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_gate1_plugin_smoke.py`

**Interfaces:**

- Produces:
  `HookInvocation.from_dict(value) -> HookInvocation`,
  `AgentDatabase.record_hook(invocation) -> AgentEvent`,
  `handle_hook(raw, database, clock) -> HookResponse`, and the executable
  `zdecision-agent` with `hook`, `mcp`, `status`, and a feasibility-only
  `test-repository` configuration subcommand.
- Consumes: `canonical_json_bytes` and `private_state_root` from the existing
  codebase.
- Preserves: only normalized, allowlisted event facts; the complete Hook input
  is never stored.

The integration test modules are opt-in with
`ZDECISION_LIVE_ACCEPTANCE=1`; ordinary discovery skips them. A skipped live
test never counts as a passed Gate—the explicit acceptance steps below remain
required.

- [ ] **Step 1: Add failing Plugin package contract tests**

  Add tests that load the marketplace, manifest, `.mcp.json`, Hook file, and
  Skill as data. Require one plugin named `zdecision`, manifest paths beginning
  with `./`, one local MCP server invoking `zdecision-agent mcp`, and exactly
  these Hook keys:

  ```python
  EXPECTED_HOOKS = {
      "SessionStart",
      "UserPromptSubmit",
      "PostToolUse",
      "Stop",
      "SessionEnd",
  }

  self.assertEqual(set(hooks_document["hooks"]), EXPECTED_HOOKS)
  self.assertEqual(mcp_document["zdecision-local"]["command"], "zdecision-agent")
  self.assertEqual(mcp_document["zdecision-local"]["args"], ["mcp"])
  ```

  Also assert that disabling the marketplace entry changes only Plugin
  availability; no repository `AGENTS.md` is required by the package. A Hook
  from an unregistered repository must return safely without creating an Event
  Ledger row.

- [ ] **Step 2: Add failing Event Ledger, repository, privacy, and replay tests**

  Use an explicit temporary database path. Cover all five event kinds, invalid
  Session/Turn IDs, corrupt JSON, duplicate delivery, bounded safe facts, and a
  sentinel present in `prompt`, `tool_input`, `tool_response`, and transcript
  content. After recording, search both SQL values and the database bytes and
  assert the sentinel is absent.

  ```python
  first = handle_hook(raw_hook, database=db, clock=fixed_clock)
  second = handle_hook(raw_hook, database=db, clock=fixed_clock)

  self.assertEqual(first.event_id, second.event_id)
  self.assertEqual(db.count_events(), 1)
  self.assertNotIn(secret_sentinel.encode(), database_path.read_bytes())
  ```

  In `tests/test_repository.py`, cover HTTPS and SCP-style remotes, Unicode and
  whitespace rejection, embedded credentials, detached HEAD, non-Git paths,
  command timeout, and stable credential-free repository IDs.

- [ ] **Step 3: Run the two focused tests and observe the missing package**

  Run:

  ```bash
  .venv/bin/python -m unittest tests.test_plugin_contract -v
  .venv/bin/python -m unittest tests.test_event_ledger -v
  .venv/bin/python -m unittest tests.test_repository -v
  ```

  Expected: both fail because the Plugin files and `zdecision.agent` package do
  not exist.

- [ ] **Step 4: Add the executable and MCP dependency**

  Add this project entry point and dependency without importing `mcp` from
  `zdecision.agent.cli` until the `mcp` branch executes:

  ```toml
  dependencies = ["mcp>=1.28,<2"]

  [project.scripts]
  zdecision = "zdecision.cli:main"
  zdecision-agent = "zdecision.agent.cli:main"
  ```

  Refresh the editable environment with:

  ```bash
  .venv/bin/pip install -e .
  ```

- [ ] **Step 5: Create the exact Plugin metadata**

  Use this minimal manifest and keep the optional production listing fields out
  of the feasibility package:

  ```json
  {
    "name": "zdecision",
    "version": "0.1.0",
    "description": "Automatically capture, review, and recall formal decisions",
    "skills": "./skills/",
    "mcpServers": "./.mcp.json",
    "hooks": "./hooks/hooks.json"
  }
  ```

  The repo marketplace entry must point to `./plugins/zdecision`, use
  `installation: AVAILABLE`, `authentication: ON_INSTALL`, and category
  `Productivity`. The Hook file invokes `zdecision-agent hook` for all five
  events. Set `additionalContextLimit` to `4000` only on `SessionStart` and
  `UserPromptSubmit`; later Recall code remains responsible for the stricter
  eight-Decision and 10,000-byte application limit.

- [ ] **Step 6: Implement strict normalized Hook values**

  In `events.py`, implement frozen values with this public shape:

  ```python
  HookEventName = Literal[
      "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"
  ]

  @dataclass(frozen=True)
  class HookInvocation:
      event_name: HookEventName
      session_id: str
      turn_id: str | None
      cwd: str
      occurred_at: str
      repository_id: str | None
      worktree_root: str | None
      branch: str | None
      head_commit: str | None
      source: str | None
      tool_name: str | None
      safe_fact: Mapping[str, object]
      input_digest: str

      @classmethod
      def from_dict(
          cls, value: Mapping[str, object], *, occurred_at: str
      ) -> "HookInvocation": ...

  @dataclass(frozen=True)
  class HookResponse:
      event_id: str
      output: Mapping[str, object]

  @dataclass(frozen=True)
  class AgentEvent:
      event_id: str
      invocation: HookInvocation
      state: Literal[
          "recorded", "processing", "consumed", "deferred",
          "failed_retryable", "failed_terminal"
      ]
      failure_code: str | None

  @dataclass(frozen=True)
  class RepositorySnapshot:
      repository_id: str
      worktree_root: str
      branch: str | None
      head_commit: str

  @dataclass(frozen=True)
  class TestRepositoryMapping:
      repository_id: str
      product_id: str
      product_name: str
      enabled: bool
  ```

  Require `session_id`, `cwd`, and `hook_event_name`; require `turn_id` only
  where the official event supplies it. Accept unknown host fields but discard
  them. For `PostToolUse`, retain only tool name plus allowlisted exit status or
  Git/validation classification; never retain arguments or output. Derive
  `input_digest` and `event_id` from canonical normalized safe input fields,
  excluding the locally assigned `occurred_at` and all discarded text. On
  replay, retain the first stored occurrence time.

  Add `RepositoryResolver.resolve(cwd) -> RepositorySnapshot | None`. Invoke
  Git with argument arrays under one 80-ms total deadline; normalize
  HTTPS and SCP-style remotes to a credential-free host/path, strip a terminal
  `.git`, and derive `repo_<32-hex>` from canonical normalized remote bytes.
  Persist the repository ID, canonical worktree root, branch, and HEAD only;
  never persist a credential-bearing remote. A timeout or non-Git directory
  records `None` and still lets the Hook continue. Cover this behavior in
  `tests/test_repository.py` using temporary repositories.

- [ ] **Step 7: Implement the SQLite Event Ledger**

  `AgentDatabase.open(path)` creates parent directories, enables foreign keys,
  WAL, and `busy_timeout`, and applies a transactionally versioned schema. The
  initial table is:

  ```sql
  CREATE TABLE events (
      event_id TEXT PRIMARY KEY,
      event_type TEXT NOT NULL,
      occurred_at TEXT NOT NULL,
      session_id TEXT NOT NULL,
      turn_id TEXT,
      cwd TEXT NOT NULL,
      repository_id TEXT,
      worktree_root TEXT,
      branch TEXT,
      head_commit TEXT,
      safe_fact_json BLOB NOT NULL,
      input_digest TEXT NOT NULL,
      state TEXT NOT NULL CHECK (
          state IN ('recorded','processing','consumed','deferred',
                    'failed_retryable','failed_terminal')
      ),
      failure_code TEXT
  );

  CREATE TABLE feasibility_repository_mappings (
      repository_id TEXT PRIMARY KEY,
      product_id TEXT NOT NULL,
      product_name TEXT NOT NULL,
      enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
  );
  ```

  Add exact methods:

  ```python
  class AgentDatabase:
      @classmethod
      def open(cls, path: Path) -> "AgentDatabase": ...
      def record_hook(self, invocation: HookInvocation) -> AgentEvent: ...
      def get_event(self, event_id: str) -> AgentEvent | None: ...
      def list_events(self, session_id: str) -> tuple[AgentEvent, ...]: ...
      def latest_open_boundary(self, cwd: str) -> tuple[str, str] | None: ...
      def put_test_repository_mapping(
          self, mapping: TestRepositoryMapping
      ) -> None: ...
      def get_repository_mapping(
          self, repository_id: str
      ) -> TestRepositoryMapping | None: ...
      def close(self) -> None: ...
  ```

  Use `INSERT ... ON CONFLICT DO NOTHING`, then compare every canonical field
  on replay. A same ID with different normalized bytes is a conflict.

- [ ] **Step 8: Implement Hook and local MCP entry points**

  `zdecision-agent hook` reads one JSON object from stdin, records it, prints
  the Hook JSON output, and exits zero. Invalid input prints one bounded
  `systemMessage` without echoing input and exits zero so Codex continues.

  Build the MCP server lazily with `FastMCP("zdecision-local")` and expose only:

  ```python
  report_work_state(
      status: Literal[
          "exploring", "implementing", "awaiting_user",
          "validation_failed", "milestone_complete"
      ],
      validation: Literal["passed", "failed", "not_applicable", "unknown"],
      unresolved_blockers: list[str],
  ) -> dict[str, object]

  zdecision_status() -> dict[str, object]
  submit_current_boundary() -> dict[str, object]
  ```

  The first and third tools resolve the one current open local Session for the
  MCP process launch `cwd` and bind its most recent turn-scoped Hook fact. A
  successful validation normally supplies that current Turn through
  `PostToolUse`; zero/multiple Sessions or no current Turn returns
  `session_binding_ambiguous` and creates no report. Tool inputs contain no
  Candidate or transcript fields. `submit_current_boundary` records a manual
  strong-trigger fact only; it does not run Capture in this task. Gate 1 must
  prove that the bundled MCP process launch `cwd` is the active repository; do
  not assume it without the live result.

  The internal `test-repository enable` CLI resolves the supplied checkout,
  canonicalizes `product_name` with the existing ID functions, and writes the
  exact `TestRepositoryMapping`. `disable` flips only `enabled`; it does not
  delete local Gate evidence. This command is labelled feasibility-only in its
  help and is never exposed as an MCP tool or end-user Skill action.

- [ ] **Step 9: Run focused tests and the existing Skill contract**

  Run:

  ```bash
  .venv/bin/python -m unittest tests.test_plugin_contract tests.test_event_ledger tests.test_repository tests.test_skill_contract -v
  ```

  Expected: all pass, and the existing manual V1 Skill tests remain unchanged.

- [ ] **Step 10: Perform Gate 1 live installation acceptance**

  This is an explicit human/app acceptance and must not be hidden in a unit
  test:

  1. Run `pipx install --editable . --force`, confirm `zdecision-agent` is on
     the GUI-visible user `PATH`, and restart the desktop app after any PATH
     change.
  2. Register the chosen local test repository with
     `zdecision-agent test-repository enable --cwd <path> --product-name <name>`;
     this derives both IDs with existing canonical functions and is removed
     from the product flow when Packet C supplies server-authoritative mapping.
  3. Run `codex plugin marketplace add <absolute-repository-path>`.
  4. Restart the ChatGPT desktop app, install `zdecision`, and approve its Hook
     trust prompt.
  5. Open an arbitrary registered test repository that has no ZDecision
     `AGENTS.md`; create one new Codex conversation and exercise all five Hook
     boundaries.
  6. Call the `zdecision_status` MCP tool and verify the same Session's five
     event kinds are present.
  7. Disable the Plugin, start another conversation, and verify no new Agent
     event or MCP tool is available.
  8. Repeat the install/discovery smoke test in Codex CLI.

  Store the Gate result in the local Agent database; do not commit Session IDs
  or local paths.

- [ ] **Step 11: Commit Gate 1**

  ```bash
  git add pyproject.toml .agents/plugins plugins/zdecision src/zdecision/agent tests/test_plugin_contract.py tests/test_event_ledger.py tests/test_repository.py tests/integration
  git commit -m "feat: add installable zdecision plugin runtime"
  ```

---

### Task 2: Hook latency, singleton Worker, retry, and active-session lease

**Files:**

- Modify: `src/zdecision/agent/db.py`
- Modify: `src/zdecision/agent/hooks.py`
- Modify: `src/zdecision/agent/cli.py`
- Create: `src/zdecision/agent/worker.py`
- Create: `tests/test_worker.py`
- Create: `tests/test_hook_latency.py`
- Modify: `tests/integration/test_gate1_plugin_smoke.py`

**Interfaces:**

- Consumes: Task 1 `AgentDatabase`, `HookInvocation`, and `zdecision-agent`.
- Produces:
  `wake_worker(database_path) -> None`,
  `Worker.run_once(now) -> WorkerCycle`, and active-session sync leases used by
  the later sync client.

- [ ] **Step 1: Add failing Worker state and crash-retry tests**

  Cover 100 unique rapid events, duplicate delivery, two simultaneous wakeups,
  a crash after claiming an event, expired processing lease recovery, clean
  Session end, and a retryable fake central outage. Assert each unique event is
  consumed exactly once.

  ```python
  with ThreadPoolExecutor(max_workers=8) as pool:
      tuple(pool.map(record_and_wake, hook_inputs))

  self.assertEqual(database.count_unique_events(), 100)
  self.assertEqual(processor.seen_event_ids, set(expected_event_ids))
  self.assertEqual(max_active_workers.value, 1)
  ```

- [ ] **Step 2: Add the failing latency and sync-lease tests**

  Measure at least 200 in-process Hook invocations after warm-up. Sort elapsed
  milliseconds and use index `ceil(0.95 * n) - 1`; require p95 at most 150 ms.
  The fake network client must sleep or fail only in the Worker, never on the
  Hook call stack.

  Add an injected clock test proving one active Session causes the fake sync
  cursor to advance within 60 seconds without another Prompt; `SessionEnd`
  closes that lease and allows the Worker to exit after the queue drains.

- [ ] **Step 3: Run tests and observe missing Worker behavior**

  ```bash
  .venv/bin/python -m unittest tests.test_worker tests.test_hook_latency -v
  ```

  Expected: fail because Worker tables and process lifecycle do not exist.

- [ ] **Step 4: Extend the database with claim and session-lease transactions**

  Add schema-version 2 tables:

  ```sql
  CREATE TABLE session_leases (
      session_id TEXT PRIMARY KEY,
      cwd TEXT NOT NULL,
      renewed_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      ended_at TEXT
  );

  CREATE TABLE worker_state (
      singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
      owner_pid INTEGER,
      lease_expires_at TEXT
  );

  CREATE TABLE sync_probe (
      singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
      cursor INTEGER NOT NULL,
      updated_at TEXT
  );
  ```

  Add `renew_session`, `end_session`, `claim_events`, `consume_event`,
  `fail_event`, `requeue_expired_claims`, and `active_session_leases`. Claim a
  bounded batch in one `BEGIN IMMEDIATE` transaction and attach a processing
  deadline before returning rows.

- [ ] **Step 5: Implement the singleton Worker**

  Use these dependency boundaries so Task 2 tests never call a real service:

  ```python
  class EventProcessor(Protocol):
      def process(self, event: AgentEvent) -> None: ...

  class SyncPoller(Protocol):
      def poll(self, current_cursor: int) -> int: ...

  @dataclass(frozen=True)
  class WorkerConfig:
      claim_limit: int = 32
      processing_lease_seconds: float = 30.0
      session_lease_seconds: float = 120.0
      poll_interval_seconds: float = 60.0
      idle_grace_seconds: float = 2.0

  @dataclass(frozen=True)
  class WorkerCycle:
      claimed: int
      consumed: int
      deferred: int
      failed_retryable: int
      sync_cursor: int
      active_sessions: int

  class Worker:
      def run_once(self, now: datetime) -> WorkerCycle: ...
      def run_until_idle(self) -> None: ...
  ```

  Acquire a non-blocking macOS `fcntl.flock` on
  `<agent-root>/worker.lock` before processing. A losing process exits zero.
  An event exception records a bounded safe code and retry deadline; it never
  deletes the event.

- [ ] **Step 6: Wake the Worker without blocking Hooks**

  After the Hook transaction commits, call:

  ```python
  subprocess.Popen(
      [sys.executable, "-m", "zdecision.agent.cli", "worker"],
      stdin=subprocess.DEVNULL,
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
      start_new_session=True,
      close_fds=True,
  )
  ```

  Do not wait for the process. `SessionStart` opens/renews a lease,
  `SessionEnd` closes it, and every other Hook renews the existing lease. The
  Worker owns all polling and network behavior.

- [ ] **Step 7: Run Task 2 focused and Gate 1 regression tests**

  ```bash
  .venv/bin/python -m unittest tests.test_worker tests.test_hook_latency tests.test_event_ledger tests.test_repository tests.test_plugin_contract -v
  ```

  Expected: all pass; the measured p95 value is printed by the latency test and
  is at most 150 ms.

- [ ] **Step 8: Run Gate 2 live lifecycle acceptance**

  With the installed Plugin from Task 1, send 100 rapid Hook fixtures through
  the actual command, kill the active Worker once, and let the next Hook wake
  it. Verify one process owns the lock, all unique events reach terminal
  `consumed`, unfinished work is reclaimed, and a deliberately unavailable
  fake sync endpoint does not delay or fail the Codex Turn.

  Keep one real Codex Session active for 60 seconds and verify the injected
  sync probe advances without a new Prompt. If the Worker cannot remain alive,
  stop and implement only the approved macOS user-service fallback; if that
  also fails, record `local_background_runtime_unavailable` and stop the plan.

- [ ] **Step 9: Commit Gate 2**

  ```bash
  git add src/zdecision/agent tests/test_worker.py tests/test_hook_latency.py tests/integration/test_gate1_plugin_smoke.py
  git commit -m "feat: run bounded local agent worker"
  ```

---

### Task 3: Typed app-server JSONL client and frozen model profile

**Files:**

- Modify: `src/zdecision/agent/db.py`
- Create: `src/zdecision/app_server/__init__.py`
- Create: `src/zdecision/app_server/models.py`
- Create: `src/zdecision/app_server/jsonl.py`
- Create: `src/zdecision/app_server/gateway.py`
- Create: `tests/test_app_server_gateway.py`

**Interfaces:**

- Consumes: Task 2 Agent database and the installed `codex` executable.
- Produces: `AppServerGateway`, `FeasibilityModelProfile`, completed source
  boundaries, ephemeral forks, and structured Turn receipts for Task 4.

- [ ] **Step 1: Add failing JSONL handshake and request tests**

  Drive the client with fake stdin/stdout queues. Require exactly one
  `initialize`, then `initialized`; monotonic request IDs; response/error
  correlation; notification collection; timeout; malformed JSON; EOF; bounded
  stderr; clean terminate; and no credential value in exceptions.

  ```python
  client.initialize()
  result = client.request("model/list", {"limit": 100, "includeHidden": True})

  self.assertEqual(sent[0]["method"], "initialize")
  self.assertEqual(sent[1], {"method": "initialized", "params": {}})
  self.assertEqual(sent[2]["method"], "model/list")
  ```

- [ ] **Step 2: Add failing Gateway contract tests**

  Cover `thread/read(includeTurns=True)`, completed-boundary validation,
  `thread/fork(ephemeral=True, lastTurnId=...)`, `model/list`, explicit model and
  effort on `turn/start`, output schema, final `turn/completed`, and rejection
  of in-progress or unknown source Turns.

  Freeze a fixture catalog with two models. Prove source profile reuse only
  when the exact model and effort are returned as supported; otherwise choose
  the one `isDefault` model and its returned `defaultReasoningEffort`. Never
  hard-code a model slug.

- [ ] **Step 3: Run the focused test and observe missing Gateway symbols**

  ```bash
  .venv/bin/python -m unittest tests.test_app_server_gateway -v
  ```

  Expected: fail because `zdecision.app_server` does not exist.

- [ ] **Step 4: Implement strict app-server values**

  ```python
  @dataclass(frozen=True)
  class FeasibilityModelProfile:
      profile_id: str
      model_id: str
      reasoning_effort: str
      discovery_digest: str
      discovered_at: str

  @dataclass(frozen=True)
  class SourceBoundary:
      thread_id: str
      turn_id: str
      cwd: str
      status: Literal["completed"]
      model_id: str | None
      reasoning_effort: str | None

  @dataclass(frozen=True)
  class AppServerTurnReceipt:
      thread_id: str
      turn_id: str
      status: Literal["completed"]
      structured_output: Mapping[str, object]
      output_sha256: str
      model_profile_id: str
  ```

  Validate every response before constructing a value. `profile_id` is the
  stable canonical digest of exact `model_id`, effort, and discovery digest.

- [ ] **Step 5: Implement the JSONL client and controlled-process transport**

  Launch only this predeclared fallback command:

  ```python
  ["codex", "app-server", "--listen", "stdio://"]
  ```

  Keep transport behind this protocol:

  ```python
  class AppServerTransport(Protocol):
      def send(self, message: Mapping[str, object]) -> None: ...
      def receive(self, timeout_seconds: float) -> Mapping[str, object]: ...
      def close(self) -> None: ...
  ```

  Use argument arrays, text UTF-8 JSONL, one reader thread, one bounded stderr
  tail, and a request map protected by a lock. Send JSON-RPC objects without a
  `jsonrpc` field. For unexpected approval/server requests, return a decline or
  cancel response and fail the structured Turn rather than hanging.

- [ ] **Step 6: Implement `AppServerGateway`**

  ```python
  class AppServerGateway:
      def read_completed_boundary(
          self, thread_id: str, turn_id: str
      ) -> SourceBoundary: ...

      def discover_and_freeze_profile(
          self, boundary: SourceBoundary
      ) -> FeasibilityModelProfile: ...

      def fork_ephemeral(
          self, thread_id: str, last_turn_id: str
      ) -> str: ...

      def run_structured_turn(
          self,
          thread_id: str,
          prompt: str,
          output_schema: Mapping[str, object],
          profile: FeasibilityModelProfile,
          cwd: str,
      ) -> AppServerTurnReceipt: ...
  ```

  `discover_and_freeze_profile` writes one immutable Agent-database record and
  replays it for all later fixture runs. A conflicting discovery digest stops
  Gate 3. `run_structured_turn` sends `model` and `effort` on every call, uses a
  read-only sandbox and non-interactive approval policy, and waits for the
  exact Turn's terminal notification.

- [ ] **Step 7: Implement route selection without inventing a host API**

  Define `connect(host_transport: AppServerTransport | None)`. When the current
  host supplies an explicitly supported transport, probe it first. When it
  supplies none—as current public Plugin contracts permit—record
  `host_transport_unavailable` and launch the controlled process. Do not infer
  a socket, inspect desktop private files, or parse the source transcript.

- [ ] **Step 8: Run Gateway tests**

  ```bash
  .venv/bin/python -m unittest tests.test_app_server_gateway -v
  ```

  Expected: all fake-protocol, validation, profile, and failure tests pass.

- [ ] **Step 9: Commit the typed Gateway**

  ```bash
  git add src/zdecision/agent/db.py src/zdecision/app_server tests/test_app_server_gateway.py
  git commit -m "feat: add typed codex app server gateway"
  ```

---

### Task 4: Separate eligibility fork and automated two-stage Capture

**Files:**

- Create: `src/zdecision/capture/eligibility.py`
- Create: `src/zdecision/capture/prompt_contracts/capture-eligibility-v1.md`
- Create: `src/zdecision/app_server/capture_runner.py`
- Modify: `src/zdecision/agent/db.py`
- Modify: `src/zdecision/agent/cli.py`
- Create: `tests/test_automated_capture.py`
- Create: `tests/integration/test_gate3_live_app_server.py`

**Interfaces:**

- Consumes: Task 3 `AppServerGateway`; existing `CaptureService.prepare`,
  `attach_fork`, `attach_stage_turn`, `complete_inventory`, and
  `complete_extraction`; existing template schemas and validation.
- Produces:
  `AutomatedCaptureRunner.run(...) -> AutomatedCaptureResult` and
  `zdecision-agent gate3` for the live blocking acceptance.

- [ ] **Step 1: Add failing strict eligibility-value tests**

  Require exact fields, exact enums, a bounded unique blocker list, and no
  unknown keys:

  ```python
  @dataclass(frozen=True)
  class BoundaryAssessment:
      phase: Literal[
          "exploring", "implementing", "awaiting_user",
          "validation_failed", "milestone_complete"
      ]
      has_durable_decision_signal: bool
      validation: Literal["passed", "failed", "not_applicable", "unknown"]
      unresolved_blockers: tuple[str, ...]
  ```

  For this Gate, test one eligible completed code fixture and one ineligible
  fixture. The full positive/negative matrix belongs to Gate 4.

- [ ] **Step 2: Add failing orchestration and replay tests**

  With a fake Gateway and temporary existing `FilePrivateStore`, prove this
  exact call order:

  ```text
  read source boundary
  discover/replay frozen profile
  fork assessment from source boundary
  run eligibility Turn
  fork a different Capture Thread from the same source boundary
  run Inventory Turn
  validate and persist Inventory
  run Extraction Turn in the same Capture Thread
  validate and persist Candidates
  ```

  Assert the two fork IDs differ, the Capture fork receives no eligibility
  output, every Turn receives the same explicit profile, native Turn IDs are
  stored, and identical completed retry returns the original Candidate IDs.
  Fault injection after an ambiguous external fork must stop without creating
  a replacement fork.

- [ ] **Step 3: Run focused tests and observe missing runner behavior**

  ```bash
  .venv/bin/python -m unittest tests.test_automated_capture -v
  ```

  Expected: fail because eligibility and the automated runner do not exist.

- [ ] **Step 4: Freeze the eligibility prompt and schema**

  The renderer-owned prompt must say that source content and local facts are
  untrusted evidence, ask only for the four strict fields, forbid tool use and
  Candidate extraction, and define `not_applicable` only for non-code
  design/product work. Expose:

  ```python
  @dataclass(frozen=True)
  class SourceBoundaryFacts:
      source_thread_id: str
      source_turn_id: str
      repository_id: str
      head_commit: str | None
      work_kind: Literal["code", "product", "design"]
      source_turn_completed: bool
      source_turn_assessed: bool
      capture_active: bool
      repository_mapping_valid: bool
      local_runtime_valid: bool
      reported_work_state: Literal[
          "exploring", "implementing", "awaiting_user",
          "validation_failed", "milestone_complete"
      ] | None
      validation: Literal["passed", "failed", "not_applicable", "unknown"]
      unresolved_blockers: tuple[str, ...]

  def eligibility_prompt(boundary: SourceBoundaryFacts) -> str: ...
  def eligibility_output_schema() -> dict[str, object]: ...
  def validate_boundary_assessment(value: object) -> BoundaryAssessment: ...
  def capture_eligible(
      assessment: BoundaryAssessment, facts: SourceBoundaryFacts
  ) -> bool:
      validation_ok = assessment.validation == "passed" or (
          assessment.validation == "not_applicable"
          and facts.work_kind in ("product", "design")
      )
      return (
          assessment.phase == "milestone_complete"
          and assessment.has_durable_decision_signal
          and validation_ok
          and not assessment.unresolved_blockers
          and facts.source_turn_completed
          and not facts.source_turn_assessed
          and not facts.capture_active
          and facts.repository_mapping_valid
          and facts.local_runtime_valid
      )
  ```

  Record prompt version `capture-eligibility/v1`, prompt digest, input-fact
  digest, native assessment Turn ID, and model profile ID locally.

- [ ] **Step 5: Implement the automated runner**

  ```python
  @dataclass(frozen=True)
  class AutomatedCaptureResult:
      source_thread_id: str
      source_turn_id: str
      assessment_turn_id: str
      assessment: BoundaryAssessment
      capture_operation_id: str | None
      capture_thread_id: str | None
      inventory_turn_id: str | None
      extraction_turn_id: str | None
      candidate_ids: tuple[str, ...]
      model_profile_id: str

  class AutomatedCaptureRunner:
      def run(
          self,
          session_id: str,
          source_turn_id: str,
          template_id: str = "business",
      ) -> AutomatedCaptureResult: ...
  ```

  Derive one `automated_capture_id` from the Session ID, source Turn ID,
  mapped product, eligibility prompt digest, template snapshot digest, and
  frozen profile ID. Resolve product only through the enabled repository
  mapping created in Task 1; the command and runner do not accept a product
  override. Add SQLite `automated_capture_runs` and
  `boundary_assessments` records with compare-and-swap state transitions. A
  completed run is returned before any new app-server request. If an external
  fork may have succeeded but its native ID was not persisted, mark the run
  `ambiguous` and stop; never create a replacement fork.

  If assessment is not eligible, return with all Capture fields `None` and do
  not call `CaptureService.prepare`. If eligible, create the separate fresh
  fork and drive the existing Capture state machine without weakening its
  limits or retry rules. Never rebuild a transcript from Hook files.

- [ ] **Step 6: Add the live Gate 3 command**

  Add this diagnostic-only command; its output contains IDs, statuses, counts,
  and digests, never Candidate text or source content:

  ```text
  zdecision-agent gate3 \
    --session-id <real-hook-session-id> \
    --turn-id <completed-source-turn-id>
  ```

  It uses the supported host transport when explicitly supplied by the host;
  otherwise it launches the controlled stdio process. It writes the detailed
  receipt only to the local Agent database.

- [ ] **Step 7: Run the focused Gate 3 tests and existing Capture regression**

  ```bash
  .venv/bin/python -m unittest tests.test_automated_capture tests.test_app_server_gateway tests.test_capture tests.test_inventory tests.test_templates -v
  ```

  Expected: all pass, including existing 100-signal, 256-KiB Inventory,
  20-Candidate, immutable retry, and fresh-fork contracts.

- [ ] **Step 8: Perform the real Gate 3 blocking acceptance**

  From a completed development Turn in the installed Plugin's real Hook
  Session:

  1. Run the Gate 3 command with that native Session and Turn ID.
  2. Verify `thread/read` sees the completed boundary.
  3. Verify the assessment fork and Capture fork are distinct and the source
     Session is unchanged.
  4. Verify model discovery is stored once and every `turn/start` receipt names
     the same exact model and effort.
  5. Verify Inventory and Extraction native Turn IDs plus one validated
     Candidate set or valid zero-Candidate result are stored locally.
  6. Retry the same command and verify no second Capture operation, fork, Turn,
     or Candidate set is created.

  If the host route is unavailable, that result is expected evidence and the
  controlled-process route is exercised. If the controlled process also cannot
  read/fork the Hook Session, record the blocking failure and stop.

- [ ] **Step 9: Run the single completion verification set**

  Run exactly once after the live Gate passes:

  ```bash
  .venv/bin/python -m unittest tests.test_plugin_contract tests.test_event_ledger tests.test_repository tests.test_worker tests.test_hook_latency tests.test_app_server_gateway tests.test_automated_capture -v
  .venv/bin/python -m unittest discover -s tests -v
  git diff --check
  ```

  Expected: focused tests pass, the full suite reports zero failures/errors,
  and `git diff --check` prints nothing.

- [ ] **Step 10: Commit Gate 3 and stop for the next plan**

  ```bash
  git add src/zdecision/capture/eligibility.py src/zdecision/capture/prompt_contracts/capture-eligibility-v1.md src/zdecision/app_server/capture_runner.py src/zdecision/agent/db.py src/zdecision/agent/cli.py tests/test_automated_capture.py tests/integration/test_gate3_live_app_server.py
  git commit -m "feat: automate app server capture from hook sessions"
  ```

  Record Gate 1, 2, and 3 results in the implementation handoff. Do not begin
  eligibility fixture expansion, reconciliation, central service, Web UI, or
  Recall until the user accepts the Gate 3 evidence and the Packet B plan is
  written.
