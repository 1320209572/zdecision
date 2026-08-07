# Final review fix report

## RED

The new schema, runner, and processor regressions were run before the
production change:

```bash
.venv/bin/python -m unittest \
  tests.test_templates.PromptContractTests.test_v5_extraction_schema_closes_an_empty_eligible_set \
  tests.test_templates.PromptContractTests.test_v5_extraction_schema_keeps_eligibility_independent_of_candidate_cap \
  tests.test_requested_capture.RequestedCaptureRunnerTest.test_v5_zero_eligible_inventory_commits_an_empty_v2_result \
  tests.test_requested_capture.RequestedCaptureRunnerTest.test_v5_extraction_schema_accepts_all_eligible_signal_ordinals \
  tests.test_capture_request_processor.CaptureRequestProcessorTest.test_zero_eligible_capture_completes_without_retry_or_upload_content -v
```

Result: `Ran 5 tests in 0.071s ... FAILED (errors=5)`. Every regression failed
at `_signal_ordinal_enum()` with
`ValueError: eligible_signal_ordinals are invalid`; the processor converted
the empty-set failure into terminal `local_capture_state_invalid`.

## GREEN

The same five regressions completed successfully after the scoped schema
change:

```bash
.venv/bin/python -m unittest \
  tests.test_templates.PromptContractTests.test_v5_extraction_schema_closes_an_empty_eligible_set \
  tests.test_templates.PromptContractTests.test_v5_extraction_schema_keeps_eligibility_independent_of_candidate_cap \
  tests.test_requested_capture.RequestedCaptureRunnerTest.test_v5_zero_eligible_inventory_commits_an_empty_v2_result \
  tests.test_requested_capture.RequestedCaptureRunnerTest.test_v5_extraction_schema_accepts_all_eligible_signal_ordinals \
  tests.test_capture_request_processor.CaptureRequestProcessorTest.test_zero_eligible_capture_completes_without_retry_or_upload_content -v
```

Result: `Ran 5 tests in 0.069s ... OK`.

The required four-module focused suite completed successfully:

```bash
.venv/bin/python -m unittest \
  tests.test_capture \
  tests.test_templates \
  tests.test_requested_capture \
  tests.test_capture_request_processor -v
```

Result: `Ran 139 tests in 0.845s ... OK`.

Compilation and diff checks:

```bash
.venv/bin/python -m compileall -q \
  src/zdecision \
  tests/test_capture.py \
  tests/test_templates.py \
  tests/test_requested_capture.py \
  tests/test_capture_request_processor.py
git diff --check
```

Result: both commands completed with exit code 0 and no output.

## Contract review

- V5 extraction eligibility accepts a unique tuple containing zero through 100
  integer ordinals, with each ordinal constrained to 1 through 100.
- An empty eligible set produces a closed schema whose Candidate array has
  `maxItems: 0` and whose item schema contains no empty ordinal enum. The only
  valid top-level payload is therefore `{"candidates": []}`.
- A nonempty eligible set is copied exactly into the source ordinal enum,
  including ordinals above 20, while the Candidate array remains capped at 20.
- A zero-eligible Inventory still runs exactly one Inventory Turn followed by
  one Extraction Turn and commits the existing v2 zero-observation result.
- Request processing completes zero-eligible slices without retries or upload
  content and uses the existing completion path.
- Candidate count enforcement remains independent in the existing service
  validation. No Task 8 or architectural changes were made.

## Concerns

None.
