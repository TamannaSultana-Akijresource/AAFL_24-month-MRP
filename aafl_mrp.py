"""
aafl_mrp.py
===========
AAFL 24-Month Material Requirement Plan — data layer + HTML renderer.

Data sources
  * STATIC  : aafl_static_data.json  (SKU forecast, BOM-derived RM plan, budget
              rates, cash calendar, geo-risk flags) — the supplied dataset.
  * LIVE    : DWH via the enterprise-api-gateway (aafl_gateway.Gateway) —
              stock, coverage, landed price history, actual FG sales.
  * MARKET  : yfinance futures + Reuters/FAO RSS (optional).

BU = 232 (Akij Agro Feed Ltd.).
"""
import datetime
import json
import os

import numpy as np

from aafl_gateway import Gateway

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_PATH = os.path.join(HERE, "aafl_static_data.json")
BU = 232

# ─────────────────────────────────────────────────────────────────────────────
# static dataset
# ─────────────────────────────────────────────────────────────────────────────
def load_static():
    with open(STATIC_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def proj_price(series):
    """Linear trend on last <=9 monthly rates -> (last, proj_6mo, proj_15mo)."""
    if not series or len(series) < 3:
        return None, None, None
    v = np.array([s[1] for s in series[-9:]], dtype=float)
    slope = np.polyfit(np.arange(len(v)), v, 1)[0]
    last = float(v[-1])
    p6 = float(np.clip(last + slope * 6, last * 0.6, last * 1.4))
    p15 = float(np.clip(last + slope * 15, last * 0.6, last * 1.4))
    return last, p6, p15


def _clean(o):
    if isinstance(o, float):
        return None if (np.isnan(o) or np.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.floating):
        v = float(o)
        return None if (np.isnan(v) or np.isinf(v)) else v
    if isinstance(o, np.integer):
        return int(o)
    return o


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _in_list(codes):
    return ",".join("'" + str(c).replace("'", "") + "'" for c in codes)


# ─────────────────────────────────────────────────────────────────────────────
# live DWH pulls (batched, read-only)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_live_rm(rm_codes, sku_codes, log=print):
    """Pull ABC, stock, consumption, landed price history and actual sales."""
    gw = Gateway()
    gw.connect()
    out = {"abc": {}, "stock": {}, "cons": {}, "price": {}, "sales": {},
           "ok": True, "pulled_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}

    def safe(label, fn):
        try:
            log("  - " + label)
            fn()
        except Exception as e:  # noqa: BLE001
            out["ok"] = False
            log("    ! " + label + " failed: " + str(e)[:160])

    def _abc():
        for r in gw.rows(
            "SELECT i.strItemCode, i.strItemName, SUM(ABS(r.numTransactionQuantity)) QTY, "
            "SUM(ABS(r.monTransactionValue)) VAL "
            "FROM wms.tblInventoryTransactionRowArc r "
            "JOIN wms.tblInventoryTransactionHeaderArc h ON r.intInventoryTransactionId=h.intInventoryTransactionId "
            "JOIN itm.tblItemArc i ON i.intItemId=r.intItemId "
            f"WHERE h.intBusinessUnitId={BU} AND h.isActive=1 AND h.TransactionGroupName='Issue Inventory' "
            "AND h.dteTransactionDate>=DATEADD(MONTH,-12,GETDATE()) "
            "AND i.strItemTypeName='Raw Materials' AND i.strItemName NOT LIKE '%(Old)%' "
            "GROUP BY i.strItemCode, i.strItemName ORDER BY VAL DESC",
            limit=500,
        ):
            out["abc"][r["strItemCode"]] = {"qty": r["QTY"], "val": r["VAL"]}
        tot = sum(v["val"] for v in out["abc"].values()) or 1.0
        c = 0.0
        for code, v in out["abc"].items():
            c += v["val"] / tot
            v["abc"] = "A" if c <= 0.70 else ("B" if c <= 0.90 else "C")

    def _stock():
        for grp in _chunks(rm_codes, 40):
            for r in gw.rows(
                "SELECT i.strItemCode, SUM(r.numTransactionQuantity) StockQty, "
                "SUM(r.monTransactionValue) StockVal "
                "FROM wms.tblInventoryTransactionRowArc r "
                "JOIN wms.tblInventoryTransactionHeaderArc h ON r.intInventoryTransactionId=h.intInventoryTransactionId "
                "JOIN itm.tblItemArc i ON i.intItemId=r.intItemId "
                f"WHERE h.intBusinessUnitId={BU} AND h.isActive=1 "
                "AND h.dteTransactionDate<=CAST(GETDATE() AS DATE) "
                f"AND i.strItemCode IN ({_in_list(grp)}) GROUP BY i.strItemCode",
                limit=500,
            ):
                out["stock"][r["strItemCode"]] = {"qty": r["StockQty"] or 0.0, "val": r["StockVal"] or 0.0}

    def _cons():
        for grp in _chunks(rm_codes, 40):
            for r in gw.rows(
                "SELECT i.strItemCode, SUM(ABS(r.numTransactionQuantity))/3.0 AvgMo "
                "FROM wms.tblInventoryTransactionRowArc r "
                "JOIN wms.tblInventoryTransactionHeaderArc h ON r.intInventoryTransactionId=h.intInventoryTransactionId "
                "JOIN itm.tblItemArc i ON i.intItemId=r.intItemId "
                f"WHERE h.intBusinessUnitId={BU} AND h.isActive=1 AND h.TransactionGroupName='Issue Inventory' "
                "AND h.dteTransactionDate>=DATEADD(MONTH,-3,GETDATE()) "
                f"AND i.strItemCode IN ({_in_list(grp)}) GROUP BY i.strItemCode",
                limit=500,
            ):
                out["cons"][r["strItemCode"]] = r["AvgMo"] or 0.0

    def _price():
        for grp in _chunks(rm_codes, 18):
            for r in gw.rows(
                "SELECT i.strItemCode, FORMAT(h.dteTransactionDate,'yyyy-MM') YM, "
                "SUM(ABS(r.monTransactionValue))/NULLIF(SUM(ABS(r.numTransactionQuantity)),0)*1000 Rate, "
                "SUM(ABS(r.numTransactionQuantity))/1000.0 QtyMT "
                "FROM wms.tblInventoryTransactionRowArc r "
                "JOIN wms.tblInventoryTransactionHeaderArc h ON r.intInventoryTransactionId=h.intInventoryTransactionId "
                "JOIN itm.tblItemArc i ON i.intItemId=r.intItemId "
                f"WHERE h.intBusinessUnitId={BU} AND h.isActive=1 AND h.TransactionGroupName='Receive Inventory' "
                "AND h.dteTransactionDate>=DATEADD(MONTH,-18,GETDATE()) "
                f"AND i.strItemCode IN ({_in_list(grp)}) "
                "GROUP BY i.strItemCode, FORMAT(h.dteTransactionDate,'yyyy-MM') "
                "HAVING SUM(ABS(r.numTransactionQuantity))>100 "
                "ORDER BY i.strItemCode, YM",
                limit=500,
            ):
                out["price"].setdefault(r["strItemCode"], []).append([r["YM"], r["Rate"], r["QtyMT"]])

    def _sales():
        sku = list(dict.fromkeys(str(c).split("_")[0] for c in sku_codes))
        for grp in _chunks(sku, 25):
            for r in gw.rows(
                "SELECT i.strItemCode, FORMAT(h.dteTransactionDate,'yyyy-MM') YM, "
                "SUM(ABS(r.numTransactionQuantity))/1000.0 SalesMT "
                "FROM wms.tblInventoryTransactionRowArc r "
                "JOIN wms.tblInventoryTransactionHeaderArc h ON r.intInventoryTransactionId=h.intInventoryTransactionId "
                "JOIN itm.tblItemArc i ON i.intItemId=r.intItemId "
                f"WHERE h.intBusinessUnitId={BU} AND h.isActive=1 "
                "AND h.TransactionGroupName IN ('Issue Inventory','Delivery Order') "
                "AND h.dteTransactionDate>=DATEADD(MONTH,-14,GETDATE()) "
                f"AND i.strItemCode IN ({_in_list(grp)}) AND i.strItemTypeName='Finished Products' "
                "GROUP BY i.strItemCode, FORMAT(h.dteTransactionDate,'yyyy-MM') "
                "ORDER BY i.strItemCode, YM",
                limit=500,
            ):
                out["sales"].setdefault(r["strItemCode"], {})[r["YM"]] = r["SalesMT"]

    safe("RM issues (ABC, 12mo)", _abc)
    safe("stock balance", _stock)
    safe("3-month consumption", _cons)
    safe("landed price history (18mo)", _price)
    safe("actual FG sales (14mo)", _sales)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# market intel (optional, network)
# ─────────────────────────────────────────────────────────────────────────────
def live_market_intel():
    out = {"futures": {}, "headlines": [], "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    try:
        import yfinance as yf
        for t, label in [("ZC=F", "Corn"), ("ZM=F", "Soy Meal"), ("ZW=F", "Wheat"), ("ZS=F", "Soybean")]:
            try:
                h = yf.Ticker(t).history(period="3mo", interval="1mo")
                if not h.empty:
                    last, first = float(h.Close.iloc[-1]), float(h.Close.iloc[0])
                    out["futures"][label] = {"price": round(last, 1),
                                             "chg3mo": round((last / first - 1) * 100, 1),
                                             "unit": "USD/bu"}
            except Exception:
                pass
    except Exception:
        pass
    try:
        import feedparser
        for url in ["https://feeds.reuters.com/reuters/commoditiesNews",
                    "https://www.fao.org/news/rss-en/"]:
            try:
                for e in feedparser.parse(url).entries[:10]:
                    t = e.get("title", "")
                    if any(k in t.lower() for k in ["corn", "maize", "soybean", "wheat", "feed",
                                                    "grain", "ukraine", "russia", "black sea",
                                                    "methionine", "fertilizer"]):
                        out["headlines"].append(t)
            except Exception:
                pass
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# merge static + live  ->  payload
# ─────────────────────────────────────────────────────────────────────────────
def build_payload(include_market=True, log=print):
    st = load_static()
    months = st["months"]
    rm = st["rm"]
    sku_map = st["sku"]
    ym_map = st["ym_map"]

    rm_codes = [str(r["code"]) for r in rm]
    sku_codes = list(sku_map.keys())

    log("Pulling live DWH data from gateway…")
    live = fetch_live_rm(rm_codes, sku_codes, log=log)

    # -- RM rows -------------------------------------------------------------
    by_code = {str(r["code"]): r for r in rm}
    rm_rows = []
    for r in rm:
        code = str(r["code"])
        stock_mt = r.get("stock_mt")
        cover = r.get("cover")
        abc = r.get("abc", "")
        # live stock / cover
        if code in live["stock"]:
            stock_mt = live["stock"][code]["qty"] / 1000.0
        if code in live["cons"] and live["cons"][code]:
            avgmo = live["cons"][code] / 1000.0
            cover = (stock_mt / avgmo) if (stock_mt is not None and avgmo) else cover
        # live price history (+ qty)
        ps = None
        if live["price"].get(code):
            ps = [[x[0], x[1], x[2]] for x in live["price"][code]]
        latest = r.get("latest_rate")
        trend = r.get("price_trend", "")
        p6, p15 = r.get("proj_rate_6m"), r.get("proj_rate_15m")
        if ps and len(ps) >= 2:
            latest = ps[-1][1]
            prev = ps[0][1]
            trend = "RISING" if latest > prev else ("FALLING" if latest < prev else "FLAT")
            _, p6, p15 = proj_price(ps)
        if not ps:
            ps = [[ym, rate, None] for ym, rate in (r.get("price_series") or [])]
        if code in live["abc"] and live["abc"][code].get("abc"):
            abc = live["abc"][code]["abc"]
        rm_rows.append({
            "code": code,
            "name": r["name"], "abc": abc, "source": r.get("source", ""),
            "risk": r.get("risk", ""), "stock_mt": stock_mt, "cover": cover,
            "req18": r.get("req18", 0), "net_order": r.get("net_order", 0),
            "budget_rate": r.get("budget_rate"), "latest_rate": latest,
            "budget_var_pct": r.get("budget_var_pct"), "price_trend": trend,
            "decision": r.get("decision", ""), "monthly": r.get("monthly", [0] * 18),
            "price_series": ps, "proj_rate_6m": p6, "proj_rate_15m": p15,
            "signal": r.get("signal", ""),
        })

    # -- SKU rows ------------------------------------------------------------
    sku_rows = []
    for code, s in sku_map.items():
        if sum(v or 0 for v in s["vals"]) <= 0:
            continue
        base = code.split("_")[0]
        act = {}
        if live["sales"].get(base):
            for ymm, mt in live["sales"][base].items():
                lbl = ym_map.get(ymm)
                if lbl:
                    act[lbl] = mt
        sku_rows.append({"code": code, "name": s["name"], "model": s["model"],
                         "months": months, "vals": s["vals"], "actual": act})

    payload = {
        "as_of": datetime.date.today().isoformat(),
        "built_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "live": live["ok"],
        "pulled_at": live["pulled_at"],
        "months": months,
        "rm": rm_rows,
        "sku": sku_rows,
        "cash": st["cash"],
        "geo": {k: list(v) for k, v in st["geo"].items()},
    }
    if include_market:
        mi = live_market_intel()
        payload["mi_futures"] = mi["futures"]
        payload["mi_headlines"] = mi["headlines"]
        payload["mi_ts"] = mi["ts"]
    else:
        payload["mi_futures"] = {}
        payload["mi_headlines"] = []
        payload["mi_ts"] = ""
    return _clean(payload)


# ─────────────────────────────────────────────────────────────────────────────
# HTML renderer
# ─────────────────────────────────────────────────────────────────────────────
_HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AAFL MRP Dashboard – __ASOF__</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.26.0/plotly.min.js"></script>
<style>
:root{--ink:#24333B;--amber:#C97B2E;--red:#B23A2F;--green:#2E7D5B;--blue:#2C5F8A;--slate:#5B6B73;--bg:#F7F7F5;--w:#fff}
*{box-sizing:border-box}body{font-family:Calibri,'Segoe UI',Arial,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:20px}
h1{font-family:Cambria,serif;font-size:26px;margin:0 0 4px}h2{font-family:Cambria,serif;font-size:18px;margin:24px 0 8px;border-left:4px solid var(--amber);padding-left:8px}
.sub{color:var(--slate);font-size:13px;margin-bottom:16px}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.dlbtn{background:var(--ink);color:#fff;border:none;border-radius:5px;padding:9px 16px;font-size:13px;cursor:pointer;font-weight:bold;white-space:nowrap}
.dlbtn:hover{background:var(--blue)}
.live-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold;color:#fff}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.kpi{background:var(--w);border-radius:6px;padding:12px 16px;flex:1;min-width:140px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.kpi .v{font-family:Cambria,serif;font-size:22px;font-weight:bold}.kpi .l{font-size:11px;color:var(--slate)}
.banner{padding:10px 14px;border-radius:4px;margin-bottom:10px;font-size:13px}
.red-b{background:#FBEAE8;color:var(--red)}.amber-b{background:#F3E1CB;color:#7A4A10}
table{width:100%;border-collapse:collapse;background:var(--w);border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,.08);font-size:12px;margin-bottom:16px}
th{background:var(--ink);color:#fff;padding:8px 6px;text-align:center;font-size:11px}
td{padding:6px;border-bottom:1px solid #eee;text-align:center}td.l{text-align:left;font-weight:bold}
tr:nth-child(even) td{background:#fafaf8}
.badge{padding:2px 8px;border-radius:3px;font-size:11px;font-weight:bold}
.b-blk{background:#DDE7F0;color:var(--blue)}.b-spt{background:#E3F1EA;color:var(--green)}.b-urg{background:#FBEAE8;color:var(--red)}
select{padding:7px 10px;border:1px solid #ccc;border-radius:4px;font-size:13px;width:100%;margin-bottom:12px}
.chart{background:var(--w);border-radius:6px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:8px;margin-bottom:14px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.note{font-size:11px;color:var(--slate);line-height:1.5;margin-top:8px}
.mi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:12px}
.mi-card{background:var(--w);border-radius:6px;padding:12px 14px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
</style></head><body>
<div class="topbar">
  <div>
    <h1>AAFL — 24-Month Material Requirement Plan</h1>
    <div class="sub">As of __ASOF__ | Built __BUILT__ | DWH-live: stock, coverage, price, actual sales | Static: SKU forecast, BOM, cash calendar
      &nbsp;<span class="live-badge" id="liveBadge" style="background:#5B6B73">…</span></div>
  </div>
  <button class="dlbtn" onclick="downloadHTML()">⬇ Download HTML</button>
</div>
<div id="root"></div>
<script>
const D=__PAYLOAD__;
const MONTHS=D.months;
const f=n=>n==null?'—':Math.round(n).toLocaleString();
const rc=r=>r==='HIGH'?'var(--red)':r==='MODERATE'?'var(--amber)':'var(--green)';
const dc=d=>d&&d.includes('URGENT')?'b-urg':d&&d.includes('BLANKET')?'b-blk':'b-spt';

function downloadHTML(){
  const clone=document.documentElement.cloneNode(true);
  const html='<!DOCTYPE html>\n'+clone.outerHTML;
  const blob=new Blob([html],{type:'text/html'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download='AAFL_MRP_Dashboard_'+D.as_of+'.html';
  document.body.appendChild(a);a.click();a.remove();
}

document.getElementById('liveBadge').textContent=D.live?'🟢 DWH LIVE':'🔴 STATIC';
document.getElementById('liveBadge').style.background=D.live?'var(--green)':'var(--red)';

const classA=D.rm.filter(r=>r.abc==='A');
const crit=classA.filter(r=>r.cover!=null&&r.cover<1);
const fgVol=D.sku.reduce((a,s)=>a+s.vals.reduce((x,v)=>x+(v||0),0),0);
document.getElementById('root').innerHTML=`
<div class="kpis">
  <div class="kpi"><div class="v">${D.rm.reduce((a,r)=>a+r.req18,0).toLocaleString(undefined,{maximumFractionDigits:0})} MT</div><div class="l">18-mo RM Required</div></div>
  <div class="kpi"><div class="v">${classA.length}</div><div class="l">Class A Materials</div></div>
  <div class="kpi" style="background:#FBEAE8"><div class="v" style="color:var(--red)">${crit.length}</div><div class="l">Class A URGENT &lt;1mo cover</div></div>
  <div class="kpi"><div class="v">${fgVol.toLocaleString(undefined,{maximumFractionDigits:0})} MT</div><div class="l">FG forecast (18mo)</div></div>
</div>
${crit.length?`<div class="banner red-b">⚠ Under 1mo cover: <b>${crit.map(r=>r.name).join(', ')}</b></div>`:''}
${D.rm.filter(r=>r.budget_var_pct>10).length?`<div class="banner red-b">⚠ Buying above budget: ${D.rm.filter(r=>r.budget_var_pct>10).map(r=>`<b>${r.name}</b> +${Math.round(r.budget_var_pct)}%`).join(' · ')}</div>`:''}

<h2>Class A RM — Coverage &amp; Decision</h2>
<table><thead><tr><th class="l">Material</th><th>Source</th><th>Risk</th><th>Stock(MT)</th><th>Cover(days)</th><th>Cover(mo)</th><th>18-Mo Req(MT)</th><th>Net Order(MT)</th><th>Budget Rate</th><th>Latest Rate</th><th>vs Budget</th><th>Trend</th><th>DECISION</th></tr></thead><tbody>
${classA.map(r=>`<tr>
  <td class="l">${r.name}</td><td>${r.source}</td>
  <td style="color:${rc(r.risk)};font-weight:bold">${r.risk}</td>
  <td>${f(r.stock_mt)}</td>
  <td style="color:${r.cover==null?'#666':r.cover*30<30?'var(--red)':r.cover*30<60?'var(--amber)':'var(--green)'};font-weight:bold">
    ${r.cover==null?'—':(r.cover*30).toFixed(0)}</td>
  <td>${r.cover==null?'—':r.cover.toFixed(1)}</td>
  <td>${f(r.req18)}</td><td><b>${f(r.net_order)}</b></td>
  <td>${f(r.budget_rate)}</td><td>${f(r.latest_rate)}</td>
  <td style="color:${r.budget_var_pct>10?'var(--red)':r.budget_var_pct<-5?'var(--green)':'inherit'}">${r.budget_var_pct==null?'—':(r.budget_var_pct>0?'+':'')+Math.round(r.budget_var_pct)+'%'}</td>
  <td style="color:${r.price_trend==='RISING'?'var(--red)':r.price_trend==='FALLING'?'var(--green)':'inherit'}">${r.price_trend}</td>
  <td><span class="badge ${dc(r.decision)}">${r.decision}</span></td>
</tr>`).join('')}
</tbody></table>

<h2>Item Detail</h2>
<select id="rmSel" onchange="renderItem()">${D.rm.map((r,i)=>`<option value="${i}">${r.name} [${r.abc}]</option>`).join('')}</select>
<select id="fgSel" onchange="renderFG()">${D.sku.map((s,i)=>`<option value="${i}">${s.name}</option>`).join('')}</select>
<div class="grid2">
  <div class="chart" id="g1"></div>
  <div class="chart" id="g2"></div>
</div>
<div id="flags" style="display:flex;gap:16px;margin-bottom:14px;font-size:13px;flex-wrap:wrap"></div>

<h2>All RM — Month-wise (MT)</h2>
<input id="qinp" placeholder="Search..." oninput="renderTable()" style="padding:7px 10px;border:1px solid #ccc;border-radius:4px;font-size:13px;margin-bottom:8px;width:300px">
<div style="overflow:auto;max-height:400px">
<table id="allRm"><thead><tr><th class="l">Material</th><th>ABC</th>${MONTHS.map(m=>`<th>${m}</th>`).join('')}<th>18-Mo</th><th>Cover(mo)</th><th>Decision</th></tr></thead><tbody></tbody></table>
</div>

<h2>Cash Calendar FY26-27 (Cr BDT)</h2>
<div class="chart" id="cashChart" style="height:200px"></div>
<div class="banner amber-b">FY26-27 company-wide fund shortfall: Cr −522.25 | Feb-27 (Cr 0.58) &amp; Jun-27 (Cr −2.09) cash-constrained</div>

<h2>Market Intelligence</h2>
<div class="mi-grid" id="miGrid"></div>
<div id="headlines"></div>

<div class="note"><b>Method:</b> SKU forecast: revised Inventory &amp; Production plan FY26-27+FY27-28. RM plan: DWH default BOM × SKU forecast. Price = Σ(value)/Σ(qty)×1000 monthly weighted average. Budget rate: parsed from budget master + fuzzy matched. Projection: linear trend ≤9 months, capped ±40%. Live data pulled from the enterprise-api-gateway (read-only).</div>
`;

function renderFG(){
  const i=+document.getElementById('fgSel').value;
  const s=D.sku[i];
  const cols=s.vals.map((_,j)=>j<12?'#C97B2E':'#5B6B73');
  const traces=[{x:MONTHS,y:s.vals,type:'bar',name:'Budget+Forecast',marker:{color:cols},opacity:0.8}];
  if(s.actual){
    const ax=MONTHS.filter(m=>s.actual[m]!=null);
    if(ax.length) traces.push({x:ax,y:ax.map(m=>s.actual[m]),mode:'lines+markers+text',
      text:ax.map(m=>s.actual[m].toFixed(0)),textposition:'top center',
      name:'Actual Sales (DWH)',line:{color:'#B23A2F',width:2.5},marker:{size:8}});
  }
  Plotly.newPlot('g1',traces,{title:s.name.substring(0,55),plot_bgcolor:'#F7F7F5',paper_bgcolor:'#F7F7F5',margin:{t:40,b:60},height:320,showlegend:true,legend:{orientation:'h',y:-0.4}},{responsive:true});
}

function renderItem(){
  const i=+document.getElementById('rmSel').value;
  const r=D.rm[i];
  const traces=[];
  if(r.price_series && r.price_series.length){
    traces.push({x:r.price_series.map(p=>p[0]),y:r.price_series.map(p=>p[1]),
      mode:'lines+markers',name:'Actual Landed Rate (DWH)',
      line:{color:'#24333B',width:2.5}, yaxis:'y1'});
    if(r.price_series.some(p=>p[2]!=null)){
      traces.push({x:r.price_series.map(p=>p[0]),y:r.price_series.map(p=>p[2]),
        type:'bar', name:'Purchase Qty (MT)', marker:{color:'#5B6B73',opacity:0.25},
        yaxis:'y2'});
    }
  }
  if(r.proj_rate_6m!=null && r.proj_rate_15m!=null && r.price_series.length){
    const lastPt = r.price_series[r.price_series.length-1];
    traces.push({x:[lastPt[0], '2027-06', '2027-12'],
      y:[lastPt[1], r.proj_rate_6m, r.proj_rate_15m],
      mode:'lines+markers', name:'Projected (linear trend)',
      line:{color:'#B23A2F', dash:'dash', width:2}, yaxis:'y1'});
  }
  if(r.budget_rate){
    traces.push({x:[r.price_series.length?r.price_series[0][0]:'2025-07','2027-12'],
      y:[r.budget_rate,r.budget_rate], mode:'lines',
      name:`Budget ${f(r.budget_rate)}`, line:{dash:'dot',color:'#C97B2E',width:2},
      yaxis:'y1'});
  }
  Plotly.newPlot('g2',traces,{
    title:`${r.name.substring(0,45)} | ${r.risk} | ${r.decision}`,
    plot_bgcolor:'#F7F7F5',paper_bgcolor:'#F7F7F5',margin:{t:50,b:60},height:320,
    legend:{orientation:'h',y:-0.4},
    yaxis:{title:'BDT / MT'},
    yaxis2:{title:'Purchase Qty (MT)', overlaying:'y', side:'right', showgrid:false}
  },{responsive:true});

  const cc=r.cover==null?'#666':r.cover<1?'#B23A2F':r.cover<2?'#C97B2E':'#2E7D5B';
  document.getElementById('flags').innerHTML=`
    <div><b>Risk:</b> <span style="color:${rc(r.risk)};font-weight:bold">${r.risk}</span></div>
    <div><b>Decision:</b> <span class="badge ${dc(r.decision)}">${r.decision}</span></div>
    <div><b>Cover:</b> <span style="color:${cc};font-weight:bold">${r.cover==null?'—':(r.cover*30).toFixed(0)+' days / '+r.cover.toFixed(1)+' mo'}</span></div>
    <div><b>vs Budget:</b> ${r.budget_var_pct==null?'—':(r.budget_var_pct>0?'<span style=color:var(--red)>+':'<span style=color:var(--green)>')+Math.round(r.budget_var_pct)+'%</span>'}</div>
    <div><b>Signal:</b> ${r.signal}</div>`;
}

function renderTable(){
  const q=document.getElementById('qinp').value.toLowerCase();
  const rows=D.rm.filter(r=>r.name.toLowerCase().includes(q));
  document.querySelector('#allRm tbody').innerHTML=rows.map(r=>`<tr>
    <td class="l">${r.name}</td><td><b>${r.abc}</b></td>
    ${r.monthly.map(v=>`<td>${Math.round(v)}</td>`).join('')}
    <td><b>${f(r.req18)}</b></td>
    <td>${r.cover==null?'—':r.cover.toFixed(1)}</td>
    <td><span class="badge ${dc(r.decision)}">${r.decision}</span></td>
  </tr>`).join('');
}

Plotly.newPlot('cashChart',
  [{x:Object.keys(D.cash),y:Object.values(D.cash),type:'bar',marker:{color:Object.values(D.cash).map(v=>v<2?'#B23A2F':'#2E7D5B')}}],
  {plot_bgcolor:'#F7F7F5',paper_bgcolor:'#F7F7F5',margin:{t:10,b:30},height:200,showlegend:false},{responsive:true});

const miHtml=Object.entries(D.mi_futures||{}).map(([l,d])=>{
  const c=d.chg3mo>5?'var(--red)':d.chg3mo<-5?'var(--green)':'var(--amber)';
  return `<div class="mi-card"><b>${l}</b><br><span style="font-size:20px;font-weight:bold;color:${c}">${d.price}</span> ${d.unit}<br><span style="color:${c}">${d.chg3mo>0?'+':''}${d.chg3mo}% (3mo)</span></div>`;
}).join('');
document.getElementById('miGrid').innerHTML=miHtml||'<i>No live futures data loaded</i>';
if((D.mi_headlines||[]).length) document.getElementById('headlines').innerHTML='<ul>'+D.mi_headlines.map(h=>`<li>${h}</li>`).join('')+'</ul>';

renderFG(); renderItem(); renderTable();
</script></body></html>"""


def build_html(payload):
    return (_HTML
            .replace("__ASOF__", payload["as_of"])
            .replace("__BUILT__", payload["built_at"])
            .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
