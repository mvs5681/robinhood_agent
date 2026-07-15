"""HTTP approval server + telemetry dashboard.

Proposal endpoints:
    GET  /proposals                — list pending proposals
    GET  /proposals/{id}           — full proposal detail
    POST /proposals/{id}/approve   — approve and execute
    POST /proposals/{id}/reject    — reject with optional {"note": "..."}
    GET  /health                   — liveness check

Dashboard (served at /):
    GET  /                         — tabbed HTML dashboard
    GET  /api/decisions            — decisions grouped by run_id
    GET  /api/decisions/{run_id}   — single decision detail
    GET  /api/market               — current GEX cache state (ticker grid)
    GET  /api/telemetry/summary    — counts by stage+result (last hour)
    GET  /api/telemetry/funnel     — pipeline funnel for today
    GET  /api/telemetry/recent     — last 50 telemetry events
    GET  /api/telemetry/pnl        — exit_signal pnl_pct time series
"""

from __future__ import annotations

import json
import logging
import time as _time
from typing import TYPE_CHECKING, Callable

from aiohttp import web

from trader.executor.schemas import ExecutionMode
from trader.telemetry.logger import TelemetryLogger

from .proposals import ProposalStore
from .telemetry_reader import TelemetryReader

if TYPE_CHECKING:
    from trader.executor.executor import Executor
    from .cache import GEXCache
    from .config import LiveConfig
    from .order_manager import OrderLifecycleManager
    from .position_store import PositionStore

logger = logging.getLogger(__name__)


def _json_response(data, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, default=str),
        content_type="application/json",
        status=status,
    )


# ---------------------------------------------------------------------------
# Dashboard CSS
# ---------------------------------------------------------------------------

_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #0d1117; --surface: #161b22; --surface2: #1c2128;
  --border: #30363d; --text: #c9d1d9; --muted: #8b949e;
  --green: #3fb950; --yellow: #d29922; --red: #f85149;
  --blue: #58a6ff; --purple: #bc8cff; --orange: #ffa657;
  --radius: 6px; --drawer-w: 520px;
}
body { font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:var(--bg); color:var(--text); font-size:13px; line-height:1.5;
       overflow-x:hidden; }

/* ── Header ── */
header { background:var(--surface); border-bottom:1px solid var(--border);
         padding:10px 20px; display:flex; align-items:center; gap:14px; position:sticky;
         top:0; z-index:100; }
header h1 { font-size:16px; font-weight:700; }
#badge-mode { font-size:11px; padding:2px 8px; border-radius:20px;
              background:rgba(88,166,255,.15); color:var(--blue); border:1px solid var(--blue); }
#refresh-timer { margin-left:auto; font-size:11px; color:var(--muted); }

/* ── Tabs ── */
nav.tabs { display:flex; border-bottom:1px solid var(--border);
           background:var(--surface); padding:0 20px; }
nav.tabs button { background:none; border:none; color:var(--muted); cursor:pointer;
                  padding:10px 16px; font-size:13px; border-bottom:2px solid transparent;
                  transition:.15s; }
nav.tabs button.active { color:var(--text); border-bottom-color:var(--blue); }
nav.tabs button:hover:not(.active) { color:var(--text); }

/* ── Tab panes ── */
.tab-pane { display:none; padding:20px; }
.tab-pane.active { display:block; }

/* ── Market grid ── */
#market-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:12px; }
.ticker-card { background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
               padding:14px; cursor:pointer; transition:border-color .15s; }
.ticker-card:hover { border-color:var(--blue); }
.ticker-card.regime-negative { border-left:3px solid var(--red); }
.ticker-card.regime-positive { border-left:3px solid var(--blue); }
.ticker-card.regime-mixed { border-left:3px solid var(--muted); }
.card-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }
.card-ticker { font-size:16px; font-weight:700; }
.badge { font-size:10px; padding:2px 7px; border-radius:12px; font-weight:600; text-transform:uppercase; }
.badge-neg { background:rgba(248,81,73,.15); color:var(--red); border:1px solid var(--red); }
.badge-pos { background:rgba(88,166,255,.15); color:var(--blue); border:1px solid var(--blue); }
.badge-mix { background:rgba(139,148,158,.15); color:var(--muted); border:1px solid var(--border); }
.card-direction { font-size:12px; color:var(--muted); }
.bar-row { display:flex; align-items:center; gap:8px; margin:4px 0; }
.bar-label { font-size:11px; color:var(--muted); width:72px; text-align:right; flex-shrink:0; }
.bar-wrap { flex:1; background:var(--bg); border-radius:3px; height:6px; overflow:hidden; }
.bar-fill { height:100%; border-radius:3px; background:var(--blue); }
.bar-val { font-size:11px; color:var(--text); width:30px; }
.card-footer { margin-top:10px; font-size:11px; color:var(--muted); display:flex; gap:12px; flex-wrap:wrap; }
.card-footer span b { color:var(--text); }

/* ── Decisions table ── */
.section-header { display:flex; align-items:center; gap:10px; margin-bottom:12px; }
.section-header h2 { font-size:14px; font-weight:600; }
table { width:100%; border-collapse:collapse; }
th { text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.06em;
     color:var(--muted); padding:6px 10px; border-bottom:1px solid var(--border); }
td { padding:8px 10px; border-bottom:1px solid rgba(48,54,61,.5); vertical-align:middle; }
tr.clickable:hover td { background:rgba(255,255,255,.03); cursor:pointer; }
tr:last-child td { border-bottom:none; }
.outcome-pill { display:inline-block; padding:2px 8px; border-radius:12px; font-size:11px; font-weight:600; }
.out-proposed  { background:rgba(88,166,255,.15); color:var(--blue); }
.out-executed  { background:rgba(63,185,80,.15); color:var(--green); }
.out-skipped   { background:rgba(210,153,34,.15); color:var(--yellow); }
.out-error     { background:rgba(248,81,73,.15); color:var(--red); }

/* ── Proposals ── */
.proposal-card { background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
                 padding:16px; margin-bottom:14px; }
.proposal-card.expired { opacity:.5; }
.prop-header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:12px; }
.prop-title { font-size:15px; font-weight:700; }
.prop-meta { font-size:11px; color:var(--muted); }
.prop-ttl { font-size:11px; color:var(--yellow); }
.prop-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:14px; }
.prop-section { background:var(--bg); border-radius:4px; padding:10px; }
.prop-section-title { font-size:10px; text-transform:uppercase; letter-spacing:.08em;
                      color:var(--muted); margin-bottom:8px; }
.kv-row { display:flex; justify-content:space-between; font-size:12px; margin:3px 0; }
.kv-row .k { color:var(--muted); }
.kv-row .v { color:var(--text); font-weight:500; }
.prop-actions { display:flex; gap:10px; }
.btn { padding:7px 20px; border-radius:5px; border:none; cursor:pointer; font-size:13px;
       font-weight:600; transition:opacity .15s; }
.btn:hover { opacity:.8; }
.btn:disabled { opacity:.35; cursor:not-allowed; }
.btn-approve { background:var(--green); color:#000; flex:1; }
.btn-reject  { background:rgba(248,81,73,.2); color:var(--red); border:1px solid var(--red); flex:1; }

/* ── Drawer ── */
#drawer { position:fixed; top:0; right:0; width:var(--drawer-w); height:100vh;
          background:var(--surface); border-left:1px solid var(--border);
          transform:translateX(100%); transition:transform .25s ease;
          overflow-y:auto; z-index:200; display:flex; flex-direction:column; }
#drawer.open { transform:translateX(0); }
#drawer-header { display:flex; align-items:center; gap:10px; padding:14px 16px;
                 border-bottom:1px solid var(--border); position:sticky; top:0;
                 background:var(--surface); z-index:1; }
#drawer-title { font-size:15px; font-weight:700; flex:1; }
#drawer-close { background:none; border:none; color:var(--muted); cursor:pointer;
                font-size:18px; padding:4px 8px; }
#drawer-close:hover { color:var(--text); }
#drawer-body { padding:16px; flex:1; }
.d-section { margin-bottom:16px; }
.d-section-title { font-size:11px; text-transform:uppercase; letter-spacing:.08em;
                   color:var(--muted); margin-bottom:10px; padding-bottom:6px;
                   border-bottom:1px solid var(--border); display:flex; align-items:center; gap:6px; }
.d-section-title .status-dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; }
.dot-ok { background:var(--green); }
.dot-skip { background:var(--yellow); }
.dot-err { background:var(--red); }
.d-grid { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
.d-kv { display:flex; flex-direction:column; }
.d-kv .k { font-size:10px; color:var(--muted); }
.d-kv .v { font-size:13px; color:var(--text); font-weight:500; }
.score-bar-row { display:flex; align-items:center; gap:8px; margin:4px 0; }
.score-bar-label { font-size:11px; color:var(--muted); width:100px; }
.score-bar-wrap { flex:1; background:var(--bg); border-radius:3px; height:8px; overflow:hidden; }
.score-bar-fill { height:100%; border-radius:3px; }
.score-bar-val { font-size:11px; color:var(--text); width:36px; text-align:right; }
.skip-reason { font-size:12px; color:var(--yellow); margin-top:6px;
               background:rgba(210,153,34,.1); padding:6px 10px; border-radius:4px; }
.err-reason { font-size:12px; color:var(--red); margin-top:6px;
              background:rgba(248,81,73,.1); padding:6px 10px; border-radius:4px; }

/* ── Radar chart ── */
.radar-wrap { display:flex; justify-content:center; margin:4px 0 8px; }

/* ── GEX ruler ── */
.ruler-wrap { margin:8px 0; overflow:visible; }

/* ── Glossary ── */
#glossary-btn { position:fixed; bottom:20px; right:20px; background:var(--surface2);
                border:1px solid var(--border); border-radius:20px; padding:6px 14px;
                color:var(--muted); cursor:pointer; font-size:12px; z-index:150;
                transition:color .15s; }
#glossary-btn:hover { color:var(--text); }
#glossary-panel { position:fixed; bottom:60px; right:20px; width:340px; max-height:420px;
                  overflow-y:auto; background:var(--surface); border:1px solid var(--border);
                  border-radius:var(--radius); padding:14px; z-index:150; display:none; }
#glossary-panel.open { display:block; }
.gloss-title { font-size:12px; font-weight:700; color:var(--text); margin-bottom:10px; }
.gloss-term { margin-bottom:8px; }
.gloss-term dt { font-size:12px; font-weight:600; color:var(--blue); }
.gloss-term dd { font-size:11px; color:var(--muted); margin-left:8px; line-height:1.5; }

/* ── Log table ── */
.log-wrap { max-height:500px; overflow-y:auto; }
.result-ok   { color:var(--green); }
.result-skip { color:var(--yellow); }
.result-err  { color:var(--red); }
.empty-msg { color:var(--muted); font-style:italic; padding:20px 0; text-align:center; }
::-webkit-scrollbar { width:6px; }
::-webkit-scrollbar-track { background:var(--bg); }
::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
[data-tip] { cursor:help; border-bottom:1px dashed var(--muted); }

/* ── P&L tab ── */
.pnl-stats-row { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:20px; }
.stat-card { background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
             padding:14px 20px; min-width:120px; }
.stat-card .stat-label { font-size:11px; color:var(--muted); text-transform:uppercase;
                         letter-spacing:.06em; margin-bottom:4px; }
.stat-card .stat-value { font-size:22px; font-weight:700; }
.stat-card .stat-value.pos { color:var(--green); }
.stat-card .stat-value.neg { color:var(--red); }
.stat-card .stat-value.neu { color:var(--text); }
.pnl-chart-outer { background:var(--surface); border:1px solid var(--border);
                   border-radius:var(--radius); padding:16px; overflow-x:auto; }
.pnl-chart-title { font-size:12px; color:var(--muted); margin-bottom:12px; }
.pnl-table-wrap { max-height:360px; overflow-y:auto; margin-top:20px; }
"""

# ---------------------------------------------------------------------------
# Dashboard JS
# ---------------------------------------------------------------------------

_JS = r"""
const GLOSSARY = {
  "GEX": "Gamma Exposure — net dealer hedging obligation. Negative GEX means dealers amplify price moves (momentum/squeezes). Positive GEX means they suppress moves (pinning/mean-reversion).",
  "Regime": "GEX direction classification. Negative = amplified moves, follow the flow. Positive = suppressed moves, fade extremes. Mixed = no clean structure, skip.",
  "Flip Point": "Price where net GEX crosses zero. Below it dealers buy dips (stabilising), above it they sell rips (destabilising). Acts as a gravitational fulcrum for price.",
  "Call Wall": "Strike with the highest call gamma concentration. Dealers who sold these calls must sell underlying as price rises — acts as resistance / ceiling.",
  "Put Wall": "Strike with the highest put gamma concentration. Dealers buy underlying as price falls toward it — acts as a support floor.",
  "Target Level": "Primary exit price derived from the nearest GEX wall in the trade direction. Trade is closed when underlying reaches this level.",
  "Composite Score": "Weighted average of the 5 signal scores, 0–1. Threshold to proceed: 0.55. Higher = stronger multi-signal conviction.",
  "Market Tide": "Directional bias of net options premium flow market-wide. Measures whether call or put buyers are in control. Score 0 = bearish dominance, 1 = bullish dominance.",
  "Darkpool": "Institutional off-exchange block trade pressure. High score = large blocks printing at or above ask (stealth accumulation). Low = distribution.",
  "Flow Pressure": "Fraction of flow alerts aligned with the trade direction + net-premium momentum over 4 hours. Measures persistence of directional conviction.",
  "IV Cost": "Implied volatility cost score. Low IV percentile = cheap options = high score. Entering when IV is elevated means overpaying for gamma — lowers conviction.",
  "Technicals": "RSI + MACD timing alignment score. Measures how well price momentum and trend confirm the GEX directional setup.",
  "Delta": "Option price change per $1 move in the underlying. Target range: 0.30–0.55 (near-the-money). Deep ITM has high delta but poor leverage; far OTM has low probability.",
  "DTE": "Days To Expiration. Target: 7–45 days. Too short increases theta decay risk; too long ties up capital and reduces leverage.",
  "Spread %": "Bid-ask spread as a % of mid price. High spread = poor liquidity = expensive to enter/exit. Filtered above 15%.",
  "Alert Premium": "Total dollar value of the whale option print (size × premium × 100). Minimum $100K to trigger the flow gate.",
  "Sweep": "Order routed across multiple exchanges simultaneously to fill immediately — signals urgency and strong directional conviction.",
  "Floor Trade": "Large block executed at a specific exchange, often at or above ask — typically institutional positioning.",
  "Structure Confidence": "0–1 score for GEX setup quality. Derived from concentration of gamma at the top 3 strikes and strength of the regime ratio. Below 0.15 = mixed regime, skip.",
};

// ── Tabs ──────────────────────────────────────────────────────────────
function initTabs() {
  document.querySelectorAll('nav.tabs button').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('nav.tabs button').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    });
  });
}

// ── Drawer ────────────────────────────────────────────────────────────
const drawer = document.getElementById('drawer');
function openDrawer(runId) {
  drawer.classList.add('open');
  if (runId.startsWith('market:')) { openMarketDrawer(runId.slice(7)); return; }
  fetch('/api/decisions/' + encodeURIComponent(runId))
    .then(r => r.json())
    .then(d => renderDrawer(d))
    .catch(() => renderDrawerError('Failed to load decision detail'));
}
function closeDrawer() { drawer.classList.remove('open'); }
document.getElementById('drawer-close').addEventListener('click', closeDrawer);

function renderDrawerError(msg) {
  document.getElementById('drawer-title').textContent = 'Detail';
  document.getElementById('drawer-body').innerHTML = `<div class="empty-msg">${msg}</div>`;
}

async function openMarketDrawer(ticker) {
  let t = null;
  try {
    const data = await fetch('/api/market').then(r => r.json());
    t = data.find(x => x.ticker === ticker);
  } catch (e) { /* fall through to error message */ }
  if (!t) { renderDrawerError(ticker + ' not found in GEX cache'); return; }

  document.getElementById('drawer-title').textContent = ticker + ' — GEX Snapshot';
  const html = [];
  html.push(sectionHeader('GEX Setup', {result: 'ok'}));
  html.push(gexRuler(t));
  html.push('<div class="d-grid">');
  html.push(kv('Regime', tip(capitalize(t.regime), 'Regime')));
  html.push(kv('Setup Type', capitalize(t.setup_type || '')));
  html.push(kv('Direction', capitalize(t.direction || '')));
  html.push(kv('Confidence', tip(fmtPct(t.confidence), 'Structure Confidence')));
  html.push(kv('Spot', fmtPrice(t.spot_price)));
  html.push(kv('Flip Point', tip(fmtPrice(t.flip_point), 'Flip Point')));
  html.push(kv('Target', tip(fmtPrice(t.target_level), 'Target Level')));
  html.push(kv('Call Wall', tip(fmtPrice(t.call_wall), 'Call Wall')));
  html.push(kv('Put Wall', tip(fmtPrice(t.put_wall), 'Put Wall')));
  html.push(kv('Last Scan', t.last_scan ? new Date(t.last_scan).toLocaleTimeString() : '—'));
  html.push(kv('Freshness', t.stale
    ? '<span style="color:var(--red)">● Stale</span>'
    : '<span style="color:var(--green)">● Live</span>'));
  html.push('</div>');
  document.getElementById('drawer-body').innerHTML = html.join('');
}

function renderDrawer(d) {
  if (!d || d.error || !d.stages) { renderDrawerError((d && d.error) || 'Decision not found'); return; }
  const outcomeText = (d.outcome || 'unknown').replace('skipped_', 'skipped: ').replace('error_', 'error: ');
  document.getElementById('drawer-title').textContent =
    d.ticker + ' — ' + (d.stages.gex_setup?.direction || '') + ' — ' + outcomeText;

  const html = [];
  const gs = d.stages.gex_setup;
  const bs = d.stages.blend_score;
  const fc = d.stages.flow_check;
  const cs = d.stages.contract_select;
  const rc = d.stages.risk_check;
  const oa = d.stages.order_attempt;

  // GEX Setup
  html.push(sectionHeader('GEX Setup', gs));
  if (gs) {
    if (gs.result === 'ok') {
      html.push(gexRuler(gs));
      html.push('<div class="d-grid">');
      html.push(kv('Regime', tip(capitalize(gs.regime), 'Regime')));
      html.push(kv('Setup Type', capitalize(gs.setup_type || '')));
      html.push(kv('Direction', capitalize(gs.direction || '')));
      html.push(kv('Confidence', tip(fmtPct(gs.confidence), 'Structure Confidence')));
      html.push(kv('Flip Point', tip(fmtPrice(gs.flip_point), 'Flip Point')));
      html.push(kv('Target', tip(fmtPrice(gs.target_level), 'Target Level')));
      html.push(kv('Call Wall', tip(fmtPrice(gs.call_wall), 'Call Wall')));
      html.push(kv('Put Wall', tip(fmtPrice(gs.put_wall), 'Put Wall')));
      html.push('</div>');
    } else {
      html.push(skipBox(gs.reason || gs.result));
    }
  }

  // Blend Score
  html.push(sectionHeader('Blend Score', bs));
  if (bs) {
    if (bs.result === 'ok') {
      html.push(radarChart(bs.scores || {}));
      html.push('<div style="margin:8px 0">');
      const scoreColors = {market_tide:'#58a6ff',darkpool:'#bc8cff',flow_pressure:'#ffa657',iv_cost:'#3fb950',technicals:'#d29922'};
      const scoreLabels = {market_tide:'Market Tide',darkpool:'Darkpool',flow_pressure:'Flow Pressure',iv_cost:'IV Cost',technicals:'Technicals'};
      html.push(scoreLine('Composite', bs.composite, 'var(--blue)', 'Composite Score'));
      for (const [k, label] of Object.entries(scoreLabels)) {
        html.push(scoreLine(label, (bs.scores||{})[k], scoreColors[k], k.replace('_',' ').replace(/\b\w/g,c=>c.toUpperCase())));
      }
      html.push('</div>');
    } else {
      html.push(skipBox(bs.reason || bs.result));
    }
  }

  // Flow Check
  html.push(sectionHeader('Flow Trigger', fc));
  if (fc) {
    if (fc.confirmed) {
      html.push('<div class="d-grid">');
      html.push(kv('Premium', tip(fmtDollar(fc.alert_premium), 'Alert Premium')));
      html.push(kv('Direction', capitalize(fc.direction || '')));
      html.push('</div>');
    } else {
      html.push(skipBox(fc.reason || 'No matching whale print'));
    }
  }

  // Contract
  html.push(sectionHeader('Contract', cs));
  if (cs) {
    if (cs.selected) {
      html.push('<div class="d-grid">');
      html.push(kv('Strike', fmtPrice(cs.strike)));
      html.push(kv('Expiry', cs.expiry || ''));
      html.push(kv('Delta', tip(cs.delta?.toFixed(3) || '—', 'Delta')));
      html.push(kv('DTE', tip(cs.dte + ' days', 'DTE')));
      html.push(kv('Spread', tip(fmtPct(cs.spread_pct), 'Spread %')));
      html.push('</div>');
    } else {
      html.push(skipBox(cs.reason || cs.result));
    }
  }

  // Risk Gate
  html.push(sectionHeader('Risk Gate', rc));
  if (rc) {
    if (rc.approved) {
      html.push('<div style="color:var(--green);font-size:12px;padding:4px 0">✓ All risk checks passed</div>');
    } else {
      const reasons = rc.rejection_reasons || [];
      html.push(skipBox(reasons.length ? reasons.join('; ') : (rc.reason || 'Risk gate failed')));
    }
  }

  // Order
  html.push(sectionHeader('Order', oa));
  if (oa) {
    html.push('<div class="d-grid">');
    html.push(kv('Mode', oa.mode || '—'));
    html.push(kv('Placed', oa.placed ? '✓ Yes' : '✗ No'));
    if (oa.limit_price) html.push(kv('Limit Price', fmtPrice(oa.limit_price)));
    if (oa.order_id) html.push(kv('Order ID', oa.order_id));
    html.push('</div>');
    if (oa.review_summary) {
      html.push('<div style="font-size:11px;color:var(--muted);margin-top:8px;padding:8px;background:var(--bg);border-radius:4px;line-height:1.6">'
        + oa.review_summary + '</div>');
    }
  }

  document.getElementById('drawer-body').innerHTML = html.join('');
}

function sectionHeader(title, stage) {
  const result = stage?.result || 'missing';
  const dotClass = result === 'ok' ? 'dot-ok' : result === 'skipped' ? 'dot-skip' : result === 'error' ? 'dot-err' : 'dot-skip';
  return `<div class="d-section-title"><span class="status-dot ${dotClass}"></span>${title}</div>`;
}
function kv(k, v) { return `<div class="d-kv"><span class="k">${k}</span><span class="v">${v}</span></div>`; }
function skipBox(msg) { return `<div class="skip-reason">⊘ ${msg || 'skipped'}</div>`; }
function tip(val, term) { return `<span data-tip="${term}" title="${GLOSSARY[term]||''}">${val}</span>`; }
function capitalize(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''; }
function fmtPrice(v) { return v != null ? '$' + parseFloat(v).toFixed(2) : '—'; }
function fmtPct(v) { return v != null ? (parseFloat(v)*100).toFixed(1) + '%' : '—'; }
function fmtDollar(v) { return v != null ? '$' + Number(v).toLocaleString() : '—'; }

function scoreLine(label, val, color, tipKey) {
  const pct = Math.min(100, Math.max(0, (val || 0) * 100));
  return `<div class="score-bar-row">
    <span class="score-bar-label">${tip(label, tipKey)}</span>
    <div class="score-bar-wrap"><div class="score-bar-fill" style="width:${pct}%;background:${color}"></div></div>
    <span class="score-bar-val">${(val||0).toFixed(2)}</span>
  </div>`;
}

// ── Radar chart (SVG) ─────────────────────────────────────────────────
function radarChart(scores) {
  const cx=110, cy=110, r=80, n=5;
  const keys=['market_tide','darkpool','flow_pressure','iv_cost','technicals'];
  const labels=['Market Tide','Darkpool','Flow Pressure','IV Cost','Technicals'];
  const colors=['#58a6ff','#bc8cff','#ffa657','#3fb950','#d29922'];
  const ang = i => (Math.PI*2*i/n) - Math.PI/2;
  const pt  = (i,v) => [cx + Math.cos(ang(i))*r*v, cy + Math.sin(ang(i))*r*v];

  let s = `<svg viewBox="0 0 220 220" width="200" height="200">`;
  // rings
  for (const v of [.25,.5,.75,1]) {
    const pts = keys.map((_,i)=>pt(i,v).join(',')).join(' ');
    s += `<polygon points="${pts}" fill="none" stroke="var(--border)" stroke-width="${v===1?1.5:1}"/>`;
  }
  // axes
  for (let i=0;i<n;i++) { const [x,y]=pt(i,1); s+=`<line x1="${cx}" y1="${cy}" x2="${x}" y2="${y}" stroke="var(--border)" stroke-width="1"/>`; }
  // filled polygon
  const pts = keys.map((k,i)=>pt(i,Math.max(0,scores[k]||0)).join(',')).join(' ');
  s += `<polygon points="${pts}" fill="rgba(88,166,255,.2)" stroke="var(--blue)" stroke-width="2"/>`;
  // dots + labels
  for (let i=0;i<n;i++) {
    const v=Math.max(0,scores[keys[i]]||0);
    const [x,y]=pt(i,v);
    s += `<circle cx="${x}" cy="${y}" r="4" fill="${colors[i]}"/>`;
    const [lx,ly]=pt(i,1.28);
    const anchor=lx<cx-5?'end':lx>cx+5?'start':'middle';
    s += `<text x="${lx}" y="${ly}" text-anchor="${anchor}" font-size="9" fill="var(--muted)">${labels[i]}</text>`;
  }
  s += `</svg>`;
  return `<div class="radar-wrap">${s}</div>`;
}

// ── GEX Ruler (SVG) ───────────────────────────────────────────────────
function gexRuler(gs) {
  const markers = [
    {key:'put_wall',  label:'Put Wall',   color:'var(--red)',    above:false, val:gs.put_wall},
    {key:'flip_point',label:'Flip',       color:'var(--yellow)', above:true,  val:gs.flip_point},
    {key:'spot_price',label:'Spot',       color:'var(--text)',   above:false, val:gs.spot_price},
    {key:'call_wall', label:'Call Wall',  color:'var(--blue)',   above:false, val:gs.call_wall},
    {key:'target_level',label:'Target',  color:'var(--green)',  above:true,  val:gs.target_level},
  ].filter(m => m.val != null);

  if (markers.length < 2) return '';
  const vals = markers.map(m=>m.val);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.1 || 1;
  const minV = lo - pad, maxV = hi + pad;
  const W=440, H=72, lpad=24, rpad=24;
  const sc = v => lpad + (v-minV)/(maxV-minV)*(W-lpad-rpad);

  let s = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:440px">`;
  s += `<line x1="${lpad}" y1="${H/2}" x2="${W-rpad}" y2="${H/2}" stroke="var(--border)" stroke-width="2" stroke-linecap="round"/>`;

  for (const m of markers) {
    const x = sc(m.val);
    s += `<line x1="${x}" y1="${H/2-10}" x2="${x}" y2="${H/2+10}" stroke="${m.color}" stroke-width="2"/>`;
    const labY = m.above ? H/2-18 : H/2+20;
    const priceY = m.above ? H/2-7  : H/2+31;
    s += `<text x="${x}" y="${labY}" text-anchor="middle" font-size="9" fill="${m.color}" font-weight="600">${m.label}</text>`;
    s += `<text x="${x}" y="${priceY}" text-anchor="middle" font-size="9" fill="var(--muted)">$${parseFloat(m.val).toFixed(1)}</text>`;
  }
  s += `</svg>`;
  return `<div class="ruler-wrap">${s}</div>`;
}

// ── Market tab ────────────────────────────────────────────────────────
async function loadMarket() {
  const data = await fetch('/api/market').then(r=>r.json());
  const grid = document.getElementById('market-grid');
  if (!data.length) { grid.innerHTML = '<div class="empty-msg">No tickers in cache — scanner not yet run.</div>'; return; }
  grid.innerHTML = data.map(t => {
    const regimeClass = t.regime === 'negative' ? 'regime-negative' : t.regime === 'positive' ? 'regime-positive' : 'regime-mixed';
    const badgeClass = t.regime === 'negative' ? 'badge-neg' : t.regime === 'positive' ? 'badge-pos' : 'badge-mix';
    const badgeText = t.regime === 'negative' ? 'NEG' : t.regime === 'positive' ? 'POS' : 'MIX';
    const dir = t.direction === 'call' ? '↑ Call' : t.direction === 'put' ? '↓ Put' : '—';
    const conf = (t.confidence||0)*100;
    const score = (t.composite_score||0)*100;
    return `<div class="ticker-card ${regimeClass}" onclick="openDrawer('market:${t.ticker}')">
      <div class="card-header">
        <span class="card-ticker">${t.ticker}</span>
        <span class="badge ${badgeClass}">${badgeText}</span>
      </div>
      <div class="card-direction">${dir} · ${capitalize(t.setup_type||'')}</div>
      <div class="bar-row" style="margin-top:10px">
        <span class="bar-label">Confidence</span>
        <div class="bar-wrap"><div class="bar-fill" style="width:${conf}%"></div></div>
        <span class="bar-val">${conf.toFixed(0)}%</span>
      </div>
      <div class="card-footer">
        <span><b>${t.ticker}</b></span>
        ${t.flip_point ? `<span>Flip <b>$${parseFloat(t.flip_point).toFixed(1)}</b></span>` : ''}
        ${t.target_level ? `<span>Target <b>$${parseFloat(t.target_level).toFixed(1)}</b></span>` : ''}
        ${t.stale ? '<span style="color:var(--red)">● Stale</span>' : '<span style="color:var(--green)">● Live</span>'}
      </div>
    </div>`;
  }).join('');
}

// ── Decisions tab ─────────────────────────────────────────────────────
function formatOutcome(o) {
  if (o === 'executed')  return '<span class="outcome-pill out-executed">Executed</span>';
  if (o === 'proposed')  return '<span class="outcome-pill out-proposed">Proposed</span>';
  if (o?.startsWith('skipped')) return '<span class="outcome-pill out-skipped">' + o.replace('skipped_','Skipped: ') + '</span>';
  if (o?.startsWith('error'))   return '<span class="outcome-pill out-error">' + o.replace('error_','Error: ') + '</span>';
  return '<span class="outcome-pill out-skipped">' + (o||'unknown') + '</span>';
}

async function loadDecisions() {
  const data = await fetch('/api/decisions').then(r=>r.json());
  const wrap = document.getElementById('decisions-wrap');
  if (!data.length) { wrap.innerHTML = '<div class="empty-msg">No decisions with run_id yet. Run the watcher to populate.</div>'; return; }
  wrap.innerHTML = `<table>
    <thead><tr>
      <th>Time</th><th>Ticker</th><th>Direction</th><th>Setup</th><th>Composite</th><th>Outcome</th>
    </tr></thead>
    <tbody>
    ${data.map(d => {
      const gs = d.stages.gex_setup;
      const bs = d.stages.blend_score;
      const dir = gs?.direction ? capitalize(gs.direction) : '—';
      const setup = gs?.setup_type ? capitalize(gs.setup_type) : '—';
      const score = bs?.composite != null ? bs.composite.toFixed(2) : '—';
      const ts = d.timestamp ? new Date(d.timestamp).toLocaleTimeString() : '—';
      return `<tr class="clickable" onclick="openDrawer('${d.run_id}')">
        <td>${ts}</td>
        <td><b>${d.ticker}</b></td>
        <td>${dir}</td>
        <td>${setup}</td>
        <td>${score}</td>
        <td>${formatOutcome(d.outcome)}</td>
      </tr>`;
    }).join('')}
    </tbody></table>`;
}

// ── Proposals tab ─────────────────────────────────────────────────────
async function loadProposals() {
  const data = await fetch('/proposals').then(r=>r.json());
  const wrap = document.getElementById('proposals-wrap');
  if (!data.length) { wrap.innerHTML = '<div class="empty-msg">No pending proposals.</div>'; return; }
  wrap.innerHTML = data.map(p => {
    const expires = p.created_at ? Math.max(0, 30 - Math.round((Date.now() - new Date(p.created_at)) / 60000)) : '?';
    const sc = p.contract || {};
    const gs = p.gex_setup || {};
    const bs = p.blend_scores || {};
    const ft = p.flow_trigger || {};
    return `<div class="proposal-card" id="prop-${p.proposal_id}">
      <div class="prop-header">
        <div>
          <div class="prop-title">${p.ticker} ${sc.strike ? '$'+sc.strike : ''} ${sc.type?.toUpperCase()||''} · ${sc.expiry||''}</div>
          <div class="prop-meta">Composite: <b>${(bs.composite||0).toFixed(2)}</b> · ${capitalize(gs.regime||'')} · ${capitalize(gs.setup_type||'')}</div>
        </div>
        <div class="prop-ttl">Expires in ${expires}m</div>
      </div>
      <div class="prop-grid">
        <div class="prop-section">
          <div class="prop-section-title">GEX Setup</div>
          ${gexRuler(gs)}
          <div class="kv-row"><span class="k">Regime</span><span class="v">${tip(capitalize(gs.regime||''), 'Regime')}</span></div>
          <div class="kv-row"><span class="k">Confidence</span><span class="v">${tip(fmtPct(gs.confidence), 'Structure Confidence')}</span></div>
          <div class="kv-row"><span class="k">Flip Point</span><span class="v">${tip(fmtPrice(gs.flip_point), 'Flip Point')}</span></div>
          <div class="kv-row"><span class="k">Target</span><span class="v">${tip(fmtPrice(gs.target_level), 'Target Level')}</span></div>
        </div>
        <div class="prop-section">
          <div class="prop-section-title">Blend Score</div>
          ${radarChart(bs)}
          <div class="kv-row"><span class="k">Composite</span><span class="v">${tip((bs.composite||0).toFixed(2), 'Composite Score')}</span></div>
        </div>
        <div class="prop-section">
          <div class="prop-section-title">Flow Trigger</div>
          <div class="kv-row"><span class="k">Premium</span><span class="v">${tip(fmtDollar(ft.total_premium), 'Alert Premium')}</span></div>
          <div class="kv-row"><span class="k">Strike</span><span class="v">${fmtPrice(ft.strike)}</span></div>
          <div class="kv-row"><span class="k">Sweep</span><span class="v">${tip(ft.has_sweep?'Yes':'No','Sweep')}</span></div>
          <div class="kv-row"><span class="k">Floor</span><span class="v">${tip(ft.has_floor?'Yes':'No','Floor Trade')}</span></div>
        </div>
        <div class="prop-section">
          <div class="prop-section-title">Contract</div>
          <div class="kv-row"><span class="k">Limit</span><span class="v">${fmtPrice(sc.mid)}</span></div>
          <div class="kv-row"><span class="k">Delta</span><span class="v">${tip(sc.delta?.toFixed(3)||'—','Delta')}</span></div>
          <div class="kv-row"><span class="k">DTE</span><span class="v">${tip(p.dte+' days'||'—','DTE')}</span></div>
          <div class="kv-row"><span class="k">Spread</span><span class="v">${tip(fmtPct(sc.spread_pct),'Spread %')}</span></div>
          <div class="kv-row"><span class="k">OI</span><span class="v">${(sc.open_interest||0).toLocaleString()}</span></div>
          <div class="kv-row"><span class="k">IV</span><span class="v">${fmtPct(sc.iv)}</span></div>
        </div>
      </div>
      <div class="prop-actions">
        <button class="btn btn-approve" onclick="approveProposal('${p.proposal_id}', this)">✓ Approve</button>
        <button class="btn btn-reject"  onclick="rejectProposal('${p.proposal_id}', this)">✗ Reject</button>
      </div>
    </div>`;
  }).join('');
}

async function approveProposal(id, btn) {
  btn.disabled = true; btn.textContent = 'Placing...';
  const r = await fetch('/proposals/'+id+'/approve', {method:'POST'});
  const d = await r.json();
  const card = document.getElementById('prop-'+id);
  if (r.ok) { card.style.opacity='.5'; btn.textContent='✓ Approved'; }
  else { btn.disabled=false; btn.textContent='✓ Approve'; alert(d.error||'Error'); }
}
async function rejectProposal(id, btn) {
  const note = prompt('Rejection note (optional):') ?? '';
  btn.disabled = true;
  const r = await fetch('/proposals/'+id+'/reject', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({note})});
  if (r.ok) { document.getElementById('prop-'+id).style.opacity='.5'; btn.textContent='✗ Rejected'; }
  else { btn.disabled=false; }
}

// ── Log tab ───────────────────────────────────────────────────────────
async function loadLog() {
  const data = await fetch('/api/telemetry/recent').then(r=>r.json());
  const wrap = document.getElementById('log-wrap');
  if (!data.length) { wrap.innerHTML='<div class="empty-msg">No events yet.</div>'; return; }
  wrap.innerHTML = `<table>
    <thead><tr><th>Time</th><th>Stage</th><th>Ticker</th><th>Result</th><th>Reason / Detail</th></tr></thead>
    <tbody>${data.map(e => {
      const cls = e.result==='ok'?'ok':e.result==='skipped'?'skip':'err';
      const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString() : '';
      return `<tr><td>${ts}</td><td>${e.stage||''}</td><td>${e.ticker||''}</td>
        <td class="result-${cls}">${e.result||''}</td>
        <td style="color:var(--muted);font-size:11px">${e.reason||''}</td></tr>`;
    }).join('')}</tbody></table>`;
}

// ── Overview stats ────────────────────────────────────────────────────
async function loadOverview() {
  const d = await fetch('/api/telemetry/summary').then(r=>r.json());
  document.getElementById('ov-ok').textContent   = d.by_result?.ok    || 0;
  document.getElementById('ov-skip').textContent = d.by_result?.skipped|| 0;
  document.getElementById('ov-err').textContent  = d.by_result?.error  || 0;
  document.getElementById('ov-total').textContent= d.total || 0;
}

// ── P&L tab ───────────────────────────────────────────────────────────
async function loadPnl() {
  const data = await fetch('/api/telemetry/pnl').then(r=>r.json());
  renderPnlStats(data);
  renderPnlChart(data);
  renderPnlTable(data);
}

function renderPnlStats(data) {
  const wrap = document.getElementById('pnl-stats');
  if (!data.length) {
    wrap.innerHTML = '<div class="empty-msg" style="padding:0">No exits recorded yet.</div>';
    return;
  }
  const pcts = data.map(d => d.pnl_pct * 100);
  const wins = pcts.filter(p => p > 0).length;
  const winRate = (wins / pcts.length * 100).toFixed(0);
  const avg = (pcts.reduce((a,b) => a+b, 0) / pcts.length).toFixed(1);
  const best = Math.max(...pcts).toFixed(1);
  const worst = Math.min(...pcts).toFixed(1);
  const total = data.length;

  const statCard = (label, value, cls) =>
    `<div class="stat-card"><div class="stat-label">${label}</div><div class="stat-value ${cls}">${value}</div></div>`;

  wrap.innerHTML = [
    statCard('Total Exits', total, 'neu'),
    statCard('Win Rate', winRate + '%', parseFloat(winRate) >= 50 ? 'pos' : 'neg'),
    statCard('Avg P&L', (avg >= 0 ? '+' : '') + avg + '%', parseFloat(avg) >= 0 ? 'pos' : 'neg'),
    statCard('Best', '+' + best + '%', 'pos'),
    statCard('Worst', worst + '%', 'neg'),
  ].join('');
}

function renderPnlChart(data) {
  const wrap = document.getElementById('pnl-chart-wrap');
  if (!data.length) { wrap.innerHTML = ''; return; }

  const items = data.slice(-60); // last 60 exits
  const pcts = items.map(d => d.pnl_pct * 100);
  const absMax = Math.max(Math.abs(Math.max(...pcts)), Math.abs(Math.min(...pcts)), 10);
  const yMax = Math.ceil(absMax * 1.15);

  const lpad=48, rpad=16, tpad=16, bpad=52;
  const barW=18, barGap=6;
  const W = lpad + items.length * (barW + barGap) + rpad;
  const H = 200;
  const chartH = H - tpad - bpad;
  const zeroY = tpad + chartH * yMax / (yMax * 2);

  const scaleY = v => tpad + chartH * (yMax - v) / (yMax * 2);

  let s = `<div class="pnl-chart-title">P&L per exit (last ${items.length} trades)</div>`;
  s += `<svg viewBox="0 0 ${W} ${H}" width="${Math.max(W, 400)}" height="${H}" style="display:block">`;

  // Y axis gridlines and labels
  const yTicks = [];
  const step = yMax <= 20 ? 5 : yMax <= 50 ? 10 : yMax <= 100 ? 25 : 50;
  for (let v = -yMax; v <= yMax; v += step) yTicks.push(v);
  for (const v of yTicks) {
    const y = scaleY(v);
    const isZero = v === 0;
    s += `<line x1="${lpad}" y1="${y}" x2="${W-rpad}" y2="${y}"
               stroke="${isZero ? 'var(--muted)' : 'var(--border)'}"
               stroke-width="${isZero ? 1.5 : 1}" stroke-dasharray="${isZero ? '' : '3,4'}"/>`;
    s += `<text x="${lpad-6}" y="${y+4}" text-anchor="end" font-size="9"
               fill="${isZero ? 'var(--text)' : 'var(--muted)'}">${v > 0 ? '+' : ''}${v}%</text>`;
  }

  // Bars
  items.forEach((d, i) => {
    const pct = d.pnl_pct * 100;
    const x = lpad + i * (barW + barGap);
    const barH = Math.max(2, Math.abs(scaleY(pct) - zeroY));
    const y = pct >= 0 ? scaleY(pct) : zeroY;
    const color = pct >= 0 ? 'var(--green)' : 'var(--red)';
    const reason = (d.reason || '').replace('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
    const ts = d.timestamp ? new Date(d.timestamp).toLocaleDateString() : '';
    s += `<rect x="${x}" y="${y}" width="${barW}" height="${barH}" fill="${color}" rx="2" opacity=".85">
            <title>${d.ticker} · ${(pct >= 0 ? '+' : '') + pct.toFixed(1)}% · ${reason} · ${ts}</title>
          </rect>`;
    // X label: ticker, rotated
    const labelX = x + barW / 2;
    const labelY = H - bpad + 10;
    s += `<text x="${labelX}" y="${labelY}" text-anchor="end" font-size="9" fill="var(--muted)"
               transform="rotate(-45,${labelX},${labelY})">${d.ticker}</text>`;
  });

  s += `</svg>`;
  wrap.innerHTML = s;
}

function renderPnlTable(data) {
  const wrap = document.getElementById('pnl-table-wrap');
  if (!data.length) { wrap.innerHTML = ''; return; }
  const reasonLabel = r => (r||'').replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
  wrap.innerHTML = `<table>
    <thead><tr><th>Time</th><th>Ticker</th><th>P&L %</th><th>Exit Reason</th></tr></thead>
    <tbody>
    ${[...data].reverse().map(d => {
      const pct = (d.pnl_pct * 100);
      const cls = pct >= 0 ? 'result-ok' : 'result-err';
      const sign = pct >= 0 ? '+' : '';
      const ts = d.timestamp ? new Date(d.timestamp).toLocaleString() : '—';
      return `<tr>
        <td style="color:var(--muted);font-size:11px">${ts}</td>
        <td><b>${d.ticker}</b></td>
        <td class="${cls}"><b>${sign}${pct.toFixed(1)}%</b></td>
        <td style="color:var(--muted);font-size:11px">${reasonLabel(d.reason)}</td>
      </tr>`;
    }).join('')}
    </tbody></table>`;
}

// ── Glossary ──────────────────────────────────────────────────────────
function initGlossary() {
  const btn   = document.getElementById('glossary-btn');
  const panel = document.getElementById('glossary-panel');
  const dl = Object.entries(GLOSSARY).map(([t,d])=>
    `<div class="gloss-term"><dt>${t}</dt><dd>${d}</dd></div>`).join('');
  panel.innerHTML = `<div class="gloss-title">Glossary</div><dl>${dl}</dl>`;
  btn.addEventListener('click', () => panel.classList.toggle('open'));
}

// ── Refresh loop ──────────────────────────────────────────────────────
let countdown = 15;
function tick() {
  countdown--;
  document.getElementById('refresh-timer').textContent = `Refresh in ${countdown}s`;
  if (countdown <= 0) {
    countdown = 15;
    refreshAll();
  }
}
function refreshAll() {
  loadOverview();
  const activeTab = document.querySelector('nav.tabs button.active')?.dataset.tab;
  if (activeTab === 'market')     loadMarket();
  if (activeTab === 'decisions')  loadDecisions();
  if (activeTab === 'proposals')  loadProposals();
  if (activeTab === 'log')        loadLog();
  if (activeTab === 'pnl')        loadPnl();
}

// ── Settings tab ──────────────────────────────────────────────────────
const SETTINGS_FIELDS = [
  {key:'seed_tickers', label:'Seed Tickers', type:'text',
   hint:'Comma-separated; always scanned every cycle, exempt from the premium threshold (e.g. SPY,QQQ,SPX)'},
  {key:'discovery_min_premium', label:'Discovery Min Premium ($)', type:'number',
   hint:'Minimum flow-alert premium for a ticker to enter the hourly scan'},
  {key:'max_discovered_tickers', label:'Max Discovered Tickers', type:'number',
   hint:'Cap on discovered tickers per scan cycle (1–100)'},
  {key:'flow_min_premium', label:'Flow Min Premium ($)', type:'number',
   hint:'Minimum whale-print premium to confirm a trade signal'},
  {key:'stop_loss_pct', label:'Stop Loss (fraction)', type:'number', step:'0.01',
   hint:'Close if option premium drops this fraction from entry (0.01–0.95, e.g. 0.35 = 35%)'},
  {key:'dte_floor', label:'DTE Floor (days)', type:'number',
   hint:'Close positions when days-to-expiry reaches this value (0–30)'},
];

async function loadSettings() {
  const wrap = document.getElementById('settings-wrap');
  let cfg;
  try { cfg = await fetch('/api/config').then(r => r.json()); }
  catch (e) { wrap.innerHTML = '<div class="empty-msg">Failed to load config.</div>'; return; }
  if (cfg.error) { wrap.innerHTML = `<div class="empty-msg">${cfg.error}</div>`; return; }

  wrap.innerHTML = `
    <div style="max-width:560px">
      <div style="font-size:12px;color:var(--muted);margin-bottom:14px">
        Changes apply from the next scan / poll cycle — no restart needed. Saved to disk, so they survive restarts and override .env values.
      </div>
      ${SETTINGS_FIELDS.map(f => {
        const val = f.key === 'seed_tickers' ? (cfg[f.key]||[]).join(',') : (cfg[f.key] ?? '');
        return `<div style="margin-bottom:14px">
          <label style="display:block;font-size:12px;font-weight:600;margin-bottom:3px">${f.label}</label>
          <input id="set-${f.key}" type="${f.type}" ${f.step ? `step="${f.step}"` : ''} value="${val}"
            style="width:100%;padding:7px 10px;background:var(--bg);color:var(--text);
                   border:1px solid var(--border);border-radius:var(--radius);font-size:13px">
          <div style="font-size:11px;color:var(--muted);margin-top:3px">${f.hint}</div>
        </div>`;
      }).join('')}
      <button id="settings-save" style="padding:8px 20px;background:var(--blue);color:#fff;border:none;
        border-radius:var(--radius);font-size:13px;font-weight:600;cursor:pointer">Save</button>
      <span id="settings-status" style="margin-left:12px;font-size:12px"></span>
    </div>`;

  document.getElementById('settings-save').addEventListener('click', saveSettings);
}

async function saveSettings() {
  const status = document.getElementById('settings-status');
  const body = {};
  for (const f of SETTINGS_FIELDS) {
    body[f.key] = document.getElementById('set-' + f.key).value;
  }
  status.style.color = 'var(--muted)';
  status.textContent = 'Saving…';
  try {
    const resp = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.ok) {
      status.style.color = 'var(--green)';
      status.textContent = '✓ Saved — applies next cycle';
    } else {
      status.style.color = 'var(--red)';
      status.textContent = (data.errors || [data.error || 'save failed']).join('; ');
    }
  } catch (e) {
    status.style.color = 'var(--red)';
    status.textContent = 'Network error — not saved';
  }
}

// ── Boot ──────────────────────────────────────────────────────────────
initTabs();
initGlossary();
loadOverview();
loadMarket();       // always pre-load market on boot
refreshAll();
setInterval(tick, 1000);

// Tab click: refresh data tabs; settings loads once on open (not on the
// auto-refresh timer, so in-progress edits are never overwritten)
document.querySelectorAll('nav.tabs button').forEach(btn => {
  btn.addEventListener('click', () => {
    countdown = 15;
    if (btn.dataset.tab === 'settings') { loadSettings(); return; }
    refreshAll();
  });
});
"""

# ---------------------------------------------------------------------------
# Dashboard HTML shell
# ---------------------------------------------------------------------------

_DASHBOARD_BODY = """
<header>
  <h1>GEX Trading Agent</h1>
  <span id="badge-mode">rh_approval</span>
  <div style="display:flex;gap:16px;margin-left:auto;align-items:center">
    <div style="display:flex;gap:10px;font-size:12px">
      <span style="color:var(--muted)">Last hr:</span>
      <span style="color:var(--green)"><b id="ov-ok">—</b> ok</span>
      <span style="color:var(--yellow)"><b id="ov-skip">—</b> skip</span>
      <span style="color:var(--red)"><b id="ov-err">—</b> err</span>
      <span style="color:var(--muted)"><b id="ov-total">—</b> total</span>
    </div>
    <span id="refresh-timer" style="color:var(--muted);font-size:11px">Refresh in 15s</span>
  </div>
</header>

<nav class="tabs">
  <button class="active" data-tab="market">Market</button>
  <button data-tab="decisions">Decisions</button>
  <button data-tab="proposals">Proposals</button>
  <button data-tab="log">Log</button>
  <button data-tab="pnl">P&amp;L</button>
  <button data-tab="settings">Settings</button>
</nav>

<div id="tab-market" class="tab-pane active">
  <div id="market-grid"><div class="empty-msg">Loading…</div></div>
</div>

<div id="tab-decisions" class="tab-pane">
  <div id="decisions-wrap"><div class="empty-msg">Loading…</div></div>
</div>

<div id="tab-proposals" class="tab-pane">
  <div id="proposals-wrap"><div class="empty-msg">Loading…</div></div>
</div>

<div id="tab-log" class="tab-pane">
  <div class="log-wrap" id="log-wrap"><div class="empty-msg">Loading…</div></div>
</div>

<div id="tab-pnl" class="tab-pane">
  <div id="pnl-stats" class="pnl-stats-row"><div class="empty-msg" style="padding:0">Loading…</div></div>
  <div id="pnl-chart-wrap" class="pnl-chart-outer" style="margin-top:4px"></div>
  <div id="pnl-table-wrap" class="pnl-table-wrap"></div>
</div>

<div id="tab-settings" class="tab-pane">
  <div id="settings-wrap"><div class="empty-msg">Loading…</div></div>
</div>

<!-- Detail drawer -->
<div id="drawer">
  <div id="drawer-header">
    <span id="drawer-title">Decision Detail</span>
    <button id="drawer-close">×</button>
  </div>
  <div id="drawer-body"></div>
</div>

<!-- Glossary -->
<button id="glossary-btn">? Glossary</button>
<div id="glossary-panel"></div>
"""


def _build_dashboard_html(token: str = "") -> str:
    """Render the dashboard HTML, optionally injecting a DASHBOARD_TOKEN into all API fetch calls."""
    token_init = (
        f"const API_TOKEN={json.dumps(token)};\n"
        "function apiFetch(url,opts){"
        "if(!API_TOKEN)return fetch(url,opts);"
        "const u=new URL(url,location.origin);"
        'u.searchParams.set("token",API_TOKEN);'
        "return fetch(u.toString(),opts);}\n"
    )
    js = token_init + _JS.replace("fetch(", "apiFetch(")
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '  <meta charset="UTF-8">\n'
        '  <meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "  <title>GEX Trading Agent</title>\n"
        f"  <style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        f"{_DASHBOARD_BODY}"
        f"<script>{js}</script>\n"
        "</body>\n</html>"
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def create_app(
    proposal_store: ProposalStore,
    executor: Executor,
    tel: TelemetryLogger | None = None,
    telemetry_reader: TelemetryReader | None = None,
    cache: GEXCache | None = None,
    dashboard_token: str = "",
    position_store: PositionStore | None = None,
    config: LiveConfig | None = None,
    order_manager: OrderLifecycleManager | None = None,
) -> web.Application:
    reader = telemetry_reader or TelemetryReader()

    middlewares: list[Callable] = []
    if dashboard_token:
        @web.middleware
        async def _auth(request: web.Request, handler: Callable) -> web.StreamResponse:
            if request.path == "/health":
                return await handler(request)
            provided = (
                request.rel_url.query.get("token")
                or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            )
            if provided != dashboard_token:
                return web.Response(status=401, text="Unauthorized")
            return await handler(request)
        middlewares.append(_auth)

    app = web.Application(middlewares=middlewares)

    # ── Health ──────────────────────────────────────────────────────────
    async def health(req: web.Request) -> web.Response:
        return _json_response({"status": "ok"})

    # ── Dashboard ───────────────────────────────────────────────────────
    async def dashboard(req: web.Request) -> web.Response:
        return web.Response(text=_build_dashboard_html(dashboard_token), content_type="text/html")

    # ── Proposals ───────────────────────────────────────────────────────
    async def list_proposals(req: web.Request) -> web.Response:
        pending = await proposal_store.list_pending()
        return _json_response([p.detail() for p in pending])

    async def get_proposal(req: web.Request) -> web.Response:
        pid = req.match_info["id"]
        p = await proposal_store.get(pid)
        if p is None:
            return _json_response({"error": "not found"}, status=404)
        return _json_response(p.detail())

    async def approve_proposal(req: web.Request) -> web.Response:
        pid = req.match_info["id"]
        proposal = await proposal_store.approve(pid)
        if proposal is None:
            return _json_response({"error": "proposal not found or already decided"}, status=404)
        t0 = _time.monotonic()
        try:
            # execute_approved: the human already approved via the dashboard —
            # must not route through the LangGraph interrupt in execute()
            result = await executor.execute_approved(proposal.candidate)
            ms = round((_time.monotonic() - t0) * 1000, 1)
            if tel:
                c = proposal.candidate
                sc = c.selected_contract
                tel.order_attempt(
                    ticker=c.ticker,
                    mode="rh_approval",
                    action="buy",
                    quantity=result.request.quantity,
                    limit_price=float(sc.mid) if sc else None,
                    placed=result.placed,
                    order_id=result.order_id,
                    account_number=executor.account_number or None,
                    rejection_reason=result.rejection_reason,
                    review_summary=result.review_summary,
                    duration_ms=ms,
                )
            if result.placed:
                if order_manager is not None:
                    # Lifecycle manager promotes to a tracked position on fill
                    await order_manager.track(proposal.candidate, result)
                elif position_store is not None:
                    from .position_store import make_position
                    pos = make_position(proposal.candidate, result, result.request.quantity)
                    if pos:
                        await position_store.add(pos)
                        logger.info("Position tracked %s position_id=%s",
                                    proposal.candidate.ticker, pos.position_id)
            await proposal_store.mark_executed(pid, result)
            return _json_response({"status": "executed", "placed": result.placed, "order_id": result.order_id})
        except Exception as exc:
            logger.error("Execute failed for proposal %s: %s", pid, exc)
            return _json_response({"error": str(exc)}, status=500)

    async def reject_proposal(req: web.Request) -> web.Response:
        pid = req.match_info["id"]
        note = ""
        try:
            body = await req.json()
            note = body.get("note", "")
        except Exception:
            pass
        proposal = await proposal_store.reject(pid, note=note)
        if proposal is None:
            return _json_response({"error": "proposal not found or already decided"}, status=404)
        return _json_response({"status": "rejected"})

    # ── Decisions API ───────────────────────────────────────────────────
    async def list_decisions(req: web.Request) -> web.Response:
        limit = int(req.rel_url.query.get("limit", "200"))
        return _json_response(reader.decisions(limit=limit))

    async def get_decision(req: web.Request) -> web.Response:
        run_id = req.match_info["run_id"]
        # Also check proposal store for rich gex/blend/contract data
        detail = reader.decision_detail(run_id)
        if detail is None:
            return _json_response({"error": "not found"}, status=404)
        return _json_response(detail)

    # ── Market API ──────────────────────────────────────────────────────
    async def market_snapshot(req: web.Request) -> web.Response:
        if cache is None:
            return _json_response([])
        snapshots = await cache.all_snapshots()
        out = []
        for ticker, snap in snapshots.items():
            gs = snap.gex_setup
            if gs is None:
                continue
            out.append({
                "ticker": ticker,
                "regime": gs.regime.value,
                "direction": gs.candidate_direction,
                "setup_type": gs.setup_type,
                "confidence": gs.structure_confidence,
                "flip_point": float(gs.flip_point) if gs.flip_point else None,
                "target_level": float(gs.target_level) if gs.target_level else None,
                "call_wall": float(gs.nearest_call_wall.strike) if gs.nearest_call_wall else None,
                "put_wall": float(gs.nearest_put_wall.strike) if gs.nearest_put_wall else None,
                "spot_price": float(gs.spot_price),
                "last_scan": snap.refreshed_at.isoformat(),
                "stale": snap.is_stale,
            })
        out.sort(key=lambda t: t["confidence"], reverse=True)
        return _json_response(out)

    # ── Config API ──────────────────────────────────────────────────────
    async def get_config(req: web.Request) -> web.Response:
        if config is None:
            return _json_response({"error": "runtime config not enabled"}, status=404)
        return _json_response(config.to_dict())

    async def update_config(req: web.Request) -> web.Response:
        if config is None:
            return _json_response({"error": "runtime config not enabled"}, status=404)
        try:
            body = await req.json()
        except Exception:
            return _json_response({"error": "invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return _json_response({"error": "expected a JSON object"}, status=400)
        errors = config.update(body)
        if errors:
            return _json_response({"errors": errors, "config": config.to_dict()}, status=400)
        logger.info("Config updated via dashboard: %s", config.to_dict())
        return _json_response({"status": "saved", "config": config.to_dict()})

    # ── Telemetry API ───────────────────────────────────────────────────
    async def telemetry_summary(req: web.Request) -> web.Response:
        return _json_response(reader.summary_last_hour())

    async def telemetry_funnel(req: web.Request) -> web.Response:
        return _json_response(reader.funnel_today())

    async def telemetry_recent(req: web.Request) -> web.Response:
        n = int(req.rel_url.query.get("n", "50"))
        return _json_response(reader.recent(n=n))

    async def telemetry_pnl(req: web.Request) -> web.Response:
        return _json_response(reader.pnl_series())

    app.router.add_get("/health", health)
    app.router.add_get("/", dashboard)
    app.router.add_get("/proposals", list_proposals)
    app.router.add_get("/proposals/{id}", get_proposal)
    app.router.add_post("/proposals/{id}/approve", approve_proposal)
    app.router.add_post("/proposals/{id}/reject", reject_proposal)
    app.router.add_get("/api/decisions", list_decisions)
    app.router.add_get("/api/decisions/{run_id}", get_decision)
    app.router.add_get("/api/market", market_snapshot)
    app.router.add_get("/api/config", get_config)
    app.router.add_post("/api/config", update_config)
    app.router.add_get("/api/telemetry/summary", telemetry_summary)
    app.router.add_get("/api/telemetry/funnel", telemetry_funnel)
    app.router.add_get("/api/telemetry/recent", telemetry_recent)
    app.router.add_get("/api/telemetry/pnl", telemetry_pnl)

    return app
