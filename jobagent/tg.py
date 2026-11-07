"""
Telegram connection and message processing.

READ-ONLY BY DESIGN: this project only ever calls iter_dialogs, get_dialogs,
get_entity, iter_messages and listens for NewMessage events. It never sends,
forwards, deletes, joins, leaves or marks anything as read. You can verify:
search the jobagent folder for "send", "forward", "delete", "join" - there are
no such calls.
"""
from telethon import TelegramClient
from telethon.tl.types import MessageEntityTextUrl

from . import detect
from .settings import SESSION_PATH, telegram_keys


def make_client() -> TelegramClient:
    api_id, api_hash = telegram_keys()
    return TelegramClient(
        str(SESSION_PATH), api_id, api_hash,
        device_model="Job Screener (read-only)",  # how this login appears in Settings > Devices
        flood_sleep_threshold=120,                # wait out Telegram rate limits up to 2 min
    )


def _all_links(msg, text: str) -> list[str]:
    links = detect.links_in_text(text)
    try:  # hidden links behind words, e.g. "Apply here"
        for ent in msg.entities or []:
            if isinstance(ent, MessageEntityTextUrl):
                links.append(ent.url)
    except Exception:
        pass
    try:  # "Apply" buttons under channel posts
        for row in msg.buttons or []:
            for btn in row:
                if getattr(btn, "url", None):
                    links.append(btn.url)
    except Exception:
        pass
    seen, out = set(), []
    for link in links:
        if link not in seen:
            seen.add(link)
            out.append(link)
    return out


def to_record(msg, chat_id: int, chat_title: str):
    """Turn a Telethon message into a record for the store. Returns None for service
    messages (joins, pins) and empty messages."""
    if getattr(msg, "action", None) is not None:
        return None
    text = msg.message or ""
    has_media = msg.media is not None
    if not text.strip() and not has_media:
        return None
    links = _all_links(msg, text)
    score, keywords = detect.job_score(text, links)
    return {
        "chat_id": chat_id,
        "msg_id": msg.id,
        "chat_title": chat_title,
        "date_utc": msg.date.isoformat(),
        "text": text,
        "lang": detect.detect_language(text),
        "job_score": score,
        "keywords": keywords,
        "is_candidate": detect.is_job_candidate(text, score),
        "links": links,
        "has_media": has_media,
        "media_only": has_media and not text.strip(),
    }
