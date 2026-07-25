# claude-code-delegate

Asynchronous, auditable delegation from Claude Code to external CLI agents.

[日本語版](README.ja.md)

Claude Code stays the place where things are decided. The work itself goes to
Codex, OpenCode, or a headless Claude: reading artifacts, mapping a repository,
applying an approved change, running checks. What comes back is a small piece of
evidence, not a transcript.

Nothing is polled. The session hands over a task, ends its turn, and is woken
when there is something to collect.

## Why

A long Claude Code session runs out of context, and it runs out on the wrong
things: log output, file dumps, directory listings, the same check repeated. The
judgement that needed the context gets squeezed by the work that did not.

Delegating that work is easy. Delegating it *safely* is the part this handles: a
worker that hangs, dies, returns nothing, or returns something that only looks
like an answer must not come back as success.

## Install

Requires Python 3.11+ and at least one of `codex`, `opencode`, or `claude` on
your PATH. Linux and macOS.

```bash
git clone https://github.com/seigo2016/claude-code-delegate
claude plugin marketplace add ./claude-code-delegate
claude plugin install delegate@claude-code-delegate --scope project
```

Installing changes nothing about your session: no agent is replaced, no tool is
removed, and no backend is enabled until you say so.

## Configure

Copy `examples/delegate.toml` to `.claude/delegate.toml` in your repository, fill
in models you actually have, and enable one worker.

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

A role asks for a *capability*, never a model, so the same roles work whichever
backend you configured. If the chosen worker has no model for the capability a
role needs, the task is refused before anything starts.

| adapter | how it is driven | how it answers |
|---|---|---|
| `codex` | `codex exec --json` | output file, plus a JSON schema it is held to |
| `opencode` | `opencode run --format json` | its last text part |
| `claude` | `claude -p --output-format stream-json` | its closing result event |

## Use

From a Claude Code session the `/delegate` skill covers the whole loop. Directly:

```bash
delegate submit --role artifact-auditor --title changelog-vs-tags --packet packet.json
# → {"task_id": "…", "status": "starting", "worker": "claude", "model": "sonnet"}

delegate collect <task-id>
```

A packet says only what this task adds. Role instructions, the model, the
prohibitions and the result contract are supplied for you.

```json
{
  "objective": "Check that CHANGELOG.md lists every tag reachable from main.",
  "read": ["CHANGELOG.md"],
  "allowed_writes": [],
  "required_evidence": ["tags missing from the changelog", "tags listed but not in git"],
  "host_only": false
}
```

Some work must not leave the session: anything needing live session state, and
any judgement a person will be held to. Mark it `"host_only": true` and it is
refused rather than sent.

## What it will not do

A result you receive is a result you can act on.

| what happened | what you get |
|---|---|
| a tool call never returned | `timeout`, `failure_class: tool_stall` |
| the worker finished but never handed back a result | `timeout`, `failure_class: finalization_timeout`, its last message kept aside |
| the worker exited non-zero | `failed`, `nonzero_exit` |
| the worker produced nothing | `failed`, `empty_result` |
| the answer did not meet the contract | `failed`, `invalid_result`, with the reasons |
| the worker wrote outside its declared scope | `failed`, `write_scope_violation`, with the paths |
| the machine rebooted under it | `orphaned`, or `degraded` if a result survived |

A message that merely looks like a valid answer is never promoted to one. It is
kept beside the task so you can read it and decide.

A result holds at most five strings of 300 characters per list. That cap is the
product: an answer that cannot be read at a glance has moved the cost back into
the session that delegated it. When the answer really is a long list, have the
worker write a file and return its path.

## Limits

Stated plainly, because a boundary you believe in but do not have is worse than
none.

- **Write scope is checked with git, so it sees the work tree and nothing else.**
  A worker that writes to a home directory, a system path, or over the network is
  outside what this can observe. A repository without git is not checked at all,
  and the task says so.
- **There is no sandbox.** Workers run with whatever their own CLI grants them.
- **Adapters follow three CLIs that change.** The event shapes were recorded from
  real runs; a backend release can move them.
- **`fcntl` means Linux and macOS.** Windows works through WSL.

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
