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
