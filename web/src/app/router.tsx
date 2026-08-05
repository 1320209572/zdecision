import { createBrowserRouter } from "react-router-dom";

import { AppShell } from "./AppShell";
import { ReviewIndexPage } from "../pages/review-index/ReviewIndexPage";
import { CandidateReviewPage } from "../pages/candidate-review/CandidateReviewPage";
import { RepositoryEntryPage } from "../pages/repository-entry/RepositoryEntryPage";
import { PublicationPreviewPage } from "../pages/publication-preview/PublicationPreviewPage";
import { PublicationHistoryPage } from "../pages/publication-history/PublicationHistoryPage";
import { PublicationDetailPage } from "../pages/publication-history/PublicationDetailPage";
import { DecisionCatalogPage } from "../pages/decision-catalog/DecisionCatalogPage";
import { DecisionDetailPage } from "../pages/decision-catalog/DecisionDetailPage";

export const router = createBrowserRouter([
  {
    path: "/",
    Component: AppShell,
    children: [
      { index: true, Component: RepositoryEntryPage },
      { path: "reviews", Component: ReviewIndexPage },
      { path: "decisions", Component: DecisionCatalogPage },
      { path: "publications", Component: PublicationHistoryPage },
      { path: "spaces/:decisionSpaceId/candidates", Component: CandidateReviewPage },
      { path: "spaces/:decisionSpaceId/decisions", Component: DecisionCatalogPage },
      {
        path: "spaces/:decisionSpaceId/decisions/:decisionId",
        Component: DecisionDetailPage,
      },
      { path: "spaces/:decisionSpaceId/publications", Component: PublicationHistoryPage },
      { path: "publication-previews/:previewId", Component: PublicationPreviewPage },
      { path: "publications/:publicationId", Component: PublicationDetailPage },
    ],
  },
]);
