import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "../../api/client";
import type { PublicationDetail } from "../../api/types";
import { AsyncState } from "../../shared/AsyncState";
import { StatusBadge } from "../../shared/StatusBadge";
import { publicationLabels, publicationTone } from "./PublicationHistoryPage";


export function PublicationDetailPage() {
  const { publicationId = "" } = useParams();
  const [publication, setPublication] = useState<PublicationDetail | null>(null);
  const [failed, setFailed] = useState(false);
  const [resuming, setResuming] = useState(false);

  async function load(active: () => boolean = () => true) {
    try {
      const value = await api<PublicationDetail>(
        `/api/v1/web/publications/${publicationId}`,
      );
      if (active()) setPublication(value);
    } catch {
      if (active()) setFailed(true);
    }
  }

  useEffect(() => {
    let active = true;
    setPublication(null);
    setFailed(false);
    void load(() => active);
    return () => { active = false; };
  }, [publicationId]);

  async function resume() {
    if (!publication || publication.state !== "committed_pending_push" || resuming) return;
    setResuming(true);
    setFailed(false);
    try {
      await api(`/api/v1/web/publications/${publicationId}/resume`, {
        method: "POST",
        body: JSON.stringify({
          client_action_id: `web_action_resume-${Date.now().toString(36)}`,
        }),
      });
      await load();
    } catch {
      setFailed(true);
    } finally {
      setResuming(false);
    }
  }

  if (failed) return <AsyncState kind="error" title="发布凭据暂时不可用" />;
  if (!publication) return <AsyncState kind="loading" title="正在验证发布凭据" />;

  return (
    <div className="page publication-detail">
      <header className="page-header publication-detail__header">
        <div>
          <p className="eyebrow">PUBLICATION / EXACT REMOTE PROOF</p>
          <h1>{publication.product_name}</h1>
          <p className="page-header__lead">
            一次确认、一份冻结预览、一个可验证的 Git 提交。
          </p>
        </div>
        <StatusBadge tone={publicationTone(publication.state)}>
          {publicationLabels[publication.state]}
        </StatusBadge>
      </header>

      <section className={`publication-proof publication-proof--${publication.state}`}>
        <span className="publication-proof__signal" aria-hidden="true" />
        <div>
          <p>耐久状态</p>
          <h2>
            {publication.state === "ambiguous"
              ? "自动恢复已锁定"
              : publication.state === "committed_pending_push"
                ? "精确提交已固定"
                : publication.state === "completed"
                  ? "远端证明已完成"
                  : "用户确认已记录"}
          </h2>
          <span>
            {publication.state === "ambiguous"
              ? "自动恢复已锁定；需要由受信任的维护者核对远端状态。"
              : publication.state === "committed_pending_push"
                ? "本地提交已固定；继续操作只会重试同一个 SHA。"
                : publication.state === "completed"
                  ? "origin/main 已证明包含这一个精确提交。"
                  : "用户确认已耐久保存，尚未写入 Registry。"}
          </span>
        </div>
        {publication.state === "committed_pending_push" ? (
          <button className="primary-button" disabled={resuming} onClick={resume}>
            {resuming ? "正在核验…" : "继续安全推送"}
          </button>
        ) : null}
      </section>

      <section className="publication-detail__ledger">
        <div><span>PUBLICATION</span><code>{publication.publication_id}</code></div>
        <div><span>PREVIEW</span><code>{publication.preview_id}</code></div>
        <div><span>PRODUCT</span><code>{publication.product_id}</code></div>
        <div><span>APPROVED BY</span><strong>{publication.actor_id}</strong><time>{publication.approved_at}</time></div>
        <div className="publication-detail__commit"><span>COMMIT SHA</span><code>{publication.commit_sha ?? "—"}</code></div>
        <div><span>RECOVERY</span><code>{publication.recovery_code ?? "none"}</code></div>
      </section>

      <section className="publication-detail__decisions">
        <header><p className="eyebrow">FORMAL OUTPUT</p><h2>精确 Decision</h2></header>
        <div>
          {publication.decision_ids.map((decisionId, index) => (
            <Link
              key={decisionId}
              to={`/products/${publication.product_id}/decisions/${decisionId}`}
            >
              <span>{String(index + 1).padStart(2, "0")}</span>
              <code>{decisionId}</code>
              <b aria-hidden="true">↗</b>
            </Link>
          ))}
        </div>
      </section>
    </div>
  );
}
