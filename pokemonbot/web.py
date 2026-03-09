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
from pokemonbot.config import AppConfig, EmailConfig, MonitorConfig, load_config, save_config
from pokemonbot.notifier import (
    ConsoleNotifier,
    DiscordWebhookNotifier,
    EmailNotifier,
    NotifierPipeline,
)
from pokemonbot.proxy import (
    ProxyPool,
    ensure_proxy_file,
    fetch_public_proxies,
    load_proxies,
    parse_proxy,
    save_proxies,
)
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


def _save_state(state: _AppState) -> None:
    """Persist the current in-memory config to the YAML file on disk."""
    try:
        save_config(state.config, state.config_path)
    except OSError:
        logger.exception("Failed to persist configuration to %s", state.config_path)


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
                "successes": ts.successes,
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

    # Proxies – ensure file exists and load
    proxy_pool: ProxyPool | None = None
    proxy_path = ensure_proxy_file(cfg.proxies.file)
    if proxy_path.is_file():
        proxies = load_proxies(proxy_path)
        if proxies:
            proxy_pool = ProxyPool(proxies)
        else:
            logger.warning("Proxy file %s contains no valid proxies – running without proxies", proxy_path)

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

    _save_state(state)
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
    _save_state(state)
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
        "max_retries": cfg.max_retries,
        "proxy_file": cfg.proxies.file,
        "monitors": monitors,
    }
    return web.json_response(data)


# -- Monitor CRUD endpoints -----------------------------------------------

def _monitor_to_dict(m: MonitorConfig) -> dict[str, Any]:
    return {
        "name": m.name,
        "url": m.url,
        "site": m.site,
        "keywords": m.keywords,
        "interval": m.interval,
        "enabled": m.enabled,
    }


async def _api_monitors_list(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    monitors = [_monitor_to_dict(m) for m in state.config.monitors]
    return web.json_response({"monitors": monitors})


async def _api_monitors_add(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    url = (body.get("url") or "").strip()
    if not url:
        return web.json_response({"error": "URL is required"}, status=400)

    name = (body.get("name") or "").strip() or url
    site = (body.get("site") or "pokemoncenter").strip()
    keywords_raw = body.get("keywords", [])
    if isinstance(keywords_raw, str):
        keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    else:
        keywords = list(keywords_raw)
    interval = float(body.get("interval", 10.0))
    enabled = body.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.lower() not in ("false", "0", "no")

    monitor = MonitorConfig(
        name=name, url=url, site=site, keywords=keywords, interval=interval,
        enabled=bool(enabled),
    )
    state.config.monitors.append(monitor)
    _save_state(state)
    logger.info("Monitor added: %s → %s", name, url)
    return web.json_response({
        "status": "added",
        "monitor": _monitor_to_dict(monitor),
        "index": len(state.config.monitors) - 1,
    })


async def _api_monitors_delete(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    try:
        idx = int(request.match_info["index"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid index"}, status=400)

    if idx < 0 or idx >= len(state.config.monitors):
        return web.json_response({"error": "Index out of range"}, status=404)

    removed = state.config.monitors.pop(idx)
    _save_state(state)
    logger.info("Monitor removed: %s → %s", removed.name, removed.url)
    return web.json_response({"status": "removed", "monitor": _monitor_to_dict(removed)})


async def _api_monitors_update(request: web.Request) -> web.Response:
    """Update an existing monitor by index."""
    state: _AppState = request.app["state"]
    try:
        idx = int(request.match_info["index"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid index"}, status=400)

    if idx < 0 or idx >= len(state.config.monitors):
        return web.json_response({"error": "Index out of range"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    m = state.config.monitors[idx]
    if "name" in body:
        m.name = str(body["name"]).strip() or m.name
    if "url" in body:
        url = str(body["url"]).strip()
        if url:
            m.url = url
    if "site" in body:
        m.site = str(body["site"]).strip() or m.site
    if "keywords" in body:
        kw = body["keywords"]
        if isinstance(kw, str):
            m.keywords = [k.strip() for k in kw.split(",") if k.strip()]
        else:
            m.keywords = list(kw)
    if "interval" in body:
        m.interval = max(1.0, float(body["interval"]))
    if "enabled" in body:
        val = body["enabled"]
        if isinstance(val, str):
            m.enabled = val.lower() not in ("false", "0", "no")
        else:
            m.enabled = bool(val)

    _save_state(state)
    logger.info("Monitor updated [%d]: %s → %s", idx, m.name, m.url)
    return web.json_response({"status": "updated", "monitor": _monitor_to_dict(m)})


# -- Proxy CRUD endpoints ------------------------------------------------

async def _api_proxies_list(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    # Return per-proxy stats if a pool is active, otherwise list from file
    if state.manager and state.manager.proxy_pool:
        data = state.manager.proxy_pool.stats()
    else:
        proxy_path = ensure_proxy_file(state.config.proxies.file)
        loaded = load_proxies(proxy_path) if proxy_path.is_file() else []
        data = [
            {"url": p.url, "protocol": p.protocol, "host": p.host, "port": p.port,
             "requests": 0, "failures": 0, "successes": 0}
            for p in loaded
        ]
    return web.json_response({"proxies": data})


async def _api_proxies_add(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    raw = (body.get("proxy") or "").strip()
    if not raw:
        return web.json_response({"error": "proxy is required"}, status=400)

    try:
        proxy = parse_proxy(raw)
    except (ValueError, IndexError) as exc:
        return web.json_response({"error": f"Invalid proxy: {exc}"}, status=400)

    # Persist to file
    proxy_path = ensure_proxy_file(state.config.proxies.file)
    existing = load_proxies(proxy_path) if proxy_path.is_file() else []
    existing.append(proxy)
    save_proxies(existing, proxy_path)
    logger.info("Proxy added: %s", proxy.url)
    return web.json_response({"status": "added", "proxy": proxy.url})


async def _api_proxies_delete(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    try:
        idx = int(request.match_info["index"])
    except (KeyError, ValueError):
        return web.json_response({"error": "Invalid index"}, status=400)

    proxy_path = ensure_proxy_file(state.config.proxies.file)
    proxies = load_proxies(proxy_path) if proxy_path.is_file() else []

    if idx < 0 or idx >= len(proxies):
        return web.json_response({"error": "Index out of range"}, status=404)

    removed = proxies.pop(idx)
    save_proxies(proxies, proxy_path)
    logger.info("Proxy removed: %s", removed.url)
    return web.json_response({"status": "removed", "proxy": removed.url})


async def _api_proxies_bulk(request: web.Request) -> web.Response:
    """Replace the entire proxy list at once (bulk edit)."""
    state: _AppState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    raw_lines: list[str] = body.get("proxies", [])
    if isinstance(raw_lines, str):
        raw_lines = [l.strip() for l in raw_lines.splitlines() if l.strip()]

    proxies = []
    errors = []
    for i, line in enumerate(raw_lines):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            proxies.append(parse_proxy(line))
        except (ValueError, IndexError) as exc:
            errors.append(f"Line {i+1}: {exc}")

    proxy_path = ensure_proxy_file(state.config.proxies.file)
    save_proxies(proxies, proxy_path)
    logger.info("Proxy list updated: %d proxies saved", len(proxies))
    return web.json_response({"status": "updated", "count": len(proxies), "errors": errors})


# -- General settings endpoints -------------------------------------------

async def _api_general_settings_get(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    return web.json_response({
        "portal_name": state.config.portal_name,
        "concurrency": state.config.concurrency,
        "request_timeout": state.config.request_timeout,
        "max_retries": state.config.max_retries,
        "base_url": state.config.base_url,
    })


async def _api_general_settings_post(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    if "portal_name" in body:
        state.config.portal_name = str(body["portal_name"]).strip() or "PokemonBot"
    if "concurrency" in body:
        state.config.concurrency = max(1, int(body["concurrency"]))
    if "request_timeout" in body:
        state.config.request_timeout = max(1.0, float(body["request_timeout"]))
    if "max_retries" in body:
        state.config.max_retries = max(1, int(body["max_retries"]))
    if "base_url" in body:
        val = str(body["base_url"]).strip().rstrip("/")
        state.config.base_url = val
    _save_state(state)
    return web.json_response({"status": "updated"})


# -- Fetch public proxies endpoint ----------------------------------------

async def _api_proxies_fetch_public(request: web.Request) -> web.Response:
    """Fetch free proxies from public sources and save them to the proxy file."""
    state: _AppState = request.app["state"]
    try:
        proxies = await fetch_public_proxies()
    except Exception as exc:
        logger.exception("Failed to fetch public proxies")
        return web.json_response({"error": str(exc)}, status=500)

    if not proxies:
        return web.json_response(
            {"error": "No proxies could be fetched from public sources"}, status=400
        )

    try:
        proxy_path = ensure_proxy_file(state.config.proxies.file)
        # Merge with any existing proxies, de-duplicate by URL
        existing = load_proxies(proxy_path) if proxy_path.is_file() else []
        existing_urls = {p.url for p in existing}
        new_proxies = [p for p in proxies if p.url not in existing_urls]
        merged = existing + new_proxies
        save_proxies(merged, proxy_path)
    except OSError as exc:
        logger.exception("Failed to save fetched proxies")
        return web.json_response(
            {"error": "Proxies fetched but could not be saved to disk"}, status=500
        )

    logger.info("Public proxies fetched: %d new, %d total", len(new_proxies), len(merged))
    return web.json_response({
        "status": "ok",
        "fetched": len(proxies),
        "new": len(new_proxies),
        "total": len(merged),
    })


# ---------------------------------------------------------------------------
# Embedded HTML dashboard
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{PORTAL_NAME}} Dashboard</title>
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
.log-line .log-success{color:#2ecc71}.log-line .log-error{color:#ff6b6b}

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
  <h1>🎴 {{PORTAL_NAME}}</h1>
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
        <div class="stat purple"><div class="num" id="st-checks">0</div><div class="lbl">Total Checks</div></div>
        <div class="stat green"><div class="num" id="st-successes">0</div><div class="lbl">Successes</div></div>
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
          <thead><tr><th>Name</th><th>URL</th><th>Checks</th><th>Successes</th><th>Alerts</th><th>Errors</th><th>Status</th></tr></thead>
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
      <!-- Add Monitor form -->
      <div class="card">
        <h2>Add Monitor</h2>
        <p style="font-size:.82rem;color:var(--muted);margin-bottom:.7rem">Add a new URL to monitor. If the bot is already running, stop and start it again to pick up the new monitor.</p>
        <div class="form-grid">
          <label>Name</label>        <input id="mon-name" placeholder="e.g. Elite Trainer Box">
          <label>URL</label>         <input id="mon-url" placeholder="https://www.pokemoncenter.com/en-gb/category/elite-trainer-box">
          <label>Site</label>
          <select id="mon-site">
            <option value="pokemoncenter">pokemoncenter</option>
            <option value="smythstoys">smythstoys</option>
            <option value="generic">generic</option>
          </select>
          <label>Keywords</label>    <input id="mon-keywords" placeholder="comma-separated (optional)">
          <label>Interval (s)</label><input id="mon-interval" type="number" value="10" min="1" step="1">
        </div>
        <div class="controls" style="margin-top:.8rem">
          <button class="btn btn-accent" onclick="addMonitor()">➕ Add Monitor</button>
        </div>
      </div>

      <!-- Monitor table -->
      <div class="card">
        <h2>Configured Monitors</h2>
        <table class="tbl" id="monitors-table">
          <thead><tr><th>Name</th><th>URL</th><th>Site</th><th>Keywords</th><th>Interval</th><th>Enabled</th><th>Checks</th><th>Successes</th><th>Alerts</th><th>Errors</th><th>Status</th><th style="width:120px"></th></tr></thead>
          <tbody id="monitors-body"></tbody>
        </table>
        <div id="monitors-empty" style="text-align:center;padding:2rem;color:var(--muted);font-size:.85rem">No monitors configured.</div>
      </div>

      <!-- Edit modal (hidden by default) -->
      <div id="edit-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:900;align-items:center;justify-content:center">
        <div class="card" style="width:480px;max-width:95vw">
          <h2>Edit Monitor</h2>
          <input type="hidden" id="edit-idx">
          <div class="form-grid">
            <label>Name</label>        <input id="edit-name">
            <label>URL</label>         <input id="edit-url">
            <label>Site</label>
            <select id="edit-site">
              <option value="pokemoncenter">pokemoncenter</option>
              <option value="smythstoys">smythstoys</option>
              <option value="generic">generic</option>
            </select>
            <label>Keywords</label>    <input id="edit-keywords">
            <label>Interval (s)</label><input id="edit-interval" type="number" min="1" step="1">
          </div>
          <div class="controls" style="margin-top:.8rem">
            <button class="btn btn-accent" onclick="saveEditMonitor()">💾 Save</button>
            <button class="btn btn-outline" onclick="closeEditModal()">Cancel</button>
          </div>
        </div>
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
          <button class="btn btn-outline btn-sm" onclick="copyLast20Logs()">📋 Copy Last 20</button>
        </div>
        <div id="log-box"></div>
      </div>
    </div>

    <!-- ============ SETTINGS PAGE ============ -->
    <div class="page" id="page-settings">

      <!-- Sub-tabs -->
      <div class="tab-bar">
        <button class="tab-btn active" onclick="showTab('general',this)">🏷️ General</button>
        <button class="tab-btn" onclick="showTab('proxies',this)">🌐 Proxies</button>
        <button class="tab-btn" onclick="showTab('email',this)">📧 Email / SMTP</button>
        <button class="tab-btn" onclick="showTab('discord',this)">💬 Discord</button>
      </div>

      <!-- General Tab -->
      <div class="tab-panel active" id="tab-general">
        <div class="card">
          <h2>General Settings</h2>
          <div class="form-grid">
            <label>Portal Name</label>    <input id="gen-name" placeholder="PokemonBot">
            <label>Concurrency</label>    <input id="gen-concurrency" type="number" value="10" min="1">
            <label>Request Timeout (s)</label> <input id="gen-timeout" type="number" value="30" min="1" step="1">
            <label>Max Retries per Request</label> <input id="gen-max-retries" type="number" value="10" min="1" step="1">
            <label>Base URL (external SSL)</label> <input id="gen-base-url" placeholder="https://mybot.example.com">
          </div>
          <p style="color:var(--text-muted);font-size:.85rem;margin:.4rem 0 0">
            Set the public base URL if you access this dashboard through a reverse proxy / external SSL.
            Leave empty for default. Example: <code>https://mybot.example.com</code>
          </p>
          <div class="controls" style="margin-top:.8rem">
            <button class="btn btn-accent" onclick="saveGeneral()">💾 Save</button>
          </div>
        </div>
      </div>

      <!-- Proxies Tab -->
      <div class="tab-panel" id="tab-proxies">
        <div class="card">
          <h2>Proxy List</h2>
          <p style="font-size:.82rem;color:var(--muted);margin-bottom:.7rem">Manage your rotating proxy list. The bot must be restarted to use updated proxies. You can also auto-populate from public sources.</p>

          <div class="stats" style="margin-bottom:.8rem">
            <div class="stat purple"><div class="num" id="px-total">0</div><div class="lbl">Total Proxies</div></div>
            <div class="stat green"><div class="num" id="px-requests">0</div><div class="lbl">Total Requests</div></div>
            <div class="stat green"><div class="num" id="px-successes">0</div><div class="lbl">Success</div></div>
            <div class="stat red"><div class="num" id="px-failures">0</div><div class="lbl">Total Failures</div></div>
          </div>

          <div class="controls" style="margin-bottom:.8rem">
            <button class="btn btn-accent" id="btn-fetch-proxies" onclick="fetchPublicProxies()">🌍 Fetch Public Proxies</button>
            <button class="btn btn-outline" onclick="refreshProxies()">🔄 Refresh</button>
          </div>

          <table class="tbl" id="proxy-table">
            <thead><tr><th>#</th><th>Protocol</th><th>Host</th><th>Port</th><th>Requests</th><th>Successes</th><th>Failures</th><th></th></tr></thead>
            <tbody id="proxy-body"></tbody>
          </table>
          <div id="proxy-empty" style="text-align:center;padding:1.5rem;color:var(--muted);font-size:.85rem">No proxies configured. Click <b>Fetch Public Proxies</b> to get started.</div>

          <h3>Add Proxy</h3>
          <div class="form-grid" style="margin-bottom:.6rem">
            <label>Proxy</label> <input id="px-new" placeholder="protocol://host:port or host:port">
          </div>
          <div class="controls">
            <button class="btn btn-accent btn-sm" onclick="addProxy()">➕ Add</button>
          </div>

          <h3>Bulk Edit</h3>
          <p style="font-size:.78rem;color:var(--muted);margin-bottom:.4rem">One proxy per line. Existing list will be replaced.</p>
          <textarea id="px-bulk" style="width:100%;height:120px;background:var(--bg);border:1px solid var(--border);border-radius:5px;padding:.5rem;color:var(--text);font-family:monospace;font-size:.78rem;resize:vertical"></textarea>
          <div class="controls" style="margin-top:.5rem">
            <button class="btn btn-accent btn-sm" onclick="bulkSaveProxies()">💾 Save All</button>
          </div>
        </div>
      </div>

      <!-- Email Tab -->
      <div class="tab-panel" id="tab-email">
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
const API='{{BASE_URL}}';
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
  if(id==='settings'){loadGeneral();loadProxies();loadEmail();loadDiscord();}
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
    let checks=0,alerts=0,errors=0,successes=0;
    if(d.tasks)d.tasks.forEach(t=>{checks+=t.checks||0;alerts+=t.alerts||0;errors+=t.errors||0;successes+=t.successes||0;});
    document.getElementById('st-monitors').textContent=d.monitors;
    document.getElementById('st-checks').textContent=checks;
    document.getElementById('st-successes').textContent=successes;
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
        `<td>${t.checks}</td><td>${t.successes||0}</td><td>${t.alerts}</td><td>${t.errors}</td><td><span class="badge ${badge}">${esc(t.last_status||'–')}</span></td></tr>`;
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
  const list=window._cfgMonitors||[];
  if(!list.length&&d.tasks&&d.tasks.length){
    empty.style.display='none';
    body.innerHTML=d.tasks.map((t,i)=>{
      let badge='badge-muted';if(t.last_status==='in_stock')badge='badge-green';else if(t.last_status==='queue_active')badge='badge-yellow';
      return `<tr><td>${esc(t.name)}</td><td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><a href="${esc(t.url)}" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none">${esc(t.url)}</a></td>`+
      `<td>–</td><td>–</td><td>–</td><td><span class="badge badge-green">Yes</span></td><td>${t.checks}</td><td>${t.successes||0}</td><td>${t.alerts}</td><td>${t.errors}</td><td><span class="badge ${badge}">${esc(t.last_status||'–')}</span></td>`+
      `<td><button class="btn btn-outline btn-sm" onclick="editMonitor(${i})" title="Edit">✏️</button> <button class="btn btn-outline btn-sm" onclick="removeMonitor(${i})" title="Remove">🗑️</button></td></tr>`;
    }).join('');
    return;
  }
  if(!list.length){empty.style.display='';body.innerHTML='';return;}
  empty.style.display='none';
  body.innerHTML=list.map((m,i)=>{
    const rt=taskMap[m.name]||{};
    let badge='badge-muted';const st=rt.last_status||'';
    if(st==='in_stock')badge='badge-green';else if(st==='queue_active')badge='badge-yellow';
    const en=m.enabled!==false;
    const enBadge=en?'badge-green':'badge-red';
    const enLabel=en?'Yes':'No';
    const toggleIcon=en?'⏸️':'▶️';
    const toggleTitle=en?'Disable':'Enable';
    const rowStyle=en?'':'opacity:.55';
    return `<tr style="${rowStyle}"><td>${esc(m.name)}</td><td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><a href="${esc(m.url)}" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none">${esc(m.url)}</a></td>`+
    `<td>${esc(m.site)}</td><td>${esc((m.keywords||[]).join(', ')||'–')}</td><td>${m.interval}s</td>`+
    `<td><span class="badge ${enBadge}">${enLabel}</span></td>`+
    `<td>${rt.checks??'–'}</td><td>${rt.successes??'–'}</td><td>${rt.alerts??'–'}</td><td>${rt.errors??'–'}</td><td><span class="badge ${badge}">${esc(st||'–')}</span></td>`+
    `<td><button class="btn btn-outline btn-sm" onclick="toggleMonitor(${i})" title="${toggleTitle}">${toggleIcon}</button> <button class="btn btn-outline btn-sm" onclick="editMonitor(${i})" title="Edit">✏️</button> <button class="btn btn-outline btn-sm" onclick="removeMonitor(${i})" title="Remove">🗑️</button></td></tr>`;
  }).join('');
}

/* ---- Monitor management ---- */
async function addMonitor(){
  const url=document.getElementById('mon-url').value.trim();
  if(!url){toast('URL is required',false);return;}
  const body={
    name:document.getElementById('mon-name').value.trim()||url,
    url:url,
    site:document.getElementById('mon-site').value,
    keywords:document.getElementById('mon-keywords').value,
    interval:parseFloat(document.getElementById('mon-interval').value)||10
  };
  const r=await fetch(API+'/api/monitors',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(r.ok){
    toast('Monitor added! Stop and start the bot to activate it.',true);
    document.getElementById('mon-name').value='';
    document.getElementById('mon-url').value='';
    document.getElementById('mon-keywords').value='';
    document.getElementById('mon-interval').value='10';
    loadConfig();fetchStatus();
  }else{toast(d.error||'Failed to add',false);}
}
async function removeMonitor(idx){
  if(!confirm('Remove this monitor?'))return;
  const r=await fetch(API+'/api/monitors/'+idx,{method:'DELETE'});
  const d=await r.json();
  if(r.ok){toast('Monitor removed. Stop and start the bot to apply.',true);loadConfig();fetchStatus();}
  else toast(d.error||'Failed to remove',false);
}
async function toggleMonitor(idx){
  const m=(window._cfgMonitors||[])[idx];
  if(!m)return;
  const newEnabled=m.enabled===false?true:false;
  const r=await fetch(API+'/api/monitors/'+idx,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:newEnabled})});
  const d=await r.json();
  if(r.ok){toast('Monitor '+(newEnabled?'enabled':'disabled')+'. Stop and start the bot to apply.',true);loadConfig();fetchStatus();}
  else toast(d.error||'Failed to toggle',false);
}
function editMonitor(idx){
  const m=(window._cfgMonitors||[])[idx];
  if(!m)return;
  document.getElementById('edit-idx').value=idx;
  document.getElementById('edit-name').value=m.name||'';
  document.getElementById('edit-url').value=m.url||'';
  document.getElementById('edit-site').value=m.site||'pokemoncenter';
  document.getElementById('edit-keywords').value=(m.keywords||[]).join(', ');
  document.getElementById('edit-interval').value=m.interval||10;
  document.getElementById('edit-modal').style.display='flex';
}
function closeEditModal(){document.getElementById('edit-modal').style.display='none';}
async function saveEditMonitor(){
  const idx=document.getElementById('edit-idx').value;
  const body={
    name:document.getElementById('edit-name').value.trim(),
    url:document.getElementById('edit-url').value.trim(),
    site:document.getElementById('edit-site').value,
    keywords:document.getElementById('edit-keywords').value,
    interval:parseFloat(document.getElementById('edit-interval').value)||10
  };
  if(!body.url){toast('URL is required',false);return;}
  const r=await fetch(API+'/api/monitors/'+idx,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(r.ok){toast('Monitor updated. Stop and start the bot to apply.',true);closeEditModal();loadConfig();fetchStatus();}
  else toast(d.error||'Failed to update',false);
}

/* ---- Logs ---- */
const _ERR_RE=/Access denied|failed|\b403\b|connection error|timed?\s*out/i;
const _OK_RE=/\bOK\b|check #\d+ OK|started|stopped|saved|added|removed|updated|fetched|sent/i;
function logClass(l){
  if(l.includes('[ERROR]'))return 'log-error';
  if(_ERR_RE.test(l))return 'log-error';
  if(l.includes('[WARNING]'))return 'lvl-WARNING';
  if(_OK_RE.test(l))return 'log-success';
  if(l.includes('[DEBUG]'))return 'lvl-DEBUG';
  if(l.includes('[INFO]'))return 'lvl-INFO';
  return '';
}
async function fetchLogs(){
  try{
    const r=await fetch(API+'/api/logs');const d=await r.json();
    allLogs=d.lines||[];
    renderLogs();
    /* Dash recent logs – last 20 */
    const box=document.getElementById('dash-log-box');
    box.innerHTML=allLogs.slice(-20).map(l=>`<div class="log-line"><span class="${logClass(l)}">${esc(l)}</span></div>`).join('');
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
    return `<div class="log-line"><span class="${logClass(l)}">${esc(l)}</span></div>`;
  }).join('');
  if(document.getElementById('log-autoscroll').checked)box.scrollTop=box.scrollHeight;
}
async function clearLogs(){
  await fetch(API+'/api/logs/clear',{method:'POST'});
  allLogs=[];renderLogs();
  document.getElementById('dash-log-box').innerHTML='';
  toast('Logs cleared',true);
}
function copyLast20Logs(){
  const lines=allLogs.slice(-20).join('\n');
  navigator.clipboard.writeText(lines).then(()=>toast('Copied last 20 log lines',true)).catch(err=>{console.error('Clipboard write failed:',err);toast('Copy failed',false);});
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

/* ---- General settings ---- */
async function loadGeneral(){
  try{
    const r=await fetch(API+'/api/general-settings');const d=await r.json();
    document.getElementById('gen-name').value=d.portal_name||'';
    document.getElementById('gen-concurrency').value=d.concurrency||10;
    document.getElementById('gen-timeout').value=d.request_timeout||30;
    document.getElementById('gen-max-retries').value=d.max_retries||10;
    document.getElementById('gen-base-url').value=d.base_url||'';
  }catch(e){}
}
async function saveGeneral(){
  const body={
    portal_name:document.getElementById('gen-name').value.trim(),
    concurrency:parseInt(document.getElementById('gen-concurrency').value)||10,
    request_timeout:parseFloat(document.getElementById('gen-timeout').value)||30,
    max_retries:parseInt(document.getElementById('gen-max-retries').value)||10,
    base_url:document.getElementById('gen-base-url').value.trim()
  };
  const r=await fetch(API+'/api/general-settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.ok)toast('General settings saved. Reload the page to see changes.',true);else toast('Failed to save',false);
}

/* ---- Proxy management ---- */
async function loadProxies(){await refreshProxies();}
async function refreshProxies(){
  try{
    const r=await fetch(API+'/api/proxies');const d=await r.json();
    const list=d.proxies||[];
    let totalReq=0,totalFail=0,totalSucc=0;
    list.forEach(p=>{totalReq+=p.requests||0;totalFail+=p.failures||0;totalSucc+=p.successes||0;});
    document.getElementById('px-total').textContent=list.length;
    document.getElementById('px-requests').textContent=totalReq;
    document.getElementById('px-successes').textContent=totalSucc;
    document.getElementById('px-failures').textContent=totalFail;
    const body=document.getElementById('proxy-body');
    const empty=document.getElementById('proxy-empty');
    if(!list.length){empty.style.display='';body.innerHTML='';return;}
    empty.style.display='none';
    body.innerHTML=list.map((p,i)=>{
      const failCls=p.failures>0?' style="color:var(--accent2)"':'';
      const succCls=p.successes>0?' style="color:var(--accent)"':'';
      return `<tr><td>${i+1}</td><td>${esc(p.protocol)}</td><td>${esc(p.host)}</td><td>${p.port}</td>`+
      `<td>${p.requests}</td><td${succCls}>${p.successes}</td><td${failCls}>${p.failures}</td>`+
      `<td><button class="btn btn-outline btn-sm" onclick="removeProxy(${i})" title="Remove">🗑️</button></td></tr>`;
    }).join('');
    /* Also populate bulk textarea */
    document.getElementById('px-bulk').value=list.map(p=>p.url).join('\n');
  }catch(e){}
}
async function addProxy(){
  const raw=document.getElementById('px-new').value.trim();
  if(!raw){toast('Enter a proxy',false);return;}
  const r=await fetch(API+'/api/proxies',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proxy:raw})});
  const d=await r.json();
  if(r.ok){toast('Proxy added',true);document.getElementById('px-new').value='';refreshProxies();}
  else toast(d.error||'Failed',false);
}
async function removeProxy(idx){
  const r=await fetch(API+'/api/proxies/'+idx,{method:'DELETE'});
  const d=await r.json();
  if(r.ok){toast('Proxy removed',true);refreshProxies();}
  else toast(d.error||'Failed',false);
}
async function bulkSaveProxies(){
  const raw=document.getElementById('px-bulk').value;
  const lines=raw.split('\n').map(l=>l.trim()).filter(l=>l&&!l.startsWith('#'));
  const r=await fetch(API+'/api/proxies',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({proxies:lines})});
  const d=await r.json();
  if(r.ok){
    let msg='Saved '+d.count+' proxies';
    if(d.errors&&d.errors.length)msg+=' ('+d.errors.length+' errors)';
    toast(msg,true);refreshProxies();
  }else toast(d.error||'Failed',false);
}
async function fetchPublicProxies(){
  const btn=document.getElementById('btn-fetch-proxies');
  btn.disabled=true;btn.textContent='⏳ Fetching proxies…';
  toast('Fetching public proxies from multiple sources… this may take up to 20 seconds',true);
  try{
    const r=await fetch(API+'/api/proxies/fetch-public',{method:'POST'});
    const text=await r.text();
    let d;
    try{d=JSON.parse(text);}catch(pe){
      toast('Server error (HTTP '+r.status+'): '+(text.length>200?text.substring(0,200)+'…':text),false);return;
    }
    if(r.ok){toast('✅ Fetched '+d.new+' new proxies ('+d.total+' total)',true);refreshProxies();}
    else toast(d.error||'Failed to fetch public proxies',false);
  }catch(e){toast('Network error fetching proxies: '+e.message,false);}
  finally{btn.disabled=false;btn.textContent='🌍 Fetch Public Proxies';}
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
fetchStatus();fetchLogs();loadGeneral();loadConfig();
setInterval(()=>{fetchStatus();fetchLogs();},2000);
</script>
</body>
</html>"""


async def _index(request: web.Request) -> web.Response:
    state: _AppState = request.app["state"]
    name = html.escape(state.config.portal_name or "PokemonBot")
    base = html.escape(state.config.base_url.rstrip("/")) if state.config.base_url else ""
    page = _DASHBOARD_HTML.replace("{{PORTAL_NAME}}", name).replace("{{BASE_URL}}", base)
    return web.Response(text=page, content_type="text/html")


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
    app.router.add_get("/api/monitors", _api_monitors_list)
    app.router.add_post("/api/monitors", _api_monitors_add)
    app.router.add_delete("/api/monitors/{index}", _api_monitors_delete)
    app.router.add_put("/api/monitors/{index}", _api_monitors_update)
    app.router.add_get("/api/proxies", _api_proxies_list)
    app.router.add_post("/api/proxies", _api_proxies_add)
    app.router.add_put("/api/proxies", _api_proxies_bulk)
    app.router.add_delete("/api/proxies/{index}", _api_proxies_delete)
    app.router.add_post("/api/proxies/fetch-public", _api_proxies_fetch_public)
    app.router.add_get("/api/general-settings", _api_general_settings_get)
    app.router.add_post("/api/general-settings", _api_general_settings_post)

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

