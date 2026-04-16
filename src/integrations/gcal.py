import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env", override=True)

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_CREDENTIALS_PATH = Path(
    os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials/gcal_credentials.json")
)
_TOKEN_PATH = Path("credentials/token.json")


def _get_service():
    required = {"GOOGLE_CALENDAR_ID", "GOOGLE_CREDENTIALS_PATH"}
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")

    if not _CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Google credentials file not found at {_CREDENTIALS_PATH}. "
            "Download it from Google Cloud Console → APIs & Services → Credentials."
        )

    creds = None
    if _TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(_TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(_CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_PATH.write_text(creds.to_json())

    return build("calendar", "v3", credentials=creds)

def _should_skip_event(item: dict) -> bool:
    """Return True if the authenticated user declined or is tentative on this event."""
    attendees = item.get("attendees", [])
    if not attendees:
        return False
    for attendee in attendees:
        if attendee.get("self"):
            return attendee.get("responseStatus") in ("declined", "tentative")
    return False


def get_events(day: date) -> list[dict]:
    """Fetch all events for a given day from GOOGLE_CALENDAR_ID."""
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]

    time_min = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=timezone.utc).isoformat()
    time_max = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc).isoformat()

    try:
        service = _get_service()
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except HttpError as e:
        raise RuntimeError(f"Google Calendar API error {e.resp.status}: {e.reason}") from e

    events = []
    for item in result.get("items", []):
        if _should_skip_event(item):
            continue
        events.append({
            "id": item.get("id"),
            "summary": item.get("summary", "(No title)"),
            "start": item.get("start", {}),
            "end": item.get("end", {}),
            "creator": item.get("creator", {}),
        })
    return events


def write_event(topic: str, start: str, end: str) -> dict:
    """
    Create a new [Mock] event on GOOGLE_CALENDAR_ID.

    Args:
        topic: Topic name — prefixed with '[Mock]' automatically.
        start: ISO-8601 datetime string (e.g. '2026-04-03T09:00:00').
        end:   ISO-8601 datetime string.

    Returns the created event dict (id, summary, start, end, creator).
    """
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
    body = {
        "summary": f"[Mock] {topic}",
        "description": "Booked by LARA - Personal Learning Assistant",
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
    }
    try:
        service = _get_service()
        created = service.events().insert(calendarId=calendar_id, body=body).execute()
    except HttpError as e:
        raise RuntimeError(f"Google Calendar API error {e.resp.status}: {e.reason}") from e

    return {
        "id": created.get("id"),
        "summary": created.get("summary"),
        "start": created.get("start", {}),
        "end": created.get("end", {}),
        "creator": created.get("creator", {}),
    }


if __name__ == "__main__":
    events = get_events(date.today())
    print(json.dumps(events, indent=2))
