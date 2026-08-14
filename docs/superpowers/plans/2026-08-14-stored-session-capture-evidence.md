# Stored Session Capture Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task with review checkpoints.

**Goal:** Let explicit “当前 Session” and “所有有效 Session” Candidate refreshes use bounded, host-verified stored user messages from the selected Codex task, so pre-Hook conversation decisions can become candidates without persisting raw conversation text or weakening existing evidence guarantees.

**Architecture:** Add a V2 Capture evidence path beside the immutable V1 Hook-only path. The App Server Gateway reads one completed stored history boundary and creates an ephemeral fork. A transient catalog maps canonical receipt IDs to exact user-message text only inside the ephemeral Capture attempt. Durable state stores only coordinates, ordinals, digests, source facets, and retry boundaries. Inventory and extraction must select catalog receipts; inherited history remains non-authoritative context. Existing V1 operations resume byte-for-byte under V1, while all newly created explicit refreshes freeze V2.

**Tech Stack:** Python 3.12+, dataclasses, SQLite, Codex App Server JSON-RPC, JSON Schema, stdlib `unittest`, MCP Apps HTML/JavaScript.

**Spec:** `docs/superpowers/specs/2026-08-14-stored-session-capture-evidence-design.md`

## Global Constraints

- Stop after Task 1 if the installed Codex App Server cannot return a complete, stable `thread/read(includeTurns=true)` boundary or cannot create a verified `thread/fork(..., ephemeral=true)` fork. Do not substitute rollout files, transcript scraping, experimental item APIs, a second Desktop broker, or raw-text persistence.
- Preserve V1 operation bytes and V1 resume behavior. Do not rewrite or migrate existing Capture operations, receipts, Candidate provenance, Central summaries, or published Decisions.
- Raw stored message text may exist only in the original stored task response and in memory during the ephemeral Capture attempt. It must not enter SQLite, Central, Registry, Git, logs, status payloads, exceptions, or durable retry data.
- New V2 limits are fixed: at most 100 eligible stored message anchors, at most 32 KiB UTF-8 per eligible message, at most 128 KiB UTF-8 catalog text per source attempt, and the existing 250,000-character App Server prompt ceiling remains authoritative.
- A stored `userMessage` proves only that Codex stored a user-role message. User-facing wording must say “已验证的任务用户消息”, not “human-authored”.
- Exact text-block bytes are authoritative. Do not trim, normalize Unicode, collapse whitespace, reorder blocks, or silently omit an oversized eligible item. An invalid/oversized required boundary fails closed as `historical_evidence_unavailable`.
- Keep Recall isolated: Recall continues to consume only published Decisions and must not read stored Session history.
- The worktree already contains uncommitted V3 extraction-contract edits in `requested_capture.py`, `on_demand.py`, prompt templates, service code, and their tests. Before editing an overlapping file, record its current diff, preserve its behavior, and extend it surgically. Never reset, overwrite, or stage unrelated files.
- Every commit stages only the paths named by its task. The protected untracked acceptance files, presentation artifacts, and `uv.lock` remain untouched.

## File Structure

### New files

- `src/zdecision/app_server/stored_history.py` — transient stored-history parsing, exact message digests, V2 boundary construction, and in-memory evidence catalog rendering. Raw text types intentionally have no serializer.
- `tests/test_stored_history_evidence.py` — closed-shape parsing, byte limits, digest determinism, Hook/stored facet merge, privacy, and retry equality tests.
- `tests/integration/test_stored_history_live_app_server.py` — opt-in real App Server acceptance for complete history and ephemeral forks.

### Modified files

- `src/zdecision/app_server/models.py` — host-response metadata types only; no raw message persistence type.
- `src/zdecision/app_server/gateway.py` — validated full-history read and ephemeral fork methods, retaining V1 persistent fork methods.
- `src/zdecision/capture/provenance.py` — immutable V2 evidence manifest, canonical message anchors, and local Candidate provenance union.
- `src/zdecision/capture/on_demand.py` — operation record version 6 and V1/V2 parser dispatch.
- `src/zdecision/app_server/requested_capture.py` — freeze/reverify V2 evidence, register ephemeral Capture, inject transient catalog, and run Inventory/extraction V6.
- `src/zdecision/capture/inventory.py`, `src/zdecision/capture/service.py`, `src/zdecision/capture/prompts.py`, `src/zdecision/capture/prompt_contracts/extraction-envelope.md`, and `decision-templates/business/*` — V6 receipt selection and semantic-audit contract.
- `src/zdecision/agent/capture_processor.py`, `src/zdecision/sync/contracts.py`, `src/zdecision/sync/client.py`, Central request storage/service modules, `src/zdecision/agent/mcp_server.py`, and `src/zdecision/agent/static/update-candidates-v1.html` — distinct safe outcome propagation and unavailable-source counts.
- `src/zdecision/app_server/reconciliation_runner.py` — accept the local V1/V2 provenance union while emitting the unchanged minimized Central V1 summary.
- `docs/architecture.md` and Candidate-refresh Skill/contract docs — describe the approved V2 path and exact UI wording.

---

## Task 1: Prove the App Server host capabilities before changing Capture semantics

**Files:**

- Create: `tests/integration/test_stored_history_live_app_server.py`
- Modify: `src/zdecision/app_server/models.py`
- Modify: `src/zdecision/app_server/gateway.py`
- Modify: `tests/test_app_server_gateway.py`

- [ ] **Step 1: Add failing unit contracts for complete stored history and ephemeral fork validation**

Add tests that exercise these public methods:

```python
snapshot = gateway.read_stored_history(
    thread_id="thread-1",
    lower_turn_id_exclusive=None,
    upper_turn_id_inclusive="turn-2",
    upper_stop_event_id="evt_" + "1" * 32,
    expected_cwd="/repo",
)
fork_id = gateway.fork_ephemeral_thread(
    thread_id="thread-1",
    last_turn_id="turn-2",
)
```

The tests must reject wrong thread IDs, wrong cwd, missing/duplicated upper Turns, non-completed Turns, reordered or duplicated Turn IDs, missing `items`, an unexpected continuation after the upper Turn, `ephemeral != true`, reused source IDs, wrong `forkedFromId`, and paginated/truncated history responses.

- [ ] **Step 2: Run the RED gateway tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_app_server_gateway -v
```

Expected: FAIL because `read_stored_history` and `fork_ephemeral_thread` do not exist.

- [ ] **Step 3: Implement the smallest validated Gateway surface**

Add metadata-only immutable types in `models.py`:

```python
@dataclass(frozen=True)
class StoredTurnEnvelope:
    turn_id: str
    status: Literal["completed"]
    items: tuple[Mapping[str, object], ...]

@dataclass(frozen=True)
class StoredThreadEnvelope:
    thread_id: str
    cwd: str
    turns: tuple[StoredTurnEnvelope, ...]
```

`StoredThreadEnvelope` is transient and must not define `to_dict`. Implement `read_stored_history` with exactly one `thread/read` request using `includeTurns: true`; reject any response that does not prove the full requested boundary. Implement `fork_ephemeral_thread` using:

```json
{
  "threadId": "<source>",
  "lastTurnId": "<upper-boundary>",
  "ephemeral": true
}
```

Retain `fork_disposable_thread` unchanged for V1 operations.

- [ ] **Step 4: Add the opt-in live acceptance test**

The live test accepts explicit environment inputs for one known stored task and completed upper Turn. It must prove:

1. at least one stored `userMessage` item is visible through `thread/read`;
2. repeated reads return identical ordered Turn/item projections;
3. `thread/fork(..., ephemeral=true)` succeeds and reports the correct source;
4. the ephemeral fork is absent from `thread/list`;
5. no source task or production database is modified.

Run only when `ZDECISION_LIVE_STORED_HISTORY=1`; otherwise skip with an explicit reason.

- [ ] **Step 5: Run automated and real capability verification**

Run:

```bash
.venv/bin/python -m unittest tests.test_app_server_gateway tests.integration.test_stored_history_live_app_server -v
ZDECISION_LIVE_STORED_HISTORY=1 .venv/bin/python -m unittest tests.integration.test_stored_history_live_app_server -v
```

Expected: unit suite GREEN and real test GREEN. If the real test cannot prove all five properties, stop this plan and report `historical_evidence_unavailable` as a host limitation.

- [ ] **Step 6: Commit the capability gate**

```bash
git add src/zdecision/app_server/models.py src/zdecision/app_server/gateway.py tests/test_app_server_gateway.py tests/integration/test_stored_history_live_app_server.py
git commit -m "test: prove stored Session host capabilities"
```

---

## Task 2: Define immutable V2 stored-message evidence without durable raw text

**Files:**

- Create: `src/zdecision/app_server/stored_history.py`
- Create: `tests/test_stored_history_evidence.py`
- Modify: `src/zdecision/capture/provenance.py`
- Modify: `tests/test_capture_provenance.py`

- [ ] **Step 1: Write RED tests for exact text parsing and canonical receipts**

Cover only `userMessage` items whose `content` is a non-empty list of text blocks. Assert that tool items, agent messages, reasoning, images, empty content, mixed content, malformed IDs, repeated item IDs, and oversize messages fail or remain nonqualifying according to the spec. Assert that whitespace and Unicode differences produce different digests.

Use these exact public types:

```python
@dataclass(frozen=True)
class StoredHistoryBoundary:
    version: Literal[1]
    repository_id: str
    source_thread_id: str
    lower_turn_id_exclusive: str | None
    upper_turn_id_inclusive: str
    upper_stop_event_id: str
    source_cwd_binding: str
    ordered_turn_digest: str

@dataclass(frozen=True)
class StoredUserMessageRecord:
    turn_id: str
    item_id: str
    turn_ordinal: int
    message_ordinal: int
    text_blocks: tuple[str, ...]
    text_block_digest: str
```

Neither type containing raw `text_blocks` nor the transient catalog may expose `to_dict`/`from_dict`.

- [ ] **Step 2: Run the RED evidence tests**

```bash
.venv/bin/python -m unittest tests.test_stored_history_evidence tests.test_capture_provenance -v
```

Expected: FAIL on missing V2 types and parser.

- [ ] **Step 3: Implement transient parsing and bounded catalog rendering**

In `stored_history.py`, implement:

```python
MAX_STORED_MESSAGE_ANCHORS = 100
MAX_STORED_MESSAGE_BYTES = 32 * 1024
MAX_STORED_SOURCE_TEXT_BYTES = 128 * 1024

def parse_stored_history(...) -> StoredHistorySnapshot: ...
def build_transient_catalog(...) -> TransientEvidenceCatalog: ...
```

The exact digest input is canonical JSON of the ordered text-block array. The rendered catalog contains a receipt ID, ordinal, and exact text only; it is built after durable boundary verification and discarded after the attempt.

- [ ] **Step 4: Add durable closed V2 provenance types**

Add:

```python
EvidenceSourceFacet = Literal["hook_observed", "stored_history"]

@dataclass(frozen=True)
class CanonicalMessageAnchor:
    receipt_id: str
    source_kind: Literal["stored_user_message_anchor"]
    turn_id: str
    item_id: str
    turn_ordinal: int
    message_ordinal: int
    text_block_digest: str
    source_facets: tuple[EvidenceSourceFacet, ...]
    recall_lineage_digest: str | None

@dataclass(frozen=True)
class CaptureEvidenceManifestV2:
    version: Literal[2]
    source_session_id: str
    lower_turn_id_exclusive: str | None
    upper_turn_id_inclusive: str
    upper_stop_event_id: str
    anchors: tuple[CanonicalMessageAnchor, ...]
    ordered_turn_digest: str
    manifest_digest: str
```

Hook and stored observations of the same exact turn/item/digest merge into one receipt with two sorted facets. Any coordinate or digest disagreement fails closed. Keep all existing V1 classes and byte encodings unchanged.

- [ ] **Step 5: Verify privacy and compatibility**

Tests must recursively serialize every durable V2 object and assert that none contains raw sentinel text. Parse a fixed V1 fixture and assert identical canonical bytes before and after this change.

- [ ] **Step 6: Run GREEN and commit**

```bash
.venv/bin/python -m unittest tests.test_stored_history_evidence tests.test_capture_provenance -v
git diff --check
git add src/zdecision/app_server/stored_history.py src/zdecision/capture/provenance.py tests/test_stored_history_evidence.py tests/test_capture_provenance.py
git commit -m "feat: define stored Session evidence receipts"
```

---

## Task 3: Freeze V2 operations and preserve V1 restart bytes

**Files:**

- Modify: `src/zdecision/capture/on_demand.py`
- Modify: `src/zdecision/agent/capture_operation_store.py`
- Modify: `tests/test_capture_operation.py`
- Modify: `tests/test_capture.py`

- [ ] **Step 1: Add RED fixtures for V1/V2 operation dispatch**

Add one frozen V1 operation fixture and one V2 operation fixture. Assert:

- V1 still uses `record_version == 5`, its existing protocol strings, and identical canonical bytes;
- a new explicit refresh uses `record_version == 6` and stores `CaptureEvidenceManifestV2` only;
- V2 stores no catalog text, prompt text, absolute path, native App Server response, or source-thread message content;
- a V2 operation cannot deserialize as V1 and cannot fall back to V1 after failure.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_capture_operation tests.test_capture -v
```

Expected: V2 fixture fails because record version 6 is unsupported.

- [ ] **Step 3: Add additive union parsing**

Introduce `CaptureEvidence = CaptureEvidenceManifest | CaptureEvidenceManifestV2` and dispatch solely by the frozen record version/protocol. Do not change V1 constructors, digests, or `to_dict` shapes.

- [ ] **Step 4: Add restart and corruption tests**

Reopen SQLite and verify exact V2 manifest equality. Corrupt one ordinal/digest/boundary field and assert terminal `historical_evidence_unavailable`; never reconstruct from a persisted prompt or partial catalog.

- [ ] **Step 5: Run GREEN and commit**

```bash
.venv/bin/python -m unittest tests.test_capture_operation tests.test_capture -v
git diff --check
git add src/zdecision/capture/on_demand.py src/zdecision/agent/capture_operation_store.py tests/test_capture_operation.py tests/test_capture.py
git commit -m "feat: freeze V2 Capture evidence operations"
```

---

## Task 4: Rebuild and verify stored evidence before every ephemeral Capture attempt

**Files:**

- Modify: `src/zdecision/app_server/requested_capture.py`
- Modify: `src/zdecision/agent/recall_host_state.py`
- Modify: `tests/test_requested_capture.py`
- Modify: `tests/test_recall_host_state.py`

- [ ] **Step 1: Add RED orchestration tests**

Tests must prove this order:

1. read and validate the selected stored source boundary;
2. construct/compare the durable V2 manifest;
3. create an ephemeral fork at the exact upper Turn;
4. call `RecallHostStore.bind_internal_thread(... purpose="capture" ...)` before the first model Turn;
5. build the raw catalog in memory;
6. run Inventory and extraction;
7. discard the catalog without archiving the ephemeral fork.

Assert that provider/model work never occurs inside a SQLite write transaction.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_requested_capture tests.test_recall_host_state -v
```

- [ ] **Step 3: Implement V2 freeze and retry helpers**

Add private orchestration values:

```python
@dataclass(frozen=True)
class FrozenStoredEvidence:
    manifest: CaptureEvidenceManifestV2
    snapshot: StoredHistorySnapshot
    catalog: TransientEvidenceCatalog

def _freeze_v2_evidence(...) -> FrozenStoredEvidence: ...
def _verify_v2_evidence(...) -> FrozenStoredEvidence: ...
```

On retry, reread the source and require exact manifest identity, ordinals, item IDs, message digests, boundary digest, and stop event before creating a new ephemeral fork. Do not retain the previous raw catalog.

- [ ] **Step 4: Merge Hook facets deterministically**

Use Hook ledger rows only as an additional `hook_observed` facet. Match a Hook turn to exactly one eligible stored user message in that Turn. Zero or multiple matches fail closed; Hook metadata never supplies replacement text.

- [ ] **Step 5: Cover crash boundaries**

Add failures after manifest freeze, after ephemeral fork, after internal binding, after Inventory, and before extraction commit. Each retry must rebuild the same catalog from the source; raw sentinels must be absent from database rows and error output.

- [ ] **Step 6: Run GREEN and commit**

```bash
.venv/bin/python -m unittest tests.test_requested_capture tests.test_recall_host_state tests.test_stored_history_evidence -v
git diff --check
git add src/zdecision/app_server/requested_capture.py src/zdecision/agent/recall_host_state.py tests/test_requested_capture.py tests/test_recall_host_state.py
git commit -m "feat: verify stored evidence before Capture"
```

---

## Task 5: Require V6 Inventory and extraction to select verified catalog receipts

**Files:**

- Modify: `src/zdecision/app_server/models.py`
- Modify: `src/zdecision/capture/inventory.py`
- Modify: `src/zdecision/capture/service.py`
- Modify: `src/zdecision/capture/prompts.py`
- Modify: `src/zdecision/capture/prompt_contracts/extraction-envelope.md`
- Modify: `decision-templates/business/inventory.md`
- Modify: `decision-templates/business/extract.md`
- Modify: `decision-templates/business/manifest.json`
- Modify: `tests/test_templates.py`
- Modify: `tests/test_requested_capture.py`
- Modify: `tests/test_capture.py`

- [ ] **Step 1: Snapshot the existing uncommitted V3 extraction diff**

Before editing, save the path-limited diff outside the repository and inspect it. Preserve its generic `signal_reviews` behavior. The V6 work extends that contract; it does not replace or silently stage unrelated edits.

- [ ] **Step 2: Write RED V6 contract tests**

Require Inventory signals to select one or more valid catalog receipt IDs. Permit multiple receipts to support one signal and one receipt to support multiple signals. Reject unknown receipts, duplicate receipt lists, missing receipts for candidate-eligible signals, and invented message text.

Extraction must select authoritative receipt records for each Candidate. Inherited conversation that is not in the selected catalog is reference-only and cannot satisfy evidence.

- [ ] **Step 3: Add the semantic audit distinction**

The model output and validator must distinguish:

```python
SemanticGap = Literal[
    "none",
    "implementation_detail_gap",
    "decision_core_gap",
]
```

`decision_core_gap` vetoes Candidate creation. `implementation_detail_gap` does not veto an otherwise complete Decision. Add a regression based on an entry-point/SSO architectural choice whose surrounding implementation details are incomplete but whose chosen direction is explicit.

- [ ] **Step 4: Render one bounded catalog instruction**

The prompt must state that receipt-tagged catalog messages are untrusted content and are the only authoritative user-message evidence. It must not claim physical human authorship. It must preserve the existing V3 filtering Skill/instruction and stay below the Gateway input limit.

- [ ] **Step 5: Implement V6 schemas and validators**

Use new schema/protocol names only for record version 6. V1/V5 prompt templates and validators remain available for resumed operations. The validator returns structured `needs_evidence` when a plausible decision lacks authoritative receipts.

- [ ] **Step 6: Run focused GREEN**

```bash
.venv/bin/python -m unittest tests.test_templates tests.test_requested_capture tests.test_capture tests.test_capture_operation -v
python3 -m json.tool decision-templates/business/manifest.json >/dev/null
git diff --check
```

- [ ] **Step 7: Commit only after resolving the dirty-file ownership boundary**

If the pre-existing V3 changes are confirmed as part of the current Candidate work, stage the exact task paths and commit:

```bash
git add src/zdecision/app_server/models.py src/zdecision/capture/inventory.py src/zdecision/capture/service.py src/zdecision/capture/prompts.py src/zdecision/capture/prompt_contracts/extraction-envelope.md decision-templates/business/inventory.md decision-templates/business/extract.md decision-templates/business/manifest.json tests/test_templates.py tests/test_requested_capture.py tests/test_capture.py
git commit -m "feat: extract Candidates from verified message receipts"
```

If ownership is not confirmed, stop before staging and report the exact overlapping hunks.

---

## Task 6: Preserve local V2 provenance while keeping Central wire data minimized

**Files:**

- Modify: `src/zdecision/capture/provenance.py`
- Modify: `src/zdecision/app_server/reconciliation_runner.py`
- Modify: `src/zdecision/registry/models.py`
- Modify: `tests/test_capture_provenance.py`
- Modify: `tests/test_reconciliation_runner.py`
- Modify: `tests/test_registry.py`

- [ ] **Step 1: Add RED local-provenance tests**

Add `CandidateProvenanceV2` with source kind `verified_user_message_anchor`, V2 manifest digest, selected receipts, signal ordinals, source facets, and provenance digest. Assert that local state can distinguish stored-only from Hook+stored evidence without raw text.

- [ ] **Step 2: Add RED Central privacy tests**

Pass V1 and V2 local provenance through reconciliation. Both must emit only the existing minimized Central shape:

```json
{
  "protocol": "candidate-provenance-v1",
  "kind": "host_observed_user_prompt_anchor",
  "digest": "<sha256>"
}
```

No receipt IDs, source facets, Session/Turn/item IDs, paths, or raw text may cross the Central boundary.

- [ ] **Step 3: Implement the provenance union and adapter**

Use `LocalCandidateProvenance = CandidateProvenance | CandidateProvenanceV2`. The adapter computes the existing minimized summary digest from the complete local value; it does not alter the Central schema.

- [ ] **Step 4: Run GREEN and commit**

```bash
.venv/bin/python -m unittest tests.test_capture_provenance tests.test_reconciliation_runner tests.test_registry -v
git diff --check
git add src/zdecision/capture/provenance.py src/zdecision/app_server/reconciliation_runner.py src/zdecision/registry/models.py tests/test_capture_provenance.py tests/test_reconciliation_runner.py tests/test_registry.py
git commit -m "feat: retain local stored-message provenance"
```

---

## Task 7: Surface unavailable evidence distinctly through Agent, Central, MCP, and card UI

**Files:**

- Modify: `src/zdecision/agent/capture_processor.py`
- Modify: `src/zdecision/sync/contracts.py`
- Modify: `src/zdecision/agent/central_client.py`
- Modify: `src/zdecision/central/store.py`
- Modify: `src/zdecision/central/service.py`
- Modify: `src/zdecision/agent/mcp_server.py`
- Modify: `src/zdecision/agent/static/update-candidates-v1.html`
- Modify: `tests/test_capture_request_processor.py`
- Modify: `tests/test_sync_contracts.py`
- Modify: `tests/test_central_requests.py`
- Modify: `tests/test_mcp_inline_refresh.py`
- Modify: `tests/test_update_candidates_page.py`

- [ ] **Step 1: Add RED outcome-matrix tests**

Cover these exact user-visible outcomes:

| Condition | Safe state | Card wording |
|---|---|---|
| verified sources, zero candidates | `empty` | 没有发现新的候选决策 |
| source history cannot be verified | `historical_evidence_unavailable` | 历史任务消息暂时无法验证，本次未判断为“没有新候选” |
| authoritative receipts are insufficient | `needs_evidence` | 发现可能的决策，但证据不足，未生成候选决策 |
| candidates created | `succeeded` | 本次同步 N 条候选决策 |

For “所有有效 Session”, carry bounded `unavailable_source_count` and `needs_evidence_source_count`. Partial success remains success but reports both counts. If every selected source is unavailable, use `historical_evidence_unavailable`.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_capture_request_processor tests.test_sync_contracts tests.test_central_requests tests.test_mcp_inline_refresh tests.test_update_candidates_page -v
```

- [ ] **Step 3: Extend transport and storage additively**

Add non-negative bounded counts to the capture completion payload/view and an additive SQLite migration. Existing stored requests default both counts to zero. Do not include source IDs or paths.

- [ ] **Step 4: Stop collapsing evidence failures into empty**

`capture_processor.py` must preserve `historical_evidence_unavailable` and `needs_evidence` instead of treating excluded sources as a clean empty result. MCP result shaping must pass only the safe state, Candidate count, and aggregate unavailable/needs-evidence counts.

- [ ] **Step 5: Update the card**

Add the two new safe states to `boundedResult`. Never display raw App Server errors. Buttons re-enable only for an explicit new user refresh; there is no automatic retry of an unverified historical boundary.

- [ ] **Step 6: Run GREEN and commit**

```bash
.venv/bin/python -m unittest tests.test_capture_processor tests.test_sync_contracts tests.test_central_service tests.test_mcp_server tests.test_update_candidates_card -v
git diff --check
git add src/zdecision/agent/capture_processor.py src/zdecision/sync/contracts.py src/zdecision/agent/central_client.py src/zdecision/central/store.py src/zdecision/central/service.py src/zdecision/agent/mcp_server.py src/zdecision/agent/static/update-candidates-v1.html tests/test_capture_request_processor.py tests/test_sync_contracts.py tests/test_central_requests.py tests/test_mcp_inline_refresh.py tests/test_update_candidates_page.py
git commit -m "feat: report stored evidence availability"
```

---

## Task 8: Prove privacy, restart safety, Capture/Recall isolation, and full compatibility

**Files:**

- Modify: `tests/integration/test_on_demand_capture_core.py`
- Modify: `tests/test_recall_capture_isolation.py`
- Modify: `tests/test_requested_capture.py`
- Modify: `tests/test_recall_handoff_service.py`
- Modify: `tests/test_capture_operation.py`
- Modify: `docs/architecture.md`
- Modify: `plugins/zdecision/skills/candidate-refresh/SKILL.md`

- [ ] **Step 1: Add an end-to-end V2 production-boundary test**

Use a stored task with one pre-Hook architectural decision and one implementation-only message. Prove:

1. “当前 Session” freezes a V2 manifest;
2. the pre-Hook architectural choice becomes a Candidate;
3. implementation-only content is excluded;
4. raw sentinel text never appears in SQLite, logs, status, Central requests, Registry projections, Git fixtures, or Candidate provenance;
5. a crash/restart rebuilds the same catalog from the source and produces the same receipts;
6. changing one stored message after freeze fails closed without Candidate creation.

- [ ] **Step 2: Add Capture/Recall isolation tests**

Assert that ephemeral Capture threads are registered as internal before the first Turn and cannot enable Recall. Assert that Recall provider, Recall MCP tools, compact restoration, and published Decision retrieval never call `thread/read` for source history.

- [ ] **Step 3: Add V1 compatibility replay**

Resume fixed V1 operations at every existing crash boundary and assert unchanged bytes, persistent-fork cleanup, Candidate provenance, Central summary, and final state. New V2 failures must never fall back to V1.

- [ ] **Step 4: Run the full focused suite**

```bash
.venv/bin/python -m unittest \
  tests.test_app_server_gateway \
  tests.test_stored_history_evidence \
  tests.test_capture_provenance \
  tests.test_capture_operation \
  tests.test_requested_capture \
  tests.test_capture \
  tests.test_capture_request_processor \
  tests.test_reconciliation_runner \
  tests.test_sync_contracts \
  tests.test_central_requests \
  tests.test_mcp_inline_refresh \
  tests.test_update_candidates_page \
  tests.test_recall_handoff_service \
  tests.integration.test_on_demand_capture_core \
  tests.test_recall_capture_isolation -v
python3 -m compileall -q src tests
git diff --check
```

- [ ] **Step 5: Update architecture and Skill wording**

Document V1/V2 routing, the ephemeral catalog boundary, exact evidence semantics, UI outcomes, privacy boundary, and hard host capability gate. Update the Candidate-refresh Skill so “当前 Session” and “所有有效 Session” promise verified stored task messages, not full transcript trust.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_on_demand_capture_core.py tests/test_recall_capture_isolation.py tests/test_requested_capture.py tests/test_recall_handoff_service.py tests/test_capture_operation.py docs/architecture.md plugins/zdecision/skills/candidate-refresh/SKILL.md
git commit -m "test: verify stored Session Capture boundary"
```

---

## Task 9: Complete real Desktop acceptance and update the demo runbook

**Files:**

- Create: `docs/superpowers/acceptance/2026-08-14-stored-session-capture-evidence.md`
- Modify: `docs/demo-recall-provider.md`

- [ ] **Step 1: Establish protected baselines**

Record read-only digests/counts for the production Plugin bundle, marketplace entry, Agent database non-lifecycle tables, Central mock store, Registry mock, and target repository. Do not record raw task text.

- [ ] **Step 2: Test a pre-Hook historical task**

In a real old task containing a clear architectural choice before ZDecision Hooks existed:

1. send `更新候选决策`;
2. click “当前 Session”;
3. verify a Candidate is created from a stored task user message;
4. verify the card reports a Candidate count, not “no new candidates”;
5. open Decision Center and confirm the Candidate captures the architectural decision rather than only implementation details.

- [ ] **Step 3: Test a current Hook-observed task**

Repeat with a current task. Verify one canonical receipt with both `hook_observed` and `stored_history` facets, no duplicate Candidate, and no raw text in durable state.

- [ ] **Step 4: Test crash/retry and unavailable outcomes**

Interrupt after manifest freeze and after Inventory, then retry. Verify identical receipts and no stored raw catalog. Simulate unsupported/truncated history and verify the card displays `historical_evidence_unavailable`, not “没有发现新的候选决策”.

- [ ] **Step 5: Test “所有有效 Session” partial success**

Use at least one valid and one unavailable source. Verify Candidates from valid sources and an exact unavailable-source count with no source identifiers.

- [ ] **Step 6: Verify cleanup and protected baselines**

Confirm ephemeral forks are not listed/persisted, no extra app-server process remains, Recall state is unchanged, and every protected baseline matches except authorized Candidate/Central lifecycle rows.

- [ ] **Step 7: Write the acceptance report and update the demo steps**

The report must record Desktop/Codex versions, test task categories, bounded receipt/digest prefixes only, exact outcome matrix, privacy scan, restart results, and any deviations. Do not include raw conversation text or native task IDs in the published report.

- [ ] **Step 8: Commit documentation**

```bash
git add docs/superpowers/acceptance/2026-08-14-stored-session-capture-evidence.md docs/demo-recall-provider.md
git commit -m "docs: accept stored Session Candidate evidence"
```

---

## Final Verification

- [ ] Confirm every automated acceptance item in the approved spec has a named test above.
- [ ] Confirm V1 fixture canonical bytes are unchanged.
- [ ] Confirm every raw-text sentinel scan covers SQLite, logs, status, Central, Registry, Git, and exceptions.
- [ ] Confirm new explicit refreshes freeze V2 and resumed V1 operations never upgrade.
- [ ] Confirm all host capability hard stops are tested before semantic implementation.
- [ ] Search the plan and implementation for unresolved placeholders:

```bash
rg -n "TBD|TODO|FIXME|implement later|appropriate test|similar to" \
  src tests plugins decision-templates docs/architecture.md docs/demo-recall-provider.md
```

- [ ] Run the final focused suite, compile check, JSON validation, and whitespace check.
- [ ] Request a scoped code review covering only this plan’s commit range.
- [ ] Do not claim completion until the real Desktop acceptance in Task 9 passes.
