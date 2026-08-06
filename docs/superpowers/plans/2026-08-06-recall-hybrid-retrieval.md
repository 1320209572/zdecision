# Local Hybrid Recall Retrieval Gate 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Gate 2's complete, signed `trusted_data_ready` corpus into one benchmark-selected local model runtime, complete immutable per-leaf indexes, an atomically recoverable `recall_ready` generation, and an offline hybrid-retrieval implementation that passes every approved Gate 3 quality, safety, and latency threshold.

**Architecture:** Gate 3 first builds runtime-neutral document, index, retrieval, and evaluation seams with deterministic test doubles. A private company-local bilingual benchmark then compares at least two real embedding candidates and two real reranker candidates on the target Mac; only a passing winner may be frozen into the signed Gate 2 profile and added as the sole production ML runtime. `RecallReadyStore` atomically binds the selected trusted generation to its complete immutable indexes, while `RecallReadyProvider` reopens the exact trusted bundle, index bundle, and model runtime after restart. Retrieval is a pure orchestration call over those four explicit values: `HybridRetriever.retrieve(intent, bundle, indexes, runtime)`.

**Tech Stack:** Python 3.11, `unittest`, SQLite/WAL, standard-library Unicode/path/token processing and exact cosine scoring, the repository's canonical JSON/SHA-256 contracts, the existing read-only `AppServerGateway` for headless benchmark classification, and exactly one production model runtime chosen by Task 8.

## Global Constraints

- This is Gate 3 only. Do not change Hooks, MCP activation/apply tools, Plugin manifests, Session authorization, Decision-envelope injection, Capture prompts, Central query APIs, Registry schemas, or Registry lifecycle behavior.
- Gate 1 owns `zdecision.recall.session.RecallIntent`; import it unchanged. Gate 2 owns `zdecision.recall.contracts` and `zdecision.agent.recall_cache`; consume `RecallCacheStore.readiness(now)`, `trusted_bundle(now)`, and `trusted_bundle_for_generation(generation, now)` unchanged.
- Gate 2 ends at `trusted_data_ready` or a still-valid `trusted_data_degraded` LKG. It supplies a `TrustedRecallBundle`, a signed `RetrievalProfileManifest`, and runtime-neutral content-addressed `ArtifactBinding` values. It does not supply indexes, a loaded model, or recall readiness.
- Gate 2 must retain immutable old generation/profile/artifact bytes while a Gate 3 ready binding can reference that independently valid generation. If this generic retention contract is absent when implementation starts, stop and repair Gate 2 before writing Gate 3 model or index code.
- Registry V1 supplies only complete active revision-1 heads. Parse the existing `DecisionRevision` model, require lifecycle `active`, preserve exact `TrustedDecisionRevision.canonical_json`, and use `scope_summary` as `display_title`. Do not invent revision 2, retirement, supersession, or a Central invalidation record.
- A missing prior tuple in a newer complete ready generation is only the fact Gate 4 will map to `removed_from_active_heads`. Gate 3 must never return a lifecycle-invalid or non-allowed-leaf item.
- Task queries, PRDs, paths, constraints, exclusions, embeddings, candidates, scores, classifications, and benchmark contents remain local. No Gate 3 request sends them to Central.
- The tracked synthetic benchmark validates schemas and mechanics only. The company benchmark, candidate configuration, raw run output, model artifacts, vectors, and per-case diagnostics live below `$ZDECISION_STATE_DIR/benchmarks/recall/<benchmark-digest>/` and never enter Git.
- Embedding and reranking are mandatory. Missing artifacts, tokenizer mismatch, dimension mismatch, load failure, index mismatch, or smoke failure must fail closed; never fall back to keyword-only retrieval.
- Model selection is an empirical hard gate. Before Task 8 freezes a passing winner, do not add a production ML dependency to `pyproject.toml`, do not add a library-specific production import, and do not publish a production profile.
- After selection, add only the winning runtime's minimal bounded dependencies. Do not commit parallel PyTorch, ONNX Runtime, MLX, or alternate serving stacks “just in case.”
- BM25, dense, and path/scope search independently scan the complete hard-filtered allowed corpus. Neither BM25 nor dense may search only the other's candidates.
- Compute the query embedding exactly once per `HybridRetriever.retrieve()` call. Gate 4 owns reuse across the same Intent Epoch; Gate 3 exposes no hidden query cache.
- Candidate depths, fusion weights, BM25 parameters, model token limits, reranker threshold, schema version, and dimension come only from the signed selected `RetrievalProfileManifest`. Prompt data cannot override them.
- `MAX_SHORTLIST_ITEMS = 8`, `MAX_SHORTLIST_UTF8_BYTES = 10_000`, and `PREFINAL_EVALUATION_DEPTH = 20` are reviewed code constants, not profile values. Keep complete canonical revisions; skip an item that does not fit rather than truncating it.
- Preserve all pre-existing worktree changes. Every commit command below stages exact paths and never uses `git add .`.

## Stable Gate 3 Interfaces

Implement and keep these names stable for Gate 4:

```python
# src/zdecision/recall/runtime.py
class EmbeddingRuntime(Protocol):
    @property
    def dimension(self) -> int: ...
    def embed_documents(
        self, texts: Sequence[str]
    ) -> tuple[tuple[float, ...], ...]: ...
    def embed_query(self, text: str) -> tuple[float, ...]: ...

class RerankerRuntime(Protocol):
    def score(
        self, query: str, documents: Sequence[str]
    ) -> tuple[float, ...]: ...

@dataclass(frozen=True)
class ModelRuntimeBundle:
    retrieval_profile_id: str
    retrieval_profile_digest: str
    embedding_model_digest: str
    reranker_model_digest: str
    embedding: EmbeddingRuntime
    reranker: RerankerRuntime
```

```python
# src/zdecision/recall/index_store.py
@dataclass(frozen=True)
class LeafIndexBinding:
    decision_space_id: str
    decision_version: int
    snapshot_digest: str
    document_count: int
    index_digest: str
    absolute_path: Path

@dataclass(frozen=True)
class RecallIndexBundle:
    generation: int
    manifest_digest: str
    retrieval_profile_id: str
    retrieval_profile_digest: str
    index_schema_version: int
    embedding_dimension: int
    leaves: tuple[LeafIndexBinding, ...]
```

`RecallIndexBundle` owns already-open verified readers and exposes bounded `bm25_search()`, `dense_search()`, `path_search()`, and `document()` methods. `HybridRetriever` does not open files, load models, consult stores, or perform network I/O.

```python
# src/zdecision/recall/retrieval.py
@dataclass(frozen=True)
class ShortlistedDecision:
    decision_space_id: str
    decision_id: str
    revision: int
    digest: str
    display_title: str
    source_generation: int
    freshness: Literal["current", "degraded"]
    match_reasons: tuple[str, ...]
    canonical_json: bytes

@dataclass(frozen=True)
class HybridRetrievalResult:
    intent_digest: str
    generation: int
    manifest_digest: str
    retrieval_profile_id: str
    retrieval_profile_digest: str
    candidate_keys_at_20: tuple[tuple[str, str, int, str], ...]
    shortlist: tuple[ShortlistedDecision, ...]
    shortlist_utf8_bytes: int

class HybridRetriever:
    def retrieve(
        self,
        intent: RecallIntent,
        bundle: TrustedRecallBundle,
        indexes: RecallIndexBundle,
        runtime: ModelRuntimeBundle,
    ) -> HybridRetrievalResult: ...
```

```python
# src/zdecision/agent/recall_ready.py
@dataclass(frozen=True)
class RecallReadyContext:
    bundle: TrustedRecallBundle
    indexes: RecallIndexBundle
    runtime: ModelRuntimeBundle

class RecallReadyProvider:
    def resolve(self, now: datetime) -> RecallReadyContext | None: ...
    def resolve_generation(
        self, generation: int, now: datetime
    ) -> RecallReadyContext | None: ...
```

`resolve_generation()` is required for Gate 4's second apply/classification call after process restart. It must reopen the exact persisted generation and fail closed if its signed lease, manifest digest, profile digest, artifacts, indexes, or runtime no longer validate. It must never substitute the current generation.

---

### Task 1: Freeze search documents, query normalization, and the Gate 2 handoff

**Files:**

- Create: `src/zdecision/recall/documents.py`
- Modify: `tests/recall_fixtures.py`
- Create: `tests/fixtures/recall/synthetic-snapshot-v1.json`
- Create: `tests/test_recall_documents.py`

**Consumes:** `RecallIntent`, `TrustedDecisionRevision`, `TrustedLeafSnapshot`, `TrustedRecallBundle`, and the existing `DecisionRevision.from_dict()`.

**Produces:** immutable `DocumentKey`, `SearchDocument`, `QueryDocument`, `build_search_document()`, and `build_query_document()`.

- [ ] **Step 1: Write the failing normalization and integrity tests**

Cover Chinese, English, mixed text, full-width Unicode, case folding, repository-relative POSIX paths, domain objects, constraints, exclusions, canonical-byte preservation, `scope_summary` display titles, wrong digests, wrong product/leaf ownership, absolute paths, `..` traversal, non-active lifecycle, and non-r1 revisions.

```python
intent = RecallIntent(
    target_decision_space_ids=("leaf-ui",),
    explicit_multi_space=False,
    feature_goal="为 VM Detail 添加 IPv6 policy",
    domain_objects=("VmNic",),
    repository_relative_paths=("src/vm/detail/policy.ts",),
    constraints=("must remain offline",),
    exclusions=("legacy wizard",),
)
query = build_query_document(intent)
self.assertIn("ipv6", query.lexical_terms)
self.assertNotIn("legacy", query.positive_lexical_terms)
self.assertEqual(("src/vm/detail/policy.ts",), query.paths)

document = build_search_document(bundle.decisions[0], bundle.leaves[0])
self.assertEqual(document.decision.scope_summary, document.display_title)
self.assertEqual(bundle.decisions[0].canonical_json, document.canonical_json)
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
.venv/bin/python -m unittest tests.test_recall_documents -v
```

Expected: FAIL because `zdecision.recall.documents` does not exist.

- [ ] **Step 3: Implement deterministic document and query construction**

Normalize searchable text with NFKC plus `casefold()`. Emit Unicode word tokens and deterministic CJK unigrams/bigrams. Normalize only repository-relative POSIX paths; reject absolute, empty, `.`/`..`, backslash, and root-escaping forms. Search text includes claim, future action, scope summary, repositories, paths, invalidation conditions, product/leaf display metadata, and domain-object literals. Preserve exclusions in the dense/reranker query but never promote them to positive BM25 terms.

```python
@dataclass(frozen=True, order=True)
class DocumentKey:
    decision_space_id: str
    decision_id: str
    revision: int
    digest: str

@dataclass(frozen=True)
class QueryDocument:
    text: str
    positive_lexical_terms: tuple[str, ...]
    lexical_terms: tuple[str, ...]
    paths: tuple[str, ...]
    domain_objects: tuple[str, ...]
    exclusions: tuple[str, ...]
```

- [ ] **Step 4: Run the focused test and verify GREEN**

```bash
.venv/bin/python -m unittest tests.test_recall_documents -v
```

Expected: PASS with exact canonical bytes unchanged.

- [ ] **Step 5: Commit the document boundary**

```bash
git add src/zdecision/recall/documents.py tests/recall_fixtures.py tests/fixtures/recall/synthetic-snapshot-v1.json tests/test_recall_documents.py
git commit -m "feat(recall): define searchable decision documents"
```

---

### Task 2: Implement independent BM25 and path/scope channels

**Files:**

- Create: `src/zdecision/recall/bm25.py`
- Create: `src/zdecision/recall/path_channel.py`
- Create: `tests/test_recall_channels.py`

**Produces:** deterministic BM25 statistics/scoring and path/scope/domain-object evidence consumed by the immutable index store.

- [ ] **Step 1: Write failing channel tests**

Prove BM25 length normalization and term frequency, CJK bigram matches, exact paths outranking basename matches, ancestor/descendant prefix matches, segment matches, domain-object literal matches, stable tie-breaking by `DocumentKey`, no negative exclusion terms in BM25, and no candidate from a disallowed leaf.

```python
scores = rank_bm25(
    query_terms=("ipv6", "policy"),
    documents=documents,
    k1=1.2,
    b=0.75,
    limit=20,
)
self.assertEqual(ipv6_key, scores[0].key)

evidence = rank_paths(query, documents, limit=20)
self.assertEqual("exact_path", evidence[0].reasons[0])
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_channels -v
```

Expected: FAIL on missing channel modules.

- [ ] **Step 3: Implement the smallest deterministic algorithms**

Use the standard BM25 formula with document frequency over the already hard-filtered corpus. Path scoring order is exact path, ancestor/descendant prefix, basename/segment, then domain-object literal in scope/search text. Return bounded ranked evidence, never raw scores in exceptions or user-facing values.

- [ ] **Step 4: Run GREEN**

```bash
.venv/bin/python -m unittest tests.test_recall_channels -v
```

Expected: PASS, including separate positive results from each channel fixture.

- [ ] **Step 5: Commit the channels**

```bash
git add src/zdecision/recall/bm25.py src/zdecision/recall/path_channel.py tests/test_recall_channels.py
git commit -m "feat(recall): add lexical and path candidate channels"
```

---

### Task 3: Build runtime-neutral dense indexes and immutable index bundles

**Files:**

- Create: `src/zdecision/recall/runtime.py`
- Create: `src/zdecision/recall/index_store.py`
- Modify: `tests/recall_fixtures.py`
- Create: `tests/test_recall_index_store.py`

**Produces:** `EmbeddingRuntime`, `RerankerRuntime`, `ModelRuntimeBundle`, `LeafIndexBinding`, `RecallIndexManifest`, `RecallIndexBuilder`, and `RecallIndexBundle`.

- [ ] **Step 1: Write failing index and fake-runtime tests**

The deterministic fake runtime must be test-only. Cover all enabled leaves including empty leaves, exact document coverage, model/profile/schema/dimension bindings, non-finite/wrong-length vectors, corrupt SQLite bytes, file-digest mutation, duplicate identity, same `(decision_id, revision)` with a different digest, missing document, incremental vector reuse only for identical revision+digest under the same embedding artifact/tokenizer, and exact cosine results over the allowed leaf set.

```python
manifest = RecallIndexBuilder().build(
    bundle=trusted_bundle,
    runtime=fake_runtime,
    target_directory=index_root,
    previous=None,
)
indexes = RecallIndexBundle.open(manifest, trusted_bundle)
self.assertEqual(len(trusted_bundle.leaves), len(indexes.leaves))
self.assertEqual(1, fake_runtime.embedding.document_batches)
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_index_store -v
```

Expected: FAIL because the runtime and index-store modules are missing.

- [ ] **Step 3: Implement immutable per-leaf SQLite indexes**

Use one owner-only SQLite file per leaf beneath a generation/profile staging directory. Create `index_metadata`, `documents`, `bm25_documents`, `bm25_terms`, `bm25_postings`, `dense_vectors`, and `path_postings`. Encode vectors as fixed little-endian float32 bytes, reject non-finite values, and use exact cosine scan initially. Hash and fsync each closed index before returning its binding. The aggregate manifest binds trusted generation/manifest, profile ID/digest, schema, dimension, every leaf snapshot/version/count, and every index digest. Never mutate an activated file.

```python
class RecallIndexBuilder:
    def build(
        self,
        bundle: TrustedRecallBundle,
        runtime: ModelRuntimeBundle,
        target_directory: Path,
        *,
        previous: RecallIndexBundle | None = None,
    ) -> RecallIndexManifest:
        ...
```

- [ ] **Step 4: Run GREEN and the Gate 2 cache regression**

```bash
.venv/bin/python -m unittest tests.test_recall_index_store tests.test_recall_cache -v
```

Expected: PASS; Gate 2 still creates no index and imports no model runtime.

- [ ] **Step 5: Commit runtime-neutral indexing**

```bash
git add src/zdecision/recall/runtime.py src/zdecision/recall/index_store.py tests/recall_fixtures.py tests/test_recall_index_store.py
git commit -m "feat(recall): build immutable hybrid indexes"
```

---

### Task 4: Implement the exact four-argument hybrid retriever

**Files:**

- Create: `src/zdecision/recall/retrieval.py`
- Create: `tests/test_hybrid_retrieval.py`

**Produces:** `ShortlistedDecision`, `HybridRetrievalResult`, and the exact `HybridRetriever.retrieve(intent, bundle, indexes, runtime)` interface.

- [ ] **Step 1: Write failing orchestration and budget tests**

Cover hard leaf filtering before every channel; independent BM25, dense, and path contributions; exactly one query embedding call; bounded per-channel depths; weighted reciprocal-rank fusion from the signed profile; bounded union; identity dedupe; integrity failure for same ID/revision with different digests; reranker input no larger than signed `rerank_depth`; deterministic ties; threshold abstention; zero-item result; top-20 candidate trace; eight-item cap; one shared multi-leaf budget; 10,000-byte complete-item packing; and an oversized Decision being skipped, never truncated.

```python
result = HybridRetriever().retrieve(
    intent,
    trusted_bundle,
    indexes,
    fake_runtime,
)
self.assertEqual(1, fake_runtime.embedding.query_calls)
self.assertLessEqual(len(result.shortlist), 8)
self.assertLessEqual(result.shortlist_utf8_bytes, 10_000)
self.assertTrue(all(item.canonical_json.endswith(b"\n") for item in result.shortlist))
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_hybrid_retrieval -v
```

Expected: FAIL because `zdecision.recall.retrieval` is missing.

- [ ] **Step 3: Implement bounded union, reranking, and shortlist packing**

Set these module constants exactly:

```python
MAX_SHORTLIST_ITEMS = 8
MAX_SHORTLIST_UTF8_BYTES = 10_000
PREFINAL_EVALUATION_DEPTH = 20
```

Validate that bundle, indexes, and runtime bind the same generation/profile/artifact digests before ranking. Build one `QueryDocument`; call `runtime.embedding.embed_query(query.text)` once; ask the three index channels independently; fuse with signed weighted reciprocal rank; dedupe by `(decision_space_id, decision_id, revision, digest)`; rerank at most the signed depth; apply the signed threshold; then pack complete canonical bytes. Match reasons are a bounded closed vocabulary derived from channel evidence, not model prose.

- [ ] **Step 4: Run GREEN**

```bash
.venv/bin/python -m unittest tests.test_hybrid_retrieval tests.test_recall_channels tests.test_recall_index_store -v
```

Expected: PASS with query embedding count exactly one.

- [ ] **Step 5: Commit the retriever**

```bash
git add src/zdecision/recall/retrieval.py tests/test_hybrid_retrieval.py
git commit -m "feat(recall): implement bounded hybrid retrieval"
```

---

### Task 5: Define the versioned benchmark and all launch-gate metrics

**Files:**

- Create: `src/zdecision/recall/benchmark.py`
- Create: `tests/fixtures/recall/synthetic-benchmark-v1.json`
- Create: `tests/test_recall_benchmark.py`

**Produces:** strict `RecallBenchmark`, `BenchmarkCase`, `BenchmarkRunContext`, `BenchmarkObservation`, `RecallMetrics`, `GateAssessment`, and sanitized `BenchmarkReport` values.

- [ ] **Step 1: Write failing schema, metric, and privacy tests**

The tracked fixture must contain synthetic Chinese, English, and mixed queries; product and Shared routes; PRD-like and conversational forms; paths; constraints; hard negatives; cross-product homonyms; ambiguity; conflict; uncertainty; no-match; first/mid activation tags; intent change; and explicit multi-product routing. Test strict fields, stable case IDs, canonical digest, disjoint tuning/acceptance/safety splits, exact gold identities, duplicate rejection, and reports that contain no query, Decision text, paths, vectors, scores, or local artifact paths.

Compute and test every approved metric and threshold:

| Metric | Pass gate |
|---|---:|
| Routing exact match | `>= 0.95` |
| Wrong-leaf retrieval | `0` |
| Ambiguity safety | `1.00` |
| Unnecessary clarification | `<= 0.10` |
| Candidate Recall@20 | `>= 0.95` |
| Pooled Final Precision@8 | `>= 0.90` |
| Final applicable Recall@8 | `>= 0.85` |
| Applicable/conflicting/uncertain macro-F1 | `>= 0.90` |
| Blocking-conflict false negatives | `0` |
| No-match correctness | `1.00` |
| Retired/invalid injection | `0` |
| Warm local retrieval P95 | `<= 800 ms` |
| Same-intent gate P95 | `<= 50 ms` |
| Warm end-to-end added P95 | `<= 3,000 ms` |

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_benchmark -v
```

Expected: FAIL on the missing benchmark module.

- [ ] **Step 3: Implement strict parsing, pooled metrics, nearest-rank P95, and sanitized reports**

Freeze benchmark version/digest, organization generation/manifest digest, requested leaf snapshot digests, catalog version, profile/model/artifact/tokenizer digests, target Mac hardware/OS, warm/cold condition, split, metric definitions, and thresholds in `BenchmarkRunContext`. Empty positive output counts as a recall miss; empty no-match output adds no precision denominator. Compute macro-F1 over exactly applicable/conflicting/uncertain. Reject an acceptance run if any frozen context field differs across candidates.

- [ ] **Step 4: Run GREEN**

```bash
.venv/bin/python -m unittest tests.test_recall_benchmark -v
```

Expected: PASS, including deliberate threshold-boundary and privacy failures.

- [ ] **Step 5: Commit benchmark contracts and the synthetic fixture**

```bash
git add src/zdecision/recall/benchmark.py tests/fixtures/recall/synthetic-benchmark-v1.json tests/test_recall_benchmark.py
git commit -m "test(recall): codify offline quality gates"
```

---

### Task 6: Add the headless routing and applicability evaluation host

**Files:**

- Create: `src/zdecision/recall/evaluation_host.py`
- Create: `tests/test_recall_evaluation_host.py`

**Consumes:** the existing `AppServerGateway`, `FeasibilityModelProfile`, benchmark cases, the exact four-argument retriever, and `RecallReadyContext`-compatible values supplied by the caller.

**Produces:** `HeadlessRecallEvaluator.evaluate_case()` observations; it is benchmark infrastructure, not the Gate 4 production apply path.

- [ ] **Step 1: Write failing fake-gateway tests**

Prove one read-only disposable thread per case, typed `RecallIntent` routing output, bounded shortlist classification into applicable/conflicting/uncertain with blocking flags, archive in `finally`, no Codex UI, no Central call, no raw prompt/report persistence, model-profile receipt validation, and safe failure on malformed output or archive failure.

```python
observation = evaluator.evaluate_case(
    case=benchmark.cases[0],
    bundle=trusted_bundle,
    indexes=indexes,
    runtime=fake_runtime,
    cwd=temporary_repository,
)
self.assertEqual(["start", "route", "classify", "archive"], gateway.events)
self.assertNotIn(benchmark.cases[0].query, observation.to_report_dict().values())
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_evaluation_host -v
```

Expected: FAIL because the evaluation host is missing.

- [ ] **Step 3: Implement the isolated headless adapter**

Use `start_disposable_thread(cwd, profile)`, `run_structured_turn(...)`, and `archive_thread(thread_id)`. The first structured schema must exactly produce Gate 1's `RecallIntent`; the second sees only the bounded shortlist and produces per-key applicability, blocking-conflict, and clarification decisions. Use a separate temporary Agent/evaluation state root. `FeasibilityModelProfile` identifies only the fixed benchmark host model and must never be confused with `RetrievalProfileManifest`.

- [ ] **Step 4: Run GREEN and gateway regressions**

```bash
.venv/bin/python -m unittest tests.test_recall_evaluation_host tests.test_app_server_gateway -v
```

Expected: PASS with every thread archived even after induced failure.

- [ ] **Step 5: Commit the headless evaluator**

```bash
git add src/zdecision/recall/evaluation_host.py tests/test_recall_evaluation_host.py
git commit -m "feat(recall): evaluate routing and applicability headlessly"
```

---

### Task 7: Persist and recover an atomic recall-ready generation

**Files:**

- Create: `src/zdecision/agent/recall_ready.py`
- Create: `tests/test_recall_ready.py`

**Produces:** `RecallReadyBinding`, `RecallReadyState`, `RecallReadyStore`, `RecallReadyContext`, `ModelRuntimeLoader`, `RecallReadyProvider`, and `RecallPreparationService`.

- [ ] **Step 1: Write failing state, restart, and LKG tests**

Cover `trusted_data_ready -> preparing -> recall_ready`; empty-device store; all-leaf completeness; atomic pointer switch; exact generation/manifest/profile/index bindings; `resolve(now)`; `resolve_generation(generation, now)` after constructing a new provider; expired/invalid trusted data; corrupt/missing index; runtime load/smoke failure; a failed generation-2 preparation retaining independently valid generation 1; no partial-leaf activation; and no substitution of current generation when an exact historical generation is requested.

```python
context = restarted_provider.resolve_generation(1, now)
self.assertIsNotNone(context)
self.assertEqual(1, context.bundle.generation)
self.assertEqual(context.bundle.retrieval_profile_digest,
                 context.runtime.retrieval_profile_digest)
```

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_ready -v
```

Expected: FAIL because `zdecision.agent.recall_ready` is missing.

- [ ] **Step 3: Implement one durable ready pointer and a resolving provider**

Store preparation rows and one `active_recall_generation` pointer in SQLite. `RecallPreparationService.prepare()` accepts only a Gate 2 bundle returned in a valid trusted-ready/degraded state, loads and smoke-checks its exact runtime, builds all indexes in a new immutable directory, reopens and verifies them, re-reads the exact Gate 2 generation, then changes the ready pointer in one immediate transaction. Provider resolution rechecks the signed lease and every digest before returning the three values; it never builds, downloads, or repairs inline.

```python
@dataclass(frozen=True)
class RecallReadyContext:
    bundle: TrustedRecallBundle
    indexes: RecallIndexBundle
    runtime: ModelRuntimeBundle

class RecallReadyProvider:
    def resolve(self, now: datetime) -> RecallReadyContext | None: ...
    def resolve_generation(
        self, generation: int, now: datetime
    ) -> RecallReadyContext | None: ...
```

- [ ] **Step 4: Run GREEN with Gate 2 cache tests**

```bash
.venv/bin/python -m unittest tests.test_recall_ready tests.test_recall_cache tests.test_recall_index_store -v
```

Expected: PASS; failed preparation never changes the prior ready pointer.

- [ ] **Step 5: Commit ready-state persistence**

```bash
git add src/zdecision/agent/recall_ready.py tests/test_recall_ready.py
git commit -m "feat(recall): activate complete ready generations"
```

---

### Task 8: Run the private real-model benchmark spike and freeze one winner

**Files:**

- Create: `src/zdecision/recall/benchmark_cli.py`
- Create: `tests/test_recall_benchmark_cli.py`
- Create after the private run: `docs/superpowers/evaluations/2026-08-06-recall-profile-selection.md`
- Modify after the private run: `docs/superpowers/plans/2026-08-06-recall-hybrid-retrieval.md`

**Hard entry condition:** Tasks 1–7 pass with deterministic test doubles, Gate 2 returns a real complete company `TrustedRecallBundle`, and the private benchmark directory is outside Git. No production ML dependency or library-specific production module may exist yet.

- [ ] **Step 1: Write failing CLI boundary and leak-prevention tests**

Require a private candidate matrix with at least two distinct embedding artifact digests and two distinct reranker artifact digests, producing all four pairings on the same benchmark/context. Reject candidate files beneath the repository, a changing benchmark digest/context, an acceptance run before tuning freeze, raw fields in the report, missing license/size/cold/warm data, and any claimed winner that fails one launch gate.

- [ ] **Step 2: Run RED**

```bash
.venv/bin/python -m unittest tests.test_recall_benchmark_cli -v
```

Expected: FAIL because the benchmark CLI is missing.

- [ ] **Step 3: Implement candidate loading only through the benchmark seam**

The private candidate file supplies external adapter factory module paths and artifact paths. Load them only in `benchmark_cli`; they implement the public runtime Protocols but are not imported by production modules. Tune depths/weights/thresholds only on the tuning split. Freeze each candidate profile before the untouched acceptance/safety splits. Reports contain candidate IDs, digests, aggregate metrics, artifact sizes, licenses, cold/warm timings, target hardware/OS, pass/fail, and no case content.

- [ ] **Step 4: Run the tracked synthetic CLI test and commit the harness**

```bash
.venv/bin/python -m unittest tests.test_recall_benchmark_cli tests.test_recall_benchmark -v
git add src/zdecision/recall/benchmark_cli.py tests/test_recall_benchmark_cli.py
git commit -m "test(recall): add private model selection harness"
```

Expected: PASS without importing any candidate runtime into normal package startup.

- [ ] **Step 5: Run the real 2-by-2 private spike on the target Mac**

```bash
export ZDECISION_STATE_DIR=/absolute/company/zdecision-state
.venv/bin/python -m zdecision.recall.benchmark_cli select \
  --benchmark "$ZDECISION_STATE_DIR/benchmarks/recall/frozen/benchmark.json" \
  --candidates "$ZDECISION_STATE_DIR/benchmarks/recall/frozen/candidates.json" \
  --output "$ZDECISION_STATE_DIR/benchmarks/recall/frozen/runs/model-selection"
```

Selection is deterministic among candidates passing every hard gate: zero safety violations first, then higher Final Precision@8, higher Final applicable Recall@8, lower warm local P95, smaller total artifact bytes, and finally lexical candidate ID. Keep raw output private.

**Stop condition:** If the benchmark is unavailable, fewer than two embedding or two reranker candidates run, context digests differ, or no candidate passes every gate, stop Gate 3 here. Do not edit `pyproject.toml`, add a production runtime, publish a profile, build production indexes, or weaken a threshold. Improve only retrieval/index/profile behavior and rerun the same frozen benchmark.

- [ ] **Step 6: Freeze sanitized evidence and amend Task 9 before continuing**

Create the evaluation document with only frozen benchmark/context digests, candidate IDs/digests, aggregate metrics, sizes/licenses, deterministic selection result, and the exact winning backend family, dependency constraints, Python imports, model/tokenizer IDs/revisions/digests, dimension, schema, depths, weights, thresholds, and artifact media types. Add the same exact dependency/import/load recipe to Task 9 of this plan. Verify neither file contains private queries, Decision text, source paths, vectors, scores, or artifact paths.

```bash
git diff --check -- docs/superpowers/evaluations/2026-08-06-recall-profile-selection.md docs/superpowers/plans/2026-08-06-recall-hybrid-retrieval.md
git add docs/superpowers/evaluations/2026-08-06-recall-profile-selection.md docs/superpowers/plans/2026-08-06-recall-hybrid-retrieval.md
git commit -m "docs(recall): freeze benchmark-selected runtime"
```

**Mandatory review checkpoint:** A human or reviewing agent must verify the sanitized evidence against the private signed run and confirm Task 9 now contains exact bounded dependencies and runnable imports. The unamended generic Task 9 below is intentionally non-executable; this prevents a pre-benchmark runtime choice.

- [ ] **Step 7: Publish only the frozen winner through the Gate 2 contract**

Write the winner's canonical `RetrievalProfileManifest` and four digest-matching model/tokenizer artifacts to the private Central `RecallProfilePack` location, load them with `RecallProfilePack.load(profile_path, artifact_root)`, and call Gate 2's `RecallDistributionPublisher.publish_available()` for the available verified projection. Synchronize the clean Agent until `RecallCacheStore.readiness(now).state == "trusted_data_ready"`, then require the returned `TrustedRecallBundle.retrieval_profile_digest` and all four `ArtifactBinding` digests to equal the frozen selection record. Keep the profile/artifacts out of Git. Stop on any mismatch; Task 9 must not compensate for an unsigned or differently packaged model.

---

### Task 9: Add only the benchmark-selected production runtime

**Files:**

- Modify: `pyproject.toml`
- Create: `src/zdecision/recall/production_runtime.py`
- Create: `tests/test_recall_production_runtime.py`

**Entry condition:** Task 8 has amended this task with the exact winning dependency constraints, imports, artifact loaders, tokenizer settings, execution provider, threading limits, and expected smoke vectors. If those exact details are absent, stop; do not infer them from model popularity or developer preference.

- [ ] **Step 1: Add the amended winner-specific failing tests before dependencies**

Tests must load the four exact digest-bound artifacts from a temporary content-addressed pack, validate tokenizer/model revision and embedding dimension, exercise multilingual embedding and reranking smoke cases, prove deterministic bounded output and normalized finite scores, reject swapped/corrupt artifacts, and prove no alternate runtime package is imported.

- [ ] **Step 2: Run the winner-specific test and verify RED for the recorded reason**

```bash
.venv/bin/python -m unittest tests.test_recall_production_runtime -v
```

Expected: FAIL only because the selected dependency/module has not been added.

- [ ] **Step 3: Add the exact selected dependencies and minimal loader from the amended record**

Implement this stable public factory while keeping all library-specific imports inside this module:

```python
def load_production_runtime(
    profile: RetrievalProfileManifest,
    artifacts: tuple[ArtifactBinding, ...],
) -> ModelRuntimeBundle:
    """Load only the benchmark-selected, digest-bound local runtime."""
```

Reject any profile/backend combination other than the frozen selected family. Configure no network download and no remote-code execution; load only verified local artifact paths supplied by Gate 2.

- [ ] **Step 4: Run GREEN and audit the dependency graph**

```bash
.venv/bin/python -m unittest tests.test_recall_production_runtime tests.test_recall_index_store tests.test_hybrid_retrieval -v
.venv/bin/python -m pip check
```

Expected: PASS with exactly the selected runtime family present and no test-double fallback.

- [ ] **Step 5: Commit the sole production runtime**

```bash
git add pyproject.toml src/zdecision/recall/production_runtime.py tests/test_recall_production_runtime.py
git commit -m "feat(recall): load benchmark-selected local models"
```

---

### Task 10: Prove clean-device production activation and the frozen offline Gate

**Files:**

- Create: `tests/integration/test_recall_ready_onboarding.py`
- Create: `tests/integration/test_recall_offline_quality.py`
- Modify: `src/zdecision/agent/service.py`
- Modify: `tests/test_agent_service.py`
- Modify: `README.md`

**Scope:** Background Agent preparation only. Do not wire Prompt, Hook, MCP, or Gate 4 classification/injection behavior.

- [ ] **Step 1: Write the clean-device production activation test and verify RED**

Start from a real Gate 2 `trusted_data_ready` cache and an empty Gate 3 ready/index directory. Use the selected production profile/artifacts, not the deterministic fixture backend. Assert artifact validation, real runtime load, all enabled-leaf index construction including empty leaves, exact Decision coverage, query smoke, one ready-pointer switch, restart recovery, and the exact four-argument retrieval call.

```bash
.venv/bin/python -m unittest tests.integration.test_recall_ready_onboarding -v
```

Expected: FAIL because the background Agent does not yet run `RecallPreparationService`.

- [ ] **Step 2: Wire background preparation after Gate 2 synchronization**

When `RecallSynchronizer` returns a new `trusted_data_ready` generation, schedule local preparation through `RecallPreparationService`. Never prepare inline in a Hook/MCP/Prompt request. A failed new profile/index records a sanitized local error and retains the prior independently valid ready binding until its own signed expiry. A successful build atomically changes only the Gate 3 ready pointer.

- [ ] **Step 3: Run the clean-device activation test and verify GREEN**

```bash
.venv/bin/python -m unittest tests.integration.test_recall_ready_onboarding tests.test_agent_service -v
```

Expected: PASS from empty Gate 3 state with no seeded index or fake backend.

- [ ] **Step 4: Run the untouched private acceptance/safety benchmark without Codex UI**

```bash
export ZDECISION_STATE_DIR=/absolute/company/zdecision-state
.venv/bin/python -m zdecision.recall.benchmark_cli accept \
  --benchmark "$ZDECISION_STATE_DIR/benchmarks/recall/frozen/benchmark.json" \
  --ready-state "$ZDECISION_STATE_DIR/agent/recall-ready.sqlite3" \
  --output "$ZDECISION_STATE_DIR/benchmarks/recall/frozen/runs/final-gate"
```

Expected: every metric in Task 5 passes on the frozen generation/profile/hardware context. `tests/integration/test_recall_offline_quality.py` consumes only the sanitized aggregate result and matching digests; it must not copy private cases into Git.

**Stop condition:** Any routing, wrong-leaf, ambiguity, clarification, retrieval, applicability, conflict, no-match, lifecycle, or latency miss keeps Gate 3 open. Change only retrieval/index/profile behavior, rerun model selection if the frozen profile changes, republish the new winner through Gate 2, rebuild a complete generation, and rerun the untouched acceptance/safety split. Do not expand injection, add online search, or weaken gates.

- [ ] **Step 5: Run focused and full verification**

```bash
.venv/bin/python -m unittest tests.test_recall_documents tests.test_recall_channels tests.test_recall_index_store tests.test_hybrid_retrieval tests.test_recall_benchmark tests.test_recall_evaluation_host tests.test_recall_ready tests.test_recall_production_runtime tests.integration.test_recall_ready_onboarding tests.integration.test_recall_offline_quality -v
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
git diff --check
git status --short
```

Expected: all tests pass; aggregate evidence matches the frozen private report; no benchmark/model/index bytes are tracked; status contains only exact Gate 3 paths plus preserved pre-existing user changes.

- [ ] **Step 6: Document and commit Gate 3 acceptance**

README must state that Gate 3 owns the selected local model pack, complete indexes, `recall_ready` pointer, offline retrieval quality, and the four-argument retriever, while Gate 4 still owns production applicability/injection and same-intent Session behavior.

```bash
git add src/zdecision/agent/service.py tests/test_agent_service.py tests/integration/test_recall_ready_onboarding.py tests/integration/test_recall_offline_quality.py README.md
git commit -m "test(recall): prove production offline retrieval gate"
```

---

## Gate 3 Completion Evidence

Do not report Gate 3 complete until the implementation handoff includes:

| Claim | Required evidence |
|---|---|
| Gate 2 boundary | Real `TrustedRecallBundle` observed only after `trusted_data_ready`; no Gate 2 index/runtime claim |
| Selection order | Timestamped/digested private 2-by-2 spike predates production dependency/runtime commit |
| Unique runtime | One selected backend/profile and minimal dependency graph; no alternate production fallback |
| Clean device | Empty Gate 3 state builds every enabled-leaf index from verified Gate 2 artifacts and atomically reaches `recall_ready` |
| Integrity | Profile/artifact/tokenizer/dimension/schema/coverage/index/query-smoke mismatch cases fail closed |
| LKG | Failed upgrade leaves only a still-valid prior ready generation resolvable |
| Restart | `resolve_generation()` reopens the exact generation/profile/index/runtime and never substitutes current state |
| Hybrid semantics | Independent BM25/dense/path channels, bounded union/dedupe/rerank/threshold, one query embedding, complete-item caps |
| Offline quality | All Task 5 routing, relevance, applicability, lifecycle, and latency gates pass on the untouched private split |
| Privacy | Git and Central observations contain no private benchmark/query/Decision/path/vector/candidate/score data |
| Gate 4 handoff | Exact four-argument `HybridRetriever.retrieve(intent, bundle, indexes, runtime)` and bounded complete `shortlist` are stable |

The final implementation report must cite exact test commands, the sanitized profile-selection evidence, the frozen benchmark/profile/manifest/snapshot digests, and the final aggregate Gate assessment. Private benchmark contents and raw per-case output remain in the company-local state directory.
