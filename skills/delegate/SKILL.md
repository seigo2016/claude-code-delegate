---
name: delegate
description: Use when a piece of work is bounded enough to hand to an external CLI agent - reading and auditing artifacts, mapping a repository, applying an approved change, running checks, extracting facts from a source, or reviewing something adversarially - so this session keeps its context for judgement instead of spending it on the work itself.
---

# delegate

Hand `$ARGUMENTS` to an external worker, keep the handle, and end your turn. Do not wait, and do not poll.

## When not to delegate

Keep the work here when it needs live session state, when the decision itself is the deliverable, or when the task cannot be described without reproducing the context you are trying to protect. Mark those packets `"host_only": true` and they will be refused rather than sent.

The final call on scope, on what counts as done, and on anything a person will be held to stays with you.

## 1. Choose a role

Roles come from `.claude/delegate.toml`. A role says what capability the work needs; it never names a model.

| role | for |
|---|---|
| `artifact-auditor` | checking files, logs, data, and manifests against what was claimed |
| `repo-cartographer` | finding what exists, what is stale, and what relates to what |
| `bounded-implementer` | applying an approved change within a stated write scope |
| `verification-runner` | running tests, linters, builds, and reproduction steps |
| `consistency-auditor` | comparing two places that are supposed to agree |
| `adversarial-critic` | trying to refute a conclusion, a patch, or a plan |

## 2. Write the packet

One objective, one family of artifacts. Split anything larger: more than about eight paths to read, or more than three files to write, usually means two tasks.

```bash
cat > /tmp/packet.json <<'JSON'
{
  "objective": "Check that CHANGELOG.md lists every tag reachable from main.",
  "read": ["CHANGELOG.md"],
  "allowed_writes": [],
  "required_evidence": ["tags missing from the changelog", "tags listed but not in git"],
  "host_only": false
}
JSON

delegate submit --role artifact-auditor --title changelog-vs-tags --packet /tmp/packet.json
```

Role instructions, the model, the prohibitions and the result contract are added for you. Do not repeat them in the packet.

**A result holds at most five short strings per list.** That cap is the point: a worker that hands back forty lines has moved the reading back into this session. When the answer is genuinely a long list — an inventory, a full mapping, every occurrence of something — ask for it as a file and let the worker return the path:

```json
{
  "objective": "Write an inventory of every module under src/ to docs/inventory.md, one line each.",
  "allowed_writes": ["docs/inventory.md"],
  "required_evidence": ["module count", "the path written"]
}
```

Asking for "one line per module" as evidence fails the contract, and you find out after the work is done rather than before.

`bounded-implementer` will refuse an empty `allowed_writes`. That is deliberate: an implementer without a stated scope is an implementer with an unstated one.

The scope is checked, not merely requested: the repository is compared before and after, and a task that wrote outside its scope comes back `failed` with the offending paths. The comparison uses git, so it sees writes inside the work tree and nothing else. Outside it — a home directory, a system path, a network call — the scope is still only a request.

## 3. Stop

`submit` returns a handle immediately. Record the `task_id`, dispatch anything else that is ready, and end your turn. A hook wakes this session when the task finishes.

Do not call `status` to see how it is going. Waiting costs the context you delegated to save.

## 4. Collect once

```bash
delegate collect <task-id>
```

Read `status` on what comes back before you read `result`:

- `completed` — evidence you can use.
- `decision_needed` — the worker stopped on a judgement that is yours to make. Answer it here.
- `failed`, `timeout`, `orphaned`, `degraded` — **the work did not happen.** Say so, look at the artifact paths in the envelope, and decide what to do. Never present a failed task's partial output as a finding.

A timeout carries a `failure_class` saying where it stopped, and a worker that produced a usable-looking final message without delivering it has that message kept aside. It is kept aside on purpose: read it yourself before you trust it.

## Other commands

```bash
delegate status <task-id>    # explicit diagnosis only, never as a waiting loop
delegate cancel <task-id>    # a task that is wrong or runaway
delegate reconcile           # classify tasks whose worker is gone
```
