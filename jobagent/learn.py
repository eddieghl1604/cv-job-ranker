"""
The learning loop: the agent studies your ratings and proposes new preference rules.

  python -m jobagent.learn          # run a review now (same as /learn in your bot)
  python -m jobagent.learn --dry    # show what it would send the AI, without calling it

How a review works:
  1. Find jobs where the AI's score disagreed with your verdict, with the reasons you tapped.
  2. Ask Claude to explain the pattern as at most 3 short, general rules.
  3. TEST each rule: re-screen all your rated jobs with the rule added, and compare with the
     current rules. A rule is only shown to you if it makes the agent agree with you more
     often, with no extra big misses and no extra missed 👍.
  4. You tap Approve or Reject in Telegram. Approved rules go into config/learned.yaml and
     apply from the next post. Rejected ones are remembered and never proposed again.

It also decides when to ask you for a spot-check rating (fewer as accuracy rises), and when
to re-run the model test (each time you reach another 15 👍).
Cost: one review is roughly 4 x (number of rated jobs) Claude calls, about USD 0.10-0.20.
"""
import argparse
import difflib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import feedback, learned
from .compare import Compare
from .config import load_agent, load_profile, profile_text
from .settings import MYT

MAX_RULES = 3
PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "patterns_seen": {"type": "string", "description": "2-4 sentences: what the disagreements have in common"},
        "rules": {
            "type": "array", "maxItems": MAX_RULES,
            "items": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string", "description": "One general scoring instruction, max 60 words"},
                    "why": {"type": "string", "description": "Which ratings and reasons support it, max 30 words"},
                    "based_on": {"type": "array", "items": {"type": "string"}, "description": "job titles"},
                },
                "required": ["rule", "why", "based_on"],
            },
        },
    },
    "required": ["patterns_seen", "rules"],
}
INSTRUCTIONS = """You improve a job-screening assistant for one person (Eddie).
The assistant gives each job a fit score 0-100 (60+ = Eddie gets an alert; 40-59 = maybe; below 40 = not shown).
Eddie rated jobs himself (good / maybe / no) and sometimes tapped reasons. Below are the jobs where the
assistant's score DISAGREED with Eddie, plus some where it agreed, for contrast.

Write at most 3 NEW rules that would make the assistant's scores match Eddie better. Rules must:
- be general (about role function, seniority, tasks, industry, pay), never about one named company or post;
- be supported by at least 2 of the rated jobs, and not contradict any job Eddie rated "good";
- say concretely how to score (e.g. "score below 35", "treat as 60-75");
- not repeat or contradict the current learned rules, and not repeat a rejected rule;
- not touch location, remote, language or years-of-experience limits (code already checks those).
If the data shows no clear pattern, return an empty list. Fewer, sharper rules are better."""


def _db():
    db = feedback._db()
    db.executescript("""CREATE TABLE IF NOT EXISTS learn_proposals (
        id TEXT PRIMARY KEY, text TEXT, why TEXT, based_on TEXT, created TEXT,
        base TEXT, cand TEXT, passed INTEGER, status TEXT)""")
    return db


def _settings() -> dict:
    s = (load_agent().get("learning") or {})
    return {"review_day": s.get("review_day", "Sun"), "review_time": s.get("review_time", "20:00"),
            "min_new_ratings": s.get("min_new_ratings", 5), "min_ratings": s.get("min_ratings", 15),
            "retest_every_likes": s.get("retest_every_likes", 15),
            "retest_providers": s.get("retest_providers", ["anthropic", "groq-small"]),
            "spot_checks": s.get("spot_checks", True)}


def _state():
    from .notify import _load_state
    return _load_state()


def _set_state(**kw):
    from .notify import _load_state, _save_state
    st = _load_state()
    st.update(kw)
    _save_state(st)


# ---------- 1. what disagreed ----------

def examples(limit_dis=30, limit_agree=10):
    rows = _db().execute(
        """SELECT f.verdict, f.reasons, f.ai_score, s.title, s.company, s.employment_type, s.salary,
                  s.years_required_min, s.raw_json, s.why_fit, s.gaps
           FROM feedback f JOIN screened s ON s.chat_id=f.chat_id AND s.msg_id=f.msg_id AND s.job_idx=f.job_idx
           ORDER BY f.rated_at DESC""").fetchall()
    dis, agree = [], []
    for r in rows:
        (dis if feedback.band_of(r["ai_score"]) != r["verdict"] else agree).append(r)
    # contrast: every 'good'/'maybe' it got right, then a few agreed 'no'
    agree.sort(key=lambda r: r["verdict"] == "no")
    return dis[:limit_dis], agree[:limit_agree], len(rows)


def _describe(r) -> str:
    try:
        reqs = (json.loads(r["raw_json"] or "{}").get("requirements") or [])[:4]
    except ValueError:
        reqs = []
    reasons = ", ".join(feedback.reason_labels(json.loads(r["reasons"] or "[]"))) or "none given"
    bits = [f"Title: {r['title']}", f"Company: {r['company'] or '?'}", f"Type: {r['employment_type'] or '?'}"]
    if r["years_required_min"] is not None:
        bits.append(f"Years asked: {r['years_required_min']:g}")
    if reqs:
        bits.append("Requirements: " + "; ".join(reqs))
    return (" | ".join(bits) + f"\n  AI score {r['ai_score']} -> Eddie said {r['verdict'].upper()} "
            f"(reasons: {reasons})")


def build_request():
    dis, agree, n = examples()
    cur = [r["text"] for r in learned.active()]
    rej = [r["text"] for r in learned.load()["rejected"]][-15:]
    parts = ["EDDIE'S PROFILE (what the assistant already knows):", profile_text(load_profile()), "",
             f"CURRENT LEARNED RULES: {json.dumps(cur, ensure_ascii=False) if cur else 'none'}",
             f"REJECTED RULES (never propose again): {json.dumps(rej, ensure_ascii=False) if rej else 'none'}", "",
             f"DISAGREEMENTS ({len(dis)} of {n} rated jobs):"]
    parts += [f"{i}. {_describe(r)}" for i, r in enumerate(dis, 1)]
    parts += ["", "AGREEMENTS (for contrast):"] + [f"- {_describe(r)}" for r in agree]
    return "\n".join(parts), len(dis), n


# ---------- 2. ask for rules ----------

def propose() -> tuple[list[dict], str]:
    from .llm import make_provider
    user, n_dis, _ = build_request()
    if n_dis == 0:
        return [], "The AI already agrees with every rating you gave."
    p = make_provider(load_agent(), "anthropic")
    resp = p.client.messages.create(
        model=p.model, max_tokens=1500, system=INSTRUCTIONS,
        messages=[{"role": "user", "content": user}],
        tools=[{"name": "propose_rules", "description": "Return proposed rules", "input_schema": PROPOSE_SCHEMA}],
        tool_choice={"type": "tool", "name": "propose_rules"})
    data = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), {}) or {}
    known = [r["text"] for r in learned.active()] + [r["text"] for r in learned.load()["rejected"]]
    out = []
    for r in (data.get("rules") or [])[:MAX_RULES]:
        text = " ".join(str(r.get("rule", "")).split())
        if not text or any(difflib.SequenceMatcher(None, text.lower(), k.lower()).ratio() > 0.8 for k in known):
            continue
        out.append({"text": text, "why": str(r.get("why", "")), "based_on": r.get("based_on") or []})
    return out, str(data.get("patterns_seen", ""))


# ---------- 3. test each rule on your past ratings ----------

def _score(c: Compare, label: str, jobs, log, extra=None):
    name = load_agent().get("provider", "anthropic")
    c.run_provider(name, jobs, log, label=label, extra_rules=extra)
    _, res = c.table(label=label)
    rows = next(iter(res.values()), {})
    return c.metrics(jobs, rows)


def passes(base, cand) -> bool:
    if not base or not cand or cand["n"] < base["n"] * 0.9:
        return False
    no_worse = cand["agree"] >= base["agree"] and cand["big"] <= base["big"] and cand["missed"] <= base["missed"]
    better = (cand["agree"] > base["agree"] or cand["big"] < base["big"]
              or (cand["rho"] or 0) > (base["rho"] or 0) + 0.02)
    return no_worse and better


def _fmt(base, cand) -> str:
    rho = lambda x: f"{x['rho']:.2f}" if x.get("rho") is not None else "-"
    return (f"agree {base['agree']:.0%} → {cand['agree']:.0%} · big misses {base['big']} → {cand['big']} · "
            f"missed 👍 {base['missed']} → {cand['missed']} · rank ρ {rho(base)} → {rho(cand)} "
            f"(1 = orders jobs exactly like you)")


def review(log=print, send=True) -> str:
    """One full review. Returns a short summary (also sent to Telegram when send=True)."""
    from . import notify
    cfg = _settings()
    c = Compare()
    jobs = c.rated()
    if len(jobs) < cfg["min_ratings"]:
        msg = f"Only {len(jobs)} ratings so far; the review needs {cfg['min_ratings']}. Keep tapping /rate."
        if send:
            notify.send(msg)
        return msg
    rules, patterns = propose()
    fp = learned.fingerprint()
    base = _score(c, f"learn-base@{fp}", jobs, log)
    db = _db()
    shown, dropped = [], []
    for r in rules:
        rid = learned.rule_id(r["text"])
        cand = _score(c, f"learn-{rid}@{fp}", jobs, log, extra=[r["text"]])
        ok = passes(base, cand)
        db.execute("INSERT OR REPLACE INTO learn_proposals VALUES (?,?,?,?,?,?,?,?,?)",
                   (rid, r["text"], r["why"], json.dumps(r["based_on"], ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps({k: v for k, v in (base or {}).items() if k != "_pairs"}),
                    json.dumps({k: v for k, v in (cand or {}).items() if k != "_pairs"}),
                    int(ok), "pending" if ok else "dropped"))
        db.commit()
        (shown if ok else dropped).append((rid, r, cand))
    _set_state(last_review=datetime.now(MYT).strftime("%Y-%m-%d"), ratings_at_review=len(jobs))

    head = [f"🧠 <b>Learning review</b> ({len(jobs)} ratings)"]
    if base:
        head.append(f"Current rules: agree {base['agree']:.0%} · big misses {base['big']} · "
                    f"missed 👍 {base['missed']}/{base['goods']}")
    if patterns:
        head.append(f"<i>{notify.e(patterns)}</i>")
    if not rules:
        head.append("No new rule worth proposing this time.")
    for rid, r, cand in dropped:
        why_not = (f" ({_fmt(base, cand)})" if base and cand else "")
        head.append(f"✖ Tested and dropped: {notify.e(r['text'][:300])}{why_not}")
    if send:
        notify.send("\n\n".join(head))
        for rid, r, cand in shown:
            text = (f"📚 <b>Proposed rule</b>\n{notify.e(r['text'])}\n\n<b>Why:</b> {notify.e(r['why'])}\n"
                    f"<b>Test on your {cand['n']} ratings:</b> {_fmt(base, cand)}")
            notify._call("sendMessage", chat_id=notify.my_chat_id(), text=text, parse_mode="HTML",
                         reply_markup={"inline_keyboard": [[
                             {"text": "✅ Approve", "callback_data": f"lr|a|{rid}"},
                             {"text": "❌ Reject", "callback_data": f"lr|r|{rid}"}]]})
    return f"{len(shown)} rule(s) proposed, {len(dropped)} dropped."


def decide(rid: str, approve: bool) -> str:
    db = _db()
    row = db.execute("SELECT * FROM learn_proposals WHERE id=?", (rid,)).fetchone()
    if row is None:
        return "That proposal no longer exists."
    if row["status"] != "pending":
        return f"Already {row['status']}."
    if approve:
        learned.add(row["text"], row["why"], source="approved after test")
    else:
        learned.reject(row["text"])
    db.execute("UPDATE learn_proposals SET status=? WHERE id=?", ("approved" if approve else "rejected", rid))
    db.commit()
    return "✅ Approved: applies from the next post." if approve else "❌ Rejected: won't be proposed again."


def rules_message() -> tuple[str, dict | None]:
    from .notify import e
    rules = learned.active()
    if not rules:
        return "No learned rules yet. They appear after a /learn review and your approval.", None
    lines = ["📏 <b>Learned rules</b> (tap 🗑 to remove one)"]
    kb = []
    for i, r in enumerate(rules, 1):
        lines.append(f"\n<b>{i}.</b> {e(r['text'])}\n<i>added {e(r.get('added', ''))}</i>")
        kb.append([{"text": f"🗑 Remove rule {i}", "callback_data": f"lr|d|{r['id']}"}])
    return "\n".join(lines), {"inline_keyboard": kb}


# ---------- schedules ----------

def review_due(now: datetime | None = None) -> bool:
    cfg = _settings()
    now = (now or datetime.now(MYT)).astimezone(MYT)
    st = _state()
    if now.strftime("%a") != cfg["review_day"] or now.strftime("%H:%M") < cfg["review_time"]:
        return False
    if st.get("last_review") == now.strftime("%Y-%m-%d"):
        return False
    total = feedback.counts()["total"]
    return total >= cfg["min_ratings"] and total - st.get("ratings_at_review", 0) >= cfg["min_new_ratings"]


def spot_interval_hours() -> float:
    """Fewer rating requests as the AI gets better at matching your blind ratings."""
    acc = feedback.recent_accuracy(20)
    if acc["n"] < 10 or acc["agree"] is None:
        return 12
    if acc["agree"] >= 0.8 and acc["big"] == 0:
        return 72      # about 2-3 a week
    if acc["agree"] >= 0.65:
        return 24
    return 12


def spot_due(now: datetime | None = None) -> bool:
    if not _settings()["spot_checks"]:
        return False
    now = now or datetime.now(timezone.utc)
    last_rated = _db().execute("SELECT MAX(rated_at) FROM feedback").fetchone()[0]
    times = [datetime.fromisoformat(t) for t in (_state().get("last_spot"), last_rated) if t]
    # a rating you gave on your own counts too: no nagging right after you rated
    return not times or now - max(times) >= timedelta(hours=spot_interval_hours())


def mark_spot(now: datetime | None = None):
    _set_state(last_spot=(now or datetime.now(timezone.utc)).isoformat())


def retest_due() -> bool:
    cfg = _settings()
    likes = feedback.counts()["good"]
    every = cfg["retest_every_likes"]
    return likes >= every and likes // every > _state().get("retest_level", 0)


def retest(log=print) -> str:
    from . import notify
    from .compare import run_models
    cfg = _settings()
    level = feedback.counts()["good"] // cfg["retest_every_likes"]
    _set_state(retest_level=level)
    text = run_models(cfg["retest_providers"], log)
    notify.send("<b>🧪 Automatic model re-test</b> (you reached "
                f"{level * cfg['retest_every_likes']} 👍)\n<pre>" + notify.e(text) + "</pre>")
    return text


def main():
    ap = argparse.ArgumentParser(description="Review your ratings and propose new rules.")
    ap.add_argument("--dry", action="store_true", help="print the request, don't call the AI")
    a = ap.parse_args()
    from . import settings  # noqa: F401  (loads .env)
    if a.dry:
        print(build_request()[0])
        return
    print(review(lambda s: print(s, flush=True)))


if __name__ == "__main__":
    main()
