import type {
  CandidateContent,
  CandidateInboxItem,
  DecisionSpaceRef,
  ReviewDraftItem,
} from "../../api/types";


export interface CandidateReviewRowProps {
  item: CandidateInboxItem;
  space: DecisionSpaceRef;
  action: ReviewDraftItem | undefined;
  selected: boolean;
  stale: boolean;
  classificationDisabled?: boolean;
  onSelectedChange(familyId: string, selected: boolean): void;
  onDirectAction(familyId: string, action: "accept" | "reject"): void;
  onEditAccept(familyId: string, content: CandidateContent): void;
  onLoadLatest?(): void;
}

function actionLabel(action: ReviewDraftItem | undefined): string {
  if (action?.action === "accept") return "已接受";
  if (action?.action === "edit_accept") return "编辑后接受";
  if (action?.action === "reject") return "已拒绝";
  return "未处理";
}

function lines(value: string): string[] {
  return value.split("\n").filter((item) => item.length > 0);
}

export function CandidateReviewRow({
  item,
  space,
  action,
  selected,
  stale,
  classificationDisabled = false,
  onSelectedChange,
  onDirectAction,
  onEditAccept,
  onLoadLatest,
}: CandidateReviewRowProps) {
  const claim = item.content.claim;
  const edited = action?.effective_content ?? item.content;
  const editing = action?.action === "edit_accept";

  function updateEdited(patch: Partial<CandidateContent>) {
    onEditAccept(item.family_id, { ...edited, ...patch });
  }

  return (
    <article
      className={`candidate-row${selected ? " candidate-row--selected" : ""}`}
      aria-label={`候选 ${claim}`}
    >
      <div className="candidate-row__main">
        <label className="candidate-row__select">
          <input
            type="checkbox"
            aria-label={`选择${claim}`}
            checked={selected}
            onChange={(event) =>
              onSelectedChange(item.family_id, event.target.checked)
            }
          />
        </label>

        <div className="candidate-row__body">
          <div className="candidate-row__identity">
            <span>R{item.revision}</span>
            <h2>{claim}</h2>
          </div>
          <p>{item.content.future_action}</p>
          <div className="candidate-row__scope">
            <span>{item.content.scope_summary}</span>
            <span>{space.breadcrumb.join(" / ")}</span>
            <span>{item.content.paths.join(" · ") || "—"}</span>
          </div>
        </div>

        <div className="candidate-row__state">
          {stale || item.stale_draft ? (
            <span className="candidate-row__stale">已有新版本</span>
          ) : null}
          <strong>{actionLabel(action)}</strong>
        </div>

        <div className="candidate-row__actions">
          <button
            type="button"
            aria-label={`接受${claim}`}
            disabled={classificationDisabled}
            onClick={() => onDirectAction(item.family_id, "accept")}
          >
            接受
          </button>
          <button
            type="button"
            aria-label={`拒绝${claim}`}
            disabled={classificationDisabled}
            onClick={() => onDirectAction(item.family_id, "reject")}
          >
            拒绝
          </button>
          <button
            type="button"
            aria-label={`编辑${claim}`}
            disabled={classificationDisabled}
            onClick={() => onEditAccept(item.family_id, edited)}
          >
            编辑
          </button>
          {(stale || item.stale_draft) && onLoadLatest ? (
            <button type="button" onClick={onLoadLatest}>载入最新版本</button>
          ) : null}
        </div>
      </div>

      <details className="candidate-row__evidence">
        <summary>查看证据</summary>
        <dl>
          <div><dt>Revision ID</dt><dd><code>{item.revision_id}</code></dd></div>
          <div><dt>Content digest</dt><dd><code>{item.content_digest}</code></dd></div>
          <div><dt>Repository</dt><dd><code>{item.repository_id}</code></dd></div>
          <div>
            <dt>Capture requests</dt>
            <dd>
              {item.capture_request_ids.length
                ? item.capture_request_ids.map((requestId) => (
                    <code key={requestId}>{requestId}</code>
                  ))
                : "—"}
            </dd>
          </div>
          <div className="candidate-row__invalidation">
            <dt>失效条件</dt>
            <dd>{item.content.invalidation_conditions.join(" · ") || "—"}</dd>
          </div>
        </dl>
      </details>

      {editing ? (
        <fieldset className="candidate-row__edit">
          <legend>编辑后接受</legend>
          <label>
            <span>决策空间（锁定）</span>
            <input value={space.breadcrumb.join(" / ")} readOnly />
          </label>
          <label>
            <span>仓库（锁定）</span>
            <input value={edited.repositories.join(", ")} readOnly />
          </label>
          <label className="candidate-row__edit-wide">
            <span>决策主张</span>
            <textarea
              value={edited.claim}
              onChange={(event) => updateEdited({ claim: event.target.value })}
            />
          </label>
          <label className="candidate-row__edit-wide">
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
              onChange={(event) => updateEdited({ paths: lines(event.target.value) })}
            />
          </label>
          <label className="candidate-row__edit-wide">
            <span>失效条件（每行一项）</span>
            <textarea
              value={edited.invalidation_conditions.join("\n")}
              onChange={(event) =>
                updateEdited({
                  invalidation_conditions: lines(event.target.value),
                })
              }
            />
          </label>
        </fieldset>
      ) : null}
    </article>
  );
}
