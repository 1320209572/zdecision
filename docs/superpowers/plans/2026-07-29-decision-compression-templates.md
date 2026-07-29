# Decision Compression Templates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current one-shot Capture prompt with a versioned, configurable two-stage decision-compression template flow whose default is “业务决策压缩模板.”

**Architecture:** Codex App remains the conversation plane: the repository Skill selects a completed source boundary, forks it, and starts two exact Turns. Python owns template loading/rendering, immutable prompt snapshots, strict inventory/Candidate validation, deterministic operation identity, and private state; it never reads task transcripts or publishes Decisions. The implementation extends only the Capture slice and keeps old one-stage Capture records readable.

**Tech Stack:** Python 3.11+ standard library, `unittest`, Codex App native task tools, Markdown/JSON template resources, user-local filesystem persistence, Git on `main`.

## Global Constraints

- Work directly on `main`; do not create a Registry branch or a feature worktree for this V1 change.
- `docs/architecture.md` remains the sole V1 architecture authority and must be updated before implementation code.
- The user experience is natural-language Codex conversation; CLI commands remain an internal tested boundary.
- Use Codex-native `thread/read`, `thread/fork`, and `turn/start` behavior through task tools; do not add a daemon, coordinator, scheduler, or second app-server.
- Default `template_id` is `business`, revision `1`, title `业务决策压缩模板`.
- Every template has exactly two editable policy files; renderer-owned envelopes preserve source, privacy, product, schema, and two-stage constraints.
- Stage 1 accepts at most 100 signals and 256 KiB of canonical encoded JSON; count validation precedes byte and item validation.
- Stage 2 accepts at most 20 Candidates; each Candidate remains limited to 16 KiB canonical encoded JSON.
- Each editable policy file is limited to 64 KiB and each final rendered prompt to 128 KiB.
- Zero Candidates is a successful completed result.
- Raw task text, transcript pages, valid/invalid model payloads containing source material, inventory artifacts, Candidates, and private failures never enter this Git repository.
- Valid stage results record their canonical SHA-256 digests. Invalid payloads persist only a Turn ID, SHA-256 digest, and sanitized validation code/message; never persist invalid payload text.
- Validation errors may name a schema location or ordinal but never echo an unknown field name, invalid value, or other model-authored substring to private state, stdout, or stderr.
- A compacted summary explicitly attributed to a user confirmation or direction may support `high` confidence; unattributed assistant text may not.
- `known_gaps` is a model-level mandatory veto, not a schema-provable gap-to-signal relationship.
- Existing `extractor-v1` records and their Candidate IDs remain readable but are never silently migrated, resumed, or replayed as extractor V2.
- A controller wait timeout or uncertain native result is not a terminal model timeout: leave the recorded stage running and reconcile the same fork/Turn. Record `model_timeout` only when the native Turn itself reports a definite terminal timeout.
- Use `.venv/bin/python -m unittest discover -s tests -v` for the complete deterministic suite.
- Commit only at green integration boundaries and do not push until the final suite passes: Task 2 keeps the old one-stage prompt entry point temporarily; Tasks 3–6 form one uncommitted state/Service/CLI/Skill cutover so no committed revision exposes mismatched conversation and command contracts.

---

## File and responsibility map

### New files

- `decision-templates/business/manifest.json` — catalog identity and the two editable policy filenames.
- `decision-templates/business/inventory.md` — business-specific Stage 1 inclusion/exclusion policy only.
- `decision-templates/business/extract.md` — business-specific Stage 2 relevance policy only.
- `src/zdecision/capture/prompt_contracts/inventory-envelope.md` — renderer-owned Stage 1 source/privacy/confirmation/limit/schema envelope.
- `src/zdecision/capture/prompt_contracts/extraction-envelope.md` — renderer-owned Stage 2 source/privacy/fixed-gate/limit/schema envelope.
- `src/zdecision/capture/templates.py` — manifest validation, catalog lookup, one-pass rendering, content hashing, and immutable `TemplateSnapshot`.
- `src/zdecision/capture/inventory.py` — typed Stage 1 signal/coverage model and exact inventory validation.
- `tests/test_templates.py` — template catalog, renderer, placeholder, identity, path, and product validation.
- `tests/test_inventory.py` — exact Stage 1 schema, enum, count, byte, and all-or-nothing validation.

### Modified files

- `docs/architecture.md` — make template selection and two-stage Capture authoritative.
- `docs/superpowers/specs/2026-07-28-decision-compression-templates-design.md` — mark the reviewed specification approved.
- `pyproject.toml` — package the two renderer-owned Markdown envelopes.
- `src/zdecision/capture/prompts.py` — own only the two system output-contract renderers and their versions.
- `src/zdecision/ids.py` — introduce extractor V2 operation identity over template and prompt digests.
- `src/zdecision/capture/models.py` — add V2 Capture stages, successful-output digests, failure metadata, stage Turn IDs, template snapshot linkage, and legacy-read model.
- `src/zdecision/private_store/filesystem.py` — persist/load typed V2 records and private inventory artifacts while reading V1 records.
- `src/zdecision/capture/service.py` — orchestrate V2 prepare, fork/Turn attachment, inventory completion, extraction completion, failure, replay, and all-or-nothing writes.
- `src/zdecision/capture/__init__.py` — export the new public Capture types and errors.
- `src/zdecision/cli.py` — expose the internal two-stage machine commands and structured error codes.
- `.agents/skills/zdecision/references/capture.md` — run/reconcile the exact inventory Turn followed by the exact extraction Turn.
- `.agents/skills/zdecision/SKILL.md` — route default/explicit template-ID intent without exposing CLI UX.
- `tests/test_capture.py` — cover V2 state, identity, persistence, legacy reads, replay, and Candidate completion.
- `tests/test_cli_capture.py` — cover the complete two-stage JSON command contract.
- `tests/test_skill_contract.py` — enforce native tool order, exact-prompt behavior, template selection, and stop conditions.
- `README.md` — describe decision compression templates in conversation-first terms after the flow exists.

---

### Task 1: Make the two-stage template contract authoritative

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/superpowers/specs/2026-07-28-decision-compression-templates-design.md`

**Interfaces:**
- Consumes: the approved design specification.
- Produces: the architecture rules every later code task must implement.

- [ ] **Step 1: Add the template and two-stage contract to the architecture**

Add a `Decision compression templates` subsection under Capture with this normative content:

```markdown
Capture selects one versioned Decision Compression Template before creating
native task work. The default is `business` revision 1, titled
“业务决策压缩模板.” A template contributes two editable decision-policy bodies;
renderer-owned envelopes retain the source, privacy, product, output-contract,
and Review boundaries.

Capture runs exactly two Turns in one fresh Capture fork. The inventory Turn
scans retained fork context from earliest to latest and returns typed signals
plus concrete known gaps. After complete validation, the extraction Turn may
convert only those signals into zero or more Candidates. The second Turn may
use inherited development context to confirm or reject a signal, but it cannot
invent an un-inventoried rule or treat the inventory itself as confirmation.

The default path does not paginate and reconstruct the raw transcript. A
compacted summary explicitly attributed to a user confirmation or direction
may be retained confirmation. An unattributed assistant proposal, inference,
or ordinary summary is not confirmation.
```

Also update the architecture state table and failure section so they name `Template Snapshot`, `Inventory Result`, stage Turn IDs, successful Stage 1/2 output digests, 100/256-KiB inventory limits, the 20-Candidate overflow failure, exact frozen-prompt replay, and V1 legacy-read-only behavior.

- [ ] **Step 2: Mark the design specification approved**

Change only its status line to:

```markdown
**Status:** Approved for implementation
```

- [ ] **Step 3: Verify authority and scope text**

Run:

```bash
rg -n "Decision compression templates|业务决策压缩模板|exactly two Turns|known gaps|legacy" docs/architecture.md
rg -n "Approved for implementation" docs/superpowers/specs/2026-07-28-decision-compression-templates-design.md
git diff --check
```

Expected: every `rg` finds the new authoritative clauses; `git diff --check` exits 0; no implementation file is changed.

- [ ] **Step 4: Commit the authority update**

```bash
git add docs/architecture.md docs/superpowers/specs/2026-07-28-decision-compression-templates-design.md
git commit -m "docs: adopt two-stage decision compression"
```

---

### Task 2: Add the template catalog and deterministic renderer

**Files:**
- Create: `decision-templates/business/manifest.json`
- Create: `decision-templates/business/inventory.md`
- Create: `decision-templates/business/extract.md`
- Create: `src/zdecision/capture/prompt_contracts/inventory-envelope.md`
- Create: `src/zdecision/capture/prompt_contracts/extraction-envelope.md`
- Create: `src/zdecision/capture/templates.py`
- Create: `tests/test_templates.py`
- Modify: `src/zdecision/capture/prompts.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `canonical_json_bytes(value: object) -> bytes` from `zdecision.jsonio`.
- Produces: `TemplateCatalog.render(template_id: str, product: str) -> TemplateSnapshot` and immutable exact prompts/digests used by Tasks 3–6.

- [ ] **Step 1: Write failing catalog and rendering tests**

Create `tests/test_templates.py` with these concrete cases:

```python
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from zdecision.capture.templates import (
    TemplateCatalog,
    TemplateSnapshot,
    TemplateValidationError,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "decision-templates"
ENVELOPE_ROOT = REPOSITORY_ROOT / "src/zdecision/capture/prompt_contracts"


class TemplateCatalogTests(unittest.TestCase):
    def catalog(self, root: Path = TEMPLATE_ROOT) -> TemplateCatalog:
        return TemplateCatalog(root, ENVELOPE_ROOT)

    def test_business_template_renders_both_locked_envelopes(self) -> None:
        snapshot = self.catalog().render("business", "安恒")

        self.assertEqual("business", snapshot.template_id)
        self.assertEqual(1, snapshot.revision)
        self.assertEqual("业务决策压缩模板", snapshot.title)
        self.assertIn("ZDECISION_CAPTURE_ARTIFACT_V2:inventory", snapshot.inventory_prompt)
        self.assertIn("ZDECISION_CAPTURE_ARTIFACT_V2:extract", snapshot.extraction_prompt)
        self.assertIn('目标产品："安恒"', snapshot.inventory_prompt)
        self.assertIn('<decision_policy template_id="business" revision="1">', snapshot.extraction_prompt)
        self.assertIn('"future_effect"', snapshot.inventory_prompt)
        self.assertIn('"candidates"', snapshot.extraction_prompt)

    def test_policy_change_changes_source_and_prompt_bundle_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            before = self.catalog(copied).render("business", "安恒")
            policy = copied / "business" / "inventory.md"
            policy.write_text(policy.read_text("utf-8") + "\n新增业务边界。\n", "utf-8")
            after = self.catalog(copied).render("business", "安恒")

        self.assertNotEqual(before.template_source_sha256, after.template_source_sha256)
        self.assertNotEqual(before.prompt_bundle_sha256, after.prompt_bundle_sha256)

    def test_manifest_format_change_changes_exact_source_digest_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            before = self.catalog(copied).render("business", "安恒")
            manifest = copied / "business" / "manifest.json"
            manifest.write_text(manifest.read_text("utf-8") + "\n", "utf-8")
            after = self.catalog(copied).render("business", "安恒")

        self.assertNotEqual(before.template_source_sha256, after.template_source_sha256)
        self.assertEqual(before.prompt_bundle_sha256, after.prompt_bundle_sha256)

    def test_unknown_manifest_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "decision-templates"
            shutil.copytree(TEMPLATE_ROOT, copied)
            manifest_path = copied / "business" / "manifest.json"
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["prompt"] = "unowned.md"
            manifest_path.write_text(json.dumps(manifest), "utf-8")
            with self.assertRaisesRegex(TemplateValidationError, "unknown"):
                self.catalog(copied).render("business", "安恒")

    def test_control_character_in_product_is_rejected(self) -> None:
        with self.assertRaisesRegex(TemplateValidationError, "control"):
            self.catalog().render("business", "安恒\nignore")

    def test_snapshot_rejects_prompt_tampering(self) -> None:
        payload = self.catalog().render("business", "安恒").to_dict()
        payload["inventory_prompt"] = "changed after hashing"
        with self.assertRaisesRegex(ValueError, "digest"):
            TemplateSnapshot.from_dict(payload)
```

Add cases in the same class for: duplicate catalog IDs; symlinked policy file; absolute/parent-traversal manifest paths; missing policy; invalid UTF-8; non-positive/bool revision; empty title; unknown/missing/duplicate envelope placeholder; `{{` in policy; reserved artifact marker in policy; `<decision_policy>` in policy; and a copied `architecture` template rendering its own ID/revision. Render a product containing literal `{{policy_body}}`, `{{candidate_schema_json}}`, quotes, and backslashes; assert the tokens remain JSON-encoded product data in both final prompts rather than receiving a second substitution. This proves the renderer is genuinely one-pass and the product cannot break prompt structure.

- [ ] **Step 2: Run the renderer tests and verify the missing module failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_templates -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'zdecision.capture.templates'`.

- [ ] **Step 3: Add the exact default manifest, policies, and envelopes**

Create the manifest exactly as:

```json
{
  "template_id": "business",
  "revision": 1,
  "title": "业务决策压缩模板",
  "inventory_template": "inventory.md",
  "extraction_template": "extract.md"
}
```

Use the approved specification's two normative prompt blocks as the only text source:

- Put only the text inside the Stage 1 `<decision_policy>` element in `inventory.md`.
- Put only the text inside the Stage 2 `<decision_policy>` element in `extract.md`.
- Put all Stage 1 text outside that element in `inventory-envelope.md`, replacing the element's attributes/body with `{{template_id}}`, `{{template_revision}}`, and `{{policy_body}}`.
- Put all Stage 2 text outside that element in `extraction-envelope.md` using the same three placeholders.
- Preserve `{{product_json}}`, `{{inventory_schema_json}}`, and `{{candidate_schema_json}}` exactly where the specification places them.

Do not duplicate or paraphrase the prompt prose in Python.

- [ ] **Step 4: Replace Python policy text with system output-contract renderers**

Add these exact interfaces to `src/zdecision/capture/prompts.py`:

```python
INVENTORY_CONTRACT_VERSION = "inventory-v1"
CANDIDATE_CONTRACT_VERSION = "candidate-v1"


def inventory_schema_json() -> str:
    return json.dumps(_inventory_schema(), ensure_ascii=False, indent=2)


def candidate_schema_json(product: str) -> str:
    return json.dumps(_candidate_schema(product), ensure_ascii=False, indent=2)
```

`_inventory_schema()` must use `future_effect`, the three exact enums, `reviewed_retained_context: earliest_to_latest`, and `known_gaps: []`. `_candidate_schema(product)` must use the existing Candidate fields, insert the exact product, and show empty optional arrays.

Keep the existing `build_extraction_prompt()` entry point byte-for-byte compatible in this Task so the current Service and full regression suite remain green. It is temporary local compatibility, is not used by extractor V2, and must be deleted during the Tasks 3–6 atomic cutover before that cutover is committed.

- [ ] **Step 5: Implement strict one-pass rendering and canonical hashes**

Implement `src/zdecision/capture/templates.py` with these public records and method signatures:

```python
@dataclass(frozen=True)
class TemplateSnapshot:
    template_id: str
    revision: int
    title: str
    template_source_sha256: str
    renderer_version: str
    inventory_contract_version: str
    candidate_contract_version: str
    inventory_prompt_sha256: str
    extraction_prompt_sha256: str
    prompt_bundle_sha256: str
    inventory_prompt: str
    extraction_prompt: str

    def to_dict(self) -> dict[str, object]:
        return {"template_id": self.template_id, "revision": self.revision, "title": self.title,
                "template_source_sha256": self.template_source_sha256,
                "renderer_version": self.renderer_version,
                "inventory_contract_version": self.inventory_contract_version,
                "candidate_contract_version": self.candidate_contract_version,
                "inventory_prompt_sha256": self.inventory_prompt_sha256,
                "extraction_prompt_sha256": self.extraction_prompt_sha256,
                "prompt_bundle_sha256": self.prompt_bundle_sha256,
                "inventory_prompt": self.inventory_prompt,
                "extraction_prompt": self.extraction_prompt}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TemplateSnapshot:
        fields = frozenset(("template_id", "revision", "title",
            "template_source_sha256", "renderer_version",
            "inventory_contract_version", "candidate_contract_version",
            "inventory_prompt_sha256", "extraction_prompt_sha256",
            "prompt_bundle_sha256", "inventory_prompt", "extraction_prompt"))
        if frozenset(value) != fields:
            raise ValueError("Invalid TemplateSnapshot fields")
        if not isinstance(value["revision"], int) or isinstance(value["revision"], bool):
            raise ValueError("TemplateSnapshot revision must be an integer")
        strings = {field: value[field] for field in fields - {"revision"}}
        if any(not isinstance(item, str) for item in strings.values()):
            raise ValueError("TemplateSnapshot text fields must be strings")
        snapshot = cls(revision=value["revision"], **strings)
        snapshot.verify_integrity()
        return snapshot

    def verify_integrity(self) -> None:
        inventory_digest = _prompt_digest(
            stage="inventory",
            contract_version=self.inventory_contract_version,
            renderer_version=self.renderer_version,
            prompt=self.inventory_prompt,
        )
        extraction_digest = _prompt_digest(
            stage="extraction",
            contract_version=self.candidate_contract_version,
            renderer_version=self.renderer_version,
            prompt=self.extraction_prompt,
        )
        bundle_digest = hashlib.sha256(canonical_json_bytes({
            "candidate_contract_version": self.candidate_contract_version,
            "extraction_prompt": self.extraction_prompt,
            "inventory_contract_version": self.inventory_contract_version,
            "inventory_prompt": self.inventory_prompt,
            "renderer_version": self.renderer_version,
        })).hexdigest()
        if (inventory_digest != self.inventory_prompt_sha256 or
                extraction_digest != self.extraction_prompt_sha256 or
                bundle_digest != self.prompt_bundle_sha256):
            raise ValueError("TemplateSnapshot prompt digest mismatch")


class TemplateValidationError(ValueError):
    pass


def _prompt_digest(*, stage: str, contract_version: str,
                   renderer_version: str, prompt: str) -> str:
    return hashlib.sha256(canonical_json_bytes({
        "contract_version": contract_version,
        "prompt": prompt,
        "renderer_version": renderer_version,
        "stage": stage,
    })).hexdigest()


class TemplateCatalog:
    def __init__(self, template_root: Path, envelope_root: Path) -> None:
        self.template_root = template_root
        self.envelope_root = envelope_root

    def render(self, template_id: str, product: str) -> TemplateSnapshot:
        manifest, inventory_policy, extraction_policy = self._load(template_id)
        return self._render(manifest, inventory_policy, extraction_policy, product)
```

Use `RENDERER_VERSION = "renderer-v1"` and `^[a-z][a-z0-9_-]{0,63}$`; reject empty products, symlinks, and any referenced file whose parent is not the selected template directory. Reject Unicode control-category characters in product. Limit each policy to 64 KiB and each rendered prompt to 128 KiB. Calculate source, individual-prompt, and bundle hashes from `canonical_json_bytes()` over named dictionaries with their relevant versions, never raw concatenation. The source-hash dictionary contains the exact decoded UTF-8 strings of the raw `manifest.json`, `inventory.md`, and `extract.md`, keyed by fixed role/filename; do not hash a normalized parsed manifest. Rendering and `TemplateSnapshot.from_dict()` must call the same `_prompt_digest()` helper; deserialization recomputes both prompt digests and the bundle digest and rejects stale or tampered values.

- [ ] **Step 6: Package envelope resources and pass renderer tests**

Add:

```toml
[tool.setuptools.package-data]
"zdecision.capture" = ["prompt_contracts/*.md"]
```

Run:

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest tests.test_templates -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: every template test and the unchanged full regression suite pass, and rendering `business` produces two prompts with different stage markers and the exact product.

- [ ] **Step 7: Commit the renderer**

```bash
git add decision-templates pyproject.toml src/zdecision/capture/prompts.py src/zdecision/capture/prompt_contracts src/zdecision/capture/templates.py tests/test_templates.py
git commit -m "feat: add decision compression template renderer"
```

---

### Task 3: Version private Capture state and inventory contracts

**Files:**
- Create: `src/zdecision/capture/inventory.py`
- Create: `tests/test_inventory.py`
- Modify: `src/zdecision/ids.py`
- Modify: `src/zdecision/capture/models.py`
- Modify: `src/zdecision/private_store/filesystem.py`
- Modify: `tests/test_capture.py`

**Interfaces:**
- Consumes: `TemplateSnapshot` from Task 2.
- Produces: `validate_inventory(value) -> InventoryResult`, extractor V2 `capture_operation_id(source_thread_id, source_turn_id, product, template) -> str`, typed V2/legacy records, and inventory persistence used by Tasks 4–6.

- [ ] **Step 1: Write failing inventory contract tests**

Create `tests/test_inventory.py` around this valid fixture:

```python
VALID_INVENTORY = {
    "signals": [{
        "topic": "升级目标",
        "rule": "用户选择实际升级目标规格",
        "future_effect": "后续升级流程不得自动替用户选择",
        "scope": "安恒实例升级",
        "status": "current_confirmed",
        "confirmation_basis": "explicit_user_direction",
        "confidence": "high",
    }],
    "coverage": {
        "reviewed_retained_context": "earliest_to_latest",
        "known_gaps": [],
    },
}


class InventoryValidationTests(unittest.TestCase):
    def test_valid_inventory_round_trips(self) -> None:
        result = validate_inventory(VALID_INVENTORY)
        self.assertEqual("升级目标", result.signals[0].topic)
        self.assertEqual((), result.coverage.known_gaps)

    def test_zero_signals_is_valid(self) -> None:
        value = {"signals": [], "coverage": {
            "reviewed_retained_context": "earliest_to_latest", "known_gaps": []}}
        self.assertEqual((), validate_inventory(value).signals)

    def test_signal_limit_is_checked_before_item_shape(self) -> None:
        value = {"signals": [{}] * 101, "coverage": {
            "reviewed_retained_context": "earliest_to_latest", "known_gaps": []}}
        with self.assertRaisesRegex(InventoryValidationError, "inventory_signal_limit_exceeded"):
            validate_inventory(value)
```

Add exact-field, enum, non-empty-string, non-string-gap, marker, 256-KiB canonical-size, and all-or-nothing cases. Assert that pipe-combined enum strings are rejected. Put a unique secret in an unknown field name and invalid value and assert `InventoryValidationError.message` does not contain it; Task 4 checks the persisted failure metadata too.

- [ ] **Step 2: Write failing identity, persistence, and legacy-read tests**

Extend `tests/test_capture.py` so operation identity receives a rendered snapshot and changes for each of: source task, source Turn, product, template ID, revision, template-source digest, and prompt-bundle digest. Seed one literal extractor-v1 capture JSON with the exact current field set and assert:

```python
loaded = store.get_capture("cap_" + "a" * 32)
self.assertIsInstance(loaded, LegacyCaptureRecord)
self.assertEqual(("cand_old_01",), loaded.candidate_ids)
```

The service-level legacy mutation assertion belongs to Task 4, after the V2 service exists. Here, assert a V2 record round-trips exact frozen prompts, both successful stage digests, stage Turn IDs, and failure metadata without `raw_messages`, `transcript`, or model payload fields. Change a persisted product or operation ID without updating the other identity inputs and assert V2 loading rejects the mismatch. Assert `get_capture(requested_id)` rejects a payload whose internally valid `operation_id` does not equal the requested filename identity. Corrupt Capture, inventory, and Candidate files with malformed JSON and wrong typed fields and assert the store raises a sanitized `PrivateStateCorrupt` rather than leaking `JSONDecodeError`, `ValueError`, or raw contents.

- [ ] **Step 3: Run the new model tests and verify contract failures**

Run:

```bash
.venv/bin/python -m unittest tests.test_inventory tests.test_capture.CaptureModelTests -v
```

Expected: FAIL because inventory types, V2 ID parameters, and V2 record fields do not exist.

- [ ] **Step 4: Implement the typed inventory contract**

Create these immutable types in `inventory.py`:

```python
SignalStatus = Literal["current_confirmed", "unresolved", "superseded"]
ConfirmationBasis = Literal[
    "explicit_user_confirmation",
    "explicit_user_direction",
    "adopted_decision_contract",
    "uncertain",
]
SignalConfidence = Literal["high", "medium", "low"]

@dataclass(frozen=True)
class DecisionSignal:
    topic: str
    rule: str
    future_effect: str
    scope: str
    status: SignalStatus
    confirmation_basis: ConfirmationBasis
    confidence: SignalConfidence

    def to_dict(self) -> dict[str, object]:
        return {"topic": self.topic, "rule": self.rule,
                "future_effect": self.future_effect, "scope": self.scope,
                "status": self.status,
                "confirmation_basis": self.confirmation_basis,
                "confidence": self.confidence}

@dataclass(frozen=True)
class InventoryCoverage:
    reviewed_retained_context: Literal["earliest_to_latest"]
    known_gaps: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"reviewed_retained_context": self.reviewed_retained_context,
                "known_gaps": list(self.known_gaps)}

@dataclass(frozen=True)
class InventoryResult:
    signals: tuple[DecisionSignal, ...]
    coverage: InventoryCoverage

    def to_dict(self) -> dict[str, object]:
        return {"signals": [signal.to_dict() for signal in self.signals],
                "coverage": self.coverage.to_dict()}
```

`validate_inventory()` must check top-level count first, canonical bytes second, then exact nested fields and values. Raise `InventoryValidationError(code, message)` with stable codes `inventory_signal_limit_exceeded`, `inventory_output_too_large`, or `invalid_inventory`.

- [ ] **Step 5: Introduce extractor V2 identity**

Change `capture_operation_id` to:

```python
CAPTURE_EXTRACTOR_VERSION = "extractor-v2"

def capture_operation_id(
    source_thread_id: str,
    source_turn_id: str,
    product: str,
    template: TemplateSnapshot,
) -> str:
    payload = canonical_json_bytes({
        "extractor_version": CAPTURE_EXTRACTOR_VERSION,
        "product": product,
        "prompt_bundle_sha256": template.prompt_bundle_sha256,
        "source_thread_id": source_thread_id,
        "source_turn_id": source_turn_id,
        "template_id": template.template_id,
        "template_revision": template.revision,
        "template_source_sha256": template.template_source_sha256,
    })
    return f"cap_{hashlib.sha256(payload).hexdigest()[:32]}"
```

Keep a private `legacy_capture_operation_id()` only if a test needs to construct an old fixture; new prepare code must never call it.

- [ ] **Step 6: Add V2 state records and read-only legacy loading**

Use these exact V2 statuses:

```python
CaptureStatus = Literal[
    "prepared", "fork_attached", "inventory_running",
    "inventory_completed", "extraction_running", "completed", "failed",
]
StageName = Literal["inventory", "extraction"]

@dataclass(frozen=True)
class StageFailure:
    stage: StageName
    code: str
    message: str
    output_sha256: str | None

@dataclass(frozen=True)
class CaptureRecord:
    record_version: Literal[2]
    operation_id: str
    source: SourceCheckpoint
    product: str
    template: TemplateSnapshot
    status: CaptureStatus
    fork_thread_id: str | None
    inventory_turn_id: str | None
    extraction_turn_id: str | None
    inventory_sha256: str | None
    extraction_sha256: str | None
    failure: StageFailure | None
    candidate_ids: tuple[str, ...]
    created_at: str
    updated_at: str

@dataclass(frozen=True)
class CapturePlan:
    record: CaptureRecord
    inventory_prompt: str
    extraction_prompt: str
    replayed: bool

@dataclass(frozen=True)
class CandidateSet:
    operation_id: str
    status: Literal["completed"]
    candidate_ids: tuple[str, ...]
    extraction_sha256: str
```

`CaptureRecord.to_dict()` is the complete private representation including the frozen snapshot. `CaptureRecord.from_dict()` enforces exact fields and legal status/field combinations, recomputes extractor V2 operation identity from the source, product, and snapshot, and rejects a mismatch. `CaptureRecord.public_dict()` contains stage/status/ID/digest/failure metadata but omits both prompt bodies. Add `LegacyCaptureRecord` with the exact extractor-v1 persisted fields and `record_version` exposed as `1` only in memory. `FilePrivateStore.get_capture()` chooses the legacy parser only when the old exact key set is present, and rejects any loaded record whose internal operation ID differs from the requested object ID. No mutation method accepts `LegacyCaptureRecord`.

Define `PrivateStateCorrupt(collection, object_id)` and make every private-store reader wrap malformed UTF-8/JSON and typed-record `ValueError` in that sanitized exception without embedding file contents or parser excerpts. Missing objects remain the existing not-found/`None` path; corrupt objects are never treated as missing.

Add `put_inventory(operation_id, inventory)` and `get_inventory(operation_id)` under the private `inventories/` collection. Writing an already-present inventory with identical canonical bytes is idempotent; different bytes for the same operation raise a private-state conflict instead of replacing the artifact.

- [ ] **Step 7: Pass model and inventory tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_inventory tests.test_capture.CaptureModelTests -v
```

Expected: all tests pass; no private JSON contains a transcript field or invalid model payload.

- [ ] **Step 8: Keep the state changes uncommitted for the atomic cutover**

```bash
git diff --check
git status --short
```

Expected: the scoped model/inventory tests pass and the intended Task 3 files are modified, but no commit is created yet. Continue directly to Task 4; the public workflow remains extractor V1 until the Tasks 3–6 atomic cutover is complete.

---

### Task 4: Implement the two-stage Capture service

**Files:**
- Modify: `src/zdecision/capture/service.py`
- Modify: `src/zdecision/capture/__init__.py`
- Modify: `tests/test_capture.py`

**Interfaces:**
- Consumes: `TemplateCatalog.render`, V2 records, `validate_inventory`, and private inventory persistence.
- Produces: the domain methods used by the CLI: `prepare`, `resume`, `attach_fork`, `attach_stage_turn`, `complete_inventory`, `complete_extraction`, `record_invalid_json`, `record_stage_failure`, `get`, and integrity-checked `get_inventory`.

- [ ] **Step 1: Write failing prepare and snapshot replay tests**

Update test setup to inject the real repository catalog:

```python
self.catalog = TemplateCatalog(TEMPLATE_ROOT, ENVELOPE_ROOT)
self.service = CaptureService(self.store, self.catalog)
```

Assert `prepare("thread-a", "turn-7", "安恒", "business")` returns both exact prompts and stores the snapshot before fork. Assert `resume(operation_id)` returns those same prompt bytes after a copied template file changes, while a new `prepare` after that change intentionally produces a different operation ID. For `inventory_completed` and every later V2 state, deleting or changing the inventory artifact makes both `resume()` and replay through `prepare()` raise `CaptureStateError` before they return an extraction prompt.

- [ ] **Step 2: Write the failing state-transition matrix tests**

Add tests for this exact successful sequence:

```python
plan = service.prepare("thread-a", "turn-7", "anheng", "business")
service.attach_fork(plan.record.operation_id, "thread-fork")
service.attach_stage_turn(plan.record.operation_id, "inventory", "turn-inventory")
service.complete_inventory(plan.record.operation_id, VALID_INVENTORY)
service.attach_stage_turn(plan.record.operation_id, "extraction", "turn-extract")
result = service.complete_extraction(plan.record.operation_id, {"candidates": []})
self.assertEqual("completed", result.status)
```

For every transition, assert the same external ID is idempotent and a different fork/Turn ID raises a conflict. Assert extraction cannot attach before valid inventory, inventory cannot complete before its Turn is attached, and a failed record cannot restart.

- [ ] **Step 3: Write failing validation/failure/replay tests**

Add concrete assertions that:

- invalid inventory records `failed`, `stage=inventory`, sanitized error code, and output digest, writes no inventory, and never permits extraction;
- model-authored secret strings placed in unknown inventory keys or invalid values appear in neither failure metadata nor raised validation messages;
- a valid inventory is stored once, exposes only `known_gaps`, and a completion retry returns the stored digest without rewriting it;
- deleting or changing a required stored inventory makes `complete_extraction()` and `capture show` fail with `CaptureStateError`; neither path substitutes `known_gaps=[]`;
- 21 Candidates records `candidate_limit_exceeded`, writes no Candidate files, and count checking happens before Candidate field validation;
- any invalid Candidate records an extraction failure while preserving the valid Stage 1 inventory;
- model-authored secret strings placed in unknown Candidate keys or invalid values appear in neither failure metadata nor raised validation messages;
- a completed extraction replays its stored Candidate IDs and extraction digest even if retry input differs;
- zero Candidates completes successfully;
- `prepare` on `failed` returns the same terminal record with `replayed=True` and never creates a replacement operation;
- mutating a `LegacyCaptureRecord` raises `CaptureStateError`, while `get()` can return it for display.

- [ ] **Step 4: Run the service tests and verify state failures**

Run:

```bash
.venv/bin/python -m unittest tests.test_capture.CaptureServiceTests -v
```

Expected: FAIL on missing catalog injection, stage methods, and V2 status transitions.

- [ ] **Step 5: Implement prepare, attachment, and replay**

Use these signatures:

```python
class CaptureService:
    def __init__(self, store: FilePrivateStore, catalog: TemplateCatalog) -> None:
        self.store = store
        self.catalog = catalog

    def prepare(self, source_thread_id: str, source_turn_id: str,
                product: str, template_id: str = "business") -> CapturePlan:
        snapshot = self.catalog.render(template_id, product)
        operation_id = capture_operation_id(
            source_thread_id, source_turn_id, product, snapshot)
        existing = self.store.get_capture(operation_id)
        if existing is None:
            record = CaptureRecord.started(operation_id, SourceCheckpoint(
                source_thread_id, source_turn_id), product, snapshot)
            self.store.put_capture(record)
            return CapturePlan(record, snapshot.inventory_prompt,
                               snapshot.extraction_prompt, False)
        return self._replayed_plan(existing)

    def resume(self, operation_id: str) -> CapturePlan:
        record = self._required_v2_capture(operation_id)
        if record.inventory_sha256 is not None:
            self._verified_inventory(record)
        return CapturePlan(record, record.template.inventory_prompt,
                           record.template.extraction_prompt, True)

    def attach_stage_turn(self, operation_id: str, stage: StageName,
                          turn_id: str) -> CaptureRecord:
        record = self._required_v2_capture(operation_id)
        return self._attach_stage_turn(record, stage, turn_id)

    def complete_inventory(self, operation_id: str,
                           output: object) -> CaptureRecord:
        return self._complete_inventory(self._required_v2_capture(operation_id), output)

    def complete_extraction(self, operation_id: str,
                            output: object) -> CandidateSet:
        return self._complete_extraction(self._required_v2_capture(operation_id), output)

    def get(self, operation_id: str) -> CaptureRecord | LegacyCaptureRecord:
        return self._required_capture(operation_id)

    def get_inventory(self, operation_id: str) -> InventoryResult | None:
        record = self._required_v2_capture(operation_id)
        return self._verified_inventory(record)
```

Preserve the existing prepared/fork ambiguity rule. Replay prompts only from the persisted snapshot, never from the live catalog. `_replayed_plan()` applies the same `_verified_inventory()` precondition as `resume()` whenever an inventory digest exists, so corrupted private state cannot yield a Stage 2 prompt or trigger a new native Turn.

- [ ] **Step 6: Implement inventory completion and terminal failure**

`complete_inventory()` must:

1. require `inventory_running`;
2. calculate the canonical output digest;
3. validate the whole result;
4. write the typed inventory;
5. update the record to `inventory_completed` with its digest;
6. on validation error, store only `StageFailure` and then re-raise the typed error.

If the inventory artifact already exists because the process stopped between the artifact write and the Capture-record update, identical canonical bytes finish the same transition; different bytes raise a state conflict. This is reconciliation of one recorded Turn, not a new model attempt.

Expose:

```python
def record_invalid_json(self, operation_id: str, stage: StageName,
                        output_sha256: str) -> CaptureRecord:
    return self._fail(operation_id, stage, "invalid_json",
                      "Stage output was not valid JSON", output_sha256)

def record_stage_failure(self, operation_id: str, stage: StageName,
                         code: Literal["model_refusal", "model_timeout",
                                       "native_unavailable", "model_contract_violation"],
                         output_sha256: str | None = None) -> CaptureRecord:
    return self._fail(operation_id, stage, code, _FAILURE_MESSAGES[code], output_sha256)
```

Do not accept an arbitrary failure message from CLI or Skill.

`_fail()` accepts only the stage that is currently eligible: inventory from `fork_attached` or `inventory_running`, extraction from `inventory_completed` or `extraction_running`. A repeated failure with the same stage, code, and digest returns the terminal record unchanged; any different failure request against `failed` raises a state conflict. Completed and legacy records are immutable. `invalid_json`, `model_refusal`, `model_timeout`, and `model_contract_violation` require the corresponding running state and attached Turn ID; only `native_unavailable` may also fail the eligible pre-Turn state because no Turn ID may exist.

- [ ] **Step 7: Implement Candidate completion without changing Candidate shape**

Rename the current `complete()` path to `complete_extraction()`. Reuse its exact Candidate field/product/16-KiB validation, but require `extraction_running`, reload the private inventory and verify its canonical digest against `record.inventory_sha256`, check the 20-item limit first, calculate and persist `extraction_sha256`, record a sanitized failure on any invalid result, and write all Candidates only after every item validates. A missing, malformed, or digest-mismatched inventory is private-state corruption and raises `CaptureStateError`; it is not converted into a model failure. Make `ExtractionValidationError(code, message)` expose stable codes: `candidate_limit_exceeded`, `candidate_item_too_large`, or `invalid_extraction`; the CLI emits that code rather than flattening every failure.

- [ ] **Step 8: Pass service tests and the existing model suite**

Run:

```bash
.venv/bin/python -m unittest tests.test_capture tests.test_inventory tests.test_templates -v
```

Expected: all Capture, inventory, and template tests pass.

- [ ] **Step 9: Keep the Service changes in the same uncommitted cutover**

```bash
git diff --check
git status --short
```

Expected: the new domain tests pass and the Task 3–4 changes remain together in the working tree. Continue directly to Task 5 so no commit exposes a V2 Service behind the old CLI.

---

### Task 5: Expose the two-stage internal command boundary

**Files:**
- Modify: `src/zdecision/cli.py`
- Modify: `tests/test_cli_capture.py`

**Interfaces:**
- Consumes: all Task 4 service methods.
- Produces: one-line JSON envelopes for the repository Skill; no user-facing CLI workflow.

- [ ] **Step 1: Replace one-stage CLI tests with a full two-stage fixture**

Make the test helper execute this exact sequence:

```python
operation_id = prepare(template_id="business")
run_cli(["capture", "attach", "--operation-id", operation_id,
         "--fork-thread-id", "thread-fork"], state_dir=state_dir)
run_cli(["capture", "attach-turn", "--operation-id", operation_id,
         "--stage", "inventory", "--turn-id", "turn-inventory"], state_dir=state_dir)
run_cli(["capture", "complete-inventory", "--operation-id", operation_id,
         "--input", "-"], stdin=json.dumps(VALID_INVENTORY), state_dir=state_dir)
run_cli(["capture", "attach-turn", "--operation-id", operation_id,
         "--stage", "extraction", "--turn-id", "turn-extraction"], state_dir=state_dir)
run_cli(["capture", "complete-extraction", "--operation-id", operation_id,
         "--input", "-"], stdin=json.dumps({"candidates": []}), state_dir=state_dir)
```

Assert prepare returns one copy each of `inventory_prompt` and `extraction_prompt`, template title/ID/revision/digest, public record metadata, and `replayed`. Assert `capture resume --operation-id ID` returns the frozen prompts without reading the live template catalog. Assert show returns Candidate data, template display metadata, and Stage 1 `known_gaps`, but not full inventory signals or rendered prompts. Attach, completion, failure, and show responses use `public_dict()` and never repeat the frozen prompt bodies.

- [ ] **Step 2: Add CLI failure and legacy display tests**

Add tests for stable error envelopes/codes:

- malformed Stage 1 stdin -> exit 2, `invalid_json`, persisted failed record;
- valid JSON with a non-object Stage 1 or Stage 2 root -> exit 2, the stage's typed validation code, persisted failed record;
- 101 signals -> exit 2, `inventory_signal_limit_exceeded`;
- canonical Stage 1 output above 256 KiB -> exit 2, `inventory_output_too_large`;
- 21 Candidates -> exit 2, `candidate_limit_exceeded`;
- invalid stage order -> exit 4, `capture_action_required`;
- conflicting fork/Turn attach -> exit 5;
- model timeout or model contract violation through `fail-stage` -> exit 0 with terminal failed record;
- malformed `--output-sha256` -> exit 2, `invalid_arguments`, without changing the running record;
- old extractor-v1 completed record -> `capture show` succeeds with `legacy: true` and existing Candidates;
- old record mutation -> exit 4.
- unknown or invalid template -> exit 2, `invalid_template`, with no Capture record or native work.
- malformed or typed-invalid private Capture/inventory/Candidate JSON -> exit 3, `private_state_invalid`, exactly one JSON stdout envelope, and no traceback or raw file content.

- [ ] **Step 3: Run the CLI tests and verify missing commands**

Run:

```bash
.venv/bin/python -m unittest tests.test_cli_capture -v
```

Expected: FAIL because `--template-id`, `attach-turn`, `complete-inventory`, `complete-extraction`, and `fail-stage` are absent.

- [ ] **Step 4: Implement the parser and service wiring**

Add these internal actions:

```text
capture prepare --thread-id ID --turn-id ID --product PRODUCT [--template-id business]
capture resume --operation-id ID
capture attach --operation-id ID --fork-thread-id ID
capture attach-turn --operation-id ID --stage inventory|extraction --turn-id ID
capture complete-inventory --operation-id ID --input -
capture complete-extraction --operation-id ID --input -
capture fail-stage --operation-id ID --stage inventory|extraction --code model_refusal|model_timeout|native_unavailable|model_contract_violation [--output-sha256 HEX]
capture show --operation-id ID
```

When present, `--output-sha256` must match exactly `^[0-9a-f]{64}$`; reject uppercase, truncated, overlong, or non-hex values as `invalid_arguments` before mutating private state.

Build `TemplateCatalog` from the repository `decision-templates/` directory and package `prompt_contracts/` directory. Permit `ZDECISION_TEMPLATE_ROOT` only as an internal test/development override; never document it as user UX. Catch `TemplateValidationError` as exit 2 / `invalid_template` so template failure still emits exactly one JSON stdout envelope and stops before a Capture record or native effect.

Catch `InventoryValidationError` and `ExtractionValidationError` separately and emit each exception's stable `.code`. Do not flatten typed contract failures into one generic CLI error.

Catch `PrivateStateCorrupt` as exit 3 / `private_state_invalid`. Its user-facing message may identify the collection and safe object ID, but must not include the underlying parser message or private file contents.

Delete the extractor-v1-only `build_extraction_prompt()` and old `capture complete` action now that the V2 Service and CLI are switched together. Legacy persisted records remain readable; legacy execution entry points do not remain callable.

- [ ] **Step 5: Hash raw stdin and record invalid JSON without persisting it**

Refactor input handling so raw text is hashed before decoding, then pass every successfully decoded JSON value—including a non-object root—to the service for terminal schema validation:

```python
def _read_json_text(input_name: str, stdin: TextIO) -> tuple[str, str]:
    text = stdin.read() if input_name == "-" else Path(input_name).read_text("utf-8")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decode_json(text: str) -> object:
    return json.loads(text, parse_constant=_reject_json_constant)
```

If `_decode_json()` fails, call `service.record_invalid_json(operation_id, stage, digest)` before emitting the one-line `invalid_json` envelope. Do not reject a decoded non-object in the CLI: `complete_inventory()` or `complete_extraction()` must record its typed terminal validation failure. Do not include raw text in stdout, stderr, or private state.

- [ ] **Step 6: Implement safe show output**

For V2, first load `inventory = service.get_inventory(operation_id)`, then return exactly:

```python
{
    "record": record.public_dict(),
    "template": {
        "template_id": record.template.template_id,
        "revision": record.template.revision,
        "title": record.template.title,
        "content_digest": record.template.template_source_sha256[:12],
    },
    "known_gaps": list(inventory.coverage.known_gaps) if inventory else [],
    "candidates": candidates,
}
```

`public_dict()` must omit frozen prompt bodies and include both successful stage digests when present. Prepare and resume may return the prompts because the Skill needs them; show must not. Whenever `record.inventory_sha256` is present, show must require a typed inventory artifact whose canonical digest matches it before reading `known_gaps`; missing or mismatched state raises `CaptureStateError` instead of being represented as an empty list.

Build prepare/resume envelopes from `record.public_dict()` plus a compact template display object, `replayed`, and the two top-level exact prompt strings. Do not serialize `record.to_dict()` into CLI output, because its private snapshot would duplicate both prompts and leak them from unrelated commands.

- [ ] **Step 7: Pass all CLI and domain tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_cli_capture tests.test_capture tests.test_inventory tests.test_templates -v
```

Expected: all tests pass and every CLI call emits exactly one JSON object on stdout.

- [ ] **Step 8: Keep the CLI cutover uncommitted until the Skill matches it**

```bash
git diff --check
git status --short
```

Expected: the state, Service, and CLI tests pass, but Tasks 3–5 remain in the working tree. Continue directly to Task 6; the old conversation-first Skill must never be committed against the new CLI.

---

### Task 6: Route Codex through both exact prompts and verify the slice

**Files:**
- Modify: `.agents/skills/zdecision/SKILL.md`
- Modify: `.agents/skills/zdecision/references/capture.md`
- Modify: `tests/test_skill_contract.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 5 JSON commands and Codex App native `read_thread`, `fork_thread`, `send_message_to_thread`, and `wait_threads` tools.
- Produces: the conversation-first Capture workflow visible to users.

- [ ] **Step 1: Write failing Skill contract tests for both stages**

Replace the one-stage phrase-order assertion with:

```python
ordered_phrases = (
    "capture prepare",
    "fork_thread",
    "capture attach",
    "inventory_prompt",
    "--stage inventory",
    "complete-inventory",
    "extraction_prompt",
    "--stage extraction",
    "complete-extraction",
    "capture show",
)
positions = [text.index(phrase) for phrase in ordered_phrases]
self.assertEqual(sorted(positions), positions)
```

Add assertions for: default `business`; explicit template-ID passthrough; `capture resume` for frozen mid-run prompts; exact/verbatim prompts; Stage 2 immediately after validated Stage 1 in the same fork; no tools/pagination from either extraction Turn; replay handling for every V2 status; controller wait timeouts reconcile rather than call `fail-stage`; corrupted/missing inventory stops at `capture resume` before Stage 2; no repair prompt after invalid JSON; failed operations never re-fork; `known_gaps` and template identity shown; zero Candidates valid; no raw source copied to stdin/Git.

- [ ] **Step 2: Run Skill tests and verify the one-stage reference fails**

Run:

```bash
.venv/bin/python -m unittest tests.test_skill_contract -v
```

Expected: FAIL because the current reference has only `extraction_prompt` and `capture complete`.

- [ ] **Step 3: Rewrite the Capture reference as the exact V2 state machine**

Keep the current bootstrap, completed-boundary selection, fork attachment, boundary verification, and `turnLimit: 10` rules. Replace the one extraction Turn with this sequence:

1. prepare with the user-selected template, defaulting to `business`;
2. reconcile the exact frozen `inventory_prompt` in the attached fork;
3. send it verbatim only when no matching Turn exists;
4. read the resulting Turn ID and persist it with `attach-turn --stage inventory`;
5. wait for the final JSON and call `complete-inventory` over stdin;
6. if the Turn used a tool or produced non-final processing output, record `model_contract_violation` and stop; otherwise stop on any definite terminal failure without repair, extra wording, pagination, or a new fork; a `wait_threads` timeout or uncertain tool result leaves the operation running for reconciliation and must not be recorded as `model_timeout`;
7. reconcile and send the exact `extraction_prompt` as the immediately next Turn;
8. persist its Turn ID with `attach-turn --stage extraction`;
9. wait and call `complete-extraction` over stdin;
10. call `capture show` and present private Candidates, template identity, and known gaps.

Include a status table with exact continuation behavior for `prepared`, `fork_attached`, `inventory_running`, `inventory_completed`, `extraction_running`, `completed`, `failed`, and legacy completed records. Every continuation after initial prepare begins with `capture resume --operation-id ID`, so a live template edit cannot replace frozen prompts.

- [ ] **Step 4: Update root routing and README without making CLI user-facing**

Add natural-language examples:

```markdown
- “压缩任务 `<task-id>` 的决策。” uses the default 业务决策压缩模板.
- “用模板 ID `architecture` 处理任务 `<task-id>`。” selects that installed template by stable ID.
```

State that adding a template means copying a template directory, assigning its stable ID/title/revision, and editing its two policy files. Selection is by stable ID in V1; `title` is display metadata, not an alias. Do not claim title-based lookup and do not list internal command syntax in README.

- [ ] **Step 5: Pass the complete deterministic suite**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
git diff --check
```

Expected: all tests pass; `git diff --check` exits 0.

- [ ] **Step 6: Perform a private model-backed acceptance run**

From a Codex conversation, use a user-approved completed task and the default business template. Verify without committing any task ID, prompt output, inventory, or Candidate payload. Evaluate only scenario claims actually present in that task; mark every absent scenario below as separately pending rather than treating one run as evidence for it:

- Stage 1 and Stage 2 run in one fresh fork with their exact frozen prompts and no tool calls.
- Stage 2 emits only Candidates corresponding to Stage 1 signals.
- simple Bug completion, implementation/testing/delivery process, and pure visual details are absent;
- confirmed durable business rules discovered during Bug work may remain;
- any intersecting or uncertain `known_gaps` signal is omitted;
- attributed compacted user confirmation can qualify as `high` confidence;
- template title/ID/revision/digest and known gaps are visible in the controlling conversation;
- retrying the completed operation creates no fork, Turn, or Candidate duplicate.

If the user defers this private run, report deterministic implementation as complete but model-quality acceptance as pending. If the chosen task lacks known-gap, compacted-attribution, Bug-discovery, or visual/process-noise examples, report those individual quality checks as pending. Do not manufacture fixture evidence.

- [ ] **Step 7: Commit the atomic V2 Capture cutover**

```bash
git add decision-templates pyproject.toml src/zdecision .agents/skills/zdecision README.md tests/test_templates.py tests/test_inventory.py tests/test_capture.py tests/test_cli_capture.py tests/test_skill_contract.py
git commit -m "feat: route capture through decision templates"
```

Expected: Tasks 3–6 land together only after the V2 state, Service, CLI, Skill, README, and contract tests agree. The Task 2 renderer files may already be tracked; adding them again is harmless and ensures no cutover file is omitted.

- [ ] **Step 8: Final repository verification and push**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
git diff --check
git status --short
git log --oneline -6
git push origin main
```

Expected: all tests pass, no diff errors, the worktree is clean, the three scoped implementation commits are visible, and `origin/main` matches local `main`.
