import { render, screen } from "@testing-library/react";
import { RouterProvider } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { router } from "../../app/router";
import type { DecisionDetail } from "../../api/types";


const PRODUCT_ID = "prod_" + "1".repeat(32);
const DECISION_ID = "dec_" + "1".repeat(32);
const PUBLICATION_ID = "plb_" + "3".repeat(32);
const COMPATIBILITY_NAME = "Shared / packages/shared/theme";

function detail(): DecisionDetail {
  const formal = {
    format: "zdecision-decision/v1" as const,
    schema_version: 1 as const,
    decision_id: DECISION_ID,
    product_id: PRODUCT_ID,
    product_name: COMPATIBILITY_NAME,
    revision: 1 as const,
    lifecycle: "active" as const,
    claim: '<img src=x onerror="alert(1)">',
    future_action: "保持只读并验证提交绑定。",
    scope: {
      summary: "产品正式决策",
      repositories: ["cloud"],
      paths: ["decision-registry/products/"],
    },
    invalidation_conditions: ["产品归属发生变化"],
    supersedes: [],
    variant_of: [],
    source: { thread_id: "opaque-thread", turn_id: "opaque-turn" },
    review_approval: {
      actor: "user" as const,
      thread_id: "approval-thread",
      turn_id: "approval-turn",
      recorded_at: "2026-08-03T08:00:00Z",
    },
    publication_preview_id: "pub_" + "3".repeat(32),
  };
  return {
    ...formal,
    decision_space_id: "dsp_" + "9".repeat(32),
    space: {
      decision_space_id: "dsp_" + "9".repeat(32),
      kind: "shared_unit" as const,
      display_name: "theme",
      breadcrumb: ["Shared", "packages/shared", "theme"],
      source_root: "packages/shared/theme",
      package_name: "@zstack/theme",
      asset_type: "library",
    },
    canonical_json: JSON.stringify(formal),
    registry_commit: "a".repeat(40),
    publication_id: PUBLICATION_ID,
    published_at: "2026-08-04T08:00:00Z",
    commit_sha: "b".repeat(40),
  };
}

afterEach(() => vi.unstubAllGlobals());

it("renders Decision text inert and exposes no mutation controls", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(detail()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
  await router.navigate(
    `/spaces/${detail().decision_space_id}/decisions/${DECISION_ID}`,
  );
  render(<RouterProvider router={router} />);

  expect(
    await screen.findByText('<img src=x onerror="alert(1)">'),
  ).toBeVisible();
  expect(screen.getByRole("link", { name: "正式决策" })).toHaveClass(
    "rail__link--active",
  );
  expect(document.querySelector("img[src='x']")).toBeNull();
  expect(screen.getByText("产品归属发生变化")).toBeVisible();
  expect(screen.getByRole("heading", { name: "theme" })).toBeVisible();
  expect(screen.getByText("Shared / packages/shared / theme")).toBeVisible();
  expect(screen.queryByText(COMPATIBILITY_NAME)).not.toBeInTheDocument();
  expect(screen.getByText("opaque-thread")).toBeVisible();
  expect(screen.getByRole("link", { name: "查看发布凭据" })).toHaveAttribute(
    "href",
    `/publications/${PUBLICATION_ID}`,
  );
  expect(
    screen.queryByRole("button", { name: /编辑|删除|退休|取代/ }),
  ).not.toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});
