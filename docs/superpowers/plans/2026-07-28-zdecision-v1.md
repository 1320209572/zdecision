# ZDecision V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the complete Codex-native path from a completed source task, through private review and confirmed Git publication, to a new Codex task carrying only applicable formal decisions.

**Architecture:** The repository Skill is the conversation and app-server gateway: it uses the Codex App's native task tools, which are backed by `thread/read`, `thread/fork`, `turn/start`, `thread/start`, `thread/resume`, and `turn/steer`. Python code owns deterministic validation, user-local private state, formal Registry mutations, applicability records, and bounded Context Packs. V1 must not spawn a second conversation runtime, run a coordinator, or persist raw task content.

**Tech Stack:** Python 3.11 standard library, `argparse`, dataclasses, canonical JSON, atomic filesystem writes, Git subprocesses, `unittest`, and one repository Skill under `.agents/skills/zdecision/`.

## Global Constraints

- `docs/architecture.md` is the only product and architecture authority.
- Work directly on `main`; V1 uses the same repository and `decision-registry/`, with no Registry branch.
- The user experience is natural-language interaction in Codex App; every CLI command below is an internal machine boundary used by the Skill.
- Use Codex App native task tools for task lifecycle. Do not launch another `codex app-server`, daemon, worker scheduler, or local conversation runtime.
- A Candidate is private and editable. Only a reviewed Candidate plus an exact final confirmation can produce a formal Decision.
- Never write raw task text, extraction transcripts, Candidate payloads, review records, workspace snapshots, credentials, or secrets under `decision-registry/`.
- Private records live outside the checkout. Resolve the root from `ZDECISION_STATE_DIR` when set; otherwise use `~/Library/Application Support/ZDecision` on macOS, `$XDG_STATE_HOME/zdecision` or `~/.local/state/zdecision` on Linux, and `%LOCALAPPDATA%\ZDecision` on Windows.
- All private writes use canonical UTF-8 JSON plus temporary-file-and-rename atomic replacement. V1 assumes one local user and adds no distributed locking.
- Internal commands emit one JSON object on stdout. Diagnostics go to stderr. Exit codes are `0` success, `2` invalid input, `3` unavailable external state, `4` explicit user action required, and `5` ambiguous external result requiring reconciliation.
- Stable operation identities cover Capture, publication, and new-task creation. A retry returns the stored result; an unattached external result stops with exit `5` instead of creating a replacement.
- Use no runtime Python dependencies outside the standard library.
- Unit and integration tests use `unittest`; run them through the repository `.venv`.

## File Map

```text
.agents/skills/zdecision/
  SKILL.md                         intent routing and user-visible workflow
  references/capture.md           native task-tool Capture sequence
  references/review-publish.md    review, preview, confirmation, publication
  references/preflight-use.md     applicability, Context Pack, task creation
src/zdecision/
  __main__.py                      `python -m zdecision`
  cli.py                           internal JSON command boundary
  jsonio.py                        canonical JSON and atomic writes
  ids.py                           deterministic operation and object ids
  app_server/contracts.py         typed host action/result records only
  capture/models.py               SourceCheckpoint, Candidate, CandidateSet
  capture/prompts.py              strict extraction prompt and result schema
  capture/service.py              Capture prepare/attach/complete/replay
  private_store/filesystem.py     private V1 object repository
  registry/models.py              DecisionRevision and RegistryManifest
  registry/codec.py               validation and canonical Registry encoding
  registry/git_repository.py      scoped main-branch fetch/commit/push
  registry/promotion.py           review, exact preview, confirmed promotion
  preflight/models.py             workspace, Registry, assessment, pack records
  preflight/registry_reader.py    fresh/stale/unavailable Registry reads
  preflight/applicability.py      assessment validation and classification
  preflight/context.py            complete-item bounded Context Pack assembly
  preflight/service.py            Preflight and task-usage operation state
tests/
  __init__.py
  fakes.py
  test_capture.py
  test_cli_capture.py
  test_review_publish.py
  test_git_registry.py
  test_preflight.py
  test_context_pack.py
  test_skill_contract.py
  test_v1_flow.py
```

The internal CLI envelope is stable across all three slices:

```python
{"ok": True, "kind": "capture.completed", "data": {"operation_id": "cap_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "candidate_ids": []}}
{"ok": False, "error": {"code": "registry_unavailable", "message": "Cannot fetch origin/main and no validated cached Registry exists.", "details": {"remote": "origin", "branch": "main"}}}
```

The private store never accepts an untyped conversation dump. Its public methods accept only the domain records named in the architecture.

---

### Task 1: Capture — Completed Source Boundary to Private Candidates

**Files:**

- Create: `src/zdecision/jsonio.py`
- Create: `src/zdecision/ids.py`
- Create: `src/zdecision/app_server/__init__.py`
- Create: `src/zdecision/app_server/contracts.py`
- Create: `src/zdecision/capture/__init__.py`
- Create: `src/zdecision/capture/models.py`
- Create: `src/zdecision/capture/prompts.py`
- Create: `src/zdecision/capture/service.py`
- Create: `src/zdecision/private_store/__init__.py`
- Create: `src/zdecision/private_store/filesystem.py`
- Create: `src/zdecision/cli.py`
- Create: `src/zdecision/__main__.py`
- Create: `.agents/skills/zdecision/SKILL.md`
- Create: `.agents/skills/zdecision/references/capture.md`
- Create: `tests/fakes.py`
- Create: `tests/__init__.py`
- Create: `tests/test_capture.py`
- Create: `tests/test_cli_capture.py`
- Create: `tests/test_skill_contract.py`
- Modify: `pyproject.toml`

**Interfaces:**

- Consumes: a source task id, one completed source Turn id selected by Codex App `read_thread`, product, a forked Capture task id, and one structured extraction result.
- Produces: `CaptureService.prepare(source_thread_id: str, source_turn_id: str, product: str) -> CapturePlan`, `CaptureService.attach_fork(operation_id: str, fork_thread_id: str) -> CaptureRecord`, `CaptureService.complete(operation_id: str, extraction: Mapping[str, object]) -> CandidateSet`, and `CaptureService.get(operation_id: str) -> CaptureRecord`.
- Produces CLI commands: `zdecision capture prepare`, `zdecision capture attach`, `zdecision capture complete`, and `zdecision capture show`.
- Produces a deterministic operation id `cap_<32 lowercase hex>` from `source_thread_id + "\n" + source_turn_id + "\n" + product + "\nextractor-v1"`.

- [ ] **Step 1: Bootstrap the editable environment and write failing model/store tests**

Add these assertions to `tests/test_capture.py`:

```python
class CaptureModelTests(unittest.TestCase):
    def test_operation_id_is_stable_and_input_sensitive(self):
        first = capture_operation_id("thread-a", "turn-7", "anheng")
        self.assertEqual(first, capture_operation_id("thread-a", "turn-7", "anheng"))
        self.assertNotEqual(first, capture_operation_id("thread-a", "turn-8", "anheng"))
        self.assertRegex(first, r"^cap_[0-9a-f]{32}$")

    def test_private_store_round_trips_typed_capture_without_raw_text(self):
        store = FilePrivateStore(self.state_dir)
        record = CaptureRecord.started(
            operation_id="cap_" + "a" * 32,
            source=SourceCheckpoint("thread-a", "turn-7"),
            product="anheng",
        )
        store.put_capture(record)
        loaded = store.get_capture(record.operation_id)
        self.assertEqual(record, loaded)
        serialized = (self.state_dir / "captures" / f"{record.operation_id}.json").read_text()
        self.assertNotIn("raw_messages", serialized)
        self.assertNotIn("transcript", serialized)
```

Run:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest tests.test_capture.CaptureModelTests -v
```

Expected: FAIL because the capture types and store do not exist.

- [ ] **Step 2: Implement canonical JSON, ids, typed records, and the private filesystem store**

Use these exact public shapes:

```python
@dataclass(frozen=True)
class SourceCheckpoint:
    thread_id: str
    turn_id: str

@dataclass(frozen=True)
class CandidateContent:
    product: str
    claim: str
    future_action: str
    scope_summary: str
    repositories: tuple[str, ...]
    paths: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]

@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    capture_id: str
    ordinal: int
    content: CandidateContent
    source: SourceCheckpoint

@dataclass(frozen=True)
class CaptureRecord:
    operation_id: str
    source: SourceCheckpoint
    product: str
    status: Literal["prepared", "fork_attached", "completed", "failed"]
    fork_thread_id: str | None
    candidate_ids: tuple[str, ...]
    created_at: str
    updated_at: str

    @classmethod
    def started(cls, operation_id: str, source: SourceCheckpoint, product: str) -> "CaptureRecord":
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return cls(operation_id, source, product, "prepared", None, (), now, now)

@dataclass(frozen=True)
class CandidateSet:
    operation_id: str
    status: Literal["completed"]
    candidate_ids: tuple[str, ...]

@dataclass(frozen=True)
class CapturePlan:
    record: CaptureRecord
    extraction_prompt: str
    replayed: bool
```

Every persisted dataclass exposes `to_dict()` and a strict `from_dict()` that rejects unknown keys. `canonical_json_bytes(value)` must call `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)` and append one newline. `atomic_write_json(path, value)` must create its temporary file in `path.parent`, `flush`, `os.fsync`, then `os.replace` it. `FilePrivateStore` exposes explicit `get_*` and `put_*` methods; it must reject a path component containing `/`, `\\`, `..`, or a NUL byte.

Run:

```bash
.venv/bin/python -m unittest tests.test_capture.CaptureModelTests -v
```

Expected: PASS.

- [ ] **Step 3: Write failing Capture prepare/attach/complete/retry tests**

Add these behaviors to `tests/test_capture.py`:

```python
class CaptureServiceTests(unittest.TestCase):
    def test_prepare_returns_prompt_and_does_not_store_source_text(self):
        plan = self.service.prepare("thread-a", "turn-7", "anheng")
        self.assertEqual("prepared", plan.record.status)
        self.assertIn('"candidates"', plan.extraction_prompt)
        self.assertNotIn("source_text", plan.record.to_dict())

    def test_complete_requires_attached_fork(self):
        plan = self.service.prepare("thread-a", "turn-7", "anheng")
        with self.assertRaises(CaptureStateError):
            self.service.complete(plan.record.operation_id, {"candidates": []})

    def test_prepare_retry_before_fork_attach_is_ambiguous(self):
        self.service.prepare("thread-a", "turn-7", "anheng")
        with self.assertRaises(CaptureForkAmbiguous):
            self.service.prepare("thread-a", "turn-7", "anheng")

    def test_attach_is_idempotent_only_for_the_same_fork(self):
        plan = self.service.prepare("thread-a", "turn-7", "anheng")
        self.service.attach_fork(plan.record.operation_id, "thread-fork")
        replay = self.service.attach_fork(plan.record.operation_id, "thread-fork")
        self.assertEqual("thread-fork", replay.fork_thread_id)
        with self.assertRaises(CaptureForkConflict):
            self.service.attach_fork(plan.record.operation_id, "different-fork")

    def test_zero_candidates_is_a_completed_result_and_replays(self):
        plan = self.service.prepare("thread-a", "turn-7", "anheng")
        self.service.attach_fork(plan.record.operation_id, "thread-fork")
        result = self.service.complete(plan.record.operation_id, {"candidates": []})
        replay = self.service.prepare("thread-a", "turn-7", "anheng")
        self.assertEqual("completed", result.status)
        self.assertEqual(result.operation_id, replay.record.operation_id)
        self.assertTrue(replay.replayed)

    def test_candidate_ids_follow_validated_result_order(self):
        plan = self.service.prepare("thread-a", "turn-7", "anheng")
        self.service.attach_fork(plan.record.operation_id, "thread-fork")
        result = self.service.complete(plan.record.operation_id, extraction_with_two_candidates())
        self.assertEqual(
            (f"cand_{plan.record.operation_id[4:]}_01", f"cand_{plan.record.operation_id[4:]}_02"),
            result.candidate_ids,
        )

    def test_extraction_rejects_unknown_fields_and_raw_evidence(self):
        plan = self.service.prepare("thread-a", "turn-7", "anheng")
        self.service.attach_fork(plan.record.operation_id, "thread-fork")
        with self.assertRaises(ExtractionValidationError):
            self.service.complete(plan.record.operation_id, {
                "candidates": [{**valid_candidate(), "evidence_quote": "private text"}]
            })
```

Run:

```bash
.venv/bin/python -m unittest tests.test_capture.CaptureServiceTests -v
```

Expected: FAIL because `CaptureService` and strict extraction validation do not exist.

- [ ] **Step 4: Implement CaptureService and its strict extraction contract**

`capture/prompts.py` must expose `EXTRACTOR_VERSION = "extractor-v1"`, `EXTRACTION_SCHEMA`, and `build_extraction_prompt(product: str)`. The accepted model output is exactly:

```json
{
  "candidates": [
    {
      "product": "anheng",
      "claim": "A concise confirmed decision",
      "future_action": "What future work must do",
      "scope": {
        "summary": "Where the decision applies",
        "repositories": ["optional canonical remote"],
        "paths": ["optional/path"]
      },
      "invalidation_conditions": ["Condition that requires review"]
    }
  ]
}
```

Reject unknown keys, empty `product`, `claim`, `future_action`, or `scope.summary`, non-string list members, more than 20 Candidates, and any encoded Candidate over 16 KiB. The prompt must say that zero Candidates is valid, only confirmed decisions qualify, raw quotations and conversation summaries are forbidden, and the response must contain JSON only.

`prepare` returns the existing completed record on retry and returns the attached fork for a `fork_attached` record. If an existing record is still `prepared`, raise `CaptureForkAmbiguous` because a fork may have succeeded before attachment; the Skill must reconcile the actual fork instead of creating another. `attach_fork` replays only when the same fork id is supplied. `complete` validates the whole result before writing any Candidate and writes the Candidate files before atomically marking the Capture completed.

Run:

```bash
.venv/bin/python -m unittest tests.test_capture.CaptureServiceTests -v
```

Expected: PASS.

- [ ] **Step 5: Write failing internal CLI tests**

In `tests/test_cli_capture.py`, invoke `main(argv, stdin, stdout, stderr, environ)` directly and assert:

```python
def test_capture_prepare_emits_machine_envelope(self):
    code, payload = run_cli(
        ["capture", "prepare", "--thread-id", "thread-a", "--turn-id", "turn-7", "--product", "anheng"],
        state_dir=self.state_dir,
    )
    self.assertEqual(0, code)
    self.assertEqual("capture.prepared", payload["kind"])
    self.assertTrue(payload["data"]["extraction_prompt"])

def test_capture_complete_reads_json_from_stdin(self):
    operation_id = prepare_and_attach(self.state_dir)
    code, payload = run_cli(
        ["capture", "complete", "--operation-id", operation_id, "--input", "-"],
        stdin=json.dumps({"candidates": []}),
        state_dir=self.state_dir,
    )
    self.assertEqual(0, code)
    self.assertEqual("capture.completed", payload["kind"])

def test_invalid_json_uses_exit_2_and_no_traceback_on_stdout(self):
    code, payload = run_cli(["capture", "complete", "--operation-id", "cap_bad", "--input", "-"], stdin="{")
    self.assertEqual(2, code)
    self.assertEqual("invalid_json", payload["error"]["code"])
```

Run:

```bash
.venv/bin/python -m unittest tests.test_cli_capture -v
```

Expected: FAIL because the internal CLI does not exist.

- [ ] **Step 6: Implement the internal CLI and console entry point**

Add to `pyproject.toml`:

```toml
[project.scripts]
zdecision = "zdecision.cli:main"
```

`main` must accept injectable streams/environment for tests, read `--input -` from stdin, and turn domain exceptions into the fixed exit codes. `python -m zdecision` must call the same `main()`.

Run:

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest tests.test_cli_capture -v
```

Expected: PASS.

- [ ] **Step 7: Add the Capture section of the repository Skill and its contract test**

The root `SKILL.md` front matter is:

```yaml
---
name: zdecision
description: Capture confirmed decisions from an existing Codex task, review and publish them to the ZDecision Registry, or start a new Codex task with applicable formal decisions.
---
```

`references/capture.md` must instruct Codex to perform this exact sequence:

1. Call the native task reader and page until the latest completed source Turn is identified. Never use an active Turn as the checkpoint.
   Missing source data or a missing completed Turn stops Capture; do not infer or reconstruct it.
2. Run `zdecision capture prepare` with the source task id, completed Turn id, and product.
3. If the result is replayed and completed, show the stored Candidate list and do not fork.
4. Call the native same-directory fork tool on the source task. Its active unfinished Turn is intentionally excluded.
5. Immediately run `zdecision capture attach` with the returned fork task id. If the fork result is unknown, stop with an ambiguous-result message.
6. Send the returned extraction prompt to the fork, wait for completion, and pass only its final JSON object to `zdecision capture complete`.
7. Show Candidate fields for review. Do not label them Decisions and do not publish.

`tests/test_skill_contract.py` must assert that the Capture reference contains all of `thread/read`, `thread/fork`, `turn/start`, `completed Turn`, `capture attach`, and `zero Candidates`, and does not contain `codex app-server`, `Coordinator`, or an instruction to store a transcript.

Run:

```bash
.venv/bin/python -m unittest tests.test_skill_contract tests.test_capture tests.test_cli_capture -v
```

Expected: PASS.

- [ ] **Step 8: Commit the demonstrable Capture slice**

Run:

```bash
git add pyproject.toml src/zdecision .agents/skills/zdecision tests/__init__.py tests/test_capture.py tests/test_cli_capture.py tests/test_skill_contract.py tests/fakes.py
git commit -m "feat: add private decision capture flow"
git push origin main
```

Acceptance: From a Codex conversation, a completed source task produces validated private Candidates or an explicit zero-Candidate result; retrying the same checkpoint does not fork again after completion.

---

### Task 2: Review and Publish — Accepted Candidate to Confirmed Git Decision

**Files:**

- Create: `src/zdecision/registry/__init__.py`
- Create: `src/zdecision/registry/models.py`
- Create: `src/zdecision/registry/codec.py`
- Create: `src/zdecision/registry/git_repository.py`
- Create: `src/zdecision/registry/promotion.py`
- Create: `.agents/skills/zdecision/references/review-publish.md`
- Create: `tests/test_review_publish.py`
- Create: `tests/test_git_registry.py`
- Modify: `src/zdecision/private_store/filesystem.py`
- Modify: `src/zdecision/cli.py`
- Modify: `.agents/skills/zdecision/SKILL.md`
- Modify: `decision-registry/README.md`
- Modify: `decision-registry/registry.json`
- Modify: `tests/test_skill_contract.py`

**Interfaces:**

- Consumes: one stored Candidate, an append-only user review, the controller task/Turn ids, and an exact confirmation token for one stored publication preview.
- Produces: `PromotionService.review(candidate_id, action, patch, approval) -> CandidateReview`, `PromotionService.preview(candidate_id) -> PublicationPreview`, and `PromotionService.confirm(preview_id, confirmation_token) -> PublicationResult`.
- Produces CLI commands: `zdecision review record`, `zdecision publish preview`, and `zdecision publish confirm`.
- Produces Registry files `decision-registry/decisions/<decision-id>/r0001.json` and a sorted manifest entry in `decision-registry/registry.json`.

- [ ] **Step 1: Write failing review and formal-model tests**

Add to `tests/test_review_publish.py`:

```python
def test_review_is_append_only_and_candidate_source_is_immutable(self):
    first = self.service.review(
        self.candidate_id,
        action="edit_accept",
        patch={"claim": "Narrowed claim"},
        approval=self.approval("controller-turn-1"),
    )
    second = self.service.review(
        self.candidate_id,
        action="reject",
        patch={},
        approval=self.approval("controller-turn-2"),
    )
    self.assertNotEqual(first.review_id, second.review_id)
    self.assertEqual(self.source_checkpoint, self.store.get_candidate(self.candidate_id).source)
    self.assertEqual([first, second], self.store.list_reviews(self.candidate_id))

def test_preview_requires_latest_review_to_be_accepted(self):
    self.service.review(self.candidate_id, "reject", {}, self.approval("turn-1"))
    with self.assertRaises(CandidateNotAccepted):
        self.service.preview(self.candidate_id)

def test_preview_is_exact_stable_and_does_not_touch_git(self):
    self.service.review(self.candidate_id, "accept", {}, self.approval("turn-1"))
    preview = self.service.preview(self.candidate_id)
    replay = self.service.preview(self.candidate_id)
    self.assertEqual(preview.preview_id, replay.preview_id)
    self.assertEqual(preview.decision_bytes, replay.decision_bytes)
    self.assertEqual(hashlib.sha256(preview.decision_bytes).hexdigest(), preview.content_sha256)
    self.assertFalse((self.repo / "decision-registry" / "decisions").exists())
```

The allowed review actions are exactly `accept`, `edit_accept`, `reject`, and `skip`. A patch may change `product`, `claim`, `future_action`, `scope_summary`, `repositories`, `paths`, `invalidation_conditions`, `supersedes`, or `variant_of`; it may not change Candidate id, Capture id, source task id, or source Turn id.

Run:

```bash
.venv/bin/python -m unittest tests.test_review_publish -v
```

Expected: FAIL because review and formal Decision models do not exist.

- [ ] **Step 2: Implement append-only reviews and exact publication previews**

Use these formal records:

```python
@dataclass(frozen=True)
class ApprovalRef:
    actor: str
    reviewed_at: str
    controller_thread_id: str
    controller_turn_id: str

@dataclass(frozen=True)
class DecisionRevision:
    decision_id: str
    product: str
    revision: int
    claim: str
    future_action: str
    scope_summary: str
    repositories: tuple[str, ...]
    paths: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    lifecycle: Literal["active", "superseded", "retired"]
    supersedes: tuple[str, ...]
    variant_of: tuple[str, ...]
    source: SourceCheckpoint
    approval: ApprovalRef

@dataclass(frozen=True)
class CandidateReview:
    review_id: str
    candidate_id: str
    action: Literal["accept", "edit_accept", "reject", "skip"]
    reviewed_content: CandidateContent | None
    approval: ApprovalRef

@dataclass(frozen=True)
class PublicationPreview:
    preview_id: str
    candidate_id: str
    review_id: str
    decision_id: str
    revision: int
    decision_json: str
    manifest_json: str
    base_registry_sha256: str
    content_sha256: str
    confirmation_token: str

    @property
    def decision_bytes(self) -> bytes:
        return self.decision_json.encode("utf-8")

@dataclass(frozen=True)
class PublicationResult:
    preview_id: str
    decision_id: str
    revision: int
    status: Literal["committed_not_pushed", "published"]
    commit_sha: str
```

New V1 publication previews create `decision_id = "dec_" + uuid.uuid4().hex`, `revision = 1`, and `lifecycle = "active"`. Replaying `preview(candidate_id)` returns the stored preview until another accepted review exists. A newer review invalidates every unpublished preview for that Candidate, and `confirm` verifies the preview still references the latest accepted review. The confirmation token is `publish:<preview_id>:<first 12 characters of content_sha256>`. The exact canonical revision bytes and exact next manifest bytes are stored privately in the preview; no field is recomputed after confirmation.

Private paths are:

```text
reviews/<candidate-id>/<review-id>.json
publication-previews/<preview-id>.json
publication-results/<preview-id>.json
```

Run:

```bash
.venv/bin/python -m unittest tests.test_review_publish -v
```

Expected: PASS for review and preview tests; confirmation tests still fail until the Git adapter exists.

- [ ] **Step 3: Write failing scoped Git Registry tests**

Create a temporary working repository and bare `origin` in `tests/test_git_registry.py`, then assert:

```python
def test_publish_commits_only_registry_paths_and_pushes_main(self):
    (self.repo / "unrelated.txt").write_text("user change", encoding="utf-8")
    result = self.registry.publish(self.preview)
    self.assertEqual("published", result.status)
    self.assertEqual("user change", (self.repo / "unrelated.txt").read_text())
    self.assertIn("unrelated.txt", git(self.repo, "status", "--porcelain"))
    changed = git(self.repo, "show", "--pretty=", "--name-only", result.commit_sha).splitlines()
    self.assertEqual(
        [
            f"decision-registry/decisions/{self.preview.decision_id}/r0001.json",
            "decision-registry/registry.json",
        ],
        sorted(changed),
    )
    self.assertEqual(result.commit_sha, git(self.repo, "rev-parse", "origin/main"))

def test_wrong_branch_dirty_registry_or_remote_ahead_stops_before_write(self):
    for arrange in (checkout_feature, dirty_registry, advance_origin):
        with self.subTest(arrange=arrange.__name__):
            arrange(self)
            with self.assertRaises(RegistryPreconditionError):
                self.registry.publish(self.preview)
            self.assertFalse(self.revision_path.exists())

def test_retry_reconciles_existing_commit_without_new_revision(self):
    first = self.registry.publish(self.preview)
    second = self.registry.publish(self.preview)
    self.assertEqual(first.commit_sha, second.commit_sha)
    self.assertEqual(1, len(list((self.repo / "decision-registry" / "decisions" / self.preview.decision_id).glob("*.json"))))
```

Run:

```bash
.venv/bin/python -m unittest tests.test_git_registry -v
```

Expected: FAIL because the Git Registry adapter does not exist.

- [ ] **Step 4: Implement the canonical Registry codec and scoped Git adapter**

`decision-registry/registry.json` becomes:

```json
{
  "decisions": {},
  "format": "zdecision-registry/v1",
  "schema_version": 1
}
```

Each manifest value contains only `product`, `head_revision`, `lifecycle`, and `head_path`. Validate that ids match `^dec_[0-9a-f]{32}$`, revisions are positive integers, relation ids exist or are being published in the same confirmed operation, and `head_path` stays beneath `decision-registry/decisions/<decision-id>/`.

`GitRegistry.publish(preview)` performs this exact order:

1. Verify repository root, branch `main`, canonical `origin`, and a clean `decision-registry/` subtree.
2. Run `git fetch origin main`; require local `HEAD == origin/main`.
3. Re-read and validate the manifest at that HEAD; require its bytes to match the base hash stored in the preview.
4. Atomically write only the previewed revision and manifest bytes.
5. Run `git add -- <revision-path> decision-registry/registry.json`.
6. Run `git commit --only -m "decision(<decision-id>): publish revision 1" -- <revision-path> decision-registry/registry.json`.
7. Store the local commit sha in the private publication result before pushing.
8. Run `git push origin main`.
9. Verify `git rev-parse origin/main` equals the stored commit sha, then mark the private result `published`.

If push fails, leave status `committed_not_pushed`. Retry checks whether the remote already contains the commit; if yes, mark published, and if local `HEAD` still equals the stored commit, retry the same push. Any other remote/local shape returns exit `5` and does not create another revision.

Run:

```bash
.venv/bin/python -m unittest tests.test_git_registry -v
```

Expected: PASS.

- [ ] **Step 5: Write and implement failing Promotion confirmation/CLI tests**

Add these cases:

```python
def test_wrong_confirmation_token_never_calls_git(self):
    with self.assertRaises(ConfirmationRequired):
        self.service.confirm(self.preview.preview_id, "publish:wrong:token")
    self.assertEqual(0, self.git_registry.publish_calls)

def test_confirm_publishes_only_the_stored_preview(self):
    result = self.service.confirm(self.preview.preview_id, self.preview.confirmation_token)
    self.assertEqual(self.preview.decision_id, result.decision_id)
    self.assertEqual(self.preview.content_sha256, self.git_registry.last_preview.content_sha256)

def test_cli_publish_confirm_replays_result(self):
    first_code, first_payload = run_cli(["publish", "confirm", "--preview-id", self.preview.preview_id, "--token", self.preview.confirmation_token])
    second_code, second_payload = run_cli(["publish", "confirm", "--preview-id", self.preview.preview_id, "--token", self.preview.confirmation_token])
    self.assertEqual((0, 0), (first_code, second_code))
    self.assertEqual(first_payload["data"]["commit_sha"], second_payload["data"]["commit_sha"])
```

Implement `review record --candidate-id --action --controller-thread-id --controller-turn-id --actor --input -`, `publish preview --candidate-id`, and `publish confirm --preview-id --token`. `--input -` contains only the allowed patch object. An empty object is required for `accept`, `reject`, and `skip`.

Run:

```bash
.venv/bin/python -m unittest tests.test_review_publish tests.test_git_registry -v
```

Expected: PASS.

- [ ] **Step 6: Add the Review/Publish Skill contract and Registry format documentation**

`references/review-publish.md` must tell Codex to:

1. Present every Candidate as private proposed content.
2. Record accept, edit-and-accept, reject, or skip with the current controller task and Turn ids; never send raw user text to the CLI.
3. Run `publish preview` only for the latest accepted review and show the exact formal JSON, target paths, Decision id/revision, and confirmation token.
4. Ask the user to reply with the exact token. A generic “可以” is not enough when more than one preview is open.
5. Run `publish confirm` only after that reply; never construct an arbitrary Decision payload or edit `decision-registry/` directly.
6. Report a local-only commit distinctly from a pushed publication and reconcile it before another attempt.

Update `decision-registry/README.md` with the exact manifest and revision paths plus the formal/private boundary. Extend `tests/test_skill_contract.py` to require `explicit confirmation`, `publish preview`, `publish confirm`, and `decision-registry/`, and forbid instructions that equate acceptance with publication.

Run:

```bash
.venv/bin/python -m unittest tests.test_review_publish tests.test_git_registry tests.test_skill_contract -v
```

Expected: PASS.

- [ ] **Step 7: Commit the demonstrable Review/Publish slice**

Run:

```bash
git add src/zdecision/registry src/zdecision/private_store/filesystem.py src/zdecision/cli.py .agents/skills/zdecision decision-registry tests/test_review_publish.py tests/test_git_registry.py tests/test_skill_contract.py
git commit -m "feat: publish reviewed decisions to git"
git push origin main
```

Acceptance: In a Codex conversation, the user can edit or reject without Git changes, inspect exact formal bytes, explicitly confirm one preview, and observe one pushed immutable Decision revision on `main`.

---

### Task 3: Preflight and Use — Applicable Context to a New Native Codex Task

**Files:**

- Create: `src/zdecision/preflight/__init__.py`
- Create: `src/zdecision/preflight/models.py`
- Create: `src/zdecision/preflight/registry_reader.py`
- Create: `src/zdecision/preflight/applicability.py`
- Create: `src/zdecision/preflight/context.py`
- Create: `src/zdecision/preflight/service.py`
- Create: `.agents/skills/zdecision/references/preflight-use.md`
- Create: `tests/test_preflight.py`
- Create: `tests/test_context_pack.py`
- Create: `tests/test_v1_flow.py`
- Modify: `src/zdecision/app_server/contracts.py`
- Modify: `src/zdecision/private_store/filesystem.py`
- Modify: `src/zdecision/cli.py`
- Modify: `.agents/skills/zdecision/SKILL.md`
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `tests/test_skill_contract.py`

**Interfaces:**

- Consumes: target checkout, product, new goal, a Registry read state, one validated assessment per active Decision revision, explicit include/exclude choices for conflicts or unknowns, and a native created-task result.
- Produces: `PreflightService.prepare(target: Path, product: str, goal: str) -> PreflightPlan`, `PreflightService.assess(preflight_id: str, assessments: Sequence[AssessmentInput]) -> AssessmentSet`, `PreflightService.assemble(preflight_id: str, choices: PackChoices) -> ContextPack`, `PreflightService.prepare_task(pack_id: str) -> TaskPlan`, and `PreflightService.attach_task(operation_id: str, created: CreatedTask) -> TaskUsage`.
- Produces CLI commands: `zdecision preflight prepare`, `zdecision preflight assess`, `zdecision preflight assemble`, `zdecision task prepare`, `zdecision task attach`, and `zdecision task usage`.
- Context Pack bounds are exactly 20 included Decision revisions and 32,768 UTF-8 bytes for the rendered pack.

Use these shared Preflight/task records throughout Task 3:

```python
@dataclass(frozen=True)
class WorkspaceIdentity:
    root: str
    remote: str
    branch: str
    head_commit: str

@dataclass(frozen=True)
class RegistryRead:
    availability: Literal["fresh", "stale", "unavailable"]
    content_state: Literal["nonempty", "empty"]
    commit_sha: str | None
    decisions: tuple[DecisionRevision, ...]
    last_successful_fetch_at: str | None

@dataclass(frozen=True)
class PreflightPlan:
    preflight_id: str
    workspace: WorkspaceIdentity
    product: str
    goal: str
    registry: RegistryRead

@dataclass(frozen=True)
class AssessmentInput:
    decision_id: str
    revision: int
    classification: Literal["matched", "conflict", "not_applicable", "unknown"]
    rationale: str

@dataclass(frozen=True)
class AssessmentSet:
    preflight_id: str
    content_state: Literal["applicable", "no_applicable", "empty"]
    assessments: tuple[AssessmentInput, ...]

@dataclass(frozen=True)
class PackChoices:
    include: frozenset[str] = frozenset()
    exclude: frozenset[str] = frozenset()
    continue_without_fresh_registry: bool = False

@dataclass(frozen=True)
class PackedDecision:
    revision_key: str
    classification: Literal["matched", "conflict", "unknown"]
    decision: DecisionRevision

@dataclass(frozen=True)
class ExcludedDecision:
    revision_key: str
    reason: Literal["not_applicable", "user_excluded", "budget"]

@dataclass(frozen=True)
class CreatedTask:
    thread_id: str | None
    host_id: str | None
    client_thread_id: str | None = None

@dataclass(frozen=True)
class TaskPlan:
    operation_id: str
    pack_id: str
    status: Literal["prepared", "pending", "attached"]
    first_prompt: str
    replayed: bool
    created_task: CreatedTask | None

@dataclass(frozen=True)
class TaskUsage:
    thread_id: str
    host_id: str | None
    pack_id: str
    registry_commit: str | None
    decision_revision_keys: tuple[str, ...]
```

- [ ] **Step 1: Write failing Registry-state and workspace-inspection tests**

Add to `tests/test_preflight.py`:

```python
def test_fresh_empty_registry_is_not_unavailable(self):
    self.fetch_succeeds_with_manifest(decisions={})
    result = self.reader.read()
    self.assertEqual("fresh", result.availability)
    self.assertEqual("empty", result.content_state)

def test_fetch_failure_with_last_known_ref_is_stale(self):
    self.seed_last_known_registry(one_active_decision())
    self.fetch_fails("offline")
    result = self.reader.read()
    self.assertEqual("stale", result.availability)
    self.assertEqual(1, len(result.decisions))

def test_fetch_failure_without_valid_registry_is_unavailable(self):
    self.fetch_fails("offline")
    result = self.reader.read()
    self.assertEqual("unavailable", result.availability)
    self.assertEqual((), result.decisions)

def test_workspace_record_contains_identity_not_dirty_content(self):
    workspace = inspect_workspace(self.target_repo)
    payload = workspace.to_dict()
    self.assertEqual(git(self.target_repo, "rev-parse", "HEAD"), payload["head_commit"])
    self.assertNotIn("diff", payload)
    self.assertNotIn("files", payload)
```

`RegistryRead` separates `availability` (`fresh`, `stale`, `unavailable`) from `content_state` (`nonempty`, `empty`). Workspace identity contains repository root, canonical remote, branch, and HEAD commit only.

Run:

```bash
.venv/bin/python -m unittest tests.test_preflight.RegistryReadTests -v
```

Expected: FAIL because Preflight Registry reading does not exist.

- [ ] **Step 2: Implement read-only Registry state and workspace inspection**

On a successful `git fetch origin main`, read Registry files from `origin/main` with `git show`; do not merge or alter the checkout. Persist `last_successful_fetch_at` and the remote commit id privately. On fetch failure, use a previously validated `origin/main` ref as stale data. If neither a fresh nor previously validated manifest can be decoded, return unavailable rather than an empty list.

`prepare` stores only typed workspace identity, goal, active formal revisions, and Registry state. It returns immediately for an empty Registry and requires an explicit `continue_without_fresh_registry` choice before assembly when availability is stale or unavailable.

Run:

```bash
.venv/bin/python -m unittest tests.test_preflight.RegistryReadTests -v
```

Expected: PASS.

- [ ] **Step 3: Write failing applicability validation tests**

Use these tests:

```python
def test_assessment_requires_exactly_one_result_per_active_revision(self):
    plan = self.prepare_with(decision_revisions("dec_a:1", "dec_b:2"))
    with self.assertRaises(AssessmentCoverageError):
        self.service.assess(plan.preflight_id, [assessment("dec_a", 1, "matched")])

def test_assessment_rejects_unknown_decision_or_lifecycle_mutation(self):
    plan = self.prepare_with(decision_revisions("dec_a:1"))
    with self.assertRaises(AssessmentValidationError):
        self.service.assess(plan.preflight_id, [{
            **assessment("dec_unknown", 1, "matched"),
            "lifecycle": "retired",
        }])

def test_all_not_applicable_is_distinct_from_empty_registry(self):
    plan = self.prepare_with(decision_revisions("dec_a:1"))
    result = self.service.assess(plan.preflight_id, [assessment("dec_a", 1, "not_applicable")])
    self.assertEqual("no_applicable", result.content_state)
```

Each assessment is exactly `{decision_id, revision, classification, rationale}`. Classification is one of `matched`, `conflict`, `not_applicable`, or `unknown`; rationale is non-empty and at most 1,000 characters. The engine validates coverage and stores the result privately but never writes a Decision or lifecycle field.

Run:

```bash
.venv/bin/python -m unittest tests.test_preflight.ApplicabilityTests -v
```

Expected: FAIL because applicability validation does not exist.

- [ ] **Step 4: Implement applicability assessment and the Skill-side reasoning handoff**

`preflight prepare` emits an `assessment_input` array containing complete formal revisions plus workspace identity and goal. The Skill performs the semantic comparison in the controlling Codex task, then sends only the four allowed assessment fields to `preflight assess --input -`. It must classify uncertainty as `unknown`, not infer formal lifecycle changes, and show conflicts/unknowns to the user before packing.

Run:

```bash
.venv/bin/python -m unittest tests.test_preflight.ApplicabilityTests -v
```

Expected: PASS.

- [ ] **Step 5: Write failing bounded Context Pack tests, including complete coverage**

Add to `tests/test_context_pack.py`:

```python
def test_pack_never_splits_a_decision_item(self):
    result = assemble_context(decisions=[decision_of_size(512), decision_of_size(512)], max_bytes=900)
    self.assertEqual(1, len(result.included))
    self.assertEqual("budget", result.excluded[0].reason)
    self.assertLessEqual(len(result.rendered.encode("utf-8")), 250)

def test_oversized_first_item_does_not_hide_later_small_items(self):
    revisions = [
        assessed("dec_big", 1, "matched", size=2048),
        assessed("dec_conflict", 1, "conflict", size=128),
        assessed("dec_small", 1, "matched", size=128),
        assessed("dec_unknown", 1, "unknown", size=128),
    ]
    result = assemble_context(revisions, max_bytes=800, include={"dec_conflict:1", "dec_unknown:1"})
    included = {item.revision_key for item in result.included}
    excluded = {item.revision_key for item in result.excluded}
    applicable = {item.revision_key for item in revisions}
    self.assertNotIn("dec_big:1", included)
    self.assertIn("dec_small:1", included)
    self.assertTrue(included.isdisjoint(excluded))
    self.assertEqual(applicable, included | excluded)

def test_conflict_and_unknown_require_explicit_choice(self):
    with self.assertRaises(PackChoiceRequired) as raised:
        assemble_context([assessed("dec_a", 1, "conflict", size=80)], max_bytes=1000)
    self.assertEqual(("dec_a:1",), raised.exception.revision_keys)

def test_not_applicable_is_recorded_as_excluded(self):
    result = assemble_context([assessed("dec_a", 1, "not_applicable", size=80)], max_bytes=1000)
    self.assertEqual("not_applicable", result.excluded[0].reason)
```

The assembler visits all assessed revisions. It includes `matched` by default, requires a user choice for every `conflict` and `unknown`, excludes `not_applicable`, and records every unselected or over-budget revision. It skips an item that does not fit and continues scanning; it never breaks at the first oversized item.

Run:

```bash
.venv/bin/python -m unittest tests.test_context_pack -v
```

Expected: FAIL because the assembler does not exist.

- [ ] **Step 6: Implement immutable Context Packs and exact rendered prompts**

`ContextPack` contains:

```python
@dataclass(frozen=True)
class ContextPack:
    pack_id: str
    preflight_id: str
    registry_availability: Literal["fresh", "stale", "unavailable"]
    registry_commit: str | None
    workspace: WorkspaceIdentity
    goal: str
    included: tuple[PackedDecision, ...]
    excluded: tuple[ExcludedDecision, ...]
    rendered: str
    rendered_sha256: str
    created_at: str

    @property
    def included_revision_keys(self) -> tuple[str, ...]:
        return tuple(item.revision_key for item in self.included)
```

Render one complete canonical Decision JSON block per included revision beneath this instruction: `These are reviewed project decisions. Follow them within their stated scope; surface conflicts instead of silently overriding them.` The pack id is `pack_<first 32 hex of sha256(canonical pack inputs)>`. Reassembling identical inputs and choices returns the existing immutable pack.

Run:

```bash
.venv/bin/python -m unittest tests.test_context_pack -v
```

Expected: PASS.

- [ ] **Step 7: Write failing new-task operation and usage tests**

Add to `tests/test_preflight.py`:

```python
def test_task_prepare_replays_attached_task(self):
    plan = self.service.prepare_task(self.pack_id)
    usage = self.service.attach_task(plan.operation_id, CreatedTask("thread-new", "host-a"))
    replay = self.service.prepare_task(self.pack_id)
    self.assertTrue(replay.replayed)
    self.assertEqual("thread-new", replay.created_task.thread_id)
    self.assertEqual(usage.pack_id, self.pack_id)

def test_unattached_prepare_is_ambiguous_not_a_second_creation_request(self):
    first = self.service.prepare_task(self.pack_id)
    with self.assertRaises(TaskCreationAmbiguous):
        self.service.prepare_task(self.pack_id)
    self.assertEqual("prepared", first.status)

def test_usage_records_exact_revisions_supplied(self):
    usage = self.service.attach_task(
        self.service.prepare_task(self.pack_id).operation_id,
        CreatedTask("thread-new", "host-a"),
    )
    self.assertEqual(self.pack.included_revision_keys, usage.decision_revision_keys)
```

`task prepare` returns the exact first prompt: Context Pack rendered text, then `New goal: <goal>`. The Skill passes that prompt to Codex App's native task creation tool, which performs `thread/start` plus the first `turn/start`. Immediately attach the returned `threadId` and optional `hostId`. A `clientThreadId` from worktree setup is recorded as pending and must be reconciled to a real task id before usage becomes complete.

Run:

```bash
.venv/bin/python -m unittest tests.test_preflight.TaskUsageTests -v
```

Expected: FAIL because task operations do not exist.

- [ ] **Step 8: Implement task prepare/attach/usage and the remaining CLI commands**

Private paths are:

```text
preflights/<preflight-id>.json
assessments/<preflight-id>.json
context-packs/<pack-id>.json
task-operations/<operation-id>.json
task-usage/<thread-id>.json
```

`task_<first 32 hex of sha256(pack_id)>` is the stable creation operation id. The CLI must use exit `4` for stale/unavailable Registry choice or conflict/unknown pack choice, and exit `5` for a prepared-but-unattached task operation. `task usage --thread-id` prints the pack id, Registry commit/state, and exact Decision revision keys without printing source conversations or Candidate content.

Run:

```bash
.venv/bin/python -m unittest tests.test_preflight tests.test_context_pack -v
```

Expected: PASS.

- [ ] **Step 9: Complete the repository Skill and native task-routing contract**

`references/preflight-use.md` must encode these routes:

- New goal/new developer handoff: prepare and assess Preflight, show stale/unavailable/conflict/unknown state, assemble the approved pack, run `task prepare`, call native project task creation with the exact prompt, then immediately run `task attach`.
- Same goal in the current task: continue in the same task; no Preflight and no new task operation.
- Same goal in an idle existing task: use the native resume/follow-up capability.
- Correction while a Turn is executing: use native `turn/steer` when the host exposes it. If it is unavailable, do not emulate steering by creating a task; tell the user the correction cannot be injected and let them stop or wait.
- Capture: native read/fork/start-turn behavior remains isolated from formal publication.

Extend `tests/test_skill_contract.py`:

```python
def test_app_server_routes_are_complete_and_non_coordinating(self):
    text = all_skill_text()
    for operation in ("thread/read", "thread/fork", "turn/start", "thread/start", "thread/resume", "turn/steer"):
        self.assertIn(operation, text)
    self.assertIn("same goal", text)
    self.assertIn("new goal", text)
    self.assertNotIn("background coordinator", text.lower())
    self.assertNotIn("start a replacement task", text.lower())
```

Update `AGENTS.md` and `README.md` only with commands that now exist and have passing tests. Document `.agents/skills/zdecision/SKILL.md` as the conversational entry point and the CLI as internal.

Run:

```bash
.venv/bin/python -m unittest tests.test_skill_contract -v
```

Expected: PASS.

- [ ] **Step 10: Add and pass the complete V1 flow test**

`tests/test_v1_flow.py` uses a fake native host only at the task-tool boundary and real private/Registry services everywhere else:

```python
def test_complete_v1_flow_keeps_private_state_out_of_git(self):
    capture = complete_capture_with_one_candidate(self.system)
    review = accept_candidate(self.system, capture.candidate_ids[0])
    preview = self.system.promotion.preview(review.candidate_id)
    publication = self.system.promotion.confirm(preview.preview_id, preview.confirmation_token)
    preflight = self.system.preflight.prepare(self.target_repo, "anheng", "Implement the approved goal")
    self.system.preflight.assess(preflight.preflight_id, [matched(publication.decision_id, 1)])
    pack = self.system.preflight.assemble(preflight.preflight_id, PackChoices())
    task_plan = self.system.preflight.prepare_task(pack.pack_id)
    usage = self.system.preflight.attach_task(task_plan.operation_id, CreatedTask("thread-new", "host-a"))

    self.assertEqual((f"{publication.decision_id}:1",), usage.decision_revision_keys)
    committed = git(self.registry_repo, "show", "--pretty=", "--name-only", publication.commit_sha)
    self.assertNotIn("candidates", committed)
    self.assertNotIn("reviews", committed)
    self.assertNotIn("context-packs", committed)
    self.assertNotIn("thread-a raw text", git(self.registry_repo, "show", publication.commit_sha))
```

Run the full suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
git diff --check
git status --short
```

Expected: all tests PASS, `git diff --check` prints nothing, and status contains only the intended Task 3 changes.

- [ ] **Step 11: Run the four real Codex conversation acceptance scenarios**

Use the repository Skill from Codex App and record only ids/statuses in a private acceptance note:

1. Capture a task whose last completed Turn contains two explicit decisions; verify two private Candidates and no source text under `decision-registry/`.
2. Reject one Candidate, edit and accept the other, inspect exact preview JSON, reply with its token, and verify one pushed `r0001.json` on `origin/main`.
3. Start a genuinely new goal in a saved target project, choose any conflict/unknown items explicitly, verify the created task's first prompt contains the complete included Decision revision, then inspect `task usage` for the same revision key.
4. In an unchanged goal, send a follow-up in the existing task and verify no `task-operation` or Context Pack was created.

If a native task capability is missing, the Skill must stop with the exact missing operation; it must not fall back to a second app-server process or a replacement local runtime.

- [ ] **Step 12: Commit the complete V1 product path**

Run:

```bash
git add src/zdecision/preflight src/zdecision/app_server/contracts.py src/zdecision/private_store/filesystem.py src/zdecision/cli.py .agents/skills/zdecision AGENTS.md README.md tests/test_preflight.py tests/test_context_pack.py tests/test_v1_flow.py tests/test_skill_contract.py
git commit -m "feat: start codex tasks with decision context"
git push origin main
```

Final acceptance: all seven completion conditions in `docs/architecture.md` section 9 are demonstrable from Codex App, and Git contains only formal reviewed Decision state.

---

## Implementation Review Gates

After each top-level Task, review only that working vertical slice:

1. **Architecture gate:** no raw task content in private typed records beyond Candidate content, no private state in Git, and no conversation runtime or Coordinator.
2. **Behavior gate:** the slice's replay path returns the original result and its ambiguous external gap stops rather than duplicating work.
3. **User gate:** Codex App can demonstrate the slice through natural language without asking the user to compose internal CLI commands.

Do not add lifecycle editing, Registry branches, distributed locks, generalized migration infrastructure, multi-level approvals, or CLI-first documentation while implementing this plan.
