"""MCP server exposing LARA study tools to Claude.ai."""

from typing import Any
from mcp.server.fastmcp import FastMCP
from src.repositories import session_repository, topic_repository
from src.services import discuss_service

mcp = FastMCP("LARA", stateless_http=True)


@mcp.tool(
    description=(
        "Retrieve SM-2 state and last session signal for a LARA topic by name. "
        "Call this automatically at the start of every /mock session before "
        "doing anything else. Use the returned weak_areas and student_quality "
        "to structure the mock — if weak_areas exist, target those areas first."
    )
)
def get_topic_context(topic_name: str) -> dict[str, Any]:
    topic_id = topic_repository.get_topic_id_by_name(topic_name)
    if topic_id is None:
        raise ValueError(f"Topic not found: {topic_name}")
    return topic_repository.get_topic_context(topic_id)


@mcp.tool(
    description=(
        "Log your assessment of Diego's performance after a session. Call this "
        "automatically after delivering /feedback — do not wait to be asked. "
        "Use teacher_quality 5 for easy, 3 for medium, 2 for hard. Populate "
        "teacher_weak_areas using the schema for the topic_type returned by "
        "get_topic_context."
    )
)
def log_session(
    topic_name: str,
    teacher_quality: int,
    teacher_weak_areas: dict,
    mode: str,
    teacher_source: str = "claude",
) -> dict[str, Any]:
    if teacher_quality not in {2, 3, 5}:
        raise ValueError("teacher_quality must be 2, 3, or 5")
    topic_id = topic_repository.get_topic_id_by_name(topic_name)
    if topic_id is None:
        raise ValueError(f"Topic not found: {topic_name}")
    calibration_gap = session_repository.log_teacher_session(
        topic_id, teacher_quality, teacher_weak_areas, teacher_source, mode
    )
    return {"success": True, "calibration_gap": calibration_gap}


@mcp.tool(
    description=(
        "Retrieve discuss-session history and mock-session history for a topic. "
        "Call this automatically at the start of every /discuss session before "
        "doing anything else. Use weak_areas and discuss_history to structure "
        "the discussion — target recurring gaps from previous sessions first."
    )
)
def get_discuss_context(topic_name: str) -> dict[str, Any]:
    return discuss_service.get_discuss_context(topic_name)


@mcp.tool(
    description=(
        "Log a discuss-session assessment and evaluate whether the topic is ready "
        "for a mock session. Call this automatically after delivering /discuss "
        "feedback — do not wait to be asked. "
        "Use teacher_quality 5 for strong, 3 for developing, 2 for weak. "
        "Pass teacher_weak_areas as a JSON string mapping dimension names to "
        "observations (e.g. '{\"critical_thinking\": \"conflated X with Y\"}'). "
        "The tool inserts the session, detects repeated gaps, and sends Diego a "
        "Telegram notification with the readiness verdict."
    )
)
def assess_discuss_readiness(
    topic_name: str,
    teacher_quality: int,
    teacher_weak_areas: str,
) -> dict[str, Any]:
    return discuss_service.assess_discuss_readiness(
        topic_name, teacher_quality, teacher_weak_areas
    )


mcp_app = mcp.streamable_http_app()
