"""
LLM layer — Gemini 2.5 Flash Lite via OpenRouter.

Single responsibility: parse a raw user message into structured JSON.
Falls back to regex-based nlp.py if the API is unavailable.

Token optimization:
- System prompt is short and instruction-dense
- Response is JSON only, no prose
- max_tokens capped at 200
"""

import os
import json
import logging
import re
from datetime import datetime
from typing import Dict, Any, Optional

import requests
import dateparser

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL              = "google/gemini-2.5-flash-lite"
API_URL            = "https://openrouter.ai/api/v1/chat/completions"

# ── System prompt — kept minimal for cheap token usage ────────────────────────

_TODAY = datetime.now().strftime("%Y-%m-%d %A")  # refreshed at module load

_SYSTEM_PROMPT = f"""You are a JSON extractor for a planning bot. Today: {_TODAY}.
Output ONLY valid JSON. No prose, no markdown, no backticks.

Intents: add_task | list_tasks | schedule_tasks | schedule_direct | complete | delete | update | habit_add | habit_list | habit_delete | help | unknown

JSON schema:
{{
  "intent": string,
  "title": string|null,           // task or event name, clean
  "deadline": "YYYY-MM-DD"|null,  // for add_task
  "slots": ["YYYY-MM-DDTHH:MM"]|null, // for schedule_direct, all time slots
  "duration_minutes": int|null,   // default 60
  "reminder_minutes": int|null,   // default 30
  "rrule": string|null,           // RRULE string if recurring
  "recurrence_summary": string|null, // human label e.g. "every Monday"
  "frequency": "daily"|"weekly"|null, // for habits
  "count": int|null,              // habit sessions per week
  "notes": string|null,           // habit descriptor e.g. "30 min"
  "task_numbers": [int]|null,     // for schedule/complete/delete/update
  "period": "today"|"week"|"next_week"|"all"|null // for list_tasks
}}

Rules:
- dates relative to today ({_TODAY})
- omit null fields to save tokens
- slots: always future datetimes, ISO format, no timezone
- rrule: standard RRULE e.g. RRULE:FREQ=WEEKLY;BYDAY=MO,TU
"""

# ── Main parser ────────────────────────────────────────────────────────────────

def parse(text: str) -> Optional[Dict[str, Any]]:
    """
    Call Gemini via OpenRouter. Returns parsed dict or None on failure.
    Caller should fall back to regex nlp.py if None is returned.
    """
    if not OPENROUTER_API_KEY:
        return None

    # Refresh today's date in system prompt each call (bot runs 24/7)
    today = datetime.now().strftime("%Y-%m-%d %A")
    system = _SYSTEM_PROMPT.replace(_TODAY, today)

    try:
        resp = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://github.com/Phantom-curly/scheduler",
            },
            json={
                "model":      MODEL,
                "max_tokens": 200,
                "temperature": 0,      # deterministic — cheaper, more consistent
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": text},
                ],
            },
            timeout=8,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()

        # Strip any accidental markdown fences
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()

        parsed = json.loads(raw)
        parsed["raw"] = text
        return parsed

    except requests.exceptions.Timeout:
        logger.warning("LLM timeout — falling back to regex")
        return None
    except Exception as exc:
        logger.warning(f"LLM error ({exc}) — falling back to regex")
        return None


# ── Normalise LLM output to match what bot.py expects ────────────────────────

def normalise(llm: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map LLM JSON fields to the internal format bot.py uses.
    Ensures missing fields get safe defaults.
    """
    out: Dict[str, Any] = {
        "intent":      _map_intent(llm.get("intent", "unknown")),
        "raw":         llm.get("raw", ""),
        "title":       llm.get("title"),
        "duration":    llm.get("duration_minutes") or 60,
        "reminder":    llm.get("reminder_minutes") or 30,
        "recurrence":  None,
        "multi_slots": None,
        "datetime":    None,
        "task_numbers": llm.get("task_numbers"),
        "period":      llm.get("period"),
        "frequency":   llm.get("frequency"),
        "count":       llm.get("count") or 1,
        "notes":       llm.get("notes"),
    }

    # Recurrence
    if llm.get("rrule"):
        out["recurrence"] = {
            "rrule":   llm["rrule"],
            "summary": llm.get("recurrence_summary", "recurring"),
        }

    # Deadline → datetime
    if llm.get("deadline"):
        try:
            out["datetime"] = datetime.fromisoformat(llm["deadline"])
        except Exception:
            out["datetime"] = dateparser.parse(llm["deadline"])

    # Slots for multi-slot direct scheduling
    if llm.get("slots"):
        parsed_slots = []
        for s in llm["slots"]:
            try:
                parsed_slots.append(datetime.fromisoformat(s))
            except Exception:
                dt = dateparser.parse(s, settings={"PREFER_DATES_FROM": "future"})
                if dt:
                    parsed_slots.append(dt)
        if len(parsed_slots) == 1:
            out["datetime"]    = parsed_slots[0]
            out["multi_slots"] = None
        elif len(parsed_slots) > 1:
            out["datetime"]    = parsed_slots[0]
            out["multi_slots"] = parsed_slots

    return out


def _map_intent(llm_intent: str) -> str:
    """Map LLM intent names to internal bot.py intent names."""
    mapping = {
        "add_task":       "add",
        "list_tasks":     "list",
        "schedule_tasks": "schedule",
        "schedule_direct":"schedule_direct",
        "complete":       "complete",
        "delete":         "delete",
        "update":         "update",
        "habit_add":      "habit_add",
        "habit_list":     "habit_list",
        "habit_delete":   "habit_delete",
        "help":           "help",
        "unknown":        "unknown",
    }
    return mapping.get(llm_intent, llm_intent)
