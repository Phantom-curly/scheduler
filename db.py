import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "planner.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                title             TEXT    NOT NULL,
                deadline          TEXT,
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
        conn.commit()


# ── Tasks: Create ──────────────────────────────────────────────────────────────

def add_task(title, deadline=None, priority="medium", notes=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, deadline, priority, notes) VALUES (?, ?, ?, ?)",
            (title, deadline, priority, notes),
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


def get_urgent_tasks(days_ahead=2):
    """Tasks due within days_ahead that are not scheduled."""
    import pytz
    tz      = pytz.timezone(os.getenv("TIMEZONE", "Asia/Seoul"))
    today   = datetime.now(tz).date()
    cutoff  = today + timedelta(days=days_ahead)
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE deadline <= ?
                 AND deadline >= ?
                 AND status = 'pending'
                 AND calendar_event_id IS NULL
               ORDER BY deadline ASC""",
            (cutoff.isoformat(), today.isoformat()),
        ).fetchall()


def get_completed_this_week():
    """Tasks completed during this calendar week (Mon–Sun)."""
    from datetime import datetime, timedelta
    today = datetime.now().date()
    start = today - timedelta(days=today.weekday())
    end   = start + timedelta(days=6)
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE status = 'done'
                 AND created_at >= ?
               ORDER BY created_at DESC""",
            (start.isoformat(),),
        ).fetchall()


def get_planned_this_week():
    """All tasks that had a deadline this week (done or not)."""
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


def complete_task(task_id):
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
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