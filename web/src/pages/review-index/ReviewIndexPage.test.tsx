import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { RepositorySpacesView } from "../../api/types";
import { ReviewIndexPage } from "./ReviewIndexPage";

const REPOSITORY_ID = "repo_" + "1".repeat(32);
const PRODUCT_SPACE_ID = "dsp_" + "2".repeat(32);
const SHARED_SPACE_ID = "dsp_" + "3".repeat(32);

afterEach(() => vi.unstubAllGlobals());

it("groups repository leaves without guessing one product", async () => {
  const view: RepositorySpacesView = {
    repository_id: REPOSITORY_ID,
    spaces: [
      {
        decision_space_id: PRODUCT_SPACE_ID,
        kind: "product",
        display_name: "Cloud",
        breadcrumb: ["Cloud"],
        source_root: "packages/products/cloud",
        package_name: null,
        asset_type: null,
        repository_ids: [REPOSITORY_ID],
        pending_candidate_count: 2,
        active_decision_count: 4,
        last_activity_at: null,
      },
      {
        decision_space_id: SHARED_SPACE_ID,
        kind: "shared_unit",
        display_name: "theme",
        breadcrumb: ["Shared", "packages/shared", "theme"],
        source_root: "packages/shared/theme",
        package_name: "@zstack/theme",
        asset_type: "library",
        repository_ids: [REPOSITORY_ID],
        pending_candidate_count: 1,
        active_decision_count: 3,
        last_activity_at: null,
      },
    ],
  };
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(view), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);

  render(
    <MemoryRouter initialEntries={[`/reviews?repository_id=${REPOSITORY_ID}`]}>
      <Routes><Route path="/reviews" element={<ReviewIndexPage />} /></Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "产品" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "Shared" })).toBeVisible();
  expect(screen.getByRole("link", { name: /Cloud/ })).toHaveAttribute(
    "href",
    `/spaces/${PRODUCT_SPACE_ID}/candidates?repository_id=${REPOSITORY_ID}`,
  );
  expect(screen.getByRole("link", { name: /theme/ })).toHaveAttribute(
    "href",
    `/spaces/${SHARED_SPACE_ID}/candidates?repository_id=${REPOSITORY_ID}`,
  );
  expect(fetchMock).toHaveBeenCalledWith(
    `/api/v1/web/repositories/${REPOSITORY_ID}/spaces`,
    expect.anything(),
  );
});
