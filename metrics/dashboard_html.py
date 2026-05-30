# -*- coding: utf-8 -*-
"""
Metrics dashboard HTML — served at GET /metrics.
Fetches /metrics/data via JS and renders side-by-side provider comparison.
Supports per-row checkbox selection → filtered KPIs + charts.
Uses Chart.js 4.4 from CDN. No server-side templating.
"""

METRICS_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice Agent — Metrics Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f0f2f5; color: #1a1a1a; margin: 0; padding: 20px;
  }
  .header {
    display: flex; align-items: center; gap: 16px;
    margin-bottom: 20px; flex-wrap: wrap;
  }
  .header h1 { margin: 0; font-size: 1.5rem; }
  .header .sub { color: #666; font-size: .85rem; flex: 1; }
  .btn-refresh {
    padding: 8px 20px; border: 1px solid #ccc; border-radius: 8px;
    background: white; cursor: pointer; font-size: .9rem;
    transition: background .15s;
  }
  .btn-refresh:hover { background: #e8eaf6; }
  .btn-home {
    padding: 8px 16px; border: 1px solid #1565c0; border-radius: 8px;
    background: white; color: #1565c0; cursor: pointer; font-size: .9rem;
    text-decoration: none;
  }

  /* ── Two-column provider grid ── */
  .providers { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  @media (max-width: 900px) { .providers { grid-template-columns: 1fr; } }

  .card {
    background: white; border-radius: 12px; padding: 22px;
    box-shadow: 0 2px 8px rgba(0,0,0,.07);
    border-top: 4px solid transparent;
  }
  .card.sarvam { border-top-color: #2e7d32; }
  .card.google  { border-top-color: #1565c0; }
  .card h2 { margin: 0 0 4px; font-size: 1.1rem; }
  .card .pipeline-desc { color: #666; font-size: .78rem; margin-bottom: 14px; }

  /* ── Filter banner ── */
  .filter-banner {
    display: none; align-items: center; gap: 10px;
    background: #fff8e1; border: 1px solid #ffc107; border-radius: 8px;
    padding: 7px 12px; margin-bottom: 12px; font-size: .82rem; color: #5d4037;
  }
  .filter-banner span { flex: 1; }
  .btn-clear {
    border: none; background: transparent; color: #795548;
    cursor: pointer; font-size: .8rem; padding: 2px 8px;
    border: 1px solid #bcaaa4; border-radius: 5px;
  }
  .btn-clear:hover { background: #efebe9; }

  /* ── KPI grid ── */
  .kpi-grid {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
    margin-bottom: 18px;
  }
  .kpi {
    background: #f8f9fa; border-radius: 8px; padding: 10px 12px;
    text-align: center;
  }
  .kpi-val { font-size: 1.35rem; font-weight: 700; line-height: 1.2; }
  .kpi-lbl { font-size: .7rem; color: #777; margin-top: 2px; }
  .ok   { color: #2e7d32; }
  .warn { color: #e65100; }
  .fail { color: #c62828; }

  /* ── Charts ── */
  .chart-wrap { margin-top: 14px; }
  .chart-wrap h3 { margin: 0 0 8px; font-size: .9rem; color: #444; }
  canvas { max-height: 200px; }

  /* ── Recent calls table ── */
  .table-wrap { margin-top: 16px; overflow-x: auto; }
  .table-wrap h3 { margin: 0 0 6px; font-size: .9rem; color: #444; }
  table { width: 100%; border-collapse: collapse; font-size: .78rem; }
  th, td { padding: 5px 8px; text-align: left; border-bottom: 1px solid #eee; }
  th { background: #f0f2f5; font-weight: 600; white-space: nowrap; }
  td.booked-yes { color: #2e7d32; font-weight: 600; }
  td.booked-no  { color: #999; }
  td.rec-cell   { min-width: 170px; vertical-align: middle; }
  th.chk-col, td.chk-col { width: 28px; text-align: center; padding: 4px 6px; }
  input[type=checkbox] { cursor: pointer; width: 14px; height: 14px; }

  /* ── Bottom full-width row ── */
  .bottom-row { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 20px; }
  @media (max-width: 900px) { .bottom-row { grid-template-columns: 1fr; } }
  .bottom-card {
    background: white; border-radius: 12px; padding: 22px;
    box-shadow: 0 2px 8px rgba(0,0,0,.07);
  }
  .bottom-card h3 { margin: 0 0 10px; font-size: .95rem; }

  .cost-full {
    background: white; border-radius: 12px; padding: 22px;
    box-shadow: 0 2px 8px rgba(0,0,0,.07); margin-top: 20px;
  }
  .cost-full h3 { margin: 0 0 10px; font-size: .95rem; }

  .no-data { color: #aaa; font-size: .85rem; text-align: center; padding: 20px 0; }

  .threshold-legend {
    font-size: .72rem; color: #888; margin-top: 4px;
    display: flex; gap: 12px; flex-wrap: wrap;
  }
  .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 3px; }
  .dot-ok   { background: #43a047; }
  .dot-warn { background: #ef6c00; }
  .dot-fail { background: #e53935; }

  .updated { font-size: .72rem; color: #aaa; }
</style>
</head>
<body>

<div class="header">
  <h1>Voice Agent — Metrics Dashboard</h1>
  <span class="sub">Neha Child Care · Real-time pipeline comparison</span>
  <a href="/" class="btn-home">← Dashboard</a>
  <button class="btn-refresh" onclick="loadData()">↻ Refresh</button>
  <span class="updated" id="updated"></span>
</div>

<!-- ── Side-by-side provider cards ── -->
<div class="providers">

  <div class="card sarvam" id="card-sarvam">
    <h2 style="color:#2e7d32">Sarvam AI Pipeline</h2>
    <div class="pipeline-desc">Deepgram STT → Sarvam 30B LLM → Sarvam TTS (Bulbul v2)</div>
    <div class="filter-banner" id="filter-banner-sarvam">
      <span id="filter-label-sarvam">📊 Filtered metrics</span>
      <button class="btn-clear" onclick="clearSelection('sarvam')">✕ Clear filter</button>
    </div>
    <div class="kpi-grid" id="kpis-sarvam"><div class="no-data">No data yet</div></div>
    <div class="chart-wrap">
      <h3>Latency Breakdown — P50 / P95 (ms)</h3>
      <div class="threshold-legend">
        <span><span class="dot dot-ok"></span>&lt; threshold</span>
        <span><span class="dot dot-warn"></span>1–1.5× threshold</span>
        <span><span class="dot dot-fail"></span>&gt; 1.5× threshold</span>
      </div>
      <canvas id="chart-lat-sarvam"></canvas>
    </div>
    <div class="table-wrap">
      <h3>Recent Calls (last 10) — check rows to filter metrics</h3>
      <div id="table-sarvam"><div class="no-data">No calls recorded yet</div></div>
    </div>
  </div>

  <div class="card google" id="card-google">
    <h2 style="color:#1565c0">Google Gemini Pipeline</h2>
    <div class="pipeline-desc">Gemini Multimodal Live API — native end-to-end audio</div>
    <div class="filter-banner" id="filter-banner-google">
      <span id="filter-label-google">📊 Filtered metrics</span>
      <button class="btn-clear" onclick="clearSelection('google')">✕ Clear filter</button>
    </div>
    <div class="kpi-grid" id="kpis-google"><div class="no-data">No data yet</div></div>
    <div class="chart-wrap">
      <h3>E2E Latency — P50 / P95 (ms)</h3>
      <div class="threshold-legend">
        <span><span class="dot dot-ok"></span>&lt; 300 ms target</span>
        <span><span class="dot dot-warn"></span>300–450 ms</span>
        <span><span class="dot dot-fail"></span>&gt; 450 ms</span>
      </div>
      <canvas id="chart-lat-google"></canvas>
    </div>
    <div class="table-wrap">
      <h3>Recent Calls (last 10) — check rows to filter metrics</h3>
      <div id="table-google"><div class="no-data">No calls recorded yet</div></div>
    </div>
  </div>

</div>

<!-- ── Deepgram STT efficiency row ── -->
<div class="cost-full" id="dg-section" style="margin-top:20px">
  <h3>Deepgram STT Efficiency (Sarvam Pipeline)</h3>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start">
    <div>
      <canvas id="chart-dg-lat" style="max-height:180px"></canvas>
      <div class="threshold-legend" style="margin-top:6px">
        <span><span class="dot dot-ok"></span>&lt; 300 ms target</span>
        <span><span class="dot dot-warn"></span>300–450 ms</span>
        <span><span class="dot dot-fail"></span>&gt; 450 ms</span>
      </div>
    </div>
    <div>
      <canvas id="chart-dg-conf" style="max-height:180px"></canvas>
      <div class="threshold-legend" style="margin-top:6px">
        <span><span class="dot dot-ok"></span>≥ 90%</span>
        <span><span class="dot dot-warn"></span>75–90%</span>
        <span><span class="dot dot-fail"></span>&lt; 75%</span>
      </div>
    </div>
  </div>
</div>

<!-- ── Bottom row: CPU / Memory ── -->
<div class="bottom-row">
  <div class="bottom-card">
    <h3>Avg CPU % per Call</h3>
    <canvas id="chart-cpu"></canvas>
  </div>
  <div class="bottom-card">
    <h3>Peak Memory RSS (MB)</h3>
    <canvas id="chart-mem"></canvas>
  </div>
</div>

<!-- ── Cost breakdown ── -->
<div class="cost-full">
  <h3>Avg Cost per Call (USD) — Provider Comparison</h3>
  <canvas id="chart-cost" style="max-height:180px"></canvas>
</div>

<!-- ─────────────────────────── JavaScript ─────────────────────────────── -->
<script>
const THRESHOLDS = { stt_ms: 300, llm_ms: 400, tts_ms: 300, e2e_ms: 800 };
const charts = {};

// ── Global state ─────────────────────────────────────────────────────────────
let _rawData   = null;                          // full /metrics/data response
let _allCalls  = { sarvam: [], google: [] };   // calls in display order (newest first)
let _selected  = { sarvam: new Set(), google: new Set() };

// ── Chart helpers ─────────────────────────────────────────────────────────────
function destroyChart(id) {
  if (charts[id]) { charts[id].destroy(); delete charts[id]; }
}

function latColour(val, threshold, alpha) {
  if (val === null || val === undefined) return `rgba(200,200,200,${alpha})`;
  if (val < threshold)           return `rgba(67,160,71,${alpha})`;
  if (val < threshold * 1.5)     return `rgba(239,108,0,${alpha})`;
  return `rgba(229,57,53,${alpha})`;
}

function kpiColourClass(val, threshold) {
  if (val === null || val === undefined) return '';
  if (val < threshold)       return 'ok';
  if (val < threshold * 1.5) return 'warn';
  return 'fail';
}

function kpiBox(label, value, cls = '', suffix = '') {
  return `<div class="kpi">
    <div class="kpi-val ${cls}">${value !== null && value !== undefined ? value + suffix : '—'}</div>
    <div class="kpi-lbl">${label}</div>
  </div>`;
}

// ── Client-side stats from a filtered call array ──────────────────────────────
function computeStats(calls, provider) {
  if (!calls || calls.length === 0) return { call_count: 0 };
  const n = calls.length;
  const bookings = calls.filter(c => c.booking);
  const costs    = calls.map(c => c.cost_usd || 0).filter(v => v > 0);

  function avg(arr) {
    const clean = arr.filter(v => v != null && !isNaN(v));
    return clean.length
      ? Math.round(clean.reduce((a, b) => a + b, 0) / clean.length * 100) / 100
      : null;
  }

  function pct(arr, p) {
    const vals = arr.filter(v => v != null).sort((a, b) => a - b);
    if (!vals.length) return null;
    return Math.round(vals[Math.min(Math.floor(vals.length * p / 100), vals.length - 1)]);
  }

  const allTurns = calls.flatMap(c => c.turn_latencies || []);
  const lat = {
    stt:  { p50: pct(allTurns.map(t => t.stt_ms),  50), p95: pct(allTurns.map(t => t.stt_ms),  95) },
    llm:  { p50: pct(allTurns.map(t => t.llm_ms),  50), p95: pct(allTurns.map(t => t.llm_ms),  95) },
    tts:  { p50: pct(allTurns.map(t => t.tts_ms),  50), p95: pct(allTurns.map(t => t.tts_ms),  95) },
    e2e:  { p50: pct(allTurns.map(t => t.e2e_ms),  50), p95: pct(allTurns.map(t => t.e2e_ms),  95) },
    tool: { p50: pct(allTurns.map(t => t.tool_ms), 50), p95: pct(allTurns.map(t => t.tool_ms), 95) },
  };

  const allConfs = calls.flatMap(c => c.dg_confidences || []);
  const avg_dg_confidence = allConfs.length
    ? Math.round(allConfs.reduce((a, b) => a + b, 0) / allConfs.length * 10) / 10
    : null;

  return {
    call_count:            n,
    booking_count:         bookings.length,
    booking_rate_pct:      n ? Math.round(100 * bookings.length / n) : 0,
    avg_duration_s:        avg(calls.map(c => c.duration_s)),
    avg_turns:             avg(calls.map(c => c.turns)),
    avg_interrupts:        avg(calls.map(c => c.interrupts || 0)),
    avg_hallucins:         avg(calls.map(c => c.hallucinations || 0)),
    avg_cost_usd:          avg(costs),
    cost_per_booking:      bookings.length ? avg(bookings.map(c => c.cost_usd || 0)) : null,
    avg_first_response_ms: avg(calls.map(c => c.first_response_ms)),
    avg_dg_confidence,
    latency: lat,
  };
}

// ── Call log table (with checkboxes) ─────────────────────────────────────────
function buildTable(calls, provider) {
  if (!calls || calls.length === 0) return '<div class="no-data">No calls yet</div>';
  const allChk = `<input type="checkbox" id="chk-all-${provider}"
    title="Select all" onchange="toggleAll('${provider}', this.checked)">`;
  const rows = calls.map((c, idx) => {
    const chk  = `<input type="checkbox" class="row-chk chk-${provider}" data-idx="${idx}"
      onchange="onCheckChange('${provider}')">`;
    const ts     = c.ts ? new Date(c.ts * 1000).toLocaleTimeString() : '—';
    const booked = c.booking
      ? '<td class="booked-yes">✓ Booked</td>'
      : '<td class="booked-no">—</td>';
    const fr  = c.first_response_ms ? `${c.first_response_ms}ms` : '—';
    const e2e = c.avg_e2e_ms        ? `${c.avg_e2e_ms}ms`        : '—';
    const recCell = c.recording
      ? `<td class="rec-cell">
           <audio controls preload="none" style="height:28px;width:160px;">
             <source src="/recordings/${c.recording}" type="audio/wav">
           </audio>
         </td>`
      : '<td class="rec-cell" style="color:#aaa">—</td>';
    return `<tr>
      <td class="chk-col">${chk}</td>
      <td>${ts}</td>
      <td>${c.caller || '?'}</td>
      <td>${c.duration_s}s</td>
      <td>${c.turns}</td>
      ${booked}
      <td>$${c.cost_usd}</td>
      <td>${fr}</td>
      <td>${e2e}</td>
      <td>${c.interrupts}</td>
      ${recCell}
    </tr>`;
  }).join('');
  return `<table>
    <thead><tr>
      <th class="chk-col">${allChk}</th>
      <th>Time</th><th>Caller</th><th>Dur</th><th>Turns</th>
      <th>Outcome</th><th>Cost</th><th>1st Resp</th><th>Avg E2E</th><th>Interrupts</th>
      <th>Recording</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

// ── Checkbox interaction ──────────────────────────────────────────────────────
function onCheckChange(provider) {
  const checked = [...document.querySelectorAll(`.chk-${provider}:checked`)]
    .map(el => +el.dataset.idx);
  _selected[provider] = new Set(checked);

  const allCalls = _allCalls[provider];
  const filtered = checked.length > 0 ? checked.map(i => allCalls[i]) : allCalls;
  const stats = checked.length > 0 ? computeStats(filtered, provider) : _rawData?.[provider];

  renderKPIs(`kpis-${provider}`, stats, provider);
  if (provider === 'sarvam') {
    buildSarvamLatChart(stats?.latency);
    buildDGLatChart(stats?.latency);
    buildDGConfChart(stats);
  } else {
    buildGoogleLatChart(stats?.latency);
  }

  // Update filter banner
  const banner = document.getElementById(`filter-banner-${provider}`);
  const label  = document.getElementById(`filter-label-${provider}`);
  if (checked.length > 0) {
    banner.style.display = 'flex';
    label.textContent = `📊 Showing metrics for ${checked.length} selected call${checked.length > 1 ? 's' : ''}`;
  } else {
    banner.style.display = 'none';
  }

  // Sync select-all checkbox state
  const allBox = document.getElementById(`chk-all-${provider}`);
  if (allBox) allBox.checked = checked.length === allCalls.length && allCalls.length > 0;
}

function toggleAll(provider, checked) {
  document.querySelectorAll(`.chk-${provider}`).forEach(el => el.checked = checked);
  onCheckChange(provider);
}

function clearSelection(provider) {
  document.querySelectorAll(`.chk-${provider}`).forEach(el => el.checked = false);
  const allBox = document.getElementById(`chk-all-${provider}`);
  if (allBox) allBox.checked = false;
  _selected[provider] = new Set();

  const full = _rawData?.[provider];
  renderKPIs(`kpis-${provider}`, full, provider);
  if (provider === 'sarvam') {
    buildSarvamLatChart(full?.latency);
    buildDGLatChart(full?.latency);
    buildDGConfChart(full);
  } else {
    buildGoogleLatChart(full?.latency);
  }
  document.getElementById(`filter-banner-${provider}`).style.display = 'none';
}

// ── Sarvam latency bar chart ──────────────────────────────────────────────────
function buildSarvamLatChart(lat) {
  destroyChart('lat-sarvam');
  if (!lat) return;
  const stages = ['STT', 'LLM', 'TTS', 'E2E', 'Tool'];
  const keys   = ['stt', 'llm', 'tts', 'e2e', 'tool'];
  const thresh = [300,    400,   300,   800,   2000];
  const p50 = keys.map(k => lat[k]?.p50 ?? null);
  const p95 = keys.map(k => lat[k]?.p95 ?? null);
  const ctx = document.getElementById('chart-lat-sarvam').getContext('2d');
  charts['lat-sarvam'] = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: stages,
      datasets: [
        { label: 'P50', data: p50,
          backgroundColor: p50.map((v,i) => latColour(v, thresh[i], 0.8)),
          borderColor:     p50.map((v,i) => latColour(v, thresh[i], 1)), borderWidth: 1 },
        { label: 'P95', data: p95,
          backgroundColor: p95.map((v,i) => latColour(v, thresh[i], 0.4)),
          borderColor:     p95.map((v,i) => latColour(v, thresh[i], 0.8)),
          borderWidth: 1, borderDash: [4, 2] },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: { legend: { position: 'top' } },
      scales: { y: { beginAtZero: true, title: { display: true, text: 'ms' } } }
    }
  });
}

// ── Google E2E latency bar chart ──────────────────────────────────────────────
function buildGoogleLatChart(lat) {
  destroyChart('lat-google');
  if (!lat) return;
  const e2e  = lat.e2e  || {};
  const tool = lat.tool || {};
  const labels = ['E2E P50', 'E2E P95', 'Tool P50', 'Tool P95'];
  const vals   = [e2e.p50, e2e.p95, tool.p50, tool.p95];
  const thresh = [300, 300, 2000, 2000];
  const ctx = document.getElementById('chart-lat-google').getContext('2d');
  charts['lat-google'] = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'ms', data: vals,
        backgroundColor: vals.map((v,i) => latColour(v, thresh[i], 0.75)),
        borderColor:     vals.map((v,i) => latColour(v, thresh[i], 1)),
        borderWidth: 1,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, title: { display: true, text: 'ms' } } }
    }
  });
}

// ── Deepgram STT latency chart ────────────────────────────────────────────────
function buildDGLatChart(lat) {
  destroyChart('dg-lat');
  const stt  = lat?.stt || {};
  const vals   = [stt.p50 ?? null, stt.p95 ?? null];
  const labels = ['STT P50', 'STT P95'];
  const thresh = [300, 300];
  const ctx = document.getElementById('chart-dg-lat').getContext('2d');
  charts['dg-lat'] = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Deepgram STT Latency (ms)', data: vals,
        backgroundColor: vals.map((v,i) => latColour(v, thresh[i], 0.8)),
        borderColor:     vals.map((v,i) => latColour(v, thresh[i], 1)),
        borderWidth: 1,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: { legend: { display: false },
                 title: { display: true, text: 'STT Latency — last chunk → final transcript' } },
      scales: { y: { beginAtZero: true, title: { display: true, text: 'ms' } } }
    }
  });
}

// ── Deepgram confidence chart ─────────────────────────────────────────────────
function buildDGConfChart(sarvam) {
  destroyChart('dg-conf');
  const conf = sarvam?.avg_dg_confidence ?? null;
  const colour = conf === null ? 'rgba(200,200,200,.75)'
               : conf >= 90   ? 'rgba(67,160,71,.8)'
               : conf >= 75   ? 'rgba(239,108,0,.8)'
               :                'rgba(229,57,53,.8)';
  const ctx = document.getElementById('chart-dg-conf').getContext('2d');
  charts['dg-conf'] = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ['Avg Recognition Confidence'],
      datasets: [{
        label: 'Confidence %', data: [conf ?? 0],
        backgroundColor: [colour], borderColor: [colour], borderWidth: 1,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: { legend: { display: false },
                 title: { display: true, text: 'Deepgram Recognition Confidence' } },
      scales: { y: { beginAtZero: true, max: 100, title: { display: true, text: '%' } } }
    }
  });
}

// ── CPU / Memory charts ───────────────────────────────────────────────────────
function buildResourceCharts(sarvam, google) {
  const providers = ['Sarvam', 'Google'];
  const cpuVals   = [sarvam?.avg_cpu_pct ?? 0, google?.avg_cpu_pct ?? 0];
  const memVals   = [sarvam?.peak_mem_mb ?? 0, google?.peak_mem_mb ?? 0];
  const colours   = ['rgba(46,125,50,.75)', 'rgba(21,101,192,.75)'];

  destroyChart('cpu');
  charts['cpu'] = new Chart(document.getElementById('chart-cpu').getContext('2d'), {
    type: 'bar',
    data: { labels: providers,
            datasets: [{ label: 'Avg CPU %', data: cpuVals,
                         backgroundColor: colours, borderColor: colours, borderWidth: 1 }] },
    options: { responsive: true, plugins: { legend: { display: false } },
               scales: { y: { beginAtZero: true, max: 100 } } }
  });

  destroyChart('mem');
  charts['mem'] = new Chart(document.getElementById('chart-mem').getContext('2d'), {
    type: 'bar',
    data: { labels: providers,
            datasets: [{ label: 'Peak MB', data: memVals,
                         backgroundColor: colours, borderColor: colours, borderWidth: 1 }] },
    options: { responsive: true, plugins: { legend: { display: false } },
               scales: { y: { beginAtZero: true } } }
  });
}

// ── Cost breakdown chart ──────────────────────────────────────────────────────
function buildCostChart(sarvam, google) {
  destroyChart('cost');
  const sb = sarvam?.cost_breakdown || {};
  const gb = google?.cost_breakdown || {};
  charts['cost'] = new Chart(document.getElementById('chart-cost').getContext('2d'), {
    type: 'bar',
    data: {
      labels: ['Sarvam', 'Google'],
      datasets: [
        { label: 'STT',  data: [sb.stt_usd ?? 0, 0],                    backgroundColor: 'rgba(66,165,245,.85)' },
        { label: 'LLM',  data: [sb.llm_usd ?? 0, gb.llm_usd ?? 0],      backgroundColor: 'rgba(102,187,106,.85)' },
        { label: 'TTS',  data: [sb.tts_usd ?? 0, 0],                    backgroundColor: 'rgba(255,167,38,.85)' },
      ]
    },
    options: {
      responsive: true,
      plugins: { legend: { position: 'top' } },
      scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true,
                title: { display: true, text: 'USD / call' } } }
    }
  });
}

// ── KPI grid renderer ─────────────────────────────────────────────────────────
function renderKPIs(containerId, stats, provider) {
  const el = document.getElementById(containerId);
  if (!stats || stats.call_count === 0) {
    el.innerHTML = '<div class="no-data">No calls recorded yet</div>';
    return;
  }
  const lat  = stats.latency || {};
  const e2e  = lat.e2e?.p50;
  const sttP50 = lat.stt?.p50;
  const conf   = stats.avg_dg_confidence;

  function confClass(v) {
    if (v === null || v === undefined) return '';
    return v >= 90 ? 'ok' : v >= 75 ? 'warn' : 'fail';
  }

  const fr   = stats.avg_first_response_ms;
  const boxes = [
    kpiBox('Total Calls',    stats.call_count),
    kpiBox('Bookings',       `${stats.booking_count} (${stats.booking_rate_pct}%)`),
    kpiBox('Avg Duration',   `${stats.avg_duration_s}s`),
    kpiBox('Avg Turns',      stats.avg_turns),
    kpiBox('Interruptions',  stats.avg_interrupts),
    kpiBox('Hallucinations', stats.avg_hallucins, stats.avg_hallucins > 0 ? 'warn' : 'ok'),
    kpiBox('Avg Cost',       `$${stats.avg_cost_usd}`),
    kpiBox('Cost/Booking',   stats.cost_per_booking ? `$${stats.cost_per_booking}` : '—'),
    kpiBox('E2E P50',        e2e ? `${e2e}ms` : '—',
           e2e ? kpiColourClass(e2e, provider === 'google' ? 300 : 800) : ''),
    kpiBox('First Response', fr ? `${fr}ms` : '—',
           fr ? kpiColourClass(fr, 3000) : ''),
  ];

  if (provider === 'sarvam') {
    boxes.push(
      kpiBox('DG STT P50',    sttP50 ? `${sttP50}ms` : '—',
             sttP50 ? kpiColourClass(sttP50, 300) : ''),
      kpiBox('DG Confidence', conf !== null && conf !== undefined ? `${conf}%` : '—',
             confClass(conf)),
    );
  }

  el.innerHTML = boxes.join('');
}

// ── Main data loader ──────────────────────────────────────────────────────────
async function loadData() {
  document.getElementById('updated').textContent = 'Loading…';
  try {
    const r    = await fetch('/metrics/data');
    const data = await r.json();
    _rawData  = data;

    // Store calls newest-first for table rendering
    _allCalls.sarvam = (data.sarvam?.recent_calls || []).slice().reverse();
    _allCalls.google = (data.google?.recent_calls || []).slice().reverse();
    _selected = { sarvam: new Set(), google: new Set() };

    const { sarvam, google } = data;

    renderKPIs('kpis-sarvam', sarvam, 'sarvam');
    renderKPIs('kpis-google', google, 'google');

    buildSarvamLatChart(sarvam?.latency);
    buildGoogleLatChart(google?.latency);
    buildDGLatChart(sarvam?.latency);
    buildDGConfChart(sarvam);
    buildResourceCharts(sarvam, google);
    buildCostChart(sarvam, google);

    document.getElementById('table-sarvam').innerHTML = buildTable(_allCalls.sarvam, 'sarvam');
    document.getElementById('table-google').innerHTML = buildTable(_allCalls.google, 'google');

    // Hide filter banners on fresh load
    document.getElementById('filter-banner-sarvam').style.display = 'none';
    document.getElementById('filter-banner-google').style.display = 'none';

    const ts = new Date(data.last_updated * 1000).toLocaleTimeString();
    document.getElementById('updated').textContent =
      `Updated ${ts} · ${data.total_calls} total calls`;
  } catch (e) {
    document.getElementById('updated').textContent = 'Error loading data';
    console.error(e);
  }
}

loadData();
setInterval(loadData, 30000);   // auto-refresh every 30 s
</script>
</body>
</html>
"""
