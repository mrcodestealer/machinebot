"""
``/sst`` — scheduled Set-Maintenance / Set-Test form card.

Flow
----
1. ``/sst`` posts a form card: **date**, **time**, **Maintenance** / **Test** toggle buttons and a
   large multi-line box for machine names (one per line).
2. Tapping **Maintenance** / **Test** toggles that selection and updates the card in place, showing
   ``Selected Set Maintenance`` / ``Selected Set Maintenance and Test`` / ``Selected Set Test``.
   Typed machine names and the picked date/time are preserved across a toggle.
3. **Confirm** validates the form and resolves every machine against ``webmachine_data.json``.
   * any unknown name → ``{machine} is not detected. Try again.`` (nothing is scheduled)
   * all found → a **review card** listing every machine for a second confirmation
4. Confirming the review card schedules **both**: a reminder 10 minutes before, and the action
   itself to run automatically at the chosen time.

The toggle/confirm buttons answer inside Lark's 3 s card-callback window (in-place card update);
the actual EGM work runs later on the scheduler, never on the callback thread.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

SST_CARD_KEY = "sst"

# Reminder lead time before the scheduled action.
SST_REMINDER_LEAD_MIN = 10

# ``/sst`` is restricted to this group — the form schedules a real, unattended PROD change, so it
# must not be drivable from arbitrary chats. Override with ``SST_ALLOWED_CHAT_ID``.
import os as _os  # noqa: E402  (kept local to this constant)

SST_ALLOWED_CHAT_ID = (
    _os.environ.get("SST_ALLOWED_CHAT_ID", "").strip()
    or "oc_51b6fbf2636525acfb4ead3afa3c93ce"
)


def chat_allowed(chat_id: str) -> bool:
    return (chat_id or "").strip() == SST_ALLOWED_CHAT_ID

_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSIONS_LOCK = threading.Lock()
_SESSION_TTL_SEC = 7200

_MACHINE_SPLIT_RE = re.compile(r"[,\n;&]+")


# ---------------------------------------------------------------------------
# session store
# ---------------------------------------------------------------------------
def _cleanup_sessions() -> None:
    now = time.time()
    with _SESSIONS_LOCK:
        for sid in [k for k, v in _SESSIONS.items() if now - float(v.get("ts") or 0) > _SESSION_TTL_SEC]:
            _SESSIONS.pop(sid, None)


def new_session(chat_id: str, *, thread_root: str | None = None) -> str:
    _cleanup_sessions()
    sid = uuid.uuid4().hex[:12]
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = {
            "chat_id": chat_id,
            "thread_root": (thread_root or "").strip() or None,
            "maint": False,
            "test": False,
            "date": "",
            "time": "",
            "machines_text": "",
            "ts": time.time(),
        }
    return sid


def get_session(sid: str) -> dict[str, Any] | None:
    _cleanup_sessions()
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(sid)
        return dict(s) if s else None


def update_session(sid: str, **fields: Any) -> dict[str, Any] | None:
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(sid)
        if not s:
            return None
        s.update(fields)
        s["ts"] = time.time()
        return dict(s)


# ---------------------------------------------------------------------------
# selection label
# ---------------------------------------------------------------------------
def selection_text(maint: bool, test: bool) -> str:
    """The line shown under the toggles (exact wording requested)."""
    if maint and test:
        return "✅ Selected Set Maintenance and Test"
    if maint:
        return "✅ Selected Set Maintenance"
    if test:
        return "✅ Selected Set Test"
    return "⚠️ Kindly select Set Maintenance or Test"


def selection_action(maint: bool, test: bool) -> str | None:
    """Map the toggles to a prod-batch action code."""
    if maint and test:
        return "set_both"
    if maint:
        return "set_maint"
    if test:
        return "set_test"
    return None


# ---------------------------------------------------------------------------
# form card
# ---------------------------------------------------------------------------
def _time_options() -> list[dict]:
    """Every 10 minutes, 12-hour labels — same granularity as the reminder form."""
    out: list[dict] = []
    for hh in range(24):
        for mm in range(0, 60, 10):
            ap = "AM" if hh < 12 else "PM"
            hh12 = hh % 12 or 12
            v = f"{hh12}:{mm:02d}{ap}"
            out.append({"text": {"tag": "plain_text", "content": v}, "value": v})
    return out


def _initial_time_index(current: str) -> int:
    opts = _time_options()
    want = (current or "").strip() or "9:30AM"
    return next((i + 1 for i, o in enumerate(opts) if o.get("value") == want), 1)


def _toggle_button(label: str, *, on: bool, sid: str, which: str) -> dict:
    # ``name`` is REQUIRED for every interactive component inside a form container — without it
    # Lark rejects the whole card (and the send failure used to be silent).
    return {
        "tag": "button",
        "name": f"sst_toggle_{which}",
        "text": {"tag": "plain_text", "content": ("✅ " if on else "") + label},
        "type": "primary" if on else "default",
        "behaviors": [{"type": "callback", "value": {"k": SST_CARD_KEY, "a": "toggle",
                                                     "s": sid, "w": which}}],
    }


def build_form_card(sid: str, session: dict[str, Any]) -> dict:
    maint = bool(session.get("maint"))
    test = bool(session.get("test"))
    date_v = str(session.get("date") or "").strip()
    time_v = str(session.get("time") or "").strip()
    machines_v = str(session.get("machines_text") or "")

    date_el: dict[str, Any] = {
        "tag": "date_picker",
        "name": "sst_date",
        "placeholder": {"tag": "plain_text", "content": "Pick the date"},
        "required": True,
    }
    if date_v:
        date_el["initial_date"] = date_v

    machines_el: dict[str, Any] = {
        "tag": "input",
        "name": "sst_machines",
        "input_type": "multiline_text",
        "rows": 8,
        "auto_resize": True,
        "max_rows": 20,
        "width": "fill",
        "label": {"tag": "plain_text", "content": "Machines (one per line)"},
        "label_position": "top",
        "placeholder": {"tag": "plain_text", "content": "NWR2205\nNWR2206\nNWR2207"},
        "required": True,
        "max_length": 4000,
    }
    if machines_v:
        machines_el["default_value"] = machines_v

    form_elements: list[dict] = [
        {"tag": "div", "text": {"tag": "plain_text", "content": "Date"}},
        date_el,
        {"tag": "div", "text": {"tag": "plain_text", "content": "Time (every 10 minutes)"}},
        {
            "tag": "select_static",
            "name": "sst_time",
            "placeholder": {"tag": "plain_text", "content": "Select time"},
            "options": _time_options(),
            "required": True,
            "initial_index": _initial_time_index(time_v),
        },
        {"tag": "div", "text": {"tag": "lark_md", "content": "**What to set** — tap to select:"}},
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [_toggle_button("Maintenance", on=maint, sid=sid, which="maint")]},
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [_toggle_button("Test", on=test, sid=sid, which="test")]},
            ],
        },
        {"tag": "div", "text": {"tag": "lark_md", "content": selection_text(maint, test)}},
        machines_el,
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [{
                    "tag": "button",
                    "name": "sst_confirm",
                    "text": {"tag": "plain_text", "content": "Confirm"},
                    "type": "primary",
                    "form_action_type": "submit",
                    "behaviors": [{"type": "callback",
                                   "value": {"k": SST_CARD_KEY, "a": "confirm", "s": sid}}],
                }]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [{
                    "tag": "button",
                    "name": "sst_cancel",
                    "text": {"tag": "plain_text", "content": "Cancel"},
                    "type": "danger",
                    "behaviors": [{"type": "callback",
                                   "value": {"k": SST_CARD_KEY, "a": "cancel", "s": sid}}],
                }]},
            ],
        },
    ]

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": "🧪 Scheduled Set Maintenance / Test"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": "Pick the **date** and **time**, choose **Maintenance** and/or **Test**, "
                        "then paste the machines (one per line) and tap **Confirm**."}},
            {"tag": "form", "name": "sst_form", "elements": form_elements},
        ]},
    }


# ---------------------------------------------------------------------------
# machine resolution against webmachine_data.json
# ---------------------------------------------------------------------------
def parse_machine_lines(raw: str) -> list[str]:
    """Split the textarea into machine tokens (newline / comma / ; / & separated)."""
    out: list[str] = []
    for part in _MACHINE_SPLIT_RE.split(raw or ""):
        tok = part.strip()
        if tok:
            out.append(tok)
    return out


def resolve_machines(tokens: list[str]) -> tuple[list[dict], list[str], list[dict]]:
    """
    Look every token up in ``webmachine_data.json`` (PROD rows only).

    Reuses ``smmachine.resolve_prod_batch_token_hits`` so the returned dicts are exactly the shape
    the prod-batch runner consumes (``belongs`` / ``machine`` / ``status`` / ``online``).
    The environment is inferred per token from its own name, so one form may mix sites.

    Returns ``(found, missing_tokens, ambiguous)``.
    """
    import smmachine
    from maintenancemachineagent import load_webmachine_rows, _env_from_machine_name

    rows = [r for r in load_webmachine_rows()
            if str(r.get("environment") or "PROD").strip().upper() == "PROD"]

    found: list[dict] = []
    missing: list[str] = []
    ambiguous: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for tok in tokens:
        env_code = (_env_from_machine_name(tok) or "").strip().upper()
        hits = smmachine.resolve_prod_batch_token_hits(env_code, tok, rows)
        if not hits:
            missing.append(tok)
            continue
        if len(hits) > 1:
            ambiguous.append({"token": tok, "candidates": hits})
            continue
        hit = dict(hits[0])
        dedupe = (str(hit.get("belongs") or "").upper(), str(hit.get("machine") or ""))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        hit["token"] = tok
        found.append(hit)
    return found, missing, ambiguous


# ---------------------------------------------------------------------------
# review card
# ---------------------------------------------------------------------------
def parse_when(date_s: str, time_s: str) -> datetime | None:
    """``2026-08-05`` + ``9:30PM`` → datetime (local)."""
    d = (date_s or "").strip()
    t = (time_s or "").strip().upper().replace(" ", "")
    if not d or not t:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})(AM|PM)$", t)
    if not m:
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "AM":
        hh = 0 if hh == 12 else hh
    else:
        hh = 12 if hh == 12 else hh + 12
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            base = datetime.strptime(d[:10], fmt)
            break
        except ValueError:
            continue
    else:
        return None
    return base.replace(hour=hh, minute=mm, second=0, microsecond=0)


def build_review_card(sid: str, session: dict[str, Any], found: list[dict], when: datetime) -> dict:
    maint, test = bool(session.get("maint")), bool(session.get("test"))
    lines = [
        f"**When:** {when.strftime('%Y-%m-%d %I:%M%p')}",
        f"**Action:** Set {_action_words(maint, test)}",
        f"**Reminder:** {SST_REMINDER_LEAD_MIN} min before "
        f"({(when - timedelta(minutes=SST_REMINDER_LEAD_MIN)).strftime('%I:%M%p')})",
        f"**Machines:** {len(found)}",
        "",
    ]
    for f in found[:60]:
        bits = " · ".join(str(f.get(k) or "") for k in ("belongs", "status", "online") if f.get(k))
        lines.append(f"• `{f.get('machine')}`" + (f"  — {bits}" if bits else ""))
    if len(found) > 60:
        lines.append(f"… and {len(found) - 60} more")

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": "🧪 Confirm scheduled Set Maintenance / Test"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)[:4000]}},
            {"tag": "div", "text": {"tag": "lark_md",
             "content": "_All machines were found. Confirm to schedule._"}},
            {"tag": "column_set", "flex_mode": "bisect", "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "Confirm"},
                    "type": "primary",
                    "behaviors": [{"type": "callback",
                                   "value": {"k": SST_CARD_KEY, "a": "schedule", "s": sid}}],
                }]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "Cancel"},
                    "type": "danger",
                    "behaviors": [{"type": "callback",
                                   "value": {"k": SST_CARD_KEY, "a": "cancel", "s": sid}}],
                }]},
            ]},
        ]},
    }


# ---------------------------------------------------------------------------
# fire-time cards + scheduling
# ---------------------------------------------------------------------------
def _action_words(maint: bool, test: bool) -> str:
    """``maintenance`` / ``test`` / ``maintenance and test`` — for the 'Now will start set …' line."""
    if maint and test:
        return "maintenance and test"
    if maint:
        return "maintenance"
    return "test"


def _details_md(session: dict[str, Any], found: list[dict], when: datetime) -> str:
    maint, test = bool(session.get("maint")), bool(session.get("test"))
    lines = [
        f"**When:** {when.strftime('%Y-%m-%d %I:%M%p')}",
        f"**Action:** Set {_action_words(maint, test)}",
        f"**Machines ({len(found)}):**",
    ]
    for f in found[:60]:
        lines.append(f"• `{f.get('machine')}`")
    if len(found) > 60:
        lines.append(f"… and {len(found) - 60} more")
    return "\n".join(lines)


def build_start_card(session: dict[str, Any], found: list[dict], when: datetime) -> dict:
    """Posted at the scheduled time, just before the batch runs."""
    maint, test = bool(session.get("maint")), bool(session.get("test"))
    body = (
        f"{_details_md(session, found, when)}\n\n"
        f"**Kindly monitor any issue happened.**"
    )
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text",
                      "content": f"▶️ Now will start set {_action_words(maint, test)}"},
        },
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body[:4000]}},
        ]},
    }


def build_reminder_card(session: dict[str, Any], found: list[dict], when: datetime) -> dict:
    """Posted ``SST_REMINDER_LEAD_MIN`` minutes before the scheduled time."""
    maint, test = bool(session.get("maint")), bool(session.get("test"))
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "yellow",
            "title": {"tag": "plain_text",
                      "content": f"⏰ In {SST_REMINDER_LEAD_MIN} min — set {_action_words(maint, test)}"},
        },
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": _details_md(session, found, when)[:4000]}},
        ]},
    }


def schedule_session(
    sid: str,
    session: dict[str, Any],
    found: list[dict],
    when: datetime,
    *,
    scheduler: Any,
    send_card: Callable[[str, dict], Any],
    run_batch: Callable[[str, str, list[dict]], Any],
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """
    Register both jobs: the lead-time reminder and the action itself.

    ``send_card(chat_id, card)`` posts an interactive card; ``run_batch(chat_id, action, machines)``
    starts the prod-batch job. Returns ``(ok, message)``.
    """
    action = selection_action(bool(session.get("maint")), bool(session.get("test")))
    if not action:
        return False, selection_text(False, False)
    chat_id = SST_ALLOWED_CHAT_ID
    now = now or datetime.now()
    if when <= now:
        return False, f"⚠️ {when.strftime('%Y-%m-%d %I:%M%p')} is already in the past — pick a future time."

    machines = [{k: v for k, v in m.items() if k != "token"} for m in found]

    def _fire_reminder() -> None:
        try:
            send_card(chat_id, build_reminder_card(session, found, when))
        except Exception as e:  # noqa: BLE001
            print(f"[sst] reminder card failed: {e!r}", flush=True)

    def _fire_action() -> None:
        try:
            send_card(chat_id, build_start_card(session, found, when))
        except Exception as e:  # noqa: BLE001
            print(f"[sst] start card failed: {e!r}", flush=True)
        try:
            run_batch(chat_id, action, machines)
        except Exception as e:  # noqa: BLE001
            print(f"[sst] scheduled batch failed to start: {e!r}", flush=True)

    remind_at = when - timedelta(minutes=SST_REMINDER_LEAD_MIN)
    if remind_at > now:
        scheduler.add_job(func=_fire_reminder, trigger="date", run_date=remind_at,
                          id=f"sst_remind_{sid}", replace_existing=True)
    scheduler.add_job(func=_fire_action, trigger="date", run_date=when,
                      id=f"sst_run_{sid}", replace_existing=True)

    remind_txt = (f" · reminder {remind_at.strftime('%I:%M%p')}"
                  if remind_at > now else " · reminder skipped (less than "
                                           f"{SST_REMINDER_LEAD_MIN} min away)")
    return True, (
        f"✅ Scheduled **Set {_action_words(bool(session.get('maint')), bool(session.get('test')))}** "
        f"on **{len(machines)}** machine(s) at **{when.strftime('%Y-%m-%d %I:%M%p')}**{remind_txt}."
    )


# ---------------------------------------------------------------------------
# card-callback handling (answers inside Lark's 3 s window)
# ---------------------------------------------------------------------------
def _toast(kind: str, content: str) -> dict:
    return {"toast": {"type": kind, "content": content[:180]}}


def _card_reply(card: dict) -> dict:
    return {"card": {"type": "raw", "data": card}}


def handle_card_callback(
    parsed: dict[str, Any],
    *,
    chat_id: str,
    form_value: dict[str, Any] | None,
    scheduler: Any,
    send_card: Callable[[str, dict], Any],
    send_text: Callable[[str, str], Any],
    run_batch: Callable[[str, str, list[dict]], Any],
) -> dict[str, Any] | None:
    """
    Handle every ``/sst`` button. Returns the synchronous card.callback body, or ``None`` when the
    callback isn't ours.
    """
    if str(parsed.get("k") or "").strip().lower() != SST_CARD_KEY:
        return None
    act = str(parsed.get("a") or "").strip().lower()
    sid = str(parsed.get("s") or "").strip()

    if not chat_allowed(chat_id):
        return _toast("error", "/sst is only available in the designated group.")

    session = get_session(sid)
    if not session:
        return _toast("error", "This /sst form expired. Send /sst again.")

    # Carry whatever the user has typed/picked so far into the session, so a toggle never
    # discards the machine list or the date/time.
    fv = form_value if isinstance(form_value, dict) else {}
    patch: dict[str, Any] = {}
    if fv.get("sst_date"):
        patch["date"] = str(fv.get("sst_date"))
    if fv.get("sst_time"):
        patch["time"] = str(fv.get("sst_time"))
    if fv.get("sst_machines") is not None:
        patch["machines_text"] = str(fv.get("sst_machines") or "")
    if patch:
        session = update_session(sid, **patch) or session

    if act == "cancel":
        update_session(sid, maint=False, test=False)
        return _card_reply({
            "schema": "2.0",
            "config": {"update_multi": True, "width_mode": "fill"},
            "header": {"template": "grey",
                       "title": {"tag": "plain_text", "content": "🚫 Cancelled"}},
            "body": {"elements": [{"tag": "div", "text": {
                "tag": "lark_md", "content": "Scheduled set maintenance/test was **cancelled**. "
                                             "Send `/sst` to start again."}}]},
        })

    if act == "toggle":
        which = str(parsed.get("w") or "").strip().lower()
        if which in ("maint", "test"):
            session = update_session(sid, **{which: not bool(session.get(which))}) or session
        return _card_reply(build_form_card(sid, session))

    if act == "confirm":
        maint, test = bool(session.get("maint")), bool(session.get("test"))
        if not (maint or test):
            return _toast("error", "Kindly select Set Maintenance or Test")
        when = parse_when(str(session.get("date") or ""), str(session.get("time") or ""))
        if when is None:
            return _toast("error", "Kindly pick both the date and the time.")
        tokens = parse_machine_lines(str(session.get("machines_text") or ""))
        if not tokens:
            return _toast("error", "Kindly type at least one machine (one per line).")

        found, missing, ambiguous = resolve_machines(tokens)
        if missing:
            return _toast("error", f"{missing[0]} is not detected. Try again.")
        if ambiguous:
            names = ", ".join(c["machine"] for c in ambiguous[0]["candidates"][:4])
            return _toast("error", f"{ambiguous[0]['token']} matches several machines ({names}). Be more specific.")
        if not found:
            return _toast("error", "No machines resolved. Try again.")
        return _card_reply(build_review_card(sid, session, found, when))

    if act == "schedule":
        when = parse_when(str(session.get("date") or ""), str(session.get("time") or ""))
        if when is None:
            return _toast("error", "Kindly pick both the date and the time.")
        tokens = parse_machine_lines(str(session.get("machines_text") or ""))
        found, missing, _amb = resolve_machines(tokens)
        if missing:
            return _toast("error", f"{missing[0]} is not detected. Try again.")
        ok, msg = schedule_session(
            sid, session, found, when,
            scheduler=scheduler, send_card=send_card, run_batch=run_batch,
        )
        if not ok:
            return _toast("error", msg)
        return _card_reply({
            "schema": "2.0",
            "config": {"update_multi": True, "width_mode": "fill"},
            "header": {"template": "green",
                       "title": {"tag": "plain_text", "content": "✅ Scheduled"}},
            "body": {"elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": msg}},
                {"tag": "div", "text": {"tag": "lark_md",
                 "content": _details_md(session, found, when)[:3000]}},
            ]},
        })

    return _toast("error", f"Unknown /sst action: {act}")
