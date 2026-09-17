"""
``NWR2000-NWR2020`` → ``NWR2000, NWR2001, … NWR2020``.

Shared by every path that accepts machine names — ``/sst``, ``/set``, the ``/sm`` wizard, the
``/nwrsetmaintenance`` family and the free-text ``set maintenance …`` handler — so a range means
the same thing wherever an operator types one.

Why this is deliberately strict
-------------------------------
A hyphen is **not** a range marker in this data: 2344 of the 2425 machine display names contain
one (``5 Dragons-NWR2113``, ``Echo Fortunes-0096``, ``118 - Carnival Cow``, ``BZZF-NCH-15``,
``CP0119-119-COINCOMBO-PEACOCK``). Treating every ``A-B`` as a range would silently turn one
cabinet into hundreds, on a command that changes PROD.

So a token is a range only when it **fully** matches ``[prefix]digits - [prefix]digits`` and
nothing else. Anything with a letter, a space or a second hyphen outside that shape is left
alone as an ordinary display name. :func:`selftest_against_names` asserts the rule fires on no
real machine name; it is run by this module's ``__main__`` against ``webmachine_data.json``.
"""

from __future__ import annotations

import re
from typing import Iterable

# Longest-first so ``WINFORD8145`` is not read as ``WF`` (mirrors sst._ENV_PREFIX_ALT and
# smmachine._ENV_PREFIX_RE — keep the three in step).
_ENV_PREFIX_ALT = "WINFORD|NWR|NCH|TBR|TBP|MDR|DHS|OSM|DYB|NP|NC|CP|WF"

# The ONLY shape accepted as a range. Anchored at both ends on purpose: a partial match is what
# would let a display name be mistaken for a span.
_RANGE_RE = re.compile(
    rf"^\s*(?P<p1>{_ENV_PREFIX_ALT})?\s*-?\s*(?P<n1>\d{{2,6}})"
    rf"\s*(?:-|–|—|\.\.|~|\bto\b)\s*"
    rf"(?P<p2>{_ENV_PREFIX_ALT})?\s*-?\s*(?P<n2>\d{{2,6}})\s*$",
    re.I,
)

# A range wider than this is a typo, not an intent. Over the cap the token is left unexpanded, so
# it fails resolution visibly ("not detected") instead of quietly setting half the floor.
#
# 300 is a performance ceiling as much as a sanity one: resolving a span costs ~4 ms per member
# against the real data, and the Confirm button that triggers it has to answer inside Lark's 3 s
# card-callback window. 300 lands at ~1.3 s with headroom, and is still an order of magnitude
# larger than any real maintenance window.
MAX_RANGE_SPAN = 300

# Same shape as _RANGE_RE but findable mid-line, for "set maintenance NWR2000-NWR2020" where the
# span shares a line with the command words. The boundary guards are what keep it off a
# hyphenated display name: ``CP0119-119-COINCOMBO-PEACOCK`` cannot match, because the character
# after any candidate second end is ``-``. Validated against every real machine name.
_INLINE_RANGE_RE = re.compile(
    rf"(?<![A-Za-z0-9\-])"
    rf"(?:{_ENV_PREFIX_ALT})?\s*-?\s*\d{{2,6}}"
    rf"\s*(?:-|–|—|\.\.|~|\bto\b)\s*"
    rf"(?:{_ENV_PREFIX_ALT})?\s*-?\s*\d{{2,6}}"
    rf"(?![A-Za-z0-9\-])",
    re.I,
)


def expand_ranges_inline(text: str) -> tuple[str, bool]:
    """
    Replace every span **inside** ``text`` with its expanded run. Returns ``(text, expanded)``.

    For callers whose input is a sentence rather than a bare token —
    ``set maintenance NWR2000-NWR2020``. Splitting such a line on whitespace first would shred
    ``NWR2000 - NWR2020`` and ``NWR2000 to NWR2020`` into pieces that are no longer spans, which
    is why the scan is done on the whole line with boundary guards instead.

    A span the expander refuses is left exactly as written, so ``expanded`` reports only real
    expansions and this can never quietly widen anything.
    """
    if not text:
        return text, False
    hit = False

    def _sub(m: re.Match) -> str:
        nonlocal hit
        members = expand_range_token(m.group(0))
        if members is None:
            return m.group(0)
        hit = True
        return " ".join(members)

    return _INLINE_RANGE_RE.sub(_sub, text), hit


def looks_like_range(token: str) -> bool:
    """``True`` for the ``A-B`` shape, whether or not it is within :data:`MAX_RANGE_SPAN`."""
    return _RANGE_RE.match((token or "").strip()) is not None


def expand_range_token(token: str) -> list[str] | None:
    """
    Expand one token, or ``None`` when it is not a range this module will touch.

    ``None`` (rather than an exception or a silent single-item list) is what lets every caller
    keep its existing "not a range → treat as a display name" path unchanged.

    Rules, all of which must hold:

    * the whole token is ``[prefix]digits <sep> [prefix]digits`` — no letters, spaces or extra
      hyphens anywhere else
    * if both sides name an environment they must name the **same** one (``NWR2000-NCH2020`` is a
      typo, not a span, and silently picking one side would set the wrong venue)
    * the end is not before the start
    * the span is at most :data:`MAX_RANGE_SPAN`

    Zero padding follows the widest side, so ``0096-0099`` yields ``0096 … 0099`` rather than
    ``96 … 99``.
    """
    m = _RANGE_RE.match((token or "").strip())
    if not m:
        return None

    p1 = (m.group("p1") or "").upper()
    p2 = (m.group("p2") or "").upper()
    if p1 and p2 and p1 != p2:
        return None
    prefix = p1 or p2

    n1, n2 = m.group("n1"), m.group("n2")
    start, end = int(n1), int(n2)
    if end < start or (end - start + 1) > MAX_RANGE_SPAN:
        return None

    width = max(len(n1), len(n2))
    return [f"{prefix}{i:0{width}d}" for i in range(start, end + 1)]


def expand_tokens(tokens: Iterable[str]) -> tuple[list[str], set[str]]:
    """
    Expand every range in ``tokens``, preserving order and dropping duplicates.

    Returns ``(expanded, from_range)`` where ``from_range`` holds the tokens that were produced by
    expanding a span. Callers use it to treat a gap in a range differently from a name the
    operator typed by hand: a typo deserves an error, whereas a range simply meaning "every
    cabinet that exists between these two" does not.
    """
    out: list[str] = []
    from_range: set[str] = set()
    seen: set[str] = set()

    def _add(tok: str, *, ranged: bool) -> None:
        key = tok.strip().upper()
        if not key or key in seen:
            return
        seen.add(key)
        out.append(tok)
        if ranged:
            from_range.add(key)

    for tok in tokens or []:
        members = expand_range_token(tok)
        if members is None:
            _add(tok, ranged=False)
        else:
            for mem in members:
                _add(mem, ranged=True)
    return out, from_range


def range_hint(tokens: Iterable[str]) -> str:
    """
    A one-line explanation for tokens that look like a range but were refused, or ``""``.

    Without this an over-wide or reversed span just comes back as "not detected", which reads like
    the machine is missing rather than like the range was rejected.
    """
    bad = [t for t in (tokens or [])
           if looks_like_range(t) and expand_range_token(t) is None]
    if not bad:
        return ""
    return (f"⚠️ `{bad[0]}` looks like a range but was not expanded — the two ends must name the "
            f"same environment, run upwards, and span at most {MAX_RANGE_SPAN} machines.")


# ---------------------------------------------------------------------------
# self-test — the rule is only safe if it fires on NO real machine name
# ---------------------------------------------------------------------------
def selftest_against_names(names: Iterable[str]) -> list[str]:
    """Real display names that this module would wrongly treat as a range (must be empty)."""
    return [n for n in names if expand_range_token(n) is not None]


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parent / "webmachine_data.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else (raw.get("machines") or raw.get("rows") or [])
    names = sorted({str(r.get("name") or "").strip() for r in rows if isinstance(r, dict)})
    names = [n for n in names if n]
    bad = selftest_against_names(names)
    print(f"checked {len(names)} real machine names — {len(bad)} false ranges")
    for n in bad[:20]:
        print("   FALSE RANGE:", n, "->", expand_range_token(n))
    sys.exit(1 if bad else 0)
