"""
Your hard rules, duplicate keys and the evidence check: all plain code, so they behave
the same every time and every skip has a reason you can read.
"""
import hashlib
import re
from urllib.parse import urlparse

from .detect import JOB_LINK_RE, URL_RE
from .schema import Screened

LANG_ALIASES = {
    "english": "english",
    "chinese": "chinese", "mandarin": "chinese", "中文": "chinese", "普通话": "chinese", "cantonese": "chinese",
    "malay": "malay", "bahasa malaysia": "malay", "bahasa melayu": "malay", "bm": "malay", "bahasa": "malay",
}
# Rules that may only be applied if the AI's quote really appears in the post/page
EVIDENCE_FOR_RULE = {"remote": "remote", "open_to_malaysia": "open_to_malaysia",
                     "years_required_min": "years_required_min", "required_languages": "required_languages"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _norm_lang(l: str) -> str:
    return LANG_ALIASES.get(_norm(l), _norm(l))


# ---------------- duplicates ----------------

def link_key(links: list[str]) -> str | None:
    for l in links:
        if JOB_LINK_RE.search(l):
            u = urlparse(l if "://" in l else "https://" + l)
            host = u.netloc.lower().removeprefix("www.")
            return f"{host}{u.path.rstrip('/').lower()}"
    return None


def text_key(text: str) -> str:
    t = URL_RE.sub(" ", text.lower())
    t = re.sub(r"[^\w一-鿿]+", " ", t).strip()
    return hashlib.sha1(t[:400].encode("utf-8")).hexdigest()[:16]


def role_key(s: Screened) -> str | None:
    if not s.title or not s.company:
        return None
    k = re.sub(r"[^\w一-鿿]+", " ", f"{s.company} | {s.title}".lower()).strip()
    return hashlib.sha1(k.encode("utf-8")).hexdigest()[:16]


# ---------------- evidence ----------------

def _loose(s: str) -> str:
    """Lowercase, drop punctuation and spacing differences (curly quotes, dashes, emoji)."""
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", (s or "").lower()).strip()


def check_evidence(s: Screened, source_text: str) -> tuple[float | None, set[str]]:
    """Share of quotes found in the post/page, and the fields whose quote was NOT found."""
    src = _loose(source_text)
    quotes = {k: v for k, v in s.evidence.items() if _loose(v)}
    if not quotes:
        return None, set()
    bad = {field for field, quote in quotes.items() if _loose(quote) not in src}
    return 1 - len(bad) / len(quotes), bad


# ---------------- hard rules ----------------

def apply_rules(s: Screened, rules: dict, unverified: set[str]) -> tuple[str, list[str], list[str]]:
    """Returns (status, skip_reasons, flags). status is 'kept' or 'skipped'.
    A rule may only SKIP a job when the AI quoted the post for it and the quote was found.
    Otherwise the problem becomes a flag: a wrongly skipped job is the worst mistake here."""
    reasons, flags = [], []

    def usable(field):
        return field in s.evidence and field not in unverified

    if not s.is_job:
        return "skipped", ["not a job post (AI)"], flags

    if rules.get("remote_only", True):
        if s.remote in ("onsite", "hybrid"):
            (reasons if usable("remote") else flags).append(
                f"{s.remote}, not remote" + ("" if usable("remote") else " (no verified quote)"))
        elif s.remote == "unknown":
            flags.append("remote status unclear")

    if rules.get("must_be_open_to_malaysia", True):
        if s.open_to_malaysia == "no":
            msg = f"remote but limited to other locations ({s.location or 'see post'})"
            (reasons if usable("open_to_malaysia") else flags).append(
                msg + ("" if usable("open_to_malaysia") else " (no verified quote)"))
        elif s.open_to_malaysia == "unknown":
            flags.append("unclear if open to Malaysia")

    limit = rules.get("skip_if_years_required_at_least")
    if limit is not None and s.years_required_min is not None and s.years_required_min >= limit:
        msg = f"requires {s.years_required_min:g}+ years"
        (reasons if usable("years_required_min") else flags).append(
            msg + ("" if usable("years_required_min") else " (no verified quote)"))

    allowed = {_norm_lang(l) for l in rules.get("allowed_required_languages", ["English", "Chinese"])}
    for lang in s.required_languages:
        if _norm_lang(lang) not in allowed:
            msg = f"requires {lang}"
            (reasons if usable("required_languages") else flags).append(
                msg + ("" if usable("required_languages") else " (no verified quote)"))

    if s.degree_required:
        flags.append(f"degree: {s.degree_required}")
    if s.scam_signals:
        flags.append("possible scam: " + "; ".join(s.scam_signals[:3]))

    return ("skipped" if reasons else "kept"), reasons, flags
