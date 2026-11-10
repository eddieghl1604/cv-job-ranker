"""Load config/profile.yaml and config/agent.yaml."""
import yaml

from .settings import CONFIG_DIR

PROFILE_PATH = CONFIG_DIR / "profile.yaml"
AGENT_PATH = CONFIG_DIR / "agent.yaml"


def _load(path):
    if not path.exists():
        raise SystemExit(f"{path} not found.")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_profile() -> dict:
    p = _load(PROFILE_PATH)
    for key in ("summary", "target_roles", "rules"):
        if key not in p:
            raise SystemExit(f"profile.yaml is missing '{key}'.")
    return p


def load_agent() -> dict:
    return _load(AGENT_PATH)


def profile_text(p: dict, extra_rules: list[str] | None = None) -> str:
    """The profile as plain text for the AI (rules are applied in code, so left out).
    Learned preferences you approved (config/learned.yaml) are added at the end;
    extra_rules lets the learning review test a candidate rule before you approve it."""
    lines = [f"Summary: {' '.join(str(p['summary']).split())}",
             f"Total relevant experience: {p.get('experience_years', 'unknown')} year(s)"]
    for e in p.get("experience", []) or []:
        lines.append(f"- {e.get('role')} at {e.get('org')} ({e.get('period')})")
        for d in e.get("did", []) or []:
            lines.append(f"    * {d}")
    for title, key in (("Projects", "projects"), ("Skills", "skills"),
                       ("Learning now (stretch, not yet proven)", "learning_now"),
                       ("Languages", "languages"), ("Target roles, in priority order", "target_roles"),
                       ("Not interested in (score low)", "not_interested")):
        items = p.get(key) or []
        if items:
            lines.append(f"{title}:")
            lines += [f"- {i}" for i in items]
    from .learned import prompt_block
    block = prompt_block(extra_rules)
    if block:
        lines += ["", block]
    return "\n".join(lines)
