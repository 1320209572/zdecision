import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
const FAMILY_B = "cfm_" + "e".repeat(32);
const REVISION_B = "crv_" + "f".repeat(32);
const DIGEST_B = "1".repeat(64);

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

function twoItemInbox(
  firstAction: ReviewDraft["items"][number]["action"],
  secondAction: ReviewDraft["items"][number]["action"],
): CandidateInbox {
  const view = inbox({ draftVersion: 1, action: firstAction });
  const second = {
    ...view.items[0],
    family_id: FAMILY_B,
    revision_id: REVISION_B,
    content_digest: DIGEST_B,
    content: {
      ...view.items[0].content,
      claim: "第二条候选用于部分审核。",
    },
    draft_action: secondAction,
  };
  view.items.push(second);
  view.draft.items.push({
    ...draftItem(secondAction),
    family_id: FAMILY_B,
    revision_id: REVISION_B,
    content_digest: DIGEST_B,
  });
  return view;
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
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

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

it("exposes safe Inbox filters and sends every approved filter", async () => {
  const candidateUrls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/candidates")) {
        candidateUrls.push(url);
        return json(inbox());
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
  await router.navigate(
    `/products/${PRODUCT_ID}/candidates?repository_id=${REPOSITORY_ID}` +
      `&capture_request_id=${REQUEST_ID}&search=explicit&state=accepted`,
  );
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  const search = await screen.findByRole("searchbox", {
    name: "搜索候选决策",
  });
  expect(search).toHaveValue("explicit");
  expect(screen.getByLabelText("筛选仓库")).toHaveValue(REPOSITORY_ID);
  expect(screen.getByLabelText("Capture Request ID")).toHaveValue(REQUEST_ID);
  const state = screen.getByLabelText("审核状态");
  expect(state).toHaveValue("accepted");
  for (const label of ["待审核", "已接受", "已拒绝", "已发布", "全部"]) {
    expect(within(state).getByRole("option", { name: label })).toBeVisible();
  }

  await user.clear(search);
  await user.type(search, "durable review");
  await user.selectOptions(state, "published");
  await user.click(screen.getByRole("button", { name: "应用筛选" }));

  await waitFor(() => expect(candidateUrls).toHaveLength(2));
  const request = new URL(candidateUrls.at(-1)!, "https://example.test");
  expect(Object.fromEntries(request.searchParams)).toEqual({
    search: "durable review",
    repository_id: REPOSITORY_ID,
    capture_request_id: REQUEST_ID,
    state: "published",
  });
});

it("renders exact safe Candidate provenance as text, never markup", async () => {
  const view = inbox();
  view.items[0].capture_request_ids = [REQUEST_ID];
  view.items[0].content.claim = "<img src=x onerror=alert(1)>";
  vi.stubGlobal("fetch", vi.fn(() => json(view)));
  await router.navigate(`/products/${PRODUCT_ID}/candidates`);
  render(<RouterProvider router={router} />);

  const card = (await screen.findByText("<img src=x onerror=alert(1)>")).closest(
    "article",
  )!;
  expect(within(card).getByText(REVISION_ID)).toBeVisible();
  expect(within(card).getByText(DIGEST)).toBeVisible();
  expect(within(card).getByText(REPOSITORY_ID)).toBeVisible();
  expect(within(card).getByText(REQUEST_ID)).toBeVisible();
  expect(within(card).queryByRole("img")).not.toBeInTheDocument();
});

it("does not let a resolved stale repository poll schedule more work", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const secondRepository = "repo_" + "2".repeat(32);
  const view = inbox();
  view.repositories.push({
    repository_id: secondRepository,
    product_id: PRODUCT_ID,
    product_name: "ZDecision",
    enabled: true,
  });
  localStorage.setItem(
    `zdecision:capture:${REPOSITORY_ID}`,
    JSON.stringify({
      request_id: REQUEST_ID,
      repository_id: REPOSITORY_ID,
      last_sequence: 4,
    }),
  );
  let resolveEvents!: (response: Response) => void;
  const deferredEvents = new Promise<Response>((resolve) => {
    resolveEvents = resolve;
  });
  const eventUrls: string[] = [];
  let candidateReads = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/candidates")) {
        candidateReads += 1;
        return json(view);
      }
      if (url.includes("/events")) {
        eventUrls.push(url);
        return deferredEvents;
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
  await router.navigate(`/products/${PRODUCT_ID}/candidates`);
  render(<RouterProvider router={router} />);
  const repository = await screen.findByLabelText("登记仓库");
  await waitFor(() => expect(eventUrls).toHaveLength(1));

  fireEvent.change(repository, { target: { value: secondRepository } });
  resolveEvents(
    new Response(JSON.stringify({ events: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  await vi.advanceTimersByTimeAsync(1100);

  expect(eventUrls).toHaveLength(1);
  expect(candidateReads).toBe(1);
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

it("submits an ordered partial accept and reject for preview eligibility", async () => {
  const view = twoItemInbox("accept", "reject");
  let reviewBody: unknown;
  let previewBody: unknown;
  const previewId = "pub_" + "4".repeat(32);
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/candidates")) return json(view);
      if (url.endsWith("/reviews") && init?.method === "POST") {
        reviewBody = JSON.parse(String(init.body));
        return json({
          review_batch_id: "rvb_" + "2".repeat(32),
          items: view.draft.items.map((item, index) => ({
            review_id: "rvi_" + String(index + 1).repeat(32),
            family_id: item.family_id,
            publication_candidate_id:
              "cand_" + item.family_id.slice(4) + "_01",
            repository_id: item.repository_id,
            revision_id: item.revision_id,
            revision: item.revision,
            content_digest: item.content_digest,
            action: item.action,
          })),
          preview_eligible: true,
          remaining_pending_count: 0,
          draft_version: 2,
        });
      }
      if (url.endsWith("/previews") && init?.method === "POST") {
        previewBody = JSON.parse(String(init.body));
        return json({ preview_id: previewId });
      }
      if (url.endsWith(`/publication-previews/${previewId}`)) {
        return json({ error: "fixture_stops_after_navigation" }, 503);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
  await router.navigate(`/products/${PRODUCT_ID}/candidates`);
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  await user.click(
    await screen.findByRole("button", { name: "生成发布预览" }),
  );

  expect(reviewBody).toEqual({
    client_action_id: expect.stringMatching(/^web_action_/),
    expected_draft_version: 1,
    items: view.draft.items,
  });
  expect(previewBody).toEqual({
    client_action_id: expect.stringMatching(/^web_action_/),
  });
  await waitFor(() =>
    expect(router.state.location.pathname).toBe(
      `/publication-previews/${previewId}`,
    ),
  );
});

it("retries a failed preview with the same durable action identity", async () => {
  const view = inbox({ draftVersion: 1, action: "accept" });
  const reviewBatchId = "rvb_" + "5".repeat(32);
  const previewId = "pub_" + "6".repeat(32);
  const previewBodies: Array<{ client_action_id: string }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/candidates")) return json(view);
      if (url.endsWith("/reviews") && init?.method === "POST") {
        return json({
          review_batch_id: reviewBatchId,
          items: [],
          preview_eligible: true,
          remaining_pending_count: 0,
          draft_version: 2,
        });
      }
      if (url.endsWith("/previews") && init?.method === "POST") {
        previewBodies.push(JSON.parse(String(init.body)));
        return previewBodies.length === 1
          ? json({ error: "registry_unavailable" }, 503)
          : json({ preview_id: previewId });
      }
      if (url.endsWith(`/publication-previews/${previewId}`)) {
        return json({ error: "fixture_stops_after_navigation" }, 503);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
  await router.navigate(`/products/${PRODUCT_ID}/candidates`);
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  await user.click(
    await screen.findByRole("button", { name: "生成发布预览" }),
  );

  expect(await screen.findByText("审核已提交，但发布预览生成失败")).toBeVisible();
  const pending = JSON.parse(
    String(localStorage.getItem(`zdecision:preview:${PRODUCT_ID}`)),
  );
  expect(pending).toEqual({
    review_batch_id: reviewBatchId,
    client_action_id: expect.stringMatching(/^web_action_/),
  });

  await user.click(screen.getByRole("button", { name: "重试生成发布预览" }));

  expect(previewBodies).toEqual([
    { client_action_id: pending.client_action_id },
    { client_action_id: pending.client_action_id },
  ]);
  await waitFor(() =>
    expect(router.state.location.pathname).toBe(
      `/publication-previews/${previewId}`,
    ),
  );
  expect(localStorage.getItem(`zdecision:preview:${PRODUCT_ID}`)).toBeNull();
});

it("submits reject-only review without claiming a preview", async () => {
  const view = inbox({ draftVersion: 1, action: "reject" });
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/candidates")) return json(view);
      if (url.endsWith("/reviews") && init?.method === "POST") {
        return json({
          review_batch_id: "rvb_" + "3".repeat(32),
          items: [
            {
              review_id: "rvi_" + "3".repeat(32),
              family_id: FAMILY_ID,
              publication_candidate_id:
                "cand_" + FAMILY_ID.slice(4) + "_01",
              repository_id: REPOSITORY_ID,
              revision_id: REVISION_ID,
              revision: 1,
              content_digest: DIGEST,
              action: "reject",
            },
          ],
          preview_eligible: false,
          remaining_pending_count: 0,
          draft_version: 2,
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
  await router.navigate(`/products/${PRODUCT_ID}/candidates`);
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  await user.click(
    await screen.findByRole("button", { name: "提交审核结果" }),
  );

  expect(screen.getByText("审核结果已提交")).toBeVisible();
  expect(screen.getByRole("button", { name: "提交审核结果" })).toBeDisabled();
});

it("retains a stale selection and loads the latest revision without resubmitting", async () => {
  const original = inbox({ draftVersion: 1, action: "accept" });
  const latest = inbox({ draftVersion: 1, action: "accept" });
  latest.items[0] = {
    ...latest.items[0],
    revision: 2,
    revision_id: "crv_" + "9".repeat(32),
    content_digest: "8".repeat(64),
    content: { ...latest.items[0].content, claim: "最新候选版本" },
  };
  let candidateReads = 0;
  let reviewWrites = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/candidates")) {
        candidateReads += 1;
        return json(candidateReads === 1 ? original : latest);
      }
      if (url.endsWith("/reviews") && init?.method === "POST") {
        reviewWrites += 1;
        return json(
          { error: "review_stale", family_ids: [FAMILY_ID] },
          409,
        );
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
  await router.navigate(`/products/${PRODUCT_ID}/candidates`);
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  await user.click(
    await screen.findByRole("button", { name: "生成发布预览" }),
  );

  expect(screen.getByDisplayValue("接受")).toBeVisible();
  expect(screen.getByText("已有新版本")).toBeVisible();
  expect(candidateReads).toBe(1);
  expect(reviewWrites).toBe(1);

  await user.click(screen.getByRole("button", { name: "载入最新版本" }));

  expect(await screen.findByText("最新候选版本")).toBeVisible();
  expect(screen.getByDisplayValue("接受")).toBeVisible();
  expect(screen.queryByText("已有新版本")).not.toBeInTheDocument();
  expect(reviewWrites).toBe(1);
});

it("merges remote-only draft choices before adopting a newer CAS version", async () => {
  const original = inbox({ draftVersion: 1, action: "accept" });
  const latest = twoItemInbox("accept", "reject");
  latest.draft.version = 2;
  latest.items[0] = {
    ...latest.items[0],
    revision: 2,
    revision_id: "crv_" + "7".repeat(32),
    content_digest: "6".repeat(64),
  };
  let candidateReads = 0;
  let reviewWrites = 0;
  let savedBody: {
    expected_version: number;
    items: ReviewDraft["items"];
  } | null = null;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/candidates")) {
        candidateReads += 1;
        return json(candidateReads === 1 ? original : latest);
      }
      if (url.endsWith("/reviews") && init?.method === "POST") {
        reviewWrites += 1;
        return json(
          { error: "review_stale", family_ids: [FAMILY_ID] },
          409,
        );
      }
      if (url.includes("/review-draft") && init?.method === "PUT") {
        savedBody = JSON.parse(String(init.body));
        return json({
          ...latest.draft,
          version: 3,
          items: savedBody!.items,
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
  await router.navigate(`/products/${PRODUCT_ID}/candidates`);
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  await user.click(
    await screen.findByRole("button", { name: "生成发布预览" }),
  );
  await user.click(screen.getByRole("button", { name: "载入最新版本" }));
  await user.click(screen.getByRole("button", { name: "保存审核草稿" }));

  expect(savedBody).not.toBeNull();
  expect(savedBody!.expected_version).toBe(2);
  expect(savedBody!.items.map((item) => [item.family_id, item.action])).toEqual([
    [FAMILY_ID, "accept"],
    [FAMILY_B, "reject"],
  ]);
  expect(reviewWrites).toBe(1);
});
