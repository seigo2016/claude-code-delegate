# Changelog

## 0.3.0

- Keep one unreachable task from stopping the hooks: in `_hook-reconcile` and `_hook-watch`, a task or registry entry that raises `OSError` (a read-only or unreadable task root) is skipped and left out of what is written back, so the other tasks are still reported and the exit code (2 for a ready task) depends only on tasks the hook can reach.
- Stop resubmitting to a broken worker: when a worker's three most recent terminal tasks in the task root all ended `failed` with the same worker-fault class (`auth_failed`, `launch_error`, `supervisor_error`), `submit` refuses with `worker_unhealthy` (exit 7) naming the worker, class, task ids and `--allow-unhealthy`, which overrides it and records `unhealthy_override`. A success ends the streak by itself; there is no cooldown.
- Resolve a relative `--task-root`/`--config` against the current directory and record absolute paths, and leave a task root inside the project out of the write-scope accounting.
- Stop nested delegation: workers run with `DELEGATE_WORKER=1` and `DELEGATE_PARENT_TASK=<task_id>`, `submit` refuses with `nested_submit` (exit 6) when `DELEGATE_WORKER` is set, and the worker prompt says the worker is not the Chair and must not delegate.
- Add the role key `compare_workers = [a, b]` (exactly two distinct enabled workers, checked at load). Without `--worker`, a task takes the arm a stable hash of its new task id picks (first byte of SHA-256, modulo 2); if that arm cannot be planned, fails the preflight or is unhealthy, the other arm is used, and the submit is refused only when neither can run. `state.json` records `route_arm` (the worker that runs) and `route_policy` (`compare`, `explicit` for `--worker`, or null). Deduplication merges identical running requests across both arms, as before.
- Add an optional `[common] instructions` table to the settings: its text is placed once in every task's prompt, before the role's own instructions.
- Add `submit --config <path>` and `--task-root <path>` (also on `status`, `collect`, `cancel`, `reconcile`, `watch`) to read settings from, and keep task records in, a place other than the project root; the worker still runs in the project root. `state.json` records `config_path`, `config_sha256` and `task_root`.
- Make the hooks follow tasks kept under any task root: handles, completion notices and `reconcile` lines carry the task's absolute `task_dir`, and the hooks, the session watch registry and `watch-group.json` record that path alone (older `project_root` + `task_id` entries still mean the default root; an entry whose directory is missing or unreadable is skipped). Collect with `--task-root` set to the parent of `task_dir`.
- Report every error as `{"error": <name>, "reason": <sentence>}` with its exit code taken from one table (`delegate/errors.py`); a refused submit is now `submit_refused` with the sentence in `reason`.
- Manual `reconcile` and `watch` no longer offer bridge-owned tasks to a session caller; `--owner <id>` shows only that owner's bridge tasks.
- Make `collect` deliver exactly once: the delivered check and its record now share one lock hold.
- Add owner kinds: `submit --owner` makes a bridge-owned task that only the same owner may `collect` or `cancel` (`owner_mismatch`, exit 5) and that hooks never announce. `collect` records `delivered_to`. The owner comes only from `--owner` (no environment variable), and workers no longer inherit `CLAUDE_CODE_SESSION_ID`; the check guards against mistakes, not against a hostile caller.
- Give each blocker a reason, `environment` or `not_done`; a legacy string or an unknown reason reads as `not_done`. A check the packet prohibits is not a blocker (the worker reports it in `observed_facts` or `report`). A `completed` result with a `not_done` blocker ends `unverified` (read-only) or is resumed then `failed` / `incomplete_work` (write-capable); with only `environment` blockers it ends `unverified` (`environment_blocked`) for every role, without a resume. `envelope.judge` makes every one of these decisions.
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
