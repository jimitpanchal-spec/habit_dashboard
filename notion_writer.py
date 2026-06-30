import re
import datetime
import os
import unicodedata

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

AUTOMATION_LOGS_PAGE_ID = "344f3b1a634a80ec86f0f5b7205f4751"
# Must match planner.IDEAL_PERSONA_PAGE_ID
IDEAL_PERSONA_PAGE_ID = "35bf3b1a634a80e4af33f01d24ab3f8b"
HABIT_TRACKER_DATABASE_ID = "36af3b1a-634a-81dc-8f16-f4cc4ae1a8eb"
HABIT_TRACKER_DATE_PROPERTY = "Date"

# Keyword (lowercase substring) -> Habit & Mood Tracker DB column.
# First match wins. Habits matching no keyword are checklist-only.
_HABIT_KEYWORD_TO_COLUMN: list[tuple[str, str]] = [
    ("read day plan",  "Read Day Plan"),
    ("sleep",          "Sleep"),
    ("social media",   "Social Media"),
    ("phone",          "Put Phone away"),
    ("work session",   "Work Session Plan"),
    ("deep work",      "Deep Work"),
    ("learn",          "Learn 1 Thing"),
    ("work out",       "Work Out"),
    ("workout",        "Work Out"),
    ("exercise",       "Work Out"),
]

# Habits never written to the Habit & Mood Tracker DB.
_UNTRACKED_KEYWORDS = ("end-of-day", "bad habit")

# Habits only added to the checklist on Sundays.
_WEEKLY_ONLY_KEYWORDS = ("weekly check-in",)


def _is_emoji_lead(ch: str) -> bool:
    return unicodedata.category(ch) in ("So", "Sk")


def parse_new_habits(habits_text: str) -> list[str]:
    """Extract habit checklist labels from the '## New Habits' section of
    the Habits & Routine page text (as returned by _fetch_page_text).
    A line is a habit label if it starts with an emoji character.
    Description lines (one plain-text line following a label) are skipped.
    Stops at the first line that is neither a label nor a description of
    the immediately preceding label (i.e. the next section heading)."""
    if not habits_text:
        return []

    lines = habits_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == "new habits":
            start = i + 1
            break
    if start is None:
        return []

    labels: list[str] = []
    awaiting_description = False
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if _is_emoji_lead(stripped[0]):
            labels.append(stripped)
            awaiting_description = True
        elif awaiting_description:
            awaiting_description = False
        else:
            break
    return labels


def habit_label_to_column(label: str) -> str | None:
    lower = label.lower()
    if any(kw in lower for kw in _UNTRACKED_KEYWORDS):
        return None
    if any(kw in lower for kw in _WEEKLY_ONLY_KEYWORDS):
        return None
    for keyword, column in _HABIT_KEYWORD_TO_COLUMN:
        if keyword in lower:
            return column
    return None


def build_habit_checklist(habits_text: str, is_sunday: bool) -> tuple[list[str], dict[str, str | None]]:
    """Returns (checklist_labels, label_to_column) for a given day.
    checklist_labels: ordered labels to render as to_do blocks today
    (Weekly Check-in only included if is_sunday).
    label_to_column: every parsed label mapped to its DB column, or None."""
    all_labels = parse_new_habits(habits_text)
    checklist_labels = []
    for label in all_labels:
        if any(kw in label.lower() for kw in _WEEKLY_ONLY_KEYWORDS):
            if is_sunday:
                checklist_labels.append(label)
            continue
        checklist_labels.append(label)
    label_to_column = {label: habit_label_to_column(label) for label in all_labels}
    return checklist_labels, label_to_column


def ensure_habit_tracker_schema(notion_key: str, columns: list[str]) -> None:
    """Add any missing checkbox columns to the Habit & Mood Tracker DB.
    Never removes or renames existing columns. Failures are logged, not raised —
    a schema-update problem shouldn't block the rest of the run."""
    import requests as _req
    headers = {
        "Authorization": f"Bearer {notion_key}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    try:
        resp = _req.get(
            f"https://api.notion.com/v1/databases/{HABIT_TRACKER_DATABASE_ID}",
            headers=headers,
        )
        resp.raise_for_status()
        existing = set(resp.json().get("properties", {}).keys())
        missing = [c for c in columns if c and c not in existing]
        if not missing:
            return
        print(f"[*] Adding new Habit Tracker columns: {missing}")
        resp = _req.patch(
            f"https://api.notion.com/v1/databases/{HABIT_TRACKER_DATABASE_ID}",
            headers=headers,
            json={"properties": {c: {"checkbox": {}} for c in missing}},
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[Warning] Could not update Habit Tracker schema: {e}")
# ── Block builders ────────────────────────────────────────────────────────────

def _text(content: str, bold=False, italic=False, color="default") -> dict:
    ann = {"bold": bold, "italic": italic, "color": color}
    return {"type": "text", "text": {"content": content}, "annotations": ann}


def _paragraph(text: str, bold=False, italic=False) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [_text(text, bold=bold, italic=italic)]},
    }


def _todo(text: str, checked: bool = False) -> dict:
    return {
        "object": "block",
        "type": "to_do",
        "to_do": {
            "rich_text": [_text(text)],
            "checked": checked,
        },
    }


def _heading2(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [_text(text)]},
    }


def _heading3(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": [_text(text)]},
    }


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _callout(text: str, emoji: str = "📋") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [_text(text)],
            "icon": {"type": "emoji", "emoji": emoji},
            "color": "gray_background",
        },
    }


def _bulleted(text: str, bold=False) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [_text(text, bold=bold)]},
    }


def _quote(text: str) -> dict:
    return {
        "object": "block",
        "type": "quote",
        "quote": {"rich_text": [_text(text)]},
    }


def _ideal_persona_mention(page_id: str = IDEAL_PERSONA_PAGE_ID) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "mention",
                    "mention": {
                        "type": "page",
                        "page": {"id": page_id.replace("-", "")},
                    },
                }
            ]
        },
    }


def _link_to_page(page_id: str) -> dict:
    return {
        "object": "block",
        "type": "link_to_page",
        "link_to_page": {"type": "page_id", "page_id": page_id},
    }


# ── Section block builders ────────────────────────────────────────────────────

def _build_your_day_blocks(schedule_text: str) -> list[dict]:
    blocks = []
    for line in schedule_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        unique_chars = set(stripped) - {" "}
        if unique_chars and unique_chars.issubset(set("─━—-")):
            blocks.append(_divider())
        elif "↳" in stripped:
            blocks.append(_bulleted(stripped))
        elif "DEEP WORK BLOCK" in stripped.upper():
            blocks.append(_heading3(stripped))
        else:
            blocks.append(_paragraph(stripped))
    return blocks


def _build_on_your_plate_blocks(plate_text: str) -> list[dict]:
    blocks = []
    for line in plate_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Strip bold/italic markers before checking label — Claude wraps
        # "HIGH PRIORITY" and "CURRENT" in **...** which breaks the match
        clean = re.sub(r"\*+", "", stripped).strip()
        if clean.upper() in ("HIGH PRIORITY", "CURRENT"):
            blocks.append(_heading3(clean))
        elif stripped.startswith("- "):
            blocks.append(_bulleted(stripped[2:]))
        else:
            blocks.append(_paragraph(stripped))
    return blocks


def _build_closing_blocks(closing_text: str) -> list[dict]:
    label_order = ("REMARKS", "MOTIVATION", "KEY_INSIGHT")
    label_emoji: dict[str, str | None] = {
        "REMARKS": None,
        "MOTIVATION": "🎯",
        "KEY_INSIGHT": "💡",
    }

    # Strip bold/italic markers — Claude wraps labels in **...** and they
    # render as literal asterisks in Notion paragraph/callout blocks
    clean = re.sub(r"\*+", "", closing_text).strip()

    # Split on label markers wherever they appear — Claude sometimes puts
    # REMARKS, MOTIVATION, KEY_INSIGHT all on one line rather than separate
    # lines, which breaks the old line-by-line parser
    parts: dict[str, str] = {lbl: "" for lbl in label_order}
    pattern = re.compile(r"(REMARKS|MOTIVATION|KEY_INSIGHT)\s*:", re.IGNORECASE)
    segments = pattern.split(clean)
    # After split with a capture group, segments = [pre, "LABEL", text, "LABEL", text, ...]
    i = 1
    while i < len(segments) - 1:
        label = segments[i].upper()
        content = segments[i + 1].strip()
        if label in parts:
            parts[label] = content
        i += 2

    blocks = []
    for label in label_order:
        text = parts[label].strip()
        if not text:
            continue
        emoji = label_emoji[label]
        if emoji:
            blocks.append(_callout(text, emoji=emoji))
        else:
            blocks.append(_paragraph(text))
    return blocks


def _build_end_of_day_report_blocks(is_sunday: bool, is_friday: bool = False, friday_sunday_tasks: dict | None = None) -> list[dict]:
    blocks: list[dict] = [
        _heading2("📓 End-of-Day Report"),
        _divider(),

        # Ratings row
        _paragraph("🧠 Inner state today (1–10):", bold=True),
        _paragraph(""),
        _paragraph("⚡ Performance today (1–10):", bold=True),
        _paragraph(""),
        _todo("Did I become 1% better today than I was yesterday?"),
        _paragraph("💭 What's one thing I want to do differently tomorrow?"),
        _paragraph(""),
        _paragraph("✅ Tasks for Tomorrow", bold=True),
        _paragraph("🔴 High Priority", bold=True),
        _bulleted(""),
        _paragraph("🟡 Medium Priority", bold=True),
        _bulleted(""),
        _paragraph("🟢 Low-Effort", bold=True),
        _bulleted(""),
        _paragraph("🗒️ Notes"),
        _paragraph(
            "These notes carry forward to tomorrow's page.",
            italic=True,
        ),
        _paragraph(""),
        _paragraph("📔 Journal — Today's Reflection", bold=True),
        _paragraph(
            "Free reflection on your day, thoughts, feelings. "
            "This stays here — it does not carry forward.",
            italic=True,
        ),
        _paragraph(""),
    ]

    if is_sunday:
        blocks.extend([
            _divider(),
            _heading2("🧠 Brain Dump"),
            _paragraph(
                "Dump everything on your mind — tasks, ideas, obligations. "
                "You'll sort them below.",
                italic=True,
            ),
            {
                "object": "block",
                "type": "table",
                "table": {
                    "table_width": 4,
                    "has_column_header": True,
                    "has_row_header": False,
                    "children": [
                        {
                            "object": "block",
                            "type": "table_row",
                            "table_row": {
                                "cells": [
                                    [{"type": "text", "text": {"content": "Task"}, "annotations": {"bold": True}}],
                                    [{"type": "text", "text": {"content": "Class / Category"}, "annotations": {"bold": True}}],
                                    [{"type": "text", "text": {"content": "Deadline"}, "annotations": {"bold": True}}],
                                    [{"type": "text", "text": {"content": "Notes"}, "annotations": {"bold": True}}],
                                ]
                            }
                        },
                        {
                            "object": "block",
                            "type": "table_row",
                            "table_row": {
                                "cells": [
                                    [{"type": "text", "text": {"content": ""}}],
                                    [{"type": "text", "text": {"content": ""}}],
                                    [{"type": "text", "text": {"content": ""}}],
                                    [{"type": "text", "text": {"content": ""}}],
                                ]
                            }
                        }
                    ]
                }
            },
            _divider(),
            _heading2("📆 Week Ahead — Organize by Day"),
            _paragraph(
                "Assign tasks from your brain dump to each day. "
                "These will appear in that day's plan under 'On Your Plate'.",
                italic=True,
            ),
            _heading3("Monday → Prep & Deep Work"),
            _bulleted(""),
            _heading3("Tuesday → Deep Work"),
            _bulleted(""),
            _heading3("Wednesday → Deep Work & Personal Project"),
            _bulleted(""),
            _heading3("Thursday → Catch Up"),
            _bulleted(""),
            _heading3("Friday → Light Tasks"),
            _bulleted(""),
            _heading3("Saturday → Personal Project"),
            _bulleted(""),
            _heading3("Sunday → Weekly Reset, Reflect & Planning"),
            _bulleted(""),
            _divider(),
            _heading2("📅 Weekly Log"),
            _paragraph(
                "For 5 minutes, log everything significant from the past week — "
                "what you did, with numbers, outcomes, and proof woven directly "
                "into each entry. No rigid template; just capture what mattered "
                "and why.",
                italic=True,
            ),
            _paragraph(""),
        ])

    if is_friday:
        friday_sunday_tasks = friday_sunday_tasks or {}

        def _pre_populated_bullets(day_key: str) -> list[dict]:
            tasks = friday_sunday_tasks.get(day_key, [])
            if tasks:
                return [_bulleted(t) for t in tasks]
            return [_bulleted("")]

        blocks.extend([
            _divider(),
            _heading2("🧠 Brain Dump"),
            _paragraph(
                "Dump everything on your mind — tasks, ideas, obligations. "
                "You'll sort them below.",
                italic=True,
            ),
            {
                "object": "block",
                "type": "table",
                "table": {
                    "table_width": 4,
                    "has_column_header": True,
                    "has_row_header": False,
                    "children": [
                        {
                            "object": "block",
                            "type": "table_row",
                            "table_row": {
                                "cells": [
                                    [{"type": "text", "text": {"content": "Task"}, "annotations": {"bold": True}}],
                                    [{"type": "text", "text": {"content": "Class / Category"}, "annotations": {"bold": True}}],
                                    [{"type": "text", "text": {"content": "Deadline"}, "annotations": {"bold": True}}],
                                    [{"type": "text", "text": {"content": "Notes"}, "annotations": {"bold": True}}],
                                ]
                            }
                        },
                        {
                            "object": "block",
                            "type": "table_row",
                            "table_row": {
                                "cells": [
                                    [{"type": "text", "text": {"content": ""}}],
                                    [{"type": "text", "text": {"content": ""}}],
                                    [{"type": "text", "text": {"content": ""}}],
                                    [{"type": "text", "text": {"content": ""}}],
                                ]
                            }
                        }
                    ]
                }
            },
            _divider(),
            _heading2("📆 Mid-Week Update"),
            _paragraph(
                "Review and update your plan for the weekend and Monday. "
                "These will appear in each day's plan under 'On Your Plate'.",
                italic=True,
            ),
            _heading3("Friday → Light Tasks"),
            *_pre_populated_bullets("Friday → Light Tasks"),
            _heading3("Saturday → Personal Project"),
            *_pre_populated_bullets("Saturday → Personal Project"),
            _heading3("Sunday → Weekly Reset, Reflect & Planning"),
            *_pre_populated_bullets("Sunday → Weekly Reset, Reflect & Planning"),
            _heading3("Monday → Prep & Deep Work"),
            *_pre_populated_bullets("Monday → Prep & Deep Work"),
            _divider(),
        ])

    blocks.append(_divider())
    return blocks


def _block_plain_text(block: dict) -> str:
    btype = block.get("type", "")
    if not btype:
        return ""
    content = block.get(btype, {})
    rich = content.get("rich_text", [])
    parts: list[str] = []
    for rt in rich:
        if rt.get("plain_text"):
            parts.append(rt["plain_text"])
        else:
            parts.append(rt.get("text", {}).get("content", ""))
    return "".join(parts).strip()


def _empty_yesterday_results() -> dict:
    return {
        "completed": [],
        "missed": [],
        "one_thing_differently": None,
        "must_do_tasks": [],
        "notes": None,
        "inner_state": None,
        "performance": None,
    }


def _parse_end_of_day_report(blocks: list[dict]) -> dict[str, object]:
    result = {
        "one_thing_differently": None,
        "must_do_tasks": [],
        "notes": None,
        "inner_state": None,
        "performance": None,
    }
    eod_index = None
    for i, block in enumerate(blocks):
        if block.get("type") == "heading_2" and _block_plain_text(block) == "📓 End-of-Day Report":
            eod_index = i
            break
    if eod_index is None:
        return result

    def _parse_rating(raw: str) -> int | None:
        m = re.search(r"\b([1-9]|10)\b", raw)
        return int(m.group(1)) if m else None

    section_blocks = blocks[eod_index + 1:]
    i = 0
    while i < len(section_blocks):
        block = section_blocks[i]
        btype = block.get("type", "")
        text = _block_plain_text(block)

        if btype == "paragraph" and text.startswith("🧠 Inner state today"):
            if i + 1 < len(section_blocks):
                raw = _block_plain_text(section_blocks[i + 1])
                result["inner_state"] = _parse_rating(raw)
            i += 2
            continue

        if btype == "paragraph" and text.startswith("⚡ Performance today"):
            if i + 1 < len(section_blocks):
                raw = _block_plain_text(section_blocks[i + 1])
                result["performance"] = _parse_rating(raw)
            i += 2
            continue

        if btype == "paragraph" and text.startswith("💭 What's one thing I want to do differently tomorrow?"):
            if i + 1 < len(section_blocks) and section_blocks[i + 1].get("type") == "paragraph":
                answer = _block_plain_text(section_blocks[i + 1])
                if answer:
                    result["one_thing_differently"] = answer
            i += 2
            continue

        if btype == "paragraph" and text.startswith("✅ Tasks for Tomorrow"):
            tiers: dict[str, list[str]] = {"high": [], "medium": [], "low": []}
            tier_labels = {
                "🔴 High Priority": "high",
                "🟡 Medium Priority": "medium",
                "🟢 Low-Effort": "low",
            }
            collected_labels: set[str] = set()
            j = i + 1
            while j < len(section_blocks):
                block_j = section_blocks[j]
                btype_j = block_j.get("type", "")
                text_j = _block_plain_text(block_j)

                if btype_j == "paragraph" and text_j in tier_labels:
                    key = tier_labels[text_j]
                    collected_labels.add(key)
                    j += 1
                    while j < len(section_blocks) and section_blocks[j].get("type") == "bulleted_list_item":
                        item_text = _block_plain_text(section_blocks[j])
                        if item_text:
                            tiers[key].append(item_text)
                        j += 1
                    if len(collected_labels) == 3:
                        break
                    continue

                if btype_j == "paragraph" and text_j.startswith("🗒️ Notes"):
                    break
                if btype_j != "bulleted_list_item":
                    break
                j += 1

            result["must_do_tasks"] = {
                "high": [t for t in tiers["high"] if t],
                "medium": [t for t in tiers["medium"] if t],
                "low": [t for t in tiers["low"] if t],
            }
            i = j
            continue

        if btype == "paragraph" and text.startswith("🗒️ Notes"):
            if i + 1 < len(section_blocks) and section_blocks[i + 1].get("type") == "paragraph":
                answer = _block_plain_text(section_blocks[i + 1])
                if answer.strip():
                    result["notes"] = answer
            i += 2
            continue

        i += 1

    return result


def find_yesterday_log_page_id(notion_key: str, yesterday_date_str: str) -> str | None:
    """Find yesterday's child page under Automation Logs by exact title match."""
    if not notion_key:
        return None
    try:
        notion = Client(auth=notion_key)
        cursor = None
        while True:
            kwargs = {"block_id": AUTOMATION_LOGS_PAGE_ID, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = notion.blocks.children.list(**kwargs)
            for block in resp.get("results", []):
                if block.get("type") != "child_page":
                    continue
                title = block.get("child_page", {}).get("title", "")
                if title == yesterday_date_str:
                    return block.get("id")
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
    except Exception:
        return None
    return None


def read_yesterday_habits(notion_key: str, yesterday_page_id: str) -> dict:
    """Read habit checkboxes and End-of-Day Report from yesterday's log page."""
    if not notion_key or not yesterday_page_id:
        return _empty_yesterday_results()
    try:
        notion = Client(auth=notion_key)
        all_blocks: list[dict] = []

        # Fetch all top-level blocks with pagination.
        cursor = None
        while True:
            kwargs = {"block_id": yesterday_page_id, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            resp = notion.blocks.children.list(**kwargs)
            all_blocks.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")

        # Find the "☑️ Habits" section heading — this is where the real daily
        # checklist starts. Scoping to this section excludes the habit RECAP
        # to_do blocks written in the Daily Briefing, which are display-only.
        habits_section_index = None
        eod_index = None
        for i, block in enumerate(all_blocks):
            btype = block.get("type")
            text = _block_plain_text(block)
            if btype == "heading_2" and "Habits" in text and habits_section_index is None:
                habits_section_index = i
            if btype == "heading_2" and text == "📓 End-of-Day Report":
                eod_index = i
                break  # EOD always comes after Habits section, so stop here

        if habits_section_index is not None:
            start = habits_section_index + 1
            end = eod_index if eod_index is not None else len(all_blocks)
            habit_blocks = all_blocks[start:end]
        else:
            # Fallback: page predates the ☑️ Habits heading — use old behavior
            habit_blocks = all_blocks[:eod_index] if eod_index is not None else all_blocks

        todo_blocks = [b for b in habit_blocks if b.get("type") == "to_do"]

        completed: list[str] = []
        missed: list[str] = []
        _BAD_HABIT_LABEL = "🚫 Avoid Bad Habit*"

        for block in todo_blocks:
            todo = block.get("to_do", {})
            text = _block_plain_text(block)
            checked = bool(todo.get("checked", False))
            print(f'[Habits Debug] found to_do: "{text}" | checked={checked}')
            if not text:
                continue
            if _BAD_HABIT_LABEL in text:
                # Inverted: checked means FAILED, unchecked means successfully avoided
                if checked:
                    missed.append(text)
                else:
                    completed.append(text)
            else:
                if checked:
                    completed.append(text)
                else:
                    missed.append(text)

        if not todo_blocks:
            print(f"[Habits Debug] No to_do blocks found on page {yesterday_page_id}")

        eod_fields = _parse_end_of_day_report(all_blocks)
        return {
            "completed": completed,
            "missed": missed,
            **eod_fields,
        }
    except Exception:
        return _empty_yesterday_results()


def update_habit_tracker(notion_key: str, habit_results: dict, yesterday_date, label_to_column: dict[str, str | None]) -> bool:
    if not HABIT_TRACKER_DATABASE_ID or HABIT_TRACKER_DATABASE_ID.startswith("["):
        print("[*] Habit tracker database ID not set — skipping.")
        return False

    tracked_columns = sorted({col for col in label_to_column.values() if col})
    ensure_habit_tracker_schema(notion_key, tracked_columns)

    import requests as _req
    yesterday_str = yesterday_date.strftime("%Y-%m-%d")
    headers = {
        "Authorization": f"Bearer {notion_key}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    completed = habit_results.get("completed", [])

    properties: dict = {
        "Name": {"title": [{"type": "text", "text": {"content": yesterday_str}}]},
        "Date": {"date": {"start": yesterday_str}},
    }

    for db_column in set(label_to_column.values()):
        if db_column is not None:
            properties[db_column] = {"checkbox": False}

    for c in completed:
        col = habit_label_to_column(c)
        if col is not None:
            properties[col] = {"checkbox": True}
            print(f"[Habit Match] '{c}' -> column='{col}' | checked=True")

    inner_state = habit_results.get("inner_state")
    performance = habit_results.get("performance")
    if inner_state is not None:
        properties["Inner State"] = {"number": inner_state}
    if performance is not None:
        properties["Performance"] = {"number": performance}

    try:
        q = _req.post(
            f"https://api.notion.com/v1/databases/{HABIT_TRACKER_DATABASE_ID}/query",
            headers=headers,
            json={
                "filter": {
                    "property": "Date",
                    "date": {"equals": yesterday_str},
                }
            },
        )
        q.raise_for_status()
        results = q.json().get("results", [])
    except Exception as e:
        print(f"[Warning] Habit tracker query failed: {e}")
        return False

    try:
        if results:
            page_id = results[0]["id"]
            r = _req.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=headers,
                json={"properties": properties},
            )
            r.raise_for_status()
            print(f"[*] Habit tracker updated for {yesterday_str}.")
        else:
            r = _req.post(
                "https://api.notion.com/v1/pages",
                headers=headers,
                json={
                    "parent": {"database_id": HABIT_TRACKER_DATABASE_ID},
                    "properties": properties,
                },
            )
            r.raise_for_status()
            print(f"[*] Habit tracker row created for {yesterday_str}.")
        return True
    except Exception as e:
        print(f"[Warning] Habit tracker write failed: {e}")
        return False


# ── Summer day counter ────────────────────────────────────────────────────────

_SUMMER_START = datetime.date(2026, 6, 22)
_SUMMER_END   = datetime.date(2026, 9, 7)
_SUMMER_TOTAL = (_SUMMER_END - _SUMMER_START).days + 1  # 78


def _summer_day_counter(today: datetime.date) -> str | None:
    if today < _SUMMER_START or today > _SUMMER_END:
        return None
    day_num = (today - _SUMMER_START).days + 1
    pct = round((day_num / _SUMMER_TOTAL) * 100, 1)
    return f"Day {day_num}/{_SUMMER_TOTAL} of Summer ({pct}%)"


def _build_habit_recap_checklist_blocks(completed: list[str], missed: list[str]) -> list[dict]:
    blocks = []
    for label in completed:
        blocks.append(_todo(label, checked=True))
    for label in missed:
        blocks.append(_todo(label, checked=False))
    return blocks


# ── Page creator ──────────────────────────────────────────────────────────────

def write_plan_to_notion(plan: dict, daily_axiom: str | None = None, friday_sunday_tasks: dict | None = None, checklist_labels: list[str] | None = None, habit_results: dict | None = None) -> str:
    notion_key = os.environ.get("NOTION_API_KEY", "")
    if not notion_key:
        raise EnvironmentError("NOTION_API_KEY not set in .env")

    notion = Client(auth=notion_key)
    children: list[dict] = []

    # Section 1 — Quote
    if plan.get("quote"):
        children.append(_quote(plan["quote"]))
        children.append(_ideal_persona_mention())

    children.append(_divider())

    # Section 2 — Daily Briefing
    children.append(_heading2("📋 Daily Briefing"))
    # Lazy import avoids circular dependency (planner.py imports from notion_writer at module level)
    from planner import DAY_THEMES
    today_date = datetime.datetime.now().date()
    today_weekday = today_date.weekday()
    today_theme = DAY_THEMES.get(today_weekday, "")
    summer_counter = _summer_day_counter(today_date)
    if summer_counter:
        children.append(_callout(summer_counter, emoji="☀️"))
    if today_theme:
        children.append(_callout(f"Today's Theme — {today_theme}", emoji="🗓️"))
    if plan.get("briefing"):
        children.append(_paragraph(plan["briefing"]))
    completed = (habit_results or {}).get("completed", [])
    missed = (habit_results or {}).get("missed", [])
    n_done = len(completed)
    n_total = n_done + len(missed)
    if n_total > 0:
        children.append(_paragraph(f"Yesterday: {n_done}/{n_total} habits completed", bold=True))
        children.extend(_build_habit_recap_checklist_blocks(completed, missed))
    if plan.get("habit_recap"):
        children.append(_callout(plan["habit_recap"], emoji="📊"))
    children.append(_divider())

    # Section 3 — Your Day
    children.append(_heading2("🗓️ Your Day"))
    if plan.get("your_day"):
        children.extend(_build_your_day_blocks(plan["your_day"]))
    children.append(_divider())

    # Section 4 — On Your Plate
    children.append(_heading2("✅ On Your Plate"))
    if plan.get("on_your_plate"):
        children.extend(_build_on_your_plate_blocks(plan["on_your_plate"]))
    if daily_axiom:
        children.append(_divider())
        children.append(_callout(daily_axiom, emoji="💡"))
        children.append(_link_to_page("344f3b1a634a80efb730de611772bd64"))
        children.append(_divider())
    else:
        children.append(_divider())

    # Section 5 — To Close
    children.append(_heading2("💬 To Close"))
    if plan.get("closing"):
        children.extend(_build_closing_blocks(plan["closing"]))

    if plan.get("notes_from_last_night"):
        children.append(_divider())
        children.append(_heading3("📝 Notes from Last Night"))
        children.append(_paragraph(plan["notes_from_last_night"]))

    # Section 6 — Habit checklist
    children.append(_divider())
    children.append(_heading2("☑️ Habits"))
    children.append(_paragraph("Check off each habit as you complete it today."))
    is_sunday = datetime.datetime.now().weekday() == 6
    is_friday = datetime.datetime.now().weekday() == 4
    for label in (checklist_labels or []):
        children.append(_todo(label, checked=False))

    children.extend(_build_end_of_day_report_blocks(is_sunday=is_sunday, is_friday=is_friday, friday_sunday_tasks=friday_sunday_tasks or {}))

    # Create the child page (Notion API limit: 100 blocks per create call)
    page = notion.pages.create(
        parent={"page_id": AUTOMATION_LOGS_PAGE_ID},
        properties={
            "title": {
                "title": [{"type": "text", "text": {"content": plan["date_str"]}}]
            }
        },
        children=children[:100],
    )

    page_id = page["id"]

    # Append remaining blocks in batches of 100
    remaining = children[100:]
    while remaining:
        batch, remaining = remaining[:100], remaining[100:]
        notion.blocks.children.append(block_id=page_id, children=batch)

    page_url = page.get("url", f"https://notion.so/{page_id.replace('-', '')}")
    print(f"[*] Notion page created: {page_url}")
    return page_url
