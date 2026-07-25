# claude-code-delegate

[![ci](https://github.com/seigo2016/claude-code-delegate/actions/workflows/ci.yml/badge.svg)](https://github.com/seigo2016/claude-code-delegate/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[English](README.md) | [日本語](README.ja.md)

**Stop spending your context on work you could have sent away.**

claude-code-delegate hands bounded work from Claude Code to Codex, OpenCode, or a
headless Claude, and brings back a few lines of evidence instead of a transcript.
Nothing is polled: the session hands over a task, ends its turn, and is woken
when there is something to collect.

## Why

A long Claude Code session runs out of context, and it runs out on the wrong
things: log output, file dumps, directory listings, the same check repeated. The
judgement that needed the context gets squeezed by the work that did not.

Sending that work away is easy. The hard part is what happens when it goes wrong.
A worker that hangs, dies, returns nothing, or returns something that only looks
like an answer must not come back as success — otherwise you have traded context
for doubt.

## Try it in five minutes

```bash
claude plugin marketplace add seigo2016/claude-code-delegate
claude plugin install delegate@claude-code-delegate --scope project

cp examples/delegate.toml .claude/delegate.toml
# edit: set enabled = true on one worker, and fill in its models

cat > /tmp/packet.json <<'JSON'
{
  "objective": "Check that CHANGELOG.md lists every tag reachable from main.",
  "read": ["CHANGELOG.md"],
  "allowed_writes": [],
  "required_evidence": ["tags missing from the changelog", "tags listed but not in git"],
  "host_only": false
}
JSON

delegate submit --role artifact-auditor --title changelog --packet /tmp/packet.json
# → {"task_id": "…", "status": "starting", "worker": "claude", "model": "sonnet"}

delegate collect <task-id>
```

Inside a Claude Code session the `/delegate` skill covers the same loop, and a
hook wakes the session when the task finishes.

## Requirements

- Python 3.11 or newer on PATH. The settings reader uses `tomllib`, which arrived
  in 3.11; macOS still ships 3.9 as `python3`, so a newer one must be installed.
  There are no other dependencies.
- At least one of `codex`, `opencode`, or `claude`.
- Linux or macOS. Windows works through WSL.

Installing the plugin replaces no agent, removes no tool, and enables no backend
until you declare one.

## Configuration

```toml
default_worker = "claude"

[workers.claude]
adapter = "claude"
enabled = true
models = { light = "haiku", standard = "sonnet", frontier = "opus" }

[roles.artifact-auditor]
capability = "standard"
effort = "high"
task_class = "review"
```

A role asks for a capability, never a model, so the same roles work whichever
backend is configured. If the chosen worker has no model for the capability a
role needs, the task is refused before anything starts.

| adapter | driven by | answers with |
|---|---|---|
| `codex` | `codex exec --json` | an output file, plus a JSON schema it is held to |
| `opencode` | `opencode run --format json` | its last text part |
| `claude` | `claude -p --output-format stream-json` | its closing result event |

Roles that ship in the example: `artifact-auditor`, `repo-cartographer`,
`bounded-implementer`, `verification-runner`, `consistency-auditor`,
`adversarial-critic`.

## What you get back

| what happened | what you get |
|---|---|
| a tool call never returned | `timeout`, `failure_class: tool_stall` |
| the worker finished but never handed back a result | `timeout`, `finalization_timeout`, its last message kept aside |
| the worker exited non-zero | `failed`, `nonzero_exit` |
| the worker produced nothing | `failed`, `empty_result` |
| the answer did not meet the contract | `failed`, `invalid_result`, with the reasons |
| the worker wrote outside its declared scope | `failed`, `write_scope_violation`, with the paths |
| the machine rebooted under it | `orphaned`, or `degraded` if a result survived |

A message that merely looks like a valid answer is never promoted to one. It is
kept beside the task so you can read it and decide.

A result holds at most five strings of 300 characters per list. An answer that
cannot be read at a glance has moved the cost back into the session that
delegated it; when the answer really is a long list, have the worker write a file
and return its path.

Work that must not leave the session — anything needing live session state, or a
judgement a person will be held to — is marked `"host_only": true` and refused
rather than sent.

## Commands

| command | for |
|---|---|
| `delegate submit --role R --title T --packet P` | start a worker and get a handle |
| `delegate collect <task-id>` | take delivery, once |
| `delegate status <task-id>` | explicit diagnosis, never a waiting loop |
| `delegate cancel <task-id>` | stop a task that is wrong or runaway |
| `delegate reconcile` | classify tasks whose worker is gone |
| `delegate watch` | what the hook runs; waits for something to collect |

## Limits

- **Write scope is checked with git, so it sees the work tree and nothing else.**
  A worker that writes to a home directory, a system path, or over the network is
  outside what this can observe. A repository without git is not checked at all,
  and the task says so.
- **There is no sandbox.** Workers run with whatever their own CLI grants them.
- **Adapters follow three CLIs that change.** The event shapes were recorded from
  real runs; a backend release can move them.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ty check
```

The tests drive a stand-in worker that can be made to hang, die, return nothing,
or return something that only looks like an answer. The handling above is
therefore tested rather than asserted.

## License

MIT
