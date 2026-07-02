"""HTTP approval server — exposes pending proposals and accepts approve/reject actions.

Endpoints:
    GET  /proposals          — list all pending proposals (JSON array)
    GET  /proposals/{id}     — get one proposal (JSON)
    POST /proposals/{id}/approve  — approve and immediately execute via Executor
    POST /proposals/{id}/reject   — reject with optional {"note": "..."} body
    GET  /health             — liveness check

Dashboard endpoints:
    GET  /                         — HTML dashboard
    GET  /api/telemetry/summary    — counts by stage+result (last hour)
    GET  /api/telemetry/funnel     — pipeline funnel for today
    GET  /api/telemetry/recent     — last 50 telemetry events
    GET  /api/telemetry/pnl        — exit_signal pnl_pct time series

The server runs in the same asyncio event loop as the scanner and watcher.
It is intentionally minimal — no auth on these endpoints, so you should
run behind a reverse proxy with auth (nginx + basic auth, or Fly.io private networking).
"""

from __future__ import annotations

import json
import logging
import time as _time
from typing import TYPE_CHECKING

from aiohttp import web

from trader.executor.schemas import ExecutionMode
from trader.telemetry.logger import TelemetryLogger

from .proposals import ProposalStore
from .telemetry_reader import TelemetryReader

if TYPE_CHECKING:
    from trader.executor.executor import Executor

logger = logging.getLogger(__name__)


def _json_response(data, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, default=str),
        content_type="application/json",
        status=status,
    )


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GEX Trading Agent Dashboard</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #0d1117;
      --surface: #161b22;
      --border: #30363d;
      --text: #c9d1d9;
      --muted: #8b949e;
      --green: #3fb950;
      --yellow: #d29922;
      --red: #f85149;
      --blue: #58a6ff;
      --accent: #1f6feb;
      --radius: 6px;
    }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.5; }
    header { background: var(--surface); border-bottom: 1px solid var(--border);
             padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
    header h1 { font-size: 18px; font-weight: 600; }
    #refresh-badge { margin-left: auto; font-size: 12px; color: var(--muted); }
    #refresh-badge span { color: var(--green); }
    main { padding: 20px 24px; display: grid; gap: 20px; }
    .panel { background: var(--surface); border: 1px solid var(--border);
             border-radius: var(--radius); padding: 16px; }
    .panel-title { font-size: 13px; font-weight: 600; text-transform: uppercase;
                   letter-spacing: 0.08em; color: var(--muted); margin-bottom: 14px; }
    /* Overview */
    .stat-row { display: flex; gap: 16px; flex-wrap: wrap; }
    .stat-card { flex: 1; min-width: 90px; background: var(--bg);
                 border: 1px solid var(--border); border-radius: var(--radius);
                 padding: 12px 16px; text-align: center; }
    .stat-card .num { font-size: 28px; font-weight: 700; }
    .stat-card .lbl { font-size: 11px; color: var(--muted); text-transform: uppercase;
                      letter-spacing: 0.06em; margin-top: 2px; }
    .ok-color { color: var(--green); }
    .skip-color { color: var(--yellow); }
    .err-color { color: var(--red); }
    /* Funnel */
    .funnel-row { display: flex; flex-direction: column; gap: 8px; }
    .funnel-stage { display: grid; grid-template-columns: 130px 1fr 80px;
                    align-items: center; gap: 10px; }
    .funnel-stage .stage-name { font-size: 12px; color: var(--muted); text-align: right; }
    .funnel-bar-wrap { background: var(--bg); border-radius: 3px; height: 18px;
                       position: relative; overflow: hidden; border: 1px solid var(--border); }
    .funnel-bar-ok   { background: var(--green); height: 100%; float: left; }
    .funnel-bar-skip { background: var(--yellow); height: 100%; float: left; }
    .funnel-bar-err  { background: var(--red);    height: 100%; float: left; }
    .funnel-stage .count-label { font-size: 12px; color: var(--text); }
    /* Proposals table */
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th { text-align: left; font-size: 11px; text-transform: uppercase;
         letter-spacing: 0.06em; color: var(--muted); padding: 6px 10px;
         border-bottom: 1px solid var(--border); }
    td { padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: middle; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(255,255,255,0.02); }
    .btn { display: inline-block; padding: 4px 12px; border-radius: 4px; border: none;
           cursor: pointer; font-size: 12px; font-weight: 600; transition: opacity 0.15s; }
    .btn:hover { opacity: 0.8; }
    .btn-approve { background: var(--green); color: #000; }
    .btn-reject  { background: var(--red);   color: #fff; margin-left: 6px; }
    .btn:disabled { opacity: 0.4; cursor: not-allowed; }
    /* Recent events */
    .events-wrap { max-height: 320px; overflow-y: auto; }
    .result-ok   { color: var(--green); }
    .result-skip { color: var(--yellow); }
    .result-err  { color: var(--red); }
    /* P&L canvas */
    #pnl-canvas { display: block; width: 100%; height: 180px; background: var(--bg);
                  border-radius: 4px; border: 1px solid var(--border); }
    .empty-msg { color: var(--muted); font-style: italic; font-size: 13px; padding: 12px 0; }
    /* Scrollbar */
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: var(--bg); }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  </style>
</head>
<body>

<header>
  <h1>GEX Trading Agent</h1>
  <div id="refresh-badge">Auto-refresh: <span id="countdown">10</span>s</div>
</header>

<main>

  <!-- 1. Overview -->
  <div class="panel">
    <div class="panel-title">Overview — last hour</div>
    <div class="stat-row" id="overview-stats">
      <div class="stat-card"><div class="num ok-color" id="ov-ok">--</div><div class="lbl">OK</div></div>
      <div class="stat-card"><div class="num skip-color" id="ov-skip">--</div><div class="lbl">Skipped</div></div>
      <div class="stat-card"><div class="num err-color" id="ov-err">--</div><div class="lbl">Errors</div></div>
      <div class="stat-card"><div class="num" id="ov-total">--</div><div class="lbl">Total</div></div>
    </div>
  </div>

  <!-- 2. Pipeline funnel -->
  <div class="panel">
    <div class="panel-title">Pipeline funnel — today</div>
    <div class="funnel-row" id="funnel-body">
      <div class="empty-msg">Loading…</div>
    </div>
  </div>

  <!-- 3. Live proposals -->
  <div class="panel">
    <div class="panel-title">Live proposals <span id="proposals-count" style="color:var(--muted);font-weight:400;font-size:12px;margin-left:8px;"></span></div>
    <div id="proposals-wrap">
      <div class="empty-msg">Loading…</div>
    </div>
  </div>

  <!-- 4. Recent events -->
  <div class="panel">
    <div class="panel-title">Recent events</div>
    <div class="events-wrap">
      <table id="events-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Stage</th>
            <th>Ticker</th>
            <th>Result</th>
            <th>Reason</th>
            <th>ms</th>
          </tr>
        </thead>
        <tbody id="events-body">
          <tr><td colspan="6" class="empty-msg">Loading…</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 5. P&L timeline -->
  <div class="panel">
    <div class="panel-title">P&L timeline — exit signals</div>
    <canvas id="pnl-canvas"></canvas>
    <div id="pnl-empty" class="empty-msg" style="display:none;">No exit signal events yet.</div>
  </div>

</main>

<script>
// ------------------------------------------------------------------ helpers

function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
}

function resultClass(r) {
  if (r === 'ok') return 'result-ok';
  if (r === 'skipped') return 'result-skip';
  return 'result-err';
}

function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

// ------------------------------------------------------------------ overview

async function loadOverview() {
  const data = await fetch('/api/telemetry/summary').then(r => r.json()).catch(() => null);
  if (!data) return;
  const br = data.by_result || {};
  document.getElementById('ov-ok').textContent    = br.ok      ?? 0;
  document.getElementById('ov-skip').textContent  = br.skipped ?? 0;
  document.getElementById('ov-err').textContent   = br.error   ?? 0;
  document.getElementById('ov-total').textContent = data.total  ?? 0;
}

// ------------------------------------------------------------------ funnel

async function loadFunnel() {
  const data = await fetch('/api/telemetry/funnel').then(r => r.json()).catch(() => null);
  const el = document.getElementById('funnel-body');
  if (!data || !data.length) { el.innerHTML = '<div class="empty-msg">No data yet.</div>'; return; }

  const maxOk = Math.max(1, ...data.map(s => s.ok));

  el.innerHTML = data.map(s => {
    const total = s.ok + s.skipped + s.error;
    const pOk   = total ? (s.ok   / total * 100).toFixed(1) : 0;
    const pSkip = total ? (s.skipped / total * 100).toFixed(1) : 0;
    const pErr  = total ? (s.error / total * 100).toFixed(1) : 0;
    // bar width based on ok count relative to max
    const barW  = maxOk ? (s.ok / maxOk * 100).toFixed(1) : 0;
    return `<div class="funnel-stage">
      <div class="stage-name">${esc(s.stage)}</div>
      <div class="funnel-bar-wrap" title="ok:${s.ok} skipped:${s.skipped} error:${s.error}">
        <div class="funnel-bar-ok"   style="width:${pOk}%"></div>
        <div class="funnel-bar-skip" style="width:${pSkip}%"></div>
        <div class="funnel-bar-err"  style="width:${pErr}%"></div>
      </div>
      <div class="count-label ok-color">${s.ok}</div>
    </div>`;
  }).join('');
}

// ------------------------------------------------------------------ proposals

async function loadProposals() {
  const data = await fetch('/proposals').then(r => r.json()).catch(() => null);
  const wrap = document.getElementById('proposals-wrap');
  const badge = document.getElementById('proposals-count');

  if (!data) { wrap.innerHTML = '<div class="empty-msg">Could not load proposals.</div>'; return; }

  const pending = data.filter(p => p.status === 'pending');
  badge.textContent = pending.length ? `(${pending.length} pending)` : '';

  if (!pending.length) {
    wrap.innerHTML = '<div class="empty-msg">No pending proposals.</div>';
    return;
  }

  wrap.innerHTML = `<table>
    <thead><tr>
      <th>Ticker</th><th>Strike</th><th>Expiry</th><th>Score</th>
      <th>Regime</th><th>Limit $</th><th>Actions</th>
    </tr></thead>
    <tbody>
    ${pending.map(p => `<tr id="prop-${esc(p.proposal_id)}">
      <td><strong>${esc(p.ticker)}</strong></td>
      <td>${p.strike != null ? p.strike : '—'}</td>
      <td>${esc(p.expiry) || '—'}</td>
      <td>${p.composite_score != null ? p.composite_score.toFixed(3) : '—'}</td>
      <td>${esc(p.regime) || '—'}</td>
      <td>${p.limit_price != null ? '$' + p.limit_price.toFixed(2) : '—'}</td>
      <td>
        <button class="btn btn-approve" onclick="doApprove('${esc(p.proposal_id)}')">Approve</button>
        <button class="btn btn-reject"  onclick="doReject('${esc(p.proposal_id)}')">Reject</button>
      </td>
    </tr>`).join('')}
    </tbody>
  </table>`;
}

async function doApprove(id) {
  const row = document.getElementById('prop-' + id);
  if (row) row.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    const res = await fetch('/proposals/' + id + '/approve', {method: 'POST'});
    const json = await res.json();
    if (row) row.querySelector('.btn-approve').textContent = json.placed ? 'Placed!' : 'Approved';
  } catch(e) {
    if (row) row.querySelector('.btn-approve').textContent = 'Error';
  }
  setTimeout(loadProposals, 1500);
}

async function doReject(id) {
  const note = prompt('Rejection note (optional):') || '';
  const row = document.getElementById('prop-' + id);
  if (row) row.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    await fetch('/proposals/' + id + '/reject', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({note})
    });
    if (row) row.querySelector('.btn-reject').textContent = 'Rejected';
  } catch(e) {
    if (row) row.querySelector('.btn-reject').textContent = 'Error';
  }
  setTimeout(loadProposals, 1500);
}

// ------------------------------------------------------------------ recent events

async function loadRecent() {
  const data = await fetch('/api/telemetry/recent').then(r => r.json()).catch(() => null);
  const tbody = document.getElementById('events-body');
  if (!data || !data.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-msg">No events yet.</td></tr>';
    return;
  }
  tbody.innerHTML = data.map(ev => {
    const rc = resultClass(ev.result);
    return `<tr>
      <td style="white-space:nowrap;color:var(--muted)">${fmtTime(ev.timestamp)}</td>
      <td>${esc(ev.stage)}</td>
      <td>${esc(ev.ticker) || '<span style="color:var(--muted)">—</span>'}</td>
      <td class="${rc}">${esc(ev.result)}</td>
      <td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)"
          title="${esc(ev.reason)}">${esc(ev.reason) || ''}</td>
      <td style="color:var(--muted)">${ev.duration_ms != null ? ev.duration_ms : ''}</td>
    </tr>`;
  }).join('');
}

// ------------------------------------------------------------------ P&L chart

async function loadPnl() {
  const data = await fetch('/api/telemetry/pnl').then(r => r.json()).catch(() => null);
  const canvas = document.getElementById('pnl-canvas');
  const empty  = document.getElementById('pnl-empty');

  if (!data || !data.length) {
    canvas.style.display = 'none';
    empty.style.display  = 'block';
    return;
  }
  canvas.style.display = 'block';
  empty.style.display  = 'none';

  const dpr  = window.devicePixelRatio || 1;
  const W    = canvas.offsetWidth  || 600;
  const H    = canvas.offsetHeight || 180;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  const ctx  = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  const pad = { top: 20, right: 20, bottom: 30, left: 52 };
  const cw  = W - pad.left - pad.right;
  const ch  = H - pad.top  - pad.bottom;

  const vals = data.map(d => d.pnl_pct);
  const minV = Math.min(...vals);
  const maxV = Math.max(...vals);
  const range = maxV - minV || 1;

  function xOf(i) { return pad.left + (i / (vals.length - 1 || 1)) * cw; }
  function yOf(v) { return pad.top  + (1 - (v - minV) / range) * ch; }

  // Grid lines
  ctx.strokeStyle = 'rgba(48,54,61,0.8)';
  ctx.lineWidth   = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (i / 4) * ch;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y); ctx.stroke();
    const label = (maxV - (i / 4) * range).toFixed(1) + '%';
    ctx.fillStyle = '#8b949e'; ctx.font = '10px sans-serif'; ctx.textAlign = 'right';
    ctx.fillText(label, pad.left - 6, y + 4);
  }

  // Zero line
  if (minV < 0 && maxV > 0) {
    const zy = yOf(0);
    ctx.strokeStyle = 'rgba(139,148,158,0.4)';
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(pad.left, zy); ctx.lineTo(W - pad.right, zy); ctx.stroke();
    ctx.setLineDash([]);
  }

  // Gradient fill
  const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + ch);
  grad.addColorStop(0,   'rgba(63,185,80,0.35)');
  grad.addColorStop(1,   'rgba(63,185,80,0.0)');

  ctx.beginPath();
  ctx.moveTo(xOf(0), yOf(vals[0]));
  for (let i = 1; i < vals.length; i++) ctx.lineTo(xOf(i), yOf(vals[i]));
  ctx.lineTo(xOf(vals.length - 1), pad.top + ch);
  ctx.lineTo(xOf(0), pad.top + ch);
  ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();

  // Line
  ctx.beginPath();
  ctx.strokeStyle = '#3fb950'; ctx.lineWidth = 2;
  ctx.moveTo(xOf(0), yOf(vals[0]));
  for (let i = 1; i < vals.length; i++) ctx.lineTo(xOf(i), yOf(vals[i]));
  ctx.stroke();

  // Dots
  for (let i = 0; i < vals.length; i++) {
    const v = vals[i];
    ctx.beginPath();
    ctx.arc(xOf(i), yOf(v), 3.5, 0, Math.PI * 2);
    ctx.fillStyle = v >= 0 ? '#3fb950' : '#f85149';
    ctx.fill();
  }

  // X-axis tick labels (up to 8)
  const step = Math.max(1, Math.floor(data.length / 8));
  ctx.fillStyle = '#8b949e'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
  for (let i = 0; i < data.length; i += step) {
    const lbl = data[i].ticker || fmtTime(data[i].timestamp);
    ctx.fillText(lbl, xOf(i), H - 8);
  }
}

// ------------------------------------------------------------------ refresh loop

let countdown = 10;

async function refreshAll() {
  await Promise.all([loadOverview(), loadFunnel(), loadProposals(), loadRecent(), loadPnl()]);
}

refreshAll();

setInterval(() => {
  countdown -= 1;
  document.getElementById('countdown').textContent = countdown;
  if (countdown <= 0) {
    countdown = 10;
    refreshAll();
  }
}, 1000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    proposal_store: ProposalStore,
    executor: "Executor",
    tel: TelemetryLogger | None = None,
    telemetry_reader: TelemetryReader | None = None,
) -> web.Application:
    app = web.Application()

    _tel_reader = telemetry_reader or TelemetryReader()

    # ------------------------------------------------------------------ #
    # GET /health
    # ------------------------------------------------------------------ #

    async def health(_: web.Request) -> web.Response:
        return _json_response({"status": "ok"})

    # ------------------------------------------------------------------ #
    # GET /proposals
    # ------------------------------------------------------------------ #

    async def list_proposals(_: web.Request) -> web.Response:
        pending = await proposal_store.list_pending()
        return _json_response([p.summary() for p in pending])

    # ------------------------------------------------------------------ #
    # GET /proposals/{id}
    # ------------------------------------------------------------------ #

    async def get_proposal(request: web.Request) -> web.Response:
        pid = request.match_info["id"]
        proposal = await proposal_store.get(pid)
        if proposal is None:
            return _json_response({"error": "not found"}, status=404)
        return _json_response(proposal.summary())

    # ------------------------------------------------------------------ #
    # POST /proposals/{id}/approve
    # ------------------------------------------------------------------ #

    async def approve_proposal(request: web.Request) -> web.Response:
        pid = request.match_info["id"]
        proposal = await proposal_store.approve(pid)
        if proposal is None:
            return _json_response({"error": "not found or not pending"}, status=404)

        ticker = proposal.candidate.ticker
        logger.info("Human approved proposal %s for %s", pid, ticker)

        # Execute immediately via the executor in autonomous mode
        # (bypasses interrupt — human already gave approval via HTTP)
        try:
            t0 = _time.monotonic()
            # Temporarily use autonomous logic for the approved candidate
            saved_mode = executor.mode
            executor.mode = ExecutionMode.AUTONOMOUS
            result = await executor.execute(proposal.candidate)
            executor.mode = saved_mode
            ms = round((_time.monotonic() - t0) * 1000, 1)

            await proposal_store.mark_executed(pid, result)

            if tel:
                lp = result.request.limit_price
                tel.order_attempt(
                    ticker=ticker,
                    mode="rh_approval_via_http",
                    action=result.request.action,
                    quantity=result.request.quantity,
                    limit_price=float(lp) if lp is not None else None,
                    placed=result.placed,
                    order_id=result.order_id,
                    account_number=executor.account_number or None,
                    rejection_reason=result.rejection_reason,
                    review_summary=result.review_summary,
                    duration_ms=ms,
                )

            logger.info("%s order placed=%s order_id=%s", ticker, result.placed, result.order_id)
            return _json_response({
                "approved": True,
                "placed": result.placed,
                "order_id": result.order_id,
                "rejection_reason": result.rejection_reason,
                "review_summary": result.review_summary,
            })

        except Exception as exc:
            logger.error("Execute after approval failed for %s: %s", pid, exc)
            return _json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # POST /proposals/{id}/reject
    # ------------------------------------------------------------------ #

    async def reject_proposal(request: web.Request) -> web.Response:
        pid = request.match_info["id"]
        note = ""
        try:
            body = await request.json()
            note = body.get("note", "")
        except Exception:
            pass

        proposal = await proposal_store.reject(pid, note=note)
        if proposal is None:
            return _json_response({"error": "not found or not pending"}, status=404)

        logger.info("Human rejected proposal %s: %s", pid, note or "(no note)")
        return _json_response({"rejected": True, "proposal_id": pid, "note": note})

    # ------------------------------------------------------------------ #
    # GET / — Dashboard HTML
    # ------------------------------------------------------------------ #

    async def dashboard(_: web.Request) -> web.Response:
        return web.Response(text=_DASHBOARD_HTML, content_type="text/html")

    # ------------------------------------------------------------------ #
    # GET /api/telemetry/summary
    # ------------------------------------------------------------------ #

    async def telemetry_summary(_: web.Request) -> web.Response:
        data = _tel_reader.summary_last_hour()
        return _json_response(data)

    # ------------------------------------------------------------------ #
    # GET /api/telemetry/funnel
    # ------------------------------------------------------------------ #

    async def telemetry_funnel(_: web.Request) -> web.Response:
        data = _tel_reader.funnel_today()
        return _json_response(data)

    # ------------------------------------------------------------------ #
    # GET /api/telemetry/recent
    # ------------------------------------------------------------------ #

    async def telemetry_recent(_: web.Request) -> web.Response:
        data = _tel_reader.recent(50)
        return _json_response(data)

    # ------------------------------------------------------------------ #
    # GET /api/telemetry/pnl
    # ------------------------------------------------------------------ #

    async def telemetry_pnl(_: web.Request) -> web.Response:
        data = _tel_reader.pnl_series()
        return _json_response(data)

    # ------------------------------------------------------------------ #
    # Route wiring
    # ------------------------------------------------------------------ #

    app.router.add_get("/", dashboard)
    app.router.add_get("/health", health)
    app.router.add_get("/proposals", list_proposals)
    app.router.add_get("/proposals/{id}", get_proposal)
    app.router.add_post("/proposals/{id}/approve", approve_proposal)
    app.router.add_post("/proposals/{id}/reject", reject_proposal)
    app.router.add_get("/api/telemetry/summary", telemetry_summary)
    app.router.add_get("/api/telemetry/funnel",  telemetry_funnel)
    app.router.add_get("/api/telemetry/recent",  telemetry_recent)
    app.router.add_get("/api/telemetry/pnl",     telemetry_pnl)

    return app
