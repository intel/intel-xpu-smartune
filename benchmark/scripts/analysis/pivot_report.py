#!/usr/bin/env python3
"""
透视报表生成器
读取 windowed_metric_medians.csv，输出一个自包含的交互式 HTML。

每个"面板" = 一张透视表 + 对应柱状图，可在页面上：
  - 纵向表头(行) / 横向表头(列) 各自是一个"可增删的维度列表" -> 支持多级(嵌套)表头
  - 选择取值指标(任意 kpi / metric)
  - 对未用作行/列的维度加过滤(默认 全部→聚合)
  - 选择聚合方式(median/mean/min/max/count)
  - 点 "+ 添加面板" 复制出更多面板，并排对比不同指标

数据处理：相同 config (model×device×batch×precision) 的重复 test 行，
在生成阶段就按每个指标折叠成 median，每个 config 只保留一行。

用法:
  python pivot_report.py [输入csv] [输出html]
  默认: windowed_metric_medians.csv -> pivot_report.html
"""
import csv, json, sys, os
from collections import OrderedDict
import argparse
from pathlib import Path

# 候选配置维度（DIMS）：按需在此扩展，如 platform / model_source 等额外配置。
# 真正落到透视表 / HTML 的维度由数据决定：CSV 中缺失该列、或整列均为空值的维度会被自动剔除。
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
        raise SystemExit("CSV 为空")
    cols = list(rows[0].keys())
    dims = [d for d in DIMS if d in cols]          # 仅保留 CSV 中存在的候选维度
    metrics = [c for c in cols if c not in EXCLUDE]

    def parse(v):
        try:
            return float(v) if v not in ("", None) else None
        except ValueError:
            return None

    # 相同 config 的重复 test 行, 按每个指标折叠成 median -> 每个 config 一行
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

    # 指标按前缀分组, 便于下拉框 optgroup
    groups = {}
    labels = {
        "kpi": "KPI 性能", "cpu": "CPU 功耗/频率/热", "gpu": "GPU 监控",
        "npu": "NPU", "memory": "内存",
    }
    for m in metrics:
        pre = m.split("_")[0]
        groups.setdefault(labels.get(pre, pre), []).append(m)
    # 剔除整列空值的维度：只有存在非空取值的维度才最终落到透视 / HTML
    effective_dims = [d for d in dims if any(r.get(d) not in (None, "") for r in data)]
    dim_values = {d: sorted({r[d] for r in data if r.get(d) not in (None, "")})
                  for d in effective_dims}
    print("重复 test 折叠: %d 原始行 -> %d 个 config(每指标取median)" % (n_raw, len(data)))
    return data, metrics, groups, dim_values, effective_dims


HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>透视报表 · 表格+柱状图</title>
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
  /* 结论/洞察栏 */
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
  <h1>透视报表 · 表格 + 柱状图</h1>
  <span class="sub" id="sub"></span>
  <button class="add" id="addBtn" style="margin-left:auto">+ 添加面板</button>
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
  DATA.length + " config · " + DIMS.length + " 维度 · " + METRICS.length + " 指标";

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
// 误差函数近似(Abramowitz & Stegun 7.1.26), 供正态分布 CDF 使用
function erf(x){
  const s=x<0?-1:1; x=Math.abs(x);
  const t=1/(1+0.3275911*x);
  const y=1-(((((1.061405429*t-1.453152027)*t)+1.421413741)*t-0.284496736)*t+0.254829592)*t*Math.exp(-x*x);
  return s*y;
}
// 标准正态 CDF: z→(0,1); 均值处斜率最大, 聚集数值区分度更高
function normCdf(z){ return 0.5*(1+erf(z/Math.SQRT2)); }
// 带单位的格式化: 根据 UNITS[metric] 追加 × / % / 单位
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
// 笛卡尔积: dims=[dim,...] -> [[v1,v2,...], ...] (按 DIMV 顺序; 空 dims -> [[]])
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
// 连续分组跨度: keys(数组的数组), level -> 每个"组起始行"的跨度(非起始为0)
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
     <h3>${cfg.title ? cfg.title.replace(/</g,"&lt;") : "面板 #"+id}</h3>
     <button class="close" title="删除">×</button>
   </div>
   <div class="ctl">
     <label>取值指标<select data-k="metric">${optionsHTML(cfg.metric)}</select></label>
     <label>聚合<select data-k="how">
       ${["median","mean","min","max","count"].map(o=>'<option'+(o===cfg.how?" selected":"")+'>'+o+'</option>').join("")}
     </select></label>
     <label>高亮<select data-k="highlight">
       ${[["max","最大值"],["min","最小值"]].map(o=>'<option value="'+o[0]+'"'+(o[0]===(cfg.highlight||"max")?" selected":"")+'>'+o[1]+'</option>').join("")}
     </select></label>
     <label>着色<select data-k="scale">
       ${[["percentile","百分位"],["normal","正态"],["linear","均匀"]].map(o=>'<option value="'+o[0]+'"'+(o[0]===(cfg.scale||"percentile")?" selected":"")+'>'+o[1]+'</option>').join("")}
     </select></label>
   </div>
   <div class="dims">
     <div class="dimrow" data-axis="rows"></div>
     <div class="dimrow" data-axis="cols"></div>
   </div>
   <div class="flt"></div>
   <div class="body"></div>`;
  document.getElementById("panels").appendChild(el);

  // 只保留数据里实际存在的维度：结论面板 spec 可能引用被剔除的空维度(如缺失的 model_name)
  const DIMSET = new Set(DIMS);
  const state = {metric:cfg.metric, how:cfg.how};
  state.rows = (cfg.rows||[]).filter(d=>DIMSET.has(d));
  state.cols = (cfg.cols||[]).filter(d=>DIMSET.has(d));
  state.filters = {};
  for(const k in (cfg.filters||{})) if(DIMSET.has(k)) state.filters[k]=cfg.filters[k];
  state.highlight = cfg.highlight || "max";   // 默认最大值高亮
  state.scale = cfg.scale || "percentile";    // 着色映射: 百分位/正态/均匀, 默认百分位
  const usedDims = ()=> new Set(state.rows.concat(state.cols));

  // 可增删的多级表头维度选择器
  function renderDims(){
    ["rows","cols"].forEach(axis=>{
      const wrap = el.querySelector('.dimrow[data-axis="'+axis+'"]');
      const label = axis==="rows" ? "纵向表头(行)" : "横向表头(列)";
      const chips = state[axis].map((d,i)=>
        '<span class="chip">'+d+'<b data-rm="'+axis+'|'+i+'" title="移除">×</b></span>').join("");
      const avail = DIMS.filter(d=>!usedDims().has(d));
      const add = avail.length
        ? '<select class="addsel" data-add="'+axis+'"><option value="">+ 维度</option>'
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

  // 过滤器(仅对未用作行/列的维度)
  function renderFilters(){
    const wrap = el.querySelector(".flt");
    const used = usedDims();
    const free = DIMS.filter(d=>!used.has(d));
    for(const k in state.filters) if(!free.includes(k)) delete state.filters[k];
    wrap.innerHTML = free.length ? free.map(d=>{
      const cur = state.filters[d]||"__all__";
      const opts = ['<option value="__all__">全部(聚合)</option>']
        .concat(DIMV[d].map(v=>'<option value="'+v+'"'+(v===cur?" selected":"")+'>'+v+'</option>')).join("");
      return '<label>'+d+' 过滤<select data-flt="'+d+'">'+opts+'</select></label>';
    }).join("") : '<span class="meta">所有维度已用作行/列表头</span>';
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
    // 累积到 bag[rowKey][colKey] = [values...]
    const bag={};
    for(const r of data){
      const rkey = rowDims.map(d=>r[d]).join(SEP);
      const ckey = colDims.map(d=>r[d]).join(SEP);
      (bag[rkey]=bag[rkey]||{}); (bag[rkey][ckey]=bag[rkey][ckey]||[]).push(r[metric]);
    }
    const K = a=>a.join(SEP);
    const val = (rw,ck)=>{ const b=bag[K(rw)]; return b && b[K(ck)]!=null ? agg(b[K(ck)],how) : null; };
    // 全组合(按 DIMV 顺序), 再剔除整行/整列全空
    let rowKeys = product(rowDims), colKeys = product(colDims);
    colKeys = colKeys.filter(ck=>rowKeys.some(rw=>val(rw,ck)!=null));
    rowKeys = rowKeys.filter(rw=>colKeys.some(ck=>val(rw,ck)!=null));
    let mn=Infinity,mx=-Infinity;
    for(const rw of rowKeys)for(const ck of colKeys){const a=val(rw,ck); if(a!=null){mn=Math.min(mn,a);mx=Math.max(mx,a);}}

    const activeF = Object.entries(filters).map(([k,v])=>k+"="+v).join(" , ");
    const title = state.title ? '<div class="ptitle">'+state.title+'</div>' : '';
    const meta = '<div class="meta">值 = <b>'+metric+'</b> · '+how+
                 (activeF?(' · 过滤: '+activeF):'')+'</div>';
    el.querySelector(".body").innerHTML =
      title + meta + tableHTML(rowDims,colDims,rowKeys,colKeys,val,mn,mx,metric,state.highlight,state.scale)
           + chartHTML(rowDims,colDims,rowKeys,colKeys,val,metric);
  }

  // ---- 多级表头透视表 ----
  function tableHTML(rowDims,colDims,rowKeys,colKeys,val,mn,mx,metric,highlight,scale){
    const best = highlight==="max" ? mx : highlight==="min" ? mn : null;
    if(!rowKeys.length || !colKeys.length) return '<div class="meta">当前筛选下无数据</div>';
    const rLev = Math.max(rowDims.length,1);
    const cornerLbl = (rowDims.join(" / ")||'行')
                    + ' \\ ' + (colDims.join(" / ")||'列');
    // 收集可见单元格数值, 供三种着色映射使用
    const vals=[];
    for(const rw of rowKeys)for(const ck of colKeys){const a=val(rw,ck); if(a!=null) vals.push(a);}
    const N=vals.length;
    const mean=N?vals.reduce((s,x)=>s+x,0)/N:0;
    const sd=N?Math.sqrt(vals.reduce((s,x)=>s+(x-mean)*(x-mean),0)/N):0;
    const sorted=[...vals].sort((x,y)=>x-y);
    const PBANDS=10;   // 百分位分段数(每段一档颜色)
    // 二分求 sorted 中 <a 与 <=a 的个数, 取平均秩得到百分位, 再离散到 PBANDS 段
    function bisect(arr, x, incl){ let lo=0,hi=arr.length; while(lo<hi){const m=(lo+hi)>>1; if(incl?arr[m]<=x:arr[m]<x) lo=m+1; else hi=m;} return lo; }
    function pctBand(a){
      if(N<=1) return 0.5;
      const p=((bisect(sorted,a,false)+bisect(sorted,a,true))/2 - 0.5)/(N-1);  // 平均秩→(0,1)
      const b=Math.min(PBANDS-1, Math.max(0, Math.floor(p*PBANDS)));            // 分段
      return b/(PBANDS-1);
    }
    // 按当前映射把数值→[0,1] 强度
    function heat(a){
      if(scale==="linear")     return mx>mn ? (a-mn)/(mx-mn) : 0.5;
      if(scale==="normal")     return sd>0 ? normCdf((a-mean)/sd) : 0.5;
      return pctBand(a);       // percentile(默认)
    }
    let t='<div class="tablewrap"><table>';
    // 列头(嵌套)
    if(colDims.length===0){
      t+='<tr><th class="corner" colspan="'+rLev+'">'+cornerLbl+'</th><th>值</th></tr>';
    } else {
      for(let L=0;L<colDims.length;L++){
        t+='<tr>';
        if(L===0) t+='<th class="corner" colspan="'+rLev+'" rowspan="'+colDims.length+'">'+cornerLbl+'</th>';
        const sp=spans(colKeys,L);
        colKeys.forEach((ck,i)=>{ if(sp[i]) t+='<th colspan="'+sp[i]+'">'+ck[L]+'</th>'; });
        t+='</tr>';
      }
    }
    // 行头 rowspan 预计算
    const rowSpans=[];
    for(let L=0;L<rowDims.length;L++) rowSpans.push(spans(rowKeys,L));
    rowKeys.forEach((rw,ri)=>{
      t+='<tr>';
      if(rowDims.length===0) t+='<td class="rowh">值</td>';
      else for(let L=0;L<rowDims.length;L++) if(rowSpans[L][ri]) t+='<td class="rowh" rowspan="'+rowSpans[L][ri]+'">'+rw[L]+'</td>';
      colKeys.forEach(ck=>{
        const a=val(rw,ck);
        if(a==null){ t+='<td class="na">–</td>'; return; }
        // 当前着色映射(百分位/正态/均匀) + 高亮方向: 最小值高亮时反转, 越小越深
        const norm = heat(a);
        const f = highlight==="min" ? 1-norm : norm;
        const isBest = best!=null && a===best;
        t+='<td'+(isBest?' class="best"':'')+' style="background:rgba(77,163,255,'+(0.08+0.5*f).toFixed(3)+')">'+fmtU(a,metric)+'</td>';
      });
      t+='</tr>';
    });
    return t+'</table></div>';
  }

  // ---- 多级分组柱状图: 组=行叶子, 系列=列叶子, 行的上层维度加括号 ----
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
           '" fill="'+COLORS[si%COLORS.length]+'"><title>'+(rw.join("/")||'值')+' | '+(ck.join("/")||'值')+'\n'+fmtU(a,metric)+'</title></rect>';
      });
      const leaf=(rw[rw.length-1]||"值")+"";
      s+='<text x="'+(gx+nS*barW/2).toFixed(1)+'" y="'+(H-padB+13)+'" fill="#e6ebf5" font-size="9" text-anchor="middle">'+
         (leaf.length>8?leaf.slice(0,7)+'…':leaf)+'</text>';
    });
    if(twoTier){   // 行上层维度的括号标注
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
        '<span><i style="background:'+COLORS[i%COLORS.length]+'"></i>'+(ck.join(" · ")||'值')+'</span>').join("")+'</div>';
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

// ---- 结论/洞察栏: 每条结论可点击, 展开其对应的一个或多个面板 ----
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
  let h='<h2>结论 / 洞察 · 点击任一条展开对应面板</h2>';
  for(const cat of order){
    h+='<div class="cat"><div class="cath">'+cat+'</div><div class="cards">';
    byCat[cat].forEach((c,i)=>{
      const has=c.panels && c.panels.length;
      h+='<div class="insight '+c.severity+(has?'':' noclick')+'" data-cat="'+cat+'" data-i="'+i+'">'
       + '<div class="t">'+c.title+(has?'<span class="n">面板×'+c.panels.length+'</span>':'')+'</div>'
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
// 默认展开第一条带面板的结论(通常是 L1 主 KPI 最优), 让页面不空
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
      res = analyze.enrich(csv_path, profile)   # 分析引擎产出数据 + 派生指标 + 结论
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
    print("已生成:", out_path, "(", len(html), "字节 )")
    print("profile=%s · %d config · %d 指标 · %d 条结论" % (
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
        raise SystemExit("找不到 windowed_metric_medians.csv 文件: " + str(csv_file))
    if args.middle_json and not json_file.exists():
        raise SystemExit("找不到 pivot_report.json 文件: " + str(json_file))

    out = Path(input_path) / "pivot_report.html"
    prof = None
    if not os.path.exists(input_path):
        raise SystemExit("找不到输入文件: " + input_path)
    if(args.middle_json):
        build(str(csv_file), str(out), prof, str(json_file))
    else:
        build(str(csv_file), str(out), prof)
