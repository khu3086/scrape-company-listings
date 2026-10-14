"""ATS adapters.

Each adapter exposes `fetch(slug) -> list[dict]` returning jobs normalized to:
    {"id": str, "title": str, "location": str, "remote": bool, "url": str}

`fetch` returns [] on any error / unknown slug so discovery and scraping can
treat "no jobs" and "broken" identically. The registry of adapters is `ADAPTERS`
(ordered for discovery probing).
"""
from __future__ import annotations

import html
import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

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


def _post_json(url, payload):
    try:
        r = requests.post(url, headers={**HEADERS, "Content-Type": "application/json",
                                        "Accept": "application/json"},
                          json=payload, timeout=TIMEOUT)
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


# --- Workday ----------------------------------------------------------------
def workday(cfg):
    """Workday CXS endpoint. cfg needs: tenant, dc (wd1/wd3/wd5/...), site.

    Paginates by offset; normalizes the externalPath into an apply URL.
    """
    tenant, dc, site = cfg.get("tenant"), cfg.get("dc"), cfg.get("site")
    if not (tenant and dc and site):
        return []
    base = f"https://{tenant}.{dc}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    out, offset, total = [], 0, None
    while offset < 3000:  # safety cap
        data = _post_json(api, {"appliedFacets": {}, "limit": 20,
                                "offset": offset, "searchText": ""})
        if not isinstance(data, dict):
            break
        if total is None:  # Workday only reports the real total on page 1
            total = data.get("total") or 0
        postings = data.get("jobPostings") or []
        if not postings:
            break
        for j in postings:
            path = _s(j.get("externalPath"))
            loc = _s(j.get("locationsText"))
            # Multi-location postings only say "3 Locations"; the path carries the
            # primary one ("/job/Bengaluru-India/..."), so surface that for filtering.
            m = re.match(r"(\d+) Locations$", loc)
            pm = re.match(r"/job/([^/]+)/", path)
            if m and pm:
                loc = f"{pm.group(1).replace('-', ' ')} (+{int(m.group(1)) - 1} more)"
            out.append({
                "id": _s(j.get("bulletFields", [None])[0] or path),
                "title": _s(j.get("title")),
                "location": loc,
                "remote": "remote" in loc.lower(),
                "url": f"{base}{path}" if path else base,
            })
        offset += 20
        if offset >= total:
            break
    return out


# --- Eightfold ---------------------------------------------------------------
def eightfold(cfg):
    """Eightfold PCS endpoint (e.g. Netflix). cfg needs: host, domain.

    Pages 10 at a time (Eightfold's per-request cap).
    """
    host, domain = cfg.get("host"), cfg.get("domain")
    if not (host and domain):
        return []
    out, start, total = [], 0, None
    while start < 3000:  # safety cap
        url = f"https://{host}/api/apply/v2/jobs?domain={domain}&start={start}&num=10"
        for attempt in range(3):  # Eightfold intermittently fails mid-pagination
            data = _get_json(url)
            if isinstance(data, dict):
                break
            time.sleep(2 * (attempt + 1))
        if not isinstance(data, dict):
            break
        if total is None:
            total = data.get("count") or 0
        postings = data.get("positions") or []
        if not postings:
            break
        for j in postings:
            locs = j.get("locations") or [j.get("location")]
            loc = "; ".join(_s(x) for x in locs if x)
            out.append({
                "id": _s(j.get("ats_job_id") or j.get("id")),
                "title": _s(j.get("name")),
                "location": loc,
                "remote": _s(j.get("work_location_option")) == "remote" or "remote" in loc.lower(),
                "url": _s(j.get("canonicalPositionUrl")) or f"https://{host}/careers/job/{j.get('id')}",
            })
        start += 10
        if start >= total:
            break
    return out


# --- Gem ----------------------------------------------------------------------
def gem(slug):
    data = _get_json(f"https://api.gem.com/job_board/v0/{slug}/job_posts/")
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        loc = _s((j.get("location") or {}).get("name"))
        out.append({
            "id": _s(j.get("id")),
            "title": _s(j.get("title")),
            "location": loc,
            "remote": _s(j.get("location_type")) == "remote" or "remote" in loc.lower(),
            "url": _s(j.get("absolute_url")),
        })
    return out


# --- Rippling -----------------------------------------------------------------
def rippling(slug):
    data = _get_json(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs")
    if not isinstance(data, list):
        return []
    by_id = {}  # a job posted in several locations is listed once per location
    for j in data:
        jid = _s(j.get("uuid"))
        loc = _s((j.get("workLocation") or {}).get("label"))
        if jid in by_id:
            if loc and loc not in by_id[jid]["location"]:
                by_id[jid]["location"] += f"; {loc}"
                by_id[jid]["remote"] |= "remote" in loc.lower()
            continue
        by_id[jid] = {
            "id": jid,
            "title": _s(j.get("name")),
            "location": loc,
            "remote": "remote" in loc.lower(),
            "url": _s(j.get("url")),
        }
    return list(by_id.values())


# --- BambooHR -----------------------------------------------------------------
def bamboohr(slug):
    data = _get_json(f"https://{slug}.bamboohr.com/careers/list")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("result") or []:
        l = j.get("location") or {}
        loc = ", ".join(_s(x) for x in (l.get("city"), l.get("state")) if x)
        remote = bool(j.get("isRemote")) or "remote" in loc.lower()
        out.append({
            "id": _s(j.get("id")),
            "title": _s(j.get("jobOpeningName")),
            "location": loc,
            "remote": remote,
            "url": f"https://{slug}.bamboohr.com/careers/{j.get('id')}",
        })
    return out


# --- YC Work at a Startup -------------------------------------------------------
_YC_PAGE_RE = re.compile(r'data-page="([^"]+)"')


def yc(slug):
    """Jobs on ycombinator.com/companies/<slug>/jobs (props embedded in the page)."""
    try:
        r = requests.get(f"https://www.ycombinator.com/companies/{slug}/jobs",
                         headers=HEADERS, timeout=TIMEOUT)
        m = _YC_PAGE_RE.search(r.text) if r.status_code == 200 else None
        if not m:
            return []
        props = json.loads(html.unescape(m.group(1))).get("props") or {}
    except (requests.RequestException, ValueError):
        return []
    out = []
    for j in props.get("jobPostings") or []:
        loc = _s(j.get("location"))
        out.append({
            "id": _s(j.get("id")),
            "title": _s(j.get("title")),
            "location": loc,
            "remote": "remote" in loc.lower(),
            "url": f"https://www.ycombinator.com{_s(j.get('url'))}",
        })
    return out


# --- Breezy HR ------------------------------------------------------------------
def breezy(slug):
    data = _get_json(f"https://{slug}.breezy.hr/json")
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        l = j.get("location") or {}
        loc = _s(l.get("name"))
        out.append({
            "id": _s(j.get("id")),
            "title": _s(j.get("name")),
            "location": loc,
            "remote": bool(l.get("is_remote")) or "remote" in loc.lower(),
            "url": _s(j.get("url")),
        })
    return out


# --- Dover -----------------------------------------------------------------------
def dover(slug):
    """slug = the careers-page id in app.dover.io/<company>/careers/<id>."""
    data = _get_json(f"https://app.dover.io/api/v1/careers-page/{slug}/jobs")
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("results") or []:
        if not j.get("is_published", True) or j.get("is_sample"):
            continue
        loc = "; ".join(_s(l.get("name")) for l in j.get("locations") or [] if l.get("name"))
        remote = _s(j.get("workplace_type")) == "REMOTE" or "remote" in loc.lower()
        out.append({
            "id": _s(j.get("id")),
            "title": _s(j.get("title")),
            "location": loc,
            "remote": remote,
            "url": f"https://app.dover.io/apply/{slug}/{j.get('id')}",
        })
    return out


# --- RSS / Atom feeds (Teamtailor jobs.rss, Homerun, Oxide) --------------------------
_ATOM = "{http://www.w3.org/2005/Atom}"


def feed(cfg):
    """cfg needs: url. Optional: location (used when an item carries none)."""
    url, default_loc = cfg.get("url"), _s(cfg.get("location"))
    if not url:
        return []
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError):
        return []
    out = []
    for it in root.iter():
        if it.tag == "item":  # RSS 2.0
            title, link = _s(it.findtext("title")), _s(it.findtext("link"))
            jid = _s(it.findtext("guid")) or link
            loc = ", ".join(_s(e.text) for e in it.iter()
                            if e.tag.endswith("}city") or e.tag.endswith("}country") and _s(e.text))
            remote = _s(it.findtext("remoteStatus")) in ("fully", "remote")
        elif it.tag == _ATOM + "entry":
            title = _s(it.findtext(_ATOM + "title"))
            le = it.find(_ATOM + "link")
            link = _s(le.get("href")) if le is not None else ""
            jid = _s(it.findtext(_ATOM + "id")) or link
            loc = _s(it.findtext(_ATOM + "location"))
            remote = False
        else:
            continue
        loc = loc or default_loc
        out.append({
            "id": jid,
            "title": title,
            "location": loc,
            "remote": remote or "remote" in loc.lower(),
            "url": link,
        })
    return out


# --- Google Careers ---------------------------------------------------------------
_GOOGLE_DATA_RE = re.compile(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(\[.*?\]), sideChannel", re.S)


def google(cfg):
    """Google's careers search page embeds its results as JSON (20 per page).

    cfg needs: queries, a list of query strings such as "location=India".
    """
    base = "https://www.google.com/about/careers/applications/jobs/results/"
    headers = {**HEADERS, "Accept-Language": "en-US"}
    by_id = {}
    for q in cfg.get("queries") or []:
        page, total = 1, None
        while page <= 100:  # safety cap
            try:
                r = requests.get(f"{base}?{q}&page={page}", headers=headers, timeout=TIMEOUT)
                m = _GOOGLE_DATA_RE.search(r.text) if r.status_code == 200 else None
                data = json.loads(m.group(1)) if m else None
            except (requests.RequestException, ValueError):
                data = None
            if not data or not data[0]:
                break
            total = data[2] if total is None else total
            for j in data[0]:
                locs = "; ".join(_s(l[0]) for l in (j[9] or []) if l and l[0])
                by_id[_s(j[0])] = {
                    "id": _s(j[0]),
                    "title": _s(j[1]),
                    "location": locs,
                    "remote": "remote" in locs.lower(),
                    "url": f"{base}{j[0]}",
                }
            if page * 20 >= (total or 0):
                break
            page += 1
    return list(by_id.values())


# --- Generic JSON endpoint -------------------------------------------------------
def _dig(obj, path):
    for part in filter(None, (path or "").split(".")):
        if isinstance(obj, list):
            obj = obj[int(part)] if part.isdigit() and int(part) < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def json_list(cfg):
    """A custom careers API that returns a JSON list of jobs.

    cfg: url, items (dot path to the list; "" = root), id, title (field names),
    location (field name, optional), default_location, link (template using
    {field} placeholders, or a field name holding the URL).
    """
    data = _get_json(cfg.get("url", ""))
    items = _dig(data, cfg.get("items", ""))
    if not isinstance(items, list):
        return []
    out = []
    for j in items:
        if not isinstance(j, dict):
            continue
        loc = j.get(cfg["location"]) if cfg.get("location") else None
        loc = "; ".join(map(_s, loc)) if isinstance(loc, list) else _s(loc)
        loc = loc or _s(cfg.get("default_location"))
        link = cfg.get("link", "")
        url = link.format(**{k: _s(v) for k, v in j.items()}) if "{" in link else _s(j.get(link))
        out.append({
            "id": _s(j.get(cfg.get("id", "id"))),
            "title": _s(j.get(cfg.get("title", "title"))),
            "location": loc,
            "remote": "remote" in loc.lower(),
            "url": url or cfg.get("url", ""),
        })
    return out


# --- Generic careers page (static or headless-rendered) ------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_RE = re.compile(r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
_GENERIC_TITLE_RE = re.compile(r"^(view( job| details| and apply)?|apply( now)?|learn more|read more|details)$", re.I)


def _render(url, wait_ms, scroll=0):
    """Load a page in headless Chromium and return its HTML ("" if unavailable)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"))
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:  # noqa: BLE001 - long-polling pages never go idle
                pass
            page.wait_for_timeout(wait_ms)
            for _ in range(scroll):  # lazy-loading lists (e.g. Meta) grow as you scroll
                page.mouse.wheel(0, 20000)
                page.wait_for_timeout(1500)
            content = page.content()
            browser.close()
            return content
    except Exception:  # noqa: BLE001 - treat any browser failure as "no jobs"
        return ""


def _clean(fragment):
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("\n", fragment))).strip()


def page(cfg):
    """Scrape a careers page that has no API. With `pages: N`, `url` must contain
    "{page}" and pages 1..N are read until one adds no new jobs."""
    if not cfg.get("pages"):
        return _page_once(cfg)
    out, ids = [], set()
    for n in range(1, int(cfg["pages"]) + 1):
        batch = [j for j in _page_once({**cfg, "url": cfg["url"].format(page=n)}) if j["id"] not in ids]
        if not batch:
            break
        ids.update(j["id"] for j in batch)
        out += batch
    return out


def _page_once(cfg):
    """One careers page that has no API.

    cfg: url; render (bool, use headless Chromium); default_location;
    then one of:
      link    regex an href must match; the anchor text's first line is the
              title (or the URL slug, if the text is just "View job"), and its
              later lines are searched for a location;
      heading regex a heading's text must match, for pages that list roles
              without links (tags: which elements count, default h1-h6/strong/b);
      block   regex whose first group is one job card's HTML; its first text
              line is the title, the second the location (e.g. Darwinbox).
    Optional: strip (regex removed from titles), scroll (times to scroll a
    rendered page), wait_ms.
    """
    url = cfg.get("url")
    if not url:
        return []
    if cfg.get("render"):
        body = _render(url, int(cfg.get("wait_ms", 2000)), int(cfg.get("scroll", 0)))
    else:
        try:
            r = requests.get(url, headers={**HEADERS, "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")}, timeout=TIMEOUT)
            body = r.text if r.status_code == 200 else ""
        except requests.RequestException:
            body = ""
    if not body:
        return []
    default_loc = _s(cfg.get("default_location"))
    strip = re.compile(cfg["strip"]) if cfg.get("strip") else None
    out, seen = [], set()

    def add(jid, title, loc, link):
        title = strip.sub("", title).strip() if strip else title
        if not title or jid in seen:
            return
        seen.add(jid)
        loc = loc or default_loc
        out.append({"id": jid, "title": title, "location": loc,
                    "remote": "remote" in loc.lower(), "url": link})

    if cfg.get("link"):
        pat = re.compile(cfg["link"])
        for href, inner in _ANCHOR_RE.findall(body):
            full = urljoin(url, html.unescape(href))
            if not pat.search(full):
                continue
            lines = [l.strip() for l in html.unescape(_TAG_RE.sub("\n", inner)).split("\n") if l.strip()]
            if not lines:
                continue
            if _GENERIC_TITLE_RE.match(lines[0]):
                slug = [x for x in full.split("#")[0].rstrip("/").split("/") if x][-1]
                lines[0] = re.sub(r"[-_]+", " ", re.sub(r"\?.*$", "", slug)).strip().title()
            loc = next((l for l in lines[1:] if re.search(
                r"remote|india|bengaluru|bangalore|hyderabad|mumbai|delhi|gurgaon|gurugram|pune|chennai|noida|"
                r"united states|usa|, [A-Z]{2}\b|london|singapore", l, re.I)), "")
            add(full.split("#")[0], lines[0], loc, full)
    elif cfg.get("heading"):
        pat = re.compile(cfg["heading"], re.I)
        tags = cfg.get("tags", r"h[1-6]|strong|b")
        for _, inner in re.findall(rf"<({tags})\b[^>]*>(.*?)</\1>", body, re.S | re.I):
            text = _clean(inner)
            if 3 <= len(text) <= 90 and pat.search(text):
                add(f"{url}#{text.lower()}", text, "", url)
    elif cfg.get("block"):
        for chunk in re.findall(cfg["block"], body, re.S):
            lines = [l.strip() for l in html.unescape(_TAG_RE.sub("\n", chunk)).split("\n") if l.strip()]
            if lines:
                add(f"{url}#{lines[0].lower()}|{(lines[1] if len(lines) > 1 else '').lower()}",
                    lines[0], lines[1] if len(lines) > 1 else "", url)
    return out


# Slug-probeable ATSes, ordered for discovery (most common first).
ADAPTERS = {
    "greenhouse": greenhouse,
    "ashby": ashby,
    "lever": lever,
    "workable": workable,
    "smartrecruiters": smartrecruiters,
}

# Slug-based but not auto-probed by discover.py (hand-registered in companies.yaml).
EXTRA_ADAPTERS = {
    "gem": gem,
    "rippling": rippling,
    "bamboohr": bamboohr,
    "yc": yc,
    "breezy": breezy,
    "dover": dover,
}


def fetch(cfg):
    """Fetch normalized jobs for one company config dict.

    cfg = {"ats": "greenhouse"|..., "slug": "..."}  for slug-based ATSes, or
    cfg = {"ats": "workday", "tenant": ..., "dc": ..., "site": ...}, or
    cfg = {"ats": "eightfold", "host": ..., "domain": ...}.
    """
    ats_name = cfg.get("ats")
    if ats_name == "workday":
        return workday(cfg)
    if ats_name == "eightfold":
        return eightfold(cfg)
    if ats_name == "feed":
        return feed(cfg)
    if ats_name == "google":
        return google(cfg)
    if ats_name == "json":
        return json_list(cfg)
    if ats_name == "page":
        return page(cfg)
    adapter = ADAPTERS.get(ats_name) or EXTRA_ADAPTERS.get(ats_name)
    if adapter is None:
        return []
    return adapter(cfg.get("slug"))
