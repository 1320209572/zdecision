# Task 5 Report: Neutral Decision-Space API and Shared Catalog Tree

## Summary

- Added safe Decision-space references, summaries, Shared catalog nodes, and
  repository-space views derived from enabled server-owned leaves.
- Added canonical `/api/v1/web/spaces/{decision_space_id}` candidate, draft,
  review, Decision, and publication routes. Catalog groups fail closed with
  `decision_space_not_leaf`; legacy product routes resolve product leaves only.
- Dashboard product totals now count only product leaves and the Shared tree
  preserves exact group/package hierarchy with descendant aggregate counts.
- React now generates canonical `/spaces/{decision_space_id}` links throughout,
  renders semantic expandable Shared groups with actions only on leaves, and
  keeps publication-preview return navigation bound to its resolved leaf.
- Repository entry no longer guesses a product. It loads the server's exact
  repository-space index, groups Product and Shared leaves, and opens a leaf
  directly only when the supplied ID occurs in that response.

## RED evidence

The new focused backend tests initially failed because the dashboard had no
`shared_tree`, groups were not distinguished from leaves, and the repository
space endpoint did not exist:

```text
FAILED (failures=3)
```

The focused frontend tests initially failed because `DecisionSpaceTree` did
not exist, company links still used `/products`, and repository entry assumed
the dashboard/product shape. A final route audit added a publication-preview
regression that failed with:

```text
Expected href=/spaces/dsp_00000000000000000000000000000000/candidates
Received href=/products/prod_11111111111111111111111111111111/candidates
```

## GREEN evidence

Exact Task 5 backend command:

```text
.venv/bin/python -m unittest tests.test_central_web_queries tests.test_central_web_api tests.test_central_web_review -v
Ran 48 tests in 4.540s
OK
```

Exact focused frontend command:

```text
npm test -- src/pages/company-overview/CompanyOverviewPage.test.tsx src/pages/review-index/ReviewIndexPage.test.tsx src/features/decision-spaces/DecisionSpaceTree.test.tsx
Test Files  3 passed (3)
Tests       3 passed (3)
```

Additional affected regression checks:

```text
npm test -- --run
Test Files  8 passed (8)
Tests       29 passed (29)

npm run typecheck
exit 0

.venv/bin/python -m unittest tests.test_central_web_preview tests.test_central_web_api
Ran 29 tests in 6.015s
OK

git diff --check
exit 0
```

The Python suites emit only the existing FastAPI/TestClient `httpx`
deprecation warning.

## Files changed

Production changes cover the central Web contracts, query/review/preview/
publication services, application and transport routes, plus the React API
types, router, shell, Decision-space tree, repository entry, affected pages,
and styles. Tests cover safe server-derived catalog results, group rejection,
canonical links, Shared hierarchy, and repository-only navigation.

## Self-review

- Confirmed `product_count` excludes Shared leaves and every Shared action URL
  terminates at an enabled leaf rather than a catalog group.
- Confirmed repository-space results omit unattached registry-only records and
  React does not infer ownership from product-shaped compatibility metadata.
- Confirmed production React sources contain no generated `/products/...`
  links; product-only endpoints remain compatibility reads on product leaves.
- Confirmed formal V1 Decision JSON is unchanged: Decision-space metadata is
  exposed only in safe Web view envelopes, including publication preview.
- Confirmed all implementation and test work stayed within Task 5; Task 6
  compact-list and batch-review behavior was not introduced.

## Concerns

- The compatibility `/products/{product_id}` API remains intentionally
  available for product leaves. New UI routes are canonical and neutral.
- Repository-space visibility depends on enabled current route heads; a leaf
  that exists only in the private registry catalog is deliberately hidden.

## Fix Round 1: Canonical Decision-space identity

### Summary

- Added the exact safe `DecisionSpaceRef` to Candidate, Decision list/detail,
  publication Preview, and publication history/detail views.
- Canonical serializers omit top-level V1 compatibility `product_id` and
  `product_name`; exact V1 Decision JSON and Preview documents retain their
  unchanged bytes. Legacy `/products` responses retain compatibility fields.
- Candidate, Preview, Decision catalog/detail, and publication history/detail
  now render leaf display name, kind context, breadcrumb, source root, package,
  and asset type instead of labeling a Shared compatibility partition as a
  product.
- Disabled leaves remain available as historical Preview identity metadata so
  the established stale-preview recovery behavior is unchanged; active route
  authorization remains enabled-only.

### RED evidence

Backend Shared canonical journey:

```text
.venv/bin/python -m unittest tests.test_central_web_api.CentralWebApiTest.test_shared_canonical_flows_expose_leaf_identity_not_v1_partition -v
KeyError: 'space'
Ran 1 test
FAILED (errors=1)
```

After adding safe refs, the stricter envelope assertion proved compatibility
identity still leaked at the canonical boundary:

```text
AssertionError: 'product_id' unexpectedly found in canonical Candidate payload
Ran 1 test
FAILED (failures=1)
```

React page regressions:

```text
Test Files  5 failed (5)
Tests       5 failed | 23 passed (28)
```

Each failure showed that the page still rendered the compatibility name and
could not find the Shared leaf heading `theme`.

### GREEN evidence

Focused Shared backend journey:

```text
Ran 1 test in 1.501s
OK
```

Affected React pages:

```text
Test Files  5 passed (5)
Tests       28 passed (28)
```

Task 5 exact backend suite:

```text
Ran 49 tests
OK
```

Task 5 exact frontend suite and full frontend suite:

```text
Test Files  3 passed (3)
Tests       3 passed (3)

Test Files  8 passed (8)
Tests       31 passed (31)
```

Task 4 Preview/publication/Registry regression suite:

```text
Ran 34 tests in 10.685s
OK
```

`npm run typecheck`, Python compilation, and `git diff --check` all exited 0.
The Python output contains only the existing FastAPI/TestClient `httpx`
deprecation warning.

### Self-review

- The end-to-end backend regression exercises one real `shared_unit` through
  canonical Candidate, Review, Preview, Decision, publish, history, and detail
  routes, asserting the exact safe leaf reference at every read boundary.
- Compatibility fields remain provable inside canonical V1 Decision bytes and
  old product endpoints, but no canonical page presents them as user-facing
  ownership.
- Product leaf pages use the same safe reference and continue to display their
  registered product display names.
- No V1 Registry model, publication state machine, recovery action, or Task 6
  batch interaction changed.

## Fix Round 2: Disabled publication history

### Summary

- Added a historical Decision-space lookup that accepts only an exact
  organization-owned canonical `dsp_*` identifier and includes disabled
  leaves. It never resolves through the mutable product compatibility mapping.
- Publication detail, global history, and canonical leaf history now use that
  historical identity path, so disabling a leaf or its repository route does
  not erase completed publication records or their safe leaf metadata.
- Canonical `/spaces/{decision_space_id}/publications` scopes the database read
  to that exact leaf. Legacy product history remains enabled-route guarded.
- Active Candidate and Review routes remain enabled-only. Preview creation and
  confirmation also retain their enabled-leaf freshness checks.

### RED evidence

The new Shared journey first published successfully, disabled both its current
repository route and leaf, and then attempted the canonical history read:

```text
.venv/bin/python -m unittest tests.test_central_web_api.CentralWebApiTest.test_shared_canonical_flows_expose_leaf_identity_not_v1_partition
AssertionError: 200 != 404 : {"error":"not_found"}
Ran 1 test
FAILED (failures=1)
```

After removing the transport guard, the store's enabled-leaf filter exposed
the second half of the same defect:

```text
IndexError: list index out of range
Ran 2 tests
FAILED (errors=1)
```

### GREEN evidence

Focused disabled-history and active-denial regressions:

```text
Ran 3 tests in 1.187s
OK
```

The focused set proves canonical and global publication history plus detail
remain readable with the exact safe Shared identity and no top-level
`product_id`/`product_name`. It also proves disabled Candidate/Review and
Preview-create actions reject, while the existing disabled-leaf publication
test proves Preview remains readable as stale and confirmation rejects.

Task 5 exact backend suite:

```text
Ran 50 tests in 5.561s
OK
```

Task 4 Preview/publication/Registry regression suite:

```text
Ran 34 tests in 10.697s
OK
```

Additional storage and vertical checks:

```text
Ran 13 tests in 0.102s
OK

Ran 1 test in 1.265s
OK
```

Python compilation and `git diff --check` both exited 0. The Python output
contains only the existing FastAPI/TestClient `httpx` deprecation warning.

### Self-review

- Historical publication reads stay organization-scoped and retain the prior
  actor visibility policy; no authorization boundary was broadened.
- Exact `decision_space_id` filtering happens in the publication query, so a
  leaf history cannot absorb records from another leaf or infer identity from
  a current repository/product mapping.
- Active paths still call enabled-only resolution. Disabled canonical
  Candidate and Review return 404, Preview creation returns 409, and publish
  freshness still marks an unpublished Preview stale before mutation.
- There are no changes under Registry models, Preview serialization, or Web
  contracts. The legacy `/products` transport changes only pass an explicit
  `None` for the new canonical scope, preserving its V1 response bytes.
