# Recall Gate A0 Semantic Oracle Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the disposable Gate A0 acceptance prove one relevant and one irrelevant Decision, the full pre-application/application/post-application sequence, and Hook trust readiness before another real Desktop run.

**Architecture:** Keep the approved production Recall design unchanged. Correct only the disposable provider, typed handoff snapshot, test-only semantic oracle, bounded diagnostics, and automated acceptance in the existing two Gate A0 integration files. A fresh disposable root remains mandatory for the next real run.

**Tech Stack:** Python 3.12, `unittest`, Pydantic, FastMCP, SQLite, Codex Plugin Hooks, MCP Apps.

## Global Constraints

- Do not modify `src/zdecision/**` or `plugins/zdecision/**`.
- Do not touch the two protected untracked files.
- Do not read or mutate production ZDecision state, Candidate state, Registry state, or Central.
- Keep the disposable root, marketplace, Plugin, MCP processes, and SQLite state isolated.
- The model-visible snapshot contains no raw Prompt, transcript, Session ID, Turn ID, absolute path, or host-owned binding ID.
- The real acceptance passes only with one `applicable`, one `not_applicable`, one pre-application denial, one post-application counter increment, zero local search/read tools, and no duplicate delivery/application/mutation.

---

### Task 1: Correct the disposable semantic oracle and acceptance preflight

**Files:**
- Modify: `tests/integration/recall_gate_a0_disposable_harness.py`
- Modify: `tests/integration/test_recall_gate_a0_disposable_vertical.py`
- Append after verification: `.superpowers/sdd/2026-08-10-recall-next-native-message-handoff-gate-a/task-0-report.md`

**Interfaces:**
- Consumes: the existing generated Hook, MCP server, App card, SQLite schema, and approved `recall-handoff-v1` flow.
- Produces: a typed test intent, two semantically distinct canonical Decisions, a terminal test-only classification oracle, exact counter sequencing instructions, and `inspect` trust readiness.

- [ ] **Step 1: Write failing semantic-fixture and snapshot tests**

Add assertions that the frozen snapshot contains a closed seven-field normalized test intent:

```python
{
    "target_decision_space_ids": ["prod_4d7b16e1616dd4cd1aeb2411836fd687"],
    "explicit_multi_space": False,
    "feature_goal": "Validate Recall handoff for the security-services application",
    "domain_objects": ["security-services", "Recall handoff"],
    "repository_relative_paths": [
        "packages/products/third-party-services/apps/security-services/"
    ],
    "constraints": ["Apply only Decisions governing this feature scope"],
    "exclusions": ["backup-services"],
}
```

Keep fixture one inside that product/path/goal. Change fixture two into a same-repository retrieval false positive whose scope and future action concern only `backup-services`, which the intent explicitly excludes. Assert both Decision envelopes remain canonical and their literal expected SHA-256 digests are updated by hand in the test.

- [ ] **Step 2: Run the semantic-fixture tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.integration.test_recall_gate_a0_disposable_vertical.GateA0DisposableVerticalTests.test_snapshot_freezes_intent_and_one_obvious_negative_control -v
```

Expected: FAIL because the current snapshot has no intent and fixture two governs Gate A0 itself.

- [ ] **Step 3: Write failing oracle and execution-order tests**

Add behavior tests proving:

```python
wrong = [first_applicable, second_applicable]
result = bound_apply(wrong)
self.assertEqual("classification_oracle_mismatch", result["structuredContent"]["code"])
self.assertEqual(0, inspect()["application_count"])
self.assertEqual(0, inspect()["mutation_count"])
```

Also assert the model-visible application instruction requires this exact order:

1. use only the typed intent and delivered Decisions;
2. call the disposable counter before application and observe denial;
3. submit complete classifications once;
4. call the counter once after `application_committed` and observe `counter == 1`;
5. do not call shell, search, file-read, status, or render tools; and
6. do not guess host-owned identifiers.

The correct `[applicable, not_applicable]` vector must still commit exactly once and permit exactly one counter increment. A semantic mismatch is terminal for that delivery and must not create an Application row.

- [ ] **Step 4: Run the oracle tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.integration.test_recall_gate_a0_disposable_vertical.GateA0DisposableVerticalTests.test_wrong_semantic_vector_is_terminal_without_application \
  tests.integration.test_recall_gate_a0_disposable_vertical.GateA0DisposableVerticalTests.test_handoff_instruction_requires_the_complete_counter_sequence -v
```

Expected: FAIL because the current server commits both `applicable` items and the instruction does not require the counter sequence or prohibit local reads.

- [ ] **Step 5: Write the failing Hook-trust preflight test**

Generate the disposable Plugin under a temporary fake Codex home. Assert `inspect` reports:

```python
{
    "hook_trust_source": "<selector>@<selector>-marketplace:hooks/hooks.json:pre_tool_use:0:0",
    "hook_trust_record_present": False,
}
```

Then add only that exact source under `[hooks.state]` in the fake `config.toml` and assert the boolean becomes true. The diagnostic must not print the trusted hash, home path, hook command, or unrelated config.

- [ ] **Step 6: Run the trust-preflight test and verify RED**

Run:

```bash
.venv/bin/python -m unittest \
  tests.integration.test_recall_gate_a0_disposable_vertical.GateA0DisposableVerticalTests.test_inspect_reports_exact_hook_trust_readiness -v
```

Expected: FAIL because current `inspect` does not expose bounded trust readiness.

- [ ] **Step 7: Implement the smallest harness changes**

In the disposable harness only:

- add the literal normalized test intent to `_snapshot()`;
- replace fixture two with the unambiguously excluded negative control and update literal digests;
- reject a classification vector other than `("applicable", "not_applicable")` with terminal `classification_oracle_mismatch` before inserting an Application;
- replace `APPLICATION_INSTRUCTION` with the exact bounded sequence above; and
- have `inspect` derive the exact source key from the immutable marker and report only key plus presence in the selected test Codex-home config.

Do not generalize the oracle into production behavior. Do not add retries, migrations, new processes, App Server calls, or a new state machine.

- [ ] **Step 8: Run focused and complete Gate A0 tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.integration.test_recall_gate_a0_disposable_vertical -v
.venv/bin/python -m compileall -q \
  tests/integration/recall_gate_a0_disposable_harness.py \
  tests/integration/test_recall_gate_a0_disposable_vertical.py
git diff --check
```

Expected: every Gate A0 test passes; compile and diff checks produce no errors.

- [ ] **Step 9: Review and commit the scoped fix**

Confirm every changed line belongs to the two disposable files or this plan/report. Confirm production and protected files are unchanged. Commit only the plan and two tracked Gate A0 files:

```bash
git add \
  docs/superpowers/plans/2026-08-11-recall-gate-a0-semantic-oracle-fix.md \
  tests/integration/recall_gate_a0_disposable_harness.py \
  tests/integration/test_recall_gate_a0_disposable_vertical.py
git commit -m "test: fix Recall Gate A0 semantic oracle"
```

- [ ] **Step 10: Prepare one fresh real Desktop rerun**

Record the current instance as FAIL, uninstall only its selector/marketplace, wait for its exact MCP leases to exit, and clean only its recorded root. Generate a fresh disposable root, install its unique selector, complete Hook trust review, restart Desktop, and verify `hook_trust_record_present == true` before asking the user to run the card.

The new real run passes only when bounded evidence shows:

```text
attempts=1
deliveries=1
context_updates=1
classifications=[applicable, not_applicable]
applications=1
active_fixtures=1
pre_application_denials=1
mutation_claims=1
mutation_counter=1
local_search_or_read_calls=0
duplicate_groups=0
```

Any other result is FAIL and stops Gate A without production changes.
