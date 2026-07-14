"""
``/findmachine`` — interactive Lark card form to find machines.

Card fields (all selects, each with an **All** choice):
  * Environment  — PROD / QAT / UAT
  * Game type    — deduped from live machine data
  * Status       — Online / Offline

Submit → Duty Bot callback ``findmachine_submit`` → machine names grouped by
environment + venue.

Data source, freshest first:
  1. ``webapp``'s in-process EGM scrape cache (the background loop refreshes it
     every ``WEBMACHINE_SCRAPE_INTERVAL_SEC``, default 30s, and persists
     ``webmachine_data.json``). If the cache is older than
     ``FINDMACHINE_MAX_AGE_SEC`` (default 300s) a one-shot refresh is attempted,
     bounded by ``FINDMACHINE_REFRESH_WAIT_SEC`` (default 40s).
  2. ``webmachine_data.json`` on disk (same path candidates as
     ``maintenancemachineagent``) — provenance then reports the file mtime.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

CARD_CALLBACK_KEY = "findmachine_submit"
FIELD_ENV = "fm_env"
FIELD_GAME = "fm_game"
FIELD_ONLINE = "fm_online"
ALL_VALUE = "ALL"

_ENV_CHOICES = ("PROD", "QAT", "UAT")
_MAX_GAME_OPTIONS = 98  # Lark select_static caps at 100 options; keep room for "All"
_MAX_NAMES_IN_REPLY = 800
_MAX_CHARS_PER_MESSAGE = 3500


def _canon(s: object) -> str:
    return " ".join(str(s or "").split()).casefold()


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _data_file_candidates() -> list[Path]:
    """Paths tried for ``webmachine_data.json`` (mirrors maintenancemachineagent)."""
    cands: list[Path] = []
    custom = (os.environ.get("WEBMACHINE_DATA_PATH") or "").strip()
    if custom:
        cands.append(Path(custom))
    here = Path(__file__).resolve().parent
    cands.append(here / "webmachine_data.json")
    try:
        cands.append(Path.cwd() / "webmachine_data.json")
    except OSError:
        pass
    cands.append(here.parent / "webmachine_data.json")
    return cands


def _load_rows_from_file() -> tuple[list[dict], str]:
    for p in _data_file_candidates():
        try:
            if not p.is_file():
                continue
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw = raw.get("machines") or raw.get("rows") or raw.get("data") or []
            if not isinstance(raw, list):
                continue
            rows = [r for r in raw if isinstance(r, dict)]
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
            return rows, f"webmachine_data.json @ {stamp}"
        except (OSError, ValueError):
            continue
    return [], "webmachine_data.json not found"


def _webapp_cache_age_sec() -> Optional[float]:
    try:
        import webapp as _wm

        with _wm._scrape_lock:
            ts = float(_wm._scrape_ts)
        return (time.time() - ts) if ts > 0 else None
    except Exception:
        return None


def _maybe_refresh_webapp_cache() -> None:
    """One-shot scrape when the live cache is stale/empty; bounded wait, errors swallowed."""
    try:
        import webapp as _wm

        if not _wm._scrape_enabled():
            return
    except Exception:
        return
    try:
        max_age = float((os.environ.get("FINDMACHINE_MAX_AGE_SEC") or "300").strip() or "300")
    except ValueError:
        max_age = 300.0
    age = _webapp_cache_age_sec()
    if age is not None and age <= max_age:
        return
    try:
        wait = float((os.environ.get("FINDMACHINE_REFRESH_WAIT_SEC") or "40").strip() or "40")
    except ValueError:
        wait = 40.0
    th = threading.Thread(target=_wm._run_scrape_once, name="findmachine-refresh", daemon=True)
    th.start()
    th.join(max(1.0, wait))


def get_latest_rows(*, refresh_if_stale: bool = False) -> tuple[list[dict], str]:
    """Freshest machine rows + provenance string (live scrape cache, else data file)."""
    if refresh_if_stale:
        _maybe_refresh_webapp_cache()
    try:
        import webapp as _wm

        rows, src = _wm._display_rows_and_provenance()
        if rows:
            return rows, src
    except Exception:
        pass
    return _load_rows_from_file()


def _row_online_state(row: dict) -> str:
    """'offline' / 'online' / '' from whichever key the source used."""
    s = _canon(row.get("online") or row.get("online_raw") or row.get("online_label"))
    if "offline" in s:
        return "offline"
    if "online" in s:
        return "online"
    return ""


# ---------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------

def _game_type_options(rows: list[dict]) -> list[str]:
    """Distinct game types, case-insensitive dedupe keeping the most common casing."""
    variants: dict[str, dict[str, int]] = {}
    for r in rows:
        raw = " ".join(str(r.get("game_type") or "").split())
        if not raw:
            continue
        key = raw.casefold()
        variants.setdefault(key, {})
        variants[key][raw] = variants[key].get(raw, 0) + 1
    display: list[str] = []
    for key in sorted(variants):
        counts = variants[key]
        best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        display.append(best)
    return display[:_MAX_GAME_OPTIONS]


def _select_options(values: list[str], *, all_label: str) -> list[dict[str, Any]]:
    opts: list[dict[str, Any]] = [
        {"text": {"tag": "plain_text", "content": all_label}, "value": ALL_VALUE}
    ]
    for v in values:
        opts.append({"text": {"tag": "plain_text", "content": str(v)}, "value": str(v)})
    return opts


def build_findmachine_form_card() -> dict[str, Any]:
    """Lark card 2.0 form: environment + game type + online status → ``findmachine_submit``."""
    rows, src = get_latest_rows()
    envs = sorted({str(r.get("environment") or "").upper() for r in rows} & set(_ENV_CHOICES)) or list(
        _ENV_CHOICES
    )
    games = _game_type_options(rows)
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "Find machine — 查找机台"},
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "Pick filters and **Submit** — matching machine names are listed "
                            "per environment/venue.\n"
                            f"📅 Machine data: {src}"
                        ),
                    },
                },
                {
                    "tag": "form",
                    "name": "findmachine_form",
                    "elements": [
                        {"tag": "div", "text": {"tag": "plain_text", "content": "Environment 环境"}},
                        {
                            "tag": "select_static",
                            "name": FIELD_ENV,
                            "placeholder": {"tag": "plain_text", "content": "All environments"},
                            "options": _select_options(envs, all_label="All environments 全部环境"),
                            "initial_option": ALL_VALUE,
                            "required": True,
                        },
                        {"tag": "div", "text": {"tag": "plain_text", "content": "Game type 游戏类型"}},
                        {
                            "tag": "select_static",
                            "name": FIELD_GAME,
                            "placeholder": {"tag": "plain_text", "content": "All game types"},
                            "options": _select_options(games, all_label="All game types 全部游戏"),
                            "initial_option": ALL_VALUE,
                            "required": True,
                        },
                        {"tag": "div", "text": {"tag": "plain_text", "content": "Status 在线状态"}},
                        {
                            "tag": "select_static",
                            "name": FIELD_ONLINE,
                            "placeholder": {"tag": "plain_text", "content": "Online + Offline"},
                            "options": [
                                {
                                    "text": {"tag": "plain_text", "content": "All 全部"},
                                    "value": ALL_VALUE,
                                },
                                {
                                    "text": {"tag": "plain_text", "content": "Online 在线"},
                                    "value": "online",
                                },
                                {
                                    "text": {"tag": "plain_text", "content": "Offline 离线"},
                                    "value": "offline",
                                },
                            ],
                            "initial_option": ALL_VALUE,
                            "required": True,
                        },
                        {
                            "tag": "button",
                            "name": "submit_findmachine",
                            "text": {"tag": "plain_text", "content": "Submit 查询"},
                            "type": "primary",
                            "form_action_type": "submit",
                            "behaviors": [{"type": "callback", "value": {"k": CARD_CALLBACK_KEY}}],
                        },
                    ],
                },
            ]
        },
    }


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def _filter_label(env: str, game: str, online: str) -> str:
    env_l = env if env and env != ALL_VALUE else "All"
    game_l = game if game and game != ALL_VALUE else "All"
    if online == "online":
        online_l = "Online"
    elif online == "offline":
        online_l = "Offline"
    else:
        online_l = "All"
    return f"Env: **{env_l}** · Game: **{game_l}** · Status: **{online_l}**"


def run_findmachine_query(env: str, game: str, online: str) -> list[str]:
    """Filter freshest rows; return one or more ready-to-send message strings."""
    env = (env or ALL_VALUE).strip()
    game = (game or ALL_VALUE).strip()
    online = (online or ALL_VALUE).strip().lower()
    if online not in ("online", "offline"):
        online = ALL_VALUE

    rows, src = get_latest_rows(refresh_if_stale=True)
    if not rows:
        return [f"❌ No machine data available ({src}). Try again after the next scrape."]

    matched: list[dict] = []
    for r in rows:
        if env != ALL_VALUE and str(r.get("environment") or "").upper() != env.upper():
            continue
        if game != ALL_VALUE and _canon(r.get("game_type")) != _canon(game):
            continue
        if online != ALL_VALUE and _row_online_state(r) != online:
            continue
        matched.append(r)

    header = f"🔎 **Find machine** — {_filter_label(env, game, online)}\n📅 Data: {src}"
    if not matched:
        return [header + "\n\n⚠️ No machines matched. Try widening the filters."]

    # Group by environment → venue, keeping data order (already env/venue/name sorted).
    groups: dict[tuple[str, str], list[str]] = {}
    for r in matched:
        key = (
            str(r.get("environment") or "?").upper(),
            str(r.get("belongs") or "?").upper(),
        )
        name = str(r.get("name") or "").strip()
        if name:
            groups.setdefault(key, []).append(name)

    total = sum(len(v) for v in groups.values())
    online_n = sum(1 for r in matched if _row_online_state(r) == "online")
    offline_n = sum(1 for r in matched if _row_online_state(r) == "offline")
    header += f"\n✅ **{total}** machine(s) · online {online_n} · offline {offline_n}"

    # One line per group; a huge group is split into "(cont.)" lines so every
    # line stays below the per-message cap (the chunker never splits a line).
    _line_budget = _MAX_CHARS_PER_MESSAGE - 300
    lines: list[str] = []
    shown = 0
    truncated = False
    for (env_k, venue_k), names in sorted(groups.items()):
        if shown >= _MAX_NAMES_IN_REPLY:
            truncated = True
            break
        take = names[: _MAX_NAMES_IN_REPLY - shown]
        if len(take) < len(names):
            truncated = True
        shown += len(take)
        batch: list[str] = []
        batch_len = 0
        first = True
        for nm in take:
            if batch and batch_len + len(nm) + 2 > _line_budget:
                label = f"({len(names)})" if first else "(cont.)"
                lines.append(f"**{env_k} · {venue_k}** {label}:\n" + ", ".join(batch))
                first = False
                batch, batch_len = [], 0
            batch.append(nm)
            batch_len += len(nm) + 2
        if batch:
            label = f"({len(names)})" if first else "(cont.)"
            lines.append(f"**{env_k} · {venue_k}** {label}:\n" + ", ".join(batch))
    if truncated:
        lines.append(
            f"… list truncated at {_MAX_NAMES_IN_REPLY} names — narrow the filters for the full list."
        )

    # Chunk into Lark-friendly messages.
    messages: list[str] = []
    cur = header
    for line in lines:
        piece = "\n\n" + line
        if len(cur) + len(piece) > _MAX_CHARS_PER_MESSAGE and cur:
            messages.append(cur)
            cur = "(cont.)\n\n" + line
        else:
            cur += piece
    if cur:
        messages.append(cur)
    return messages
