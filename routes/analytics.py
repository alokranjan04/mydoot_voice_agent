# -*- coding: utf-8 -*-
"""
routes/analytics.py — Extraction quality analytics dashboard.

GET /analytics          → HTML dashboard
GET /analytics/data     → JSON aggregates (calls get_quality_analytics)
"""
import asyncio
import json

from aiohttp import web
from mydoot_functions import get_quality_analytics


# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Extraction Quality — MyDoot</title>
<style>
  :root {
    --primary:#6366f1; --primary-hover:#4f46e5;
    --bg:#f8fafc; --card:#ffffff;
    --text:#0f172a; --text-muted:#64748b;
    --border:#e2e8f0;
    --green:#16a34a; --yellow:#ca8a04; --red:#dc2626;
    --green-bg:#dcfce7; --yellow-bg:#fef9c3; --red-bg:#fee2e2;
  }
  *,*::before,*::after{box-sizing:border-box}
  body{font-family:'Inter',-apple-system,sans-serif;background:var(--bg);margin:0;padding:32px;color:var(--text)}
  .container{max-width:1380px;margin:0 auto}
  header{margin-bottom:32px;display:flex;align-items:center;justify-content:space-between}
  h1{margin:0;font-size:2rem;font-weight:800;background:linear-gradient(to right,#6366f1,#a855f7);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .sub{color:var(--text-muted);font-size:.9rem;margin-top:4px}
  .nav-links{display:flex;gap:16px;align-items:center}
  .back{color:var(--primary);text-decoration:none;font-size:.85rem;font-weight:600;padding:7px 16px;border:2px solid var(--primary);border-radius:8px}
  .back:hover{background:var(--primary);color:white}

  /* Stats row */
  .stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:16px;margin-bottom:28px}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:20px}
  .stat-label{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted);margin-bottom:6px}
  .stat-val{font-size:1.75rem;font-weight:800;color:var(--text)}
  .stat-sub{font-size:.78rem;color:var(--text-muted);margin-top:2px}

  /* Controls */
  .controls{display:flex;gap:12px;align-items:center;margin-bottom:24px}
  .controls label{font-size:.85rem;font-weight:600;color:var(--text-muted)}
  .controls select{padding:7px 12px;border:1px solid var(--border);border-radius:8px;font-size:.84rem;background:var(--card);cursor:pointer}
  .refresh-btn{background:var(--primary);color:white;border:none;border-radius:8px;padding:7px 16px;font-size:.82rem;font-weight:600;cursor:pointer}
  .refresh-btn:hover{background:var(--primary-hover)}

  /* Cards */
  .card{background:var(--card);border:1px solid var(--border);border-radius:20px;overflow:hidden;margin-bottom:24px}
  .card-header{padding:20px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
  .card-header h2{margin:0;font-size:1.05rem;font-weight:700}
  .card-header .hint{font-size:.78rem;color:var(--text-muted)}

  /* Tables */
  table{width:100%;border-collapse:collapse;font-size:.84rem}
  th{padding:11px 14px;text-align:left;font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted);background:#f8fafc;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:11px 14px;border-bottom:1px solid #f1f5f9;vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:#f5f3ff}

  /* Badges */
  .badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:.72rem;font-weight:700}
  .badge-green{background:var(--green-bg);color:var(--green)}
  .badge-yellow{background:var(--yellow-bg);color:var(--yellow)}
  .badge-red{background:var(--red-bg);color:var(--red)}

  /* Accuracy bar */
  .bar-wrap{display:flex;align-items:center;gap:8px}
  .bar-bg{flex:1;height:6px;background:#e2e8f0;border-radius:99px;overflow:hidden}
  .bar-fill{height:100%;border-radius:99px}
  .bar-pct{font-size:.78rem;font-weight:700;min-width:36px;text-align:right}

  /* 3-column grid for corrections */
  .corr-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:0;border-top:1px solid var(--border)}
  .corr-col{border-right:1px solid var(--border);padding:0}
  .corr-col:last-child{border-right:none}
  .corr-col h3{margin:0;padding:14px 18px;font-size:.82rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted);background:#f8fafc;border-bottom:1px solid var(--border)}
  .corr-col table{font-size:.8rem}
  .corr-col td,.corr-col th{padding:9px 14px}
  .corr-col .empty{padding:18px;color:var(--text-muted);font-size:.82rem;font-style:italic}

  /* Daily trend */
  .trend-rate{font-size:.8rem;font-weight:700}

  /* Empty / loading states */
  .empty-state{padding:48px;text-align:center;color:var(--text-muted)}
  .spinner{display:inline-block;width:20px;height:20px;border:3px solid #e2e8f0;border-top-color:var(--primary);border-radius:50%;animation:spin .6s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="container">

  <header>
    <div>
      <h1>Extraction Quality</h1>
      <div class="sub">First-pass accuracy &amp; correction rates by field</div>
    </div>
    <div class="nav-links">
      <a class="back" href="/latency">Latency</a>
      <a class="back" href="/calls">Calls</a>
      <a class="back" href="/">Home</a>
    </div>
  </header>

  <div class="controls">
    <label for="days">Window</label>
    <select id="days" onchange="load()">
      <option value="7">Last 7 days</option>
      <option value="30" selected>Last 30 days</option>
      <option value="90">Last 90 days</option>
    </select>
    <button class="refresh-btn" onclick="load()">Refresh</button>
    <span id="loading" style="display:none"><span class="spinner"></span></span>
  </div>

  <!-- Summary stats -->
  <div class="stats" id="stats-row">
    <div class="stat"><div class="stat-label">Total Records</div><div class="stat-val" id="s-total">—</div><div class="stat-sub">field extractions</div></div>
    <div class="stat"><div class="stat-label">Overall First-Pass</div><div class="stat-val" id="s-fp">—</div><div class="stat-sub">no correction needed</div></div>
    <div class="stat"><div class="stat-label">Highest Correction</div><div class="stat-val" id="s-worst-field">—</div><div class="stat-sub" id="s-worst-rate">correction rate</div></div>
    <div class="stat"><div class="stat-label">Total Corrections</div><div class="stat-val" id="s-corrections">—</div><div class="stat-sub">across all fields</div></div>
    <div class="stat"><div class="stat-label">ASR Confusions</div><div class="stat-val" id="s-confusions">—</div><div class="stat-sub">distinct heard→corrected pairs</div></div>
  </div>

  <!-- Field accuracy table -->
  <div class="card">
    <div class="card-header">
      <h2>Field Accuracy</h2>
      <span class="hint">Saved calls only &bull; sorted worst-first</span>
    </div>
    <div id="accuracy-table"><div class="empty-state">Loading…</div></div>
  </div>

  <!-- Top corrections per field -->
  <div class="card">
    <div class="card-header">
      <h2>Top Corrections by Field</h2>
      <span class="hint">First extracted → final confirmed value</span>
    </div>
    <div class="corr-grid" id="corr-grid">
      <div class="empty-state">Loading…</div>
    </div>
  </div>

  <!-- ASR confusion pairs -->
  <div class="card">
    <div class="card-header">
      <h2>ASR Confusion Pairs</h2>
      <span class="hint">Systematic misrecognitions across all fields — top 30</span>
    </div>
    <div id="confusion-table"><div class="empty-state">Loading…</div></div>
  </div>

  <!-- Daily trend -->
  <div class="card">
    <div class="card-header">
      <h2>Daily Correction Trend</h2>
      <span class="hint">Last 14 days, per field</span>
    </div>
    <div id="trend-table"><div class="empty-state">Loading…</div></div>
  </div>

</div><!-- .container -->

<script>
const FIELD_ORDER = ['brand','address','customer_name','preferred_time','category','subcategory','issue_type','model'];

function pctBadge(v) {
  if (v === null || v === undefined) return '—';
  const cls = v >= 90 ? 'badge-green' : v >= 70 ? 'badge-yellow' : 'badge-red';
  return `<span class="badge ${cls}">${v}%</span>`;
}

function barHtml(pct) {
  if (pct === null || pct === undefined) return '—';
  const color = pct >= 90 ? '#16a34a' : pct >= 70 ? '#ca8a04' : '#dc2626';
  return `<div class="bar-wrap">
    <div class="bar-bg"><div class="bar-fill" style="width:${pct}%;background:${color}"></div></div>
    <span class="bar-pct" style="color:${color}">${pct}%</span>
  </div>`;
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function trunc(s, n=40) {
  s = String(s);
  return s.length > n ? `<span title="${esc(s)}">${esc(s.slice(0,n))}…</span>` : esc(s);
}

async function load() {
  const days = document.getElementById('days').value;
  document.getElementById('loading').style.display = '';
  let d;
  try {
    const r = await fetch(`/analytics/data?days=${days}`);
    d = await r.json();
  } catch(e) {
    document.getElementById('accuracy-table').innerHTML =
      '<div class="empty-state">Failed to load data.</div>';
    document.getElementById('loading').style.display = 'none';
    return;
  }
  document.getElementById('loading').style.display = 'none';

  if (!d || !d.total_records) {
    const msg = '<div class="empty-state">No extraction quality data yet. Data populates after calls complete with PostgreSQL configured.</div>';
    ['accuracy-table','corr-grid','confusion-table','trend-table'].forEach(id =>
      document.getElementById(id).innerHTML = msg);
    document.getElementById('s-total').textContent = '0';
    return;
  }

  // ── Stats row ──────────────────────────────────────────────────────────────
  const fa = d.field_accuracy || [];
  document.getElementById('s-total').textContent = d.total_records.toLocaleString();
  const totalFirst = fa.reduce((a,r) => a + (r.first_pass||0), 0);
  const totalAll   = fa.reduce((a,r) => a + (r.total||0), 0);
  document.getElementById('s-fp').textContent =
    totalAll ? (100*totalFirst/totalAll).toFixed(1)+'%' : '—';
  const worst = fa[0];  // sorted worst-first
  document.getElementById('s-worst-field').textContent = worst ? worst.field : '—';
  document.getElementById('s-worst-rate').textContent =
    worst ? (worst.correction_rate_pct||0)+'% correction rate' : '';
  const totalCorr = fa.reduce((a,r) => a + (r.corrected||0), 0);
  document.getElementById('s-corrections').textContent = totalCorr.toLocaleString();
  document.getElementById('s-confusions').textContent =
    (d.confusion_pairs||[]).length.toLocaleString();

  // ── Field accuracy table ───────────────────────────────────────────────────
  let rows = '';
  for (const r of fa) {
    rows += `<tr>
      <td><strong>${esc(r.field)}</strong></td>
      <td>${(r.total||0).toLocaleString()}</td>
      <td>${barHtml(r.first_pass_pct)}</td>
      <td>${pctBadge(r.correction_rate_pct)}</td>
      <td>${r.avg_attempts ?? '—'}</td>
      <td>${r.avg_confidence != null ? parseFloat(r.avg_confidence).toFixed(3) : '—'}</td>
    </tr>`;
  }
  document.getElementById('accuracy-table').innerHTML = rows
    ? `<table><thead><tr>
        <th>Field</th><th>Total</th>
        <th>First-pass accuracy</th><th>Correction rate</th>
        <th>Avg attempts</th><th>Avg confidence</th>
       </tr></thead><tbody>${rows}</tbody></table>`
    : '<div class="empty-state">No data for this window.</div>';

  // ── Top corrections per field ──────────────────────────────────────────────
  const tc = d.top_corrections || {};
  const fields = FIELD_ORDER.filter(f => tc[f] && tc[f].length);
  let cols = '';
  if (fields.length === 0) {
    cols = '<div class="empty-state" style="grid-column:1/-1">No corrections recorded in this window.</div>';
  } else {
    for (const fld of fields) {
      let trows = '';
      for (const c of tc[fld]) {
        trows += `<tr><td>${trunc(c.heard_as)}</td><td>→</td><td>${trunc(c.corrected_to)}</td><td><strong>${c.count}</strong></td></tr>`;
      }
      cols += `<div class="corr-col">
        <h3>${esc(fld)}</h3>
        <table><thead><tr><th>Heard As</th><th></th><th>Corrected To</th><th>#</th></tr></thead>
        <tbody>${trows}</tbody></table>
      </div>`;
    }
  }
  document.getElementById('corr-grid').innerHTML = cols;

  // ── ASR confusion pairs ────────────────────────────────────────────────────
  const cp = d.confusion_pairs || [];
  if (cp.length === 0) {
    document.getElementById('confusion-table').innerHTML =
      '<div class="empty-state">No confusion pairs recorded.</div>';
  } else {
    let crows = '';
    for (const p of cp) {
      crows += `<tr>
        <td><span class="badge badge-yellow">${esc(p.field)}</span></td>
        <td>${trunc(p.heard_as, 50)}</td>
        <td style="color:#94a3b8">→</td>
        <td>${trunc(p.corrected_to, 50)}</td>
        <td><strong>${p.count}</strong></td>
      </tr>`;
    }
    document.getElementById('confusion-table').innerHTML =
      `<table><thead><tr><th>Field</th><th>Heard As</th><th></th><th>Corrected To</th><th>Count</th></tr></thead>
       <tbody>${crows}</tbody></table>`;
  }

  // ── Daily trend ────────────────────────────────────────────────────────────
  const dt = d.daily_trend || [];
  if (dt.length === 0) {
    document.getElementById('trend-table').innerHTML =
      '<div class="empty-state">No trend data available.</div>';
  } else {
    let trows = '';
    for (const row of dt) {
      const rate = row.total ? (100*row.corrected/row.total).toFixed(0) : 0;
      const cls  = rate >= 30 ? 'badge-red' : rate >= 10 ? 'badge-yellow' : 'badge-green';
      trows += `<tr>
        <td>${esc(row.date)}</td>
        <td>${esc(row.field)}</td>
        <td>${row.total}</td>
        <td>${row.corrected}</td>
        <td><span class="badge ${cls}">${rate}%</span></td>
      </tr>`;
    }
    document.getElementById('trend-table').innerHTML =
      `<table><thead><tr><th>Date</th><th>Field</th><th>Total</th><th>Corrected</th><th>Correction Rate</th></tr></thead>
       <tbody>${trows}</tbody></table>`;
  }
}

load();
</script>
</body>
</html>
"""


# ── Handlers ──────────────────────────────────────────────────────────────────

async def analytics_page(request: web.Request) -> web.Response:
    return web.Response(text=_HTML, content_type="text/html")


async def analytics_data(request: web.Request) -> web.Response:
    days = int(request.rel_url.query.get("days", "30"))
    days = max(1, min(days, 365))
    data = await asyncio.get_event_loop().run_in_executor(
        None, get_quality_analytics, days
    )
    return web.Response(
        text=json.dumps(data, default=str),
        content_type="application/json",
    )
