"""Fetch jobs from every resolved company, filter, dedupe, and email new ones.

Run:
    python scrape.py --dry-run         # print matches, no email, no state write
    python scrape.py                   # normal run: email only NEW listings
    python scrape.py --email-first-run # on a fresh state, email everything matching now

State (the set of job ids already seen) lives in state.json so reruns only
report genuinely new listings. Email is sent via Gmail SMTP using credentials
from the environment (see config.example.env).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from email.utils import formatdate

import requests
import yaml

import ats as ats_mod
import contracts

HERE = __file__.rsplit("/", 1)[0]
COMPANIES_YAML = f"{HERE}/companies.yaml"
STATE_JSON = f"{HERE}/state.json"

# --- filters ----------------------------------------------------------------
ENGINEERING_RE = re.compile(
    r"\b("
    r"engineer|engineering|developer|swe|sde|programmer|backend|back[- ]?end|"
    r"frontend|front[- ]?end|full[- ]?stack|machine learning|ml engineer|"
    r"infrastructure|infra|platform|software|devops|sre|systems|firmware|"
    r"data engineer|forward[- ]?deployed|robotics|compiler|kernel|architect|"
    r"member of technical staff|\bmts\b|technical staff"
    r")\b",
    re.I,
)

# YC writes India as the bare country code: "IN / Remote (IN)". Case-sensitive, and
# not after a comma, so "Indianapolis, IN" (Indiana) doesn't count.
INDIA_CODE_RE = re.compile(r"(^|\()IN(\)|\s*/|$)")
INDIA_RE = re.compile(
    r"\b("
    r"india|bangalore|bengaluru|mumbai|new delhi|delhi|hyderabad|pune|chennai|"
    r"gurgaon|gurugram|noida|kolkata|ahmedabad|gandhinagar|gift city|kochi|cochin|jaipur|indore"
    r")\b",
    re.I,
)

# "Remote - USA" / "Remote, UK" etc. are remote-within-a-foreign-geography and
# are NOT relevant to an India-based search. If a remote role names one of these
# (and does not also name India), we drop it.
FOREIGN_RE = re.compile(
    r"\b("
    r"usa|u\.s\.|\bus\b|united states|america|americas|canada|uk|u\.k\.|"
    r"united kingdom|england|ireland|scotland|netherlands|germany|france|spain|"
    r"portugal|poland|italy|sweden|norway|denmark|switzerland|austria|belgium|"
    r"europe|emea|israel|uae|dubai|abu dhabi|singapore|japan|china|korea|"
    r"australia|new zealand|brazil|mexico|argentina|colombia|latam|philippines|"
    r"indonesia|vietnam|thailand|malaysia|nigeria|kenya|egypt|turkey|"
    r"new york|san francisco|london|berlin|paris|toronto|seattle|austin|boston"
    r")\b",
    re.I,
)

# Regions that include India -> a remote role scoped to these is still relevant.
GLOBAL_RE = re.compile(
    r"\b(global|anywhere|worldwide|world wide|fully remote|apac|asia[- ]?pacific|asia)\b",
    re.I,
)


HOME_BASED_RE = re.compile(r"\bhome[- ]based\b|\bwork from home\b|\bwfh\b", re.I)


def is_engineering(title):
    return bool(ENGINEERING_RE.search(title or ""))


PRODUCT_RE = re.compile(
    r"\b(product manager|product management|product owner|product lead|head of product|"
    r"director,? (of )?product|vp,? (of )?product|\bapm\b|product strategist)\b", re.I)
NOT_PRODUCT_RE = re.compile(r"product (marketing|design|analyst|support|specialist)", re.I)
SCIENCE_RE = re.compile(r"\b(scientists?|deployment strategist)\b", re.I)


def role_type(title):
    """"product" | "science" | "engineering" | None (not a role we track)."""
    t = title or ""
    if PRODUCT_RE.search(t) and not NOT_PRODUCT_RE.search(t):
        return "product"
    if SCIENCE_RE.search(t):
        return "science"
    if ENGINEERING_RE.search(t):
        return "engineering"
    return None


def is_india_or_remote(job):
    """Keep India-located roles, plus genuinely-open remote roles.

    A remote role tied to a specific foreign geography ("Remote - USA") is
    excluded, since it isn't open to an India-based applicant.
    """
    loc = (job.get("location") or "").strip()
    low = loc.lower()

    if INDIA_RE.search(low) or INDIA_CODE_RE.search(loc):
        return True

    # "Home based - Worldwide" (Canonical) / "work from home" mean remote too.
    remote = bool(job.get("remote")) or "remote" in low or bool(HOME_BASED_RE.search(low))
    if not remote:
        return False

    # Remote with no location, or explicitly global/anywhere -> keep.
    if not loc or GLOBAL_RE.search(low):
        return True
    # Remote, but the only text is "remote" (no country) -> keep.
    if not re.sub(r"[^a-z]", "", HOME_BASED_RE.sub("", low.replace("remote", ""))):
        return True
    # Remote, but scoped to a specific (non-India) location -> drop. This covers
    # named foreign geographies ("Remote - USA") and city offices flagged
    # remote-eligible ("SF Office"). India and global/APAC were handled above.
    return False


# US-located roles, kept only for companies under `h1b_sponsors:` in companies.yaml.
US_RE = re.compile(
    r"\b("
    r"united states|usa|u\.s\.a?\.?|us|bay area|san francisco|sf|new york|nyc|"
    r"seattle|bellevue|redmond|kirkland|sunnyvale|mountain view|palo alto|"
    r"menlo park|san jose|santa clara|cupertino|redwood city|san mateo|"
    r"foster city|south san francisco|san diego|los angeles|austin|dallas|"
    r"houston|boston|chicago|denver|boulder|atlanta|miami|pittsburgh|"
    r"portland|raleigh|durham|salt lake city|phoenix|ann arbor|detroit|"
    r"washington,? d\.?c\.?|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|"
    r"louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|"
    r"missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    r"north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    r"rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|"
    r"virginia|wisconsin|wyoming"
    r")\b",
    re.I,
)
# "Seattle, WA" / "Austin, TX": a two-letter state code after a comma (case-sensitive).
US_STATE_CODE_RE = re.compile(
    r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
    r"MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|"
    r"WV|WI|WY|DC)\b"
)


def is_us(job):
    loc = job.get("location") or ""
    return bool(US_RE.search(loc) or US_STATE_CODE_RE.search(loc))


# US startup hubs: roles here are emailed for the startups selected by `us_hubs:`
# in companies.yaml, whether or not the company has an H-1B filing history.
US_HUB_RE = re.compile(
    r"san francisco|\bsf\b|bay area|silicon valley|palo alto|mountain view|menlo park|redwood city|"
    r"san mateo|san jose|sunnyvale|santa clara|cupertino|berkeley|oakland|los angeles|santa monica|"
    r"el segundo|san diego|irvine|california|, ?ca\b|new york|\bnyc\b|brooklyn|manhattan|, ?ny\b",
    re.I)


def is_us_hub(job):
    return bool(US_HUB_RE.search(job.get("location") or ""))


def load_hub_email_backlog():
    """Startups whose CURRENT SF/CA/NYC roles should be emailed once, not seeded."""
    with open(COMPANIES_YAML) as f:
        doc = yaml.safe_load(f) or {}
    return set((doc.get("us_hubs") or {}).get("email_backlog") or [])


def load_us_hub_companies(registry):
    """Companies whose SF / California / NYC roles are emailed (us_hubs: in companies.yaml)."""
    with open(COMPANIES_YAML) as f:
        doc = yaml.safe_load(f) or {}
    cfg = doc.get("us_hubs") or {}
    stages = set(cfg.get("stages") or [])
    exclude = set(cfg.get("exclude") or [])
    tagged = load_stages()
    return {c for c in registry if tagged.get(c) in stages and c not in exclude}


# --- state ------------------------------------------------------------------
def _read_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def load_state(path):
    return set(_read_state(path).get("seen", []))


# Bump when a change makes many previously-hidden US roles visible at once (e.g.
# better location parsing), so every sponsor's backlog is re-absorbed silently.
US_SEED_VERSION = 2


def load_us_seeded(path):
    """H-1B sponsors whose existing US listings have already been absorbed into state."""
    data = _read_state(path)
    if data.get("us_seed_version", 1) != US_SEED_VERSION:
        return set()
    return set(data.get("us_seeded", []))


def load_hub_seeded(path):
    """Startups whose existing SF / CA / NYC roles have already been absorbed into state."""
    return set(_read_state(path).get("hub_seeded", []))


# Role types whose existing listings have been absorbed into state. Product and
# science roles were added 2026-11; their backlog is recorded silently once.
ROLE_TYPES = ("engineering", "science", "product")


def load_role_seeded(path):
    data = _read_state(path)
    return set(data.get("role_seeded", ["engineering"] if data else []))


def load_company_seeded(path, seen):
    """Companies whose existing listings have already been absorbed into state. Older state
    files have no list, so any company that already has a job id in `seen` counts."""
    data = _read_state(path)
    if "company_seeded" in data:
        return set(data["company_seeded"])
    return {k.split(":", 1)[0] for k in seen if not k.startswith("contract:")}


def load_contract_seeded(path):
    """Contract platforms whose existing listings have already been absorbed into state."""
    data = _read_state(path)
    contracts.MICRO1_TITLES.update(data.get("micro1_titles", {}))
    return set(data.get("contract_seeded", []))


def save_state(seen, path, us_seeded=(), lab_watch=None, hub_seeded=None, contract_seeded=None,
               role_seeded=ROLE_TYPES, company_seeded=None):
    data = {"seen": sorted(seen), "us_seeded": sorted(us_seeded),
            "us_seed_version": US_SEED_VERSION, "role_seeded": sorted(role_seeded)}
    if hub_seeded is not None:
        data["hub_seeded"] = sorted(hub_seeded)
    if contract_seeded is not None:
        data["contract_seeded"] = sorted(contract_seeded)
    if company_seeded is not None:
        data["company_seeded"] = sorted(company_seeded)
    if contracts.MICRO1_TITLES:
        data["micro1_titles"] = dict(sorted(contracts.MICRO1_TITLES.items()))
    if lab_watch is not None:
        data["lab_watch"] = lab_watch
    with open(path, "w") as f:
        json.dump(data, f, indent=0)


def job_key(company, job):
    return f"{company}:{job['id']}"


def contract_key(job):
    # Prefixed so a platform's contract ids never collide with its full-time ATS board.
    return f"contract:{job['company']}:{job['id']}"


# --- fetch ------------------------------------------------------------------
def load_registry():
    with open(COMPANIES_YAML) as f:
        doc = yaml.safe_load(f) or {}
    return doc.get("companies", {}) or {}


def load_unresolved():
    with open(COMPANIES_YAML) as f:
        doc = yaml.safe_load(f) or {}
    return doc.get("unresolved", []) or []


def load_linkedin_ids():
    """Optional map of company name -> LinkedIn numeric company id (f_C facet).

    Companies with a known id are queried entity-scoped (reliable); the rest fall
    back to name keyword search.
    """
    with open(COMPANIES_YAML) as f:
        doc = yaml.safe_load(f) or {}
    return doc.get("linkedin_ids", {}) or {}


LINKEDIN_CURSOR = f"{HERE}/state.linkedin.cursor"


def _read_cursor():
    try:
        with open(LINKEDIN_CURSOR) as f:
            return int(f.read().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def gather_linkedin(companies, location="India", ids=None, rotate=False):
    """Best-effort LinkedIn pass for companies with no scrapable ATS.

    Sequential + rate-limited; stops early if LinkedIn rate-limits us. Local use
    only (LinkedIn blocks datacenter IPs and this is against their ToS).

    LinkedIn usually cuts a run off partway, so with rotate=True each run starts
    where the previous one stopped (cursor in state.linkedin.cursor) and every
    company gets covered across runs instead of only the first few dozen.
    """
    start = _read_cursor() % len(companies) if rotate and companies else 0
    companies = companies[start:] + companies[:start]
    processed = len(companies)
    import linkedin  # local import so a missing residential setup never breaks ATS runs
    ids = ids or {}
    sess = requests.Session()
    matches = []
    for i, company in enumerate(companies, 1):
        try:
            jobs = linkedin.search_company(company, location=location, pages=1,
                                           session=sess, company_id=ids.get(company))
        except linkedin.LinkedInBlocked as e:
            print(f"  LinkedIn rate-limited after {i-1} companies ({e}); stopping LinkedIn pass.",
                  file=sys.stderr)
            processed = i - 1
            break
        except Exception as e:  # noqa: BLE001
            print(f"  ! LinkedIn {company}: {e}", file=sys.stderr)
            continue
        for j in jobs:
            role = role_type(j["title"])
            if role and is_india_or_remote(j):
                j["role"] = role
                matches.append(j)
    if rotate and companies:
        with open(LINKEDIN_CURSOR, "w") as f:
            f.write(str((start + processed) % len(companies)))
    print(f"LinkedIn pass: {len(matches)} matches from {processed}/{len(companies)} unresolved "
          f"companies (started at #{start}).", file=sys.stderr)
    return matches


def fetch_company(company, cfg):
    jobs = ats_mod.fetch(cfg)
    for j in jobs:
        j["company"] = company
    return jobs


def load_h1b_sponsors():
    with open(COMPANIES_YAML) as f:
        doc = yaml.safe_load(f) or {}
    return set(doc.get("h1b_sponsors", []) or [])


# Email section order and headings for the `stages:` tags in companies.yaml.
STAGE_ORDER = [
    ("research", "Research labs"),
    ("public", "Big tech / public & established"),
    ("late", "Late-stage startups (Series D+)"),
    ("series_c", "Series C"),
    ("series_b", "Series B"),
    ("series_a", "Series A"),
    ("seed", "Seed"),
    ("pre_seed", "Pre-seed"),
    ("other", "Other (acquired / non-profit / stage unknown)"),
]


def load_stages():
    """{company: stage key}; untagged companies fall under "other"."""
    with open(COMPANIES_YAML) as f:
        doc = yaml.safe_load(f) or {}
    out = {}
    for stage, names in (doc.get("stages") or {}).items():
        for n in names or []:
            out[n] = stage
    return out


def gather_matches(registry, workers=12, h1b_sponsors=(), hub_companies=()):
    matches = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_company, c, cfg): c for c, cfg in registry.items()}
        for fut in as_completed(futs):
            company = futs[fut]
            try:
                jobs = fut.result()
            except Exception as e:  # noqa: BLE001 - never let one board kill the run
                print(f"  ! {company}: {e}", file=sys.stderr)
                continue
            anywhere = bool(registry[company].get("any_location"))
            # Optional per-company title regex, narrowing broad boards (e.g. CERN's
            # civil/mechanical roles) to software-adjacent ones.
            only = registry[company].get("title_filter")
            only = re.compile(only, re.I) if only else None
            # Optional per-company regex of extra titles to keep even if they don't
            # look like engineering (e.g. "Research Scientist" at academic labs).
            extra = registry[company].get("include_titles")
            extra = re.compile(extra, re.I) if extra else None
            for j in jobs:
                if not j.get("id"):
                    continue
                role = role_type(j["title"])
                if not role and extra and extra.search(j["title"]):
                    role = "science"
                if not role:
                    continue
                j["role"] = role
                if only and not only.search(j["title"]):
                    continue
                if is_india_or_remote(j):
                    matches.append(j)
                elif anywhere:
                    # e.g. CERN: roles abroad that Indian nationals are eligible for
                    j["abroad"] = True
                    matches.append(j)
                elif company in h1b_sponsors and is_us(j):
                    j["us_h1b"] = True
                    matches.append(j)
                elif company in hub_companies and is_us_hub(j):
                    j["us_hub"] = True
                    matches.append(j)
    return matches


YC_BOARD = "(YC jobs board)"


def gather_yc_board(registry):
    """Roles from YC's public jobs board at YC companies not already tracked directly.
    India / remote roles match like any board; SF / NYC roles go in the startups section,
    except those YC marks "US citizen/visa only" (no sponsorship)."""
    tracked = {c.lower() for c in registry}
    out = []
    for j in ats_mod.yc_board():
        if j["company"].lower() in tracked:
            continue
        role = role_type(j["title"])
        if not role:
            continue
        j["role"], j["yc_board"] = role, True
        if is_india_or_remote(j):
            out.append(j)
        elif is_us_hub(j) and "citizen" not in j.get("visa", "").lower():
            j["us_hub"] = True
            out.append(j)
    return out


# --- email ------------------------------------------------------------------
EMAIL_TITLE = "Khushi's Awesome Job Scraper"


def load_academic():
    with open(COMPANIES_YAML) as f:
        doc = yaml.safe_load(f) or {}
    return set(doc.get("academic", []) or [])


def render_email(new_jobs, lab_updates=(), contract_jobs=()):
    stages = load_stages()
    academic_names = load_academic()
    academic = [j for j in new_jobs if j["company"] in academic_names]
    rest = [j for j in new_jobs if j["company"] not in academic_names]
    india = [j for j in rest if not (j.get("us_h1b") or j.get("us_hub") or j.get("abroad"))]
    us = [j for j in rest if j.get("us_h1b")]
    hub = [j for j in rest if j.get("us_hub")]
    abroad = [j for j in rest if j.get("abroad")]
    lines = [EMAIL_TITLE, "=" * len(EMAIL_TITLE), ""]
    if contract_jobs:
        lines += ["######## Contract openings (AI training platforms) ########", "",
                  f"{len(contract_jobs)} new software / ML / data / AI contract(s):", ""]
        lines += _render_section(contract_jobs)
    if academic or lab_updates:
        heading = "Academic & research institutions"
        lines += [f"######## {heading} ########", ""]
        if academic:
            lines += [f"{len(academic)} new listing(s):", ""]
            lines += _render_section(academic)
        if lab_updates:
            lines += [f"Lab / professor page hiring updates ({len(lab_updates)}):", ""]
            for u in lab_updates:
                lines.append(f"== {u['name']} ==")
                lines.append(f"  {u['url']}")
                lines += [f"  + {t}" for t in u["added"]] or ["  (hiring text changed or was removed)"]
                lines.append("")
    if india:
        lines += [f"{len(india)} new listing(s) (India + Remote):", ""]
        lines += _render_by_stage(india, stages)
    if us:
        lines += [f"{len(us)} new US engineering listing(s) at H-1B sponsors:", ""]
        lines += _render_by_stage(us, stages)
    if hub:
        lines += [f"{len(hub)} new startup listing(s) in SF / California / NYC "
                  f"(no H-1B history; check sponsorship):", ""]
        lines += _render_by_stage(hub, stages)
    if abroad:
        lines += [f"{len(abroad)} new listing(s) abroad (visa-sponsoring / open to Indian nationals):", ""]
        lines += _render_section(abroad)
    return "\n".join(lines)


# --- academic lab watch -----------------------------------------------------
# Labs without a job board: watch the lab page / professor's homepage for
# hiring-related sentences and report when they change.
HIRING_RE = re.compile(
    r"hiring|recruit|openings?|open positions?|vacanc|postdoc|post-doc|research (?:engineer|assistant|"
    r"scientist|fellow|intern)|internships?|prospective (?:students|postdocs)|join (?:us|our|the) "
    r"(?:lab|group|team)|looking for|we are seeking|apply", re.I)


def load_lab_watch():
    with open(COMPANIES_YAML) as f:
        doc = yaml.safe_load(f) or {}
    return doc.get("lab_watch", {}) or {}


def _hiring_lines(url):
    r = requests.get(url, timeout=20, headers={"User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")})
    r.raise_for_status()
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", r.text, flags=re.S | re.I)
    # Inline tags (links, emphasis) sit inside sentences; only block tags break lines.
    body = re.sub(r"</?(?:a|strong|em|b|i|span|code|abbr|small|sup|sub)\b[^>]*>", "", body, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", body))
    out = []
    for block in text.split("\n"):
        for sent in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", block).strip()):
            if 15 <= len(sent) <= 300 and HIRING_RE.search(sent) and sent not in out:
                out.append(sent)
    return out


def check_labs(labs, prev):
    """Return (updates, new_state). First sighting of a page is stored silently."""
    updates, state = [], {}
    for name, cfg in labs.items():
        urls = cfg.get("urls") or [cfg["url"]]
        for url in urls:
            key = f"{name}|{url}"
            try:
                lines = _hiring_lines(url)
            except Exception as e:  # noqa: BLE001
                print(f"  ! lab {name} {url}: {e}", file=sys.stderr)
                if key in prev:
                    state[key] = prev[key]
                continue
            state[key] = lines
            if key in prev and lines != prev[key]:
                added = [l for l in lines if l not in prev[key]]
                updates.append({"name": name, "url": url, "added": added})
    return updates, state


def _render_by_stage(jobs, stages):
    known = {key for key, _ in STAGE_ORDER}
    by_stage = {}
    for j in jobs:
        stage = stages.get(j["company"], "other")
        by_stage.setdefault(stage if stage in known else "other", []).append(j)

    lines = []
    for key, heading in STAGE_ORDER:
        if key in by_stage:
            group = by_stage[key]
            lines += [f"######## {heading} ({len(group)}) ########", ""]
            if key == "other":
                untagged = sorted({j["company"] for j in group if j["company"] not in stages}, key=str.lower)
                if untagged:
                    lines += [f"(no stage tag yet in companies.yaml: {', '.join(untagged)})", ""]
            lines += _render_section(group)
    return lines


def _render_section(jobs):
    by_company = {}
    for j in jobs:
        by_company.setdefault(j["company"], []).append(j)

    lines = []
    for company in sorted(by_company, key=str.lower):
        lines.append(f"== {company} ==")
        for j in sorted(by_company[company], key=lambda x: x["title"].lower()):
            loc = j["location"] or ("Remote" if j["remote"] else "—")
            pay = f"  {j['pay']}" if j.get("pay") else ""
            tag = {"product": "[PM] ", "science": "[Science] "}.get(j.get("role"), "")
            lines.append(f"  • {tag}{j['title']}  [{loc}]{pay}")
            lines.append(f"    {j['url']}")
        lines.append("")
    return lines


def load_dotenv(path=None):
    """Load KEY=VALUE pairs from .env into the environment (without overriding
    vars already set, e.g. GitHub Actions secrets). Robust to spaces/quotes in
    values, so a Gmail App Password pasted with spaces still works.
    """
    path = path or f"{HERE}/.env"
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, val)
    except FileNotFoundError:
        pass


def send_email(subject, body):
    sender = os.environ.get("SENDER_GMAIL")
    # App passwords never contain spaces; strip them so a pasted "abcd efgh ..."
    # value still authenticates.
    password = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    # RECIPIENT may list several addresses, comma-separated.
    recipients = [r.strip() for r in os.environ.get("RECIPIENT", "khushi.nigamwork@gmail.com").split(",")
                  if r.strip()]
    if not sender or not password:
        print("ERROR: SENDER_GMAIL / GMAIL_APP_PASSWORD not set; cannot send email.", file=sys.stderr)
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())
    print(f"Emailed {', '.join(recipients)}: {subject}", file=sys.stderr)
    return True


# --- main -------------------------------------------------------------------
def main():
    load_dotenv()  # pick up .env for local runs (no-op when vars already set, e.g. CI)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print matches, no email, no state write")
    ap.add_argument("--email-first-run", action="store_true",
                    help="if state is empty, email everything currently matching (not just deltas)")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--linkedin", action="store_true",
                    help="ALSO scrape LinkedIn for unresolved companies (LOCAL ONLY; against LinkedIn ToS)")
    ap.add_argument("--linkedin-location", default="India",
                    help="LinkedIn location filter for the --linkedin pass (default: India)")
    ap.add_argument("--no-ats", action="store_true",
                    help="skip the ATS pass (e.g. LinkedIn-only local runs while the cloud cron handles ATS)")
    ap.add_argument("--state", default=STATE_JSON,
                    help="path to the dedup state file (use a separate file for local LinkedIn runs)")
    ap.add_argument("--dump-matches", metavar="PATH",
                    help="also write every current match to PATH (input for board.py --jobs)")
    args = ap.parse_args()

    matches = []
    sponsors = set()
    hubs = set()
    if not args.no_ats:
        registry = load_registry()
        print(f"Scanning {len(registry)} companies...", file=sys.stderr)
        sponsors = load_h1b_sponsors()
        untagged = sorted((c for c in registry if c not in load_stages()), key=str.lower)
        if untagged:
            print(f"Note: {len(untagged)} companies have no stage tag (grouped under Other): "
                  f"{', '.join(untagged)}", file=sys.stderr)
        hubs = load_us_hub_companies(registry)
        matches = gather_matches(registry, workers=args.workers, h1b_sponsors=sponsors,
                                 hub_companies=hubs)
        yc = gather_yc_board(registry)
        print(f"YC jobs board: {len(yc)} matching roles at YC companies not tracked directly.",
              file=sys.stderr)
        matches += yc
        n_us = sum(1 for j in matches if j.get("us_h1b"))
        n_hub = sum(1 for j in matches if j.get("us_hub"))
        n_abroad = sum(1 for j in matches if j.get("abroad"))
        print(f"Matched {len(matches) - n_us - n_hub - n_abroad} engineering roles via ATS "
              f"(India + Remote), plus {n_us} US roles at {len(sponsors)} H-1B sponsors, "
              f"{n_hub} SF/CA/NYC roles at {len(hubs)} startups and {n_abroad} abroad "
              f"(any_location boards).", file=sys.stderr)

    if args.linkedin:
        unresolved = load_unresolved()
        ids = load_linkedin_ids()
        print(f"LinkedIn pass over {len(unresolved)} unresolved companies "
              f"({len(ids)} with pinned company ids, rate-limited)...", file=sys.stderr)
        matches.extend(gather_linkedin(unresolved, location=args.linkedin_location, ids=ids,
                                       rotate=not args.dry_run))
        print(f"Total matched: {len(matches)}.", file=sys.stderr)

    seen = load_state(args.state)
    company_seeded = load_company_seeded(args.state, seen)  # before any backlog is added below
    fresh_state = len(seen) == 0
    # The first time a sponsor's US roles are scanned, absorb its existing backlog
    # silently (thousands of roles across big boards) and only email new postings after.
    us_seeded = load_us_seeded(args.state)
    unseeded = sponsors - us_seeded
    if unseeded and not fresh_state:
        backlog = [j for j in matches if j.get("us_h1b") and j["company"] in unseeded]
        seen.update(job_key(j["company"], j) for j in backlog)
        print(f"Seeding {len(backlog)} existing US roles for {len(unseeded)} newly listed "
              f"H-1B sponsor(s) without emailing.", file=sys.stderr)
    us_seeded |= sponsors
    # Same for startups newly in us_hubs scope: absorb current SF/CA/NYC roles silently.
    hub_seeded = load_hub_seeded(args.state)
    hub_new = hubs - hub_seeded - load_hub_email_backlog()
    if hub_new and not fresh_state:
        backlog = [j for j in matches if j.get("us_hub") and j["company"] in hub_new]
        seen.update(job_key(j["company"], j) for j in backlog)
        print(f"Seeding {len(backlog)} existing SF/CA/NYC roles for {len(hub_new)} startup(s) "
              f"without emailing.", file=sys.stderr)
    hub_seeded |= hubs
    # Role types newly tracked (e.g. product management): absorb their backlog silently.
    role_new = set(ROLE_TYPES) - load_role_seeded(args.state)
    if role_new and not fresh_state and not args.no_ats:
        backlog = [j for j in matches if j.get("role") in role_new]
        seen.update(job_key(j["company"], j) for j in backlog)
        print(f"Seeding {len(backlog)} existing {'/'.join(sorted(role_new))} roles without emailing.",
              file=sys.stderr)
    # Companies newly added to companies.yaml: absorb their current roles silently too,
    # so adding a big board (Amazon, Oracle, ...) doesn't email hundreds of old listings.
    if not args.no_ats:
        company_new = set(registry) - company_seeded
        if company_new and not fresh_state:
            backlog = [j for j in matches if j["company"] in company_new]
            seen.update(job_key(j["company"], j) for j in backlog)
            print(f"Seeding {len(backlog)} existing roles for {len(company_new)} newly added "
                  f"company board(s) without emailing.", file=sys.stderr)
        # Only once a board actually returned roles: a flaky board (Apple) that comes back empty
        # on its first scan would otherwise have its whole backlog emailed on the next run.
        company_seeded |= {j["company"] for j in matches if j["company"] in registry}
        # The YC board counts as one source: its current roles are recorded silently once.
        if YC_BOARD not in company_seeded and not fresh_state:
            backlog = [j for j in matches if j.get("yc_board")]
            seen.update(job_key(j["company"], j) for j in backlog)
            print(f"Seeding {len(backlog)} existing YC jobs-board roles without emailing.", file=sys.stderr)
        if any(j.get("yc_board") for j in matches):
            company_seeded.add(YC_BOARD)
    new_jobs = [j for j in matches if job_key(j["company"], j) not in seen]

    # Contract platforms (Mercor, micro1, ...): absorb each one's current listings
    # silently the first time it answers, then email only new postings.
    contract_jobs, contract_seeded = [], load_contract_seeded(args.state)
    if not args.no_ats:
        contract_jobs, answered, _failed = contracts.gather()
        new_platforms = set(answered) - contract_seeded
        if new_platforms and not fresh_state:
            backlog = [j for j in contract_jobs if j["company"] in new_platforms]
            seen.update(contract_key(j) for j in backlog)
            print(f"Seeding {len(backlog)} existing contract openings for {len(new_platforms)} newly "
                  f"tracked platform(s) without emailing.", file=sys.stderr)
        contract_seeded |= set(answered)
    new_contracts = [j for j in contract_jobs if contract_key(j) not in seen]
    if args.dump_matches:
        # board.py builds the hosted board from this file; contract=True puts them in their own section.
        with open(args.dump_matches, "w") as f:
            json.dump(matches + [{**j, "contract": True} for j in contract_jobs], f)

    lab_updates, lab_state = [], None
    if not args.no_ats:
        labs = load_lab_watch()
        if labs:
            lab_updates, lab_state = check_labs(labs, _read_state(args.state).get("lab_watch", {}))
            print(f"Watched {len(labs)} academic lab page set(s); {len(lab_updates)} changed.", file=sys.stderr)

    if args.dry_run:
        print(render_email(new_jobs or matches, lab_updates, new_contracts or contract_jobs))
        if lab_state:
            for k, v in lab_state.items():
                print(f"[lab] {k}: {len(v)} hiring line(s)", *(f"\n    - {l}" for l in v[:5]), file=sys.stderr)
        print(f"\n[dry-run] {len(new_jobs)} new / {len(matches)} total matching, {len(new_contracts)} new / "
              f"{len(contract_jobs)} contract openings. No email, no state change.", file=sys.stderr)
        return

    # Decide whether to email.
    should_email = bool(new_jobs or lab_updates or new_contracts)
    if fresh_state and not args.email_first_run:
        # First ever run: seed state silently, don't blast every existing listing.
        should_email = False
        print("First run: seeding state without emailing (use --email-first-run to email current snapshot).", file=sys.stderr)

    if should_email:
        parts = [f"{len(new_jobs)} new listing(s)"] if new_jobs or not new_contracts else []
        if new_contracts:
            parts.append(f"{len(new_contracts)} contract opening(s)")
        if lab_updates:
            parts.append(f"{len(lab_updates)} lab hiring update(s)")
        subject = f"{EMAIL_TITLE}: {', '.join(parts)}"
        try:
            ok = send_email(subject, render_email(new_jobs, lab_updates, new_contracts))
        except Exception as e:  # noqa: BLE001
            print(f"ERROR sending email: {e}", file=sys.stderr)
            ok = False
        if not ok:
            # Do NOT update state, so these listings are retried next run.
            print("Email failed; leaving state unchanged so listings retry next run.", file=sys.stderr)
            sys.exit(1)
    else:
        print("No email sent (no new listings).", file=sys.stderr)

    # Update state with everything currently matching.
    for j in matches:
        seen.add(job_key(j["company"], j))
    seen.update(contract_key(j) for j in contract_jobs)
    if lab_state is None and not args.no_ats:
        lab_state = _read_state(args.state).get("lab_watch")
    save_state(seen, args.state, us_seeded, lab_state,
               hub_seeded if not args.no_ats else load_hub_seeded(args.state), contract_seeded,
               company_seeded=company_seeded)
    print(f"State now tracks {len(seen)} job ids.", file=sys.stderr)


if __name__ == "__main__":
    main()
