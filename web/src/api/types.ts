export type RegistryState = "available" | "unavailable";
export type PublicationState =
  | "confirmed"
  | "committed_pending_push"
  | "completed";

export interface DashboardMetrics {
  product_count: number;
  pending_candidate_count: number;
  active_decision_count: number | null;
  completed_this_week: number;
}

export interface RegistryStatus {
  state: RegistryState;
  commit_sha: string | null;
}

export interface ProductSummary {
  product_id: string;
  product_name: string;
  repository_ids: string[];
  pending_candidate_count: number;
  active_decision_count: number | null;
  last_activity_at: string | null;
}

export interface PublicationSummary {
  publication_id: string;
  preview_id: string;
  product_id: string;
  product_name: string;
  decision_count: number;
  actor_id: string;
  approved_at: string;
  state: PublicationState;
  recovery_code: string | null;
  commit_sha: string | null;
}

export interface Dashboard {
  metrics: DashboardMetrics;
  registry: RegistryStatus;
  products: ProductSummary[];
  recent_publications: PublicationSummary[];
}
