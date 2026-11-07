"""
Check public channels by @username WITHOUT joining them.

  python -m jobagent.check_channels                  # checks config/candidates.txt
  python -m jobagent.check_channels --days 7 --pause 15

For each @username it reports: whether it exists and is a channel/group,
messages and job-like posts in the last N days, which languages its posts are
in, the last post date, and a verdict. Nothing is joined, stored or sent.

Telegram strictly limits how many @usernames one account can look up in a
short time. So this script goes slowly (a pause between lookups), saves every
result to config/channel_check.csv as it goes, and skips usernames already
checked. If Telegram asks it to wait a long time, it stops cleanly; just run
it again later and it continues where it left off.
"""
import argparse
import asyncio
import csv
from collections import Counter
from datetime import datetime, timedelta, timezone

from telethon import errors, utils
from telethon.tl.types import Channel, Chat, User

from . import detect
from .settings import CONFIG_DIR, MYT
from .tg import make_client

CANDIDATES_PATH = CONFIG_DIR / "candidates.txt"
RESULTS_PATH = CONFIG_DIR / "channel_check.csv"
SUGGESTED_PATH = CONFIG_DIR / "suggested_sources.txt"
FIELDS = ["username", "status", "id", "title", "kind", "msgs", "job_like", "job_per_day",
          "en_zh_share", "langs", "last_post", "verdict"]
MAX_WAIT_SECONDS = 300   # if Telegram asks us to wait longer than this, stop and resume later
SCAN_LIMIT = 1000        # max messages read per channel (busy channels)


def read_candidates() -> list[str]:
    if not CANDIDATES_PATH.exists():
        raise SystemExit(f"{CANDIDATES_PATH} not found.")
    names = []
    for line in CANDIDATES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = line.split()[0]
        if not name.startswith("@"):
            name = "@" + name
        if name.lower() not in {n.lower() for n in names}:
            names.append(name)
    return names


def read_done() -> dict:
    if not RESULTS_PATH.exists():
        return {}
    with open(RESULTS_PATH, encoding="utf-8-sig") as f:
        return {r["username"].lower(): r for r in csv.DictReader(f)}


def write_results(rows: list[dict]):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def verdict(msgs: int, job_like: int, days: int, share: float) -> str:
    if msgs == 0:
        return "inactive"
    if share < 0.5:
        return "skip: mostly other languages"
    if job_like / days < 0.3:
        return "skip: few job posts"
    return "KEEP"


def existing_source_ids() -> set:
    """Numeric IDs already in sources.txt (switched on or off)."""
    try:
        from .survey import all_listed_ids
        return {v for v, _ in all_listed_ids() if isinstance(v, int)}
    except SystemExit:
        return set()


async def check_one(client, name: str, since, days: int, existing_ids: set = frozenset()) -> dict:
    row = {k: "" for k in FIELDS}
    row["username"] = name
    try:
        ent = await client.get_entity(name)
    except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
        row.update(status="not found", verdict="skip: doesn't exist")
        return row
    except errors.ChannelPrivateError:
        row.update(status="private", verdict="skip: private")
        return row

    if isinstance(ent, User):
        row.update(status="person or bot", title=utils.get_display_name(ent),
                   verdict="skip: not a channel")
        return row
    if isinstance(ent, Channel):
        kind = "group" if ent.megagroup else "channel"
    elif isinstance(ent, Chat):
        kind = "group"
    else:
        kind = type(ent).__name__
    row.update(status="ok", id=utils.get_peer_id(ent), title=utils.get_display_name(ent), kind=kind)
    if row["id"] in existing_ids:
        row.update(verdict="already in your list")
        return row

    msgs = job_like = 0
    langs = Counter()
    last = None
    try:
        async for msg in client.iter_messages(ent, offset_date=since, reverse=True, limit=SCAN_LIMIT):
            if getattr(msg, "action", None) is not None:
                continue
            text = msg.message or ""
            if not text.strip():
                continue
            msgs += 1
            last = msg.date
            langs[detect.detect_language(text)] += 1
            score, _ = detect.job_score(text)
            job_like += detect.is_job_candidate(text, score)
        if last is None:
            async for msg in client.iter_messages(ent, limit=1):
                last = msg.date
    except errors.ChannelPrivateError:
        row.update(status="can't read without joining", verdict="skip: needs joining")
        return row

    share = (langs["en"] + langs["zh"]) / msgs if msgs else 0.0
    row.update(
        msgs=msgs, job_like=job_like, job_per_day=round(job_like / days, 1),
        en_zh_share=f"{share:.0%}", langs=", ".join(f"{k}:{v}" for k, v in langs.most_common()),
        last_post=last.astimezone(MYT).strftime("%Y-%m-%d") if last else "",
        verdict=verdict(msgs, job_like, days, share),
    )
    return row


async def main(days: int, pause: float):
    names = read_candidates()
    done = read_done()
    todo = [n for n in names if n.lower() not in done]
    print(f"{len(names)} candidates | already checked: {len(names) - len(todo)} | to check now: {len(todo)}")
    if todo:
        print(f"Going slowly ({pause:.0f}s between lookups) to stay within Telegram's limits: "
              f"about {len(todo) * (pause + 3) / 60:.0f} min.\n")

    client = make_client()
    await client.start()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    existing = existing_source_ids()
    rows = list(done.values())

    for i, name in enumerate(todo, start=1):
        try:
            row = await check_one(client, name, since, days, existing)
        except errors.FloodWaitError as e:
            if e.seconds > MAX_WAIT_SECONDS:
                hours = e.seconds / 3600
                print(f"\nTelegram asked to pause lookups for {e.seconds}s (~{hours:.1f} h). "
                      f"Progress is saved: run this again after that and it will continue.")
                break
            print(f"  waiting {e.seconds}s (Telegram rate limit)…")
            await asyncio.sleep(e.seconds + 2)
            row = await check_one(client, name, since, days, existing)
        rows.append(row)
        write_results(rows)  # save after every channel, so nothing is lost
        print(f"[{i}/{len(todo)}] {name}: {row['verdict']}"
              + (f"  ({row['job_per_day']}/day, en+zh {row['en_zh_share']})" if row["job_per_day"] != "" else ""))
        await asyncio.sleep(pause)
    await client.disconnect()

    summarize(rows)


def summarize(rows: list[dict]):
    order = {"KEEP": 0}
    rows = sorted(rows, key=lambda r: (order.get(r["verdict"], 1), -float(r["job_per_day"] or 0)))
    print(f"\n{'verdict':30} {'username':28} {'job/day':>7} {'en+zh':>6}  {'last post':10}  langs")
    for r in rows:
        print(f"{r['verdict'][:30]:30} {r['username'][:28]:28} {str(r['job_per_day']):>7} "
              f"{r['en_zh_share']:>6}  {r['last_post']:10}  {r['langs'][:40]}")

    keep = [r for r in rows if r["verdict"] == "KEEP"]
    total = sum(float(r["job_per_day"]) for r in keep)
    with open(SUGGESTED_PATH, "w", encoding="utf-8") as f:
        f.write("# Channels the checker recommends. Copy the lines you want into sources.txt.\n")
        for r in keep:
            f.write(f"{r['username']:30} # {r['title']} | {r['job_per_day']}/day | en+zh {r['en_zh_share']}\n")
    print(f"\nKEEP: {len(keep)} channels, about {total:.1f} job-like posts/day together.")
    print(f"Full results: {RESULTS_PATH}\nReady-to-copy lines: {SUGGESTED_PATH}")


def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--pause", type=float, default=15, help="seconds between username lookups")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.days, args.pause))
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved; run again to continue.")


if __name__ == "__main__":
    cli()
