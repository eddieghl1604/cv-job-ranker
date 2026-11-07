"""
Step 2: read messages from the groups in config/sources.txt and store them locally.

  python -m jobagent.listen --backfill-days 7     # read the last 7 days, then stop
  python -m jobagent.listen --live                # keep running; store new messages as they arrive
  python -m jobagent.listen --backfill-days 7 --live

Backfill gives you a week of history immediately, so you can measure volume
today instead of waiting. Running it twice is safe: stored messages are skipped.
Nothing is sent to any AI in milestone 1; everything stays in data/agent.db.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from telethon import events, utils

from .settings import MYT, load_sources
from .store import Store
from .tg import make_client, to_record


def _short(text: str, n: int = 90) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= n else one_line[: n - 1] + "…"


async def main(args):
    client = make_client()
    await client.start()
    await client.get_dialogs()  # loads your chats so IDs can be resolved

    store = Store()
    chats = {}
    for cid in load_sources():
        try:
            ent = await client.get_entity(cid)
        except Exception as e:
            print(f"! Could not open chat {cid}: {e}. Are you still a member? Skipping.")
            continue
        chats[utils.get_peer_id(ent)] = (ent, utils.get_display_name(ent))
    if not chats:
        raise SystemExit("None of the chats in sources.txt could be opened.")
    print(f"Watching {len(chats)} chats: " + ", ".join(t for _, t in chats.values()))

    if args.backfill_days:
        since = datetime.now(timezone.utc) - timedelta(days=args.backfill_days)
        print(f"\nBackfilling messages since {since.astimezone(MYT):%Y-%m-%d %H:%M} MYT …")
        for peer_id, (ent, title) in chats.items():
            new = cand = 0
            async for msg in client.iter_messages(ent, offset_date=since, reverse=True):
                rec = to_record(msg, peer_id, title)
                if rec and store.save(rec):
                    new += 1
                    cand += rec["is_candidate"]
            print(f"  {title}: {new} new messages stored, {cand} look like job posts")

    if args.live:
        @client.on(events.NewMessage(chats=[ent for ent, _ in chats.values()]))
        async def on_message(event):
            peer_id = event.chat_id
            title = chats.get(peer_id, (None, str(peer_id)))[1]
            rec = to_record(event.message, peer_id, title)
            if rec and store.save(rec) and rec["is_candidate"]:
                stamp = event.message.date.astimezone(MYT).strftime("%H:%M")
                print(f"[{stamp}] JOB? {title}: {_short(rec['text'])}")

        print("\nListening for new messages. Leave this window open; press Ctrl+C to stop.")
        try:
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            pass
    await client.disconnect()


def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-days", type=int, default=0)
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    if not args.backfill_days and not args.live:
        ap.error("choose --backfill-days N, --live, or both")
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    cli()
