"""LangGraph conditional-edge routing functions.

Each function receives the current AgentState and returns the name of the
next node to visit.  All routing decisions are pure — no side effects.
"""

from src.agent.state import AgentState


def route_from_router(state: AgentState) -> str:
    trigger = state.get("trigger", "")
    mapping = {
        "daily":                "daily_planning",
        "evening":              "daily_planning",
        "weekend":              "weekend_brief",
        "study":                "send_duration_picker",
        "done":                 "done_parser",
        "pick":                 "study_topic",
        "activate":             "activate_topic",
        "discuss":              "discuss_parser",
        "discuss_ready_confirm": "notify_discuss_ready",
    }
    return mapping.get(trigger, "output")


def route_from_daily_planning(state: AgentState) -> str:
    if state.get("preview_only") or not state.get("has_study_plan"):
        return "output"
    return "await_daily_confirmation"


def route_from_await_daily_confirmation(state: AgentState) -> str:
    payload = (state.get("payload") or "").lower().strip()
    return "book_events" if payload == "yes, book them" else "output"


def route_from_done_parser(state: AgentState) -> str:
    if not state.get("has_unlogged_sessions"):
        return "output"
    return "log_session" if state.get("current_topic_id") is not None else "select_done_topic"


def route_from_select_done_topic(state: AgentState) -> str:
    """Route to output on error (messages set or no current_topic_id), else log_session."""
    if state.get("messages") or state.get("current_topic_id") is None:
        return "output"
    return "log_session"


def route_from_on_demand(state: AgentState) -> str:
    return "generate_brief" if state.get("proposed_topic") else "output"


def route_from_generate_brief(state: AgentState) -> str:
    """Route to await_brief_confirmation when a slot is available; else output."""
    if state.get("has_study_plan") and state.get("proposed_slot"):
        return "await_brief_confirmation"
    return "output"


def route_from_await_brief_confirmation(state: AgentState) -> str:
    payload = (state.get("payload") or "").lower().strip()
    return "book_events" if payload == "yes, book them" else "output"


def route_from_activate_topic(state: AgentState) -> str:
    # graduate_topic when a topic selection message was queued (no error)
    return "output" if state.get("messages") else "graduate_topic"


def route_from_study_topic(state: AgentState) -> str:
    return "output" if state.get("messages") else "study_topic_category"


def route_from_study_topic_category(state: AgentState) -> str:
    return "output" if state.get("messages") else "study_topic_confirm"


def route_from_log_weak_areas(state: AgentState) -> str:
    """Conceptual topics complete in Q1 and route to output; all others need Q2."""
    if state.get("weak_areas_topic_type") == "conceptual":
        return "output"
    return "log_weak_areas_q2"


def route_from_discuss_parser(state: AgentState) -> str:
    """Route to output when a message is set (single-topic or error); else start_discuss."""
    return "output" if state.get("messages") else "start_discuss"


def route_from_graduate_topic(state: AgentState) -> str:
    """Route to confirm_graduate when a soft-warning button was sent; else output.

    ``graduate_topic`` signals a pending soft-guard prompt by setting
    ``pending_message_id`` with an empty ``messages`` list.  Any other
    outcome (activation completed, hard block, or error) leaves ``messages``
    non-empty so we go straight to output.
    """
    if state.get("pending_message_id") is not None and not state.get("messages"):
        return "confirm_graduate"
    return "output"
