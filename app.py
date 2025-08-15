#!/usr/bin/env python3
"""
app.py - Single-file Flask + Flask-SocketIO sandbox with integrated web terminal (pty),
file manager, pip installer, single-file runner, and terminals — all UI inlined.

Requirements:
    pip install flask flask-socketio

Notes / Security:
- This app allows executing commands and an interactive shell inside subprocesses on the host.
  Do NOT expose to the public without proper authentication, network restrictions and sandboxing.
- No hard-coded passwords are included. To protect the integrated terminal set TERMINAL_TOKEN
  environment variable (a secret string) and the UI will require that token to open the terminal.
- On UNIX, the interactive terminal uses os.forkpty to allocate a PTY and runs /bin/bash (or shell).
  On Windows the app falls back to a non-pty subprocess (less interactive).
- Resource limits are applied (UNIX only) to child processes (CPU/time/memory).
- The app is intended for local / private use only.

Run:
    TERMINAL_TOKEN=some_secret python app.py
or without token (development only):
    python app.py

Open: http://127.0.0.1:5000

Author: Generated for user request
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
from flask import Flask, jsonify, request, render_template_string, send_file, abort
from flask_socketio import SocketIO, emit, join_room, leave_room

# UNIX-only resource module
try:
    import resource  # type: ignore
except Exception:
    resource = None

# PTY imports
try:
    import pty
    import tty
    import termios
    import fcntl
    import struct
except Exception:
    pty = None

# Config
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(BASE_DIR, "workspace")
os.makedirs(WORKDIR, exist_ok=True)

DEFAULT_TIMEOUT = 120  # seconds
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024  # 512MB
CPU_TIME_LIMIT = 60  # seconds

# Terminal protection token (set in environment)
TERMINAL_TOKEN = os.environ.get("TERMINAL_TOKEN", "").strip()

# Flask + SocketIO
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Global process registries
processes: Dict[str, Dict[str, Any]] = {}
proc_lock = threading.Lock()

# Terminal registry: term_id -> meta {pid, fd, thread, sid}
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
    """Set resource limits in the child process (UNIX)."""
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


def read_stream_and_emit(proc: subprocess.Popen, job_id: str, sid: Optional[str], stream_name: str, stream):
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
    except Exception as exc:
        socketio.emit("proc.output", {"job_id": job_id, "stream": "stderr", "text": f"[stream error] {exc}\n"}, room=sid or None)


def start_process(cmd: list, cwd: str, sid: Optional[str], timeout: Optional[int] = None) -> str:
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

    t_out = threading.Thread(target=read_stream_and_emit, args=(proc, job_id, sid, "stdout", proc.stdout), daemon=True)
    t_err = threading.Thread(target=read_stream_and_emit, args=(proc, job_id, sid, "stderr", proc.stderr), daemon=True)
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


def stop_process(job_id: str) -> bool:
    with proc_lock:
        meta = processes.get(job_id)
    if not meta:
        return False
    proc = meta["proc"]
    try:
        proc.kill()
        return True
    except Exception:
        try:
            proc.terminate()
            return True
        except Exception:
            return False


# ----------------------
# PTY-based interactive terminal (UNIX)
# ----------------------
def spawn_pty_shell(term_id: str, sid: str, cols: int = 80, rows: int = 24):
    """
    Spawn a shell in a new PTY. Runs /bin/bash -i if available, otherwise system shell.
    This function forks the process (os.forkpty) and returns metadata stored in terminals[term_id].
    Reader thread streams output from the PTY fd and emits 'term.output' events to the client's room (sid).
    """
    if pty is None:
        socketio.emit("term.error", {"msg": "PTY not available on this platform"}, room=sid)
        return False

    # Determine shell
    shell = os.environ.get("SHELL", "/bin/bash")
    pid, fd = pty.fork()
    if pid == 0:
        # Child: set resource limits and exec shell
        try:
            if resource:
                # soft limits already defined in set_limits_for_child, call here
                set_limits_for_child()
            os.execv(shell, [shell, "-i"])
        except Exception:
            # If exec fails, exit child
            os._exit(1)
    else:
        # Parent: set non-blocking
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except Exception:
            pass

        # Resize the PTY
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

        def reader():
            try:
                while True:
                    try:
                        data = os.read(fd, 4096)
                        if not data:
                            break
                        try:
                            text = data.decode(errors="replace")
                        except Exception:
                            text = str(data)
                        socketio.emit("term.output", {"term_id": term_id, "data": text}, room=sid)
                    except OSError:
                        break
                    except Exception:
                        break
            finally:
                # PTY closed
                socketio.emit("term.exit", {"term_id": term_id}, room=sid)
                with term_lock:
                    terminals.pop(term_id, None)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        with term_lock:
            terminals[term_id] = {"pid": pid, "fd": fd, "thread": t, "sid": sid}
        return True


def write_to_pty(term_id: str, data: str):
    with term_lock:
        meta = terminals.get(term_id)
    if not meta:
        return False
    fd = meta["fd"]
    try:
        os.write(fd, data.encode())
        return True
    except Exception:
        return False


def resize_pty(term_id: str, cols: int, rows: int):
    with term_lock:
        meta = terminals.get(term_id)
    if not meta:
        return False
    fd = meta["fd"]
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        return True
    except Exception:
        return False


def kill_pty(term_id: str):
    with term_lock:
        meta = terminals.get(term_id)
    if not meta:
        return False
    pid = meta["pid"]
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    return True


# ----------------------
# HTML / JS interface (inlined)
# ----------------------
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>PySandbox - Integrated Terminal</title>
  <style>
    body{font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial;margin:0;background:#071022;color:#e6eef8}
    header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:#051225}
    .brand{font-weight:700}
    .container{display:flex;gap:12px;padding:12px}
    .sidebar{width:300px;background:#061427;padding:12px;border-radius:10px}
    .main{flex:1;background:#041022;padding:12px;border-radius:10px;min-height:70vh}
    button{background:#0b1220;border:1px solid rgba(255,255,255,0.04);color:#e6eef8;padding:8px;border-radius:6px;cursor:pointer}
    .term-modal{position:fixed;left:0;top:0;width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.6)}
    .term-box{width:90%;max-width:1000px;height:80%;background:#000;border-radius:8px;display:flex;flex-direction:column;overflow:hidden}
    .term-header{display:flex;align-items:center;justify-content:space-between;padding:8px;background:#071827;color:#a8b3bd}
    .term-body{flex:1;display:flex;flex-direction:column}
    .controls{display:flex;gap:8px;margin-bottom:8px}
    .small{font-size:13px;color:#9aa6b2}
    .inline-input{padding:6px;border-radius:6px;border:1px solid rgba(255,255,255,0.04);background:#071427;color:#e6eef8}
  </style>

  <!-- xterm.js from CDN -->
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.1.0/css/xterm.css" />
  <script src="https://cdn.jsdelivr.net/npm/xterm@5.1.0/lib/xterm.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.7.0/lib/xterm-addon-fit.js"></script>
  <script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
</head>
<body>
  <header>
    <div class="brand">PySandbox</div>
    <div>
      <button id="openTerminalBtn">Open Integrated Terminal</button>
    </div>
  </header>

  <div class="container">
    <div class="sidebar">
      <div style="font-weight:700;margin-bottom:8px">Quick Actions</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        <button id="openFileMgr">File Manager</button>
        <button id="openPip">Pip Installer</button>
      </div>
      <div style="margin-top:12px;color:#9aa6b2;font-size:13px">Workspace: <span id="cwd">{{WORKDIR}}</span></div>
    </div>

    <div class="main">
      <div style="font-weight:700">Integrated Terminal Demo</div>
      <p class="small">Click "Open Integrated Terminal". You will be prompted for a token if the server requires one.
         This terminal runs a shell on the server inside a PTY (UNIX). Use responsibly.</p>

      <div style="margin-top:12px">
        <button id="openTerminalInline">Open Terminal Inline</button>
      </div>

      <div style="margin-top:18px">
        <div style="font-weight:600">Global Terminal Output (logs)</div>
        <pre id="logOut" style="background:#01060a;color:#e6eef8;padding:8px;border-radius:6px;height:220px;overflow:auto;font-family:monospace"></pre>
      </div>
    </div>
  </div>

  <!-- Terminal modal (hidden by default) -->
  <div id="termModal" class="term-modal" style="display:none">
    <div class="term-box">
      <div class="term-header">
        <div>Integrated Shell <span id="termIdLabel" style="margin-left:12px;color:#9aa6b2"></span></div>
        <div>
          <span class="small" id="termStatus">Disconnected</span>
          <button id="termClose">Close</button>
        </div>
      </div>
      <div class="term-body">
        <div style="padding:8px;display:flex;gap:8px;align-items:center;background:#071427">
          <div class="controls">
            <button id="termCtrlKill">Kill</button>
            <button id="termCtrlClear">Clear</button>
          </div>
          <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
            <input id="tokenInput" class="inline-input" placeholder="Terminal token (if required)" />
            <button id="termAuthBtn">Connect</button>
          </div>
        </div>
        <div id="xtermContainer" style="flex:1;background:#000"></div>
      </div>
    </div>
  </div>

<script>
  const socket = io({transports:['websocket'], upgrade:false});
  const logOut = document.getElementById('logOut');
  function appendLog(txt){ logOut.textContent += txt; logOut.scrollTop = logOut.scrollHeight; }

  socket.on('connect', () => {
    appendLog('[socket connected]\\n');
  });
  socket.on('proc.output', (m) => {
    appendLog(`[${m.job_id}][${m.stream}] ${m.text}`);
  });
  socket.on('proc.preview', (m) => {
    appendLog(`[preview] ${m.url}\\n`);
  });

  // Terminal UI
  const termModal = document.getElementById('termModal');
  const openTerminalBtn = document.getElementById('openTerminalBtn');
  const openTerminalInline = document.getElementById('openTerminalInline');
  const termClose = document.getElementById('termClose');
  const termAuthBtn = document.getElementById('termAuthBtn');
  const tokenInput = document.getElementById('tokenInput');
  const termStatus = document.getElementById('termStatus');
  const termCtrlKill = document.getElementById('termCtrlKill');
  const termCtrlClear = document.getElementById('termCtrlClear');
  const termIdLabel = document.getElementById('termIdLabel');

  let term; // xterm instance
  let fitAddon;
  let currentTermId = null;
  let clientSid = null;

  function createXterm(){
    term = new Terminal();
    fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(document.getElementById('xtermContainer'));
    term.focus();
    fitAddon.fit();
    window.addEventListener('resize', ()=> fitAddon.fit());
  }

  // Show modal and optionally auto-connect if no token required
  function showTerminalModal(){
    tokenInput.value = '';
    termModal.style.display = 'flex';
    if(!term) createXterm();
  }

  openTerminalBtn.addEventListener('click', showTerminalModal);
  openTerminalInline.addEventListener('click', showTerminalModal);
  termClose.addEventListener('click', ()=>{
    termModal.style.display = 'none';
    if(currentTermId){
      socket.emit('term.disconnect', {term_id: currentTermId});
      currentTermId = null;
    }
  });

  termCtrlClear.addEventListener('click', ()=>{
    if(term) term.clear();
  });
  termCtrlKill.addEventListener('click', ()=>{
    if(currentTermId) socket.emit('term.kill', {term_id: currentTermId});
  });

  termAuthBtn.addEventListener('click', ()=> {
    const token = tokenInput.value.trim();
    socket.emit('term.request', {token});
  });

  // Socket events for terminal
  socket.on('term.token.required', (m) => {
    appendLog('[server] terminal token required\\n');
    termStatus.textContent = 'Token required';
  });

  socket.on('term.ready', (m) => {
    // {term_id, cols, rows}
    appendLog('[server] terminal ready ' + m.term_id + '\\n');
    currentTermId = m.term_id;
    termIdLabel.textContent = m.term_id;
    termStatus.textContent = 'Connected';
    // bind xterm input to socket writes
    term.onData(function(data){
      socket.emit('term.input', {term_id: currentTermId, data});
    });
    // handle resize
    setTimeout(()=> {
      fitAddon.fit();
      const dims = {cols: term.cols || 80, rows: term.rows || 24};
      socket.emit('term.resize', {term_id: currentTermId, cols: dims.cols, rows: dims.rows});
    }, 200);
  });

  socket.on('term.output', (m) => {
    // {term_id, data}
    if(!term) return;
    if(m.term_id !== currentTermId) return;
    term.write(m.data);
  });

  socket.on('term.exit', (m) => {
    appendLog('[server] terminal closed ' + m.term_id + '\\n');
    if(m.term_id === currentTermId){
      termStatus.textContent = 'Exited';
      currentTermId = null;
    }
  });

  socket.on('term.error', (m) => {
    appendLog('[server error] ' + (m.msg||JSON.stringify(m)) + '\\n');
  });

  // request token automatically if server doesn't require it (click connect)
  // The server will respond with term.ready or term.token.required
</script>

</body>
</html>
"""

# ----------------------
# Flask routes: file manager APIs (minimal)
# ----------------------
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
# SocketIO: commands, pip, paste-run, and integrated terminal events
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
    job_id = start_process(parts, cwd, sid, timeout=DEFAULT_TIMEOUT)
    emit("proc.output", {"job_id": job_id, "stream": "stdout", "text": f"Started job {job_id}\n"}, room=sid)


@socketio.on("pip.install")
def on_pip_install(msg):
    sid = request.sid
    pkg = (msg or {}).get("pkg")
    if not pkg:
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": "No package specified\n"}, room=sid)
        return
    cmd = [sys.executable, "-m", "pip", "install", pkg]
    job_id = start_process(cmd, WORKDIR, sid, timeout=DEFAULT_TIMEOUT * 2)
    emit("pip.job", {"job_id": job_id, "cmd": cmd}, room=sid)


@socketio.on("pip.install.requirements")
def on_pip_install_requirements(_msg):
    sid = request.sid
    req = os.path.join(WORKDIR, "requirements.txt")
    if not os.path.exists(req):
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": "requirements.txt not found\n"}, room=sid)
        return
    cmd = [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"]
    job_id = start_process(cmd, WORKDIR, sid, timeout=DEFAULT_TIMEOUT * 5)
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
    job_id = start_process(cmd, run_dir, sid, timeout=timeout)
    emit("run.paste.job", {"job_id": job_id, "cmd": cmd}, room=sid)


# ----------------------
# Integrated terminal socket events
# ----------------------
@socketio.on("term.request")
def on_term_request(message):
    """
    Client requests a terminal. Optional token must match TERMINAL_TOKEN if configured.
    Server responds:
      - term.token.required : if token missing/invalid
      - term.ready : {term_id}
      - term.error  : {msg}
    """
    sid = request.sid
    token = (message or {}).get("token", "") or ""
    if TERMINAL_TOKEN:
        if token != TERMINAL_TOKEN:
            emit("term.token.required", {"msg": "Invalid token"}, room=sid)
            return
    # spawn PTY terminal
    term_id = str(uuid.uuid4())
    success = False
    if pty is not None and os.name != "nt":
        # spawn PTY in a separate thread to avoid blocking socket handler
        def sp():
            spawned = spawn_pty_shell(term_id, sid, cols=80, rows=24)
            if spawned:
                emit("term.ready", {"term_id": term_id}, room=sid)
            else:
                emit("term.error", {"msg": "Failed to spawn PTY"}, room=sid)
        t = threading.Thread(target=sp, daemon=True)
        t.start()
        success = True
    else:
        # fallback: spawn a subprocess and stream stdout/stderr (non-pty). Interactive behavior limited.
        try:
            # Use a pseudo terminal alternative not available -> start bash and stream
            proc = subprocess.Popen([os.environ.get("SHELL", "/bin/bash")], cwd=WORKDIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
            with term_lock:
                terminals[term_id] = {"proc": proc, "sid": sid}
            # start reader threads to emit proc output as term.output
            def reader():
                try:
                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            break
                        try:
                            text = line.decode(errors="replace")
                        except Exception:
                            text = str(line)
                        socketio.emit("term.output", {"term_id": term_id, "data": text}, room=sid)
                finally:
                    socketio.emit("term.exit", {"term_id": term_id}, room=sid)
                    with term_lock:
                        terminals.pop(term_id, None)
            threading.Thread(target=reader, daemon=True).start()
            emit("term.ready", {"term_id": term_id}, room=sid)
            success = True
        except Exception as e:
            emit("term.error", {"msg": f"Failed to spawn shell: {e}"}, room=sid)
            success = False

    if not success:
        emit("term.error", {"msg": "Unable to create terminal on server"}, room=sid)


@socketio.on("term.input")
def on_term_input(message):
    term_id = message.get("term_id")
    data = message.get("data", "")
    if not term_id:
        return
    # PTY mode
    with term_lock:
        meta = terminals.get(term_id)
    if not meta:
        return
    if "fd" in meta:
        try:
            os.write(meta["fd"], data.encode())
        except Exception:
            pass
    elif "proc" in meta:
        try:
            meta["proc"].stdin.write(data.encode())
            meta["proc"].stdin.flush()
        except Exception:
            pass


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
    killed = kill_pty(term_id)
    emit("term.killed", {"term_id": term_id, "ok": bool(killed)}, room=request.sid)


@socketio.on("term.disconnect")
def on_term_disconnect(message):
    term_id = message.get("term_id")
    if not term_id:
        return
    # kill / cleanup
    with term_lock:
        meta = terminals.get(term_id)
    if not meta:
        return
    if "fd" in meta and "pid" in meta:
        try:
            os.kill(meta["pid"], signal.SIGTERM)
        except Exception:
            pass
        with term_lock:
            terminals.pop(term_id, None)
    elif "proc" in meta:
        try:
            meta["proc"].terminate()
        except Exception:
            pass
        with term_lock:
            terminals.pop(term_id, None)
    emit("term.exit", {"term_id": term_id}, room=request.sid)


# ----------------------
# Cleanup function
# ----------------------
def cleanup():
    with proc_lock:
        keys = list(processes.keys())
    for k in keys:
        try:
            stop_process(k)
        except Exception:
            pass
    with term_lock:
        tkeys = list(terminals.keys())
    for k in tkeys:
        try:
            if "pid" in terminals[k]:
                os.kill(terminals[k]["pid"], signal.SIGKILL)
        except Exception:
            pass


# ----------------------
# Run app
# ----------------------
if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", 5000))
        host = "0.0.0.0"
        print(f"Starting PySandbox on http://{host}:{port} (TERMINAL_TOKEN set: {bool(TERMINAL_TOKEN)})")
        # allow_unsafe_werkzeug param used on platforms like Render for quick deployments; change as needed
        socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("Shutting down, cleaning up...")
        cleanup()
        sys.exit(0)
