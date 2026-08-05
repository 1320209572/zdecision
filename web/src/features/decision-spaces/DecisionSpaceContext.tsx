import type { DecisionSpaceRef } from "../../api/types";


export function DecisionSpaceContext({ space }: { space: DecisionSpaceRef }) {
  const breadcrumb = space.breadcrumb.join(" / ");
  const showBreadcrumb = breadcrumb !== space.display_name;
  const packageAndAsset = [space.package_name, space.asset_type]
    .filter((value): value is string => Boolean(value))
    .join(" · ");

  return (
    <p className="decision-space-context">
      {showBreadcrumb ? <span>{breadcrumb}</span> : null}
      <code>{space.source_root}</code>
      {packageAndAsset ? <span>{packageAndAsset}</span> : null}
    </p>
  );
}
