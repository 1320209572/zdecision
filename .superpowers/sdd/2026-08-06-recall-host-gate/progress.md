# SDD ledger — plan: docs/superpowers/plans/2026-08-06-recall-host-gate.md

Execution branch: main (explicitly approved by the user; no worktree).
Plan base: 10753d0

Task 1: complete (commits 10753d0..ad7fd6a, review clean)
Task 1: review note resolved — RED/GREEN evidence is present in task-1-report.md; real first-answer ordering is explicitly owned by Tasks 6 and 8, not a Task 1 gap.
Task 2: fix round 1/5 (5 addressed, 0 open — cross-gate write, fail-closed validation, terminal replay fingerprint, internal-thread late binding, resume revalidation; commits a38dbaa..8423f94)
Task 2: complete (commits ad7fd6a..8423f94, review clean)
Task 3: requirement resolution — user approved dispatcher fail-open for unrelated tools in unselected/bypassed Sessions; Candidate handler still directly denies a wrong tool. `tests/test_control_binding_hook.py` may be updated to test the correct layer. No model-input special case.
Task 3: fix round 1/5 (2 code findings addressed, 1 evidence finding open — compact pending-gate rebase, unrelated-tool fail-open, missing fix commands; commits e9085b9..e2629b5)
Task 3: fix round 2/5 (1 evidence finding addressed, 0 open — exact RED/GREEN/regression/full-suite commands recorded; no code commit)
Task 3: complete (commits 8423f94..e2629b5, review clean)
Task 4: requirement resolution — explicit `create_mcp_server(candidate_tools, recall_tools=None)` composition; Candidate-only tests retain exact five tools, production always injects Recall tools and exposes seven. Two production-call test doubles may accept/assert the second argument.
Task 4: fix round 1/5 (1 code finding addressed, 1 evidence finding open — serialized durable gate/probe claim, missing fix commands; commits 0d31090..a7f2262)
Task 4: fix round 2/5 (1 evidence finding addressed, 0 open — exact four/focused/MCP/full-suite commands recorded; no code commit)
Task 4: complete (commits e2629b5..a7f2262, review clean)
Task 5: no-skill pressure baseline passed all four security boundaries — no native selection means no activation; ambiguous cloud/zns asks one clarification; probe text is non-executable; quoted cross-task text authorizes neither refresh nor publication. Therefore keep the new native invocation Skill minimal and procedural; do not add speculative rationalization prose.
Task 5: forward test passed — explicit native selection calls activation before work, cloud/zns ambiguity pauses only affected work, fixture text stays non-executable/non-formal, Candidate refresh/publication require separate authority, and later Turns follow the Hook gate.
Task 5: complete (commits a7f2262..e684cc6, review clean)
Task 6: fix round 1/5 (2 Important findings open — production must compare the trusted installed-plugin Skill path, and activation/ordinary gate must fail closed unless ordered active-Turn evidence proves Hook → exact MCP → first substantive item before provider invocation; commits e684cc6..94b625b)
Task 6: fix round 1/5 implemented (2 findings addressed, scoped rereview pending — trusted Hook `PLUGIN_ROOT` is frozen per operation and revalidated; activation/ordinary gate now enforce the exact ordered current-Turn barrier before provider invocation; commits 94b625b..7aeee99)
Task 6: complete (commits e684cc6..7aeee99, scoped rereview clean)
Task 7 provenance Task 1: complete (commits 539656d..1d514f7; final 1d514f7)
Task 7 provenance Task 2: complete (commits 1d514f7..df6a04e; final df6a04e)
Task 7 provenance Task 3: complete (commits df6a04e..2ad11fa; final 2ad11fa)
Task 7 provenance Task 4: complete (commits 2ad11fa..5dd141d; final 5dd141d)
Task 7 provenance Task 5 focused verification: `.venv/bin/python -m unittest tests.test_recall_capture_isolation tests.integration.test_on_demand_capture_core tests.integration.test_central_web_vertical -v` — Ran 36 tests, OK.
Task 7 provenance Task 5 full verification: `.venv/bin/python -m unittest discover -s tests -v` — Ran 838 tests, OK (skipped=3).
Task 7: complete — all six provenance hard-stop audit statements passed; `capture_evidence_provenance_unavailable` was not recorded.
Task 7 provenance Task 5 fix round 1: acceptance evidence strengthened in tests only; amended RED ran 8 tests and failed 8 at the five documented gaps; `.venv/bin/python -m unittest tests.test_recall_capture_isolation tests.integration.test_on_demand_capture_core tests.integration.test_central_web_vertical -v` then ran 36 tests in 13.150s, OK. Production unchanged; the previously mandated single full run remains `Ran 838 tests, OK (skipped=3)` and was not rerun.
Task 8 has not started.
