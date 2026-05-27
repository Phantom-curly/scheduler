"""
Google Calendar client — CRUD, recurring events, reminders, reading events.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES           = ["https://www.googleapis.com/auth/calendar"]
TOKEN_PATH       = os.getenv("GOOGLE_TOKEN_PATH",       "token.json")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
CALENDAR_ID      = os.getenv("GOOGLE_CALENDAR_ID",      "primary")
TIMEZONE         = os.getenv("TIMEZONE",                "Asia/Seoul")


# ── Auth ───────────────────────────────────────────────────────────────────────

def _get_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"credentials.json not found at '{CREDENTIALS_PATH}'."
                )
            flow  = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _reminder_override(minutes: int) -> dict:
    return {
        "useDefault": False,
        "overrides": [
            {"method": "popup",  "minutes": minutes},
            {"method": "email",  "minutes": minutes},
        ],
    }


def parse_event_start(event: dict) -> Optional[datetime]:
    """Parse a Google Calendar event's start time into a timezone-aware datetime."""
    import pytz
    tz       = pytz.timezone(os.getenv("TIMEZONE", "Asia/Seoul"))
    start    = event.get("start", {})
    dt_str   = start.get("dateTime") or start.get("date")
    if not dt_str:
        return None
    try:
        if "T" in dt_str:
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
                dt = tz.localize(dt)
            else:
                dt = dt.astimezone(tz)
            return dt
        else:
            # All-day event — return as midnight in local tz
            d  = datetime.fromisoformat(dt_str)
            return tz.localize(d)
    except Exception:
        return None


def fmt_event_time(event: dict) -> str:
    dt = parse_event_start(event)
    if dt:
        return dt.strftime("%a %b %d, %I:%M %p").lstrip("0")
    return event.get("start", {}).get("date", "?")


# ── Create ─────────────────────────────────────────────────────────────────────

def create_event(
    title: str,
    start: datetime,
    duration_minutes: int  = 60,
    description: str       = None,
    reminder_minutes: int  = 30,
    rrule: str             = None,
) -> str:
    """Create a calendar event (optionally recurring) and return its event ID."""
    service = _get_service()
    end     = start + timedelta(minutes=duration_minutes)

    body = {
        "summary":   title,
        "start":     {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
        "end":       {"dateTime": end.isoformat(),   "timeZone": TIMEZONE},
        "reminders": _reminder_override(reminder_minutes),
    }
    if description:
        body["description"] = description
    if rrule:
        body["recurrence"] = [rrule]

    event = service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
    return event["id"]


# ── Update ─────────────────────────────────────────────────────────────────────

def update_event(
    event_id: str,
    title: str            = None,
    start: datetime       = None,
    duration_minutes: int = None,
    reminder_minutes: int = None,
):
    service = _get_service()
    event   = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()

    if title:
        event["summary"] = title
    if start:
        dur = duration_minutes or 60
        end = start + timedelta(minutes=dur)
        event["start"] = {"dateTime": start.isoformat(), "timeZone": TIMEZONE}
        event["end"]   = {"dateTime": end.isoformat(),   "timeZone": TIMEZONE}
    if reminder_minutes is not None:
        event["reminders"] = _reminder_override(reminder_minutes)

    service.events().update(
        calendarId=CALENDAR_ID, eventId=event_id, body=event
    ).execute()


# ── Delete ─────────────────────────────────────────────────────────────────────

def delete_event(event_id: str):
    _get_service().events().delete(
        calendarId=CALENDAR_ID, eventId=event_id
    ).execute()


# ── Read ───────────────────────────────────────────────────────────────────────

def list_upcoming_events(days: int = 7) -> List[dict]:
    service  = _get_service()
    now      = datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + timedelta(days=days)).isoformat() + "Z"

    result = service.events().list(
        calendarId  = CALENDAR_ID,
        timeMin     = time_min,
        timeMax     = time_max,
        singleEvents= True,
        orderBy     = "startTime",
    ).execute()
    return result.get("items", [])


def get_todays_events() -> List[dict]:
    service  = _get_service()
    now      = datetime.utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end   = now.replace(hour=23, minute=59, second=59, microsecond=0)

    result = service.events().list(
        calendarId  = CALENDAR_ID,
        timeMin     = day_start.isoformat() + "Z",
        timeMax     = day_end.isoformat()   + "Z",
        singleEvents= True,
        orderBy     = "startTime",
    ).execute()
    return result.get("items", [])


def get_events_starting_soon(window_minutes: int = 35) -> List[dict]:
    """Return events whose start time is within the next window_minutes."""
    service  = _get_service()
    now      = datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + timedelta(minutes=window_minutes)).isoformat() + "Z"

    result = service.events().list(
        calendarId  = CALENDAR_ID,
        timeMin     = time_min,
        timeMax     = time_max,
        singleEvents= True,
        orderBy     = "startTime",
    ).execute()
    return result.get("items", [])