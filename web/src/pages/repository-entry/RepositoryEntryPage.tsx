import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { api } from "../../api/client";
import type { RepositorySpacesView } from "../../api/types";
import { AsyncState } from "../../shared/AsyncState";
import { CompanyOverviewPage } from "../company-overview/CompanyOverviewPage";

export function RepositoryEntryPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const repositoryId = params.get("repository_id");
  const decisionSpaceId = params.get("decision_space_id");
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    if (!repositoryId) return;
    let active = true;
    api<RepositorySpacesView>(
      `/api/v1/web/repositories/${encodeURIComponent(repositoryId)}/spaces`,
    )
      .then((result) => {
        if (!active) return;
        if (decisionSpaceId) {
          const leaf = result.spaces.find(
            (space) => space.decision_space_id === decisionSpaceId,
          );
          if (!leaf) {
            setUnavailable(true);
            return;
          }
          void navigate(
            `/spaces/${leaf.decision_space_id}/candidates?repository_id=${repositoryId}`,
            { replace: true },
          );
          return;
        }
        void navigate(`/reviews?repository_id=${repositoryId}`, { replace: true });
      })
      .catch(() => active && setUnavailable(true));
    return () => { active = false; };
  }, [decisionSpaceId, navigate, repositoryId]);

  if (!repositoryId) return <CompanyOverviewPage />;
  if (unavailable) return <AsyncState kind="error" title="仓库或决策空间未登记" />;
  return <AsyncState kind="loading" title="正在读取仓库决策空间" />;
}
