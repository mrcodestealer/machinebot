"""
Casual chat agent for Duty Bot — two backends:

1. **API LLM** — OpenAI-compatible (Claude, GPT, Ollama, etc.), no local training
2. **Local generative** — fine-tune DistilGPT-2 on your chat pairs (``train-llm``)

The DistilBERT intent classifier has been removed — replies come from the LLM
(API or local generative), with deterministic math/memory helpers on top.

**Backend** (``BOT_CHATAGENT_BACKEND``):
    ``auto`` — API LLM if key set → else local generative if trained
    ``llm`` — API only
    ``local-llm`` — self-trained generative model only (``chatagent_llm_pt/``)

**Self-train generative (自己练 LLM):**
    python chatagent.py train-llm [--epochs 5]
    Saves to ``chatagent_llm_pt/`` (DistilGPT-2 fine-tuned on chat Q→A pairs).
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import random
import re
import sys
import threading
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

_CHBOX_DIR = Path(__file__).resolve().parent
DEFAULT_GEN_MODEL_DIR = _CHBOX_DIR / "chatagent_llm_pt"
GEN_BASE_MODEL = (os.getenv("BOT_CHAT_GEN_BASE_MODEL") or "distilgpt2").strip()
GEN_MAX_SEQ_LEN = int(os.getenv("BOT_CHAT_GEN_MAX_SEQ_LEN", "256"))
GEN_MAX_NEW_TOKENS = int(os.getenv("BOT_CHAT_GEN_MAX_NEW_TOKENS", "100"))
MAX_CHAT_WORDS = int(os.getenv("BOT_CHAT_MAX_WORDS", "400"))


def _llm_timeout_sec() -> float:
    return float(os.getenv("BOT_CHAT_LLM_TIMEOUT", "30"))


def _llm_max_tokens() -> int:
    return int(os.getenv("BOT_CHAT_LLM_MAX_TOKENS", "2048"))


def _llm_max_reply_chars() -> int:
    return int(os.getenv("BOT_CHAT_LLM_MAX_REPLY_CHARS", "4000"))
DEFAULT_LLM_MODEL = (os.getenv("BOT_CHAT_MODEL") or "gpt-4o-mini").strip()
DEFAULT_LLM_BASE = (os.getenv("BOT_CHAT_API_BASE") or "https://api.openai.com/v1").strip().rstrip("/")

_SYSTEM_PROMPT = """You are Duty Bot, a friendly workplace assistant on Lark/Feishu for the OSE team.
Users may casually chat or ask about duty rosters, leave/WFH, holidays, machines, and Jenkins helpers.

For casual conversation: reply naturally, warm, and helpful. Give thorough answers when the user asks for detail; stay brief only if they want something short. Light emoji is fine.
Language policy (strict): only **English**, **Mandarin Chinese (普通话)**, and **Filipino/Tagalog**.
Priority when choosing reply language: **English > Chinese > Filipino**.
- User writes English → reply in English.
- User writes Mandarin → reply in Chinese.
- User writes Filipino/Tagalog → reply in Filipino.
- Mixed or unclear → prefer English, then Chinese, then Filipino.
- Do not reply in Cantonese, other dialects, or any language outside this list.
If they ask for work data you cannot look up in chat, gently suggest `/help` or examples like "who is on fpms duty" / 「今天谁值班」.
Never invent duty names, phone numbers, machine IDs, or confidential information.

Memory: you DO remember this week's messages with each user in this chat — memory keeps the current week, resets every Monday, and survives bot restarts. Recent turns may be included above as context. NEVER claim you "start fresh every conversation", "cannot remember previous chats", or that memory is lost between sessions or days — that is false for you.
Stay professional; avoid politics, religion, and inappropriate topics.

CRITICAL — you CANNOT perform actions. You are only a chat responder. You do NOT run code, deploy, pull git, restart services, trigger Jenkins, book leave/offset, send messages, or execute ANY command. You have no tools and nothing you say makes anything happen.
So you must NEVER claim, imply, or promise that you are doing, triggering, running, starting, scheduling, or "about to" do such an action (do not say "Got it, I'll trigger…", "I'll restart…", "deploying now", "let me pull…", "I'll let you know when it's done", or similar). That would be a lie — nothing runs.
Instead, when the user asks you to DO an action, tell them plainly you can't do it from chat and point them to the exact slash command to run themselves. Known mappings:
- pull code / deploy / update the bot / git pull → `/gitpullrestart` (or `/deploy`)
- restart services / restart webapp+bot → `/restartservices`
- who is on duty (fpms/bi/sre/db/fe/cpms/ote) → e.g. `/fpms`
- leave / WFH / offset → `/leave`, `/wfh`, `/offset`
- check machine / credit → `/checkcredit`, or the machine command
If you are not sure which command fits, say so and suggest `/help` — never guess by pretending to act. Example: "I can't run that from chat, but you can trigger it with `/gitpullrestart`." """

_VISION_EXTRA = (
    "\nWhen the user sends image(s), describe what you see clearly and answer their question. "
    "Use only English, Mandarin Chinese, or Filipino/Tagalog — priority English > Chinese > Filipino."
)
_DEFAULT_VISION_PROMPT = (
    "Describe what you see in this image. If it looks like an error screenshot, log, or dashboard, "
    "summarize the key information. Reply in English, 普通话, or Filipino only (priority: English > Chinese > Filipino)."
)

_torch = None
_generative_singleton: Optional["LocalGenerativeChat"] = None
_generative_failed: bool = False
_llm_failed_logged: bool = False

_COMMANDISH_RE = re.compile(
    r"(?i)\b("
    r"duty|fpms|bi|sre|db|fe|cpms|pms|ote|leave|wfh|holiday|jenkins|"
    r"machine|asset|nch|nwr|winford|checkcredit|offset|reminder|"
    r"wholeave|cctv|sms|credit|deploy|build|ticket|incident|oncall|on-call"
    r")\b|/"
)

@dataclass
class ChatIntentSpec:
    tag: str
    patterns: list[str] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)


def _lazy_torch():
    global _torch
    if _torch is None:
        import torch

        _torch = torch
    return _torch


def is_enabled() -> bool:
    return (os.getenv("BOT_USE_CHATAGENT") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def backend_mode() -> str:
    mode = (os.getenv("BOT_CHATAGENT_BACKEND") or "auto").strip().lower()
    # ``local`` (DistilBERT classifier) was removed → treat it as ``local-llm``.
    if mode == "local":
        return "local-llm"
    if mode in ("llm", "local-llm", "auto"):
        return mode
    return "auto"


def _llm_api_key() -> str:
    return (
        os.getenv("BOT_CHAT_API_KEY")
        or os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()


def _command_llm_model() -> str:
    return (os.getenv("BOT_COMMANDAGENT_LLM_MODEL") or "qwen2.5:0.5b").strip()


def routing_llm_model() -> str:
    """Fast model for all offset NL: offsetleave classify + offsetai agent/tools."""
    override = (os.getenv("BOT_OFFSET_LLM_MODEL") or "").strip()
    if override:
        return override
    return _command_llm_model()


def _use_command_model_only() -> bool:
    return (os.getenv("BOT_USE_COMMAND_MODEL_ONLY") or "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def shared_llm_model(*, images: bool = False, module_override: Optional[str] = None) -> str:
    """Central model picker for all bot LLM callers."""
    if _use_command_model_only():
        return _command_llm_model()
    if (module_override or "").strip():
        return module_override.strip()
    if images:
        vision_model = (os.getenv("BOT_CHAT_VISION_MODEL") or "").strip()
        if vision_model:
            return vision_model
    return (os.getenv("BOT_CHAT_MODEL") or DEFAULT_LLM_MODEL).strip()


def _llm_model() -> str:
    return shared_llm_model()


def _llm_model_for_request(*, images: bool = False) -> str:
    """Text chat uses BOT_CHAT_MODEL; vision needs a multimodal model (e.g. qwen3.5:9b)."""
    return shared_llm_model(images=images)


def _llm_base_url() -> str:
    return (os.getenv("BOT_CHAT_API_BASE") or DEFAULT_LLM_BASE).strip().rstrip("/")


def llm_available() -> bool:
    return bool(_llm_api_key())


def vision_enabled() -> bool:
    return (os.getenv("BOT_CHAT_VISION") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _vision_max_images() -> int:
    try:
        return max(1, int(os.getenv("BOT_CHAT_VISION_MAX_IMAGES", "3")))
    except ValueError:
        return 3


_chat_memory_lock = threading.Lock()
_chat_memory_sessions: dict[str, dict[str, Any]] = {}
_chat_memory_loaded = False


def memory_persist_enabled() -> bool:
    return (os.getenv("BOT_CHAT_MEMORY_PERSIST") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _memory_persist_path() -> Path:
    explicit = (os.getenv("BOT_CHAT_MEMORY_FILE") or "").strip()
    if explicit:
        return Path(explicit)
    return _CHBOX_DIR / ".chat_memory_sessions.json"


def _memory_load_locked() -> None:
    """Load persisted sessions once (caller holds ``_chat_memory_lock``). Keeps only the **current week**."""
    global _chat_memory_loaded
    if _chat_memory_loaded:
        return
    _chat_memory_loaded = True
    if not memory_persist_enabled():
        return
    path = _memory_persist_path()
    try:
        if not path.is_file():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        week = _memory_week_key()
        loaded = 0
        for sk, bucket in raw.items():
            if not isinstance(bucket, dict) or bucket.get("week") != week:
                continue
            msgs = bucket.get("messages")
            if not isinstance(msgs, list):
                continue
            _chat_memory_sessions.setdefault(str(sk), bucket)
            loaded += 1
        if loaded:
            print(
                f"[chatagent] memory restored {loaded} session(s) from {path.name}",
                flush=True,
            )
    except Exception as ex:
        print(f"[chatagent] memory load failed: {ex!r}", flush=True)


def _memory_save_locked() -> None:
    """Atomically persist this week's sessions (caller holds ``_chat_memory_lock``)."""
    if not memory_persist_enabled():
        return
    path = _memory_persist_path()
    week = _memory_week_key()
    keep = {
        sk: b
        for sk, b in _chat_memory_sessions.items()
        if isinstance(b, dict) and b.get("week") == week
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(path)
    except Exception as ex:
        print(f"[chatagent] memory save failed: {ex!r}", flush=True)


def memory_enabled() -> bool:
    return (os.getenv("BOT_CHAT_MEMORY") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _memory_timezone() -> ZoneInfo:
    name = (os.getenv("BOT_CHAT_MEMORY_TZ") or "Asia/Shanghai").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _memory_today_key() -> str:
    return datetime.now(_memory_timezone()).date().isoformat()


def _memory_week_key() -> str:
    """ISO date of **Monday** of the current week — memory keeps the whole week and resets every Monday."""
    today = datetime.now(_memory_timezone()).date()
    return (today - timedelta(days=today.weekday())).isoformat()


def _memory_max_turns() -> int:
    """Turns included in the **LLM prompt** (keep small — prod model context is tiny)."""
    try:
        return max(1, int(os.getenv("BOT_CHAT_MEMORY_MAX_TURNS", "12")))
    except ValueError:
        return 12


def _memory_max_stored_turns() -> int:
    """Turns kept **on disk** for the week (recall/search works over all of these)."""
    try:
        return max(
            _memory_max_turns(),
            int(os.getenv("BOT_CHAT_MEMORY_MAX_STORED_TURNS", "80")),
        )
    except ValueError:
        return 80


def memory_session_key(chat_id: str, sender_id: str) -> str:
    """Per-user memory in a chat (keeps the current week, resets every Monday)."""
    return f"{(chat_id or '').strip()}:{(sender_id or '').strip()}"


def _memory_get_history(session_key: str) -> list[dict[str, str]]:
    """This week's turns as ``{"role", "content"}`` dicts (LLM-safe — extra keys stripped).

    Callers that build an LLM prompt should slice to the last ``_memory_max_turns() * 2``
    entries; recall/search helpers use the full week.
    """
    return [
        {"role": str(m.get("role")), "content": str(m.get("content"))}
        for m in _memory_raw_messages(session_key)
    ]


def _memory_raw_messages(session_key: str) -> list[dict[str, Any]]:
    """This week's stored messages incl. ``ts`` (for day filtering). Copies — safe outside the lock."""
    if not session_key or not memory_enabled():
        return []
    week = _memory_week_key()
    with _chat_memory_lock:
        _memory_load_locked()
        bucket = _chat_memory_sessions.get(session_key)
        if not bucket or bucket.get("week") != week:
            return []
        msgs = bucket.get("messages")
        if not isinstance(msgs, list):
            return []
        return [
            dict(m)
            for m in msgs
            if isinstance(m, dict) and m.get("role") and m.get("content")
        ]


def _memory_append_turn(
    session_key: str, user_text: str, assistant_text: str, *, user_label: str = ""
) -> None:
    if not session_key or not memory_enabled():
        return
    user_part = (user_label or user_text or "").strip()
    assistant_part = (assistant_text or "").strip()
    if not user_part or not assistant_part:
        return
    week = _memory_week_key()
    cap = _memory_max_stored_turns() * 2
    now_ts = datetime.now(_memory_timezone()).timestamp()
    with _chat_memory_lock:
        _memory_load_locked()
        bucket = _chat_memory_sessions.get(session_key)
        if not bucket or bucket.get("week") != week:
            bucket = {"week": week, "messages": []}
            _chat_memory_sessions[session_key] = bucket
        msgs: list[dict[str, Any]] = list(bucket.get("messages") or [])
        msgs.append({"role": "user", "content": user_part[:4000], "ts": now_ts})
        msgs.append({"role": "assistant", "content": assistant_part[:4000], "ts": now_ts})
        if len(msgs) > cap:
            msgs = msgs[-cap:]
        bucket["messages"] = msgs
        _memory_save_locked()
    print(
        f"[chatagent] memory saved session={session_key!r} turns={len(msgs) // 2} week={week}",
        flush=True,
    )


def clear_memory_session(session_key: str) -> bool:
    """Clear one user's memory for this week (optional admin hook)."""
    with _chat_memory_lock:
        _memory_load_locked()
        if session_key in _chat_memory_sessions:
            _chat_memory_sessions.pop(session_key, None)
            _memory_save_locked()
            return True
    return False


def _memory_bucket(session_key: str) -> Optional[dict[str, Any]]:
    if not session_key or not memory_enabled():
        return None
    week = _memory_week_key()
    with _chat_memory_lock:
        _memory_load_locked()
        bucket = _chat_memory_sessions.get(session_key)
        if not bucket or bucket.get("week") != week:
            return None
        return bucket


def _memory_set_recall_pending(
    session_key: str, choices: list[dict[str, str]]
) -> None:
    if not session_key or not choices:
        return
    week = _memory_week_key()
    with _chat_memory_lock:
        _memory_load_locked()
        bucket = _chat_memory_sessions.get(session_key)
        if not bucket or bucket.get("week") != week:
            return
        bucket["recall_pending"] = [
            {"user": c.get("user", ""), "bot": c.get("bot", "")} for c in choices
        ]
        _memory_save_locked()


def _memory_get_recall_pending(session_key: str) -> list[dict[str, str]]:
    bucket = _memory_bucket(session_key)
    if not bucket:
        return []
    pending = bucket.get("recall_pending")
    if not isinstance(pending, list):
        return []
    return [p for p in pending if isinstance(p, dict) and p.get("user")]


def _memory_clear_recall_pending(session_key: str) -> None:
    with _chat_memory_lock:
        _memory_load_locked()
        bucket = _chat_memory_sessions.get(session_key)
        if isinstance(bucket, dict):
            bucket.pop("recall_pending", None)
            _memory_save_locked()


def _sanitize_llm_reply(text: str) -> str:
    out = (text or "").strip()
    out = re.sub(r"@_user_\d+", "", out)
    out = re.sub(r"<at[^>]*>.*?</at>", "", out, flags=re.I)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = out.strip()
    max_chars = _llm_max_reply_chars()
    if max_chars > 0 and len(out) > max_chars:
        out = out[: max_chars - 3].rstrip() + "..."
    return out


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_GARBAGE_LLM_REPLY_RE = re.compile(r"^[\s.…,，。!！?？\-—~·•]+$")
_LARK_MENTION_ONLY_RE = re.compile(r"^@_user_\d+\s*$")
_META_LLM_GARBAGE_RE = re.compile(
    r"(?i)(?:"
    r"^\.\s*(?:they|the user|user)\s+(?:typed|said|wrote|asked)\b|"
    r"^(?:they|the user|user)\s+(?:typed|said|wrote|asked)\b|"
    r"^@_user_\d+\s*$|"
    r"^(?:thinking|draft|option|revised|analyze|determine)\s*:"
    r")"
)
_MATH_FOLLOWUP_RE = re.compile(
    r"(?:"
    r"(?:所以|那|那么)?(?:答案|结果)(?:是什么|呢|多少)?|"
    r"到底是多少|到底等于多少|等于几|"
    r"what(?:'s| is) the answer|so what is (?:it|the answer)|the answer\??"
    r")",
    re.I,
)
_MEMORY_RECALL_RE = re.compile(
    r"(?:"
    r"(?:刚才|刚刚|之前).{0,24}(?:说|问|发|讲|聊)|"
    r"(?:说|问|发).{0,16}(?:什么|啥).{0,12}(?:刚才|刚刚)|"
    r"我(?:刚才|刚刚).{0,16}(?:什么|啥)|"
    r"(?:记得|还记得).{0,20}(?:问|说|吗)|"
    r"刚问(?:了)?什么|"
    r"(?:那个|那件|那一).{0,12}(?:问|说)|"
    r"what did i (?:just )?(?:say|ask|tell)|"
    r"what was (?:my|the) (?:last )?(?:question|message)|"
    r"do you remember.{0,20}(?:ask|said)"
    r")",
    re.I,
)
_MATH_MEMORY_RECALL_RE = re.compile(
    r"(?:"
    r"数学题|算式|计算题|算术题|"
    r"数学.{0,10}(?:题|问)|"
    r"(?:什么|啥|哪道).{0,12}(?:数学|算)|"
    r"math(?:ematics)?\s*(?:question|problem)|"
    r"(?:arithmetic|calculation)\s*(?:question|problem)?"
    r")",
    re.I,
)
_RECALL_CUE_RE = re.compile(
    r"(?:什么|啥|哪道|多少)|(?:刚才|刚刚|之前)|(?:记得|还记得)|刚问|问了",
    re.I,
)
_TODAY_MEMORY_RECALL_RE = re.compile(
    r"(?:"
    r"今天.{0,12}(?:问|说|聊).{0,12}(?:什么|哪些|啥)|"
    r"(?:什么|哪些).{0,8}今天.{0,8}(?:问|说)|"
    r"what (?:did|have) i (?:ask|say).{0,12}today"
    r")",
    re.I,
)
_WEEK_MEMORY_RECALL_RE = re.compile(
    r"(?:"
    r"(?:这|本|这一)\s*(?:周|礼拜|星期).{0,12}(?:问|说|聊).{0,12}(?:什么|哪些|啥)|"
    r"(?:什么|哪些).{0,8}(?:这|本)\s*(?:周|礼拜|星期).{0,8}(?:问|说)|"
    r"what (?:did|have) i (?:ask(?:ed)?|say|said).{0,12}this week"
    r")",
    re.I,
)
# "Do you have memory / can you remember (across chats/days)?" — a question about the bot's
# ABILITY, not a recall of a specific message. Answered deterministically (the small prod LLM
# otherwise replies with the canned "I start fresh every conversation" disclaimer — wrong here).
_MEMORY_CAPABILITY_RE = re.compile(
    r"(?:"
    r"(?:你|妳|u|you).{0,6}(?:有|有没有|是否有).{0,4}(?:记忆|記憶)|"
    r"(?:你|妳).{0,4}(?:能|会|可以|能不能|会不会|可不可以).{0,6}记(?:住|得)|"
    r"记忆.{0,8}(?:多久|几天|多长|怎么|如何)|"
    r"(?:跨|隔)(?:会话|对话|天)|"
    r"(?:新对话|新会话|重启|下次|明天|隔天).{0,10}(?:记得|记住|记忆|忘)|"
    r"(?:do|does|will|would|can|could)\s+(?:you|u)\s+(?:have|keep|retain)\s+(?:a\s+|any\s+)?memor|"
    r"(?:can|will|would|do)\s+(?:you|u)\s+remember\s+(?:me|us|things|stuff|this|anything|"
    r"(?:(?:our|previous|past|earlier|old)\s+)+(?:chats?|conversations?|messages?)|"
    r"(?:across|between)\s+(?:sessions?|chats?|conversations?|days?)|"
    r"tomorrow|next\s+time|after\s+(?:a\s+)?restart)|"
    r"how\s+long\s+(?:do|can|will)\s+(?:you|u)\s+remember"
    r")",
    re.I,
)
_RECALL_TOPIC_PATTERNS = (
    re.compile(r"关于(.+?)(?:的|吗|呢|？|\?|$)"),
    re.compile(r"(?:问|说)(?:过)?(?:的)?(.+?)(?:是什么|是啥|什么|啥|吗|呢|？|\?|$)"),
    re.compile(r"(.+?)(?:那道|那个)(?:题|问题|事)"),
)
_RECALL_NOISE_WORDS_RE = re.compile(
    r"(?:刚才|刚刚|之前|今天|记得|还记得|我问|问你|问我|说过|说的|问的|"
    r"什么|啥|哪道|哪个|那一个|那一|回事|问题|吗|呢|啊|呀|了|的|我|你|还|"
    r"duty\s*bot|bot|math|question)",
    re.I,
)
_TOPIC_STOPWORDS = frozenset(
    {
        "什么",
        "啥",
        "哪个",
        "哪道",
        "怎样",
        "如何",
        "为什么",
        "为何",
        "那个",
        "那件",
        "那一",
        "那个是",
        "那件是",
        "语言是",
        "题是",
        "问题是",
        "回事",
    }
)
_PENDING_PICK_RE = re.compile(
    r"^(?:第)?([1-9]\d?)(?:[号个\.、\)）]|$)|^#([1-9]\d?)$",
    re.I,
)
_MATH_QUESTION_RE = re.compile(
    r"(?i)(?:"
    r"\d[\d,.\s]*[+\-*/×÷]\s*\d|"
    r"\d\s*(?:加|减|乘|除|除以)\s*\d|"
    r"(?:how much|what is|calculate|equals?)\b|"
    r"是多少|等于多少|等于几|多少"
    r")"
)


def _is_garbage_llm_content(text: str) -> bool:
    """True when Ollama/qwen puts a placeholder in content (e.g. '.') instead of a real reply."""
    t = _sanitize_llm_reply(text)
    if not t:
        return True
    if _LARK_MENTION_ONLY_RE.fullmatch(t):
        return True
    if _META_LLM_GARBAGE_RE.search(t):
        return True
    if len(t) <= 3 and _GARBAGE_LLM_REPLY_RE.fullmatch(t):
        return True
    if t.lower() in ("ok", "okay", "k", "嗯", "哦", "啊"):
        return True
    if len(t) <= 24 and re.fullmatch(r"[\s.@_user\d]+", t, re.I):
        return True
    return False


def looks_like_math_followup(text: str) -> bool:
    """Follow-up like 「所以答案是什么」 with no digits — needs memory or prior math turn."""
    raw = (text or "").strip()
    if not raw or re.search(r"\d", raw):
        return False
    if _looks_like_command(raw):
        return False
    return bool(_MATH_FOLLOWUP_RE.search(raw))


def looks_like_memory_recall(text: str) -> bool:
    """User asks what they said earlier — answer from today's chat memory, not LLM guess."""
    raw = _strip_lark_mention_noise(text)
    if not raw or _looks_like_command(raw):
        return False
    return bool(_MEMORY_RECALL_RE.search(raw))


def looks_like_math_memory_recall(text: str) -> bool:
    raw = _strip_lark_mention_noise(text)
    if not raw:
        return False
    if not _MATH_MEMORY_RECALL_RE.search(raw):
        return False
    return bool(_RECALL_CUE_RE.search(raw))


def looks_like_today_memory_recall(text: str) -> bool:
    raw = _strip_lark_mention_noise(text)
    if not raw:
        return False
    if _TODAY_MEMORY_RECALL_RE.search(raw):
        return True
    return bool(
        looks_like_memory_recall(raw)
        and re.search(r"(?:今天|today).{0,12}(?:问|说|聊)", raw, re.I)
    )


def looks_like_week_memory_recall(text: str) -> bool:
    raw = _strip_lark_mention_noise(text)
    if not raw:
        return False
    if _WEEK_MEMORY_RECALL_RE.search(raw):
        return True
    return bool(
        looks_like_memory_recall(raw)
        and re.search(r"(?:这周|本周|这一周|这个?礼拜|this week)", raw, re.I)
    )


def looks_like_memory_capability_question(text: str) -> bool:
    """「你有记忆吗 / 你能记住我们的对话吗 / do you remember across sessions?」— ability, not recall."""
    raw = _strip_lark_mention_noise(text)
    if not raw or _looks_like_command(raw):
        return False
    return bool(_MEMORY_CAPABILITY_RE.search(raw))


def try_memory_capability_reply(
    text: str, *, session_key: Optional[str] = None
) -> Optional[str]:
    """Deterministic, truthful answer about the bot's memory (the small LLM would deny having any)."""
    if not looks_like_memory_capability_question(text):
        return None
    cjk = bool(_CJK_RE.search(text))
    turns = len(_collect_user_turns_from_memory(session_key, limit=10_000)) if session_key else 0
    if cjk:
        lines = [
            "有的～我会记住你在这个聊天里**最近一周**的对话（每周一重置，bot 重启也不会丢）。",
        ]
        if turns:
            lines.append(f"这周我已经记下你的 {turns} 条消息。")
        lines.append(
            "想回顾可以问我「今天我问过什么」「这周我问过什么」，或者直接说关键词"
            "（例如「还记得那道数学题吗」）。Jenkins 更新也可以直接说 **rebuild**，"
            "我会用你上次的参数再跑一次并先让你确认。"
        )
        return "\n".join(lines)
    lines = [
        "Yes — I remember **this week's** conversation with you in this chat "
        "(resets every Monday; it also survives bot restarts).",
    ]
    if turns:
        lines.append(f"So far this week I've noted {turns} message(s) from you.")
    lines.append(
        "You can ask “what did I ask today?” / “what did I ask this week?”, or use a "
        "keyword (e.g. “do you remember the math question?”). For Jenkins you can just "
        "say **rebuild** — I'll reuse your last build parameters and confirm first."
    )
    return "\n".join(lines)


def _latest_math_turn_from_memory(
    session_key: Optional[str],
) -> tuple[str, str]:
    if not session_key:
        return "", ""
    history = _memory_get_history(session_key)
    for idx in range(len(history) - 1, -1, -1):
        msg = history[idx]
        if msg.get("role") != "user":
            continue
        user_content = (msg.get("content") or "").strip()
        if not user_content or not looks_like_math_question(user_content):
            continue
        bot_reply = ""
        if idx + 1 < len(history) and history[idx + 1].get("role") == "assistant":
            bot_reply = (history[idx + 1].get("content") or "").strip()
        return user_content, bot_reply
    return "", ""


def _today_user_messages_from_memory(
    session_key: Optional[str], *, limit: int = 10
) -> list[str]:
    """User messages sent **today** (memory now spans the week, so filter by timestamp)."""
    if not session_key:
        return []
    today = _memory_today_key()
    tz = _memory_timezone()
    out: list[str] = []
    for msg in _memory_raw_messages(session_key):
        if msg.get("role") != "user":
            continue
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        try:
            msg_day = datetime.fromtimestamp(float(msg.get("ts") or 0), tz).date().isoformat()
        except Exception:
            msg_day = ""
        if msg_day == today:
            out.append(content)
    return out[-limit:]


def _collect_user_turns_from_memory(
    session_key: Optional[str], *, limit: int = 12
) -> list[dict[str, str]]:
    if not session_key:
        return []
    history = _memory_get_history(session_key)
    turns: list[dict[str, str]] = []
    idx = 0
    while idx < len(history):
        msg = history[idx]
        if msg.get("role") != "user":
            idx += 1
            continue
        user_content = (msg.get("content") or "").strip()
        bot_content = ""
        if idx + 1 < len(history) and history[idx + 1].get("role") == "assistant":
            bot_content = (history[idx + 1].get("content") or "").strip()
            idx += 2
        else:
            idx += 1
        if user_content:
            turns.append({"user": user_content, "bot": bot_content})
    return turns[-limit:]


def _extract_recall_topic(text: str) -> str:
    raw = _strip_lark_mention_noise(text)
    if _MATH_MEMORY_RECALL_RE.search(raw):
        return "数学题"
    for pat in _RECALL_TOPIC_PATTERNS:
        m = pat.search(raw)
        if not m:
            continue
        topic = (m.group(1) or "").strip(" 的了吗呢？?是")
        topic = re.sub(r"(?:是什么|是啥)$", "", topic).strip()
        if len(topic) >= 2 and topic not in _TOPIC_STOPWORDS:
            return topic
    topic = _RECALL_NOISE_WORDS_RE.sub("", raw)
    topic = re.sub(r"\s+", "", topic).strip(" 的了吗呢？?是")
    if topic in _TOPIC_STOPWORDS or len(topic) < 2:
        return ""
    return topic


def _score_topic_match(user_text: str, topic: str) -> float:
    if not topic or not user_text:
        return 0.0
    user_cf = user_text.casefold()
    topic_cf = topic.casefold()
    if topic_cf in user_cf:
        return 1.0 + min(len(topic_cf), 20) / 100.0
    if _CJK_RE.search(topic):
        chars = [c for c in topic if c.strip()]
        if not chars:
            return 0.0
        hits = sum(1 for c in chars if c in user_text)
        ratio = hits / len(chars)
        if hits >= 1 and len(chars) <= 3:
            return 0.52 + ratio * 0.45
        if ratio >= 0.6 and hits >= 2:
            return 0.55 + ratio * 0.4
    return 0.0


def _search_turns_by_topic(
    session_key: Optional[str], topic: str, *, min_score: float = 0.5
) -> list[dict[str, str]]:
    turns = _collect_user_turns_from_memory(session_key)
    if not topic:
        return turns
    scored: list[tuple[float, dict[str, str]]] = []
    for turn in turns:
        score = _score_topic_match(turn["user"], topic)
        if score >= min_score:
            scored.append((score, turn))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored]


def _preview_turn(user_text: str, *, max_len: int = 72) -> str:
    one = re.sub(r"\s+", " ", (user_text or "").strip())
    if len(one) <= max_len:
        return one
    return one[: max_len - 1] + "…"


def _format_turn_recall(turn: dict[str, str], *, cjk: bool) -> str:
    user_part = turn.get("user") or ""
    bot_part = turn.get("bot") or ""
    if cjk:
        lines = [f"你当时说的是：{user_part}"]
        if bot_part:
            lines.append(f"我当时回复：{bot_part}")
        return "\n".join(lines)
    lines = [f"You said: {user_part}"]
    if bot_part:
        lines.append(f"I replied: {bot_part}")
    return "\n".join(lines)


def _format_recall_clarification(
    session_key: str,
    choices: list[dict[str, str]],
    *,
    cjk: bool,
    intro: str,
) -> str:
    _memory_set_recall_pending(session_key, choices)
    lines = [intro, "请回复编号，或直接说关键词（例如「菲律宾语」「数学题」）："]
    if not cjk:
        lines[1] = "Reply with a number, or a keyword (e.g. Filipino, math):"
    for i, turn in enumerate(choices[:8], 1):
        lines.append(f"{i}. {_preview_turn(turn.get('user', ''))}")
    return "\n".join(lines)


def looks_like_vague_memory_recall(text: str) -> bool:
    raw = _strip_lark_mention_noise(text)
    if not raw:
        return False
    if _is_last_message_recall(raw):
        return False
    return bool(
        re.search(r"(?:那个|那件|那一)", raw, re.I)
        or re.search(r"还记得", raw, re.I)
    )


def _is_last_message_recall(raw: str) -> bool:
    return bool(
        re.search(
            r"(?:刚才|刚刚|上一条).{0,10}(?:说|问).{0,8}(?:什么|啥)(?:了)?"
            r"|what did i (?:just )?(?:say|ask)",
            raw,
            re.I,
        )
    )


def _reply_last_turn(
    session_key: str, *, cjk: bool
) -> Optional[str]:
    last_turns = _collect_user_turns_from_memory(session_key)
    if not last_turns:
        if cjk:
            return "我这周还没记下你之前说过什么。（记忆每周一重置。）"
        return (
            "I don't have an earlier message from you in this week's chat memory "
            "(memory resets every Monday)."
        )
    last = last_turns[-1]
    _memory_clear_recall_pending(session_key)
    if cjk:
        lines = [f"你上一条说的是：{last['user']}"]
        if last.get("bot"):
            lines.append(f"我当时回复：{last['bot']}")
        if len(last_turns) > 1:
            lines.append(
                "（若要找更早的内容，请说关键词，例如「菲律宾语」「数学题」，"
                "或问「今天我问过什么」。）"
            )
        return "\n".join(lines)
    lines = [f"Your last message was: {last['user']}"]
    if last.get("bot"):
        lines.append(f"I replied: {last['bot']}")
    if len(last_turns) > 1:
        lines.append(
            "For earlier messages, say a keyword or ask what you asked today."
        )
    return "\n".join(lines)


def try_resolve_pending_recall(
    text: str, *, session_key: Optional[str] = None
) -> Optional[str]:
    if not session_key:
        return None
    pending = _memory_get_recall_pending(session_key)
    if not pending:
        return None
    raw = _strip_lark_mention_noise(text).strip()
    if not raw or raw.startswith("/"):
        return None

    m = _PENDING_PICK_RE.match(raw)
    if m:
        idx = int((m.group(1) or m.group(2) or "0")) - 1
        if 0 <= idx < len(pending):
            _memory_clear_recall_pending(session_key)
            cjk = bool(_CJK_RE.search(text)) or any(
                _CJK_RE.search(p.get("user") or "") for p in pending
            )
            return _format_turn_recall(pending[idx], cjk=cjk)

    topic = _extract_recall_topic(raw)
    if topic:
        for turn in pending:
            if _score_topic_match(turn.get("user", ""), topic) >= 0.5:
                _memory_clear_recall_pending(session_key)
                return _format_turn_recall(turn, cjk=bool(_CJK_RE.search(text)))

    if looks_like_memory_recall(raw) or looks_like_vague_memory_recall(raw):
        return None

    return None


def try_memory_recall_reply(
    text: str, *, session_key: Optional[str] = None
) -> Optional[str]:
    if not session_key:
        return None
    raw = _strip_lark_mention_noise(text)
    cjk = bool(_CJK_RE.search(text))

    pending_reply = try_resolve_pending_recall(raw, session_key=session_key)
    if pending_reply:
        return pending_reply

    capability_reply = try_memory_capability_reply(text, session_key=session_key)
    if capability_reply:
        return capability_reply

    if looks_like_math_memory_recall(raw):
        math_user, math_bot = _latest_math_turn_from_memory(session_key)
        if not math_user:
            if cjk:
                return "我这周还没记下你问过的数学题，请把算式再发一次。"
            return (
                "I don't have a math question from you in this week's chat memory — "
                "please send the full expression again."
            )
        _memory_clear_recall_pending(session_key)
        return _format_turn_recall(
            {"user": math_user, "bot": math_bot}, cjk=cjk
        )

    week_recall = looks_like_week_memory_recall(raw)
    if week_recall or looks_like_today_memory_recall(raw):
        if week_recall:
            users = [
                t["user"]
                for t in _collect_user_turns_from_memory(session_key, limit=10)
            ]
        else:
            users = _today_user_messages_from_memory(session_key)
        if not users:
            if week_recall:
                if cjk:
                    return "我这周还没记下你问过什么。（记忆每周一重置。）"
                return (
                    "I don't have any messages from you in this week's chat memory "
                    "(memory resets every Monday)."
                )
            if cjk:
                return "我今天还没记下你问过什么。"
            return "I don't have any messages from you in today's chat memory."
        _memory_clear_recall_pending(session_key)
        if cjk:
            lines = ["这周你问过：" if week_recall else "今天你问过："]
            for i, item in enumerate(users, 1):
                lines.append(f"{i}. {_preview_turn(item)}")
            lines.append("若要展开某一条，请回复编号或说关键词。")
            _memory_set_recall_pending(
                session_key,
                [{"user": u, "bot": ""} for u in users],
            )
            return "\n".join(lines)
        lines = ["This week you asked:" if week_recall else "Today you asked:"]
        for i, item in enumerate(users, 1):
            lines.append(f"{i}. {_preview_turn(item)}")
        lines.append("Reply with a number or keyword to expand one.")
        _memory_set_recall_pending(
            session_key,
            [{"user": u, "bot": ""} for u in users],
        )
        return "\n".join(lines)

    topic = _extract_recall_topic(raw)
    wants_recall = (
        looks_like_memory_recall(raw)
        or looks_like_vague_memory_recall(raw)
        or bool(topic and _RECALL_CUE_RE.search(raw))
    )
    if not wants_recall:
        return None

    if _is_last_message_recall(raw) and not topic:
        return _reply_last_turn(session_key, cjk=cjk)

    if topic:
        matches = _search_turns_by_topic(session_key, topic)
        if len(matches) == 1:
            _memory_clear_recall_pending(session_key)
            return _format_turn_recall(matches[0], cjk=cjk)
        if len(matches) > 1:
            intro = (
                f"关于「{topic}」，我这周记下 {len(matches)} 条，你指的是哪一个？"
                if cjk
                else f"I found {len(matches)} messages about “{topic}”. Which one?"
            )
            return _format_recall_clarification(
                session_key, matches, cjk=cjk, intro=intro
            )
        all_turns = _collect_user_turns_from_memory(session_key)
        if not all_turns:
            if cjk:
                return "我这周还没记下你问过什么。"
            return "I don't have earlier messages in this week's memory."
        intro = (
            f"我没找到关于「{topic}」的明确记录。你这周问过这些："
            if cjk
            else f"I couldn't find “{topic}”. This week you asked:"
        )
        return _format_recall_clarification(
            session_key, list(reversed(all_turns)), cjk=cjk, intro=intro
        )

    if looks_like_vague_memory_recall(raw):
        recent = list(reversed(_collect_user_turns_from_memory(session_key)))
        if not recent:
            if cjk:
                return "我这周还没记下你之前说过什么。"
            return "I don't have earlier messages in this week's memory."
        if len(recent) == 1:
            _memory_clear_recall_pending(session_key)
            return _format_turn_recall(recent[0], cjk=cjk)
        intro = (
            "我不太确定你指的是哪一条。你这周问过这些："
            if cjk
            else "I'm not sure which message you mean. This week you asked:"
        )
        return _format_recall_clarification(session_key, recent, cjk=cjk, intro=intro)

    return _reply_last_turn(session_key, cjk=cjk)


def _math_source_from_memory(session_key: Optional[str]) -> str:
    user, _ = _latest_math_turn_from_memory(session_key)
    return user


def resolve_math_from_context(text: str, *, session_key: Optional[str] = None) -> Optional[str]:
    """Deterministic math for the current message or a prior math turn in this week's memory."""
    raw = (text or "").strip()
    direct = try_math_reply(raw)
    if direct:
        return direct
    if not looks_like_math_followup(raw):
        return None
    prior = _math_source_from_memory(session_key)
    if not prior:
        return None
    return try_math_reply(prior)


def remember_chat_turn(
    session_key: Optional[str], user_text: str, assistant_text: str
) -> None:
    _save_memory_turn(session_key, user_text, assistant_text)


def has_pending_recall(session_key: Optional[str]) -> bool:
    return bool(_memory_get_recall_pending(session_key or ""))


# Card callback key for the recall-clarification card buttons (handled in main.py).
MEMORY_RECALL_PICK_KEY = "mem_pick"


def build_recall_choice_card(
    session_key: Optional[str], reply_text: str
) -> Optional[dict]:
    """
    Lark 卡片 JSON 2.0 for a pending recall clarification: the numbered list as markdown
    plus one tap-button per choice (``{"k": "mem_pick", "i": "<n>"}``). Returns ``None``
    when there is nothing pending, so callers fall back to the plain-text reply.
    """
    pending = _memory_get_recall_pending(session_key or "")
    if not pending or not (reply_text or "").strip():
        return None
    cjk = any(_CJK_RE.search(p.get("user") or "") for p in pending) or bool(
        _CJK_RE.search(reply_text)
    )
    buttons: list[dict] = []
    for i, _turn in enumerate(pending[:8], 1):
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": str(i)},
                "type": "primary" if i == 1 else "default",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {"k": MEMORY_RECALL_PICK_KEY, "i": str(i)},
                    }
                ],
                "element_id": f"mem_pick_{i}"[:20],
            }
        )
    columns = [
        {
            "tag": "column",
            "width": "auto",
            "weight": 1,
            "vertical_align": "top",
            "elements": [b],
        }
        for b in buttons
    ]
    note = "点一下编号即可展开那条记录。" if cjk else "Tap a number to expand that message."
    title = "你指的是哪一条？" if cjk else "Which message did you mean?"
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": reply_text}},
                {
                    "tag": "column_set",
                    "flex_mode": "flow",
                    "background_style": "default",
                    "horizontal_spacing": "8px",
                    "columns": columns,
                },
                {"tag": "div", "text": {"tag": "lark_md", "content": note}},
            ]
        },
    }


def resolve_recall_pick(
    chat_id: str, sender_ids: list[str], index: int
) -> Optional[str]:
    """
    Resolve a recall-clarification card tap: try each candidate sender id (open_id /
    union_id) to find the pending list, expand choice ``index`` (1-based) and clear it.
    """
    for sid in sender_ids:
        sid = (sid or "").strip()
        if not sid:
            continue
        session_key = memory_session_key(chat_id, sid)
        pending = _memory_get_recall_pending(session_key)
        if not pending:
            continue
        if not (1 <= index <= len(pending)):
            return None
        _memory_clear_recall_pending(session_key)
        turn = pending[index - 1]
        cjk = bool(
            _CJK_RE.search(turn.get("user") or "") or _CJK_RE.search(turn.get("bot") or "")
        )
        return _format_turn_recall(turn, cjk=cjk)
    return None


def _save_memory_turn(
    session_key: Optional[str],
    user_text: str,
    assistant_text: str,
    *,
    user_label: str = "",
) -> None:
    if not session_key or not memory_enabled():
        return
    if _is_garbage_llm_content(assistant_text):
        return
    if user_label:
        _memory_append_turn(session_key, user_text, assistant_text, user_label=user_label)
    else:
        _memory_append_turn(session_key, user_text, assistant_text)


_DATETIME_LIKE_RE = re.compile(
    r"\d{4}\s*[/-]\s*\d{1,2}\s*[/-]\s*\d{1,2}"  # 2026/01/01 or 2026-06-03
    r"|\d{1,2}\s*[/-]\s*\d{1,2}\s*[/-]\s*\d{2,4}"  # 01/01/2026
    r"|\d{1,2}:\d{2}(?::\d{2})?"  # 00:00 / 00:00:00
)


def _looks_like_datetime_blob(text: str) -> bool:
    """Date/time strings (records, ranges, timestamps) are NOT arithmetic."""
    return bool(_DATETIME_LIKE_RE.search(text or ""))


def looks_like_math_question(text: str) -> bool:
    """Simple arithmetic / 除法 style questions (not machine-id lookups)."""
    raw = _normalize_math_operators(_strip_lark_mention_noise(text))
    if not raw or not re.search(r"\d", raw):
        return False
    # A date range / timestamp ("2026/01/01 00:00:00 - 2026/06/03") superficially
    # looks like arithmetic (/, -, :) — never treat such reports as a calculation.
    if _looks_like_datetime_blob(raw):
        return False
    return bool(_MATH_QUESTION_RE.search(raw))


def _strip_lark_mention_noise(text: str) -> str:
    """Remove Lark @mention placeholders so math parsing sees only the expression."""
    t = (text or "").strip()
    t = re.sub(r"@_user_\d+", "", t)
    t = re.sub(r"@\S+", "", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"(?i)\bduty\s+bot\b", "", t)
    t = re.sub(r"(?i)^bot\s+", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _normalize_math_operators(text: str) -> str:
    t = (text or "").strip()
    for src, dst in (
        ("＋", "+"),
        ("－", "-"),
        ("×", "*"),
        ("÷", "/"),
        ("／", "/"),
    ):
        t = t.replace(src, dst)
    return t


def math_parse_failure_message(user_text: str) -> str:
    return _math_parse_failure_message(user_text)


def _math_parse_failure_message(user_text: str) -> str:
    if _CJK_RE.search(user_text):
        return "这个算式我没解析成功，请写成类似 `123 + 456 除 5 是多少`。"
    return "I couldn't parse that calculation — try e.g. `123 + 456 / 5`."


def _normalize_math_expression(text: str) -> str:
    t = _normalize_math_operators(_strip_lark_mention_noise(text))
    t = re.sub(r"[,，]", "", t)
    t = re.sub(
        r"(?i)(?:是多少|等于多少|等于几|等于|how much is|what is|calculate|equals?).*$",
        "",
        t,
    ).strip()
    div_tail = re.match(
        r"^(.*?)(?:除以|除|/)\s*(\d+(?:\.\d+)?)\s*$",
        t,
    )
    if div_tail:
        left, divisor = div_tail.group(1).strip(), div_tail.group(2)
        left = _apply_chinese_binary_ops(left)
        if re.search(r"[+\-]", left) and not left.startswith("("):
            left = f"({left})"
        return f"{left} / {divisor}"
    t = _apply_chinese_binary_ops(t)
    t = re.sub(r"[\u4e00-\u9fff]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _apply_chinese_binary_ops(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"加", "+", t)
    t = re.sub(r"减", "-", t)
    t = re.sub(r"乘|×", "*", t)
    return t


def _safe_eval_arithmetic(expr: str) -> Optional[float]:
    expr = (expr or "").strip()
    if not expr or not re.fullmatch(r"[\d+\-*/().\s]+", expr):
        return None
    try:
        result = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 — digits/operators only
    except Exception:
        return None
    if isinstance(result, bool):
        return None
    if isinstance(result, (int, float)):
        return float(result)
    return None


def _format_math_result(value: float, user_text: str) -> str:
    if abs(value - round(value)) < 1e-9:
        num = f"{int(round(value)):,}"
    else:
        num = f"{value:,.1f}".rstrip("0").rstrip(".")
        if "." not in num:
            num = f"{int(round(value)):,}"
    if _CJK_RE.search(user_text):
        return f"结果是 {num}。"
    if re.search(r"(?i)\b(ano|magkano|kumusta|salamat|tagalog)\b", user_text):
        return f"Ang sagot ay {num}."
    return f"The answer is {num}."


def try_math_reply(text: str) -> Optional[str]:
    """Deterministic fallback for simple + − × ÷ questions when the LLM fails."""
    raw = _strip_lark_mention_noise(text)
    if not looks_like_math_question(raw):
        return None
    expr = _normalize_math_expression(raw)
    value = _safe_eval_arithmetic(expr)
    if value is None:
        return None
    return _format_math_result(value, raw)


def _is_ollama_base() -> bool:
    base = _llm_base_url().lower()
    return "11434" in base or "ollama" in base


def enrich_ollama_chat_payload(payload: dict, *, think: Optional[bool] = None) -> dict:
    """Add Ollama fields (``think``, ``keep_alive``) when ``BOT_CHAT_API_BASE`` points at Ollama."""
    if not _is_ollama_base():
        return payload
    if think is None:
        think = (os.getenv("BOT_CHAT_LLM_THINK") or "false").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    payload["think"] = bool(think)
    # Ollama's /v1 (OpenAI-compat) endpoint IGNORES "think" — it only honors
    # "reasoning_effort". Without this, thinking models (qwen3.x) reason at full
    # effort on every reply (hundreds of hidden tokens -> very slow answers).
    payload["reasoning_effort"] = (
        (os.getenv("BOT_CHAT_OLLAMA_REASONING_EFFORT") or "").strip().lower()
        or ("medium" if think else "none")
    )
    keep_alive = (os.getenv("BOT_CHAT_OLLAMA_KEEP_ALIVE") or "-1").strip()
    if keep_alive.lower() not in ("0", "off", "false", "no"):
        try:
            payload["keep_alive"] = int(keep_alive)
        except ValueError:
            payload["keep_alive"] = keep_alive
    return payload


def _draft_from_thinking_trace(text: str) -> str:
    """Pull a reply draft from Ollama/qwen thinking traces when content is empty."""
    skip = re.compile(
        r"^(thinking|draft|option|revised|analyze|determine|select|user|sentence)\b",
        re.I,
    )
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if re.search(r"=\s*[\d,.]+", ln) and len(ln) >= 8 and not skip.search(ln):
            return ln
        if re.search(r"结果是|the answer is|ang sagot", ln, re.I) and not skip.search(ln):
            return ln
    quotes = re.findall(r'["\u201c\u2018](.{8,240}?)["\u201d\u2019]', text, re.I)
    for q in reversed(quotes):
        q = q.strip()
        if q and not skip.search(q):
            return q
    for label in ("draft", "reply", "response", "final answer", "final"):
        m = re.search(rf"(?im)^{re.escape(label)}\s*:\s*(.+)$", text)
        if m:
            line = m.group(1).strip().strip("\"'")
            if len(line) >= 8 and not skip.search(line):
                return line
    return ""


def _extract_from_reasoning(reasoning: str) -> str:
    """Best-effort answer from qwen3.5 / Ollama ``reasoning`` when ``content`` is empty or '.'."""
    text = (reasoning or "").strip()
    if not text:
        return ""
    draft = _draft_from_thinking_trace(text)
    if draft:
        return draft
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if _is_garbage_llm_content(ln):
            continue
        if len(ln) >= 12 and not ln.lower().startswith(("thinking", "analyze", "determine")):
            return ln
    return text if len(text) >= 12 else ""


def _text_from_llm_message(message: dict) -> str:
    """OpenAI-compatible message; Ollama thinking models may leave content empty."""
    content = (message.get("content") or "").strip()
    reasoning = (message.get("reasoning") or "").strip()
    if content and not _is_garbage_llm_content(content):
        return content
    if reasoning:
        extracted = _extract_from_reasoning(reasoning)
        if extracted and not _is_garbage_llm_content(extracted):
            return extracted
    return content if content and not _is_garbage_llm_content(content) else ""


def _log_llm_failure(detail: str, *, user_text: str = "") -> None:
    """Log LLM failures; always log short chitchat (e.g. hi) for easier local debugging."""
    global _llm_failed_logged
    short = len((user_text or "").split()) <= 3
    if short or not _llm_failed_logged:
        print(f"⚠️ Chat LLM: {detail}", flush=True)
        if not short:
            _llm_failed_logged = True


def llm_notice_reply(
    facts: str,
    *,
    user_text: str = "",
    must_contain: tuple[str, ...] = (),
) -> Optional[str]:
    """
    One-shot LLM rewrite of a system NOTICE into a short natural chat reply.

    Used for bot status/fallback messages so they read like a person instead of a
    canned template. Returns ``None`` when the LLM is off/unavailable, the reply is
    garbage, or it dropped a required token from ``must_contain`` — the caller keeps
    its static text as the safety net. No memory, no history, no action claims.
    """
    api_key = _llm_api_key()
    if not api_key or not (facts or "").strip():
        return None
    system = (
        "You are Duty Bot, a friendly workplace assistant on Lark for the OSE team.\n"
        "The user just sent a message. Tell them the situation below in your own words — "
        "short and friendly, 2-4 sentences, same language as the user (English, Mandarin "
        "Chinese, or Filipino; default English).\n"
        "Keep every slash command and every word wrapped in backticks EXACTLY as written.\n"
        "Do not invent details, promise actions, or claim anything was performed.\n"
        "Do not repeat these instructions or use headings — output only the reply text."
    )
    user = (
        f"Situation to explain to the user:\n{facts.strip()}\n\n"
        f"The user's message was:\n{(user_text or '').strip()[:600] or '(not available)'}\n\n"
        "Now write your reply to the user:"
    )
    payload: dict[str, Any] = {
        "model": _llm_model_for_request(images=False),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 300,
        "temperature": 0.4,
    }
    if _is_ollama_base():
        enrich_ollama_chat_payload(payload)
    req = urllib.request.Request(
        f"{_llm_base_url()}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_llm_timeout_sec()) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        message = ((body.get("choices") or [{}])[0].get("message") or {})
        reply = _sanitize_llm_reply(_text_from_llm_message(message))
        if not reply or _is_garbage_llm_content(reply):
            return None
        low = reply.casefold()
        # Small models fail this task in predictable ways; each is worse than the
        # caller's static text, so reject and fall back:
        #   1. echoing the prompt scaffolding back verbatim
        #   2. claiming to perform the action themselves ("I'll cancel…")
        #   3. inventing slash commands that were never in the facts
        #   4. rambling walls of text
        for echo in (
            "situation to explain",
            "the user's message was",
            "user's message",
            "now write your reply",
            "notice:",
            "user message",
            "bot response",
        ):
            if echo in low:
                print(
                    f"[chatagent] llm_notice_reply echoed prompt ({echo!r}) — using static text",
                    flush=True,
                )
                return None
        if re.search(r"(?i)\b(i'?ll|i will|let me|i am going to|i'?m going to)\b", reply):
            print(
                "[chatagent] llm_notice_reply claimed an action — using static text",
                flush=True,
            )
            return None
        facts_low = facts.casefold()
        for span in re.findall(r"`+([^`\n]+)`+", reply):
            if span.strip() and span.strip().casefold() not in facts_low:
                print(
                    f"[chatagent] llm_notice_reply invented command {span!r} — using static text",
                    flush=True,
                )
                return None
        if len(reply) > 700:
            print(
                f"[chatagent] llm_notice_reply too long ({len(reply)} chars) — using static text",
                flush=True,
            )
            return None
        for token in must_contain:
            if token.casefold() not in low:
                print(
                    f"[chatagent] llm_notice_reply dropped required {token!r} — using static text",
                    flush=True,
                )
                return None
        return reply
    except Exception as exc:
        print(f"[chatagent] llm_notice_reply failed: {exc!r}", flush=True)
        return None


def _llm_chat(
    user_text: str,
    *,
    images: Optional[list[tuple[str, bytes]]] = None,
    session_key: Optional[str] = None,
) -> Optional[str]:
    """Call OpenAI-compatible chat/completions. Returns None on failure."""
    import base64

    if not images:
        math_first = resolve_math_from_context(user_text, session_key=session_key)
        if math_first:
            if session_key and memory_enabled():
                _save_memory_turn(session_key, user_text, math_first)
            return math_first
        if looks_like_math_question(user_text):
            return None

    api_key = _llm_api_key()
    if not api_key:
        _log_llm_failure("no API key (BOT_CHAT_API_KEY / OPENAI_API_KEY)", user_text=user_text)
        return None
    url = f"{_llm_base_url()}/chat/completions"
    system_prompt = _SYSTEM_PROMPT + (_VISION_EXTRA if images else "")
    # PERF: keep the system message byte-identical on EVERY turn. Ollama caches
    # the prompt KV by prefix; any change to this message forces the CPU to
    # re-read ~700 tokens (~10s). Variable content (codeassist ctx) goes into a
    # separate message near the end instead.
    if not images and memory_enabled():
        system_prompt += (
            "\nEarlier messages in this chat (kept for the current week) may be included "
            "below for context. Do not invent facts from memory — duty data still "
            "needs slash commands."
        )
    code_ctx = ""
    try:
        import codeassist as _codeassist

        if _codeassist.is_enabled() and not images:
            code_ctx = _codeassist.context_for_llm(user_text) or ""
    except Exception as _code_err:
        print(f"[chatagent] codeassist skipped: {_code_err!r}", flush=True)
    history: list[dict[str, str]] = []
    if session_key and memory_enabled() and not images:
        # Full week is stored on disk; only the most recent turns go to the LLM
        # (prod model context is tiny).
        history = _memory_get_history(session_key)[-(_memory_max_turns() * 2):]
    if images:
        parts: list[dict] = [
            {
                "type": "text",
                "text": (user_text or "").strip() or _DEFAULT_VISION_PROMPT,
            }
        ]
        for mime, data in images[: _vision_max_images()]:
            b64 = base64.standard_b64encode(data).decode("ascii")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                }
            )
        user_message: dict = {"role": "user", "content": parts}
    else:
        user_message = {"role": "user", "content": user_text}
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    if code_ctx:
        # Placed AFTER the static prefix + history so it never invalidates the
        # cached prompt prefix (it changes with every user message).
        messages.append({"role": "system", "content": code_ctx})
    messages.append(user_message)
    payload = {
        "model": _llm_model_for_request(images=bool(images)),
        "messages": messages,
        "max_tokens": _llm_max_tokens(),
        "temperature": float(os.getenv("BOT_CHAT_LLM_TEMPERATURE", "0.75")),
    }
    if _is_ollama_base():
        enrich_ollama_chat_payload(payload)
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
        with urllib.request.urlopen(req, timeout=_llm_timeout_sec()) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        choices = body.get("choices") or []
        if not choices:
            _log_llm_failure(f"no choices in response for {user_text!r}", user_text=user_text)
            return None
        message = choices[0].get("message") or {}
        extracted = _text_from_llm_message(message)
        reply = _sanitize_llm_reply(extracted)
        if reply and _is_garbage_llm_content(reply):
            reply = ""
        math_fb = resolve_math_from_context(user_text, session_key=session_key)
        if math_fb:
            reply = math_fb
        elif looks_like_math_question(user_text):
            reply = ""
        if not reply:
            content = (message.get("content") or "").strip()
            reasoning = (message.get("reasoning") or "").strip()
            _log_llm_failure(
                f"empty reply for {user_text!r} "
                f"(content_len={len(content)} reasoning_len={len(reasoning)} "
                f"extracted_len={len(extracted)})",
                user_text=user_text,
            )
        if reply and session_key and memory_enabled():
            if images:
                label = (user_text or "").strip() or "[image]"
                _save_memory_turn(
                    session_key, user_text, reply, user_label=f"{label} [image]"
                )
            else:
                _save_memory_turn(session_key, user_text, reply)
        return reply or None
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        _log_llm_failure(f"HTTP {exc.code}: {err_body or exc.reason}", user_text=user_text)
        return None
    except Exception as exc:
        _log_llm_failure(f"request failed: {exc!r}", user_text=user_text)
        return None


def gen_model_dir() -> Path:
    explicit = (os.getenv("BOT_CHATAGENT_LLM_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    return DEFAULT_GEN_MODEL_DIR


def generative_model_ready() -> bool:
    path = gen_model_dir()
    return (path / "config.json").is_file()


def _gen_training_prompt(user: str, bot: str) -> str:
    return f"User: {user.strip()}\nBot: {bot.strip()}\n"


def _gen_inference_prompt(user: str) -> str:
    return f"User: {user.strip()}\nBot: "


def build_generative_training_texts(intents: list[ChatIntentSpec]) -> list[str]:
    texts: list[str] = []
    seen: set[str] = set()
    for spec in intents:
        for pat in spec.patterns:
            for resp in spec.responses:
                for user in (pat, pat.lower()):
                    line = _gen_training_prompt(user, resp)
                    if line not in seen:
                        seen.add(line)
                        texts.append(line)
    return texts


class LocalGenerativeChat:
    """Small locally fine-tuned causal LM (e.g. DistilGPT-2)."""

    def __init__(self, model_path: Path):
        from transformers import AutoModelForCausalLM, AutoTokenizer

        torch = _lazy_torch()
        path = str(model_path)
        local = {"local_files_only": True}
        self.tokenizer = AutoTokenizer.from_pretrained(path, **local)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model = AutoModelForCausalLM.from_pretrained(path, **local)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model = self.model.to(self.device)
        self.model.eval()

    def reply(self, user_text: str) -> Optional[str]:
        torch = _lazy_torch()
        prompt = _gen_inference_prompt(user_text)
        inputs = self.tokenizer(
            prompt,
            return_tensors="pt",
            truncation=True,
            max_length=GEN_MAX_SEQ_LEN,
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.no_grad():
            out = self.model.generate(
                **inputs,
                max_new_tokens=GEN_MAX_NEW_TOKENS,
                do_sample=True,
                temperature=float(os.getenv("BOT_CHAT_GEN_TEMPERATURE", "0.85")),
                top_p=float(os.getenv("BOT_CHAT_GEN_TOP_P", "0.92")),
                pad_token_id=self.tokenizer.pad_token_id,
                eos_token_id=self.tokenizer.eos_token_id,
            )
        full = self.tokenizer.decode(out[0], skip_special_tokens=True)
        if "Bot: " not in full:
            return None
        reply = full.split("Bot: ", 1)[1]
        if "User:" in reply:
            reply = reply.split("User:", 1)[0]
        reply = _sanitize_llm_reply(reply.strip())
        return reply or None


def _get_generative() -> Optional[LocalGenerativeChat]:
    global _generative_singleton, _generative_failed
    if _generative_singleton is not None:
        return _generative_singleton
    if _generative_failed:
        return None
    path = gen_model_dir()
    if not (path / "config.json").is_file():
        return None
    try:
        _generative_singleton = LocalGenerativeChat(path)
        print(f"✅ Local generative chat loaded from {path}", flush=True)
        return _generative_singleton
    except Exception as exc:
        print(f"⚠️ Local generative chat load failed: {exc!r}", flush=True)
        traceback.print_exc()
        _generative_failed = True
        return None


def _local_generative_reply(text: str) -> Optional[str]:
    gen = _get_generative()
    if gen is None:
        return None
    try:
        return gen.reply(text)
    except Exception as exc:
        print(f"⚠️ Local generative reply error: {exc!r}", flush=True)
        return None


def _chat_intent(tag: str, responses: list[str], *pattern_groups: str) -> ChatIntentSpec:
    pats: list[str] = []
    for g in pattern_groups:
        pats.extend(s.strip() for s in g.split("|") if s.strip())
    return ChatIntentSpec(tag=tag, patterns=list(dict.fromkeys(pats)), responses=responses)


def build_chat_catalog() -> list[ChatIntentSpec]:
    intents: list[ChatIntentSpec] = []

    intents.append(
        _chat_intent(
            "chat_greeting",
            [
                "Hi! 👋 I'm Duty Bot — ask me about duty, leave, machines, or say `/help`.",
                "Hello! How can I help you today?",
                "Hey there! 👋 Work questions welcome — e.g. “who is on fpms duty”.",
            ],
            "hi|hello|hey|hiya|yo|howdy|greetings|good day|"
            "你好|您好|嗨|哈喽|哈啰|早上好|下午好|晚上好",
        )
    )

    intents.append(
        _chat_intent(
            "chat_how_are_you",
            [
                "I'm doing great, thanks for asking! 🤖 What can I help you with?",
                "All good here — ready when you need duty info or machine lookup.",
                "Pretty good! Ask me anything work-related or just say hi anytime.",
            ],
            "how are you|how r u|how are u|how is it going|how are things|"
            "how you doing|how are you doing|how are you doing today|"
            "hey how are you|hey how are you doing|what's up|whats up|sup|wassup|you good|"
            "你好吗|怎么样|还好吗|最近怎样",
        )
    )

    intents.append(
        _chat_intent(
            "chat_thanks",
            [
                "You're welcome! 😊",
                "Anytime — happy to help!",
                "No problem at all!",
            ],
            "thanks|thank you|thanks so much|thank you so much|thx|ty|cheers|"
            "much appreciated|appreciate it|"
            "谢谢|多谢|感谢|辛苦了",
        )
    )

    intents.append(
        _chat_intent(
            "chat_goodbye",
            [
                "Bye! 👋 Ping me anytime you need duty info.",
                "See you later!",
                "Take care — I'll be here when you need me.",
            ],
            "bye|goodbye|see you|see ya|cya|later|talk later|catch you later|"
            "good night|gn|night night|再见|拜拜|晚安",
        )
    )

    intents.append(
        _chat_intent(
            "chat_who_are_you",
            [
                "I'm **Duty Bot** 🤖 — department duty, leave/WFH, holidays, machines, Jenkins helpers. Try `/help`.",
                "Duty Bot at your service! I understand English for both chat and work commands (with AI on).",
            ],
            "who are you|what are you|what can you do|are you a bot|are you real|"
            "tell me about yourself|introduce yourself|"
            "你是谁|你是什么|你能做什么",
        )
    )

    intents.append(
        _chat_intent(
            "chat_compliment",
            [
                "Thanks! 😄 That's kind of you.",
                "Aw, thank you — glad I could help!",
                "You're too kind! Let me know if you need anything else.",
            ],
            "nice|cool|awesome|great job|well done|good bot|you rock|love you bot|"
            "you're the best|amazing|fantastic|厉害|不错|棒极了",
        )
    )

    intents.append(
        _chat_intent(
            "chat_laugh",
            [
                "😄 Glad something's funny!",
                "Haha — need anything else?",
                "LOL — I'm here if you need duty stuff too.",
            ],
            "haha|hahaha|lol|lmao|rofl|hehe|funny|that's funny|so funny",
        )
    )

    intents.append(
        _chat_intent(
            "chat_sorry",
            [
                "No worries at all!",
                "It's okay — how can I help?",
                "All good! Don't worry about it.",
            ],
            "sorry|my bad|apologies|didn't mean to|oops|excuse me|对不起|抱歉",
        )
    )

    intents.append(
        _chat_intent(
            "chat_ack",
            [
                "Got it 👍",
                "Okay!",
                "Sure thing.",
                "Alright — shout if you need me.",
            ],
            "ok|okay|k|sure|alright|all right|got it|understood|roger|noted|fine|"
            "好的|明白|收到|嗯|行",
        )
    )

    intents.append(
        _chat_intent(
            "chat_morning",
            [
                "Good morning! ☀️ Hope you have a smooth day — I'm here if you need duty info.",
                "Morning! Coffee time? ☕ I'm ready when you need `/fpms` or anything else.",
            ],
            "good morning|morning|gm|top of the morning|早|早安",
        )
    )

    intents.append(
        _chat_intent(
            "chat_tired",
            [
                "Hang in there! 💪 Take a break if you can — I'll handle the bot stuff when you're back.",
                "Long day? Rest up — I'm always here for quick duty lookups.",
            ],
            "i'm tired|so tired|exhausted|long day|need a break|burned out|burnout|"
            "好累|太累了|累死了",
        )
    )

    intents.append(
        _chat_intent(
            "chat_weather",
            [
                "I don't have a window 🌤️ — but I can fetch duty rosters! Try `/fpms` or “who is on bi duty”.",
                "No weather radar here — only spreadsheets and duty lists 😄",
            ],
            "weather|rain|sunny|hot today|cold today|going to rain|temperature|"
            "天气|下雨|好热",
        )
    )

    intents.append(
        _chat_intent(
            "chat_weekend",
            [
                "Hope you get a good rest! 🎉 I'll be here Monday for duty questions.",
                "Enjoy the weekend! Ping me anytime for on-call / duty info.",
            ],
            "weekend|friday|happy friday|tgif|saturday plans|sunday|long weekend|"
            "周末|星期五",
        )
    )

    intents.append(
        _chat_intent(
            "chat_confused",
            [
                "No problem — try `/help` for commands, or ask in plain English like “show fpms duty”.",
                "I'm not sure what you mean — duty question? Try `@Duty Bot /help`.",
            ],
            "i don't understand|don't get it|what do you mean|confused|huh|"
            "听不懂|不明白|什么意思",
        )
    )

    intents.append(
        _chat_intent(
            "chat_bored",
            [
                "Maybe check who's on duty? `/fpms` `/bi` `/sre` — or just chat, I'm listening 😄",
                "Bored? I can't stream Netflix — but I can tell you today's holidays with `/holiday`.",
            ],
            "i'm bored|so bored|nothing to do|kill time|boring|im bored|im boring|i am bored|"
            "i am boring|feeling bored|好无聊",
        )
    )

    # Catch-all casual English (trained heavily so most off-topic chat gets a friendly reply)
    general_patterns = [
        "just chatting",
        "wanted to say hi",
        "random thought",
        "having a coffee",
        "taking a break",
        "chilling at desk",
        "just chilling at my desk",
        "slow day today",
        "busy day today",
        "almost lunch time",
        "feeling good today",
        "not bad today",
        "you there",
        "anyone there",
        "talk to me",
        "let's chat",
        "nice to meet you",
        "what a day",
        "crazy day",
        "stressed out",
        "happy today",
        "just saying",
        "never mind",
        "fair enough",
        "makes sense",
        "sounds good",
        "interesting",
        "tell me more",
        "really",
        "wow",
        "oh nice",
        "that's cool",
        "good to know",
        "what's new",
        "how's your day",
        "you busy",
        "are you free",
        "just wondering",
        "no reason",
        "forget it",
        "carry on",
        "as you were",
        "good talk",
        "hmm",
        "i see",
        "right",
        "yeah",
        "yep",
        "nope",
        "maybe later",
        "we'll see",
        "let me think",
        "one sec",
        "hold on",
        "brb",
        "back now",
        "still here",
        "you awake",
        "anybody home",
        "knock knock",
        "tell me something",
        "surprise me",
        "cheer me up",
        "i need a break",
        "monday again",
        "almost friday",
        "weekend soon",
        "coffee break",
        "lunch soon",
    ]

    intents.append(
        _chat_intent(
            "chat_general",
            [
                "I'm mostly built for work stuff (duty, leave, machines) — but happy to chat briefly! "
                "Need anything? Try `/help` or ask naturally.",
                "Got you 😊 I'm Duty Bot — casual chat is fine; for tasks say things like “who is on fpms duty”.",
                "I hear you! For work I can help with rosters and lookups — otherwise I'm glad to keep you company.",
            ],
            "|".join(general_patterns),
        )
    )

    return intents


def _looks_like_command(text: str) -> bool:
    return (text or "").lstrip().startswith("/") or bool(_COMMANDISH_RE.search(text or ""))


def startup_status() -> None:
    enabled = is_enabled()
    mode = backend_mode()
    llm_ok = llm_available()
    gen_path = gen_model_dir()
    has_generative = generative_model_ready()
    print(
        f"[chatagent] BOT_USE_CHATAGENT={os.getenv('BOT_USE_CHATAGENT')!r} enabled={enabled} "
        f"backend={mode} api_key={'yes' if llm_ok else 'no'} api_model={_llm_model()!r} "
        f"command_only={_use_command_model_only()} "
        f"generative_dir={gen_path} generative_exists={has_generative}",
        flush=True,
    )
    if not enabled:
        print("[chatagent] Casual chat OFF.", flush=True)
        return
    if mode in ("llm", "auto") and llm_ok:
        mem = "on" if memory_enabled() else "off"
        print(
            f"[chatagent] ✅ API LLM ready ({_llm_model()} @ {_llm_base_url()}) "
            f"memory={mem} tz={_memory_timezone().key}",
            flush=True,
        )
    elif mode == "llm" and not llm_ok:
        print("[chatagent] ⚠️ backend=llm but no OPENAI_API_KEY / BOT_CHAT_API_KEY.", flush=True)
    if mode in ("local-llm", "auto") and has_generative:
        gen = _get_generative()
        if gen is None:
            print("[chatagent] ⚠️ Generative model present but failed to load.", flush=True)
        else:
            print(f"[chatagent] ✅ Self-trained generative chat ready ({gen_path})", flush=True)
    elif mode == "local-llm" and not has_generative:
        print("[chatagent] ⚠️ Run: python chatagent.py train-llm", flush=True)


def reply_if_enabled(text: str, *, session_key: Optional[str] = None) -> Optional[str]:
    """Return a casual chat reply: API LLM → local generative."""
    if not is_enabled():
        return None
    raw = (text or "").strip()
    if not raw:
        return None

    math_reply = resolve_math_from_context(raw, session_key=session_key)
    if math_reply:
        if session_key:
            _save_memory_turn(session_key, raw, math_reply)
        print(f"[chatagent] Math reply ({len(math_reply)} chars)", flush=True)
        return math_reply
    if looks_like_math_followup(raw):
        if _CJK_RE.search(raw):
            return "我记不清上一题了，请把完整算式再发一次。"
        return "I don't recall the earlier calculation — please send the full expression again."

    if looks_like_math_question(raw):
        return _math_parse_failure_message(raw)

    recall_reply = try_memory_recall_reply(raw, session_key=session_key)
    if recall_reply:
        if session_key:
            _save_memory_turn(session_key, raw, recall_reply)
        print(f"[chatagent] Memory recall reply ({len(recall_reply)} chars)", flush=True)
        return recall_reply

    if _looks_like_command(raw):
        return None
    if len(raw.split()) > MAX_CHAT_WORDS:
        return None

    mode = backend_mode()
    if mode in ("llm", "auto") and llm_available():
        reply = _llm_chat(raw, session_key=session_key)
        if reply and not _is_garbage_llm_content(reply):
            hist = len(_memory_get_history(session_key)) // 2 if session_key else 0
            print(
                f"[chatagent] API LLM reply ({len(reply)} chars, memory_turns={hist})",
                flush=True,
            )
            return reply
        if mode == "llm":
            print(f"[chatagent] API LLM no reply for {raw!r}", flush=True)
            return None

    if mode in ("local-llm", "auto"):
        reply = _local_generative_reply(raw)
        if reply and not _is_garbage_llm_content(reply):
            print(f"[chatagent] Local generative reply ({len(reply)} chars)", flush=True)
            return reply
        if mode == "local-llm":
            return None

    return None


def sanitize_outbound_chat_reply(text: str) -> Optional[str]:
    """Last gate before sending a chat reply to Lark — drop placeholders and meta junk."""
    out = _sanitize_llm_reply(text)
    if not out or _is_garbage_llm_content(out):
        return None
    return out


def reply_with_images(
    user_text: str,
    images: list[tuple[str, bytes]],
    *,
    session_key: Optional[str] = None,
) -> Optional[str]:
    """Vision chat via API LLM (Ollama qwen3.5:9b, GPT-4o, etc.)."""
    if not is_enabled() or not vision_enabled():
        return None
    if not images:
        return reply_if_enabled(user_text, session_key=session_key)
    mode = backend_mode()
    if mode not in ("llm", "auto") or not llm_available():
        return None
    reply = _llm_chat(user_text, images=images, session_key=session_key)
    if reply:
        print(
            f"[chatagent] API LLM vision reply ({len(reply)} chars, {len(images)} image(s))",
            flush=True,
        )
        return reply
    print(f"[chatagent] API LLM vision no reply for {user_text!r}", flush=True)
    return None


def train_generative_model(
    output_dir: Path,
    *,
    epochs: int = 5,
    batch_size: int = 4,
    lr: float = 5e-5,
) -> dict[str, Any]:
    """Fine-tune a small causal LM (DistilGPT-2) on chat Q→A pairs — train your own LLM locally."""
    from torch.utils.data import DataLoader, TensorDataset
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch = _lazy_torch()
    intents = build_chat_catalog()
    samples = build_generative_training_texts(intents)
    random.seed(42)
    random.shuffle(samples)
    split = int(len(samples) * 0.9)
    train_samples, val_samples = samples[:split], samples[split:]
    print(
        f"[chatagent] Generative train: {len(train_samples)} samples, "
        f"val={len(val_samples)}, base={GEN_BASE_MODEL!r}"
    )

    tok = AutoTokenizer.from_pretrained(GEN_BASE_MODEL)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = AutoModelForCausalLM.from_pretrained(GEN_BASE_MODEL).to(device)

    def _encode_batch(texts: list[str]):
        enc = tok(
            texts,
            truncation=True,
            padding="max_length",
            max_length=GEN_MAX_SEQ_LEN,
            return_tensors="pt",
        )
        return enc["input_ids"], enc["attention_mask"]

    def _loader(items: list[str], shuffle: bool) -> DataLoader:
        ids_list, mask_list = [], []
        for text in items:
            i, m = _encode_batch([text])
            ids_list.append(i.squeeze(0))
            mask_list.append(m.squeeze(0))
        ds = TensorDataset(torch.stack(ids_list), torch.stack(mask_list))
        return DataLoader(ds, batch_size=batch_size, shuffle=shuffle)

    train_loader = _loader(train_samples, shuffle=True)
    val_loader = _loader(val_samples, shuffle=False)
    optim = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)

    best_loss = float("inf")
    output_dir.mkdir(parents=True, exist_ok=True)

    for epoch in range(1, epochs + 1):
        model.train()
        train_loss = 0.0
        for input_ids, attention_mask in train_loader:
            input_ids = input_ids.to(device)
            attention_mask = attention_mask.to(device)
            optim.zero_grad()
            out = model(input_ids=input_ids, attention_mask=attention_mask, labels=input_ids)
            out.loss.backward()
            optim.step()
            train_loss += float(out.loss.item())

        model.eval()
        val_loss = 0.0
        with torch.no_grad():
            for input_ids, attention_mask in val_loader:
                input_ids = input_ids.to(device)
                attention_mask = attention_mask.to(device)
                out = model(input_ids=input_ids, attention_mask=attention_mask, labels=input_ids)
                val_loss += float(out.loss.item())

        avg_train = train_loss / max(len(train_loader), 1)
        avg_val = val_loss / max(len(val_loader), 1)
        print(f"Epoch {epoch}/{epochs}  train_loss={avg_train:.4f}  val_loss={avg_val:.4f}")
        if avg_val <= best_loss:
            best_loss = avg_val
            model.save_pretrained(str(output_dir))
            tok.save_pretrained(str(output_dir))

    meta = {"base_model": GEN_BASE_MODEL, "samples": len(samples), "format": "User: ...\\nBot: ...\\n"}
    with (output_dir / "metadata.pkl").open("wb") as f:
        pickle.dump(meta, f)

    print(f"✅ Generative chat model saved to {output_dir} best_val_loss={best_loss:.4f}")
    print("   Set BOT_CHATAGENT_BACKEND=local-llm (or auto) and restart larkbot.")
    return {"val_loss": best_loss, "samples": len(samples)}


def _cli_test(phrase: str) -> None:
    print(f"Input:       {phrase!r}")
    print(f"Backend:     {backend_mode()}  api_key={'yes' if llm_available() else 'no'}")
    if backend_mode() in ("llm", "auto") and llm_available():
        print(f"API reply:   {_llm_chat(phrase)!r}")
        if backend_mode() == "llm":
            return
    gen_path = gen_model_dir()
    if backend_mode() in ("local-llm", "auto") and (gen_path / "config.json").is_file():
        global _generative_singleton, _generative_failed
        _generative_singleton = None
        _generative_failed = False
        print(f"Gen reply:   {_local_generative_reply(phrase)!r}")
        return
    print("No backend available — set BOT_CHAT_API_KEY or run: python chatagent.py train-llm")


def main() -> None:
    parser = argparse.ArgumentParser(description="Duty Bot chat agent (casual conversation)")
    sub = parser.add_subparsers(dest="cmd")

    p_train_llm = sub.add_parser("train-llm", help="Fine-tune local generative LLM (DistilGPT-2)")
    p_train_llm.add_argument("--epochs", type=int, default=5)
    p_train_llm.add_argument("--output", type=str, default=str(DEFAULT_GEN_MODEL_DIR))

    p_test = sub.add_parser("test", help="Test a phrase")
    p_test.add_argument("phrase", type=str)

    sub.add_parser("patterns", help="Show pattern counts per intent")

    args = parser.parse_args()
    if args.cmd == "train-llm":
        train_generative_model(Path(args.output), epochs=args.epochs)
    elif args.cmd == "test":
        _cli_test(args.phrase)
    elif args.cmd == "patterns":
        intents = build_chat_catalog()
        total = 0
        for spec in intents:
            n = len(spec.patterns)
            total += n
            print(f"{spec.tag:22} {n:4} patterns  {len(spec.responses)} responses")
        print(f"Total patterns: {total}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
