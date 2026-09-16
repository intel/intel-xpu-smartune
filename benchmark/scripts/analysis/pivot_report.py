#!/usr/bin/env python3
"""
Pivot report generator
Reads windowed_metric_medians.csv and outputs a self-contained interactive HTML.

Each "panel" = one pivot table + its bar chart, and on the page you can:
  - Row header / column header are each an "add/remove-able dimension list" -> supports multi-level (nested) headers
  - Select the value metric (any kpi / metric)
  - Add filters on dimensions not used as rows/cols (default: all -> aggregate)
  - Choose aggregation (median/mean/min/max/count)
  - Click "+ Add panel" to clone more panels and compare different metrics side by side

Data processing: repeated test rows of the same config (model x device x batch x precision)
are folded to median per metric at generation time, so each config keeps a single row.

Usage:
  python pivot_report.py [input_csv] [output_html]
  Default: windowed_metric_medians.csv -> pivot_report.html
"""
import csv, json, sys, os
from collections import OrderedDict
import argparse
from pathlib import Path

# Candidate config dimensions (DIMS): extend here as needed, e.g. platform / model_source.
# The dimensions that actually reach the pivot table / HTML are decided by the data: dimensions whose column is missing from the CSV, or whose entire column is empty, are automatically dropped.
DIMS = ["model_name", "model_source", "platform", "device", "batch_size", "precision"]
EXCLUDE = set(DIMS + ["case_name", "case_dir"])


def _median(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def load(csv_path):
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit("CSV is empty")
    cols = list(rows[0].keys())
    dims = [d for d in DIMS if d in cols]          # keep only candidate dimensions present in the CSV
    metrics = [c for c in cols if c not in EXCLUDE]

    def parse(v):
        try:
            return float(v) if v not in ("", None) else None
        except ValueError:
            return None

    # repeated test rows of the same config, folded to median per metric -> one row per config
    buckets = OrderedDict()
    for r in rows:
        key = tuple(r.get(d, "") for d in dims)
        b = buckets.setdefault(key, {m: [] for m in metrics})
        for m in metrics:
            b[m].append(parse(r.get(m, "")))
    data = []
    n_raw = len(rows)
    for key, b in buckets.items():
        rec = dict(zip(dims, key))
        for m in metrics:
            rec[m] = _median(b[m])
        data.append(rec)

    # group metrics by prefix, convenient for dropdown optgroup
    groups = {}
    labels = {
        "kpi": "KPI Performance", "cpu": "CPU Power/Freq/Thermal", "gpu": "GPU Monitoring",
        "npu": "NPU", "memory": "Memory",
    }
    for m in metrics:
        pre = m.split("_")[0]
        groups.setdefault(labels.get(pre, pre), []).append(m)
    # drop all-empty dimensions: only dimensions with at least one non-empty value reach the pivot / HTML
    effective_dims = [d for d in dims if any(r.get(d) not in (None, "") for r in data)]
    dim_values = {d: sorted({r[d] for r in data if r.get(d) not in (None, "")})
                  for d in effective_dims}
    print("Repeated-test folding: %d raw rows -> %d configs (median per metric)" % (n_raw, len(data)))
    return data, metrics, groups, dim_values, effective_dims


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pivot Report · Table + Bar Chart</title>
<style>
  :root{--bg:#0f1420;--panel:#1a2130;--line:#2a3346;--fg:#e6ebf5;--mut:#8b97ad;
        --accent:#4da3ff;--good:#3ecf8e;--bad:#ff6b6b;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:13px/1.5 -apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
  header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;
         align-items:center;gap:16px;position:sticky;top:0;background:var(--bg);z-index:5}
  header h1{font-size:16px;margin:0;font-weight:600}
  header .sub{color:var(--mut);font-size:12px}
  button{cursor:pointer;background:var(--panel);color:var(--fg);border:1px solid var(--line);
         border-radius:7px;padding:6px 12px;font-size:13px}
  button:hover{border-color:var(--accent)}
  button.add{background:var(--accent);color:#04263f;border:0;font-weight:600}
  #panels{padding:18px;display:grid;grid-template-columns:repeat(auto-fill,minmax(560px,1fr));gap:18px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
  .ctl{display:flex;flex-wrap:wrap;gap:8px 10px;padding:12px 14px;border-bottom:1px solid var(--line);align-items:center}
  .ctl label{color:var(--mut);font-size:11px;display:flex;flex-direction:column;gap:3px}
  select{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 6px;font-size:12px;max-width:200px}
  .panel .body{padding:12px 14px 16px}
  .panel h3{margin:0;font-size:13px;font-weight:600}
  .panel .close{margin-left:auto;background:transparent;border:0;color:var(--mut);font-size:18px;padding:0 4px}
  .panel .close:hover{color:var(--bad)}
  table{border-collapse:collapse;width:100%;font-size:12px;margin-bottom:14px}
  th,td{border:1px solid var(--line);padding:5px 8px;text-align:right;white-space:nowrap}
  th{background:#141a27;color:var(--mut);font-weight:600;text-align:center}
  th.corner,td.rowh{text-align:left;background:#141a27;color:var(--fg);font-weight:600}
  td.na{color:#556; text-align:center}
  .legend{display:flex;flex-wrap:wrap;gap:10px;margin-top:6px;font-size:11px;color:var(--mut)}
  .legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
  .meta{color:var(--mut);font-size:11px;margin:2px 0 10px}
  .flt{display:flex;flex-wrap:wrap;gap:8px 10px;padding:0 14px 10px}
  .dims{padding:8px 14px 2px;display:flex;flex-direction:column;gap:7px}
  .dimrow{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
  .axl{color:var(--mut);font-size:11px;min-width:88px}
  .chip{background:#26324a;border:1px solid var(--line);border-radius:14px;padding:2px 7px 2px 11px;
        font-size:12px;display:inline-flex;align-items:center;gap:5px}
  .chip b{cursor:pointer;color:var(--mut);font-weight:700;font-size:13px}
  .chip b:hover{color:var(--bad)}
  .addsel{max-width:120px}
  .chartwrap{overflow-x:auto;max-width:100%;padding-bottom:2px}
  .tablewrap{overflow:auto;max-width:100%;max-height:60vh}
  td.best{outline:2px solid var(--good);outline-offset:-2px;font-weight:700;color:#fff}
  .ptitle{font-size:12px;font-weight:600;color:var(--fg);margin:0 0 6px}
  /* Conclusions / insights bar */
  #insights{padding:14px 18px 4px}
  #insights .cat{margin-bottom:12px}
  #insights .cath{font-size:12px;color:var(--mut);font-weight:700;margin:0 0 6px}
  #insights .cards{display:flex;flex-wrap:wrap;gap:8px}
  .insight{cursor:pointer;border:1px solid var(--line);border-left-width:4px;border-radius:8px;
           background:#141a27;padding:7px 11px;font-size:12px;max-width:520px;transition:.12s}
  .insight:hover{background:#1b2334;transform:translateY(-1px)}
  .insight .t{color:var(--fg);font-weight:600}
  .insight .d{color:var(--mut);font-size:11px;margin-top:2px}
  .insight .n{color:var(--accent);font-size:10px;margin-left:6px}
  .insight.good{border-left-color:var(--good)}
  .insight.warn{border-left-color:#f7c948}
  .insight.bad{border-left-color:var(--bad)}
  .insight.info{border-left-color:var(--mut)}
  .insight.noclick{cursor:default;opacity:.75}
  .insight.noclick:hover{transform:none;background:#141a27}
  #insights h2{font-size:14px;margin:0 0 10px;font-weight:600}
</style>
</head>
<body>
<header>
  <h1>Pivot Report · Table + Bar Chart</h1>
  <span class="sub" id="sub"></span>
  <button class="add" id="addBtn" style="margin-left:auto">+ Add panel</button>
</header>
<div id="insights"></div>
<div id="panels"></div>

<script>
const DATA = /*__DATA__*/[];
const METRICS = /*__METRICS__*/[];
const GROUPS = /*__GROUPS__*/{};
const DIMV = /*__DIMV__*/{};
const DIMS = /*__DIMS__*/[];
const UNITS = /*__UNITS__*/{};
const CONCLUSIONS = /*__CONCLUSIONS__*/[];
const COLORS = ["#4da3ff","#3ecf8e","#ff9f43","#c17bff","#ff6b6b","#5ad1e0","#f7c948","#a0d468",
                "#ff8fb1","#7ec8ff","#b5e48c","#e0aaff"];
const SEP = "";

document.getElementById("sub").textContent =
  DATA.length + " config · " + DIMS.length + " dims · " + METRICS.length + " metrics";

let uid = 0;
function agg(vals, how){
  const v = vals.filter(x=>x!=null);
  if(!v.length) return null;
  if(how==="count") return v.length;
  if(how==="mean") return v.reduce((a,b)=>a+b,0)/v.length;
  if(how==="min") return Math.min(...v);
  if(how==="max") return Math.max(...v);
  const s=[...v].sort((a,b)=>a-b), m=s.length>>1;      // median
  return s.length%2 ? s[m] : (s[m-1]+s[m])/2;
}
function fmt(v){
  if(v==null) return "–";
  const a=Math.abs(v);
  if(a>=1000) return v.toFixed(0);
  if(a>=100)  return v.toFixed(1);
  if(a>=1)    return v.toFixed(2);
  return v.toFixed(3);
}
// error function approximation (Abramowitz & Stegun 7.1.26), for the normal-distribution CDF
function erf(x){
  const s=x<0?-1:1; x=Math.abs(x);
  const t=1/(1+0.3275911*x);
  const y=1-(((((1.061405429*t-1.453152027)*t)+1.421413741)*t-0.284496736)*t+0.254829592)*t*Math.exp(-x*x);
  return s*y;
}
// standard normal CDF: z->(0,1); steepest slope at the mean, better resolution where values cluster
function normCdf(z){ return 0.5*(1+erf(z/Math.SQRT2)); }
// formatting with unit: append x / % / unit based on UNITS[metric]
function fmtU(v, metric){
  if(v==null) return "–";
  const u=UNITS[metric];
  if(u==="x") return fmt(v)+"×";
  if(u==="%") return fmt(v)+"%";
  if(u) return fmt(v)+" "+u;
  return fmt(v);
}
function optionsHTML(sel){
  let h="";
  for(const g in GROUPS){
    h+='<optgroup label="'+g+'">';
    for(const m of GROUPS[g]) h+='<option value="'+m+'"'+(m===sel?" selected":"")+'>'+m+'</option>';
    h+='</optgroup>';
  }
  return h;
}
// Cartesian product: dims=[dim,...] -> [[v1,v2,...], ...] (in DIMV order; empty dims -> [[]])
function product(dims){
  let res=[[]];
  for(const d of dims){
    const nx=[];
    for(const combo of res) for(const v of DIMV[d]) nx.push(combo.concat(v));
    res=nx;
  }
  return res;
}
function eqPrefix(a,b,level){ for(let k=0;k<=level;k++) if(a[k]!==b[k]) return false; return true; }
// contiguous group spans: keys(array of arrays), level -> span of each "group start row" (0 for non-start)
function spans(keys, level){
  const res=new Array(keys.length).fill(0);
  let i=0;
  while(i<keys.length){ let j=i+1; while(j<keys.length && eqPrefix(keys[j],keys[i],level)) j++; res[i]=j-i; i=j; }
  return res;
}

function makePanel(cfg){
  cfg = cfg || {rows:[DIMS[0]], cols:[DIMS[1]], metric:METRICS[0], how:"median", filters:{}};
  const id = ++uid;
  const el = document.createElement("div");
  el.className="panel";
  el.innerHTML = `
   <div class="ctl">
     <h3>${cfg.title ? cfg.title.replace(/</g,"&lt;") : "Panel #"+id}</h3>
     <button class="close" title="delete">×</button>
   </div>
   <div class="ctl">
     <label>Value metric<select data-k="metric">${optionsHTML(cfg.metric)}</select></label>
     <label>Aggregate<select data-k="how">
       ${["median","mean","min","max","count"].map(o=>'<option'+(o===cfg.how?" selected":"")+'>'+o+'</option>').join("")}
     </select></label>
     <label>Highlight<select data-k="highlight">
       ${[["max","maximum"],["min","minimum"]].map(o=>'<option value="'+o[0]+'"'+(o[0]===(cfg.highlight||"max")?" selected":"")+'>'+o[1]+'</option>').join("")}
     </select></label>
     <label>Coloring<select data-k="scale">
       ${[["percentile","percentile"],["normal","normal"],["linear","uniform"]].map(o=>'<option value="'+o[0]+'"'+(o[0]===(cfg.scale||"percentile")?" selected":"")+'>'+o[1]+'</option>').join("")}
     </select></label>
   </div>
   <div class="dims">
     <div class="dimrow" data-axis="rows"></div>
     <div class="dimrow" data-axis="cols"></div>
   </div>
   <div class="flt"></div>
   <div class="body"></div>`;
  document.getElementById("panels").appendChild(el);

  // keep only dimensions that actually exist in the data: conclusion panel specs may reference dropped empty dims (e.g. a missing model_name)
  const DIMSET = new Set(DIMS);
  const state = {metric:cfg.metric, how:cfg.how};
  state.rows = (cfg.rows||[]).filter(d=>DIMSET.has(d));
  state.cols = (cfg.cols||[]).filter(d=>DIMSET.has(d));
  state.filters = {};
  for(const k in (cfg.filters||{})) if(DIMSET.has(k)) state.filters[k]=cfg.filters[k];
  state.highlight = cfg.highlight || "max";   // default: highlight the maximum
  state.scale = cfg.scale || "percentile";    // coloring map: percentile/normal/uniform, default percentile
  const usedDims = ()=> new Set(state.rows.concat(state.cols));

  // add/remove-able multi-level header dimension selector
  function renderDims(){
    ["rows","cols"].forEach(axis=>{
      const wrap = el.querySelector('.dimrow[data-axis="'+axis+'"]');
      const label = axis==="rows" ? "Row header (rows)" : "Column header (cols)";
      const chips = state[axis].map((d,i)=>
        '<span class="chip">'+d+'<b data-rm="'+axis+'|'+i+'" title="remove">×</b></span>').join("");
      const avail = DIMS.filter(d=>!usedDims().has(d));
      const add = avail.length
        ? '<select class="addsel" data-add="'+axis+'"><option value="">+ dim</option>'
          + avail.map(d=>'<option value="'+d+'">'+d+'</option>').join("") + '</select>'
        : '';
      wrap.innerHTML = '<span class="axl">'+label+'</span>'+chips+add;
    });
    el.querySelectorAll("[data-rm]").forEach(b=>b.onclick=()=>{
      const p=b.dataset.rm.split("|"); state[p[0]].splice(+p[1],1); render();
    });
    el.querySelectorAll("[data-add]").forEach(s=>s.onchange=()=>{
      if(s.value){ state[s.dataset.add].push(s.value); render(); }
    });
  }

  // filters (only on dimensions not used as rows/cols)
  function renderFilters(){
    const wrap = el.querySelector(".flt");
    const used = usedDims();
    const free = DIMS.filter(d=>!used.has(d));
    for(const k in state.filters) if(!free.includes(k)) delete state.filters[k];
    wrap.innerHTML = free.length ? free.map(d=>{
      const cur = state.filters[d]||"__all__";
      const opts = ['<option value="__all__">all (aggregate)</option>']
        .concat(DIMV[d].map(v=>'<option value="'+v+'"'+(v===cur?" selected":"")+'>'+v+'</option>')).join("");
      return '<label>'+d+' filter<select data-flt="'+d+'">'+opts+'</select></label>';
    }).join("") : '<span class="meta">all dimensions are used as row/col headers</span>';
    wrap.querySelectorAll("select").forEach(s=>s.onchange=()=>{
      const d=s.dataset.flt;
      if(s.value==="__all__") delete state.filters[d]; else state.filters[d]=s.value;
      render();
    });
  }

  function render(){
    renderDims(); renderFilters();
    const {rows:rowDims, cols:colDims, metric, how, filters}=state;
    const data = DATA.filter(r=>Object.entries(filters).every(([k,v])=>r[k]===v));
    // accumulate into bag[rowKey][colKey] = [values...]
    const bag={};
    for(const r of data){
      const rkey = rowDims.map(d=>r[d]).join(SEP);
      const ckey = colDims.map(d=>r[d]).join(SEP);
      (bag[rkey]=bag[rkey]||{}); (bag[rkey][ckey]=bag[rkey][ckey]||[]).push(r[metric]);
    }
    const K = a=>a.join(SEP);
    const val = (rw,ck)=>{ const b=bag[K(rw)]; return b && b[K(ck)]!=null ? agg(b[K(ck)],how) : null; };
    // full combinations (in DIMV order), then drop all-empty rows/cols
    let rowKeys = product(rowDims), colKeys = product(colDims);
    colKeys = colKeys.filter(ck=>rowKeys.some(rw=>val(rw,ck)!=null));
    rowKeys = rowKeys.filter(rw=>colKeys.some(ck=>val(rw,ck)!=null));
    let mn=Infinity,mx=-Infinity;
    for(const rw of rowKeys)for(const ck of colKeys){const a=val(rw,ck); if(a!=null){mn=Math.min(mn,a);mx=Math.max(mx,a);}}

    const activeF = Object.entries(filters).map(([k,v])=>k+"="+v).join(" , ");
    const title = state.title ? '<div class="ptitle">'+state.title+'</div>' : '';
    const meta = '<div class="meta">value = <b>'+metric+'</b> · '+how+
                 (activeF?(' · filter: '+activeF):'')+'</div>';
    el.querySelector(".body").innerHTML =
      title + meta + tableHTML(rowDims,colDims,rowKeys,colKeys,val,mn,mx,metric,state.highlight,state.scale)
           + chartHTML(rowDims,colDims,rowKeys,colKeys,val,metric);
  }

  // ---- multi-level header pivot table ----
  function tableHTML(rowDims,colDims,rowKeys,colKeys,val,mn,mx,metric,highlight,scale){
    const best = highlight==="max" ? mx : highlight==="min" ? mn : null;
    if(!rowKeys.length || !colKeys.length) return '<div class="meta">no data under current filters</div>';
    const rLev = Math.max(rowDims.length,1);
    const cornerLbl = (rowDims.join(" / ")||'row')
                    + ' \\ ' + (colDims.join(" / ")||'col');
    // collect visible cell values for the three coloring maps
    const vals=[];
    for(const rw of rowKeys)for(const ck of colKeys){const a=val(rw,ck); if(a!=null) vals.push(a);}
    const N=vals.length;
    const mean=N?vals.reduce((s,x)=>s+x,0)/N:0;
    const sd=N?Math.sqrt(vals.reduce((s,x)=>s+(x-mean)*(x-mean),0)/N):0;
    const sorted=[...vals].sort((x,y)=>x-y);
    const PBANDS=10;   // number of percentile bands (one color per band)
    // binary-search count of <a and <=a in sorted, take average rank as percentile, then discretize into PBANDS bands
    function bisect(arr, x, incl){ let lo=0,hi=arr.length; while(lo<hi){const m=(lo+hi)>>1; if(incl?arr[m]<=x:arr[m]<x) lo=m+1; else hi=m;} return lo; }
    function pctBand(a){
      if(N<=1) return 0.5;
      const p=((bisect(sorted,a,false)+bisect(sorted,a,true))/2 - 0.5)/(N-1);  // average rank -> (0,1)
      const b=Math.min(PBANDS-1, Math.max(0, Math.floor(p*PBANDS)));            // band
      return b/(PBANDS-1);
    }
    // map value -> [0,1] intensity under the current mapping
    function heat(a){
      if(scale==="linear")     return mx>mn ? (a-mn)/(mx-mn) : 0.5;
      if(scale==="normal")     return sd>0 ? normCdf((a-mean)/sd) : 0.5;
      return pctBand(a);       // percentile (default)
    }
    let t='<div class="tablewrap"><table>';
    // column header (nested)
    if(colDims.length===0){
      t+='<tr><th class="corner" colspan="'+rLev+'">'+cornerLbl+'</th><th>value</th></tr>';
    } else {
      for(let L=0;L<colDims.length;L++){
        t+='<tr>';
        if(L===0) t+='<th class="corner" colspan="'+rLev+'" rowspan="'+colDims.length+'">'+cornerLbl+'</th>';
        const sp=spans(colKeys,L);
        colKeys.forEach((ck,i)=>{ if(sp[i]) t+='<th colspan="'+sp[i]+'">'+ck[L]+'</th>'; });
        t+='</tr>';
      }
    }
    // row-header rowspan precompute
    const rowSpans=[];
    for(let L=0;L<rowDims.length;L++) rowSpans.push(spans(rowKeys,L));
    rowKeys.forEach((rw,ri)=>{
      t+='<tr>';
      if(rowDims.length===0) t+='<td class="rowh">value</td>';
      else for(let L=0;L<rowDims.length;L++) if(rowSpans[L][ri]) t+='<td class="rowh" rowspan="'+rowSpans[L][ri]+'">'+rw[L]+'</td>';
      colKeys.forEach(ck=>{
        const a=val(rw,ck);
        if(a==null){ t+='<td class="na">–</td>'; return; }
        // current coloring map (percentile/normal/uniform) + highlight direction: invert when highlighting min, smaller is darker
        const norm = heat(a);
        const f = highlight==="min" ? 1-norm : norm;
        const isBest = best!=null && a===best;
        t+='<td'+(isBest?' class="best"':'')+' style="background:rgba(77,163,255,'+(0.08+0.5*f).toFixed(3)+')">'+fmtU(a,metric)+'</td>';
      });
      t+='</tr>';
    });
    return t+'</table></div>';
  }

  // ---- multi-level grouped bar chart: group=row leaf, series=col leaf, upper row dims shown in parentheses ----
  function chartHTML(rowDims,colDims,rowKeys,colKeys,val,metric){
    if(!rowKeys.length || !colKeys.length) return '';
    const K=a=>a.join(SEP);
    const nS=colKeys.length;
    const barW=Math.max(6, Math.min(26, Math.round(150/nS)));
    const gap=14, groupW=nS*barW+gap;
    const padL=48,padR=12,padT=10, twoTier=rowDims.length>1, padB=twoTier?66:46, H=250;
    const W=Math.max(340, padL+padR+rowKeys.length*groupW);
    const ph=H-padT-padB;
    let mx=-Infinity,mn=0;
    for(const rw of rowKeys)for(const ck of colKeys){const a=val(rw,ck); if(a!=null){mx=Math.max(mx,a);mn=Math.min(mn,a,0);}}
    if(mx===-Infinity) return '';
    if(mx===mn) mx=mn+1;
    const y=v=>padT+ph-(v-mn)/(mx-mn)*ph;
    let s='<div class="chartwrap"><svg width="'+W+'" height="'+H+'" viewBox="0 0 '+W+' '+H+'">';
    for(let i=0;i<=4;i++){const vv=mn+(mx-mn)*i/4, yy=y(vv);
      s+='<line x1="'+padL+'" x2="'+(W-padR)+'" y1="'+yy+'" y2="'+yy+'" stroke="#2a3346"/>';
      s+='<text x="'+(padL-4)+'" y="'+(yy+3)+'" fill="#8b97ad" font-size="9" text-anchor="end">'+fmt(vv)+'</text>';}
    rowKeys.forEach((rw,gi)=>{
      const gx=padL+gi*groupW+gap/2;
      colKeys.forEach((ck,si)=>{
        const a=val(rw,ck); if(a==null) return;
        const bx=gx+si*barW, by=y(a), bh=padT+ph-by;
        s+='<rect x="'+bx.toFixed(1)+'" y="'+by.toFixed(1)+'" width="'+(barW-2).toFixed(1)+'" height="'+bh.toFixed(1)+
           '" fill="'+COLORS[si%COLORS.length]+'"><title>'+(rw.join("/")||'value')+' | '+(ck.join("/")||'value')+'\n'+fmtU(a,metric)+'</title></rect>';
      });
      const leaf=(rw[rw.length-1]||"value")+"";
      s+='<text x="'+(gx+nS*barW/2).toFixed(1)+'" y="'+(H-padB+13)+'" fill="#e6ebf5" font-size="9" text-anchor="middle">'+
         (leaf.length>8?leaf.slice(0,7)+'…':leaf)+'</text>';
    });
    if(twoTier){   // bracket annotation for the upper row dimension
      const sp=spans(rowKeys,rowDims.length-2);
      rowKeys.forEach((rw,gi)=>{ if(!sp[gi]) return;
        const x0=padL+gi*groupW+2, x1=padL+(gi+sp[gi])*groupW-2, yb=H-padB+30;
        s+='<line x1="'+x0+'" x2="'+x1+'" y1="'+yb+'" y2="'+yb+'" stroke="#4a5568"/>';
        s+='<text x="'+((x0+x1)/2).toFixed(1)+'" y="'+(yb+13)+'" fill="#8b97ad" font-size="9" text-anchor="middle">'+
           rw.slice(0,rowDims.length-1).join("/")+'</text>';
      });
    }
    s+='</svg></div>';
    s+='<div class="legend">'+colKeys.map((ck,i)=>
        '<span><i style="background:'+COLORS[i%COLORS.length]+'"></i>'+(ck.join(" · ")||'value')+'</span>').join("")+'</div>';
    return s;
  }

  el.querySelectorAll('.ctl select[data-k]').forEach(sel=>sel.onchange=()=>{
    state[sel.dataset.k]=sel.value; render();
  });
  el.querySelector(".close").onclick=()=>el.remove();
  render();
  return state;
}

document.getElementById("addBtn").onclick=()=>makePanel();

// ---- conclusions / insights bar: each conclusion is clickable and expands its one or more panels ----
function openConclusion(c){
  if(!c.panels || !c.panels.length) return;
  const first = document.getElementById("panels").children.length;
  c.panels.forEach(p=>makePanel(p));
  const kids = document.getElementById("panels").children;
  if(kids[first]) kids[first].scrollIntoView({behavior:"smooth", block:"start"});
}
function renderInsights(){
  const root=document.getElementById("insights");
  if(!CONCLUSIONS.length){ root.innerHTML=""; return; }
  const order=[], byCat={};
  for(const c of CONCLUSIONS){ if(!byCat[c.category]){byCat[c.category]=[];order.push(c.category);} byCat[c.category].push(c); }
  let h='<h2>Conclusions / Insights · click any item to expand its panels</h2>';
  for(const cat of order){
    h+='<div class="cat"><div class="cath">'+cat+'</div><div class="cards">';
    byCat[cat].forEach((c,i)=>{
      const has=c.panels && c.panels.length;
      h+='<div class="insight '+c.severity+(has?'':' noclick')+'" data-cat="'+cat+'" data-i="'+i+'">'
       + '<div class="t">'+c.title+(has?'<span class="n">panels x'+c.panels.length+'</span>':'')+'</div>'
       + (c.detail?'<div class="d">'+c.detail+'</div>':'')+'</div>';
    });
    h+='</div></div>';
  }
  root.innerHTML=h;
  root.querySelectorAll(".insight").forEach(el=>{
    el.onclick=()=>openConclusion(byCat[el.dataset.cat][+el.dataset.i]);
  });
}
renderInsights();
// by default expand the first conclusion that has panels (usually L1 primary KPI best), so the page isn't empty
const firstWithPanels = CONCLUSIONS.find(c=>c.panels && c.panels.length);
if(firstWithPanels) openConclusion(firstWithPanels);
</script>
</body>
</html>
"""


def build(csv_path, out_path, profile=None, json_path=None):
    res = None
    if json_path == None:
      import analyze
      res = analyze.enrich(csv_path, profile)   # analysis engine outputs data + derived metrics + conclusions
      middle_json = Path(out_path).with_suffix(".json")
      with open(middle_json, "w", encoding="utf-8") as f:
          json.dump(res, f, ensure_ascii=False, indent=2)
    else:
      with open(json_path, "r", encoding="utf-8") as f:
        res = json.load(f)
    html = (HTML
            .replace("/*__DATA__*/[]", json.dumps(res["data"], ensure_ascii=False))
            .replace("/*__METRICS__*/[]", json.dumps(res["metrics"], ensure_ascii=False))
            .replace("/*__GROUPS__*/{}", json.dumps(res["groups"], ensure_ascii=False))
            .replace("/*__DIMV__*/{}", json.dumps(res["dim_values"], ensure_ascii=False))
            .replace("/*__DIMS__*/[]", json.dumps(res["dims"], ensure_ascii=False))
            .replace("/*__UNITS__*/{}", json.dumps(res["units"], ensure_ascii=False))
            .replace("/*__CONCLUSIONS__*/[]", json.dumps(res["conclusions"], ensure_ascii=False)))
    with open(out_path, "w") as f:
        f.write(html)
    print("Generated:", out_path, "(", len(html), "bytes )")
    print("profile=%s · %d config · %d metrics · %d conclusions" % (
        res["profile"]["name"], len(res["data"]), len(res["metrics"]), len(res["conclusions"])))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='analysis windowed_metric_medians.csv and generate a pivot report HTML'
    )
    parser.add_argument('input_path', help='Case directory containing windowed_metric_medians.csv')
    parser.add_argument('--middle-json', action='store_true', help='json input for AI extra input conclusion', default=None)
    args = parser.parse_args()

    input_path = args.input_path
    csv_file = Path(input_path) / "windowed_metric_medians.csv"
    json_file = Path(input_path) / "pivot_report.json"
    if not csv_file.exists():
        raise SystemExit("windowed_metric_medians.csv not found: " + str(csv_file))
    if args.middle_json and not json_file.exists():
        raise SystemExit("pivot_report.json not found: " + str(json_file))

    out = Path(input_path) / "pivot_report.html"
    prof = None
    if not os.path.exists(input_path):
        raise SystemExit("Input file not found: " + input_path)
    if(args.middle_json):
        build(str(csv_file), str(out), prof, str(json_file))
    else:
        build(str(csv_file), str(out), prof)
