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


def create_reminder(title: str, remind_at: datetime, rrule: str = None) -> str:
    """
    Create a reminder-style calendar event:
    - 15 min duration
    - Popup fires at exactly the start time (0 min before)
    - Prefixed with 🔔 so it's visually distinct
    """
    service = _get_service()
    end     = remind_at + timedelta(minutes=15)
    body = {
        "summary":   f"🔔 {title}",
        "start":     {"dateTime": remind_at.isoformat(), "timeZone": TIMEZONE},
        "end":       {"dateTime": end.isoformat(),       "timeZone": TIMEZONE},
        "reminders": {
            "useDefault": False,
            "overrides":  [{"method": "popup", "minutes": 0}],
        },
    }
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


def search_events_by_title(query: str, days_ahead: int = 14) -> list:
    """Find calendar events whose title contains query (case-insensitive)."""
    events = list_upcoming_events(days=days_ahead)
    q = query.lower().strip()
    return [e for e in events if q in e.get("summary", "").lower()]


def reschedule_event(event_id: str, new_start: datetime, duration_minutes: int = None):
    """Move an existing event to a new start time, preserving duration if not specified."""
    service = _get_service()
    event   = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()

    # Calculate original duration if not overriding
    if duration_minutes is None:
        try:
            orig_start = datetime.fromisoformat(event["start"]["dateTime"])
            orig_end   = datetime.fromisoformat(event["end"]["dateTime"])
            duration_minutes = int((orig_end - orig_start).total_seconds() / 60)
        except Exception:
            duration_minutes = 60

    new_end = new_start + timedelta(minutes=duration_minutes)
    event["start"] = {"dateTime": new_start.isoformat(), "timeZone": TIMEZONE}
    event["end"]   = {"dateTime": new_end.isoformat(),   "timeZone": TIMEZONE}

    service.events().update(
        calendarId=CALENDAR_ID, eventId=event_id, body=event
    ).execute()
    return duration_minutes


# ── Read ───────────────────────────────────────────────────────────────────────

def _local_day_bounds_utc() -> tuple:
    """Return (day_start_iso, day_end_iso) in UTC for today in local TIMEZONE."""
    import pytz
    tz        = pytz.timezone(TIMEZONE)
    local_now = datetime.now(tz)
    day_start = local_now.replace(hour=0,  minute=0,  second=0,  microsecond=0)
    day_end   = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
    day_start_utc = day_start.astimezone(pytz.utc)
    day_end_utc   = day_end.astimezone(pytz.utc)
    return (
        day_start_utc.isoformat().replace("+00:00", "Z"),
        day_end_utc.isoformat().replace("+00:00", "Z"),
    )


def list_upcoming_events(days: int = 7) -> List[dict]:
    from datetime import timezone
    service  = _get_service()
    now      = datetime.now(timezone.utc)
    time_min = now.isoformat().replace("+00:00", "Z")
    time_max = (now + timedelta(days=days)).isoformat().replace("+00:00", "Z")

    result = service.events().list(
        calendarId  = CALENDAR_ID,
        timeMin     = time_min,
        timeMax     = time_max,
        singleEvents= True,
        orderBy     = "startTime",
    ).execute()
    return result.get("items", [])


def list_events_by_day(day: datetime.date) -> List[dict]:
    """Return all calendar events for a specific day."""
    import pytz
    service  = _get_service()
    tz       = pytz.timezone(TIMEZONE)
    day_start = tz.localize(datetime(day.year, day.month, day.day, 0, 0, 0))
    day_end   = tz.localize(datetime(day.year, day.month, day.day, 23, 59, 59))

    result = service.events().list(
        calendarId  = CALENDAR_ID,
        timeMin     = day_start.astimezone(pytz.utc).isoformat().replace("+00:00", "Z"),
        timeMax     = day_end.astimezone(pytz.utc).isoformat().replace("+00:00", "Z"),
        singleEvents= True,
        orderBy     = "startTime",
    ).execute()
    return result.get("items", [])


def get_event(event_id: str) -> Optional[dict]:
    """Fetch a single event by its ID. Returns None if not found."""
    try:
        service = _get_service()
        return service.events().get(
            calendarId=CALENDAR_ID,
            eventId=event_id,
        ).execute()
    except Exception:
        return None


def get_todays_events() -> List[dict]:
    service                    = _get_service()
    day_start_iso, day_end_iso = _local_day_bounds_utc()

    result = service.events().list(
        calendarId  = CALENDAR_ID,
        timeMin     = day_start_iso,
        timeMax     = day_end_iso,
        singleEvents= True,
        orderBy     = "startTime",
    ).execute()
    return result.get("items", [])


def get_events_starting_soon(window_minutes: int = 35) -> List[dict]:
    """Return events whose start time is within the next window_minutes."""
    from datetime import timezone
    service  = _get_service()
    now      = datetime.now(timezone.utc)
    time_min = now.isoformat().replace("+00:00", "Z")
    time_max = (now + timedelta(minutes=window_minutes)).isoformat().replace("+00:00", "Z")

    result = service.events().list(
        calendarId  = CALENDAR_ID,
        timeMin     = time_min,
        timeMax     = time_max,
        singleEvents= True,
        orderBy     = "startTime",
    ).execute()
    return result.get("items", [])