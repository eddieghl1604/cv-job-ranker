"""
One interface for every AI provider, so switching model is a config change.

  anthropic : Claude models (needs ANTHROPIC_API_KEY in .env)
  zai, groq : OpenAI-compatible APIs (need their keys; used in milestone 3)
  mock      : free fake AI for testing the pipeline; returns rough guesses from keywords
"""
import os
import re
import time
from dataclasses import dataclass

from .prompt import ANSWER_SCHEMA, TOOL_NAME


@dataclass
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    seconds: float = 0.0
    data: dict | None = None        # structured answer, when the provider supports it
    stop_reason: str = ""


class Provider:
    structured = False   # True = answers come back as a validated object, not free text

    def __init__(self, name: str, cfg: dict, max_output_tokens: int):
        self.name = name
        self.cfg = cfg
        self.model = cfg.get("model")
        self.max_output_tokens = max_output_tokens
        self.prices = cfg.get("price_per_m", {})

    def cost_usd(self, r: Reply) -> float:
        p = self.prices
        uncached = max(r.input_tokens, 0)
        return (uncached * p.get("input", 0) + r.output_tokens * p.get("output", 0)
                + r.cache_read_tokens * p.get("cache_read", p.get("input", 0))
                + r.cache_write_tokens * p.get("cache_write", p.get("input", 0))) / 1e6

    def ask(self, system: str, user: str) -> Reply:
        raise NotImplementedError


def _key(cfg: dict) -> str:
    env = cfg.get("key_env", "")
    key = os.getenv(env, "").strip()
    if not key:
        raise SystemExit(f"{env} is missing from .env. Add it, or set provider: mock in config/agent.yaml to test.")
    return key


class AnthropicProvider(Provider):
    """Uses a forced tool call, so Claude must answer in exactly ANSWER_SCHEMA's shape."""
    structured = True

    def __init__(self, *a):
        super().__init__(*a)
        import anthropic
        self.client = anthropic.Anthropic(api_key=_key(self.cfg), max_retries=self.cfg.get("max_retries", 4))

    def ask(self, system, user):
        t0 = time.time()
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_output_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            tools=[{"name": TOOL_NAME, "description": "Record the screening result for this post.",
                    "input_schema": ANSWER_SCHEMA}],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        # Newer Claude models reject `temperature`; only send it if agent.yaml sets one.
        if self.cfg.get("temperature") is not None:
            kwargs["temperature"] = self.cfg["temperature"]
        resp = self.client.messages.create(**kwargs)
        u = resp.usage
        data = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
        text = "".join(getattr(b, "text", "") or "" for b in resp.content)
        return Reply(text, u.input_tokens, u.output_tokens,
                     getattr(u, "cache_read_input_tokens", 0) or 0,
                     getattr(u, "cache_creation_input_tokens", 0) or 0,
                     time.time() - t0, data=data, stop_reason=getattr(resp, "stop_reason", "") or "")


class OpenAICompatProvider(Provider):
    def __init__(self, *a):
        super().__init__(*a)
        from openai import OpenAI  # pip install openai (only needed for zai/groq)
        # Free models are often busy (error 429): retry up to 6 times with growing pauses.
        self.client = OpenAI(api_key=_key(self.cfg), base_url=self.cfg["base_url"],
                             max_retries=self.cfg.get("max_retries", 6), timeout=90)

    def ask(self, system, user):
        t0 = time.time()
        kwargs = dict(model=self.model, max_tokens=self.max_output_tokens,
                      messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        if self.cfg.get("temperature", 0) is not None:
            kwargs["temperature"] = self.cfg.get("temperature", 0)
        if self.cfg.get("json_mode"):
            kwargs["response_format"] = {"type": "json_object"}
        extra = {}
        if self.cfg.get("reasoning_effort"):           # GPT-OSS on Groq: how much the model "thinks"
            extra["reasoning_effort"] = self.cfg["reasoning_effort"]
            extra["include_reasoning"] = False         # don't send the thinking text back (still billed)
        if extra:
            kwargs["extra_body"] = extra
        resp = self.client.chat.completions.create(**kwargs)
        u = resp.usage
        prompt = getattr(u, "prompt_tokens", 0) or 0
        details = getattr(u, "prompt_tokens_details", None)
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        return Reply(resp.choices[0].message.content or "",
                     prompt - cached, getattr(u, "completion_tokens", 0) or 0, cache_read_tokens=cached,
                     seconds=time.time() - t0, stop_reason=resp.choices[0].finish_reason or "")


class MockProvider(Provider):
    """Keyword guesses only. For testing the plumbing, never for real screening."""

    def ask(self, system, user):
        import json
        post = user.split("<<<", 1)[-1]
        low = post.lower()
        years = re.search(r"(\d+)\s*\+?\s*(?:-\s*\d+\s*)?years?", low)
        remote = ("onsite" if re.search(r"on-?site|in office", low) else
                  "hybrid" if "hybrid" in low else "remote" if "remote" in low or "远程" in post else "unknown")
        if re.search(r"remote\s*[-–]\s*(usa|us|uk|emea|europe|india)\b", low):
            open_my = "no"
        elif re.search(r"worldwide|global|anywhere|apac|asia|malaysia", low):
            open_my = "yes"
        else:
            open_my = "unknown"
        langs = [l for l in ("Malay", "Spanish", "German") if l.lower() in low and "required" in low]
        target = any(w in low for w in ("analyst", "investment", "ai ", "machine learning", "automation", "fintech"))
        evidence = {}
        for field, pat in (("remote", r"[^.\n]{0,20}(remote|on-?site|hybrid)[^.\n]{0,20}"),
                           ("years_required_min", r"\d+\s*\+?\s*years?[^.\n]{0,15}"),
                           ("open_to_malaysia", r"(worldwide|global|anywhere|apac|asia|malaysia|remote\s*[-–]\s*\w+)")):
            m = re.search(pat, post, re.I)
            if m:
                evidence[field] = m.group(0).strip()
        score = 75 if target else 30
        job = {
            "title": post.strip().splitlines()[0][:60] if post.strip() else None,
            "company": None, "location": None, "remote": remote, "open_to_malaysia": open_my,
            "employment_type": None, "years_required_min": float(years.group(1)) if years else None,
            "required_languages": langs, "requirements": [], "evidence": evidence, "scam_signals": [],
            "fit_score": score, "fit_label": "maybe" if target else "weak",
            "why_fit": ["mock result"], "requirements_met": [], "gaps": [], "stretch": [],
        }
        out = {"is_job_post": True, "not_job_reason": None, "jobs": [job]}
        return Reply(json.dumps(out), input_tokens=len(system + user) // 4, output_tokens=150)


def make_provider(agent_cfg: dict, override: str | None = None) -> Provider:
    name = override or agent_cfg.get("provider", "mock")
    cfg = (agent_cfg.get("providers") or {}).get(name)
    if cfg is None:
        raise SystemExit(f"Provider '{name}' is not defined in config/agent.yaml.")
    max_out = (agent_cfg.get("budget") or {}).get("max_output_tokens", 2500)
    cls = {"anthropic": AnthropicProvider, "mock": MockProvider}.get(name, OpenAICompatProvider)
    return cls(name, cfg, max_out)


def check(name: str, model: str | None = None):
    """Send one tiny request to a provider to prove the key, address and model name work."""
    from . import settings  # noqa: F401  (loads .env)
    from .config import load_agent
    cfg = load_agent()
    p = make_provider(cfg, name)
    if model:
        p.model = model
    print(f"Checking {name}: model '{p.model}'" + (f" at {p.cfg.get('base_url')}" if p.cfg.get("base_url") else ""))
    try:
        r = p.ask("You check that an API works. Reply with a JSON object only.",
                  'Reply exactly: {"is_job_post": false, "not_job_reason": "connection test", "jobs": []}')
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        hint = ("the service is busy or rate-limited (common for free models). Your key, address and model "
                "name were accepted. Try again in a few minutes" if any(x in low for x in ("429", "overload", "rate limit", "too many"))
                else "the service isn't activated for this account, or the key is from another region. In the "
                     "provider's console: pick the right region, accept the service terms to activate it, and "
                     "create the key in that same region" if any(x in low for x in ("403", "access to model denied", "eligible", "accessdenied"))
                else "the key is wrong or not active yet" if any(x in low for x in ("401", "auth", "api key", "invalid key"))
                else "the model name isn't available on this account/region: check the model list in the provider's console"
                if any(x in low for x in ("404", "not found", "does not exist"))
                else "no credit / free quota not activated" if any(x in low for x in ("402", "balance", "quota", "insufficient", "arrear"))
                else "network or address problem")
        raise SystemExit(f"FAILED: {type(e).__name__}: {msg[:300]}\nLikely cause: {hint}.")
    reply = r.data if r.data is not None else r.text
    print(f"OK in {r.seconds:.1f}s | tokens in {r.input_tokens + r.cache_read_tokens}, out {r.output_tokens} | "
          f"cost ${p.cost_usd(r):.6f}")
    print(f"Reply: {str(reply)[:200]}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Check an AI provider's key and model name.")
    ap.add_argument("--check", required=True, help="provider name from config/agent.yaml: anthropic, zai, groq, groq-small")
    ap.add_argument("--model", help="try a different model name without editing agent.yaml")
    a = ap.parse_args()
    check(a.check, a.model)
