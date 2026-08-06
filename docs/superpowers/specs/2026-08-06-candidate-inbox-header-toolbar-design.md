# Candidate Inbox Header and Filter Toolbar Design

**Status:** Proposed for user review.

**Scope:** Redesign only the upper portion of the Candidate Inbox: Decision
space identity, repository refresh action, and Candidate filters. The review
summary, batch actions, private draft workflow, Candidate rows, submission,
and publication boundaries remain unchanged.

## 1. Problem

The current upper section gives equal visual weight to product identity,
repository refresh, ordinary filters, and technical identifiers. It creates
three avoidable sources of friction:

- the title and refresh control look like unrelated panels;
- the update repository and filter repository controls repeat a long opaque
  repository ID even though they have different meanings; and
- `Capture Request ID` is presented as a primary daily filter instead of an
  occasional diagnostic filter.

The result is tall, fragmented, and visually heavier than the Candidate review
work below it.

## 2. Considered approaches

### A. Compact identity header plus progressive filters — selected

Keep the Decision-space identity and refresh action in one calm header row.
Place search and review state in the primary filter row. Move repository
filtering and `Capture Request ID` into a disclosed **更多筛选** region, while
showing any active advanced filters as visible summary chips.

This preserves every current capability, makes the common path obvious, and
can be implemented without API or review-state changes.

### B. One dense command bar

Put the title, repository, refresh action, search, state, and filters on one
horizontal line. This saves the most vertical space but becomes difficult to
scan and degrades quickly on narrower screens.

### C. Permanent filter side panel

Move all filters into a right-side drawer or rail. This separates filtering
from product identity, but adds navigation and responsive complexity that is
not justified by four filters.

Approach A is selected because it gives the page a clear hierarchy with the
smallest behavioral and implementation surface.

## 3. Information hierarchy

The upper section has two compact layers.

### 3.1 Decision-space header

The left side contains:

- the existing `PRODUCT / CANDIDATE INBOX` or `SHARED UNIT / CANDIDATE INBOX`
  eyebrow;
- the canonical Decision-space display name;
- the existing source-root, package, and asset context; and
- one short sentence explaining that review drafts are private and do not
  publish Decisions.

The right side is a lightweight refresh action rather than a framed console:

- label the selector **更新仓库** so it cannot be confused with list
  filtering;
- keep the exact **更新候选决策** action and its current authorization and
  progress behavior;
- show refresh progress or failure immediately below the action; and
- remove the offset box shadow and large bordered card.

The header remains visually asymmetrical but shares one baseline and one
surface. The page title stays dominant; the opaque repository ID is secondary.

### 3.2 Candidate filter toolbar

The always-visible primary row contains:

- a flexible **搜索候选决策** field;
- an **审核状态** selector;
- a **更多筛选** disclosure showing the number of active advanced filters;
  and
- **应用筛选** as the single primary toolbar action.

The disclosed advanced row contains:

- **筛选仓库**, which affects only the Candidate list; and
- **Capture Request ID**, retained for diagnostics and precise historical
  filtering.

Advanced filters never become invisible state. When the advanced region is
closed and either value is active, the toolbar renders a compact summary chip
with its value and exposes a clear action. Opening and closing the region does
not apply, reset, or otherwise mutate filters.

The existing URL parameter contract remains authoritative. Submitting filters
continues to update `search`, `repository_id`, `capture_request_id`, and
`state`; loading a URL restores those values. No filter is automatically
applied merely because a control changes.

## 4. Visual direction

The page keeps the established ZStack industrial palette: deep navy text,
cool gray canvas, precise one-pixel dividers, and green only for the refresh
action and successful state. The redesign is restrained rather than decorative:

- reduce total header height and vertical gaps;
- replace nested bordered boxes with alignment, spacing, and one subtle
  divider;
- keep labels small but use Chinese product language for common controls;
- preserve the strong product-name typography;
- use one quiet surface for the filter toolbar; and
- avoid new gradients, floating cards, oversized shadows, icons, or motion.

At desktop width, the review summary should appear materially higher in the
viewport than it does today. At tablet width, the refresh group moves below
the title without becoming a separate card. At mobile width, all controls
stack in logical tab order and retain visible labels and focus states.

## 5. Component and state boundary

The change remains inside `CandidateReviewPage` and the corresponding
Candidate-page CSS. One local disclosure state may be added for advanced
filters. Existing `useCandidateRefresh`, Candidate query loading, draft state,
batch review, and submission code are not changed.

The update repository and filter repository stay separate state values because
they have different domain meanings. Visual proximity must not merge their
behavior:

- update repository selects the repository sent to the explicit all-valid
  Session refresh request;
- filter repository restricts the Candidate list after **应用筛选**.

No backend schema, endpoint, Candidate record, Review record, or publication
contract changes in this slice. The known new-group `capture_request_id`
compatibility defect is also outside this visual redesign.

## 6. Error and progress behavior

- Refresh running, success, empty-result, and failure messages remain next to
  the refresh action and do not resize the primary filter row.
- Candidate loading and page-level failure continue to use the existing async
  states.
- Advanced-filter disclosure is local presentation state and does not survive
  navigation; filter values continue to survive through the URL.
- Disabled refresh and filter actions retain their existing domain conditions.

## 7. Verification

Implementation must use test-first changes and preserve the existing refresh,
URL-filter, draft-retention, batch-review, and submission tests. Focused UI
tests must additionally prove:

1. search and review state are visible in the primary toolbar;
2. repository filtering and `Capture Request ID` are hidden behind
   **更多筛选** by default;
3. the disclosure reveals both advanced controls without changing their
   values or applying filters;
4. active advanced filters remain visibly summarized while collapsed;
5. update repository and filter repository remain independent; and
6. keyboard names, focus order, and mobile stacking remain usable.

After focused tests pass, run the full Web test suite, TypeScript checking, and
production build. Perform visual checks at the current 1600×900 desktop
acceptance size and one narrow mobile viewport.

## 8. Acceptance criteria

- The upper section reads as one product header followed by one filter toolbar,
  not three competing boxes.
- The common review path requires no interaction with repository filtering or
  `Capture Request ID`.
- The lower review console and Candidate list begin higher in the viewport.
- All existing refresh and filter capabilities remain available and retain
  their current domain semantics.
- No Candidate, Review, publication, Central API, or repository-routing
  behavior changes as part of this redesign.
