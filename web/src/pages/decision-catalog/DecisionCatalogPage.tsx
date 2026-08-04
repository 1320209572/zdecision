import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { api, ApiError } from "../../api/client";
import type { DecisionListView } from "../../api/types";
import { AsyncState } from "../../shared/AsyncState";
import { StatusBadge } from "../../shared/StatusBadge";


function formatPublished(value: string | null): string {
  if (!value) return "发布凭据未联结";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function DecisionCatalogPage() {
  const { productId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const routedSearch = searchParams.get("search") ?? "";
  const routedRepository = searchParams.get("repository") ?? "";
  const routedPublishedAfter = searchParams.get("published_after") ?? "";
  const [keyword, setKeyword] = useState(routedSearch);
  const [repository, setRepository] = useState(routedRepository);
  const [publishedAfter, setPublishedAfter] = useState(routedPublishedAfter);
  const [view, setView] = useState<DecisionListView | null>(null);
  const [failure, setFailure] = useState<"registry" | "request" | null>(null);

  const requestPath = useMemo(() => {
    const query = new URLSearchParams();
    if (productId) query.set("product_id", productId);
    if (routedSearch) query.set("search", routedSearch);
    if (routedRepository) query.set("repository", routedRepository);
    if (routedPublishedAfter) {
      query.set("published_after", routedPublishedAfter);
    }
    const suffix = query.size ? `?${query.toString()}` : "";
    return `/api/v1/web/decisions${suffix}`;
  }, [productId, routedPublishedAfter, routedRepository, routedSearch]);

  useEffect(() => {
    let active = true;
    setView(null);
    setFailure(null);
    api<DecisionListView>(requestPath)
      .then((value) => {
        if (!active) return;
        if (value.registry_state === "unavailable" || value.items === null) {
          setFailure("registry");
          return;
        }
        setView(value);
      })
      .catch((error: unknown) => {
        if (!active) return;
        setFailure(
          error instanceof ApiError && error.code === "registry_unavailable"
            ? "registry"
            : "request",
        );
      });
    return () => { active = false; };
  }, [requestPath]);

  useEffect(() => {
    setKeyword(routedSearch);
    setRepository(routedRepository);
    setPublishedAfter(routedPublishedAfter);
  }, [routedPublishedAfter, routedRepository, routedSearch]);

  function filter(event: FormEvent) {
    event.preventDefault();
    const next = new URLSearchParams();
    if (keyword) next.set("search", keyword);
    if (repository) next.set("repository", repository);
    if (publishedAfter) next.set("published_after", publishedAfter);
    setSearchParams(next);
  }

  if (failure === "registry") {
    return (
      <AsyncState
        kind="error"
        title="正式决策仓库暂不可用"
        detail="无法证明当前 Registry 提交；目录不会以空结果替代不可用状态。"
      />
    );
  }
  if (failure === "request") {
    return <AsyncState kind="error" title="正式决策目录加载失败" />;
  }
  if (!view) return <AsyncState kind="loading" title="正在验证 Registry 快照" />;

  const items = view.items ?? [];
  const productName = productId && items.length ? items[0].product_name : null;
  return (
    <div className="page decision-catalog">
      <header className="page-header decision-catalog__header">
        <div>
          <p className="eyebrow">FORMAL / COMMIT-BOUND REGISTRY</p>
          <h1>{productName ?? (productId ? "产品正式决策" : "正式决策目录")}</h1>
          <p className="page-header__lead">
            仅显示当前 Registry 提交中的 active V1 正式决策；产品归属由服务端登记关系确定。
          </p>
        </div>
        <div className="decision-catalog__proof">
          <StatusBadge tone="success">Registry 已验证</StatusBadge>
          <code>{view.registry_commit}</code>
        </div>
      </header>

      <form className="decision-filters" onSubmit={filter}>
        <label>
          <span>关键词 / KEYWORD</span>
          <input value={keyword} maxLength={200} onChange={(event) => setKeyword(event.target.value)} />
        </label>
        <label>
          <span>仓库 / REPOSITORY</span>
          <input value={repository} maxLength={200} onChange={(event) => setRepository(event.target.value)} />
        </label>
        <label>
          <span>发布于此后 / RFC 3339</span>
          <input
            value={publishedAfter}
            placeholder="2026-08-04T00:00:00Z"
            onChange={(event) => setPublishedAfter(event.target.value)}
          />
        </label>
        <button className="filter-button" type="submit">筛选目录</button>
      </form>

      <div className="decision-catalog__summary">
        <strong>{view.total ?? 0}</strong>
        <span>ACTIVE FORMAL DECISIONS</span>
      </div>

      {items.length === 0 ? (
        <AsyncState kind="empty" title="暂无正式决策" />
      ) : (
        <section className="decision-list" aria-label="正式决策列表">
          {items.map((item, index) => (
            <article className="decision-row" key={`${item.product_id}:${item.decision_id}`}>
              <span className="decision-row__number">{String(index + 1).padStart(2, "0")}</span>
              <div className="decision-row__body">
                <div className="decision-row__ownership">
                  <strong>{item.product_name}</strong>
                  <code>{item.product_id}</code>
                  <StatusBadge tone="success">R{item.revision} · {item.lifecycle}</StatusBadge>
                </div>
                <h2>{item.claim}</h2>
                <p>{item.future_action}</p>
                <dl>
                  <div><dt>范围</dt><dd>{item.scope_summary}</dd></div>
                  <div><dt>仓库</dt><dd>{item.repositories.join(" · ") || "—"}</dd></div>
                  <div><dt>路径</dt><dd>{item.paths.join(" · ") || "—"}</dd></div>
                  <div><dt>发布时间</dt><dd>{formatPublished(item.published_at)}</dd></div>
                </dl>
              </div>
              <Link
                className="decision-row__open"
                to={`/products/${item.product_id}/decisions/${item.decision_id}`}
              >
                查看决策
              </Link>
            </article>
          ))}
        </section>
      )}
    </div>
  );
}
