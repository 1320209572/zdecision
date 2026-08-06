# Central Decision Web Demo

This runbook exercises the implemented monorepo path against the registered
`zstack-ui-next` checkout while using the ZDecision checkout as the V1 Registry
working tree. It is a loopback technical Demo, not a production deployment.

## Prerequisites

- Keep `/Users/zhaohuiying/Desktop/Zstack-repos/zstack-ui-next` read-only for
  this acceptance. Do not clean, amend, commit, or otherwise modify its working
  tree.
- Use a clean, synchronized ZDecision `main` and its expected `origin` for a
  publication smoke. Use a disposable Registry clone when that condition is not
  available.
- Create a new private Demo directory once; never overwrite existing Demo data:

```bash
zdecision-central demo-config init \
  --repository-cwd /Users/zhaohuiying/Desktop/Zstack-repos/zstack-ui-next \
  --output-dir /absolute/path/to/new-demo-config
```

The generated catalog contains the product and concrete Shared package roots
listed in the repository README. Verify the working tree actually contains any
leaf you intend to exercise. `Shared` itself is not a route target.

## Start the loopback services

```bash
export ZDECISION_REPO=/Users/zhaohuiying/Desktop/Zstack-repos/zdecision
export ZDECISION_DEMO_DIR=/absolute/path/to/new-demo-config
cd "$ZDECISION_REPO"
"$ZDECISION_REPO/.venv/bin/python" -m zdecision.central.cli run \
  --database "$ZDECISION_DEMO_DIR/central.sqlite3" \
  --config "$ZDECISION_DEMO_DIR/central.json" \
  --registry-repository-root "$ZDECISION_REPO" \
  --host 127.0.0.1 \
  --port 8765
```

In a second terminal:

```bash
export ZDECISION_REPO=/Users/zhaohuiying/Desktop/Zstack-repos/zdecision
export ZDECISION_DEMO_DIR=/absolute/path/to/new-demo-config
cd "$ZDECISION_REPO"
"$ZDECISION_REPO/.venv/bin/python" -m zdecision.agent.cli service run \
  --config "$ZDECISION_DEMO_DIR/agent.json"
```

Both services remain local. Stop if configuration, repository identity,
Registry synchronization, or expected-origin validation fails.

## One bounded acceptance flow

1. In a Codex task bound to the enabled monorepo, show **更新候选决策** and click
   **所有有效 Session** once. Do not enter or copy a Session ID and do not choose
   a product or Shared package.
2. Verify one Capture group reaches `succeeded` or
   `succeeded_no_candidates`. For a non-empty result, verify the frozen Git route
   plan creates one slice per matched leaf and no slice named `Shared`.
3. Open `/reviews?repository_id=<repository_id>`. Expand `Shared`; only concrete
   directory/package leaves have Candidate, Decision, and Publication links.
   Every leaf link is canonical under `/spaces/{decision_space_id}`.
4. Open one leaf Candidate Inbox. Select several rows, apply batch Accept or
   Reject, use Undo once, and submit only the explicitly classified current
   revisions. Checkbox selection alone is not acceptance.
5. Create the read-only Preview. Verify complete formal documents, exact target
   paths, digests, base commit, and commit message. Review submission has not
   published anything.
6. Perform the separate explicit publish action once. Verify the resulting
   Decision and publication history remain under the same leaf and its isolated
   `decision-registry/products/prod_<stable-id>/` V1 compatibility partition.
   Verify no `Shared` root partition exists.
7. Restart the Agent after one slice receipt in the automated fixture, then
   verify that receipt suppresses a second upload of the same batch and the
   remaining slices finish normally.
8. Inspect central persistence and recorded HTTP bodies. Raw Session and Prompt
   text, model/tool output, source, diffs, credentials, and local absolute paths
   must be absent.

The handoff records the Capture group/request ID, exact observed leaf names,
Review batch ID, Preview ID, Publication ID, Decision ID, and commit SHA. If the
host cannot reliably deliver a native card click from the current environment,
record that interaction gap explicitly and run the equivalent real
configuration/service/API/Agent chain; never report a click that was not
observed.

## Stop and exclusions

Stop both loopback services after the bounded flow. This Demo does not prove
SSO, Git-role authorization, Registry V2, route administration, comments,
notifications, automatic recall, or the separately excluded Dashboard
Git-fetch performance change.
