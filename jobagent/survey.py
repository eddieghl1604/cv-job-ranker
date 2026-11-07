"""
Count activity in EVERY chat listed in config/sources.txt, including lines
switched off with '#', to see which channels are alive and how many job posts
they produce. Nothing is stored and nothing is sent anywhere: it only counts.

  python -m jobagent.survey               # last 7 days
  python -m jobagent.survey --days 14

Use the result to decide which channels to switch on, and to estimate total
job posts per day (which decides the AI cost in milestone 2).
"""
import argparse
import asyncio
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from telethon import utils

from . import detect
from .settings import SOURCES_PATH
from .tg import make_client

ID_LINE = re.compile(r"^\s*(#)?\s*(@\w{4,}|-?\d{6,})")


def all_listed_ids():
    """Every chat ID in sources.txt, on or off. Returns [(id, is_on)]."""
    if not SOURCES_PATH.exists():
        raise SystemExit(f"{SOURCES_PATH} not found.")
    out = []
    for line in SOURCES_PATH.read_text(encoding="utf-8").splitlines():
        m = ID_LINE.match(line)
        if m:
            val = m.group(2)
            out.append((val if val.startswith("@") else int(val), m.group(1) is None))
    return out


async def main(days: int):
    client = make_client()
    await client.start()
    await client.get_dialogs()
    since = datetime.now(timezone.utc) - timedelta(days=days)

    results = []
    listed = all_listed_ids()
    print(f"Surveying {len(listed)} chats over the last {days} days (counting only)…")
    for cid, is_on in listed:
        try:
            ent = await client.get_entity(cid)
        except Exception:
            results.append({"title": f"(could not open {cid})", "on": is_on, "msgs": 0,
                            "cand": 0, "img": 0, "langs": Counter(), "last": None})
            continue
        title = utils.get_display_name(ent)
        msgs = cand = img = 0
        langs = Counter()
        last = None
        async for msg in client.iter_messages(ent, offset_date=since, reverse=True):
            if getattr(msg, "action", None) is not None:
                continue
            text = msg.message or ""
            if not text.strip() and msg.media is None:
                continue
            msgs += 1
            last = msg.date
            if not text.strip():
                img += 1
                continue
            score, _ = detect.job_score(text)
            if detect.is_job_candidate(text, score):
                cand += 1
                langs[detect.detect_language(text)] += 1
        if last is None:  # nothing in the window: when was the last post at all?
            async for msg in client.iter_messages(ent, limit=1):
                last = msg.date
        results.append({"title": title, "on": is_on, "msgs": msgs, "cand": cand,
                        "img": img, "langs": langs, "last": last})
        print(f"  done: {title[:50]}")
    await client.disconnect()

    results.sort(key=lambda r: -r["cand"])
    print(f"\n{'ON':3} {'Chat':44} {'msgs':>6} {'job?':>6} {'job?/day':>9} {'img-only':>9}  {'last post':10}  job? langs")
    for r in results:
        last = r["last"].strftime("%Y-%m-%d") if r["last"] else "-"
        langs = ", ".join(f"{k}:{v}" for k, v in sorted(r["langs"].items())) or "-"
        print(f"{'yes' if r['on'] else '':3} {r['title'][:44]:44} {r['msgs']:>6} {r['cand']:>6} "
              f"{r['cand'] / days:>9.1f} {r['img']:>9}  {last:10}  {langs}")
    total = sum(r["cand"] for r in results)
    print(f"\nAll listed chats: about {total / days:.1f} job-like posts per day (before duplicates and filters).")
    print("Chats with a 'last post' older than your window are inactive: leave them off.")


def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    asyncio.run(main(args.days))


if __name__ == "__main__":
    cli()
