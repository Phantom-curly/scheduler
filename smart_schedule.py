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
Smart scheduling engine — free slot detection, task scoring, greedy planning, and
AI-powered recommendations.

Provides three layers of scheduling logic:

1. **Free slot finder**: Queries Google Calendar for existing events, builds busy
   blocks, and computes free windows within waking hours (07:00–23:00). Sleep
   (23:00–07:00) is hard-blocked; meal times are soft-blocked.

2. **Task scoring and greedy planner**: Scores each task-slot pair on deadline
   pressure, category/time-of-day fit, energy level, and priority. Assigns tasks
   greedily, supporting split placement for large tasks.

3. **AI recommendation**: Optional Gemini 2.5 Flash Lite via OpenRouter for
   single-slot suggestions, weekly plans, daily recommendations, and weekly
   reviews.
"""

import os, logging, re, json
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import pytz
import requests

import calendar_client

logger             = logging.getLogger(__name__)
TIMEZONE           = os.getenv("TIMEZONE", "Asia/Seoul")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL              = "google/gemini-2.5-flash-lite"
API_URL            = "https://openrouter.ai/api/v1/chat/completions"

# Hard-blocked: sleep window (no tasks assigned during these hours)
SLEEP_START  = (23, 0)
SLEEP_END    = (7,  0)

# Soft-blocked: meal times (avoided when possible but not strictly prohibited)
SOFT_BLOCKS  = [          # (start_h, start_m, end_h, end_m, label)
    (7, 30,  8, 30,  "breakfast"),
    (12, 0, 13,  0,  "lunch"),
    (18,30, 19, 30,  "dinner"),
]


def _is_sleep(dt: datetime) -> bool:
    """Check whether *dt* falls within the hard-blocked sleep window (23:00–07:00).

    Args:
        dt: A timezone-aware datetime.

    Returns:
        bool: ``True`` if the time is between 23:00 and 07:00.
    """
    h = dt.hour
    return h >= SLEEP_START[0] or h < SLEEP_END[0]


def _is_soft_blocked(dt: datetime) -> bool:
    """Check whether *dt* falls within any of the soft-blocked meal windows.

    Args:
        dt: A timezone-aware datetime.

    Returns:
        bool: ``True`` if the time falls within a meal window.
    """
    for sh, sm, eh, em, _ in SOFT_BLOCKS:
        start = dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end   = dt.replace(hour=eh, minute=em, second=0, microsecond=0)
        if start <= dt < end:
            return True
    return False


# ── Free slot finder ──────────────────────────────────────────────────────────


def get_free_slots(
    days_ahead:       int  = 5,
    min_duration_min: int  = 30,
    respect_soft:     bool = True,
) -> List[Dict]:
    """Find free calendar slots within the next *days_ahead* days.

    Queries Google Calendar for existing events, builds busy blocks, then
    computes free windows within waking hours (07:00–23:00). Sleep is always
    excluded; meal times are optionally excluded based on ``respect_soft``.

    Args:
        days_ahead: How many days to check from today (default 5).
        min_duration_min: Minimum slot duration in minutes (default 30).
            Slots shorter than this are discarded.
        respect_soft: If ``True`` (default), meal-time windows are removed
            from the free slots.

    Returns:
        List[Dict]: Free slots, each with keys ``start``, ``end``,
            ``duration_minutes``. Sorted chronologically, capped at 25 slots.
    """
    tz       = pytz.timezone(TIMEZONE)
    now      = datetime.now(tz)

    try:
        events = calendar_client.list_upcoming_events(days=days_ahead)
    except Exception as exc:
        logger.warning(f"smart_schedule fetch failed: {exc}")
        return []

    # Build busy blocks from calendar
    busy: List[Tuple[datetime, datetime]] = []
    for e in events:
        start = calendar_client.parse_event_start(e)
        if not start:
            continue
        if start.tzinfo is None:
            start = tz.localize(start)
        end_raw = e.get("end", {})
        end_str = end_raw.get("dateTime") or end_raw.get("date")
        try:
            end = datetime.fromisoformat(end_str)
            if end.tzinfo is None:
                end = tz.localize(end)
            else:
                end = end.astimezone(tz)
        except Exception:
            end = start + timedelta(hours=1)
        busy.append((start, end))

    busy.sort(key=lambda x: x[0])

    free_slots = []
    for day_offset in range(days_ahead):
        day = (now + timedelta(days=day_offset)).date()

        # Build waking window for this day (SLEEP_END → SLEEP_START)
        wake_start = tz.localize(datetime(day.year, day.month, day.day, SLEEP_END[0],  SLEEP_END[1]))
        wake_end   = tz.localize(datetime(day.year, day.month, day.day, SLEEP_START[0], SLEEP_START[1]))

        # Don't go before now (add 15 min buffer to avoid immediate scheduling)
        cursor = max(wake_start, now + timedelta(minutes=15))
        if cursor >= wake_end:
            continue

        day_busy = [(s, e) for s, e in busy if s.date() == day]

        def add_slot(s, e):
            if respect_soft:
                # Trim soft-blocked meal windows from the slot edges
                for sh, sm, eh, em, _ in SOFT_BLOCKS:
                    sb = s.replace(hour=sh, minute=sm, second=0, microsecond=0)
                    se = s.replace(hour=eh, minute=em, second=0, microsecond=0)
                    if sb <= s < se:
                        s = se
                    if sb < e <= se:
                        e = sb
                if s >= e:
                    return
            dur = int((e - s).total_seconds() / 60)
            if dur >= min_duration_min:
                free_slots.append({"start": s, "end": e, "duration_minutes": dur})

        # Walk through busy blocks chronologically, adding gaps as free slots
        for busy_start, busy_end in day_busy:
            if cursor < busy_start:
                add_slot(cursor, busy_start)
            cursor = max(cursor, busy_end)

        # Add remaining time after the last busy block until sleep
        add_slot(cursor, wake_end)

    return free_slots[:25]


def format_slots_for_display(slots: List[Dict], limit: int = 5) -> str:
    """Format free slots into a human-readable string for Telegram messages.

    Args:
        slots: List of free slot dicts (from ``get_free_slots()``).
        limit: Maximum number of slots to display (default 5).

    Returns:
        str: Newline-separated slot descriptions like
            ``"1. Thu Jun 11, 2:00 PM – 3:30 PM (90 min free)"``.
    """
    lines = []
    for i, s in enumerate(slots[:limit]):
        day   = s["start"].strftime("%a %b %d")
        t_s   = s["start"].strftime("%I:%M %p").lstrip("0")
        t_e   = s["end"].strftime("%I:%M %p").lstrip("0")
        dur   = s["duration_minutes"]
        lines.append(f"  {i+1}. {day}, {t_s} – {t_e} ({dur} min free)")
    return "\n".join(lines)


def _task_minutes(task: Dict, fallback: int = 60) -> int:
    """Extract the estimated duration of a task in minutes.

    Args:
        task: A task dict (from ``db.get_*``).
        fallback: Default duration if ``estimated_minutes`` is missing or
            invalid (default 60).

    Returns:
        int: Duration in minutes.
    """
    try:
        return int(task.get("estimated_minutes") or fallback)
    except Exception:
        return fallback


def _deadline_pressure(task: Dict, slot_start: datetime) -> int:
    """Compute a deadline-pressure score for a task relative to a slot.

    The score increases as the deadline approaches:
        - Overdue: 90
        - Due today: 70
        - Due tomorrow: 45
        - Due within 3 days: 25
        - Due later: 5
        - No deadline: 0

    Args:
        task: A task dict with an optional ``deadline`` field (ISO date).
        slot_start: The datetime of the proposed slot.

    Returns:
        int: A score from 0 (no pressure) to 90 (critical).
    """
    deadline = task.get("deadline")
    if not deadline:
        return 0
    try:
        days_left = (datetime.fromisoformat(deadline).date() - slot_start.date()).days
    except Exception:
        return 0
    if days_left < 0:
        return 90
    if days_left == 0:
        return 70
    if days_left == 1:
        return 45
    if days_left <= 3:
        return 25
    return 5


def _slot_fit_score(task: Dict, slot: Dict) -> int:
    """Score how well a task fits into a given free slot.

    Evaluates multiple dimensions:
        - **Duration**: Slot must be at least as long as the task's estimated
          minutes (otherwise returns -10,000, which eliminates the slot).
        - **Deadline pressure**: Urgent tasks score higher (0–90).
        - **Priority**: High-priority tasks get +25, medium +10, low +0.
        - **Slot waste**: Penalises slots that are much larger than the task.
        - **Category/time-of-day fit**: Focus work → mornings, fitness →
          morning/evening, meetings → business hours, errands → midday.
        - **Energy level**: High-energy tasks penalised late at night.
        - **Earliest start**: If the slot starts before ``earliest_start``,
          returns -10,000.

    Args:
        task: A task dict with keys ``estimated_minutes``, ``category``,
            ``energy``, ``priority``, ``earliest_start``.
        slot: A free slot dict with keys ``start``, ``duration_minutes``.

    Returns:
        int: A score where higher is better. Negative values indicate a poor
            or invalid fit; -10,000 means the slot is incompatible.
    """
    minutes = _task_minutes(task)
    if slot["duration_minutes"] < minutes:
        return -10_000

    start    = slot["start"]
    category = (task.get("category") or "general").lower()
    energy   = (task.get("energy") or "medium").lower()
    priority = (task.get("priority") or "medium").lower()

    score = 100
    score += _deadline_pressure(task, start)
    score += {"high": 25, "medium": 10, "low": 0}.get(priority, 10)

    # Penalise wasted slot space (every 30 min of excess reduces score)
    leftover = slot["duration_minutes"] - minutes
    score -= min(leftover // 30, 8)

    hour = start.hour
    # Category/time-of-day bonuses
    if category == "focus":
        if 8 <= hour <= 13:
            score += 25
        elif hour >= 20:
            score -= 20
    elif category == "fitness":
        if 7 <= hour <= 10 or 18 <= hour <= 21:
            score += 25
        elif 11 <= hour <= 16:
            score -= 10
    elif category == "meeting":
        if 9 <= hour <= 17:
            score += 20
    elif category in ("errand", "home"):
        if 10 <= hour <= 18:
            score += 10

    # Energy-level adjustments
    if energy == "high" and hour >= 20:
        score -= 15
    if energy == "low" and 8 <= hour <= 11:
        score -= 5

    # Earliest-start constraint
    earliest = task.get("earliest_start")
    if earliest:
        try:
            if start < datetime.fromisoformat(earliest):
                return -10_000
        except Exception:
            pass

    return score


def best_slots_for_task(task: Dict, free_slots: List[Dict], limit: int = 3) -> List[Dict]:
    """Return the best-fitting free slots for a single task, ranked by fit score.

    Args:
        task: A task dict (see ``_slot_fit_score`` for required keys).
        free_slots: List of free slot dicts to evaluate.
        limit: Maximum number of results (default 3).

    Returns:
        List[Dict]: The top ``limit`` slots, each augmented with a ``score``
            field.
    """
    ranked = []
    for slot in free_slots:
        score = _slot_fit_score(task, slot)
        if score > -10_000:
            ranked.append({**slot, "score": score})
    ranked.sort(key=lambda s: s["score"], reverse=True)
    return ranked[:limit]


def build_task_plan(tasks: List[Dict], free_slots: List[Dict], limit: int = 6) -> List[Dict]:
    """Greedily assign unscheduled tasks to the best-fitting free slots.

    Tasks are sorted by deadline → priority → estimated duration. Each task is
    assigned to its best available slot. If a task cannot fit in a single slot
    and is tagged as splittable (or is >= 120 min), it is split across multiple
    consecutive slots.

    Args:
        tasks: List of unscheduled task dicts.
        free_slots: List of free slot dicts from ``get_free_slots()``.
        limit: Maximum number of planned items to return (default 6).

    Returns:
        List[Dict]: Planned assignments, each with keys ``task``, ``start``,
            ``end``, ``score``, and optionally ``chunk`` / ``total_chunks``
            for split tasks.
    """
    planned = []
    used = set()

    def task_sort_key(t):
        priority = {"high": 0, "medium": 1, "low": 2}.get((t.get("priority") or "medium").lower(), 1)
        deadline = t.get("deadline") or "9999-12-31"
        return (deadline, priority, _task_minutes(t))

    for task in sorted(tasks, key=task_sort_key):
        task_minutes = _task_minutes(task)
        # A task is splittable if explicitly tagged or if its duration >= 2 hours
        splittable = task.get("splittable", False) or (task_minutes >= 120)
        remaining = task_minutes
        chunks = []

        # First: try to fit the entire task in one slot
        candidates = best_slots_for_task(task, [s for i, s in enumerate(free_slots) if i not in used], limit=1)
        if candidates:
            slot = candidates[0]
            if slot["duration_minutes"] >= remaining:
                original_idx = free_slots.index(next(s for s in free_slots if s["start"] == slot["start"] and s["end"] == slot["end"]))
                used.add(original_idx)
                planned.append({
                    "task": task,
                    "start": slot["start"],
                    "end": slot["start"] + timedelta(minutes=remaining),
                    "slot_end": slot["end"],
                    "score": slot["score"],
                    "chunk": None,
                })
                if len(planned) >= limit:
                    break
                continue

        # Second: try splitting across multiple slots
        if splittable:
            available_slots = [s for i, s in enumerate(free_slots) if i not in used]
            available_slots.sort(key=lambda s: s["start"])
            for slot in available_slots:
                if remaining <= 0:
                    break
                chunk_duration = min(remaining, slot["duration_minutes"])
                if chunk_duration < 30:
                    continue  # Skip slots too small even for a chunk
                original_idx = free_slots.index(next(s for s in free_slots if s["start"] == slot["start"] and s["end"] == slot["end"]))
                used.add(original_idx)
                chunks.append({
                    "task": task,
                    "start": slot["start"],
                    "end": slot["start"] + timedelta(minutes=chunk_duration),
                    "score": 50,
                    "chunk": len(chunks) + 1,
                })
                remaining -= chunk_duration
                if len(planned) + len(chunks) >= limit:
                    break

            # Only add chunks if we were able to fit the entire task
            if chunks and remaining <= 0:
                for ch in chunks:
                    ch["total_chunks"] = len(chunks)
                    planned.append(ch)
                if len(planned) >= limit:
                    break

    return planned


def format_task_plan(plan: List[Dict]) -> str:
    """Format a task plan into a human-readable Telegram message.

    Args:
        plan: A list of planned assignments from ``build_task_plan()``.

    Returns:
        str: Newline-separated plan lines, e.g.
            ``"1. Thu Jun 11, 2:00 PM - 4:00 PM: Finish report (120 min, due Fri Jun 19)"``.
    """
    lines = []
    for i, item in enumerate(plan, start=1):
        task = item["task"]
        day  = item["start"].strftime("%a %b %d")
        t_s  = item["start"].strftime("%I:%M %p").lstrip("0")
        t_e  = item["end"].strftime("%I:%M %p").lstrip("0")
        chunk = item.get("chunk")
        total = item.get("total_chunks")
        chunk_tag = f" (part {chunk}/{total})" if chunk and total else ""
        due  = f", due {task['deadline']}" if task.get("deadline") else ""
        dur = int((item["end"] - item["start"]).total_seconds() / 60)
        lines.append(f"{i}. {day}, {t_s} - {t_e}: {task['title']}{chunk_tag} ({dur} min{due})")
    return "\n".join(lines)


# ── AI slot picker ────────────────────────────────────────────────────────────


def suggest_slot(title: str, duration_minutes: int, free_slots: List[Dict]) -> Optional[Dict]:
    """Use Gemini 2.5 Flash Lite to pick the best free slot from a list.

    The LLM is prompted with the task title, required duration, and the first
    8 available free slots. It is instructed to follow category/time-of-day
    heuristics (e.g., study → morning, gym → morning or evening).

    Args:
        title: Task title.
        duration_minutes: Required duration in minutes.
        free_slots: List of free slot dicts.

    Returns:
        Optional[Dict]: The chosen slot dict, or ``None`` if the LLM call
            fails or returns an invalid index.
    """
    if not OPENROUTER_API_KEY or not free_slots:
        return None

    slots_text = "\n".join(
        f"{i+1}. {s['start'].strftime('%A %b %d %H:%M')} ({s['duration_minutes']} min free)"
        for i, s in enumerate(free_slots[:8])
    )

    prompt = (
        f"Task: {title} (needs {duration_minutes} min)\n"
        f"Free slots:\n{slots_text}\n\n"
        "Pick best slot number. Rules:\n"
        "- Study/focus/reading/work → morning or early afternoon\n"
        "- Gym/run/exercise/sport → morning (7-10am) or evening (6-9pm)\n"
        "- Meetings/calls → business hours\n"
        "- Creative/writing → morning\n"
        "- Slot must be long enough\n"
        "Reply ONLY with the number."
    )

    try:
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "max_tokens": 5, "temperature": 0,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=6,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        n   = int(re.search(r"\d+", raw).group())
        if 1 <= n <= len(free_slots):
            return free_slots[n - 1]
    except Exception as exc:
        logger.warning(f"suggest_slot: {exc}")
    return None


def build_suggestion_message(
    title: str,
    duration_minutes: int,
    free_slots: List[Dict],
) -> Tuple[str, Optional[Dict]]:
    """Build a Telegram message suggesting a free slot for a task.

    Tries Gemini first via ``suggest_slot()``. If that fails or no free slots
    exist, falls back to listing all available slots and asking the user to
    pick.

    Args:
        title: Task title.
        duration_minutes: Required duration.
        free_slots: List of free slot dicts.

    Returns:
        Tuple[str, Optional[Dict]]: A (message_text, best_slot) pair. The slot
            is ``None`` if no suggestion could be made.
    """
    if not free_slots:
        return (
            f"📅 Scheduling: *{title}*\n\nYour calendar looks packed this week!\n"
            "When would you like to squeeze it in? (e.g. `Thursday 9pm for 1 hour`)", None
        )

    best = suggest_slot(title, duration_minutes, free_slots)

    if best:
        start   = best["start"]
        end     = start + timedelta(minutes=duration_minutes)
        day_str = start.strftime("%A %b %d")
        t_s     = start.strftime("%I:%M %p").lstrip("0")
        t_e     = end.strftime("%I:%M %p").lstrip("0")
        alts    = format_slots_for_display([s for s in free_slots if s != best], limit=3)
        msg = (
            f"📅 *{title}*\n\n"
            f"✨ Best slot: *{day_str}, {t_s} – {t_e}*\n\n"
            f"Reply `yes` to confirm, `no` for other options, or type a different time."
        )
        if alts:
            msg += f"\n\n_Other free slots:_\n{alts}"
        return msg, best

    slots_display = format_slots_for_display(free_slots, limit=5)
    return (
        f"📅 *{title}* — your free slots:\n\n{slots_display}\n\n"
        "Reply with a number or type a specific time.", None
    )


# ── Weekly/daily AI recommendation engine ────────────────────────────────────


def generate_weekly_plan(tasks: list, habits: list, free_slots: List[Dict]) -> str:
    """Generate a short, intelligent weekly plan recommendation via Gemini.

    Called in the Sunday planning message. The LLM receives the list of tasks
    due next week, weekly habits, and free slots, and returns a bullet-point
    plan assigning each item to specific time slots.

    Args:
        tasks: List of task dicts with ``title``, ``deadline``, ``priority``.
        habits: List of habit dicts with ``title``, ``frequency``, ``count``.
        free_slots: List of free slot dicts.

    Returns:
        str: The AI-generated plan text, or an empty string if the API key is
            not configured or the call fails.
    """
    if not OPENROUTER_API_KEY:
        return ""

    today     = datetime.now().strftime("%Y-%m-%d %A")
    slots_txt = "\n".join(
        f"- {s['start'].strftime('%A %b %d %H:%M')} ({s['duration_minutes']} min)"
        for s in free_slots[:15]
    ) or "No free slots found."

    tasks_txt = "\n".join(
        f"- {t['title']} due {t['deadline'] or '?'} [{t.get('priority','medium')}]"
        for t in tasks
    ) or "No tasks."

    habits_txt = "\n".join(
        f"- {h['title']} {h['count']}x/week" if h['frequency'] == 'weekly'
        else f"- {h['title']} daily"
        for h in habits
    ) or "No habits."

    prompt = (
        f"Today: {today}\n"
        f"Tasks:\n{tasks_txt}\n\n"
        f"Weekly habits:\n{habits_txt}\n\n"
        f"Free slots this week:\n{slots_txt}\n\n"
        "Write a SHORT weekly plan recommendation (max 8 lines). Be specific:\n"
        "- Assign each task/habit to a specific slot\n"
        "- Keep sleep (11pm-7am) and meals free\n"
        "- Group similar tasks (study sessions together)\n"
        "- Respect deadlines — urgent tasks get early slots\n"
        "- Use bullet points, be conversational\n"
        "Format: emoji + day + time + task"
    )

    try:
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "max_tokens": 300, "temperature": 0.4,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=12,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning(f"generate_weekly_plan: {exc}")
        return ""


def generate_daily_recommendations(
    todays_events: list,
    todays_tasks:  list,
    unscheduled:   list,
    habits:        list,
    free_slots:    List[Dict],
) -> str:
    """Generate a morning briefing recommendation for fitting unscheduled items
    into today's gaps.

    Called in the morning briefing job. The LLM receives today's calendar
    events, free gaps, unscheduled tasks, and daily habits, and suggests a
    schedule.

    Args:
        todays_events: List of Google Calendar event dicts for today.
        todays_tasks: List of task dicts due today.
        unscheduled: List of unscheduled task dicts.
        habits: List of daily habit dicts.
        free_slots: List of today's free slot dicts.

    Returns:
        str: AI-generated recommendation text, or empty string on failure.
    """
    if not OPENROUTER_API_KEY or (not unscheduled and not habits):
        return ""

    today     = datetime.now().strftime("%Y-%m-%d %A")
    slots_txt = "\n".join(
        f"- {s['start'].strftime('%H:%M')}–{s['end'].strftime('%H:%M')} ({s['duration_minutes']} min)"
        for s in free_slots[:8]
    ) or "No gaps found."

    events_txt = "\n".join(
        f"- {e.get('summary','?')} at {e.get('start',{}).get('dateTime','?')}"
        for e in todays_events[:8]
    ) or "Empty calendar."

    unsch_txt = "\n".join(
        f"- {t['title']} [{t.get('priority','medium')} priority]"
        for t in unscheduled[:6]
    ) or "None."

    habits_txt = "\n".join(f"- {h['title']}" for h in habits[:5]) or "None."

    prompt = (
        f"Today: {today}\n"
        f"Scheduled today:\n{events_txt}\n\n"
        f"Free gaps today:\n{slots_txt}\n\n"
        f"Unscheduled tasks:\n{unsch_txt}\n\n"
        f"Daily habits to fit in:\n{habits_txt}\n\n"
        "Give a SHORT recommendation (max 5 lines) for fitting unscheduled items into today's gaps.\n"
        "Be specific with times. Prioritize urgent tasks. Keep it actionable.\n"
        "Format: emoji + time + task"
    )

    try:
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "max_tokens": 180, "temperature": 0.3,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning(f"generate_daily_recommendations: {exc}")
        return ""


# ── Weekly review ─────────────────────────────────────────────────────────────


def generate_weekly_review(completed: list, planned: list) -> str:
    """Generate a short, encouraging weekly review using Gemini.

    Called by the Sunday weekly review job. The LLM receives the list of
    completed and missed tasks, computes a completion rate, and returns a
    motivational summary.

    Args:
        completed: Tasks marked 'done' this week.
        planned: All tasks that had deadlines this week.

    Returns:
        str: AI-generated review text, or empty string on failure.
    """
    if not OPENROUTER_API_KEY:
        return ""

    total     = len(planned)
    done      = len(completed)
    missed    = [t for t in planned if t["status"] != "done"]
    rate      = int((done / total * 100)) if total else 0

    completed_txt = "\n".join(f"- ✅ {t['title']}" for t in completed) or "None."
    missed_txt    = "\n".join(f"- ❌ {t['title']} (due {t['deadline'] or '?'})" for t in missed) or "None."

    today = datetime.now().strftime("%Y-%m-%d %A")

    prompt = (
        f"Weekly review for week ending {today}.\n"
        f"Completion rate: {done}/{total} tasks ({rate}%)\n\n"
        f"Completed:\n{completed_txt}\n\n"
        f"Missed/incomplete:\n{missed_txt}\n\n"
        "Write a SHORT encouraging weekly review (3-4 lines max):\n"
        "- Acknowledge what was done\n"
        "- Note what was missed without judgment\n"
        "- One actionable suggestion for next week\n"
        "Keep it warm, motivating, and brief."
    )

    try:
        resp = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "max_tokens": 150, "temperature": 0.5,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning(f"generate_weekly_review: {exc}")
        return ""