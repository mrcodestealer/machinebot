"""
Playwright automation for PROD machine batch maintenance/test on EGM status pages.

Recheck / verification always reads the **live** EGM table in the headless browser
(``smmachine`` row parsers). It never uses webapp JSON or ``webmachine_data.json``.
"""
from __future__ import annotations

import logging
import os
import queue as _queue
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

from smmachine import (
    _can_pagination_next,
    _click_pagination_next,
    _ensure_row_checkbox_checked,
    _ensure_row_checkbox_unchecked,
    _find_row_for_target,
    _go_first_page,
    _parse_target_line,
    _resolve_collect_page_limit,
    _row_match_text_for_target,
    _row_report_fields,
    _row_text_matches,
    _site_synthetic_machine,
    _smachine_resolve_headless,
    _table_body_rows,
    _wait_table_idle,
)

logger = logging.getLogger(__name__)

ACTION_BUTTONS = {
    "set_maint": ["BatchMaintenance"],
    "set_test": ["BatchTest"],
    "set_both": ["BatchMaintenance", "BatchTest"],
    "unset_maint": ["BatchStart Using"],
    "unset_test": ["BatchTestCancel"],
    "unset_both": ["BatchStart Using", "BatchTestCancel"],
}

# Two-step actions: finish phase 1 on live EGM (retry until pass or max), then phase 2, then summary.
PHASED_STEPS: dict[str, list[tuple[str, list[str]]]] = {
    "set_both": [
        ("set_maint", ["BatchMaintenance"]),
        ("set_test", ["BatchTest"]),
    ],
    "unset_both": [
        ("unset_maint", ["BatchStart Using"]),
        ("unset_test", ["BatchTestCancel"]),
    ],
}

PHASE_LABELS = {
    "set_maint": "Set maintenance",
    "set_test": "Set test",
    "unset_maint": "Unset maintenance",
    "unset_test": "Unset test",
}

# Single-step actions: same retry + live EGM recheck as each phase in set_both (e.g. player in game).
AUTO_RETRY_ACTIONS = frozenset(
    {"set_maint", "set_test", "unset_maint", "unset_test"} | set(PHASED_STEPS.keys())
)

ACTION_LABELS = {
    "set_maint": "Set maintenance",
    "set_test": "Set test",
    "set_both": "Set maintenance and Set test",
    "unset_maint": "Unset maintenance",
    "unset_test": "Unset test",
    "unset_both": "Unset maintenance and unset test",
}

LARK_INTRO = {
    "set_maint": "Will set maintenance to machines below:",
    "set_test": "Will set test to machines below:",
    "set_both": "Will set maintenance and test to machines below:",
    "unset_maint": "Will unset maintenance on machines below:",
    "unset_test": "Will unset test on machines below:",
    "unset_both": "Will unset maintenance and test on machines below:",
}

# SET PROD MACHINE only uses these four toolbar buttons (ignore BatchKick Out, Sync DB Config, …).
EGM_PROD_BATCH_BUTTONS: tuple[str, ...] = (
    "BatchMaintenance",
    "BatchTest",
    "BatchStart Using",
    "BatchTestCancel",
)

# Dry-run probe uses the same set as :data:`ACTION_BUTTONS`.
EGM_TOOLBAR_BATCH_BUTTONS: tuple[str, ...] = EGM_PROD_BATCH_BUTTONS

_SITE_ALIAS_BELONGS: dict[str, str] = {
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

_BELONGS_SITE = {
    "NP": "nwr",
    "NWR": "nwr",
    "NCH": "nch",
    "NC": "nch",
    "TBR": "tbr",
    "TBP": "tbp",
    "MDR": "mdr",
    "DHS": "dhs",
    "CP": "cp",
    "OSM": "osm",
    "WF": "wf",
    "WINFORD": "wf",
}


def _belongs_for_machine(belongs: str) -> str:
    b = (belongs or "").strip().upper()
    if b in ("NWR", "NP"):
        return "NP"
    return b


def _belongs_site_key(belongs: str) -> str:
    b = _belongs_for_machine(belongs)
    return _BELONGS_SITE.get(b, b.lower())


def _machine_display_name(machine: dict) -> str:
    return (machine.get("name") or machine.get("machine") or "").strip()


def _default_timeout_ms() -> int:
    return int(os.environ.get("PROD_SET_TIMEOUT_MS", "600000"))


def prod_batch_screenshots_enabled() -> bool:
    """After set/unset maintenance/test, capture EGM status-row PNGs (default on)."""
    return (os.environ.get("PROD_BATCH_SCREENSHOTS", "1").strip().lower() not in ("0", "false", "no", "off"))


def _screenshot_egm_row_locator(page, row, *, timeout_ms: int) -> str:
    """PNG of one EGM table row only — no View / cog / operation dialog."""
    try:
        row.scroll_into_view_if_needed(timeout=min(15_000, timeout_ms))
    except Exception:
        pass
    _page_pause(page, 200)
    fd, out_path = tempfile.mkstemp(suffix=".png", prefix="prod_batch_egm_row_")
    os.close(fd)
    row.screenshot(path=out_path, animations="disabled", scale="css")
    return out_path


def capture_machine_row_screenshot_on_page(
    page,
    machine_name: str,
    *,
    timeout_ms: int,
    max_pages: int,
) -> str:
    """Locate one machine row on the live EGM table and screenshot that row only."""
    row = _find_machine_row_live(page, machine_name, timeout_ms=timeout_ms, max_pages=max_pages)
    if row is None:
        raise RuntimeError(f"machine not found on EGM page: {machine_name!r}")
    return _screenshot_egm_row_locator(page, row, timeout_ms=timeout_ms)


def capture_machine_operation_screenshot_on_page(
    page,
    machine_name: str,
    *,
    timeout_ms: int,
    max_pages: int,
) -> str:
    """Alias — prod batch uses table-row capture (no operation dialog)."""
    return capture_machine_row_screenshot_on_page(
        page, machine_name, timeout_ms=timeout_ms, max_pages=max_pages
    )


def _capture_prod_batch_screenshots_on_page(
    page,
    machines: list[dict],
    *,
    timeout_ms: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    shots: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    by_env: dict[str, list[dict]] = {}
    for m in machines:
        b = _belongs_for_machine(m.get("belongs", ""))
        by_env.setdefault(b, []).append(m)

    for belongs, env_machines in by_env.items():
        ok_login, login_err = _ensure_env_egm_page(
            page, belongs, timeout_ms=timeout_ms, max_pages=max_pages
        )
        if not ok_login:
            for m in env_machines:
                name = _machine_display_name(m)
                errors.append(
                    {
                        "belongs": m.get("belongs", belongs),
                        "machine": name,
                        "error": login_err or "login failed",
                    }
                )
            continue

        specs = _machine_lookup_specs(env_machines)
        pending = list(specs)
        limit = _resolve_collect_page_limit(max_pages)
        _go_first_page(page, timeout_ms=timeout_ms, max_steps=limit)
        _wait_table_idle(page, timeout_ms)
        steps = 0
        safety = 0
        shot_names: set[tuple[str, str]] = set()

        while pending:
            safety += 1
            if safety > limit * max(len(specs), 1) + 50:
                break

            resolved: list[tuple[str, dict, str, str]] = []
            for spec in list(pending):
                name, m, kind, key = spec
                dedupe = (str(m.get("belongs") or belongs).upper(), name)
                if dedupe in shot_names:
                    if kind == "full":
                        resolved.append(spec)
                    continue
                rows = _find_all_rows_for_target(page, kind, key, timeout_ms=timeout_ms)
                if not rows:
                    continue
                try:
                    path = _screenshot_egm_row_locator(page, rows[0], timeout_ms=timeout_ms)
                    shots.append({"belongs": m.get("belongs", belongs), "machine": name, "path": path})
                    shot_names.add(dedupe)
                except Exception as exc:
                    logger.warning("prod-batch row screenshot failed for %s: %s", name, exc)
                    errors.append(
                        {
                            "belongs": m.get("belongs", belongs),
                            "machine": name,
                            "error": str(exc),
                        }
                    )
                if kind == "full":
                    resolved.append(spec)

            for spec in resolved:
                pending.remove(spec)

            if not pending:
                break
            if not _can_pagination_next(page) or steps >= limit:
                break
            _click_pagination_next(page, timeout_ms=timeout_ms)
            steps += 1
            _wait_table_idle(page, timeout_ms)

        for name, m, _kind, _key in pending:
            dedupe = (str(m.get("belongs") or belongs).upper(), name)
            if dedupe in shot_names:
                continue
            errors.append(
                {
                    "belongs": m.get("belongs", belongs),
                    "machine": name,
                    "error": "machine not found on EGM page",
                }
            )
    return shots, errors


def capture_prod_machine_screenshots(
    machines: list[dict],
    *,
    headless: bool | None = None,
    timeout_ms: int | None = None,
    max_pages: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Dedicated browser session — EGM status-table row PNG for each machine."""
    from playwright.sync_api import sync_playwright

    if not machines:
        return [], []

    timeout_ms = timeout_ms or _default_timeout_ms()
    if max_pages is None:
        max_pages = int(os.environ.get("SM_MACHINE_MAX_PAGES") or 0) or None
    hl = _smachine_resolve_headless(headless)
    if headless is None:
        hl = _smachine_resolve_headless(
            os.environ.get("SMACHINE_HEADLESS", "1").strip().lower() not in ("0", "false", "no")
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=hl)
        context = browser.new_context(
            viewport={"width": 1600, "height": 900},
            ignore_https_errors=True,
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            return _capture_prod_batch_screenshots_on_page(
                page, machines, timeout_ms=timeout_ms, max_pages=max_pages
            )
        finally:
            try:
                context.close()
            except Exception:
                pass
            browser.close()


def _max_phase_retries() -> int:
    try:
        return max(1, int((os.environ.get("PROD_SET_MAX_PHASE_RETRIES") or "10").strip()))
    except ValueError:
        return 10


def _prod_batch_fast_mode() -> bool:
    """Shorter UI settles and batch pagination (default on). Set ``PROD_BATCH_FAST=0`` to disable."""
    return (os.environ.get("PROD_BATCH_FAST", "1").strip().lower() not in ("0", "false", "no", "off"))


def _fast_ms(ms: int) -> int:
    if not _prod_batch_fast_mode():
        return ms
    return max(40, int(ms * 0.35))


def _page_pause(page, ms: int) -> None:
    page.wait_for_timeout(_fast_ms(ms))


def _list_path() -> str:
    path = (os.environ.get("SM_MACHINE_PATH") or "/egm/egmStatusList").strip() or "/egm/egmStatusList"
    if not path.startswith("/"):
        path = "/" + path
    return path


def _machine_target(machine_name: str) -> tuple[str, str]:
    return _parse_target_line(machine_name)


def _egm_table_ready(page, timeout_ms: int) -> bool:
    try:
        page.wait_for_selector(".app-container .el-table, .filter-container .el-table", timeout=min(20_000, timeout_ms))
        _wait_table_idle(page, timeout_ms)
        return True
    except Exception:
        return False


def _login_egm_backend(page, base: str, user: str, pw: str, *, timeout_ms: int) -> None:
    path = _list_path()
    login = "/login"
    login_url = f"{base.rstrip('/')}{login}?redirect={quote(path, safe='')}"
    list_url = f"{base.rstrip('/')}{path}"

    page.goto(login_url, wait_until="domcontentloaded")
    _page_pause(page, 900)

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

    # Wait for the login POST to actually establish the session (URL leaves /login) before we try
    # to load the list — navigating too early lands back on the login page and looks like a
    # "table did not load" failure. Poll up to ~12s instead of a fixed short pause.
    deadline = time.monotonic() + min(12.0, max(4.0, timeout_ms / 1000.0))
    while time.monotonic() < deadline:
        cur = page.url or ""
        if login not in cur or path in cur:
            break
        page.wait_for_timeout(300)
    page.wait_for_timeout(_fast_ms(900))
    if path not in (page.url or ""):
        page.goto(list_url, wait_until="domcontentloaded")
    if not _egm_table_ready(page, timeout_ms):
        # One more reload — the backend sometimes serves a blank table on the first navigation.
        page.goto(list_url, wait_until="domcontentloaded")
        if not _egm_table_ready(page, timeout_ms):
            raise RuntimeError("EGM status table did not load after login")


def _login_retries() -> int:
    """How many times to retry a flaky EGM login/table-load before giving up (default 3)."""
    try:
        return max(1, int((os.environ.get("PROD_SET_LOGIN_RETRIES") or "3").strip()))
    except ValueError:
        return 3


def _ensure_env_egm_page(page, belongs: str, *, timeout_ms: int, max_pages: int) -> tuple[bool, str]:
    """
    Open the correct PROD backend EGM list once per environment (reuse same page).

    Returns ``(ok, error)``. A transient login / table-load failure is retried a few times since
    the EGM backend occasionally drops the first attempt (this used to fail a whole batch as
    "login failed"). ``error`` carries the real reason for the failure summary.
    """
    from checkcredit import _np_resolve_backend  # noqa: WPS433

    norm = _belongs_for_machine(belongs)
    if getattr(page, "_prod_set_belongs", None) == norm and _egm_table_ready(page, timeout_ms):
        return True, ""

    site = _belongs_site_key(norm)
    try:
        synth = _site_synthetic_machine(site)
    except (SystemExit, ValueError) as e:
        logger.warning("prod-set: unknown site for belongs %r: %s", belongs, e)
        return False, f"unknown site for {belongs!r}: {e}"

    base, user, pw = _np_resolve_backend(synth)
    if not (base and user and pw):
        logger.warning("prod-set: missing credentials for belongs %r (site %r)", belongs, site)
        return False, f"missing backend credentials for {belongs!r} (site {site!r})"

    attempts = _login_retries()
    last_err = ""
    for attempt in range(1, attempts + 1):
        try:
            _login_egm_backend(page, base, user, pw, timeout_ms=timeout_ms)
            limit = _resolve_collect_page_limit(max_pages)
            _go_first_page(page, timeout_ms=timeout_ms, max_steps=limit)
            _wait_table_idle(page, timeout_ms)
            page._prod_set_belongs = norm  # type: ignore[attr-defined]
            if attempt > 1:
                logger.info("prod-set: login for %r recovered on attempt %d", belongs, attempt)
            return True, ""
        except Exception as e:  # noqa: BLE001
            last_err = str(e) or e.__class__.__name__
            logger.warning(
                "prod-set: login/navigation failed for %r (attempt %d/%d): %s",
                belongs, attempt, attempts, last_err,
            )
            try:
                page._prod_set_belongs = None  # type: ignore[attr-defined]
            except Exception:
                pass
            if attempt < attempts:
                try:
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
    return False, f"login/table load failed after {attempts} attempt(s): {last_err}"


def _find_all_rows_for_target(page, kind: str, key: str, *, timeout_ms: int):
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


def _machine_lookup_specs(machines: list[dict]) -> list[tuple[str, dict, str, str]]:
    specs: list[tuple[str, dict, str, str]] = []
    for m in machines:
        name = _machine_display_name(m)
        if not name:
            continue
        kind, key = _machine_target(name)
        specs.append((name, m, kind, key))
    return specs


def _batch_select_machines_on_live_page(
    page,
    machines: list[dict],
    *,
    timeout_ms: int,
    max_pages: int,
    read_states: bool = False,
) -> tuple[list[dict], list[dict], dict[str, dict[str, Any] | None]]:
    """
    Tick the checkbox for each target machine, paginating only as needed.

    When ``read_states`` is set, each found row's live state (status / test) is read in the **same
    pagination pass** so callers can skip a second full table scan. Returns
    ``(selected, fail_list, states)`` where ``states`` is empty unless ``read_states`` is set.
    """
    specs = _machine_lookup_specs(machines)
    if not specs:
        return [], [], {}

    pending = list(specs)
    limit = _resolve_collect_page_limit(max_pages)
    _go_first_page(page, timeout_ms=timeout_ms, max_steps=limit)
    _wait_table_idle(page, timeout_ms)

    selected: list[dict] = []
    fail_list: list[dict] = []
    states: dict[str, dict[str, Any] | None] = {}
    seen: set[tuple[str, str]] = set()
    steps = 0
    safety = 0

    while pending:
        safety += 1
        if safety > limit * max(len(specs), 1) + 50:
            break

        resolved: list[tuple[str, dict, str, str]] = []
        for spec in list(pending):
            name, m, kind, key = spec
            rows = _find_all_rows_for_target(page, kind, key, timeout_ms=timeout_ms)
            if not rows:
                continue
            dedupe = (str(m.get("belongs") or "").upper(), name)
            if dedupe not in seen:
                try:
                    if read_states:
                        try:
                            mn, is_test, _gt, status, online = _row_report_fields(
                                rows[0], timeout_ms=timeout_ms
                            )
                            states[name] = {
                                "name": mn,
                                "test": bool(is_test),
                                "status": (status or "").strip(),
                                "online": (online or "").strip(),
                            }
                        except Exception:
                            states[name] = None
                    _ensure_row_checkbox_checked(page, rows[0], timeout_ms=timeout_ms)
                    seen.add(dedupe)
                    selected.append(m)
                except Exception as exc:
                    fail_list.append(
                        {"belongs": m.get("belongs", ""), "machine": name, "error": str(exc)}
                    )
            if kind == "full":
                resolved.append(spec)

        for spec in resolved:
            pending.remove(spec)

        if not pending:
            break
        if not _can_pagination_next(page) or steps >= limit:
            break
        _click_pagination_next(page, timeout_ms=timeout_ms)
        steps += 1
        _wait_table_idle(page, timeout_ms)

    for name, m, _kind, _key in pending:
        if any(_machine_display_name(x) == name for x in selected):
            continue
        fail_list.append(
            {
                "belongs": m.get("belongs", ""),
                "machine": name,
                "error": "machine not found on EGM page",
            }
        )
    return selected, fail_list, states


def _batch_read_live_states(
    page,
    machines: list[dict],
    *,
    timeout_ms: int,
    max_pages: int,
) -> dict[str, dict[str, Any] | None]:
    specs = _machine_lookup_specs(machines)
    if not specs:
        return {}

    pending = list(specs)
    limit = _resolve_collect_page_limit(max_pages)
    _go_first_page(page, timeout_ms=timeout_ms, max_steps=limit)
    _wait_table_idle(page, timeout_ms)

    out: dict[str, dict[str, Any] | None] = {name: None for name, _m, _k, _key in specs}
    steps = 0
    safety = 0

    while pending:
        safety += 1
        if safety > limit * max(len(specs), 1) + 50:
            break

        resolved: list[tuple[str, dict, str, str]] = []
        for spec in list(pending):
            name, _m, kind, key = spec
            rows = _find_all_rows_for_target(page, kind, key, timeout_ms=timeout_ms)
            if not rows:
                continue
            try:
                mn, is_test, _gt, status, online = _row_report_fields(rows[0], timeout_ms=timeout_ms)
                out[name] = {
                    "name": mn,
                    "test": bool(is_test),
                    "status": (status or "").strip(),
                    "online": (online or "").strip(),
                }
            except Exception:
                out[name] = None
            if kind == "full":
                resolved.append(spec)

        for spec in resolved:
            pending.remove(spec)

        if not pending:
            break
        if not _can_pagination_next(page) or steps >= limit:
            break
        _click_pagination_next(page, timeout_ms=timeout_ms)
        steps += 1
        _wait_table_idle(page, timeout_ms)

    return out


def _find_machine_row_live(page, machine_name: str, *, timeout_ms: int, max_pages: int):
    """Locate a machine row by scanning the **current** EGM table (paginate if needed)."""
    kind, key = _machine_target(machine_name)
    limit = _resolve_collect_page_limit(max_pages)
    _go_first_page(page, timeout_ms=timeout_ms, max_steps=limit)
    _wait_table_idle(page, timeout_ms)

    steps = 0
    while True:
        rows = _find_all_rows_for_target(page, kind, key, timeout_ms=timeout_ms)
        if rows:
            return rows[0]
        if not _can_pagination_next(page) or steps >= limit:
            return None
        _click_pagination_next(page, timeout_ms=timeout_ms)
        steps += 1
        _wait_table_idle(page, timeout_ms)


def _select_machine_on_live_page(page, machine_name: str, *, timeout_ms: int, max_pages: int) -> bool:
    row = _find_machine_row_live(page, machine_name, timeout_ms=timeout_ms, max_pages=max_pages)
    if row is None:
        return False
    _ensure_row_checkbox_checked(page, row, timeout_ms=timeout_ms)
    return True


def _read_live_row_state(page, machine_name: str, *, timeout_ms: int, max_pages: int) -> dict[str, Any] | None:
    """Status / test mode from the headless page DOM only (never from webapp cache)."""
    row = _find_machine_row_live(page, machine_name, timeout_ms=timeout_ms, max_pages=max_pages)
    if row is None:
        return None
    mn, is_test, _game_type, status, online = _row_report_fields(row, timeout_ms=timeout_ms)
    return {
        "name": mn,
        "test": bool(is_test),
        "status": (status or "").strip(),
        "online": (online or "").strip(),
    }


def _status_is_maintenance(status_text: str) -> bool:
    su = (status_text or "").upper()
    return "MAINTAIN" in su or "METERCHECK" in su


def _status_is_occupy(status_text: str) -> bool:
    su = (status_text or "").upper()
    return "OCCUPY" in su


def _status_is_timeout(status_text: str) -> bool:
    return "TIMEOUT" in (status_text or "").upper()


def _failure_is_game_running(error: str, live: dict[str, Any] | None = None) -> bool:
    """True when set maintenance failed because a player/game is still on the machine."""
    if live and _status_is_timeout(str(live.get("status") or "")):
        return False
    if "game currently running" in (error or "").lower():
        return True
    if live and _status_is_occupy(str(live.get("status") or "")):
        return True
    err = (error or "").lower()
    if "timeout" in err:
        return False
    return "occupy" in err or ("game" in err and "running" in err)


def _set_maint_verify_poll_sec() -> float:
    try:
        return max(3.0, float((os.environ.get("PROD_SET_MAINT_VERIFY_POLL_SEC") or "18").strip()))
    except ValueError:
        return 18.0


def _row_state_indicates_maintenance(
    page,
    row,
    *,
    timeout_ms: int,
    check_toolbar: bool = False,
) -> bool:
    """True when the EGM row is in maintenance (status text, row HTML, or toolbar buttons)."""
    _mn, _is_test, _gt, status, _online = _row_report_fields(row, timeout_ms=timeout_ms)
    if _status_is_maintenance(status):
        return True
    try:
        row_text = row.inner_text(timeout=min(8_000, timeout_ms)) or ""
        if _status_is_maintenance(row_text):
            return True
    except Exception:
        pass
    try:
        cells = row.locator("td.el-table__cell")
        if cells.count() >= 7:
            html = (cells.nth(6).inner_html(timeout=min(8_000, timeout_ms)) or "").lower()
            if "maintain" in html or "metercheck" in html or "pill-maint" in html:
                return True
    except Exception:
        pass
    if check_toolbar:
        return _toolbar_row_in_maintenance(page, row, timeout_ms=timeout_ms)
    return False


def _toolbar_row_in_maintenance(page, row, *, timeout_ms: int) -> bool:
    """
    With the row selected: BatchStart Using enabled and BatchMaintenance disabled
    means maintenance mode is active even when the Status pill still reads ``occupy``.
    """
    _ensure_row_checkbox_checked(page, row, timeout_ms=timeout_ms)
    _wait_batch_toolbar_ready(page, "BatchStart Using", timeout_ms=timeout_ms, wait_ms=8_000)
    start_btn = _locate_batch_toolbar_button(page, "BatchStart Using")
    maint_btn = _locate_batch_toolbar_button(page, "BatchMaintenance")
    return (
        _batch_toolbar_button_actionable(start_btn)
        and not _batch_toolbar_button_actionable(maint_btn)
    )


_ROW_MAINT_BTN_PAT = re.compile(r"Maintenance|维护", re.I)


def _egm_row_button_actionable(btn) -> bool:
    try:
        if btn.count() == 0:
            return False
        cls = btn.get_attribute("class") or ""
        if "is-disabled" in cls:
            return False
        aria_d = (btn.get_attribute("aria-disabled") or "").strip().lower()
        if aria_d == "true":
            return False
        dis = btn.get_attribute("disabled")
        if dis is not None:
            dsl = str(dis).strip().lower()
            if dsl and dsl not in ("false", "0"):
                return False
        return bool(btn.is_enabled())
    except Exception:
        return False


def _row_operation_cell(row):
    cell = row.locator("td.el-table__cell").last
    if cell.count():
        return cell
    return row.locator("td").last


def _locate_row_maintenance_button(row):
    """Per-row **Maintenance** button (last column) — required when status is ``timeout``."""
    op_cell = _row_operation_cell(row)
    btns = op_cell.locator("button")
    try:
        n = btns.count()
    except Exception:
        n = 0
    for i in range(n):
        b = btns.nth(i)
        try:
            if not _egm_row_button_actionable(b):
                continue
            tx = (b.inner_text() or "").strip()
            if _ROW_MAINT_BTN_PAT.search(tx):
                return b
            title = f"{b.get_attribute('title') or ''} {b.get_attribute('aria-label') or ''}"
            if _ROW_MAINT_BTN_PAT.search(title):
                return b
        except Exception:
            continue
    return None


def _submit_row_maintenance_action(
    page, row, remark: str, *, timeout_ms: int
) -> tuple[bool, str]:
    """Click the row-level Maintenance button (not BatchMaintenance toolbar)."""
    btn = _locate_row_maintenance_button(row)
    if btn is None:
        return False, (
            "row Maintenance button not found or disabled "
            "(timeout machines cannot use BatchMaintenance)"
        )
    try:
        row.scroll_into_view_if_needed(timeout=min(15_000, timeout_ms))
        btn.click(timeout=min(30_000, timeout_ms))
    except Exception:
        try:
            btn.click(force=True, timeout=min(30_000, timeout_ms))
        except Exception as ex2:
            return False, f"row Maintenance click failed: {ex2!r}"
    _page_pause(page, 500)
    if _visible_confirm_layer(page) is not None:
        try:
            _click_save_confirm(page, remark, timeout_ms=timeout_ms)
            _wait_batch_done(page, timeout_ms=timeout_ms)
        except Exception as e:
            return False, f"confirm/save failed: {e}"
    else:
        _page_pause(page, 800)
        if _visible_confirm_layer(page) is not None:
            try:
                _click_save_confirm(page, remark, timeout_ms=timeout_ms)
                _wait_batch_done(page, timeout_ms=timeout_ms)
            except Exception as e:
                return False, f"confirm/save failed: {e}"
        else:
            _wait_batch_done(page, timeout_ms=timeout_ms)
    return True, ""


def _split_still_need_by_timeout(
    still_need: list[dict], live_states: dict[str, dict[str, Any] | None]
) -> tuple[list[dict], list[dict]]:
    timeout_rows: list[dict] = []
    batch_rows: list[dict] = []
    for m in still_need:
        name = _machine_display_name(m)
        live = live_states.get(name) or {}
        if _status_is_timeout(str(live.get("status") or "")):
            timeout_rows.append(m)
        else:
            batch_rows.append(m)
    return timeout_rows, batch_rows


def _process_timeout_set_maint_rows(
    page,
    machines: list[dict],
    belongs: str,
    remark: str,
    live_states: dict[str, dict[str, Any] | None],
    verify_action: str,
    cancel_check: Callable[[], bool],
    manual_stop_check: Callable[[], bool],
    *,
    timeout_ms: int,
    max_pages: int,
    ok_list: list[dict],
    fail_list: list[dict],
) -> None:
    """``timeout`` rows must use per-row Maintenance — BatchMaintenance is disabled for them."""
    _clear_table_row_selection(page, timeout_ms=timeout_ms)
    for m in machines:
        if cancel_check() or manual_stop_check():
            break
        name = _machine_display_name(m)
        if not name:
            continue
        live = live_states.get(name)
        if live and _verify_live_state(live, verify_action):
            ok_list.append({"belongs": m.get("belongs", belongs), "machine": name})
            continue
        row = _find_machine_row_live(page, name, timeout_ms=timeout_ms, max_pages=max_pages)
        if row is None:
            fail_list.append(
                {
                    "belongs": m.get("belongs", belongs),
                    "machine": name,
                    "error": "machine not found on EGM page",
                    "live": live,
                }
            )
            continue
        clicked, why = _submit_row_maintenance_action(
            page, row, remark, timeout_ms=timeout_ms
        )
        if not clicked:
            fail_list.append(
                {
                    "belongs": m.get("belongs", belongs),
                    "machine": name,
                    "error": why or "row Maintenance failed",
                    "live": live,
                }
            )
            continue
        _refresh_egm_table(page, timeout_ms=timeout_ms, max_pages=max_pages)
        post_live = _read_live_row_state(
            page, name, timeout_ms=timeout_ms, max_pages=max_pages
        )
        verified = bool(post_live and _verify_live_state(post_live, verify_action))
        if not verified and verify_action == "set_maint":
            verified = _verify_set_maint_applied(
                page, name, post_live, timeout_ms=timeout_ms, max_pages=max_pages
            )
        if verified:
            ok_list.append({"belongs": m.get("belongs", belongs), "machine": name})
        else:
            fail_list.append(
                {
                    "belongs": m.get("belongs", belongs),
                    "machine": name,
                    "error": "timeout row Maintenance — status not as expected after click",
                    "live": post_live,
                }
            )


def _verify_set_maint_applied(
    page,
    machine_name: str,
    live: dict[str, Any] | None,
    *,
    timeout_ms: int,
    max_pages: int,
) -> bool:
    """Poll live EGM after BatchMaintenance — occupy rows may lag or only show via toolbar."""
    if live and _verify_live_state(live, "set_maint"):
        return True

    deadline = time.monotonic() + _set_maint_verify_poll_sec()
    while time.monotonic() < deadline:
        row = _find_machine_row_live(
            page, machine_name, timeout_ms=timeout_ms, max_pages=max_pages
        )
        if row is not None and _row_state_indicates_maintenance(
            page, row, timeout_ms=timeout_ms, check_toolbar=True
        ):
            return True
        _page_pause(page, 1200)
        _refresh_egm_table(page, timeout_ms=timeout_ms, max_pages=max_pages)
        live = _read_live_row_state(
            page, machine_name, timeout_ms=timeout_ms, max_pages=max_pages
        )
        if live and _verify_live_state(live, "set_maint"):
            return True
    return False


def _verify_live_state(state: dict[str, Any], action: str) -> bool:
    is_maint = _status_is_maintenance(state.get("status", ""))
    is_test = bool(state.get("test"))
    if action == "set_maint":
        return is_maint
    if action == "set_test":
        return is_test
    if action == "set_both":
        return is_maint and is_test
    if action == "unset_maint":
        return not is_maint
    if action == "unset_test":
        return not is_test
    if action == "unset_both":
        return not is_maint and not is_test
    return True


def _verify_machine_live(
    page, machine_name: str, action: str, *, timeout_ms: int, max_pages: int
) -> bool:
    state = _read_live_row_state(page, machine_name, timeout_ms=timeout_ms, max_pages=max_pages)
    if state is None:
        return False
    return _verify_live_state(state, action)


def _refresh_egm_table(page, *, timeout_ms: int, max_pages: int) -> None:
    """Reload table data on the page we already have (stay on EGM list, do not use webapp API)."""
    scope = page.locator(".filter-container, .app-container").first
    refresh = scope.get_by_role("button", name=re.compile(r"^refresh$", re.I))
    if refresh.count():
        try:
            refresh.first.click(timeout=min(30_000, timeout_ms))
            _wait_table_idle(page, timeout_ms)
        except Exception:
            pass
    limit = _resolve_collect_page_limit(max_pages)
    _go_first_page(page, timeout_ms=timeout_ms, max_steps=limit)
    _wait_table_idle(page, timeout_ms)


def _batch_button_match_pattern(label: str) -> re.Pattern[str]:
    """Match toolbar labels with flexible whitespace (``BatchKick Out`` vs ``BatchKickOut``)."""
    parts = [p for p in re.split(r"\s+", (label or "").strip()) if p]
    if not parts:
        return re.compile(r"^$")
    return re.compile(r"\s+".join(re.escape(p) for p in parts), re.I)


def _locate_batch_toolbar_button(page, label: str):
    pat = _batch_button_match_pattern(label)
    for scope in (
        page.locator(".filter-container"),
        page.locator(".app-container"),
        page,
    ):
        btn = scope.locator("button").filter(has_text=pat).first
        if btn.count():
            return btn
    role_btn = page.get_by_role("button", name=pat).first
    if role_btn.count():
        return role_btn
    return page.locator("button").filter(has_text=pat).first


def _batch_toolbar_button_actionable(btn) -> bool:
    """True when the batch toolbar button can receive a click (not ``is-disabled``)."""
    try:
        if btn.count() == 0:
            return False
        cls = btn.get_attribute("class") or ""
        if "is-disabled" in cls:
            return False
        aria_d = (btn.get_attribute("aria-disabled") or "").strip().lower()
        if aria_d == "true":
            return False
        dis = btn.get_attribute("disabled")
        if dis is not None:
            dsl = str(dis).strip().lower()
            if dsl and dsl not in ("false", "0"):
                return False
        return bool(btn.is_enabled())
    except Exception:
        return False


def _wait_batch_toolbar_ready(page, label: str, *, timeout_ms: int, wait_ms: int = 12_000) -> bool:
    """After row checkbox selection, EGM may enable toolbar buttons after a short delay."""
    deadline = time.monotonic() + min(wait_ms / 1000.0, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        btn = _locate_batch_toolbar_button(page, label)
        if btn.count() and _batch_toolbar_button_actionable(btn):
            return True
        page.wait_for_timeout(_fast_ms(250))
    btn = _locate_batch_toolbar_button(page, label)
    return btn.count() > 0 and _batch_toolbar_button_actionable(btn)


def _click_batch_button(page, label: str, *, timeout_ms: int, force: bool = False) -> tuple[bool, str]:
    """
    Click a toolbar batch button. Tries a real click first (Occupy/Timeout rows are
    often clickable once selected). Only reports disabled if click fails and button
    still looks non-actionable; optional force click as last resort.
    """
    btn = _locate_batch_toolbar_button(page, label)
    if btn.count() == 0:
        return False, "not found"
    try:
        btn.click(timeout=min(30_000, timeout_ms))
        _page_pause(page, 400)
        return True, ""
    except Exception:
        if force:
            try:
                btn.click(force=True, timeout=min(30_000, timeout_ms))
                _page_pause(page, 400)
                return True, ""
            except Exception:
                try:
                    btn.evaluate("el => el.click()")
                    _page_pause(page, 400)
                    return True, ""
                except Exception:
                    pass
        if not _batch_toolbar_button_actionable(btn):
            return False, "disabled"
        return False, "click failed"


def _submit_batch_action(page, label: str, remark: str, *, timeout_ms: int) -> tuple[bool, str]:
    """Click batch toolbar button and Save when confirm dialog opens."""
    _wait_batch_toolbar_ready(page, label, timeout_ms=timeout_ms)
    clicked, why = _click_batch_button(page, label, timeout_ms=timeout_ms)
    if not clicked and why in ("disabled", "click failed"):
        clicked, why = _click_batch_button(page, label, timeout_ms=timeout_ms, force=True)
    if not clicked:
        return False, why

    _page_pause(page, 500)
    if _visible_confirm_layer(page) is not None:
        try:
            _click_save_confirm(page, remark, timeout_ms=timeout_ms)
            _wait_batch_done(page, timeout_ms=timeout_ms)
        except Exception as e:
            return False, f"confirm/save failed: {e}"
    else:
        _page_pause(page, 800)
        if _visible_confirm_layer(page) is not None:
            try:
                _click_save_confirm(page, remark, timeout_ms=timeout_ms)
                _wait_batch_done(page, timeout_ms=timeout_ms)
            except Exception as e:
                return False, f"confirm/save failed: {e}"
        else:
            _wait_batch_done(page, timeout_ms=timeout_ms)
    return True, ""


def _visible_confirm_layer(page):
    for sel in (
        ".el-dialog__wrapper:not([style*='display: none'])",
        ".el-message-box__wrapper:not([style*='display: none'])",
    ):
        loc = page.locator(sel).filter(has=page.locator(".el-dialog, .el-message-box")).last
        if loc.count():
            try:
                if loc.is_visible():
                    return loc
            except Exception:
                continue
    dlg = page.locator(".el-dialog__wrapper").filter(has=page.locator(".el-dialog")).last
    if dlg.count():
        try:
            if dlg.is_visible():
                return dlg
        except Exception:
            pass
    mbox = page.locator(".el-message-box__wrapper").last
    if mbox.count():
        try:
            if mbox.is_visible():
                return mbox
        except Exception:
            pass
    return None


def _dismiss_batch_confirm_cancel(page, *, timeout_ms: int) -> bool:
    """Close Warning / confirm UI with **Cancel** — never Save."""
    _page_pause(page, 350)
    layer = _visible_confirm_layer(page)
    if layer is None:
        try:
            page.wait_for_function(
                """() => {
                  const dlg = document.querySelector('.el-dialog__wrapper:not([style*="display: none"]) .el-dialog');
                  const mb = document.querySelector('.el-message-box__wrapper:not([style*="display: none"]) .el-message-box');
                  return !!(dlg && dlg.offsetParent) || !!(mb && mb.offsetParent);
                }""",
                timeout=min(8_000, timeout_ms),
            )
            layer = _visible_confirm_layer(page)
        except Exception:
            layer = None
    if layer is None:
        return False

    inner = layer.locator(".el-dialog, .el-message-box").first
    if inner.count() == 0:
        inner = layer

    for name_pat in (r"^cancel$", r"^close$", r"^no$"):
        btn = inner.get_by_role("button", name=re.compile(name_pat, re.I))
        if btn.count():
            try:
                btn.first.click(timeout=min(15_000, timeout_ms))
                _wait_table_idle(page, timeout_ms)
                return True
            except Exception:
                continue

    cancel = inner.locator("button").filter(has_text=re.compile(r"cancel|关闭", re.I)).first
    if cancel.count():
        try:
            cancel.click(timeout=min(15_000, timeout_ms))
            _wait_table_idle(page, timeout_ms)
            return True
        except Exception:
            pass

    # Never click Save / Confirm / OK / primary.
    return False


def _clear_table_row_selection(page, *, timeout_ms: int) -> None:
    """Uncheck every visible row checkbox on the current page."""
    rows = _table_body_rows(page)
    n = rows.count()
    for i in range(n):
        try:
            _ensure_row_checkbox_unchecked(page, rows.nth(i), timeout_ms=timeout_ms)
        except Exception:
            continue


def _expect_toolbar_enabled_with_selection(label: str, *, is_maint: bool, is_test: bool) -> bool:
    """
    After one row is selected, which toolbar buttons should be clickable?

    - BatchStart Using → only when row already in maintenance.
    - BatchTestCancel → only when row in test mode.
    - BatchMaintenance / BatchTest → only when not already in that state.
    """
    if label == "BatchStart Using":
        return is_maint
    if label == "BatchTestCancel":
        return is_test
    if label == "BatchMaintenance":
        return not is_maint
    if label == "BatchTest":
        return not is_test
    return False


def _pick_probe_row(page, *, timeout_ms: int) -> tuple[Any, dict[str, Any]]:
    """Prefer a normal/online row so BatchMaintenance/BatchTest are enabled."""
    rows = _table_body_rows(page)
    n = rows.count()
    fallback = None
    fallback_ctx: dict[str, Any] = {}
    for i in range(n):
        row = rows.nth(i)
        try:
            mn, is_test, _gt, status, online = _row_report_fields(row, timeout_ms=timeout_ms)
        except Exception:
            continue
        su = (status or "").upper()
        ou = (online or "").upper()
        is_maint = "MAINTAIN" in su or "METERCHECK" in su
        is_online = "ONLINE" in ou and "OFFLINE" not in ou
        is_normalish = "NORMAL" in su or "OCCUPY" in su
        ctx = {
            "machine": mn,
            "maintenance": is_maint,
            "test": bool(is_test),
            "status": status,
            "online": online,
        }
        if fallback is None:
            fallback, fallback_ctx = row, ctx
        if is_online and is_normalish and not is_maint:
            return row, ctx
    if fallback is not None:
        return fallback, fallback_ctx
    raise RuntimeError("no EGM rows")


def _probe_one_toolbar_button(
    page,
    label: str,
    *,
    timeout_ms: int,
    expect_disabled: bool,
    expect_enabled_with_selection: bool | None = None,
) -> dict[str, Any]:
    """Check disabled/enabled state; if enabled, click and Cancel (no Save)."""
    btn = _locate_batch_toolbar_button(page, label)
    found = btn.count() > 0
    actionable = _batch_toolbar_button_actionable(btn) if found else False
    disabled = not actionable if found else None

    out: dict[str, Any] = {
        "found": found,
        "disabled": disabled,
        "ok": False,
        "detail": "",
    }

    if not found:
        out["detail"] = "button not found in toolbar"
        return out

    if expect_disabled:
        out["ok"] = not actionable
        if actionable:
            out["detail"] = "expected disabled with no row selected, but button is clickable"
        else:
            out["detail"] = "disabled without selection (expected)"
        return out

    should_enable = True if expect_enabled_with_selection is None else expect_enabled_with_selection
    if not should_enable:
        out["ok"] = not actionable
        if actionable:
            out["detail"] = "enabled but not expected for this row state (UI may differ)"
        else:
            out["detail"] = "disabled (expected for this row — e.g. not in maintenance/test)"
        return out

    if not actionable:
        out["detail"] = "expected enabled after row selected, but button is disabled"
        return out

    if not _click_batch_button(page, label, timeout_ms=timeout_ms)[0]:
        out["detail"] = "click failed"
        return out

    cancelled = _dismiss_batch_confirm_cancel(page, timeout_ms=timeout_ms)
    if cancelled:
        out["ok"] = True
        out["detail"] = "clicked; Warning/confirm dismissed with Cancel (Save not used)"
        return out

    # Some actions may not open a dialog (harmless no-op UI); still count as click OK.
    out["ok"] = True
    out["detail"] = "clicked; no confirm dialog (Save not used)"
    return out


def probe_egm_batch_toolbar_buttons(
    page,
    belongs: str,
    *,
    timeout_ms: int = 120_000,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """
    Dry-run every EGM toolbar batch button: no selection → must be disabled;
    one row selected → click → Cancel only (never Save).
    """
    result: dict[str, Any] = {
        "belongs": belongs,
        "sample_machine": "",
        "buttons": {},
        "error": None,
    }

    ok_login, login_err = _ensure_env_egm_page(
        page, belongs, timeout_ms=timeout_ms, max_pages=max_pages
    )
    if not ok_login:
        result["error"] = login_err or "login failed"
        return result

    rows = _table_body_rows(page)
    try:
        rows.first.wait_for(state="visible", timeout=min(20_000, timeout_ms))
    except Exception:
        pass
    if rows.count() == 0:
        result["error"] = "no EGM rows on first page"
        return result

    _clear_table_row_selection(page, timeout_ms=timeout_ms)

    for label in EGM_TOOLBAR_BATCH_BUTTONS:
        result["buttons"][label] = {
            "without_selection": _probe_one_toolbar_button(
                page, label, timeout_ms=timeout_ms, expect_disabled=True
            )
        }

    row, row_ctx = _pick_probe_row(page, timeout_ms=timeout_ms)
    result["sample_machine"] = row_ctx.get("machine") or "(probe row)"
    is_maint = bool(row_ctx.get("maintenance"))
    is_test = bool(row_ctx.get("test"))

    _ensure_row_checkbox_checked(page, row, timeout_ms=timeout_ms)

    for label in EGM_TOOLBAR_BATCH_BUTTONS:
        _ensure_row_checkbox_checked(page, row, timeout_ms=timeout_ms)
        expect_on = _expect_toolbar_enabled_with_selection(
            label, is_maint=is_maint, is_test=is_test
        )
        probe = _probe_one_toolbar_button(
            page,
            label,
            timeout_ms=timeout_ms,
            expect_disabled=False,
            expect_enabled_with_selection=expect_on,
        )
        result["buttons"][label]["with_selection"] = probe
        result["buttons"][label]["row_context"] = {**row_ctx, "expects_enabled": expect_on}
        _dismiss_batch_confirm_cancel(page, timeout_ms=timeout_ms)
        _wait_table_idle(page, timeout_ms)

    _clear_table_row_selection(page, timeout_ms=timeout_ms)
    return result


def run_egm_batch_button_probe(
    site_aliases: list[str] | None = None,
    *,
    headless: bool | None = None,
    timeout_ms: int = 120_000,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """Login to each PROD backend and dry-run toolbar batch buttons (Cancel only, never Save)."""
    from playwright.sync_api import sync_playwright

    from smmachine import DEFAULT_WEBMACHINE_SITES, _dedupe_site_keys_by_resolved_backend

    if site_aliases:
        use = [s.strip().lower() for s in site_aliases if (s or "").strip()]
    else:
        raw_env = (os.environ.get("WEBMACHINE_SITES") or "").strip()
        if raw_env:
            use = [s.strip().lower() for s in raw_env.split(",") if s.strip()]
        else:
            use = list(DEFAULT_WEBMACHINE_SITES)

    use, skipped = _dedupe_site_keys_by_resolved_backend(use)
    hl = _smachine_resolve_headless(headless)
    if headless is None and _truthy_env("SM_MACHINE_HEADED"):
        hl = False

    report: dict[str, Any] = {"sites": {}, "skipped": skipped, "headless": hl}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=hl)
        context = browser.new_context(
            viewport={"width": 1600, "height": 900},
            ignore_https_errors=True,
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            for sk in use:
                belongs = _SITE_ALIAS_BELONGS.get(sk, sk.upper())
                try:
                    synth = _site_synthetic_machine(sk)
                    from checkcredit import _np_resolve_backend  # noqa: WPS433

                    base, user, pw = _np_resolve_backend(synth)
                    if not (user and pw):
                        report["sites"][sk] = {
                            "belongs": belongs,
                            "error": f"missing credentials for site {sk!r}",
                        }
                        continue
                    page._prod_set_belongs = None  # type: ignore[attr-defined]
                    report["sites"][sk] = probe_egm_batch_toolbar_buttons(
                        page,
                        belongs,
                        timeout_ms=timeout_ms,
                        max_pages=max_pages,
                    )
                except Exception as e:
                    report["sites"][sk] = {"belongs": belongs, "error": str(e)}
        finally:
            try:
                context.close()
            except Exception:
                pass
            browser.close()

    return report


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _click_save_confirm(page, remark: str, *, timeout_ms: int) -> None:
    _page_pause(page, 500)
    dlg = page.locator(".el-dialog__wrapper").filter(has=page.locator(".el-dialog")).last
    if dlg.count() == 0:
        dlg = page.locator(".el-dialog").last
    dlg.wait_for(state="visible", timeout=min(30_000, timeout_ms))
    inner = dlg.locator(".el-dialog").first if dlg.locator(".el-dialog").count() else dlg
    if remark:
        for sel in ("textarea", "input[type='text']"):
            ta = inner.locator(sel).first
            if ta.count() and ta.is_visible():
                ta.fill(remark)
                break
    for name_pat in (r"^save$", r"confirm", r"^ok$"):
        btn = inner.get_by_role("button", name=re.compile(name_pat, re.I))
        if btn.count():
            btn.first.click(timeout=min(30_000, timeout_ms))
            _wait_table_idle(page, timeout_ms)
            return
    inner.locator("button.el-button--primary").first.click(timeout=min(30_000, timeout_ms))
    _wait_table_idle(page, timeout_ms)


def _wait_batch_done(page, *, timeout_ms: int) -> None:
    deadline = time.monotonic() + min(timeout_ms / 1000.0, 120.0)
    while time.monotonic() < deadline:
        _wait_table_idle(page, min(15_000, timeout_ms))
        _page_pause(page, 350)
        try:
            busy = page.locator(".el-loading-mask").filter(has=page.locator(":visible")).count()
            if busy == 0:
                break
        except Exception:
            break
    _page_pause(page, 600)


def _process_env(
    page,
    belongs: str,
    machines: list[dict],
    action: str,
    remark: str,
    cancel_check: Callable[[], bool],
    manual_stop_check: Callable[[], bool],
    *,
    timeout_ms: int,
    max_pages: int,
) -> tuple[list[dict], list[dict]]:
    ok_list: list[dict] = []
    fail_list: list[dict] = []

    if cancel_check():
        return ok_list, fail_list

    ok_login, login_err = _ensure_env_egm_page(
        page, belongs, timeout_ms=timeout_ms, max_pages=max_pages
    )
    if not ok_login:
        for m in machines:
            fail_list.append(
                {
                    "belongs": m.get("belongs", belongs),
                    "machine": _machine_display_name(m),
                    "error": login_err or "login failed",
                }
            )
        return ok_list, fail_list

    buttons = ACTION_BUTTONS.get(action, [])
    if not buttons:
        return ok_list, fail_list

    return _process_env_batch(
        page,
        belongs,
        machines,
        action,
        remark,
        buttons,
        action,
        cancel_check,
        manual_stop_check,
        timeout_ms=timeout_ms,
        max_pages=max_pages,
        ok_list=ok_list,
        fail_list=fail_list,
    )


def _batch_disabled_error(
    page,
    btn: str,
    machines: list[dict],
    belongs: str,
    *,
    timeout_ms: int,
    max_pages: int,
) -> str:
    """Human-readable reason when a toolbar batch button stays disabled."""
    hints: list[str] = []
    try:
        live_states = _batch_read_live_states(
            page, machines, timeout_ms=timeout_ms, max_pages=max_pages
        )
    except Exception:
        live_states = {}
    for m in machines:
        name = _machine_display_name(m)
        if not name:
            continue
        live = live_states.get(name)
        if live is None:
            hints.append(f"{name}: not on current EGM table (or lost selection)")
            continue
        st = (live.get("status") or "").strip()
        ol = (live.get("online") or "").strip()
        su = st.upper()
        ou = ol.upper()
        is_maint = _status_is_maintenance(st)
        is_test = bool(live.get("test"))
        if btn == "BatchMaintenance" and is_maint:
            hints.append(f"{name}: already in maintenance (status={st!r})")
        elif btn == "BatchTest" and is_test:
            hints.append(f"{name}: already in test mode")
        elif "OCCUPY" in su:
            hints.append(
                f"{name}: status {st!r} — BatchMaintenance should be clickable when row is "
                f"selected; check checkbox selection (online={ol!r})"
            )
        elif "TIMEOUT" in su:
            hints.append(
                f"{name}: status timeout — use row Maintenance button (BatchMaintenance disabled)"
            )
        elif "OFFLINE" in ou:
            hints.append(f"{name}: offline ({ol!r}) — batch maintenance usually blocked")
        elif btn == "BatchMaintenance":
            hints.append(f"{name}: BatchMaintenance disabled (status={st!r}, online={ol!r})")
        else:
            hints.append(f"{name}: {btn} disabled (status={st!r}, online={ol!r})")
    if hints:
        return f"button {btn} disabled — " + "; ".join(hints)
    return (
        f"button {btn} disabled (row state may already match, game running, offline, "
        "or no row selected)"
    )


def _process_env_batch(
    page,
    belongs: str,
    machines: list[dict],
    action: str,
    remark: str,
    buttons: list[str],
    verify_action: str,
    cancel_check: Callable[[], bool],
    manual_stop_check: Callable[[], bool],
    *,
    timeout_ms: int,
    max_pages: int,
    ok_list: list[dict],
    fail_list: list[dict],
) -> tuple[list[dict], list[dict]]:
    if cancel_check() or manual_stop_check():
        return ok_list, fail_list

    try:
        # Read each row's live state in the same pagination pass that ticks its checkbox, so we
        # avoid a second full table scan before clicking (big speed-up for large machine lists).
        selected, select_fail, live_states = _batch_select_machines_on_live_page(
            page, machines, timeout_ms=timeout_ms, max_pages=max_pages, read_states=True
        )
        fail_list.extend(select_fail)
    except Exception as exc:
        for m in machines:
            fail_list.append(
                {
                    "belongs": m.get("belongs", belongs),
                    "machine": _machine_display_name(m),
                    "error": str(exc),
                }
            )
        return ok_list, fail_list

    if cancel_check() or manual_stop_check() or not selected:
        return ok_list, fail_list

    # Skip machines already in the desired state (e.g. on a retry); a failed state read is treated
    # as "needs action" since the row was found and selected — the post-click verify decides.
    still_need: list[dict] = []
    for m in selected:
        if cancel_check() or manual_stop_check():
            break
        name = _machine_display_name(m)
        if not name:
            continue
        live = live_states.get(name)
        if live and _verify_live_state(live, verify_action):
            ok_list.append({"belongs": m.get("belongs", belongs), "machine": name})
        else:
            still_need.append(m)

    if cancel_check() or manual_stop_check() or not still_need:
        return ok_list, fail_list

    timeout_need: list[dict] = []
    batch_need = list(still_need)
    if verify_action in ("set_maint", "set_both"):
        timeout_need, batch_need = _split_still_need_by_timeout(still_need, live_states)

    if timeout_need and verify_action in ("set_maint", "set_both"):
        _process_timeout_set_maint_rows(
            page,
            timeout_need,
            belongs,
            remark,
            live_states,
            verify_action,
            cancel_check,
            manual_stop_check,
            timeout_ms=timeout_ms,
            max_pages=max_pages,
            ok_list=ok_list,
            fail_list=fail_list,
        )

    still_need = batch_need
    if cancel_check() or manual_stop_check() or not still_need:
        return ok_list, fail_list

    _clear_table_row_selection(page, timeout_ms=timeout_ms)
    try:
        selected, reselect_fail, live_states2 = _batch_select_machines_on_live_page(
            page, still_need, timeout_ms=timeout_ms, max_pages=max_pages, read_states=True
        )
        fail_list.extend(reselect_fail)
        live_states.update(live_states2)
    except Exception as exc:
        for m in still_need:
            fail_list.append(
                {
                    "belongs": m.get("belongs", belongs),
                    "machine": _machine_display_name(m),
                    "error": str(exc),
                }
            )
        return ok_list, fail_list

    if cancel_check() or manual_stop_check() or not selected:
        return ok_list, fail_list

    for btn in buttons:
        _wait_batch_toolbar_ready(page, btn, timeout_ms=timeout_ms)

    for btn in buttons:
        if cancel_check() or manual_stop_check():
            break
        clicked, why = _submit_batch_action(page, btn, remark, timeout_ms=timeout_ms)
        if not clicked:
            if why == "disabled":
                err = _batch_disabled_error(
                    page, btn, selected, belongs, timeout_ms=timeout_ms, max_pages=max_pages
                )
                err += " (click failed — ensure row checkbox is selected on EGM page)"
            elif why == "not found":
                err = f"button {btn} not found"
            else:
                err = why or f"button {btn} failed"
            for m in selected:
                fail_list.append(
                    {
                        "belongs": m.get("belongs", belongs),
                        "machine": _machine_display_name(m),
                        "error": err,
                    }
                )
            return ok_list, fail_list

    _refresh_egm_table(page, timeout_ms=timeout_ms, max_pages=max_pages)

    try:
        post_states = _batch_read_live_states(
            page, selected, timeout_ms=timeout_ms, max_pages=max_pages
        )
    except Exception as exc:
        for m in selected:
            fail_list.append(
                {
                    "belongs": m.get("belongs", belongs),
                    "machine": _machine_display_name(m),
                    "error": str(exc),
                }
            )
        return ok_list, fail_list

    for m in selected:
        if cancel_check() or manual_stop_check():
            break
        name = _machine_display_name(m)
        if not name:
            continue
        live = post_states.get(name)
        verified = bool(live and _verify_live_state(live, verify_action))
        if not verified and verify_action == "set_maint":
            verified = _verify_set_maint_applied(
                page,
                name,
                live,
                timeout_ms=timeout_ms,
                max_pages=max_pages,
            )
        if verified:
            ok_list.append({"belongs": m.get("belongs", belongs), "machine": name})
        else:
            if verify_action == "set_maint" and live and _status_is_occupy(str(live.get("status") or "")):
                err = "game currently running"
            else:
                detail = ""
                if live:
                    detail = f" (live status={live.get('status')!r}, test={live.get('test')})"
                err = f"status not as expected on EGM page{detail}"
            fail_list.append(
                {
                    "belongs": m.get("belongs", belongs),
                    "machine": name,
                    "error": err,
                    "live": live,
                }
            )

    return ok_list, fail_list


def _run_step_with_retries(
    page,
    belongs: str,
    targets: list[dict],
    parent_action: str,
    remark: str,
    step_verify: str,
    step_buttons: list[str],
    cancel_check: Callable[[], bool],
    manual_stop_check: Callable[[], bool],
    *,
    timeout_ms: int,
    max_pages: int,
    on_phase_retry: Optional[Callable[[str, int, list[dict], list[dict]], None]] = None,
) -> tuple[bool, list[dict], list[dict]]:
    """
    Run one batch step, recheck live EGM, retry only failures until all pass or max attempts.
    Returns (passed, remaining_failures, done_ok) — done_ok so exhausted-retry runs still report
    the machines that DID verify (otherwise they'd vanish from the final summary).
    """
    pending = list(targets)
    max_r = _max_phase_retries()
    done_so_far: list[dict] = []

    ok_login, login_err = _ensure_env_egm_page(
        page, belongs, timeout_ms=timeout_ms, max_pages=max_pages
    )
    if not ok_login:
        return False, [
            {
                "belongs": m.get("belongs", belongs),
                "machine": _machine_display_name(m),
                "error": login_err or "login failed",
            }
            for m in pending
        ], []

    for attempt in range(1, max_r + 1):
        if cancel_check() or manual_stop_check():
            return False, pending, done_so_far

        ok_part: list[dict] = []
        fail_part: list[dict] = []
        ok_part, fail_part = _process_env_batch(
            page,
            belongs,
            pending,
            parent_action,
            remark,
            step_buttons,
            step_verify,
            cancel_check,
            manual_stop_check,
            timeout_ms=timeout_ms,
            max_pages=max_pages,
            ok_list=ok_part,
            fail_list=fail_part,
        )
        done_so_far.extend(ok_part)
        if not fail_part:
            return True, [], done_so_far
        if on_phase_retry:
            try:
                on_phase_retry(step_verify, attempt, fail_part, list(done_so_far))
            except Exception:
                logger.exception("on_phase_retry callback failed for %s", belongs)
        pending = fail_part

    return False, pending, done_so_far


def _run_single_action_env(
    page,
    belongs: str,
    machines: list[dict],
    action: str,
    remark: str,
    cancel_check: Callable[[], bool],
    manual_stop_check: Callable[[], bool],
    *,
    timeout_ms: int,
    max_pages: int,
    on_phase_retry: Optional[Callable[[str, int, list[dict], list[dict]], None]] = None,
) -> tuple[list[dict], list[dict]]:
    """set_maint / set_test / unset_* — retry until live EGM confirms, then return ok/fail lists."""
    buttons = ACTION_BUTTONS.get(action, [])
    if not buttons:
        return [], list(machines)

    targets = list(machines)
    passed, still_fail, done_ok = _run_step_with_retries(
        page,
        belongs,
        targets,
        action,
        remark,
        action,
        buttons,
        cancel_check,
        manual_stop_check,
        timeout_ms=timeout_ms,
        max_pages=max_pages,
        on_phase_retry=on_phase_retry,
    )
    if not passed:
        return done_ok, still_fail

    return [
        {"belongs": m.get("belongs", belongs), "machine": _machine_display_name(m)}
        for m in targets
        if _machine_display_name(m)
    ], []


def _run_phased_env(
    page,
    belongs: str,
    machines: list[dict],
    parent_action: str,
    remark: str,
    cancel_check: Callable[[], bool],
    manual_stop_check: Callable[[], bool],
    *,
    timeout_ms: int,
    max_pages: int,
    on_phase_retry: Optional[Callable[[str, int, list[dict], list[dict]], None]] = None,
    on_phase_continue: Optional[Callable[[str, str, list[dict], list[dict]], None]] = None,
) -> tuple[list[dict], list[dict]]:
    """
    For ``set_both`` / ``unset_both``: phase 1 (e.g. maintenance) → live recheck → retry failures
    until all pass; then phase 2 (test) the same way; then final combined verify on EGM page.

    A machine that exhausts retries in one phase is dropped (reported failed) but the remaining
    phase(s) still run for the machines that DID pass — one stuck machine no longer aborts the env.
    """
    steps = PHASED_STEPS.get(parent_action, [])
    if not steps:
        return [], list(machines)

    targets = list(machines)
    all_ok: list[dict] = []
    all_fail: list[dict] = []

    for step_idx, (step_verify, step_buttons) in enumerate(steps):
        if cancel_check() or manual_stop_check():
            return all_ok, all_fail

        phase_passed, pending, phase_done = _run_step_with_retries(
            page,
            belongs,
            targets,
            parent_action,
            remark,
            step_verify,
            step_buttons,
            cancel_check,
            manual_stop_check,
            timeout_ms=timeout_ms,
            max_pages=max_pages,
            on_phase_retry=on_phase_retry,
        )
        if not phase_passed:
            all_fail.extend(pending)
            if cancel_check() or manual_stop_check():
                return all_ok, all_fail
            # Retries exhausted for `pending` — continue the remaining step(s) with the
            # machines that did pass this phase instead of aborting the whole env.
            done_names = {str(x.get("machine") or "") for x in phase_done}
            targets = [m for m in targets if _machine_display_name(m) in done_names]
            if not targets:
                return all_ok, all_fail
            if on_phase_continue and step_idx < len(steps) - 1:
                next_verify = steps[step_idx + 1][0]
                try:
                    on_phase_continue(step_verify, next_verify, list(pending), list(targets))
                except Exception:
                    logger.exception("on_phase_continue callback failed for %s", belongs)

    _refresh_egm_table(page, timeout_ms=timeout_ms, max_pages=max_pages)
    try:
        final_states = _batch_read_live_states(
            page, targets, timeout_ms=timeout_ms, max_pages=max_pages
        )
    except Exception as exc:
        # Keep machines already failed in earlier phases — only the survivors get the
        # final-verify error.
        return all_ok, all_fail + [
            {
                "belongs": m.get("belongs", belongs),
                "machine": _machine_display_name(m),
                "error": str(exc),
            }
            for m in targets
            if _machine_display_name(m)
        ]

    for m in targets:
        if cancel_check() or manual_stop_check():
            break
        name = _machine_display_name(m)
        if not name:
            continue
        live = final_states.get(name)
        verified = bool(live and _verify_live_state(live, parent_action))
        if not verified and parent_action == "set_maint":
            verified = _verify_set_maint_applied(
                page,
                name,
                live,
                timeout_ms=timeout_ms,
                max_pages=max_pages,
            )
        if verified:
            all_ok.append({"belongs": m.get("belongs", belongs), "machine": name})
        else:
            if parent_action == "set_maint" and live and _status_is_occupy(str(live.get("status") or "")):
                err = "game currently running"
            else:
                detail = ""
                if live:
                    detail = f" (live status={live.get('status')!r}, test={live.get('test')})"
                err = f"final EGM check failed{detail}"
            all_fail.append(
                {
                    "belongs": m.get("belongs", belongs),
                    "machine": name,
                    "error": err,
                    "live": live,
                }
            )

    return all_ok, all_fail


def _dispatch_env_processing(
    page,
    belongs: str,
    env_machines: list[dict],
    action: str,
    remark: str,
    cancel_check: Callable[[], bool],
    manual_stop_check: Callable[[], bool],
    *,
    timeout_ms: int,
    max_pages: int | None,
    on_phase_retry: Optional[Callable[[str, int, list[dict], list[dict]], None]] = None,
    on_phase_continue: Optional[Callable[[str, str, list[dict], list[dict]], None]] = None,
) -> tuple[list[dict], list[dict]]:
    """Run one environment's machines on ``page`` using the right strategy for ``action``."""
    if action in PHASED_STEPS:
        return _run_phased_env(
            page, belongs, env_machines, action, remark,
            cancel_check, manual_stop_check,
            timeout_ms=timeout_ms, max_pages=max_pages, on_phase_retry=on_phase_retry,
            on_phase_continue=on_phase_continue,
        )
    if action in AUTO_RETRY_ACTIONS:
        return _run_single_action_env(
            page, belongs, env_machines, action, remark,
            cancel_check, manual_stop_check,
            timeout_ms=timeout_ms, max_pages=max_pages, on_phase_retry=on_phase_retry,
        )
    return _process_env(
        page, belongs, env_machines, action, remark,
        cancel_check, manual_stop_check,
        timeout_ms=timeout_ms, max_pages=max_pages,
    )


# ===================== Warm browser POOL (one per PROD environment) =====================
# Keeps a pre-launched, already-logged-in EGM browser for each PROD environment so a
# set/unset maintenance/test job only pays the click + live-recheck cost (no per-run Chromium
# cold start + EGM login). Each environment owns its own thread + Playwright objects (sync
# Playwright is thread-bound), so multi-environment jobs also run in parallel.
#
# Disable with ``PROD_WARM_POOL=0``. Choose which environments to keep open with
# ``PROD_WARM_ENVS`` (comma list, default = all). Pre-warm at startup unless
# ``PROD_WARM_PREWARM_ON_STARTUP=0``.

# Distinct PROD EGM backends (normalised ``belongs``). NWR/NP share one ("NP").
PROD_WARM_ALL_ENVS: tuple[str, ...] = ("NP", "NCH", "TBR", "TBP", "MDR", "DHS", "CP", "WF")

# Re-load the EGM list this often so a warm session never goes stale.
_PROD_WARM_KEEPALIVE_SEC = 240.0


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


def _prod_warm_pool_enabled() -> bool:
    return (os.environ.get("PROD_WARM_POOL", "1") or "").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _prod_warm_prewarm_on_startup() -> bool:
    return (os.environ.get("PROD_WARM_PREWARM_ON_STARTUP", "1") or "").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _prod_warm_envs() -> list[str]:
    raw = (os.environ.get("PROD_WARM_ENVS") or "").strip()
    if not raw:
        return list(PROD_WARM_ALL_ENVS)
    out: list[str] = []
    seen: set[str] = set()
    for tok in re.split(r"[,\s;]+", raw):
        env = _belongs_for_machine(tok)
        if env and env in PROD_WARM_ALL_ENVS and env not in seen:
            seen.add(env)
            out.append(env)
    return out or list(PROD_WARM_ALL_ENVS)


def _prod_warm_headless() -> bool:
    return _smachine_resolve_headless(
        os.environ.get("SMACHINE_HEADLESS", "1").strip().lower() not in ("0", "false", "no")
    )


class _ProdEnvWarm:
    """One pre-launched, EGM-logged-in browser for a single PROD environment, on its own thread."""

    def __init__(self, env: str) -> None:
        self.env = _belongs_for_machine(env)
        self._tasks: "_queue.Queue[dict]" = _queue.Queue()
        self._p = None
        self._context = None
        self._page = None
        self._last_active = time.monotonic()
        self._thread = threading.Thread(
            target=self._loop, name=f"prod-warm-{self.env}", daemon=True
        )
        self._thread.start()
        self._keepalive = threading.Thread(
            target=self._keepalive_loop, name=f"prod-warm-ka-{self.env}", daemon=True
        )
        self._keepalive.start()

    # ---- public API ----
    def submit_prewarm(self) -> None:
        self._tasks.put({"kind": "prewarm"})

    def run_env(
        self,
        action: str,
        env_machines: list[dict],
        remark: str,
        cancel_check: Callable[[], bool],
        manual_stop_check: Callable[[], bool],
        *,
        timeout_ms: int,
        max_pages: int | None,
        on_phase_retry: Optional[Callable[[str, int, list[dict], list[dict]], None]] = None,
        on_phase_continue: Optional[Callable[[str, str, list[dict], list[dict]], None]] = None,
        want_shots: bool,
    ) -> dict:
        done = threading.Event()
        box: dict = {}
        self._tasks.put({
            "kind": "run",
            "action": action,
            "machines": env_machines,
            "remark": remark,
            "cancel_check": cancel_check,
            "manual_stop_check": manual_stop_check,
            "timeout_ms": timeout_ms,
            "max_pages": max_pages,
            "on_phase_retry": on_phase_retry,
            "on_phase_continue": on_phase_continue,
            "want_shots": want_shots,
            "done": done,
            "box": box,
        })
        done.wait()
        return box

    # ---- worker thread ----
    def _loop(self) -> None:
        while True:
            task = self._tasks.get()
            kind = task.get("kind")
            if kind == "prewarm":
                try:
                    self._ensure_ready(task.get("timeout_ms") or _default_timeout_ms(),
                                       task.get("max_pages"))
                    print(f"[prod-warm:{self.env}] pre-warmed.", flush=True)
                except Exception as ex:
                    print(f"[prod-warm:{self.env}] prewarm failed: {ex!r}", flush=True)
                    self._teardown()
                continue
            if kind == "keepalive":
                try:
                    if self._healthy():
                        self._rewarm(task.get("timeout_ms") or _default_timeout_ms())
                except Exception:
                    self._teardown()
                continue
            if kind == "run":
                self._handle_run(task)
                continue

    def _handle_run(self, task: dict) -> None:
        box = task["box"]
        timeout_ms = task["timeout_ms"]
        max_pages = task["max_pages"]
        self._last_active = time.monotonic()
        try:
            ok_login, login_err = self._ensure_ready(timeout_ms, max_pages)
            if not ok_login:
                box["ok"] = []
                box["fail"] = [
                    {
                        "belongs": m.get("belongs", self.env),
                        "machine": _machine_display_name(m),
                        "error": login_err or "login failed",
                    }
                    for m in task["machines"]
                    if _machine_display_name(m)
                ]
                box["shots"] = []
                box["shot_errs"] = []
                self._teardown()
                return
            ok, fail = _dispatch_env_processing(
                self._page,
                self.env,
                task["machines"],
                task["action"],
                task["remark"],
                task["cancel_check"],
                task["manual_stop_check"],
                timeout_ms=timeout_ms,
                max_pages=max_pages,
                on_phase_retry=task["on_phase_retry"],
                on_phase_continue=task.get("on_phase_continue"),
            )
            box["ok"] = ok
            box["fail"] = fail
            shots: list[dict[str, Any]] = []
            shot_errs: list[dict[str, Any]] = []
            if task["want_shots"] and task["machines"] and not task["cancel_check"]() \
                    and not _prod_batch_fast_mode():
                try:
                    shots, shot_errs = _capture_prod_batch_screenshots_on_page(
                        self._page, task["machines"], timeout_ms=timeout_ms, max_pages=max_pages
                    )
                except Exception as exc:
                    logger.exception("prod-warm:%s screenshot failed", self.env)
                    shot_errs.append({"belongs": self.env, "machine": "", "error": str(exc)})
            box["shots"] = shots
            box["shot_errs"] = shot_errs
        except Exception as ex:
            # A failure mid-run may have partially acted on EGM — never silently re-run elsewhere.
            box["ok"] = box.get("ok") or []
            box["fail"] = box.get("fail") or [
                {
                    "belongs": m.get("belongs", self.env),
                    "machine": _machine_display_name(m),
                    "error": str(ex),
                }
                for m in task["machines"]
                if _machine_display_name(m)
            ]
            box["shots"] = box.get("shots") or []
            box["shot_errs"] = box.get("shot_errs") or []
            self._teardown()
        finally:
            self._last_active = time.monotonic()
            if self._healthy():
                try:
                    self._rewarm(timeout_ms)
                except Exception:
                    self._teardown()
            task["done"].set()

    def _keepalive_loop(self) -> None:
        while True:
            time.sleep(_PROD_WARM_KEEPALIVE_SEC)
            if self._healthy() and (time.monotonic() - self._last_active) >= _PROD_WARM_KEEPALIVE_SEC:
                self._tasks.put({"kind": "keepalive"})

    # ---- browser lifecycle ----
    def _healthy(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    def _launch(self) -> None:
        from playwright.sync_api import sync_playwright

        self._teardown()
        self._p = sync_playwright().start()
        profile = Path(os.path.join(tempfile.gettempdir(), f"prod_warm_profile_{self.env.lower()}"))
        profile.mkdir(parents=True, exist_ok=True)
        self._context = self._p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=_prod_warm_headless(),
            viewport={"width": 1600, "height": 900},
            ignore_https_errors=True,
        )
        self._page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )
        print(f"[prod-warm:{self.env}] browser launched.", flush=True)

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

    def _ensure_ready(self, timeout_ms: int, max_pages: int | None) -> tuple[bool, str]:
        if not self._healthy():
            self._launch()
            self._page.set_default_timeout(timeout_ms)
        return _ensure_env_egm_page(
            self._page, self.env, timeout_ms=timeout_ms, max_pages=max_pages
        )

    def _rewarm(self, timeout_ms: int) -> None:
        # Return to a clean first page of the EGM list so the next job starts ready.
        try:
            limit = _resolve_collect_page_limit(None)
            _go_first_page(self._page, timeout_ms=timeout_ms, max_steps=limit)
            _wait_table_idle(self._page, timeout_ms)
        except Exception:
            pass


class _ProdEnvWarmPool:
    def __init__(self) -> None:
        self._workers: dict[str, _ProdEnvWarm] = {}
        self._lock = threading.Lock()

    def _get(self, env: str) -> _ProdEnvWarm:
        norm = _belongs_for_machine(env)
        with self._lock:
            w = self._workers.get(norm)
            if w is None:
                w = _ProdEnvWarm(norm)
                self._workers[norm] = w
            return w

    def prewarm(self, envs: list[str]) -> None:
        for env in envs:
            self._get(env).submit_prewarm()

    def run_for_envs(
        self,
        by_env: dict[str, list[dict]],
        action: str,
        remark: str,
        cancel_check: Callable[[], bool],
        manual_stop_check: Callable[[], bool],
        *,
        timeout_ms: int,
        max_pages: int | None,
        on_phase_retry: Optional[Callable[[str, int, list[dict], list[dict]], None]] = None,
        on_phase_continue: Optional[Callable[[str, str, list[dict], list[dict]], None]] = None,
        on_env_start: Optional[Callable[[str, int], None]] = None,
        want_shots: bool,
    ) -> list[dict]:
        """Run each environment's machines on its warm browser, in parallel; collect result boxes."""
        boxes: dict[str, dict] = {}
        threads: list[threading.Thread] = []

        def _run(env: str, env_machines: list[dict]) -> None:
            worker = self._get(env)
            if on_env_start:
                try:
                    on_env_start(env, len(env_machines))
                except Exception:
                    logger.exception("on_env_start callback failed for %s", env)
            boxes[env] = worker.run_env(
                action,
                env_machines,
                remark,
                cancel_check,
                manual_stop_check,
                timeout_ms=timeout_ms,
                max_pages=max_pages,
                on_phase_retry=on_phase_retry,
                on_phase_continue=on_phase_continue,
                want_shots=want_shots,
            )

        for belongs, env_machines in by_env.items():
            if cancel_check():
                break
            t = threading.Thread(
                target=_run, args=(belongs, env_machines),
                name=f"prod-warm-dispatch-{belongs}", daemon=True,
            )
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        return list(boxes.values())


_prod_env_warm_pool_singleton: "_ProdEnvWarmPool | None" = None
_prod_env_warm_pool_lock = threading.Lock()


def _prod_env_warm_pool() -> "_ProdEnvWarmPool":
    global _prod_env_warm_pool_singleton
    with _prod_env_warm_pool_lock:
        if _prod_env_warm_pool_singleton is None:
            _prod_env_warm_pool_singleton = _ProdEnvWarmPool()
        return _prod_env_warm_pool_singleton


def prewarm_prod_env_pool_on_startup() -> None:
    """Pre-launch + EGM-login a browser for every configured PROD environment (call once at boot)."""
    if not _prod_warm_pool_enabled():
        print("[prod-warm] disabled (PROD_WARM_POOL=0).", flush=True)
        return
    if not _prod_warm_prewarm_on_startup():
        print("[prod-warm] startup pre-warm skipped (PROD_WARM_PREWARM_ON_STARTUP=0).", flush=True)
        return
    if not _playwright_available():
        print(
            "[prod-warm] startup pre-warm skipped — playwright not installed "
            "(pip install playwright && playwright install chromium). "
            "Local chat-only PC: set PROD_WARM_POOL=0 in .env.",
            flush=True,
        )
        return
    envs = _prod_warm_envs()
    try:
        print(f"[prod-warm] startup pre-warm: {', '.join(envs)}", flush=True)
        _prod_env_warm_pool().prewarm(envs)
    except Exception as ex:
        print(f"[prod-warm] startup pre-warm failed: {ex!r}", flush=True)


def run_prod_batch_job(
    action: str,
    machines: list[dict],
    remark: str = "",
    *,
    cancel_check: Optional[Callable[[], bool]] = None,
    manual_stop_check: Optional[Callable[[], bool]] = None,
    on_manual_stop: Optional[Callable[[dict], None]] = None,
    on_phase_retry: Optional[Callable[[str, int, list[dict], list[dict]], None]] = None,
    on_phase_continue: Optional[Callable[[str, str, list[dict], list[dict]], None]] = None,
    on_env_start: Optional[Callable[[str, int], None]] = None,
    capture_screenshots: bool | None = None,
) -> dict[str, Any]:
    """
    Run batch job grouped by environment. Returns summary with success/failed lists
    of {belongs, machine} dicts for Lark/UI.

    Verification always re-reads the Playwright EGM table (never webapp machine JSON).
    """
    from playwright.sync_api import sync_playwright

    if cancel_check is None:
        cancel_check = lambda: False
    if manual_stop_check is None:
        manual_stop_check = lambda: False

    by_env: dict[str, list[dict]] = {}
    for m in machines:
        b = _belongs_for_machine(m.get("belongs", ""))
        by_env.setdefault(b, []).append(m)

    all_ok: list[dict] = []
    all_fail: list[dict] = []
    shot_list: list[dict[str, Any]] = []
    shot_errs: list[dict[str, Any]] = []
    want_shots = capture_screenshots if capture_screenshots is not None else prod_batch_screenshots_enabled()

    timeout_ms = _default_timeout_ms()
    max_pages = int(os.environ.get("SM_MACHINE_MAX_PAGES") or 0) or None
    headless = _smachine_resolve_headless(
        os.environ.get("SMACHINE_HEADLESS", "1").strip().lower() not in ("0", "false", "no")
    )

    # Fast path: per-environment warm browsers (already logged into EGM). Each env runs on its own
    # browser/thread → multi-env jobs run in parallel and there is no cold start + login per run.
    if _prod_warm_pool_enabled():
        try:
            pool = _prod_env_warm_pool()
        except Exception:
            logger.exception("prod warm pool unavailable; using a fresh browser")
            pool = None
        if pool is not None:
            boxes = pool.run_for_envs(
                by_env, action, remark, cancel_check, manual_stop_check,
                timeout_ms=timeout_ms, max_pages=max_pages,
                on_phase_retry=on_phase_retry, on_phase_continue=on_phase_continue,
                on_env_start=on_env_start,
                want_shots=want_shots,
            )
            for box in boxes:
                all_ok.extend(box.get("ok") or [])
                all_fail.extend(box.get("fail") or [])
                shot_list.extend(box.get("shots") or [])
                shot_errs.extend(box.get("shot_errs") or [])
            return {
                "action": action,
                "success": all_ok,
                "failed": all_fail,
                "ok": [f"{x['belongs']}::{x['machine']}" for x in all_ok],
                "failed_keys": [f"{x['belongs']}::{x['machine']}" for x in all_fail],
                "screenshots": shot_list,
                "screenshot_errors": shot_errs,
            }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1600, "height": 900},
            ignore_https_errors=True,
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            for belongs, env_machines in by_env.items():
                if cancel_check():
                    break
                if on_env_start:
                    try:
                        on_env_start(belongs, len(env_machines))
                    except Exception:
                        logger.exception("on_env_start callback failed for %s", belongs)
                ok, fail = _dispatch_env_processing(
                    page,
                    belongs,
                    env_machines,
                    action,
                    remark,
                    cancel_check,
                    manual_stop_check,
                    timeout_ms=timeout_ms,
                    max_pages=max_pages,
                    on_phase_retry=on_phase_retry,
                    on_phase_continue=on_phase_continue,
                )
                all_ok.extend(ok)
                all_fail.extend(fail)

            if want_shots and machines and not cancel_check() and not _prod_batch_fast_mode():
                try:
                    shot_list, shot_errs = _capture_prod_batch_screenshots_on_page(
                        page, machines, timeout_ms=timeout_ms, max_pages=max_pages
                    )
                except Exception as exc:
                    logger.exception("prod-batch screenshot batch failed")
                    shot_errs.append({"belongs": "", "machine": "", "error": str(exc)})
        finally:
            try:
                context.close()
            except Exception:
                pass
            browser.close()

    return {
        "action": action,
        "success": all_ok,
        "failed": all_fail,
        "ok": [f"{x['belongs']}::{x['machine']}" for x in all_ok],
        "failed_keys": [f"{x['belongs']}::{x['machine']}" for x in all_fail],
        "screenshots": shot_list,
        "screenshot_errors": shot_errs,
    }


def live_verify_prod_machines(
    action: str,
    machines: list[dict],
    *,
    headless: bool | None = None,
    timeout_ms: int | None = None,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """
    Read-only live EGM check for each requested machine (no batch UI clicks).
    Returns the same ``success`` / ``failed`` shape as :func:`run_prod_batch_job`.
    """
    from playwright.sync_api import sync_playwright

    if max_pages is None:
        max_pages = int(os.environ.get("SM_MACHINE_MAX_PAGES") or 0) or None
    timeout_ms = timeout_ms or _default_timeout_ms()
    hl = _smachine_resolve_headless(headless)
    if headless is None:
        hl = _smachine_resolve_headless(
            os.environ.get("SMACHINE_HEADLESS", "1").strip().lower() not in ("0", "false", "no")
        )

    by_env: dict[str, list[dict]] = {}
    for m in machines:
        b = _belongs_for_machine(m.get("belongs", ""))
        by_env.setdefault(b, []).append(m)

    success: list[dict] = []
    failed: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=hl)
        context = browser.new_context(
            viewport={"width": 1600, "height": 900},
            ignore_https_errors=True,
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            for belongs, env_machines in by_env.items():
                ok_login, login_err = _ensure_env_egm_page(
                    page, belongs, timeout_ms=timeout_ms, max_pages=max_pages
                )
                if not ok_login:
                    for m in env_machines:
                        failed.append(
                            {
                                "belongs": m.get("belongs", belongs),
                                "machine": _machine_display_name(m),
                                "error": login_err or "login failed",
                            }
                        )
                    continue
                for m in env_machines:
                    name = _machine_display_name(m)
                    if not name:
                        continue
                    state = _read_live_row_state(
                        page, name, timeout_ms=timeout_ms, max_pages=max_pages
                    )
                    if state is None:
                        failed.append(
                            {
                                "belongs": m.get("belongs", belongs),
                                "machine": name,
                                "error": "machine not found on EGM page",
                            }
                        )
                        continue
                    if _verify_live_state(state, action):
                        success.append(
                            {
                                "belongs": m.get("belongs", belongs),
                                "machine": name,
                                "live": state,
                            }
                        )
                    else:
                        detail = (
                            f"live status={state.get('status')!r}, test={state.get('test')}"
                        )
                        failed.append(
                            {
                                "belongs": m.get("belongs", belongs),
                                "machine": name,
                                "error": detail,
                                "live": state,
                            }
                        )
        finally:
            try:
                context.close()
            except Exception:
                pass
            browser.close()

    return {
        "action": action,
        "success": success,
        "failed": failed,
        "ok": [f"{x['belongs']}::{x['machine']}" for x in success],
        "failed_keys": [f"{x['belongs']}::{x['machine']}" for x in failed],
        "live_check": True,
    }


run_prod_set_job = run_prod_batch_job
