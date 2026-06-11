"""
NLP helpers — intent detection, datetime/duration/reminder/recurrence extraction.
"""

import os
import re

import dateparser
import pytz
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List


TIMEZONE = os.getenv("TIMEZONE", "Asia/Seoul")

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
        r"\bschedule\b.{1,80}\b(on|at|this|next|every|today|tomorrow|from)\b",
        r"\bschedule\b.{1,40}\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b",
        r"\bschedule\b.{1,60}\d{1,2}\s*(?:am|pm)\b",
        r"\bschedule\b.{1,80}\bfrom\b",
        r"\b(block|put|add)\b.{1,40}\b(on|at|in|to)\b.{1,30}\b(calendar|cal)\b",
    ],
    "batch_schedule": [
        r"\bschedule\b.{0,20}\btask\s+\d+\b.{0,60}\b(on|at)\b",
        r"\bschedule\b.{0,20}\btask\s+\d+\b.{0,20}\band\b.{0,20}\btask\s+\d+\b",
    ],
    "schedule": [
        r"\bschedule\b\s*[\d\s,and]+$",
        r"\bschedule\s+\d",
    ],
    "plan": [
        r"\b(plan|organize|fit|place)\b.{0,40}\b(tasks?|todos?|week|day)\b",
        r"\bplan\s+my\s+(day|week|tasks?)\b",
    ],
    "free_time": [
        r"\b(when|where)\b.{0,30}\b(free|available|open)\b",
        r"\b(find|show|give)\b.{0,30}\b(free|available|open)\b.{0,20}\b(time|slot|block)s?\b",
        r"\bfind\s+me\b.{0,20}\b\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?)\b",
        r"\b(can i fit|fit in)\b",
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

# Absolute reminder time: "remind me at 2:30 PM", "notify me at 14:30"
_ABSOLUTE_REMINDER_RE = re.compile(
    r"(?:remind|notify|alert)\s+(?:me\s+)?(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*$"
    r"|(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s+(?:remind|notify|alert)",
    re.IGNORECASE,
)

_DURATION_RE = re.compile(
    r"\bfor\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)
_EFFORT_RE = re.compile(
    r"\b(?:takes?|needs?|requires?|estimate(?:d)?|about|around)\s+"
    r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)
_BARE_DURATION_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)
_BOTH_DURATION_RE = re.compile(
    r"\bboth\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)

# "from X to Y" duration — e.g. "from 9 pm to 11 pm", "from 9:30 to 11"
_FROM_TO_DURATION_RE = re.compile(
    r"\bfrom\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:to|–|-)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    re.IGNORECASE,
)

# ── Strip patterns for title extraction ───────────────────────────────────────

_STRIP_INTENTS_RE = re.compile(
    r"\b(add|create|new|track|save|log|set|remind me (about|to)?|task|todo|"
    r"a |an |the |schedule|daily|weekly|habit[s]?|do )\b",
    re.IGNORECASE,
)
_STRIP_DATES_RE = re.compile(
    r"(\bby\b|\bdue\b|\bon\b|\bbefore\b|\bnext\b|\bthis\b|\btomorrow\b|\btoday\b)"
    r"[\s\w,]*|"
    r"\bat\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|"
    r"\bfor\s+\d+\s*(?:hours?|hrs?|minutes?|mins?)\b|"
    r"\b\d{1,2}[\/\-\.]\d{1,2}(?:[\/\-\.]\d{2,4})?\b|"
    r"remind\s+me\s+\d+\s*(?:hours?|minutes?|mins?|hrs?)\s+before",
    re.IGNORECASE,
)
_STRIP_COUNT_RE = re.compile(
    r"\d+\s*(?:times?|sessions?|x)\s*(?:a\s+|per\s+)?(?:week|day)?"
    r"|(?:once|twice|thrice|double|triple)\s+(?:a|per)\s+(?:week|day)"
    r"|\d+\s*(?:-|to|–)\s*\d+\s*(?:times?|sessions?|x)",
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

# "next week monday", "this week friday"
_NEXT_WEEK_DAY_RE = re.compile(
    r"^(?:next|this)\s+week\s+(?:on\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)$",
    re.IGNORECASE,
)


def _next_weekday(name: str, force_next: bool = False, add_weeks: int = 0) -> datetime:
    tz         = pytz.timezone(TIMEZONE)
    today      = datetime.now(tz)
    target     = _DAYS[name.lower()]
    days_ahead = (target - today.weekday()) % 7
    if days_ahead == 0 or force_next:
        days_ahead += 7
    return today + timedelta(days=days_ahead + 7 * add_weeks)


def _default_morning(dt: datetime) -> datetime:
    """If dt has no meaningful time component (midnight), default to 9 AM."""
    if dt.hour == 0 and dt.minute == 0:
        return dt.replace(hour=9, minute=0, second=0, microsecond=0)
    return dt


def _parse_date_phrase(phrase: str) -> Optional[datetime]:
    phrase = phrase.strip()
    tz     = pytz.timezone(TIMEZONE)
    
    # "next week monday", "this week friday"
    m = _NEXT_WEEK_DAY_RE.match(phrase)
    if m:
        add = 1 if phrase.lower().startswith("next") else 0
        return _default_morning(_next_weekday(m.group(1), force_next=True, add_weeks=add))
    
    # "next monday", "this monday"
    m = _NEXT_RE.match(phrase)
    if m and m.group(1).lower() in _DAYS:
        return _default_morning(_next_weekday(m.group(1), force_next=True))
    
    m = _THIS_RE.match(phrase)
    if m and m.group(1).lower() in _DAYS:
        return _default_morning(_next_weekday(m.group(1), force_next=False))
    
    # Bare day name
    m = _PLAIN_RE.match(phrase)
    if m:
        return _default_morning(_next_weekday(m.group(1)))
    
    # "tomorrow" → tomorrow at 9 AM
    if phrase.lower() == "tomorrow":
        now = datetime.now(tz)
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    
    if phrase.lower() in ("today", "tonight"):
        return datetime.now(tz)
    
    return dateparser.parse(phrase, settings={"PREFER_DATES_FROM": "future", "PREFER_DAY_OF_MONTH": "first"})


# ── Time-of-day words ─────────────────────────────────────────────────────────

# Mapping of time-of-day words to hour (minutes = 0).
# Ordered chronologically for reference.
_TIMES_OF_DAY = {
    "midnight": 0,
    "dawn":     5,
    "morning":  8,
    "lunch":    12,
    "noon":     12,
    "afternoon": 14,
    "dusk":     17,
    "evening":  20,
    "night":    21,
}

# Deadline context markers — when these precede a time phrase, treat "midnight" as end-of-day
_DEADLINE_MARKERS_RE = re.compile(
    r"\b(by|due|deadline|before|until)\b",
    re.IGNORECASE,
)

# Detect if the text is in a deadline context (by/due/before/until)
_IS_DEADLINE_CONTEXT_RE = re.compile(
    r"\b(by|due|deadline|before|until)\b",
    re.IGNORECASE,
)

_TIMES_OF_DAY_RE = re.compile(
    r"\b(?:"
    r"at\s+|in\s+the\s+|this\s+|"
    r")?"
    r"(dawn|morning|lunch|noon|afternoon|dusk|evening|night|midnight)"
    r"\b",
    re.IGNORECASE,
)


def resolve_time_of_day(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """
    Resolve a time-of-day word to the *closest future* datetime.
    
    If the time-of-day today is still in the future, return today at that hour.
    If it has already passed, return *tomorrow* at that hour.
    
    Examples (now=10:00 AM):
      "at lunch"    → today 12:00 PM
      "in the evening" → today 8:00 PM
      "morning"     → today 8:00 AM (past!) → tomorrow 8:00 AM
    
    Examples (now=2:00 PM):
      "morning"     → tomorrow 8:00 AM
      "evening"     → today 8:00 PM
    """
    if now is None:
        now = datetime.now(pytz.timezone(TIMEZONE))
    
    m = _TIMES_OF_DAY_RE.search(text)
    if not m:
        return None
    
    word = m.group(1).lower()
    if word not in _TIMES_OF_DAY:
        return None
    
    hour = _TIMES_OF_DAY[word]
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    
    # If candidate is in the past, push to next occurrence (tomorrow)
    if candidate <= now:
        candidate += timedelta(days=1)
    
    return candidate


# ── FIXME: "day of month" parsing ─────────────────────────────────────────────

_NTH_OF_MONTH_RE = re.compile(
    r"(\d+)(?:st|nd|rd|th)\s+of\s+(?:(?:this|the\s+next|next)\s+)?month",
    re.IGNORECASE,
)


def resolve_nth_of_month(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Resolve patterns like '17th of next month' or '3rd of this month'."""
    if now is None:
        now = datetime.now(pytz.timezone(TIMEZONE))
    
    m = _NTH_OF_MONTH_RE.search(text)
    if not m:
        return None
    
    day = int(m.group(1))
    phrase = m.group(0).lower()
    
    if "next" in phrase:
        target_month = now.month + 1
        target_year = now.year
        if target_month > 12:
            target_month = 1
            target_year += 1
    else:
        target_month = now.month
        target_year = now.year
    
    # Clamp day to valid range for the target month
    import calendar
    max_day = calendar.monthrange(target_year, target_month)[1]
    day = min(day, max_day)
    
    return now.replace(year=target_year, month=target_month, day=day, hour=9, minute=0, second=0, microsecond=0)


# ── Regex patterns for extract_datetime ────────────────────────────────────────

# Day+time together (for reminders): "tomorrow 4pm", "Monday 9am", "monday evening", "tomorrow morning"
_DAY_WITH_TIME_RE = re.compile(
    r"((?:next\s+|this\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today)"
    r"\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)\b"
    r"|(?:next\s+|this\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today)"
    r"\s+(?:at\s+|in\s+the\s+)?(?:dawn|morning|lunch|noon|afternoon|dusk|evening|night|midnight)"
    r")",
    re.IGNORECASE,
)

# Relative time: "in 2 hours", "in 30 minutes"
_RELATIVE_TIME_RE = re.compile(
    r"\bin\s+(\d+)\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)

# Bare time reference (no day): "at 9 pm", "at 3:30am"
_BARE_TIME_RE = re.compile(
    r"\b(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
    re.IGNORECASE,
)

# Relative days: "in 3 days", "in 2 weeks"
_RELATIVE_DAY_RE = re.compile(
    r"\bin\s+(\d+)\s*(days?|weeks?)\b",
    re.IGNORECASE,
)

# Named relative days: "day after tomorrow", "day before yesterday", "after tomorrow", "before yesterday"
_NAMED_RELATIVE_DAY_RE = re.compile(
    r"\b(the\s+)?(day\s+after\s+tomorrow|day\s+before\s+yesterday|after\s+tomorrow|before\s+yesterday)\b",
    re.IGNORECASE,
)

# Day reference without explicit time: "on monday", "monday", "friday"
_DAY_ONLY_RE = re.compile(
    r"\b((?:this\s+|next\s+|on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun))\b",
    re.IGNORECASE,
)

# Explicit marker: by/due/before/until/on/for + date phrase
_DATE_MARKER_RE = re.compile(
    r"(?:by|due|before|until|for)\s+"
    r"("
    r"(?:next\s+|this\s+|coming\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)"
    r"(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?"
    r"|tomorrow|today|tonight|eod"
    r"|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:\s+\d{4})?"
    r"|\d{1,2}\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+\d{4})?"
    r"|\d{1,2}[\/\-\.]\d{1,2}(?:[\/\-\.]\d{2,4})?"
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


def _slot_has_time(slot_str: str) -> bool:
    """Check if a slot string contains an hour reference (e.g. '9 pm', '3:30')."""
    return bool(re.search(r"\d{1,2}(?::\d{2})?\s*(?:am|pm|\d{1,2})", slot_str, re.IGNORECASE))


def _is_deadline_context(text: str) -> bool:
    """Check if the text is in a deadline context (by/due/before/until)."""
    return bool(_IS_DEADLINE_CONTEXT_RE.search(text))


def _end_of_day(dt: datetime) -> datetime:
    """Set the time to 23:59 (end of day)."""
    return dt.replace(hour=23, minute=59, second=0, microsecond=0)


def extract_datetime(text: str) -> Optional[datetime]:
    """
    Robust datetime extraction with multiple fallback strategies.
    
    Priority order:
      0.  Day + explicit time together (e.g. "Monday 9am", "tomorrow 4pm")
      0a. Day + time-of-day together (e.g. "monday evening", "tomorrow morning")
      0b. Relative time (e.g. "in 2 hours", "in 30 minutes")
      0c. Time-of-day word alone (e.g. "at lunch", "in the evening", "morning")
      0d. Relative days (e.g. "in 3 days", "in 2 weeks")
      0e. Named relative days (e.g. "day after tomorrow", "day before yesterday")
      0f. "Nth of month" (e.g. "17th of next month", "3rd of this month")
      0g. Bare time (e.g. "at 9 pm", "at 3:30am") → defaults to today
      1.  Explicit markers (by/due/before/until/for + date)
      2.  "on [weekday]"
      3.  Bare day/date references
      4.  Full text fallback via dateparser
    
    Deadline context (by/due/before/until):
      - "midnight" → 23:59 (end of day)
      - "in 2 days" → date at 23:59 (end of day)
      - Bare day name → day at 23:59 (end of day)
    """
    # Normalize period-as-separator in times: "2.11 am" → "2:11 am"
    text = re.sub(r"(\d)\.(\d{2})\s*(am|pm)", r"\1:\2 \3", text, flags=re.IGNORECASE)
    
    is_deadline = _is_deadline_context(text)
    
    # ─── Step 0: Day + explicit time ────────────────────────────────────────
    # e.g. "Monday 9am", "tomorrow 4pm", "monday evening", "tomorrow morning"
    m = _DAY_WITH_TIME_RE.search(text)
    if m:
        group = m.group(1)
        # Check if it's a day + time-of-day (e.g. "monday evening")
        if re.search(r"(dawn|morning|lunch|noon|afternoon|dusk|evening|night|midnight)", group, re.IGNORECASE):
            # Extract the day and the time-of-day separately
            day_match = re.search(
                r"(?:next\s+|this\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today)",
                group, re.IGNORECASE
            )
            tod_match = _TIMES_OF_DAY_RE.search(group)
            if day_match and tod_match:
                # Get the date from the day name
                day_dt = _parse_date_phrase(day_match.group(1))
                if day_dt:
                    word = tod_match.group(1).lower()
                    # In deadline context, "midnight" means 23:59 (end of day)
                    if is_deadline and word == "midnight":
                        return _end_of_day(day_dt)
                    hour = _TIMES_OF_DAY.get(word)
                    if hour is not None:
                        return day_dt.replace(hour=hour, minute=0, second=0, microsecond=0)
        else:
            # Parse day and explicit time separately using timezone-aware functions.
            # dateparser doesn't know KST, so "today at 2:37 am" gets the wrong UTC day.
            day_time_match = re.match(
                r"((?:next\s+|this\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun|tomorrow|today))"
                r"\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm))",
                group, re.IGNORECASE
            )
            if day_time_match:
                day_dt = _parse_date_phrase(day_time_match.group(1))
                time_str = day_time_match.group(2)
                time_dt = dateparser.parse(time_str, settings={"PREFER_DATES_FROM": "future"})
                if day_dt and time_dt:
                    return day_dt.replace(hour=time_dt.hour, minute=time_dt.minute, second=0, microsecond=0)
            dt = dateparser.parse(group, settings={"PREFER_DATES_FROM": "future"})
            if dt:
                return dt
    
    # ─── Step 0b: Relative time (hours/minutes) ─────────────────────────────
    # e.g. "in 2 hours", "in 30 minutes"
    m = _RELATIVE_TIME_RE.search(text)
    if m:
        amount = int(m.group(1))
        unit   = m.group(2).lower()
        delta  = timedelta(hours=amount) if "h" in unit else timedelta(minutes=amount)
        return datetime.now(pytz.timezone(TIMEZONE)) + delta
    
    # ─── Step 0c: Time-of-day word alone ────────────────────────────────────
    # e.g. "at lunch", "in the evening", "morning"
    tod_dt = resolve_time_of_day(text)
    if tod_dt:
        # In deadline context, "midnight" alone → today at 23:59
        if is_deadline and "midnight" in text.lower():
            return _end_of_day(tod_dt)
        return tod_dt
    
    # ─── Step 0d: Relative days ─────────────────────────────────────────────
    # e.g. "in 3 days", "in 2 weeks"
    m = _RELATIVE_DAY_RE.search(text)
    if m:
        amount = int(m.group(1))
        unit   = m.group(2).lower()
        delta  = timedelta(weeks=amount) if "week" in unit else timedelta(days=amount)
        result = datetime.now(pytz.timezone(TIMEZONE)) + delta
        # In deadline context, "in 2 days" means end of that day (23:59)
        if is_deadline:
            return _end_of_day(result)
        return _default_morning(result)
    
    # ─── Step 0e: Named relative days ───────────────────────────────────────
    # e.g. "day after tomorrow", "day before yesterday", "after tomorrow"
    m = _NAMED_RELATIVE_DAY_RE.search(text)
    if m:
        phrase = m.group(2).lower()
        if "after tomorrow" in phrase:
            result = datetime.now(pytz.timezone(TIMEZONE)) + timedelta(days=2)
            if is_deadline:
                return _end_of_day(result)
            return _default_morning(result)
        if "before yesterday" in phrase:
            return _default_morning(datetime.now(pytz.timezone(TIMEZONE)) - timedelta(days=2))
        return None
    
    # ─── Step 0f: Nth of month ──────────────────────────────────────────────
    # e.g. "17th of next month", "3rd of this month"
    nth_dt = resolve_nth_of_month(text)
    if nth_dt:
        if is_deadline:
            return _end_of_day(nth_dt)
        return nth_dt
    
    # ─── Step 0g: Bare time ─────────────────────────────────────────────────
    # e.g. "at 9 pm", "at 3:30am" → defaults to today
    m = _BARE_TIME_RE.search(text)
    if m:
        dt = dateparser.parse(m.group(1), settings={"PREFER_DATES_FROM": "future"})
        if dt:
            now = datetime.now(pytz.timezone(TIMEZONE))
            return now.replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
    
    # ─── Step 1: Explicit markers ───────────────────────────────────────────
    # e.g. "by monday", "due friday", "for tomorrow"
    m = _DATE_MARKER_RE.search(text)
    if m:
        dt = _parse_date_phrase(m.group(1))
        if dt:
            # In deadline context, bare day name → end of day (23:59)
            if is_deadline:
                return _end_of_day(dt)
            return dt
    
    # ─── Step 2: "on [weekday]" ─────────────────────────────────────────────
    m = _ON_DAY_RE.search(text)
    if m:
        dt = _parse_date_phrase(m.group(1))
        if dt:
            if is_deadline:
                return _end_of_day(dt)
            return dt
    
    # ─── Step 3: Bare day/date references ───────────────────────────────────
    m = _BARE_DATE_RE.search(text)
    if m:
        dt = _parse_date_phrase(m.group(1))
        if dt:
            if is_deadline:
                return _end_of_day(dt)
            return dt
    
    # ─── Step 4: Full text fallback ─────────────────────────────────────────
    return dateparser.parse(text, settings={"PREFER_DATES_FROM": "future", "PREFER_DAY_OF_MONTH": "first"})


def extract_duration(text: str) -> Optional[int]:
    # First: try "from X to Y" — compute the difference between start and end times
    m = _FROM_TO_DURATION_RE.search(text)
    if m:
        try:
            start_str = m.group(1).strip()
            end_str   = m.group(2).strip()
            # Parse both times, assume today
            start_dt = dateparser.parse(start_str, settings={"PREFER_DATES_FROM": "future"})
            end_dt   = dateparser.parse(end_str,   settings={"PREFER_DATES_FROM": "future"})
            if start_dt and end_dt:
                now = datetime.now(pytz.timezone(TIMEZONE))
                start = now.replace(hour=start_dt.hour, minute=start_dt.minute, second=0, microsecond=0)
                end   = now.replace(hour=end_dt.hour, minute=end_dt.minute, second=0, microsecond=0)
                # If end is before start, assume it crosses midnight (e.g. 9pm to 11pm won't, but 10pm to 2am would)
                if end <= start:
                    end += timedelta(days=1)
                diff = int((end - start).total_seconds() / 60)
                if diff > 0:
                    return diff
        except Exception:
            pass

    m = _BOTH_DURATION_RE.search(text) or _DURATION_RE.search(text) or _EFFORT_RE.search(text) or _BARE_DURATION_RE.search(text)
    if m:
        amount = float(m.group(1))
        unit   = m.group(2).lower()
        return int(amount * 60) if ("hour" in unit or "hr" in unit) else int(amount)
    return None


def extract_reminder_minutes(text: str) -> int:
    m = _REMINDER_RE.search(text)
    if m:
        amount = int(m.group(1))
        unit   = m.group(2).lower()
        return amount * 60 if ("hour" in unit or "hr" in unit) else amount
    return 30


def compute_reminder_minutes(text: str, event_start_dt: Optional[datetime] = None) -> int:
    """
    Compute reminder minutes before event, supporting both relative and absolute times.

    Priority:
    1. Absolute reminder time: "remind me at 2:30 PM" + event_start → minutes_before
    2. Relative reminder: "remind me 45 min before" → 45
    3. Default: 30 min before
    """
    # Try absolute time first
    if event_start_dt:
        m = _ABSOLUTE_REMINDER_RE.search(text)
        if m:
            time_str = m.group(1) or m.group(2)
            if time_str:
                try:
                    reminder_dt = dateparser.parse(
                        time_str,
                        settings={
                            "PREFER_DATES_FROM": "future",
                            "RELATIVE_BASE": event_start_dt,
                        }
                    )
                    if reminder_dt:
                        # Parse out just the time
                        abs_time = event_start_dt.replace(
                            hour=reminder_dt.hour,
                            minute=reminder_dt.minute,
                            second=0, microsecond=0
                        )
                        diff_minutes = int((event_start_dt - abs_time).total_seconds() / 60)
                        if 1 <= diff_minutes <= 1440:
                            return diff_minutes
                except Exception:
                    pass

    # Fall back to relative reminder
    return extract_reminder_minutes(text)


def extract_habit_count(text: str) -> int:
    """Extract repetition count — e.g. '4 times', '2 sessions', '3x', 'twice', 'thrice'."""
    # Word forms
    word_counts = {
        "once": 1, "twice": 2, "thrice": 3,
        "double": 2, "triple": 3,
    }
    for word, count in word_counts.items():
        if re.search(rf"\b{word}\b", text, re.IGNORECASE):
            return count
    # Numeric forms: "4 times", "2 sessions", "3x", "2-3 times", etc.
    m = re.search(r"(\d+)\s*(?:-|to|–)\s*(\d+)", text, re.IGNORECASE)
    if m:
        return int(m.group(2))  # take the upper bound
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


def infer_category(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(gym|run|running|workout|exercise|sport|swim|yoga)\b", t):
        return "fitness"
    if re.search(r"\b(study|read|reading|write|writing|essay|report|code|coding|project|deep work|focus)\b", t):
        return "focus"
    if re.search(r"\b(call|meeting|standup|sync|interview)\b", t):
        return "meeting"
    if re.search(r"\b(shop|grocery|errand|dentist|doctor|bank|post office)\b", t):
        return "errand"
    if re.search(r"\b(clean|laundry|dishes|chores?)\b", t):
        return "home"
    return "general"


def infer_energy(text: str, category: str = None) -> str:
    t = text.lower()
    category = category or infer_category(text)
    if re.search(r"\b(deep|hard|intense|exam|midterm|final|urgent|important)\b", t):
        return "high"
    if category in ("focus", "fitness"):
        return "high"
    if category in ("errand", "home"):
        return "low"
    return "medium"


def infer_splittable(text: str, duration: int, category: str = None) -> bool:
    t = text.lower()
    category = category or infer_category(text)
    if re.search(r"\b(split|chunk|over several|over multiple)\b", t):
        return True
    if category in ("fitness", "meeting", "errand"):
        return False
    if duration is None:
        return False
    return duration >= 120


_UNTIL_RE = re.compile(
    r"\buntil\s+(.+?)(?:\s*(?:\.|,|$|remind|notify|at))",
    re.IGNORECASE,
)


def _parse_until(text: str) -> Optional[str]:
    """Extract UNTIL date from recurrence text and return it as an ICS-formatted string."""
    m = _UNTIL_RE.search(text)
    if not m:
        return None
    dt = extract_datetime(m.group(1))
    if dt:
        # ICS format: YYYYMMDDTHHMMSSZ
        return dt.astimezone(pytz.utc).strftime("%Y%m%dT%H%M%SZ")
    return None


def extract_recurrence(text: str) -> Optional[Dict]:
    m = _RECURRENCE_RE.search(text)
    if not m:
        return None
    period = m.group(1).lower().strip()

    # Build base rrule
    if period == "day":
        base = "RRULE:FREQ=DAILY"
        summary = "every day"
    elif period == "weekday":
        base = "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
        summary = "every weekday"
    elif period == "weekend":
        base = "RRULE:FREQ=WEEKLY;BYDAY=SA,SU"
        summary = "every weekend"
    elif period in ("week", "month"):
        freq = "WEEKLY" if period == "week" else "MONTHLY"
        base = f"RRULE:FREQ={freq}"
        summary = f"every {period}"
    elif "last day of" in period:
        base = "RRULE:FREQ=MONTHLY;BYMONTHDAY=-1"
        summary = "every last day of month"
    else:
        # Check for "Nth of month"
        nth = re.match(r"(\d+)", period)
        if nth and "of" in period:
            d = nth.group(1)
            base = f"RRULE:FREQ=MONTHLY;BYMONTHDAY={d}"
            summary = f"every {d}th of the month"
        else:
            # Check for specific days: "every monday and friday"
            days_found = re.findall(
                r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)",
                period,
            )
            if days_found:
                codes    = [_DAY_MAP[d] for d in days_found]
                day_str  = ",".join(codes)
                day_names = " and ".join(days_found).capitalize()
                base = f"RRULE:FREQ=WEEKLY;BYDAY={day_str}"
                summary = f"every {day_names}"
            else:
                return None

    # Check for UNTIL
    until_str = _parse_until(text)
    if until_str:
        base += f";UNTIL={until_str}"
        try:
            until_dt = datetime.strptime(until_str, "%Y%m%dT%H%M%SZ")
            from pytz import UTC
            until_dt = until_dt.replace(tzinfo=UTC)
            tz = pytz.timezone(TIMEZONE)
            local_until = until_dt.astimezone(tz)
            summary += f" until {local_until.strftime('%B %d')}"
        except Exception:
            pass

    return {"rrule": base, "summary": summary}


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
    r"(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)",
    re.IGNORECASE,
)


def extract_multi_slots(text: str) -> Optional[list]:
    """Returns list of datetimes if 2+ day/time slots found, else None.
    Also returns a list of (datetime, had_time) tuples for clarification checks."""
    slots = _SLOT_RE.findall(text)
    if len(slots) < 2:
        return None
    parsed = []
    had_times = []
    for s in slots:
        dt = dateparser.parse(s, settings={"PREFER_DATES_FROM": "future"})
        if dt:
            parsed.append(dt)
            had_times.append(_slot_has_time(s))
    if len(parsed) < 2:
        return None
    # Attach time-info as an extra attribute for downstream use
    result = parsed
    result._had_times = had_times if len(had_times) == len(parsed) else None
    return result


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

    if intent in ("add", "schedule", "schedule_direct", "batch_schedule", "update"):
        result["datetime"]    = extract_datetime(text)
        result["title"]       = extract_task_title(text)
        result["reminder"]    = extract_reminder_minutes(text)
        result["recurrence"]  = extract_recurrence(text)
        result["multi_slots"] = extract_multi_slots(text)
        # duration, category, energy, splittable omitted for "add" — user sets those when scheduling
        if intent != "add":
            result["duration"]    = extract_duration(text)
            result["category"]    = infer_category(text)
            result["energy"]      = infer_energy(text, result["category"])
            result["splittable"]  = infer_splittable(text, result["duration"], result["category"])

    if intent in ("free_time", "plan"):
        result["datetime"] = extract_datetime(text)
        result["duration"] = extract_duration(text)
        result["category"] = infer_category(text)
        result["energy"]   = infer_energy(text, result["category"])

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