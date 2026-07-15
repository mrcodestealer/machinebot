"""
Standalone **Machine / Encoder** Lark bot.

This is a self-contained bot that ONLY does the machine + encoder flows mirrored from
``osedutybot``:

  - ``set/unset maintenance|test <machines>``  PROD batch set/unset via EGM backends (confirm card)
  - ``/sm``                                    set-machine wizard (env → action → machines)
  - ``/stresstest <paste announcement>``       one-time reminder 10 min before the set-maintenance time
  - maintenance schedule paste (@bot)          auto reminder 10 min before the action time
  - ``machine status <names>``                 read-only status from webmachine_data.json
  - ``/findmachine`` (``/fm``)                 interactive card: env + game type + online/offline
  - ``/nch /nwr /wf /tbr /tbp /cp /dhs /mdr``  asset/encoder sheet lookups (TRTC-parsed cards)
  - ``/encoder <machine(s)>``                  MAIN/POOL/CCTV IPs from OSM-Watch (latestencoder.json)
  - ``/osmwatch [url]``                        OSM-Watch dashboard screenshot (warm browser)
  - ``/loginosmwatch``                         force a fresh OSM-Watch login QR (lab group)
  - ``/wm``                                    machine dashboard (webmachine blueprint + scrape loop)

…plus the credit / log flows mirrored from ``logcreditbot`` (same Lark app):

  - ``/checkcredit <machine> [YYYY-MM-DD]``    log → latest players → NP choice card (Third Http)
  - ``/checkcreditdate``                       interactive card (machine + player + date)
  - ``/machineerror <machine> [date]``         latest two players, error context only
  - ``/checkmachinelog <machine> [date]``      logic log → card + AI summary (+ Third Http)
  - ``/stuckcredit <machine> [date]``          stuck credit: log + Third Http transfer-out check
  - ``/npthirdhttp <player_id> [date time]``   NP/WF/DHS/NCH/CP/OSM/MDR/TBP Third Http Detail
  - ``/cctv <machine>``                        EGM CCTV screenshot · ``/al [DD/MM]`` Amount Loss
  - reply **1**–**4** after an NP prompt · **Missing Credit** alert paste → checkcredit card

The heavy lifting lives in the sibling modules copied verbatim from ``osedutybot``:
  - ``smmachine.py`` / ``prod_machine_batch.py``  prod-batch set/unset engine (Playwright EGM automation)
  - ``maintenancemachineagent.py``                LLM/regex intent parsing for maintenance messages
  - ``checkcredit.py`` (+ ``np_third_http_page.py``, ``third_http_warm_pool.py``) — checkcredit engine
    (OSS/LogNavigator log read, Third Http screenshots, cards) + backend routing (``_np_resolve_backend``)
  - ``checkmachinelog.py``                        logic-log reader (optional AI summary via chatagent)
  - ``amountloss.py`` / ``chatagent.py``          FPMS Amount Loss + CHECKLOG (``/al``) / optional LLM
  - ``webmachine.py`` (+ ``webapp.py`` alias)     machine dashboard + scrape loop → webmachine_data.json
  - ``findmachine.py`` / ``machine_card.py``      find-machine form card + TRTC card rendering
  - ``osmwatch.py``                               OSM-Watch warm browser + encoder scraper (``/encoder``)
  - ``reminder.py``                               one-time maintenance reminders (Bitable sheet + APScheduler)
  - ``nch/nwr/winford/tbr/tbp/cp/dhs/mdr``        per-site asset sheet lookups

``smmachine`` lazily does ``import main`` for ``_set_prod_batch_thread_root``,
``upload_image_lark`` and ``prod_batch_send_image_message`` — all defined here; the
``sys.modules`` alias below makes ``import main`` resolve to this loaded ``__main__``
without re-executing module-level code.

Subscription mode: **Receive events through a persistent connection** (Lark long connection /
WebSocket). Set ``LARK_EVENT_MODE=websocket`` in ``.env`` (default here) and run ``python main.py``.
"""

import base64
import contextvars
import http
import json
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

import requests
from dotenv import load_dotenv

# Log lines use emoji + arrows; force UTF-8 so a redirected stdout (systemd/journald, pipes,
# Windows cp1252) never crashes the process with UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Resolve sibling-module imports regardless of process CWD (systemd, gunicorn, etc.)
_CHBOX_DIR = os.path.dirname(os.path.abspath(__file__))
if _CHBOX_DIR not in sys.path:
    sys.path.insert(0, _CHBOX_DIR)

# ``python main.py`` loads this file as ``__main__``. If any engine path does ``import main``,
# alias it to this module so it does NOT re-execute module-level code.
if __name__ == "__main__":
    sys.modules.setdefault("main", sys.modules["__main__"])

# Load .env from the project directory (works under systemd even when CWD is not the app folder).
_ENV_PATH = os.path.join(_CHBOX_DIR, ".env")
load_dotenv(_ENV_PATH)

from flask import Flask, request, jsonify, Response

# ================= CONFIGURATION =================
APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")
VERIFICATION_TOKEN = (os.getenv("VERIFICATION_TOKEN") or "").strip()

app = Flask(__name__)
app.config.setdefault(
    "SECRET_KEY",
    (os.environ.get("APP_SECRET") or "change-me").strip() or "change-me",
)

# Bot's own open_id — used to skip our own messages and to detect @mentions in group chats.
# Auto-resolved from Lark at startup when not pinned in .env (see _run_main_entry).
BOT_OPEN_ID = (os.getenv("BOT_OPEN_ID") or "").strip()


# ================= Tenant access token =================
_tenant_token_cache: dict[str, object] = {"token": None, "expires_at": 0.0}
_tenant_token_lock = threading.Lock()
_TENANT_TOKEN_REFRESH_SEC = 120  # refresh before Lark expiry (typically 7200s)


def get_tenant_access_token():
    """Return ``tenant_access_token``; cached ~2h with early refresh; stale token on transient failure."""
    now = time.time()
    with _tenant_token_lock:
        tok = _tenant_token_cache.get("token")
        exp = float(_tenant_token_cache.get("expires_at") or 0.0)
        if tok and now < exp:
            return tok

    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    headers = {"Content-Type": "application/json"}
    data = {"app_id": APP_ID, "app_secret": APP_SECRET}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=15)
        response.raise_for_status()
        body = response.json()
    except Exception as ex:
        print(f"[lark] tenant_access_token request failed: {ex!r}", flush=True)
        with _tenant_token_lock:
            stale = _tenant_token_cache.get("token")
            if stale:
                return stale
        return None

    if body.get("code") not in (0, None):
        print(f"[lark] tenant_access_token API error: {body}", flush=True)
        return None

    token = body.get("tenant_access_token")
    if not token:
        print(f"[lark] tenant_access_token missing in response: {body}", flush=True)
        return None

    try:
        expire_sec = int(body.get("expire") or 7200)
    except (TypeError, ValueError):
        expire_sec = 7200
    ttl = max(60, expire_sec - _TENANT_TOKEN_REFRESH_SEC)
    with _tenant_token_lock:
        _tenant_token_cache["token"] = token
        _tenant_token_cache["expires_at"] = time.time() + ttl
    return token


def get_bot_open_id():
    """Bot open_id via ``GET /open-apis/bot/v3/info`` (used for self-skip + @mention detection)."""
    token = get_tenant_access_token()
    if not token:
        print("❌ Failed to get bot open_id: no tenant_access_token", flush=True)
        return None
    host = (os.getenv("LARK_HOST") or "https://open.larksuite.com").rstrip("/")
    url = f"{host}/open-apis/bot/v3/info"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=15).json()
    except Exception as ex:
        print(f"❌ Failed to get bot open_id: {ex!r}", flush=True)
        return None
    if resp.get("code") == 0:
        oid = ((resp.get("bot") or {}).get("open_id") or "").strip()
        if oid:
            return oid
    print("❌ Failed to get bot open_id:", resp, flush=True)
    return None


# ================= Reactions =================
def add_message_reaction(message_id, emoji_type, *, fallbacks: tuple[str, ...] = ()):
    mid = (message_id or "").strip()
    if not mid:
        print("[lark] reaction skipped: missing message_id", flush=True)
        return None
    token = get_tenant_access_token()
    url = f"https://open.larksuite.com/open-apis/im/v1/messages/{mid}/reactions"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for code in (emoji_type, *fallbacks):
        et = (code or "").strip()
        if not et:
            continue
        payload = {"reaction_type": {"emoji_type": et}}
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        try:
            body = response.json()
        except Exception:
            body = {}
        if response.status_code == 200 and int(body.get("code", -1)) == 0:
            print(f"✅ Added {et} reaction to message {mid}", flush=True)
            return body
        print(
            f"⚠️ {et} reaction failed: status={response.status_code} body={body!r}",
            flush=True,
        )
    return None


# Lark UI tooltip may say "GotIt"; official emoji_type is **Get** (see im message-reaction emojis doc).
_GOT_IT_REACTION_FALLBACKS = ("GotIt", "GOTIT", "LGTM", "OnIt", "CheckMark")


def add_gotit_reaction(message_id):
    return add_message_reaction(message_id, "Get", fallbacks=_GOT_IT_REACTION_FALLBACKS)


_DONE_REACTION_FALLBACKS = ("Done", "CheckMark", "JIAYI")


def add_done_reaction(message_id):
    return add_message_reaction(message_id, "DONE", fallbacks=_DONE_REACTION_FALLBACKS)


# ================= Incoming-message context (quoted replies + DONE reaction) =================
_lark_user_message_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_lark_user_message_id", default=None
)
_lark_defer_done_reaction: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_lark_defer_done_reaction", default=False
)


def set_lark_incoming_message(message_id: Optional[str] = None) -> None:
    mid = (message_id or "").strip() or None
    _lark_user_message_id.set(mid)
    _lark_defer_done_reaction.set(False)


def defer_lark_done_reaction() -> None:
    """Background work will call :func:`mark_lark_process_done` when finished."""
    _lark_defer_done_reaction.set(True)


def mark_lark_process_done(message_id: Optional[str] = None) -> None:
    mid = (message_id or _lark_user_message_id.get() or "").strip()
    if mid:
        add_done_reaction(mid)


def finish_lark_incoming_message_if_sync() -> None:
    if _lark_defer_done_reaction.get():
        return
    if not (_lark_user_message_id.get() or "").strip():
        return
    mark_lark_process_done()


def lark_background_task(fn, *args, **kwargs):
    """Run ``fn`` in a thread; add **DONE** on the triggering user message when it returns."""
    defer_lark_done_reaction()
    try:
        return fn(*args, **kwargs)
    finally:
        mark_lark_process_done()


def start_lark_background_thread(fn, *args, **kwargs) -> None:
    """Spawn a daemon thread that preserves Lark incoming-message context for quoted replies."""
    ctx = contextvars.copy_context()

    def _target() -> None:
        ctx.run(lark_background_task, fn, *args, **kwargs)

    threading.Thread(target=_target, daemon=True).start()


def _lark_im_ack():
    """HTTP 200 for Lark without GotIt/Done reactions (ignored messages)."""
    return jsonify({"success": True})


def _lark_im_done():
    finish_lark_incoming_message_if_sync()
    return jsonify({"success": True})


# ================= Message send / reply / image =================
def _lark_build_message_content(text, msg_type: str = "text") -> str:
    if msg_type == "interactive":
        return text if isinstance(text, str) else json.dumps(text)
    if msg_type == "image":
        return json.dumps({"image_key": text})
    return json.dumps({"text": text})


def _lark_post_message_reply(
    parent_message_id: str,
    text,
    *,
    msg_type: str = "text",
    mentions=None,
    reply_in_thread: bool = False,
) -> dict:
    """POST ``/im/v1/messages/{message_id}/reply`` — quoted reply or thread-only reply."""
    mid = (parent_message_id or "").strip()
    if not mid:
        return {"code": -1, "msg": "no message_id"}
    token = get_tenant_access_token()
    if not token:
        print("[lark] message reply skipped: no tenant_access_token", flush=True)
        return {"code": -1, "msg": "no tenant_access_token"}
    url = f"https://open.larksuite.com/open-apis/im/v1/messages/{mid}/reply"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body: dict[str, Any] = {
        "msg_type": msg_type,
        "content": _lark_build_message_content(text, msg_type),
    }
    if reply_in_thread:
        body["reply_in_thread"] = True
    if mentions:
        body["mentions"] = mentions
    return requests.post(url, headers=headers, json=body).json()


def send_message(
    chat_id,
    text,
    msg_type="text",
    mentions=None,
    receive_id_type="chat_id",
    reply_to_message_id=None,
):
    """Send to chat, or quote-reply to ``reply_to_message_id`` (defaults to inbound user message)."""
    if reply_to_message_id is not None:
        reply_mid = (reply_to_message_id or "").strip() or None
    else:
        reply_mid = (_lark_user_message_id.get() or "").strip() or None
    if reply_mid:
        return _lark_post_message_reply(
            reply_mid, text, msg_type=msg_type, mentions=mentions, reply_in_thread=False
        )
    token = get_tenant_access_token()
    if not token:
        print("[lark] send_message skipped: no tenant_access_token", flush=True)
        return {"code": -1, "msg": "no tenant_access_token"}
    url = "https://open.larksuite.com/open-apis/im/v1/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    content = _lark_build_message_content(text, msg_type)
    body = {
        "receive_id": chat_id,
        "msg_type": msg_type,
        "content": content,
    }
    if mentions:
        body["mentions"] = mentions
    rid_type = (receive_id_type or "chat_id").strip() or "chat_id"
    params = {"receive_id_type": rid_type}
    response = requests.post(url, headers=headers, params=params, json=body)
    return response.json()


def _extract_lark_message_id(resp: Any) -> str:
    if not isinstance(resp, dict):
        return ""
    data = resp.get("data") or {}
    if not isinstance(data, dict):
        return ""
    mid = str(data.get("message_id") or "").strip()
    if mid:
        return mid
    nested = data.get("message") or {}
    if isinstance(nested, dict):
        return str(nested.get("message_id") or "").strip()
    return ""


def reply_message_in_thread(
    parent_message_id: str,
    text: str,
    msg_type: str = "text",
    mentions=None,
) -> dict:
    """Reply inside a thread only (``reply_in_thread=true`` — not main chat stream)."""
    return _lark_post_message_reply(
        parent_message_id,
        text,
        msg_type=msg_type,
        mentions=mentions,
        reply_in_thread=True,
    )


def send_file(chat_id, file_token):
    token = get_tenant_access_token()
    url = "https://open.larksuite.com/open-apis/im/v1/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": chat_id,
        "msg_type": "file",
        "content": json.dumps({"file_key": file_token}),
    }
    params = {"receive_id_type": "chat_id"}
    response = requests.post(url, headers=headers, params=params, json=payload)
    return response.json()


def upload_image_lark(image_path: str):
    """Upload PNG/JPEG for im/v1/messages msg_type=image; returns image_key or None."""
    token = get_tenant_access_token()
    url = "https://open.larksuite.com/open-apis/im/v1/images"
    headers = {"Authorization": f"Bearer {token}"}
    ext = os.path.splitext(image_path)[1].lower()
    mime, _ = mimetypes.guess_type(image_path)
    if not mime or mime not in ("image/png", "image/jpeg"):
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    with open(image_path, "rb") as f:
        files = {"image": (os.path.basename(image_path), f, mime)}
        data = {"image_type": "message"}
        resp = requests.post(url, headers=headers, files=files, data=data)
    result = resp.json()
    if result.get("code") == 0:
        return result.get("data", {}).get("image_key")
    print(f"❌ Lark image upload failed: {result}")
    return None


def send_image_message(chat_id, image_key: str):
    token = get_tenant_access_token()
    url = "https://open.larksuite.com/open-apis/im/v1/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": chat_id,
        "msg_type": "image",
        "content": json.dumps({"image_key": image_key}),
    }
    params = {"receive_id_type": "chat_id"}
    return requests.post(url, headers=headers, params=params, json=payload).json()


# ================= Prod-batch thread binding (set/unset maintenance flows) =================
# ``/nwrsetmaintenance`` etc. — thread replies under the user's command message only.
PROD_BATCH_THREAD_ROOT: dict[str, dict] = {}


def _set_prod_batch_thread_root(chat_id: str, message_id: str) -> None:
    cid = (chat_id or "").strip()
    mid = (message_id or "").strip()
    if not cid or not mid:
        return
    PROD_BATCH_THREAD_ROOT[cid] = {"message_id": mid, "ts": time.time()}


def _prod_batch_thread_root_from_incoming_message(
    message: dict, *, message_id: Optional[str] = None
) -> Optional[str]:
    """Prefer ``root_id`` when the command was sent inside an existing thread."""
    root = str((message or {}).get("root_id") or "").strip()
    if root:
        return root
    mid = (message_id or (message or {}).get("message_id") or "").strip()
    return mid or None


def _get_prod_batch_thread_root(chat_id: str, max_age_sec: float = 7200.0) -> Optional[str]:
    ent = PROD_BATCH_THREAD_ROOT.get((chat_id or "").strip())
    if not ent:
        return None
    if time.time() - ent["ts"] > max_age_sec:
        del PROD_BATCH_THREAD_ROOT[(chat_id or "").strip()]
        return None
    return str(ent.get("message_id") or "").strip() or None


def make_prod_batch_thread_send(
    chat_id: str,
    *,
    thread_root: Optional[str] = None,
    base_send=None,
):
    if base_send is None:
        base_send = send_message
    cid = (chat_id or "").strip()
    bound_root = (thread_root or "").strip() or None

    def _send(target_chat_id, text, msg_type="text", mentions=None, **kwargs):
        root = (bound_root or _get_prod_batch_thread_root(cid) or "").strip()
        if root and (target_chat_id or "").strip() == cid:
            return reply_message_in_thread(root, text, msg_type=msg_type, mentions=mentions)
        try:
            return base_send(target_chat_id, text, msg_type=msg_type, mentions=mentions, **kwargs)
        except TypeError:
            try:
                return base_send(target_chat_id, text, msg_type=msg_type)
            except TypeError:
                return base_send(target_chat_id, text)

    return _send


def prod_batch_send_image_message(chat_id: str, image_key: str) -> dict:
    """smmachine resolves this via ``import main`` to post EGM-row screenshots in-thread."""
    cid = (chat_id or "").strip()
    root = (_get_prod_batch_thread_root(cid) or "").strip()
    if root:
        return reply_message_in_thread(root, image_key, msg_type="image")
    return send_image_message(chat_id, image_key)


def _lark_http_card_callback_response(body: dict) -> Response:
    """Return card.callback body (toast and/or in-place card update) within the 3s window."""
    print(f"[lark] HTTP 200 card callback response keys={list(body.keys())!r}", flush=True)
    return Response(
        json.dumps(body, ensure_ascii=False),
        status=200,
        mimetype="application/json",
    )


# ================= /checkcredit thread binding + NP pending (per-chat) =================
# Last NP prompt result in this chat — used by `/npthirdhttp` and reply 1–4 for date + credit
# time window. Threaded replies stay under the user's /checkcredit command message.
CHECKCREDIT_NP_PENDING: dict[str, dict] = {}
CHECKCREDIT_THREAD_ROOT: dict[str, dict] = {}


def _set_checkcredit_thread_root(chat_id: str, message_id: str) -> None:
    mid = (message_id or "").strip()
    if not mid:
        return
    CHECKCREDIT_THREAD_ROOT[chat_id] = {"message_id": mid, "ts": time.time()}


def _get_checkcredit_thread_root(chat_id: str, max_age_sec: float = 3600.0) -> Optional[str]:
    ent = CHECKCREDIT_THREAD_ROOT.get(chat_id)
    if not ent:
        return None
    if time.time() - ent["ts"] > max_age_sec:
        del CHECKCREDIT_THREAD_ROOT[chat_id]
        return None
    return str(ent.get("message_id") or "").strip() or None


def _checkcredit_begin_thread(
    chat_id: str,
    parent_message_id: Optional[str] = None,
) -> Optional[str]:
    """Thread under the user's ``/checkcredit`` message (``reply_in_thread`` — not main chat)."""
    parent = (parent_message_id or "").strip() or None
    if parent:
        _set_checkcredit_thread_root(chat_id, parent)
    return parent


def _checkcredit_send(
    chat_id: str,
    text: str,
    *,
    thread_root: Optional[str] = None,
    msg_type: str = "text",
    mentions=None,
) -> dict:
    root = (thread_root or _get_checkcredit_thread_root(chat_id) or "").strip()
    if root:
        return reply_message_in_thread(root, text, msg_type=msg_type, mentions=mentions)
    return send_message(chat_id, text, msg_type=msg_type, mentions=mentions)


def _checkcredit_send_image(chat_id: str, image_key: str, *, thread_root: Optional[str] = None) -> dict:
    root = (thread_root or _get_checkcredit_thread_root(chat_id) or "").strip()
    if root:
        return reply_message_in_thread(root, image_key, msg_type="image")
    return send_image_message(chat_id, image_key)


def _set_checkcredit_np_pending(
    chat_id: str,
    payload: dict,
    thread_root: Optional[str] = None,
) -> None:
    root = (thread_root or _get_checkcredit_thread_root(chat_id) or "").strip() or None
    CHECKCREDIT_NP_PENDING[chat_id] = {"payload": payload, "ts": time.time(), "thread_root": root}
    if root:
        _set_checkcredit_thread_root(chat_id, root)


def _get_checkcredit_np_pending(chat_id: str, max_age_sec: float = 3600.0):
    ent = CHECKCREDIT_NP_PENDING.get(chat_id)
    if not ent:
        return None
    if time.time() - ent["ts"] > max_age_sec:
        del CHECKCREDIT_NP_PENDING[chat_id]
        return None
    return ent["payload"]



# ================= Machine lookup card helpers (/nch /nwr /wf /tbr /tbp /cp /dhs /mdr) =================
def _send_machine_lookup_card(chat_id: str, text: str, *, title: str) -> None:
    """Send a machine-lookup result as a TRTC-parsed Lark card; fall back to raw text
    when the card cannot be built or the interactive send is rejected."""
    card = None
    try:
        import machine_card

        card = machine_card.build_card_from_text(text, title=title)
    except Exception as ex:
        print(f"[machine-card] build failed: {ex!r}", flush=True)
    if isinstance(card, dict):
        try:
            resp = send_message(chat_id, json.dumps(card, ensure_ascii=False), msg_type="interactive")
            if isinstance(resp, dict) and resp.get("code") in (0, None):
                return
            print(f"[machine-card] interactive rejected: {resp!r}", flush=True)
        except Exception as ex:
            print(f"[machine-card] send failed: {ex!r}", flush=True)
    send_message(chat_id, text)


def _machine_query_after_prefix(clean_text: str, prefix: str) -> str:
    """Text after a machine command prefix, accepting both '/nwr 2005' and '/nwr2005'."""
    return re.sub(r"(?i)^" + re.escape(prefix), "", clean_text.strip(), count=1).strip()


# ================= Incoming message text extraction =================
def _lark_flatten_rich_content(obj) -> str:
    """Collect plain text from Lark post / rich ``content`` JSON."""
    parts: list[str] = []
    if isinstance(obj, str):
        s = obj.strip()
        if s:
            parts.append(s)
    elif isinstance(obj, dict):
        if str(obj.get("tag") or "").lower() == "text":
            t = obj.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t.strip())
        else:
            for key in ("text", "title", "content"):
                if key in obj:
                    sub = _lark_flatten_rich_content(obj[key])
                    if sub:
                        parts.append(sub)
    elif isinstance(obj, list):
        for item in obj:
            sub = _lark_flatten_rich_content(item)
            if sub:
                parts.append(sub)
    return " ".join(parts)


def _lark_extract_message_text(content_str: str) -> str:
    """Parse ``im.message`` ``content`` JSON — text, post, and rich variants."""
    raw = (content_str or "").strip()
    if not raw:
        return ""
    try:
        content = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(content, dict):
        return str(content)
    plain = content.get("text")
    if isinstance(plain, str) and plain.strip():
        return plain.strip()
    for locale in ("zh_cn", "en_us", "ja_jp", "zh_hk", "en", "zh"):
        block = content.get(locale)
        if isinstance(block, dict):
            flat = _lark_flatten_rich_content(block.get("content"))
            if flat.strip():
                return flat.strip()
    flat_all = _lark_flatten_rich_content(content)
    return flat_all.strip()


def _lark_full_message_body(
    original_text: str, clean_text: str, message_content_raw: str
) -> str:
    """Best-effort full user text (multi-line post / Missing Credit blocks), not one-liner."""
    for candidate in (original_text, clean_text):
        c = (candidate or "").replace("\r\n", "\n").strip()
        if not c:
            continue
        low = c.casefold()
        if "missing credit" in low or "withdrawal" in low or "account:" in low:
            return c
        if len(c.splitlines()) >= 2:
            return c
    flat = _lark_extract_message_text(message_content_raw or "")
    if flat.strip():
        return flat.strip()
    return (clean_text or original_text or "").strip()


# ================= WebSocket redelivery dedup + stale-event filter =================
processed_messages = set()
processed_lock = threading.Lock()
_MAX_PROCESSED_MESSAGE_IDS = 50_000
_PROCESSED_PRUNE_CHUNK = 10_000
# Lark WebSocket may redeliver recent events after reconnect; in-memory dedup is cleared on restart.
_BOT_STARTED_AT_MS = int(time.time() * 1000)


def _lark_event_create_time_ms(data: dict) -> Optional[int]:
    """Best-effort event/message timestamp (ms) from Lark schema 2.0 or legacy callback."""
    if not isinstance(data, dict):
        return None
    hdr = data.get("header")
    if isinstance(hdr, dict):
        ct = hdr.get("create_time")
        if ct is not None:
            try:
                return int(ct)
            except (TypeError, ValueError):
                pass
    ev = data.get("event")
    if isinstance(ev, dict):
        msg = ev.get("message")
        if isinstance(msg, dict):
            ct = msg.get("create_time")
            if ct is not None:
                try:
                    return int(ct)
                except (TypeError, ValueError):
                    pass
        ct = ev.get("create_time")
        if ct is not None:
            try:
                return int(ct)
            except (TypeError, ValueError):
                pass
    return None


def _lark_skip_stale_event_on_startup(data: dict) -> bool:
    """Ignore events that happened before this process started (replay on WS reconnect)."""
    skip = (os.getenv("LARK_SKIP_STALE_ON_STARTUP") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    if not skip:
        return False
    try:
        grace_ms = int(os.getenv("LARK_STARTUP_STALE_GRACE_MS", "10000"))
    except ValueError:
        grace_ms = 10_000
    created_ms = _lark_event_create_time_ms(data)
    if created_ms is None:
        return False
    return created_ms < _BOT_STARTED_AT_MS - grace_ms


def _remember_processed_message_id(message_id: str) -> bool:
    """Record ``message_id``; return True if it was already seen (duplicate)."""
    if not message_id:
        return False
    with processed_lock:
        if message_id in processed_messages:
            return True
        if len(processed_messages) >= _MAX_PROCESSED_MESSAGE_IDS:
            for _ in range(_PROCESSED_PRUNE_CHUNK):
                try:
                    processed_messages.pop()
                except KeyError:
                    break
        processed_messages.add(message_id)
        return False


# ================= Webhook payload parsing / verification / card-callback helpers =================
def _feishu_decrypt_encrypt_field(ciphertext_b64: str, encrypt_key: str) -> str:
    """Decrypt Feishu ``encrypt`` field (AES-256-CBC + PKCS7); only when console Encrypt Key is on."""
    import hashlib

    try:
        from Crypto.Cipher import AES
    except ImportError as e:
        raise ImportError("pip install pycryptodome") from e

    bs = AES.block_size
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    enc = base64.b64decode(ciphertext_b64)
    iv = enc[:bs]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    raw = cipher.decrypt(enc[bs:])
    pad_len = raw[-1]
    if pad_len < 1 or pad_len > bs:
        raise ValueError("invalid PKCS7 padding")
    raw = raw[:-pad_len]
    return raw.decode("utf-8")


def _feishu_maybe_decrypt_webhook_payload(raw):
    """Decrypt ``{"encrypt": "..."}`` bodies when ``LARK_ENCRYPT_KEY`` is set; else pass through."""
    if not isinstance(raw, dict) or "encrypt" not in raw:
        return raw
    ek = (
        os.getenv("LARK_ENCRYPT_KEY")
        or os.getenv("ENCRYPT_KEY")
        or os.getenv("FEISHU_ENCRYPT_KEY")
        or ""
    ).strip()
    if not ek:
        print(
            "[lark] POST body has `encrypt` but LARK_ENCRYPT_KEY is unset — "
            "set it to match 事件与回调 → Encrypt Key, or turn off encryption there.",
            flush=True,
        )
        return raw
    try:
        plain = _feishu_decrypt_encrypt_field(str(raw["encrypt"]), ek)
        plain = plain.lstrip("﻿")
        return json.loads(plain)
    except ImportError as ex:
        print(f"[lark] {ex} — encrypted webhooks disabled until installed.", flush=True)
        return raw
    except Exception as ex:
        print(f"[lark] decrypt webhook failed: {ex!r}", flush=True)
        return raw


def _lark_is_schema_v2(data):
    if not isinstance(data, dict):
        return False
    s = data.get("schema")
    return s == "2.0" or str(s).strip() == "2.0"


def _lark_looks_like_lark_card_update_credential(token_str):
    s = (token_str or "").strip()
    if not s:
        return False
    return s.startswith("c-") or s.startswith("d-")


def _lark_extract_verification_token(data):
    """App **Verification Token**: schema 2.0 uses ``header.token``; some payloads use ``verification_token``."""
    if not isinstance(data, dict):
        return None
    h = data.get("header")
    if isinstance(h, dict):
        for key in ("token", "Token", "verification_token"):
            t = h.get(key)
            if t is not None:
                return str(t).strip()
    vt = data.get("verification_token")
    if vt is not None:
        return str(vt).strip()
    t2 = data.get("token")
    if t2 is None:
        return None
    ts = str(t2).strip()
    if _lark_looks_like_lark_card_update_credential(ts):
        return None
    return ts


def _lark_is_legacy_card_trigger_v1_flat(data):
    """Earlier flat ``card.action.trigger_v1`` body (no ``schema`` / ``event`` envelope)."""
    if not isinstance(data, dict):
        return False
    if data.get("encrypt") is not None:
        return False
    het = _lark_header_event_type(data)
    if het.startswith("card.action"):
        return False
    if isinstance(data.get("header"), dict) and data["header"].get("event_type"):
        return False
    if not isinstance(data.get("action"), dict):
        return False
    return bool(data.get("open_message_id") or data.get("open_id"))


def _lark_normalize_legacy_card_trigger_v1_flat(data):
    """Map flat ``trigger_v1`` body into schema-2 ``event`` + ``header.event_type`` shape."""
    if not isinstance(data, dict) or not _lark_is_legacy_card_trigger_v1_flat(data):
        return data
    ev = {"operator": {}, "action": data.get("action"), "context": {}}
    oid = data.get("open_id")
    if oid:
        ev["operator"]["open_id"] = str(oid).strip()
    uid = data.get("union_id")
    if uid:
        ev["operator"]["union_id"] = str(uid).strip()
    ocid = data.get("open_chat_id") or data.get("chat_id")
    if ocid:
        ev["open_chat_id"] = str(ocid).strip()
        ev["context"]["open_chat_id"] = str(ocid).strip()
    omid = data.get("open_message_id")
    if omid:
        ev["context"]["open_message_id"] = str(omid).strip()
    data["event"] = ev
    hdr = data.get("header") if isinstance(data.get("header"), dict) else {}
    hdr["event_type"] = "card.action.trigger_v1"
    hdr["event_id"] = hdr.get("event_id") or str(omid or "")[:80]
    data["header"] = hdr
    data["schema"] = "2.0"
    return data


def _lark_http_card_callback_ok():
    """Feishu ``card.action.trigger``: HTTP **200** + JSON ``{}`` within ~3s (or toast if enabled)."""
    print("[lark] HTTP 200 card ACK (instant)", flush=True)
    if (os.getenv("LARK_CARD_ACK_TOAST") or "").strip() == "1":
        body = json.dumps(
            {"toast": {"type": "success", "content": "OK", "i18n": {"en_us": "OK", "zh_cn": "OK"}}},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return Response(body, status=200, mimetype="application/json")
    return Response(b"{}", status=200, mimetype="application/json")


def _lark_parse_card_action_value(val):
    """Decode ``event.action.value`` (object or JSON string)."""
    if val is None:
        return None
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            o = json.loads(s)
            return o if isinstance(o, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _lark_form_field_text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v).strip()
    if isinstance(v, list):
        parts = []
        for x in v:
            t = _lark_form_field_text(x)
            if t:
                parts.append(t)
        return " ".join(parts).strip()
    if isinstance(v, dict):
        if "hour" in v and "minute" in v:
            try:
                hh = int(v.get("hour"))
                mm = int(v.get("minute"))
                if 0 <= hh <= 23 and 0 <= mm <= 59:
                    return f"{hh:02d}:{mm:02d}"
            except Exception:
                pass
        for k in ("value", "text", "content", "date", "time", "datetime"):
            t = _lark_form_field_text(v.get(k))
            if t:
                return t
        for vv in v.values():
            t = _lark_form_field_text(vv)
            if t:
                return t
    return ""


def _lark_get_card_form_field(action_obj, name):
    if not isinstance(action_obj, dict):
        return ""
    fv = action_obj.get("form_value")
    if not isinstance(fv, dict):
        return ""
    return _lark_form_field_text(fv.get(name))


def _lark_find_field_deep(obj, name):
    if isinstance(obj, dict):
        if name in obj:
            t = _lark_form_field_text(obj.get(name))
            if t:
                return t
        for vv in obj.values():
            t = _lark_find_field_deep(vv, name)
            if t:
                return t
    elif isinstance(obj, list):
        for it in obj:
            t = _lark_find_field_deep(it, name)
            if t:
                return t
    return ""


def _lark_safe_parse_json_body(req):
    """Prefer ``get_json``; fallback to raw body (some proxies strip / alter Content-Type)."""
    raw = req.get_json(silent=True)
    if isinstance(raw, dict):
        return raw
    b = req.get_data(cache=False)
    if not b:
        return None
    if b.startswith(b"\xef\xbb\xbf"):
        b = b[3:]
    try:
        parsed = json.loads(b.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _lark_coerce_event_dict(data):
    """Some gateways deliver ``event`` as a JSON string — normalize to a dict."""
    if not isinstance(data, dict):
        return data
    ev = data.get("event")
    if isinstance(ev, str):
        try:
            parsed = json.loads(ev)
            data["event"] = parsed if isinstance(parsed, dict) else {}
        except Exception:
            data["event"] = {}
    elif ev is None and isinstance(data, dict):
        het = _lark_header_event_type(data)
        if het.startswith("card.action"):
            data["event"] = {}
        elif _lark_is_schema_v2(data) and isinstance(data.get("action"), dict):
            data["event"] = {}
    return data


def _lark_should_merge_flat_card_callback(data):
    """True when payload is (or looks like) ``card.action.trigger`` including SDK-flat shapes."""
    if not isinstance(data, dict):
        return False
    et = _lark_header_event_type(data)
    if et.startswith("card.action"):
        return True
    if _lark_is_schema_v2(data) and isinstance(data.get("action"), dict):
        return True
    return False


def _lark_normalize_card_callback_envelope(data):
    """Merge flattened ``card.action.trigger`` fields into ``event`` when proxies strip nesting."""
    if not isinstance(data, dict):
        return data
    if not _lark_should_merge_flat_card_callback(data):
        return data
    ev = data.get("event")
    if not isinstance(ev, dict):
        ev = {}
    for k in (
        "action",
        "operator",
        "open_chat_id",
        "chat_id",
        "context",
        "host",
        "delivery_type",
        "token",
    ):
        if k in data and data[k] is not None and k not in ev:
            ev[k] = data[k]
    ctx = ev.get("context")
    if not isinstance(ctx, dict):
        ctx = {}
        ev["context"] = ctx
    if isinstance(data.get("open_chat_id"), str) and data["open_chat_id"].strip() and not ctx.get(
        "open_chat_id"
    ):
        ctx["open_chat_id"] = data["open_chat_id"].strip()
    if isinstance(data.get("open_message_id"), str) and data["open_message_id"].strip() and not ctx.get(
        "open_message_id"
    ):
        ctx["open_message_id"] = data["open_message_id"].strip()
    top_uid = data.get("open_id") or data.get("user_id")
    top_union = data.get("union_id")
    op = ev.get("operator")
    if top_uid or top_union:
        if not isinstance(op, dict):
            ev["operator"] = {}
            op = ev["operator"]
        if isinstance(op, dict):
            op = dict(op)
            if top_uid and not op.get("open_id"):
                op["open_id"] = top_uid
            if top_union and not op.get("union_id"):
                op["union_id"] = top_union
            ev["operator"] = op
    ctx_merge = ev.get("context") if isinstance(ev.get("context"), dict) else {}
    if not ev.get("open_chat_id") and ctx_merge.get("open_chat_id"):
        ev["open_chat_id"] = ctx_merge["open_chat_id"]
    data["event"] = ev
    return data


def _lark_extract_card_event_fields(ev):
    """Resolve chat / sender / button ``value`` from ``event`` for ``card.action.trigger`` payloads."""
    ctx = ev.get("context") if isinstance(ev.get("context"), dict) else {}
    act = ev.get("action") or {}
    val = act.get("value")
    chat_id = ev.get("open_chat_id") or ev.get("chat_id")
    if not chat_id:
        chat_id = ctx.get("open_chat_id") or ctx.get("chat_id")
    op = ev.get("operator") or {}
    sender_id = op.get("open_id")
    if not sender_id:
        sender_id = op.get("union_id")
    if not sender_id:
        sender_id = ev.get("open_id") or ev.get("user_id") or op.get("user_id")
    return chat_id, sender_id, val


def _lark_event_body_looks_like_card_interaction(ev):
    """When ``header.event_type`` is missing or wrong, still recognize card callbacks by shape."""
    if not isinstance(ev, dict):
        return False
    act = ev.get("action")
    if not isinstance(act, dict):
        return False
    if ev.get("message"):
        return False
    if act.get("tag") == "button":
        return True
    if act.get("name") and act.get("value") is not None:
        return bool(ev.get("operator") or ev.get("context"))
    if act.get("value") is not None and (ev.get("operator") or ev.get("context")):
        return True
    return bool(ev.get("operator") or ev.get("context"))


def _lark_resolve_card_action(data):
    """Returns ``(chat_id, sender_id, value, event_id)`` for card button callbacks, or ``None``."""
    if not isinstance(data, dict):
        return None
    hdr = data.get("header") if isinstance(data.get("header"), dict) else {}
    et = _lark_header_event_type(data)
    eid = hdr.get("event_id") if isinstance(hdr, dict) else None
    if eid is None:
        eid = data.get("event_id")
    ev = data.get("event") if isinstance(data.get("event"), dict) else {}

    named = et in ("card.action.trigger", "card.action.trigger_v1")
    heuristic = et != "im.message.receive_v1" and (
        (_lark_is_schema_v2(data) and _lark_event_body_looks_like_card_interaction(ev))
        or (
            isinstance(ev.get("action"), dict)
            and len(ev.get("action") or {}) > 0
            and (ev.get("operator") or ev.get("context"))
        )
    )
    ctx0 = ev.get("context") if isinstance(ev.get("context"), dict) else {}
    legacy_shape = (
        et != "im.message.receive_v1"
        and isinstance(ev.get("action"), dict)
        and len(ev.get("action") or {}) > 0
        and (ev.get("operator") or ev.get("context"))
        and bool(
            ev.get("open_chat_id")
            or ev.get("chat_id")
            or ctx0.get("open_chat_id")
            or ctx0.get("chat_id")
        )
    )
    if not (named or heuristic or legacy_shape):
        return None
    chat_id, sender_id, val = _lark_extract_card_event_fields(ev)
    return (chat_id, sender_id, val, eid)


def _lark_payload_has_card_action(data):
    """True when ``event.action`` **or** SDK-flat top-level ``action`` is present."""
    if not isinstance(data, dict):
        return False
    ev = data.get("event")
    if isinstance(ev, dict):
        act = ev.get("action")
        if isinstance(act, dict) and len(act) > 0:
            return True
    act_top = data.get("action")
    return isinstance(act_top, dict) and len(act_top) > 0


def _lark_header_event_type(data):
    """``header.event_type``, or rare top-level ``event_type`` (some gateway proxies strip nested keys)."""
    if isinstance(data, dict):
        h = data.get("header")
        if isinstance(h, dict):
            et = h.get("event_type")
            if et is not None:
                return str(et).strip()
        et2 = data.get("event_type")
        if et2 is not None:
            return str(et2).strip()
    return ""


def _lark_ack_only_event_type(het: str) -> bool:
    """Subscribed in console but not implemented here — still HTTP 200 (avoid log spam)."""
    if not het:
        return False
    return het.lower().startswith("meeting_room.")


# ================= Scheduler + maintenance-reminder targets (schedule / stresstest flows) =================
import atexit

from apscheduler.schedulers.background import BackgroundScheduler

# One in-process scheduler powers the one-time maintenance / stress-test reminders that
# maintenancemachineagent registers via ``reminder.add_sheet_reminder(...)``. Started in
# ``_run_main_entry``.
scheduler = BackgroundScheduler()

# Lark open_id @mentioned on scheduled maintenance reminders (same default as osedutybot).
TARGET_USER_OPEN_ID = (
    os.getenv("omduty", "").strip()
    or os.getenv("OMDUTY", "").strip()
    or "ou_d7bc33724e2d6ced4050c944c2ca5650"
)
# Maintenance / stress-test reminders are always delivered to this group.
REMINDER_TARGET_CHAT_ID = os.getenv(
    "REMINDER_TARGET_CHAT_ID",
    "oc_9de3d63fc589df6feeb9b0bee9c45b72",
).strip() or "oc_9de3d63fc589df6feeb9b0bee9c45b72"


# ================= Check Credit / Log engine dispatch (mirrored from osedutybot) =================
AMOUNT_LOSS_MAX_ATTEMPTS = 2
AMOUNT_LOSS_RETRY_NOTICE = "Error occurred... Auto retry Please wait..."


def run_amountloss_check(chat_id, date_str=None, *, scheduled_9am=False):
    """在后台线程中执行 amount loss 检查，并将结果发送到指定 chat_id（失败自动重跑一轮）"""
    try:
        from amountloss import amount_loss_9am_enabled, fetch_fpms_data
    except ImportError as e:
        send_message(
            chat_id,
            "❌ 无法加载 FPMS 抓取模块（fetch_fpms_data）。"
            f" 请把与开发环境一致的 amountloss.py 部署到服务器，并安装 playwright。\n{str(e)}",
        )
        return

    if scheduled_9am and not amount_loss_9am_enabled():
        print(
            "[Amount Loss] 9:00 display/sheet fill skipped (temporarily disabled; AMOUNT_LOSS_9AM_ENABLED=1 to restore)",
            flush=True,
        )
        return

    for attempt in range(1, AMOUNT_LOSS_MAX_ATTEMPTS + 1):
        try:
            result = fetch_fpms_data(
                headless=True,
                target_date_str=date_str,
                filterdata=True,
                checklog=True,
                scheduled_9am=scheduled_9am,
            )
            if isinstance(result, dict) and result.get("lark_card"):
                sync_note = str(result.get("sync_note") or "").strip()
                if sync_note:
                    send_message(chat_id, sync_note)
                card_json = json.dumps(result["lark_card"])
                resp = send_message(chat_id, card_json, msg_type="interactive")
                if resp.get("code") != 0:
                    send_message(chat_id, result.get("text") or str(result))
                tsv_all = (result.get("sheet_tsv_all") or "").strip()
                tsv_game = (result.get("sheet_tsv_game") or "").strip()
                if tsv_all:
                    send_message(
                        chat_id,
                        "📋 Copy for Sheet — python3 amountloss.py --getdata\n```text\n" + tsv_all + "\n```",
                    )
                if tsv_game:
                    send_message(
                        chat_id,
                        "📋 Copy for Sheet — By Game\n```text\n" + tsv_game + "\n```",
                    )
            else:
                send_message(chat_id, result if isinstance(result, str) else str(result))
            return
        except Exception as e:
            if attempt < AMOUNT_LOSS_MAX_ATTEMPTS:
                send_message(chat_id, AMOUNT_LOSS_RETRY_NOTICE)
                print(f"[Amount Loss] attempt {attempt} failed: {e!r}, auto-retrying...")
            else:
                send_message(chat_id, f"❌ Amount Loss 检查失败: {str(e)}")
                print(f"[Amount Loss] failed after {AMOUNT_LOSS_MAX_ATTEMPTS} attempts: {e!r}")


def _prewarm_third_http_for_machine(machine_query: str) -> None:
    """R1: start the Third-Http warm browser login for this machine's backend NOW, so it
    overlaps phase A (the OSS log read) instead of paying launch+login later on the critical
    path. Non-blocking (queues a prewarm task on the per-tag worker); safe no-op when the pool
    is disabled, credentials are missing, or the browser is already warm (login fast-paths)."""
    try:
        from third_http_warm_pool import third_http_warm_pool, third_http_warm_pool_enabled

        if not third_http_warm_pool_enabled():
            return
        import checkcredit as _cc

        tag = _cc._np_log_backend_tag(str(machine_query).strip())
        if not tag or not _cc._np_backend_has_credentials(tag):
            return
        third_http_warm_pool().prewarm([tag])
        print(
            f"[third-http-warm] prewarm {tag} submitted (overlaps log read for {machine_query!r})",
            flush=True,
        )
    except Exception as ex:
        print(f"[third-http-warm] prewarm skip for {machine_query!r}: {ex!r}", flush=True)


def _third_http_warm_enabled_for_bot() -> bool:
    try:
        from third_http_warm_pool import third_http_warm_pool_enabled

        return bool(third_http_warm_pool_enabled())
    except Exception:
        return False


def run_checkcredit_finderror(
    chat_id,
    machine_query: str,
    date_str: str,
    mode: str = "default",
    navigator_logic_log_basename: Optional[str] = None,
    thread_root_message_id: Optional[str] = None,
):
    """Background: same as checkcredit + `--date`. Uses OSS HTTP by default (see checkcredit_use_oss_source)."""
    thread_root = (thread_root_message_id or _get_checkcredit_thread_root(chat_id) or "").strip() or None
    if thread_root:
        _set_checkcredit_thread_root(chat_id, thread_root)

    # R1: warm the Third-Http browser login for this machine's backend now, so it overlaps
    # the log read below — the later Detail screenshot (checkcredit/machineerror) then reuses
    # the ready page instead of launching+logging-in on the critical path.
    _prewarm_third_http_for_machine(machine_query)

    def _cc_send(text, **kwargs):
        return _checkcredit_send(chat_id, text, thread_root=thread_root, **kwargs)

    try:
        import checkcredit
    except ImportError as e:
        _cc_send(f"❌ Cannot load checkcredit module: {e}")
        return
    try:
        td = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        use_oss = checkcredit.checkcredit_use_oss_source()
        # ``base`` is the LogNavigator host — only used when source="navigator". checkcredit no
        # longer defines a module-level DEFAULT_BASE, so fall back to CHECKCREDIT_BASE (empty is
        # fine for the default OSS source, which ignores ``base``).
        _cc_base = getattr(checkcredit, "DEFAULT_BASE", None) or os.getenv("CHECKCREDIT_BASE", "")
        out = checkcredit.run_finderror(
            str(machine_query).strip(),
            target_date=td,
            timeout_ms=max(15_000, 90_000),
            base=_cc_base,
            user=checkcredit.DEFAULT_USER,
            pw=checkcredit.DEFAULT_PASS,
            source="oss" if use_oss else "navigator",
            navigator_logic_log_basename=navigator_logic_log_basename,
        )
        text = (out.get("text") or "").strip()
        np = out.get("np_followup")
        preview_img_path = None
        preview_img_key = ""
        preview_img_err = ""
        preview_img_attempted = False
        error_ctx_paths: list[str] = []
        machineerror_fb: list[str] = []
        if isinstance(np, dict):
            try:
                md = str(np.get("machine_display") or "").strip() or None
                ms = str(np.get("machine_match_substr") or "").strip() or None
                cap = getattr(checkcredit, "screenshot_egm_status_window", None)
                if callable(cap) and md:
                    preview_img_attempted = True
                    preview_img_path = cap(
                        machine_display=md,
                        machine_substr=ms,
                        timeout_ms=120_000,
                        headed=False,
                    )
                    preview_img_key = upload_image_lark(preview_img_path) or ""
                    if not preview_img_key:
                        preview_img_err = "upload image failed"
                        print("[checkcredit] EGM preview screenshot upload failed", flush=True)
                if callable(getattr(checkcredit, "build_np_choice_lark_card", None)):
                    np_choices = np.get("np_choices") or []
                    intro_line = ""
                    extra_md = ""
                    extra_error_images: list[dict[str, str]] = []
                    if str(mode or "").strip().lower() == "error_only":
                        np_choices = np.get("np_choices_error_only") or []
                        intro_line = "Found players error"
                        extra_md = str(np.get("machineerror_context_md") or "")
                        merged_rows = out.get("merged_players") or []
                        pick_err = getattr(checkcredit, "select_top2_error_players", None)
                        build_ctx = getattr(checkcredit, "build_error_context_screenshots", None)
                        fb_ctx = getattr(checkcredit, "format_error_context_text_fallback", None)
                        if callable(pick_err) and callable(build_ctx):
                            err_rows = pick_err(merged_rows) or []
                            for rr in err_rows[:2]:
                                if not (rr.get("errors") or []):
                                    continue
                                ctx_items = build_ctx(rr, max_errors=6, lines_before_after=4) or []
                                row_got_img = False
                                for ci in ctx_items:
                                    pth = str(ci.get("path") or "").strip()
                                    if not pth:
                                        continue
                                    error_ctx_paths.append(pth)
                                    ik = upload_image_lark(pth) or ""
                                    if not ik:
                                        try:
                                            sz = os.path.getsize(pth)
                                        except OSError:
                                            sz = -1
                                        print(
                                            f"[checkcredit] error-context upload failed: path={pth} size={sz}",
                                            flush=True,
                                        )
                                        continue
                                    row_got_img = True
                                    extra_error_images.append(
                                        {
                                            "img_key": ik,
                                            "title": str(ci.get("title") or "Error context screenshot"),
                                        }
                                    )
                                if not row_got_img and callable(fb_ctx):
                                    chunk = fb_ctx(rr, max_errors=6)
                                    if chunk:
                                        machineerror_fb.append(chunk)
                    else:
                        intro_line = str(np.get("np_choice_intro") or "").strip()
                    same_last_line = ""
                    if str(mode or "").strip().lower() != "error_only":
                        same_last_line = str(np.get("same_last_line") or "")
                    np["np_choices"] = np_choices
                    out["lark_card_candidates"] = checkcredit.build_np_choice_lark_card(
                        np_choices,
                        target_date_iso=str(np.get("target_date") or ""),
                        machine_display=str(np.get("machine_display") or ""),
                        third_http_backend=str(np.get("third_http_backend") or "NP"),
                        image_key=preview_img_key,
                        intro_line=intro_line,
                        same_last_line=same_last_line,
                        extra_md=extra_md,
                        extra_error_images=extra_error_images,
                        navigator_same_day_multi_log=bool(np.get("navigator_same_day_multi_log")),
                    )
            except Exception as e:
                preview_img_err = str(e)
                print(f"[checkcredit] EGM preview screenshot failed: {e!r}", flush=True)
            finally:
                if preview_img_path and os.path.isfile(preview_img_path):
                    try:
                        os.unlink(preview_img_path)
                    except OSError:
                        pass
                for pth in error_ctx_paths:
                    if pth and os.path.isfile(pth):
                        try:
                            os.unlink(pth)
                        except OSError:
                            pass
            if preview_img_attempted and not preview_img_key:
                msg = (
                    f"⚠️ EGM preview screenshot unavailable: {preview_img_err}"
                    if preview_img_err
                    else "⚠️ EGM preview screenshot unavailable."
                )
                _cc_send(msg)
        card = out.get("lark_card_candidates")
        if isinstance(card, dict):
            card_json = json.dumps(card)
            resp = _cc_send(card_json, msg_type="interactive")
            if resp.get("code") != 0:
                _cc_send(text if text else "(no output)")
            if machineerror_fb and str(mode or "").strip().lower() == "error_only":
                _cc_send(
                    "⚠️ Error log images unavailable (PNG render or Lark upload failed). Text context:\n\n"
                    + "\n\n".join(machineerror_fb),
                )
        else:
            _cc_send(text if text else "(no output)")

        if isinstance(np, dict):
            _set_checkcredit_np_pending(chat_id, np, thread_root=thread_root)
    except Exception as e:
        cmd = "machineerror" if str(mode or "").strip().lower() == "error_only" else "checkcredit"
        _cc_send(f"❌ {cmd} failed: {e}")
        print(f"[{cmd}] error: {e!r}")


def run_check_machine_log_job(
    chat_id: str,
    machine_query: str,
    date_str: str,
    thread_root_message_id: Optional[str] = None,
    *,
    stuck_credit: bool = False,
) -> None:
    """OSS/LogNavigator logic log → threaded Lark card + AI summary (+ Third Http when applicable)."""
    thread_root = (thread_root_message_id or _get_checkcredit_thread_root(chat_id) or "").strip() or None
    if thread_root:
        _set_checkcredit_thread_root(chat_id, thread_root)

    # R1: kick the Third-Http browser login off concurrently with the log read below, so a
    # cold/slept browser finishes authenticating while OSS fetch runs — the screenshot step
    # then reuses the ready page instead of launching+logging-in on the critical path.
    _prewarm_third_http_for_machine(machine_query)

    def _cml_send(text, **kwargs):
        return _checkcredit_send(chat_id, text, thread_root=thread_root, **kwargs)

    try:
        import checkmachinelog
    except ImportError as e:
        _cml_send(f"❌ Cannot load checkmachinelog module: {e}")
        return
    try:
        td = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
        out = checkmachinelog.run_check_machine_log(
            str(machine_query).strip(),
            target_date=td,
            stuck_credit=stuck_credit,
        )
        card = out.get("lark_card")
        if isinstance(card, dict):
            resp = _cml_send(json.dumps(card, ensure_ascii=False), msg_type="interactive")
            if resp.get("code") != 0:
                text = (out.get("text") or "").strip()
                if text:
                    _cml_send(text)
                else:
                    _cml_send(f"❌ {'stuck credit' if stuck_credit else 'checkmachinelog'} card failed: {resp}")
        else:
            text = (out.get("text") or "").strip()
            if text:
                _cml_send(text)
            else:
                _cml_send(f"✅ {'stuck credit' if stuck_credit else 'checkmachinelog'} finished (no output).")

        pick = out.get("third_http_followup")
        if isinstance(pick, dict) and (pick.get("user_id") or "").strip():
            be = str(pick.get("third_http_backend") or "NP").strip().upper()
            uid = str(pick["user_id"]).strip()
            cr = pick.get("credit_value")
            cr_s = str(cr) if cr is not None else "n/a"
            ts = str(pick.get("time_short") or "").strip()
            md = str(pick.get("machine_display") or machine_query).strip()
            if stuck_credit:
                _cml_send(
                    f"📋 **Stuck credit** on `{md}` — last player **`{uid}`** (credit `{cr_s}` @ `{ts}`).\n"
                    f"**Checking Third Http ({be})** — did the player **transfer out credit**?\n\n"
                    f"卡机额度 — 正在查 **Third Http ({be})** 玩家 **`{uid}`** 是否已成功转出…"
                )
                success_caption = (
                    f"✅ **Third Http ({be})** — player `{uid}` **transferred out credit** successfully "
                    f"(Detail matches log amount `{cr_s}` @ `{ts}`).\n"
                    f"✅ Third Http 有匹配记录 — 玩家 **`{uid}`** 额度应已成功转出（卡机可清）。"
                )
            else:
                err_p = str(pick.get("error_player_id") or "").strip()
                kind = str(pick.get("verify_kind") or "").strip()
                if kind == "transfer_out" and err_p and err_p != uid:
                    _cml_send(
                        f"📋 Log **error** player `{err_p}` · card **transfer-out** "
                        f"`{cr_s}` @ `{ts}` → player **`{uid}`**.\n"
                        f"**Checking Third Http ({be})** for **`{uid}`** (not error player).\n\n"
                        f"日志 error 玩家 `{err_p}` ≠ 转出玩家 **`{uid}`** — 查 Third Http 转出记录…"
                    )
                    success_caption = (
                        f"✅ **Third Http ({be})** — player **`{uid}`** **transferred out credit** "
                        f"(Detail `{cr_s}` @ `{ts}`, machine `{md}`).\n"
                        f"✅ Third Http — 玩家 **`{uid}`** 转出成功（非 error 玩家 `{err_p}`）。"
                    )
                else:
                    _cml_send(
                        f"📋 Log shows an **error** for player `{uid}` (credit `{cr_s}` @ `{ts}`).\n"
                        f"**Now checking Third Http ({be})** — did the player **transfer out credit** successfully?\n\n"
                        f"日志有 error — 正在查 **Third Http ({be})** 是否已成功转出额度…"
                    )
                    success_caption = (
                        f"✅ **Third Http ({be})** — player `{uid}` **transferred out credit** successfully "
                        f"(Detail matches log amount `{cr_s}` @ `{ts}`).\n"
                        f"✅ Third Http 有匹配记录 — 玩家 **`{uid}`** 额度应已成功转出。"
                    )
            _np_run_screenshot_worker(
                chat_id,
                uid,
                str(pick.get("target_date_iso") or date_str).strip(),
                ts,
                machine_substr=pick.get("machine_match_substr"),
                expected_credit=cr if isinstance(cr, (int, float)) else None,
                machine_display=str(pick.get("machine_display") or "").strip() or None,
                thread_root=thread_root,
                success_caption=success_caption,
                time_short_candidates=pick.get("time_short_candidates"),
            )
    except Exception as e:
        label = "stuck credit" if stuck_credit else "checkmachinelog"
        _cml_send(f"❌ {label} failed: {e}")
        print(f"[{label}] error: {e!r}")


def run_checkcredit_navigator_next_log(chat_id: str) -> None:
    """Open the next same-day logic log (card **check another logs**) — OSS or LogNavigator."""
    pend = _get_checkcredit_np_pending(chat_id)
    files = (pend or {}).get("navigator_logic_log_files") or []
    opened = str((pend or {}).get("navigator_opened_logic_log_basename") or "").strip()
    if not pend or len(files) < 2:
        _checkcredit_send(
            chat_id,
            "❌ No alternate logic logs in context — run `/checkcredit …` again.",
        )
        return
    try:
        idx = files.index(opened) if opened in files else 0
    except ValueError:
        idx = 0
    next_idx = (idx + 1) % len(files)
    next_fn = str(files[next_idx] or "").strip()
    if not next_fn:
        _checkcredit_send(chat_id, "❌ Could not resolve next log filename.")
        return
    mq = str((pend.get("machine_display") or "")).strip()
    date_iso = str((pend.get("target_date") or "")).strip()
    if not mq or not date_iso:
        _checkcredit_send(chat_id, "❌ Pending machine/date missing — run `/checkcreditdate …` again.")
        return
    thread_root = _get_checkcredit_thread_root(chat_id)
    _checkcredit_send(chat_id, f"⏳ Opening next logic log `{next_fn}` …", thread_root=thread_root)
    run_checkcredit_finderror(
        chat_id,
        mq,
        date_iso,
        mode="default",
        navigator_logic_log_basename=next_fn,
        thread_root_message_id=thread_root,
    )


def run_checkcredit_player_job(chat_id: str, machine: str, player_id: str, date_iso: str) -> None:
    """OSS log → player credit row → Third Http Detail screenshot (same path as ``/npthirdhttp``)."""
    try:
        import checkcredit
        from datetime import datetime as _dt

        td = _dt.strptime(date_iso.strip(), "%Y-%m-%d").date()
    except ValueError:
        _checkcredit_send(chat_id, "❌ Invalid date — use YYYY-MM-DD.")
        return
    except ImportError as e:
        _checkcredit_send(chat_id, f"❌ Cannot load checkcredit: {e}")
        return
    md, lc, err = checkcredit.resolve_player_log_credit_snapshot(
        machine.strip(), player_id.strip(), td
    )
    if err:
        _checkcredit_send(chat_id, f"❌ {err}")
        return
    assert lc is not None
    ts = str(lc.get("time_short") or "").strip()
    if not ts:
        _checkcredit_send(chat_id, "❌ No credit time in log for this player.")
        return
    exp: Optional[float] = None
    v = lc.get("value")
    if v is not None:
        try:
            exp = float(v)
        except (TypeError, ValueError):
            exp = None
    display_md = (md or "").strip() or None
    ms = checkcredit.machine_match_substr_from_display((md or "").strip()) or None
    _np_run_screenshot_worker(
        chat_id,
        player_id.strip(),
        date_iso.strip(),
        ts,
        machine_substr=ms,
        expected_credit=exp,
        machine_display=display_md,
    )


def run_cctv_screenshot_job(chat_id: str, machine_query: str) -> None:
    """EGM Status: click **CCTV**, screenshot dialog only (no credit / log checks)."""
    try:
        import checkcredit
    except ImportError as e:
        send_message(chat_id, f"❌ Cannot load checkcredit module: {e}")
        return
    cap = getattr(checkcredit, "screenshot_egm_cctv_window", None)
    resolve_route = getattr(checkcredit, "resolve_machine_display_for_egm_route", None)
    if not callable(cap):
        send_message(
            chat_id,
            "❌ `checkcredit.screenshot_egm_cctv_window` missing — deploy the latest `checkcredit.py`.",
        )
        return
    mq = (machine_query or "").strip()
    if not mq:
        send_message(
            chat_id,
            "❌ Usage: `/cctv <machine>` — same machine label as checkcredit (e.g. `OSMCP181`, `Dragons-0181`).",
        )
        return
    md_resolved = mq
    ms_resolved: Optional[str] = None
    if callable(resolve_route):
        send_message(chat_id, "⏳ LogNavigator / OSS — resolving machine → correct backend (NCH, CP, …)…")
        try:
            md_resolved, ms_resolved = resolve_route(mq, timeout_ms=120_000)
        except Exception as e:
            send_message(chat_id, f"❌ Could not resolve machine / environment: {e}")
            return
        tag_fn = getattr(checkcredit, "_np_log_backend_tag", lambda _: "?")
        send_message(
            chat_id,
            f"→ **{md_resolved}** · backend **{tag_fn(md_resolved)}** — EGM **CCTV**…",
        )
    else:
        send_message(chat_id, "⏳ EGM **CCTV** — login → click **CCTV** → screenshot…")
    path = None
    try:
        path = cap(
            machine_display=md_resolved,
            machine_substr=(ms_resolved or "").strip() or None,
            timeout_ms=120_000,
            headed=False,
        )
        key = upload_image_lark(path)
        if not key:
            send_message(chat_id, "❌ CCTV screenshot upload failed.")
            return
        r = send_image_message(chat_id, key)
        if r.get("code") != 0:
            send_message(chat_id, f"❌ Failed to send image: {r}")
    except Exception as e:
        send_message(chat_id, f"❌ CCTV screenshot failed: {e}")
        print(f"[cctv] error: {e!r}", flush=True)
    finally:
        if path and os.path.isfile(path):
            try:
                os.unlink(path)
            except OSError:
                pass


def _np_run_screenshot_worker(
    chat_id: str,
    uid: str,
    date_iso: str,
    time_short: str,
    *,
    machine_substr: Optional[str] = None,
    expected_credit: Optional[float] = None,
    machine_display: Optional[str] = None,
    thread_root: Optional[str] = None,
    success_caption: Optional[str] = None,
    time_short_candidates: Optional[list[str]] = None,
) -> None:
    """NP / WF / DHS / NCH / CP / OSM / MDR / TBP Log Third Http → `recharge` Detail screenshot. Always **headless** on server."""
    root = (thread_root or _get_checkcredit_thread_root(chat_id) or "").strip() or None

    def _np_send(text, **kwargs):
        return _checkcredit_send(chat_id, text, thread_root=root, **kwargs)

    try:
        import checkcredit

        screenshot_np_recharge_detail = checkcredit.screenshot_np_recharge_detail
    except ImportError as e:
        _np_send(f"❌ Cannot load checkcredit module: {e}")
        return
    except AttributeError:
        _np_send(
            "❌ checkcredit.screenshot_np_recharge_detail missing — deploy the latest `checkcredit.py`.",
        )
        return
    backend_tag = getattr(checkcredit, "_np_log_backend_tag", lambda _: "NP")(
        (machine_display or "").strip() or None
    )
    _np_send(
        f"⏳ {backend_tag} backend (Playwright): Log Third Http → recharge Detail"
        f"{' (warm browser)' if _third_http_warm_enabled_for_bot() else ''}…",
    )
    path = None
    try:
        path = screenshot_np_recharge_detail(
            uid,
            date_iso,
            time_short,
            timeout_ms=120_000,
            machine_substr=machine_substr,
            expected_credit=expected_credit,
            machine_display=machine_display,
            headed=False,
            time_short_candidates=time_short_candidates,
        )
        key = upload_image_lark(path)
        if not key:
            _np_send("❌ Failed to upload screenshot to Lark.")
            return
        if (success_caption or "").strip():
            _np_send(success_caption.strip())
        if root:
            r = reply_message_in_thread(root, key, msg_type="image")
        else:
            r = send_image_message(chat_id, key)
        if r.get("code") != 0:
            _np_send(f"❌ Failed to send image: {r}")
    except Exception as e:
        err_s = str(e)
        if "No Log Third Http rows" in err_s or "empty table after Search" in err_s:
            tip = (
                "\n💡 **No Third Http rows** for this UserId/time window — transfer likely **did not complete** "
                f"(log error player `{uid}` @ `{time_short}`). "
                "Widen `NP_BACKEND_WINDOW_MINUTES` if the log time is near the window edge."
                "\n💡 Third Http **无记录** — 转出可能**未成功**（日志 error 玩家与时间见上）。"
                " 可调大 `NP_BACKEND_WINDOW_MINUTES`。"
            )
        elif "did not load after Search" in err_s or "tbody stayed hidden" in err_s:
            tip = (
                "\n💡 Search results never became ready (empty/hidden table or slow UI). "
                "Retry, or run locally: `python3 checkcredit.py --checkuser ... --pause`."
                "\n💡 搜索结果未就绪（空表/隐藏 tbody 或页面慢），请重试或用 `--checkuser --pause` 本地查看。"
            )
        elif "No " in err_s and " Detail on pages" in err_s:
            tip = (
                "\n💡 Rows exist but **no matching recharge Detail** — bot already retries **machineId-only** "
                "when log `amount` ≠ Detail `amount`. Try `NP_BACKEND_MAX_PAGES` / `NP_BACKEND_WINDOW_MINUTES`. "
                "Disable machine-only pass: `NP_THIRD_HTTP_NO_MACHINE_ONLY_FALLBACK=1`."
                "\n💡 有 recharge 行但 Detail 不匹配 — 已自动尝试仅匹配机台；可调页数/时间窗。"
            )
        elif "No usable temporary directory" in err_s or "writable temporary directory" in err_s:
            tip = (
                "\n💡 **Server has no writable `/tmp`** — screenshot never started (not an NP search miss). "
                "On the bot host: `mkdir -p /root/machinebot/.tmp && chmod 700 /root/machinebot/.tmp`, "
                "add `TMPDIR=/root/machinebot/.tmp` to `.env`, restart the bot, then retry stuck credit."
                "\n💡 服务器 **没有可写临时目录**，截图步骤未执行（不是 Third Http 搜不到）。"
                " 在主机创建 `.tmp` 并设置 `TMPDIR`，重启后再试。"
            )
        else:
            tip = (
                "\n💡 This screenshot runs **headless**. Try raising `NP_BACKEND_MAX_PAGES` / "
                "`NP_BACKEND_WINDOW_MINUTES`, or widen `NP_BACKEND_AMOUNT_EPS` (default `0.05`) in `.env`. "
                "For **TBP**, try `TBP_THIRD_HTTP_AMOUNT_SCALE` (e.g. `100` for cents) or "
                "`TBP_THIRD_HTTP_NO_MACHINE_ONLY_FALLBACK=1` to disable the extra machine-only pass. "
                "For a **visible** Chromium window, run locally: `python3 checkcredit.py --checkuser ... --pause`."
            )
        print(
            "[npthirdhttp] screenshot context "
            f"uid={uid!r} date={date_iso!r} time={time_short!r} "
            f"machine_substr={machine_substr!r} credit={expected_credit!r} "
            f"machine_display={machine_display!r}",
            flush=True,
        )
        _np_send(f"❌ {backend_tag} third-http screenshot failed: {e}{tip}")
        print(f"[npthirdhttp] error: {e!r}")
    finally:
        if path and os.path.isfile(path):
            try:
                os.unlink(path)
            except OSError:
                pass


def run_np_third_http_by_choice(chat_id: str, choice_idx: int) -> None:
    """choice_idx: 1–4 matching `np_choices` from last `/checkcreditdate` NP prompt."""
    pend = _get_checkcredit_np_pending(chat_id)
    choices = (pend or {}).get("np_choices") or []
    if not pend or choice_idx < 1 or choice_idx > len(choices):
        _checkcredit_send(
            chat_id,
            "❌ No active NP choice list — run `/checkcreditdate …` again, then reply **1**–**4**.",
        )
        return
    ch = choices[choice_idx - 1]
    uid = str(ch.get("user_id") or "").strip()
    date_iso = (pend.get("target_date") or "").strip()
    time_short = (ch.get("time_short") or "").strip()
    if not uid or not date_iso or not time_short:
        _checkcredit_send(chat_id, "❌ Pending NP choice is incomplete — use `/npthirdhttp …` with full date/time.")
        return
    ms = (pend.get("machine_match_substr") or "").strip() or None
    exp = ch.get("credit_value")
    if exp is not None:
        try:
            exp = float(exp)
        except (TypeError, ValueError):
            exp = None
    if exp is None and ch.get("credit") not in (None, "", "n/a"):
        try:
            exp = float(str(ch.get("credit")).strip())
        except ValueError:
            exp = None
    md = (pend.get("machine_display") or "").strip() or None
    # If EGM small window currently shows the same member as selected player,
    # short-circuit and prompt that the player has not left machine yet.
    if md:
        try:
            import checkcredit

            get_member = getattr(checkcredit, "get_egm_member_user_id", None)
            if callable(get_member):
                cur_member = str(
                    get_member(
                        machine_display=md,
                        machine_substr=ms,
                        timeout_ms=120_000,
                        headed=False,
                    )
                    or ""
                ).strip()
                if cur_member and cur_member == uid:
                    _checkcredit_send(chat_id, "Player haven't out the machine")
                    return
        except Exception as e:
            print(f"[npthirdhttp] EGM member pre-check skipped: {e!r}", flush=True)
    _np_run_screenshot_worker(
        chat_id,
        uid,
        date_iso,
        time_short,
        machine_substr=ms,
        expected_credit=exp,
        machine_display=md,
    )


def run_np_third_http_job(chat_id: str, argv: list[str]):
    """Background: NP Log Third Http Req → first `recharge` row → Detail dialog screenshot."""
    try:
        import checkcredit

        _ = checkcredit.screenshot_np_recharge_detail
    except ImportError as e:
        _checkcredit_send(chat_id, f"❌ Cannot load checkcredit module: {e}")
        return
    except AttributeError:
        _checkcredit_send(
            chat_id,
            "❌ checkcredit.screenshot_np_recharge_detail missing — deploy the latest `checkcredit.py`.",
        )
        return
    if not argv:
        _checkcredit_send(
            chat_id,
            "❌ Usage: `/npthirdhttp <player_id>` — or `/npthirdhttp <player_id> YYYY-MM-DD HH:MM:SS.mmm`",
        )
        return
    uid = argv[0].strip()
    date_iso: Optional[str] = None
    time_short: Optional[str] = None
    pend = None
    if len(argv) == 2:
        _checkcredit_send(
            chat_id,
            "❌ Use `/npthirdhttp <player_id>` after checkcredit, "
            "or full `/npthirdhttp <player_id> YYYY-MM-DD HH:MM:SS.mmm` (three parts).",
        )
        return
    if len(argv) >= 3:
        date_iso = argv[1].strip()
        time_short = argv[2].strip()
        try:
            datetime.strptime(date_iso, "%Y-%m-%d")
        except ValueError:
            _checkcredit_send(chat_id, "❌ Date must be `YYYY-MM-DD`.")
            return
        if not time_short:
            _checkcredit_send(chat_id, "❌ Missing time (HH:MM:SS or HH:MM:SS.mmm).")
            return
    else:
        pend = _get_checkcredit_np_pending(chat_id)
        if not pend:
            _checkcredit_send(
                chat_id,
                "❌ No pending `/checkcreditdate` context in this chat. "
                "Run checkcredit first, or use `/npthirdhttp <player_id> YYYY-MM-DD HH:MM:SS.mmm`.",
            )
            return
        date_iso = pend["target_date"]
        time_short = ""
        for ch in pend.get("np_choices") or []:
            if str(ch.get("user_id")) == str(uid):
                time_short = (ch.get("time_short") or "").strip()
                break
        if not time_short:
            for p in pend.get("latest_two_players", []):
                if str(p.get("user_id")) == str(uid):
                    time_short = (p.get("time_short") or "").strip()
                    break
        if not time_short:
            _checkcredit_send(
                chat_id,
                f"❌ User ID `{uid}` not in the last checkcredit NP list (choices 1–4). "
                f"Use: `/npthirdhttp {uid} YYYY-MM-DD HH:MM:SS.mmm`",
            )
            return

    assert date_iso is not None and time_short is not None
    ms = None
    exp = None
    md: Optional[str] = None
    if pend:
        md = (pend.get("machine_display") or "").strip() or None
        ms = (pend.get("machine_match_substr") or "").strip() or None
        for ch in pend.get("np_choices") or []:
            if str(ch.get("user_id")) == str(uid):
                exp = ch.get("credit_value")
                if exp is not None:
                    try:
                        exp = float(exp)
                    except (TypeError, ValueError):
                        exp = None
                if exp is None and ch.get("credit") not in (None, "", "n/a"):
                    try:
                        exp = float(str(ch.get("credit")).strip())
                    except ValueError:
                        exp = None
                break
    _np_run_screenshot_worker(
        chat_id,
        uid,
        date_iso,
        time_short,
        machine_substr=ms,
        expected_credit=exp,
        machine_display=md,
    )


def _parse_missing_credit_alert(text: str) -> Optional[dict]:
    raw = (text or "").replace("\r\n", "\n")
    if not re.search(r"(?i)(?:type\s*:\s*)?missing\s+credit", raw):
        return None
    out: dict[str, str] = {}
    m = re.search(r"(?im)^\s*account\s*:\s*(\d+)", raw)
    if m:
        out["account"] = m.group(1)
    m = re.search(r"(?im)^\s*amount\s+missing\s*:\s*([\d.]+)", raw)
    if m:
        out["amount"] = m.group(1)
    m = re.search(
        r"(?im)withdrawal\s+time\s*:\s*(\d{4})[/-](\d{2})[/-](\d{2})(?:\s+\d{2}:\d{2}:\d{2})?",
        raw,
    )
    if m:
        out["date_iso"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(?im)proposal\s+withdrawal\s*:\s*(\S+)", raw)
    if m:
        out["proposal"] = m.group(1)
    return out or None


def _try_missing_credit_inquiry(
    chat_id: str,
    body: str,
    *,
    bot_mentioned: bool,
    message_id: Optional[str],
    send_func,
) -> bool:
    if not bot_mentioned:
        return False
    parsed = _parse_missing_credit_alert(body)
    if not parsed:
        return False
    lines = [
        "📋 **Missing Credit alert parsed**",
        f"• Account: `{parsed.get('account', '?')}`",
        f"• Amount missing: `{parsed.get('amount', '?')}`",
        f"• Withdrawal date: `{parsed.get('date_iso', '?')}`",
    ]
    if parsed.get("proposal"):
        lines.append(f"• Proposal: `{parsed['proposal']}`")
    lines.append(
        "\n⏳ I need the **machine type** (e.g. `NWR2074`) to scan logs. "
        "Fill the form below — player/date are pre-filled when possible."
    )
    send_func(chat_id, "\n".join(lines))
    try:
        import checkcredit

        card = checkcredit.build_checkcredit_player_form_card()
        send_func(chat_id, json.dumps(card, ensure_ascii=False), msg_type="interactive")
    except Exception as ex:
        acct = parsed.get("account") or ""
        dt = parsed.get("date_iso") or ""
        send_func(
            chat_id,
            f"Use `/checkcreditdate <machine>` with player `{acct}` date `{dt}`.",
        )
        print(f"[missing-credit] card failed: {ex!r}", flush=True)
    print(f"[missing-credit] parsed {parsed!r}", flush=True)
    return True



# ================= Self-deploy: git pull origin main + restart the systemd service =================
MACHINEBOT_SERVICE = (os.getenv("MACHINEBOT_SERVICE") or "machine").strip() or "machine"
_DEPLOY_ALLOWED_OPEN_IDS = {
    x.strip() for x in (os.getenv("DEPLOY_ALLOWED_OPEN_IDS") or "").split(",") if x.strip()
}


def _deploy_allowed(sender_open_id: Optional[str]) -> bool:
    """Empty allowlist = anyone who can address the bot may deploy; otherwise restrict to it."""
    if not _DEPLOY_ALLOWED_OPEN_IDS:
        return True
    return (sender_open_id or "").strip() in _DEPLOY_ALLOWED_OPEN_IDS


def _looks_like_deploy_command(text: str) -> bool:
    """Match ``git pull origin main and restart service`` / ``/deploy`` / ``/gitpullrestart``."""
    t = (text or "").strip().casefold()
    if not t:
        return False
    if t in ("/deploy", "/gitpullrestart") or t.startswith("/deploy ") or t.startswith(
        "/gitpullrestart "
    ):
        return True
    has_pull = bool(re.search(r"\bgit\s+pull\b", t)) or bool(
        re.search(r"\bpull\s+(?:origin|code|repo|latest)\b", t)
    )
    has_restart = bool(re.search(r"\b(?:restart|reboot)\b", t)) or "重启" in t
    if has_pull and has_restart:
        return True
    return bool(re.search(r"拉代码.*重启|部署.*重启", t))


def _schedule_service_restart(delay_sec: float = 2.0) -> None:
    """Restart the systemd unit from a DETACHED process so it survives this process exiting."""
    import subprocess

    try:
        subprocess.Popen(
            ["bash", "-c", f"sleep {delay_sec}; systemctl restart {MACHINEBOT_SERVICE}"],
            start_new_session=True,
        )
        print(
            f"[deploy] scheduled: systemctl restart {MACHINEBOT_SERVICE} (in {delay_sec}s)",
            flush=True,
        )
    except Exception as exc:
        print(f"[deploy] restart schedule failed: {exc!r}", flush=True)


def _run_git_pull_and_restart(chat_id: str) -> None:
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=_CHBOX_DIR,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception as exc:
        send_message(chat_id, f"❌ `git pull origin main` failed: {exc!r}")
        return
    out = "\n".join(x for x in (proc.stdout, proc.stderr) if x).strip()
    tail = out[-1200:] if len(out) > 1200 else out
    if proc.returncode != 0:
        send_message(
            chat_id,
            f"❌ `git pull origin main` failed (exit {proc.returncode}).\n```\n{tail or '(no output)'}\n```",
        )
        return
    send_message(
        chat_id,
        f"✅ `git pull origin main` OK — restarting `{MACHINEBOT_SERVICE}`…\n"
        f"```\n{tail or 'Already up to date.'}\n```",
    )
    _schedule_service_restart()


def _handle_deploy_command(chat_id: str) -> None:
    send_message(chat_id, f"⏳ `git pull origin main` + restart `{MACHINEBOT_SERVICE}`…")
    defer_lark_done_reaction()  # background thread marks DONE when the pull finishes
    start_lark_background_thread(_run_git_pull_and_restart, chat_id)


# ================= Card-callback worker (prod-batch buttons / findmachine form / reminders) =================
def _run_card_callback_worker(data: dict, resolved: tuple) -> None:
    chat_id_ca, sender_id_ca, val_ca, event_id_ca = resolved
    ev_ca = data.get("event") if isinstance(data.get("event"), dict) else {}
    op_ca = ev_ca.get("operator") if isinstance(ev_ca.get("operator"), dict) else {}
    parsed_ca = _lark_parse_card_action_value(val_ca)
    hdr_et = _lark_header_event_type(data)
    try:
        if event_id_ca and _remember_processed_message_id(str(event_id_ca)):
            print(f"⏭️ Duplicate card callback {event_id_ca} ignored ({hdr_et!r})", flush=True)
            return
        if not chat_id_ca:
            print(f"⚠️ card action skipped: no chat_id event_type={hdr_et!r}", flush=True)
            return
        if not isinstance(parsed_ca, dict):
            print(
                f"⚠️ card action ignored (no value) chat_id={chat_id_ca!r} event_type={hdr_et!r}",
                flush=True,
            )
            return

        # Buttons carry the thread root in ``r`` so replies stay under the user's command message.
        thread_r = str(parsed_ca.get("r") or "").strip()
        if thread_r:
            _set_prod_batch_thread_root(chat_id_ca, thread_r)

        key_ca = str(parsed_ca.get("k") or "").strip().lower()

        # Maintenance / stress-test reminder card: **done** confirm button.
        import reminder as _reminder_mod

        if key_ca == _reminder_mod.MAINT_REMINDER_CONFIRM_KEY:
            rid_m = str(parsed_ca.get("id") or "").strip()
            at_id_m = (
                (op_ca.get("open_id") or "").strip()
                or (sender_id_ca or "").strip()
                or (op_ca.get("union_id") or "").strip()
            )
            at_prefix = f'<at user_id="{at_id_m}"></at> ' if at_id_m else ""
            send_message(
                chat_id_ca,
                f"{at_prefix}✅ Confirmed: maintenance & test have been set"
                + (f" (reminder ID `{rid_m}`)." if rid_m else "."),
            )
            return

        # Reminder card: **delete** button.
        if key_ca == "rem_del":
            rid = str(parsed_ca.get("id") or "").strip()
            if not rid:
                send_message(chat_id_ca, "❌ Reminder delete failed: missing ID.")
                return
            try:
                result = _reminder_mod.delete_sheet_reminders(
                    ids=[rid],
                    get_token_func=get_tenant_access_token,
                    scheduler=scheduler,
                    send_func=send_message,
                    chat_id=chat_id_ca,
                    target_user_id=TARGET_USER_OPEN_ID,
                    schedule_chat_id=REMINDER_TARGET_CHAT_ID,
                )
                send_message(chat_id_ca, result)
            except Exception as e:
                send_message(chat_id_ca, f"❌ Reminder delete failed: {e}")
            return

        # Reply "check another logs" — open the next same-day logic log.
        if isinstance(parsed_ca, dict) and str(parsed_ca.get("k") or "").strip().lower() == "np_check_alt_logs":
            threading.Thread(
                target=run_checkcredit_navigator_next_log,
                args=(chat_id_ca,),
                daemon=True,
            ).start()
            return

        # NP choice button (1–4) — Third Http Detail screenshot for the picked player.
        if isinstance(parsed_ca, dict) and str(parsed_ca.get("k") or "").strip().lower() == "np_pick":
            try:
                idx_np = int(parsed_ca.get("i"))
            except (TypeError, ValueError):
                return
            pend_np = _get_checkcredit_np_pending(chat_id_ca)
            choices_np = (pend_np or {}).get("np_choices") or []
            if pend_np and 1 <= idx_np <= len(choices_np):
                threading.Thread(
                    target=run_np_third_http_by_choice,
                    args=(chat_id_ca, idx_np),
                    daemon=True,
                ).start()
            return

        # /checkcreditdate form submit — machine + player + date → Third Http Detail.
        if (
            isinstance(parsed_ca, dict)
            and str(parsed_ca.get("k") or "").strip().lower() == "checkcredit_player_submit"
        ):
            act_ca = ev_ca.get("action") if isinstance(ev_ca.get("action"), dict) else {}
            machine_raw = _lark_get_card_form_field(act_ca, "machine_type")
            player_raw = _lark_get_card_form_field(act_ca, "player_id")
            date_raw = _lark_get_card_form_field(act_ca, "log_date")
            fv_cb = parsed_ca.get("form_value")
            if isinstance(fv_cb, dict):
                machine_raw = machine_raw or _lark_form_field_text(fv_cb.get("machine_type"))
                player_raw = player_raw or _lark_form_field_text(fv_cb.get("player_id"))
                date_raw = date_raw or _lark_form_field_text(fv_cb.get("log_date"))
            machine_raw = machine_raw or _lark_form_field_text(parsed_ca.get("machine_type"))
            player_raw = player_raw or _lark_form_field_text(parsed_ca.get("player_id"))
            date_raw = date_raw or _lark_form_field_text(parsed_ca.get("log_date"))
            machine_raw = machine_raw or _lark_find_field_deep(ev_ca, "machine_type")
            player_raw = player_raw or _lark_find_field_deep(ev_ca, "player_id")
            date_raw = date_raw or _lark_find_field_deep(ev_ca, "log_date")

            def _normalize_checkcredit_date_iso(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
                s = str(raw or "").strip()
                if not s:
                    return None, "Date is empty."
                if re.match(r"^\d{10,13}$", s):
                    try:
                        ts = int(s)
                        if ts > 10**12:
                            ts = ts // 1000
                        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d"), None
                    except Exception:
                        return None, "Invalid date timestamp."
                m = re.match(r"^\s*(\d{4})-(\d{2})-(\d{2})", s)
                if m:
                    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}", None
                m2 = re.match(r"^\s*(\d{4})/(\d{2})/(\d{2})", s)
                if m2:
                    return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}", None
                return None, "Date must be YYYY-MM-DD (or use the date picker)."

            date_iso_cb, derr = _normalize_checkcredit_date_iso(date_raw)
            if derr:
                send_message(chat_id_ca, f"❌ {derr}")
                return
            machine_cb = str(machine_raw or "").strip()
            player_cb = str(player_raw or "").strip()
            if not machine_cb or not player_cb:
                send_message(chat_id_ca, "❌ Please fill Machine type and Player ID.")
                return
            assert date_iso_cb is not None
            threading.Thread(
                target=run_checkcredit_player_job,
                args=(chat_id_ca, machine_cb, player_cb, date_iso_cb),
                daemon=True,
            ).start()
            return

        # Prod-batch confirm / cancel / job-cancel / sm wizard buttons (smmachine owns the keys).
        import smmachine as _sm_cb

        if _sm_cb.handle_prod_batch_card_callback(
            parsed_ca,
            chat_id=chat_id_ca,
            send_message=make_prod_batch_thread_send(chat_id_ca),
            action_obj=(ev_ca.get("action") if isinstance(ev_ca.get("action"), dict) else None),
        ):
            return

        # /findmachine form submit — environment + game type + online/offline.
        if key_ca == "findmachine_submit":
            act_ca = ev_ca.get("action") if isinstance(ev_ca.get("action"), dict) else {}
            fm_env_raw = _lark_get_card_form_field(act_ca, "fm_env")
            fm_game_raw = _lark_get_card_form_field(act_ca, "fm_game")
            fm_online_raw = _lark_get_card_form_field(act_ca, "fm_online")
            fv_fm = parsed_ca.get("form_value")
            if isinstance(fv_fm, dict):
                fm_env_raw = fm_env_raw or _lark_form_field_text(fv_fm.get("fm_env"))
                fm_game_raw = fm_game_raw or _lark_form_field_text(fv_fm.get("fm_game"))
                fm_online_raw = fm_online_raw or _lark_form_field_text(fv_fm.get("fm_online"))
            fm_env_raw = fm_env_raw or _lark_find_field_deep(ev_ca, "fm_env")
            fm_game_raw = fm_game_raw or _lark_find_field_deep(ev_ca, "fm_game")
            fm_online_raw = fm_online_raw or _lark_find_field_deep(ev_ca, "fm_online")

            def _run_findmachine_job():
                try:
                    import findmachine as _fm_mod

                    for _fm_msg in _fm_mod.run_findmachine_query(
                        fm_env_raw, fm_game_raw, fm_online_raw
                    ):
                        send_message(chat_id_ca, _fm_msg)
                except Exception as _fm_err:
                    print(f"❌ findmachine job: {_fm_err!r}", flush=True)
                    try:
                        send_message(chat_id_ca, f"❌ findmachine failed: {_fm_err}")
                    except Exception:
                        pass

            threading.Thread(target=_run_findmachine_job, daemon=True).start()
            return

        print(
            f"⚠️ card action ignored (unrecognized value) chat_id={chat_id_ca!r} "
            f"event_type={hdr_et!r} value={parsed_ca!r}",
            flush=True,
        )
    except Exception as ex:
        print(f"❌ card callback worker: {ex!r}", flush=True)
        try:
            send_message(chat_id_ca, f"❌ Card action failed: {ex}")
        except Exception:
            pass


# ================= Message handler (machine / encoder dispatch) =================
_HELP_TEXT = (
    "🛠 I'm the **Machine / Encoder** bot. Commands:\n"
    "• `set maintenance NWR2008` / `unset test TBP8609 …` — PROD batch set/unset (confirm card)\n"
    "• `/sm` — set-machine wizard (env → action → machines)\n"
    "• `/stresstest <paste announcement>` — one-time reminder 10 min before the set time\n"
    "• paste a maintenance schedule (with @bot) — auto reminder 10 min before\n"
    "• `machine status NWR2008` — read-only status from the live scrape\n"
    "• `/findmachine` (`/fm`) — interactive card: env + game type + online/offline\n"
    "• `/nch /nwr /wf /tbr /tbp /cp /dhs /mdr <id(s)>` — asset / encoder sheet lookup\n"
    "• `/encoder <machine(s)>` — MAIN/POOL/CCTV IPs from OSM-Watch (`/encoder refresh` to rescrape)\n"
    "• `/osmwatch [url]` — OSM-Watch dashboard screenshot\n"
    "• `/loginosmwatch` — force a fresh OSM-Watch login QR (lab group)\n"
    "• `/checkcredit <machine> [YYYY-MM-DD]` — log → players → NP choice card\n"
    "• `/checkcreditdate` — interactive card (machine + player + date)\n"
    "• `/machineerror <machine> [date]` — latest two players, error context\n"
    "• `/checkmachinelog <machine> [date]` — logic log card + AI summary\n"
    "• `/stuckcredit <machine> [date]` — stuck credit + Third Http transfer-out check\n"
    "• `/npthirdhttp <player_id> [YYYY-MM-DD HH:MM:SS.mmm]` — Third Http Detail\n"
    "• `/cctv <machine>` — EGM CCTV screenshot · `/al [DD/MM]` — Amount Loss\n"
    "• reply **1**–**4** after an NP prompt · paste a **Missing Credit** alert to auto-fill\n"
    "• `/deploy` — git pull origin main + restart the systemd service"
)

# (prefix, module, function, card title, usage text) — mirrored from osedutybot's ladder.
_MACHINE_LOOKUPS = (
    ("/nch", "nch", "get_nch_info", "NCH machine",
     "❌ Usage: `/nch <asset_id(s)>`\nExamples: `/nch 1900`, `/nch1900`, `/nch nch2839 nch2378`, `/nch nch2839,nch2378`"),
    ("/nwr", "nwr", "get_nwr_info", "NWR machine",
     "❌ Usage: `/nwr <nwr_number(s)>`\nExamples: `/nwr 2005`, `/nwr2005`, `/nwr 2005,2006`, `/nwr nwr2005 nwr2006`"),
    ("/wf", "winford", "get_winford_info", "Winford asset",
     "❌ Usage: `/wf <asset_id(s)>`\nExamples: `/wf 8092`, `/wf8092`, `/wf 8092,8093`, `/wf win8092 win8093`"),
    ("/tbr", "tbr", "get_tbr_info", "TBR machine",
     "❌ Usage: `/tbr <machine_id(s)>`\nExamples: `/tbr 2099`, `/tbr2099`, `/tbr tbr2099 tbr2100`, `/tbr 2099,2100`"),
    ("/tbp", "tbp", "get_tbp_info", "TBP machine",
     "❌ Usage: `/tbp <machine_id(s)>`\nExamples: `/tbp 1234`, `/tbp1234`, `/tbp tbp1234 tbp5678`, `/tbp 1234,5678`"),
    ("/cp", "cp", "get_cp_info", "CP asset",
     "❌ Usage: `/cp <asset_number(s)>`\nExamples: `/cp 1234`, `/cp1234`, `/cp cp2839 cp2378`, `/cp cp2839,cp2378`"),
    ("/dhs", "dhs", "get_dhs_info", "DHS asset",
     "❌ Usage: `/dhs <asset_id(s)>`\nExamples: `/dhs 1234`, `/dhs1234`, `/dhs dhs1234 dhs5678`, `/dhs 1234,5678`"),
    ("/mdr", "mdr", "get_mdr_info", "MDR asset",
     "❌ Usage: `/mdr <asset_id(s)>`\nExamples: `/mdr 1234`, `/mdr1234`, `/mdr mdr1234 mdr5678`, `/mdr 1234,5678`"),
)


def _handle_machine_message(
    chat_id,
    sender_id,
    message_id,
    chat_type,
    bot_mentioned,
    is_np_reply,
    original_text,
    clean_text,
    clean_text_multiline,
    mention_keys,
    incoming_message_obj,
    message_content_raw,
) -> None:
    set_lark_incoming_message(message_id)
    if message_id and (chat_type == "p2p" or bot_mentioned or is_np_reply):
        add_gotit_reaction(message_id)

    # Cheap imports (stdlib + dotenv at import time; Playwright loads lazily inside).
    import maintenancemachineagent
    import smmachine

    ct = clean_text.strip()
    low = ct.lower()
    cmd_parts = ct.split()
    cmd = cmd_parts[0].lower() if cmd_parts else ""

    def _thread_root_for_prod_batch() -> Optional[str]:
        root = _prod_batch_thread_root_from_incoming_message(
            incoming_message_obj if isinstance(incoming_message_obj, dict) else {},
            message_id=message_id,
        )
        if root:
            _set_prod_batch_thread_root(chat_id, root)
        return root

    def _finish_agent_branch(handled: bool, reply, send_func) -> None:
        """Send the handler's reply; when a matched message was NOT handled, fall back to help
        (osedutybot fell through to its AI chat there — this bot has no chat fallback)."""
        if reply:
            send_func(chat_id, reply)
        elif not handled and (chat_type == "p2p" or bot_mentioned):
            send_func(chat_id, _HELP_TEXT)

    # ---- Reply 1–4 after an NP prompt (group works without @mention while a list is pending) ----
    if is_np_reply:
        idx_np = int(ct)
        start_lark_background_thread(run_np_third_http_by_choice, chat_id, idx_np)
        return


    # ---- OSM-Watch: force a fresh login QR (posted to the lab group) ----
    if cmd == "/loginosmwatch":
        def _run_osmwatch_login():
            try:
                import osmwatch as _ow_mod

                _ow_mod.request_login(chat_id)
                send_message(
                    chat_id,
                    "🔐 OSM-Watch: login requested — a fresh QR will be posted to the lab group "
                    "shortly. Scan it with your Lark app to sign the bot in.",
                )
            except Exception as _ow_err:
                print(f"❌ loginosmwatch: {_ow_err!r}", flush=True)
                try:
                    send_message(chat_id, f"❌ /loginosmwatch failed: {_ow_err}")
                except Exception:
                    pass

        start_lark_background_thread(_run_osmwatch_login)
        return

    # ---- OSM-Watch dashboard screenshot (warm browser) ----
    if cmd == "/osmwatch":
        _ow_url = None
        for _tok in cmd_parts[1:]:
            if _tok.startswith("http"):
                _ow_url = _tok
                break

        def _run_osmwatch_shot(chat_id_ow=chat_id, url_ow=_ow_url):
            try:
                import osmwatch as _ow_mod

                send_message(chat_id_ow, "📸 OSM-Watch: capturing the dashboard…")
                box = _ow_mod.capture_and_send(chat_id_ow, url=url_ow)
                err = box.get("error")
                # 'blocked' / 'not_authenticated' already notify the chat themselves;
                # a screenshot on success is sent from inside capture_and_send.
                if err and err not in ("blocked", "not_authenticated"):
                    send_message(chat_id_ow, f"❌ OSM-Watch capture failed: {err}")
            except Exception as _ow_err:
                print(f"❌ osmwatch: {_ow_err!r}", flush=True)
                try:
                    send_message(chat_id_ow, f"❌ /osmwatch failed: {_ow_err}")
                except Exception:
                    pass

        start_lark_background_thread(_run_osmwatch_shot)
        return

    # ---- Encoder / TRTC lookup from latestencoder.json (osmwatch keeps it fresh) ----
    if cmd == "/encoder":
        _enc_arg = " ".join(cmd_parts[1:]).strip()

        def _run_encoder(chat_id_enc=chat_id, arg_enc=_enc_arg):
            try:
                import osmwatch as _ow_mod

                if arg_enc.lower() == "refresh":
                    send_message(chat_id_enc, "🔄 OSM-Watch: refreshing encoder data…")
                    _ow_mod.refresh_encoder(chat_id_enc)
                    return
                # Prefer an interactive emoji card; fall back to plain text when the
                # card can't render (no data / no match / usage) or the send fails.
                _enc_card = _ow_mod.build_encoder_card(arg_enc)
                if _enc_card:
                    _enc_resp = send_message(chat_id_enc, json.dumps(_enc_card), msg_type="interactive")
                    if isinstance(_enc_resp, dict) and _enc_resp.get("code") == 0:
                        return
                for _msg in _ow_mod.query_encoder(arg_enc):
                    send_message(chat_id_enc, _msg)
            except Exception as _enc_err:
                print(f"❌ encoder: {_enc_err!r}", flush=True)
                try:
                    send_message(chat_id_enc, f"❌ /encoder failed: {_enc_err}")
                except Exception:
                    pass

        start_lark_background_thread(_run_encoder)
        return

    # ---- Stress-test announcement paste → one-time reminder 10 min before the set time ----
    if low.startswith("/stresstest"):
        _stress_body = re.sub(
            r"(?is)^\s*/stresstest\b[ \t]*", "", clean_text_multiline or clean_text, count=1
        ).strip()

        def _run_stresstest(body_st=_stress_body):
            try:
                _stress_reply = maintenancemachineagent.handle_stresstest_command(
                    body_st,
                    chat_id=chat_id,
                    send_message=send_message,
                    get_token_func=get_tenant_access_token,
                    scheduler=scheduler,
                    target_user_id=TARGET_USER_OPEN_ID,
                    schedule_chat_id=REMINDER_TARGET_CHAT_ID,
                )
            except Exception as _stress_err:
                _stress_reply = f"❌ /stresstest failed: {_stress_err}"
            if _stress_reply:
                send_message(chat_id, _stress_reply)

        start_lark_background_thread(_run_stresstest)
        return

    # /al or /al DD/MM: Amount Loss (CHECKLOG) → interactive card + TSV.
    if re.match(r"^/al(?:\s+\d{1,2}/\d{1,2})?\s*$", low):
        parts = ct.split()
        date_param = parts[1].strip() if len(parts) > 1 else None
        send_message(chat_id, "⏳ Checking Amount Loss (CHECKLOG), please wait...")
        start_lark_background_thread(run_amountloss_check, chat_id, date_param)
        return

    # /cctv <machine> — EGM CCTV screenshot (no credit check).
    if re.match(r"^/cctv\b", ct, re.I):
        m_cv = re.match(r"^/cctv\s+(\S+)", ct, re.I)
        if not m_cv:
            send_message(
                chat_id,
                "❌ Usage: `/cctv <machine>` — EGM **CCTV** only (no credit check).\n"
                "Example: `/cctv OSMCP181` · `/cctv Dragons-0181`",
            )
            return
        start_lark_background_thread(run_cctv_screenshot_job, chat_id, m_cv.group(1))
        return

    # /npthirdhttp <player_id> [date time]
    if low.startswith("/npthirdhttp"):
        parts = ct.split()
        start_lark_background_thread(run_np_third_http_job, chat_id, parts[1:])
        return

    # Bare /checkcreditdate — interactive card (machine + player + date).
    if re.match(r"^/checkcreditdate\s*$", ct, re.I):
        try:
            import checkcredit

            card_cp = checkcredit.build_checkcredit_player_form_card()
            send_message(chat_id, json.dumps(card_cp), msg_type="interactive")
        except Exception as e:
            send_message(chat_id, f"❌ checkcredit date card failed: {e}")
        return

    # /checkmachinelog <machine> [YYYY-MM-DD]
    if re.search(r"/checkmachinelog\b", ct, re.I):
        m_cml = re.search(r"/checkmachinelog\b\s+(\S+)(?:\s+(\d{4}-\d{2}-\d{2}))?", ct, re.I)
        if not m_cml:
            send_message(
                chat_id,
                "❌ Usage:\n"
                "• `/checkmachinelog <machine> [YYYY-MM-DD]` — last player, transfer-out credit, error ±10 lines (or success log)\n"
                "Examples: `/checkmachinelog DHS3077` · `/checkmachinelog DHS3077 2026-06-26`",
            )
            return
        machine_q = m_cml.group(1).strip()
        date_arg = (m_cml.group(2) or "").strip() or datetime.now().strftime("%Y-%m-%d")
        try:
            datetime.strptime(date_arg, "%Y-%m-%d")
        except ValueError:
            send_message(chat_id, "❌ Date must be `YYYY-MM-DD`.")
            return
        try:
            import checkcredit

            use_oss = checkcredit.checkcredit_use_oss_source()
        except Exception:
            use_oss = True
        thread_root = _checkcredit_begin_thread(chat_id, message_id)
        wait_msg = (
            "⏳ Running checkmachinelog via OSS HTTP, please wait..."
            if use_oss
            else "⏳ Running checkmachinelog (LogNavigator), please wait..."
        )
        _checkcredit_send(chat_id, wait_msg, thread_root=thread_root)
        start_lark_background_thread(run_check_machine_log_job, chat_id, machine_q, date_arg, thread_root)
        return

    # /stuckcredit <machine> [YYYY-MM-DD]
    if re.search(r"/stuckcredit\b", ct, re.I):
        m_sc = re.search(r"/stuckcredit\b\s+(\S+)(?:\s+(\d{4}-\d{2}-\d{2}))?", ct, re.I)
        if not m_sc:
            send_message(
                chat_id,
                "❌ Usage:\n"
                "• `/stuckcredit <machine> [YYYY-MM-DD]` — stuck credit: log + Third Http transfer-out check\n"
                "Examples: `/stuckcredit NWR2938` · `/stuckcredit NWR2938 2026-06-26`",
            )
            return
        machine_q = m_sc.group(1).strip()
        date_arg = (m_sc.group(2) or "").strip() or datetime.now().strftime("%Y-%m-%d")
        try:
            datetime.strptime(date_arg, "%Y-%m-%d")
        except ValueError:
            send_message(chat_id, "❌ Date must be `YYYY-MM-DD`.")
            return
        try:
            import checkcredit

            use_oss = checkcredit.checkcredit_use_oss_source()
        except Exception:
            use_oss = True
        thread_root = _checkcredit_begin_thread(chat_id, message_id)
        wait_msg = (
            "⏳ Stuck credit — reading machine log via OSS HTTP, then Third Http…"
            if use_oss
            else "⏳ Stuck credit — reading machine log (LogNavigator), then Third Http…"
        )
        _checkcredit_send(chat_id, wait_msg, thread_root=thread_root)
        start_lark_background_thread(
            run_check_machine_log_job, chat_id, machine_q, date_arg, thread_root, stuck_credit=True
        )
        return

    # /checkcredit | /checkcreditdate <machine> | /machineerror
    if re.search(r"/(?:checkcreditdate|checkcredit|machineerror)\b", ct, re.I):
        # Longer token first in alternation so `/checkcreditdate` is not parsed as `/checkcredit` + `date`.
        m_cc = re.search(
            r"/(checkcreditdate|checkcredit|machineerror)\b\s+(\S+)(?:\s+(\d{4}-\d{2}-\d{2}))?",
            ct,
            re.I,
        )
        if not m_cc:
            send_message(
                chat_id,
                "❌ Usage:\n"
                "• `/checkcreditdate` — **interactive card**: machine + player + date → Third Http Detail\n"
                "• `/checkcredit <machine>` — **today** (same as `--date` omitted in CLI)\n"
                "• `/checkcreditdate <machine> [YYYY-MM-DD]` — optional date; omit for today\n"
                "• `/machineerror <machine> [YYYY-MM-DD]` — latest two players with error only\n"
                "Examples: `/checkcredit 1171` · `/checkcreditdate 2074 2026-04-27`",
            )
            return
        cmd_cc = (m_cc.group(1) or "").strip().lower()
        machine_q = m_cc.group(2).strip()
        date_arg = (m_cc.group(3) or "").strip()
        if not date_arg:
            date_arg = datetime.now().strftime("%Y-%m-%d")
        try:
            datetime.strptime(date_arg, "%Y-%m-%d")
        except ValueError:
            send_message(chat_id, "❌ Date must be `YYYY-MM-DD` (e.g. `2026-04-27`).")
            return
        try:
            import checkcredit

            use_oss_wait = checkcredit.checkcredit_use_oss_source()
        except Exception:
            use_oss_wait = True
        thread_root = _checkcredit_begin_thread(chat_id, message_id)
        wait_msg = (
            "⏳ Running machineerror via OSS HTTP, please wait..."
            if cmd_cc == "machineerror" and use_oss_wait
            else "⏳ Running machineerror, browser may take a while — please wait..."
            if cmd_cc == "machineerror"
            else "⏳ Running checkcredit via OSS HTTP , please wait..."
            if use_oss_wait
            else "⏳ Running LogNavigator checkcredit, browser may take a while — please wait..."
        )
        _checkcredit_send(chat_id, wait_msg, thread_root=thread_root)
        start_lark_background_thread(
            run_checkcredit_finderror,
            chat_id,
            machine_q,
            date_arg,
            "error_only" if cmd_cc == "machineerror" else "default",
            None,
            thread_root,
        )
        return


    # ---- Scheduled maintenance announcement (action + future date/time + machine list) ----
    if maintenancemachineagent.is_maintenance_schedule_message(original_text, mention_keys):
        def _run_maint_schedule():
            try:
                handled_maint, maint_reply = maintenancemachineagent.handle_maintenance_schedule_message(
                    original_text,
                    mention_keys,
                    chat_id=chat_id,
                    send_message=send_message,
                    get_token_func=get_tenant_access_token,
                    scheduler=scheduler,
                    target_user_id=TARGET_USER_OPEN_ID,
                    schedule_chat_id=REMINDER_TARGET_CHAT_ID,
                )
            except Exception as _maint_err:
                handled_maint, maint_reply = True, f"❌ Maintenance schedule failed: {_maint_err}"
            _finish_agent_branch(handled_maint, maint_reply, send_message)

        start_lark_background_thread(_run_maint_schedule)
        return

    # ---- Read-only machine status from webmachine_data.json ----
    if maintenancemachineagent.is_machine_status_check_message(original_text, mention_keys):
        def _run_status_check():
            try:
                handled_st, st_reply = maintenancemachineagent.handle_machine_status_check_message(
                    original_text,
                    mention_keys,
                    chat_id=chat_id,
                    send_message=send_message,
                )
            except Exception as _st_err:
                handled_st, st_reply = True, f"❌ Machine status check failed: {_st_err}"
            _finish_agent_branch(handled_st, st_reply, send_message)

        start_lark_background_thread(_run_status_check)
        return

    # ---- Short ``set/unset NWR2008`` — execute immediately; LLM only when ambiguous ----
    if maintenancemachineagent.is_direct_set_unset_message(original_text, mention_keys):
        thread_root = _thread_root_for_prod_batch()
        pb_send = make_prod_batch_thread_send(chat_id, thread_root=thread_root)

        def _run_direct_set_unset():
            try:
                handled_direct, direct_reply = maintenancemachineagent.handle_direct_set_unset_message(
                    original_text,
                    mention_keys,
                    chat_id=chat_id,
                    send_message=pb_send,
                    thread_root_message_id=thread_root,
                )
            except Exception as _direct_err:
                handled_direct, direct_reply = True, f"❌ Direct set/unset failed: {_direct_err}"
            _finish_agent_branch(handled_direct, direct_reply, pb_send)

        start_lark_background_thread(_run_direct_set_unset)
        return

    # ---- Bare ``set`` / ``unset`` shorthand — usage help ----
    if maintenancemachineagent.is_short_set_unset_only_message(original_text, mention_keys):
        thread_root = _thread_root_for_prod_batch()
        pb_send = make_prod_batch_thread_send(chat_id, thread_root=thread_root)
        pb_send(
            chat_id,
            maintenancemachineagent.short_set_unset_usage_text(original_text, mention_keys),
        )
        return

    # ---- Immediate set/unset — "ALL <ENV> MACHINES <Venue>" or explicit machine list ----
    if maintenancemachineagent.is_maintenance_now_message(original_text, mention_keys):
        thread_root = _thread_root_for_prod_batch()
        pb_send = make_prod_batch_thread_send(chat_id, thread_root=thread_root)

        def _run_maintenance_now():
            try:
                handled_now, now_reply = maintenancemachineagent.handle_maintenance_now_message(
                    original_text,
                    mention_keys,
                    chat_id=chat_id,
                    send_message=pb_send,
                    thread_root_message_id=thread_root,
                )
            except Exception as _maint_now_err:
                handled_now, now_reply = True, f"❌ Maintenance group command failed: {_maint_now_err}"
            _finish_agent_branch(handled_now, now_reply, pb_send)

        start_lark_background_thread(_run_maintenance_now)
        return

    # ---- /sm — set-machine wizard (env picker card) ----
    if smmachine.is_prod_batch_sm_command(original_text, mention_keys):
        thread_root = _thread_root_for_prod_batch()
        pb_send = make_prod_batch_thread_send(chat_id, thread_root=thread_root)

        def _run_sm_command():
            try:
                handled_sm, sm_reply = smmachine.handle_prod_batch_sm_command(
                    chat_id=chat_id,
                    send_message=pb_send,
                    thread_root_message_id=thread_root,
                )
            except Exception as _sm_err:
                handled_sm, sm_reply = True, f"❌ /sm failed: {_sm_err}"
            _finish_agent_branch(handled_sm, sm_reply, pb_send)

        start_lark_background_thread(_run_sm_command)
        return

    # ---- Prod-batch command (``/nwrsetmaintenance …`` and friends) ----
    if smmachine.is_prod_batch_bot_message(original_text, mention_keys) or smmachine.is_prod_batch_bot_message(
        clean_text, []
    ):
        if smmachine.is_prod_batch_bot_message(original_text, mention_keys):
            _pb_text, _pb_mentions = original_text, mention_keys
        else:
            _pb_text, _pb_mentions = clean_text, []
        thread_root = _thread_root_for_prod_batch()
        pb_send = make_prod_batch_thread_send(chat_id, thread_root=thread_root)

        def _run_prod_batch_command(pb_text=_pb_text, pb_mentions=_pb_mentions):
            try:
                handled_pb, pb_reply = smmachine.handle_prod_batch_bot_command(
                    pb_text,
                    pb_mentions,
                    chat_id=chat_id,
                    send_message=pb_send,
                    thread_root_message_id=thread_root,
                )
            except Exception as _pb_err:
                handled_pb, pb_reply = True, f"❌ Prod-batch command failed: {_pb_err}"
            _finish_agent_branch(handled_pb, pb_reply, pb_send)

        start_lark_background_thread(_run_prod_batch_command)
        return

    # ---- Find machine — interactive form card ----
    if re.match(r"^/(?:findmachine|fm)\b", ct, re.I) or re.match(r"(?i)^find\s*machines?\s*$", ct):
        def _run_findmachine_card():
            try:
                import findmachine as _findmachine

                card_fm = _findmachine.build_findmachine_form_card()
                resp_fm = send_message(chat_id, json.dumps(card_fm, ensure_ascii=False), msg_type="interactive")
                if isinstance(resp_fm, dict) and resp_fm.get("code") not in (0, None):
                    send_message(chat_id, f"❌ Find-machine card rejected: {resp_fm}")
            except Exception as e:
                send_message(chat_id, f"❌ findmachine card failed: {e}")

        start_lark_background_thread(_run_findmachine_card)
        return

    # ---- Asset / encoder sheet lookups (/nch /nwr /wf /tbr /tbp /cp /dhs /mdr) ----
    for _prefix, _mod_name, _fn_name, _title, _usage in _MACHINE_LOOKUPS:
        if low.startswith(_prefix):
            query = _machine_query_after_prefix(ct, _prefix)

            def _run_lookup(q=query, mod_name=_mod_name, fn_name=_fn_name, title=_title,
                            usage=_usage, prefix=_prefix):
                if not q:
                    send_message(chat_id, usage)
                    return
                try:
                    import importlib

                    _mod = importlib.import_module(mod_name)
                    _send_machine_lookup_card(chat_id, getattr(_mod, fn_name)(q), title=title)
                except Exception as _lk_err:
                    send_message(chat_id, f"❌ {prefix} lookup failed: {_lk_err}")

            start_lark_background_thread(_run_lookup)
            return

    # ---- Admin: self-deploy — "git pull origin main and restart service" / /deploy ----
    if _looks_like_deploy_command(ct) or _looks_like_deploy_command(clean_text_multiline):
        if not _deploy_allowed(sender_id):
            send_message(chat_id, "❌ You are not allowed to deploy this bot.")
            return
        _handle_deploy_command(chat_id)
        return

    # Missing Credit alert → parse + checkcredit form card (requires @mention).
    _full_body = _lark_full_message_body(original_text, clean_text_multiline, message_content_raw)
    if _try_missing_credit_inquiry(
        chat_id, _full_body, bot_mentioned=bot_mentioned, message_id=message_id, send_func=send_message
    ):
        return


    # ---- Nothing matched — help only when directly addressed ----
    if chat_type == "p2p" or bot_mentioned:
        send_message(chat_id, _HELP_TEXT)


# ================= Flask webhook (persistent-connection frames dispatch here in-process) =================
@app.route("/", methods=["GET"])
def _index():
    return jsonify({"ok": True, "service": "machinebot"})


@app.route("/webhook/event", methods=["POST", "GET", "OPTIONS"])
def lark_webhook():
    if request.method in ("GET", "OPTIONS"):
        return jsonify({"ok": True})

    data = _lark_safe_parse_json_body(request)
    if not isinstance(data, dict):
        return jsonify({"error": "bad body"}), 400

    data = _feishu_maybe_decrypt_webhook_payload(data)
    if not isinstance(data, dict):
        return jsonify({"error": "bad body"}), 400

    # URL verification handshake (only used in public-webhook mode; harmless under long connection).
    if data.get("type") == "url_verification" or ("challenge" in data and "header" not in data):
        return jsonify({"challenge": data.get("challenge", "")})

    data = _lark_normalize_legacy_card_trigger_v1_flat(data)
    data = _lark_coerce_event_dict(data)
    data = _lark_normalize_card_callback_envelope(data)

    # Verification token (schema 2.0 header.token). Only reject when present AND mismatched.
    token_in = _lark_extract_verification_token(data)
    if VERIFICATION_TOKEN and token_in and token_in != VERIFICATION_TOKEN:
        print(f"[lark] verification token mismatch (got {token_in!r}) — 403", flush=True)
        return jsonify({"error": "invalid verification token"}), 403

    hdr_et = _lark_header_event_type(data)

    # ---- card.action.trigger (prod-batch / sm wizard / findmachine form / reminder buttons) ----
    card_resolved = _lark_resolve_card_action(data)
    if card_resolved is not None:
        chat_id_ca, sender_id_ca, val_ca, eid_ca = card_resolved
        if sender_id_ca and BOT_OPEN_ID and sender_id_ca == BOT_OPEN_ID:
            return _lark_http_card_callback_ok()
        parsed_sync = _lark_parse_card_action_value(val_ca)
        if isinstance(parsed_sync, dict):
            thread_r = str(parsed_sync.get("r") or "").strip()
            if thread_r and chat_id_ca:
                _set_prod_batch_thread_root(chat_id_ca, thread_r)
            # /sm wizard env pick updates the card IN-PLACE — must answer inside the 3s window.
            try:
                import smmachine as _sm_sync

                sm_sync = _sm_sync.try_prod_batch_sm_env_card_response(
                    parsed_sync,
                    chat_id=chat_id_ca or "",
                )
            except Exception as _sm_sync_err:
                print(f"❌ sm env card sync response failed: {_sm_sync_err!r}", flush=True)
                sm_sync = None
            if sm_sync is not None:
                if eid_ca:
                    _remember_processed_message_id(str(eid_ca))
                return _lark_http_card_callback_response(sm_sync)
        # Never do slow work on this thread — Lark times out ~3s (code: undefined toast).
        threading.Thread(
            target=_run_card_callback_worker, args=(data, card_resolved), daemon=True
        ).start()
        return _lark_http_card_callback_ok()
    if _lark_payload_has_card_action(data):
        print("[lark] card-like payload but resolver returned None — ACK 200 {}", flush=True)
        return _lark_http_card_callback_ok()

    # ---- im.message.receive_v1 ----
    if hdr_et == "im.message.receive_v1":
        event = data.get("event", {}) or {}
        message = event.get("message", {}) or {}
        chat_id = message.get("chat_id")
        message_id = message.get("message_id")
        chat_type = message.get("chat_type")
        mentions = message.get("mentions", []) or []
        message_content_raw = message.get("content") or "{}"
        try:
            text = _lark_extract_message_text(message_content_raw)
        except Exception as ex:
            print(f"[lark] content parse failed: {ex!r}", flush=True)
            text = ""

        sender = event.get("sender", {}) or {}
        sid_obj = sender.get("sender_id") or {}
        sender_id = sid_obj.get("open_id") if isinstance(sid_obj, dict) else None

        if _lark_skip_stale_event_on_startup(data):
            print(f"⏭️ Stale event ignored (before bot start) message_id={message_id!r}", flush=True)
            return _lark_im_done()

        if message_id and _remember_processed_message_id(message_id):
            print(f"⏭️ Duplicate message {message_id} ignored", flush=True)
            return _lark_im_done()

        if sender_id and BOT_OPEN_ID and sender_id == BOT_OPEN_ID:
            print("⏭️ Ignoring own message", flush=True)
            return _lark_im_ack()

        if not chat_id or text is None:
            print("❌ Could not extract chat_id or text", flush=True)
            return jsonify({"error": "Missing data"}), 400

        original_text = text
        # Strip @mention placeholders before command parsing; keep the keys for the
        # maintenance-agent matchers (they receive original_text + mention_keys).
        mention_keys = [m.get("key", "") for m in mentions if m.get("key")]
        for key in mention_keys:
            text = text.replace(key, "")
        text = re.sub(r"@_user_\d+", "", text)
        text = re.sub(r"<[^>]+>", "", text)
        clean_text_multiline = re.sub(r"[ \t]+\n", "\n", text).strip()
        clean_text_multiline = re.sub(r"\n[ \t]+", "\n", clean_text_multiline)
        clean_text = re.sub(r"\s+", " ", clean_text_multiline).strip()

        # Group chats require an @mention; p2p always responds.
        bot_mentioned = chat_type == "p2p"
        if chat_type != "p2p":
            for mention in mentions:
                mid_obj = mention.get("id")
                mid = mid_obj.get("open_id", "") if isinstance(mid_obj, dict) else mid_obj
                if mid and BOT_OPEN_ID and mid == BOT_OPEN_ID:
                    bot_mentioned = True
                    break

        stripped_choice = clean_text.strip()
        pend_np = _get_checkcredit_np_pending(chat_id)
        _np_choices = (pend_np or {}).get("np_choices") or []
        # Only an IN-RANGE digit is an NP pick — an out-of-range "3"/"4" in a group without
        # @mention is silently acked (not answered with an error), matching osedutybot.
        is_np_reply = stripped_choice in ("1", "2", "3", "4") and 1 <= int(stripped_choice) <= len(
            _np_choices
        )

        if chat_type != "p2p" and not bot_mentioned and not is_np_reply:
            return _lark_im_ack()

        _handle_machine_message(
            chat_id,
            sender_id,
            message_id,
            chat_type,
            bot_mentioned,
            is_np_reply,
            original_text,
            clean_text,
            clean_text_multiline,
            mention_keys,
            message,
            message_content_raw,
        )
        return _lark_im_done()

    # ---- events we subscribed to but do not implement ----
    if _lark_ack_only_event_type(hdr_et):
        return _lark_im_done()
    if _lark_payload_has_card_action(data) or hdr_et.lower().startswith("card.action"):
        return _lark_http_card_callback_ok()
    print(f"⚠️ Unknown webhook branch hdr_et={hdr_et!r}", flush=True)
    return _lark_im_done()


# ================= Lark persistent connection (long connection / WebSocket) =================
def _lark_event_mode() -> str:
    """``http`` = public Request URL only; ``websocket`` = persistent connection + local Flask."""
    return (os.getenv("LARK_EVENT_MODE") or "websocket").strip().lower()


def _lark_ws_uses_persistent_connection() -> bool:
    return _lark_event_mode() in ("websocket", "ws", "longconn", "persistent", "long_connection")


def _lark_ws_ensure_inbound_message_id(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return payload
    ev = payload.get("event")
    if not isinstance(ev, dict):
        return payload
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return payload
    if (msg.get("message_id") or "").strip():
        return payload
    for alt in (
        ev.get("message_id"),
        (ev.get("message") or {}).get("message_id") if isinstance(ev.get("message"), dict) else None,
    ):
        mid = str(alt or "").strip()
        if mid:
            msg["message_id"] = mid
            break
    return payload


def _lark_ws_ensure_card_webhook_payload(payload: dict) -> dict:
    out = dict(payload)
    out.setdefault("schema", "2.0")
    hdr = dict(out.get("header") or {})
    hdr.setdefault("event_type", "card.action.trigger")
    hdr.setdefault("event_id", hdr.get("event_id") or str(uuid.uuid4()))
    if VERIFICATION_TOKEN and not str(hdr.get("token") or "").strip():
        hdr["token"] = VERIFICATION_TOKEN
    out["header"] = hdr
    ev = out.get("event")
    if isinstance(ev, dict):
        ctx = ev.get("context") if isinstance(ev.get("context"), dict) else {}
        if not ev.get("open_chat_id") and ctx.get("open_chat_id"):
            ev["open_chat_id"] = str(ctx["open_chat_id"]).strip()
        if not ev.get("chat_id") and ctx.get("chat_id"):
            ev["chat_id"] = str(ctx["chat_id"]).strip()
        out["event"] = ev
    return out


def _lark_ws_to_webhook_payload(data) -> dict:
    import lark_oapi as lark

    raw = json.loads(lark.JSON.marshal(data))
    if isinstance(raw, dict) and "header" in raw and "event" in raw:
        payload = dict(raw)
        hdr = dict(payload.get("header") or {})
        payload["header"] = hdr
    else:
        inner = raw.get("event", raw) if isinstance(raw, dict) else raw
        payload = {
            "schema": "2.0",
            "header": {
                "event_id": str(uuid.uuid4()),
                "event_type": "im.message.receive_v1",
                "create_time": str(int(time.time() * 1000)),
            },
            "event": inner,
        }
    if VERIFICATION_TOKEN:
        hdr = payload.setdefault("header", {})
        if not str(hdr.get("token") or "").strip():
            hdr["token"] = VERIFICATION_TOKEN
    payload = _lark_ws_ensure_inbound_message_id(payload)
    mid = (
        ((payload.get("event") or {}).get("message") or {}).get("message_id")
        if isinstance(payload.get("event"), dict)
        else None
    )
    if not str(mid or "").strip():
        print("[lark-ws] warning: payload missing event.message.message_id", flush=True)
    return payload


def _lark_ws_dispatch_payload(payload: dict) -> tuple[int, dict]:
    """In-process POST to ``lark_webhook`` (same handlers as HTTPS Request URL mode)."""
    with app.test_client() as client:
        rv = client.post("/webhook/event", json=payload)
    body: dict = {}
    if rv.data:
        try:
            parsed = json.loads(rv.get_data(as_text=True))
            if isinstance(parsed, dict):
                body = parsed
        except (ValueError, TypeError):
            body = {}
    return int(rv.status_code), body


def _lark_ws_on_message(data) -> None:
    try:
        payload = _lark_ws_to_webhook_payload(data)
        status, _ = _lark_ws_dispatch_payload(payload)
        print(f"[lark-ws] im.message.receive_v1 dispatched status={status}", flush=True)
    except Exception as exc:
        print(f"[lark-ws] im.message dispatch failed: {exc!r}", flush=True)


def _lark_ws_on_card_action(data):
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    import lark_oapi as lark

    try:
        payload = _lark_ws_ensure_card_webhook_payload(json.loads(lark.JSON.marshal(data)))
        status, body = _lark_ws_dispatch_payload(payload)
        print(
            f"[lark-ws] card.action.trigger dispatched status={status} resp_keys={list(body.keys())!r}",
            flush=True,
        )
        if status == 200 and isinstance(body, dict):
            return P2CardActionTriggerResponse(body)
        if status == 403:
            print(
                "[lark-ws] card callback 403 — check VERIFICATION_TOKEN matches developer console",
                flush=True,
            )
    except Exception as exc:
        print(f"[lark-ws] card callback failed: {exc!r}", flush=True)
    return P2CardActionTriggerResponse({})


def _lark_ws_handler_dispatch(handler, payload: bytes) -> Any:
    """Dispatch a WebSocket frame through ``EventDispatcherHandler`` (SDK method name varies)."""
    for name in ("_do_without_validation", "do_without_validation"):
        fn = getattr(handler, name, None)
        if callable(fn):
            return fn(payload)
    return _lark_ws_handler_dispatch_manual(handler, payload)


def _lark_ws_handler_dispatch_manual(handler, payload: bytes) -> Any:
    """Last resort when installed lark-oapi predates ``do_without_validation``."""
    from lark_oapi.core.const import UTF_8
    from lark_oapi.core.json import JSON
    from lark_oapi.core.utils import Strings
    from lark_oapi.event.context import EventContext
    from lark_oapi.core.exception import EventException

    pl = payload.decode(UTF_8)
    context = JSON.unmarshal(pl, EventContext)
    if Strings.is_not_empty(context.schema):
        context.schema = "p2"
        context.type = context.header.event_type
    elif Strings.is_not_empty(context.uuid):
        context.schema = "p1"
        context.type = context.event.get("type")

    event_key = f"{context.schema}.{context.type}"
    cb_map = getattr(handler, "_callback_processor_map", None) or {}
    if event_key in cb_map:
        processor = cb_map.get(event_key)
        if processor is None:
            raise EventException(f"callback processor not found, type: {context.type}")
        data = JSON.unmarshal(pl, processor.type())
        return processor.do(data)

    proc_map = getattr(handler, "_processorMap", None) or {}
    processor = proc_map.get(event_key)
    if processor is None:
        raise EventException(f"processor not found, type: {context.type}")
    data = JSON.unmarshal(pl, processor.type())
    processor.do(data)
    return None


def _lark_ws_apply_card_frame_patch() -> None:
    """lark-oapi ws client drops MessageType.CARD without ACK → Lark shows code: undefined."""
    try:
        from lark_oapi.core.const import UTF_8
        from lark_oapi.core.json import JSON
        from lark_oapi.ws.client import Client, _get_by_key
        from lark_oapi.ws.const import (
            HEADER_BIZ_RT,
            HEADER_MESSAGE_ID,
            HEADER_SEQ,
            HEADER_SUM,
            HEADER_TRACE_ID,
            HEADER_TYPE,
        )
        from lark_oapi.ws.enum import MessageType
        from lark_oapi.ws.model import Response as _WsResponse
    except ImportError:
        print("[lark-ws] pip install lark-oapi for persistent connection mode", flush=True)
        raise

    if getattr(Client, "_machinebot_card_patch", False):
        return

    async def _handle_data_frame_patched(self, frame):
        hs = frame.headers
        msg_id = _get_by_key(hs, HEADER_MESSAGE_ID)
        trace_id = _get_by_key(hs, HEADER_TRACE_ID)
        sum_ = _get_by_key(hs, HEADER_SUM)
        seq = _get_by_key(hs, HEADER_SEQ)
        type_ = _get_by_key(hs, HEADER_TYPE)

        pl = frame.payload
        if int(sum_) > 1:
            pl = self._combine(msg_id, int(sum_), int(seq), pl)
            if pl is None:
                return

        message_type = MessageType(type_)
        resp = _WsResponse(code=http.HTTPStatus.OK)
        try:
            start = int(round(time.time() * 1000))
            if message_type in (MessageType.EVENT, MessageType.CARD):
                result = _lark_ws_handler_dispatch(self._event_handler, pl)
            else:
                return
            end = int(round(time.time() * 1000))
            header = hs.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
                resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception as e:
            from lark_oapi.core.log import logger

            logger.error(
                self._fmt_log(
                    "handle message failed, message_type: {}, message_id: {}, trace_id: {}, err: {}",
                    message_type.value,
                    msg_id,
                    trace_id,
                    e,
                )
            )
            resp = _WsResponse(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = JSON.marshal(resp).encode(UTF_8)
        await self._write_message(frame.SerializeToString())

    Client._handle_data_frame = _handle_data_frame_patched
    Client._machinebot_card_patch = True
    print("[lark-ws] patched lark-oapi ws Client for CARD callbacks", flush=True)


def _run_lark_ws_forever() -> None:
    """Block on Lark persistent connection (im.message + card.action.trigger)."""
    import lark_oapi as lark

    if not (APP_ID and APP_SECRET):
        raise RuntimeError("Set APP_ID and APP_SECRET in .env for LARK_EVENT_MODE=websocket")

    _lark_ws_apply_card_frame_patch()
    builder = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_lark_ws_on_message)
        .register_p2_card_action_trigger(_lark_ws_on_card_action)
    )
    # The bot's own GotIt/DONE reactions + read receipts make Lark push extra events;
    # register no-op handlers so they ACK cleanly instead of logging "processor not found".
    for _reg_name in (
        "register_p2_im_message_reaction_created_v1",
        "register_p2_im_message_reaction_deleted_v1",
        "register_p2_im_message_message_read_v1",
    ):
        _reg = getattr(builder, _reg_name, None)
        if callable(_reg):
            builder = _reg(lambda _data: None)
    handler = builder.build()
    _probe = getattr(handler, "_do_without_validation", None) or getattr(
        handler, "do_without_validation", None
    )
    print(
        "[lark-ws] EventDispatcherHandler dispatch="
        + (getattr(_probe, "__name__", "manual_fallback") if callable(_probe) else "manual_fallback"),
        flush=True,
    )
    domain_name = (os.getenv("LARK_DOMAIN") or "lark").strip().lower()
    domain = lark.FEISHU_DOMAIN if domain_name == "feishu" else lark.LARK_DOMAIN
    cli = lark.ws.Client(
        str(APP_ID).strip(),
        str(APP_SECRET).strip(),
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
        domain=domain,
    )
    print(
        "[lark-ws] Persistent connection active (im.message + card.action.trigger). "
        "Developer console: Subscription mode → Receive events through persistent connection.",
        flush=True,
    )
    cli.start()


def _resolve_bot_open_id_on_startup() -> None:
    """Pin BOT_OPEN_ID from Lark so group @mention detection + self-skip work without manual config."""
    global BOT_OPEN_ID
    if BOT_OPEN_ID:
        print(f"[lark] BOT_OPEN_ID pinned from .env: {BOT_OPEN_ID!r}", flush=True)
        return
    try:
        oid = get_bot_open_id()
    except Exception as ex:
        print(f"[lark] bot open_id lookup failed: {ex!r}", flush=True)
        oid = None
    if oid:
        BOT_OPEN_ID = oid
        print(f"[lark] BOT_OPEN_ID resolved from Lark: {BOT_OPEN_ID!r}", flush=True)
    else:
        print(
            "[lark] WARNING: BOT_OPEN_ID unresolved — group @mention detection may fail. "
            "Set BOT_OPEN_ID in .env to fix.",
            flush=True,
        )


def _mount_webmachine_dashboard() -> None:
    """Mount the ``/wm`` machine dashboard + start the background scrape loop that keeps
    ``webmachine_data.json`` fresh (set/unset targeting, machine status and /findmachine
    all read that file). Opt out with ``WEBMACHINE_MOUNT_IN_MAIN=0``."""
    _v = (os.environ.get("WEBMACHINE_MOUNT_IN_MAIN") or "").strip().lower()
    if _v in ("0", "false", "no", "off"):
        return
    try:
        import webmachine as _wm
    except Exception as e:
        print("[webmachine] optional mount skipped (import failed): %r" % (e,), flush=True)
        return
    prefix = (os.environ.get("WEBMACHINE_URL_PREFIX") or "/wm").strip()
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix
    try:
        _wm.register_webmachine(app, url_prefix=prefix)
        _wm.start_background_scrape_loop()
        print(
            "[webmachine] dashboard registered at prefix %r "
            "(scrape loop on; WEBMACHINE_SCRAPE=0 to disable)" % prefix,
            flush=True,
        )
    except Exception as e:
        print("[webmachine] optional mount failed: %r" % (e,), flush=True)


def _run_main_entry() -> int:
    """
    ``LARK_EVENT_MODE=websocket`` (default here) — Flask in a background thread (diag + /wm
    dashboard) + Lark persistent connection on the main thread. ``http`` — Flask only
    (needs a public HTTPS Request URL).
    """
    import traceback

    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)

    try:
        port = int(os.getenv("PORT") or os.getenv("LARKBOT_PORT") or "5000")

        _resolve_bot_open_id_on_startup()

        # One-time maintenance / stress-test reminders fire from this scheduler.
        try:
            if not scheduler.running:
                scheduler.start()
                atexit.register(lambda: scheduler.shutdown(wait=False))
        except Exception as _sched_err:
            print(f"⚠️ scheduler start failed: {_sched_err!r}", flush=True)

        _mount_webmachine_dashboard()

        # Warm pools — every one is optional; a failure degrades to cold-start per request.
        try:
            import prod_machine_batch as _boot_pmb

            _boot_pmb.prewarm_prod_env_pool_on_startup()
        except Exception as _boot_pmb_err:
            print(f"[prod-warm] startup pre-warm skipped: {_boot_pmb_err!r}", flush=True)
        try:
            import smmachine as _boot_wm

            _boot_wm.prewarm_webmachine_scrape_pool_on_startup()
        except Exception as _boot_wm_err:
            print(f"[wm-warm] startup pre-warm skipped: {_boot_wm_err!r}", flush=True)
        try:
            import checkcredit as _boot_cc

            _boot_cc._ensure_writable_temp_dir()
        except Exception as _boot_tmp_err:
            print(f"[checkcredit] temp dir init failed: {_boot_tmp_err!r}", flush=True)
        try:
            from third_http_warm_pool import prewarm_third_http_pool_on_startup

            prewarm_third_http_pool_on_startup()
        except Exception as _boot_th_err:
            print(f"[third-http-warm] startup pre-warm skipped: {_boot_th_err!r}", flush=True)
        try:
            import osmwatch as _boot_ow

            _boot_ow.prewarm_osmwatch_on_startup()
        except Exception as _boot_ow_err:
            print(f"[osmwatch-warm] startup pre-warm skipped: {_boot_ow_err!r}", flush=True)

        if _lark_ws_uses_persistent_connection():
            # /wm dashboard is served by this Flask; bind 0.0.0.0 via FLASK_BIND_HOST when the
            # dashboard must be reachable from other hosts (or tunnel it, e.g. ngrok).
            bind_host = (os.getenv("FLASK_BIND_HOST") or "127.0.0.1").strip() or "127.0.0.1"

            def _flask_bg() -> None:
                app.run(host=bind_host, port=port, debug=False, threaded=True, use_reloader=False)

            threading.Thread(target=_flask_bg, daemon=True, name="machinebot-flask").start()
            print(
                "[lark] LARK_EVENT_MODE=websocket — Flask on http://%s:%d (diag + /wm); "
                "events via persistent connection." % (bind_host, port),
                flush=True,
            )
            time.sleep(1.0)
            _run_lark_ws_forever()
            return 0

        print(
            "[lark] Listening http://0.0.0.0:%d (threaded=True). "
            "Feishu Request URL must be HTTPS and reachable; reverse-proxy to this port." % port,
            flush=True,
        )
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
        return 0
    except OSError as e:
        traceback.print_exc(file=sys.stderr)
        print(f"Flask bind failed (port in use?): {e}", file=sys.stderr, flush=True)
        return 1
    except BaseException:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_run_main_entry())
