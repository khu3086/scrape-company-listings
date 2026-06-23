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


# --- state ------------------------------------------------------------------
def load_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return set(data.get("seen", []))
    except (FileNotFoundError, ValueError):
        return set()


def save_state(seen, path):
    with open(path, "w") as f:
        json.dump({"seen": sorted(seen)}, f, indent=0)


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


def gather_linkedin(companies, location="India"):
    """Best-effort LinkedIn pass for companies with no scrapable ATS.

    Sequential + rate-limited; stops early if LinkedIn rate-limits us. Local use
    only (LinkedIn blocks datacenter IPs and this is against their ToS).
    """
    import linkedin  # local import so a missing residential setup never breaks ATS runs
    sess = requests.Session()
    matches = []
    for i, company in enumerate(companies, 1):
        try:
            jobs = linkedin.search_company(company, location=location, pages=1, session=sess)
        except linkedin.LinkedInBlocked as e:
            print(f"  LinkedIn rate-limited after {i-1} companies ({e}); stopping LinkedIn pass.",
                  file=sys.stderr)
            break
        except Exception as e:  # noqa: BLE001
            print(f"  ! LinkedIn {company}: {e}", file=sys.stderr)
            continue
        for j in jobs:
            if is_engineering(j["title"]) and is_india_or_remote(j):
                matches.append(j)
    print(f"LinkedIn pass: {len(matches)} matches across {len(companies)} unresolved companies.",
          file=sys.stderr)
    return matches


def fetch_company(company, cfg):
    jobs = ats_mod.fetch(cfg)
    for j in jobs:
        j["company"] = company
    return jobs


def gather_matches(registry, workers=12):
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
                if j.get("id") and is_engineering(j["title"]) and is_india_or_remote(j):
                    matches.append(j)
    return matches


# --- email ------------------------------------------------------------------
def render_email(new_jobs):
    by_company = {}
    for j in new_jobs:
        by_company.setdefault(j["company"], []).append(j)

    lines = [f"{len(new_jobs)} new engineering listing(s) (India + Remote):", ""]
    for company in sorted(by_company, key=str.lower):
        lines.append(f"== {company} ==")
        for j in sorted(by_company[company], key=lambda x: x["title"].lower()):
            loc = j["location"] or ("Remote" if j["remote"] else "—")
            lines.append(f"  • {j['title']}  [{loc}]")
            lines.append(f"    {j['url']}")
        lines.append("")
    return "\n".join(lines)


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
    recipient = os.environ.get("RECIPIENT", "khushi.nigamwork@gmail.com")
    if not sender or not password:
        print("ERROR: SENDER_GMAIL / GMAIL_APP_PASSWORD not set; cannot send email.", file=sys.stderr)
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())
    print(f"Emailed {recipient}: {subject}", file=sys.stderr)
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
    if not args.no_ats:
        registry = load_registry()
        print(f"Scanning {len(registry)} companies...", file=sys.stderr)
        matches = gather_matches(registry, workers=args.workers)
        print(f"Matched {len(matches)} engineering roles via ATS (India + Remote).", file=sys.stderr)

    if args.linkedin:
        unresolved = load_unresolved()
        print(f"LinkedIn pass over {len(unresolved)} unresolved companies (rate-limited)...", file=sys.stderr)
        matches.extend(gather_linkedin(unresolved, location=args.linkedin_location))
        print(f"Total matched: {len(matches)}.", file=sys.stderr)

    seen = load_state(args.state)
    fresh_state = len(seen) == 0
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
    save_state(seen, args.state)
    print(f"State now tracks {len(seen)} job ids.", file=sys.stderr)


if __name__ == "__main__":
    main()
