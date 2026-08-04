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

export type ReviewAction = "accept" | "edit_accept" | "reject" | "skip";
export type CandidateReviewState =
  | "pending"
  | "accepted"
  | "rejected"
  | "published";

export interface RepositoryView {
  repository_id: string;
  product_id: string;
  product_name: string;
  enabled: boolean;
}

export interface CandidateContent {
  product: string;
  claim: string;
  future_action: string;
  scope_summary: string;
  repositories: string[];
  paths: string[];
  invalidation_conditions: string[];
}

export interface ReviewDraftItem {
  family_id: string;
  repository_id: string;
  revision_id: string;
  revision: number;
  content_digest: string;
  action: ReviewAction;
  effective_content: CandidateContent | null;
  note: string | null;
}

export interface ReviewDraft {
  organization_id: string;
  actor_id: string;
  product_id: string;
  version: number;
  items: ReviewDraftItem[];
  updated_at: string | null;
}

export interface CandidateInboxItem {
  family_id: string;
  repository_id: string;
  capture_request_ids: string[];
  revision_id: string;
  revision: number;
  content_digest: string;
  content: CandidateContent;
  review_state: CandidateReviewState;
  draft_action: ReviewAction | null;
  stale_draft: boolean;
}

export interface CandidateInbox {
  product_id: string;
  product_name: string;
  repositories: RepositoryView[];
  items: CandidateInboxItem[];
  draft: ReviewDraft;
}

export interface CaptureRequestView {
  request_id: string;
  repository_id: string;
  product_id: string;
  product_name: string;
  template_id: string;
  state: string;
  progress_code: string;
  candidate_revision_count: number | null;
  last_sequence: number;
  created_at: string;
  updated_at: string;
}

export interface ProgressEvent {
  request_id: string;
  sequence: number;
  state: string;
  code: string;
  occurred_at: string;
}
