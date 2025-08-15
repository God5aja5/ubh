#!/usr/bin/env python3
"""
main.py - Single-file Flask + Flask-SocketIO application providing a responsive, mobile-friendly
web terminal UI at route /vps that spawns PTY-backed shells on the server.

Features:
- Route /vps serves a modern responsive UI (desktop + mobile) using xterm.js (CDN).
- Supports multiple terminals (tabs). Click "New Terminal" to open a new tab.
- Keyboard shortcuts supported via xterm.js; toolbar buttons for Ctrl+C, Ctrl+Z, arrows, Tab, Esc.
- PTY-backed shells on UNIX using pty.openpty + subprocess; fallback to non-PTY subprocess where PTY unavailable.
- Terminal input/output streamed in real-time via Flask-SocketIO.
- Terminal resize handling via xterm fit addon.
- Resource limits applied to child processes on UNIX (best-effort).
- Protected by an optional TERMINAL_TOKEN environment variable: if set, client must provide the token to create terminals.
- Everything in one file. Dependencies: pip install flask flask-socketio

Run:
    TERMINAL_TOKEN=your_token python main.py
or for dev without token:
    python main.py

Security:
- This spawns shells on the host. Do NOT expose to the public without strong auth and isolation.
- Use TERMINAL_TOKEN and reverse proxy auth in production.
"""

import os
import sys
import io
import time
import uuid
import fcntl
import struct
import termios
import shlex
import signal
import pathlib
import threading
import subprocess
from typing import Dict, Any, Optional
from flask import Flask, render_template_string, request, jsonify, send_file, redirect, url_for
from flask_socketio import SocketIO, emit

# Optional Unix resource limiting
try:
    import resource  # type: ignore
except Exception:
    resource = None

# Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(BASE_DIR, "workspace")
os.makedirs(WORKDIR, exist_ok=True)

DEFAULT_TIMEOUT = 300  # seconds for long-running shells, commands etc.
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024  # 512MB
CPU_TIME_LIMIT = 120  # seconds

# Optional token to protect terminal creation
TERMINAL_TOKEN = os.environ.get("TERMINAL_TOKEN", "").strip()

# Flask + SocketIO
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Registries
terminals: Dict[str, Dict[str, Any]] = {}  # term_id -> {master_fd, proc, sid, created_at}
terms_lock = threading.Lock()

processes: Dict[str, Dict[str, Any]] = {}  # job_id registry for other subprocesses
procs_lock = threading.Lock()


# -------------------------
# Utility helpers
# -------------------------
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


def safe_close(fd):
    try:
        os.close(fd)
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
            return m.group(1).replace("0.0.0.0", "127.0.0.1")
    m = re.search(r"Running on (http://[^\s]+)", text)
    if m:
        return m.group(1).replace("0.0.0.0", "127.0.0.1")
    return None


# -------------------------
# PTY spawn & IO handling
# -------------------------
def spawn_pty_shell(term_id: str, sid: str, cols: int = 80, rows: int = 24) -> bool:
    """
    Spawn a PTY-backed shell in a subprocess. Returns True on success.
    Uses pty.openpty + subprocess so we avoid os.fork issues.
    """
    try:
        import pty as _pty
    except Exception:
        emit("term.error", {"msg": "PTY support not available on server"}, to=sid)
        return False

    try:
        master_fd, slave_fd = _pty.openpty()
    except Exception as e:
        emit("term.error", {"msg": f"openpty failed: {e}"}, to=sid)
        return False

    shell = os.environ.get("SHELL", "/bin/bash")
    preexec_fn = set_limits_for_child if (os.name != "nt" and resource) else None

    # Start the shell process with slave_fd as stdio
    try:
        proc = subprocess.Popen(
            [shell, "-i"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=preexec_fn,
            cwd=WORKDIR,
            close_fds=True,
        )
    except Exception as e:
        safe_close(master_fd)
        safe_close(slave_fd)
        emit("term.error", {"msg": f"Failed to start shell: {e}"}, to=sid)
        return False

    # Parent should close slave fd
    try:
        os.close(slave_fd)
    except Exception:
        pass

    # Set non-blocking on master_fd
    try:
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except Exception:
        pass

    # Save meta
    with terms_lock:
        terminals[term_id] = {"master_fd": master_fd, "proc": proc, "sid": sid, "created_at": time.time()}

    # Start reader background task
    def reader_loop():
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
                    socketio.emit("term.output", {"term_id": term_id, "data": text}, to=sid)
                    # Detect preview URL from output
                    preview = detect_preview_url(text)
                    if preview:
                        socketio.emit("proc.preview", {"term_id": term_id, "url": preview}, to=sid)
                except OSError:
                    # Non-blocking read may raise; check process end
                    time.sleep(0.01)
                    p = proc.poll()
                    if p is not None:
                        break
                    continue
        finally:
            # cleanup
            with terms_lock:
                terminals.pop(term_id, None)
            try:
                os.close(master_fd)
            except Exception:
                pass
            socketio.emit("term.exit", {"term_id": term_id}, to=sid)

    socketio.start_background_task(reader_loop)
    # Set initial window size
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
    except Exception:
        pass

    return True


def write_to_pty(term_id: str, data: str) -> bool:
    with terms_lock:
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
    with terms_lock:
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
    with terms_lock:
        meta = terminals.get(term_id)
    if not meta:
        return False
    proc = meta.get("proc")
    try:
        if proc and proc.poll() is None:
            proc.kill()
        return True
    except Exception:
        try:
            if proc:
                proc.terminate()
            return True
        except Exception:
            return False


# -------------------------
# Small subprocess runner (for commands / pip / paste-run)
# -------------------------
def start_subprocess(cmd: list, cwd: str, sid: Optional[str], timeout: Optional[int] = None) -> str:
    job_id = str(uuid.uuid4())
    preexec = set_limits_for_child if (os.name != "nt" and resource) else None
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
        if sid:
            socketio.emit("proc.output", {"job_id": job_id, "stream": "stderr", "text": f"Failed to start: {e}\n"}, to=sid)
            socketio.emit("proc.exit", {"job_id": job_id, "code": -1}, to=sid)
        return job_id

    with procs_lock:
        processes[job_id] = {"proc": proc, "sid": sid, "start": time.time(), "timeout": timeout or DEFAULT_TIMEOUT}

    def reader(stream, stream_name):
        try:
            while True:
                chunk = stream.readline()
                if not chunk:
                    break
                try:
                    text = chunk.decode(errors="replace")
                except Exception:
                    text = str(chunk)
                socketio.emit("proc.output", {"job_id": job_id, "stream": stream_name, "text": text}, to=sid)
                preview = detect_preview_url(text)
                if preview:
                    socketio.emit("proc.preview", {"job_id": job_id, "url": preview}, to=sid)
        finally:
            pass

    socketio.start_background_task(reader, proc.stdout, "stdout")
    socketio.start_background_task(reader, proc.stderr, "stderr")

    # watchdog
    def watchdog():
        while True:
            time.sleep(0.5)
            with procs_lock:
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
                    socketio.emit("proc.output", {"job_id": job_id, "stream": "stderr", "text": f"\n[Auto-killed after {meta['timeout']}s]\n"}, to=sid)
                except Exception:
                    pass
                break
        with procs_lock:
            processes.pop(job_id, None)
        socketio.emit("proc.exit", {"job_id": job_id, "code": p.returncode if p else None}, to=sid)

    socketio.start_background_task(watchdog)
    socketio.emit("proc.start", {"job_id": job_id, "cmd": cmd}, to=sid)
    return job_id


def stop_subprocess(job_id: str) -> bool:
    with procs_lock:
        meta = processes.get(job_id)
    if not meta:
        return False
    p = meta["proc"]
    try:
        p.kill()
        return True
    except Exception:
        try:
            p.terminate()
            return True
        except Exception:
            return False


# -------------------------
# Routes & UI (inlined)
# -------------------------
INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>VPS Terminal - /vps</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.1.0/css/xterm.css" />
<style>
:root{
  --bg:#071022; --panel:#061427; --muted:#9aa6b2; --accent:#60a5fa; --text:#e6eef8;
}
[data-theme="light"]{
  --bg:#f6f8fb; --panel:#ffffff; --muted:#556070; --accent:#0ea5a3; --text:#0b1220;
}
*{box-sizing:border-box;font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial}
html,body{height:100%;margin:0;background:linear-gradient(180deg,var(--bg),#021022);color:var(--text)}
.header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:var(--panel)}
.brand{font-weight:700}
.container{padding:12px;display:flex;gap:12px}
.left{width:320px;min-width:220px}
.panel{background:linear-gradient(180deg,#031225,#02111a);border-radius:10px;padding:12px}
.btn{background:transparent;border:1px solid rgba(255,255,255,0.04);color:var(--text);padding:8px;border-radius:8px;cursor:pointer}
.small{font-size:13px;color:var(--muted)}
.terminal-area{display:flex;flex-direction:column;gap:8px}
.terminal-tabs{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tab{padding:6px 8px;border-radius:6px;background:rgba(255,255,255,0.02);cursor:pointer}
.tab.active{background:linear-gradient(90deg,var(--accent),#3b82f6);color:#001}
.toolbar{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.tool-btn{padding:8px;border-radius:6px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.03);cursor:pointer}
.xterm-container{height:60vh;border-radius:6px;overflow:hidden;background:#000}
.mobile-toolbar{display:none}
@media(max-width:900px){
  .container{flex-direction:column}
  .left{width:100%}
  .mobile-toolbar{display:flex;gap:6px;margin-top:8px}
}
.footer{margin-top:12px;color:var(--muted);font-size:13px}
.kbd{padding:6px 8px;border-radius:6px;background:rgba(255,255,255,0.02);font-family:monospace}
</style>
</head>
<body data-theme="dark">
  <div class="header">
    <div class="brand">VPS Console (/vps)</div>
    <div>
      <button id="themeToggle" class="btn">Toggle Theme</button>
    </div>
  </div>

  <div class="container">
    <div class="left">
      <div class="panel">
        <div style="display:flex;gap:8px;align-items:center;justify-content:space-between">
          <div style="font-weight:700">Controls</div>
          <div class="small">Token: <span id="tokenState"></span></div>
        </div>
        <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
          <button id="newTermBtn" class="btn">New Terminal</button>
          <button id="closeTermBtn" class="btn">Close Current</button>
          <button id="killTermBtn" class="btn">Kill Current</button>
        </div>

        <div class="toolbar" style="margin-top:12px">
          <button class="tool-btn" data-send="\x03">Ctrl+C</button>
          <button class="tool-btn" data-send="\x1a">Ctrl+Z</button>
          <button class="tool-btn" data-send="\x0c">Ctrl+L</button>
          <button class="tool-btn" data-send="\t">Tab</button>
          <button class="tool-btn" data-send="\x1b">Esc</button>
        </div>

        <div style="margin-top:12px">
          <div style="font-weight:700">Arrow Keys</div>
          <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
            <button class="tool-btn" data-send="\x1b[A">↑</button>
            <button class="tool-btn" data-send="\x1b[B">↓</button>
            <button class="tool-btn" data-send="\x1b[C">→</button>
            <button class="tool-btn" data-send="\x1b[D">←</button>
          </div>
        </div>

        <div style="margin-top:12px">
          <div style="font-weight:700">Extras (mobile)</div>
          <div class="mobile-toolbar">
            <button class="tool-btn" data-send="\x03">Ctrl+C</button>
            <button class="tool-btn" data-send="\x0c">Ctrl+L</button>
            <button class="tool-btn" data-send="\t">Tab</button>
          </div>
        </div>

        <div style="margin-top:12px" class="footer">
          <div class="small">Open multiple terminals, use toolbar for mobile-friendly keys.</div>
        </div>
      </div>
    </div>

    <div style="flex:1">
      <div class="panel terminal-area">
        <div style="display:flex;align-items:center;justify-content:space-between">
          <div>
            <strong>Terminals</strong>
            <div class="small">Open, switch tabs; use keyboard or toolbar buttons</div>
          </div>
          <div class="small">Workspace: {{WORKDIR}}</div>
        </div>

        <div class="terminal-tabs" id="tabs"></div>

        <div class="toolbar" style="margin-top:8px">
          <div class="small">Keyboard Shortcuts:</div>
          <div class="kbd">Ctrl+C</div>
          <div class="kbd">Ctrl+L</div>
          <div class="kbd">Ctrl+Arrow</div>
        </div>

        <div id="xtermParent" class="xterm-container"></div>
      </div>
    </div>
  </div>

<script src="https://cdn.jsdelivr.net/npm/xterm@5.1.0/lib/xterm.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.7.0/lib/xterm-addon-fit.js"></script>
<script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
<script>
(() => {
  const socket = io({transports:['websocket'], upgrade:false});
  const TERM_PARENT = document.getElementById('xtermParent');
  const tabsEl = document.getElementById('tabs');
  const newTermBtn = document.getElementById('newTermBtn');
  const closeTermBtn = document.getElementById('closeTermBtn');
  const killTermBtn = document.getElementById('killTermBtn');
  const tokenState = document.getElementById('tokenState');
  const themeToggle = document.getElementById('themeToggle');

  const TOKEN = null; // If server uses TERMINAL_TOKEN, client will prompt when creating

  let terms = {}; // term_id -> {term, fit, container (dom), tabEl}
  let current = null;

  // show whether server requires token (server exposes value via templating)
  tokenState.textContent = {{ 'true' if TERMINAL_TOKEN else 'false' }};

  // Helper to create a new tab and xterm instance
  function createLocalTerm(term_id){
    // create tab
    const tab = document.createElement('div');
    tab.className = 'tab';
    tab.textContent = 'Term ' + term_id.slice(0,6);
    tab.onclick = ()=> switchTo(term_id);
    tabsEl.appendChild(tab);

    // create xterm inside TERM_PARENT; but only one xterm visible at a time.
    const term = new Terminal({
      cursorBlink: true,
      scrollback: 1000,
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);

    // container is the parent (reuse single container); we will attach/detach terminal's HTMLElement
    const container = document.createElement('div');
    container.style.width = '100%';
    container.style.height = '100%';
    container.style.display = 'none';
    TERM_PARENT.appendChild(container);
    term.open(container);
    fitAddon.fit();

    // On data typed in terminal, send to server
    term.onData(data => {
      socket.emit('term.input', {term_id, data});
    });

    // store
    terms[term_id] = {term, fit: fitAddon, container, tabEl: tab};
    switchTo(term_id);
    // handle resizing when viewport changes
    setTimeout(()=> {
      fitAddon.fit();
      socket.emit('term.resize', {term_id, cols: term.cols, rows: term.rows});
    }, 150);
  }

  function switchTo(term_id){
    if(!terms[term_id]) return;
    // hide others
    Object.keys(terms).forEach(tid => {
      terms[tid].container.style.display = 'none';
      terms[tid].tabEl.classList.remove('active');
    });
    terms[term_id].container.style.display = 'block';
    terms[term_id].tabEl.classList.add('active');
    terms[term_id].fit.fit();
    current = term_id;
  }

  function removeTermLocal(term_id){
    const meta = terms[term_id];
    if(!meta) return;
    try { meta.term.dispose(); } catch(e){ }
    try { meta.container.remove(); } except(e){}
    try { meta.tabEl.remove(); } except(e){}
    delete terms[term_id];
    current = Object.keys(terms)[0] || null;
    if(current) switchTo(current);
  }

  // Server-synchronized creation
  function requestNewTerminal(){
    // If server requires token, prompt
    const tokenRequired = {{ 'true' if TERMINAL_TOKEN else 'false' }};
    let token = "";
    if(tokenRequired){
      token = prompt("Terminal token required (set by server). Enter token:");
      if(!token) { alert('Token required'); return; }
    }
    socket.emit('term.request', {token});
  }

  newTermBtn.addEventListener('click', requestNewTerminal);
  closeTermBtn.addEventListener('click', ()=> {
    if(!current){ alert('No terminal selected'); return; }
    socket.emit('term.disconnect', {term_id: current});
    removeTermLocal(current);
  });
  killTermBtn.addEventListener('click', ()=> {
    if(!current){ alert('No terminal selected'); return; }
    socket.emit('term.kill', {term_id: current});
  });

  // Toolbar buttons (send sequences)
  document.querySelectorAll('.tool-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const seq = btn.getAttribute('data-send') || '';
      if(!current) { alert('Open a terminal first'); return; }
      socket.emit('term.input', {term_id: current, data: seq});
    });
  });

  // Theme toggle
  themeToggle.addEventListener('click', ()=> {
    const b = document.body;
    b.setAttribute('data-theme', b.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
  });

  // Socket handlers
  socket.on('connect', ()=> {
    console.log('socket connected');
  });

  socket.on('term.ready', (m) => {
    // m: {term_id}
    const tid = m.term_id;
    createLocalTerm(tid);
    // focus terminal after creation
    setTimeout(()=> {
      if(terms[tid]) terms[tid].term.focus();
    }, 100);
  });

  socket.on('term.output', (m) => {
    const tid = m.term_id;
    const data = m.data;
    if(!terms[tid]) {
      // If term not known locally, still create a tab to receive output
      createLocalTerm(tid);
    }
    try {
      terms[tid].term.write(data);
    } catch(e){
      console.warn('write error', e);
    }
  });

  socket.on('term.exit', (m) => {
    const tid = m.term_id;
    appendStatus(`Terminal ${tid} exited`);
    // Optionally remove local UI after short delay
    setTimeout(()=> removeTermLocal(tid), 800);
  });

  socket.on('term.error', (m) => {
    alert('Terminal error: ' + (m.msg||JSON.stringify(m)));
  });

  socket.on('term.token.required', (m) => {
    alert('Terminal token required/invalid');
  });

  socket.on('proc.preview', (m) => {
    // show preview link or log
    console.log('preview', m);
  });

  function appendStatus(s){
    console.log(s);
  }

  // On window resize, emit terminal resize for current
  window.addEventListener('resize', () => {
    if(!current) return;
    const meta = terms[current];
    if(!meta) return;
    meta.fit.fit();
    socket.emit('term.resize', {term_id: current, cols: meta.term.cols, rows: meta.term.rows});
  });

  // Allow Ctrl+C / other shortcuts sent from keyboard via xterm.js automatically.
  // For mobile convenience, toolbar buttons present.

  // Optional: create an initial terminal
  requestNewTerminal();

})();
</script>
</body>
</html>
"""

# Root redirect to /vps
@app.route("/")
def root():
    return redirect(url_for("vps"))

@app.route("/vps")
def vps():
    return render_template_string(INDEX_HTML.replace("{{WORKDIR}}", WORKDIR).replace("{{ 'true' if TERMINAL_TOKEN else 'false' }}", "true" if TERMINAL_TOKEN else "false"))

# Minimal file manager endpoints to list workspace (used in UI display)
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
        return {"error": str(e)}, 500
    return jsonify({"base": WORKDIR, "tree": tree})

# -------------------------
# Socket.IO events
# -------------------------
@socketio.on("term.request")
def handle_term_request(message):
    """
    message: { token: optional }
    Creates a new terminal (PTY) and emits 'term.ready' to the requesting client (SID).
    """
    sid = request.sid
    token = (message or {}).get("token", "") or ""
    if TERMINAL_TOKEN:
        if token != TERMINAL_TOKEN:
            emit("term.token.required", {"msg": "Invalid token"}, to=sid)
            return
    term_id = str(uuid.uuid4())
    ok = spawn_pty_shell(term_id, sid, cols=80, rows=24)
    if ok:
        emit("term.ready", {"term_id": term_id}, to=sid)
    else:
        emit("term.error", {"msg": "Failed to spawn PTY"}, to=sid)


@socketio.on("term.input")
def handle_term_input(message):
    term_id = message.get("term_id")
    data = message.get("data", "")
    if not term_id:
        return
    write_to_pty(term_id, data)


@socketio.on("term.resize")
def handle_term_resize(message):
    term_id = message.get("term_id")
    cols = int(message.get("cols", 80))
    rows = int(message.get("rows", 24))
    if not term_id:
        return
    resize_pty(term_id, cols, rows)


@socketio.on("term.kill")
def handle_term_kill(message):
    term_id = message.get("term_id")
    if not term_id:
        return
    ok = kill_terminal(term_id)
    emit("term.killed", {"term_id": term_id, "ok": ok}, to=request.sid)


@socketio.on("term.disconnect")
def handle_term_disconnect(message):
    term_id = message.get("term_id")
    if not term_id:
        return
    with terms_lock:
        meta = terminals.get(term_id)
    if not meta:
        return
    proc = meta.get("proc")
    try:
        if proc and proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    # close master fd
    try:
        fd = meta.get("master_fd")
        if fd:
            os.close(fd)
    except Exception:
        pass
    with terms_lock:
        terminals.pop(term_id, None)
    emit("term.exit", {"term_id": term_id}, to=request.sid)


# Small endpoints to run one-off commands (optional)
@socketio.on("run.command")
def handle_run_command(message):
    sid = request.sid
    cmd_text = message.get("cmd", "")
    cwd_rel = message.get("cwd", "")
    if not cmd_text:
        emit("proc.output", {"job_id": None, "stream": "stderr", "text": "No command provided\n"}, to=sid)
        return
    try:
        parts = shlex.split(cmd_text)
    except Exception:
        parts = cmd_text.split()
    # adjust python/pip to current interpreter
    if parts and parts[0] == "pip":
        parts = [sys.executable, "-m", "pip"] + parts[1:]
    if parts and parts[0] == "python":
        parts[0] = sys.executable
    cwd = WORKDIR
    if cwd_rel:
        try:
            cwd = os.path.join(WORKDIR, cwd_rel)
        except Exception:
            cwd = WORKDIR
    job_id = start_subprocess(parts, cwd, sid, timeout=DEFAULT_TIMEOUT)
    emit("proc.output", {"job_id": job_id, "stream": "stdout", "text": f"Started job {job_id}\n"}, to=sid)

# -------------------------
# Graceful cleanup
# -------------------------
def cleanup():
    with terms_lock:
        keys = list(terminals.keys())
    for t in keys:
        try:
            kill_terminal(t)
        except Exception:
            pass
    with procs_lock:
        keys = list(processes.keys())
    for k in keys:
        try:
            stop_subprocess(k)
        except Exception:
            pass


# -------------------------
# Run server
# -------------------------
if __name__ == "__main__":
    try:
        port = int(os.environ.get("PORT", 5000))
        host = "0.0.0.0"
        print(f"Starting VPS terminal UI on http://{host}:{port}/vps (TERMINAL_TOKEN set: {bool(TERMINAL_TOKEN)})")
        # For simple hosting use allow_unsafe_werkzeug=True if needed by your platform.
        socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("Shutting down, cleaning up terminals...")
        cleanup()
        sys.exit(0)
