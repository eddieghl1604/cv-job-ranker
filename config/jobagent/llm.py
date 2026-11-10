"""
One interface for every AI provider, so switching model is a config change.

  anthropic : Claude models (needs ANTHROPIC_API_KEY in .env)
  zai, qwen : OpenAI-compatible APIs (need their keys; used in milestone 3)
  mock      : free fake AI for testing the pipeline; returns rough guesses from keywords
"""
import os
import re
import time
from dataclasses import dataclass


@dataclass
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    seconds: float = 0.0


class Provider:
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
    def __init__(self, *a):
        super().__init__(*a)
        import anthropic
        self.client = anthropic.Anthropic(api_key=_key(self.cfg))

    def ask(self, system, user):
        t0 = time.time()
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_output_tokens,
            temperature=0,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        u = resp.usage
        text = "".join(getattr(b, "text", "") for b in resp.content)
        return Reply(text, u.input_tokens, u.output_tokens,
                     getattr(u, "cache_read_input_tokens", 0) or 0,
                     getattr(u, "cache_creation_input_tokens", 0) or 0,
                     time.time() - t0)


class OpenAICompatProvider(Provider):
    def __init__(self, *a):
        super().__init__(*a)
        from openai import OpenAI  # pip install openai (only needed for zai/qwen)
        self.client = OpenAI(api_key=_key(self.cfg), base_url=self.cfg["base_url"])

    def ask(self, system, user):
        t0 = time.time()
        resp = self.client.chat.completions.create(
            model=self.model, temperature=0, max_tokens=self.max_output_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        u = resp.usage
        return Reply(resp.choices[0].message.content or "",
                     getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0,
                     seconds=time.time() - t0)


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
        out = {
            "is_job": True, "title": post.strip().splitlines()[0][:60] if post.strip() else None,
            "company": None, "location": None, "remote": remote, "open_to_malaysia": open_my,
            "employment_type": None, "years_required_min": float(years.group(1)) if years else None,
            "required_languages": langs, "requirements": [], "evidence": evidence, "scam_signals": [],
            "fit_score": score, "fit_label": "maybe" if target else "weak",
            "why_fit": ["mock result"], "requirements_met": [], "gaps": [], "stretch": [],
        }
        return Reply(json.dumps(out), input_tokens=len(system + user) // 4, output_tokens=150)


def make_provider(agent_cfg: dict, override: str | None = None) -> Provider:
    name = override or agent_cfg.get("provider", "mock")
    cfg = (agent_cfg.get("providers") or {}).get(name)
    if cfg is None:
        raise SystemExit(f"Provider '{name}' is not defined in config/agent.yaml.")
    max_out = (agent_cfg.get("budget") or {}).get("max_output_tokens", 900)
    cls = {"anthropic": AnthropicProvider, "mock": MockProvider}.get(name, OpenAICompatProvider)
    return cls(name, cfg, max_out)
