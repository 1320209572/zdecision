import { render, screen, within } from "@testing-library/react";
import { RouterProvider } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import { router } from "../../app/router";
import type { PublicationPreview } from "../../api/types";


const PRODUCT_ID = "prod_" + "1".repeat(32);
const PREVIEW_ID = "pub_" + "2".repeat(32);
const DECISION_ID = "dec_" + "3".repeat(32);
const DECISION_PATH =
  `decision-registry/products/${PRODUCT_ID}/decisions/${DECISION_ID}/r0001.json`;
const ROOT_PATH = "decision-registry/registry.json";
const PRODUCT_PATH = `decision-registry/products/${PRODUCT_ID}/product.json`;
const REGISTRY_PATH = `decision-registry/products/${PRODUCT_ID}/registry.json`;
const CLAIM = "<img src=x onerror=alert(1)>";

function file(path: string, content: string) {
  return { path, content, sha256: "a".repeat(64) };
}

function preview(
  publishability: PublicationPreview["publishability"] = "publishable",
): PublicationPreview {
  const decision = {
    format: "zdecision-decision/v1" as const,
    schema_version: 1 as const,
    decision_id: DECISION_ID,
    product_id: PRODUCT_ID,
    product_name: "ZDecision",
    revision: 1 as const,
    lifecycle: "active" as const,
    claim: CLAIM,
    future_action: "Inspect the exact publication bytes.",
    scope: {
      summary: "Exact central preview",
      repositories: ["zdecision"],
      paths: ["src/zdecision/central/web/"],
    },
    invalidation_conditions: ["The reviewed Candidate changes."],
    supersedes: [],
    variant_of: [],
    source: {
      thread_id: "candidate_family_" + "4".repeat(32),
      turn_id: "candidate_revision_" + "5".repeat(32),
    },
    review_approval: {
      actor: "user",
      thread_id: "web_review_" + "6".repeat(32),
      turn_id: "web_action_review-1",
      recorded_at: "2026-08-04T08:00:00Z",
    },
    publication_preview_id: PREVIEW_ID,
  };
  const canonicalJson = `${JSON.stringify(decision)}\n`;
  const displayDocuments = [
    file(ROOT_PATH, '{"format":"zdecision-registry/v1"}\n'),
    file(DECISION_PATH, canonicalJson),
    file(PRODUCT_PATH, '{"format":"zdecision-product/v1"}\n'),
    file(REGISTRY_PATH, '{"format":"zdecision-product-registry/v1"}\n'),
  ];
  return {
    record_version: 1,
    preview_id: PREVIEW_ID,
    content_digest: "b".repeat(64),
    state: "previewed",
    created_at: "2026-08-04T08:01:00Z",
    review_batch_id: "rvb_" + "7".repeat(32),
    review_ids: ["rvi_" + "8".repeat(32)],
    candidate_ids: ["cand_" + "9".repeat(32) + "_01"],
    decision_ids: [DECISION_ID],
    product_id: PRODUCT_ID,
    product_name: "ZDecision",
    base_commit: "c".repeat(40),
    base_registry_digests: {
      [ROOT_PATH]: "d".repeat(64),
      [PRODUCT_PATH]: "missing",
      [REGISTRY_PATH]: "missing",
      [DECISION_PATH]: "missing",
    },
    display_documents: displayDocuments,
    changed_files: displayDocuments,
    commit_message:
      `decision(${PRODUCT_ID}): publish 1 decisions\n\n` +
      `ZDecision-Preview: ${PREVIEW_ID}\n`,
    publishability,
    publication_id: null,
    decisions: [
      {
        path: DECISION_PATH,
        sha256: "a".repeat(64),
        canonical_json: canonicalJson,
        ...decision,
      },
    ],
  };
}

async function renderPreviewPage(value: PublicationPreview) {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(
    new Response(JSON.stringify(value), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  )));
  await router.navigate(`/publication-previews/${PREVIEW_ID}`);
  render(<RouterProvider router={router} />);
}

afterEach(() => vi.unstubAllGlobals());

it("shows exact files and uses the preview page as the only confirmation", async () => {
  await renderPreviewPage(preview());

  const publish = await screen.findByRole("button", {
    name: "确认发布 1 条决策",
  });
  expect(publish).toBeEnabled();
  expect(screen.getAllByRole("button")).toEqual([publish]);
  expect(screen.getAllByText(DECISION_PATH).length).toBeGreaterThan(0);
  expect(screen.getAllByText("完整 JSON").length).toBeGreaterThan(0);
  expect(screen.getByText("Registry 根索引")).toBeVisible();
  expect(screen.getByText("产品元数据")).toBeVisible();
  expect(screen.getByText("产品 Registry")).toBeVisible();
  expect(screen.getByText("提交消息")).toBeVisible();
  expect(screen.getByText("user")).toBeVisible();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("renders untrusted Decision text inertly and exposes every canonical byte", async () => {
  const value = preview();
  await renderPreviewPage(value);

  const claim = await screen.findByText(CLAIM);
  expect(claim).toBeVisible();
  expect(claim.closest("article")?.querySelector("img")).toBeNull();
  for (const document of value.display_documents) {
    const section = screen.getByTestId(`preview-file-${document.sha256}-${document.path}`);
    const exact = within(section).getByText("完整 JSON").parentElement!
      .querySelector("pre")!;
    expect(exact).toBeVisible();
    expect(exact.textContent).toBe(document.content);
  }
});

it.each([
  ["stale", "预览已过期"],
  ["registry_unavailable", "Registry 暂不可用"],
] as const)("disables the only publish action for %s", async (state, label) => {
  await renderPreviewPage(preview(state));

  expect((await screen.findAllByText(label))[0]).toBeVisible();
  const publish = screen.getByRole("button", {
    name: "确认发布 1 条决策",
  });
  expect(publish).toBeDisabled();
  expect(screen.getAllByRole("button")).toEqual([publish]);
});
