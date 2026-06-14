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
Telegram bot entry point — command handlers, state machine, and message router
for the Planning Bot.

This module is the application's main entry point. It builds a
``telegram.ext.Application`` with long polling, registers command handlers
(``/start``, ``/tasks``, ``/today``, ``/tomorrow``, ``/week``, ``/habits``,
``/free``, ``/plan``, ``/now``, ``/cancel``, ``/help``), and a catch-all
message handler that routes messages through a hybrid parsing pipeline
(LLM primary, regex NLP fallback).

State machine
=============
The bot maintains ``context.user_data['state']`` with 12 conversation states:
``idle``, ``scheduling``, ``schedule_direct``, ``deleting``, ``updating``,
``reminder_time``, ``plan_confirm``, ``reschedule_pick``, ``reschedule_time``,
``clarifying``, ``replying_delete``, ``replying_update``.

Key features
============
- Natural language task creation and scheduling
- Inline keyboard callbacks (Done, Undo, Plan)
- Reply-to-edit: users can reply to any bot message containing an entity
  reference (``📎 t#42``) to edit or delete that entity inline
- Hybrid parsing: LLM (Gemini via OpenRouter) primary, regex NLP fallback
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

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
    """Check whether the user is authorised to interact with this bot.

    If ``ALLOWED_USER_ID`` is 0 (the default), all users are allowed.
    Otherwise, only the user with the matching Telegram user ID is permitted.

    Args:
        update: The incoming Telegram update.

    Returns:
        bool: ``True`` if the user is authorised.
    """
    if ALLOWED_USER_ID == 0:
        return True
    return update.effective_user.id == ALLOWED_USER_ID


# ── Formatting ─────────────────────────────────────────────────────────────────

# Emoji icons mapped to task status values
STATUS_ICON = {"pending": "⏳", "in_progress": "🔄", "done": "✅"}


def _fmt_deadline(d):
    """Format an ISO date string into a human-readable short form.

    Args:
        d: An ISO date string (e.g. ``"2026-06-19"``).

    Returns:
        str: Formatted date like ``"Fri Jun 19"``, or the original string if
            parsing fails.
    """
    try:
        return datetime.fromisoformat(d).strftime("%a %b %d")
    except Exception:
        return d


def _fmt_dt(dt):
    """Format a timezone-aware datetime for display in Telegram messages.

    Args:
        dt: A datetime object.

    Returns:
        str: Formatted string like ``"Thu Jun 11, 2:00 PM"`` with leading
            zeros stripped from the hour.
    """
    return dt.strftime("%a %b %d, %I:%M %p").lstrip("0")


PRIORITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def _fmt_task_row(idx, task):
    """Format a single task as a one-line summary string for a task list.

    Includes status icon, priority icon, title, deadline indicator, and a
    calendar-scheduled tag if applicable.

    Args:
        idx: 1-based index in the list.
        task: A task dict (from ``sqlite3.Row`` or dict).

    Returns:
        str: Formatted task line.
    """
    icon     = STATUS_ICON.get(task["status"], "⏳")
    p_icon   = PRIORITY_ICON.get(task["priority"] or "medium", "🟡")
    cal_tag  = " 📅" if task["calendar_event_id"] else ""
    due_tag  = f" — due {_fmt_deadline(task['deadline'])}" if task["deadline"] else ""
    return f"{idx}. {icon}{p_icon} {task['title']}{due_tag}{cal_tag}"


def _fmt_task_list(tasks):
    """Format a list of tasks into a multi-line Telegram message.

    Args:
        tasks: A list of task dicts.

    Returns:
        str: Newline-separated task lines, or ``"No tasks found."``.
    """
    if not tasks:
        return "No tasks found."
    return "\n".join(_fmt_task_row(i + 1, t) for i, t in enumerate(tasks))


# ── Entity references (for reply-to-edit) ──────────────────────────────────────

# Regex to extract entity references from bot messages.
# Format: 📎 t#42 (task), 📎 r#7 (reminder), 📎 e#abc123 (calendar event)
_ENTITY_REF_RE = re.compile(r"📎\s*(t|r|e)#(\S+)", re.IGNORECASE)


def _parse_entity_ref(text: str):
    """Extract an entity reference from a bot message.

    The reference format is ``📎 t#<id>`` for tasks, ``📎 r#<id>`` for
    reminders, and ``📎 e#<id>`` for calendar events.

    Args:
        text: The bot message text.

    Returns:
        tuple or None: ``(entity_type, entity_id)`` where ``entity_type`` is
            ``"task"``, ``"reminder"``, or ``"cal_event"``, or ``None`` if no
            reference is found.
    """
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
    """Build a reference line to append to a bot message for reply-to-edit.

    Args:
        entity_type: ``"task"``, ``"reminder"``, or ``"cal_event"``.
        entity_id: The entity's ID.

    Returns:
        str: A reference line like ``"\\n📎 t#42"``.
    """
    prefix_map = {"task": "t", "reminder": "r", "cal_event": "e"}
    p = prefix_map.get(entity_type, "?")
    return f"\n📎 {p}#{entity_id}"


def _fmt_entity(entity_type: str, entity) -> str:
    """Format an entity's current state for an edit-confirmation message.

    Converts ``sqlite3.Row`` to dict internally for safe access.

    Args:
        entity_type: ``"task"`` or ``"reminder"``.
        entity: A row object or dict.

    Returns:
        str: Formatted entity details with emoji labels.
    """
    if entity is not None:
        entity = dict(entity)
    else:
        entity = {}
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
    """Try to interpret a reply as an edit/delete to the referenced entity.

    When a user replies to a bot message that contains an entity reference
    (``📎 t#42``), this handler processes the reply text as an edit or delete
    command.

    Supported actions:
        - Reply with ``"delete"`` → enters deletion confirmation flow.
        - Reply with ``"update"`` → enters interactive update flow.
        - Reply with a datetime → updates time (for tasks: deadline;
          for reminders/events: start time).
        - Reply with ``"title: New Title"`` → updates title.
        - Reply with ``"time: <datetime>"`` → updates time.
        - Reply with plain text (no keywords) → updates title (if long enough
          and not a command keyword).

    Args:
        update: The incoming Telegram update.
        context: The callback context (holds ``user_data`` state).

    Returns:
        bool: ``True`` if the reply was handled as an edit, ``False`` to fall
            through to normal message routing.
    """
    replied = update.message.reply_to_message
    text = update.message.text.strip()

    ref = _parse_entity_ref(replied.text or "")
    if not ref:
        return False

    entity_type, entity_id = ref
    text_lower = text.lower()

    # "delete" keyword → confirmation flow
    if text_lower in ("delete", "remove", "del", "rm", "🗑"):
        context.user_data["reply_delete"] = {
            "entity_type": entity_type,
            "entity_id": entity_id,
        }
        context.user_data["state"] = "replying_delete"
        type_emoji = {"task": "📝", "reminder": "🔔", "cal_event": "📅"}
        emoji = type_emoji.get(entity_type, "❓")
        title = _get_entity_title(entity_type, entity_id) or "this item"
        await update.message.reply_text(
            f"{emoji} Delete *{title}*?\n\nReply `yes` to confirm, `no` to cancel.",
            parse_mode="Markdown",
        )
        return True

    # "update" keyword → enter interactive update flow
    if text_lower in ("update", "edit", "change", "modify", "✏️"):
        context.user_data["reply_update"] = {
            "entity_type": entity_type,
            "entity_id": entity_id,
        }
        context.user_data["state"] = "replying_update"
        title = _get_entity_title(entity_type, entity_id) or "this item"
        await update.message.reply_text(
            f"✏️ Updating *{title}*\n\n"
            "What to change? Reply with:\n"
            "• `time: tomorrow 4pm` — new time\n"
            "• `title: New Name` — new title\n"
            "• A plain time like `tomorrow 6pm` — new time\n"
            "• A plain title — new title",
            parse_mode="Markdown",
        )
        return True

    new_dt = nlp.extract_datetime(text)
    title_prefix = re.match(r"^title:\s*(.+)$", text, re.IGNORECASE)
    time_prefix  = re.match(r"^time:\s*(.+)$", text, re.IGNORECASE)

    if entity_type == "reminder":
        reminder = db.get_reminder(int(entity_id))
        if not reminder:
            await update.message.reply_text("⚠️ That reminder no longer exists.")
            return True
        if time_prefix:
            dt = nlp.extract_datetime(time_prefix.group(1))
            if not dt:
                await update.message.reply_text("Couldn't parse that time. Try `time: tomorrow 4pm`.")
                return True
            db.update_reminder(reminder["id"], remind_at=dt.isoformat())
            reminder = db.get_reminder(reminder["id"])
            await update.message.reply_text(
                f"✅ *Reminder Updated*\n\n{_fmt_entity('reminder', reminder)}"
                f"{_entity_ref_line('reminder', reminder['id'])}",
                parse_mode="Markdown",
            )
            return True
        if title_prefix:
            new_title = title_prefix.group(1).strip().capitalize()
            if len(new_title) < 2:
                await update.message.reply_text("Title too short.")
                return True
            db.update_reminder(reminder["id"], title=new_title)
            reminder = db.get_reminder(reminder["id"])
            await update.message.reply_text(
                f"✅ *Reminder Updated*\n\n{_fmt_entity('reminder', reminder)}"
                f"{_entity_ref_line('reminder', reminder['id'])}",
                parse_mode="Markdown",
            )
            return True
        if new_dt:
            db.update_reminder(reminder["id"], remind_at=new_dt.isoformat())
            reminder = db.get_reminder(reminder["id"])
            await update.message.reply_text(
                f"✅ *Reminder Updated*\n\n{_fmt_entity('reminder', reminder)}"
                f"{_entity_ref_line('reminder', reminder['id'])}",
                parse_mode="Markdown",
            )
            return True
        if len(text) > 2 and not _is_command_keyword(text):
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
        if time_prefix:
            dt = nlp.extract_datetime(time_prefix.group(1))
            if dt:
                updates["deadline"] = dt.date().isoformat()
            else:
                await update.message.reply_text("Couldn't parse that time. Try `time: next Friday`.")
                return True
        if title_prefix:
            new_title = title_prefix.group(1).strip().capitalize()
            if len(new_title) >= 2:
                updates["title"] = new_title
        if not title_prefix and not time_prefix and new_dt:
            updates["deadline"] = new_dt.date().isoformat()
        if not updates and len(text) > 2 and not _is_command_keyword(text):
            updates["title"] = text
        if updates:
            db.update_task(task["id"], **updates)
            if "title" in updates and task["calendar_event_id"]:
                try:
                    calendar_client.update_event(task["calendar_event_id"], title=updates["title"])
                except Exception:
                    pass
            task = db.get_task(task["id"])
            await update.message.reply_text(
                f"✏️ *Task Updated*\n\n{_fmt_entity('task', task)}"
                f"{_entity_ref_line('task', task['id'])}",
                parse_mode="Markdown",
            )
            return True
        return False

    elif entity_type == "cal_event":
        if time_prefix:
            dt = nlp.extract_datetime(time_prefix.group(1))
            if not dt:
                await update.message.reply_text("Couldn't parse that time. Try `time: tomorrow 6pm`.")
                return True
            try:
                dur = calendar_client.reschedule_event(entity_id, dt)
                end = dt + timedelta(minutes=dur)
                db.update_task_schedule_by_event(entity_id, dt.isoformat(), end.isoformat())
                await update.message.reply_text(
                    f"✅ *Calendar Event Updated*\n\n"
                    f"New time: {_fmt_dt(dt)} → {end.strftime('%I:%M %p')}"
                    f"{_entity_ref_line('cal_event', entity_id)}",
                    parse_mode="Markdown",
                )
                return True
            except Exception as exc:
                await update.message.reply_text(f"⚠️ Couldn't update calendar event: {exc}")
                return True
        if not new_dt:
            return False
        try:
            dur = calendar_client.reschedule_event(entity_id, new_dt)
            end = new_dt + timedelta(minutes=dur)
            db.update_task_schedule_by_event(entity_id, new_dt.isoformat(), end.isoformat())
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


# Known command words that should never be treated as valid entity titles.
_COMMAND_KEYWORDS = frozenset({
    "yes", "no", "ok", "cancel", "done", "y", "n",
    "delete", "remove", "del", "rm", "🗑",
    "update", "edit", "change", "modify", "✏️",
    "stop", "nevermind", "skip",
    "title", "time", "rename",
})


def _is_command_keyword(text: str) -> bool:
    """Check if *text* looks like a command keyword rather than a real title.

    Args:
        text: The text to check.

    Returns:
        bool: ``True`` if the text is a recognised command keyword.
    """
    return text.lower().strip() in _COMMAND_KEYWORDS


def _get_entity_title(entity_type: str, entity_id) -> Optional[str]:
    """Get a human-readable title for an entity by its type and ID.

    Returns ``None`` if the title looks like a corrupted command keyword.

    Args:
        entity_type: ``"task"``, ``"reminder"``, or ``"cal_event"``.
        entity_id: The entity's ID.

    Returns:
        Optional[str]: The entity's title, or ``None``.
    """
    try:
        if entity_type == "task":
            t = db.get_task(int(entity_id))
            title = t["title"] if t else None
        elif entity_type == "reminder":
            r = db.get_reminder(int(entity_id))
            title = r["title"] if r else None
        elif entity_type == "cal_event":
            return "calendar event"
        else:
            return None
        if title and _is_command_keyword(title):
            return None
        return title
    except Exception:
        return None


# ── Commands ───────────────────────────────────────────────────────────────────


async def cmd_start(update, context):
    """Handle the ``/start`` command — send a welcome message.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
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


async def cmd_help(update, context):
    """Handle the ``/help`` command — show detailed usage reference.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
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


async def cmd_tasks(update, context):
    """Handle ``/tasks`` — list unscheduled pending tasks.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
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


async def cmd_today(update, context):
    """Handle ``/today`` — show tasks due today, calendar events, reminders.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
    if not _authorized(update):
        return
    import pytz
    tz    = pytz.timezone(os.getenv("TIMEZONE", "Asia/Seoul"))
    today = datetime.now(tz).date()
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
    reminders = db.get_reminders_by_period(today, today)
    if reminders:
        lines.append("\n⏰ *Reminders:*")
        for r in reminders:
            try:
                dt = datetime.fromisoformat(r["remind_at"])
                time_str = dt.strftime("%I:%M %p").lstrip("0")
            except Exception:
                time_str = r["remind_at"]
            recur_tag = " 🔁" if r["recurrence_rrule"] else ""
            lines.append(f"  • 🔔 {r['title']} — {time_str}{recur_tag}")
    if len(lines) == 1:
        lines.append("Nothing on the plate today! 🎉")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_tomorrow(update, context):
    """Handle ``/tomorrow`` — show tasks due tomorrow and calendar events.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
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
    reminders = db.get_reminders_by_period(tomorrow, tomorrow)
    if reminders:
        lines.append("\n⏰ *Reminders:*")
        for r in reminders:
            try:
                dt = datetime.fromisoformat(r["remind_at"])
                time_str = dt.strftime("%I:%M %p").lstrip("0")
            except Exception:
                time_str = r["remind_at"]
            recur_tag = " 🔁" if r["recurrence_rrule"] else ""
            lines.append(f"  • 🔔 {r['title']} — {time_str}{recur_tag}")
    if len(lines) == 1:
        lines.append("Nothing on the plate tomorrow! 🎉")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_week(update, context):
    """Handle ``/week`` — full week view with tasks and events per day.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
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


async def cmd_reminders(update, context):
    """Handle ``/reminders`` — list active bot-side reminders.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
    if not _authorized(update):
        return
    reminders = db.get_active_reminders()
    if not reminders:
        await update.message.reply_text("⏰ No active reminders.")
        return
    lines = [f"⏰ *Active Reminders* ({len(reminders)})\n"]
    for i, r in enumerate(reminders):
        try:
            dt = datetime.fromisoformat(r["remind_at"])
            time_str = _fmt_dt(dt)
        except Exception:
            time_str = r["remind_at"]
        recur_tag = " 🔁" if r["recurrence_rrule"] else ""
        lines.append(f"  {i+1}. 🔔 {r['title']} — {time_str}{recur_tag} (#{r['id']})")
    lines.append("\n_Reply to a reminder message to edit/delete it_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_habits(update, context):
    """Handle ``/habits`` — list active daily and weekly habits.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
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


async def cmd_free(update, context):
    """Handle ``/free`` — show free calendar blocks.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
    if not _authorized(update):
        return
    text = " ".join(context.args) if context.args else "find me 60 minutes this week"
    parsed = nlp.parse_message(text)
    parsed["intent"] = "free_time"
    await _free_time_intent(update, context, parsed)


async def cmd_plan(update, context):
    """Handle ``/plan`` — auto-suggest task placements.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
    if not _authorized(update):
        return
    await _plan_tasks_intent(update, context, {"intent": "plan", "raw": "plan my tasks"})


async def cmd_now(update, context):
    """Handle ``/now`` — show bot's current date, time, and timezone.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
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


async def cmd_cancel(update, context):
    """Handle ``/cancel`` — cancel current operation and clear state.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
    if not _authorized(update):
        return
    from telegram import ReplyKeyboardRemove
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())


# ── Main message router ────────────────────────────────────────────────────────


async def handle_message(update, context):
    """Main message router — routes all incoming text messages.

    Routing order:
        1. Check conversation state — dispatch to state-specific handlers.
        2. Check reply-to-edit context (user replying to a bot message).
        3. Check for ``"resume"`` keyword.
        4. Try LLM parse (Gemini) as primary parser.
        5. Fall back to ``nlp.parse_message()`` regex parsing.
        6. Dispatch parsed intent to appropriate handler.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
    if not _authorized(update):
        return

    text  = update.message.text.strip()
    state = context.user_data.get("state", "idle")

    # Check reply-to-edit context
    if state == "idle" and update.message.reply_to_message:
        replied = update.message.reply_to_message
        if replied.from_user and replied.from_user.is_bot:
            if await _handle_reply_edit(update, context):
                return

    # "resume" keyword to restore paused scheduling
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

    # State-specific handlers
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
    if state == "replying_delete":
        await _reply_delete_confirm(update, context, text)
        return
    if state == "replying_update":
        await _reply_update_value(update, context, text)
        return
    if state == "reminder_time":
        pending  = context.user_data.pop("pending_reminder", {})
        title    = pending.get("title", "Reminder")
        recurrence = pending.get("recurrence")
        dt       = nlp.extract_datetime(text)
        if not dt:
            p2 = llm_client.parse(text)
            if p2:
                dt = llm_client.normalise(p2).get("datetime")
        if not dt:
            await update.message.reply_text("Couldn't parse that time. Try `tomorrow 4pm` or `Monday 9am`.")
            context.user_data["pending_reminder"] = pending
            return
        context.user_data["state"] = "idle"
        await _do_create_reminder(update, title, dt, recurrence)
        return
    if state == "plan_confirm":
        await _plan_confirm(update, context, text)
        return
    if state == "reschedule_pick":
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
                        f"New time for *{event.get('summary','Event')}*? (e.g. `tomorrow 6pm`)",
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
            p = llm_client.parse(text)
            if p:
                new_dt = llm_client.normalise(p).get("datetime")
        if not new_dt:
            await update.message.reply_text("Couldn't parse that time. Try `tomorrow 6pm` or `Friday 10am`.")
            return
        context.user_data["state"] = "idle"
        await _do_reschedule(update, event, new_dt)
        return
    if state == "clarifying":
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

    # Primary parsing: try LLM first, fall back to regex NLP
    llm_result = llm_client.parse(text)
    if llm_result:
        parsed = llm_client.normalise(llm_result)
        if parsed["intent"] in ("unknown", "clarify"):
            parsed = nlp.parse_message(text)
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
        "reminder_list":  lambda u, c, *_: cmd_reminders(u, c),
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


async def _add_task(update, context, parsed):
    """Handle ``"add"`` intent — create one or more tasks.

    Supports multi-task creation from sentences like ``"I have midterm Thursday
    and assignment due Friday"``. Also supports deadline clarification
    follow-ups where the user replies ``"deadline is Friday"`` after adding a
    task without a deadline.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict from ``nlp.parse_message()`` or
            ``llm.normalise()``.
    """
    raw = parsed.get("raw", "")
    m   = _DEADLINE_CLARIFICATION_RE.match(raw.strip())
    last_task_id = context.user_data.get("last_added_task_id")
    if m and last_task_id:
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
    if not deadline_str:
        context.user_data["last_added_task_id"] = task_id
    else:
        context.user_data.pop("last_added_task_id", None)
    reply  = f"✅ *Task added!*\n\n{p_icon} *{title}*\n"
    reply += f"Due: {_fmt_deadline(deadline_str)}\n" if deadline_str else "No deadline set — reply `deadline is [date]` to add one\n"
    reply += f"Priority: {priority}\n"
    reply += f"ID: #{task_id}"
    reply += _entity_ref_line("task", task_id)
    await update.message.reply_text(reply, parse_mode="Markdown")


# ── LIST TASKS ─────────────────────────────────────────────────────────────────


async def _list_tasks(update, context, parsed):
    """Handle ``"list"`` intent — display tasks filtered by time period.

    Supports today, this week, next week, tomorrow, and all pending tasks.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
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
    elif "tomorrow" in text_lower:
        today    = datetime.now().date()
        tomorrow = today + timedelta(days=1)
        tasks    = db.get_tasks_by_period(tomorrow, tomorrow)
        header   = "📅 *Tomorrow's Tasks*"
    else:
        tasks   = db.get_unscheduled_tasks_sorted()
        header  = "📋 *All Pending Tasks*"
    if not tasks:
        await update.message.reply_text("✨ No tasks found — you're all clear!")
        return
    context.user_data["last_task_list"] = [dict(t) for t in tasks]
    await update.message.reply_text(
        f"{header}:\n\n{_fmt_task_list(tasks)}",
        parse_mode="Markdown",
    )


# ── SCHEDULE INTO CALENDAR ────────────────────────────────────────────────────


async def _schedule_intent(update, context, parsed):
    """Handle ``"schedule"`` intent — begin batch scheduling of tasks by index.

    User sends ``"schedule 1 2 3"`` after a task list. This handler enters
    the ``scheduling`` state and prompts for date/time for each task.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    raw  = parsed.get("raw", "")
    nums = re.findall(r"\d+", raw)
    if not nums:
        await update.message.reply_text("Which tasks? E.g. `schedule 1 2`")
        return
    task_list = context.user_data.get("last_task_list", [])
    if not task_list:
        await update.message.reply_text("No recent task list. Try /tasks first.")
        return
    indices = []
    for n in nums:
        i = int(n) - 1
        if 0 <= i < len(task_list):
            indices.append(i)
    if not indices:
        await update.message.reply_text("Task numbers not found in the last list.")
        return
    task_ids = [task_list[i]["id"] for i in indices]
    context.user_data["pending_schedule"] = task_ids
    context.user_data["schedule_idx"]     = 0
    context.user_data["state"]            = "scheduling"
    await _ask_schedule_time(update, context)


async def _batch_schedule_intent(update, context, parsed):
    """Handle ``"batch_schedule"`` intent — schedule tasks with datetime inline.

    Similar to ``_schedule_intent`` but the user provided a datetime in the
    same message (e.g. ``"schedule task 1 and 2 on Monday at 2pm"``).

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    await _schedule_intent(update, context, parsed)


async def _ask_schedule_time(update, context):
    """Prompt the user for a date/time for the current task being scheduled.

    Called iteratively for each task in the pending schedule queue.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
    idx       = context.user_data["schedule_idx"]
    task_ids  = context.user_data["pending_schedule"]
    if idx >= len(task_ids):
        context.user_data["state"] = "idle"
        await update.message.reply_text("✅ All tasks scheduled!")
        return
    task = db.get_task(task_ids[idx])
    if not task:
        context.user_data["schedule_idx"] = idx + 1
        await _ask_schedule_time(update, context)
        return
    title = task["title"]
    context.user_data["scheduling_task_id"] = task["id"]
    await update.message.reply_text(
        f"⏰ Schedule {idx+1}/{len(task_ids)} — *{title}*\n\n"
        "When? (e.g. `tomorrow 2pm`, `Thursday 10am`, or a duration like `2 hours`)",
        parse_mode="Markdown",
    )


async def _scheduling_time(update, context, text):
    """Handle user's time input during the scheduling flow.

    Parses the reply as either a duration or a datetime. If a duration is
    detected, enters ``scheduling_end_time`` state to ask for the date.
    If a datetime is detected, proceeds to confirmation.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        text: The user's reply text.
    """
    dur = nlp.extract_duration(text)
    if dur:
        context.user_data["scheduling_duration"] = dur
        context.user_data["state"] = "scheduling_end_time"
        await update.message.reply_text(
            f"Got it, {dur} minutes. What day/time? (e.g. `Thursday 2pm`)"
        )
        return
    dt = nlp.extract_datetime(text)
    if not dt:
        await update.message.reply_text("Couldn't parse that. Try `tomorrow 2pm` or `2 hours`.")
        return
    context.user_data["scheduling_dt"] = dt
    context.user_data["state"] = "confirm_schedule"
    await _show_schedule_confirm(update, context)


async def _scheduling_end_time(update, context, text):
    """Handle user's date input after they provided a duration.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        text: The user's reply text.
    """
    dt = nlp.extract_datetime(text)
    if not dt:
        await update.message.reply_text("Couldn't parse that. Try `Thursday 2pm`.")
        return
    context.user_data["scheduling_dt"] = dt
    context.user_data["state"] = "confirm_schedule"
    await _show_schedule_confirm(update, context)


async def _show_schedule_confirm(update, context):
    """Show a schedule confirmation message with proposed time and duration.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
    """
    task_id = context.user_data.get("scheduling_task_id")
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text("Task not found. Starting over.")
        context.user_data["state"] = "idle"
        return
    dt   = context.user_data.get("scheduling_dt")
    dur  = context.user_data.get("scheduling_duration") or task.get("estimated_minutes", 60)
    end  = dt + timedelta(minutes=dur)
    reminder_min = nlp.extract_reminder_minutes(task.get("title", ""))
    await update.message.reply_text(
        f"📅 *Confirm schedule:*\n"
        f"*{task['title']}*\n"
        f"📍 {_fmt_dt(dt)} → {end.strftime('%I:%M %p')} ({dur} min)\n"
        f"⏰ Reminder: {reminder_min} min before\n\n"
        "Reply `yes` to confirm, `no` to skip, or a different time.",
        parse_mode="Markdown",
    )


async def _confirm_schedule(update, context, text):
    """Handle user's confirmation reply after a schedule proposal.

    On ``"yes"``: creates the calendar event, updates the task's
    ``calendar_event_id`` and timestamps, then moves to the next task.
    On ``"no"``: skips the current task and proceeds to the next.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        text: The user's reply text.
    """
    text_lower = text.lower().strip()
    if text_lower in ("yes", "y", "ok", "✅"):
        task_id = context.user_data.get("scheduling_task_id")
        task = db.get_task(task_id)
        if not task:
            await update.message.reply_text("Task not found.")
            context.user_data["state"] = "idle"
            return
        dt   = context.user_data.get("scheduling_dt")
        dur  = context.user_data.get("scheduling_duration") or task.get("estimated_minutes", 60)
        reminder_min = nlp.extract_reminder_minutes(task.get("title", ""))
        try:
            event_id = calendar_client.create_event(task["title"], dt, dur, reminder_minutes=reminder_min)
            end = dt + timedelta(minutes=dur)
            db.update_task(task["id"],
                calendar_event_id=event_id,
                scheduled_start=dt.isoformat(),
                scheduled_end=end.isoformat(),
                status="in_progress",
            )
            await update.message.reply_text(
                f"✅ *Scheduled!*\n"
                f"*{task['title']}*\n"
                f"📍 {_fmt_dt(dt)} → {end.strftime('%I:%M %p')} ({dur} min)\n"
                f"⏰ Reminder: {reminder_min} min before"
                f"{_entity_ref_line('task', task['id'])}",
                parse_mode="Markdown",
            )
        except Exception as exc:
            await update.message.reply_text(f"⚠️ Couldn't create calendar event: {exc}")
        context.user_data["schedule_idx"] = context.user_data.get("schedule_idx", 0) + 1
        context.user_data["state"] = "scheduling"
        await _ask_schedule_time(update, context)
    else:
        await update.message.reply_text("Skipped.")
        context.user_data.pop("scheduling_dt", None)
        context.user_data.pop("scheduling_duration", None)
        context.user_data["schedule_idx"] = context.user_data.get("schedule_idx", 0) + 1
        context.user_data["state"] = "scheduling"
        await _ask_schedule_time(update, context)


# ── SCHEDULE DIRECT ────────────────────────────────────────────────────────────


async def _schedule_direct_intent(update, context, parsed):
    """Handle ``"schedule_direct"`` intent — schedule a direct calendar event.

    If a datetime was provided in the message, enters ``confirm_schedule``
    state. Otherwise, queries free slots and suggests the best one.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    title = parsed.get("title", "").strip() or "Event"
    dt    = parsed.get("datetime")
    dur   = parsed.get("duration") or 60
    context.user_data["scheduling_direct"] = {"title": title, "duration": dur}
    if dt:
        # User provided time inline — create the event directly, no confirmation needed
        try:
            event_id = calendar_client.create_event(title, dt, dur)
            end = dt + timedelta(minutes=dur)
            reminder_min = nlp.extract_reminder_minutes(title)
            await update.message.reply_text(
                f"✅ *Scheduled!*\n*{title}*\n"
                f"📍 {_fmt_dt(dt)} → {end.strftime('%I:%M %p')} ({dur} min)\n"
                f"⏰ Reminder: {reminder_min} min before"
                f"{_entity_ref_line('cal_event', event_id)}",
                parse_mode="Markdown",
            )
        except Exception as exc:
            await update.message.reply_text(f"⚠️ Couldn't create calendar event: {exc}")
        return
    else:
        free = smart_schedule.get_free_slots()
        msg, best = smart_schedule.build_suggestion_message(title, dur, free)
        context.user_data["state"] = "schedule_direct"
        await update.message.reply_text(msg, parse_mode="Markdown")


async def _schedule_direct_time(update, context, text):
    """Handle user's time input for a direct calendar event (no task).

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        text: The user's reply text.
    """
    text_lower = text.lower().strip()
    if text_lower in ("yes", "y", "ok", "✅"):
        direct = context.user_data.get("scheduling_direct", {})
        title  = direct.get("title", "Event")
        dur    = direct.get("duration", 60)
        free   = smart_schedule.get_free_slots()
        best   = smart_schedule.suggest_slot(title, dur, free)
        if best:
            dt = best["start"]
            try:
                event_id = calendar_client.create_event(title, dt, dur)
                end = dt + timedelta(minutes=dur)
                await update.message.reply_text(
                    f"✅ *Scheduled!*\n*{title}*\n📍 {_fmt_dt(dt)} → {end.strftime('%I:%M %p')} ({dur} min)"
                    f"{_entity_ref_line('cal_event', event_id)}",
                    parse_mode="Markdown",
                )
            except Exception as exc:
                await update.message.reply_text(f"⚠️ Couldn't create event: {exc}")
        else:
            await update.message.reply_text("No suitable free slots found.")
        context.user_data["state"] = "idle"
    else:
        dt = nlp.extract_datetime(text)
        if not dt:
            await update.message.reply_text("Couldn't parse that. Try `tomorrow 2pm`.")
            return
        direct = context.user_data.get("scheduling_direct", {})
        title  = direct.get("title", "Event")
        dur    = direct.get("duration", 60)
        try:
            event_id = calendar_client.create_event(title, dt, dur)
            end = dt + timedelta(minutes=dur)
            await update.message.reply_text(
                f"✅ *Scheduled!*\n*{title}*\n📍 {_fmt_dt(dt)} → {end.strftime('%I:%M %p')} ({dur} min)"
                f"{_entity_ref_line('cal_event', event_id)}",
                parse_mode="Markdown",
            )
        except Exception as exc:
            await update.message.reply_text(f"⚠️ Couldn't create event: {exc}")
        context.user_data["state"] = "idle"


# ── COMPLETE TASKS ─────────────────────────────────────────────────────────────


async def _complete_intent(update, context, parsed):
    """Handle ``"complete"`` intent — mark one or more tasks as done.

    Supports ``"done 1 2"`` (by list index) and ``"mark task 1 done"``
    (by task ID). Each completion includes an inline undo button.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    raw     = parsed.get("raw", "")
    task_id = parsed.get("task_id")
    # Try list indices first
    nums = re.findall(r"\bdone\s+(\d+)|\b(\d+)\s+done|^(\d+)$", raw, re.IGNORECASE)
    flat = [int(x) for t in nums for x in t if x]
    if flat:
        task_list = context.user_data.get("last_task_list", [])
        for n in flat:
            i = n - 1
            if 0 <= i < len(task_list):
                tid = task_list[i]["id"]
                db.complete_task(tid)
                task = db.get_task(tid)
                title = task["title"] if task else "Task"
                keyboard = [[InlineKeyboardButton("↩️ Undo", callback_data=f"undo_{tid}")]]
                await update.message.reply_text(
                    f"✅ *{title}* completed!",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
        return
    # Fallback: try numeric task_id
    nums = re.findall(r"\d+", raw)
    if nums:
        tid = int(nums[0])
        db.complete_task(tid)
        keyboard = [[InlineKeyboardButton("↩️ Undo", callback_data=f"undo_{tid}")]]
        await update.message.reply_text(
            f"✅ Task #{tid} completed!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return
    await update.message.reply_text("Which task? E.g. `done 1`, `mark task 1 done`")


# ── DELETE TASKS ───────────────────────────────────────────────────────────────


async def _delete_intent(update, context, parsed):
    """Handle ``"delete"`` intent — enter deletion confirmation flow.

    Supports ``"delete task 2"`` (by ID) and ``"delete 1"`` (by list index).

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    raw = parsed.get("raw", "")
    nums = re.findall(r"\bdelete\s+(?:task\s+)?(\d+)", raw, re.IGNORECASE)
    if nums:
        task_id = int(nums[0])
        context.user_data["pending_delete"] = task_id
        context.user_data["state"] = "deleting"
        task = db.get_task(task_id)
        title = task["title"] if task else "this task"
        await update.message.reply_text(
            f"🗑 Delete *{title}*?\n\nReply `yes` to confirm, `no` to cancel.",
            parse_mode="Markdown",
        )
    else:
        # Try list indices
        nums2 = re.findall(r"\bdelete\s+(\d+)", raw, re.IGNORECASE)
        if nums2:
            n = int(nums2[0]) - 1
            task_list = context.user_data.get("last_task_list", [])
            if 0 <= n < len(task_list):
                task_id = task_list[n]["id"]
                context.user_data["pending_delete"] = task_id
                context.user_data["state"] = "deleting"
                task = db.get_task(task_id)
                title = task["title"] if task else "this task"
                await update.message.reply_text(
                    f"🗑 Delete *{title}*?\n\nReply `yes` to confirm, `no` to cancel.",
                    parse_mode="Markdown",
                )
                return
        await update.message.reply_text("Which task? E.g. `delete task 2` or `delete 1`")


async def _delete_confirm(update, context, text):
    """Handle user's confirmation reply for deletion.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        text: The user's reply text.
    """
    if text.lower().strip() in ("yes", "y", "ok", "✅"):
        task_id = context.user_data.pop("pending_delete", None)
        if task_id:
            task = db.delete_task(task_id)
            if task:
                await update.message.reply_text(f"🗑 Deleted *{task['title']}*", parse_mode="Markdown")
            else:
                await update.message.reply_text("Task not found.")
    else:
        await update.message.reply_text("Cancelled.")
    context.user_data["state"] = "idle"


# ── UPDATE TASKS ───────────────────────────────────────────────────────────────


async def _update_intent(update, context, parsed):
    """Handle ``"update"`` intent — enter interactive update flow for a task.

    Supports ``"update task 1 deadline to Monday"`` — the deadline is parsed
    immediately. Otherwise enters ``updating`` state to collect the new value.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    raw = parsed.get("raw", "")
    m   = re.search(r"update\s+(?:task\s+)?(\d+)", raw, re.IGNORECASE)
    if not m:
        await update.message.reply_text("Which task? E.g. `update task 1 deadline to Monday`")
        return
    task_id = int(m.group(1))
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text("Task not found.")
        return
    # Try to extract a new deadline directly
    dt = parsed.get("datetime")
    if dt:
        db.update_task(task_id, deadline=dt.date().isoformat())
        await update.message.reply_text(
            f"✏️ Updated deadline for *{task['title']}*: {_fmt_deadline(dt.date().isoformat())}"
            f"{_entity_ref_line('task', task_id)}",
            parse_mode="Markdown",
        )
        return
    context.user_data["pending_update"] = {"task_id": task_id, "field": "deadline"}
    context.user_data["state"] = "updating"
    await update.message.reply_text(
        f"✏️ *{task['title']}*\nNew deadline? (e.g. `Friday`, `next Monday`)",
        parse_mode="Markdown",
    )


async def _update_value(update, context, text):
    """Handle user's new value input during the update flow.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        text: The user's reply text.
    """
    pending = context.user_data.get("pending_update", {})
    task_id = pending.get("task_id")
    field   = pending.get("field", "deadline")
    if not task_id:
        await update.message.reply_text("No pending update.")
        context.user_data["state"] = "idle"
        return
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text("Task not found.")
        context.user_data["state"] = "idle"
        return
    if field == "deadline":
        dt = nlp.extract_datetime(text)
        if dt:
            db.update_task(task_id, deadline=dt.date().isoformat())
            await update.message.reply_text(
                f"✏️ Updated deadline for *{task['title']}*: {_fmt_deadline(dt.date().isoformat())}"
                f"{_entity_ref_line('task', task_id)}",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("Couldn't parse that date.")
    context.user_data["state"] = "idle"


# ── RESCHEDULE ─────────────────────────────────────────────────────────────────


async def _reschedule_intent(update, context, parsed):
    """Handle ``"reschedule"`` intent — move a calendar event to a new time.

    Searches for events matching the task title and lets the user pick.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    raw = parsed.get("raw", "")
    m   = re.search(r"move\s+(?:task\s+)?(\d+)", raw, re.IGNORECASE)
    if m:
        task_id = int(m.group(1))
        task = db.get_task(task_id)
        if task and task["calendar_event_id"]:
            event = calendar_client.get_event(task["calendar_event_id"])
            if event:
                new_dt = parsed.get("datetime")
                if new_dt:
                    await _do_reschedule(update, event, new_dt)
                else:
                    context.user_data["reschedule_event"] = event
                    context.user_data["state"] = "reschedule_time"
                    await update.message.reply_text(
                        f"New time for *{event.get('summary','Event')}*? (e.g. `tomorrow 6pm`)",
                        parse_mode="Markdown",
                    )
                return
    await update.message.reply_text("Which event? Try `move task 1 to next Thursday`.")


async def _do_reschedule(update, event, new_dt):
    """Execute a calendar event reschedule.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        event: The Google Calendar event dict.
        new_dt: The new start datetime.
    """
    event_id = event["id"]
    try:
        dur = calendar_client.reschedule_event(event_id, new_dt)
        end = new_dt + timedelta(minutes=dur)
        db.update_task_schedule_by_event(event_id, new_dt.isoformat(), end.isoformat())
        await update.message.reply_text(
            f"✅ *Calendar Event Updated*\n\n"
            f"*{event.get('summary','Event')}*\n"
            f"📍 {_fmt_dt(new_dt)} → {end.strftime('%I:%M %p')} ({dur} min)"
            f"{_entity_ref_line('cal_event', event_id)}",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"⚠️ Couldn't update event: {exc}")


# ── HABITS ─────────────────────────────────────────────────────────────────────


async def _habit_add(update, context, parsed):
    """Handle ``"habit_add"`` intent — create a new habit.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    title     = parsed.get("title", "").strip()
    frequency = parsed.get("frequency", "daily")
    count     = parsed.get("count", 1)
    notes     = parsed.get("notes")
    if not title or len(title) < 2:
        await update.message.reply_text("What habit? E.g. `add daily habit: read 30 min`")
        return
    habit_id = db.add_habit(title, frequency, count, notes)
    freq_label = "daily" if frequency == "daily" else f"{count}x/week"
    await update.message.reply_text(
        f"✅ *Habit added!*\n\n{title} — {freq_label}\nID: #{habit_id}"
    )


async def _habit_delete(update, context, parsed):
    """Handle ``"habit_delete"`` intent — delete a habit by ID.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    raw = parsed.get("raw", "")
    m   = re.search(r"delete\s+habit\s+(\d+)", raw, re.IGNORECASE)
    if m:
        habit_id = int(m.group(1))
        habit = db.delete_habit(habit_id)
        if habit:
            await update.message.reply_text(f"🗑 Deleted habit *{habit['title']}*", parse_mode="Markdown")
        else:
            await update.message.reply_text("Habit not found.")
    else:
        await update.message.reply_text("Which habit? E.g. `delete habit 1`")


# ── REMINDERS ──────────────────────────────────────────────────────────────────


async def _reminder_intent(update, context, parsed):
    """Handle ``"reminder"`` intent — create a bot-side reminder.

    If a datetime was provided, creates the reminder immediately. Otherwise
    enters ``reminder_time`` state to collect the time.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    title      = parsed.get("title", "Reminder").strip()
    dt         = parsed.get("datetime")
    recurrence = parsed.get("recurrence")
    if not title:
        await update.message.reply_text("What should I remind you about?")
        return
    if dt:
        await _do_create_reminder(update, title, dt, recurrence)
    else:
        context.user_data["pending_reminder"] = {"title": title, "recurrence": recurrence}
        context.user_data["state"] = "reminder_time"
        await update.message.reply_text(f"⏰ *{title}*\n\nWhen? (e.g. `tomorrow 4pm`, `Monday 9am`)", parse_mode="Markdown")


async def _do_create_reminder(update, title, dt, recurrence=None):
    """Create a bot-side reminder and store it in the database.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        title: Reminder text.
        dt: The trigger datetime.
        recurrence: Optional RRULE dict.
    """
    rrule = recurrence["rrule"] if recurrence else None
    rid   = db.add_reminder(title, dt.isoformat(), rrule)
    recur_tag = f" ({recurrence['summary']})" if recurrence else ""
    await update.message.reply_text(
        f"✅ *Reminder set!*\n\n🔔 {title}\n🕐 {_fmt_dt(dt)}{recur_tag}"
        f"{_entity_ref_line('reminder', rid)}",
        parse_mode="Markdown",
    )


# ── REPLY-TO-EDIT CONFIRMATIONS ────────────────────────────────────────────────


async def _reply_delete_confirm(update, context, text):
    """Handle confirmation for reply-to-delete flow.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        text: User's reply text.
    """
    if text.lower().strip() in ("yes", "y", "ok", "✅"):
        info = context.user_data.pop("reply_delete", {})
        entity_type = info.get("entity_type")
        entity_id   = info.get("entity_id")
        if entity_type == "task":
            task = db.delete_task(int(entity_id))
            await update.message.reply_text(f"🗑 Deleted *{task['title']}*" if task else "⚠️ Not found.", parse_mode="Markdown")
        elif entity_type == "reminder":
            rem = db.delete_reminder(int(entity_id))
            await update.message.reply_text(f"🗑 Deleted reminder *{rem['title']}*" if rem else "⚠️ Not found.", parse_mode="Markdown")
        elif entity_type == "cal_event":
            try:
                calendar_client.delete_event(entity_id)
                await update.message.reply_text("🗑 Deleted calendar event.")
            except Exception:
                await update.message.reply_text("⚠️ Couldn't delete event.")
    else:
        await update.message.reply_text("Cancelled.")
    context.user_data["state"] = "idle"


async def _reply_update_value(update, context, text):
    """Handle user's input during reply-to-update flow.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        text: User's reply text.
    """
    info = context.user_data.get("reply_update", {})
    entity_type = info.get("entity_type")
    entity_id   = info.get("entity_id")
    if not entity_type or not entity_id:
        await update.message.reply_text("No pending update.")
        context.user_data["state"] = "idle"
        return
    new_dt = nlp.extract_datetime(text)
    if entity_type == "task":
        task = db.get_task(int(entity_id))
        if not task:
            await update.message.reply_text("⚠️ Task not found.")
            context.user_data["state"] = "idle"
            return
        if new_dt:
            db.update_task(task["id"], deadline=new_dt.date().isoformat())
        else:
            db.update_task(task["id"], title=text)
        task = db.get_task(task["id"])
        await update.message.reply_text(
            f"✏️ *Task Updated*\n\n{_fmt_entity('task', task)}"
            f"{_entity_ref_line('task', task['id'])}",
            parse_mode="Markdown",
        )
    elif entity_type == "reminder":
        if new_dt:
            db.update_reminder(int(entity_id), remind_at=new_dt.isoformat())
        else:
            db.update_reminder(int(entity_id), title=text)
        reminder = db.get_reminder(int(entity_id))
        await update.message.reply_text(
            f"✏️ *Reminder Updated*\n\n{_fmt_entity('reminder', reminder)}"
            f"{_entity_ref_line('reminder', reminder['id'])}",
            parse_mode="Markdown",
        )
    context.user_data["state"] = "idle"


# ── FREE TIME / PLANNING ───────────────────────────────────────────────────────


async def _free_time_intent(update, context, parsed):
    """Handle ``"free_time"`` intent — show free calendar blocks.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    dur    = parsed.get("duration") or 60
    days   = parsed.get("days", 7)
    free   = smart_schedule.get_free_slots(days_ahead=days, min_duration_min=dur)
    if not free:
        await update.message.reply_text("No free slots found this week. Try a different period.")
        return
    msg = f"📅 *Free slots* (≥{dur} min):\n\n"
    msg += smart_schedule.format_slots_for_display(free, limit=8)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def _plan_tasks_intent(update, context, parsed):
    """Handle ``"plan"`` intent — auto-suggest task placements.

    Fetches unscheduled tasks and free slots, then builds a plan and shows
    it with an accept option.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    tasks = db.get_plannable_tasks(limit=10)
    if not tasks:
        await update.message.reply_text("No unscheduled tasks to plan.")
        return
    free = smart_schedule.get_free_slots()
    if not free:
        await update.message.reply_text("No free slots available this week.")
        return
    plan = smart_schedule.build_task_plan(tasks, free, limit=6)
    if not plan:
        await update.message.reply_text("Couldn't fit tasks into this week's schedule.")
        return
    plan_text = smart_schedule.format_task_plan(plan)
    context.user_data["last_plan"] = plan
    context.user_data["state"] = "plan_confirm"
    await update.message.reply_text(
        f"📋 *Suggested Plan*\n\n{plan_text}\n\n"
        "Reply `yes` to schedule all, or `schedule 1 3` to pick specific ones.",
        parse_mode="Markdown",
    )


async def _plan_confirm(update, context, text):
    """Handle user's confirmation of a suggested plan.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        text: User's reply text.
    """
    text_lower = text.lower().strip()
    plan = context.user_data.get("last_plan", [])
    if text_lower in ("yes", "y", "ok", "✅"):
        created = 0
        for item in plan:
            task = item["task"]
            try:
                event_id = calendar_client.create_event(
                    task["title"], item["start"],
                    int((item["end"] - item["start"]).total_seconds() / 60),
                )
                db.update_task(task["id"],
                    calendar_event_id=event_id,
                    scheduled_start=item["start"].isoformat(),
                    scheduled_end=item["end"].isoformat(),
                    status="in_progress",
                )
                created += 1
            except Exception:
                pass
        await update.message.reply_text(f"✅ Scheduled {created}/{len(plan)} tasks!")
    else:
        m = re.search(r"schedule\s+([\d\s,]+)", text_lower)
        if m:
            nums = re.findall(r"\d+", m.group(1))
            for n in nums:
                i = int(n) - 1
                if 0 <= i < len(plan):
                    item = plan[i]
                    task = item["task"]
                    try:
                        event_id = calendar_client.create_event(
                            task["title"], item["start"],
                            int((item["end"] - item["start"]).total_seconds() / 60),
                        )
                        db.update_task(task["id"],
                            calendar_event_id=event_id,
                            scheduled_start=item["start"].isoformat(),
                            scheduled_end=item["end"].isoformat(),
                            status="in_progress",
                        )
                    except Exception:
                        pass
            await update.message.reply_text(f"✅ Selected tasks scheduled!")
        else:
            await update.message.reply_text("Cancelled.")
    context.user_data["state"] = "idle"


# ── CALENDAR QUERY ─────────────────────────────────────────────────────────────


async def _calendar_query(update, context, parsed):
    """Handle ``"calendar_query"`` intent — answer questions about the schedule.

    Delegates to the LLM for natural-language calendar queries.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    events = calendar_client.list_upcoming_events(days=7)
    msg = llm_client.answer_calendar_query(parsed.get("raw", ""), events)
    if msg:
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("Couldn't answer that. Try a simpler question.")


# ── CLARIFY ────────────────────────────────────────────────────────────────────


async def _handle_clarify(update, context, parsed):
    """Handle ``"clarify"`` intent — ask the user for more information.

    Enters ``clarifying`` state and saves the original message so it can be
    re-parsed after the user answers.

    Args:
        update: The incoming Telegram update.
        context: The callback context.
        parsed: Parsed intent dict.
    """
    context.user_data["clarify_original"] = parsed.get("raw", "")
    context.user_data["state"] = "clarifying"
    await update.message.reply_text(
        parsed.get("question", "Could you clarify? What would you like to do?")
    )


# ── INLINE CALLBACK HANDLER ───────────────────────────────────────────────────


async def handle_callback(update, context):
    """Handle inline keyboard button callbacks.

    Supports:
        - ``"undo_<task_id>"`` — uncomplete a task
        - ``"plan_<task_id>"`` — schedule a specific task

    Args:
        update: The incoming Telegram update (containing callback query).
        context: The callback context.
    """
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("undo_"):
        task_id = int(data.split("_")[1])
        db.uncomplete_task(task_id)
        await query.edit_message_text(f"↩️ Task #{task_id} reverted to pending.")
    elif data.startswith("plan_"):
        task_id = int(data.split("_")[1])
        task = db.get_task(task_id)
        if task:
            context.user_data["pending_schedule"] = [task_id]
            context.user_data["schedule_idx"]     = 0
            context.user_data["state"]            = "scheduling"
            await query.edit_message_text(f"🔄 Scheduling *{task['title']}*...", parse_mode="Markdown")
            await _ask_schedule_time(update, context)


# ── MAIN ───────────────────────────────────────────────────────────────────────


def main():
    """Application entry point — build the bot, register handlers, and start
    polling.

    Registers all command and message handlers, sets up the APScheduler
    background jobs, and starts the Telegram long-polling loop.
    """
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("tasks",   cmd_tasks))
    app.add_handler(CommandHandler("today",   cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("week",    cmd_week))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("habits",  cmd_habits))
    app.add_handler(CommandHandler("free",    cmd_free))
    app.add_handler(CommandHandler("plan",    cmd_plan))
    app.add_handler(CommandHandler("now",     cmd_now))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))

    # Catch-all message handler (async, filters.TEXT)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Callback query handler for inline buttons
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Initialise the database
    db.init_db()

    # Set up APScheduler background jobs
    setup_scheduler(app)

    # Start long polling
    logger.info("Starting Planning Bot...")
    app.run_polling()


if __name__ == "__main__":
    main()