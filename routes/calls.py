# -*- coding: utf-8 -*-
"""
Calls observability dashboard.

GET /calls               → HTML dashboard showing per-call quality metrics
GET /calls/data          → JSON from Call_Logs Google Sheet (last 200 calls)
GET /calls/audio         → Stream WAV audio from GCS to browser
GET /calls/local-audio   → Stream WAV audio from local recordings/ directory
"""
import asyncio
import io
import os
from aiohttp import web
from mydoot_functions import get_call_logs, get_google_creds

# Must match RECORDINGS_DIR in pipelines/gemini.py
_LOCAL_RECORDINGS_DIR = os.getenv("RECORDINGS_DIR", "recordings")


# ── Dashboard HTML ────────────────────────────────────────────────────────────

_CALLS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Call Logs — MyDoot</title>
<style>
  :root {
    --primary: #6366f1; --primary-hover: #4f46e5;
    --bg: #f8fafc; --card: #ffffff;
    --text: #0f172a; --text-muted: #64748b;
    --border: #e2e8f0;
    --agent-bg: #ede9fe; --agent-text: #3730a3;
    --cust-bg: #dcfce7;  --cust-text: #14532d;
  }
  *,*::before,*::after { box-sizing: border-box; }
  body { font-family: 'Inter', -apple-system, sans-serif; background: var(--bg); margin: 0; padding: 32px; color: var(--text); }
  .container { max-width: 1380px; margin: 0 auto; }
  header { margin-bottom: 32px; display: flex; align-items: center; justify-content: space-between; }
  h1 { margin: 0; font-size: 2rem; font-weight: 800; background: linear-gradient(to right, #6366f1, #a855f7); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .sub { color: var(--text-muted); font-size: 0.9rem; margin-top: 4px; }
  .nav-links { display: flex; gap: 16px; align-items: center; }
  .back { color: var(--primary); text-decoration: none; font-size: 0.85rem; font-weight: 600; padding: 7px 16px; border: 2px solid var(--primary); border-radius: 8px; }
  .back:hover { background: var(--primary); color: white; }

  /* Stats */
  .stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 16px; margin-bottom: 28px; }
  .stat { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 20px; }
  .stat-label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); margin-bottom: 6px; }
  .stat-val { font-size: 1.75rem; font-weight: 800; color: var(--text); }
  .stat-sub { font-size: 0.78rem; color: var(--text-muted); margin-top: 2px; }

  /* Table card */
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 20px; overflow: hidden; }
  .card-header { padding: 20px 24px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
  .card-header h2 { margin: 0; font-size: 1.05rem; font-weight: 700; }
  .refresh-btn { background: var(--primary); color: white; border: none; border-radius: 8px; padding: 7px 16px; font-size: 0.82rem; font-weight: 600; cursor: pointer; }
  .refresh-btn:hover { background: var(--primary-hover); }

  table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
  th { padding: 11px 14px; text-align: left; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); background: #f8fafc; border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 11px 14px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tr.data-row { cursor: pointer; transition: background 0.1s; }
  tr.data-row:hover td { background: #f5f3ff; }
  tr.data-row.expanded td { background: #ede9fe; }

  /* Badges */
  .badge { display: inline-block; padding: 2px 9px; border-radius: 99px; font-size: 0.7rem; font-weight: 700; }
  .badge-green { background: #d1fae5; color: #065f46; }
  .badge-red   { background: #fee2e2; color: #991b1b; }
  .badge-gray  { background: #f1f5f9; color: var(--text-muted); }

  /* Expand toggle arrow */
  .toggle-arrow { display: inline-block; transition: transform 0.2s; font-size: 0.7rem; color: var(--text-muted); margin-right: 4px; }
  tr.data-row.expanded .toggle-arrow { transform: rotate(90deg); }

  /* Detail panel */
  tr.detail-row td { padding: 0; border-bottom: 2px solid var(--border); }
  .detail-panel { display: none; padding: 20px 28px 24px; background: #fafbff; }
  .detail-panel.open { display: block; }
  .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }

  /* Audio section */
  .audio-section { margin-bottom: 0; }
  .section-label { font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: var(--text-muted); margin-bottom: 10px; }
  .audio-player { width: 100%; height: 40px; border-radius: 8px; }
  .no-audio { font-size: 0.82rem; color: var(--text-muted); font-style: italic; padding: 10px 0; }

  /* Transcript chat bubbles */
  .transcript-section { max-height: 340px; overflow-y: auto; padding-right: 4px; }
  .chat-line { display: flex; flex-direction: column; margin-bottom: 8px; }
  .chat-line.agent { align-items: flex-start; }
  .chat-line.customer { align-items: flex-end; }
  .chat-ts { font-size: 0.65rem; color: var(--text-muted); margin-bottom: 2px; font-family: monospace; }
  .chat-bubble { max-width: 85%; padding: 7px 12px; border-radius: 12px; font-size: 0.82rem; line-height: 1.5; word-break: break-word; }
  .chat-line.agent .chat-bubble { background: var(--agent-bg); color: var(--agent-text); border-top-left-radius: 3px; }
  .chat-line.customer .chat-bubble { background: var(--cust-bg); color: var(--cust-text); border-top-right-radius: 3px; }
  .chat-role { font-size: 0.65rem; font-weight: 700; margin-bottom: 3px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }
  .no-transcript { font-size: 0.82rem; color: var(--text-muted); font-style: italic; }

  .loading { padding: 48px; text-align: center; color: var(--text-muted); font-size: 0.9rem; }
  .empty { padding: 48px; text-align: center; color: var(--text-muted); }

  /* Latency table */
  .lat-table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
  .lat-table th { padding: 10px 16px; text-align: left; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); background: #f8fafc; border-bottom: 1px solid var(--border); }
  .lat-table td { padding: 10px 16px; border-bottom: 1px solid #f1f5f9; font-variant-numeric: tabular-nums; }
  .lat-table tr:last-child td { border-bottom: none; }
  .lat-warn { color: #b45309; font-weight: 700; }
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>MyDoot Call Logs</h1>
      <div class="sub">Per-call quality and observability — last 200 calls</div>
    </div>
    <div class="nav-links">
      <a class="back" href="/">&#8592; Dashboard</a>
    </div>
  </header>

  <div class="stats" id="stats-row">
    <div class="stat"><div class="stat-label">Total Calls</div><div class="stat-val" id="s-total">—</div></div>
    <div class="stat"><div class="stat-label">Requests Saved</div><div class="stat-val" id="s-saved">—</div><div class="stat-sub" id="s-saved-pct"></div></div>
    <div class="stat"><div class="stat-label">Avg Duration</div><div class="stat-val" id="s-dur">—</div><div class="stat-sub">seconds</div></div>
    <div class="stat"><div class="stat-label">Avg STT Latency</div><div class="stat-val" id="s-stt">—</div><div class="stat-sub">ms</div></div>
    <div class="stat"><div class="stat-label">Avg STT Drops</div><div class="stat-val" id="s-drops">—</div></div>
    <div class="stat"><div class="stat-label">Avg Barge-Ins</div><div class="stat-val" id="s-bargeins">—</div></div>
  </div>

  <div class="card" style="margin-bottom:24px">
    <div class="card-header">
      <h2>Turn Latency — last 24 h (P50 / P95 / P99)</h2>
    </div>
    <div id="latency-wrap"><div class="loading">Loading…</div></div>
  </div>

  <div class="card">
    <div class="card-header">
      <h2>Calls</h2>
      <button class="refresh-btn" onclick="load()">Refresh</button>
    </div>
    <div id="table-wrap">
      <div class="loading">Loading…</div>
    </div>
  </div>
</div>

<script>
let _rows = [];

async function loadLatency() {
  const wrap = document.getElementById('latency-wrap');
  try {
    const r = await fetch('/latency?hours=24');
    const d = await r.json();
    const n = d['sample_count'];
    if (!n) { wrap.innerHTML = '<div class="empty">No latency data yet — data populates after calls complete.</div>'; return; }
    const rows = [
      ['STT',        'stt_ms',              800],
      ['LLM first token', 'llm_first_token_ms', 2500],
      ['End-to-end', 'end_to_end_turn_ms',  4000],
    ];
    const fmt = (v, thresh) => {
      if (v == null) return '—';
      const ms = Math.round(v);
      return ms > thresh ? `<span class="lat-warn">${ms} ms ⚠</span>` : `${ms} ms`;
    };
    let html = `<table class="lat-table"><thead><tr><th>Stage</th><th>P50</th><th>P95</th><th>P99</th></tr></thead><tbody>`;
    rows.forEach(([label, key, thresh]) => {
      html += `<tr><td>${label}</td><td>${fmt(d[key+'_p50'],thresh)}</td><td>${fmt(d[key+'_p95'],thresh)}</td><td>${fmt(d[key+'_p99'],thresh)}</td></tr>`;
    });
    html += `</tbody></table><div style="padding:8px 16px;font-size:0.72rem;color:var(--text-muted)">${n} turns sampled</div>`;
    wrap.innerHTML = html;
  } catch(e) {
    wrap.innerHTML = '<div class="empty">Latency data unavailable (PostgreSQL not configured).</div>';
  }
}

async function load() {
  document.getElementById('table-wrap').innerHTML = '<div class="loading">Loading…</div>';
  try {
    const r = await fetch('/calls/data');
    const d = await r.json();
    _rows = d.calls || [];
    renderStats(_rows);
    renderTable(_rows);
  } catch(e) {
    document.getElementById('table-wrap').innerHTML = '<div class="empty">Failed to load: ' + e + '</div>';
  }
}

function avg(arr) {
  const v = arr.filter(x => x !== '' && x !== null && !isNaN(parseFloat(x))).map(parseFloat);
  return v.length ? (v.reduce((a,b)=>a+b,0) / v.length) : null;
}

function renderStats(rows) {
  const total = rows.length;
  const saved = rows.filter(r => r['Saved'] === 'TRUE' || r['Saved'] === true || r['Saved'] === '1' || r['Saved'] === 'YES').length;
  const pct = total ? Math.round(100 * saved / total) : 0;
  const durAvg = avg(rows.map(r => r['Duration (s)']));
  const sttAvg = avg(rows.map(r => r['STT Avg (ms)']));
  const dropAvg = avg(rows.map(r => r['STT Drops']));
  const bargeAvg = avg(rows.map(r => r['Barge-Ins']));
  document.getElementById('s-total').textContent = total;
  document.getElementById('s-saved').textContent = saved;
  document.getElementById('s-saved-pct').textContent = pct + '% completion';
  document.getElementById('s-dur').textContent = durAvg != null ? Math.round(durAvg) : '—';
  document.getElementById('s-stt').textContent = sttAvg != null ? Math.round(sttAvg) : '—';
  document.getElementById('s-drops').textContent = dropAvg != null ? dropAvg.toFixed(1) : '—';
  document.getElementById('s-bargeins').textContent = bargeAvg != null ? bargeAvg.toFixed(1) : '—';
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Parse "[HH:MM:SS.mmm] Role: text" lines into chat bubbles HTML
function buildTranscriptHTML(raw) {
  if (!raw || !raw.trim()) return '<div class="no-transcript">No transcript recorded.</div>';
  const lines = raw.split('\\n').filter(l => l.trim());
  if (!lines.length) return '<div class="no-transcript">No transcript recorded.</div>';
  let html = '';
  const lineRe = /^\\[([^\\]]+)\\]\\s*(Agent|Customer):\\s*(.*)$/i;
  lines.forEach(line => {
    const m = line.match(lineRe);
    if (!m) {
      // Unrecognised line — show as plain text
      html += `<div class="chat-line agent"><div class="chat-bubble" style="background:#f1f5f9;color:#475569">${esc(line)}</div></div>`;
      return;
    }
    const [, ts, role, text] = m;
    const cls = role.toLowerCase() === 'agent' ? 'agent' : 'customer';
    html += `<div class="chat-line ${cls}">
      <div class="chat-ts">${esc(ts)} · ${esc(role)}</div>
      <div class="chat-bubble">${esc(text)}</div>
    </div>`;
  });
  return html || '<div class="no-transcript">No transcript recorded.</div>';
}

function renderTable(rows) {
  if (!rows.length) {
    document.getElementById('table-wrap').innerHTML = '<div class="empty">No call logs yet.</div>';
    return;
  }
  let html = '<table><thead><tr>'
    + '<th></th>'
    + '<th>Time (IST)</th><th>Caller</th><th>Dur (s)</th>'
    + '<th>Category</th><th>Subcategory</th><th>Issue Type</th>'
    + '<th>Stage</th><th>Saved</th>'
    + '<th>STT Avg</th><th>Drops</th><th>Barge-Ins</th><th>Reconnects</th>'
    + '</tr></thead><tbody>';

  rows.forEach((r, i) => {
    const saved = r['Saved'] === 'TRUE' || r['Saved'] === true || r['Saved'] === '1' || r['Saved'] === 'YES';
    const badgeSaved = saved
      ? '<span class="badge badge-green">Saved</span>'
      : '<span class="badge badge-red">No</span>';
    const stage = r['Stage Reached'] || '—';
    const audio = r['Audio GCS'] || '';
    const localWav = r['Local Recording'] || '';
    const transcriptHTML = buildTranscriptHTML(r['Transcript'] || '');

    let audioSection;
    if (audio) {
      audioSection = `<div class="audio-section">
          <div class="section-label">&#9654; Recording</div>
          <audio class="audio-player" controls src="/calls/audio?uri=${encodeURIComponent(audio)}"></audio>
         </div>`;
    } else if (localWav) {
      audioSection = `<div class="audio-section">
          <div class="section-label">&#9654; Recording (local)</div>
          <audio class="audio-player" controls src="/calls/local-audio?file=${encodeURIComponent(localWav)}"></audio>
         </div>`;
    } else {
      audioSection = `<div class="audio-section">
          <div class="section-label">&#9654; Recording</div>
          <div class="no-audio">No recording for this call.</div>
         </div>`;
    }

    html += `<tr class="data-row" id="row-${i}" onclick="toggleDetail(${i})">
      <td style="width:28px;text-align:center"><span class="toggle-arrow">&#9654;</span></td>
      <td style="white-space:nowrap;font-size:0.78rem">${esc(r['Timestamp (IST)'] || '—')}</td>
      <td style="font-family:monospace;font-size:0.78rem">${esc(r['Caller ID'] || '—')}</td>
      <td>${esc(r['Duration (s)'] || '—')}</td>
      <td>${esc(r['Category'] || '—')}</td>
      <td>${esc(r['Subcategory'] || '—')}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r['Issue Type'] || '')}">${esc(r['Issue Type'] || '—')}</td>
      <td><span class="badge badge-gray">${esc(stage)}</span></td>
      <td>${badgeSaved}</td>
      <td>${r['STT Avg (ms)'] ? Math.round(parseFloat(r['STT Avg (ms)'])) + ' ms' : '—'}</td>
      <td>${esc(r['STT Drops'] || '0')}</td>
      <td>${esc(r['Barge-Ins'] || '0')}</td>
      <td>${esc(r['Reconnects'] || '0')}</td>
    </tr>
    <tr class="detail-row" id="dr-${i}">
      <td colspan="13">
        <div class="detail-panel" id="dp-${i}">
          <div class="detail-grid">
            ${audioSection}
            <div>
              <div class="section-label">&#128172; Transcript</div>
              <div class="transcript-section">${transcriptHTML}</div>
            </div>
          </div>
        </div>
      </td>
    </tr>`;
  });

  html += '</tbody></table>';
  document.getElementById('table-wrap').innerHTML = html;
}

function toggleDetail(i) {
  const panel = document.getElementById('dp-' + i);
  const row   = document.getElementById('row-' + i);
  if (!panel || !row) return;
  const isOpen = panel.classList.toggle('open');
  row.classList.toggle('expanded', isOpen);
  // Pause audio when collapsing
  if (!isOpen) {
    const aud = panel.querySelector('audio');
    if (aud) aud.pause();
  }
}

load();
loadLatency();
</script>
</body>
</html>
"""


# ── Route handlers ─────────────────────────────────────────────────────────────

async def calls_page(request: web.Request) -> web.Response:
    """Serve the call logs HTML dashboard."""
    return web.Response(text=_CALLS_HTML, content_type="text/html")


async def calls_data(request: web.Request) -> web.Response:
    """Return the last 200 call log records as JSON (from Google Sheets)."""
    records = await asyncio.to_thread(get_call_logs, 200)
    return web.json_response({"calls": records, "count": len(records)})


async def audio_proxy(request: web.Request) -> web.Response:
    """Stream a GCS WAV file to the browser.

    Query param: uri=gs://bucket/path/to/file.wav
    """
    gcs_uri = request.query.get("uri", "").strip()
    if not gcs_uri.startswith("gs://"):
        return web.Response(status=400, text="Bad uri")

    try:
        from google.cloud import storage as gcs_storage
    except ImportError:
        return web.Response(status=503, text="google-cloud-storage not installed")

    creds_data = get_google_creds()
    if not creds_data:
        return web.Response(status=503, text="No GCS credentials")

    # Parse gs://bucket/blob
    path_part = gcs_uri[5:]           # strip gs://
    slash_idx = path_part.find("/")
    if slash_idx == -1:
        return web.Response(status=400, text="Bad GCS URI")
    bucket_name = path_part[:slash_idx]
    blob_name   = path_part[slash_idx + 1:]

    try:
        from google.oauth2 import service_account as _sa
        creds = _sa.Credentials.from_service_account_info(
            creds_data,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        client = gcs_storage.Client(credentials=creds, project=creds_data.get("project_id"))
        bucket = client.bucket(bucket_name)
        blob   = bucket.blob(blob_name)

        buf = io.BytesIO()
        await asyncio.to_thread(blob.download_to_file, buf)
        buf.seek(0)
        data = buf.read()
    except Exception as e:
        return web.Response(status=404, text=f"GCS error: {e}")

    return web.Response(
        body=data,
        content_type="audio/wav",
        headers={"Content-Disposition": f'inline; filename="{os.path.basename(blob_name)}"'},
    )


async def local_audio(request: web.Request) -> web.Response:
    """
    Stream a WAV file from the local recordings/ directory to the browser.

    Query param: file=<caller_id>_<timestamp>.wav
    Only filenames — no path traversal allowed.
    """
    filename = request.query.get("file", "").strip()
    # First-pass: reject obvious traversal characters before touching the filesystem
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return web.Response(status=400, text="Invalid filename")
    if not filename.endswith(".wav"):
        return web.Response(status=400, text="Only .wav files are served here")

    wav_path = os.path.join(_LOCAL_RECORDINGS_DIR, filename)
    # Second-pass: resolve symlinks and verify the path stays inside recordings dir
    recordings_real = os.path.realpath(_LOCAL_RECORDINGS_DIR)
    wav_real        = os.path.realpath(wav_path)
    if not wav_real.startswith(recordings_real + os.sep):
        return web.Response(status=400, text="Invalid filename")

    if not os.path.isfile(wav_real):
        return web.Response(status=404, text="Recording not found")

    return web.FileResponse(wav_real, headers={"Content-Type": "audio/wav"})
