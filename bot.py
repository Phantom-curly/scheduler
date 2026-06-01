"""
Planning Telegram Bot — main entry point.

State machine in context.user_data['state']:
  idle            → normal routing
  scheduling      → collecting time/duration for a batch of tasks
  schedule_direct → collecting time for a direct calendar event
  deleting        → confirmation
  updating        → collecting new value
"""

import asyncio
import base64
import logging
import os
import re
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import db
import nlp
import llm as llm_client
import calendar_client
import smart_schedule
from config    import TELEGRAM_TOKEN, ALLOWED_USER_ID
from scheduler import setup_scheduler

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Auth ───────────────────────────────────────────────────────────────────────

def _authorized(update: Update) -> bool:
    if ALLOWED_USER_ID == 0:
        return True
    return update.effective_user.id == ALLOWED_USER_ID


# ── Formatting ─────────────────────────────────────────────────────────────────

STATUS_ICON = {"pending": "⏳", "in_progress": "🔄", "done": "✅"}


def _fmt_deadline(d):
    try:
        return datetime.fromisoformat(d).strftime("%a %b %d")
    except Exception:
        return d


def _fmt_dt(dt):
    return dt.strftime("%a %b %d, %I:%M %p").lstrip("0")


PRIORITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}

def _fmt_task_row(idx, task):
    icon     = STATUS_ICON.get(task["status"], "⏳")
    p_icon   = PRIORITY_ICON.get(task["priority"] or "medium", "🟡")
    cal_tag  = " 📅" if task["calendar_event_id"] else ""
    due_tag  = f" — due {_fmt_deadline(task['deadline'])}" if task["deadline"] else ""
    return f"{idx}. {icon}{p_icon} {task['title']}{due_tag}{cal_tag}"


def _fmt_task_list(tasks):
    if not tasks:
        return "No tasks found."
    return "\n".join(_fmt_task_row(i + 1, t) for i, t in enumerate(tasks))


# ── Commands ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        "👋 *Planning Bot* — your personal planner\n\n"
        "Just write naturally:\n"
        "› `finish report by next Friday`\n"
        "› `what do I have this week?`\n"
        "› `schedule running session on Wednesday 9pm`\n"
        "› `every Monday at 9am remind me to review goals`\n"
        "› `add daily habit: drink water at 8am`\n\n"
        "Commands: /tasks /today /week /calendar /habits /help",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        "📖 *Commands*\n"
        "/tasks — all pending tasks\n"
        "/today — tasks + calendar today\n"
        "/week — tasks due this week\n"
        "/calendar — upcoming calendar events\n"
        "/habits — your active habits\n"
        "/cancel — cancel current operation\n\n"
        "📝 *Tasks*\n"
        "• `finish report by next Friday`\n"
        "• `what do I have this week?`\n"
        "• `schedule 1 2` (after listing tasks)\n"
        "• `mark task 1 done` / `done 2 3`\n"
        "• `update task 1 deadline to Monday`\n"
        "• `delete task 2`\n\n"
        "📅 *Direct calendar*\n"
        "• `schedule running session on Wednesday 10pm`\n"
        "• `schedule gym on Friday 6am for 1 hour`\n"
        "• `schedule standup every Monday at 9am`\n\n"
        "🔁 *Recurring & habits*\n"
        "• `every Monday at 9am remind me to review goals`\n"
        "• `add daily habit: drink water at 8am`\n"
        "• `every last day of month remind me to log expenses`\n\n"
        "⏰ *Custom reminders*\n"
        "• `schedule gym on Friday 6am remind me 1 hour before`\n"
        "• Default reminder is 30 min before any event",
        parse_mode="Markdown",
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    tasks = db.get_all_tasks(status="pending") + db.get_all_tasks(status="in_progress")
    if not tasks:
        await update.message.reply_text("✨ No pending tasks — you're all clear!")
        return
    context.user_data["last_task_list"] = [dict(t) for t in tasks]
    await update.message.reply_text(
        f"📋 *All Tasks* ({len(tasks)})\n\n{_fmt_task_list(tasks)}\n\n"
        "_Reply `schedule 1 2` to block time in your calendar_",
        parse_mode="Markdown",
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    today  = datetime.now().date()
    tasks  = db.get_tasks_by_period(today, today)
    lines  = [f"📅 *Today — {today.strftime('%A %b %d')}*\n"]

    if tasks:
        context.user_data["last_task_list"] = [dict(t) for t in tasks]
        lines.append("📋 *Tasks:*")
        for i, t in enumerate(tasks):
            lines.append(_fmt_task_row(i + 1, t))
        lines.append("")

    try:
        events = calendar_client.get_todays_events()
        if events:
            lines.append("🗓 *Calendar:*")
            for e in events:
                lines.append(f"  • {e.get('summary', 'Event')} — {calendar_client.fmt_event_time(e)}")
    except Exception:
        pass

    if len(lines) == 1:
        lines.append("Nothing on the plate today! 🎉")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    tasks = db.get_tasks_this_week()
    if not tasks:
        await update.message.reply_text("No tasks this week 🎉")
        return
    context.user_data["last_task_list"] = [dict(t) for t in tasks]
    await update.message.reply_text(
        f"📆 *This Week's Tasks* ({len(tasks)})\n\n{_fmt_task_list(tasks)}\n\n"
        "_Reply `schedule 1 2` to add to calendar_",
        parse_mode="Markdown",
    )


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    try:
        events = calendar_client.list_upcoming_events(days=7)
        if not events:
            await update.message.reply_text("No upcoming calendar events this week.")
            return
        lines = ["🗓 *Upcoming 7 Days*\n"]
        for e in events:
            lines.append(f"• {e.get('summary', 'No title')} — {calendar_client.fmt_event_time(e)}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Calendar error: {exc}")


async def cmd_habits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    habits = db.get_habits(active_only=True)
    if not habits:
        await update.message.reply_text(
            "No active habits yet.\n\n"
            "Add some:\n"
            "• `add weekly habit: gym 2 times`\n"
            "• `add weekly habit: running 4 times`\n"
            "• `add daily habit: read 30 min`\n"
            "• `add daily habit: cold shower`"
        )
        return
    daily  = [h for h in habits if h["frequency"] == "daily"]
    weekly = [h for h in habits if h["frequency"] == "weekly"]
    lines  = [f"🔁 *Active Habits* ({len(habits)})\n"]
    if daily:
        lines.append("📌 *Daily* — shown every morning:")
        for i, h in enumerate(daily):
            note = f" ({h['notes']})" if h["notes"] else ""
            lines.append(f"  {i+1}. {h['title']}{note}")
        lines.append("")
    if weekly:
        lines.append("🏋️ *Weekly* — shown every Sunday:")
        offset = len(daily)
        for i, h in enumerate(weekly):
            note  = f" ({h['notes']})" if h["notes"] else ""
            times = f" — {h['count']}x per week" if h["count"] > 1 else ""
            lines.append(f"  {offset+i+1}. {h['title']}{note}{times}")
        lines.append("")
    lines.append("_Reply `delete habit 1` to remove one_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    import pytz
    tz  = pytz.timezone(os.getenv("TIMEZONE", "Asia/Seoul"))
    now = datetime.now(tz)
    date_str = now.strftime("%A, %B %d %Y")
    time_str = now.strftime("%I:%M %p")
    zone_str = now.strftime("%Z") + " (UTC" + now.strftime("%z") + ")"
    await update.message.reply_text(
        f"\U0001f550 *Bot time*\n\nDate: {date_str}\nTime: {time_str}\nZone: {zone_str}",
        parse_mode="Markdown",
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    from telegram import ReplyKeyboardRemove
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())


# ── Main message router ────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return

    text  = update.message.text.strip()
    state = context.user_data.get("state", "idle")

    if state == "scheduling":
        await _scheduling_time(update, context, text)
        return
    if state == "schedule_direct":
        await _schedule_direct_time(update, context, text)
        return
    if state == "deleting":
        await _delete_confirm(update, context, text)
        return
    if state == "updating":
        await _update_value(update, context, text)
        return
    if state == "reschedule_pick":
        # User picking which event to reschedule from a list
        if text.strip().isdigit():
            matches = context.user_data.get("reschedule_matches", [])
            new_dt  = context.user_data.get("reschedule_new_dt")
            n       = int(text.strip()) - 1
            if 0 <= n < len(matches):
                event = matches[n]
                context.user_data["state"] = "idle"
                if new_dt:
                    await _do_reschedule(update, event, new_dt)
                else:
                    context.user_data["state"]            = "reschedule_time"
                    context.user_data["reschedule_event"] = event
                    await update.message.reply_text(
                        f"New time for *{event.get('summary','Event')}*? "
                        "(e.g. `tomorrow 6pm`)",
                        parse_mode="Markdown",
                    )
            else:
                await update.message.reply_text("Invalid number. Try again.")
        else:
            await update.message.reply_text("Reply with the number of the event.")
        return

    if state == "reschedule_time":
        event  = context.user_data.get("reschedule_event")
        new_dt = nlp.extract_datetime(text)
        if not new_dt:
            # Try with LLM
            p = llm_client.parse(text)
            if p:
                new_dt = llm_client.normalise(p).get("datetime")
        if not new_dt:
            await update.message.reply_text(
                "Couldn't parse that time. Try `tomorrow 6pm` or `Friday 10am`."
            )
            return
        context.user_data["state"] = "idle"
        await _do_reschedule(update, event, new_dt)
        return

    if state == "clarifying":
        # User answered the clarification — re-parse with original + answer combined
        original = context.user_data.pop("clarify_original", "")
        context.user_data["state"] = "idle"
        combined = f"{original} — {text}"
        llm_result = llm_client.parse(combined)
        if llm_result:
            parsed2 = llm_client.normalise(llm_result)
        else:
            parsed2 = nlp.parse_message(combined)
        intent2 = parsed2.get("intent", "unknown")
        dispatch2 = {
            "add": _add_task, "list": _list_tasks, "schedule": _schedule_intent,
            "schedule_direct": _schedule_direct_intent, "complete": _complete_intent,
            "delete": _delete_intent, "update": _update_intent,
            "habit_add": _habit_add, "calendar_query": _calendar_query,
        }
        h2 = dispatch2.get(intent2)
        if h2:
            await h2(update, context, parsed2)
        else:
            await update.message.reply_text("Still not sure — try rephrasing or /help for examples.")
        return

    # Try LLM first, fall back to regex if unavailable
    llm_result = llm_client.parse(text)
    if llm_result:
        parsed = llm_client.normalise(llm_result)
    else:
        parsed = nlp.parse_message(text)
    intent = parsed["intent"]

    dispatch = {
        "add":            _add_task,
        "list":           _list_tasks,
        "schedule":       _schedule_intent,
        "schedule_direct":_schedule_direct_intent,
        "complete":       _complete_intent,
        "delete":         _delete_intent,
        "update":         _update_intent,
        "reschedule":     _reschedule_intent,
        "habit_add":      _habit_add,
        "habit_list":     lambda u, c, *_: cmd_habits(u, c),
        "habit_delete":   _habit_delete,
        "calendar_query": _calendar_query,
        "help":           lambda u, c, *_: cmd_help(u, c),
        "clarify":        _handle_clarify,
    }

    handler = dispatch.get(intent)
    if handler:
        await handler(update, context, parsed)
    else:
        await update.message.reply_text(
            "🤔 Not sure what you mean. Try:\n"
            "• `add [task] by [date]`\n"
            "• `what do I have this week?`\n"
            "• `schedule running session on Wednesday 9pm`\n"
            "• `every Monday at 9am remind me to do X`\n"
            "Or /help for all examples."
        )


# ── ADD TASK ───────────────────────────────────────────────────────────────────

async def _add_task(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    # Multi-task add: "I have midterm Thursday and assignment due Friday"
    titles    = parsed.get("titles")
    deadlines = parsed.get("deadlines")
    priority  = parsed.get("priority", "medium") or "medium"

    if titles and len(titles) > 1:
        added = []
        for i, t in enumerate(titles):
            dl  = deadlines[i] if deadlines and i < len(deadlines) else None
            tid = db.add_task(title=t, deadline=dl, priority=priority)
            due = f" — due {_fmt_deadline(dl)}" if dl else ""
            added.append(f"  {PRIORITY_ICON[priority]} {t}{due} (#{tid})")
        await update.message.reply_text(
            f"✅ *{len(added)} tasks added!*\n\n" + "\n".join(added),
            parse_mode="Markdown",
        )
        return

    title = (parsed.get("title") or "").strip()
    dt    = parsed.get("datetime")

    if not title or len(title) < 2:
        await update.message.reply_text("What's the task? E.g. `finish report by Friday`")
        return

    deadline_str = dt.date().isoformat() if dt else None
    task_id      = db.add_task(title=title, deadline=deadline_str, priority=priority)
    p_icon       = PRIORITY_ICON.get(priority, "🟡")

    reply  = f"✅ *Task added!*\n\n{p_icon} *{title}*\n"
    reply += f"Due: {_fmt_deadline(deadline_str)}\n" if deadline_str else "No deadline\n"
    reply += f"Priority: {priority}\n"
    reply += f"ID: #{task_id}"
    await update.message.reply_text(reply, parse_mode="Markdown")


# ── LIST TASKS ─────────────────────────────────────────────────────────────────

async def _list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    text_lower = parsed["raw"].lower()
    if "today" in text_lower:
        today = datetime.now().date()
        tasks  = db.get_tasks_by_period(today, today)
        header = "📅 *Today's Tasks*"
    elif "next week" in text_lower:
        today = datetime.now().date()
        start = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
        end   = start + timedelta(days=6)
        tasks  = db.get_tasks_by_period(start, end)
        header = "📆 *Next Week's Tasks*"
    elif any(w in text_lower for w in ("this week", "week")):
        tasks  = db.get_tasks_this_week()
        header = "📆 *This Week's Tasks*"
    else:
        tasks  = db.get_all_tasks(status="pending")
        header = "📋 *All Pending Tasks*"

    if not tasks:
        await update.message.reply_text("No tasks found for that period 🎉")
        return

    context.user_data["last_task_list"] = [dict(t) for t in tasks]
    await update.message.reply_text(
        f"{header} ({len(tasks)})\n\n{_fmt_task_list(tasks)}\n\n"
        "_Reply `schedule 1 2 3` to add to your calendar_",
        parse_mode="Markdown",
    )


# ── SCHEDULE TASKS (from task list) ───────────────────────────────────────────

async def _schedule_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    raw       = parsed["raw"]
    numbers   = [int(n) for n in re.findall(r"\d+", raw)]
    last_list = context.user_data.get("last_task_list", [])

    if not last_list:
        tasks = db.get_all_tasks(status="pending") + db.get_all_tasks(status="in_progress")
        if not tasks:
            await update.message.reply_text("No pending tasks to schedule!")
            return
        context.user_data["last_task_list"] = [dict(t) for t in tasks]
        last_list = context.user_data["last_task_list"]
        await update.message.reply_text(
            f"📋 *Your Tasks*\n\n{_fmt_task_list(tasks)}\n\nWhich numbers to schedule?",
            parse_mode="Markdown",
        )
        return

    if not numbers:
        await update.message.reply_text("Tell me which tasks by number, e.g. `schedule 1 3`")
        return

    task_ids = [
        last_list[n - 1]["id"]
        for n in numbers
        if 1 <= n <= len(last_list)
    ]
    if not task_ids:
        await update.message.reply_text("Those numbers don't match. Try /tasks first.")
        return

    context.user_data["state"]            = "scheduling"
    context.user_data["pending_schedule"] = task_ids
    context.user_data["schedule_idx"]     = 0
    await _ask_schedule_time(update, context)


async def _ask_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    idx      = context.user_data["schedule_idx"]
    task_ids = context.user_data["pending_schedule"]
    task     = db.get_task(task_ids[idx])
    total    = len(task_ids)

    # Try smart suggestions
    try:
        free_slots = smart_schedule.get_free_slots(days_ahead=5, min_duration_min=60)
        msg, best  = smart_schedule.build_suggestion_message(task["title"], 60, free_slots)
        header     = f"⏰ *Schedule {idx+1}/{total}* — _{task['title']}_\n\n"
        context.user_data["schedule_suggested"] = best
        await update.message.reply_text(header + msg, parse_mode="Markdown")
    except Exception:
        context.user_data["schedule_suggested"] = None
        await update.message.reply_text(
            f"⏰ *Schedule {idx+1}/{total}*\n\n"
            f"Task: _{task['title']}_\n\n"
            "When? (e.g. `Thursday 2pm for 2 hours remind me 40 mins before`)\n"
            "_Type /cancel to stop._",
            parse_mode="Markdown",
        )


async def _scheduling_time(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    idx       = context.user_data["schedule_idx"]
    task_ids  = context.user_data["pending_schedule"]
    task_id   = task_ids[idx]
    task      = db.get_task(task_id)
    suggested = context.user_data.get("schedule_suggested")

    # Confirmed suggested slot
    if suggested and text.lower().strip() in ("yes", "y", "yep", "sure", "ok", "confirm"):
        dt = suggested["start"]
    # Rejected — ask plainly
    elif suggested and text.lower().strip() in ("no", "n", "nope"):
        context.user_data["schedule_suggested"] = None
        await update.message.reply_text(
            f"When would you like to schedule _{task['title']}_?\n"
            "(e.g. `Thursday 2pm for 2 hours`)",
            parse_mode="Markdown",
        )
        return
    # Numbered slot pick
    elif re.match(r"^\d+$", text.strip()):
        try:
            free_slots = smart_schedule.get_free_slots(days_ahead=5, min_duration_min=60)
            n = int(text.strip()) - 1
            dt = free_slots[n]["start"] if 0 <= n < len(free_slots) else None
        except Exception:
            dt = None
        if not dt:
            dt = nlp.extract_datetime(text)
    else:
        dt = nlp.extract_datetime(text)

    if not dt:
        await update.message.reply_text("Couldn't parse that. Try `Thursday 2pm`, pick a number, or `yes` to confirm.")
        return

    duration  = nlp.extract_duration(text)
    reminder  = nlp.extract_reminder_minutes(text)
    end_dt    = dt + timedelta(minutes=duration)

    try:
        event_id = calendar_client.create_event(
            title            = task["title"],
            start            = dt,
            duration_minutes = duration,
            reminder_minutes = reminder,
            description      = f"Scheduled via Planning Bot (task #{task_id})",
        )
        db.update_task(
            task_id,
            calendar_event_id = event_id,
            scheduled_start   = dt.isoformat(),
            scheduled_end     = end_dt.isoformat(),
            status            = "in_progress",
        )
        await update.message.reply_text(
            f"✅ *Scheduled!*\n\n"
            f"*{task['title']}*\n"
            f"{_fmt_dt(dt)} → {end_dt.strftime('%I:%M %p')}\n"
            f"⏰ Reminder: {reminder} min before",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Calendar error: {exc}")

    next_idx = idx + 1
    if next_idx < len(task_ids):
        context.user_data["schedule_idx"] = next_idx
        await _ask_schedule_time(update, context)
    else:
        context.user_data["state"]            = "idle"
        context.user_data["pending_schedule"] = []
        await update.message.reply_text("🎉 All tasks scheduled!")


# ── DIRECT CALENDAR SCHEDULING (no task needed) ───────────────────────────────

async def _schedule_direct_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    title       = parsed.get("title", "").strip()
    dt          = parsed.get("datetime")
    recurrence  = parsed.get("recurrence")
    reminder    = parsed.get("reminder", 30)
    duration    = parsed.get("duration", 60)
    multi_slots = parsed.get("multi_slots")

    if not title or len(title) < 2:
        await update.message.reply_text(
            "What do you want to schedule? E.g.\n"
            "`schedule gym tuesday 10pm and friday 9am`\n"
            "`schedule running session on Wednesday 9pm`"
        )
        return

    # Multi-slot: create all events in one go
    if multi_slots and len(multi_slots) >= 2:
        await _do_multi_slot_schedule(update, title, multi_slots, duration, reminder)
        return

    # Single slot with time — schedule directly
    if dt:
        await _do_direct_schedule(update, title, dt, duration, reminder, recurrence)
        return

    # No time — use smart scheduling to suggest slots
    context.user_data["state"]             = "schedule_direct"
    context.user_data["direct_title"]      = title
    context.user_data["direct_recurrence"] = recurrence
    context.user_data["direct_reminder"]   = reminder
    context.user_data["direct_duration"]   = duration
    context.user_data["direct_suggested"]  = None

    try:
        free_slots = smart_schedule.get_free_slots(days_ahead=5, min_duration_min=duration)
        msg, best  = smart_schedule.build_suggestion_message(title, duration, free_slots)
        if best:
            context.user_data["direct_suggested"] = best
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception:
        recap = f"_{recurrence['summary']}_" if recurrence else "once"
        await update.message.reply_text(
            f"📅 Scheduling: *{title}* ({recap})\n\n"
            "When? (e.g. `Wednesday 9pm for 1 hour` or `tuesday 10pm and friday 9am`)",
            parse_mode="Markdown",
        )


async def _schedule_direct_time(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    title      = context.user_data.get("direct_title", "Event")
    recurrence = context.user_data.get("direct_recurrence")
    reminder   = nlp.extract_reminder_minutes(text) or context.user_data.get("direct_reminder", 30)
    duration   = nlp.extract_duration(text) or context.user_data.get("direct_duration", 60)
    suggested  = context.user_data.get("direct_suggested")

    # User confirmed the suggested slot
    if suggested and text.lower().strip() in ("yes", "y", "yep", "sure", "ok", "confirm"):
        context.user_data["state"] = "idle"
        await _do_direct_schedule(update, title, suggested["start"], duration, reminder, recurrence)
        return

    # User said no — show more options
    if suggested and text.lower().strip() in ("no", "n", "nope", "other", "other options"):
        context.user_data["direct_suggested"] = None
        try:
            free_slots = smart_schedule.get_free_slots(days_ahead=7, min_duration_min=duration)
            slots_text = smart_schedule.format_slots_for_display(free_slots, limit=6)
            await update.message.reply_text(
                f"📅 Here are your free slots for *{title}*:\n\n{slots_text}\n\n"
                "Reply with a number or type a specific time.",
                parse_mode="Markdown",
            )
        except Exception:
            await update.message.reply_text("When would you like to schedule it?")
        return

    # User picked a numbered slot from the list
    if re.match(r"^\d+$", text.strip()):
        try:
            free_slots = smart_schedule.get_free_slots(days_ahead=7, min_duration_min=duration)
            n = int(text.strip()) - 1
            if 0 <= n < len(free_slots):
                context.user_data["state"] = "idle"
                await _do_direct_schedule(update, title, free_slots[n]["start"], duration, reminder, recurrence)
                return
        except Exception:
            pass

    # Check for multi-slot reply
    multi_slots = nlp.extract_multi_slots(text)
    if multi_slots and len(multi_slots) >= 2:
        context.user_data["state"] = "idle"
        await _do_multi_slot_schedule(update, title, multi_slots, duration, reminder)
        return

    dt = nlp.extract_datetime(text)
    if not dt:
        await update.message.reply_text(
            "Couldn't parse that. Try `Wednesday 9pm`, pick a slot number, or reply `yes` to confirm the suggestion."
        )
        return

    context.user_data["state"] = "idle"
    await _do_direct_schedule(update, title, dt, duration, reminder, recurrence)


async def _do_multi_slot_schedule(update, title, slots, duration, reminder):
    """Create one calendar event per slot, confirm all at once."""
    lines    = [f"✅ *Scheduled {len(slots)} sessions:*\n\n*{title}*\n"]
    errors   = []

    for dt in slots:
        end_dt = dt + timedelta(minutes=duration)
        try:
            calendar_client.create_event(
                title            = title,
                start            = dt,
                duration_minutes = duration,
                reminder_minutes = reminder,
            )
            lines.append(f"  • {_fmt_dt(dt)} → {end_dt.strftime('%I:%M %p')}")
        except Exception as exc:
            errors.append(f"{_fmt_dt(dt)}: {exc}")

    lines.append(f"\n⏰ Reminder: {reminder} min before each")
    if errors:
        lines.append("\n⚠️ Failed:\n" + "\n".join(errors))

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _do_direct_schedule(update, title, dt, duration, reminder, recurrence):
    end_dt = dt + timedelta(minutes=duration)
    try:
        rrule    = recurrence["rrule"] if recurrence else None
        calendar_client.create_event(
            title            = title,
            start            = dt,
            duration_minutes = duration,
            reminder_minutes = reminder,
            rrule            = rrule,
        )
        recur_str = f"\n🔁 {recurrence['summary'].capitalize()}" if recurrence else ""
        await update.message.reply_text(
            f"✅ *Scheduled!*\n\n"
            f"*{title}*\n"
            f"{_fmt_dt(dt)} → {end_dt.strftime('%I:%M %p')}"
            f"{recur_str}\n"
            f"⏰ Reminder: {reminder} min before",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Calendar error: {exc}")


# ── COMPLETE ───────────────────────────────────────────────────────────────────

async def _complete_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    numbers   = [int(n) for n in re.findall(r"\d+", parsed["raw"])]
    last_list = context.user_data.get("last_task_list", [])

    if not numbers:
        await update.message.reply_text("Which task? E.g. `mark task 2 done` or `done 1 3`")
        return

    completed = []
    for n in numbers:
        task_id = last_list[n - 1]["id"] if (last_list and 1 <= n <= len(last_list)) else n
        task    = db.get_task(task_id)
        if task:
            db.complete_task(task_id)
            completed.append(task["title"])

    if completed:
        await update.message.reply_text(
            "✅ Marked as done:\n" + "\n".join(f"• {t}" for t in completed)
        )
    else:
        await update.message.reply_text("Couldn't find those tasks. Try /tasks first.")


# ── DELETE ─────────────────────────────────────────────────────────────────────

async def _delete_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    numbers   = [int(n) for n in re.findall(r"\d+", parsed["raw"])]
    last_list = context.user_data.get("last_task_list", [])

    if not numbers:
        await update.message.reply_text("Which task to delete? E.g. `delete task 2`")
        return

    n       = numbers[0]
    task_id = last_list[n - 1]["id"] if (last_list and 1 <= n <= len(last_list)) else n
    task    = db.get_task(task_id)

    if not task:
        await update.message.reply_text(f"Task #{task_id} not found.")
        return

    context.user_data["state"]             = "deleting"
    context.user_data["pending_delete_id"] = task_id
    cal_note = " and remove from Google Calendar" if task["calendar_event_id"] else ""
    await update.message.reply_text(
        f"🗑 Delete *{task['title']}*{cal_note}?\n\nReply `yes` to confirm.",
        parse_mode="Markdown",
    )


async def _delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if text.lower() in ("yes", "y", "yep", "sure", "confirm", "delete"):
        task_id = context.user_data.get("pending_delete_id")
        task    = db.delete_task(task_id)
        if task and task["calendar_event_id"]:
            try:
                calendar_client.delete_event(task["calendar_event_id"])
                msg = f"🗑 Deleted *{task['title']}* and removed from calendar."
            except Exception:
                msg = f"🗑 Deleted *{task['title']}* (calendar removal failed)."
        else:
            msg = f"🗑 Deleted *{task['title'] if task else 'task'}*."
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Deletion cancelled.")

    context.user_data["state"] = "idle"
    context.user_data.pop("pending_delete_id", None)


# ── UPDATE ─────────────────────────────────────────────────────────────────────

async def _update_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    raw       = parsed["raw"]
    numbers   = [int(n) for n in re.findall(r"\d+", raw)]
    last_list = context.user_data.get("last_task_list", [])

    if not numbers:
        await update.message.reply_text("Which task? E.g. `update task 1 deadline to Monday`")
        return

    n       = numbers[0]
    task_id = last_list[n - 1]["id"] if (last_list and 1 <= n <= len(last_list)) else n
    task    = db.get_task(task_id)

    if not task:
        await update.message.reply_text(f"Task #{task_id} not found.")
        return

    updates = {}

    rename = re.search(r"\brename\b.{0,10}\bto\s+(.+)$", raw, re.IGNORECASE)
    if rename:
        updates["title"] = rename.group(1).strip().capitalize()

    if any(w in raw.lower() for w in ("deadline", "due", "move", "reschedule", "by", "to")):
        clean = re.sub(r"\b(update|edit|change|move|reschedule|task|deadline|due|to|by)\b", " ", raw, flags=re.IGNORECASE)
        clean = re.sub(r"\b\d+\b", " ", clean)
        dt    = nlp.extract_datetime(clean)
        if dt:
            updates["deadline"] = dt.date().isoformat()

    if updates:
        db.update_task(task_id, **updates)
        if "title" in updates and task["calendar_event_id"]:
            try:
                calendar_client.update_event(task["calendar_event_id"], title=updates["title"])
            except Exception:
                pass
        lines = []
        if "title"    in updates: lines.append(f"Title → {updates['title']}")
        if "deadline" in updates: lines.append(f"Deadline → {_fmt_deadline(updates['deadline'])}")
        await update.message.reply_text(
            f"✏️ Updated *{task['title']}*:\n" + "\n".join(lines),
            parse_mode="Markdown",
        )
    else:
        context.user_data["state"]             = "updating"
        context.user_data["pending_update_id"] = task_id
        await update.message.reply_text(
            f"✏️ Updating *{task['title']}*\n\n"
            "What to change?\n"
            "• New deadline — e.g. `next Monday`\n"
            "• Rename — e.g. `rename: New Title`",
            parse_mode="Markdown",
        )


async def _update_value(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    task_id = context.user_data.get("pending_update_id")
    task    = db.get_task(task_id)
    if not task:
        await update.message.reply_text("Task not found.")
        context.user_data["state"] = "idle"
        return

    updates = {}
    if text.lower().startswith("rename:"):
        updates["title"] = text[7:].strip().capitalize()
    else:
        dt = nlp.extract_datetime(text)
        if dt:
            updates["deadline"] = dt.date().isoformat()

    if not updates:
        await update.message.reply_text(
            "Couldn't parse that. Try `next Monday` or `rename: New Title`.\nOr /cancel."
        )
        return

    db.update_task(task_id, **updates)
    lines = []
    if "title"    in updates: lines.append(f"Title → {updates['title']}")
    if "deadline" in updates: lines.append(f"Deadline → {_fmt_deadline(updates['deadline'])}")
    await update.message.reply_text("✏️ Updated:\n" + "\n".join(lines))
    context.user_data["state"] = "idle"
    context.user_data.pop("pending_update_id", None)


# ── HABITS ─────────────────────────────────────────────────────────────────────

async def _habit_add(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    title     = parsed.get("title", "").strip()
    frequency = parsed.get("frequency", "weekly")
    count     = parsed.get("count", 1)
    notes     = parsed.get("notes")

    if not title or len(title) < 2:
        await update.message.reply_text(
            "What's the habit?\n\n"
            "Weekly habits (shown every Sunday):\n"
            "• `add weekly habit: gym 2 times`\n"
            "• `add weekly habit: running 4 times`\n\n"
            "Daily habits (shown every morning):\n"
            "• `add daily habit: read 30 min`\n"
            "• `add daily habit: cold shower`"
        )
        return

    habit_id  = db.add_habit(title=title, frequency=frequency, count=count, notes=notes)
    freq_desc = "every morning" if frequency == "daily" else "every Sunday"
    count_str = f" — {count}x per week" if frequency == "weekly" and count > 1 else ""
    note_str  = f" ({notes})" if notes else ""

    await update.message.reply_text(
        f"🔁 *Habit added!*\n\n"
        f"*{title}*{note_str}{count_str}\n"
        f"Reminder: {freq_desc} at planning time\n"
        f"ID: #{habit_id}",
        parse_mode="Markdown",
    )


async def _habit_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    numbers = [int(n) for n in re.findall(r"\d+", parsed["raw"])]
    if not numbers:
        await update.message.reply_text("Which habit? E.g. `delete habit 2` — see /habits for the list.")
        return

    habits = db.get_habits(active_only=True)
    n      = numbers[0]
    if 1 <= n <= len(habits):
        habit = db.delete_habit(habits[n - 1]["id"])
        await update.message.reply_text(f"🗑 Deleted habit: *{habit['title']}*", parse_mode="Markdown")
    else:
        await update.message.reply_text("That number doesn't match. Check /habits.")


# ── RESCHEDULE ────────────────────────────────────────────────────────────────

async def _reschedule_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    """
    Natural rescheduling: find a calendar event by title and move it.
    e.g. "reschedule my gym to tomorrow 6pm"
         "move Tuesday standup to Wednesday same time"
         "push my report session to Friday"
    """
    raw   = parsed.get("raw", "")
    title = parsed.get("title", "").strip()
    dt    = parsed.get("datetime")

    if not title:
        await update.message.reply_text(
            "Which event? E.g.\n"
            "`reschedule gym to tomorrow 6pm`\n"
            "`move standup to Wednesday same time`"
        )
        return

    # Search calendar for matching events
    try:
        matches = calendar_client.search_events_by_title(title, days_ahead=14)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Couldn't search calendar: {exc}")
        return

    if not matches:
        await update.message.reply_text(
            f"Couldn't find *{title}* in your calendar (next 14 days).\n"
            "Try a different name or check /calendar.",
            parse_mode="Markdown",
        )
        return

    # If multiple matches, ask which one
    if len(matches) > 1:
        lines = [f"Found {len(matches)} events matching *{title}*:\n"]
        for i, e in enumerate(matches[:5]):
            lines.append(f"  {i+1}. {e.get('summary','?')} — {calendar_client.fmt_event_time(e)}")
        lines.append("\nReply with the number to reschedule.")
        context.user_data["state"]             = "reschedule_pick"
        context.user_data["reschedule_matches"]= matches[:5]
        context.user_data["reschedule_new_dt"] = dt
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    # Single match
    event = matches[0]
    if not dt:
        context.user_data["state"]              = "reschedule_time"
        context.user_data["reschedule_event"]   = event
        await update.message.reply_text(
            f"📅 Moving *{event.get('summary','Event')}*\n"
            f"Currently: {calendar_client.fmt_event_time(event)}\n\n"
            "New time? (e.g. `tomorrow 6pm`, `Friday same time`)",
            parse_mode="Markdown",
        )
        return

    await _do_reschedule(update, event, dt)


async def _do_reschedule(update, event: dict, new_dt: datetime):
    try:
        dur = calendar_client.reschedule_event(event["id"], new_dt)
        end = new_dt + timedelta(minutes=dur)

        # Also update task DB if this event is linked
        with __import__("sqlite3").connect(__import__("os").getenv("DB_PATH", "planner.db")) as conn:
            conn.row_factory = __import__("sqlite3").Row
            task = conn.execute(
                "SELECT * FROM tasks WHERE calendar_event_id = ?", (event["id"],)
            ).fetchone()
            if task:
                conn.execute(
                    "UPDATE tasks SET scheduled_start=?, scheduled_end=? WHERE id=?",
                    (new_dt.isoformat(), end.isoformat(), task["id"])
                )
                conn.commit()

        title = event.get("summary", "Event")
        await update.message.reply_text(
            f"✅ *Rescheduled!*\n\n"
            f"*{title}*\n"
            f"New time: {_fmt_dt(new_dt)} → {end.strftime('%I:%M %p')}",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Reschedule failed: {exc}")


# ── CALENDAR QUERY ────────────────────────────────────────────────────────────

async def _calendar_query(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    query = parsed.get("query") or parsed.get("raw", "")
    await update.message.reply_text("🔍 Let me check your calendar...", parse_mode="Markdown")
    try:
        events = calendar_client.list_upcoming_events(days=14)
        tasks  = [dict(t) for t in db.get_all_tasks()]
        answer = llm_client.answer_calendar_query(query, events, tasks)
        await update.message.reply_text(f"📅 {answer}")
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Couldn't query calendar: {exc}")


# ── CLARIFY ────────────────────────────────────────────────────────────────────

async def _handle_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    question = parsed.get("clarify", "Could you be more specific?")
    context.user_data["state"]            = "clarifying"
    context.user_data["clarify_original"] = parsed.get("raw", "")
    await update.message.reply_text(f"🤔 {question}")


# ── Entry point ────────────────────────────────────────────────────────────────

async def post_init(app):
    scheduler = setup_scheduler(app)
    scheduler.start()
    logger.info("✅ Scheduler started")


def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN is not set.")

    db.init_db()

    token_b64 = os.getenv("GOOGLE_TOKEN_B64")
    if token_b64 and not os.path.exists("token.json"):
        with open("token.json", "w") as f:
            f.write(base64.b64decode(token_b64).decode())
        logger.info("Wrote token.json from GOOGLE_TOKEN_B64")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("tasks",    cmd_tasks))
    app.add_handler(CommandHandler("today",    cmd_today))
    app.add_handler(CommandHandler("week",     cmd_week))
    app.add_handler(CommandHandler("calendar", cmd_calendar))
    app.add_handler(CommandHandler("habits",   cmd_habits))
    app.add_handler(CommandHandler("now",      cmd_now))
    app.add_handler(CommandHandler("cancel",   cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()