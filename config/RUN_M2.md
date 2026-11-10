# Milestone 2 (part A): screen and score

New files: `config/profile.yaml`, `config/agent.yaml`, and in `jobagent/`: `config.py`,
`schema.py`, `prompt.py`, `llm.py`, `linkread.py`, `rules.py`, `screen.py` (plus an updated `settings.py`).

## How a post is screened (cheapest step first)

| Step | Cost | What happens |
| --- | --- | --- |
| 1. Language | free | Only English and Chinese posts continue |
| 2. Duplicates | free | Same job link or same text already screened: skipped |
| 3. Link | free | Short posts: the job page's text is read too |
| 4. AI | ~$0.0007 | Extracts the facts, quotes the post as evidence, scores fit 0-100 with reasons |
| 5. Evidence check | free | Each quote must really appear in the post/page |
| 6. Your rules | free | Remote, open to Malaysia, under 2 years, English/Chinese only. A rule can only skip a job if its quote was verified; otherwise it becomes a flag |

## Commands

```
python -m jobagent.screen --dry-run --limit 20   # no AI, no cost: plan + estimate
python -m jobagent.screen --limit 20             # real screening (needs ANTHROPIC_API_KEY)
python -m jobagent.screen --export               # data/screened.csv
python -m jobagent.screen --reset                # forget results and redo (messages kept)
python -m jobagent.screen --provider mock --limit 5   # free fake AI, plumbing test only
```

The fit score is a ranking aid for you, not a probability of being hired.
