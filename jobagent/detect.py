"""
Cheap, no-AI checks run on every message:
  - which language it is in (English / Chinese / Malay / other)
  - whether it looks like a job post (keyword score)
  - which links it contains

Scoring: strong evidence (an ad word like "hiring"/"招聘", or listing structure
such as labelled fields, job emojis or a job-page link) is worth 3 points; weak
evidence ("salary", "远程", a job-title word, a pay amount) 1 point; each counted
once. A message with 3+ points and at least 40 characters is a job candidate.

Version 2.2 (7 Nov): added listing-structure signals (incl. job hashtags) after the M1 review showed
aggregator channels (Remote OK, Degen Crypto Jobs, Jobs Crypto UAE) post jobs
without any ad wording.
"""
import re

EN_STRONG = [
    "hiring", "we're hiring", "we are hiring", "job opening", "job openings", "vacancy",
    "vacancies", "internship", "internships", "job description", "apply now", "how to apply",
    "send your cv", "send your resume", "now recruiting", "open position", "open positions",
]
EN_WEAK = [
    "job", "jobs", "role", "position", "intern", "apply", "requirements", "requirement",
    "qualifications", "responsibilities", "salary", "remote", "full-time", "full time",
    "part-time", "part time", "contract", "freelance", "experience", "candidate", "candidates",
    "jd", "cv", "resume", "per month", "benefits", "analyst", "engineer", "work from home", "wfh",
]
ZH_STRONG = ["招聘", "诚聘", "急聘", "职位", "岗位", "实习", "职位描述", "岗位职责", "任职要求"]
ZH_WEAK = ["远程", "薪资", "薪酬", "待遇", "要求", "职责", "工作地点", "兼职", "全职", "简历",
           "经验", "面试", "福利", "月薪", "投递"]
MS_MARKERS = ["dan", "yang", "untuk", "kami", "anda", "kerja", "jawatan", "gaji", "syarat",
              "mohon", "sebagai", "dengan", "boleh", "pengalaman", "kosong"]

# Pay patterns such as "RM 3,000", "RM3k", "$1500", "USD 2,000"
PAY_RE = re.compile(r"(\bRM\s?\d|\$\s?\d|\bUSD\s?\d|\bSGD\s?\d|\d+\s?k\s?(?:/|per)\s?(?:month|mo)\b)", re.I)
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\"']+", re.I)
CJK_RE = re.compile(r"[一-鿿]")
LATIN_RE = re.compile(r"[A-Za-z]")
CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
ARABIC_RE = re.compile(r"[؀-ۿ]")
ES_MARKERS = ["para", "con", "trabajo", "empleo", "remoto", "experiencia", "que", "los", "las",
              "una", "del", "por", "vacante", "requisitos", "salario", "postulate", "nosotros"]


def _word_re(words):
    # \b doesn't work around "'" or "-" reliably, so use explicit non-letter boundaries
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z])({alts})(?![A-Za-z])", re.I)


# Listing structure (aggregator channels)
FIELD_LABEL_RE = re.compile(
    r"(?:^|[\s|•·,;])(company|title|job title|position|role|location|salary|compensation|"
    r"date posted|posted|job type|employment type|work type|seniority|level|tags|apply)\s*[:：]",
    re.I | re.M)
JOB_EMOJIS = ["📍", "💼", "💰", "👤", "🏢", "💵", "🕒", "⏰", "🌍", "🌏", "🌎"]
ROLE_EMOJIS = {"💼", "👤"}
# Hashtag-style listings, e.g. "LEAD BACKEND DEVELOPER | REMOTE | FINTECH #remote #fulltime #itjob"
JOB_HASHTAGS = {
    "itjob", "itjobs", "job", "jobs", "hiring", "wearehiring", "vacancy", "vacancies", "career",
    "careers", "fulltime", "parttime", "remote", "remotejob", "remotejobs", "remotework",
    "internship", "intern", "contract", "freelance", "relocation", "招聘", "远程", "全职", "兼职",
}
HASHTAG_RE = re.compile(r"#([\w\u4e00-\u9fff]+)")
SEEKER_RE = re.compile(
    r"(\b(i'?m|i am|im)\s+(currently\s+)?(looking|searching|seeking)\b"
    r"|\bopen to work\b|#opentowork"
    r"|\blooking for (a |an )?(new |remote |part[- ]time |full[- ]time )?(job|work|employment)s?\b"
    r"|求职意向|本人求职|寻找工作)",
    re.I)
# Channel ads selling job-posting services, e.g. "Post A Job! Join our Talent Pool".
# They link to job pages, so they need a penalty big enough to cancel a job link.
PROMO_RE = re.compile(
    r"(\bpost (a|your) jobs?\b|\badvertise (a|your) jobs?\b|\bhire with us\b"
    r"|\btalent pool\b|\bjobs newsletter\b|\bapply to jobs for you\b)",
    re.I)
JOB_LINK_RE = re.compile(
    r"(/(?:remote-)?jobs?/|/careers?/|jobdetail|/job\?|/vacanc|/positions?/|/opening|/apply|"
    r"greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|smartrecruiters\.com|myworkdayjobs\.com|"
    r"bamboohr\.com|recruitee\.com|teamtailor\.com|breezy\.hr|jobvite\.com|linkedin\.com/jobs|"
    r"remoteok\.com|weworkremotely\.com|wellfound\.com|indeed\.|jobstreet\.|glassdoor\.|"
    r"jobstash\.xyz|cryptojobslist\.com|web3\.career|cryptocurrencyjobs\.co|remoteyeah\.com)",
    re.I)
TITLE_WORDS = [
    "manager", "engineer", "analyst", "specialist", "associate", "lead", "officer", "intern",
    "designer", "architect", "assistant", "executive", "director", "developer", "counsel",
    "agent", "consultant", "coordinator", "administrator", "accountant", "recruiter",
    "representative", "head of", "vp", "principal", "scientist", "researcher", "operator",
    "advisor", "strategist", "writer", "editor", "tester", "nurse", "technician",
]
EN_STRONG_RE = _word_re(EN_STRONG)
TITLE_RE = _word_re(TITLE_WORDS)
EN_WEAK_RE = _word_re(EN_WEAK)
MS_RE = _word_re(MS_MARKERS)
ES_RE = _word_re(ES_MARKERS)

STRONG_POINTS, WEAK_POINTS, PAY_POINTS = 3, 1, 1
THRESHOLD, MIN_CHARS = 3, 20  # 20, not 40: aggregator listings can be very short


def detect_language(text: str) -> str:
    """en, zh, ms (Malay/Indonesian), es (Spanish), ru (Cyrillic), ar (Arabic/Persian) or other.
    A rough check by script and common words: good enough to sort channels, not perfect."""
    cjk = len(CJK_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    cyr = len(CYRILLIC_RE.findall(text))
    arab = len(ARABIC_RE.findall(text))
    total = cjk + latin + cyr + arab
    if total == 0:
        return "other"
    if cjk / total >= 0.2:
        return "zh"
    if cyr / total >= 0.3:
        return "ru"
    if arab / total >= 0.3:
        return "ar"
    malay_hits = {m.lower() for m in MS_RE.findall(text)}
    spanish_hits = {m.lower() for m in ES_RE.findall(text)}
    if len(spanish_hits) >= 4 and len(spanish_hits) > len(malay_hits):
        return "es"
    return "ms" if len(malay_hits) >= 3 else "en"


def job_score(text: str, links=None) -> tuple[int, list[str]]:
    """Score how much a message looks like a job post.

    Two kinds of evidence:
      - ad wording: "hiring", "apply now", "招聘" ... (keyword lists above)
      - listing structure, used by aggregator channels that never say "hiring":
          labelled fields   "Company: X  Title: Y  Location: Z"   (2+ labels)
          job emojis        "👤 Risk Manager 📍 Dubai 💼 OKX"     (2+ emojis)
          a job-page link   remoteok.com/remote-jobs/..., greenhouse.io, /careers/
          job hashtags      "#remote #fulltime #itjob"                 (2+ tags)
          a job-title word  manager, analyst, engineer, intern ...
    `links` adds hidden links/buttons that aren't in the text itself.
    """
    all_links = links_in_text(text) + list(links or [])
    plain = URL_RE.sub(" ", text)  # words inside links (e.g. "jobs.example.com") shouldn't count
    found_strong = {m.lower() for m in EN_STRONG_RE.findall(plain)} | {w for w in ZH_STRONG if w in plain}
    found_weak = {m.lower() for m in EN_WEAK_RE.findall(plain)} | {w for w in ZH_WEAK if w in plain}
    found_weak -= found_strong
    score = STRONG_POINTS * len(found_strong) + WEAK_POINTS * len(found_weak)
    keywords = sorted(found_strong) + sorted(found_weak)

    if PAY_RE.search(plain):
        score += PAY_POINTS
        keywords.append("<pay>")
    labels = {m.lower() for m in FIELD_LABEL_RE.findall(plain)}
    if len(labels) >= 2:
        score += STRONG_POINTS
        keywords.append("<fields>")
    emojis = {e for e in JOB_EMOJIS if e in plain}
    # needs a role/company emoji (👤 or 💼): 📍 + 🕒 alone is usually an event, 💰 + 🌍 a promo
    if len(emojis) >= 2 and emojis & ROLE_EMOJIS:
        score += STRONG_POINTS
        keywords.append("<job emojis>")
    tags = {t.lower() for t in HASHTAG_RE.findall(plain)} & JOB_HASHTAGS
    if len(tags) >= 2:
        score += STRONG_POINTS
        keywords.append("<job hashtags>")
    if any(JOB_LINK_RE.search(u) for u in all_links):
        score += STRONG_POINTS
        keywords.append("<job link>")
    if TITLE_RE.search(plain):
        score += WEAK_POINTS
        keywords.append("<title>")
    if SEEKER_RE.search(plain):  # someone looking FOR a job, not offering one
        score -= STRONG_POINTS
        keywords.append("<seeker>")
    promo_hits = {m[0].lower() for m in PROMO_RE.findall(plain)}
    if promo_hits:  # an ad for a job board's services, not a job
        score -= 2 * STRONG_POINTS * len(promo_hits)
        keywords.append("<job-board ad>")
    return score, keywords


def is_job_candidate(text: str, score: int) -> bool:
    return score >= THRESHOLD and len(text.strip()) >= MIN_CHARS


def links_in_text(text: str) -> list[str]:
    return [u.rstrip(".,;:!?)") for u in URL_RE.findall(text)]
