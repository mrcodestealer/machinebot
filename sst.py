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
            # target selection: "" (not chosen) | "game" | "machines"
            "mode": "",
            "env_code": "",
            "game_type": "",
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
# environment / game-type catalogue (from webmachine_data.json)
# ---------------------------------------------------------------------------
SST_ENV_CODES: tuple[str, ...] = ("NWR", "NCH", "TBR", "TBP", "MDR", "DHS", "CP", "WF")


def _norm_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _prod_rows() -> list[dict]:
    from maintenancemachineagent import load_webmachine_rows

    return [r for r in load_webmachine_rows()
            if str(r.get("environment") or "PROD").strip().upper() == "PROD"]


def _env_rows(env_code: str) -> list[dict]:
    """Rows for one environment (handles NWR being stored as ``belongs=NP``)."""
    from maintenancemachineagent import _row_matches_env

    return [r for r in _prod_rows() if _row_matches_env(r, (env_code or "").strip().upper())]


def list_game_types(env_code: str) -> list[tuple[str, int]]:
    """``[(game_type, machine_count)]`` for one environment, most machines first."""
    counts: dict[str, int] = {}
    for r in _env_rows(env_code):
        gt = str(r.get("game_type") or "").strip()
        if gt:
            counts[gt] = counts.get(gt, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))


def _row_to_machine(row: dict) -> dict:
    return {
        "belongs": str(row.get("belongs") or "").strip(),
        "machine": str(row.get("name") or row.get("machine") or "").strip(),
        "status": str(row.get("status") or "").strip(),
        "online": str(row.get("online") or "").strip(),
        "is_test": bool(row.get("is_test")),
    }


def machines_for_game_type(env_code: str, game_type: str) -> list[dict]:
    """
    Machines of one environment + game type.

    Matched on a normalised key so ``Rising Rockets`` finds ``RISINGROCKETS``. Unlike the
    free-text group flow this never falls back to "all machines in the environment" — an
    unmatched game type returns an empty list so the caller reports it instead of widening.
    """
    want = _norm_key(game_type)
    if not want:
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for r in _env_rows(env_code):
        if _norm_key(str(r.get("game_type") or "")) != want:
            continue
        m = _row_to_machine(r)
        key = (m["belongs"].upper(), m["machine"])
        if not m["machine"] or key in seen:
            continue
        seen.add(key)
        out.append(m)
    return sorted(out, key=lambda x: x["machine"].lower())


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
    #
    # ``form_action_type: submit`` matters: a plain button does not carry the form's current values,
    # so toggling used to re-render the card with an empty date/time/machine box. Submitting hands
    # us ``form_value``, which we fold back into the session before re-rendering. The fields are
    # deliberately NOT ``required`` — otherwise Lark would block the toggle until the whole form is
    # filled; the real validation happens on Confirm instead.
    return {
        "tag": "button",
        "name": f"sst_toggle_{which}",
        "text": {"tag": "plain_text", "content": ("✅ " if on else "") + label},
        "type": "primary" if on else "default",
        "form_action_type": "submit",
        "behaviors": [{"type": "callback", "value": {"k": SST_CARD_KEY, "a": "toggle",
                                                     "s": sid, "w": which}}],
    }


def _form_button(label: str, name: str, value: dict, *, kind: str = "default") -> dict:
    """A form button that submits (so the form's current values reach us) and calls back."""
    return {
        "tag": "button",
        "name": name,
        "text": {"tag": "plain_text", "content": label[:60]},
        "type": kind,
        "form_action_type": "submit",
        "behaviors": [{"type": "callback", "value": {"k": SST_CARD_KEY, **value}}],
    }


def _btn_row(buttons: list[dict]) -> dict:
    """
    One row of buttons.

    ``flex_mode`` must NOT be ``bisect`` here — bisect means *exactly two* equal columns, so a row
    of 3 (Confirm/Back/Cancel) or 4 (environments) made Lark reject the card. ``none`` + weighted
    columns lays out any count, and is the pattern the prod-batch confirm card already uses.
    """
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [b]}
            for b in buttons
        ],
    }


def _back_row(sid: str) -> dict:
    return _btn_row([
        _form_button("◀ Change target", "sst_back", {"a": "back", "s": sid}),
        {
            "tag": "button",
            "name": "sst_cancel",
            "text": {"tag": "plain_text", "content": "Cancel"},
            "type": "danger",
            "behaviors": [{"type": "callback",
                           "value": {"k": SST_CARD_KEY, "a": "cancel", "s": sid}}],
        },
    ])


def _confirm_row(sid: str, *, with_back: bool = False) -> dict:
    buttons = [_form_button("Confirm", "sst_confirm", {"a": "confirm", "s": sid}, kind="primary")]
    if with_back:
        buttons.append(_form_button("◀ Change target", "sst_back", {"a": "back", "s": sid}))
    buttons.append({
        "tag": "button",
        "name": "sst_cancel",
        "text": {"tag": "plain_text", "content": "Cancel"},
        "type": "danger",
        "behaviors": [{"type": "callback",
                       "value": {"k": SST_CARD_KEY, "a": "cancel", "s": sid}}],
    })
    return _btn_row(buttons)


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
        "required": False,
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
        # Not ``required``: the toggle buttons submit this form, and Lark would refuse the submit
        # (blocking the toggle) while the box is still empty. Confirm validates it instead.
        "required": False,
        # Lark hard-caps form input max_length at 1000 — anything larger is rejected with
        # "max_length exceed the default maximum 1000" and the whole card fails to render.
        "max_length": 1000,
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
            "required": False,
            "initial_index": _initial_time_index(time_v),
        },
        {"tag": "div", "text": {"tag": "lark_md", "content": "**What to set** — tap to select:"}},
        _btn_row([
            _toggle_button("Maintenance", on=maint, sid=sid, which="maint"),
            _toggle_button("Test", on=test, sid=sid, which="test"),
        ]),
        {"tag": "div", "text": {"tag": "lark_md", "content": selection_text(maint, test)}},
    ]

    # ---- target section: Game Type wizard or the machines textarea -------------------
    mode = str(session.get("mode") or "")
    env_code = str(session.get("env_code") or "").strip().upper()
    game_type = str(session.get("game_type") or "").strip()
    hint = "Pick **date** and **time**, choose **Maintenance** / **Test**, then choose a target."

    if not mode:
        form_elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**Target** — choose one:"}},
            _btn_row([
                _form_button("Game Type", "sst_mode_game", {"a": "mode", "s": sid, "m": "game"},
                             kind="primary"),
                _form_button("Machines", "sst_mode_machines", {"a": "mode", "s": sid, "m": "machines"},
                             kind="primary"),
            ]),
        ]
    elif mode == "machines":
        hint = "Paste the machines (one per line), then tap **Confirm**."
        form_elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**Target:** Machines"}},
            machines_el,
            _confirm_row(sid, with_back=True),
        ]
    elif not env_code:
        hint = "Select the **environment** for the game type."
        rows = [SST_ENV_CODES[i:i + 4] for i in range(0, len(SST_ENV_CODES), 4)]
        form_elements.append({"tag": "div", "text": {"tag": "lark_md",
                              "content": "**Target:** Game Type — select environment:"}})
        for chunk in rows:
            form_elements.append(_btn_row([
                _form_button(code, f"sst_env_{code}", {"a": "env", "s": sid, "e": code})
                for code in chunk
            ]))
        form_elements.append(_back_row(sid))
    elif not game_type:
        hint = f"Select the **game type** in **{env_code}**."
        gts = list_game_types(env_code)
        form_elements.append({"tag": "div", "text": {"tag": "lark_md",
                              "content": f"**Environment:** {env_code} — select game type:"}})
        if not gts:
            form_elements.append({"tag": "div", "text": {"tag": "lark_md",
                                  "content": f"_No game types found for {env_code}._"}})
        for i in range(0, len(gts[:30]), 2):
            chunk = gts[i:i + 2]
            form_elements.append(_btn_row([
                _form_button(f"{gt} ({n})", f"sst_gt_{i + j}",
                             {"a": "gt", "s": sid, "g": gt})
                for j, (gt, n) in enumerate(chunk)
            ]))
        form_elements.append(_back_row(sid))
    else:
        machines = machines_for_game_type(env_code, game_type)
        hint = "Review the machines, then tap **Confirm**."
        names = "\n".join(f"• `{m['machine']}`" for m in machines[:25])
        more = f"\n… and {len(machines) - 25} more" if len(machines) > 25 else ""
        body = (f"**Environment:** {env_code}\n**Game type:** {game_type}\n"
                f"**Machines:** {len(machines)}\n\n{names}{more}")
        if not machines:
            body = (f"**Environment:** {env_code}\n**Game type:** {game_type}\n\n"
                    f"⚠️ No machines found for this game type.")
        form_elements.append({"tag": "div", "text": {"tag": "lark_md", "content": body[:2500]}})
        form_elements.append(_confirm_row(sid, with_back=True))

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": "🧪 Scheduled Set Maintenance / Test"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": hint}},
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
def normalize_date_value(raw: Any) -> str:
    """
    Lark's ``date_picker`` returns a **millisecond timestamp**, not ``YYYY-MM-DD``.

    Storing it raw broke two things: ``initial_date`` rejected it (so the picker reset to empty on
    every re-render) and ``parse_when`` could not read it. Normalise to ``YYYY-MM-DD`` here.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if re.fullmatch(r"\d{10,13}", s):
        ts = int(s)
        if ts > 10 ** 11:  # milliseconds
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m2 = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m2:
        return f"{m2.group(3)}-{int(m2.group(2)):02d}-{int(m2.group(1)):02d}"
    return ""


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
    ]
    if session.get("game_type"):
        lines.append(f"**Game type:** {session.get('env_code')} · {session.get('game_type')}")
    lines += [
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
            {"tag": "column_set", "flex_mode": "none", "columns": [
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


# ---------------------------------------------------------------------------
# persistence — survives a service restart / redeploy
# ---------------------------------------------------------------------------
from pathlib import Path  # noqa: E402

_ROOT_DIR = Path(__file__).resolve().parent
STORE_PATH = _ROOT_DIR / (_os.environ.get("SST_STORE_FILE") or "scheduledSetMachine.json")

# A schedule missed while the bot was down still runs if it's this fresh; older ones are
# reported as missed instead of firing hours late.
SST_MISFIRE_GRACE_MIN = 10

_STORE_LOCK = threading.Lock()


def _store_read() -> list[dict]:
    try:
        raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = raw.get("schedules") if isinstance(raw, dict) else raw
    return [x for x in (items or []) if isinstance(x, dict)]


def _store_write(items: list[dict]) -> None:
    """Atomic write so a concurrent reader never sees a truncated file."""
    tmp = STORE_PATH.with_suffix(".tmp")
    payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), "schedules": items}
    try:
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STORE_PATH)
    except OSError as e:
        print(f"[sst] could not persist {STORE_PATH.name}: {e!r}", flush=True)


def store_list() -> list[dict]:
    with _STORE_LOCK:
        items = _store_read()
    return sorted(items, key=lambda x: str(x.get("when") or ""))


def store_add(entry: dict) -> None:
    with _STORE_LOCK:
        items = [x for x in _store_read() if str(x.get("id")) != str(entry.get("id"))]
        items.append(entry)
        _store_write(items)


def store_remove(sched_id: str) -> dict | None:
    with _STORE_LOCK:
        items = _store_read()
        keep, gone = [], None
        for x in items:
            if str(x.get("id")) == str(sched_id) and gone is None:
                gone = x
            else:
                keep.append(x)
        if gone is not None:
            _store_write(keep)
        return gone


def _entry_when(entry: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(entry.get("when")))
    except (TypeError, ValueError):
        return None


def _entry_session(entry: dict) -> dict[str, Any]:
    """Minimal session-shaped dict so the card builders work from a stored entry."""
    return {"maint": bool(entry.get("maint")), "test": bool(entry.get("test"))}


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------
def cancel_jobs(scheduler: Any, sched_id: str) -> None:
    for jid in (f"sst_remind_{sched_id}", f"sst_run_{sched_id}"):
        try:
            scheduler.remove_job(jid)
        except Exception:
            pass


def register_entry(
    entry: dict,
    *,
    scheduler: Any,
    send_card: Callable[[str, dict], Any],
    run_batch: Callable[..., Any],
    now: Optional[datetime] = None,
    catch_up: bool = False,
) -> tuple[bool, str]:
    """
    Register the reminder + action jobs for one stored entry.

    ``send_card(chat_id, card)`` must return the posted card's ``message_id`` (or ""), which becomes
    the thread root so the batch's own progress messages and screenshots land **inside** the
    "Now will start …" card's thread. ``run_batch(chat_id, action, machines, thread_root=…)``.
    """
    sched_id = str(entry.get("id") or "")
    when = _entry_when(entry)
    action = selection_action(bool(entry.get("maint")), bool(entry.get("test")))
    chat_id = str(entry.get("chat_id") or SST_ALLOWED_CHAT_ID)
    machines = [m for m in (entry.get("machines") or []) if isinstance(m, dict)]
    if not (sched_id and when and action and machines):
        return False, "invalid schedule entry"
    now = now or datetime.now()

    session = _entry_session(entry)
    found = machines

    def _fire_reminder() -> None:
        try:
            send_card(chat_id, build_reminder_card(session, found, when))
        except Exception as e:  # noqa: BLE001
            print(f"[sst] reminder card failed: {e!r}", flush=True)

    def _fire_action() -> None:
        root = ""
        try:
            root = str(send_card(chat_id, build_start_card(session, found, when)) or "")
        except Exception as e:  # noqa: BLE001
            print(f"[sst] start card failed: {e!r}", flush=True)
        # The schedule has fired — drop it so a later restart can't replay it.
        store_remove(sched_id)
        try:
            run_batch(chat_id, action, machines, thread_root=root or None)
        except Exception as e:  # noqa: BLE001
            print(f"[sst] scheduled batch failed to start: {e!r}", flush=True)

    if when <= now:
        if not catch_up:
            return False, f"⚠️ {when.strftime('%Y-%m-%d %I:%M%p')} is already in the past — pick a future time."
        late_min = (now - when).total_seconds() / 60.0
        if late_min > SST_MISFIRE_GRACE_MIN:
            return False, (f"missed by {int(late_min)} min (grace {SST_MISFIRE_GRACE_MIN} min) — not run")
        scheduler.add_job(func=_fire_action, trigger="date",
                          run_date=now + timedelta(seconds=5),
                          id=f"sst_run_{sched_id}", replace_existing=True)
        return True, f"catching up (was due {int(late_min)} min ago)"

    remind_at = when - timedelta(minutes=SST_REMINDER_LEAD_MIN)
    if remind_at > now:
        scheduler.add_job(func=_fire_reminder, trigger="date", run_date=remind_at,
                          id=f"sst_remind_{sched_id}", replace_existing=True)
    scheduler.add_job(func=_fire_action, trigger="date", run_date=when,
                      id=f"sst_run_{sched_id}", replace_existing=True)
    return True, ("reminder " + remind_at.strftime("%I:%M%p")) if remind_at > now else "reminder skipped"


def schedule_session(
    sid: str,
    session: dict[str, Any],
    found: list[dict],
    when: datetime,
    *,
    scheduler: Any,
    send_card: Callable[[str, dict], Any],
    run_batch: Callable[..., Any],
    now: Optional[datetime] = None,
    created_by: str = "",
) -> tuple[bool, str]:
    """Persist the schedule to ``scheduledSetMachine.json`` and register its jobs."""
    action = selection_action(bool(session.get("maint")), bool(session.get("test")))
    if not action:
        return False, selection_text(False, False)
    now = now or datetime.now()
    # Round to the minute FIRST, then validate — the stored value is what gets registered, so
    # checking the unrounded time could accept a schedule that is past-due once persisted.
    when = when.replace(second=0, microsecond=0)
    if when <= now:
        return False, f"⚠️ {when.strftime('%Y-%m-%d %I:%M%p')} is already in the past — pick a future time."

    machines = [{k: v for k, v in m.items() if k != "token"} for m in found]
    entry = {
        "id": sid,
        "chat_id": SST_ALLOWED_CHAT_ID,
        "when": when.isoformat(timespec="seconds"),
        "maint": bool(session.get("maint")),
        "test": bool(session.get("test")),
        "action": action,
        "machines": machines,
        "created": now.isoformat(timespec="seconds"),
        "created_by": created_by,
    }
    store_add(entry)
    ok, note = register_entry(
        entry, scheduler=scheduler, send_card=send_card, run_batch=run_batch, now=now
    )
    if not ok:
        store_remove(sid)
        return False, note

    remind_at = when - timedelta(minutes=SST_REMINDER_LEAD_MIN)
    remind_txt = (f" · reminder {remind_at.strftime('%I:%M%p')}" if remind_at > now
                  else f" · reminder skipped (less than {SST_REMINDER_LEAD_MIN} min away)")
    return True, (
        f"✅ Scheduled **Set {_action_words(bool(session.get('maint')), bool(session.get('test')))}** "
        f"on **{len(machines)}** machine(s) at **{when.strftime('%Y-%m-%d %I:%M%p')}**{remind_txt}."
    )


def restore_schedules(
    *,
    scheduler: Any,
    send_card: Callable[[str, dict], Any],
    run_batch: Callable[..., Any],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """
    Re-register every persisted schedule at boot (called from main's startup).

    Entries whose time passed while the bot was down run immediately when within
    ``SST_MISFIRE_GRACE_MIN``; older ones are dropped and reported so nobody assumes they ran.
    """
    now = now or datetime.now()
    restored: list[str] = []
    missed: list[dict] = []
    for entry in store_list():
        ok, note = register_entry(
            entry, scheduler=scheduler, send_card=send_card, run_batch=run_batch,
            now=now, catch_up=True,
        )
        label = f"{entry.get('when')} · {entry.get('action')} · {len(entry.get('machines') or [])} machine(s)"
        if ok:
            restored.append(f"{label} ({note})")
        else:
            missed.append({"entry": entry, "note": note})
            store_remove(str(entry.get("id") or ""))
    print(f"[sst] restore: {len(restored)} re-armed, {len(missed)} missed", flush=True)
    for r in restored:
        print(f"[sst]   re-armed {r}", flush=True)
    for m in missed:
        print(f"[sst]   MISSED {m['entry'].get('when')} — {m['note']}", flush=True)
    if missed:
        try:
            send_card(SST_ALLOWED_CHAT_ID, build_missed_card(missed))
        except Exception as e:  # noqa: BLE001
            print(f"[sst] missed-card failed: {e!r}", flush=True)
    return {"restored": restored, "missed": missed}


def build_missed_card(missed: list[dict]) -> dict:
    lines = ["These scheduled set maintenance/test runs were **missed** while the bot was "
             "restarting and were **NOT executed**:", ""]
    for m in missed[:20]:
        e = m["entry"]
        w = _entry_when(e)
        when_txt = w.strftime("%Y-%m-%d %I:%M%p") if w else str(e.get("when"))
        lines.append(
            f"• **{when_txt}** — Set {_action_words(bool(e.get('maint')), bool(e.get('test')))} "
            f"on {len(e.get('machines') or [])} machine(s) — _{m['note']}_"
        )
    lines.append("")
    lines.append("Re-schedule with `/sst` if these still need to run.")
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "red",
                   "title": {"tag": "plain_text", "content": "⚠️ Missed scheduled set maintenance/test"}},
        "body": {"elements": [{"tag": "div", "text": {"tag": "lark_md",
                               "content": "\n".join(lines)[:4000]}}]},
    }


# ---------------------------------------------------------------------------
# /sstlist — pending schedules, each with a Delete button
# ---------------------------------------------------------------------------
def build_list_card() -> dict:
    items = store_list()
    if not items:
        return {
            "schema": "2.0",
            "config": {"update_multi": True, "width_mode": "fill"},
            "header": {"template": "grey",
                       "title": {"tag": "plain_text", "content": "🗓 Scheduled Set Maintenance / Test"}},
            "body": {"elements": [{"tag": "div", "text": {"tag": "lark_md",
                                   "content": "No scheduled set maintenance/test.\nSend `/sst` to add one."}}]},
        }

    elements: list[dict] = []
    for e in items:
        w = _entry_when(e)
        when_txt = w.strftime("%Y-%m-%d %I:%M%p") if w else str(e.get("when"))
        machines = [m for m in (e.get("machines") or []) if isinstance(m, dict)]
        names = ", ".join(str(m.get("machine") or "") for m in machines[:6])
        if len(machines) > 6:
            names += f", … (+{len(machines) - 6})"
        body = (
            f"**{when_txt}**\n"
            f"Set {_action_words(bool(e.get('maint')), bool(e.get('test')))} · "
            f"**{len(machines)}** machine(s)\n"
            f"`{names}`"
        )
        if elements:  # separator between entries only — no leading rule
            elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": body[:1500]},
            "extra": {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Delete"},
                "type": "danger",
                "behaviors": [{"type": "callback",
                               "value": {"k": SST_CARD_KEY, "a": "del", "i": str(e.get("id") or "")}}],
            },
        })

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "blue",
                   "title": {"tag": "plain_text", "content": "🗓 Scheduled Set Maintenance / Test"}},
        "body": {"elements": elements},
    }


# ---------------------------------------------------------------------------
# card-callback handling (answers inside Lark's 3 s window)
# ---------------------------------------------------------------------------
def resolve_session_target(session: dict[str, Any]) -> tuple[list[dict], str]:
    """
    Resolve the session's target to machines. Returns ``(found, error_message)``.

    Game-type mode never widens to the whole environment — an unmatched game type is an error,
    not "all machines of that site".
    """
    mode = str(session.get("mode") or "")
    if mode == "game":
        env_code = str(session.get("env_code") or "").strip().upper()
        game_type = str(session.get("game_type") or "").strip()
        if not (env_code and game_type):
            return [], "Kindly select the environment and game type."
        found = machines_for_game_type(env_code, game_type)
        if not found:
            return [], f"{game_type} is not detected in {env_code}. Try again."
        return found, ""

    tokens = parse_machine_lines(str(session.get("machines_text") or ""))
    if not tokens:
        return [], "Kindly type at least one machine (one per line)."
    found, missing, ambiguous = resolve_machines(tokens)
    if missing:
        return [], f"{missing[0]} is not detected. Try again."
    if ambiguous:
        names = ", ".join(c["machine"] for c in ambiguous[0]["candidates"][:4])
        return [], f"{ambiguous[0]['token']} matches several machines ({names}). Be more specific."
    if not found:
        return [], "No machines resolved. Try again."
    return found, ""


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

    # Delete from /sstlist — operates on the persisted store, not a live form session.
    if act == "del":
        sched_id = str(parsed.get("i") or "").strip()
        gone = store_remove(sched_id)
        cancel_jobs(scheduler, sched_id)
        if gone is None:
            return _toast("error", "That schedule is already gone.")
        w = _entry_when(gone)
        when_txt = w.strftime("%Y-%m-%d %I:%M%p") if w else str(gone.get("when"))
        print(f"[sst] deleted schedule {sched_id} ({when_txt})", flush=True)
        return _card_reply(build_list_card())

    session = get_session(sid)
    if not session:
        return _toast("error", "This /sst form expired. Send /sst again.")

    # Carry whatever the user has typed/picked so far into the session, so a toggle never
    # discards the machine list or the date/time.
    fv = form_value if isinstance(form_value, dict) else {}
    patch: dict[str, Any] = {}
    if fv.get("sst_date"):
        norm_date = normalize_date_value(fv.get("sst_date"))
        if norm_date:
            patch["date"] = norm_date
    if fv.get("sst_time"):
        patch["time"] = str(fv.get("sst_time")).strip()
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

    if act == "mode":
        m = str(parsed.get("m") or "").strip().lower()
        if m not in ("game", "machines"):
            return _toast("error", "Unknown target type.")
        session = update_session(sid, mode=m, env_code="", game_type="") or session
        return _card_reply(build_form_card(sid, session))

    if act == "back":
        session = update_session(sid, mode="", env_code="", game_type="") or session
        return _card_reply(build_form_card(sid, session))

    if act == "env":
        code = str(parsed.get("e") or "").strip().upper()
        if code not in SST_ENV_CODES:
            return _toast("error", f"Unknown environment: {code}")
        session = update_session(sid, env_code=code, game_type="") or session
        return _card_reply(build_form_card(sid, session))

    if act == "gt":
        gt = str(parsed.get("g") or "").strip()
        if not gt:
            return _toast("error", "Unknown game type.")
        session = update_session(sid, game_type=gt) or session
        return _card_reply(build_form_card(sid, session))

    if act == "confirm":
        maint, test = bool(session.get("maint")), bool(session.get("test"))
        if not (maint or test):
            return _toast("error", "Kindly select Set Maintenance or Test")
        when = parse_when(str(session.get("date") or ""), str(session.get("time") or ""))
        if when is None:
            return _toast("error", "Kindly pick both the date and the time.")

        found, err = resolve_session_target(session)
        if err:
            return _toast("error", err)
        return _card_reply(build_review_card(sid, session, found, when))

    if act == "schedule":
        when = parse_when(str(session.get("date") or ""), str(session.get("time") or ""))
        if when is None:
            return _toast("error", "Kindly pick both the date and the time.")
        found, err = resolve_session_target(session)
        if err:
            return _toast("error", err)
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
