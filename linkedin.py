"""Best-effort LinkedIn jobs lookup for companies with no scrapable ATS.

Uses LinkedIn's public, no-auth "guest" jobs endpoint (the same one that powers
the logged-out jobs search). For each company we search by name + location and
keep only cards whose company actually matches the target (a keyword search for
"Groq" otherwise returns unrelated jobs that merely mention it).

IMPORTANT — read before using:
  * This is against LinkedIn's Terms of Service. Use it only for low-volume,
    personal job tracking, at your own risk.
  * LinkedIn blocks datacenter IPs hard (instant 999/429), so this only works
    from a residential connection (your Mac) — NOT from the GitHub Actions cron.
  * It is inherently fragile: LinkedIn can change the HTML or start rate-limiting
    at any time. On a 429/999 we back off and stop rather than hammer.

Returned jobs use the same normalized shape as ats.py:
    {"id", "title", "location", "remote", "url", "company"}
"""
from __future__ import annotations

import html
import re
import time

import requests

GUEST_URL = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/"
             "search?keywords={kw}&location={loc}&start={start}")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
}
TIMEOUT = 20

# Signals we've been rate-limited / blocked.
_BLOCKED_STATUS = {429, 999, 403}


class LinkedInBlocked(Exception):
    """Raised when LinkedIn rate-limits us, so the caller can stop early."""


def _norm(name):
    """Normalize a company name for fuzzy matching: lowercase alnum, no suffixes."""
    base = re.sub(r"\.(ai|sh|dev|io|com|app|co)$", "", name.strip(), flags=re.I)
    s = re.sub(r"[^a-z0-9]", "", base.lower())
    for suf in ("technologies", "technology", "labs", "lab", "inc", "ai",
                "global", "india", "systems"):
        if s.endswith(suf) and len(s) > len(suf) + 2:
            s = s[: -len(suf)]
    return s


def _company_matches(target, card_company):
    a, b = _norm(target), _norm(card_company)
    if not a or not b:
        return False
    return a == b or a in b or b in a


# --- card parsing -----------------------------------------------------------
_URN_RE = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
_TITLE_RE = re.compile(r'base-search-card__title">\s*([^<]+)')
_COMPANY_RE = re.compile(r'base-search-card__subtitle">\s*(?:<a[^>]*>)?\s*([^<]+)')
_LOCATION_RE = re.compile(r'job-search-card__location">\s*([^<]+)')
_LINK_RE = re.compile(r'base-card__full-link"[^>]*href="([^"?]+)')


def _parse_cards(page_html):
    """Split the guest HTML into per-card chunks and extract fields."""
    chunks = re.split(r'(?=data-entity-urn="urn:li:jobPosting:)', page_html)
    out = []
    for c in chunks:
        urn = _URN_RE.search(c)
        title = _TITLE_RE.search(c)
        if not (urn and title):
            continue
        company = _COMPANY_RE.search(c)
        loc = _LOCATION_RE.search(c)
        link = _LINK_RE.search(c)
        out.append({
            "id": urn.group(1),
            "title": html.unescape(title.group(1).strip()),
            "company_raw": html.unescape(company.group(1).strip()) if company else "",
            "location": html.unescape(loc.group(1).strip()) if loc else "",
            "url": link.group(1).strip() if link else
                   f"https://www.linkedin.com/jobs/view/{urn.group(1)}",
        })
    return out


def search_company(company, location="India", pages=1, pause=1.5, session=None):
    """Return normalized jobs for `company` from LinkedIn guest search.

    Only cards whose company matches `company` are kept. Raises LinkedInBlocked
    on a rate-limit response so a bulk caller can stop.
    """
    sess = session or requests.Session()
    results, seen = [], set()
    for page in range(pages):
        url = GUEST_URL.format(kw=requests.utils.quote(company),
                               loc=requests.utils.quote(location),
                               start=page * 10)
        try:
            r = sess.get(url, headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException:
            break
        if r.status_code in _BLOCKED_STATUS:
            raise LinkedInBlocked(f"HTTP {r.status_code} for {company!r}")
        if r.status_code != 200 or not r.text.strip():
            break
        cards = _parse_cards(r.text)
        if not cards:
            break
        for c in cards:
            if c["id"] in seen or not _company_matches(company, c["company_raw"]):
                continue
            seen.add(c["id"])
            loc_l = c["location"].lower()
            results.append({
                "id": f"li:{c['id']}",
                "title": c["title"],
                "location": c["location"],
                "remote": "remote" in loc_l,
                "url": c["url"],
                "company": company,
            })
        time.sleep(pause)  # be gentle; avoid tripping rate limits
    return results
