"""
LangGraph graph definition for the Learning Manager agent.

Flow (HITL pattern — interrupt() replaces awaiting_* flags):

  START → router → (conditional) → daily_planning | weekend_brief | send_duration_picker
                                  | done_parser | study_topic | activate_topic | output

  daily_planning (morning)  → interrupt() → book_events → output → END
  daily_planning (skip/no-plan/evening) → output → END
  weekend_brief             → output → END
  send_duration_picker → interrupt() → on_demand → generate_brief → interrupt() → book_events → output → END
  done_parser → (select_done_topic → interrupt() →)? log_session → interrupt() → log_weak_areas → output → END
  study_topic → interrupt() → study_topic_category → interrupt() → study_topic_confirm → output → END
  activate_topic → interrupt() → graduate_topic → output → END

Checkpointer: SqliteSaver backed by db/state.db.
Thread ID: chat_id from state (one thread per user).
"""

import sqlite3
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env", override=True)

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.agent.nodes import (
    AgentState,
    activate_topic,
    await_brief_confirmation,
    await_daily_confirmation,
    book_events,
    daily_planning,
    done_parser,
    generate_brief,
    graduate_topic,
    log_session,
    log_weak_areas,
    log_weak_areas_q2,
    on_demand,
    output,
    route_from_activate_topic,
    route_from_await_brief_confirmation,
    route_from_await_daily_confirmation,
    route_from_daily_planning,
    route_from_done_parser,
    route_from_generate_brief,
    route_from_on_demand,
    route_from_router,
    route_from_select_done_topic,
    route_from_study_topic,
    route_from_study_topic_category,
    router,
    select_done_topic,
    send_duration_picker,
    study_topic,
    study_topic_category,
    study_topic_confirm,
    weekend_brief,
)

_DB_DIR = Path(__file__).parents[2] / "db"
_STATE_DB_PATH = str(_DB_DIR / "state.db")


def build_graph(checkpointer=None):
    """Construct and compile the LangGraph StateGraph."""
    builder: Any = StateGraph(cast(Any, AgentState))

    # Register all nodes
    builder.add_node("router", router)
    builder.add_node("daily_planning", daily_planning)
    builder.add_node("await_daily_confirmation", await_daily_confirmation)
    builder.add_node("weekend_brief", weekend_brief)
    builder.add_node("send_duration_picker", send_duration_picker)
    builder.add_node("on_demand", on_demand)
    builder.add_node("done_parser", done_parser)
    builder.add_node("select_done_topic", select_done_topic)
    builder.add_node("generate_brief", generate_brief)
    builder.add_node("await_brief_confirmation", await_brief_confirmation)
    builder.add_node("log_session", log_session)
    builder.add_node("log_weak_areas", log_weak_areas)
    builder.add_node("log_weak_areas_q2", log_weak_areas_q2)
    builder.add_node("output", output)
    builder.add_node("book_events", book_events)
    builder.add_node("study_topic", study_topic)
    builder.add_node("study_topic_category", study_topic_category)
    builder.add_node("study_topic_confirm", study_topic_confirm)
    builder.add_node("activate_topic", activate_topic)
    builder.add_node("graduate_topic", graduate_topic)

    # Entry point
    builder.add_edge(START, "router")

    # Router → conditional branch (fresh triggers only)
    builder.add_conditional_edges(
        "router",
        route_from_router,
        {
            "daily_planning":       "daily_planning",
            "weekend_brief":        "weekend_brief",
            "send_duration_picker": "send_duration_picker",
            "done_parser":          "done_parser",
            "study_topic":          "study_topic",
            "activate_topic":       "activate_topic",
            "output":               "output",
        },
    )

    # Morning/evening briefing
    builder.add_conditional_edges(
        "daily_planning",
        route_from_daily_planning,
        {"await_daily_confirmation": "await_daily_confirmation", "output": "output"},
    )
    builder.add_conditional_edges(
        "await_daily_confirmation",
        route_from_await_daily_confirmation,
        {"book_events": "book_events", "output": "output"},
    )

    # Weekend brief
    builder.add_edge("weekend_brief", "output")

    # On-demand flow (interrupt lives in on_demand)
    builder.add_edge("send_duration_picker", "on_demand")
    builder.add_conditional_edges(
        "on_demand",
        route_from_on_demand,
        {"generate_brief": "generate_brief", "output": "output"},
    )
    builder.add_conditional_edges(
        "generate_brief",
        route_from_generate_brief,
        {"await_brief_confirmation": "await_brief_confirmation", "output": "output"},
    )
    builder.add_conditional_edges(
        "await_brief_confirmation",
        route_from_await_brief_confirmation,
        {"book_events": "book_events", "output": "output"},
    )

    # Done / logging flow
    builder.add_conditional_edges(
        "done_parser",
        route_from_done_parser,
        {"log_session": "log_session", "select_done_topic": "select_done_topic", "output": "output"},
    )
    builder.add_conditional_edges(
        "select_done_topic",
        route_from_select_done_topic,
        {"log_session": "log_session", "output": "output"},
    )
    builder.add_edge("log_session", "log_weak_areas")
    builder.add_edge("log_weak_areas", "log_weak_areas_q2")
    builder.add_edge("log_weak_areas_q2", "output")

    # Pick a topic flow
    builder.add_conditional_edges(
        "study_topic",
        route_from_study_topic,
        {"study_topic_category": "study_topic_category", "output": "output"},
    )
    builder.add_conditional_edges(
        "study_topic_category",
        route_from_study_topic_category,
        {"study_topic_confirm": "study_topic_confirm", "output": "output"},
    )
    builder.add_edge("study_topic_confirm", "output")

    # Activate / graduate flow
    builder.add_conditional_edges(
        "activate_topic",
        route_from_activate_topic,
        {"graduate_topic": "graduate_topic", "output": "output"},
    )
    builder.add_edge("graduate_topic", "output")

    # Shared terminal
    builder.add_edge("book_events", "output")
    builder.add_edge("output", END)

    if checkpointer is None:
        _DB_DIR.mkdir(parents=True, exist_ok=True)
        # Use an in-memory saver by default for local runs/tests. If you
        # need durable persistence across restarts, pass a persistent
        # checkpointer explicitly when calling build_graph().
        checkpointer = MemorySaver()

    return builder.compile(checkpointer=checkpointer)


# Singleton graph for import by server / scheduler
graph = build_graph()


def get_state(chat_id: int) -> dict:
    """Read the latest checkpointed state for a given chat_id. Returns {} if none."""
    try:
        config = cast(Any, {"configurable": {"thread_id": str(chat_id)}})
        snapshot = graph.get_state(config)
        if snapshot and snapshot.values:
            return cast(dict, snapshot.values)
        return {}
    except Exception:
        return {}


def update_state(chat_id: int, values: dict) -> None:
    """Write partial state values into the checkpoint for a given chat_id."""
    config = cast(Any, {"configurable": {"thread_id": str(chat_id)}})
    graph.update_state(config, values)


def invoke(trigger: str, chat_id: int, **kwargs) -> AgentState:
    """Convenience wrapper to invoke the graph with a fresh trigger."""
    initial_state: AgentState = {
        "trigger": trigger,
        "chat_id": chat_id,
    }
    initial_state_dict: Any = initial_state
    for key in ("pending_message_id", "duration_min", "proposed_topic", "proposed_slot",
                "quality_score", "messages", "current_topic_id", "current_topic_name",
                "study_topic_category"):
        if kwargs.get(key) is not None:
            initial_state_dict[key] = kwargs[key]

    config = cast(Any, {"configurable": {"thread_id": str(chat_id)}})
    return cast(AgentState, graph.invoke(cast(Any, initial_state), config=config))


# ---------------------------------------------------------------------------
# Direct run: simulate a daily briefing (print only, no Telegram send)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    from datetime import date
    from unittest.mock import patch

    CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))

    print("=" * 60)
    print(f"Learning Manager — Daily Briefing Dry Run ({date.today()})")
    print("=" * 60)

    with patch("src.integrations.telegram_client.send_message") as mock_msg, \
         patch("src.integrations.telegram_client.send_buttons") as mock_btn:

        final_state = invoke(trigger="daily", chat_id=CHAT_ID)

        messages = final_state.get("messages") or []
        briefing = messages[-1] if messages else "(no message generated)"

        print("\n--- Morning Briefing ---\n")
        print(briefing)
        print("=" * 60)
