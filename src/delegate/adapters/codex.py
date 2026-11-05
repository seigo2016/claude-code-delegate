"""Codex CLI, mapped onto the normalized event kinds."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import tomllib
import warnings
from pathlib import Path
from typing import Any

from delegate import config
from delegate.adapters.base import NormalizedEvent, api_error_event

MINIMAL_CONFIG = 'approval_policy = "never"\nsandbox_mode = "read-only"\n'


def user_codex_home() -> Path:
    """Where the user's interactive codex setup lives."""
    configured = os.environ.get("CODEX_HOME")
    if configured and configured.strip():
        return Path(configured).expanduser().resolve(strict=False)
    return (Path.home() / ".codex").resolve(strict=False)


def delegate_codex_home() -> Path:
    """Where delegate isolates codex home to avoid interactive instructions."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg and xdg.strip():
        return (Path(xdg).expanduser() / "delegate" / "codex-home").resolve(strict=False)
    return (Path.home() / ".cache" / "delegate" / "codex-home").resolve(strict=False)


def prepare_codex_home(
    *,
    target_home: Path | None = None,
    user_home: Path | None = None,
) -> Path | None:
    base = (target_home if target_home is not None else delegate_codex_home()).resolve()
    source = (user_home if user_home is not None else user_codex_home()).resolve()
    if base == source or base in source.parents or source in base.parents:
        warnings.warn(
            "Codex home isolation disabled: source and isolation paths overlap", stacklevel=2
        )
        return None

    key = hashlib.sha256(os.fsencode(str(source))).hexdigest()[:24]
    target = (base / key).resolve()
    if target == source or target in source.parents or source in target.parents:
        warnings.warn(
            "Codex home isolation disabled: source and isolation paths overlap", stacklevel=2
        )
        return None
    target.mkdir(parents=True, exist_ok=True)
    # Unique temporary names and atomic replacements keep simultaneous preparations safe.
    with tempfile.TemporaryDirectory(dir=target, prefix=".prepare-") as temporary:
        temp = Path(temporary)
        (temp / "auth.json").symlink_to((source / "auth.json").resolve())
        (temp / "config.toml").write_text(MINIMAL_CONFIG, encoding="utf-8")
        os.replace(temp / "auth.json", target / "auth.json")
        os.replace(temp / "config.toml", target / "config.toml")
    (target / "AGENTS.md").unlink(missing_ok=True)
    return target


def resolve_isolate_home(project_root: Path) -> bool:
    candidates = (
        project_root / ".claude" / "delegate.toml",
        project_root / "delegate.toml",
    )
    for candidate in candidates:
        if candidate.is_file():
            try:
                settings = config.load(candidate)
                worker = settings.workers.get("codex")
                if worker is not None:
                    return worker.isolate_home
                for w in settings.workers.values():
                    if w.adapter == "codex":
                        return w.isolate_home
            except Exception:
                try:
                    with candidate.open("rb") as handle:
                        data = tomllib.load(handle)
                    workers = data.get("workers", {})
                    if isinstance(workers, dict):
                        codex_section = workers.get("codex")
                        if isinstance(codex_section, dict) and "isolate_home" in codex_section:
                            val = codex_section["isolate_home"]
                            if isinstance(val, bool):
                                return val
                except Exception:
                    pass
    return True


def cache_root() -> Path:
    """Where build tools keep the cache they insist on writing to.

    `workspace-write` covers the repository and the temporary directory, but not the
    home directory, and `uv run pytest` was measured stopping before pytest started
    because it could not open its cache there.
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    return Path.home() / ".cache"


# Codex retries its model list in the background; repeated timeouts there mean
# the run is stuck rather than working.
MODEL_REFRESH_TIMEOUT = (
    "codex_models_manager::manager: failed to refresh available models: "
    "timeout waiting for child process to exit"
)
USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


class CodexAdapter:
    name = "codex"

    def __init__(
        self,
        *,
        isolate_home: bool | None = None,
        target_home: Path | None = None,
        user_home: Path | None = None,
    ) -> None:
        self.isolate_home = isolate_home
        self.target_home = target_home
        self.user_home = user_home

    def build_command(
        self,
        *,
        project_root: Path,
        prompt_path: Path,
        result_path: Path,
        schema_path: Path,
        model: str,
        effort: str,
        writes_allowed: bool,
        runs_commands: bool,
        allowed_read_roots: tuple[str, ...] = (),
        allowed_write_roots: tuple[str, ...] = (),
        timeout_sec: int = 1800,
        isolate_home: bool | None = None,
        temp_dir: Path | None = None,
        fast: bool = False,
    ) -> list[str]:
        del allowed_read_roots
        del timeout_sec
        # What keeps a role that runs commands off the repository is the scope check
        # afterwards, not the sandbox.
        writable = writes_allowed or runs_commands
        roots = (
            json.dumps(
                list(
                    dict.fromkeys(
                        [
                            str(cache_root()),
                            *allowed_write_roots,
                            *([str(temp_dir)] if temp_dir else []),
                        ]
                    )
                )
            )
            if writable
            else "[]"
        )
        base_cmd = [
            "codex",
            "exec",
            "-C",
            str(project_root),
            "--json",
            "-o",
            str(result_path),
            "--model",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            # Left to ~/.codex/config.toml, how far a worker can reach is decided by
            # a file this project does not own, and differs from machine to machine.
            "--sandbox",
            "workspace-write" if writable else "read-only",
            "-c",
            'approval_policy="never"',
            "-c",
            f"sandbox_workspace_write.network_access={str(writable).lower()}",
            "-c",
            f"sandbox_workspace_write.writable_roots={roots}",
            "--output-schema",
            str(schema_path),
            *(
                ["-c", "features.fast_mode=true", "-c", 'service_tier="fast"']
                if fast
                else ["--disable", "fast_mode"]
            ),
            "-",
        ]
        effective_isolate = isolate_home
        if effective_isolate is None:
            if self.isolate_home is not None:
                effective_isolate = self.isolate_home
            else:
                effective_isolate = resolve_isolate_home(project_root)

        env = [f"TMPDIR={temp_dir}"] if writable and temp_dir is not None else []
        if not effective_isolate:
            return ["env", *env, *base_cmd] if env else base_cmd

        codex_home_dir = prepare_codex_home(
            target_home=self.target_home,
            user_home=self.user_home,
        )
        if codex_home_dir is None:
            return ["env", *env, *base_cmd] if env else base_cmd
        return ["env", f"CODEX_HOME={codex_home_dir}", *env, *base_cmd]

    def build_resume_command(self, *, session_id: str, **kwargs: Any) -> list[str]:
        """Keep the original sandbox, schema and Codex home when resuming."""
        command = self.build_command(**kwargs)
        return [*command[:-1], "resume", session_id, "-"]

    def parse_events(self, raw_line: str) -> list[NormalizedEvent]:
        try:
            event: Any = json.loads(raw_line)
        except json.JSONDecodeError:
            return []
        if not isinstance(event, dict):
            return []

        parsed_error = api_error_event(event)
        if parsed_error is not None:
            return [parsed_error]

        kind = event.get("type")
        if kind == "thread.started":
            session_id = event.get("thread_id")
            return [
                NormalizedEvent(
                    kind="session_started",
                    session_id=session_id if isinstance(session_id, str) else None,
                )
            ]
        if kind == "turn.completed":
            usage = event.get("usage")
            tokens = tuple(
                (field, usage[field])
                for field in USAGE_TOKEN_FIELDS
                if isinstance(usage, dict)
                and isinstance(usage.get(field), int)
                and not isinstance(usage[field], bool)
                and usage[field] >= 0
            )
            return [
                NormalizedEvent(
                    kind="turn_completed",
                    usage_source="codex" if tokens else None,
                    usage_tokens=tokens,
                )
            ]
        if kind in {"item.started", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict):
                return []
            text = item.get("text")
            changes = item.get("changes")
            changed_paths = (
                tuple(
                    change["path"]
                    for change in changes
                    if isinstance(change, dict) and isinstance(change.get("path"), str)
                )
                if isinstance(changes, list)
                else ()
            )
            return [
                NormalizedEvent(
                    kind="item_started" if kind == "item.started" else "item_completed",
                    item_id=item.get("id") if isinstance(item.get("id"), str) else None,
                    item_type=item.get("type") if isinstance(item.get("type"), str) else None,
                    text=text if isinstance(text, str) else None,
                    changed_paths=changed_paths,
                )
            ]
        return []

    def parse_stderr_lines(self, raw_line: str) -> list[NormalizedEvent]:
        if MODEL_REFRESH_TIMEOUT in raw_line:
            return [NormalizedEvent(kind="runtime_warning", text="model_refresh_timeout")]
        return []
