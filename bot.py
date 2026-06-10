"""
Planning Telegram Bot — main entry point.

State machine in context.user_data['state']:
  idle            → normal routing
  scheduling      → collecting time/duration for a batch of tasks
  schedule_direct → collecting time for a direct calendar event
  deleting        → confirmation
  updating        → collecting new value
  reminder_time   → collecting time for an app reminder
  plan_confirm    → confirming suggested task/calendar placements
  reschedule_pick → picking one calendar event from matches
  reschedule_time → collecting new time for a calendar event
  clarifying      → collecting an LLM clarification answer
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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


# ── Entity references (for reply-to-edit) ──────────────────────────────────────

_ENTITY_REF_RE = re.compile(r"📎\s*(t|r|e)#(\S+)", re.IGNORECASE)


def _parse_entity_ref(text: str):
    """Extract entity reference from a bot message.
    Returns (type, id) where type is 'task', 'reminder', or 'cal_event',
    or None if no reference found."""
    if not text:
        return None
    m = _ENTITY_REF_RE.search(text)
    if not m:
        return None
    prefix = m.group(1).lower()
    entity_id = m.group(2)
    mapping = {"t": "task", "r": "reminder", "e": "cal_event"}
    return (mapping.get(prefix), entity_id) if prefix in mapping else None


def _entity_ref_line(entity_type: str, entity_id) -> str:
    """Build the reference line to append to a bot message."""
    prefix_map = {"task": "t", "reminder": "r", "cal_event": "e"}
    p = prefix_map.get(entity_type, "?")
    return f"\n📎 {p}#{entity_id}"


def _fmt_entity(entity_type: str, entity) -> str:
    """Format an entity's current state for the edit-confirmation message."""
    lines = []
    if entity_type == "task":
        title = entity.get("title", "?")
        lines.append(f"📝 *{title}*")
        if entity.get("deadline"):
            lines.append(f"📅 Due: {_fmt_deadline(entity['deadline'])}")
        if entity.get("scheduled_start"):
            try:
                dt = datetime.fromisoformat(entity["scheduled_start"])
                lines.append(f"🕐 Scheduled: {_fmt_dt(dt)}")
            except Exception:
                pass
        if entity.get("notes"):
            lines.append(f"📌 Notes: {entity['notes']}")
        lines.append(f"🔖 ID: #{entity['id']}")
    elif entity_type == "reminder":
        title = entity.get("title", "?")
        lines.append(f"🔔 *{title}*")
        try:
            dt = datetime.fromisoformat(entity["remind_at"])
            lines.append(f"🕐 {_fmt_dt(dt)}")
        except Exception:
            lines.append(f"🕐 {entity.get('remind_at', '?')}")
        lines.append(f"🔖 ID: #{entity['id']}")
    return "\n".join(lines)


async def _handle_reply_edit(update, context) -> bool:
    """Try to interpret a reply as an edit to the referenced entity.
    Returns True if the reply was handled as an edit, False to fall through."""
    replied = update.message.reply_to_message
    text = update.message.text.strip()

    # Extract entity reference from the replied message
    ref = _parse_entity_ref(replied.text or "")
    if not ref:
        return False

    entity_type, entity_id = ref

    # Try to parse as a time update
    new_dt = nlp.extract_datetime(text)

    if entity_type == "reminder":
        reminder = db.get_reminder(int(entity_id))
        if not reminder:
            await update.message.reply_text("⚠️ That reminder no longer exists.")
            return True

        if new_dt:
            db.update_reminder(reminder["id"], remind_at=new_dt.isoformat())
            reminder = db.get_reminder(reminder["id"])  # re-fetch
            await update.message.reply_text(
                f"✅ *Reminder Updated*\n\n{_fmt_entity('reminder', reminder)}"
                f"{_entity_ref_line('reminder', reminder['id'])}",
                parse_mode="Markdown",
            )
            return True

        # Maybe user gave a new title
        if len(text) > 2 and text.lower() not in ("yes", "no", "ok", "cancel", "done"):
            db.update_reminder(reminder["id"], title=text)
            reminder = db.get_reminder(reminder["id"])
            await update.message.reply_text(
                f"✅ *Reminder Updated*\n\n{_fmt_entity('reminder', reminder)}"
                f"{_entity_ref_line('reminder', reminder['id'])}",
                parse_mode="Markdown",
            )
            return True

        return False

    elif entity_type == "task":
        task = db.get_task(int(entity_id))
        if not task:
            await update.message.reply_text("⚠️ That task no longer exists.")
            return True

        updates = {}
        if new_dt:
            updates["deadline"] = new_dt.date().isoformat()

        # Check for title: just text without time, longer than 2 chars
        if not updates and len(text) > 2 and text.lower() not in ("yes", "no", "ok", "cancel", "done"):
            updates["title"] = text

        if updates:
            db.update_task(task["id"], **updates)
            task = db.get_task(task["id"])
            await update.message.reply_text(
                f"✏️ *Task Updated*\n\n{_fmt_entity('task', task)}"
                f"{_entity_ref_line('task', task['id'])}",
                parse_mode="Markdown",
            )
            return True

        return False

    elif entity_type == "cal_event":
        if not new_dt:
            return False
        try:
            from calendar_client import reschedule_event as _reschedule
            dur = _reschedule(entity_id, new_dt)
            end = new_dt + timedelta(minutes=dur)
            await update.message.reply_text(
                f"✅ *Calendar Event Updated*\n\n"
                f"New time: {_fmt_dt(new_dt)} → {end.strftime('%I:%M %p')}"
                f"{_entity_ref_line('cal_event', entity_id)}",
                parse_mode="Markdown",
            )
            return True
        except Exception as exc:
            await update.message.reply_text(f"⚠️ Couldn't update calendar event: {exc}")
            return True

    return False


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
        "› `find me 2 hours this week`\n"
        "› `plan my unscheduled tasks`\n"
        "› `every Monday at 9am remind me to review goals`\n"
        "› `add daily habit: drink water at 8am`\n\n"
        "Commands: /tasks /today /tomorrow /week /habits /free /plan /help",
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
        "/habits — your active habits\n"
        "/free — free calendar blocks\n"
        "/plan — suggested placements for unscheduled tasks\n"
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
        "🧠 *Planning*\n"
        "• `find me 2 hours this week`\n"
        "• `when am I free tomorrow?`\n"
        "• `plan my unscheduled tasks`\n"
        "• `add report by Friday needs 2 hours`\n\n"
        "🔁 *Recurring & habits*\n"
        "• `every Monday at 9am remind me to review goals`\n"
        "• `add daily habit: drink water at 8am`\n"
        "• `every last day of month remind me to log expenses`\n\n"
        "⏰ *Custom reminders* — sent by the bot, not calendar blocks\n"
        "• `schedule gym on Friday 6am remind me 1 hour before`\n"
        "• `remind me tomorrow 4pm to check results`",
        parse_mode="Markdown",
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    tasks = db.get_unscheduled_tasks_sorted()
    if not tasks:
        await update.message.reply_text("✨ No unscheduled tasks — you're all clear!")
        return
    context.user_data["last_task_list"] = [dict(t) for t in tasks]
    lines = [f"📋 *Unscheduled Tasks* ({len(tasks)})\n"]
    for i, t in enumerate(tasks):
        due = f" — due {_fmt_deadline(t['deadline'])}" if t["deadline"] else " — no deadline"
        icon = STATUS_ICON.get(t["status"], "⏳")
        p_icon = PRIORITY_ICON.get(t["priority"] or "medium", "🟡")
        lines.append(f"{i+1}. {icon}{p_icon} {t['title']}{due}")
    lines.append("\n_Reply `schedule 1 2` to add to calendar_")
    lines.append("_Reply `plan my tasks` for suggestions_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
            lines.append("*Calendar:*")
            for e in events:
                title = e.get('summary', 'Event')
                lines.append(f"  • *{title}* — {calendar_client.fmt_event_time_range(e)}")
    except Exception:
        pass

    if len(lines) == 1:
        lines.append("Nothing on the plate today! 🎉")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    today    = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    tasks    = db.get_tasks_by_period(tomorrow, tomorrow)
    lines    = [f"📅 *Tomorrow — {tomorrow.strftime('%A %b %d')}*\n"]

    if tasks:
        context.user_data["last_task_list"] = [dict(t) for t in tasks]
        lines.append("📋 *Tasks:*")
        for i, t in enumerate(tasks):
            lines.append(_fmt_task_row(i + 1, t))
        lines.append("")

    try:
        events = calendar_client.list_events_by_day(tomorrow)
        if events:
            lines.append("*Calendar:*")
            for e in events:
                title = e.get('summary', 'Event')
                lines.append(f"  • *{title}* — {calendar_client.fmt_event_time_range(e)}")
    except Exception:
        pass

    if len(lines) == 1:
        lines.append("Nothing on the plate tomorrow! 🎉")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    import pytz
    tz = pytz.timezone(os.getenv("TIMEZONE", "Asia/Seoul"))
    today = datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())
    lines = [f"📆 *This Week* ({monday.strftime('%b %d')} — {(monday + timedelta(days=6)).strftime('%b %d')})\n"]

    has_content = False
    for day_offset in range(7):
        day = monday + timedelta(days=day_offset)
        day_tasks = db.get_tasks_by_period(day, day)
        day_events = []
        try:
            day_events = calendar_client.list_events_by_day(day)
        except Exception:
            pass

        if not day_tasks and not day_events:
            continue
        has_content = True
        day_label = "☀️ *Today*" if day == today else day.strftime("%A %b %d")
        lines.append(f"\n{day_label}:")
        for t in day_tasks:
            cal_tag = " 📅" if t["calendar_event_id"] else " ⏳"
            due_tag = f" — due {_fmt_deadline(t['deadline'])}" if t["deadline"] else ""
            icon = STATUS_ICON.get(t["status"], "⏳")
            p_icon = PRIORITY_ICON.get(t["priority"] or "medium", "🟡")
            lines.append(f"  {icon}{p_icon} {t['title']}{due_tag}{cal_tag}")
        for e in day_events:
            try:
                title = e.get('summary', 'Event')
                lines.append(f"  • *{title}* — {calendar_client.fmt_event_time_range(e)}")
            except Exception:
                lines.append(f"  • *{e.get('summary','Event')}*")

    if not has_content:
        lines.append("Nothing scheduled this week 🎉")

    # Also show unscheduled tasks
    unscheduled = db.get_unscheduled_tasks_sorted()
    if unscheduled:
        lines.append(f"\n📝 *Unscheduled ({len(unscheduled)}):*")
        for i, t in enumerate(unscheduled[:5]):
            due = f" — due {_fmt_deadline(t['deadline'])}" if t["deadline"] else ""
            p_icon = PRIORITY_ICON.get(t["priority"] or "medium", "🟡")
            lines.append(f"  {i+1}. {p_icon} {t['title']}{due}")
        if len(unscheduled) > 5:
            lines.append(f"  _...and {len(unscheduled)-5} more_")

    context.user_data["last_task_list"] = [dict(t) for t in db.get_unscheduled_tasks_sorted()]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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


async def cmd_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    text = " ".join(context.args) if context.args else "find me 60 minutes this week"
    parsed = nlp.parse_message(text)
    parsed["intent"] = "free_time"
    await _free_time_intent(update, context, parsed)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await _plan_tasks_intent(update, context, {"intent": "plan", "raw": "plan my tasks"})


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

    # NEW: Check if user is replying to a bot message to edit it
    if state == "idle" and update.message.reply_to_message:
        replied = update.message.reply_to_message
        if replied.from_user and replied.from_user.is_bot:
            if await _handle_reply_edit(update, context):
                return

    # Check for "resume" to restore paused scheduling
    if state == "idle" and text.strip().lower() == "resume":
        paused = context.user_data.get("paused_schedule")
        if paused:
            context.user_data["state"] = "scheduling"
            context.user_data["pending_schedule"] = paused["task_ids"]
            context.user_data["schedule_idx"] = paused["idx"]
            context.user_data.pop("paused_schedule", None)
            await update.message.reply_text("⏩ Resuming scheduling...")
            await _ask_schedule_time(update, context)
            return

    if state == "scheduling":
        await _scheduling_time(update, context, text)
        return
    if state == "scheduling_end_time":
        await _scheduling_end_time(update, context, text)
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
    if state == "reminder_time":
        pending  = context.user_data.pop("pending_reminder", {})
        title    = pending.get("title", "Reminder")
        recurrence = pending.get("recurrence")
        dt       = nlp.extract_datetime(text)
        if not dt:
            # Try LLM
            p2 = llm_client.parse(text)
            if p2:
                dt = llm_client.normalise(p2).get("datetime")
        if not dt:
            await update.message.reply_text("Couldn't parse that time. Try `tomorrow 4pm` or `Monday 9am`.")
            # Put it back
            context.user_data["pending_reminder"] = pending
            return
        context.user_data["state"] = "idle"
        await _do_create_reminder(update, title, dt, recurrence)
        return

    if state == "plan_confirm":
        await _plan_confirm(update, context, text)
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

    if state == "confirm_schedule":
        await _confirm_schedule(update, context, text)
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
            "free_time": _free_time_intent, "plan": _plan_tasks_intent,
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
        "batch_schedule": _batch_schedule_intent,
        "schedule_direct":_schedule_direct_intent,
        "complete":       _complete_intent,
        "delete":         _delete_intent,
        "update":         _update_intent,
        "reschedule":     _reschedule_intent,
        "habit_add":      _habit_add,
        "habit_list":     lambda u, c, *_: cmd_habits(u, c),
        "habit_delete":   _habit_delete,
        "reminder":       _reminder_intent,
        "free_time":      _free_time_intent,
        "plan":           _plan_tasks_intent,
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

_DEADLINE_CLARIFICATION_RE = re.compile(
    r"^(?:deadline\s+is|due|by|it'?s?\s+due)\s+(.+)$",
    re.IGNORECASE,
)


async def _add_task(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    # Check if this looks like a deadline clarification for the last added task
    raw = parsed.get("raw", "")
    m   = _DEADLINE_CLARIFICATION_RE.match(raw.strip())
    last_task_id = context.user_data.get("last_added_task_id")

    if m and last_task_id:
        # User is clarifying the deadline of the previous task
        task = db.get_task(last_task_id)
        if task and not task["deadline"]:
            dt = parsed.get("datetime")
            if dt:
                deadline_str = dt.date().isoformat()
                db.update_task(last_task_id, deadline=deadline_str)
                await update.message.reply_text(
                    f"✏️ Updated deadline for *{task['title']}*: {_fmt_deadline(deadline_str)}",
                    parse_mode="Markdown",
                )
                context.user_data.pop("last_added_task_id", None)
                return

    # Multi-task add: "I have midterm Thursday and assignment due Friday"
    titles    = parsed.get("titles")
    deadlines = parsed.get("deadlines")
    priority  = parsed.get("priority", "medium") or "medium"
    duration  = parsed.get("duration", 60)
    category  = parsed.get("category", "general")
    energy    = parsed.get("energy", "medium")
    splittable = parsed.get("splittable", False)

    if titles and len(titles) > 1:
        added = []
        for i, t in enumerate(titles):
            dl  = deadlines[i] if deadlines and i < len(deadlines) else None
            tid = db.add_task(
                title=t, deadline=dl, priority=priority,
                estimated_minutes=duration, category=category,
                energy=energy, splittable=splittable,
            )
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
    task_id      = db.add_task(
        title=title,
        deadline=deadline_str,
        priority=priority,
        estimated_minutes=duration,
        category=category,
        energy=energy,
        splittable=splittable,
    )
    p_icon       = PRIORITY_ICON.get(priority, "🟡")

    # Store for potential deadline clarification follow-up
    if not deadline_str:
        context.user_data["last_added_task_id"] = task_id
    else:
        context.user_data.pop("last_added_task_id", None)

    reply  = f"✅ *Task added!*\n\n{p_icon} *{title}*\n"
    reply += f"Due: {_fmt_deadline(deadline_str)}\n" if deadline_str else "No deadline set — reply `deadline is [date]` to add one\n"
    reply += f"Estimate: {duration} min · {category} · {energy} energy\n"
    reply += f"Priority: {priority}\n"
    reply += f"ID: #{task_id}"
    reply += _entity_ref_line("task", task_id)
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
    duration = int(task["estimated_minutes"] or 60)

    # Try smart suggestions
    try:
        free_slots = smart_schedule.get_free_slots(days_ahead=5, min_duration_min=duration)
        msg, best  = smart_schedule.build_suggestion_message(task["title"], duration, free_slots)
        header     = f"⏰ *Schedule {idx+1}/{total}* — _{task['title']}_\n\n"
        context.user_data["schedule_suggested"] = best
        await update.message.reply_text(header + msg, parse_mode="Markdown")
    except Exception:
        context.user_data["schedule_suggested"] = None
        await update.message.reply_text(
            f"⏰ *Schedule {idx+1}/{total}*\n\n"
            f"Task: _{task['title']}_\n\n"
            f"Estimated duration: {duration} min\n\n"
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
            "(e.g. `Thursday 2pm to 4pm`)",
            parse_mode="Markdown",
        )
        return
    # Numbered slot pick
    elif re.match(r"^\d+$", text.strip()):
        try:
            task_duration = int(task["estimated_minutes"] or 60)
            free_slots = smart_schedule.get_free_slots(days_ahead=5, min_duration_min=task_duration)
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
    reminder  = nlp.compute_reminder_minutes(text, dt)

    # If no duration given, enter end-time state
    if not duration:
        context.user_data["scheduling_start_dt"] = dt
        context.user_data["scheduling_reminder"] = reminder
        context.user_data["state"] = "scheduling_end_time"
        await update.message.reply_text(
            f"⏰ _{task['title']}_ at {_fmt_dt(dt)}\n\n"
            "When does it finish? (e.g. `2pm`, `4:30 PM`, `in 2 hours`)",
            parse_mode="Markdown",
        )
        return

    end_dt = dt + timedelta(minutes=duration)

    # Confirmation step — ask before creating event
    reply_lines = [
        f"📅 *Confirm schedule:*\n",
        f"*{task['title']}*\n"
        f"📍 {_fmt_dt(dt)} → {end_dt.strftime('%I:%M %p')} ({duration} min)\n"
        f"⏰ Reminder: {reminder} min before",
    ]

    recurrence = nlp.extract_recurrence(text)
    if recurrence:
        reply_lines.append(f"🔄 {recurrence['summary']}")

    reply_lines.append("\nReply `yes` to confirm, `no` to skip.")

    context.user_data["confirm_schedule"] = {
        "type": "task",
        "task_id": task_id,
        "task_title": task["title"],
        "dt": dt,
        "duration": duration,
        "reminder": reminder,
        "end_dt": end_dt,
        "recurrence_rrule": recurrence["rrule"] if recurrence else None,
    }
    context.user_data["state"] = "confirm_schedule"

    await update.message.reply_text("\n".join(reply_lines), parse_mode="Markdown")


async def _scheduling_end_time(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """User already gave a start time, now we need the end time."""
    task_ids = context.user_data["pending_schedule"]
    task_id  = task_ids[context.user_data["schedule_idx"]]
    task     = db.get_task(task_id)
    dt       = context.user_data["scheduling_start_dt"]
    reminder = context.user_data.get("scheduling_reminder", 30)

    end_dt = nlp.extract_datetime(text, base_time=dt)
    if not end_dt:
        # Try parsing as a time-of-day (e.g. "2pm", "4:30 PM")
        import pytz
        tz = pytz.timezone(os.getenv("TIMEZONE", "Asia/Seoul"))
        try:
            parsed_time = datetime.strptime(text.strip(), "%I:%M %p")
            parsed_time = datetime.strptime(text.strip().lstrip("0"), "%I:%M %p")
        except ValueError:
            try:
                parsed_time = datetime.strptime(text.strip(), "%I %p")
            except ValueError:
                parsed_time = None
        if parsed_time:
            end_dt = dt.replace(hour=parsed_time.hour, minute=parsed_time.minute, second=0, microsecond=0)
            if end_dt <= dt:
                end_dt += timedelta(days=1)  # next day if end is before start
        else:
            # Try "in X hours/minutes"
            dur = nlp.extract_duration(text)
            if dur:
                end_dt = dt + timedelta(minutes=dur)

    if not end_dt or end_dt <= dt:
        await update.message.reply_text(
            "Couldn't parse that. Try a time like `2pm`, `4:30 PM`, `in 2 hours`, or /cancel.",
        )
        return

    duration = int((end_dt - dt).total_seconds() / 60)
    context.user_data.pop("scheduling_start_dt", None)
    context.user_data.pop("scheduling_reminder", None)

    reply_lines = [
        f"📅 *Confirm schedule:*\n",
        f"*{task['title']}*\n"
        f"📍 {_fmt_dt(dt)} → {end_dt.strftime('%I:%M %p')} ({duration} min)\n"
        f"⏰ Reminder: {reminder} min before",
    ]
    reply_lines.append("\nReply `yes` to confirm, `no` to skip.")

    context.user_data["confirm_schedule"] = {
        "type": "task",
        "task_id": task_id,
        "task_title": task["title"],
        "dt": dt,
        "duration": duration,
        "reminder": reminder,
        "end_dt": end_dt,
    }
    context.user_data["state"] = "confirm_schedule"
    await update.message.reply_text("\n".join(reply_lines), parse_mode="Markdown")



async def _confirm_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle confirmation/rejection of a scheduled event before creation."""
    data = context.user_data.get("confirm_schedule")
    if not data:
        context.user_data["state"] = "idle"
        await update.message.reply_text("⚠️ Nothing to confirm right now.")
        return

    raw = text.strip().lower()
    if raw in ("no", "n", "nope", "cancel", "skip"):
        context.user_data["state"] = "idle"
        context.user_data.pop("confirm_schedule", None)

        # If we were in batch scheduling (multi-task), move to next or finish
        task_ids = context.user_data.get("pending_schedule", [])
        if task_ids:
            idx = context.user_data.get("schedule_idx", 0)
            next_idx = idx + 1
            if next_idx < len(task_ids):
                context.user_data["schedule_idx"] = next_idx
                await _ask_schedule_time(update, context)
            else:
                context.user_data["state"] = "idle"
                context.user_data["pending_schedule"] = []
                await update.message.reply_text("Scheduling cancelled for this task.")
        else:
            await update.message.reply_text("❌ Cancelled.")
        return

    if raw not in ("yes", "y", "yep", "sure", "ok", "confirm", "schedule"):
        await update.message.reply_text("Reply `yes` to confirm or `no` to cancel.")
        return

    # Confirmed — create the event
    task_id = data.get("task_id")
    title = data.get("task_title", "Event")
    dt = data["dt"]
    duration = data["duration"]
    reminder = data["reminder"]
    end_dt = data["end_dt"]
    rrule = data.get("recurrence_rrule")

    try:
        event_id = calendar_client.create_event(
            title=title,
            start=dt,
            duration_minutes=duration,
            reminder_minutes=reminder,
            description=f"Scheduled via Planning Bot (task #{task_id})" if task_id else None,
            rrule=rrule,
        )

        if task_id:
            db.update_task(
                task_id,
                calendar_event_id=event_id,
                scheduled_start=dt.isoformat(),
                scheduled_end=end_dt.isoformat(),
                status="in_progress",
            )

        recur_str = ""
        if context.user_data.get("confirm_schedule", {}).get("recurrence_rrule"):
            # Re-extract summary for display
            rec = nlp.extract_recurrence(text)
            if rec:
                recur_str = f"\n🔁 {rec['summary'].capitalize()}"

        await update.message.reply_text(
            f"✅ *Scheduled!*\n\n"
            f"*{title}*\n"
            f"📍 {_fmt_dt(dt)} → {end_dt.strftime('%I:%M %p')} ({duration} min)"
            f"{recur_str}\n"
            f"⏰ Reminder: {reminder} min before",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Calendar error: {exc}")
        context.user_data["state"] = "idle"
        context.user_data.pop("confirm_schedule", None)
        return

    context.user_data["state"] = "idle"
    context.user_data.pop("confirm_schedule", None)

    # If we were in batch scheduling, move to the next task
    task_ids = context.user_data.get("pending_schedule", [])
    if task_ids:
        idx = context.user_data.get("schedule_idx", 0)
        next_idx = idx + 1
        if next_idx < len(task_ids):
            context.user_data["schedule_idx"] = next_idx
            await _ask_schedule_time(update, context)
        else:
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
        # Check if any slot lacks a specific time — ask for clarification
        had_times = getattr(multi_slots, "_had_times", None)
        if had_times and not all(had_times):
            # Find which slots lack times
            slot_names = {0: "first", 1: "second", 2: "third", 3: "fourth"}
            missing_info = []
            for i, (s, has_time) in enumerate(zip(multi_slots, had_times)):
                day_name = s.strftime("%A")
                if not has_time:
                    missing_info.append(f"  {slot_names.get(i, str(i+1))} ({day_name})")
            context.user_data["state"]                 = "schedule_direct"
            context.user_data["direct_title"]           = title
            context.user_data["direct_recurrence"]      = recurrence
            context.user_data["direct_reminder"]        = reminder
            context.user_data["direct_duration"]        = duration
            context.user_data["direct_multi_pending"]   = multi_slots
            context.user_data["direct_multi_had_times"] = had_times
            await update.message.reply_text(
                f"⏰ I found multiple slots, but some need a specific time:\n\n"
                + "\n".join(missing_info)
                + "\n\nReply with the missing time, e.g. `first at 7pm` or `9pm` for the first one.",
                parse_mode="Markdown",
            )
            return
        await _do_multi_slot_schedule(update, title, multi_slots, duration, reminder)
        return

    # Single slot with time — confirm before creating
    if dt:
        end_dt = dt + timedelta(minutes=duration)
        rrule = recurrence["rrule"] if recurrence else None
        recur_str = f"\n🔁 {recurrence['summary'].capitalize()}" if recurrence else ""
        context.user_data["confirm_schedule"] = {
            "type": "direct",
            "title": title,
            "dt": dt,
            "duration": duration,
            "reminder": reminder,
            "end_dt": end_dt,
            "recurrence_rrule": rrule,
            "recur_summary": recur_str,
        }
        context.user_data["state"] = "confirm_schedule"
        reply_lines = [
            f"📅 *Confirm schedule:*\n",
            f"*{title}*\n"
            f"📍 {_fmt_dt(dt)} → {end_dt.strftime('%I:%M %p')} ({duration} min)"
            f"{recur_str}\n"
            f"⏰ Reminder: {reminder} min before",
        ]
        reply_lines.append("\nReply `yes` to confirm, `no` to cancel.")
        await update.message.reply_text("\n".join(reply_lines), parse_mode="Markdown")
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
    reminder   = nlp.compute_reminder_minutes(text, nlp.extract_datetime(text)) or context.user_data.get("direct_reminder", 30)
    duration   = nlp.extract_duration(text) or context.user_data.get("direct_duration", 60)
    suggested  = context.user_data.get("direct_suggested")

    # User confirmed the suggested slot
    if suggested and text.lower().strip() in ("yes", "y", "yep", "sure", "ok", "confirm"):
        dt = suggested["start"]
        end_dt = dt + timedelta(minutes=duration)
        rrule = recurrence["rrule"] if recurrence else None
        recur_str = f"\n🔁 {recurrence['summary'].capitalize()}" if recurrence else ""
        context.user_data["confirm_schedule"] = {
            "type": "direct",
            "title": title,
            "dt": dt,
            "duration": duration,
            "reminder": reminder,
            "end_dt": end_dt,
            "recurrence_rrule": rrule,
            "recur_summary": recur_str,
        }
        ctx_text = f"{title} {_fmt_dt(dt)}"
        context.user_data["state"] = "confirm_schedule"
        reply_lines = [
            f"📅 *Confirm schedule:*\n",
            f"*{title}*\n"
            f"📍 {_fmt_dt(dt)} → {end_dt.strftime('%I:%M %p')} ({duration} min)"
            f"{recur_str}\n"
            f"⏰ Reminder: {reminder} min before",
        ]
        reply_lines.append("\nReply `yes` to confirm, `no` to cancel.")
        await update.message.reply_text("\n".join(reply_lines), parse_mode="Markdown")
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
                dt = free_slots[n]["start"]
                end_dt = dt + timedelta(minutes=duration)
                rrule = recurrence["rrule"] if recurrence else None
                recur_str = f"\n🔁 {recurrence['summary'].capitalize()}" if recurrence else ""
                context.user_data["confirm_schedule"] = {
                    "type": "direct",
                    "title": title,
                    "dt": dt,
                    "duration": duration,
                    "reminder": reminder,
                    "end_dt": end_dt,
                    "recurrence_rrule": rrule,
                    "recur_summary": recur_str,
                }
                context.user_data["state"] = "confirm_schedule"
                reply_lines = [
                    f"📅 *Confirm schedule:*\n",
                    f"*{title}*\n"
                    f"📍 {_fmt_dt(dt)} → {end_dt.strftime('%I:%M %p')} ({duration} min)"
                    f"{recur_str}\n"
                    f"⏰ Reminder: {reminder} min before",
                ]
                reply_lines.append("\nReply `yes` to confirm, `no` to cancel.")
                await update.message.reply_text("\n".join(reply_lines), parse_mode="Markdown")
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

    end_dt = dt + timedelta(minutes=duration)
    rrule = recurrence["rrule"] if recurrence else None
    recur_str = f"\n🔁 {recurrence['summary'].capitalize()}" if recurrence else ""
    context.user_data["confirm_schedule"] = {
        "type": "direct",
        "title": title,
        "dt": dt,
        "duration": duration,
        "reminder": reminder,
        "end_dt": end_dt,
        "recurrence_rrule": rrule,
        "recur_summary": recur_str,
    }
    context.user_data["state"] = "confirm_schedule"
    reply_lines = [
        f"📅 *Confirm schedule:*\n",
        f"*{title}*\n"
        f"📍 {_fmt_dt(dt)} → {end_dt.strftime('%I:%M %p')} ({duration} min)"
        f"{recur_str}\n"
        f"⏰ Reminder: {reminder} min before",
    ]
    reply_lines.append("\nReply `yes` to confirm, `no` to cancel.")
    await update.message.reply_text("\n".join(reply_lines), parse_mode="Markdown")


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
    """Directly create a calendar event (no confirmation) — used by _do_multi_slot_schedule only."""
    end_dt = dt + timedelta(minutes=duration)
    try:
        rrule = recurrence["rrule"] if recurrence else None
        calendar_client.create_event(
            title=title,
            start=dt,
            duration_minutes=duration,
            reminder_minutes=reminder,
            rrule=rrule,
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


# ── BATCH SCHEDULE (schedule multiple tasks at multiple slots in one message) ──

_BATCH_SCHEDULE_RE = re.compile(
    r"(?:schedule|plan|block|add)\s+"
    r"(?:task\s+)?(\d+)\s+"
    r"(?:on\s+|at\s+)?"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun|tomorrow|today)"
    r"(?:\s+(?:at\s+|from\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?"
    r"(?:\s+(?:for|and)\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?))?"
    r"(?:\s+(?:and|,)\s+task\s+(\d+)\s+"
    r"(?:on\s+|at\s+)?"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun|tomorrow|today)"
    r"(?:\s+(?:at\s+|from\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?"
    r"(?:\s+(?:for|to|and)\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?))?"
    r")?",
    re.IGNORECASE,
)


async def _batch_schedule_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    """
    Handle "schedule task 1 on monday at 3pm for 1.5 hours and task 2 on thursday from 9 am to 11am"
    Parses tasks from the last task list and creates calendar events for each.
    """
    raw = parsed.get("raw", "").lower()
    last_list = context.user_data.get("last_task_list", [])

    if not last_list:
        # Fetch unscheduled tasks if no list cached
        tasks = db.get_unscheduled_tasks_sorted()
        if not tasks:
            await update.message.reply_text("No unscheduled tasks to schedule.")
            return
        context.user_data["last_task_list"] = [dict(t) for t in tasks]
        last_list = context.user_data["last_task_list"]

    # Parse multi-task, multi-slot format
    # Pattern: "schedule task X on DAY at TIME for DURATION and task Y on DAY at TIME for DURATION"
    # First extract task numbers and their corresponding slots
    task_assignments = []  # list of (task_number, day_name, time_str, duration_minutes)

    # Find all "task <num>" patterns and their associated slot info
    segments = re.split(r"\b(?:and|,)\s*(?=task\s+\d)", raw, flags=re.IGNORECASE)
    for seg in segments:
        m = re.match(
            r"\s*(?:schedule|plan|block|add)?\s*(?:task\s+)?(\d+)\s*"
            r"(?:on\s+|at\s+)?"
            r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun|tomorrow|today)"
            r"(?:\s+(?:at\s+|from\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?"
            r"(?:\s+(?:for|and|to)\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?))?"
            r"(?:\s+(?:and|to)\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?))?",
            seg.strip(), re.IGNORECASE
        )
        if m:
            task_num = int(m.group(1))
            day_name = m.group(2)
            time_str = m.group(3)
            dur1_val = m.group(4)
            dur1_unit = m.group(5)
            dur2_val = m.group(6)
            dur2_unit = m.group(7)

            # Calculate duration
            if dur2_val:
                duration = float(dur2_val)
                duration = int(duration * 60) if dur2_unit and ("hour" in dur2_unit or "hr" in dur2_unit) else int(duration)
            elif dur1_val:
                duration = float(dur1_val)
                duration = int(duration * 60) if dur1_unit and ("hour" in dur1_unit or "hr" in dur1_unit) else int(duration)
            else:
                duration = 60

            task_assignments.append((task_num, day_name, time_str, duration))

    if not task_assignments:
        # Fallback: try simpler parsing
        await update.message.reply_text(
            "Couldn't parse that. Try:\n"
            "`schedule task 1 on monday at 3pm for 1.5 hours and task 2 on thursday 9am for 2 hours`"
        )
        return

    scheduled = []
    errors = []

    for task_num, day_name, time_str, duration in task_assignments:
        if task_num < 1 or task_num > len(last_list):
            errors.append(f"Task #{task_num} not found in list.")
            continue

        task = db.get_task(last_list[task_num - 1]["id"])
        if not task:
            errors.append(f"Task #{task_num} no longer exists.")
            continue

        # Build a datetime string from the day and time
        time_query = f"{day_name} {time_str}".strip() if time_str else day_name
        dt = nlp.extract_datetime(time_query)
        if not dt:
            errors.append(f"Couldn't parse time for task #{task_num}: '{time_query}'")
            continue

        end_dt = dt + timedelta(minutes=duration)

        try:
            event_id = calendar_client.create_event(
                title=task["title"],
                start=dt,
                duration_minutes=duration,
                reminder_minutes=30,
                description=f"Scheduled via Planning Bot (task #{task['id']})",
            )
            db.update_task(
                task["id"],
                calendar_event_id=event_id,
                scheduled_start=dt.isoformat(),
                scheduled_end=end_dt.isoformat(),
                status="in_progress",
            )
            scheduled.append(f"• *{task['title']}* — {_fmt_dt(dt)} → {end_dt.strftime('%I:%M %p')}")
        except Exception as exc:
            errors.append(f"• {task['title']}: {exc}")

    if scheduled:
        msg = "✅ *Tasks scheduled!*\n\n" + "\n".join(scheduled)
        if errors:
            msg += "\n\n⚠️ *Errors:*\n" + "\n".join(errors)
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "Couldn't schedule those tasks:\n" + "\n".join(errors)
        )


# ── FREE TIME / TASK PLANNING ─────────────────────────────────────────────────

async def _free_time_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    duration = int(parsed.get("duration") or 60)
    raw      = parsed.get("raw", "").lower()
    days     = 2 if "tomorrow" in raw else 1 if "today" in raw else 7 if "week" in raw else 5

    try:
        slots = smart_schedule.get_free_slots(days_ahead=days, min_duration_min=duration)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Couldn't read calendar: {exc}")
        return

    if "tomorrow" in raw:
        import pytz
        tz = pytz.timezone(os.getenv("TIMEZONE", "Asia/Seoul"))
        tomorrow = datetime.now(tz).date() + timedelta(days=1)
        slots = [s for s in slots if s["start"].date() == tomorrow]

    if not slots:
        await update.message.reply_text(f"I couldn't find a free {duration}-minute block in that window.")
        return

    await update.message.reply_text(
        f"🕰 *Free {duration}+ min blocks*\n\n"
        f"{smart_schedule.format_slots_for_display(slots, limit=8)}\n\n"
        "_Try `plan my unscheduled tasks` if you want me to match tasks to these._",
        parse_mode="Markdown",
    )


async def _plan_tasks_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    tasks = [dict(t) for t in db.get_plannable_tasks(limit=12)]
    if not tasks:
        await update.message.reply_text("No unscheduled tasks to plan right now.")
        return

    min_duration = min(max(int(t.get("estimated_minutes") or 60), 30) for t in tasks)
    try:
        slots = smart_schedule.get_free_slots(days_ahead=7, min_duration_min=min_duration)
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Couldn't read calendar: {exc}")
        return

    plan = smart_schedule.build_task_plan(tasks, slots, limit=6)
    if not plan:
        await update.message.reply_text(
            "I found unscheduled tasks, but no free blocks long enough this week.\n"
            "Try adding shorter estimates or asking for free time first."
        )
        return

    context.user_data["state"] = "plan_confirm"
    context.user_data["pending_plan"] = [
        {
            "task_id": item["task"]["id"],
            "title": item["task"]["title"],
            "start": item["start"].isoformat(),
            "end": item["end"].isoformat(),
            "duration": int((item["end"] - item["start"]).total_seconds() / 60),
        }
        for item in plan
    ]

    await update.message.reply_text(
        "🧠 *Suggested task plan*\n\n"
        f"{smart_schedule.format_task_plan(plan)}\n\n"
        "_Reply `yes` to schedule all, `schedule 1 3` for selected items, or `no` to leave them unscheduled._",
        parse_mode="Markdown",
    )


async def _plan_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    plan = context.user_data.get("pending_plan", [])
    raw = text.strip().lower()

    if raw in ("no", "n", "cancel", "stop"):
        context.user_data["state"] = "idle"
        context.user_data.pop("pending_plan", None)
        await update.message.reply_text("No problem. I left those tasks unscheduled.")
        return

    if raw in ("yes", "y", "ok", "confirm", "schedule all"):
        indexes = list(range(len(plan)))
    else:
        indexes = [int(n) - 1 for n in re.findall(r"\d+", raw)]

    indexes = [i for i in indexes if 0 <= i < len(plan)]
    if not indexes:
        await update.message.reply_text(
            "Reply `yes` to schedule all, `schedule 1 3` for selected items, or `no` to skip."
        )
        return

    scheduled = []
    errors = []
    for i in indexes:
        item = plan[i]
        task = db.get_task(item["task_id"])
        if not task:
            errors.append(f"{i+1}. Task no longer exists")
            continue

        start = datetime.fromisoformat(item["start"])
        end = datetime.fromisoformat(item["end"])
        duration = item["duration"]

        try:
            event_id = calendar_client.create_event(
                title=task["title"],
                start=start,
                duration_minutes=duration,
                reminder_minutes=30,
                description=f"Scheduled via Planning Bot (task #{task['id']})",
            )
            db.update_task(
                task["id"],
                calendar_event_id=event_id,
                scheduled_start=start.isoformat(),
                scheduled_end=end.isoformat(),
                status="in_progress",
            )
            scheduled.append(f"{i+1}. {task['title']} — {_fmt_dt(start)} → {end.strftime('%I:%M %p')}")
        except Exception as exc:
            errors.append(f"{i+1}. {task['title']}: {exc}")

    context.user_data["state"] = "idle"
    context.user_data.pop("pending_plan", None)

    if scheduled:
        msg = "✅ *Scheduled from plan:*\n\n" + "\n".join(scheduled)
        if errors:
            msg += "\n\n⚠️ *Failed:*\n" + "\n".join(errors)
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("I couldn't schedule those items:\n" + "\n".join(errors))


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
            completed.append(task)

    if completed:
        text_lines = ["✅ *Done!*"]
        reply_markup = None
        for task in completed:
            text_lines.append(f"• *{task['title']}*")
            if task["deadline"]:
                text_lines[-1] += f" (due {_fmt_deadline(task['deadline'])})"
        if len(completed) == 1:
            # Single task — add undo button
            text_lines.append("\n↩️ Undo within 30 seconds:")
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"↩️ Undo: {completed[0]['title'][:25]}", callback_data=f"undo_task_{completed[0]['id']}")]
            ])
        await update.message.reply_text("\n".join(text_lines), parse_mode="Markdown", reply_markup=reply_markup)
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
            title = e.get('summary', '?')
            lines.append(f"  {i+1}. *{title}* — {calendar_client.fmt_event_time_range(e)}")
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

        db.update_task_schedule_by_event(event["id"], new_dt.isoformat(), end.isoformat())

        title = event.get("summary", "Event")
        await update.message.reply_text(
            f"✅ *Rescheduled!*\n\n"
            f"*{title}*\n"
            f"New time: {_fmt_dt(new_dt)} → {end.strftime('%I:%M %p')}",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Reschedule failed: {exc}")


# ── REMINDER ──────────────────────────────────────────────────────────────────

async def _reminder_intent(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    """
    Create a lightweight reminder stored in SQLite and sent by the bot.
    It does not block Google Calendar time.
    """
    title      = (parsed.get("title") or "").strip()
    dt         = parsed.get("datetime")
    recurrence = parsed.get("recurrence")

    if not title or len(title) < 2:
        await update.message.reply_text(
            "What should I remind you about?\n"
            "E.g. `remind me tomorrow 4pm to check results`"
        )
        return

    if not dt:
        # Ask for time
        context.user_data["state"]            = "reminder_time"
        context.user_data["pending_reminder"] = {"title": title, "recurrence": recurrence}
        await update.message.reply_text(
            f"⏰ Reminder: *{title}*\n\nWhen? (e.g. `tomorrow 4pm`, `Monday 9am`, `in 2 hours`)",
            parse_mode="Markdown",
        )
        return

    await _do_create_reminder(update, title, dt, recurrence)


async def _do_create_reminder(update, title: str, dt: datetime, recurrence=None):
    try:
        rrule    = recurrence["rrule"] if recurrence else None
        reminder_id = db.add_reminder(title=title, remind_at=dt.isoformat(), recurrence_rrule=rrule)

        time_str  = dt.strftime("%a %b %d, %I:%M %p").lstrip("0")
        recur_str = f"\n🔁 {recurrence['summary'].capitalize()}" if recurrence else ""
        ref_line = _entity_ref_line("reminder", reminder_id)
        await update.message.reply_text(
            f"🔔 *Reminder set!*\n\n"
            f"*{title}*\n"
            f"{time_str}"
            f"{recur_str}\n"
            f"ID: #{reminder_id}"
            f"{ref_line}",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Couldn't set reminder: {exc}")


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


# ── Callback query handler (for inline "Done" button) ─────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses (e.g. 'Done' button on reminders)."""
    if not _authorized(update):
        return

    query = update.callback_query
    await query.answer()  # Acknowledge the button press

    data = query.data
    if data.startswith("done_task_"):
        task_id = int(data.replace("done_task_", ""))
        task = db.get_task(task_id)
        if not task:
            await query.edit_message_text(
                text=f"⚠️ Task #{task_id} no longer exists.",
                parse_mode="Markdown",
            )
            return

        db.complete_task(task_id)
        title = task["title"]
        await query.edit_message_text(
            text=f"✅ *{title}* marked as done!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("↩️ Undo", callback_data=f"undo_task_{task_id}")]
            ]),
        )
        return

    if data.startswith("undo_task_"):
        task_id = int(data.replace("undo_task_", ""))
        task = db.get_task(task_id)
        if not task:
            await query.edit_message_text(
                text=f"⚠️ Task #{task_id} no longer exists.",
                parse_mode="Markdown",
            )
            return

        db.uncomplete_task(task_id)
        title = task["title"]
        await query.edit_message_text(
            text=f"↩️ *{title}* reverted to pending!",
            parse_mode="Markdown",
        )
        return

    if data.startswith("plan_task_"):
        task_id = int(data.replace("plan_task_", ""))
        task = db.get_task(task_id)
        if not task:
            await query.edit_message_text(
                text=f"⚠️ Task #{task_id} no longer exists.",
                parse_mode="Markdown",
            )
            return

        # Enter scheduling flow for this single task
        context.user_data["state"]            = "scheduling"
        context.user_data["pending_schedule"] = [task_id]
        context.user_data["schedule_idx"]     = 0
        context.user_data["last_task_list"]   = [
            {"id": task["id"], "title": task["title"], "status": task.get("status", "pending"),
             "priority": task.get("priority", "medium"), "deadline": task.get("deadline"),
             "estimated_minutes": task.get("estimated_minutes", 60)}
        ]

        await query.edit_message_text(
            text=f"📅 Starting scheduling for *{task['title']}*...",
            parse_mode="Markdown",
        )
        await _ask_schedule_time(update, context)
        return

    # Unknown callback
    await query.edit_message_text(
        text="❓ Unknown action.",
        parse_mode="Markdown",
    )


# ── Entry point ────────────────────────────────────────────────────────────────

async def post_init(app):
    scheduler = setup_scheduler(app)
    scheduler.start()
    logger.info("✅ Scheduler started")


def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN is not set.")

    db.init_db()

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
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("week",     cmd_week))
    app.add_handler(CommandHandler("habits",   cmd_habits))
    app.add_handler(CommandHandler("free",     cmd_free))
    app.add_handler(CommandHandler("plan",     cmd_plan))
    app.add_handler(CommandHandler("now",      cmd_now))
    app.add_handler(CommandHandler("cancel",   cmd_cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
