"""
Maintenance machine agent — schedule "set / unset maintenance & test" stress-test reminders.

When the bot is @mentioned with a *scheduled* maintenance announcement such as::

    @bot Please set maintenance and test ALL WF MACHINES Good Fortune later
    JUNE 09, 2026  09:45 pm, due to Change Parameters Settings tomorrow at 10:00am
    and followed by Stress Test.

    5 Dragons-WF8145
    Dragon of the Eastern Ocean-WF8146
    ...

this module parses:

* the **action** (``set`` / ``unset`` + ``maintenance`` / ``test`` / both),
* the **action time** (``JUNE 09, 2026  09:45 pm``),
* the **reason** (collapsed to *Stress Test* — the "Change Parameters" part is only the
  business reason and is ignored for the reminder), and
* the **target machines** — either the explicit pasted list, or, for
  ``ALL WF MACHINES Good Fortune``, every WF machine of that venue read from
  ``webmachine_data.json`` (so no live page-by-page lookup is needed just to schedule).

It then schedules a **one-time** reminder **10 minutes before** the action time (e.g. 09:35 pm)
via the existing Bitable reminder sheet (``reminder.add_sheet_reminder`` with ``When = One time``),
so it survives a bot restart. The reminder fires a rich card that lists the action + machines and
carries an "I have set maintenance" confirm button.

The agent only *schedules a reminder*; it never sets maintenance automatically. At the action
time the duty staff set maintenance manually (the existing ``@bot <env> set maintenance`` /
``/sm`` prod-batch flow), then confirm on the reminder card.

**Understanding arbitrary phrasing (AI)**
``parse_intent`` first tries an LLM (OpenAI-compatible, same config as ``chatagent``) to read *any*
wording into a structured intent (op / what / env / venue / machine list / time). The machine
resolution (from ``webmachine_data.json``) and the actual set/unset stay 100% deterministic — the
LLM is only used to understand language, never to decide which machines to touch. If the LLM is
unavailable, a deterministic keyword/regex parser is used as fallback.

Relevant env vars:
* ``BOT_CHAT_API_KEY`` / ``OPENAI_API_KEY`` — enables LLM intent parsing (reused from chatagent).
* ``BOT_MAINT_AGENT_LLM`` — set ``0`` to force the regex fallback even when a key exists.
* ``BOT_MAINT_AGENT_MODEL`` — model override (defaults to ``BOT_CHAT_MODEL`` / ``gpt-4o-mini``).
* ``BOT_CHAT_API_BASE`` — OpenAI-compatible base URL.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

# Fire the reminder this many minutes before the announced action time.
MAINT_LEAD_MINUTES = 10

# Prefer an explicit pasted machine list over "all machines" when at least this many are named.
_EXPLICIT_LIST_MIN = 3

# Map (set/unset) + (maintenance/test/both) → prod-batch action code + human label.
ACTION_LABELS: dict[str, str] = {
    "set_maint": "Set maintenance",
    "set_test": "Set test",
    "set_both": "Set maintenance and test",
    "unset_maint": "Unset maintenance",
    "unset_test": "Unset test",
    "unset_both": "Unset maintenance and test",
}

# Action code → prod-batch slash-command suffix (e.g. set_both → "setmaintenancetest").
_ACTION_CMD_SUFFIX: dict[str, str] = {
    "set_maint": "setmaintenance",
    "set_test": "settest",
    "set_both": "setmaintenancetest",
    "unset_maint": "unsetmaintenance",
    "unset_test": "unsettest",
    "unset_both": "unsetmaintenancetest",
}

# env_code → prod-batch site alias accepted by ``smmachine`` command regex.
_ENV_SITE_ALIAS: dict[str, str] = {
    "NWR": "nwr",
    "NP": "nwr",
    "NCH": "nch",
    "TBR": "tbr",
    "TBP": "tbp",
    "MDR": "mdr",
    "DHS": "dhs",
    "CP": "cp",
    "WF": "wf",
}

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# Env prefixes used to recognise pasted machine names + "ALL <ENV> MACHINES …" phrases.
_ENV_PREFIXES = ("NWR", "MDR", "NCH", "TBR", "TBP", "DHS", "CP", "OSM", "WF", "WINFORD")

# "5 Dragons-WF8145", "Pure Diamonds-WF8147", "NWR2113", "Echo-TBP8671" …
_MACHINE_LINE_RE = re.compile(
    r"(?:" + "|".join(_ENV_PREFIXES) + r")\s*-?\s*\d{2,}",
    re.I,
)

# "ALL WF MACHINES Good Fortune", "ALL MACHINES Good Fortune", "all wf machine Good Fortune"
_ALL_GROUP_RE = re.compile(
    r"\ball\b\s*(?P<env>" + "|".join(_ENV_PREFIXES) + r")?\s*machines?\s+"
    r"(?P<venue>[A-Za-z][A-Za-z .'&-]*?)"
    r"(?=\s+(?:later|due\b|tomorrow|at\b|on\b|by\b|before\b|followed\b)|[,.\n]|$)",
    re.I,
)

# "All Rising Rockets Link machines", "all WF machines Good Fortune" (venue before the word machines).
_ALL_MACHINES_SCOPE_RE = re.compile(
    r"\ball\b\s+(?:the\s+)?(?:[\w][\w\s'&./-]{0,72}?\s+)?machines?\b",
    re.I,
)

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)

# Date: "Month DD[,] [YYYY]"  (year optional)
_DATE_RE = re.compile(
    r"\b(?P<mon>" + "|".join(_MONTHS.keys()) + r")\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\s*,?\s*"
    r"(?P<year>\d{4})?",
    re.I,
)

# Max chars between a date and a time when pairing them loosely (e.g. "June 17, 2026, Set … at 6:50am").
_MAX_DATE_TIME_GAP = 220

# Time tokens anywhere in the message (for loose date+time pairing).
_TIME_IN_TEXT_RE = re.compile(
    r"(?:\bat\s+|@\s*|around\s+)?"
    r"(?:"
    r"(?P<hh>\d{1,2}):(?P<mm>\d{2})\s*(?P<ap>am|pm)?"
    r"|(?P<h2>\d{1,2})\s*(?P<ap2>am|pm)"
    r"|(?P<mil>\d{3,4})\s*(?:h|hrs?|hours)\b"
    r")",
    re.I,
)
# Time right after a date — supports 9:45pm, 9pm, 21:45, and military "2145H" / "930 hrs".
_TIME_RE = re.compile(
    r"^\s*(?:at\s+|@\s*|,\s*|-\s*)?"
    r"(?:"
    r"(?P<hh>\d{1,2}):(?P<mm>\d{2})\s*(?P<ap>am|pm)?"      # 9:45pm / 21:45
    r"|(?P<h2>\d{1,2})\s*(?P<ap2>am|pm)"                    # 9pm
    r"|(?P<mil>\d{3,4})\s*(?:h|hrs?|hours)\b"               # 2145H / 930hrs
    r"|(?:(?<=at )|(?<=at  )|(?<=@)|(?<=@ ))(?P<mil2>\d{4})\b"  # "at 2145" bare 24h
    r")",
    re.I,
)

# Action instruction anchor: "set/unset … maintenance|test|mode" — the datetime that follows it is
# the *action* time (vs the stress-test/event time, which we must ignore).
_ACTION_PHRASE_RE = re.compile(
    r"(?:\b(?:set|unset|enable|disable|put|perform|execute)\b|启用|设置|取消|执行|进行)"
    r"[^.\n]{0,120}?(?:maintenance|maintain|test|mode|维护|测试)",
    re.I,
)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def _strip_mentions(text: str, mention_keys: Sequence[str]) -> str:
    t = text or ""
    for key in mention_keys or ():
        if key:
            t = t.replace(key, "")
    t = re.sub(r"@_user_\d+", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    return t


_ENV_TOKEN_RE = re.compile(
    r"(NWR|MDR|NCH|TBR|TBP|DHS|OSM|CP|WF|WINFORD)\s*-?\s*\d",
    re.I,
)


def _env_from_machine_name(machine_name: str) -> str | None:
    """
    WF / NWR / … environment from a machine display name.

    Recognises the env token immediately before the asset id anywhere in the name
    (``5 Dragons-WF8145`` → ``WF``, ``Echo-TBP8671`` → ``TBP``), and falls back to the
    smmachine prefix rule / ``winford`` keyword.
    """
    raw = (machine_name or "").strip()
    if not raw:
        return None
    m = _ENV_TOKEN_RE.search(raw)
    if m:
        env = m.group(1).upper()
        if env == "WINFORD":
            return "WF"
        if env == "OSM":
            return "CP"
        return env
    if re.search(r"winford", raw, re.I):
        return "WF"
    try:
        import smmachine

        return smmachine._prod_batch_machine_env_from_name(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
# Verb synonyms — order/word-independent, matched anywhere in the message (EN + 中文).
_UNSET_RE = re.compile(
    r"\b(unset|disable|deactivate|remove|clear|cancel|lift|unmark|"
    r"turn\s*off|switch\s*off|take\s*off|out\s*of)\b"
    r"|取消|解除|关闭|移除|停用|去掉|撤[销除下]?|下线|退出",
    re.I,
)
_SET_RE = re.compile(
    r"\b(set|enable|activate|put|apply|mark|flag|turn\s*on|switch\s*on)\b"
    r"|设置|设定|开启|启用|打开|上线|进入|加上",
    re.I,
)
# Maintenance / test keywords (EN + 中文).
_MAINT_KW_RE = re.compile(r"mainten|maintain|维护|检修|保养", re.I)
_TEST_KW_RE = re.compile(r"\btest\b|测试", re.I)
# A "stress test" announcement means: set maintenance AND test (per the duty workflow), even when
# the words "set" / "maintenance" never appear (e.g. "machines subject for Stress Test on …").
_STRESS_TEST_RE = re.compile(r"stress\s*-?\s*test|压测|压力测试", re.I)

# Operational steps that contain "clear" but are not unset-maintenance commands.
_RAM_CLEAR_RE = re.compile(r"\bram\s+clear\b", re.I)

# Read-only status check (no set/unset) — same machine targeting as maintenance commands.
_STATUS_CHECK_RE = re.compile(
    r"(?:"
    r"\bcheck\s+(?:the\s+)?status(?:\s+of)?(?:\s+(?:the\s+)?(?:machines?|egms?|cabinets?))?"
    r"|\bstatus\s+(?:of|for)\s+(?:the\s+)?(?:machines?|egms?|cabinets?)"
    r"|\b(?:machines?|egms?)\s+status\b"
    r"|\bshow\s+(?:the\s+)?status(?:\s+of)?(?:\s+(?:the\s+)?(?:machines?|egms?))?"
    r"|\bget\s+(?:the\s+)?status(?:\s+of)?(?:\s+(?:the\s+)?(?:machines?|egms?))?"
    r"|查看?状态|检查状态|机器状态|查状态"
    r")",
    re.I,
)


def _scrub_unset_false_positives(text: str) -> str:
    """Remove phrases like ``RAM Clear`` so they are not read as ``unset`` verbs."""
    t = _RAM_CLEAR_RE.sub("", text or "")
    return re.sub(r"\bparameter\s+settings?\s+update\b", "", t, flags=re.I)


def _normalize_env_code(env: str) -> str:
    e = (env or "").strip().upper()
    if e == "OSM":
        return "CP"
    if e == "NP":
        return "NWR"
    if e == "WINFORD":
        return "WF"
    return e


def parse_action(text: str) -> str | None:
    """
    Return a prod-batch action code (``set_both`` …) from free-form text, or ``None``.

    Verb is detected by synonym (set/enable/put/turn on … vs unset/disable/remove/out of …),
    anywhere in the message — not a fixed phrase. ``maintenance`` and ``test`` may appear in any
    order; both present → ``both``. A **stress test** implies *both* maintenance and test. If no
    explicit verb is found, it defaults to ``set`` (matches the existing commandagent behaviour).

    Shorthand: bare ``set`` / ``unset`` or ``set nwr8237`` (no ``maintenance`` word) → maintenance.
    """
    tl = (text or "").lower()
    has_stress = bool(_STRESS_TEST_RE.search(tl))
    has_maint = bool(_MAINT_KW_RE.search(tl)) or has_stress
    has_test = bool(_TEST_KW_RE.search(tl)) or has_stress
    tl_scrub = _scrub_unset_false_positives(tl)
    has_unset = bool(_UNSET_RE.search(tl_scrub))
    has_set = bool(_SET_RE.search(tl))
    if not (has_maint or has_test):
        # ``@bot set nwr8237`` / bare ``set`` — maintenance implied when a verb is present.
        if has_unset or has_set:
            has_maint = True
        else:
            return None
    if has_unset:
        op = "unset"
    elif has_set:
        op = "set"
    else:
        op = "set"  # "X maintenance" with no explicit verb almost always means set
    if has_maint and has_test:
        what = "both"
    elif has_maint:
        what = "maint"
    else:
        what = "test"
    return f"{op}_{what}"


def _time_from_slice(slice_text: str) -> tuple[int, int] | None:
    """Parse a time (9:45pm / 9pm / 21:45 / 2145H / 930hrs) at the start of ``slice_text``."""
    m = _TIME_RE.search(slice_text or "")
    if not m:
        return None
    ap = ""
    if m.group("hh") is not None:
        hh, mm, ap = int(m.group("hh")), int(m.group("mm")), (m.group("ap") or "").lower()
    elif m.group("h2") is not None:
        hh, mm, ap = int(m.group("h2")), 0, (m.group("ap2") or "").lower()
    else:
        raw = m.group("mil") or m.group("mil2")
        if raw is None:
            return None
        if len(raw) <= 2:
            hh, mm = int(raw), 0
        elif len(raw) == 3:
            hh, mm = int(raw[0]), int(raw[1:])
        else:
            hh, mm = int(raw[:2]), int(raw[2:])
    if ap == "pm" and hh != 12:
        hh += 12
    elif ap == "am" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def _build_dt(mon: int, day: int, year_raw: str | None, hh: int, mm: int, *, now: datetime) -> datetime | None:
    year = int(year_raw) if year_raw else now.year
    try:
        dt = datetime(year, mon, day, hh, mm)
    except ValueError:
        return None
    # No explicit year and already passed → assume next year.
    if not year_raw and dt < now - timedelta(minutes=1):
        try:
            dt = dt.replace(year=year + 1)
        except ValueError:
            return None
    return dt


def _all_datetime_candidates(text: str, *, now: datetime) -> list[tuple[int, datetime]]:
    """All ``Month DD[, YYYY] <time>`` datetimes with their start positions."""
    out: list[tuple[int, datetime]] = []
    seen_dt: set[datetime] = set()
    body = text or ""
    dates: list[tuple[int, re.Match[str]]] = [(m.start(), m) for m in _DATE_RE.finditer(body)]

    def _add(pos: int, dt: datetime | None) -> None:
        if dt and dt not in seen_dt:
            seen_dt.add(dt)
            out.append((pos, dt))

    # Tight: time within ~120 chars right after the date ("June 17, 2026 at 6:50am").
    for pos, dm in dates:
        mon = _MONTHS.get(dm.group("mon").lower())
        if not mon:
            continue
        day = int(dm.group("day"))
        if not (1 <= day <= 31):
            continue
        t = _time_from_slice(body[dm.end(): dm.end() + 120])
        if not t:
            continue
        _add(pos, _build_dt(mon, day, dm.group("year"), t[0], t[1], now=now))

    # Loose: standalone times paired with the nearest preceding date ("June 17, 2026, Set … at 6:50am").
    for tm in _TIME_IN_TEXT_RE.finditer(body):
        t = _time_from_slice(tm.group(0))
        if not t:
            continue
        prev: re.Match[str] | None = None
        prev_pos = -1
        for dpos, dm in dates:
            if dpos <= tm.start() and tm.start() - dpos <= _MAX_DATE_TIME_GAP:
                prev, prev_pos = dm, dpos
        if not prev:
            continue
        mon = _MONTHS.get(prev.group("mon").lower())
        if not mon:
            continue
        day = int(prev.group("day"))
        if not (1 <= day <= 31):
            continue
        _add(tm.start(), _build_dt(mon, day, prev.group("year"), t[0], t[1], now=now))

    out.sort(key=lambda x: x[0])
    return out


def parse_action_datetime(text: str, *, now: datetime | None = None) -> datetime | None:
    """
    Parse the **action** datetime — when to actually set/unset maintenance.

    When several times appear (e.g. the stress-test event time *and* a separate
    "Set Maintenance … at <time>" instruction), prefer the datetime that follows the action
    instruction; otherwise fall back to the first datetime found.
    """
    now = now or datetime.now()
    body = text or ""
    cands = _all_datetime_candidates(body, now=now)
    if not cands:
        return None
    am = next(_ACTION_PHRASE_RE.finditer(body), None)
    if not am:
        return cands[0][1]
    ap_end = am.end()
    line_start = body.rfind("\n", 0, am.start()) + 1
    line_end = body.find("\n", am.end())
    if line_end < 0:
        line_end = len(body)
    # e.g. "10:30 PM – Set all machines to Maintenance" (time just before the phrase on one line).
    same_line = [(p, dt) for p, dt in cands if line_start <= p <= line_end]
    after = [(p, dt) for p, dt in cands if p >= am.start()]
    pool = same_line or after
    if pool:
        pool.sort(key=lambda x: (abs(x[0] - ap_end), x[0]))
        return pool[0][1]
    return cands[0][1]


def parse_reason(text: str) -> str:
    """Collapse the announcement reason to a short label (defaults to *Stress Test*)."""
    m = re.search(
        r"\breason\s*:?\s*\n?\s*(.+?)(?=\n\s*\n|\nSchedule|\nList of|$)",
        text or "",
        re.I | re.S,
    )
    if m:
        reason = re.sub(r"\s+", " ", m.group(1).strip())
        if reason:
            return reason[:200]
    if re.search(r"stress\s*test", text or "", re.I):
        return "Stress Test"
    m = re.search(r"\bdue to\s+(.+?)(?=[,.\n]|$)", text or "", re.I)
    if m:
        return m.group(1).strip()
    return "Stress Test"


def extract_links(text: str) -> list[str]:
    """HTTP(S) URLs from the announcement (e.g. Lark applink), deduped in order."""
    out: list[str] = []
    seen: set[str] = set()
    for url in _URL_RE.findall(text or ""):
        url = url.rstrip(".,;)")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _has_all_machines_scope(text: str) -> bool:
    """True when the message scopes the action to *all* machines of a venue/game-type."""
    return bool(_ALL_MACHINES_SCOPE_RE.search(text or ""))


# Asset token: env prefix + digits, e.g. ``TBP8609``, ``WF 8145``, ``WF-8147``.
_MACHINE_TOKEN_RE = re.compile(
    r"(?:" + "|".join(_ENV_PREFIXES) + r")\s*-?\s*\d+",
    re.I,
)


def extract_machine_lines(text: str) -> list[str]:
    """
    Machine references from the message — two styles supported:

    * **Pasted full display names**, one per line (``5 Dragons-WF8145``) → kept whole.
    * **Inline asset tokens** after the command (``unset maintenance TBP8609 TBP8610``) → only the
      ``TBP8609``-style tokens are extracted, so the command words don't pollute the machine list.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(x: str) -> None:
        x = x.strip()
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # Drop list bullets so "- Dancing Drums-WF8092" → "Dancing Drums-WF8092".
        line = re.sub(r"^[-*•·]+\s*", "", line).strip()
        if not line:
            continue
        is_action_line = bool(
            _SET_RE.search(line) or _UNSET_RE.search(line)
            or _MAINT_KW_RE.search(line) or _TEST_KW_RE.search(line)
            or _STRESS_TEST_RE.search(line)
        )
        tokens = [re.sub(r"[\s-]", "", t) for t in _MACHINE_TOKEN_RE.findall(line)]
        if is_action_line:
            # Imperative line — take only the asset tokens, never the whole sentence.
            for t in tokens:
                _add(t)
        elif _MACHINE_LINE_RE.search(line):
            line_compact = re.sub(r"[^A-Za-z0-9]", "", line).upper()
            token_compact = "".join(t.upper() for t in tokens)
            # Status / command noise on the same line (e.g. ``nwr2197 machine status``) — asset only.
            if _STATUS_CHECK_RE.search(line) or (
                tokens
                and token_compact
                and token_compact in line_compact
                and line_compact != token_compact
            ):
                for t in tokens:
                    _add(t)
            else:
                # Clean pasted machine line — keep the full display name.
                _add(line)
        else:
            for t in tokens:
                _add(t)
    return out


def parse_all_group(text: str) -> dict[str, str] | None:
    """Detect ``ALL [ENV] MACHINES <Venue>`` → ``{"env_code": "WF", "venue": "Good Fortune"}``."""
    m = _ALL_GROUP_RE.search(text or "")
    if not m:
        return None
    env = (m.group("env") or "").strip().upper()
    if env == "WINFORD":
        env = "WF"
    if env == "OSM":
        env = "CP"
    venue = re.sub(r"\s+", " ", (m.group("venue") or "").strip())
    # Drop a trailing "machine(s)" noise word if the regex over-captured.
    venue = re.sub(r"\bmachines?\b", "", venue, flags=re.I).strip()
    return {"env_code": env, "venue": venue}


# ---------------------------------------------------------------------------
# webmachine_data.json lookup (for "ALL <ENV> MACHINES <Venue>")
# ---------------------------------------------------------------------------
def _webmachine_data_candidates() -> list[Path]:
    """All paths we will try for ``webmachine_data.json``, in priority order."""
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
    # The scraper (webmachine.py) writes next to itself — usually the same dir, but be safe.
    cands.append(here.parent / "webmachine_data.json")
    # De-dupe while preserving order.
    seen: set[str] = set()
    out: list[Path] = []
    for c in cands:
        key = str(c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# Last data path actually read (for diagnostics in error messages).
_last_data_path: str = ""


def load_webmachine_rows() -> list[dict]:
    """Load + normalise rows from ``webmachine_data.json`` (tries several paths; [] if none work)."""
    global _last_data_path
    for p in _webmachine_data_candidates():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            raw = raw.get("machines") or raw.get("rows") or raw.get("data") or []
        if not isinstance(raw, list):
            continue
        rows = [r for r in raw if isinstance(r, dict)]
        if rows:
            _last_data_path = str(p)
            return rows
    _last_data_path = ""
    return []


def _data_path_hint() -> str:
    """Human-readable note about where we looked for the machine list (for error cards)."""
    if _last_data_path:
        return f"Loaded from `{_last_data_path}`."
    tried = ", ".join(f"`{p}`" for p in _webmachine_data_candidates())
    return f"Could not read webmachine_data.json. Looked at: {tried}."


def _row_matches_env(row: dict, env_code: str) -> bool:
    env = (env_code or "").strip().upper()
    if not env or env == "ALL":
        return True
    belongs = str(row.get("belongs") or "").strip().upper()
    name = str(row.get("name") or row.get("machine") or "")
    if env == "CP":
        return belongs in ("CP", "OSM") or _env_from_machine_name(name) == "CP"
    if env in ("NWR", "NP"):
        return belongs in ("NWR", "NP") or _env_from_machine_name(name) in ("NWR", "NP")
    return belongs == env or _env_from_machine_name(name) == env


def _row_display_name(row: dict) -> str:
    return str(row.get("name") or row.get("machine") or "").strip()


def resolve_all_group(env_code: str, venue: str) -> tuple[list[str], str]:
    """
    Resolve ``ALL <ENV> MACHINES <Venue>`` to machine display names from ``webmachine_data.json``.

    Returns ``(machine_names, note)``. Matching strategy:

    1. Keep rows in the requested environment (``WF`` …).
    2. If a venue phrase is given, keep rows whose ``belongs`` / name / game_type contains it.
    3. If the venue phrase matches nothing but the environment has machines, fall back to **all**
       machines of that environment and say so in ``note`` (the persisted data only labels the
       environment, not the venue, so this keeps the schedule usable).
    """
    rows = load_webmachine_rows()
    if not rows:
        return [], "⚠️ webmachine_data.json is empty or missing — could not resolve the machine list."

    # Maintenance set/unset only targets PROD machines.
    rows = [r for r in rows if str(r.get("environment") or "PROD").strip().upper() == "PROD"]
    env_rows = [r for r in rows if _row_matches_env(r, env_code)]
    if not env_rows:
        return [], f"⚠️ No PROD {env_code or 'matching'} machines found in webmachine_data.json."

    venue_key = re.sub(r"[^a-z0-9]", "", (venue or "").lower())
    if not venue_key:
        names = [_row_display_name(r) for r in env_rows if _row_display_name(r)]
        return names, ""

    def _hay(r: dict) -> str:
        bits = [r.get("belongs"), r.get("name"), r.get("machine"), r.get("game_type"), r.get("venue")]
        return re.sub(r"[^a-z0-9]", "", " ".join(str(b or "") for b in bits).lower())

    venue_rows = [r for r in env_rows if venue_key in _hay(r)]
    if venue_rows:
        names = [_row_display_name(r) for r in venue_rows if _row_display_name(r)]
        return names, ""

    names = [_row_display_name(r) for r in env_rows if _row_display_name(r)]
    note = (
        f"ℹ️ Venue “{venue}” is not separately labelled in webmachine_data.json — "
        f"used all {len(names)} {env_code} machine(s) instead."
    )
    return names, note


def _row_asset_id(row: dict) -> str:
    """Trailing numeric asset id of a row name (``Rising Rockets Emperor-0253(TEST)`` → ``0253``)."""
    nums = re.findall(r"\d{2,}", _row_display_name(row))
    return nums[-1] if nums else ""


# Explicit machine reference inside a command: a full display name ending in ``-1234`` /
# ``WF8145``, or a bare asset id (``0253``). Used to honour "cp set test 0253,0254".
_EXPLICIT_REF_SPLIT_RE = re.compile(r"[,;&\n]+")
_TRAILING_ID_RE = re.compile(r"(\d{2,6})\s*(?:\([^)]*\))?\s*$")


def resolve_env_asset_refs(env_code: str, text: str) -> list[str]:
    """
    Resolve explicit machine references in a command to full display names within ``env_code``.

    Handles bare asset ids (``0253``), comma/space lists (``0253,0254``) and pasted display names
    without an env prefix (``Rising Rockets Emperor-0253``). Returns ``[]`` when nothing resolves,
    so the caller can fall back to the group/all behaviour.
    """
    env = _normalize_env_code(env_code)
    if not env:
        return []
    rows = [r for r in load_webmachine_rows() if str(r.get("environment") or "PROD").strip().upper() == "PROD"]
    env_rows = [r for r in rows if _row_matches_env(r, env)]
    if not env_rows:
        return []

    by_id: dict[str, str] = {}
    by_norm: dict[str, str] = {}
    for r in env_rows:
        name = _row_display_name(r)
        if not name:
            continue
        aid = _row_asset_id(r)
        if aid:
            by_id.setdefault(aid.lstrip("0") or aid, name)
        by_norm.setdefault(_norm(name), name)

    # Strip the command preamble (env word + verb + maintenance/test/mode words) so only the
    # machine references remain to be tokenised.
    refs = _strip_command_words(text)

    out: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            out.append(name)

    for seg in _EXPLICIT_REF_SPLIT_RE.split(refs):
        seg = seg.strip()
        if not seg:
            continue
        # Whole-name match first (e.g. "Rising Rockets Emperor-0253").
        nm = by_norm.get(_norm(seg))
        if nm:
            _add(nm)
            continue
        m = _TRAILING_ID_RE.search(seg)
        if m:
            key = m.group(1).lstrip("0") or m.group(1)
            nm = by_id.get(key)
            if nm:
                _add(nm)
                continue
        # Bare space-separated ids inside the segment ("0253 0254").
        for tok in re.findall(r"\b\d{2,6}\b", seg):
            nm = by_id.get(tok.lstrip("0") or tok)
            if nm:
                _add(nm)
    return out


# Command keywords to drop before reading explicit machine references.
_COMMAND_WORD_RE = re.compile(
    r"\b(?:set|unset|enable|disable|put|apply|mark|turn\s*on|turn\s*off|switch\s*on|switch\s*off|"
    r"maintenance|maintain|test|mode|and|to|the|all|machines?|cabinets?|egms?|please|kindly)\b",
    re.I,
)


def _strip_command_words(text: str) -> str:
    """Remove the env word + verb + maintenance/test/mode keywords, leaving machine refs."""
    t = text or ""
    t = _ENV_WORD_RE.sub(" ", t)
    t = _COMMAND_WORD_RE.sub(" ", t)
    return t


# ---------------------------------------------------------------------------
# Data-grounded entity detection (NOT a fixed sentence template).
#
# The set of environments + venue/game-type names is learned from
# ``webmachine_data.json`` itself, so any phrasing/word-order works and it adapts
# automatically when the machine data changes — no hardcoded "ALL <ENV> MACHINES
# <venue>" sentence, no model retraining needed.
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


# Spoken env words → env code (word-level, matched anywhere in the message).
_ENV_WORDS: dict[str, str] = {
    "nwr": "NWR", "np": "NWR",
    "newport": "NWR",
    "nch": "NCH", "nc": "NCH",
    "tbr": "TBR", "tbp": "TBP",
    "mdr": "MDR", "dhs": "DHS",
    "cp": "CP", "osm": "CP",
    "wf": "WF", "winford": "WF",
}
_ENV_WORD_RE = re.compile(r"\b(" + "|".join(sorted(_ENV_WORDS, key=len, reverse=True)) + r")\b", re.I)

_VENUE_VOCAB_CACHE: dict[str, Any] = {"sig": None, "by_norm": {}}
_VENUE_VOCAB_LOCK = threading.Lock()


def _data_signature() -> str:
    """Cheap change signal so the venue vocab refreshes when the data file changes."""
    for p in _webmachine_data_candidates():
        try:
            st = p.stat()
            return f"{p}:{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            continue
    return ""


def _venue_vocab() -> dict[str, str]:
    """``{normalized_game_type: original_game_type}`` learned from the data file (cached)."""
    sig = _data_signature()
    with _VENUE_VOCAB_LOCK:
        if _VENUE_VOCAB_CACHE["sig"] == sig and _VENUE_VOCAB_CACHE["by_norm"]:
            return _VENUE_VOCAB_CACHE["by_norm"]
    by_norm: dict[str, str] = {}
    for r in load_webmachine_rows():
        for key in ("game_type", "venue", "belongs"):
            val = str(r.get(key) or "").strip()
            n = _norm(val)
            # Skip very short / pure-numeric tokens to avoid false matches.
            if len(n) >= 4 and not n.isdigit():
                by_norm.setdefault(n, val)
    with _VENUE_VOCAB_LOCK:
        _VENUE_VOCAB_CACHE["sig"] = sig
        _VENUE_VOCAB_CACHE["by_norm"] = by_norm
    return by_norm


def detect_envs(text: str) -> list[str]:
    """Env codes mentioned anywhere in the text (deduped, in order of appearance)."""
    out: list[str] = []
    for m in _ENV_WORD_RE.finditer(text or ""):
        code = _ENV_WORDS.get(m.group(1).lower())
        if code and code not in out:
            out.append(code)
    return out


def detect_venue(text: str) -> str:
    """
    Longest known venue/game-type name (learned from the data file) that appears anywhere in the
    message. Order-independent: "good fortune", "GoodFortune", "winford good fortune" all match.
    """
    nt = _norm(text)
    if not nt:
        return ""
    best = ""
    best_orig = ""
    for n, orig in _venue_vocab().items():
        if n in nt and len(n) > len(best):
            best, best_orig = n, orig
    return best_orig


def _env_for_venue(venue: str, *, prefer: Sequence[str] = ()) -> str:
    """Pick the single env whose machines actually have this venue/game-type, else ''."""
    if not venue:
        return ""
    vkey = _norm(venue)
    envs: list[str] = []
    for r in load_webmachine_rows():
        hay = _norm(f"{r.get('game_type','')} {r.get('venue','')} {r.get('belongs','')}")
        if vkey and vkey in hay:
            e = (str(r.get("belongs") or "").upper() or _env_from_machine_name(_row_display_name(r)) or "")
            if e == "OSM":
                e = "CP"
            if e and e not in envs:
                envs.append(e)
    if prefer:
        inter = [e for e in envs if e in set(prefer)]
        if len(inter) == 1:
            return inter[0]
    return envs[0] if len(envs) == 1 else ""


def _venue_from_all_machines_scope(text: str) -> str:
    """Venue/game-type from the ``all … machines`` scope phrase (ignores incidental bullet items)."""
    m = _ALL_MACHINES_SCOPE_RE.search(text or "")
    if not m:
        return ""
    return detect_venue(m.group(0))


def detect_group_target(text: str) -> dict[str, str] | None:
    """
    Detect a machine *group* (env + optional venue/game-type) from free phrasing, grounded in the
    data-file vocabulary. Falls back to the legacy ``ALL <ENV> MACHINES <Venue>`` template only if
    grounding finds nothing. Returns ``{"env_code", "venue"}`` or ``None``.
    """
    envs = detect_envs(text)
    venue = _venue_from_all_machines_scope(text) or detect_venue(text)
    if not envs and not venue:
        legacy = parse_all_group(text)
        if not legacy:
            return None
        return legacy
    env_code = ""
    if len(envs) == 1:
        env_code = envs[0]
    elif venue:
        env_code = _env_for_venue(venue, prefer=envs)
    elif envs:
        env_code = envs[0]
    if not env_code:
        return None
    return {"env_code": env_code, "venue": venue}


# ---------------------------------------------------------------------------
# Announcement → reminder
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# AI intent extraction (LLM) — understands arbitrary phrasing, then machine
# resolution + the actual set/unset stay 100% deterministic (never guessed).
# Reuses the OpenAI-compatible config already used by ``chatagent``.
# ---------------------------------------------------------------------------
def _maint_llm_key() -> str:
    return (os.getenv("BOT_CHAT_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()


def _maint_llm_enabled() -> bool:
    """LLM intent parsing is on when an API key exists and ``BOT_MAINT_AGENT_LLM`` isn't disabled."""
    if (os.getenv("BOT_MAINT_AGENT_LLM") or "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return bool(_maint_llm_key())


def _maint_llm_model() -> str:
    try:
        import chatagent as ca

        return ca.shared_llm_model(module_override=os.getenv("BOT_MAINT_AGENT_MODEL"))
    except Exception:
        return (os.getenv("BOT_MAINT_AGENT_MODEL") or os.getenv("BOT_CHAT_MODEL") or "gpt-4o-mini").strip()


def _maint_llm_base() -> str:
    return (os.getenv("BOT_CHAT_API_BASE") or "https://api.openai.com/v1").strip().rstrip("/")


_MAINT_LLM_TIMEOUT = float(os.getenv("BOT_MAINT_AGENT_LLM_TIMEOUT", "20"))
_maint_llm_failed_logged = False

_MAINT_LLM_SYSTEM = (
    "You extract a machine-maintenance command from a chat message for an EGM duty bot. "
    "Reply with ONLY a compact JSON object, no prose, no code fences.\n"
    "Schema:\n"
    "{\n"
    '  "is_maintenance_request": bool,   // true only if the user wants to set/unset maintenance and/or test on machines\n'
    '  "op": "set" | "unset" | null,\n'
    '  "what": "maintenance" | "test" | "both" | null,\n'
    '  "env": "NWR"|"NCH"|"TBR"|"TBP"|"MDR"|"DHS"|"CP"|"WF"|null,  // venue/environment if stated\n'
    '  "target_kind": "list" | "all_group" | null,  // list = explicit machine names given; all_group = "all <env> machines <venue>"\n'
    '  "machines": [string],            // explicit machine display names exactly as written, else []\n'
    '  "venue": string|null,            // e.g. "Good Fortune" for all_group, else null\n'
    '  "action_datetime": string|null   // ISO-8601 local time of when to do it, e.g. "2026-06-09T21:45", else null\n'
    "}\n"
    "Environment aliases (stress test / maintenance):\n"
    "- **OSM and CP are the same environment** — always set env to **CP** when the message says OSM, CP, "
    "osmslot, or machines like OSM253 / OSMCP181 / CP-xxxx (CP environment).\n"
    "- Do not treat OSM as a separate env from CP.\n"
    "Rules: 'maintenance and test' => what='both'. Stress test announcements => what='both'. "
    "If only a future time is mentioned it is action_datetime. "
    "Do NOT invent machine names or a venue. If the message is not about machine maintenance/test, "
    "set is_maintenance_request=false."
)


def _maint_llm_extract(text: str, *, now: datetime) -> Optional[dict[str, Any]]:
    """Call the LLM to extract a structured maintenance intent. Returns a raw dict or ``None``."""
    global _maint_llm_failed_logged
    api_key = _maint_llm_key()
    if not api_key:
        return None
    user = (
        f"Current local datetime is {now.strftime('%Y-%m-%dT%H:%M')} ({now.strftime('%A')}). "
        f"Resolve relative times against it.\n\nMessage:\n{text.strip()}"
    )
    payload = {
        "model": _maint_llm_model(),
        "messages": [
            {"role": "system", "content": _MAINT_LLM_SYSTEM},
            {"role": "user", "content": user},
        ],
        "max_tokens": 400,
        "temperature": 0,
    }
    req = urllib.request.Request(
        f"{_maint_llm_base()}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_MAINT_LLM_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return _maint_llm_parse_json(content)
    except urllib.error.HTTPError as exc:
        if not _maint_llm_failed_logged:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = exc.reason
            print(f"⚠️ maintenance LLM HTTP {exc.code}: {detail}", flush=True)
            _maint_llm_failed_logged = True
        return None
    except Exception as exc:  # noqa: BLE001
        if not _maint_llm_failed_logged:
            print(f"⚠️ maintenance LLM request failed: {exc!r}", flush=True)
            _maint_llm_failed_logged = True
        return None


def _maint_llm_parse_json(content: str) -> Optional[dict[str, Any]]:
    s = (content or "").strip()
    if not s:
        return None
    # Strip code fences / locate the JSON object.
    s = re.sub(r"^```(?:json)?|```$", "", s.strip(), flags=re.I | re.M).strip()
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1 or b < a:
        return None
    try:
        obj = json.loads(s[a : b + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _action_from_op_what(op: str, what: str) -> str | None:
    op = (op or "").strip().lower()
    what = (what or "").strip().lower()
    if op not in ("set", "unset"):
        return None
    what_map = {"maintenance": "maint", "maint": "maint", "test": "test", "both": "both"}
    w = what_map.get(what)
    return f"{op}_{w}" if w else None


def _normalize_llm_intent(raw: dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
    """Turn the LLM JSON into the normalized intent (resolving machines deterministically)."""
    if not raw.get("is_maintenance_request"):
        return None
    action = _action_from_op_what(str(raw.get("op") or ""), str(raw.get("what") or ""))
    if not action:
        return None

    env_code = str(raw.get("env") or "").strip().upper()
    if env_code == "WINFORD":
        env_code = "WF"
    if env_code == "OSM":
        env_code = "CP"
    venue = str(raw.get("venue") or "").strip()
    target_kind = str(raw.get("target_kind") or "").strip().lower()

    note = ""
    machines_raw = raw.get("machines")
    if isinstance(machines_raw, str):
        # Small models sometimes return a bare string instead of a list — don't char-split it.
        machines_raw = [machines_raw]
    machines = [str(m).strip() for m in (machines_raw or []) if str(m).strip()]
    if machines:
        target_kind = "list"
    elif target_kind == "all_group" or venue:
        if not env_code:
            return None
        machines, note = resolve_all_group(env_code, venue)
        target_kind = "all_group"
    if not machines:
        return None

    action_dt = None
    iso = str(raw.get("action_datetime") or "").strip()
    if iso:
        action_dt = _parse_iso_dt(iso)

    return _build_intent(
        action=action,
        action_dt=action_dt,
        machines=machines,
        env_code=env_code,
        target_kind=target_kind or "list",
        venue=venue,
        note=note,
        reason=parse_reason(raw.get("_source_text") or ""),
        source="llm",
    )


def _parse_iso_dt(s: str) -> datetime | None:
    s = (s or "").strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Unified intent parsing (LLM first, deterministic rules as fallback)
# ---------------------------------------------------------------------------
def _build_intent(
    *,
    action: str,
    action_dt: datetime | None,
    machines: list[str],
    env_code: str,
    target_kind: str,
    venue: str,
    note: str,
    reason: str,
    source: str,
) -> dict[str, Any] | None:
    if not action or not machines:
        return None
    env_codes = sorted({e for e in (_env_from_machine_name(m) for m in machines) if e})
    env_summary = "/".join(env_codes) if env_codes else (env_code or "?")
    if not env_code and len(env_codes) == 1:
        env_code = env_codes[0]
    return {
        "action": action,
        "action_label": ACTION_LABELS.get(action, action),
        "action_dt": action_dt,
        "reminder_dt": (action_dt - timedelta(minutes=MAINT_LEAD_MINUTES)) if action_dt else None,
        "reason": reason or "Stress Test",
        "machines": machines,
        "env_code": env_code,
        "env_summary": env_summary,
        "target_kind": target_kind,
        "venue": venue,
        "note": note,
        "source": source,
    }


def _looks_like_maintenance_request(text: str) -> bool:
    """
    Cheap gate so heavier parsing only runs on plausible maintenance messages.

    ``maintenance`` is a strong signal (needs only a verb or a scope word). A bare ``test`` is weak
    (casual chat), so it needs both a verb and a scope to qualify.

    Shorthand ``set nwr8237`` / bare ``set`` / ``unset`` (maintenance implied) also qualifies.
    """
    t = (text or "").strip()
    if not t or t.lstrip().startswith("/"):
        return False
    tl = t.lower()
    tl_scrub = _scrub_unset_false_positives(tl)
    has_stress = bool(_STRESS_TEST_RE.search(tl))
    has_maint = bool(_MAINT_KW_RE.search(tl)) or has_stress
    has_test = bool(_TEST_KW_RE.search(tl))
    has_verb = bool(_SET_RE.search(tl) or _UNSET_RE.search(tl_scrub))
    has_machine_ref = bool(_MACHINE_LINE_RE.search(t)) or bool(_MACHINE_TOKEN_RE.search(t))
    # ``@bot set nwr8237`` or ``set`` + machines on following lines.
    if has_verb and (has_machine_ref or re.fullmatch(r"(?i)(?:set|unset)\s*", t.strip())):
        return True
    if not (has_maint or has_test):
        return False
    has_scope = (
        bool(re.search(r"\bmachines?\b|\ball\b|\bcabinets?\b|\begms?\b|机台|机器|全部|所有", tl))
        or has_machine_ref
        or bool(_ENV_WORD_RE.search(t))
    )
    if has_maint:
        return has_verb or has_scope
    return has_verb and has_scope


def _parse_intent_rules(text: str, *, now: datetime) -> dict[str, Any] | None:
    """Deterministic fallback parser (no AI)."""
    action = parse_action(text)
    if not action:
        return None
    action_dt = parse_action_datetime(text, now=now)
    machines = extract_machine_lines(text)
    target_kind = "list"
    env_code = ""
    venue = ""
    note = ""
    group = detect_group_target(text)
    has_date = bool(_DATE_RE.search(text))
    if (
        not machines
        and group
        and not has_date
        and not _has_all_machines_scope(text)
    ):
        refs = resolve_env_asset_refs(group.get("env_code") or "", text)
        if refs:
            machines = refs
            env_code = _normalize_env_code(group.get("env_code") or "")
            target_kind = "list"
    if group and _has_all_machines_scope(text) and len(machines) < _EXPLICIT_LIST_MIN:
        env_code = _normalize_env_code(group["env_code"])
        venue = group["venue"]
        machines, note = resolve_all_group(env_code, venue)
        target_kind = "all_group"
    elif machines:
        target_kind = "list"
    elif group:
        env_code = _normalize_env_code(group["env_code"])
        venue = group["venue"]
        machines, note = resolve_all_group(env_code, venue)
        target_kind = "all_group"
    else:
        return None
    if not machines:
        return None
    return _build_intent(
        action=action,
        action_dt=action_dt,
        machines=machines,
        env_code=env_code,
        target_kind=target_kind,
        venue=venue,
        note=note,
        reason=parse_reason(text),
        source="rule",
    )


_INTENT_CACHE: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
_INTENT_CACHE_LOCK = threading.Lock()
_INTENT_CACHE_TTL = 180.0


def parse_intent(text: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """
    Parse a maintenance request from arbitrary phrasing.

    Order: cheap keyword gate → LLM extraction (if enabled) → deterministic rule fallback.
    Machine resolution (``webmachine_data.json``) and the action stay deterministic regardless.
    """
    body = (text or "").strip()
    if not _looks_like_maintenance_request(body):
        return None

    use_cache = now is None
    real_now = now or datetime.now()
    if use_cache:
        with _INTENT_CACHE_LOCK:
            hit = _INTENT_CACHE.get(body)
            if hit and (time.time() - hit[0]) < _INTENT_CACHE_TTL:
                return hit[1]

    result: dict[str, Any] | None = None
    if _maint_llm_enabled():
        raw = _maint_llm_extract(body, now=real_now)
        if isinstance(raw, dict):
            raw["_source_text"] = body
            result = _normalize_llm_intent(raw, now=real_now)
    if result is None:
        result = _parse_intent_rules(body, now=real_now)

    if use_cache:
        with _INTENT_CACHE_LOCK:
            _INTENT_CACHE[body] = (time.time(), result)
            if len(_INTENT_CACHE) > 256:
                _INTENT_CACHE.clear()
    return result


def _classify_rules(text: str, *, now: datetime) -> dict[str, Any] | None:
    """
    Lightweight **routing** classification (no machine resolution) so detection still works even
    when ``webmachine_data.json`` can't be read. Returns action / time / target_kind / env / venue.
    """
    if not _looks_like_maintenance_request(text):
        return None
    action = parse_action(text)
    if not action:
        return None
    action_dt = parse_action_datetime(text, now=now)
    machines = extract_machine_lines(text)
    group = detect_group_target(text)
    has_date = bool(_DATE_RE.search(text))
    # Explicit asset refs ("cp set test 0253,0254" / "...Rising Rockets Emperor-0253") must beat the
    # group/all behaviour — resolve them within the named env. Skip when a schedule lists "all
    # machines" or carries a date (those are handled by the branches below).
    if (
        not machines
        and group
        and not has_date
        and not _has_all_machines_scope(text)
    ):
        refs = resolve_env_asset_refs(group.get("env_code") or "", text)
        if refs:
            return {
                "action": action,
                "action_dt": action_dt,
                "target_kind": "list",
                "env_code": _normalize_env_code(group.get("env_code") or ""),
                "venue": "",
                "machines": refs,
                "source": "rule",
            }
    # "All Rising Rockets Link machines" must win over incidental OSM253 tokens in bullet remarks,
    # but a substantial pasted list (e.g. 20 named machines) always wins over schedule "all machines".
    if group and _has_all_machines_scope(text) and len(machines) < _EXPLICIT_LIST_MIN:
        return {
            "action": action,
            "action_dt": action_dt,
            "target_kind": "all_group",
            "env_code": _normalize_env_code(group.get("env_code") or ""),
            "venue": group.get("venue") or "",
            "machines": [],
            "source": "rule",
        }
    if machines:
        # Infer env from the pasted machine names (TBP8609 → TBP), so no site word is needed.
        envs = sorted({_normalize_env_code(e) for e in (_env_from_machine_name(m) for m in machines) if e})
        return {
            "action": action,
            "action_dt": action_dt,
            "target_kind": "list",
            "env_code": envs[0] if len(envs) == 1 else "",
            "venue": "",
            "machines": machines,
            "source": "rule",
        }
    if not group:
        return None
    return {
        "action": action,
        "action_dt": action_dt,
        "target_kind": "all_group",
        "env_code": _normalize_env_code(group.get("env_code") or ""),
        "venue": group.get("venue") or "",
        "machines": [],
        "source": "rule",
    }


def _classify(text: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Routing classification: deterministic rules first, LLM only as a fallback (if enabled)."""
    real_now = now or datetime.now()
    c = _classify_rules(text, now=real_now)
    if c:
        return c
    if _maint_llm_enabled():
        intent = parse_intent(text, now=real_now)
        if intent:
            target_kind = intent.get("target_kind") or "list"
            machines = intent["machines"] if target_kind == "list" else []
            env_code = (intent.get("env_code") or "").strip().upper()
            if not env_code and machines:
                envs = sorted({e for e in (_env_from_machine_name(m) for m in machines) if e})
                if len(envs) == 1:
                    env_code = envs[0]
            return {
                "action": intent["action"],
                "action_dt": intent.get("action_dt"),
                "target_kind": target_kind,
                "env_code": env_code,
                "venue": intent.get("venue") or "",
                "machines": machines,
                "source": "llm",
            }
    return None


def _resolve_machines_for(c: dict[str, Any]) -> tuple[list[str], str]:
    """Machines + note for a classification (explicit list as-is, or expand the group)."""
    if c.get("machines"):
        return list(c["machines"]), ""
    if c.get("target_kind") == "all_group":
        return resolve_all_group(c.get("env_code") or "", c.get("venue") or "")
    return [], ""


def _resolve_machine_targets(text: str) -> dict[str, Any] | None:
    """
    Resolve env + machine list from free-form text (shared by maintenance set/unset and status check).

    Returns ``target_kind``, ``env_code``, ``venue``, ``machines`` (display names), ``note``.
    """
    machines = extract_machine_lines(text)
    group = detect_group_target(text)
    has_date = bool(_DATE_RE.search(text))
    target_kind = "list"
    env_code = ""
    venue = ""
    note = ""

    if (
        not machines
        and group
        and not has_date
        and not _has_all_machines_scope(text)
    ):
        refs = resolve_env_asset_refs(group.get("env_code") or "", text)
        if refs:
            return {
                "target_kind": "list",
                "env_code": _normalize_env_code(group.get("env_code") or ""),
                "venue": "",
                "machines": refs,
                "note": "",
            }

    if group and _has_all_machines_scope(text) and len(machines) < _EXPLICIT_LIST_MIN:
        env_code = _normalize_env_code(group["env_code"])
        venue = group["venue"]
        machines, note = resolve_all_group(env_code, venue)
        target_kind = "all_group"
    elif machines:
        envs = sorted({_normalize_env_code(e) for e in (_env_from_machine_name(m) for m in machines) if e})
        env_code = envs[0] if len(envs) == 1 else ""
        target_kind = "list"
    elif group:
        env_code = _normalize_env_code(group["env_code"])
        venue = group["venue"]
        machines, note = resolve_all_group(env_code, venue)
        target_kind = "all_group"
    else:
        return None

    if not machines:
        return None
    return {
        "target_kind": target_kind,
        "env_code": env_code,
        "venue": venue,
        "machines": machines,
        "note": note,
    }


def _looks_like_status_check_request(text: str) -> bool:
    """True for read-only machine status queries (not set/unset maintenance)."""
    if _looks_like_maintenance_request(text):
        return False
    t = (text or "").strip()
    if not t or t.lstrip().startswith("/"):
        return False
    if not _STATUS_CHECK_RE.search(t):
        return False
    tl = t.lower()
    return bool(
        re.search(r"\bmachines?\b|\ball\b|\bcabinets?\b|\begms?\b|机台|机器|全部|所有", tl)
        or _MACHINE_LINE_RE.search(t)
        or _MACHINE_TOKEN_RE.search(t)
        or _ENV_WORD_RE.search(t)
        or extract_machine_lines(t)
        or detect_group_target(t)
    )


def _classify_status_check(text: str) -> dict[str, Any] | None:
    """Routing classification for a status-check request."""
    if not _looks_like_status_check_request(text):
        return None
    targets = _resolve_machine_targets(text)
    if not targets:
        return None
    env_code = targets.get("env_code") or ""
    if not env_code and targets.get("machines"):
        envs = sorted(
            {_normalize_env_code(e) for e in (_env_from_machine_name(m) for m in targets["machines"]) if e}
        )
        if len(envs) == 1:
            env_code = envs[0]
    return {
        "action": "check_status",
        "target_kind": targets["target_kind"],
        "env_code": env_code,
        "venue": targets.get("venue") or "",
        "machines": targets.get("machines") or [],
        "note": targets.get("note") or "",
        "source": "rule",
    }


def _lookup_status_rows(env_code: str, machine_names: list[str]) -> tuple[list[dict], list[str]]:
    """Match machine names against ``webmachine_data.json`` PROD rows (same rules as confirm card)."""
    import smmachine

    rows = load_webmachine_rows()
    rows = [r for r in rows if str(r.get("environment") or "PROD").strip().upper() == "PROD"]
    if not rows:
        return [], list(machine_names)
    return smmachine.resolve_prod_batch_bot_targets(env_code, machine_names, rows)


def _format_status_line(m: dict) -> str:
    head = f"{m.get('belongs', '')} — {m.get('machine', '')}"
    bits: list[str] = []
    st = (m.get("status") or "").strip()
    onl = (m.get("online") or "").strip()
    if st:
        bits.append(st)
    if onl:
        bits.append(onl)
    if m.get("is_test"):
        bits.append("TEST")
    if bits:
        return f"• {head} — {' | '.join(bits)}"
    return f"• {head}"


def build_status_report(
    matched: list[dict],
    not_found: list[str],
    *,
    env_code: str = "",
    note: str = "",
) -> str:
    """Human-readable status reply (same bullet style as the maintenance confirm card)."""
    header = "Machine status:"
    if env_code:
        header = f"Machine status ({env_code}):"
    lines = [header, "", "Found Machines -"]
    if matched:
        for m in matched[:80]:
            lines.append(_format_status_line(m))
        if len(matched) > 80:
            lines.append(f"... and {len(matched) - 80} more")
    else:
        lines.append("• _(none)_")
    if not_found:
        lines.append("")
        lines.append("Not Found Machines -")
        for nf in not_found[:40]:
            lines.append(f"• {nf}")
        if len(not_found) > 40:
            lines.append(f"... and {len(not_found) - 40} more")
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def is_machine_status_check_message(original_text: str, mention_keys: Sequence[str]) -> bool:
    """True when the message is a read-only machine status query."""
    body = _strip_mentions(original_text, mention_keys)
    c = _classify_status_check(body)
    if not c:
        return False
    if c.get("machines"):
        return True
    return bool(_ENV_SITE_ALIAS.get(c.get("env_code") or ""))


def handle_machine_status_check_message(
    original_text: str,
    mention_keys: Sequence[str],
    *,
    chat_id: str,
    send_message: Callable[..., Any],
) -> tuple[bool, str | None]:
    """Resolve machines like maintenance commands; reply with status from ``webmachine_data.json``."""
    body = _strip_mentions(original_text, mention_keys)
    c = _classify_status_check(body)
    if not c:
        return False, None

    env_code = (c.get("env_code") or "").strip().upper()
    machine_names, note = _resolve_machines_for(c)
    if not machine_names:
        venue_txt = f" “{c.get('venue')}”" if c.get("venue") else ""
        return True, (
            f"⚠️ Understood a status check for **{env_code or '?'}**{venue_txt}, "
            f"but found **0** machines.\n{_data_path_hint()}"
        )

    if not env_code:
        envs = sorted({_normalize_env_code(e) for e in (_env_from_machine_name(m) for m in machine_names) if e})
        if len(envs) == 1:
            env_code = envs[0]
        else:
            return True, (
                "⚠️ Machines span multiple environments — please include the site "
                f"(e.g. `tbp`, `cp`) or paste machines from one env only."
            )

    matched, not_found = _lookup_status_rows(env_code, machine_names)
    print(
        f"[maintenanceagent] status-check env={env_code} requested={len(machine_names)} "
        f"found={len(matched)} not_found={len(not_found)} data={_last_data_path or 'NOT FOUND'}",
        flush=True,
    )
    reply = build_status_report(matched, not_found, env_code=env_code, note=note)
    send_message(chat_id, reply)
    if matched:
        try:
            import smmachine
            from prod_machine_batch import prod_batch_screenshots_enabled

            if prod_batch_screenshots_enabled():
                cap = _direct_set_unset_max()
                to_shot = matched[:cap]
                smmachine.send_machine_row_screenshots_for_chat(chat_id, to_shot, send_message)
                if len(matched) > cap:
                    send_message(
                        chat_id,
                        f"ℹ️ EGM row screenshots: first **{cap}** of **{len(matched)}** machines "
                        f"(raise `MAINT_DIRECT_SET_UNSET_MAX` for more).",
                    )
        except Exception as exc:
            print(f"[maintenanceagent] status-check screenshots failed: {exc!r}", flush=True)
    return True, None


def parse_announcement(text: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Scheduled maintenance request (has an action time). ``None`` otherwise."""
    intent = parse_intent(text, now=now)
    if not intent or not intent.get("action_dt"):
        return None
    return intent


def is_maintenance_schedule_message(original_text: str, mention_keys: Sequence[str]) -> bool:
    """True when the (mention-stripped) message is a *scheduled* maintenance announcement."""
    body = _strip_mentions(original_text, mention_keys)
    c = _classify(body)
    if not c or c.get("action_dt") is None:
        return False
    if c["target_kind"] == "list":
        return bool(c["machines"])
    return bool(_ENV_SITE_ALIAS.get(c.get("env_code") or ""))


# Max machines for direct set/unset (no confirm card). Larger lists use the confirm flow.
def _direct_set_unset_max() -> int:
    try:
        return max(1, int((os.environ.get("MAINT_DIRECT_SET_UNSET_MAX") or "10").strip()))
    except ValueError:
        return 10


_DIRECT_DISAMBIG_LLM_SYSTEM = (
    "You help an EGM duty bot clarify a machine maintenance command.\n"
    "The user's token matched **zero** or **multiple** machines in webmachine_data.json.\n"
    "Reply in 1–4 short sentences in the same language as the user (English or 中文).\n"
    "Ask which machine they meant. List candidate full display names as bullets (•).\n"
    "If nothing matched, mention that and suggest the closest names if provided.\n"
    "Do not execute anything — only clarify."
)


def _llm_direct_disambiguation_reply(
    *,
    user_text: str,
    action: str,
    env_code: str,
    ambiguous: list[dict[str, Any]],
    not_found: list[str],
    suggestions: list[str],
) -> str | None:
    """Ask the LLM to phrase a clarification question; ``None`` if LLM unavailable."""
    if not _maint_llm_enabled():
        return None
    action_label = ACTION_LABELS.get(action, action)
    parts = [
        f"User message: {user_text.strip()}",
        f"Intended action: {action_label} ({env_code})",
    ]
    if not_found:
        parts.append("Not found tokens: " + ", ".join(not_found))
    if suggestions:
        parts.append("Similar machine names in data: " + "; ".join(suggestions[:12]))
    for item in ambiguous:
        tok = item.get("token", "")
        cands = [c.get("machine", "") for c in (item.get("candidates") or [])]
        parts.append(f"Token {tok!r} matched multiple: " + "; ".join(cands))
    user = "\n".join(parts)
    payload = {
        "model": _maint_llm_model(),
        "messages": [
            {"role": "system", "content": _DIRECT_DISAMBIG_LLM_SYSTEM},
            {"role": "user", "content": user},
        ],
        "max_tokens": 350,
        "temperature": 0.2,
    }
    try:
        req = urllib.request.Request(
            f"{_maint_llm_base()}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_maint_llm_key()}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_MAINT_LLM_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        content = content.strip()
        return content or None
    except Exception as exc:
        logger.warning("direct-set/unset LLM disambiguation failed: %s", exc)
        return None


def _deterministic_disambiguation_reply(
    *,
    action: str,
    env_code: str,
    ambiguous: list[dict[str, Any]],
    not_found: list[str],
    suggestions: list[str],
) -> str:
    """Fallback clarification when the LLM is unavailable."""
    label = ACTION_LABELS.get(action, action)
    lines = [f"⚠️ Could not run **{label}** directly — please clarify which machine(s):"]
    for item in ambiguous:
        tok = item.get("token", "")
        lines.append("")
        lines.append(f"**{tok}** matched multiple machines:")
        for c in item.get("candidates") or []:
            lines.append(f"• {c.get('belongs', '')} — {c.get('machine', '')}")
    if not_found:
        lines.append("")
        lines.append("**Not found:** " + ", ".join(not_found))
        if suggestions:
            lines.append("")
            lines.append("Did you mean:")
            for s in suggestions[:8]:
                lines.append(f"• {s}")
    lines.append("")
    lines.append("Reply with the exact machine name or asset id, e.g. `@bot unset NWR2008`.")
    return "\n".join(lines)


def resolve_direct_set_unset_targets(
    env_code: str,
    machine_tokens: list[str],
) -> tuple[list[dict], list[dict[str, Any]], list[str]]:
    """
    Resolve shorthand tokens against ``webmachine_data.json``.

    Returns ``(matched, ambiguous, not_found)`` where ``ambiguous`` is
    ``[{"token": "NWR2008", "candidates": [...]}]``.
    """
    import smmachine

    rows = load_webmachine_rows()
    rows = [r for r in rows if str(r.get("environment") or "PROD").strip().upper() == "PROD"]
    matched: list[dict] = []
    ambiguous: list[dict[str, Any]] = []
    not_found: list[str] = []
    seen: set[tuple[str, str]] = set()

    for token in machine_tokens:
        hits = smmachine.resolve_prod_batch_token_hits(env_code, token, rows)
        if len(hits) == 0:
            not_found.append(token)
        elif len(hits) == 1:
            dedupe = (hits[0]["belongs"].upper(), hits[0]["machine"])
            if dedupe not in seen:
                seen.add(dedupe)
                matched.append(hits[0])
        else:
            ambiguous.append({"token": token, "candidates": hits})

    return matched, ambiguous, not_found


def _similar_machine_names(env_code: str, token: str, *, limit: int = 8) -> list[str]:
    """Fuzzy suggestions when a token matches nothing."""
    import smmachine

    rows = load_webmachine_rows()
    rows = [r for r in rows if str(r.get("environment") or "PROD").strip().upper() == "PROD"]
    key = re.sub(r"[^a-z0-9]", "", (token or "").lower())
    if not key:
        return []
    out: list[str] = []
    for r in rows:
        if not smmachine._prod_batch_row_matches_env(r, env_code):
            continue
        name = _row_display_name(r)
        if not name:
            continue
        hay = re.sub(r"[^a-z0-9]", "", name.lower())
        if key in hay or hay in key or key[-4:] in hay:
            out.append(name)
            if len(out) >= limit:
                break
    return out


def is_direct_set_unset_message(original_text: str, mention_keys: Sequence[str]) -> bool:
    """
    True for short ``set/unset <machine>`` commands that run immediately (no confirm card).

    Bulk lists, group/all scope, and scheduled announcements use the confirm flow instead.
    """
    body = _strip_mentions(original_text, mention_keys)
    if _looks_like_status_check_request(body):
        return False
    c = _classify(body)
    if not c or c.get("action_dt") is not None:
        return False
    if _DATE_RE.search(body) and _TIME_IN_TEXT_RE.search(body):
        return False
    if c.get("target_kind") != "list":
        return False
    machines = c.get("machines") or []
    if not machines or len(machines) > _direct_set_unset_max():
        return False
    env_code = (c.get("env_code") or "").strip().upper()
    if not env_code:
        envs = sorted({_normalize_env_code(e) for e in (_env_from_machine_name(m) for m in machines) if e})
        if len(envs) != 1:
            return False
        env_code = envs[0]
    return bool(_ENV_SITE_ALIAS.get(env_code))


def handle_direct_set_unset_message(
    original_text: str,
    mention_keys: Sequence[str],
    *,
    chat_id: str,
    send_message: Callable[..., Any],
    thread_root_message_id: str | None = None,
) -> tuple[bool, str | None]:
    """
    Execute ``set/unset <machine>`` immediately from ``webmachine_data.json``.

    When a token is missing or matches multiple machines, ask the user (LLM phrasing when available).
    """
    import smmachine

    body = _strip_mentions(original_text, mention_keys)
    c = _classify(body)
    if not c or c.get("action_dt") is not None:
        return False, None

    action = c["action"]
    machines_tokens = list(c.get("machines") or [])
    env_code = (c.get("env_code") or "").strip().upper()
    if not env_code:
        envs = sorted({_normalize_env_code(e) for e in (_env_from_machine_name(m) for m in machines_tokens) if e})
        if len(envs) != 1:
            return True, "⚠️ Machines span multiple environments — specify one site or use full machine names."
        env_code = envs[0]

    matched, ambiguous, not_found = resolve_direct_set_unset_targets(env_code, machines_tokens)

    if ambiguous or not_found:
        suggestions: list[str] = []
        for tok in not_found:
            suggestions.extend(_similar_machine_names(env_code, tok))
        suggestions = list(dict.fromkeys(suggestions))
        reply = _llm_direct_disambiguation_reply(
            user_text=body,
            action=action,
            env_code=env_code,
            ambiguous=ambiguous,
            not_found=not_found,
            suggestions=suggestions,
        )
        if not reply:
            reply = _deterministic_disambiguation_reply(
                action=action,
                env_code=env_code,
                ambiguous=ambiguous,
                not_found=not_found,
                suggestions=suggestions,
            )
        send_message(chat_id, reply)
        return True, None

    if not matched:
        return True, f"⚠️ No machines matched in webmachine_data.json.\n{_data_path_hint()}"

    print(
        f"[maintenanceagent] direct {action} env={env_code} machines={len(matched)} "
        f"data={_last_data_path or 'NOT FOUND'}",
        flush=True,
    )
    smmachine.start_prod_batch_job_direct(
        chat_id=chat_id,
        action=action,
        machines=matched,
        send_message=send_message,
        thread_root_message_id=thread_root_message_id,
    )
    return True, None


def is_short_set_unset_only_message(original_text: str, mention_keys: Sequence[str]) -> bool:
    """
    True when the message is only ``set`` or ``unset`` (maintenance implied) with no machine list.

    Used to reply with usage instead of falling through silently.
    """
    body = _strip_mentions(original_text, mention_keys).strip()
    if not re.fullmatch(r"(?i)(?:set|unset)\s*", body):
        return False
    return parse_action(body) is not None


def short_set_unset_usage_text(original_text: str, mention_keys: Sequence[str]) -> str:
    body = _strip_mentions(original_text, mention_keys).strip().lower()
    verb = "unset" if _UNSET_RE.search(_scrub_unset_false_positives(body)) else "set"
    return (
        f"Usage: `@bot {verb} <machine>` — defaults to **{verb} maintenance**.\n\n"
        f"Examples:\n"
        f"• `@bot {verb} nwr8237`\n"
        f"• `@bot {verb} nwr8237,nwr8238&nwr8239`\n"
        f"• `@bot {verb} maintenance` also works explicitly\n\n"
        f"Short commands (≤{_direct_set_unset_max()} machines) run **immediately** with no confirm card.\n"
        f"Larger lists still show Proceed/Cancel.\n\n"
        f"You can also paste machine names on the lines after `{verb}`."
    )


def is_maintenance_now_message(original_text: str, mention_keys: Sequence[str]) -> bool:
    """
    True for an immediate (no time) ``set/unset maintenance/test`` request — either an
    ``ALL <ENV> MACHINES <Venue>`` group, **or** an explicit machine list where the env can be
    inferred from the names (e.g. ``unset maintenance TBP8609`` → TBP, no site word needed).

    Requires a single resolvable env (so we know which backend). Multi-env lists are left to the
    prod-batch flow.
    """
    body = _strip_mentions(original_text, mention_keys)
    c = _classify(body)
    if not c or c.get("action_dt") is not None:
        return False
    # A date + time in the message is a schedule announcement — never run live set/unset.
    if _DATE_RE.search(body) and _TIME_IN_TEXT_RE.search(body):
        return False
    return bool(_ENV_SITE_ALIAS.get(c.get("env_code") or ""))


def handle_maintenance_now_message(
    original_text: str,
    mention_keys: Sequence[str],
    *,
    chat_id: str,
    send_message: Callable[..., Any],
    thread_root_message_id: str | None = None,
) -> tuple[bool, str | None]:
    """
    Expand ``ALL <ENV> MACHINES <Venue>`` to machine names (from ``webmachine_data.json``) and run
    the existing prod-batch flow (confirm card from JSON → execute on live EGM).

    Returns ``(handled, optional_reply_text)``.
    """
    import smmachine

    body = _strip_mentions(original_text, mention_keys)
    c = _classify(body)
    if not c or c.get("action_dt") is not None:
        return False, None
    env_code = (c.get("env_code") or "").strip().upper()
    site = _ENV_SITE_ALIAS.get(env_code)
    if not site:
        return False, None

    action = c["action"]
    label = ACTION_LABELS.get(action, action)
    suffix = _ACTION_CMD_SUFFIX.get(action)
    if not suffix:
        return True, f"❌ Unsupported action: {action}"

    if c.get("target_kind") == "all_group":
        venue = c.get("venue") or ""
        machines, note = resolve_all_group(env_code, venue)
        venue_txt = f" “{venue}”" if venue else ""
        print(
            f"[maintenanceagent] now-request {action} env={env_code} venue={venue!r} "
            f"resolved={len(machines)} src={c.get('source')} data={_last_data_path or 'NOT FOUND'}",
            flush=True,
        )
        if not machines:
            return True, (
                f"⚠️ I understood **{label}** for all **{env_code}**{venue_txt} machines, "
                f"but found **0** matching machines in the machine list.\n{_data_path_hint()}\n"
                f"(If the file is fine, the venue name may differ — try the exact game type, "
                f"or paste the machine names.)"
            )
    else:
        # Explicit machine list — env inferred from names; no site word needed.
        machines = c.get("machines") or []
        if not machines:
            return False, None
        print(
            f"[maintenanceagent] now-request {action} env={env_code} (explicit list, "
            f"{len(machines)} machine(s)) src={c.get('source')}",
            flush=True,
        )

    # Hand off to prod-batch confirm card (machines from webmachine_data.json — no live lookup).
    parsed_pb: dict[str, Any] = {
        "action": action,
        "env_code": env_code,
        "site": site,
    }
    smmachine._prod_batch_bot_prepare_confirm(
        parsed_pb,
        machines,
        chat_id=chat_id,
        send_message=send_message,
        thread_root_message_id=thread_root_message_id,
    )
    return True, None


def build_reminder_reason(parsed: dict[str, Any]) -> str:
    """Rich reminder text stored in the sheet ``Reason`` (starts with the maintenance marker)."""
    from reminder import MAINT_REMINDER_MARKER

    action_label = parsed["action_label"]
    action_dt: datetime = parsed["action_dt"]
    machines: list[str] = parsed["machines"]
    when_str = action_dt.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")
    env_summary = parsed.get("env_summary") or "?"
    reason = (parsed.get("reason") or "Stress Test").strip()
    header = MAINT_REMINDER_MARKER
    if reason and reason.lower() != "stress test":
        header += f" — {reason}"
    lines = [
        header,
        f"**Action:** {action_label} at **{when_str}** ({env_summary})",
        f"**Machines ({len(machines)}):**",
    ]
    lines.extend(f"• {m}" for m in machines)
    for url in parsed.get("links") or ():
        lines.append(f"Link: {url}")
    note = parsed.get("note")
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def _schedule_stress_reminder(
    *,
    body: str,
    action: str,
    action_dt: datetime,
    machines: list[str],
    note: str,
    env_code: str,
    now: datetime,
    chat_id: str,
    send_message: Callable[..., Any],
    get_token_func: Callable[[], str],
    scheduler: Any,
    target_user_id: str,
    schedule_chat_id: str | None,
) -> str:
    """
    Shared core for schedule announcements and ``/stresstest``: validate the action time,
    write the One-time sheet reminder (fires ``MAINT_LEAD_MINUTES`` early), return the reply.
    """
    import reminder

    label = ACTION_LABELS.get(action, action)
    env_codes = sorted({e for e in (_env_from_machine_name(m) for m in machines) if e})
    env_summary = "/".join(env_codes) if env_codes else (env_code or "?")
    parsed = {
        "action": action,
        "action_label": label,
        "action_dt": action_dt,
        "reminder_dt": action_dt - timedelta(minutes=MAINT_LEAD_MINUTES),
        "reason": parse_reason(body),
        "links": extract_links(body),
        "machines": machines,
        "env_summary": env_summary,
        "note": note,
    }

    reminder_dt: datetime = parsed["reminder_dt"]
    when_str = action_dt.strftime("%b %d, %Y %I:%M %p")
    if action_dt <= now:
        return (
            f"⚠️ The action time **{when_str}** is already in the past "
            f"(now is {now.strftime('%b %d, %Y %I:%M %p')}). Nothing scheduled — "
            f"if this still needs doing, set maintenance now, or re-send with a future date/time."
        )
    if reminder_dt <= now:
        return (
            f"⚠️ The action time **{when_str}** is less than {MAINT_LEAD_MINUTES} min away, "
            f"so the {MAINT_LEAD_MINUTES}-min-early reminder would land in the past. "
            f"Nothing scheduled — please set maintenance now if needed."
        )

    reason_text = build_reminder_reason(parsed)
    try:
        result = reminder.add_sheet_reminder(
            start_raw=action_dt.strftime("%Y/%m/%d"),
            end_raw=action_dt.strftime("%Y/%m/%d"),
            time_raw=reminder_dt.strftime("%H:%M"),
            reason=reason_text,
            when_labels=["One time"],
            get_token_func=get_token_func,
            scheduler=scheduler,
            send_func=send_message,
            chat_id=chat_id,
            target_user_id=target_user_id,
            schedule_chat_id=schedule_chat_id,
        )
    except Exception as e:  # noqa: BLE001
        return f"❌ Failed to schedule maintenance reminder: {e}"

    if result:
        # add_sheet_reminder returns a non-empty string only on failure.
        return result

    summary = (
        f"✅ Scheduled stress-test reminder for **{reminder_dt.strftime('%b %d, %Y %I:%M %p')}** "
        f"({MAINT_LEAD_MINUTES} min before {action_dt.strftime('%I:%M %p')}).\n"
        f"• Action: {parsed['action_label']} ({parsed.get('env_summary')})\n"
        f"• Machines: {len(parsed['machines'])}"
    )
    if parsed.get("note"):
        summary += f"\n{parsed['note']}"
    return summary


def handle_maintenance_schedule_message(
    original_text: str,
    mention_keys: Sequence[str],
    *,
    chat_id: str,
    send_message: Callable[..., Any],
    get_token_func: Callable[[], str],
    scheduler: Any,
    target_user_id: str,
    schedule_chat_id: str | None = None,
) -> tuple[bool, str | None]:
    """
    Parse the announcement and schedule a one-time reminder ``MAINT_LEAD_MINUTES`` before the
    action time. Returns ``(handled, optional_reply_text)``.
    """
    body = _strip_mentions(original_text, mention_keys)
    now = datetime.now()
    c = _classify(body, now=now)
    if not c or c.get("action_dt") is None:
        return False, None

    env_code = (c.get("env_code") or "").strip().upper()
    if c["target_kind"] == "all_group" and not _ENV_SITE_ALIAS.get(env_code):
        return False, None
    machines, note = _resolve_machines_for(c)
    action = c["action"]
    label = ACTION_LABELS.get(action, action)
    action_dt: datetime = c["action_dt"]
    when_str_full = action_dt.strftime("%b %d, %Y %I:%M %p")
    if not machines:
        venue_txt = f" “{c.get('venue')}”" if c.get("venue") else ""
        return True, (
            f"⚠️ Understood a scheduled **{label}** for all **{env_code}**{venue_txt} machines at "
            f"**{when_str_full}**, but found **0** matching machines. {_data_path_hint()}\n"
            f"Nothing scheduled."
        )
    return True, _schedule_stress_reminder(
        body=body,
        action=action,
        action_dt=action_dt,
        machines=machines,
        note=note,
        env_code=env_code,
        now=now,
        chat_id=chat_id,
        send_message=send_message,
        get_token_func=get_token_func,
        scheduler=scheduler,
        target_user_id=target_user_id,
        schedule_chat_id=schedule_chat_id,
    )


# ---------------------------------------------------------------------------
# /stresstest — explicit "parse this announcement" command (always replies)
# ---------------------------------------------------------------------------
STRESSTEST_USAGE = (
    "Usage: `/stresstest` then paste the announcement (same or next line).\n\n"
    f"The AI reads the **set maintenance/test date & time** and the **machine list**, then "
    f"schedules a one-time reminder **{MAINT_LEAD_MINUTES} min before** that time.\n\n"
    "Example:\n"
    "/stresstest\n"
    "4 DFDC machines are subject for Stress Test on July 15, 2026 at 11:00 AM. "
    "Please set to maintain status and test mode July 14, 2026 at 2145H\n"
    "- WF8109 ( 5 Treasures )\n"
    "- WF8112 ( 5 Treasures )"
)


def classify_stresstest_announcement(
    body: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """
    Classification for an explicit ``/stresstest`` announcement.

    Same parse as the auto-detection path (deterministic rules first, LLM fallback), but the
    command itself already declares the intent, so when the generic maintenance gate abstains
    this falls back to *set maintenance and test* + machine tokens + action datetime from the
    raw text.
    """
    real_now = now or datetime.now()
    c = _classify(body, now=real_now)
    if c is None:
        machines = extract_machine_lines(body)
        if not machines:
            return None
        envs = sorted(
            {_normalize_env_code(e) for e in (_env_from_machine_name(m) for m in machines) if e}
        )
        c = {
            "action": "set_both",
            "action_dt": parse_action_datetime(body, now=real_now),
            "target_kind": "list",
            "env_code": envs[0] if len(envs) == 1 else "",
            "venue": "",
            "machines": machines,
            "source": "rule",
        }
    if not c.get("action"):
        c["action"] = "set_both"
    return c


def handle_stresstest_command(
    body: str,
    *,
    chat_id: str,
    send_message: Callable[..., Any],
    get_token_func: Callable[[], str],
    scheduler: Any,
    target_user_id: str,
    schedule_chat_id: str | None = None,
) -> str:
    """
    Handle ``/stresstest <announcement>`` — always returns a reply.

    Parses the pasted stress-test notice (machines + the set-maintenance action time; the
    stress-test event time itself is ignored) and schedules the one-time reminder
    ``MAINT_LEAD_MINUTES`` min before via the reminder sheet. On success the reply echoes the
    parsed machines and times so the duty staff can verify what the AI read.
    """
    text = (body or "").strip()
    if not text:
        return STRESSTEST_USAGE
    now = datetime.now()
    c = classify_stresstest_announcement(text, now=now)
    machines: list[str] = []
    note = ""
    all_group_no_env = bool(
        c
        and c.get("target_kind") == "all_group"
        and not _ENV_SITE_ALIAS.get((c.get("env_code") or "").strip().upper())
    )
    if c and not all_group_no_env:
        machines, note = _resolve_machines_for(c)
    action_dt = c.get("action_dt") if c else None

    problems: list[str] = []
    if all_group_no_env:
        problems.append(
            "• **Environment** — the announcement targets *all machines* but no site "
            "(WF / NWR / NCH / TBR / TBP / MDR / DHS / CP) could be determined."
        )
    elif not machines:
        hint = _data_path_hint() if (c and c.get("target_kind") == "all_group") else ""
        problems.append(
            "• **Machines** — none found. Paste one machine per line, e.g. `WF8109 ( 5 Treasures )`."
            + (f"\n  {hint}" if hint else "")
        )
    if not action_dt:
        problems.append(
            "• **Set-maintenance time** — no date + time found, e.g. `July 14, 2026 at 2145H`."
        )
    if problems:
        return (
            "⚠️ Could not schedule the stress-test reminder:\n"
            + "\n".join(problems)
            + "\n\n"
            + STRESSTEST_USAGE
        )

    print(
        f"[maintenanceagent] /stresstest {c['action']} at {action_dt:%Y-%m-%d %H:%M} "
        f"machines={len(machines)} src={c.get('source')}",
        flush=True,
    )
    reply = _schedule_stress_reminder(
        body=text,
        action=c["action"],
        action_dt=action_dt,
        machines=machines,
        note=note,
        env_code=(c.get("env_code") or "").strip().upper(),
        now=now,
        chat_id=chat_id,
        send_message=send_message,
        get_token_func=get_token_func,
        scheduler=scheduler,
        target_user_id=target_user_id,
        schedule_chat_id=schedule_chat_id,
    )
    if reply.startswith("✅"):
        shown = machines[:30]
        lines = [reply, "", f"**Machines ({len(machines)}):**"]
        lines.extend(f"• {m}" for m in shown)
        if len(machines) > 30:
            lines.append(f"… and {len(machines) - 30} more")
        # A *set* time normally precedes the stress-test event. If the notice mentions an
        # earlier datetime, the set instruction likely had no date of its own and borrowed
        # the event's date (e.g. "set … at 2145H" + "Stress Test July 15 11:00 AM").
        if str(c["action"]).startswith("set"):
            earlier = [dt for _p, dt in _all_datetime_candidates(text, now=now) if dt < action_dt]
            if earlier:
                other = max(earlier)
                lines.append(
                    f"\n⚠️ The announcement also mentions **{other.strftime('%b %d, %Y %I:%M %p')}**, "
                    f"which is *earlier* than the set-maintenance time above — double-check the "
                    f"reminder date. If it is wrong, `/deletereminder` it and re-send the notice "
                    f"with an explicit date (e.g. `July 14, 2026 at 2145H`)."
                )
        reply = "\n".join(lines)
    return reply
