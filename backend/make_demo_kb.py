"""
Build the demo knowledge base from data/demo_support_tickets.json
----------------------------------------------------------------
The customer-facing demo tickets are authored as JSON (that is the shape the
product owner supplied them in), but the KB loader reads a spreadsheet. This
converts one into the other.

The output columns are deliberately named to match the headers already pinned in
data/kb_column_map.json, so switching the KB to this file needs no mapping change
at all:

    ticket_id             -> number
    "Closed"              -> state
    error_message         -> short_description   (embedded: the customer's symptom)
    root_cause            -> description         (embedded: what was actually wrong)
    task_name             -> u_issue_type        (embedded: the discriminative type)
    error_code            -> category            (metadata, not embedded)
    resolution + RCA tag  -> close_notes         (the recorded fix, plus the tag the
                                                  loader reads the resolution code from)
    team and timings      -> comments_and_work_notes

Usage (from backend/):
    ../rca/bin/python make_demo_kb.py
    ../rca/bin/python make_demo_kb.py --csv     # write a .csv instead of .xlsx
"""

import os
import sys
import json

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
SOURCE = os.path.join(DATA_DIR, "demo_support_tickets.json")
OUT_XLSX = os.path.join(DATA_DIR, "demo_support_tickets.xlsx")
OUT_CSV = os.path.join(DATA_DIR, "demo_support_tickets.csv")


def _pretty(task_name: str) -> str:
    """MODEM_CONNECTIVITY -> Modem Connectivity. The issue type is embedded, so it
    reads better as words than as a constant, and it matches natural phrasing."""
    return " ".join(w.capitalize() for w in str(task_name or "").split("_"))


def rows(tickets: list) -> list:
    out = []
    for t in tickets:
        code = t.get("error_code", "")
        task = _pretty(t.get("task_name", ""))
        # The loader reads the resolution code out of an {RCA TAG : ...} marker in the
        # close notes, which is how the real export carries it. Same convention here.
        close = f"{t.get('resolution', '')} {{RCA TAG : {code} - {task}}}"
        notes = (f"Resolved by team: {t.get('resolved_team', '')}. "
                 f"Resolution time: {t.get('resolution_time_hours', '')} hours. "
                 f"Resolved at: {t.get('resolved_at', '')}. "
                 f"Reported via: {t.get('source_system', '')}.")
        out.append({
            "number": t.get("ticket_id", ""),
            "state": "Closed",
            "short_description": t.get("error_message", ""),
            "description": t.get("root_cause", ""),
            "u_issue_type": task,
            "category": code,
            "close_notes": close,
            "comments_and_work_notes": notes,
        })
    return out


def main():
    with open(SOURCE, "r", encoding="utf-8") as f:
        data = json.load(f)
    tickets = data.get("tickets", [])
    if not tickets:
        raise SystemExit(f"No tickets found in {SOURCE}")

    df = pd.DataFrame(rows(tickets))
    if "--csv" in sys.argv:
        df.to_csv(OUT_CSV, index=False)
        target = OUT_CSV
    else:
        df.to_excel(OUT_XLSX, index=False)
        target = OUT_XLSX

    print(f"Wrote {len(df)} rows to {os.path.abspath(target)}")
    print(f"Columns: {', '.join(df.columns)}")
    print(f"Issue types: {dict(df['u_issue_type'].value_counts())}")
    print(f"Error codes: {dict(df['category'].value_counts())}")


if __name__ == "__main__":
    main()
