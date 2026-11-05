# Changelog

## Unreleased

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
