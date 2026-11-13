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
claude plugin install delegate@claude-code-delegate --scope user
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
forbids_allowed_writes = true
repo_local_reads = true
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

Set top-level `temp_root = "/absolute/path"` in `.claude/delegate.toml` to place task temporary directories there; the default is the delegate cache directory under `tmp`.

A role asks for a level, never a model, and each worker says what that level
means for itself. A role may also name `preferred_worker`. Worker selection is,
in order: `--worker`, the role's `preferred_worker`, then `default_worker`. A
role asking for a level the chosen worker does not define is refused before
anything starts.

To compare two workers on one role, give it `compare_workers = ["codex",
"claude"]` (exactly two distinct names of enabled workers, checked when the
settings load). Without `--worker`, a task takes the arm that a stable hash of its
new task id picks (the first byte of its SHA-256, modulo 2), so submits spread over
the two arms without any counting or locking. If that arm cannot be planned or fails
a submit-time check (a missing binary, say) or is unhealthy (below), the other arm is
used; the submit is refused only when neither can run, with the first arm's error.
`state.json` records `route_arm` (the worker that actually runs) and `route_policy`
(`"compare"`, `"explicit"` for `--worker`, which also wins over the comparison and
records no `route_arm`, or null). The comparison replaces `preferred_worker` for that
role. An identical task that is still running is returned instead of started again,
before the health check, but only on the same arm: the two arms of a comparison are
two runs.

An optional top-level `[common]` table with `instructions = """..."""` holds text
that goes into every task's prompt, once, under "Instructions common to all roles"
and before the role's own `instructions`.

A role may limit escalation with `allowed_escalation_reasons`. When it does,
the packet must give an `escalation_reason` from that list. A role with
`requires_explicit_user_direction = true` also requires
`"user_directed": true` in the packet. Both declarations are recorded with the
task for audit; `user_directed` records the caller's declaration and does not
prove that a person gave the instruction.

| adapter | driven by | answers with |
|---|---|---|
| `codex` | `codex exec --json` | an output file, plus a JSON schema it is held to |
| `opencode` | `opencode run --format json` | its last text part |
| `claude` | `claude -p --output-format stream-json` | its closing result event |

For `codex`, delegate redirects `CODEX_HOME` to a dedicated cache directory
(`$XDG_CACHE_HOME/delegate/codex-home` or `~/.cache/delegate/codex-home`) with a
symlink to `~/.codex/auth.json` and a minimal `config.toml` without `AGENTS.md`.
This keeps interactive instructions from leaking into delegated runs. Set
`isolate_home = false` under `[workers.codex]` to use the unisolated home instead.
For tasks with allowed writes, an incomplete Codex result resumes the same session
up to twice; set `resume_limit` under `[workers.codex]` to change that limit (zero
disables resumes). Identical consecutive blocker lists stop retries. The task's
`state.json` records `resume_count` and `resume_results` for each attempt.

Codex fast mode is opt-in and defaults to false. Set `fast = true` in a Codex
worker level (for example, `standard = { model = "...", effort = "high", fast = true }`)
or on a role; the role overrides the level. `delegate submit --fast` and
`--no-fast` override either setting for a single task. `--fast` requires a Codex
worker. The resolved value appears in the submit handle and task status, and is
preserved for resumes. Fast mode may incur higher costs and depends on model and
account availability. The opt-in passes `features.fast_mode=true` and
`service_tier="fast"` to Codex. When using an unisolated home, existing Codex
settings may independently affect the service tier; false retains the existing
command and disables the Codex fast-mode feature.

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

For a role that enables either policy, add only the applicable optional fields:

```json
{
  "escalation_reason": "contradictory-evidence",
  "user_directed": true
}
```

`read` entries are checked at submit: a concrete path that does not exist (relative
paths are taken from the project root) refuses the submit and lists the missing
paths, and no task is created. Entries containing `*`, `?` or `[` and URLs are not
checked. Put paths the worker may read if present, or will itself create, in the
optional `read_optional` list; they are not checked and the prompt marks them as
possibly absent.

## What you get back

| what happened | what you get |
|---|---|
| a tool call never returned | `timeout`, `failure_class: tool_stall` |
| the worker finished but never handed back a result | `timeout`, `finalization_timeout`, its last message kept aside |
| the worker exited non-zero | `failed`, `nonzero_exit` |
| the worker reported an authentication failure | `failed`, `auth_failed`, with the first 200 characters of its error message |
| the worker reported another API error | `failed`, `worker_api_error`, with the first 200 characters of its error message |
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

The Chair receives at most five strings of 300 characters per evidence list.
Workers can return complete tables and details in `report`; delegate saves that
text as `report.md` in the task log and returns `report_path`. `report`, `counts`, and `coverage` are required worker fields and may be null.
`counts` is an array of `{ "name": string, "count": integer }` objects, preserved as a list
for the Chair; coverage shows at most ten remaining items. Only a non-empty report
creates `report.md`; `report_path` is set by delegate, not the worker.
Large output alone is not a reason to reduce the task scope or request a decision.

Roles may set \`requires_allowed_writes = true\` for patchers,
\`forbids_allowed_writes = true\` for auditors, and
\`repo_local_reads = true\` when absolute paths, parent traversal, and URLs must be
refused before launch.

For a strict role that must inspect a mounted data root, keep
\`repo_local_reads = true\` and add \`allowed_read_roots = ["/mnt/research-data"]\`.
Only absolute reads that resolve under one of those roots are accepted; a symlink
that resolves outside is refused. This option requires \`repo_local_reads = true\`
and does not broaden write scope.

To deny reads globally, add `denied_read_paths = ["/absolute/path"]` at the top
level of `.claude/delegate.toml`. Submit resolves packet paths and denied paths
before comparison, so it refuses both a denied path's descendants and its parents.

`required_evidence` is instruction to the worker, and appears in its prompt. The
contract checks that the evidence fields are present, well formed and within those
limits. It does not check that they answer what was asked, and it cannot tell
whether they are true.

When a role permits external write roots and the packet has `allowed_writes`, the worker may write only to those repository-relative paths inside the repository and only below the declared roots outside it. If requested verification cannot run or cannot be evidenced, the worker returns `unverified` with the reason in `blockers`; collection keeps it distinct from `completed`.

Each blocker is `{"reason", "text"}`: `environment` (a tool, host or sandbox failure) or `not_done` (planned work or verification unfinished). A bare string from an older worker, and any other reason, counts as `not_done`. A check the packet prohibits is not a blocker; the worker reports it in `observed_facts` or `report`. A `completed` result carrying a `not_done` blocker is not completed: a read-only task ends `unverified`, and a write-capable task is treated like `work_complete: false` (resumed up to `resume_limit`, then `failed` / `incomplete_work`). A `completed` result with only `environment` blockers ends `unverified` with `terminal_reason: environment_blocked` for every role (a write-capable task is not resumed, since the same environment would fail again). All of this is decided in one place, `envelope.judge`.

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
| `delegate collect <task-id> [--task-root DIR]` | take delivery, once |
| `delegate status <task-id>` | explicit diagnosis, never a waiting loop |
| `delegate cancel <task-id>` | stop a task that is wrong or runaway |
| `delegate reconcile` | classify tasks whose worker is gone |
| `delegate watch` | what the hook runs; waits for something to collect |

`submit` takes `--worker <name>` to override the backend for one task. Every
command takes `--project-root`.

A worker is not the Chair. The runner starts every worker with `DELEGATE_WORKER=1`
and `DELEGATE_PARENT_TASK=<task_id>`, the prompt says it must not delegate whatever
repository instructions say, and `delegate submit` called with `DELEGATE_WORKER`
set is refused with `nested_submit` (exit 6).

A worker that is itself broken is not sent more work. At submit, the three most
recent terminal tasks of the chosen worker in the task root (by `terminal_at`) are
looked at; when all three ended `failed` with the same worker-fault class, submit is
refused with `worker_unhealthy` (exit 7), naming the worker, the class, the three
task ids and `--allow-unhealthy`. The worker-fault classes are `auth_failed` (a
401/403, an authentication error or an `api_key` code, as the adapters report it),
`launch_error` and `supervisor_error` (matched on `terminal_reason`, because their
`failure_class` is an exception name). Left out because the task or a later retry
may be the cause: `worker_api_error`, `provider_capacity`, the timeout classes,
`nonzero_exit`, `empty_result`, `invalid_result`, `incomplete_work` and
`write_scope_violation`. Any other outcome among the three, or fewer than three
tasks, means the worker is not held back, so a success ends the streak by itself.
`--allow-unhealthy` submits anyway and records `unhealthy_override` in `state.json`.
For a role with `compare_workers`, a chosen arm that is unhealthy gives way to the
other arm when that one is not; `--allow-unhealthy` keeps the chosen arm. A worker
named with `--worker` is never replaced.

Every error prints `{"error": <name>, "reason": <sentence>}` plus any detail, and
the exit code comes from one table (`delegate/errors.py`): 1 for `no_settings`,
`invalid_config`, `invalid_packet`, `submit_refused` and `task_not_found`, 2 is
`WAKE` (not an error), 3 `already_delivered`, 4 `task_not_terminal`, 5
`owner_mismatch`, 6 `nested_submit`, 7 `worker_unhealthy`.

Settings and task records need not live under the project root. A relative
`--config` or `--task-root` is taken from the current directory and recorded as an
absolute path. A task root inside the project is not counted as the worker's
changes in the write-scope check. `submit --config <path>` reads that file instead
of `<project-root>/.claude/delegate.toml`, and `--task-root <path>` (on `submit`,
`status`, `collect`, `cancel`, `reconcile` and `watch`) keeps the task records there
instead of under `<project-root>/.claude/logs/delegate/`. The worker still runs with
the project root as its working directory. `state.json` records `config_path`
(absolute), `config_sha256` (of the bytes that were read) and `task_root` for every
task. Every handle, completion notice and `reconcile` line carries the task's
absolute `task_dir`; the hooks, their per-session registry and `watch-group.json`
record that path alone, so a task is followed wherever it is stored (entries from
older versions, which name a `project_root` and `task_id`, still mean the default
root). To collect a task, pass `--task-root` set to the parent of its `task_dir`.
A watch entry whose directory is missing or unreadable is skipped, not fatal. One
Bash call may submit several independent tasks: the hook tracks every returned
handle. After collecting one, it re-arms itself for any still-uncollected task from
that same call.
When Claude Code provides `CLAUDE_CODE_SESSION_ID`, submit records it and hooks
only report that session's tasks. Pending watches are stored per session; legacy
pending watches migrate when their owner runs the hook. Submissions without the
ID do not wake a session. Manual `watch`, `reconcile`, and `collect`
remain project-wide diagnostic commands; called as a session they leave bridge-owned
tasks out, and with `--owner <id>` (`watch`, `reconcile`) they show only that
owner's bridge tasks.

`submit --owner <id>` makes a bridge-owned task instead:
`owner_kind` is `bridge`, no session is recorded, and no hook announces it.
Only `collect` and `cancel` called with the same `--owner` may act on it; anyone else gets `owner_mismatch` (exit 5). A session-owned task
(the default, and any task from before owners existed) stays open to every
session, but not to a bridge owner. `collect` records who took delivery in
`delivered_to` (`{kind, id}`), and of concurrent collects exactly one succeeds;
the rest get `already_delivered` (exit 3).

The owner is set only by the `--owner` flag; no environment variable sets it,
and the worker's environment has `CLAUDE_CODE_SESSION_ID` removed. The owner check prevents mistakes between a session and a bridge. It is
not authentication: the owner id is stored in plain text in the task state.

## What is and is not checked

Detected:

- a worker that never started, stalled, died, or lost its supervisor
- a result that is absent, malformed, or missing required fields
- a final message that never became a delivered result
- writes outside the declared scope when the worker backend reports the changed path
- work-tree paths that became dirty during the run but cannot be attributed to that worker

Not detected:

- writes outside the work tree: a home directory, a system path
- writes to files `.gitignore` excludes
- the author of an unreported work-tree change; these are recorded as
  `unattributed_workspace_changes`, do not fail the task, and set
  `write_scope_checked` to `false`
- network effects, credential access, or anything git does not represent
- a result that satisfies the contract and is still wrong

What a sandbox stops before the fact depends on the backend. A task that declared
no writes gets the narrowest posture its backend has:

| backend | a task that may not write | enforced by |
|---|---|---|
| codex | `--sandbox read-only`, network closed | the operating system |
| claude | `auto`, minus the editing tools | a classifier, and tools withheld |
| opencode | an injected `delegate-readonly` agent | OpenCode permissions |

Only codex refuses in the kernel: an attempt to write comes back `Read-only file
system`. Claude Code is not handed the editing tools, and its classifier turned
down every way round them that was tried: a shell redirect, `tee`, and a Python
one-liner. That is a judgement made each time rather than a boundary. OpenCode
read-only tasks receive a per-run primary agent whose `edit`, `bash`, `task`,
`webfetch`, and `websearch` permissions are denied. This is a tool-permission
boundary, not an operating-system sandbox; the task contract still forbids
scratch files in `.claude`, `/tmp`, and elsewhere.

A role that runs tests or builds gets a writable filesystem even when it may not
change the repository, because build tools write caches. Python and Ruff caches go under the task temporary
directory, and pytest's cache provider is disabled.
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
