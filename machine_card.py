"""
Render machine-lookup results (nwr / nch / wf / cp / dhs / mdr / tbp / tbr) as a
Lark interactive card, extracting the TRTC streaming credentials from each stream URL.

The eight machine modules all emit the same shape of plain text:

    Machine : NWR2005            (or "📌 Machine : NCH1404", "WINFORD8092", …)

    • IP address
    Top Encoder : 10.240.20.5
    ...

    • URL                        (or "• Streaming address")
    CCTV URL : rtmp://http://intl-rtmp.rtc.qq.com/push/NWR2005_CCTV?sdkappid=20008185&userid=NWR2005_CCTV&usersig=hmd5-...
    ...

Rather than refactor all eight data layers, we parse that text (keying stream lines
off ``://`` in the value, not module-specific labels) and rebuild it as a card where
each stream URL is expanded into AppID / 房间号 / 用户号 / 签名.

A TRTC push URL looks like::

    rtmp://http://intl-rtmp.rtc.qq.com/push/<ROOM>?sdkappid=<APPID>&userid=<USERID>&usersig=<SIG>

so ROOM (房间号) is the ``/push/`` path segment, and AppID / 用户号 / 签名 come from the
query string. URLs that don't carry those params are shown as-is (graceful fallback).
"""

from __future__ import annotations

import re
from typing import Any, Optional

_APPID_RE = re.compile(r"[?&]sdkappid=([^&\s]+)", re.I)
_USERID_RE = re.compile(r"[?&]userid=([^&\s]+)", re.I)
_USERSIG_RE = re.compile(r"[?&]usersig=([^&\s]+)", re.I)
_ROOM_PUSH_RE = re.compile(r"/push/([^/?#&\s]+)", re.I)
_ROOM_LASTSEG_RE = re.compile(r"/([^/?#&\s]+)\?")

# Header: "Machine : NWR2005" / "📌 Machine : NCH1404" / "📌 Machine: DHS3077".
_HEADER_MACHINE_RE = re.compile(r"^(?:📌\s*)?Machine\s*[:：]\s*(.+?)\s*$")
# Bare header (winford emits "WINFORD8092" with no "Machine :").
_HEADER_BARE_RE = re.compile(r"^([A-Za-z]{2,8}\d{2,6})$")
# "Label : value" — labels never contain a colon, so split on the first one.
_KV_RE = re.compile(r"^\s*([^:：]+?)\s*[:：]\s*(.+?)\s*$")


def parse_trtc_url(url: str) -> Optional[dict[str, str]]:
    """Extract AppID / room / userid / usersig from a TRTC push URL; None if not TRTC."""
    u = (url or "").strip()
    if not u:
        return None
    app = _APPID_RE.search(u)
    uid = _USERID_RE.search(u)
    sig = _USERSIG_RE.search(u)
    if not (app or uid or sig):
        return None
    room = _ROOM_PUSH_RE.search(u) or _ROOM_LASTSEG_RE.search(u)
    return {
        "app_id": app.group(1) if app else "",
        "room": room.group(1) if room else "",
        "user_id": uid.group(1) if uid else "",
        "usersig": sig.group(1) if sig else "",
    }


def _stream_name(label: str) -> str:
    """Short stream name from a URL label (e.g. 'TOP Streaming URL' -> 'Top')."""
    low = (label or "").lower()
    if "cctv" in low:
        return "CCTV"
    if "substream" in low or "sub stream" in low:
        return "Main Substream"
    if "main" in low:
        return "Main"
    if "top" in low or "pool" in low:
        return "Top"
    name = re.sub(r"(?i)\b(streaming|stream|video|url|link|address)\b", "", label).strip(" :：-")
    return name or label


def _parse_records(raw_text: str) -> list[dict[str, Any]]:
    """Split the text output into per-machine records with ip / stream field lists."""
    records: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    for line in (raw_text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("•"):
            continue
        if "not found" in s.lower():
            records.append({"label": "", "found": False, "note": s, "ips": [], "streams": []})
            cur = None
            continue
        mh = _HEADER_MACHINE_RE.match(s)
        if mh:
            cur = {"label": mh.group(1).strip(), "found": True, "ips": [], "streams": []}
            records.append(cur)
            continue
        if ":" not in s and "：" not in s:
            mb = _HEADER_BARE_RE.match(s)
            if mb:
                cur = {"label": mb.group(1).strip(), "found": True, "ips": [], "streams": []}
                records.append(cur)
                continue
        mkv = _KV_RE.match(s)
        if mkv and cur is not None:
            label, value = mkv.group(1).strip(), mkv.group(2).strip()
            if "://" in value:
                cur["streams"].append((label, value))
            else:
                cur["ips"].append((label, value))
    return records


def _render_record_md(rec: dict[str, Any]) -> str:
    if not rec.get("found", True):
        return rec.get("note") or "not found"
    lines = [f"**{rec.get('label') or 'Machine'}**"]
    if rec["ips"]:
        lines.append("**• IP address**")
        for label, value in rec["ips"]:
            lines.append(f"{label} : {value}")
    for label, url in rec["streams"]:
        lines.append("")
        lines.append(f"**{_stream_name(label)}**")
        parsed = parse_trtc_url(url)
        if parsed:
            lines.append(f"AppID : {parsed['app_id']}")
            lines.append(f"房间号 : {parsed['room']}")
            lines.append(f"用户号 : {parsed['user_id']}")
            lines.append(f"签名 : {parsed['usersig']}")
        else:
            lines.append(url)
    return "\n".join(lines)


def build_card_from_text(raw_text: str, *, title: str = "Machine info") -> Optional[dict[str, Any]]:
    """Parse machine-lookup text into a Lark card; None when there's nothing to render
    (e.g. an error/usage message) so the caller can fall back to the raw text."""
    records = _parse_records(raw_text)
    if not records:
        return None
    elements: list[dict[str, Any]] = []
    for i, rec in enumerate(records):
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": _render_record_md(rec)}})
        if i != len(records) - 1:
            elements.append({"tag": "hr"})
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": title}},
        "body": {"elements": elements},
    }
