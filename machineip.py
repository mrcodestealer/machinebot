#!/usr/bin/env python3
"""
OSM Machine List (Lark wiki sheet) -> machine IPs. The PRIMARY IP source.

One wiki node ("OSM Machine List") holds one tab per venue — CP, NCH, DYB, DHS,
WF, NWR, TBR, TBP — every tab with the same columns:

    No. | Status | Zone | Asset ID | ... | Mini PC | Main Encoder | Top Encoder | CCTV | Lucky Link Mini PC | ...

``/encoder`` ``/main`` ``/pool`` ``/cctv`` ``/minipc`` and a bare "@bot NWR2205"
answer from this sheet FIRST. OSM-Watch's IP Audit (``latestmachineip.json``) is
the fallback — used for a machine the sheet has no row for, or for the one stream
whose cell is blank/``N/A``. TRTC room/user/sig still come from OSM-Watch; only
the IP address changes source.

Two tab groups are ignored on purpose: any title containing "(Lab)" and the
"Template" tab. Those are staging copies, not the live floor.

"Top Encoder" is this sheet's name for the POOL stream — the same mapping the IP
Audit uses (``top`` -> ``pool``), so ``/pool`` reads the Top Encoder column.

Machine naming follows the venue modules (nch.py, tbp.py, cp.py, ...): the Asset
ID *is* the machine name, venue-prefixed when the sheet writes it bare —
``1084`` in the NCH tab is ``NCH1084``, ``DHS 3049`` is ``DHS3049``, ``DYB0021``
and CP's ``OSM077`` are already complete. A query is matched on venue + asset
digits, so ``dyb21``, ``DYB0021`` and ``OSMDYB0001``-style aliases all land on the
right row, and a bare ``8527`` is looked up in every tab's Asset ID column.

CLI:
    python machineip.py                   # refresh, then print a per-tab summary
    python machineip.py NWR2205 dyb21     # look up machine(s)
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

_ROOT_DIR = Path(__file__).resolve().parent

LARK_BASE = os.getenv("LARK_BASE_URL", "https://open.larksuite.com").rstrip("/")

# The bot's own app by default. MACHINE_IP_APP_ID/SECRET exist for the same reason
# mdr.py has its own pair: the sheet may only be shared with a different app, and
# waiting on a permissions change should not mean editing code.
APP_ID = os.getenv("MACHINE_IP_APP_ID") or os.getenv("APP_ID")
APP_SECRET = os.getenv("MACHINE_IP_APP_SECRET") or os.getenv("APP_SECRET")

# The wiki node from the link people share. The underlying spreadsheet token is
# resolved from it at runtime (wiki nodes and sheet tokens are different things),
# unless MACHINE_IP_SPREADSHEET_TOKEN pins the sheet directly.
WIKI_TOKEN = os.getenv("MACHINE_IP_WIKI_TOKEN", "NmsFwjzOcibBJckhsqjlRS2kgrb").strip()
SPREADSHEET_TOKEN_ENV = os.getenv("MACHINE_IP_SPREADSHEET_TOKEN", "").strip()

DATA_FILE = _ROOT_DIR / os.getenv("MACHINE_IP_DATA_FILE", "machineiplist.json")


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    """Numeric setting that tolerates a typo — same guard osmwatch's own knobs use.

    A bad value here would otherwise raise at import time, which the caller sees
    only as "sheet source unavailable": the primary IP source silently off
    because someone left a stray character in .env."""
    try:
        return max(minimum, int(str(os.getenv(name, "")).strip() or default))
    except ValueError:
        print(f"[machineip] {name} is not a number — using {default}", flush=True)
        return default


TTL_SEC = _int_env("MACHINE_IP_TTL_SEC", 600, minimum=0)
HTTP_TIMEOUT = _int_env("MACHINE_IP_HTTP_TIMEOUT", 30)

SOURCE_TAG = "sheet"          # what lands in info["ip_source"]
SOURCE_LABEL = "OSM Machine List sheet"

# --- which columns carry which stream -------------------------------------
# Keys match OSM-Watch's stream types so a sheet row can be overlaid straight
# onto an /encoder entry; "minipc*" are sheet-only (the IP Audit has no such row).
IP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("main", "Main Encoder"),
    ("pool", "Top Encoder"),
    ("cctv", "CCTV"),
    ("minipc", "Mini PC"),
    ("minipc_lucky", "Lucky Link Mini PC"),
)
ASSET_COLUMN = "Asset ID"
STATUS_COLUMN = "Status"

# Cells that mean "we don't have one" — treated as missing so the lookup falls
# through to OSM-Watch instead of printing "N/A" as if it were an address.
_BLANKISH = {"", "-", "--", "—", "N/A", "NA", "N.A.", "NIL", "NONE", "TBA", "TBD", "?"}
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

# Tabs to skip: staging copies of the real floor.
_IGNORE_EXACT = {t.strip().upper() for t in os.getenv("MACHINE_IP_IGNORE_TABS", "Template").split(",") if t.strip()}
_IGNORE_CONTAINS = tuple(
    t.strip().upper() for t in os.getenv("MACHINE_IP_IGNORE_TAB_MARKERS", "(Lab)").split(",") if t.strip()
)

# Prefix aliases, same table smmachine.py uses: these denote the SAME venue, so a
# spelling difference is not a different machine. CP's own naming is OSM<number>
# (see cp.py), which is why OSM maps to CP.
_VENUE_ALIASES = {"OSM": "CP", "WINFORD": "WF", "WIN": "WF", "NP": "NWR", "NC": "NCH", "NEW": "NCH"}
_KEY_RE = re.compile(r"^([A-Z]+)?0*(\d+)$")


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def enabled() -> bool:
    """Sheet-first IP resolution can be switched off without a code change."""
    return _truthy(os.getenv("MACHINE_IP_SHEET", "1"))


# ---------------------------------------------------------------------------
# machine-name canonicalisation
# ---------------------------------------------------------------------------
def canon_key(name: object, default_venue: str | None = None) -> str | None:
    """``VENUE`` + asset digits with leading zeros dropped — the join key.

    ``DYB0021``, ``dyb21`` and ``OSMDYB0021`` all become ``DYB21``; CP's ``OSM077``
    and ``OSMCP77`` both become ``CP77``; a bare ``1084`` becomes ``NCH1084`` only
    when the caller supplies the tab it came from. Returns ``None`` for anything
    that isn't a machine token, and ``"?"`` + digits for a bare number with no
    venue — the caller then searches every venue for those digits.
    """
    s = re.sub(r"[^A-Za-z0-9]", "", str(name or "")).upper()
    if not s:
        return None
    # OSM<letters…> is an alias prefix (OSMDYB0001 -> DYB0001). OSM<digits> is
    # CP's own asset naming, so it must NOT be stripped — see _strip_osm_prefix
    # in osmwatch.py, which draws the same line.
    if s.startswith("OSM") and any(c.isalpha() for c in s[3:]):
        s = s[3:]
    m = _KEY_RE.match(s)
    if not m:
        return None
    prefix, digits = m.group(1), m.group(2)
    venue = _VENUE_ALIASES.get(prefix, prefix) if prefix else (default_venue or "").upper()
    return f"{venue or '?'}{digits}"


def _display_name(asset_id: str, venue: str) -> str:
    """Machine name as the venue modules print it: ``NCH1084``, ``DHS3049``, ``OSM077``."""
    alnum = re.sub(r"[^A-Za-z0-9]", "", asset_id or "").upper()
    if not alnum:
        return venue
    return alnum if any(c.isalpha() for c in alnum) else f"{venue}{alnum}"


def _flat_cell(cell: object) -> str:
    """Lark cells arrive as a scalar or a list of segments — flatten either."""
    if cell is None:
        return ""
    if isinstance(cell, (str, int, float)):
        return str(cell).strip()
    if isinstance(cell, list):
        parts = []
        for seg in cell:
            if isinstance(seg, dict):
                parts.append(str(seg.get("text") or seg.get("link") or ""))
            else:
                parts.append(str(seg))
        return "".join(parts).strip()
    return str(cell).strip()


def _clean_ip(raw: object) -> str:
    """An IPv4 address, or ``""`` for blank / ``N/A`` / anything that isn't one.

    Non-address text is dropped rather than shown: the point of a blank cell is
    that the answer comes from OSM-Watch instead, and "TBD" is not an address.
    """
    v = _flat_cell(raw)
    if v.upper() in _BLANKISH:
        return ""
    if not _IPV4_RE.match(v):
        return ""
    return "" if any(int(o) > 255 for o in v.split(".")) else v


def _tab_ignored(title: str) -> bool:
    t = (title or "").strip().upper()
    return t in _IGNORE_EXACT or any(marker in t for marker in _IGNORE_CONTAINS)


# ---------------------------------------------------------------------------
# Lark API
# ---------------------------------------------------------------------------
_TOKEN_CACHE: dict[str, object] = {"token": "", "exp": 0.0}
_TOKEN_LOCK = threading.Lock()


def _tenant_token() -> str:
    """Tenant access token, cached until shortly before it expires."""
    with _TOKEN_LOCK:
        now = time.time()
        if _TOKEN_CACHE["token"] and now < float(_TOKEN_CACHE["exp"]):
            return str(_TOKEN_CACHE["token"])
        if not APP_ID or not APP_SECRET:
            raise RuntimeError("APP_ID / APP_SECRET missing (set them in .env)")
        r = requests.post(
            f"{LARK_BASE}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": APP_ID, "app_secret": APP_SECRET},
            timeout=HTTP_TIMEOUT,
        )
        data = r.json()
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise RuntimeError(f"tenant token failed: {data}")
        _TOKEN_CACHE["token"] = data["tenant_access_token"]
        _TOKEN_CACHE["exp"] = now + max(60, int(data.get("expire", 7200)) - 300)
        return str(_TOKEN_CACHE["token"])


_SS_CACHE: dict[str, object] = {"token": "", "ts": 0.0}


def spreadsheet_token(token: str | None = None) -> str:
    """Underlying spreadsheet token for the wiki node (cached for the process).

    A wiki link carries a *node* token; the Sheets API needs the object token
    behind it. Same resolution dutybot's ai_duty.py does for its wiki sheet.
    """
    if SPREADSHEET_TOKEN_ENV:
        return SPREADSHEET_TOKEN_ENV
    if _SS_CACHE["token"] and (time.time() - float(_SS_CACHE["ts"])) < 86400:
        return str(_SS_CACHE["token"])
    if not WIKI_TOKEN:
        raise RuntimeError("MACHINE_IP_WIKI_TOKEN is empty and no MACHINE_IP_SPREADSHEET_TOKEN set")
    tok = token or _tenant_token()
    r = requests.get(
        f"{LARK_BASE}/open-apis/wiki/v2/spaces/get_node",
        params={"token": WIKI_TOKEN},
        headers={"Authorization": f"Bearer {tok}"},
        timeout=HTTP_TIMEOUT,
    )
    data = r.json()
    node = ((data.get("data") or {}).get("node") or {})
    obj = str(node.get("obj_token") or "")
    if data.get("code") != 0 or not obj:
        raise RuntimeError(
            f"wiki node {WIKI_TOKEN} did not resolve to a spreadsheet: {data} "
            "(is the doc shared with this Lark app?)"
        )
    _SS_CACHE["token"], _SS_CACHE["ts"] = obj, time.time()
    return obj


def _col_letter(n: int) -> str:
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _list_tabs(token: str, ss: str) -> list[dict]:
    r = requests.get(
        f"{LARK_BASE}/open-apis/sheets/v3/spreadsheets/{ss}/sheets/query",
        headers={"Authorization": f"Bearer {token}"},
        timeout=HTTP_TIMEOUT,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"sheets/query failed: {data}")
    tabs = []
    for s in (data.get("data") or {}).get("sheets") or []:
        grid = s.get("grid_properties") or {}
        tabs.append({
            "sheet_id": s.get("sheet_id") or "",
            "title": (s.get("title") or "").strip(),
            "rows": int(grid.get("row_count") or 0),
            "cols": int(grid.get("column_count") or 0),
        })
    return tabs


def _read_grid(token: str, ss: str, tab: dict) -> list[list[str]]:
    rng = f"{tab['sheet_id']}!A1:{_col_letter(max(1, tab['cols']))}{max(1, tab['rows'])}"
    r = requests.get(
        f"{LARK_BASE}/open-apis/sheets/v2/spreadsheets/{ss}/values/{rng}",
        params={"valueRenderOption": "FormattedValue"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=max(HTTP_TIMEOUT, 60),
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"values read failed for {tab['title']}: {data}")
    rows = ((data.get("data") or {}).get("valueRange") or {}).get("values") or []
    return [[_flat_cell(c) for c in row] for row in rows]


# ---------------------------------------------------------------------------
# snapshot building
# ---------------------------------------------------------------------------
def _header_index(header: list[str]) -> dict[str, int]:
    """Column name -> index, matched case/space-insensitively.

    Resolved per tab from the header row, never by position: NCH and DYB have one
    column fewer than the others (no "Game Type"), so fixed indices would read
    the wrong cells for two whole venues.
    """
    idx: dict[str, int] = {}
    for i, cell in enumerate(header):
        key = re.sub(r"\s+", " ", str(cell or "")).strip().lower()
        if key and key not in idx:
            idx[key] = i
    return idx


def _tab_entries(title: str, grid: list[list[str]]) -> tuple[dict[str, dict], list[str]]:
    """One tab -> ``({canon_key: entry}, warnings)``."""
    warnings: list[str] = []
    if not grid:
        return {}, [f"{title}: empty"]
    venue = title.strip().upper()
    header = grid[0]
    idx = _header_index(header)
    asset_i = idx.get(ASSET_COLUMN.lower())
    if asset_i is None:
        return {}, [f"{title}: no '{ASSET_COLUMN}' column"]
    status_i = idx.get(STATUS_COLUMN.lower())
    cols = [(typ, idx.get(name.lower()), name) for typ, name in IP_COLUMNS]
    for typ, i, name in cols:
        if i is None:
            warnings.append(f"{title}: no '{name}' column")

    out: dict[str, dict] = {}
    for row_no, row in enumerate(grid[1:], start=2):
        if asset_i >= len(row):
            continue
        asset_id = row[asset_i].strip()
        if not asset_id:
            continue
        key = canon_key(asset_id, default_venue=venue)
        if not key or key.startswith("?"):
            warnings.append(f"{title} row {row_no}: unparsable Asset ID {asset_id!r}")
            continue
        types: dict[str, dict] = {}
        for typ, i, _name in cols:
            if i is None or i >= len(row):
                continue
            ip = _clean_ip(row[i])
            if ip:
                types[typ] = {"ip": ip, "ip_source": SOURCE_TAG, "sheet_tab": venue}
        entry = {
            "machine": _display_name(asset_id, venue),
            "asset_id": asset_id,
            "venue": venue,
            "sheet_status": (row[status_i].strip() if status_i is not None and status_i < len(row) else ""),
            "sheet_row": row_no,
            "types": types,
        }
        prev = out.get(key)
        if prev is None:
            out[key] = entry
            continue
        # The sheet does carry the odd repeated Asset ID (two cabinets typed with
        # the same id, different serials and IPs). First row wins so the answer is
        # stable, and the clash is recorded so the card can say so out loud rather
        # than quietly picking one.
        prev.setdefault("dupe_rows", []).append(row_no)
        if {t: i["ip"] for t, i in types.items()} != {t: i["ip"] for t, i in (prev.get("types") or {}).items()}:
            prev["dupe_conflict"] = True
    return out, warnings


def _build_snapshot() -> dict:
    token = _tenant_token()
    ss = spreadsheet_token(token)
    tabs = _list_tabs(token, ss)
    used, skipped, warnings = [], [], []
    machines: dict[str, dict] = {}
    for tab in tabs:
        if not tab["title"] or not tab["sheet_id"]:
            continue
        if _tab_ignored(tab["title"]):
            skipped.append(tab["title"])
            continue
        try:
            grid = _read_grid(token, ss, tab)
        except Exception as e:
            # One unreadable tab must not cost us the other seven venues.
            warnings.append(f"{tab['title']}: read failed ({e})")
            continue
        entries, warns = _tab_entries(tab["title"], grid)
        warnings.extend(warns)
        used.append({"title": tab["title"], "sheet_id": tab["sheet_id"], "machines": len(entries)})
        for key, entry in entries.items():
            # First tab wins on a cross-tab clash (same venue+digits in two tabs);
            # the canon key includes the venue, so this is rare by construction.
            machines.setdefault(key, entry)
    if not machines:
        raise RuntimeError(f"no machines read from the sheet (tabs seen: {[t['title'] for t in tabs]})")
    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": f"{LARK_BASE}/open-apis/sheets (wiki {WIKI_TOKEN})",
        "spreadsheet_token": ss,
        "tabs": used,
        "skipped_tabs": skipped,
        "warnings": warnings,
        "count": len(machines),
        "machines": machines,
    }


# ---------------------------------------------------------------------------
# cache (memory + disk)
# ---------------------------------------------------------------------------
# Two locks on purpose. _CACHE_LOCK only ever guards a few dict reads and writes
# and is NEVER held across a request; _FETCH_LOCK serialises the sheet read itself.
# Holding one lock over ~9 HTTP calls would freeze every /main, /cctv and @bot tag
# in the bot for as long as Lark took to answer.
_CACHE: dict[str, object] = {"snap": None, "ts": 0.0, "fail_ts": 0.0, "error": "", "busy": False}
_CACHE_LOCK = threading.Lock()
_FETCH_LOCK = threading.Lock()
_FAIL_BACKOFF_SEC = 60


def _persist(snapshot: dict) -> None:
    # Atomic write, same reason osmwatch.py does it: a refresh on one thread must
    # never hand a half-written file to a command running on another.
    try:
        tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, DATA_FILE)
    except OSError as e:
        print(f"[machineip] could not save {DATA_FILE.name}: {e!r}", flush=True)


def _load_disk() -> dict | None:
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("machines") else None


def load_machine_ips(*, force: bool = False) -> dict:
    """The current snapshot — ``{}`` when the sheet has never been read.

    Fresh copy in memory, ``machineiplist.json`` behind it, and a re-read at most
    every ``MACHINE_IP_TTL_SEC``. Three things keep a slow or broken sheet from
    becoming a slow or broken bot: a failed refresh keeps serving the last good
    snapshot (a stale IP beats no answer) and is retried after a short backoff; a
    command that arrives while another thread is already reading the sheet gets
    the stale snapshot instead of queueing behind the API call; and the read
    happens outside the cache lock so it can never block a reader.
    """
    if not enabled():
        return {}
    with _CACHE_LOCK:
        if _CACHE["snap"] is None:
            disk = _load_disk()
            if disk:
                # Age unknown, so it counts as due for a refresh — but it can be
                # served immediately, which is what makes a restart cheap.
                _CACHE["snap"], _CACHE["ts"] = disk, 0.0
        snap = _CACHE["snap"]
        now = time.time()
        if not force:
            # Only the TTL check needs something cached. The other two must apply
            # to an EMPTY cache as well: on a host where the sheet has never been
            # readable there is no snapshot to age, and a caller can ask many
            # times for one chat command — /encoder resolves the sheet for every
            # machine it matched. Without these, that is one failed sheet read
            # per match.
            if snap is not None and (now - float(_CACHE["ts"])) < TTL_SEC:
                return snap
            if (now - float(_CACHE["fail_ts"])) < _FAIL_BACKOFF_SEC:
                return snap or {}
            if _CACHE["busy"]:
                return snap or {}
        _CACHE["busy"] = True
    try:
        with _FETCH_LOCK:
            with _CACHE_LOCK:
                snap, ts = _CACHE["snap"], float(_CACHE["ts"])
                # Another thread may have finished the read while we waited for
                # the lock; on a cold start several commands arrive at once.
                if snap is not None and not force and (time.time() - ts) < TTL_SEC:
                    return snap
            built = _build_snapshot()
            # Published while the fetch lock is still held, so the next waiter
            # sees it on the check above instead of reading the sheet again.
            with _CACHE_LOCK:
                _CACHE.update({"snap": built, "ts": time.time(), "fail_ts": 0.0, "error": ""})
    except Exception as e:
        with _CACHE_LOCK:
            _CACHE["fail_ts"], _CACHE["error"] = time.time(), f"{e}"
            stale = _CACHE["snap"]
        print(f"[machineip] refresh failed: {e!r}", flush=True)
        return stale or {}
    finally:
        with _CACHE_LOCK:
            _CACHE["busy"] = False
    _persist(built)
    print(
        f"[machineip] {built['count']} machines from {len(built['tabs'])} tabs "
        f"({', '.join(t['title'] for t in built['tabs'])}); skipped {built['skipped_tabs'] or 'none'}",
        flush=True,
    )
    return built


def refresh() -> dict:
    """Force a re-read; raises when the sheet could not be read.

    ``load_machine_ips`` deliberately answers with the last good snapshot when a
    refresh fails — but ``/iprefresh`` is the one moment someone is explicitly
    asking whether the sheet is readable, and reporting a stale count as success
    there would hide exactly what they asked about. The check is by identity: a
    successful build is the only thing that advances the cache's read time, so
    that is the signal — not the returned snapshot, which may be a copy loaded
    from disk on this very call and would otherwise look like a fresh read."""
    with _CACHE_LOCK:
        before_ts = float(_CACHE["ts"])
    snap = load_machine_ips(force=True)
    with _CACHE_LOCK:
        after_ts, err = float(_CACHE["ts"]), str(_CACHE["error"] or "")
    if not snap or after_ts <= before_ts:
        raise RuntimeError(err or "the sheet could not be read")
    return snap


def prewarm() -> None:
    """Best-effort warm-up at startup so the first lookup isn't the slow one."""
    try:
        load_machine_ips()
    except Exception as e:
        print(f"[machineip] prewarm failed: {e!r}", flush=True)


def last_error() -> str:
    return str(_CACHE.get("error") or "")


def updated_at() -> str:
    return str((load_machine_ips() or {}).get("updated_at") or "")


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------
def _copy_entry(entry: dict) -> dict:
    """A private copy of a cached entry, nested stream dicts included.

    Callers overlay these onto their own records (osmwatch merges them into an
    /encoder entry), so handing out the cached objects themselves would let one
    command's edit show up in the next command's answer."""
    out = dict(entry)
    out["types"] = {t: dict(info) for t, info in (entry.get("types") or {}).items()}
    if entry.get("dupe_rows"):
        out["dupe_rows"] = list(entry["dupe_rows"])
    return out


def _key_digits(key: str) -> str:
    """``NWR2205`` -> ``2205`` — the venue-less half of a canon key."""
    return re.sub(r"^[A-Z?]+", "", key or "")


def entry_for(name: object) -> dict | None:
    """Sheet row for one machine name, matched on venue + asset digits.

    A bare number resolves only when exactly one venue uses it: CP and DYB share
    the 77-181 range, and answering with the wrong venue's machine is worse than
    saying nothing (the caller then shows both matches instead).
    """
    machines = (load_machine_ips() or {}).get("machines") or {}
    key = canon_key(name)
    if not key:
        return None
    if not key.startswith("?"):
        found = machines.get(key)
        return _copy_entry(found) if found else None
    digits = _key_digits(key)
    hits = [e for k, e in machines.items() if _key_digits(k) == digits]
    return _copy_entry(hits[0]) if len(hits) == 1 else None


def types_for(name: object) -> dict:
    """``{type: {ip, ip_source, sheet_tab}}`` for a machine — ``{}`` when absent."""
    entry = entry_for(name)
    return dict((entry or {}).get("types") or {})


def match(tokens: list[str]) -> dict[str, dict]:
    """Query tokens -> ``{canon_key: entry}``.

    Exact venue+digits first (so ``2205`` finds NWR2205 and TBR2010 finds only
    TBR), then a substring pass over the machine names for partial tokens like
    ``dyb`` — the permissive behaviour /encoder has always had, kept as a
    fallback rather than the first move.
    """
    machines = (load_machine_ips() or {}).get("machines") or {}
    if not machines:
        return {}
    out: dict[str, dict] = {}
    for tok in tokens or []:
        key = canon_key(tok)
        if key and not key.startswith("?"):
            if key in machines:
                out.setdefault(key, _copy_entry(machines[key]))
                continue
        elif key:
            digits = _key_digits(key)
            hit = False
            for k, e in machines.items():
                if _key_digits(k) == digits:
                    out.setdefault(k, _copy_entry(e))
                    hit = True
            if hit:
                continue
        needle = re.sub(r"[^A-Za-z0-9]", "", str(tok or "")).upper()
        if not needle:
            continue
        for k, e in machines.items():
            name = re.sub(r"[^A-Za-z0-9]", "", e.get("machine") or "").upper()
            if needle in name or needle in k:
                out.setdefault(k, _copy_entry(e))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli(argv: list[str]) -> int:
    try:
        snap = refresh()
    except Exception as e:
        print(f"❌ could not read the sheet: {e}")
        stale = load_machine_ips()
        if stale:
            print(f"   (still holding the copy read at {stale.get('updated_at')})")
        return 1
    if not argv:
        print(f"OSM Machine List — {snap.get('count', 0)} machines · updated {snap.get('updated_at')}")
        print(f"spreadsheet: {snap.get('spreadsheet_token')}")
        for t in snap.get("tabs") or []:
            print(f"  {t['title']:<6} {t['machines']:>5} machines  ({t['sheet_id']})")
        print(f"  skipped: {', '.join(snap.get('skipped_tabs') or []) or 'none'}")
        for w in snap.get("warnings") or []:
            print(f"  ⚠ {w}")
        return 0
    for tok in argv:
        hits = match([tok])
        if not hits:
            print(f"{tok}: not in the sheet (would fall back to OSM-Watch)")
            continue
        for key, e in hits.items():
            streams = "  ".join(f"{t}={i['ip']}" for t, i in e["types"].items())
            dupe = f"  ⚠ also rows {e['dupe_rows']}" if e.get("dupe_rows") else ""
            print(f"{tok} -> {e['machine']} [{e['venue']} row {e['sheet_row']} · {e['sheet_status']}] {streams}{dupe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli([a for a in sys.argv[1:] if a.strip()]))
