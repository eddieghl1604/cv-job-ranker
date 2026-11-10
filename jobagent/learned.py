"""
Preferences the agent has learned from your ratings, kept in config/learned.yaml.

Only rules YOU approved live here (via the Approve button, or by editing the file).
They are added to the AI's instructions on every screening, so they change scores from the
next post onwards. Remove one with /rules in your bot, or delete its lines from the file.
"""
import hashlib
from datetime import datetime

import yaml

from .settings import CONFIG_DIR, MYT

LEARNED_PATH = CONFIG_DIR / "learned.yaml"
HEADER = ("# Preferences learned from your ratings. Only rules you approved are here.\n"
          "# Edit freely: one rule per item. Remove a rule with /rules in your bot.\n")


def load() -> dict:
    try:
        with open(LEARNED_PATH, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
    except FileNotFoundError:
        d = {}
    d.setdefault("rules", [])
    d.setdefault("rejected", [])
    return d


def save(d: dict):
    LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LEARNED_PATH, "w", encoding="utf-8") as f:
        f.write(HEADER)
        yaml.safe_dump(d, f, allow_unicode=True, sort_keys=False, width=100)


def rule_id(text: str) -> str:
    return hashlib.sha1(" ".join(text.lower().split()).encode()).hexdigest()[:8]


def active() -> list[dict]:
    return [r for r in load()["rules"] if r.get("text")]


def prompt_block(extra_rules: list[str] | None = None) -> str:
    texts = [r["text"] for r in active()] + list(extra_rules or [])
    if not texts:
        return ""
    return ("Learned preferences (Eddie approved these from his own ratings; when they conflict with the "
            "target-role list above, these win):\n" + "\n".join(f"- {' '.join(t.split())}" for t in texts))


def add(text: str, why: str = "", source: str = "approved in bot") -> str:
    d = load()
    rid = rule_id(text)
    if not any(r.get("id") == rid for r in d["rules"]):
        d["rules"].append({"id": rid, "text": " ".join(text.split()), "why": why,
                           "added": datetime.now(MYT).strftime("%Y-%m-%d"), "source": source})
        save(d)
    return rid


def remove(rid: str) -> str | None:
    d = load()
    for r in d["rules"]:
        if r.get("id") == rid:
            d["rules"].remove(r)
            d["rejected"].append({"text": r["text"], "when": datetime.now(MYT).strftime("%Y-%m-%d"),
                                  "note": "removed by you"})
            save(d)
            return r["text"]
    return None


def reject(text: str):
    d = load()
    d["rejected"].append({"text": " ".join(text.split()), "when": datetime.now(MYT).strftime("%Y-%m-%d"),
                          "note": "rejected by you"})
    save(d)


def fingerprint() -> str:
    """Changes whenever the approved rules change (used to cache test results)."""
    return hashlib.sha1("\n".join(r["text"] for r in active()).encode()).hexdigest()[:8]
