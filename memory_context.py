"""
Daily-updating Memory/Context digest page for Notion.

Runs once per day AFTER the existing daily-plan pipeline.
Reads: 7-day journal notes, upcoming 3-day non-routine calendar events,
       completed tasks (last 7 days) + Deep Work habit history.
Writes: condensed Claude digest to the Memory/Context Notion page (overwrite each run).

Does NOT modify generate_plan(), the system prompt, write_plan_to_notion(),
or any existing YOUR_DAY/ON_YOUR_PLATE/HABIT_RECAP logic.
"""

import os
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import anthropic
from googleapiclient.discovery import build
from notion_client import Client

from planner import (
    fetch_calendar_events,
    fetch_habit_history,
    get_google_credentials,
    TORONTO_TZ,
    TASK_LISTS_TO_USE,
)
from notion_writer import (
    _heading2,
    _paragraph,
    _bulleted,
    _divider,
    _block_plain_text,
    find_yesterday_log_page_id,
    _parse_end_of_day_report,
)

MEMORY_CONTEXT_PAGE_ID = "385f3b1a634a80b7bc74f66e62b88cbb"

DEEP_WORK_HABIT_COLUMN = "Deep Work"

# Substrings (lowercased) that identify routine/recurring calendar events.
# One-off named events that don't match any of these will surface in the digest.
# Adjust this list to taste — it's the primary filter before Claude summarizes.
_ROUTINE_KEYWORDS = [
    # Daily schedule staples
    "sleep", "morning routine", "breakfast", "lunch", "dinner",
    "night routine", "wind down",
    # Work/study blocks
    "deep work block",
    # School recurring
    "homeroom", "school arrival",
    "period 1", "period 2", "period 3", "period 4",
    "period 5", "period 6", "period 7", "period 8",
    # Habit tracker
    "weekly check-in",
    # Ideal Routine calendar entries (daily/weekly repeating)
    "read day plan", "fill end-of-day report",
    "light tasks/hobby", "light tasks",
    "workout", "biking", "reading", "recreation",
    "listen to podcast", "break",
    "weekly prep", "weekly planning", "weekly reflection", "weekly planning/report",
    "focus prep", "prep",
]

# Deep work blocks created with emoji prefixes instead of the "Deep Work Block" title.
_ROUTINE_LEAD_EMOJIS = {"🎧", "📙", "🔬", "💻", "📊", "🔭", "📌"}

# Text of the auto-written instruction paragraph in the Personal Notes section.
# Used to identify and skip it when reading user-written notes.
_PERSONAL_NOTES_INSTRUCTION = (
    "Add notes here that you want the planner to stay aware of. "
    "These are never auto-overwritten — edit or delete them manually when they no longer apply."
)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _date_str_for_day(dt: datetime) -> str:
    """Format datetime to Automation Logs page-title format, Windows-safe."""
    if os.name == "nt":
        return dt.strftime("%A, %B %d").replace(" 0", " ")
    return dt.strftime("%A, %B %-d")


def _fetch_page_blocks(notion: Client, page_id: str) -> list[dict]:
    """Fetch all top-level blocks from a Notion page with pagination."""
    blocks: list[dict] = []
    cursor = None
    while True:
        kwargs: dict = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.blocks.children.list(**kwargs)
        blocks.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return blocks


def _is_routine_event(event: dict) -> bool:
    summary_lower = (event.get("summary") or "").lower().strip()
    if any(kw in summary_lower for kw in _ROUTINE_KEYWORDS):
        return True
    # Emoji-prefixed deep work blocks
    raw = (event.get("summary") or "").strip()
    if raw and raw[0] in _ROUTINE_LEAD_EMOJIS:
        return True
    return False


def _sanitize_block_for_append(block: dict) -> dict:
    """Strip server-side fields from a block dict before re-appending to Notion."""
    btype = block.get("type", "")
    return {
        "object": "block",
        "type": btype,
        btype: block.get(btype, {}),
    }


def _read_personal_notes(notion: Client, page_id: str) -> tuple[list[dict], str]:
    """
    Read the Personal Notes section from the Memory/Context page.
    Returns (user_note_blocks, personal_notes_text).
    user_note_blocks excludes the auto-written instruction paragraph so it is
    not doubled when blocks are re-appended after clearing.
    Returns ([], "") if no Personal Notes heading exists (first run).
    """
    try:
        blocks = _fetch_page_blocks(notion, page_id)
    except Exception as e:
        print(f"[Warning] Memory: could not read page for personal notes: {e}")
        return [], ""

    heading_index = None
    for i, block in enumerate(blocks):
        if block.get("type") == "heading_2" and _block_plain_text(block) == "Personal Notes":
            heading_index = i
            break

    if heading_index is None:
        return [], ""

    raw_after = blocks[heading_index + 1:]
    user_note_blocks: list[dict] = []
    for j, block in enumerate(raw_after):
        # Skip the auto-written instruction paragraph (always written as the first block)
        if j == 0 and _block_plain_text(block) == _PERSONAL_NOTES_INSTRUCTION:
            continue
        user_note_blocks.append(block)

    lines: list[str] = []
    for block in user_note_blocks:
        text = _block_plain_text(block)
        if text:
            lines.append(text)

    return user_note_blocks, "\n".join(lines).strip()


def fetch_memory_context_text(notion_key: str) -> str:
    """
    Read the current Memory/Context Notion page and return its content as flat
    readable text for injection into generate_plan()'s prompt.
    Splits at the Personal Notes heading: auto content comes first, then a
    '## Personal Notes:' section if the user has written any notes.
    Returns "(no recent context available)" on empty page or any error.
    """
    if not notion_key:
        return "(no recent context available)"
    try:
        notion = Client(auth=notion_key)
        blocks = _fetch_page_blocks(notion, MEMORY_CONTEXT_PAGE_ID)
        if not blocks:
            return "(no recent context available)"

        # Split at the Personal Notes heading_2
        personal_notes_index = None
        for i, block in enumerate(blocks):
            if block.get("type") == "heading_2" and _block_plain_text(block) == "Personal Notes":
                personal_notes_index = i
                break

        auto_blocks = blocks[:personal_notes_index] if personal_notes_index is not None else blocks
        notes_blocks = blocks[personal_notes_index + 1:] if personal_notes_index is not None else []

        # Format auto section (before Personal Notes heading) — same logic as before
        auto_lines: list[str] = []
        for block in auto_blocks:
            btype = block.get("type", "")
            if btype == "divider":
                continue
            text = _block_plain_text(block)
            if not text:
                continue
            if btype == "heading_2":
                auto_lines.append(f"\n{text}:")
            elif btype == "bulleted_list_item":
                auto_lines.append(f"- {text}")
            else:
                auto_lines.append(text)

        # Format personal notes section (after heading), skipping the instruction paragraph
        notes_lines: list[str] = []
        for j, block in enumerate(notes_blocks):
            text = _block_plain_text(block)
            if j == 0 and text == _PERSONAL_NOTES_INSTRUCTION:
                continue
            if not text:
                continue
            btype = block.get("type", "")
            if btype == "bulleted_list_item":
                notes_lines.append(f"- {text}")
            else:
                notes_lines.append(text)

        result_parts: list[str] = []
        auto_text = "\n".join(auto_lines).strip()
        if auto_text:
            result_parts.append(auto_text)
        if notes_lines:
            result_parts.append("## Personal Notes:\n" + "\n".join(notes_lines))

        result = "\n\n".join(result_parts).strip()
        return result if result else "(no recent context available)"
    except Exception as e:
        print(f"[Warning] Memory: could not read Memory/Context page: {e}")
        return "(no recent context available)"


# ── Step 1a: Journal / Notes ──────────────────────────────────────────────────

# The Notes field template prints this italic instruction line. When a user leaves
# the Notes section blank, _parse_end_of_day_report() picks it up as the "notes"
# value. Filter it out so it doesn't pollute the digest.
_NOTES_PLACEHOLDER = "these notes carry forward to tomorrow's page."


def fetch_7day_journal_notes(notion_key: str, local_now: datetime) -> list[dict]:
    """
    Return [{date, notes}, ...] for each of the last 7 days that has a log page
    with a non-empty, non-placeholder Notes field. Skips missing or blank entries.
    """
    notion = Client(auth=notion_key)
    results: list[dict] = []
    for days_ago in range(1, 8):
        dt = local_now - timedelta(days=days_ago)
        date_str = _date_str_for_day(dt)
        page_id = find_yesterday_log_page_id(notion_key, date_str)
        if not page_id:
            continue
        try:
            blocks = _fetch_page_blocks(notion, page_id)
            eod = _parse_end_of_day_report(blocks)
            notes = eod.get("notes")
            if not notes or not notes.strip():
                continue
            if notes.strip().lower() == _NOTES_PLACEHOLDER:
                continue
            results.append({"date": date_str, "notes": notes.strip()})
        except Exception as e:
            print(f"[Warning] Memory: could not read notes for {date_str}: {e}")
    return results


# ── Step 1b: Upcoming Non-Routine Calendar ────────────────────────────────────

def fetch_upcoming_nonroutine_events(creds, local_now: datetime, days: int = 3) -> list[dict]:
    """
    Pull events for today through today+(days-1), filter out routine events,
    and attach a _date_label key to each kept event.
    """
    all_events: list[dict] = []
    for offset in range(days):
        target_dt = local_now + timedelta(days=offset)
        target_utc = target_dt.astimezone(timezone.utc)
        try:
            events = fetch_calendar_events(creds, target_utc)
            date_label = _date_str_for_day(target_dt)
            for e in events:
                if not _is_routine_event(e):
                    enriched = dict(e)
                    enriched["_date_label"] = date_label
                    all_events.append(enriched)
        except Exception as ex:
            print(f"[Warning] Memory: calendar fetch failed for day +{offset}: {ex}")
    return all_events


# ── Step 1c: Completed Tasks + Deep Work habit ────────────────────────────────

def fetch_completed_tasks(creds, days: int = 7) -> list[dict]:
    """
    Fetch tasks with status=completed from TASK_LISTS_TO_USE for the last `days` days.
    Uses Google Tasks completedMin/completedMax filter. Isolated from fetch_tasks().
    """
    service = build("tasks", "v1", credentials=creds)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    completed_min = since.isoformat()
    completed_max = now.isoformat()

    all_task_lists = service.tasklists().list().execute().get("items", [])
    completed: list[dict] = []
    for tl in all_task_lists:
        name = tl.get("title", "")
        if name not in TASK_LISTS_TO_USE:
            continue
        try:
            items = service.tasks().list(
                tasklist=tl["id"],
                showCompleted=True,
                showHidden=True,
                completedMin=completed_min,
                completedMax=completed_max,
            ).execute().get("items", [])
            for task in items:
                if task.get("status") == "completed":
                    completed.append({
                        "title": task.get("title", "(Untitled)"),
                        "completed": task.get("completed", ""),
                        "list": name,
                    })
        except Exception as e:
            print(f"[Warning] Memory: could not fetch completed tasks from '{name}': {e}")
    return completed


# ── Step 2: Claude summarization ──────────────────────────────────────────────

def _format_raw_data_for_prompt(
    journal_notes: list[dict],
    upcoming_events: list[dict],
    completed_tasks: list[dict],
    habit_history: list[dict],
) -> str:
    lines: list[str] = []

    lines.append("=== JOURNAL & NOTES (last 7 days) ===")
    if journal_notes:
        for entry in journal_notes:
            lines.append(f"[{entry['date']}] {entry['notes']}")
    else:
        lines.append("(no notes found)")

    lines.append("")
    lines.append("=== UPCOMING CALENDAR (next 2-3 days, non-routine events only) ===")
    if upcoming_events:
        for e in upcoming_events:
            lines.append(f"[{e.get('_date_label', '')}] {e['summary']} — {e.get('start', '')}")
    else:
        lines.append("(no non-routine events)")

    lines.append("")
    lines.append("=== COMPLETED TASKS (last 7 days) ===")
    if completed_tasks:
        for t in completed_tasks:
            completed_date = t["completed"][:10] if t["completed"] else "unknown"
            lines.append(f"- [{t['list']}] {t['title']} (completed {completed_date})")
    else:
        lines.append("(no completed tasks found)")

    lines.append("")
    lines.append("=== DEEP WORK HABIT HISTORY (last 7 days, most recent first) ===")
    if habit_history:
        for row in habit_history:
            dw = row.get("habits", {}).get(DEEP_WORK_HABIT_COLUMN)
            status = "hit" if dw else "missed"
            lines.append(f"[{row['date']}] Deep Work: {status}")
    else:
        lines.append("(no habit history found)")

    return "\n".join(lines)


def summarize_with_claude(raw_data: str, personal_notes_text: str = "") -> str:
    """Call Claude to condense the raw 7-day pull into a structured digest."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    personal_notes_section = ""
    if personal_notes_text:
        personal_notes_section = (
            "\n\n## Persistent Personal Notes (user-maintained, keep these in mind when summarizing "
            "— do not restate them verbatim, just factor them in):\n"
            + personal_notes_text
        )

    prompt = f"""You are condensing 7-day context data into a brief digest for a daily planning system.
Produce output in EXACTLY this format with these section markers (no other headers):

## JOURNAL_NOTES
- [bullet summarizing something notable from the notes]
- [additional bullets as needed — 3-5 max total]

## UPCOMING_CALENDAR
- [date + time: event name]
- [additional bullets — one line per event]

## TASK_PATTERN
[1-2 sentences combining task completion count and Deep Work habit pattern into one observation — e.g. "5/7 days hit Deep Work; both misses followed late nights noted in journal."]

Rules:
- JOURNAL_NOTES: concise bullet digest of notable content. If any note mentions a forward-looking plan or event (e.g. "going somewhere tomorrow with friends"), surface it even if it's not on the calendar. If no notes exist, write a single bullet: "(no notes this week)"
- UPCOMING_CALENDAR: one bullet per event with date and time. If no non-routine events, write a single bullet: "(none in the next 2-3 days)"
- TASK_PATTERN: a signal sentence or two — NOT a raw list. Combine the completed-task count and the Deep Work hit/miss pattern into one observation. Mention any streak or pattern (e.g. consecutive misses, strong streak).
- Keep sections concise. This digest is a context injection for another prompt, not a report.

Raw data:

{raw_data}{personal_notes_section}"""

    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1500,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


# ── Step 3: Parse digest + write Notion page ──────────────────────────────────

def _parse_digest_sections(digest: str) -> dict:
    """Parse Claude's ## SECTION output into structured dicts."""
    sections: dict = {
        "journal_notes": [],
        "upcoming_calendar": [],
        "task_pattern": "",
    }

    pattern = re.compile(
        r"##\s*(JOURNAL_NOTES|UPCOMING_CALENDAR|TASK_PATTERN)\s*\n(.*?)(?=##\s*(?:JOURNAL_NOTES|UPCOMING_CALENDAR|TASK_PATTERN)|$)",
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(digest):
        key = m.group(1).upper()
        content = m.group(2).strip()
        if key in ("JOURNAL_NOTES", "UPCOMING_CALENDAR"):
            bullets = [
                line.lstrip("- ").strip()
                for line in content.splitlines()
                if line.strip().startswith("-")
            ]
            # Fall back to the whole content block if Claude didn't use bullet format
            field = "journal_notes" if key == "JOURNAL_NOTES" else "upcoming_calendar"
            sections[field] = bullets if bullets else ([content] if content else [])
        elif key == "TASK_PATTERN":
            sections["task_pattern"] = content

    return sections


def _clear_page_blocks(notion: Client, page_id: str) -> None:
    """Delete all top-level blocks from a Notion page (clears it for overwrite)."""
    blocks = _fetch_page_blocks(notion, page_id)
    for block in blocks:
        try:
            notion.blocks.delete(block_id=block["id"])
        except Exception as e:
            print(f"[Warning] Memory: could not delete block {block['id']}: {e}")


def write_memory_context_to_notion(digest: str, notion_key: str) -> None:
    """Overwrite the auto content of the Memory/Context Notion page with the Claude digest,
    then re-append any user-written Personal Notes that existed before clearing."""
    notion = Client(auth=notion_key)

    # Read and preserve personal notes BEFORE clearing the page
    print("[Memory] Reading personal notes before clearing page...")
    user_note_blocks, _ = _read_personal_notes(notion, MEMORY_CONTEXT_PAGE_ID)
    print(f"[Memory]   Found {len(user_note_blocks)} user note block(s) to preserve.")

    print("[Memory] Clearing existing blocks from Memory/Context page...")
    _clear_page_blocks(notion, MEMORY_CONTEXT_PAGE_ID)

    sections = _parse_digest_sections(digest)

    now_toronto = datetime.now(TORONTO_TZ)
    if os.name == "nt":
        updated_str = now_toronto.strftime("%A, %B %d, %Y at %I:%M %p").replace(" 0", " ").replace(":0", ":")
    else:
        updated_str = now_toronto.strftime("%A, %B %-d, %Y at %-I:%M %p")

    blocks: list[dict] = [
        _paragraph(f"Auto-Updated Context — last updated: {updated_str} (America/Toronto)", bold=True),
        _divider(),
        _heading2("Recent Journal & Notes"),
    ]
    for bullet in sections["journal_notes"]:
        blocks.append(_bulleted(bullet))

    blocks.append(_divider())
    blocks.append(_heading2("Upcoming Non-Routine Calendar"))
    for bullet in sections["upcoming_calendar"]:
        blocks.append(_bulleted(bullet))

    blocks.append(_divider())
    blocks.append(_heading2("Task Completion Pattern"))
    blocks.append(_paragraph(sections["task_pattern"] or "(no data)"))

    # Personal Notes section header + instruction (always written)
    blocks.append(_divider())
    blocks.append(_heading2("Personal Notes"))
    blocks.append(_paragraph(_PERSONAL_NOTES_INSTRUCTION, italic=True))

    # Append auto content blocks in batches of 100 (Notion API limit)
    remaining = list(blocks)
    while remaining:
        batch, remaining = remaining[:100], remaining[100:]
        notion.blocks.children.append(block_id=MEMORY_CONTEXT_PAGE_ID, children=batch)

    # Re-append preserved user note blocks one by one (sanitized — Notion rejects server-side fields)
    for block in user_note_blocks:
        sanitized = _sanitize_block_for_append(block)
        try:
            notion.blocks.children.append(
                block_id=MEMORY_CONTEXT_PAGE_ID, children=[sanitized]
            )
        except Exception as e:
            print(f"[Warning] Memory: could not re-append user note block: {e}")

    print("[Memory] Memory/Context page updated successfully.")


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_memory_context_pipeline(creds=None) -> None:
    """
    Full pipeline: fetch raw data → Claude summarization → write to Notion.
    Each step is individually guarded — a failure in one step never crashes
    the caller (main.py wraps the whole call in its own try/except too).
    """
    notion_key = os.environ.get("NOTION_API_KEY", "")
    if not notion_key:
        print("[Warning] Memory: NOTION_API_KEY not set — skipping pipeline.")
        return

    if creds is None:
        try:
            creds = get_google_credentials()
        except Exception as e:
            print(f"[Warning] Memory: Google auth failed: {e}")
            return

    # Read personal notes FIRST — before any data fetching or page modification
    personal_notes_text = ""
    try:
        _notion_for_notes = Client(auth=notion_key)
        _, personal_notes_text = _read_personal_notes(_notion_for_notes, MEMORY_CONTEXT_PAGE_ID)
        if personal_notes_text:
            print(f"[Memory] Personal notes found ({len(personal_notes_text)} chars).")
        else:
            print("[Memory] No personal notes found.")
    except Exception as e:
        print(f"[Warning] Memory: could not read personal notes: {e}")

    local_now = datetime.now(TORONTO_TZ)

    print("[Memory] Fetching 7-day journal notes...")
    journal_notes: list[dict] = []
    try:
        journal_notes = fetch_7day_journal_notes(notion_key, local_now)
        print(f"[Memory]   Notes found for {len(journal_notes)} day(s).")
    except Exception as e:
        print(f"[Warning] Memory: journal fetch failed: {e}")

    print("[Memory] Fetching upcoming non-routine calendar events (next 3 days)...")
    upcoming_events: list[dict] = []
    try:
        upcoming_events = fetch_upcoming_nonroutine_events(creds, local_now, days=3)
        print(f"[Memory]   {len(upcoming_events)} non-routine event(s) found.")
    except Exception as e:
        print(f"[Warning] Memory: calendar fetch failed: {e}")

    print("[Memory] Fetching completed tasks (last 7 days)...")
    completed_tasks: list[dict] = []
    try:
        completed_tasks = fetch_completed_tasks(creds, days=7)
        print(f"[Memory]   {len(completed_tasks)} completed task(s) found.")
    except Exception as e:
        print(f"[Warning] Memory: completed tasks fetch failed: {e}")

    print("[Memory] Fetching Deep Work habit history (last 7 days)...")
    habit_history: list[dict] = []
    try:
        habit_history = fetch_habit_history(notion_key, days=7, habit_columns=[DEEP_WORK_HABIT_COLUMN])
        print(f"[Memory]   {len(habit_history)} habit history row(s) found.")
    except Exception as e:
        print(f"[Warning] Memory: habit history fetch failed: {e}")

    raw_data = _format_raw_data_for_prompt(
        journal_notes, upcoming_events, completed_tasks, habit_history
    )

    print("[Memory] Calling Claude for digest summarization...")
    digest = ""
    try:
        digest = summarize_with_claude(raw_data, personal_notes_text=personal_notes_text)
    except Exception as e:
        print(f"[Warning] Memory: Claude summarization failed: {e}")
        digest = (
            "## JOURNAL_NOTES\n- (summarization unavailable)\n\n"
            "## UPCOMING_CALENDAR\n- (summarization unavailable)\n\n"
            "## TASK_PATTERN\n(summarization unavailable)"
        )

    print("[Memory] Writing digest to Memory/Context Notion page...")
    try:
        write_memory_context_to_notion(digest, notion_key)
    except Exception as e:
        print(f"[Warning] Memory: failed to write to Notion: {e}")


# ── Standalone verification run ───────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    # Windows terminal is cp1252 by default; event summaries can contain emoji.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # Python 3.14 enforces stricter X.509 CA validation. Three HTTP transports
    # are in play — each needs its own bypass, matching dashboard.py's approach.
    import urllib3
    urllib3.disable_warnings()

    # Patch 1: requests.Session.send → covers google-auth token refresh
    import requests as _req_patch
    _orig_send = _req_patch.Session.send
    def _no_verify_send(self, *args, **kwargs):
        kwargs["verify"] = False
        return _orig_send(self, *args, **kwargs)
    _req_patch.Session.send = _no_verify_send

    # Patch 2: httplib2._build_ssl_context → covers googleapiclient Calendar/Tasks.
    # disable_ssl_certificate_validation is always the first positional arg.
    import httplib2 as _httplib2
    _orig_build_ssl = _httplib2._build_ssl_context
    def _no_verify_ssl_ctx(*args, **kwargs):
        args = (True,) + args[1:] if args else args
        kwargs.pop("disable_ssl_certificate_validation", None)
        return _orig_build_ssl(*args, **kwargs)
    _httplib2._build_ssl_context = _no_verify_ssl_ctx

    # Patch 3: ssl.create_default_context → httpx calls this via httpx._config
    # (confirmed in stack trace: httpx/_config.py line 40). Patching at this
    # level disables verification for notion_client and the Anthropic SDK.
    import ssl as _ssl_mod
    _orig_create_default_ctx = _ssl_mod.create_default_context
    def _no_verify_create_ctx(*args, **kwargs):
        ctx = _orig_create_default_ctx(*args, **kwargs)
        ctx.check_hostname = False
        ctx.verify_mode = _ssl_mod.CERT_NONE
        return ctx
    _ssl_mod.create_default_context = _no_verify_create_ctx

    from dotenv import load_dotenv
    load_dotenv()

    _notion_key = os.environ.get("NOTION_API_KEY", "")
    if not _notion_key:
        raise EnvironmentError("NOTION_API_KEY not set in .env")

    _creds = get_google_credentials()
    _local_now = datetime.now(TORONTO_TZ)

    print("=" * 60)
    print("PRE-STEP: READING PERSONAL NOTES")
    print("=" * 60)
    _notion_for_notes = Client(auth=_notion_key)
    _user_note_blocks, _personal_notes_text = _read_personal_notes(_notion_for_notes, MEMORY_CONTEXT_PAGE_ID)
    if _personal_notes_text:
        print(f"\nPersonal notes found ({len(_user_note_blocks)} block(s)):")
        print(_personal_notes_text)
    else:
        print("\n(no personal notes found — first run or section not yet created)")

    print("\n" + "=" * 60)
    print("STEP 1: RAW 7-DAY PULL")
    print("=" * 60)

    print("\n--- Journal/Notes (last 7 days) ---")
    _journal_notes = fetch_7day_journal_notes(_notion_key, _local_now)
    if _journal_notes:
        for entry in _journal_notes:
            print(f"  [{entry['date']}] {entry['notes'][:300]}")
    else:
        print("  (none found)")

    print("\n--- Upcoming Non-Routine Calendar (next 3 days) ---")
    _upcoming_events = fetch_upcoming_nonroutine_events(_creds, _local_now, days=3)
    if _upcoming_events:
        for e in _upcoming_events:
            print(f"  [{e.get('_date_label')}] {e['summary']} — {e.get('start', '')}")
    else:
        print("  (none found)")

    print("\n--- Completed Tasks (last 7 days) ---")
    _completed_tasks = fetch_completed_tasks(_creds, days=7)
    if _completed_tasks:
        for t in _completed_tasks:
            completed_date = t["completed"][:10] if t["completed"] else "unknown"
            print(f"  [{t['list']}] {t['title']} (completed {completed_date})")
    else:
        print("  (none found)")

    print("\n--- Deep Work Habit History (last 7 days) ---")
    _habit_history = fetch_habit_history(_notion_key, days=7, habit_columns=[DEEP_WORK_HABIT_COLUMN])
    if _habit_history:
        for row in _habit_history:
            dw = row.get("habits", {}).get(DEEP_WORK_HABIT_COLUMN)
            print(f"  [{row['date']}] Deep Work: {'hit' if dw else 'missed'}")
    else:
        print("  (none found)")

    print("\n" + "=" * 60)
    print("STEP 2: CLAUDE DIGEST")
    print("=" * 60)
    _raw_data = _format_raw_data_for_prompt(
        _journal_notes, _upcoming_events, _completed_tasks, _habit_history
    )
    print("\n[Raw data sent to Claude:]\n")
    print(_raw_data)
    if _personal_notes_text:
        print(f"\n[Personal notes passed to Claude:]\n{_personal_notes_text}")
    print("\n[Claude digest:]\n")
    _digest = summarize_with_claude(_raw_data, personal_notes_text=_personal_notes_text)
    print(_digest)

    print("\n" + "=" * 60)
    print("STEP 3: WRITING TO NOTION + VERIFICATION")
    print("=" * 60)
    write_memory_context_to_notion(_digest, _notion_key)

    print("\n--- Verifying page content (fetching back) ---")
    _notion_client = Client(auth=_notion_key)
    _written_blocks = _fetch_page_blocks(_notion_client, MEMORY_CONTEXT_PAGE_ID)
    print(f"Total blocks written: {len(_written_blocks)}")
    for blk in _written_blocks:
        btype = blk.get("type", "")
        if btype == "divider":
            print("  ────────────────────")
        else:
            content = blk.get(btype, {})
            rich = content.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich)
            print(f"  [{btype}] {text[:120]}")

    print("\n" + "=" * 60)
    print("STEP 4: fetch_memory_context_text() OUTPUT")
    print("=" * 60)
    print("\nThis is what the planner sees:\n")
    print(fetch_memory_context_text(_notion_key))

    print("\nDone. Memory/Context page successfully overwritten.")
