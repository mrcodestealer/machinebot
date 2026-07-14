#!/usr/bin/env python3
"""
osmwatch.py — let the bot reach the OSM-Watch dashboard (Lark-OAuth protected)
and screenshot it.

Why this shape
--------------
``osm-watch.cliveslot.com`` is gated by **Lark OAuth** for app
``cli_a83bcf5f86fad029`` and asks for *user* contact scopes
(``contact:user.base``, ``contact:user.email``). That needs a **logged-in Lark
user session**, not this bot's app token (our APP_ID is a different Lark app and
we don't hold osm-watch's secret). So there is no server-to-server token path —
the only robust route is a real browser session.

The session (cookies + localStorage) is stored in a portable ``osmwatch.json``
so it SURVIVES a service restart (reloaded on launch) and can be copied to
another machine. A long-lived **warm browser** (mirroring third_http_warm_pool)
stays open in the bot process, refreshes the session while idle, and — when the
session expires — auto-pushes a Lark QR to the group. If nobody scans it in time
the bot stops (no spam) and waits for a manual ``/loginosmwatch`` to re-send.

How the bot gets in — Lark QR only
----------------------------------
Sign in with Lark by scanning a QR. On a headless server nobody can see the
browser, so the QR is pushed to a Lark chat (default: the lab group); scan it
from your phone. The session is then saved to osmwatch.json and reused headless.
When it expires the warm browser auto-pushes a fresh QR; if that QR times out
unscanned, tag the bot with /loginosmwatch to get a new one.

Usage
-----
  python osmwatch.py --login             # push a login QR to the group; scan it
  python osmwatch.py                     # capture, save osmwatch.png locally
  python osmwatch.py --send              # also send the PNG to the Lark duty chat
  python osmwatch.py --send-to oc_xxx    # send to a specific chat_id
  python osmwatch.py --url https://osm-watch.cliveslot.com/some/page --send
  python osmwatch.py --headed            # watch it run locally (debug)

Requires: pip install playwright python-dotenv requests && playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

# Windows consoles default to cp1252 and choke on the ✅/→ symbols below.
# The Linux server runs UTF-8 already; this is a no-op there.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is a declared dep
    load_dotenv = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Paths / env
# ---------------------------------------------------------------------------
_ROOT_DIR = Path(__file__).resolve().parent
_ENV_PATH = _ROOT_DIR / ".env"
if load_dotenv is not None:
    load_dotenv(str(_ENV_PATH))

# Session store. We keep cookies + localStorage in a portable storage_state JSON
# so the session survives a service restart (reloaded on launch) and can even be
# copied to another machine. Gitignored — it holds live session tokens.
OSMWATCH_JSON = _ROOT_DIR / os.getenv("OSMWATCH_STATE_FILE", "osmwatch.json")
# QR image the bot posts to Lark, and a tiny file remembering whether we're
# waiting for a manual /loginosmwatch (so a restart doesn't re-spam the group).
QR_PNG = _ROOT_DIR / "osmwatch_qr.png"
_LOGIN_STATE = _ROOT_DIR / "browser_data" / "osmwatch_login.json"

# The dashboard + the full Lark-OAuth login entry point you were given.
OSM_BASE = os.getenv("OSMWATCH_BASE_URL", "https://osm-watch.cliveslot.com").rstrip("/")
LOGIN_URL = os.getenv(
    "OSMWATCH_LOGIN_URL",
    "https://accounts.larksuite.com/accounts/page/login?app_id=12&no_trap=1"
    "&redirect_uri=https%3A%2F%2Fopen.larksuite.com%2Fopen-apis%2Fauthen%2Fv1%2Fauthorize"
    "%3Fapp_id%3Dcli_a83bcf5f86fad029%26redirect_uri%3Dhttps%253A%252F%252Fosm-watch.cliveslot.com"
    "%252Fauth%252Flark%252Fcallback%252F%26response_type%3Dcode"
    "%26scope%3Dcontact%253Auser.base%253Areadonly%2Bcontact%253Auser.email%253Areadonly",
)

# Hosts that mean "you are NOT authenticated yet" (Lark login / consent walls).
_AUTH_WALL_HOSTS = ("accounts.larksuite.com", "accounts.feishu.cn", "open.larksuite.com", "open.feishu.cn")

# osm-watch serves its OWN login wall at these paths (same host as the dashboard),
# so a host match alone does not prove we're signed in. The page shows
# "Welcome Back / Sign in with Lark / Use password instead".
_LOGIN_PATH_HINTS = ("/login", "/signin", "/sign-in", "/auth/lark", "/auth/login", "/auth/callback")

DEFAULT_SHOT = _ROOT_DIR / "osmwatch.png"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _truthy(val: str | None) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "yes", "on", "y")


def _headless_default() -> bool:
    """Headless unless BOT_PLAYWRIGHT_HEADLESS says otherwise (matches the repo)."""
    v = os.getenv("BOT_PLAYWRIGHT_HEADLESS")
    if v is None:
        return True
    return _truthy(v)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _is_auth_wall(url: str) -> bool:
    h = _host(url)
    return any(h == w or h.endswith("." + w) for w in _AUTH_WALL_HOSTS)


def _on_osmwatch(url: str) -> bool:
    return _host(url) == _host(OSM_BASE)


def _is_login_path(url: str) -> bool:
    p = (urlparse(url).path or "").lower()
    return any(p == h or p.startswith(h) for h in _LOGIN_PATH_HINTS)


def _authenticated(url: str) -> bool:
    """Signed in = on the osm-watch host AND not sitting on a login/auth wall.

    URL-only signal — use :func:`_classify` after a navigation to also rule out
    Cloudflare/WAF block pages (which live on the same host, path ``/``)."""
    return _on_osmwatch(url) and not _is_auth_wall(url) and not _is_login_path(url)


# Cloudflare / WAF interstitials render on the osm-watch host itself, so a URL
# check alone would mistake them for the dashboard. These phrases identify them.
_BLOCK_MARKERS = (
    "been blocked",
    "attention required",
    "verify you are human",
    "checking your browser",
    "access denied",
    "cloudflare ray id",
)


def _looks_blocked(page) -> bool:
    try:
        body = (page.inner_text("body") or "")[:800].lower()
    except Exception:
        return False
    return any(m in body for m in _BLOCK_MARKERS)


def _classify(page, resp) -> str:
    """Post-navigation verdict: 'blocked' | 'login' | 'error' | 'authenticated'."""
    status = resp.status if resp else None
    if _looks_blocked(page):
        return "blocked"
    url = page.url
    if not _on_osmwatch(url) or _is_auth_wall(url) or _is_login_path(url):
        return "login"
    if status is not None and status >= 400:
        return "error"
    return "authenticated"


# ---------------------------------------------------------------------------
# Lark image send (self-contained; uses this bot's APP_ID/APP_SECRET)
# ---------------------------------------------------------------------------
def _lark_base() -> str:
    return os.getenv("LARK_OPEN_BASE", "https://open.larksuite.com").rstrip("/")


def get_tenant_access_token() -> str:
    app_id = os.getenv("APP_ID")
    app_secret = os.getenv("APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("APP_ID / APP_SECRET not set in environment (.env)")
    url = f"{_lark_base()}/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": app_id, "app_secret": app_secret}, timeout=30)
    result = resp.json()
    if result.get("code") != 0:
        raise RuntimeError(f"Failed to get tenant token: {result}")
    return result["tenant_access_token"]


def upload_image_lark(image_path: str) -> str | None:
    """Upload a PNG/JPEG for im/v1/messages msg_type=image; returns image_key or None."""
    token = get_tenant_access_token()
    url = f"{_lark_base()}/open-apis/im/v1/images"
    headers = {"Authorization": f"Bearer {token}"}
    ext = os.path.splitext(image_path)[1].lower()
    mime, _ = mimetypes.guess_type(image_path)
    if not mime or mime not in ("image/png", "image/jpeg"):
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    with open(image_path, "rb") as f:
        files = {"image": (os.path.basename(image_path), f, mime)}
        data = {"image_type": "message"}
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    result = resp.json()
    if result.get("code") == 0:
        return result.get("data", {}).get("image_key")
    print(f"❌ Lark image upload failed: {result}")
    return None


def send_image_message(chat_id: str, image_key: str) -> dict:
    import json

    token = get_tenant_access_token()
    url = f"{_lark_base()}/open-apis/im/v1/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": chat_id,
        "msg_type": "image",
        "content": json.dumps({"image_key": image_key}),
    }
    params = {"receive_id_type": "chat_id"}
    return requests.post(url, headers=headers, params=params, json=payload, timeout=30).json()


def send_text_message(chat_id: str, text: str) -> dict:
    import json

    token = get_tenant_access_token()
    url = f"{_lark_base()}/open-apis/im/v1/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }
    params = {"receive_id_type": "chat_id"}
    return requests.post(url, headers=headers, params=params, json=payload, timeout=30).json()


def send_screenshot_to_lark(shot_path: str, chat_id: str) -> bool:
    key = upload_image_lark(shot_path)
    if not key:
        return False
    resp = send_image_message(chat_id, key)
    if resp.get("code") == 0:
        print(f"✅ Screenshot sent to Lark chat {chat_id}")
        return True
    print(f"❌ Lark send failed: {resp}")
    return False


# ---------------------------------------------------------------------------
# Browser flows
# ---------------------------------------------------------------------------
def _open(p, *, headless: bool):
    """Launch a browser + context, restoring the saved session from osmwatch.json."""
    browser = p.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    kwargs = {
        "viewport": {"width": 1600, "height": 900},
        "ignore_https_errors": True,
        "user_agent": _UA,
        "locale": "en-US",
        "timezone_id": os.getenv("OSMWATCH_TZ", "Asia/Manila"),
    }
    if OSMWATCH_JSON.exists():
        kwargs["storage_state"] = str(OSMWATCH_JSON)
    context = browser.new_context(**kwargs)
    # Light touch so trivial bot checks don't flag us; NOT a Cloudflare bypass.
    try:
        context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    except Exception:
        pass
    page = context.pages[0] if context.pages else context.new_page()
    return browser, context, page


def _save_state(context) -> None:
    """Persist cookies + localStorage so the session survives a service restart."""
    try:
        context.storage_state(path=str(OSMWATCH_JSON))
    except Exception as e:
        print(f"[osmwatch] could not save session to {OSMWATCH_JSON.name}: {e!r}", flush=True)


# --- "waiting for manual /loginosmwatch" flag (persisted across restarts) -----
def _set_needs_manual(val: bool) -> None:
    try:
        _LOGIN_STATE.parent.mkdir(parents=True, exist_ok=True)
        _LOGIN_STATE.write_text(json.dumps({"needs_manual": bool(val)}))
    except Exception:
        pass


def _get_needs_manual() -> bool:
    try:
        return bool(json.loads(_LOGIN_STATE.read_text()).get("needs_manual"))
    except Exception:
        return False


# --- warm-browser configuration ----------------------------------------------
def _warm_enabled() -> bool:
    return _truthy(os.getenv("OSMWATCH_WARM", "1"))


def _qr_chat_default() -> str:
    return os.getenv("OSMWATCH_QR_CHAT_ID", "oc_ad9b5bdbb2826ba2ee9730920ef25432").strip()


def _keepalive_sec() -> int:
    try:
        return max(120, int(os.getenv("OSMWATCH_KEEPALIVE_SEC", "1800")))
    except ValueError:
        return 1800


def _login_timeout_s() -> int:
    try:
        return max(30, int(os.getenv("OSMWATCH_LOGIN_TIMEOUT", "240")))
    except ValueError:
        return 240


def _settle_url(page, *, seconds: float = 20.0) -> str:
    """Wait through any silent SSO redirect chain until the URL stops moving."""
    deadline = time.time() + seconds
    last = page.url
    stable_for = 0.0
    while time.time() < deadline:
        page.wait_for_timeout(700)
        cur = page.url
        if cur == last:
            stable_for += 0.7
            if stable_for >= 2.0:
                break
        else:
            stable_for = 0.0
            last = cur
    return page.url


def _find_qr_element(page, *, tries: int = 8):
    """Return the visible, roughly-square QR canvas/img, or None."""
    for _ in range(max(1, tries)):
        for el in page.query_selector_all("canvas, img[src*='qr'], img[alt*='QR'], img[alt*='qr']"):
            try:
                if not el.is_visible():
                    continue
                b = el.bounding_box()
                if not b:
                    continue
                w, h = b.get("width", 0), b.get("height", 0)
                if 120 <= w <= 400 and 120 <= h <= 400 and 0.8 <= (w / max(h, 1)) <= 1.25:
                    return el
            except Exception:
                continue
        page.wait_for_timeout(700)
    return None


def _reveal_qr(page) -> None:
    """Lark defaults to email login; the top-right corner icon toggles QR view."""
    for attempt in (
        lambda: page.click(".login-qr-switch-box", position={"x": 388, "y": 12}, force=True, timeout=4000),
        lambda: page.click(".login-qr-switch-box", force=True, timeout=3000),
    ):
        try:
            attempt()
            page.wait_for_timeout(1200)
            return
        except Exception:
            continue


def _capture_qr(page, out_path: Path) -> Path:
    """Screenshot the Lark QR (tight crop). Toggle to QR view only if needed —
    re-clicking the toggle when a QR is already shown flips back to email login."""
    el = _find_qr_element(page, tries=1)  # already in QR view (e.g. periodic re-send)?
    if el is None:
        _reveal_qr(page)
        el = _find_qr_element(page, tries=8)
    if el is not None:
        try:
            el.screenshot(path=str(out_path))
            return out_path
        except Exception:
            pass
    page.screenshot(path=str(out_path), full_page=True)
    return out_path


# Hosts of the Lark QR login page (where re-sending a fresh QR makes sense).
_QR_PAGE_HOSTS = ("accounts.larksuite.com", "accounts.feishu.cn")

# Consent-screen host + "Authorize" labels across locales (English default + zh).
_CONSENT_HOSTS = ("open.larksuite.com", "open.feishu.cn")
_AUTHORIZE_LABELS = ("Authorize", "授权", "同意授权", "同意", "允许")


def _click_authorize(page, *, log=print) -> bool:
    """After the QR is scanned, Lark shows an OAuth consent screen. Click its
    primary 'Authorize' button (never 'Reject'). Robust to non-<button> markup
    and iframes. Returns True iff a click landed."""
    # Search the main frame and any iframes (Lark sometimes nests the consent UI).
    for frame in page.frames:
        for lab in _AUTHORIZE_LABELS:
            # 1) accessible role=button with this name
            try:
                loc = frame.get_by_role("button", name=lab, exact=True)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=3000)
                    return True
            except Exception:
                pass
            # 2) any element whose exact text is the label (div/span/a acting as a button)
            try:
                loc = frame.get_by_text(lab, exact=True)
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    if el.is_visible():
                        el.click(timeout=3000)
                        return True
            except Exception:
                pass
            # 3) CSS :has-text fallback (real <button> element)
            try:
                el = frame.query_selector(f"button:has-text('{lab}')")
                if el and el.is_visible():
                    el.click(timeout=3000)
                    return True
            except Exception:
                pass
    return False


def _log_consent_candidates(page, log) -> None:
    """One-time debug: dump the visible clickable labels on the consent screen."""
    try:
        cands = page.eval_on_selector_all(
            "button, [role=button], a, input[type=submit]",
            "els => els.filter(e => e.offsetParent !== null)"
            ".map(e => (e.innerText || e.value || '').trim()).filter(Boolean).slice(0, 25)",
        )
        log(f"[osmwatch] consent screen at {page.url} — clickable labels: {cands}")
    except Exception as e:
        log(f"[osmwatch] consent candidate probe failed: {e!r}")


def _qr_login_on_page(page, *, qr_chat_id: str | None, timeout_s: int, resend_sec: int = 90, log=print) -> bool:
    """Drive the Lark QR login on an already-open page.

    Pushes the QR to ``qr_chat_id`` (re-sending a fresh one every ``resend_sec``
    because Lark QRs expire) and polls until authenticated or ``timeout_s``.
    Returns True iff we land on the authenticated dashboard. Caller saves state.
    """
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass

    def push_qr(first: bool) -> None:
        if not qr_chat_id:
            return
        try:
            _capture_qr(page, QR_PNG)
            if first:
                send_text_message(
                    qr_chat_id,
                    "🔐 OSM-Watch login: scan this QR with your Lark app to sign the bot in. "
                    "A fresh QR is re-sent every ~90s until login completes.",
                )
            send_screenshot_to_lark(str(QR_PNG), qr_chat_id)
        except Exception as e:
            log(f"⚠️  Could not push QR to Lark: {e!r}")

    push_qr(first=True)
    deadline = time.time() + timeout_s
    next_resend = time.time() + resend_sec
    approved_note = False
    consent_logged = False
    while time.time() < deadline:
        if _authenticated(page.url):
            _settle_url(page, seconds=8)
            if _authenticated(page.url):
                return True

        # After the user scans, Lark shows a consent screen — auto-approve it.
        if _click_authorize(page, log=log):
            log("→ clicked Authorize on the consent screen")
            if qr_chat_id and not approved_note:
                approved_note = True
                try:
                    send_text_message(qr_chat_id, "✅ Scanned — approving access…")
                except Exception:
                    pass
            page.wait_for_timeout(1500)
            _settle_url(page, seconds=10)
            if _authenticated(page.url):
                return True
        elif _host(page.url) in _CONSENT_HOSTS and not consent_logged:
            # On the consent screen but no Authorize button matched — dump what's
            # there once so the selector can be fixed from the logs.
            consent_logged = True
            _log_consent_candidates(page, log)

        # Only re-send a fresh QR while still on the QR login page (not after the
        # scan, when we're on the consent screen or redirecting to the dashboard).
        if (
            qr_chat_id
            and time.time() >= next_resend
            and _host(page.url) in _QR_PAGE_HOSTS
        ):
            push_qr(first=False)
            next_resend = time.time() + resend_sec
        page.wait_for_timeout(1000)
    return _authenticated(page.url)


def do_login(*, timeout_s: int, headless: bool, qr_chat_id: str | None, resend_sec: int = 90) -> int:
    """One-shot CLI login; session is saved to osmwatch.json for the warm bot to reuse."""
    from playwright.sync_api import sync_playwright

    mode = "headless" if headless else "visible"
    print(f"→ Opening Lark login ({mode}) …")
    if qr_chat_id:
        print(f"  QR will be sent to Lark chat {qr_chat_id} — scan it with your Lark app.")
    elif not headless:
        print("  Scan the QR in the browser window with your Lark app.")

    with sync_playwright() as p:
        browser, ctx, page = _open(p, headless=headless)
        try:
            ok = _qr_login_on_page(page, qr_chat_id=qr_chat_id, timeout_s=timeout_s, resend_sec=resend_sec)
            if not ok:
                print(f"⚠️  Timed out after {timeout_s}s still at: {page.url}")
                print("   Login not completed. Re-run:  python osmwatch.py --login")
                if qr_chat_id:
                    try:
                        send_text_message(qr_chat_id, "⚠️ OSM-Watch login timed out — QR not scanned in time.")
                    except Exception:
                        pass
                return 1

            print(f"✅ Logged in — landed on {page.url}")
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            _save_state(ctx)
            _set_needs_manual(False)
            page.screenshot(path=str(DEFAULT_SHOT), full_page=True)
            print(f"✅ Session saved to {OSMWATCH_JSON.name}")
            print(f"✅ Screenshot: {DEFAULT_SHOT}")
            if qr_chat_id:
                try:
                    send_text_message(qr_chat_id, "✅ OSM-Watch: bot logged in successfully.")
                except Exception:
                    pass
            print("\nYou can now run headless anytime:  python osmwatch.py --send")
            return 0
        finally:
            try:
                browser.close()
            except Exception:
                pass


def do_capture(*, headless: bool, target_url: str, out_path: Path, timeout_ms: int) -> int:
    """Reuse the saved session then screenshot the dashboard."""
    from playwright.sync_api import sync_playwright

    mode = "headless" if headless else "headed"
    print(f"→ Opening {target_url} ({mode}, reusing saved session)…")

    with sync_playwright() as p:
        browser, ctx, page = _open(p, headless=headless)
        try:
            try:
                resp = page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as e:
                print(f"❌ Navigation error: {e!r}")
                return 1

            _settle_url(page, seconds=25)
            verdict = _classify(page, resp)

            if verdict == "blocked":
                print(f"⛔ Blocked by Cloudflare/WAF at {page.url}")
                print("   This is not a login problem. Allowlist the bot server's IP (or add a")
                print("   WAF bypass rule) in the osm-watch Cloudflare dashboard, then retry.")
                return 4
            if verdict != "authenticated":
                where = "Lark login wall" if _is_auth_wall(page.url) else "osm-watch login page"
                print(f"⚠️  Not authenticated — sitting on the {where}: {page.url}")
                print("   Do the one-time Lark login:  python osmwatch.py --login")
                return 2

            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass

            final = page.url
            _save_state(ctx)  # refresh the on-disk session after a good load
            out_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out_path), full_page=True)
            title = (page.title() or "").strip()
            print(f"✅ Access OK — {final}")
            print(f"   title: {title!r}")
            print(f"✅ Screenshot saved: {out_path}")
            return 0
        finally:
            try:
                browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Encoder / TRTC page inspection (one-shot DOM dump for building selectors)
# ---------------------------------------------------------------------------
# The /encoder-batchtools/trtc-details/ page is Lark + Cloudflare gated, so its
# DOM can't be inspected from a dev box. This one-shot mode reuses the saved
# session, walks the intended click flow (page -> red bar -> asset number ->
# POOL/MAIN/CCTV panel), and at each step saves a screenshot + full HTML + a
# JSON of "candidate" elements. That lets the real scraper's selectors be written
# from actual markup instead of guessed. Run it on the server:
#     python osmwatch.py --encoder-dump
# then send back the PNGs + *.candidates.json (or the printed summaries).
ENCODER_TRTC_URL = os.getenv(
    "OSMWATCH_ENCODER_URL",
    f"{OSM_BASE}/encoder-batchtools/trtc-details/",
)

# Runs inside the page. Collects visible, "notable" elements — interactive, or a
# reddish bar, or asset-like text (OSM/DHS/MDR/NCH/NWR/TBP/WF + digits), or
# POOL/MAIN/CCTV, or an IP — with enough attributes to craft Playwright selectors.
# v2: skips nav/SVG/icon chrome (which flooded the cap) and raises the cap so the
# scan actually reaches the data table below the toolbar.
_CANDIDATE_JS = r"""
() => {
  const ipRe = /\b\d{1,3}(\.\d{1,3}){3}\b/;
  const assetRe = /\b(OSM|DHS|MDR|NCH|NWR|TBP|WF)\s?-?\d+\b/i;
  const SKIP_TAGS = new Set(['script','style','head','meta','link','svg','g','circle',
    'path','rect','line','polygon','polyline','ellipse','use','defs','symbol','clippath',
    'mask','i','br','hr','img','noscript','picture','source']);
  const clsOf = (el) => {
    const c = el.className;
    if (c == null) return '';
    return (typeof c === 'string') ? c : (c.baseVal || '');
  };
  const cssPath = (el) => {
    if (!(el instanceof Element)) return '';
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 6) {
      let sel = cur.nodeName.toLowerCase();
      if (cur.classList && cur.classList.length) {
        sel += '.' + [...cur.classList].slice(0, 3).map(c => CSS.escape(c)).join('.');
      }
      const parent = cur.parentNode;
      if (parent && parent.children) {
        const same = [...parent.children].filter(c => c.nodeName === cur.nodeName);
        if (same.length > 1) sel += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
      }
      parts.unshift(sel);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };
  const isRed = (col) => {
    const m = (col || '').match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([0-9.]+))?/);
    if (!m) return false;
    const R=+m[1], G=+m[2], B=+m[3], A=(m[4]===undefined?1:+m[4]);
    return A > 0.3 && R > 110 && R > G*1.4 && R > B*1.4;
  };
  const rows = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('*')) {
    const tag = el.nodeName.toLowerCase();
    if (SKIP_TAGS.has(tag)) continue;
    // Skip page chrome (navbars/subnav/header/footer) — it flooded v1's cap.
    if (el.closest('nav, .navbar, .secondary-nav, .encoder-subnav, header, footer')) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const text = (el.innerText || el.value || '').trim().replace(/\s+/g, ' ');
    const bg = cs.backgroundColor || '';
    const clsStr = clsOf(el);
    const reddish = isRed(bg) || isRed(cs.borderTopColor) || /(^|[\s-])(red|danger|alert|warn)/i.test(clsStr);
    const interactive = ['button','a','input','select','textarea'].includes(tag)
      || el.getAttribute('role') === 'button'
      || el.hasAttribute('onclick')
      || el.getAttribute('tabindex') !== null
      || cs.cursor === 'pointer';
    const childCount = el.childElementCount;
    const shortText = text.length > 0 && text.length < 60 && childCount < 8;
    const isAsset = shortText && assetRe.test(text);
    const isPMC = shortText && /(POOL|MAIN|CCTV)/i.test(text);
    const hasIp = text.length < 200 && ipRe.test(text);
    const reddishBar = reddish && r.width >= r.height;   // bar-ish, not a red dot
    if (!(interactive || reddishBar || isAsset || isPMC || hasIp)) continue;
    const selector = cssPath(el);
    const key = selector + '|' + text.slice(0, 40);
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push({
      tag, id: el.id || '', cls: clsStr.slice(0, 120),
      role: el.getAttribute('role') || '', href: el.getAttribute('href') || '',
      text: text.slice(0, 80), bg,
      w: Math.round(r.width), h: Math.round(r.height),
      x: Math.round(r.x), y: Math.round(r.y),
      reddish, interactive, isAsset, isPMC, hasIp, selector,
    });
    if (rows.length >= 1500) break;
  }
  return rows;
}
"""

# Extracts the data region: every <table> (headers + sample rows + row count) and
# a few "row template" outerHTML samples for any element that carries an IP or an
# asset id (so non-<table> card/grid layouts are covered too).
_TABLE_JS = r"""
() => {
  const clean = (s) => (s || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const ipRe = /\b\d{1,3}(\.\d{1,3}){3}\b/;
  const assetRe = /\b(OSM|DHS|MDR|NCH|NWR|TBP|WF)\s?-?\d+\b/i;
  const clsFirst = (el) => {
    const c = el.className;
    const s = (c == null) ? '' : (typeof c === 'string' ? c : (c.baseVal || ''));
    return s.trim().split(/\s+/).filter(Boolean).slice(0, 2).join('.');
  };
  const selOf = (el) => el.id ? '#' + el.id
    : (el.nodeName.toLowerCase() + (clsFirst(el) ? '.' + clsFirst(el) : ''));
  const out = { tables: [], rowSamples: [] };
  for (const t of document.querySelectorAll('table')) {
    const headers = [...t.querySelectorAll('thead th, thead td')].map(th => clean(th.innerText));
    let bodyRows = [...t.querySelectorAll('tbody tr')];
    if (!bodyRows.length) bodyRows = [...t.querySelectorAll('tr')];
    const sample = bodyRows.slice(0, 8).map(tr => [...tr.children].map(td => clean(td.innerText)));
    out.tables.push({ selector: selOf(t), headers, rowCount: bodyRows.length, sample });
  }
  // Row-template samples: smallest elements matching common row/card selectors
  // that contain an IP or asset id — dedupe by class signature.
  const seenSig = new Set();
  const cand = document.querySelectorAll(
    'tr, [class*="row"], [class*="card"], [class*="item"], [class*="asset"], [class*="trtc"], li');
  for (const el of cand) {
    const txt = (el.innerText || '').replace(/\s+/g, ' ');
    if (!(ipRe.test(txt) || assetRe.test(txt))) continue;
    if (txt.length > 400) continue;               // skip big containers
    const sig = el.nodeName + '|' + clsFirst(el);
    if (seenSig.has(sig)) continue;
    seenSig.add(sig);
    out.rowSamples.push({
      selector: selOf(el),
      text: txt.slice(0, 200),
      outerHTML: el.outerHTML.slice(0, 1200),
    });
    if (out.rowSamples.length >= 8) break;
  }
  return out;
}
"""


def _encoder_dump_step(page, outdir: Path, tag: str, *, send_to: str | None = None) -> list[dict]:
    """Save screenshot + full HTML + candidate JSON for the current page state.

    Returns the candidate rows (also printed, grouped, to the console)."""
    outdir.mkdir(parents=True, exist_ok=True)
    png = outdir / f"{tag}.png"
    html = outdir / f"{tag}.html"
    cand = outdir / f"{tag}.candidates.json"
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass
    try:
        page.screenshot(path=str(png), full_page=True)
    except Exception as e:
        print(f"[encoder-dump] screenshot {tag} failed: {e!r}", flush=True)
    try:
        html.write_text(page.content(), encoding="utf-8")
    except Exception as e:
        print(f"[encoder-dump] html {tag} failed: {e!r}", flush=True)
    rows: list[dict] = []
    try:
        rows = page.evaluate(_CANDIDATE_JS) or []
    except Exception as e:
        print(f"[encoder-dump] candidate scan {tag} failed: {e!r}", flush=True)
    try:
        cand.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    tables: dict = {}
    try:
        tables = page.evaluate(_TABLE_JS) or {}
    except Exception as e:
        print(f"[encoder-dump] table scan {tag} failed: {e!r}", flush=True)
    try:
        (outdir / f"{tag}.tables.json").write_text(
            json.dumps(tables, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    try:
        title = page.title()
    except Exception:
        title = ""
    print(f"\n===== [{tag}] {page.url}  title={title!r} =====", flush=True)
    print(f"  saved: {png.name}, {html.name}, {cand.name}, {tag}.tables.json  "
          f"({len(rows)} candidates)", flush=True)

    # ---- data table(s) — the most useful part for the scraper ----
    for t in (tables.get("tables") or []):
        print(f"  --- TABLE {t.get('selector')} · {t.get('rowCount')} rows ---", flush=True)
        print(f"      headers: {t.get('headers')}", flush=True)
        for sr in (t.get("sample") or [])[:6]:
            print(f"      row: {sr}", flush=True)
    for rs in (tables.get("rowSamples") or []):
        print(f"  --- ROW SAMPLE {rs.get('selector')} ---", flush=True)
        print(f"      text: {rs.get('text')!r}", flush=True)
        print(f"      html: {rs.get('outerHTML')}", flush=True)

    def _show(label: str, items: list[dict], n: int = 25) -> None:
        if not items:
            return
        print(f"  --- {label} ({len(items)}) ---", flush=True)
        for c in items[:n]:
            print(
                f"   - <{c['tag']}> {c['w']}x{c['h']} @({c['x']},{c['y']}) bg={c['bg']} "
                f"cls={c['cls']!r} text={c['text']!r}\n       sel: {c['selector']}",
                flush=True,
            )

    _show("REDDISH / bar", [c for c in rows if c["reddish"]], n=30)
    _show("ASSET-like text", [c for c in rows if c["isAsset"]], n=40)
    _show("POOL/MAIN/CCTV", [c for c in rows if c["isPMC"]], n=40)
    _show("IP-bearing", [c for c in rows if c["hasIp"]], n=40)
    _show(
        "other interactive (content area)",
        [c for c in rows if c["interactive"] and not (c["reddish"] or c["isAsset"] or c["isPMC"] or c["hasIp"])],
        n=40,
    )
    if send_to:
        try:
            send_screenshot_to_lark(str(png), send_to)
        except Exception:
            pass
    return rows


def _dump_try_click(page, selector: str | None, *, kind: str) -> bool:
    """Click a user-given selector, else auto-pick the best candidate for ``kind``
    ('redbar' = largest reddish element; 'asset' = first asset-like element)."""
    if selector:
        try:
            page.click(selector, timeout=5_000)
            print(f"[encoder-dump] clicked {kind} via --{kind} selector: {selector}", flush=True)
            return True
        except Exception as e:
            print(f"[encoder-dump] {kind} selector click failed ({selector}): {e!r}", flush=True)
            return False
    try:
        rows = page.evaluate(_CANDIDATE_JS) or []
    except Exception:
        rows = []
    if kind == "redbar":
        picks = sorted((c for c in rows if c["reddish"]), key=lambda c: c["w"] * c["h"], reverse=True)
    else:
        picks = sorted((c for c in rows if c["isAsset"]), key=lambda c: (c["y"], c["x"]))
    pick = picks[0] if picks else None
    if not pick:
        return False
    sel = pick["selector"]
    try:
        page.click(sel, timeout=5_000)
        print(f"[encoder-dump] auto-clicked {kind}: {pick['text']!r}  sel={sel}", flush=True)
        return True
    except Exception as e:
        print(f"[encoder-dump] auto-click {kind} failed ({sel}): {e!r} — trying text fallback", flush=True)
        try:
            page.get_by_text(pick["text"], exact=False).first.click(timeout=4_000)
            print(f"[encoder-dump] {kind} clicked via text fallback: {pick['text']!r}", flush=True)
            return True
        except Exception as e2:
            print(f"[encoder-dump] {kind} text fallback failed: {e2!r}", flush=True)
            return False


def do_encoder_dump(
    *,
    headless: bool,
    url: str,
    outdir: Path,
    redbar_sel: str | None,
    asset_sel: str | None,
    search: str | None,
    search_sel: str,
    send_to: str | None,
    timeout_ms: int,
) -> int:
    """One-shot: dump the encoder/TRTC page and its data table for selector authoring.

    Flow: load -> dump 00-initial (incl. table extract). If ``search`` is given,
    type it into the built-in filter box and dump the filtered result. Otherwise
    walk the red-bar -> asset click flow and dump each state."""
    from playwright.sync_api import sync_playwright

    print(f"→ Encoder DOM dump: {url}")
    print(f"  output dir: {outdir}")
    with sync_playwright() as p:
        browser, ctx, page = _open(p, headless=headless)
        try:
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as e:
                print(f"❌ Navigation error: {e!r}")
                return 1
            _settle_url(page, seconds=20)
            verdict = _classify(page, resp)
            if verdict != "authenticated":
                print(f"⚠️  Not authenticated / blocked ({verdict}) at {page.url}")
                if verdict == "blocked":
                    print("   Cloudflare/WAF block — allowlist the server IP, then retry.")
                else:
                    print("   Do the Lark login first:  python osmwatch.py --login")
                _encoder_dump_step(page, outdir, "00-initial", send_to=send_to)
                return 2

            # Give the SPA a moment to fetch + render the table after settle.
            try:
                page.wait_for_timeout(2500)
            except Exception:
                pass
            _encoder_dump_step(page, outdir, "00-initial", send_to=send_to)

            if search:
                # Test the built-in filter box — this is likely how /encoder will work.
                print(f"[encoder-dump] typing search {search!r} into {search_sel}", flush=True)
                try:
                    page.fill(search_sel, search, timeout=5_000)
                    page.wait_for_timeout(1500)
                except Exception as e:
                    print(f"[encoder-dump] search fill failed ({search_sel}): {e!r}", flush=True)
                _encoder_dump_step(page, outdir, "01-after-search", send_to=send_to)

            # Step 1 — the red bar (reveals the asset list, "picture 3").
            if _dump_try_click(page, redbar_sel, kind="redbar"):
                _settle_url(page, seconds=6)
                _encoder_dump_step(page, outdir, "02-after-redbar", send_to=send_to)
            else:
                print('[encoder-dump] no red-bar click (see 00-initial candidates; '
                      're-run with --redbar "<css>").')

            # Step 2 — an asset number (reveals the POOL/MAIN/CCTV IP panel).
            if _dump_try_click(page, asset_sel, kind="asset"):
                _settle_url(page, seconds=6)
                _encoder_dump_step(page, outdir, "03-after-asset", send_to=send_to)
            else:
                print('[encoder-dump] no asset click (see prior candidates; '
                      're-run with --asset "<css>").')

            print(f"\n✅ Dump complete → {outdir}")
            print("   Send me: the PNG screenshots + the *.tables.json / *.candidates.json "
                  "files (or paste the grouped summaries above).")
            return 0
        finally:
            try:
                browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Encoder / TRTC scraper -> latestencoder.json  (+ /encoder lookup)
# ---------------------------------------------------------------------------
# The trtc-details page renders ALL rows (~3300) into a single table
# (#trtcTable / table.trtc-flat); each machine has 3 rows (one per stream type),
# each carrying data-* attributes. So the whole dataset is read in one
# page.evaluate — no per-asset clicking. Rows are grouped by machine into
# latestencoder.json; the warm browser re-scrapes on a silent interval, and the
# /encoder lookup reads the FILE (never the browser) so replies are instant.
LATESTENCODER_JSON = _ROOT_DIR / os.getenv("OSMWATCH_ENCODER_DATA_FILE", "latestencoder.json")

# data-type on each row -> our normalized stream type. "top" is the dashboard's
# internal name for the POOL stream (its badge reads "🎮 POOL").
_ENCODER_TYPE_MAP = {"main": "main", "top": "pool", "pool": "pool", "cctv": "cctv"}
_ENCODER_TYPE_ORDER = ("main", "pool", "cctv")
_ENCODER_TYPE_LABEL = {"main": "MAIN", "pool": "POOL", "cctv": "CCTV"}
_ENCODER_TYPE_EMOJI = {"main": "🎬", "pool": "🎱", "cctv": "📹"}
_ENCODER_QUERY_SPLIT = re.compile(r"[\s,&]+")


def _encoder_enabled() -> bool:
    return _truthy(os.getenv("OSMWATCH_ENCODER", "1"))


def _encoder_interval_sec() -> int:
    try:
        return max(300, int(os.getenv("OSMWATCH_ENCODER_INTERVAL_SEC", "1800")))
    except ValueError:
        return 1800


def _encoder_max_matches() -> int:
    try:
        return max(1, int(os.getenv("OSMWATCH_ENCODER_MAX_MATCHES", "50")))
    except ValueError:
        return 50


# Reads every data row from the trtc table. Returns raw per-row dicts; grouping
# stays in Python so it's unit-testable without a browser. The clipboard glyph
# (📋, U+1F4CB) that the copy buttons render into cell text is stripped out.
_ENCODER_ROWS_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\u{1F4CB}/gu, '').replace(/\s+/g, ' ').trim();
  // The IP / room / user cells are visually truncated (text-overflow ellipsis)
  // but their copy button carries the FULL value: copyToClipboard('<value>', ..).
  const copyVal = (td) => {
    if (!td) return null;
    const btn = td.querySelector('[onclick*="copyToClipboard"]');
    if (!btn) return null;
    const oc = btn.getAttribute('onclick') || '';
    const m = oc.match(/copyToClipboard\(\s*'([^']*)'/) || oc.match(/copyToClipboard\(\s*"([^"]*)"/);
    return m ? m[1] : null;
  };
  // USER SIG is different: its copy button is copyUserSig('<id>', ..) (an id, not
  // the value) and the <code> text is ellipsis-truncated. The FULL sig is in the
  // code element's data-original-sig attribute.
  const sigVal = (td) => {
    if (!td) return '';
    const c = td.querySelector('[data-original-sig]');
    const v = c ? c.getAttribute('data-original-sig') : null;
    if (v) return v;
    const cp = copyVal(td);
    return (cp !== null && cp !== '') ? cp : clean(td.innerText);
  };
  const rows = [];
  const trs = document.querySelectorAll(
    '#trtcTable tbody tr.trtc-row, table.trtc-flat tbody tr.trtc-row');
  for (const tr of trs) {
    const tds = tr.children;
    const cell = (i) => tds[i] ? clean(tds[i].innerText) : '';
    const full = (i) => {
      const td = tds[i];
      if (!td) return '';
      const c = copyVal(td);
      return (c !== null && c !== '') ? c : clean(td.innerText);
    };
    rows.push({
      env: (tr.getAttribute('data-env') || cell(0) || '').trim(),
      machine: (tr.getAttribute('data-machine') || cell(1) || '').trim(),
      type: (tr.getAttribute('data-type') || '').trim().toLowerCase(),
      type_label: cell(2),
      ip: (tr.getAttribute('data-ip') || full(3) || '').trim(),
      room_id: full(4),
      user_id: full(5),
      user_sig: sigVal(tds[6]),
      status: cell(7) || (tr.getAttribute('data-status') || ''),
      updated: cell(8),
    });
  }
  return rows;
}
"""


def _group_encoder_rows(rows: list[dict]) -> dict[str, dict]:
    """Group raw scrape rows into {UPPER_MACHINE: {machine, env, types:{...}}}."""
    machines: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        machine = str(r.get("machine") or "").strip()
        if not machine:
            continue
        env = str(r.get("env") or "").strip()
        raw_type = str(r.get("type") or "").strip().lower()
        typ = _ENCODER_TYPE_MAP.get(raw_type, raw_type or "?")
        entry = machines.setdefault(machine.upper(), {"machine": machine, "env": env, "types": {}})
        if env and not entry.get("env"):
            entry["env"] = env
        entry["types"][typ] = {
            "ip": str(r.get("ip") or "").strip(),
            "room_id": str(r.get("room_id") or "").strip(),
            "user_id": str(r.get("user_id") or "").strip(),
            "user_sig": str(r.get("user_sig") or "").strip(),
            "status": str(r.get("status") or "").strip(),
            "updated": str(r.get("updated") or "").strip(),
        }
    return machines


def _build_encoder_snapshot(rows: list[dict]) -> dict:
    machines = _group_encoder_rows(rows)
    return {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": ENCODER_TRTC_URL,
        "row_count": len(rows),
        "machine_count": len(machines),
        "machines": machines,
    }


def _persist_latestencoder(snapshot: dict) -> None:
    # Atomic write: the warm browser rewrites this every ~30 min on its worker
    # thread while /encoder reads it from separate command threads. Writing to a
    # temp file then os.replace() means a reader never sees a truncated/partial
    # file (which, since status cells carry a multibyte glyph like "⚠", could
    # otherwise split a UTF-8 sequence mid-read).
    try:
        data = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
        tmp = LATESTENCODER_JSON.with_name(LATESTENCODER_JSON.name + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, LATESTENCODER_JSON)
    except OSError as e:
        print(f"[osmwatch-enc] could not save {LATESTENCODER_JSON.name}: {e!r}", flush=True)


def load_latestencoder() -> dict | None:
    # ValueError covers both json.JSONDecodeError and UnicodeDecodeError, so a
    # torn read degrades to the graceful "not ready yet" path instead of crashing.
    try:
        raw = json.loads(LATESTENCODER_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _parse_encoder_queries(arg: str) -> list[str]:
    return [t for t in _ENCODER_QUERY_SPLIT.split((arg or "").strip()) if t]


def _fmt_encoder_machine(entry: dict) -> str:
    machine = entry.get("machine") or "?"
    env = entry.get("env") or ""
    lines = [f"🎥 **{machine}**" + (f" ({env})" if env else "")]
    types = entry.get("types") or {}
    ordered = list(_ENCODER_TYPE_ORDER) + [t for t in types if t not in _ENCODER_TYPE_ORDER]
    for t in ordered:
        info = types.get(t)
        if not info:
            continue
        ip = info.get("ip") or "—"
        meta = " · ".join(x for x in (info.get("status") or "", info.get("updated") or "") if x)
        label = _ENCODER_TYPE_LABEL.get(t, t.upper())
        head = f" • **{label} Encoder** IP ADDRESS — `{ip}`"
        if meta:
            head += f"  {meta}"
        lines.append(head)
        room, user, sig = info.get("room_id") or "", info.get("user_id") or "", info.get("user_sig") or ""
        if room:
            lines.append(f"     ROOM ID  : `{room}`")
        if user:
            lines.append(f"     User ID  : `{user}`")
        if sig:
            lines.append(f"     User Sig : `{sig}`")
    return "\n".join(lines)


def query_encoder(arg: str) -> list[str]:
    """Look up machines in latestencoder.json; return Lark-ready message string(s).

    Tokens split on whitespace / comma / ``&`` and matched as case-insensitive
    substrings of the machine name (e.g. ``nwr2205`` -> ``NWR2205``)."""
    snap = load_latestencoder()
    if not snap or not snap.get("machines"):
        return ["⚠️ Encoder data isn't ready yet — it's scraped in the background. "
                "Try again shortly (or run `/encoder refresh`)."]
    machines = snap.get("machines") or {}
    updated = snap.get("updated_at") or "?"
    tokens = _parse_encoder_queries(arg)
    if not tokens:
        return [f"Usage: `/encoder <machine>` — e.g. `/encoder nwr2205` "
                f"(multiple: `nwr2205 & nwr2206`).\n"
                f"📅 {snap.get('machine_count', '?')} machines · updated {updated}"]

    matched: dict[str, dict] = {}  # ordered, deduped by machine key
    for tok in tokens:
        up = tok.upper()
        for key, entry in machines.items():
            if up in key:
                matched.setdefault(key, entry)

    if not matched:
        return [f"🔎 No encoder machine matched: {', '.join(tokens)}\n📅 updated {updated}"]

    cap = _encoder_max_matches()
    keys = list(matched.keys())
    truncated = len(keys) > cap
    keys = keys[:cap]

    header = (f"🎬 **Encoder RTC** — {len(matched)} match(es) for {', '.join(tokens)}"
              f"\n📅 updated {updated}")
    blocks = [header] + [_fmt_encoder_machine(matched[k]) for k in keys]
    if truncated:
        blocks.append(f"… {len(matched) - cap} more not shown — narrow your query.")

    messages: list[str] = []
    cur = ""
    for b in blocks:
        piece = ("\n\n" + b) if cur else b
        if cur and len(cur) + len(piece) > 3500:
            messages.append(cur)
            cur = b
        else:
            cur += piece
    if cur:
        messages.append(cur)
    return messages


# --- Lark interactive card (emoji) -------------------------------------------
def _encoder_status_emoji(status: str) -> str:
    s = (status or "").lower()
    if "not" in s or "✗" in status or "✖" in status or "⚠" in status:
        return "⚠️"
    if "sync" in s or "ok" in s or "✓" in status or "✔" in status:
        return "✅"
    return "▫️"


def _encoder_machine_md(entry: dict) -> str:
    """lark_md body for one machine card block (emoji per stream type)."""
    machine = entry.get("machine") or "?"
    env = entry.get("env") or ""
    parts = [f"🎥 **{machine}**" + (f"  ·  {env}" if env else "")]
    types = entry.get("types") or {}
    ordered = list(_ENCODER_TYPE_ORDER) + [t for t in types if t not in _ENCODER_TYPE_ORDER]
    for t in ordered:
        info = types.get(t)
        if not info:
            continue
        emoji = _ENCODER_TYPE_EMOJI.get(t, "🎞️")
        label = _ENCODER_TYPE_LABEL.get(t, t.upper())
        ip = info.get("ip") or "—"
        status_raw = info.get("status") or ""
        status_txt = re.sub(r"^[✓✔✗✖⚠️\s]+", "", status_raw).strip()
        status_bit = f"{_encoder_status_emoji(status_raw)} {status_txt}".strip() if (status_raw or status_txt) else ""
        meta = " · ".join(x for x in (status_bit, info.get("updated") or "") if x)
        block = [f"{emoji} **{label} Encoder** — IP ADDRESS `{ip}`"]
        if meta:
            block.append(meta)
        room, user, sig = info.get("room_id") or "", info.get("user_id") or "", info.get("user_sig") or ""
        if room:
            block.append(f"🏠 ROOM ID  : `{room}`")
        if user:
            block.append(f"👤 User ID  : `{user}`")
        if sig:
            block.append(f"🔑 User Sig : `{sig}`")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def build_encoder_card(arg: str) -> dict | None:
    """Build a Lark schema-2.0 interactive card for the query, or None to signal
    the caller to fall back to plain text (no data / no tokens / no match)."""
    snap = load_latestencoder()
    if not snap or not snap.get("machines"):
        return None
    tokens = _parse_encoder_queries(arg)
    if not tokens:
        return None
    machines = snap.get("machines") or {}
    matched: dict[str, dict] = {}
    for tok in tokens:
        up = tok.upper()
        for key, entry in machines.items():
            if up in key:
                matched.setdefault(key, entry)
    if not matched:
        return None

    cap = _encoder_max_matches()
    keys = list(matched.keys())
    truncated = len(keys) > cap
    keys = keys[:cap]
    updated = snap.get("updated_at") or "?"

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md",
         "content": f"🔎 **{len(matched)}** match(es) for `{', '.join(tokens)}`\n📅 updated {updated}"}},
        {"tag": "hr"},
    ]
    for i, k in enumerate(keys):
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": _encoder_machine_md(matched[k])}})
        if i != len(keys) - 1:
            elements.append({"tag": "hr"})
    if truncated:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": f"… {len(matched) - cap} more not shown — narrow your query."}})

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "🎬 Encoder RTC Info"}},
        "body": {"elements": elements},
    }


def do_encoder_scrape_cli(*, headless: bool, timeout_ms: int) -> int:
    """One-shot CLI: scrape the encoder/TRTC page into latestencoder.json (reuses
    the saved session). Handy for testing the scraper without the bot running."""
    from playwright.sync_api import sync_playwright

    print(f"→ Encoder scrape: {ENCODER_TRTC_URL}")
    with sync_playwright() as p:
        browser, ctx, page = _open(p, headless=headless)
        try:
            resp = page.goto(ENCODER_TRTC_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            _settle_url(page, seconds=15)
            verdict = _classify(page, resp)
            if verdict != "authenticated":
                print(f"⚠️  Not authenticated / blocked ({verdict}) at {page.url}")
                print("   Log in first:  python osmwatch.py --login")
                return 2
            try:
                page.wait_for_selector("tr.trtc-row", timeout=30_000)
            except Exception:
                pass
            deadline, last, stable = time.time() + 40, -1, 0
            while time.time() < deadline:
                try:
                    n = page.evaluate("() => document.querySelectorAll('tr.trtc-row').length")
                except Exception:
                    n = 0
                if n > 0 and n == last:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable, last = 0, n
                page.wait_for_timeout(700)
            rows = page.evaluate(_ENCODER_ROWS_JS) or []
            snap = _build_encoder_snapshot(rows)
            _persist_latestencoder(snap)
            _save_state(ctx)
            print(f"✅ scraped {snap['row_count']} rows / {snap['machine_count']} machines "
                  f"-> {LATESTENCODER_JSON}")
            return 0
        finally:
            try:
                browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Warm browser (long-lived, in-process; mirrors third_http_warm_pool)
# ---------------------------------------------------------------------------
# Playwright's sync API is thread-confined, so ALL browser calls run on one
# dedicated worker thread. Other threads submit tasks via a queue and (for
# capture) block on an Event. A keepalive loop refreshes the session while idle
# so it never dies mid-request; on expiry it auto-pushes a QR to the group once,
# then waits for a manual /loginosmwatch (tracked by the needs_manual flag).
class _OsmWatchWarm:
    def __init__(self) -> None:
        self._tasks: queue.Queue[dict] = queue.Queue()
        self._p = None
        self._browser = None
        self._context = None
        self._page = None
        self._login_in_progress = False
        self._started = False
        self._start_lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._started = True
            threading.Thread(target=self._loop, name="osmwatch-warm", daemon=True).start()
            threading.Thread(target=self._keepalive_loop, name="osmwatch-warm-ka", daemon=True).start()
            if _encoder_enabled():
                threading.Thread(target=self._encoder_loop, name="osmwatch-warm-enc", daemon=True).start()

    def _launch(self) -> None:
        from playwright.sync_api import sync_playwright

        self._teardown()
        self._p = sync_playwright().start()
        self._browser, self._context, self._page = _open(self._p, headless=_headless_default())
        print("[osmwatch-warm] browser launched (kept open).", flush=True)

    def _teardown(self) -> None:
        for closer in (
            lambda: self._browser.close() if self._browser else None,
            lambda: self._p.stop() if self._p else None,
        ):
            try:
                closer()
            except Exception:
                pass
        self._p = self._browser = self._context = self._page = None

    def _healthy(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    # -- public submit API (thread-safe) -------------------------------------
    def submit_ensure(self, *, auto: bool = True) -> None:
        self._tasks.put({"kind": "ensure", "auto": auto})

    def request_login(self, chat_id: str | None = None) -> None:
        self._tasks.put({"kind": "login", "chat_id": chat_id})

    def capture(self, *, url: str | None = None, chat_id: str | None = None, timeout_ms: int = 60_000) -> dict:
        done = threading.Event()
        box: dict = {}
        self._tasks.put({
            "kind": "capture", "url": url or OSM_BASE, "chat_id": chat_id,
            "timeout_ms": timeout_ms, "done": done, "box": box,
        })
        done.wait()
        return box

    def scrape_encoder(self, *, chat_id: str | None = None, block: bool = False) -> dict:
        """Queue an encoder scrape. With ``block`` the caller waits for completion."""
        task: dict = {"kind": "encoder_scrape", "auto": False, "chat_id": chat_id}
        if block:
            done = threading.Event()
            box: dict = {}
            task["done"] = done
            task["box"] = box
            self._tasks.put(task)
            done.wait()
            return box
        self._tasks.put(task)
        return {}

    # -- worker loop ---------------------------------------------------------
    def _loop(self) -> None:
        while True:
            task = self._tasks.get()
            kind = task.get("kind")
            try:
                if kind in ("ensure", "keepalive"):
                    self._handle_ensure(task)
                elif kind == "login":
                    self._handle_login(task)
                elif kind == "capture":
                    self._handle_capture(task)
                elif kind == "encoder_scrape":
                    self._handle_encoder_scrape(task)
            except Exception as e:
                print(f"[osmwatch-warm] task {kind} error: {e!r}", flush=True)
                self._teardown()
            finally:
                if task.get("done"):
                    task["done"].set()

    def _keepalive_loop(self) -> None:
        while True:
            time.sleep(_keepalive_sec())
            self._tasks.put({"kind": "keepalive", "auto": True})

    def _encoder_loop(self) -> None:
        # Small initial delay so the startup ensure/login settles first, then a
        # silent re-scrape on the interval keeps latestencoder.json fresh.
        time.sleep(min(90, _encoder_interval_sec()))
        while True:
            self._tasks.put({"kind": "encoder_scrape", "auto": True})
            time.sleep(_encoder_interval_sec())

    # -- task handlers (worker thread only) ----------------------------------
    def _check_auth(self, timeout_ms: int = 60_000) -> str:
        """Return _classify() verdict after loading the dashboard root."""
        resp = self._page.goto(OSM_BASE, wait_until="domcontentloaded", timeout=timeout_ms)
        _settle_url(self._page, seconds=20)
        return _classify(self._page, resp)

    def _notify_blocked(self, chat_id: str | None) -> None:
        target = chat_id or (_qr_chat_default() or None)
        if not target:
            return
        try:
            send_text_message(
                target,
                "⛔ OSM-Watch: the request was blocked by Cloudflare/WAF (not a login issue). "
                "Allowlist the bot server's IP or add a WAF bypass rule in the osm-watch "
                "Cloudflare dashboard, then try again.",
            )
        except Exception:
            pass

    def _handle_ensure(self, task: dict) -> None:
        if not self._healthy():
            self._launch()
        verdict = self._check_auth()
        if verdict == "authenticated":
            _save_state(self._context)
            _set_needs_manual(False)
            return
        if verdict == "blocked":
            print("[osmwatch-warm] blocked by Cloudflare/WAF — QR won't help; skipping.", flush=True)
            return  # don't QR-spam; a login won't fix a WAF block
        if verdict == "error":
            print("[osmwatch-warm] dashboard returned an error status; will retry next keepalive.", flush=True)
            return
        # verdict == "login" → session expired.
        if task.get("auto") and not _get_needs_manual():
            self._do_qr_login()
        else:
            print("[osmwatch-warm] session expired; waiting for /loginosmwatch", flush=True)

    def _handle_login(self, task: dict) -> None:
        chat_id = task.get("chat_id")
        if self._login_in_progress:
            if chat_id:
                send_text_message(chat_id, "⏳ OSM-Watch login already in progress — check the group for the QR.")
            return
        if not self._healthy():
            self._launch()
        verdict = self._check_auth()
        if verdict == "authenticated":
            _save_state(self._context)
            _set_needs_manual(False)
            if chat_id:
                send_text_message(chat_id, "✅ OSM-Watch is already logged in.")
            return
        if verdict == "blocked":
            self._notify_blocked(chat_id)
            return
        _set_needs_manual(False)  # forced fresh attempt
        self._do_qr_login(ack_chat=chat_id)

    def _do_qr_login(self, *, ack_chat: str | None = None) -> bool:
        qr_chat = _qr_chat_default() or None
        self._login_in_progress = True
        try:
            if ack_chat and ack_chat != qr_chat and qr_chat:
                try:
                    send_text_message(ack_chat, "📨 Sending a fresh OSM-Watch login QR to the group…")
                except Exception:
                    pass
            ok = _qr_login_on_page(self._page, qr_chat_id=qr_chat, timeout_s=_login_timeout_s())
            if ok:
                try:
                    self._page.goto(OSM_BASE, wait_until="domcontentloaded", timeout=60_000)
                    _settle_url(self._page, seconds=15)
                except Exception:
                    pass
                _save_state(self._context)
                _set_needs_manual(False)
                if qr_chat:
                    send_text_message(qr_chat, "✅ OSM-Watch: bot logged in successfully.")
                return True
            _set_needs_manual(True)
            if qr_chat:
                send_text_message(
                    qr_chat,
                    "⚠️ OSM-Watch login QR expired (not scanned in time). "
                    "Tag me and send /loginosmwatch to get a fresh QR.",
                )
            return False
        finally:
            self._login_in_progress = False

    def _handle_capture(self, task: dict) -> None:
        box = task["box"]
        chat_id = task.get("chat_id")
        timeout_ms = int(task.get("timeout_ms") or 60_000)
        try:
            if not self._healthy():
                self._launch()
            verdict = self._check_auth(timeout_ms)
            if verdict == "blocked":
                box["error"] = "blocked"
                self._notify_blocked(chat_id)
                return
            if verdict != "authenticated":
                if verdict == "login" and not _get_needs_manual():
                    self._do_qr_login()  # one auto attempt
                    verdict = self._check_auth(timeout_ms)
                if verdict != "authenticated":
                    box["error"] = "blocked" if verdict == "blocked" else "not_authenticated"
                    if verdict == "blocked":
                        self._notify_blocked(chat_id)
                    elif chat_id:
                        send_text_message(
                            chat_id,
                            "⚠️ OSM-Watch: not logged in. Tag me and send /loginosmwatch to sign in.",
                        )
                    return
            url = task.get("url") or OSM_BASE
            if self._page.url.rstrip("/") != url.rstrip("/"):
                resp = self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                _settle_url(self._page, seconds=15)
                v2 = _classify(self._page, resp)
                if v2 != "authenticated":
                    box["error"] = v2
                    if v2 == "blocked":
                        self._notify_blocked(chat_id)
                    return
            try:
                self._page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            _save_state(self._context)
            out = str(DEFAULT_SHOT)
            self._page.screenshot(path=out, full_page=True)
            box["path"] = out
            if chat_id:
                send_screenshot_to_lark(out, chat_id)
        except Exception as e:
            box["error"] = repr(e)
            self._teardown()

    def _wait_rows_stable(self, *, timeout_ms: int = 40_000) -> int:
        """Wait until the trtc table's row count stops growing (SPA finished
        rendering), then return the final count."""
        deadline = time.time() + timeout_ms / 1000.0
        last, stable = -1, 0
        while time.time() < deadline:
            try:
                n = self._page.evaluate(
                    "() => document.querySelectorAll('tr.trtc-row').length")
            except Exception:
                n = 0
            if n > 0 and n == last:
                stable += 1
                if stable >= 3:
                    return n
            else:
                stable = 0
                last = n
            self._page.wait_for_timeout(700)
        return max(last, 0)

    def _scrape_encoder_into_file(self, *, timeout_ms: int = 90_000) -> dict:
        """Load the encoder/TRTC page, read every row, persist latestencoder.json.
        Returns the snapshot. Raises on nav/auth failure (caller decides)."""
        resp = self._page.goto(ENCODER_TRTC_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        _settle_url(self._page, seconds=15)
        verdict = _classify(self._page, resp)
        if verdict != "authenticated":
            raise RuntimeError(f"encoder page not authenticated ({verdict})")
        try:
            self._page.wait_for_selector("tr.trtc-row", timeout=30_000)
        except Exception:
            pass
        self._wait_rows_stable()
        rows = self._page.evaluate(_ENCODER_ROWS_JS) or []
        snap = _build_encoder_snapshot(rows)
        _persist_latestencoder(snap)
        _save_state(self._context)
        print(f"[osmwatch-enc] scraped {snap['row_count']} rows / {snap['machine_count']} "
              f"machines -> {LATESTENCODER_JSON.name}", flush=True)
        return snap

    def _handle_encoder_scrape(self, task: dict) -> None:
        box = task.get("box")
        chat_id = task.get("chat_id")
        try:
            if not self._healthy():
                self._launch()
            verdict = self._check_auth()
            if verdict != "authenticated":
                if verdict == "login" and not _get_needs_manual():
                    self._do_qr_login()  # one auto attempt
                    verdict = self._check_auth()
                if verdict != "authenticated":
                    print(f"[osmwatch-enc] skip scrape — verdict={verdict}", flush=True)
                    if box is not None:
                        box["error"] = "blocked" if verdict == "blocked" else "not_authenticated"
                    if chat_id:
                        if verdict == "blocked":
                            self._notify_blocked(chat_id)
                        else:
                            send_text_message(
                                chat_id,
                                "⚠️ OSM-Watch: not logged in — tag me and send /loginosmwatch first.",
                            )
                    return
            snap = self._scrape_encoder_into_file()
            if box is not None:
                box["snapshot"] = snap
            if chat_id:
                send_text_message(
                    chat_id,
                    f"✅ Encoder data refreshed: {snap['machine_count']} machines "
                    f"({snap['row_count']} rows).",
                )
        except Exception as e:
            print(f"[osmwatch-enc] scrape error: {e!r}", flush=True)
            if box is not None:
                box["error"] = repr(e)
            self._teardown()


_warm_singleton: _OsmWatchWarm | None = None
_warm_lock = threading.Lock()


def warm() -> _OsmWatchWarm:
    global _warm_singleton
    with _warm_lock:
        if _warm_singleton is None:
            _warm_singleton = _OsmWatchWarm()
        return _warm_singleton


def prewarm_osmwatch_on_startup() -> None:
    """Called from main.py at boot: launch the warm browser + verify the session."""
    if not _warm_enabled():
        print("[osmwatch-warm] disabled (OSMWATCH_WARM=0)", flush=True)
        return
    w = warm()
    w.start()
    w.submit_ensure(auto=True)
    if _encoder_enabled():
        # Populate latestencoder.json soon after boot (don't wait a full interval).
        w.scrape_encoder()
    print("[osmwatch-warm] startup pre-warm submitted", flush=True)


def request_login(chat_id: str | None = None) -> None:
    """`/loginosmwatch` entry point — force a fresh QR to the group."""
    w = warm()
    w.start()
    w.request_login(chat_id)


def capture_and_send(chat_id: str | None = None, url: str | None = None) -> dict:
    """`/osmwatch` entry point — screenshot the dashboard and send it to ``chat_id``."""
    w = warm()
    w.start()
    return w.capture(url=url, chat_id=chat_id)


def refresh_encoder(chat_id: str | None = None) -> None:
    """`/encoder refresh` entry point — queue a fresh scrape of latestencoder.json."""
    w = warm()
    w.start()
    w.scrape_encoder(chat_id=chat_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Reach the Lark-protected OSM-Watch dashboard and screenshot it.",
    )
    ap.add_argument("--login", action="store_true",
                    help="One-time: open a visible browser to log in to Lark and save the session.")
    ap.add_argument("--headed", action="store_true",
                    help="Run the capture with a visible browser (debug); still reuses the session.")
    ap.add_argument("--url", default=OSM_BASE,
                    help=f"Page to open/screenshot (default: {OSM_BASE}).")
    ap.add_argument("--out", default=str(DEFAULT_SHOT),
                    help=f"Screenshot output path (default: {DEFAULT_SHOT}).")
    ap.add_argument("--send", action="store_true",
                    help="After a successful screenshot, send it to the Lark duty chat (DUTY_CHAT_ID).")
    ap.add_argument("--send-to", default=None,
                    help="Send the screenshot to this chat_id (implies --send).")
    ap.add_argument("--qr-to", default=os.getenv("OSMWATCH_QR_CHAT_ID", "oc_ad9b5bdbb2826ba2ee9730920ef25432"),
                    help="With --login, send the Lark QR to this chat_id so you can scan it "
                         "(default: the lab group; set OSMWATCH_QR_CHAT_ID to change; '' to disable).")
    ap.add_argument("--login-timeout", type=int, default=300,
                    help="Seconds to wait for you to finish the manual login (default 300).")
    ap.add_argument("--timeout-ms", type=int, default=60_000,
                    help="Navigation timeout for the capture (default 60000).")
    # --- encoder / TRTC page DOM dump (one-shot, for building the scraper) -----
    ap.add_argument("--encoder-dump", action="store_true",
                    help="Walk the encoder/TRTC page (page -> red bar -> asset) and dump "
                         "screenshots + HTML + candidate elements at each step.")
    ap.add_argument("--encoder-url", default=ENCODER_TRTC_URL,
                    help=f"Encoder/TRTC page to dump (default: {ENCODER_TRTC_URL}).")
    ap.add_argument("--dump-out", default=str(_ROOT_DIR / "encoder_dump"),
                    help="Directory for the encoder-dump output (default: ./encoder_dump).")
    ap.add_argument("--redbar", default=None,
                    help="CSS selector for the red bar to click (overrides auto-pick).")
    ap.add_argument("--asset", default=None,
                    help="CSS selector for the asset number to click (overrides auto-pick).")
    ap.add_argument("--search", default=None,
                    help="With --encoder-dump, type this into the page filter box and dump the result.")
    ap.add_argument("--search-sel", default="#searchFilter",
                    help="CSS selector for the filter box used by --search (default: #searchFilter).")
    ap.add_argument("--dump-to", default=None,
                    help="With --encoder-dump, also push each step's screenshot to this chat_id.")
    # --- encoder scraper / lookup (production feature) -------------------------
    ap.add_argument("--encoder-scrape", action="store_true",
                    help="One-shot: scrape the encoder/TRTC page into latestencoder.json.")
    ap.add_argument("--encoder-query", default=None,
                    help="Look up machine(s) in latestencoder.json (e.g. 'nwr2205 & nwr2206') and print.")
    args = ap.parse_args(argv)

    if args.encoder_query is not None:
        for msg in query_encoder(args.encoder_query):
            print(msg)
            print("-" * 40)
        return 0

    if args.encoder_scrape:
        return do_encoder_scrape_cli(
            headless=(_headless_default() and not args.headed),
            timeout_ms=max(5_000, args.timeout_ms),
        )

    if args.encoder_dump:
        return do_encoder_dump(
            headless=(_headless_default() and not args.headed),
            url=args.encoder_url,
            outdir=Path(args.dump_out),
            redbar_sel=args.redbar,
            asset_sel=args.asset,
            search=args.search,
            search_sel=args.search_sel,
            send_to=(args.dump_to.strip() or None) if args.dump_to else None,
            timeout_ms=max(5_000, args.timeout_ms),
        )

    if args.login:
        return do_login(
            timeout_s=max(30, args.login_timeout),
            headless=(_headless_default() and not args.headed),
            qr_chat_id=(args.qr_to.strip() or None) if args.qr_to else None,
        )

    out_path = Path(args.out)
    rc = do_capture(
        headless=(_headless_default() and not args.headed),
        target_url=args.url,
        out_path=out_path,
        timeout_ms=max(5_000, args.timeout_ms),
    )
    if rc != 0:
        return rc

    chat_id = args.send_to or (os.getenv("DUTY_CHAT_ID") if args.send else None)
    if chat_id:
        ok = send_screenshot_to_lark(str(out_path), chat_id)
        return 0 if ok else 3
    elif args.send:
        print("⚠️  --send given but no chat_id (set DUTY_CHAT_ID in .env or use --send-to).")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
