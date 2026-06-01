"""
Smart scheduling — finds free slots in Google Calendar and suggests times.

When a user wants to schedule something but hasn't specified a time,
instead of just asking "when?", the bot checks the calendar and suggests
available windows.
"""

import os
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

import pytz
import requests
import json
import re

import calendar_client

logger    = logging.getLogger(__name__)
TIMEZONE  = os.getenv("TIMEZONE", "Asia/Seoul")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL     = "google/gemini-2.5-flash-lite"
API_URL   = "https://openrouter.ai/api/v1/chat/completions"


# ── Free slot finder ──────────────────────────────────────────────────────────

def get_free_slots(
    days_ahead:       int = 5,
    min_duration_min: int = 30,
    work_start_hour:  int = 8,
    work_end_hour:    int = 23,
) -> List[Dict]:
    """
    Returns list of free slots in the next `days_ahead` days.
    Each slot: {start: datetime, end: datetime, duration_minutes: int}
    """
    tz       = pytz.timezone(TIMEZONE)
    now      = datetime.now(tz)
    end_scan = now + timedelta(days=days_ahead)

    # Fetch all events in range
    try:
        events = calendar_client.list_upcoming_events(days=days_ahead)
    except Exception as exc:
        logger.warning(f"smart_schedule: calendar fetch failed: {exc}")
        return []

    # Build busy blocks
    busy: List[Tuple[datetime, datetime]] = []
    for e in events:
        start = calendar_client.parse_event_start(e)
        if not start:
            continue
        if start.tzinfo is None:
            start = tz.localize(start)

        # Get end time
        end_raw = e.get("end", {})
        end_str = end_raw.get("dateTime") or end_raw.get("date")
        if end_str:
            try:
                end = datetime.fromisoformat(end_str)
                if end.tzinfo is None:
                    end = tz.localize(end)
                else:
                    end = end.astimezone(tz)
            except Exception:
                end = start + timedelta(hours=1)
        else:
            end = start + timedelta(hours=1)

        busy.append((start, end))

    busy.sort(key=lambda x: x[0])

    # Find free slots day by day
    free_slots = []
    current_day = now.date()

    for day_offset in range(days_ahead):
        day       = current_day + timedelta(days=day_offset)
        day_start = tz.localize(datetime(day.year, day.month, day.day, work_start_hour, 0))
        day_end   = tz.localize(datetime(day.year, day.month, day.day, work_end_hour,   0))

        # Don't look before now
        slot_start = max(day_start, now + timedelta(minutes=30))
        if slot_start >= day_end:
            continue

        # Walk through busy blocks for this day
        day_busy = [(s, e) for s, e in busy if s.date() == day]

        cursor = slot_start
        for busy_start, busy_end in day_busy:
            if cursor < busy_start:
                duration = int((busy_start - cursor).total_seconds() / 60)
                if duration >= min_duration_min:
                    free_slots.append({
                        "start":            cursor,
                        "end":              busy_start,
                        "duration_minutes": duration,
                    })
            cursor = max(cursor, busy_end)

        # Gap after last event
        if cursor < day_end:
            duration = int((day_end - cursor).total_seconds() / 60)
            if duration >= min_duration_min:
                free_slots.append({
                    "start":            cursor,
                    "end":              day_end,
                    "duration_minutes": duration,
                })

    return free_slots[:20]  # cap to avoid huge prompts


def format_slots_for_display(slots: List[Dict], limit: int = 5) -> str:
    """Format free slots as a numbered list for Telegram."""
    lines = []
    for i, s in enumerate(slots[:limit]):
        start = s["start"]
        end   = s["end"]
        dur   = s["duration_minutes"]
        day   = start.strftime("%A %b %d")
        t_s   = start.strftime("%I:%M %p").lstrip("0")
        t_e   = end.strftime("%I:%M %p").lstrip("0")
        lines.append(f"  {i+1}. {day}, {t_s} – {t_e} ({dur} min free)")
    return "\n".join(lines)


# ── LLM slot picker ───────────────────────────────────────────────────────────

def suggest_slot(
    title:            str,
    duration_minutes: int,
    free_slots:       List[Dict],
) -> Optional[Dict]:
    """
    Ask Gemini to pick the best slot for a task given its title and free slots.
    Returns the chosen slot dict, or None to fall back to asking the user.
    """
    if not OPENROUTER_API_KEY or not free_slots:
        return None

    slots_text = "\n".join(
        f"{i+1}. {s['start'].strftime('%A %b %d %H:%M')} "
        f"({s['duration_minutes']} min free)"
        for i, s in enumerate(free_slots[:8])
    )

    prompt = (
        f"Task: {title}\n"
        f"Duration needed: {duration_minutes} min\n"
        f"Free slots:\n{slots_text}\n\n"
        f"Pick the single best slot number for this task. "
        f"Consider: morning for focused work, evening for gym/exercise, "
        f"prefer slots with enough buffer. "
        f"Reply with ONLY the slot number, nothing else."
    )

    try:
        resp = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       "google/gemini-2.5-flash-lite",
                "max_tokens":  5,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=6,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        n   = int(re.search(r"\d+", raw).group())
        if 1 <= n <= len(free_slots):
            return free_slots[n - 1]
    except Exception as exc:
        logger.warning(f"suggest_slot error: {exc}")

    return None


# ── Build suggestion message ──────────────────────────────────────────────────

def build_suggestion_message(
    title:            str,
    duration_minutes: int,
    free_slots:       List[Dict],
) -> Tuple[str, Optional[Dict]]:
    """
    Returns (message_text, suggested_slot_or_None).
    If a slot is strongly suggested, message asks for confirmation.
    Otherwise lists options for the user to pick.
    """
    if not free_slots:
        return (
            f"📅 Scheduling: *{title}*\n\n"
            "Your calendar looks packed! When would you like to schedule this?\n"
            "(e.g. `Thursday 2pm for 1 hour`)",
            None,
        )

    best = suggest_slot(title, duration_minutes, free_slots)

    if best:
        start    = best["start"]
        end      = start + timedelta(minutes=duration_minutes)
        day_str  = start.strftime("%A %b %d")
        t_s      = start.strftime("%I:%M %p").lstrip("0")
        t_e      = end.strftime("%I:%M %p").lstrip("0")
        alts     = format_slots_for_display(
            [s for s in free_slots if s != best], limit=3
        )
        msg = (
            f"📅 *{title}* — I found a good slot:\n\n"
            f"✨ *{day_str}, {t_s} – {t_e}*\n\n"
            f"Reply `yes` to confirm, `no` to see other options, "
            f"or type a different time."
        )
        if alts:
            msg += f"\n\n_Other free slots:_\n{alts}"
        return msg, best

    # No strong suggestion — show list
    slots_display = format_slots_for_display(free_slots, limit=5)
    msg = (
        f"📅 *{title}* — here are your free slots:\n\n"
        f"{slots_display}\n\n"
        f"Reply with a number, or type a specific time."
    )
    return msg, None
