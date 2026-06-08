"""
Scheduler — all timed jobs.

Jobs:
  1. morning_briefing        — daily at MORNING_TIME
  2. urgency_check           — daily at 12:00 (midday alert)
  3. evening_planning_prompt — daily at 21:00
  4. sunday_weekly_review    — every Sunday at 20:00
  5. sunday_weekly_planning  — every Sunday at 21:00
  6. calendar_reminders      — every minute
  7. app_reminders           — every minute
"""

import logging, os
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron      import CronTrigger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import pytz

import db

logger       = logging.getLogger(__name__)
TIMEZONE     = os.getenv("TIMEZONE",     "Asia/Seoul")
MORNING_TIME = os.getenv("MORNING_TIME", "08:00")


def _fmt_deadline(d):
    try:
        return datetime.fromisoformat(d).strftime("%a %b %d")
    except Exception:
        return d


def _fmt_dt(dt):
    return dt.strftime("%a %b %d, %I:%M %p").lstrip("0")


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
        import calendar_client
        events = calendar_client.get_todays_events()
        if events:
            lines.append("*Scheduled today:*")
            for e in events:
                title = e.get('summary', 'Event')
                lines.append(f"  • *{title}* — {calendar_client.fmt_event_time_range(e)}")
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
        import smart_schedule as ss
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
    now      = datetime.now(tz)
    tomorrow = now.date() + timedelta(days=1)
    lines    = [f"🌙 *Evening Planning — {tomorrow.strftime('%A')}*\n"]

    # Calendar events tomorrow (from Google Calendar — both user-added and bot-added)
    try:
        import calendar_client as cc
        events = cc.list_events_by_day(tomorrow)
        if events:
            lines.append("*Calendar events tomorrow:*")
            for e in events:
                title = e.get('summary', 'Event')
                lines.append(f"  • *{title}* — {cc.fmt_event_time_range(e)}")
            lines.append("")
        else:
            lines.append("No calendar events tomorrow.\n")
    except Exception as exc:
        logger.warning(f"evening calendar fetch: {exc}")
        lines.append("🗓 (Couldn't fetch calendar)\n")

    # Tasks due tomorrow
    tasks = db.get_tasks_by_period(tomorrow, tomorrow)
    if tasks:
        lines.append("📋 *Tasks due tomorrow:*")
        for t in tasks:
            cal  = " 📅" if t["calendar_event_id"] else " ⏳"
            icon = _priority_icon(t["priority"])
            status_tag = " ✅" if t["status"] == "done" else ""
            lines.append(f"  {icon} {t['title']}{cal}{status_tag}")
        lines.append("")

    # Today's completed & remaining tasks
    today_tasks = db.get_tasks_by_period(now.date(), now.date())
    done_today = [t for t in today_tasks if t["status"] == "done"]
    remaining_today = [t for t in today_tasks if t["status"] != "done"]
    if remaining_today:
        lines.append("⏳ *Today's unfinished:*")
        for t in remaining_today:
            cal  = " 📅" if t["calendar_event_id"] else " ⏳"
            icon = _priority_icon(t["priority"])
            lines.append(f"  {icon} {t['title']}{cal}")
        lines.append("")
    if done_today:
        lines.append("✅ *Completed today:*")
        for t in done_today:
            lines.append(f"  • {t['title']}")
        lines.append("")

    # Daily habits for tomorrow
    daily_habits = db.get_habits(frequency="daily")
    if daily_habits:
        lines.append("📌 *Daily habits for tomorrow:*")
        for h in daily_habits:
            note = f" ({h['notes']})" if h["notes"] else ""
            lines.append(f"  • {h['title']}{note}")
        lines.append("")

    # Unscheduled tasks (upcoming) — sorted by closest deadline
    unscheduled = db.get_tasks_sorted_by_deadline()
    if unscheduled:
        lines.append(f"📝 *Unscheduled tasks ({len(unscheduled)}):*")
        shown = 0
        for t in unscheduled:
            if t["calendar_event_id"] or t["status"] == "done":
                continue
            if shown >= 5:
                lines.append(f"  _+{len([u for u in unscheduled if not u['calendar_event_id'] and u['status'] != 'done']) - 5} more_")
                break
            due = f" — due {_fmt_deadline(t['deadline'])}" if t["deadline"] else " — no deadline"
            icon = _priority_icon(t["priority"])
            lines.append(f"  {icon} {t['title']}{due}")
            shown += 1
        lines.append("")

    if len(lines) == 1:
        lines.append("Nothing due tomorrow — enjoy your evening! 🎉\n")

    lines.append("💡 `/tasks` — view unscheduled tasks")
    lines.append("💡 `/week` — see full week")
    lines.append("💡 `/plan` — auto-schedule suggestions")
    await app.bot.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown"
    )


# ── 4. Sunday weekly review (8 PM) ───────────────────────────────────────────

async def sunday_weekly_review(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    tz       = pytz.timezone(TIMEZONE)
    today    = datetime.now(tz).date()
    start    = today - timedelta(days=today.weekday())
    end      = start + timedelta(days=6)

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
            lines.append(f"  {icon} {t['title']} — due {_fmt_deadline(t['deadline'])}" if t["deadline"] else f"  {icon} {t['title']}")

    # AI reflection
    try:
        import smart_schedule as ss
        review = ss.generate_weekly_review(
            [dict(t) for t in completed],
            [dict(t) for t in planned],
        )
        if review:
            lines.append(f"\n💬 _{review}_")
    except Exception as exc:
        logger.warning(f"weekly review AI: {exc}")

    if not planned:
        lines.append("\nNo tasks were planned this week.")

    lines.append("\n_See you at 9 PM for weekly planning!_")

    await app.bot.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown"
    )


# ── 5. Sunday weekly planning (9 PM) ─────────────────────────────────────────

async def sunday_weekly_planning(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    tz       = pytz.timezone(TIMEZONE)
    today    = datetime.now(tz).date()
    next_mon = today + timedelta(days=(7 - today.weekday()))
    next_sun = next_mon + timedelta(days=6)
    habits   = db.get_habits(frequency="weekly")

    lines = ["📅 *Plan your week!*\n"]

    # Unscheduled tasks sorted by deadline
    unscheduled = db.get_unscheduled_tasks_sorted()
    if unscheduled:
        lines.append(f"📝 *Unscheduled tasks ({len(unscheduled)}):*")
        for i, t in enumerate(unscheduled):
            due = f" — due {_fmt_deadline(t['deadline'])}" if t["deadline"] else ""
            icon = _priority_icon(t["priority"])
            urgent = " ⏰" if t["deadline"] and datetime.fromisoformat(t["deadline"]).date() <= next_mon + timedelta(days=2) else ""
            lines.append(f"  {i+1}. {icon} {t['title']}{due}{urgent}")
        lines.append("")

    # Tasks due next week
    tasks_next = db.get_tasks_by_period(next_mon, next_sun)
    if tasks_next:
        lines.append(f"📆 *Due next week ({len(tasks_next)}):*")
        for t in tasks_next:
            cal = " 📅" if t["calendar_event_id"] else " ⏳"
            icon = _priority_icon(t["priority"])
            lines.append(f"  {icon} {t['title']}{cal}")
        lines.append("")

    # Weekly habits
    if habits:
        lines.append("🏋️ *Habits to maintain:*")
        for h in habits:
            note = f" ({h['notes']})" if h["notes"] else ""
            times = f" — {h['count']}x" if h["count"] > 1 else ""
            lines.append(f"  • {h['title']}{note}{times}")
        lines.append("")

    lines.append("💡 `/tasks` — view unscheduled tasks")
    lines.append("💡 `/week` — view full calendar")
    lines.append("💡 Reply `schedule [numbers]` to block time")
    lines.append("💡 Reply `plan my tasks` for auto-schedule suggestions")

    await app.bot.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown"
    )


# ── 6. Calendar event reminders ───────────────────────────────────────────────

async def calendar_reminders(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    try:
        import calendar_client
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

        # Find associated task
        task_ref = ""
        task_id = None
        try:
            tasks = db.get_all_tasks(status="in_progress")
            for t in tasks:
                if t["calendar_event_id"] == event_id:
                    task_id = t["id"]
                    task_ref = f"\n📎 t#{task_id}"
                    break
        except Exception:
            pass

        ref_line = f"\n📎 e#{event_id}{task_ref}"

        # Build inline keyboard with "Done" button if it's a task
        reply_markup = None
        if task_id:
            # Show task title on button
            btn_text = f"✅ Done: {title[:22]}"
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(btn_text, callback_data=f"done_task_{task_id}")]
            ])

        kwargs = {
            "chat_id": chat_id,
            "text": f"⏰ *Reminder:* _{title}_ starts at *{time_str}* (in ~{int(minutes_until)} min){ref_line}",
            "parse_mode": "Markdown",
        }
        if reply_markup:
            kwargs["reply_markup"] = reply_markup
        await app.bot.send_message(**kwargs)


# ── 7. App reminders (not calendar blocks) ───────────────────────────────────

def _parse_local_dt(value):
    tz = pytz.timezone(TIMEZONE)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return tz.localize(dt)
    return dt.astimezone(tz)


def _next_recurrence(rrule_text, start, now):
    if not rrule_text:
        return None
    try:
        from dateutil.rrule import rrulestr
        rule = rrulestr(rrule_text, dtstart=start)
        nxt = rule.after(now, inc=False)
        return nxt.isoformat() if nxt else None
    except Exception as exc:
        logger.warning(f"reminder recurrence parse: {exc}")
        return None


async def app_reminders(app):
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    tz  = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    try:
        due = db.get_due_app_reminders(now.isoformat())
    except Exception as exc:
        logger.warning(f"app_reminders fetch: {exc}")
        return

    for reminder in due:
        try:
            remind_at = _parse_local_dt(reminder["remind_at"])
            if remind_at > now:
                continue

            ref_line = f"\n📎 r#{reminder['id']}" if reminder.get('id') else ""
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"🔔 *Reminder:* {reminder['title']}{ref_line}",
                parse_mode="Markdown",
            )

            next_at = _next_recurrence(reminder["recurrence_rrule"], remind_at, now)
            db.complete_app_reminder(reminder["id"], now.isoformat(), next_at)
        except Exception as exc:
            logger.warning(f"app_reminder send failed: {exc}")


# ── 8. Collision detection — stale event cleanup ─────────────────────────────

async def cleanup_stale_events(app):
    """Check all tasks with calendar_event_id and verify events still exist in GCal.
    Runs every hour. Clears stale references from DB."""
    try:
        tasks_with_events = db.get_conn().execute(
            "SELECT * FROM tasks WHERE calendar_event_id IS NOT NULL AND status != 'done'"
        ).fetchall()
    except Exception as exc:
        logger.warning(f"cleanup_stale_events db: {exc}")
        return

    cleaned = 0
    for task in tasks_with_events:
        event_id = task["calendar_event_id"]
        try:
            import calendar_client
            exists = calendar_client.get_event(event_id)
            if not exists:
                raise Exception("event not found")
        except Exception:
            # Event no longer exists in Google Calendar — clear the reference
            try:
                db.update_task(
                    task["id"],
                    calendar_event_id=None,
                    scheduled_start=None,
                    scheduled_end=None,
                    status="pending",
                )
                cleaned += 1
                logger.info(f"cleanup: task #{task['id']} ('{task['title']}') — stale event {event_id} cleared")
            except Exception as exc2:
                logger.warning(f"cleanup_stale_events update #{task['id']}: {exc2}")

    if cleaned:
        logger.info(f"cleanup_stale_events: cleared {cleaned} stale event reference(s)")


# ── 9. Overdue task lifecycle ──────────────────────────────────────────────────

async def overdue_task_check(app):
    """Check for overdue unscheduled tasks and handle their lifecycle.
    - Day 1 overdue: send reminder
    - Day 7 overdue: send auto-delete warning
    - Day 8+ overdue: auto-delete with notification
    """
    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    tz       = pytz.timezone(TIMEZONE)
    today    = datetime.now(tz).date()

    try:
        overdue = db.get_overdue_unscheduled_tasks()
    except Exception as exc:
        logger.warning(f"overdue_task_check db: {exc}")
        return

    for task in overdue:
        try:
            deadline = datetime.fromisoformat(task["deadline"]).date()
        except Exception:
            continue

        days_overdue = (today - deadline).days
        if days_overdue < 1:
            continue

        task_id = task["id"]

        if days_overdue == 1:
            # Day 1: send reminder
            key = f"overdue_remind_{task_id}"
            if db.reminder_already_sent(key):
                continue
            db.mark_reminder_sent(key)
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ *Overdue:* _{task['title']}_ was due yesterday and still unscheduled.\n"
                     "Schedule it, mark done, or delete it.",
                parse_mode="Markdown",
            )

        elif days_overdue == 7:
            # Day 7: send auto-delete warning
            key = f"overdue_warn_{task_id}"
            if db.reminder_already_sent(key):
                continue
            db.mark_reminder_sent(key)
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"🗑 *Auto-delete warning:* _{task['title']}_ was due {days_overdue} days ago.\n"
                     "It will be deleted in 24h unless you take action.",
                parse_mode="Markdown",
            )

        elif days_overdue >= 8:
            # Day 8+: auto-delete
            key = f"overdue_delete_{task_id}"
            if db.reminder_already_sent(key):
                continue
            db.mark_reminder_sent(key)
            db.delete_task(task_id)
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"🗑 *Deleted:* _{task['title']}_ was overdue for over a week and has been removed.",
                parse_mode="Markdown",
            )


# ── 10. Sunday planning with inline buttons ────────────────────────────────────

async def sunday_weekly_planning_with_buttons(app):
    """
    Wrapper around sunday_weekly_planning that sends inline buttons per task.
    Calls the existing sunday_weekly_planning first, then sends a follow-up
    with clickable buttons for the first 5 unscheduled tasks.
    """
    await sunday_weekly_planning(app)

    chat_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    if not chat_id:
        return

    try:
        unscheduled = db.get_unscheduled_tasks_sorted()
    except Exception:
        return

    if not unscheduled:
        return

    keyboard = []
    for t in unscheduled[:5]:
        title = t["title"][:30] + "..." if len(t["title"]) > 30 else t["title"]
        keyboard.append([
            InlineKeyboardButton(f"📅 {title}", callback_data=f"plan_task_{t['id']}")
        ])

    if keyboard:
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            from telegram import Update
            await app.bot.send_message(
                chat_id=chat_id,
                text="👇 Click a task to schedule it:",
                reply_markup=reply_markup,
            )
        except Exception as exc:
            logger.warning(f"sunday_planning_buttons: {exc}")


# ── Setup ──────────────────────────────────────────────────────────────────────

def setup_scheduler(app) -> AsyncIOScheduler:
    morning_h, morning_m = map(int, MORNING_TIME.split(":"))
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    scheduler.add_job(morning_briefing,        CronTrigger(hour=morning_h, minute=morning_m, timezone=TIMEZONE), args=[app], id="morning")
    scheduler.add_job(urgency_check,            CronTrigger(hour=12, minute=0, timezone=TIMEZONE),                args=[app], id="urgency")
    scheduler.add_job(evening_planning_prompt,  CronTrigger(hour=21, minute=0, timezone=TIMEZONE),                args=[app], id="evening")
    scheduler.add_job(sunday_weekly_review,     CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=TIMEZONE), args=[app], id="sunday_review")
    scheduler.add_job(sunday_weekly_planning_with_buttons, CronTrigger(day_of_week="sun", hour=21, minute=0, timezone=TIMEZONE), args=[app], id="sunday_planning")
    scheduler.add_job(calendar_reminders,       "interval", minutes=1,                                             args=[app], id="cal_reminders")
    scheduler.add_job(app_reminders,            "interval", minutes=1,                                             args=[app], id="app_reminders")
    scheduler.add_job(overdue_task_check,       CronTrigger(hour=9, minute=0, timezone=TIMEZONE),                 args=[app], id="overdue_check")
    scheduler.add_job(cleanup_stale_events,     "interval", hours=1,                                               args=[app], id="stale_cleanup")

    return scheduler
