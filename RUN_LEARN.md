# Self-learning (after milestone 3)

Install: unzip over the project, then restart the agent (stop_agent.ps1, then setup_autostart.ps1,
or just restart the PC). Add this line to .gitignore:  config/learned.yaml

What changes in Telegram:
- After 🤔 or 👎: "Why?" buttons. Tap all that apply, then Done (2 seconds).
- /rate shows more high-scored jobs (last 30 days), still blind.
- Spot-check cards arrive on their own: every 12 h at first, then 24 h, then about every 3 days
  once the AI matches you 80%+ with no big misses. Never during quiet hours.
- Sunday 20:00 (if you gave 5+ new ratings): learning review. Rules that pass the test on your
  past ratings come with Approve / Reject buttons. /learn runs one now.
- /rules lists approved rules; 🗑 removes one.
- Each time you reach another 15 👍, the model test (Claude vs GPT-OSS-20B) re-runs by itself.

Cost: a review is about USD 0.10-0.20 (~RM0.50); spot-checks are free.
Try now:  python -m jobagent.learn --dry   (shows what the review would send the AI; free)
