# Task 2 implementation report

## Status

Implemented the commit-bound Registry read boundary, organization-scoped
dashboard queries, transport-only Web API, SPA delivery boundary, explicit Demo
CLI Registry composition, reproducible React/Vite package, and the company
shell/overview required by Task 2. No Task 3 actions or mutation endpoints were
added.

## Implementation

- Added `RegistryQuery.snapshot()` with exact-main synchronization, canonical
  JSON checks, symlink/path containment checks, strict Registry V1 parsing, and
  product/Decision/head/revision ownership proofs. Failures collapse to the
  stable `registry_unavailable` boundary instead of an empty snapshot.
- Added immutable dashboard read values and `CentralWebQueries` with
  organization-scoped enabled repository resolution, server-derived product
  grouping, current-head pending counts, receipt exclusion, latest matching
  Review semantics, Registry-derived active Decision counts, activity times,
  and canonically revalidated recent publications.
- Added `CentralWebApplication` and a transport-only `/api/v1/web/dashboard`
  router. `create_app` configures it only when supplied, mounts built assets,
  serves the same SPA index for non-API browser paths, and preserves JSON 404s
  for all `/api/...` misses.
- Added the required absolute existing Git-root validation and explicit
  `GitRegistryAdapter` / `RegistryQuery` / `CentralWebQueries` /
  `CentralWebApplication` composition to `zdecision-central run`, while keeping
  loopback rejection ahead of filesystem access.
- Added the exact pinned React 19 / React Router 7 / TypeScript 7 / Vite 8 /
  Vitest package and lockfile, Vite proxy/build layout, and package data for the
  generated static assets.
- Added the dark ZStack rail, company overview metrics, server-derived product
  workspaces, recent publication feed, cross-product Review index, loading/error
  states, stable API client, all approved browser routes, and neutral actionless
  placeholders for later slices.

## Files

Backend and packaging:

- `src/zdecision/registry/query.py`
- `src/zdecision/central/web/queries.py`
- `src/zdecision/central/web/application.py`
- `src/zdecision/central/web/api.py`
- `src/zdecision/central/api.py`
- `src/zdecision/central/cli.py`
- `src/zdecision/central/static/index.html`
- `src/zdecision/central/static/assets/*`
- `pyproject.toml`

Frontend:

- `web/package.json`, `web/package-lock.json`, `web/tsconfig.json`,
  `web/vite.config.ts`, and `web/index.html`
- `web/src/main.tsx`, `web/src/vite-env.d.ts`
- `web/src/app/AppShell.tsx`, `web/src/app/router.tsx`
- `web/src/api/client.ts`, `web/src/api/types.ts`
- `web/src/assets/zstack-logo.svg`
- `web/src/styles/tokens.css`, `web/src/styles/app.css`
- `web/src/pages/company-overview/CompanyOverviewPage.tsx`
- `web/src/pages/company-overview/CompanyOverviewPage.test.tsx`
- `web/src/pages/review-index/ReviewIndexPage.tsx`
- `web/src/shared/AsyncState.tsx`, `web/src/shared/StatusBadge.tsx`
- `web/src/test/setup.ts`

Tests:

- `tests/test_central_web_queries.py`
- `tests/test_central_web_api.py`
- `tests/test_demo_config.py` (supplies the newly required Registry-root
  argument in the existing loopback compatibility scenario)

## TDD evidence

### Backend RED

Command:

```text
.venv/bin/python -m unittest tests.test_central_web_queries tests.test_central_web_api -v
```

Result before production modules existed: exit 1, 2 import errors, specifically
missing `zdecision.central.web.queries` and
`zdecision.central.web.application`. This was the expected Task 2 RED.

The focused CLI validation test was then added before its helper. Its isolated
run failed at import because `_registry_repository_root` did not exist. After
implementation the same isolated test passed 1/1.

### Backend GREEN

Initial focused query/API run after implementation:

```text
.venv/bin/python -m unittest tests.test_central_web_queries tests.test_central_web_api -v
```

Result: exit 0, 6/6 passed. A later scoped test also passed 1/1 after adding
explicit coverage that only the latest matching terminal Review resolves a
current head and that a later `skip` remains pending.

Independent pre-commit review then reproduced a canonical uncommitted Registry
change being returned under the synchronized commit SHA. A real local/bare Git
fixture was added first and failed because `RegistryQueryUnavailable` was not
raised. The initial defense required a clean Registry before and after reading
and re-fetched/re-required the same exact commit before returning. The review
also required a repository-local `node_modules/` ignore rule so package hygiene
does not depend on a user-level Git configuration.

Fix round 1 demonstrated that Git's `assume-unchanged` index bit bypassed that
cleanliness defense: a tracked Decision could contain different canonical
worktree bytes while status remained clean. The real Git regression failed with
`'committed formal decision' != 'uncommitted canonical decision'`. The reader
now resolves every root-declared document with `git ls-tree` at the proven
commit, requires an exact regular blob entry, reads that object with
`git cat-file`, and applies the existing canonical JSON and strict V1 ownership
checks to those immutable bytes. The isolated regression then passed 1/1.

Fix round 2 demonstrated that a repository-local `refs/replace` could redirect
`git cat-file blob <original-sha>` to different canonical bytes while the
synchronized `origin/main` commit SHA remained unchanged. The real Git
regression failed with
`'committed formal decision' != 'replacement canonical decision'`. Both the
`ls-tree` resolution and `cat-file` blob read now use
`git --no-replace-objects`, preventing replacement commits, trees, or blobs
from altering the commit-bound snapshot. The isolated regression then passed
1/1.

### Frontend RED

After installing the exact lockfile dependencies, command:

```text
cd web && npm test -- CompanyOverviewPage.test.tsx
```

Result before router/page production files existed: exit 1, failed suite with
the expected unresolved `../../app/router` import.

The first typecheck exposed four missing asset/config declarations, which were
fixed with the Vite client declaration and Vitest config typing. The first
component GREEN attempt then caught a real accessibility defect: numeric nav
markers changed the link name from `候选审核` to `02候选审核`. Marking the visual
ordinal `aria-hidden` made the intended accessible contract pass.

### Frontend GREEN and build

Command:

```text
cd web && npm run typecheck && npm test -- CompanyOverviewPage.test.tsx && npm test && npm run build
```

Result: exit 0. TypeScript passed; focused test 1/1 passed; complete frontend
test run 1/1 passed; Vite transformed 32 modules and generated:

- `src/zdecision/central/static/index.html` (0.45 kB)
- `src/zdecision/central/static/assets/index-COg6qIa0.css` (9.47 kB)
- `src/zdecision/central/static/assets/index-mcGfibPC.js` (290.56 kB)

### Backend/API compatibility

Command:

```text
.venv/bin/python -m unittest tests.test_central_web_queries tests.test_central_web_api tests.test_central_api -v
```

Final fresh result after review fixes: exit 0, 23/23 passed, including the real
Git `assume-unchanged` and replacement-object reproducers, latest-Review
semantics, Web API, and all Packet 1 Central API compatibility cases.

Command:

```text
.venv/bin/python -m unittest tests.test_demo_config -v
```

Result after updating the existing invocation for the required argument: exit
0, 3/3 passed. It still proves non-loopback rejection occurs before opening the
database/config paths.

## Self-review

- Checked every Task 2 brief item against the diff and found no Task 3 mutation
  API, Candidate inbox, draft, Review submission, preview, or publication action.
- Confirmed product and repository reads are organization-scoped and enabled;
  the second-organization fixture and the extra Registry product cannot enter
  the requesting user's product, Candidate, active Decision, or publication
  summaries.
- Confirmed pending SQL starts from current heads, joins enabled mappings,
  left-joins receipts, matches Reviews to the same repository/family/revision,
  and only treats the newest matching `accept`, `edit_accept`, or `reject` as
  resolved. Missing Review and latest `skip` remain pending.
- Confirmed Registry failures never become an available empty snapshot and that
  declared canonical paths and strict V1 ownership are checked before data is
  exposed. Every document is read from a regular blob in the synchronized commit
  object with replacement-object processing disabled, and that synchronized
  commit is revalidated before returning the snapshot; mutable worktree bytes,
  index cleanliness, and repository-local replacement refs are not read inputs.
- Confirmed existing Packet 1 API tests pass and the SPA catch-all explicitly
  refuses `/api` and `/api/...` paths.
- Confirmed frontend source contains no representative hard-coded product
  catalog or Session identifier and later routes expose no working action.
- `git diff --check` reports no whitespace errors.
- Independent review reported no Critical findings. Its package-hygiene finding
  was addressed before the Task 2 commit. Its follow-up `assume-unchanged`
  reproducer showed the first worktree defense was incomplete; fix round 1 now
  removes the worktree from the read boundary entirely. Fix round 2 closes the
  replacement-object bypass for both tree resolution and blob reads. The
  reviewer reported no other correctness, isolation, SQL, SPA/API, CLI,
  frontend, or lockfile finding and no remaining scope creep.

## Concerns

- The brief references an approved private
  `zstack-ui-next/packages/products/cloud/bff/public/theme/default/zh-CN/logo.svg`,
  but that repository/asset is absent from all available workspace and user
  paths. The shell therefore uses a checked-in white SVG ZStack wordmark (never
  the rejected bitmap). It is isolated at `web/src/assets/zstack-logo.svg` for
  exact replacement when the private reviewed source becomes available.
- The exact required `jsdom@30.0.1` currently advertises Node
  `^22.22.2 || ^24.15.0 || >=26`, while the exact required application engine is
  `>=22.12 <23` and the available runtime is Node 22.18. npm emits an engine
  warning, but dependency installation, typecheck, Vitest, and the production
  build all complete successfully. Required versions and engine values were not
  changed.
