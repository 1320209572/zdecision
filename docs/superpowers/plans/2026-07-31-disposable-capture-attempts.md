# Disposable Capture Attempts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unsupported native-task adoption with durable Capture
operations, disposable whole-pipeline attempts, generation fencing, and one
Candidate-effect commit.

**Architecture:** The page request freezes one immutable source input. A local
SQLite `CaptureOperationStore` owns that business operation and permits
multiple persisted, read-only Codex execution attempts. An attempt runs
Inventory and Extraction in one fresh fork, stores its validated structured
result, and may win one operation-level CAS; reconciliation uses the same
attempt semantics but keeps its separate request-owned persistence boundary.

**Tech Stack:** Python 3.11+, SQLite/WAL, dataclasses, Codex app-server JSONL,
`unittest`, FastAPI test client.

## Global Constraints

- Work directly on the existing local `main`; do not create a worktree or
  branch.
- Preserve every existing Task 9 worktree change. Do not reset, restore, or
  rewrite unrelated history.
- Candidate generation remains authorized only by the page's
  **更新候选决策** action.
- Raw Session content, Prompts, code, diffs, tool output, and
  `transcript_path` JSONL never enter central storage or Git.
- Packet 1 uses only official app-server `thread/read`,
  `thread/fork(lastTurnId)`, `turn/start`, and `thread/archive` for source and
  execution behavior.
- `threadSource` and `clientUserMessageId` must not participate in correctness.
- One attempt always contains both Inventory and Extraction. Any unknown stage
  result abandons the whole attempt.
- Model execution may be at-least-once; Capture and Candidate effects must be
  exactly-once through generation fencing and atomic commits.
- Existing extractor-v1/v2 records remain readable and are never silently
  migrated.
- Stop after one focused suite, one full suite, packaging verification, and
  one live fault-injected app-server acceptance. Do not start another broad
  review.

---

### Task 1: Preserve and land the accepted Task 9 cleanup

**Files:**

- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `plugins/zdecision/skills/zdecision/SKILL.md`
- Modify: `src/zdecision/agent/cli.py`
- Modify: `src/zdecision/agent/db.py`
- Modify: `src/zdecision/agent/events.py`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `src/zdecision/agent/service.py`
- Delete: `src/zdecision/app_server/capture_runner.py`
- Delete: `src/zdecision/capture/eligibility.py`
- Delete:
  `src/zdecision/capture/prompt_contracts/capture-eligibility-v1.md`
- Delete: `tests/integration/test_gate3_live_app_server.py`
- Delete: `tests/test_automated_capture.py`
- Modify: `tests/test_event_ledger.py`
- Modify: `tests/test_plugin_contract.py`
- Modify: `tests/test_requested_capture.py`
- Modify: `tests/test_skill_contract.py`
- Preserve untracked for Task 6:
  `tests/integration/test_on_demand_capture_core.py`

**Interfaces:**

- Consumes: the already implemented Packet 1 page-request path.
- Produces: one clean commit that removes only the rejected zero-touch path;
  the unsupported native-attempt implementation remains temporarily intact for
  replacement in Tasks 2–5.

- [ ] **Step 1: Record the exact pre-task worktree**

Run:

```bash
git status --short --branch
git diff --check
```

Expected: the Task 9 paths listed above are dirty, the Gate A–C integration
test is untracked, and `git diff --check` exits `0`.

- [ ] **Step 2: Run the cleanup-focused tests**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_event_ledger \
  tests.test_plugin_contract \
  tests.test_skill_contract \
  tests.test_requested_capture -v
```

Expected: all tests pass. These tests prove that lifecycle observation remains
and that no Skill, MCP tool, or CLI path can authorize Candidate extraction.

- [ ] **Step 3: Stage only the cleanup**

Run:

```bash
git add \
  README.md \
  docs/architecture.md \
  plugins/zdecision/skills/zdecision/SKILL.md \
  src/zdecision/agent/cli.py \
  src/zdecision/agent/db.py \
  src/zdecision/agent/events.py \
  src/zdecision/agent/mcp_server.py \
  src/zdecision/agent/service.py \
  src/zdecision/app_server/capture_runner.py \
  src/zdecision/capture/eligibility.py \
  src/zdecision/capture/prompt_contracts/capture-eligibility-v1.md \
  tests/integration/test_gate3_live_app_server.py \
  tests/test_automated_capture.py \
  tests/test_event_ledger.py \
  tests/test_plugin_contract.py \
  tests/test_requested_capture.py \
  tests/test_skill_contract.py
git diff --cached --check
git status --short
```

Expected: `tests/integration/test_on_demand_capture_core.py` remains untracked
and is not present in `git diff --cached --name-only`.

- [ ] **Step 4: Commit the cleanup**

Run:

```bash
git commit -m "refactor: retire zero-touch candidate capture"
```

Expected: one commit containing only the staged Task 9 cleanup.

---

### Task 2: Add durable Capture operations and fenced execution attempts

**Files:**

- Create: `src/zdecision/capture/on_demand.py`
- Create: `src/zdecision/agent/capture_operation_store.py`
- Modify: `src/zdecision/capture/service.py`
- Modify: `src/zdecision/ids.py`
- Create: `tests/test_capture_operation.py`
- Modify: `tests/test_capture.py`

**Interfaces:**

- Produces:
  `FrozenCaptureInput.create(request_id, repository_id, source_key,
  session_id, cwd, lineage, previous_handled_turn_id, upper_turn_id,
  source_fingerprint, product, template, model_profile_id, model_id,
  reasoning_effort, model_discovery_digest, model_discovered_at) ->
  FrozenCaptureInput`,
  `ValidatedCaptureResult.create(frozen, inventory_output,
  extraction_output) -> ValidatedCaptureResult`,
  `CaptureOperationStore.open(path) -> CaptureOperationStore`,
  `ensure_operation(frozen) -> CaptureOperation`,
  `operation_for_source(request_id, source_key) -> CaptureOperation | None`,
  `begin_attempt(operation_id, started_at) -> ExecutionAttempt`,
  `attach_thread(attempt_id, thread_id) -> ExecutionAttempt`,
  `attach_turn(attempt_id, stage, turn_id) -> ExecutionAttempt`,
  `abandon_attempt(attempt_id, failure_code, finished_at)`,
  `store_validated_attempt(attempt_id, result, finished_at)`,
  `commit_attempt(attempt_id) -> CaptureCommit`,
  `fail_operation_terminal(operation_id, failure_code) -> CaptureOperation`,
  `committed_result(operation_id) -> ValidatedCaptureResult | None`,
  `pending_archives() -> tuple[ExecutionAttempt, ...]`, and
  `mark_archived(attempt_id)`.
- Consumes later: Tasks 4 and 6.

- [ ] **Step 1: Write failing frozen-input identity tests**

Create `tests/test_capture_operation.py` with a fixture using exact values for
request, repository, source key, Session, lineage, upper Turn, fingerprint,
template snapshot, model profile, and protocol revision. Assert:

```python
def test_operation_identity_binds_every_frozen_input(self) -> None:
    first = frozen_input()
    replay = frozen_input()
    changed = frozen_input(source_fingerprint="f" * 64)

    self.assertEqual(first.operation_id, replay.operation_id)
    self.assertNotEqual(first.operation_id, changed.operation_id)
    self.assertEqual(3, first.record_version)
```

Also mutate `request_id`, `repository_id`, `source_key`, `session_id`,
`upper_turn_id`, `product`, template digest, model profile ID, and protocol
revision one at a time and assert every mutation changes the operation ID.

- [ ] **Step 2: Write failing attempt-generation and CAS tests**

Define the desired restart-safe behavior:

```python
def test_late_generation_cannot_commit(self) -> None:
    operation = self.store.ensure_operation(frozen_input())
    first = self.store.begin_attempt(operation.operation_id, NOW)
    self.store.abandon_attempt(first.attempt_id, "fork_result_unknown", NOW)
    second = self.store.begin_attempt(operation.operation_id, NOW)

    self.store.store_validated_attempt(
        first.attempt_id, validated_result(claim="old"), NOW
    )
    old_commit = self.store.commit_attempt(first.attempt_id)
    self.assertEqual("superseded", old_commit.attempt.state)
    self.assertIsNone(old_commit.result)

    self.store.store_validated_attempt(
        second.attempt_id, validated_result(claim="new"), NOW
    )
    winner = self.store.commit_attempt(second.attempt_id)
    self.assertEqual("committed", winner.operation.status)
    self.assertEqual("new", winner.result.observations[0].content.claim)
```

Add cases for:

- exact operation reopen after process restart;
- monotonically increasing generations;
- a validated result surviving restart before CAS;
- exact winner replay returning the existing receipt;
- a different late result never replacing the winner;
- an abandoned known-thread attempt appearing in `pending_archives`;
- an unknown-thread attempt requiring no archive.

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_capture_operation -v
```

Expected: fail because `zdecision.capture.on_demand` and
`zdecision.agent.capture_operation_store` do not exist.

- [ ] **Step 4: Implement strict immutable records**

In `src/zdecision/capture/on_demand.py`, define:

```python
CaptureOperationStatus = Literal["open", "committed", "failed_terminal"]
AttemptState = Literal[
    "prepared", "creating_thread", "running", "validated",
    "accepted", "superseded", "abandoned",
]
ArchiveState = Literal["not_applicable", "pending", "archived"]
```

`FrozenCaptureInput` must contain all fields listed in the approved design and
must serialize with exact-field validation. Its operation ID is
`cap_` plus the first 32 hex characters of SHA-256 over canonical JSON that
includes `"protocol": "extractor-v3"`.

`ValidatedCaptureResult` contains:

```text
operation_id
inventory
inventory_sha256
extraction_sha256
observations
result_digest
```

It accepts only a validated `InventoryResult` and `Candidate` tuple whose
capture/source/product identities match the frozen operation.

`CaptureCommit` contains exactly the selected `CaptureOperation`, the selected
`ExecutionAttempt`, and its `ValidatedCaptureResult`. A superseded attempt
returns the already selected operation/result with the losing attempt marked
`superseded`.

- [ ] **Step 5: Extract the reusable Stage 2 validator**

Move the pure validation logic currently in
`CaptureService._validated_candidates` into:

```python
validate_extraction_output(
    operation_id: str,
    source: SourceCheckpoint,
    product: str,
    extraction: object,
) -> tuple[Candidate, ...]
```

Keep all current limits: at most 20 Candidates, 16 KiB canonical bytes per
Candidate, exact fields, exact product, and deterministic ordinal IDs. Make
the legacy extractor-v2 method delegate to this function without changing its
behavior.

- [ ] **Step 6: Implement the SQLite operation store**

Use two tables:

```sql
capture_operations(
  operation_id PRIMARY KEY,
  request_id,
  source_key,
  frozen_json,
  frozen_digest,
  status,
  active_generation,
  winner_generation,
  committed_result_json,
  committed_result_digest
)

capture_execution_attempts(
  attempt_id PRIMARY KEY,
  operation_id,
  generation,
  state,
  thread_id,
  inventory_turn_id,
  extraction_turn_id,
  failure_code,
  validated_result_json,
  validated_result_digest,
  archive_state,
  started_at,
  finished_at,
  UNIQUE(operation_id, generation)
)
```

Every mutating method uses `BEGIN IMMEDIATE`. `commit_attempt` verifies
`generation == active_generation` and `status == 'open'` in the same
transaction that selects the winner. A stale generation becomes
`superseded`. A same-digest replay returns the stored winner.
`store_validated_attempt` may retain a late diagnostic result for an already
abandoned generation, but that generation can only become `superseded`; it can
never become the operation winner.

- [ ] **Step 7: Run operation and legacy Capture tests**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_capture_operation \
  tests.test_capture -v
```

Expected: all tests pass; existing extractor-v2 tests remain unchanged except
for importing the extracted validator.

- [ ] **Step 8: Commit the operation domain**

Run:

```bash
git add \
  src/zdecision/capture/on_demand.py \
  src/zdecision/agent/capture_operation_store.py \
  src/zdecision/capture/service.py \
  src/zdecision/ids.py \
  tests/test_capture_operation.py \
  tests/test_capture.py
git commit -m "feat: persist disposable capture attempts"
```

---

### Task 3: Replace tag recovery with documented app-server primitives

**Files:**

- Modify: `src/zdecision/app_server/gateway.py`
- Modify: `tests/test_app_server_gateway.py`

**Interfaces:**

- Produces:
  `start_disposable_thread(cwd, profile) -> str`,
  `fork_disposable_thread(thread_id, last_turn_id) -> str`,
  `archive_thread(thread_id) -> None`, and the existing
  `run_structured_turn(thread_id, prompt, output_schema, profile, cwd) ->
  AppServerTurnReceipt`.
- Removes after callers migrate:
  `find_thread_by_source`,
  `read_structured_turn_by_client_id`, tagged creation behavior, and
  `clientUserMessageId` response validation.

- [ ] **Step 1: Replace tagged-gateway tests with failing official-payload tests**

Assert exact request payloads:

```python
def test_forks_a_persisted_disposable_thread_at_exact_boundary(self) -> None:
    thread_id = gateway.fork_disposable_thread(SOURCE_THREAD, SOURCE_TURN)
    self.assertEqual(FORK_THREAD, thread_id)
    self.assertEqual(
        ("thread/fork", {
            "threadId": SOURCE_THREAD,
            "lastTurnId": SOURCE_TURN,
        }),
        client.requests[0],
    )

def test_structured_turn_has_no_recovery_tag(self) -> None:
    gateway.run_structured_turn(
        thread_id=FORK_THREAD,
        prompt="Inventory.",
        output_schema={"type": "object"},
        profile=self.profile,
        cwd=str(self.root),
    )
    params = client.requests[0][1]
    self.assertNotIn("clientUserMessageId", params)
    self.assertNotIn("threadSource", params)
```

Add tests for persistent `thread/start`, `thread/archive`, wrong
`forkedFromId`, an ephemeral response when persistence was requested, and an
archive JSON-RPC error.

- [ ] **Step 2: Run Gateway tests and verify RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_app_server_gateway -v
```

Expected: fail because the three disposable-task methods do not exist and the
old methods still send tags.

- [ ] **Step 3: Implement the minimal Gateway methods**

`start_disposable_thread` sends:

```python
{
    "cwd": resolved_cwd,
    "model": profile.model_id,
    "sandbox": "read-only",
}
```

`fork_disposable_thread` sends only `threadId` and `lastTurnId`.
Both reject `ephemeral: true` responses. Do not require undocumented
top-level echoes of `cwd` or `model`; validate them only when app-server
returns them, and reject contradictory values. `archive_thread` sends
`{"threadId": thread_id}` to `thread/archive` and requires an object result.

Keep `turn/start` read-only through the existing `sandboxPolicy`. Stop writing
or requiring `clientUserMessageId`.

- [ ] **Step 4: Run Gateway tests**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_app_server_gateway -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the Gateway boundary**

Run:

```bash
git add src/zdecision/app_server/gateway.py tests/test_app_server_gateway.py
git commit -m "refactor: use disposable app server tasks"
```

---

### Task 4: Run page-authorized Capture through whole disposable attempts

**Files:**

- Modify: `src/zdecision/app_server/requested_capture.py`
- Modify: `src/zdecision/agent/capture_processor.py`
- Modify: `src/zdecision/agent/service.py`
- Modify: `src/zdecision/agent/cli.py`
- Modify: `tests/test_requested_capture.py`
- Modify: `tests/test_capture_request_processor.py`
- Modify: `tests/test_agent_service.py`

**Interfaces:**

- Consumes: `FrozenSessionSource`, `CaptureOperationStore`, `TemplateCatalog`,
  and the Gateway methods from Task 3.
- Produces:
  `CaptureAttemptRetryable`,
  `SourceBoundaryUnavailable`,
  `RequestedCaptureRunner.run(source, product_name, template_id,
  heartbeat=None) -> SessionCaptureResult`, and
  `RequestedCaptureRunner.sweep_archives()`.

- [ ] **Step 1: Write failing unknown-fork and unknown-Turn tests**

Change the fake Gateway so every fork returns a unique Thread ID and stores no
tag map. Assert:

```python
def test_unknown_fork_starts_a_new_generation(self) -> None:
    self.gateway.drop_first_fork_response = True
    with self.assertRaises(CaptureAttemptRetryable):
        self._run()

    result = self._run()

    self.assertEqual(2, self.gateway.fork_count)
    self.assertEqual(1, self.gateway.inventory_count)
    self.assertEqual(1, self.gateway.extraction_count)
    self.assertEqual("completed", result.status)

def test_unknown_inventory_abandons_the_whole_attempt(self) -> None:
    self.gateway.drop_first_inventory_result = True
    with self.assertRaises(CaptureAttemptRetryable):
        self._run()

    result = self._run()

    self.assertEqual(2, self.gateway.fork_count)
    self.assertEqual(2, self.gateway.inventory_count)
    self.assertEqual(1, self.gateway.extraction_count)
    operation = self.operation_store.operation_for_source(
        REQUEST_ID, self.source.source_key
    )
    self.assertIsNotNone(operation)
    self.assertEqual("committed", operation.status)
```

Add the symmetric unknown-Extraction case, which must run Inventory and
Extraction twice. Assert no fake method accepts or stores a source/client tag.

- [ ] **Step 2: Write failing fencing, source, and archive tests**

Add cases proving:

- a later source Turn does not replace `upper_turn_id`;
- model profile and rendered prompts are loaded from the existing operation on
  retry;
- a late result from generation 1 cannot replace generation 2;
- a completed operation replay starts no fork or Turn;
- a known abandoned Thread becomes archive-pending;
- an archive failure leaves only archive work pending and never reruns model
  work;
- a missing exact source boundary raises `SourceBoundaryUnavailable` and does
  not advance the Session checkpoint.

- [ ] **Step 3: Run requested-Capture tests and verify RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_requested_capture \
  tests.test_capture_request_processor -v
```

Expected: fail because the runner still constructs one extractor-v2
`CaptureRecord` and adopts native tags.

- [ ] **Step 4: Implement the whole-attempt runner**

The runner must execute in this order:

```text
read and verify exact source boundary
  -> load or create frozen CaptureOperation
  -> return committed result if present
  -> begin new active generation
  -> persist creating_thread
  -> fork_disposable_thread at exact upper Turn
  -> attach known Thread ID
  -> run and validate Inventory
  -> run and validate Extraction in the same Thread
  -> persist complete ValidatedCaptureResult on the attempt
  -> commit_attempt CAS
  -> return the stored winner
  -> schedule/archive every known terminal attempt Thread
```

On any unknown fork or Turn result, mark only the attempt abandoned and raise
`CaptureAttemptRetryable`. Never call `find_thread_by_source`, never call
`read_structured_turn_by_client_id`, and never attach a replacement Turn to the
same ambiguous Thread.

If an already frozen exact boundary disappears or contradicts its stored
identity, call
`fail_operation_terminal(operation.operation_id,
"source_boundary_unavailable")` when an operation exists and raise
`SourceBoundaryUnavailable`. Do not substitute a later Turn.

- [ ] **Step 5: Map retryable and terminal errors explicitly**

In `OnDemandCaptureProcessor`:

- map `CaptureAttemptRetryable` to
  `RetryableCaptureRequestError("capture_attempt_retryable")`;
- map `SourceBoundaryUnavailable` to
  `TerminalCaptureRequestError("source_boundary_unavailable")`;
- preserve the current central and local-state error mappings.

Call `sweep_archives()` at the start of an Agent processing cycle. Archive
failures remain recorded and do not change request success.

- [ ] **Step 6: Run the Capture and Agent tests**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_requested_capture \
  tests.test_capture_request_processor \
  tests.test_agent_service -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit requested Capture**

Run:

```bash
git add \
  src/zdecision/app_server/requested_capture.py \
  src/zdecision/agent/capture_processor.py \
  src/zdecision/agent/service.py \
  src/zdecision/agent/cli.py \
  tests/test_requested_capture.py \
  tests/test_capture_request_processor.py \
  tests/test_agent_service.py
git commit -m "feat: retry capture with disposable attempts"
```

---

### Task 5: Fence reconciliation and atomically commit Candidate effects

**Files:**

- Modify: `src/zdecision/agent/request_state.py`
- Modify: `src/zdecision/app_server/reconciliation_runner.py`
- Modify: `src/zdecision/agent/capture_processor.py`
- Modify: `tests/test_request_state.py`
- Modify: `tests/test_reconciliation_runner.py`
- Modify: `tests/test_capture_request_processor.py`

**Interfaces:**

- Produces:
  `begin_reconciliation_attempt(request_id, input_digest, started_at)`,
  `attach_reconciliation_thread(attempt_id, thread_id)`,
  `store_validated_reconciliation(attempt_id, result, finished_at)`,
  `commit_reconciliation_attempt(attempt_id) -> ReconciliationResult`,
  `commit_candidate_result(request_id, result, batch)`, and exact-replay
  `staged_batch`, `pending_batch`, and `upload_receipt`.
- Removes: `NativeCallCoordinator`, `NativeAttempt`, `native_attempts`, and all
  stable-tag recovery methods after both runners have migrated.

- [ ] **Step 1: Replace native-attempt tests with failing generation tests**

Test a request-level reconciliation operation:

```python
def test_late_reconciliation_generation_cannot_change_family_heads(self):
    first = store.begin_reconciliation_attempt(
        REQUEST_ID, INPUT_DIGEST, NOW
    )
    store.abandon_reconciliation_attempt(
        first.attempt_id, "turn_result_unknown", NOW
    )
    second = store.begin_reconciliation_attempt(
        REQUEST_ID, INPUT_DIGEST, NOW
    )

    store.store_validated_reconciliation(
        second.attempt_id, result_for_claim("winner"), NOW
    )
    winner = store.commit_reconciliation_attempt(second.attempt_id)
    winner_batch = candidate_batch(winner)
    store.commit_candidate_result(REQUEST_ID, winner, winner_batch)

    store.store_validated_reconciliation(
        first.attempt_id, result_for_claim("late"), NOW
    )
    replay = store.commit_reconciliation_attempt(first.attempt_id)

    self.assertEqual(winner, replay)
    self.assertEqual("winner", store.current_families(REPOSITORY_ID)[0].content.claim)
```

Also assert exact input-digest replay, different input conflict, restart before
winner CAS, and zero observations requiring no native attempt.

- [ ] **Step 2: Write a failing atomic Candidate/outbox crash test**

Replace the separate `save_reconciliation` then `stage_batch` expectation with:

```python
store.commit_candidate_result(REQUEST_ID, result, batch)
```

Fault-inject immediately before commit and assert no family head, result, or
outbox row exists. Fault-inject immediately after commit and assert all three
exist with matching digests. Replaying the same values returns the stored
batch; a different digest raises `BatchConflict`.

- [ ] **Step 3: Run reconciliation tests and verify RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_request_state \
  tests.test_reconciliation_runner \
  tests.test_capture_request_processor -v
```

Expected: fail because reconciliation still uses one tagged ephemeral Thread
and Candidate/outbox writes are separate.

- [ ] **Step 4: Implement disposable reconciliation attempts**

Persist `reconciliation_operations` and `reconciliation_attempts` separately
from Capture operations. Freeze a canonical digest of repository, ordered
Observation IDs/content digests, current family revisions, prompt revision,
and model profile.

Each generation starts one persisted read-only Thread, runs one structured
Turn, validates the complete result, stores it, and attempts the winner CAS.
An unknown native result abandons that generation and raises the existing
retryable request error on the next processor boundary.

Known terminal reconciliation Threads use the same pending/archive sweep as
Capture attempts. An archive failure never changes the reconciliation winner
or Candidate commit.

- [ ] **Step 5: Make Candidate state and outbox one transaction**

`commit_candidate_result` must use one `BEGIN IMMEDIATE` transaction to:

1. verify or insert the request's winning reconciliation result;
2. insert immutable family revisions;
3. move family heads only forward;
4. insert the exact immutable outbox batch, including an empty batch; and
5. commit.

An exact replay is success. A different canonical result or batch for the same
request is `BatchConflict`. Upload acknowledgement remains a later idempotent
transaction.

- [ ] **Step 6: Remove unsupported recovery code**

Delete:

- `NativeCallCoordinator`;
- `NativeAttempt`;
- the `native_attempts` table and transition methods;
- Gateway stable-tag lookup/readback methods now unused; and
- all tests that require adoption through `threadSource` or
  `clientUserMessageId`.

Keep a migration that drops `native_attempts` only after
`capture_operations`, `capture_execution_attempts`,
`reconciliation_operations`, `reconciliation_attempts`, and
`candidate_outbox` exist.

- [ ] **Step 7: Run reconciliation and processor tests**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_request_state \
  tests.test_reconciliation_runner \
  tests.test_capture_request_processor \
  tests.test_app_server_gateway -v
```

Expected: all tests pass and a repository search finds no correctness use of
the rejected fields:

```bash
rg -n "find_thread_by_source|read_structured_turn_by_client_id|NativeCallCoordinator" src tests
```

Expected: no matches.

- [ ] **Step 8: Commit reconciliation and Candidate CAS**

Run:

```bash
git add \
  src/zdecision/agent/request_state.py \
  src/zdecision/app_server/reconciliation_runner.py \
  src/zdecision/agent/capture_processor.py \
  src/zdecision/app_server/gateway.py \
  src/zdecision/agent/db.py \
  tests/test_request_state.py \
  tests/test_reconciliation_runner.py \
  tests/test_capture_request_processor.py \
  tests/test_app_server_gateway.py
git commit -m "feat: fence candidate reconciliation"
```

---

### Task 6: Prove Gate C with duplicate compute and one Candidate effect

**Files:**

- Modify: `tests/integration/test_on_demand_capture_core.py`
- Create:
  `tests/integration/test_disposable_attempts_live_app_server.py`
- Modify: `tests/test_event_ledger.py`
- Modify: `docs/architecture.md`
- Modify:
  `docs/superpowers/specs/2026-07-30-on-demand-candidate-refresh-design.md`
- Modify:
  `docs/superpowers/plans/2026-07-30-on-demand-capture-core.md`
- Modify:
  `docs/superpowers/specs/2026-07-31-disposable-capture-attempts-design.md`

**Interfaces:**

- Consumes: completed Tasks 1–5.
- Produces: executable proof that unknown native results duplicate only
  disposable compute, never Candidate effects.

- [ ] **Step 1: Rewrite the Gate A–C fake around unique attempts**

Every fake `thread/start` or `thread/fork` returns a new native ID. Remove
`thread_sources`, client-message maps, and stable-ID-derived fake Turn IDs.

Replace `test_unknown_native_results_are_adopted_without_duplicate_work` with
tests asserting:

- unknown fork: two forks, one full two-stage execution, one Capture commit;
- unknown Inventory: two forks, two Inventory calls, one Extraction result,
  one Capture commit;
- unknown Extraction: two complete two-stage attempts, one Capture commit;
- late different output from the abandoned generation cannot change the
  Candidate family;
- central SQLite contains one request result and one immutable batch;
- handled checkpoints advance only after central acknowledgement.

- [ ] **Step 2: Add crash-point and source-boundary assertions**

Cover:

- restart after validated attempt persistence but before Capture CAS;
- restart after Capture CAS but before reconciliation;
- restart immediately before and after Candidate/outbox transaction;
- restart after central persistence but before local acknowledgement;
- a later Turn arriving after the frozen request;
- a missing exact source boundary;
- an empty Candidate result; and
- a malicious `transcript_path` sentinel that is never opened or stored.

- [ ] **Step 3: Run the deterministic Gate A–C suite**

Run:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.integration.test_on_demand_capture_core \
  tests.test_plugin_contract \
  tests.test_skill_contract -v
```

Expected: all tests pass.

- [ ] **Step 4: Add the live fault-injected app-server test**

Create an opt-in test that launches `ProcessJsonlTransport`, wraps it with
`DropResponseTransport`, and creates its own trivial persisted source Thread
and completed source Turn.

`DropResponseTransport` must:

1. forward every request to the real transport;
2. remember the JSON-RPC ID of the first `thread/fork`;
3. discard exactly that response while continuing to forward notifications;
4. let the client reach its bounded timeout; and
5. pass all later responses unchanged.

Run the real `RequestedCaptureRunner` twice. Assert the first call records
`fork_result_unknown`, the second creates a higher generation and completes,
only one operation result exists, no request contains `threadSource` or
`clientUserMessageId`, and replay makes no model call.

Guard the test with `ZDECISION_LIVE_APP_SERVER=1` so the ordinary suite skips
model-backed acceptance.

- [ ] **Step 5: Run the live acceptance once**

Run:

```bash
ZDECISION_LIVE_APP_SERVER=1 \
PYTHONPATH=src \
python3 -m unittest \
  tests.integration.test_disposable_attempts_live_app_server -v
```

Expected: pass against the installed authenticated Codex app-server. Record
the source Thread/Turn, first and winning attempt IDs, operation ID, generation
count, Candidate count, and archive outcomes. Do not print raw source or
Candidate text.

- [ ] **Step 6: Update active architecture text**

Replace the active design's “adopt/resume the same native result” language with
the approved operation/attempt contract. Mark the disposable-attempt design
`Implemented in Packet 1` only after the live acceptance passes. Historical
superseded feasibility evidence remains unchanged.

Update the old Task 6/Task 9 plan prose so it no longer instructs later agents
to restore stable-tag adoption.

- [ ] **Step 7: Run one focused suite and one full suite**

Run the focused suite:

```bash
PYTHONPATH=src python3 -m unittest \
  tests.test_capture_operation \
  tests.test_requested_capture \
  tests.test_request_state \
  tests.test_reconciliation_runner \
  tests.test_capture_request_processor \
  tests.test_app_server_gateway \
  tests.integration.test_on_demand_capture_core -v
```

Then run the full suite exactly once:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Expected: all tests pass. If one confirmed regression appears, make one
focused correction, rerun that module, and rerun the full command once.

- [ ] **Step 8: Verify packaging and repository integrity**

Run:

```bash
python3 -m pip install "build>=1.2,<2"
python3 -m build
git diff --check
git status --short
```

Expected: wheel and source distribution build successfully; the worktree
contains only intended Task 6 changes.

- [ ] **Step 9: Commit the corrected Packet 1**

Run:

```bash
git add \
  tests/integration/test_on_demand_capture_core.py \
  tests/integration/test_disposable_attempts_live_app_server.py \
  tests/test_event_ledger.py \
  docs/architecture.md \
  docs/superpowers/specs/2026-07-30-on-demand-candidate-refresh-design.md \
  docs/superpowers/plans/2026-07-30-on-demand-capture-core.md \
  docs/superpowers/specs/2026-07-31-disposable-capture-attempts-design.md
git commit -m "feat: complete disposable capture recovery"
```

## Completion report

Report only:

- the six implementation commits;
- focused, full-suite, packaging, and live-acceptance results;
- the real operation ID and two attempt generations, without raw content;
- proof that one Candidate result/outbox batch won;
- proof that handled checkpoints advanced only after acknowledgement; and
- deferred non-blocking risks, limited to the unstable future of app-server
  schemas and cleanup of an unidentifiable blank orphan Thread.
