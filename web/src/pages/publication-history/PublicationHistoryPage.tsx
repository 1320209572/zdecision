import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "../../api/client";
import type {
  PublicationDetail,
  PublicationHistory,
  PublicationState,
} from "../../api/types";
import { AsyncState } from "../../shared/AsyncState";
import { StatusBadge } from "../../shared/StatusBadge";


export const publicationLabels: Record<PublicationState, string> = {
  confirmed: "准备提交",
  committed_pending_push: "已提交，等待推送",
  completed: "发布完成",
  ambiguous: "需要人工处理",
};

export function publicationTone(state: PublicationState) {
  if (state === "completed") return "success" as const;
  if (state === "ambiguous") return "danger" as const;
  return "warning" as const;
}

function PublicationRow({ publication }: { publication: PublicationDetail }) {
  return (
    <Link
      className="publication-history__row"
      to={`/publications/${publication.publication_id}`}
    >
      <span className="publication-history__count">
        {String(publication.decision_count).padStart(2, "0")}
        <small>DECISIONS</small>
      </span>
      <span className="publication-history__identity">
        <strong>发布凭据</strong>
        <code>{publication.publication_id}</code>
      </span>
      <span className="publication-history__approval">
        <b>{publication.actor_id}</b>
        <time>{publication.approved_at}</time>
      </span>
      <StatusBadge tone={publicationTone(publication.state)}>
        {publicationLabels[publication.state]}
      </StatusBadge>
      <span className="publication-history__arrow" aria-hidden="true">↗</span>
    </Link>
  );
}

export function PublicationHistoryPage() {
  const { productId } = useParams();
  const [history, setHistory] = useState<PublicationHistory | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    const query = productId ? `?product_id=${encodeURIComponent(productId)}` : "";
    setHistory(null);
    setFailed(false);
    api<PublicationHistory>(`/api/v1/web/publications${query}`)
      .then((value) => { if (active) setHistory(value); })
      .catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [productId]);

  const groups = useMemo(() => {
    const values = new Map<string, PublicationDetail[]>();
    for (const item of history?.items ?? []) {
      const key = `${item.product_id}:${item.product_name}`;
      values.set(key, [...(values.get(key) ?? []), item]);
    }
    return [...values.entries()];
  }, [history]);

  if (failed) return <AsyncState kind="error" title="发布历史暂时不可用" />;
  if (!history) return <AsyncState kind="loading" title="正在读取发布凭据" />;

  return (
    <div className="page publication-history">
      <header className="page-header publication-history__header">
        <div>
          <p className="eyebrow">PUBLICATION / PROOF LEDGER</p>
          <h1>{productId ? "产品发布历史" : "发布历史"}</h1>
          <p className="page-header__lead">
            每一行只陈列用户批准、耐久状态与 origin/main 提交证明。
          </p>
        </div>
        <span className="publication-history__total">
          <strong>{history.total}</strong><small>PUBLICATIONS</small>
        </span>
      </header>

      {groups.length ? groups.map(([key, items]) => (
        <section className="publication-history__group" key={key}>
          <header>
            <div>
              <p className="eyebrow">PRODUCT LEDGER</p>
              <h2>{items[0].product_name}</h2>
            </div>
            <code>{items[0].product_id}</code>
          </header>
          <div>{items.map((item) => (
            <PublicationRow publication={item} key={item.publication_id} />
          ))}</div>
        </section>
      )) : (
        <section className="publication-history__empty">
          <span aria-hidden="true">◇</span>
          <h2>还没有发布凭据</h2>
          <p>只有经过显式确认的冻结预览才会出现在这里。</p>
        </section>
      )}
    </div>
  );
}
