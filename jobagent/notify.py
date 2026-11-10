"""
Telegram messages to YOU, sent by your bot (never by your own account).

  python -m jobagent.notify --test       # find your chat and send a test message
  python -m jobagent.notify --alerts     # send pending instant alerts (respects quiet hours)
  python -m jobagent.notify --digest     # send today's digest now (even if already sent)

The bot only ever sends to one chat: yours, found from the "hi" you sent it.
"""
import argparse
import html
import json
import os
import sqlite3
from datetime import datetime, time, timedelta, timezone

import requests

from .config import load_agent
from .settings import DATA_DIR, MYT
from .store import Store

STATE_PATH = DATA_DIR / "bot_state.json"
CHATS_PATH = DATA_DIR / "chat_info.json"   # chat id -> public username / membership, for post links
API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 3900  # Telegram's limit is 4096 characters per message

NOTIFIED_SCHEMA = """
CREATE TABLE IF NOT EXISTS notified (
    chat_id INTEGER, msg_id INTEGER, job_idx INTEGER, kind TEXT, sent_at TEXT,
    PRIMARY KEY (chat_id, msg_id, job_idx, kind)
);
CREATE TABLE IF NOT EXISTS digests (day TEXT PRIMARY KEY, sent_at TEXT);
"""


# ---------------- bot basics ----------------

def _token() -> str:
    t = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not t:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing from .env (the token from @BotFather).")
    return t


def _call(method: str, _http_timeout: float = 20, **payload):
    r = requests.post(API.format(token=_token(), method=method), json=payload, timeout=_http_timeout)
    data = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description') or r.status_code}")
    return data["result"]


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def my_chat_id() -> int:
    """Your private chat with the bot. From .env if set, else from the 'hi' you sent the bot."""
    env = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if env:
        return int(env)
    state = _load_state()
    if state.get("chat_id"):
        return int(state["chat_id"])
    for upd in reversed(_call("getUpdates", timeout=0, allowed_updates=["message"])):
        chat = (upd.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private":
            state["chat_id"] = chat["id"]
            state["chat_name"] = chat.get("first_name", "")
            _save_state(state)
            return int(chat["id"])
    raise SystemExit("Couldn't find your chat. Open your bot in Telegram, press Start, send 'hi', then try again.")


def _split(text: str) -> list[str]:
    parts, cur = [], ""
    for block in text.split("\n\n"):
        piece = (block + "\n\n")
        if len(cur) + len(piece) > MAX_LEN and cur:
            parts.append(cur.rstrip())
            cur = ""
        while len(piece) > MAX_LEN:      # a single huge block
            parts.append(piece[:MAX_LEN])
            piece = piece[MAX_LEN:]
        cur += piece
    if cur.strip():
        parts.append(cur.rstrip())
    return parts


def send_file(path, caption_html: str = ""):
    """Send a file (e.g. the CSV export) to your chat. Telegram bots can send files up to 50 MB."""
    with open(path, "rb") as f:
        r = requests.post(API.format(token=_token(), method="sendDocument"),
                          data={"chat_id": my_chat_id(), "caption": caption_html, "parse_mode": "HTML"},
                          files={"document": (os.path.basename(str(path)), f, "text/csv")}, timeout=120)
    data = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendDocument failed: {data.get('description') or r.status_code}")


def send_job(text_html: str, r, source: str, with_next: bool = False):
    """One job card with 👍/🤔/👎 buttons underneath (one message; never split)."""
    from . import feedback
    _call("sendMessage", chat_id=my_chat_id(), text=text_html[:4000], parse_mode="HTML",
          link_preview_options={"is_disabled": True},
          reply_markup=feedback.keyboard(r["chat_id"], r["msg_id"], r["job_idx"], source, with_next=with_next))


def send(text_html: str):
    chat = my_chat_id()
    for part in _split(text_html):
        _call("sendMessage", chat_id=chat, text=part, parse_mode="HTML",
              link_preview_options={"is_disabled": True})


# ---------------- formatting ----------------

def e(x) -> str:
    return html.escape(str(x or ""), quote=False)


def _lst(x, n=3) -> str:
    try:
        items = json.loads(x or "[]")
    except ValueError:
        items = [x] if x else []
    return "; ".join(str(i) for i in items[:n])


def _link(url, label="Apply") -> str:
    if not url:
        return ""
    url = url if "://" in url else "https://" + url
    return f'<a href="{html.escape(url, quote=True)}">{e(label)}</a>'


def chat_link_info(ent) -> dict:
    """Public username (if any) and whether you're a member, from a Telethon chat."""
    username = getattr(ent, "username", None)
    if not username:
        for u in getattr(ent, "usernames", None) or []:
            if getattr(u, "active", False):
                username = u.username
                break
    return {"username": username, "member": not getattr(ent, "left", False)}


def save_chat_info(info: dict):
    try:
        old = json.loads(CHATS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    old.update(info)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHATS_PATH.write_text(json.dumps(old, indent=2), encoding="utf-8")


_chat_cache = None


def post_link(chat_id: int, msg_id: int) -> str | None:
    """A link to the original post that opens for you:
    public channel -> t.me/<username>/<id> (works for anyone);
    private channel you joined -> t.me/c/<id>/<msg> (members only);
    private channel you didn't join -> None (no link that can't open)."""
    global _chat_cache
    if _chat_cache is None:
        try:
            _chat_cache = json.loads(CHATS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _chat_cache = {}
    info = _chat_cache.get(str(chat_id)) or {}
    if info.get("username"):
        return f"https://t.me/{info['username']}/{msg_id}"
    cid = str(chat_id)
    if cid.startswith("-100") and info.get("member"):
        return f"https://t.me/c/{cid[4:]}/{msg_id}"
    return None


def _links(r) -> str:
    parts = []
    if r["apply_link"]:
        parts.append(_link(r["apply_link"]))
    tg = post_link(r["chat_id"], r["msg_id"])
    if tg:
        parts.append(_link(tg, "Open post"))
    return " · ".join(parts)


def _icon(score) -> str:
    s = score or 0
    return "🟢" if s >= 75 else "🟡" if s >= 60 else "⚪"


def format_alert(r) -> str:
    where = " · ".join(e(x) for x in (r["company"], r["location"], r["employment_type"]) if x)
    lines = [f"{_icon(r['fit_score'])} <b>{r['fit_score']}</b> · <b>{e(r['title'] or 'Untitled role')}</b>"]
    if where:
        lines.append(where)
    if r["salary"]:
        lines.append(f"💰 {e(r['salary'])}")
    for label, key in (("Why", "why_fit"), ("You have", "requirements_met"), ("Gaps", "gaps"), ("Stretch", "stretch")):
        val = _lst(r[key])
        if val:
            lines.append(f"<b>{label}:</b> {e(val)}")
    flags = _lst(r["flags"], 4)
    if flags:
        lines.append(f"⚠️ {e(flags)}")
    posted = datetime.fromisoformat(r["date_utc"]).astimezone(MYT).strftime("%d %b %H:%M")
    lines.append(" · ".join(x for x in (_links(r), e(r["chat_title"]), posted) if x))
    return "\n".join(lines)


# ---------------- alerts ----------------

def _in_quiet_hours(now: datetime, spec: str) -> bool:
    if not spec:
        return False
    a, b = [time.fromisoformat(x.strip()) for x in spec.split("-")]
    t = now.timetz().replace(tzinfo=None)
    return (a <= t or t < b) if a > b else (a <= t < b)


def _rows(db, where: str, args=()):
    db.row_factory = sqlite3.Row
    return db.execute(
        f"""SELECT s.*, m.chat_title, m.date_utc FROM screened s
            JOIN messages m ON m.chat_id = s.chat_id AND m.msg_id = s.msg_id
            WHERE {where}""", args).fetchall()


def send_alerts(now: datetime | None = None) -> int:
    cfg = (load_agent().get("notify") or {})
    now = now or datetime.now(MYT)
    if _in_quiet_hours(now, cfg.get("quiet_hours", "")):
        return 0
    db = Store().conn
    db.executescript(NOTIFIED_SCHEMA)
    since = (now - timedelta(hours=24)).astimezone(timezone.utc).isoformat()
    rows = _rows(db, """s.status = 'kept' AND s.fit_score >= ? AND s.screened_at >= ?
                        AND NOT EXISTS (SELECT 1 FROM notified n WHERE n.chat_id = s.chat_id
                            AND n.msg_id = s.msg_id AND n.job_idx = s.job_idx AND n.kind = 'alert')
                        ORDER BY s.fit_score DESC""", (cfg.get("alert_min_score", 60), since))
    sent = 0
    for r in rows[: cfg.get("max_alerts_per_run", 5)]:
        send_job(format_alert(r), r, "a")
        db.execute("INSERT OR REPLACE INTO notified VALUES (?,?,?,?,?)",
                   (r["chat_id"], r["msg_id"], r["job_idx"], "alert", datetime.now(timezone.utc).isoformat()))
        db.commit()
        sent += 1
    return sent


# ---------------- digest ----------------

def build_digest(now: datetime | None = None) -> str:
    cfg = (load_agent().get("notify") or {})
    now = now or datetime.now(MYT)
    db = Store().conn
    since = (now - timedelta(hours=24)).astimezone(timezone.utc).isoformat()
    all_rows = _rows(db, "s.screened_at >= ?", (since,))
    kept = sorted([r for r in all_rows if r["status"] == "kept"], key=lambda r: -(r["fit_score"] or 0))
    near = sorted([r for r in all_rows if r["status"] == "skipped" and (r["fit_score"] or 0) >= cfg.get("near_miss_min_score", 55)],
                  key=lambda r: -(r["fit_score"] or 0))
    cost = sum((r["cost_usd"] or 0) for r in all_rows)
    counts = {}
    for r in all_rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    out = [f"📋 <b>Job digest · {now.strftime('%a %d %b')}</b>",
           f"Last 24 h: {len(all_rows)} jobs screened · kept {counts.get('kept', 0)} · "
           f"skipped {counts.get('skipped', 0)} · AI cost RM{cost * 4.09:.2f}"]

    top = kept[: cfg.get("digest_top_n", 10)]
    out.append("")
    out.append("<b>Top matches</b>" if top else "<b>Top matches</b>\nNo jobs passed your rules today.")
    for i, r in enumerate(top, 1):
        bits = " · ".join(e(x) for x in (r["company"], r["location"]) if x)
        why = _lst(r["why_fit"], 1)
        out.append(f"{i}. {_icon(r['fit_score'])} <b>{r['fit_score']}</b> {e(r['title'] or 'Untitled')}"
                   + (f"\n    {bits}" if bits else "") + (f"\n    {e(why)}" if why else "")
                   + (f"\n    {_links(r)}" if _links(r) else ""))

    if near:
        out.append("")
        out.append("<b>Near misses</b> (strong fit, skipped by a rule)")
        for r in near[:5]:
            out.append(f"• <b>{r['fit_score']}</b> {e(r['title'] or 'Untitled')} — {e(_lst(r['reasons'], 2))}"
                       + (f"  {_links(r)}" if _links(r) else ""))
    errors = counts.get("error", 0)
    if errors:
        out.append(f"\n⚠️ {errors} post(s) couldn't be read by the AI; run screen --retry-errors.")
    return "\n".join(out)


def send_digest_if_due(now: datetime | None = None, force: bool = False) -> bool:
    cfg = (load_agent().get("notify") or {})
    now = now or datetime.now(MYT)
    db = Store().conn
    db.executescript(NOTIFIED_SCHEMA)
    day = now.strftime("%Y-%m-%d")
    due = now.timetz().replace(tzinfo=None) >= time.fromisoformat(cfg.get("digest_time", "21:00"))
    already = db.execute("SELECT 1 FROM digests WHERE day = ?", (day,)).fetchone()
    if not force and (not due or already):
        return False
    send(build_digest(now))
    if not force:  # an on-demand /digest doesn't replace the scheduled evening one
        db.execute("INSERT OR REPLACE INTO digests VALUES (?, ?)", (day, datetime.now(timezone.utc).isoformat()))
        db.commit()
    return True


# ---------------- bot commands ----------------

COMMANDS = [
    ("status", "Is the agent running? Jobs, alerts and cost today"),
    ("top", "Today's top 5 jobs"),
    ("digest", "Send the digest now"),
    ("export", "Get every screened job as a CSV file"),
    ("rate", "Rate a job blind (teaches the agent your taste)"),
    ("learn", "Review my ratings now and propose new rules"),
    ("rules", "Show learned rules (remove any)"),
    ("pause", "Pause screening and alerts (stops AI spending)"),
    ("resume", "Resume screening and alerts"),
    ("help", "What each command does"),
]


def set_commands():
    """Puts the commands in the bot's '/' menu, so you can tap instead of type."""
    _call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])


def help_text() -> str:
    lines = ["🤖 <b>Job agent commands</b>"] + [f"/{c} – {e(d)}" for c, d in COMMANDS]
    lines.append("\nCommands work while the agent is running on your laptop.")
    return "\n".join(lines)


def get_events(offset: int | None, wait_seconds: int = 25) -> tuple[list[dict], int | None]:
    """Long-poll the bot. Returns (events from YOU only, next offset). Anyone else is ignored.
    Event: {"type": "cmd", "name": "status"} or {"type": "tap", "data": ..., "id": ..., "message_id": ...}."""
    me = my_chat_id()
    updates = _call("getUpdates", _http_timeout=wait_seconds + 15, offset=offset,
                    timeout=wait_seconds, allowed_updates=["message", "callback_query"])
    events, nxt = [], offset
    for u in updates:
        nxt = u["update_id"] + 1
        if "callback_query" in u:
            cq = u["callback_query"]
            if (cq.get("from") or {}).get("id") != me:
                continue
            events.append({"type": "tap", "data": cq.get("data", ""), "id": cq["id"],
                           "message_id": ((cq.get("message") or {}).get("message_id"))})
            continue
        msg = u.get("message") or {}
        if (msg.get("chat") or {}).get("id") != me:
            continue
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            events.append({"type": "cmd", "name": text.split()[0].split("@")[0][1:].lower()})
    return events, nxt


def answer_tap(callback_id: str, text: str = ""):
    _call("answerCallbackQuery", callback_query_id=callback_id, text=text)


def mark_choice(message_id: int, keyboard: dict):
    _call("editMessageReplyMarkup", chat_id=my_chat_id(), message_id=message_id, reply_markup=keyboard)


def _today_utc_start(now: datetime) -> str:
    start = now.astimezone(MYT).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat()


def _ratings_line() -> str:
    from . import feedback
    from . import learned
    c = feedback.counts()
    acc = feedback.recent_accuracy()
    line = f"Your ratings: {c['total']} ({c['good']} 👍 · {c['maybe']} 🤔 · {c['no']} 👎)"
    if acc["agree"] is not None:
        line += f"\nAI matched you on {acc['agree']:.0%} of your last {acc['n']} blind ratings"
    return line + f" · {len(learned.active())} learned rule(s) (/rules)"


def status_text(info: dict) -> str:
    """info: running_since, last_cycle, next_cycle (datetimes or None), chats (int), paused (bool)."""
    now = datetime.now(MYT)
    db = Store().conn
    db.executescript(NOTIFIED_SCHEMA)
    since = _today_utc_start(now)
    counts = dict(db.execute("SELECT status, COUNT(*) FROM screened WHERE screened_at >= ? GROUP BY status",
                             (since,)).fetchall())
    cost = db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM screened WHERE screened_at >= ?", (since,)).fetchone()[0]
    alerts = db.execute("SELECT COUNT(*) FROM notified WHERE kind = 'alert' AND sent_at >= ?", (since,)).fetchone()[0]
    cap = float(((load_agent().get("budget") or {}).get("daily_usd", 0.6)))

    def t(x):
        return x.astimezone(MYT).strftime("%H:%M") if x else "–"

    state = "⏸ <b>Paused</b> (fetching only; no AI, no alerts)" if info.get("paused") else "✅ <b>Running</b>"
    return "\n".join([
        f"{state} · watching {info.get('chats', 0)} chats",
        f"Since {info['running_since'].astimezone(MYT).strftime('%d %b %H:%M')} · last check {t(info.get('last_cycle'))}"
        f" · next {t(info.get('next_cycle'))}",
        f"Today: {sum(counts.values())} jobs screened · kept {counts.get('kept', 0)} · "
        f"skipped {counts.get('skipped', 0)} · alerts {alerts}",
        f"AI cost today: RM{cost * 4.09:.2f} of RM{cap * 4.09:.2f} cap",
        _ratings_line()
        + (f"\n⚠️ {counts['error']} unreadable post(s)" if counts.get("error") else ""),
    ])


def send_top(n: int = 5):
    """Today's top jobs, one card each, with feedback buttons."""
    now = datetime.now(MYT)
    db = Store().conn
    since = (now - timedelta(hours=24)).astimezone(timezone.utc).isoformat()
    rows = sorted(_rows(db, "s.status = 'kept' AND s.screened_at >= ?", (since,)),
                  key=lambda r: -(r["fit_score"] or 0))[:n]
    if not rows:
        send("No kept jobs in the last 24 hours yet.")
        return
    send(f"🏆 <b>Top {len(rows)} (last 24 h)</b>. Tap a button under each to rate it.")
    for r in rows:
        send_job(format_alert(r), r, "t")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--alerts", action="store_true")
    ap.add_argument("--digest", action="store_true")
    args = ap.parse_args()
    if args.test:
        cid = my_chat_id()
        send("✅ <b>Job agent connected.</b>\nAlerts and the daily digest will arrive in this chat.")
        print(f"Test message sent to chat {cid}. Saved in {STATE_PATH}.")
    if args.alerts:
        print(f"Alerts sent: {send_alerts()}")
    if args.digest:
        send_digest_if_due(force=True)
        print("Digest sent.")
    if not (args.test or args.alerts or args.digest):
        ap.print_help()


if __name__ == "__main__":
    main()
