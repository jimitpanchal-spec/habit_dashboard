# dashboard.py
# Pulls last 7 days of habit and mood data from Notion and prints a clean JSON bundle.
# No new pip dependencies — uses: requests, python-dotenv, notion-client, tzdata (all already installed).
#
# SSL note: Python 3.14 enforces stricter X.509 CA validation (RFC 5280 Basic Constraints must be
# critical) that rejects the intermediate CA in Notion's cert chain on this machine. Since this is a
# personal script connecting to a single hardcoded trusted endpoint, SSL verification is disabled.

import json
import os
import shutil
import subprocess
import sys
import urllib3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic
import httpx
import requests
from dotenv import load_dotenv
from notion_client import Client

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

NOTION_API_KEY = os.environ["NOTION_API_KEY"]
TORONTO_TZ = ZoneInfo("America/Toronto")

HABIT_TRACKER_DATABASE_ID = "36af3b1a-634a-81dc-8f16-f4cc4ae1a8eb"
AUTOMATION_LOGS_PAGE_ID = "344f3b1a634a80ec86f0f5b7205f4751"
GOALS_PAGE_ID = "355f3b1a634a8009aaf4e58a8c047cef"
IDEAL_PERSONA_PAGE_ID = "35bf3b1a634a80e4af33f01d24ab3f8b"
MEMORY_CONTEXT_PAGE_ID = "385f3b1a634a80b7bc74f66e62b88cbb"
NOTION_VERSION = "2022-06-28"

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_history.json")

# Shared session with SSL verification off (see module-level comment).
_session = requests.Session()
_session.verify = False

# Shared httpx client for notion_client (also needs SSL off on this machine).
_httpx_client = httpx.Client(verify=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def _plain(block: dict) -> str:
    """Extract concatenated plain_text from any block's rich_text array."""
    btype = block.get("type", "")
    content = block.get(btype, {})
    rich_text = content.get("rich_text", [])
    return "".join(seg.get("plain_text", "") for seg in rich_text)


def _fmt_title(dt: date) -> str:
    """Format a date as the Notion page title the planner writes (e.g. 'Monday, June 16')."""
    if os.name == "nt":
        return dt.strftime("%A, %B %d").replace(" 0", " ")
    return dt.strftime("%A, %B %-d")


def _is_logged(entry: dict) -> bool:
    """A day is logged only if the user filled in an EOD score (performance or inner_state non-null).
    A row that exists with null scores and all-false checkboxes is an auto-created stub — treat as NOT logged."""
    return entry.get("performance") is not None or entry.get("inner_state") is not None


# ── Step 1: Discover habit columns from DB schema ─────────────────────────────

def get_habit_columns() -> list[str]:
    resp = _session.get(
        f"https://api.notion.com/v1/databases/{HABIT_TRACKER_DATABASE_ID}",
        headers=_headers(),
    )
    resp.raise_for_status()
    props = resp.json().get("properties", {})
    return [name for name, prop in props.items() if prop.get("type") == "checkbox"]


# ── Step 2: Query habit tracker (raw requests workaround for notion-client v3) ─

def query_tracker(start: date, end: date, habit_cols: list[str]) -> dict[str, dict]:
    """Returns rows keyed by YYYY-MM-DD date string."""
    url = f"https://api.notion.com/v1/databases/{HABIT_TRACKER_DATABASE_ID}/query"
    payload = {
        "filter": {
            "and": [
                {"property": "Date", "date": {"on_or_after": str(start)}},
                {"property": "Date", "date": {"on_or_before": str(end)}},
            ]
        },
        "sorts": [{"property": "Date", "direction": "ascending"}],
        "page_size": 100,
    }
    resp = _session.post(url, headers=_headers(), json=payload)
    resp.raise_for_status()

    result: dict[str, dict] = {}
    for row in resp.json().get("results", []):
        props = row.get("properties", {})
        date_val = (props.get("Date") or {}).get("date") or {}
        date_str = date_val.get("start", "")[:10]
        if not date_str:
            continue

        performance = (props.get("Performance") or {}).get("number")
        inner_state = (props.get("Inner State") or {}).get("number")

        habits = {col: bool((props.get(col) or {}).get("checkbox", False)) for col in habit_cols}

        result[date_str] = {"performance": performance, "inner_state": inner_state, **habits}

    return result


# ── Step 3: Build daily_entries array ─────────────────────────────────────────

def build_daily_entries(rows_by_date: dict, habit_cols: list[str], dates: list[date]) -> list[dict]:
    entries = []
    for d in dates:
        ds = str(d)
        if ds in rows_by_date:
            row = rows_by_date[ds]
            entry: dict = {"date": ds, "performance": row["performance"], "inner_state": row["inner_state"]}
            for col in habit_cols:
                entry[col] = row.get(col, False)
        else:
            entry = {"date": ds, "performance": None, "inner_state": None}
            for col in habit_cols:
                entry[col] = False
        entry["logged"] = _is_logged(entry)
        entries.append(entry)
    return entries


# ── Step 4: List Automation Logs child pages ──────────────────────────────────

def list_log_pages() -> list[dict]:
    """Returns [{id, title}] for all child pages under AUTOMATION_LOGS_PAGE_ID."""
    notion = Client(auth=NOTION_API_KEY, client=_httpx_client)
    pages: list[dict] = []
    cursor = None
    while True:
        kwargs: dict = {"block_id": AUTOMATION_LOGS_PAGE_ID, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.blocks.children.list(**kwargs)
        for block in resp.get("results", []):
            if block.get("type") == "child_page":
                pages.append({
                    "id": block["id"],
                    "title": block.get("child_page", {}).get("title", ""),
                })
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return pages


# ── Step 5: Fetch EOD journal data from a log page ───────────────────────────

def fetch_eod_for_page(page_id: str) -> dict:
    """
    Replicates _parse_end_of_day_report from notion_writer.py.
    Returns {do_differently, journal} (empty strings if not found).
    """
    notion = Client(auth=NOTION_API_KEY, client=_httpx_client)
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

    eod_index = None
    for i, block in enumerate(blocks):
        if block.get("type") == "heading_2" and _plain(block) == "📓 End-of-Day Report":
            eod_index = i
            break
    if eod_index is None:
        return {"do_differently": "", "journal": ""}

    do_differently = ""
    journal = ""
    section = blocks[eod_index + 1:]
    i = 0
    while i < len(section):
        block = section[i]
        btype = block.get("type", "")
        text = _plain(block)

        if btype == "paragraph" and text.startswith("💭 What's one thing I want to do differently tomorrow?"):
            if i + 1 < len(section) and section[i + 1].get("type") == "paragraph":
                answer = _plain(section[i + 1])
                if answer:
                    do_differently = answer
            i += 2
            continue

        if btype == "paragraph" and text.startswith("🗒️ Notes"):
            if i + 1 < len(section) and section[i + 1].get("type") == "paragraph":
                answer = _plain(section[i + 1])
                if answer.strip():
                    journal = answer
            i += 2
            continue

        i += 1

    return {"do_differently": do_differently, "journal": journal}


def build_journal_lines(log_pages: list[dict], dates: list[date]) -> list[dict]:
    title_to_id = {p["title"]: p["id"] for p in log_pages}
    lines = []
    for d in dates:
        title = _fmt_title(d)
        page_id = title_to_id.get(title)
        if page_id:
            try:
                eod = fetch_eod_for_page(page_id)
            except Exception:
                eod = {"do_differently": "", "journal": ""}
        else:
            eod = {"do_differently": "", "journal": ""}
        lines.append({"date": str(d), **eod})
    return lines


# ── Step 6: Streaks ───────────────────────────────────────────────────────────

def compute_streaks(daily_entries: list[dict], habit_cols: list[str]) -> dict:
    # Trim ALL trailing unlogged days — a single-entry trim broke when multiple gap days exist at the end.
    entries = list(daily_entries)
    while entries and not _is_logged(entries[-1]):
        entries.pop()
    streaks: dict[str, int] = {}
    for col in habit_cols:
        count = 0
        for entry in reversed(entries):
            if entry.get(col) is True:
                count += 1
            else:
                break
        streaks[col] = count
    return streaks


# ── Step 7: Deep work comparison ──────────────────────────────────────────────

def compute_deep_work_comparison(daily_entries: list[dict]) -> dict:
    with_dw = [
        e["performance"] for e in daily_entries
        if e.get("Deep Work") is True and e["performance"] is not None
    ]
    without_dw = [
        e["performance"] for e in daily_entries
        if e.get("Deep Work") is not True and e["performance"] is not None
    ]

    def _avg(vals: list) -> float | str:
        return round(sum(vals) / len(vals), 1) if vals else "N/A"

    return {
        "with_deep_work_avg_performance": _avg(with_dw),
        "without_deep_work_avg_performance": _avg(without_dw),
        "sample_size": len(daily_entries),
    }


# ── Step 8: Week summary ──────────────────────────────────────────────────────

def compute_week_summary(daily_entries: list[dict], habit_cols: list[str]) -> dict:
    perf_vals = [e["performance"] for e in daily_entries if e["performance"] is not None]
    state_vals = [e["inner_state"] for e in daily_entries if e["inner_state"] is not None]

    avg_perf = round(sum(perf_vals) / len(perf_vals), 1) if perf_vals else None
    avg_state = round(sum(state_vals) / len(state_vals), 1) if state_vals else None

    # Only count habits and Deep Work on logged days — unlogged stubs must not inflate counts.
    deep_work_days = sum(1 for e in daily_entries if e.get("Deep Work") is True and _is_logged(e))
    habit_counts = {col: sum(1 for e in daily_entries if e.get(col) is True and _is_logged(e)) for col in habit_cols}
    top_habit = max(sorted(habit_counts), key=lambda h: habit_counts[h]) if habit_counts else None

    return {
        "avg_performance": avg_perf,
        "avg_inner_state": avg_state,
        "deep_work_days": deep_work_days,
        "top_habit": top_habit,
    }


# ── Step 9: Claude insights ───────────────────────────────────────────────────

def _fetch_notion_page(notion: Client, page_id: str, _depth: int = 0) -> str:
    """Fetch all text from a Notion page. Replicates _fetch_page_text from planner.py."""
    if _depth > 3:
        return ""
    clean_id = page_id.replace("-", "")
    try:
        blocks: list[dict] = []
        cursor = None
        while True:
            kwargs: dict = {"block_id": clean_id, "page_size": 100}
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
            if btype == "table_row":
                cells = content.get("cells", [])
                row_parts = [
                    "".join(rt.get("plain_text", "") for rt in cell).strip()
                    for cell in cells
                ]
                row_str = " | ".join(p for p in row_parts if p)
                if row_str:
                    lines.append(row_str)
                continue
            rich = content.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich)
            if text.strip():
                lines.append(text.strip())
            if block.get("has_children"):
                child_text = _fetch_notion_page(notion, block["id"], _depth + 1)
                if child_text:
                    lines.append(child_text)

        return "\n".join(filter(None, lines))
    except Exception as e:
        print(f"[Notion] Could not read page {page_id}: {e}", file=sys.stderr)
        return ""


_INSIGHTS_SYSTEM_PROMPT = """You are a personal coach reviewing someone's week of habit and performance data.
Your job is to return a JSON object with exactly three keys: affirmation, forward_note, slipping_note.

Rules:
- affirmation: 2-3 sentences. Celebrate a real, specific win from THIS week's logged data only. Reference an actual number or pattern visible in the entries provided (e.g. "Your Deep Work days averaged 8.5 vs 6.5 without it"). Do not reference specific dates or events from prior weeks or from the memory context — only what is in the current window. Be genuine, not generic.
- forward_note: 2-3 sentences. One specific, actionable thing to focus on next week based on what the data shows. Future-facing, not a criticism of the past. If the memory context explains a gap or situation, acknowledge it briefly and frame the note accordingly.
- slipping_note: 1-2 sentences. One habit or pattern that slipped this week, named directly. If the journal lines or memory context mention a reason (like a health situation), reference it — gaps caused by real circumstances are not failures.

Tone: warm, direct, human. Short sentences. No em-dashes (—). No bullet points inside the fields. No generic productivity advice.
Return only valid JSON — no markdown, no preamble."""


def generate_insights(data: dict) -> dict:
    """Call Claude to generate affirmation, forward_note, slipping_note from the week bundle."""
    fallback = {"affirmation": "", "forward_note": "", "slipping_note": ""}
    try:
        notion = Client(auth=NOTION_API_KEY, client=_httpx_client)
        goals_text = _fetch_notion_page(notion, GOALS_PAGE_ID)
        ideal_persona_text = _fetch_notion_page(notion, IDEAL_PERSONA_PAGE_ID)
        memory_context_text = _fetch_notion_page(notion, MEMORY_CONTEXT_PAGE_ID)

        today_str = data["week_range"]["end"]
        logged_count = data.get("logged_count", sum(1 for e in data["daily_entries"] if _is_logged(e)))
        gap_dates = data.get("gap_dates", [e["date"] for e in data["daily_entries"] if not _is_logged(e)])
        total_days = len(data["daily_entries"])

        journal_with_content = [
            j for j in data["journal_lines"]
            if j.get("do_differently") or j.get("journal")
        ]

        gap_note = ""
        if gap_dates:
            gap_note = (
                f"\nIMPORTANT: {len(gap_dates)} of {total_days} days in this window have no logged data "
                f"(dates: {', '.join(gap_dates)}). These are gaps — not bad days, not habit misses. "
                f"Do NOT count unlogged days as habit failures or treat them as evidence of slipping. "
                f"Only the {logged_count} logged days carry real signal."
            )

        user_prompt = f"""Today: {today_str}
Week window: {data["week_range"]["start"]} to {today_str}
Logged days: {logged_count} of {total_days}{gap_note}

## Memory / Context (recent personal situation — read this before generating feedback)
{memory_context_text or "(none available)"}

## Week Summary (computed from logged days only)
{json.dumps(data["week_summary"], indent=2)}

## Deep Work Impact
{json.dumps(data["deep_work_comparison"], indent=2)}

## Daily Habit & Performance Data (logged=true means EOD was filled; logged=false means gap day — ignore habit values on gap days)
{json.dumps(data["daily_entries"], indent=2)}

## Journal Notes (days with entries only)
{json.dumps(journal_with_content, indent=2)}

## Goals
{goals_text}

## Ideal Persona
{ideal_persona_text}
"""

        client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            http_client=httpx.Client(verify=False),
        )
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            temperature=0,
            system=_INSIGHTS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if Claude wrapped the JSON despite instructions.
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[Warning] generate_insights failed: {e}", file=sys.stderr)
        return fallback


# ── Step 10: Persist weekly history ──────────────────────────────────────────

def update_history(summary: dict) -> None:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history: list[dict] = json.load(f)
    else:
        history = []

    history = [e for e in history if e.get("week_start") != summary["week_start"]]
    history.append(summary)

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


# ── Step 11: HTML dashboard ───────────────────────────────────────────────────

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Weekly Dashboard</title>
<style>
:root{--cream:#F5EFE6;--cream-card:#FBF7F0;--warm-brown:#6B5B4D;--terracotta:#C9714D;--sage:#8FA888;--line-soft:#E5DCCB}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--cream);font-family:-apple-system,system-ui,sans-serif;color:var(--warm-brown);min-height:100vh;padding-bottom:88px}
.app{max-width:420px;margin:0 auto;padding:0 16px}
.hdr{padding:20px 0 12px}
.hdr h1{font-size:26px;font-weight:700}
.hdr .wr{font-size:13px;opacity:.55;margin-top:2px}
.card{background:var(--cream-card);border-radius:20px;box-shadow:0 4px 14px rgba(107,91,77,.10);border:1px solid rgba(255,255,255,.6);padding:18px;margin-bottom:14px}
.ct{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;opacity:.5;margin-bottom:12px}
.aff{background:var(--terracotta);border-radius:20px;box-shadow:0 6px 22px rgba(201,113,77,.35);padding:22px 20px;margin-bottom:14px;color:#fff}
.aff .hl{font-size:15px;font-weight:600;line-height:1.5;margin-bottom:10px}
.aff .sub{font-size:13px;line-height:1.55;opacity:.88}
.tab-content{display:none}
.nav{position:fixed;bottom:0;left:0;right:0;background:rgba(245,239,230,.96);backdrop-filter:blur(8px);padding:10px 16px 20px;display:flex;justify-content:center;gap:8px;border-top:1px solid var(--line-soft)}
.np{flex:1;max-width:112px;padding:10px 0;text-align:center;border-radius:14px;font-size:13px;font-weight:500;cursor:pointer;background:var(--cream-card);color:var(--warm-brown);box-shadow:0 4px 14px rgba(107,91,77,.10);border:1px solid rgba(255,255,255,.6);transition:background .15s,color .15s}
.np.active{background:var(--terracotta);color:#fff;box-shadow:0 4px 14px rgba(201,113,77,.30);border-color:transparent}
.wi{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:50%;background:rgba(201,113,77,.15);color:var(--terracotta);font-weight:700;font-size:15px;flex-shrink:0;margin-right:10px;margin-top:1px}
.sb{flex:1;text-align:center;border-radius:12px;padding:14px 8px}
.sb .sn{font-size:26px;font-weight:700;line-height:1}
.sb .sl{font-size:11px;margin-top:5px;opacity:.65;line-height:1.3}
.hstat{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}
.hstat div{font-size:13px}
.hstat span{font-weight:600;color:var(--terracotta)}
.lgnd{display:flex;gap:14px;margin-bottom:10px;align-items:center}
.li{display:flex;align-items:center;gap:5px;font-size:12px}
</style>
</head>
<body>
<div class="app">
  <div class="hdr">
    <h1>Your Week</h1>
    <div class="wr" id="wr"></div>
    <div id="logged-info" style="font-size:12px;margin-top:4px;opacity:.6"></div>
  </div>

  <div class="tab-content" id="tab-week" style="display:block">
    <div id="aff"></div>
    <div class="card">
      <div class="lgnd">
        <div class="li"><svg width="22" height="10"><line x1="0" y1="5" x2="22" y2="5" stroke="#C9714D" stroke-width="2.5" stroke-linecap="round"/></svg><span>Performance</span></div>
        <div class="li"><svg width="22" height="10"><line x1="0" y1="5" x2="22" y2="5" stroke="#8FA888" stroke-width="2" stroke-dasharray="5,3"/></svg><span>Inner State</span></div>
      </div>
      <div id="chart"></div>
      <div style="font-size:11px;opacity:.4;margin-top:5px">&#9679; below axis = Deep Work day</div>
    </div>
    <div id="streaks"></div>
    <div id="dw"></div>
    <div id="watch"></div>
  </div>

  <div class="tab-content" id="tab-history">
    <div id="hist"></div>
  </div>

  <div class="tab-content" id="tab-journal">
    <div id="journal-content"></div>
  </div>
</div>

<nav class="nav">
  <button class="np active" data-tab="week">This Week</button>
  <button class="np" data-tab="history">History</button>
  <button class="np" data-tab="journal">Journal</button>
</nav>

<script>
const DASHBOARD_DATA = __DATA_JSON__;
const HISTORY_DATA = __HISTORY_JSON__;
</script>
<script>
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

function fmt(iso){
  const d=new Date(iso+'T12:00:00');
  return d.toLocaleDateString('en-US',{month:'short',day:'numeric'});
}

function buildChart(entries){
  const W=340,H=155,PT=10,PR=10,PB=44,PL=10;
  const cW=W-PL-PR,cH=H-PT-PB,n=entries.length;
  const xp=i=>PL+i*(cW/(n-1));
  const yp=v=>PT+cH-((v-1)/9)*cH;
  function mkpath(key){
    let d='',pen=false;
    entries.forEach((e,i)=>{
      const v=e[key];
      if(v!==null&&v!==undefined){
        const x=xp(i).toFixed(1),y=yp(v).toFixed(1);
        d+=pen?' L '+x+' '+y:'M '+x+' '+y;pen=true;
      }else pen=false;
    });
    return d;
  }
  const ltrs=entries.map(e=>['S','M','T','W','T','F','S'][new Date(e.date+'T12:00:00').getDay()]);
  const grid=[3,5,7,9].map(v=>{const y=yp(v).toFixed(1);return '<line x1="'+PL+'" y1="'+y+'" x2="'+(W-PR)+'" y2="'+y+'" stroke="#E5DCCB" stroke-width="1"/>';}).join('');
  const xl=ltrs.map((l,i)=>'<text x="'+xp(i).toFixed(1)+'" y="'+(PT+cH+16).toFixed(1)+'" text-anchor="middle" font-size="11" fill="#6B5B4D" font-family="system-ui">'+l+'</text>').join('');
  const pd=mkpath('performance'),sd=mkpath('inner_state');
  const dots=entries.map((e,i)=>e.performance!=null?'<circle cx="'+xp(i).toFixed(1)+'" cy="'+yp(e.performance).toFixed(1)+'" r="3" fill="#C9714D"/>':'').join('');
  const dwd=entries.map((e,i)=>e['Deep Work']===true?'<circle cx="'+xp(i).toFixed(1)+'" cy="'+(PT+cH+32).toFixed(1)+'" r="4" fill="#C9714D" opacity=".75"/>':'').join('');
  return '<svg width="100%" viewBox="0 0 '+W+' '+H+'" style="display:block">'+grid+
    (pd?'<path d="'+pd+'" stroke="#C9714D" stroke-width="2.5" fill="none" stroke-linejoin="round" stroke-linecap="round"/>':'')+
    (sd?'<path d="'+sd+'" stroke="#8FA888" stroke-width="2" fill="none" stroke-dasharray="5,3" stroke-linejoin="round" stroke-linecap="round"/>':'')+
    dots+xl+dwd+'</svg>';
}

function buildStreaks(data){
  const entries=data.daily_entries,streaks=data.streaks;
  const cols=Object.keys(streaks);
  // Count only logged days for habit totals
  const cnts={};cols.forEach(h=>{cnts[h]=entries.filter(e=>e[h]===true&&e.logged).length;});
  const sorted=[...cols].sort((a,b)=>cnts[b]-cnts[a]||a.localeCompare(b));
  const rows=sorted.map(h=>{
    const sqs=entries.map(e=>{
      var bg,title;
      if(!e.logged){bg='#EDE7DC';title='gap day';}
      else if(e[h]===true){bg='#C9714D';title='done';}
      else{bg='#E5DCCB';title='missed';}
      return '<div style="width:22px;height:22px;border-radius:5px;flex-shrink:0;background:'+bg+';'+((!e.logged)?'border:1.5px dashed #B8A898;box-sizing:border-box;':'')+'" title="'+title+'"></div>';
    }).join('');
    return '<div style="display:flex;align-items:center;gap:7px;margin-bottom:9px">'+
      '<span style="flex:1;font-size:13px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;min-width:0">'+esc(h)+'</span>'+
      '<div style="display:flex;gap:3px">'+sqs+'</div>'+
      '<span style="background:#C9714D;color:#fff;border-radius:9px;padding:2px 8px;font-size:12px;font-weight:600;flex-shrink:0">'+(streaks[h]||0)+'</span>'+
      '</div>';
  }).join('');
  var legend='<div style="display:flex;gap:12px;margin-bottom:10px;font-size:11px;opacity:.6">'+
    '<span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#C9714D;margin-right:3px"></span>done</span>'+
    '<span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#E5DCCB;margin-right:3px"></span>missed</span>'+
    '<span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#EDE7DC;border:1px dashed #B8A898;margin-right:3px"></span>gap</span>'+
    '</div>';
  return '<div class="card"><div class="ct">Habit Streaks</div>'+legend+rows+'</div>';
}

function buildDW(data){
  const dw=data.deep_work_comparison;
  const wdw=dw.with_deep_work_avg_performance,wodw=dw.without_deep_work_avg_performance;
  if(wdw==='N/A'||wodw==='N/A')return '<div class="card"><div class="ct">What&#39;s Driving It</div><p style="font-size:14px">Not enough data yet.</p></div>';
  return '<div class="card"><div class="ct">What&#39;s Driving It</div>'+
    '<div style="display:flex;gap:12px;margin-bottom:12px">'+
    '<div class="sb" style="background:rgba(201,113,77,.1)"><div class="sn" style="color:#C9714D">'+wdw+'</div><div class="sl">avg on Deep Work days</div></div>'+
    '<div class="sb" style="background:rgba(107,91,77,.07)"><div class="sn" style="color:#6B5B4D">'+wodw+'</div><div class="sl">avg without</div></div>'+
    '</div><p style="font-size:13px">Deep Work was your strongest lever this week.</p></div>';
}

function buildWatch(ins){
  return '<div class="card"><div class="ct">Worth Watching</div>'+
    '<div style="display:flex;align-items:flex-start">'+
    '<div class="wi">!</div>'+
    '<p style="font-size:14px;line-height:1.6">'+esc(ins.slipping_note||'')+'</p>'+
    '</div></div>';
}

function buildHist(history){
  if(!history||!history.length)return '<div class="card"><p style="font-size:14px">No history yet.</p></div>';
  const sorted=[...history].sort((a,b)=>b.week_start.localeCompare(a.week_start));
  let html=sorted.map(e=>
    '<div class="card">'+
    '<div style="font-size:12px;opacity:.5;margin-bottom:8px">'+fmt(e.week_start)+' – '+fmt(e.week_end)+'</div>'+
    '<div class="hstat">'+
    '<div>Performance <span>'+(e.avg_performance||'–')+'</span></div>'+
    '<div>Inner State <span>'+(e.avg_inner_state||'–')+'</span></div>'+
    '<div>Deep Work <span>'+e.deep_work_days+'/7</span></div>'+
    '<div>Top habit <span>'+(e.top_habit||'–')+'</span></div>'+
    '</div></div>'
  ).join('');
  if(history.length===1)html+='<p style="font-size:13px;text-align:center;opacity:.45;padding:4px 0 10px">More weeks will appear here as the summer progresses.</p>';
  return html;
}

function buildJournal(data){
  var lines=data.journal_lines||[];
  var filler="These notes carry forward to tomorrow's page.";
  var cards=lines.filter(function(e){
    return e.do_differently||(e.journal&&e.journal!==filler);
  }).map(function(e){
    var h='<div class="card"><div class="ct">'+fmt(e.date)+'</div>';
    if(e.do_differently){
      h+='<div style="margin-bottom:10px">';
      h+='<div style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;opacity:.5;margin-bottom:4px">One thing differently</div>';
      h+='<p style="font-size:14px;line-height:1.55">'+esc(e.do_differently)+'</p></div>';
    }
    if(e.journal&&e.journal!==filler){
      h+='<div>';
      h+='<div style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;opacity:.5;margin-bottom:4px">Reflection</div>';
      h+='<p style="font-size:14px;line-height:1.55">'+esc(e.journal)+'</p></div>';
    }
    h+='</div>';
    return h;
  });
  if(!cards.length)return '<div class="card"><p style="font-size:14px">No journal entries this week yet.</p></div>';
  return cards.join('');
}

function render(){
  const d=DASHBOARD_DATA,ins=d.insights||{},wr=d.week_range;
  document.getElementById('wr').textContent=fmt(wr.start)+' – '+fmt(wr.end);
  var loggedEl=document.getElementById('logged-info');
  if(loggedEl){
    var lc=d.logged_count!=null?d.logged_count:d.daily_entries.filter(function(e){return e.logged;}).length;
    var total=d.daily_entries.length;
    loggedEl.textContent=lc+' / '+total+' days logged this week';
  }
  document.getElementById('aff').innerHTML='<div class="aff"><div class="hl">'+esc(ins.affirmation||'')+'</div><div class="sub">'+esc(ins.forward_note||'')+'</div></div>';
  document.getElementById('chart').innerHTML=buildChart(d.daily_entries);
  document.getElementById('streaks').innerHTML=buildStreaks(d);
  document.getElementById('dw').innerHTML=buildDW(d);
  document.getElementById('watch').innerHTML=buildWatch(ins);
  document.getElementById('hist').innerHTML=buildHist(HISTORY_DATA);
  document.getElementById('journal-content').innerHTML=buildJournal(d);
}
(function init(){
  render();
  document.querySelectorAll('.np').forEach(function(btn){
    btn.addEventListener('click',function(){
      var tab=this.getAttribute('data-tab');
      document.querySelectorAll('.tab-content').forEach(function(el){el.style.display='none';});
      document.querySelectorAll('.np').forEach(function(el){el.classList.remove('active');});
      var target=document.getElementById('tab-'+tab);
      if(target)target.style.display='block';
      this.classList.add('active');
    });
  });
})();
</script>
</body>
</html>"""


def write_html(data: dict, history: list) -> None:
    html_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    data_json = json.dumps(data, ensure_ascii=False)
    history_json = json.dumps(history, ensure_ascii=False)
    html = _HTML_TEMPLATE.replace("__DATA_JSON__", data_json).replace("__HISTORY_JSON__", history_json)
    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[*] dashboard.html written ({len(html) // 1024} KB).", file=sys.stderr)


# ── Step 12: Netlify deploy ───────────────────────────────────────────────────

def deploy_to_netlify() -> None:
    folder = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(folder, "dashboard.html")
    dst = os.path.join(folder, "index.html")

    try:
        shutil.copy2(src, dst)
        print("[*] Copied dashboard.html → index.html.", file=sys.stderr)
    except Exception as e:
        print(f"[Warning] Could not copy dashboard.html to index.html: {e}", file=sys.stderr)
        return

    # Locate netlify CLI: check PATH first, then the npm global install location.
    # Task Scheduler runs with a minimal environment that may not include npm's bin dir.
    netlify_cmd = shutil.which("netlify")
    if netlify_cmd is None:
        npm_global = os.path.join(os.environ.get("APPDATA", ""), "npm", "netlify.cmd")
        if os.path.isfile(npm_global):
            netlify_cmd = npm_global
    if netlify_cmd is None:
        print("[Warning] netlify CLI not found on PATH — skipping deploy.", file=sys.stderr)
        return

    # Quote the resolved path so spaces in APPDATA or npm dir don't break parsing.
    # shell=True is required on Windows for .cmd files.
    cmd = f'"{netlify_cmd}" deploy --prod --dir . --filter index.html'
    print(f"[*] Running: {cmd}", file=sys.stderr)
    # Same SSL strictness issue as Python on this machine — Node.js also rejects Netlify's
    # intermediate CA. NODE_OPTIONS=--use-system-ca loads the Windows cert store instead.
    env = {**os.environ, "NODE_OPTIONS": "--use-system-ca"}
    try:
        result = subprocess.run(
            cmd,
            cwd=folder,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if result.returncode == 0:
            print("[*] Netlify deploy succeeded.", file=sys.stderr)
            if result.stdout.strip():
                print(result.stdout.strip(), file=sys.stderr)
        else:
            print(f"[Warning] Netlify deploy failed (exit {result.returncode}):", file=sys.stderr)
            if result.stderr.strip():
                print(result.stderr.strip(), file=sys.stderr)
            if result.stdout.strip():
                print(result.stdout.strip(), file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("[Warning] Netlify deploy timed out after 60 seconds.", file=sys.stderr)
    except Exception as e:
        print(f"[Warning] Netlify deploy error: {e}", file=sys.stderr)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    today = datetime.now(tz=TORONTO_TZ).date()
    start = today - timedelta(days=6)
    dates = [start + timedelta(days=i) for i in range(7)]

    print(f"[*] Fetching habit columns from DB schema...", file=sys.stderr)
    habit_cols = get_habit_columns()
    print(f"    Found {len(habit_cols)} habit columns: {habit_cols}", file=sys.stderr)

    print(f"[*] Querying habit tracker for {start} → {today}...", file=sys.stderr)
    rows_by_date = query_tracker(start, today, habit_cols)
    print(f"    Got {len(rows_by_date)} rows from DB.", file=sys.stderr)

    daily_entries = build_daily_entries(rows_by_date, habit_cols, dates)

    logged_count = sum(1 for e in daily_entries if _is_logged(e))
    gap_dates = [e["date"] for e in daily_entries if not _is_logged(e)]

    print(f"[*] Logged coverage: {logged_count}/{len(daily_entries)} days", file=sys.stderr)
    for e in daily_entries:
        status = "LOGGED  " if _is_logged(e) else "GAP     "
        dw = " [Deep Work]" if e.get("Deep Work") and _is_logged(e) else ""
        print(f"    {e['date']} {status}{dw}", file=sys.stderr)
    print(f"[*] Deep Work days (logged only): {sum(1 for e in daily_entries if e.get('Deep Work') and _is_logged(e))}", file=sys.stderr)

    print("[*] Fetching Automation Logs page list...", file=sys.stderr)
    log_pages = list_log_pages()
    print(f"    Found {len(log_pages)} log pages.", file=sys.stderr)

    print("[*] Fetching EOD journal data for each day...", file=sys.stderr)
    journal_lines = build_journal_lines(log_pages, dates)

    streaks = compute_streaks(daily_entries, habit_cols)
    deep_work_comparison = compute_deep_work_comparison(daily_entries)
    week_summary = compute_week_summary(daily_entries, habit_cols)

    output = {
        "week_range": {"start": str(start), "end": str(today)},
        "logged_count": logged_count,
        "gap_dates": gap_dates,
        "daily_entries": daily_entries,
        "streaks": streaks,
        "deep_work_comparison": deep_work_comparison,
        "journal_lines": journal_lines,
        "week_summary": week_summary,
    }

    print("[*] Generating Claude insights...", file=sys.stderr)
    output["insights"] = generate_insights(output)

    print(json.dumps(output, indent=2))

    history_entry = {
        "week_start": str(start),
        "week_end": str(today),
        "avg_performance": week_summary["avg_performance"],
        "avg_inner_state": week_summary["avg_inner_state"],
        "deep_work_days": week_summary["deep_work_days"],
        "top_habit": week_summary["top_habit"],
    }
    update_history(history_entry)
    print("[*] dashboard_history.json updated.", file=sys.stderr)

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)
    print("[*] Generating HTML dashboard...", file=sys.stderr)
    write_html(output, history)
    deploy_to_netlify()


if __name__ == "__main__":
    main()
