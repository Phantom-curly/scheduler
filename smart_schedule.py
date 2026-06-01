"""
Smart scheduling — free slot finder + AI-powered recommendations.

Logic:
- Sleep window blocked (23:00 - 07:00)
- Meal times soft-blocked (07:30-08:30, 12:00-13:00, 18:30-19:30)
- Task-type awareness: focus work → morning, gym/exercise → morning or evening
- Generates natural language recommendations via Gemini
"""

import os, logging, re, json
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import pytz
import requests

import calendar_client

logger             = logging.getLogger(__name__)
TIMEZONE           = os.getenv("TIMEZONE", "Asia/Seoul")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL              = "google/gemini-2.5-flash-lite"
API_URL            = "https://openrouter.ai/api/v1/chat/completions"

# ── Blocked windows (hour, minute) tuples ────────────────────────────────────

SLEEP_START  = (23, 0)
SLEEP_END    = (7,  0)
SOFT_BLOCKS  = [          # (start_h, start_m, end_h, end_m, label)
    (7, 30,  8, 30,  "breakfast"),
    (12, 0, 13,  0,  "lunch"),
    (18,30, 19, 30,  "dinner"),
]


def _is_sleep(dt: datetime) -> bool:
    h = dt.hour
    return h >= SLEEP_START[0] or h < SLEEP_END[0]


def _is_soft_blocked(dt: datetime) -> bool:
    for sh, sm, eh, em, _ in SOFT_BLOCKS:
        start = dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end   = dt.replace(hour=eh, minute=em, second=0, microsecond=0)
        if start <= dt < end:
            return True
    return False


# ── Free slot finder ──────────────────────────────────────────────────────────

def get_free_slots(
    days_ahead:       int  = 5,
    min_duration_min: int  = 30,
    respect_soft:     bool = True,
) -> List[Dict]:
    """
    Returns free slots respecting sleep and optionally meal times.
    Each slot: {start, end, duration_minutes, label}
    """
    tz       = pytz.timezone(TIMEZONE)
    now      = datetime.now(tz)

    try:
        events = calendar_client.list_upcoming_events(days=days_ahead)
    except Exception as exc:
        logger.warning(f"smart_schedule fetch failed: {exc}")
        return []

    # Build busy blocks from calendar
    busy: List[Tuple[datetime, datetime]] = []
    for e in events:
        start = calendar_client.parse_event_start(e)
        if not start:
            continue
        if start.tzinfo is None:
            start = tz.localize(start)
        end_raw = e.get("end", {})
        end_str = end_raw.get("dateTime") or end_raw.get("date")
        try:
            end = datetime.fromisoformat(end_str)
            if end.tzinfo is None:
                end = tz.localize(end)
            else:
                end = end.astimezone(tz)
        except Exception:
            end = start + timedelta(hours=1)
        busy.append((start, end))

    busy.sort(key=lambda x: x[0])

    free_slots = []
    for day_offset in range(days_ahead):
        day = (now + timedelta(days=day_offset)).date()

        # Build waking window for this day
        wake_start = tz.localize(datetime(day.year, day.month, day.day, SLEEP_END[0],  SLEEP_END[1]))
        wake_end   = tz.localize(datetime(day.year, day.month, day.day, SLEEP_START[0],SLEEP_START[1]))

        # Don't go before now
        cursor = max(wake_start, now + timedelta(minutes=15))
        if cursor >= wake_end:
            continue

        day_busy = [(s, e) for s, e in busy if s.date() == day]

        def add_slot(s, e):
            if respect_soft:
                # Trim soft blocks from edges
                for sh, sm, eh, em, _ in SOFT_BLOCKS:
                    sb = s.replace(hour=sh, minute=sm, second=0, microsecond=0)
                    se = s.replace(hour=eh, minute=em, second=0, microsecond=0)
                    if sb <= s < se:
                        s = se
                    if sb < e <= se:
                        e = sb
                if s >= e:
                    return
            dur = int((e - s).total_seconds() / 60)
            if dur >= min_duration_min:
                free_slots.append({"start": s, "end": e, "duration_minutes": dur})

        for busy_start, busy_end in day_busy:
            if cursor < busy_start:
                add_slot(cursor, busy_start)
            cursor = max(cursor, busy_end)

        add_slot(cursor, wake_end)

    return free_slots[:25]


def format_slots_for_display(slots: List[Dict], limit: int = 5) -> str:
    lines = []
    for i, s in enumerate(slots[:limit]):
        day   = s["start"].strftime("%a %b %d")
        t_s   = s["start"].strftime("%I:%M %p").lstrip("0")
        t_e   = s["end"].strftime("%I:%M %p").lstrip("0")
        dur   = s["duration_minutes"]
        lines.append(f"  {i+1}. {day}, {t_s} – {t_e} ({dur} min free)")
    return "\n".join(lines)


# ── AI slot picker ────────────────────────────────────────────────────────────

def suggest_slot(title: str, duration_minutes: int, free_slots: List[Dict]) -> Optional[Dict]:
    """Ask Gemini to pick the best slot. Returns slot dict or None."""
    if not OPENROUTER_API_KEY or not free_slots:
        return None

    slots_text = "\n".join(
        f"{i+1}. {s['start'].strftime('%A %b %d %H:%M')} ({s['duration_minutes']} min free)"
        for i, s in enumerate(free_slots[:8])
    )

    prompt = (
        f"Task: {title} (needs {duration_minutes} min)\n"
        f"Free slots:\n{slots_text}\n\n"
        "Pick best slot number. Rules:\n"
        "- Study/focus/reading/work → morning or early afternoon\n"
        "- Gym/run/exercise/sport → morning (7-10am) or evening (6-9pm)\n"
        "- Meetings/calls → business hours\n"
        "- Creative/writing → morning\n"
        "- Slot must be long enough\n"
        "Reply ONLY with the number."
    )

    try:
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "max_tokens": 5, "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=6,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        n   = int(re.search(r"\d+", raw).group())
        if 1 <= n <= len(free_slots):
            return free_slots[n - 1]
    except Exception as exc:
        logger.warning(f"suggest_slot: {exc}")
    return None


def build_suggestion_message(
    title: str,
    duration_minutes: int,
    free_slots: List[Dict],
) -> Tuple[str, Optional[Dict]]:
    if not free_slots:
        return (
            f"📅 Scheduling: *{title}*\n\nYour calendar looks packed this week!\n"
            "When would you like to squeeze it in? (e.g. `Thursday 9pm for 1 hour`)", None
        )

    best = suggest_slot(title, duration_minutes, free_slots)

    if best:
        start   = best["start"]
        end     = start + timedelta(minutes=duration_minutes)
        day_str = start.strftime("%A %b %d")
        t_s     = start.strftime("%I:%M %p").lstrip("0")
        t_e     = end.strftime("%I:%M %p").lstrip("0")
        alts    = format_slots_for_display([s for s in free_slots if s != best], limit=3)
        msg = (
            f"📅 *{title}*\n\n"
            f"✨ Best slot: *{day_str}, {t_s} – {t_e}*\n\n"
            f"Reply `yes` to confirm, `no` for other options, or type a different time."
        )
        if alts:
            msg += f"\n\n_Other free slots:_\n{alts}"
        return msg, best

    slots_display = format_slots_for_display(free_slots, limit=5)
    return (
        f"📅 *{title}* — your free slots:\n\n{slots_display}\n\n"
        "Reply with a number or type a specific time.", None
    )


# ── Weekly/daily AI recommendation engine ────────────────────────────────────

def generate_weekly_plan(tasks: list, habits: list, free_slots: List[Dict]) -> str:
    """
    Generate a natural, intelligent weekly plan recommendation.
    Called in Sunday planning message.
    """
    if not OPENROUTER_API_KEY:
        return ""

    today     = datetime.now().strftime("%Y-%m-%d %A")
    slots_txt = "\n".join(
        f"- {s['start'].strftime('%A %b %d %H:%M')} ({s['duration_minutes']} min)"
        for s in free_slots[:15]
    ) or "No free slots found."

    tasks_txt = "\n".join(
        f"- {t['title']} due {t['deadline'] or '?'} [{t.get('priority','medium')}]"
        for t in tasks
    ) or "No tasks."

    habits_txt = "\n".join(
        f"- {h['title']} {h['count']}x/week" if h['frequency'] == 'weekly'
        else f"- {h['title']} daily"
        for h in habits
    ) or "No habits."

    prompt = (
        f"Today: {today}\n"
        f"Tasks:\n{tasks_txt}\n\n"
        f"Weekly habits:\n{habits_txt}\n\n"
        f"Free slots this week:\n{slots_txt}\n\n"
        "Write a SHORT weekly plan recommendation (max 8 lines). Be specific:\n"
        "- Assign each task/habit to a specific slot\n"
        "- Keep sleep (11pm-7am) and meals free\n"
        "- Group similar tasks (study sessions together)\n"
        "- Respect deadlines — urgent tasks get early slots\n"
        "- Use bullet points, be conversational\n"
        "Format: emoji + day + time + task"
    )

    try:
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "max_tokens": 300, "temperature": 0.4,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=12,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning(f"generate_weekly_plan: {exc}")
        return ""


def generate_daily_recommendations(
    todays_events: list,
    todays_tasks:  list,
    unscheduled:   list,
    habits:        list,
    free_slots:    List[Dict],
) -> str:
    """
    Generate morning briefing recommendations — what to do with free time today.
    """
    if not OPENROUTER_API_KEY or (not unscheduled and not habits):
        return ""

    today     = datetime.now().strftime("%Y-%m-%d %A")
    slots_txt = "\n".join(
        f"- {s['start'].strftime('%H:%M')}–{s['end'].strftime('%H:%M')} ({s['duration_minutes']} min)"
        for s in free_slots[:8]
    ) or "No gaps found."

    events_txt = "\n".join(
        f"- {e.get('summary','?')} at {e.get('start',{}).get('dateTime','?')}"
        for e in todays_events[:8]
    ) or "Empty calendar."

    unsch_txt = "\n".join(
        f"- {t['title']} [{t.get('priority','medium')} priority]"
        for t in unscheduled[:6]
    ) or "None."

    habits_txt = "\n".join(f"- {h['title']}" for h in habits[:5]) or "None."

    prompt = (
        f"Today: {today}\n"
        f"Scheduled today:\n{events_txt}\n\n"
        f"Free gaps today:\n{slots_txt}\n\n"
        f"Unscheduled tasks:\n{unsch_txt}\n\n"
        f"Daily habits to fit in:\n{habits_txt}\n\n"
        "Give a SHORT recommendation (max 5 lines) for fitting unscheduled items into today's gaps.\n"
        "Be specific with times. Prioritize urgent tasks. Keep it actionable.\n"
        "Format: emoji + time + task"
    )

    try:
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "max_tokens": 180, "temperature": 0.3,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning(f"generate_daily_recommendations: {exc}")
        return ""


# ── Weekly review ─────────────────────────────────────────────────────────────

def generate_weekly_review(completed: list, planned: list) -> str:
    """
    Generate a short weekly review using Gemini.
    completed: tasks marked done this week
    planned: all tasks that had deadlines this week
    """
    if not OPENROUTER_API_KEY:
        return ""

    total     = len(planned)
    done      = len(completed)
    missed    = [t for t in planned if t["status"] != "done"]
    rate      = int((done / total * 100)) if total else 0

    completed_txt = "\n".join(f"- ✅ {t['title']}" for t in completed) or "None."
    missed_txt    = "\n".join(f"- ❌ {t['title']} (due {t['deadline'] or '?'})" for t in missed) or "None."

    today = datetime.now().strftime("%Y-%m-%d %A")

    prompt = (
        f"Weekly review for week ending {today}.\n"
        f"Completion rate: {done}/{total} tasks ({rate}%)\n\n"
        f"Completed:\n{completed_txt}\n\n"
        f"Missed/incomplete:\n{missed_txt}\n\n"
        "Write a SHORT encouraging weekly review (3-4 lines max):\n"
        "- Acknowledge what was done\n"
        "- Note what was missed without judgment\n"
        "- One actionable suggestion for next week\n"
        "Keep it warm, motivating, and brief."
    )

    try:
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "max_tokens": 150, "temperature": 0.5,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning(f"generate_weekly_review: {exc}")
        return ""