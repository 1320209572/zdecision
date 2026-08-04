import { createBrowserRouter } from "react-router-dom";

import { AppShell } from "./AppShell";
import { ReviewIndexPage } from "../pages/review-index/ReviewIndexPage";
import {
  CandidateReviewPage,
  RepositoryEntryPage,
} from "../pages/candidate-review/CandidateReviewPage";
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
      { path: "products/:productId/candidates", Component: CandidateReviewPage },
      { path: "products/:productId/decisions", Component: DecisionCatalogPage },
      {
        path: "products/:productId/decisions/:decisionId",
        Component: DecisionDetailPage,
      },
      { path: "products/:productId/publications", Component: PublicationHistoryPage },
      { path: "publication-previews/:previewId", Component: PublicationPreviewPage },
      { path: "publications/:publicationId", Component: PublicationDetailPage },
    ],
  },
]);
