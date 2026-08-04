import { render, screen } from "@testing-library/react";
import { RouterProvider } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { router } from "../../app/router";
import type { Dashboard } from "../../api/types";


const PRODUCT_ID = "prod_56af5528bcf4f5a5dc629562dee92d01";

function mockDashboard(
  product: Pick<Dashboard["products"][number], "product_id" | "product_name">,
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
        repository_ids: ["repo_" + "1".repeat(32)],
        pending_candidate_count: 12,
        active_decision_count: 14,
        last_activity_at: "2026-08-04T00:00:00Z",
      },
    ],
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
  mockDashboard({ product_id: PRODUCT_ID, product_name: "ZStack Cloud" });
  render(<RouterProvider router={router} />);
  expect(await screen.findByText("ZStack Cloud")).toBeVisible();
  expect(screen.getByRole("link", { name: "候选审核" })).toHaveAttribute(
    "href",
    "/reviews",
  );
  expect(screen.queryByText(/session_id/i)).not.toBeInTheDocument();
});
