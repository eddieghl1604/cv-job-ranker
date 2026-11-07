"""Local SQLite store. One row per Telegram message; re-running never creates duplicates.

Privacy: no sender names or user IDs are stored, only the group, time and text.
"""
import json
import sqlite3
from datetime import datetime, timezone

from .settings import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id      INTEGER NOT NULL,
    msg_id       INTEGER NOT NULL,
    chat_title   TEXT,
    date_utc     TEXT,
    text         TEXT,
    lang         TEXT,
    job_score    INTEGER,
    keywords     TEXT,
    is_candidate INTEGER,
    links        TEXT,
    has_media    INTEGER,
    media_only   INTEGER,
    fetched_at   TEXT,
    PRIMARY KEY (chat_id, msg_id)
);
"""


class Store:
    def __init__(self, path=DB_PATH):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def save(self, rec: dict) -> bool:
        """Insert a message record. Returns False if it was already stored."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO messages
               (chat_id, msg_id, chat_title, date_utc, text, lang, job_score, keywords,
                is_candidate, links, has_media, media_only, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec["chat_id"], rec["msg_id"], rec["chat_title"], rec["date_utc"], rec["text"],
             rec["lang"], rec["job_score"], json.dumps(rec["keywords"], ensure_ascii=False),
             int(rec["is_candidate"]), json.dumps(rec["links"], ensure_ascii=False),
             int(rec["has_media"]), int(rec["media_only"]),
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def all_rows(self):
        return self.conn.execute("SELECT * FROM messages ORDER BY date_utc").fetchall()
