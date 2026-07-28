# ZDecision

ZDecision lets Codex turn decisions from an existing development task into
small, reviewed project memory, then provide the relevant decisions to a new
task without copying the original conversation.

V1 is conversation-first: clone this repository, open it in Codex App, and
describe what you want in natural language—for example:

- “压缩任务 `<task-id>` 中已经确认的决策。”
- “审核刚才提取的候选决策。”
- “带上安恒项目现有决策，开始一个新的开发任务。”

The authoritative V1 design is [docs/architecture.md](docs/architecture.md).
Repository instructions for Codex are in [AGENTS.md](AGENTS.md).

## V1 storage

Source code and formal decisions share this repository and its `main` branch.
Formal decisions are isolated under `decision-registry/`. Raw conversations,
candidate decisions, and private review state never enter that subtree.

The implementation is being rebuilt from a clean skeleton under
`src/zdecision/`; no legacy architecture or compatibility layer is retained.
