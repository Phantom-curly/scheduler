import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "planner.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(conn, table, column, definition):
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
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
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def get_all_tasks(status=None):
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
    """All non-done tasks sorted by closest deadline first, no-deadline last."""
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
    """Unscheduled, non-done tasks sorted by deadline (nulls last)."""
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
    import pytz
    tz    = pytz.timezone(os.getenv("TIMEZONE", "Asia/Seoul"))
    today = datetime.now(tz).date()
    start = today - timedelta(days=today.weekday())
    end   = start + timedelta(days=6)
    return get_tasks_by_period(start, end)


def get_tasks_by_period(start_date, end_date):
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
    """Tasks past their deadline that are still pending."""
    from datetime import datetime
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
    """Tasks past their deadline, unscheduled, not done — for overdue lifecycle."""
    from datetime import datetime
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
    """Tasks completed this week (Mon–Sun)."""
    from datetime import datetime, timedelta
    today = datetime.now().date()
    start = today - timedelta(days=today.weekday())  # Monday
    end   = start + timedelta(days=6)
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE status = 'done'
                 AND deadline BETWEEN ? AND ?
               ORDER BY deadline ASC""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()


def get_planned_this_week():
    """All tasks (any status) that had deadlines this week."""
    from datetime import datetime, timedelta
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
    """Tasks due within days_ahead that are not scheduled and not done."""
    from datetime import datetime, timedelta
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
    """Tasks without a calendar event."""
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
    """Unscheduled, unfinished tasks ordered by urgency and priority."""
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
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE title LIKE ? AND status != 'done' ORDER BY deadline",
            (f"%{query}%",),
        ).fetchall()


# ── Tasks: Update ──────────────────────────────────────────────────────────────

def update_task(task_id, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [task_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE tasks SET {fields} WHERE id = ?", values)
        conn.commit()


def update_task_schedule_by_event(event_id, scheduled_start, scheduled_end):
    with get_conn() as conn:
        conn.execute(
            """UPDATE tasks
               SET scheduled_start = ?, scheduled_end = ?
               WHERE calendar_event_id = ?""",
            (scheduled_start, scheduled_end, event_id),
        )
        conn.commit()


def complete_task(task_id):
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'done', completed_at = datetime('now') WHERE id = ?", (task_id,))
        conn.commit()


def uncomplete_task(task_id):
    """Revert a completed task back to pending status."""
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'pending', completed_at = NULL WHERE id = ?", (task_id,))
        conn.commit()


# ── Tasks: Delete ──────────────────────────────────────────────────────────────

def delete_task(task_id):
    with get_conn() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return task


# ── Habits ─────────────────────────────────────────────────────────────────────

def add_habit(title, frequency, count=1, notes=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO habits (title, frequency, count, notes) VALUES (?, ?, ?, ?)",
            (title, frequency, count, notes),
        )
        conn.commit()
        return cur.lastrowid


def get_habits(frequency=None, active_only=True):
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
    with get_conn() as conn:
        return conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()


def delete_habit(habit_id):
    with get_conn() as conn:
        habit = conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        conn.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
        conn.commit()
        return habit


def toggle_habit(habit_id, active):
    with get_conn() as conn:
        conn.execute("UPDATE habits SET active = ? WHERE id = ?", (1 if active else 0, habit_id))
        conn.commit()


# ── Sent reminders ─────────────────────────────────────────────────────────────

def reminder_already_sent(event_key):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM sent_reminders WHERE event_key = ?", (event_key,)
        ).fetchone()
        return row is not None


def mark_reminder_sent(event_key):
    with get_conn() as conn:
        try:
            conn.execute("INSERT INTO sent_reminders (event_key) VALUES (?)", (event_key,))
            conn.commit()
        except Exception:
            pass


# ── App reminders (not calendar blocks) ───────────────────────────────────────

def add_reminder(title, remind_at, recurrence_rrule=None):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO reminders (title, remind_at, recurrence_rrule)
               VALUES (?, ?, ?)""",
            (title, remind_at, recurrence_rrule),
        )
        conn.commit()
        return cur.lastrowid


def get_due_app_reminders(now_iso):
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM reminders
               WHERE active = 1
                 AND remind_at <= ?
               ORDER BY remind_at ASC""",
            (now_iso,),
        ).fetchall()


def get_reminder(reminder_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()


def delete_reminder(reminder_id):
    with get_conn() as conn:
        reminder = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.commit()
        return reminder


def update_reminder(reminder_id, **kwargs):
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [reminder_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE reminders SET {fields} WHERE id = ?", values)
        conn.commit()


def complete_app_reminder(reminder_id, sent_at, next_remind_at=None):
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