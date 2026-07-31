# ZDecision

ZDecision turns decisions from normal Codex development into small, reviewed
project memory, then recalls the relevant formal Decisions in later work
without copying the source conversation.

The implemented pre-Demo **Packet 1** is page-triggered:

1. The installed Plugin observes Codex activity only in company-enabled Git
   repositories.
2. The user clicks **更新候选决策** for that repository on the ZDecision page.
3. The local Agent selects changed Sessions, runs two-stage decision
   extraction, and uploads only structured Candidate decisions.
4. Current Candidate revisions appear in the product-isolated Candidate Inbox.

No Session ID, separate compression conversation, or CLI command is part of
the Plugin product flow. Packet 1 ends at the Candidate Inbox. Web
Review/publication is Packet 2, and automatic Decision recall is Packet 3; they
are deliberately not simulated by the Packet 1 page.

For the local technical-loop operator, the internal startup boundary is:

```bash
zdecision-central demo-config init \
  --repository-cwd /absolute/path/to/repository \
  --product-name PRODUCT \
  --output-dir /absolute/path/to/new-config-directory

zdecision-central run \
  --database /absolute/path/to/central.sqlite3 \
  --config /absolute/path/to/new-config-directory/central.json \
  --host 127.0.0.1 \
  --port 8765

zdecision-agent service run \
  --config /absolute/path/to/new-config-directory/agent.json
```

The generated config files are private onboarding artifacts. The browser opens
`http://127.0.0.1:8765`; clicking **更新候选决策** is the only action that
authorizes model-based Candidate generation.

V1 selects templates by stable ID. A template's title is display metadata, not
an alias. To add one, copy a template directory, assign its stable ID, title,
and revision, then edit its two policy files. The repository currently bundles
the high-precision `business` revision 2 template, titled
**业务决策压缩模板**; `architecture` above is only an example of a template the
user might install. The bundled template keeps durable product choices and
business boundaries, not rediscoverable API details or ordinary implementation
correctness. Separate technical-contract templates can be added later for those
facts. When several signals express one underlying product principle,
`business` keeps one complete representative instead of publishing each
implementation fragment.

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

The proven Review and publication domain remains private and batched. Preview
is read-only and shows the exact formal documents and paths; acceptance does
not publish. Wiring that domain to the Plugin page belongs to Packet 2.

The implementation lives under `src/zdecision/`. Legacy execution paths are
not retained; extractor-v1 completed records remain display-only so existing
private Candidates can still be reviewed.
