import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { router } from "../../app/router";
import type { CandidateInbox, ReviewDraft } from "../../api/types";


const PRODUCT_ID = "prod_56af5528bcf4f5a5dc629562dee92d01";
const SPACE_ID = "dsp_" + "9".repeat(32);
const REPOSITORY_ID = "repo_" + "1".repeat(32);
const FAMILY_ID = "cfm_" + "a".repeat(32);
const REVISION_ID = "crv_" + "b".repeat(32);
const DIGEST = "c".repeat(64);
const REQUEST_ID = "crq_" + "d".repeat(32);
const FAMILY_B = "cfm_" + "e".repeat(32);
const REVISION_B = "crv_" + "f".repeat(32);
const DIGEST_B = "1".repeat(64);
const FAMILY_C = "cfm_" + "2".repeat(32);
const REVISION_C = "crv_" + "3".repeat(32);
const DIGEST_C = "4".repeat(64);

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
    space: {
      decision_space_id: "dsp_" + "9".repeat(32),
      kind: "shared_unit",
      display_name: "theme",
      breadcrumb: ["Shared", "packages/shared", "theme"],
      source_root: "packages/shared/theme",
      package_name: "@zstack/theme",
      asset_type: "library",
    },
    repositories: [
      {
        repository_id: REPOSITORY_ID,
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
      decision_space_id: "dsp_" + "9".repeat(32),
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

function candidate(
  template: CandidateInbox["items"][number],
  familyId: string,
  claim: string,
  revisionId: string,
  digest: string,
): CandidateInbox["items"][number] {
  return {
    ...template,
    family_id: familyId,
    revision_id: revisionId,
    content_digest: digest,
    content: { ...template.content, claim },
    draft_action: null,
  };
}

function threeCandidateInbox(): CandidateInbox {
  const view = inbox();
  view.items[0].content.claim = "决策 A";
  view.items.push(
    candidate(view.items[0], FAMILY_B, "决策 B", REVISION_B, DIGEST_B),
    candidate(view.items[0], FAMILY_C, "决策 C", REVISION_C, DIGEST_C),
  );
  return view;
}

function mixedDraftInbox(): CandidateInbox {
  const view = threeCandidateInbox();
  const rejected = {
    ...draftItem("reject"),
    family_id: FAMILY_B,
    revision_id: REVISION_B,
    content_digest: DIGEST_B,
  };
  const edited = {
    ...draftItem("edit_accept"),
    family_id: FAMILY_C,
    revision_id: REVISION_C,
    content_digest: DIGEST_C,
    effective_content: view.items[2].content,
  };
  view.draft = { ...view.draft, version: 1, items: [rejected, edited] };
  view.items[1].draft_action = "reject";
  view.items[2].draft_action = "edit_accept";
  return view;
}

function candidateInboxWithCount(count: number): CandidateInbox {
  const view = inbox();
  view.items = Array.from({ length: count }, (_, index) => {
    const hex = (index + 1).toString(16).padStart(32, "0");
    return candidate(
      view.items[0],
      `cfm_${hex}`,
      `决策 ${String(index + 1).padStart(2, "0")}`,
      `crv_${hex}`,
      (index % 16).toString(16).repeat(64),
    );
  });
  return view;
}

interface CapturedRequests {
  draftPuts: Array<{ items: ReviewDraft["items"] }>;
  reviewPosts: Array<{ items: ReviewDraft["items"] }>;
  previewPosts: unknown[];
}

async function renderCandidatePage(
  view: CandidateInbox,
): Promise<CapturedRequests> {
  const requests: CapturedRequests = {
    draftPuts: [],
    reviewPosts: [],
    previewPosts: [],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/candidates")) return json(view);
      if (url.includes("/review-draft") && init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        requests.draftPuts.push(body);
        return json({ ...view.draft, version: view.draft.version + 1, items: body.items });
      }
      if (url.endsWith("/reviews") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        requests.reviewPosts.push(body);
        return json({
          review_batch_id: "rvb_" + "5".repeat(32),
          items: [],
          preview_eligible: body.items.some(
            (item: ReviewDraft["items"][number]) =>
              item.action === "accept" || item.action === "edit_accept",
          ),
          remaining_pending_count: view.items.length - body.items.length,
          draft_version: view.draft.version + 2,
        });
      }
      if (url.endsWith("/previews") && init?.method === "POST") {
        requests.previewPosts.push(JSON.parse(String(init.body)));
        return json({ preview_id: "pub_" + "6".repeat(32) });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }),
  );
  await router.navigate(`/spaces/${SPACE_ID}/candidates`);
  render(<RouterProvider router={router} />);
  await screen.findByRole("heading", { name: "theme" });
  return requests;
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

it("does not treat Checkbox selection as acceptance", async () => {
  const user = userEvent.setup();
  await renderCandidatePage(threeCandidateInbox());

  await user.click(screen.getByRole("checkbox", { name: "选择决策 A" }));

  expect(screen.getByText("已选 1 条")).toBeVisible();
  expect(
    within(screen.getByRole("article", { name: "候选 决策 A" }))
      .getByText("未处理"),
  ).toBeVisible();
});

it("batch accepts exactly selected rows and undo restores mixed actions", async () => {
  const user = userEvent.setup();
  await renderCandidatePage(mixedDraftInbox());

  expect(screen.queryByRole("group", { name: "编辑决策 C" }))
    .not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "编辑决策 C" }));
  expect(screen.getByRole("group", { name: "编辑决策 C" })).toBeVisible();
  await user.click(screen.getByRole("checkbox", { name: "选择决策 A" }));
  await user.click(screen.getByRole("checkbox", { name: "选择决策 C" }));
  await user.click(screen.getByRole("button", { name: "批量接受" }));

  expect(screen.queryByRole("group", { name: "编辑决策 C" }))
    .not.toBeInTheDocument();
  expect(within(screen.getByRole("article", { name: "候选 决策 A" })).getByText("已接受")).toBeVisible();
  expect(within(screen.getByRole("article", { name: "候选 决策 C" })).getByText("已接受")).toBeVisible();
  expect(within(screen.getByRole("article", { name: "候选 决策 B" })).getByText("已拒绝")).toBeVisible();

  await user.click(screen.getByRole("button", { name: "撤销" }));

  expect(within(screen.getByRole("article", { name: "候选 决策 A" })).getByText("未处理")).toBeVisible();
  expect(within(screen.getByRole("article", { name: "候选 决策 C" })).getByText(
    "编辑后接受",
    { selector: ".candidate-row__state strong" },
  )).toBeVisible();
});

it("blocks a 21-row batch instead of truncating it", async () => {
  const user = userEvent.setup();
  await renderCandidatePage(candidateInboxWithCount(21));

  await user.click(screen.getByRole("checkbox", { name: "选择当前页 21 条" }));

  expect(screen.getByText("单次最多审核 20 条")).toBeVisible();
  expect(screen.getByRole("button", { name: "批量接受" })).toBeDisabled();
  expect(within(screen.getByRole("region", { name: "候选决策列表" })).getAllByText("未处理"))
    .toHaveLength(21);
});

it("disables a twenty-first direct classification before state changes", async () => {
  const user = userEvent.setup();
  const view = candidateInboxWithCount(21);
  view.draft = {
    ...view.draft,
    version: 1,
    items: view.items.slice(0, 20).map((item) => ({
      family_id: item.family_id,
      repository_id: item.repository_id,
      revision_id: item.revision_id,
      revision: item.revision,
      content_digest: item.content_digest,
      action: "accept" as const,
      effective_content: null,
      note: null,
    })),
  };

  await renderCandidatePage(view);

  const lastRow = screen.getByRole("article", { name: "候选 决策 21" });
  expect(within(lastRow).getByRole("button", { name: "接受决策 21" }))
    .toBeDisabled();
  expect(within(lastRow).getByRole("button", { name: "拒绝决策 21" }))
    .toBeDisabled();
  expect(within(lastRow).getByRole("button", { name: "编辑决策 21" }))
    .toBeDisabled();
  expect(within(lastRow).getByText("未处理")).toBeVisible();
  expect(screen.getByText("单次最多审核 20 条")).toBeVisible();
  fireEvent.click(within(lastRow).getByRole("button", { name: "编辑决策 21" }));
  expect(within(lastRow).queryByRole("group", { name: "编辑决策 21" }))
    .not.toBeInTheDocument();

  const firstRow = screen.getByRole("article", { name: "候选 决策 01" });
  expect(within(firstRow).getByRole("button", { name: "编辑决策 01" }))
    .toBeEnabled();
  await user.click(within(firstRow).getByRole("button", { name: "编辑决策 01" }));
  expect(within(firstRow).getByRole("group", { name: "编辑决策 01" }))
    .toBeVisible();
  await user.click(within(firstRow).getByRole("button", {
    name: "保存并接受决策 01",
  }));
  expect(within(firstRow).getByText("编辑后接受")).toBeVisible();

  await user.click(within(lastRow).getByRole("checkbox", { name: "选择决策 21" }));
  expect(within(lastRow).getByText("未处理")).toBeVisible();
});

it("keeps editing single-row and transient until an explicit save", async () => {
  const user = userEvent.setup();
  const view = twoItemInbox("accept", "reject");
  view.items[0].content.claim = "决策 A";
  view.items[1].content.claim = "决策 B";
  const requests = await renderCandidatePage(view);
  const rowA = screen.getByRole("article", { name: "候选 决策 A" });
  const rowB = screen.getByRole("article", { name: "候选 决策 B" });
  const summary = screen.getByLabelText("审核汇总");

  await user.click(within(rowA).getByRole("button", { name: "编辑决策 A" }));
  expect(within(rowA).getByRole("group", { name: "编辑决策 A" })).toBeVisible();
  expect(within(rowB).queryByRole("group", { name: "编辑决策 B" }))
    .not.toBeInTheDocument();
  expect(within(rowA).getByText("已接受")).toBeVisible();
  expect(within(rowB).getByText("已拒绝")).toBeVisible();
  expect(summary).toHaveTextContent("1 已接受");
  expect(summary).toHaveTextContent("1 已拒绝");

  await user.click(within(rowB).getByRole("button", { name: "编辑决策 B" }));
  expect(within(rowA).queryByRole("group", { name: "编辑决策 A" }))
    .not.toBeInTheDocument();
  expect(within(rowB).getByRole("group", { name: "编辑决策 B" })).toBeVisible();
  expect(within(rowA).getByText("已接受")).toBeVisible();
  expect(within(rowB).getByText("已拒绝")).toBeVisible();
  expect(summary).toHaveTextContent("1 已接受");
  expect(summary).toHaveTextContent("1 已拒绝");

  await user.clear(within(rowB).getByRole("textbox", { name: "决策主张" }));
  await user.type(
    within(rowB).getByRole("textbox", { name: "决策主张" }),
    "未保存的决策 B",
  );
  await user.click(within(rowB).getByRole("button", { name: "取消编辑决策 B" }));
  expect(within(rowB).queryByRole("group", { name: "编辑决策 B" }))
    .not.toBeInTheDocument();
  expect(within(rowB).getByText("已拒绝")).toBeVisible();

  await user.click(screen.getByRole("button", { name: "保存审核草稿" }));
  await waitFor(() => expect(requests.draftPuts).toHaveLength(1));
  expect(requests.draftPuts[0].items.map(({ family_id, action }) => [
    family_id,
    action,
  ])).toEqual([
    [FAMILY_ID, "accept"],
    [FAMILY_B, "reject"],
  ]);

  await user.click(within(rowB).getByRole("button", { name: "编辑决策 B" }));
  await user.clear(within(rowB).getByRole("textbox", { name: "决策主张" }));
  await user.type(
    within(rowB).getByRole("textbox", { name: "决策主张" }),
    "已保存的决策 B",
  );
  await user.click(within(rowB).getByRole("button", {
    name: "保存并接受决策 B",
  }));

  expect(within(rowB).getByText("编辑后接受")).toBeVisible();
  expect(summary).toHaveTextContent("2 已接受");
  expect(summary).toHaveTextContent("0 已拒绝");
  await user.click(screen.getByRole("button", { name: "保存审核草稿" }));
  await waitFor(() => expect(requests.draftPuts).toHaveLength(2));
  expect(requests.draftPuts[1].items[1]).toMatchObject({
    family_id: FAMILY_B,
    action: "edit_accept",
    effective_content: { claim: "已保存的决策 B" },
  });

  await user.click(within(rowB).getByRole("button", { name: "编辑决策 B" }));
  await user.click(within(rowA).getByRole("button", { name: "接受决策 A" }));
  expect(within(rowB).queryByRole("group", { name: "编辑决策 B" }))
    .not.toBeInTheDocument();
});

it("submits only explicitly classified current revisions without creating Preview", async () => {
  const user = userEvent.setup();
  const requests = await renderCandidatePage(threeCandidateInbox());

  await user.click(screen.getByRole("button", { name: "接受决策 A" }));
  await user.click(screen.getByRole("button", { name: "拒绝决策 B" }));
  await user.click(screen.getByRole("checkbox", { name: "选择决策 C" }));
  await user.click(screen.getByRole("button", { name: "提交审核" }));

  await waitFor(() => expect(requests.reviewPosts).toHaveLength(1));
  expect(requests.reviewPosts[0].items.map((item) => item.family_id)).toEqual([
    FAMILY_ID,
    FAMILY_B,
  ]);
  expect(requests.previewPosts).toHaveLength(0);
});

it("clears transient selection on material filters while preserving local draft", async () => {
  const user = userEvent.setup();
  await renderCandidatePage(threeCandidateInbox());

  await user.click(screen.getByRole("checkbox", { name: "选择决策 A" }));
  await user.click(screen.getByRole("button", { name: "接受决策 B" }));
  await user.click(screen.getByRole("button", { name: "编辑决策 B" }));
  expect(screen.getByRole("group", { name: "编辑决策 B" })).toBeVisible();
  await user.type(screen.getByRole("searchbox", { name: "搜索候选决策" }), "decision");
  await user.click(screen.getByRole("button", { name: "应用筛选" }));

  await waitFor(() => expect(screen.getByText("已选 0 条")).toBeVisible());
  expect(screen.queryByRole("group", { name: "编辑决策 B" }))
    .not.toBeInTheDocument();
  expect(within(screen.getByRole("article", { name: "候选 决策 B" })).getByText("已接受")).toBeVisible();
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
    `/spaces/${SPACE_ID}/candidates?repository_id=${REPOSITORY_ID}`,
  );
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  expect(await screen.findByText("已接受", { selector: ".candidate-row__state strong" }))
    .toBeVisible();
  const heading = screen.getByRole("heading", { name: "theme" });
  expect(heading).toBeVisible();
  expect(within(heading.closest("header")!).getByText(
    "Shared / packages/shared / theme",
    { selector: ".decision-space-context span" },
  )).toBeVisible();
  expect(within(heading.closest("header")!).queryByText("ZDecision"))
    .not.toBeInTheDocument();
  fireEvent.click(screen.getByText("查看证据"));
  expect(within(screen.getByRole("article", {
    name: "候选 候选内容必须由用户明确审核。",
  })).getByText(REPOSITORY_ID, { selector: "code" })).toBeVisible();
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
    `/spaces/${SPACE_ID}/candidates?repository_id=${REPOSITORY_ID}` +
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

it("never executes Candidate markup and preserves exact provenance", async () => {
  const view = inbox();
  view.items[0].capture_request_ids = [REQUEST_ID];
  view.items[0].content.claim =
    '<button onclick="fetch(\'/secret\')">run</button>';
  vi.stubGlobal("fetch", vi.fn(() => json(view)));
  await router.navigate(`/spaces/${SPACE_ID}/candidates`);
  render(<RouterProvider router={router} />);

  const card = (await screen.findByText(/onclick=/)).closest("article")!;
  expect(screen.getByRole("link", { name: "候选审核" })).toHaveClass(
    "rail__link--active",
  );
  const details = within(card).getByText("查看证据").closest("details")!;
  expect(details).not.toHaveAttribute("open");
  fireEvent.click(within(card).getByText("查看证据"));
  expect(within(card).getByText(REVISION_ID)).toBeVisible();
  expect(within(card).getByText(DIGEST)).toBeVisible();
  expect(within(card).getByText(REPOSITORY_ID)).toBeVisible();
  expect(within(card).getByText(REQUEST_ID)).toBeVisible();
  expect(document.querySelector("button[onclick]")).toBeNull();
});

it("does not let a resolved stale repository poll schedule more work", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const secondRepository = "repo_" + "2".repeat(32);
  const view = inbox();
  view.repositories.push({
    repository_id: secondRepository,
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
  await router.navigate(`/spaces/${SPACE_ID}/candidates`);
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
  await router.navigate(`/spaces/${SPACE_ID}/candidates`);
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);
  await user.click(await screen.findByRole("button", {
    name: "拒绝候选内容必须由用户明确审核。",
  }));
  await user.click(screen.getByRole("button", { name: "保存审核草稿" }));

  expect(screen.getByText("已拒绝", { selector: ".candidate-row__state strong" }))
    .toBeVisible();
  expect(screen.getByText("审核草稿已在其他页面更新")).toBeVisible();
});

it("marks a restored action that still targets an older revision", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => json(inbox({ draftVersion: 3, action: "accept", stale: true }))),
  );
  await router.navigate(`/spaces/${SPACE_ID}/candidates`);
  render(<RouterProvider router={router} />);

  expect(await screen.findByText("已有新版本")).toBeVisible();
  expect(screen.getByText("已接受", { selector: ".candidate-row__state strong" }))
    .toBeVisible();
  expect(screen.getByRole("button", { name: "提交审核" })).toBeDisabled();
  expect(screen.queryByLabelText("审核动作")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "跳过" })).not.toBeInTheDocument();
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
    `/spaces/${SPACE_ID}/candidates?repository_id=${REPOSITORY_ID}`,
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
    json({ error: "not_found" }, 404),
  );
  vi.stubGlobal("fetch", fetcher);
  await router.navigate(`/?repository_id=${unknownRepository}`);
  render(<RouterProvider router={router} />);

  expect(await screen.findByText("仓库或决策空间未登记")).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher).toHaveBeenCalledWith(
    `/api/v1/web/repositories/${unknownRepository}/spaces`,
    expect.any(Object),
  );
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
  await router.navigate(`/spaces/${SPACE_ID}/candidates`);
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  await user.click(
    await screen.findByRole("button", { name: "提交审核" }),
  );

  expect(reviewBody).toEqual({
    client_action_id: expect.stringMatching(/^web_action_/),
    expected_draft_version: 1,
    items: view.draft.items,
  });
  expect(previewBody).toBeUndefined();
  expect(router.state.location.pathname).toBe(`/spaces/${SPACE_ID}/candidates`);

  await user.click(screen.getByRole("button", { name: "生成发布预览" }));

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
  await router.navigate(`/spaces/${SPACE_ID}/candidates`);
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  await user.click(
    await screen.findByRole("button", { name: "提交审核" }),
  );
  expect(previewBodies).toHaveLength(0);

  await user.click(screen.getByRole("button", { name: "生成发布预览" }));

  expect(await screen.findByText("审核已提交，但发布预览生成失败")).toBeVisible();
  const pending = JSON.parse(
    String(localStorage.getItem(`zdecision:preview:${SPACE_ID}`)),
  );
  expect(pending).toEqual({
    review_batch_id: reviewBatchId,
    client_action_id: expect.stringMatching(/^web_action_/),
  });

  await user.click(screen.getByRole("button", { name: "生成发布预览" }));

  expect(previewBodies).toEqual([
    { client_action_id: pending.client_action_id },
    { client_action_id: pending.client_action_id },
  ]);
  await waitFor(() =>
    expect(router.state.location.pathname).toBe(
      `/publication-previews/${previewId}`,
    ),
  );
  expect(localStorage.getItem(`zdecision:preview:${SPACE_ID}`)).toBeNull();
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
  await router.navigate(`/spaces/${SPACE_ID}/candidates`);
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  await user.click(
    await screen.findByRole("button", { name: "提交审核" }),
  );

  expect(screen.getByText("审核结果已提交")).toBeVisible();
  expect(screen.getByRole("button", { name: "提交审核" })).toBeDisabled();
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
  await router.navigate(`/spaces/${SPACE_ID}/candidates`);
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  await user.click(
    await screen.findByRole("button", { name: "提交审核" }),
  );

  expect(screen.getByText("已接受", { selector: ".candidate-row__state strong" }))
    .toBeVisible();
  expect(screen.getByText("已有新版本")).toBeVisible();
  expect(candidateReads).toBe(1);
  expect(reviewWrites).toBe(1);

  await user.click(screen.getByRole("button", { name: "载入最新版本" }));

  expect(await screen.findByText("最新候选版本")).toBeVisible();
  expect(screen.getByText("已接受", { selector: ".candidate-row__state strong" }))
    .toBeVisible();
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
  await router.navigate(`/spaces/${SPACE_ID}/candidates`);
  const user = userEvent.setup();
  render(<RouterProvider router={router} />);

  await user.click(
    await screen.findByRole("button", { name: "提交审核" }),
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
