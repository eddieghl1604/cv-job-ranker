# Milestone 3: model test

Needs: about 40 ratings (done), keys in .env for ANTHROPIC_API_KEY, ZAI_API_KEY, GROQ_API_KEY.

    python -m jobagent.llm --check groq
    python -m jobagent.llm --check groq-small
    python -m jobagent.compare --send

- Takes ~20-30 min (free models are paced). Leave the window open; the agent can keep running.
- If it stops or a model was busy: run `python -m jobagent.compare` again; it continues.
- Results only: `python -m jobagent.compare --report`
- Per-job scores: data\compare_results.csv
- Cost: about USD 0.03 for Claude + Groq; GLM is free.
