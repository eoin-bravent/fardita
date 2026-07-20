#!/usr/bin/env python3
"""Web dashboard for the regulation-archive pipeline.

    python dashboard.py            # http://localhost:8642

Reads each agency's stores/<AGENCY>/{pipeline_state,verification}.json and store file,
renders the fleet as a pipeline board (Download -> Survey -> Backfill -> Verify -> LLM)
with drillable coverage-proof certificates, and can launch orchestrator steps. Stdlib
only; all state lives on disk, so the dashboard always agrees with the CLI."""
import os
import sys
import json
import glob
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

PY = sys.executable
_procs = {}


def agencies():
    d = json.load(open(os.path.join(HERE, "data", "agencies.json"), encoding="utf-8"))
    return [a for a in d if not a.startswith("_")]


def _read(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


_store_cache = {}


def store_stats(a):
    """rows/editions/span straight from the store file (covers stores built BEFORE the
    app, like FAR's live pipeline/store). Cached by mtime -- FAR's file is 100+ MB."""
    for d in (os.path.join(ROOT, "stores", a), os.path.join(HERE, "store") if a == "FAR" else ""):
        p = os.path.join(d, f"{a}_store.json") if d else ""
        if p and os.path.exists(p):
            mt = os.path.getmtime(p)
            hit = _store_cache.get(p)
            if hit and hit[0] == mt:
                return hit[1]
            try:
                data = json.load(open(p, encoding="utf-8"))
                eds = data.get("editions", [])
                s = {"rows": len(data.get("rows", [])), "editions": len(eds),
                     "floor": min((e["effective_date"] for e in eds), default=""),
                     "ceiling": max((e["effective_date"] for e in eds), default=""),
                     "store_mb": round(os.path.getsize(p) / 1e6, 1)}
            except Exception:
                s = {}
            _store_cache[p] = (mt, s)
            return s
    return {}


def agency_info(a):
    sdir = os.path.join(ROOT, "stores", a)
    stp = os.path.join(sdir, "pipeline_state.json")
    st = _read(stp)
    ver = _read(os.path.join(sdir, "verification.json"))
    stepobjs = st.get("steps") or {}
    steps = {k: v.get("status", "") for k, v in stepobjs.items()}
    progress = (stepobjs.get("download") or {}).get("progress", "") \
        if steps.get("download") == "running" else ""
    certs = ver.get("certificates", [])
    acc = [c.get("accounted_pct") for c in certs
           if isinstance(c.get("accounted_pct"), (int, float))]
    # running = a live process we own, or a fresh on-disk claim (stale claims -- e.g.
    # after a killed run -- expire after 10 minutes without a state-file update)
    proc_alive = (a in _procs and _procs[a].poll() is None) or \
                 ("ALL" in _procs and _procs["ALL"].poll() is None)
    fresh = os.path.exists(stp) and (os.path.getmtime(stp) > __import__("time").time() - 600)
    running = proc_alive and bool(st.get("running")) or (bool(st.get("running")) and fresh)
    ss = store_stats(a)
    if ss.get("editions") and not steps.get("backfill"):
        steps["backfill"] = "ok"            # store exists although the app never ran it
    return {"agency": a, "steps": steps, "running": running, "progress": progress,
            "rows": ss.get("rows", 0), "editions": ss.get("editions", 0),
            "floor": ss.get("floor", ""), "ceiling": ss.get("ceiling", ""),
            "invariants_ok": ver.get("invariants_ok"),
            "accounted": round(min(acc), 3) if acc else None,   # worst era = honest claim
            "certs_pass": sum(1 for c in certs if c.get("pass")),
            "certs_total": len([c for c in certs if "pass" in c]),
            "queue": ver.get("review_queue_len", 0),
            "store_mb": ss.get("store_mb", 0)}


def agency_detail(a):
    sdir = os.path.join(ROOT, "stores", a)
    ver = _read(os.path.join(sdir, "verification.json"))
    queue = _read(os.path.join(sdir, "review_queue.json")) or []
    eras_file = os.path.join(HERE, "cache", "archive_eras.json" if a == "FAR"
                             else f"{a.lower()}_eras.json")
    eras = {}
    if os.path.exists(eras_file):
        for folder, v in _read(eras_file).items():
            e = eras.setdefault(v["era"], {"folders": 0, "from": "9999", "to": ""})
            e["folders"] += 1
            d = v.get("effective_date") or ""
            if d:
                e["from"] = min(e["from"], d)
                e["to"] = max(e["to"], d)
    bf = _read(os.path.join(sdir, "backfill_report.json"))
    editions = (bf.get("editions") or [])[-8:]
    return {"agency": a, "eras": eras, "verification": ver,
            "queue_sample": queue[:12], "recent_editions": editions}


def launch(agency, steps):
    if agency in _procs and _procs[agency].poll() is None:
        return False
    logdir = os.path.join(ROOT, "stores", agency if agency != "ALL" else "")
    os.makedirs(logdir, exist_ok=True)
    logf = open(os.path.join(logdir, "run.log" if agency != "ALL" else "fleet.log"),
                "a", encoding="utf-8")
    kw = {}
    if os.name != "nt":
        kw["start_new_session"] = True          # own process group -> killable tree
    _procs[agency] = subprocess.Popen(
        [PY, os.path.join(HERE, "orchestrator.py"), "run",
         "--agency", agency.lower() if agency == "ALL" else agency,
         "--steps", steps, "--keep-going"],
        cwd=HERE, stdout=logf, stderr=subprocess.STDOUT, **kw)
    return True


def stop(agency):
    """Kill the run's whole process tree (orchestrator + downloader children) and
    clear the stale 'running' flags so the board doesn't show ghosts."""
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
    targets = agencies() if agency == "ALL" else [agency]
    for a in targets:                            # clear on-disk running claims
        sp = os.path.join(ROOT, "stores", a, "pipeline_state.json")
        st = _read(sp)
        if st.get("running"):
            st["running"] = False
            for k, v in (st.get("steps") or {}).items():
                if v.get("status") == "running":
                    v["status"] = "stopped"
            json.dump(st, open(sp, "w", encoding="utf-8"), indent=1)
    fp = os.path.join(ROOT, "stores", "fleet_state.json")
    if os.path.exists(fp):
        f = _read(fp)
        f["active"] = False
        f["note"] = "stopped by user"
        json.dump(f, open(fp, "w", encoding="utf-8"))
    return killed


HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Acquisition Regulation Store Pipeline</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--txt:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--run:#58a6ff;--acc:#bc8cff}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 'Segoe UI',system-ui,sans-serif;
background:var(--bg);color:var(--txt)}
header{padding:26px 34px 10px;display:flex;justify-content:space-between;align-items:flex-end}
h1{margin:0;font-size:26px}h1 b{color:var(--acc)}.sub{color:var(--dim);margin-top:4px}
.hero{display:flex;gap:14px;padding:16px 34px;flex-wrap:wrap}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 20px;min-width:150px}.stat .n{font-size:26px;font-weight:700}
.stat .l{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:1px}
.flow{display:flex;align-items:center;gap:6px;padding:6px 34px 14px;color:var(--dim);flex-wrap:wrap}
.flow .node{background:var(--card);border:1px solid var(--line);border-radius:20px;
padding:6px 16px;font-weight:600;color:var(--txt)}.flow .arr{color:var(--acc);font-size:18px}
table{border-collapse:collapse;width:calc(100% - 68px);margin:4px 34px 40px}
th{color:var(--dim);text-transform:uppercase;font-size:11px;letter-spacing:1px;
text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:9px 10px;border-bottom:1px solid #21262d;vertical-align:middle}
tr:hover td{background:#161b2280}
.chip{display:inline-block;border-radius:12px;padding:2px 10px;font-size:11px;
font-weight:700;margin-right:4px;border:1px solid var(--line);color:var(--dim);cursor:pointer}
.chip.ok{background:#12261a;color:var(--ok);border-color:#1f4d2e}
.chip.FAIL,.chip.bad{background:#2b1214;color:var(--bad);border-color:#67282b}
.chip.ATTENTION,.chip.warn{background:#2b2410;color:var(--warn);border-color:#5c4a1a}
.chip.running,.chip.run{background:#101f33;color:var(--run);border-color:#1e4976}
.prog{color:var(--run);font-size:11px;font-family:Consolas,monospace;display:block;
max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mono{font-family:Consolas,monospace;font-size:12px;color:var(--dim)}
button{background:#21262d;color:var(--txt);border:1px solid var(--line);border-radius:7px;
padding:5px 12px;cursor:pointer;font-weight:600}button:hover{border-color:var(--acc)}
button.primary{background:#1f6feb;border-color:#1f6feb}
.spin{display:inline-block;width:11px;height:11px;border:2px solid var(--run);
border-top-color:transparent;border-radius:50%;animation:r 1s linear infinite;margin-left:6px}
@keyframes r{to{transform:rotate(360deg)}}
.acct{cursor:pointer;text-decoration:underline dotted var(--dim)}
#modal{position:fixed;inset:0;background:#000a;display:none;align-items:flex-start;
justify-content:center;overflow:auto;padding:40px}
#mbox{background:var(--card);border:1px solid var(--line);border-radius:12px;
max-width:980px;width:100%;padding:24px 28px}
#mbox h2{margin:0 0 4px}#mbox h3{color:var(--acc);margin:22px 0 8px}
.sbar{display:flex;height:22px;border-radius:6px;overflow:hidden;border:1px solid var(--line);margin:6px 0}
.sbar div{height:22px}.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--dim);font-size:12px;margin:4px 0 10px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
.sample{background:#0d1117;border:1px solid var(--line);border-left:3px solid var(--warn);
border-radius:6px;padding:8px 12px;margin:6px 0;font-family:Consolas,monospace;font-size:12px;color:var(--dim)}
.kv{color:var(--dim)}.kv b{color:var(--txt)}
.eratbl td,.eratbl th{padding:5px 12px;font-size:13px}
</style></head><body>
<header><div><h1>Acquisition Regulation <b>Versioned Store</b> Pipeline</h1>
<div class="sub">Every FAR &amp; agency supplement &middot; every edition since the archives began &middot; parsed, proven, tracked nightly</div></div>
<div><button class=primary onclick="runAll('download,survey,backfill,audit')">&#9654; Build ALL agencies</button>
<button onclick="runAll('llm-triage,llm')">Run LLM phase (all)</button>
<button style="border-color:var(--bad);color:var(--bad)" onclick="stopRun('ALL')">&#9632; Stop all</button></div></header>
<div id="fleet" style="display:none;margin:0 34px;padding:10px 16px;background:#101f33;
border:1px solid #1e4976;border-radius:8px;color:var(--run)"></div>
<div class="hero" id="hero"></div>
<div class="flow">
 <span class="node">1 &middot; Download archives</span><span class="arr">&#10142;</span>
 <span class="node">2 &middot; Classify eras</span><span class="arr">&#10142;</span>
 <span class="node">3 &middot; Parse &amp; merge (oldest&rarr;newest)</span><span class="arr">&#10142;</span>
 <span class="node">4 &middot; Prove every character accounted for</span><span class="arr">&#10142;</span>
 <span class="node">5 &middot; LLM reference audit</span><span class="arr">&#10142;</span>
 <span class="node">Versioned store</span>
</div>
<table id="tbl"><thead><tr>
<th>Regulation</th><th>Pipeline (click a stage for detail)</th><th>Editions</th><th>History</th>
<th>Rows</th><th>Text accounted for</th><th>Invariants</th><th>Review</th><th>Store</th><th></th>
</tr></thead><tbody></tbody></table>
<div id="modal" onclick="if(event.target.id=='modal')this.style.display='none'">
<div id="mbox"></div></div>
<script>
const chip=(n,s,a)=>`<span class="chip ${s||''}" onclick="detail('${a}')">${n}</span>`;
const CLS={captured:'#2ea043',heading:'#316dca',toc:'#8957e5','toc-foreign':'#6e7681',
nav:'#57606a',furniture:'#57606a',short:'#444c56',UNCLASSIFIED:'#f85149'};
async function refresh(){
 const resp=await (await fetch('/api/agencies')).json();
 const d=resp.agencies||resp;const fleet=resp.fleet||{};
 const fb=document.getElementById('fleet');
 if(fleet.active){fb.style.display='block';
   fb.innerHTML=`<span class=spin style="margin-right:10px"></span>
    Fleet run: <b>${fleet.current}</b> (${fleet.index}/${fleet.total}) &middot; steps: ${(fleet.steps||[]).join(' &rarr; ')}
    <button style="margin-left:14px;border-color:var(--bad);color:var(--bad)" onclick="stopRun('ALL')">Stop</button>`;
 } else fb.style.display='none';
 let rows=0,eds=0,built=0,q=0,acc=[];
 for(const a of d){rows+=a.rows||0;eds+=a.editions||0;q+=a.queue||0;
   if(a.accounted!=null)acc.push(a.accounted);
   if((a.steps.backfill||'')=='ok')built++;}
 document.getElementById('hero').innerHTML=
  `<div class=stat><div class=n>${d.length}</div><div class=l>Regulations</div></div>
   <div class=stat><div class=n>${eds.toLocaleString()}</div><div class=l>Editions ingested</div></div>
   <div class=stat><div class=n>${rows.toLocaleString()}</div><div class=l>Version rows</div></div>
   <div class=stat><div class=n>${built}/${d.length}</div><div class=l>Stores built</div></div>
   <div class=stat><div class=n>${acc.length?Math.min(...acc).toFixed(2)+'%':'&mdash;'}</div>
     <div class=l>Text accounted (worst)</div></div>
   <div class=stat><div class=n>${q}</div><div class=l>Items for review</div></div>`;
 const tb=document.querySelector('#tbl tbody');tb.innerHTML='';
 for(const a of d){
  const tr=document.createElement('tr');const st=a.steps||{};
  const pipeline=['download','survey','backfill','audit','llm'].map(s=>
    chip(s,st[s]||'',a.agency)).join('')
    +(a.progress?`<span class=prog title="${a.progress}">${a.progress}</span>`:'');
  const acct=a.accounted==null?'&mdash;':
    `<span class="acct" style="color:${a.accounted>=99.5?'var(--ok)':'var(--warn)'}"
      onclick="detail('${a.agency}')"><b>${a.accounted.toFixed(2)}%</b> accounted
      &middot; ${a.certs_pass}/${a.certs_total} eras pass</span>`;
  const inv=a.invariants_ok==null?'&mdash;':(a.invariants_ok?
    '<span class="chip ok">clean</span>':'<span class="chip bad">FAIL</span>');
  tr.innerHTML=`<td><b>${a.agency}</b>${a.running?'<span class=spin></span>':''}</td>
   <td>${pipeline}</td><td>${a.editions||'&mdash;'}</td>
   <td class=mono>${a.floor?a.floor+' &rarr; '+(a.ceiling||''):'&mdash;'}</td>
   <td>${(a.rows||0).toLocaleString()}</td><td>${acct}</td><td>${inv}</td>
   <td>${a.queue||0}</td><td class=mono>${a.store_mb?a.store_mb+' MB':'&mdash;'}</td>
   <td>${a.running?`<button style="border-color:var(--bad);color:var(--bad)" onclick="stopRun('${a.agency}')">Stop</button>`
        :`<button class=primary onclick="run('${a.agency}')">Run</button>`}
       <button onclick="detail('${a.agency}')">Detail</button></td>`;
  tb.appendChild(tr);}
}
function sbar(c){
 const total=c.source_chars||1;const res=c.residue||{};
 const segs=[['captured',Math.round(c.covered_pct*total/100)]];
 for(const k of Object.keys(res))segs.push([k,res[k]]);
 let html='<div class=sbar>';
 for(const [k,v] of segs){const w=100*v/total;
   if(w>0.05)html+=`<div style="width:${w}%;background:${CLS[k]||'#444'}" title="${k}: ${v.toLocaleString()} chars"></div>`;}
 html+='</div>';
 return html;
}
async function detail(a){
 const d=await (await fetch('/api/detail?agency='+a)).json();
 const v=d.verification||{};const certs=v.certificates||[];
 let h=`<h2>${a} &mdash; verification detail</h2>
 <div class=kv>Store invariants: <b>${v.invariants_ok===true?'clean':(v.invariants_ok===false?'FAILING':'not run')}</b>
 &middot; editions <b>${v.editions||'-'}</b> &middot; rows <b>${(v.rows||0).toLocaleString()}</b>
 &middot; last audit <b>${v.at||'-'}</b></div>`;
 h+=`<h3>Era survey</h3><table class=eratbl><tr><th>era</th><th>editions</th><th>date span</th></tr>`;
 for(const [e,x] of Object.entries(d.eras||{}))
   h+=`<tr><td>${e}</td><td>${x.folders}</td><td class=mono>${x.from} &rarr; ${x.to}</td></tr>`;
 h+=`</table>`;
 h+=`<h3>Coverage proof (conservation of text, newest edition per era)</h3>
 <div class=legend>${Object.entries(CLS).map(([k,c])=>`<span><i style="background:${c}"></i>${k}</span>`).join('')}</div>
 <div class=kv style="margin-bottom:8px">Reading: every character of the source HTML must be
 <b>captured</b> in a chunk or <b>classified</b> as deliberate skip (headings are stored as titles;
 TOC entries, nav and page furniture are not regulation text; "toc-foreign" = the supplement's TOC
 listing its PARENT regulation's sections). <b style="color:var(--bad)">Red = unexplained</b> &mdash;
 the only number that matters, threshold 0.5%.</div>`;
 for(const c of certs){
   if(c.error){h+=`<div class=kv><b>${c.era}</b> ${c.folder}: <span style="color:var(--bad)">${c.error}</span></div>`;continue;}
   h+=`<div class=kv><b>${c.era}</b> &middot; ${c.folder} &middot; ${(c.source_chars||0).toLocaleString()} source chars
    &middot; <b style="color:${c.pass?'var(--ok)':'var(--warn)'}">${c.accounted_pct}% accounted</b>
    (${c.unclassified_chars.toLocaleString()} chars unexplained${c.pass?' — PASS':' — REVIEW'})</div>`+sbar(c);
   for(const s of (c.samples||[]))h+=`<div class=sample>${s.replace(/</g,'&lt;')}</div>`;
 }
 if((d.queue_sample||[]).length){
   h+=`<h3>Review queue (first ${d.queue_sample.length})</h3>`;
   for(const q of d.queue_sample)
     h+=`<div class=sample>[${q.era} &middot; ${q.file}] ${String(q.text||'').replace(/</g,'&lt;')}</div>`;}
 if((d.recent_editions||[]).length){
   h+=`<h3>Recent ingests</h3>`;
   for(const e of d.recent_editions)
     h+=`<div class=kv mono>${e.date} ${e.fac} [${e.era}] chunks=${e.chunks} collapsed=${e.collapsed} verify=${e.verify_ok===null?'skip':(e.verify_ok?'OK':'FAIL')}</div>`;}
 h+=`<div style="margin-top:18px"><button onclick="document.getElementById('modal').style.display='none'">Close</button></div>`;
 document.getElementById('mbox').innerHTML=h;
 document.getElementById('modal').style.display='flex';
}
async function run(a){
 const steps=prompt('Steps for '+a,'download,survey,backfill,audit');
 if(!steps)return;
 await fetch('/api/run',{method:'POST',body:JSON.stringify({agency:a,steps})});
 setTimeout(refresh,800);}
async function runAll(steps){
 if(!confirm('Run ['+steps+'] across ALL agencies, sequentially?'))return;
 await fetch('/api/run',{method:'POST',body:JSON.stringify({agency:'ALL',steps})});
 setTimeout(refresh,800);}
async function stopRun(a){
 if(!confirm('Stop '+(a=='ALL'?'the fleet run (and any per-agency runs)':a)+'?'))return;
 await fetch('/api/stop',{method:'POST',body:JSON.stringify({agency:a})});
 setTimeout(refresh,800);}
refresh();setInterval(refresh,4000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            b = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path.startswith("/api/agencies"):
            fleet = _read(os.path.join(ROOT, "stores", "fleet_state.json"))
            if fleet.get("active") and "ALL" in _procs and _procs["ALL"].poll() is not None:
                fleet["active"] = False        # process died without cleanup
            self._json({"fleet": fleet, "agencies": [agency_info(a) for a in agencies()]})
        elif self.path.startswith("/api/detail"):
            a = self.path.split("agency=")[-1].split("&")[0]
            self._json(agency_detail(a))
        elif self.path.startswith("/api/log"):
            a = self.path.split("agency=")[-1].split("&")[0]
            p = os.path.join(ROOT, "stores", a, "run.log")
            log = ""
            if os.path.exists(p):
                log = open(p, encoding="utf-8", errors="replace").read()[-8000:]
            self._json({"agency": a, "log": log})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/run":
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            a = (body.get("agency") or "").upper()
            steps = body.get("steps") or "download,survey,backfill,audit"
            if a != "ALL" and a not in agencies():
                return self._json({"error": f"unknown agency {a}"}, 400)
            if a != "ALL":
                os.makedirs(os.path.join(ROOT, "stores", a), exist_ok=True)
            ok = launch(a, steps)
            self._json({"launched": ok})
        elif self.path == "/api/stop":
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            self._json({"stopped": stop((body.get("agency") or "ALL").upper())})
        else:
            self._json({"error": "not found"}, 404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8642
    print(f"dashboard: http://localhost:{port}")
    HTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    main()
