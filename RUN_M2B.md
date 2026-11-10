# Milestone 2 (part B): sheet, alerts, digest, and the agent loop

New in `jobagent/`: `notify.py`, `sheet.py`, `run.py`. Settings: the `notify`, `agent` and `sheet`
sections at the bottom of `config/agent.yaml`.

| Setting | Value |
| --- | --- |
| Instant alert | kept jobs scoring 60+ |
| Quiet hours | 23:00-08:00 (alerts wait until morning) |
| Daily digest | 21:00, top 10 + near misses (skipped but 55+) |
| Checks channels | every 15 minutes |
| Ignores posts older than | 3 days |

## Commands

```
python -m jobagent.notify --test     # bot finds your chat and sends a test message
python -m jobagent.sheet --test      # checks sheet access, writes the header row
python -m jobagent.sheet             # adds rows for jobs already screened
python -m jobagent.notify --digest   # sends a digest right now
python -m jobagent.run --once        # one full cycle
python -m jobagent.run               # keeps running (Ctrl+C to stop)
```

Safety: the bot only messages your own chat; sheet text is written raw (no formulas);
if a step fails, the bot tells you (at most once an hour) and the other steps still run.
