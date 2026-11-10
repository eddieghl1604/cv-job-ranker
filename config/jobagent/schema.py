"""The exact shape the AI must return. Anything else is rejected (and retried once)."""
import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Screened(BaseModel):
    is_job: bool
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
    fit_score: int = Field(ge=0, le=100)
    fit_label: Literal["strong", "maybe", "weak"]
    why_fit: list[str] = Field(default_factory=list)
    requirements_met: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    stretch: list[str] = Field(default_factory=list)

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence_strings(cls, v):
        if not isinstance(v, dict):
            return {}
        return {str(k): str(val) for k, val in v.items() if val not in (None, "", [])}

    @field_validator("remote", "open_to_malaysia", "fit_label", mode="before")
    @classmethod
    def _lower(cls, v):
        return str(v).strip().lower() if v is not None else v

    @field_validator("fit_score", mode="before")
    @classmethod
    def _int_score(cls, v):
        return int(round(float(v)))


def parse_reply(text: str) -> Screened:
    """Pull the first JSON object out of the reply (models sometimes wrap it in ``` fences)."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in the reply")
    return Screened.model_validate(json.loads(m.group(0)))
