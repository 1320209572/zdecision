# Decision Registry

This subtree contains only formal decisions that a user explicitly reviewed
and approved for sharing.

Do not store raw Codex conversations, Candidate payloads, evidence excerpts,
rejected or edited Review content, publication approval, workspace snapshots,
credentials, or secrets here.

V1 stores this subtree in the zdecision repository on `main`; it does not use a
separate Registry branch. The root index lists products only. Each stable
`prod_...` directory owns its metadata, Decision-head index, and independently
versioned `dec_.../r0001.json` objects. Human product names never become path
components.
