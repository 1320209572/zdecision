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
