"""
Milestone 2: screen stored job posts with AI, apply your rules, and export the results.

  python -m jobagent.screen --dry-run --limit 20   # no AI: what would happen + cost estimate
  python -m jobagent.screen --limit 20             # screen the 20 newest unscreened job posts
  python -m jobagent.screen --provider mock --limit 20   # free fake AI, to test the plumbing
  python -m jobagent.screen --export               # write data/screened.csv

Order for each post (cheapest first):
  1. language filter (free)          only English/Chinese posts go on
  2. duplicate check (free)          same job link or same text already screened -> skipped
  3. read the job link (free)        only for short posts
  4. AI: extract facts + score       one call per post
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
from .schema import parse_reply
from .settings import DATA_DIR, MYT
from .store import Store

EXPORT_PATH = DATA_DIR / "screened.csv"
USD_TO_MYR = 4.09  # approximate, Nov 2026

SCHEMA = """
CREATE TABLE IF NOT EXISTS screened (
    chat_id INTEGER, msg_id INTEGER, screened_at TEXT, status TEXT,
    reasons TEXT, flags TEXT,
    title TEXT, company TEXT, location TEXT, remote TEXT, open_to_malaysia TEXT,
    employment_type TEXT, years_required_min REAL, required_languages TEXT,
    salary TEXT, deadline TEXT, apply_link TEXT,
    fit_score INTEGER, fit_label TEXT, why_fit TEXT, requirements_met TEXT, gaps TEXT, stretch TEXT,
    evidence_ok REAL, link_used TEXT, link_status TEXT,
    dup_of TEXT, link_key TEXT, text_key TEXT, role_key TEXT,
    provider TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cost_usd REAL, seconds REAL, raw_json TEXT,
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_screened_keys ON screened (link_key, text_key, role_key);
"""


def _j(x):
    return json.dumps(x, ensure_ascii=False)


class Screener:
    def __init__(self, provider_override=None, dry_run=False):
        self.profile = load_profile()
        self.agent = load_agent()
        self.rules = self.profile["rules"]
        self.store = Store()
        self.db: sqlite3.Connection = self.store.conn
        self.db.executescript(SCHEMA)
        self.system = system_prompt(profile_text(self.profile))
        self.dry_run = dry_run
        self.provider = None if dry_run else make_provider(self.agent, provider_override)
        budget = self.agent.get("budget") or {}
        self.daily_usd = float(budget.get("daily_usd", 0.6))
        self.links_cfg = self.agent.get("links") or {}

    # ---------- helpers ----------
    def spent_today(self) -> float:
        today = datetime.now(MYT).strftime("%Y-%m-%d")
        rows = self.db.execute("SELECT screened_at, cost_usd FROM screened WHERE cost_usd > 0").fetchall()
        return sum(c for t, c in rows if datetime.fromisoformat(t).astimezone(MYT).strftime("%Y-%m-%d") == today)

    def find_dup(self, column: str, key):
        if not key:
            return None
        row = self.db.execute(f"SELECT chat_id, msg_id FROM screened WHERE {column} = ? LIMIT 1", (key,)).fetchone()
        return f"{row[0]}/{row[1]}" if row else None

    def save(self, msg, **f):
        f.setdefault("screened_at", datetime.now(timezone.utc).isoformat())
        f["chat_id"], f["msg_id"] = msg["chat_id"], msg["msg_id"]
        cols = ", ".join(f)
        self.db.execute(f"INSERT OR REPLACE INTO screened ({cols}) VALUES ({', '.join('?' * len(f))})",
                        list(f.values()))
        self.db.commit()

    def pending(self, limit: int):
        return self.db.execute(
            """SELECT m.* FROM messages m
               LEFT JOIN screened s ON s.chat_id = m.chat_id AND s.msg_id = m.msg_id
               WHERE m.is_candidate = 1 AND s.chat_id IS NULL
               ORDER BY m.date_utc DESC LIMIT ?""", (limit,)).fetchall()

    # ---------- one post ----------
    def screen_one(self, msg) -> dict:
        text = msg["text"] or ""
        links = json.loads(msg["links"] or "[]")
        lk, tk = rules.link_key(links), rules.text_key(text)

        if msg["lang"] not in self.rules.get("screen_post_languages", ["en", "zh"]):
            self.save(msg, status="skipped", reasons=_j([f"post language: {msg['lang']}"]), flags="[]",
                      link_key=lk, text_key=tk)
            return {"status": "skipped", "why": f"language {msg['lang']}"}

        dup = self.find_dup("link_key", lk) or self.find_dup("text_key", tk)
        if dup:
            self.save(msg, status="duplicate", reasons=_j(["same job already screened"]), flags="[]",
                      dup_of=dup, link_key=lk, text_key=tk)
            return {"status": "duplicate", "why": f"same as {dup}"}

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

        reply = self.provider.ask(self.system, user)
        cost = self.provider.cost_usd(reply)
        try:
            s = parse_reply(reply.text)
        except Exception:
            reply2 = self.provider.ask(self.system, user + "\n\nYour last reply was not valid JSON. Return ONLY the JSON object.")
            cost += self.provider.cost_usd(reply2)
            try:
                s = parse_reply(reply2.text)
                reply = reply2
            except Exception as e:
                self.save(msg, status="error", reasons=_j([f"AI reply unreadable: {type(e).__name__}"]), flags="[]",
                          link_key=lk, text_key=tk, provider=self.provider.name, model=self.provider.model,
                          cost_usd=cost, raw_json=reply2.text[:4000])
                return {"status": "error", "why": "unreadable AI reply"}

        evidence_ok, unverified = rules.check_evidence(s, text + " " + (page_text or ""))
        status, reasons, flags = rules.apply_rules(s, self.rules, unverified)
        if page_url and link_status != "ok":
            flags.append(f"link not readable ({link_status})")
        rk = rules.role_key(s)
        role_dup = self.find_dup("role_key", rk) if status == "kept" else None
        if role_dup:
            status, reasons = "duplicate", ["same company + title already screened"]

        self.save(msg, status=status, reasons=_j(reasons), flags=_j(flags),
                  title=s.title, company=s.company, location=s.location, remote=s.remote,
                  open_to_malaysia=s.open_to_malaysia, employment_type=s.employment_type,
                  years_required_min=s.years_required_min, required_languages=_j(s.required_languages),
                  salary=s.salary, deadline=s.deadline, apply_link=s.apply_link or page_url,
                  fit_score=s.fit_score, fit_label=s.fit_label, why_fit=_j(s.why_fit),
                  requirements_met=_j(s.requirements_met), gaps=_j(s.gaps), stretch=_j(s.stretch),
                  evidence_ok=evidence_ok, link_used=page_url, link_status=link_status, dup_of=role_dup,
                  link_key=lk, text_key=tk, role_key=rk, provider=self.provider.name, model=self.provider.model,
                  input_tokens=reply.input_tokens + reply.cache_read_tokens + reply.cache_write_tokens,
                  output_tokens=reply.output_tokens, cost_usd=cost, seconds=reply.seconds,
                  raw_json=s.model_dump_json())
        return {"status": status, "why": "; ".join(reasons) or f"{s.fit_score} {s.fit_label}",
                "title": s.title, "cost": cost}

    # ---------- run ----------
    def run(self, limit: int):
        todo = self.pending(limit)
        if not todo:
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
            res = self.screen_one(msg)
            counts[res["status"]] = counts.get(res["status"], 0) + 1
            if self.dry_run:
                est_tokens += res.get("est_in", 0)
                print(f"[{i}/{len(todo)}] {msg['chat_title'][:25]:25} -> {res['status']}"
                      + (f" (link: {res['link']})" if res.get("link") else ""))
                continue
            run_cost += res.get("cost", 0) or 0
            label = res.get("title") or (msg["text"] or "")[:50].replace("\n", " ")
            print(f"[{i}/{len(todo)}] {res['status']:9} {str(label)[:45]:45} | {res['why'][:60]}")

        print("\nResult: " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
        if self.dry_run:
            n = counts.get("dry-run", 0)
            if n and self.agent.get("provider") in (self.agent.get("providers") or {}):
                p = self.agent["providers"][self.agent["provider"]]["price_per_m"]
                usd = (est_tokens * p.get("input", 0) + n * 500 * p.get("output", 0)) / 1e6
                print(f"Would send {n} posts to the AI: about {est_tokens:,} input tokens, "
                      f"roughly ${usd:.4f} (RM{usd * USD_TO_MYR:.3f}) with {self.agent['provider']}.")
            print("Nothing was sent or saved (dry run). Duplicates are only detected on real runs.")
        else:
            print(f"Cost this run: ${run_cost:.4f} (RM{run_cost * USD_TO_MYR:.3f})")
            print("See everything: python -m jobagent.screen --export")


def export():
    store = Store()
    store.conn.executescript(SCHEMA)
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
              "posted_myt", "evidence_ok", "cost_usd", "model", "post_text", "your_fit"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(EXPORT_PATH, "w", newline="", encoding="utf-8-sig") as f:
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
                "your_fit": "",
            })
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    total = sum((r["cost_usd"] or 0) for r in rows)
    print(f"Exported {len(rows)} screened posts to {EXPORT_PATH}: "
          + ", ".join(f"{k} {v}" for k, v in by.items()))
    print(f"Total AI cost so far: ${total:.4f} (RM{total * USD_TO_MYR:.3f})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20, help="max posts to screen this run")
    ap.add_argument("--dry-run", action="store_true", help="no AI calls; show plan and cost estimate")
    ap.add_argument("--provider", help="override config/agent.yaml provider (anthropic, zai, qwen, mock)")
    ap.add_argument("--export", action="store_true", help="write data/screened.csv and stop")
    ap.add_argument("--reset", action="store_true", help="forget all screening results (keeps messages)")
    args = ap.parse_args()
    if args.export:
        export()
        return
    if args.reset:
        st = Store()
        st.conn.executescript(SCHEMA)
        n = st.conn.execute("DELETE FROM screened").rowcount
        st.conn.commit()
        print(f"Removed {n} screening results. Messages are kept; run screen again to redo them.")
        return
    Screener(args.provider, args.dry_run).run(args.limit)


if __name__ == "__main__":
    main()
