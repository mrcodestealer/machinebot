"""
``/setgt`` — **immediate** Set-Maintenance / Set-Test for one game type.

The sibling of :mod:`sst`, with the scheduling removed. ``/sst`` asks for a date and a time
because it parks the work on the scheduler; ``/setgt`` runs the moment the second confirmation is
tapped, so a date/time picker would be noise — and the target is **always** a game type, never a
pasted machine list.

Flow
----
1. ``@bot /setgt`` posts a form card: **Maintenance** / **Test** toggle buttons and the game-type
   wizard (environment → game type → venue, when the game type spans two venues).
2. Every button updates the card in place inside Lark's 3 s card-callback window.
3. **Confirm** resolves the game type against ``webmachine_data.json`` and shows a **review card**
   listing every machine that will be changed.
4. Confirming the review card posts the "Now will start set …" card and fires the prod-batch job
   immediately — nothing is persisted, because there is no future run to survive a restart.

The environment / game-type / venue catalogue, the machine renderer and the access gate are all
reused from :mod:`sst` so the two commands can never drift apart on what a game type means or on
who is allowed to drive a PROD change.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

import sst as _sst

SETGT_CARD_KEY = "setgt"

# One source of truth with /sst: same catalogue, same venue split, same renderer.
SETGT_ENV_CODES = _sst.SST_ENV_CODES
SETGT_VENUE_ALL = _sst.SST_VENUE_ALL

list_game_types = _sst.list_game_types
machines_for_game_type = _sst.machines_for_game_type
venues_for_game_type = _sst.venues_for_game_type
machine_lines = _sst.machine_lines
selection_text = _sst.selection_text
selection_action = _sst.selection_action


def chat_allowed(chat_id: str) -> bool:
    """Same designated groups as ``/sst`` — this runs the same PROD change, only sooner."""
    return _sst.chat_allowed(chat_id)


def pm_allowed(sender_id: str) -> bool:
    return _sst.pm_allowed(sender_id)


def access_allowed(chat_id: str, *, sender_id: str = "", is_pm: bool = False) -> bool:
    """See :func:`sst.access_allowed` — the gate is deliberately identical."""
    return _sst.access_allowed(chat_id, sender_id=sender_id, is_pm=is_pm)


# ---------------------------------------------------------------------------
# session store
# ---------------------------------------------------------------------------
# Kept separate from sst's store even though the shapes overlap: a /setgt session has no date,
# time or machines_text, and sharing the dict would let an sst-only code path read fields that
# never exist here.
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSIONS_LOCK = threading.Lock()
_SESSION_TTL_SEC = 7200


def _cleanup_sessions() -> None:
    now = time.time()
    with _SESSIONS_LOCK:
        for sid in [k for k, v in _SESSIONS.items()
                    if now - float(v.get("ts") or 0) > _SESSION_TTL_SEC]:
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
            "env_code": "",
            "game_type": "",
            # venue inside a split environment: "" (not chosen) | "NCH" | "DYB" | "ALL"
            "venue": "",
            # the exact machine list the operator approved on the review card, and the action
            # that went with it — frozen at Confirm so the run cannot drift (see act == "confirm")
            "approved": [],
            "approved_action": "",
            # set once the run has been fired, so a double-tap on Confirm cannot run it twice
            "fired": False,
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


def claim_run(sid: str) -> bool:
    """
    Flip ``fired`` under the lock; ``True`` only for the first caller.

    Lark re-delivers a card callback when the first response is slow, and the Confirm button stays
    tappable until the card re-renders — without this latch an impatient double-tap would start two
    identical prod-batch jobs against the same cabinets.
    """
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(sid)
        if not s or s.get("fired"):
            return False
        s["fired"] = True
        s["ts"] = time.time()
        return True


def release_run(sid: str) -> None:
    """Undo :func:`claim_run` when the job never actually started, so it can be retried."""
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(sid)
        if s:
            s["fired"] = False
            s["ts"] = time.time()


def claim_cancel(sid: str) -> bool:
    """
    ``True`` only while the run has **not** been claimed — checked under the same lock as
    :func:`claim_run` so a Cancel racing a Confirm cannot render "Cancelled" over a firing batch.
    """
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(sid)
        if not s or s.get("fired"):
            return False
        s["maint"] = False
        s["test"] = False
        s["ts"] = time.time()
        return True


# ---------------------------------------------------------------------------
# card widgets
# ---------------------------------------------------------------------------
# Unlike /sst this card has no text inputs or pickers, so it needs no ``form`` container — and
# without one the buttons must NOT carry ``form_action_type`` (Lark rejects it outside a form).
# Every piece of state is button-driven and lives in the session, so nothing is lost by it.
def _button(label: str, value: dict, *, kind: str = "default") -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label[:60]},
        "type": kind,
        "behaviors": [{"type": "callback", "value": {"k": SETGT_CARD_KEY, **value}}],
    }


def _toggle_button(label: str, *, on: bool, sid: str, which: str) -> dict:
    return _button(("✅ " if on else "") + label,
                   {"a": "toggle", "s": sid, "w": which},
                   kind="primary" if on else "default")


def _btn_row(buttons: list[dict]) -> dict:
    """One row of buttons — ``flex_mode: none`` lays out any count (see :func:`sst._btn_row`)."""
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [b]}
            for b in buttons
        ],
    }


def _btn_rows(buttons: list[dict], per_row: int = 2) -> list[dict]:
    return [_btn_row(buttons[i:i + per_row]) for i in range(0, len(buttons), per_row)]


def _cancel_button(sid: str) -> dict:
    return _button("Cancel", {"a": "cancel", "s": sid}, kind="danger")


def _back_row(sid: str) -> dict:
    return _btn_row([
        _button("◀ Back", {"a": "back", "s": sid}),
        _cancel_button(sid),
    ])


def build_form_card(sid: str, session: dict[str, Any], *, error: str = "") -> dict:
    """
    The ``/setgt`` form card. ``error`` renders a banner above the form.

    A banner rather than a toast: :func:`_toast` truncates at 180 characters, which would eat the
    machine detail an operator needs to see why a game type resolved to nothing.
    """
    maint = bool(session.get("maint"))
    test = bool(session.get("test"))
    env_code = str(session.get("env_code") or "").strip().upper()
    game_type = str(session.get("game_type") or "").strip()
    venue = str(session.get("venue") or "").strip().upper()

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "**What to set** — tap to select:"}},
        _btn_row([
            _toggle_button("Maintenance", on=maint, sid=sid, which="maint"),
            _toggle_button("Test", on=test, sid=sid, which="test"),
        ]),
        {"tag": "div", "text": {"tag": "lark_md", "content": selection_text(maint, test)}},
    ]

    # Loaded once and reused by the venue step and the review list: each call re-reads
    # webmachine_data.json from disk.
    game_machines = (machines_for_game_type(env_code, game_type)
                     if (env_code and game_type) else [])
    venue_choices = venues_for_game_type(env_code, game_type, game_machines)

    if not env_code:
        hint = "Choose **Maintenance** / **Test**, then select the **environment**."
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": "**Target:** Game Type — select environment:"}})
        elements += _btn_rows([
            _button(code, {"a": "env", "s": sid, "e": code}) for code in SETGT_ENV_CODES
        ])
        elements.append(_btn_row([_cancel_button(sid)]))
    elif not game_type:
        hint = f"Select the **game type** in **{env_code}**."
        gts = list_game_types(env_code)
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**Environment:** {env_code} — select game type:"}})
        if not gts:
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                             "content": f"_No game types found for {env_code}._"}})
        # Capped at 30 like /sst: every button is elements against Lark's 200-element card cap.
        shown = gts[:30]
        for i in range(0, len(shown), 2):
            elements.append(_btn_row([
                _button(f"{gt} ({n})", {"a": "gt", "s": sid, "g": gt})
                for gt, n in shown[i:i + 2]
            ]))
        if len(gts) > len(shown):
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                             "content": f"_… {len(gts) - len(shown)} more game types not shown._"}})
        elements.append(_back_row(sid))
    elif venue_choices and not venue:
        # Only reached when the game type really does span both venues — venues_for_game_type
        # returns [] otherwise, so single-venue game types go straight to the review list.
        names = " + ".join(v for v, _ in venue_choices)
        total = sum(n for _, n in venue_choices)
        hint = f"**{game_type}** has machines at more than one venue — pick which to set."
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**Environment:** {env_code}\n**Game type:** {game_type}\n\n"
                                    f"Select the **venue**:"}})
        elements += _btn_rows(
            [_button(f"{v} ({n})", {"a": "venue", "s": sid, "v": v}, kind="primary")
             for v, n in venue_choices]
            + [_button(f"{names} ({total})", {"a": "venue", "s": sid, "v": SETGT_VENUE_ALL})]
        )
        elements.append(_back_row(sid))
    else:
        machines = _sst._filter_by_venue(env_code, game_machines, venue)
        hint = "Review the machines, then tap **Confirm** — this runs **now**."
        head = f"**Environment:** {env_code}\n**Game type:** {game_type}\n"
        if venue:
            head += f"**Venue:** {_sst._venue_label(env_code, venue)}\n"
        body = f"{head}**Machines:** {len(machines)}\n\n{machine_lines(machines)}"
        if not machines:
            body = f"{head}\n⚠️ No machines found for this game type."
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": body}})
        elements += _btn_rows([
            _button("Confirm", {"a": "confirm", "s": sid}, kind="primary"),
            _button("◀ Back", {"a": "back", "s": sid}),
            _cancel_button(sid),
        ])

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text",
                             "content": "⚙️ Set Maintenance / Test — Game Type"}},
        "body": {"elements": (
            ([{"tag": "div", "text": {"tag": "lark_md", "content": error}}, {"tag": "hr"}]
             if error else [])
            + [{"tag": "div", "text": {"tag": "lark_md", "content": hint}}]
            + elements
        )},
    }


# ---------------------------------------------------------------------------
# review / run cards
# ---------------------------------------------------------------------------
def _where_line(session: dict[str, Any]) -> str:
    env_code = str(session.get("env_code") or "").strip().upper()
    venue = str(session.get("venue") or "").strip().upper()
    where = env_code
    if venue:
        where += f" ({_sst._venue_label(env_code, venue)})"
    return f"**Game type:** {where} · {session.get('game_type') or ''}"


def _details_md(session: dict[str, Any], found: list[dict], *, detail: bool = False) -> str:
    maint, test = bool(session.get("maint")), bool(session.get("test"))
    return "\n".join([
        f"**Action:** Set {_sst._action_words(maint, test)}",
        _where_line(session),
        f"**Machines ({len(found)}):**",
        machine_lines(found, detail=detail),
    ])


def build_review_card(sid: str, session: dict[str, Any], found: list[dict]) -> dict:
    """Second confirmation — the last stop before the cabinets actually change."""
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text",
                             "content": "⚙️ Confirm Set Maintenance / Test"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": _details_md(session, found, detail=True)}},
            {"tag": "div", "text": {"tag": "lark_md",
             "content": "_Confirm to run this **now** — there is no scheduled time._"}},
            _btn_row([
                _button("Confirm", {"a": "run", "s": sid}, kind="primary"),
                _cancel_button(sid),
            ]),
        ]},
    }


def build_start_card(session: dict[str, Any], found: list[dict]) -> dict:
    """Posted the instant the run is fired — its message_id becomes the job's thread root."""
    maint, test = bool(session.get("maint")), bool(session.get("test"))
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text",
                      "content": f"▶️ Now will start set {_sst._action_words(maint, test)}"},
        },
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"{_details_md(session, found)}\n\n"
                        f"**Kindly monitor any issue happened.**"}},
        ]},
    }


def build_started_card(session: dict[str, Any], found: list[dict]) -> dict:
    """
    Replaces the review card so the tapped Confirm button cannot be tapped again.

    Deliberately short: the review card above it and the start card below it both already carry
    the full machine list, and a third copy of 100 cabinet names buries the thread.
    """
    maint, test = bool(session.get("maint")), bool(session.get("test"))
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "green",
                   "title": {"tag": "plain_text", "content": "✅ Started"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"Set **{_sst._action_words(maint, test)}** is running on "
                        f"**{len(found)}** machine(s) — progress posts below.\n"
                        f"{_where_line(session)}"}},
        ]},
    }


def build_failed_card(session: dict[str, Any], found: list[dict], err: Exception) -> dict:
    """
    Posted when the job never actually started, correcting the "✅ Started" card above it.

    Without this the group is told a PROD change is running when nothing is — the worst possible
    failure mode for a command whose whole point is that it acts immediately.
    """
    maint, test = bool(session.get("maint")), bool(session.get("test"))
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "red",
                   "title": {"tag": "plain_text", "content": "❌ Did NOT start"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"Set **{_sst._action_words(maint, test)}** on **{len(found)}** machine(s) "
                        f"**failed to start** — nothing was changed.\n"
                        f"{_where_line(session)}\n\n"
                        f"`{str(err)[:300]}`\n\nSend `/setgt` to try again."}},
        ]},
    }


def build_cancelled_card() -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "grey",
                   "title": {"tag": "plain_text", "content": "🚫 Cancelled"}},
        "body": {"elements": [{"tag": "div", "text": {
            "tag": "lark_md", "content": "Set maintenance/test was **cancelled**. "
                                         "Send `/setgt` to start again."}}]},
    }


# ---------------------------------------------------------------------------
# target resolution
# ---------------------------------------------------------------------------
def resolve_session_target(session: dict[str, Any]) -> tuple[list[dict], str]:
    """
    Resolve the session's game type to machines. Returns ``(found, error_message)``.

    Never widens to the whole environment — an unmatched game type is an error, not "every
    machine at that site".
    """
    env_code = str(session.get("env_code") or "").strip().upper()
    game_type = str(session.get("game_type") or "").strip()
    venue = str(session.get("venue") or "").strip().upper()
    if not (env_code and game_type):
        return [], "Kindly select the environment and game type."
    all_machines = machines_for_game_type(env_code, game_type)
    choices = venues_for_game_type(env_code, game_type, all_machines)
    if choices and not venue:
        # Reachable only from a stale card: the venue step renders no Confirm button. Refuse
        # rather than default to both venues.
        return [], "Kindly select the venue (" + " / ".join(v for v, _ in choices) + ")."
    found = _sst._filter_by_venue(env_code, all_machines, venue)
    if not found:
        where = env_code + (f" ({_sst._venue_label(env_code, venue)})" if venue else "")
        return [], f"{game_type} is not detected in {where}. Try again."
    return found, ""


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
    send_card: Callable[[str, dict], Any],
    run_batch: Callable[..., Any],
    sender_id: str = "",
) -> dict[str, Any] | None:
    """
    Handle every ``/setgt`` button. Returns the synchronous card.callback body, or ``None`` when
    the callback isn't ours.

    ``send_card(chat_id, card)`` must return the posted card's ``message_id``, which becomes the
    thread root so the batch's progress messages and screenshots land inside that card's thread.
    """
    if str(parsed.get("k") or "").strip().lower() != SETGT_CARD_KEY:
        return None
    act = str(parsed.get("a") or "").strip().lower()
    sid = str(parsed.get("s") or "").strip()

    # The buttons carry the same gate as the command — the card is what actually changes PROD,
    # so a check only the command honours is no check at all.
    if not (chat_allowed(chat_id) or pm_allowed(sender_id)):
        return _toast("error", "You are not allowed to use /setgt here.")

    session = get_session(sid)
    if not session:
        return _toast("error", "This /setgt form expired. Send /setgt again.")

    if act == "cancel":
        # A stale card can still send this after the run fired. Nothing here can stop a running
        # prod-batch job, so say so rather than render "Cancelled" over a change already in
        # flight. Claimed under the session lock so a Cancel racing a Confirm cannot win.
        if not claim_cancel(sid):
            return _toast("info", "Already started — this cannot be cancelled from here.")
        return _card_reply(build_cancelled_card())

    # Any change to what is targeted invalidates an approval made before it, so a stale review
    # card cannot run yesterday's machine list under today's heading.
    if act in ("toggle", "back", "env", "gt", "venue"):
        session = update_session(sid, approved=[], approved_action="") or session

    if act == "toggle":
        which = str(parsed.get("w") or "").strip().lower()
        if which in ("maint", "test"):
            session = update_session(sid, **{which: not bool(session.get(which))}) or session
        return _card_reply(build_form_card(sid, session))

    if act == "back":
        # One step back, not all the way out: venue → game type → environment.
        if str(session.get("venue") or ""):
            session = update_session(sid, venue="") or session
        elif str(session.get("game_type") or ""):
            session = update_session(sid, game_type="", venue="") or session
        else:
            session = update_session(sid, env_code="", game_type="", venue="") or session
        return _card_reply(build_form_card(sid, session))

    if act == "env":
        code = str(parsed.get("e") or "").strip().upper()
        if code not in SETGT_ENV_CODES:
            return _toast("error", f"Unknown environment: {code}")
        session = update_session(sid, env_code=code, game_type="", venue="") or session
        return _card_reply(build_form_card(sid, session))

    if act == "gt":
        gt = str(parsed.get("g") or "").strip()
        if not gt:
            return _toast("error", "Unknown game type.")
        # Clear the venue: which venues exist is per game type, so a carried-over choice could
        # silently target a venue this game type has none of.
        session = update_session(sid, game_type=gt, venue="") or session
        return _card_reply(build_form_card(sid, session))

    if act == "venue":
        v = str(parsed.get("v") or "").strip().upper()
        env_code = str(session.get("env_code") or "").strip().upper()
        allowed = set(_sst.SST_VENUE_SPLITS.get(env_code, ())) | {SETGT_VENUE_ALL}
        if v not in allowed:
            return _toast("error", f"Unknown venue: {v}")
        session = update_session(sid, venue=v) or session
        return _card_reply(build_form_card(sid, session))

    if act == "confirm":
        if not (bool(session.get("maint")) or bool(session.get("test"))):
            return _toast("error", "Kindly select Set Maintenance or Test")
        found, err = resolve_session_target(session)
        if err:
            return _card_reply(build_form_card(sid, session, error=err))
        # Freeze the approved list onto the session. resolve_session_target() re-reads
        # webmachine_data.json on every call and the scrape loop rewrites that file in this same
        # process, so re-resolving at run time could set a different set of cabinets than the ones
        # on the card the operator actually approved. What was reviewed is what runs.
        session = update_session(sid, approved=found, approved_action=selection_action(
            bool(session.get("maint")), bool(session.get("test")))) or session
        return _card_reply(build_review_card(sid, session, found))

    if act == "run":
        # Deliberately NOT re-resolved — see the snapshot note under "confirm".
        found = [m for m in (session.get("approved") or []) if isinstance(m, dict)]
        action = str(session.get("approved_action") or "")
        if not (found and action):
            return _card_reply(build_form_card(
                sid, session,
                error="⚠️ This confirmation expired before it ran — nothing was set. "
                      "Review the machines and tap **Confirm** again."))
        if not claim_run(sid):
            return _toast("info", "Already running — check the cards below.")

        run_chat = str(session.get("chat_id") or "").strip() or chat_id
        run_session = dict(session)
        # An immediate PROD change with no schedule and no reminder leaves no other trace of who
        # ordered it — /sst at least persists ``created_by`` in its store.
        print(f"[setgt] run by {sender_id or 'unknown'} in {run_chat}: {action} on "
              f"{len(found)} machine(s) — {run_session.get('env_code')} / "
              f"{run_session.get('game_type')} / venue={run_session.get('venue') or '-'}",
              flush=True)

        # Posting the start card and starting the job are both network work; Lark gives this
        # callback ~3 s, so they go on their own thread and the card is answered immediately.
        def _fire() -> None:
            root = ""
            try:
                root = str(send_card(run_chat, build_start_card(run_session, found)) or "")
            except Exception as e:  # noqa: BLE001
                print(f"[setgt] start card failed: {e!r}", flush=True)
            try:
                run_batch(run_chat, action, found, thread_root=root or None)
            except Exception as e:  # noqa: BLE001
                # The card already says "Started". Saying nothing here would leave the group
                # believing a PROD change is running when it never began — and the latch would
                # keep them from retrying it.
                print(f"[setgt] batch failed to start: {e!r}", flush=True)
                release_run(sid)
                try:
                    send_card(run_chat, build_failed_card(run_session, found, e))
                except Exception as e2:  # noqa: BLE001
                    print(f"[setgt] failure card also failed: {e2!r}", flush=True)

        threading.Thread(target=_fire, daemon=True).start()
        return _card_reply(build_started_card(session, found))

    return _toast("error", f"Unknown /setgt action: {act}")
