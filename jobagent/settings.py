"""Settings: keys from .env, file locations, and the list of groups to watch."""
import os
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"
DB_PATH = DATA_DIR / "agent.db"
SESSION_PATH = ROOT / "jobagent"          # Telethon adds ".session"; this file IS your login
SOURCES_PATH = CONFIG_DIR / "sources.txt"
AVAILABLE_CHATS_PATH = CONFIG_DIR / "available_chats.csv"

# Malaysia has no daylight saving, so a fixed UTC+8 offset is exact.
MYT = timezone(timedelta(hours=8), "MYT")


def telegram_keys() -> tuple[int, str]:
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID or TELEGRAM_API_HASH is missing from .env "
                         f"(looked in {ROOT / '.env'}).")
    if not api_id.isdigit():
        raise SystemExit("TELEGRAM_API_ID should be digits only, with no quotes or spaces.")
    return int(api_id), api_hash


def load_sources() -> list:
    """Chats from config/sources.txt: one per line, either a numeric ID
    (-1001234567890) or a public @username; '#' starts a comment."""
    if not SOURCES_PATH.exists():
        raise SystemExit(f"{SOURCES_PATH} not found. Run `python -m jobagent.list_chats` first, "
                         f"then put the IDs you want into that file.")
    ids = []
    for n, line in enumerate(SOURCES_PATH.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        line = line.split()[0]
        if line.startswith("@") and len(line) > 1:
            ids.append(line)
            continue
        try:
            ids.append(int(line))
        except ValueError:
            raise SystemExit(f"sources.txt line {n}: '{line}' is not a chat ID or @username")
    if not ids:
        raise SystemExit("sources.txt has no chat IDs yet.")
    return ids
