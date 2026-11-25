"""Contract / freelance openings on AI-training platforms.

These platforms aren't company career boards: they list paid expert contracts
(Mercor, micro1, Ethos, ...). Each fetcher returns the common job shape plus a
`pay` string, and only software / ML / data / AI roles are kept. scrape.py
emails them in their own "Contract openings" section.

Outlier is not covered: its listings need an account.
"""
from __future__ import annotations

import html
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36"
TIMEOUT = 90  # Ethos and Handshake are slow (40-80s)

# Software, ML, data, programming and AI roles. Bare "engineer" and "AI" are left
# out on purpose: they pull in mechanical/civil engineers and every "for AI
# Training" gig (economists, voice actors, ...).
ROLE_RE = re.compile(
    r"\b("
    r"software|swe|sde|swe-bench|developers?|programmers?|programming|coding|coders?|code|"
    r"python|java|javascript|typescript|c\+\+|rust|golang|kotlin|swift|"
    r"backend|back[- ]?end|frontend|front[- ]?end|full[- ]?stack|web develop\w*|mobile develop\w*|"
    r"ios|android|devops|sre|site reliability|mlops|platform engineer\w*|infrastructure engineer\w*|"
    r"cloud|database|sql|kernel|algorithms?|competitive programming|computer science|"
    r"machine learning|ml|deep learning|llms?|nlp|computer vision|reinforcement learning|"
    r"ai engineer\w*|ai develop\w*|ai research\w*|applied ai|ai/ml|ml/ai|ai red team\w*|"
    r"ai agents?|agentic|agent engineer|prompt engineer\w*|applied engineer|"
    r"data scien\w*|data engineer\w*|data analy\w*|kaggle|"
    r"cyber ?security|cyber|security engineer\w*|red team\w*|"
    # GPU work, AI safety / governance, formal-proof languages, competitive programming, PM
    r"cuda|gpu|jailbreak\w*|prompt[- ]injection|ai safety|responsible ai|ai governance|data privacy|"
    r"coq|isabelle|mathlib|lean ?4|codeforces|leetcode|product manag\w*"
    r")\b",
    re.I,
)
EXCLUDE_RE = re.compile(
    r"\b("
    # medical billing coders
    r"(medical|inpatient|outpatient|clinical|records|certified)\b.*\bcod(er|ing)|cod(er|ing)\b.*\b(ccs|cpc|icd)|"
    # users of software rather than builders of it
    r"software (survey|workflows?|strategy)|productivity software|"
    # non-software engineering disciplines that also mention data/ML
    r"(industrial|mechanical|civil|electrical|chemical|structural|manufacturing|process) engineers?|"
    r"cbrne"
    r")\b",
    re.I,
)


# Gigs that need fluency in another language ("Bilingual Spanish Generalist — AI Safety").
# Hindi is kept: Khushi speaks it.
LANGUAGE_RE = re.compile(r"^(?!.*\bhindi\b).*\b(bilingual|spanish|chinese|mandarin|cantonese|japanese|korean|german|french|portuguese|italian|arabic|russian|turkish|vietnamese|thai|indonesian|malay|tagalog|filipino|dutch|polish|hebrew|swedish|norwegian|danish|finnish|greek|czech(?! republic)|romanian|ukrainian|hungarian|farsi|persian|swahili|bangla|bengali|urdu|tamil|telugu|kannada|malayalam|marathi|gujarati|punjabi|native speaker|linguist\w*)\b", re.I)


def is_tech_role(title):
    t = title or ""
    return bool(ROLE_RE.search(t)) and not EXCLUDE_RE.search(t) and not LANGUAGE_RE.search(t)


def _get(url, **kw):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _post(url, payload, headers=None):
    r = requests.post(url, json=payload, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})},
                      timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _rate(lo, hi):
    if lo and hi and lo != hi:
        return f"${lo:g}-{hi:g}/hr"
    if lo or hi:
        return f"${(lo or hi):g}/hr"
    return ""


def _job(company, id_, title, url, location="", pay=""):
    return {"company": company, "id": str(id_), "title": (title or "").strip(), "url": url,
            "location": location, "remote": True, "pay": pay}


def mercor():
    out = []
    for x in _get("https://aws.api.mercor.com/work/listings-explore-page").json()["listings"]:
        if x.get("status") != "active" or x.get("isPrivate"):
            continue
        locs = x.get("eligibleLocation") or []
        where = ", ".join(locs[:5]) + (f" +{len(locs) - 5} more" if len(locs) > 5 else "")
        out.append(_job("Mercor", x["listingId"], x["title"], f"https://work.mercor.com/jobs/{x['listingId']}",
                        where or x.get("location") or "Remote", _rate(x.get("rateMin"), x.get("rateMax"))))
    return out


# micro1 job id -> title, read from job pages when the API is blocked. scrape.py keeps
# it in state.json so each run only fetches pages for newly posted jobs.
MICRO1_TITLES = {}


def micro1():
    try:
        return micro1_api()
    except requests.HTTPError as e:
        # The API answers 403 to cloud servers such as GitHub Actions runners.
        if e.response is None or e.response.status_code != 403:
            raise
        print("[contracts] micro1 API returned 403; reading the sitemap and job pages instead", file=sys.stderr)
        return micro1_pages()


def micro1_pages():
    ids = sorted(set(re.findall(r"jobs\.micro1\.ai/post/([0-9a-f-]{36})",
                                _get("https://jobs.micro1.ai/sitemap.xml").text)))

    def title(id_):
        """Job title, or "" for micro1's own full-time staff jobs (the API leaves those out)."""
        page = _get(f"https://jobs.micro1.ai/post/{id_}").text
        if re.search(r"full[- ]time", page, re.I) and not re.search(r"\bcontract", page, re.I):
            return ""
        m = re.search(r"<title>(.*?)</title>", page, re.S)
        return html.unescape(re.sub(r"\s*\|\s*Apply on Job\s*$", "", m.group(1))).strip() if m else ""

    missing = [i for i in ids if i not in MICRO1_TITLES]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for id_, fut in [(i, pool.submit(title, i)) for i in missing]:
            try:
                MICRO1_TITLES[id_] = fut.result()
            except requests.RequestException:
                pass  # retried next run
    for stale in set(MICRO1_TITLES) - set(ids):
        del MICRO1_TITLES[stale]
    return [_job("micro1", i, MICRO1_TITLES[i], f"https://jobs.micro1.ai/post/{i}", "Remote")
            for i in ids if MICRO1_TITLES.get(i)]


def micro1_api():
    out, page = [], 1
    while page <= 20:
        data = _post(f"https://prod-api.micro1.ai/api/v1/job/portal?page={page}&limit=100&keyword=",
                     {"action": "get_all_jobs", "filters": {"type": ["EXPERT"]}})
        for x in data["data"]:
            r = x.get("ideal_hourly_rate") or {}
            out.append(_job("micro1", x["job_id"], x["job_name"], f"https://jobs.micro1.ai/post/{x['job_id']}",
                            "Remote", _rate(r.get("min"), r.get("max"))))
        if not data["data"] or len(out) >= data.get("total", 0):
            break
        page += 1
    return out


def ethos():
    # Ethos (askethos.com) publishes through General Catalyst's Getro job board.
    out, page = [], 0
    while page <= 20:
        data = _post("https://api.getro.com/api/v2/collections/222/search/jobs",
                     {"hitsPerPage": 100, "page": page, "query": "", "filters": {"organization.id": [1910559]}},
                     headers={"Origin": "https://jobs.generalcatalyst.com"})["results"]
        for x in data["jobs"]:
            out.append(_job("Ethos", x["id"], x["title"], x["url"], ", ".join(x.get("locations") or [])))
        if not data["jobs"] or len(out) >= data.get("count", 0):
            break
        page += 1
    return out


def dataannotation():
    page = _get("https://www.dataannotation.tech/careers").text
    slugs = sorted(set(re.findall(r'href="/job-board/([a-z0-9-]+)"', page)))
    return [_job("DataAnnotation", s, s.replace("-", " ").title(), f"https://www.dataannotation.tech/job-board/{s}",
                 "Remote") for s in slugs]


def ashby_contracts(company, board):
    """Contract roles only: these boards also carry the company's full-time staff jobs."""
    def run():
        jobs = _get(f"https://api.ashbyhq.com/posting-api/job-board/{board}").json()["jobs"]
        return [_job(company, x["id"], x["title"], x["jobUrl"], x.get("location") or "")
                for x in jobs
                if x.get("isListed", True) and x["title"] != "TEMPLATE"
                and (x.get("employmentType") in ("Contract", "Temporary") or "contract" in x["title"].lower())]
    return run


def greenhouse_contracts(company, board):
    def run():
        jobs = _get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs").json()["jobs"]
        return [_job(company, x["id"], x["title"], x["absolute_url"], (x.get("location") or {}).get("name", ""))
                for x in jobs if "contract" in x["title"].lower()]
    return run


PLATFORMS = {
    "Mercor": mercor,
    "micro1": micro1,
    "Ethos": ethos,
    "DataAnnotation": dataannotation,
    "Terac": ashby_contracts("Terac", "terac"),
    "Handshake AI": ashby_contracts("Handshake AI", "handshake"),
    "Turing": greenhouse_contracts("Turing", "turing"),
    "Patronus AI": greenhouse_contracts("Patronus AI", "patronusaiinc"),
}


def gather():
    """Return (tech-role jobs, platforms that answered, platforms that failed)."""
    jobs, ok, failed = [], [], []
    with ThreadPoolExecutor(max_workers=len(PLATFORMS)) as pool:
        futures = {name: pool.submit(fn) for name, fn in PLATFORMS.items()}
    for name, fut in futures.items():
        try:
            listed = fut.result()
        except Exception as e:  # noqa: BLE001
            print(f"[contracts] {name} failed: {e}", file=sys.stderr)
            failed.append(name)
            continue
        kept = [j for j in listed if is_tech_role(j["title"])]
        print(f"[contracts] {name}: {len(kept)} of {len(listed)} are software/ML/data/AI roles", file=sys.stderr)
        jobs += kept
        ok.append(name)
    return jobs, ok, failed
