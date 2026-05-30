# -*- coding: utf-8 -*-
"""
Home page dashboard and provider-switch API.

GET  /           → Pipeline selector + links to metrics
POST /api/set-provider  → Switch active_provider in APP_CONFIG and persist to disk
"""
from aiohttp import web
from config.settings import APP_CONFIG, PORT, save_config

# ── Dashboard HTML template ───────────────────────────────────────────────────
# Uses str.format() — curly braces in CSS/JS are doubled to escape them.

_HOME_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Priya — Voice Agent</title>
<style>
  :root {{
    --primary: #6366f1;
    --primary-hover: #4f46e5;
    --bg: #f8fafc;
    --card: #ffffff;
    --text: #0f172a;
    --text-muted: #64748b;
    --border: #e2e8f0;
    --google: #1a73e8;
    --sarvam: #10b981;
  }}
  *,*::before,*::after{{box-sizing:border-box}}
  body{{font-family:'Inter', -apple-system, sans-serif;
       background:var(--bg);margin:0;padding:40px;color:var(--text);line-height:1.5}}
  
  .container {{ max-width: 900px; margin: 0 auto; }}
  
  header {{ margin-bottom: 40px; }}
  h1{{margin:0;font-size:2.5rem;font-weight:800;letter-spacing:-0.025em;background: linear-gradient(to right, #6366f1, #a855f7); -webkit-background-clip: text; -webkit-text-fill-color: transparent;}}
  .sub{{color:var(--text-muted);font-size:1.1rem;margin-top:8px}}
  
  .card{{background:var(--card);border-radius:24px;padding:32px;
         box-shadow:0 10px 25px -5px rgba(0,0,0,0.04), 0 8px 10px -6px rgba(0,0,0,0.04);
         border: 1px solid var(--border); margin-bottom:32px; transition: transform 0.2s;}}
  
  .card h2{{margin:0 0 24px;font-size:1.25rem;font-weight:700;display:flex;align-items:center;gap:10px}}
  .card h2 svg {{ color: var(--primary); }}

  .grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
  
  .provider{{border:2px solid var(--border);border-radius:18px;padding:24px;
             cursor:pointer;transition:all .2s ease;background:var(--bg);text-align:left;position:relative;overflow:hidden}}
  .provider:hover{{transform: translateY(-2px); border-color: var(--primary); }}
  .provider.active{{ border-width: 3px; background: white; box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1); }}
  
  .provider.active.sarvam{{border-color:var(--sarvam);}}
  .provider.active.google{{border-color:var(--google);}}
  
  .provider h3{{margin:0 0 8px;font-size:1.1rem;font-weight:700}}
  .provider p{{margin:0;font-size:.85rem;color:var(--text-muted);line-height:1.5}}
  
  .badge{{position:absolute; top: 12px; right: 12px; padding:4px 12px;border-radius:99px;
           font-size:.7rem;font-weight:800;background:var(--sarvam);color:white;text-transform:uppercase;letter-spacing:0.05em}}
  .provider.google .badge{{background:var(--google)}}
  
  .links{{display:flex;gap:16px;flex-wrap:wrap}}
  .btn{{padding:12px 28px;border-radius:12px;font-size:.95rem;font-weight:600;text-decoration:none;
        display:inline-flex;align-items:center;gap:8px;border:none;cursor:pointer;transition:all 0.2s}}
  .btn-primary{{background:var(--primary);color:white}}
  .btn-primary:hover{{background:var(--primary-hover); transform: scale(1.02);}}
  .btn-outline{{background:white;color:var(--primary);border:2px solid var(--primary)}}
  .btn-outline:hover{{background:var(--primary); color:white;}}
  
  .status{{font-size:.9rem;color:var(--text-muted);margin-top:20px;min-height:1.4em;font-weight:500}}

  /* Config Elements */
  .input-select {{
    width: 100%; padding: 10px 14px; border-radius: 10px; border: 1px solid var(--border);
    background: white; font-family: inherit; font-size: 0.9rem; font-weight: 500;
  }}
  .slider {{
    width: 100%; height: 6px; background: var(--border); border-radius: 5px;
    outline: none; -webkit-appearance: none; margin: 10px 0;
  }}
  .slider::-webkit-slider-thumb {{
    -webkit-appearance: none; appearance: none; width: 18px; height: 18px;
    background: var(--primary); cursor: pointer; border-radius: 50%;
  }}
  .config-group {{
    background: #ffffff; padding: 24px; border-radius: 20px; border: 1px solid var(--border);
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
  }}
  .config-group label {{
    font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted);
    margin-bottom: 12px; display: block;
  }}
  .temp-display {{
    float: right; font-weight: 700; color: var(--primary); font-size: 1rem;
  }}

  /* Upload Section */
  .upload-area {{
    border: 2px dashed var(--border); border-radius: 16px; padding: 40px; text-align: center;
    background: #fafafa; transition: all 0.2s; cursor: pointer; margin-bottom: 24px;
  }}
  .upload-area:hover {{ border-color: var(--primary); background: #f5f3ff; }}
  .file-list {{ list-style: none; padding: 0; margin: 0; }}
  .file-item {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 18px; border-bottom: 1px solid var(--border);
  }}
  .file-item:last-child {{ border-bottom: none; }}
  .file-info {{ display: flex; flex-direction: column; gap: 2px; }}
  .file-name {{ font-weight: 600; font-size: 0.95rem; }}
  .file-meta {{ font-size: 0.8rem; color: var(--text-muted); }}
  .btn-delete {{ color: #ef4444; background: none; border: none; cursor: pointer; padding: 4px; border-radius: 4px; }}
  .btn-delete:hover {{ background: #fee2e2; }}

  input[type="file"] {{ display: none; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Priya — Voice Agent</h1>
    <p class="sub">Neha Child Care · AI Receptionist · Port {port}</p>
  </header>

  <div class="card">
    <h2>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
      Active Pipeline
    </h2>
    <div class="grid">
      <div class="provider {sa_active} sarvam" onclick="switchProvider('sarvam')">
        <h3>Sarvam AI</h3>
        <p>Deepgram Nova-3 STT → Sarvam 30B LLM → Sarvam Bulbul v2 TTS</p>
        {sa_badge}
      </div>
      <div class="provider {goo_active} google" onclick="switchProvider('google')">
        <h3>Google Gemini</h3>
        <p>Gemini Multimodal Live — native end-to-end audio (lowest latency)</p>
        {goo_badge}
      </div>
    </div>
    <p class="status" id="status"></p>
  </div>

    <div class="card">
      <h2>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20v-8m0 0V4m0 8h8m-8 0H4"/></svg>
        Model Configuration
      </h2>
      <p class="sub" style="margin-top:-16px; margin-bottom:24px">Fine-tune the AI's behavior and performance.</p>

      <div class="grid">
        <div class="config-group">
          <label>Gemini Model</label>
          <select id="google-model" class="input-select" onchange="updateParams()">
            <option value="models/gemini-3.1-flash-live-preview">Gemini 3.1 Flash Live</option>
            <option value="models/gemini-1.5-flash">Gemini 1.5 Flash</option>
            <option value="models/gemini-1.5-pro">Gemini 1.5 Pro</option>
          </select>
          
          <label style="margin-top:24px">
            Temperature
            <span id="google-temp-val" class="temp-display">{goo_temp}</span>
          </label>
          <input type="range" id="google-temp" min="0" max="1" step="0.1" value="{goo_temp}" class="slider" oninput="document.getElementById('google-temp-val').innerText=this.value" onchange="updateParams()">
        </div>

        <div class="config-group">
          <label>Sarvam Model</label>
          <select id="sarvam-model" class="input-select" onchange="updateParams()">
            <option value="sarvam-30b">Sarvam 30B</option>
            <option value="sarvam-2b-instruct">Sarvam 2B Instruct</option>
          </select>
          
          <label style="margin-top:24px">
            Temperature
            <span id="sarvam-temp-val" class="temp-display">{sa_temp}</span>
          </label>
          <input type="range" id="sarvam-temp" min="0" max="1" step="0.1" value="{sa_temp}" class="slider" oninput="document.getElementById('sarvam-temp-val').innerText=this.value" onchange="updateParams()">
        </div>
      </div>
    </div>

    <div class="card">
      <h2>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        Knowledge Base
      </h2>
    <p class="sub" style="margin-top:-16px; margin-bottom:24px">Upload PDFs or documents to train the agent's knowledge.</p>
    
    <label class="upload-area" id="drop-zone">
      <input type="file" id="file-input" multiple onchange="handleUpload(this.files)">
      <div style="font-size: 2rem; margin-bottom: 8px">📄</div>
      <div style="font-weight: 600">Click to upload or drag & drop</div>
      <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 4px">Gemini supports PDF, CSV, TXT, and Images</div>
    </label>

    <div id="file-list-container" style="display:none">
      <h3 style="font-size: 0.9rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px">Uploaded Files</h3>
      <div style="background: #fafafa; border-radius: 12px; border: 1px solid var(--border)">
        <ul class="file-list" id="file-list"></ul>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M18 17l-6-6-4 4-5-5"/></svg>
      Monitoring
    </h2>
    <div class="links">
      <a href="/metrics" class="btn btn-primary">Metrics Dashboard</a>
      <a href="/metrics/data" class="btn btn-outline">Raw JSON</a>
    </div>
  </div>
</div>

<script>
async function switchProvider(p) {{
  document.getElementById('status').textContent = 'Switching to ' + p + '…';
  const r = await fetch('/api/set-provider', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{provider: p}})
  }});
  const d = await r.json();
  if (d.ok) {{ location.reload(); }}
  else {{ document.getElementById('status').textContent = 'Error: ' + (d.error || 'unknown'); }}
}}

async function refreshFiles() {{
  const r = await fetch('/api/files');
  const d = await r.json();
  const list = document.getElementById('file-list');
  const container = document.getElementById('file-list-container');
  
  if (d.files && d.files.length > 0) {{
    container.style.display = 'block';
    list.innerHTML = d.files.map(f => `
      <li class="file-item">
        <div class="file-info">
          <span class="file-name">${{f.name}}</span>
          <span class="file-meta">${{(f.size / 1024).toFixed(1)}} KB · ${{new Date(f.time * 1000).toLocaleDateString()}}</span>
        </div>
        <button class="btn-delete" onclick="deleteFile('${{f.name}}')">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
        </button>
      </li>
    `).join('');
  }} else {{
    container.style.display = 'none';
  }}
}}

async function handleUpload(files) {{
  for (let file of files) {{
    const formData = new FormData();
    formData.append('file', file);
    
    try {{
      const r = await fetch('/api/upload', {{
        method: 'POST',
        body: formData
      }});
      const d = await r.json();
      if (!d.ok) alert('Upload failed: ' + d.error);
    {{ catch (e) {{
      alert('Upload error: ' + e);
    }}
  }}
  refreshFiles();
}}

async function deleteFile(name) {{
  if (!confirm('Delete ' + name + '?')) return;
  const r = await fetch('/api/delete-file', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{filename: name}})
  }});
  refreshFiles();
}}

async function updateParams() {{
  const data = {{
    google: {{
      model: document.getElementById('google-model').value,
      temperature: parseFloat(document.getElementById('google-temp').value)
    }},
    sarvam: {{
      model: document.getElementById('sarvam-model').value,
      temperature: parseFloat(document.getElementById('sarvam-temp').value)
    }}
  }};
  
  document.getElementById('status').textContent = 'Saving configuration…';
  try {{
    const r = await fetch('/api/set-parameters', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(data)
    }});
    const d = await r.json();
    if (d.ok) {{ 
      document.getElementById('status').textContent = 'Configuration saved!';
      setTimeout(() => document.getElementById('status').textContent = '', 2000);
    }} else {{
      document.getElementById('status').textContent = 'Error: ' + d.error;
    }}
  }} catch (e) {{
    document.getElementById('status').textContent = 'Network error: ' + e;
  }}
}}

// Initial load
document.addEventListener('DOMContentLoaded', () => {{
  document.getElementById('google-model').value = "{goo_model}";
  document.getElementById('sarvam-model').value = "{sa_model}";
  refreshFiles();
}});

// Drag & drop support
const dropZone = document.getElementById('drop-zone');
dropZone.addEventListener('dragover', e => {{ e.preventDefault(); dropZone.style.borderColor = 'var(--primary)'; }});
dropZone.addEventListener('dragleave', () => {{ dropZone.style.borderColor = 'var(--border)'; }});
dropZone.addEventListener('drop', e => {{
  e.preventDefault();
  handleUpload(e.dataTransfer.files);
}});
</script>
</body>
</html>
"""



async def home_page(request: web.Request) -> web.Response:
    provider   = APP_CONFIG.get("active_provider", "sarvam")
    sa_active  = "active" if provider == "sarvam" else ""
    goo_active = "active" if provider == "google"  else ""
    sa_badge   = '<span class="badge">● ACTIVE</span>' if provider == "sarvam" else ""
    goo_badge  = '<span class="badge">● ACTIVE</span>' if provider == "google"  else ""
    
    # Model parameters
    params = APP_CONFIG.get("parameters", {})
    sa_params = params.get("sarvam", {})
    goo_params = params.get("google", {})
    
    html = _HOME_TEMPLATE.format(
        port=PORT,
        sa_active=sa_active, goo_active=goo_active,
        sa_badge=sa_badge,   goo_badge=goo_badge,
        sa_model=sa_params.get("model", "sarvam-30b"),
        sa_temp=sa_params.get("temperature", 0.1),
        goo_model=goo_params.get("model", "models/gemini-3.1-flash-live-preview"),
        goo_temp=goo_params.get("temperature", 0.1),
    )
    return web.Response(text=html, content_type="text/html")


async def set_provider(request: web.Request) -> web.Response:
    try:
        data     = await request.json()
        provider = data.get("provider", "").strip().lower()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    if provider not in ("sarvam", "google"):
        return web.json_response(
            {"ok": False, "error": "Must be 'sarvam' or 'google'"}, status=400
        )

    APP_CONFIG["active_provider"] = provider
    try:
        save_config()
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)

    print(f"🔄 Provider switched → {provider}")
    return web.json_response({"ok": True, "provider": provider})


async def set_parameters(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        if "parameters" not in APP_CONFIG:
            APP_CONFIG["parameters"] = {}
            
        if "google" in data:
            APP_CONFIG["parameters"]["google"] = data["google"]
        if "sarvam" in data:
            APP_CONFIG["parameters"]["sarvam"] = data["sarvam"]
            
        save_config()
        print(f"⚙️ Parameters updated: {data}")
        return web.json_response({"ok": True})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)
