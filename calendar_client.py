# Copyright (c) 2026 Planning Bot Contributors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Google Calendar API client — OAuth 2.0 authentication, event CRUD, and reminder
management.

Uses the ``google-api-python-client`` library to interact with the Google Calendar
v3 API. All authentication is handled via OAuth 2.0 with automatic token refresh.
The first-time setup requires running ``auth_calendar.py`` locally to generate a
``token.json`` file via a browser-based consent flow; thereafter the client refreshes
the token silently.

Environment variables:
    - ``GOOGLE_TOKEN_PATH``: Path to the OAuth token file (default ``token.json``).
    - ``GOOGLE_CREDENTIALS_PATH``: Path to the OAuth client secrets JSON downloaded
      from Google Cloud Console (default ``credentials.json``). Only needed during
      first-time auth.
    - ``GOOGLE_CALENDAR_ID``: The calendar to use (default ``primary``).
    - ``TIMEZONE``: Local timezone for event display (default ``Asia/Seoul``).
"""

import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from google.auth import exceptions as google_auth_exceptions
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

# Google may revoke a refresh token when:
#   1. The token hasn't been used for 6 months.
#   2. The user revokes access via their Google Account.
#   3. The OAuth consent screen settings are changed in Google Cloud Console.
#   4. The app is in "testing" mode and the token exceeds its 7-day lifetime.
#
# Recovery: run `python auth_calendar.py` locally, then copy the resulting
# token.json to the deployed environment.


class TokenRefreshError(RuntimeError):
    """Raised when the Google OAuth refresh token is invalid, revoked, or
    the refresh request fails for any reason. The original exception is
    attached as ``__cause__`` so the full traceback is preserved in logs."""


def _get_service():
    """Obtain an authenticated Google Calendar API service object.

    Credential lifecycle:
        1. Load ``token.json`` from ``GOOGLE_TOKEN_PATH``.
        2. If expired and a refresh token exists → attempt silent refresh.
        3. If refresh fails → raise ``TokenRefreshError``.
        4. If no token exists → run the local OAuth consent flow (requires a
           browser; used only during first-time setup).
        5. Persist the (refreshed or newly authorised) token back to disk.

    Returns:
        googleapiclient.discovery.Resource: A Calendar v3 API service instance.

    Raises:
        TokenRefreshError: The OAuth token could not be refreshed. Run
            ``auth_calendar.py`` to generate a new one.
        FileNotFoundError: ``credentials.json`` is missing and no token file
            exists.
    """
    import logging
    logger = logging.getLogger(__name__)

    creds = None
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        except Exception as exc:
            logger.warning("Failed to load token.json: %s", exc, exc_info=True)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Attempt silent refresh before falling back to full OAuth flow
            try:
                creds.refresh(Request())
                logger.info("Google token refreshed successfully.")
            except google_auth_exceptions.RefreshError as exc:
                # Covers invalid_grant, revoked token, expired token
                logger.error(
                    "Google OAuth refresh token is invalid or revoked. "
                    "Run `python auth_calendar.py` locally to generate a "
                    "new token.json, then deploy it to the server environment. "
                    "Underlying error: %s",
                    exc,
                    exc_info=True,
                )
                raise TokenRefreshError(
                    "Google OAuth refresh token is invalid or revoked. "
                    "Run auth_calendar.py to generate a new token.json and redeploy it."
                ) from exc
            except google_auth_exceptions.TransportError as exc:
                logger.error(
                    "Network error during Google OAuth token refresh. "
                    "Check server connectivity. Underlying error: %s",
                    exc,
                    exc_info=True,
                )
                raise TokenRefreshError(
                    "Network error during Google OAuth token refresh."
                ) from exc
            except google_auth_exceptions.GoogleAuthError as exc:
                logger.error(
                    "Google OAuth refresh failed with an unexpected error: %s",
                    exc,
                    exc_info=True,
                )
                raise TokenRefreshError(
                    "Unexpected Google OAuth error during token refresh."
                ) from exc
            except Exception as exc:
                logger.error(
                    "Unexpected error during Google OAuth token refresh: %s",
                    exc,
                    exc_info=True,
                )
                raise TokenRefreshError(
                    "Unexpected error during Google OAuth token refresh."
                ) from exc
        else:
            # No valid token at all — run the browser-based consent flow
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"credentials.json not found at '{CREDENTIALS_PATH}'. "
                    "Download it from Google Cloud Console and place it in "
                    "the project root."
                )
            flow  = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        # Persist the (possibly refreshed or newly authorised) token to disk
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _reminder_override(minutes: int) -> dict:
    """Build a reminders override dict that fires popup + email at *minutes* before.

    Args:
        minutes: Minutes before the event to trigger the reminder.

    Returns:
        dict: The Google Calendar API reminder-overrides body.
    """
    return {
        "useDefault": False,
        "overrides": [
            {"method": "popup",  "minutes": minutes},
            {"method": "email",  "minutes": minutes},
        ],
    }


def parse_event_start(event: dict) -> Optional[datetime]:
    """Parse a Google Calendar event's start time into a timezone-aware datetime.

    Handles both datetime events (with ``dateTime``) and all-day events (with
    ``date`` only). All-day events are returned as midnight in the local timezone.

    Args:
        event: A Google Calendar event dict from the API response.

    Returns:
        Optional[datetime]: A timezone-aware datetime, or ``None`` if parsing
            fails.
    """
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
    """Format a calendar event's start time for display.

    Example output: ``"Thu Jun 11, 2:00 PM"``.

    Args:
        event: A Google Calendar event dict.

    Returns:
        str: Human-readable start time string.
    """
    dt = parse_event_start(event)
    if dt:
        return dt.strftime("%a %b %d, %I:%M %p").lstrip("0")
    return event.get("start", {}).get("date", "?")


def fmt_event_time_range(event: dict) -> str:
    """Format a calendar event's start → end time range for display.

    Example output: ``"Thu Jun 11, 2:00 PM → 3:30 PM"``.

    For all-day events, only the start date is returned.

    Args:
        event: A Google Calendar event dict.

    Returns:
        str: Human-readable time range string.
    """
    start_dt = parse_event_start(event)
    if not start_dt:
        return event.get("start", {}).get("date", "?")
    start_str = start_dt.strftime("%a %b %d, %I:%M %p").lstrip("0")
    end = event.get("end", {})
    end_str = end.get("dateTime") or end.get("date")
    if not end_str or "T" not in end_str:
        return start_str  # all-day event, no end time
    try:
        import pytz
        tz = pytz.timezone(os.getenv("TIMEZONE", "Asia/Seoul"))
        end_dt = datetime.fromisoformat(end_str)
        if end_dt.tzinfo is None:
            end_dt = tz.localize(end_dt)
        else:
            end_dt = end_dt.astimezone(tz)
        end_fmt = end_dt.strftime("%I:%M %p").lstrip("0")
        return f"{start_str} → {end_fmt}"
    except Exception:
        return start_str


# ── Create ─────────────────────────────────────────────────────────────────────


def create_event(
    title: str,
    start: datetime,
    duration_minutes: int  = 60,
    description: str       = None,
    reminder_minutes: int  = 30,
    rrule: str             = None,
) -> str:
    """Create a calendar event (optionally recurring) and return its event ID.

    Args:
        title: Event title / summary.
        start: Timezone-aware start datetime.
        duration_minutes: Duration in minutes (default 60).
        description: Optional description text.
        reminder_minutes: Minutes before the event to fire popup + email
            reminders (default 30).
        rrule: Optional RRULE string for recurring events
            (e.g. ``"RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"``).

    Returns:
        str: The Google Calendar event ID assigned to the newly created event.
    """
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
    """Create a reminder-style calendar event (15-min popup block).

    The event is prefixed with 🔔 so it's visually distinct from regular events
    on the calendar. A popup fires at exactly the start time (0 min before).

    Args:
        title: Reminder text.
        remind_at: Timezone-aware trigger datetime.
        rrule: Optional RRULE for recurring reminders.

    Returns:
        str: The Google Calendar event ID.
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
    """Update an existing calendar event's title, start time, duration, or reminder.

    Only the fields that are provided are modified — others are preserved from
    the existing event.

    Args:
        event_id: The Google Calendar event ID to update.
        title: New event title (``None`` to keep current).
        start: New start datetime (``None`` to keep current).
        duration_minutes: New duration in minutes (only used if ``start`` is
            also provided).
        reminder_minutes: New reminder minutes before event (``None`` to keep
            current).
    """
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
    """Delete a calendar event by its ID.

    Args:
        event_id: The Google Calendar event ID to remove.
    """
    _get_service().events().delete(
        calendarId=CALENDAR_ID, eventId=event_id
    ).execute()


def search_events_by_title(query: str, days_ahead: int = 14) -> list:
    """Find upcoming calendar events whose title contains a substring.

    Performs a case-insensitive search over events in the next *days_ahead* days.

    Args:
        query: Substring to match against event summaries.
        days_ahead: How many days of events to search (default 14).

    Returns:
        list[dict]: Matching Google Calendar event dicts.
    """
    events = list_upcoming_events(days=days_ahead)
    q = query.lower().strip()
    return [e for e in events if q in e.get("summary", "").lower()]


def reschedule_event(event_id: str, new_start: datetime, duration_minutes: int = None):
    """Move an existing event to a new start time.

    If ``duration_minutes`` is not specified, the original event duration is
    preserved.

    Args:
        event_id: The Google Calendar event ID to reschedule.
        new_start: New timezone-aware start datetime.
        duration_minutes: New duration in minutes, or ``None`` to keep the
            original.

    Returns:
        int: The duration (in minutes) of the rescheduled event.
    """
    service = _get_service()
    event   = service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()

    # Preserve original duration if not overriding
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
    """Return (day_start_iso, day_end_iso) in UTC for today in the local timezone.

    Used internally to query the Calendar API for today's events without having
    to manually convert local midnight/midnight to UTC.

    Returns:
        tuple[str, str]: Two ISO 8601 strings (UTC, with trailing ``Z``).
    """
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
    """Return upcoming calendar events for the next *days* days.

    Args:
        days: Number of days to look ahead (default 7).

    Returns:
        List[dict]: Google Calendar event dicts ordered by start time.
    """
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
    """Return all calendar events for a specific day.

    Args:
        day: The date to query (``datetime.date``).

    Returns:
        List[dict]: Calendar events occurring on that day.
    """
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
    """Fetch a single calendar event by its ID.

    Args:
        event_id: The Google Calendar event ID.

    Returns:
        Optional[dict]: The event dict, or ``None`` if not found.
    """
    try:
        service = _get_service()
        return service.events().get(
            calendarId=CALENDAR_ID,
            eventId=event_id,
        ).execute()
    except Exception:
        return None


def get_todays_events() -> List[dict]:
    """Return all calendar events for today in the local timezone.

    Returns:
        List[dict]: Today's calendar events.
    """
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
    """Return events whose start time is within the next *window_minutes*.

    Used by the 1-min calendar reminder scheduler job to detect events that
    need popup notifications.

    Args:
        window_minutes: Look-ahead window in minutes (default 35).

    Returns:
        List[dict]: Events starting within the window.
    """
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