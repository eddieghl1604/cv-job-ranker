# Milestone 1: listen locally

Goal: see which messages in your groups are job posts, and how many arrive per day.
Nothing is sent to any AI and nothing is sent on Telegram. Everything stays in `data/agent.db` on your laptop.

## Files

| File | What it does |
| --- | --- |
| `jobagent/settings.py` | Loads keys from `.env`, holds file locations |
| `jobagent/detect.py` | Free checks: language, job-post keyword score, links |
| `jobagent/store.py` | Saves messages to a local database (no sender names) |
| `jobagent/tg.py` | Telegram connection (read-only) |
| `jobagent/list_chats.py` | Lists your groups/channels so you can pick |
| `jobagent/listen.py` | Reads past days and/or listens live |
| `jobagent/report.py` | Volume table, review sheet, detector check |

## Commands (run from the project folder with `(.venv)` active)

```
pip install telethon python-dotenv
pip freeze > requirements.txt
python -m jobagent.list_chats
python -m jobagent.listen --backfill-days 7
python -m jobagent.report
python -m jobagent.report --score data/review_m1.csv
```

## Gate to pass before milestone 2

- The detector misses no real job posts in the review sample (or we tune it until it doesn't)
- You know roughly how many job posts per day your groups produce
