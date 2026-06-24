"""Resolve unresolved companies to their LinkedIn numeric company id (f_C facet).

Companion to discover.py (which resolves ATS endpoints). Name keyword search on
LinkedIn is unreliable: results get buried under "(Freelancer)" gig spam and can
match a *different* company entity with the same name, so real engineering roles
never surface. Pinning the correct f_C id in companies.yaml `linkedin_ids:` makes
linkedin.py query that entity directly.

Method per company (stays on the guest jobs API; the /company/<slug>/ HTML page
returns HTTP 999 for guests):
  1. keyword job-search the name -> job cards (title + /company/<slug> + job id).
  2. keep cards whose company fuzzily matches the name; group by slug (entity).
  3. score each entity by its cards: engineering titles vs "(Freelancer)".
  4. pick the entity with the most engineering (tiebreak: least freelancer).
  5. fetch one of its jobs' guest detail -> numeric id (facetCurrentCompany=<id>).
  6. assign a confidence so a human can decide what to trust.

Usage:
    python discover_linkedin.py                 # review only: writes linkedin_review.{jsonl,md}
    python discover_linkedin.py --limit 10      # smoke test on first N unresolved
    python discover_linkedin.py --only "Lyzr AI,Miko"   # target specific companies
    python discover_linkedin.py --apply         # also merge resolved ids into companies.yaml
    python discover_linkedin.py --apply --min-confidence high   # only merge high-confidence

Resumable: re-running skips companies already in the JSONL and any already pinned
in companies.yaml. Stops gracefully (progress saved) when LinkedIn rate-limits;
just re-run to continue. Delete linkedin_review.jsonl to start fresh.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time

import requests
import yaml

import linkedin
from scrape import is_engineering

HERE = __file__.rsplit("/", 1)[0]
COMPANIES_YAML = f"{HERE}/companies.yaml"
REVIEW_JSONL = f"{HERE}/linkedin_review.jsonl"
REVIEW_MD = f"{HERE}/linkedin_review.md"

SLUG_RE = re.compile(r'/company/([^/?"]+)')
# The numeric company id (f_C) is exposed in the guest job-detail HTML as
# facetCurrentCompany=<id> (often url-encoded %3D).
FC_RE = re.compile(r'facetCurrentCompany(?:%3D|=)(\d+)')

CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2, "none": 3}


# --- inputs -----------------------------------------------------------------
def load_yaml():
    with open(COMPANIES_YAML) as f:
        return yaml.safe_load(f) or {}


def load_targets(only=None):
    doc = load_yaml()
    unresolved = doc.get("unresolved", []) or []
    pinned = set(doc.get("linkedin_ids", {}) or {})
    if only:
        wanted = {c.strip().lower() for c in only}
        return [c for c in unresolved if c.lower() in wanted]
    return [c for c in unresolved if c not in pinned]


def done_companies():
    done = set()
    if os.path.exists(REVIEW_JSONL):
        with open(REVIEW_JSONL) as f:
            for line in f:
                line = line.strip()
                if line:
                    done.add(json.loads(line)["company"])
    return done


# --- resolution -------------------------------------------------------------
def candidate_entities(company, sess, pages=2):
    """Return {slug: {"titles":[...], "name": company_raw, "job_ids":[...]}}."""
    cands = {}
    for page in range(pages):
        url = linkedin.GUEST_URL.format(kw=requests.utils.quote(company),
                                        loc=requests.utils.quote("India"),
                                        start=page * 10)
        text = linkedin._fetch_page(sess, url, company)  # raises LinkedInBlocked
        if not text:
            break
        chunks = re.split(r'(?=data-entity-urn="urn:li:jobPosting:)', text)
        found = 0
        for c in chunks:
            urn_m = linkedin._URN_RE.search(c)
            title_m = linkedin._TITLE_RE.search(c)
            comp_m = linkedin._COMPANY_RE.search(c)
            slug_m = SLUG_RE.search(c)
            if not (urn_m and title_m and comp_m and slug_m):
                continue
            comp_raw = html.unescape(comp_m.group(1).strip())
            if not linkedin._company_matches(company, comp_raw):
                continue
            slug = slug_m.group(1)
            ent = cands.setdefault(slug, {"titles": [], "name": comp_raw, "job_ids": []})
            ent["titles"].append(html.unescape(title_m.group(1).strip()))
            ent["job_ids"].append(urn_m.group(1))
            found += 1
        if found == 0:
            break
        time.sleep(2 + random.uniform(0, 2))
    return cands


def score(ent):
    titles = ent["titles"]
    total = len(titles)
    return {
        "total": total,
        "eng": sum(1 for t in titles if is_engineering(t)),
        "free": sum(1 for t in titles if "freelancer" in t.lower()),
    }


def fetch_fc(job_id, sess):
    """Numeric company id via the guest job-detail HTML (facetCurrentCompany)."""
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    text = linkedin._fetch_page(sess, url, job_id)  # backs off / raises on block
    if not text:
        return None
    m = FC_RE.search(text)
    return m.group(1) if m else None


def resolve_one(company, sess):
    cands = candidate_entities(company, sess)
    if not cands:
        return {"company": company, "status": "no_results", "company_id": None,
                "confidence": "none", "candidates": 0, "note": "no matching cards"}

    scored = {slug: score(ent) for slug, ent in cands.items()}
    # Best: most engineering, then least freelancer, then most total.
    best = max(scored, key=lambda s: (scored[s]["eng"], -scored[s]["free"], scored[s]["total"]))
    bs = scored[best]
    time.sleep(2 + random.uniform(0, 2))
    fc = fetch_fc(cands[best]["job_ids"][0], sess)

    free_ratio = bs["free"] / bs["total"] if bs["total"] else 0
    if not fc:
        conf = "low"
    elif bs["eng"] >= 1 and free_ratio < 0.5 and len(cands) == 1:
        conf = "high"
    elif bs["eng"] >= 1 and free_ratio < 0.5:
        conf = "medium"
    else:
        conf = "low"

    return {
        "company": company,
        "status": "ok" if fc else "no_id",
        "company_id": fc,
        "slug": best,
        "confidence": conf,
        "candidates": len(cands),
        "eng": bs["eng"], "free": bs["free"], "total": bs["total"],
    }


# --- outputs ----------------------------------------------------------------
def read_review():
    rows = []
    if os.path.exists(REVIEW_JSONL):
        with open(REVIEW_JSONL) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def write_md(rows):
    rows = sorted(rows, key=lambda r: (CONFIDENCE_ORDER.get(r["confidence"], 9),
                                       r["company"].lower()))
    out = ["# LinkedIn company-id resolution — review",
           "",
           f"{len(rows)} companies. Pin trusted rows with "
           "`python discover_linkedin.py --apply`. Verify low/medium by opening "
           "the company's LinkedIn /jobs/ URL (the f_C in the address bar).",
           "",
           "| Confidence | Company | company_id | slug | eng/free/total | cands |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f'| {r["confidence"]} | {r["company"]} | `{r.get("company_id") or "—"}` '
                   f'| {r.get("slug","—")} | {r.get("eng","-")}/{r.get("free","-")}'
                   f'/{r.get("total","-")} | {r.get("candidates","-")} |')
    with open(REVIEW_MD, "w") as f:
        f.write("\n".join(out) + "\n")


def apply_to_yaml(rows, min_confidence):
    """Merge resolved ids into companies.yaml `linkedin_ids:` in place.

    Text-based so the file's comments and discover.py header survive. Returns the
    number of new ids written.
    """
    threshold = CONFIDENCE_ORDER[min_confidence]
    new = {r["company"]: int(r["company_id"]) for r in rows
           if r.get("company_id") and CONFIDENCE_ORDER.get(r["confidence"], 9) <= threshold}
    if not new:
        return 0, {}

    with open(COMPANIES_YAML) as f:
        lines = f.read().splitlines()

    existing, start, end = {}, None, len(lines)
    for i, line in enumerate(lines):
        if re.match(r"^linkedin_ids:\s*$", line):
            start = i
            for j in range(i + 1, len(lines)):
                m = re.match(r"^  (.+?):\s*(\d+)\s*$", lines[j])
                if m:
                    existing[m.group(1)] = int(m.group(2))
                elif lines[j].strip() == "":
                    end = j
                    break
                else:  # next top-level key
                    end = j
                    break
            else:
                end = len(lines)
            break

    merged = dict(existing)
    added = {c: i for c, i in new.items() if c not in merged}
    merged.update(new)
    block = ["linkedin_ids:"] + [f"  {c}: {merged[c]}"
                                 for c in sorted(merged, key=str.lower)]

    if start is None:
        header = [
            "",
            "# LinkedIn numeric company ids (the f_C facet) for entity-scoped job search.",
            "# Generated/updated by discover_linkedin.py --apply; hand-editable. Find an id",
            "# manually in the company's LinkedIn /jobs/ URL: ...?f_C=<id>.",
        ]
        lines = lines + header + block
    else:
        lines = lines[:start] + block + lines[end:]

    with open(COMPANIES_YAML, "w") as f:
        f.write("\n".join(lines) + "\n")
    return len(added), added


# --- main -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only resolve first N companies")
    ap.add_argument("--only", default="", help="comma-separated company names to target")
    ap.add_argument("--apply", action="store_true",
                    help="merge resolved ids into companies.yaml linkedin_ids:")
    ap.add_argument("--min-confidence", choices=["high", "medium", "low"], default="low",
                    help="lowest confidence to merge with --apply (default: low = all resolved)")
    args = ap.parse_args()

    only = [c for c in args.only.split(",") if c.strip()] if args.only else None
    targets = load_targets(only)
    done = done_companies() if not only else set()  # --only always re-resolves
    todo = [c for c in targets if c not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(targets)} target(s), {len(done)} already in review, {len(todo)} to do.",
          file=sys.stderr)

    sess = requests.Session()
    with open(REVIEW_JSONL, "a") as out:
        for i, company in enumerate(todo, 1):
            try:
                rec = resolve_one(company, sess)
            except linkedin.LinkedInBlocked as e:
                print(f"[{i}/{len(todo)}] RATE-LIMITED at {company!r}: {e}. "
                      f"Progress saved; re-run to resume.", file=sys.stderr)
                break
            except Exception as e:  # noqa: BLE001
                rec = {"company": company, "status": "error", "company_id": None,
                       "confidence": "none", "candidates": 0, "note": str(e)}
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"[{i}/{len(todo)}] {company}: {rec['confidence']} "
                  f"id={rec.get('company_id')}", file=sys.stderr)
            time.sleep(4 + random.uniform(0, 4))  # gentle between companies

    rows = read_review()
    write_md(rows)
    print(f"Wrote {REVIEW_MD} ({len(rows)} rows)", file=sys.stderr)

    if args.apply:
        n, added = apply_to_yaml(rows, args.min_confidence)
        print(f"Applied {n} new id(s) to {COMPANIES_YAML} "
              f"(min-confidence={args.min_confidence}).", file=sys.stderr)
        for c in sorted(added, key=str.lower):
            print(f"  + {c}: {added[c]}", file=sys.stderr)


if __name__ == "__main__":
    main()
