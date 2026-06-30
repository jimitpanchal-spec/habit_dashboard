import os
import re
import hashlib
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from notion_client import Client
import anthropic
from notion_writer import (
    _block_plain_text,
    find_yesterday_log_page_id,
    read_yesterday_habits,
    build_habit_checklist,
)

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/tasks.readonly",
]

# Replace with your calendar ID from Google Calendar settings (AI Automation calendar).
AI_AUTOMATION_CALENDAR_ID = "9f8e8d14d40afc85c2ae9b9fec7cd2cd1a16aea86d75f98500eed7bc81f4f752@group.calendar.google.com"
ACADEMIC_CALENDAR_ID = "d2d861f4fe2c39d36b5c0816a51b2193c203a7bcfe7382716f5255096aa022af@group.calendar.google.com"
EXTRACURRICULAR_CALENDAR_ID = "jimitspanchal@gmail.com"
PERSONAL_CALENDAR_ID = "6daab7ff698e380a4203489e10c9831fd288f16a2f83350c97bf8015f2c17067@group.calendar.google.com"

TORONTO_TZ = ZoneInfo("America/Toronto")

SUMMER_START = date(2026, 6, 23)
SUMMER_END   = date(2026, 9, 7)

HABITS_PAGE_ID = "464f3b1a634a8346b4da01ba9a58527e"
IDEAL_PERSONA_PAGE_ID = "35bf3b1a634a80e4af33f01d24ab3f8b"
PLAN_PAGE_ID = "344f3b1a634a80efb730de611772bd64"
GOALS_PAGE_ID = "355f3b1a634a8009aaf4e58a8c047cef"
ACTIVITY_LOG_PAGE_ID = "352f3b1a634a80c79178f7c45d66995c"
QUOTES_PAGE_ID = "363f3b1a634a80c580f1dd85820125a8"
HABIT_TRACKER_DATABASE_ID = "36af3b1a634a81dc8f16f4cc4ae1a8eb"

TASK_LISTS_TO_USE = ["Current", "High Priority"]

DAY_THEMES: dict[int, str] = {
    0: "Prep & Deep Work",
    1: "Deep Work",
    2: "Deep Work & Personal Project",
    3: "Catch Up",
    4: "Light Tasks",
    5: "Personal Project",
    6: "Weekly Reset, Reflect & Planning",
}

# ── Google Auth ──────────────────────────────────────────────────────────────

def get_google_credentials():
    creds = None
    token_path = os.path.join(os.path.dirname(__file__), "token.json")
    creds_path = os.path.join(os.path.dirname(__file__), "credentials.json")

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"[Auth] Token refresh failed: {e}. Re-authenticating...")
                creds = None

        if not creds:
            if not os.path.exists(creds_path):
                raise FileNotFoundError(
                    "credentials.json not found. Download it from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


# ── Google Calendar ──────────────────────────────────────────────────────────

def _calendar_item_to_dict(e: dict) -> dict:
    start = e.get("start", {})
    end = e.get("end", {})
    start_str = start.get("dateTime") or start.get("date", "")
    end_str = end.get("dateTime") or end.get("date", "")
    return {
        "summary": e.get("summary", "(No title)"),
        "start": start_str,
        "end": end_str,
        "description": e.get("description", ""),
        "location": e.get("location", ""),
    }


def _event_sort_key(e: dict) -> datetime:
    start_str = e.get("start", "")
    if not start_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        if "T" in start_str:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        return datetime.fromisoformat(start_str).replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def fetch_calendar_events(creds, date: datetime) -> list[dict]:
    service = build("calendar", "v3", credentials=creds)

    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    local_day = date.astimezone(TORONTO_TZ)
    day_start_local = local_day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = day_start_local + timedelta(days=1)
    time_min = day_start_local.astimezone(timezone.utc).isoformat()
    time_max = day_end_local.astimezone(timezone.utc).isoformat()

    calendar_ids = ["primary"]
    if not AI_AUTOMATION_CALENDAR_ID.startswith("[PASTE YOUR"):
        calendar_ids.append(AI_AUTOMATION_CALENDAR_ID)
    if not ACADEMIC_CALENDAR_ID.startswith("[PASTE"):
        calendar_ids.append(ACADEMIC_CALENDAR_ID)
    if not EXTRACURRICULAR_CALENDAR_ID.startswith("[PASTE"):
        calendar_ids.append(EXTRACURRICULAR_CALENDAR_ID)
    if not PERSONAL_CALENDAR_ID.startswith("[PASTE"):
        calendar_ids.append(PERSONAL_CALENDAR_ID)
    merged: list[dict] = []
    for cal_id in calendar_ids:
        events_result = service.events().list(
            calendarId=cal_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        for e in events_result.get("items", []):
            merged.append(_calendar_item_to_dict(e))

    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for e in merged:
        key = (e.get("summary", ""), e.get("start", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)

    deduped.sort(key=_event_sort_key)
    return deduped


def _toronto_day_bounds(plan_date: datetime) -> tuple[datetime, datetime]:
    """Start (inclusive) and end (exclusive) of the calendar day in America/Toronto."""
    local = plan_date.astimezone(TORONTO_TZ)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return day_start, day_end


def _deep_work_blocks_exist_today(service, day_start: datetime, day_end: datetime) -> bool:
    events_result = service.events().list(
        calendarId=AI_AUTOMATION_CALENDAR_ID,
        timeMin=day_start.isoformat(),
        timeMax=day_end.isoformat(),
        singleEvents=True,
    ).execute()
    for e in events_result.get("items", []):
        summary = (e.get("summary") or "").lower()
        if (
            "deep work block" in summary
            or summary.startswith("🎧")
            or summary.startswith("📙")
            or summary.startswith("🔬")
            or summary.startswith("💻")
            or summary.startswith("📊")
            or summary.startswith("🔭")
            or summary.startswith("📌")
        ):
            return True
    return False


def create_focus_block_event(creds, plan_date: datetime, school_day: bool, must_do_tasks: dict | None = None) -> None:
    """Create deep work blocks on the AI Automation calendar (weekday vs weekend rules).

    Block times follow Toronto local weekday/weekend; school_day is kept for callers.
    """
    if AI_AUTOMATION_CALENDAR_ID.startswith("[PASTE YOUR"):
        print("[*] Skipping focus blocks: set AI_AUTOMATION_CALENDAR_ID in planner.py.")
        return

    service = build("calendar", "v3", credentials=creds)
    day_start, day_end = _toronto_day_bounds(plan_date)

    if _deep_work_blocks_exist_today(service, day_start, day_end):
        print("[*] Focus blocks already on calendar today — skipping creation.")
        return

    local = plan_date.astimezone(TORONTO_TZ)
    today_local = local.date()
    is_summer = SUMMER_START <= today_local <= SUMMER_END
    is_weekend = local.weekday() >= 5 or (is_summer and local.weekday() < 5)

    def _task_to_block_name(task: str) -> str:
        task_lower = task.lower()
        if any(w in task_lower for w in ["english", "essay", "write", "outline", "read", "isu"]):
            emoji = "📙"
        elif any(w in task_lower for w in ["math", "calc", "chemistry", "chem", "science", "bio", "physics"]):
            emoji = "🔬"
        elif any(w in task_lower for w in ["code", "python", "program", "cs", "computer"]):
            emoji = "💻"
        elif any(w in task_lower for w in ["deca", "business", "plan", "present"]):
            emoji = "📊"
        elif any(w in task_lower for w in ["project", "science fair", "neurolux", "research"]):
            emoji = "🔭"
        else:
            emoji = "📌"
        return f"{emoji} {task}"

    must_do_tasks = must_do_tasks or {}
    high_tasks = must_do_tasks.get("high", [])
    med_tasks = must_do_tasks.get("medium", [])
    primary_task = high_tasks[0] if len(high_tasks) > 0 else None
    secondary_task = high_tasks[1] if len(high_tasks) > 1 else (med_tasks[0] if med_tasks else None)

    block1_name = _task_to_block_name(primary_task) if primary_task else "🎧 Deep Work Block"
    block2_name = _task_to_block_name(secondary_task) if secondary_task else "🎧 Deep Work Block"
    weekday_block_name = block1_name

    def _insert(summary: str, start_local: datetime, end_local: datetime) -> None:
        body = {
            "summary": summary,
            "start": {"dateTime": start_local.isoformat(), "timeZone": "America/Toronto"},
            "end": {"dateTime": end_local.isoformat(), "timeZone": "America/Toronto"},
        }
        service.events().insert(calendarId=AI_AUTOMATION_CALENDAR_ID, body=body).execute()

    d = local.date()
    if is_weekend:
        b1_start = datetime(d.year, d.month, d.day, 13, 0, tzinfo=TORONTO_TZ)
        b1_end = datetime(d.year, d.month, d.day, 14, 30, tzinfo=TORONTO_TZ)
        b2_start = datetime(d.year, d.month, d.day, 15, 30, tzinfo=TORONTO_TZ)
        b2_end = datetime(d.year, d.month, d.day, 17, 0, tzinfo=TORONTO_TZ)
        _insert(block1_name, b1_start, b1_end)
        _insert(block2_name, b2_start, b2_end)
        print("[*] Created weekend focus blocks on AI Automation calendar.")
    else:
        w_start = datetime(d.year, d.month, d.day, 15, 30, tzinfo=TORONTO_TZ)
        w_end = datetime(d.year, d.month, d.day, 17, 0, tzinfo=TORONTO_TZ)
        _insert(weekday_block_name, w_start, w_end)
        print("[*] Created weekday focus block on AI Automation calendar.")


# ── School Day Detection ──────────────────────────────────────────────────────

_SCHOOL_KEYWORDS = [
    "school arrival", "school", "pickering high",
    "phhs", "homeroom", "period", "class",
]


def is_school_day(events: list[dict]) -> bool:
    today_local = datetime.now(TORONTO_TZ).date()
    if SUMMER_START <= today_local <= SUMMER_END:
        return False
    if datetime.now(TORONTO_TZ).weekday() >= 5:
        return False
    for e in events:
        summary = (e.get("summary") or "").lower().strip()
        if any(kw in summary for kw in _SCHOOL_KEYWORDS):
            return True
        start_str = e.get("start", "")
        end_str = e.get("end", "")
        if "T" in start_str and "T" in end_str:
            try:
                start_dt = datetime.fromisoformat(
                    start_str.replace("Z", "+00:00")
                )
                end_dt = datetime.fromisoformat(
                    end_str.replace("Z", "+00:00")
                )
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                start_local = start_dt.astimezone(TORONTO_TZ)
                end_local = end_dt.astimezone(TORONTO_TZ)
                start_min = start_local.hour * 60 + start_local.minute
                end_min = end_local.hour * 60 + end_local.minute
                if 7 * 60 + 30 <= start_min <= 10 * 60 and end_min <= 15 * 60 + 30:
                    return True
            except Exception:
                pass
    return False


# ── Google Tasks ─────────────────────────────────────────────────────────────

def _parse_task_metadata(notes: str) -> dict:
    meta = {"time": None, "energy": None, "due": None}
    if not notes:
        return meta
    for line in notes.splitlines():
        line = line.strip().lower()
        m = re.search(r"time:\s*(\d+)\s*min", line)
        if m:
            meta["time"] = int(m.group(1))
        m = re.search(r"energy:\s*(high|medium|low)", line)
        if m:
            meta["energy"] = m.group(1)
        m = re.search(r"due:\s*(\S+)", line)
        if m:
            meta["due"] = m.group(1)
    return meta


def fetch_tasks(creds) -> dict[str, list[dict]]:
    service = build("tasks", "v1", credentials=creds)
    all_lists = service.tasklists().list().execute().get("items", [])

    result = {"High Priority": [], "Current": []}
    for tl in all_lists:
        name = tl.get("title", "")
        if name not in TASK_LISTS_TO_USE:
            continue
        items = service.tasks().list(
            tasklist=tl["id"], showCompleted=False, showHidden=False
        ).execute().get("items", [])
        for task in items:
            meta = _parse_task_metadata(task.get("notes", ""))
            result[name].append({
                "title": task.get("title", "(Untitled)"),
                "notes": task.get("notes", ""),
                "time_min": meta["time"],
                "energy": meta["energy"],
                "due": meta["due"],
            })

    return result


# ── Notion Reader ─────────────────────────────────────────────────────────────

def _fetch_page_text(notion: Client, page_id: str, _depth: int = 0) -> str:
    """Fetch all text from a Notion page including nested children."""
    if _depth > 3:
        return ""
    clean_id = page_id.replace("-", "")
    try:
        blocks: list[dict] = []
        cursor = None
        while True:
            kwargs = {"block_id": clean_id, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = notion.blocks.children.list(**kwargs)
            blocks.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")

        lines: list[str] = []
        for block in blocks:
            btype = block.get("type", "")
            if btype == "child_page":
                continue
            content = block.get(btype, {})

            # Handle table_row blocks — their data is in "cells", not "rich_text"
            if btype == "table_row":
                cells = content.get("cells", [])
                row_parts = []
                for cell in cells:
                    cell_text = "".join(rt.get("plain_text", "") for rt in cell)
                    if cell_text.strip():
                        row_parts.append(cell_text.strip())
                if row_parts:
                    lines.append(" | ".join(row_parts))
                continue  # skip the rich_text path below

            rich = content.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich)
            if text.strip():
                lines.append(text.strip())
            if block.get("has_children"):
                child_text = _fetch_page_text(notion, block["id"], _depth + 1)
                if child_text:
                    lines.append(child_text)

        return "\n".join(filter(None, lines))
    except Exception as e:
        print(f"[Notion] Could not read page {page_id}: {e}")
        return ""


def fetch_notion_context(notion_key: str) -> dict[str, str]:
    notion = Client(auth=notion_key)
    return {
        "habits": _fetch_page_text(notion, HABITS_PAGE_ID),
        "ideal_persona": _fetch_page_text(notion, IDEAL_PERSONA_PAGE_ID),
        "plan": _fetch_page_text(notion, PLAN_PAGE_ID),
        "goals": _fetch_page_text(notion, GOALS_PAGE_ID),
        "activity_log": _fetch_page_text(notion, ACTIVITY_LOG_PAGE_ID),
        "quotes": _fetch_page_text(notion, QUOTES_PAGE_ID),
    }


def fetch_summer_routine(notion_key: str) -> str:
    """Return the full text of the Habits & Routine page for summer schedule extraction."""
    notion = Client(auth=notion_key)
    return _fetch_page_text(notion, HABITS_PAGE_ID)


def fetch_habit_history(notion_key: str, days: int, habit_columns: list[str]) -> list[dict]:
    """
    Query the Habit & Mood Tracker database for the last `days` rows,
    sorted by Date descending. Returns a list of dicts with keys:
    date, habits (dict of column->bool), inner_state, performance.
    Returns empty list on any failure.
    """
    import requests as _req
    headers = {
        "Authorization": f"Bearer {notion_key}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    try:
        resp = _req.post(
            f"https://api.notion.com/v1/databases/{HABIT_TRACKER_DATABASE_ID}/query",
            headers=headers,
            json={
                "sorts": [{"property": "Date", "direction": "descending"}],
                "page_size": days,
            },
        )
        resp.raise_for_status()
        rows = resp.json().get("results", [])
    except Exception as e:
        print(f"[Warning] Could not fetch habit history: {e}")
        return []

    history = []
    for row in rows:
        props = row.get("properties", {})
        date_val = (props.get("Date") or {}).get("date") or {}
        date_str = date_val.get("start", "")
        habits = {
            col: bool((props.get(col) or {}).get("checkbox", False))
            for col in habit_columns
        }
        inner = (props.get("Inner State") or {}).get("number")
        perf  = (props.get("Performance") or {}).get("number")
        history.append({
            "date": date_str,
            "habits": habits,
            "inner_state": inner,
            "performance": perf,
        })
    return history


_WEEK_AHEAD_DAYS = (
    "Monday → Prep & Deep Work",
    "Tuesday → Deep Work",
    "Wednesday → Deep Work & Personal Project",
    "Thursday → Catch Up",
    "Friday → Light Tasks",
    "Saturday → Personal Project",
    "Sunday → Weekly Reset, Reflect & Planning",
)
_WEEK_AHEAD_HEADING = "📆 Week Ahead — Organize by Day"


def fetch_week_ahead_tasks(notion_key: str, local_now: datetime) -> dict:
    """Read per-day bullets from the most recent Sunday log's Week Ahead section."""
    if not notion_key:
        return {}

    if local_now.weekday() == 6:
        sunday_dt = local_now
    else:
        sunday_dt = local_now - timedelta(days=local_now.weekday() + 1)

    sunday_date_str = (
        sunday_dt.strftime("%A, %B %-d")
        if os.name != "nt"
        else sunday_dt.strftime("%A, %B %d").replace(" 0", " ")
    )

    page_id = find_yesterday_log_page_id(notion_key, sunday_date_str)
    if not page_id:
        return {}

    import requests as _req

    headers = {
        "Authorization": f"Bearer {notion_key}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    all_blocks: list[dict] = []
    cursor = None
    try:
        while True:
            params: dict = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = _req.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            all_blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
    except Exception as e:
        print(f"[Warning] Could not fetch Sunday page blocks: {e}")
        return {}

    section_start = None
    for i, block in enumerate(all_blocks):
        if block.get("type") == "heading_2" and _block_plain_text(block) == _WEEK_AHEAD_HEADING:
            section_start = i + 1
            break

    if section_start is None:
        return {}

    result: dict[str, list[str]] = {}
    current_day: str | None = None
    i = section_start
    while i < len(all_blocks):
        block = all_blocks[i]
        btype = block.get("type", "")
        text = _block_plain_text(block)

        if btype in ("heading_2", "divider"):
            break

        if btype == "heading_3" and text in _WEEK_AHEAD_DAYS:
            current_day = text
            result.setdefault(current_day, [])
            i += 1
            continue

        if btype == "bulleted_list_item" and current_day is not None:
            if text:
                result.setdefault(current_day, []).append(text)
            i += 1
            continue

        i += 1

    return {day: tasks for day, tasks in result.items() if tasks}


def fetch_friday_tasks(notion_key: str, local_now: datetime) -> dict:
    """Read Saturday/Sunday/Monday bullets from the most recent Friday log's Mid-Week Update section."""
    if not notion_key:
        return {}

    weekday = local_now.weekday()  # Mon=0 ... Sun=6
    # Calculate how many days back Friday was
    if weekday == 4:    # Friday — use today
        days_back = 0
    elif weekday == 5:  # Saturday
        days_back = 1
    elif weekday == 6:  # Sunday
        days_back = 2
    elif weekday == 0:  # Monday
        days_back = 3
    else:
        # Tue/Wed/Thu — no Friday update exists yet this week
        return {}

    friday_dt = local_now - timedelta(days=days_back)
    friday_date_str = (
        friday_dt.strftime("%A, %B %-d")
        if os.name != "nt"
        else friday_dt.strftime("%A, %B %d").replace(" 0", " ")
    )

    page_id = find_yesterday_log_page_id(notion_key, friday_date_str)
    if not page_id:
        return {}

    import requests as _req
    headers = {
        "Authorization": f"Bearer {notion_key}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    all_blocks: list[dict] = []
    cursor = None
    try:
        while True:
            params: dict = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = _req.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=headers,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            all_blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
    except Exception as e:
        print(f"[Warning] Could not fetch Friday page blocks: {e}")
        return {}

    _MIDWEEK_HEADING = "📆 Mid-Week Update"
    _MIDWEEK_DAYS = (
        "Friday → Light Tasks",
        "Saturday → Personal Project",
        "Sunday → Weekly Reset, Reflect & Planning",
        "Monday → Prep & Deep Work",
    )

    section_start = None
    for i, block in enumerate(all_blocks):
        if block.get("type") == "heading_2" and _block_plain_text(block) == _MIDWEEK_HEADING:
            section_start = i + 1
            break

    if section_start is None:
        return {}

    result: dict[str, list[str]] = {}
    current_day: str | None = None
    i = section_start
    while i < len(all_blocks):
        block = all_blocks[i]
        btype = block.get("type", "")
        text = _block_plain_text(block)

        if btype in ("heading_2", "divider"):
            break

        if btype == "heading_3" and text in _MIDWEEK_DAYS:
            current_day = text
            result.setdefault(current_day, [])
            i += 1
            continue

        if btype == "bulleted_list_item" and current_day is not None:
            if text:
                result.setdefault(current_day, []).append(text)
            i += 1
            continue

        i += 1

    return {day: tasks for day, tasks in result.items() if tasks}


# ── Claude AI ─────────────────────────────────────────────────────────────────

def _pick_daily_quote(date: datetime, quotes_text: str) -> str:
    lines = [
        line.strip()
        for line in quotes_text.splitlines()
        if line.strip() and line.strip().startswith('"')
    ]
    if not lines:
        return '"Do the work." — Unknown'
    local = date.astimezone(TORONTO_TZ)
    date_seed = local.strftime("%Y-%m-%d")
    index = int(hashlib.md5(date_seed.encode()).hexdigest(), 16) % len(lines)
    return lines[index]


_AXIOM_SECTION_STOP_WORDS = {
    "identity statement", "values", "non-negotiables",
    "non negotiables", "ideal persona", "resources",
}


def _pick_daily_axiom(ideal_persona_text: str, date: datetime) -> str | None:
    lines = ideal_persona_text.splitlines()
    axioms: list[str] = []
    in_axioms = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        clean = stripped.lstrip("#").strip()

        if clean.lower() == "axioms":
            in_axioms = True
            continue

        if in_axioms and clean.lower() in _AXIOM_SECTION_STOP_WORDS:
            break

        if in_axioms and len(clean) > 10:
            axioms.append(clean)

    if not axioms:
        return None

    local = date.astimezone(TORONTO_TZ)
    date_seed = local.strftime("%Y-%m-%d") + "_axiom"
    index = int(hashlib.md5(date_seed.encode()).hexdigest(), 16) % len(axioms)
    return axioms[index]


# Quarter boundaries for tone calibration (school-year quarters).
_QUARTER_RANGES: list[tuple[tuple[int, int], tuple[int, int]]] = [
    ((1,  1),  (3, 26)),   # Q1
    ((3, 27),  (6, 22)),   # Q2
    ((6, 23),  (9,  2)),   # Q3
    ((9,  3), (12, 31)),   # Q4
]

_QUARTER_GUIDANCE: dict[str, str] = {
    "early":  "plenty of runway remains; frame milestones as upcoming, not overdue",
    "middle": "some milestones should be in progress; balance what's started with what's still ahead",
    "late":   "limited time remains; direct accountability framing is appropriate for milestones not yet started",
}


def _quarter_progress_descriptor(local_date: datetime) -> str:
    """Returns 'early', 'middle', or 'late' based on which third of the current quarter today falls into."""
    today = local_date.date() if hasattr(local_date, "date") else local_date
    year = today.year
    for start_md, end_md in _QUARTER_RANGES:
        q_start = date(year, start_md[0], start_md[1])
        q_end   = date(year, end_md[0],   end_md[1])
        if q_start <= today <= q_end:
            total   = (q_end - q_start).days + 1
            elapsed = (today - q_start).days + 1
            third   = total / 3
            if elapsed <= third:
                return "early"
            elif elapsed <= 2 * third:
                return "middle"
            else:
                return "late"
    return "middle"


_SYSTEM_PROMPT = """You are Jimit's sharp personal productivity assistant. Generate a structured daily plan.

USER PROFILE:
- Deep work: one 90-min block on weekdays, two 90-min afternoon blocks on weekends (visible on the Google Calendar)

---
STUDENT IDENTITY AND GOALS — SOURCE OF TRUTH

The user prompt contains four Notion pages that define who Jimit is and
what he is working toward:

- "Ideal Persona": his identity statement, values, non-negotiables,
  and axioms. Use these to inform the tone and content of the
  ### CLOSING section. The closing motivation should feel like it
  comes from someone who knows his actual values — not generic
  productivity advice. Reference an axiom or identity principle
  directly when relevant. Never use generic phrases like
  "keep working hard" or "stay focused."

- "Current Plan (active milestones)": his quarter-by-quarter academic
  and extracurricular roadmap through Grade 12. Use this to make the
  ### OPENING_BRIEFING section feel grounded in his real current
  priorities. If a calendar event or task connects to a plan milestone,
  surface that connection. Do not recite the plan — use it as background
  awareness.

- "Goals": his short-term and long-term goals. Use this alongside the
  Plan to inform the ### CLOSING section — specifically the MOTIVATION
  subsection. Motivation should reference real goals, not invented ones.

- "Achievement & Activity Log": his actual achievements to date. Use
  this as background context for who he is. Do not list achievements in
  the briefing — use them so the writing feels like it knows his history.

Never invent goals, activities, or identity details that are not in
these pages. If a page is marked "Not available", omit references to
it and fall back to general framing for that section only.
---

TONE: Direct and focused, like a sharp mentor. Never generic. Ground every section in the Notion context provided — never invent details. No clichés like "you've got this" or "believe in yourself."

TASK ENERGY (when not specified): coding/writing/deep analysis → high; emails/reviews/light reading → medium; admin/scheduling/chores → low.
Always put high-energy tasks first in the deep work block, low-energy tasks last."""

def _format_tasks_for_prompt(tasks: dict[str, list[dict]]) -> str:
    today_local = datetime.now(TORONTO_TZ)
    today_iso = today_local.strftime("%Y-%m-%d")
    today_abbr = today_local.strftime("%b %-d") if os.name != "nt" else today_local.strftime("%b %d").replace(" 0", " ")
    today_full = today_local.strftime("%B %-d") if os.name != "nt" else today_local.strftime("%B %d").replace(" 0", " ")

    def _is_due_today(due_val: str | None) -> bool:
        if not due_val:
            return False
        due_lower = due_val.lower()
        return (
            "today" in due_lower
            or today_iso in due_val
            or today_abbr in due_val
            or today_full in due_val
        )

    lines = []
    for list_name in ["High Priority", "Current"]:
        items = tasks.get(list_name, [])
        if not items:
            continue
        lines.append(f"\n### {list_name} Tasks")
        for t in items:
            time_str = f"{t['time_min']} min" if t["time_min"] else "unknown duration"
            energy_str = t["energy"] or "unknown energy"
            due_str = f", due {t['due']}" if t["due"] else ""
            due_today_suffix = " ⚡ DUE TODAY" if _is_due_today(t.get("due")) else ""
            lines.append(f"- {t['title']} ({time_str}, {energy_str}{due_str}){due_today_suffix}")
            if t["notes"]:
                lines.append(f"  Notes: {t['notes'][:200]}")
    return "\n".join(lines) if lines else "No tasks found."


def _format_events_for_prompt(events: list[dict]) -> str:
    if not events:
        return "No calendar events today."
    lines = []
    for e in events:
        lines.append(f"- {e['summary']}: {e['start']} → {e['end']}")
        if e.get("description"):
            lines.append(f"  {e['description'][:150]}")
    return "\n".join(lines)


def _parse_sections(raw: str) -> dict:
    """
    Parse Claude's response into the five named sections.
    Handles BOTH output formats Claude may use:
      - ### OPENING_BRIEFING  (the format requested in the user_prompt)
      - # 📋 Daily Briefing   (what Claude outputs when system prompt uses emoji names)
    """
    KEY_ORDER = ["opening_briefing", "habit_recap", "your_day", "on_your_plate", "closing"]

    # Format 1: exact ### MARKER (what was requested)
    MARKER_PATTERNS = [
        ("opening_briefing", re.compile(r"###\s*\**\s*OPENING_BRIEFING\s*\**", re.IGNORECASE)),
        ("habit_recap",      re.compile(r"###\s*\**\s*HABIT_RECAP\s*\**",      re.IGNORECASE)),
        ("your_day",         re.compile(r"###\s*\**\s*YOUR_DAY\s*\**",          re.IGNORECASE)),
        ("on_your_plate",    re.compile(r"###\s*\**\s*ON_YOUR_PLATE\s*\**",     re.IGNORECASE)),
        ("closing",          re.compile(r"###\s*\**\s*CLOSING\s*\**",           re.IGNORECASE)),
    ]

    # Format 2: # Emoji Name (what Claude outputs due to system prompt naming conflict)
    # Patterns match the ENTIRE header line to avoid emoji variation-selector edge cases
    EMOJI_PATTERNS = [
        ("opening_briefing", re.compile(r"^#{1,3}[^\S\n]*📋[^\n]*Daily Briefing[^\n]*$",  re.IGNORECASE | re.MULTILINE)),
        ("habit_recap",      re.compile(r"^#{1,3}[^\S\n]*📊[^\n]*Habit Recap[^\n]*$",     re.IGNORECASE | re.MULTILINE)),
        ("your_day",         re.compile(r"^#{1,3}[^\S\n]*🗓[^\n]*Your Day[^\n]*$",         re.IGNORECASE | re.MULTILINE)),
        ("on_your_plate",    re.compile(r"^#{1,3}[^\S\n]*✅[^\n]*On Your Plate[^\n]*$",   re.IGNORECASE | re.MULTILINE)),
        ("closing",          re.compile(r"^#{1,3}[^\S\n]*💬[^\n]*To Close[^\n]*$",        re.IGNORECASE | re.MULTILINE)),
    ]

    def _find_header_spans(patterns):
        spans = {}
        for key, pat in patterns:
            m = pat.search(raw)
            if m:
                spans[key] = (m.start(), m.end())
        return spans

    header_spans = _find_header_spans(MARKER_PATTERNS)
    format_used = "MARKER"
    if not header_spans:
        header_spans = _find_header_spans(EMOJI_PATTERNS)
        format_used = "EMOJI"

    if not header_spans:
        # Neither format matched — log a diagnostic to help future debugging
        first_line = raw.splitlines()[0] if raw.strip() else "(empty response)"
        print(f"[Parse Error] No section headers found in Claude response.")
        print(f"[Parse Error] Response length: {len(raw)} chars")
        print(f"[Parse Error] First line: {first_line!r}")
        print(f"[Parse Error] First 300 chars: {raw[:300]!r}")
        print(f"[Parse Error] To fix: add a pattern to EMOJI_PATTERNS in _parse_sections() that matches the header format above.")
        return {k: "" for k in KEY_ORDER}

    print(f"[Parse] Matched using {format_used} format. Found {len(header_spans)}/5 sections: {list(header_spans.keys())}")

    sorted_keys = sorted(header_spans.items(), key=lambda x: x[1][0])
    result = {}

    for i, (key, (header_start, content_start)) in enumerate(sorted_keys):
        if i + 1 < len(sorted_keys):
            next_key = sorted_keys[i + 1][0]
            end = header_spans[next_key][0]  # end at START of next header line, not its content
        else:
            end = len(raw)
        result[key] = raw[content_start:end].strip()

    for k in KEY_ORDER:
        result.setdefault(k, "")
    return result


def _truncate(text: str, max_chars: int) -> str:
    """Truncate Notion page text to a safe character limit for the prompt."""
    if not text or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated for length]"


def generate_plan(
    events: list[dict],
    tasks: dict[str, list[dict]],
    notion_context: dict[str, str],
    date: datetime,
    daily_quote: str,
    school_day: bool,
    habit_results: dict[str, list[str]],
    one_thing_differently: str | None = None,
    must_do_tasks: list[str] | None = None,
    notes: str | None = None,
    week_ahead_tasks: dict | None = None,
    total_habits: int = 9,
) -> dict:
    _local_date = date.astimezone(TORONTO_TZ) if date.tzinfo else date.replace(tzinfo=timezone.utc).astimezone(TORONTO_TZ)
    date_str = _local_date.strftime("%A, %B %-d") if os.name != "nt" else _local_date.strftime("%A, %B %d").replace(" 0", " ")
    plan_quote = daily_quote

    plan_date = date.astimezone(TORONTO_TZ) if date.tzinfo else date.replace(tzinfo=timezone.utc).astimezone(TORONTO_TZ)
    daily_axiom = _pick_daily_axiom(
        notion_context.get("ideal_persona", ""),
        plan_date,
    )

    local_date = plan_date
    today_theme_name = DAY_THEMES.get(local_date.weekday(), "")
    is_summer = SUMMER_START <= local_date.date() <= SUMMER_END
    if school_day:
        day_type_note = (
            "TODAY IS A SCHOOL DAY. Use the School Day Ideal Routine table from "
            "Habits & Routine. There is exactly ONE deep work block — do not create "
            "any other deep work blocks."
        )
    elif is_summer and local_date.weekday() <= 4:
        day_type_note = (
            "TODAY IS A SUMMER WEEKDAY. Use the Summer Day Ideal Routine table from "
            "Habits & Routine (under the Ideal Routine → Summer Day toggle). Google "
            "Calendar is the primary source — all existing calendar events stay as-is. "
            "The Summer Day routine only fills gaps for times not already covered by "
            "calendar events. There are TWO deep work blocks. Do not create school-style structure."
        )
    elif local_date.weekday() <= 4:
        day_type_note = (
            "TODAY IS A NON-SCHOOL WEEKDAY (day off or holiday). Use the School Day "
            "Ideal Routine table from Habits & Routine. There is exactly ONE deep "
            "work block — do not create two deep work blocks."
        )
    else:
        day_type_note = (
            "TODAY IS A WEEKEND. Use the Week-end Day Ideal Routine table from "
            "Habits & Routine. There are two deep work blocks — do not create only one."
        )

    quarter_descriptor = _quarter_progress_descriptor(local_date)
    quarter_guidance = _QUARTER_GUIDANCE[quarter_descriptor]

    completed = habit_results.get("completed", []) if habit_results else []
    missed = habit_results.get("missed", []) if habit_results else []
    habits_completed_str = ", ".join(completed) if completed else "(none)"
    habits_missed_str = ", ".join(missed) if missed else "(none)"
    n_done = len(completed)
    must_do_tasks = must_do_tasks or {}
    if isinstance(must_do_tasks, list):
        must_do_tasks = {"high": must_do_tasks, "medium": [], "low": []}

    briefing_eod_instruction = ""
    if one_thing_differently:
        briefing_eod_instruction = f"""
- Include a single sentence in the Daily Briefing that naturally references yesterday's note — framed as a forward-looking intention for today, not a criticism of yesterday. Example framing: "Yesterday you noted you wanted to {one_thing_differently} today." Keep it to one sentence woven into the briefing paragraph."""

    on_your_plate_priority_instruction = ""
    if must_do_tasks and any(must_do_tasks.values()):
        high = "\n".join(f"- 🔴 {t}" for t in must_do_tasks.get("high", []))
        med  = "\n".join(f"- 🟡 {t}" for t in must_do_tasks.get("medium", []))
        low  = "\n".join(f"- 🟢 {t}" for t in must_do_tasks.get("low", []))
        tiers = "\n".join(filter(None, [high, med, low]))
        on_your_plate_priority_instruction = f"""🔺 Priority from last night:
{tiers}

"""

    _NOTION_LIMITS = {
        "habits":        8000,
        "ideal_persona": 3000,
        "plan":          3000,
        "goals":         2000,
        "activity_log":  2000,
        "quotes":        4000,
    }

    def _notion_section(key: str) -> str:
        value = notion_context.get(key, "")
        if not value or not value.strip():
            return "Not available"
        limit = _NOTION_LIMITS.get(key, 3000)
        return _truncate(value, limit)

    week_ahead_tasks = week_ahead_tasks or {}
    today_day_name = local_date.strftime("%A")
    today_task_list = next((v for k, v in week_ahead_tasks.items() if k.startswith(today_day_name)), [])
    if today_task_list:
        today_tasks_formatted = "\n".join(f"- {t}" for t in today_task_list)
    else:
        today_tasks_formatted = "(none assigned)"

    today_idx = next((i for i, d in enumerate(_WEEK_AHEAD_DAYS) if d.startswith(today_day_name)), -1)
    remaining_parts: list[str] = []
    for day in _WEEK_AHEAD_DAYS[today_idx + 1:]:
        day_tasks = week_ahead_tasks.get(day, [])
        if day_tasks:
            bullets = "\n".join(f"- {t}" for t in day_tasks)
            remaining_parts.append(f"{day}:\n{bullets}")
    remaining_days_formatted = "\n\n".join(remaining_parts) if remaining_parts else "(none)"

    # Lazy import avoids circular dependency (memory_context.py imports from planner.py)
    recent_context = "(no recent context available)"
    try:
        from memory_context import fetch_memory_context_text
        _mc_notion_key = os.environ.get("NOTION_API_KEY", "")
        if _mc_notion_key:
            recent_context = _truncate(fetch_memory_context_text(_mc_notion_key), 2000)
    except Exception as _mc_err:
        print(f"[Warning] Memory context fetch failed — proceeding without it: {_mc_err}")

    summer_routine_section = ""
    if is_summer and local_date.weekday() <= 4:
        _sr_notion_key = os.environ.get("NOTION_API_KEY", "")
        if _sr_notion_key:
            try:
                _sr_text = fetch_summer_routine(_sr_notion_key)
                if _sr_text and _sr_text.strip():
                    summer_routine_section = (
                        "\n\n## Summer Day Routine (from Notion)\n"
                        + _truncate(_sr_text, 4000)
                    )
                    print(f"[Summer] Routine fetched ({len(_sr_text)} chars). Preview: {_sr_text[:200]!r}")
            except Exception as _sr_err:
                print(f"[Warning] Summer routine fetch failed: {_sr_err}")

    user_prompt = f"""Today is {date_str}.

## Habits & Routine
{_notion_section('habits')}{summer_routine_section}

## Ideal Persona
{_notion_section('ideal_persona')}

## Current Plan (active milestones)
{_notion_section('plan')}

## Goals
{_notion_section('goals')}

## Achievement & Activity Log
{_notion_section('activity_log')}

## Google Calendar Events
{_format_events_for_prompt(events)}

## Tasks
{_format_tasks_for_prompt(tasks)}

## Week Ahead (from Sunday's plan)
Today is {today_day_name}.

### Tasks assigned for today ({today_day_name}):
{today_tasks_formatted}

### Tasks for the rest of the week:
{remaining_days_formatted}

## Yesterday's habit completion (for recap)
Yesterday's completed habits: [{habits_completed_str}]
Yesterday's missed habits: [{habits_missed_str}]
Total completed: {n_done} out of {total_habits}

## Day Type
{day_type_note}

## Day Theme
Today's theme is: {today_theme_name}

## Quarter Progress
{quarter_descriptor} in the current quarter — {quarter_guidance}

## Recent Context
{recent_context}

---

Produce EXACTLY these six sections with these exact headers (no other headers):

### OPENING_BRIEFING
3–5 sentence paragraph. Tone: focused and direct, like a sharp mentor.

Do all of the following:
- Reference the kind of day it is and the most important task.
- The first or second sentence of the briefing should naturally incorporate today's theme (provided in the "Day Theme" section above). Do not quote it mechanically — connect it to the most important task or the shape of the day. For example, on a "Catch Up" day, note what is being caught up on. On a "Deep Work" day, name what the deep work is pointed at. On a "Personal Project" day, name the project. Make it specific to what is actually on the plate today.
- Look at the Current Plan (active milestones) provided in the Notion
  context. Identify which quarter we are currently in based on today's
  date (the plan is organized by grade year and quarter). Find the
  milestones listed for the current quarter. Reference 1–2 of the most
  active or time-sensitive milestones in the briefing — not as a list,
  but woven naturally into the paragraph. Calibrate the tone using the
  Quarter Progress signal: early → forward-looking (milestones are
  upcoming, not overdue); middle → balanced; late → direct accountability
  is appropriate.
- If the "Recent Context" section mentions something time-sensitive or notable for the next 1–2 days — an event, an upcoming plan, something from journal notes — it's fine to weave that naturally into the briefing. Only do this if it's genuinely relevant to today; if Recent Context is empty or not applicable, skip it and default to milestone language.
- If one_thing_differently was set by last night's End-of-Day Report,
  include one sentence weaving it in as a forward intention for today.{briefing_eod_instruction}
- No bullet points. No generic phrases.

### HABIT_RECAP
1–2 sentences maximum. Give ONE concrete, specific tip for the single hardest habit to fix today, based on what was missed yesterday.
If all habits were completed, skip the tip and raise the standard in one sentence (e.g. "keep the streak — nothing slips today").
Tone: direct mentor, not cheerleader.
Do NOT list or restate which habits were completed or missed — that is already shown as a checklist above this section.

### YOUR_DAY
List every event from today's Google Calendar in chronological order by start time.

Format each line exactly as:
9:00 AM – 10:30 AM · [event title verbatim]

Rules:
- Use the event's exact title — do not rename, reformat, or add emoji unless the original event title already includes them.
- Include ALL events without exception: Sleep, Morning Routine, Breakfast, Lunch, Dinner, Night Routine, Wind Down, Deep Work blocks, and any others.
- Deep Work blocks are ordinary events — no divider lines, no nested task lists, no special heading format.
- Do not add, skip, merge, or reorder events.
- All-day events (no time): list them at the top as "All day · [title]".

### ON_YOUR_PLATE
{on_your_plate_priority_instruction}CURRENT
- [Task name] — ~[X min]

(Write "Nothing here — you're clear." under CURRENT if that list is empty)

HIGH PRIORITY
- [Task name] — [X min], due [date] (omit the date if no due date is set for this task)

(Write "Nothing here — you're clear." under HIGH PRIORITY if that list is empty)

### CLOSING
REMARKS: [2 sentences wrapping up the plan, noting anything to watch out for or finish strong on]

MOTIVATION: Write 2–3 sentences of specific, earned motivation.
Draw directly from the Ideal Persona axioms and identity statement
provided in the Notion context — do not invent or genericize.
Connect today's tasks or calendar to a real goal from the Goals or
Plan pages where a natural connection exists.
Use the Quarter Progress signal to calibrate tone: early in a quarter → frame
as upcoming goals with runway still ahead; late → direct about what needs to
happen now.
Never use the phrases "keep working hard", "stay focused",
"you've got this", or any generic productivity filler.
The closing should feel like it was written by someone who has
read the plan and knows what this specific day is building toward.

KEY_INSIGHT: [One sharp, specific sentence — a mini-lesson or reframe relevant to today's tasks. Mentor-voice, not motivational-poster-voice]
"""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=8000,
        temperature=0,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = message.content[0].text
    print(f"[Debug] stop_reason={message.stop_reason} | raw_len={len(raw)} | preview={raw[:120]!r}")
    sections = _parse_sections(raw)

    result = {
        "date_str": date_str,
        "quote": plan_quote,
        "briefing": sections.get("opening_briefing", ""),
        "habit_recap": sections.get("habit_recap", ""),
        "your_day": sections.get("your_day", ""),
        "on_your_plate": sections.get("on_your_plate", ""),
        "closing": sections.get("closing", ""),
        "is_school_day": school_day,
        "daily_axiom": daily_axiom,
    }
    if notes:
        result["notes_from_last_night"] = notes
    return result


# ── Main entry ────────────────────────────────────────────────────────────────

def run_planner() -> tuple[
    dict,
    Credentials,
    datetime,
    bool,
    date,
    dict[str, list[str]],
    dict,
    list[str],
    dict[str, str | None],
]:
    today = datetime.now(tz=timezone.utc)
    local_now = today.astimezone(TORONTO_TZ)
    local_today = local_now.date()
    yesterday_date = (local_now - timedelta(days=1)).date()

    print("[*] Authenticating with Google...")
    try:
        creds = get_google_credentials()
    except Exception as e:
        print(f"[Error] Google authentication failed: {e}")
        raise

    print("[*] Fetching Google Calendar events...")
    events = fetch_calendar_events(creds, today)
    print(f"    Found {len(events)} event(s).")
    school_day = is_school_day(events)
    print(f"    Detected: {'school day' if school_day else 'non-school day'}.")

    print("[*] Fetching Google Tasks...")
    tasks = fetch_tasks(creds)
    hp = len(tasks.get("High Priority", []))
    cur = len(tasks.get("Current", []))
    print(f"    High Priority: {hp} task(s), Current: {cur} task(s).")

    print("[*] Reading Notion pages...")
    notion_key = os.environ.get("NOTION_API_KEY", "")
    if not notion_key:
        raise EnvironmentError("NOTION_API_KEY not set in .env")
    notion_context = fetch_notion_context(notion_key)

    today_is_sunday = local_now.weekday() == 6
    checklist_labels, label_to_column = build_habit_checklist(
        notion_context.get("habits", ""), is_sunday=today_is_sunday
    )
    print(f"[*] Today's habit checklist ({len(checklist_labels)} items): {checklist_labels}")
    print(f"[*] Tracked DB columns: {sorted({c for c in label_to_column.values() if c})}")

    daily_quote = _pick_daily_quote(today, notion_context.get("quotes", ""))
    print(f"[*] Today's quote: {daily_quote[:60]}...")

    print("[*] Reading yesterday's habit checklist from Notion...")
    yesterday_dt = local_now - timedelta(days=1)
    yesterday_date_str = (
        yesterday_dt.strftime("%A, %B %-d")
        if os.name != "nt"
        else yesterday_dt.strftime("%A, %B %d").replace(" 0", " ")
    )
    yesterday_page_id = find_yesterday_log_page_id(notion_key, yesterday_date_str)
    if yesterday_page_id:
        habit_results = read_yesterday_habits(notion_key, yesterday_page_id)
        yesterday_checklist, _ = build_habit_checklist(
            notion_context.get("habits", ""), is_sunday=(yesterday_dt.weekday() == 6)
        )
        total_habits = len(yesterday_checklist)
        print(f"[*] Found yesterday's log — habits completed: {len(habit_results.get('completed', []))}/{total_habits}")
    else:
        habit_results = {
            "completed": [],
            "missed": [],
            "one_thing_differently": None,
            "must_do_tasks": [],
            "notes": None,
            "inner_state": None,
            "performance": None,
        }
        total_habits = len(checklist_labels)
        print("[*] No yesterday log page found — habit recap will be skipped.")

    one_thing_differently = habit_results.get("one_thing_differently")
    must_do_tasks = habit_results.get("must_do_tasks") or []
    notes = habit_results.get("notes")

    week_ahead_tasks = fetch_week_ahead_tasks(notion_key, local_now)
    friday_tasks = fetch_friday_tasks(notion_key, local_now)
    # Friday's mid-week update takes priority over Sunday's plan for Sat/Sun/Mon
    for key in (
        "Friday → Light Tasks",
        "Saturday → Personal Project",
        "Sunday → Weekly Reset, Reflect & Planning",
        "Monday → Prep & Deep Work",
    ):
        if key in friday_tasks:
            week_ahead_tasks[key] = friday_tasks[key]

    print("[*] Generating daily plan with Claude...")
    plan = generate_plan(
        events,
        tasks,
        notion_context,
        today,
        daily_quote,
        school_day,
        habit_results,
        one_thing_differently=one_thing_differently,
        must_do_tasks=must_do_tasks,
        notes=notes,
        week_ahead_tasks=week_ahead_tasks,
        total_habits=total_habits,
    )
    print("[*] Plan generated.")

    return (
        plan,
        creds,
        today,
        school_day,
        yesterday_date,
        habit_results,
        friday_tasks,
        checklist_labels,
        label_to_column,
    )
