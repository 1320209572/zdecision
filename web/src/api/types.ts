export type RegistryState = "available" | "unavailable";
export type PublicationState =
  | "confirmed"
  | "committed_pending_push"
  | "completed"
  | "ambiguous";

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

export type DecisionSpaceKind = "product" | "shared_unit";

export interface DecisionSpaceRef {
  decision_space_id: string;
  kind: DecisionSpaceKind;
  display_name: string;
  breadcrumb: string[];
  source_root: string;
  package_name: string | null;
  asset_type: string | null;
}

export interface DecisionSpaceSummary extends DecisionSpaceRef {
  repository_ids: string[];
  pending_candidate_count: number;
  active_decision_count: number | null;
  last_activity_at: string | null;
}

export interface CatalogNode {
  node_id: string;
  kind: "catalog_group" | DecisionSpaceKind;
  display_name: string;
  breadcrumb: string[];
  pending_candidate_count: number;
  active_decision_count: number | null;
  last_activity_at: string | null;
  space: DecisionSpaceSummary | null;
  children: CatalogNode[];
}

export interface RepositorySpacesView {
  repository_id: string;
  spaces: DecisionSpaceSummary[];
}

export interface PublicationSummary {
  decision_space_id: string;
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

export interface PublicationDetail extends PublicationSummary {
  decision_ids: string[];
}

export interface PublicationHistory {
  items: PublicationDetail[];
  total: number;
  limit: number;
  offset: number;
}

export interface Dashboard {
  metrics: DashboardMetrics;
  registry: RegistryStatus;
  products: DecisionSpaceSummary[];
  shared_tree: CatalogNode | null;
  recent_publications: PublicationSummary[];
}

export interface DecisionListItem {
  decision_space_id: string;
  product_id: string;
  product_name: string;
  decision_id: string;
  revision: number;
  lifecycle: "active";
  claim: string;
  future_action: string;
  scope_summary: string;
  repositories: string[];
  paths: string[];
  published_at: string | null;
  publication_id: string | null;
  commit_sha: string | null;
}

export interface DecisionListView {
  registry_state: RegistryState;
  registry_commit: string | null;
  items: DecisionListItem[] | null;
  total: number | null;
}

export interface DecisionDetail {
  decision_space_id: string;
  format: "zdecision-decision/v1";
  schema_version: 1;
  decision_id: string;
  product_id: string;
  product_name: string;
  revision: 1;
  lifecycle: "active";
  claim: string;
  future_action: string;
  scope: {
    summary: string;
    repositories: string[];
    paths: string[];
  };
  invalidation_conditions: string[];
  supersedes: unknown[];
  variant_of: unknown[];
  source: { thread_id: string; turn_id: string };
  review_approval: {
    actor: "user";
    thread_id: string;
    turn_id: string;
    recorded_at: string;
  };
  publication_preview_id: string;
  canonical_json: string;
  registry_commit: string;
  publication_id: string | null;
  published_at: string | null;
  commit_sha: string | null;
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
  decision_space_id: string;
  version: number;
  items: ReviewDraftItem[];
  updated_at: string | null;
}

export interface ReviewSubmissionItem {
  review_id: string;
  family_id: string;
  publication_candidate_id: string;
  repository_id: string;
  revision_id: string;
  revision: number;
  content_digest: string;
  action: ReviewAction;
}

export interface ReviewSubmissionResult {
  review_batch_id: string;
  items: ReviewSubmissionItem[];
  preview_eligible: boolean;
  remaining_pending_count: number;
  draft_version: number;
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
  space: DecisionSpaceRef;
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

export type PreviewPublishability =
  | "publishable"
  | "stale"
  | "registry_unavailable";

export interface PublicationFile {
  path: string;
  content: string;
  sha256: string;
}

export interface DecisionPreview {
  path: string;
  sha256: string;
  canonical_json: string;
  format: "zdecision-decision/v1";
  schema_version: 1;
  decision_id: string;
  product_id: string;
  product_name: string;
  revision: 1;
  lifecycle: "active";
  claim: string;
  future_action: string;
  scope: {
    summary: string;
    repositories: string[];
    paths: string[];
  };
  invalidation_conditions: string[];
  supersedes: unknown[];
  variant_of: unknown[];
  source: { thread_id: string; turn_id: string };
  review_approval: {
    actor: string;
    thread_id: string;
    turn_id: string;
    recorded_at: string;
  };
  publication_preview_id: string;
}

export interface PublicationPreview {
  record_version: 1;
  preview_id: string;
  content_digest: string;
  state: "previewed";
  created_at: string;
  review_batch_id: string;
  review_ids: string[];
  candidate_ids: string[];
  decision_ids: string[];
  decision_space_id: string;
  product_id: string;
  product_name: string;
  base_commit: string;
  base_registry_digests: Record<string, string>;
  display_documents: PublicationFile[];
  changed_files: PublicationFile[];
  commit_message: string;
  publishability: PreviewPublishability;
  publication_id: string | null;
  decisions: DecisionPreview[];
}
