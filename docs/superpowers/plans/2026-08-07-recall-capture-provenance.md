# Recall-to-Capture Provenance Firewall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent recalled formal Decisions and other inherited reference context from silently qualifying themselves as new Candidate evidence, while preserving the existing explicit Candidate-refresh, Review, preview, and exact publication-confirmation workflow.

**Architecture:** Freeze a host-issued, prompt-free evidence manifest from the local Hook event ledger before any Capture fork is created. Inventory may select only opaque receipt IDs from that manifest; Extraction may select only host-eligible Inventory signals; Candidate provenance remains a versioned local sidecar and follows the observation through reconciliation. Central receives only a versioned provenance kind and digest. Existing `extractor-v3`/`extractor-v4` records, legacy Candidate batches, Reviews, and published Decisions remain readable and unchanged.

**Tech Stack:** Python 3.11, standard-library `sqlite3`, existing Codex Hook and app-server integration, existing local operation/outbox stores, strict JSON schemas, canonical SHA-256 digests, and `unittest`.

## Implementation boundary

- This plan is the authoritative replacement for Task 7 of `2026-08-06-recall-host-gate.md`. Do not implement the former marker-only exclusion.
- Candidate Capture remains explicit: only the existing repository page or inline **当前 Session** / **所有有效 Session** action starts it.
- Do not persist or upload Prompt text, Prompt hashes, transcript text, PRDs, source code, diffs, tool output, evidence excerpts, local paths, Session IDs, Turn IDs, or receipt IDs.
- Call the qualifying source `hook_observed_user_prompt_anchor`. Do not claim `native_human_evidence` or semantic entailment.
- Deterministic code proves receipt membership, source-window boundaries, exact signal-to-Candidate linkage, digest integrity, and upload minimization. The fixed model contract supplies a conservative semantic classification; human Review remains the semantic authority.
- `Candidate` stays unchanged. Provenance is a private Capture-result sidecar and an optional revision field so legacy objects preserve their exact meaning.
- New Capture requests only write `extractor-v5` operations and `candidate-provenance-v1` slice batches. An already-frozen v3/v4 request may finish only under its original immutable protocol and legacy batch contract; it cannot be upgraded, mixed with v5, or used as provenance for a new request.
- Do not add Registry schema changes, a second Review queue, automatic Capture, retrieval, embeddings, reranking, production Decision injection, or Task 8 live-host behavior here.
- A missing or unstable source boundary is a source-level, non-retryable exclusion. A forged or structurally inconsistent receipt is a terminal operation failure. Neither condition may create an infinite model retry loop.
- If the supported host cannot preserve the prompt-event association across freeze, retry, and compaction without rollout parsing, transcript filenames, CWD guessing, or recency heuristics, record `capture_evidence_provenance_unavailable` and stop Packet 3.

---

### Task 1: Freeze host-issued prompt anchors at the Session boundary

**Files:**
- Create: `src/zdecision/capture/provenance.py`
- Modify: `src/zdecision/agent/db.py`
- Modify: `src/zdecision/agent/session_index.py`
- Modify: `src/zdecision/agent/recall_host_state.py`
- Modify: `tests/test_event_ledger.py`
- Modify: `tests/test_session_index.py`
- Modify: `tests/test_recall_host_state.py`
- Create: `tests/test_capture_provenance.py`

**Host-owned values:**

```python
EvidenceKind = Literal["hook_observed_user_prompt_anchor"]

@dataclass(frozen=True)
class PromptAnchor:
    receipt_id: str
    hook_event_id: str
    turn_id: str
    anchor_ordinal: int
    active_reference_set_digest: str | None

@dataclass(frozen=True)
class CaptureEvidenceManifest:
    version: Literal[1]
    kind: EvidenceKind
    source_session_id: str
    previous_handled_event_id: str | None
    upper_stop_event_id: str
    anchors: tuple[PromptAnchor, ...]
    manifest_digest: str

    @classmethod
    def create(
        cls,
        *,
        source_session_id: str,
        previous_handled_event_id: str | None,
        upper_stop_event_id: str,
        anchors: tuple[PromptAnchor, ...],
    ) -> "CaptureEvidenceManifest":
        payload = {
            "version": 1,
            "kind": "hook_observed_user_prompt_anchor",
            "source_session_id": source_session_id,
            "previous_handled_event_id": previous_handled_event_id,
            "upper_stop_event_id": upper_stop_event_id,
            "anchors": [anchor.to_dict() for anchor in anchors],
        }
        return cls(
            version=1,
            kind="hook_observed_user_prompt_anchor",
            source_session_id=source_session_id,
            previous_handled_event_id=previous_handled_event_id,
            upper_stop_event_id=upper_stop_event_id,
            anchors=anchors,
            manifest_digest=hashlib.sha256(
                canonical_json_bytes(payload)
            ).hexdigest(),
        )
```

`receipt_id` is a deterministic opaque identifier derived from the local Hook `event_id` and the fixed evidence kind. The model sees `receipt_id`, `anchor_ordinal`, and the presence or absence of a reference-set digest; it never receives `hook_event_id`, Session identity, Turn identity, or raw Prompt data.

- [ ] **Step 1: Write the RED ledger and manifest tests**

Add tests that record two `UserPromptSubmit -> Stop` windows and prove:

- the query returns only prompt events in `(previous_handled_event_id, upper_stop_event_id]` by SQLite append order;
- the upper event is an exact `Stop` for the same Session, CWD, and repository;
- an unknown lower event, a lower event after the upper event, a different Session/CWD/repository, duplicate event, and post-boundary prompt are rejected;
- receipt IDs and anchor ordinals are stable after database reopen;
- a sentinel raw Prompt, its SHA-256, and a transcript filename are absent from every SQLite text/blob column; and
- manifest parsing rejects unknown fields, duplicate/reordered receipts, invalid digests, empty anchors, and a noncanonical ordinal sequence.

Add migration tests proving an old `session_checkpoints`/`capture_request_sources` database opens with nullable event-boundary columns and is not silently backfilled from Turn text or recency.

Add a Recall Host migration test proving a pre-migration committed gate has `reference_state_version=None`, while a newly committed gate stores version 1 even when its valid `active_set_digest` is `None`.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest \
  tests.test_event_ledger \
  tests.test_session_index \
  tests.test_recall_host_state \
  tests.test_capture_provenance -v
```

Expected: FAIL because event-window anchors, frozen event boundaries, manifest values, and per-Turn reference-set persistence do not exist.

- [ ] **Step 3: Implement the strict provenance values**

In `capture/provenance.py`:

- validate receipt IDs, event IDs, Turn IDs, ordinals, and lowercase SHA-256 digests with bounded patterns;
- make `to_dict()` and `from_dict()` closed-world and canonical;
- compute `manifest_digest` over every field except the digest itself;
- require one to 100 anchors, unique receipt/event/Turn identities, canonical receipt order, and ordinals `1..N`; and
- expose a deterministic `prompt_anchor_receipt_id(hook_event_id)` helper that does not reveal the raw event ID.

Do not add Prompt text, Prompt hash, excerpt, repository path, or model-authored source labels to these values.

- [ ] **Step 4: Add the exact ledger-window query**

Add this read-only API to `AgentDatabase`:

```python
def prompt_anchors_between(
    self,
    *,
    session_id: str,
    cwd: str,
    repository_id: str,
    previous_handled_event_id: str | None,
    upper_stop_event_id: str,
) -> tuple[PromptAnchor, ...]:
    """Return canonical Hook prompt anchors inside one frozen Stop window."""
```

Implementation rules:

- resolve both boundaries from `events.rowid`, never from lexicographic Turn IDs;
- require the upper row to be an exact `Stop` with the supplied source tuple;
- when a lower boundary is present, require it to be an earlier exact `Stop` with the same source tuple;
- select only `UserPromptSubmit` rows from the same tuple and bounded rowid interval;
- order by rowid, enforce the 100-anchor limit, and derive receipts in code; and
- initialize `active_reference_set_digest=None`; Task 3 enriches it only from committed Recall Host state.

- [ ] **Step 5: Freeze and acknowledge event boundaries**

Extend `FrozenSessionSource` with:

```python
previous_handled_event_id: str | None
upper_stop_event_id: str | None
```

Add nullable `handled_event_id` to `session_checkpoints` and nullable `previous_handled_event_id` / `upper_stop_event_id` to `capture_request_sources`.

`freeze_sources()` copies `handled_event_id` and `latest_event_id` into the request source in the same transaction as the existing Turn/fingerprint boundary. `acknowledge()` advances `handled_event_id` to `upper_stop_event_id` together with the handled Turn, fingerprint, and head commit. A replayed request reads the exact frozen values.

For an upgraded checkpoint with a handled Turn but no handled event ID, permit only an exact lookup of the matching local `Stop` row for that same source. If the match is absent or non-unique, leave the lower event boundary unavailable; do not guess or reuse the complete Session.

- [ ] **Step 6: Persist the committed Turn reference-set digest**

Add nullable `active_set_digest` and nullable `reference_state_version` to `recall_turn_gates`, `TurnGate`, and `_gate()`. `commit_turn_gate()` writes `reference_state_version=1` plus the digest into the gate row in the same transaction that commits the gate and updates `recall_sessions`. Replayed commits require the same digest through the existing commit fingerprint.

The version bit distinguishes a newly committed Turn whose valid active set is genuinely empty from a pre-migration committed Turn whose per-Turn reference state was never stored. Existing committed rows keep `reference_state_version=NULL`; Task 3 must fail closed on those rows rather than interpreting them as recall-free.

This field records whether a qualifying Prompt Turn was exposed to recalled reference context. It is not a semantic proof and it does not contain Decision IDs or envelope text.

- [ ] **Step 7: Run GREEN and migration regressions**

```bash
.venv/bin/python -m unittest \
  tests.test_event_ledger \
  tests.test_session_index \
  tests.test_recall_host_state \
  tests.test_capture_provenance -v
```

Expected: all tests pass; raw Prompt sentinels remain absent from local persistence.

- [ ] **Step 8: Commit**

```bash
git add \
  src/zdecision/capture/provenance.py \
  src/zdecision/agent/db.py \
  src/zdecision/agent/session_index.py \
  src/zdecision/agent/recall_host_state.py \
  tests/test_event_ledger.py \
  tests/test_session_index.py \
  tests/test_recall_host_state.py \
  tests/test_capture_provenance.py
git commit -m "feat: freeze capture prompt anchors"
```

---

### Task 2: Add the extractor-v5 evidence-first Capture protocol

**Files:**
- Modify: `src/zdecision/ids.py`
- Modify: `src/zdecision/capture/provenance.py`
- Modify: `src/zdecision/capture/inventory.py`
- Modify: `src/zdecision/capture/service.py`
- Modify: `src/zdecision/capture/on_demand.py`
- Modify: `src/zdecision/app_server/models.py`
- Modify: `src/zdecision/capture/prompt_contracts/inventory-envelope.md`
- Modify: `src/zdecision/capture/prompt_contracts/extraction-envelope.md`
- Modify: `tests/test_inventory.py`
- Modify: `tests/test_capture.py`
- Modify: `tests/test_capture_operation.py`
- Modify: `tests/test_templates.py`

**Private sidecar values:**

```python
SignalDisposition = Literal[
    "candidate_eligible",
    "existing_decision_adoption",
    "needs_evidence",
    "excluded_reference_only",
    "excluded_code_fact_only",
    "excluded_unverified",
]

@dataclass(frozen=True)
class SignalProvenance:
    signal_ordinal: int
    evidence_receipt_ids: tuple[str, ...]
    active_reference_set_digests: tuple[str, ...]
    disposition: SignalDisposition
    provenance_digest: str

@dataclass(frozen=True)
class CandidateProvenance:
    version: Literal[1]
    kind: EvidenceKind
    candidate_id: str
    manifest_digest: str
    source_signal_ordinal: int
    evidence_receipt_ids: tuple[str, ...]
    active_reference_set_digests: tuple[str, ...]
    reference_decision_ids: tuple[str, ...]
    disposition: Literal["candidate_eligible"]
    provenance_digest: str
```

`reference_decision_ids` is host-derived and remains empty during the Host Probe gate because the probe is not a formal Decision. A later production Recall provider may resolve a validated active-set digest to local Decision IDs; this plan must not infer IDs from envelope text.

- [ ] **Step 1: Write RED schema, validation, and compatibility tests**

Cover these contracts:

- legacy `validate_inventory()` and legacy extraction output still parse their original exact field sets;
- v5 Inventory requires canonical `signal_ordinal` plus receipt IDs selected from the supplied enum;
- `current_confirmed` requires a nonempty evidence set;
- unknown, duplicate, reordered, cross-manifest, or forged receipts reject the complete v5 result;
- host classification maps reference-influenced short confirmation/adoption to `needs_evidence` unless an explicit host-owned adoption receipt exists;
- direct `explicit_user_direction` with a valid prompt anchor can be `candidate_eligible` even when a reference set was active;
- non-current, uncertain, reference-only, and code/tool-only corpus cases never become eligible observations;
- v5 Extraction must return one `source_signal_ordinal` per Candidate, only from the eligible ordinal enum, with no duplicate ordinal;
- Extraction cannot add, remove, or reorder the selected Inventory signal's receipt set; and
- v3/v4 frozen inputs and v1 validated results retain their exact historical bytes, while v5 requires a manifest and v2 result sidecars.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest \
  tests.test_inventory \
  tests.test_capture \
  tests.test_capture_operation \
  tests.test_templates -v
```

Expected: FAIL because the v5 structured schemas, sidecars, and dual-version result parser do not exist.

- [ ] **Step 3: Version the frozen input without rewriting legacy bytes**

Change `ON_DEMAND_CAPTURE_PROTOCOL` to `extractor-v5` and add `_FROZEN_V5_FIELDS` in `capture/on_demand.py`.

`FrozenCaptureInput` accepts record versions 3, 4, and 5:

- v3 keeps its exact original field set and no route context;
- v4 keeps its exact original field set and route context;
- v5 requires the v4 route context plus `evidence_manifest`;
- `create()` only writes record version 5;
- the canonical manifest enters `_identity_payload()`, operation ID, serialized bytes, replay comparison, and frozen digest; and
- a v5 input cannot use an `extractor-v3`/`extractor-v4` revision, while an old input cannot carry a v5 manifest.

- [ ] **Step 4: Add dynamic v5 structured-output schemas**

Preserve the old call forms, and add bounded v5 forms:

```python
def inventory_output_schema(
    evidence_receipt_ids: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """Return the legacy schema or a receipt-bounded v5 schema."""

def extraction_output_schema(
    product: str,
    eligible_signal_ordinals: tuple[int, ...] | None = None,
) -> dict[str, object]:
    """Return the legacy schema or a signal-bounded v5 schema."""
```

The v5 Inventory signal adds `signal_ordinal` and `evidence_receipt_ids`. The receipt array uses the exact host enum. The v5 Candidate adds `source_signal_ordinal` using the exact eligible ordinal enum. Both schemas remain closed-world and preserve existing size/count limits.

- [ ] **Step 5: Validate Inventory receipts and derive dispositions**

Keep `validate_inventory(value)` unchanged for legacy callers. Add:

```python
def validate_inventory_v5(
    value: object,
    manifest: CaptureEvidenceManifest,
) -> tuple[InventoryResult, tuple[SignalProvenance, ...]]:
    """Validate model-selected receipts and derive conservative host dispositions."""
```

The validator:

- reconstructs the existing business `DecisionSignal` values without adding provenance fields to them;
- verifies ordinals, exact manifest membership, uniqueness, and manifest order;
- derives reference-set digests only from the selected host anchors;
- treats `confirmation_basis` as model-authored business interpretation, not source authority;
- allows `candidate_eligible` only for a high-confidence, `current_confirmed` signal with a valid receipt and an accepted basis;
- routes reference-influenced `explicit_user_confirmation` and `adopted_decision_contract` to `needs_evidence` unless a future host-owned adoption receipt is explicitly supplied; and
- keeps every noneligible signal in the private v5 result for coverage, never in `observations`.

The prompt contract must explicitly state that recalled Decisions, assistant proposals, tool/code facts, Capture artifacts, and compaction summaries do not issue receipt IDs. It must explain that receipt ordinal N corresponds to the Nth eligible Hook-observed user Prompt in the frozen source window. It must not depend on marker keywords.

- [ ] **Step 6: Bind Extraction to one eligible signal**

Keep `validate_extraction_output()` unchanged for legacy callers. Add a v5 validator that accepts the validated Inventory and provenance sidecars, rejects any noneligible or repeated ordinal, calls the existing Candidate-content validation, and returns:

```python
tuple[
    tuple[Candidate, ...],
    tuple[CandidateProvenance, ...],
]
```

The host copies the exact receipt set and reference-set digests from the selected signal. The model cannot return provenance kind, receipt IDs, reference IDs, or provenance digest during Extraction. `ValidatedCaptureResult.create()` keeps its legacy branch for an already-frozen v3/v4 operation and writes result version 1 there; only a v5 frozen input can write result version 2.

- [ ] **Step 7: Add a dual-version committed Capture result**

Preserve the six-field legacy `ValidatedCaptureResult` serialization as result version 1 by field-shape detection. Add result version 2 for v5 with:

```python
result_version: Literal[2]
signal_provenance: tuple[SignalProvenance, ...]
candidate_provenance: tuple[CandidateProvenance, ...]
```

For result version 2:

- `observations` contains only Candidates whose sidecar disposition is `candidate_eligible`;
- every observation has exactly one matching Candidate sidecar and source signal;
- every noneligible signal remains only in `signal_provenance`;
- inventory, extraction, manifest, signal sidecars, Candidate sidecars, and observations all enter `result_digest`; and
- `from_dict()` rejects v5/v1 and legacy/v2 pairings without rewriting old records.

- [ ] **Step 8: Run GREEN**

```bash
.venv/bin/python -m unittest \
  tests.test_inventory \
  tests.test_capture \
  tests.test_capture_operation \
  tests.test_templates -v
```

Expected: all tests pass, including exact legacy round trips.

- [ ] **Step 9: Commit**

```bash
git add \
  src/zdecision/ids.py \
  src/zdecision/capture/provenance.py \
  src/zdecision/capture/inventory.py \
  src/zdecision/capture/service.py \
  src/zdecision/capture/on_demand.py \
  src/zdecision/app_server/models.py \
  src/zdecision/capture/prompt_contracts/inventory-envelope.md \
  src/zdecision/capture/prompt_contracts/extraction-envelope.md \
  tests/test_inventory.py \
  tests/test_capture.py \
  tests/test_capture_operation.py \
  tests/test_templates.py
git commit -m "feat: bind capture candidates to prompt anchors"
```

---

### Task 3: Enforce the manifest before model work and isolate internal Threads

**Files:**
- Modify: `src/zdecision/app_server/requested_capture.py`
- Modify: `src/zdecision/app_server/reconciliation_runner.py`
- Modify: `src/zdecision/agent/capture_processor.py`
- Modify: `src/zdecision/agent/service.py`
- Modify: `tests/test_requested_capture.py`
- Modify: `tests/test_reconciliation_runner.py`
- Modify: `tests/test_capture_request_processor.py`

**Runner contract:**

```python
class SourceEvidenceUnavailable(RequestedCaptureError):
    """The frozen source cannot produce a verified prompt-anchor manifest."""

class RequestedCaptureRunner:
    def __init__(
        self,
        *,
        gateway: AppServerGateway,
        operation_store: CaptureOperationStore,
        template_catalog: TemplateCatalog,
        evidence_ledger: AgentDatabase,
        recall_host_store: RecallHostStore,
    ) -> None:
        self.gateway = gateway
        self.operation_store = operation_store
        self.template_catalog = template_catalog
        self.evidence_ledger = evidence_ledger
        self.recall_host_store = recall_host_store
```

- [ ] **Step 1: Write RED runner-ordering tests**

Add fake-gateway/store tests proving:

- missing event boundaries, no prompt anchors, a noncommitted or unversioned Recall gate, or an inconsistent reference-set digest raises `SourceEvidenceUnavailable` before `fork_disposable_thread()`;
- a valid manifest is byte-identical on retry and after process restart, even when later Prompts and Stops are added;
- a v4 open or committed operation remains on its frozen legacy result/upload path and cannot be resumed as v5 or mixed into a v1 provenance batch;
- a request that already owns a legacy operation fails with `legacy_capture_protocol_mixed` before creating a new v5 sibling operation;
- the Inventory schema receives only the frozen receipt enum;
- Extraction receives only host-eligible signal ordinals;
- receipt/provenance validation failure terminalizes the operation once and does not request another model retry;
- the Capture child is registered with purpose `capture` after fork and before the first structured Turn;
- the reconciliation Thread is registered with purpose `reconciliation` before its first structured Turn; and
- Recall activation/gating remains denied for both internal purposes.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest \
  tests.test_requested_capture \
  tests.test_reconciliation_runner \
  tests.test_capture_request_processor -v
```

Expected: FAIL because the runners do not own the evidence ledger or Recall Host store and do not register internal Threads.

- [ ] **Step 3: Build the immutable manifest before the Capture operation**

In `RequestedCaptureRunner.run()` keep the existing interactive-thread and completed-boundary checks. If an operation already exists, branch on its frozen record version before building new state: v3/v4 resumes only the existing legacy Inventory/Extraction/result path, while v5 reuses its frozen manifest. A request that already contains any legacy operation must not create a new v5 sibling source operation.

For a new operation or an existing v5 operation:

1. require `source.upper_stop_event_id`;
2. call `evidence_ledger.prompt_anchors_between()` with the frozen lower/upper event IDs;
3. for each anchor Turn, read `RecallHostStore.get_turn_gate()`;
4. reject a present gate unless it is committed with `reference_state_version=1`;
5. copy only that committed gate's persisted `active_set_digest` into the anchor;
6. create one canonical `CaptureEvidenceManifest`;
7. create or verify the `extractor-v5` operation with that exact manifest; and
8. only then begin a disposable attempt and fork.

Do not read rollout files, transcript text, Prompt text, or marker strings. A later event cannot enlarge an existing operation manifest.

- [ ] **Step 4: Register internal Threads before their first Turn**

Immediately after a Capture fork succeeds, call:

```python
recall_host_store.bind_internal_thread(
    thread_id=fork_thread_id,
    parent_thread_id=source.session_id,
    purpose="capture",
    operation_id=attempt.attempt_id,
    now=_now(),
)
```

For the root reconciliation Thread, bind it with `parent_thread_id=thread_id` as an explicit self-parent sentinel because `start_disposable_thread()` creates no inherited parent. Use an operation ID derived from the request ID, slice ID, and thread ID so a replacement Thread receives a distinct binding. This field is diagnostic; recall denial depends only on the exact internal `thread_id` membership.

If either binding fails, archive/abandon the disposable Thread and fail closed before `run_structured_turn()`.

- [ ] **Step 5: Run the v5 model sequence**

Append a bounded host manifest section to the Inventory developer prompt containing only receipt ID, anchor ordinal, and optional reference-set digest. Run Inventory with the receipt-bounded schema and validate it before starting Extraction. Run Extraction with only eligible signal ordinals and create the v2 validated result.

Extend `SessionCaptureResult` with `protocol_revision` plus the exact `signal_provenance` and `candidate_provenance` tuples returned by the committed v2 result. For a resumed legacy operation, both tuples are empty and `protocol_revision` identifies its frozen legacy path; an empty sidecar is never interpreted as v5 evidence. The processor rejects a slice containing both legacy and v5 capture results before reconciliation.

Treat a schema/provider transport failure under the existing attempt policy. Treat an unknown/reordered/forged receipt, signal linkage mismatch, or result-sidecar mismatch as `capture_provenance_invalid`: fail the operation terminally and do not reopen model computation.

- [ ] **Step 6: Make unavailable evidence a source-level terminal exclusion**

Catch `SourceEvidenceUnavailable` inside `_capture_sources()` before the processor-wide exception mapping:

```python
except SourceEvidenceUnavailable:
    self.session_index.mark_excluded(
        group.request_id,
        source.source_key,
        "user_prompt_evidence_unavailable",
    )
    continue
```

For **所有有效 Session**, continue with other frozen sources. For **当前 Session**, return the existing zero-Candidate completion when no valid source remains. Do not retry, fork, upload, or emit raw diagnostic data.

- [ ] **Step 7: Wire one shared local host store**

In `configured_processor()`, open `RecallHostStore` on the same absolute local state path as `AgentDatabase`, inject it into both runners, and close it on construction failure. The worker-lifetime processor owns the shared connection until process exit. Keep the current Candidate-refresh card and MCP command behavior unchanged.

- [ ] **Step 8: Run GREEN**

```bash
.venv/bin/python -m unittest \
  tests.test_requested_capture \
  tests.test_reconciliation_runner \
  tests.test_capture_request_processor -v
```

Expected: all tests pass; unavailable evidence produces no fork and no retry loop.

- [ ] **Step 9: Commit**

```bash
git add \
  src/zdecision/app_server/requested_capture.py \
  src/zdecision/app_server/reconciliation_runner.py \
  src/zdecision/agent/capture_processor.py \
  src/zdecision/agent/service.py \
  tests/test_requested_capture.py \
  tests/test_reconciliation_runner.py \
  tests/test_capture_request_processor.py
git commit -m "feat: enforce capture evidence before model work"
```

---

### Task 4: Preserve minimal provenance through reconciliation and Central

**Files:**
- Modify: `src/zdecision/capture/provenance.py`
- Modify: `src/zdecision/capture/reconciliation.py`
- Modify: `src/zdecision/app_server/reconciliation_runner.py`
- Modify: `src/zdecision/agent/capture_processor.py`
- Modify: `src/zdecision/agent/request_state.py`
- Modify: `src/zdecision/sync/contracts.py`
- Modify: `src/zdecision/central/web/reviews.py`
- Modify: `src/zdecision/capture/prompt_contracts/candidate-reconciliation-v1.md`
- Modify: `tests/test_candidate_reconciliation.py`
- Modify: `tests/test_reconciliation_runner.py`
- Modify: `tests/test_capture_request_processor.py`
- Modify: `tests/test_request_state.py`
- Modify: `tests/test_sync_contracts.py`
- Modify: `tests/test_central_api.py`
- Modify: `tests/test_central_client.py`
- Modify: `tests/test_central_candidate_ownership.py`
- Modify: `tests/test_central_web_review.py`

**Central-safe domain value:**

```python
@dataclass(frozen=True)
class CandidateProvenanceSummary:
    protocol: Literal["candidate-provenance-v1"]
    kind: Literal["host_observed_user_prompt_anchor"]
    digest: str
```

Define this value in `capture/provenance.py`; `capture/reconciliation.py` and `sync/contracts.py` import it from there so the Capture domain never depends on the transport module. This object is the complete Central provenance surface. It contains no manifest, receipt, reference-set, Session, Turn, Prompt, path, or excerpt.

- [ ] **Step 1: Write RED reconciliation and transport tests**

Prove:

- only `candidate_eligible` observations reach reconciliation;
- every v5 observation passed to reconciliation has exactly one Candidate sidecar;
- `unrelated`, `refine`, and `replace` copy the triggering observation's unchanged provenance digest into the new revision;
- `same` creates no revision and leaves the observation provenance in its committed local Capture result;
- `ambiguous` uploads nothing;
- `refine` or `replace` targeting a provenance-free legacy family is normalized to ambiguous and uploads nothing;
- a v5 slice batch missing provenance, using an unknown protocol/kind, or changing the digest on replay is rejected;
- an old Candidate revision and old slice/root batch still round-trip without a provenance field;
- the legacy root endpoint rejects a provenance-bearing item instead of accepting a new-protocol payload without a slice declaration;
- the immutable outbox replays the exact provenance after restart and detects a provenance-only conflict;
- Central accepts a complete v1 provenance summary but receives no local identifiers or raw data; and
- Review/edit/preview/publish does not put provenance into formal Decision bytes.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest \
  tests.test_candidate_reconciliation \
  tests.test_reconciliation_runner \
  tests.test_capture_request_processor \
  tests.test_request_state \
  tests.test_sync_contracts \
  tests.test_central_api \
  tests.test_central_client \
  tests.test_central_candidate_ownership \
  tests.test_central_web_review -v
```

Expected: FAIL because revisions, outbox records, and transport items do not retain provenance.

- [ ] **Step 3: Bind provenance to local family revisions**

Add optional `provenance: CandidateProvenanceSummary | None` to `CandidateFamilyRevision`, with dual exact field sets so legacy JSON remains readable.

Pass `candidate_provenance` into `ReconciliationRunner.run()` keyed by Candidate ID. Validate the key set exactly before any model call. Do not render receipt IDs, manifest digests, or provenance details into the reconciliation prompt; the model only compares business content and existing family IDs.

For each eligible observation, construct `CandidateProvenanceSummary(protocol="candidate-provenance-v1", kind="host_observed_user_prompt_anchor", digest=candidate_provenance.provenance_digest)`. This is a one-way minimization boundary: reconciliation and Central never receive the fields hashed inside the private digest.

Extend `apply_reconciliation()` with the validated host provenance map:

- new/unrelated revision: bind the observation's Central-safe kind/digest;
- refine/replace: bind the triggering observation's Central-safe kind/digest;
- same: create no revision;
- ambiguous: create no revision; and
- refine/replace against a current `provenance is None` revision: append the observation to `ambiguous_observation_ids` and do not mutate the family.

Include provenance in `CandidateFamilyRevision.to_dict()`, canonical `ReconciliationResult` persistence, and replay identity. Keep the existing content-derived revision ID for compatibility; the canonical reconciliation-record digest and outbox batch digest bind the provenance digest alongside that ID. The reconciliation model never returns or edits provenance.

- [ ] **Step 4: Add dual-version upload contracts**

Extend `CandidateRevisionUpload` with optional `provenance` and two exact parsers:

- legacy six-field items omit the field and parse as `None`;
- v1 seven-field items require a complete `CandidateProvenanceSummary` object.

Extend `CandidateSliceBatchUpload` with an optional `item_protocol`:

- legacy payloads omit it and require every item to have `provenance is None`;
- new payloads set `item_protocol="candidate-provenance-v1"` and require every item to have valid v1 provenance;
- unknown protocol, mixed legacy/v1 items, missing kind/digest, unknown fields, or malformed digest are rejected; and
- `batch_digest` continues to hash canonical item dictionaries, so provenance changes the immutable batch identity.

Keep `CandidateBatchUpload` readable for the legacy root endpoint, but require all of its items to have `provenance is None`; the old endpoint must reject a v1 item so new clients cannot bypass the slice protocol declaration. New v5 Capture code must emit only the v1 slice form; an already-frozen legacy request may finish only with its original legacy batch form.

- [ ] **Step 5: Preserve provenance through the outbox and Central record**

Update `_candidate_slice_batch()` and both `RequestStateStore` expected-item reconstructions to include the revision provenance and declare the v1 item protocol. Do not add a second outbox table.

The existing Central API parser and `candidate_revisions.record_json` persistence should retain the normalized object automatically. Add assertions rather than a new provenance table. Existing Central query/replay paths must parse both legacy and v1 Candidate revision records.

- [ ] **Step 6: Keep Review and Registry provenance-neutral**

In `central/web/reviews.py`, replace the temporary `CandidateRevisionUpload` construction with zero-filled `evidence_digest`—currently used only to size-check `edit_accept` content—with a dedicated Candidate-content size validator. Do not create fake provenance and do not copy Candidate provenance into Review actions, preview Decision bytes, Registry Decisions, or publication records.

Keep reviewer actions unchanged: accept, reject, edit, preview, and exact **确认发布** remain the only semantic/publication gates.

- [ ] **Step 7: Run GREEN and privacy regressions**

```bash
.venv/bin/python -m unittest \
  tests.test_candidate_reconciliation \
  tests.test_reconciliation_runner \
  tests.test_capture_request_processor \
  tests.test_request_state \
  tests.test_sync_contracts \
  tests.test_central_api \
  tests.test_central_client \
  tests.test_central_candidate_ownership \
  tests.test_central_web_review -v
```

Expected: all tests pass; Central sees only `protocol`, `kind`, and `digest` for new provenance.

- [ ] **Step 8: Commit**

```bash
git add \
  src/zdecision/capture/provenance.py \
  src/zdecision/capture/reconciliation.py \
  src/zdecision/app_server/reconciliation_runner.py \
  src/zdecision/agent/capture_processor.py \
  src/zdecision/agent/request_state.py \
  src/zdecision/sync/contracts.py \
  src/zdecision/central/web/reviews.py \
  src/zdecision/capture/prompt_contracts/candidate-reconciliation-v1.md \
  tests/test_candidate_reconciliation.py \
  tests/test_reconciliation_runner.py \
  tests/test_capture_request_processor.py \
  tests/test_request_state.py \
  tests/test_sync_contracts.py \
  tests/test_central_api.py \
  tests/test_central_client.py \
  tests/test_central_candidate_ownership.py \
  tests/test_central_web_review.py
git commit -m "feat: preserve candidate provenance through review"
```

---

### Task 5: Prove isolation, privacy, legacy compatibility, and the stop boundary

**Files:**
- Create: `tests/test_recall_capture_isolation.py`
- Modify: `tests/integration/test_on_demand_capture_core.py`
- Modify: `tests/integration/test_central_web_vertical.py`
- Modify: `.superpowers/sdd/2026-08-06-recall-host-gate/progress.md`

- [ ] **Step 1: Write the integrated acceptance matrix**

Create a deterministic fake-host corpus that covers every approved design case:

1. recalled Decision or Host Probe context with no qualifying prompt anchor yields zero eligible Candidates;
2. assistant, tool, code, Capture artifact, or compaction summary alone yields zero eligible Candidates;
3. an independently anchored explicit user direction yields one Candidate even when recalled context discusses the same topic;
4. identical text in a recalled envelope and a Hook-observed Prompt is distinguished by source receipt, not keywords;
5. unknown, duplicate, reordered, cross-Session, post-boundary, and forged receipts fail the complete attempt;
6. model-authored `explicit_user_direction` without a valid receipt cannot qualify;
7. Extraction cannot change an Inventory evidence set;
8. reconciliation preserves provenance for new/refine/replace and cannot create it;
9. adoption, `needs_evidence`, and excluded results upload no Candidate content;
10. retry/restart reuses the exact manifest, sidecars, result digest, and outbox bytes;
11. Capture and reconciliation Threads reject Recall activation and Turn gates;
12. recalled rule plus only an unrelated **继续** anchor is `needs_evidence` in the fixed semantic corpus, with a test comment stating this is a model-quality assertion rather than host proof; and
13. no raw source or native IDs cross the Central boundary.

- [ ] **Step 2: Run the new test RED, then make only fixture/wiring corrections**

```bash
.venv/bin/python -m unittest tests.test_recall_capture_isolation -v
```

Expected on the first run: failures identify any missing cross-layer wiring. Fix only provenance-isolation wiring already specified in Tasks 1-4; do not add a new feature or widen Task 8.

- [ ] **Step 3: Run the focused vertical suites**

```bash
.venv/bin/python -m unittest \
  tests.test_recall_capture_isolation \
  tests.integration.test_on_demand_capture_core \
  tests.integration.test_central_web_vertical -v
```

Expected: all tests pass. The Central/Registry sentinel scan must include `session_id`, `turn_id`, `hook_event_id`, `receipt_id`, `active_reference_set_digest`, raw Prompt sentinel, source path sentinel, transcript sentinel, and reference Decision IDs.

- [ ] **Step 4: Run the complete existing test suite once**

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected: all tests pass. Do not start another broad review loop after this run.

- [ ] **Step 5: Apply the hard stop audit**

Confirm from the automated evidence that:

- anchor association uses only the supported Hook event ledger and frozen Stop IDs;
- no fallback reads raw rollout data or transcript filenames;
- evidence-unavailable sources terminate without model calls or retries;
- v5 cannot silently downgrade to v4;
- new Central batches cannot omit provenance; and
- legacy batches remain explicitly legacy rather than receiving invented provenance.

If any statement is false, record `capture_evidence_provenance_unavailable` in the SDD progress file, leave parent Task 7 incomplete, and stop before Task 8. Do not substitute marker filtering.

- [ ] **Step 6: Commit the acceptance evidence and record Task 7 completion**

Update `.superpowers/sdd/2026-08-06-recall-host-gate/progress.md` with the four preceding implementation commit IDs, focused/full test commands, pass counts, and the statement that Task 8 has not started. The fifth commit is this acceptance commit and is reported by the executor after it is created; do not attempt to embed a commit's own hash inside itself.

```bash
git add \
  tests/test_recall_capture_isolation.py \
  tests/integration/test_on_demand_capture_core.py \
  tests/integration/test_central_web_vertical.py \
  .superpowers/sdd/2026-08-06-recall-host-gate/progress.md
git commit -m "test: prove recall capture provenance isolation"
```

## Completion rule

Task 7 is complete only when all five tasks above are committed, the focused vertical suites pass, the complete test suite passes once, the working tree is clean, and the hard stop audit remains false. Then return to Task 8 of `2026-08-06-recall-host-gate.md` for the bounded real Codex Desktop acceptance. Task 8 is not part of this implementation plan.
