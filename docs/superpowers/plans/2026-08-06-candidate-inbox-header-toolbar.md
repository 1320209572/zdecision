# Candidate Inbox Header and Filter Toolbar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Candidate Inbox's fragmented upper section with one compact Decision-space header and one progressive filter toolbar while preserving all refresh, URL-filter, and review behavior.

**Architecture:** Keep the change inside the existing `CandidateReviewPage` and Candidate-page CSS. Add one local presentation state for the advanced-filter disclosure; keep update-repository and filter-repository state independent, and leave the Candidate query, Capture hook, review draft, batch actions, submission, and publication contracts untouched.

**Tech Stack:** React 19, React Router 7, TypeScript 7, CSS, Vitest 4, Testing Library, Vite 8.

## Global Constraints

- The upper section must read as one product header followed by one filter toolbar, not three competing boxes.
- Search and review state remain visible; repository filtering and `Capture Request ID` are hidden behind **更多筛选** by default.
- Active advanced filters remain visibly summarized while the disclosure is closed.
- Update repository selects the explicit all-valid Session refresh target; filter repository only changes the Candidate-list URL after **应用筛选**.
- Existing URL parameters remain `search`, `repository_id`, `capture_request_id`, and `state`.
- Do not change Candidate, Review, publication, Central API, or repository-routing behavior.
- Do not fix the separate new-group `capture_request_id` compatibility defect in this slice.
- Preserve the established ZStack navy, gray, and green industrial visual language; add no dependency, icon set, floating card, large shadow, or decorative animation.

---

### Task 1: Compact header and progressive Candidate filters

**Files:**
- Modify: `web/src/pages/candidate-review/CandidateReviewPage.test.tsx:522-640`
- Modify: `web/src/pages/candidate-review/CandidateReviewPage.tsx:663-764`
- Modify: `web/src/styles/app.css:77-97`
- Modify: `web/src/styles/app.css:338-339`

**Interfaces:**
- Consumes: existing `selectedRepository`, `filterSearch`, `filterRepository`, `filterCaptureRequest`, `filterState`, `capture`, and `applyFilters` values inside `CandidateReviewPage`.
- Produces: local `advancedFiltersOpen: boolean`, `activeAdvancedFilterCount: number`, accessible **更多筛选** disclosure behavior, active-filter summary chips, and the existing form submission contract.

- [ ] **Step 1: Add a focused failing disclosure test**

Add a test beside `exposes safe Inbox filters and sends every approved filter` that renders a URL with both advanced filters active and asserts the user-facing behavior rather than CSS implementation details:

```tsx
it("keeps diagnostic filters behind a disclosure without hiding active values", async () => {
  vi.stubGlobal("fetch", vi.fn(() => json(inbox())));
  await router.navigate(
    `/spaces/${SPACE_ID}/candidates?repository_id=${REPOSITORY_ID}` +
      `&capture_request_id=${REQUEST_ID}&state=pending`,
  );
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  const more = await screen.findByRole("button", { name: "更多筛选 2" });
  expect(more).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByLabelText("筛选仓库")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Capture Request ID")).not.toBeInTheDocument();
  expect(screen.getByText(REPOSITORY_ID, { selector: "code" })).toBeVisible();
  expect(screen.getByText(REQUEST_ID, { selector: "code" })).toBeVisible();

  await user.click(more);
  expect(more).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByLabelText("筛选仓库")).toHaveValue(REPOSITORY_ID);
  expect(screen.getByLabelText("Capture Request ID")).toHaveValue(REQUEST_ID);
});
```

Add a second test that proves clearing is staged locally and does not submit
the form:

```tsx
it("clears one summarized advanced filter without applying the form", async () => {
  const candidateUrls: string[] = [];
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/candidates")) {
      candidateUrls.push(url);
      return json(inbox());
    }
    throw new Error(`Unexpected fetch: ${url}`);
  }));
  await router.navigate(
    `/spaces/${SPACE_ID}/candidates?repository_id=${REPOSITORY_ID}` +
      `&capture_request_id=${REQUEST_ID}&state=pending`,
  );
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  await screen.findByRole("button", { name: "更多筛选 2" });
  await user.click(screen.getByRole("button", { name: "清除请求筛选" }));

  expect(screen.getByRole("button", { name: "更多筛选 1" })).toBeVisible();
  expect(screen.queryByText(REQUEST_ID, { selector: "code" }))
    .not.toBeInTheDocument();
  expect(candidateUrls).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "更多筛选 1" }));
  expect(screen.getByLabelText("Capture Request ID")).toHaveValue("");
});
```

Before adding the test, confirm the production mutation it catches: rendering
the two technical controls unconditionally, losing their values while closed,
or failing to expose the disclosure state must fail this test.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
cd web
npm test -- CandidateReviewPage.test.tsx -t "keeps diagnostic filters behind a disclosure"
```

Expected: FAIL because the **更多筛选 2** button does not exist and the two
advanced controls are currently rendered unconditionally.

- [ ] **Step 3: Update the existing filter-contract test for the approved interaction**

In `exposes safe Inbox filters and sends every approved filter`, reveal the
advanced controls before reading or changing them:

```tsx
const more = screen.getByRole("button", { name: "更多筛选 2" });
await user.click(more);
expect(screen.getByLabelText("筛选仓库")).toHaveValue(REPOSITORY_ID);
expect(screen.getByLabelText("Capture Request ID")).toHaveValue(REQUEST_ID);
```

Keep its final literal URL assertion unchanged so the test continues to prove
all four approved query parameters are submitted.

- [ ] **Step 4: Implement the minimal progressive-filter state and markup**

Add presentation-only state near the existing filter state:

```tsx
const [advancedFiltersOpen, setAdvancedFiltersOpen] = useState(false);
const activeAdvancedFilterCount =
  Number(Boolean(filterRepository)) + Number(Boolean(filterCaptureRequest));
```

Replace the current five-column filter form with:

```tsx
<form className="candidate-filters" onSubmit={applyFilters}>
  <div className="candidate-filters__primary">
    <label className="candidate-filters__search">
      <span>搜索候选决策</span>
      <input
        type="search"
        aria-label="搜索候选决策"
        maxLength={200}
        value={filterSearch}
        onChange={(event) => setFilterSearch(event.target.value)}
      />
    </label>
    <label className="candidate-filters__state">
      <span>审核状态</span>
      <select
        aria-label="审核状态"
        value={filterState}
        onChange={(event) =>
          setFilterState(event.target.value as CandidateStateFilter)
        }
      >
        <option value="pending">待审核</option>
        <option value="accepted">已接受</option>
        <option value="rejected">已拒绝</option>
        <option value="published">已发布</option>
        <option value="all">全部</option>
      </select>
    </label>
    <button
      className="candidate-filters__more"
      type="button"
      aria-expanded={advancedFiltersOpen}
      aria-controls="candidate-advanced-filters"
      onClick={() => setAdvancedFiltersOpen((open) => !open)}
    >
      更多筛选{activeAdvancedFilterCount ? ` ${activeAdvancedFilterCount}` : ""}
    </button>
    <button className="filter-button" type="submit">应用筛选</button>
  </div>

  {!advancedFiltersOpen && activeAdvancedFilterCount ? (
    <div className="candidate-filters__active" aria-label="已启用的高级筛选">
      {filterRepository ? (
        <span>
          仓库 <code>{filterRepository}</code>
          <button
            type="button"
            aria-label="清除仓库筛选"
            onClick={() => setFilterRepository("")}
          >×</button>
        </span>
      ) : null}
      {filterCaptureRequest ? (
        <span>
          请求 <code>{filterCaptureRequest}</code>
          <button
            type="button"
            aria-label="清除请求筛选"
            onClick={() => setFilterCaptureRequest("")}
          >×</button>
        </span>
      ) : null}
    </div>
  ) : null}

  {advancedFiltersOpen ? (
    <div className="candidate-filters__advanced" id="candidate-advanced-filters">
      <label>
        <span>筛选仓库</span>
        <select
          aria-label="筛选仓库"
          value={filterRepository}
          onChange={(event) => setFilterRepository(event.target.value)}
        >
          <option value="">全部仓库</option>
          {inbox.repositories.map((repository) => (
            <option value={repository.repository_id} key={repository.repository_id}>
              {repository.repository_id}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>Capture Request ID</span>
        <input
          aria-label="Capture Request ID"
          value={filterCaptureRequest}
          onChange={(event) => setFilterCaptureRequest(event.target.value)}
        />
      </label>
    </div>
  ) : null}
</form>
```

Keep `applyFilters` unchanged. Do not synchronize `selectedRepository` and
`filterRepository`.

- [ ] **Step 5: Flatten the header without changing refresh behavior**

Keep the existing Decision-space content and refresh handler, but give them a
single compact composition:

```tsx
<header className="page-header candidate-page__header">
  <div className="candidate-page__identity">
    <p className="eyebrow">
      {inbox.space.kind === "product" ? "PRODUCT" : "SHARED UNIT"} / CANDIDATE INBOX
    </p>
    <h1>{inbox.space.display_name}</h1>
    <DecisionSpaceContext space={inbox.space} />
    <p className="page-header__lead">
      审阅当前候选版本并保存私人草稿。保存不会提交审核或生成发布内容。
    </p>
  </div>
  <div className="refresh-console">
    <label>
      <span>更新仓库</span>
      <select
        aria-label="更新仓库"
        value={selectedRepository}
        onChange={(event) => setSelectedRepository(event.target.value)}
      >
        <option value="">请选择仓库</option>
        {inbox.repositories.map((repository) => (
          <option value={repository.repository_id} key={repository.repository_id}>
            {repository.repository_id}
          </option>
        ))}
      </select>
    </label>
    <button
      className="primary-button"
      type="button"
      disabled={!selectedRepository || capture.running}
      onClick={() => void capture.refresh()}
    >
      更新候选决策
    </button>
    {capture.message ? (
      <p className={capture.failed ? "capture-status capture-status--failed" : "capture-status"}>
        {capture.message}
      </p>
    ) : null}
  </div>
</header>
```

Update the existing accessibility test from `findByLabelText("登记仓库")` to
`findByLabelText("更新仓库")`. The exact button name, `capture.refresh()` call,
disabled condition, repository option values, and progress message branches
remain unchanged.

- [ ] **Step 6: Run the focused Candidate-page suite and verify GREEN**

Run:

```bash
cd web
npm test -- CandidateReviewPage.test.tsx
```

Expected: every `CandidateReviewPage` test passes, including refresh payload,
all four URL filters, draft retention, and the new disclosure test.

- [ ] **Step 7: Refine the approved industrial layout in CSS**

After the behavioral suite is green, refactor the Candidate-page CSS without
changing behavior:

- reduce `.page-header` bottom spacing for this page;
- remove the bordered refresh card and offset shadow;
- align `.refresh-console` as a compact two-column action group;
- make `.candidate-filters` one quiet surface rather than a five-cell grid;
- define primary, active-summary, and advanced rows as separate grids;
- truncate long repository/request codes visually without changing their
  accessible text;
- preserve visible hover, disabled, and focus-visible states; and
- stack the header, primary row, and advanced row at the existing 980 px and
  680 px breakpoints.

Use these concrete desktop grids:

```css
.candidate-filters__primary {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) 150px auto auto;
}

.candidate-filters__advanced {
  display: grid;
  grid-template-columns: minmax(220px, .8fr) minmax(260px, 1fr);
}
```

Do not change `.candidate-review-console`, `.candidate-summary`,
`.candidate-batch-toolbar`, `.candidate-toolbar`, `.candidate-stack`, or any
Candidate-row selector.

- [ ] **Step 8: Run complete Web verification**

Run:

```bash
cd web
npm test
npm run typecheck
npm run build
```

Expected: Vitest reports zero failing tests, TypeScript exits 0, and Vite
produces the production build with exit 0.

- [ ] **Step 9: Perform desktop and mobile visual acceptance**

Start the real local Central Web build and inspect the Candidate Inbox at:

- 1600×900: header and toolbar are materially shorter, active advanced filters
  are summarized, and the review summary begins higher in the viewport;
- narrow mobile viewport: title, refresh action, primary filters, and advanced
  filters stack in keyboard order without horizontal overflow.

Exercise **更多筛选**, change each advanced value without submitting, close and
reopen it, then apply. Confirm values persist locally and only **应用筛选**
changes the URL.

- [ ] **Step 10: Commit the implementation**

```bash
git add \
  web/src/pages/candidate-review/CandidateReviewPage.test.tsx \
  web/src/pages/candidate-review/CandidateReviewPage.tsx \
  web/src/styles/app.css
git commit -m "feat: refine candidate inbox controls"
```
