"""Presentation layer for the Learning Manager agent.

All user-facing strings and button label lists live here.
Nodes call these functions to get (text, buttons) tuples and
then pass the results straight to the telegram client.

No external I/O happens in this module — every function is pure.

HTML-escaping convention
------------------------
``telegram_client`` always sends with ``parse_mode="HTML"``.  Any value
interpolated into a message string that originates from user input or DB
free-text (topic names, weak-area labels, etc.) **must** be wrapped with
``html.escape()`` to prevent broken or injected markup.
"""

import html as _html

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

ButtonList = list[str]
InlineButtonList = list[tuple[str, str]]


# ---------------------------------------------------------------------------
# Duration picker  (send_duration_picker)
# ---------------------------------------------------------------------------

def duration_picker() -> tuple[str, ButtonList]:
    """Prompt and button options for the study-session duration picker."""
    return "How long do you have?", ["30 min", "45 min", "60 min"]


# ---------------------------------------------------------------------------
# On-demand  (on_demand)
# ---------------------------------------------------------------------------

def generating_brief(topic_name: str, duration_min: int) -> str:
    """Status message while the brief is being generated."""
    return f"📚 Generating a {duration_min} min brief for {topic_name}…"


def nothing_due() -> str:
    """Message when there are no topics due for review."""
    return "🎉 Nothing due for review right now — enjoy your break!"


# ---------------------------------------------------------------------------
# Brief confirmation  (generate_brief)
# ---------------------------------------------------------------------------

#: Button labels reused across daily planning and brief confirmation.
BOOKING_BUTTONS: ButtonList = ["Yes, book them", "Skip"]


# ---------------------------------------------------------------------------
# Book events  (book_events)
# ---------------------------------------------------------------------------

def booked_sessions(booked_study: list[str], booked_mock: list[str]) -> str:
    """Confirmation message listing all newly booked sessions."""
    parts: list[str] = []
    if booked_study:
        lines = "\n".join(f"  • {t}" for t in booked_study)
        parts.append(f"📚 Booked {len(booked_study)} study session(s):\n{lines}")
    if booked_mock:
        lines = "\n".join(f"  • {t}" for t in booked_mock)
        parts.append(f"🎯 Booked {len(booked_mock)} mock session(s):\n{lines}")
    return "\n\n".join(parts)


#: Shown when Google Calendar is unavailable and no events could be written.
BOOKING_FAILED = (
    "⚠️ Could not book any sessions — Google Calendar may be unavailable. "
    "Please try confirming again."
)


# ---------------------------------------------------------------------------
# Done flow  (done_parser / select_done_topic)
# ---------------------------------------------------------------------------

def topic_picker(topics: list[dict]) -> tuple[str, InlineButtonList]:
    """Inline picker asking the user which topic they just finished."""
    return "Which topic did you just finish?", [(t["name"], t["name"]) for t in topics]


def rating_prompt(topic_name: str) -> tuple[str, ButtonList]:
    """Rating buttons after a study session."""
    return f"How did {topic_name} go?", ["😕 Hard", "😐 OK", "😊 Easy"]


def no_sessions_to_log() -> str:
    """Shown when /done is triggered but nothing is unlogged."""
    return "No active sessions to log right now."


# ---------------------------------------------------------------------------
# Weak areas — Q1 prompts  (log_session)
# ---------------------------------------------------------------------------

def weak_areas_q1_dsa() -> tuple[str, InlineButtonList]:
    """Q1 inline-button prompt for DSA topics."""
    return "What broke down?", [
        ("Edge case", "Edge case"),
        ("Time complexity", "Time complexity"),
        ("Implementation", "Implementation"),
        ("All of the above", "All of the above"),
        ("Nothing", "Nothing"),
    ]


def weak_areas_q1_system_design() -> tuple[str, ButtonList]:
    """Q1 text prompt for system-design topics."""
    return "Describe the scenario briefly, or tap Skip.", ["Skip"]


def weak_areas_q1_conceptual() -> tuple[str, ButtonList]:
    """Q1 text prompt for conceptual topics."""
    return "What couldn't you answer? or tap Skip", ["Skip"]


def weak_areas_q1_behavioral() -> tuple[str, ButtonList]:
    """Q1 text prompt for behavioral topics."""
    return "Which story did you practice? or tap Skip.", ["Skip"]


# ---------------------------------------------------------------------------
# Weak areas — Q2 prompts  (log_weak_areas)
# ---------------------------------------------------------------------------

def weak_areas_q2_dsa() -> tuple[str, ButtonList]:
    """Q2 text prompt for DSA topics (problem names)."""
    return "Which problems did you solve? (e.g Two Sum, Valid Parentheses)", ["Skip"]


def weak_areas_q2_system_design() -> tuple[str, InlineButtonList]:
    """Q2 inline-button prompt for system-design topics."""
    return "What felt weak?", [
        ("Scalability", "Scalability"),
        ("Data pipeline", "Data pipeline"),
        ("Trade-offs", "Trade-offs"),
        ("Estimation", "Estimation"),
        ("Component selection", "Component selection"),
        ("Latency vs throughput", "Latency vs throughput"),
        ("All of the above", "All of the above"),
        ("Nothing", "Nothing"),
    ]


def weak_areas_q2_behavioral() -> tuple[str, InlineButtonList]:
    """Q2 inline-button prompt for behavioral topics."""
    return "What felt weak?", [
        ("Delivery", "Delivery"),
        ("Quantification", "Quantification"),
        ("Structure", "Structure"),
        ("All of the above", "All of the above"),
        ("Nothing", "Nothing"),
    ]


# ---------------------------------------------------------------------------
# Completion messages  (log_weak_areas / log_weak_areas_q2)
# ---------------------------------------------------------------------------

def completion_all_done(topic_name: str) -> str:
    """Shown when all planned topics for today have been logged."""
    return f"✅ {topic_name} logged. All done for today! 💪"


def completion_still_unlogged(topic_name: str, remaining: list[dict]) -> str:
    """Shown when some planned topics are still unlogged."""
    bullet_list = "\n".join(f"• {t['name']}" for t in remaining)
    return f"✅ {topic_name} logged. Still unlogged:\n{bullet_list}\n\nPress /done when you're ready."


# ---------------------------------------------------------------------------
# Pick flow  (study_topic / study_topic_category / study_topic_confirm)
# ---------------------------------------------------------------------------

#: Inline picker asking the user to choose a topic category.
CATEGORY_PICKER_PROMPT = "Which category?"

#: Inline picker asking the user to choose a specific topic.
TOPIC_PICKER_PROMPT = "Which topic?"

#: Fallback shown when the user types a command instead of tapping a button.
CATEGORY_PICKER_FALLBACK = "Please choose a category using the buttons."

#: Fallback shown in start_discuss when the user types a command instead of tapping a button.
DISCUSS_PICKER_FALLBACK = "Please use the buttons to select a topic for this discuss session."


def no_inactive_topics() -> str:
    """Shown when /pick is triggered but no inactive topics are available."""
    return "No inactive topics available to start studying."


def topic_added_to_in_progress(topic_name: str) -> str:
    """Confirmation after a topic is moved to in-progress."""
    return (
        f"✅ {topic_name} added to In Progress. "
        "It will be booked on your calendar tomorrow morning."
    )


# ---------------------------------------------------------------------------
# Activate / graduate flow  (activate_topic / graduate_topic)
# ---------------------------------------------------------------------------

#: Inline picker asking the user which in-progress topic they studied.
ACTIVATE_TOPIC_PROMPT = "Which topic are you ready to be tested on?"


def no_topics_in_progress() -> str:
    """Shown when /activate is triggered but nothing is in-progress."""
    return "No topics currently in progress."


def topic_graduated(topic_name: str) -> str:
    """Confirmation after a topic is graduated to active SM-2 review."""
    return (
        f"✅ {topic_name} graduated to active. "
        "First SM-2 review scheduled for tomorrow."
    )


# ---------------------------------------------------------------------------
# Discuss-mode readiness  (assess_discuss_readiness)
# ---------------------------------------------------------------------------

def discuss_ready(topic_name: str, is_reentry: bool = False) -> str:
    """Readiness notification after a strong discuss session.

    Args:
        topic_name: Display name of the topic.
        is_reentry: ``True`` when the topic already has prior mock history
            (i.e. it is returning through discuss rather than graduating for
            the first time).  Re-entry topics are already active in SM-2 so
            the activation step does not apply.

    Returns:
        Plain-text notification string (no buttons — the activation flow is
        handled separately via ``/activate``).
    """
    name = _html.escape(topic_name)
    if is_reentry:
        return (
            f"✅ {name} looks strong again — no repeated gaps and quality is solid. "
            "Ready for a mock session whenever you are."
        )
    return (
        f"✅ {name} looks ready for its first mock — no repeated gaps and quality is strong. "
        "Use /activate when you're ready to move it into SM-2."
    )


def discuss_not_ready(topic_name: str, weak_areas: list[str]) -> str:
    """Message when quality or gaps are insufficient for a mock session."""
    if weak_areas:
        bullet_list = "\n".join(f"  • {_html.escape(area)}" for area in weak_areas)
        return (
            f"📖 {_html.escape(topic_name)} isn't ready yet. Focus on these areas before the next discuss:\n"
            f"{bullet_list}"
        )
    return f"📖 {_html.escape(topic_name)} isn't ready yet. Keep discussing before moving to mock."


def discuss_session_ready(
    topic_name: str,
    topic_type: str,
    weak_areas: list[str],
    session_number: int,
) -> str:
    """Message sent when a discuss session is initiated for a topic.

    Args:
        topic_name: Display name of the topic.
        topic_type: Raw topic type string (e.g. ``"dsa"``, ``"system_design"``).
        weak_areas: Parsed list of focus-area labels from the topic's weak_areas
            field.  Pass an empty list when no prior gaps are recorded.
        session_number: 1-indexed session counter (prior sessions + 1).

    Returns:
        Plain-text notification string with HTML formatting.
    """
    name = _html.escape(topic_name)
    type_label = {
        "dsa": "DSA",
        "system_design": "System Design",
        "conceptual": "Conceptual",
        "behavioral": "Behavioral",
    }.get(topic_type, _html.escape(topic_type.replace("_", " ").title()))
    header = (
        f"📝 Discuss session started — <b>{name}</b>\n"
        f"Session #{session_number} | {type_label}"
    )
    if weak_areas:
        gaps = ", ".join(_html.escape(a) for a in weak_areas)
        return f"{header}\nFocus areas: {gaps}"
    return header


def discuss_go_back_to_study(
    topic_name: str,
    repeated_weak_areas: list[str],
    was_moved: bool = True,
) -> str:
    """Message when repeated gaps indicate the topic needs more study.

    Args:
        topic_name: Display name of the topic.
        repeated_weak_areas: Dimension names that recurred across sessions.
        was_moved: ``True`` when the topic status was successfully changed to
            ``in_progress``.  ``False`` when the topic was already ``active``
            (or ``in_progress``) so no status change occurred — the message
            is adjusted to avoid falsely claiming the topic was moved.
    """
    name = _html.escape(topic_name)
    bullet_list = "\n".join(f"  • {_html.escape(area)}" for area in repeated_weak_areas)
    if was_moved:
        return (
            f"🔄 {name} moved back to In Progress — the following gaps are recurring "
            f"and need more study before discussing again:\n{bullet_list}"
        )
    return (
        f"⚠️ {name} has recurring gaps that need more study before the next discuss session:\n"
        f"{bullet_list}"
    )
