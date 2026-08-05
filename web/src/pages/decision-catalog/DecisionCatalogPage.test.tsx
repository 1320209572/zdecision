import { render, screen } from "@testing-library/react";
import { RouterProvider } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { router } from "../../app/router";
import type { DecisionListItem, DecisionListView } from "../../api/types";


const CLOUD_PRODUCT_ID = "prod_" + "1".repeat(32);
const CLOUD_DECISION_ID = "dec_" + "1".repeat(32);
const ZMETIS_PRODUCT_ID = "prod_" + "2".repeat(32);
const ZMETIS_DECISION_ID = "dec_" + "2".repeat(32);
const SHARED_SPACE_ID = "dsp_" + "3".repeat(32);
const SHARED_COMPATIBILITY_NAME = "Shared / packages/shared/theme";

function decision(
  productId: string,
  productName: string,
  decisionId: string,
): DecisionListItem {
  const decisionSpaceId = `dsp_${productId.slice(-32)}`;
  return {
    decision_space_id: decisionSpaceId,
    space: {
      decision_space_id: decisionSpaceId,
      kind: "product",
      display_name: productName,
      breadcrumb: [productName],
      source_root: ".",
      package_name: null,
      asset_type: null,
    },
    decision_id: decisionId,
    revision: 1,
    lifecycle: "active",
    claim: `${productName} 的正式隔离决策`,
    future_action: "只读取已提交的 Registry 快照。",
    scope_summary: "中央决策目录",
    repositories: [productName === "ZStack Cloud" ? "cloud" : "zmetis"],
    paths: ["decision-registry/"],
    published_at: "2026-08-04T08:00:00Z",
    publication_id: "plb_" + decisionId.slice(-32),
    commit_sha: "a".repeat(40),
  };
}

function respond(view: DecisionListView | { error: string }, status = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(view), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
}

afterEach(() => vi.unstubAllGlobals());

it("keeps global ownership visible and product routes isolated", async () => {
  const items = [
    decision(CLOUD_PRODUCT_ID, "ZStack Cloud", CLOUD_DECISION_ID),
    decision(ZMETIS_PRODUCT_ID, "ZMetis", ZMETIS_DECISION_ID),
  ];
  respond({
    registry_state: "available",
    registry_commit: "a".repeat(40),
    items,
    total: 2,
  });
  await router.navigate("/decisions");
  render(<RouterProvider router={router} />);

  expect(await screen.findByText("ZStack Cloud")).toBeVisible();
  expect(screen.getByText("ZMetis")).toBeVisible();
  expect(screen.getAllByRole("link", { name: "查看决策" })[0]).toHaveAttribute(
    "href",
    `/spaces/dsp_${CLOUD_PRODUCT_ID.slice(-32)}/decisions/${CLOUD_DECISION_ID}`,
  );
  expect(screen.getAllByText("中央决策目录")).toHaveLength(2);
});

it("distinguishes Registry unavailability from an empty catalog", async () => {
  respond(
    { error: "registry_unavailable" },
    503,
  );
  await router.navigate("/decisions");
  render(<RouterProvider router={router} />);

  expect(await screen.findByText("正式决策仓库暂不可用")).toBeVisible();
  expect(screen.queryByText("暂无正式决策")).not.toBeInTheDocument();
});

it("presents a canonical Shared leaf instead of its V1 partition", async () => {
  const item = {
    ...decision(
      "prod_" + "3".repeat(32),
      SHARED_COMPATIBILITY_NAME,
      "dec_" + "3".repeat(32),
    ),
    decision_space_id: SHARED_SPACE_ID,
    space: {
      decision_space_id: SHARED_SPACE_ID,
      kind: "shared_unit" as const,
      display_name: "theme",
      breadcrumb: ["Shared", "packages/shared", "theme"],
      source_root: "packages/shared/theme",
      package_name: "@zstack/theme",
      asset_type: "library",
    },
  };
  respond({
    registry_state: "available",
    registry_commit: "a".repeat(40),
    items: [item],
    total: 1,
  });
  await router.navigate(`/spaces/${SHARED_SPACE_ID}/decisions`);
  render(<RouterProvider router={router} />);

  expect(await screen.findByRole("heading", { name: "theme" })).toBeVisible();
  expect(screen.getAllByText("Shared / packages/shared / theme").length)
    .toBeGreaterThan(0);
  expect(screen.queryByText(SHARED_COMPATIBILITY_NAME)).not.toBeInTheDocument();
});
