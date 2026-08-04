import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../../api/client";
import type {
  DecisionPreview,
  PublicationFile,
  PublicationPreview,
} from "../../api/types";
import { AsyncState } from "../../shared/AsyncState";
import { StatusBadge } from "../../shared/StatusBadge";


function joined(values: string[]) {
  return values.length ? values.join(" · ") : "—";
}

function registryLabel(path: string) {
  if (path === "decision-registry/registry.json") return "Registry 根索引";
  if (path.endsWith("/product.json")) return "产品元数据";
  if (path.endsWith("/registry.json")) return "产品 Registry";
  return "Decision 正式文档";
}

function ExactFile({ document }: { document: PublicationFile }) {
  return (
    <article
      className="preview-file"
      data-testid={`preview-file-${document.sha256}-${document.path}`}
    >
      <div className="preview-file__head">
        <div>
          <span>{registryLabel(document.path)}</span>
          <code>{document.path}</code>
        </div>
        <code>{document.sha256}</code>
      </div>
      <details open>
        <summary>完整 JSON</summary>
        <pre>{document.content}</pre>
      </details>
    </article>
  );
}

function DecisionPanel({ decision, index }: {
  decision: DecisionPreview;
  index: number;
}) {
  return (
    <article className="preview-decision">
      <header>
        <span>{String(index + 1).padStart(2, "0")}</span>
        <div>
          <p>DECISION / R{String(decision.revision).padStart(4, "0")}</p>
          <h2>{decision.claim}</h2>
        </div>
        <StatusBadge tone="success">{decision.lifecycle}</StatusBadge>
      </header>
      <dl className="preview-decision__fields">
        <div><dt>格式</dt><dd><code>{decision.format}</code><span>schema v{decision.schema_version}</span></dd></div>
        <div><dt>产品身份</dt><dd>{decision.product_name}<code>{decision.product_id}</code></dd></div>
        <div><dt>Decision ID</dt><dd><code>{decision.decision_id}</code></dd></div>
        <div><dt>目标路径</dt><dd><code>{decision.path}</code></dd></div>
        <div><dt>后续行动</dt><dd>{decision.future_action}</dd></div>
        <div><dt>适用范围</dt><dd>{decision.scope.summary}</dd></div>
        <div><dt>仓库范围</dt><dd>{joined(decision.scope.repositories)}</dd></div>
        <div><dt>路径范围</dt><dd>{joined(decision.scope.paths)}</dd></div>
        <div><dt>失效条件</dt><dd>{joined(decision.invalidation_conditions)}</dd></div>
        <div><dt>关系</dt><dd>supersedes {decision.supersedes.length} · variant {decision.variant_of.length}</dd></div>
        <div><dt>候选坐标</dt><dd><code>{decision.source.thread_id}</code><code>{decision.source.turn_id}</code></dd></div>
        <div><dt>审核批准</dt><dd><span>{decision.review_approval.actor}</span><code>{decision.review_approval.thread_id}</code><code>{decision.review_approval.turn_id}</code><span>{decision.review_approval.recorded_at}</span></dd></div>
        <div><dt>预览绑定</dt><dd><code>{decision.publication_preview_id}</code></dd></div>
        <div><dt>文件摘要</dt><dd><code>{decision.sha256}</code></dd></div>
      </dl>
      <details className="preview-decision__json" open>
        <summary>完整 JSON</summary>
        <pre>{decision.canonical_json}</pre>
      </details>
    </article>
  );
}

export function PublicationPreviewPage() {
  const { previewId = "" } = useParams();
  const navigate = useNavigate();
  const [preview, setPreview] = useState<PublicationPreview | null>(null);
  const [failed, setFailed] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [publishFailed, setPublishFailed] = useState(false);

  useEffect(() => {
    let active = true;
    setPreview(null);
    setFailed(false);
    api<PublicationPreview>(
      `/api/v1/web/publication-previews/${previewId}`,
    ).then((value) => {
      if (active) setPreview(value);
    }).catch(() => {
      if (active) setFailed(true);
    });
    return () => { active = false; };
  }, [previewId]);

  if (failed) {
    return <AsyncState kind="error" title="发布预览暂时不可用" />;
  }
  if (!preview) {
    return <AsyncState kind="loading" title="正在验证精确发布预览" />;
  }

  const hasPublication = preview.publication_id !== null;
  const isPublishable = preview.publishability === "publishable";
  const canPublish = isPublishable && !hasPublication;
  const status = hasPublication
    ? "已进入发布流程"
    : preview.publishability === "stale"
      ? "预览已过期"
      : preview.publishability === "registry_unavailable"
        ? "Registry 暂不可用"
        : "可确认发布";
  const statusTone = hasPublication || isPublishable
    ? "success"
    : preview.publishability === "stale" ? "danger" : "warning";

  async function publish() {
    if (!canPublish || publishing) return;
    setPublishing(true);
    setPublishFailed(false);
    const clientActionId = `web_action_publish-${Date.now().toString(36)}`;
    try {
      const result = await api<{ publication_id: string }>(
        `/api/v1/web/publication-previews/${previewId}/publish`,
        {
          method: "POST",
          body: JSON.stringify({ client_action_id: clientActionId }),
        },
      );
      await navigate(`/publications/${result.publication_id}`);
    } catch {
      try {
        const refreshed = await api<PublicationPreview>(
          `/api/v1/web/publication-previews/${previewId}`,
        );
        if (refreshed.publication_id !== null) {
          await navigate(`/publications/${refreshed.publication_id}`);
          return;
        }
      } catch {
        // The confirmation outcome remains unknown; never issue it again here.
      }
      setPublishFailed(true);
    }
  }

  return (
    <div className="page preview-page">
      <header className="page-header preview-page__header">
        <div>
          <p className="eyebrow">PUBLICATION / IMMUTABLE MANIFEST</p>
          <h1>发布预览</h1>
          <p className="page-header__lead">
            这是唯一确认页面。下列路径、摘要与 JSON 字节已冻结，页面不会改写 Registry。
          </p>
        </div>
        <div className="preview-page__status">
          <StatusBadge tone={statusTone}>{status}</StatusBadge>
          <code>{preview.preview_id}</code>
        </div>
      </header>

      {preview.publishability !== "publishable" && !hasPublication ? (
        <section className="preview-alert" aria-live="polite">
          <strong>{status}</strong>
          <span>
            {preview.publishability === "stale"
              ? "请返回修改审核并显式生成新的发布预览。"
              : "当前无法重新证明 origin/main 与 Registry 基线，确认操作已锁定。"}
          </span>
        </section>
      ) : null}

      <section className="preview-ledger" aria-label="预览身份与基线">
        <div><span>PRODUCT</span><strong>{preview.product_name}</strong><code>{preview.product_id}</code></div>
        <div><span>BASE COMMIT</span><code>{preview.base_commit}</code></div>
        <div><span>CONTENT DIGEST</span><code>{preview.content_digest}</code></div>
        <div><span>CREATED</span><time>{preview.created_at}</time></div>
      </section>

      <section className="preview-section">
        <div className="preview-section__head">
          <div><p className="eyebrow">FORMAL DECISIONS</p><h2>决策字段</h2></div>
          <span>{preview.decisions.length} 条</span>
        </div>
        <div className="preview-decision-list">
          {preview.decisions.map((decision, index) => (
            <DecisionPanel decision={decision} index={index} key={decision.decision_id} />
          ))}
        </div>
      </section>

      <section className="preview-section">
        <div className="preview-section__head">
          <div><p className="eyebrow">EXACT DISPLAY DOCUMENTS</p><h2>冻结文件</h2></div>
          <span>{preview.display_documents.length} 份</span>
        </div>
        <div className="preview-files">
          {preview.display_documents.map((document) => (
            <ExactFile document={document} key={document.path} />
          ))}
        </div>
      </section>

      <section className="preview-split">
        <article className="preview-technical">
          <h2>基线 Registry 摘要</h2>
          <dl>
            {Object.entries(preview.base_registry_digests).map(([path, digest]) => (
              <div key={path}><dt><code>{path}</code></dt><dd><code>{digest}</code></dd></div>
            ))}
          </dl>
        </article>
        <article className="preview-technical">
          <h2>Changed files</h2>
          <ol>
            {preview.changed_files.map((document) => (
              <li key={document.path}><code>{document.path}</code><code>{document.sha256}</code></li>
            ))}
          </ol>
        </article>
        <article className="preview-technical">
          <h2>审核与正式身份</h2>
          <dl>
            <div><dt>Review batch</dt><dd><code>{preview.review_batch_id}</code></dd></div>
            {preview.review_ids.map((value) => <div key={value}><dt>Review</dt><dd><code>{value}</code></dd></div>)}
            {preview.candidate_ids.map((value) => <div key={value}><dt>Candidate</dt><dd><code>{value}</code></dd></div>)}
            {preview.decision_ids.map((value) => <div key={value}><dt>Decision</dt><dd><code>{value}</code></dd></div>)}
          </dl>
        </article>
      </section>

      <section className="preview-commit">
        <span>提交消息</span>
        <pre>{preview.commit_message}</pre>
      </section>

      <footer className="preview-confirmation">
        <Link to={`/products/${preview.product_id}/candidates`}>
          ← 返回修改审核
        </Link>
        <div>
          <span aria-live="polite">
            {publishFailed
              ? "发布状态未能确认，请从发布历史检查。"
              : "仅本次明确确认会启动精确写入"}
          </span>
          {hasPublication ? (
            <Link
              className="primary-button preview-publish"
              to={`/publications/${preview.publication_id}`}
            >
              查看发布状态
            </Link>
          ) : (
            <button
              className="primary-button preview-publish"
              type="button"
              disabled={!canPublish || publishing}
              onClick={publish}
            >
              {publishing ? "正在证明提交…" : `确认发布 ${preview.decisions.length} 条决策`}
            </button>
          )}
        </div>
      </footer>
    </div>
  );
}
