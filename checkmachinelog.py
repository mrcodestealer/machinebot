#!/usr/bin/env python3
"""
Machine logic log — detect errors, last player, last error (±10 line context).

Log source matches **checkcredit** (OSS HTTP by default; LogNavigator when
``CHECKCREDIT_USE_NAVIGATOR=1`` / ``CHECKCREDIT_USE_OSS=0``).

  python3 checkmachinelog.py 2074 --date 2026-06-26
  python3 checkmachinelog.py NCH1422 --date 2026-04-27 --no-ai
  python3 checkmachinelog.py CP0231 --navigator

Env (optional):
  CHECKMACHINELOG_AI_TAIL_LINES — lines from log end for AI (default 200)
  CHECKMACHINELOG_CTX_BEFORE / CHECKMACHINELOG_CTX_AFTER — error context (default 10 each)
  CHECKMACHINELOG_USE_AI — 0/false to skip LLM even when API key is set
  Same OSS / LogNavigator vars as checkcredit (``.env`` auto-loaded).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from typing import Any, Optional

_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_ROOT_DIR, ".env"))
except ImportError:
    pass

import checkcredit as cc


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default)).strip() or str(default)))
    except ValueError:
        return default


def _env_truthy(name: str, *, default: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def ai_tail_line_count() -> int:
    return max(1, _env_int("CHECKMACHINELOG_AI_TAIL_LINES", 200))


def ctx_before_lines() -> int:
    return _env_int("CHECKMACHINELOG_CTX_BEFORE", 10)


def ctx_after_lines() -> int:
    return _env_int("CHECKMACHINELOG_CTX_AFTER", 10)


def use_ai_summary() -> bool:
    return _env_truthy("CHECKMACHINELOG_USE_AI", default=True)


def tail_log_lines(log_text: str, *, max_lines: int | None = None) -> list[str]:
    lines = (log_text or "").splitlines()
    n = max_lines if max_lines is not None else ai_tail_line_count()
    if len(lines) <= n:
        return lines
    return lines[-n:]


def enrich_error_context(
    raw_lines: list[str],
    errors: list[dict[str, Any]],
    *,
    before: int | None = None,
    after: int | None = None,
) -> None:
    """In-place: set context_lines to ±before/after around each error line."""
    b = ctx_before_lines() if before is None else max(0, int(before))
    a = ctx_after_lines() if after is None else max(0, int(after))
    for e in errors:
        li = int(e.get("line_idx", -1))
        if li < 0:
            continue
        start = max(0, li - b)
        end = min(len(raw_lines), li + a + 1)
        ctx: list[str] = []
        for gi in range(start, end):
            marker = ">>" if gi == li else "  "
            ctx.append(f"{marker} {raw_lines[gi]}")
        e["context_lines"] = ctx


def enrich_merged_context(raw_lines: list[str], merged: list[dict[str, Any]]) -> None:
    for row in merged:
        enrich_error_context(raw_lines, row.get("errors") or [])


def _split_player_blocks(raw_lines: list[str]) -> list[tuple[str, list[tuple[int, str]]]]:
    """Same block boundaries as ``checkcredit.parse_user_blocks_full``."""
    blocks: list[tuple[str, list[tuple[int, str]]]] = []
    cur_uid: str | None = None
    cur_lines: list[tuple[int, str]] = []
    for i, line in enumerate(raw_lines):
        new_uid = cc._userid_from_marker_line(line)
        if new_uid:
            if cur_uid is not None:
                blocks.append((cur_uid, cur_lines))
            cur_uid = new_uid
            cur_lines = [(i, line)]
        elif cur_uid is not None:
            cur_lines.append((i, line))
    if cur_uid is not None:
        blocks.append((cur_uid, cur_lines))
    return blocks


def _last_block_lines_for_uid(
    raw_lines: list[str], uid: str | None
) -> list[tuple[int, str]]:
    if not uid:
        return []
    want = str(uid).strip()
    last: list[tuple[int, str]] = []
    for block_uid, blines in _split_player_blocks(raw_lines):
        if block_uid == want:
            last = blines
    return last


def _line_context(
    raw_lines: list[str],
    line_idx: int,
    *,
    before: int | None = None,
    after: int | None = None,
    highlight: bool = True,
) -> list[str]:
    b = ctx_before_lines() if before is None else max(0, int(before))
    a = ctx_after_lines() if after is None else max(0, int(after))
    start = max(0, line_idx - b)
    end = min(len(raw_lines), line_idx + a + 1)
    out: list[str] = []
    for gi in range(start, end):
        if highlight:
            marker = ">>" if gi == line_idx else "  "
            out.append(f"{marker} {raw_lines[gi]}")
        else:
            out.append(raw_lines[gi])
    return out


def extract_transfer_out(row: dict[str, Any] | None) -> dict[str, Any]:
    """Last resolved credit for a player (cur_coin / reduce_num / enter_game / aft)."""
    if not row:
        return {"amount": None, "time": None, "source": None, "line_idx": -1}
    lc = row.get("latest_credit")
    if not isinstance(lc, dict):
        return {"amount": None, "time": None, "source": None, "line_idx": -1}
    return {
        "amount": lc.get("value"),
        "time": (lc.get("time_short") or "").strip() or None,
        "source": (lc.get("source") or "").strip() or None,
        "line_idx": int(lc.get("line_idx", -1)),
        "user_id": str(row.get("user_id") or "").strip() or None,
    }


def find_last_success_line(
    raw_lines: list[str], uid: str | None
) -> dict[str, Any] | None:
    """Last ``successJson`` line with ``error: 0`` in the player's final log block."""
    blines = _last_block_lines_for_uid(raw_lines, uid)
    if not blines:
        return None
    best: dict[str, Any] | None = None
    for line_idx, line in blines:
        if "successJson" not in line:
            continue
        if not cc._ERR_ZERO.search(line):
            continue
        coin = cc._CUR_COIN.search(line)
        amount = None
        if coin:
            try:
                amount = float(coin.group(1))
            except ValueError:
                amount = None
        best = {
            "line_idx": line_idx,
            "full_line": line.rstrip(),
            "time": cc._line_time_prefix(line) or None,
            "cur_coin": amount,
        }
    if not best:
        return None
    best["context_lines"] = _line_context(raw_lines, int(best["line_idx"]))
    return best


def _row_for_uid(merged: list[dict[str, Any]], uid: str | None) -> dict[str, Any] | None:
    if not uid:
        return None
    want = str(uid).strip()
    for row in merged:
        if str(row.get("user_id", "")).strip() == want:
            return row
    return None


def _format_transfer_out_line(transfer: dict[str, Any], *, uid: str | None) -> str:
    amt = transfer.get("amount")
    ts = transfer.get("time") or "n/a"
    src = transfer.get("source") or "n/a"
    show_uid = str(transfer.get("user_id") or uid or "n/a").strip()
    if amt is None:
        return f"**Last player transfer-out credit:** `n/a` (User `{show_uid}` — no credit line parsed)"
    src_note = {
        "cur_coin": "successJson cur_coin",
        "reduce_num": "reduce_num",
        "enter_game_target": "enter_game target/add_num",
        "aft_interrogation_faild_amount": "aft interrogation amount",
    }.get(str(src), str(src))
    return (
        f"**Last player transfer-out credit:** `{amt}` @ `{ts}` "
        f"(User `{show_uid}`, from `{src_note}`)"
    )


def format_success_context_block(success: dict[str, Any] | None, *, uid: str | None) -> str:
    if not success:
        return (
            f"**Success log:** (none parsed for User `{uid or 'n/a'}` — no successJson with error 0)\n"
        )
    t = (success.get("time") or "").strip() or "n/a"
    coin = success.get("cur_coin")
    coin_s = "n/a" if coin is None else str(coin)
    ctx = success.get("context_lines") or []
    body = "\n".join(ctx) if ctx else (success.get("full_line") or "").strip() or "(no line)"
    b, a = ctx_before_lines(), ctx_after_lines()
    return (
        f"**Last success log** (no errors in log)\n"
        f"- User ID: `{uid or 'n/a'}`\n"
        f"- Time: `{t}`\n"
        f"- cur_coin: `{coin_s}`\n"
        f"- Context ({b} lines above + line + {a} below):\n"
        f"```\n{body}\n```\n"
    )


def _pick_latest_error(merged: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_li = -1
    for row in merged:
        for e in row.get("errors") or []:
            li = int(e.get("line_idx", -1))
            if li > best_li:
                best_li = li
                best = {**e, "user_id": cc.error_owner_user_id(e, row)}
    return best


def _transfer_out_for_report(
    last_player_row: dict[str, Any] | None,
    last_error: dict[str, Any] | None,
    *,
    latest_err_uid: str | None,
    raw_lines: list[str] | None = None,
    error_player_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transfer-out credit for display / Third Http — prefer aft-fail error line when present."""
    if last_error and _is_aft_interrogation_fail_error(last_error):
        fl = last_error.get("full_line") or ""
        parsed = cc._parse_aft_interrogation_fail_credit(fl)
        amt: float | None = None
        ts = (last_error.get("time") or "").strip() or None
        uid = str(last_error.get("user_id") or latest_err_uid or "").strip() or None
        if parsed:
            amt, ts2 = parsed
            if not ts:
                ts = ts2
        prow = error_player_row or last_player_row
        cands = cc.third_http_time_candidates(
            raw_lines=raw_lines,
            uid=uid,
            last_error=last_error,
            player_row=prow,
            primary_time=ts,
        )
        if cands:
            ts = cands[0]
        return {
            "amount": amt,
            "time": ts,
            "source": "aft_interrogation_faild_amount",
            "user_id": uid,
        }
    return extract_transfer_out(last_player_row)


def _is_aft_interrogation_fail_error(err: dict[str, Any]) -> bool:
    fl = (err.get("full_line") or err.get("snippet") or "").lower()
    return "aft interrogation faild" in fl or "aft interrogation failed" in fl


_LOG_AI_SYSTEM = (
    "You analyze the tail of a casino EGM logic log file.\n"
    "User blocks start with extra1/extra2/extra3 userid, enter_game userid, or leave_game userid.\n"
    "``aft interrogation faild`` (error 11) is a LEAVE/transfer-out failure — attribute it to the "
    "player in the nearest preceding httpaft:leave_game line, NOT a later enter_game player.\n"
    "Error lines contain JSON with 'error': N where N is an integer > 0.\n"
    "Reply with ONE JSON object only, no markdown, keys:\n"
    '  "last_player_user_id": string or null — userid of the player block that ends last in this tail\n'
    '  "last_error_user_id": string or null — userid owning the last error>0 line in this tail\n'
    '  "last_error_line": string or null — short quote of that error line (max 200 chars)\n'
    '  "last_error_code": string or null — the error field value\n'
    '  "summary": one sentence in English and one short line in 中文 explaining what happened\n'
)


def _llm_available() -> bool:
    try:
        import chatagent as ca

        return bool(ca.llm_available())
    except Exception:
        return False


def ai_summarize_log_tail(
    tail_text: str,
    *,
    machine_display: str,
    target_date: date,
) -> dict[str, Any] | None:
    """Ask LLM to read tail lines; return parsed JSON dict or None."""
    if not use_ai_summary() or not _llm_available():
        return None
    try:
        import chatagent as ca
    except Exception:
        return None

    api_key = ca._llm_api_key()
    if not api_key:
        return None

    user = (
        f"machine={machine_display!r} date={target_date.isoformat()}\n"
        f"--- log tail ({ai_tail_line_count()} lines max) ---\n"
        f"{tail_text.strip()}"
    )
    payload: dict[str, Any] = {
        "model": ca._llm_model_for_request(images=False),
        "messages": [
            {"role": "system", "content": _LOG_AI_SYSTEM},
            {"role": "user", "content": user},
        ],
        "max_tokens": 600,
        "temperature": 0.0,
    }
    try:
        if ca._is_ollama_base():
            payload["think"] = False
    except Exception:
        pass

    url = f"{ca._llm_base_url()}/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=ca._llm_timeout_sec()) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        return {"_error": str(e)}

    try:
        content = (body["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return {"_error": "invalid LLM response shape"}

    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I | re.M).strip()
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else {"_error": "LLM JSON not an object"}
    except json.JSONDecodeError:
        return {"summary": content[:800], "_raw": True}


def _truncate_for_lark(text: str, limit: int = 2400) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[: limit - 20] + "\n… (truncated)"


def _deterministic_log_summary(
    *,
    latest_any_uid: str | None,
    latest_err_uid: str | None,
    last_error: dict[str, Any] | None,
    transfer_out: dict[str, Any] | None,
) -> str:
    """Rule-based summary when LLM is off or unavailable."""
    err_uid = str((last_error or {}).get("user_id") or latest_err_uid or "").strip() or None
    uid = err_uid or latest_any_uid or "n/a"
    xfer = transfer_out or {}
    amt = xfer.get("amount")
    ts = xfer.get("time") or "n/a"
    lines: list[str] = []
    if last_error:
        code = last_error.get("error_count", "?")
        et = (last_error.get("time") or "").strip() or "n/a"
        fl = (last_error.get("full_line") or last_error.get("snippet") or "").lower()
        if "aft interrogation faild" in fl or "aft interrogation failed" in fl:
            gist = (
                f"Player `{uid}` tried to leave/transfer **{amt}** credit @ `{ts}`; "
                f"**AFT interrogation failed** (error **{code}** @ `{et}`)."
            )
            lines.append(gist)
            lines.append(
                "玩家离场/转出额度时 **AFT 问询失败** — 机台可能未正常清账，需查 AFT/Third Http。"
            )
            if (
                latest_any_uid
                and latest_err_uid
                and latest_any_uid != latest_err_uid
            ):
                lines.append(
                    f"Note: error player `{latest_err_uid}` is **not** the last player in the log "
                    f"(`{latest_any_uid}`) — delayed OUT CHECK / AFT response after `leave_game`, "
                    f"not the newer `enter_game` session."
                )
                lines.append(
                    f"说明：error 玩家 **`{latest_err_uid}`** 不是日志末位玩家 **`{latest_any_uid}`** — "
                    f"属前一位玩家离场后 **延迟 OUT CHECK**，不是新进场玩家的问题。"
                )
        else:
            lines.append(
                f"Player `{uid}` — last error **{code}** @ `{et}`; transfer-out credit **{amt}** @ `{ts}`."
            )
    elif amt is not None:
        lines.append(f"Player `{uid}` — last activity; transfer-out / last credit **{amt}** @ `{ts}` (no error in log).")
        lines.append(f"玩家 `{uid}` — 末次额度 **{amt}** @ `{ts}`，日志无 error。")
    else:
        lines.append(f"Player `{uid}` — no parsed error and no credit line in log tail.")
    if latest_any_uid and latest_err_uid and latest_any_uid == latest_err_uid:
        lines.append("Last player in log = last player with error.")
    elif latest_any_uid and latest_err_uid:
        fl_tail = (
            (last_error.get("full_line") or last_error.get("snippet") or "").lower()
            if last_error
            else ""
        )
        aft_mismatch = "aft interrogation faild" in fl_tail or "aft interrogation failed" in fl_tail
        if not aft_mismatch:
            lines.append(
                f"Last player in log `{latest_any_uid}` ≠ last error player `{latest_err_uid}`."
            )
    return "\n".join(lines)


def format_ai_summary_md(
    ai_summary: dict[str, Any] | None,
    *,
    latest_any_uid: str | None,
    latest_err_uid: str | None,
    last_error: dict[str, Any] | None,
    transfer_out: dict[str, Any] | None,
) -> str:
    """Always returns non-empty markdown for the AI section."""
    fallback = _deterministic_log_summary(
        latest_any_uid=latest_any_uid,
        latest_err_uid=latest_err_uid,
        last_error=last_error,
        transfer_out=transfer_out,
    )
    if not use_ai_summary():
        return f"ℹ️ AI off (`CHECKMACHINELOG_USE_AI=0`).\n\n{fallback}"
    if ai_summary is None and not _llm_available():
        return f"ℹ️ LLM not configured (`BOT_CHAT_API_KEY`).\n\n**Analysis:**\n{fallback}"
    if not ai_summary:
        return f"**Analysis:**\n{fallback}"
    if ai_summary.get("_error"):
        return f"⚠️ LLM failed: `{ai_summary['_error']}`\n\n**Analysis:**\n{fallback}"
    parts: list[str] = []
    summary = (ai_summary.get("summary") or "").strip()
    if summary:
        parts.append(f"**Summary:** {summary}")
    if ai_summary.get("_raw") and not summary:
        parts.append(f"**Summary:** {str(ai_summary.get('summary') or '').strip()}")
    for label, key in (
        ("Last player", "last_player_user_id"),
        ("Last error player", "last_error_user_id"),
        ("Error code", "last_error_code"),
        ("Error line", "last_error_line"),
    ):
        v = ai_summary.get(key)
        if key == "last_error_user_id" and latest_err_uid:
            v = latest_err_uid
        elif key == "last_player_user_id" and latest_any_uid:
            v = latest_any_uid
        if v is not None and str(v).strip():
            parts.append(f"- **{label}:** `{v}`")
    llm_err = str((ai_summary or {}).get("last_error_user_id") or "").strip()
    if llm_err and latest_err_uid and llm_err != str(latest_err_uid).strip():
        parts.append(
            f"- ⚠️ LLM misread error player (`{llm_err}`) — using parser **`{latest_err_uid}`** "
            f"(AFT leave errors follow ``leave_game``, not a later ``enter_game``)."
        )
    if not parts:
        parts.append(f"**Analysis:**\n{fallback}")
    elif fallback and not summary:
        parts.append(f"\n**Analysis:**\n{fallback}")
    return "\n".join(parts)


def build_checkmachinelog_lark_card(
    *,
    machine_display: str,
    target_date: date,
    opened_basename: str,
    latest_any_uid: str | None,
    latest_err_uid: str | None,
    last_error: dict[str, Any] | None,
    transfer_out: dict[str, Any] | None,
    last_success: dict[str, Any] | None,
    ai_summary: dict[str, Any] | None,
    header_lines: list[str],
    stuck_credit: bool = False,
) -> dict[str, Any]:
    dstr = target_date.isoformat()
    xfer = transfer_out or {}
    amt = xfer.get("amount")
    xts = xfer.get("time") or "n/a"
    src = xfer.get("source") or "n/a"
    same = bool(latest_any_uid and latest_err_uid and latest_any_uid == latest_err_uid)

    elements: list[dict[str, Any]] = []
    if header_lines:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": _truncate_for_lark("\n".join(header_lines), 800)},
            }
        )
        elements.append({"tag": "hr"})

    meta = (
        f"**Machine:** `{machine_display}` · **Date:** `{dstr}`\n"
        f"**File:** `{opened_basename}`\n\n"
        f"**Last player:** `{latest_any_uid or 'n/a'}`\n"
        f"**Last error player:** `{latest_err_uid or 'n/a'}`\n"
    )
    if latest_any_uid and latest_err_uid:
        meta += (
            "**Same player?** "
            + ("Yes" if same else "No — different users")
            + "\n"
        )
    if amt is not None:
        meta += f"\n**Transfer-out credit:** `{amt}` @ `{xts}` (`{src}`)"
    else:
        meta += "\n**Transfer-out credit:** `n/a`"
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": meta}})
    elements.append({"tag": "hr"})

    if last_error:
        uid = str(last_error.get("user_id") or "n/a")
        code = last_error.get("error_count", "?")
        t = (last_error.get("time") or "").strip() or "n/a"
        ctx = last_error.get("context_lines") or []
        body = "\n".join(ctx) if ctx else (last_error.get("full_line") or "").strip()
        b, a = ctx_before_lines(), ctx_after_lines()
        err_md = (
            f"**Last error** · User `{uid}` · code `{code}` · `{t}`\n"
            f"Context ({b}+1+{a} lines):\n```\n{_truncate_for_lark(body)}\n```"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": err_md}})
        header_tpl = "orange"
        title = "stuck credit — error in log" if stuck_credit else "checkmachinelog — error"
    else:
        succ = last_success or {}
        uid = latest_any_uid or "n/a"
        t = (succ.get("time") or "").strip() or "n/a"
        coin = succ.get("cur_coin")
        coin_s = "n/a" if coin is None else str(coin)
        ctx = succ.get("context_lines") or []
        body = "\n".join(ctx) if ctx else (succ.get("full_line") or "").strip() or "(no successJson)"
        b, a = ctx_before_lines(), ctx_after_lines()
        ok_md = (
            f"**No errors** — last success log · User `{uid}` · cur_coin `{coin_s}` · `{t}`\n"
            f"```\n{_truncate_for_lark(body)}\n```"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": ok_md}})
        header_tpl = "purple" if stuck_credit else "green"
        title = f"stuck credit — {machine_display}" if stuck_credit else "checkmachinelog — OK"

    elements.append({"tag": "hr"})
    ai_md = format_ai_summary_md(
        ai_summary,
        latest_any_uid=latest_any_uid,
        latest_err_uid=latest_err_uid,
        last_error=last_error,
        transfer_out=transfer_out,
    )
    elements.append(
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**AI summary**\n{_truncate_for_lark(ai_md, 1200)}"},
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": header_tpl, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def _fetch_logic_log_body(
    machine_query: str,
    td: date,
    *,
    timeout_ms: int,
    source: str,
    logic_log_basename: str | None = None,
) -> tuple[str, str, list[str], str]:
    """
    Load logic log text (OSS or LogNavigator). Returns
    (log_body, machine_display, header_lines, opened_basename).
    """
    text_parts: list[str] = []
    machine_display = (machine_query or "").strip()
    date_str = td.isoformat()
    timeout_sec = max(30.0, timeout_ms / 1000.0)
    chosen = ""

    if source == "oss":
        same_day = cc.list_oss_logic_log_basenames_for_date(
            machine_query, td, timeout_sec=min(30.0, timeout_sec)
        )
        want = (logic_log_basename or "").strip()

        def _oss_fetch(basename: str) -> str:
            body, oss_parts = cc.fetch_log_via_oss(
                machine_query,
                td,
                timeout_sec=timeout_sec,
                logic_log_basename=basename,
            )
            text_parts.extend(oss_parts)
            return body

        log_body = ""
        if same_day:
            if want and want in same_day:
                chosen = want
                log_body = _oss_fetch(chosen)
            elif len(same_day) >= 2:
                best_fn, best_body, best_ts = "", "", ""
                for fn in same_day:
                    try:
                        body = _oss_fetch(fn)
                    except Exception as e:
                        text_parts.append(f"⚠ Could not fetch logic log {fn}: {e}")
                        continue
                    ts = cc._latest_log_ts_in_body(body)
                    text_parts.append(f"→ scanned {fn}: last activity {ts or 'n/a'}")
                    if best_fn == "" or ts > best_ts:
                        best_fn, best_body, best_ts = fn, body, ts
                if best_fn:
                    chosen, log_body = best_fn, best_body
                else:
                    chosen = f"{date_str}.log" if f"{date_str}.log" in same_day else same_day[0]
                    log_body = _oss_fetch(chosen)
            else:
                chosen = f"{date_str}.log" if f"{date_str}.log" in same_day else same_day[0]
                log_body = _oss_fetch(chosen)
        else:
            chosen = f"{date_str}.log"
            log_body = _oss_fetch(chosen)
        machine_display = cc.resolve_oss_machine_folder(machine_query)
    else:
        log_body, machine_display, nav_parts, nav_meta = cc.fetch_log_via_navigator(
            machine_query,
            td,
            timeout_ms=timeout_ms,
            base=cc.DEFAULT_BASE,
            user=cc.DEFAULT_USER,
            pw=cc.DEFAULT_PASS,
            debug_headed=False,
            logic_log_basename=logic_log_basename,
        )
        text_parts.extend(nav_parts)
        chosen = str(nav_meta.get("opened_logic_log_basename") or f"{date_str}.log")

    return log_body, machine_display, text_parts, chosen


def format_error_context_block(err: dict[str, Any] | None, *, title: str = "Last error") -> str:
    if not err:
        return f"**{title}:** (none)\n"
    uid = str(err.get("user_id") or "n/a")
    code = err.get("error_count", "?")
    t = (err.get("time") or "").strip() or "n/a"
    ctx = err.get("context_lines") or []
    if ctx:
        body = "\n".join(ctx)
    else:
        body = (err.get("full_line") or err.get("snippet") or "").strip() or "(no line)"
    b, a = ctx_before_lines(), ctx_after_lines()
    head = (
        f"**{title}**\n"
        f"- User ID: `{uid}`\n"
        f"- Time: `{t}`\n"
        f"- Error code: `{code}`\n"
        f"- Context ({b} lines above + error + {a} below):\n"
    )
    return f"{head}```\n{body}\n```\n"


def format_report(
    *,
    machine_display: str,
    target_date: date,
    opened_basename: str,
    latest_any_uid: str | None,
    latest_err_uid: str | None,
    last_error: dict[str, Any] | None,
    transfer_out: dict[str, Any] | None,
    last_success: dict[str, Any] | None,
    ai_summary: dict[str, Any] | None,
    header_lines: list[str],
) -> str:
    dstr = target_date.isoformat()
    parts: list[str] = []
    if header_lines:
        parts.append("\n".join(header_lines))
        parts.append("")
    parts.append(f"**Machine:** `{machine_display}`  **Date:** `{dstr}`  **File:** `{opened_basename}`")
    parts.append("")
    parts.append(
        f"**Last player in log (any):** `{latest_any_uid or 'n/a'}`\n"
        f"**Last player with error:** `{latest_err_uid or 'n/a'}`"
    )
    if latest_any_uid and latest_err_uid:
        same = latest_any_uid == latest_err_uid
        parts.append(
            "**Same player?** "
            + ("Yes — last activity and last error refer to the same user." if same else "No — different users.")
        )
    parts.append("")
    xfer = transfer_out or {}
    parts.append(_format_transfer_out_line(xfer, uid=latest_any_uid))
    parts.append("")
    if last_error:
        parts.append(format_error_context_block(last_error))
    else:
        parts.append(format_success_context_block(last_success, uid=latest_any_uid))
    if ai_summary:
        parts.append("**AI summary (tail read):**")
        parts.append(
            format_ai_summary_md(
                ai_summary,
                latest_any_uid=latest_any_uid,
                latest_err_uid=latest_err_uid,
                last_error=last_error,
                transfer_out=transfer_out,
            )
        )
    else:
        parts.append("**AI summary:**")
        parts.append(
            format_ai_summary_md(
                None,
                latest_any_uid=latest_any_uid,
                latest_err_uid=latest_err_uid,
                last_error=last_error,
                transfer_out=transfer_out,
            )
        )
    return "\n".join(parts).strip() + "\n"


def build_third_http_followup(
    *,
    machine_display: str,
    target_date: date,
    latest_err_uid: str | None,
    latest_any_uid: str | None,
    last_error: dict[str, Any] | None,
    transfer_out: dict[str, Any] | None,
    allow_without_error: bool = False,
    raw_lines: list[str] | None = None,
    error_player_row: dict[str, Any] | None = None,
    last_player_row: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Pick player/time/credit for Third Http screenshot (checkcredit /npthirdhttp path)."""
    if not last_error and not latest_err_uid and not allow_without_error:
        return None

    xfer = transfer_out or {}
    err_uid = str(latest_err_uid or "").strip()
    any_uid = str(latest_any_uid or "").strip()
    xfer_uid = str(xfer.get("user_id") or "").strip()
    if not xfer_uid and last_player_row:
        xfer_uid = str(last_player_row.get("user_id") or "").strip()

    is_aft_fail = bool(last_error and _is_aft_interrogation_fail_error(last_error))
    has_xfer = xfer.get("amount") is not None and (xfer.get("time") or "").strip()

    if allow_without_error and not last_error:
        uid = any_uid or xfer_uid
        prow = last_player_row
    elif is_aft_fail:
        uid = str(last_error.get("user_id") or err_uid or any_uid).strip()
        prow = error_player_row
    elif has_xfer:
        # Card transfer-out line — verify THAT player (often last player ≠ error player).
        uid = xfer_uid or any_uid or err_uid
        prow = last_player_row if uid == any_uid else error_player_row
    else:
        uid = err_uid or any_uid
        prow = error_player_row

    if not uid:
        return None

    ts = (xfer.get("time") or "").strip()
    if not ts and last_error:
        ts = (last_error.get("time") or "").strip()
    time_candidates = cc.third_http_time_candidates(
        raw_lines=raw_lines,
        uid=uid,
        last_error=last_error if uid == err_uid or is_aft_fail else None,
        transfer_out=xfer if has_xfer else None,
        player_row=prow,
        primary_time=ts or None,
    )
    if time_candidates:
        ts = time_candidates[0]
    if not ts:
        return None
    credit_val: float | None = None
    amt = xfer.get("amount")
    if amt is not None:
        try:
            credit_val = float(amt)
        except (TypeError, ValueError):
            credit_val = None
    md = (machine_display or "").strip()
    extra_cands = [t for t in time_candidates if t != ts]
    return {
        "user_id": uid,
        "time_short": ts,
        "time_short_candidates": extra_cands,
        "credit_value": credit_val,
        "machine_display": md,
        "target_date_iso": target_date.isoformat(),
        "machine_match_substr": cc.machine_match_substr_from_display(md) or None,
        "third_http_backend": cc._np_log_backend_tag(md),
        "verify_kind": "aft_fail" if is_aft_fail else ("transfer_out" if has_xfer else "error"),
        "error_player_id": err_uid or None,
    }


def run_check_machine_log(
    machine_query: str,
    *,
    target_date: date | None = None,
    timeout_ms: int = 90_000,
    source: str | None = None,
    logic_log_basename: str | None = None,
    skip_ai: bool = False,
    stuck_credit: bool = False,
) -> dict[str, Any]:
    """
    Fetch logic log (checkcredit path), parse errors, enrich ±10 context, optional AI on tail.

    Returns dict with keys: text, machine_display, target_date, opened_basename,
    latest_any_uid, latest_err_uid, last_error, ai_summary, merged_players, log_tail_lines.
    """
    td = target_date or date.today()
    if source is None:
        source = "oss" if cc.checkcredit_use_oss_source() else "navigator"

    log_body, machine_display, header_lines, opened_basename = _fetch_logic_log_body(
        machine_query,
        td,
        timeout_ms=timeout_ms,
        source=source,
        logic_log_basename=logic_log_basename,
    )

    raw_lines = log_body.splitlines()
    parsed = cc.parse_user_blocks_full(log_body)
    merged = cc.merge_players_full(parsed)
    enrich_merged_context(raw_lines, merged)

    le_uid, _ = cc.pick_latest_error_uid(merged)
    la_uid, _ = cc.pick_latest_any_uid(merged)
    last_error = _pick_latest_error(merged)
    last_player_row = _row_for_uid(merged, la_uid)
    err_player_row = _row_for_uid(merged, le_uid)
    transfer_out = _transfer_out_for_report(
        last_player_row,
        last_error,
        latest_err_uid=le_uid,
        raw_lines=raw_lines,
        error_player_row=err_player_row,
    )
    last_success = find_last_success_line(raw_lines, la_uid) if not last_error else None

    tail_lines = tail_log_lines(log_body)
    tail_text = "\n".join(tail_lines)

    ai_summary: dict[str, Any] | None = None
    if not skip_ai:
        ai_summary = ai_summarize_log_tail(
            tail_text,
            machine_display=machine_display,
            target_date=td,
        )

    text = format_report(
        machine_display=machine_display,
        target_date=td,
        opened_basename=opened_basename,
        latest_any_uid=la_uid,
        latest_err_uid=le_uid,
        last_error=last_error,
        transfer_out=transfer_out,
        last_success=last_success,
        ai_summary=ai_summary,
        header_lines=header_lines,
    )
    lark_card = build_checkmachinelog_lark_card(
        machine_display=machine_display,
        target_date=td,
        opened_basename=opened_basename,
        latest_any_uid=la_uid,
        latest_err_uid=le_uid,
        last_error=last_error,
        transfer_out=transfer_out,
        last_success=last_success,
        ai_summary=ai_summary,
        header_lines=header_lines,
        stuck_credit=stuck_credit,
    )

    third_http_followup = build_third_http_followup(
        machine_display=machine_display,
        target_date=td,
        latest_err_uid=le_uid,
        latest_any_uid=la_uid,
        last_error=last_error,
        transfer_out=transfer_out,
        allow_without_error=bool(stuck_credit),
        raw_lines=raw_lines,
        error_player_row=err_player_row,
        last_player_row=last_player_row,
    )

    return {
        "text": text,
        "lark_card": lark_card,
        "third_http_followup": third_http_followup,
        "machine_display": machine_display,
        "target_date": td,
        "opened_basename": opened_basename,
        "latest_any_uid": la_uid,
        "latest_err_uid": le_uid,
        "last_error": last_error,
        "transfer_out": transfer_out,
        "last_success": last_success,
        "ai_summary": ai_summary,
        "merged_players": merged,
        "log_tail_lines": tail_lines,
        "source": source,
        "stuck_credit": bool(stuck_credit),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="checkmachinelog — machine error log (same fetch as checkcredit)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 checkmachinelog.py 2074 --date 2026-06-26\n"
            "  python3 checkmachinelog.py NCH1422 --no-ai\n"
            "  python3 checkmachinelog.py CP0231 --navigator --date 2026-02-05\n"
        ),
    )
    ap.add_argument("machine", help="Machine query (e.g. 2074, NWR2074, NCH1422)")
    ap.add_argument("--date", metavar="YYYY-MM-DD", help="Log date (default: today)")
    ap.add_argument("--timeout-ms", type=int, default=90_000)
    ap.add_argument(
        "--oss",
        action="store_true",
        help="OSS HTTP (default when CHECKCREDIT_USE_OSS not disabled)",
    )
    ap.add_argument("--navigator", action="store_true", help="Force LogNavigator browser")
    ap.add_argument("--logic-file", metavar="BASENAME", help="Specific logic log basename")
    ap.add_argument("--no-ai", action="store_true", help="Skip LLM tail summary")
    args = ap.parse_args(argv)

    td = date.today()
    if args.date:
        try:
            td = datetime.strptime(args.date.strip(), "%Y-%m-%d").date()
        except ValueError:
            print("❌ Invalid --date; use YYYY-MM-DD", file=sys.stderr)
            return 2

    if args.navigator:
        source = "navigator"
    elif args.oss:
        source = "oss"
    else:
        source = "oss" if cc.checkcredit_use_oss_source() else "navigator"

    try:
        out = run_check_machine_log(
            str(args.machine).strip(),
            target_date=td,
            timeout_ms=args.timeout_ms,
            source=source,
            logic_log_basename=(args.logic_file or "").strip() or None,
            skip_ai=bool(args.no_ai),
        )
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    print(out.get("text") or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
