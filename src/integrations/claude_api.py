import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env", override=True)

_SYSTEM_PROMPT = "You are a focused study coach. Generate a concise study brief."


def _get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("Missing required env var: ANTHROPIC_API_KEY")
    return anthropic.Anthropic(api_key=api_key)


def generate_brief(topic: str, duration_min: int, context: str) -> str:
    """Generate a focused study brief for a given topic and session duration."""
    client = _get_client()
    prompt = (
        f"Topic: {topic}\n"
        f"Session duration: {duration_min} minutes\n"
        f"Context: {context}\n\n"
        "Generate a concise study brief for this session."
    )
    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        raise RuntimeError(f"Claude API error: {e}") from e

    return message.content[0].text


if __name__ == "__main__":
    brief = generate_brief(
        topic="System Design",
        duration_min=45,
        context="Practice questions",
    )
    print(brief)
