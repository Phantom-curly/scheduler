"""
NLP helpers — intent detection, datetime/duration/reminder/recurrence extraction.
"""

import re
import dateparser
from datetime import datetime
from typing import Optional, Dict, Any

# ── Intent patterns (order matters — more specific first) ──────────────────────

_INTENTS = {
    "habit_delete": [
        r"\b(delete|remove)\b.{0,20}\bhabit\b",
    ],
    "habit_list": [
        r"\b(show|list|my|view)\b.{0,20}\bhabit[s]?\b",
        r"^habits?\s*$",
    ],
    "habit_add": [
        r"\badd\s+(daily|weekly)\s+habit\b",
        r"\b(daily|weekly)\s+habit\s*:",
        r"\bevery\s+(day|week|morning|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b.{0,60}\b(remind|remember|habit)\b",
        r"\bhabit\s*:\s*.+",
    ],
    "schedule_direct": [
        r"\bschedule\b.{1,80}\b(on|at|this|next|every)\b",
        r"\bschedule\b.{1,40}\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b",
        r"\b(block|put|add)\b.{1,40}\b(on|at|in|to)\b.{1,30}\b(calendar|cal)\b",
    ],
    "schedule": [
        r"\bschedule\b\s*[\d\s,and]+$",
        r"\bschedule\s+\d",
    ],
    "add": [
        r"\b(add|create|new|track|save|log|set)\b",
        r"\b(remind me (about|to))\b",
    ],
    "list": [
        r"\b(list|show|what|display|see|view|check)\b.{0,30}\b(tasks?|todo|deadlines?|week|today|upcoming|schedule)\b",
        r"\b(tasks?|todo|deadlines?)\b.{0,20}\b(do i have|for (this|next|the)|today|week)\b",
        r"^(tasks?|todo|deadlines?)\s*$",
        r"\bwhat.{0,20}(week|today|upcoming|due)\b",
    ],
    "complete": [
        r"\b(done|complete[d]?|finished?|mark.{0,10}done|close[d]?|checked? off)\b",
    ],
    "delete": [
        r"\b(delete|remove|cancel|drop|erase|get rid of)\b",
    ],
    "update": [
        r"\b(update|edit|change|move|reschedule|rename|modify|push back|extend)\b",
    ],
    "help": [
        r"\b(help|commands?|what can you|how do i)\b",
    ],
}

_DEADLINE_HINTS = re.compile(
    r"\b(by|due|deadline|before|until)\s+\w", re.IGNORECASE
)

# ── Recurrence ─────────────────────────────────────────────────────────────────

_DAY_MAP = {
    "monday": "MO", "tuesday": "TU", "wednesday": "WE",
    "thursday": "TH", "friday": "FR", "saturday": "SA", "sunday": "SU",
    "mon": "MO", "tue": "TU", "wed": "WE", "thu": "TH",
    "fri": "FR", "sat": "SA", "sun": "SU",
}

_RECURRENCE_RE = re.compile(
    r"\bevery\s+"
    r"((?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|wed|thu|fri|sat|sun)(?:\s*(?:and|,)\s*)?)+|"
    r"day|weekday|weekend|week|month|"
    r"last\s+day\s+of\s+(?:the\s+)?month|"
    r"(\d+)(?:st|nd|rd|th)\s+of\s+(?:the\s+)?month)",
    re.IGNORECASE,
)

# ── Habit count ────────────────────────────────────────────────────────────────

_COUNT_RE = re.compile(
    r"(\d+)\s*(?:times?|sessions?|x)\s*(?:a\s+|per\s+)?(?:week|day)?",
    re.IGNORECASE,
)

# ── Reminder / duration ────────────────────────────────────────────────────────

_REMINDER_RE = re.compile(
    r"remind\s+me\s+(\d+)\s*(hour[s]?|hr[s]?|minute[s]?|min[s]?)\s+before",
    re.IGNORECASE,
)

_DURATION_RE = re.compile(
    r"\bfor\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)

# ── Strip patterns for title extraction ───────────────────────────────────────

_STRIP_INTENTS_RE = re.compile(
    r"\b(add|create|new|track|save|log|set|remind me (about|to)?|task|todo|"
    r"a |an |the |schedule|daily|weekly|habit[s]?)\b",
    re.IGNORECASE,
)
_STRIP_DATES_RE = re.compile(
    r"(\bby\b|\bdue\b|\bon\b|\bbefore\b|\bnext\b|\bthis\b|\btomorrow\b|\btoday\b)"
    r"[\s\w,]*|"
    r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|"
    r"\bfor\s+\d+\s*(?:hours?|hrs?|minutes?|mins?)\b|"
    r"\b\d{1,2}[\/\-]\d{1,2}(?:[\/\-]\d{2,4})?\b|"
    r"remind\s+me\s+\d+\s*(?:hours?|minutes?|mins?|hrs?)\s+before",
    re.IGNORECASE,
)
_STRIP_COUNT_RE = re.compile(
    r"\d+\s*(?:times?|sessions?|x)\s*(?:a\s+|per\s+)?(?:week|day)?",
    re.IGNORECASE,
)


# ── Public API ─────────────────────────────────────────────────────────────────

def detect_intent(text: str) -> str:
    t = text.lower().strip()
    for intent, patterns in _INTENTS.items():
        for pat in patterns:
            if re.search(pat, t):
                return intent
    if _DEADLINE_HINTS.search(t):
        return "add"
    if dateparser.parse(text, settings={"PREFER_DATES_FROM": "future"}):
        return "add"
    return "unknown"


def extract_datetime(text: str) -> Optional[datetime]:
    return dateparser.parse(text, settings={
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": False,
        "PREFER_DAY_OF_MONTH": "first",
    })


def extract_duration(text: str) -> int:
    m = _DURATION_RE.search(text)
    if m:
        amount = float(m.group(1))
        unit   = m.group(2).lower()
        return int(amount * 60) if ("hour" in unit or "hr" in unit) else int(amount)
    return 60


def extract_reminder_minutes(text: str) -> int:
    m = _REMINDER_RE.search(text)
    if m:
        amount = int(m.group(1))
        unit   = m.group(2).lower()
        return amount * 60 if ("hour" in unit or "hr" in unit) else amount
    return 30


def extract_habit_count(text: str) -> int:
    """Extract repetition count — e.g. '4 times', '2 sessions', '3x'."""
    m = _COUNT_RE.search(text)
    return int(m.group(1)) if m else 1


def extract_habit_notes(text: str) -> Optional[str]:
    """
    Extract a short descriptor — e.g. '30 min', '×2', '5km'.
    Looks for patterns like '30 min', '1 hour', '5 km', 'x2'.
    """
    patterns = [
        r"\b(\d+(?:\.\d+)?)\s*(min(?:utes?)?|hour[s]?|hr[s]?|km|miles?)\b",
        r"[x×](\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return None


def extract_recurrence(text: str) -> Optional[Dict]:
    m = _RECURRENCE_RE.search(text)
    if not m:
        return None
    period = m.group(1).lower().strip()

    if period == "day":
        return {"rrule": "RRULE:FREQ=DAILY", "summary": "every day"}
    if period == "weekday":
        return {"rrule": "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "summary": "every weekday"}
    if period == "weekend":
        return {"rrule": "RRULE:FREQ=WEEKLY;BYDAY=SA,SU", "summary": "every weekend"}
    if period in ("week", "month"):
        freq = "WEEKLY" if period == "week" else "MONTHLY"
        return {"rrule": f"RRULE:FREQ={freq}", "summary": f"every {period}"}
    if "last day of" in period:
        return {"rrule": "RRULE:FREQ=MONTHLY;BYMONTHDAY=-1", "summary": "every last day of month"}
    nth = re.match(r"(\d+)", period)
    if nth and "of" in period:
        d = nth.group(1)
        return {"rrule": f"RRULE:FREQ=MONTHLY;BYMONTHDAY={d}", "summary": f"every {d}th of the month"}
    days_found = re.findall(
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)",
        period,
    )
    if days_found:
        codes    = [_DAY_MAP[d] for d in days_found]
        day_str  = ",".join(codes)
        day_names = " and ".join(days_found).capitalize()
        return {"rrule": f"RRULE:FREQ=WEEKLY;BYDAY={day_str}", "summary": f"every {day_names}"}
    return None


def extract_task_title(text: str) -> str:
    cleaned = _STRIP_INTENTS_RE.sub(" ", text)
    cleaned = _STRIP_DATES_RE.sub(" ", cleaned)
    cleaned = _RECURRENCE_RE.sub(" ", cleaned)
    cleaned = _REMINDER_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[,;:]+", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .,")
    return cleaned.capitalize() if cleaned else text.strip()


def extract_habit_title(text: str) -> str:
    """Like extract_task_title but also strips count expressions."""
    cleaned = extract_task_title(text)
    cleaned = _STRIP_COUNT_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .,")
    return cleaned.capitalize() if cleaned else text.strip()


_SLOT_RE = re.compile(
    r"((?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|wed|thu|fri|sat|sun)"
    r"(?:\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)",
    re.IGNORECASE,
)


def extract_multi_slots(text: str) -> Optional[list]:
    """Returns list of datetimes if 2+ day/time slots found, else None."""
    slots = _SLOT_RE.findall(text)
    if len(slots) < 2:
        return None
    parsed = []
    for s in slots:
        dt = dateparser.parse(s, settings={"PREFER_DATES_FROM": "future"})
        if dt:
            parsed.append(dt)
    return parsed if len(parsed) >= 2 else None


def parse_message(text: str) -> Dict:
    intent = detect_intent(text)
    result: Dict = {"intent": intent, "raw": text}

    if intent in ("add", "schedule", "schedule_direct", "update"):
        result["datetime"]    = extract_datetime(text)
        result["title"]       = extract_task_title(text)
        result["duration"]    = extract_duration(text)
        result["reminder"]    = extract_reminder_minutes(text)
        result["recurrence"]  = extract_recurrence(text)
        result["multi_slots"] = extract_multi_slots(text)

    if intent == "habit_add":
        result["title"]     = extract_habit_title(text)
        result["count"]     = extract_habit_count(text)
        result["notes"]     = extract_habit_notes(text)
        result["frequency"] = "daily" if re.search(r"\bdaily\b|\bevery\s+day\b|\bevery\s+morning\b", text, re.IGNORECASE) else "weekly"

    return result