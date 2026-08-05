import { render, screen } from "@testing-library/react";
import { RouterProvider } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { router } from "../../app/router";


const CLOUD_ID = "prod_" + "1".repeat(32);
const ZMETIS_ID = "prod_" + "2".repeat(32);

function publication(productId: string, productName: string, ordinal: string) {
  const decisionSpaceId = "dsp_" + productId.slice(-32);
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
    publication_id: "plb_" + ordinal.repeat(32),
    preview_id: "pub_" + ordinal.repeat(32),
    product_id: productId,
    product_name: productName,
    decision_count: 2,
    decision_ids: ["dec_" + ordinal.repeat(32)],
    actor_id: "user_demo",
    approved_at: "2026-08-04T08:00:00Z",
    state: "completed",
    recovery_code: null,
    commit_sha: ordinal.repeat(40),
  };
}

function respond(value: unknown) {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(
    new Response(JSON.stringify(value), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  )));
}

afterEach(() => vi.unstubAllGlobals());

it("groups global history without permitting cross-product mutation", async () => {
  respond({
    items: [
      publication(CLOUD_ID, "ZStack Cloud", "3"),
      publication(ZMETIS_ID, "ZMetis", "4"),
    ],
    total: 2,
    limit: 50,
    offset: 0,
  });
  await router.navigate("/publications");
  render(<RouterProvider router={router} />);

  expect(await screen.findByText("ZStack Cloud")).toBeVisible();
  expect(screen.getByText("ZMetis")).toBeVisible();
  expect(screen.queryByRole("button", { name: /批量发布/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("shows safe resume only for pending push and never for ambiguity", async () => {
  const pending = {
    ...publication(CLOUD_ID, "ZStack Cloud", "5"),
    state: "committed_pending_push",
  };
  respond(pending);
  await router.navigate(`/publications/${pending.publication_id}`);
  const view = render(<RouterProvider router={router} />);

  expect(await screen.findByText("已提交，等待推送")).toBeVisible();
  expect(screen.getByRole("button", { name: "继续安全推送" })).toBeVisible();
  expect(screen.getByRole("link", { name: "决策空间目录" })).toHaveAttribute(
    "href",
    `/spaces/${pending.decision_space_id}/decisions`,
  );

  view.unmount();
  const ambiguous = {
    ...pending,
    state: "ambiguous",
    recovery_code: "ambiguous",
  };
  respond(ambiguous);
  await router.navigate(`/publications/${ambiguous.publication_id}`);
  render(<RouterProvider router={router} />);

  expect(await screen.findByText("需要人工处理")).toBeVisible();
  expect(screen.queryByRole("button", { name: "继续安全推送" })).not.toBeInTheDocument();
});

it("uses Shared leaf identity in canonical history and detail", async () => {
  const compatibilityName = "Shared / packages/shared/theme";
  const shared = {
    ...publication("prod_" + "6".repeat(32), compatibilityName, "6"),
    space: {
      decision_space_id: "dsp_" + "6".repeat(32),
      kind: "shared_unit",
      display_name: "theme",
      breadcrumb: ["Shared", "packages/shared", "theme"],
      source_root: "packages/shared/theme",
      package_name: "@zstack/theme",
      asset_type: "library",
    },
  };
  respond({ items: [shared], total: 1, limit: 50, offset: 0 });
  await router.navigate(`/spaces/${shared.decision_space_id}/publications`);
  const history = render(<RouterProvider router={router} />);

  expect(await screen.findByRole("heading", { name: "theme" })).toBeVisible();
  expect(screen.getByText("Shared / packages/shared / theme")).toBeVisible();
  expect(screen.queryByText(compatibilityName)).not.toBeInTheDocument();

  history.unmount();
  respond(shared);
  await router.navigate(`/publications/${shared.publication_id}`);
  render(<RouterProvider router={router} />);

  expect(await screen.findByRole("heading", { name: "theme" })).toBeVisible();
  expect(screen.getByText("@zstack/theme · library")).toBeVisible();
  expect(screen.queryByText(compatibilityName)).not.toBeInTheDocument();
});
