# Recall Native Elicitation Gate E0

## Result

Recorded at `2026-08-09T08:40:17Z`.

**FAIL — production activation remains blocked.**

The first Desktop case reached one native prompt and one completion but ended
in `cancel`, not the required `accept`. The hard stop was applied immediately:
replay and all later Desktop cases were not run, and no retry can change this
Gate E0 result.

## Environment and source

- Codex Desktop: `26.803.41515` (build `6321`).
- Codex CLI: `0.147.0`.
- Python: `3.14.4`.
- MCP SDK: `1.29.0`.
- Git source base: `5093afef20ef9f3dd826204f71aeffe4381da3f8`.
- Probe SHA-256: `4bab61f2bfe68b915733d836150d327b0628cf588c1b53835d93d3fda30d6b0e`.
- `tool_call_mcp_elicitation`: stable and enabled.
- `mcp_2026_07_28`: disabled.

## Scenario evidence

| Case ID | Source | Request digest prefix | Action/state | Prompt count | Completion count | Result |
|---|---|---:|---|---:|---:|---|
| `accept` | desktop | `4b656bd3dc6f` | `cancel` | 1 | 1 | FAIL |
| `decline` | desktop | `not_sent` | `not_run` | 0 | 0 | FAIL |
| `cancel` | desktop | `not_sent` | `not_run` | 0 | 0 | FAIL |
| `restart` | desktop | `not_sent` | `not_run` | 0 | 0 | FAIL |
| `capability_unavailable` | automated | `not_sent` | `unavailable` | 0 | 0 | PASS |

The four unexecuted Desktop requirements are FAIL because the hard stop forbids
continuing after the first failed case. The automated no-capability row comes
from the exact in-memory MCP test and did not enter the Desktop receipt
database.

## Automated command status

- `.venv/bin/python -m unittest tests.test_recall_elicitation_probe -v`:
  27 tests, OK.
- `.venv/bin/python -m unittest tests.test_recall_elicitation_probe.RecallElicitationProtocolTest.test_client_without_form_capability_returns_unavailable_without_eliciting -v`:
  1 test, OK.
- `.venv/bin/python -m unittest tests.integration.test_recall_elicitation_desktop -v`:
  1 test, OK with 1 expected skip because live opt-in was disabled.
- `.venv/bin/python -m unittest tests.test_mcp_recall_host_gate tests.test_recall_hook_gate tests.test_recall_skill_contract tests.test_plugin_contract -v`:
  60 tests, OK.
- `.venv/bin/python -m compileall -q tests/recall_elicitation_probe.py tests/test_recall_elicitation_probe.py tests/integration/test_recall_elicitation_desktop.py`:
  exit 0.
- Opt-in live Desktop assertion: not run because the initial Desktop case
  triggered the hard stop before the four required receipts existed.
- Privacy scan of this report and the private receipt database: no test
  sentinel matched.

## Temporary MCP cleanup

- The preflight lookup for `zdecision-elicitation-e0` was collision-free.
- `zdecision-elicitation-e0` was verified as the exact enabled stdio test probe
  before the live case.
- After the failure, removal of `zdecision-elicitation-e0` succeeded. The name
  was absent immediately after removal and remained absent after the cleanup
  Desktop restart.
- The private receipt database was deleted after the sanitized evidence commit;
  a final filesystem check confirmed it is absent.

No production ZDecision service, tool, state, Candidate, Capture, Central,
Registry, or Recall lifecycle was invoked or changed by this acceptance run.

## Final decision

**FAIL — production activation remains blocked.**
