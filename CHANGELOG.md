# Changelog

## Unreleased

- Add `submit --config <path>` and `--task-root <path>` (also on `status`, `collect`, `cancel`, `reconcile`, `watch`) to read settings from, and keep task records in, a place other than the project root; the worker still runs in the project root. `state.json` records `config_path`, `config_sha256` and `task_root`. Handles, the session watch registry and `watch-group.json` carry `task_root` when it is not the default, and entries without it still mean the default.

- Manual `reconcile` and `watch` no longer offer bridge-owned tasks to a session caller; `--owner <id>` shows only that owner's bridge tasks.
- Make `collect` deliver exactly once: the delivered check and its record now share one lock hold.
- Add owner kinds: `submit --owner` makes a bridge-owned task that only the same owner may `collect` or `cancel` (`owner_mismatch`, exit 5) and that hooks never announce. `collect` records `delivered_to`. The owner comes only from `--owner` (no environment variable), and workers no longer inherit `CLAUDE_CODE_SESSION_ID` or owner variables; the check guards against mistakes, not against a hostile caller.
- Give each blocker a reason (`forbidden_by_packet`, `environment`, `not_done`); legacy string blockers read as `not_done`. A `completed` result with a `not_done` blocker ends `unverified` (read-only) or is resumed then `failed` / `incomplete_work` (write-capable). `forbidden_by_packet` counts only with a `forbidden_item` that exactly matches an entry of the packet's `forbidden` list (else `not_done`); an `environment` blocker on `completed` ends `unverified` (`environment_blocked`) for every role, without a resume.
- Refuse a submit whose packet `read` names a concrete path that does not exist, listing the missing paths; add `read_optional` for paths that may be absent.

## 0.2.13

- Add opt-in Codex fast mode per worker level, role, or submit flag, and record its resolved value.

## 0.2.12

- Restrict hook completion notices and session-start ready lists to tasks owned by the submitting session; tasks without a session ID remain available for explicit collection.
- Store pending completion watches per submitting session, and migrate legacy pending watches only for their owner, preventing other sessions and older hooks from consuming new watches.

## 0.2.11

- Record failure_class and a one-line failure_message for failed, timed-out, degraded and orphaned tasks, including supervisor errors.
- Terminate an already-started worker when the supervisor fails, and keep a single terminal event.
- Do not announce collected terminal tasks (including orphaned and decision_needed) again.

- Make worker result schemas compatible with strict structured outputs; use nullable report, counts, and coverage fields.

- Route completion hooks to the submitting Claude Code session when its ID is available.

## 0.2.10

- Preserve complete worker reports in task logs while keeping collected evidence concise.
- Direct command caches to task temporary directories and clarify write permissions.

- Keep workers with write permissions working until implementation is complete;
  reject incomplete `unverified` results as `incomplete_work`.
