#!/usr/bin/env python3
"""Personal job board: every opening the watcher currently tracks, ranked
against profile.yaml, with Apply links and an application tracker.

    python board.py              # scan all boards (~8 min), build, serve, open browser
    python board.py --no-scan    # re-rank the last scan (after editing profile.yaml)
    python board.py --no-serve   # just write board.html

Served at http://localhost:8765. Statuses (saved / applied / interviewing / ...)
are written to applications.json, so they survive rebuilds. Opened directly as a
file, the page falls back to the browser's localStorage.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

import contracts
import scrape

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = f"{HERE}/profile.yaml"
JOBS_CACHE = f"{HERE}/board_jobs.json"
SEEN = f"{HERE}/board_seen.json"
APPS = f"{HERE}/applications.json"
OUT = f"{HERE}/board.html"
PORT = 8765
BLR_RE = re.compile(r"bengaluru|bangalore", re.I)


def scan():
    reg = scrape.load_registry()
    print(f"Scanning {len(reg)} companies (takes several minutes)...", file=sys.stderr)
    jobs = scrape.gather_matches(reg, h1b_sponsors=scrape.load_h1b_sponsors(),
                                 hub_companies=scrape.load_us_hub_companies(reg))
    contract_jobs, _, _ = contracts.gather()
    jobs += [{**j, "contract": True} for j in contract_jobs]
    with open(JOBS_CACHE, "w") as f:
        json.dump(jobs, f)
    return jobs


def _rules(profile, section):
    return [(r["label"], int(r["weight"]), re.compile(r["re"], re.I))
            for r in profile.get(section) or []]


def score(title, region, locations, profile, rules):
    skill, adj, why, warn = 0, 0, [], []
    for section, items in rules.items():
        for label, w, rx in items:
            if not rx.search(title):
                continue
            if section == "skills":
                skill += w
                why.append(label)
            else:
                adj += w
                if w < 0:
                    warn.append(label)
    pts = min(skill, int(profile.get("skill_cap", 100))) + adj
    bonus = profile.get("location_bonus") or {}
    pts += int(bonus.get(region, 0))
    if region == "india" and any(BLR_RE.search(l) for l in locations):
        pts += int(bonus.get("bengaluru", 0))
    return max(0, min(100, pts)), why, warn


FDE_RE = re.compile(r"forward[- ]?deployed|deployment (engineer|strategist)|\bfde\b", re.I)


def kind_of(j):
    """engineering | fde | science | product (board filter)."""
    role = j.get("role") or scrape.role_type(j["title"]) or "engineering"
    return "fde" if FDE_RE.search(j["title"]) else role


def build(jobs, out=OUT, seen_path=SEEN):
    with open(PROFILE) as f:
        profile = yaml.safe_load(f)
    rules = {s: _rules(profile, s) for s in ("skills", "seniority", "off_profile")}
    stages = scrape.load_stages()
    academic = set(scrape.load_academic())

    groups = {}
    for j in jobs:
        c = j["company"]
        region = ("contract" if j.get("contract") else "academic" if c in academic else
                  "us_h1b" if j.get("us_h1b") else "us_hub" if j.get("us_hub") else
                  "abroad" if j.get("abroad") else "india")
        key = f"{c}|{j['title'].strip().lower()}|{region}"
        g = groups.setdefault(key, {"key": key, "company": c, "title": j["title"].strip(),
                                    "region": region,
                                    "stage": "contract" if j.get("contract") else stages.get(c, "other"),
                                    "url": j["url"], "pay": j.get("pay", ""), "locations": [], "ids": []})
        g.setdefault("kind", kind_of(j))
        loc = (j.get("location") or "").strip() or ("Remote" if j.get("remote") else "")
        if loc and loc not in g["locations"]:
            g["locations"].append(loc)
        g["ids"].append(scrape.contract_key(j) if j.get("contract") else scrape.job_key(c, j))

    today = dt.date.today().isoformat()
    seen = {}
    if os.path.exists(seen_path):
        with open(seen_path) as f:
            seen = json.load(f)
    first_build = seen.get("_first_build") or today
    known = seen.get("jobs", {})
    for key, g in groups.items():
        g["first_seen"] = known.setdefault(key, today)
        g["score"], g["why"], g["warn"] = score(g["title"], g["region"], g["locations"],
                                                profile, rules)
    with open(seen_path, "w") as f:
        json.dump({"_first_build": first_build, "jobs": {k: known[k] for k in groups}}, f)

    rows = sorted(groups.values(), key=lambda g: (-g["score"], g["company"].lower()))
    meta = {"name": profile.get("name", ""), "summary": profile.get("summary", ""),
            "built": dt.datetime.now().strftime("%d %b %Y, %H:%M"),
            "first_build": first_build, "today": today}
    data = json.dumps({"meta": meta, "jobs": rows}, separators=(",", ":"))
    page = TEMPLATE.replace("/*DATA*/null", data.replace("</", "<\\/"))
    page = page.replace("{{NAME}}", html.escape(meta["name"]))
    with open(out, "w") as f:
        f.write(page)
    print(f"Wrote {out}: {len(rows)} openings ({len(jobs)} postings), "
          f"{sum(1 for r in rows if r['score'] >= 50)} scoring 50+.", file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path in ("/", "/index.html", "/board.html"):
            with open(OUT, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, json.dumps(_load_apps()))
        self._send(404, "{}")

    def do_POST(self):
        if self.path != "/api/status":
            return self._send(404, "{}")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            key = str(body["key"])
        except (ValueError, KeyError):
            return self._send(400, "{}")
        with _lock:
            apps = _load_apps()
            if body.get("status"):
                apps[key] = {"status": str(body["status"]), "note": str(body.get("note", "")),
                             "updated": dt.date.today().isoformat(),
                             **{k: body[k] for k in ("company", "title", "url") if k in body}}
            else:
                apps.pop(key, None)
            tmp = APPS + ".tmp"
            with open(tmp, "w") as f:
                json.dump(apps, f, indent=1)
            os.replace(tmp, APPS)
        self._send(200, "{}")


_lock = threading.Lock()


def _load_apps():
    if not os.path.exists(APPS):
        return {}
    with open(APPS) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-scan", action="store_true", help="re-rank the last scan (board_jobs.json)")
    ap.add_argument("--no-serve", action="store_true", help="only write board.html")
    ap.add_argument("--jobs", help="build from this matches JSON instead of scanning")
    ap.add_argument("--out", default=OUT, help="where to write the page (default board.html)")
    ap.add_argument("--seen", default=SEEN, help="first-seen dates file (default board_seen.json)")
    args = ap.parse_args()

    if args.jobs or args.no_scan:
        path = args.jobs or JOBS_CACHE
        if not os.path.exists(path):
            sys.exit(f"{path} not found; run without --no-scan first.")
        with open(path) as f:
            jobs = json.load(f)
    else:
        jobs = scan()
    build(jobs, args.out, args.seen)
    if args.no_serve:
        return
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}/"
    print(f"Serving {url} (Ctrl+C to stop)", file=sys.stderr)
    threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Board</title>
<style>
:root{--bg:#f6f6f3;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dc;--accent:#2f5bd3;
--accent-ink:#fff;--good:#1f7a4d;--good-bg:#e3f3ea;--warn:#a2551a;--warn-bg:#fbeee2;--chip:#efeee9;
--hi:#1f7a4d;--mid:#b07d10;--lo:#9b9b94}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151514;--panel:#1e1e1c;
--ink:#ecebe6;--muted:#9c9b94;--line:#33322f;--accent:#7c9bff;--accent-ink:#10131c;--good:#6fd29e;
--good-bg:#1d3327;--warn:#f0a868;--warn-bg:#3a2a1c;--chip:#2a2a27;--hi:#6fd29e;--mid:#e3b341;--lo:#6b6b66}}
:root[data-theme="dark"]{--bg:#151514;--panel:#1e1e1c;--ink:#ecebe6;--muted:#9c9b94;--line:#33322f;
--accent:#7c9bff;--accent-ink:#10131c;--good:#6fd29e;--good-bg:#1d3327;--warn:#f0a868;--warn-bg:#3a2a1c;
--chip:#2a2a27;--hi:#6fd29e;--mid:#e3b341;--lo:#6b6b66}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 60px}
header h1{margin:0;font-size:22px;letter-spacing:-.01em}
header p{margin:4px 0 0;color:var(--muted)}
.tabs{display:flex;gap:4px;margin:18px 0 12px;border-bottom:1px solid var(--line);overflow-x:auto}
.tab{background:none;border:0;padding:8px 12px;color:var(--muted);font:inherit;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
.tab.on{color:var(--ink);border-color:var(--accent);font-weight:600}
.tab b{font-weight:500;color:var(--muted);margin-left:4px}
.controls{display:grid;grid-template-columns:1fr auto auto auto;gap:8px;align-items:center}
input[type=search],select{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px 10px}
.regions{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.pill{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:999px;padding:4px 11px;cursor:pointer}
.pill.on{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
.pill span{opacity:.7;margin-left:3px}
.row2{display:flex;gap:16px;align-items:center;flex-wrap:wrap;color:var(--muted);font-size:13px;margin-bottom:10px}
.row2 label{display:flex;gap:6px;align-items:center}
.count{margin-left:auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:8px;display:grid;grid-template-columns:48px 1fr auto;gap:12px;align-items:start}
.score{width:44px;height:44px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:15px;border:3px solid currentColor}
.t{font-weight:600;font-size:15px}
.t a{color:inherit;text-decoration:none}.t a:hover{text-decoration:underline}
.sub{color:var(--muted);font-size:13px;margin-top:1px}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.chip{font-size:12px;background:var(--chip);border-radius:6px;padding:1px 7px}
.chip.g{background:var(--good-bg);color:var(--good)}
.chip.w{background:var(--warn-bg);color:var(--warn)}
.chip.new{background:var(--accent);color:var(--accent-ink);font-weight:600}
.acts{display:flex;flex-direction:column;gap:6px;align-items:stretch;min-width:132px}
.btn{font:inherit;font-size:13px;border-radius:8px;padding:6px 10px;border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer;text-align:center;text-decoration:none}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
.btn-row{display:flex;gap:6px}.btn-row .btn{flex:1;padding:5px 6px}
.btn.on{border-color:var(--accent);color:var(--accent)}
.acts select{font-size:13px;padding:5px 8px}
.note{width:100%;margin-top:6px;font:inherit;font-size:13px;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:5px 8px}
.more{display:block;margin:14px auto;padding:8px 18px}
.empty{color:var(--muted);text-align:center;padding:40px 0}
.mode{font-size:12px;color:var(--muted)}
.mode a{color:var(--accent)}
@media (max-width:640px){.controls{grid-template-columns:1fr 1fr}.controls input{grid-column:1/-1}
.card{grid-template-columns:40px 1fr}.acts{grid-column:1/-1;flex-direction:row;flex-wrap:wrap;min-width:0}
.acts>*{flex:1}.score{width:38px;height:38px;font-size:13px}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>Openings for {{NAME}}</h1>
  <p id="summary"></p>
  <p class="mode"><span id="mode"></span> · <a href="#" id="export">Export tracking</a> · <a href="#" id="import">Import</a><input type="file" id="importf" accept=".json,application/json" hidden></p>
</header>
<nav class="tabs" id="tabs"></nav>
<div class="controls">
  <input type="search" id="q" placeholder="Search title, company, location…">
  <select id="stage"><option value="">All stages</option></select>
  <select id="sort"><option value="score">Best match</option><option value="new">Newest</option><option value="company">Company A–Z</option></select>
  <select id="theme" title="Theme"><option value="">Auto</option><option value="light">Light</option><option value="dark">Dark</option></select>
</div>
<div class="regions" id="kinds"></div>
<div class="regions" id="regions"></div>
<div class="row2">
  <label>Min match <input type="range" id="min" min="0" max="90" step="5" value="40"> <b id="minv">40</b></label>
  <label><input type="checkbox" id="newonly"> New since last build</label>
  <span class="count" id="count"></span>
</div>
<main id="list"></main>
<button class="btn more" id="more" hidden>Show more</button>
</div>
<script>
const DATA = /*DATA*/null;
const REGIONS = {contract:"Contract openings", india:"India & Remote", us_h1b:"US · H-1B sponsors", us_hub:"SF / CA / NYC startups", abroad:"Abroad", academic:"Academic"};
const STAGES = {research:"Research lab", public:"Public / big co", late:"Late stage", series_c:"Series C", series_b:"Series B", series_a:"Series A", seed:"Seed", pre_seed:"Pre-seed", contract:"Contract platform", other:"Other"};
const STATUS = {saved:"Saved", applied:"Applied", interview:"Interviewing", offer:"Offer", rejected:"Rejected", hidden:"Hidden"};
const KINDS = {engineering:"Engineering", fde:"Forward deployed", science:"Scientist", product:"Product management"};
const TABS = [["review","To review"],["saved","Saved"],["applied","Applied"],["hidden","Hidden"]];
const PAGE = 60;
let apps = {}, server = false, tab = "review", region = "", kind = "", shown = PAGE;
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const lsGet = k => { try { return localStorage.getItem(k) } catch(e) { return null } };
const lsSet = (k,v) => { try { localStorage.setItem(k,v) } catch(e) {} };

async function loadApps() {
  try { const r = await fetch("/api/status"); if (r.ok) { apps = await r.json(); server = true; } } catch(e) {}
  if (!server) { try { apps = JSON.parse(lsGet("board-apps") || "{}") } catch(e) { apps = {} } }
  $("mode").textContent = server ? "Tracking saved to applications.json" : "Tracking is saved in this browser; use Export / Import to move it to another device";
}
function setStatus(job, status, note) {
  const cur = apps[job.key] || {};
  if (note === undefined) note = cur.note || "";
  if (!status && !note) delete apps[job.key];
  else apps[job.key] = {status: status || "", note, updated: DATA.meta.today, company: job.company, title: job.title, url: job.url};
  const body = {key: job.key, status: status || (note ? "note" : ""), note, company: job.company, title: job.title, url: job.url};
  if (server) fetch("/api/status", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  else lsSet("board-apps", JSON.stringify(apps));
}
const st = j => (apps[j.key] || {}).status || "";
const inTab = (j, t) => { const s = st(j);
  return t === "review" ? (!s || s === "note") : t === "saved" ? s === "saved" : t === "hidden" ? s === "hidden" : ["applied","interview","offer","rejected"].includes(s); };
const isNew = j => j.first_seen > DATA.meta.first_build && j.first_seen === DATA.meta.today;

function filtered(ignoreRegion, ignoreKind) {
  const q = $("q").value.trim().toLowerCase(), stage = $("stage").value, min = +$("min").value, nw = $("newonly").checked;
  return DATA.jobs.filter(j => inTab(j, tab)
    && (ignoreRegion || !region || j.region === region)
    && (ignoreKind || !kind || j.kind === kind)
    && (!stage || j.stage === stage)
    && (tab !== "review" || j.score >= min)
    && (!nw || isNew(j))
    && (!q || (j.title + " " + j.company + " " + j.locations.join(" ")).toLowerCase().includes(q)));
}
function color(s) { return s >= 65 ? "var(--hi)" : s >= 45 ? "var(--mid)" : "var(--lo)"; }

function card(j) {
  const s = st(j), a = apps[j.key] || {};
  const locs = j.locations.slice(0,3).join(" · ") + (j.locations.length > 3 ? ` +${j.locations.length-3} more` : "");
  const chips = [isNew(j) ? '<span class="chip new">New</span>' : "",
    j.kind !== "engineering" ? `<span class="chip new">${esc(KINDS[j.kind])}</span>` : "",
    `<span class="chip">${esc(REGIONS[j.region])}</span>`, `<span class="chip">${esc(STAGES[j.stage]||j.stage)}</span>`,
    j.pay ? `<span class="chip">${esc(j.pay)}</span>` : "",
    ...j.why.map(w => `<span class="chip g">${esc(w)}</span>`), ...j.warn.map(w => `<span class="chip w">${esc(w)}</span>`)].join("");
  const opts = ['<option value="">Status…</option>', ...Object.entries(STATUS).map(([k,v]) => `<option value="${k}"${s===k?" selected":""}>${v}</option>`)].join("");
  return `<article class="card" data-key="${esc(j.key)}">
    <div class="score" style="color:${color(j.score)}" title="Match score">${j.score}</div>
    <div><div class="t"><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a></div>
      <div class="sub">${esc(j.company)}${locs ? " — " + esc(locs) : ""}</div>
      <div class="chips">${chips}</div>
      ${s && s !== "hidden" || a.note ? `<input class="note" data-act="note" placeholder="Notes (referral, contact, date applied…)" value="${esc(a.note||"")}">` : ""}
    </div>
    <div class="acts">
      <a class="btn primary" href="${esc(j.url)}" target="_blank" rel="noopener" data-act="apply">Apply ↗</a>
      <div class="btn-row"><button class="btn${s==="saved"?" on":""}" data-act="saved">Save</button>
      <button class="btn${s==="applied"?" on":""}" data-act="applied">Applied</button>
      <button class="btn${s==="hidden"?" on":""}" data-act="hidden" title="Not interested">Hide</button></div>
      <select data-act="status">${opts}</select>
    </div></article>`;
}

function render(keep) {
  if (!keep) shown = PAGE;
  $("tabs").innerHTML = TABS.map(([k,v]) => `<button class="tab${tab===k?" on":""}" data-tab="${k}">${v}<b>${DATA.jobs.filter(j => inTab(j,k)).length}</b></button>`).join("");
  const base = filtered(true), counts = {};
  base.forEach(j => counts[j.region] = (counts[j.region]||0) + 1);
  $("regions").innerHTML = `<button class="pill${!region?" on":""}" data-region="">All<span>${base.length}</span></button>` +
    Object.entries(REGIONS).map(([k,v]) => `<button class="pill${region===k?" on":""}" data-region="${k}">${v}<span>${counts[k]||0}</span></button>`).join("");
  const kb = filtered(false, true), kc = {};
  kb.forEach(j => kc[j.kind] = (kc[j.kind]||0) + 1);
  $("kinds").innerHTML = `<button class="pill${!kind?" on":""}" data-kind="">All roles<span>${kb.length}</span></button>` +
    Object.entries(KINDS).map(([k,v]) => `<button class="pill${kind===k?" on":""}" data-kind="${k}">${v}<span>${kc[k]||0}</span></button>`).join("");
  let rows = filtered(false);
  const sort = $("sort").value;
  if (sort === "new") rows.sort((a,b) => b.first_seen.localeCompare(a.first_seen) || b.score - a.score);
  else if (sort === "company") rows.sort((a,b) => a.company.localeCompare(b.company) || b.score - a.score);
  $("count").textContent = `${rows.length} opening${rows.length===1?"":"s"}`;
  $("list").innerHTML = rows.length ? rows.slice(0, shown).map(card).join("") : '<p class="empty">Nothing here with these filters.</p>';
  $("more").hidden = rows.length <= shown;
}

const byKey = new Map(DATA.jobs.map(j => [j.key, j]));
$("list").addEventListener("click", e => {
  const el = e.target.closest("[data-act]"); if (!el) return;
  const j = byKey.get(el.closest(".card").dataset.key), act = el.dataset.act;
  if (act === "apply") { if (!st(j)) { setStatus(j, "saved"); setTimeout(() => render(true), 300); } return; }
  if (act === "status" || act === "note") return;
  setStatus(j, st(j) === act ? "" : act); render(true);
});
$("list").addEventListener("change", e => {
  const el = e.target.closest("[data-act]"); if (!el) return;
  const j = byKey.get(el.closest(".card").dataset.key);
  if (el.dataset.act === "status") { setStatus(j, el.value); render(true); }
  if (el.dataset.act === "note") setStatus(j, st(j) || "note", el.value);
});
$("tabs").addEventListener("click", e => { const b = e.target.closest("[data-tab]"); if (b) { tab = b.dataset.tab; render(); } });
$("regions").addEventListener("click", e => { const b = e.target.closest("[data-region]"); if (b) { region = b.dataset.region; render(); } });
["q","stage","sort","newonly"].forEach(id => $(id).addEventListener("input", () => render()));
$("min").addEventListener("input", () => { $("minv").textContent = $("min").value; render(); });
$("kinds").addEventListener("click", e => { const b = e.target.closest("[data-kind]"); if (b) { kind = b.dataset.kind; render(); } });
$("export").addEventListener("click", e => { e.preventDefault();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(apps, null, 1)], {type: "application/json"}));
  a.download = "applications.json"; a.click(); });
$("import").addEventListener("click", e => { e.preventDefault(); $("importf").click(); });
$("importf").addEventListener("change", async () => {
  const f = $("importf").files[0]; if (!f) return;
  let got; try { got = JSON.parse(await f.text()); } catch(e) { alert("Not a valid applications.json"); return; }
  Object.entries(got).forEach(([k, v]) => { if (!v || typeof v !== "object") return; apps[k] = v;
    if (server) fetch("/api/status", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({key: k, ...v})}); });
  if (!server) lsSet("board-apps", JSON.stringify(apps));
  $("importf").value = ""; render(); });
$("more").addEventListener("click", () => { shown += PAGE; render(true); });
$("theme").addEventListener("change", () => { const v = $("theme").value; v ? document.documentElement.dataset.theme = v : delete document.documentElement.dataset.theme; lsSet("board-theme", v); });
const th = lsGet("board-theme"); if (th) { $("theme").value = th; document.documentElement.dataset.theme = th; }
$("stage").innerHTML += Object.entries(STAGES).map(([k,v]) => `<option value="${k}">${v}</option>`).join("");
$("summary").textContent = `${DATA.meta.summary} · ${DATA.jobs.length} openings · updated ${DATA.meta.built}`;
loadApps().then(() => render());
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
