#!/usr/bin/env python3
"""
app.py - Single-file Flask + Flask-SocketIO sandbox with integrated web PTY terminal,
file manager, pip installer, single-file runner, and responsive mobile-first UI.

Dependencies:
    pip install flask flask-socketio

Run:
    TERMINAL_TOKEN=your_token python app.py
or
    python app.py

Security:
- This runs commands and shells on the host. Do NOT expose publicly without strong authentication
  and additional isolation (containers, VMs, network rules).
- If you set TERMINAL_TOKEN env var, the browser must provide that token to open the integrated terminal.

Fixes / Improvements in this version:
- Replaced unsafe pty.fork usage with pty.openpty() + subprocess to avoid forking in threads.
- Use socketio.start_background_task for reader loops (thread-safe with SocketIO).
- Emit terminal events reliably and only after background task started.
- Improved CSS for responsive mobile layout and collapsible sidebar.
- Various robustness fixes: non-blocking reads, proper cleanup, and safe path handling.

Works on Python 3.10+ (Unix recommended for PTY behavior).
"""

import os
import sys
import io
import time
import uuid
import shlex
import fcntl
import struct
import termios
import pathlib
import threading
import subprocess
from typing import Dict, Any, Optional
from flask import Flask, jsonify, request, render_template_string, send_file
from flask_socketio import SocketIO, emit, start_background_task

# Optional resource for UNIX resource limits
try:
    import resource  # type: ignore
except Exception:
    resource = None

# ----------------------
# Config
# ----------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(BASE_DIR, "workspace")
os.makedirs(WORKDIR, exist_ok=True)

DEFAULT_TIMEOUT = 120
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
CPU_TIME_LIMIT = 60

TERMINAL_TOKEN = os.environ.get("TERMINAL_TOKEN", "").strip()

# Flask + SocketIO
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Process registries
processes: Dict[str, Dict[str, Any]] = {}
process_lock = threading.Lock()

# Terminal registry: term_id -> meta (master_fd, subprocess, background task)
terminals: Dict[str, Dict[str, Any]] = {}
term_lock = threading.Lock()


# ----------------------
# Utilities
# ----------------------
def safe_join(base: str, *paths: str) -> str:
    base = os.path.abspath(base)
    candidate = os.path.abspath(os.path.join(base, *paths))
    if not candidate.startswith(base):
        raise ValueError("Invalid path")
    return candidate


def set_limits_for_child():
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


def human_size(num: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if num < 1024.0:
            return f"{num:.0f}{unit}"
        num /= 1024.0
    return f"{num:.0f}TB"


def detect_preview_url(text: str) -> Optional[str]:
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
            return m.group(1).replace("0.0.0.0", "127.0.0.1")
    m = re.search(r"Running on (http://[^\s]+)", text)
    if m:
        return m.group(1).replace("0.0.0.0", "127.0.0.1")
    return None


# ----------------------
# Subprocess management (for commands and paste-run & pip)
# ----------------------
def _stream_reader(proc: subprocess.Popen, job_id: str, sid: Optional[str], stream_name: str, stream):
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
        socketio.emit("proc.output", {"job_id": job_id, "stream": "stderr", "text": f"[stream error] {e}\n"}, room=sid or None)


def start_subprocess(cmd: list, cwd: str, sid: Optional[str], timeout: Optional[int] = None) -> str:
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

    with process_lock:
        processes[job_id] = {"proc": proc, "sid": sid, "start": time.time(), "timeout": timeout or DEFAULT_TIMEOUT, "cmd": cmd, "cwd": cwd}

    # background readers
    socketio.start_background_task(_stream_reader, proc, job_id, sid, "stdout", proc.stdout)
    socketio.start_background_task(_stream_reader, proc, job_id, sid, "stderr", proc.stderr)

    # watchdog
    def watchdog():
        while True:
            time.sleep(0.5)
            with process_lock:
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
        with process_lock:
            processes.pop(job_id, None)
        socketio.emit("proc.exit", {"job_id": job_id, "code": p.returncode if p else None}, room=sid or None)

    socketio.start_background_task(watchdog)
    socketio.emit("proc.start", {"job_id": job_id, "cmd": cmd, "cwd": cwd}, room=sid or None)
    return job_id


def stop_subprocess(job_id: str) -> bool:
    with process_lock:
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


# ----------------------
# PTY-based integrated terminal (safe spawn with pty.openpty + subprocess)
# ----------------------
def spawn_pty_process(term_id: str, sid: str, cols: int = 80, rows: int = 24) -> bool:
    """
    Allocates a new PTY pair via pty.openpty(), then launches a shell subprocess
    bound to the slave FD. Parent reads/writes to master_fd.
    This avoids performing os.fork() in threads which can be unsafe in multi-threaded apps.
    """
    try:
        import pty as _pty  # local import for clarity
    except Exception:
        socketio.emit("term.error", {"msg": "PTY support not available"}, room=sid)
        return False

    master_fd, slave_fd = _pty.openpty()

    shell = os.environ.get("SHELL", "/bin/bash")
    preexec = None
    if os.name != "nt" and resource:
        preexec = set_limits_for_child

    # Start subprocess using slave_fd as its stdio
    try:
        proc = subprocess.Popen(
            [shell, "-i"],
            preexec_fn=preexec,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=WORKDIR,
            close_fds=True,
        )
    except Exception as e:
        try:
            os.close(master_fd)
            os.close(slave_fd)
        except Exception:
            pass
        socketio.emit("term.error", {"msg": f"Failed to spawn shell: {e}"}, room=sid)
        return False

    # close slave_fd in parent (it's used by child)
    try:
        os.close(slave_fd)
    except Exception:
        pass

    # Set master fd non-blocking
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    # reader loop - runs in background task
    def reader():
        try:
            while True:
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                    try:
                        text = data.decode(errors="replace")
                    except Exception:
                        text = str(data)
                    socketio.emit("term.output", {"term_id": term_id, "data": text}, room=sid)
                except OSError:
                    # EAGAIN or other non-fatal; sleep a bit
                    time.sleep(0.01)
                    # check if process ended
                    if proc.poll() is not None:
                        break
                    continue
        finally:
            # cleanup
            try:
                os.close(master_fd)
            except Exception:
                pass
            socketio.emit("term.exit", {"term_id": term_id}, room=sid)
            with term_lock:
                terminals.pop(term_id, None)

    # register terminal meta and start reader background task
    with term_lock:
        terminals[term_id] = {"master_fd": master_fd, "proc": proc, "sid": sid}

    socketio.start_background_task(reader)
    return True


def write_to_master(term_id: str, data: str) -> bool:
    with term_lock:
        meta = terminals.get(term_id)
    if not meta:
        return False
    fd = meta.get("master_fd")
    if fd is None:
        return False
    try:
        os.write(fd, data.encode())
        return True
    except Exception:
        return False


def resize_pty(term_id: str, cols: int, rows: int) -> bool:
    with term_lock:
        meta = terminals.get(term_id)
    if not meta:
        return False
    fd = meta.get("master_fd")
    if fd is None:
        return False
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        return True
    except Exception:
        return False


def kill_terminal(term_id: str) -> bool:
    with term_lock:
        meta = terminals.get(term_id)
    if not meta:
        return False
    proc = meta.get("proc")
    if proc:
        try:
            proc.kill()
            return True
        except Exception:
            try:
                proc.terminate()
                return True
            except Exception:
                return False
    return False


# ----------------------
# HTML / JS UI (inlined). Responsive & mobile-friendly tweaks
# ----------------------
INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>PySandbox - Integrated Terminal + File Manager</title>
<style>
:root{--bg:#071022;--panel:#061427;--muted:#95a3b3;--accent:#60a5fa;--text:#e6eef8}
[data-theme="light"]{--bg:#f6f8fb;--panel:#fff;--muted:#556070;--accent:#0ea5a3;--text:#0b1220}
*{box-sizing:border-box;font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial}
html,body{height:100%;margin:0;background:linear-gradient(180deg,var(--bg),#021022);color:var(--text)}
.header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:var(--panel);}
.brand{font-weight:700}
.container{display:flex;gap:12px;padding:12px;height:calc(100vh - 72px)}
.sidebar{width:300px;background:linear-gradient(180deg,#041426,#03121a);padding:12px;border-radius:10px;display:flex;flex-direction:column;gap:12px}
.sidebar.collapsed{width:64px}
.content{flex:1;display:flex;flex-direction:column;gap:12px}
.top-actions{display:flex;gap:8px;align-items:center}
.panel{background:linear-gradient(180deg,#031225,#02111a);border-radius:10px;padding:12px;overflow:auto}
.grid{display:grid;grid-template-columns:1fr 420px;gap:12px;min-height:0}
@media (max-width:900px){
  .container{flex-direction:column;height:auto}
  .sidebar{width:100%;flex-direction:row;overflow:auto}
  .grid{grid-template-columns:1fr}
}
.button{background:transparent;border:1px solid rgba(255,255,255,0.04);color:var(--text);padding:8px;border-radius:8px;cursor:pointer}
.input{padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,0.03);background:transparent;color:var(--text);flex:1}
.terminal{background:#000;padding:8px;border-radius:8px;color:#e6eef8;height:220px;overflow:auto;font-family:monospace}
.xterm-wrapper{height:60vh;border-radius:8px;overflow:hidden;background:#000}
.term-modal{position:fixed;left:0;top:0;width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.6)}
.term-box{width:95%;height:85%;max-width:1200px;background:#071227;border-radius:8px;display:flex;flex-direction:column;overflow:hidden}
.term-header{display:flex;align-items:center;justify-content:space-between;padding:8px;background:#071827;color:#a8b3bd}
.term-body{flex:1;display:flex;flex-direction:column;gap:8px;padding:8px}
.small{font-size:13px;color:var(--muted)}
.previewLink{color:var(--accent);text-decoration:none}
</style>

<!-- xterm.js and addons -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.1.0/css/xterm.css" />
<script src="https://cdn.jsdelivr.net/npm/xterm@5.1.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.7.0/lib/xterm-addon-fit.js"></script>
<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
</head>
<body data-theme="dark">
<div class="header">
  <div class="brand">PySandbox</div>
  <div>
    <button id="themeToggle" class="button">Toggle Theme</button>
  </div>
</div>

<div class="container">
  <div id="sidebar" class="sidebar">
    <div style="display:flex;flex-direction:column;gap:8px">
      <button id="openTerminalBtn" class="button">Open Terminal</button>
      <button id="openRunnerBtn" class="button">Single-file Runner</button>
      <button id="openPipBtn" class="button">Pip Installer</button>
      <button id="openFMBtn" class="button">File Manager</button>
    </div>
    <div style="margin-top:auto" class="small">Workspace: <span id="cwdDisp"></span></div>
  </div>

  <div class="content">
    <div class="top-actions">
      <div style="font-weight:700">Integrated Terminal & Tools</div>
      <div style="flex:1"></div>
      <div class="small">Token-protected: <span id="tokenState"></span></div>
    </div>

    <div class="grid">
      <div class="panel">
        <div style="font-weight:700;margin-bottom:8px">Single-file Runner</div>
        <textarea id="runnerCode" style="width:100%;height:180px;background:transparent;border-radius:6px;padding:8px;border:1px solid rgba(255,255,255,0.03);color:var(--text);font-family:monospace" placeholder="# Paste Python code here"></textarea>
        <div style="display:flex;gap:8px;margin-top:8px;align-items:center">
          <input id="runnerTimeout" class="input" style="width:120px" placeholder="Timeout(s)" value="60"/>
          <button id="runnerRun" class="button">Run</button>
          <button id="runnerStop" class="button">Stop</button>
          <a id="runnerPreview" class="previewLink" href="#" target="_blank" style="display:none;margin-left:8px">Open Preview</a>
        </div>
        <div style="margin-top:12px">
          <div style="font-weight:600">Output</div>
          <pre id="runnerOutput" class="terminal"></pre>
        </div>
      </div>

      <div class="panel">
        <div style="font-weight:700;margin-bottom:8px">Terminals & Logs</div>
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
          <input id="cmdInput" class="input" placeholder="Run command (e.g., python -m pip install requests)" />
          <input id="cmdCwd" class="input" style="width:160px" placeholder="cwd (relative)" />
          <button id="cmdRun" class="button">Run</button>
        </div>
        <div style="display:flex;gap:8px">
          <div style="flex:1">
            <div style="font-weight:600">Global Output</div>
            <pre id="globalOutput" class="terminal"></pre>
          </div>
          <div style="width:200px">
            <div style="font-weight:600">Active Jobs</div>
            <div id="jobList" style="background:rgba(255,255,255,0.01);padding:6px;border-radius:6px;max-height:300px;overflow:auto"></div>
          </div>
        </div>
      </div>
    </div>

    <div style="margin-top:12px" class="panel">
      <div style="display:flex;gap:8px;align-items:center;justify-content:space-between">
        <div style="font-weight:700">File Manager</div>
        <div class="small">Upload files/folders, create, delete, download</div>
      </div>
      <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
        <input id="fmPath" class="input" placeholder="path (relative)" />
        <button id="fmCreateFile" class="button">Create File</button>
        <button id="fmCreateDir" class="button">Create Folder</button>
        <input id="hiddenUpload" type="file" multiple webkitdirectory directory mozdirectory style="display:none"/>
        <button id="fmUpload" class="button">Upload</button>
        <button id="fmRefresh" class="button">Refresh</button>
      </div>
      <div id="fileTree" style="margin-top:8px;max-height:240px;overflow:auto;border-radius:6px;padding:8px;background:rgba(255,255,255,0.01)"></div>
    </div>

  </div>
</div>

<!-- Terminal modal -->
<div id="termModal" class="term-modal" style="display:none">
  <div class="term-box" role="dialog" aria-modal="true">
    <div class="term-header">
      <div>Integrated Shell <span id="termIdLabel" class="small"></span></div>
      <div>
        <span id="termStatus" class="small">Disconnected</span>
        <button id="termClose" class="button">Close</button>
      </div>
    </div>
    <div class="term-body">
      <div style="display:flex;gap:8px;align-items:center">
        <div style="display:flex;gap:6px">
          <button id="termKill" class="button">Kill</button>
          <button id="termClear" class="button">Clear</button>
        </div>
        <div style="margin-left:auto;display:flex;gap:6px;align-items:center">
          <input id="tokenInput" class="input" placeholder="Terminal token (if required)" />
          <button id="termConnect" class="button">Connect</button>
        </div>
      </div>
      <div id="xterm" class="xterm-wrapper"></div>
    </div>
  </div>
</div>

<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.1.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.7.0/lib/xterm-addon-fit.js"></script>
<script>
(function(){
  const socket = io({transports:['websocket'], upgrade:false});
  const cwdDisp = document.getElementById('cwdDisp');
  cwdDisp.textContent = "{{WORKDIR}}";
  document.getElementById('tokenState').textContent = {{ 'true' if TERMINAL_TOKEN else 'false' }};

  // Elements
  const globalOutput = document.getElementById('globalOutput');
  const runnerOutput = document.getElementById('runnerOutput');
  const runnerRun = document.getElementById('runnerRun');
  const runnerStop = document.getElementById('runnerStop');
  const runnerTimeout = document.getElementById('runnerTimeout');
  const runnerPreview = document.getElementById('runnerPreview');

  const cmdInput = document.getElementById('cmdInput');
  const cmdCwd = document.getElementById('cmdCwd');
  const cmdRun = document.getElementById('cmdRun');

  const termModal = document.getElementById('termModal');
  const openTerminalBtn = document.getElementById('openTerminalBtn');
  const termConnect = document.getElementById('termConnect');
  const termClose = document.getElementById('termClose');
  const tokenInput = document.getElementById('tokenInput');
  const termStatus = document.getElementById('termStatus');
  const termIdLabel = document.getElementById('termIdLabel');
  const termKill = document.getElementById('termKill');
  const termClear = document.getElementById('termClear');

  const fmUpload = document.getElementById('fmUpload');
  const hiddenUpload = document.getElementById('hiddenUpload');
  const fmRefresh = document.getElementById('fmRefresh');
  const fileTree = document.getElementById('fileTree');
  const fmCreateFile = document.getElementById('fmCreateFile');
  const fmCreateDir = document.getElementById('fmCreateDir');
  const fmPath = document.getElementById('fmPath');

  const xtermContainer = document.getElementById('xterm');
  let term = null;
  let fitAddon = null;
  let currentTermId = null;

  let currentRunnerJob = null;
  let currentPipJob = null;
  const activeJobs = {};

  function appendGlobal(txt){
    globalOutput.textContent += txt;
    globalOutput.scrollTop = globalOutput.scrollHeight;
  }
  function appendRunner(txt){
    runnerOutput.textContent += txt;
    runnerOutput.scrollTop = runnerOutput.scrollHeight;
  }

  // Socket events
  socket.on('connect', () => {
    appendGlobal('[connected]\\n');
    loadTree();
  });

  socket.on('proc.output', (m) => {
    const out = `[${m.job_id}][${m.stream}] ${m.text}`;
    appendGlobal(out);
    if (currentRunnerJob && m.job_id === currentRunnerJob) appendRunner(m.text);
    if (currentPipJob && m.job_id === currentPipJob) appendGlobal(m.text);
  });

  socket.on('proc.start', (m) => {
    activeJobs[m.job_id] = m;
    renderJobs();
    appendGlobal(`[start ${m.job_id}] ${m.cmd.join ? m.cmd.join(' ') : m.cmd}\\n`);
  });

  socket.on('proc.exit', (m) => {
    appendGlobal(`[exit ${m.job_id}] code=${m.code}\\n`);
    if (currentRunnerJob === m.job_id) currentRunnerJob = null;
    if (currentPipJob === m.job_id) currentPipJob = null;
    activeJobs[m.job_id] = {...(activeJobs[m.job_id]||{}), exit: m.code};
    renderJobs();
  });

  socket.on('proc.preview', (m) => {
    appendGlobal(`[preview ${m.job_id}] ${m.url}\\n`);
    if (currentRunnerJob === m.job_id){
      runnerPreview.href = m.url;
      runnerPreview.style.display = 'inline-block';
    }
  });

  socket.on('pip.job', (m) => {
    currentPipJob = m.job_id;
    activeJobs[m.job_id] = m;
    renderJobs();
  });

  socket.on('run.paste.job', (m) => {
    currentRunnerJob = m.job_id;
    activeJobs[m.job_id] = m;
    renderJobs();
  });

  // Terminal events
  socket.on('term.ready', (m) => {
    currentTermId = m.term_id;
    termIdLabel.textContent = m.term_id;
    termStatus.textContent = 'Connected';
    // Hook xterm to send input events
    term.onData(data => {
      socket.emit('term.input', {term_id: currentTermId, data});
    });
    // send resize
    setTimeout(()=> {
      fitAddon.fit();
      socket.emit('term.resize', {term_id: currentTermId, cols: term.cols, rows: term.rows});
    }, 200);
  });

  socket.on('term.output', (m) => {
    if (!currentTermId) return;
    if (m.term_id !== currentTermId) return;
    term.write(m.data);
  });

  socket.on('term.exit', (m) => {
    appendGlobal(`[term exited ${m.term_id}]\\n`);
    if (m.term_id === currentTermId) {
      termStatus.textContent = 'Exited';
      currentTermId = null;
      termIdLabel.textContent = '';
    }
  });

  socket.on('term.error', (m) => {
    appendGlobal(`[term error] ${m.msg}\\n`);
    termStatus.textContent = 'Error';
  });

  socket.on('term.token.required', (m) => {
    appendGlobal('[terminal token required]\\n');
    termStatus.textContent = 'Token required';
  });

  // UI actions
  openTerminalBtn.addEventListener('click', () => {
    showTermModal();
  });

  function showTermModal(){
    termModal.style.display = 'flex';
    if (!term) {
      term = new Terminal();
      fitAddon = new FitAddon.FitAddon();
      term.loadAddon(fitAddon);
      term.open(xtermContainer);
      setTimeout(()=> fitAddon.fit(), 200);
      window.addEventListener('resize', ()=> fitAddon.fit());
    }
    term.clear();
    termStatus.textContent = 'Disconnected';
    termIdLabel.textContent = '';
  }

  termClose.addEventListener('click', () => {
    termModal.style.display = 'none';
    if (currentTermId) {
      socket.emit('term.disconnect', {term_id: currentTermId});
      currentTermId = null;
    }
  });

  termConnect.addEventListener('click', () => {
    const token = tokenInput.value.trim();
    socket.emit('term.request', {token});
  });

  termKill.addEventListener('click', () => {
    if (!currentTermId) return;
    socket.emit('term.kill', {term_id: currentTermId});
  });

  termClear.addEventListener('click', () => { if (term) term.clear(); });

  // Runner
  runnerRun.addEventListener('click', () => {
    const code = document.getElementById('runnerCode').value;
    const timeout = parseInt(runnerTimeout.value) || 60;
    if (!code) { alert('Paste code'); return; }
    runnerOutput.textContent = '';
    runnerPreview.style.display = 'none';
    socket.emit('run.paste', {code, timeout});
  });
  runnerStop.addEventListener('click', () => {
    if (!currentRunnerJob) return;
    socket.emit('stop.process', {job_id: currentRunnerJob});
  });

  // Commands
  cmdRun.addEventListener('click', () => runCmd());
  cmdInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') runCmd(); });
  function runCmd(){
    const cmd = cmdInput.value.trim();
    const cwd = cmdCwd.value.trim();
    if (!cmd) return;
    socket.emit('run.command', {cmd, cwd});
    cmdInput.value = '';
  }

  // File manager
  fmUpload.addEventListener('click', ()=> hiddenUpload.click());
  hiddenUpload.addEventListener('change', async (ev) => {
    const files = Array.from(ev.target.files);
    if (!files.length) return;
    const fd = new FormData();
    for (const f of files) fd.append('files', f, f.webkitRelativePath || f.name);
    const res = await fetch('/api/upload', {method:'POST', body: fd});
    if (res.ok) { alert('Uploaded'); loadTree(); } else { alert('Upload failed'); }
    hiddenUpload.value = '';
  });

  fmCreateFile.addEventListener('click', ()=> {
    const p = fmPath.value.trim(); if (!p) return alert('Provide path');
    fetch('/api/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:p, type:'file'})}).then(r=> { if (r.ok) loadTree(); else alert('Failed'); });
  });
  fmCreateDir.addEventListener('click', ()=> {
    const p = fmPath.value.trim(); if (!p) return alert('Provide path');
    fetch('/api/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:p, type:'dir'})}).then(r=> { if (r.ok) loadTree(); else alert('Failed'); });
  });
  fmRefresh.addEventListener('click', loadTree);

  async function loadTree(){
    const res = await fetch('/api/tree');
    const j = await res.json();
    fileTree.innerHTML = renderTree(j.tree, '');
  }

  function renderTree(tree, prefix){
    let html = '';
    for (const node of tree){
      const rel = prefix ? prefix + '/' + node.name : node.name;
      if (node.type === 'dir'){
        html += `<div style="margin-left:0px"><div style="padding:6px" class="small">📁 ${node.name}</div>`;
        if (node.children && node.children.length) html += `<div style="margin-left:12px">${renderTree(node.children, rel)}</div>`;
        html += `</div>`;
      } else {
        html += `<div style="padding:6px" class="small">📄 <a href="#" onclick="return false" data-path="${rel}" class="file-link">${node.name}</a></div>`;
      }
    }
    return html;
  }

  // job list
  function renderJobs(){
    const el = document.getElementById('jobList');
    el.innerHTML = '';
    const keys = Object.keys(activeJobs || {});
    if (!keys.length) { el.textContent = 'No active jobs'; return; }
    for (const k of keys.slice().reverse()){
      const meta = activeJobs[k];
      const div = document.createElement('div');
      div.style.display='flex'; div.style.justifyContent='space-between'; div.style.alignItems='center'; div.style.padding='6px'; div.style.borderBottom='1px solid rgba(255,255,255,0.02)';
      const left = document.createElement('div'); left.style.fontFamily='monospace'; left.textContent = k;
      const right = document.createElement('div');
      const stop = document.createElement('button'); stop.className='button'; stop.textContent='Stop'; stop.onclick = ()=> socket.emit('stop.process', {job_id:k});
      right.appendChild(stop);
      div.appendChild(left); div.appendChild(right);
      el.appendChild(div);
    }
  }

  // helper to expose activeJobs updates from socket handlers
  socket.on('proc.start', (m) => { activeJobs[m.job_id] = m; renderJobs(); });
  socket.on('proc.exit', (m) => { if (activeJobs[m.job_id]) activeJobs[m.job_id].exit = m.code; renderJobs(); });

  // initial
  loadTree();

})();
</script>
</body>
</html>
"""

# ----------------------
# Flask API endpoints
# ----------------------
@app.route("/")
def index():
    return render_template_string(INDEX_HTML.replace("{{WORKDIR}}", WORKDIR).replace("{{ 'true' if TERMINAL_TOKEN else 'false' }}", "true" if TERMINAL_TOKEN else "false"))


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


# ----------------------
# SocketIO handlers (commands, pip, paste-run)
# ----------------------
@socketio.on("run.command")
def on_run_command(message):
    sid = request.sid
    cmd_text = message.get("cmd")
    cwd_rel = message.get("cwd", "")
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


# ----------------------
# SocketIO handlers for integrated terminal (PTY)
# ----------------------
@socketio.on("term.request")
def on_term_request(message):
    sid = request.sid
    token = (message or {}).get("token", "") or ""
    if TERMINAL_TOKEN:
        if token != TERMINAL_TOKEN:
            emit("term.token.required", {"msg": "Invalid token"}, room=sid)
            return
    term_id = str(uuid.uuid4())
    # spawn PTY-backed shell safely (uses pty.openpty + subprocess)
    ok = spawn_pty_process(term_id, sid, cols=80, rows=24)
    if ok:
        emit("term.ready", {"term_id": term_id}, room=sid)
    else:
        emit("term.error", {"msg": "Failed to create terminal"}, room=sid)


@socketio.on("term.input")
def on_term_input(message):
    term_id = message.get("term_id")
    data = message.get("data", "")
    if not term_id or data is None:
        return
    write_to_master(term_id, data)


@socketio.on("term.resize")
def on_term_resize(message):
    term_id = message.get("term_id")
    cols = int(message.get("cols", 80))
    rows = int(message.get("rows", 24))
    if not term_id:
        return
    resize_pty(term_id, cols, rows)


@socketio.on("term.kill")
def on_term_kill(message):
    term_id = message.get("term_id")
    if not term_id:
        return
    ok = kill_terminal(term_id)
    emit("term.killed", {"term_id": term_id, "ok": ok}, room=request.sid)


@socketio.on("term.disconnect")
def on_term_disconnect(message):
    term_id = message.get("term_id")
    if not term_id:
        return
    with term_lock:
        meta = terminals.get(term_id)
    if not meta:
        return
    proc = meta.get("proc")
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    with term_lock:
        terminals.pop(term_id, None)
    emit("term.exit", {"term_id": term_id}, room=request.sid)


# ----------------------
# Cleanup
# ----------------------
def cleanup():
    with process_lock:
        keys = list(processes.keys())
    for k in keys:
        try:
            stop_subprocess(k)
        except Exception:
            pass
    with term_lock:
        for tid, meta in list(terminals.items()):
            try:
                proc = meta.get("proc")
                if proc:
                    proc.kill()
            except Exception:
                pass
            try:
                fd = meta.get("master_fd")
                if fd:
                    os.close(fd)
            except Exception:
                pass
        terminals.clear()


# ----------------------
# Run
# ----------------------
if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", 5000))
        host = "0.0.0.0"
        print(f"Starting PySandbox on http://{host}:{port} (TERMINAL_TOKEN set: {bool(TERMINAL_TOKEN)})")
        socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("Shutting down, cleaning up...")
        cleanup()
        sys.exit(0)
