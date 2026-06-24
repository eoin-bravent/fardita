#!/usr/bin/env python3
"""Stage 4: render the review queue as a self-contained HTML page.

No server: the queue + evidence are embedded as JSON; the reviewer picks
parser / LLM / manual / reject per item, then clicks "Export decisions" to
download decisions.json (consumed by `pipeline.py apply`). Nothing is written
to disk except the file the reviewer downloads.
"""
import json, html

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
 body{{font:14px/1.5 system-ui,sans-serif;margin:0;background:#f4f4f0;color:#1a2230}}
 header{{position:sticky;top:0;background:#102032;color:#fff;padding:12px 20px;display:flex;
   gap:16px;align-items:center;justify-content:space-between}}
 header b{{font-size:16px}} .meta{{opacity:.8;font-size:12px}}
 button{{font:inherit;padding:8px 14px;border:0;border-radius:7px;cursor:pointer}}
 .exp{{background:#0e7c8b;color:#fff}} .filt{{padding:8px 20px;background:#fff;border-bottom:1px solid #ddd}}
 .item{{background:#fff;margin:14px 20px;border:1px solid #d9d6cc;border-radius:10px;padding:14px 16px}}
 .hd{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}}
 .cit{{font-weight:700;font-size:15px}} .badge{{font:11px monospace;padding:2px 8px;border-radius:5px}}
 .b-conflict{{background:#f6e3d7;color:#9a4a1c}} .b-llm_new{{background:#e2f0f1;color:#0e7c8b}}
 .cols{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin:8px 0}}
 .col{{border:1px solid #e6e3da;border-radius:8px;padding:10px}}
 .col h4{{margin:0 0 6px;font:12px monospace;text-transform:uppercase;color:#6a7c8c}}
 .tgt{{font-weight:600}} .ev{{font-size:12.5px;color:#33485c;margin-top:5px}}
 .ev mark{{background:#fde68a}} .none{{color:#9aa;font-style:italic}}
 .choose{{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:6px}}
 .choose label{{display:flex;gap:5px;align-items:center}} input[type=text]{{padding:5px 8px;width:260px}}
 a.far{{color:#0e7c8b}} .done{{opacity:.45}}
</style></head><body>
<header><b>{title}</b>
 <span class="meta" id="meta"></span>
 <span>
   <input type=file id=imp accept="application/json,.json" style="display:none" onchange="importDecisions(this)">
   <button onclick="document.getElementById('imp').click()">Import ▲</button>
   <button class="exp" onclick="exportDecisions()">Export decisions ▼</button>
 </span></header>
<div class="filt">
 Show: <label><input type=checkbox class=f value=conflict checked onchange=flt()> conflict</label>
 <label><input type=checkbox class=f value=llm_new checked onchange=flt()> llm-new</label>
 &nbsp;|&nbsp; <label><input type=checkbox id=hideDone onchange=flt()> hide decided</label>
</div>
<div id="list"></div>
<script>
const Q = {data};
const KEY = 'review:' + document.title;        // localStorage key (auto-save survives reloads)
function esc(s){{return (s||'').replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]));}}
function evHtml(s){{ // show raw <xref ...> tags but mark the cited text
  s = esc(s);
  return s.replace(/&lt;xref href=&quot;([^&]*?)&quot;&gt;(.*?)&lt;\\/xref&gt;/g,
    '<mark>&lt;xref href=&quot;$1&quot;&gt;$2&lt;/xref&gt;</mark>');
}}
function render(){{
 const list=document.getElementById('list'); list.innerHTML='';
 Q.forEach((it,i)=>{{
  const d=document.createElement('div'); d.className='item'; d.dataset.bucket=it.bucket; d.dataset.i=i;
  const p=it.parser, far=it.url;
  d.innerHTML=`<div class=hd><span class=cit>${{esc(it.unit)}} &middot;
     <a class=far href="${{esc(far)}}" target=_blank rel=noopener>acquisition.gov ↗</a></span>
     <span class="badge b-${{it.bucket}}">${{it.bucket}} / ${{it.validation}}</span></div>
   <div class=cols>
     <div class=col><h4>Parser suggestion</h4>${{p?`<div class=tgt>${{esc(p.target)}}</div>
        <div class=ev>${{evHtml(p.evidence)}}</div>`:'<div class=none>(none — parser did not find this)</div>'}}</div>
     <div class=col><h4>LLM suggestion</h4><div class=tgt>${{esc(it.llm.target)}}</div>
        <div class=ev>${{esc(it.llm.evidence)}}</div></div>
     <div class=col><h4>Judge recommendation</h4>${{it.judge?`<div class=tgt>${{esc(it.judge.choice)}}${{(it.judge.value&&it.judge.value.length)?': '+esc(it.judge.value.join(', ')):''}}</div>
        <div class=ev>${{esc(it.judge.rationale||'')}}</div>`:'<div class=none>(judge off)</div>'}}</div>
   </div>
   <div class=choose>
     ${{p?`<label><input type=radio name=c${{i}} value=parser> Use parser (${{esc(p.target)}})</label>`:''}}
     <label><input type=radio name=c${{i}} value=llm> Use LLM (${{esc(it.llm.target)}})</label>
     <label><input type=radio name=c${{i}} value=manual onchange=this.closest('.choose').querySelector('.man').focus()> Manual:</label>
     <input type=text class=man placeholder="comma-separated, e.g. 5.202(a)(2), 6.302-2">
     <label><input type=radio name=c${{i}} value=reject> Reject (not a reference)</label>
   </div>`;
  list.appendChild(d);
 }});
 flt();
}}
function flt(){{
 const on=[...document.querySelectorAll('.f:checked')].map(x=>x.value);
 const hide=document.getElementById('hideDone').checked;
 let shown=0,decided=0;
 document.querySelectorAll('.item').forEach(d=>{{
   const i=d.dataset.i, picked=document.querySelector(`input[name=c${{i}}]:checked`);
   if(picked) decided++;
   const vis=on.includes(d.dataset.bucket) && !(hide&&picked);
   d.style.display=vis?'':'none'; d.classList.toggle('done',!!picked); if(vis)shown++;
 }});
 document.getElementById('meta').textContent=`${{Q.length}} items · ${{decided}} decided · ${{shown}} shown`;
}}
function collect(){{
 const out=[];
 Q.forEach((it,i)=>{{
   const picked=document.querySelector(`input[name=c${{i}}]:checked`); if(!picked)return;
   let value=[];
   if(picked.value=='parser'&&it.parser) value=[it.parser.target];
   else if(picked.value=='llm') value=[it.llm.target];
   else if(picked.value=='manual') value=document.querySelectorAll('.man')[i].value.split(',').map(s=>s.trim()).filter(Boolean);
   out.push({{unit:it.unit, bucket:it.bucket, choice:picked.value, value, llm_target:it.llm.target}});
 }});
 return out;
}}
function save(){{ try{{localStorage.setItem(KEY, JSON.stringify(collect()));}}catch(e){{}} }}
function applyDecisions(list){{   // restore selections from a decisions array (import or auto-save)
 const idx={{}}; Q.forEach((it,i)=>idx[it.unit+'|'+it.llm.target]=i);
 (list||[]).forEach(d=>{{
   const i=idx[d.unit+'|'+(d.llm_target!==undefined?d.llm_target:'')];
   if(i===undefined) return;
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
Q.forEach((it,i)=>{{   // pre-fill the judge's recommendation as the default selection
  if(it.judge&&it.judge.choice){{
    const r=document.querySelector(`input[name=c${{i}}][value=${{it.judge.choice}}]`);
    if(r) r.checked=true;
    if(it.judge.choice=='manual') document.querySelectorAll('.man')[i].value=(it.judge.value||[]).join(', ');
  }}
}});
const _saved=localStorage.getItem(KEY); if(_saved){{try{{applyDecisions(JSON.parse(_saved));}}catch(e){{}}}}
flt();
</script></body></html>"""

def write_review(queue, out_path, title):
    html_doc = PAGE.format(title=html.escape(title), data=json.dumps(queue, ensure_ascii=False))
    open(out_path, "w", encoding="utf-8").write(html_doc)
    return out_path
