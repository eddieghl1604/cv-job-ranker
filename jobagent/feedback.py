"""
Your own fit judgements, collected with buttons, for milestone 3.

Buttons appear under alerts, /top cards and /rate cards:
    👍 Good fit   🤔 Maybe   👎 Not for me
Each tap is saved in data/agent.db (table `feedback`), with the AI's score at that moment,
so milestone 3 can compare your judgement with the AI's.

/rate shows a BLIND sample (no AI score or reasons, so they can't anchor your answer) from every
score band, weighted towards high scores so you see more likely-good jobs. Lower bands are still
included, because that's the only way to catch the AI wrongly scoring a good job low.

After 🤔 or 👎 you get a second row: "Why?" Tap every reason that applies, then Done.
The learning review (/learn) uses those reasons to propose new preference rules.
"""
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone

from .store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    chat_id INTEGER, msg_id INTEGER, job_idx INTEGER,
    verdict TEXT, source TEXT, rated_at TEXT,
    ai_status TEXT, ai_score INTEGER, ai_label TEXT, ai_model TEXT,
    PRIMARY KEY (chat_id, msg_id, job_idx)
);
"""
REASONS = {"r": "Wrong role type", "s": "Too senior", "b": "Sales/BD-heavy",
           "i": "Industry/company", "p": "Pay", "l": "Location/visa", "o": "Other"}
VERDICTS = {"g": ("good", "👍 Good fit"), "m": ("maybe", "🤔 Maybe"), "n": ("no", "👎 Not for me")}
SOURCES = {"a": "alert", "t": "top", "r": "rate"}
# /rate keeps the bands in proportion to these weights (more high-scored jobs = more 👍 examples)
BANDS = {
    "high": "s.status = 'kept' AND s.fit_score >= 60",
    "mid": "s.status = 'kept' AND s.fit_score BETWEEN 40 AND 59",
    "low": "s.status = 'kept' AND s.fit_score < 40",
    "skipped": "s.status = 'skipped' AND s.title IS NOT NULL",
}
BAND_WEIGHT = {"high": 4, "mid": 3, "skipped": 2, "low": 1}
RATE_WINDOW_DAYS = 30


def _db() -> sqlite3.Connection:
    db = Store().conn
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    if "reasons" not in [r[1] for r in db.execute("PRAGMA table_info(feedback)")]:
        db.execute("ALTER TABLE feedback ADD COLUMN reasons TEXT")
        db.commit()
    return db


def keyboard(chat_id, msg_id, job_idx, source: str, chosen: str | None = None, with_next: bool = False,
             asking: bool = False, reasons: list[str] | None = None) -> dict:
    """Verdict buttons; while `asking`, also the "Why?" reason buttons (tap several, then Done)."""
    row = []
    for code, (_, label) in VERDICTS.items():
        text = ("✅ " + label.split(" ", 1)[1]) if chosen == code else label
        row.append({"text": text, "callback_data": f"fb|{source}|{chat_id}|{msg_id}|{job_idx}|{code}"})
    rows = [row]
    if asking:
        picked = set(reasons or [])
        btns = [{"text": ("✅ " if c in picked else "") + label,
                 "callback_data": f"rs|{source}|{chat_id}|{msg_id}|{job_idx}|{c}"} for c, label in REASONS.items()]
        rows += [btns[i:i + 3] for i in range(0, len(btns), 3)]
        rows.append([{"text": "Done ✔", "callback_data": f"rd|{source}|{chat_id}|{msg_id}|{job_idx}"}])
    elif with_next:
        rows.append([{"text": "Next job ▶", "callback_data": "rate|next"}])
    return {"inline_keyboard": rows}


def save(chat_id: int, msg_id: int, job_idx: int, code: str, source: str) -> str:
    db = _db()
    s = db.execute("SELECT status, fit_score, fit_label, model FROM screened WHERE chat_id=? AND msg_id=? AND job_idx=?",
                   (chat_id, msg_id, job_idx)).fetchone()
    db.execute("""INSERT OR REPLACE INTO feedback (chat_id, msg_id, job_idx, verdict, source, rated_at,
                  ai_status, ai_score, ai_label, ai_model, reasons) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (chat_id, msg_id, job_idx, VERDICTS[code][0], SOURCES.get(source, source),
                datetime.now(timezone.utc).isoformat(),
                s["status"] if s else None, s["fit_score"] if s else None,
                s["fit_label"] if s else None, s["model"] if s else None, "[]"))
    db.commit()
    return VERDICTS[code][1]


def toggle_reason(chat_id: int, msg_id: int, job_idx: int, code: str) -> list[str]:
    """Add or remove one reason on a saved rating; returns the reason codes now picked."""
    db = _db()
    row = db.execute("SELECT reasons FROM feedback WHERE chat_id=? AND msg_id=? AND job_idx=?",
                     (chat_id, msg_id, job_idx)).fetchone()
    if row is None:
        return []
    picked = json.loads(row["reasons"] or "[]")
    picked = [c for c in picked if c != code] if code in picked else picked + [code]
    db.execute("UPDATE feedback SET reasons=? WHERE chat_id=? AND msg_id=? AND job_idx=?",
               (json.dumps(picked), chat_id, msg_id, job_idx))
    db.commit()
    return picked


def verdict_code(chat_id, msg_id, job_idx) -> str | None:
    row = _db().execute("SELECT verdict FROM feedback WHERE chat_id=? AND msg_id=? AND job_idx=?",
                        (chat_id, msg_id, job_idx)).fetchone()
    return next((c for c, (v, _) in VERDICTS.items() if row and v == row["verdict"]), None)


def reason_labels(codes) -> list[str]:
    return [REASONS.get(c, c) for c in codes or []]


def counts() -> dict:
    db = _db()
    by = dict(db.execute("SELECT verdict, COUNT(*) FROM feedback GROUP BY verdict").fetchall())
    blind = db.execute("SELECT COUNT(*) FROM feedback WHERE source = 'rate'").fetchone()[0]
    return {"total": sum(by.values()), "blind": blind, "good": by.get("good", 0),
            "maybe": by.get("maybe", 0), "no": by.get("no", 0)}


def band_of(score) -> str:
    if score is None:
        return "no"
    return "good" if score >= 60 else "maybe" if score >= 40 else "no"


def recent_accuracy(last: int = 20) -> dict:
    """How often the AI's score (at rating time) matched your blind verdict, over your last N /rate ratings."""
    rows = _db().execute("SELECT verdict, ai_score FROM feedback WHERE source='rate' AND ai_score IS NOT NULL "
                         "ORDER BY rated_at DESC LIMIT ?", (last,)).fetchall()
    if not rows:
        return {"n": 0, "agree": None, "big": 0}
    agree = sum(band_of(r["ai_score"]) == r["verdict"] for r in rows)
    big = sum({band_of(r["ai_score"]), r["verdict"]} == {"good", "no"} for r in rows)
    return {"n": len(rows), "agree": agree / len(rows), "big": big}


def next_to_rate(days: int = RATE_WINDOW_DAYS):
    """An unrated job from the band furthest below its share (BAND_WEIGHT), random within it."""
    db = _db()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    done = {}
    for band, cond in BANDS.items():
        done[band] = db.execute(
            f"""SELECT COUNT(*) FROM feedback f JOIN screened s ON s.chat_id=f.chat_id AND s.msg_id=f.msg_id
                AND s.job_idx=f.job_idx WHERE f.source='rate' AND {cond}""").fetchone()[0]
    for band in sorted(BANDS, key=lambda b: done[b] / BAND_WEIGHT[b]):
        rows = db.execute(
            f"""SELECT s.*, m.chat_title, m.date_utc, m.text FROM screened s
                JOIN messages m ON m.chat_id=s.chat_id AND m.msg_id=s.msg_id
                WHERE {BANDS[band]} AND s.screened_at >= ?
                  AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.chat_id=s.chat_id
                                  AND f.msg_id=s.msg_id AND f.job_idx=s.job_idx)""", (since,)).fetchall()
        if rows:
            return random.choice(rows), band, sum(done.values())
    return None, None, sum(done.values())


def blind_card(r, done: int) -> str:
    """The job's facts only: no AI score, label or reasons."""
    from .notify import _links, e
    try:
        reqs = json.loads(r["raw_json"] or "{}").get("requirements") or []
    except ValueError:
        reqs = []
    lines = [f"🎯 <b>Rate this job</b> (#{done + 1})", "",
             f"<b>{e(r['title'] or 'Untitled role')}</b>"]
    where = " · ".join(e(x) for x in (r["company"], r["location"], r["employment_type"]) if x)
    if where:
        lines.append(where)
    if r["salary"]:
        lines.append(f"💰 {e(r['salary'])}")
    if r["years_required_min"] is not None:
        lines.append(f"Experience asked: {r['years_required_min']:g}+ years")
    if reqs:
        lines.append("<b>Requirements:</b> " + e("; ".join(reqs[:5])))
    snippet = " ".join((r["text"] or "").split())[:400]
    if snippet:
        lines += ["", f"<i>{e(snippet)}{'…' if len(r['text'] or '') > 400 else ''}</i>"]
    links = _links(r)
    if links:
        lines += ["", links]
    lines += ["", "Would this role suit you? Judge the role and its requirements; ignore location and "
                  "remote rules (the agent checks those separately). After 🤔 or 👎, tap why."]
    return "\n".join(lines)
