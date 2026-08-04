import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { router } from "../../app/router";
import type { CandidateInbox, ReviewDraft } from "../../api/types";


const PRODUCT_ID = "prod_56af5528bcf4f5a5dc629562dee92d01";
const REPOSITORY_ID = "repo_" + "1".repeat(32);
const FAMILY_ID = "cfm_" + "a".repeat(32);
const REVISION_ID = "crv_" + "b".repeat(32);
const DIGEST = "c".repeat(64);
const REQUEST_ID = "crq_" + "d".repeat(32);

function draftItem(action: ReviewDraft["items"][number]["action"]) {
  return {
    family_id: FAMILY_ID,
    repository_id: REPOSITORY_ID,
    revision_id: REVISION_ID,
    revision: 1,
    content_digest: DIGEST,
    action,
    effective_content: null,
    note: null,
  };
}

function inbox(options?: {
  draftVersion?: number;
  action?: ReviewDraft["items"][number]["action"];
  stale?: boolean;
}): CandidateInbox {
  const action = options?.action;
  return {
    product_id: PRODUCT_ID,
    product_name: "ZDecision",
    repositories: [
      {
        repository_id: REPOSITORY_ID,
        product_id: PRODUCT_ID,
        product_name: "ZDecision",
        enabled: true,
      },
    ],
    items: [
      {
        family_id: FAMILY_ID,
        repository_id: REPOSITORY_ID,
        capture_request_ids: [],
        revision_id: REVISION_ID,
        revision: 1,
        content_digest: DIGEST,
        content: {
          product: "ZDecision",
          claim: "候选内容必须由用户明确审核。",
          future_action: "保留显式更新边界。",
          scope_summary: "中央候选审核",
          repositories: ["zdecision"],
          paths: ["src/zdecision/central/web/"],
          invalidation_conditions: ["审核边界发生变化。"],
        },
        review_state: "pending",
        draft_action: action ?? null,
        stale_draft: options?.stale ?? false,
      },
    ],
    draft: {
      organization_id: "org_demo",
      actor_id: "user_demo",
      product_id: PRODUCT_ID,
      version: options?.draftVersion ?? 0,
      items: action ? [draftItem(action)] : [],
      updated_at: action ? "2026-08-04T08:00:00Z" : null,
    },
  };
}

function json(value: unknown, status = 200): Promise<Response> {
  return Promise.resolve(
    new Response(JSON.stringify(value), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

beforeEach(() => localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

it("refreshes one owned repository and restores a partial draft", async () => {
  let captureBody: unknown;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/candidates")) return json(inbox({ draftVersion: 2, action: "accept" }));
      if (url === "/api/v1/capture-requests" && init?.method === "POST") {
        captureBody = JSON.parse(String(init.body));
        return json({
          request_id: REQUEST_ID,
          repository_id: REPOSITORY_ID,
          product_id: PRODUCT_ID,
          product_name: "ZDecision",
          template_id: "business",
          state: "queued",
          progress_code: "queued",
          candidate_revision_count: null,
          last_sequence: 1,
          created_at: "2026-08-04T08:00:00Z",
          updated_at: "2026-08-04T08:00:00Z",
        });
      }
      if (url.includes(`/capture-requests/${REQUEST_ID}/events`)) {
        return json({ events: [] });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
  await router.navigate(
    `/products/${PRODUCT_ID}/candidates?repository_id=${REPOSITORY_ID}`,
  );
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  expect(await screen.findByDisplayValue("接受")).toBeVisible();
  expect(screen.getByText("zdecision")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "更新候选决策" }));

  expect(captureBody).toEqual({
    repository_id: REPOSITORY_ID,
    template_id: "business",
    capture_scope: "all_valid_sessions",
    client_action_id: expect.stringMatching(/^web_action_/),
  });
  expect(
    JSON.parse(String(localStorage.getItem(`zdecision:capture:${REPOSITORY_ID}`))),
  ).toEqual({
    request_id: REQUEST_ID,
    repository_id: REPOSITORY_ID,
    last_sequence: 1,
  });
});

it("keeps local actions visible when draft compare-and-swap conflicts", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/candidates")) return json(inbox());
      if (url.includes("/review-draft") && init?.method === "PUT") {
        return json({ error: "review_draft_conflict" }, 409);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
  await router.navigate(`/products/${PRODUCT_ID}/candidates`);
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);
  const selector = await screen.findByLabelText("审核动作");

  await user.selectOptions(selector, "reject");
  await user.click(screen.getByRole("button", { name: "保存审核草稿" }));

  expect(screen.getByDisplayValue("拒绝")).toBeVisible();
  expect(screen.getByText("审核草稿已在其他页面更新")).toBeVisible();
});

it("marks a restored action that still targets an older revision", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => json(inbox({ draftVersion: 3, action: "accept", stale: true }))),
  );
  await router.navigate(`/products/${PRODUCT_ID}/candidates`);
  render(<RouterProvider router={router} />);

  expect(await screen.findByText("已有新版本")).toBeVisible();
  expect(screen.getByDisplayValue("接受")).toBeVisible();
});

it("resumes durable capture progress from the stored event cursor", async () => {
  localStorage.setItem(
    `zdecision:capture:${REPOSITORY_ID}`,
    JSON.stringify({
      request_id: REQUEST_ID,
      repository_id: REPOSITORY_ID,
      last_sequence: 4,
    }),
  );
  const fetcher = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/candidates")) return json(inbox());
    if (url.endsWith(`/events?after_sequence=4`)) {
      return json({
        events: [
          {
            request_id: REQUEST_ID,
            sequence: 5,
            state: "succeeded_no_candidates",
            code: "succeeded_no_candidates",
            occurred_at: "2026-08-04T08:01:00Z",
          },
        ],
      });
    }
    throw new Error(`Unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", fetcher);
  await router.navigate(
    `/products/${PRODUCT_ID}/candidates?repository_id=${REPOSITORY_ID}`,
  );
  render(<RouterProvider router={router} />);

  expect(await screen.findByText("更新完成，未发现新候选决策")).toBeVisible();
  await waitFor(() =>
    expect(fetcher).toHaveBeenCalledWith(
      `/api/v1/capture-requests/${REQUEST_ID}/events?after_sequence=4`,
      expect.any(Object),
    ),
  );
});

it("does not guess a product route for an unknown repository deep link", async () => {
  const unknownRepository = "repo_" + "f".repeat(32);
  const fetcher = vi.fn(() =>
    json({
      repositories: [
        {
          repository_id: REPOSITORY_ID,
          product_id: PRODUCT_ID,
          product_name: "ZDecision",
          enabled: true,
        },
      ],
    }),
  );
  vi.stubGlobal("fetch", fetcher);
  await router.navigate(`/?repository_id=${unknownRepository}`);
  render(<RouterProvider router={router} />);

  expect(await screen.findByText("仓库未登记或未启用")).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(1);
});
