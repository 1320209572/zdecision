# Task 7 Report: Real Monorepo Vertical Acceptance

## Outcome

Task 7 proves the implemented monorepo boundary from one Update action through
trusted Git path routing, independent product and concrete Shared leaf slices,
local Candidate processing, leaf Review, read-only Preview, and explicit V1
publication. The committed Demo catalog matches the selected
`zstack-ui-next` package tree, the installed Skill keeps exactly the two Update
scopes, and the documentation now describes canonical `/spaces` navigation and
the actual privacy boundary.

## Stage commits

The seven primary stage commits are:

1. Task 1: `3499757c9b82daa11cc32fdc62c22173382553b6` — `feat: add decision space catalog routes`
2. Task 2: `8e6364f6aef656d9228489c52b8a3f44716b3eb8` — `feat: freeze capture slice ownership`
3. Task 3: `501f3ad` — `feat: capture candidates by decision space`
4. Task 4: `4424e36` — `feat: publish leaf decision spaces`
5. Task 5: `a636ff9` — `feat: browse decision spaces/shared packages`
6. Task 6: `0688e7e` — `feat: add batch candidate review`
7. Task 7: this report's commit — `test: prove monorepo decision workflow`

The bounded repair commits retained in the stage history are `c15fc6e`,
`56d11e5`, `847bc3a`, `ecf1775`, `2f70df5`, `4d62c92`, and `2f6d1eb`.

## TDD and focused verification

The new four-leaf integration first failed at the first receipt because the
integration app-server fake still emitted the old hard-coded product identity.
The fake was corrected to honor the requested JSON-schema product enum; the
ownership assertion was not weakened.

The exact Task 7 Step 2 command then passed:

```text
Ran 22 tests in 8.133s
OK
```

The Demo/Skill contract tests were also established red first. The failures
showed the missing real product roots, three obsolete Shared leaves, and the
missing no-selector Skill language. After the catalog and Skill changes, the
two directed tests passed. The exact Step 3 command then passed:

```text
Ran 49 tests in 8.025s
OK
```

Those integrations assert one Capture group, exact `cloud`, `zns`,
`zcf-audit`, and `theme` leaf ownership, no generic `Shared` slice, one upload
of the first accepted receipt across restart, leaf Candidate/Inboxes, raw-data
privacy, and theme publication below its V1 compatibility partition with no
Shared-root Registry partition.

## Complete verification pass

The one permitted backend discovery run executed 662 tests. It reported 658
passes, 2 skips, and 2 failures:

- a README case-sensitive compatibility phrase;
- a legacy page test fixture that registered only the retired repository to
  product mapping and omitted the now-required enabled repository, leaf, and
  trusted route.

Both fixtures were repaired narrowly. The two affected tests were rerun once
and passed (`Ran 2 tests ... OK`). The complete discovery command was not
repeated, preserving the Task 7 once-only bound; the other 660 outcomes from
that run remain unchanged.

Frontend verification completed successfully on its single pass:

```text
npm test
Test Files  9 passed (9)
Tests       42 passed (42)

npm run typecheck
exit 0

npm run build
43 modules transformed
src/zdecision/central/static/assets/index-DoM6JbCG.css
src/zdecision/central/static/assets/index-CrMsAOpJ.js
exit 0
```

`git diff --check` also exited 0.

## Real `zstack-ui-next` smoke

The source checkout at
`/Users/zhaohuiying/Desktop/Zstack-repos/zstack-ui-next` was inspected
read-only on branch `anheng` at
`ee5720268fe2f26b7a67bbc5901df84d072bfe3f`. It already contained 45 user
working-tree entries and was not modified. A disposable no-hardlinks clone
kept the real SSH origin and received exactly these three smoke changes:

```text
packages/products/cloud/AGENTS.md
packages/products/shared/zcf-audit/package.json
packages/shared/theme/package.json
```

The generated Demo catalog contained 25 routes. Direct trusted Git evidence
and the route matcher resolved the exact leaves `cloud`, `zcf-audit`, and
`theme`. The first real Agent-service request failed closed with
`repository_snapshot_unavailable`, as expected before any local Hook snapshot
existed. After a temporary SessionStart Hook seeded that prerequisite, request
`crq_390fd6496595f27f59e6a03aedb93924` reached
`succeeded_no_candidates`: there was no eligible real app-server source, but
the actual service produced three independently accepted slices in route-plan
order:

```text
theme      dsp_dcda0ced83eab3669668b97cc396ed20  accepted  0 candidates
zcf-audit  dsp_ef3bfba4f917cc7d87e54857d736617f  accepted  0 candidates
cloud      dsp_d4bfbf69787c2e28827413d1b255b264  accepted  0 candidates
```

No slice used the generic name `Shared`. Dashboard/API inspection showed the
Shared root as a non-actionable `catalog_group` with no `decision_space_id`,
and returned 25 concrete repository spaces.

The temporary central SQLite bytes did not contain the smoke Session marker,
the temporary absolute path, or any of the three changed source filenames.
The synthetic three-candidate HTTP smoke performed earlier also produced one
leaf Inbox per exact leaf and stored no raw source marker. Only temporary
service processes were stopped; the user's pre-existing Agent service was left
running. The disposable smoke directory was moved to the user's Trash as
`zdecision-task7-smoke.7raKU0`, so cleanup remains recoverable.

The native Codex card click and manual browser batch Accept/Reject/Undo were
not executable from this non-interactive acceptance environment. Their
behavior remains covered by the focused Plugin contract, 22/49-test vertical
integration gates, and the 42-test Web suite. This is recorded as a host-level
interaction gap, not represented as completed manual evidence.

## Documentation and privacy

README, architecture, and operator documentation now use the implemented
sequence:

```text
Update action -> Capture group -> trusted Git route plan -> leaf slices
leaf slice -> local extraction/reconciliation -> frozen Candidate ownership
leaf Candidate Inbox -> Review -> Preview -> explicit publish -> V1 partition
```

They enumerate the exact 13 product roots and 12 concrete Shared leaves,
describe Shared groups as navigation-only, use canonical `/spaces` URLs, and
state that raw Sessions, Prompts, model context, tool output, source, diffs,
credentials, and local absolute paths remain on the device.

## Explicitly excluded, non-blocking risks

- native host card-click and manual browser interaction coverage;
- dashboard fetch/performance work;
- Registry V2;
- SSO and Git-role authorization;
- route-management UI;
- comments and notifications;
- automatic Decision recall.

No push or additional architecture/review loop was performed.
