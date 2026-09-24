"""
Dependency checks for the daily health card (``health_report.py``), wired up once in
``main._run_main_entry``.

Every check is read-only: in-memory state the bot already keeps, snapshot files it
already writes, or one short request with a timeout. Nothing here sends a Lark
message, re-reads a sheet, drives a browser, or adds/removes scheduler jobs.

Each check returns ``(status, detail)`` with status ``"ok"`` / ``"warn"`` / ``"fail"``,
or ``None`` for a feature that is switched off. Details never carry secrets or full URLs.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import requests

# Same host main.send_message / get_tenant_access_token use.
_LARK_BASE = "https://open.larksuite.com"
_HTTP_TIMEOUT = 8


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _is_off(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("0", "false", "no", "off")


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or str(default)).strip() or default)
    except ValueError:
        return default


def _ago(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s ago"
    if s < 90 * 60:
        return f"{s // 60}m ago"
    if s < 48 * 3600:
        return f"{s // 3600}h {s % 3600 // 60}m ago"
    return f"{s // 86400}d ago"


def _short(err: object, limit: int = 90) -> str:
    """One line of an error message, URLs dropped (sheet / wiki tokens live in them)."""
    text = re.sub(r"https?://\S+", "<url>", " ".join(str(err or "").split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _iso_z_age(stamp: object) -> float | None:
    """Seconds since an osmwatch ``updated_at`` (UTC, ``%Y-%m-%dT%H:%M:%SZ``)."""
    try:
        dt = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return time.time() - dt.timestamp()


def _uptime_sec(bot: Any) -> float:
    try:
        return time.time() - int(bot._BOT_STARTED_AT_MS) / 1000.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------
def _check_lark_api(bot: Any):
    """Fresh tenant-token fetch: proves APP_ID/APP_SECRET and the Open API (never cached)."""
    app_id = str(bot.APP_ID or "").strip()
    app_secret = str(bot.APP_SECRET or "").strip()
    if not (app_id and app_secret):
        return "fail", "APP_ID / APP_SECRET not set"
    t0 = time.monotonic()
    try:
        r = requests.post(
            f"{_LARK_BASE}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        return "fail", f"unreachable ({type(e).__name__})"
    ms = (time.monotonic() - t0) * 1000
    try:
        body = r.json()
    except ValueError:
        return "fail", f"HTTP {r.status_code}, reply is not JSON"
    if not isinstance(body, dict):
        return "fail", f"HTTP {r.status_code}, unexpected reply"
    if body.get("code") != 0 or not body.get("tenant_access_token"):
        return "fail", f"token rejected: code {body.get('code')} {_short(body.get('msg'), 60)}".strip()
    if not str(bot.BOT_OPEN_ID or "").strip():
        return "warn", f"token OK in {ms:.0f} ms, but BOT_OPEN_ID is unresolved (group @mentions are missed)"
    return "ok", f"tenant token OK in {ms:.0f} ms"


def _check_lark_connection(bot: Any):
    """Persistent connection (lark-oapi ws.Client) — the only way events reach this bot."""
    if not bot._lark_ws_uses_persistent_connection():
        return None, "LARK_EVENT_MODE=http (events arrive on the Flask webhook)"
    cli = getattr(bot, "_lark_ws_client", None)
    if cli is None:
        return "fail", "ws client not started"
    if not hasattr(cli, "_conn"):
        return None, "connection state not exposed by this lark-oapi version"
    if getattr(cli, "_conn", None) is None:
        return "fail", "disconnected (lark-oapi auto-reconnect pending)"
    return "ok", "connected"


def _check_scheduler(bot: Any):
    """APScheduler that fires /sst runs and maintenance reminders; stored /sst entries armed."""
    sch = bot.scheduler
    if not sch.running:
        return "fail", "APScheduler not running (/sst and reminders will not fire)"
    jobs = sch.get_jobs()
    ids = {str(j.id) for j in jobs}
    parts = [f"{len(jobs)} job{'' if len(jobs) == 1 else 's'}"]
    n_sst = sum(1 for i in ids if i.startswith("sst_run_"))
    if n_sst:
        parts.append(f"{n_sst} /sst run{'' if n_sst == 1 else 's'}")
    nxt = min((j.next_run_time for j in jobs if getattr(j, "next_run_time", None)), default=None)
    if nxt is not None:
        parts.append("next " + nxt.strftime("%m-%d %H:%M"))
    # A stored future /sst schedule without its job means a restart did not re-arm it.
    unarmed = 0
    sst = sys.modules.get("sst")
    if sst is not None:
        now = datetime.now()  # sst stores naive host-local times
        for entry in sst.store_list():
            when = sst._entry_when(entry)
            if when is not None and when > now and f"sst_run_{entry.get('id')}" not in ids:
                unarmed += 1
    if unarmed:
        return "warn", f"{unarmed} stored /sst schedule(s) not armed · " + " · ".join(parts)
    return "ok", "running · " + " · ".join(parts)


def _check_egm_scrape(bot: Any):
    """webmachine scrape loop (webmachine_data.json) — feeds set/unset, status, /findmachine, /sst."""
    if _is_off("WEBMACHINE_MOUNT_IN_MAIN"):
        return None, "disabled by WEBMACHINE_MOUNT_IN_MAIN"
    wm = sys.modules.get("webmachine")
    if wm is None:
        return "fail", "webmachine not loaded (mount failed at boot)"
    if not wm._scrape_enabled():
        return None, "disabled by WEBMACHINE_SCRAPE"
    if not wm._bg_started:
        return "fail", "scrape loop not started"
    with wm._scrape_lock:
        ts, rows, errs = float(wm._scrape_ts or 0), list(wm._scrape_rows), dict(wm._scrape_errs)
    interval = max(60, _int_env("WEBMACHINE_SCRAPE_INTERVAL_SEC", 900))
    # A cycle is the sleep plus the scrape itself (Chromium logins), hence the floor.
    stale_after = max(3 * interval, 900)
    if ts <= 0:
        if _uptime_sec(bot) < stale_after:
            return None, "first scrape still running after boot"
        return "fail", "no scrape has finished since boot"
    age = time.time() - ts
    # "skipped — same EGM as …" notes are dedupe info, not failures.
    real = sorted(k for k, v in errs.items() if not str(v).startswith("skipped"))
    nonprod = [k for k in real if ":" in k or k.upper() in ("QAT", "UAT")]
    prod_err = [k for k in real if k not in nonprod]
    deployments = [
        d.strip().upper() for d in (os.environ.get("WEBMACHINE_DEPLOYMENTS") or "prod,qat,uat").split(",")
    ]
    if "PROD" in deployments:
        g = wm._compute_stats(wm._filter_rows_by_deployment(rows, "PROD"))[0]
        label = "PROD"
    else:
        g = wm._compute_stats(rows)[0]
        label = "all"
    what = f"{label} {g['total']} machines ({g['online']} online)"
    tail = f" · QAT/UAT errors on {len(nonprod)} backend(s)" if nonprod else ""
    if not g["total"]:
        extra = f" · errors on {', '.join(prod_err[:6])}" if prod_err else ""
        return "fail", f"0 {label} machines in the last scrape ({_ago(age)}){extra}"
    if prod_err:
        return "warn", f"{what} · errors on {', '.join(prod_err[:6])} · last scrape {_ago(age)}{tail}"
    if age > stale_after:
        return "warn", f"{what} · last scrape {_ago(age)} (loop every {interval}s)"
    return "ok", f"{what} · last scrape {_ago(age)}{tail}"


def _check_osmwatch(bot: Any):
    """OSM-Watch warm browser/session plus the encoder + IP-audit snapshots it keeps fresh."""
    ow = sys.modules.get("osmwatch")
    if ow is None:
        if _is_off("OSMWATCH_WARM"):
            return None, "disabled by OSMWATCH_WARM"
        return "fail", "osmwatch not loaded (startup pre-warm failed)"
    if not ow._warm_enabled():
        return None, "disabled by OSMWATCH_WARM"
    w = ow._warm_singleton
    if w is None or not getattr(w, "_started", False):
        return "fail", "warm browser not started"
    # Exact name: "osmwatch-warm" is also a prefix of the -ka / -enc thread names.
    if not any(t.name == "osmwatch-warm" and t.is_alive() for t in threading.enumerate()):
        return "fail", "worker thread not running"
    bad: list[str] = []
    good: list[str] = []
    if ow._get_needs_manual():
        bad.append("session expired, waiting for a /loginosmwatch QR scan")
    elif w._login_in_progress:
        bad.append("QR login in progress")
    elif not w._healthy():
        bad.append("browser closed (relaunches on the next keepalive)")
    else:
        good.append("browser open")
    if ow._encoder_enabled():
        stale_after = 3 * ow._encoder_interval_sec()
        snaps = [("encoders", ow.load_latestencoder())]
        if ow._ipaudit_enabled():
            snaps.append(("IPs", ow.load_latestmachineip()))
        for label, snap in snaps:
            age = _iso_z_age((snap or {}).get("updated_at"))
            if age is None:
                bad.append(f"no {label} snapshot")
                continue
            n = (snap or {}).get("machine_count")
            if age > stale_after:
                bad.append(f"{label} stale ({_ago(age)})")
            else:
                good.append(f"{label} {n} machines {_ago(age)}")
            if label == "IPs" and not (snap or {}).get("cmdb_available", True):
                bad.append("CMDB column unavailable")
    if bad:
        return "warn", " · ".join(bad + good)
    return "ok", " · ".join(good) or "running"


def _check_machine_ip_sheet(bot: Any):
    """OSM Machine List wiki sheet cache (machineip.py) — first IP source for /encoder /main …"""
    mi = sys.modules.get("machineip")
    if mi is None:
        return "fail", "machineip not loaded"
    if not mi.enabled():
        return None, "disabled by MACHINE_IP_SHEET"
    with mi._CACHE_LOCK:
        c = dict(mi._CACHE)
    snap = c.get("snap") or {}
    ts, fail_ts = float(c.get("ts") or 0), float(c.get("fail_ts") or 0)
    err = str(c.get("error") or "")
    now = time.time()
    if not snap:
        if c.get("busy"):
            return None, "first sheet read in progress"
        return "fail", "sheet never read" + (f": {_short(err)}" if err else "")
    what = f"{snap.get('count')} machines from {len(snap.get('tabs') or [])} tabs"
    if err and fail_ts >= ts:
        return "warn", f"last refresh failed {_ago(now - fail_ts)}: {_short(err)} · serving {what}"
    # Re-read on demand (TTL), so an old read on a quiet day is normal.
    read = f"read {_ago(now - ts)}" if ts else f"disk copy from {snap.get('updated_at') or '?'}"
    return "ok", f"{what} · {read}"


def _check_oss_logs(bot: Any):
    """OSS log bucket — default log source for /checkcredit, /checkmachinelog, /stuckcredit."""
    cc = sys.modules.get("checkcredit")
    if cc is not None:
        if not cc.checkcredit_use_oss_source():
            return None, "disabled by CHECKCREDIT_USE_OSS"
        base = str(cc.DEFAULT_OSS_LIST_URL)
    else:
        if _is_off("CHECKCREDIT_USE_OSS"):
            return None, "disabled by CHECKCREDIT_USE_OSS"
        base = os.environ.get("OSM_LOG_OSS_LIST_URL") or "https://oss-osm-log.osmplay.com/"
    base = base.rstrip("/")
    host = urlparse(base).hostname or "OSS"
    t0 = time.monotonic()
    try:
        # Same ListObjects call list_oss_logic_log_basenames_for_date makes, capped at one key.
        r = requests.get(
            base + "/",
            params={"prefix": "MINIPC/", "max-keys": "1"},
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "machinebot-health/1.0"},
        )
    except requests.RequestException as e:
        return "fail", f"{host} unreachable ({type(e).__name__})"
    ms = (time.monotonic() - t0) * 1000
    if r.status_code == 200 and "<ListBucketResult" in (r.text or ""):
        return "ok", f"{host} list OK in {ms:.0f} ms"
    return "fail", f"{host} HTTP {r.status_code} in {ms:.0f} ms"


def _check_llm(bot: Any):
    """Ollama behind BOT_CHAT_API_BASE — jackpot vision, AI log summaries, intent parsing."""
    base = (os.environ.get("BOT_CHAT_API_BASE") or "").strip().rstrip("/")
    model = (os.environ.get("BOT_CHAT_MODEL") or "").strip()
    if not base or not model:
        return None, "not configured (BOT_CHAT_API_BASE / BOT_CHAT_MODEL)"
    root = base[:-3].rstrip("/") if base.endswith("/v1") else base
    u = urlparse(root)
    where = f"{u.hostname}:{u.port}" if u.port else (u.hostname or "LLM")
    if not ("11434" in root or "ollama" in root.lower() or u.hostname in ("127.0.0.1", "localhost")):
        return None, f"{where} is not Ollama; not probed"
    t0 = time.monotonic()
    try:
        r = requests.get(root + "/api/tags", timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        return "warn", f"{where} unreachable ({type(e).__name__}); AI features fall back"
    ms = (time.monotonic() - t0) * 1000
    try:
        names = {str(m.get("name") or "") for m in (r.json().get("models") or [])}
    except (ValueError, AttributeError, TypeError):
        return "warn", f"{where} HTTP {r.status_code}, not an Ollama reply"
    wanted = [model] + [m for m in [(os.environ.get("BOT_CHAT_VISION_MODEL") or "").strip()] if m and m != model]
    missing = [m for m in wanted if m not in names and f"{m}:latest" not in names]
    if missing:
        return "warn", f"{where} up, model not pulled: {', '.join(missing)}"
    return "ok", f"{where} · {', '.join(wanted)} available · {ms:.0f} ms"


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
def checks(bot: Any) -> list[tuple[str, Callable[[], Any]]]:
    """``(name, zero-arg check)`` pairs for ``health_report.start``; ``bot`` is the main module."""
    table = [
        ("Lark API", _check_lark_api),
        ("Lark connection", _check_lark_connection),
        ("Scheduler", _check_scheduler),
        ("EGM scrape (/wm)", _check_egm_scrape),
        ("OSM-Watch", _check_osmwatch),
        ("Machine IP sheet", _check_machine_ip_sheet),
        ("OSS machine logs", _check_oss_logs),
        ("LLM (Ollama)", _check_llm),
    ]
    return [(name, (lambda fn=fn: fn(bot))) for name, fn in table]


def expect_threads(bot: Any) -> list[str]:
    """Long-lived threads this process runs in its current configuration (call after boot setup)."""
    names: list[str] = []
    if bot._lark_ws_uses_persistent_connection():
        names.append("machinebot-flask")  # http mode serves Flask on the main thread instead
    try:
        if bot.scheduler.running:
            names.append("APScheduler")
    except Exception:
        pass
    wm = sys.modules.get("webmachine")
    if wm is not None and getattr(wm, "_bg_started", False):
        names.append("webmachine-scrape")
    ow = sys.modules.get("osmwatch")
    w = getattr(ow, "_warm_singleton", None) if ow is not None else None
    if w is not None and getattr(w, "_started", False):
        # "osmwatch-warm" itself is checked by exact name in _check_osmwatch.
        names.append("osmwatch-warm-ka")
        try:
            if ow._encoder_enabled():
                names.append("osmwatch-warm-enc")
        except Exception:
            pass
    return names
