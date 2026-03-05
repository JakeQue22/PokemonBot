"""Web UI dashboard for PokemonBot – start/stop, logs, email settings."""

from __future__ import annotations

import asyncio
import html
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
        # Suppress noisy aiohttp access / server logs from the UI log buffer
        if record.name in ("aiohttp.access", "aiohttp.server"):
            return
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

    # Proxies – guard against path being a directory (Docker volume mount edge case)
    proxy_pool: ProxyPool | None = None
    proxy_path = Path(cfg.proxies.file)
    if proxy_path.is_file():
        proxies = load_proxies(proxy_path)
        if proxies:
            proxy_pool = ProxyPool(proxies)
    elif proxy_path.exists():
        logger.warning("Proxy path %s exists but is not a file – skipping", proxy_path)

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


async def _api_clear_logs(request: web.Request) -> web.Response:
    log_buffer.records.clear()
    return web.json_response({"status": "cleared"})


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


# -- Discord settings endpoints -------------------------------------------

async def _api_discord_settings_get(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    cfg = state.config.notifier
    return web.json_response({"discord_webhook_url": cfg.discord_webhook_url})


async def _api_discord_settings_post(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if "discord_webhook_url" in body:
        state.config.notifier.discord_webhook_url = body["discord_webhook_url"]
    return web.json_response({"status": "updated"})


async def _api_test_discord(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    url = state.config.notifier.discord_webhook_url
    if not url:
        return web.json_response(
            {"status": "error", "message": "Discord webhook URL is not configured"},
            status=400,
        )

    payload = {
        "embeds": [
            {
                "title": "🎴 PokemonBot – Test Notification",
                "description": "If you see this message your webhook is configured correctly!",
                "color": 0x6C63FF,
            }
        ]
    }
    try:
        import aiohttp as _aiohttp

        async with _aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status < 400:
                    return web.json_response(
                        {"status": "ok", "message": "Test message sent to Discord"}
                    )
                text = await resp.text()
                return web.json_response(
                    {"status": "error", "message": f"Discord returned {resp.status}: {text}"},
                    status=400,
                )
    except Exception as exc:
        return web.json_response(
            {"status": "error", "message": str(exc)}, status=400
        )


# -- Config endpoint (read-only) ------------------------------------------

async def _api_config(request: web.Request) -> web.Response:
    """Return a sanitised view of the running configuration."""
    state: _AppState = request.app["state"]
    cfg = state.config
    monitors = []
    for m in cfg.monitors:
        monitors.append({
            "name": m.name,
            "url": m.url,
            "site": m.site,
            "keywords": m.keywords,
            "interval": m.interval,
        })
    data = {
        "concurrency": cfg.concurrency,
        "request_timeout": cfg.request_timeout,
        "proxy_file": cfg.proxies.file,
        "monitors": monitors,
    }
    return web.json_response(data)


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
:root{
  --bg:#0f1117;--card:#1a1d27;--border:#2a2d3a;--accent:#6c63ff;
  --accent2:#ff6b6b;--green:#2ecc71;--yellow:#f1c40f;--orange:#e67e22;
  --text:#e0e0e0;--muted:#888;--hover:#252836;--radius:10px;
}
html,body{height:100%}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);display:flex;flex-direction:column}

/* ---- Header ---- */
.header{background:var(--card);border-bottom:1px solid var(--border);padding:.8rem 1.5rem;display:flex;align-items:center;gap:.8rem;flex-shrink:0}
.header h1{font-size:1.2rem;font-weight:700;white-space:nowrap}
.header .version{color:var(--muted);font-size:.8rem}
.status-pill{margin-left:auto;padding:.25rem .75rem;border-radius:20px;font-size:.75rem;font-weight:600;display:flex;align-items:center;gap:.4rem}
.status-pill.on{background:rgba(46,204,113,.15);color:var(--green)}
.status-pill.off{background:rgba(255,107,107,.15);color:var(--accent2)}
.status-pill .dot{width:8px;height:8px;border-radius:50%;background:currentColor}

/* ---- Layout ---- */
.layout{display:flex;flex:1;overflow:hidden}
.sidebar{width:210px;background:var(--card);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;padding:.6rem 0}
.sidebar a{display:flex;align-items:center;gap:.6rem;padding:.55rem 1.2rem;color:var(--muted);text-decoration:none;font-size:.85rem;font-weight:500;transition:.15s;border-left:3px solid transparent}
.sidebar a:hover{background:var(--hover);color:var(--text)}
.sidebar a.active{color:var(--accent);border-left-color:var(--accent);background:rgba(108,99,255,.08)}
.sidebar .sep{height:1px;background:var(--border);margin:.5rem 1rem}
.main{flex:1;overflow-y:auto;padding:1.2rem 1.5rem}

/* ---- Cards ---- */
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:1.1rem;margin-bottom:1rem}
.card h2{font-size:.95rem;margin-bottom:.7rem;color:var(--accent);font-weight:600}
.card h3{font-size:.85rem;margin:1rem 0 .5rem;color:var(--muted);font-weight:600}

/* ---- Stat Cards ---- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.8rem;margin-bottom:1rem}
.stat{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:.8rem 1rem;text-align:center}
.stat .num{font-size:1.6rem;font-weight:700}
.stat .lbl{font-size:.75rem;color:var(--muted);margin-top:.15rem}
.stat.green .num{color:var(--green)}.stat.red .num{color:var(--accent2)}.stat.purple .num{color:var(--accent)}.stat.yellow .num{color:var(--yellow)}

/* ---- Buttons ---- */
.btn{padding:.45rem 1rem;border:none;border-radius:6px;cursor:pointer;font-size:.82rem;font-weight:600;transition:.15s;display:inline-flex;align-items:center;gap:.35rem}
.btn:hover{opacity:.85}.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-start{background:var(--green);color:#fff}
.btn-stop{background:var(--accent2);color:#fff}
.btn-accent{background:var(--accent);color:#fff}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text)}
.btn-outline:hover{border-color:var(--accent);color:var(--accent)}
.btn-sm{padding:.3rem .7rem;font-size:.78rem}
.controls{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}

/* ---- Tables ---- */
.tbl{width:100%;border-collapse:collapse;font-size:.82rem}
.tbl th,.tbl td{padding:.5rem .6rem;text-align:left;border-bottom:1px solid var(--border)}
.tbl th{color:var(--muted);font-weight:500;font-size:.78rem;text-transform:uppercase;letter-spacing:.03em}
.tbl tr:hover td{background:var(--hover)}

/* ---- Badge ---- */
.badge{display:inline-block;padding:.15rem .5rem;border-radius:4px;font-size:.72rem;font-weight:600}
.badge-green{background:rgba(46,204,113,.15);color:var(--green)}
.badge-red{background:rgba(255,107,107,.15);color:var(--accent2)}
.badge-yellow{background:rgba(241,196,15,.15);color:var(--yellow)}
.badge-muted{background:rgba(136,136,136,.15);color:var(--muted)}

/* ---- Log box ---- */
.log-toolbar{display:flex;gap:.5rem;align-items:center;margin-bottom:.5rem;flex-wrap:wrap}
.log-toolbar input[type=text]{flex:1;min-width:120px;background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:.35rem .6rem;color:var(--text);font-size:.82rem}
.log-toolbar select{background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:.35rem .5rem;color:var(--text);font-size:.82rem}
#log-box{background:#0a0c10;border:1px solid var(--border);border-radius:8px;padding:.7rem;
  font-family:'Cascadia Code','Fira Code',monospace;font-size:.76rem;height:420px;overflow-y:auto;white-space:pre-wrap;color:#b0b8c8;line-height:1.45}
.log-line{padding:1px 0}.log-line:hover{background:rgba(108,99,255,.06)}
.log-line .ts{color:#666}.log-line .lvl-INFO{color:#2ecc71}.log-line .lvl-WARNING{color:#f1c40f}.log-line .lvl-ERROR{color:#ff6b6b}.log-line .lvl-DEBUG{color:#888}

/* ---- Forms ---- */
.form-grid{display:grid;grid-template-columns:140px 1fr;gap:.45rem .8rem;align-items:center}
.form-grid label{font-size:.82rem;color:var(--muted)}
.form-grid input,.form-grid select,.form-grid textarea{background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:.4rem .6rem;color:var(--text);font-size:.82rem;width:100%}
.form-grid input[type=checkbox]{width:auto;justify-self:start}
.form-grid textarea{font-family:monospace;resize:vertical;min-height:60px}

/* ---- Toast ---- */
.toast{position:fixed;top:1rem;right:1rem;padding:.65rem 1.1rem;border-radius:8px;color:#fff;font-size:.82rem;opacity:0;transition:.3s;z-index:999;max-width:360px;box-shadow:0 4px 12px rgba(0,0,0,.3)}
.toast.show{opacity:1}.toast.ok{background:var(--green)}.toast.err{background:var(--accent2)}

/* ---- Tabs (within page) ---- */
.tab-bar{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:.8rem}
.tab-btn{padding:.45rem 1rem;background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);font-size:.82rem;font-weight:600;cursor:pointer;transition:.15s}
.tab-btn:hover{color:var(--text)}.tab-btn.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-panel{display:none}.tab-panel.active{display:block}

/* ---- Page sections ---- */
.page{display:none}.page.active{display:block}

/* ---- Responsive ---- */
@media(max-width:768px){
  .sidebar{width:56px;overflow:hidden}.sidebar a span{display:none}.sidebar a{justify-content:center;padding:.55rem .5rem}
  .main{padding:.8rem}
  .stats{grid-template-columns:repeat(2,1fr)}
  .form-grid{grid-template-columns:1fr}
}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <h1>🎴 PokemonBot</h1>
  <span class="version" id="version"></span>
  <div class="status-pill off" id="status-pill"><div class="dot"></div><span id="status-label">Stopped</span></div>
</div>

<!-- Layout -->
<div class="layout">
  <!-- Sidebar -->
  <nav class="sidebar">
    <a href="#" class="active" data-page="dashboard" onclick="showPage('dashboard',this)">📊 <span>Dashboard</span></a>
    <a href="#" data-page="monitors" onclick="showPage('monitors',this)">🖥️ <span>Monitors</span></a>
    <a href="#" data-page="logs" onclick="showPage('logs',this)">📋 <span>Logs</span></a>
    <div class="sep"></div>
    <a href="#" data-page="settings" onclick="showPage('settings',this)">⚙️ <span>Settings</span></a>
    <a href="#" data-page="config" onclick="showPage('config',this)">📄 <span>Config</span></a>
  </nav>

  <!-- Main content -->
  <div class="main">

    <!-- ============ DASHBOARD PAGE ============ -->
    <div class="page active" id="page-dashboard">
      <div class="stats">
        <div class="stat purple"><div class="num" id="st-monitors">0</div><div class="lbl">Monitors</div></div>
        <div class="stat green"><div class="num" id="st-checks">0</div><div class="lbl">Total Checks</div></div>
        <div class="stat yellow"><div class="num" id="st-alerts">0</div><div class="lbl">Alerts Sent</div></div>
        <div class="stat red"><div class="num" id="st-errors">0</div><div class="lbl">Errors</div></div>
      </div>

      <div class="card">
        <h2>Bot Controls</h2>
        <div class="controls">
          <button class="btn btn-start" id="btn-start" onclick="startBot()">▶ Start</button>
          <button class="btn btn-stop"  id="btn-stop"  onclick="stopBot()" disabled>■ Stop</button>
        </div>
      </div>

      <!-- Quick monitor table -->
      <div class="card" id="dash-tasks-card" style="display:none">
        <h2>Active Monitors</h2>
        <table class="tbl">
          <thead><tr><th>Name</th><th>URL</th><th>Checks</th><th>Alerts</th><th>Errors</th><th>Status</th></tr></thead>
          <tbody id="dash-tasks-body"></tbody>
        </table>
      </div>

      <!-- Recent logs -->
      <div class="card">
        <h2>Recent Logs</h2>
        <div id="dash-log-box" style="background:#0a0c10;border:1px solid var(--border);border-radius:8px;padding:.6rem;font-family:monospace;font-size:.76rem;height:180px;overflow-y:auto;white-space:pre-wrap;color:#b0b8c8"></div>
      </div>
    </div>

    <!-- ============ MONITORS PAGE ============ -->
    <div class="page" id="page-monitors">
      <div class="card">
        <h2>Configured Monitors</h2>
        <p style="font-size:.82rem;color:var(--muted);margin-bottom:.7rem">These monitors are loaded from <code>config.yaml</code>. Restart the bot after editing the config file to apply changes.</p>
        <table class="tbl" id="monitors-table">
          <thead><tr><th>Name</th><th>URL</th><th>Site</th><th>Keywords</th><th>Interval</th><th>Checks</th><th>Alerts</th><th>Errors</th><th>Status</th></tr></thead>
          <tbody id="monitors-body"></tbody>
        </table>
        <div id="monitors-empty" style="text-align:center;padding:2rem;color:var(--muted);font-size:.85rem">No monitors configured.</div>
      </div>
    </div>

    <!-- ============ LOGS PAGE ============ -->
    <div class="page" id="page-logs">
      <div class="card">
        <h2>Application Logs</h2>
        <div class="log-toolbar">
          <input type="text" id="log-search" placeholder="Search logs…" oninput="renderLogs()">
          <select id="log-level" onchange="renderLogs()">
            <option value="">All Levels</option>
            <option value="DEBUG">DEBUG</option>
            <option value="INFO">INFO</option>
            <option value="WARNING">WARNING</option>
            <option value="ERROR">ERROR</option>
          </select>
          <label style="font-size:.8rem;color:var(--muted);display:flex;align-items:center;gap:.3rem">
            <input type="checkbox" id="log-autoscroll" checked> Auto-scroll
          </label>
          <button class="btn btn-outline btn-sm" onclick="clearLogs()">🗑️ Clear</button>
        </div>
        <div id="log-box"></div>
      </div>
    </div>

    <!-- ============ SETTINGS PAGE ============ -->
    <div class="page" id="page-settings">

      <!-- Sub-tabs for Email / Discord -->
      <div class="tab-bar">
        <button class="tab-btn active" onclick="showTab('email',this)">📧 Email / SMTP</button>
        <button class="tab-btn" onclick="showTab('discord',this)">💬 Discord</button>
      </div>

      <!-- Email Tab -->
      <div class="tab-panel active" id="tab-email">
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
            <button class="btn btn-accent" onclick="saveEmail()">💾 Save</button>
            <button class="btn btn-outline" onclick="testEmail()">📧 Send Test Email</button>
          </div>
        </div>
      </div>

      <!-- Discord Tab -->
      <div class="tab-panel" id="tab-discord">
        <div class="card">
          <h2>Discord Webhook Settings</h2>
          <p style="font-size:.82rem;color:var(--muted);margin-bottom:.7rem">Paste a Discord webhook URL to receive real-time stock alerts in your channel.</p>
          <div class="form-grid">
            <label>Webhook URL</label>  <input id="dc-url" placeholder="https://discord.com/api/webhooks/...">
          </div>
          <div class="controls" style="margin-top:.8rem">
            <button class="btn btn-accent" onclick="saveDiscord()">💾 Save</button>
            <button class="btn btn-outline" onclick="testDiscord()">🔔 Send Test Message</button>
          </div>
        </div>
      </div>

    </div>

    <!-- ============ CONFIG PAGE ============ -->
    <div class="page" id="page-config">
      <div class="card">
        <h2>Running Configuration</h2>
        <p style="font-size:.82rem;color:var(--muted);margin-bottom:.7rem">Read-only view of the loaded configuration. Edit <code>config.yaml</code> and restart the bot to apply changes.</p>
        <pre id="config-json" style="background:#0a0c10;border:1px solid var(--border);border-radius:8px;padding:.8rem;font-family:monospace;font-size:.78rem;overflow:auto;max-height:500px;color:#b0b8c8;white-space:pre-wrap"></pre>
      </div>
    </div>

  </div><!-- /main -->
</div><!-- /layout -->

<div class="toast" id="toast"></div>

<script>
const API='';
let allLogs=[];

/* ---- Utility ---- */
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function toast(msg,ok){const t=document.getElementById('toast');t.textContent=msg;t.className='toast show '+(ok?'ok':'err');setTimeout(()=>t.className='toast',3500);}

/* ---- Navigation ---- */
function showPage(id,el){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.getElementById('page-'+id).classList.add('active');
  document.querySelectorAll('.sidebar a').forEach(a=>a.classList.remove('active'));
  if(el)el.classList.add('active');
  if(id==='config')loadConfig();
  if(id==='settings'){loadEmail();loadDiscord();}
  return false;
}
function showTab(id,el){
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  el.parentElement.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  el.classList.add('active');
}

/* ---- Status polling ---- */
async function fetchStatus(){
  try{
    const r=await fetch(API+'/api/status');const d=await r.json();
    document.getElementById('version').textContent='v'+d.version;
    const pill=document.getElementById('status-pill');
    const lbl=document.getElementById('status-label');
    const bs=document.getElementById('btn-start');
    const bt=document.getElementById('btn-stop');
    if(d.running){pill.className='status-pill on';lbl.textContent='Running';bs.disabled=true;bt.disabled=false;}
    else{pill.className='status-pill off';lbl.textContent='Stopped';bs.disabled=false;bt.disabled=true;}

    /* Stats */
    let checks=0,alerts=0,errors=0;
    if(d.tasks)d.tasks.forEach(t=>{checks+=t.checks;alerts+=t.alerts;errors+=t.errors;});
    document.getElementById('st-monitors').textContent=d.monitors;
    document.getElementById('st-checks').textContent=checks;
    document.getElementById('st-alerts').textContent=alerts;
    document.getElementById('st-errors').textContent=errors;

    /* Dash task table */
    const dc=document.getElementById('dash-tasks-card');const db=document.getElementById('dash-tasks-body');
    if(d.tasks&&d.tasks.length){
      dc.style.display='';
      db.innerHTML=d.tasks.map(t=>{
        let badge='badge-muted';
        if(t.last_status==='in_stock')badge='badge-green';
        else if(t.last_status==='queue_active')badge='badge-yellow';
        else if(t.errors>0)badge='badge-red';
        return `<tr><td>${esc(t.name)}</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.url)}</td>`+
        `<td>${t.checks}</td><td>${t.alerts}</td><td>${t.errors}</td><td><span class="badge ${badge}">${esc(t.last_status||'–')}</span></td></tr>`;
      }).join('');
    }else{dc.style.display='none';}

    /* Monitors page table */
    updateMonitorsPage(d);
  }catch(e){}
}

function updateMonitorsPage(d){
  const body=document.getElementById('monitors-body');
  const empty=document.getElementById('monitors-empty');
  const taskMap={};
  if(d.tasks)d.tasks.forEach(t=>taskMap[t.name]=t);
  /* We merge config monitors with runtime stats */
  /* config monitors were loaded on /api/config – use cached if available */
  const list=window._cfgMonitors||[];
  if(!list.length&&d.tasks&&d.tasks.length){
    /* Fallback: just use runtime tasks */
    empty.style.display='none';
    body.innerHTML=d.tasks.map(t=>{
      let badge='badge-muted';if(t.last_status==='in_stock')badge='badge-green';else if(t.last_status==='queue_active')badge='badge-yellow';
      return `<tr><td>${esc(t.name)}</td><td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.url)}</td>`+
      `<td>–</td><td>–</td><td>–</td><td>${t.checks}</td><td>${t.alerts}</td><td>${t.errors}</td><td><span class="badge ${badge}">${esc(t.last_status||'–')}</span></td></tr>`;
    }).join('');
    return;
  }
  if(!list.length){empty.style.display='';body.innerHTML='';return;}
  empty.style.display='none';
  body.innerHTML=list.map(m=>{
    const rt=taskMap[m.name]||{};
    let badge='badge-muted';const st=rt.last_status||'';
    if(st==='in_stock')badge='badge-green';else if(st==='queue_active')badge='badge-yellow';
    return `<tr><td>${esc(m.name)}</td><td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(m.url)}</td>`+
    `<td>${esc(m.site)}</td><td>${esc((m.keywords||[]).join(', ')||'–')}</td><td>${m.interval}s</td>`+
    `<td>${rt.checks??'–'}</td><td>${rt.alerts??'–'}</td><td>${rt.errors??'–'}</td><td><span class="badge ${badge}">${esc(st||'–')}</span></td></tr>`;
  }).join('');
}

/* ---- Logs ---- */
async function fetchLogs(){
  try{
    const r=await fetch(API+'/api/logs');const d=await r.json();
    allLogs=d.lines||[];
    renderLogs();
    /* Dash recent logs – last 20 */
    const box=document.getElementById('dash-log-box');
    box.textContent=allLogs.slice(-20).join('\n');
    box.scrollTop=box.scrollHeight;
  }catch(e){}
}
function renderLogs(){
  const search=(document.getElementById('log-search').value||'').toLowerCase();
  const level=document.getElementById('log-level').value;
  const box=document.getElementById('log-box');
  let lines=allLogs;
  if(level)lines=lines.filter(l=>l.includes('['+level+']'));
  if(search)lines=lines.filter(l=>l.toLowerCase().includes(search));
  /* colour the lines */
  box.innerHTML=lines.map(l=>{
    let cls='';
    if(l.includes('[ERROR]'))cls='lvl-ERROR';
    else if(l.includes('[WARNING]'))cls='lvl-WARNING';
    else if(l.includes('[DEBUG]'))cls='lvl-DEBUG';
    else if(l.includes('[INFO]'))cls='lvl-INFO';
    return `<div class="log-line"><span class="${cls}">${esc(l)}</span></div>`;
  }).join('');
  if(document.getElementById('log-autoscroll').checked)box.scrollTop=box.scrollHeight;
}
async function clearLogs(){
  await fetch(API+'/api/logs/clear',{method:'POST'});
  allLogs=[];renderLogs();
  document.getElementById('dash-log-box').textContent='';
  toast('Logs cleared',true);
}

/* ---- Bot controls ---- */
async function startBot(){
  const r=await fetch(API+'/api/start',{method:'POST'});const d=await r.json();
  if(r.ok){toast('Bot started!',true);loadConfig();}else toast(d.error||'Failed to start',false);
  fetchStatus();
}
async function stopBot(){
  const r=await fetch(API+'/api/stop',{method:'POST'});const d=await r.json();
  if(r.ok)toast('Bot stopped',true);else toast(d.error||'Failed to stop',false);
  fetchStatus();
}

/* ---- Email settings ---- */
async function loadEmail(){
  try{
    const r=await fetch(API+'/api/email-settings');const d=await r.json();
    document.getElementById('em-enabled').checked=d.enabled;
    document.getElementById('em-host').value=d.smtp_host||'';
    document.getElementById('em-port').value=d.smtp_port||465;
    document.getElementById('em-user').value=d.username||'';
    document.getElementById('em-pass').value=d.password||'';
    document.getElementById('em-ssl').checked=d.use_ssl;
    document.getElementById('em-from').value=d.from_address||'';
    document.getElementById('em-to').value=(d.to_addresses||[]).join(', ');
  }catch(e){}
}
async function saveEmail(){
  const body={enabled:document.getElementById('em-enabled').checked,smtp_host:document.getElementById('em-host').value,
    smtp_port:parseInt(document.getElementById('em-port').value)||465,username:document.getElementById('em-user').value,
    password:document.getElementById('em-pass').value,use_ssl:document.getElementById('em-ssl').checked,
    from_address:document.getElementById('em-from').value,to_addresses:document.getElementById('em-to').value};
  const r=await fetch(API+'/api/email-settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.ok)toast('Email settings saved',true);else toast('Failed to save',false);
}
async function testEmail(){
  toast('Sending test email…',true);
  const r=await fetch(API+'/api/test-email',{method:'POST'});const d=await r.json();
  if(r.ok)toast(d.message,true);else toast(d.message||'Failed',false);
}

/* ---- Discord settings ---- */
async function loadDiscord(){
  try{
    const r=await fetch(API+'/api/discord-settings');const d=await r.json();
    document.getElementById('dc-url').value=d.discord_webhook_url||'';
  }catch(e){}
}
async function saveDiscord(){
  const body={discord_webhook_url:document.getElementById('dc-url').value};
  const r=await fetch(API+'/api/discord-settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.ok)toast('Discord settings saved',true);else toast('Failed to save',false);
}
async function testDiscord(){
  toast('Sending test message…',true);
  const r=await fetch(API+'/api/test-discord',{method:'POST'});const d=await r.json();
  if(r.ok)toast(d.message,true);else toast(d.message||'Failed',false);
}

/* ---- Config viewer ---- */
async function loadConfig(){
  try{
    const r=await fetch(API+'/api/config');const d=await r.json();
    document.getElementById('config-json').textContent=JSON.stringify(d,null,2);
    window._cfgMonitors=d.monitors||[];
  }catch(e){}
}

/* ---- Init ---- */
fetchStatus();fetchLogs();loadEmail();loadDiscord();loadConfig();
setInterval(()=>{fetchStatus();fetchLogs();},2000);
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

    # Suppress aiohttp access logs so they don't flood the console / log buffer
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    app.router.add_get("/", _index)
    app.router.add_get("/api/status", _api_status)
    app.router.add_post("/api/start", _api_start)
    app.router.add_post("/api/stop", _api_stop)
    app.router.add_get("/api/logs", _api_logs)
    app.router.add_post("/api/logs/clear", _api_clear_logs)
    app.router.add_get("/api/email-settings", _api_email_settings_get)
    app.router.add_post("/api/email-settings", _api_email_settings_post)
    app.router.add_post("/api/test-email", _api_test_email)
    app.router.add_get("/api/discord-settings", _api_discord_settings_get)
    app.router.add_post("/api/discord-settings", _api_discord_settings_post)
    app.router.add_post("/api/test-discord", _api_test_discord)
    app.router.add_get("/api/config", _api_config)

    return app


def run_web(config_path: str = "config.yaml", host: str = "0.0.0.0", port: int = 3005) -> None:
    """Start the web dashboard (blocking)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Suppress aiohttp access logs
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    # Make sure the log buffer is attached
    root = logging.getLogger()
    if log_buffer not in root.handlers:
        root.addHandler(log_buffer)

    app = create_web_app(config_path)
    logger.info("Starting PokemonBot dashboard on http://%s:%d", host, port)
    web.run_app(app, host=host, port=port, print=lambda *a: None)

