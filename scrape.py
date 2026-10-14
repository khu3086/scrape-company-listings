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

INDIA_RE = re.compile(
    r"\b("
    r"india|bangalore|bengaluru|mumbai|new delhi|delhi|hyderabad|pune|chennai|"
    r"gurgaon|gurugram|noida|kolkata|ahmedabad|kochi|cochin|jaipur|indore"
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


def is_engineering(title):
    return bool(ENGINEERING_RE.search(title or ""))


def is_india_or_remote(job):
    """Keep India-located roles, plus genuinely-open remote roles.

    A remote role tied to a specific foreign geography ("Remote - USA") is
    excluded, since it isn't open to an India-based applicant.
    """
    loc = (job.get("location") or "").strip()
    low = loc.lower()

    if INDIA_RE.search(low):
        return True

    remote = bool(job.get("remote")) or "remote" in low
    if not remote:
        return False

    # Remote with no location, or explicitly global/anywhere -> keep.
    if not loc or GLOBAL_RE.search(low):
        return True
    # Remote, but the only text is "remote" (no country) -> keep.
    if not re.sub(r"[^a-z]", "", low.replace("remote", "")):
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


def save_state(seen, path, us_seeded=()):
    with open(path, "w") as f:
        json.dump({"seen": sorted(seen), "us_seeded": sorted(us_seeded),
                   "us_seed_version": US_SEED_VERSION}, f, indent=0)


def job_key(company, job):
    return f"{company}:{job['id']}"


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
            if is_engineering(j["title"]) and is_india_or_remote(j):
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


def gather_matches(registry, workers=12, h1b_sponsors=()):
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
            for j in jobs:
                if not (j.get("id") and is_engineering(j["title"])):
                    continue
                if is_india_or_remote(j):
                    matches.append(j)
                elif company in h1b_sponsors and is_us(j):
                    j["us_h1b"] = True
                    matches.append(j)
    return matches


# --- email ------------------------------------------------------------------
def render_email(new_jobs):
    stages = load_stages()
    india = [j for j in new_jobs if not j.get("us_h1b")]
    us = [j for j in new_jobs if j.get("us_h1b")]
    lines = []
    if india:
        lines += [f"{len(india)} new engineering listing(s) (India + Remote):", ""]
        lines += _render_by_stage(india, stages)
    if us:
        lines += [f"{len(us)} new US engineering listing(s) at H-1B sponsors:", ""]
        lines += _render_by_stage(us, stages)
    return "\n".join(lines)


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
            lines.append(f"  • {j['title']}  [{loc}]")
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
    args = ap.parse_args()

    matches = []
    sponsors = set()
    if not args.no_ats:
        registry = load_registry()
        print(f"Scanning {len(registry)} companies...", file=sys.stderr)
        sponsors = load_h1b_sponsors()
        untagged = sorted((c for c in registry if c not in load_stages()), key=str.lower)
        if untagged:
            print(f"Note: {len(untagged)} companies have no stage tag (grouped under Other): "
                  f"{', '.join(untagged)}", file=sys.stderr)
        matches = gather_matches(registry, workers=args.workers, h1b_sponsors=sponsors)
        n_us = sum(1 for j in matches if j.get("us_h1b"))
        print(f"Matched {len(matches) - n_us} engineering roles via ATS (India + Remote), "
              f"plus {n_us} US roles at {len(sponsors)} H-1B sponsors.", file=sys.stderr)

    if args.linkedin:
        unresolved = load_unresolved()
        ids = load_linkedin_ids()
        print(f"LinkedIn pass over {len(unresolved)} unresolved companies "
              f"({len(ids)} with pinned company ids, rate-limited)...", file=sys.stderr)
        matches.extend(gather_linkedin(unresolved, location=args.linkedin_location, ids=ids,
                                       rotate=not args.dry_run))
        print(f"Total matched: {len(matches)}.", file=sys.stderr)

    seen = load_state(args.state)
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
    new_jobs = [j for j in matches if job_key(j["company"], j) not in seen]

    if args.dry_run:
        print(render_email(new_jobs or matches))
        print(f"\n[dry-run] {len(new_jobs)} new / {len(matches)} total matching. No email, no state change.", file=sys.stderr)
        return

    # Decide whether to email.
    should_email = bool(new_jobs)
    if fresh_state and not args.email_first_run:
        # First ever run: seed state silently, don't blast every existing listing.
        should_email = False
        print("First run: seeding state without emailing (use --email-first-run to email current snapshot).", file=sys.stderr)

    if should_email and new_jobs:
        subject = f"{len(new_jobs)} new engineering listing(s)"
        try:
            ok = send_email(subject, render_email(new_jobs))
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
    save_state(seen, args.state, us_seeded)
    print(f"State now tracks {len(seen)} job ids.", file=sys.stderr)


if __name__ == "__main__":
    main()
