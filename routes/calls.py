# -*- coding: utf-8 -*-
"""
Calls observability dashboard.

GET /calls          → HTML dashboard showing per-call quality metrics
GET /calls/data     → JSON from Call_Logs Google Sheet (last 200 calls)
GET /calls/audio    → Stream WAV audio from GCS to browser
"""
import asyncio
import io
import os
from aiohttp import web
from mydoot_functions import get_call_logs, get_google_creds


# ── Dashboard HTML ────────────────────────────────────────────────────────────

_CALLS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Call Logs — Mydoot</title>
<style>
  :root {
    --primary: #6366f1; --primary-hover: #4f46e5;
    --bg: #f8fafc; --card: #ffffff;
    --text: #0f172a; --text-muted: #64748b;
    --border: #e2e8f0;
    --green: #10b981; --red: #ef4444; --yellow: #f59e0b;
  }
  *,*::before,*::after { box-sizing: border-box; }
  body { font-family: 'Inter', -apple-system, sans-serif; background: var(--bg); margin: 0; padding: 32px; color: var(--text); }
  .container { max-width: 1300px; margin: 0 auto; }
  header { margin-bottom: 32px; display: flex; align-items: center; justify-content: space-between; }
  h1 { margin: 0; font-size: 2rem; font-weight: 800; background: linear-gradient(to right, #6366f1, #a855f7); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .sub { color: var(--text-muted); font-size: 0.9rem; margin-top: 4px; }
  .back { color: var(--primary); text-decoration: none; font-size: 0.85rem; font-weight: 600; }
  .back:hover { text-decoration: underline; }

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
  tr.data-row { cursor: pointer; }
  tr.data-row:hover td { background: #f8f9ff; }

  /* Badges */
  .badge { display: inline-block; padding: 2px 9px; border-radius: 99px; font-size: 0.7rem; font-weight: 700; }
  .badge-green { background: #d1fae5; color: #065f46; }
  .badge-red   { background: #fee2e2; color: #991b1b; }
  .badge-gray  { background: #f1f5f9; color: var(--text-muted); }

  /* Expandable transcript */
  tr.transcript-row td { padding: 0; }
  .transcript-panel { display: none; padding: 16px 24px; background: #fafbff; border-top: 1px solid var(--border); }
  .transcript-panel.open { display: block; }
  .transcript-text { white-space: pre-wrap; font-size: 0.8rem; color: #334155; line-height: 1.6; font-family: monospace; max-height: 260px; overflow-y: auto; }
  audio { margin-top: 12px; width: 100%; max-width: 480px; height: 36px; }

  .loading { padding: 48px; text-align: center; color: var(--text-muted); font-size: 0.9rem; }
  .empty { padding: 48px; text-align: center; color: var(--text-muted); }
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>Call Logs</h1>
      <div class="sub">Per-call quality and observability — last 200 calls</div>
    </div>
    <a class="back" href="/">&#8592; Dashboard</a>
  </header>

  <div class="stats" id="stats-row">
    <div class="stat"><div class="stat-label">Total Calls</div><div class="stat-val" id="s-total">—</div></div>
    <div class="stat"><div class="stat-label">Requests Saved</div><div class="stat-val" id="s-saved">—</div><div class="stat-sub" id="s-saved-pct"></div></div>
    <div class="stat"><div class="stat-label">Avg Duration</div><div class="stat-val" id="s-dur">—</div><div class="stat-sub">seconds</div></div>
    <div class="stat"><div class="stat-label">Avg STT Latency</div><div class="stat-val" id="s-stt">—</div><div class="stat-sub">ms</div></div>
    <div class="stat"><div class="stat-label">Avg STT Drops</div><div class="stat-val" id="s-drops">—</div></div>
    <div class="stat"><div class="stat-label">Avg Barge-Ins</div><div class="stat-val" id="s-bargeins">—</div></div>
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
  const saved = rows.filter(r => r['Saved'] === 'TRUE' || r['Saved'] === true || r['Saved'] === '1').length;
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

function renderTable(rows) {
  if (!rows.length) {
    document.getElementById('table-wrap').innerHTML = '<div class="empty">No call logs yet.</div>';
    return;
  }
  let html = '<table><thead><tr>'
    + '<th>Time (IST)</th><th>Caller</th><th>Dur (s)</th>'
    + '<th>Category</th><th>Subcategory</th><th>Issue Type</th>'
    + '<th>Stage</th><th>Saved</th>'
    + '<th>STT Avg</th><th>STT Drops</th><th>Barge-Ins</th><th>Reconnects</th>'
    + '<th>Audio</th>'
    + '</tr></thead><tbody>';

  rows.forEach((r, i) => {
    const saved = r['Saved'] === 'TRUE' || r['Saved'] === true || r['Saved'] === '1';
    const badgeSaved = saved
      ? '<span class="badge badge-green">Saved</span>'
      : '<span class="badge badge-red">No</span>';
    const stage = r['Stage Reached'] || '—';
    const audio = r['Audio GCS'] || '';
    const audioBtn = audio
      ? `<button onclick="event.stopPropagation();playAudio('${esc(audio)}',this)" style="background:none;border:1px solid var(--border);border-radius:6px;padding:3px 9px;font-size:0.75rem;cursor:pointer;">&#9654;</button>`
      : '<span style="color:var(--text-muted);font-size:0.75rem">—</span>';

    html += `<tr class="data-row" onclick="toggleTranscript(${i})">
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
      <td>${audioBtn}</td>
    </tr>
    <tr class="transcript-row" id="tr-${i}">
      <td colspan="13">
        <div class="transcript-panel" id="tp-${i}">
          <div style="font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--text-muted);margin-bottom:8px">Transcript</div>
          <div class="transcript-text">${esc(r['Transcript'] || 'No transcript.')}</div>
          ${audio ? `<audio id="aud-${i}" controls src="" style="display:none"></audio>` : ''}
        </div>
      </td>
    </tr>`;
  });

  html += '</tbody></table>';
  document.getElementById('table-wrap').innerHTML = html;
}

function toggleTranscript(i) {
  const panel = document.getElementById('tp-' + i);
  if (!panel) return;
  panel.classList.toggle('open');
}

function playAudio(gcsUri, btn) {
  const row = btn.closest('tr');
  const next = row.nextElementSibling;
  if (!next) return;
  const panel = next.querySelector('.transcript-panel');
  if (panel) panel.classList.add('open');
  const aud = next.querySelector('audio');
  if (!aud) return;
  const proxied = '/calls/audio?uri=' + encodeURIComponent(gcsUri);
  aud.src = proxied;
  aud.style.display = 'block';
  aud.play();
}

load();
</script>
</body>
</html>
"""


# ── Route handlers ─────────────────────────────────────────────────────────────

async def calls_page(request: web.Request) -> web.Response:
    return web.Response(text=_CALLS_HTML, content_type="text/html")


async def calls_data(request: web.Request) -> web.Response:
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
