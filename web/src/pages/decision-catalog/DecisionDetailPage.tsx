import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, ApiError } from "../../api/client";
import type { DecisionDetail } from "../../api/types";
import { AsyncState } from "../../shared/AsyncState";
import { StatusBadge } from "../../shared/StatusBadge";


function TextList({ values }: { values: string[] }) {
  if (!values.length) return <span>无</span>;
  return <ul>{values.map((value) => <li key={value}>{value}</li>)}</ul>;
}

export function DecisionDetailPage() {
  const { productId = "", decisionId = "" } = useParams();
  const [decision, setDecision] = useState<DecisionDetail | null>(null);
  const [failure, setFailure] = useState<"registry" | "request" | null>(null);

  useEffect(() => {
    let active = true;
    setDecision(null);
    setFailure(null);
    api<DecisionDetail>(
      `/api/v1/web/products/${productId}/decisions/${decisionId}`,
    )
      .then((value) => { if (active) setDecision(value); })
      .catch((error: unknown) => {
        if (!active) return;
        setFailure(
          error instanceof ApiError && error.code === "registry_unavailable"
            ? "registry"
            : "request",
        );
      });
    return () => { active = false; };
  }, [decisionId, productId]);

  if (failure === "registry") {
    return <AsyncState kind="error" title="正式决策仓库暂不可用" />;
  }
  if (failure === "request") {
    return <AsyncState kind="error" title="正式决策不存在或不可访问" />;
  }
  if (!decision) return <AsyncState kind="loading" title="正在读取正式决策" />;

  return (
    <div className="page decision-detail">
      <header className="page-header decision-detail__header">
        <div>
          <p className="eyebrow">FORMAL DECISION / READ ONLY</p>
          <h1>{decision.product_name}</h1>
          <p className="page-header__lead">完整 V1 正式文档，由 Registry 提交与中央发布凭据共同定位。</p>
        </div>
        <StatusBadge tone="success">R{decision.revision} · {decision.lifecycle}</StatusBadge>
      </header>

      <nav className="decision-detail__nav" aria-label="决策上下文">
        <Link to={`/products/${decision.product_id}/decisions`}>← 返回产品决策目录</Link>
        {decision.publication_id ? (
          <Link to={`/publications/${decision.publication_id}`}>查看发布凭据</Link>
        ) : null}
      </nav>

      <section className="decision-detail__claim">
        <span>DECISION CLAIM</span>
        <h2>{decision.claim}</h2>
        <p>{decision.future_action}</p>
      </section>

      <section className="decision-detail__ledger" aria-label="正式决策元数据">
        <div><span>DECISION</span><code>{decision.decision_id}</code></div>
        <div><span>PRODUCT</span><code>{decision.product_id}</code></div>
        <div><span>PREVIEW</span><code>{decision.publication_preview_id}</code></div>
        <div><span>PUBLICATION</span><code>{decision.publication_id ?? "—"}</code></div>
        <div><span>PUBLISHED</span><time>{decision.published_at ?? "—"}</time></div>
        <div className="decision-detail__commit"><span>REGISTRY COMMIT</span><code>{decision.registry_commit}</code></div>
        <div className="decision-detail__commit"><span>PUBLICATION COMMIT</span><code>{decision.commit_sha ?? "—"}</code></div>
      </section>

      <section className="decision-detail__document">
        <div>
          <header><p className="eyebrow">FORMAL SCOPE</p><h2>适用范围</h2></header>
          <p>{decision.scope.summary}</p>
          <h3>仓库</h3><TextList values={decision.scope.repositories} />
          <h3>路径</h3><TextList values={decision.scope.paths} />
        </div>
        <div>
          <header><p className="eyebrow">INVALIDATION</p><h2>失效条件</h2></header>
          <TextList values={decision.invalidation_conditions} />
        </div>
        <div>
          <header><p className="eyebrow">SAFE PROVENANCE</p><h2>来源与批准坐标</h2></header>
          <dl>
            <div><dt>来源任务</dt><dd><code>{decision.source.thread_id}</code></dd></div>
            <div><dt>来源检查点</dt><dd><code>{decision.source.turn_id}</code></dd></div>
            <div><dt>批准任务</dt><dd><code>{decision.review_approval.thread_id}</code></dd></div>
            <div><dt>批准检查点</dt><dd><code>{decision.review_approval.turn_id}</code></dd></div>
            <div><dt>批准时间</dt><dd><time>{decision.review_approval.recorded_at}</time></dd></div>
          </dl>
        </div>
      </section>

      <details className="decision-detail__json">
        <summary>查看规范化 JSON</summary>
        <pre>{decision.canonical_json}</pre>
      </details>
    </div>
  );
}
