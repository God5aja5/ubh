#!/usr/bin/env python3
"""
Updated app.py - Adds improved UI and small backend helpers to support the UI.

Notes:
 - Keep using: pip install flask flask-socketio
 - Run: TERMINAL_TOKEN=your_token python app.py
 - Security: This runs commands on the host. Do NOT expose publicly without protection.
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
import shutil
import zipfile
from typing import Dict, Any, Optional
from flask import Flask, jsonify, request, render_template_string, send_file
from flask_socketio import SocketIO, emit

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
    """
    timeout semantics:
      - None => use DEFAULT_TIMEOUT
      - 0 => no timeout (run forever unless killed)
      - >0 => that many seconds
    """
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

    # interpret timeout
    if timeout is None:
        tval = DEFAULT_TIMEOUT
    elif timeout == 0:
        tval = None
    else:
        tval = int(timeout)

    with process_lock:
        processes[job_id] = {"proc": proc, "sid": sid, "start": time.time(), "timeout": tval, "cmd": cmd, "cwd": cwd}

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
# PTY-based terminal helpers (unchanged)
# ----------------------
def spawn_pty_process(term_id: str, sid: str, cols: int = 80, rows: int = 24) -> bool:
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

    try:
        os.close(slave_fd)
    except Exception:
        pass

    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

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
                    time.sleep(0.01)
                    if proc.poll() is not None:
                        break
                    continue
        finally:
            try:
                os.close(master_fd)
            except Exception:
                pass
            socketio.emit("term.exit", {"term_id": term_id}, room=sid)
            with term_lock:
                terminals.pop(term_id, None)

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
# HTML / JS UI (inlined). Responsive & mobile-friendly, orange accents
# ----------------------
INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>PySandbox - Orange UI</title>
<style>
:root{--bg:#0b0a0a;--panel:#101010;--muted:#9aa4ad;--accent:#ff7a18;--text:#f6f6f6}
[data-theme="light"]{--bg:#f6f7f8;--panel:#fff;--muted:#556070;--accent:#ff7a18;--text:#0b1220}
*{box-sizing:border-box;font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial}
html,body{height:100%;margin:0;background:linear-gradient(180deg,var(--bg),#041018);color:var(--text)}
.header{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;background:linear-gradient(90deg, rgba(255,122,24,0.06), transparent);backdrop-filter: blur(6px);}
.brand{font-weight:800;display:flex;align-items:center;gap:8px}
.logo{width:26px;height:26px;border-radius:6px;background:linear-gradient(180deg,var(--accent),#ff9a4a);display:inline-block}
.container{display:flex;gap:12px;padding:12px;height:calc(100vh - 64px)}
.sidebar{width:320px;background:linear-gradient(180deg,#08121a,#051018);padding:12px;border-radius:12px;display:flex;flex-direction:column;gap:12px;min-height:0}
.sidebar.collapsed{width:64px}
.content{flex:1;display:flex;flex-direction:column;gap:12px;min-height:0}
.top-actions{display:flex;gap:8px;align-items:center}
.panel{background:linear-gradient(180deg,#07121a,#051018);border-radius:10px;padding:12px;overflow:auto;min-height:0}
.grid{display:grid;grid-template-columns:1fr 420px;gap:12px;min-height:0}
@media (max-width:980px){
  .container{flex-direction:column;height:auto}
  .sidebar{width:100%;flex-direction:row;overflow:auto}
  .grid{grid-template-columns:1fr}
}
.button{background:transparent;border:1px solid rgba(255,255,255,0.04);color:var(--text);padding:8px;border-radius:8px;cursor:pointer}
.input{padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,0.03);background:transparent;color:var(--text);flex:1}
.terminal{background:#000;padding:8px;border-radius:8px;color:#e6eef8;height:220px;overflow:auto;font-family:monospace}
.xterm-wrapper{height:60vh;border-radius:8px;overflow:hidden;background:#000}
.small{font-size:13px;color:var(--muted)}
.previewLink{color:var(--accent);text-decoration:none}
.section-title{font-weight:700;color:var(--accent)}
.footer-note{font-size:12px;color:var(--muted);margin-top:8px}
.left-menu{display:flex;flex-direction:column;gap:6px}
.left-menu .button{display:flex;align-items:center;gap:8px}
.file-row{display:flex;justify-content:space-between;align-items:center;padding:6px;border-radius:6px}
.file-row:hover{background:rgba(255,255,255,0.02)}
.editor{width:100%;height:60vh;background:transparent;border-radius:6px;padding:8px;border:1px solid rgba(255,255,255,0.03);color:var(--text);font-family:monospace}
.small-btn{padding:6px;border-radius:6px;font-size:13px}
.badge{background:rgba(255,255,255,0.03);padding:4px 6px;border-radius:6px;font-size:13px}
</style>

<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.1.0/css/xterm.css" />
<script src="https://cdn.jsdelivr.net/npm/xterm@5.1.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.7.0/lib/xterm-addon-fit.js"></script>
<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
</head>
<body data-theme="dark">
<div class="header">
  <div class="brand"><span class="logo"></span> PySandbox</div>
  <div style="display:flex;gap:8px;align-items:center">
    <div class="small">Workspace: <span class="badge" id="cwdDisp"></span></div>
    <button id="themeToggle" class="button">Toggle Theme</button>
  </div>
</div>

<div class="container">
  <div id="sidebar" class="sidebar">
    <div class="left-menu">
      <button id="navHome" class="button">Home</button>
      <button id="navTerminal" class="button">Terminal</button>
      <button id="navRunner" class="button">Runner</button>
      <button id="navPip" class="button">Pip Installer</button>
      <button id="navFiles" class="button">File Manager</button>
      <button id="navHost" class="button">Host (24/7)</button>
    </div>
    <div style="margin-top:auto">
      <div class="small">Token-protected: <span id="tokenState"></span></div>
      <div class="footer-note">Be careful: this environment executes host commands.</div>
    </div>
  </div>

  <div class="content">
    <!-- Top: Runner -->
    <div class="panel" id="viewRunner">
      <div class="section-title">Single-file Runner</div>
      <div style="display:flex;gap:8px;margin-top:8px">
        <textarea id="runnerCode" class="editor" placeholder="# Paste Python code here"></textarea>
        <div style="width:360px;display:flex;flex-direction:column;gap:8px">
          <div>
            <div style="display:flex;gap:8px;align-items:center">
              <input id="runnerTimeout" class="input" style="width:130px" placeholder="Timeout(s)" value="60"/>
              <button id="runnerRun" class="button">Run</button>
              <button id="runnerStop" class="button">Stop</button>
              <button id="runnerClear" class="button">Clear Output</button>
            </div>
            <a id="runnerPreview" class="previewLink" href="#" target="_blank" style="display:none;margin-left:8px">Open Preview</a>
          </div>
          <div style="flex:1;display:flex;flex-direction:column">
            <div style="font-weight:600;margin-bottom:6px">Output (interactive)</div>
            <div id="runnerOutput" class="terminal" contenteditable="false" style="flex:1;white-space:pre-wrap;overflow:auto"></div>
            <div style="display:flex;gap:8px;margin-top:8px">
              <input id="runnerStdIn" class="input" placeholder="Send stdin to running job"/>
              <button id="runnerSend" class="button">Send</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Grid: Commands and Pip -->
    <div class="grid">
      <div class="panel" id="viewCommands">
        <div class="section-title">Commands & Jobs</div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
          <input id="cmdInput" class="input" placeholder="Run command (e.g., python -m pip install requests)" />
          <input id="cmdCwd" class="input" style="width:160px" placeholder="cwd (relative)" />
          <button id="cmdRun" class="button">Run</button>
          <button id="cmdClear" class="button">Clear Global Output</button>
        </div>

        <div style="display:flex;gap:8px;margin-top:12px;min-height:0">
          <div style="flex:1;display:flex;flex-direction:column;min-height:0">
            <div style="font-weight:600">Global Output</div>
            <div id="globalOutput" class="terminal" contenteditable="false" style="flex:1;overflow:auto"></div>
            <div style="display:flex;gap:8px;margin-top:8px">
              <input id="globalStdIn" class="input" placeholder="Send stdin to job (job id auto)" />
              <button id="globalSend" class="button">Send</button>
            </div>
          </div>

          <div style="width:220px">
            <div style="font-weight:600">Active Jobs</div>
            <div id="jobList" style="background:rgba(255,255,255,0.01);padding:6px;border-radius:6px;max-height:300px;overflow:auto"></div>
            <div style="margin-top:8px">
              <div style="font-weight:600">Pip Installer</div>
              <div style="display:flex;gap:6px;margin-top:6px">
                <input id="pipPkg" class="input" placeholder="package or -r requirements.txt" />
                <button id="pipInstall" class="button">Install</button>
              </div>
              <div style="display:flex;gap:6px;margin-top:6px">
                <button id="pipClear" class="button small-btn">Clear</button>
              </div>
              <div style="margin-top:6px">
                <div class="small">Pip Output</div>
                <pre id="pipOutput" class="terminal" style="height:140px;overflow:auto"></pre>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="panel" id="viewFiles">
        <div class="section-title">File Manager</div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
          <input id="fmPath" class="input" placeholder="path (relative)" />
          <button id="fmCreateFile" class="button">Create File</button>
          <button id="fmCreateDir" class="button">Create Folder</button>
          <input id="hiddenUpload" type="file" multiple webkitdirectory directory mozdirectory style="display:none"/>
          <button id="fmUpload" class="button">Upload</button>
          <button id="fmRefresh" class="button">Refresh</button>
          <button id="downloadZip" class="button">Download Workspace ZIP</button>
        </div>

        <div style="display:flex;gap:12px;margin-top:12px">
          <div style="flex:1;max-height:420px;overflow:auto;border-radius:6px;padding:8px;background:rgba(255,255,255,0.01)" id="fileTree"></div>
          <div style="width:420px;display:flex;flex-direction:column;gap:8px">
            <div style="font-weight:600">File Editor</div>
            <input id="editorPath" class="input" placeholder="Select a file from tree" />
            <textarea id="editorContent" class="editor" placeholder="File content..." ></textarea>
            <div style="display:flex;gap:8px">
              <button id="fileSave" class="button">Save</button>
              <button id="fileUnzip" class="button">Unzip Here</button>
              <button id="fileDelete" class="button">Delete</button>
              <button id="fileClear" class="button">Clear Editor</button>
              <button id="fileDownload" class="button">Download</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Host section -->
    <div class="panel" id="viewHost" style="display:none">
      <div class="section-title">Host (keep script running)</div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
        <input id="hostPath" class="input" placeholder="script path (relative) e.g. app.py" />
        <button id="hostStart" class="button">Start Host</button>
        <button id="hostStop" class="button">Stop Host</button>
        <button id="hostClear" class="button">Clear Host Output</button>
      </div>
      <div style="margin-top:12px">
        <div style="font-weight:600">Host Output (persistent)</div>
        <pre id="hostOutput" class="terminal" style="height:220px;overflow:auto"></pre>
      </div>
    </div>

  </div>
</div>

<!-- Terminal modal (xterm) -->
<div id="termModal" style="display:none;position:fixed;left:0;top:0;width:100%;height:100%;background:rgba(0,0,0,0.6);align-items:center;justify-content:center">
  <div style="width:95%;height:90%;max-width:1200px;background:var(--panel);border-radius:8px;display:flex;flex-direction:column;overflow:hidden">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px;border-bottom:1px solid rgba(255,255,255,0.02)">
      <div style="display:flex;gap:12px;align-items:center">
        <div style="font-weight:700">Integrated Shell</div>
        <div id="termIdLabel" class="small"></div>
      </div>
      <div>
        <span id="termStatus" class="small">Disconnected</span>
        <button id="termClose" class="button">Close</button>
      </div>
    </div>
    <div style="padding:8px;display:flex;flex-direction:column;height:100%">
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <button id="termKill" class="button">Kill</button>
        <button id="termClear" class="button">Clear</button>
        <input id="tokenInput" class="input" placeholder="Terminal token (if required)" />
        <button id="termConnect" class="button">Connect</button>
      </div>
      <div id="xterm" class="xterm-wrapper"></div>
    </div>
  </div>
</div>

<script>
(function(){
  const socket = io({transports:['websocket'], upgrade:false});
  const cwdDisp = document.getElementById('cwdDisp');
  cwdDisp.textContent = "{{WORKDIR}}";
  document.getElementById('tokenState').textContent = {{ 'true' if TERMINAL_TOKEN else 'false' }};

  // Views
  const viewRunner = document.getElementById('viewRunner');
  const viewCommands = document.getElementById('viewCommands');
  const viewFiles = document.getElementById('viewFiles');
  const viewHost = document.getElementById('viewHost');

  function showView(name){
    viewRunner.style.display = name==='runner' ? 'block' : 'none';
    viewCommands.style.display = name==='commands' ? 'block' : 'none';
    viewFiles.style.display = name==='files' ? 'block' : 'none';
    viewHost.style.display = name==='host' ? 'block' : 'none';
  }

  // nav buttons
  document.getElementById('navHome').onclick = ()=> showView('runner');
  document.getElementById('navTerminal').onclick = ()=> openTerminalModal();
  document.getElementById('navRunner').onclick = ()=> showView('runner');
  document.getElementById('navPip').onclick = ()=> showView('commands');
  document.getElementById('navFiles').onclick = ()=> showView('files');
  document.getElementById('navHost').onclick = ()=> showView('host');

  // initial
  showView('runner');

  // Elements
  const globalOutput = document.getElementById('globalOutput');
  const runnerOutput = document.getElementById('runnerOutput');
  const runnerRun = document.getElementById('runnerRun');
  const runnerStop = document.getElementById('runnerStop');
  const runnerTimeout = document.getElementById('runnerTimeout');
  const runnerPreview = document.getElementById('runnerPreview');
  const runnerClear = document.getElementById('runnerClear');
  const runnerStdIn = document.getElementById('runnerStdIn');
  const runnerSend = document.getElementById('runnerSend');

  const cmdInput = document.getElementById('cmdInput');
  const cmdCwd = document.getElementById('cmdCwd');
  const cmdRun = document.getElementById('cmdRun');
  const cmdClear = document.getElementById('cmdClear');

  const pipPkg = document.getElementById('pipPkg');
  const pipInstall = document.getElementById('pipInstall');
  const pipOutput = document.getElementById('pipOutput');
  const pipClear = document.getElementById('pipClear');

  const fmUpload = document.getElementById('fmUpload');
  const hiddenUpload = document.getElementById('hiddenUpload');
  const fmRefresh = document.getElementById('fmRefresh');
  const fileTree = document.getElementById('fileTree');
  const fmCreateFile = document.getElementById('fmCreateFile');
  const fmCreateDir = document.getElementById('fmCreateDir');
  const fmPath = document.getElementById('fmPath');

  const editorPath = document.getElementById('editorPath');
  const editorContent = document.getElementById('editorContent');
  const fileSave = document.getElementById('fileSave');
  const fileUnzip = document.getElementById('fileUnzip');
  const fileDelete = document.getElementById('fileDelete');
  const fileClear = document.getElementById('fileClear');
  const fileDownload = document.getElementById('fileDownload');
  const downloadZip = document.getElementById('downloadZip');

  const hostPath = document.getElementById('hostPath');
  const hostStart = document.getElementById('hostStart');
  const hostStop = document.getElementById('hostStop');
  const hostOutput = document.getElementById('hostOutput');
  const hostClear = document.getElementById('hostClear');

  // Terminal modal elements
  const termModal = document.getElementById('termModal');
  const openTerminalBtn = document.getElementById('navTerminal');
  const termConnect = document.getElementById('termConnect');
  const termClose = document.getElementById('termClose');
  const tokenInput = document.getElementById('tokenInput');
  const termStatus = document.getElementById('termStatus');
  const termIdLabel = document.getElementById('termIdLabel');
  const termKill = document.getElementById('termKill');
  const termClear = document.getElementById('termClear');
  const xtermContainer = document.getElementById('xterm');

  let term = null;
  let fitAddon = null;
  let currentTermId = null;

  let currentRunnerJob = null;
  let currentPipJob = null;
  let hostJobId = null;
  const activeJobs = {};

  function appendGlobal(txt){
    globalOutput.textContent += txt;
    globalOutput.scrollTop = globalOutput.scrollHeight;
  }
  function appendRunner(txt){
    runnerOutput.textContent += txt;
    runnerOutput.scrollTop = runnerOutput.scrollHeight;
  }
  function appendPip(txt){
    pipOutput.textContent += txt;
    pipOutput.scrollTop = pipOutput.scrollHeight;
  }
  function appendHost(txt){
    hostOutput.textContent += txt;
    hostOutput.scrollTop = hostOutput.scrollHeight;
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
    if (currentPipJob && m.job_id === currentPipJob) appendPip(m.text);
    if (hostJobId && m.job_id === hostJobId) appendHost(m.text);
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
    if (hostJobId === m.job_id) hostJobId = None;
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
    term.onData(data => {
      socket.emit('term.input', {term_id: currentTermId, data});
    });
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

  // UI actions - Terminal
  function openTerminalModal(){
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

  document.getElementById('navTerminal').addEventListener('click', openTerminalModal);

  document.getElementById('termClose').addEventListener('click', () => {
    termModal.style.display = 'none';
    if (currentTermId) {
      socket.emit('term.disconnect', {term_id: currentTermId});
      currentTermId = null;
    }
  });

  document.getElementById('termConnect').addEventListener('click', () => {
    const token = tokenInput.value.trim();
    socket.emit('term.request', {token});
  });

  document.getElementById('termKill').addEventListener('click', () => {
    if (!currentTermId) return;
    socket.emit('term.kill', {term_id: currentTermId});
  });

  document.getElementById('termClear').addEventListener('click', () => { if (term) term.clear(); });

  // Runner actions
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
  runnerClear.addEventListener('click', ()=> { runnerOutput.textContent=''; });
  runnerSend.addEventListener('click', ()=> {
    const text = runnerStdIn.value;
    if (!currentRunnerJob || !text) return;
    socket.emit('proc.input', {job_id: currentRunnerJob, data: text + '\\n'});
    runnerStdIn.value = '';
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
  cmdClear.addEventListener('click', ()=> { globalOutput.textContent=''; });

  document.getElementById('globalSend').addEventListener('click', ()=> {
    const text = document.getElementById('globalStdIn').value;
    if (!text) return;
    // If job id included at start like "<jobid> some input" allow it, else use last active job
    const parts = text.trim().split(' ');
    let jobId = null;
    if (parts[0] && parts[0].length > 10 && activeJobs[parts[0]]) {
      jobId = parts.shift();
    } else {
      // pick last active job
      const keys = Object.keys(activeJobs || {});
      if (keys.length) jobId = keys[keys.length - 1];
    }
    if (!jobId) { alert('No job to send to'); return; }
    const payload = parts.join(' ');
    socket.emit('proc.input', {job_id: jobId, data: payload + '\\n'});
    document.getElementById('globalStdIn').value = '';
  });

  // Pip installer
  pipInstall.addEventListener('click', ()=> {
    const pkg = pipPkg.value.trim();
    if (!pkg) return alert('Provide package or -r requirements.txt');
    pipOutput.textContent = '';
    if (pkg === '-r' || pkg === '-r requirements.txt' || pkg.endsWith('.txt')) {
      // prefer server-side requirements handler if they pass '-r' or a requirements file
      socket.emit('pip.install.requirements', {});
    } else {
      socket.emit('pip.install', {pkg});
    }
  });
  pipClear.addEventListener('click', ()=> { pipOutput.textContent=''; });

  // File manager
  document.getElementById('fmUpload').addEventListener('click', ()=> hiddenUpload.click());
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
    fetch('/api/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:p, type:'file'})}).then(r=> { if (r.ok) loadTree(); else r.json().then(x=>alert(JSON.stringify(x))); });
  });
  fmCreateDir.addEventListener('click', ()=> {
    const p = fmPath.value.trim(); if (!p) return alert('Provide path');
    fetch('/api/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path:p, type:'dir'})}).then(r=> { if (r.ok) loadTree(); else r.json().then(x=>alert(JSON.stringify(x))); });
  });
  fmRefresh.addEventListener('click', loadTree);

  downloadZip.addEventListener('click', ()=> {
    window.location = '/api/download?zip=1';
  });

  async function loadTree(){
    const res = await fetch('/api/tree');
    const j = await res.json();
    fileTree.innerHTML = renderTree(j.tree, '');
    // wire file links
    Array.from(document.getElementsByClassName('file-link')).forEach(el => {
      el.onclick = async (ev) => {
        ev.preventDefault();
        const p = el.getAttribute('data-path');
        editorPath.value = p;
        const r = await fetch('/api/file', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path:p})});
        const j2 = await r.json();
        if (j2.type === 'text') {
          editorContent.value = j2.content;
        } else if (j2.type === 'dir') {
          editorContent.value = '[directory]';
        } else {
          editorContent.value = '[binary or unreadable]';
        }
      };
    });
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
        html += `<div style="padding:6px" class="file-row small">📄 <a href="#" onclick="return false" data-path="${rel}" class="file-link">${node.name}</a> <div><button data-path="${rel}" class="button small-btn dl-btn">DL</button></div></div>`;
      }
    }
    return html;
  }

  // download per-file handlers (delegated)
  fileTree.addEventListener('click', (ev)=> {
    const b = ev.target.closest('.dl-btn');
    if (!b) return;
    const p = b.getAttribute('data-path');
    if (!p) return;
    window.location = '/api/download?path=' + encodeURIComponent(p);
  });

  fileSave.addEventListener('click', async ()=> {
    const p = editorPath.value.trim();
    if (!p) return alert('Provide path');
    const content = editorContent.value;
    const res = await fetch('/api/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path:p, content})});
    if (res.ok) { alert('Saved'); loadTree(); } else { const j = await res.json(); alert(JSON.stringify(j)); }
  });

  fileUnzip.addEventListener('click', async ()=> {
    const p = editorPath.value.trim();
    if (!p) return alert('Provide zip path');
    const res = await fetch('/api/unzip', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path:p})});
    const j = await res.json();
    if (res.ok) { alert('Unzipped'); loadTree(); } else { alert(JSON.stringify(j)); }
  });

  fileDelete.addEventListener('click', async ()=> {
    const p = editorPath.value.trim();
    if (!p) return alert('Provide path');
    if (!confirm('Delete ' + p + '?')) return;
    const res = await fetch('/api/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path:p})});
    const j = await res.json();
    if (res.ok) { alert('Deleted'); loadTree(); editorContent.value=''; editorPath.value=''; } else { alert(JSON.stringify(j)); }
  });

  fileClear.addEventListener('click', ()=> { editorContent.value=''; });
  fileDownload.addEventListener('click', ()=> {
    const p = editorPath.value.trim();
    if (!p) return alert('Select file');
    window.location = '/api/download?path=' + encodeURIComponent(p);
  });

  // Host controls
  hostStart.addEventListener('click', ()=> {
    const p = hostPath.value.trim();
    if (!p) return alert('Provide script path');
    // send host.start via socket
    socket.emit('host.start', {path: p});
  });
  hostStop.addEventListener('click', ()=> {
    socket.emit('host.stop', {});
  });
  hostClear.addEventListener('click', ()=> { hostOutput.textContent=''; });

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
      const stop = document.createElement('button'); stop.className='button small-btn'; stop.textContent='Stop'; stop.onclick = ()=> socket.emit('stop.process', {job_id:k});
      const send = document.createElement('button'); send.className='button small-btn'; send.textContent='Send'; send.onclick = ()=> {
        const txt = prompt('Send stdin to job ' + k);
        if (txt) socket.emit('proc.input', {job_id: k, data: txt + '\\n'});
      };
      right.appendChild(send); right.appendChild(stop);
      div.appendChild(left); div.appendChild(right);
      el.appendChild(div);
    }
  }

  socket.on('proc.start', (m) => { activeJobs[m.job_id] = m; renderJobs(); });
  socket.on('proc.exit', (m) => { if (activeJobs[m.job_id]) activeJobs[m.job_id].exit = m.code; renderJobs(); });

  // handler for host notifications
  socket.on('host.started', (m) => {
    hostJobId = m.job_id;
    appendHost('[host started ' + m.job_id + ']\\n');
  });
  socket.on('host.stopped', (m) => {
    appendHost('[host stopped]\\n');
    hostJobId = null;
  });

  // proc input (for processes with stdin)
  // backend writes to process.stdin when receiving 'proc.input' socket events

  // initial load
  loadTree();

})();
</script>
</body>
</html>
"""

# ----------------------
# Flask API endpoints (keep original functionality, add safe helpers)
# ----------------------
@app.route("/")
@app.route("/terminal")
@app.route("/file-manager")
def index():
    return render_template_string(
        INDEX_HTML.replace("{{WORKDIR}}", WORKDIR).replace("{{ 'true' if TERMINAL_TOKEN else 'false' }}", "true" if TERMINAL_TOKEN else "false")
    )


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


@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.get_json() or {}
    path = data.get("path")
    content = data.get("content", "")
    if not path:
        return jsonify({"error": "Missing path"}), 400
    try:
        full = safe_join(WORKDIR, path)
    except Exception:
        return jsonify({"error": "Invalid path"}), 400
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/unzip", methods=["POST"])
def api_unzip():
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
    try:
        with zipfile.ZipFile(full, "r") as zf:
            zf.extractall(os.path.dirname(full))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/download")
def api_download():
    if request.args.get("zip"):
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
    timeout = msg.get("timeout")
    try:
        timeout = int(timeout) if timeout is not None else DEFAULT_TIMEOUT
    except Exception:
        timeout = DEFAULT_TIMEOUT
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
    # support timeout=0 meaning no timeout
    job_id = start_subprocess(cmd, run_dir, sid, timeout=(0 if timeout == 0 else timeout))
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


@socketio.on("proc.input")
def on_proc_input(msg):
    """
    Write provided data to a running subprocess's stdin (if available).
    msg: {"job_id": str, "data": str}
    """
    sid = request.sid
    job_id = msg.get("job_id")
    data = msg.get("data", "")
    if not job_id or data is None:
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": "Missing job_id or data\n"}, room=sid)
        return
    with process_lock:
        meta = processes.get(job_id)
    if not meta:
        emit("proc.output", {"job_id": job_id, "stream": "stderr", "text": "Job not found\n"}, room=sid)
        return
    p: subprocess.Popen = meta["proc"]
    try:
        if p.stdin:
            p.stdin.write(data.encode())
            p.stdin.flush()
            emit("proc.output", {"job_id": job_id, "stream": "stdout", "text": f"[stdin sent] {data}"}, room=sid)
        else:
            emit("proc.output", {"job_id": job_id, "stream": "stderr", "text": "No stdin for process\n"}, room=sid)
    except Exception as e:
        emit("proc.output", {"job_id": job_id, "stream": "stderr", "text": f"Failed to write stdin: {e}\n"}, room=sid)


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
# Host (start a persistent script)
# ----------------------
@socketio.on("host.start")
def on_host_start(message):
    sid = request.sid
    path = (message or {}).get("path", "").strip()
    if not path:
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": "Missing host path\n"}, room=sid)
        return
    try:
        full = safe_join(WORKDIR, path)
    except Exception:
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": "Invalid path\n"}, room=sid)
        return
    if not os.path.exists(full):
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": "Not found\n"}, room=sid)
        return
    cmd = [sys.executable, full]
    # no timeout (0)
    job_id = start_subprocess(cmd, os.path.dirname(full) or WORKDIR, sid, timeout=0)
    emit("host.started", {"job_id": job_id}, room=sid)


@socketio.on("host.stop")
def on_host_stop(_message):
    sid = request.sid
    # attempt to stop any process that was started as host (best-effort)
    with process_lock:
        # heuristic: hosted jobs are ones with timeout None and cwd in workspace (or where cmd points to a file)
        hosted = [k for k, v in processes.items() if v.get("timeout") is None]
    ok_any = False
    for k in hosted:
        ok = stop_subprocess(k)
        ok_any = ok_any or ok
    emit("host.stopped", {"ok": ok_any}, room=sid)


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
