"""Recently funded startups (pre-seed to Series C), for reaching out to founders.

Sources, all free:
  - Google News RSS searches for "raises ... seed / Series A ..." (US and India editions)
  - TechCrunch, Crunchbase News, Inc42 and Entrackr RSS feeds
  - SEC Form D filings (US companies must file within 15 days of selling shares).
    They list founders / officers by name, but SEC only serves requests whose
    User-Agent carries a contact email, so this source is on only when the
    SEC_CONTACT_EMAIL environment variable (a GitHub secret in CI) is set.

Each round gets: company, round (or a guess from the amount for Form D), amount in
USD, date, headline + link, India flag, founders (Form D), whether the company is
already on the watch list, and a job board found by probing Ashby / Greenhouse /
Lever. A probed board can belong to a different company with the same name, so
it is shown as a lead, never added to companies.yaml automatically.

State lives in funding.json (committed by CI): rounds from the last 90 days, the
Form D filings already read, and board probes. scrape.py emails rounds that are
new since the last run; board.py shows them all in a "Funded" tab.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "funding.json")
KEEP_DAYS = 90
TIMEOUT = 30
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

ROUNDS = ("pre-seed", "seed", "series a", "series b", "series c")
ROUND_LABEL = {"pre-seed": "Pre-seed", "seed": "Seed", "series a": "Series A",
               "series b": "Series B", "series c": "Series C"}


def _gnews(q, india=False):
    loc = "hl=en-IN&gl=IN&ceid=IN:en" if india else "hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}+when:7d&{loc}"


# (url, india edition?)
FEEDS = [(_gnews(f'"raises" "{r}"', india), india)
         for r in ("pre-seed", "seed round", "seed funding", "Series A", "Series B", "Series C")
         for india in (False, True)] + [
    ("https://techcrunch.com/feed/", False),
    ("https://news.crunchbase.com/feed/", False),
    ("https://inc42.com/feed/", True),
    ("https://entrackr.com/rss", True),
]

VERB = r"(?:raises|raised|secures|secured|bags|bagged|lands|landed|closes|closed|nets|snags|gets|grabs|picks up|scores)"
ROUND_RE = re.compile(r"\b(pre[- ]?seed|seed|series [a-e])\b", re.I)
AMOUNT_RE = re.compile(
    r"(?P<cur>[$€£₹]|rs\.?\s?|inr\s?|usd\s?)\s?(?P<num>\d[\d,]*(?:\.\d+)?)\s?"
    r"(?P<unit>billion|bn|b\b|million|mn|mln|m\b|crore|cr\b|lakh|k\b)?", re.I)
# Words that mark the end of a descriptor before the company name:
# "Agentic AI regulatory startup Reversa raises ..." -> "Reversa".
DESCRIPTOR_RE = re.compile(r"^.*\b(startup|start-up|company|firm|platform|maker|provider|unicorn|"
                           r"developer|specialist|player|venture|app|co\.|protocol|spinout|spin-off|"
                           r"lab|network|marketplace|tool|solution|brand)\s+", re.I)
PREFIX_RE = re.compile(r"^(exclusive|first look|funding|breaking|scoop|report|news|watch)\s*[:|-]\s*", re.I)
TECH_RE = re.compile(r"\b(ai|a\.i\.|agents?|agentic|llms?|genai|software|saas|developer|devtools?|data|cloud|"
                     r"security|cyber|fintech|api|robot\w*|ml|machine learning|automation|infra\w*|platform|"
                     r"compute|gpu|chips?|semiconductor|analytics|observability|database|search|voice|"
                     r"coding|code|open[- ]source|model|inference|quantum|web3|crypto|blockchain|edtech|"
                     r"healthtech|insurtech|legaltech|proptech|deeptech|tech)\b", re.I)
NOT_STARTUP_RE = re.compile(r"\b(fund (i{1,3}|iv|v|\d)|launches .*fund|first close|corpus|vc firm|venture fund|"
                            r"debt fund|ipo|acquires|acquired|merger)\b", re.I)
INDIA_RE = re.compile(r"\b(india|indian|bengaluru|bangalore|mumbai|delhi|gurugram|gurgaon|noida|hyderabad|pune|"
                      r"chennai|kolkata|ahmedabad|crore|lakh|inc42|entrackr|yourstory|economic times|"
                      r"moneycontrol|business standard|livemint|mint|vccircle|the hindu)\b|₹|\brs\.?\s?\d", re.I)

USD_PER = {"$": 1.0, "usd": 1.0, "€": 1.08, "£": 1.27, "₹": 1 / 84, "rs": 1 / 84, "inr": 1 / 84}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _get(url, **kw):
    r = requests.get(url, headers={"User-Agent": UA, **kw.pop("headers", {})}, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# --- parsing headlines ------------------------------------------------------
def amount_usd_m(text):
    """'$11 million' -> 11.0, '₹300 Cr' -> 3.57, '€750k' -> 0.81 (USD millions), or None."""
    for m in AMOUNT_RE.finditer(text or ""):
        cur = m.group("cur").lower().strip().rstrip(".").strip()
        cur = "rs" if cur.startswith("rs") else cur
        try:
            n = float(m.group("num").replace(",", ""))
        except ValueError:
            continue
        unit = (m.group("unit") or "").lower()
        if unit in ("crore", "cr"):
            n *= 10           # 1 crore = 10 million
        elif unit == "lakh":
            n *= 0.1
        elif unit in ("billion", "bn", "b"):
            n *= 1000
        elif unit == "k":
            n /= 1000
        elif unit in ("million", "mn", "mln", "m"):
            pass
        elif n >= 10_000:     # a bare "$500,000"
            n /= 1_000_000
        else:
            continue
        return round(n * USD_PER.get(cur, 1.0), 2)  # n is in millions of the local currency
    return None


def company_from_title(title):
    """'Agentic AI regulatory startup Reversa raises €750k in pre-seed' -> 'Reversa'."""
    t = PREFIX_RE.sub("", title.strip())
    m = re.search(rf"^(?P<co>.+?)\s+(?:has\s+|have\s+)?{VERB}\b", t, re.I)
    if not m:
        return ""
    co = m.group("co").strip(" ,:;-'\"‘’“”")
    co = DESCRIPTOR_RE.sub("", co)
    # "Sebastian Thrun's Dulo", "Egypt's Chipverse" -> the name after the possessive
    if re.search(r"['’]s\s+\S", co):
        co = re.split(r"['’]s\s+", co)[-1]
    co = re.sub(r"^(the|how|why|india'?s|us-based|uk-based|[a-z]+-based)\s+", "", co, flags=re.I)
    co = re.sub(r"['’]s$", "", co).strip(" ,:;-")
    words = co.split()
    if not words or len(words) > 5 or not re.match(r"[A-Z0-9]", words[0]):
        return ""
    if re.search(r"\b(startup|start-up|company|firm|platform|maker|provider|unicorn|startups|founders?|it|this|"
                 r"report|sources?)$", co, re.I):
        return ""  # "Israeli startup raises ...": no name in the headline
    return co


def round_of(text):
    m = ROUND_RE.search(text or "")
    if not m:
        return ""
    r = re.sub(r"pre[- ]?seed", "pre-seed", m.group(1).lower())
    return r if r in ROUNDS else ""


def parse_feed(xml, india_edition):
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def tag(name):
            m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", item, re.S)
            v = m.group(1) if m else ""
            v = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", v.strip(), flags=re.S)
            return html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(v))).strip()
        title, link, desc = tag("title"), tag("link"), tag("description")
        source = tag("source")
        if " - " in title and not source:
            source = title.rsplit(" - ", 1)[1]
        headline = re.sub(r"\s+-\s+[^-]{2,60}$", "", title) if "news.google.com" in link else title
        rnd = round_of(headline) or round_of(desc[:300])
        if not rnd or NOT_STARTUP_RE.search(headline):
            continue
        co = company_from_title(headline)
        if not co:
            continue
        text = f"{headline} {desc[:400]}"
        try:
            date = email.utils.parsedate_to_datetime(tag("pubDate")).date().isoformat()
        except (TypeError, ValueError):
            date = dt.date.today().isoformat()
        out.append({
            "company": co, "round": rnd, "amount_m": amount_usd_m(headline) or amount_usd_m(desc[:300]),
            "date": date, "headline": headline, "url": link, "source": source,
            "india": bool(INDIA_RE.search(f"{text} {source}")) or (india_edition and "₹" in text),
            "tech": bool(TECH_RE.search(text)),
        })
    return out


def from_news():
    def one(feed):
        url, india = feed
        try:
            return parse_feed(_get(url).text, india)
        except requests.RequestException as e:
            print(f"[funding] feed failed ({url[:70]}): {e}", file=sys.stderr)
            return []
    with ThreadPoolExecutor(max_workers=8) as pool:
        return [r for batch in pool.map(one, FEEDS) for r in batch]


# --- SEC Form D -------------------------------------------------------------
SEC_TECH = {"Other Technology", "Computers", "Telecommunications"}


def round_from_amount(m):
    """Form D doesn't name the round; guess it from the amount raised (USD millions)."""
    if m is None:
        return ""
    return "seed" if m < 3 else "series a" if m < 20 else "series b" if m < 60 else "series c" if m < 150 else ""


def from_form_d(seen_filings, days=7):
    """Tech startups that filed a Form D in the last `days` days. Returns (rounds, filings read)."""
    contact = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if not contact:
        return [], []
    headers = {"User-Agent": f"scrape-company-listings job watcher {contact}"}
    filings = []
    today = dt.date.today()
    for back in range(days):
        d = today - dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        q = (d.month - 1) // 3 + 1
        url = f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{q}/form.{d:%Y%m%d}.idx"
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        for line in r.text.splitlines():
            if line.startswith("D ") and not line.startswith("D/A"):
                path = line.split()[-1]
                if path not in seen_filings:
                    filings.append(path)
        time.sleep(0.2)

    def read(path):
        cik = path.split("/")[2]
        acc = os.path.basename(path)[:-4].replace("-", "")
        try:
            time.sleep(0.3)  # SEC asks for at most 10 requests a second
            r = requests.get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/primary_doc.xml",
                             headers=headers, timeout=TIMEOUT)
            return path, (r.text if r.status_code == 200 else None)
        except requests.RequestException:
            return path, None

    rounds, done = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for path, xml in pool.map(read, filings[:1500]):
            if xml is None:
                continue
            done.append(path)
            g = lambda t: (re.search(rf"<{t}>(.*?)</{t}>", xml, re.S) or [None, ""])[1].strip()
            industry = g("industryGroupType")
            if industry not in SEC_TECH or "<isAmendment>true" in xml:
                continue
            year = re.search(r"<yearOfInc>.*?<value>(\d{4})</value>", xml, re.S)
            if g("entityType") != "Corporation" or (year and int(year.group(1)) < 2015):
                continue
            try:
                sold = float(g("totalAmountSold") or 0) / 1e6
            except ValueError:
                sold = 0
            if not 0.25 <= sold <= 150:
                continue
            people = []
            for p in re.findall(r"<relatedPersonInfo>(.*?)</relatedPersonInfo>", xml, re.S):
                first = (re.search(r"<firstName>(.*?)</firstName>", p) or [None, ""])[1]
                last = (re.search(r"<lastName>(.*?)</lastName>", p) or [None, ""])[1]
                rel = re.findall(r"<relationship>(.*?)</relationship>", p)
                if "Executive Officer" in rel and first and first != "*":
                    people.append(html.unescape(f"{first} {last}".strip()))
            name = html.unescape(g("entityName"))
            name = re.sub(r",?\s+(inc|corp|corporation|co|ltd|llc|technologies inc)\.?$", "", name, flags=re.I)
            filed = re.search(r"/(\d{10})-(\d{2})-", path)
            rounds.append({
                "company": name.title() if name.isupper() else name, "round": round_from_amount(sold),
                "round_guessed": True, "amount_m": round(sold, 2), "date": today.isoformat(),
                "headline": f"Form D: sold ${sold:.1f}M ({industry}), {g('city').title()}, {g('stateOrCountry')}",
                "url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={path.split('/')[2]}&type=D",
                "source": "SEC Form D", "india": False, "tech": True, "founders": people[:4],
            })
    return [r for r in rounds if r["round"]], done


# --- enrich -----------------------------------------------------------------
def tracked_names():
    with open(os.path.join(HERE, "companies.yaml")) as f:
        doc = yaml.safe_load(f) or {}
    return {norm(n): n for n in list(doc.get("companies") or {}) + list(doc.get("unresolved") or [])}


def probe_board(company):
    """First Ashby / Greenhouse / Lever board found under an obvious slug: (url, open jobs) or None."""
    base = norm(company)
    slugs = list(dict.fromkeys([base, re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-"),
                                re.sub(r"(ai|labs|hq|inc)$", "", base) or base]))
    for s in slugs:
        for url, api, count in (
            (f"https://jobs.ashbyhq.com/{s}", f"https://api.ashbyhq.com/posting-api/job-board/{s}",
             lambda d: len(d.get("jobs") or [])),
            (f"https://job-boards.greenhouse.io/{s}", f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
             lambda d: len(d.get("jobs") or [])),
            (f"https://jobs.lever.co/{s}", f"https://api.lever.co/v0/postings/{s}?mode=json",
             lambda d: len(d) if isinstance(d, list) else 0),
        ):
            try:
                r = requests.get(api, timeout=15)
                if r.status_code == 200:
                    n = count(r.json())
                    if n:
                        return {"url": url, "jobs": n}
            except (requests.RequestException, ValueError):
                pass
    return None


def links(company):
    q = urllib.parse.quote
    return {
        "linkedin": f"https://www.linkedin.com/search/results/people/?keywords={q(company + ' founder')}",
        "x": f"https://x.com/search?q={q(company + ' founder')}&f=user",
        "web": f"https://www.google.com/search?q={q(company + ' startup')}",
    }


# --- state ------------------------------------------------------------------
def load_state(path=STATE):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def update(path=STATE):
    """Fetch, merge into funding.json, and return the rounds that are new since the last run
    (empty on the very first run, which only records what is out there)."""
    state = load_state(path)
    first_run = not state
    rounds = state.get("rounds", {})
    sec_seen = set(state.get("sec_seen", []))
    probes = state.get("probes", {})

    found = [r for r in from_news() if r["tech"]]
    sec, read = from_form_d(sec_seen)
    sec_seen.update(read)
    found += sec

    tracked = tracked_names()
    new = []
    for r in found:
        key = norm(r["company"])
        if len(key) < 2:
            continue
        cur = rounds.get(key)
        if cur and cur["round"] == r["round"]:
            # Same round seen again: keep the earliest date, add founders / amount / India if missing.
            cur["date"] = min(cur["date"], r["date"])
            cur["india"] = cur["india"] or r["india"]
            cur["amount_m"] = cur.get("amount_m") or r.get("amount_m")
            if r.get("founders") and not cur.get("founders"):
                cur["founders"] = r["founders"]
            if r["url"] not in cur["sources"]:
                cur["sources"].append(r["url"])
            continue
        rec = {k: r[k] for k in ("company", "round", "amount_m", "date", "headline", "india")}
        rec.update(round_guessed=r.get("round_guessed", False), founders=r.get("founders", []),
                   sources=[r["url"]], source=r["source"], first_seen=dt.date.today().isoformat())
        rounds[key] = rec
        new.append(rec)

    # Watch-list status and job-board probes (probe each company once).
    to_probe = [k for k, r in rounds.items() if k not in probes and k not in tracked]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for k, res in zip(to_probe, pool.map(lambda k: probe_board(rounds[k]["company"]), to_probe)):
            probes[k] = res
    for k, r in rounds.items():
        r["tracked"] = tracked.get(k, "")
        r["board"] = probes.get(k)
        r["links"] = links(r["company"])

    cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    rounds = {k: r for k, r in rounds.items() if r["date"] >= cutoff}
    probes = {k: v for k, v in probes.items() if k in rounds}
    with open(path, "w") as f:
        json.dump({"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                   "rounds": rounds, "probes": probes, "sec_seen": sorted(sec_seen)[-20000:]},
                  f, indent=1, ensure_ascii=False)
    new = [r for r in new if r["date"] >= cutoff]
    return [] if first_run else sorted(new, key=lambda r: (not r["india"], r["company"].lower()))


def render_email(new):
    lines = ["######## Recently funded (pre-seed to Series C): message the founders ########", ""]
    for r in new:
        amt = f"${r['amount_m']:g}M" if r.get("amount_m") else "amount n/a"
        rnd = ROUND_LABEL.get(r["round"], r["round"]) + (" (guess from amount)" if r.get("round_guessed") else "")
        tags = [t for t in ("India" if r["india"] else "", "on watch list" if r.get("tracked") else "") if t]
        lines.append(f"== {r['company']} · {rnd} · {amt}{' · ' + ', '.join(tags) if tags else ''}")
        lines.append(f"  {r['headline']}")
        if r.get("founders"):
            lines.append(f"  Founders / officers: {', '.join(r['founders'])}")
        if r.get("board"):
            lines.append(f"  Possible job board ({r['board']['jobs']} open, check it's the same company): {r['board']['url']}")
        lines.append(f"  {r['sources'][0]}")
        lines.append(f"  Find founders: {r['links']['linkedin']}")
        lines.append("")
    return lines


if __name__ == "__main__":
    new = update(sys.argv[1] if len(sys.argv) > 1 else STATE)
    st = load_state(sys.argv[1] if len(sys.argv) > 1 else STATE)
    print(f"{len(st.get('rounds', {}))} rounds tracked, {len(new)} new", file=sys.stderr)
