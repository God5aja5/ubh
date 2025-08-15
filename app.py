# File: app.py
#!/usr/bin/env python3
"""
app.py - Single-file Flask + Flask-SocketIO full-stack file manager, runner, terminals, and pip installer.

Minimal dependencies:
    pip install -r requirements.txt
or:
    pip install flask flask-socketio

Run:
    python app.py

Security:
- This app runs user-provided code in subprocesses with best-effort resource limits on UNIX (via `resource`).
- Do NOT expose this service publicly without additional authentication and network controls.
"""

import os
import sys
import io
import json
import shlex
import time
import uuid
import shutil
import signal
import pathlib
import threading
import subprocess
from typing import Dict, Any, Optional
from functools import wraps
from flask import Flask, jsonify, request, render_template_string, send_file
from flask_socketio import SocketIO, emit

# Optional resource module for UNIX resource limits
try:
    import resource  # type: ignore
except Exception:
    resource = None

# -----------------------
# Configuration
# -----------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(BASE_DIR, "workspace")
os.makedirs(WORKDIR, exist_ok=True)

DEFAULT_TIMEOUT = 120  # seconds per job (default)
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024  # 512MB
CPU_TIME_LIMIT = 60  # seconds

# Flask + Socket.IO
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# In-memory process registry
processes: Dict[str, Dict[str, Any]] = {}
proc_lock = threading.Lock()


# -----------------------
# Utilities
# -----------------------
def safe_join(base: str, *paths: str) -> str:
    """Join and enforce path stays within base (prevents directory traversal)."""
    base = os.path.abspath(base)
    candidate = os.path.abspath(os.path.join(base, *paths))
    if not candidate.startswith(base):
        raise ValueError("Invalid path")
    return candidate


def set_limits_for_child():
    """Apply resource limits in subprocess (UNIX-only)."""
    if resource:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (CPU_TIME_LIMIT, CPU_TIME_LIMIT))
        except Exception:
            pass
        try:
            resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
        except Exception:
            pass
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
        except Exception:
            pass


def detect_preview_url(text: str) -> Optional[str]:
    """Detect common local server URLs in output lines and return a usable preview URL."""
    import re

    patterns = [
        r"(https?://127\.0\.0\.1:\d+)",
        r"(https?://0\.0\.0\.0:\d+)",
        r"(http://localhost:\d+)",
        r"(https?://localhost:\d+)",
        r"(https?://[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:\d+)",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            url = m.group(1).replace("0.0.0.0", "127.0.0.1")
            return url
    m = re.search(r"Running on (http://[^\s]+)", text)
    if m:
        return m.group(1).replace("0.0.0.0", "127.0.0.1")
    return None


def read_and_emit(proc: subprocess.Popen, job_id: str, sid: Optional[str], stream_name: str, stream):
    """Read stream lines and emit to client room via socketio."""
    try:
        while True:
            chunk = stream.readline()
            if not chunk:
                break
            try:
                text = chunk.decode(errors="replace")
            except Exception:
                text = str(chunk)
            socketio.emit("proc.output", {"job_id": job_id, "stream": stream_name, "text": text}, room=sid or None)
            preview = detect_preview_url(text)
            if preview:
                socketio.emit("proc.preview", {"job_id": job_id, "url": preview}, room=sid or None)
    except Exception as e:
        socketio.emit("proc.output", {"job_id": job_id, "stream": "stderr", "text": f"[stream error] {e}\n"})


def start_subprocess(cmd: list, cwd: str, sid: Optional[str], timeout: Optional[int] = None) -> str:
    """Start a subprocess and stream its output. Returns a job_id."""
    job_id = str(uuid.uuid4())
    preexec = None
    if os.name != "nt" and resource:
        preexec = set_limits_for_child
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            shell=False,
            preexec_fn=preexec,
        )
    except Exception as e:
        socketio.emit("proc.output", {"job_id": job_id, "stream": "stderr", "text": f"Failed to start: {e}\n"}, room=sid or None)
        socketio.emit("proc.exit", {"job_id": job_id, "code": -1}, room=sid or None)
        return job_id

    with proc_lock:
        processes[job_id] = {"proc": proc, "sid": sid, "start": time.time(), "timeout": timeout or DEFAULT_TIMEOUT, "cmd": cmd, "cwd": cwd}

    # readers
    t_out = threading.Thread(target=read_and_emit, args=(proc, job_id, sid, "stdout", proc.stdout), daemon=True)
    t_err = threading.Thread(target=read_and_emit, args=(proc, job_id, sid, "stderr", proc.stderr), daemon=True)
    t_out.start()
    t_err.start()

    def watchdog():
        while True:
            time.sleep(0.5)
            with proc_lock:
                meta = processes.get(job_id)
            if not meta:
                break
            p = meta["proc"]
            if p.poll() is not None:
                break
            elapsed = time.time() - meta["start"]
            if meta["timeout"] and elapsed > meta["timeout"]:
                try:
                    p.kill()
                    socketio.emit("proc.output", {"job_id": job_id, "stream": "stderr", "text": f"\n[Auto-killed after {meta['timeout']}s]\n"}, room=sid or None)
                except Exception:
                    pass
                break
        with proc_lock:
            processes.pop(job_id, None)
        socketio.emit("proc.exit", {"job_id": job_id, "code": p.returncode if p else None}, room=sid or None)

    tw = threading.Thread(target=watchdog, daemon=True)
    tw.start()

    socketio.emit("proc.start", {"job_id": job_id, "cmd": cmd, "cwd": cwd}, room=sid or None)
    return job_id


def stop_subprocess(job_id: str) -> bool:
    with proc_lock:
        meta = processes.get(job_id)
    if not meta:
        return False
    p: subprocess.Popen = meta["proc"]
    try:
        p.kill()
        return True
    except Exception:
        try:
            p.terminate()
            return True
        except Exception:
            return False


# -----------------------
# HTML UI (inlined)
# -----------------------
INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>PySandbox - File Manager & Runners</title>
<style>
:root{--bg:#071022;--panel:#05121a;--muted:#9aa6b2;--accent:#60a5fa;--text:#e6eef8}
[data-theme="light"]{--bg:#f6f8fb;--panel:#ffffff;--muted:#6b7280;--accent:#0ea5a3;--text:#0b1220}
*{box-sizing:border-box;font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,"Helvetica Neue",Arial}
html,body,#app{height:100%;margin:0}
body{background:linear-gradient(180deg,var(--bg),#031022);color:var(--text);padding:12px}
.app{display:flex;gap:12px;height:calc(100vh - 24px)}
.sidebar{width:300px;background:var(--panel);border-radius:10px;padding:12px;display:flex;flex-direction:column}
.top{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.brand{font-weight:700;font-size:18px;flex:1}
.btn{background:transparent;border:1px solid rgba(255,255,255,0.04);color:var(--text);padding:6px 8px;border-radius:8px;cursor:pointer}
.small{padding:6px 8px;font-size:13px}
.files{flex:1;overflow:auto;background:linear-gradient(180deg,#041022,#03121a);padding:8px;border-radius:8px}
.file-item{padding:6px 8px;border-radius:6px;display:flex;align-items:center;gap:8px;cursor:pointer}
.file-item:hover{background:rgba(255,255,255,0.02)}
.main{flex:1;display:flex;flex-direction:column;gap:12px}
.grid{display:grid;grid-template-columns:1fr 420px;gap:12px;min-height:0}
.panel{background:linear-gradient(180deg,#031225,#02111a);border-radius:10px;padding:12px;overflow:auto}
.row{display:flex;gap:8px;align-items:center}
.input{flex:1;padding:8px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.03);color:var(--text)}
.textarea{width:100%;height:160px;background:transparent;padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,0.03);resize:vertical;color:var(--text);font-family:monospace}
.terminal{background:#01060a;color:#e6eef8;padding:8px;border-radius:8px;height:220px;overflow:auto;font-family:monospace;font-size:13px}
.previewLink{display:inline-block;padding:6px 8px;border-radius:6px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.04);color:var(--accent);text-decoration:none}
.footer{display:flex;justify-content:space-between;align-items:center;margin-top:8px;color:var(--muted);font-size:13px}
@media(max-width:900px){.app{flex-direction:column}.grid{grid-template-columns:1fr}}
</style>
</head>
<body data-theme="dark" id="body">
<div id="app" class="app">
  <div id="sidebar" class="sidebar">
    <div class="top">
      <div class="brand">PySandbox</div>
      <div>
        <button id="toggleSidebar" class="btn">☰</button>
      </div>
    </div>

    <div style="display:flex;gap:8px;margin-bottom:8px">
      <button id="openFM" class="btn small">File Manager</button>
      <button id="openPipUI" class="btn small">Pip Installer</button>
    </div>

    <div class="panel" style="padding:8px">
      <div style="font-weight:600;margin-bottom:6px">Quick</div>
      <div style="display:flex;gap:6px;margin-bottom:8px">
        <button id="newFile" class="btn small">New File</button>
        <button id="newFolder" class="btn small">New Folder</button>
      </div>
      <div style="font-weight:600;margin-bottom:6px">Upload</div>
      <input id="upload" type="file" multiple webkitdirectory directory mozdirectory style="display:none"/>
      <div style="display:flex;gap:6px">
        <button id="uploadBtn" class="btn small">Upload</button>
        <button id="downloadAll" class="btn small">Download Zip</button>
      </div>
      <div class="footer" style="margin-top:12px">
        <div>Workspace: <span id="cwdDisp"></span></div>
        <div><button id="themeToggle" class="btn small">Theme</button></div>
      </div>
    </div>
  </div>

  <div class="main">
    <div class="row" style="justify-content:space-between">
      <div style="display:flex;gap:8px">
        <button id="openFileManagerPanel" class="btn">File Manager</button>
        <button id="openPipPanel" class="btn">Pip Installer</button>
        <button id="openTermPanel" class="btn">Terminals</button>
        <button id="openRunnerPanel" class="btn">Single-file Runner</button>
        <button id="openUbuntuTtyd" class="btn">Ubuntu Web Terminal</button>
      </div>
      <div style="color:var(--muted)">Preview links auto-detected from process output</div>
    </div>

    <div class="grid">
      <div id="leftColumn" class="panel">
        <!-- File Manager -->
        <div id="fileManagerSection">
          <div style="font-weight:700;margin-bottom:8px">File Manager</div>
          <div class="row" style="margin-bottom:8px">
            <input id="fm_path" class="input" placeholder="Path (relative)" />
            <button id="fm_new_file" class="btn small">Create</button>
          </div>
          <div id="fileTree" style="max-height:320px;overflow:auto;border-radius:6px;padding:6px;background:rgba(255,255,255,0.01)"></div>
          <div style="margin-top:8px;display:flex;gap:6px">
            <button id="fm_refresh" class="btn small">Refresh</button>
            <button id="fm_delete" class="btn small">Delete</button>
            <button id="fm_rename" class="btn small">Rename</button>
            <button id="fm_download" class="btn small">Download</button>
            <label class="small" style="margin-left:auto">Selected: <span id="fm_selected">/</span></label>
          </div>
        </div>

        <!-- Pip Installer -->
        <div id="pipSection" style="margin-top:12px;display:none">
          <div style="font-weight:700;margin-bottom:8px">Pip Installer</div>
          <div class="row" style="margin-bottom:8px">
            <input id="pip_pkg" class="input" placeholder="package (e.g., requests==2.28.1)" />
            <button id="pip_install" class="btn small">Install</button>
            <button id="pip_requirements" class="btn small">Install requirements.txt</button>
          </div>
          <div style="font-weight:600;margin-bottom:6px">Pip Output</div>
          <div id="pipOutput" class="terminal"></div>
        </div>

        <!-- Runner (paste code) -->
        <div id="runnerSection" style="margin-top:12px;display:none">
          <div style="font-weight:700;margin-bottom:8px">Single-file Runner</div>
          <textarea id="runnerCode" class="textarea" placeholder="# Paste Python code here"></textarea>
          <div style="display:flex;gap:8px;margin-top:6px">
            <input id="runner_timeout" class="input" style="width:120px" placeholder="Timeout(s)" value="60" />
            <button id="runner_run" class="btn small">Run</button>
            <button id="runner_stop" class="btn small">Stop</button>
            <a id="runner_preview" class="previewLink" href="#" target="_blank" style="display:none">Open Preview</a>
          </div>
          <div style="font-weight:600;margin-top:8px">Output</div>
          <div id="runnerOutput" class="terminal"></div>
        </div>
      </div>

      <div id="rightColumn" class="panel">
        <div style="display:flex;gap:8px;align-items:center;justify-content:space-between">
          <div style="font-weight:700">Terminals & Logs</div>
          <div>
            <button id="term_clear" class="btn small">Clear</button>
            <button id="term_run_cmd" class="btn small">Run Command</button>
          </div>
        </div>
        <div style="margin-top:8px">
          <div style="display:flex;gap:8px;margin-bottom:6px">
            <input id="term_cmd" class="input" placeholder="Command (e.g., ls -la, python app.py, python -m pip install req)" />
            <input id="term_cwd" class="input" style="width:180px" placeholder="cwd (relative)" />
          </div>
          <div style="display:flex;gap:8px">
            <div style="flex:1">
              <div style="font-weight:600;margin-bottom:6px">Global Terminal Output</div>
              <div id="globalTerminal" class="terminal"></div>
            </div>
            <div style="width:260px">
              <div style="font-weight:600;margin-bottom:6px">Active Jobs</div>
              <div id="jobList" style="background:rgba(255,255,255,0.01);padding:6px;border-radius:6px;max-height:300px;overflow:auto"></div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;color:var(--muted)">
      <div>Tip: Use the Single-file Runner for quick experiments. For full projects, use the File Manager.</div>
      <div>Port preview links are clickable when a process prints its URL.</div>
    </div>
  </div>
</div>

<input id="hidden_upload" type="file" multiple webkitdirectory directory mozdirectory style="display:none" />

<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
<script>
const socket = io({transports:['websocket'], upgrade:false});
let selectedItem = null;
let currentRunnerJob = null;
let currentPipJob = null;
let activeJobs = {};

function q(id){return document.getElementById(id)}
function appendTerminal(elId, txt){
  const el = q(elId);
  el.textContent += txt;
  el.scrollTop = el.scrollHeight;
}

q('cwdDisp').textContent = "{{WORKDIR}}";

// basic theme toggle
q('themeToggle').addEventListener('click', ()=>{
  const b = document.body; const t = b.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'; b.setAttribute('data-theme', t);
});

q('toggleSidebar').addEventListener('click', ()=> q('sidebar').classList.toggle('collapsed'));

// Panel buttons
q('openFileManagerPanel').addEventListener('click', ()=> { q('fileManagerSection').style.display=''; q('pipSection').style.display='none'; q('runnerSection').style.display='none'; });
q('openPipPanel').addEventListener('click', ()=> { q('fileManagerSection').style.display='none'; q('pipSection').style.display=''; q('runnerSection').style.display='none'; });
q('openRunnerPanel').addEventListener('click', ()=> { q('fileManagerSection').style.display='none'; q('pipSection').style.display='none'; q('runnerSection').style.display=''; });
q('openTermPanel').addEventListener('click', ()=> { q('fileManagerSection').style.display='none'; q('pipSection').style.display='none'; q('runnerSection').style.display='none'; });
q('openUbuntuTtyd').addEventListener('click', ()=> { alert('Ubuntu web terminal is a separate container/service. Deploy the ttyd container and open its configured url/port.'); });

// File manager
async function loadTree(){
  const res = await fetch('/api/tree');
  const data = await res.json();
  q('fileTree').innerHTML = renderTree(data.tree, '');
  q('cwdDisp').textContent = data.base;
}
function renderTree(tree, prefix){
  let html = '';
  for(const node of tree){
    const rel = prefix ? prefix + '/' + node.name : node.name;
    if(node.type === 'dir'){
      html += `<div><div class="file-item" data-path="${rel}" onclick="toggleDir(this)">📁 ${node.name}</div>`;
      if(node.children && node.children.length){
        html += `<div style="margin-left:12px">${renderTree(node.children, rel)}</div>`;
      }
      html += `</div>`;
    } else {
      html += `<div class="file-item" data-path="${rel}" onclick="selectFile('${rel}')">📄 ${node.name}</div>`;
    }
  }
  return html;
}
window.toggleDir = ()=>{}; // placeholder

window.selectFile = async (rel)=>{
  selectedItem = rel;
  q('fm_selected').textContent = rel;
  const res = await fetch('/api/file', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:rel})});
  if(res.ok){
    const j = await res.json();
    if(j.type === 'text' && j.content !== undefined){
      q('runnerCode').value = j.content;
    }
  }
};

q('fm_refresh').addEventListener('click', loadTree);
q('newFile').addEventListener('click', ()=> {
  const name = prompt('New file path (relative):', 'untitled.py');
  if(!name) return;
  fetch('/api/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:name, type:'file'})}).then(r=>{ if(r.ok) loadTree(); else alert('Failed'); });
});
q('newFolder').addEventListener('click', ()=> {
  const name = prompt('New folder path (relative):', 'new_folder');
  if(!name) return;
  fetch('/api/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:name, type:'dir'})}).then(r=>{ if(r.ok) loadTree(); else alert('Failed'); });
});
q('fm_delete').addEventListener('click', ()=> {
  if(!selectedItem){ alert('Select an item'); return; }
  if(!confirm('Delete ' + selectedItem + '?')) return;
  fetch('/api/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:selectedItem})}).then(r=>{ if(r.ok){ loadTree(); selectedItem=null; q('fm_selected').textContent='/'; } else alert('Failed'); });
});
q('fm_rename').addEventListener('click', ()=> {
  if(!selectedItem){ alert('Select'); return; }
  const dst = prompt('New path:', selectedItem);
  if(!dst) return;
  fetch('/api/rename', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({src:selectedItem, dst})}).then(r=>{ if(r.ok){ loadTree(); selectedItem=dst; q('fm_selected').textContent=dst; } else alert('Failed'); });
});
q('fm_download').addEventListener('click', ()=> {
  if(!selectedItem){ alert('Select'); return; }
  window.open('/api/download?path=' + encodeURIComponent(selectedItem), '_blank');
});
q('downloadAll').addEventListener('click', ()=> window.open('/api/download?zip=1','_blank'));

// Upload
q('uploadBtn').addEventListener('click', ()=> q('hidden_upload').click());
q('hidden_upload').addEventListener('change', async (ev)=>{
  const files = Array.from(ev.target.files);
  if(!files.length) return;
  const fd = new FormData();
  for(const f of files) fd.append('files', f, f.webkitRelativePath || f.name);
  const res = await fetch('/api/upload', {method:'POST', body:fd});
  if(res.ok){ alert('Uploaded'); loadTree(); } else { alert('Upload failed'); }
  q('hidden_upload').value = '';
});

// Pip installer
q('openPipUI').addEventListener('click', ()=> { q('fileManagerSection').style.display='none'; q('pipSection').style.display=''; q('runnerSection').style.display='none'; });
q('pip_install').addEventListener('click', ()=> {
  const pkg = q('pip_pkg').value.trim();
  if(!pkg) return alert('Provide package');
  q('pipOutput').textContent = '';
  socket.emit('pip.install', {pkg});
});
q('pip_requirements').addEventListener('click', ()=> {
  q('pipOutput').textContent = '';
  socket.emit('pip.install.requirements', {});
});

// Runner paste
q('runner_run').addEventListener('click', ()=> {
  const code = q('runnerCode').value;
  if(!code) return alert('Paste some code');
  const to = parseInt(q('runner_timeout').value) || 60;
  q('runnerOutput').textContent = '';
  q('runner_preview').style.display = 'none';
  socket.emit('run.paste', {code, timeout: to});
});
q('runner_stop').addEventListener('click', ()=> {
  if(!currentRunnerJob) return;
  socket.emit('stop.process', {job_id: currentRunnerJob});
});

// Global terminal
q('term_run_cmd').addEventListener('click', ()=> runCommand());
q('term_cmd').addEventListener('keydown', (e)=>{ if(e.key === 'Enter'){ runCommand(); }});
function runCommand(){
  const cmd = q('term_cmd').value.trim();
  const cwd = q('term_cwd').value.trim();
  if(!cmd) return;
  socket.emit('run.command', {cmd, cwd});
  q('term_cmd').value = '';
}
q('term_clear').addEventListener('click', ()=> { q('globalTerminal').textContent=''; });

// Socket events
socket.on('connect', ()=> {
  appendTerminal('globalTerminal', '[Connected]\\n');
  loadTree();
});
socket.on('proc.start', (m)=> {
  activeJobs[m.job_id] = m;
  renderJobs();
  appendTerminal('globalTerminal', `[START ${m.job_id}] ${m.cmd.join ? m.cmd.join(' ') : m.cmd}\\n`);
});
socket.on('proc.output', (m)=> {
  appendTerminal('globalTerminal', `[${m.job_id}][${m.stream}] ${m.text}`);
  if(currentRunnerJob && m.job_id === currentRunnerJob) appendTerminal('runnerOutput', m.text);
  if(currentPipJob && m.job_id === currentPipJob) appendTerminal('pipOutput', m.text);
});
socket.on('proc.exit', (m)=> {
  appendTerminal('globalTerminal', `[EXIT ${m.job_id}] code=${m.code}\\n`);
  if(currentRunnerJob === m.job_id) currentRunnerJob = null;
  if(currentPipJob === m.job_id) currentPipJob = null;
  activeJobs[m.job_id] = {...(activeJobs[m.job_id]||{}), exit: m.code};
  renderJobs();
});
socket.on('proc.preview', (m)=> {
  appendTerminal('globalTerminal', `[PREVIEW ${m.job_id}] ${m.url}\\n`);
  if(currentRunnerJob === m.job_id){
    q('runner_preview').href = m.url;
    q('runner_preview').style.display = 'inline-block';
  }
});
socket.on('pip.job', (m)=> { currentPipJob = m.job_id; activeJobs[m.job_id] = m; renderJobs(); });
socket.on('run.paste.job', (m)=> { currentRunnerJob = m.job_id; activeJobs[m.job_id] = m; renderJobs(); });

// Jobs list rendering
function renderJobs(){
  const el = q('jobList');
  el.innerHTML = '';
  const keys = Object.keys(activeJobs);
  if(!keys.length) el.textContent = 'No active jobs';
  for(const k of keys.slice().reverse()){
    const meta = activeJobs[k];
    const div = document.createElement('div');
    div.style.display='flex'; div.style.justifyContent='space-between'; div.style.alignItems='center'; div.style.padding='6px'; div.style.borderBottom='1px solid rgba(255,255,255,0.02)';
    const left = document.createElement('div');
    left.innerHTML = `<div style="font-family:monospace">${k}</div><div style="font-size:12px;color:${meta.exit===undefined?'#9ad':''}">${(meta.cmd||[]).join(' ')}</div>`;
    const right = document.createElement('div');
    const stop = document.createElement('button'); stop.textContent='Stop'; stop.className='btn small'; stop.addEventListener('click', ()=> socket.emit('stop.process',{job_id:k}));
    const view = document.createElement('button'); view.textContent='View'; view.className='btn small'; view.addEventListener('click', ()=> { alert('Job: ' + k + '\\nSee Global Terminal for logs'); });
    right.appendChild(stop); right.appendChild(view);
    div.appendChild(left); div.appendChild(right);
    el.appendChild(div);
  }
}

loadTree();
</script>
</body>
</html>
"""

# -----------------------
# Flask routes (APIs)
# -----------------------
@app.route("/")
def index():
    return render_template_string(INDEX_HTML.replace("{{WORKDIR}}", WORKDIR))


@app.route("/api/tree")
def api_tree():
    base = pathlib.Path(WORKDIR)
    def node(p: pathlib.Path):
        try:
            if p.is_dir():
                children = []
                try:
                    for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                        children.append({"name": c.name, "type": "dir" if c.is_dir() else "file"})
                except Exception:
                    children = []
                return {"name": p.name, "type": "dir", "children": children}
            else:
                return {"name": p.name, "type": "file"}
        except Exception:
            return {"name": p.name, "type": "file"}

    tree = []
    try:
        for p in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            tree.append(node(p))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"base": WORKDIR, "tree": tree})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "files" not in request.files:
        return jsonify({"error": "No files"}), 400
    files = request.files.getlist("files")
    for f in files:
        filename = f.filename
        if not filename:
            continue
        if filename.startswith("/") or filename.startswith(".."):
            return jsonify({"error": "Invalid filename"}), 400
        try:
            target = safe_join(WORKDIR, filename)
        except Exception:
            return jsonify({"error": "Invalid target"}), 400
        os.makedirs(os.path.dirname(target), exist_ok=True)
        f.save(target)
    return jsonify({"ok": True})


@app.route("/api/create", methods=["POST"])
def api_create():
    data = request.get_json() or {}
    path = data.get("path")
    typ = data.get("type", "file")
    if not path:
        return jsonify({"error": "Missing path"}), 400
    try:
        full = safe_join(WORKDIR, path)
    except Exception:
        return jsonify({"error": "Invalid path"}), 400
    try:
        if typ == "dir":
            os.makedirs(full, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "a", encoding="utf-8"):
                pass
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    data = request.get_json() or {}
    path = data.get("path")
    if not path:
        return jsonify({"error": "Missing path"}), 400
    try:
        full = safe_join(WORKDIR, path)
    except Exception:
        return jsonify({"error": "Invalid path"}), 400
    try:
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/rename", methods=["POST"])
def api_rename():
    data = request.get_json() or {}
    src = data.get("src"); dst = data.get("dst")
    if not src or not dst:
        return jsonify({"error": "Missing src/dst"}), 400
    try:
        s = safe_join(WORKDIR, src); d = safe_join(WORKDIR, dst)
    except Exception:
        return jsonify({"error": "Invalid path"}), 400
    try:
        os.makedirs(os.path.dirname(d), exist_ok=True)
        os.replace(s, d)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/file", methods=["POST"])
def api_file():
    data = request.get_json() or {}
    path = data.get("path")
    if not path:
        return jsonify({"error": "Missing path"}), 400
    try:
        full = safe_join(WORKDIR, path)
    except Exception:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.exists(full):
        return jsonify({"error": "Not found"}), 404
    if os.path.isdir(full):
        items = [{"name": n, "type": "dir" if os.path.isdir(os.path.join(full, n)) else "file"} for n in sorted(os.listdir(full))]
        return jsonify({"type": "dir", "children": items})
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return jsonify({"type": "text", "content": content})
    except Exception:
        return jsonify({"type": "binary"})


@app.route("/api/download")
def api_download():
    if request.args.get("zip"):
        import zipfile
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(WORKDIR):
                for f in files:
                    full = os.path.join(root, f)
                    arc = os.path.relpath(full, WORKDIR)
                    zf.write(full, arc)
        mem.seek(0)
        return send_file(mem, mimetype="application/zip", download_name="workspace.zip", as_attachment=True)
    path = request.args.get("path")
    if not path:
        return jsonify({"error": "Missing path"}), 400
    try:
        full = safe_join(WORKDIR, path)
    except Exception:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.exists(full):
        return jsonify({"error": "Not found"}), 404
    return send_file(full, as_attachment=True, download_name=os.path.basename(full))


# -----------------------
# SocketIO handlers
# -----------------------
@socketio.on("run.command")
def on_run_command(msg):
    sid = request.sid
    cmd_text = msg.get("cmd")
    cwd_rel = msg.get("cwd", "")
    if not cmd_text:
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": "No command\n"}, room=sid)
        return
    try:
        parts = shlex.split(cmd_text)
    except Exception:
        parts = cmd_text.split()
    if parts and parts[0] == "pip":
        parts = [sys.executable, "-m", "pip"] + parts[1:]
    if parts and parts[0] == "python":
        parts[0] = sys.executable
    cwd = WORKDIR
    if cwd_rel:
        try:
            cwd = safe_join(WORKDIR, cwd_rel)
        except Exception:
            cwd = WORKDIR
    job_id = start_subprocess(parts, cwd, sid, timeout=DEFAULT_TIMEOUT)
    emit("proc.output", {"job_id": job_id, "stream": "stdout", "text": f"Started job {job_id}\n"}, room=sid)


@socketio.on("pip.install")
def on_pip_install(msg):
    sid = request.sid
    pkg = (msg or {}).get("pkg")
    if not pkg:
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": "No package specified\n"}, room=sid)
        return
    cmd = [sys.executable, "-m", "pip", "install", pkg]
    job_id = start_subprocess(cmd, WORKDIR, sid, timeout=DEFAULT_TIMEOUT * 2)
    emit("pip.job", {"job_id": job_id, "cmd": cmd}, room=sid)


@socketio.on("pip.install.requirements")
def on_pip_install_requirements(_msg):
    sid = request.sid
    req = os.path.join(WORKDIR, "requirements.txt")
    if not os.path.exists(req):
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": "requirements.txt not found\n"}, room=sid)
        return
    cmd = [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"]
    job_id = start_subprocess(cmd, WORKDIR, sid, timeout=DEFAULT_TIMEOUT * 5)
    emit("pip.job", {"job_id": job_id, "cmd": cmd}, room=sid)


@socketio.on("run.paste")
def on_run_paste(msg):
    sid = request.sid
    code = msg.get("code", "")
    timeout = int(msg.get("timeout") or DEFAULT_TIMEOUT)
    if not code:
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": "No code provided\n"}, room=sid)
        return
    run_dir = os.path.join(WORKDIR, "paste_runs")
    os.makedirs(run_dir, exist_ok=True)
    fname = f"paste_{int(time.time())}_{uuid.uuid4().hex[:8]}.py"
    full = os.path.join(run_dir, fname)
    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(code)
    except Exception as e:
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": f"Failed to write temp file: {e}\n"}, room=sid)
        return
    cmd = [sys.executable, full]
    job_id = start_subprocess(cmd, run_dir, sid, timeout=timeout)
    emit("run.paste.job", {"job_id": job_id, "cmd": cmd}, room=sid)


@socketio.on("stop.process")
def on_stop_process(msg):
    sid = request.sid
    job_id = msg.get("job_id")
    if not job_id:
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": "Missing job_id\n"}, room=sid)
        return
    ok = stop_subprocess(job_id)
    if ok:
        emit("proc.output", {"job_id": job_id, "stream": "stdout", "text": f"Requested stop of {job_id}\n"}, room=sid)
    else:
        emit("proc.output", {"job_id": job_id, "stream": "stderr", "text": f"Failed to stop {job_id}\n"}, room=sid)


# -----------------------
# Cleanup
# -----------------------
def cleanup():
    with proc_lock:
        keys = list(processes.keys())
    for k in keys:
        try:
            stop_subprocess(k)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", 5000))
        host = "0.0.0.0"
        print(f"Starting PySandbox on http://{host}:{port} (allow_unsafe_werkzeug={True})")
        # If you want a more production-ready server, run with eventlet/gevent or use gunicorn.
        # Quick fix for hosting platforms like Render: allow the Werkzeug server to run.
        socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("Shutting down, cleaning up processes...")
        cleanup()
        sys.exit(0)
