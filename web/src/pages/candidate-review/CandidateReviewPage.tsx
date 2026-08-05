import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";

import { api, ApiError } from "../../api/client";
import type {
  CandidateContent,
  CandidateInbox,
  CandidateInboxItem,
  ClassifiedReviewAction,
  ReviewDraft,
  ReviewDraftItem,
  ReviewSubmissionResult,
} from "../../api/types";
import { useCandidateRefresh } from "../../features/candidate-refresh/useCandidateRefresh";
import { DecisionSpaceContext } from "../../features/decision-spaces/DecisionSpaceContext";
import { CandidateReviewRow } from "../../features/reviews/CandidateReviewRow";
import { AsyncState } from "../../shared/AsyncState";


type CandidateStateFilter =
  | "pending"
  | "accepted"
  | "rejected"
  | "published"
  | "all";

const candidateStates = new Set<CandidateStateFilter>([
  "pending",
  "accepted",
  "rejected",
  "published",
  "all",
]);

const MAX_REVIEW_ITEMS = 20;

interface BatchUndo {
  message: string;
  previousByFamily: Map<string, ReviewDraftItem | undefined>;
}

function isClassified(
  action: ReviewDraftItem | undefined,
): action is ReviewDraftItem & { action: ClassifiedReviewAction } {
  return action?.action === "accept" ||
    action?.action === "edit_accept" ||
    action?.action === "reject";
}

function actionForCandidate(
  item: CandidateInboxItem,
  action: ClassifiedReviewAction,
  effectiveContent: CandidateContent | null = null,
): ReviewDraftItem {
  return {
    family_id: item.family_id,
    repository_id: item.repository_id,
    revision_id: item.revision_id,
    revision: item.revision,
    content_digest: item.content_digest,
    action,
    effective_content: action === "edit_accept" ? effectiveContent : null,
    note: null,
  };
}

function staleFamilyIds(error: ApiError): string[] {
  if (typeof error.details !== "object" || error.details === null) return [];
  if (!("family_ids" in error.details)) return [];
  const values = error.details.family_ids;
  if (!Array.isArray(values)) return [];
  return values.filter(
    (value): value is string =>
      typeof value === "string" && /^cfm_[0-9a-f]{32}$/.test(value),
  );
}

interface PendingPreviewAction {
  review_batch_id: string;
  client_action_id: string;
}

function pendingPreviewKey(decisionSpaceId: string) {
  return `zdecision:preview:${decisionSpaceId}`;
}

function readPendingPreview(decisionSpaceId: string): PendingPreviewAction | null {
  try {
    const raw = localStorage.getItem(pendingPreviewKey(decisionSpaceId));
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (
      typeof value !== "object" || value === null ||
      !("review_batch_id" in value) || !("client_action_id" in value) ||
      typeof value.review_batch_id !== "string" ||
      typeof value.client_action_id !== "string" ||
      !/^rvb_[0-9a-f]{32}$/.test(value.review_batch_id) ||
      !/^web_action_[A-Za-z0-9-]{1,96}$/.test(value.client_action_id)
    ) return null;
    return {
      review_batch_id: value.review_batch_id,
      client_action_id: value.client_action_id,
    };
  } catch {
    return null;
  }
}


export function CandidateReviewPage() {
  const { decisionSpaceId = "" } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const routedRepository = searchParams.get("repository_id") ?? "";
  const captureRequestId = searchParams.get("capture_request_id") ?? "";
  const routedSearch = searchParams.get("search") ?? "";
  const requestedState = searchParams.get("state") ?? "pending";
  const routedState: CandidateStateFilter = candidateStates.has(
    requestedState as CandidateStateFilter,
  )
    ? (requestedState as CandidateStateFilter)
    : "pending";
  const [inbox, setInbox] = useState<CandidateInbox | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [selectedRepository, setSelectedRepository] = useState(routedRepository);
  const [filterSearch, setFilterSearch] = useState(routedSearch);
  const [filterRepository, setFilterRepository] = useState(routedRepository);
  const [filterCaptureRequest, setFilterCaptureRequest] = useState(
    captureRequestId,
  );
  const [filterState, setFilterState] = useState<CandidateStateFilter>(
    routedState,
  );
  const [draftVersion, setDraftVersion] = useState(0);
  const [draftByFamily, setDraftByFamily] = useState(
    () => new Map<string, ReviewDraftItem>(),
  );
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [savedDraftSignature, setSavedDraftSignature] = useState("[]");
  const [submitting, setSubmitting] = useState(false);
  const [pendingPreview, setPendingPreview] = useState<PendingPreviewAction | null>(
    () => readPendingPreview(decisionSpaceId),
  );
  const [staleFamilies, setStaleFamilies] = useState(
    () => new Set<string>(),
  );
  const [selectedFamilyIds, setSelectedFamilyIds] = useState(
    () => new Set<string>(),
  );
  const [lastBatchUndo, setLastBatchUndo] = useState<BatchUndo | null>(null);
  const loadedDecisionSpace = useRef<string | null>(null);
  const selectPageCheckbox = useRef<HTMLInputElement | null>(null);

  const candidatePath = useMemo(() => {
    const query = new URLSearchParams();
    query.set("search", routedSearch);
    if (routedRepository) query.set("repository_id", routedRepository);
    if (captureRequestId) query.set("capture_request_id", captureRequestId);
    query.set("state", routedState);
    const suffix = query.size ? `?${query.toString()}` : "";
    return `/api/v1/web/spaces/${decisionSpaceId}/candidates${suffix}`;
  }, [captureRequestId, decisionSpaceId, routedRepository, routedSearch, routedState]);

  useEffect(() => {
    let active = true;
    setInbox(null);
    setLoadFailed(false);
    api<CandidateInbox>(candidatePath)
      .then((value) => {
        if (!active) return;
        const sameDecisionSpace = loadedDecisionSpace.current === decisionSpaceId;
        loadedDecisionSpace.current = decisionSpaceId;
        setInbox(value);
        setDraftVersion(value.draft.version);
        setDraftByFamily((current) => {
          const replacement = new Map(
            value.draft.items.map((item) => [item.family_id, item]),
          );
          if (sameDecisionSpace) {
            for (const [familyId, action] of current) {
              replacement.set(familyId, action);
            }
          }
          return replacement;
        });
        setSavedDraftSignature(JSON.stringify(value.draft.items));
        setStaleFamilies(new Set());
        setSelectedRepository(
          routedRepository || value.repositories[0]?.repository_id || "",
        );
      })
      .catch(() => {
        if (active) setLoadFailed(true);
      });
    return () => {
      active = false;
    };
  }, [candidatePath, reload, routedRepository]);

  useEffect(() => {
    setSelectedFamilyIds(new Set());
  }, [
    decisionSpaceId,
    routedRepository,
    routedSearch,
    captureRequestId,
    routedState,
  ]);

  useEffect(() => {
    setFilterSearch(routedSearch);
    setFilterRepository(routedRepository);
    setFilterCaptureRequest(captureRequestId);
    setFilterState(routedState);
  }, [captureRequestId, routedRepository, routedSearch, routedState]);

  useEffect(() => {
    setPendingPreview(readPendingPreview(decisionSpaceId));
  }, [decisionSpaceId]);

  const refreshCompleted = useCallback(() => {
    setReload((value) => value + 1);
  }, []);
  const capture = useCandidateRefresh(selectedRepository, refreshCompleted);

  const draftItems = useMemo(
    () => Array.from(draftByFamily.values()),
    [draftByFamily],
  );
  const orderedClassifiedItems = useMemo(
    () =>
      inbox?.items
        .filter(
          (item) => !item.stale_draft && !staleFamilies.has(item.family_id),
        )
        .map((item) => {
          const action = draftByFamily.get(item.family_id);
          if (!isClassified(action)) return undefined;
          if (
            action?.repository_id !== item.repository_id ||
            action.revision_id !== item.revision_id ||
            action.revision !== item.revision ||
            action.content_digest !== item.content_digest
          ) return undefined;
          return action;
        })
        .filter(
          (item): item is ReviewDraftItem & { action: ClassifiedReviewAction } =>
            item !== undefined,
        ) ?? [],
    [draftByFamily, inbox, staleFamilies],
  );
  const classifiedCount = useMemo(
    () => draftItems.filter(isClassified).length,
    [draftItems],
  );
  const selectedNewClassifications = useMemo(
    () => Array.from(selectedFamilyIds).filter(
      (familyId) => !isClassified(draftByFamily.get(familyId)),
    ).length,
    [draftByFamily, selectedFamilyIds],
  );
  const batchWouldExceedLimit =
    classifiedCount + selectedNewClassifications > MAX_REVIEW_ITEMS;
  const classificationLimitReached = classifiedCount >= MAX_REVIEW_ITEMS;
  const allVisibleSelected = Boolean(inbox?.items.length) &&
    inbox!.items.every((item) => selectedFamilyIds.has(item.family_id));
  const someVisibleSelected = inbox?.items.some(
    (item) => selectedFamilyIds.has(item.family_id),
  ) ?? false;

  useEffect(() => {
    if (selectPageCheckbox.current) {
      selectPageCheckbox.current.indeterminate =
        someVisibleSelected && !allVisibleSelected;
    }
  }, [allVisibleSelected, someVisibleSelected]);

  const reviewCounts = useMemo(() => {
    const counts = { accepted: 0, rejected: 0, unprocessed: 0, stale: 0 };
    for (const item of inbox?.items ?? []) {
      const action = draftByFamily.get(item.family_id);
      if (item.stale_draft || staleFamilies.has(item.family_id)) counts.stale += 1;
      if (action?.action === "accept" || action?.action === "edit_accept") {
        counts.accepted += 1;
      } else if (action?.action === "reject") {
        counts.rejected += 1;
      } else {
        counts.unprocessed += 1;
      }
    }
    return counts;
  }, [draftByFamily, inbox, staleFamilies]);

  function updateLocalDraft(
    familyId: string,
    value: ReviewDraftItem | undefined,
  ) {
    setSaveMessage(null);
    setDraftByFamily((current) => {
      const replacement = new Map(current);
      if (value) replacement.set(familyId, value);
      else replacement.delete(familyId);
      return replacement;
    });
  }

  function candidateByFamily(familyId: string): CandidateInboxItem | undefined {
    return inbox?.items.find((item) => item.family_id === familyId);
  }

  function setSelected(familyId: string, selected: boolean) {
    setSelectedFamilyIds((current) => {
      const replacement = new Set(current);
      if (selected) replacement.add(familyId);
      else replacement.delete(familyId);
      return replacement;
    });
  }

  function selectCurrentPage(selected: boolean) {
    setSelectedFamilyIds((current) => {
      const replacement = new Set(current);
      for (const item of inbox?.items ?? []) {
        if (selected) replacement.add(item.family_id);
        else replacement.delete(item.family_id);
      }
      return replacement;
    });
  }

  function directAction(familyId: string, action: "accept" | "reject") {
    const item = candidateByFamily(familyId);
    if (!item) return;
    if (!isClassified(draftByFamily.get(familyId)) && classificationLimitReached) {
      return;
    }
    updateLocalDraft(familyId, actionForCandidate(item, action));
  }

  function editAccept(familyId: string, content: CandidateContent) {
    const item = candidateByFamily(familyId);
    if (!item) return;
    if (!isClassified(draftByFamily.get(familyId)) && classificationLimitReached) {
      return;
    }
    updateLocalDraft(
      familyId,
      actionForCandidate(item, "edit_accept", content),
    );
  }

  function applyBatch(action: "accept" | "reject") {
    if (selectedFamilyIds.size === 0 || batchWouldExceedLimit) return;
    const previousByFamily = new Map<string, ReviewDraftItem | undefined>();
    const replacement = new Map(draftByFamily);
    for (const familyId of selectedFamilyIds) {
      const item = candidateByFamily(familyId);
      if (!item) continue;
      previousByFamily.set(familyId, draftByFamily.get(familyId));
      replacement.set(familyId, actionForCandidate(item, action));
    }
    setDraftByFamily(replacement);
    setSaveMessage(null);
    setLastBatchUndo({
      message: `已将 ${selectedFamilyIds.size} 条标记为${
        action === "accept" ? "接受" : "拒绝"
      }`,
      previousByFamily,
    });
    setSelectedFamilyIds(new Set());
  }

  function undoBatch() {
    if (!lastBatchUndo) return;
    setDraftByFamily((current) => {
      const replacement = new Map(current);
      for (const [familyId, previous] of lastBatchUndo.previousByFamily) {
        if (previous) replacement.set(familyId, previous);
        else replacement.delete(familyId);
      }
      return replacement;
    });
    setSaveMessage("已撤销上一次批量操作");
    setLastBatchUndo(null);
  }

  async function saveDraft() {
    setSaveMessage(null);
    try {
      const saved = await api<ReviewDraft>(
        `/api/v1/web/spaces/${decisionSpaceId}/review-draft`,
        {
          method: "PUT",
          body: JSON.stringify({
            expected_version: draftVersion,
            items: draftItems,
          }),
        },
      );
      setDraftVersion(saved.version);
      setDraftByFamily(
        new Map(saved.items.map((item) => [item.family_id, item])),
      );
      setSavedDraftSignature(JSON.stringify(saved.items));
      setSaveMessage("审核草稿已保存");
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === "review_draft_conflict"
      ) {
        setSaveMessage("审核草稿已在其他页面更新");
      } else {
        setSaveMessage("审核草稿保存失败");
      }
    }
  }

  async function submitReview() {
    if (
      orderedClassifiedItems.length === 0 ||
      orderedClassifiedItems.length > MAX_REVIEW_ITEMS ||
      submitting
    ) return;
    setSaveMessage(null);
    setSubmitting(true);
    try {
      let version = draftVersion;
      let submittedItems: ReviewDraftItem[] = orderedClassifiedItems;
      if (JSON.stringify(draftItems) !== savedDraftSignature) {
        const saved = await api<ReviewDraft>(
          `/api/v1/web/spaces/${decisionSpaceId}/review-draft`,
          {
            method: "PUT",
            body: JSON.stringify({
              expected_version: draftVersion,
              items: draftItems,
            }),
          },
        );
        version = saved.version;
        setDraftVersion(saved.version);
        setSavedDraftSignature(JSON.stringify(saved.items));
        const savedByFamily = new Map(
          saved.items.map((item) => [item.family_id, item]),
        );
        submittedItems = orderedClassifiedItems
          .map((item) => savedByFamily.get(item.family_id))
          .filter(
            (item): item is ReviewDraftItem & { action: ClassifiedReviewAction } =>
              isClassified(item),
          );
      }
      const result = await api<ReviewSubmissionResult>(
        `/api/v1/web/spaces/${decisionSpaceId}/reviews`,
        {
          method: "POST",
          body: JSON.stringify({
            client_action_id: `web_action_${crypto.randomUUID()}`,
            expected_draft_version: version,
            items: submittedItems,
          }),
        },
      );
      const submittedFamilies = new Set(
        submittedItems.map((item) => item.family_id),
      );
      const remaining = draftItems.filter(
        (item) => !submittedFamilies.has(item.family_id),
      );
      setDraftVersion(result.draft_version);
      setDraftByFamily(
        new Map(remaining.map((item) => [item.family_id, item])),
      );
      setSavedDraftSignature(JSON.stringify(remaining));
      setStaleFamilies(new Set());
      if (result.preview_eligible) {
        const action = {
          review_batch_id: result.review_batch_id,
          client_action_id: `web_action_${crypto.randomUUID()}`,
        };
        localStorage.setItem(pendingPreviewKey(decisionSpaceId), JSON.stringify(action));
        setPendingPreview(action);
        setSaveMessage("审核已提交，可单独生成发布预览");
      } else {
        setSaveMessage("审核结果已提交");
      }
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === "review_stale"
      ) {
        setStaleFamilies(new Set(staleFamilyIds(error)));
        setSaveMessage("候选版本已更新，请核对后载入最新版本");
      } else if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === "review_draft_conflict"
      ) {
        setSaveMessage("审核草稿已在其他页面更新");
      } else {
        setSaveMessage("审核提交失败");
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function openPendingPreview(action: PendingPreviewAction) {
    try {
      const preview = await api<{ preview_id: string }>(
        `/api/v1/web/reviews/${action.review_batch_id}/previews`,
        {
          method: "POST",
          body: JSON.stringify({ client_action_id: action.client_action_id }),
        },
      );
      localStorage.removeItem(pendingPreviewKey(decisionSpaceId));
      setPendingPreview(null);
      void navigate(`/publication-previews/${preview.preview_id}`);
    } catch {
      setSaveMessage("审核已提交，但发布预览生成失败");
    }
  }

  async function retryPendingPreview() {
    if (!pendingPreview || submitting) return;
    setSubmitting(true);
    setSaveMessage(null);
    try {
      await openPendingPreview(pendingPreview);
    } finally {
      setSubmitting(false);
    }
  }

  async function loadLatestVersions() {
    setSaveMessage(null);
    try {
      const latest = await api<CandidateInbox>(candidatePath);
      const latestByFamily = new Map(
        latest.items.map((item) => [item.family_id, item]),
      );
      setInbox({
        ...latest,
        items: latest.items.map((item) =>
          staleFamilies.has(item.family_id)
            ? { ...item, stale_draft: false }
            : item,
        ),
      });
      setDraftByFamily((current) => {
        const replacement = new Map(
          latest.draft.items.map((item) => [item.family_id, item]),
        );
        for (const [familyId, action] of current) {
          const candidate = latestByFamily.get(familyId);
          if (!staleFamilies.has(familyId) || !candidate) {
            replacement.set(familyId, action);
            continue;
          }
          replacement.set(familyId, {
            ...action,
            repository_id: candidate.repository_id,
            revision_id: candidate.revision_id,
            revision: candidate.revision,
            content_digest: candidate.content_digest,
            effective_content:
              action.action === "edit_accept"
                ? {
                    ...(action.effective_content ?? candidate.content),
                    product: candidate.content.product,
                    repositories: candidate.content.repositories,
                  }
                : null,
          });
        }
        return replacement;
      });
      setDraftVersion(latest.draft.version);
      setSavedDraftSignature(JSON.stringify(latest.draft.items));
      setStaleFamilies(new Set());
      setSaveMessage("已载入最新候选版本，请重新提交审核");
    } catch {
      setSaveMessage("最新候选版本载入失败");
    }
  }

  function applyFilters(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = new URLSearchParams();
    if (filterSearch) next.set("search", filterSearch);
    if (filterRepository) next.set("repository_id", filterRepository);
    if (filterCaptureRequest) {
      next.set("capture_request_id", filterCaptureRequest);
    }
    next.set("state", filterState);
    setSearchParams(next);
  }

  if (loadFailed) {
    return (
      <AsyncState
        kind="error"
        title="候选决策暂时不可用"
        detail="产品或仓库未登记，或中央服务无法读取候选决策。"
      />
    );
  }
  if (!inbox) {
    return <AsyncState kind="loading" title="正在读取候选决策" />;
  }

  return (
    <div className="page candidate-page">
      <header className="page-header candidate-page__header">
        <div>
          <p className="eyebrow">
            {inbox.space.kind === "product" ? "PRODUCT" : "SHARED UNIT"} / CANDIDATE INBOX
          </p>
          <h1>{inbox.space.display_name}</h1>
          <DecisionSpaceContext space={inbox.space} />
          <p className="page-header__lead">
            审阅当前候选版本并保存私人草稿。保存不会提交审核或生成发布内容。
          </p>
        </div>
        <div className="refresh-console">
          <label>
            <span>登记仓库</span>
            <select
              aria-label="登记仓库"
              value={selectedRepository}
              onChange={(event) => setSelectedRepository(event.target.value)}
            >
              <option value="">请选择仓库</option>
              {inbox.repositories.map((repository) => (
                <option
                  value={repository.repository_id}
                  key={repository.repository_id}
                >
                  {repository.repository_id}
                </option>
              ))}
            </select>
          </label>
          <button
            className="primary-button"
            type="button"
            disabled={!selectedRepository || capture.running}
            onClick={() => void capture.refresh()}
          >
            更新候选决策
          </button>
          {capture.message ? (
            <p className={capture.failed ? "capture-status capture-status--failed" : "capture-status"}>
              {capture.message}
            </p>
          ) : null}
        </div>
      </header>

      <form className="candidate-filters" onSubmit={applyFilters}>
        <label className="candidate-filters__search">
          <span>搜索候选决策</span>
          <input
            type="search"
            aria-label="搜索候选决策"
            maxLength={200}
            value={filterSearch}
            onChange={(event) => setFilterSearch(event.target.value)}
          />
        </label>
        <label>
          <span>筛选仓库</span>
          <select
            aria-label="筛选仓库"
            value={filterRepository}
            onChange={(event) => setFilterRepository(event.target.value)}
          >
            <option value="">全部仓库</option>
            {inbox.repositories.map((repository) => (
              <option
                value={repository.repository_id}
                key={repository.repository_id}
              >
                {repository.repository_id}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Capture Request ID</span>
          <input
            aria-label="Capture Request ID"
            value={filterCaptureRequest}
            onChange={(event) => setFilterCaptureRequest(event.target.value)}
          />
        </label>
        <label>
          <span>审核状态</span>
          <select
            aria-label="审核状态"
            value={filterState}
            onChange={(event) =>
              setFilterState(event.target.value as CandidateStateFilter)
            }
          >
            <option value="pending">待审核</option>
            <option value="accepted">已接受</option>
            <option value="rejected">已拒绝</option>
            <option value="published">已发布</option>
            <option value="all">全部</option>
          </select>
        </label>
        <button className="filter-button" type="submit">应用筛选</button>
      </form>

      <div className="candidate-review-console">
        <div className="candidate-summary" aria-label="审核汇总">
          <span><strong>{reviewCounts.accepted}</strong> 已接受</span>
          <span><strong>{reviewCounts.rejected}</strong> 已拒绝</span>
          <span><strong>{reviewCounts.unprocessed}</strong> 未处理</span>
          <span><strong>{reviewCounts.stale}</strong> 已过期</span>
        </div>

        <div className="candidate-batch-toolbar" aria-label="批量审核工具栏">
          <label className="candidate-batch-toolbar__select">
            <input
              ref={selectPageCheckbox}
              type="checkbox"
              aria-label={`选择当前页 ${inbox.items.length} 条`}
              checked={allVisibleSelected}
              onChange={(event) => selectCurrentPage(event.target.checked)}
            />
            <span>当前页 {inbox.items.length} 条</span>
          </label>
          <span className="candidate-batch-toolbar__selected">
            已选 {selectedFamilyIds.size} 条
          </span>
          <button
            type="button"
            disabled={selectedFamilyIds.size === 0 || batchWouldExceedLimit}
            onClick={() => applyBatch("accept")}
          >
            批量接受
          </button>
          <button
            type="button"
            disabled={selectedFamilyIds.size === 0 || batchWouldExceedLimit}
            onClick={() => applyBatch("reject")}
          >
            批量拒绝
          </button>
          {lastBatchUndo ? (
            <span className="candidate-batch-toolbar__undo" role="status">
              {lastBatchUndo.message}
              <button type="button" onClick={undoBatch}>撤销</button>
            </span>
          ) : null}
          {batchWouldExceedLimit || classificationLimitReached ? (
            <span className="candidate-batch-toolbar__limit" role="status">
              单次最多审核 20 条
            </span>
          ) : null}
        </div>

        <div className="candidate-toolbar">
          <div className="candidate-toolbar__save">
          {saveMessage ? <span role="status">{saveMessage}</span> : null}
          <button
            className="quiet-button"
            type="button"
            onClick={() => void saveDraft()}
          >
            保存审核草稿
          </button>
          {pendingPreview ? (
            <button
              className="quiet-button"
              type="button"
              disabled={submitting}
              onClick={() => void retryPendingPreview()}
            >
              生成发布预览
            </button>
          ) : null}
          <button
            className="primary-button"
            type="button"
            disabled={
              orderedClassifiedItems.length === 0 ||
              orderedClassifiedItems.length > MAX_REVIEW_ITEMS ||
              submitting
            }
            onClick={() => void submitReview()}
          >
            提交审核
          </button>
          </div>
        </div>
      </div>

      {inbox.items.length === 0 ? (
        <AsyncState
          kind="empty"
          title={
            capture.message === "更新完成，未发现新候选决策"
              ? "本次更新未发现候选决策"
              : "当前筛选下没有候选决策"
          }
        />
      ) : (
        <section className="candidate-stack" aria-label="候选决策列表">
          {inbox.items.map((item) => (
            <CandidateReviewRow
              item={item}
              space={inbox.space}
              action={draftByFamily.get(item.family_id)}
              selected={selectedFamilyIds.has(item.family_id)}
              stale={item.stale_draft || staleFamilies.has(item.family_id)}
              classificationDisabled={
                !isClassified(draftByFamily.get(item.family_id)) &&
                classificationLimitReached
              }
              onSelectedChange={setSelected}
              onDirectAction={directAction}
              onEditAccept={editAccept}
              onLoadLatest={
                item.stale_draft || staleFamilies.has(item.family_id)
                  ? () => void loadLatestVersions()
                  : undefined
              }
              key={`${item.family_id}:${item.revision_id}`}
            />
          ))}
        </section>
      )}
    </div>
  );
}
