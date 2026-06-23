# Company job-listing watcher

Watches ~280 companies' career pages and **emails you when a new engineering
role appears** (India + Remote only). It talks to each company's Applicant
Tracking System (ATS) JSON API — Greenhouse, Ashby, Lever, Workable,
SmartRecruiters — so there's no fragile HTML scraping.

```
companies.txt    the company list (one per line, editable)
discover.py      resolves each company -> its ATS endpoint, writes companies.yaml
companies.yaml   generated registry of {company: {ats, slug}} (hand-editable)
ats.py           ATS adapters, all normalized to a common job shape
scrape.py        fetch -> filter (engineering + India/Remote) -> dedup -> email
state.json       set of job ids already seen (so you only get NEW listings)
```

## How it works
1. `discover.py` maps each company name to its ATS board (resolved **147 / 284**
   on first run; the rest are big custom/Workday sites with no public API —
   listed under `unresolved:` in `companies.yaml`).
2. `scrape.py` fetches every resolved board, keeps roles whose **title** looks
   like engineering (any seniority) and whose **location** is in India or is an
   open/remote role (foreign-only remote like "Remote – USA" is excluded).
3. New roles (not in `state.json`) are emailed as a single digest; `state.json`
   is then updated so the next run only reports deltas.

## Setup
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Email (Gmail App Password)
The script sends mail through Gmail SMTP. You need an **App Password** on the
sending account:
1. On the sender Gmail, enable **2-Step Verification**
   (Google Account → Security).
2. Go to **Google Account → Security → App passwords**, generate one, copy the
   16-character code.
3. `cp config.example.env .env` and fill in:
   - `SENDER_GMAIL` – the Gmail you send from
   - `GMAIL_APP_PASSWORD` – the 16-char app password
   - `RECIPIENT` – defaults to `khushi.nigamwork@gmail.com`

## Run it
```bash
# 1. (Re)build the company -> ATS registry. Run once, or after editing companies.txt
.venv/bin/python discover.py

# 2. See what currently matches, without sending email or touching state
.venv/bin/python scrape.py --dry-run

# 3. First real run: load creds and seed state.
#    Add --email-first-run to also email the full current snapshot (~250 roles).
set -a; source .env; set +a
.venv/bin/python scrape.py --email-first-run

# 4. From now on, each run emails only NEW listings:
.venv/bin/python scrape.py
```

## Schedule it (runs when your laptop is off)
This repo includes a **GitHub Actions** cron (`.github/workflows/scrape.yml`)
that runs every 3 hours in GitHub's cloud and commits `state.json` back to the
repo so dedup memory survives between runs:
1. Push this folder to a GitHub repo.
2. Repo → **Settings → Secrets and variables → Actions** → add
   `SENDER_GMAIL`, `GMAIL_APP_PASSWORD`, `RECIPIENT`.
3. The workflow runs on schedule; trigger a first run manually from the
   **Actions** tab (use it once for the initial seed).

> State persistence note: because each cloud run starts fresh, `state.json`
> *must* be committed back (the workflow does this). If you instead run on your
> Mac via `cron`/`launchd`, `state.json` just lives on disk.

## Tuning
- **Add/fix a company:** edit `companies.txt` and re-run `discover.py`, or add an
  entry by hand under `companies:` in `companies.yaml`:
  ```yaml
  companies:
    Some Company:
      ats: greenhouse        # greenhouse|ashby|lever|workable|smartrecruiters
      slug: theirboardslug
  ```
- **Change what counts as "engineering" or "India/Remote":** edit `ENGINEERING_RE`,
  `INDIA_RE`, `FOREIGN_RE`, `GLOBAL_RE` at the top of `scrape.py`.
- **Tighten to ~2–3 years experience:** currently all seniorities pass. To filter
  by years you'd fetch each job description and parse "X+ years" — not enabled yet.

## Coverage caveats
- Resolution is automatic and best-effort: a common name can resolve to the wrong
  board (e.g. a generic slug). Spot-check `companies.yaml` and correct slugs.
- Custom/Workday career sites (Google, Apple, Amazon, Microsoft, Meta, NVIDIA,
  Uber, DoorDash, …) have no simple public API and are left unresolved. They'd
  need bespoke adapters or a Google-Jobs aggregator.
