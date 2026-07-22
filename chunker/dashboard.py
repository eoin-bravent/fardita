#!/usr/bin/env python3
"""chunker dashboard — fleet view + control panel over stores/<AG>/state.json.

Reads ONLY state.json (never the big store), so it always agrees with the CLI. Shows, per
regulation: pipeline step status, the corpus HISTORY span (from -> to effective dates),
honest reg coverage (covered / accounted / missing, companions out of the denominator), a
first-class COMPANION line, and health (invariants + date-tracking) with plain-English help.
It can also LAUNCH work: `chunker build`/`verify` per-agency or fleet-wide (into whatever
--base it was started on), and stop a run. Stdlib only.

  python -m chunker.cli dashboard [--port 8643] [--base stores_staging]
"""
import os
import sys
import json
import time
import subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from chunker import paths

_BASE = None                 # store root this dashboard views/builds (None => stores/)
_PY = sys.executable         # the interpreter running the dashboard (has lxml) -> subprocesses
_procs = {}                  # key ("ALL" or agency) -> Popen
_active = {"agencies": set(), "verb": None}


# ---------------------------------------------------------------- data
def _state(agency):
    p = os.path.join(paths.store_dir(agency, _BASE), "state.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _running(agency):
    if agency in _procs and _procs[agency].poll() is None:
        return True
    all_p = _procs.get("ALL")
    return bool(all_p and all_p.poll() is None and agency in _active["agencies"])


def fleet():
    out = []
    for a in paths.agencies():
        st = _state(a)
        v = st.get("verification") or {}
        comp = st.get("companion") or {}
        dc = st.get("dates") or {}
        out.append({
            "agency": a, "built": bool(st), "running": _running(a),
            "steps": {k: x.get("status") for k, x in (st.get("steps") or {}).items()},
            "editions": v.get("editions"), "rows": v.get("rows"),
            "floor": dc.get("floor"), "ceiling": dc.get("ceiling"),
            "current": dc.get("current"),
            "covered": v.get("covered_pct"), "accounted": v.get("accounted_pct"),
            "missing": v.get("missing_sections"),
            "invariants_ok": v.get("invariants_ok"),
            "dates_ok": dc.get("ok"),
            "companion": ({"units": comp.get("current_units"), "pct": comp.get("captured_pct"),
                           "classes": comp.get("by_class") or {},
                           "out": comp.get("out_of_ditamap_total") or 0}
                          if comp.get("present") else None),
            "certs": v.get("certificates") or [],
        })
    return out


def verify_gate():
    """Read-only gate of each BUILT store vs docs/BASELINE.json (same rule as `chunker
    verify`): invariants clean, 0 missing, dates tracked, covered% not regressed. Edition
    COUNT is NOT gated (it legitimately grows). Returns per-agency PASS/FAIL + reasons."""
    try:
        base = json.load(open(os.path.join(paths.ROOT, "docs", "BASELINE.json"),
                              encoding="utf-8"))["agencies"]
    except Exception:
        base = {}
    from chunker.ingest.canon import NO_REPO       # agencies with no upstream GitHub repo
    out = []
    for a in fleet():
        if not a["built"]:
            continue
        b = base.get(a["agency"], {})
        cov, miss, inv, dates = a["covered"], a["missing"], a["invariants_ok"], a["dates_ok"]
        if inv is None:            # built but not yet audited (mid-build) -> pending, not a fail
            out.append({"agency": a["agency"], "ok": None, "covered": cov,
                        "baseline_covered": b.get("covered"),
                        "reasons": ["building / not yet audited"]})
            continue
        cov_ok = (cov is None or b.get("covered") is None or cov >= b["covered"] - 0.5)
        # canon must have run unless the agency has no GitHub source (NO_REPO) -- else it is
        # silently archive-only (the AGAR dead-symlink gap) yet clean on every other metric.
        canon_status = (a.get("steps") or {}).get("canon")
        canon_ok = (a["agency"] in NO_REPO) or (canon_status == "ok")
        reasons = []
        if not inv:
            reasons.append("invariants FAIL")
        if miss:
            reasons.append(f"{miss} missing sections")
        if not dates:
            reasons.append("date gap")
        if not cov_ok:
            reasons.append(f"covered {cov} < baseline {b.get('covered')}")
        if not canon_ok:
            reasons.append(f"canon {canon_status or 'MISSING'} (built archive-only)")
        out.append({"agency": a["agency"],
                    "ok": bool(inv) and miss == 0 and bool(dates) and cov_ok and canon_ok,
                    "covered": cov, "baseline_covered": b.get("covered"), "reasons": reasons})
    return out


# ---------------------------------------------------------------- actions
def _logf(agency):
    d = paths.store_dir(agency if agency != "ALL" else "", _BASE)
    os.makedirs(d or ".", exist_ok=True)
    return open(os.path.join(d or ".", "run.log"), "a", encoding="utf-8")


def launch(agency, verb, except_="", parallel=1, fresh=False, force=False):
    """Spawn `chunker <verb> --agency ...` as a child. agency 'ALL' expands to every agency
    minus `except_`; parallel>1 builds N agencies concurrently; fresh archives the prior store
    to prerebuild/ first (clean in-place rebuild)."""
    if agency in _procs and _procs[agency].poll() is None:
        return False
    if agency == "ALL":
        skip = {s.strip().upper() for s in except_.split(",") if s.strip()}
        targets = [a for a in paths.agencies() if a not in skip]
        agency_arg = ",".join(targets)
        _active["agencies"] = set(targets)
        _active["verb"] = verb
    else:
        agency_arg = agency
    cmd = [_PY, "-m", "chunker.cli", verb, "--agency", agency_arg]
    if _BASE:
        cmd += ["--base", _BASE]
    if verb == "build":
        if parallel > 1:
            cmd += ["--parallel", str(parallel)]
        if fresh:
            cmd.append("--fresh")
        if force:
            cmd.append("--force")
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    kw = {}
    if os.name != "nt":
        kw["start_new_session"] = True
    _procs[agency] = subprocess.Popen(cmd, cwd=paths.ROOT, stdout=_logf(agency),
                                      stderr=subprocess.STDOUT, env=env, **kw)
    return True


def stop(agency):
    killed = False
    for key in ([agency] if agency != "ALL" else list(_procs)):
        p = _procs.get(key)
        if p and p.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True)
                else:
                    import signal
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                killed = True
            except Exception:
                pass
    if agency == "ALL":
        _active["agencies"] = set()
    return killed


HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>FARDITA — fleet</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--txt:#e6edf3;--dim:#8b949e;--ok:#3fb950;
--warn:#d29922;--bad:#f85149;--acc:#bc8cff;--comp:#58a6ff;--run:#58a6ff}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--txt)}
header{padding:22px 30px 6px;display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:10px}
h1{margin:0;font-size:23px}h1 b{color:var(--acc)}.sub{color:var(--dim);margin-top:4px}
.sub code{color:var(--comp)}
button{background:#21262d;color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:5px 11px;cursor:pointer;font-weight:600;font-size:13px}
button:hover{border-color:var(--acc)}button.primary{background:#1f6feb;border-color:#1f6feb}
button.bad{border-color:var(--bad);color:var(--bad)}button.sm{padding:3px 8px;font-size:12px}
.hero{display:flex;gap:12px;padding:12px 30px;flex-wrap:wrap}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 16px;min-width:120px}
.stat .n{font-size:22px;font-weight:700}.stat .l{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:1px}
.help{padding:2px 30px 10px}.help button{margin-right:8px}
table{border-collapse:collapse;width:calc(100% - 60px);margin:6px 30px 40px}
th{color:var(--dim);text-transform:uppercase;font-size:11px;letter-spacing:.5px;text-align:left;padding:7px 9px;border-bottom:1px solid var(--line)}
th.help-h{cursor:help;text-decoration:underline dotted var(--dim)}
td{padding:8px 9px;border-bottom:1px solid #21262d;vertical-align:middle}tr:hover td{background:#161b2280}
.chip{display:inline-block;border-radius:11px;padding:1px 7px;font-size:10.5px;font-weight:700;border:1px solid var(--line);color:var(--dim);margin-right:3px}
.chip.ok{background:#12261a;color:var(--ok);border-color:#1f4d2e}.chip.bad{background:#2b1214;color:var(--bad);border-color:#67282b}
.chip.warn{background:#2b2410;color:var(--warn);border-color:#5c4a1a}.chip.run{background:#101f33;color:var(--run);border-color:#1e4976}
.chip.skip{opacity:.5}
.bar{display:inline-block;height:9px;border-radius:5px;background:#21262d;width:90px;vertical-align:middle;overflow:hidden;border:1px solid var(--line);margin-right:5px}
.bar i{display:block;height:9px}
.comp{background:#0e1b2e;border:1px solid #1e4976;border-radius:8px;padding:2px 8px;color:var(--comp);font-size:12px;cursor:pointer}
.comp b{color:#9ecbff}.none{color:#485460}.mono{font-family:Consolas,monospace;font-size:12px;color:var(--dim)}
.spin{display:inline-block;width:10px;height:10px;border:2px solid var(--run);border-top-color:transparent;border-radius:50%;animation:r 1s linear infinite;margin-left:5px}
@keyframes r{to{transform:rotate(360deg)}}
#modal{position:fixed;inset:0;background:#000a;display:none;align-items:flex-start;justify-content:center;overflow:auto;padding:40px;z-index:9}
#mbox{background:var(--card);border:1px solid var(--line);border-radius:12px;max-width:900px;width:100%;padding:24px 28px}
#mbox h2{margin:0 0 4px}#mbox h3{color:var(--acc);margin:18px 0 6px}.kv{color:var(--dim)}.kv b{color:var(--txt)}
#mbox table{width:100%;margin:8px 0}
</style></head><body>
<header><div><h1>FARDITA <b>fleet</b></h1>
<div class="sub">honest regulation coverage (companions excluded from the denominator) &middot; companion docs captured as a separate class &middot; viewing <code id="baselbl"></code></div></div>
<div><button class=primary onclick="runAll()">&#9654; Build&hellip;</button>
<button onclick="verifyAll()">&#10003; Verify all</button>
<button class=bad onclick="stopRun('ALL')">&#9632; Stop all</button></div></header>
<div class="hero" id="hero"></div>
<div class="help">
 <button onclick="helpActions()">&#10067; What do Build &amp; Verify do?</button>
 <button onclick="helpCoverage()">&#10067; Covered / Accounted / Missing</button>
 <button onclick="helpCompanions()">&#10067; Companion documents</button>
 <button onclick="helpStages()">&#10067; Pipeline stages</button>
 <button onclick="helpHealth()">&#10067; History, Dates &amp; Invariants</button></div>
<table id="tbl"><thead><tr>
 <th>Regulation</th><th>Pipeline</th><th>Editions</th>
 <th class="help-h" onclick="helpHealth()">History (from &rarr; to)</th><th>Rows</th>
 <th class="help-h" onclick="helpCoverage()">Coverage (covered &middot; accounted &middot; missing)</th>
 <th>Companion documents</th>
 <th class="help-h" onclick="helpHealth()">Dates</th><th class="help-h" onclick="helpHealth()">Inv.</th><th></th>
</tr></thead><tbody><tr><td colspan=10 style="padding:16px;color:var(--dim)">Loading&hellip;</td></tr></tbody></table>
<div id="modal" onclick="if(event.target.id=='modal')this.style.display='none'"><div id="mbox"></div></div>
<script>
let DATA=[];
const pct=x=>x==null?'&mdash;':x.toFixed(1)+'%';
async function load(){try{const r=await fetch('/api/fleet',{cache:'no-store'});const j=await r.json();
  DATA=j.fleet;document.getElementById('baselbl').textContent=j.base;render();}catch(e){}}
function render(){
 const built=DATA.filter(a=>a.built),cov=built.filter(a=>a.covered!=null).map(a=>a.covered);
 const comp=built.filter(a=>a.companion),units=comp.reduce((s,a)=>s+(a.companion.units||0),0);
 const running=DATA.filter(a=>a.running).length;
 document.getElementById('hero').innerHTML=
  `<div class=stat><div class=n>${built.length}/${DATA.length}</div><div class=l>Built</div></div>
   <div class=stat><div class=n>${built.reduce((s,a)=>s+(a.rows||0),0).toLocaleString()}</div><div class=l>Reg rows</div></div>
   <div class=stat><div class=n>${cov.length?Math.min(...cov).toFixed(1)+'%':'&mdash;'}</div><div class=l>Worst covered</div></div>
   <div class=stat><div class=n>${comp.length}</div><div class=l>Agencies w/ companions</div></div>
   <div class=stat><div class=n>${units.toLocaleString()}</div><div class=l>Companion units</div></div>
   ${running?`<div class=stat><div class=n style="color:var(--run)">${running}<span class=spin></span></div><div class=l>Building</div></div>`:''}`;
 const tb=document.querySelector('#tbl tbody');tb.innerHTML='';
 for(const a of DATA){
  const tr=document.createElement('tr');
  const act=a.running?`<button class="sm bad" onclick="stopRun('${a.agency}')">Stop</button>`
                     :`<button class=sm onclick="run('${a.agency}')">Build</button>`;
  if(!a.built){tr.innerHTML=`<td><b>${a.agency}</b>${a.running?'<span class=spin></span>':''}</td>
    <td colspan=8 class=none>${a.running?'building&hellip;':'not built'}</td><td>${act}</td>`;tb.appendChild(tr);continue;}
  const steps=['survey','backfill','canon','audit'].map(s=>{const v=a.steps[s]||'';
    const c=v=='ok'?'ok':v=='FAIL'?'bad':v=='ATTENTION'?'warn':v?'skip':'skip';
    return `<span class="chip ${c}" title="${s}: ${v||'—'}">${s[0].toUpperCase()}</span>`;}).join('');
  const missOk=(a.missing||0)===0;
  const cc=a.covered==null?'#485460':a.covered>=80?'var(--ok)':a.covered>=50?'var(--warn)':'var(--bad)';
  const cover=`<span class=bar><i style="width:${a.covered||0}%;background:${cc}"></i></span><b>${pct(a.covered)}</b> &middot; ${pct(a.accounted)} &middot; <b style="color:${missOk?'var(--ok)':'var(--bad)'}">${missOk?'0 miss':a.missing+' MISS'}</b>`;
  const hist=a.floor?`<span class=mono>${a.floor} &rarr; ${a.current?'present':(a.ceiling||'')}</span>`:'<span class=none>&mdash;</span>';
  let cdoc='<span class=none>&mdash;</span>';
  if(a.companion){const cl=Object.entries(a.companion.classes).map(([k,v])=>`${v.captured} ${k}`).join(', ');
   const og=a.companion.out?` <span style="color:var(--warn)" title="companion files outside the ditamap, not yet captured">+${a.companion.out} off-map</span>`:'';
   cdoc=`<span class=comp onclick="detail('${a.agency}')" title="click for detail"><b>${a.companion.units}</b> units (${cl}) &middot; ${pct(a.companion.pct)} captured${og}</span>`;}
  tr.innerHTML=`<td><b>${a.agency}</b>${a.running?'<span class=spin></span>':''}</td><td>${steps}</td>
   <td>${a.editions||'&mdash;'}</td><td>${hist}</td><td>${(a.rows||0).toLocaleString()}</td>
   <td>${cover}</td><td>${cdoc}</td>
   <td>${a.dates_ok==null?'&mdash;':a.dates_ok?'<span class="chip ok" title="every edition has an effective_date; every GitHub edition/row a commit_date">tracked</span>':'<span class="chip bad">gap</span>'}</td>
   <td>${a.invariants_ok==null?'&mdash;':a.invariants_ok?'<span class="chip ok" title="valid citations; no overlapping/duplicate version intervals; current-flag consistent">clean</span>':'<span class="chip bad">FAIL</span>'}</td>
   <td>${act} <button class=sm onclick="detail('${a.agency}')">Detail</button></td>`;
  tb.appendChild(tr);}
}
async function run(a){if(!confirm('Rebuild '+a+' in place? (prior store archived to prerebuild/, then rebuilt fresh + audited)'))return;
  await fetch('/api/run',{method:'POST',body:JSON.stringify({agency:a,verb:'build',fresh:true,force:true})});setTimeout(load,600);}
async function runAll(){const ex=prompt('Rebuild ALL agencies in place (prior stores archived to prerebuild/). Skip which? (comma-separated, blank = none)','');if(ex===null)return;
  const p=prompt('How many agencies at once? (parallel; 1 = sequential)','4');if(p===null)return;
  if(!confirm('Rebuild all agencies (except: '+(ex||'none')+'), '+(parseInt(p)||1)+' at a time — fresh + audited — into '+document.getElementById('baselbl').textContent+'?'))return;
  await fetch('/api/run',{method:'POST',body:JSON.stringify({agency:'ALL',verb:'build',except:ex,parallel:parseInt(p)||1,fresh:true})});setTimeout(load,600);}
async function verifyAll(){const r=await (await fetch('/api/verify',{cache:'no-store'})).json();const v=r.verify;
 const pass=v.filter(x=>x.ok===true).length, pend=v.filter(x=>x.ok===null).length;
 let h=`<h2>Verify — gate vs BASELINE (${pass}/${v.length} pass${pend?', '+pend+' building':''})</h2>
 <div class=kv>Read-only check of each built store against the frozen docs/BASELINE.json oracle: invariants clean, 0 missing sections, dates tracked, and covered% not regressed. Edition COUNT is not gated (the marker-based replay legitimately adds editions). Nothing is rebuilt.</div>
 <table><tr><th>agency</th><th>result</th><th>covered</th><th>baseline</th><th>notes</th></tr>`;
 for(const x of v){const chip=x.ok===true?'<span class="chip ok">PASS</span>':x.ok===null?'<span class="chip warn">building</span>':'<span class="chip bad">FAIL</span>';
  h+=`<tr><td>${x.agency}</td><td>${chip}</td><td>${pct(x.covered)}</td><td>${x.baseline_covered==null?'&mdash;':x.baseline_covered+'%'}</td><td style="color:var(--warn)">${(x.reasons||[]).join('; ')}</td></tr>`;}
 h+='</table>';modal(h);}
async function stopRun(a){if(!confirm('Stop '+(a=='ALL'?'all runs':a)+'?'))return;
  await fetch('/api/stop',{method:'POST',body:JSON.stringify({agency:a})});setTimeout(load,600);}
function modal(h){document.getElementById('mbox').innerHTML=h+'<div style="margin-top:16px"><button onclick="document.getElementById(\'modal\').style.display=\'none\'">Close</button></div>';document.getElementById('modal').style.display='flex';}
function helpCoverage(){modal(`<h2>What the coverage numbers mean</h2>
 <div class=kv>Measured on the newest edition of each format, over <b>regulation text only</b> — companion documents (MP/annex/attachment/appendix) are a different document class and are excluded from this denominator (captured separately, see the Companion column).</div>
 <h3>Covered</h3><div class=kv>Share of the source text that ended up <b>inside a saved rule</b>. The strict, no-interpretation floor.</div>
 <h3>Accounted</h3><div class=kv>Covered <b>plus</b> text deliberately set aside as non-rule (a heading, a table of contents, page navigation, furniture). Whatever's left is truly unexplained residue — the alarm we watch. Always &ge; Covered.</div>
 <h3>Missing</h3><div class=kv>The number of sections the publisher clearly wrote body text for that produced <b>no saved rule at all</b> — a genuine dropped section. <b>0 means nothing was dropped</b> (the most important number).</div>`);}
function helpStages(){modal(`<h2>Pipeline stages</h2>
 <h3>Survey</h3><div class=kv>Classify each archived edition folder into its markup "era" so the right parser is used.</div>
 <h3>Backfill</h3><div class=kv>Parse every archived edition and merge oldest&rarr;newest into the dated version history (identical text collapses; only real changes make a new version).</div>
 <h3>Canon</h3><div class=kv>Ingest the GitHub editions on top, grouped into published editions by branch marker, and capture companion documents into companion.json.</div>
 <h3>Audit</h3><div class=kv>Verify store invariants + prove text conservation on the newest edition of each era (produces Covered / Accounted / Missing).</div>
 <div class=kv style="margin-top:8px">Chip colors: green ok &middot; amber attention &middot; red failed &middot; dim skipped.</div>`);}
function helpHealth(){modal(`<h2>History, Dates &amp; Invariants</h2>
 <h3>History (from &rarr; to)</h3><div class=kv>The span of legal <b>effective dates</b> this corpus covers — the earliest edition's date &rarr; the latest edition's date (or <b>present</b> if the newest version is still in force). This is the real temporal reach of the stored history.</div>
 <h3>Dates</h3><div class=kv><b>tracked</b> = every edition carries an effective_date and every GitHub-sourced edition/row carries a commit_date (so each version is both legally dated and traceable to its source commit). <b>gap</b> = something is missing a date.</div>
 <h3>Inv. (invariants)</h3><div class=kv><b>clean</b> = the versioned store is internally consistent: valid citations, no overlapping or duplicate version intervals for a section, and the "current" flag matches the open interval. <b>FAIL</b> = a structural problem to fix.</div>`);}
function helpActions(){modal(`<h2>What the action buttons do</h2>
 <h3>Build (one agency) &amp; Build&hellip; (all)</h3><div class=kv>A clean <b>in-place rebuild</b>, fully automatic &mdash; no staging, no manual swap. For each agency it (1) archives the prior store aside to <b>stores/&lt;AG&gt;/prerebuild/</b> (a non-destructive backup), (2) re-surveys and re-ingests every archived edition oldest&rarr;newest, then adds the GitHub editions (grouped into published editions by branch marker) and captures companion documents, and (3) audits the result &mdash; all in one shot. <b>Build&hellip;</b> runs the whole fleet, N agencies at a time (you choose N), and is <b>resumable</b>: it skips agencies already rebuilt, so an interruption doesn't restart everything. Per-agency <b>Build</b> forces a rebuild of just that one.</div>
 <h3>Verify all</h3><div class=kv>A <b>read-only gate</b> &mdash; it rebuilds nothing. It checks every built store against the frozen <b>docs/BASELINE.json</b> oracle and reports PASS/FAIL per agency: invariants clean, 0 missing sections, dates fully tracked, and covered% not regressed below baseline. Edition COUNT is deliberately not gated (the marker-based GitHub replay legitimately adds editions, and honest reg-only covered% can rise as companions leave the denominator). Run it after a rebuild to confirm nothing regressed.</div>
 <h3>Stop</h3><div class=kv>Terminates a running build (one agency, or all). Because a build is resumable, stopping and re-running is safe.</div>`);}
function helpCompanions(){modal(`<h2>Companion documents</h2>
 <div class=kv>Alongside the numbered regulation, each agency ships a different <b>document class</b> &mdash; supporting material that is not the regulation text itself. The pipeline used to drop these; now it <b>captures</b> them into a separate <b>companion.json</b> store (kept out of the regulation store so a search can include or exclude them), versioned and dated exactly like the regulation. Keeping them out is also why the regulation's <b>covered%</b> is now honest &mdash; companion text no longer drags it down.</div>
 <h3>The classes (doc_class)</h3>
 <table><tr><th>code</th><th>what it is</th><th>example</th></tr>
 <tr><td><b>mp</b></td><td>Mandatory Procedures (binding how-to that implements a section)</td><td>DAFFARS MP5301.601</td></tr>
 <tr><td><b>ig</b></td><td>Informational Guidance (non-binding explanation)</td><td>DAFFARS IG5301.601</td></tr>
 <tr><td><b>annex</b></td><td>Annex &mdash; J&amp;A templates, business-clearance memos</td><td>NMCARS Annex 1</td></tr>
 <tr><td><b>attachment</b></td><td>Attachment / fillable form template</td><td>SOFARS Attachment 5601-1</td></tr>
 <tr><td><b>appendix</b></td><td>Appendix (may be a form, or genuine regulation &mdash; see promotion)</td><td>AFARS Appendix AA</td></tr>
 <tr><td><b>exhibit</b></td><td>Exhibit</td><td>(none present yet)</td></tr></table>
 <h3>What the numbers mean</h3>
 <div class=kv><b>captured</b> = companion units actually stored (only units with real body text). <b>available (body)</b> = the body-bearing companion units the current source ships &mdash; so <b>captured / available</b> is companion completeness (100% = every body-bearing companion was captured; empty container files never count). <b>out-of-map</b> = companion files in the repo its ditamap does not reference, so they are not captured yet (a known gap, e.g. DFARS's hashed Appendix-id* files).</div>`);}
function detail(ag){const a=DATA.find(x=>x.agency==ag);let h=`<h2>${ag}</h2>`;
 if(a.companion){h+=`<h3>Companion documents</h3><div class=kv>${a.companion.units} current units &middot; ${pct(a.companion.pct)} body-bearing captured${a.companion.out?` &middot; <b style="color:var(--warn)">${a.companion.out} out-of-ditamap (not captured)</b>`:''}</div>
  <table><tr><th>class</th><th>captured</th><th>available (body)</th><th>out-of-map</th></tr>`;
  for(const [k,v] of Object.entries(a.companion.classes))h+=`<tr><td>${k}</td><td>${v.captured}</td><td>${v.available_body}</td><td>${v.out_of_ditamap}</td></tr>`;h+='</table>';}
 h+='<h3>Coverage certificates (per era, honest reg-only)</h3><table><tr><th>era</th><th>covered</th><th>accounted</th><th>missing</th><th>src chars</th></tr>';
 for(const c of a.certs){if(c.error){h+=`<tr><td>${c.era}</td><td colspan=4 style="color:var(--bad)">${c.error}</td></tr>`;continue;}
  h+=`<tr><td>${c.era}</td><td>${pct(c.covered_pct)}</td><td>${pct(c.accounted_pct)}</td><td>${c.missing_count||0}</td><td class=mono>${(c.source_chars||0).toLocaleString()}</td></tr>`;}
 h+='</table>';modal(h);}
load();setInterval(load,3000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/fleet"):
            self._send(json.dumps({"fleet": fleet(), "base": _BASE or "stores/"}),
                       "application/json")
        elif self.path.startswith("/api/verify"):
            self._send(json.dumps({"verify": verify_gate()}), "application/json")
        else:
            self._send(HTML, "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/run":
            ok = launch((body.get("agency") or "").upper(), body.get("verb") or "build",
                        except_=str(body.get("except") or ""),
                        parallel=int(body.get("parallel") or 1),
                        fresh=bool(body.get("fresh")), force=bool(body.get("force")))
            self._send(json.dumps({"launched": ok}), "application/json")
        elif self.path == "/api/stop":
            self._send(json.dumps({"stopped": stop((body.get("agency") or "ALL").upper())}),
                       "application/json")
        else:
            self._send(json.dumps({"error": "not found"}), "application/json")


def main(port=8643, base=None):
    global _BASE
    _BASE = base
    print(f"chunker dashboard: http://localhost:{port}  (base={base or 'stores/'})")
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    srv.serve_forever()
