"""dubeval-viz: render a dubeval report JSON into one self-contained HTML file (no network, no build).

Two synced players (original / dub) with an A/B audio toggle, a full-length timeline of original
lines, dub spans and issues per source lane, a filterable issues table and the aligned-lines table.
Accepts a v2 BatchReport ({"reports": [...]}) or a single v1/v2 Report.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>dubeval report</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mute:#555;--line:#ddd;--crit:#b00020;--major:#c25e00;--minor:#5a6b00;--orig:#2f5d9e;--dub:#2e7d4f;--hl:#fff3c4}
*{box-sizing:border-box}body{margin:0;font:14px/1.4 system-ui,sans-serif;color:var(--fg);background:var(--bg)}
header,main{padding:12px 16px}header{border-bottom:1px solid var(--line)}h1{font-size:18px;margin:0 0 6px}h2{font-size:15px;margin:18px 0 6px}
.kv{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--mute)}.kv b{color:var(--fg)}
.players{display:grid;grid-template-columns:1fr 1fr;gap:12px}.players figure{margin:0}.players video{width:100%;background:#000}
figcaption{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:4px 0}
button,select,input{font:inherit;padding:3px 8px}button:focus-visible,select:focus-visible,input:focus-visible,tr:focus-visible{outline:2px solid #1a5fb4;outline-offset:1px}
#timeline{width:100%;display:block;border:1px solid var(--line);cursor:pointer;background:#fafafa}
.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:6px 0}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}
th{position:sticky;top:0;background:#f3f3f3}th button{border:0;background:none;padding:0;font-weight:600;cursor:pointer}
tbody tr{cursor:pointer}tbody tr.active{background:var(--hl)}tbody tr.sel{outline:2px solid #1a5fb4}
.sev{font-weight:600;text-transform:uppercase;font-size:12px}.sev.critical{color:var(--crit)}.sev.major{color:var(--major)}.sev.minor{color:var(--minor)}
.small{font-size:12px;color:var(--mute)}.bar{display:inline-block;height:8px;background:var(--dub);vertical-align:middle}
.wrap{max-height:420px;overflow:auto;border:1px solid var(--line)}.warn{color:var(--major)}a.iss{margin-right:4px}
</style></head><body>
<header><h1 id="title">dubeval report</h1>
<label>Pair <select id="pair" aria-label="Language pair"></select></label>
<div class="kv" id="summary" aria-live="polite"></div><div id="notes" class="small"></div></header>
<main>
<section class="players" aria-label="Video players">
<figure><figcaption><span>Original <span id="olab" class="small"></span></span>
<button id="omute" aria-pressed="false">Audio: on</button></figcaption><video id="vo" controls preload="metadata"></video></figure>
<figure><figcaption><span>Dub <span id="dlab" class="small"></span></span>
<button id="dmute" aria-pressed="true">Audio: off</button></figcaption><video id="vd" controls preload="metadata" muted></video>
<div class="small">Browsers play the file's default audio track; pick other tracks with an external player.</div></figure>
</section>
<h2>Timeline <span class="small">(click to seek; lanes: original lines, dub speech, issues by source)</span></h2>
<canvas id="timeline" height="200" role="img" aria-label="Timeline of lines and issues"></canvas>
<h2>Issues <span id="icount" class="small"></span></h2>
<div class="filters">
<label>Severity <select id="fsev"><option value="">all</option><option>critical</option><option>major</option><option>minor</option></select></label>
<label>Source <select id="fsrc"><option value="">all</option></select></label>
<label>Category <select id="fcat"><option value="">all</option></select></label>
<label>Search <input id="fq" type="search" placeholder="text…"></label>
<label><input id="follow" type="checkbox" checked> follow playback</label></div>
<div class="wrap"><table id="issues"><thead><tr>
<th><button data-sort="id">id</button></th><th><button data-sort="start">start</button></th><th>end</th>
<th><button data-sort="severity">severity</button></th><th>source / category</th><th>description &amp; fix</th><th>text</th><th>visual</th>
</tr></thead><tbody></tbody></table></div>
<h2>Aligned lines</h2>
<div class="wrap"><table id="lines"><thead><tr><th>#</th><th>orig</th><th>dub</th><th>onset Δ</th><th>coverage</th>
<th>original</th><th>original (EN)</th><th>dub</th><th>dub (EN)</th><th>issues</th></tr></thead><tbody></tbody></table></div>
</main>
<script id="data" type="application/json">__DATA__</script>
<script id="media" type="application/json">__MEDIA__</script>
<script>
"use strict";
const RAW=JSON.parse(document.getElementById("data").textContent);
const MEDIA=JSON.parse(document.getElementById("media").textContent);
const REPORTS=RAW.reports?RAW.reports:[RAW];
const SEVR={critical:3,major:2,minor:1};
const COL={critical:"#b00020",major:"#c25e00",minor:"#8a9a00"};
const SOURCES=["timing","audio","semantic","visual","language","subtitle","video"];
const $=id=>document.getElementById(id);
const vo=$("vo"),vd=$("vd"),cv=$("timeline");
let R=null,sortKey="start",sortDir=1,selected=null;

function el(tag,text,cls){const e=document.createElement(tag);if(text!=null)e.textContent=String(text);if(cls)e.className=cls;return e}
function fmt(t){if(t==null)return"–";const m=Math.floor(t/60),s=(t%60).toFixed(2).padStart(5,"0");return m+":"+s}
function pairName(r,i){return r.pair||(r.inputs&&r.inputs.dubbed)||("report "+(i+1))}

REPORTS.forEach((r,i)=>{const o=el("option",pairName(r,i)+(r.target?"  [a:"+r.target.audio_index+"]":""));o.value=i;$("pair").appendChild(o)});
$("pair").hidden=REPORTS.length<2;$("pair").parentElement.hidden=REPORTS.length<2;
$("pair").addEventListener("change",e=>load(+e.target.value));

function load(i){
  R=REPORTS[i];selected=null;
  const m=MEDIA[i]||{};
  if(m.original&&vo.getAttribute("src")!==m.original)vo.src=m.original;
  if(m.dubbed&&vd.getAttribute("src")!==m.dubbed)vd.src=m.dubbed;
  $("olab").textContent=m.original||"(no file)";$("dlab").textContent=m.dubbed||"(no file)";
  const s=R.summary,sc=s.severity_counts||{};
  $("title").textContent="dubeval · "+pairName(R,i);
  const kv=$("summary");kv.replaceChildren();
  [["score",s.score+"/100"],["critical",sc.critical],["major",sc.major],["minor",sc.minor],["duration",fmt(s.duration)],
   ["speech overlap",Math.round(100*s.speech_overlap_ratio)+"%"],["unmatched dub",Math.round(100*s.unmatched_dub_ratio)+"%"],
   ["median onset",s.median_onset_delta+"s"],["p90 |onset|",s.p90_abs_onset_delta+"s"],["bg offset",s.background_offset_ms==null?"–":s.background_offset_ms+" ms"]]
   .forEach(([k,v])=>{const d=el("span",k+": ");d.appendChild(el("b",v));kv.appendChild(d)});
  const src=R.source&&R.source.language,tgt=R.target&&R.target.language;
  if(src||tgt){const d=el("span","languages: ");d.appendChild(el("b",(src?src.name:"?")+" → "+(tgt?tgt.name:"?")));kv.appendChild(d)}
  const notes=$("notes");notes.replaceChildren();
  (R.warnings||[]).forEach(w=>notes.appendChild(el("div","warning: "+w,"warn")));
  (RAW.skipped_tracks||[]).forEach(t=>notes.appendChild(el("div","skipped "+(t.file||"")+" a:"+t.index+" — "+t.reason)));
  (RAW.failed||[]).forEach(f=>notes.appendChild(el("div","FAILED "+f.dub+" — "+f.error,"warn")));
  fillSelect($("fsrc"),[...new Set(R.issues.map(x=>x.source))].sort());
  fillSelect($("fcat"),[...new Set(R.issues.map(x=>x.category))].sort());
  renderIssues();renderLines();draw();
}
function fillSelect(sel,vals){const keep=sel.value;sel.replaceChildren(el("option","all"));sel.firstChild.value="";
  vals.forEach(v=>{const o=el("option",v);o.value=v;sel.appendChild(o)});sel.value=vals.includes(keep)?keep:""}

function filtered(){
  const sev=$("fsev").value,src=$("fsrc").value,cat=$("fcat").value,q=$("fq").value.trim().toLowerCase();
  return R.issues.filter(x=>(!sev||x.severity===sev)&&(!src||x.source===src)&&(!cat||x.category===cat)&&
    (!q||[x.description,x.fix_hint,x.original_text,x.dubbed_text,x.category,x.id].join(" ").toLowerCase().includes(q)))
   .sort((a,b)=>{const k=sortKey;const va=k==="severity"?SEVR[a.severity]:a[k],vb=k==="severity"?SEVR[b.severity]:b[k];
     return (va<vb?-1:va>vb?1:0)*sortDir||a.start-b.start});
}
function renderIssues(){
  const tb=$("issues").tBodies[0];tb.replaceChildren();const rows=filtered();
  $("icount").textContent="("+rows.length+" of "+R.issues.length+")";
  rows.forEach(x=>{
    const tr=document.createElement("tr");tr.tabIndex=0;tr.dataset.id=x.id;tr.dataset.start=x.start;tr.dataset.end=x.end;
    tr.appendChild(el("td",x.id));tr.appendChild(el("td",x.start_tc||fmt(x.start)));tr.appendChild(el("td",x.end_tc||fmt(x.end)));
    tr.appendChild(el("td",x.severity,"sev "+x.severity));
    const sc=el("td",x.source);sc.appendChild(document.createElement("br"));sc.appendChild(el("b",x.category));
    sc.appendChild(el("div","conf "+x.confidence,"small"));tr.appendChild(sc);
    const d=el("td",x.description);d.appendChild(el("div","fix: "+x.fix_hint,"small"));tr.appendChild(d);
    const t=el("td");if(x.original_text)t.appendChild(el("div","orig: "+x.original_text,"small"));
    if(x.dubbed_text)t.appendChild(el("div","dub: "+x.dubbed_text,"small"));tr.appendChild(t);
    const v=x.visual;tr.appendChild(el("td",v?("speaking: "+(v.speaking_likely==null?"no face":v.speaking_likely)+
      " · mouth "+v.mouth_activity+" · face "+v.max_face_area):"","small"));
    if(selected===x.id)tr.classList.add("sel");
    const go=()=>selectIssue(x.id,true);tr.addEventListener("click",go);
    tr.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();go()}});
    tb.appendChild(tr);
  });
}
function renderLines(){
  const tb=$("lines").tBodies[0];tb.replaceChildren();
  R.aligned_lines.forEach(l=>{
    const tr=document.createElement("tr");tr.tabIndex=0;tr.dataset.start=l.orig_start;tr.dataset.end=Math.max(l.orig_end,l.dub_end||0);
    tr.appendChild(el("td",l.index));tr.appendChild(el("td",fmt(l.orig_start)+"–"+fmt(l.orig_end)));
    tr.appendChild(el("td",l.dub_start==null?"–":fmt(l.dub_start)+"–"+fmt(l.dub_end)));
    tr.appendChild(el("td",l.onset_delta==null?"–":(l.onset_delta>0?"+":"")+l.onset_delta+"s"));
    const c=el("td");const b=el("span",null,"bar");b.style.width=Math.round(60*l.dub_coverage)+"px";c.appendChild(b);
    c.appendChild(el("span"," "+Math.round(100*l.dub_coverage)+"%","small"));tr.appendChild(c);
    [l.original_text,l.original_en,l.dubbed_text,l.dubbed_en].forEach(s=>tr.appendChild(el("td",s||"")));
    const it=el("td");(l.issue_ids||[]).forEach(id=>{const a=el("a",id,"iss");a.href="#";
      a.addEventListener("click",e=>{e.preventDefault();e.stopPropagation();clearFilters();selectIssue(id,true)});it.appendChild(a)});
    tr.appendChild(it);
    tr.addEventListener("click",()=>seek(l.orig_start-0.5));
    tr.addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();seek(l.orig_start-0.5)}});
    tb.appendChild(tr);
  });
}
function clearFilters(){["fsev","fsrc","fcat"].forEach(i=>$(i).value="");$("fq").value="";renderIssues()}
function selectIssue(id,doSeek){
  selected=id;const x=R.issues.find(i=>i.id===id);if(!x)return;
  if(doSeek)seek(x.start-1);
  document.querySelectorAll("#issues tbody tr").forEach(tr=>tr.classList.toggle("sel",tr.dataset.id===id));
  const tr=document.querySelector('#issues tbody tr[data-id="'+CSS.escape(id)+'"]');if(tr)tr.scrollIntoView({block:"nearest"});
  draw();
}

// ---- synced players
let syncing=false;
function seek(t){t=Math.max(0,t);[vo,vd].forEach(v=>{try{v.currentTime=t}catch(e){}});draw()}
function mirror(src,dst){
  src.addEventListener("play",()=>{if(!syncing&&dst.paused){syncing=true;dst.play().catch(()=>{}).finally(()=>syncing=false)}});
  src.addEventListener("pause",()=>{if(!syncing&&!dst.paused){syncing=true;dst.pause();syncing=false}});
  src.addEventListener("seeked",()=>{if(Math.abs(dst.currentTime-src.currentTime)>0.15)dst.currentTime=src.currentTime});
  src.addEventListener("ratechange",()=>{dst.playbackRate=src.playbackRate});
}
mirror(vo,vd);mirror(vd,vo);
setInterval(()=>{if(!vo.paused&&Math.abs(vd.currentTime-vo.currentTime)>0.2)vd.currentTime=vo.currentTime},1000);
function muteBtn(btn,v){const upd=()=>{btn.textContent="Audio: "+(v.muted?"off":"on");btn.setAttribute("aria-pressed",String(!v.muted))};
  btn.addEventListener("click",()=>{v.muted=!v.muted;upd()});upd()}
muteBtn($("omute"),vo);muteBtn($("dmute"),vd);
function now(){return vo.currentTime||vd.currentTime||0}
vo.addEventListener("timeupdate",tick);vd.addEventListener("timeupdate",tick);
function tick(){
  const t=now();draw();
  const act=tr=>t>=+tr.dataset.start&&t<=+tr.dataset.end;
  let firstLine=null,firstIss=null;
  document.querySelectorAll("#lines tbody tr").forEach(tr=>{const a=act(tr);tr.classList.toggle("active",a);if(a&&!firstLine)firstLine=tr});
  document.querySelectorAll("#issues tbody tr").forEach(tr=>{const a=act(tr);tr.classList.toggle("active",a);if(a&&!firstIss)firstIss=tr});
  if($("follow").checked&&!(vo.paused&&vd.paused)){[firstLine,firstIss].forEach(r=>{if(r)r.scrollIntoView({block:"nearest"})})}
}

// ---- timeline
const LANES=()=>["original","dub",...SOURCES.filter(s=>R.issues.some(x=>x.source===s))];
function draw(){
  if(!R)return;const dpr=window.devicePixelRatio||1,W=cv.clientWidth,lanes=LANES(),LH=20,L=70,H=lanes.length*LH+18;
  cv.style.height=H+"px";cv.width=W*dpr;cv.height=H*dpr;const g=cv.getContext("2d");g.scale(dpr,dpr);
  const D=R.summary.duration||1,x=t=>L+(W-L-8)*t/D;
  g.clearRect(0,0,W,H);g.font="11px system-ui,sans-serif";g.textBaseline="middle";
  lanes.forEach((name,i)=>{const y=i*LH;g.fillStyle=i%2?"#f4f4f4":"#fafafa";g.fillRect(0,y,W,LH);g.fillStyle="#333";g.fillText(name,4,y+LH/2)});
  g.fillStyle="#2f5d9e";R.aligned_lines.forEach(l=>g.fillRect(x(l.orig_start),3,Math.max(1,x(l.orig_end)-x(l.orig_start)),LH-6));
  g.fillStyle="#2e7d4f";R.aligned_lines.forEach(l=>{if(l.dub_start!=null)g.fillRect(x(l.dub_start),LH+3,Math.max(1,x(l.dub_end)-x(l.dub_start)),LH-6)});
  const vis=new Set(filtered().map(i=>i.id));
  R.issues.slice().sort((a,b)=>SEVR[a.severity]-SEVR[b.severity]).forEach(it=>{
    const li=lanes.indexOf(it.source);if(li<0)return;const y=li*LH;
    g.globalAlpha=vis.has(it.id)?1:0.2;g.fillStyle=COL[it.severity];
    g.fillRect(x(it.start),y+3,Math.max(2,x(it.end)-x(it.start)),LH-6);
    if(it.id===selected){g.globalAlpha=1;g.strokeStyle="#1a5fb4";g.lineWidth=2;g.strokeRect(x(it.start)-1,y+2,Math.max(4,x(it.end)-x(it.start))+2,LH-4)}
  });g.globalAlpha=1;
  const ty=lanes.length*LH;g.fillStyle="#555";
  for(let s=0;s<=D;s+=30){const xx=x(s);g.fillRect(xx,ty,1,4);g.fillText(fmt(s).replace(/\.\d+$/,""),xx+2,ty+10)}
  const px=x(now());g.fillStyle="#000";g.fillRect(px-1,0,2,ty);
}
cv.addEventListener("click",e=>{const r=cv.getBoundingClientRect(),L=70,W=r.width;const t=(e.clientX-r.left-L)/(W-L-8)*(R.summary.duration||1);
  if(t>=0)seek(t)});
window.addEventListener("resize",draw);
["fsev","fsrc","fcat"].forEach(i=>$(i).addEventListener("change",()=>{renderIssues();draw()}));
$("fq").addEventListener("input",()=>{renderIssues();draw()});
document.querySelectorAll("th button[data-sort]").forEach(b=>b.addEventListener("click",()=>{
  const k=b.dataset.sort;sortDir=sortKey===k?-sortDir:(k==="severity"?-1:1);sortKey=k;renderIssues()}));
load(0);
</script></body></html>
"""


def _media_paths(report: dict, original: str | None, dubbed: str | None, out_dir: Path) -> dict:
    src = original or (report.get("source") or {}).get("file") or (report.get("inputs") or {}).get("original")
    dub = dubbed or (report.get("target") or {}).get("file") or (report.get("inputs") or {}).get("dubbed")

    def rel(p):
        return None if not p else Path(os.path.relpath(Path(p).resolve(), out_dir.resolve())).as_posix()
    return {"original": rel(src), "dubbed": rel(dub)}


def render(data: dict, out: Path, original: str | None = None, dubbed: str | None = None) -> Path:
    reports = data["reports"] if "reports" in data else [data]
    media = [_media_paths(r, original, dubbed, out.parent) for r in reports]

    def embed(obj) -> str:  # safe inside <script>: no "</script" or "<!--" can appear
        return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")
    out.write_text(HTML.replace("__DATA__", embed(data)).replace("__MEDIA__", embed(media)), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dubeval-viz", description=__doc__)
    p.add_argument("report", type=Path)
    p.add_argument("-o", "--out", type=Path, help="default: <report>.html")
    p.add_argument("--original", help="override original video path (all pairs)")
    p.add_argument("--dubbed", help="override dubbed video path (all pairs)")
    a = p.parse_args(argv)
    out = a.out or a.report.with_suffix(".html")
    render(json.loads(a.report.read_text(encoding="utf-8")), out, a.original, a.dubbed)
    print(f"wrote {out}")
    return 0
