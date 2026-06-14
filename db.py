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
SQLite database layer — CRUD operations for tasks, habits, and reminders.

All persistence is handled via the sqlite3 standard library module. No external
database server is required. The database file location is configured by the
``DB_PATH`` environment variable (default: ``planner.db``).

Four tables are managed:
    - **tasks**: Task entries with priority, deadline, category, energy, and
      calendar scheduling metadata.
    - **habits**: Daily or weekly habit tracking with repetition counts.
    - **reminders**: Bot-side reminders (independent of Google Calendar) with
      optional recurrence via RRULE strings.
    - **sent_reminders**: Deduplication table that records which calendar-event
      reminders have already been dispatched, keyed by a unique ``event_key``.
"""

import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "planner.db")


# ── Connection helpers ─────────────────────────────────────────────────────────


def get_conn():
    """Open a new SQLite connection with row-factory set to dict-like access.

    Returns:
        sqlite3.Connection: A connection whose ``fetchone()`` and ``fetchall()``
            results behave as dictionaries (via ``sqlite3.Row``).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    """Return the set of column names for a given table.

    Args:
        conn: SQLite connection.
        table: Table name (e.g. ``"tasks"``).

    Returns:
        set[str]: Column names currently present in the table schema.
    """
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(conn, table, column, definition):
    """Add a column to a table if it does not already exist (auto-migration).

    This allows the schema to evolve without manual ``ALTER TABLE`` statements
    or destructive migrations. New columns are only appended; existing data is
    never dropped.

    Args:
        conn: SQLite connection.
        table: Table name.
        column: Column name to add.
        definition: SQL type and default clause (e.g. ``"INTEGER DEFAULT 60"``).
    """
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# ── Database initialisation ────────────────────────────────────────────────────


def init_db():
    """Create all four tables and apply any pending schema migrations.

    Tables are created with ``CREATE TABLE IF NOT EXISTS`` so the function is
    idempotent. After table creation, ``_ensure_column()`` is called for each
    column that was added after the initial schema release (``earliest_start``,
    ``estimated_minutes``, ``category``, ``energy``, ``splittable``).

    Call once at application startup before any other ``db.*`` function.
    """
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                title             TEXT    NOT NULL,
                deadline          TEXT,
                earliest_start    TEXT,
                estimated_minutes INTEGER DEFAULT 60,
                category          TEXT    DEFAULT 'general',
                energy            TEXT    DEFAULT 'medium',
                splittable        INTEGER DEFAULT 0,
                priority          TEXT    DEFAULT 'medium',
                status            TEXT    DEFAULT 'pending',
                notes             TEXT,
                calendar_event_id TEXT,
                scheduled_start   TEXT,
                scheduled_end     TEXT,
                created_at        TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS habits (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT    NOT NULL,
                frequency  TEXT    NOT NULL,
                count      INTEGER DEFAULT 1,
                notes      TEXT,
                active     INTEGER DEFAULT 1,
                created_at TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_reminders (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT    NOT NULL,
                sent_at   TEXT    DEFAULT (datetime('now')),
                UNIQUE(event_key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                title            TEXT    NOT NULL,
                remind_at        TEXT    NOT NULL,
                recurrence_rrule TEXT,
                active           INTEGER DEFAULT 1,
                created_at       TEXT    DEFAULT (datetime('now')),
                last_sent_at     TEXT
            )
        """)

        # Auto-migrate: add columns that were introduced after the initial schema
        _ensure_column(conn, "tasks", "earliest_start", "TEXT")
        _ensure_column(conn, "tasks", "estimated_minutes", "INTEGER DEFAULT 60")
        _ensure_column(conn, "tasks", "category", "TEXT DEFAULT 'general'")
        _ensure_column(conn, "tasks", "energy", "TEXT DEFAULT 'medium'")
        _ensure_column(conn, "tasks", "splittable", "INTEGER DEFAULT 0")
        conn.commit()


# ── Tasks: Create ──────────────────────────────────────────────────────────────


def add_task(
    title,
    deadline=None,
    priority="medium",
    notes=None,
    estimated_minutes=60,
    category="general",
    energy="medium",
    earliest_start=None,
    splittable=False,
):
    """Insert a new task into the database.

    Args:
        title: Task name (displayed in Telegram messages).
        deadline: ISO-formatted date string (``"YYYY-MM-DD"``) or ``None``.
        priority: One of ``"low"``, ``"medium"``, ``"high"``.
        notes: Optional free-text notes.
        estimated_minutes: Expected effort in minutes (default 60).
        category: Task category for smart scheduling (e.g. ``"focus"``,
            ``"fitness"``, ``"meeting"``, ``"errand"``, ``"general"``).
        energy: Expected energy level (``"low"``, ``"medium"``, ``"high"``).
        earliest_start: ISO date string before which the task should not be
            scheduled.
        splittable: Whether the task can be split across multiple calendar
            blocks.

    Returns:
        int: The auto-generated ``id`` of the newly created task row.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO tasks
               (title, deadline, priority, notes, estimated_minutes, category, energy, earliest_start, splittable)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title, deadline, priority, notes, estimated_minutes, category,
                energy, earliest_start, 1 if splittable else 0,
            ),
        )
        conn.commit()
        return cur.lastrowid


# ── Tasks: Read ────────────────────────────────────────────────────────────────


def get_task(task_id):
    """Fetch a single task by its primary key.

    Args:
        task_id: The task ID.

    Returns:
        sqlite3.Row or None: The task row, or ``None`` if not found.
    """
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def get_all_tasks(status=None):
    """Return all tasks, optionally filtered by status.

    Results are ordered by priority (high first), then deadline (nearest first),
    then creation date.

    Args:
        status: If provided, only tasks with this status are returned (e.g.
            ``"pending"``, ``"done"``, ``"in_progress"``). If ``None``, all
            statuses are included.

    Returns:
        list[sqlite3.Row]: Matching task rows.
    """
    with get_conn() as conn:
        if status:
            return conn.execute(
                """SELECT * FROM tasks WHERE status = ?
                   ORDER BY
                     CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                     deadline ASC, created_at ASC""",
                (status,),
            ).fetchall()
        return conn.execute(
            """SELECT * FROM tasks
               ORDER BY
                 CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                 deadline ASC, created_at ASC"""
        ).fetchall()


def get_tasks_sorted_by_deadline(status_filter=None):
    """Return non-done tasks ordered by deadline (nulls last), then priority.

    Args:
        status_filter: Optional status string to filter by. If ``None``, all
            statuses except ``"done"`` are included.

    Returns:
        list[sqlite3.Row]: Tasks with deadlines first (ascending), then tasks
            without deadlines.
    """
    with get_conn() as conn:
        if status_filter:
            return conn.execute(
                """SELECT * FROM tasks
                   WHERE status = ?
                   ORDER BY
                     CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
                     deadline ASC,
                     CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                     created_at ASC""",
                (status_filter,),
            ).fetchall()
        return conn.execute(
            """SELECT * FROM tasks
               WHERE status != 'done'
               ORDER BY
                 CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
                 deadline ASC,
                 CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                 created_at ASC"""
        ).fetchall()


def get_unscheduled_tasks_sorted():
    """Return unscheduled, non-done tasks ordered by deadline (nulls last).

    A task is considered unscheduled if its ``calendar_event_id`` is ``NULL``.

    Returns:
        list[sqlite3.Row]: Tasks without a Google Calendar event attached.
    """
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE calendar_event_id IS NULL
                 AND status != 'done'
               ORDER BY
                 CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
                 deadline ASC,
                 CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                 created_at ASC"""
        ).fetchall()


def get_tasks_this_week():
    """Return unfinished tasks whose deadlines fall within the current week.

    The week is defined as Monday 00:00 through Sunday 23:59 in the timezone
    configured via the ``TIMEZONE`` environment variable (default
    ``"Asia/Seoul"``).

    Returns:
        list[sqlite3.Row]: Tasks due this week.
    """
    import pytz
    tz    = pytz.timezone(os.getenv("TIMEZONE", "Asia/Seoul"))
    today = datetime.now(tz).date()
    start = today - timedelta(days=today.weekday())  # Monday
    end   = start + timedelta(days=6)                 # Sunday
    return get_tasks_by_period(start, end)


def get_tasks_by_period(start_date, end_date):
    """Return unfinished tasks whose deadlines fall within a date range.

    Args:
        start_date: Inclusive start (``datetime.date`` or ISO string).
        end_date: Inclusive end (``datetime.date`` or ISO string).

    Returns:
        list[sqlite3.Row]: Tasks in the date range, ordered by priority then
            deadline.
    """
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE deadline BETWEEN ? AND ?
                 AND status != 'done'
               ORDER BY
                 CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                 deadline ASC""",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()


def get_overdue_tasks():
    """Return pending tasks whose deadline has passed (before today).

    Returns:
        list[sqlite3.Row]: Overdue tasks sorted by deadline (oldest first).
    """
    today = datetime.now().date().isoformat()
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE deadline < ?
                 AND status != 'done'
               ORDER BY deadline ASC""",
            (today,),
        ).fetchall()


def get_overdue_unscheduled_tasks():
    """Return overdue, unscheduled, non-done tasks for the overdue lifecycle.

    These are candidates for the reminder → warning → auto-delete lifecycle
    managed by the scheduler.

    Returns:
        list[sqlite3.Row]: Overdue tasks without a calendar event, ordered by
            deadline (oldest first).
    """
    today = datetime.now().date().isoformat()
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE deadline < ?
                 AND calendar_event_id IS NULL
                 AND status != 'done'
               ORDER BY deadline ASC""",
            (today,),
        ).fetchall()


def get_completed_this_week():
    """Return tasks marked 'done' whose deadlines fell within the current week.

    Used by the Sunday weekly review job to compute completion statistics.

    Returns:
        list[sqlite3.Row]: Completed tasks from this week.
    """
    today = datetime.now().date()
    start = today - timedelta(days=today.weekday())  # Monday
    end   = start + timedelta(days=6)                 # Sunday
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE status = 'done'
                 AND deadline BETWEEN ? AND ?
               ORDER BY deadline ASC""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()


def get_planned_this_week():
    """Return all tasks (any status) with deadlines falling within the current week.

    Used by the Sunday weekly review to compare completed vs. planned counts.

    Returns:
        list[sqlite3.Row]: All tasks whose deadline falls this week.
    """
    today = datetime.now().date()
    start = today - timedelta(days=today.weekday())
    end   = start + timedelta(days=6)
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE deadline BETWEEN ? AND ?
               ORDER BY deadline ASC""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()


def get_urgent_tasks(days_ahead: int = 2):
    """Return unscheduled, non-done tasks due within *days_ahead* days.

    Used by the midday urgency check scheduler job to alert the user about
    looming deadlines that haven't been calendar-blocked yet.

    Args:
        days_ahead: Look-ahead window (default 2 days).

    Returns:
        list[sqlite3.Row]: Urgent, unscheduled tasks.
    """
    today    = datetime.now().date()
    deadline = (today + timedelta(days=days_ahead)).isoformat()
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE deadline <= ?
                 AND deadline >= ?
                 AND status != 'done'
                 AND calendar_event_id IS NULL
               ORDER BY deadline ASC, priority DESC""",
            (deadline, today.isoformat()),
        ).fetchall()


def get_unscheduled_tasks(status_filter=None):
    """Return tasks without a calendar event, optionally filtered by status.

    Args:
        status_filter: If provided, only tasks with this status are returned.
            If ``None``, all statuses except ``"done"`` are included.

    Returns:
        list[sqlite3.Row]: Unscheduled tasks.
    """
    with get_conn() as conn:
        if status_filter:
            return conn.execute(
                """SELECT * FROM tasks WHERE calendar_event_id IS NULL
                   AND status = ? ORDER BY
                   CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                   deadline ASC NULLS LAST""",
                (status_filter,),
            ).fetchall()
        return conn.execute(
            """SELECT * FROM tasks WHERE calendar_event_id IS NULL
               AND status != 'done' ORDER BY
               CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
               deadline ASC NULLS LAST"""
        ).fetchall()


def get_plannable_tasks(limit=12):
    """Return the most urgent unscheduled tasks for the smart planner.

    Tasks are ordered by priority (high first) then deadline (nearest first),
    then creation date. The result is capped so the planner doesn't overflow
    the Telegram message length.

    Args:
        limit: Maximum number of tasks to return (default 12).

    Returns:
        list[sqlite3.Row]: The ``limit`` most urgent plannable tasks.
    """
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE calendar_event_id IS NULL
                 AND status != 'done'
               ORDER BY
                 CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                 deadline ASC NULLS LAST,
                 created_at ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()


def search_tasks(query):
    """Search non-done tasks by title substring (case-insensitive via SQL ``LIKE``).

    Args:
        query: Substring to search for in task titles.

    Returns:
        list[sqlite3.Row]: Matching tasks ordered by deadline.
    """
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE title LIKE ? AND status != 'done' ORDER BY deadline",
            (f"%{query}%",),
        ).fetchall()


# ── Tasks: Update ──────────────────────────────────────────────────────────────


def update_task(task_id, **kwargs):
    """Update one or more fields on a task by its ID.

    Args:
        task_id: The task to update.
        **kwargs: Column-value pairs to set (e.g. ``title="New name"``,
            ``deadline="2026-06-20"``).
    """
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [task_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE tasks SET {fields} WHERE id = ?", values)
        conn.commit()


def update_task_schedule_by_event(event_id, scheduled_start, scheduled_end):
    """Update scheduling timestamps for the task attached to a calendar event.

    Called after a calendar event is created or rescheduled.

    Args:
        event_id: The Google Calendar event ID.
        scheduled_start: ISO datetime string for the new start.
        scheduled_end: ISO datetime string for the new end.
    """
    with get_conn() as conn:
        conn.execute(
            """UPDATE tasks
               SET scheduled_start = ?, scheduled_end = ?
               WHERE calendar_event_id = ?""",
            (scheduled_start, scheduled_end, event_id),
        )
        conn.commit()


def complete_task(task_id):
    """Mark a task as done and record the completion timestamp.

    Args:
        task_id: The task to mark complete.
    """
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'done', completed_at = datetime('now') WHERE id = ?", (task_id,))
        conn.commit()


def uncomplete_task(task_id):
    """Revert a completed task back to pending status (undo).

    Args:
        task_id: The task to uncomplete.
    """
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'pending', completed_at = NULL WHERE id = ?", (task_id,))
        conn.commit()


# ── Tasks: Delete ──────────────────────────────────────────────────────────────


def delete_task(task_id):
    """Delete a task and return the deleted row data.

    Args:
        task_id: The task to delete.

    Returns:
        sqlite3.Row or None: The deleted task row, or ``None`` if not found.
    """
    with get_conn() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return task


# ── Habits ─────────────────────────────────────────────────────────────────────


def add_habit(title, frequency, count=1, notes=None):
    """Create a new habit entry.

    Args:
        title: Habit name.
        frequency: ``"daily"`` or ``"weekly"``.
        count: Target repetitions per frequency period (default 1).
        notes: Optional short descriptor (e.g. ``"30 min"``, ``"5km"``).

    Returns:
        int: The new habit ID.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO habits (title, frequency, count, notes) VALUES (?, ?, ?, ?)",
            (title, frequency, count, notes),
        )
        conn.commit()
        return cur.lastrowid


def get_habits(frequency=None, active_only=True):
    """Return habits, optionally filtered by frequency and active status.

    Args:
        frequency: ``"daily"`` or ``"weekly"``, or ``None`` for both.
        active_only: If ``True`` (default), only return active habits.

    Returns:
        list[sqlite3.Row]: Matching habits.
    """
    with get_conn() as conn:
        if frequency:
            return conn.execute(
                "SELECT * FROM habits WHERE frequency = ? AND active = 1 ORDER BY created_at",
                (frequency,),
            ).fetchall()
        if active_only:
            return conn.execute(
                "SELECT * FROM habits WHERE active = 1 ORDER BY frequency, created_at"
            ).fetchall()
        return conn.execute("SELECT * FROM habits ORDER BY frequency, created_at").fetchall()


def get_habit(habit_id):
    """Fetch a single habit by its ID.

    Args:
        habit_id: The habit ID.

    Returns:
        sqlite3.Row or None: The habit row, or ``None``.
    """
    with get_conn() as conn:
        return conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()


def delete_habit(habit_id):
    """Delete a habit and return the deleted row data.

    Args:
        habit_id: The habit to delete.

    Returns:
        sqlite3.Row or None: The deleted habit row.
    """
    with get_conn() as conn:
        habit = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        conn.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
        conn.commit()
        return habit


def toggle_habit(habit_id, active):
    """Activate or deactivate a habit.

    Args:
        habit_id: The habit to toggle.
        active: ``True`` to activate, ``False`` to deactivate.
    """
    with get_conn() as conn:
        conn.execute("UPDATE habits SET active = ? WHERE id = ?", (1 if active else 0, habit_id))
        conn.commit()


# ── Sent reminders (deduplication) ─────────────────────────────────────────────


def reminder_already_sent(event_key):
    """Check whether a calendar-event reminder has already been dispatched.

    Uses the unique ``event_key`` constraint in ``sent_reminders`` to prevent
    duplicate notifications for the same event.

    Args:
        event_key: A unique string identifying the event reminder (typically
            ``"{event_id}_{minutes_before}"``).

    Returns:
        bool: ``True`` if the reminder was already sent.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM sent_reminders WHERE event_key = ?", (event_key,)
        ).fetchone()
        return row is not None


def mark_reminder_sent(event_key):
    """Record that a calendar-event reminder has been dispatched.

    The ``UNIQUE`` constraint on ``event_key`` silently ignores duplicates,
    so it is safe to call this function multiple times for the same event.

    Args:
        event_key: The deduplication key.
    """
    with get_conn() as conn:
        try:
            conn.execute("INSERT INTO sent_reminders (event_key) VALUES (?)", (event_key,))
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # Duplicate key — reminder was already recorded


# ── App reminders (not calendar blocks) ───────────────────────────────────────


def add_reminder(title, remind_at, recurrence_rrule=None):
    """Create a bot-side reminder that will be sent via Telegram.

    These reminders are independent of Google Calendar events. They can be
    one-shot or recurring (via RRULE).

    Args:
        title: Reminder message text.
        remind_at: ISO datetime string for the first trigger time.
        recurrence_rrule: Optional RRULE string for recurring reminders
            (e.g. ``"RRULE:FREQ=DAILY"``).

    Returns:
        int: The new reminder ID.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO reminders (title, remind_at, recurrence_rrule)
               VALUES (?, ?, ?)""",
            (title, remind_at, recurrence_rrule),
        )
        conn.commit()
        return cur.lastrowid


def get_due_app_reminders(now_iso):
    """Return active reminders whose trigger time is at or before *now*.

    Called every minute by the 1-min app reminder scheduler job.

    Args:
        now_iso: Current time as an ISO-formatted string.

    Returns:
        list[sqlite3.Row]: Due reminders ordered by trigger time (oldest first).
    """
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM reminders
               WHERE active = 1
                 AND remind_at <= ?
               ORDER BY remind_at ASC""",
            (now_iso,),
        ).fetchall()


def get_reminder(reminder_id):
    """Fetch a single bot-side reminder by its ID.

    Args:
        reminder_id: The reminder ID.

    Returns:
        sqlite3.Row or None: The reminder row.
    """
    with get_conn() as conn:
        return conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()


def delete_reminder(reminder_id):
    """Delete a bot-side reminder and return the deleted row data.

    Args:
        reminder_id: The reminder to delete.

    Returns:
        sqlite3.Row or None: The deleted reminder row.
    """
    with get_conn() as conn:
        reminder = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.commit()
        return reminder


def update_reminder(reminder_id, **kwargs):
    """Update one or more fields on a bot-side reminder.

    Args:
        reminder_id: The reminder to update.
        **kwargs: Column-value pairs (e.g. ``remind_at="..."``,
            ``title="..."``).
    """
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [reminder_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE reminders SET {fields} WHERE id = ?", values)
        conn.commit()


def get_active_reminders(limit=20):
    """Return all active reminders ordered by trigger time (nearest first).

    Args:
        limit: Maximum number to return (default 20).

    Returns:
        list[sqlite3.Row]: Active reminders.
    """
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM reminders WHERE active = 1 ORDER BY remind_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()


def get_reminders_by_period(start_date, end_date):
    """Return active reminders whose trigger time falls within a date range.

    Args:
        start_date: Inclusive start date.
        end_date: Inclusive end date.

    Returns:
        list[sqlite3.Row]: Active reminders in the range.
    """
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM reminders
               WHERE active = 1
                 AND remind_at >= ? AND remind_at < ?
               ORDER BY remind_at ASC""",
            (start_date.isoformat(), (end_date + timedelta(days=1)).isoformat()),
        ).fetchall()


def complete_app_reminder(reminder_id, sent_at, next_remind_at=None):
    """Mark a bot-side reminder as dispatched and optionally reschedule it.

    For one-shot reminders, ``active`` is set to 0. For recurring reminders,
    the ``remind_at`` is advanced to the next occurrence without deactivating.

    Args:
        reminder_id: The dispatched reminder.
        sent_at: ISO datetime string for when it was sent.
        next_remind_at: If provided (recurring), the next trigger time. The
            reminder stays active. If ``None`` (one-shot), the reminder is
            deactivated.
    """
    with get_conn() as conn:
        if next_remind_at:
            conn.execute(
                """UPDATE reminders
                   SET remind_at = ?, last_sent_at = ?
                   WHERE id = ?""",
                (next_remind_at, sent_at, reminder_id),
            )
        else:
            conn.execute(
                """UPDATE reminders
                   SET active = 0, last_sent_at = ?
                   WHERE id = ?""",
                (sent_at, reminder_id),
            )
        conn.commit()