# ZDecision

ZDecision turns decisions from ordinary Codex development into small, reviewed
project memory without copying the source conversation into shared storage.

The implemented monorepo workflow is:

```text
Update action
  -> one repository Capture group
  -> trusted local Git route plan
  -> product and concrete Shared leaf slices
  -> local extraction and reconciliation
  -> frozen leaf Candidate ownership
  -> leaf Candidate Inbox
  -> Review
  -> read-only Preview
  -> separate explicit publish
  -> isolated V1 Registry compatibility partition
```

After a completed and verified code-development boundary, the installed Plugin
may render **更新候选决策**. The exact same-task phrase is also available after
the registered-and-enabled repository gate. The card keeps exactly two Update
scopes: **当前 Session** and **所有有效 Session**. It never asks the user to pick
a product or Shared package; the local Agent routes frozen repository-relative
Git paths through the trusted catalog after repository and Session
authorization.

No Session ID, product selector, separate compression conversation, or Capture
CLI is part of the Plugin flow. Rendering the card does not authorize Capture;
only one of its scope buttons or the repository page Update action does.
Packet 1 ends at the Candidate Inbox. Web Review/publication is Packet 2, and
automatic Decision recall is Packet 3.

## Technical Demo startup

Create a new private configuration directory for the registered monorepo:

```bash
zdecision-central demo-config init \
  --repository-cwd /absolute/path/to/zstack-ui-next \
  --output-dir /absolute/path/to/new-config-directory

zdecision-central run \
  --database /absolute/path/to/central.sqlite3 \
  --config /absolute/path/to/new-config-directory/central.json \
  --registry-repository-root /absolute/path/to/zdecision-checkout \
  --host 127.0.0.1 \
  --port 8765

zdecision-agent service run \
  --config /absolute/path/to/new-config-directory/agent.json
```

The generated `central.json` owns the complete trusted catalog and route
versions. `agent.json` contains only the enabled repository identity and device
credentials. The Demo registers these independent product roots:

```text
packages/products/{cloud,idp,lifecycle,portal,redis,third-party-services,
zcf-installer,ziam,zmetis,zns,zstack-ai-studio,zstone,zsv}
```

It also registers only these concrete Shared leaves:

```text
packages/products/shared/{zcf-audit,zcf-license}
packages/shared/{design-x,theme}
packages/{design,form,table,hooks,auth,i18n,utils,zephyr}
```

`Shared` and its directory groups are navigation nodes. They aggregate counts
and expand to the listed leaves, but they cannot own Capture output, Review,
Preview, publication, or Registry files. Browser links use canonical
`/spaces/{decision_space_id}/...` routes.

## Templates and privacy

Templates are selected by stable ID; a template title is display metadata, not
an alias. To install another template, copy a template directory, assign its
stable ID, title, and revision, then edit its two policy files. The repository
bundles the high-precision `business` revision 2 template titled
**业务决策压缩模板**; `architecture` is only an example of another template a
user might install. extractor-v1 completed records remain display-only.

Raw Sessions, Prompts, model context, tool output, source, diffs, credentials,
and local absolute paths never leave the device. Central persistence receives
only typed request metadata, route/path digests, frozen leaf ownership,
Candidate revisions, and later Review/publication records.

## V1 Registry compatibility

Only reviewed formal Decisions enter Git. Each product or concrete Shared leaf
has a distinct internal `prod_<stable-id>` compatibility partition; the Shared
root has none:

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

The product authority is [docs/architecture.md](docs/architecture.md), and the
bounded operator acceptance is
[docs/demo-central-web.md](docs/demo-central-web.md). Repository instructions
for Codex are in [AGENTS.md](AGENTS.md).
