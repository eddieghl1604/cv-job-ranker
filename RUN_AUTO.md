# Auto-start and bot commands

| File | What it does |
| --- | --- |
| `setup_autostart.ps1` | Starts the agent at every Windows login (hidden) and starts it now |
| `stop_agent.ps1` | Stops the background agent (it starts again at next login) |
| `remove_autostart.ps1` | Stops it and removes auto-start |
| `data/agent.log` | What the hidden agent is doing (rotates at 5 MB) |
| `.vscode/settings.json` | VS Code terminals open with the virtual environment already on |

Bot commands (tap "/" in the chat with your bot): /status /top /digest /pause /resume /help.
They only work while the agent is running, and only from your own chat.

Keep-awake: while the agent runs on a plugged-in laptop, Windows won't go to sleep
(the screen still turns off). Change with `agent: keep_awake:` in config/agent.yaml.
