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

- Probe unit suite: 27 tests, OK.
- Exact client-without-form-capability test: 1 test, OK.
- Desktop assertion with live opt-in disabled: 1 test, OK with 1 expected skip.
- Recall host, Hook, Skill, and Plugin regression suites: 60 tests, OK.
- Probe and acceptance module compilation: exit 0.
- Opt-in live Desktop assertion: not run because the initial Desktop case
  triggered the hard stop before the four required receipts existed.
- Privacy scan of this report and the private receipt database: no test
  sentinel matched.

## Temporary MCP cleanup

- The preflight temporary-name lookup was collision-free.
- The temporary server was verified as the exact enabled stdio test probe
  before the live case.
- After the failure, removal succeeded and the temporary name was absent both
  before and after the cleanup Desktop restart.
- The private receipt database was retained only to verify and commit this
  sanitized evidence and is to be deleted immediately after the commit.

No production ZDecision service, tool, state, Candidate, Capture, Central,
Registry, or Recall lifecycle was invoked or changed by this acceptance run.

## Final decision

**FAIL — production activation remains blocked.**
