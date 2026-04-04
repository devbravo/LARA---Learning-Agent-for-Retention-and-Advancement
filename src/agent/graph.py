"""
LangGraph graph definition for the Learning Manager agent.

Flow:
  START → router → (conditional) → daily_briefing | study_picker | done_parser | output
  daily_briefing  → confirm → END
  study_picker    → brief_generator → confirm → END
  done_parser     → (conditional) → log_session | output
  log_session     → output → END
  output          → END

Checkpointer: SqliteSaver backed by db/state.db.
Thread ID: chat_id from state (one thread per user).
"""

import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env", override=True)

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from src.agent.nodes import (
    AgentState,
    brief_generator,
    calendar_reader,
    confirm,
    daily_briefing,
    done_parser,
    gap_finder,
    log_session,
    output,
    route_from_daily_briefing,
    route_from_done_parser,
    route_from_router,
    router,
    sm2_engine,
    study_picker,
)

_DB_DIR = Path(__file__).parents[2] / "db"
_STATE_DB_PATH = str(_DB_DIR / "state.db")


def build_graph(checkpointer=None):
    """
    Construct and compile the LangGraph StateGraph.

    Args:
        checkpointer: Optional LangGraph checkpointer. Defaults to SqliteSaver.
    """
    builder = StateGraph(AgentState)

    # Register all nodes
    builder.add_node("router", router)
    builder.add_node("daily_briefing", daily_briefing)
    builder.add_node("study_picker", study_picker)
    builder.add_node("done_parser", done_parser)
    builder.add_node("calendar_reader", calendar_reader)
    builder.add_node("sm2_engine", sm2_engine)
    builder.add_node("gap_finder", gap_finder)
    builder.add_node("brief_generator", brief_generator)
    builder.add_node("confirm", confirm)
    builder.add_node("log_session", log_session)
    builder.add_node("output", output)

    # Entry point
    builder.add_edge(START, "router")

    # Router → conditional branch
    builder.add_conditional_edges(
        "router",
        route_from_router,
        {
            "daily_briefing": "daily_briefing",
            "study_picker": "study_picker",
            "done_parser": "done_parser",
            "output": "output",
        },
    )

    # Main flows
    builder.add_conditional_edges(
        "daily_briefing",
        route_from_daily_briefing,
        {"confirm": "confirm", "output": "output"},
    )
    builder.add_edge("study_picker", "brief_generator")
    builder.add_edge("brief_generator", "confirm")
    builder.add_edge("confirm", END)

    # done flow
    builder.add_conditional_edges(
        "done_parser",
        route_from_done_parser,
        {
            "log_session": "log_session",
            "output": "output",
        },
    )
    builder.add_edge("log_session", "output")
    builder.add_edge("output", END)

    if checkpointer is None:
        _DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_STATE_DB_PATH, check_same_thread=False)
        checkpointer = SqliteSaver(conn)

    return builder.compile(checkpointer=checkpointer)


# Singleton graph for import by server / scheduler
graph = build_graph()


def invoke(trigger: str, chat_id: int, **kwargs) -> AgentState:
    """
    Convenience wrapper to invoke the graph.

    Args:
        trigger:  'daily' | 'study_picker' | 'done' | 'confirm'
        chat_id:  Telegram chat ID (used as LangGraph thread_id)
        **kwargs: Additional state fields (duration_min, messages, etc.)

    Returns the final AgentState.
    """
    initial_state: AgentState = {
        "trigger": trigger,
        "chat_id": chat_id,
        "duration_min": kwargs.get("duration_min"),
        "proposed_topic": kwargs.get("proposed_topic"),
        "proposed_slot": kwargs.get("proposed_slot"),
        "session_summary": kwargs.get("session_summary"),
        "quality_score": kwargs.get("quality_score"),
        "messages": kwargs.get("messages", []),
    }
    config = {"configurable": {"thread_id": str(chat_id)}}
    return graph.invoke(initial_state, config=config)


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
        from src.agent.nodes import _fmt_time
        slots = final_state.get("proposed_slots")
        if slots:
            for i, slot in enumerate(slots, 1):
                t = f"{_fmt_time(slot['start'])}–{_fmt_time(slot['end'])} ({slot['duration_min']}min)"
                print(f"  proposed_slots[{i}] : {slot['topic']} @ {t}")
        else:
            # study_picker fallback
            print(f"  proposed_topic : {final_state.get('proposed_topic')}")
            slot = final_state.get("proposed_slot")
            if slot:
                print(f"  proposed_slot  : {_fmt_time(slot['start'])}–{_fmt_time(slot['end'])} ({slot['duration_min']}min)")
        print("=" * 60)
