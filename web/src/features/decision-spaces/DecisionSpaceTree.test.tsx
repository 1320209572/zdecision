import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

import type { CatalogNode } from "../../api/types";
import { DecisionSpaceTree } from "./DecisionSpaceTree";

const THEME_SPACE_ID = "dsp_" + "2".repeat(32);

const sharedTreeFixture: CatalogNode = {
  node_id: "dsg_" + "1".repeat(32),
  kind: "catalog_group",
  display_name: "Shared",
  breadcrumb: ["Shared"],
  pending_candidate_count: 3,
  active_decision_count: 4,
  last_activity_at: "2026-08-04T00:00:00Z",
  space: null,
  children: [
    {
      node_id: "dsg_" + "3".repeat(32),
      kind: "catalog_group",
      display_name: "packages/shared",
      breadcrumb: ["Shared", "packages/shared"],
      pending_candidate_count: 3,
      active_decision_count: 4,
      last_activity_at: "2026-08-04T00:00:00Z",
      space: null,
      children: [
        {
          node_id: THEME_SPACE_ID,
          kind: "shared_unit",
          display_name: "theme",
          breadcrumb: ["Shared", "packages/shared", "theme"],
          pending_candidate_count: 3,
          active_decision_count: 4,
          last_activity_at: "2026-08-04T00:00:00Z",
          space: {
            decision_space_id: THEME_SPACE_ID,
            kind: "shared_unit",
            display_name: "theme",
            breadcrumb: ["Shared", "packages/shared", "theme"],
            source_root: "packages/shared/theme",
            package_name: "@zstack/theme",
            asset_type: "library",
            repository_ids: ["repo_" + "1".repeat(32)],
            pending_candidate_count: 3,
            active_decision_count: 4,
            last_activity_at: "2026-08-04T00:00:00Z",
          },
          children: [],
        },
      ],
    },
  ],
};

it("renders Shared groups without actions and package leaves with space links", () => {
  render(
    <MemoryRouter>
      <DecisionSpaceTree root={sharedTreeFixture} />
    </MemoryRouter>,
  );

  expect(screen.getByText("packages/shared")).toBeVisible();
  expect(screen.getByText("theme")).toBeVisible();
  expect(screen.getByText("Shared / packages/shared / theme")).toBeVisible();
  expect(screen.getByText("packages/shared/theme")).toBeVisible();
  expect(screen.getByText("组件库 · @zstack/theme")).toBeVisible();
  expect(screen.getByRole("link", { name: "theme 候选" })).toHaveAttribute(
    "href",
    `/spaces/${THEME_SPACE_ID}/candidates`,
  );
  expect(screen.getByRole("link", { name: "theme 决策" })).toHaveAttribute(
    "href",
    `/spaces/${THEME_SPACE_ID}/decisions`,
  );
  expect(screen.queryByRole("link", { name: "Shared 候选" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "折叠 Shared" })).toBeVisible();
});
