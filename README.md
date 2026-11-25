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
1. `discover.py` auto-maps each company name to its ATS board; hand-verified
   boards (including Workday sites like NVIDIA, Red Hat, Fractal, Uniphore) were
   then added on top. **179 / 284 companies resolved.** The remaining ~105 use
   unsupported ATSes (Gem, Keka, Teamtailor, BambooHR, Rippling, YC
   WorkAtAStartup) or fully custom sites; they're listed under `unresolved:`.
2. `scrape.py` fetches every resolved board, keeps roles whose **title** looks
   like engineering (any seniority) and whose **location** is in India or is an
   open/remote role (foreign-only remote like "Remote – USA" is excluded).
3. New roles (not in `state.json`) are emailed as a single digest; `state.json`
   is then updated so the next run only reports deltas.
   A company newly added to `companies.yaml` has its current roles recorded
   silently the first time its board returns any (`company_seeded`), so only
   roles posted after that are emailed.

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

## Email layout

Each digest is split into India + Remote, then US roles at H-1B sponsors.
Within each part, companies are grouped by stage: research labs, big tech /
public & established, late-stage (Series D+), Series C, B, A, seed, pre-seed,
then other. Stages come from the hand-editable `stages:` section of
`companies.yaml`; an untagged company lands in "Other".

## SF / California / NYC roles at startups

`us_hubs:` in `companies.yaml` picks startups by stage (research labs through
pre-seed) whose engineering roles in SF / the Bay Area / California / NYC are
emailed in their own section, even without an H-1B filing history (check
sponsorship before applying). Each startup's existing roles are recorded
silently the first time, so only new postings are sent, except for companies
listed under `email_backlog:`. Defense/space firms that need US-person status
(SpaceX, Anduril, ...) are excluded.

## Roles abroad and academic labs

Companies listed under `academic:` in `companies.yaml` (AI4Bharat, CERN, IAIRO,
MIT CSAIL) get their own "Academic & research institutions" section at the top
of the email, which also carries the `lab_watch` updates.

- `any_location: true` on a company keeps its roles wherever they are (used for
  CERN, where Indian nationals are eligible since India is an associate member).
  They go in an "abroad (visa-sponsoring / open to Indian nationals)" section;
  Mercari's Tokyo board is tracked the same way. `title_filter:` (a
  regex) narrows a broad board to the roles you want.
  `include_titles:` (a regex) keeps extra titles the engineering filter would
  drop, such as "Research Scientist" at academic labs.
- MIT CSAIL is read from MIT's PeopleClick job site (`ats: peopleclick`,
  filtered to CSAIL's department codes). As a US university it sponsors H-1Bs
  outside the lottery, so its roles go in the "abroad" section too.
- Caltech's staff and research job board is on Taleo Business Edition
  (`ats: taleo`) and is handled the same way. `lab_watch:` also covers several
  Caltech labs: Anandkumar, Yue, Bouman, Gkioxari, Perona, Wierman, Ames,
  Chung, CAST and IQIM.
- `lab_watch:` lists academic labs that have no job board. Each run fetches the
  given pages (lab site, head professor's homepage) and pulls out sentences
  about hiring (hiring, postdoc, research engineer, openings, ...). When those
  change, the new sentences are emailed under "Academic lab hiring updates".
  The first check of a page is stored silently. LinkedIn and X profiles need a
  login, so they can't be watched this way.

## YC jobs board

Each run also reads YC's public jobs board (ycombinator.com/jobs: software, science
and product roles in India, remote, SF and NYC, about 200 postings across 20 pages)
and keeps roles at YC companies that aren't tracked directly. India / remote roles
match as usual; SF / NYC roles go in the startups section unless YC marks them
"US citizen/visa only". Its current roles were recorded silently the first time.
The full YC list (Work at a Startup) needs a login, so it isn't used.

## Wellfound

Each run also reads Wellfound's public role pages (software, ML, data science, data
engineering and product roles in India, San Francisco, New York and remote) and keeps
roles at startups that aren't tracked directly or on the YC board, posted in the last 45
days. India and open-remote roles match as usual. Remote roles limited to other countries
("Remote (United States)") are dropped. SF / NYC roles go in the startups section,
unless the description rules out sponsorship (US citizens only, security clearance,
"not sponsoring visas"). Wellfound's salary / equity range shows on the board.

Role pages show at most 3 listings per startup. For startups showing 3, the startup's own
Wellfound jobs page (which lists every opening) is also read, 40 a run, least recently read
first. These are cached in `state.json` (`wellfound_companies`) and refreshed within about
a day. Roles found this way that were posted more than 10 days earlier are recorded
silently, so only genuinely new postings are emailed.

- Wellfound is behind Cloudflare. Pages are fetched one at a time with a pause, because
  fetching in parallel got the IP challenged within minutes. The first 429 or challenge
  stops the pass for that run.
- Current roles are recorded silently the first time, but only after a full, unblocked
  pass, so a partial scan can't leave a backlog to be emailed later.

## Contract openings (AI training platforms)

`contracts.py` reads the paid-contract listings on Mercor, micro1, Ethos
(askethos.com, via General Catalyst's job board), DataAnnotation, Terac,
Handshake AI, Turing and Patronus AI, and keeps only software / ML / data / AI roles
(`ROLE_RE` / `EXCLUDE_RE`). Terac, Handshake, Turing and Patronus boards also carry staff
jobs, so only their contract roles are kept. New ones are emailed at the top of
the digest under "Contract openings", with the hourly pay where the platform
shows it, and get their own "Contract openings" filter on the job board. Each
platform's existing listings are recorded silently the first time it answers
(`contract_seeded` in `state.json`). micro1's API refuses GitHub's servers (403),
so there it falls back to its sitemap and job pages (no pay shown); titles are
cached in `state.json` (`micro1_titles`) so only new postings are fetched.
Outlier isn't covered.

## US roles at H-1B sponsors

Companies listed under `h1b_sponsors:` in `companies.yaml` (employers with
regular recent H-1B filings, per DOL LCA data) also have their **US-located**
engineering roles kept, emailed in a separate "US ... at H-1B sponsors" section.
The first time a sponsor is scanned, its existing US backlog is absorbed into
`state.json` silently (`us_seeded`), so you only get newly posted US roles.
Sponsorship history is no guarantee for a given role. Since Sept 2025, new
petitions for applicants outside the US also carry a $100k fee.

## LinkedIn fallback (for unresolved companies) — local only

Companies with no scrapable ATS (Gem/Keka/Teamtailor/custom sites under
`unresolved:`) can be covered best-effort via LinkedIn's public guest jobs
endpoint:

```bash
.venv/bin/python scrape.py --linkedin                       # ATS pass + LinkedIn for unresolved
.venv/bin/python scrape.py --linkedin --linkedin-location "India" --dry-run
```

It searches each unresolved company by name, keeps only cards whose company
actually matches (so a keyword hit on an unrelated job is dropped), then applies
the same engineering + India/Remote filters.

**Read before relying on it:**
- It uses LinkedIn against their **Terms of Service** — personal, low-volume use
  only, at your own risk.
- LinkedIn **blocks datacenter IPs** (instant `999`/`429`), so this only works
  from your **home connection (your Mac)** — do **NOT** add `--linkedin` to the
  GitHub Actions cron. On a rate-limit response the LinkedIn pass backs off and
  stops automatically.
- It's inherently fragile (LinkedIn can change their HTML anytime) and lower
  precision than the ATS APIs. Treat it as a bonus, not the backbone.

A good pattern: run the reliable ATS-only scrape in the cloud cron, and
occasionally run `scrape.py --linkedin` by hand on your Mac for extra coverage.

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

## Personal job board (apply + track)

`python board.py` scans every tracked board (about 8 minutes), ranks each opening
against `profile.yaml` and opens a page at http://localhost:8765 with:

- a match score (0-100) and the reasons behind it (green: skills that match;
  orange: penalties such as "Too senior" or "Hardware")
- filters by region (India & Remote, US H-1B sponsors, SF/CA/NYC startups,
  abroad, academic), funding stage, minimum score and search, plus a "New since
  last build" toggle
- a "Posted" filter (past 24 hours / 3 days / week) and a Newest sort. Most
  boards give a posting date (Greenhouse, Ashby, Lever, Workday, Oracle, ...);
  for the rest (Google, custom pages, YC) the date the board first scraped the
  opening is used
- an Apply button for each opening, plus Save / Applied / Hide and a status
  (Interviewing, Offer, Rejected) with notes, all saved to `applications.json`

`python board.py --no-scan` re-ranks the last scan instantly. Use it after
editing the keyword weights in `profile.yaml`. The page, its cache and
`applications.json` are personal and git-ignored.

## Role types, hosting

Besides engineering, the watcher now tracks scientist roles (Research / Applied /
Data Scientist, deployment strategist) and product management (PM, APM, product
owner / lead). Their existing listings were recorded silently once
(`role_seeded` in `state.json`); new ones are emailed with a `[PM]` or
`[Science]` tag. Forward-deployed roles count as engineering in email and have
their own filter on the board.

The board is also published on GitHub Pages by the 3-hourly workflow
(`scrape.py --dump-matches`, then `board.py --jobs ... --out site/index.html`).
Enable it once in the repo: Settings → Pages → Source: GitHub Actions. On the
hosted page, tracking lives in the browser; use Export / Import to move it
between devices or into `applications.json` for the local server. The repo is
public, so the hosted page is too.

## Recently funded startups (founder outreach)

`funding.py` collects pre-seed to Series C rounds every run from Google News
(US and India editions), TechCrunch, Crunchbase News, Inc42 and Entrackr, and keeps
the last 90 days in `funding.json`.

- **Email:** a "Recently funded" section lists rounds that are new since the last run
  (every round, no profile filter): company, round, amount in USD, headline + link,
  India flag, founders when known, and a LinkedIn search for the founders.
- **Board:** a "Recently funded" tab with Messaged / Replied / Hide tracking, notes,
  round and India filters, and a **Fits my experience** filter that tags each startup
  with the areas of your background it touches (`funding_fit` in `profile.yaml`).
- **Job boards:** each company is probed on Ashby / Greenhouse / Lever. A match is shown
  as "possible job board" (same-name companies exist), not added to `companies.yaml`.
- **SEC Form D (optional):** set the `SEC_CONTACT_EMAIL` repository secret (any address
  you're happy to give SEC; it goes in the request header, as SEC requires) to also read
  US Form D filings, which name founders / officers. Form D has no round name, so the
  round is estimated from the amount (under $3M seed, $3-20M A, $20-60M B, $60-150M C).
- Headline parsing is heuristic: expect the odd wrong company name or a missed round.
