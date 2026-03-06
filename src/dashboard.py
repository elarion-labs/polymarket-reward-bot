"""
dashboard.py — FastAPI dashboard for the Polymarket Reward Bot.

Endpoints:
  GET  /                  — HTML dashboard (human-friendly)
  GET  /api/state         — JSON snapshot of current bot state
  GET  /api/logs          — last N log lines
  GET  /api/control       — current UI control state
  POST /api/control       — send bot command (START / PAUSE / STOP)
  GET  /api/manual-market
  POST /api/manual-market
  GET  /api/runtime       — runtime overrides + status
  POST /api/runtime       — update runtime overrides
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from models import BotState, DashboardSnapshot
from utils import get_log_tail, utc_iso

logger = logging.getLogger("reward_bot")

app = FastAPI(title="Polymarket Reward Bot Dashboard", version="1.1.0")

# ---------------------------------------------------------------------------
# Shared in-memory state
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()

_snapshot: DashboardSnapshot = DashboardSnapshot(state=BotState.SELECTING)

_control_command: str = ""
_manual_market_ref: dict[str, Any] = {}
_manual_market_updated_at: str = ""

_runtime_overrides: dict[str, Any] = {}
_runtime_overrides_updated_at: str = ""

_runtime_status: dict[str, Any] = {
    "available_usdc": None,
    "usable_capital": None,
    "committed_capital": None,
    "config_effective": {},
    "updated_at": "",
}


class ControlRequest(BaseModel):
    command: str


class ManualMarketRequest(BaseModel):
    ref_type: str
    value: str


class RuntimeOverrideRequest(BaseModel):
    market_selection_mode: Optional[str] = None
    mode: Optional[str] = None
    bankroll_usdc: Optional[float] = None
    max_capital_per_market: Optional[float] = None
    free_usdc_buffer_pct: Optional[float] = None
    max_position_usd: Optional[float] = None
    target_min_distance: Optional[float] = None
    target_max_distance: Optional[float] = None


def update_snapshot(snap: DashboardSnapshot) -> None:
    global _snapshot
    with _state_lock:
        _snapshot = snap


def update_runtime_status(status: dict[str, Any]) -> None:
    global _runtime_status
    with _state_lock:
        merged = dict(_runtime_status)
        merged.update(status or {})
        merged["updated_at"] = utc_iso()
        _runtime_status = merged


def pop_control_command() -> str:
    global _control_command
    with _state_lock:
        cmd = _control_command.strip().upper()
        _control_command = ""
        return cmd


def get_manual_market_ref() -> dict[str, Any]:
    with _state_lock:
        return dict(_manual_market_ref)


def set_manual_market_ref(ref: dict[str, Any]) -> None:
    global _manual_market_ref, _manual_market_updated_at
    with _state_lock:
        _manual_market_ref = dict(ref)
        _manual_market_updated_at = utc_iso()


def get_runtime_overrides() -> dict[str, Any]:
    with _state_lock:
        return dict(_runtime_overrides)


def set_runtime_overrides(overrides: dict[str, Any]) -> None:
    global _runtime_overrides, _runtime_overrides_updated_at
    with _state_lock:
        _runtime_overrides = dict(overrides or {})
        _runtime_overrides_updated_at = utc_iso()


def clear_runtime_overrides() -> None:
    set_runtime_overrides({})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/state", response_class=JSONResponse)
async def get_state():
    """Return current bot state as JSON."""
    with _state_lock:
        snap = _snapshot.model_copy()

    snap.last_updated = utc_iso()
    snap.log_tail = get_log_tail(50)
    return snap.model_dump()


@app.get("/api/logs", response_class=JSONResponse)
async def get_logs(n: int = 100):
    return {"logs": get_log_tail(n)}


@app.get("/api/control", response_class=JSONResponse)
async def get_control():
    with _state_lock:
        return {
            "pending_command": _control_command,
            "manual_market_ref": dict(_manual_market_ref),
            "manual_market_updated_at": _manual_market_updated_at,
        }


@app.post("/api/control", response_class=JSONResponse)
async def post_control(req: ControlRequest):
    global _control_command
    cmd = req.command.strip().upper()
    if cmd not in {"START", "PAUSE", "STOP"}:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "command must be START, PAUSE, or STOP"},
        )

    with _state_lock:
        _control_command = cmd

    logger.info("Dashboard command received: %s", cmd)
    return {"ok": True, "command": cmd}


@app.get("/api/manual-market", response_class=JSONResponse)
async def get_manual_market():
    with _state_lock:
        return {
            "manual_market_ref": dict(_manual_market_ref),
            "updated_at": _manual_market_updated_at,
        }


@app.post("/api/manual-market", response_class=JSONResponse)
async def post_manual_market(req: ManualMarketRequest):
    ref_type = req.ref_type.strip()
    value = req.value.strip()

    allowed = {"url", "slug", "condition_id", "market_id"}
    if ref_type not in allowed:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"ref_type must be one of {sorted(allowed)}"},
        )
    if not value:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "value cannot be empty"},
        )

    ref = {ref_type: value}
    set_manual_market_ref(ref)
    logger.info("Dashboard manual market updated: %s=%s", ref_type, value)
    return {"ok": True, "manual_market_ref": ref, "updated_at": utc_iso()}


@app.get("/api/runtime", response_class=JSONResponse)
async def get_runtime():
    with _state_lock:
        return {
            "overrides": dict(_runtime_overrides),
            "overrides_updated_at": _runtime_overrides_updated_at,
            "status": dict(_runtime_status),
        }


@app.post("/api/runtime", response_class=JSONResponse)
async def post_runtime(req: RuntimeOverrideRequest):
    payload = req.model_dump(exclude_none=True)

    mode = str(payload.get("mode", "") or "").strip().upper()
    if mode and mode not in {"AUTO", "SINGLE_SIDED_SAFE", "TWO_SIDED_BALANCED"}:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "mode must be AUTO, SINGLE_SIDED_SAFE, or TWO_SIDED_BALANCED",
            },
        )

    market_selection_mode = str(payload.get("market_selection_mode", "") or "").strip().upper()
    if market_selection_mode and market_selection_mode not in {"AUTO", "MANUAL"}:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "market_selection_mode must be AUTO or MANUAL",
            },
        )

    clean: dict[str, Any] = {}

    if market_selection_mode:
        clean["market_selection_mode"] = market_selection_mode
    if mode:
        clean["mode"] = mode

    numeric_fields = (
        "bankroll_usdc",
        "max_capital_per_market",
        "free_usdc_buffer_pct",
        "max_position_usd",
        "target_min_distance",
        "target_max_distance",
    )
    for field in numeric_fields:
        if field in payload:
            try:
                clean[field] = float(payload[field])
            except Exception:
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "error": f"invalid numeric field: {field}"},
                )

    if "free_usdc_buffer_pct" in clean:
        if not (0.0 < clean["free_usdc_buffer_pct"] < 1.0):
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "free_usdc_buffer_pct must be between 0 and 1"},
            )

    if (
        "target_min_distance" in clean
        and "target_max_distance" in clean
        and clean["target_min_distance"] >= clean["target_max_distance"]
    ):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "target_min_distance must be < target_max_distance"},
        )

    current = get_runtime_overrides()
    current.update(clean)
    set_runtime_overrides(current)

    logger.info("Dashboard runtime overrides updated: %s", clean)
    return {
        "ok": True,
        "overrides": current,
        "updated_at": utc_iso(),
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Simple HTML dashboard that polls the JSON API."""
    return HTMLResponse(content=_DASHBOARD_HTML)


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Polymarket Reward Bot</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Courier New', monospace; background: #0d1117; color: #c9d1d9; }
    header { background: #161b22; padding: 1rem 2rem; border-bottom: 1px solid #30363d; display: flex; align-items: center; gap: 1rem; }
    header h1 { font-size: 1.2rem; color: #58a6ff; }
    .badge { padding: 0.2rem 0.6rem; border-radius: 999px; font-size: 0.75rem; font-weight: bold; }
    .RUNNING  { background: #1f6feb; color: #fff; }
    .SELECTING{ background: #388bfd30; color: #79c0ff; border: 1px solid #388bfd; }
    .COOLDOWN { background: #f0883e30; color: #f0883e; border: 1px solid #f0883e; }
    .UNHEDGED { background: #da363330; color: #f85149; border: 1px solid #da3633; }
    .PAUSED   { background: #30363d; color: #8b949e; }
    main { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; padding: 1.5rem; }
    .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1rem; }
    .card h2 { font-size: 0.85rem; text-transform: uppercase; color: #8b949e; margin-bottom: 0.75rem; letter-spacing: 0.05em; }
    .metric { display: flex; justify-content: space-between; gap: 1rem; padding: 0.3rem 0; border-bottom: 1px solid #21262d; }
    .metric:last-child { border-bottom: none; }
    .metric .label { color: #8b949e; font-size: 0.85rem; min-width: 120px; }
    .metric .value { color: #e6edf3; font-size: 0.85rem; font-weight: bold; text-align: right; word-break: break-word; }
    .value.good  { color: #3fb950; }
    .value.warn  { color: #f0883e; }
    .value.bad   { color: #f85149; }
    .log-box { font-size: 0.72rem; line-height: 1.5; height: 260px; overflow-y: auto; background: #0d1117; padding: 0.5rem; border-radius: 4px; color: #8b949e; }
    table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
    th { text-align: left; padding: 0.3rem 0.5rem; color: #8b949e; border-bottom: 1px solid #30363d; }
    td { padding: 0.3rem 0.5rem; border-bottom: 1px solid #21262d; }
    .full-width { grid-column: 1 / -1; }
    #last-update { font-size: 0.7rem; color: #8b949e; margin-left: auto; }
    .controls { display: grid; grid-template-columns: 180px 1fr auto auto auto; gap: 0.5rem; align-items: center; }
    .controls-2 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.5rem; align-items: center; }
    .controls-3 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.5rem; align-items: center; margin-top: 0.5rem; }
    select, input {
      width: 100%;
      background: #0d1117;
      color: #e6edf3;
      border: 1px solid #30363d;
      border-radius: 6px;
      padding: 0.65rem 0.75rem;
      font-family: inherit;
      font-size: 0.85rem;
    }
    button {
      border: 1px solid #30363d;
      background: #21262d;
      color: #e6edf3;
      border-radius: 6px;
      padding: 0.65rem 0.9rem;
      cursor: pointer;
      font-family: inherit;
      font-size: 0.82rem;
      font-weight: bold;
    }
    button:hover { background: #30363d; }
    .btn-primary { background: #1f6feb; border-color: #1f6feb; color: #fff; }
    .btn-warn { background: #f0883e22; border-color: #f0883e; color: #f0883e; }
    .btn-danger { background: #da363322; border-color: #da3633; color: #f85149; }
    .small-note { margin-top: 0.75rem; color: #8b949e; font-size: 0.75rem; }
    .status-line { margin-top: 0.75rem; font-size: 0.78rem; color: #8b949e; }
    .mono { font-family: inherit; word-break: break-word; }
  </style>
</head>
<body>
  <header>
    <h1>⚡ Polymarket Reward Bot</h1>
    <span id="state-badge" class="badge SELECTING">SELECTING</span>
    <span id="last-update">—</span>
  </header>

  <main>
    <div class="card full-width">
      <h2>Bot Controls</h2>
      <div class="controls">
        <select id="ref-type">
          <option value="url">url</option>
          <option value="slug">slug</option>
          <option value="condition_id">condition_id</option>
          <option value="market_id">market_id</option>
        </select>
        <input id="ref-value" type="text" placeholder="Paste market url / slug / condition_id / market_id"/>
        <button id="save-market-btn">Save Market</button>
        <button id="start-btn" class="btn-primary">Start Bot</button>
        <button id="pause-btn" class="btn-warn">Pause Bot</button>
      </div>
      <div class="controls" style="grid-template-columns: 1fr auto;">
        <div class="status-line mono" id="manual-market-status">Manual market: —</div>
        <button id="stop-btn" class="btn-danger">Stop Bot</button>
      </div>
      <div class="small-note">
        Save a manual market reference here when using MANUAL market selection.
      </div>
    </div>

    <div class="card full-width">
      <h2>Runtime Overrides</h2>

      <div class="controls-2">
        <select id="market-selection-mode">
          <option value="AUTO">AUTO market selection</option>
          <option value="MANUAL">MANUAL market selection</option>
        </select>

        <select id="quote-mode">
          <option value="AUTO">AUTO quote mode (use .env)</option>
          <option value="SINGLE_SIDED_SAFE">SINGLE_SIDED_SAFE</option>
          <option value="TWO_SIDED_BALANCED">TWO_SIDED_BALANCED</option>
        </select>

        <input id="bankroll-usdc" type="number" step="0.01" min="0" placeholder="bankroll_usdc"/>
        <input id="max-capital-per-market" type="number" step="0.01" min="0" placeholder="max_capital_per_market"/>
      </div>

      <div class="controls-3">
        <input id="free-usdc-buffer-pct" type="number" step="0.01" min="0.01" max="0.99" placeholder="free_usdc_buffer_pct"/>
        <input id="max-position-usd" type="number" step="0.01" min="0" placeholder="max_position_usd"/>
        <input id="target-min-distance" type="number" step="0.001" min="0.001" placeholder="target_min_distance"/>
        <input id="target-max-distance" type="number" step="0.001" min="0.001" placeholder="target_max_distance"/>
      </div>

      <div class="controls" style="grid-template-columns: 1fr auto auto;">
        <div class="status-line mono" id="runtime-override-status">Overrides: —</div>
        <button id="save-runtime-btn">Save Overrides</button>
        <button id="reload-runtime-btn">Reload View</button>
      </div>
    </div>

    <div class="card">
      <h2>Capital / Runtime</h2>
      <div class="metric"><span class="label">Wallet USDC</span><span class="value" id="cap-wallet">—</span></div>
      <div class="metric"><span class="label">Configured Bankroll</span><span class="value" id="cap-bankroll">—</span></div>
      <div class="metric"><span class="label">Usable Capital</span><span class="value good" id="cap-usable">—</span></div>
      <div class="metric"><span class="label">Committed Capital</span><span class="value warn" id="cap-committed">—</span></div>
      <div class="metric"><span class="label">Market Select</span><span class="value" id="cap-market-mode">—</span></div>
      <div class="metric"><span class="label">Quote Mode</span><span class="value" id="cap-quote-mode">—</span></div>
    </div>

    <div class="card">
      <h2>Current Market</h2>
      <div class="metric"><span class="label">Question</span><span class="value" id="m-question">—</span></div>
      <div class="metric"><span class="label">Market ID</span><span class="value mono" id="m-id">—</span></div>
      <div class="metric"><span class="label">Condition ID</span><span class="value mono" id="m-condition">—</span></div>
      <div class="metric"><span class="label">Midpoint</span><span class="value" id="m-mid">—</span></div>
      <div class="metric"><span class="label">Max Spread (v)</span><span class="value" id="m-v">—</span></div>
      <div class="metric"><span class="label">Min Size</span><span class="value" id="m-minsize">—</span></div>
    </div>

    <div class="card">
      <h2>Reward Estimate</h2>
      <div class="metric"><span class="label">Share Est.</span><span class="value good" id="r-share">—</span></div>
      <div class="metric"><span class="label">Daily Reward Est.</span><span class="value good" id="r-daily">—</span></div>
      <div class="metric"><span class="label">Reward/Capital</span><span class="value good" id="r-roc">—</span></div>
      <div class="metric"><span class="label">Capital Deployed</span><span class="value" id="r-capital">—</span></div>
    </div>

    <div class="card">
      <h2>Open Orders</h2>
      <table>
        <thead><tr><th>Side</th><th>Price</th><th>Size</th><th>Token</th></tr></thead>
        <tbody id="orders-body"><tr><td colspan="4" style="color:#8b949e">No orders</td></tr></tbody>
      </table>
    </div>

    <div class="card">
      <h2>Positions</h2>
      <table>
        <thead><tr><th>Token</th><th>Size</th><th>Avg Price</th><th>Notional</th></tr></thead>
        <tbody id="positions-body"><tr><td colspan="4" style="color:#8b949e">No positions</td></tr></tbody>
      </table>
    </div>

    <div class="card">
      <h2>Recent Fills</h2>
      <table>
        <thead><tr><th>Side</th><th>Price</th><th>Size</th><th>Time</th></tr></thead>
        <tbody id="fills-body"><tr><td colspan="4" style="color:#8b949e">No fills</td></tr></tbody>
      </table>
    </div>

    <div class="card">
      <h2>Log Tail</h2>
      <div class="log-box" id="log-box">—</div>
    </div>
  </main>

  <script>
    const $ = id => document.getElementById(id);

    async function postJson(url, payload) {
      const resp = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      return await resp.json();
    }

    function fmtShort(s, n = 12) {
      if (!s) return '—';
      return s.length > n ? s.slice(0, n) + '…' : s;
    }

    function fmtMoney(v) {
      if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
      return '$' + Number(v).toFixed(2);
    }

    function readNumberOrNull(id) {
      const raw = $(id).value.trim();
      if (!raw) return null;
      const n = Number(raw);
      return Number.isFinite(n) ? n : null;
    }

    async function saveManualMarket() {
      const refType = $('ref-type').value;
      const value = $('ref-value').value.trim();
      if (!value) {
        alert('Enter a market reference first.');
        return;
      }
      const res = await postJson('/api/manual-market', { ref_type: refType, value });
      if (!res.ok) {
        alert(res.error || 'Failed to save market reference.');
        return;
      }
      await refresh();
    }

    async function saveRuntimeOverrides() {
      const payload = {
        market_selection_mode: $('market-selection-mode').value,
        mode: $('quote-mode').value,
        bankroll_usdc: readNumberOrNull('bankroll-usdc'),
        max_capital_per_market: readNumberOrNull('max-capital-per-market'),
        free_usdc_buffer_pct: readNumberOrNull('free-usdc-buffer-pct'),
        max_position_usd: readNumberOrNull('max-position-usd'),
        target_min_distance: readNumberOrNull('target-min-distance'),
        target_max_distance: readNumberOrNull('target-max-distance'),
      };

      const res = await postJson('/api/runtime', payload);
      if (!res.ok) {
        alert(res.error || 'Failed to save runtime overrides.');
        return;
      }
      await refresh();
    }

    async function sendCommand(command) {
      const res = await postJson('/api/control', { command });
      if (!res.ok) {
        alert(res.error || ('Failed to send command ' + command));
        return;
      }
      await refresh();
    }

    async function refresh() {
      try {
        const [stateResp, controlResp, runtimeResp] = await Promise.all([
          fetch('/api/state'),
          fetch('/api/control'),
          fetch('/api/runtime'),
        ]);

        const d = await stateResp.json();
        const c = await controlResp.json();
        const rt = await runtimeResp.json();

        $('state-badge').textContent = d.state;
        $('state-badge').className = 'badge ' + d.state;
        $('last-update').textContent = 'Updated: ' + (d.last_updated || '—');

        const ref = c.manual_market_ref || {};
        const refKey = Object.keys(ref)[0];
        const refVal = refKey ? ref[refKey] : '';
        $('manual-market-status').textContent =
          refKey ? `Manual market: ${refKey} = ${refVal}` : 'Manual market: —';

        if (refKey && refVal) {
          $('ref-type').value = refKey;
          $('ref-value').value = refVal;
        }

        const overrides = rt.overrides || {};
        const status = rt.status || {};
        const effective = status.config_effective || {};

        $('runtime-override-status').textContent = 'Overrides: ' + JSON.stringify(overrides || {});

        if (overrides.market_selection_mode) $('market-selection-mode').value = overrides.market_selection_mode;
        else if (effective.market_selection_mode) $('market-selection-mode').value = effective.market_selection_mode;

        if (overrides.mode) $('quote-mode').value = overrides.mode;
        else $('quote-mode').value = 'AUTO';

        if (effective.bankroll_usdc !== undefined) $('bankroll-usdc').value = Number(effective.bankroll_usdc).toFixed(2);
        if (effective.max_capital_per_market !== undefined) $('max-capital-per-market').value = Number(effective.max_capital_per_market).toFixed(2);
        if (effective.free_usdc_buffer_pct !== undefined) $('free-usdc-buffer-pct').value = Number(effective.free_usdc_buffer_pct).toFixed(2);
        if (effective.max_position_usd !== undefined) $('max-position-usd').value = Number(effective.max_position_usd).toFixed(2);
        if (effective.target_min_distance !== undefined) $('target-min-distance').value = Number(effective.target_min_distance).toFixed(3);
        if (effective.target_max_distance !== undefined) $('target-max-distance').value = Number(effective.target_max_distance).toFixed(3);

        $('cap-wallet').textContent = fmtMoney(status.available_usdc);
        $('cap-bankroll').textContent = fmtMoney(effective.bankroll_usdc);
        $('cap-usable').textContent = fmtMoney(status.usable_capital);
        $('cap-committed').textContent = fmtMoney(status.committed_capital);
        $('cap-market-mode').textContent = effective.market_selection_mode || '—';
        $('cap-quote-mode').textContent = effective.mode || '—';

        const m = d.market;
        if (m) {
          $('m-question').textContent = m.question.length > 60 ? m.question.slice(0, 60) + '…' : m.question;
          $('m-id').textContent = m.market_id || '—';
          $('m-condition').textContent = m.condition_id || '—';
          $('m-mid').textContent = (typeof m.midpoint === 'number') ? m.midpoint.toFixed(4) : '—';
          $('m-v').textContent = m.reward_params_yes ? Number(m.reward_params_yes.max_incentive_spread).toFixed(4) : '—';
          $('m-minsize').textContent = m.reward_params_yes ? m.reward_params_yes.min_incentive_size : '—';

          $('r-share').textContent = ((m.share_est || 0) * 100).toFixed(2) + '%';
          $('r-daily').textContent = '$' + Number(m.daily_reward_est || 0).toFixed(3);
          $('r-roc').textContent = (Number(m.reward_per_capital || 0) * 100).toFixed(3) + '%/day';
          $('r-capital').textContent = '$' + Number(m.capital_required || 0).toFixed(2);
        } else {
          $('m-question').textContent = '—';
          $('m-id').textContent = '—';
          $('m-condition').textContent = '—';
          $('m-mid').textContent = '—';
          $('m-v').textContent = '—';
          $('m-minsize').textContent = '—';

          $('r-share').textContent = '—';
          $('r-daily').textContent = '—';
          $('r-roc').textContent = '—';
          $('r-capital').textContent = '—';
        }

        const orders = d.open_orders || [];
        if (orders.length) {
          $('orders-body').innerHTML = orders.map(o =>
            `<tr><td>${o.side}</td><td>${Number(o.price).toFixed(4)}</td><td>${Number(o.size_remaining).toFixed(2)}</td><td style="font-size:0.65rem">${fmtShort(o.token_id, 10)}</td></tr>`
          ).join('');
        } else {
          $('orders-body').innerHTML = '<tr><td colspan="4" style="color:#8b949e">No orders</td></tr>';
        }

        const positions = d.positions || [];
        if (positions.length) {
          $('positions-body').innerHTML = positions.map(p =>
            `<tr><td style="font-size:0.65rem">${fmtShort(p.token_id, 10)}</td><td>${Number(p.size).toFixed(2)}</td><td>${Number(p.avg_price).toFixed(4)}</td><td>$${(Number(p.size) * Number(p.avg_price)).toFixed(2)}</td></tr>`
          ).join('');
        } else {
          $('positions-body').innerHTML = '<tr><td colspan="4" style="color:#8b949e">No positions</td></tr>';
        }

        const fills = d.recent_fills || [];
        if (fills.length) {
          $('fills-body').innerHTML = fills.slice(-10).reverse().map(f =>
            `<tr><td>${f.side}</td><td>${Number(f.price).toFixed(4)}</td><td>${Number(f.size).toFixed(2)}</td><td style="font-size:0.65rem">${(f.timestamp || '').slice(11,19)}</td></tr>`
          ).join('');
        } else {
          $('fills-body').innerHTML = '<tr><td colspan="4" style="color:#8b949e">No fills</td></tr>';
        }

        const logs = d.log_tail || [];
        $('log-box').innerHTML = logs.slice().reverse().map(l => {
          const parsed = (() => { try { return JSON.parse(l); } catch { return {msg: l}; } })();
          const lvl = parsed.level || 'INFO';
          const color = lvl === 'ERROR' ? '#f85149' : lvl === 'WARNING' ? '#f0883e' : '#8b949e';
          return `<div style="color:${color}">${parsed.ts ? parsed.ts.slice(11,19) : ''} [${lvl}] ${parsed.msg || l}</div>`;
        }).join('');
      } catch (e) {
        console.error('Dashboard fetch error:', e);
      }
    }

    $('save-market-btn').addEventListener('click', () => saveManualMarket());
    $('save-runtime-btn').addEventListener('click', () => saveRuntimeOverrides());
    $('reload-runtime-btn').addEventListener('click', () => refresh());
    $('start-btn').addEventListener('click', () => sendCommand('START'));
    $('pause-btn').addEventListener('click', () => sendCommand('PAUSE'));
    $('stop-btn').addEventListener('click', () => sendCommand('STOP'));

    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
"""