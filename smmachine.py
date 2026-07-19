#!/usr/bin/env python3
"""
SM machine list — login routing matches ``checkcredit.py`` backends; tick table row checkboxes with pagination.

Usage::

    python3 smmachine.py nwr
    1932
    NCH1933
    nch1922
    <press Enter on an empty line to finish — or press Ctrl+D>

Or one line (no stdin needed)::

    python3 smmachine.py nwr 1932 NCH1933 nch1922

**Batch maintenance + test** (same as webapp ``set_both``; opens a **headed** browser by default)::

    python3 smmachine.py maintenancetest nch1422
    python3 smmachine.py maintenancetest "Dragons Trio-NCH1462"

Optional remark: ``SM_BATCH_REMARK=your note`` (max 100 chars on the EGM dialog).

**Batch toolbar dry-run** (maintenance/test buttons only; opens dialog then **Cancel** — never Save)::

    python3 smmachine.py batchbuttontest
    python3 smmachine.py batchbuttontest nch cp wf

Tests: ``BatchMaintenance``, ``BatchTest``, ``BatchStart Using``, ``BatchTestCancel`` only
(ignores ``BatchKick Out``, ``Sync DB Config``, …).

First argument is a **site alias** (which backend / login to open):

- ``nwr``, ``np`` → NP (``backend-np``), synthetic route ``NWR0001``
- ``nch``, ``nc``, ``new`` → NCH (``backend-nc``)
- **Check status (read-only):** alias suffix ``cs``. Groups found machines under headings like ``Machine in online, maintain, no test mode`` then lists names; **only non-empty groups** are printed. **Test** = ``span.test`` or ``(TEST)`` in text. **Not found** section only if any request is missing.
- ``tbr`` → TBR (``backend-tbr``)
- ``tbp``, ``mdr``, ``dhs``, ``cp``, ``osm``, ``wf``, ``winford`` → same mapping as ``checkcredit``

Credentials: same env / ``.env`` as Duty Bot (``NP_BACKEND_*``, ``NCH_BACKEND_*``, ``TBR_BACKEND_*``, …).

Flow:

1. Login and open the machine table (default ``/egm/egmStatusList``; override with ``SM_MACHINE_PATH``).
2. Ensure pagination is on **first** page (Previous until disabled).
3. In **request order**, find each machine; **only tick** if Status is **normal** or **occupy** and Online/Offline is **online**. Rows in **maintenance** / **offline** / other statuses are **not ticked**; if their checkbox is on, it is cleared. Those machines are listed before the backward pass.
4. If some targets remain unfound, click **Next**, repeat (bounded by ``NP_BACKEND_MAX_PAGES`` / ``SM_MACHINE_MAX_PAGES``).
5. After every **eligible** row is ticked, walk **backward** with **Previous** through every page visited; on each page re-verify checkboxes for ticked machines only (do not assume).
6. Print machine row labels that are still checked; then AFK ``SM_MACHINE_AFK_SEC`` (default **90**) seconds.

Env:

- ``SM_MACHINE_PATH`` — path after host (default ``/egm/egmStatusList``).
- ``SM_MACHINE_AFK_SEC`` — idle seconds at end (default ``90``).
- ``SM_MACHINE_MAX_PAGES`` — max Next steps for **CLI** tick/report (default: ``NP_BACKEND_MAX_PAGES``, often 20).
- ``SM_MACHINE_COLLECT_MAX_PAGES`` — for **read-only** ``smachine_collect_all_machine_rows`` / web dashboard only:
  max Next steps when ``SM_MACHINE_MAX_PAGES`` is **unset** (default **500** so full machine lists are not cut off early).
- ``SM_MACHINE_HEADLESS=1`` — headless Chromium (default: headed unless Linux without DISPLAY).
- ``SM_MACHINE_HEADED=1`` — force headed (used by ``maintenancetest`` mode).
- ``SM_BATCH_REMARK`` — optional remark for ``maintenancetest`` / batch EGM save dialog.
- ``SM_MACHINE_STRICT_BACKWARD=1`` — do not re-tick on backward verify if checkboxes were cleared by paging (Element UI tables often drop selection across pages unless ``reserve-selection`` is enabled).

Programmatic read-only export (for dashboards / ``webapp``):

- ``smachine_collect_all_machine_rows(site, …)`` — one backend, all table pages (read-only); returns ``(rows, truncation_warning)``.
- ``smachine_collect_machines_multi_sites()`` — all default backends (deduped by EGM URL); ``WEBMACHINE_SITES`` overrides.
- ``WEBMACHINE_WARM_POOL=1`` (default) — keep one **headed** browser open per backend for ``webmachine_data.json`` refresh; set ``WEBMACHINE_WARM_POOL=0`` for one-shot launch/close scrapes.
"""

from __future__ import annotations

import json
import logging
import os
import queue as _queue
import re
import sys
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from urllib.parse import quote

logger = logging.getLogger(__name__)

_ROOT_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT_DIR / ".env")
except ImportError:
    pass


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


# CLI batch modes (first argv): run ``prod_machine_batch`` ``set_both`` (BatchMaintenance + BatchTest).
_BATCH_CLI_MODES: dict[str, str] = {
    "maintenancetest": "set_both",
    "maintaintest": "set_both",
    "setboth": "set_both",
}


def _infer_belongs_from_machine_line(line: str) -> str:
    """Guess belongs code from machine token (e.g. ``nch1422`` → ``NCH``)."""
    alnum = re.sub(r"[^A-Za-z0-9]", "", _normalize_machine_target_line(line)).upper()
    if not alnum:
        return ""
    prefixes = (
        ("NCH", "NCH"),
        ("NWR", "NP"),
        ("NP", "NP"),
        ("TBR", "TBR"),
        ("TBP", "TBP"),
        ("MDR", "MDR"),
        ("DHS", "DHS"),
        ("OSM", "CP"),
        ("CP", "CP"),
        ("WF", "WF"),
        ("WINFORD", "WF"),
    )
    for needle, belongs in prefixes:
        if alnum.startswith(needle) or needle in alnum:
            return belongs
    return ""


def _run_batch_cli_mode(action: str, raw_targets: list[str]) -> None:
    """Headed Playwright batch maintenance/test (webapp-equivalent ``set_both``)."""
    from prod_machine_batch import ACTION_LABELS, run_prod_batch_job

    os.environ.setdefault("SM_MACHINE_HEADED", "1")
    os.environ["SMACHINE_HEADLESS"] = "0"

    remark = (os.environ.get("SM_BATCH_REMARK") or "").strip()[:100]
    machines: list[dict] = []
    for line in raw_targets:
        belongs = _infer_belongs_from_machine_line(line)
        if not belongs:
            raise SystemExit(
                f"Cannot infer belongs for {line!r}. "
                "Use a name with NCH/NWR/MDR/… in it, or pass belongs explicitly later."
            )
        machines.append({"belongs": belongs, "machine": line.strip()})

    label = ACTION_LABELS.get(action, action)
    print(f"Batch mode: {label!r} (action={action})")
    print(f"Headed browser (SM_MACHINE_HEADED=1, SMACHINE_HEADLESS=0)")
    print(f"Targets: {machines}")
    if remark:
        print(f"Remark: {remark!r}")

    summary = run_prod_batch_job(action, machines, remark=remark)
    ok = summary.get("success") or []
    fail = summary.get("failed") or []
    print("")
    print(f"Success: {len(ok)}")
    for m in ok:
        print(f"  ✓ {m.get('belongs')} — {m.get('machine')}")
    print(f"Failed: {len(fail)}")
    for m in fail:
        err = (m.get("error") or "").strip()
        suffix = f" — {err}" if err else ""
        print(f"  ✗ {m.get('belongs')} — {m.get('machine')}{suffix}")
    if fail:
        sys.exit(1)


def _run_batch_button_probe_cli(site_filters: list[str] | None) -> None:
    """Probe EGM batch toolbar buttons on each backend (Cancel only, never Save)."""
    from prod_machine_batch import EGM_TOOLBAR_BATCH_BUTTONS, run_egm_batch_button_probe

    os.environ.setdefault("SM_MACHINE_HEADED", "1")
    os.environ["SMACHINE_HEADLESS"] = "0"

    print("Batch toolbar probe — maintenance/test buttons only; Cancel on confirm (never Save)")
    print(f"Buttons: {', '.join(EGM_TOOLBAR_BATCH_BUTTONS)}")
    if site_filters:
        print(f"Sites: {site_filters}")
    else:
        print("Sites: all PROD backends (WEBMACHINE_SITES or default list)")

    report = run_egm_batch_button_probe(site_filters, headless=False)
    failed = 0
    for sk, site in (report.get("sites") or {}).items():
        print("")
        print(f"=== {sk} ({site.get('belongs', '?')}) ===")
        if site.get("error"):
            print(f"  ERROR: {site['error']}")
            failed += 1
            continue
        if site.get("sample_machine"):
            print(f"  sample row: {site['sample_machine']}")
        for label, probes in (site.get("buttons") or {}).items():
            wo = probes.get("without_selection") or {}
            ws = probes.get("with_selection") or {}
            wo_ok = wo.get("ok")
            ws_ok = ws.get("ok")
            mark = "OK" if wo_ok and ws_ok else "FAIL"
            if not (wo_ok and ws_ok):
                failed += 1
            print(f"  [{mark}] {label}")
            print(f"       no selection: {wo.get('detail') or wo.get('detail', '—')}")
            print(f"       with selection: {ws.get('detail') or '—'}")
    skipped = report.get("skipped") or {}
    if skipped:
        print("")
        print("Skipped (duplicate backend):", skipped)
    print("")
    if failed:
        print(f"Probe finished with {failed} issue(s).")
        sys.exit(1)
    print("Probe finished — all toolbar buttons behaved as expected.")


def _site_routing_key(site: str) -> str:
    """
    ``<alias>cs`` (suffix ``cs`` = check status) routes like ``<alias>`` for backend / credentials.
    Example: ``mdrcs`` → ``mdr``, ``nchcs`` → ``nch``.
    """
    s = (site or "").strip().lower()
    if len(s) > 2 and s.endswith("cs"):
        return s[:-2]
    return s


def _site_synthetic_machine(site: str) -> str:
    """Machine label that routes ``checkcredit._np_resolve_backend`` to the desired host."""
    s = _site_routing_key(site)
    aliases: dict[str, str] = {
        "nwr": "NWR0001",
        "np": "NWR0001",
        "nch": "NCH0001",
        "nc": "NCH0001",
        "new": "NCH0001",
        "tbr": "TBR0001",
        "tbp": "TBP0001",
        "mdr": "MDR0001",
        "dhs": "DHS0001",
        "cp": "CP0001",
        "osm": "OSM0001",
        "wf": "WF0001",
        "winford": "WF0001",
    }
    syn = aliases.get(s)
    if not syn:
        raise SystemExit(
            f"Unknown site alias {site!r}. Try: {', '.join(sorted(set(aliases.keys())))}"
        )
    return syn


def _normalize_machine_target_line(line: str) -> str:
    """
    Strip dashboard-style ``(TEST)`` suffix before row matching.

    Scraped names often append ``(TEST)`` when ``span.test`` is present, but EGM row
    ``inner_text`` usually omits it — matching would fail on ``…1422(TEST)`` vs ``…1422``.
    """
    raw = (line or "").strip()
    raw = re.sub(r"\(TEST\)\s*$", "", raw, flags=re.I).strip()
    return raw


def _parse_target_line(line: str) -> tuple[str, str]:
    """
    Returns (kind, key) where kind is ``digits`` or ``full``.

    * ``digits`` — standalone numeric asset id (exact match on machine suffix).
    * ``full`` — alphanumeric token; also matches by trailing asset id when the title is typo'd
      (e.g. ``Echo FTBP8671``, ``TP8674``, ``Echo Fornes-TBP8673``).
    """
    raw = _normalize_machine_target_line(line)
    if not raw:
        raise ValueError("empty machine line")
    alnum = re.sub(r"[^A-Za-z0-9]", "", raw)
    if not alnum:
        raise ValueError(f"no machine token in {line!r}")
    if alnum.isdigit():
        return ("digits", alnum)
    return ("full", alnum.upper())


def _site_belongs_label(site_key: str) -> str:
    """Venue / property code for dashboard ``belongs`` column (PROD site aliases)."""
    labels = {
        "nwr": "NP",
        "np": "NP",
        "nch": "NCH",
        "nc": "NCH",
        "new": "NCH",
        "tbr": "TBR",
        "tbp": "TBP",
        "mdr": "MDR",
        "dhs": "DHS",
        "cp": "CP",
        "osm": "CP",
        "wf": "WF",
        "winford": "WF",
    }
    return labels.get((site_key or "").strip().lower(), (site_key or "").upper())


def _osmslot_admin_credentials() -> tuple[str, str]:
    user = (os.environ.get("WEBMACHINE_OSMSLOT_USER") or os.environ.get("OSMSLOT_ADMIN_USER") or "admin").strip()
    pw = (os.environ.get("WEBMACHINE_OSMSLOT_PASSWORD") or os.environ.get("OSMSLOT_ADMIN_PASSWORD") or "123456").strip()
    return user, pw


def _nonprod_backend_specs(deployment: str) -> list[dict[str, str | bool]]:
    """QAT / UAT EGM backends on ``*.osmslot.org`` (see webapp deployment tabs)."""
    dep = (deployment or "").strip().upper()
    if dep not in ("QAT", "UAT"):
        return []
    prefix = "qat" if dep == "QAT" else "uat"
    user, pw = _osmslot_admin_credentials()
    hosts: tuple[tuple[str, str], ...] = (
        ("CP", f"https://{prefix}-cp.osmslot.org"),
        ("TBP", f"https://{prefix}-tbp.osmslot.org"),
        ("TBR", f"https://{prefix}-tbr.osmslot.org"),
        ("DHS", f"https://{prefix}-dhs.osmslot.org"),
        ("NCH", f"https://{prefix}-nc.osmslot.org"),
        ("WF", f"https://{prefix}-wf.osmslot.org"),
        ("MDR", f"https://{prefix}-mdr.osmslot.org"),
        ("NP", f"https://{prefix}-np.osmslot.org"),
    )
    out: list[dict[str, str | bool]] = []
    for belongs, base in hosts:
        out.append(
            {
                "belongs": belongs,
                "base": base,
                "user": user,
                "password": pw,
                "deployment": dep,
                "dismiss_warning_dialog": dep == "QAT",
                "list_path": "/egm/egmStatusList",
                "login_path": "/login",
            }
        )
    return out


def _dismiss_warning_dialog(page, timeout_ms: int) -> None:
    """Close Element UI ``Warnning`` modal (QAT) via header X before reading the EGM table."""
    try:
        dialog = page.locator('.el-dialog[aria-label="Warnning"], .el-dialog:has(.el-dialog__title:has-text("Warnning"))').first
        if dialog.count() == 0:
            return
        close = dialog.locator(".el-dialog__headerbtn[aria-label='Close'], .el-dialog__headerbtn").first
        if close.count() and close.is_visible(timeout=min(5000, timeout_ms)):
            close.click()
            page.wait_for_timeout(450)
    except Exception:
        pass


def _resolve_collect_page_limit(max_pages: int | None) -> int:
    from checkcredit import NP_BACKEND_MAX_PAGES  # noqa: WPS433

    if max_pages is None:
        explicit = (os.environ.get("SM_MACHINE_MAX_PAGES") or "").strip()
        if explicit:
            try:
                return max(1, int(explicit))
            except ValueError:
                return max(1, NP_BACKEND_MAX_PAGES)
        try:
            collect_cap = int((os.environ.get("SM_MACHINE_COLLECT_MAX_PAGES") or "500").strip() or "500")
        except ValueError:
            collect_cap = 500
        return max(1, collect_cap)
    return max(1, int(max_pages))


def _smachine_egm_urls(
    base_url: str,
    list_path: str = "/egm/egmStatusList",
    login_path: str = "/login",
) -> tuple[str, str, str]:
    base = (base_url or "").strip().rstrip("/")
    path = (list_path or "/egm/egmStatusList").strip() or "/egm/egmStatusList"
    if not path.startswith("/"):
        path = "/" + path
    login = (login_path or "/login").strip() or "/login"
    if not login.startswith("/"):
        login = "/" + login
    login_url = f"{base}{login}?redirect={quote(path, safe='')}"
    list_url = f"{base}{path}"
    return base, login_url, list_url


def _smachine_login_and_open_egm_list(
    page,
    *,
    base_url: str,
    username: str,
    password: str,
    list_path: str = "/egm/egmStatusList",
    login_path: str = "/login",
    dismiss_warning_dialog: bool = False,
    timeout_ms: int = 120_000,
    stall_check: Callable[[], bool] | None = None,
) -> str:
    """Log in and navigate to the EGM status table; returns ``list_url``."""
    _base, login_url, list_url = _smachine_egm_urls(base_url, list_path, login_path)
    path = list_url.split(_base, 1)[-1] if _base else "/egm/egmStatusList"

    def _maybe_stall(where: str) -> None:
        if stall_check and stall_check():
            raise RuntimeError(f"EGM scrape stalled ({where}; no progress detected)")

    page.goto(login_url, wait_until="domcontentloaded")
    page.wait_for_timeout(900)
    _maybe_stall("login page")

    pwd_box = page.locator('input[type="password"]').first
    pwd_box.wait_for(state="visible", timeout=min(30_000, timeout_ms))
    _maybe_stall("login form")
    form = pwd_box.locator("xpath=ancestor::form[1]")
    user = (username or "").strip()
    pw = (password or "").strip()
    if form.count():
        tin = form.locator(
            'input[type="text"], input:not([type]), input[type="tel"], input[type="email"]'
        ).first
        tin.fill(user)
    else:
        page.locator('input[type="text"]').first.fill(user)
    pwd_box.fill(pw)
    lb = page.get_by_role("button", name=re.compile(r"login|sign in|log in", re.I))
    if lb.count():
        lb.first.click()
    else:
        page.locator('button[type="submit"], button.el-button--primary').first.click()

    page.wait_for_timeout(1800)
    _maybe_stall("after login")
    if dismiss_warning_dialog:
        _dismiss_warning_dialog(page, timeout_ms)
    if path not in (page.url or ""):
        page.goto(list_url, wait_until="domcontentloaded")
    if dismiss_warning_dialog:
        _dismiss_warning_dialog(page, timeout_ms)

    page.wait_for_selector(".app-container, .filter-container, .el-table", timeout=timeout_ms)
    _wait_table_idle(page, timeout_ms)
    _maybe_stall("machine table")
    return list_url


def _smachine_collect_rows_on_egm_page(
    page,
    *,
    belongs: str,
    deployment: str,
    max_pages: int | None = None,
    timeout_ms: int = 120_000,
    stall_check: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict], str | None]:
    """Walk the EGM table on an already-logged-in page (read-only)."""
    limit = _resolve_collect_page_limit(max_pages)
    dep_label = (deployment or "PROD").strip().upper() or "PROD"
    belong_label = (belongs or "—").strip() or "—"
    collected: list[dict] = []
    trunc_msg: str | None = None

    def _tick(pages: int, rows: int) -> None:
        if on_progress:
            on_progress(pages, rows)

    def _maybe_stall(where: str) -> None:
        if stall_check and stall_check():
            raise RuntimeError(f"EGM scrape stalled ({where}; no progress detected)")

    _tick(0, 0)
    _go_first_page(page, timeout_ms=timeout_ms, max_steps=limit)
    _wait_table_idle(page, timeout_ms)
    expected_total = _pagination_total_entries(page)

    next_clicks = 0
    while True:
        _maybe_stall("pagination")
        for mn, test, game_type, st, onl in _collect_visible_table_machine_rows(page, timeout_ms=timeout_ms):
            collected.append(
                {
                    "environment": dep_label,
                    "belongs": belong_label,
                    "name": mn,
                    "game_type": game_type,
                    "status": st,
                    "online": onl,
                    "is_test": test,
                }
            )

        _tick(next_clicks + 1, len(collected))

        if not _can_pagination_next(page):
            break
        if next_clicks >= limit:
            try:
                if _can_pagination_next(page):
                    trunc_msg = (
                        f"pagination stopped after {limit} page(s); more data exists — "
                        "raise SM_MACHINE_COLLECT_MAX_PAGES or set SM_MACHINE_MAX_PAGES"
                    )
            except Exception:
                trunc_msg = f"pagination stopped after {limit} page(s) (could not verify Next)"
            break
        _click_pagination_next(page, timeout_ms=timeout_ms)
        next_clicks += 1
        _wait_table_idle(page, timeout_ms)
    if expected_total is not None and len(collected) < expected_total:
        note = (
            f"table reports {expected_total} entries but collected {len(collected)} "
            f"for {belong_label} @ {dep_label}"
        )
        trunc_msg = f"{trunc_msg}; {note}" if trunc_msg else note
    return collected, trunc_msg


def smachine_collect_rows_at_backend(
    *,
    base_url: str,
    username: str,
    password: str,
    belongs: str,
    deployment: str,
    list_path: str = "/egm/egmStatusList",
    login_path: str = "/login",
    dismiss_warning_dialog: bool = False,
    headless: bool | None = None,
    max_pages: int | None = None,
    timeout_ms: int = 120_000,
    stall_check: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict], str | None]:
    """
    Log in to one explicit EGM origin, optionally dismiss the QAT warning dialog, walk
    ``/egm/egmStatusList`` (read-only), and return normalized rows for webapp.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("Install playwright: pip install playwright && playwright install chromium") from e

    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("empty base_url")
    user = (username or "").strip()
    pw = (password or "").strip()
    if not user or not pw:
        raise RuntimeError(f"missing credentials for {belongs!r} @ {deployment}")

    hl = _smachine_resolve_headless(headless)

    def _maybe_stall(where: str) -> None:
        if stall_check and stall_check():
            raise RuntimeError(f"EGM scrape stalled ({where}; no progress detected)")

    if on_progress:
        on_progress(0, 0)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=hl)
        try:
            context = browser.new_context(
                viewport={"width": 1600, "height": 900},
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.set_default_timeout(timeout_ms)

            _smachine_login_and_open_egm_list(
                page,
                base_url=base,
                username=user,
                password=pw,
                list_path=list_path,
                login_path=login_path,
                dismiss_warning_dialog=dismiss_warning_dialog,
                timeout_ms=timeout_ms,
                stall_check=stall_check,
            )
            return _smachine_collect_rows_on_egm_page(
                page,
                belongs=belongs,
                deployment=deployment,
                max_pages=max_pages,
                timeout_ms=timeout_ms,
                stall_check=stall_check,
                on_progress=on_progress,
            )
        finally:
            browser.close()


def _machine_name_alnum_upper(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _machine_asset_digits_from_name(name: str) -> str | None:
    """Asset id digits from a machine label or full table row text (e.g. ``…-TBP8671`` → ``8671``)."""
    alnum = _machine_name_alnum_upper(name)
    if not alnum:
        return None
    env_pat = re.compile(
        r"(?:NWR|NCH|NC|NP|TBR|TBP|MDR|DHS|CP|OSM|WF|WINFORD)(\d+)",
        re.I,
    )
    matches = list(env_pat.finditer(alnum))
    if matches:
        return matches[-1].group(1)
    digit_runs = list(re.finditer(r"\d+", alnum))
    if digit_runs:
        return digit_runs[-1].group(0)
    return None


def _query_asset_digits_from_key(key_alnum: str) -> str | None:
    """Trailing asset id digits parsed from a user token (``TP8674``, ``8673``, full title, …)."""
    key_alnum = (key_alnum or "").upper()
    m = re.search(
        r"(?:NWR|NCH|NC|NP|TBR|TBP|MDR|DHS|CP|OSM|WF|WINFORD)(\d+)$",
        key_alnum,
    )
    if m:
        return m.group(1)
    m2 = re.search(r"(\d+)$", key_alnum)
    return m2.group(1) if m2 else None


def _row_text_matches(kind: str, key: str, row_text: str) -> bool:
    """
    Match a scraped EGM row to a user token.

    * **digits** — exact asset id on the machine (``8671`` matches ``…-TBP8671``, not ``…-TBP8617``).
    * **full** — alnum substring, suffix, or same trailing asset id (typos like ``TP8674`` / ``Echo Fornes-…8673``).
    """
    row_alnum = _machine_name_alnum_upper(row_text)
    if not row_alnum:
        return False

    if kind == "digits":
        row_asset = _machine_asset_digits_from_name(row_text)
        if row_asset is not None:
            return row_asset == key
        return bool(re.search(rf"(?<![0-9]){re.escape(key)}(?![0-9])", row_alnum))

    key_alnum = key.upper()
    if key_alnum in row_alnum:
        return True
    if len(key_alnum) >= 4 and row_alnum.endswith(key_alnum):
        return True

    q_digits = _query_asset_digits_from_key(key_alnum)
    r_digits = _machine_asset_digits_from_name(row_text)
    if q_digits and r_digits and q_digits == r_digits:
        return True

    return False


def _wait_table_idle(page, timeout_ms: int) -> None:
    try:
        page.wait_for_function(
            "() => !Array.from(document.querySelectorAll('.el-loading-mask')).some(x => x && x.offsetParent !== null)",
            timeout=min(timeout_ms, 30_000),
        )
    except Exception:
        pass
    page.wait_for_timeout(350)


def _pagination_root(page):
    """Prefer the list page footer inside ``.app-container`` (dialogs often teleport outside)."""
    scoped = page.locator(".app-container .el-pagination")
    if scoped.count():
        return scoped.first
    return page.locator(".el-pagination").first


def _pagination_prev_btn(page):
    return _pagination_root(page).locator("button.btn-prev").first


def _pagination_next_btn(page):
    return _pagination_root(page).locator("button.btn-next").first


def _can_pagination_prev(page) -> bool:
    btn = _pagination_prev_btn(page)
    if btn.count() == 0:
        return False
    try:
        return not btn.is_disabled()
    except Exception:
        return False


def _can_pagination_next(page) -> bool:
    btn = _pagination_next_btn(page)
    if btn.count() == 0:
        return False
    try:
        return not btn.is_disabled()
    except Exception:
        return False


def _click_pagination_prev(page, *, timeout_ms: int) -> None:
    btn = _pagination_prev_btn(page)
    btn.wait_for(state="visible", timeout=min(15_000, timeout_ms))
    btn.click(timeout=min(30_000, timeout_ms))
    page.wait_for_timeout(900)


def _click_pagination_next(page, *, timeout_ms: int) -> None:
    btn = _pagination_next_btn(page)
    btn.wait_for(state="visible", timeout=min(15_000, timeout_ms))
    btn.click(timeout=min(30_000, timeout_ms))
    page.wait_for_timeout(900)


def _go_first_page(page, *, timeout_ms: int, max_steps: int) -> None:
    for _ in range(max_steps + 5):
        if not _can_pagination_prev(page):
            return
        _click_pagination_prev(page, timeout_ms=timeout_ms)


def _row_checkbox_input(row):
    # Prefer the selection column only (avoids accidental inputs elsewhere in wide rows).
    sel_cell = row.locator("td.el-table-column--selection").first
    if sel_cell.count():
        for sel in (
            'input.el-checkbox__original[type="checkbox"]',
            ".el-checkbox__input input.el-checkbox__original",
            'input[type="checkbox"]',
        ):
            cand = sel_cell.locator(sel).first
            if cand.count():
                return cand
    # Element UI row selection: label.el-checkbox > span.el-checkbox__input > input.el-checkbox__original
    for sel in (
        'input.el-checkbox__original[type="checkbox"]',
        ".el-checkbox__input input.el-checkbox__original",
        '.el-checkbox input[type="checkbox"]',
    ):
        cand = row.locator(sel).first
        if cand.count():
            return cand
    return row.locator('td.el-table-column--selection input[type="checkbox"]').first


def _read_dom_checked(inp, *, timeout_ms: int) -> bool:
    """Read ``HTMLInputElement.checked`` with a short bound (avoids 120s default action timeout)."""
    try:
        return bool(inp.evaluate("el => el.checked", timeout=max(500, min(10_000, timeout_ms))))
    except Exception:
        return False


def _wait_until_checked(page, inp, *, total_ms: int, poll_ms: int = 200) -> bool:
    """Poll until checked or ``total_ms`` elapsed (Vue / Element UI may update after paint)."""
    total_ms = max(500, total_ms)
    poll_ms = max(80, poll_ms)
    deadline = time.monotonic() + total_ms / 1000.0
    while time.monotonic() < deadline:
        if _read_dom_checked(inp, timeout_ms=min(3_000, total_ms)):
            return True
        page.wait_for_timeout(poll_ms)
    return _read_dom_checked(inp, timeout_ms=min(3_000, total_ms))


def _wait_until_unchecked(page, inp, *, total_ms: int, poll_ms: int = 200) -> bool:
    total_ms = max(500, total_ms)
    poll_ms = max(80, poll_ms)
    deadline = time.monotonic() + total_ms / 1000.0
    while time.monotonic() < deadline:
        if not _read_dom_checked(inp, timeout_ms=min(3_000, total_ms)):
            return True
        page.wait_for_timeout(poll_ms)
    return not _read_dom_checked(inp, timeout_ms=min(3_000, total_ms))


def _norm_cell_upper(s: str) -> str:
    return " ".join((s or "").upper().split())


def _row_tick_eligibility(row, *, timeout_ms: int) -> tuple[bool, str]:
    """
    Tick only when Status (cell index **6**) is **normal** or **occupy** and Online/Offline (index **7**) is **online**.
    Reject maintenance, offline, or any other status.
    """
    cells = row.locator("td.el-table__cell")
    try:
        n = cells.count()
    except Exception:
        n = 0
    if n < 8:
        return False, "fewer than 8 columns — cannot read Status / Online-Offline"

    status_raw = _cell_text_one_line(cells.nth(6), timeout_ms=timeout_ms)
    online_raw = _cell_text_one_line(cells.nth(7), timeout_ms=timeout_ms)
    su = _norm_cell_upper(status_raw)
    ou = _norm_cell_upper(online_raw)

    reasons: list[str] = []
    if "MAINTAIN" in su:
        reasons.append("status is maintenance")
    elif "NORMAL" not in su and "OCCUPY" not in su:
        reasons.append(f"status is not normal or occupy ({status_raw or 'empty'})")

    if "OFFLINE" in ou:
        reasons.append("Online/Offline column shows offline")
    elif "ONLINE" not in ou:
        reasons.append(f"Online/Offline column is not online ({online_raw or 'empty'})")

    if reasons:
        return False, "; ".join(reasons)
    return True, ""


def _row_display_name(row) -> str:
    try:
        return " ".join(((row.inner_text() or "").strip()).split())
    except Exception:
        return ""


def _cell_raw_text(cell, *, timeout_ms: int) -> str:
    """
    Prefer ``text_content()`` for full subtree text; use with ``span.test`` detection because ``(TEST)``
    may be CSS-only (not in text nodes).
    """
    t = min(8_000, timeout_ms)
    try:
        tc = cell.text_content(timeout=t)
        if tc is not None and tc.strip():
            return tc
    except Exception:
        pass
    try:
        return cell.inner_text(timeout=t) or ""
    except Exception:
        return ""


def _cell_text_one_line(cell, *, timeout_ms: int) -> str:
    raw = _cell_raw_text(cell, timeout_ms=timeout_ms)
    return " ".join((raw or "").strip().split())


def _machine_name_cell_test_mode_and_display(cell, *, timeout_ms: int) -> tuple[bool, str]:
    """
    Detect EGM test row: Vue uses ``<div>…name…</div><span class="test"></span>``; ``(TEST)`` is often
    **not** in the DOM (only ``::after`` / CSS), so ``textContent`` misses it. Fallback: literal ``(TEST)`` in text.
    """
    name_line = _cell_text_one_line(cell, timeout_ms=timeout_ms)
    literal = bool(re.search(r"\(TEST\)", name_line or "", re.I))
    span_test = False
    try:
        span_test = cell.locator("span.test").first.count() > 0
    except Exception:
        span_test = False
    is_test = literal or span_test
    if is_test and span_test and not literal:
        display = f"{name_line}(TEST)" if name_line else "(TEST)"
    else:
        display = name_line
    return is_test, display


def _row_summary_label(row, *, timeout_ms: int) -> str:
    """
    Short row label for EGM status table (Element UI): machine name, game type, status column.
    Typical columns: 0 selection, 1 Machine Name, 2 Game Type, …, 6 Status (``occupy`` / …).
    """
    cells = row.locator("td.el-table__cell")
    try:
        n = cells.count()
    except Exception:
        n = 0
    parts: list[str] = []
    if n >= 2:
        _tm, t = _machine_name_cell_test_mode_and_display(cells.nth(1), timeout_ms=timeout_ms)
        if t:
            parts.append(t)
    if n >= 3:
        t = _cell_text_one_line(cells.nth(2), timeout_ms=timeout_ms)
        if t:
            parts.append(t)
    if n >= 7:
        t = _cell_text_one_line(cells.nth(6), timeout_ms=timeout_ms)
        if t:
            parts.append(t)
    if parts:
        return " ".join(parts)
    return _row_display_name(row)


def _row_report_fields(row, *, timeout_ms: int) -> tuple[str, bool, str, str, str]:
    """
    Machine name (col 1), test mode, Game Type (col 2), Status (col 7),
    Online/Offline (col 8). Returns ``(machine_name, is_test_mode, game_type, status_text, online_or_offline)``.
    """
    cells = row.locator("td.el-table__cell")
    try:
        n = cells.count()
    except Exception:
        n = 0
    if n >= 2:
        is_test, name = _machine_name_cell_test_mode_and_display(cells.nth(1), timeout_ms=timeout_ms)
    else:
        is_test, name = False, ""
    game_type = _cell_text_one_line(cells.nth(2), timeout_ms=timeout_ms) if n >= 3 else ""
    status = _cell_text_one_line(cells.nth(6), timeout_ms=timeout_ms) if n >= 7 else ""
    online_raw = _cell_text_one_line(cells.nth(7), timeout_ms=timeout_ms) if n >= 8 else ""
    ol = " ".join((online_raw or "").lower().split())
    if "offline" in ol:
        online_disp = "offline"
    elif "online" in ol:
        online_disp = "online"
    else:
        online_disp = online_raw or "(unknown)"
    return name, is_test, game_type, status, online_disp


def _row_match_text_for_target(row, *, timeout_ms: int) -> str:
    """Prefer machine-name column for token matching; fall back to full row ``inner_text``."""
    try:
        machine_name, _, _, _, _ = _row_report_fields(row, timeout_ms=timeout_ms)
        if (machine_name or "").strip():
            return machine_name.strip()
    except Exception:
        pass
    try:
        return row.inner_text(timeout=min(8_000, timeout_ms))
    except Exception:
        return ""


def _norm_online_word(onl: str) -> str:
    ol = " ".join((onl or "").lower().split())
    if "offline" in ol:
        return "offline"
    if "online" in ol:
        return "online"
    return ol or "unknown"


def _norm_status_word(st: str) -> str:
    return " ".join((st or "").lower().split()) or "unknown"


def _print_check_status_groups(
    report: dict[tuple[str, str], tuple[str, bool, str, str]],
    targets: list[tuple[str, str, str]],
) -> None:
    """
    One section per (online/offline, Status column, test | no test) combo that has at least one machine.
    Heading: ``Machine in online, maintain, test mode`` then indented machine display names.
    """
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    not_found_lines: list[str] = []

    for _line, kind, key in targets:
        row = report.get((kind, key))
        if row is None:
            not_found_lines.append(_line)
            continue
        mn, is_test, st, onl = row
        conn = _norm_online_word(onl)
        stat = _norm_status_word(st)
        test_phrase = "test" if is_test else "no test"
        groups[(conn, stat, test_phrase)].append(mn)

    n_req = len(targets)
    n_ok = n_req - len(not_found_lines)
    print(f"Checked {n_req} request(s); found {n_ok}, not found {len(not_found_lines)}.")
    print("")

    def _group_sort_key(k: tuple[str, str, str]) -> tuple:
        conn, stat, test_phrase = k
        conn_i = {"online": 0, "offline": 1}.get(conn, 2)
        test_i = 0 if test_phrase == "test" else 1
        return (conn_i, stat, test_i)

    for key in sorted(groups.keys(), key=_group_sort_key):
        names = groups[key]
        if not names:
            continue
        conn, stat, test_phrase = key
        print(f"Machine in {conn}, {stat}, {test_phrase} mode")
        for name in names:
            print(f"  {name}")
        print("")

    if not_found_lines:
        print("Not found in table (within page limit):")
        for line in not_found_lines:
            print(f"  {line!r}")
        print("")


def _scan_targets_report_only(
    page,
    targets: list[tuple[str, str, str]],
    *,
    timeout_ms: int,
    max_pages: int,
) -> dict[tuple[str, str], tuple[str, bool, str, str]]:
    """Paginate forward until every target row is found; no checkbox interaction."""
    pending = targets.copy()
    next_clicks = 0
    found: dict[tuple[str, str], tuple[str, bool, str, str]] = {}
    safety = 0
    while pending:
        safety += 1
        if safety > max_pages * max(len(targets), 1) + 50:
            raise RuntimeError("Status scan exceeded safety iteration limit.")

        matched_this_page: list[tuple[str, str, str]] = []
        for spec in list(pending):
            _line, kind, key = spec
            row = _find_row_for_target(page, kind, key, timeout_ms)
            if row is None:
                continue
            mn, test, _gt, st, onl = _row_report_fields(row, timeout_ms=timeout_ms)
            found[(kind, key)] = (mn, test, st, onl)
            matched_this_page.append(spec)

        for spec in matched_this_page:
            pending.remove(spec)

        if not pending:
            break

        if not _can_pagination_next(page):
            missing = [s[0] for s in pending]
            raise RuntimeError(f"No Next page; still missing machines: {missing}")

        if next_clicks >= max_pages:
            missing = [s[0] for s in pending]
            raise RuntimeError(f"Hit SM_MACHINE_MAX_PAGES ({max_pages}); missing: {missing}")

        _click_pagination_next(page, timeout_ms=timeout_ms)
        next_clicks += 1
        _wait_table_idle(page, timeout_ms)

    return found


def _find_all_rows_for_target_on_page(page, kind: str, key: str, *, timeout_ms: int):
    rows = _table_body_rows(page)
    try:
        rows.first.wait_for(state="visible", timeout=min(15_000, timeout_ms))
    except Exception:
        pass
    matched = []
    n = rows.count()
    for i in range(n):
        row = rows.nth(i)
        txt = _row_match_text_for_target(row, timeout_ms=timeout_ms)
        if not txt:
            continue
        if _row_text_matches(kind, key, txt):
            matched.append(row)
    return matched


def _prod_batch_scan_retries() -> int:
    """Extra full re-scans (from page 1) for machines missed on the first pass (transient re-render)."""
    try:
        return max(0, int((os.environ.get("PROD_BATCH_SCAN_RETRIES") or "2").strip() or "2"))
    except ValueError:
        return 2


def _scan_targets_collect_rows(
    page,
    targets: list[tuple[str, str, str]],
    *,
    belongs: str,
    deployment: str,
    timeout_ms: int,
    max_pages: int,
) -> tuple[list[dict], list[str]]:
    """
    Paginate until target tokens are resolved; return normalized rows + not-found tokens.

    A single pass can transiently miss exactly one row (Element-UI ``el-table`` re-renders a row
    when its status flips, e.g. to/from ``occupy``, and its text reads empty for a moment). So if
    any targets are still missing after a full pass, we go back to page 1 and re-scan **only** the
    missing ones, up to ``PROD_BATCH_SCAN_RETRIES`` times (default 2).
    """
    pending = targets.copy()
    collected: list[dict] = []
    seen_names: set[str] = set()
    dep_label = (deployment or "PROD").strip().upper() or "PROD"
    belong_label = (belongs or "—").strip() or "—"

    def _one_pass() -> None:
        next_clicks = 0
        safety = 0
        while pending:
            safety += 1
            if safety > max_pages * max(len(targets), 1) + 50:
                break

            resolved: list[tuple[str, str, str]] = []
            for spec in list(pending):
                line, kind, key = spec
                if kind == "invalid":
                    continue
                matched_here = False
                for row in _find_all_rows_for_target_on_page(page, kind, key, timeout_ms=timeout_ms):
                    matched_here = True
                    mn, test, game_type, st, onl = _row_report_fields(row, timeout_ms=timeout_ms)
                    name_key = (mn or "").strip()
                    if not name_key or name_key in seen_names:
                        continue
                    seen_names.add(name_key)
                    collected.append(
                        {
                            "environment": dep_label,
                            "belongs": belong_label,
                            "name": mn,
                            "game_type": game_type,
                            "status": st,
                            "online": onl,
                            "is_test": test,
                        }
                    )
                if matched_here:
                    resolved.append(spec)

            for spec in resolved:
                pending.remove(spec)

            if not pending:
                break
            if not _can_pagination_next(page):
                break
            if next_clicks >= max_pages:
                break
            _click_pagination_next(page, timeout_ms=timeout_ms)
            next_clicks += 1
            _wait_table_idle(page, timeout_ms)

    _one_pass()
    retries = _prod_batch_scan_retries()
    attempt = 0
    while pending and attempt < retries:
        attempt += 1
        # Re-scan from the first page for the few machines missed by a transient re-render.
        non_invalid = [s for s in pending if s[1] != "invalid"]
        if not non_invalid:
            break
        print(
            f"[prod-batch] scan retry {attempt}/{retries} for {len(non_invalid)} missed: "
            f"{[s[0] for s in non_invalid]}",
            flush=True,
        )
        try:
            _go_first_page(page, timeout_ms=timeout_ms, max_steps=max_pages)
            _wait_table_idle(page, timeout_ms)
            page.wait_for_timeout(400)
        except Exception:
            pass
        _one_pass()

    not_found = [line for line, kind, _key in pending if kind != "invalid"]
    not_found.extend(line for line, kind, _key in targets if kind == "invalid")
    return collected, not_found


def _table_body_rows(page):
    """
    Data rows only (not header). Target **main** scroll body only — fixed-column tables also use
    ``tr.el-table__row`` and duplicate rows; loose selectors pick clones whose checkbox does not
    reflect the real selection.
    """
    strict = page.locator(
        "div.el-table__body-wrapper > table.el-table__body > tbody > tr.el-table__row"
    )
    if strict.count():
        return strict
    primary = page.locator(
        ".el-table__body-wrapper:not(.el-table__fixed-body-wrapper) tbody tr.el-table__row"
    )
    if primary.count():
        return primary
    fallback = page.locator(".el-table__body tbody tr.el-table__row")
    if fallback.count():
        return fallback
    return page.locator(".el-table__body tr.el-table__row")


def _find_row_for_target(
    page,
    kind: str,
    key: str,
    timeout_ms: int,
    *,
    prefer_checked: bool = False,
):
    rows = _table_body_rows(page)
    try:
        rows.first.wait_for(state="visible", timeout=min(15_000, timeout_ms))
    except Exception:
        pass
    n = rows.count()
    matched_indices: list[int] = []
    for i in range(n):
        row = rows.nth(i)
        txt = _row_match_text_for_target(row, timeout_ms=timeout_ms)
        if not txt:
            continue
        if _row_text_matches(kind, key, txt):
            if not prefer_checked:
                return row
            matched_indices.append(i)

    if not prefer_checked or not matched_indices:
        return None

    for i in matched_indices:
        row = rows.nth(i)
        if _verify_row_checkbox_checked(page, row, timeout_ms=timeout_ms):
            return row
    return rows.nth(matched_indices[0])


def _ensure_row_checkbox_checked(page, row, *, timeout_ms: int) -> None:
    try:
        row.scroll_into_view_if_needed(timeout=min(15_000, timeout_ms))
    except Exception:
        pass
    inp = _row_checkbox_input(row)
    if inp.count() == 0:
        raise RuntimeError("Row matched machine but no checkbox input (selector mismatch).")
    inp.wait_for(state="attached", timeout=min(15_000, timeout_ms))
    if _read_dom_checked(inp, timeout_ms=3_000):
        return
    lab = row.locator(".el-checkbox").first
    if lab.count():
        lab.click(timeout=min(30_000, timeout_ms))
    else:
        inp.click(timeout=min(30_000, timeout_ms))
    if _wait_until_checked(page, inp, total_ms=12_000):
        return
    inp.click(force=True, timeout=min(30_000, timeout_ms))
    if not _wait_until_checked(page, inp, total_ms=12_000):
        raise RuntimeError("Could not tick checkbox after click (still unchecked after ~12s polls).")


def _ensure_row_checkbox_unchecked(page, row, *, timeout_ms: int) -> None:
    """Clear row selection if checked (maintenance / offline skips)."""
    try:
        row.scroll_into_view_if_needed(timeout=min(15_000, timeout_ms))
    except Exception:
        pass
    inp = _row_checkbox_input(row)
    if inp.count() == 0:
        return
    inp.wait_for(state="attached", timeout=min(15_000, timeout_ms))
    if not _read_dom_checked(inp, timeout_ms=3_000):
        return
    lab = row.locator(".el-checkbox").first
    if lab.count():
        lab.click(timeout=min(30_000, timeout_ms))
    else:
        inp.click(timeout=min(30_000, timeout_ms))
    if _wait_until_unchecked(page, inp, total_ms=12_000):
        return
    inp.click(force=True, timeout=min(30_000, timeout_ms))
    if not _wait_until_unchecked(page, inp, total_ms=12_000):
        raise RuntimeError("Could not clear checkbox after click (still checked after ~12s polls).")


def _verify_row_checkbox_checked(page, row, *, timeout_ms: int) -> bool:
    try:
        row.scroll_into_view_if_needed(timeout=min(8_000, timeout_ms))
    except Exception:
        pass
    inp = _row_checkbox_input(row)
    if inp.count() == 0:
        return False
    try:
        inp.wait_for(state="attached", timeout=min(8_000, timeout_ms))
        return _read_dom_checked(inp, timeout_ms=min(8_000, timeout_ms))
    except Exception:
        return False


def _pagination_total_entries(page) -> int | None:
    """Parse Element UI footer text like ``Showing 1 to 200 of 247 entries``."""
    try:
        txt = _pagination_root(page).inner_text(timeout=5_000) or ""
    except Exception:
        return None
    m = re.search(r"of\s+([\d,]+)\s+entries", txt, re.I)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _collect_visible_table_machine_rows(page, *, timeout_ms: int) -> list[tuple[str, bool, str, str, str]]:
    """All data rows on the current page: ``(machine_name, is_test, game_type, status, online_word)``."""
    rows = _table_body_rows(page)
    try:
        rows.first.wait_for(state="visible", timeout=min(15_000, timeout_ms))
    except Exception:
        pass
    out: list[tuple[str, bool, str, str, str]] = []
    try:
        n = rows.count()
    except Exception:
        n = 0
    for i in range(n):
        row = rows.nth(i)
        try:
            mn, test, game_type, st, onl = _row_report_fields(row, timeout_ms=timeout_ms)
        except Exception:
            continue
        name = (mn or "").strip()
        if not name:
            continue
        out.append((name, test, (game_type or "").strip(), (st or "").strip(), (onl or "").strip()))
    return out


def _smachine_resolve_headless(headless: bool | None) -> bool:
    if headless is not None:
        return bool(headless)
    if _truthy_env("BOT_PLAYWRIGHT_HEADLESS") or _truthy_env("PLAYWRIGHT_HEADLESS"):
        return True
    if _truthy_env("SM_MACHINE_HEADLESS"):
        return True
    if _truthy_env("SM_MACHINE_HEADED"):
        return False
    return sys.platform == "linux" and not (os.environ.get("DISPLAY") or "").strip()


def smachine_collect_all_machine_rows(
    site: str,
    *,
    headless: bool | None = None,
    max_pages: int | None = None,
    timeout_ms: int = 120_000,
    stall_check: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict], str | None]:
    """
    Log in to one backend (same routing as CLI), walk the EGM status table from first page forward,
    and return every visible row (**read-only**, no checkbox changes, no other UI actions).

    Pagination: if ``max_pages`` is ``None`` and ``SM_MACHINE_MAX_PAGES`` is unset, uses
    ``SM_MACHINE_COLLECT_MAX_PAGES`` (default **500**) so large sites are fully walked. Set
    ``SM_MACHINE_MAX_PAGES`` to override with the same knob as the CLI.

    Returns ``(rows, truncation_warning)`` where ``truncation_warning`` is set if the table still
    had a enabled **Next** when the page cap was hit (list may be incomplete).
    """
    from checkcredit import _np_resolve_backend  # noqa: WPS433

    site_key = _site_routing_key(site or "")
    if not site_key:
        raise ValueError("empty site")
    try:
        synth = _site_synthetic_machine(site)
    except SystemExit as e:
        raise ValueError(str(e)) from e
    base, user, pw = _np_resolve_backend(synth)
    if not user or not pw:
        raise RuntimeError(f"missing backend credentials for {site_key!r}")

    path = (os.environ.get("SM_MACHINE_PATH") or "/egm/egmStatusList").strip() or "/egm/egmStatusList"
    return smachine_collect_rows_at_backend(
        base_url=base,
        username=user,
        password=pw,
        belongs=_site_belongs_label(site_key),
        deployment="PROD",
        list_path=path,
        login_path="/login",
        dismiss_warning_dialog=False,
        headless=headless,
        max_pages=max_pages,
        timeout_ms=timeout_ms,
        stall_check=stall_check,
        on_progress=on_progress,
    )


def _dedupe_site_keys_by_resolved_backend(site_keys: list[str]) -> tuple[list[str], dict[str, str]]:
    """
    Each distinct EGM origin (``base_url`` + login user) is scraped once; later aliases that map to
    the same login (e.g. ``osm`` after ``cp`` on ``backend.osmplay.com``) are skipped with a note.
    """
    from checkcredit import _np_resolve_backend  # noqa: WPS433

    seen: dict[tuple[str, str], str] = {}
    order: list[str] = []
    skipped: dict[str, str] = {}
    for sk in site_keys:
        try:
            synth = _site_synthetic_machine(sk)
        except SystemExit:
            order.append(sk)
            continue
        try:
            base, u, pw = _np_resolve_backend(synth)
        except Exception:
            order.append(sk)
            continue
        if not pw:
            order.append(sk)
            continue
        key = (base.rstrip("/"), (u or "").strip())
        if key in seen:
            skipped[sk] = f"skipped — same EGM as {seen[key]!r}"
            continue
        seen[key] = sk
        order.append(sk)
    return order, skipped


DEFAULT_WEBMACHINE_SITES: tuple[str, ...] = ("nwr", "nch", "tbr", "tbp", "mdr", "dhs", "cp", "osm", "wf")


def _scrape_concurrency(item_count: int) -> int:
    """
    Max concurrent EGM page scrapes (each runs its own headless Chromium).

    Controlled by ``WEBMACHINE_SCRAPE_CONCURRENCY`` (default **8**):
    * ``0`` (or negative) → **unlimited** = open *all* pages at the same time.
    * ``1`` → old sequential behaviour.
    Capped to the number of items so we never start idle workers.
    """
    try:
        n = int((os.environ.get("WEBMACHINE_SCRAPE_CONCURRENCY") or "8").strip() or "8")
    except ValueError:
        n = 8
    if n <= 0:  # unlimited → one worker per page (all at once)
        return max(1, item_count)
    return max(1, min(n, max(1, item_count)))


# A scrape "unit": (label, callable) where the callable returns ``(rows, warning)``.
ScrapeUnit = tuple[str, Callable[[], tuple[list[dict], Optional[str]]]]


def _collect_units(units: list[ScrapeUnit]) -> tuple[list[dict], dict[str, str]]:
    """
    Run every scrape unit in parallel (up to :func:`_scrape_concurrency`), so all EGM pages refresh
    at once instead of one-by-one. ``units`` may span sites *and* deployments — the whole set shares
    one thread pool, which is what lets PROD/QAT/UAT load simultaneously.
    """
    errs: dict[str, str] = {}
    all_rows: list[dict] = []
    workers = _scrape_concurrency(len(units))

    if workers <= 1 or len(units) <= 1:
        for label, fn in units:
            try:
                part, twarn = fn()
                all_rows.extend(part)
                if twarn:
                    errs[label] = twarn
            except Exception as e:  # noqa: BLE001
                errs[label] = str(e)
        return all_rows, errs

    results: dict[str, tuple[tuple[list[dict], str | None] | None, Exception | None]] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sm-scrape") as ex:
        future_map = {ex.submit(fn): label for label, fn in units}
        for fut in as_completed(future_map):
            label = future_map[fut]
            try:
                results[label] = (fut.result(), None)
            except Exception as e:  # noqa: BLE001
                results[label] = (None, e)
    for label, _fn in units:
        res, err = results.get(label, (None, None))
        if err is not None:
            errs[label] = str(err)
            continue
        if res is None:
            continue
        part, twarn = res
        all_rows.extend(part)
        if twarn:
            errs[label] = twarn
    return all_rows, errs


def _collect_concurrently(
    keys: list[str],
    worker: Callable[[str], tuple[list[dict], str | None]],
) -> tuple[list[dict], dict[str, str]]:
    """Backwards-compatible wrapper: run ``worker(key)`` for every key as scrape units."""
    units: list[ScrapeUnit] = [(k, (lambda k=k: worker(k))) for k in keys]
    return _collect_units(units)


def _prod_scrape_units(sites: Sequence[str] | None = None, **kwargs: Any) -> tuple[list[ScrapeUnit], dict[str, str]]:
    """PROD scrape units (one per deduped backend site) + skipped-alias notes."""
    raw_env = (os.environ.get("WEBMACHINE_SITES") or "").strip()
    if sites is not None:
        use = [s.strip().lower() for s in sites if (s or "").strip()]
    elif raw_env:
        use = [s.strip().lower() for s in raw_env.split(",") if s.strip()]
    else:
        use = list(DEFAULT_WEBMACHINE_SITES)
    use, skipped = _dedupe_site_keys_by_resolved_backend(use)
    units: list[ScrapeUnit] = [
        (sk, (lambda sk=sk: smachine_collect_all_machine_rows(sk, **kwargs))) for sk in use
    ]
    return units, dict(skipped)


def _nonprod_scrape_units(deployment: str, **kwargs: Any) -> tuple[list[ScrapeUnit], dict[str, str]]:
    """QAT/UAT scrape units (one per ``*.osmslot.org`` backend)."""
    dep = (deployment or "").strip().upper()
    specs = _nonprod_backend_specs(dep)
    if not specs:
        return [], {dep: f"unsupported deployment {deployment!r}"}
    units: list[ScrapeUnit] = []
    for spec in specs:
        key = f"{dep}:{spec['belongs']}"
        units.append(
            (
                key,
                (
                    lambda spec=spec: smachine_collect_rows_at_backend(
                        base_url=str(spec["base"]),
                        username=str(spec["user"]),
                        password=str(spec["password"]),
                        belongs=str(spec["belongs"]),
                        deployment=dep,
                        list_path=str(spec["list_path"]),
                        login_path=str(spec["login_path"]),
                        dismiss_warning_dialog=bool(spec["dismiss_warning_dialog"]),
                        **kwargs,
                    )
                ),
            )
        )
    return units, {}


def smachine_collect_machines_multi_sites(
    sites: Sequence[str] | None = None,
    **kwargs: Any,
) -> tuple[list[dict], dict[str, str]]:
    """
    Scrape several site aliases **concurrently** (thread pool, see ``WEBMACHINE_SCRAPE_CONCURRENCY``).
    ``kwargs`` are passed to ``smachine_collect_all_machine_rows`` (e.g. ``headless=``,
    ``max_pages=``, ``timeout_ms=``).

    Returns ``(rows, errors_by_site_key)`` where ``errors_by_site_key`` holds per-site failure or
    truncation messages (and skipped-alias notes from :func:`_dedupe_site_keys_by_resolved_backend`).

    Default site list: ``DEFAULT_WEBMACHINE_SITES`` (every routed backend from ``checkcredit``) or
    env ``WEBMACHINE_SITES`` (comma-separated).
    """
    units, skipped = _prod_scrape_units(sites, **kwargs)
    rows, errs = _collect_units(units)
    # Keep skipped-alias notes alongside scrape errors.
    merged = dict(skipped)
    merged.update(errs)
    return rows, merged


def smachine_collect_nonprod_deployment(
    deployment: str,
    **kwargs: Any,
) -> tuple[list[dict], dict[str, str]]:
    """Scrape every QAT or UAT ``*.osmslot.org`` backend in :func:`_nonprod_backend_specs`."""
    units, errs = _nonprod_scrape_units(deployment, **kwargs)
    if not units:
        return [], errs
    rows, scrape_errs = _collect_units(units)
    errs.update(scrape_errs)
    return rows, errs


# ---------------------------------------------------------------------------
# Webmachine warm browser pool (keep EGM browsers open for webmachine_data.json)
# ---------------------------------------------------------------------------
# One persistent, headed Chromium per backend (PROD site + QAT/UAT host). Browsers stay open
# between scrapes; the background webapp thread re-walks tables and writes webmachine_data.json.
# Disable with ``WEBMACHINE_WARM_POOL=0``. Headed by default when warm pool is on
# (``WEBMACHINE_WARM_HEADLESS=1`` to hide windows).

BackendScrapeSpec = dict[str, Any]
_WEBMACHINE_WARM_KEEPALIVE_SEC = 240.0


def _webmachine_warm_pool_enabled() -> bool:
    return (os.environ.get("WEBMACHINE_WARM_POOL", "1") or "").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _webmachine_warm_prewarm_on_startup() -> bool:
    return (os.environ.get("WEBMACHINE_WARM_PREWARM_ON_STARTUP", "1") or "").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _webmachine_warm_headless() -> bool:
    if _truthy_env("WEBMACHINE_WARM_HEADLESS"):
        return True
    if _truthy_env("WEBMACHINE_WARM_HEADED") or _truthy_env("SM_MACHINE_HEADED"):
        return False
    if _webmachine_warm_pool_enabled():
        return False
    return _smachine_resolve_headless(None)


def _wm_warm_profile_dir(label: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", (label or "backend").strip())[:48]
    return Path(tempfile.gettempdir()) / f"wm_warm_profile_{safe}"


def _prod_backend_spec_for_site(site: str) -> BackendScrapeSpec:
    from checkcredit import _np_resolve_backend  # noqa: WPS433

    site_key = _site_routing_key(site or "")
    synth = _site_synthetic_machine(site)
    base, user, pw = _np_resolve_backend(synth)
    path = (os.environ.get("SM_MACHINE_PATH") or "/egm/egmStatusList").strip() or "/egm/egmStatusList"
    return {
        "base_url": base,
        "username": user,
        "password": pw,
        "belongs": _site_belongs_label(site_key),
        "deployment": "PROD",
        "list_path": path,
        "login_path": "/login",
        "dismiss_warning_dialog": False,
    }


def _nonprod_backend_spec(spec: dict[str, Any]) -> BackendScrapeSpec:
    return {
        "base_url": str(spec["base"]),
        "username": str(spec["user"]),
        "password": str(spec["password"]),
        "belongs": str(spec["belongs"]),
        "deployment": str(spec["deployment"]),
        "list_path": str(spec["list_path"]),
        "login_path": str(spec["login_path"]),
        "dismiss_warning_dialog": bool(spec["dismiss_warning_dialog"]),
    }


def _all_deployment_backend_specs(**kwargs: Any) -> tuple[list[tuple[str, BackendScrapeSpec]], dict[str, str]]:
    """Backend login specs for every configured deployment (same scope as full scrape)."""
    raw = (os.environ.get("WEBMACHINE_DEPLOYMENTS") or "prod,qat,uat").strip()
    deployments = [d.strip().upper() for d in raw.split(",") if d.strip()]
    if not deployments:
        deployments = ["PROD"]

    specs: list[tuple[str, BackendScrapeSpec]] = []
    errs: dict[str, str] = {}
    for dep in deployments:
        if dep == "PROD":
            raw_env = (os.environ.get("WEBMACHINE_SITES") or "").strip()
            if raw_env:
                use = [s.strip().lower() for s in raw_env.split(",") if s.strip()]
            else:
                use = list(DEFAULT_WEBMACHINE_SITES)
            use, skipped = _dedupe_site_keys_by_resolved_backend(use)
            errs.update(skipped)
            for sk in use:
                try:
                    specs.append((sk, _prod_backend_spec_for_site(sk)))
                except Exception as e:  # noqa: BLE001
                    errs[sk] = str(e)
        elif dep in ("QAT", "UAT"):
            for spec in _nonprod_backend_specs(dep):
                key = f"{dep}:{spec['belongs']}"
                specs.append((key, _nonprod_backend_spec(spec)))
        else:
            errs[dep] = f"unknown deployment {dep!r}"
    return specs, errs


class _WebmachineScrapeWarm:
    """One long-lived EGM browser for a single backend (read-only scrape for webmachine_data.json)."""

    def __init__(self, label: str, spec: BackendScrapeSpec) -> None:
        self.label = label
        self.spec = dict(spec)
        self._tasks: _queue.Queue[dict] = _queue.Queue()
        self._p = None
        self._context = None
        self._page = None
        self._list_url = ""
        self._thread = threading.Thread(
            target=self._loop, name=f"wm-warm-{label}", daemon=True
        )
        self._thread.start()

    def submit_prewarm(self) -> None:
        self._tasks.put({"kind": "prewarm"})

    def submit_keepalive(self) -> None:
        self._tasks.put({"kind": "keepalive"})

    def collect(self, **kwargs: Any) -> tuple[list[dict], str | None]:
        done = threading.Event()
        box: dict[str, Any] = {}
        self._tasks.put({"kind": "collect", "kwargs": kwargs, "done": done, "box": box})
        done.wait()
        if box.get("error"):
            raise RuntimeError(str(box["error"]))
        return list(box.get("rows") or []), box.get("warn")

    def _loop(self) -> None:
        while True:
            task = self._tasks.get()
            kind = task.get("kind")
            if kind == "prewarm":
                try:
                    self._ensure_ready(task.get("timeout_ms") or 120_000)
                    print(f"[wm-warm:{self.label}] pre-warmed (browser stays open).", flush=True)
                except Exception as ex:
                    print(f"[wm-warm:{self.label}] prewarm failed: {ex!r}", flush=True)
                    self._teardown()
                continue
            if kind == "keepalive":
                try:
                    if self._healthy():
                        self._refresh_table(task.get("timeout_ms") or 120_000)
                except Exception:
                    self._teardown()
                continue
            if kind == "collect":
                box = task["box"]
                try:
                    timeout_ms = int(task["kwargs"].get("timeout_ms") or 120_000)
                    self._ensure_ready(timeout_ms)
                    self._refresh_table(timeout_ms)
                    rows, warn = _smachine_collect_rows_on_egm_page(
                        self._page,
                        belongs=str(self.spec.get("belongs") or "—"),
                        deployment=str(self.spec.get("deployment") or "PROD"),
                        max_pages=task["kwargs"].get("max_pages"),
                        timeout_ms=timeout_ms,
                        stall_check=task["kwargs"].get("stall_check"),
                        on_progress=task["kwargs"].get("on_progress"),
                    )
                    box["rows"] = rows
                    box["warn"] = warn
                except Exception as ex:
                    box["error"] = ex
                    self._teardown()
                finally:
                    task["done"].set()

    def _healthy(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    def _launch(self) -> None:
        from playwright.sync_api import sync_playwright

        self._teardown()
        self._p = sync_playwright().start()
        profile = _wm_warm_profile_dir(self.label)
        profile.mkdir(parents=True, exist_ok=True)
        self._context = self._p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=_webmachine_warm_headless(),
            viewport={"width": 1600, "height": 900},
            ignore_https_errors=True,
        )
        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )
        print(f"[wm-warm:{self.label}] browser launched (kept open).", flush=True)

    def _teardown(self) -> None:
        for closer in (
            lambda: self._context.close() if self._context else None,
            lambda: self._p.stop() if self._p else None,
        ):
            try:
                closer()
            except Exception:
                pass
        self._context = None
        self._page = None
        self._p = None
        self._list_url = ""

    def _ensure_ready(self, timeout_ms: int) -> None:
        if not self._healthy():
            self._launch()
            self._page.set_default_timeout(timeout_ms)
            self._list_url = _smachine_login_and_open_egm_list(
                self._page,
                base_url=str(self.spec["base_url"]),
                username=str(self.spec["username"]),
                password=str(self.spec["password"]),
                list_path=str(self.spec.get("list_path") or "/egm/egmStatusList"),
                login_path=str(self.spec.get("login_path") or "/login"),
                dismiss_warning_dialog=bool(self.spec.get("dismiss_warning_dialog")),
                timeout_ms=timeout_ms,
            )

    def _refresh_table(self, timeout_ms: int) -> None:
        if not self._healthy():
            return
        try:
            if self._list_url:
                self._page.goto(self._list_url, wait_until="domcontentloaded", timeout=timeout_ms)
            if self.spec.get("dismiss_warning_dialog"):
                _dismiss_warning_dialog(self._page, timeout_ms)
            limit = _resolve_collect_page_limit(None)
            _go_first_page(self._page, timeout_ms=timeout_ms, max_steps=limit)
            _wait_table_idle(self._page, timeout_ms)
        except Exception:
            pass


class _WebmachineWarmPool:
    def __init__(self) -> None:
        self._workers: dict[str, _WebmachineScrapeWarm] = {}
        self._lock = threading.Lock()
        self._keepalive = threading.Thread(
            target=self._keepalive_loop, name="wm-warm-keepalive", daemon=True
        )
        self._keepalive.start()

    def _get(self, label: str, spec: BackendScrapeSpec) -> _WebmachineScrapeWarm:
        with self._lock:
            w = self._workers.get(label)
            if w is None:
                w = _WebmachineScrapeWarm(label, spec)
                self._workers[label] = w
            return w

    def prewarm_specs(self, specs: list[tuple[str, BackendScrapeSpec]]) -> None:
        for label, spec in specs:
            self._get(label, spec).submit_prewarm()

    def collect_specs(
        self, specs: list[tuple[str, BackendScrapeSpec]], **kwargs: Any
    ) -> tuple[list[dict], dict[str, str]]:
        errs: dict[str, str] = {}
        all_rows: list[dict] = []
        workers_n = _scrape_concurrency(len(specs))

        def _one(label: str, spec: BackendScrapeSpec) -> tuple[str, list[dict], str | None, Exception | None]:
            try:
                rows, warn = self._get(label, spec).collect(**kwargs)
                return label, rows, warn, None
            except Exception as e:  # noqa: BLE001
                return label, [], None, e

        if workers_n <= 1 or len(specs) <= 1:
            for label, spec in specs:
                _label, rows, warn, err = _one(label, spec)
                if err is not None:
                    errs[label] = str(err)
                    continue
                all_rows.extend(rows)
                if warn:
                    errs[label] = warn
            return all_rows, errs

        results: dict[str, tuple[list[dict], str | None, Exception | None]] = {}
        with ThreadPoolExecutor(max_workers=workers_n, thread_name_prefix="wm-warm-collect") as ex:
            futs = {ex.submit(_one, label, spec): label for label, spec in specs}
            for fut in as_completed(futs):
                label, rows, warn, err = fut.result()
                results[label] = (rows, warn, err)
        for label, _spec in specs:
            rows, warn, err = results.get(label, ([], None, None))
            if err is not None:
                errs[label] = str(err)
                continue
            all_rows.extend(rows)
            if warn:
                errs[label] = warn
        return all_rows, errs

    def _keepalive_loop(self) -> None:
        while True:
            time.sleep(_WEBMACHINE_WARM_KEEPALIVE_SEC)
            with self._lock:
                workers = list(self._workers.values())
            for w in workers:
                w.submit_keepalive()


_webmachine_warm_pool_singleton: _WebmachineWarmPool | None = None
_webmachine_warm_pool_lock = threading.Lock()


def _webmachine_warm_pool() -> _WebmachineWarmPool:
    global _webmachine_warm_pool_singleton
    with _webmachine_warm_pool_lock:
        if _webmachine_warm_pool_singleton is None:
            _webmachine_warm_pool_singleton = _WebmachineWarmPool()
        return _webmachine_warm_pool_singleton


def prewarm_webmachine_scrape_pool_on_startup() -> None:
    """Launch + EGM-login one persistent browser per backend for webmachine_data.json (stays open)."""
    if not _webmachine_warm_pool_enabled():
        print("[wm-warm] disabled (WEBMACHINE_WARM_POOL=0).", flush=True)
        return
    if not _webmachine_warm_prewarm_on_startup():
        print("[wm-warm] startup pre-warm skipped (WEBMACHINE_WARM_PREWARM_ON_STARTUP=0).", flush=True)
        return
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print(
            "[wm-warm] startup pre-warm skipped — playwright not installed "
            "(pip install playwright && playwright install chromium). "
            "Set WEBMACHINE_WARM_POOL=0 to silence.",
            flush=True,
        )
        return
    specs, skipped = _all_deployment_backend_specs()
    if skipped:
        for k, v in skipped.items():
            print(f"[wm-warm] note {k}: {v}", flush=True)
    if not specs:
        print("[wm-warm] no backends configured — nothing to pre-warm.", flush=True)
        return
    labels = ", ".join(lbl for lbl, _ in specs)
    print(f"[wm-warm] startup pre-warm ({len(specs)} browser(s), kept open): {labels}", flush=True)
    try:
        _webmachine_warm_pool().prewarm_specs(specs)
    except Exception as ex:
        print(f"[wm-warm] startup pre-warm failed: {ex!r}", flush=True)


def smachine_collect_machines_all_deployments(
    **kwargs: Any,
) -> tuple[list[dict], dict[str, str]]:
    """
    Scrape configured deployments (``WEBMACHINE_DEPLOYMENTS``, default ``prod,qat,uat``).

    All backends across **all** deployments are loaded in a **single shared thread pool**, so
    PROD/QAT/UAT pages open at the same time (subject to ``WEBMACHINE_SCRAPE_CONCURRENCY``; set it
    to ``0`` for truly unlimited / everything at once). This minimises the staleness window.

    When ``WEBMACHINE_WARM_POOL=1`` (default), each backend uses a **persistent headed browser**
    that stays open between scrapes (for ``webmachine_data.json`` background refresh).
    """
    if _webmachine_warm_pool_enabled():
        specs, errs = _all_deployment_backend_specs(**kwargs)
        rows, scrape_errs = _webmachine_warm_pool().collect_specs(specs, **kwargs)
        errs.update(scrape_errs)
        return rows, errs

    raw = (os.environ.get("WEBMACHINE_DEPLOYMENTS") or "prod,qat,uat").strip()
    deployments = [d.strip().upper() for d in raw.split(",") if d.strip()]
    if not deployments:
        deployments = ["PROD"]

    units: list[ScrapeUnit] = []
    errs: dict[str, str] = {}
    for dep in deployments:
        if dep == "PROD":
            dep_units, skipped = _prod_scrape_units(**kwargs)
            units.extend(dep_units)
            errs.update(skipped)
        elif dep in ("QAT", "UAT"):
            dep_units, dep_err = _nonprod_scrape_units(dep, **kwargs)
            units.extend(dep_units)
            errs.update(dep_err)
        else:
            errs[dep] = f"unknown deployment {dep!r}"

    rows, scrape_errs = _collect_units(units)
    errs.update(scrape_errs)
    return rows, errs


def main() -> None:
    if len(sys.argv) < 2:
        print(
            __doc__.strip(),
            file=sys.stderr,
        )
        sys.exit(2)

    site = sys.argv[1].strip()
    site_key = site.lower()

    if site_key in ("batchbuttontest", "batchbuttonprobe", "probbatch"):
        optional_sites = [x.strip().lower() for x in sys.argv[2:] if x.strip()]
        _run_batch_button_probe_cli(optional_sites or None)
        return

    if len(sys.argv) > 2:
        raw_targets = [x.strip() for x in sys.argv[2:] if x.strip()]
    else:
        raw_targets = []
        tty_in = sys.stdin.isatty()
        if tty_in:
            print(
                "Machine lines: type one name per line, then press Enter on an empty line to start.\n"
                "Tip: one-shot — python3 smmachine.py <site> <machine> <machine> …",
                file=sys.stderr,
            )
        for line in sys.stdin:
            s = line.strip()
            # Interactive terminal: EOF is easy to forget; empty line ends input (pipes still use EOF).
            if tty_in and not s and raw_targets:
                break
            if s:
                raw_targets.append(s)

    if not raw_targets:
        print("No machine lines provided (stdin or argv after site).", file=sys.stderr)
        sys.exit(2)

    batch_action = _BATCH_CLI_MODES.get(site_key)
    if batch_action:
        _run_batch_cli_mode(batch_action, raw_targets)
        return

    synth = _site_synthetic_machine(site)

    targets: list[tuple[str, str, str]] = []
    for line in raw_targets:
        kind, key = _parse_target_line(line)
        targets.append((line, kind, key))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Install playwright: pip install playwright && playwright install chromium")

    from checkcredit import NP_BACKEND_MAX_PAGES, _np_log_backend_tag, _np_resolve_backend  # noqa: WPS433

    base, user, pw = _np_resolve_backend(synth)
    tag = _np_log_backend_tag(synth)
    if not user or not pw:
        raise SystemExit(f"Missing backend credentials for routed backend {tag} (see checkcredit env vars).")

    path = (os.environ.get("SM_MACHINE_PATH") or "/egm/egmStatusList").strip() or "/egm/egmStatusList"
    if not path.startswith("/"):
        path = "/" + path
    login_url = f"{base}/login?redirect={quote(path, safe='')}"
    list_url = f"{base}{path}"

    try:
        max_pages = max(1, int((os.environ.get("SM_MACHINE_MAX_PAGES") or "").strip() or str(NP_BACKEND_MAX_PAGES)))
    except ValueError:
        max_pages = max(1, NP_BACKEND_MAX_PAGES)

    try:
        afk_sec = max(0, int((os.environ.get("SM_MACHINE_AFK_SEC") or "90").strip() or "90"))
    except ValueError:
        afk_sec = 90

    # Default: show a window when possible so you can confirm ticks during AFK.
    headless = _smachine_resolve_headless(None)

    timeout_ms = 120_000

    sk = (site or "").strip().lower()
    report_only = len(sk) > 2 and sk.endswith("cs")
    print(f"Site alias: {site!r} → backend tag {tag!r} ({base})")
    if report_only:
        base_alias = _site_routing_key(site)
        print(
            f"Mode: status report only (suffix 'cs' = check status; same backend as {base_alias!r}; "
            "no checkbox changes)."
        )
    print(f"Targets (order): {raw_targets}")

    pending = targets.copy()
    next_clicks = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                viewport={"width": 1600, "height": 900},
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.set_default_timeout(timeout_ms)

            page.goto(login_url, wait_until="domcontentloaded")
            page.wait_for_timeout(900)

            pwd_box = page.locator('input[type="password"]').first
            pwd_box.wait_for(state="visible", timeout=min(30_000, timeout_ms))
            form = pwd_box.locator("xpath=ancestor::form[1]")
            if form.count():
                tin = form.locator(
                    'input[type="text"], input:not([type]), input[type="tel"], input[type="email"]'
                ).first
                tin.fill(user)
            else:
                page.locator('input[type="text"]').first.fill(user)
            pwd_box.fill(pw)
            lb = page.get_by_role("button", name=re.compile(r"login|sign in|log in", re.I))
            if lb.count():
                lb.first.click()
            else:
                page.locator('button[type="submit"], button.el-button--primary').first.click()

            page.wait_for_timeout(1800)
            if path not in (page.url or ""):
                page.goto(list_url, wait_until="domcontentloaded")

            page.wait_for_selector(".app-container, .filter-container, .el-table", timeout=timeout_ms)
            _wait_table_idle(page, timeout_ms)

            _go_first_page(page, timeout_ms=timeout_ms, max_steps=max_pages)
            _wait_table_idle(page, timeout_ms)

            if report_only:
                report = _scan_targets_report_only(page, targets, timeout_ms=timeout_ms, max_pages=max_pages)
                print("")
                _print_check_status_groups(report, targets)
                print(f"AFK {afk_sec}s — inspect the browser; close the window manually when done.")
                time.sleep(afk_sec)
                return

            tick_targets: list[tuple[str, str, str]] = []
            skipped_report: list[tuple[str, str, str]] = []

            # --- Forward: tick eligible targets only ---
            safety = 0
            while pending:
                safety += 1
                if safety > max_pages * max(len(targets), 1) + 50:
                    raise RuntimeError("Forward scan exceeded safety iteration limit.")

                matched_this_page: list[tuple[str, str, str]] = []
                for spec in list(pending):
                    _line, kind, key = spec
                    row = _find_row_for_target(page, kind, key, timeout_ms)
                    if row is None:
                        continue
                    summ = _row_summary_label(row, timeout_ms=timeout_ms)
                    ok_elig, why_not = _row_tick_eligibility(row, timeout_ms=timeout_ms)
                    if not ok_elig:
                        print(
                            f"  Skip (ineligible): {_line!r} → {summ!r} — {why_not}",
                            file=sys.stderr,
                        )
                        _ensure_row_checkbox_unchecked(page, row, timeout_ms=timeout_ms)
                        skipped_report.append((_line, summ, why_not))
                        matched_this_page.append(spec)
                        continue

                    print(f"  Tick (forward): {_line!r} → {summ!r}")
                    _ensure_row_checkbox_checked(page, row, timeout_ms=timeout_ms)
                    if not _verify_row_checkbox_checked(page, row, timeout_ms=timeout_ms):
                        raise RuntimeError(f"Checkbox for {_line!r} did not read as checked after tick.")
                    matched_this_page.append(spec)
                    tick_targets.append(spec)

                for spec in matched_this_page:
                    pending.remove(spec)

                if not pending:
                    break

                if not _can_pagination_next(page):
                    missing = [s[0] for s in pending]
                    raise RuntimeError(f"No Next page; still missing machines: {missing}")

                if next_clicks >= max_pages:
                    missing = [s[0] for s in pending]
                    raise RuntimeError(f"Hit SM_MACHINE_MAX_PAGES ({max_pages}); missing: {missing}")

                _click_pagination_next(page, timeout_ms=timeout_ms)
                next_clicks += 1
                _wait_table_idle(page, timeout_ms)

            if skipped_report:
                print("", file=sys.stderr)
                print(
                    "Will not tick these machines (checkbox cleared if it was checked), since they are in "
                    "maintenance status or offline, or status is not normal/occupy:",
                    file=sys.stderr,
                )
                for sl, lbl, rs in skipped_report:
                    print(f"  {sl!r} → {lbl} — {rs}", file=sys.stderr)
                print("", file=sys.stderr)

            # --- Backward: re-verify every page (only rows we ticked) ---
            confirmed: dict[tuple[str, str], str] = {}

            for step in range(next_clicks, -1, -1):
                print(f"Re-verify page (backward step {next_clicks - step}/{next_clicks})…")
                _wait_table_idle(page, timeout_ms)
                for _line, kind, key in tick_targets:
                    row = _find_row_for_target(
                        page, kind, key, timeout_ms, prefer_checked=True
                    )
                    if row is None:
                        continue
                    if not _verify_row_checkbox_checked(page, row, timeout_ms=timeout_ms):
                        # Element UI often clears row selection when leaving the page unless the table uses
                        # reserve-selection — DOM then reads unchecked even though we ticked earlier.
                        if _truthy_env("SM_MACHINE_STRICT_BACKWARD"):
                            raise RuntimeError(
                                f"Backward verify failed: {_line!r} row present but checkbox not checked "
                                f"({_row_summary_label(row, timeout_ms=timeout_ms)!r}). "
                                f"Try enabling reserve-selection on the table, or omit SM_MACHINE_STRICT_BACKWARD "
                                f"to allow one automatic re-tick during backward pass."
                            )
                        print(
                            f"  Backward: {_line!r} reads unchecked after paging — "
                            f"re-ticking once (selection often clears across pages in Element UI).",
                            file=sys.stderr,
                        )
                        _ensure_row_checkbox_checked(page, row, timeout_ms=timeout_ms)
                        if not _verify_row_checkbox_checked(page, row, timeout_ms=timeout_ms):
                            raise RuntimeError(
                                f"Backward verify failed after re-tick: {_line!r} "
                                f"({_row_summary_label(row, timeout_ms=timeout_ms)!r})."
                            )
                    label = _row_summary_label(row, timeout_ms=timeout_ms)
                    if not label:
                        raise RuntimeError(f"Backward verify: empty row text for requested {_line!r}.")
                    confirmed[(kind, key)] = label

                if step > 0:
                    if not _can_pagination_prev(page):
                        raise RuntimeError("Expected Previous during backward walk but button disabled.")
                    _click_pagination_prev(page, timeout_ms=timeout_ms)

            for _line, kind, key in tick_targets:
                if (kind, key) not in confirmed:
                    raise RuntimeError(
                        f"Backward pass never re-located a row for {_line!r}; cannot verify checkbox state."
                    )

            print("")
            print("Verified ticked machines (re-checked on walk-back, checkbox read from DOM):")
            printed = 0
            for _line, kind, key in tick_targets:
                lbl = confirmed.get((kind, key))
                if lbl:
                    print(f"  {_line!r} → {lbl}")
                    printed += 1
            if printed == 0:
                print("  (none — no rows matched during backward pass)")

            print("")
            print(f"AFK {afk_sec}s — inspect the browser; close the window manually when done.")
            time.sleep(afk_sec)
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Lark bot: /{site}{set|unset}{maintenance|test|maintenancetest|…} + machine lines
# ---------------------------------------------------------------------------

PROD_BATCH_BOT_CARD_KEY = "prod_batch_set"
PROD_BATCH_BOT_CARD_CALLBACK_KEYS = frozenset({PROD_BATCH_BOT_CARD_KEY})

_PROD_BATCH_BOT_CMD_RE = re.compile(
    r"/(?P<site>nwr|np|nch|nc|new|tbr|tbp|mdr|dhs|cp|osm|wf|winford)"
    r"(?P<op>set|unset)"
    r"(?P<what>maintenancetest|testmaintenance|maintenance|test)\b",
    re.I,
)

_PROD_BATCH_SITE_ENV: dict[str, str] = {
    "nwr": "NWR",
    "np": "NWR",
    "nch": "NCH",
    "nc": "NCH",
    "new": "NCH",
    "tbr": "TBR",
    "tbp": "TBP",
    "mdr": "MDR",
    "dhs": "DHS",
    "cp": "CP",
    "osm": "CP",
    "wf": "WF",
    "winford": "WF",
}

_PROD_BATCH_PENDING: dict[str, dict[str, Any]] = {}
_PROD_BATCH_PENDING_LOCK = threading.Lock()
_PROD_BATCH_PENDING_TTL_SEC = 600

_PROD_BATCH_JOBS: dict[str, dict[str, Any]] = {}
_PROD_BATCH_JOBS_LOCK = threading.Lock()

_PROD_BATCH_ENV_TO_SITE: dict[str, str] = {
    "NWR": "nwr",
    "NCH": "nch",
    "TBR": "tbr",
    "TBP": "tbp",
    "MDR": "mdr",
    "DHS": "dhs",
    "CP": "cp",
    "WF": "wf",
}


def _prod_batch_action_from_parts(op: str, what: str) -> str | None:
    op_l = (op or "").strip().lower()
    what_l = (what or "").strip().lower()
    set_map = {
        "maintenance": "set_maint",
        "test": "set_test",
        "maintenancetest": "set_both",
        "testmaintenance": "set_both",
    }
    unset_map = {
        "maintenance": "unset_maint",
        "test": "unset_test",
        "maintenancetest": "unset_both",
        "testmaintenance": "unset_both",
    }
    if op_l == "set":
        return set_map.get(what_l)
    if op_l == "unset":
        return unset_map.get(what_l)
    return None


def _prod_batch_machine_env_from_name(machine_name: str) -> str | None:
    """Match SET PROD MACHINE page (``wm_prod_set`` ``machineEnvFromName``)."""
    raw = (machine_name or "").strip()
    if not raw:
        return None
    seg = raw.replace("\\", "/").split("/")[-1].strip()
    alnum = re.sub(r"[^A-Za-z0-9]", "", seg).upper()
    if re.match(r"^DHS", seg, re.I) or alnum.startswith("DHS"):
        return "DHS"
    if re.match(r"^NCH", seg, re.I) or alnum.startswith("NCH"):
        return "NCH"
    if re.match(r"^OSM", seg, re.I) or alnum.startswith("OSM"):
        return "CP"
    if re.match(r"^CP", seg, re.I) or alnum.startswith("CP"):
        return "CP"
    if re.match(r"^MDR", seg, re.I) or alnum.startswith("MDR"):
        return "MDR"
    if re.match(r"^TBR", seg, re.I) or alnum.startswith("TBR"):
        return "TBR"
    if re.match(r"^TBP", seg, re.I) or alnum.startswith("TBP"):
        return "TBP"
    if re.match(r"^NWR", seg, re.I) or alnum.startswith("NWR") or re.search(r"NWR[0-9]", alnum):
        return "NWR"
    if re.search(r"winford", raw, re.I):
        return "WF"
    if re.match(r"^WF", seg, re.I) or alnum.startswith("WF"):
        return "WF"
    return None


def _prod_batch_row_matches_env(row: dict, env_code: str) -> bool:
    env = (env_code or "").strip().upper()
    if not env or env == "ALL":
        return True
    belongs = str(row.get("belongs") or "").upper()
    machine = str(row.get("name") or row.get("machine") or "")
    if env == "NWR":
        return _prod_batch_machine_env_from_name(machine) == "NWR"
    if env == "CP":
        return belongs in ("CP", "OSM") or _prod_batch_machine_env_from_name(machine) == "CP"
    return belongs == env or _prod_batch_machine_env_from_name(machine) == env


def _prod_batch_split_target_tokens(line: str) -> list[str]:
    """
    One pasted machine name per line (may contain spaces, e.g. ``5 Dragons-NWR2113``).

    Only ``,`` / ``;`` split multiple names on the same line — never split on whitespace
    inside a display name (otherwise ``5`` matches every machine with ``5`` in the title).
    """
    line = (line or "").strip()
    if not line:
        return []
    if re.search(r"[,;&]", line):
        return [p.strip() for p in re.split(r"[,;&]+", line) if p.strip()]
    # Full display name with spaces + asset digits — keep whole line.
    if re.search(r"(?:NWR|MDR|NCH|TBR|TBP|DHS|CP|OSM|WF|WINFORD)\s*-?\s*\d", line, re.I):
        return [line]
    if re.search(r"\d", line) and len(line) > 12:
        return [line]
    # Same-line shorthand: ``NWR2113 NWR2114`` or ``2113 2114``
    parts = line.split()
    if len(parts) > 1:
        return parts
    return [line]


def _prod_batch_strip_mention_text(text: str, mention_keys: Sequence[str]) -> str:
    t = text or ""
    for key in mention_keys:
        t = t.replace(key, "")
    t = re.sub(r"@_user_\d+", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    return t


def parse_prod_batch_bot_command(text: str) -> dict[str, Any] | None:
    m = _PROD_BATCH_BOT_CMD_RE.search(text or "")
    if not m:
        return None
    site = m.group("site").lower()
    action = _prod_batch_action_from_parts(m.group("op"), m.group("what"))
    env_code = _PROD_BATCH_SITE_ENV.get(site)
    if not action or not env_code:
        return None
    return {
        "action": action,
        "env_code": env_code,
        "site": site,
        "match": m,
    }


def is_prod_batch_bot_message(original_text: str, mention_keys: Sequence[str]) -> bool:
    body = _prod_batch_strip_mention_text(original_text, mention_keys)
    return parse_prod_batch_bot_command(body) is not None


# ---------------------------------------------------------------------------
# Lark bot: @bot /sm — env picker → action form → confirm (thread replies)
# ---------------------------------------------------------------------------

_PROD_BATCH_SM_CMD_RE = re.compile(r"/sm\b", re.I)

_PROD_BATCH_SM_ENV_CODES: tuple[str, ...] = (
    "NWR",
    "NCH",
    "TBR",
    "TBP",
    "MDR",
    "DHS",
    "CP",
    "WF",
)

_PROD_BATCH_SM_ACTION_BUTTONS: tuple[tuple[str, str], ...] = (
    ("set_maint", "Set maintenance"),
    ("set_test", "Set test"),
    ("set_both", "Set both"),
    ("unset_maint", "Unset maintenance"),
    ("unset_test", "Unset test"),
    ("unset_both", "Unset both"),
)

_PROD_BATCH_SM_SESSIONS: dict[str, dict[str, Any]] = {}
_PROD_BATCH_SM_SESSIONS_LOCK = threading.Lock()
_PROD_BATCH_SM_SESSION_TTL_SEC = 7200
_PROD_BATCH_SM_STATE_FILE = (
    os.environ.get("PROD_BATCH_SM_STATE_FILE") or ".prod_batch_sm_sessions.json"
).strip()
_PROD_BATCH_SM_LOADED = False


def _prod_batch_sm_state_path() -> Path:
    p = Path(_PROD_BATCH_SM_STATE_FILE)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent / p
    return p


def _prod_batch_sm_load_sessions_from_disk() -> None:
    global _PROD_BATCH_SM_LOADED
    path = _prod_batch_sm_state_path()
    if not path.is_file():
        _PROD_BATCH_SM_LOADED = True
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            now = time.time()
            for sid, ent in raw.items():
                if not isinstance(ent, dict):
                    continue
                if now - float(ent.get("created_at") or 0) > _PROD_BATCH_SM_SESSION_TTL_SEC:
                    continue
                _PROD_BATCH_SM_SESSIONS[str(sid)] = ent
    except Exception as exc:
        print(f"[prod_batch_sm] load sessions failed: {exc!r}", flush=True)
    _PROD_BATCH_SM_LOADED = True


def _prod_batch_sm_save_sessions_to_disk() -> None:
    path = _prod_batch_sm_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _PROD_BATCH_SM_SESSIONS_LOCK:
            payload = dict(_PROD_BATCH_SM_SESSIONS)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[prod_batch_sm] save sessions failed: {exc!r}", flush=True)


def _prod_batch_sm_ensure_sessions_loaded() -> None:
    if _PROD_BATCH_SM_LOADED:
        return
    with _PROD_BATCH_SM_SESSIONS_LOCK:
        if not _PROD_BATCH_SM_LOADED:
            _prod_batch_sm_load_sessions_from_disk()


def _prod_batch_sm_chat_match(stored: str, incoming: str) -> bool:
    a = (stored or "").strip()
    b = (incoming or "").strip()
    return bool(a and b and a == b)


def _prod_batch_sm_upsert_session(
    session_id: str,
    *,
    chat_id: str,
    thread_root_message_id: str | None = None,
    env_code: str | None = None,
) -> dict[str, Any]:
    sid = (session_id or "").strip()
    _prod_batch_sm_ensure_sessions_loaded()
    with _PROD_BATCH_SM_SESSIONS_LOCK:
        ent = dict(_PROD_BATCH_SM_SESSIONS.get(sid) or {})
        ent["chat_id"] = (chat_id or "").strip()
        if thread_root_message_id:
            ent["thread_root_message_id"] = (thread_root_message_id or "").strip() or None
        if env_code:
            ent["env_code"] = (env_code or "").strip().upper()
        ent["created_at"] = time.time()
        _PROD_BATCH_SM_SESSIONS[sid] = ent
    _prod_batch_sm_save_sessions_to_disk()
    return dict(ent)


def is_prod_batch_sm_command(original_text: str, mention_keys: Sequence[str]) -> bool:
    body = _prod_batch_strip_mention_text(original_text, mention_keys)
    return bool(_PROD_BATCH_SM_CMD_RE.search(body or ""))


def _prod_batch_sm_cleanup_sessions() -> None:
    now = time.time()
    changed = False
    _prod_batch_sm_ensure_sessions_loaded()
    with _PROD_BATCH_SM_SESSIONS_LOCK:
        expired = [
            sid
            for sid, ent in _PROD_BATCH_SM_SESSIONS.items()
            if now - float(ent.get("created_at") or 0) > _PROD_BATCH_SM_SESSION_TTL_SEC
        ]
        for sid in expired:
            _PROD_BATCH_SM_SESSIONS.pop(sid, None)
            changed = True
    if changed:
        _prod_batch_sm_save_sessions_to_disk()


def _prod_batch_sm_get_session(session_id: str) -> dict[str, Any] | None:
    sid = (session_id or "").strip()
    if not sid:
        return None
    _prod_batch_sm_ensure_sessions_loaded()
    with _PROD_BATCH_SM_SESSIONS_LOCK:
        ent = _PROD_BATCH_SM_SESSIONS.get(sid)
        if not ent:
            return None
        if time.time() - float(ent.get("created_at") or 0) > _PROD_BATCH_SM_SESSION_TTL_SEC:
            _PROD_BATCH_SM_SESSIONS.pop(sid, None)
            return None
        return dict(ent)


def _prod_batch_sm_touch_session(session_id: str, **updates: Any) -> dict[str, Any] | None:
    sid = (session_id or "").strip()
    if not sid:
        return None
    _prod_batch_sm_ensure_sessions_loaded()
    with _PROD_BATCH_SM_SESSIONS_LOCK:
        ent = _PROD_BATCH_SM_SESSIONS.get(sid)
        if not ent:
            return None
        ent.update(updates)
        ent["created_at"] = time.time()
        out = dict(ent)
    _prod_batch_sm_save_sessions_to_disk()
    return out


def _prod_batch_form_field_text(
    name: str,
    *,
    action_obj: dict | None = None,
    parsed: dict | None = None,
) -> str:
    def _text(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v).strip()
        if isinstance(v, dict):
            for key in ("text", "value", "input", "content"):
                t = _text(v.get(key))
                if t:
                    return t
        if isinstance(v, list) and v:
            return _text(v[0])
        return ""

    if isinstance(action_obj, dict):
        fv = action_obj.get("form_value")
        if isinstance(fv, dict):
            t = _text(fv.get(name))
            if t:
                return t
    if isinstance(parsed, dict):
        fv = parsed.get("form_value")
        if isinstance(fv, dict):
            t = _text(fv.get(name))
            if t:
                return t
        t = _text(parsed.get(name))
        if t:
            return t
    return ""


def _prod_batch_target_lines_from_text(raw: str) -> list[str]:
    lines: list[str] = []
    for line in (raw or "").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        lines.extend(_prod_batch_split_target_tokens(line))
    return lines


def _prod_batch_sm_refresh_thread_root(chat_id: str, thread_root: str | None) -> None:
    if not thread_root:
        return
    try:
        import main as _main_mod  # noqa: WPS433

        _main_mod._set_prod_batch_thread_root(chat_id, thread_root)
    except Exception:
        pass


def _prod_batch_sm_env_picker_card(session_id: str, *, thread_root: str | None = None) -> dict:
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**Step 1 — Choose environment**",
            },
        }
    ]
    thread_root = (thread_root or "").strip() or None
    for i in range(0, len(_PROD_BATCH_SM_ENV_CODES), 4):
        chunk = _PROD_BATCH_SM_ENV_CODES[i : i + 4]
        columns = []
        for env in chunk:
            cb_value: dict[str, Any] = {
                "k": PROD_BATCH_BOT_CARD_KEY,
                "a": "sm_env",
                "s": session_id,
                "e": env,
            }
            if thread_root:
                cb_value["r"] = thread_root
            columns.append(
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": env},
                            "type": "primary",
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": cb_value,
                                }
                            ],
                        }
                    ],
                }
            )
        elements.append({"tag": "column_set", "flex_mode": "bisect", "columns": columns})
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "Set machine (/sm)"[:80]},
        },
        "body": {"elements": elements},
    }


def _prod_batch_sm_action_form_card(
    session_id: str,
    env_code: str,
    *,
    thread_root: str | None = None,
) -> dict:
    thread_root = (thread_root or "").strip() or None
    form_elements: list[dict] = [
        {
            "tag": "input",
            "name": "machines",
            "input_type": "multiline_text",
            "rows": 6,
            "auto_resize": True,
            "max_rows": 15,
            "width": "fill",
            "label": {"tag": "plain_text", "content": "Machine name(s)"},
            "label_position": "top",
            "placeholder": {
                "tag": "plain_text",
                "content": "One per line — e.g. NCH1299",
            },
            "required": True,
            "max_length": 4000,
        },
    ]
    for row_start in (0, 3):
        row_actions = _PROD_BATCH_SM_ACTION_BUTTONS[row_start : row_start + 3]
        columns = []
        for act, label in row_actions:
            cb_value: dict[str, Any] = {
                "k": PROD_BATCH_BOT_CARD_KEY,
                "a": "sm_action",
                "s": session_id,
                "act": act,
            }
            if thread_root:
                cb_value["r"] = thread_root
            columns.append(
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": label[:40]},
                            "type": "primary" if act.startswith("set_") else "default",
                            "form_action_type": "submit",
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": cb_value,
                                }
                            ],
                        }
                    ],
                }
            )
        form_elements.append({"tag": "column_set", "flex_mode": "bisect", "columns": columns})
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": f"Set machine — {env_code}"[:80],
            },
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**Environment:** {env_code}\n\n"
                            "**Step 2** — Enter machine name(s), then tap an action button."
                        ),
                    },
                },
                {"tag": "form", "name": "sm_batch_form", "elements": form_elements},
            ]
        },
    }


def handle_prod_batch_sm_command(
    *,
    chat_id: str,
    send_message: Callable[..., Any],
    thread_root_message_id: str | None = None,
) -> tuple[bool, str | None]:
    """Start ``/sm`` wizard — environment picker card in thread."""
    _prod_batch_cleanup_pending()
    _prod_batch_sm_cleanup_sessions()
    session_id = uuid.uuid4().hex[:16]
    thread_root = (thread_root_message_id or "").strip() or None
    _prod_batch_sm_upsert_session(
        session_id,
        chat_id=chat_id,
        thread_root_message_id=thread_root,
    )
    card = _prod_batch_sm_env_picker_card(session_id, thread_root=thread_root)
    _prod_batch_send_lark_card(chat_id, card, send_message)
    return True, None


def try_prod_batch_sm_env_card_response(
    parsed: dict[str, Any],
    *,
    chat_id: str,
) -> dict[str, Any] | None:
    """
    Build synchronous card.callback HTTP body for Step 1 → Step 2 (in-place card update).
    Returns ``None`` when this callback is not ``sm_env``.
    """
    action_btn = str(parsed.get("a") or "").strip().lower()
    if action_btn != "sm_env":
        return None

    session_id = str(parsed.get("s") or "").strip()
    env_code = str(parsed.get("e") or "").strip().upper()
    thread_root = str(parsed.get("r") or "").strip() or None

    if env_code not in _PROD_BATCH_SM_ENV_CODES:
        return {
            "toast": {
                "type": "error",
                "content": f"Unknown environment: {env_code}",
            }
        }

    session = _prod_batch_sm_get_session(session_id)
    if session and not _prod_batch_sm_chat_match(str(session.get("chat_id") or ""), chat_id):
        return {
            "toast": {
                "type": "error",
                "content": "Session chat mismatch. Send @bot /sm again.",
            }
        }

    if not session:
        _prod_batch_sm_upsert_session(
            session_id,
            chat_id=chat_id,
            thread_root_message_id=thread_root,
            env_code=env_code,
        )
    else:
        thread_root = thread_root or (session.get("thread_root_message_id") or "").strip() or None
        _prod_batch_sm_touch_session(session_id, env_code=env_code)

    _prod_batch_sm_refresh_thread_root(chat_id, thread_root)
    card = _prod_batch_sm_action_form_card(session_id, env_code, thread_root=thread_root)
    return {"card": {"type": "raw", "data": card}}


def _prod_batch_sm_on_env_picked(
    parsed: dict[str, Any],
    chat_id: str,
    send_message: Callable[..., Any],
) -> bool:
    """Fallback when synchronous card update is unavailable — send Step 2 as a new message."""
    session_id = str(parsed.get("s") or "").strip()
    env_code = str(parsed.get("e") or "").strip().upper()
    thread_root = str(parsed.get("r") or "").strip() or None
    session = _prod_batch_sm_get_session(session_id)
    if not session:
        if not session_id or env_code not in _PROD_BATCH_SM_ENV_CODES:
            send_message(chat_id, "⏭️ Session expired. Send `@bot /sm` again.")
            return True
        session = _prod_batch_sm_upsert_session(
            session_id,
            chat_id=chat_id,
            thread_root_message_id=thread_root,
            env_code=env_code,
        )
    if env_code not in _PROD_BATCH_SM_ENV_CODES:
        send_message(chat_id, f"❌ Unknown environment: {env_code!r}")
        return True
    if not _prod_batch_sm_chat_match(str(session.get("chat_id") or ""), chat_id):
        send_message(chat_id, "❌ Session chat mismatch. Send `@bot /sm` again.")
        return True
    _prod_batch_sm_touch_session(session_id, env_code=env_code)
    thread_root = thread_root or (session.get("thread_root_message_id") or "").strip() or None
    _prod_batch_sm_refresh_thread_root(chat_id, thread_root)
    card = _prod_batch_sm_action_form_card(session_id, env_code, thread_root=thread_root)
    _prod_batch_send_lark_card(chat_id, card, send_message)
    return True


def _prod_batch_sm_bot_prepare_confirm(
    session_id: str,
    env_code: str,
    action: str,
    target_lines: list[str],
    *,
    chat_id: str,
    send_message: Callable[..., Any],
    thread_root_message_id: str | None,
) -> None:
    site = _PROD_BATCH_ENV_TO_SITE.get(env_code) or env_code.lower()
    matched, not_found, data_src = _prod_batch_resolve_confirm_targets(
        site, env_code, target_lines
    )
    if "stuck" in data_src.lower() or "stalled" in data_src.lower():
        send_message(chat_id, f"❌ {data_src}")
        return
    if not matched:
        nf = ", ".join(not_found[:20]) if not_found else "(none parsed)"
        send_message(chat_id, f"❌ No machines matched for **{env_code}**. Not found: {nf}")
        return

    token = uuid.uuid4().hex[:16]
    with _PROD_BATCH_PENDING_LOCK:
        _PROD_BATCH_PENDING[token] = {
            "action": action,
            "env_code": env_code,
            "machines": matched,
            "not_found": not_found,
            "chat_id": chat_id,
            "thread_root_message_id": (thread_root_message_id or "").strip() or None,
            "created_at": time.time(),
        }

    card = _prod_batch_confirm_card(
        token=token,
        action=action,
        env_code=env_code,
        matched=matched,
        not_found=not_found,
        data_src=data_src,
    )
    _prod_batch_send_lark_card(chat_id, card, send_message)


def _prod_batch_sm_on_action_submit(
    parsed: dict[str, Any],
    chat_id: str,
    send_message: Callable[..., Any],
    action_obj: dict | None,
) -> bool:
    session_id = str(parsed.get("s") or "").strip()
    action = str(parsed.get("act") or "").strip()
    session = _prod_batch_sm_get_session(session_id)
    if not session:
        send_message(chat_id, "⏭️ Session expired. Send `@bot /sm` again.")
        return True
    if not _prod_batch_sm_chat_match(str(session.get("chat_id") or ""), chat_id):
        send_message(chat_id, "❌ Session chat mismatch. Send `@bot /sm` again.")
        return True
    env_code = str(session.get("env_code") or "").strip().upper()
    if not env_code:
        send_message(chat_id, "❌ Pick an environment first.")
        return True
    from prod_machine_batch import ACTION_LABELS

    if action not in ACTION_LABELS:
        send_message(chat_id, f"❌ Unknown action: {action!r}")
        return True

    machines_raw = _prod_batch_form_field_text(
        "machines", action_obj=action_obj, parsed=parsed
    )
    target_lines = _prod_batch_target_lines_from_text(machines_raw)
    if not target_lines:
        send_message(chat_id, "❌ Enter at least one machine name in the text box.")
        return True

    thread_root = (
        str(parsed.get("r") or "").strip()
        or (session.get("thread_root_message_id") or "").strip()
        or None
    )
    _prod_batch_sm_refresh_thread_root(chat_id, thread_root)
    threading.Thread(
        target=_prod_batch_sm_bot_prepare_confirm,
        args=(session_id, env_code, action, target_lines),
        kwargs={
            "chat_id": chat_id,
            "send_message": send_message,
            "thread_root_message_id": thread_root,
        },
        daemon=True,
    ).start()
    return True


def _prod_batch_scrape_stall_sec() -> int:
    try:
        return max(60, int((os.environ.get("PROD_BATCH_SCRAPE_STALL_SEC") or "180").strip()))
    except ValueError:
        return 180


def _prod_batch_lookup_target_rows(
    site: str,
    env_code: str,
    target_lines: list[str],
) -> tuple[list[dict], list[str], str]:
    """
    Login once and paginate only until requested machine tokens are resolved (fast path).
    Falls back to the same stall detection as the old full-site scrape.
    """
    sk = (site or "").strip().lower()
    if not sk:
        return [], ["empty site"], "empty site"

    target_specs: list[tuple[str, str, str]] = []
    parse_not_found: list[str] = []
    for line in target_lines:
        for token in _prod_batch_split_target_tokens(line):
            try:
                kind, key = _parse_target_line(token)
            except ValueError:
                parse_not_found.append(token)
                continue
            target_specs.append((token, kind, key))

    if not target_specs:
        return [], parse_not_found, "no valid machine tokens"

    try:
        from checkcredit import _np_resolve_backend  # noqa: WPS433
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return [], parse_not_found, str(exc)

    try:
        synth = _site_synthetic_machine(sk)
    except SystemExit as exc:
        return [], parse_not_found, str(exc)

    base, user, pw = _np_resolve_backend(synth)
    if not user or not pw:
        return [], parse_not_found, f"missing credentials for {sk!r}"

    path = (os.environ.get("SM_MACHINE_PATH") or "/egm/egmStatusList").strip() or "/egm/egmStatusList"
    if not path.startswith("/"):
        path = "/" + path
    login_url = f"{base.rstrip('/')}/login?redirect={quote(path, safe='')}"
    list_url = f"{base.rstrip('/')}{path}"
    max_pages = _resolve_collect_page_limit(None)
    stall_sec = _prod_batch_scrape_stall_sec()
    timeout_ms = max(120_000, stall_sec * 1000 + 60_000)
    progress = {"last_at": time.monotonic()}
    progress_lock = threading.Lock()

    def on_progress(_pages: int, _rows: int) -> None:
        with progress_lock:
            progress["last_at"] = time.monotonic()

    def stall_check() -> bool:
        with progress_lock:
            idle = time.monotonic() - progress["last_at"]
        return idle >= stall_sec

    belong_label = _site_belongs_label(sk)
    rows: list[dict] = []
    scan_not_found: list[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    viewport={"width": 1600, "height": 900},
                    ignore_https_errors=True,
                )
                page.set_default_timeout(timeout_ms)
                on_progress(0, 0)

                page.goto(login_url, wait_until="domcontentloaded")
                page.wait_for_timeout(900)
                if stall_check():
                    raise RuntimeError(f"EGM scrape stalled (login page; no progress for {stall_sec}s)")

                pwd_box = page.locator('input[type="password"]').first
                pwd_box.wait_for(state="visible", timeout=min(30_000, timeout_ms))
                form = pwd_box.locator("xpath=ancestor::form[1]")
                if form.count():
                    tin = form.locator(
                        'input[type="text"], input:not([type]), input[type="tel"], input[type="email"]'
                    ).first
                    tin.fill(user)
                else:
                    page.locator('input[type="text"]').first.fill(user)
                pwd_box.fill(pw)
                lb = page.get_by_role("button", name=re.compile(r"login|sign in|log in", re.I))
                if lb.count():
                    lb.first.click()
                else:
                    page.locator('button[type="submit"], button.el-button--primary').first.click()

                page.wait_for_timeout(1800)
                on_progress(0, 0)
                if stall_check():
                    raise RuntimeError(f"EGM scrape stalled (after login; no progress for {stall_sec}s)")
                if path not in (page.url or ""):
                    page.goto(list_url, wait_until="domcontentloaded")

                page.wait_for_selector(".app-container, .filter-container, .el-table", timeout=timeout_ms)
                _wait_table_idle(page, timeout_ms)
                _go_first_page(page, timeout_ms=timeout_ms, max_steps=max_pages)
                _wait_table_idle(page, timeout_ms)
                on_progress(0, 0)

                rows, scan_not_found = _scan_targets_collect_rows(
                    page,
                    target_specs,
                    belongs=belong_label,
                    deployment="PROD",
                    timeout_ms=timeout_ms,
                    max_pages=max_pages,
                )
                on_progress(1, len(rows))
            finally:
                browser.close()
    except RuntimeError as exc:
        if "stalled" in str(exc).lower():
            return [], parse_not_found, (
                f"Scrape stuck — no progress for {stall_sec}s "
                f"(EGM login or table may be hung). Try again later."
            )
        raise
    except Exception as exc:
        logger.exception("prod-batch bot targeted lookup failed for %r", sk)
        return [], parse_not_found, str(exc)

    matched, resolve_not_found = resolve_prod_batch_bot_targets(env_code, target_lines, rows)
    not_found = list(dict.fromkeys(parse_not_found + scan_not_found + resolve_not_found))
    data_src = f"live EGM fast lookup ({sk.upper()}, {len(matched)} matched)"
    return matched, not_found, data_src


def _prod_batch_lookup_webmachine_data(
    env_code: str,
    target_lines: list[str],
) -> tuple[list[dict], list[str], str]:
    """
    Resolve target machines from ``webmachine_data.json`` (PROD rows only).

    Used for set/unset maintenance/test confirm cards — the JSON file is kept up to date by
    the scraper, so a live EGM paginated lookup is not needed just to build the machine list.
    """
    from maintenancemachineagent import _data_path_hint, _last_data_path, load_webmachine_rows

    rows = load_webmachine_rows()
    rows = [r for r in rows if str(r.get("environment") or "PROD").strip().upper() == "PROD"]
    if not rows:
        tokens: list[str] = []
        for line in target_lines:
            tokens.extend(_prod_batch_split_target_tokens(line))
        return [], tokens, _data_path_hint()

    matched, not_found = resolve_prod_batch_bot_targets(env_code, target_lines, rows)
    path_note = f"`{_last_data_path}`" if _last_data_path else "webmachine_data.json"
    data_src = f"webmachine_data.json ({len(matched)} matched) — {path_note}"
    return matched, not_found, data_src


def _prod_batch_resolve_confirm_targets(
    site: str,
    env_code: str,
    target_lines: list[str],
) -> tuple[list[dict], list[str], str]:
    """Machine list for the confirm card — ``webmachine_data.json`` by default."""
    use_live = (os.environ.get("PROD_BATCH_LIVE_LOOKUP") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if use_live:
        print(
            f"[prod-batch] confirm lookup: live EGM ({env_code}, {len(target_lines)} line(s))",
            flush=True,
        )
        return _prod_batch_lookup_target_rows(site, env_code, target_lines)
    print(
        f"[prod-batch] confirm lookup: webmachine_data.json ({env_code}, "
        f"{len(target_lines)} line(s))",
        flush=True,
    )
    return _prod_batch_lookup_webmachine_data(env_code, target_lines)


def _prod_batch_scrape_site_rows(site: str) -> tuple[list[dict], str]:
    """
    Live read-only EGM scrape for one PROD backend.

    While ``⏳ Scraping…`` is shown, only a **stall** (no login/table/page progress for
    ``PROD_BATCH_SCRAPE_STALL_SEC``, default 180s) is treated as a scrape error. Slow but
    moving scrapes are allowed to run until finished.
    """
    sk = (site or "").strip().lower()
    if not sk:
        return [], "empty site"

    stall_sec = _prod_batch_scrape_stall_sec()
    progress = {"last_at": time.monotonic()}
    progress_lock = threading.Lock()

    def on_progress(_pages: int, _rows: int) -> None:
        with progress_lock:
            progress["last_at"] = time.monotonic()

    def stall_check() -> bool:
        with progress_lock:
            idle = time.monotonic() - progress["last_at"]
        return idle >= stall_sec

    try:
        rows, twarn = smachine_collect_all_machine_rows(
            sk,
            headless=True,
            stall_check=stall_check,
            on_progress=on_progress,
            timeout_ms=max(120_000, stall_sec * 1000 + 60_000),
        )
        src = f"live EGM ({sk.upper()})"
        if twarn:
            src = f"{src} — {twarn}"
        return rows, src
    except RuntimeError as exc:
        if "stalled" in str(exc).lower():
            return [], (
                f"Scrape stuck — no progress for {stall_sec}s "
                f"(EGM login or table may be hung). Try again later."
            )
        raise
    except Exception as exc:
        logger.exception("prod-batch bot scrape failed for %r", sk)
        return [], str(exc)


def _prod_batch_format_live_summary_md(action: str, summary: dict, *, title_prefix: str) -> str:
    from prod_machine_batch import ACTION_LABELS

    ok = summary.get("success") or []
    fail = summary.get("failed") or []
    lines = [
        f"**{title_prefix} — {ACTION_LABELS.get(action, action)}**",
        f"**Done:** {len(ok)}",
        f"**Not done:** {len(fail)}",
        "",
    ]
    if ok:
        lines.append("**Done (goal met on EGM):**")
        for m in ok[:40]:
            lines.append(f"✓ {m.get('belongs', '')} — {m.get('machine', '')}")
        if len(ok) > 40:
            lines.append(f"... and {len(ok) - 40} more")
    if fail:
        lines.append("")
        lines.append("**Not done:**")
        for m in fail[:40]:
            err = (m.get("error") or "").strip()
            suffix = f" ({err})" if err else ""
            lines.append(f"✗ {m.get('belongs', '')} — {m.get('machine', '')}{suffix}")
        if len(fail) > 40:
            lines.append(f"... and {len(fail) - 40} more")
    return "\n".join(lines)


def _prod_batch_cancel_button(job_id: str) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "Cancel"},
        "type": "danger",
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "k": PROD_BATCH_BOT_CARD_KEY,
                    "j": job_id,
                    "a": "job_cancel",
                },
            }
        ],
    }


def _prod_batch_job_is_running(job_id: str) -> bool:
    with _PROD_BATCH_JOBS_LOCK:
        job = _PROD_BATCH_JOBS.get(job_id)
        return bool(job and job.get("status") == "running")


def _prod_batch_request_job_cancel(
    job_id: str,
    chat_id: str,
    send_message: Callable[..., Any],
) -> None:
    with _PROD_BATCH_JOBS_LOCK:
        job = _PROD_BATCH_JOBS.get(job_id)
        if not job:
            send_message(chat_id, "⏭️ Job not found or already finished.")
            return
        if job.get("status") != "running":
            send_message(chat_id, "⏭️ Job already finished.")
            return
        thread_root = (job.get("thread_root_message_id") or "").strip() or None
        job["cancel_requested"] = True
    if thread_root:
        try:
            import main as _main_mod  # noqa: WPS433

            _main_mod._set_prod_batch_thread_root(chat_id, thread_root)
        except Exception:
            pass
    send_message(chat_id, "🛑 Cancel requested — stopping after the current step…")


def _prod_batch_send_cancel_live_summary(
    job_id: str,
    send_message: Callable[..., Any],
) -> None:
    from prod_machine_batch import ACTION_LABELS, live_verify_prod_machines

    with _PROD_BATCH_JOBS_LOCK:
        job = _PROD_BATCH_JOBS.get(job_id)
        if not job or job.get("cancel_summary_sent"):
            return
        job["cancel_summary_sent"] = True
        job["status"] = "cancelled"
        action = str(job.get("action") or "")
        machines = list(job.get("machines") or [])
        chat_id = str(job.get("chat_id") or "")
        thread_root = (job.get("thread_root_message_id") or "").strip() or None

    if thread_root and chat_id:
        try:
            import main as _main_mod  # noqa: WPS433

            _main_mod._set_prod_batch_thread_root(chat_id, thread_root)
        except Exception:
            pass

    if not chat_id or not action or not machines:
        return

    try:
        summary = live_verify_prod_machines(action, machines)
    except Exception as exc:
        logger.exception("prod-batch bot cancel verify %s failed", job_id)
        summary = {
            "action": action,
            "success": [],
            "failed": [
                {
                    "belongs": m.get("belongs", ""),
                    "machine": m.get("machine") or m.get("name") or "",
                    "error": str(exc),
                }
                for m in machines
            ],
        }

    with _PROD_BATCH_JOBS_LOCK:
        if job_id in _PROD_BATCH_JOBS:
            _PROD_BATCH_JOBS[job_id]["summary"] = summary

    fail_n = len(summary.get("failed") or [])
    tpl = "red" if fail_n else "green"
    _prod_batch_send_lark_md(
        chat_id,
        f"Cancelled — {ACTION_LABELS.get(action, action)}",
        _prod_batch_format_live_summary_md(action, summary, title_prefix="Cancelled"),
        send_message,
        header_template=tpl,
    )
    _prod_batch_send_machine_screenshots_background(chat_id, machines, summary, send_message)


def resolve_prod_batch_bot_targets(
    env_code: str,
    target_lines: list[str],
    all_rows: list[dict],
) -> tuple[list[dict], list[str]]:
    matched: list[dict] = []
    not_found: list[str] = []
    seen: set[tuple[str, str]] = set()

    for line in target_lines:
        for token in _prod_batch_split_target_tokens(line):
            try:
                kind, key = _parse_target_line(token)
            except ValueError:
                not_found.append(token)
                continue
            hits: list[dict] = []
            for row in all_rows:
                if not _prod_batch_row_matches_env(row, env_code):
                    continue
                machine_name = str(row.get("name") or row.get("machine") or "").strip()
                if not machine_name:
                    continue
                if _row_text_matches(kind, key, machine_name):
                    hits.append(row)
            if not hits:
                not_found.append(token)
                continue
            for row in hits:
                belongs = str(row.get("belongs") or "").strip()
                machine_name = str(row.get("name") or row.get("machine") or "").strip()
                dedupe = (belongs.upper(), machine_name)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                matched.append(
                    {
                        "belongs": belongs,
                        "machine": machine_name,
                        "status": str(row.get("status") or "").strip(),
                        "online": str(row.get("online") or "").strip(),
                        "is_test": bool(row.get("is_test")),
                    }
                )

    return matched, not_found


def resolve_prod_batch_token_hits(
    env_code: str,
    token: str,
    all_rows: list[dict],
) -> list[dict]:
    """All ``webmachine_data.json`` rows matching one user token within ``env_code``."""
    try:
        kind, key = _parse_target_line(token)
    except ValueError:
        return []
    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in all_rows:
        if not _prod_batch_row_matches_env(row, env_code):
            continue
        machine_name = str(row.get("name") or row.get("machine") or "").strip()
        if not machine_name:
            continue
        if not _row_text_matches(kind, key, machine_name):
            continue
        belongs = str(row.get("belongs") or "").strip()
        dedupe = (belongs.upper(), machine_name)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        hits.append(
            {
                "belongs": belongs,
                "machine": machine_name,
                "status": str(row.get("status") or "").strip(),
                "online": str(row.get("online") or "").strip(),
                "is_test": bool(row.get("is_test")),
            }
        )
    return hits


def start_prod_batch_job_direct(
    *,
    chat_id: str,
    action: str,
    machines: list[dict],
    send_message: Callable[..., Any],
    thread_root_message_id: str | None = None,
) -> None:
    """Run set/unset immediately — no Proceed/Cancel confirm card."""
    from prod_machine_batch import ACTION_LABELS

    if thread_root_message_id:
        try:
            import main as _main_mod  # noqa: WPS433

            _main_mod._set_prod_batch_thread_root(chat_id, thread_root_message_id)
        except Exception:
            pass

    label = ACTION_LABELS.get(action, action)
    send_message(
        chat_id,
        f"▶️ **{label}** on **{len(machines)}** machine(s) — executing now (no confirmation)…",
    )

    run_job_id = uuid.uuid4().hex
    with _PROD_BATCH_JOBS_LOCK:
        _PROD_BATCH_JOBS[run_job_id] = {
            "status": "running",
            "action": action,
            "machines": machines,
            "chat_id": chat_id,
            "thread_root_message_id": (thread_root_message_id or "").strip() or None,
            "cancel_requested": False,
            "cancel_summary_sent": False,
        }

    threading.Thread(
        target=_run_prod_batch_bot_job_thread,
        args=(run_job_id, chat_id, action, "", machines, send_message),
        daemon=True,
    ).start()


def _prod_batch_format_matched_line(m: dict) -> str:
    """One confirm-card bullet — machine name + status | online from webmachine_data.json."""
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


def _prod_batch_cleanup_pending() -> None:
    now = time.time()
    with _PROD_BATCH_PENDING_LOCK:
        expired = [
            tok
            for tok, ent in _PROD_BATCH_PENDING.items()
            if now - float(ent.get("created_at") or 0) > _PROD_BATCH_PENDING_TTL_SEC
        ]
        for tok in expired:
            _PROD_BATCH_PENDING.pop(tok, None)


def _prod_batch_confirm_card(
    *,
    token: str,
    action: str,
    env_code: str,
    matched: list[dict],
    not_found: list[str],
    data_src: str,
) -> dict:
    from prod_machine_batch import ACTION_LABELS, LARK_INTRO

    intro = LARK_INTRO.get(action, action)
    label = ACTION_LABELS.get(action, action)
    lines = [
        intro,
        "",
        "Found Machines -",
    ]
    for m in matched[:80]:
        lines.append(_prod_batch_format_matched_line(m))
    if len(matched) > 80:
        lines.append(f"... and {len(matched) - 80} more")
    if not_found:
        lines.append("")
        lines.append("Not Found Machines -")
        for nf in not_found[:40]:
            lines.append(f"• {nf}")
        if len(not_found) > 40:
            lines.append(f"... and {len(not_found) - 40} more")
    body_md = "\n".join(lines)
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"Confirm — {label}"[:80]},
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body_md[:4000]}},
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Proceed"},
                                    "type": "primary",
                                    "behaviors": [
                                        {
                                            "type": "callback",
                                            "value": {
                                                "k": PROD_BATCH_BOT_CARD_KEY,
                                                "t": token,
                                                "a": "proceed",
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "Cancel"},
                                    "type": "default",
                                    "behaviors": [
                                        {
                                            "type": "callback",
                                            "value": {
                                                "k": PROD_BATCH_BOT_CARD_KEY,
                                                "t": token,
                                                "a": "cancel",
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            ]
        },
    }


def _prod_batch_send_lark_card(
    chat_id: str,
    card: dict,
    send_message: Callable[..., Any],
) -> None:
    resp = send_message(chat_id, json.dumps(card, ensure_ascii=False), msg_type="interactive")
    if isinstance(resp, dict) and int(resp.get("code", 0)) != 0:
        print(f"[prod_batch] send card failed chat={chat_id!r}: {resp!r}", flush=True)


def _prod_batch_resolve_image_helpers() -> tuple[Callable[..., Any] | None, Callable[..., Any] | None]:
    try:
        import main as _main_mod  # noqa: WPS433

        up = getattr(_main_mod, "upload_image_lark", None)
        si = getattr(_main_mod, "prod_batch_send_image_message", None)
        if not callable(si):
            si = getattr(_main_mod, "send_image_message", None)
        if callable(up) and callable(si):
            return up, si
    except Exception:
        pass
    return None, None


def _prod_batch_cleanup_screenshot_paths(paths: list[str]) -> None:
    for pth in paths:
        if not pth:
            continue
        try:
            os.remove(pth)
        except OSError:
            pass


def _prod_batch_send_machine_screenshots_background(
    chat_id: str,
    machines: list[dict],
    summary: dict | None,
    send_message: Callable[..., Any],
) -> None:
    threading.Thread(
        target=_prod_batch_send_machine_screenshots,
        args=(chat_id, machines, summary, send_message),
        daemon=True,
        name="prod-batch-screenshots",
    ).start()


def send_machine_row_screenshots_for_chat(
    chat_id: str,
    machines: list[dict],
    send_message: Callable[..., Any],
) -> None:
    """Background EGM status-table row PNGs for machine dicts (``belongs`` + ``machine``)."""
    if not machines:
        return
    _prod_batch_send_machine_screenshots_background(chat_id, machines, None, send_message)


def _prod_batch_send_machine_screenshots(
    chat_id: str,
    machines: list[dict],
    summary: dict | None,
    send_message: Callable[..., Any],
) -> None:
    from prod_machine_batch import capture_prod_machine_screenshots, prod_batch_screenshots_enabled

    if not prod_batch_screenshots_enabled():
        return

    shots = list((summary or {}).get("screenshots") or [])
    shot_errors = list((summary or {}).get("screenshot_errors") or [])

    if not shots and machines:
        try:
            shots, extra_err = capture_prod_machine_screenshots(machines)
            shot_errors.extend(extra_err)
        except Exception as exc:
            logger.exception("prod-batch bot standalone screenshot capture failed")
            send_message(chat_id, f"⚠️ Machine screenshots unavailable: {exc}")
            return

    if not shots:
        if shot_errors:
            send_message(
                chat_id,
                f"⚠️ Could not capture machine screenshots ({len(shot_errors)} failed).",
            )
        return

    upload_fn, send_img_fn = _prod_batch_resolve_image_helpers()
    paths_to_clean: list[str] = []
    if not upload_fn or not send_img_fn:
        for item in shots:
            pth = str(item.get("path") or "")
            if pth:
                paths_to_clean.append(pth)
        _prod_batch_cleanup_screenshot_paths(paths_to_clean)
        send_message(
            chat_id,
            "⚠️ Machine screenshots were captured but Lark image upload is unavailable on this host.",
        )
        return

    sent = 0
    for item in shots:
        pth = str(item.get("path") or "")
        if not pth:
            continue
        paths_to_clean.append(pth)
        key = upload_fn(pth) or ""
        if not key:
            continue
        belongs = str(item.get("belongs") or "").strip()
        machine = str(item.get("machine") or "").strip()
        label = f"{belongs} — {machine}".strip(" —")
        send_message(chat_id, f"📸 **{label}**")
        resp = send_img_fn(chat_id, key)
        if isinstance(resp, dict) and resp.get("code") == 0:
            sent += 1

    _prod_batch_cleanup_screenshot_paths(paths_to_clean)

    if sent < len(shots):
        send_message(chat_id, f"⚠️ Sent {sent}/{len(shots)} machine screenshot(s).")


def _prod_batch_send_lark_md(
    chat_id: str,
    title: str,
    body_md: str,
    send_message: Callable[..., Any],
    *,
    header_template: str | None = None,
    job_id: str | None = None,
) -> None:
    header: dict[str, Any] = {"title": {"tag": "plain_text", "content": title[:80]}}
    if header_template:
        header["template"] = header_template
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": body_md[:4000]}},
    ]
    if job_id and _prod_batch_job_is_running(job_id):
        elements.append(
            {
                "tag": "action",
                "actions": [_prod_batch_cancel_button(job_id)],
            }
        )
    card = {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": header,
        "body": {"elements": elements},
    }
    _prod_batch_send_lark_card(chat_id, card, send_message)


def _run_prod_batch_bot_job_thread(
    job_id: str,
    chat_id: str,
    action: str,
    remark: str,
    machines: list[dict],
    send_message: Callable[..., Any],
) -> None:
    from prod_machine_batch import ACTION_LABELS, run_prod_batch_job

    with _PROD_BATCH_JOBS_LOCK:
        job = _PROD_BATCH_JOBS.get(job_id) or {}
        if job_id not in _PROD_BATCH_JOBS:
            _PROD_BATCH_JOBS[job_id] = {
                "status": "running",
                "action": action,
                "machines": machines,
                "chat_id": chat_id,
                "cancel_requested": False,
                "cancel_summary_sent": False,
            }
        else:
            _PROD_BATCH_JOBS[job_id]["status"] = "running"
        thread_root = (job.get("thread_root_message_id") or "").strip() or None
    _prod_batch_sm_refresh_thread_root(chat_id, thread_root)

    def cancel_check() -> bool:
        with _PROD_BATCH_JOBS_LOCK:
            return bool(_PROD_BATCH_JOBS.get(job_id, {}).get("cancel_requested"))

    def manual_stop_check() -> bool:
        with _PROD_BATCH_JOBS_LOCK:
            return bool(_PROD_BATCH_JOBS.get(job_id, {}).get("manual_stop"))

    def on_manual(summary: dict) -> None:
        fail_n = len(summary.get("failed") or [])
        _prod_batch_send_lark_md(
            chat_id,
            f"Manual needed — {ACTION_LABELS.get(action, action)}",
            (
                f"Some machines failed ({fail_n}) — may have players inside.\n"
                "Finish manually on EGM, then check live status.\n\n"
                "Tap **Cancel** below to stop retries."
            ),
            send_message,
            header_template="red",
            job_id=job_id,
        )

    def on_env_start(env: str, count: int) -> None:
        send_message(
            chat_id,
            f"⏳ **{env}** — {ACTION_LABELS.get(action, action)} started on **{count}** machine(s)...",
        )

    def on_phase_continue(
        prev_step: str, next_step: str, dropped: list, continuing: list
    ) -> None:
        from prod_machine_batch import PHASE_LABELS

        prev_label = PHASE_LABELS.get(prev_step, prev_step)
        next_label = PHASE_LABELS.get(next_step, next_step)
        lines = [
            f"▶️ **{next_label}** — continuing with **{len(continuing)}** machine(s) that passed {prev_label}.",
            "",
            f"⏭️ **Skipped ({len(dropped)})** — {prev_label} failed after all retries:",
        ]
        for m in dropped[:20]:
            lines.append(f"• {m.get('belongs', '')} — {m.get('machine') or m.get('name') or ''}")
        if len(dropped) > 20:
            lines.append(f"... and {len(dropped) - 20} more")
        send_message(chat_id, "\n".join(lines))

    def on_phase_retry(
        step_verify: str, attempt: int, failed: list, done: list | None = None
    ) -> None:
        from prod_machine_batch import (
            PHASE_LABELS,
            _failure_is_game_running,
            _max_phase_retries,
        )

        max_r = _max_phase_retries()
        done = done or []
        is_final = attempt >= max_r
        game_running = bool(failed) and all(
            _failure_is_game_running(str(m.get("error") or ""), m.get("live"))
            for m in failed
        )

        done_lines: list[str] = []
        if done:
            done_lines.append(f"✅ **Done ({len(done)}):**")
            for m in done[:20]:
                done_lines.append(
                    f"• {m.get('belongs', '')} — {m.get('machine') or m.get('name') or ''}"
                )
            if len(done) > 20:
                done_lines.append(f"... and {len(done) - 20} more")
            done_lines.append("")

        fail_head = (
            f"🔄 **Still retrying ({len(failed)}):**"
            if not is_final
            else f"❌ **Still failed ({len(failed)}):**"
        )

        if step_verify == "set_maint" and game_running:
            head = (
                "⚠️ **Game currently running** error occurred — will retry **set maintenance** again.\n"
                f"Attempt **{attempt}** / **{max_r}**.\n\n"
                if not is_final
                else "⚠️ **Game currently running** — attempt "
                f"**{attempt}** / **{max_r}** was the last, **no more retries** for these machine(s).\n\n"
            )
            body_lines = list(done_lines)
            body_lines.append(fail_head)
            for m in failed[:20]:
                body_lines.append(
                    f"• {m.get('belongs', '')} — {m.get('machine') or m.get('name') or ''}"
                )
            if len(failed) > 20:
                body_lines.append(f"... and {len(failed) - 20} more")
            send_message(chat_id, head + "\n".join(body_lines))
            return

        label = PHASE_LABELS.get(step_verify, step_verify)
        lines = [
            f"**{label} — {len(failed)} machine(s) still failing**",
            (
                f"Will retry automatically (attempt {attempt}/{max_r}) unless you tap **Cancel** below."
                if not is_final
                else f"Attempt {attempt}/{max_r} was the last — **no more retries** for these machine(s)."
            ),
            "",
        ]
        lines.extend(done_lines)
        lines.append(fail_head)
        for m in failed[:30]:
            nm = m.get("machine") or m.get("name") or ""
            err = (m.get("error") or "").strip()
            suffix = f" — {err}" if err else ""
            lines.append(f"• {m.get('belongs', '')} — {nm}{suffix}")
        if len(failed) > 30:
            lines.append(f"... and {len(failed) - 30} more")
        _prod_batch_send_lark_md(
            chat_id,
            f"{label} — retry {attempt}" if not is_final else f"{label} — final attempt failed",
            "\n".join(lines),
            send_message,
            header_template="red",
            job_id=job_id,
        )

    cancelled = False
    try:
        summary = run_prod_batch_job(
            action,
            machines,
            remark=remark,
            cancel_check=cancel_check,
            manual_stop_check=manual_stop_check,
            on_manual_stop=on_manual,
            on_phase_retry=on_phase_retry,
            on_phase_continue=on_phase_continue,
            on_env_start=on_env_start,
        )
        with _PROD_BATCH_JOBS_LOCK:
            cancelled = bool(_PROD_BATCH_JOBS.get(job_id, {}).get("cancel_requested"))
            if cancelled:
                _PROD_BATCH_JOBS[job_id]["status"] = "cancelled"
            else:
                _PROD_BATCH_JOBS[job_id]["status"] = "done"
                _PROD_BATCH_JOBS[job_id]["summary"] = summary

        if cancelled:
            _prod_batch_send_cancel_live_summary(job_id, send_message)
            return

        ok_n = len(summary.get("success") or [])
        fail_n = len(summary.get("failed") or [])
        failed = list(summary.get("failed") or [])
        from prod_machine_batch import _failure_is_game_running, _max_phase_retries

        max_r = _max_phase_retries()
        all_game_running = bool(failed) and all(
            _failure_is_game_running(str(m.get("error") or ""), m.get("live")) for m in failed
        )

        if fail_n and action == "set_maint" and all_game_running:
            lines = [
                f"❌ **Set maintenance** failed after **{max_r}** attempts.",
                f"All **{max_r}** attempts were **game currently running**.",
                "",
            ]
            if ok_n:
                lines.append(f"✅ **Done ({ok_n}):**")
                for m in (summary.get("success") or [])[:30]:
                    lines.append(f"✓ {m.get('belongs')} — {m.get('machine')}")
                if ok_n > 30:
                    lines.append(f"... and {ok_n - 30} more done")
                lines.append("")
                lines.append(f"❌ **Still failed ({fail_n}):**")
            for m in failed[:30]:
                lines.append(f"• {m.get('belongs')} — {m.get('machine')}")
            if fail_n > 30:
                lines.append(f"... and {fail_n - 30} more")
            _prod_batch_send_lark_md(
                chat_id,
                "Failed — game currently running",
                "\n".join(lines),
                send_message,
                header_template="red",
            )
            _prod_batch_send_machine_screenshots_background(chat_id, machines, summary, send_message)
            return

        lines = [
            f"**SUMMARY — {ACTION_LABELS.get(action, action)}**",
            f"Success: {ok_n}",
            f"Failed: {fail_n}",
            "",
        ]
        for m in (summary.get("success") or [])[:30]:
            lines.append(f"✓ {m.get('belongs')} — {m.get('machine')}")
        if fail_n:
            lines.append("")
            lines.append("**Still failed:**")
        for m in (summary.get("failed") or [])[:30]:
            err = (m.get("error") or "").strip()
            suffix = f" ({err})" if err else ""
            lines.append(f"✗ {m.get('belongs')} — {m.get('machine')}{suffix}")
        if fail_n > 30:
            lines.append(f"... and {fail_n - 30} more failed")
        tpl = "red" if fail_n else "green"
        title = (
            f"Failed — {ACTION_LABELS.get(action, action)}"
            if fail_n
            else f"Success — {ACTION_LABELS.get(action, action)}"
        )
        _prod_batch_send_lark_md(chat_id, title, "\n".join(lines), send_message, header_template=tpl)
        _prod_batch_send_machine_screenshots_background(chat_id, machines, summary, send_message)
    except Exception as exc:
        logger.exception("prod-batch bot job %s failed", job_id)
        with _PROD_BATCH_JOBS_LOCK:
            if job_id in _PROD_BATCH_JOBS:
                _PROD_BATCH_JOBS[job_id]["status"] = "done"
        _prod_batch_send_lark_md(
            chat_id,
            f"Failed — {ACTION_LABELS.get(action, action)}",
            f"**Job error**\n\n{str(exc)[:3500]}",
            send_message,
            header_template="red",
        )


def _prod_batch_bot_prepare_confirm(
    parsed: dict[str, Any],
    target_lines: list[str],
    *,
    chat_id: str,
    send_message: Callable[..., Any],
    thread_root_message_id: str | None = None,
) -> None:
    _prod_batch_cleanup_pending()
    env_code = parsed["env_code"]
    site = _PROD_BATCH_ENV_TO_SITE.get(env_code) or parsed.get("site") or ""
    matched, not_found, data_src = _prod_batch_resolve_confirm_targets(
        site, env_code, target_lines
    )
    if "stuck" in data_src.lower() or "stalled" in data_src.lower():
        send_message(chat_id, f"❌ {data_src}")
        return
    if not matched:
        nf = ", ".join(not_found[:20]) if not_found else "(none parsed)"
        send_message(chat_id, f"❌ No machines matched for **{env_code}**. Not found: {nf}")
        return

    token = uuid.uuid4().hex[:16]
    with _PROD_BATCH_PENDING_LOCK:
        _PROD_BATCH_PENDING[token] = {
            "action": parsed["action"],
            "env_code": env_code,
            "machines": matched,
            "not_found": not_found,
            "chat_id": chat_id,
            "thread_root_message_id": (thread_root_message_id or "").strip() or None,
            "created_at": time.time(),
        }

    card = _prod_batch_confirm_card(
        token=token,
        action=parsed["action"],
        env_code=env_code,
        matched=matched,
        not_found=not_found,
        data_src=data_src,
    )
    _prod_batch_send_lark_card(chat_id, card, send_message)


def handle_prod_batch_bot_command(
    original_text: str,
    mention_keys: Sequence[str],
    *,
    chat_id: str,
    send_message: Callable[..., Any],
    thread_root_message_id: str | None = None,
) -> tuple[bool, str | None]:
    """
    Parse bot message, resolve machines from ``webmachine_data.json``, send confirm card.
    Returns ``(handled, optional_error_text)``.
    """
    _prod_batch_cleanup_pending()
    body = _prod_batch_strip_mention_text(original_text, mention_keys)
    parsed = parse_prod_batch_bot_command(body)
    if not parsed:
        return False, None

    m = parsed["match"]
    first_line = body.splitlines()[0] if body.splitlines() else body
    rest_first = first_line[m.end() :].strip()

    target_lines: list[str] = []
    if rest_first:
        target_lines.append(rest_first)
    for ln in body.splitlines()[1:]:
        ln = ln.strip()
        if ln:
            target_lines.append(ln)

    if not target_lines:
        from prod_machine_batch import ACTION_LABELS

        site = parsed["site"]
        label = ACTION_LABELS.get(parsed["action"], parsed["action"])
        usage = (
            f"❌ Usage: `/{site}{'set' if 'set_' in parsed['action'] else 'unset'}…` "
            f"then machine name(s) on the next lines.\n\n"
            f"Example:\n"
            f"@bot /{site}setmaintenancetest\n"
            f"NCH1422\n"
            f"1423\n\n"
            f"Action: {label}"
        )
        return True, usage

    env_code = parsed["env_code"]
    kwargs = {
        "chat_id": chat_id,
        "send_message": send_message,
        "thread_root_message_id": thread_root_message_id,
    }
    use_live = (os.environ.get("PROD_BATCH_LIVE_LOOKUP") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if use_live:
        threading.Thread(
            target=_prod_batch_bot_prepare_confirm,
            args=(parsed, target_lines),
            kwargs=kwargs,
            daemon=True,
        ).start()
    else:
        _prod_batch_bot_prepare_confirm(parsed, target_lines, **kwargs)
    return True, None


def handle_prod_batch_card_callback(
    parsed: dict[str, Any],
    *,
    chat_id: str,
    send_message: Callable[..., Any],
    action_obj: dict | None = None,
) -> bool:
    key = str(parsed.get("k") or "").strip().lower()
    if key != PROD_BATCH_BOT_CARD_KEY:
        return False

    job_id = str(parsed.get("j") or "").strip()
    action_btn = str(parsed.get("a") or "").strip().lower()

    if job_id and action_btn == "job_cancel":
        _prod_batch_request_job_cancel(job_id, chat_id, send_message)
        return True

    if action_btn == "sm_env":
        return _prod_batch_sm_on_env_picked(parsed, chat_id, send_message)
    if action_btn == "sm_action":
        return _prod_batch_sm_on_action_submit(parsed, chat_id, send_message, action_obj)

    token = str(parsed.get("t") or "").strip()
    if not token:
        send_message(chat_id, "⏭️ This confirmation expired or was already handled. Send the command again.")
        return True

    _prod_batch_cleanup_pending()

    with _PROD_BATCH_PENDING_LOCK:
        pending = _PROD_BATCH_PENDING.pop(token, None)

    if not pending:
        send_message(chat_id, "⏭️ This confirmation expired or was already handled. Send the command again.")
        return True

    if action_btn == "cancel":
        send_message(chat_id, "Cancelled")
        return True

    if action_btn != "proceed":
        send_message(chat_id, "❌ Unknown action on confirmation card.")
        return True

    machines = pending.get("machines") or []
    action = str(pending.get("action") or "").strip()
    if not machines or not action:
        send_message(chat_id, "❌ Confirmation data missing. Send the command again.")
        return True

    thread_root = (pending.get("thread_root_message_id") or "").strip() or None
    if thread_root:
        try:
            import main as _main_mod  # noqa: WPS433

            _main_mod._set_prod_batch_thread_root(chat_id, thread_root)
        except Exception:
            pass

    run_job_id = uuid.uuid4().hex
    with _PROD_BATCH_JOBS_LOCK:
        _PROD_BATCH_JOBS[run_job_id] = {
            "status": "running",
            "action": action,
            "machines": machines,
            "chat_id": chat_id,
            "thread_root_message_id": (pending.get("thread_root_message_id") or "").strip() or None,
            "cancel_requested": False,
            "cancel_summary_sent": False,
        }

    send_message(chat_id, "Proceeding... Please wait...")

    threading.Thread(
        target=_run_prod_batch_bot_job_thread,
        args=(run_job_id, chat_id, action, "", machines, send_message),
        daemon=True,
    ).start()
    return True


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted (Ctrl+C); browser cleanup runs before exit.", file=sys.stderr)
        raise SystemExit(130)
