"""
LangGraph graph definition for the Learning Manager agent.

Flow:
  START → router → (conditional) → daily_planning | on_demand | done_parser | output
  daily_planning  → confirm → END
  on_demand       → generate_brief → confirm → END
  done_parser     → END  (sends rating buttons directly, waits for tap via webhook)
  rate trigger    → log_session → output → END  (log_session sends weak-areas prompt)
  weak_areas      → log_weak_areas → output → END
  output          → END

Checkpointer: SqliteSaver backed by db/state.db.
Thread ID: chat_id from state (one thread per user).
"""

import sqlite3
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env", override=True)

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from src.agent.nodes import (
    AgentState,
    generate_brief,
    confirm,
    daily_planning,
    weekend_brief,
    done_parser,
    log_session,
    log_weak_areas,
    output,
    route_from_daily_planning,
    route_from_router,
    router,
    on_demand,
    study_topic,
    study_topic_category,
    study_topic_confirm,
)

_DB_DIR = Path(__file__).parents[2] / "db"
_STATE_DB_PATH = str(_DB_DIR / "state.db")


def build_graph(checkpointer=None):
    """
    Construct and compile the LangGraph StateGraph.

    Args:
        checkpointer: Optional LangGraph checkpointer. Defaults to SqliteSaver.
    """
    builder: Any = StateGraph(cast(Any, AgentState))

    # Register all nodes
    builder.add_node("router", router)
    builder.add_node("daily_planning", daily_planning)
    builder.add_node("weekend_brief", weekend_brief)
    builder.add_node("on_demand", on_demand)
    builder.add_node("done_parser", done_parser)
    builder.add_node("generate_brief", generate_brief)
    builder.add_node("confirm", confirm)
    builder.add_node("log_session", log_session)
    builder.add_node("log_weak_areas", log_weak_areas)
    builder.add_node("output", output)
    builder.add_node("study_topic", study_topic)
    builder.add_node("study_topic_category", study_topic_category)
    builder.add_node("study_topic_confirm", study_topic_confirm)

    # Entry point
    builder.add_edge(START, "router")

    # Router → conditional branch
    builder.add_conditional_edges(
        "router",
        route_from_router,
        {
            "daily_planning":       "daily_planning",
            "weekend_brief":        "weekend_brief",
            "on_demand":            "on_demand",
            "done_parser":          "done_parser",
            "output":               "output",
            "log_session":          "log_session",
            "log_weak_areas":       "log_weak_areas",
            "study_topic":          "study_topic",
            "study_topic_category": "study_topic_category",
            "study_topic_confirm":  "study_topic_confirm",
        },
    )

    # Main flows
    builder.add_conditional_edges(
        "daily_planning",
        route_from_daily_planning,
        {"confirm": "confirm", "output": "output"},
    )

    builder.add_edge("weekend_brief", "output")
    builder.add_edge("on_demand", "generate_brief")
    builder.add_edge("generate_brief", "confirm")
    builder.add_edge("confirm", END)

    # done flow
    builder.add_edge("done_parser", END)
    builder.add_edge("log_session", "output")
    builder.add_edge("log_weak_areas", "output")
    builder.add_edge("output", END)

    # study_topic flow
    builder.add_edge("study_topic", END)
    builder.add_edge("study_topic_category", END)
    builder.add_edge("study_topic_confirm", END)


    if checkpointer is None:
        _DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_STATE_DB_PATH, check_same_thread=False)
        checkpointer = SqliteSaver(conn)

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


def invoke(trigger: str, chat_id: int, **kwargs) -> AgentState:
    """
    Convenience wrapper to invoke the graph.

    Args:
        trigger:  'daily' | 'on_demand' | 'done' | 'confirm'
        chat_id:  Telegram chat ID (used as LangGraph thread_id)
        **kwargs: Additional state fields (duration_min, messages, etc.)

    Returns the final AgentState.
    """
    initial_state: AgentState = {
        "trigger": trigger,
        "chat_id": chat_id,
    }
    initial_state_dict: Any = initial_state
    # Only include kwargs that are explicitly provided — don't overwrite
    # checkpointed state with None values
    for key in ("message_id", "duration_min", "proposed_topic", "proposed_slot",
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

    # Patch Telegram sends so nothing goes to the bot during dry run
    with patch("src.integrations.telegram_client.send_message") as mock_msg, \
         patch("src.integrations.telegram_client.send_buttons") as mock_btn:

        final_state = invoke(trigger="daily", chat_id=CHAT_ID)

        messages = final_state.get("messages") or []
        briefing = messages[-1] if messages else "(no message generated)"

        print("\n--- Morning Briefing ---\n")
        print(briefing)
        print("\n--- State ---")
        from src.agent.formatting import format_time
        slots = final_state.get("proposed_slots")
        if slots:
            for i, slot in enumerate(slots, 1):
                t = f"{format_time(slot['start'])}–{format_time(slot['end'])} ({slot['duration_min']}min)"
                print(f"  proposed_slots[{i}] : {slot['topic']} @ {t}")
        else:
            # on_demand fallback
            print(f"  proposed_topic : {final_state.get('proposed_topic')}")
            slot = final_state.get("proposed_slot")
            if slot:
                print(f"  proposed_slot  : {format_time(slot['start'])}–{format_time(slot['end'])} ({slot['duration_min']}min)")
        print("=" * 60)
