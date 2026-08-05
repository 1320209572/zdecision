import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api/client";
import type { Dashboard } from "../../api/types";
import { DecisionSpaceTree } from "../../features/decision-spaces/DecisionSpaceTree";
import { AsyncState } from "../../shared/AsyncState";
import { StatusBadge } from "../../shared/StatusBadge";

function formatDate(value: string | null): string {
  if (!value) return "尚无活动";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function CompanyOverviewPage() {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    api<Dashboard>("/api/v1/web/dashboard")
      .then((value) => {
        if (active) setDashboard(value);
      })
      .catch(() => {
        if (active) setFailed(true);
      });
    return () => {
      active = false;
    };
  }, []);

  if (failed) {
    return (
      <AsyncState
        kind="error"
        title="总览暂时不可用"
        detail="中央服务未能返回公司决策概况。"
      />
    );
  }
  if (!dashboard) {
    return <AsyncState kind="loading" title="正在汇总公司决策数据" />;
  }

  const metrics = [
    ["已启用产品", dashboard.metrics.product_count, "PRODUCTS"],
    ["待审核候选", dashboard.metrics.pending_candidate_count, "PENDING"],
    [
      "生效中决策",
      dashboard.metrics.active_decision_count ?? "—",
      "ACTIVE",
    ],
    ["本周已发布", dashboard.metrics.completed_this_week, "THIS WEEK"],
  ] as const;

  return (
    <div className="page overview-page">
      <header className="page-header">
        <div>
          <p className="eyebrow">COMPANY / DECISION CONTROL</p>
          <h1>公司决策总览</h1>
          <p className="page-header__lead">
            从已登记仓库汇总候选审核、正式决策与发布状态。
          </p>
        </div>
        <StatusBadge
          tone={dashboard.registry.state === "available" ? "success" : "danger"}
        >
          Registry {dashboard.registry.state === "available" ? "已同步" : "不可用"}
        </StatusBadge>
      </header>

      <section className="metric-grid" aria-label="公司决策指标">
        {metrics.map(([label, value, caption], index) => (
          <article className="metric-card" key={label} style={{ "--delay": `${index * 55}ms` } as React.CSSProperties}>
            <span className="metric-card__caption">{caption}</span>
            <strong>{value}</strong>
            <span>{label}</span>
          </article>
        ))}
      </section>

      <section className="section-block">
        <div className="section-heading">
          <div>
            <p className="eyebrow">REGISTERED PRODUCTS</p>
            <h2>产品工作区</h2>
          </div>
          <span className="section-heading__count">{dashboard.products.length} 个产品</span>
        </div>
        {dashboard.products.length === 0 ? (
          <AsyncState kind="empty" title="尚未登记产品" />
        ) : (
          <div className="product-list">
            {dashboard.products.map((product, index) => (
              <article
                className="product-row"
                key={product.decision_space_id}
                style={{ "--delay": `${220 + index * 55}ms` } as React.CSSProperties}
              >
                <span className="product-row__index">{String(index + 1).padStart(2, "0")}</span>
                <span className="product-row__identity">
                  <strong>{product.display_name}</strong>
                  <small>{product.repository_ids.length} 个已启用仓库</small>
                </span>
                <span className="product-row__stat"><strong>{product.pending_candidate_count}</strong><small>待审核</small></span>
                <span className="product-row__stat"><strong>{product.active_decision_count ?? "—"}</strong><small>生效决策</small></span>
                <span className="product-row__activity">
                  {formatDate(product.last_activity_at)}
                </span>
                <span className="product-row__actions">
                  <Link
                    aria-label={`候选 ${product.pending_candidate_count}`}
                    to={`/spaces/${product.decision_space_id}/candidates`}
                  >候选</Link>
                  <Link
                    aria-label={`决策 ${product.active_decision_count ?? "未知"}`}
                    to={`/spaces/${product.decision_space_id}/decisions`}
                  >决策</Link>
                  <Link to={`/spaces/${product.decision_space_id}/publications`}>发布</Link>
                </span>
              </article>
            ))}
          </div>
        )}
      </section>

      {dashboard.shared_tree ? (
        <section className="section-block shared-catalog">
          <div className="section-heading">
            <div>
              <p className="eyebrow">SHARED / SOURCE-ROOT CATALOG</p>
              <h2>Shared</h2>
            </div>
            <span className="section-heading__count">按真实目录边界</span>
          </div>
          <DecisionSpaceTree root={dashboard.shared_tree} />
        </section>
      ) : null}

      <section className="section-block section-block--compact">
        <div className="section-heading">
          <div>
            <p className="eyebrow">RECENT PUBLICATIONS</p>
            <h2>最近发布</h2>
          </div>
        </div>
        {dashboard.recent_publications.length === 0 ? (
          <p className="quiet-panel">还没有发布记录。完成审核后，发布批次会出现在这里。</p>
        ) : (
          <div className="publication-feed">
            {dashboard.recent_publications.map((publication) => (
              <Link to={`/publications/${publication.publication_id}`} key={publication.publication_id}>
                <strong>{publication.product_name}</strong>
                <span>{publication.decision_count} 条决策</span>
                <StatusBadge tone={publication.state === "completed" ? "success" : "warning"}>
                  {publication.state}
                </StatusBadge>
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
