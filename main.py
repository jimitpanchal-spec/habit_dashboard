import os
import subprocess
import sys

# Windows terminal defaults to cp1252; habit labels and calendar events contain
# emoji. Reconfigure before any print() calls (matches standalone SSL fix block).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Python 3.14 enforces stricter X.509 CA validation that breaks Google OAuth
# token refresh (requests) and googleapiclient (httplib2) on this machine.
# Same root cause as the SSL workaround already in dashboard.py.
import urllib3
urllib3.disable_warnings()
import requests as _req
_orig_req_send = _req.Session.send
def _no_verify_req_send(self, *args, **kwargs):
    kwargs["verify"] = False
    return _orig_req_send(self, *args, **kwargs)
_req.Session.send = _no_verify_req_send
import httplib2 as _httplib2
_orig_build_ssl = _httplib2._build_ssl_context
def _no_verify_build_ssl(*args, **kwargs):
    args = (True,) + args[1:] if args else args
    kwargs.pop("disable_ssl_certificate_validation", None)
    return _orig_build_ssl(*args, **kwargs)
_httplib2._build_ssl_context = _no_verify_build_ssl
import ssl as _ssl
_orig_create_ctx = _ssl.create_default_context
def _no_verify_ctx(*args, **kwargs):
    ctx = _orig_create_ctx(*args, **kwargs)
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    return ctx
_ssl.create_default_context = _no_verify_ctx

from planner import run_planner, create_focus_block_event
from notion_writer import (
    update_habit_tracker,
    write_plan_to_notion,
)
from memory_context import run_memory_context_pipeline


def main():
    print("=== Daily Planner ===")
    try:
        (
            plan,
            creds,
            today,
            school_day,
            yesterday_date,
            habit_results,
            friday_tasks,
            checklist_labels,
            label_to_column,
        ) = run_planner()
        create_focus_block_event(creds, today, school_day, must_do_tasks=habit_results.get("must_do_tasks"))
    except FileNotFoundError as e:
        print(f"[Error] {e}")
        sys.exit(1)
    except EnvironmentError as e:
        print(f"[Error] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[Error] Planner failed: {e}")
        raise

    try:
        tracker_updated = update_habit_tracker(
            os.environ.get("NOTION_API_KEY", ""),
            habit_results,
            yesterday_date,
            label_to_column,
        )
        if not tracker_updated:
            print("[Warning] Habit tracker was not updated.")
        url = write_plan_to_notion(
            plan,
            daily_axiom=plan.get("daily_axiom"),
            friday_sunday_tasks=friday_tasks,
            checklist_labels=checklist_labels,
            habit_results=habit_results,
        )
        print(f"\nDone! Your daily plan is ready:\n{url}")
    except Exception as e:
        print(f"[Error] Failed to write to Notion: {e}")
        raise

    try:
        run_memory_context_pipeline(creds=creds)
    except Exception as e:
        print(f"[Warning] Memory context pipeline failed: {e}", file=sys.stderr)

    try:
        dashboard_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.py")
        subprocess.run([sys.executable, dashboard_py], check=True)
    except Exception as e:
        print(f"[Warning] Dashboard generation failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
