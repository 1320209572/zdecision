# ZDecision

ZDecision turns decisions from normal Codex development into small, reviewed
project memory, then recalls the relevant formal Decisions in later work
without copying the source conversation.

The current pre-Demo Plugin direction is page-triggered:

1. The installed Plugin observes Codex activity only in company-enabled Git
   repositories.
2. When a body of work is ready, the user clicks **更新候选决策** for that
   repository on the ZDecision page.
3. The local Agent selects changed Sessions, runs two-stage decision
   extraction, and uploads only structured Candidate decisions.
4. The user accepts or rejects Candidates and explicitly publishes the chosen
   batch.
5. The Plugin automatically recalls applicable formal Decisions in later Codex
   tasks.

No Session ID, separate compression conversation, or CLI command is part of
the Plugin product flow. The existing conversation and CLI paths remain useful
as internal diagnostics for the proven Capture domain.

V1 selects templates by stable ID. A template's title is display metadata, not
an alias. To add one, copy a template directory, assign its stable ID, title,
and revision, then edit its two policy files. The repository currently bundles
the high-precision `business` revision 2 template; `architecture` above is only
an example of a template the user might install. The bundled template keeps
durable product choices and business boundaries, not rediscoverable API details
or ordinary implementation correctness. Separate technical-contract templates
can be added later for those facts. When several signals express one underlying
product principle, `business` keeps one complete representative instead of
publishing each implementation fragment.

The product authority is [docs/architecture.md](docs/architecture.md). The
active Plugin contract is the
[on-demand Candidate refresh design](docs/superpowers/specs/2026-07-30-on-demand-candidate-refresh-design.md).
Repository instructions for Codex are in [AGENTS.md](AGENTS.md).

## V1 storage

Source code and formal decisions share this repository and its `main` branch.
Formal decisions are isolated under `decision-registry/`. Raw conversations,
candidate decisions, and private review state never enter that subtree.

Each product has its own formal partition. The root index points to product
metadata and that product's Decision index; every Decision remains an
independent revision document:

```text
decision-registry/
├── registry.json
└── products/
    └── prod_<stable-id>/
        ├── product.json
        ├── registry.json
        └── decisions/
            └── dec_<stable-id>/
                └── r0001.json
```

Review is private and batched. Preview is read-only and shows the exact formal
documents and paths. Review acceptance does not publish. The manual diagnostic
path requires a later user message whose complete trimmed content is exactly
`确认发布`; the Plugin page uses a separate explicit publication action bound to
the same frozen preview.

The implementation lives under `src/zdecision/`. Legacy execution paths are
not retained; extractor-v1 completed records remain display-only so existing
private Candidates can still be reviewed.
