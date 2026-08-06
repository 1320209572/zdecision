# Real Session Decision Recall Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Gate 4 by wiring the proven Codex host boundary, trusted signed Decision data, and the selected local retrieval runtime into one Session-opt-in production recall path, then prove every lifecycle, privacy, and real Codex Desktop acceptance case without expanding Registry V1.

**Architecture:** Reuse Gate 1's `RecallHostStore`, trusted Hook bindings, `RecallIntent`, and activation/Turn-gate MCP tools; do not create another Session or gate store. A new `RecallService` receives Gate 3's I/O-free `TrustedRecallBundle + RecallIndexBundle + ModelRuntimeBundle` provider, runs `HybridRetriever`, freezes the shortlist, validates Codex's bounded applicability classification, and atomically commits usage state through one `RecallUsageStore` before returning typed Decision envelopes. Background distribution remains the only Central network path, while Prompt routing, retrieval, ranking, applicability, overrides, restoration, and Task Usage state remain device-local.

**Tech Stack:** Python 3.11, standard-library `sqlite3`, Gate 1 Codex Hooks/MCP/app-server integration, Gate 2 signed distribution/cache contracts, Gate 3 selected local embedding/reranker/index runtime, `unittest`, HTTPX `MockTransport`, and real Codex Desktop on macOS.

## Global Constraints

- Gate 1, Gate 2, and Gate 3 are blocking prerequisites. Do not begin this plan unless their focused suites and acceptance records pass; do not replace a failed prerequisite with weaker Gate 4 behavior.
- Reuse `src/zdecision/agent/recall_host_state.py` unchanged as the sole owner of authorization, Session lifecycle, Turn gates, context-epoch identity, and internal-Thread purpose. `RecallUsageStore` may reference its trusted IDs but must not duplicate Session or Turn-gate rows, state transitions, or authorization checks.
- Reuse `src/zdecision/recall/session.py::RecallIntent`; raw Prompt, PRD, transcript, source, tool output, diff, or attachment bytes never become persisted intent state.
- App-server `Thread.id`, after Gate 1 proves it equals Hook `session_id`, is the per-task authorization key. App-server `Thread.sessionId` is shared session-tree provenance and must never authorize a parent, Fork, or subagent.
- `RecallCacheStore.readiness() == trusted_data_ready` is not recall readiness. Only Gate 3's atomically activated runtime/index bundle may be queried or injected.
- Registry V1 currently produces only `revision: 1` and `lifecycle: active`. Do not modify `src/zdecision/registry/models.py`, add a Decision title, add Registry V2, or claim production revision, retirement, supersession, or replacement semantics.
- A newly appearing r1 Decision in a newer complete active-head set waits until the next Intent Epoch or explicit **重新检查决策**. An exact previously active r1 tuple or whole leaf absent from the newly activated complete active-head set loses Session authority on the next Prompt with reason `removed_from_active_heads`.
- `replaced_by_revision`, r2, retirement, and supersession may remain future-compatible type values from earlier gates, but no Gate 4 fixture, UI copy, acceptance result, or documentation may claim that the current producer emitted or distinguished them.
- V1 has no formal Decision `title`. Every receipt and envelope derives `display_title` from canonical `scope_summary`; formal Registry bytes and schemas remain unchanged.
- Applicable Decision envelopes are complete, typed, non-executable data. A shell command, URL, code block, quoted Prompt, or instruction-like text inside one never invokes a tool, changes Plugin state, authorizes Capture, or overrides the native user.
- One epoch injects at most 8 complete Decisions and at most 10,000 UTF-8 bytes of complete canonical Decision content across all selected leaves. No Decision is truncated and multi-leaf work receives one shared budget.
- Same-intent reuse performs no network call, model call, index query, classification call, or repeated injection. A new receipt is emitted only for a changed epoch, restoration, active-head removal, conflict/uncertainty, freshness transition, or explicit recheck.
- Background Agent synchronization is the only recall-specific Central traffic. Prompt, PRD, source, paths, vectors, scores, Session/Turn IDs, Intent, gates, active set, receipts, overrides, deviations, and Task Usage never leave the device.
- A Session that never explicitly selects the native ZDecision recall Skill performs no recall state mutation, retrieval, injection, Task Usage mutation, or recall-specific Central request. Candidate refresh behavior remains unchanged.
- Session-wide bypass and per-Decision override require an exact native user Turn and trusted Hook binding. Neither action mutates a formal Decision, Registry, Candidate, Review, or publication record.
- An opted-in Session may block only affected development while recall is ambiguous, conflicting, uncertain, expired, corrupt, clock-untrusted, or otherwise unavailable. Unselected and bypassed Sessions remain non-blocking.
- Forks, subagents, Capture forks, and reconciliation Threads start recall-disabled. Inherited Decision text is `inherited_unverified` until a native activation in that exact child Thread succeeds.
- Use exact-path `git add` commands. Preserve the approved specification edit, earlier Gate plans/evidence, and all unrelated user-owned worktree changes.
- After Gate 4, run one focused suite, one complete suite, and one bounded real Desktop acceptance. Record non-blocking improvements and stop; do not begin another broad audit, blind Skill test, Registry expansion, or hardening loop.

---

## Stable Prerequisite Interfaces

Gate 4 consumes these interfaces exactly and must not duplicate them:

| Gate | File | Interface consumed by Gate 4 |
|---|---|---|
| Gate 1 | `src/zdecision/recall/session.py` | `RecallIntent`, `TurnGateResult`, `GateDisposition` |
| Gate 1 | `src/zdecision/agent/recall_host_state.py` | `RecallHostStore`, `RecallSession`, `TurnGate`, context restoration and internal-Thread bindings |
| Gate 1 | `src/zdecision/agent/recall_mcp.py` | `RecallMcpTools.activate_zdecision_recall()`, `RecallMcpTools.gate_zdecision_turn()`, and the `RecallGateProvider` seam |
| Gate 1 | `src/zdecision/app_server/models.py` | `ThreadIdentity`, `ActiveTurnEvidence` |
| Gate 2 | `src/zdecision/agent/recall_cache.py` | `TrustedRecallBundle`, `TrustedDecisionRevision`, `RecallReadiness`, `RecallCacheStore.trusted_bundle()`, `RecallCacheStore.trusted_bundle_for_generation()` |
| Gate 2 | `src/zdecision/recall/contracts.py` | canonical r1 Decision documents, signed manifest/profile identities, and complete active-head membership |
| Gate 3 | `src/zdecision/recall/runtime.py` | `EmbeddingRuntime`, `RerankerRuntime`, `ModelRuntimeBundle` |
| Gate 3 | `src/zdecision/recall/documents.py` | strict local retrieval-document projection derived from canonical r1 bytes |
| Gate 3 | `src/zdecision/recall/index_store.py` | `RecallIndexBundle` |
| Gate 3 | `src/zdecision/recall/retrieval.py` | `HybridRetriever.retrieve(intent, bundle, indexes, runtime) -> HybridRetrievalResult`; result owns the frozen `shortlist`, `candidate_keys_at_20`, and exact generation/manifest/profile binding |
| Gate 3 | `src/zdecision/agent/recall_ready.py` | `RecallReadyProvider.resolve(now)` and `RecallReadyProvider.resolve_generation(generation, now)` return an exact persisted `RecallReadyContext` or fail closed |

The Gate 3 provider used here returns one atomically compatible tuple of `TrustedRecallBundle`, `RecallIndexBundle`, and `ModelRuntimeBundle`. `HybridRetriever` performs no file, cache, network, or model-loading I/O; this makes every Gate 4 ordering/failure test deterministic. Gate 4 consumes only `HybridRetrievalResult.shortlist`, `generation`, `manifest_digest`, `retrieval_profile_digest`, and freshness metadata. It never persists or exposes `candidate_keys_at_20`, vectors, or scores. A pending classification is reopened only with `resolve_generation()` for its frozen generation and must match both frozen digests; Gate 4 never substitutes the current generation silently.

## File Structure

```text
src/zdecision/
  recall/
    application.py                 # typed non-executable envelopes, markers, and receipts
  agent/
    recall_usage_store.py            # epochs, operations, classifications, active items and receipts
    recall_service.py                # production coordinator joining Gates 1-3
    recall_mcp.py                    # existing activation/gate tools plus override/bypass controls
    hooks.py                         # restoration, removal, and trusted control bindings
    mcp_server.py                    # production coordinator/tool composition
    cli.py                           # acceptance-only network observer and fixture reset commands
plugins/zdecision/
  skills/decision-recall/SKILL.md
tests/
  test_recall_application.py
  test_recall_usage_store.py
  test_recall_service.py
  test_recall_controls.py
  test_recall_lifecycle.py
  test_recall_feedback.py
  test_recall_network_privacy.py
  test_recall_docs_contract.py
  integration/
    test_recall_real_session.py
    test_recall_real_network.py
    recall_network_observer.py
    recall_live_fixture.py
docs/superpowers/acceptance/
  2026-08-06-recall-real-session.md
```

---

### Task 1: Define classification submissions, non-executable Decision envelopes, markers, and receipts

**Files:**

- Create: `src/zdecision/recall/application.py`
- Create: `tests/test_recall_application.py`
- Test: `tests/test_recall_contracts.py`
- Test: `tests/test_registry.py`

**Interfaces:**

- Consumes: Gate 3's `ShortlistedDecision`, Gate 2's `TrustedDecisionRevision.canonical_json`, `canonical_json_bytes()`, and Registry V1 `DecisionRevision.from_dict()`.
- Produces:

```python
ApplicationCategory = Literal["applicable", "conflicting", "uncertain"]
ContextMarkerReason = Literal[
    "intent_replaced",
    "removed_from_active_heads",
    "expired",
    "invalid",
    "clock_untrusted",
    "overridden_for_epoch",
    "session_bypassed",
    "inherited_unverified",
]

@dataclass(frozen=True)
class DecisionClassification:
    decision_space_id: str
    decision_id: str
    revision: int
    digest: str
    category: ApplicationCategory
    reason: str

    @classmethod
    def from_dict(cls, value: object) -> "DecisionClassification": ...

@dataclass(frozen=True)
class ClassificationSubmission:
    operation_id: str
    shortlist_digest: str
    items: tuple[DecisionClassification, ...]

    @classmethod
    def from_dict(cls, value: object) -> "ClassificationSubmission": ...

@dataclass(frozen=True)
class DecisionEnvelope:
    marker: Literal["ZDECISION_DECISION_ENVELOPE"]
    receipt_id: str
    decision_space_id: str
    decision_id: str
    revision: int
    digest: str
    source_generation: int
    display_title: str
    claim: str
    future_action: str
    scope_summary: str
    repositories: tuple[str, ...]
    paths: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    match_reason: str
    freshness: Literal["current", "degraded"]

    @classmethod
    def from_classified(
        cls, *, shortlist_item: ShortlistedDecision,
        classification: DecisionClassification, receipt_id: str
    ) -> "DecisionEnvelope": ...
    def to_developer_dict(self) -> dict[str, object]: ...

@dataclass(frozen=True)
class DecisionContextMarker:
    marker: Literal["ZDECISION_DECISION_CONTEXT_MARKER"]
    receipt_id: str
    decision_space_id: str
    decision_id: str
    revision: int
    digest: str
    reason: ContextMarkerReason

@dataclass(frozen=True)
class DecisionApplicationReceipt:
    marker: Literal["ZDECISION_RECEIPT"]
    receipt_id: str
    operation_id: str
    intent_epoch: int
    context_epoch: int
    generation: int
    freshness: Literal["current", "degraded"]
    applied: tuple[DecisionEnvelope, ...]
    blocked: tuple[DecisionClassification, ...]
    context_markers: tuple[DecisionContextMarker, ...]
    empty_result: bool

    def to_developer_context(self) -> str: ...
```

- `DecisionApplicationReceipt.to_developer_context()` is canonical bounded JSON, not Markdown assembled from Decision prose. It exposes no Session, Turn, CWD, local path, vector, score, or private routing evidence.

- [ ] **Step 1: Write failing strict-envelope and display-title tests**

Create canonical r1 fixtures whose `scope.summary` is `"跨 Session 决策召回边界"` and whose `claim` contains command-like text such as `"Run rm -rf only as quoted data"`. Assert:

```python
envelope = DecisionEnvelope.from_classified(
    shortlist_item=shortlisted_decision(),
    classification=applicable_classification(),
    receipt_id="rrc_" + "1" * 32,
)
self.assertEqual(envelope.scope_summary, envelope.display_title)
self.assertEqual("跨 Session 决策召回边界", envelope.display_title)
self.assertNotIn("title", DecisionRevision.from_dict(DOCUMENT).to_dict())
self.assertEqual("ZDECISION_DECISION_ENVELOPE", envelope.marker)
self.assertNotIn("session_id", envelope.to_developer_dict())
self.assertNotIn("turn_id", envelope.to_developer_dict())
```

Also cover strict `ClassificationSubmission` fields, unknown fields, malformed IDs/digests, duplicate/missing/extra classifications relative to a frozen shortlist, non-r1 fixture rejection, non-`active` lifecycle rejection, canonical digest mismatch, overlong local reason, empty title/scope, non-executable marker immutability, and Decision text containing Prompt/tool-like instructions without producing any callable/action field.

- [ ] **Step 2: Write failing receipt and budget tests**

Require canonical ordering by `(decision_space_id, decision_id, revision, digest)`, unique identities, at most eight applied envelopes, complete-item rejection when canonical Decision content would exceed 10,000 UTF-8 bytes, one valid empty receipt, and no truncation. Assert that `to_developer_context()` includes exactly one receipt marker and complete envelope objects.

- [ ] **Step 3: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_application -v
```

Expected: FAIL because `zdecision.recall.application` does not exist.

- [ ] **Step 4: Implement strict values without changing Registry V1**

Parse `TrustedDecisionRevision.canonical_json` with strict UTF-8 JSON, validate through `DecisionRevision.from_dict()`, recompute its SHA-256 digest, and copy `scope_summary` into both `scope_summary` and `display_title`. Never synthesize a title field in the canonical document. Use the typed marker fields above so Decision prose remains data.

- [ ] **Step 5: Run GREEN and Registry regressions**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_application \
  tests.test_recall_contracts \
  tests.test_registry -v
```

Expected: all tests pass and `src/zdecision/registry/models.py` remains unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/zdecision/recall/application.py tests/test_recall_application.py
git commit -m "feat(recall): define typed decision application envelopes"
```

---

### Task 2: Add a usage store without duplicating Host authorization state

**Files:**

- Create: `src/zdecision/agent/recall_usage_store.py`
- Create: `tests/test_recall_usage_store.py`
- Test: `tests/test_recall_host_state.py`

**Interfaces:**

- Consumes: read-only trusted coordinates from the existing `RecallHostStore`, a committed Gate 1 `TurnGate`, `RecallIntent`, and Task 1 receipt/envelope/marker values.
- Produces one separate usage-state owner. It stores no Session activation/state row. Epoch rows are staged against the exact service-owned `TurnGate`, and become authoritative only when the injected `RecallHostStore.require_committed_gate(session_id, turn_id)` returns a gate whose `gate_id` and `result_digest` match that operation:

```python
@dataclass(frozen=True)
class ActiveInjectedDecision:
    session_id: str
    intent_epoch: int
    decision_space_id: str
    decision_id: str
    revision: int
    digest: str
    source_generation: int
    source_expires_at: str
    receipt_id: str
    envelope_json: str

@dataclass(frozen=True)
class RecallEpochCommit:
    operation_id: str
    session_id: str
    turn_id: str
    gate_id: str
    intent_epoch: int
    context_epoch: int
    intent_digest: str
    generation: int
    manifest_digest: str
    retrieval_profile_digest: str
    receipt: DecisionApplicationReceipt

@dataclass(frozen=True)
class ActiveSetTransition:
    unchanged: tuple[ActiveInjectedDecision, ...]
    removed: tuple[DecisionContextMarker, ...]
    newly_available_deferred: tuple[tuple[str, str, int, str], ...]
    freshness_changed: bool

@dataclass(frozen=True)
class DecisionOverride:
    override_id: str
    session_id: str
    intent_epoch: int
    decision_space_id: str
    decision_id: str
    revision: int
    digest: str
    native_resolution_turn_id: str
    created_at: str

@dataclass(frozen=True)
class DecisionDeviationEvent:
    deviation_id: str
    session_id: str
    intent_epoch: int
    decision_space_id: str
    decision_id: str
    revision: int
    digest: str
    native_resolution_turn_id: str
    resolution: Literal["override_current_requirement", "scope_not_applicable"]
    created_at: str

class RecallUsageStore:
    @classmethod
    def open(
        cls, path: Path, *, host_store: RecallHostStore
    ) -> "RecallUsageStore": ...
    def commit_epoch(
        self, *, gate_id: str, operation_id: str, intent: RecallIntent,
        generation: int, manifest_digest: str,
        retrieval_profile_digest: str,
        receipt: DecisionApplicationReceipt,
        committed_at: datetime,
    ) -> RecallEpochCommit: ...
    def epoch_commit(self, operation_id: str) -> RecallEpochCommit | None: ...
    def active_items(self, session_id: str) -> tuple[ActiveInjectedDecision, ...]: ...
    def transition_active_set(
        self, *, session_id: str, bundle: TrustedRecallBundle,
        freshness: Literal["current", "degraded"], now: datetime,
    ) -> ActiveSetTransition: ...
    def invalidate_active_set(
        self, *, session_id: str,
        reason: Literal["expired", "invalid", "clock_untrusted"],
        now: datetime,
    ) -> ActiveSetTransition: ...
    def apply_override(
        self, *, gate_id: str, override_id: str,
        decision_space_id: str, decision_id: str, revision: int, digest: str,
        native_resolution_turn_id: str, resolution: str, now: datetime,
    ) -> tuple[DecisionOverride, DecisionDeviationEvent, DecisionContextMarker]: ...
    def bypass_session(
        self, *, gate_id: str, native_resolution_turn_id: str, now: datetime,
    ) -> tuple[DecisionContextMarker, ...]: ...
    def restoration_receipt(
        self, *, session_id: str, context_epoch: int,
    ) -> DecisionApplicationReceipt | None: ...
```

- [ ] **Step 1: Write failing atomic epoch tests**

Prove that `commit_epoch()` requires the exact service-owned pending Gate and writes the epoch result, active items, blocked items, and receipt in one `BEGIN IMMEDIATE` usage transaction. The service then commits Gate 1's Turn gate with the receipt/result digest; usage reads become authoritative only through that matching Host commit. A crash before the Host commit leaves the Turn blocked and replay completes the same operation; a crash after it returns byte-identical receipt state. An injected usage failure changes no usage row and leaves the Host gate pending. Conflicting operation, cross-Session gate, cross-Turn gate, stale context epoch, stale active generation, and changed intent digest fail closed.

- [ ] **Step 2: Write failing V1 transition tests**

Use only r1 active fixtures:

1. Exact tuple still present in generation 2: update source generation/lease locally without reinjection.
2. New r1 tuple appears in generation 2 during the same Intent Epoch: report it in `newly_available_deferred` and do not add it to `active_items`.
3. Previously active r1 tuple is absent from generation 2's complete active-head set: remove it immediately with `removed_from_active_heads`, even if its old lease has time remaining.
4. Whole selected leaf is absent: remove every pinned item in that leaf.
5. Expired, invalid, or clock-untrusted runtime: remove authority with the exact freshness marker and block affected work.

Assert no test constructs revision 2, `retired`, `superseded`, or `replacement_revision` data.

- [ ] **Step 3: Write failing override, bypass, and privacy tests**

Prove one override is bound to `(session_id, intent_epoch, decision_space_id, decision_id, revision, digest)`, is idempotent for one native resolution Turn, and expires on epoch change, r1 removal, or SessionEnd. `RecallUsageStore.bypass_session()` clears usage state and creates one marker per previously active item; the existing `RecallHostStore` performs the authoritative transition to `bypassed` in the same service operation and prevents later Turn-gate creation until explicit reactivation. Search SQLite values and database bytes for Prompt/PRD sentinels and assert they are absent.

- [ ] **Step 4: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_usage_store -v
```

Expected: FAIL because `RecallUsageStore` does not exist.

- [ ] **Step 5: Implement focused usage tables beside the Host store**

Add only these tables to the store's existing SQLite initialization:

```sql
recall_epochs
recall_operations
recall_classifications
recall_active_items
recall_application_receipts
recall_decision_overrides
recall_deviation_events
recall_restorations
```

Use unique constraints for operation IDs, receipt IDs, trusted Host gate coordinates, active identities, override identity, and one restoration per `(session_id, context_epoch)`. Because `RecallHostStore` owns a separate SQLite connection and lifecycle, never pretend the two stores share a SQL transaction and never duplicate its Session/Turn-gate rows merely to add cross-database foreign keys. Bind staged usage to the exact pending `TurnGate`; after Gate 1 commits its receipt/result digest, validate every authoritative read through `require_committed_gate()`. Do not alter Candidate `session_leases`, Event Ledger rows, Capture state, or Gate 2/3 cache/index tables.

- [ ] **Step 6: Run GREEN and Gate 1 store regressions**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_usage_store \
  tests.test_recall_host_state \
  tests.test_control_binding_hook \
  tests.test_event_ledger -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/zdecision/agent/recall_usage_store.py tests/test_recall_usage_store.py
git commit -m "feat(recall): persist active decisions and local resolutions"
```

---

### Task 3: Join the trusted Host gate, ready runtime, retrieval, and classification in one service

**Files:**

- Create: `src/zdecision/agent/recall_service.py`
- Create: `tests/test_recall_service.py`
- Test: `tests/test_recall_usage_store.py`
- Test: `tests/test_hybrid_retrieval.py`
- Test: `tests/test_recall_ready.py`

**Interfaces:**

- Consumes:

```python
class RecallReadyProvider(Protocol):
    def resolve(self, now: datetime) -> RecallReadyContext | None: ...
    def resolve_generation(
        self, generation: int, now: datetime
    ) -> RecallReadyContext | None: ...

@dataclass(frozen=True)
class RecallReadyContext:
    bundle: TrustedRecallBundle
    indexes: RecallIndexBundle
    runtime: ModelRuntimeBundle

class HybridRetriever:
    def retrieve(
        self,
        intent: RecallIntent,
        bundle: TrustedRecallBundle,
        indexes: RecallIndexBundle,
        runtime: ModelRuntimeBundle,
    ) -> HybridRetrievalResult: ...
```

`RecallReadyProvider` and `RecallReadyContext` are Gate 3 interfaces. `resolve()` performs the ready-store reads and runtime binding before entering `RecallService`; `HybridRetriever.retrieve()` performs no I/O and returns a result frozen to the supplied objects.

- Produces:

```python
RecallServiceState = Literal[
    "awaiting_classification",
    "active",
    "reused",
    "clarify_product",
    "blocked",
    "bypassed",
]

@dataclass(frozen=True)
class RecallServiceResult:
    state: RecallServiceState
    disposition: GateDisposition
    operation_id: str | None
    shortlist_digest: str | None
    shortlist: tuple[ShortlistedDecision, ...]
    receipt: DecisionApplicationReceipt | None
    code: str | None
    question: str | None

    def to_tool_output(self) -> dict[str, object]: ...

class RecallService:
    def __init__(
        self,
        *,
        host_store: RecallHostStore,
        usage_store: RecallUsageStore,
        ready_provider: RecallReadyProvider,
        retriever: HybridRetriever,
        clock: Callable[[], datetime],
        operation_id_factory: Callable[[], str],
        receipt_id_factory: Callable[[], str],
    ) -> None: ...

    def activate(
        self, *, activation_binding_id: str, intent_value: object
    ) -> RecallServiceResult: ...
    def gate_turn(
        self, *, turn_gate_id: str, intent_value: object,
        force_recheck: bool = False,
    ) -> RecallServiceResult: ...
    def apply(
        self, *, turn_gate_id: str, operation_id: str,
        submission_value: object,
    ) -> RecallServiceResult: ...
    def resolve(
        self, *, turn_gate_id: str, receipt_id: str,
        decision_space_id: str, decision_id: str, revision: int,
        digest: str,
        resolution: Literal[
            "follow_decision", "override_current_requirement",
            "scope_not_applicable",
        ],
    ) -> RecallServiceResult: ...
    def bypass(self, *, turn_gate_id: str) -> RecallServiceResult: ...
```

- [ ] **Step 1: Write failing four-argument retrieval and no-I/O tests**

Use a fake ready provider that returns one object containing exact `bundle`, `indexes`, and `runtime` instances and a fake retriever that records object identity:

```python
result = service.activate(
    activation_binding_id=ACTIVATION_BINDING,
    intent_value=intent().to_dict(),
)
self.assertIs(fake_retriever.calls[0].bundle, ready_context.bundle)
self.assertIs(fake_retriever.calls[0].indexes, ready_context.indexes)
self.assertIs(fake_retriever.calls[0].runtime, ready_context.runtime)
self.assertEqual("awaiting_classification", result.state)
self.assertEqual(retrieval.shortlist, result.shortlist)
```

Patch `CentralClient._request`, `socket.socket`, and Gate 2 synchronizer entry points to raise if called. Assert the service never reads or persists `candidate_keys_at_20`, never serializes scores/vectors, and persists only the shortlist, its canonical digest, `generation`, `manifest_digest`, and `retrieval_profile_digest`.

- [ ] **Step 2: Write failing activation, ambiguity, multi-leaf, and empty-result tests**

Cover:

- valid first activation transitions the existing Host Session from `activating` to an operation awaiting classification while keeping the Turn gate uncommitted;
- absent ready context maps exact local readiness states to `preparing`, `expired`, `invalid`, `rollback_detected`, or `clock_untrusted`, never to a false empty result;
- malformed/zero-target intent and an ambiguous leaf result return one `clarify_product` question without retrieval;
- one explicit two-leaf `RecallIntent` passes both leaves to Gate 3 and retains its already-proven shared eight-item/10,000-byte budget; and
- a valid empty shortlist commits one empty epoch/receipt and returns `active` without calling `apply()`.

- [ ] **Step 3: Write failing classification/apply and response-loss tests**

Require `apply()` to accept exactly one classification per frozen shortlist identity and reject missing, extra, duplicate, altered-digest, altered-revision, altered-leaf, or cross-operation entries. `applicable` items become complete envelopes; `conflicting` and `uncertain` items become blocked entries and keep only affected work blocked. The usage transaction atomically stages the epoch, active set, classifications, and receipt; the service then commits the matching Host Turn gate/result digest before returning `RecallServiceResult`. A staged epoch has no authority while that Host gate remains pending.

Simulate response loss after commit, reopen both stores, replay the same `operation_id` and submission, and assert byte-identical tool output with no retrieval, classification mutation, second receipt, or second injection. A conflicting replay fails closed.

- [ ] **Step 4: Write failing same-intent, intent-change, and explicit-recheck tests**

For an ordinary `继续`, tests, or same-feature refinement, assert `gate_turn()` returns `reused`, commits the exact Turn gate below 50 ms in the focused benchmark, and performs only the provider's bounded local ready/membership resolution: no retriever, index query, embedding, reranker, classifier, network call, or reinjection. For changed product/feature/path/constraint/exclusion, assert it creates a new operation and Intent Epoch; old-feature items receive `intent_replaced` markers before new applicable items become active. `force_recheck=True` always creates a new operation even when the normalized intent digest matches.

- [ ] **Step 5: Write failing generation-transition tests using only r1**

Before same-intent reuse, resolve the current ready context locally and call `RecallUsageStore.transition_active_set()`:

- exact old r1 tuple still present: rebind generation/freshness without reinjection;
- newly appearing r1 tuple: defer it and still reuse the existing set;
- missing old r1 tuple: commit `removed_from_active_heads` marker before gate completion;
- active set expiry/corruption/clock rollback: invalidate authority and return `blocked`; and
- changed current ready generation between retrieval and uncommitted `apply()`: reopen the frozen generation with `resolve_generation()`, require exact manifest/profile digest equality, and revalidate every frozen r1 tuple against the current complete active set. Retained tuples may apply; newly appearing r1 tuples remain deferred; a missing tuple or unavailable/mismatched frozen runtime fails closed as `stale_generation` and requires explicit recheck/new epoch.

No branch may construct or report r2, retirement, supersession, or replacement.

- [ ] **Step 6: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_service -v
```

Expected: FAIL because `RecallService` does not exist.

- [ ] **Step 7: Implement the minimal coordinator**

Call `RecallReadyProvider.resolve(now)` once per retrieval operation, pass its three members unchanged to the four-argument retriever, freeze only the bounded shortlist, and persist the result's `generation`, `manifest_digest`, and `retrieval_profile_digest` before returning it to Codex. On `apply()`, call `resolve_generation(frozen_generation, now)`, require both frozen digests to match, and separately use the current complete active set only to retain or remove frozen tuples. Never substitute a newer generation or admit a newly appearing r1 in the same epoch. An exact committed replay reads its stored receipt without requiring the old runtime.

- [ ] **Step 8: Run GREEN and Gate 3 regressions**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_service \
  tests.test_recall_usage_store \
  tests.test_hybrid_retrieval \
  tests.test_recall_ready -v
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add src/zdecision/agent/recall_service.py tests/test_recall_service.py
git commit -m "feat(recall): integrate local retrieval with session gates"
```

---

### Task 4: Expose apply, resolution, and bypass through trusted MCP calls

**Files:**

- Modify: `src/zdecision/agent/recall_mcp.py`
- Modify: `src/zdecision/agent/hooks.py`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `plugins/zdecision/hooks/hooks.json`
- Create: `tests/test_recall_controls.py`
- Modify: `tests/test_mcp_recall_host_gate.py`
- Modify: `tests/test_recall_hook_gate.py`
- Modify: `tests/test_plugin_contract.py`

**Interfaces:**

```python
APPLY_RECALL_TOOL = "mcp__zdecision_local__apply_zdecision_recall"
RESOLVE_RECALL_TOOL = "mcp__zdecision_local__resolve_zdecision_recall"
BYPASS_RECALL_TOOL = "mcp__zdecision_local__bypass_zdecision_recall"

class RecallMcpTools:
    def activate_zdecision_recall(
        self, *, activation_binding_id: str, intent: object
    ) -> dict[str, object]: ...
    def gate_zdecision_turn(
        self, *, turn_gate_id: str, intent: object,
        force_recheck: bool = False,
    ) -> dict[str, object]: ...
    def apply_zdecision_recall(
        self, *, turn_gate_id: str, operation_id: str,
        submission: object,
    ) -> dict[str, object]: ...
    def resolve_zdecision_recall(
        self, *, turn_gate_id: str, receipt_id: str,
        decision_space_id: str, decision_id: str, revision: int,
        digest: str, resolution: str,
    ) -> dict[str, object]: ...
    def bypass_zdecision_recall(
        self, *, turn_gate_id: str,
    ) -> dict[str, object]: ...
```

- [ ] **Step 1: Write failing MCP schema and visibility tests**

Require all five recall tools to be model-visible, idempotent, non-destructive, non-open-world tools with no MCP App UI. Verify exact JSON input/output shapes and bounded errors. Neither model input nor tool output may contain or accept Session ID, Turn ID, CWD, local absolute path, Central URL/token, vector, score, `candidate_keys_at_20`, or other retrieval internals.

- [ ] **Step 2: Write failing trusted-binding tests**

Extend the Gate 1 `PreToolUse` dispatcher so apply/resolve/bypass receive the exact current `turn_gate_id` through `updatedInput`; discard any model-authored gate/session/turn/CWD field. Preserve the model-visible `operation_id`, receipt/Decision identity, classification, and resolution only after strict size/type validation. Deny wrong/superseded/ended Turns, disabled/bypassed Sessions, `agent_id`, internal Capture/reconciliation Threads, unknown operations, and cross-Session receipt IDs.

The broad mutation backstop remains pending until `apply`, a valid resolution, an allowed empty/clarification result, or bypass commits the current gate. Calling activation or retrieval alone must not unlock Bash, `apply_patch`, Agent, or another MCP mutation.

- [ ] **Step 3: Write failing conflict and resolution tests**

Prove:

- `follow_decision` supplies the complete Decision envelope and creates no deviation event;
- `override_current_requirement` and `scope_not_applicable` create one epoch-bound override, one typed marker, and one local `DecisionDeviationEvent` with identities/timestamps only;
- Decision-versus-Decision conflict cannot choose a winner through this tool and remains blocked until native scope clarification changes the intent;
- uncertainty permits only a native clarification or explicit per-Decision `scope_not_applicable` resolution;
- wrong Decision/digest/receipt/epoch replay fails closed; and
- bypass clears every active item, returns one `session_bypassed` marker per item, commits the gate, and causes later Prompt hooks to create no gate until fresh native activation.

- [ ] **Step 4: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_controls -v
```

Expected: FAIL because production apply/resolve/bypass tools are absent.

- [ ] **Step 5: Implement composition without enlarging Candidate tools**

Keep `RecallMcpTools` composed beside `LocalMcpTools`. Open `RecallHostStore`, `RecallUsageStore`, Gate 2 cache, and Gate 3 ready provider independently in `run_mcp()`; build one `RecallService` and close every owned resource in reverse order. A Central configuration failure may disable background synchronization but must not make a valid local ready/LKG bundle disappear.

- [ ] **Step 6: Run GREEN and all Hook/MCP regressions**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_controls \
  tests.test_mcp_recall_host_gate \
  tests.test_recall_hook_gate \
  tests.test_mcp_inline_refresh \
  tests.test_control_binding_hook \
  tests.test_plugin_contract -v
```

Expected: all tests pass and the Candidate refresh card/tool schemas are unchanged.

- [ ] **Step 7: Commit**

```bash
git add \
  src/zdecision/agent/recall_mcp.py \
  src/zdecision/agent/hooks.py \
  src/zdecision/agent/mcp_server.py \
  plugins/zdecision/hooks/hooks.json \
  tests/test_recall_controls.py \
  tests/test_mcp_recall_host_gate.py \
  tests/test_recall_hook_gate.py \
  tests/test_plugin_contract.py
git commit -m "feat(recall): expose trusted application controls"
```

---

### Task 5: Revalidate, restore, resume, and isolate real Decision context

**Files:**

- Modify: `src/zdecision/agent/recall_service.py`
- Modify: `src/zdecision/agent/recall_usage_store.py`
- Modify: `src/zdecision/agent/hooks.py`
- Create: `tests/test_recall_lifecycle.py`
- Modify: `tests/test_recall_hook_gate.py`
- Modify: `tests/test_app_server_gateway.py`
- Modify: `tests/test_recall_capture_isolation.py`

**Interfaces:**

```python
class RecallService:
    def restore_context(
        self, *, session_id: str, context_epoch: int,
        lifecycle_key: str,
    ) -> DecisionApplicationReceipt | None: ...
    def end_session(
        self, *, session_id: str, ended_at: datetime,
    ) -> None: ...
    def revalidate_resume(
        self, *, session_id: str,
    ) -> RecallServiceResult: ...
    def inherited_context_markers(
        self, *, child: ThreadIdentity,
        inherited_receipt_ids: tuple[str, ...],
    ) -> tuple[DecisionContextMarker, ...]: ...
```

- [ ] **Step 1: Write failing compact/clear restoration tests**

For a committed active set, execute `继续`, then `SessionStart(source=compact)` and `source=clear`. Require one `recall_restorations` row per trusted lifecycle key/context epoch and one complete receipt containing only still-authoritative envelopes. Exact replay returns byte-identical developer context without another epoch or injection; the next Prompt returns no repeated receipt. Exercise the full eight-item/10,000-byte boundary and assert Hook `additionalContextLimit=0` delivers complete canonical envelope bytes rather than a spill-file preview.

Before restoration, revalidate exact r1 membership and freshness locally. Missing r1 items return `removed_from_active_heads` markers; expired/invalid/clock-untrusted state returns only invalidation markers and blocked feedback, never stale Decision envelopes. Restoration performs no retrieval, model call, app-server transcript read, or network call.

- [ ] **Step 2: Write failing SessionEnd/resume tests**

`SessionEnd` moves the existing Host Session to `dormant`, expires per-Decision overrides, and preserves active item/receipt data for same-Thread resume. `SessionStart(source=resume)` does not increment Intent or context epoch; it marks the Host Session `revalidating`. The next bound gate calls `revalidate_resume()` before any answer, rebinds exact surviving r1 items to the valid ready generation/LKG, or blocks with bounded freshness state.

- [ ] **Step 3: Write failing Fork and subagent isolation tests**

Use Gate 1's proven `ThreadIdentity` relation. A child with `forked_from_id=parent.thread_id` starts with no Host activation or usage state. If parent `hookPrompt` Decision envelopes/receipts are inherited, return developer-level `inherited_unverified` markers naming those receipt/Decision identities for the child. Every gate/apply/resolve/restoration path rejects parent receipt IDs in the child. A new native child activation performs fresh routing/retrieval/classification and creates child-owned receipts; app-server `session_tree_id` never grants access.

Repeat with `agent_id`, `purpose="capture"`, and `purpose="reconciliation"`; all remain disabled. Feed real typed Decision envelopes into the existing Capture isolation harness and assert Inventory/Extraction/Reconciliation produce zero Candidates without separate native source confirmation.

- [ ] **Step 4: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_lifecycle -v
```

Expected: FAIL because production restoration/revalidation is not wired.

- [ ] **Step 5: Implement lifecycle methods as local bounded reads**

Use existing Gate 1 compaction keys and Host lifecycle transitions. Store restoration operation/receipt state only in `RecallUsageStore`; do not copy Session lifecycle columns. Parse inherited receipt IDs only from Gate 1's bounded `ActiveTurnEvidence`, never from arbitrary assistant text or transcript files.

- [ ] **Step 6: Run GREEN and isolation regressions**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_lifecycle \
  tests.test_recall_hook_gate \
  tests.test_app_server_gateway \
  tests.test_recall_capture_isolation \
  tests.test_requested_capture \
  tests.test_reconciliation_runner -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add \
  src/zdecision/agent/recall_service.py \
  src/zdecision/agent/recall_usage_store.py \
  src/zdecision/agent/hooks.py \
  tests/test_recall_lifecycle.py \
  tests/test_recall_hook_gate.py \
  tests/test_app_server_gateway.py \
  tests/test_recall_capture_isolation.py
git commit -m "feat(recall): restore and isolate decision context"
```

---

### Task 6: Make the Skill execute the production gate/apply protocol and render bounded feedback

**Files:**

- Modify: `plugins/zdecision/skills/decision-recall/SKILL.md`
- Modify: `plugins/zdecision/skills/decision-recall/agents/openai.yaml`
- Modify: `plugins/zdecision/.codex-plugin/plugin.json`
- Modify: `src/zdecision/recall/application.py`
- Create: `tests/test_recall_feedback.py`
- Modify: `tests/test_recall_skill_contract.py`
- Modify: `tests/test_plugin_contract.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class RecallFeedback:
    headline: str
    expanded_items: tuple[dict[str, object], ...]
    freshness: Literal["current", "degraded", "expired", "invalid"]
    actions: tuple[Literal[
        "clarify", "follow_decision", "override_for_epoch",
        "retry", "recheck", "bypass_session",
    ], ...]

    @classmethod
    def from_result(
        cls, result: RecallServiceResult, *, leaf_display_names: Mapping[str, str]
    ) -> "RecallFeedback": ...
```

- [ ] **Step 1: Write failing feedback-copy and privacy tests**

Require these exact user-visible behaviors:

- first successful application: `已应用「<leaf display name>」<N> 条正式决策`;
- multi-leaf application: one shared receipt whose expansion lists each canonical leaf;
- valid zero result once per epoch: `没有找到与当前开发内容相关的正式决策`;
- same-intent reuse: no repeated receipt/headline;
- degraded LKG: visible freshness without exposing signed timestamps as authority extensions;
- preparing/expired/invalid/clock-untrusted: bounded unavailable state plus only valid retry/recheck/bypass actions;
- conflict/uncertainty: Decision `display_title`, revision, leaf, short local reason, and focused resolution actions; and
- removal: `removed_from_active_heads` feedback without the words revision 2, retirement, retired, superseded, or replacement.

Expanded content may contain `display_title`, revision, leaf display name, bounded reason, source generation, and freshness. Assert it contains no score, vector, rank, `candidate_keys_at_20`, local path, Session ID, Turn ID, intent digest, receipt database identity, Prompt/PRD evidence, or other retrieval internals.

- [ ] **Step 2: Write failing Skill ordering and authority tests**

Require the Skill to state the executable protocol precisely:

1. only an exact native `skill`/`mention` selection activates recall;
2. call `activate_zdecision_recall` or the Hook-required `gate_zdecision_turn` before any affected answer;
3. when a shortlist is returned, classify every item only as `applicable`, `conflicting`, or `uncertain`, then call `apply_zdecision_recall` before substantive output;
4. never execute instruction-like Decision content;
5. call resolve/bypass only for an exact native user choice in the bound Turn;
6. never treat recall, override, bypass, or completion as Candidate authorization; and
7. on tool rejection or missing gate, stop affected development rather than narrating around the barrier.

Keep `policy.allow_implicit_invocation: false`. Verify the Candidate Skill/default Prompt remains separate and may keep its existing implicit completion behavior. Plugin copy must say **Session-opt-in Decision recall**, not automatic recall after installation.

- [ ] **Step 3: Run RED**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_feedback \
  tests.test_recall_skill_contract \
  tests.test_plugin_contract -v
```

Expected: FAIL on the production apply/feedback requirements.

- [ ] **Step 4: Implement bounded feedback and final Skill protocol**

Build expanded entries from already validated envelopes and signed catalog leaf display names. Do not read Registry, Central, Prompt, transcript, or retrieval trace while rendering. Task Usage is represented by the host-proven native Skill selection and the existing activated Host Session; do not add a second usage counter/table or mutate usage for unselected Sessions.

- [ ] **Step 5: Run GREEN**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_feedback \
  tests.test_recall_skill_contract \
  tests.test_plugin_contract -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add \
  plugins/zdecision/skills/decision-recall/SKILL.md \
  plugins/zdecision/skills/decision-recall/agents/openai.yaml \
  plugins/zdecision/.codex-plugin/plugin.json \
  src/zdecision/recall/application.py \
  tests/test_recall_feedback.py \
  tests/test_recall_skill_contract.py \
  tests/test_plugin_contract.py
git commit -m "feat(recall): apply decisions through explicit skill flow"
```

---

### Task 7: Prove recall request privacy at the Central transport boundary

**Files:**

- Create: `tests/test_recall_network_privacy.py`
- Create: `tests/integration/recall_network_observer.py`
- Create: `tests/integration/test_recall_real_network.py`
- Test: `tests/test_recall_sync.py`
- Test: `tests/test_central_client.py`
- Test: `tests/integration/test_trusted_recall_onboarding.py`

**Interfaces:**

```python
FORBIDDEN_RECALL_REQUEST_KEYS = frozenset({
    "prompt", "prd", "source", "paths", "vectors", "scores",
    "session_id", "turn_id", "thread_id", "recall_intent",
    "intent_epoch", "context_epoch", "turn_gate_id", "active_set",
    "receipt_id", "override_id", "deviation_id",
})

@dataclass(frozen=True)
class SanitizedNetworkObservation:
    method: str
    path: str
    body_size: int
    json_keys: tuple[str, ...]
    forbidden_key_seen: bool
    sentinel_seen: bool

class RecallNetworkObserver:
    def observe_request(self, request: httpx.Request) -> None: ...
    def sanitized_report(self) -> tuple[SanitizedNetworkObservation, ...]: ...
```

The observer scans Agent-to-Central requests in memory and immediately discards raw bodies and authorization headers. It records no token, body text, query value, formal Decision response, or user sentinel.

- [ ] **Step 1: Write failing mock-transport privacy tests**

Use unique sentinels in a Prompt, PRD, source snippet, repository-relative path, vector, score, Hook Session/Turn IDs, Intent, gate, receipt, override, and deviation. Exercise activation, changed-intent retrieval, classification, override, same-intent reuse, restoration, and background synchronization through an `httpx.MockTransport` observer. Assert:

```python
self.assertTrue(observations)  # background manifest/snapshot traffic occurred
self.assertFalse(any(item.forbidden_key_seen for item in observations))
self.assertFalse(any(item.sentinel_seen for item in observations))
self.assertEqual(0, recall_service_network_calls)
```

Inspect outbound requests only. Formal Decision `scope.paths` received in signed Central responses is allowed authoritative data and must not be misreported as an uploaded local path.

- [ ] **Step 2: Write failing allowlist and log-safety tests**

Permit only the Gate 2 authenticated manifest/catalog/leaf/profile/artifact request shapes and existing unrelated Candidate endpoints. Recall GETs may carry authenticated organization/content identifiers already defined by Gate 2, but no current-task query. Search observer report files, Agent logs, SQLite databases, and cache metadata bytes for every sentinel and authorization token; all must be absent.

- [ ] **Step 3: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_network_privacy -v
```

Expected: FAIL because the sanitized observer and integrated proof do not exist.

- [ ] **Step 4: Implement the test-only observer and loopback forwarder**

`tests/integration/recall_network_observer.py` may expose a loopback-only forwarding server for live acceptance. It forwards method/path/headers/body to the configured real Central endpoint, but removes hop-by-hop headers and never writes `Authorization` or raw body bytes. Its JSON report contains only `SanitizedNetworkObservation` fields plus total request count. Refuse non-loopback listen or upstream addresses.

- [ ] **Step 5: Run GREEN with Gate 2 network regressions**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_network_privacy \
  tests.test_recall_sync \
  tests.test_central_client \
  tests.integration.test_trusted_recall_onboarding \
  tests.integration.test_recall_real_network -v
```

Expected: all non-live tests pass; the live observer case is skipped unless `ZDECISION_LIVE_ACCEPTANCE=1`.

- [ ] **Step 6: Commit**

```bash
git add \
  tests/test_recall_network_privacy.py \
  tests/integration/recall_network_observer.py \
  tests/integration/test_recall_real_network.py
git commit -m "test(recall): prove central request privacy"
```

---

### Task 8: Align architecture, repository guidance, README, and the installed Skill

**Files:**

- Modify: `docs/architecture.md`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `plugins/zdecision/skills/decision-recall/SKILL.md`
- Create: `tests/test_recall_docs_contract.py`
- Modify: `tests/test_recall_skill_contract.py`
- Modify: `tests/test_plugin_contract.py`

- [ ] **Step 1: Write failing active-document contract tests**

Assert all four active product instructions say **Session-opt-in Decision recall**, require an explicit native ZDecision selection in that Session, preserve explicit Candidate refresh, keep Prompt/query state local, and name complete signed r1 active sets. Assert active text does not say installation automatically recalls Decisions for every Session.

Add exact scope-honesty assertions:

```python
for text in active_documents:
    self.assertIn("Session-opt-in", text)
    self.assertNotIn("automatic Decision recall", text)
    self.assertNotIn("every new Session receives", text)

self.assertIn("scope_summary", architecture)
self.assertIn("display_title", architecture)
self.assertIn("removed_from_active_heads", architecture)
self.assertNotIn("Registry V2 is implemented", architecture)
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_docs_contract -v
```

Expected: FAIL because README/AGENTS/architecture still describe automatic or later-packet recall.

- [ ] **Step 3: Update only active product guidance**

Document:

- explicit first- or later-Turn activation and default-disabled new/Fork tasks;
- local intent/retrieval/classification, per-Turn gate, compact restoration, overrides, bypass, and affected-work blocking;
- signed complete r1 distribution, local ready runtime, LKG/expiry/clock behavior;
- new r1 Decision deferral until a new epoch/recheck and immediate exact-tuple removal;
- `display_title = scope_summary` without formal schema change;
- no Central semantic query or Task/Prompt data; and
- Candidate Capture remaining page/card-click authorized and insulated from recalled envelopes.

Do not rewrite or delete historical superseded plans/specifications; tests scope only active instructions.

- [ ] **Step 4: Run GREEN and Plugin contracts**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_docs_contract \
  tests.test_recall_skill_contract \
  tests.test_plugin_contract -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add \
  docs/architecture.md \
  README.md \
  AGENTS.md \
  plugins/zdecision/skills/decision-recall/SKILL.md \
  tests/test_recall_docs_contract.py \
  tests/test_recall_skill_contract.py \
  tests/test_plugin_contract.py
git commit -m "docs: align session opt-in decision recall"
```

---

### Task 9: Run the complete automated Gate 4 scenario matrix

**Files:**

- Create: `tests/integration/recall_live_fixture.py`
- Create: `tests/integration/test_recall_real_session.py`
- Modify: `tests/integration/test_recall_real_network.py`
- Test: `tests/integration/test_recall_host_gate.py`
- Test: `tests/integration/test_trusted_recall_onboarding.py`
- Test: `tests/test_recall_benchmark.py`
- Test: `tests/test_recall_ready.py`
- Test: `tests/test_recall_production_runtime.py`
- Test: `tests/integration/test_recall_ready_onboarding.py`
- Test: `tests/integration/test_recall_offline_quality.py`

**Fixture contract:**

```python
@dataclass(frozen=True)
class RecallIntegrationFixture:
    root: Path
    development_repository: Path
    central_database: Path
    agent_database: Path
    cache_root: Path
    root_decision_id: str
    new_decision_id: str

    @classmethod
    def create(cls, root: Path) -> "RecallIntegrationFixture": ...
    def activate_generation_one(self) -> TrustedRecallBundle: ...
    def publish_new_r1_generation(self) -> TrustedRecallBundle: ...
    def activate_complete_set_without_root_r1(self) -> TrustedRecallBundle: ...
```

The fixture creates only temporary Git/Central/Agent state and canonical active r1 documents. It uses Gate 2's real signing/projection/distribution path and Gate 3's selected production contract with its deterministic test artifacts; it never seeds cache rows or constructs r2/lifecycle-history records.

- [ ] **Step 1: Write the failing integrated matrix**

Create one test method per Gate 4 acceptance row:

1. no native Skill selection: no Host Session, usage row, retrieval call, receipt, Task Usage, or recall Central request;
2. first-Turn PRD activation: retrieval → classification → apply commits before the first substantive item;
3. later activation: prior native context contributes only to the model-authored bounded `RecallIntent`; no transcript is copied into Agent state;
4. every active Turn: exact pending gate precedes answer/mutation; invalid/cross-Turn replay remains blocked;
5. ambiguous target asks once without retrieval; explicit two-leaf intent uses one shared shortlist budget;
6. same intent reuses silently; changed intent replaces the active set and marks old-only items;
7. conflict, uncertainty, follow, epoch-bound override, and Session bypass follow Task 4 rules;
8. empty `继续` retains the active set and one compact/clear event restores it exactly once;
9. newly published r1 remains deferred in the same epoch, then appears after explicit recheck/new epoch; removed root r1 loses authority on the first Prompt with `removed_from_active_heads`;
10. SessionEnd/resume preserves activation and revalidates before the resumed answer;
11. valid offline LKG is degraded but usable; expiry, corruption, and clock rollback remove authority and block affected work;
12. Fork inherits text only as unverified, rejects parent receipts, and may reactivate only through native child selection;
13. Capture/reconciliation forks remain recall-disabled and envelope-only content produces zero Candidates; and
14. Task 7 observer sees distribution traffic but none of the forbidden request keys or sentinels.

- [ ] **Step 2: Add exact ordering assertions from app-server evidence**

For activation, changed-intent retrieval, and ordinary active Turns, assert the ordered item types satisfy:

```python
self.assertLess(index("hookPrompt"), index("mcpToolCall:gate-or-activate"))
self.assertLess(index("mcpToolCall:gate-or-activate"), index("mcpToolCall:apply"))
self.assertLess(index("mcpToolCall:apply"), index("agentMessage:substantive"))
self.assertLess(index("mcpToolCall:apply"), index("commandExecution-or-fileChange"))
```

For valid empty/reuse/clarification/blocked results where `apply` is not required, require the committing gate tool to precede substantive output. Use Gate 1's bounded `TurnItemEvidence`; do not parse transcript text.

- [ ] **Step 3: Run RED**

```bash
.venv/bin/python -m unittest tests.integration.test_recall_real_session -v
```

Expected: FAIL at the first missing integrated behavior, not because the fixture seeded state.

- [ ] **Step 4: Correct only confirmed integration defects**

Restrict fixes to `recall_service.py`, `recall_usage_store.py`, `recall_mcp.py`, Hooks, application serialization, and the test fixture. A quality miss returns to Gate 3 profile/index/retrieval work; a signature/cache miss returns to Gate 2; a native ordering/identity miss returns to Gate 1. Do not mask a prerequisite failure in Gate 4.

- [ ] **Step 5: Run the focused automated Gate 4 suite**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_application \
  tests.test_recall_usage_store \
  tests.test_recall_service \
  tests.test_recall_controls \
  tests.test_recall_lifecycle \
  tests.test_recall_feedback \
  tests.test_recall_network_privacy \
  tests.test_recall_docs_contract \
  tests.integration.test_recall_real_session \
  tests.integration.test_recall_real_network -v
```

Expected: all automated cases pass; only explicitly real-Desktop methods are skipped.

- [ ] **Step 6: Commit the integrated harness**

```bash
git add \
  tests/integration/recall_live_fixture.py \
  tests/integration/test_recall_real_session.py \
  tests/integration/test_recall_real_network.py
git commit -m "test(recall): cover integrated session lifecycle"
```

---

### Task 10: Prove the bounded real Codex Desktop and network acceptance, then stop

**Files:**

- Create after the run: `docs/superpowers/acceptance/2026-08-06-recall-real-session.md`
- Test: `tests/integration/test_recall_real_session.py`
- Test: `tests/integration/test_recall_real_network.py`
- Use: `tests/integration/recall_live_fixture.py`
- Use: `tests/integration/recall_network_observer.py`

- [ ] **Step 1: Verify all prerequisite evidence before starting Desktop**

Run the focused Gate 4 suite exactly once after all implementation/documentation commits:

```bash
.venv/bin/python -m unittest \
  tests.test_recall_application \
  tests.test_recall_usage_store \
  tests.test_recall_service \
  tests.test_recall_controls \
  tests.test_recall_lifecycle \
  tests.test_recall_feedback \
  tests.test_recall_network_privacy \
  tests.test_recall_docs_contract \
  tests.integration.test_recall_real_session \
  tests.integration.test_recall_real_network -v
```

Expected: PASS, with real-only cases skipped. Confirm `docs/superpowers/acceptance/2026-08-06-recall-host-gate.md` says PASS, Gate 2's focused/full completion evidence passes, and Gate 3's profile-selection record plus focused ready/onboarding/offline-quality evidence all pass with matching digests. A missing or failed prerequisite stops this task.

- [ ] **Step 2: Run the complete repository suite once**

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected: PASS. Do not rerun the complete suite unless a concrete failure is fixed.

- [ ] **Step 3: Prepare an isolated live fixture and observer**

```bash
RECALL_ACCEPTANCE_ROOT=$(mktemp -d /tmp/zdecision-recall-gate4.XXXXXX)
ZDECISION_LIVE_ACCEPTANCE=1 .venv/bin/python -m \
  tests.integration.recall_live_fixture prepare \
  --root "$RECALL_ACCEPTANCE_ROOT"
ZDECISION_LIVE_ACCEPTANCE=1 .venv/bin/python -m \
  tests.integration.recall_live_fixture start \
  --root "$RECALL_ACCEPTANCE_ROOT" \
  --central-port 18765 \
  --observer-port 18766
```

The helper must refuse a non-absolute/non-`/tmp/zdecision-recall-gate4.*` root, occupied ports, a non-loopback address, or an already-running fixture. It creates a disposable registered development Git repository, temporary Registry/Central/Agent databases, owner-readable trust keys, generation-1 active r1 Decisions, the selected Gate 3 ready runtime, and an Agent configuration whose Central URL is the loopback observer. It stores process IDs only beneath the explicit fixture root and never touches the user's normal Agent cache or Registry checkout.

Install/reload the local Plugin only if Gate 1's documented live procedure requires it. Open the fixture development repository in Codex Desktop and record the exact Codex/app/plugin versions.

- [ ] **Step 4: Execute the fourteen bounded acceptance cases**

Use separate native tasks where isolation is part of the assertion. Record only task/operation/receipt digests and pass/fail state; never copy Prompt, PRD, source, Decision prose, transcript, or tool output into the acceptance record.

1. **No selection:** perform an ordinary development Turn without selecting ZDecision; verify no recall Task Usage, tool, Host/usage row, or recall request.
2. **First-Turn PRD:** natively select the recall Skill with a PRD-led development task; inspect ordered app-server evidence and verify apply commits before the answer.
3. **Later activation:** establish at least two ordinary Turns, then select ZDecision; verify activation uses the current bounded intent and precedes continued work.
4. **Every active Turn:** exercise reuse, a denied mutation while pending, a committed gate, then a cross-Turn gate replay denial.
5. **Routing:** run one ambiguous product prompt that asks without retrieval, then one explicit two-leaf task whose single receipt stays within the shared budget.
6. **Intent epochs:** run `继续`, a same-feature test/fix, a path/constraint change, and a product change; verify silent reuse followed by replacement markers and one new receipt.
7. **Applicability controls:** exercise applicable, conflicting, uncertain, follow-Decision, one per-Decision override, and explicit Session bypass; verify only affected work blocks and re-enable performs fresh activation.
8. **Compaction:** active set → empty `继续` → compact and clear; verify one complete restoration per real context epoch and no next-Prompt duplicate.
9. **V1 live changes:** run `publish-new-r1`, wait for a complete ready generation, and prove same-epoch deferral; then recheck/new epoch and apply it. Run `remove-root-r1`, wait for the next complete ready generation, and prove immediate `removed_from_active_heads` on the first Prompt.
10. **Resume:** close normally through SessionEnd, resume the exact task, and verify dormant authorization is revalidated before the first resumed answer without incrementing the Intent Epoch.
11. **Freshness:** stop Central while the signed LKG is valid and observe degraded recall; use the fixture's short signed lease to observe expiry; run its isolated cache-corruption case. Use the automated production-clock test result for backward-clock detection—never change the Mac system clock—and verify Desktop displays the resulting `clock_untrusted` fixture state without old authority.
12. **Fork:** create a user-visible Fork, inspect `Thread.id/forkedFromId/sessionId`, verify parent receipts are inactive, then natively reactivate the child and obtain child-owned receipts.
13. **Capture fork:** click the existing explicit Candidate refresh from a Session containing real Decision envelopes; verify Capture/reconciliation Threads are disabled and envelope-only rules create zero Candidates.
14. **Network:** complete the scenarios through the loopback observer and verify its sanitized report saw expected background distribution requests but no forbidden key or sentinel.

The fixture transition commands are:

```bash
ZDECISION_LIVE_ACCEPTANCE=1 .venv/bin/python -m \
  tests.integration.recall_live_fixture publish-new-r1 \
  --root "$RECALL_ACCEPTANCE_ROOT"
ZDECISION_LIVE_ACCEPTANCE=1 .venv/bin/python -m \
  tests.integration.recall_live_fixture remove-root-r1 \
  --root "$RECALL_ACCEPTANCE_ROOT"
ZDECISION_LIVE_ACCEPTANCE=1 .venv/bin/python -m \
  tests.integration.recall_live_fixture status \
  --root "$RECALL_ACCEPTANCE_ROOT"
```

Both transition commands must use the production verified projection and signed complete-generation publisher against the temporary Registry. They may add or remove active r1 files only in that fixture; they must never construct r2, retirement, supersession, or a synthetic Central invalidation record.

- [ ] **Step 5: Verify and persist only sanitized network evidence**

```bash
ZDECISION_LIVE_ACCEPTANCE=1 .venv/bin/python -m \
  tests.integration.recall_live_fixture network-report \
  --root "$RECALL_ACCEPTANCE_ROOT" \
  --output "$RECALL_ACCEPTANCE_ROOT/sanitized-network-report.json"
ZDECISION_LIVE_ACCEPTANCE=1 .venv/bin/python -m unittest \
  tests.integration.test_recall_real_network -v
```

Expected: PASS. The report contains endpoint paths, methods, sizes, top-level key names, counts, and boolean sentinel/forbidden-key results only. Search it for every live sentinel and native ID before attaching its SHA-256 digest—not its raw path or contents—to the acceptance record.

- [ ] **Step 6: Apply the hard stop rule**

Gate 4 fails if any of these occurs:

- implicit/quoted/delegated content activates recall or unselected work records Task Usage;
- activation/gate/apply follows substantive text or a command/code mutation;
- a gate/operation/receipt crosses a Turn, Session, Fork, subagent, Capture, or reconciliation boundary;
- ambiguity retrieves, multi-leaf work multiplies the budget, or same intent re-runs retrieval;
- conflict/uncertainty does not block affected work or an override escapes its exact epoch/Decision;
- compact/clear duplicates or truncates restoration;
- a new r1 enters the same epoch automatically, or a removed r1 retains authority;
- expired/corrupt/clock-untrusted content remains advisory/active;
- a Capture Candidate treats a recalled envelope as confirmation;
- Central sees any Prompt/PRD/source/path/query/vector/score/native ID/recall-state sentinel; or
- acceptance requires r2, retirement, supersession, Registry V2, a Decision title field, cloud search, or system-clock mutation.

On failure, write a bounded failed acceptance record naming the exact gate/case and stop. Correct only that confirmed boundary in Gate 1, 2, 3, or 4 as owned; do not weaken the criterion or continue to unrelated hardening.

- [ ] **Step 7: Record success and stop the isolated fixture**

The acceptance record contains:

- exact app/Codex/plugin version and commit;
- prerequisite acceptance record digests;
- focused and complete-suite commands with pass counts;
- one row per fourteen cases with bounded receipt/operation/generation digests;
- ready runtime/profile and signed manifest digests;
- sanitized network report digest and `forbidden_key_seen=false`, `sentinel_seen=false`;
- explicit statements that `display_title` came from `scope_summary`, producer data was r1 active only, new r1 was deferred, removed r1 was invalidated locally, and no r2/retire/supersede claim was made; and
- final `PASS` and stop decision.

Then stop processes without deleting evidence:

```bash
ZDECISION_LIVE_ACCEPTANCE=1 .venv/bin/python -m \
  tests.integration.recall_live_fixture stop \
  --root "$RECALL_ACCEPTANCE_ROOT"
```

- [ ] **Step 8: Commit only the bounded acceptance record**

```bash
git add docs/superpowers/acceptance/2026-08-06-recall-real-session.md
git commit -m "test(recall): prove real session decision recall"
```

Do not commit fixture state, keys, caches, network report bodies, model artifacts, Prompts, transcripts, or source repositories.

## Gate 4 Completion Rule

Gate 4 is complete only when the focused suite, one complete suite, all fourteen bounded real-Session rows, and the sanitized network proof pass with the final installed Skill and active documentation. A failure remains a blocking acceptance result. After PASS, record non-blocking improvements and stop; do not start another architecture audit, Skill blind test, Registry revision project, or generalized memory feature.
