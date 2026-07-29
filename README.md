# ZDecision

ZDecision lets Codex turn decisions from an existing development task into
small, reviewed project memory, then provide the relevant decisions to a new
task without copying the original conversation.

V1 is conversation-first: clone this repository, open it in Codex App, and
describe what you want in natural language—for example:

- “压缩任务 `<task-id>` 的决策。” uses the default 业务决策压缩模板.
- “用模板 ID `architecture` 处理任务 `<task-id>`。” selects that hypothetical
  `architecture` template only after you copy or install it locally.
- “审核刚才提取的候选决策。”
- 在 Codex 展示完整发布预览后，单独回复“确认发布”才会授权正式发布。
- “带上安恒项目现有决策，开始一个新的开发任务。”

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

The authoritative V1 design is [docs/architecture.md](docs/architecture.md).
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
documents and paths. Review acceptance does not publish: publication requires
a later user message whose complete trimmed content is exactly `确认发布`.

The implementation lives under `src/zdecision/`. Legacy execution paths are
not retained; extractor-v1 completed records remain display-only so existing
private Candidates can still be reviewed.
