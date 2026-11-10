"""
The shape of the AI's answer. One post can contain several jobs, so the answer is:
  { "is_job_post": bool, "not_job_reason": str|null, "jobs": [Job, ...] }

The models are deliberately forgiving: a number sent as text, a list sent as a string,
"Remote - USA" in the remote field and so on are cleaned up instead of rejected.
"""
import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

STR_FIELDS = ("title", "company", "location", "employment_type", "degree_required",
              "salary", "deadline", "apply_link")
LIST_FIELDS = ("required_languages", "requirements", "scam_signals", "why_fit",
               "requirements_met", "gaps", "stretch")


def _to_str(v):
    if v is None:
        return None
    if isinstance(v, list):
        v = "; ".join(str(x) for x in v if x not in (None, ""))
    elif isinstance(v, dict):
        v = json.dumps(v, ensure_ascii=False)
    v = str(v).strip()
    return v or None


def _to_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)]


class Job(BaseModel):
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    remote: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    open_to_malaysia: Literal["yes", "no", "unknown"] = "unknown"
    employment_type: Optional[str] = None
    years_required_min: Optional[float] = None
    required_languages: list[str] = Field(default_factory=list)
    degree_required: Optional[str] = None
    salary: Optional[str] = None
    deadline: Optional[str] = None
    apply_link: Optional[str] = None
    requirements: list[str] = Field(default_factory=list)
    evidence: dict[str, str] = Field(default_factory=dict)
    scam_signals: list[str] = Field(default_factory=list)
    fit_score: int = 0
    fit_label: Literal["strong", "maybe", "weak"] = "weak"
    why_fit: list[str] = Field(default_factory=list)
    requirements_met: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    stretch: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _clean(cls, d):
        if not isinstance(d, dict):
            return d
        d = dict(d)
        for k in STR_FIELDS:
            if k in d:
                d[k] = _to_str(d[k])
        for k in LIST_FIELDS:
            if k in d:
                d[k] = _to_list(d[k])

        r = str(d.get("remote") or "").lower()
        d["remote"] = ("hybrid" if "hybrid" in r else
                       "onsite" if re.search(r"on-?site|in[- ]office|office", r) else
                       "remote" if "remote" in r or r in ("yes", "true") else "unknown")

        o = d.get("open_to_malaysia")
        o = str(o).lower() if o is not None else ""
        d["open_to_malaysia"] = "yes" if o in ("yes", "true", "y") else "no" if o in ("no", "false", "n") else "unknown"

        y = d.get("years_required_min")
        if isinstance(y, str):
            m = re.search(r"\d+(\.\d+)?", y)
            d["years_required_min"] = float(m.group(0)) if m else None
        elif isinstance(y, bool) or not isinstance(y, (int, float, type(None))):
            d["years_required_min"] = None

        ev = d.get("evidence")
        if isinstance(ev, dict):
            d["evidence"] = {str(k): _to_str(v) for k, v in ev.items() if _to_str(v)}
        else:
            d["evidence"] = {}

        try:
            s = int(round(float(d.get("fit_score", 0))))
        except (TypeError, ValueError):
            s = 0
        d["fit_score"] = max(0, min(100, s))
        label = str(d.get("fit_label") or "").lower()
        if label not in ("strong", "maybe", "weak"):
            label = "strong" if s >= 80 else "maybe" if s >= 50 else "weak"
        d["fit_label"] = label
        return d


class ScreenResult(BaseModel):
    is_job_post: bool = True
    not_job_reason: Optional[str] = None
    jobs: list[Job] = Field(default_factory=list)

    @field_validator("jobs", mode="before")
    @classmethod
    def _jobs_list(cls, v):
        if v is None:
            return []
        return v if isinstance(v, list) else [v]

    @model_validator(mode="after")
    def _consistent(self):
        if not self.jobs:
            self.is_job_post = False
        return self


def _strip_json_noise(text: str) -> str:
    """Remove // comments and trailing commas that some models add."""
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    text = re.sub(r"(?<![:\"'\w])//[^\n\"]*$", "", text, flags=re.M)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def parse_data(data: dict) -> ScreenResult:
    # Accept an old-style single job ({"is_job": ..., "title": ...}) too
    if "jobs" not in data and ("title" in data or "is_job" in data):
        is_job = bool(data.get("is_job", True))
        data = {"is_job_post": is_job, "jobs": [data] if is_job else []}
    return ScreenResult.model_validate(data)


def parse_reply(text: str) -> ScreenResult:
    """For providers that answer in plain text: find the JSON object and parse it."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in the reply")
    raw = m.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(_strip_json_noise(raw))
    return parse_data(data)
