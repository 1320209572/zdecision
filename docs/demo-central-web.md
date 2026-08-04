# Central Decision Web Demo

This is the exact Gate 7 operator runbook for the current ZDecision checkout. It
uses the fixed technical-Demo principal, loopback HTTP, the existing Demo
configuration, and the checkout itself as the Decision Registry working tree.
Do not use these commands with a different checkout or a non-Demo Registry.

## Prerequisite

The repository must be on a clean, synchronized `main` branch with its expected
`origin`. The directory `/Users/zhaohuiying/.zdecision/demo` must already contain
the matching `central.json` and local Agent configuration created by
`zdecision.central.cli demo-config init`. Never recreate that directory over
existing Demo data. The repository-local virtual environment must already exist
at `/Users/zhaohuiying/Desktop/Zstack-repos/zdecision/.venv` with ZDecision and
its runtime dependencies installed.

## Start the loopback service

Run this block exactly in a terminal and leave it running:

```bash
export ZDECISION_REPO=/Users/zhaohuiying/Desktop/Zstack-repos/zdecision
export ZDECISION_DEMO_DIR=/Users/zhaohuiying/.zdecision/demo
cd "$ZDECISION_REPO"
"$ZDECISION_REPO/.venv/bin/python" -m zdecision.central.cli run \
  --database "$ZDECISION_DEMO_DIR/central.sqlite3" \
  --config "$ZDECISION_DEMO_DIR/central.json" \
  --registry-repository-root "$ZDECISION_REPO" \
  --host 127.0.0.1 \
  --port 8765
```

The command must remain bound to `127.0.0.1`. Stop immediately if startup
reports a configuration, Registry, Git synchronization, or origin error.

## Perform the one bounded acceptance flow

1. In a Codex task whose current repository is enabled by the Demo
   configuration, ask Codex to show **更新候选决策**.
2. On the inline card, click **所有有效 Session** exactly once. Do not enter,
   copy, or pass a Session ID. Record the resulting Capture Request ID.
3. Verify the default browser opens that repository's product Candidate Inbox,
   and verify the Capture Request reaches `succeeded` or
   `succeeded_no_candidates`.
4. If Candidates exist, choose a partial mix of **接受**, **拒绝**, and **跳过**,
   then click **保存审核草稿**. Record the selected Candidate family IDs.
5. Close the browser tab. Stop the loopback service with `Ctrl-C`, rerun the
   exact startup block, reopen the product Inbox, and verify the same saved
   actions are restored.
6. Submit the Review once and record the Review batch ID. Verify rejected and
   skipped Candidate claims are absent from the next publication artifact.
7. Create the publication preview and record its Preview ID. Inspect every
   changed path, SHA-256 digest, complete canonical JSON document, base commit,
   and commit message. They must exactly match the accepted Review content.
8. Click the single **确认发布** action once. Record the Publication ID and wait
   for the publication state to become **发布完成**. Do not retry with a second
   browser action while a pending or ambiguous state is shown.
9. Follow the resulting product-isolated Decision link. Record the Decision ID
   and publication commit SHA, then open **发布历史** and follow the same
   Publication ID back to its detail page.

The Gate 7 handoff is complete only when it records this tuple from the same
flow: Capture Request ID, Review batch ID, Preview ID, Publication ID, Decision
ID, and commit SHA.

## Bounded visual inspection

During that same flow, inspect exactly these routes at one desktop width and one
narrow width: `/`, `/reviews`, the product Candidate route, the preview route,
`/decisions`, the Decision detail route, `/publications`, and the publication
detail route. Check the ZStack mark, selected navigation item, text wrapping,
visible keyboard focus, disabled/stale/pending/ambiguous contrast, and absence
of horizontal overflow. Fix only a defect visible on one of those routes, then
rerun its affected frontend test and the production build. Do not begin another
visual iteration.

## Stop and scope

Stop the loopback service with `Ctrl-C` after recording the six IDs. This Demo
does not demonstrate SSO, Git-role authorization, Decision updates, or
automatic recall. It also does not authorize publication to any non-temporary
Registry outside this explicit manual flow.
