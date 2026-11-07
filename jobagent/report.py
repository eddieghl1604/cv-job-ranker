"""
Step 3: measure volume and check the job-post detector.

  python -m jobagent.report                          # volume table + makes data/review_m1.csv
  python -m jobagent.report --score data/review_m1.csv   # after you fill in the review sheet
  python -m jobagent.report --score data/review_m1.csv --redetect
        # re-check your SAME judgements against the current (updated) detector
  python -m jobagent.report --refresh                # re-flag everything stored with the current detector

The review sheet holds every message flagged as a job post, plus a random sample
of messages that were NOT flagged. Mark each one in the 'your_verdict' column:
  job (or yes)     = a job post: someone could apply to something from it
  not_job (or no)  = anything else
Then --score reports how often the detector was right and, most importantly,
how many real jobs it missed (milestone 1's gate).
"""
import argparse
import csv
import json
import random
from collections import defaultdict
from datetime import datetime

from . import detect
from .settings import DATA_DIR, MYT
from .store import Store

REVIEW_PATH = DATA_DIR / "review_m1.csv"
FIELDS = ["chat_id", "msg_id", "group", "date_myt", "lang", "flagged", "job_score",
          "keywords", "links", "media_only", "text", "your_verdict"]


def volume(rows):
    per = defaultdict(lambda: {"msgs": 0, "cand": 0, "media_only": 0, "days": set(), "langs": defaultdict(int)})
    for r in rows:
        p = per[r["chat_title"]]
        day = datetime.fromisoformat(r["date_utc"]).astimezone(MYT).date()
        p["msgs"] += 1
        p["cand"] += r["is_candidate"]
        p["media_only"] += r["media_only"]
        p["days"].add(day)
        if r["is_candidate"]:
            p["langs"][r["lang"]] += 1

    all_days = {d for p in per.values() for d in p["days"]}
    span = (max(all_days) - min(all_days)).days + 1 if all_days else 1
    print(f"Messages stored: {len(rows)} over {span} day(s)\n")
    print(f"{'Group':40} {'msgs':>6} {'job?':>6} {'job?/day':>9} {'img-only':>9}  job? languages")
    total_cand = 0
    for title, p in sorted(per.items(), key=lambda kv: -kv[1]["cand"]):
        total_cand += p["cand"]
        langs = ", ".join(f"{k}:{v}" for k, v in sorted(p["langs"].items())) or "-"
        print(f"{title[:40]:40} {p['msgs']:>6} {p['cand']:>6} {p['cand'] / span:>9.1f} {p['media_only']:>9}  {langs}")
    print(f"\nEstimated job posts per day (before removing duplicates): {total_cand / span:.1f}")
    print("'img-only' = posts with an image/file and no text: the detector can't read these yet.")


def _already_reviewed() -> set:
    """(chat_id, msg_id) of every message in earlier review sheets, so it isn't asked twice."""
    keys = set()
    for path in DATA_DIR.glob("review_m1*.csv"):
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                keys.add((str(r.get("chat_id")), str(r.get("msg_id"))))
    return keys


def _next_review_path():
    """review_m1.csv, then review_m1_2.csv, review_m1_3.csv ... never overwrites your judgements."""
    if not REVIEW_PATH.exists():
        return REVIEW_PATH
    n = 2
    while (DATA_DIR / f"review_m1_{n}.csv").exists():
        n += 1
    return DATA_DIR / f"review_m1_{n}.csv"


def make_review(rows, n_unflagged: int, seed: int):
    seen = _already_reviewed()
    rows = [r for r in rows if (str(r["chat_id"]), str(r["msg_id"])) not in seen]
    if seen:
        print(f"\nSkipping {len(seen)} messages you already judged in earlier review sheets.")
    flagged = [r for r in rows if r["is_candidate"]]
    unflagged = [r for r in rows if not r["is_candidate"] and (r["text"] or "").strip()]
    random.Random(seed).shuffle(unflagged)
    sample = flagged + unflagged[:n_unflagged]
    random.Random(seed + 1).shuffle(sample)  # mixed order, so you judge each on its own
    if not sample:
        print("No new messages to review.")
        return

    path = _next_review_path()
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in sample:
            w.writerow({
                "chat_id": r["chat_id"], "msg_id": r["msg_id"], "group": r["chat_title"],
                "date_myt": datetime.fromisoformat(r["date_utc"]).astimezone(MYT).strftime("%Y-%m-%d %H:%M"),
                "lang": r["lang"], "flagged": "yes" if r["is_candidate"] else "no",
                "job_score": r["job_score"], "keywords": ", ".join(json.loads(r["keywords"])),
                "links": " ".join(json.loads(r["links"])), "media_only": r["media_only"],
                "text": r["text"], "your_verdict": "",
            })
    print(f"\nReview sheet: {path} ({len(flagged)} flagged + {min(n_unflagged, len(unflagged))} unflagged)")
    print("Tip: hide the 'flagged' column while you judge, so it doesn't sway you.")


JOB_WORDS = {"job", "yes", "y", "1", "true"}
NOT_JOB_WORDS = {"not_job", "not job", "notjob", "no", "n", "0", "false"}


def _verdict(raw) -> str:
    """'job', 'not_job', or '' (blank / not understood). Accepts job/not_job or yes/no."""
    v = (raw or "").strip().lower()
    if v in JOB_WORDS:
        return "job"
    if v in NOT_JOB_WORDS:
        return "not_job"
    return ""


def score(path, redetect: bool = False):
    with open(path, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f)]
    if rows and "your_verdict" not in rows[0]:
        raise SystemExit("No 'your_verdict' column found. Was the file saved as CSV UTF-8?")
    for r in rows:
        r["_v"] = _verdict(r.get("your_verdict"))
        r["_old_flag"] = r["flagged"]
        if redetect:
            links = (r.get("links") or "").split()
            sc, kw = detect.job_score(r.get("text") or "", links)
            r["flagged"] = "yes" if detect.is_job_candidate(r.get("text") or "", sc) else "no"
            r["job_score"], r["keywords"] = sc, ", ".join(kw)
    done = [r for r in rows if r["_v"]]
    unclear = [r for r in rows if (r.get("your_verdict") or "").strip() and not r["_v"]]
    blank = len(rows) - len(done) - len(unclear)

    print(f"Judged: {len(done)} | blank: {blank} | not understood: {len(unclear)}")
    if unclear:
        examples = sorted({r["your_verdict"].strip() for r in unclear})[:5]
        print(f"  Values I couldn't read: {examples}. Use job/not_job (or yes/no).")
    if not done:
        raise SystemExit("\nNothing judged yet, so there is no result. Fill in 'your_verdict', "
                         "save as CSV UTF-8, and run this again.")

    if redetect:
        _compare(done)
        return

    flagged = [r for r in done if r["flagged"] == "yes"]
    unflagged = [r for r in done if r["flagged"] == "no"]
    tp = sum(r["_v"] == "job" for r in flagged)
    fn = [r for r in unflagged if r["_v"] == "job"]

    if flagged:
        print(f"Of the flagged posts, real jobs: {tp}/{len(flagged)} = {tp / len(flagged):.0%}"
              f"  (the rest cost a little AI money later but are harmless)")
    print(f"Unflagged messages judged: {len(unflagged)}")
    print(f"Real jobs the detector MISSED: {len(fn)}")
    for r in fn:
        print(f"  - {r['group']} | score {r['job_score']} | {' '.join(r['text'].split())[:100]}")

    if blank or unclear:
        print("\nNot finished: judge every row before trusting the gate result.")
    elif not unflagged:
        print("\nNo unflagged messages were judged, so misses can't be tested yet.")
    elif fn:
        print("\nSend me the missed posts listed above and we'll adjust the detector.")
    else:
        print("\nNo misses: the detector passes the M1 gate on this data.")


def _compare(done):
    """Old vs current detector on the same human judgements."""
    def stats(flag_key):
        jobs = [r for r in done if r["_v"] == "job"]
        non = [r for r in done if r["_v"] == "not_job"]
        caught = sum(r[flag_key] == "yes" for r in jobs)
        false_alarm = sum(r[flag_key] == "yes" for r in non)
        return len(jobs), caught, len(non), false_alarm

    print("\nSame judgements, two detector versions:")
    print(f"{'':22} {'jobs caught':>14} {'non-jobs flagged':>18}")
    for label, key in (("Old (in the sheet)", "_old_flag"), ("Current detector", "flagged")):
        nj, caught, nn, fa = stats(key)
        print(f"{label:22} {caught:>5}/{nj:<3} {caught / nj if nj else 0:>5.0%} {fa:>9}/{nn:<3} {fa / nn if nn else 0:>5.0%}")

    still = [r for r in done if r["_v"] == "job" and r["flagged"] == "no"]
    new_fa = [r for r in done if r["_v"] == "not_job" and r["flagged"] == "yes"]
    print(f"\nReal jobs the current detector still MISSES: {len(still)}")
    for r in still:
        print(f"  - {r['group']} | score {r['job_score']} | {' '.join((r['text'] or '').split())[:100]}")
    print(f"Non-jobs the current detector flags: {len(new_fa)} (these cost a little AI money; harmless)")
    for r in new_fa:
        print(f"  - {r['group']} | score {r['job_score']} [{r['keywords']}] | {' '.join((r['text'] or '').split())[:80]}")
    if not still:
        print("\nNo misses on your judged sample. Next: run --refresh, then check fresh channels.")


def refresh():
    """Re-run the current detector on every stored message and update its flag."""
    store = Store()
    rows = store.all_rows()
    changed = 0
    for r in rows:
        links = json.loads(r["links"] or "[]")
        sc, kw = detect.job_score(r["text"] or "", links)
        cand = int(detect.is_job_candidate(r["text"] or "", sc))
        if cand != r["is_candidate"] or sc != r["job_score"]:
            changed += cand != r["is_candidate"]
            store.conn.execute(
                "UPDATE messages SET job_score=?, keywords=?, is_candidate=? WHERE chat_id=? AND msg_id=?",
                (sc, json.dumps(kw, ensure_ascii=False), cand, r["chat_id"], r["msg_id"]))
    store.conn.commit()
    print(f"Re-checked {len(rows)} stored messages with the current detector; {changed} changed flag.")
    print("Run `python -m jobagent.report` for the updated volume table.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", help="path to your filled-in review sheet")
    ap.add_argument("--unflagged", type=int, default=30, help="how many unflagged messages to sample")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--redetect", action="store_true",
                    help="with --score: compare your judgements against the current detector")
    ap.add_argument("--refresh", action="store_true",
                    help="re-flag all stored messages with the current detector")
    args = ap.parse_args()
    if args.refresh:
        refresh()
        return
    if args.score:
        score(args.score, redetect=args.redetect)
        return
    rows = Store().all_rows()
    if not rows:
        raise SystemExit("No messages stored yet. Run `python -m jobagent.listen --backfill-days 7` first.")
    volume(rows)
    make_review(rows, args.unflagged, args.seed)


if __name__ == "__main__":
    main()
