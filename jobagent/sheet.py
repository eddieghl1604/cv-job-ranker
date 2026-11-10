"""
Google Sheet tracker: one row per screened job, appended (never rewritten), so your own
notes in the sheet are safe.

  python -m jobagent.sheet --test     # check access and create the header row
  python -m jobagent.sheet            # add rows for jobs not yet in the sheet
  python -m jobagent.sheet --rebuild  # clear the sheet and refill it with the current filter
                                      # (deletes everything below the header, incl. your notes)

Which jobs go in (config/agent.yaml, `sheet:`): kept jobs scoring at least `min_score`,
plus near misses (skipped by a rule but scoring at least notify.near_miss_min_score).
Everything is still kept in data/agent.db and in `python -m jobagent.screen --export`.

Text is written RAW, never as formulas: a job post starting with "=" can't run anything
in your sheet (a known trick called formula injection).
"""
import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

from .config import load_agent
from .settings import CONFIG_DIR, MYT
from .store import Store

KEY_PATH = CONFIG_DIR / "google-service-account.json"
HEADER = ["Found (MYT)", "Status", "Score", "Fit", "Title", "Company", "Location", "Remote",
          "Open to MY", "Years req.", "Languages req.", "Type", "Salary", "Why it fits", "You have",
          "Gaps", "Flags", "Skip reason", "Apply link", "Channel", "Posted (MYT)",
          "My status", "My notes"]
SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS sheet_synced (
    chat_id INTEGER, msg_id INTEGER, job_idx INTEGER, synced_at TEXT,
    PRIMARY KEY (chat_id, msg_id, job_idx)
);
"""


def _lst(x):
    try:
        return "; ".join(str(i) for i in json.loads(x or "[]"))
    except ValueError:
        return x or ""


def _myt(iso):
    return datetime.fromisoformat(iso).astimezone(MYT).strftime("%Y-%m-%d %H:%M") if iso else ""


def open_worksheet():
    import gspread
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise SystemExit("GOOGLE_SHEET_ID is missing from .env.")
    if not KEY_PATH.exists():
        raise SystemExit(f"{KEY_PATH} not found (the service account's JSON key).")
    gc = gspread.service_account(filename=str(KEY_PATH))
    try:
        sh = gc.open_by_key(sheet_id)
    except gspread.exceptions.SpreadsheetNotFound:
        email = json.loads(KEY_PATH.read_text(encoding="utf-8")).get("client_email", "the service account")
        raise SystemExit(f"Can't open the sheet. Share it with {email} as Editor, and check GOOGLE_SHEET_ID.")
    name = (load_agent().get("sheet") or {}).get("worksheet", "Jobs")
    try:
        ws = sh.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=len(HEADER))
    if not ws.row_values(1):
        ws.update(range_name="A1", values=[HEADER], value_input_option="RAW")
        ws.freeze(rows=1)
    return ws


def to_row(r) -> list:
    return [_myt(r["screened_at"]), r["status"], r["fit_score"] if r["fit_score"] is not None else "",
            r["fit_label"] or "", r["title"] or "", r["company"] or "", r["location"] or "", r["remote"] or "",
            r["open_to_malaysia"] or "", "" if r["years_required_min"] is None else r["years_required_min"],
            _lst(r["required_languages"]), r["employment_type"] or "", r["salary"] or "", _lst(r["why_fit"]),
            _lst(r["requirements_met"]), _lst(r["gaps"]), _lst(r["flags"]), _lst(r["reasons"]),
            r["apply_link"] or "", r["chat_title"], _myt(r["date_utc"]), "", ""]


def sync(batch: int = 200) -> int:
    cfg = load_agent()
    min_score = (cfg.get("sheet") or {}).get("min_score", 30)
    near = (cfg.get("notify") or {}).get("near_miss_min_score", 55)
    db = Store().conn
    db.row_factory = sqlite3.Row
    db.executescript(SYNC_SCHEMA)
    rows = db.execute(
        """SELECT s.*, m.chat_title, m.date_utc FROM screened s
           JOIN messages m ON m.chat_id = s.chat_id AND m.msg_id = s.msg_id
           WHERE ((s.status = 'kept' AND s.fit_score >= ?) OR (s.status = 'skipped' AND s.fit_score >= ?))
             AND NOT EXISTS (SELECT 1 FROM sheet_synced y WHERE y.chat_id = s.chat_id
                             AND y.msg_id = s.msg_id AND y.job_idx = s.job_idx)
           ORDER BY s.screened_at LIMIT ?""", (min_score, near, batch)).fetchall()
    if not rows:
        return 0
    ws = open_worksheet()
    ws.append_rows([to_row(r) for r in rows], value_input_option="RAW")
    now = datetime.now(timezone.utc).isoformat()
    db.executemany("INSERT OR REPLACE INTO sheet_synced VALUES (?,?,?,?)",
                   [(r["chat_id"], r["msg_id"], r["job_idx"], now) for r in rows])
    db.commit()
    return len(rows)


def rebuild():
    ws = open_worksheet()
    ws.resize(rows=1)                       # keeps only the header row
    ws.resize(rows=1000)
    db = Store().conn
    db.executescript(SYNC_SCHEMA)
    db.execute("DELETE FROM sheet_synced")
    db.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="check access and create the header row")
    ap.add_argument("--rebuild", action="store_true", help="clear the sheet and refill with the current filter")
    args = ap.parse_args()
    if args.rebuild:
        rebuild()
        print("Sheet cleared (header kept).")
    if args.test:
        ws = open_worksheet()
        print(f"Connected to sheet tab '{ws.title}'. Header row is in place.")
        return
    total = 0
    while True:
        n = sync()
        total += n
        if n == 0:
            break
    print(f"Added {total} rows to the sheet.")


if __name__ == "__main__":
    main()
