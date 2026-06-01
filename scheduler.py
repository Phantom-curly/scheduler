"""
Scheduler — all timed jobs.

Jobs:
  1. morning_briefing        — daily at MORNING_TIME
  2. urgency_check           — daily at 12:00 (midday alert)
  3. evening_planning_prompt — daily at 21:00
  4. sunday_weekly_prompt    — every Sunday at 21:00
  5. calendar_reminders      — every minute
"""

import logging, os
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron      import CronTrigger
import pytz

import db
import calendar_client
import smart_schedule as ss

logger       = logging.getLogger(__name__)
TIMEZONE     = os.getenv("TIMEZONE",     "Asia/Seoul")
MORNING_TIME = os.getenv("MORNING_TIME", "08:00")


def _fmt_deadline(d):
    try:
        return datetime.fromisoformat(d).strftime("%a %b %d")
    except Exception:
        return d


def _priority_icon(p):
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(p or "medium", "🟡")


# ── 1. Morning briefing ───────────────────────────────────────────────────────

async def morning_briefing(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    tz    = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).date()
    lines = [f"☀️ *Good morning! Here's your {datetime.now(tz).strftime('%A')}:*\n"]

    # Tasks due today
    tasks_today = db.get_tasks_by_period(today, today)
    if tasks_today:
        lines.append("📋 *Due today:*")
        for t in tasks_today:
            cal  = " 📅" if t["calendar_event_id"] else ""
            icon = _priority_icon(t["priority"])
            lines.append(f"  {icon} {t['title']}{cal}")
        lines.append("")

    # Calendar events today
    try:
        events = calendar_client.get_todays_events()
        if events:
            lines.append("🗓 *Scheduled today:*")
            for e in events:
                lines.append(f"  • {e.get('summary','Event')} — {calendar_client.fmt_event_time(e)}")
            lines.append("")
    except Exception as exc:
        logger.warning(f"morning calendar fetch: {exc}")
        events = []

    # Daily habits
    daily_habits = db.get_habits(frequency="daily")
    if daily_habits:
        lines.append("📌 *Daily habits:*")
        for h in daily_habits:
            note = f" ({h['notes']})" if h["notes"] else ""
            lines.append(f"  • {h['title']}{note}")
        lines.append("")

    # Unscheduled tasks (not just today)
    unscheduled = db.get_unscheduled_tasks(status_filter="pending")
    if unscheduled:
        lines.append("⚠️ *Unscheduled tasks:*")
        for t in unscheduled[:4]:
            due  = f" — due {_fmt_deadline(t['deadline'])}" if t["deadline"] else ""
            icon = _priority_icon(t["priority"])
            lines.append(f"  {icon} {t['title']}{due}")
        if len(unscheduled) > 4:
            lines.append(f"  _...and {len(unscheduled)-4} more_")
        lines.append("")

    # AI recommendations for today's free slots
    try:
        free_slots = ss.get_free_slots(days_ahead=1, min_duration_min=30)
        if free_slots and (unscheduled or daily_habits):
            rec = ss.generate_daily_recommendations(
                todays_events = events,
                todays_tasks  = list(tasks_today),
                unscheduled   = [dict(t) for t in unscheduled[:5]],
                habits        = [dict(h) for h in daily_habits],
                free_slots    = free_slots,
            )
            if rec:
                lines.append("💡 *Suggested plan for today:*")
                lines.append(rec)
                lines.append("")
    except Exception as exc:
        logger.warning(f"morning AI rec: {exc}")

    if len(lines) == 1:
        lines.append("Nothing scheduled today — enjoy the free time! 🎉")

    lines.append("_/week · /tasks · /habits_")

    await app.bot.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown"
    )


# ── 2. Midday urgency check ───────────────────────────────────────────────────

async def urgency_check(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    urgent = db.get_urgent_tasks(days_ahead=1)
    if not urgent:
        return

    lines = ["⚠️ *Heads up!*\n"]
    lines.append(f"You have *{len(urgent)} unscheduled task(s)* due very soon:\n")
    for t in urgent:
        due  = _fmt_deadline(t["deadline"])
        icon = _priority_icon(t["priority"])
        lines.append(f"  {icon} {t['title']} — due {due}")

    lines.append("\nReply `/tasks` then `schedule [numbers]` to block time.")

    await app.bot.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown"
    )


# ── 3. Evening planning prompt ────────────────────────────────────────────────

async def evening_planning_prompt(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    tz       = pytz.timezone(TIMEZONE)
    tomorrow = datetime.now(tz).date() + timedelta(days=1)
    tasks    = db.get_tasks_by_period(tomorrow, tomorrow)
    lines    = ["🌙 *Time to plan tomorrow!*\n"]

    if tasks:
        lines.append("📋 *Tasks due tomorrow:*")
        for t in tasks:
            cal  = " 📅" if t["calendar_event_id"] else " _(not scheduled)_"
            icon = _priority_icon(t["priority"])
            lines.append(f"  {icon} {t['title']}{cal}")
        lines.append("")
        unscheduled = [t for t in tasks if not t["calendar_event_id"]]
        if unscheduled:
            lines.append("💡 Reply `schedule` then numbers to block time for unscheduled tasks.")
    else:
        lines.append("No tasks due tomorrow — good!\n")
        lines.append("💡 Add tasks: `add [task] by [date]`")

    await app.bot.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown"
    )


# ── 4. Sunday weekly planning ─────────────────────────────────────────────────

async def sunday_weekly_prompt(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    tz       = pytz.timezone(TIMEZONE)
    today    = datetime.now(tz).date()
    next_mon = today + timedelta(days=(7 - today.weekday()))
    next_sun = next_mon + timedelta(days=6)
    tasks    = db.get_tasks_by_period(next_mon, next_sun)
    habits   = db.get_habits(frequency="weekly")

    # ── Weekly review first ──────────────────────────────────────────────────
    completed = db.get_completed_this_week()
    planned   = db.get_planned_this_week()
    total     = len(planned)
    done      = len(completed)
    rate      = int((done / total * 100)) if total else 0
    rate_bar  = "▓" * (rate // 10) + "░" * (10 - rate // 10)

    lines = ["📊 *Weekly Review*\n"]
    lines.append(f"Completion: {rate_bar} {done}/{total} tasks ({rate}%)")

    if completed:
        lines.append("\n✅ *Done this week:*")
        for t in completed:
            lines.append(f"  • {t['title']}")

    missed = [t for t in planned if t["status"] != "done"]
    if missed:
        lines.append("\n❌ *Missed/incomplete:*")
        for t in missed:
            icon = _priority_icon(t["priority"])
            lines.append(f"  {icon} {t['title']}")

    # AI reflection
    try:
        review = ss.generate_weekly_review(
            [dict(t) for t in completed],
            [dict(t) for t in planned],
        )
        if review:
            lines.append(f"\n💬 _{review}_")
    except Exception as exc:
        logger.warning(f"weekly review AI: {exc}")

    lines.append("\n" + "─" * 20)
    lines.append("\n📅 *Sunday planning time! Let's set up your week.*\n")

    # Tasks
    if tasks:
        lines.append(f"📋 *Tasks next week ({len(tasks)}):*")
        for t in tasks:
            due  = f" — due {_fmt_deadline(t['deadline'])}" if t["deadline"] else ""
            icon = _priority_icon(t["priority"])
            cal  = " 📅" if t["calendar_event_id"] else ""
            lines.append(f"  {icon} {t['title']}{due}{cal}")
        lines.append("")

    # Weekly habits
    if habits:
        lines.append("🏋️ *Weekly habits to schedule:*")
        for h in habits:
            note  = f" ({h['notes']})" if h["notes"] else ""
            times = f" — {h['count']}x" if h["count"] > 1 else ""
            lines.append(f"  • {h['title']}{note}{times}")
        lines.append("")

    # AI weekly plan
    try:
        free_slots = ss.get_free_slots(days_ahead=7, min_duration_min=30)
        all_tasks  = [dict(t) for t in tasks]
        all_habits = [dict(h) for h in habits]

        if free_slots and (all_tasks or all_habits):
            plan = ss.generate_weekly_plan(all_tasks, all_habits, free_slots)
            if plan:
                lines.append("🤖 *Suggested schedule for the week:*")
                lines.append(plan)
                lines.append("")
    except Exception as exc:
        logger.warning(f"sunday AI plan: {exc}")

    lines.append("_Reply `schedule [task numbers]` or `schedule [event] on [day time]`_")

    await app.bot.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown"
    )


# ── 5. Calendar event reminders ───────────────────────────────────────────────

async def calendar_reminders(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    try:
        events = calendar_client.get_events_starting_soon(window_minutes=35)
    except Exception as exc:
        logger.warning(f"calendar_reminders fetch: {exc}")
        return

    tz        = pytz.timezone(TIMEZONE)
    local_now = datetime.now(tz)

    for event in events:
        start = calendar_client.parse_event_start(event)
        if not start:
            continue
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

    scheduler.add_job(morning_briefing,        CronTrigger(hour=morning_h, minute=morning_m, timezone=TIMEZONE), args=[app], id="morning")
    scheduler.add_job(urgency_check,            CronTrigger(hour=12, minute=0, timezone=TIMEZONE),                args=[app], id="urgency")
    scheduler.add_job(evening_planning_prompt,  CronTrigger(hour=21, minute=0, timezone=TIMEZONE),                args=[app], id="evening")
    scheduler.add_job(sunday_weekly_prompt,     CronTrigger(day_of_week="sun", hour=21, minute=0, timezone=TIMEZONE), args=[app], id="sunday")
    scheduler.add_job(calendar_reminders,       "interval", minutes=1,                                             args=[app], id="cal_reminders")

    return scheduler