"""What the event stream says about where a run stopped.

Events are folded in one at a time and never re-read, so watching a long run
costs the same as watching a short one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from delegate import envelope
from delegate.adapters.base import NormalizedEvent

# Item kinds that can legitimately sit unfinished for a long time. An unfinished
# item of any other kind means the worker is thinking, not blocked.
STALLABLE_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "mcp_tool_call",
        "dynamic_tool_call",
        "collab_tool_call",
        "web_search",
        "image_generation",
    }
)
RUNTIME_WARNING_LIMIT = 2


@dataclass(frozen=True)
class RunView:
    event_count: int = 0
    session_id: str | None = None
    open_items: tuple[tuple[str, str], ...] = ()
    turn_completed: bool = False
    last_agent_message: str | None = None
    runtime_warnings: int = 0


def observe(view: RunView, event: NormalizedEvent) -> RunView:
    """Fold one event into the running view."""
    view = replace(view, event_count=view.event_count + 1)

    if event.kind == "session_started":
        return replace(view, session_id=event.session_id or view.session_id)
    if event.kind == "turn_completed":
        return replace(view, turn_completed=True)
    if event.kind == "runtime_warning":
        return replace(view, runtime_warnings=view.runtime_warnings + 1)
    if event.kind == "item_started" and event.item_id and event.item_type:
        return replace(view, open_items=(*view.open_items, (event.item_id, event.item_type)))
    if event.kind == "item_completed":
        open_items = tuple(item for item in view.open_items if item[0] != event.item_id)
        if event.item_type == "agent_message" and event.text is not None:
            return replace(view, open_items=open_items, last_agent_message=event.text)
        return replace(view, open_items=open_items)
    return view


def has_usable_result(view: RunView) -> bool:
    """Whether the worker's last message would have been a valid result."""
    if view.last_agent_message is None:
        return False
    try:
        parsed = json.loads(view.last_agent_message)
    except json.JSONDecodeError:
        return False
    return envelope.violations(parsed) == []


def classify_timeout(view: RunView) -> str:
    """Name the way this run stopped, most specific reading first."""
    if view.turn_completed or has_usable_result(view):
        return "finalization_timeout"
    if any(item_type in STALLABLE_ITEM_TYPES for _, item_type in view.open_items):
        return "tool_stall"
    if view.runtime_warnings >= RUNTIME_WARNING_LIMIT:
        return "runtime_stall"
    if view.event_count > 0:
        return "event_stream_stall"
    return "wall_clock_timeout"
