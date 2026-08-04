import type {
  CandidateContent,
  CandidateInboxItem,
  ReviewAction,
  ReviewDraftItem,
} from "../../api/types";


interface ReviewEditorProps {
  item: CandidateInboxItem;
  action: ReviewDraftItem | undefined;
  onChange: (value: ReviewDraftItem | undefined) => void;
}

const actionLabels: Record<ReviewAction, string> = {
  accept: "接受",
  edit_accept: "编辑后接受",
  reject: "拒绝",
  skip: "跳过",
};

function fromCandidate(
  item: CandidateInboxItem,
  action: ReviewAction,
): ReviewDraftItem {
  return {
    family_id: item.family_id,
    repository_id: item.repository_id,
    revision_id: item.revision_id,
    revision: item.revision,
    content_digest: item.content_digest,
    action,
    effective_content: action === "edit_accept" ? item.content : null,
    note: null,
  };
}

export function ReviewEditor({ item, action, onChange }: ReviewEditorProps) {
  const edited = action?.effective_content ?? item.content;

  function selectAction(value: string) {
    if (!value) {
      onChange(undefined);
      return;
    }
    onChange(fromCandidate(item, value as ReviewAction));
  }

  function updateEdited(patch: Partial<CandidateContent>) {
    if (!action || action.action !== "edit_accept") return;
    onChange({
      ...action,
      effective_content: { ...edited, ...patch },
    });
  }

  return (
    <article className="candidate-card">
      <div className="candidate-card__topline">
        <span className="candidate-card__revision">R{item.revision}</span>
        <code>{item.family_id}</code>
        {item.stale_draft ? (
          <span className="candidate-card__stale">已有新版本</span>
        ) : null}
      </div>

      <div className="candidate-card__provenance" aria-label="候选来源标识">
        <span>
          <b>REVISION ID</b>
          <code>{item.revision_id}</code>
        </span>
        <span>
          <b>CONTENT DIGEST</b>
          <code>{item.content_digest}</code>
        </span>
        <span>
          <b>OWNING REPOSITORY</b>
          <code>{item.repository_id}</code>
        </span>
        <span>
          <b>CAPTURE REQUESTS</b>
          {item.capture_request_ids.length ? (
            item.capture_request_ids.map((requestId) => (
              <code key={requestId}>{requestId}</code>
            ))
          ) : (
            <em>—</em>
          )}
        </span>
      </div>

      <div className="candidate-card__content">
        <div className="candidate-card__claim">
          <span>DECISION CLAIM</span>
          <h2>{item.content.claim}</h2>
        </div>
        <dl className="candidate-fields">
          <div>
            <dt>后续行动</dt>
            <dd>{item.content.future_action}</dd>
          </div>
          <div>
            <dt>适用范围</dt>
            <dd>{item.content.scope_summary}</dd>
          </div>
          <div>
            <dt>仓库范围</dt>
            <dd>{item.content.repositories.join(" · ") || "—"}</dd>
          </div>
          <div>
            <dt>路径</dt>
            <dd>{item.content.paths.join(" · ") || "—"}</dd>
          </div>
          <div>
            <dt>失效条件</dt>
            <dd>{item.content.invalidation_conditions.join(" · ") || "—"}</dd>
          </div>
        </dl>
      </div>

      <div className="review-editor">
        <label>
          <span>审核动作</span>
          <select
            aria-label="审核动作"
            value={action?.action ?? ""}
            onChange={(event) => selectAction(event.target.value)}
          >
            <option value="">未选择</option>
            {Object.entries(actionLabels).map(([value, label]) => (
              <option value={value} key={value}>{label}</option>
            ))}
          </select>
        </label>
        <label className="review-editor__note">
          <span>内部备注</span>
          <input
            value={action?.note ?? ""}
            maxLength={1000}
            disabled={!action}
            onChange={(event) => {
              if (action) onChange({ ...action, note: event.target.value || null });
            }}
          />
        </label>
      </div>

      {action?.action === "edit_accept" ? (
        <fieldset className="edit-grid">
          <legend>编辑候选内容</legend>
          <label>
            <span>产品（锁定）</span>
            <input value={edited.product} readOnly />
          </label>
          <label>
            <span>仓库（锁定）</span>
            <input value={edited.repositories.join(", ")} readOnly />
          </label>
          <label className="edit-grid__wide">
            <span>决策主张</span>
            <textarea
              value={edited.claim}
              onChange={(event) => updateEdited({ claim: event.target.value })}
            />
          </label>
          <label className="edit-grid__wide">
            <span>后续行动</span>
            <textarea
              value={edited.future_action}
              onChange={(event) =>
                updateEdited({ future_action: event.target.value })
              }
            />
          </label>
          <label>
            <span>适用范围</span>
            <textarea
              value={edited.scope_summary}
              onChange={(event) =>
                updateEdited({ scope_summary: event.target.value })
              }
            />
          </label>
          <label>
            <span>路径（每行一项）</span>
            <textarea
              value={edited.paths.join("\n")}
              onChange={(event) =>
                updateEdited({ paths: event.target.value.split("\n") })
              }
            />
          </label>
          <label className="edit-grid__wide">
            <span>失效条件（每行一项）</span>
            <textarea
              value={edited.invalidation_conditions.join("\n")}
              onChange={(event) =>
                updateEdited({
                  invalidation_conditions: event.target.value.split("\n"),
                })
              }
            />
          </label>
        </fieldset>
      ) : null}
    </article>
  );
}
