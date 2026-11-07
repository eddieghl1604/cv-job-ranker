"""
Step 1: log in once and list your groups and channels, so you can pick which to watch.

  python -m jobagent.list_chats

First run asks for your phone number (with country code, e.g. +60123456789),
the login code Telegram sends you, and your two-step verification password.
After that, the login is saved in jobagent.session and you won't be asked again.

Private one-to-one chats are never listed or read.
"""
import asyncio
import csv

from .settings import AVAILABLE_CHATS_PATH, CONFIG_DIR, SOURCES_PATH
from .tg import make_client


async def main():
    client = make_client()
    await client.start()
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (read-only use).\n")

    rows = []
    async for d in client.iter_dialogs():
        if not (d.is_group or d.is_channel):
            continue  # skip private chats entirely
        kind = "channel" if d.is_channel and not d.is_group else "group"
        rows.append({"id": d.id, "type": kind, "title": d.name or ""})
    await client.disconnect()

    rows.sort(key=lambda r: (r["type"], r["title"].lower()))
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(AVAILABLE_CHATS_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["id", "type", "title"])
        w.writeheader()
        w.writerows(rows)

    print(f"{'ID':>16}  {'TYPE':8} TITLE")
    for r in rows:
        print(f"{r['id']:>16}  {r['type']:8} {r['title']}")
    print(f"\n{len(rows)} groups/channels. Also saved to {AVAILABLE_CHATS_PATH}")

    if not SOURCES_PATH.exists():
        SOURCES_PATH.write_text(
            "# One chat ID per line. Start with 2-3 job groups for milestone 1.\n"
            "# Text after # is ignored, so you can note the name:\n"
            "# -1001234567890   # Example Remote Jobs MY\n",
            encoding="utf-8",
        )
        print(f"Created {SOURCES_PATH}: paste the IDs you want to watch into it.")


if __name__ == "__main__":
    asyncio.run(main())
