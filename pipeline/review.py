#!/usr/bin/env python3
"""Stage 4: render the reconciliation ledger as a self-contained HTML review page.

No server: the full master list (every atomic cross-reference, tagged corroborated /
parser_explicit / parser_inferred / llm_only) plus a run-summary banner are embedded as
JSON. Rows are grouped by unit; each unit has an "Add reference(s)" box. Every row is
editable — Accept / Reject / Manual — and the Manual/Add boxes accept comma lists AND
ranges (expanded client-side into atomic citations, mirroring the parser). When a judge
ran, its recommended option is tagged "judge ✓". "Export decisions" downloads decisions.json
(consumed by `pipeline.py apply`); selections auto-save to the browser.
"""
import json, html

PAGE = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
 body{font:14px/1.5 system-ui,sans-serif;margin:0;background:#f4f4f0;color:#16202e}
 .topbar{position:sticky;top:0;z-index:5}            /* header + Show bar stay pinned; banner below scrolls away */
 header{background:#102032;color:#fff;padding:12px 20px;display:flex;
   gap:16px;align-items:center;justify-content:space-between}
 header b{font-size:16px} .meta{opacity:.85;font-size:12px}
 button{font:inherit;padding:8px 14px;border:0;border-radius:7px;cursor:pointer}
 .exp{background:#0e7c8b;color:#fff}
 #banner{padding:8px 20px;background:#eef2f4;border-bottom:1px solid #d9d6cc;font:12px/1.5 ui-monospace,monospace;color:#2a3a4a}
 .filt{padding:8px 20px;background:#fff;border-bottom:1px solid #ddd;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
 .filt label{display:flex;gap:5px;align-items:center}
 .unit{margin:16px 20px}
 .uh{font-weight:700;font-size:15px;padding:6px 2px;border-bottom:2px solid #102032;margin-bottom:6px}
 .ucount{font:11px ui-monospace,monospace;color:#6a7c8c;margin-left:8px;font-weight:400}
 .ccount{cursor:pointer;text-decoration:underline dotted} .ccount:hover{color:#0e7c8b}
 .clist{font:12px/1.5 ui-monospace,monospace;color:#33485c;background:#fff;border:1px solid #e6e3da;border-radius:6px;padding:6px 10px;margin:4px 0 8px;font-weight:400}
 a.far{color:#0e7c8b}
 .item{background:#fff;margin:8px 0;border:1px solid #d9d6cc;border-left:4px solid transparent;border-radius:10px;padding:10px 14px}
 .item.done{border-left-color:#5aa86e}
 .hd{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;gap:10px}
 .tgt{font:600 15px ui-monospace,monospace;color:#102032}
 .badge{font:11px ui-monospace,monospace;padding:2px 8px;border-radius:5px;white-space:nowrap}
 .b-corroborated{background:#d7ecdd;color:#1c6a2e} .b-parser_explicit{background:#e3e6ee;color:#33425e}
 .b-parser_inferred{background:#f5ddcb;color:#8f4316} .b-llm_only{background:#d6ebed;color:#0b6a78}
 .b-added{background:#efe2f4;color:#6a2c83}
 .b-ext{background:#e7eef3;color:#3a5a72} .ext-loc{font:11px ui-monospace,monospace;color:#6a7c8c;margin-left:4px}
 .val{font:10px ui-monospace,monospace;opacity:.6;margin-left:6px}
 .cols{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin:8px 0}
 .col{border:1px solid #e6e3da;border-radius:8px;padding:8px 10px}
 .col h4{margin:0 0 5px;font:11px ui-monospace,monospace;text-transform:uppercase;color:#5a6c7c}
 .ev{font-size:12.5px;color:#222} .ev mark{background:#fde047;padding:0 1px} .none{color:#9aa;font-style:italic}
 .choose{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:4px}
 .choose label{display:flex;gap:5px;align-items:center}
 input[type=text]{padding:5px 8px;font:inherit} .man{width:240px}
 .jtag{background:#d7ecdd;color:#1c6a2e;font:10px ui-monospace,monospace;padding:1px 6px;border-radius:4px;margin-left:4px;cursor:pointer}
 .addbox{margin:8px 0 2px;padding:8px 10px;border:1px dashed #c7c3b8;border-radius:8px;background:#fbfbf8}
 .addin{width:460px} .addbtn{background:#102032;color:#fff;margin-left:8px;padding:5px 12px}
</style></head><body>
<div class="topbar">
<header><b>__TITLE__</b>
 <span class="meta" id="meta"></span>
 <span>
   <input type=file id=imp accept="application/json,.json" style="display:none" onchange="importDecisions(this)">
   <button onclick="document.getElementById('imp').click()">Import ▲</button>
   <button class="exp" onclick="exportDecisions()">Export decisions ▼</button>
   <button class="exp" onclick="saveApply()" title="Save decisions to out/ and run apply (served review only)">Save &amp; Apply ▶</button>
 </span></header>
<div class="filt">
 <b style="font-size:12px">Show:</b>
 <label title="The LLM found this; the parser did not — usually an untagged prose reference."><input type=checkbox class=f value=llm_only checked onchange=fltAnchored()> LLM only (parser missed)</label>
 <label title="The parser inferred this from prose / a range; the LLM did not find it."><input type=checkbox class=f value=parser_inferred checked onchange=fltAnchored()> Parser guess (LLM missed)</label>
 <label title="The parser found a real <xref> link the LLM did not echo (kept automatically)."><input type=checkbox class=f value=parser_explicit onchange=fltAnchored()> Tagged link (LLM missed)</label>
 <label title="Both the parser and the LLM found this (auto-accepted)."><input type=checkbox class=f value=corroborated onchange=fltAnchored()> Both agree</label>
 <label title="A reference you added by hand that neither tool found."><input type=checkbox class=f value=added checked onchange=fltAnchored()> Manually added</label>
 &nbsp;|&nbsp; <b style="font-size:12px">Scope:</b>
 <label title="References within this regulation (FAR → FAR)."><input type=checkbox class=sf value=internal checked onchange=fltAnchored()> Internal</label>
 <label title="References to other government documents (U.S.C., CFR, E.O., Pub. L., OMB…)."><input type=checkbox class=sf value=external onchange=fltAnchored()> External</label>
 &nbsp;|&nbsp; <label title="Hide rows you have already chosen Accept / Reject / Manual on."><input type=checkbox id=hideDone onchange=fltAnchored()> hide reviewed</label>
</div>
</div>
<div id="banner"></div>
<div id="list"></div>
<script>
const Q = __DATA__;
const S = __SUMMARY__;
const KEY = 'review:' + document.title;
const NEEDS = new Set(['parser_inferred','llm_only','added']);
const q = s => document.querySelector(s);
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function fmt(n){return (n||0).toLocaleString();}
function xrefHtml(s){return esc(s).replace(/&lt;xref href=&quot;([^&]*?)&quot;&gt;(.*?)&lt;\/xref&gt;/g,
   '<mark>&lt;xref href=&quot;$1&quot;&gt;$2&lt;/xref&gt;</mark>');}
function llmHtml(s){return esc(s).replace(/«(.*?)»/g,'<mark>$1</mark>');}  // strip « » delimiters, keep the highlight
function unitBase(u){return (u||'').replace(/^[A-Za-z]+-/,'');}
function unitUrl(u){const it=Q.find(x=>x.unit===u);return it?it.url:'';}

// ---- range / comma expansion (mirrors the parser; used by Manual + Add boxes) ----
const RV={i:1,v:5,x:10,l:50,c:100,d:500,m:1000};
function rti(s){s=s.toLowerCase();let t=0,p=0;for(let k=s.length-1;k>=0;k--){const c=s[k];if(!(c in RV))return null;const v=RV[c];t+=v<p?-v:v;p=Math.max(p,v);}return t;}
function itr(n){const seq=[[1000,'m'],[900,'cm'],[500,'d'],[400,'cd'],[100,'c'],[90,'xc'],[50,'l'],[40,'xl'],[10,'x'],[9,'ix'],[5,'v'],[4,'iv'],[1,'i']];let o='';for(const [v,sy] of seq){while(n>=v){o+=sy;n-=v;}}return o;}
function enumTok(a,b){
  if(!a||!b)return null;
  if(/^\d+$/.test(a)&&/^\d+$/.test(b)){const x=+a,y=+b;return (y>=x&&y-x<=40)?Array.from({length:y-x+1},(_,k)=>''+(x+k)):null;}
  if((a.toUpperCase()===a)!==(b.toUpperCase()===b))return null;
  const up=a.toUpperCase()===a, lo=a.toLowerCase(), hi=b.toLowerCase(), rom=t=>/^[ivxlcdm]+$/.test(t);
  if(rom(lo)&&rom(hi)&&(lo.length>1||hi.length>1)){const x=rti(lo),y=rti(hi);if(x==null||y==null||!(y>x&&y-x<=40))return null;let r=[];for(let n=x;n<=y;n++)r.push(itr(n));return up?r.map(s=>s.toUpperCase()):r;}
  if(lo.length===1&&hi.length===1&&/[a-z]/.test(lo)&&/[a-z]/.test(hi)){if(rom(lo)&&rom(hi))return null;const x=lo.charCodeAt(0),y=hi.charCodeAt(0);if(!(y>=x&&y-x<=25))return null;let r=[];for(let n=x;n<=y;n++)r.push(String.fromCharCode(n));return up?r.map(s=>s.toUpperCase()):r;}
  return null;
}
function expandCitations(raw, base){
  const out=[];
  (raw||'').split(',').forEach(piece=>{
    let s=piece.trim(); if(!s) return;
    let m=s.match(/^(\d+\.\d+(?:-\d+)?)?\s*((?:\([A-Za-z0-9]+\))+)\s*(?:through|thru|to|[-–—])\s*(\d+\.\d+(?:-\d+)?)?\s*((?:\([A-Za-z0-9]+\))+)$/i);
    if(m){
      const lt=(m[2].match(/\(([A-Za-z0-9]+)\)/g)||[]).map(x=>x.slice(1,-1));
      const rt=(m[4].match(/\(([A-Za-z0-9]+)\)/g)||[]).map(x=>x.slice(1,-1));
      const fixed=lt.slice(0,-1);
      if(rt.slice(0,-1).length && rt.slice(0,-1).join()!==fixed.join()){out.push(s);return;}
      const mem=enumTok(lt[lt.length-1], rt[rt.length-1]);
      const b=m[1]||m[3]||base||'';
      if(mem){mem.forEach(tk=>out.push(b+fixed.map(f=>'('+f+')').join('')+'('+tk+')'));return;}
    } else {
      m=s.match(/^(\d+\.\d+)-(\d+)\s*(?:through|thru|to|–|—|-)\s*(?:(?:\d+\.\d+)-)?(\d+)$/i);
      if(m){const x=+m[2],y=+m[3];if(y>=x&&y-x<=40){for(let n=x;n<=y;n++)out.push(m[1]+'-'+n);return;}}
    }
    out.push(s);
  });
  return out;
}

// ---- plain-English labels + tooltips for the status buckets ----
const LABELS={corroborated:'Both agree', llm_only:'LLM only (parser missed)',
  parser_inferred:'Parser guess (LLM missed)', parser_explicit:'Tagged link (LLM missed)', added:'Manually added'};
const STATUS_ORDER=['llm_only','parser_inferred','parser_explicit','corroborated','added'];
const statusRank=s=>{const i=STATUS_ORDER.indexOf(s); return i<0?99:i;};
const TIPS={corroborated:'Both the parser and the LLM found this reference (auto-accepted).',
  llm_only:'The LLM found this; the deterministic parser did not — usually an untagged prose reference.',
  parser_inferred:'The parser inferred this from prose / an expanded range (not a tagged link) and the LLM did not corroborate it.',
  parser_explicit:'The parser found a real <xref> hyperlink but the LLM did not echo it (kept automatically).',
  added:'A reference you added by hand that neither tool found.'};
const lbl=s=>LABELS[s]||s;

// ---- natural FAR citation order (mirrors reconcile.cit_sort_key), for grouping + added rows ----
const LADDER=['alpha','digit','roman','alpha','digit','roman'];
function citCells(t){
  const m=(t||'').match(/^(\d+)\.(\d+)(?:-(\d+))?(.*)$/);
  if(!m){const nums=(t||'').match(/\d+/g)||[]; return [[1,0]].concat(nums.map(n=>[0,+n])).concat([[2,(t||'').toLowerCase()]]);}
  const cells=[[0,+m[1]],[0,+m[2]],[0,+(m[3]||0)]];
  (m[4].match(/\(([A-Za-z0-9]+)\)/g)||[]).map(x=>x.slice(1,-1)).forEach((tk,i)=>{
    const typ=LADDER[i]||'alpha';
    if(typ==='digit') cells.push(/^\d+$/.test(tk)?[0,+tk]:[2,tk.toLowerCase()]);
    else if(typ==='roman'){const v=rti(tk); cells.push(v!=null?[0,v]:[2,tk.toLowerCase()]);}
    else cells.push([1,tk.toLowerCase()]);
  });
  return cells;
}
function citCmp(a,b){
  const A=citCells(a),B=citCells(b),n=Math.min(A.length,B.length);
  for(let i=0;i<n;i++){ if(A[i][0]!==B[i][0])return A[i][0]-B[i][0];
    if(A[i][1]<B[i][1])return -1; if(A[i][1]>B[i][1])return 1; }
  return A.length-B.length;
}

function defChoice(it){
  if(it.status==='corroborated'||it.status==='parser_explicit'||it.status==='added') return 'accept';
  return (it.judge&&it.judge.choice)||null;
}
function jtag(it,val){return (it.judge&&it.judge.choice===val)?`<span class=jtag onclick="pickJudge(${Q.indexOf(it)})">judge ✓</span>`:'';}
function pickJudge(i){const j=Q[i].judge; if(!j)return; const r=q(`input[name=c${i}][value=${j.choice}]`); if(r)r.checked=true;
  if(j.choice==='manual'){const mb=document.getElementById('man'+i); if(mb)mb.value=(j.value||[]).join(', ');} flt(); save();}
function rowHtml(it,i){
  const p=it.parser,l=it.llm,j=it.judge;
  const disp=it.scope==='external'?(it.citation||it.target):it.target;
  const rt=it.scope==='external'?`<span class="badge b-ext" title="external document node: ${esc(it.target)}">${esc(it.ref_type||'ext')}</span> `:'';
  return `<div class=hd><span class=tgt>${esc(disp)}</span>
     <span>${rt}<span class="badge b-${it.status}" title="${esc(TIPS[it.status]||'')}">${esc(lbl(it.status))}</span><span class=val>${esc(it.validation||'')}</span></span></div>
   <div class=cols>
     <div class=col><h4>Parser</h4>${p?`<div class=ev><b>${esc(p.kind)}</b><br>${xrefHtml(p.evidence)}</div>`:'<div class=none>(parser did not find this)</div>'}</div>
     <div class=col><h4>LLM</h4>${l?`<div class=ev>${llmHtml(l.evidence)}</div>`:'<div class=none>(LLM did not find this)</div>'}</div>
     <div class=col><h4>Judge</h4>${j?`<div class=ev><b>${esc(j.choice)}${(j.value&&j.value.length)?': '+esc(j.value.join(', ')):''}</b><br>${esc(j.rationale||'')}</div>`:'<div class=none>(no judge / agreement)</div>'}</div>
   </div>
   <div class=choose>
     <label><input type=radio name=c${i} value=accept> Accept (${esc(disp)})</label>${jtag(it,'accept')}
     <label><input type=radio name=c${i} value=reject> Reject</label>${jtag(it,'reject')}
     ${it.scope==='external'
       ? `<label><input type=radio name=c${i} value=manual onchange="document.getElementById('doc${i}').focus()"> Manual:</label>${jtag(it,'manual')}
          <input type=text id=doc${i} class=man placeholder="document (e.g. Small Business Act)" value="${esc(it.node_label||'')}">
          <input type=text id=sec${i} placeholder="section (e.g. 8(a))" style="width:140px" value="${esc(it.locator||'')}">`
       : `<label><input type=radio name=c${i} value=manual onchange="document.getElementById('man${i}').focus()"> Manual:</label>${jtag(it,'manual')}
          <input type=text id=man${i} class=man placeholder="comma list or range">`}
   </div>`;
}
function render(){
 const list=document.getElementById('list'); list.innerHTML='';
 const g=new Map();
 Q.forEach((it,i)=>{ if(!g.has(it.unit)) g.set(it.unit,[]); g.get(it.unit).push(i); });
 g.forEach((idx,unit)=>{
   idx.sort((i,j)=>{                                    // FAR (internal) first, external second; natural order within each
     const sa=Q[i].scope==='external'?1:0, sb=Q[j].scope==='external'?1:0;
     return sa-sb || citCmp(Q[i].target, Q[j].target);
   });
   const sec=document.createElement('div'); sec.className='unit';
   const byStatus={}; idx.forEach(i=>{const it=Q[i]; (byStatus[it.status]=byStatus[it.status]||[]).push(
     it.scope==='external'?(it.citation||it.target):it.target);});
   const url=unitUrl(unit);
   const chips=Object.keys(byStatus).sort((a,b)=>statusRank(a)-statusRank(b)).map(k=>{
     const list=byStatus[k].join(', ');
     return `<span class=ccount data-list="${esc(list)}" title="${esc(list)}">${lbl(k)} ${byStatus[k].length}</span>`;
   }).join(' · ');
   sec.innerHTML=`<div class=uh><span>${esc(unit)}</span> ·
     <a class=far href="${esc(url)}" target=_blank rel=noopener>acquisition.gov ↗</a>
     <span class=ucount>${chips}</span></div><div class=clist style="display:none"></div>`;
   idx.forEach(i=>{const d=document.createElement('div'); d.className='item'; d.dataset.bucket=Q[i].status; d.dataset.scope=Q[i].scope||'internal'; d.dataset.i=i; d.innerHTML=rowHtml(Q[i],i); sec.appendChild(d);});
   const add=document.createElement('div'); add.className='addbox';
   add.innerHTML=`<input type=text class=addin placeholder="add reference(s) to ${esc(unit)} — comma list or range, e.g. 5.202(a)(2), 5.203(a)-(c)"><button class=addbtn>+ Add</button>`;
   add.querySelector('.addbtn').onclick=()=>addRefs(unit, add.querySelector('.addin'));
   sec.appendChild(add); list.appendChild(sec);
 });
 Q.forEach((it,i)=>{ const c=defChoice(it); if(c){const r=q(`input[name=c${i}][value=${c}]`); if(r)r.checked=true;}
   if(it.judge&&it.judge.choice==='manual'){const mb=document.getElementById('man'+i); if(mb)mb.value=(it.judge.value||[]).join(', ');}});
 flt();
}
function addRefs(unit, inputEl){
 const tgts=expandCitations(inputEl.value, unitBase(unit)); if(!tgts.length) return;
 const saved=collect();
 tgts.forEach(t=>{ if(!Q.some(it=>it.unit===unit&&it.target===t))
   Q.push({unit, url:unitUrl(unit), target:t, status:'added', validation:'', parser:null, llm:null, judge:null, needs_review:true, added:true}); });
 render(); applyDecisions(saved); save(); flt();
}
function fltAnchored(){                           // keep the section you're viewing in place while filtering
 let anchor=null, top=0;
 for(const u of document.querySelectorAll('.unit')){ const r=u.getBoundingClientRect(); if(r.bottom>0){ anchor=u; top=r.top; break; } }
 flt();
 if(anchor) window.scrollBy(0, anchor.getBoundingClientRect().top - top);
}
function flt(){
 const on=[...document.querySelectorAll('.f:checked')].map(x=>x.value);
 const scopes=[...document.querySelectorAll('.sf:checked')].map(x=>x.value);
 const hide=document.getElementById('hideDone').checked;
 let shown=0, rTot=0, rDone=0;
 document.querySelectorAll('.item').forEach(d=>{
   const i=d.dataset.i, st=d.dataset.bucket, sc=d.dataset.scope, picked=q(`input[name=c${i}]:checked`);
   if(NEEDS.has(st)){rTot++; if(picked)rDone++;}
   const vis=on.includes(st) && scopes.includes(sc) && !(hide&&picked);
   d.style.display=vis?'':'none'; d.classList.toggle('done',!!picked); if(vis)shown++;
 });
 document.getElementById('meta').textContent=`${Q.length} refs · flagged reviewed ${rDone}/${rTot} · ${shown} shown`;
}
function collect(){
 const out=[];
 Q.forEach((it,i)=>{
   const picked=q(`input[name=c${i}]:checked`); if(!picked) return;
   const base={unit:it.unit, target:it.target, status:it.status, choice:picked.value,
               scope:it.scope||'internal', locator:it.locator||''};
   if(picked.value==='manual' && it.scope==='external'){       // structured external edit: document + section
     const dv=document.getElementById('doc'+i), sv=document.getElementById('sec'+i);
     out.push({...base, value:[], edit:{document:dv?dv.value:'', section:sv?sv.value:'', ref_type:it.ref_type||'other'}});
     return;
   }
   let value=[];
   if(picked.value==='accept') value=[it.target];
   else if(picked.value==='manual'){const mb=document.getElementById('man'+i); value=expandCitations(mb?mb.value:'', unitBase(it.unit));}
   out.push({...base, value});
 });
 return out;
}
function save(){ try{localStorage.setItem(KEY, JSON.stringify(collect()));}catch(e){} }
function applyDecisions(list){
 list=list||[];
 let grew=false;
 list.forEach(d=>{ if(d.status==='added' && !Q.some(it=>it.unit===d.unit&&it.target===d.target)){
   Q.push({unit:d.unit, url:unitUrl(d.unit), target:d.target, status:'added', validation:'', parser:null, llm:null, judge:null, needs_review:true, added:true}); grew=true; }});
 if(grew) render();
 const idx={}; Q.forEach((it,i)=>idx[it.unit+'|'+it.target]=i);
 list.forEach(d=>{
   const i=idx[d.unit+'|'+d.target]; if(i===undefined) return;
   const r=q(`input[name=c${i}][value=${d.choice}]`); if(r) r.checked=true;
   if(d.choice==='manual'){
     if(d.scope==='external' && d.edit){
       const dv=document.getElementById('doc'+i), sv=document.getElementById('sec'+i);
       if(dv)dv.value=d.edit.document||''; if(sv)sv.value=d.edit.section||'';
     } else {
       const mb=document.getElementById('man'+i); if(mb)mb.value=(d.value||[]).join(', ');
     }
   }
 });
}
function importDecisions(input){
 const f=input.files[0]; if(!f) return;
 const r=new FileReader();
 r.onload=()=>{ try{applyDecisions(JSON.parse(r.result)); save(); flt();}catch(e){alert('Invalid decisions.json: '+e);} };
 r.readAsText(f); input.value='';
}
function exportDecisions(){
 const blob=new Blob([JSON.stringify(collect(),null,2)],{type:'application/json'});
 const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='decisions.json'; a.click();
}
function saveApply(){                            // served review only: POST decisions -> server writes out/ + runs apply
 fetch('apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(collect())})
  .then(r=>r.json()).then(d=>{ alert(d.ok ? ('Saved & applied ✓\n'+d.decisions+' decisions → '+d.verified) : ('Error: '+(d.error||'unknown'))); })
  .catch(e=>alert('“Save & Apply” needs the served review — run:  python pipeline.py review\n('+e+')'));
}
function banner(){
 const el=document.getElementById('banner');
 if(!S||!S.model){el.style.display='none';return;}
 const sc=S.status_counts||{}, tk=(S.tokens&&S.tokens.total)||{}, tm=S.timing_sec||{};
 const parts=[`${S.provider} / ${S.model}`,
   `${S.units} units (cache hits ${S.cache_hits})`,
   `corrob ${sc.corroborated||0} · parser-exp ${sc.parser_explicit||0} · parser-inf ${sc.parser_inferred||0} · llm-only ${sc.llm_only||0}`];
 if(tk.calls) parts.push(`tokens ${fmt(tk.total)} total · in ${fmt(tk.prompt)} · think ${fmt(tk.thinking)} · out ${fmt(tk.output)} · ${tk.calls} calls`);
 const co=S.cost;
 if(co && (co.rates_per_1m.input||co.rates_per_1m.output) && tk.calls) parts.push(`est ${co.currency} $${co.total.toFixed(2)}`);
 if(tm.total!=null) parts.push(`${tm.total}s`);
 el.textContent='▸ '+parts.join('   ·   ');
}
document.addEventListener('change',e=>{
 if(e.target.name&&e.target.name[0]==='c'){flt();save();}
 if(e.target.classList&&e.target.classList.contains('man')) save();
});
document.addEventListener('click',e=>{           // click a header count chip -> toggle its reference list
 if(!(e.target.classList&&e.target.classList.contains('ccount'))) return;
 const box=e.target.closest('.unit').querySelector('.clist'), txt=e.target.dataset.list;
 if(box.style.display!=='none' && box.dataset.src===txt){ box.style.display='none'; }
 else { box.textContent=txt; box.dataset.src=txt; box.style.display=''; }
});
banner(); render();
const _saved=localStorage.getItem(KEY); if(_saved){try{applyDecisions(JSON.parse(_saved));}catch(e){}}
flt();
</script></body></html>"""

def write_review(ledger, out_path, title, summary=None):
    doc = (PAGE.replace("__TITLE__", html.escape(title))
               .replace("__DATA__", json.dumps(ledger, ensure_ascii=False))
               .replace("__SUMMARY__", json.dumps(summary or {}, ensure_ascii=False)))
    open(out_path, "w", encoding="utf-8").write(doc)
    return out_path
