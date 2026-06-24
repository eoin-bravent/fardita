#!/usr/bin/env python3
"""Stage 4: render the reconciliation ledger as a self-contained HTML review page.

No server: the full master list (every atomic cross-reference, tagged corroborated /
parser_explicit / parser_inferred / llm_only) is embedded as JSON. Every row is editable —
Accept / Reject / Manual — so the reviewer can inspect agreements and override anything.
By default only the disagreements (parser_inferred + llm_only) are shown; tick the other
status filters to see corroborated / explicit refs too. "Export decisions" downloads
decisions.json (consumed by `pipeline.py apply`); selections auto-save to the browser.
"""
import json, html

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
 body{{font:14px/1.5 system-ui,sans-serif;margin:0;background:#f4f4f0;color:#1a2230}}
 header{{position:sticky;top:0;background:#102032;color:#fff;padding:12px 20px;display:flex;
   gap:16px;align-items:center;justify-content:space-between;z-index:5}}
 header b{{font-size:16px}} .meta{{opacity:.85;font-size:12px}}
 button{{font:inherit;padding:8px 14px;border:0;border-radius:7px;cursor:pointer}}
 .exp{{background:#0e7c8b;color:#fff}}
 .filt{{padding:8px 20px;background:#fff;border-bottom:1px solid #ddd;display:flex;gap:14px;flex-wrap:wrap;align-items:center}}
 .filt label{{display:flex;gap:5px;align-items:center}}
 .item{{background:#fff;margin:12px 20px;border:1px solid #d9d6cc;border-radius:10px;padding:12px 16px}}
 .hd{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;gap:10px}}
 .cit{{font-weight:700;font-size:15px}} .tgt{{font:600 15px monospace;color:#102032}}
 .badge{{font:11px monospace;padding:2px 8px;border-radius:5px;white-space:nowrap}}
 .b-corroborated{{background:#dcefe0;color:#1c6a2e}} .b-parser_explicit{{background:#e7e9ef;color:#3a4a63}}
 .b-parser_inferred{{background:#f6e3d7;color:#9a4a1c}} .b-llm_only{{background:#e2f0f1;color:#0e7c8b}}
 .val{{font:10px monospace;opacity:.6;margin-left:6px}}
 .cols{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin:8px 0}}
 .col{{border:1px solid #e6e3da;border-radius:8px;padding:8px 10px}}
 .col h4{{margin:0 0 5px;font:11px monospace;text-transform:uppercase;color:#6a7c8c}}
 .ev{{font-size:12.5px;color:#33485c}} .ev mark{{background:#fde68a;padding:0 1px}} .none{{color:#9aa;font-style:italic}}
 .choose{{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:4px}}
 .choose label{{display:flex;gap:5px;align-items:center}} input[type=text]{{padding:5px 8px;width:240px;font:inherit}}
 a.far{{color:#0e7c8b}} .done{{opacity:.5}}
</style></head><body>
<header><b>{title}</b>
 <span class="meta" id="meta"></span>
 <span>
   <input type=file id=imp accept="application/json,.json" style="display:none" onchange="importDecisions(this)">
   <button onclick="document.getElementById('imp').click()">Import ▲</button>
   <button class="exp" onclick="exportDecisions()">Export decisions ▼</button>
 </span></header>
<div class="filt">
 <b style="font-size:12px">Show:</b>
 <label><input type=checkbox class=f value=llm_only checked onchange=flt()> LLM-only</label>
 <label><input type=checkbox class=f value=parser_inferred checked onchange=flt()> Parser-only (inferred)</label>
 <label><input type=checkbox class=f value=corroborated onchange=flt()> Corroborated</label>
 <label><input type=checkbox class=f value=parser_explicit onchange=flt()> Parser-only (explicit)</label>
 &nbsp;|&nbsp; <label><input type=checkbox id=hideDone onchange=flt()> hide decided</label>
</div>
<div id="list"></div>
<script>
const Q = {data};
const KEY = 'review:' + document.title;        // localStorage key (auto-save survives reloads)
const NEEDS = new Set(['parser_inferred','llm_only']);
function esc(s){{return (s||'').replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));}}
function xrefHtml(s){{ // parser evidence: show raw <xref ...> tags but mark the cited text
  return esc(s).replace(/&lt;xref href=&quot;([^&]*?)&quot;&gt;(.*?)&lt;\\/xref&gt;/g,
    '<mark>&lt;xref href=&quot;$1&quot;&gt;$2&lt;/xref&gt;</mark>');
}}
function llmHtml(s){{ return esc(s).replace(/«(.*?)»/g,'<mark>«$1»</mark>'); }}  // llm evidence: mark « » span
function defChoice(it){{                          // default selection per status (judge pre-fills disagreements)
  if(it.judge&&it.judge.choice) return it.judge.choice;
  if(it.status=='corroborated'||it.status=='parser_explicit') return 'accept';
  return null;
}}
function render(){{
 const list=document.getElementById('list'); list.innerHTML='';
 Q.forEach((it,i)=>{{
  const d=document.createElement('div'); d.className='item'; d.dataset.bucket=it.status; d.dataset.i=i;
  const p=it.parser, l=it.llm, j=it.judge;
  d.innerHTML=`<div class=hd>
     <span><span class=cit>${{esc(it.unit)}}</span> &middot;
       <a class=far href="${{esc(it.url)}}" target=_blank rel=noopener>acquisition.gov ↗</a></span>
     <span><span class=tgt>${{esc(it.target)}}</span>
       <span class="badge b-${{it.status}}">${{it.status}}</span><span class=val>${{esc(it.validation)}}</span></span></div>
   <div class=cols>
     <div class=col><h4>Parser</h4>${{p?`<div class=ev><b>${{esc(p.kind)}}</b><br>${{xrefHtml(p.evidence)}}</div>`
        :'<div class=none>(parser did not find this)</div>'}}</div>
     <div class=col><h4>LLM</h4>${{l?`<div class=ev>${{llmHtml(l.evidence)}}</div>`
        :'<div class=none>(LLM did not find this)</div>'}}</div>
     <div class=col><h4>Judge</h4>${{j?`<div class=ev><b>${{esc(j.choice)}}${{(j.value&&j.value.length)?': '+esc(j.value.join(', ')):''}}</b><br>${{esc(j.rationale||'')}}</div>`
        :'<div class=none>(no judge / agreement)</div>'}}</div>
   </div>
   <div class=choose>
     <label><input type=radio name=c${{i}} value=accept> Accept (${{esc(it.target)}})</label>
     <label><input type=radio name=c${{i}} value=reject> Reject</label>
     <label><input type=radio name=c${{i}} value=manual onchange=this.closest('.choose').querySelector('.man').focus()> Manual:</label>
     <input type=text class=man placeholder="comma-separated, e.g. 5.202(a)(2), 6.302-2">
   </div>`;
  list.appendChild(d);
 }});
 Q.forEach((it,i)=>{{                            // apply default selections
   const c=defChoice(it);
   if(c){{const r=document.querySelector(`input[name=c${{i}}][value=${{c}}]`); if(r) r.checked=true;}}
   if(it.judge&&it.judge.choice=='manual') document.querySelectorAll('.man')[i].value=(it.judge.value||[]).join(', ');
 }});
 flt();
}}
function flt(){{
 const on=[...document.querySelectorAll('.f:checked')].map(x=>x.value);
 const hide=document.getElementById('hideDone').checked;
 let shown=0, reviewTotal=0, reviewDone=0;
 const byStatus={{}};
 document.querySelectorAll('.item').forEach(d=>{{
   const i=d.dataset.i, st=d.dataset.bucket;
   byStatus[st]=(byStatus[st]||0)+1;
   const picked=document.querySelector(`input[name=c${{i}}]:checked`);
   if(NEEDS.has(st)){{reviewTotal++; if(picked) reviewDone++;}}
   const vis=on.includes(st) && !(hide&&picked);
   d.style.display=vis?'':'none'; d.classList.toggle('done',!!picked); if(vis)shown++;
 }});
 const parts=Object.keys(byStatus).sort().map(k=>`${{byStatus[k]}} ${{k}}`);
 document.getElementById('meta').textContent=
   `${{Q.length}} refs · ${{parts.join(' · ')}} · flagged decided ${{reviewDone}}/${{reviewTotal}} · ${{shown}} shown`;
}}
function collect(){{                              // every row's effective decision -> decisions.json
 const out=[];
 Q.forEach((it,i)=>{{
   const picked=document.querySelector(`input[name=c${{i}}]:checked`); if(!picked) return;
   let value=[];
   if(picked.value=='accept') value=[it.target];
   else if(picked.value=='manual') value=document.querySelectorAll('.man')[i].value.split(',').map(s=>s.trim()).filter(Boolean);
   out.push({{unit:it.unit, target:it.target, status:it.status, choice:picked.value, value}});
 }});
 return out;
}}
function save(){{ try{{localStorage.setItem(KEY, JSON.stringify(collect()));}}catch(e){{}} }}
function applyDecisions(list){{                   // restore selections (import or auto-save), keyed by unit|target
 const idx={{}}; Q.forEach((it,i)=>idx[it.unit+'|'+it.target]=i);
 (list||[]).forEach(d=>{{
   const i=idx[d.unit+'|'+d.target]; if(i===undefined) return;
   const radio=document.querySelector(`input[name=c${{i}}][value=${{d.choice}}]`);
   if(radio) radio.checked=true;
   if(d.choice=='manual') document.querySelectorAll('.man')[i].value=(d.value||[]).join(', ');
 }});
}}
function importDecisions(input){{
 const f=input.files[0]; if(!f) return;
 const r=new FileReader();
 r.onload=()=>{{ try{{applyDecisions(JSON.parse(r.result)); save(); flt();}}catch(e){{alert('Invalid decisions.json: '+e);}} }};
 r.readAsText(f); input.value='';
}}
function exportDecisions(){{
 const blob=new Blob([JSON.stringify(collect(),null,2)],{{type:'application/json'}});
 const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='decisions.json'; a.click();
}}
document.addEventListener('change',e=>{{
 if(e.target.name&&e.target.name[0]=='c'){{flt();save();}}
 if(e.target.classList&&e.target.classList.contains('man')) save();
}});
render();
const _saved=localStorage.getItem(KEY); if(_saved){{try{{applyDecisions(JSON.parse(_saved));}}catch(e){{}}}}
flt();
</script></body></html>"""

def write_review(ledger, out_path, title):
    html_doc = PAGE.format(title=html.escape(title), data=json.dumps(ledger, ensure_ascii=False))
    open(out_path, "w", encoding="utf-8").write(html_doc)
    return out_path
