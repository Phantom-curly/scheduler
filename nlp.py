"""
NLP helpers — intent detection, datetime/duration/reminder/recurrence extraction.
"""

import re
import dateparser
from datetime import datetime, timedelta
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
    "reminder": [
        r"\bremind\s+me\b",
        r"\bset\s+(a\s+)?reminder\b",
        r"\breminder\s*(for|to|at|on)\b",
    ],
    "add": [
        r"\b(add|create|new|track|save|log|set)\b",
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
    "reschedule": [
        r"\b(reschedule|move|push|shift)\b.{1,40}\b(to|for|on)\b.{1,30}\b(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next|morning|evening|night|am|pm)\b",
    ],
    "update": [
        r"\b(update|edit|change|rename|modify|extend)\b",
    ],
    "help": [
        r"\b(help|commands?|what can you|how do i)\b",
    ],
}

_DEADLINE_HINTS = re.compile(
    r"\b(by|due|deadline|before|until|on)\s+\w"
    r"|\b(this|next|coming)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|\bI\s+have\s+.{1,30}\b(?:on|this|next)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
    re.IGNORECASE
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


# ── Day name helpers ──────────────────────────────────────────────────────────

_DAYS = {
    "monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6,
    "mon":0,"tue":1,"wed":2,"thu":3,"fri":4,"sat":5,"sun":6,
}
_NEXT_RE  = re.compile(r"^next\s+(\w+)$",    re.I)
_THIS_RE  = re.compile(r"^this\s+(\w+)$",    re.I)
_PLAIN_RE = re.compile(r"^(mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)$", re.I)


def _next_weekday(name: str, force_next: bool = False) -> datetime:
    today      = datetime.now()
    target     = _DAYS[name.lower()]
    days_ahead = (target - today.weekday()) % 7
    if days_ahead == 0 or force_next:
        days_ahead += 7
    return today + timedelta(days=days_ahead)


def _parse_date_phrase(phrase: str) -> Optional[datetime]:
    phrase = phrase.strip()
    m = _NEXT_RE.match(phrase)
    if m and m.group(1).lower() in _DAYS:
        return _next_weekday(m.group(1), force_next=True)
    m = _THIS_RE.match(phrase)
    if m and m.group(1).lower() in _DAYS:
        return _next_weekday(m.group(1), force_next=False)
    m = _PLAIN_RE.match(phrase)
    if m:
        return _next_weekday(m.group(1))
    if phrase.lower() == "tomorrow":
        return datetime.now() + timedelta(days=1)
    if phrase.lower() in ("today", "tonight"):
        return datetime.now()
    return dateparser.parse(phrase, settings={"PREFER_DATES_FROM": "future", "PREFER_DAY_OF_MONTH": "first"})


# Explicit marker: by/due/before/until/on/for + date phrase
_DATE_MARKER_RE = re.compile(
    r"(?:by|due|before|until|for)\s+"
    r"("
    r"(?:next\s+|this\s+|coming\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)"
    r"(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?"
    r"|tomorrow|today|tonight|eod"
    r"|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:\s+\d{4})?"
    r"|\d{1,2}\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+\d{4})?"
    r"|\d{1,2}[\/\-]\d{1,2}(?:[\/\-]\d{2,4})?"
    r")",
    re.IGNORECASE,
)

# "on [day]" with word boundary
_ON_DAY_RE = re.compile(
    r"\bon\s+((?:next\s+|this\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun))\b",
    re.IGNORECASE,
)

# Bare date reference (no marker)
_BARE_DATE_RE = re.compile(
    r"\b((?:next\s+|this\s+|coming\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)"
    r"|tomorrow|today|tonight"
    r"|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:\s+\d{4})?)\b",
    re.IGNORECASE,
)


# Day+time together (for reminders): "tomorrow 4pm", "Monday 9am"
_DAY_WITH_TIME_RE = re.compile(
    r"((?:next\s+|this\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today)"
    r"\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)\b"
    r"|(?:tomorrow|today)\s+(?:morning|afternoon|evening|night))",
    re.IGNORECASE,
)

# Relative time: "in 2 hours", "in 30 minutes"
_RELATIVE_TIME_RE = re.compile(
    r"\bin\s+(\d+)\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)

def extract_datetime(text: str) -> Optional[datetime]:
    """Robust datetime extraction with multiple fallback strategies."""
    # 0. Day + time together (e.g. "tomorrow 4pm", "Monday 9am")
    m = _DAY_WITH_TIME_RE.search(text)
    if m:
        dt = dateparser.parse(m.group(1), settings={"PREFER_DATES_FROM": "future"})
        if dt:
            return dt
    # 0b. Relative time ("in 2 hours", "in 30 minutes")
    m = _RELATIVE_TIME_RE.search(text)
    if m:
        from datetime import timedelta as _td
        amount = int(m.group(1))
        unit   = m.group(2).lower()
        delta  = _td(hours=amount) if "h" in unit else _td(minutes=amount)
        return datetime.now() + delta
    # 1. Explicit markers (by/due/before/until/for + date)
    m = _DATE_MARKER_RE.search(text)
    if m:
        dt = _parse_date_phrase(m.group(1))
        if dt:
            return dt
    # 2. "on [weekday]" with word boundary
    m = _ON_DAY_RE.search(text)
    if m:
        dt = _parse_date_phrase(m.group(1))
        if dt:
            return dt
    # 3. Bare day/date references
    m = _BARE_DATE_RE.search(text)
    if m:
        dt = _parse_date_phrase(m.group(1))
        if dt:
            return dt
    # 4. Full text fallback
    return dateparser.parse(text, settings={"PREFER_DATES_FROM": "future", "PREFER_DAY_OF_MONTH": "first"})


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


def extract_reminder_title(text: str) -> str:
    """Extract what the reminder is about from natural language."""
    t = text.strip()

    # "remind me [time] to/about X"
    m = re.search(r"remind\s+me\b.{0,40}?\b(?:to|about|that)\s+(.+?)(?:\s+(?:by|before|on|at|tomorrow|today|next|this|\d).*)?$", t, re.IGNORECASE)
    if m:
        title = re.sub(r"\s+(?:by|before|at|on)\s+.+$", "", m.group(1), flags=re.IGNORECASE).strip(" .,")
        if len(title) > 2:
            return title.capitalize()

    # "set a reminder (for/to) ... to/about X"
    m = re.search(r"(?:set\s+(?:a\s+)?reminder\s+(?:for|to))\s+.{0,30}?\s+(?:to|about)\s+(.+)$", t, re.IGNORECASE)
    if m:
        return m.group(1).strip().capitalize()

    # "remind me to X"
    m = re.search(r"remind\s+me\s+(?:to|about)\s+(.+)$", t, re.IGNORECASE)
    if m:
        return re.sub(r"\s+(?:by|before|at|on)\s+.+$", "", m.group(1), flags=re.IGNORECASE).strip(" .,").capitalize()

    # Fallback: strip noise
    cleaned = re.sub(r"\b(remind(?:\s+me)?|set\s+a\s+reminder(?:\s+for)?)\b", "", t, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(tomorrow|today|tonight|next\s+\w+|this\s+\w+)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\d{1,2}(?::\d{2})?\s*(?:am|pm)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(to|about|that|for|at|by)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" .,")
    return cleaned.capitalize() if cleaned else t


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

    if intent == "reminder":
        result["datetime"]   = extract_datetime(text)
        result["title"]      = extract_reminder_title(text)
        result["recurrence"] = extract_recurrence(text)
        result["duration"]   = 15  # short block, just a notification

    if intent == "habit_add":
        result["title"]     = extract_habit_title(text)
        result["count"]     = extract_habit_count(text)
        result["notes"]     = extract_habit_notes(text)
        result["frequency"] = "daily" if re.search(r"\bdaily\b|\bevery\s+day\b|\bevery\s+morning\b", text, re.IGNORECASE) else "weekly"

    return result