# claude-code-delegate

[![ci](https://github.com/seigo2016/claude-code-delegate/actions/workflows/ci.yml/badge.svg)](https://github.com/seigo2016/claude-code-delegate/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[English](README.md) | [日本語](README.ja.md)

**Stop spending your context on work you could have sent away.**

claude-code-delegate hands bounded work from Claude Code to Codex, OpenCode, or a
headless Claude. What comes back is unverified until it is checked: the worker
finished rather than stalled or vanished, the result meets a fixed contract, and
nothing was written outside the scope the task declared.

The Claude session does not poll for progress. It hands the task over, ends its
turn, and is woken only when there is something to collect.

## Why

In a long session, context goes to log output, file dumps, directory listings and
repeated checks, rather than to the judgement that needed it.

A worker can hang, die, return nothing, or return something that only looks like
a result. None of those comes back as success.

## Try it

```bash
claude plugin marketplace add seigo2016/claude-code-delegate
claude plugin install delegate@claude-code-delegate --scope project
```

Then write `.claude/delegate.toml` in your project, naming one backend you have:

```toml
default_worker = "claude"

[workers.claude]
adapter = "claude"
enabled = true
light = { model = "haiku", effort = "medium" }
standard = { model = "sonnet", effort = "high" }
frontier = { model = "opus", effort = "xhigh" }

[roles.artifact-auditor]
level = "standard"
```

`examples/delegate.toml` in this repository carries all six roles and all three
backends, switched off.

Then, in a Claude Code session:

> /delegate check that CHANGELOG.md lists every tag reachable from main

Claude describes the task, hands it to a worker, and ends its turn. A hook wakes
the session when the worker finishes, and Claude collects the result.

## Requirements

- Python 3.11+ on PATH, for `tomllib`. macOS ships 3.9 as `python3`, so install a
  newer one (`brew install python@3.12`). No other dependencies.
- At least one of `codex`, `opencode`, or `claude`.
- Linux or macOS. Windows works through WSL.

Installing the plugin replaces no agent, removes no tool, and enables no backend
until you declare one.

## Configuration

A role asks for a level, never a model, and each worker says what that level
means for itself. The same roles therefore work whichever backend is configured,
and `--worker` picks one per task. A role asking for a level the chosen worker
does not define is refused before anything starts.

| adapter | driven by | answers with |
|---|---|---|
| `codex` | `codex exec --json` | an output file, plus a JSON schema it is held to |
| `opencode` | `opencode run --format json` | its last text part |
| `claude` | `claude -p --output-format stream-json` | its closing result event |

Roles that ship in the example: `artifact-auditor`, `repo-cartographer`,
`bounded-implementer`, `verification-runner`, `consistency-auditor`,
`adversarial-critic`.

Claude writes the task itself. It says what to do, what may be read, what may be
written, and what evidence to bring back:

```json
{
  "objective": "Check that CHANGELOG.md lists every tag reachable from main.",
  "read": ["CHANGELOG.md"],
  "allowed_writes": [],
  "required_evidence": ["tags missing from the changelog", "tags listed but not in git"],
  "host_only": false
}
```

## What you get back

| what happened | what you get |
|---|---|
| a tool call never returned | `timeout`, `failure_class: tool_stall` |
| the worker finished but never handed back a result | `timeout`, `finalization_timeout`, its last message kept aside |
| the worker exited non-zero | `failed`, `nonzero_exit` |
| the worker produced nothing | `failed`, `empty_result` |
| the result did not meet the contract | `failed`, `invalid_result`, with the reasons |
| the worker wrote outside its declared scope | `failed`, `write_scope_violation`, with the paths |
| the backend could not be started | `failed`, `launch_error` |
| you cancelled it | `cancelled` |
| the machine went down under it | `orphaned`, or `degraded` if a result survived |

A timeout carries the reading of where it stopped: `tool_stall`,
`finalization_timeout`, `runtime_stall`, `event_stream_stall`, or
`wall_clock_timeout`.

A message that looks like a valid result is never promoted to one. It is kept
beside the task.

A result holds at most five strings of 300 characters per list. For a longer one,
have the worker write a file and return its path.

`required_evidence` is instruction to the worker, and appears in its prompt. The
contract checks that the evidence fields are present, well formed and within those
limits. It does not check that they answer what was asked, and it cannot tell
whether they are true.

Claude marks a task `"host_only": true` when it needs live session state, or when
the judgement is one a person must make rather than approve after the fact. Submit
then fails closed instead of sending it. This is an explicit marker written into
the packet, not an automatic classifier.

## Commands

The skill and the hooks run these. You need them yourself only to diagnose
something.

| command | for |
|---|---|
| `delegate submit --role R --title T --packet P` | start a worker and get a handle |
| `delegate collect <task-id>` | take delivery, once |
| `delegate status <task-id>` | explicit diagnosis, never a waiting loop |
| `delegate cancel <task-id>` | stop a task that is wrong or runaway |
| `delegate reconcile` | classify tasks whose worker is gone |
| `delegate watch` | what the hook runs; waits for something to collect |

`submit` takes `--worker <name>` to override the backend for one task. Every
command takes `--project-root`, which the hooks pass because they run from the
installed plugin rather than from the repository.

## What is and is not checked

Detected:

- a worker that never started, stalled, died, or lost its supervisor
- a result that is absent, malformed, or missing required fields
- a final message that never became a delivered result
- writes outside the declared scope, within the git work tree

Not detected:

- writes outside the work tree: a home directory, a system path
- writes to files `.gitignore` excludes
- network effects, credential access, or anything git does not represent
- a result that satisfies the contract and is still wrong

What a sandbox stops before the fact depends on the backend. A task that declared
no writes gets the narrowest posture its backend has:

| backend | a task that may not write | enforced by |
|---|---|---|
| codex | `--sandbox read-only`, network closed | the operating system |
| claude | `auto`, minus the editing tools | a classifier, and tools withheld |
| opencode | no per-run flag to set | the prompt alone |

Only codex refuses in the kernel: an attempt to write came back `Read-only file
system`. Claude Code is not handed the editing tools, and its classifier turned
down every way round them that was tried: a shell redirect, `tee`, and a Python
one-liner. That is a judgement made each time rather than a boundary. opencode was
measured writing a file and running a shell command with no permission flag passed
at all, so a read-only task on that backend is read-only by request.

A role that runs tests or builds gets a writable filesystem even when it may not
change the repository, because build tools write caches under the home directory.
Nothing then stops such a run from touching the repository; the scope check above
catches it afterwards.

The adapters follow three CLIs whose event shapes were recorded from real runs,
and a backend release can move them.

## Not yet

Three things were left out rather than overlooked.

**Making delegation compulsory.** A `PreToolUse` hook can refuse a tool call, so a
setting could turn the skill from something Claude may reach for into something it
has to. Left out for now: Claude was measured delegating a large reading job
unprompted and doing a two-file check itself, and a refusal that is walked around
by doing the same work another way is not enforcement.

**A read-only posture for opencode.** It has no per-run permission flag, so a task
that declared no writes is asked there rather than stopped.

**Isolation.** Workers run in the work tree you are working in. A worktree each
would contain them, but `bounded-implementer` exists to change that tree, so the
containment would have to be undone to be useful.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ty check
```

Tests drive a stand-in worker that can be made to fail on demand. They cover
stalled and dead workers, a backend that cannot be launched, empty and malformed
results, a finished turn that never handed one back, writes outside the declared
scope, orphan and degraded reconciliation, and collecting a result exactly once.

## License

MIT
