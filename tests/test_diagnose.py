"""Where a run stopped, decided from what was actually observed.

A timeout on its own says nothing useful. The events tell us whether the worker
was waiting on a tool, spinning in its runtime, already finished and failing to
hand back a result, or never started at all. The reading is incremental: each
event is folded in once, so a long run is not re-read on every heartbeat.
"""

from __future__ import annotations

import json

from delegate import diagnose
from delegate.adapters.base import NormalizedEvent


def fold(*events: NormalizedEvent) -> diagnose.RunView:
    view = diagnose.RunView()
    for event in events:
        view = diagnose.observe(view, event)
    return view


def test_file_changes_accumulate_without_duplicates() -> None:
    view = fold(
        NormalizedEvent(kind="item_completed", changed_paths=("one.txt", "two.txt")),
        NormalizedEvent(kind="item_completed", changed_paths=("two.txt",)),
    )

    assert view.changed_paths == ("one.txt", "two.txt")


def test_a_tool_write_is_counted_only_after_successful_completion() -> None:
    started = NormalizedEvent(kind="item_started", item_id="write-1", changed_paths=("one.txt",))

    assert fold(started).changed_paths == ()
    assert (
        fold(
            started,
            NormalizedEvent(kind="item_completed", item_id="write-1", succeeded=False),
        ).changed_paths
        == ()
    )
    assert fold(
        started,
        NormalizedEvent(kind="item_completed", item_id="write-1", succeeded=True),
    ).changed_paths == ("one.txt",)


def test_nothing_observed_at_all_is_a_wall_clock_timeout() -> None:
    assert diagnose.classify_timeout(fold()) == "wall_clock_timeout"


def test_events_that_simply_stopped_arriving_are_an_event_stream_stall() -> None:
    view = fold(NormalizedEvent(kind="session_started", session_id="s-1"))

    assert diagnose.classify_timeout(view) == "event_stream_stall"
    assert view.session_id == "s-1"


def test_a_tool_call_that_never_finished_is_a_tool_stall() -> None:
    view = fold(
        NormalizedEvent(kind="item_started", item_id="i-1", item_type="command_execution"),
    )

    assert diagnose.classify_timeout(view) == "tool_stall"


def test_a_tool_call_that_finished_no_longer_counts_as_stalled() -> None:
    view = fold(
        NormalizedEvent(kind="item_started", item_id="i-1", item_type="command_execution"),
        NormalizedEvent(kind="item_completed", item_id="i-1", item_type="command_execution"),
    )

    assert diagnose.classify_timeout(view) == "event_stream_stall"


def test_an_unfinished_item_that_cannot_stall_is_not_a_tool_stall() -> None:
    view = fold(NormalizedEvent(kind="item_started", item_id="i-1", item_type="reasoning"))

    assert diagnose.classify_timeout(view) == "event_stream_stall"


def test_repeated_runtime_warnings_are_a_runtime_stall() -> None:
    warning = NormalizedEvent(kind="runtime_warning")

    assert diagnose.classify_timeout(fold(warning)) == "event_stream_stall"
    assert diagnose.classify_timeout(fold(warning, warning)) == "runtime_stall"


def test_a_finished_turn_without_a_result_is_a_finalization_timeout() -> None:
    view = fold(
        NormalizedEvent(kind="item_started", item_id="i-1", item_type="command_execution"),
        NormalizedEvent(kind="turn_completed"),
    )

    assert diagnose.classify_timeout(view) == "finalization_timeout"


def test_a_usable_final_message_that_never_landed_is_a_finalization_timeout() -> None:
    message = json.dumps(
        {
            "status": "completed",
            "observed_facts": ["one"],
            "verified_comparisons": [],
            "artifact_paths": [],
            "blockers": [],
            "decision_needed": None,
        }
    )
    view = fold(
        NormalizedEvent(kind="item_completed", item_type="agent_message", text=message),
    )

    assert view.last_agent_message == message
    assert diagnose.classify_timeout(view) == "finalization_timeout"


def test_an_unusable_final_message_does_not_claim_the_work_finished() -> None:
    view = fold(
        NormalizedEvent(kind="item_completed", item_type="agent_message", text="I think so?"),
    )

    assert diagnose.classify_timeout(view) == "event_stream_stall"
