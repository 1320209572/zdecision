# Task 2 implementation report

## RED

Command:

```bash
.venv/bin/python -m unittest tests.test_inventory tests.test_capture tests.test_capture_operation tests.test_templates -v
```

Result: failed as expected with five errors. The v5 inventory and extraction
validators did not exist, `FrozenCaptureInput.create()` did not accept an
evidence manifest, and the structured-output schemas did not accept bounded
receipt or signal-ordinal enums.

## GREEN

The same command completed successfully: `Ran 116 tests ... OK`.

`git diff --check` and:

```bash
.venv/bin/python -m compileall -q src/zdecision tests/test_inventory.py tests/test_capture.py tests/test_capture_operation.py tests/test_templates.py
```

also completed successfully.

## Contract review

- v3/v4 frozen inputs and six-field v1 results retain their original field
  shapes; v5 requires a manifest and produces result v2 sidecars.
- Inventory receipt membership, uniqueness, manifest order, ordinal sequence,
  and host-derived reference digests are validated before disposition.
- Extraction accepts only eligible signal ordinals once and host-copies the
  selected signal receipt set; it accepts no model-authored provenance fields.
- No reference Decision ID is inferred from model or envelope text; Task 2
  writes an empty `reference_decision_ids` tuple.
- Result loading rechecks the frozen operation and manifest, then rederives
  every signal sidecar from the serialized Inventory and manifest before
  admitting an eligible Candidate.
- Task 3 runner integration remains deliberately out of scope.

## Fix round 1

### RED

Command:

```bash
.venv/bin/python -m unittest tests.test_capture_operation.CaptureOperationStoreTests.test_v5_operation_rejects_a_legacy_result_before_persistence -v
```

Result: failed as expected. A v5 frozen operation accepted a canonical,
six-field v1 result before persistence because the durable store parsed the
result without its frozen input.

### GREEN

Commands:

```bash
.venv/bin/python -m unittest tests.test_inventory tests.test_capture tests.test_capture_operation tests.test_templates -v
git diff --check
.venv/bin/python -m compileall -q src/zdecision tests/test_inventory.py tests/test_capture.py tests/test_capture_operation.py tests/test_templates.py
```

Result: `Ran 123 tests ... OK`; diff and compilation checks completed with exit
code 0.

The durable store now supplies the owning frozen input while validating staged,
committed, and reloaded results. Regression coverage proves both v5/v1 and
legacy/v2 results fail before persistence, while v3/v4 frozen bytes round-trip
exactly. Additional focused coverage exercises receipt ordering and manifest
membership, host dispositions, eligible ordinal uniqueness, and multi-receipt
sidecar order.

## Fix round 2

### RED

Command:

```bash
.venv/bin/python -m unittest tests.test_capture_operation.FrozenCaptureInputTests.test_historical_v3_and_v4_frozen_bytes_round_trip_exactly tests.test_capture_operation.FrozenCaptureInputTests.test_code_and_tool_only_v5_signal_cannot_create_an_observation -v
```

Result: `FAILED (failures=2)`. The initial literal v3/v4 fixture bytes omitted
the canonical terminal newline, while the independent code/tool-only corpus
case passed. This exposed fixture formatting rather than a production defect.

### GREEN

Commands:

```bash
.venv/bin/python -m unittest tests.test_inventory tests.test_capture tests.test_capture_operation tests.test_templates -q
git diff --check
.venv/bin/python -m compileall -q src/zdecision/capture src/zdecision/agent tests
```

Result: `Ran 124 tests in 0.408s ... OK`; diff and compilation checks completed
with exit code 0.

The v3/v4 check now loads fixed, literal canonical JSON byte fixtures rather
than generating historical inputs through the current serializer, and verifies
their exact bytes on reserialization. A receipt-free v5 code/tool-only signal
uses its actual input shape and is shown unable to create an observation or
become candidate eligible.
