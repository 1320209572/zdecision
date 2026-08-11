# Recall Next-Native-Message Handoff Gate A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken Recall-specific App Server proof with an
authoritative, idempotent confirmation-to-delivery flow that hands a frozen
formal-Decision fixture to the next native Codex message and commits a real
active item set before covered development proceeds.

**Architecture:** A pure Recall handoff domain defines preflight, shortlist,
context, and four-way applicability contracts. `RecallHostStore` durably owns
confirmation, delivery, application, and active-item transactions, while a
small `RecallHandoffService` orchestrates an injected provider outside SQLite
transactions. The Plugin Hook binds trusted task coordinates and blocks
covered mutation; the MCP App performs one `ui/update-model-context` request
and an app-only acknowledgement. Production uses a fail-closed unavailable
provider until Gates B and C supply trusted data and retrieval; only tests and
a disposable acceptance Plugin receive a deterministic formal-Decision
provider.

**Tech Stack:** Python 3.11+, standard-library dataclasses/JSON/SHA-256/SQLite
WAL, MCP SDK 1.28+, Pydantic 2, vanilla MCP Apps HTML/JavaScript, `unittest`, and
Codex Desktop.

## Global Constraints

- The authority is
  `docs/superpowers/specs/2026-08-10-recall-next-native-message-handoff-design.md`.
- This plan implements Gate A only. Do not add Central Recall endpoints,
  signing keys, snapshot distribution, model runtimes, embeddings, BM25,
  reranking, or production indexes.
- Only the app-only **启用本任务决策召回** click authorizes Recall. Plugin or
  Skill selection, Prompt text, model intent, tool invocation, and Hook output
  do not authorize it.
- Product ambiguity is resolved before a card executes. The Hook may return
  bounded display-name choices, but it must create no activation attempt and
  execute no retrieval for an ambiguous route.
- The synchronous path uses no Central request, second App Server,
  `thread/read`, `hookPrompt` proof, transcript parsing, private Desktop IPC, or
  `ui/message`.
- Prompt, PRD, transcript, source, diff, tool output, local absolute paths,
  normalized intent, candidates, scores, application, and active state remain
  local. Do not echo trusted Session, Turn, CWD, repository, attempt, claim, or
  delivery coordinates into model-visible Hook output.
- The deterministic formal-Decision provider is test-only. Do not register it
  in `plugins/zdecision`, `pyproject.toml`, the production CLI, or the
  production MCP factory.
- Production returns bounded unavailable state until a later Gate supplies a
  `recall_ready` provider. Do not read Git Registry state directly as a
  substitute.
- Preserve Candidate refresh, Capture App Server behavior, Review,
  publication, Registry V1 bytes, and Central Web behavior.
- Keep at most eight complete formal Decisions and 10,000 UTF-8 bytes of
  canonical Decision content. Never truncate a Decision.
- Covered mutation remains denied from accepted consent until the exact
  application receipt commits. Plain assistant text is not claimed as a hard
  enforcement boundary.
- Direct work on `main` is explicitly approved. Do not create a branch or
  worktree, and do not push unless the user asks.
- Preserve and never stage or edit these user-owned untracked paths:
  `docs/superpowers/acceptance/2026-08-06-recall-host-gate.md` and
  `tests/integration/test_recall_host_gate.py`.
- Every commit stages exact paths. Do not use `git add .`.
- Use one scoped review per task through the selected execution skill. Do not
  add a separate broad final review or Skill blind-test loop.

## File responsibility map

- `src/zdecision/recall/handoff.py`: pure, canonical handoff and application
  values; no I/O.
- `src/zdecision/recall/provider.py`: provider protocol and fail-closed
  production fallback.
- `src/zdecision/agent/recall_host_state.py`: SQLite schema and atomic state
  transitions only; no retrieval or UI.
- `src/zdecision/agent/recall_handoff.py`: provider orchestration and bounded
  tool outcomes; no MCP registration.
- `src/zdecision/agent/recall_plugin_identity.py`: immutable Plugin identity,
  exact bundle/Hook validation, and production identity constant; no I/O
  beyond bounded local bundle reads.
- `src/zdecision/agent/recall_mcp.py`: thin MCP-facing domain adapter.
- `src/zdecision/agent/hooks.py`: trusted host binding, lifecycle instruction,
  and mutation backstop.
- `src/zdecision/agent/mcp_server.py`: MCP schemas, visibility, annotations,
  resources, and safe result envelopes.
- `src/zdecision/agent/static/recall-confirmation-v1.html`: two-button card,
  context update, explicit retry, and app-only acknowledgement.
- `plugins/zdecision/skills/zdecision/`: model workflow instructions only.
- `tests/integration/recall_gate_a_desktop_harness.py`: test-only provider,
  disposable Plugin generator/runtime, and bounded inspector/cleanup; never
  packaged into production.

## Execution preflight

Before Task 1, record but do not change:

```bash
git status --short --branch
git rev-parse HEAD
```

Expected: `main` contains approved design commit `8987a9a`; the two protected
untracked files may be present. If any other uncommitted path overlaps a task,
stop and report it rather than overwriting it.

---

### Task 0: Prove the disposable Decision-application vertical

**Files:**
- Create: `tests/integration/recall_gate_a0_disposable_harness.py`
- Create: `tests/integration/test_recall_gate_a0_disposable_vertical.py`
- Create after real PASS:
  `docs/superpowers/acceptance/2026-08-10-recall-gate-a0-disposable-vertical.md`
- Do not modify: `src/zdecision/**` or `plugins/zdecision/**`

**Interfaces:**
- Consumes: only the already-proved MCP Apps capabilities and two canonical,
  explicitly test-only `DecisionRevision` fixtures.
- Produces: a disposable Plugin generator/server/Hook, a durable isolated test
  database, automated protocol coverage, and one real Desktop PASS/FAIL report.
- Hard stop: 120 minutes from implementation start. A FAIL or unavailable host
  capability stops Gate A; it does not authorize production fixes.

- [ ] **Step 1: Write failing disposable-vertical tests**

Name and exercise these real behavior breaks:

1. a model-visible card tool whose `PreToolUse` Hook replaces all model host
   coordinates with the trusted task/Turn binding;
2. one app-only enable action that commits exactly one stable delivery holding
   two complete, canonical test `DecisionRevision` envelopes;
3. exactly one `ui/update-model-context` request containing the complete typed
   snapshot and zero `ui/message` requests;
4. an app-only acknowledgement and remount status that recover the same
   receipt without another mutation;
5. a next-native-message application tool whose Hook preserves only the two
   four-category classifications and injects the trusted delivery binding;
6. a safe, test-only mutation-counter tool denied before application and
   allowed exactly once after one fixture is `applicable` and the other is
   `not_applicable`; and
7. isolation from the production Plugin, production agent database, Central,
   Candidate tables, Registry, source tree, and any App Server process.

The tests must execute the generated Hook, MCP server, card JavaScript bridge,
SQLite restart/remount path, and cleanup logic. They may mock only the Codex
Desktop transport boundary. Expected Decision bytes and digests are literal or
hand-checked fixtures, never computed by the production code under test.

Run:

```bash
.venv/bin/python -m unittest \
  tests.integration.test_recall_gate_a0_disposable_vertical -v
```

Expected RED: import/file failure because the disposable harness does not yet
exist. No production file changes during RED.

- [ ] **Step 2: Implement the smallest isolated harness and reach GREEN**

The harness supports exact commands:

```text
create --root <temporary-absolute-path> --repository <enabled-repository>
hook --root <temporary-absolute-path>
mcp --root <temporary-absolute-path>
inspect --root <temporary-absolute-path>
cleanup --root <temporary-absolute-path>
```

Reuse the already validated app-only/state/App-bridge patterns from repository
history commits `5cf5a55` through `8694736`; do not restore their marker-only
acceptance or production probe surfaces. The new typed snapshot contains two
valid fixtures, a protocol/version, one opaque delivery ID, and a literal
application instruction. It contains no raw Prompt, task ID, Session/Turn ID,
absolute path, or full receipt.

The mutation-counter tool changes only the disposable SQLite database. It must
not execute a shell command or edit a repository file. The Hook returns a
non-empty denial before the exact application receipt and permits it after the
atomic application commit. The server never imports or constructs
`AppServerGateway`.

Run the same test command until GREEN, followed by:

```bash
.venv/bin/python -m compileall -q \
  tests/integration/recall_gate_a0_disposable_harness.py
git diff --check
```

- [ ] **Step 3: Commit the automated disposable harness**

```bash
git add \
  tests/integration/recall_gate_a0_disposable_harness.py \
  tests/integration/test_recall_gate_a0_disposable_vertical.py
git commit -m "test: add disposable Recall Gate A0 vertical"
```

- [ ] **Step 4: Run one bounded real Desktop acceptance**

Create a fresh `mktemp -d` root outside the repository and production Plugin
cache, generate/install a uniquely named **ZDecision Gate A0** Plugin, and
restart Codex once. In an enabled repository, the user selects that disposable
entry and performs only these bounded actions:

1. render the card and click enable once;
2. switch away and back once to prove receipt recovery without another action;
3. keep the App attachment and send one native message;
4. verify Codex receives both complete fixture Decisions without a read tool,
   calls the application tool with one `applicable` and one `not_applicable`,
   observes the safe pre-application mutation denial, and succeeds exactly once
   after application; and
5. send one ordinary follow-up to prove no repeated delivery or mutation.

Inspect only bounded states, counts, and digest/receipt prefixes. A pass requires
zero `ui/message`, zero App Server start/connect, one delivery, one context
update, one application receipt, one active fixture, one permitted counter
increment, and no production/Candidate/Registry mutation. Any missing
capability, cross-task binding, automatic retry, duplicate mutation, or manual
database intervention is FAIL.

- [ ] **Step 5: Clean up, record the result, and decide the gate**

Uninstall only the disposable selector/marketplace, stop only its processes,
and delete only the recorded temporary root. Prove the production ZDecision
Plugin is still installed/enabled and unchanged.

Write a sanitized report with environment versions, the behavior matrix,
bounded counts, redacted prefixes, cleanup evidence, and PASS/FAIL. It must not
contain a Prompt, full Decision, full receipt, task/Session/Turn ID, absolute
path, database row, or transcript.

```bash
git add docs/superpowers/acceptance/2026-08-10-recall-gate-a0-disposable-vertical.md
git commit -m "docs: record Recall Gate A0 result"
```

Only PASS permits Task 1. FAIL appends the exact blocker to the SDD ledger and
ends this implementation run without changing production Recall.

---

### Task 1: Define canonical handoff and provider contracts

**Files:**
- Create: `src/zdecision/recall/handoff.py`
- Create: `src/zdecision/recall/provider.py`
- Modify: `src/zdecision/recall/__init__.py`
- Create: `tests/test_recall_handoff_contracts.py`

**Interfaces:**
- Consumes: `zdecision.recall.session.RecallIntent` and
  `zdecision.registry.models.DecisionRevision`.
- Produces:
  `RecallPreflightReady`, `RecallPreflightClarification`,
  `RecallPreflightUnavailable`, `RecallPreflightResult`, `RecalledDecision`,
  `RecallShortlist`, `RecallApplicationItem`, `RecallApplicationSubmission`,
  `build_handoff_context()`, `RecallProvider`, and
  `UnavailableRecallProvider`.

- [ ] **Step 1: Write failing canonical-contract tests**

Add tests that construct one valid `DecisionRevision` fixture and prove exact
field validation, stable digests, item/byte limits, four application categories,
and non-executable context encoding:

```python
def test_handoff_context_is_canonical_bounded_and_complete(self) -> None:
    preflight = ready_preflight(intent=valid_intent())
    item = RecalledDecision.create(
        decision_space_id=preflight.target_decision_space_ids[0],
        revision=formal_decision(),
        match_reason="Exact product and capability match",
    )
    shortlist = RecallShortlist.create(preflight=preflight, items=(item,))
    text = build_handoff_context("delivery_" + "a" * 32, preflight, shortlist)
    payload = json.loads(text)
    self.assertEqual("ZDECISION_RECALL_HANDOFF", payload["marker"])
    self.assertEqual("recall-handoff-v1", payload["protocol"])
    self.assertEqual(item.digest, payload["decisions"][0]["digest"])
    self.assertEqual(item.revision.to_dict(), payload["decisions"][0]["formal_decision"])
    self.assertNotIn("session_id", text)
    self.assertNotIn("turn_id", text)
    self.assertNotIn('"repository_id"', text)
    self.assertNotIn(str(Path.cwd()), text)
```

Also prove `RecallShortlist.create()` rejects a ninth item, a truncated or
digest-mismatched revision, a wrong preflight digest, duplicate Decision
tuples, and more than 10,000 canonical Decision bytes. Prove
`RecallApplicationSubmission.from_dict()` requires every field exactly once
and accepts only `applicable`, `not_applicable`, `conflicting`, or `uncertain`.

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_handoff_contracts -v
```

Expected: import failure because `zdecision.recall.handoff` and
`zdecision.recall.provider` do not exist.

- [ ] **Step 3: Implement the pure values and provider seam**

Use frozen dataclasses with exact `to_dict()`/`from_dict()` round trips and
`canonical_json_bytes` digests. Keep this public shape stable for every later
task:

```python
RECALL_HANDOFF_PROTOCOL = "recall-handoff-v1"
ApplicationDisposition = Literal[
    "applicable", "not_applicable", "conflicting", "uncertain"
]

@dataclass(frozen=True)
class RecallPreflightReady:
    repository_id: str
    repository_display_name: str
    intent: RecallIntent
    target_decision_space_ids: tuple[str, ...]
    target_display_names: tuple[str, ...]
    catalog_digest: str
    generation: int
    generation_digest: str
    retrieval_profile_digest: str
    index_generation: int
    freshness: Literal["ready", "degraded"]
    expires_at: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_dict())).hexdigest()

@dataclass(frozen=True)
class RecalledDecision:
    decision_space_id: str
    revision: DecisionRevision
    digest: str
    match_reason: str

    @classmethod
    def create(
        cls,
        *,
        decision_space_id: str,
        revision: DecisionRevision,
        match_reason: str,
    ) -> "RecalledDecision":
        revision_digest = hashlib.sha256(
            canonical_json_bytes(revision.to_dict())
        ).hexdigest()
        return cls(
            decision_space_id=decision_space_id,
            revision=revision,
            digest=revision_digest,
            match_reason=match_reason,
        )

@dataclass(frozen=True)
class RecallApplicationItem:
    decision_id: str
    revision: int
    digest: str
    disposition: ApplicationDisposition
    reason: str

class RecallProvider(Protocol):
    def preflight(
        self,
        *,
        repository_id: str,
        repository_display_name: str,
        intent: RecallIntent,
        now: datetime,
    ) -> RecallPreflightResult:
        raise NotImplementedError

    def retrieve(self, preflight: RecallPreflightReady) -> RecallShortlist:
        raise NotImplementedError
```

`UnavailableRecallProvider.preflight()` returns
`RecallPreflightUnavailable(code="recall_not_ready")`; its `retrieve()` raises
a bounded `RecallProviderUnavailable`. Do not add a deterministic provider to
`src/`.

- [ ] **Step 4: Run focused GREEN**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_session_contracts \
  tests.test_recall_handoff_contracts -v
```

Expected: all tests pass; no test imports a model runtime or Central client.

- [ ] **Step 5: Commit Task 1**

```bash
git add \
  src/zdecision/recall/handoff.py \
  src/zdecision/recall/provider.py \
  src/zdecision/recall/__init__.py \
  tests/test_recall_handoff_contracts.py
git commit -m "feat: define Recall handoff contracts"
```

---

### Task 2: Add durable delivery, application, and active-item state

**Files:**
- Modify: `src/zdecision/agent/recall_host_state.py`
- Modify: `tests/test_recall_host_state.py`

**Interfaces:**
- Consumes: Task 1 canonical values.
- Produces: `RecallDelivery`, `ActiveInjectedItem`, `DeliveryClaim`, and these
  `RecallHostStore` methods:
  `create_activation_attempt()` extended with required `intent` and `preflight`
  keyword parameters, `begin_delivery()`,
  `commit_prepared_delivery()`, `get_delivery()`, `delivery_for_attempt()`,
  `ack_delivery()`, `mark_delivery_unknown()`, `claim_delivery_retry()`,
  `commit_delivery_application()`, and `list_active_items()`.

- [ ] **Step 1: Write failing schema, migration, and transaction tests**

Add tests for:

```python
claim = store.begin_delivery(
    attempt_id=ATTEMPT_ID,
    delivery_id=DELIVERY_ID,
    claim_token="claim_" + "c" * 32,
    now=NOW,
    claim_expires_at=NOW + timedelta(seconds=30),
)
self.assertTrue(claim.owned)
self.assertEqual("preparing", claim.delivery.state)
self.assertEqual("activating", store.get_session(SESSION_ID).state)

prepared = store.commit_prepared_delivery(
    delivery_id=DELIVERY_ID,
    claim_token=claim.claim_token,
    shortlist=shortlist,
    context_text=build_handoff_context(DELIVERY_ID, preflight, shortlist),
    now=NOW,
)
self.assertEqual("delivery_claimed", prepared.state)
```

Prove all of the following:

- consent, `activating` Session creation, and delivery insertion commit in one
  short transaction;
- provider work is absent from the store API;
- concurrent callers cannot own the same unexpired claim;
- an expired claim can be taken over with the same delivery ID;
- prepared bytes and digests are immutable;
- ack requires the exact context digest;
- unknown delivery can be explicitly reclaimed without changing bytes;
- application requires every frozen item exactly once;
- only `applicable` items enter `recall_active_injected_items`;
- all-`not_applicable` commits an active empty set;
- application, Turn-gate commit, Session activation, active items, and receipt
  commit atomically;
- restart reopens every state without rerunning a transition; and
- legacy rows with no `recall-handoff-v1` protocol never become active under
  the new protocol.

- [ ] **Step 2: Run the state tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_host_state -v
```

Expected: failures for missing preflight columns, delivery tables, active-item
tables, dataclasses, and store methods.

- [ ] **Step 3: Implement additive SQLite migration and atomic APIs**

Add nullable migration columns to `recall_activation_attempts`:
`protocol_version`, `preflight_json`, and `preflight_digest`. Add
`protocol_version` and `repository_id` to `recall_sessions`. Add the exact new
tables:

```sql
CREATE TABLE IF NOT EXISTS recall_deliveries (
    delivery_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'preparing', 'context_prepared', 'delivery_claimed',
        'host_delivered', 'delivery_unknown', 'application_committed',
        'blocked', 'invalidated'
    )),
    preflight_digest TEXT NOT NULL,
    claim_token TEXT,
    claim_expires_at TEXT,
    shortlist_json TEXT,
    snapshot_digest TEXT,
    context_text TEXT,
    context_digest TEXT,
    application_json TEXT,
    application_digest TEXT,
    application_receipt_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recall_active_injected_items (
    session_id TEXT NOT NULL,
    intent_epoch INTEGER NOT NULL,
    context_epoch INTEGER NOT NULL,
    decision_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    decision_space_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    delivery_id TEXT NOT NULL,
    application_receipt_id TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    PRIMARY KEY(session_id, intent_epoch, decision_id, revision, digest)
);
```

Every JSON column stores canonical UTF-8 JSON text and is reparsed through Task
1 values on read. `begin_delivery()` performs no provider call. Derive the
active-set digest from sorted full member identities, not from model text.
Keep legacy tables readable during this task, but reject legacy authorization
through the protocol field.

Keep the existing physical attempt-state literal `committed` as the stored
representation of the specification's logical `accepted` state; do not rebuild
the table merely to rename that value. Only a `committed` row with
`protocol_version = 'recall-handoff-v1'` and a matching delivery may authorize
the new flow. A legacy `committed` row with a null/older protocol remains
non-authoritative.

- [ ] **Step 4: Run focused state GREEN**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_handoff_contracts \
  tests.test_recall_host_state -v
```

Expected: all tests pass, including reopen, conflict, and rollback cases.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/zdecision/agent/recall_host_state.py tests/test_recall_host_state.py
git commit -m "feat: persist Recall handoff transactions"
```

---

### Task 3: Bind typed preflight before rendering the card

**Files:**
- Modify: `src/zdecision/agent/hooks.py`
- Modify: `src/zdecision/agent/recall_mcp.py`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `tests/test_recall_hook_gate.py`
- Modify: `tests/test_mcp_recall_confirmation.py`
- Modify: `tests/test_hook_latency.py`

**Interfaces:**
- Consumes: Task 1 provider and Task 2 frozen attempt.
- Produces: a model-visible
  `show_zdecision_recall_confirmation(intent, activation_attempt_id="")` whose
  Hook preserves only strict intent and injects the trusted attempt ID.

- [ ] **Step 1: Write failing ready, ambiguous, unavailable, and privacy tests**

Inject a deterministic provider into `handle_hook()` in tests and assert:

```python
response = self._pre_tool(
    SHOW_RECALL_CONFIRMATION_TOOL,
    tool_input={"activation_attempt_id": "model-value", "intent": VALID_INTENT},
    recall_provider=ReadyProvider(preflight),
)
self.assertEqual("allow", permission(response))
self.assertEqual(
    {"activation_attempt_id": ACTIVATION_ID, "intent": VALID_INTENT},
    response.output["hookSpecificOutput"]["updatedInput"],
)
self.assertEqual(preflight.digest, store.get_activation_attempt(ACTIVATION_ID).preflight.digest)
```

For `RecallPreflightClarification`, assert `permissionDecision == "deny"`, the
bounded reason contains only candidate display names, no attempt row exists,
and the MCP tool never runs. For `RecallPreflightUnavailable`, assert a generic
non-retry reason with no private coordinate. Missing/extra/malformed intent,
unregistered repository, wrong Plugin root, and cross-task replay fail closed.

Update the MCP schema test to require all seven strict intent fields, optional
host-supplied attempt ID, and `additionalProperties: false`.
Extend the existing warm Hook benchmark with a ready in-memory preflight and
assert P95 remains at most 150 ms while `socket.connect` is forbidden. The
preflight contract must never perform retrieval, model inference, Central I/O,
or index construction inside the Hook.

- [ ] **Step 2: Run focused tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_hook_gate \
  tests.test_mcp_recall_confirmation -v
```

Expected: current Hook drops intent, has no provider preflight, and creates an
attempt without frozen routing/generation data.

- [ ] **Step 3: Implement the trusted local preflight**

Add `recall_provider: RecallProvider | None = None` to `handle_hook()`,
`handle_pre_tool_hook()`, and `bind_recall_tool_call()`. The default is
`UnavailableRecallProvider()`. The ready branch must be equivalent to:

```python
intent = RecallIntent.from_dict(tool_input["intent"])
result = provider.preflight(
    repository_id=repository.repository_id,
    repository_display_name=Path(repository.worktree_root).name,
    intent=intent,
    now=now,
)
if isinstance(result, RecallPreflightClarification):
    return deny_with_display_names(result.candidate_display_names)
if isinstance(result, RecallPreflightUnavailable):
    return deny_without_private_state(result.code)
attempt = recall_store.create_activation_attempt(
    session_id=session_id,
    turn_id=turn_id,
    cwd=cwd,
    repository_id=repository.repository_id,
    repository_display_name=result.repository_display_name,
    attempt_id=attempt_id,
    now=now,
    expires_at=now + _CONFIRMATION_LIFETIME,
    plugin_root=plugin_root,
    preflight=result,
)
return allow({"activation_attempt_id": attempt.attempt_id, "intent": intent.to_dict()})
```

`RecallMcpTools.show_recall_confirmation()` reparses intent and requires its
digest to match the frozen preflight before attaching the UI digest. Its
model-visible result contains safe state only; App `_meta` may contain attempt
ID, repository display name, target display names, and freshness. It contains
no Decision text.

Mark the show tool `readOnlyHint=True`; confirmation mutation remains app-only.

- [ ] **Step 4: Run focused GREEN**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_handoff_contracts \
  tests.test_recall_host_state \
  tests.test_recall_hook_gate \
  tests.test_mcp_recall_confirmation \
  tests.test_hook_latency -v
```

Expected: all tests pass; ambiguity executes no render tool and creates no
attempt.

- [ ] **Step 5: Commit Task 3**

```bash
git add \
  src/zdecision/agent/hooks.py \
  src/zdecision/agent/recall_mcp.py \
  src/zdecision/agent/mcp_server.py \
  tests/test_recall_hook_gate.py \
  tests/test_mcp_recall_confirmation.py \
  tests/test_hook_latency.py
git commit -m "feat: preflight Recall before confirmation"
```

---

### Task 4: Implement idempotent enable-and-prepare orchestration

**Files:**
- Create: `src/zdecision/agent/recall_handoff.py`
- Create: `tests/test_recall_handoff_service.py`
- Modify: `src/zdecision/agent/recall_mcp.py`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `tests/test_mcp_recall_confirmation.py`

**Interfaces:**
- Consumes: `RecallProvider.retrieve()`, Task 2 claim/store APIs, and frozen
  preflight.
- Produces: `RecallHandoffService.enable()`, `.decline()`, `.status()`, and a
  `decide_zdecision_recall` app-only result with one private frozen delivery.

- [ ] **Step 1: Write failing service ownership and recovery tests**

Use a counting provider and prove:

```python
first = service.enable(
    attempt_id=ATTEMPT_ID,
    current_ui_digest=UI_DIGEST,
)
self.assertEqual("delivery_claimed", first["state"])
self.assertEqual(1, provider.retrieve_calls)

replay = service.enable(
    attempt_id=ATTEMPT_ID,
    current_ui_digest=UI_DIGEST,
)
self.assertEqual(first["delivery_id"], replay["delivery_id"])
self.assertEqual(first["context_digest"], replay["context_digest"])
self.assertEqual(1, provider.retrieve_calls)
```

Cover double click, concurrent in-progress response, expired claim takeover,
provider exception, crash after `preparing`, crash after delivery commit but
before response, wrong UI digest, expired attempt, generation/preflight
mismatch, decline, and all-Decision byte limits. Assert no provider call occurs
inside a SQLite transaction by making the provider open a second writer.
Also prove that `status()` reports an expired unacknowledged claim as derived
`delivery_unknown` without mutating it, and that only a later explicit enable
click may atomically persist that state and reclaim the same delivery ID,
digest, and context bytes. Before claim expiry, the same click returns bounded
in-progress state and does not resend.

- [ ] **Step 2: Run the service tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_recall_handoff_service -v
```

Expected: missing `RecallHandoffService` and enable transaction APIs.

- [ ] **Step 3: Implement the thin orchestration service**

Use injectable ID/token factories and a 30-second claim lease:

```python
class RecallHandoffService:
    def __init__(
        self,
        *,
        store: RecallHostStore,
        provider: RecallProvider,
        clock: Callable[[], datetime],
        delivery_id_factory: Callable[[str], str],
        claim_token_factory: Callable[[], str],
    ) -> None:
        self.store = store
        self.provider = provider
        self.clock = clock
        self.delivery_id_factory = delivery_id_factory
        self.claim_token_factory = claim_token_factory

    def enable(
        self,
        *,
        attempt_id: str,
        current_ui_digest: str,
    ) -> dict[str, object]:
        now = self.clock()
        claim = self.store.begin_delivery(
            attempt_id=attempt_id,
            delivery_id=self.delivery_id_factory(attempt_id),
            claim_token=self.claim_token_factory(),
            current_ui_digest=current_ui_digest,
            now=now,
            claim_expires_at=now + timedelta(seconds=30),
        )
        if not claim.owned:
            return self.status(attempt_id=attempt_id)
        shortlist = self.provider.retrieve(claim.delivery.preflight)
        context_text = build_handoff_context(
            claim.delivery.delivery_id,
            claim.delivery.preflight,
            shortlist,
        )
        return delivery_output(
            self.store.commit_prepared_delivery(
                delivery_id=claim.delivery.delivery_id,
                claim_token=claim.claim_token,
                shortlist=shortlist,
                context_text=context_text,
                now=self.clock(),
            )
        )
```

The service returns context text, delivery ID, snapshot digest, and context
digest only in private app `_meta`. Model-visible content receives bounded
state and code. Refactor `RecallMcpTools` to delegate confirmation decisions to
this service. Do not construct an App Server Gateway.

- [ ] **Step 4: Run focused GREEN**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_host_state \
  tests.test_recall_handoff_service \
  tests.test_mcp_recall_confirmation -v
```

Expected: all tests pass and one logical attempt invokes the provider at most
once per owned claim/recovery path.

- [ ] **Step 5: Commit Task 4**

```bash
git add \
  src/zdecision/agent/recall_handoff.py \
  src/zdecision/agent/recall_mcp.py \
  src/zdecision/agent/mcp_server.py \
  tests/test_recall_handoff_service.py \
  tests/test_mcp_recall_confirmation.py
git commit -m "feat: prepare Recall delivery on confirmation"
```

---

### Task 5: Deliver one snapshot through the confirmation App

**Files:**
- Modify: `src/zdecision/agent/static/recall-confirmation-v1.html`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `src/zdecision/agent/recall_mcp.py`
- Modify: `src/zdecision/agent/recall_handoff.py`
- Modify: `tests/test_mcp_recall_confirmation.py`
- Modify: `tests/test_recall_handoff_service.py`

**Interfaces:**
- Consumes: Task 4 private delivery result.
- Produces: app-only `get_zdecision_recall_handoff` and
  `ack_zdecision_recall_delivery`, exactly one context update per explicit
  claim, and explicit exact-byte retry.

- [ ] **Step 1: Write failing MCP App protocol tests**

Extend the existing JavaScript harness to assert this exact order:

```javascript
const click = widget.elements.enable.dispatch("click");
widget.respondToTool("decide_zdecision_recall", deliveryClaimedResult);
await flush();
check(widget.contextUpdates().length === 1, "missing single context update");
check(widget.toolCalls()[1].params.name === "ack_zdecision_recall_delivery",
      "ack did not follow context update");
check(widget.messages().length === 0, "ui/message must not be used");
```

Also prove:

- `ui/initialize.result.hostCapabilities.updateModelContext.text` is
  feature-detected;
- missing capability does not call context update or ack;
- `ui/update-model-context` uses
  `{content: [{type: "text", text: contextText}]}` and one complete snapshot;
- rejected/timeout update sends no success ack and never automatically retries;
- ack timeout displays unknown and never resends automatically;
- remount calls only the app-only status tool and performs no mutation;
- explicit retry reuses the same delivery ID, digest, and context bytes;
- decline sends no context or ack; and
- wrong attempt/delivery/context digests fail closed.

- [ ] **Step 2: Run card tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_mcp_recall_confirmation -v
```

Expected: current card calls unsupported `ui/message`, never calls
`ui/update-model-context`, and has no delivery acknowledgement tools.

- [ ] **Step 3: Implement capability-aware delivery and app-only tools**

Register exact app-only tools:

```python
def get_zdecision_recall_handoff(
    activation_attempt_id: str,
) -> CallToolResult:
    return _confirmation_call_result(
        recall_tools.get_recall_handoff(
            activation_attempt_id=activation_attempt_id,
        )
    )

def ack_zdecision_recall_delivery(
    activation_attempt_id: str,
    delivery_id: str,
    context_digest: str,
) -> CallToolResult:
    return _confirmation_call_result(
        recall_tools.ack_recall_delivery(
            activation_attempt_id=activation_attempt_id,
            delivery_id=delivery_id,
            context_digest=context_digest,
        )
    )
```

The status tool is read-only and never returns context bytes unless an explicit
retry claim has been made by the user's click. The ack tool accepts only the
current attempt/delivery/digest tuple and moves it to `host_delivered`.
An update rejection or timeout leaves the durable claim unapplied. An ack
timeout displays local unknown state; remount derives unknown only after the
claim lease expires, and the server persists/reissues it only when the user
explicitly clicks `重新交付`. If the original ack actually committed, status
returns `host_delivered` and the retry button remains disabled.

In the card, delete `sendContinuationOnce()` and every `ui/message` reference.
After a successful ack display:

```text
决策已交付。请保留此附件并发送下一条原生消息；应用完成前不会修改代码。
```

Keep exactly two buttons. In `delivery_unknown`, relabel the enable button
`重新交付` and require another explicit click; never run it on load or remount.

- [ ] **Step 4: Run focused GREEN**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_handoff_service \
  tests.test_mcp_recall_confirmation -v
```

Expected: all card simulations pass and every exercised success, rejection,
timeout, retry, and remount flow records zero outbound `ui/message` requests.

- [ ] **Step 5: Commit Task 5**

```bash
git add \
  src/zdecision/agent/static/recall-confirmation-v1.html \
  src/zdecision/agent/mcp_server.py \
  src/zdecision/agent/recall_mcp.py \
  src/zdecision/agent/recall_handoff.py \
  tests/test_mcp_recall_confirmation.py \
  tests/test_recall_handoff_service.py
git commit -m "feat: hand Recall context to the next message"
```

---

### Task 6: Commit next-message applicability before covered mutation

**Files:**
- Modify: `src/zdecision/agent/hooks.py`
- Modify: `src/zdecision/agent/recall_handoff.py`
- Modify: `src/zdecision/agent/recall_mcp.py`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `plugins/zdecision/hooks/hooks.json`
- Modify: `tests/test_recall_hook_gate.py`
- Modify: `tests/test_recall_handoff_service.py`
- Create: `tests/test_mcp_recall_handoff.py`

**Interfaces:**
- Consumes: host-delivered snapshot and Task 2 application transaction.
- Produces: model-visible `apply_zdecision_recall_delivery(items,
  turn_gate_id="", delivery_id="")`, trusted Hook rewrite, application receipt,
  and real active members.

- [ ] **Step 1: Write failing binding, application, and guard tests**

Prove a `UserPromptSubmit` for an `activating` Session creates one pending gate
and a bounded instruction naming the application tool. Then assert:

```python
bound = self._pre_tool(
    APPLY_RECALL_DELIVERY_TOOL,
    turn_id="turn-next",
    tool_input={
        "turn_gate_id": "model-gate",
        "delivery_id": "model-delivery",
        "items": APPLICATION_ITEMS,
    },
)
self.assertEqual(
    {
        "turn_gate_id": TRUSTED_GATE_ID,
        "delivery_id": DELIVERY_ID,
        "items": APPLICATION_ITEMS,
    },
    updated_input(bound),
)
```

Before commit, `Bash`, `apply_patch`, `Edit`, `Write`, `Agent`, and
`mcp__other__mutate` must be denied with a non-empty safe reason. After an exact
application commit they are allowed for that Turn. Missing, extra, duplicate,
wrong-revision, wrong-digest, cross-task, cross-Turn, stale-generation, or
unacknowledged delivery submissions remain denied. Conflict/uncertainty commits
the blocked items but keeps affected mutation denied. All-`not_applicable`
commits an empty active set and releases the Turn.
An exact application received from model context may atomically reconcile a
`delivery_unknown` record to `application_committed`; no other state may use
that recovery path.

- [ ] **Step 2: Run focused tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_hook_gate \
  tests.test_recall_handoff_service \
  tests.test_mcp_recall_handoff -v
```

Expected: missing application tool/binding and current enable incorrectly
creates an immediately active Session.

- [ ] **Step 3: Implement trusted application binding and atomic commit**

Add the application tool to the exact Hook matcher. Its Hook branch discards
model host coordinates, resolves the one current delivery from trusted Session
state, preserves only `items`, and injects the trusted `turn_gate_id` and
`delivery_id`.

The service method is fixed as:

```python
def apply(
    self,
    *,
    session_id: str,
    turn_id: str,
    gate_id: str,
    delivery_id: str,
    submission: RecallApplicationSubmission,
) -> dict[str, object]:
    delivery = self.store.commit_delivery_application(
        session_id=session_id,
        turn_id=turn_id,
        gate_id=gate_id,
        delivery_id=delivery_id,
        submission=submission,
        now=self.clock(),
    )
    return bounded_application_output(delivery)
```

The model-visible result returns disposition counts, receipt ID, intent epoch,
and safe display titles derived from `scope_summary`; it returns no Session,
Turn, CWD, score, vector, claim token, or local path.

Exempt only the exact app-only decision/status/ack tools from the activating
mutation guard; their server-side opaque binding remains mandatory. Do not
fail-open arbitrary `mcp__.*` tools.

- [ ] **Step 4: Run focused GREEN**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_host_state \
  tests.test_recall_hook_gate \
  tests.test_recall_handoff_service \
  tests.test_mcp_recall_confirmation \
  tests.test_mcp_recall_handoff -v
```

Expected: all tests pass; active items contain complete canonical Decision
envelopes and only applicable revisions.

- [ ] **Step 5: Commit Task 6**

```bash
git add \
  src/zdecision/agent/hooks.py \
  src/zdecision/agent/recall_handoff.py \
  src/zdecision/agent/recall_mcp.py \
  src/zdecision/agent/mcp_server.py \
  plugins/zdecision/hooks/hooks.json \
  tests/test_recall_hook_gate.py \
  tests/test_recall_handoff_service.py \
  tests/test_mcp_recall_handoff.py
git commit -m "feat: apply delivered Recall decisions"
```

---

### Task 7: Reuse active intent, restore full context, and remove the old proof

**Files:**
- Modify: `src/zdecision/recall/session.py`
- Modify: `src/zdecision/recall/__init__.py`
- Modify: `src/zdecision/agent/hooks.py`
- Modify: `src/zdecision/agent/recall_handoff.py`
- Modify: `src/zdecision/agent/recall_mcp.py`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `src/zdecision/agent/cli.py`
- Modify: `src/zdecision/app_server/models.py`
- Modify: `src/zdecision/app_server/gateway.py`
- Modify: `plugins/zdecision/hooks/hooks.json`
- Modify: `tests/test_recall_session_contracts.py`
- Modify: `tests/test_recall_host_state.py`
- Modify: `tests/test_recall_hook_gate.py`
- Modify: `tests/test_app_server_gateway.py`
- Delete: `tests/test_mcp_recall_host_gate.py`
- Delete: `tests/integration/test_recall_host_identity.py`
- Keep unchanged: `tests/integration/test_gate1_plugin_smoke.py`
- Keep untouched: `tests/integration/test_recall_host_gate.py` (untracked)

**Interfaces:**
- Consumes: active items and Task 1 provider.
- Produces: a provider-backed `gate_zdecision_turn`, full compact/clear
  restoration, and no Recall-specific App Server/probe code.

- [ ] **Step 1: Write failing reuse, changed-intent, restoration, and absence tests**

Add tests for:

```python
reuse = service.gate_turn(
    session_id=SESSION_ID,
    turn_id=TURN_2,
    gate_id=GATE_2,
    intent=ACTIVE_INTENT,
)
self.assertEqual("reuse", reuse["state"])
self.assertEqual(0, provider.retrieve_calls)
```

A changed intent must create one frozen tool-result delivery through the same
provider seam, return the complete shortlist to Codex, and keep the Turn gate
pending until `apply_zdecision_recall_delivery` commits. Ambiguity returns
display names and retrieves nothing. An ordinary “继续” reuses the active set.

Change compact/clear tests to parse `ZDECISION_RECALL_RESTORATION` with complete
active Decision envelopes, current application receipt, and context epoch;
replay restores exactly once. Product change retires the old set before new
routing. Startup/resume revalidates without another confirmation.

Add negative behavior assertions:

```python
with patch.object(
    AppServerGateway,
    "connect",
    side_effect=AssertionError("Recall must not connect an App Server"),
):
    run_recall_mcp_with_noop_transport()

for environment in ({}, {"ZDECISION_LIVE_ACCEPTANCE": "1"}):
    with patch.dict(os.environ, environment, clear=True):
        with self.assertRaises(SystemExit):
            agent_cli.build_parser().parse_args(["recall-host-gate", "clear"])
```

Exercise a same-intent gate and a changed-intent gate under the same forbidden
`AppServerGateway.connect` patch. Assert neither creates a host-probe artifact
or reads an acceptance fixture. Code review verifies removal of the obsolete
types; tests verify their former behavior is unreachable rather than grepping
source text.

Before deleting `tests/test_mcp_recall_host_gate.py`, port every assertion that
does not depend on App Server evidence, native-selection proof, or the live
probe into the replacement suites. In particular, retain confirmation
idempotency/card-digest tests in `tests/test_mcp_recall_confirmation.py`,
provider/receipt/restart ownership tests in
`tests/test_recall_handoff_service.py`, and closed MCP schema/cross-Turn replay
tests in `tests/test_mcp_recall_handoff.py`. Delete the old file only after a
test-name inventory proves each still-valid contract has a named replacement.

- [ ] **Step 2: Run focused tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_session_contracts \
  tests.test_recall_hook_gate \
  tests.test_mcp_recall_handoff \
  tests.test_app_server_gateway -v
```

Expected: current gate invokes active-Turn evidence, restoration contains only
a digest, and obsolete symbols/CLI remain.

- [ ] **Step 3: Implement local later-Turn gating and exact cleanup**

`gate_zdecision_turn` must:

1. validate the trusted gate and strict intent;
2. return and atomically commit `reuse` when the digest is unchanged;
3. use `RecallProvider.preflight()` and `.retrieve()` only for a changed Intent
   Epoch or explicit refresh;
4. expose the changed-intent shortlist as model-visible typed tool content;
5. leave the gate pending until application commits; and
6. block ambiguity/unavailable state without replacing the old active set.

For compact/clear, read `list_active_items()` and emit complete typed envelopes
once. Keep `SessionStart.additionalContextLimit` in `hooks.json` at `0`, which
the current Hook contract uses for the full already-bounded context rather
than a spill-file preview; test the exact value and the 10,000-byte Decision
budget plus fixed envelope metadata independently.

Remove Recall-only `ActiveTurnEvidence`, `SelectedSkill`,
`TurnItemEvidence`, `read_active_turn_evidence`,
`ActiveTurnEvidenceGateway`, `LiveHostProbeProvider`, `HostProbeEnvelope`, the
probe receipt code, `recall-host-gate` CLI, and the
`ZDECISION_LIVE_ACCEPTANCE` production branch. Preserve every App Server method
and type still referenced by Capture.

`run_mcp()` constructs `UnavailableRecallProvider()` directly and never calls
`AppServerGateway.connect()` for Recall.

- [ ] **Step 4: Run focused cleanup GREEN**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_session_contracts \
  tests.test_recall_handoff_contracts \
  tests.test_recall_host_state \
  tests.test_recall_hook_gate \
  tests.test_recall_handoff_service \
  tests.test_mcp_recall_confirmation \
  tests.test_mcp_recall_handoff \
  tests.test_app_server_gateway \
  tests.test_requested_capture \
  tests.test_reconciliation_runner -v
```

Expected: all pass; Capture App Server tests remain green and no tracked test
imports the removed proof.

- [ ] **Step 5: Commit Task 7**

```bash
git add \
  src/zdecision/recall/session.py \
  src/zdecision/recall/__init__.py \
  src/zdecision/agent/hooks.py \
  src/zdecision/agent/recall_handoff.py \
  src/zdecision/agent/recall_mcp.py \
  src/zdecision/agent/mcp_server.py \
  src/zdecision/agent/cli.py \
  src/zdecision/app_server/models.py \
  src/zdecision/app_server/gateway.py \
  plugins/zdecision/hooks/hooks.json \
  tests/test_recall_session_contracts.py \
  tests/test_recall_host_state.py \
  tests/test_recall_hook_gate.py \
  tests/test_mcp_recall_handoff.py \
  tests/test_app_server_gateway.py \
  tests/test_mcp_recall_host_gate.py \
  tests/integration/test_recall_host_identity.py
git commit -m "refactor: remove Recall App Server proof"
```

---

### Task 8: Align Plugin and active architecture documentation

**Files:**
- Modify: `plugins/zdecision/skills/zdecision/SKILL.md`
- Modify: `plugins/zdecision/skills/zdecision/agents/openai.yaml`
- Modify: `plugins/zdecision/.codex-plugin/plugin.json`
- Modify: `docs/architecture.md`
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `tests/test_recall_skill_contract.py`
- Modify: `tests/test_plugin_contract.py`
- Modify: `tests/test_skill_contract.py`

**Interfaces:**
- Consumes: implemented Gate A tool names and user flow.
- Produces: one non-contradictory active instruction set and a refreshed Plugin
  cache identity.

- [ ] **Step 1: Write failing executable routing/metadata tests and define Skill pressure scenarios**

The implemented Skill must direct Codex to:

- construct the exact seven-field intent before calling the confirmation tool;
- ask in chat when the Hook returns bounded product choices;
- selection is not authorization;
- after the click, keep the App attachment and wait for the next native message;
- classify every frozen item and call `apply_zdecision_recall_delivery` before
  affected mutation;
- ordinary later Turns call `gate_zdecision_turn` and reuse same intent;
- no second App Server, `thread/read`, `hookPrompt`, or `ui/message`; and
- production may report `recall_not_ready` until Gates B/C land.

Contract tests exercise frontmatter discovery policy, registered tool schemas,
manifest references, and the observable routing/result envelopes. They do not
assert prose substrings. Use `superpowers:writing-skills` to run RED/GREEN
pressure scenarios for the workflow above, and record those outcomes in the
task report. The real Task 10 Desktop acceptance is the final behavioral proof
that the installed Skill follows the workflow.

Manifest/README/architecture copy must describe next-native-message UX, not a
“later native Turn gate” backed by App Server proof. Require a new sanitized
cachebuster version matching the repository's existing version regex.

- [ ] **Step 2: Run contract tests to verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_skill_contract \
  tests.test_plugin_contract \
  tests.test_skill_contract -v
```

Expected: current metadata/tool composition still exposes the old post-click
gate and the Skill pressure baseline follows the unsupported continuation path.

- [ ] **Step 3: Update active instructions and version**

Keep `allow_implicit_invocation: false`. Do not create another Recall Skill.
Update `default_prompt` to describe explicit confirmation and the next native
message without claiming retrieval is already production-ready. Increment only
the manifest build cachebuster after every Plugin byte is final.

In `docs/architecture.md`, retain App Server operations for Capture and replace
only the Recall sentence in section 12.4. In `AGENTS.md`, replace “later native
Turn gating” with the approved delivery/application language. Do not edit the
Central Web or Candidate design.

- [ ] **Step 4: Run contract GREEN**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_skill_contract \
  tests.test_plugin_contract \
  tests.test_skill_contract -v
```

Expected: all pass; manifest schema and cachebuster remain valid.

- [ ] **Step 5: Commit Task 8**

```bash
git add \
  plugins/zdecision/skills/zdecision/SKILL.md \
  plugins/zdecision/skills/zdecision/agents/openai.yaml \
  plugins/zdecision/.codex-plugin/plugin.json \
  docs/architecture.md \
  AGENTS.md \
  README.md \
  tests/test_recall_skill_contract.py \
  tests/test_plugin_contract.py \
  tests/test_skill_contract.py
git commit -m "docs: align Plugin with Recall handoff"
```

---

### Task 9A: Freeze production and disposable Plugin identity

**Files:**
- Create: `src/zdecision/agent/recall_plugin_identity.py`
- Modify: `src/zdecision/agent/recall_host_state.py`
- Modify: `src/zdecision/agent/hooks.py`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `tests/test_plugin_contract.py`
- Modify: `tests/test_recall_host_state.py`
- Modify: `tests/test_recall_hook_gate.py`
- Modify: `tests/test_mcp_recall_handoff.py`

**Interfaces:**
- Consumes: design section 14's six-field immutable identity and existing
  production Plugin bytes.
- Produces: one production-default identity seam shared by Store, Hook, and MCP
  composition, plus exact contract tests for production and disposable
  identities.
- Does not change: Plugin bytes, CLI arguments, SQLite schema, bundle-digest
  inputs, provider behavior, or Gate B/C code.

- [ ] **Step 1: Write RED identity and bundle-contract tests**

Add tests that first import the missing identity module and require:

```python
PRODUCTION_RECALL_PLUGIN_IDENTITY == RecallPluginIdentity(
    plugin_name="zdecision",
    mcp_server_key="zdecision-local",
    mcp_command="zdecision-agent",
    mcp_args=("mcp",),
    hook_command="zdecision-agent hook",
    recall_skill_relative_path="skills/zdecision/SKILL.md",
)
```

The RED matrix must cover every individual manifest name, MCP key, command,
ordered argument, Skill path, Hook command, required event, matcher, timeout,
additional-context limit, mutation coverage, and extra-Hook substitution. It
must also reject malformed names, underscores, ambiguous namespace mappings,
NULs, overlong values, and non-normalized Skill paths. Extend the temporary
valid-Plugin fixtures in `test_recall_host_state.py` and
`test_recall_hook_gate.py` with a valid default `hooks/hooks.json`; otherwise
the new mandatory Hook validation would make unrelated tests invalid.

- [ ] **Step 2: Run the focused suite to verify RED**

```bash
.venv/bin/python -m unittest \
  tests.test_plugin_contract \
  tests.test_recall_host_state \
  tests.test_recall_hook_gate \
  tests.test_mcp_recall_handoff -v
```

Expected: missing `RecallPluginIdentity`/identity injection and new
substitution assertions fail. Existing production behavior remains unchanged.

- [ ] **Step 3: Implement the immutable value and exact verifier**

Create the identity module with only these stored fields and derived helpers:

```python
_HOOK_TOOL_BASENAMES = frozenset(
    {
        "show_zdecision_update",
        "show_zdecision_recall_confirmation",
        "decide_zdecision_recall",
        "get_zdecision_recall_handoff",
        "ack_zdecision_recall_delivery",
        "apply_zdecision_recall_delivery",
        "gate_zdecision_turn",
    }
)
_PRE_TOOL_PREFIX_BASENAMES = (
    "show_zdecision_update",
    "show_zdecision_recall_confirmation",
    "apply_zdecision_recall_delivery",
    "gate_zdecision_turn",
)


@dataclass(frozen=True)
class RecallPluginIdentity:
    plugin_name: str
    mcp_server_key: str
    mcp_command: str
    mcp_args: tuple[str, ...]
    hook_command: str
    recall_skill_relative_path: str

    @property
    def tool_namespace(self) -> str:
        return self.mcp_server_key.replace("-", "_")

    def tool_name(self, basename: str) -> str:
        if basename not in _HOOK_TOOL_BASENAMES:
            raise ValueError("unsupported identity-sensitive MCP tool")
        return f"mcp__{self.tool_namespace}__{basename}"

    @property
    def pre_tool_matcher(self) -> str:
        return "|".join(
            (
                *(self.tool_name(name) for name in _PRE_TOOL_PREFIX_BASENAMES),
                "Bash",
                "apply_patch",
                "Edit",
                "Write",
                "Agent",
                "mcp__.*",
            )
        )


@dataclass(frozen=True)
class VerifiedRecallPluginBundle:
    root: Path
    skill_path: Path
    bundle_digest: str
```

`__post_init__` must call private validators for all six fields and enforce the
closed grammar and byte limits from design section 14 before the value exists.
`tool_name()` accepts only the closed Candidate/Recall basenames. Expose one
`verify_recall_plugin_bundle(plugin_root, identity)` function returning the
verified value or `None`; do not put mutable cache or filesystem state on the
identity. The verifier preserves the existing digest bytes exactly:

```text
manifest raw bytes + NUL + .mcp.json raw bytes + NUL + SKILL.md raw bytes
```

It validates the resolved root and containment; exact manifest name and absent
manifest `hooks`; one exact MCP key/command/ordered args; the exact normalized
Skill path; and the complete eight-event Hook security projection. Hook bytes
are mandatory for every acceptance but are not added to the digest. Return the
resolved root explicitly so callers never reconstruct it with `parents[2]`.

- [ ] **Step 4: Make Store, Hook, and MCP composition identity-owned**

Give `RecallHostStore.open()` a keyword-only production-default identity and
store it immutably for the lifetime of the Store. All attempt, delivery,
application, restart, reuse, compact, resume, and bound-Skill revalidation must
call the Store's verifier. A Store reopened with another identity may open the
database but cannot authorize rows frozen under the first identity. Keep a
production-default `installed_recall_skill_path()` compatibility wrapper if an
existing public test needs it; the wrapper delegates to the new verifier.

Thread the same production-default identity through `handle_hook()`,
`handle_pre_tool_hook()`, Recall binding/lifecycle/guard helpers, and trusted
root lookup. Every full Candidate/Recall MCP tool name comes from
`identity.tool_name()`; remove independent namespace constants from branch
decisions. Internally opened Stores receive that identity. A supplied Store
with a different identity fails closed. Use `VerifiedRecallPluginBundle.root`
instead of inferring a root from Skill-path depth.

Freeze the composition keyword as `recall_identity` for Hook and MCP functions
and the Store's read-only property as `identity`. Add keyword-only
`recall_identity=PRODUCTION_RECALL_PLUGIN_IDENTITY` to `create_mcp_server()` and
construct `FastMCP(recall_identity.mcp_server_key)`. `run_mcp()` and the
ordinary Hook CLI retain their public signatures and explicitly use only the
production constant; there is no CLI or environment identity selector.

- [ ] **Step 5: Prove cross-identity and production-default behavior GREEN**

Add focused behavior tests proving:

1. an injected disposable identity accepts only its exact Hook tool namespace;
2. the production namespace is rejected by that composition and vice versa;
3. Store/Hook identity mismatch fails closed;
4. a frozen attempt cannot be replayed/revalidated under another identity;
5. attempt, gate, restart, delivery, application, reuse, and compact checks all
   reuse the Store identity; and
6. `create_mcp_server()` uses the injected key while `run_mcp()` composes the
   production constant for both Store and server.

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_plugin_contract \
  tests.test_recall_host_state \
  tests.test_recall_hook_gate \
  tests.test_mcp_recall_handoff \
  tests.test_recall_skill_contract \
  tests.test_hook_latency \
  tests.test_mcp_inline_refresh \
  tests.test_mcp_recall_confirmation -v
.venv/bin/python -m compileall -q src
git diff --check
```

Expected: all pass; Hook latency remains within its existing bound; production
Plugin files and the digest algorithm are byte-for-byte unchanged.

- [ ] **Step 6: Run one scoped Task 9A review and commit exact paths**

Review only Task 9A against design sections 14.2–14.3. Fix confirmed findings
with one focused RED/GREEN loop, then commit:

```bash
git add \
  src/zdecision/agent/recall_plugin_identity.py \
  src/zdecision/agent/recall_host_state.py \
  src/zdecision/agent/hooks.py \
  src/zdecision/agent/mcp_server.py \
  tests/test_plugin_contract.py \
  tests/test_recall_host_state.py \
  tests/test_recall_hook_gate.py \
  tests/test_mcp_recall_handoff.py
git commit -m "feat: freeze Recall Plugin identity"
```

---

### Task 9: Build the production-boundary test vertical and disposable Desktop harness

**Files:**
- Create: `tests/integration/test_recall_handoff_gate_a.py`
- Create: `tests/integration/recall_gate_a_desktop_harness.py`
- Modify: `tests/test_recall_capture_isolation.py`

**Interfaces:**
- Consumes: the reviewed Task 9A identity seam, production Gate A Store/Hook/MCP
  composition, the production card resource, and a test-only provider.
- Produces: one automated production-boundary vertical and a generated,
  self-verifying disposable Desktop Plugin outside the production bundle.
- Does not prove: real Desktop `ui/update-model-context`; Task 10 remains the
  only host-level proof of that capability.

- [ ] **Step 1: Write the failing integrated vertical**

The automated integration must exercise the generated launcher and real
production boundaries through:

```text
enabled repository + native-Turn-shaped Hook input
  -> Hook preflight and frozen attempt
  -> production show-card resource/result
  -> app-only enable and one frozen delivery
  -> simulated exactly-one context-update acknowledgement
  -> next native Turn and trusted application binding
  -> covered mutation denied before application
  -> complete [applicable, not_applicable] submission
  -> application commit
  -> covered mutation released exactly once
  -> same-intent Turn reuse without retrieve or reinjection
  -> compact restoration with complete active envelope
  -> Store restart and exact identity revalidation
```

Assert preflight runs once and retrieval runs once for the logical delivery;
provider work happens outside SQLite transactions. Exact generated Hook/MCP
names succeed; production or mixed identities fail closed. Assert Candidate
tables, Capture state/eligibility, Central mock transport/counts, Registry
files, the production Plugin tree, production marketplace fixture, and
production database fixture are unchanged. Add a Capture-isolation case proving
formal envelopes, handoff instructions/markers, receipts, and application state
cannot become Prompt anchors, Candidate evidence, or Capture upload content.

- [ ] **Step 2: Run integration test to verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.integration.test_recall_handoff_gate_a \
  tests.test_recall_capture_isolation -v
```

Expected: missing vertical harness/provider composition.

- [ ] **Step 3: Implement the deterministic provider and generated identity**

Define `DeterministicGateAProvider` inside the integration test/harness. It
returns one fixed unambiguous `RecallPreflightReady` and two complete canonical
`DecisionRevision` fixtures: one clearly applicable to the designated target
and one clearly excluded. It records preflight/retrieve counts and performs no
network, Git, Central, Registry, App Server, or production-database operation.

The desktop harness supports exact subcommands:

```text
create --root <temporary-absolute-path> --repository <enabled-repository>
inspect --root <temporary-absolute-path>
cleanup --root <temporary-absolute-path>
```

`create` writes a disposable marketplace and Plugin below the supplied
fresh absolute root, outside the repository, home root, production cache, and
production database. Generate one unique closed-grammar identity, distinct
from `zdecision`/`zdecision-local`, and derive the Plugin folder, manifest and
marketplace names, MCP key, Hook matcher names, commands, and Skill path only
from it. The marketplace source is relative and resolves inside that disposable
marketplace root. The manifest omits `hooks`; the default `hooks/hooks.json`
exactly matches the identity verifier.

- [ ] **Step 4: Generate a self-verifying launcher and safe lifecycle**

Write one minimal launcher below the disposable Plugin root. The generated MCP
definition is exactly:

```text
command = <repository>/.venv/bin/python
args = [<absolute-generated-launcher>, "mcp"]
```

Every Hook command is the safely quoted equivalent of:

```text
<repository>/.venv/bin/python <absolute-generated-launcher> hook
```

The launcher contains the immutable identity literals and accepts only `hook`
or `mcp`; it accepts no root, database, provider, or identity argument. It
derives its root from its own resolved path and validates an ownership marker
containing launcher relative path/digest, root device/inode, and generation
before importing the tracked harness. It compares, never rewrites,
host-provided `PLUGIN_ROOT`. The tracked harness then directly composes
`RecallHostStore.open(database_path, identity=identity)`,
`handle_hook(..., recall_identity=identity)`, and
`create_mcp_server(..., recall_identity=identity)` with one isolated database
and the deterministic provider. It must not call `run_mcp()`, patch the
production constant, translate tool names, copy the identity validator, or
mutate bundle bytes after generation.

`inspect` and `cleanup` revalidate that exact marker generation and launcher
digest. Inspection returns only bounded counts/states and digest/receipt
prefixes—never a Prompt, Decision, Session/Turn/binding ID, absolute path,
command, trusted hash, row, or transcript. Cleanup refuses while any exact MCP
lease is live; after all leases exit it quarantines and deletes only the
validated root. Any mismatch is a hard failure and preserves the uncertain
root. The disposable root/state directories are mode `0700`; marker, launcher,
database sidecars, and lease/identity files are no broader than `0600`.

- [ ] **Step 5: Add failure, replay, privacy, and cleanup coverage**

Test marker/launcher/root generation mutations, each identity/bundle/Hook
substitution, mismatched `PLUGIN_ROOT`, mixed tool namespace, missing exact MCP
client, two concurrent MCP clients, restart replay, same-intent reuse, compact
restoration, and cleanup races. All must stop/fail closed without translation,
second App Server, private IPC, manual database mutation, `ui/message`, or
production-state changes. Assert no disposable MCP child process, TCP
connection, or named external Unix endpoint and no provider filesystem write.
The card and resource must be the production ones;
the application path must call production `RecallMcpTools`,
`RecallHandoffService`, and Hook guard code.

- [ ] **Step 6: Run automated vertical GREEN**

Run:

```bash
.venv/bin/python -m unittest \
  tests.integration.test_recall_handoff_gate_a \
  tests.test_recall_capture_isolation -v
.venv/bin/python -m compileall -q \
  src \
  tests/integration/recall_gate_a_desktop_harness.py \
  tests/integration/test_recall_handoff_gate_a.py
git diff --check
```

Expected: all pass; compile and diff checks produce no error.

- [ ] **Step 7: Run one scoped Task 9 review and commit exact paths**

Review only the test-only provider, generated launcher/Plugin vertical,
Capture isolation, privacy, and cleanup contract. Fix confirmed findings with
one focused RED/GREEN loop, then commit:

```bash
git add \
  tests/integration/test_recall_handoff_gate_a.py \
  tests/integration/recall_gate_a_desktop_harness.py \
  tests/test_recall_capture_isolation.py
git commit -m "test: add Recall Gate A vertical"
```

---

### Task 10: Run one real Desktop acceptance, clean up, and stop

**Files:**
- Create after real evidence:
  `docs/superpowers/acceptance/2026-08-10-recall-next-native-message-gate-a.md`
- Modify only if the real run exposes a confirmed Gate A defect: files already
  listed in Tasks 1–9 and their exact tests.
- Never modify: the two protected untracked paths.

**Interfaces:**
- Consumes: independently GREEN/approved Task 9A and Task 9, plus one user
  click/native message.
- Produces: sanitized Gate A acceptance evidence and a clean machine/repository
  state.

- [ ] **Step 1: Run the focused automated acceptance once before installation**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_handoff_contracts \
  tests.test_recall_host_state \
  tests.test_recall_hook_gate \
  tests.test_recall_handoff_service \
  tests.test_mcp_recall_confirmation \
  tests.test_mcp_recall_handoff \
  tests.test_plugin_contract \
  tests.test_recall_capture_isolation \
  tests.integration.test_recall_handoff_gate_a -v
```

Expected: all pass. If this fails, fix only the confirmed Gate A defect and
rerun this focused command; do not begin Desktop acceptance.

- [ ] **Step 2: Create and install the disposable Plugin**

Create a fresh exact temporary root with `mktemp -d`, record the resulting
absolute path, run the harness `create`, add its marketplace with the supported
Codex Plugin command, and install its unique selector. Verify `inspect` reports
the exact generated identity/launcher readiness in bounded form and confirm the
production `zdecision@zdecision-local` and all pre-existing marketplace entries
remain installed and unchanged; only the exact disposable marketplace entry may
be added temporarily. Before installation, record bounded read-only baselines
for the production marketplace entry, installed Plugin bundle bytes, and
production database. Review and trust the current disposable Plugin's exact
Hook hash separately for each of its eight required event entries via the
supported Codex Hook UI before starting the acceptance task; prior disposable
Hook trust does not carry across identities or event sources. Restart Codex
once after all eight trust records are present.

Do not place the temporary root under the repository, home root, or production
Plugin cache path. Do not reuse a prior probe database.

- [ ] **Step 3: Perform the bounded human acceptance**

Ask the user to select the disposable **ZDecision Gate A** entry in one enabled
repository and send a normal code-development request. Verify:

1. an ambiguous test request returns product choices and no card;
2. an unambiguous request renders the two-button card;
3. decline creates no Session or delivery;
4. enable performs one retrieval and one context update;
5. remount before another click performs no mutation;
6. the card tells the user to keep the attachment and send the next message;
7. the user's next native message sees both fixture Decisions without a read
   tool, calls the application tool, and classifies one applicable and one not
   applicable;
8. a covered code/tool mutation is denied before application and allowed after
   commit;
9. a second ordinary Prompt performs reuse with no retrieval or reinjection;
10. one compact/clear restores the complete applicable item once; and
11. no App Server process, Central request, Candidate mutation, or raw private
    content appears in bounded diagnostics; and
12. the selected turn exposes the exact generated MCP namespace, never falls
    back to production `zdecision-local`, and never translates a mismatched
    tool or Plugin root.

If app-only tools, remount recovery, or `ui/update-model-context` regresses,
record FAIL and stop. Do not substitute `ui/message`, a second App Server,
private IPC, or manual DB mutation.

- [ ] **Step 4: Clean the disposable environment before writing PASS**

Remove the exact disposable Plugin selector and marketplace, wait for every
exact launcher/MCP lease to exit, and invoke only the harness's validated
cleanup for the recorded temporary root. Verify the production
Plugin remains installed/enabled and the disposable selector/process/database
is absent. Verify the production marketplace entry, installed bundle bytes, and
production database exactly match their pre-install baselines. If exact cleanup
or baseline equality cannot be proven, stop and report it rather than using a
broad recursive deletion.

- [ ] **Step 5: Write and commit the sanitized acceptance report**

The report records:

- exact Codex Desktop/CLI/Python/MCP versions;
- PASS/FAIL for preflight, app-only action, context update, next-message
  application, guard, reuse, restoration, isolation, and cleanup;
- provider call counts and only redacted digest/receipt prefixes;
- the fact that the provider was test-only and production remains unavailable;
  and
- any bounded procedural deviation.

It contains no raw Prompt, full Decision, full marker, full receipt, task ID,
Session/Turn ID, absolute local path, database row, or tool transcript.

```bash
git add docs/superpowers/acceptance/2026-08-10-recall-next-native-message-gate-a.md
git commit -m "docs: record Recall Gate A acceptance"
```

- [ ] **Step 6: Run the complete committed suite exactly once**

The two protected untracked files are intentionally not part of the committed
suite. Run all Git-tracked tests without moving or editing them:

```bash
.venv/bin/python - <<'PY'
import subprocess
import sys

files = subprocess.check_output(
    ["git", "ls-files", "tests/test_*.py", "tests/integration/test_*.py"],
    text=True,
).splitlines()
raise SystemExit(subprocess.call([sys.executable, "-m", "unittest", *files, "-v"]))
PY
.venv/bin/python -m compileall -q src
git diff --check
git status --short --branch
```

Expected: every committed test passes; compile and diff checks are clean; only
the two protected untracked files may remain. Do not rerun the complete suite
after success.

If a confirmed Critical or Important defect appears, add one focused regression
test, make the smallest correction, run that focused test, then run the complete
suite one final time. Record non-blocking improvements and stop. Do not start
another broad architecture review, Skill blind test, or Gate B task.

## Completion boundary

Gate A is complete only when the real Desktop acceptance report is PASS and the
complete committed suite passes. The user-visible production Plugin must still
return bounded `recall_not_ready`; no report may claim real formal-Decision
Recall, signed distribution, or retrieval quality. The next independent work
item is reconciliation and approval of the Gate B trusted-distribution plan.
