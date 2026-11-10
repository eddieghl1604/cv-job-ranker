"""Read the text of a job link, politely and briefly. Failures are normal and just noted."""
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .detect import JOB_LINK_RE

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; JobScreener/0.1; personal job search)"}
MAX_BYTES = 2_000_000


def _domain(url: str) -> str:
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def pick_link(links: list[str], skip_domains: list[str]) -> str | None:
    """Prefer a link that looks like a job page; otherwise the first usable link."""
    usable = [l for l in links if not any(_domain(l) == d or _domain(l).endswith("." + d) for d in skip_domains)]
    for l in usable:
        if JOB_LINK_RE.search(l):
            return l
    return usable[0] if usable else None


def read_page(url: str, max_chars: int) -> tuple[str | None, str]:
    """Returns (text or None, status). Status is 'ok' or a short reason."""
    if "://" not in url:
        url = "https://" + url
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, stream=True, allow_redirects=True)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        if "html" not in r.headers.get("Content-Type", "html").lower():
            return None, "not a web page"
        raw = r.raw.read(MAX_BYTES, decode_content=True)
        r.close()
    except requests.RequestException as e:
        return None, type(e).__name__
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "svg", "form"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    if len(text) < 200:
        return None, "page has little text (may need JavaScript or a login)"
    return text[:max_chars], "ok"
