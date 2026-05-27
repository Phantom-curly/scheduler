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
                frequency  TEXT    NOT NULL,  -- 'daily' | 'weekly'
                count      INTEGER DEFAULT 1, -- how many times per week (weekly only)
                notes      TEXT,              -- e.g. '30 min', 'x2'
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


# ── Tasks ──────────────────────────────────────────────────────────────────────

def add_task(title, deadline=None, priority="medium", notes=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, deadline, priority, notes) VALUES (?, ?, ?, ?)",
            (title, deadline, priority, notes),
        )
        conn.commit()
        return cur.lastrowid


def get_task(task_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def get_all_tasks(status=None):
    with get_conn() as conn:
        if status:
            return conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY deadline ASC, created_at ASC",
                (status,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM tasks ORDER BY deadline ASC, created_at ASC"
        ).fetchall()


def get_tasks_this_week():
    today = datetime.now().date()
    start = today - timedelta(days=today.weekday())
    end   = start + timedelta(days=6)
    return get_tasks_by_period(start, end)


def get_tasks_by_period(start_date, end_date):
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE deadline BETWEEN ? AND ?
                 AND status != 'done'
               ORDER BY deadline ASC""",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()


def search_tasks(query):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE title LIKE ? AND status != 'done' ORDER BY deadline",
            (f"%{query}%",),
        ).fetchall()


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


# ── Sent reminders (for calendar event dedup) ──────────────────────────────────

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