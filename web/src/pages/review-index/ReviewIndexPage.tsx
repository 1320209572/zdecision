import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "../../api/client";
import type {
  CatalogNode,
  Dashboard,
  DecisionSpaceSummary,
  RepositorySpacesView,
} from "../../api/types";
import { AsyncState } from "../../shared/AsyncState";

function sharedLeaves(root: CatalogNode | null): DecisionSpaceSummary[] {
  if (!root) return [];
  return [
    ...(root.space ? [root.space] : []),
    ...root.children.flatMap(sharedLeaves),
  ];
}

function SpaceGroup({
  title,
  spaces,
  repositoryId,
}: {
  title: string;
  spaces: DecisionSpaceSummary[];
  repositoryId: string;
}) {
  if (!spaces.length) return null;
  return (
    <section className="review-index__group">
      <header><h2>{title}</h2><span>{spaces.length} 个决策空间</span></header>
      <div className="review-index">
        {spaces.map((space, index) => {
          const query = repositoryId
            ? `?repository_id=${encodeURIComponent(repositoryId)}`
            : "";
          return (
            <Link
              key={space.decision_space_id}
              to={`/spaces/${space.decision_space_id}/candidates${query}`}
              className="review-index__row"
            >
              <span>{String(index + 1).padStart(2, "0")}</span>
              <strong>{space.display_name}</strong>
              <small>{space.source_root}</small>
              <b>{space.pending_candidate_count}</b>
              <em>待审核</em>
            </Link>
          );
        })}
      </div>
    </section>
  );
}

export function ReviewIndexPage() {
  const [searchParams] = useSearchParams();
  const repositoryId = searchParams.get("repository_id") ?? "";
  const [spaces, setSpaces] = useState<DecisionSpaceSummary[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    setSpaces(null);
    setFailed(false);
    const request = repositoryId
      ? api<RepositorySpacesView>(
          `/api/v1/web/repositories/${encodeURIComponent(repositoryId)}/spaces`,
        ).then((view) => view.spaces)
      : api<Dashboard>("/api/v1/web/dashboard").then((dashboard) => [
          ...dashboard.products,
          ...sharedLeaves(dashboard.shared_tree),
        ]);
    request
      .then((value) => active && setSpaces(value))
      .catch(() => active && setFailed(true));
    return () => { active = false; };
  }, [repositoryId]);

  const grouped = useMemo(() => ({
    products: (spaces ?? [])
      .filter((space) => space.kind === "product")
      .sort((left, right) => right.pending_candidate_count - left.pending_candidate_count),
    shared: (spaces ?? [])
      .filter((space) => space.kind === "shared_unit")
      .sort((left, right) => right.pending_candidate_count - left.pending_candidate_count),
  }), [spaces]);

  if (failed) return <AsyncState kind="error" title="候选审核索引暂时不可用" />;
  if (!spaces) return <AsyncState kind="loading" title="正在读取待审核候选" />;

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <p className="eyebrow">CANDIDATE REVIEW / DECISION SPACES</p>
          <h1>候选审核</h1>
          <p className="page-header__lead">
            {repositoryId
              ? "此仓库包含多个独立决策空间，请选择明确叶子进入审核。"
              : "按产品与 Shared 叶子进入独立审核工作区。"}
          </p>
        </div>
      </header>
      <SpaceGroup title="产品" spaces={grouped.products} repositoryId={repositoryId} />
      <SpaceGroup title="Shared" spaces={grouped.shared} repositoryId={repositoryId} />
      {!spaces.length ? <AsyncState kind="empty" title="没有可审核的决策空间" /> : null}
    </div>
  );
}
