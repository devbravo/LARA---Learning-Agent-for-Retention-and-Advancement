from typing import TypedDict


class AgentState(TypedDict, total=False):
    trigger: str               # fresh flow routing signal
    chat_id: int
    duration_min: int | None
    proposed_topic: str | None          # single-slot flow (on_demand)
    proposed_slot: dict | None          # single-slot flow (on_demand)
    proposed_slots: list[dict] | None   # multi-slot flow (daily_planning)
    has_study_plan: bool                # False → skip confirm, go straight to output
    preview_only: bool                  # True → evening briefing, route to output not confirm
    quality_score: int | None
    messages: list[str]        # outbound Telegram messages
    payload: str | None        # raw resume value from interrupt()
    current_topic_id: int | None
    current_topic_name: str | None
    study_topic_category: str | None    # selected category in /pick flow
    pending_message_id: int | None      # message_id of the one button message currently awaiting user interaction
    has_unlogged_sessions: bool | None  # True when done_parser / log_weak_areas queued a topic for rating
    weak_areas_first_answer: str | None # Q1 answer carried from log_weak_areas to log_weak_areas_q2
    weak_areas_topic_type: str | None    # topic_type carried from log_weak_areas to log_weak_areas_q2
