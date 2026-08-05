import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";

import type {
  CandidateInboxItem,
  DecisionSpaceRef,
  ReviewDraftItem,
} from "../../api/types";
import { CandidateReviewRow } from "./CandidateReviewRow";


const FAMILY_ID = "cfm_" + "a".repeat(32);
const REPOSITORY_ID = "repo_" + "1".repeat(32);
const REVISION_ID = "crv_" + "b".repeat(32);
const DIGEST = "c".repeat(64);
const REQUEST_ID = "crq_" + "d".repeat(32);

const space: DecisionSpaceRef = {
  decision_space_id: "dsp_" + "9".repeat(32),
  kind: "shared_unit",
  display_name: "theme",
  breadcrumb: ["Shared", "packages/shared", "theme"],
  source_root: "packages/shared/theme",
  package_name: "@zstack/theme",
  asset_type: "library",
};

const item: CandidateInboxItem = {
  family_id: FAMILY_ID,
  repository_id: REPOSITORY_ID,
  capture_request_ids: [REQUEST_ID],
  revision_id: REVISION_ID,
  revision: 1,
  content_digest: DIGEST,
  content: {
    product: "ZDecision",
    claim: "决策 A",
    future_action: "保留显式审核边界。",
    scope_summary: "中央候选审核",
    repositories: ["zdecision"],
    paths: ["src/zdecision/central/web/"],
    invalidation_conditions: ["审核边界发生变化。"],
  },
  review_state: "pending",
  draft_action: null,
  stale_draft: false,
};

function renderRow(action?: ReviewDraftItem) {
  const handlers = {
    onSelectedChange: vi.fn(),
    onDirectAction: vi.fn(),
    onEditAccept: vi.fn(),
  };
  render(
    <CandidateReviewRow
      item={item}
      space={space}
      action={action}
      selected={false}
      stale={false}
      {...handlers}
    />,
  );
  return handlers;
}

it("keeps evidence collapsed while exposing unambiguous row actions", async () => {
  const user = userEvent.setup();
  const handlers = renderRow();
  const row = screen.getByRole("article", { name: "候选 决策 A" });

  expect(within(row).getByRole("checkbox", { name: "选择决策 A" }))
    .not.toBeChecked();
  expect(within(row).getByText("未处理")).toBeVisible();
  expect(within(row).queryByText(REVISION_ID)).not.toBeVisible();
  expect(within(row).queryByText(DIGEST)).not.toBeVisible();

  await user.click(within(row).getByRole("button", { name: "接受决策 A" }));
  expect(handlers.onDirectAction).toHaveBeenCalledWith(FAMILY_ID, "accept");
});

it("reveals exact evidence as React text only on request", async () => {
  const user = userEvent.setup();
  renderRow();
  const evidence = screen.getByText("查看证据").closest("details")!;

  expect(evidence).not.toHaveAttribute("open");
  await user.click(screen.getByText("查看证据"));
  expect(within(evidence).getByText(REVISION_ID)).toBeVisible();
  expect(within(evidence).getByText(DIGEST)).toBeVisible();
  expect(within(evidence).getByText(REPOSITORY_ID)).toBeVisible();
  expect(within(evidence).getByText(REQUEST_ID)).toBeVisible();
});

it("opens one inline edit panel with locked ownership fields", async () => {
  const user = userEvent.setup();
  const handlers = renderRow();

  await user.click(screen.getByRole("button", { name: "编辑决策 A" }));
  expect(handlers.onEditAccept).toHaveBeenCalledWith(FAMILY_ID, item.content);
});
