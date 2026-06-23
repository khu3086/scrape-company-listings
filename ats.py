"""ATS adapters.

Each adapter exposes `fetch(slug) -> list[dict]` returning jobs normalized to:
    {"id": str, "title": str, "location": str, "remote": bool, "url": str}

`fetch` returns [] on any error / unknown slug so discovery and scraping can
treat "no jobs" and "broken" identically. The registry of adapters is `ADAPTERS`
(ordered for discovery probing).
"""
from __future__ import annotations

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-watcher/1.0)"}
TIMEOUT = 20


def _get_json(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        return None


def _s(v):
    return "" if v is None else str(v).strip()


# --- Greenhouse -------------------------------------------------------------
def greenhouse(slug):
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("jobs", []) or []:
        loc = _s((j.get("location") or {}).get("name"))
        out.append({
            "id": _s(j.get("id")),
            "title": _s(j.get("title")),
            "location": loc,
            "remote": "remote" in loc.lower(),
            "url": _s(j.get("absolute_url")),
        })
    return out


# --- Ashby ------------------------------------------------------------------
def ashby(slug):
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("jobs", []) or []:
        if j.get("isListed") is False:
            continue
        loc = _s(j.get("location"))
        out.append({
            "id": _s(j.get("id")),
            "title": _s(j.get("title")),
            "location": loc,
            "remote": bool(j.get("isRemote")) or "remote" in loc.lower(),
            "url": _s(j.get("jobUrl") or j.get("applyUrl")),
        })
    return out


# --- Lever ------------------------------------------------------------------
def lever(slug):
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        cats = j.get("categories") or {}
        loc = _s(cats.get("location"))
        wt = _s(j.get("workplaceType")).lower()
        out.append({
            "id": _s(j.get("id")),
            "title": _s(j.get("text")),
            "location": loc,
            "remote": wt == "remote" or "remote" in loc.lower(),
            "url": _s(j.get("hostedUrl") or j.get("applyUrl")),
        })
    return out


# --- Workable ---------------------------------------------------------------
def workable(slug):
    data = _get_json(
        f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    )
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("jobs", []) or []:
        parts = [j.get("city"), j.get("state"), j.get("country")]
        loc = ", ".join(_s(p) for p in parts if _s(p))
        out.append({
            "id": _s(j.get("shortcode") or j.get("id")),
            "title": _s(j.get("title")),
            "location": loc,
            "remote": bool(j.get("remote")) or "remote" in loc.lower(),
            "url": _s(j.get("url") or j.get("application_url")),
        })
    return out


# --- SmartRecruiters --------------------------------------------------------
def smartrecruiters(slug):
    data = _get_json(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"
    )
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("content", []) or []:
        loc = j.get("location") or {}
        parts = [loc.get("city"), loc.get("region"), loc.get("country")]
        loc_str = ", ".join(_s(p) for p in parts if _s(p))
        jid = _s(j.get("id"))
        out.append({
            "id": jid,
            "title": _s(j.get("name")),
            "location": loc_str,
            "remote": bool(loc.get("remote")) or "remote" in loc_str.lower(),
            "url": f"https://jobs.smartrecruiters.com/{slug}/{jid}",
        })
    return out


# Ordered for discovery probing (most common first).
ADAPTERS = {
    "greenhouse": greenhouse,
    "ashby": ashby,
    "lever": lever,
    "workable": workable,
    "smartrecruiters": smartrecruiters,
}


def fetch(ats_name, slug):
    """Fetch normalized jobs for a given ATS + slug."""
    adapter = ADAPTERS.get(ats_name)
    if adapter is None:
        return []
    return adapter(slug)
