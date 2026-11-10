"""
Milestone 3: which AI model agrees with YOU best, for the least money?

Every job you rated (👍 / 🤔 / 👎) is screened again by each model, with exactly the same post
and the same linked page, and the model's fit score is compared with your verdict.

    python -m jobagent.compare                       # all models in agent.yaml (not mock)
    python -m jobagent.compare --providers anthropic,groq-small
    python -m jobagent.compare --report              # just show results from earlier runs
    python -m jobagent.compare --send                # also send the summary to your Telegram bot

It is resumable: answers are saved in data/agent.db (table compare_runs), so if it stops or a
free model is busy, run it again and it continues where it left off. Nothing in your normal
results, sheet or alerts is changed.

How the score is read (same thresholds as your alerts):
    60+   -> "would alert"   ~ 👍       40-59 -> ~ 🤔       below 40 / not a job -> ~ 👎
Location and remote rules are ignored here, because your /rate cards told you to ignore them too.
"""
import argparse
import csv
import difflib
import json
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone

from . import linkread
from .config import load_agent, load_profile, profile_text
from .llm import make_provider
from .prompt import system_prompt, user_message
from .schema import parse_data, parse_reply
from .settings import DATA_DIR, DB_PATH
from .store import Store

POSTS_PER_DAY = 430         # your measured volume; used to project a monthly bill
USD_TO_MYR = 4.09
VALUE = {"good": 1.0, "maybe": 0.5, "no": 0.0}
# Seconds between calls per model, to stay under free-tier limits
# (Groq free: 8,000 tokens/minute and one post is ~3,600 tokens -> about 2 calls a minute).
PACE = {"groq": 30, "groq-small": 30, "zai": 5}
BUSY_WAITS = [60, 120, 240]  # "retry slowly" when a free model says it's busy

SCHEMA = """
CREATE TABLE IF NOT EXISTS compare_runs (
    provider TEXT, model TEXT, chat_id INTEGER, msg_id INTEGER, job_idx INTEGER,
    fit_score INTEGER, matched_title TEXT, ok INTEGER, error TEXT,
    cost_usd REAL, seconds REAL, input_tokens INTEGER, output_tokens INTEGER, run_at TEXT,
    PRIMARY KEY (provider, model, chat_id, msg_id, job_idx)
);
"""


def band(score) -> str:
    if score is None:
        return "no"
    return "good" if score >= 60 else "maybe" if score >= 40 else "no"


def _ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1   # ties share the average rank
        i = j + 1
    return ranks


def spearman(a, b) -> float | None:
    """Rank correlation: 1 = model orders jobs exactly like you, 0 = no relation."""
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra) ** 0.5
    vb = sum((y - mb) ** 2 for y in rb) ** 0.5
    return cov / (va * vb) if va and vb else None


class Compare:
    def __init__(self):
        Store()   # makes sure the messages table exists
        # one connection shared by the model threads (every write is behind self.lock)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        from .feedback import SCHEMA as FEEDBACK_SCHEMA
        self.db.executescript(FEEDBACK_SCHEMA)
        self.agent = load_agent()
        self.links_cfg = self.agent.get("links") or {}
        self.lock = threading.Lock()
        self.pages: dict[tuple, tuple] = {}

    # ---------- data ----------
    def rated(self):
        return self.db.execute(
            """SELECT f.chat_id, f.msg_id, f.job_idx, f.verdict, f.source,
                      s.title, s.fit_score AS stored_score, s.model AS stored_model, s.link_used,
                      m.text, m.chat_title, m.links
               FROM feedback f
               JOIN screened s ON s.chat_id=f.chat_id AND s.msg_id=f.msg_id AND s.job_idx=f.job_idx
               JOIN messages m ON m.chat_id=f.chat_id AND m.msg_id=f.msg_id
               ORDER BY f.rated_at""").fetchall()

    def page_for(self, r):
        """Read each linked page once and give every model the same copy (fair test)."""
        key = (r["chat_id"], r["msg_id"])
        with self.lock:
            if key in self.pages:
                return self.pages[key]
        url = r["link_used"]
        text = None
        if url and len(r["text"] or "") < self.links_cfg.get("only_if_post_shorter_than", 1200):
            text, _ = linkread.read_page(url, self.links_cfg.get("max_page_chars", 6000))
        with self.lock:
            self.pages[key] = (url if text else None, text)
        return self.pages[key]

    def done(self, provider, model):
        with self.lock:
            return {(r[0], r[1], r[2]) for r in self.db.execute(
            "SELECT chat_id, msg_id, job_idx FROM compare_runs WHERE provider=? AND model=? AND ok=1",
            (provider, model))}

    # ---------- one model ----------
    @staticmethod
    def suffix() -> str:
        """Results are stored per version of your learned rules, so old and new prompts never mix."""
        from . import learned
        return "@" + learned.fingerprint() if learned.active() else ""

    def run_provider(self, name: str, jobs, log, label: str | None = None, extra_rules=None):
        """Screen every rated job with one model. `label` + `extra_rules` are used by the learning
        review to test a candidate rule without touching your real results."""
        p = make_provider(self.agent, name)
        system = system_prompt(profile_text(load_profile(), extra_rules), for_text_reply=not p.structured)
        label = label or name + self.suffix()
        done = self.done(label, p.model)
        todo = [r for r in jobs if (r["chat_id"], r["msg_id"], r["job_idx"]) not in done]
        log(f"{label} ({p.model}): {len(done)} already done, {len(todo)} to go")
        # several rated jobs can come from one post: ask once per post
        posts: dict[tuple, list] = {}
        for r in todo:
            posts.setdefault((r["chat_id"], r["msg_id"]), []).append(r)
        for n, (key, rows) in enumerate(posts.items(), 1):
            r0 = rows[0]
            url, page = self.page_for(r0)
            user = user_message(r0["text"] or "", r0["chat_title"], url, page)
            result, replies, err = self._ask(p, system, user)
            cost = sum(p.cost_usd(x) for x in replies)
            secs = sum(x.seconds for x in replies)
            tin = sum(x.input_tokens + x.cache_read_tokens + x.cache_write_tokens for x in replies)
            tout = sum(x.output_tokens for x in replies)
            for i, r in enumerate(rows):
                score, title, ok = None, None, 0
                if result is not None:
                    ok = 1
                    job = self._match(result, r)
                    if job is not None:            # None = model says "not a job" -> treated as score 0
                        score, title = job.fit_score, job.title
                    else:
                        score = 0
                with self.lock:
                    self.db.execute("INSERT OR REPLACE INTO compare_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (label, p.model, r["chat_id"], r["msg_id"], r["job_idx"], score, title, ok,
                                     err, cost if i == 0 else 0, secs if i == 0 else 0,
                                     tin if i == 0 else 0, tout if i == 0 else 0,
                                     datetime.now(timezone.utc).isoformat()))
                    self.db.commit()
            log(f"  {label}: post {n}/{len(posts)} " + ("ok" if result is not None else f"FAILED ({err})"))
            if n < len(posts):
                time.sleep(PACE.get(name, 0))

    def _ask(self, p, system, user):
        replies, last = [], None
        for attempt in range(2):
            prompt = user if attempt == 0 else user + "\n\nYour previous answer could not be read. Answer again in the required format only."
            for wait in BUSY_WAITS + [None]:
                try:
                    reply = p.ask(system, prompt)
                    break
                except Exception as e:
                    msg = str(e).lower()
                    busy = any(x in msg for x in ("429", "rate limit", "overload", "too many", "busy"))
                    if not busy or wait is None:
                        return None, replies, f"{type(e).__name__}: {str(e)[:150]}"
                    time.sleep(wait)
            replies.append(reply)
            try:
                return (parse_data(reply.data) if reply.data is not None else parse_reply(reply.text)), replies, None
            except Exception as e:
                last = f"unreadable reply: {str(e)[:120]}"
        return None, replies, last

    @staticmethod
    def _match(result, r):
        """Find the rated job in the model's answer (posts can list several jobs)."""
        if not result.is_job_post or not result.jobs:
            return None
        want = (r["title"] or "").lower()
        if want:
            best = max(result.jobs, key=lambda j: difflib.SequenceMatcher(None, want, (j.title or "").lower()).ratio())
            if difflib.SequenceMatcher(None, want, (best.title or "").lower()).ratio() >= 0.5:
                return best
        idx = r["job_idx"]
        return result.jobs[idx] if idx < len(result.jobs) else result.jobs[0]

    # ---------- results ----------
    def table(self, label: str | None = None):
        """Your ratings, plus each model's saved scores. Default: model runs made with your
        current learned rules. label=...: one exact run (used by the learning review)."""
        jobs = self.rated()
        sfx = self.suffix()
        res = {}
        for row in self.db.execute("SELECT * FROM compare_runs WHERE ok=1"):
            pv = row["provider"]
            if label is not None:
                keep = pv == label
            elif pv.startswith("learn"):
                keep = False
            else:
                keep = pv.endswith(sfx) if sfx else "@" not in pv
            if not keep:
                continue
            base = pv[:-len(sfx)] if sfx and pv.endswith(sfx) else pv
            res.setdefault((base, row["model"]), {})[(row["chat_id"], row["msg_id"], row["job_idx"])] = row
        return jobs, res

    def metrics(self, jobs, rows):
        pairs = [(r, rows[(r["chat_id"], r["msg_id"], r["job_idx"])]) for r in jobs
                 if (r["chat_id"], r["msg_id"], r["job_idx"]) in rows]
        if not pairs:
            return None
        you = [VALUE[r["verdict"]] for r, _ in pairs]
        sc = [x["fit_score"] or 0 for _, x in pairs]
        agree = sum(band(s) == r["verdict"] for (r, _), s in zip(pairs, sc))
        big = sum({band(s), r["verdict"]} == {"good", "no"} for (r, _), s in zip(pairs, sc))
        goods = [s for (r, _), s in zip(pairs, sc) if r["verdict"] == "good"]
        alerts = [r["verdict"] for (r, _), s in zip(pairs, sc) if s >= 60]
        cost = sum(x["cost_usd"] or 0 for _, x in pairs)
        posts = len({(r["chat_id"], r["msg_id"]) for r, _ in pairs})
        secs = sum(x["seconds"] or 0 for _, x in pairs)
        return {
            "n": len(pairs), "agree": agree / len(pairs), "big": big,
            "missed": sum(s < 60 for s in goods), "hidden": sum(s < 30 for s in goods), "goods": len(goods),
            "precision": (alerts.count("good") / len(alerts)) if alerts else None, "alerts": len(alerts),
            "rho": spearman(sc, you), "cost_post": cost / posts if posts else 0, "secs": secs / posts if posts else 0,
            "_pairs": {(r["chat_id"], r["msg_id"], r["job_idx"]): (VALUE[r["verdict"]], s) for (r, _), s in zip(pairs, sc)},
        }

    @staticmethod
    def rho_gap(a, b, rounds=2000):
        """How sure are we that model B ranks jobs differently from model A? (paired bootstrap)"""
        keys = sorted(set(a["_pairs"]) & set(b["_pairs"]))
        if len(keys) < 10:
            return None
        rng = random.Random(7)
        diffs = []
        for _ in range(rounds):
            s = [rng.choice(keys) for _ in keys]
            ra = spearman([a["_pairs"][k][1] for k in s], [a["_pairs"][k][0] for k in s])
            rb = spearman([b["_pairs"][k][1] for k in s], [b["_pairs"][k][0] for k in s])
            if ra is not None and rb is not None:
                diffs.append(rb - ra)
        diffs.sort()
        return diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs)) - 1]

    def report(self) -> str:
        jobs, res = self.table()
        if not jobs:
            return "No ratings found. Rate some jobs with /rate first."
        v = [r["verdict"] for r in jobs]
        blind = sum(r["source"] == "rate" for r in jobs)
        lines = [f"Your ratings: {len(jobs)} jobs ({v.count('good')} 👍, {v.count('maybe')} 🤔, {v.count('no')} 👎; "
                 f"{blind} blind from /rate)", ""]
        m = {k: self.metrics(jobs, rows) for k, rows in res.items()}
        m = {k: x for k, x in m.items() if x}
        if not m:
            return "\n".join(lines + ["No model results yet. Run: python -m jobagent.compare"])
        claude = next((x for (pv, _), x in m.items() if pv == "anthropic"), None)
        hdr = f"{'Model':28} {'n':>3} {'Agree':>6} {'BigMiss':>7} {'Missed👍':>8} {'Alert✓':>7} {'Rank ρ':>7} {'$/post':>8} {'RM/month':>8} {'sec':>5}"
        lines += [hdr, "-" * len(hdr)]
        order = sorted(m, key=lambda k: -(m[k]["rho"] or -1))
        for k in order:
            x = m[k]
            month = x["cost_post"] * POSTS_PER_DAY * 30 * USD_TO_MYR
            prec = f"{x['precision']:.0%}" if x["precision"] is not None else "-"
            rho = f"{x['rho']:.2f}" if x["rho"] is not None else "-"
            lines.append(f"{(k[0] + ' ' + k[1])[:28]:28} {x['n']:>3} {x['agree']:>6.0%} {x['big']:>7} "
                         f"{str(x['missed']) + '/' + str(x['goods']):>8} {prec:>7} {rho:>7} "
                         f"{x['cost_post']:>8.5f} {month:>8.1f} {x['secs']:>5.1f}")
        lines += ["", "Agree = same band as you (60+ 👍, 40-59 🤔, <40 👎).  BigMiss = 👍 vs 👎 opposite.",
                  "Missed👍 = jobs you liked that it scored below 60 (no alert).  Alert✓ = share of its 60+ jobs you rated 👍.",
                  "Rank ρ = how well its scores order jobs the way you do (1 = perfect, 0 = random)."]
        if claude:
            lines += ["", "Compared with Claude (95% range of the difference in Rank ρ; a range crossing 0 = could be noise):"]
            for k in order:
                if k[0] == "anthropic":
                    continue
                gap = self.rho_gap(claude, m[k])
                if gap:
                    lines.append(f"  {k[0]:12} {gap[0]:+.2f} to {gap[1]:+.2f}")
            lines += ["", self.verdict(m, claude)]
        lines += ["", "Missed and incomplete runs: re-run `python -m jobagent.compare` to fill gaps."]
        return "\n".join(lines)

    @staticmethod
    def verdict(m, claude) -> str:
        """Your rule: the cheapest model that does at least as well as Claude."""
        ok = []
        for k, x in m.items():
            if x["n"] < claude["n"] * 0.9:
                continue   # incomplete run: not judged
            good_enough = ((x["rho"] or 0) >= (claude["rho"] or 0) - 0.05
                           and x["missed"] <= claude["missed"] and x["agree"] >= claude["agree"] - 0.05)
            if good_enough:
                ok.append((x["cost_post"], k))
        if not ok:
            return "Suggested pick: none qualified yet."
        cost, k = min(ok)
        tail = " (Claude stays)" if k[0] == "anthropic" else " (cheaper and at least as good as Claude on your ratings)"
        return (f"Suggested pick by your rule: {k[0]} / {k[1]}{tail}.\n"
                f"Caution: with ~{claude['n']} ratings, gaps under ~10 points can be luck. Treat this as a first read.")

    def export_csv(self):
        jobs, res = self.table()
        keys = sorted(res)
        path = DATA_DIR / "compare_results.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["title", "your verdict", "rated via"] + [f"{p} score" for p, _ in keys])
            for r in jobs:
                k = (r["chat_id"], r["msg_id"], r["job_idx"])
                w.writerow([r["title"], r["verdict"], r["source"]] +
                           [(res[x][k]["fit_score"] if k in res[x] else "") for x in keys])
        return path


def run_models(names, log=print) -> str:
    """Screen all rated jobs with each model (in parallel, each paced) and return the report text."""
    c = Compare()
    jobs = c.rated()
    if not jobs:
        raise SystemExit("No ratings found. Rate jobs with /rate first.")
    lock = threading.Lock()

    def say(s):
        with lock:
            log(s)

    def worker(name):
        try:
            c.run_provider(name, jobs, say)
        except SystemExit as e:          # e.g. missing key in .env
            say(f"{name}: skipped - {e}")
        except Exception as e:
            say(f"{name}: stopped - {type(e).__name__}: {str(e)[:200]}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    c.export_csv()
    return c.report()


def main():
    ap = argparse.ArgumentParser(description="Milestone 3: compare AI models against your own ratings.")
    ap.add_argument("--providers", help="comma list, default: every provider in agent.yaml except mock")
    ap.add_argument("--report", action="store_true", help="only print results from earlier runs")
    ap.add_argument("--send", action="store_true", help="also send the summary to your Telegram bot")
    a = ap.parse_args()
    c = Compare()
    if a.report:
        text = c.report()
    else:
        names = [x.strip() for x in a.providers.split(",")] if a.providers else \
            [n for n in (c.agent.get("providers") or {}) if n != "mock"]
        print(f"{len(c.rated())} rated jobs. Models: {', '.join(names)}. Free models are paced, so this can take ~20-30 min.")
        text = run_models(names, lambda s: print(s, flush=True))
    print("\n" + text)
    print(f"\nPer-job scores: {c.export_csv()}")
    if a.send:
        from .notify import e, send
        send("<b>🧪 Model test results</b>\n<pre>" + e(text) + "</pre>")


if __name__ == "__main__":
    main()
