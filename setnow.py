"""
``/set`` — **immediate** Set-Maintenance / Set-Test.

:mod:`sst` with the schedule taken out. ``/sst`` asks for a date and a time because it parks the
work on the scheduler; ``/set`` runs the moment the second confirmation is tapped, so a date/time
picker would be noise. Everything else is the same card, including both targets:

* **Game Type** — environment → game type → venue (only when the game type spans two venues)
* **Machines**  — the same box ``/sst`` uses, so ``NWR2000-NWR2020`` ranges, ``DHS3106 DHS3107``
  runs and full display names all behave identically in both commands.

Flow
----
1. ``@bot /set`` posts the form card. ``@bot /set NWR2000-NWR2020`` opens it with the machines box
   already filled.
2. Every button updates the card in place inside Lark's 3 s card-callback window.
3. **Confirm** resolves the target against ``webmachine_data.json`` and shows a **review card**
   listing every machine that will be changed. The list is frozen at that point.
4. Confirming the review card posts the "Now will start set …" card and fires the prod-batch job
   immediately — nothing is persisted, because there is no future run to survive a restart.

The catalogue, the machine tokenizer/resolver, the renderer and the access gate are all reused
from :mod:`sst`, so the two commands can never drift apart on what a game type or a machine
reference means, or on who is allowed to drive a PROD change.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

import sst as _sst

SETNOW_CARD_KEY = "setnow"

# One source of truth with /sst: same catalogue, same venue split, same renderer, same tokenizer.
SETNOW_ENV_CODES = _sst.SST_ENV_CODES
SETNOW_VENUE_ALL = _sst.SST_VENUE_ALL

list_game_types = _sst.list_game_types
machines_for_game_type = _sst.machines_for_game_type
venues_for_game_type = _sst.venues_for_game_type
machine_lines = _sst.machine_lines
selection_text = _sst.selection_text
selection_action = _sst.selection_action
parse_machine_lines_with_ranges = _sst.parse_machine_lines_with_ranges
resolve_machines = _sst.resolve_machines

MODE_GAME = "game"
MODE_MACHINES = "machines"

CMD = "/set"


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
# Kept separate from sst's store even though the shapes overlap: a /set session has no date or
# time, and sharing the dict would let an sst-only code path read fields that never exist here.
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSIONS_LOCK = threading.Lock()
_SESSION_TTL_SEC = 7200


def _cleanup_sessions() -> None:
    now = time.time()
    with _SESSIONS_LOCK:
        for sid in [k for k, v in _SESSIONS.items()
                    if now - float(v.get("ts") or 0) > _SESSION_TTL_SEC]:
            _SESSIONS.pop(sid, None)


def new_session(chat_id: str, *, machines_text: str = "",
                thread_root: str | None = None) -> str:
    """
    A fresh form session. ``machines_text`` pre-fills the box (``/set NWR2000-NWR2020``) and
    starts the card on the Machines target, since that is plainly what was meant.
    """
    _cleanup_sessions()
    sid = uuid.uuid4().hex[:12]
    seed = (machines_text or "").strip()
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = {
            "chat_id": chat_id,
            "thread_root": (thread_root or "").strip() or None,
            # target selection: "" (not chosen) | "game" | "machines"
            "mode": MODE_MACHINES if seed else "",
            "maint": False,
            "test": False,
            "machines_text": seed,
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
            # terminal: a cancelled session can never run, however stale the card that asks
            "cancelled": False,
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
        if not s or s.get("fired") or s.get("cancelled"):
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

    Cancelling is **terminal**: it sets ``cancelled`` and throws away the frozen approval. Both
    matter, because Confirm and Cancel sit on the same review card and Lark keeps both tappable
    until the card re-renders (and re-delivers a callback when the first answer is slow). Without
    this, tapping Cancel and then Confirm ran the PROD batch after the group had been told the
    change was cancelled.
    """
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(sid)
        if not s or s.get("fired"):
            return False
        s["cancelled"] = True
        s["approved"] = []
        s["approved_action"] = ""
        s["maint"] = False
        s["test"] = False
        s["ts"] = time.time()
        return True


# ---------------------------------------------------------------------------
# card widgets
# ---------------------------------------------------------------------------
# The whole card lives inside one ``form`` container, exactly as /sst's does. Inside a form every
# interactive component must carry a ``name`` and a ``form_action_type`` or Lark rejects the card,
# and ``submit`` is what makes the typed machines box reach the callback as ``form_value`` — a
# plain button would re-render the card with an empty box.
def _form_button(label: str, name: str, value: dict, *, kind: str = "default") -> dict:
    return {
        "tag": "button",
        "name": name,
        "text": {"tag": "plain_text", "content": label[:60]},
        "type": kind,
        "form_action_type": "submit",
        "behaviors": [{"type": "callback", "value": {"k": SETNOW_CARD_KEY, **value}}],
    }


def _toggle_button(label: str, *, on: bool, sid: str, which: str) -> dict:
    return _form_button(("✅ " if on else "") + label, f"setnow_toggle_{which}",
                        {"a": "toggle", "s": sid, "w": which},
                        kind="primary" if on else "default")


def _btn_row(buttons: list[dict]) -> dict:
    """
    One row of buttons.

    ``flex_mode`` must NOT be ``bisect`` — that means *exactly two* equal columns, so a row of 3
    or 4 makes Lark reject the card. ``none`` + weighted columns lays out any count.
    """
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [b]}
            for b in buttons
        ],
    }


def _btn_rows(buttons: list[dict], per_row: int = 2) -> list[dict]:
    """Chunk buttons into rows of at most ``per_row`` — 2, like every card this bot renders."""
    return [_btn_row(buttons[i:i + per_row]) for i in range(0, len(buttons), per_row)]


def _back_row(sid: str) -> dict:
    return _btn_row([
        _form_button("◀ Change target", "setnow_back", {"a": "back", "s": sid}),
        _form_button("Cancel", "setnow_cancel", {"a": "cancel", "s": sid}, kind="danger"),
    ])


def _confirm_rows(sid: str) -> list[dict]:
    return _btn_rows([
        _form_button("Confirm", "setnow_confirm", {"a": "confirm", "s": sid}, kind="primary"),
        _form_button("◀ Change target", "setnow_back", {"a": "back", "s": sid}),
        _form_button("Cancel", "setnow_cancel", {"a": "cancel", "s": sid}, kind="danger"),
    ])


def _machines_input(value: str) -> dict:
    el: dict[str, Any] = {
        "tag": "input",
        "name": "setnow_machines",
        "input_type": "multiline_text",
        "rows": 8,
        "auto_resize": True,
        "max_rows": 20,
        "width": "fill",
        "label": {"tag": "plain_text", "content": "Machines (one per line)"},
        "label_position": "top",
        "placeholder": {"tag": "plain_text", "content": "NWR2205\nNWR2206\nNWR2000-NWR2020"},
        # Not ``required``: the toggles submit this form, and Lark would refuse the submit
        # (blocking the toggle) while the box is still empty. Confirm validates it instead.
        "required": False,
        # Lark hard-caps form input max_length at 1000 — larger is rejected and the card fails.
        "max_length": 1000,
    }
    if value:
        el["default_value"] = value
    return el


def build_form_card(sid: str, session: dict[str, Any], *, error: str = "") -> dict:
    """
    The ``/set`` form card. ``error`` renders a banner above the form.

    A banner rather than a toast: :func:`_toast` truncates at 180 characters, which would eat the
    per-candidate detail an operator needs to tell a name collision apart.
    """
    maint = bool(session.get("maint"))
    test = bool(session.get("test"))
    mode = str(session.get("mode") or "")
    env_code = str(session.get("env_code") or "").strip().upper()
    game_type = str(session.get("game_type") or "").strip()
    venue = str(session.get("venue") or "").strip().upper()

    form_elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "**What to set** — tap to select:"}},
        _btn_row([
            _toggle_button("Maintenance", on=maint, sid=sid, which="maint"),
            _toggle_button("Test", on=test, sid=sid, which="test"),
        ]),
        {"tag": "div", "text": {"tag": "lark_md", "content": selection_text(maint, test)}},
    ]

    # Loaded once and reused by the venue step and the review list: every call re-reads
    # webmachine_data.json from disk.
    game_machines = (machines_for_game_type(env_code, game_type)
                     if (mode == MODE_GAME and env_code and game_type) else [])
    venue_choices = venues_for_game_type(env_code, game_type, game_machines)

    hint = "Choose **Maintenance** / **Test**, then choose a target. This runs **now**."

    if not mode:
        form_elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**Target** — choose one:"}},
            _btn_row([
                _form_button("Game Type", "setnow_mode_game",
                             {"a": "mode", "s": sid, "m": MODE_GAME}, kind="primary"),
                _form_button("Machines", "setnow_mode_machines",
                             {"a": "mode", "s": sid, "m": MODE_MACHINES}, kind="primary"),
            ]),
        ]
    elif mode == MODE_MACHINES:
        hint = ("Paste the machines — one per line, several on one line (`DHS3106 DHS3107`), or a "
                "range (`NWR2000-NWR2020`) — then tap **Confirm**. This runs **now**.")
        form_elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**Target:** Machines"}},
            _machines_input(str(session.get("machines_text") or "")),
            *_confirm_rows(sid),
        ]
    elif not env_code:
        hint = "Select the **environment** for the game type."
        form_elements.append({"tag": "div", "text": {"tag": "lark_md",
                              "content": "**Target:** Game Type — select environment:"}})
        form_elements += _btn_rows([
            _form_button(code, f"setnow_env_{code}", {"a": "env", "s": sid, "e": code})
            for code in SETNOW_ENV_CODES
        ])
        form_elements.append(_back_row(sid))
    elif not game_type:
        hint = f"Select the **game type** in **{env_code}**."
        gts = list_game_types(env_code)
        form_elements.append({"tag": "div", "text": {"tag": "lark_md",
                              "content": f"**Environment:** {env_code} — select game type:"}})
        if not gts:
            form_elements.append({"tag": "div", "text": {"tag": "lark_md",
                                  "content": f"_No game types found for {env_code}._"}})
        # Capped at 30 like /sst: every button counts against Lark's 200-element card cap.
        shown = gts[:30]
        for i in range(0, len(shown), 2):
            form_elements.append(_btn_row([
                _form_button(f"{gt} ({n})", f"setnow_gt_{i + j}", {"a": "gt", "s": sid, "g": gt})
                for j, (gt, n) in enumerate(shown[i:i + 2])
            ]))
        if len(gts) > len(shown):
            form_elements.append({"tag": "div", "text": {"tag": "lark_md",
                                  "content": f"_… {len(gts) - len(shown)} more not shown._"}})
        form_elements.append(_back_row(sid))
    elif venue_choices and not venue:
        # Only reached when the game type really does span both venues — venues_for_game_type
        # returns [] otherwise, so single-venue game types go straight to the review list.
        names = " + ".join(v for v, _ in venue_choices)
        total = sum(n for _, n in venue_choices)
        hint = f"**{game_type}** has machines at more than one venue — pick which to set."
        form_elements.append({"tag": "div", "text": {"tag": "lark_md",
                              "content": f"**Environment:** {env_code}\n**Game type:** {game_type}"
                                         f"\n\nSelect the **venue**:"}})
        form_elements += _btn_rows(
            [_form_button(f"{v} ({n})", f"setnow_venue_{v}", {"a": "venue", "s": sid, "v": v},
                          kind="primary")
             for v, n in venue_choices]
            + [_form_button(f"{names} ({total})", "setnow_venue_all",
                            {"a": "venue", "s": sid, "v": SETNOW_VENUE_ALL})]
        )
        form_elements.append(_back_row(sid))
    else:
        machines = _sst._filter_by_venue(env_code, game_machines, venue)
        hint = "Review the machines, then tap **Confirm** — this runs **now**."
        head = f"**Environment:** {env_code}\n**Game type:** {game_type}\n"
        if venue:
            head += f"**Venue:** {_sst._venue_label(env_code, venue)}\n"
        body = f"{head}**Machines:** {len(machines)}\n\n{machine_lines(machines)}"
        if not machines:
            body = f"{head}\n⚠️ No machines found for this game type."
        form_elements.append({"tag": "div", "text": {"tag": "lark_md", "content": body}})
        form_elements += _confirm_rows(sid)

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": "⚙️ Set Maintenance / Test"}},
        "body": {"elements": (
            ([{"tag": "div", "text": {"tag": "lark_md", "content": error}}, {"tag": "hr"}]
             if error else [])
            + [
                {"tag": "div", "text": {"tag": "lark_md", "content": hint}},
                {"tag": "form", "name": "setnow_form", "elements": form_elements},
            ]
        )},
    }


# ---------------------------------------------------------------------------
# review / run cards
# ---------------------------------------------------------------------------
def _where_line(session: dict[str, Any]) -> str:
    if str(session.get("mode") or "") == MODE_MACHINES:
        return "**Target:** pasted machine list"
    env_code = str(session.get("env_code") or "").strip().upper()
    venue = str(session.get("venue") or "").strip().upper()
    where = env_code
    if venue:
        where += f" ({_sst._venue_label(env_code, venue)})"
    return f"**Game type:** {where} · {session.get('game_type') or ''}"


# ``sst._action_words`` falls through to "test" for (False, False), so wording taken from the live
# toggles can misdescribe a job — Cancel zeroes those toggles, and the start card then announced
# "set test" for a set-maintenance run. Once Confirm has frozen ``approved_action`` that is the
# authoritative answer, because it is the exact value handed to the prod-batch runner.
_ACTION_WORDS = {
    "set_maint": "maintenance",
    "set_test": "test",
    "set_both": "maintenance and test",
}


def _action_words(session: dict[str, Any]) -> str:
    approved = str(session.get("approved_action") or "")
    if approved in _ACTION_WORDS:
        return _ACTION_WORDS[approved]
    return _sst._action_words(bool(session.get("maint")), bool(session.get("test")))


def _details_md(session: dict[str, Any], found: list[dict], *, detail: bool = False) -> str:
    return "\n".join([
        f"**Action:** Set {_action_words(session)}",
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
                   "title": {"tag": "plain_text", "content": "⚙️ Confirm Set Maintenance / Test"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": _details_md(session, found, detail=True)}},
            {"tag": "div", "text": {"tag": "lark_md",
             "content": "_Confirm to run this **now** — there is no scheduled time._"}},
            _btn_row([
                {"tag": "button", "text": {"tag": "plain_text", "content": "Confirm"},
                 "type": "primary",
                 "behaviors": [{"type": "callback",
                                "value": {"k": SETNOW_CARD_KEY, "a": "run", "s": sid}}]},
                {"tag": "button", "text": {"tag": "plain_text", "content": "Cancel"},
                 "type": "danger",
                 "behaviors": [{"type": "callback",
                                "value": {"k": SETNOW_CARD_KEY, "a": "cancel", "s": sid}}]},
            ]),
        ]},
    }


def build_start_card(session: dict[str, Any], found: list[dict]) -> dict:
    """Posted the instant the run is fired — its message_id becomes the job's thread root."""
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "red",
                   "title": {"tag": "plain_text",
                             "content": f"▶️ Now will start set {_action_words(session)}"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"{_details_md(session, found)}\n\n"
                        f"**Kindly monitor any issue happened.**"}},
        ]},
    }


def build_started_card(session: dict[str, Any], found: list[dict]) -> dict:
    """
    Replaces the review card so the tapped Confirm cannot be tapped again.

    Deliberately short: the review card above it and the start card below it both carry the full
    machine list already, and a third copy of 100 cabinet names buries the thread.
    """
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "green",
                   "title": {"tag": "plain_text", "content": "✅ Started"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"Set **{_action_words(session)}** is running on "
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
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "red",
                   "title": {"tag": "plain_text", "content": "❌ Did NOT start"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"Set **{_action_words(session)}** on **{len(found)}** machine(s) "
                        f"**failed to start** — nothing was changed.\n"
                        f"{_where_line(session)}\n\n"
                        f"`{str(err)[:300]}`\n\nSend `{CMD}` to try again."}},
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
                                         f"Send `{CMD}` to start again."}}]},
    }


# ---------------------------------------------------------------------------
# target resolution
# ---------------------------------------------------------------------------
def resolve_session_target(session: dict[str, Any]) -> tuple[list[dict], str]:
    """
    Resolve the session's target to machines. Returns ``(found, error_message)``.

    Never widens: an unmatched game type is an error, not "every machine at that site", and an
    unresolved machine name blocks the whole submission rather than running the rest.
    """
    mode = str(session.get("mode") or "")

    if mode == MODE_MACHINES:
        import machine_ranges

        tokens, ranged = parse_machine_lines_with_ranges(str(session.get("machines_text") or ""))
        if not tokens:
            return [], "Kindly type at least one machine (one per line)."
        found, problems = resolve_machines(tokens, optional_tokens=ranged)
        # A span the expander refused (reversed / cross-environment / too wide) arrives as one
        # unmatchable name — say why, or it reads as "that machine is missing".
        hint = machine_ranges.range_hint(tokens)
        if problems:
            report = _sst.problem_report_md(problems, found, game_type_hint=False)
            return [], (f"{hint}\n\n{report}" if hint else report)
        if not found:
            if ranged and {t.strip().upper() for t in tokens} <= ranged:
                return [], (hint or f"No machines exist in that range ({len(tokens)} checked). "
                                    f"Check the numbers and try again.")
            return [], (hint or "No machines resolved. Try again.")
        return found, ""

    if mode != MODE_GAME:
        return [], "Kindly choose a target — **Game Type** or **Machines**."

    env_code = str(session.get("env_code") or "").strip().upper()
    game_type = str(session.get("game_type") or "").strip()
    venue = str(session.get("venue") or "").strip().upper()
    if not (env_code and game_type):
        return [], "Kindly select the environment and game type."
    all_machines = machines_for_game_type(env_code, game_type)
    choices = venues_for_game_type(env_code, game_type, all_machines)
    if choices and not venue:
        # Reachable only from a stale card: the venue step renders no Confirm button. Refuse
        # rather than default to both venues — that guess is the whole bug.
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
    form_value: dict[str, Any] | None = None,
    sender_id: str = "",
) -> dict[str, Any] | None:
    """
    Handle every ``/set`` button. Returns the synchronous card.callback body, or ``None`` when the
    callback isn't ours.

    ``send_card(chat_id, card)`` must return the posted card's ``message_id``, which becomes the
    thread root so the batch's progress messages and screenshots land inside that card's thread.
    """
    if str(parsed.get("k") or "").strip().lower() != SETNOW_CARD_KEY:
        return None
    act = str(parsed.get("a") or "").strip().lower()
    sid = str(parsed.get("s") or "").strip()

    # The buttons carry the same gate as the command — the card is what actually changes PROD,
    # so a check only the command honours is no check at all.
    if not (chat_allowed(chat_id) or pm_allowed(sender_id)):
        return _toast("error", f"You are not allowed to use {CMD} here.")

    session = get_session(sid)
    if not session:
        return _toast("error", f"This {CMD} form expired. Send {CMD} again.")

    # Fold the typed machine list back in before anything reads it: the toggles submit the form,
    # so without this a toggle would re-render the card with an empty box and Confirm would then
    # resolve against nothing.
    fv = form_value if isinstance(form_value, dict) else {}
    typed = fv.get("setnow_machines")
    if typed is not None:
        session = update_session(sid, machines_text=str(typed or "")) or session

    if act == "cancel":
        # A stale card can still send this after the run fired. Nothing here can stop a running
        # prod-batch job, so say so rather than render "Cancelled" over a change already in
        # flight. Claimed under the session lock so a Cancel racing a Confirm cannot win.
        if not claim_cancel(sid):
            return _toast("info", "Already started — use the Cancel button on the progress card.")
        return _card_reply(build_cancelled_card())

    # Any change to what is targeted invalidates an approval made before it, so a stale review
    # card cannot run yesterday's machine list under today's heading. A re-submitted machines box
    # counts as a change: the text may differ from the one that was approved.
    if act in ("toggle", "mode", "back", "env", "gt", "venue") or typed is not None:
        session = update_session(sid, approved=[], approved_action="") or session

    if act == "toggle":
        which = str(parsed.get("w") or "").strip().lower()
        if which in ("maint", "test"):
            session = update_session(sid, **{which: not bool(session.get(which))}) or session
        return _card_reply(build_form_card(sid, session))

    if act == "mode":
        m = str(parsed.get("m") or "").strip().lower()
        if m not in (MODE_GAME, MODE_MACHINES):
            return _toast("error", "Unknown target type.")
        session = update_session(sid, mode=m, env_code="", game_type="", venue="") or session
        return _card_reply(build_form_card(sid, session))

    if act == "back":
        session = update_session(sid, mode="", env_code="", game_type="", venue="") or session
        return _card_reply(build_form_card(sid, session))

    if act == "env":
        code = str(parsed.get("e") or "").strip().upper()
        if code not in SETNOW_ENV_CODES:
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
        allowed = set(_sst.SST_VENUE_SPLITS.get(env_code, ())) | {SETNOW_VENUE_ALL}
        if v not in allowed:
            return _toast("error", f"Unknown venue: {v}")
        session = update_session(sid, venue=v) or session
        return _card_reply(build_form_card(sid, session))

    if act == "confirm":
        if not (bool(session.get("maint")) or bool(session.get("test"))):
            return _toast("error", "Kindly select Set Maintenance or Test")
        found, err = resolve_session_target(session)
        if err:
            # Banner on the form, not a toast: the report is multi-line and _toast cuts at 180.
            return _card_reply(build_form_card(sid, session, error=err))
        # Freeze the approved list onto the session. resolve_session_target() re-reads
        # webmachine_data.json on every call and the scrape loop rewrites that file in this same
        # process, so re-resolving at run time could set a different set of cabinets than the ones
        # on the card the operator actually approved. What was reviewed is what runs.
        session = update_session(
            sid, approved=found,
            approved_action=selection_action(bool(session.get("maint")), bool(session.get("test"))),
        ) or session
        return _card_reply(build_review_card(sid, session, found))

    if act == "run":
        # A stale review card can still offer Confirm after Cancel was tapped on it.
        if session.get("cancelled"):
            return _toast("info", f"This was cancelled — send {CMD} again.")
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
        if str(run_session.get("mode") or "") == MODE_MACHINES:
            what = "machines=" + ",".join(str(m.get("machine") or "") for m in found[:20])
            if len(found) > 20:
                what += f",… +{len(found) - 20}"
        else:
            what = (f"{run_session.get('env_code')} / {run_session.get('game_type')} / "
                    f"venue={run_session.get('venue') or '-'}")
        print(f"[setnow] {CMD} run by {sender_id or 'unknown'} in {run_chat}: {action} on "
              f"{len(found)} machine(s) — {what}", flush=True)

        # Posting the start card and starting the job are both network work; Lark gives this
        # callback ~3 s, so they go on their own thread and the card is answered immediately.
        def _fire() -> None:
            root = ""
            try:
                root = str(send_card(run_chat, build_start_card(run_session, found)) or "")
            except Exception as e:  # noqa: BLE001
                print(f"[setnow] start card failed: {e!r}", flush=True)
            try:
                run_batch(run_chat, action, found, thread_root=root or None)
            except Exception as e:  # noqa: BLE001
                # The card already says "Started". Saying nothing here would leave the group
                # believing a PROD change is running when it never began — and the latch would
                # keep them from retrying it.
                print(f"[setnow] batch failed to start: {e!r}", flush=True)
                release_run(sid)
                try:
                    send_card(run_chat, build_failed_card(run_session, found, e))
                except Exception as e2:  # noqa: BLE001
                    print(f"[setnow] failure card also failed: {e2!r}", flush=True)

        threading.Thread(target=_fire, daemon=True).start()
        return _card_reply(build_started_card(session, found))

    return _toast("error", f"Unknown {CMD} action: {act}")
