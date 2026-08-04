import { useCallback, useEffect, useMemo, useState } from "react";
import {
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";

import { api, ApiError } from "../../api/client";
import type {
  CandidateInbox,
  ReviewDraft,
  ReviewDraftItem,
  RepositoryView,
} from "../../api/types";
import { useCandidateRefresh } from "../../features/candidate-refresh/useCandidateRefresh";
import { ReviewEditor } from "../../features/reviews/ReviewEditor";
import { AsyncState } from "../../shared/AsyncState";
import { CompanyOverviewPage } from "../company-overview/CompanyOverviewPage";


export function RepositoryEntryPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const repositoryId = new URLSearchParams(location.search).get("repository_id");
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    if (!repositoryId) return;
    let active = true;
    api<{ repositories: RepositoryView[] }>("/api/v1/repositories")
      .then((result) => {
        if (!active) return;
        const repository = result.repositories.find(
          (item) => item.repository_id === repositoryId && item.enabled,
        );
        if (!repository) {
          setUnavailable(true);
          return;
        }
        void navigate(
          `/products/${repository.product_id}/candidates?repository_id=${repository.repository_id}`,
          { replace: true },
        );
      })
      .catch(() => {
        if (active) setUnavailable(true);
      });
    return () => {
      active = false;
    };
  }, [navigate, repositoryId]);

  if (!repositoryId) return <CompanyOverviewPage />;
  if (unavailable) {
    return <AsyncState kind="error" title="仓库未登记或未启用" />;
  }
  return <AsyncState kind="loading" title="正在查找仓库对应产品" />;
}


export function CandidateReviewPage() {
  const { productId = "" } = useParams();
  const [searchParams] = useSearchParams();
  const routedRepository = searchParams.get("repository_id") ?? "";
  const captureRequestId = searchParams.get("capture_request_id");
  const [inbox, setInbox] = useState<CandidateInbox | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [selectedRepository, setSelectedRepository] = useState(routedRepository);
  const [draftVersion, setDraftVersion] = useState(0);
  const [draftByFamily, setDraftByFamily] = useState(
    () => new Map<string, ReviewDraftItem>(),
  );
  const [saveMessage, setSaveMessage] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setInbox(null);
    setLoadFailed(false);
    const query = new URLSearchParams();
    if (routedRepository) query.set("repository_id", routedRepository);
    if (captureRequestId) query.set("capture_request_id", captureRequestId);
    const suffix = query.size ? `?${query.toString()}` : "";
    api<CandidateInbox>(
      `/api/v1/web/products/${productId}/candidates${suffix}`,
    )
      .then((value) => {
        if (!active) return;
        setInbox(value);
        setDraftVersion(value.draft.version);
        setDraftByFamily(
          new Map(value.draft.items.map((item) => [item.family_id, item])),
        );
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
  }, [captureRequestId, productId, reload, routedRepository]);

  const refreshCompleted = useCallback(() => {
    setReload((value) => value + 1);
  }, []);
  const capture = useCandidateRefresh(selectedRepository, refreshCompleted);

  const draftItems = useMemo(
    () => Array.from(draftByFamily.values()),
    [draftByFamily],
  );

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

  async function saveDraft() {
    setSaveMessage(null);
    try {
      const saved = await api<ReviewDraft>(
        `/api/v1/web/products/${productId}/review-draft`,
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
          <p className="eyebrow">PRODUCT / CANDIDATE INBOX</p>
          <h1>{inbox.product_name}</h1>
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

      <div className="candidate-toolbar">
        <div>
          <span className="candidate-toolbar__count">{inbox.items.length}</span>
          <span>条当前候选</span>
        </div>
        <div className="candidate-toolbar__save">
          {saveMessage ? <span role="status">{saveMessage}</span> : null}
          <button
            className="quiet-button"
            type="button"
            onClick={() => void saveDraft()}
          >
            保存审核草稿
          </button>
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
            <ReviewEditor
              item={item}
              action={draftByFamily.get(item.family_id)}
              onChange={(value) => updateLocalDraft(item.family_id, value)}
              key={`${item.family_id}:${item.revision_id}`}
            />
          ))}
        </section>
      )}
    </div>
  );
}
