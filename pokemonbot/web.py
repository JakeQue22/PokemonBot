"""Web UI dashboard for PokemonBot – start/stop, logs, email settings."""

from __future__ import annotations

import asyncio
import html
import json
import logging
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

from pokemonbot import __version__
from pokemonbot.config import AppConfig, EmailConfig, load_config
from pokemonbot.notifier import (
    ConsoleNotifier,
    DiscordWebhookNotifier,
    EmailNotifier,
    NotifierPipeline,
)
from pokemonbot.proxy import ProxyPool, load_proxies
from pokemonbot.tasks import TaskManager

logger = logging.getLogger(__name__)

MAX_LOG_LINES = 500


# ---------------------------------------------------------------------------
# In-memory log handler – captures log records for the web UI
# ---------------------------------------------------------------------------

class _LogBuffer(logging.Handler):
    """Ring-buffer logging handler that stores the last *maxlen* formatted records."""

    def __init__(self, maxlen: int = MAX_LOG_LINES) -> None:
        super().__init__()
        self.records: deque[str] = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:
            pass


# Singleton shared across the app
log_buffer = _LogBuffer()


# ---------------------------------------------------------------------------
# Application state shared via ``app["state"]``
# ---------------------------------------------------------------------------

@dataclass
class _AppState:
    config_path: str = "config.yaml"
    config: AppConfig = field(default_factory=AppConfig)
    manager: TaskManager | None = None
    manager_task: asyncio.Task[None] | None = None
    running: bool = False


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------

async def _api_status(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    task_states = []
    if state.manager:
        for ts in state.manager.task_states:
            task_states.append({
                "name": ts.config.name,
                "url": ts.config.url,
                "checks": ts.checks,
                "alerts": ts.alerts,
                "errors": ts.errors,
                "last_status": ts.last_status,
            })
    payload = {
        "version": __version__,
        "running": state.running,
        "monitors": len(state.config.monitors),
        "tasks": task_states,
    }
    return web.json_response(payload)


async def _api_start(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    if state.running:
        return web.json_response({"error": "Bot is already running"}, status=409)

    # (Re)load config
    path = Path(state.config_path)
    if not path.exists():
        return web.json_response({"error": f"Config not found: {state.config_path}"}, status=400)

    cfg = load_config(path)
    state.config = cfg

    if not cfg.monitors:
        return web.json_response({"error": "No monitors configured"}, status=400)

    # Proxies
    proxy_pool: ProxyPool | None = None
    proxy_path = Path(cfg.proxies.file)
    if proxy_path.exists():
        proxies = load_proxies(proxy_path)
        if proxies:
            proxy_pool = ProxyPool(proxies)

    # Notifiers
    notifier = NotifierPipeline()
    if cfg.notifier.console:
        notifier.add(ConsoleNotifier())
    if cfg.notifier.discord_webhook_url:
        notifier.add(DiscordWebhookNotifier(cfg.notifier.discord_webhook_url))
    if cfg.email.enabled:
        notifier.add(EmailNotifier(cfg.email))

    manager = TaskManager(app_config=cfg, proxy_pool=proxy_pool, notifier=notifier)
    state.manager = manager
    state.running = True
    state.manager_task = asyncio.create_task(_run_manager(state, manager))

    logger.info("Bot started with %d monitor(s)", len(cfg.monitors))
    return web.json_response({"status": "started", "monitors": len(cfg.monitors)})


async def _run_manager(state: _AppState, manager: TaskManager) -> None:
    try:
        await manager.run()
    except Exception:
        logger.exception("TaskManager crashed")
    finally:
        state.running = False


async def _api_stop(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    if not state.running or state.manager is None:
        return web.json_response({"error": "Bot is not running"}, status=409)

    state.manager.stop()
    if state.manager_task:
        try:
            await asyncio.wait_for(state.manager_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    state.running = False
    logger.info("Bot stopped")
    return web.json_response({"status": "stopped"})


async def _api_logs(request: web.Request) -> web.Response:
    lines = list(log_buffer.records)
    return web.json_response({"lines": lines})


async def _api_email_settings_get(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    cfg = state.config.email
    # Never expose password in GET – return masked version
    data = asdict(cfg)
    data["password"] = "••••••••" if cfg.password else ""
    return web.json_response(data)


async def _api_email_settings_post(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    cfg = state.config.email
    for key in ("enabled", "smtp_host", "smtp_port", "username", "use_ssl", "from_address"):
        if key in body:
            setattr(cfg, key, body[key])
    # Only update password if a real value was sent (not the masked placeholder)
    if "password" in body and body["password"] not in ("", "••••••••"):
        cfg.password = body["password"]
    if "to_addresses" in body:
        val = body["to_addresses"]
        if isinstance(val, str):
            cfg.to_addresses = [a.strip() for a in val.split(",") if a.strip()]
        else:
            cfg.to_addresses = list(val)

    return web.json_response({"status": "updated"})


async def _api_test_email(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    cfg = state.config.email
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, EmailNotifier.send_test_email, cfg)
    if result == "ok":
        return web.json_response({"status": "ok", "message": "Test email sent successfully"})
    return web.json_response({"status": "error", "message": result}, status=400)


# ---------------------------------------------------------------------------
# Embedded HTML dashboard
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PokemonBot Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--accent:#6c63ff;
      --accent2:#ff6b6b;--green:#2ecc71;--text:#e0e0e0;--muted:#888}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.header{background:var(--card);border-bottom:1px solid var(--border);padding:1rem 2rem;display:flex;align-items:center;gap:1rem}
.header h1{font-size:1.3rem;font-weight:600}
.header .version{color:var(--muted);font-size:.85rem}
.header .status-dot{width:10px;height:10px;border-radius:50%;margin-left:auto}
.header .status-dot.on{background:var(--green);box-shadow:0 0 8px var(--green)}
.header .status-dot.off{background:var(--accent2)}
.container{max-width:1100px;margin:1.5rem auto;padding:0 1rem;display:grid;gap:1.2rem}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:1.2rem}
.card h2{font-size:1rem;margin-bottom:.8rem;color:var(--accent)}
.btn{padding:.5rem 1.2rem;border:none;border-radius:6px;cursor:pointer;font-size:.85rem;font-weight:600;transition:.15s}
.btn-start{background:var(--green);color:#fff}
.btn-stop{background:var(--accent2);color:#fff}
.btn-test{background:var(--accent);color:#fff}
.btn:hover{opacity:.85}
.btn:disabled{opacity:.4;cursor:not-allowed}
.controls{display:flex;gap:.8rem;flex-wrap:wrap;align-items:center}
#log-box{background:#0a0c10;border:1px solid var(--border);border-radius:8px;padding:.8rem;
  font-family:'Cascadia Code',monospace;font-size:.78rem;height:340px;overflow-y:auto;white-space:pre-wrap;color:#b0b8c8}
.form-grid{display:grid;grid-template-columns:140px 1fr;gap:.5rem .8rem;align-items:center}
.form-grid label{font-size:.85rem;color:var(--muted)}
.form-grid input,.form-grid select{background:#0f1117;border:1px solid var(--border);border-radius:5px;
  padding:.4rem .6rem;color:var(--text);font-size:.85rem}
.form-grid input[type=checkbox]{width:auto}
.task-table{width:100%;border-collapse:collapse;font-size:.82rem}
.task-table th,.task-table td{padding:.4rem .6rem;text-align:left;border-bottom:1px solid var(--border)}
.task-table th{color:var(--muted);font-weight:500}
.toast{position:fixed;top:1rem;right:1rem;padding:.7rem 1.2rem;border-radius:8px;color:#fff;
  font-size:.85rem;opacity:0;transition:.3s;z-index:999}
.toast.show{opacity:1}
.toast.ok{background:var(--green)}.toast.err{background:var(--accent2)}
</style>
</head>
<body>
<div class="header">
  <h1>🎴 PokemonBot</h1>
  <span class="version" id="version"></span>
  <div class="status-dot off" id="status-dot"></div>
</div>

<div class="container">
  <!-- Controls -->
  <div class="card">
    <h2>Bot Controls</h2>
    <div class="controls">
      <button class="btn btn-start" id="btn-start" onclick="startBot()">▶ Start</button>
      <button class="btn btn-stop"  id="btn-stop"  onclick="stopBot()" disabled>■ Stop</button>
      <span id="status-text" style="margin-left:.5rem;color:var(--muted)">Stopped</span>
    </div>
  </div>

  <!-- Monitor tasks -->
  <div class="card" id="tasks-card" style="display:none">
    <h2>Active Monitors</h2>
    <table class="task-table">
      <thead><tr><th>Name</th><th>URL</th><th>Checks</th><th>Alerts</th><th>Errors</th><th>Status</th></tr></thead>
      <tbody id="tasks-body"></tbody>
    </table>
  </div>

  <!-- Logs -->
  <div class="card">
    <h2>Logs</h2>
    <div id="log-box"></div>
  </div>

  <!-- Email / SMTP Settings -->
  <div class="card">
    <h2>Email / SMTP Settings</h2>
    <div class="form-grid">
      <label>Enabled</label>       <input type="checkbox" id="em-enabled">
      <label>SMTP Host</label>     <input id="em-host" placeholder="smtp.gmail.com">
      <label>SMTP Port</label>     <input id="em-port" type="number" value="465">
      <label>Username</label>      <input id="em-user" placeholder="you@gmail.com">
      <label>Password</label>      <input id="em-pass" type="password" placeholder="app password">
      <label>Use SSL</label>       <input type="checkbox" id="em-ssl" checked>
      <label>From Address</label>  <input id="em-from" placeholder="you@gmail.com">
      <label>To Addresses</label>  <input id="em-to" placeholder="a@b.com, c@d.com">
    </div>
    <div class="controls" style="margin-top:.8rem">
      <button class="btn btn-test" onclick="saveEmail()">💾 Save</button>
      <button class="btn btn-test" onclick="testEmail()">📧 Send Test Email</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const API = '';
let polling = null;

function toast(msg, ok) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (ok ? 'ok' : 'err');
  setTimeout(() => t.className = 'toast', 3000);
}

async function fetchStatus() {
  try {
    const r = await fetch(API + '/api/status');
    const d = await r.json();
    document.getElementById('version').textContent = 'v' + d.version;
    const dot = document.getElementById('status-dot');
    const txt = document.getElementById('status-text');
    const bstart = document.getElementById('btn-start');
    const bstop  = document.getElementById('btn-stop');
    if (d.running) {
      dot.className = 'status-dot on';
      txt.textContent = 'Running – ' + d.monitors + ' monitor(s)';
      bstart.disabled = true; bstop.disabled = false;
    } else {
      dot.className = 'status-dot off';
      txt.textContent = 'Stopped';
      bstart.disabled = false; bstop.disabled = true;
    }
    const tc = document.getElementById('tasks-card');
    const tb = document.getElementById('tasks-body');
    if (d.tasks && d.tasks.length) {
      tc.style.display = '';
      tb.innerHTML = d.tasks.map(t =>
        `<tr><td>${esc(t.name)}</td><td style="max-width:250px;overflow:hidden;text-overflow:ellipsis">${esc(t.url)}</td>` +
        `<td>${t.checks}</td><td>${t.alerts}</td><td>${t.errors}</td><td>${esc(t.last_status||'–')}</td></tr>`
      ).join('');
    } else {
      tc.style.display = 'none';
    }
  } catch(e) {}
}

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}

async function fetchLogs() {
  try {
    const r = await fetch(API + '/api/logs');
    const d = await r.json();
    const box = document.getElementById('log-box');
    box.textContent = d.lines.join('\n');
    box.scrollTop = box.scrollHeight;
  } catch(e) {}
}

async function startBot() {
  const r = await fetch(API + '/api/start', {method:'POST'});
  const d = await r.json();
  if (r.ok) { toast('Bot started!', true); }
  else { toast(d.error || 'Failed to start', false); }
  fetchStatus();
}

async function stopBot() {
  const r = await fetch(API + '/api/stop', {method:'POST'});
  const d = await r.json();
  if (r.ok) { toast('Bot stopped', true); }
  else { toast(d.error || 'Failed to stop', false); }
  fetchStatus();
}

async function loadEmail() {
  try {
    const r = await fetch(API + '/api/email-settings');
    const d = await r.json();
    document.getElementById('em-enabled').checked = d.enabled;
    document.getElementById('em-host').value = d.smtp_host || '';
    document.getElementById('em-port').value = d.smtp_port || 465;
    document.getElementById('em-user').value = d.username || '';
    document.getElementById('em-pass').value = d.password || '';
    document.getElementById('em-ssl').checked = d.use_ssl;
    document.getElementById('em-from').value = d.from_address || '';
    document.getElementById('em-to').value = (d.to_addresses||[]).join(', ');
  } catch(e) {}
}

async function saveEmail() {
  const body = {
    enabled: document.getElementById('em-enabled').checked,
    smtp_host: document.getElementById('em-host').value,
    smtp_port: parseInt(document.getElementById('em-port').value) || 465,
    username: document.getElementById('em-user').value,
    password: document.getElementById('em-pass').value,
    use_ssl: document.getElementById('em-ssl').checked,
    from_address: document.getElementById('em-from').value,
    to_addresses: document.getElementById('em-to').value,
  };
  const r = await fetch(API + '/api/email-settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  if (r.ok) toast('Settings saved', true);
  else toast('Failed to save', false);
}

async function testEmail() {
  toast('Sending test email…', true);
  const r = await fetch(API + '/api/test-email', {method:'POST'});
  const d = await r.json();
  if (r.ok) toast(d.message, true);
  else toast(d.message || 'Failed', false);
}

// Poll every 2s
fetchStatus(); fetchLogs(); loadEmail();
polling = setInterval(() => { fetchStatus(); fetchLogs(); }, 2000);
</script>
</body>
</html>"""


async def _index(request: web.Request) -> web.Response:
    return web.Response(text=_DASHBOARD_HTML, content_type="text/html")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_web_app(config_path: str = "config.yaml") -> web.Application:
    """Create and return the aiohttp ``Application`` with all routes registered."""
    app = web.Application()

    state = _AppState(config_path=config_path)
    # Pre-load config if it exists so email settings are available immediately
    path = Path(config_path)
    if path.exists():
        state.config = load_config(path)
    app["state"] = state

    # Install the log buffer handler on the root logger
    root = logging.getLogger()
    if log_buffer not in root.handlers:
        root.addHandler(log_buffer)

    app.router.add_get("/", _index)
    app.router.add_get("/api/status", _api_status)
    app.router.add_post("/api/start", _api_start)
    app.router.add_post("/api/stop", _api_stop)
    app.router.add_get("/api/logs", _api_logs)
    app.router.add_get("/api/email-settings", _api_email_settings_get)
    app.router.add_post("/api/email-settings", _api_email_settings_post)
    app.router.add_post("/api/test-email", _api_test_email)

    return app


def run_web(config_path: str = "config.yaml", host: str = "0.0.0.0", port: int = 3005) -> None:
    """Start the web dashboard (blocking)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Make sure the log buffer is attached
    root = logging.getLogger()
    if log_buffer not in root.handlers:
        root.addHandler(log_buffer)

    app = create_web_app(config_path)
    logger.info("Starting PokemonBot dashboard on http://%s:%d", host, port)
    web.run_app(app, host=host, port=port, print=lambda *a: None)
