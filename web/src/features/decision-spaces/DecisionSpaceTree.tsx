import { useState } from "react";
import { Link } from "react-router-dom";

import type { CatalogNode } from "../../api/types";

const assetLabels: Record<string, string> = {
  component_library: "组件库",
  library: "组件库",
  cross_product_module: "跨产品模块",
};

function DecisionSpaceNode({ node, depth }: { node: CatalogNode; depth: number }) {
  const group = node.kind === "catalog_group";
  const [expanded, setExpanded] = useState(true);
  const space = node.space;

  return (
    <li className={`space-tree__node space-tree__node--${node.kind}`}>
      <div className="space-tree__row" style={{ "--space-depth": depth } as React.CSSProperties}>
        {group ? (
          <button
            type="button"
            className="space-tree__toggle"
            aria-expanded={expanded}
            aria-label={`${expanded ? "折叠" : "展开"} ${node.display_name}`}
            onClick={() => setExpanded((value) => !value)}
          >
            <span aria-hidden="true">{expanded ? "−" : "+"}</span>
            <strong>{node.display_name}</strong>
          </button>
        ) : (
          <div className="space-tree__leaf-name">
            <span aria-hidden="true">◇</span>
            <strong>{node.display_name}</strong>
          </div>
        )}
        <span className="space-tree__counts">
          <b>{node.pending_candidate_count}</b> 待审核
          <i aria-hidden="true" />
          <b>{node.active_decision_count ?? "—"}</b> 生效
        </span>
        {space ? (
          <nav className="space-tree__actions" aria-label={`${space.display_name} 操作`}>
            <Link aria-label={`${space.display_name} 候选`} to={`/spaces/${space.decision_space_id}/candidates`}>候选</Link>
            <Link aria-label={`${space.display_name} 决策`} to={`/spaces/${space.decision_space_id}/decisions`}>决策</Link>
            <Link aria-label={`${space.display_name} 发布`} to={`/spaces/${space.decision_space_id}/publications`}>发布</Link>
          </nav>
        ) : null}
      </div>
      {space ? (
        <div className="space-tree__meta" style={{ "--space-depth": depth } as React.CSSProperties}>
          <span>{space.breadcrumb.join(" / ")}</span>
          <code>{space.source_root}</code>
          <span>{assetLabels[space.asset_type ?? ""] ?? "共享单元"}{space.package_name ? ` · ${space.package_name}` : ""}</span>
        </div>
      ) : null}
      {group && expanded && node.children.length ? (
        <ul>{node.children.map((child) => (
          <DecisionSpaceNode node={child} depth={depth + 1} key={child.node_id} />
        ))}</ul>
      ) : null}
    </li>
  );
}

export function DecisionSpaceTree({ root }: { root: CatalogNode }) {
  return <ul className="space-tree"><DecisionSpaceNode node={root} depth={0} /></ul>;
}
