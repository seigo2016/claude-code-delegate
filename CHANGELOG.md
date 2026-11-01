# Changelog

## Unreleased

- Make worker result schemas compatible with strict structured outputs; use nullable report, counts, and coverage fields.

- Route completion hooks to the submitting Claude Code session when its ID is available.

## 0.2.10

- Preserve complete worker reports in task logs while keeping collected evidence concise.
- Direct command caches to task temporary directories and clarify write permissions.

- Keep workers with write permissions working until implementation is complete;
  reject incomplete `unverified` results as `incomplete_work`.
