"""
LLM layer — Gemini 2.5 Flash Lite via OpenRouter.

Handles:
- Complex multi-intent messages
- Priority extraction
- Clarification requests
- Calendar queries
- Minimal token output
"""

import os, json, logging, re
from datetime import datetime
from typing import Dict, Any, Optional
import requests
import dateparser

logger             = logging.getLogger(__name__)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL              = "google/gemini-2.5-flash-lite"
API_URL            = "https://openrouter.ai/api/v1/chat/completions"


# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM = """\
You are the NLP core of a personal planning Telegram bot. Extract structured intent from the user's message.
Today: {today}. User timezone: Asia/Seoul.

Output ONLY valid compact JSON — no prose, no markdown, no backticks.

INTENTS:
add_task | list_tasks | schedule_tasks | schedule_direct | complete | delete | update
habit_add | habit_list | habit_delete | calendar_query | help | clarify | unknown

SCHEMA (omit null fields):
{
  "intent": string,
  "title": string,              // clean event/task name
  "titles": [string],           // multiple tasks in one message
  "deadline": "YYYY-MM-DD",
  "deadlines": ["YYYY-MM-DD"],  // one per title if different
  "priority": "high"|"medium"|"low",  // infer from urgency words
  "slots": ["YYYY-MM-DDTHH:MM"],      // ALL time slots for scheduling
  "duration_minutes": int,
  "reminder_minutes": int,
  "rrule": string,              // RRULE:FREQ=... if recurring
  "recurrence_summary": string,
  "frequency": "daily"|"weekly",
  "count": int,
  "notes": string,
  "task_numbers": [int],
  "period": "today"|"week"|"next_week"|"all",
  "query": string,              // for calendar_query: what user wants to know
  "clarify": string             // question to ask user if intent truly unclear
}

PRIORITY RULES:
- "urgent","asap","immediately","critical","important" → high
- "sometime","eventually","when I can","low priority" → low
- default → medium

MULTI-TASK RULES:
- "I have a midterm Thursday and assignment due Friday" → titles=["Midterm","Assignment"] deadlines=[...]
- "block 3h study Tuesday and Wednesday evening" → intent=schedule_direct slots=[both datetimes]

RESCHEDULE: "reschedule X to Y", "move X to Y", "push X to Y" → intent=reschedule, title=event name, slots=[new datetime]

CLARIFY ONLY when genuinely ambiguous (not just missing a time — that's normal).
Example: "do the thing" → clarify="What task did you mean?"

NEVER clarify just because a time is missing — smart scheduling will handle that.
"""


# ── Call ──────────────────────────────────────────────────────────────────────

def parse(text: str, conversation_context: str = "") -> Optional[Dict[str, Any]]:
    """
    Call Gemini. Returns raw parsed dict or None on failure.
    conversation_context: last bot message, helps resolve pronouns like "it","that".
    """
    if not OPENROUTER_API_KEY:
        return None

    today  = datetime.now().strftime("%Y-%m-%d %A")
    system = _SYSTEM.format(today=today)

    messages = [{"role": "system", "content": system}]
    if conversation_context:
        messages.append({"role": "assistant", "content": f"[prev: {conversation_context[:200]}]"})
    messages.append({"role": "user", "content": text})

    try:
        resp = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://github.com/Phantom-curly/scheduler",
            },
            json={
                "model":       MODEL,
                "max_tokens":  250,
                "temperature": 0,
                "messages":    messages,
            },
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
        parsed        = json.loads(raw)
        parsed["raw"] = text
        return parsed
    except requests.exceptions.Timeout:
        logger.warning("LLM timeout — falling back to regex")
        return None
    except Exception as exc:
        logger.warning(f"LLM error ({exc}) — falling back to regex")
        return None


# ── Normalise ─────────────────────────────────────────────────────────────────

def normalise(llm: Dict[str, Any]) -> Dict[str, Any]:
    """Map LLM output to the internal format bot.py expects."""
    out: Dict[str, Any] = {
        "intent":       _map_intent(llm.get("intent", "unknown")),
        "raw":          llm.get("raw", ""),
        "title":        llm.get("title"),
        "titles":       llm.get("titles"),       # multi-task add
        "deadlines":    llm.get("deadlines"),    # parallel to titles
        "priority":     llm.get("priority", "medium"),
        "duration":     llm.get("duration_minutes") or 60,
        "reminder":     llm.get("reminder_minutes") or 30,
        "recurrence":   None,
        "multi_slots":  None,
        "datetime":     None,
        "task_numbers": llm.get("task_numbers"),
        "period":       llm.get("period"),
        "frequency":    llm.get("frequency"),
        "count":        llm.get("count") or 1,
        "notes":        llm.get("notes"),
        "query":        llm.get("query"),
        "clarify":      llm.get("clarify"),
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

    # Slots
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
            out["datetime"]   = parsed_slots[0]
        elif len(parsed_slots) > 1:
            out["datetime"]   = parsed_slots[0]
            out["multi_slots"]= parsed_slots

    return out


def _map_intent(i: str) -> str:
    return {
        "add_task":        "add",
        "list_tasks":      "list",
        "schedule_tasks":  "schedule",
        "schedule_direct": "schedule_direct",
        "complete":        "complete",
        "delete":          "delete",
        "update":          "update",
        "reschedule":      "reschedule",
        "habit_add":       "habit_add",
        "habit_list":      "habit_list",
        "habit_delete":    "habit_delete",
        "calendar_query":  "calendar_query",
        "clarify":         "clarify",
        "help":            "help",
        "unknown":         "unknown",
    }.get(i, i)


# ── Calendar query answerer ───────────────────────────────────────────────────

def answer_calendar_query(query: str, events: list, tasks: list) -> str:
    """
    Use Gemini to answer a natural language question about the user's schedule.
    e.g. "what's my busiest day?", "when am I free Thursday?"
    """
    if not OPENROUTER_API_KEY:
        return "I can't answer calendar questions without the LLM configured."

    today = datetime.now().strftime("%Y-%m-%d %A")

    events_text = "\n".join(
        f"- {e.get('summary','?')} at {e.get('start',{}).get('dateTime','?')}"
        for e in events[:30]
    ) or "No events."

    tasks_text = "\n".join(
        f"- {t['title']} due {t['deadline'] or 'no deadline'} [{t['status']}]"
        for t in tasks[:20]
    ) or "No tasks."

    prompt = (
        f"Today: {today}\n"
        f"Calendar events:\n{events_text}\n\n"
        f"Tasks:\n{tasks_text}\n\n"
        f"Question: {query}\n\n"
        "Answer in 2-3 sentences max. Be direct and specific."
    )

    try:
        resp = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       MODEL,
                "max_tokens":  120,
                "temperature": 0.3,
                "messages":    [{"role": "user", "content": prompt}],
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning(f"calendar_query LLM error: {exc}")
        return "Couldn't analyse your calendar right now."