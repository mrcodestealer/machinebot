"""
Readings taken from the EGM operation-window screenshot by the local vision model.

Two of them:

* :func:`detect_jackpot` — does the game screen show a jackpot-sized win? (``/checkcredit``)
* :func:`read_machine_credit` — what does the cabinet's **Machine Credit** field say? (``/showurl``
  matches the recharge Detail amount against it.)

``/checkcredit`` already screenshots the machine's operation window for the card it posts
(machine name, member, credit, and the live game screen below it). The same PNG is handed to the
local vision model — Ollama's OpenAI-compatible endpoint, the one ``chatagent`` already talks to
(``BOT_CHAT_API_BASE`` / ``BOT_CHAT_API_KEY`` / ``BOT_CHAT_MODEL``) — which reads the WIN / CREDIT
counters and any feature banner. A positive verdict makes Duty Bot post a follow-up in the card's
thread so a floor attendant checks the cabinet by hand.

Env:
  ``CHECKCREDIT_JACKPOT_VISION``      ``0`` disables the check (default on).
  ``CHECKCREDIT_JACKPOT_MODEL``       override the model (default: the vision/chat model
                                      ``chatagent`` resolves, i.e. ``BOT_CHAT_VISION_MODEL`` or
                                      ``BOT_CHAT_MODEL``). It must be vision-capable —
                                      ``ollama show <model>`` has to list ``vision``.
  ``CHECKCREDIT_JACKPOT_TIMEOUT``     seconds to wait for the model (default 180).

Never raises: every failure path returns a verdict with ``error`` set and ``jackpot`` False, so a
model that is down or slow can only cost the follow-up message, never the card itself.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

# CLI / subprocess: same convention as checkcredit.py, so BOT_CHAT_* matches Duty Bot.
_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(_ROOT_DIR, ".env"))
except ImportError:
    pass

_PROMPT = (
    "You are inspecting a screenshot of one slot machine's operation window from a casino "
    "back-office tool. The lower half of the image is the live game screen, with counters such "
    "as BET, WIN and CREDIT along the bottom, and sometimes a banner (\"Feature Completed\", a "
    "jackpot celebration, or a line about an amount being paid).\n\n"
    "Decide whether this screen shows a JACKPOT or an unusually large win that a floor "
    "attendant should check by hand.\n\n"
    "Answer jackpot = true when any of these is visible:\n"
    "- a WIN counter far larger than the CREDIT and BET counters "
    "(for example WIN 1010089 with BET 38 and CREDIT 872);\n"
    "- a jackpot / grand / major / minor banner or a celebration overlay;\n"
    "- a line saying an amount was paid, or a hand pay prompt.\n\n"
    "Answer jackpot = false for an ordinary spin, an idle or attract screen, a game with no win "
    "shown, a blank/black screen, or when you cannot read the counters.\n\n"
    "Reply with ONE line of JSON and nothing else:\n"
    '{"jackpot": true or false, "win": "<WIN counter exactly as shown, else empty>", '
    '"credit": "<CREDIT counter, else empty>", "reason": "<at most 15 words>"}'
)


def _truthy(name: str, default: str = "") -> bool:
    return (os.getenv(name) or default).strip().lower() in ("1", "true", "yes", "on")


def jackpot_vision_enabled() -> bool:
    """On unless ``CHECKCREDIT_JACKPOT_VISION`` says otherwise."""
    return _truthy("CHECKCREDIT_JACKPOT_VISION", "1")


def _timeout_sec() -> float:
    try:
        return max(10.0, float((os.getenv("CHECKCREDIT_JACKPOT_TIMEOUT") or "180").strip()))
    except ValueError:
        return 180.0


def _model() -> str:
    explicit = (os.getenv("CHECKCREDIT_JACKPOT_MODEL") or "").strip()
    if explicit:
        return explicit
    try:
        from chatagent import shared_llm_model

        return shared_llm_model(images=True)
    except Exception:
        return (os.getenv("BOT_CHAT_VISION_MODEL") or os.getenv("BOT_CHAT_MODEL") or "").strip()


def _api_base() -> str:
    return (os.getenv("BOT_CHAT_API_BASE") or "https://api.openai.com/v1").strip().rstrip("/")


def _api_key() -> str:
    return (
        os.getenv("BOT_CHAT_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    ).strip()


def _parse_verdict(text: str) -> dict[str, Any]:
    """First JSON object in the reply; a bare yes/no sentence is accepted as a fallback."""
    # Thinking models can wrap the answer in <think>…</think> even with reasoning off.
    raw = re.sub(r"<think>.*?</think>", " ", (text or "").strip(), flags=re.S | re.I).strip()
    obj = _first_json_object(raw)
    if obj is not None:
        return {
            "jackpot": bool(obj.get("jackpot")),
            "win": str(obj.get("win") or "").strip(),
            "credit": str(obj.get("credit") or "").strip(),
            "reason": str(obj.get("reason") or "").strip(),
            "raw": raw[:600],
            "error": "",
        }
    low = raw.lower()
    if low.startswith(("yes", "true", "jackpot")):
        return {"jackpot": True, "win": "", "credit": "", "reason": raw[:120],
                "raw": raw[:600], "error": ""}
    return {"jackpot": False, "win": "", "credit": "", "reason": "",
            "raw": raw[:600], "error": "" if raw else "empty model reply"}


def _ask_about_image(
    prompt: str,
    png_bytes: bytes,
    *,
    max_tokens: int = 300,
) -> tuple[str, str, str]:
    """
    One vision question about one PNG → ``(reply_text, model, error)``.

    Never raises: a model that is down, slow or unconfigured comes back as an ``error`` string,
    so a reading can only cost the extra message, never the command that asked for it.
    """
    if not png_bytes:
        return "", "", "no screenshot bytes"
    model = _model()
    if not model:
        return "", "", "no vision model configured (BOT_CHAT_MODEL)"
    api_key = _api_key()
    if not api_key:
        return "", model, "no API key (BOT_CHAT_API_KEY)"

    b64 = base64.standard_b64encode(png_bytes).decode("ascii")
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            }
        ],
        # Enough for the one-line JSON; reading a counter is not a place for creativity.
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    try:
        from chatagent import enrich_ollama_chat_payload

        enrich_ollama_chat_payload(payload, think=False)
    except Exception:
        pass

    req = urllib.request.Request(
        f"{_api_base()}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout_sec()) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        return "", model, f"HTTP {exc.code}: {detail or exc.reason}"
    except Exception as exc:  # noqa: BLE001 - a reading must never break its caller
        return "", model, f"request failed: {exc!r}"

    choices = body.get("choices") or []
    if not choices:
        return "", model, "no choices in model response"
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):  # some servers return content parts
        content = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return str(content or ""), model, ""


def _first_json_object(text: str) -> dict[str, Any] | None:
    """First ``{...}`` in a reply, with any ``<think>`` wrapper stripped."""
    raw = re.sub(r"<think>.*?</think>", " ", (text or "").strip(), flags=re.S | re.I).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


_CREDIT_PROMPT = (
    "This is a casino back-office operation window for one slot machine. Near the top it lists "
    "fields such as Machine Name, GameType, Member, Machine Credit, Member Status and Machine "
    "Status. Below that is the live game screen, whose bottom bar shows CREDIT, BET and WIN.\n\n"
    "Read two numbers:\n"
    "- machine_credit: the value of the \"Machine Credit\" field in the text area at the top.\n"
    "- screen_credit: the CREDIT counter on the game screen (digits only \u2014 drop currency "
    "symbols and thousands separators).\n\n"
    "Use null for a number you cannot read. Reply with ONE line of JSON and nothing else:\n"
    '{"machine_credit": <number or null>, "screen_credit": <number or null>}'
)


def _as_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(str(val).replace(",", "").replace("\u20b1", "").strip())
    except (TypeError, ValueError):
        return None


def read_machine_credit(png_bytes: bytes, *, machine_display: str = "") -> dict[str, Any]:
    """
    Read the cabinet's credit off its operation window.

    Returns ``{"credit", "machine_credit", "screen_credit", "raw", "error", "model"}`` where
    ``credit`` is the **Machine Credit** field when the model could read it, else the on-screen
    CREDIT counter, else ``None``. ``/showurl`` matches the recharge Detail amount against it.
    """
    out: dict[str, Any] = {
        "credit": None, "machine_credit": None, "screen_credit": None,
        "raw": "", "error": "", "model": "",
    }
    prompt = _CREDIT_PROMPT
    md = (machine_display or "").strip()
    if md:
        prompt = f"{prompt}\n\nThis window belongs to machine {md}."
    text, model, err = _ask_about_image(prompt, png_bytes, max_tokens=200)
    out["model"] = model
    out["raw"] = (text or "")[:600]
    if err:
        out["error"] = err
        return out
    obj = _first_json_object(text)
    if obj is None:
        out["error"] = "model reply was not JSON"
        return out
    out["machine_credit"] = _as_float(obj.get("machine_credit"))
    out["screen_credit"] = _as_float(obj.get("screen_credit"))
    out["credit"] = out["machine_credit"] if out["machine_credit"] is not None else out["screen_credit"]
    if out["credit"] is None:
        out["error"] = "no credit value in the model reply"
    return out


def detect_jackpot(png_bytes: bytes, *, machine_display: str = "") -> dict[str, Any]:
    """
    Ask the vision model whether this operation-window screenshot shows a jackpot.

    Returns ``{"jackpot", "win", "credit", "reason", "raw", "error", "model"}``; ``jackpot`` is
    False whenever anything went wrong (``error`` says what).
    """
    out: dict[str, Any] = {
        "jackpot": False, "win": "", "credit": "", "reason": "",
        "raw": "", "error": "", "model": "",
    }
    prompt = _PROMPT
    md = (machine_display or "").strip()
    if md:
        prompt = f"{prompt}\n\nThis window belongs to machine {md}."
    text, model, err = _ask_about_image(prompt, png_bytes)
    out["model"] = model
    if err:
        out["error"] = err
        return out
    verdict = _parse_verdict(text)
    verdict["model"] = model
    return verdict


def format_jackpot_notice(verdict: dict[str, Any], *, machine_display: str = "") -> str:
    """The follow-up posted in the card's thread when the model says jackpot."""
    lines = ["🎰 **It seems hit jackpot. Kindly check.**"]
    bits: list[str] = []
    md = (machine_display or "").strip()
    if md:
        bits.append(f"machine `{md}`")
    win = str(verdict.get("win") or "").strip()
    if win:
        bits.append(f"WIN `{win}`")
    credit = str(verdict.get("credit") or "").strip()
    if credit:
        bits.append(f"CREDIT `{credit}`")
    reason = str(verdict.get("reason") or "").strip()
    if reason:
        bits.append(reason)
    if bits:
        lines.append("_Screen read by `" + str(verdict.get("model") or "vision model")
                     + "`: " + " · ".join(bits) + "_")
    return "\n".join(lines)


if __name__ == "__main__":  # manual check: python jackpotvision.py <screenshot.png>
    import sys

    try:  # the notice has emoji; a cp1252 console would otherwise raise
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if len(sys.argv) < 2:
        raise SystemExit("usage: python jackpotvision.py <screenshot.png>")
    with open(sys.argv[1], "rb") as fh:
        data = fh.read()
    v = detect_jackpot(data, machine_display=(sys.argv[2] if len(sys.argv) > 2 else ""))
    print(json.dumps(v, ensure_ascii=False, indent=2))
    if v.get("jackpot"):
        print()
        print(format_jackpot_notice(v))
