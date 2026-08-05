import { render, screen } from "@testing-library/react";
import { RouterProvider } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { router } from "../../app/router";
import type { Dashboard } from "../../api/types";


const PRODUCT_ID = "prod_56af5528bcf4f5a5dc629562dee92d01";

function mockDashboard(
  product: Pick<Dashboard["products"][number], "display_name">,
) {
  const dashboard: Dashboard = {
    metrics: {
      product_count: 1,
      pending_candidate_count: 12,
      active_decision_count: 14,
      completed_this_week: 2,
    },
    registry: { state: "available", commit_sha: "a".repeat(40) },
    products: [
      {
        ...product,
        decision_space_id: "dsp_" + "9".repeat(32),
        kind: "product",
        breadcrumb: [product.display_name],
        source_root: "packages/products/cloud",
        package_name: null,
        asset_type: null,
        repository_ids: ["repo_" + "1".repeat(32)],
        pending_candidate_count: 12,
        active_decision_count: 14,
        last_activity_at: "2026-08-04T00:00:00Z",
      },
    ],
    shared_tree: null,
    recent_publications: [],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(dashboard), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
}

afterEach(() => vi.unstubAllGlobals());

it("renders server products and routes without hard-coded product pages", async () => {
  mockDashboard({ display_name: "ZStack Cloud" });
  render(<RouterProvider router={router} />);
  expect(await screen.findByText("ZStack Cloud")).toBeVisible();
  expect(screen.getByRole("link", { name: "候选审核" })).toHaveAttribute(
    "href",
    "/reviews",
  );
  expect(screen.getByRole("link", { name: "候选 12" })).toHaveAttribute(
    "href",
    `/spaces/${"dsp_" + "9".repeat(32)}/candidates`,
  );
  expect(screen.getByRole("link", { name: "决策 14" })).toHaveAttribute(
    "href",
    `/spaces/${"dsp_" + "9".repeat(32)}/decisions`,
  );
  expect(screen.getByRole("link", { name: "发布" })).toHaveAttribute(
    "href",
    `/spaces/${"dsp_" + "9".repeat(32)}/publications`,
  );
  expect(screen.queryByText(/session_id/i)).not.toBeInTheDocument();
});
