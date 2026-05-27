"""
Scheduler — all timed jobs.

Jobs:
  1. morning_briefing        — daily at MORNING_TIME (default 08:00)
  2. evening_planning_prompt — daily at 21:00
  3. sunday_weekly_prompt    — every Sunday at 21:00
  4. calendar_reminders      — every minute, checks upcoming events
"""

import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron      import CronTrigger

import db
import calendar_client

logger      = logging.getLogger(__name__)
TIMEZONE     = os.getenv("TIMEZONE",     "Asia/Seoul")
MORNING_TIME = os.getenv("MORNING_TIME", "08:00")


def _fmt_deadline(d):
    try:
        return datetime.fromisoformat(d).strftime("%a %b %d")
    except Exception:
        return d


# ── 1. Morning briefing ───────────────────────────────────────────────────────

async def morning_briefing(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    lines = ["☀️ *Good morning! Here's your day:*\n"]

    # Tasks due today
    today = datetime.now().date()
    tasks = db.get_tasks_by_period(today, today)
    if tasks:
        lines.append("📋 *Tasks due today:*")
        for t in tasks:
            cal = " 📅" if t["calendar_event_id"] else ""
            lines.append(f"  • {t['title']}{cal}")
        lines.append("")

    # Calendar events today
    try:
        events = calendar_client.get_todays_events()
        if events:
            lines.append("🗓 *Calendar today:*")
            for e in events:
                lines.append(f"  • {e.get('summary', 'Event')} — {calendar_client.fmt_event_time(e)}")
            lines.append("")
    except Exception as exc:
        logger.warning(f"Calendar fetch failed in morning briefing: {exc}")

    # Daily habits
    daily_habits = db.get_habits(frequency="daily")
    if daily_habits:
        lines.append("📌 *Daily habits to schedule:*")
        for h in daily_habits:
            note = f" ({h['notes']})" if h["notes"] else ""
            lines.append(f"  • {h['title']}{note}")
        lines.append("")
        lines.append("_Reply `schedule [habit] today [time]` to block time_")

    if len(lines) == 1:
        lines.append("Nothing on the plate today — enjoy! 🎉")

    lines.append("\n_/week to see this week's full plan_")

    await app.bot.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown"
    )


# ── 2. Evening planning prompt ────────────────────────────────────────────────

async def evening_planning_prompt(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    tomorrow = datetime.now().date() + timedelta(days=1)
    tasks    = db.get_tasks_by_period(tomorrow, tomorrow)

    lines = ["🌙 *Time to plan tomorrow!*\n"]

    if tasks:
        lines.append("📋 *Tasks due tomorrow:*")
        for t in tasks:
            cal = " 📅" if t["calendar_event_id"] else " _(not scheduled yet)_"
            lines.append(f"  • {t['title']}{cal}")
        lines.append("")
        if any(not t["calendar_event_id"] for t in tasks):
            lines.append("💡 Reply `schedule [number]` to block time for unscheduled tasks.")
    else:
        lines.append("No tasks due tomorrow yet.")
        lines.append("💡 Add some: `add [task] by tomorrow`")

    await app.bot.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown"
    )


# ── 3. Sunday weekly planning ─────────────────────────────────────────────────

async def sunday_weekly_prompt(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    today    = datetime.now().date()
    next_mon = today + timedelta(days=(7 - today.weekday()))
    next_sun = next_mon + timedelta(days=6)
    tasks    = db.get_tasks_by_period(next_mon, next_sun)

    lines = ["📅 *Sunday planning time!*\n"]
    lines.append("Take 10 minutes to plan your week ahead.\n")

    # Next week tasks
    if tasks:
        lines.append(f"📋 *Tasks next week ({len(tasks)}):*")
        for t in tasks:
            due = f" — due {_fmt_deadline(t['deadline'])}" if t["deadline"] else ""
            cal = " 📅" if t["calendar_event_id"] else ""
            lines.append(f"  • {t['title']}{due}{cal}")
        lines.append("")

    # Weekly habits
    weekly_habits = db.get_habits(frequency="weekly")
    if weekly_habits:
        lines.append("🏋️ *Weekly habits to schedule:*")
        for h in weekly_habits:
            note  = f" ({h['notes']})" if h["notes"] else ""
            times = f" — {h['count']}x this week" if h["count"] > 1 else ""
            lines.append(f"  • {h['title']}{note}{times}")
        lines.append("")
        lines.append("_Reply `schedule gym tuesday 10pm and friday 9am` to block them_")

    if len(lines) == 2:
        lines.append("No tasks or habits yet — add some first!")

    lines.append("\n_/week to see this week, /tasks for all tasks_")

    await app.bot.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown"
    )


# ── 4. Calendar event reminders ───────────────────────────────────────────────

async def calendar_reminders(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    try:
        events = calendar_client.get_events_starting_soon(window_minutes=35)
    except Exception as exc:
        logger.warning(f"calendar_reminders fetch error: {exc}")
        return

    import pytz
    tz        = pytz.timezone(TIMEZONE)
    local_now = datetime.now(tz)

    for event in events:
        start = calendar_client.parse_event_start(event)
        if not start:
            continue

        # Ensure start is tz-aware for comparison
        if start.tzinfo is None:
            start = tz.localize(start)

        minutes_until = (start - local_now).total_seconds() / 60
        if minutes_until < 0:
            continue

        reminder_mins = 30
        overrides     = event.get("reminders", {}).get("overrides", [])
        if overrides:
            reminder_mins = overrides[0].get("minutes", 30)

        if abs(minutes_until - reminder_mins) > 1.5:
            continue

        # Dedup key: per event per hour (prevents duplicate fires within same minute window)
        event_id = event["id"]
        key      = f"cal:{event_id}:{start.strftime('%Y-%m-%d-%H')}"
        if db.reminder_already_sent(key):
            continue
        db.mark_reminder_sent(key)

        title    = event.get("summary", "Event")
        time_str = start.strftime("%I:%M %p").lstrip("0")
        await app.bot.send_message(
            chat_id    = chat_id,
            text       = f"⏰ *Reminder:* _{title}_ starts at *{time_str}* (in ~{int(minutes_until)} min)",
            parse_mode = "Markdown",
        )


# ── Setup ──────────────────────────────────────────────────────────────────────

def setup_scheduler(app) -> AsyncIOScheduler:
    morning_h, morning_m = map(int, MORNING_TIME.split(":"))
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    scheduler.add_job(
        morning_briefing, CronTrigger(hour=morning_h, minute=morning_m, timezone=TIMEZONE),
        args=[app], id="morning_briefing",
    )
    scheduler.add_job(
        evening_planning_prompt, CronTrigger(hour=21, minute=0, timezone=TIMEZONE),
        args=[app], id="evening_prompt",
    )
    scheduler.add_job(
        sunday_weekly_prompt, CronTrigger(day_of_week="sun", hour=21, minute=0, timezone=TIMEZONE),
        args=[app], id="sunday_prompt",
    )
    scheduler.add_job(
        calendar_reminders, "interval", minutes=1,
        args=[app], id="calendar_reminders",
    )

    return scheduler