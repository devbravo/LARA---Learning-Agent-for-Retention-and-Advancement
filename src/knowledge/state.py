"""LangGraph state TypedDict for the Knowledge Agent graph."""

from typing import TypedDict


class KGState(TypedDict, total=False):
    topic: str
    chat_id: int
    synthesis_result: dict | None       # output of synthesize_concept_note
    pending_message_id: int | None      # message_id of the one active button message
    user_interest: str | None           # "approved" | "rejected" after confirm node
    write_result: dict | None           # output of write_concept_note on approved path
