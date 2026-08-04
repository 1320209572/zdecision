import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api/client";
import type { Dashboard } from "../../api/types";
import { AsyncState } from "../../shared/AsyncState";

export function ReviewIndexPage() {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    api<Dashboard>("/api/v1/web/dashboard")
      .then((value) => active && setDashboard(value))
      .catch(() => active && setFailed(true));
    return () => {
      active = false;
    };
  }, []);

  if (failed) return <AsyncState kind="error" title="候选审核索引暂时不可用" />;
  if (!dashboard) return <AsyncState kind="loading" title="正在读取待审核候选" />;
  const products = [...dashboard.products].sort(
    (left, right) => right.pending_candidate_count - left.pending_candidate_count,
  );

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <p className="eyebrow">CANDIDATE REVIEW / ALL PRODUCTS</p>
          <h1>候选审核</h1>
          <p className="page-header__lead">按产品进入独立审核工作区，不跨产品合并审批。</p>
        </div>
      </header>
      <div className="review-index">
        {products.map((product, index) => (
          <Link
            key={product.product_id}
            to={`/products/${product.product_id}/candidates`}
            className="review-index__row"
          >
            <span>{String(index + 1).padStart(2, "0")}</span>
            <strong>{product.product_name}</strong>
            <small>{product.repository_ids.length} 个仓库</small>
            <b>{product.pending_candidate_count}</b>
            <em>待审核</em>
          </Link>
        ))}
      </div>
    </div>
  );
}
