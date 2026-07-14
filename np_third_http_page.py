"""Log Third Http Req page actions (login, search, Detail screenshot) — shared by cold + warm browsers."""
from __future__ import annotations

import os
import re
from typing import Any


def np_third_http_post_search_ms(*, headless: bool) -> int:
    post_search_ms = int(os.environ.get("NP_BACKEND_POST_SEARCH_MS", "2500").strip() or "2500")
    if headless:
        post_search_ms = max(
            post_search_ms,
            int(os.environ.get("NP_BACKEND_HEADLESS_POST_SEARCH_MS", "7000").strip() or "7000"),
        )
    return post_search_ms


def np_third_http_dialog_settle_ms(*, headless: bool) -> int:
    dialog_settle_ms = int(os.environ.get("NP_BACKEND_DIALOG_SETTLE_MS", "0").strip() or "0")
    if dialog_settle_ms <= 0:
        dialog_settle_ms = 2200 if headless else 900
    return dialog_settle_ms


def np_third_http_clear_and_fill_input(page, inp, value: str) -> None:
    """Element UI inputs often ignore ``fill('')`` on warm/persistent browsers — select-all + delete first."""
    inp.click()
    page.wait_for_timeout(120)
    try:
        inp.press("Control+a")
    except Exception:
        pass
    page.keyboard.press("Backspace")
    inp.fill("")
    inp.fill(str(value))
    page.wait_for_timeout(150)
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)


def np_third_http_dismiss_open_dialogs(page) -> None:
    """Close stray Detail dialogs before a new search."""
    try:
        for _ in range(3):
            dlg = page.locator(
                ".el-dialog.details-dialog, div[role='dialog'].details-dialog"
            )
            if dlg.count() == 0:
                break
            try:
                if not dlg.last.is_visible():
                    break
            except Exception:
                break
            page.keyboard.press("Escape")
            page.wait_for_timeout(350)
    except Exception:
        pass


def np_third_http_login_and_open_log_page(
    page,
    *,
    base: str,
    user: str,
    pw: str,
    machine_display: str | None,
    timeout_ms: int,
) -> None:
    import checkcredit as cc

    log_url = f"{base}/log/logThirdHttpReq"
    cur = page.url or ""
    if "/log/logThirdHttpReq" in cur:
        try:
            page.locator(".filter-container").first.wait_for(state="visible", timeout=5000)
            return
        except Exception:
            pass

    if cc._np_use_backend_osmplay_com(machine_display):
        login_url = f"{base}/login?redirect=%2Fegm%2FegmStatusList"
    else:
        login_url = f"{base}/login?redirect=%2Flog%2FlogThirdHttpReq"

    page.goto(login_url, wait_until="domcontentloaded")
    page.wait_for_timeout(800)

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

    login_btn = page.get_by_role("button", name=re.compile(r"login|sign in|log in", re.I))
    if login_btn.count():
        login_btn.first.click()
    else:
        page.locator('button[type="submit"], button.el-button--primary').first.click()

    page.wait_for_timeout(2000)
    if "/log/logThirdHttpReq" not in (page.url or ""):
        page.goto(log_url, wait_until="domcontentloaded")

    page.wait_for_selector(".filter-container", timeout=timeout_ms)


def np_third_http_run_search_and_screenshot(
    page,
    *,
    player_id: str,
    date_iso: str,
    time_short: str,
    time_short_candidates: list[str] | None,
    machine_substr: str | None,
    expected_credit: float | None,
    machine_display: str | None,
    timeout_ms: int,
    headless: bool,
    out_path: str,
) -> None:
    import checkcredit as cc

    _log_http_backend_tag = cc._np_log_backend_tag(machine_display)
    start_s, end_s = cc._np_window_strings(
        date_iso, time_short, extra_times=time_short_candidates
    )
    np_time_candidates = cc._np_dedupe_time_strings(
        [time_short, *(time_short_candidates or [])]
    )
    use_same_minute_boost = cc._np_same_minute_boost_useful(date_iso, np_time_candidates)
    post_search_ms = np_third_http_post_search_ms(headless=headless)
    dialog_settle_ms = np_third_http_dialog_settle_ms(headless=headless)

    dinputs = page.locator(".filter-container .data-content .ssdate input.el-input__inner")
    if dinputs.count() >= 2:
        np_third_http_clear_and_fill_input(page, dinputs.nth(0), start_s)
        np_third_http_clear_and_fill_input(page, dinputs.nth(1), end_s)
    else:
        raise RuntimeError("Could not find date range inputs on Log Third Http Req page.")

    uid_in = page.locator(".filter-container .el-form-item").filter(has_text="UserId").locator(
        "input.el-input__inner"
    ).first
    np_third_http_clear_and_fill_input(page, uid_in, str(player_id).strip())
    page.wait_for_timeout(300)

    def _click_np_search() -> None:
        attempts = [
            page.locator(".filter-container button, .filter-container .el-button").filter(
                has_text=re.compile(r"Search|查询", re.I)
            ),
            page.get_by_role("button", name=re.compile(r"search|查询", re.I)),
            page.locator('button.el-button--primary').filter(
                has_text=re.compile(r"Search|查询|search", re.I)
            ),
            page.locator("button").filter(has_text=re.compile(r"^Search$")),
        ]
        last_err: Exception | None = None
        for loc in attempts:
            try:
                if loc.count() == 0:
                    continue
                btn = loc.first
                btn.wait_for(state="visible", timeout=min(30_000, timeout_ms))
                btn.click(timeout=min(60_000, timeout_ms))
                return
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(
            "Could not find or click the Search button on Log Third Http Req "
            f"(UI may have changed). Last error: {last_err!r}"
        )

    _click_np_search()
    page.wait_for_timeout(post_search_ms)

    ms = (machine_substr or "").strip()
    exp_match = cc._np_expected_credit_for_match(expected_credit)
    need_detail_match = bool(ms) or exp_match is not None
    amt_scale = cc._np_tbp_amount_scale() if cc._np_use_tbp_log_backend(machine_display) else 1.0

    if need_detail_match:

        def _scan_detail_pages(
            exp_try: float | None, *, settle_ms: int | None = None
        ) -> tuple[bool, dict[str, Any]]:
            dsm = dialog_settle_ms if settle_ms is None else settle_ms
            stats: dict[str, Any] = {
                "recharge_rows": 0,
                "details_tried": 0,
                "sample_mids": [],
            }
            cc._np_pagination_go_first_page(page, timeout_ms=timeout_ms)
            for _pi in range(cc.NP_BACKEND_MAX_PAGES):
                n = cc._np_wait_third_http_search_results(page, timeout_ms=timeout_ms)
                if n <= 0:
                    if not cc._np_pagination_can_go_next(page):
                        break
                    cc._np_click_pagination_next(page, timeout_ms=timeout_ms)
                    continue
                rows = page.locator(".el-table__body tr.el-table__row")
                ordered = cc._np_list_recharge_indices_time_ordered(
                    page,
                    rows,
                    n,
                    date_iso,
                    time_short,
                    time_candidates=np_time_candidates,
                )
                stats["recharge_rows"] += len(ordered)
                if ordered:
                    if use_same_minute_boost and not need_detail_match:
                        same_min_rows = cc._np_indices_table_same_minute(
                            page,
                            rows,
                            ordered,
                            date_iso,
                            time_short,
                            time_candidates=np_time_candidates,
                        )
                        to_scan = cc._np_merge_same_minute_then_rest(same_min_rows, ordered)
                    else:
                        to_scan = ordered
                    stats["details_tried"] += len(to_scan)
                    ok = cc._np_try_screenshot_matching_detail(
                        page,
                        rows,
                        to_scan,
                        machine_substr=machine_substr,
                        expected_credit=exp_try,
                        out_path=out_path,
                        timeout_ms=timeout_ms,
                        dialog_settle_ms=dsm,
                        amount_scale=amt_scale,
                        soft_expected_credit=exp_match if exp_try is None else None,
                        scan_stats=stats,
                    )
                    if ok:
                        return True, stats
                if not cc._np_pagination_can_go_next(page):
                    break
                cc._np_click_pagination_next(page, timeout_ms=timeout_ms)
            return False, stats

        def _run_match_pass(*, extra_post_ms: int = 0, extra_dialog_ms: int = 0) -> tuple[bool, dict[str, Any]]:
            if extra_post_ms > 0:
                page.wait_for_timeout(extra_post_ms)
            settle = dialog_settle_ms + extra_dialog_ms
            matched, stats = _scan_detail_pages(exp_match, settle_ms=settle)
            ran_machine_only = False
            if (
                not matched
                and ms
                and exp_match is not None
                and cc._np_machine_only_fallback_enabled(_log_http_backend_tag)
            ):
                ran_machine_only = True
                mo_ok, mo_stats = _scan_detail_pages(None, settle_ms=settle)
                stats["recharge_rows"] += mo_stats.get("recharge_rows", 0)
                stats["details_tried"] += mo_stats.get("details_tried", 0)
                for s in mo_stats.get("sample_mids") or []:
                    if len(stats["sample_mids"]) < 8:
                        stats["sample_mids"].append(s)
                matched = mo_ok
            stats["ran_machine_only"] = ran_machine_only
            return matched, stats

        matched, scan_stats = _run_match_pass()
        if not matched and headless:
            np_third_http_dismiss_open_dialogs(page)
            _click_np_search()
            page.wait_for_timeout(post_search_ms + 2500)
            matched, scan_stats = _run_match_pass(extra_post_ms=800, extra_dialog_ms=900)

        if not matched:
            bits: list[str] = []
            if ms:
                bits.append(f"`machineId` containing `{ms}`")
            if exp_match is not None:
                bits.append(
                    f"`amount` within {cc._np_amount_match_eps()} of `{exp_match}`"
                    + (f" (÷ `{amt_scale}` scale)" if amt_scale != 1.0 else "")
                )
            elif ms:
                bits.append(
                    "latest credit was 0 or unset — only `machineId` is matched, not `amount`"
                )
            if scan_stats.get("ran_machine_only"):
                bits.append(
                    "machine-only fallback (log `reduce_num` / aft amount ≠ Detail `amount`) "
                    "also found nothing"
                )
            crit = "; ".join(bits) if bits else "expected filters"
            hint = (
                f" UserId `{player_id}` window `{start_s}`–`{end_s}`; "
                f"recharge rows seen `{scan_stats.get('recharge_rows', 0)}`, "
                f"Detail tries `{scan_stats.get('details_tried', 0)}`."
            )
            samples = scan_stats.get("sample_mids") or []
            if samples:
                hint += " Sample Detail machineId/amount: " + ", ".join(
                    f"{m or '?'}@{a}" for m, a in samples[:5]
                ) + "."
            elif int(scan_stats.get("recharge_rows") or 0) == 0:
                hint += " No recharge rows in table — check date/UserId filters or stale warm browser."
            raise RuntimeError(
                f"No {_log_http_backend_tag} Detail on pages 1–{cc.NP_BACKEND_MAX_PAGES} with {crit}.{hint} "
                "Increase NP_BACKEND_MAX_PAGES or NP_BACKEND_WINDOW_MINUTES."
            )
    else:
        n = cc._np_wait_third_http_search_results(page, timeout_ms=timeout_ms)
        if n <= 0:
            raise RuntimeError(
                f"No Log Third Http rows for UserId `{player_id}` between "
                f"`{start_s}` and `{end_s}` (empty table after Search)."
            )
        rows = page.locator(".el-table__body tr.el-table__row")
        ordered = cc._np_list_recharge_indices_time_ordered(
            page,
            rows,
            n,
            date_iso,
            time_short,
            time_candidates=np_time_candidates,
        )
        if not ordered:
            raise RuntimeError(
                'No table row with Event Type "recharge" for this UserId/time window.'
            )

        pick_i = ordered[0]
        row = rows.nth(pick_i)
        cc._np_click_row_show_details_link(row, timeout_ms=timeout_ms)

        dlg = page.locator(
            ".el-dialog.details-dialog, div[role='dialog'].details-dialog"
        ).last
        dlg.wait_for(state="visible", timeout=timeout_ms)
        cc._np_capture_detail_dialog_screenshot(
            page,
            dlg,
            out_path,
            timeout_ms=timeout_ms,
            settle_ms=600,
        )
