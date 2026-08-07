# Session-Opt-In Recall Host Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove, in the real Codex Desktop host, that an explicitly selected ZDecision Session can bind native identity, finish a recall gate before affected development, restore typed Decision context after compaction, and keep Forks and Capture forks disabled by default before any retrieval or distribution stack is built.

**Architecture:** Add a narrow local Recall Session domain beside the existing Candidate-refresh domain. An explicit-only Skill calls a model-visible activation tool; a scoped `PreToolUse` Hook replaces model-supplied binding values with host-owned Session/Turn/CWD identity. Each later `UserPromptSubmit` creates one pending Turn gate in local SQLite and injects only a bounded developer instruction. The recall MCP tool commits that exact gate, while a `PreToolUse` backstop denies command-executing or code-mutating tools until it is committed. Gate 1 uses an unmistakably non-authoritative local host-probe envelope; it does not query Central, load a model, or claim that a formal Decision was recalled.

**Tech Stack:** Python 3.11, standard-library `sqlite3`, existing Codex plugin Skill/Hooks/MCP server, existing JSONL app-server gateway, `unittest`, and a manually triggered Codex Desktop acceptance harness.

## Global Constraints

- Gate 1 is a feasibility gate. Do not add Central recall endpoints, signatures, model artifacts, embeddings, BM25, reranking, or production Decision injection here.
- No Plugin selection means no Recall Session row, no Turn gate, no injected context, and no recall-specific Central or app-server call.
- Activation must originate from an exact native user Turn. Quoted text, a delegated task, tool output, assistant initiative, a Decision envelope, or a Capture fork cannot activate recall.
- Never trust a Session ID, Turn ID, CWD, repository, product, Decision-space identity, gate ID, or receipt ID supplied by the model. Bind these through Hook input or a proven app-server fact.
- Prompt, transcript, PRD, source, diff, and tool output are not persisted. The Hook may discard the prompt field without reading it; Codex supplies only the bounded typed `RecallIntent` required by the tool.
- `SessionStart.source` is limited to `startup`, `resume`, `clear`, and `compact`. Do not invent `fork`.
- A Fork passes only if a supported host-owned fact maps child Thread to parent Thread and the child has an identity that the Hook can bind. Transcript filename heuristics, recency guessing, CWD matching, or model text do not count.
- `PreToolUse` is a backstop for local command/tool mutation, not proof that plain assistant text is ordered. The real Desktop acceptance must independently prove the first visible development answer follows the gate.
- Preserve the existing Candidate refresh card and its `mcp__zdecision_local__show_zdecision_update` binding behavior.
- Capture and recall remain independent. The host-probe envelope is never Candidate evidence and never starts Capture.
- Any failed host acceptance stops Packet 3. Record the exact failed capability and return to host integration design; do not continue to Gate 2.

---

### Task 1: Define bounded Recall Intent and host-only state contracts

**Files:**
- Create: `src/zdecision/recall/__init__.py`
- Create: `src/zdecision/recall/session.py`
- Create: `tests/test_recall_session_contracts.py`

**Interfaces:**

```python
RecallSessionState = Literal[
    "activating", "active", "blocked", "bypassed", "dormant", "closed"
]
GateDisposition = Literal[
    "reuse", "retrieve", "clarify_product", "refresh_required", "blocked"
]

@dataclass(frozen=True)
class RecallIntent:
    target_decision_space_ids: tuple[str, ...]
    explicit_multi_space: bool
    feature_goal: str
    domain_objects: tuple[str, ...]
    repository_relative_paths: tuple[str, ...]
    constraints: tuple[str, ...]
    exclusions: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object) -> "RecallIntent": ...
    def to_dict(self) -> dict[str, object]: ...
    @property
    def digest(self) -> str: ...

@dataclass(frozen=True)
class HostProbeEnvelope:
    probe_id: str
    marker: Literal["host_gate_fixture_not_formal"]
    instruction: str

@dataclass(frozen=True)
class TurnGateResult:
    disposition: GateDisposition
    intent_digest: str
    context_epoch: int
    intent_epoch: int
    probe: HostProbeEnvelope | None
```

- [ ] **Step 1: Write failing strict-contract tests**

Cover exact-field parsing, tuple normalization, canonical digest stability, duplicate Decision-space rejection, one-space default, explicit multi-space consistency, relative-path normalization, traversal/absolute-path rejection, per-string limits, total 10 KB Intent limit, and unknown-field rejection. Add a test proving `HostProbeEnvelope.marker` cannot be changed to `formal_decision`.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
.venv/bin/python -m unittest tests.test_recall_session_contracts -v
```

Expected: FAIL because `zdecision.recall.session` does not exist.

- [ ] **Step 3: Implement the smallest strict values**

Use `canonical_json_bytes()` for `RecallIntent.digest`. Permit one to eight target leaves only when `explicit_multi_space` is true; otherwise require exactly one. Bound `feature_goal` to 2,000 characters, each list to 32 members, each member to 512 characters, and reject blank normalized members.

- [ ] **Step 4: Run the focused test and verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_recall_session_contracts -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add \
  src/zdecision/recall/__init__.py \
  src/zdecision/recall/session.py \
  tests/test_recall_session_contracts.py
git commit -m "feat: define recall session contracts"
```

---

### Task 2: Persist trusted activation, Turn gates, context epochs, and dormancy

**Files:**
- Create: `src/zdecision/agent/recall_host_state.py`
- Create: `tests/test_recall_host_state.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class RecallSession:
    session_id: str
    state: RecallSessionState
    authorization_turn_id: str
    cwd: str
    context_epoch: int
    intent_epoch: int
    active_intent_digest: str | None
    active_set_digest: str | None
    last_gate_turn_id: str | None

@dataclass(frozen=True)
class TurnGate:
    gate_id: str
    session_id: str
    turn_id: str
    context_epoch: int
    intent_epoch: int
    active_generation: int | None
    state: Literal["pending", "committed", "blocked"]
    result_digest: str | None

class RecallHostStore:
    @classmethod
    def open(cls, path: Path) -> "RecallHostStore": ...
    def bind_activation(..., binding_id: str) -> RecallSession: ...
    def begin_turn_gate(..., gate_id: str) -> TurnGate: ...
    def commit_turn_gate(..., result: TurnGateResult) -> TurnGate: ...
    def require_committed_gate(self, session_id: str, turn_id: str) -> TurnGate: ...
    def begin_context_epoch(..., compaction_key: str) -> ContextRestoration: ...
    def mark_dormant(self, session_id: str, ended_at: datetime) -> RecallSession | None: ...
    def begin_resume(self, session_id: str, cwd: str, now: datetime) -> RecallSession | None: ...
    def bind_internal_thread(
        self,
        *,
        thread_id: str,
        parent_thread_id: str,
        purpose: Literal["capture", "reconciliation"],
        operation_id: str,
        now: datetime,
    ) -> InternalThreadBinding: ...
    def is_internal_thread(self, thread_id: str) -> bool: ...
```

- [ ] **Step 1: Write failing state-machine and crash/replay tests**

Create tests for:

- no row for an unselected Session;
- one activation binding frozen to exact Session, Turn, and normalized absolute CWD;
- same binding replay returning the committed row and conflicting replay failing;
- one gate per native Turn, with cross-Session, cross-Turn, and stale-context replay rejected;
- `commit_turn_gate()` atomically updating the gate and Session epoch;
- a failed/invalid result leaving the prior active set unchanged but the current Turn blocked;
- `compact`/`clear` restoration keyed by `(session_id, source, latest_observed_turn_id, active_set_digest)` so one host event restores once, while a later real compaction after another Turn creates a new epoch;
- `SessionEnd` moving only activated Sessions to `dormant` and `resume` preserving authorization while requiring revalidation; and
- internal Capture/reconciliation Threads being permanently recall-disabled even if inherited context contains an activation or receipt; and
- the existing Capture `session_leases` table remaining untouched.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_host_state -v
```

Expected: FAIL because the focused store does not exist.

- [ ] **Step 3: Implement a focused SQLite store**

Open the existing Agent database path with WAL and `busy_timeout=5000`, following `ControlBindingStore.open()`. Own only these new tables:

```sql
recall_sessions
recall_activation_bindings
recall_turn_gates
recall_context_restorations
recall_internal_threads
```

Use `BEGIN IMMEDIATE` for activation, gate commit, and context restoration. Do not add Recall columns to `agent_events`, `session_leases`, or Candidate tables.

- [ ] **Step 4: Run GREEN and the existing Agent-store regressions**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_host_state \
  tests.test_control_binding_hook \
  tests.test_event_ledger -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/zdecision/agent/recall_host_state.py tests/test_recall_host_state.py
git commit -m "feat: persist trusted recall session gates"
```

---

### Task 3: Bind recall MCP calls and guard active development Turns

**Files:**
- Modify: `src/zdecision/agent/hooks.py`
- Modify: `src/zdecision/agent/events.py`
- Modify: `plugins/zdecision/hooks/hooks.json`
- Create: `tests/test_recall_hook_gate.py`
- Modify: `tests/test_plugin_contract.py`
- Modify: `tests/test_hook_latency.py`

**Hook dispatch:**

```python
ACTIVATE_RECALL_TOOL = "mcp__zdecision_local__activate_zdecision_recall"
TURN_GATE_TOOL = "mcp__zdecision_local__gate_zdecision_turn"
RECALL_MUTATION_MATCHER = (
    "Bash|apply_patch|Edit|Write|Agent|mcp__.*"
)

    def handle_pre_tool_hook(..., recall_store: RecallHostStore | None) -> HookResponse:
    if tool_name == CONTROL_BINDING_TOOL:
        return handle_control_binding_hook(...)
    if tool_name in (ACTIVATE_RECALL_TOOL, TURN_GATE_TOOL):
        return bind_recall_tool_call(...)
    return guard_active_turn_tool(...)
```

- [ ] **Step 1: Write failing Hook tests**

Prove all of the following:

- the Candidate render tool still receives its existing `control_id` rewrite;
- an activation tool call without host Session/Turn/CWD, from a subagent (`agent_id` present), or outside an enabled registered repository is denied;
- a valid activation call receives only a Hook-generated `activation_binding_id`; any model-supplied value is replaced;
- `UserPromptSubmit` creates a pending gate only for an already active Session and returns a bounded `additionalContext` instruction naming no private path or raw Prompt;
- the Turn-gate tool receives only a Hook-generated/bound `turn_gate_id`;
- `Bash`, `apply_patch`, `Edit`, `Write`, `Agent`, and non-ZDecision MCP calls are denied while that exact active Turn gate is pending or blocked and allowed after commit;
- unselected Sessions and bypassed Sessions retain existing fail-open tool behavior;
- malformed, replayed, and cross-Turn gates fail closed for the active Session;
- `SessionStart(source=compact|clear)` returns one typed restoration envelope for the committed active set and a replay returns the same receipt without advancing the epoch;
- `PreCompact(trigger=manual|auto)` and `PostCompact` bind one exact native `turn_id` token consumed by the following `SessionStart(source=compact)`; an unmatched token fails closed for an active Session;
- `startup` and `resume` do not increment `context_epoch`;
- `SessionEnd` marks Recall state dormant without changing Candidate event behavior; and
- Hook output never contains Prompt, transcript path, source, diff, absolute path, Session ID, or Turn ID.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_hook_gate \
  tests.test_plugin_contract.PluginContractTests.test_plugin_registers_five_lifecycle_hooks_and_one_render_matcher -v
```

Expected: FAIL because recall binding/guard dispatch and matcher coverage do not exist.

- [ ] **Step 3: Implement dispatch without broadening event persistence**

Keep `HookInvocation` limited to safe lifecycle facts. `PreToolUse` recall handling remains a separate trusted control path, as Candidate binding does today. For active Sessions, `UserPromptSubmit` may open `RecallHostStore`, create the bound pending gate, and return developer context; it must not read the prompt, call a model, or contact Central.

Update the single `PreToolUse` matcher to cover Candidate binding, both recall MCP tools, local command/code mutation, Agent spawning, and MCP calls. Register `PreCompact` and `PostCompact` with matcher `manual|auto`; their bounded tokens make automatic/manual compact distinguishable without treating `SessionStart` as a unique event. Set the `SessionStart` restoration handler's `additionalContextLimit` to `0`, which the current host contract defines as passing the full bounded context directly; keep the restored envelope itself below the later 10,000-byte Decision budget plus fixed metadata. Keep timeout at three seconds. The Hook returns an explicit `permissionDecision: deny` only for an active Session with an incomplete gate or an invalid recall binding.

For `clear`, use the equivalent local key `(session_id, source, latest_observed_turn_id, active_set_digest)` and keep the real Desktop replay test mandatory. If the host can deliver two semantically distinct clears with the same key and requires two restorations, stop Gate 1 instead of adding wall-clock identity.

- [ ] **Step 4: Verify latency and regressions**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_hook_gate \
  tests.test_control_binding_hook \
  tests.test_event_ledger \
  tests.test_plugin_contract \
  tests.test_hook_latency -v
```

Expected: all tests pass; the same-intent Hook-only path remains below the existing Hook latency budget and performs no network call.

- [ ] **Step 5: Commit**

```bash
git add \
  src/zdecision/agent/hooks.py \
  src/zdecision/agent/events.py \
  plugins/zdecision/hooks/hooks.json \
  tests/test_recall_hook_gate.py \
  tests/test_plugin_contract.py \
  tests/test_hook_latency.py
git commit -m "feat: gate active recall turns in hooks"
```

---

### Task 4: Add explicit-only activation and gate MCP tools

**Files:**
- Create: `src/zdecision/agent/recall_mcp.py`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `src/zdecision/agent/cli.py`
- Create: `tests/test_mcp_recall_host_gate.py`

**Interfaces:**

```python
class RecallGateProvider(Protocol):
    def activate(self, intent: RecallIntent) -> TurnGateResult: ...
    def gate(self, previous: RecallSession, intent: RecallIntent) -> TurnGateResult: ...

class RecallMcpTools:
    def activate_zdecision_recall(
        self, *, activation_binding_id: str, intent: object
    ) -> dict[str, object]: ...

    def gate_zdecision_turn(
        self, *, turn_gate_id: str, intent: object
    ) -> dict[str, object]: ...
```

Production wiring in Gate 1 uses a readiness provider that returns `blocked` with code `host_gate_only`; it never claims an empty or formal result. A live-acceptance-only provider may return one `HostProbeEnvelope(marker="host_gate_fixture_not_formal")` after the user explicitly prepares the host probe.

- [ ] **Step 1: Write failing tool tests**

Test exact JSON schemas, missing/unknown bindings, input size rejection, activation replay, gate replay, cross-Turn binding rejection, atomic commit-before-response, response-loss reconciliation, and the unmistakable host-probe marker. Assert that neither tool accepts Session/Turn/CWD/product/generation fields from the model and neither tool exposes native IDs in output. Until Task 6 supplies exact native Turn evidence, activation returns `blocked: native_selection_unproven`; a model calling the tool is not itself authority.

Add a CLI fixture command available only when `ZDECISION_LIVE_ACCEPTANCE=1`:

```text
zdecision-agent recall-host-gate prepare --cwd /absolute/enabled/repository
zdecision-agent recall-host-gate clear
```

It writes only a bounded `HostProbeEnvelope`; it cannot write a `DecisionRevision`, Candidate, Review, or Registry file.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_mcp_recall_host_gate -v
```

Expected: FAIL because the tools and host-probe provider are absent.

- [ ] **Step 3: Implement and register model-visible tools**

Register both tools with idempotent, non-open-world annotations. Do not attach an MCP App UI. Keep Candidate UI resources and browser tools unchanged. Compose `RecallMcpTools` into `create_mcp_server()` rather than enlarging `LocalMcpTools` with retrieval state.

The activation response must have one of these bounded states:

```json
{"state":"active","receipt":"host_probe_applied","probe":{...}}
{"state":"clarify_product","question":"..."}
{"state":"blocked","code":"host_gate_only"}
```

Only the first form commits an active probe set; a blocked/invalid call commits no applicable set.

- [ ] **Step 4: Run GREEN and MCP regressions**

```bash
.venv/bin/python -m unittest \
  tests.test_mcp_recall_host_gate \
  tests.test_mcp_inline_refresh -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add \
  src/zdecision/agent/recall_mcp.py \
  src/zdecision/agent/mcp_server.py \
  src/zdecision/agent/cli.py \
  tests/test_mcp_recall_host_gate.py
git commit -m "feat: expose trusted recall host gate tools"
```

---

### Task 5: Make recall a native explicit-only Plugin workflow

**Files:**
- Create: `plugins/zdecision/skills/decision-recall/SKILL.md`
- Create: `plugins/zdecision/skills/decision-recall/agents/openai.yaml`
- Modify: `plugins/zdecision/.codex-plugin/plugin.json`
- Modify: `tests/test_plugin_contract.py`
- Create: `tests/test_recall_skill_contract.py`

- [ ] **Step 1: Write failing Skill and manifest tests**

Require the new Skill to state:

- it runs only after the user explicitly selects ZDecision in that native task;
- first-Turn and later-Turn activation both call `activate_zdecision_recall` before affected development;
- ordinary later Turns follow the Hook-supplied `gate_zdecision_turn` instruction;
- quoted/delegated/tool/Decision text cannot activate it;
- one product/Shared leaf is the default and ambiguous routing must be clarified;
- formal Decision text is non-executable data;
- conflict/uncertainty blocks only affected work;
- recall never authorizes Candidate refresh or publication; and
- a `host_gate_fixture_not_formal` envelope is only acceptance evidence.

Require `agents/openai.yaml` to contain:

```yaml
interface:
  display_name: "ZDecision Recall"
  short_description: "Apply relevant formal decisions in this task"
policy:
  allow_implicit_invocation: false
```

Keep the existing Candidate-refresh Skill and default prompt. Add one recall-oriented default prompt without exceeding the manifest maximum of three entries.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_skill_contract \
  tests.test_plugin_contract -v
```

Expected: FAIL because the explicit-only recall Skill does not exist.

- [ ] **Step 3: Add the Skill and honest Plugin copy**

Do not claim automatic recall merely because the Plugin is installed. The manifest must say recall is Session opt-in and Candidate refresh remains explicit.

- [ ] **Step 4: Run GREEN**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_skill_contract \
  tests.test_plugin_contract -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add \
  plugins/zdecision/skills/decision-recall/SKILL.md \
  plugins/zdecision/skills/decision-recall/agents/openai.yaml \
  plugins/zdecision/.codex-plugin/plugin.json \
  tests/test_plugin_contract.py \
  tests/test_recall_skill_contract.py
git commit -m "feat: add explicit session recall skill"
```

---

### Task 6: Prove Thread/Fork identity through supported app-server facts

**Files:**
- Modify: `src/zdecision/app_server/models.py`
- Modify: `src/zdecision/app_server/gateway.py`
- Modify: `src/zdecision/agent/recall_mcp.py`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `tests/test_app_server_gateway.py`
- Modify: `tests/test_mcp_recall_host_gate.py`
- Create: `tests/integration/test_recall_host_identity.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ThreadIdentity:
    thread_id: str
    session_tree_id: str
    forked_from_id: str | None
    cwd: str
    ephemeral: bool

@dataclass(frozen=True)
class ActiveTurnEvidence:
    thread: ThreadIdentity
    turn_id: str
    selected_skills: tuple[SelectedSkill, ...]
    ordered_items: tuple[TurnItemEvidence, ...]

class AppServerGateway:
    def read_thread_identity(self, thread_id: str) -> ThreadIdentity: ...
    def read_active_turn_evidence(
        self, thread_id: str, turn_id: str
    ) -> ActiveTurnEvidence: ...
```

- [ ] **Step 1: Write failing protocol validation tests**

Test `thread/read(includeTurns=False)` for a root and a child, requiring returned `thread.id`, `thread.sessionId`, `thread.forkedFromId`, `cwd`, and `ephemeral` to be type-safe. Reject a response whose ID differs from the request, a self-parent, a malformed parent, or contradictory root/child fields. Extend `fork_disposable_thread()` tests so an available `forkedFromId` is exact, not advisory.

Also test `thread/read(includeTurns=True)` for the exact active Turn. Parse native `userMessage.content[]` items of type `skill` or `mention`, retaining only bounded `name` and normalized installed-plugin `path`. Parse ordered Turn item evidence for `hookPrompt`, `mcpToolCall`, `agentMessage`, `commandExecution`, `fileChange`, and `contextCompaction`; store only type, item ID, tool name, and typed ZDecision receipt/probe ID, never message text, Prompt text, tool arguments, source, or output.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest \
  tests.test_app_server_gateway.AppServerGatewayTests.test_reads_root_and_fork_identity -v
```

Expected: FAIL because `ThreadIdentity` and `read_thread_identity()` do not exist.

- [ ] **Step 3: Implement only stable app-server fields**

Use the official `thread/read` response. Do not enable experimental `parentThreadId` filters, infer ancestry from `session_tree_id`, or read rollout/transcript files. `session_tree_id` is tree provenance; it is not automatically the Hook Session identity. Activation accepts native-selection authority only when the exact active Turn contains a `skill` or `mention` item whose resolved path belongs to the installed ZDecision recall Skill. Ordinary prompt text and `allow_implicit_invocation` are insufficient.

The ordered item view is acceptance evidence for the first-answer barrier: the ZDecision activation/gate MCP item and its Hook prompt must precede the first substantive `agentMessage`, command execution, or file change. If an in-progress Turn cannot be read from the controlled app-server connection before the answer, Gate 1 fails; do not fall back to post-hoc transcript parsing.

- [ ] **Step 4: Add a live identity comparison test**

The live test is skipped unless `ZDECISION_LIVE_ACCEPTANCE=1`. It records one root Hook `session_id`, reads that exact value as `thread.id`, then repeats in a user-visible Codex Desktop Fork and compares:

```text
root hook session_id == root thread.id
child hook session_id == child thread.id
child thread.forkedFromId == root thread.id
child thread.sessionId == root thread.sessionId
```

If either Hook `session_id` cannot identify the exact current `thread.id`, the test fails with `host_thread_identity_unavailable`; do not add CWD/recency/transcript fallback.

Wire activation to `read_active_turn_evidence()` only after these unit contracts pass. The exact active Turn must contain native ZDecision recall Skill/mention evidence and the Hook-bound CWD must equal `ThreadIdentity.cwd`; otherwise return `native_selection_unproven` without activating.

- [ ] **Step 5: Run focused unit tests**

```bash
.venv/bin/python -m unittest \
  tests.test_app_server_gateway \
  tests.integration.test_recall_host_identity -v
```

Expected before live acceptance: unit tests pass and the live test is skipped.

- [ ] **Step 6: Commit**

```bash
git add \
  src/zdecision/app_server/models.py \
  src/zdecision/app_server/gateway.py \
  src/zdecision/agent/recall_mcp.py \
  src/zdecision/agent/mcp_server.py \
  tests/test_app_server_gateway.py \
  tests/test_mcp_recall_host_gate.py \
  tests/integration/test_recall_host_identity.py
git commit -m "feat: validate recall thread and fork identity"
```

---

### Task 7: Implement the Recall-to-Capture provenance firewall

The former marker-only exclusion is superseded. The authoritative design and
implementation plan are:

- `../specs/2026-08-07-recall-capture-provenance-design.md`
- `2026-08-07-recall-capture-provenance.md`

- [ ] **Step 1: Execute the five focused implementation tasks**

Use the focused plan's RED/GREEN sequence and five bounded commits. Do not
substitute Prompt-marker filtering, transcript parsing, raw Prompt storage, or
model-authored source labels.

- [ ] **Step 2: Apply its completion and hard-stop rules**

Task 7 is complete only after the focused vertical tests and one full suite
pass, Central receives only the minimal provenance kind/digest, legacy records
remain readable, and the SDD progress file records the exact evidence. If the
host prompt-event association is unavailable, record
`capture_evidence_provenance_unavailable` and stop Packet 3.

---

### Task 8: Run the real Codex Desktop Host Gate and stop on any failed capability

**Files:**
- Create: `tests/integration/test_recall_host_gate.py`
- Create after the run: `docs/superpowers/acceptance/2026-08-06-recall-host-gate.md`

- [ ] **Step 1: Add an automated preflight integration test**

The test composes real Hook JSON, Recall stores, MCP domain methods, and a fake app-server response to prove:

- unselected task has zero recall state;
- first-Turn and later-Turn activation bind exact native identity;
- pending gate blocks a mutation tool and committed gate permits it;
- same-intent gate replay is idempotent;
- compact/clear restoration occurs once;
- child identity begins disabled; and
- one native Prompt produces one stable frozen Hook anchor while Capture and
  reconciliation preserve the approved provenance firewall.

- [ ] **Step 2: Run the focused automated Gate suite**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_session_contracts \
  tests.test_recall_host_state \
  tests.test_recall_hook_gate \
  tests.test_mcp_recall_host_gate \
  tests.test_recall_skill_contract \
  tests.test_app_server_gateway \
  tests.test_capture_provenance \
  tests.test_recall_capture_isolation \
  tests.integration.test_recall_host_gate -v
```

Expected: all tests pass.

- [ ] **Step 3: Prepare the unmistakable live probe**

```bash
ZDECISION_LIVE_ACCEPTANCE=1 .venv/bin/zdecision-agent \
  recall-host-gate prepare \
  --cwd /Users/zhaohuiying/Desktop/Zstack-repos/zdecision
```

Restart Codex only if the changed local Plugin bundle requires reload. Trust the changed Hook definition when the app asks.

- [ ] **Step 4: Execute the seven real Desktop cases**

Record native task IDs, operation receipts, timestamps, and sanitized results only; never copy Prompt/PRD/source/transcript into the evidence file.

1. **No selection:** ordinary development Prompt produces no recall tool call, row, or receipt.
2. **First Turn:** select ZDecision and ask for a small code change. The activation tool and host probe must appear before any substantive development answer or mutation tool.
3. **Later Turn:** begin without ZDecision, exchange at least two Turns, then select it. Activation must use prior context and precede continuation.
4. **Later active Turn:** force a pending gate, demonstrate one denied mutation attempt, commit the gate, and demonstrate the exact tool is then allowed. Replay another Turn's gate and verify denial.
5. **Compact/clear:** relevant probe -> an empty-match `继续` -> compact/clear. The same active probe restores once; the next Prompt does not duplicate it. Repeat with a maximum-size 10,000-byte synthetic envelope and prove `thread/read` contains the complete marker/digest rather than a saved-file preview.
6. **Fork:** create a real user-visible Fork. Prove supported parent/child facts, child disabled state, inherited probe marked inactive, and a conflicting child task does not apply it before explicit child activation.
7. **Capture provenance:** run the existing explicit Candidate refresh from a
   Session containing the probe. Prove one ordinary native Prompt produces one
   stable Hook anchor; the Capture fork selects only IDs from its frozen
   manifest; retry, resume, compact, and Fork do not mint or rebind anchors;
   the probe plus an unrelated **继续** produces no eligible Candidate; a later
   independent explicit user rule can qualify through its own anchor; and a
   Hook-created continuation is either distinguishable from physical-user
   submission or is conservatively retained only as
   `hook_observed_user_prompt_anchor`, never upgraded to human-authorship
   proof.

- [ ] **Step 5: Apply the hard stop rule**

Pass only when all seven cases succeed. In particular:

- if explicit Plugin selection cannot be distinguished from implicit invocation, stop;
- if visible development text precedes activation/gating, stop;
- if Hook `session_id` cannot map to exact child `thread.id`, stop;
- if compact restoration cannot be made idempotent, stop; or
- if Capture accepts recalled/probe text without a frozen qualifying anchor,
  or prompt-event association changes across retry/compact/Fork, record
  `capture_evidence_provenance_unavailable` and stop.

Do not soften a failure into a warning and do not start Gate 2.

- [ ] **Step 6: Record bounded evidence and clear the fixture**

The acceptance record includes exact app/Codex version, plugin version, seven pass/fail rows, receipt IDs/digests, focused test command/result, and the stop decision. Then run:

```bash
ZDECISION_LIVE_ACCEPTANCE=1 .venv/bin/zdecision-agent recall-host-gate clear
```

- [ ] **Step 7: Run the complete suite once**

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected: complete suite passes.

- [ ] **Step 8: Commit the acceptance harness and evidence**

```bash
git add \
  tests/integration/test_recall_host_gate.py \
  docs/superpowers/acceptance/2026-08-06-recall-host-gate.md
git commit -m "test: prove codex recall host gate"
```

## Gate 1 Completion Rule

Gate 1 is complete only after the focused suite, one complete suite, and all seven real Desktop cases pass. Do not run another broad architecture review. If it passes, continue to `2026-08-06-recall-trusted-distribution.md`; if it fails, preserve the evidence, mark the exact host capability blocked, and redesign only that integration boundary.
