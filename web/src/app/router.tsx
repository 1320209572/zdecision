import { createBrowserRouter, useLocation } from "react-router-dom";

import { AppShell } from "./AppShell";
import { CompanyOverviewPage } from "../pages/company-overview/CompanyOverviewPage";
import { ReviewIndexPage } from "../pages/review-index/ReviewIndexPage";

function DeferredSlicePage() {
  const location = useLocation();
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <p className="eyebrow">PLANNED PRODUCT SLICE</p>
          <h1>工作区</h1>
          <p className="page-header__lead">{location.pathname}</p>
        </div>
      </header>
      <section className="deferred-panel">
        <span aria-hidden="true">◇</span>
        <div>
          <h2>功能将在后续切片启用</h2>
          <p>当前页面只保留稳定路由，不提供尚未完成的操作。</p>
        </div>
      </section>
    </div>
  );
}

export const router = createBrowserRouter([
  {
    path: "/",
    Component: AppShell,
    children: [
      { index: true, Component: CompanyOverviewPage },
      { path: "reviews", Component: ReviewIndexPage },
      { path: "decisions", Component: DeferredSlicePage },
      { path: "publications", Component: DeferredSlicePage },
      { path: "products/:productId/candidates", Component: DeferredSlicePage },
      { path: "products/:productId/decisions", Component: DeferredSlicePage },
      {
        path: "products/:productId/decisions/:decisionId",
        Component: DeferredSlicePage,
      },
      { path: "products/:productId/publications", Component: DeferredSlicePage },
      { path: "publication-previews/:previewId", Component: DeferredSlicePage },
      { path: "publications/:publicationId", Component: DeferredSlicePage },
    ],
  },
]);
