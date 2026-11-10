"""
Milestone 2: screen stored job posts with AI, apply your rules, and export the results.

  python -m jobagent.screen --dry-run --limit 20   # no AI: what would happen + cost estimate
  python -m jobagent.screen --limit 20             # screen the 20 newest unscreened job posts
  python -m jobagent.screen --retry-errors         # try again on posts that failed last time
  python -m jobagent.screen --provider mock --limit 20   # free fake AI, to test the plumbing
  python -m jobagent.screen --export               # write data/screened.csv
  python -m jobagent.screen --reset                # forget all results (messages are kept)

Order for each post (cheapest first):
  1. language filter (free)          only English/Chinese posts go on
  2. duplicate check (free)          same specific job link or same text already screened -> skipped
  3. read the job link (free)        only for short posts
  4. AI: extract + score             one call per post; a post listing 5 jobs gives 5 results
  5. evidence check (free)           the AI must quote the post; unverifiable facts can't skip a job
  6. your hard rules (free)          remote, Malaysia-OK, years, languages
Every result, including every skip and its reason, is saved in data/agent.db.
"""
import argparse
import csv
import json
import sqlite3
from datetime import datetime, timezone

from . import linkread, rules
from .config import load_agent, load_profile, profile_text
from .llm import make_provider
from .prompt import system_prompt, user_message
from .schema import parse_data, parse_reply
from .settings import DATA_DIR, MYT
from .store import Store

EXPORT_PATH = DATA_DIR / "screened.csv"
USD_TO_MYR = 4.09  # approximate, Nov 2026

SCHEMA = """
CREATE TABLE IF NOT EXISTS screened (
    chat_id INTEGER, msg_id INTEGER, job_idx INTEGER DEFAULT 0, screened_at TEXT, status TEXT,
    reasons TEXT, flags TEXT,
    title TEXT, company TEXT, location TEXT, remote TEXT, open_to_malaysia TEXT,
    employment_type TEXT, years_required_min REAL, required_languages TEXT,
    salary TEXT, deadline TEXT, apply_link TEXT,
    fit_score INTEGER, fit_label TEXT, why_fit TEXT, requirements_met TEXT, gaps TEXT, stretch TEXT,
    evidence_ok REAL, link_used TEXT, link_status TEXT,
    dup_of TEXT, link_key TEXT, text_key TEXT, role_key TEXT,
    provider TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cost_usd REAL, seconds REAL, raw_json TEXT,
    PRIMARY KEY (chat_id, msg_id, job_idx)
);
CREATE INDEX IF NOT EXISTS idx_screened_link ON screened (link_key);
CREATE INDEX IF NOT EXISTS idx_screened_text ON screened (text_key);
CREATE INDEX IF NOT EXISTS idx_screened_role ON screened (role_key);
"""


def ensure_schema(db: sqlite3.Connection):
    """Create the table; an older one-job-per-post table is renamed and kept as a backup."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(screened)").fetchall()]
    if cols and "job_idx" not in cols:
        n = 1
        while db.execute("SELECT name FROM sqlite_master WHERE name = ?", (f"screened_old_{n}",)).fetchone():
            n += 1
        db.execute(f"ALTER TABLE screened RENAME TO screened_old_{n}")
        for idx in ("idx_screened_keys", "idx_screened_link", "idx_screened_text", "idx_screened_role"):
            db.execute(f"DROP INDEX IF EXISTS {idx}")
        print(f"(Upgraded the results table; earlier results kept as screened_old_{n}.)")
    db.executescript(SCHEMA)


def _j(x):
    return json.dumps(x, ensure_ascii=False)


class Screener:
    def __init__(self, provider_override=None, dry_run=False):
        self.profile = load_profile()
        self.agent = load_agent()
        self.rules = self.profile["rules"]
        self.store = Store()
        self.db: sqlite3.Connection = self.store.conn
        ensure_schema(self.db)
        self.dry_run = dry_run
        self.provider = None if dry_run else make_provider(self.agent, provider_override)
        structured = bool(self.provider and self.provider.structured)
        self.system = system_prompt(profile_text(self.profile), for_text_reply=not structured)
        budget = self.agent.get("budget") or {}
        self.daily_usd = float(budget.get("daily_usd", 0.6))
        self.links_cfg = self.agent.get("links") or {}

    # ---------- helpers ----------
    def spent_today(self) -> float:
        today = datetime.now(MYT).strftime("%Y-%m-%d")
        rows = self.db.execute("SELECT screened_at, cost_usd FROM screened WHERE cost_usd > 0").fetchall()
        return sum(c for t, c in rows if datetime.fromisoformat(t).astimezone(MYT).strftime("%Y-%m-%d") == today)

    def find_dup(self, column: str, key, exclude_msg=None):
        if not key:
            return None
        q = f"SELECT chat_id, msg_id FROM screened WHERE {column} = ? AND status != 'error'"
        args = [key]
        if exclude_msg:
            q += " AND NOT (chat_id = ? AND msg_id = ?)"
            args += list(exclude_msg)
        row = self.db.execute(q + " LIMIT 1", args).fetchone()
        return f"{row[0]}/{row[1]}" if row else None

    def save(self, msg, job_idx=0, **f):
        f.setdefault("screened_at", datetime.now(timezone.utc).isoformat())
        f["chat_id"], f["msg_id"], f["job_idx"] = msg["chat_id"], msg["msg_id"], job_idx
        cols = ", ".join(f)
        self.db.execute(f"INSERT OR REPLACE INTO screened ({cols}) VALUES ({', '.join('?' * len(f))})",
                        list(f.values()))

    def pending(self, limit: int, max_age_days: float | None = None):
        rows = self.db.execute(
            """SELECT m.* FROM messages m
               WHERE m.is_candidate = 1
                 AND NOT EXISTS (SELECT 1 FROM screened s WHERE s.chat_id = m.chat_id AND s.msg_id = m.msg_id)
               ORDER BY m.date_utc DESC""").fetchall()
        if max_age_days:
            cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
            rows = [r for r in rows if datetime.fromisoformat(r["date_utc"]).timestamp() >= cutoff]
        return rows[:limit]

    def _ask(self, user):
        """One AI call, plus one retry if the answer can't be read. Returns (result, replies)."""
        replies, last_err = [], None
        for attempt in range(2):
            prompt = user if attempt == 0 else (
                user + "\n\nYour previous answer could not be read. Answer again in the required format only.")
            reply = self.provider.ask(self.system, prompt)
            replies.append(reply)
            try:
                result = parse_data(reply.data) if reply.data is not None else parse_reply(reply.text)
                return result, replies, None
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:200]}"
                if reply.stop_reason in ("max_tokens", "length"):
                    last_err += " (answer was cut off: too long)"
        return None, replies, last_err

    # ---------- one post ----------
    def screen_one(self, msg) -> dict:
        text = msg["text"] or ""
        links = json.loads(msg["links"] or "[]")
        lk, tk = rules.link_key(links), rules.text_key(text)

        if msg["lang"] not in self.rules.get("screen_post_languages", ["en", "zh"]):
            self.save(msg, status="skipped", reasons=_j([f"post language: {msg['lang']}"]), flags="[]",
                      link_key=lk, text_key=tk)
            self.db.commit()
            return {"status": "skipped", "why": f"language {msg['lang']}", "cost": 0}

        dup = self.find_dup("link_key", lk) or self.find_dup("text_key", tk)
        if dup:
            self.save(msg, status="duplicate", reasons=_j(["same job already screened"]), flags="[]",
                      dup_of=dup, link_key=lk, text_key=tk)
            self.db.commit()
            return {"status": "duplicate", "why": f"same as {dup}", "cost": 0}

        page_url = page_text = None
        link_status = "not needed"
        if self.links_cfg.get("read_linked_pages", True) and len(text) < self.links_cfg.get("only_if_post_shorter_than", 1200):
            page_url = linkread.pick_link(links, self.links_cfg.get("skip_domains", []))
            if page_url and not self.dry_run:
                page_text, link_status = linkread.read_page(page_url, self.links_cfg.get("max_page_chars", 6000))
            elif page_url:
                link_status = "would read"
            else:
                link_status = "no usable link"

        user = user_message(text, msg["chat_title"], page_url, page_text)
        if self.dry_run:
            est_in = (len(self.system) + len(user)) // 3   # rough: ~3-4 characters per token
            return {"status": "dry-run", "est_in": est_in, "link": link_status}

        result, replies, err = self._ask(user)
        cost = sum(self.provider.cost_usd(r) for r in replies)
        tokens_in = sum(r.input_tokens + r.cache_read_tokens + r.cache_write_tokens for r in replies)
        tokens_out = sum(r.output_tokens for r in replies)
        secs = sum(r.seconds for r in replies)
        common = dict(provider=self.provider.name, model=self.provider.model, link_used=page_url,
                      link_status=link_status)

        if result is None:   # no keys saved, so --retry-errors can try again later
            last = replies[-1]
            raw = json.dumps(last.data, ensure_ascii=False) if last.data is not None else last.text
            self.save(msg, status="error", reasons=_j([f"AI reply unreadable: {err}"]), flags="[]",
                      input_tokens=tokens_in, output_tokens=tokens_out, cost_usd=cost, seconds=secs,
                      raw_json=(raw or "")[:4000], **common)
            self.db.commit()
            return {"status": "error", "why": err or "unreadable AI reply", "cost": cost}

        if not result.is_job_post:
            self.save(msg, status="skipped", reasons=_j([f"not a job post: {result.not_job_reason or 'AI'}"]),
                      flags="[]", link_key=lk, text_key=tk, input_tokens=tokens_in, output_tokens=tokens_out,
                      cost_usd=cost, seconds=secs, raw_json=result.model_dump_json(), **common)
            self.db.commit()
            return {"status": "skipped", "why": f"not a job post: {result.not_job_reason or ''}", "cost": cost}

        source = text + " " + (page_text or "")
        summary = []
        for idx, job in enumerate(result.jobs):
            evidence_ok, unverified = rules.check_evidence(job, source)
            status, reasons, flags = rules.apply_rules(job, self.rules, unverified)
            if page_url and link_status != "ok":
                flags.append(f"link not readable ({link_status})")
            if len(result.jobs) > 1:
                flags.append(f"job {idx + 1} of {len(result.jobs)} in this post")
            rk = rules.role_key(job)
            role_dup = self.find_dup("role_key", rk, exclude_msg=(msg["chat_id"], msg["msg_id"])) if status == "kept" else None
            if role_dup:
                status, reasons = "duplicate", ["same company + title already screened"]
            self.save(msg, job_idx=idx, status=status, reasons=_j(reasons), flags=_j(flags),
                      title=job.title, company=job.company, location=job.location, remote=job.remote,
                      open_to_malaysia=job.open_to_malaysia, employment_type=job.employment_type,
                      years_required_min=job.years_required_min, required_languages=_j(job.required_languages),
                      salary=job.salary, deadline=job.deadline, apply_link=job.apply_link or page_url,
                      fit_score=job.fit_score, fit_label=job.fit_label, why_fit=_j(job.why_fit),
                      requirements_met=_j(job.requirements_met), gaps=_j(job.gaps), stretch=_j(job.stretch),
                      evidence_ok=evidence_ok, dup_of=role_dup,
                      link_key=lk if idx == 0 else None, text_key=tk if idx == 0 else None, role_key=rk,
                      # the call's cost is recorded once (on the first job) so totals aren't double-counted
                      input_tokens=tokens_in if idx == 0 else 0, output_tokens=tokens_out if idx == 0 else 0,
                      cost_usd=cost if idx == 0 else 0, seconds=secs if idx == 0 else 0,
                      raw_json=job.model_dump_json(), **common)
            summary.append((status, job.title, "; ".join(reasons) or f"{job.fit_score} {job.fit_label}"))
        self.db.commit()
        return {"status": "jobs", "jobs": summary, "cost": cost}

    # ---------- run ----------
    def run(self, limit: int, max_age_days: float | None = None, quiet: bool = False):
        todo = self.pending(limit, max_age_days)
        if not todo:
            if not quiet:
                print("No unscreened job posts. Run `python -m jobagent.listen --backfill-days 1` to fetch new ones.")
            return
        spent = 0.0 if self.dry_run else self.spent_today()
        if not self.dry_run:
            print(f"Provider: {self.provider.name} ({self.provider.model}) | spent today: ${spent:.4f} "
                  f"of ${self.daily_usd:.2f} cap")
        counts, est_tokens, run_cost = {}, 0, 0.0
        for i, msg in enumerate(todo, start=1):
            if not self.dry_run and spent + run_cost >= self.daily_usd:
                print(f"\nDaily cap of ${self.daily_usd:.2f} reached. The rest waits until tomorrow.")
                break
            try:
                res = self.screen_one(msg)
            except KeyboardInterrupt:
                print("\nStopped by you. Finished posts are saved.")
                break
            except Exception as e:  # API/config problems hit every post, so stop instead of repeating
                print(f"\nStopped on post {i}: {type(e).__name__}: {str(e)[:300]}")
                print("Nothing was saved for this post. Send me this message if it isn't obvious.")
                break
            run_cost += res.get("cost", 0) or 0

            if self.dry_run:
                counts[res["status"]] = counts.get(res["status"], 0) + 1
                est_tokens += res.get("est_in", 0)
                print(f"[{i}/{len(todo)}] {msg['chat_title'][:25]:25} -> {res['status']}"
                      + (f" (link: {res['link']})" if res.get("link") else ""))
                continue
            if res["status"] == "jobs":
                for j, (status, title, why) in enumerate(res["jobs"]):
                    counts[status] = counts.get(status, 0) + 1
                    prefix = f"[{i}/{len(todo)}]" if j == 0 else " " * len(f"[{i}/{len(todo)}]")
                    print(f"{prefix} {status:9} {str(title or '')[:45]:45} | {why[:60]}")
            else:
                counts[res["status"]] = counts.get(res["status"], 0) + 1
                label = (msg["text"] or "")[:45].replace("\n", " ")
                print(f"[{i}/{len(todo)}] {res['status']:9} {label:45} | {res['why'][:60]}")

        print("\nResult (jobs): " + (", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "nothing screened"))
        if self.dry_run:
            n = counts.get("dry-run", 0)
            if n and self.agent.get("provider") in (self.agent.get("providers") or {}):
                p = self.agent["providers"][self.agent["provider"]]["price_per_m"]
                usd = (est_tokens * p.get("input", 0) + n * 600 * p.get("output", 0)) / 1e6
                print(f"Would send {n} posts to the AI: about {est_tokens:,} input tokens before linked pages, "
                      f"roughly ${usd:.4f} (RM{usd * USD_TO_MYR:.3f}) with {self.agent['provider']}.")
            print("Nothing was sent or saved (dry run). Duplicates are only detected on real runs.")
        else:
            print(f"Cost this run: ${run_cost:.4f} (RM{run_cost * USD_TO_MYR:.3f})")
            print("See everything: python -m jobagent.screen --export")


def export(path=None):
    path = path or EXPORT_PATH
    store = Store()
    ensure_schema(store.conn)
    rows = store.conn.execute(
        """SELECT s.*, m.chat_title, m.date_utc, m.text FROM screened s
           JOIN messages m ON m.chat_id = s.chat_id AND m.msg_id = s.msg_id""").fetchall()
    order = {"kept": 0, "skipped": 1, "duplicate": 2, "error": 3}
    rows = sorted(rows, key=lambda r: (order.get(r["status"], 9), -(r["fit_score"] or 0)))

    def lst(x):
        try:
            return "; ".join(json.loads(x or "[]"))
        except Exception:
            return x or ""

    fields = ["status", "fit_score", "fit_label", "title", "company", "location", "remote", "open_to_malaysia",
              "years_required_min", "required_languages", "employment_type", "salary", "why_fit",
              "requirements_met", "gaps", "stretch", "flags", "skip_reasons", "apply_link", "channel",
              "posted_myt", "evidence_ok", "cost_usd", "model", "post_text", "ai_raw_if_error", "your_fit"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({
                "status": r["status"], "fit_score": r["fit_score"], "fit_label": r["fit_label"],
                "title": r["title"], "company": r["company"], "location": r["location"], "remote": r["remote"],
                "open_to_malaysia": r["open_to_malaysia"], "years_required_min": r["years_required_min"],
                "required_languages": lst(r["required_languages"]), "employment_type": r["employment_type"],
                "salary": r["salary"], "why_fit": lst(r["why_fit"]), "requirements_met": lst(r["requirements_met"]),
                "gaps": lst(r["gaps"]), "stretch": lst(r["stretch"]), "flags": lst(r["flags"]),
                "skip_reasons": lst(r["reasons"]), "apply_link": r["apply_link"], "channel": r["chat_title"],
                "posted_myt": datetime.fromisoformat(r["date_utc"]).astimezone(MYT).strftime("%Y-%m-%d %H:%M"),
                "evidence_ok": "" if r["evidence_ok"] is None else f"{r['evidence_ok']:.0%}",
                "cost_usd": f"{r['cost_usd'] or 0:.5f}", "model": r["model"], "post_text": r["text"],
                "ai_raw_if_error": (r["raw_json"] or "")[:1500] if r["status"] == "error" else "",
                "your_fit": "",
            })
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    total = sum((r["cost_usd"] or 0) for r in rows)
    print(f"Exported {len(rows)} jobs to {path}: " + ", ".join(f"{k} {v}" for k, v in by.items()))
    print(f"Total AI cost of these results: ${total:.4f} (RM{total * USD_TO_MYR:.3f})")
    return len(rows), by


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20, help="max posts to screen this run")
    ap.add_argument("--dry-run", action="store_true", help="no AI calls; show plan and cost estimate")
    ap.add_argument("--provider", help="override config/agent.yaml provider (anthropic, zai, groq, groq-small, mock)")
    ap.add_argument("--export", action="store_true", help="write data/screened.csv and stop")
    ap.add_argument("--retry-errors", action="store_true", help="screen again the posts that failed before")
    ap.add_argument("--reset", action="store_true", help="forget all screening results (keeps messages)")
    args = ap.parse_args()
    if args.export:
        export()
        return
    if args.reset or args.retry_errors:
        st = Store()
        ensure_schema(st.conn)
        where = "" if args.reset else " WHERE status = 'error'"
        n = st.conn.execute(f"DELETE FROM screened{where}").rowcount
        st.conn.commit()
        print(f"Removed {n} {'screening results' if args.reset else 'failed results'}; they'll be screened again.")
        if args.reset:
            return
    Screener(args.provider, args.dry_run).run(args.limit)


if __name__ == "__main__":
    main()
