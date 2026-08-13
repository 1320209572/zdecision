# Recall Demo Provider leadership rehearsal

This runbook proves one bounded, local leadership Demo for `zstack-ui-next`,
`third-party-services`, and Decision Space
`prod_3e6e73b8defbfee89ce7bf26e739b1dc`. It does not prove production Gate B/C,
production distribution, or representative retrieval quality. Recall stays
offline and the rehearsal stops before product-code mutation.

## 1. Install the optional dependencies

From `/Users/zhaohuiying/Desktop/Zstack-repos/zdecision`:

```bash
.venv/bin/python -m pip install -e '.[recall-demo]'
```

## 2. Prepare the two pinned models once

The profile pins `intfloat/multilingual-e5-small` at
`614241f622f53c4eeff9890bdc4f31cfecc418b3` and
`BAAI/bge-reranker-base` at
`2cfc18c9415c912f9d8155881c133215df768a70`.

```bash
mkdir -p /Users/zhaohuiying/.local/share/zdecision-recall-demo/model-state
mkdir -p /Users/zhaohuiying/.cache/huggingface/hub
zdecision-recall-demo prepare-models \
  --profile /Users/zhaohuiying/Desktop/Zstack-repos/zdecision/src/zdecision/recall/demo/demo-profile.json \
  --state-dir /Users/zhaohuiying/.local/share/zdecision-recall-demo/model-state \
  --model-cache /Users/zhaohuiying/.cache/huggingface/hub
zdecision-recall-demo model-status \
  --profile /Users/zhaohuiying/Desktop/Zstack-repos/zdecision/src/zdecision/recall/demo/demo-profile.json \
  --state-dir /Users/zhaohuiying/.local/share/zdecision-recall-demo/model-state
```

Complete this preparation while model downloads are permitted. Every Recall
run after preparation must be offline.

## 3. Generate the external signing and trust keys

The following creates raw Ed25519 keys in a Demo-only directory outside Git.
Both files are owner-only; the private key never enters a bundle.

```bash
mkdir -p /Users/zhaohuiying/.local/share/zdecision-recall-demo/keys
chmod 700 /Users/zhaohuiying/.local/share/zdecision-recall-demo/keys
test ! -e /Users/zhaohuiying/.local/share/zdecision-recall-demo/keys/demo-private.key
test ! -e /Users/zhaohuiying/.local/share/zdecision-recall-demo/keys/demo-trust-root.pub
.venv/bin/python -c 'from pathlib import Path; from cryptography.hazmat.primitives import serialization; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; root=Path("/Users/zhaohuiying/.local/share/zdecision-recall-demo/keys"); key=Ed25519PrivateKey.generate(); (root/"demo-private.key").write_bytes(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())); (root/"demo-trust-root.pub").write_bytes(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))'
chmod 600 \
  /Users/zhaohuiying/.local/share/zdecision-recall-demo/keys/demo-private.key \
  /Users/zhaohuiying/.local/share/zdecision-recall-demo/keys/demo-trust-root.pub
```

Stop if either file already exists; do not overwrite an existing Demo trust
root or private key.

## 4. Configure the bounded provider

Create the bundle state directory and configure exactly the Registry product
root for `prod_3e6e73b8defbfee89ce7bf26e739b1dc`:

```bash
mkdir -p /Users/zhaohuiying/.local/share/zdecision-recall-demo/bundles
chmod 700 /Users/zhaohuiying/.local/share/zdecision-recall-demo/bundles
zdecision-agent recall-demo configure \
  --registry-product-root /Users/zhaohuiying/Desktop/Zstack-repos/zdecision/decision-registry/products/prod_3e6e73b8defbfee89ce7bf26e739b1dc \
  --profile /Users/zhaohuiying/Desktop/Zstack-repos/zdecision/src/zdecision/recall/demo/demo-profile.json \
  --model-state-root /Users/zhaohuiying/.local/share/zdecision-recall-demo/model-state \
  --trust-root /Users/zhaohuiying/.local/share/zdecision-recall-demo/keys/demo-trust-root.pub \
  --bundle-state-root /Users/zhaohuiying/.local/share/zdecision-recall-demo/bundles \
  --signing-private-key /Users/zhaohuiying/.local/share/zdecision-recall-demo/keys/demo-private.key \
  --signing-key-id demo-leadership-v1
zdecision-agent recall-demo status
```

The owner-only configuration is created at
`/Users/zhaohuiying/Library/Application Support/ZDecision/agent/recall-demo.json`.
`configure` intentionally refuses to overwrite it.

## 5. Seed immutable generation 1

Publish the current Registry commit exactly once through the production Demo
publisher. The second command must report `current_generation` as `1`.

```bash
.venv/bin/python -c 'import os, subprocess; from zdecision.recall.demo.config import load_demo_recall_config, recall_demo_config_path; from zdecision.recall.demo.publication import DemoBundlePublisher; repository="/Users/zhaohuiying/Desktop/Zstack-repos/zdecision"; commit=subprocess.run(("git","-C",repository,"rev-parse","HEAD"),check=True,capture_output=True,text=True).stdout.strip(); config=load_demo_recall_config(recall_demo_config_path(os.environ)); DemoBundlePublisher(config.publisher).refresh(commit)'
zdecision-agent recall-demo status
```

Do not rerun this command with a different Registry commit immediately before
the rehearsal; the next Central publication must be the N to N+1 transition.

## 6. Start Central and the existing Agent service

Create the ordinary loopback Central/Agent configuration once:

```bash
zdecision-central demo-config init \
  --repository-cwd /Users/zhaohuiying/Desktop/Zstack-repos/zstack-ui-next \
  --product-name third-party-services \
  --output-dir /Users/zhaohuiying/.local/share/zdecision-recall-demo/central-config
```

Start Central in one terminal:

```bash
cd /Users/zhaohuiying/Desktop/Zstack-repos/zdecision
zdecision-central run \
  --database /Users/zhaohuiying/.local/share/zdecision-recall-demo/central-config/central.sqlite3 \
  --config /Users/zhaohuiying/.local/share/zdecision-recall-demo/central-config/central.json \
  --registry-repository-root /Users/zhaohuiying/Desktop/Zstack-repos/zdecision \
  --host 127.0.0.1 \
  --port 8765
```

Start the existing Agent service in a second terminal:

```bash
cd /Users/zhaohuiying/Desktop/Zstack-repos/zdecision
zdecision-agent service run \
  --config /Users/zhaohuiying/.local/share/zdecision-recall-demo/central-config/agent.json
```

## 7. Reload and trust the local Plugin

Reinstall or reload the local marketplace Plugin from
`/Users/zhaohuiying/Desktop/Zstack-repos/zdecision/plugins/zdecision`. If Codex
reports that the bundle hash changed, inspect and trust exactly the eight Hook
entries in `hooks/hooks.json`: `SessionStart`, `UserPromptSubmit`, `PreCompact`,
`PostCompact`, `PostToolUse`, `Stop`, `SessionEnd`, and `PreToolUse`. Do not
approve a different command, matcher, or Plugin root.

## 8. Leadership flow

1. Start with a **pre-publication Recall attempt**. Open a new Codex task whose
   working directory is
   `/Users/zhaohuiying/Desktop/Zstack-repos/zstack-ui-next` and explicitly
   select **ZDecision**.
2. State that the target product is `third-party-services` and the relevant
   path is
   `packages/products/third-party-services/apps/security-services`.
3. Click the Recall confirmation card and keep the App attachment. Verify the
   card reaches `host_delivered`, then inspect the complete frozen handoff and
   verify the prepared target new Decision ID prefix is absent. Record only the
   recalled Decision count and the bounded absent result; do not record
   Decision text.
4. **Stop this pre-publication task** without sending the next native message,
   classifying or applying the handoff, or invoking any mutation tool. Close
   the task; do not continue it after publication. Confirm
   `git status --short` in `zstack-ui-next` is unchanged.
5. Run the bounded status command and record the selected pre-publication
   generation as N plus its generation digest prefix:

   ```bash
   zdecision-agent recall-demo status
   ```

6. In an existing changed `zstack-ui-next` Codex task, select **ZDecision
   Candidate refresh** and send the exact native message `更新候选决策`.
7. Click the Candidate card's shipped **打开决策中心** control exactly once.
   Observe `已请求使用默认浏览器打开决策中心`, then verify the default browser
   opens Decision Center for the expected repository and product. Do not copy
   a Session ID or start publication from Candidate text.
8. In Decision Center, review and explicitly publish the
   `security-services` Candidate.
9. Verify the Decision Center UI reports only the completed publication state
   **发布完成** and record the publication commit prefix. Do not treat this UI
   state as evidence of Demo refresh.
10. Run the exact bounded status command after publication:

    ```bash
    zdecision-agent recall-demo status
    ```

11. Verify `current_generation` is exactly N+1 and
    `current_digest_prefix` differs from the digest prefix recorded for N.
    Record only the N+1 generation number and digest prefix. Stop the rehearsal
    if either value fails to advance.
12. **Open a new Codex task** only after the N to N+1 status check. Its working
    directory must be
    `/Users/zhaohuiying/Desktop/Zstack-repos/zstack-ui-next`. Explicitly select
    **ZDecision**.
13. State that the target product is `third-party-services` and the relevant
   path is
   `packages/products/third-party-services/apps/security-services`.
14. Click the Recall confirmation card. Verify its visible state changes from
   `pending_confirmation` toward delivery only after the trusted click.
15. Keep the App attachment and send the next native message. Do not reopen the
   card or invent any delivery, Session, Turn, or gate identifier.
16. Verify the complete handoff contains the newly published Decision ID prefix,
   every recalled Decision is classified exactly once, and application reaches
   `application_committed`. Verify the mutation guard is released.
17. Stop before any code modification. Confirm `git status --short` in
    `zstack-ui-next` is byte-for-byte unchanged from its pre-rehearsal output.

For the acceptance note, record only:

- pre-publication recalled Decision count and target new Decision ID prefix
  absent;
- pre-publication selected generation N and digest prefix;
- Decision Center publication state `发布完成` and publication commit prefix;
- post-publication selected generation N+1 and changed digest prefix;
- Recall card states `pending_confirmation` then `host_delivered`;
- recalled Decision count and whether the new Decision ID prefix is present;
- application state `application_committed` and mutation guard released;
- no code changed and no network was used during Recall.

Never record full Decisions, private paths, keys, Session/Turn IDs, raw database
rows, or model scores.

## 9. Failure demonstration

After the successful flow, temporarily rename only the exact owner-only config:

```bash
mv '/Users/zhaohuiying/Library/Application Support/ZDecision/agent/recall-demo.json' \
  '/Users/zhaohuiying/Library/Application Support/ZDecision/agent/recall-demo.json.disabled'
```

Reload the Agent/Plugin, open another new `zstack-ui-next` task, explicitly
select ZDecision, provide the same product/path intent, and attempt to open the
Recall card. Verify the bounded result is `recall_not_ready` and that no
Decision content is fabricated. Restore the exact file afterward:

```bash
mv '/Users/zhaohuiying/Library/Application Support/ZDecision/agent/recall-demo.json.disabled' \
  '/Users/zhaohuiying/Library/Application Support/ZDecision/agent/recall-demo.json'
```

## 10. Rollback

Stop Central and the Agent service. Restore the prior local ZDecision Plugin
installation in Codex. Inspect each exact Demo path before removal, then remove
only these paths created by this runbook:

```bash
rm -f -- '/Users/zhaohuiying/Library/Application Support/ZDecision/agent/recall-demo.json'
rm -rf -- /Users/zhaohuiying/.local/share/zdecision-recall-demo/bundles
rm -rf -- /Users/zhaohuiying/.local/share/zdecision-recall-demo/model-state
rm -rf -- /Users/zhaohuiying/.local/share/zdecision-recall-demo/keys
rm -rf -- /Users/zhaohuiying/.local/share/zdecision-recall-demo/central-config
```

Never delete `/Users/zhaohuiying/Library/Application Support/ZDecision`,
`/Users/zhaohuiying/.cache/huggingface`, a repository root, or any other broad
state root.
