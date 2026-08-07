# Task 5 implementation report

## Status

Complete. The deterministic 13-case acceptance matrix passes, the focused
vertical suites pass, the complete suite passes, and the six-statement hard
stop audit found no unavailable host capability. Task 8 has not started.

## TDD evidence

RED command:

```text
.venv/bin/python -m unittest tests.test_recall_capture_isolation -v
```

Initial result: `Ran 13 tests`; 12 passed and case 10 failed. The failure was
the expected integration setup debt:
`RequestedCaptureRunner.__init__()` lacked `evidence_ledger` and
`recall_host_store` in the on-demand vertical fixture.

The fixture correction then exposed two more v5 integration mismatches. The
fake host still emitted legacy Inventory/Extraction shapes, and the
multi-leaf restart path used an unscoped legacy fallback lookup after more
than one v5 slice operation existed. The latter was a real Tasks 3-4 wiring
defect rather than a fixture assertion: the unscoped lookup raised
`CaptureOperationCorrupt` and left later leaf slices planned.

The narrow correction:

- opens one shared `RecallHostStore` beside the integration `AgentDatabase`,
  injects it into Capture and reconciliation, and closes it on restart;
- makes the fake host copy only the v5 receipt and signal-ordinal enums that
  the real schemas supply;
- uses an explicit legacy-only operation lookup for the immutable v3/v4
  fallback, so multiple v5 leaf operations are valid while any actual legacy
  owner still blocks a new v5 sibling; and
- updates the manually constructed abandoned-generation fixture to use its
  frozen v5 manifest and sidecars.

The existing four-leaf restart test is the regression for protocol-scoped
lookup ordering. It now completes every leaf after the first receipt crash.

Final matrix result: `Ran 13 tests ... OK`.

## Acceptance coverage

`tests/test_recall_capture_isolation.py` contains exactly 13 deterministic
cases covering unanchored recalled/probe and non-Prompt context, independent
explicit directions, identical-text source distinction, invalid receipt
sets, receipt-free model claims, Extraction immutability, reconciliation
provenance preservation, noneligible dispositions, restart byte identity,
internal-Thread Recall denial, the fixed **继续** semantic corpus, and the
Central privacy boundary.

The retry/restart vertical compares the exact frozen manifest bytes, signal
and Candidate sidecar bytes, committed result digest, and local outbox bytes
before and after reopening local state. The Central vertical now uploads a
`candidate-provenance-v1` slice and scans Central SQLite, HTTP fixtures, and
Git blobs for sentinels representing `session_id`, `turn_id`, `hook_event_id`,
`receipt_id`, `active_reference_set_digest`, raw Prompt, source path,
transcript, and reference Decision IDs.

## Verification

Focused command:

```text
.venv/bin/python -m unittest \
  tests.test_recall_capture_isolation \
  tests.integration.test_on_demand_capture_core \
  tests.integration.test_central_web_vertical -v
```

Result: `Ran 36 tests in 12.967s` — `OK`.

Complete-suite command, run exactly once after focused GREEN:

```text
.venv/bin/python -m unittest discover -s tests -v
```

Result: `Ran 838 tests` — `OK (skipped=3)`. The skips are the existing
explicit live-host acceptances; no marker filtering was used.

## Hard-stop audit

All six statements are true:

1. Anchor association reads only `AgentDatabase.prompt_anchors_between()` and
   the frozen lower/upper Stop event IDs, ordered by the Hook ledger row ID.
2. Capture provenance has no rollout-data or transcript-filename fallback;
   the transcript-path vertical remains unread and absent from persistence.
3. `SourceEvidenceUnavailable` excludes the source once, produces no fork or
   model Turn, and is not reinvoked in later slices.
4. v5 operations require their frozen manifest and v2 sidecars; the explicit
   legacy-only fallback cannot reinterpret or silently downgrade v5.
5. `candidate-provenance-v1` slice batches require provenance on every item,
   and the production processor emits that protocol for v5 results.
6. Legacy root/slice batches omit the protocol and provenance together; they
   remain readable as legacy and reject provenance-bearing legacy payloads.

Therefore the hard-stop condition is false and
`capture_evidence_provenance_unavailable` was not recorded.

## Concerns

No blocking concerns. The complete suite still emits the existing
FastAPI/TestClient deprecation warning and one pre-existing SQLite
`ResourceWarning`; neither corresponds to a test failure. No real Codex
Desktop acceptance was attempted.

## Fix round 1

Status: complete. This round strengthens only acceptance tests and fixtures;
production code is unchanged and Task 8 remains unstarted.

The amended cases now provide the missing evidence:

- cases 1-4 use a deterministic semantic corpus model that consumes recalled,
  probe, assistant, tool, code, Capture-artifact, compaction, and Hook Prompt
  channels together with a real ledger-derived, Stop-bounded host manifest;
  non-Prompt sources produce zero validated Extraction observations, while an
  independently receipted Prompt produces one Candidate. Identical recalled
  and Prompt text differs only by channel and receipt authority;
- case 5 records real prompts in the source Session, another Session, and
  after the frozen source Stop. Unknown, duplicate, reordered, cross-Session,
  post-boundary, and malformed forged receipt outputs each traverse
  `RequestedCaptureRunner`, terminate with `capture_provenance_invalid`, leave
  one abandoned/archived attempt, and cannot start a second model attempt;
- case 9 carries adoption, `needs_evidence`, and every excluded disposition to
  reconciliation/outbox/upload, proving zero reconciliation calls, zero
  Candidate revisions, and two empty v1 staged/uploaded slice batches;
- case 12 consumes an actual recalled rule plus a Hook-observed `继续` Prompt
  and produces `needs_evidence`; its comment explicitly scopes this as model
  quality rather than host proof; and
- the Central vertical sends every forbidden field/value independently, then
  separately scans accepted normalized v1 revision records, Central SQLite,
  accepted HTTP responses, and Git blobs for every sentinel value.

Amended RED command:

```text
.venv/bin/python -m unittest \
  tests.test_recall_capture_isolation.RecallCaptureIsolationTest.test_01_recalled_decision_or_host_probe_without_anchor_yields_zero_candidates \
  tests.test_recall_capture_isolation.RecallCaptureIsolationTest.test_02_non_prompt_sources_alone_yield_zero_candidates \
  tests.test_recall_capture_isolation.RecallCaptureIsolationTest.test_03_anchored_explicit_direction_qualifies_with_recalled_context \
  tests.test_recall_capture_isolation.RecallCaptureIsolationTest.test_04_identical_recalled_and_prompt_text_is_distinguished_by_receipt \
  tests.test_recall_capture_isolation.RecallCaptureIsolationTest.test_05_invalid_receipt_sets_fail_the_complete_attempt \
  tests.test_recall_capture_isolation.RecallCaptureIsolationTest.test_09_noneligible_dispositions_upload_no_candidate_content \
  tests.test_recall_capture_isolation.RecallCaptureIsolationTest.test_12_recalled_rule_plus_unrelated_continue_anchor_needs_evidence \
  tests.integration.test_central_web_vertical.CentralWebVerticalTest.test_theme_review_preview_and_explicit_publish_use_v1_partition -v
```

Result: `Ran 8 tests` — `FAILED (failures=8)`. Each failure was the intended
missing acceptance boundary; no production failure was exposed.

Amended specific GREEN used the same command and passed: `Ran 8 tests in
2.022s` — `OK`. The complete 13-case isolation module then passed: `Ran 13
tests in 2.679s` — `OK`.

Mandated focused command:

```text
.venv/bin/python -m unittest \
  tests.test_recall_capture_isolation \
  tests.integration.test_on_demand_capture_core \
  tests.integration.test_central_web_vertical -v
```

Result: `Ran 36 tests in 13.150s` — `OK`.

Per the fix-round instruction, the complete 838-test suite was not rerun for
these test-only changes. The one mandated production baseline remains the
earlier `.venv/bin/python -m unittest discover -s tests -v` result: `Ran 838
tests` — `OK (skipped=3)`.
