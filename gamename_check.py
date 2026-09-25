"""
Game Name TEST check — runs after ``/unset`` finishes.

Unsetting test on the cabinets is not the whole story: a **game** can itself be flagged test on the
backend's Game Name page (``/egm/floor/gameNameList`` — the Name column reads ``dragonlaw (TEST)``).
The bot cannot and must not clear that flag; SRE does. So once an ``/unset`` job has posted its
summary and screenshots, this looks up every game the unset touched and, for any still in test,
posts::

    Detected dragonlaw is in test. Kindly inform SRE to unset the game name.

How a machine maps to a row on that page
----------------------------------------
The EGM status list's **Game Type** column (``webmachine_data.json`` → ``game_type``) holds the same
text as the Game Name page's **Game Tag** column (``Dragon's Law``). Names are compared on a
normalised key (NFKC, then letters and digits only), so ``Dragon's Law`` / ``Dragon’s Law`` /
``DRAGONS LAW`` all agree. The Name column is accepted as a second key. The match is deliberately
**exact** on that key: a looser match could pair a machine with the wrong row and report a TEST game
as clear.

How (TEST) is recognised
------------------------
This backend's Vue app does not always put ``(TEST)`` in the text: on the EGM list it renders an
empty ``<span class="test">`` and draws ``(TEST)`` with CSS ``::after`` (see
``smmachine._machine_name_cell_test_mode_and_display``), which ``innerText`` never sees. So a cell
counts as TEST when its text has ``(TEST)`` / ``[TEST]`` (full-width brackets too), when a *visible*
``.test`` element sits inside it, or when a ``::before`` / ``::after`` on a visible element inside it
draws "test". A Name that carries the bare word "test" in any other form is reported as unknown,
never as clear.

READ-ONLY — this is the part that matters
-----------------------------------------
That page carries **BatchTest**, **BatchTestCancel**, **Add**, per-row **Edit**, **Show** and
**Hidden** buttons, every one of which changes PROD. This module reads text through
``page.evaluate`` and touches exactly two controls, both found only inside a pagination /
page-length container:

* the *next page* control — clicked with ``dispatch_event('click')``, which targets that element
  directly. A coordinate-based mouse click can deliver its release to whatever the page moved under
  the cursor; a dispatched event cannot land anywhere else.
* the *entries per page* ``<select>`` — only when exactly one select on the page is unambiguously a
  page-length control (every option a page size).

It never clicks inside a table, and never sends keys.

Honest failure
--------------
Every game ends as ``test``, ``clear`` or ``unknown``. Silence means "checked and clear". A game is
only ``clear`` when the whole list was read — checked against the total the page itself displays
where it shows one — and no matching row carries a TEST mark. Anything else (login failed, page
unreachable or lacking permission, table not found, list only partly read, no matching row) goes on
the "check incomplete" card, because a missed TEST game is exactly the failure this exists to
prevent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

GAME_NAME_LIST_PATH = (os.environ.get("GAME_NAME_LIST_PATH") or "/egm/floor/gameNameList").strip()
if not GAME_NAME_LIST_PATH.startswith("/"):
    GAME_NAME_LIST_PATH = "/" + GAME_NAME_LIST_PATH

# "(TEST)" is what the page shows; "[TEST]" is tolerated in case another backend brackets it.
# Matched after NFKC, so full-width （TEST） counts too.
TEST_MARK_RE = re.compile(r"[\(\[]\s*test\s*[\)\]]", re.I)
_BARE_TEST_WORD_RE = re.compile(r"\btest\b", re.I)

# A game list is a few hundred rows at most; this only stops a pager that never reports its end.
MAX_PAGES = 60

# Lark card text is capped by this bot at 4000 characters; budget below it so the "… and N more"
# line always survives.
_CARD_BUDGET = 3800


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int((os.environ.get(name) or str(default)).strip()))
    except ValueError:
        return default


def check_enabled() -> bool:
    """``GAME_NAME_CHECK=0`` turns the post-unset check off without a deploy."""
    return (os.environ.get("GAME_NAME_CHECK", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def _nfkc(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s or ""))


def norm_key(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", _nfkc(s).lower())


def has_test_mark(s: Any) -> bool:
    return bool(TEST_MARK_RE.search(_nfkc(s)))


def strip_test_mark(name: Any) -> str:
    return re.sub(r"\s+", " ", TEST_MARK_RE.sub("", _nfkc(name))).strip()


def machine_name_key(name: Any) -> str:
    """
    Identity of a cabinet name with any TEST mark removed.

    The data file writes a test cabinet as ``5 Dragons-0278(TEST)``; once the unset has run and the
    scrape loop rewrites the file, the same cabinet is ``5 Dragons-0278``. Keying on the stripped
    form keeps the lookup working across that rename.
    """
    return norm_key(strip_test_mark(name))


def _short_err(e: Any) -> str:
    """One line, no Playwright call log — these land in a card bullet."""
    return re.sub(r"\s+", " ", str(e).split("Call log:")[0]).strip()[:200] or e.__class__.__name__


# ---------------------------------------------------------------------------
# which games, on which backend
# ---------------------------------------------------------------------------
def game_type_index() -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """``({(belongs, name_key): game_type}, {name_key: game_type})`` from the PROD rows."""
    from maintenancemachineagent import load_webmachine_rows  # noqa: WPS433
    from prod_machine_batch import _belongs_for_machine  # noqa: WPS433

    by_name: dict[tuple[str, str], str] = {}
    by_bare: dict[str, str] = {}
    for r in load_webmachine_rows():
        if str(r.get("environment") or "PROD").strip().upper() != "PROD":
            continue
        key = machine_name_key(r.get("name"))
        gt = str(r.get("game_type") or "").strip()
        if not (key and gt):
            continue
        by_name[(_belongs_for_machine(str(r.get("belongs") or "")), key)] = gt
        by_bare.setdefault(key, gt)
    return by_name, by_bare


def attach_game_types(machines: Iterable[dict], *, game_type_hint: str = "") -> list[dict]:
    """
    Copies of ``machines`` with ``game_type`` filled in from the data file, **now**.

    Call this before the unset runs: after it, the scrape loop renames test cabinets (it drops the
    ``(TEST)`` suffix) and the lookup by name would have to rely on :func:`machine_name_key` alone.
    """
    from prod_machine_batch import _belongs_for_machine  # noqa: WPS433

    by_name, by_bare = game_type_index()
    out: list[dict] = []
    for m in machines or []:
        if not isinstance(m, dict):
            continue
        c = dict(m)
        if not str(c.get("game_type") or "").strip():
            key = machine_name_key(c.get("machine") or c.get("name"))
            b = _belongs_for_machine(str(c.get("belongs") or ""))
            c["game_type"] = (by_name.get((b, key)) or by_bare.get(key)
                              or (game_type_hint or "").strip())
        out.append(c)
    return out


def plan_checks(machines: Iterable[dict], *, game_type_hint: str = "") -> dict[str, dict]:
    """
    Group the unset machines by backend and collect each backend's distinct game types.

    Returns ``{base_url: {"env": label, "user": .., "pw": .., "games": {key: {"game_type": ..,
    "machines": n}}, "unmapped": [machine, ...], "error": ""}}``. The backend is resolved through
    the very chain the unset itself logged in with (``prod_machine_batch._ensure_env_egm_page``), so
    the check reads the same backend whose cabinets were just changed. CP and OSM share one host,
    which is why this groups by URL and not by site.
    """
    from checkcredit import _np_resolve_backend  # noqa: WPS433
    from prod_machine_batch import _belongs_for_machine, _belongs_site_key  # noqa: WPS433
    from smmachine import _site_synthetic_machine  # noqa: WPS433

    plans: dict[str, dict] = {}
    for m in attach_game_types(machines, game_type_hint=game_type_hint):
        belongs = _belongs_for_machine(str(m.get("belongs") or ""))
        name = str(m.get("machine") or m.get("name") or "").strip()
        gt = str(m.get("game_type") or "").strip()

        try:
            site = _belongs_site_key(belongs)
            base, user, pw = _np_resolve_backend(_site_synthetic_machine(site))
        except (SystemExit, Exception) as e:  # noqa: BLE001  (SystemExit: unknown site alias)
            plan = plans.setdefault(f"?{belongs}", {"env": belongs or "?", "user": "", "pw": "",
                                                    "games": {}, "unmapped": [], "error": ""})
            plan["error"] = f"unknown backend for {belongs!r}: {_short_err(e)}"
            if gt:
                g = plan["games"].setdefault(norm_key(gt), {"game_type": gt, "machines": 0})
                g["machines"] += 1
            else:
                plan["unmapped"].append(name or "?")
            continue

        base = (base or "").rstrip("/")
        plan = plans.setdefault(base, {"env": belongs, "user": user, "pw": pw,
                                       "games": {}, "unmapped": [], "error": ""})
        if belongs and belongs not in plan["env"].split(" / "):
            plan["env"] = f"{plan['env']} / {belongs}"
        if not (user and pw):
            plan["error"] = f"missing backend credentials for {belongs!r}"
        if not gt or not norm_key(gt):
            plan["unmapped"].append(name or "?")
            continue
        g = plan["games"].setdefault(norm_key(gt), {"game_type": gt, "machines": 0})
        g["machines"] += 1
    return plans


# ---------------------------------------------------------------------------
# reading the Game Name page
# ---------------------------------------------------------------------------
# Framework-agnostic on purpose: the page could not be inspected from outside the allowlisted
# server, so this reads the three table shapes the backends are known to use — Element UI (header
# and body in separate <table>s), jQuery DataTables with scrolling (same split, different classes)
# and a plain <table> — and lets the header text pick the columns.
_READ_TABLES_JS = r"""
() => {
  const shown = el => !!(el && el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden';
  // A pseudo-element counts only when its content IS a TEST marker — "latest", "contest" or a
  // url(".../test/...") must not leak into the text and break the match.
  const isMark = c => /^[\s\(\[（【]*test[\s\)\]）】]*$/i.test(c || '');
  // (TEST) that innerText cannot see: CSS ::before / ::after on a rendered element (this backend's
  // EGM list draws the marker that way), or a .test element that is really painted — width AND
  // height, as Playwright's is_visible() requires. An empty inline span.test has width 0 but a
  // line's height and must not count: templates render it on every row and gate the drawing.
  const marks = el => {
    let s = '';
    for (const e of [el, ...el.querySelectorAll('*')]) {
      if (e !== el && !shown(e)) continue;
      for (const w of ['::before', '::after']) {
        const c = (getComputedStyle(e, w).content || '').replace(/^["']|["']$/g, '');
        if (c && c !== 'none' && c !== 'normal' && isMark(c)) s += ' (TEST)';
      }
      if (e !== el && e.classList && e.classList.contains('test')) {
        const st = getComputedStyle(e), r = e.getBoundingClientRect();
        if (st.display !== 'none' && st.visibility !== 'hidden'
            && ((r.width > 0 && r.height > 0) || (e.textContent || '').trim())) s += ' (TEST)';
      }
    }
    return s;
  };
  const txt = el => el ? ((el.innerText || el.textContent || '') + marks(el))
      .replace(/ /g, ' ').replace(/\s+/g, ' ').trim() : '';
  const htxt = el => el ? (el.innerText || el.textContent || '')
      .replace(/ /g, ' ').replace(/\s+/g, ' ').trim() : '';
  const cells = tr => Array.from(tr.children).filter(c => /^(TD|TH)$/.test(c.tagName));
  const out = [];
  document.querySelectorAll('[data-gn-idx]').forEach(e => e.removeAttribute('data-gn-idx'));
  // The table root is tagged so the pager and the total can be looked up NEXT TO this table —
  // a client-side attribute only, nothing is sent anywhere.
  const push = (root, rec) => { root.setAttribute('data-gn-idx', String(out.length)); rec.idx = out.length; out.push(rec); };

  document.querySelectorAll('.el-table').forEach(t => {
    if (!shown(t)) return;
    const head = t.querySelector(':scope > .el-table__header-wrapper');
    const body = t.querySelector(':scope > .el-table__body-wrapper');
    if (!head || !body) return;
    const hr = head.querySelectorAll('thead tr');
    if (!hr.length) return;
    const headers = cells(hr[hr.length - 1]).map(th => htxt(th.querySelector('.cell') || th));
    // A fixed="left"/"right" column is rendered twice: a hidden copy in the main body (td.is-hidden,
    // theme-chalk sets visibility:hidden on its contents) and the copy people see in the fixed
    // layer. CSS-drawn (TEST) only exists on the visible copy, so read that one — same row, same
    // column index, since the fixed layer renders every column. No visible copy -> unreadable,
    // which makes the whole read incomplete rather than silently clear.
    const fixedTrs = sel => Array.from(t.querySelectorAll(
        ':scope > ' + sel + ' > .el-table__fixed-body-wrapper tbody > tr'));
    const L = fixedTrs('.el-table__fixed'), R = fixedTrs('.el-table__fixed-right');
    let unreadable = false;
    const rows = [];
    Array.from(body.querySelectorAll('tbody > tr')).forEach((tr, k) => {   // index BEFORE filtering
      if (!shown(tr)) return;
      rows.push(cells(tr).map((td, ci) => {
        let src = td;
        if (td.classList.contains('is-hidden')) {
          src = null;
          for (const FX of [L, R]) {
            const c = FX[k] && cells(FX[k])[ci];
            if (c && !c.classList.contains('is-hidden')) { src = c; break; }
          }
          if (!src) { unreadable = true; src = td; }
        }
        return txt(src.querySelector('.cell') || src);
      }));
    });
    push(t, {kind: 'element-ui', headers, rows, unreadable});
  });

  document.querySelectorAll('.dataTables_scroll').forEach(w => {
    if (!shown(w)) return;
    const hr = w.querySelectorAll('.dataTables_scrollHead thead tr');
    const body = w.querySelector('.dataTables_scrollBody table');
    if (!hr.length || !body) return;
    const headers = cells(hr[hr.length - 1]).map(htxt);
    const rows = Array.from(body.querySelectorAll(':scope > tbody > tr')).filter(shown)
      .map(tr => cells(tr).map(txt));
    push(w, {kind: 'datatables-scroll', headers, rows});
  });

  document.querySelectorAll('table').forEach(t => {
    if (t.closest('.el-table') || t.closest('.dataTables_scroll') || !shown(t)) return;
    let hr = t.querySelectorAll(':scope > thead > tr');
    let headerRow = hr.length ? hr[hr.length - 1] : null;
    let bodyRows = Array.from(t.querySelectorAll(':scope > tbody > tr'));
    if (!headerRow) {
      const first = t.querySelector('tr');
      if (first && first.querySelector(':scope > th')) {
        headerRow = first;
        bodyRows = bodyRows.filter(r => r !== first);
      }
    }
    if (!headerRow) return;
    const headers = cells(headerRow).map(htxt);
    const rows = bodyRows.filter(shown).map(tr => cells(tr).map(txt));
    push(t, {kind: 'table', headers, rows});
  });
  return out;
}
"""


def _column_indexes(headers: list[str]) -> tuple[int, int, int]:
    """``(name_idx, tag_idx, id_idx)`` from header text; ``-1`` when absent."""
    name_idx = tag_idx = id_idx = -1
    for i, h in enumerate(headers):
        k = norm_key(h)
        if name_idx < 0 and k in ("name", "gamename"):
            name_idx = i
        if tag_idx < 0 and k in ("gametag", "tag"):
            tag_idx = i
        if id_idx < 0 and k == "id":
            id_idx = i
    return name_idx, tag_idx, id_idx


def pick_game_table(tables: list[dict]) -> dict | None:
    """
    The Game Name table: the one whose headers include **Name** (and ideally **Game Tag**).

    Returned as ``{"kind", "idx", "rows": [(name, tag), ...], "keys": [...]}``. Each key is the row's
    IDENTITY — its ID cell where the table has one, else Name + Tag — never the whole row's text: an
    icon that fails to load changes a row's text without it being a different row, and keying on
    text made that look like a new page and counted the same row twice. An empty-state row
    (DataTables' single ``colspan`` cell) is dropped, as is any row too short to reach Name.
    """
    best: dict | None = None
    best_score = -1
    for t in tables or []:
        headers = list(t.get("headers") or [])
        name_idx, tag_idx, id_idx = _column_indexes(headers)
        if name_idx < 0:
            continue
        rows: list[tuple[str, str]] = []
        keys: list[str] = []
        for cells in t.get("rows") or []:
            if len(cells) <= name_idx or (len(headers) > 1 and len(cells) == 1):
                continue
            name = str(cells[name_idx] or "").strip()
            tag = str(cells[tag_idx] or "").strip() if 0 <= tag_idx < len(cells) else ""
            if not name:
                continue
            ident = str(cells[id_idx] or "").strip() if 0 <= id_idx < len(cells) else ""
            rows.append((name, tag))
            keys.append(f"id:{ident}" if ident else
                        f"nt:{norm_key(strip_test_mark(name))}\x1f{norm_key(strip_test_mark(tag))}")
        score = (2 if tag_idx >= 0 else 0) * 100_000 + len(rows)
        if score > best_score:
            best_score = score
            best = {"kind": t.get("kind"), "idx": t.get("idx"), "name_idx": name_idx,
                    "tag_idx": tag_idx, "headers": headers, "rows": rows, "keys": keys,
                    # a fixed-column cell with no visible copy: its text is not trustworthy
                    "unreadable": bool(t.get("unreadable"))}
    return best


# Only controls inside a pager. Anything broader risks landing on BatchTest / Show / Hidden.
_NEXT_SELECTORS = (
    ".el-pagination button.btn-next",
    ".dataTables_paginate .paginate_button.next",
    ".dataTables_paginate li.next > a",
    ".dt-paging .dt-paging-button.next",
    "ul.pagination li.next > a",
    "ul.pagination li.page-item.next > a",
    "ul.pagination a[aria-label='Next']",
    "ul.pagination a[rel='next']",
)

# The page-length <select>, only when exactly one select is unambiguously that: inside
# .dataTables_length / .dt-length, or inside a label whose OWN text (not its options') says
# "entries", and every option a page size. Anything else — including two candidates — is left alone.
_LENGTH_CANDIDATES_JS = r"""
() => {
  const shown = e => !!(e && e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const ownText = e => Array.from(e.childNodes).filter(n => n.nodeType === 3)
      .map(n => n.textContent).join(' ');
  const pageSize = o => /^-?\d+$/.test((o.value || '').trim())
      && (/^\d+$/.test((o.text || '').trim()) || /^all$/i.test((o.text || '').trim()));
  return Array.from(document.querySelectorAll('select')).filter(s => {
    if (!shown(s) || s.disabled || s.closest('table') || s.form) return false;
    const lab = s.closest('label');
    if (!(s.closest('.dataTables_length') || s.closest('.dt-length')
          || (lab && /entries/i.test(ownText(lab))))) return false;
    return s.options.length > 1 && Array.from(s.options).every(pageSize);
  });
}
"""

_LENGTH_SELECT_JS = r"""
() => {
  const cands = (""" + _LENGTH_CANDIDATES_JS + r""")();
  if (cands.length !== 1) return null;
  const sel = cands[0];
  let best = null, bestN = -1;
  for (const o of sel.options) {
    const n = parseInt(o.value, 10);
    const score = (n === -1 || /^all$/i.test((o.text || '').trim())) ? Number.MAX_SAFE_INTEGER : n;
    if (score > bestN) { bestN = score; best = o.value; }
  }
  sel.setAttribute('data-gamename-check', '1');
  return {best: best === sel.value ? null : best, current: parseInt(sel.value, 10)};
}
"""

# Everything about the pager, read RELATIVE TO the game table (the root tagged data-gn-idx):
# the nearest next control and the nearest footer text — the first one after the table, else the
# last one before it. Another widget's pager or "of 3 entries" elsewhere on the page must not end
# the walk or prove it complete. Read-only, except tagging the chosen control data-gn-next so
# Python can dispatch to exactly that element.
_PAGER_STATE_JS = r"""
([idx, nextSel]) => {
  const root = document.querySelector('[data-gn-idx="' + idx + '"]');
  if (!root) return null;
  const shown = e => !!(e && e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const nearest = list => {
    const vis = list.filter(shown);
    const after = vis.filter(e => root === e || root.contains(e)
        || (root.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING));
    if (after.length) return after[0];
    return vis.length ? vis[vis.length - 1] : null;
  };
  document.querySelectorAll('[data-gn-next]').forEach(e => e.removeAttribute('data-gn-next'));

  const nx = nearest(Array.from(document.querySelectorAll(nextSel)));
  let next = {found: false, disabled: null, unsafe: false};
  if (nx) {
    const off = x => !!x && (x.disabled === true || x.getAttribute('aria-disabled') === 'true'
        || /(^|\s)disabled(\s|$)/.test(typeof x.className === 'string' ? x.className : ''));
    // A <button> inside a form with no type IS a submit button: a click would submit the form.
    const unsafe = (nx.tagName === 'BUTTON' || nx.tagName === 'INPUT') && !!nx.form
        && (nx.type || '').toLowerCase() === 'submit';
    nx.setAttribute('data-gn-next', '1');
    next = {found: true, disabled: off(nx) || off(nx.parentElement), unsafe};
  }

  const info = nearest(Array.from(document.querySelectorAll(
      '.dataTables_info, .dt-info, .el-pagination__total, .el-pagination, .pagination-info')));
  const itxt = info ? (info.innerText || '') : '';
  let total = null;
  const m = itxt.match(/of\s+([\d,]+)\s+entries/i) || itxt.match(/total\s*:?\s*([\d,]+)/i)
         || itxt.match(/共\s*([\d,]+)\s*条/);
  if (m) total = parseInt(m[1].replace(/,/g, ''), 10);
  const filtered = /filtered\s+from/i.test(itxt);

  const pagerPresent = Array.from(document.querySelectorAll('[class]')).some(e =>
      /(^|[\s_-])(paginat\w*|pager|paging|el-pagination|dataTables_paginate)([\s_-]|$)/i.test(
          typeof e.className === 'string' ? e.className : '') && shown(e));

  const lens = (""" + _LENGTH_CANDIDATES_JS + r""")();
  const len = lens.length === 1 ? parseInt(lens[0].value, 10) : null;
  return {next, total, filtered, pagerPresent, length: isNaN(len) ? null : len};
}
"""


def _read(page) -> dict | None:
    try:
        return pick_game_table(page.evaluate(_READ_TABLES_JS))
    except Exception as e:  # noqa: BLE001
        logger.warning("gamename-check: table read failed: %s", _short_err(e))
        return None


def _pager_state(page, table: dict | None) -> dict | None:
    """Pager facts next to ``table``; ``None`` if the page could not be evaluated (→ incomplete)."""
    if not table or table.get("idx") is None:
        return None
    try:
        st = page.evaluate(_PAGER_STATE_JS, [table["idx"], ", ".join(_NEXT_SELECTORS)])
    except Exception as e:  # noqa: BLE001
        logger.warning("gamename-check: pager state failed: %s", _short_err(e))
        return None
    return st if isinstance(st, dict) else None


def _signature(table: dict | None) -> str:
    if not table:
        return ""
    keys = table.get("keys") or []
    return json.dumps(keys[:3] + keys[-2:] + [len(keys)])


def _wait_for_table(page, *, timeout_ms: int) -> dict | None:
    """First qualifying table with rows, polled — SPA lists render empty, then fill."""
    waited = 0
    last: dict | None = None
    while waited <= timeout_ms:
        last = _read(page)
        if last and last["rows"]:
            return last
        page.wait_for_timeout(400)
        waited += 400
    return last


def _wait_for_change(page, before: str, *, timeout_ms: int) -> dict | None:
    waited = 0
    while waited <= timeout_ms:
        page.wait_for_timeout(300)
        waited += 300
        cur = _read(page)
        if cur and cur["rows"] and _signature(cur) != before:
            return cur
    return None


def read_all_game_rows(page, *, timeout_ms: int) -> tuple[list[tuple[str, str]], str, bool]:
    """
    Every ``(name, tag)`` on the Game Name page, across all pages.

    Returns ``(rows, error, complete)``. It fails **closed**: ``complete`` is ``True`` only when the
    read can be shown to cover the whole list —

    * the footer next to the table states a total, the list is not filtered, and the distinct rows
      read equal it exactly (a total that disagrees, either way, is someone else's number); or
    * with no total shown, the pager next to the table reported its last page twice, a moment
      apart, with the table unchanged in between; or
    * with no total shown, there is no pager-looking element anywhere and the page is not exactly
      full at its page size.

    Every other ending — an unrecognised pager, a stall, a reset to an earlier page, a failed
    click, a pager that would submit a form, a page that could not be evaluated, the list
    vanishing, the page cap — is ``False``, and the caller reports games as unknown, not clear.
    """
    table = _wait_for_table(page, timeout_ms=timeout_ms)
    if not table:
        return [], "Game Name table not found on the page", False
    if not table["rows"]:
        return [], "Game Name table is empty", False

    # Show as many entries per page as the page offers — fewer clicks, fewer chances to stall.
    try:
        info = page.evaluate(_LENGTH_SELECT_JS)
    except Exception:  # noqa: BLE001
        info = None
    if info and info.get("best") is not None:
        before = _signature(table)
        try:
            page.locator("select[data-gamename-check='1']").select_option(
                str(info["best"]), timeout=min(5_000, timeout_ms))
        except Exception as e:  # noqa: BLE001
            logger.info("gamename-check: page-length select skipped: %s", _short_err(e))
        changed = _wait_for_change(page, before, timeout_ms=min(timeout_ms, 10_000))
        table = changed or _read(page)
        if not table or not table["rows"]:
            return [], "the Game Name list disappeared after changing entries per page", False

    rows: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    signatures: set[str] = set()
    end = "max_pages"
    st: dict | None = None
    page_rows = len(table["rows"])
    confirms = 0
    unreadable = False
    for _step in range(MAX_PAGES * 2):
        sig = _signature(table)
        if sig not in signatures:
            signatures.add(sig)
            page_rows = len(table["rows"])
            unreadable = unreadable or bool(table.get("unreadable"))
            for r, k in zip(table["rows"], table.get("keys") or []):
                if k not in seen_keys:
                    seen_keys.add(k)
                    rows.append(r)
            confirms = 0

        st = _pager_state(page, table)
        if st is None:
            end = "eval_failed"
            break
        nxt = st.get("next") or {}
        if not nxt.get("found"):
            end = "no_pager"
            break
        if nxt.get("unsafe"):
            end = "unsafe_pager"    # clicking would submit a form — never
            break
        if nxt.get("disabled") is None:
            end = "eval_failed"
            break
        if nxt.get("disabled"):
            # A pager can be disabled for a moment while it loads. Only a second look, a moment
            # later, with the table unchanged, counts as the last page.
            confirms += 1
            if confirms >= 2:
                end = "last_page"
                break
            page.wait_for_timeout(700)
            again = _read(page)
            if not again or not again["rows"]:
                end = "list_gone"
                break
            table = again
            continue
        if len(signatures) >= MAX_PAGES:
            end = "max_pages"
            break
        try:
            # Targets the pager element itself — no coordinates, so nothing the page moves under
            # the cursor can receive the event.
            page.locator("[data-gn-next='1']").first.dispatch_event("click")
        except Exception as e:  # noqa: BLE001
            logger.warning("gamename-check: next page failed: %s", _short_err(e))
            end = "click_failed"
            break
        changed = _wait_for_change(page, sig, timeout_ms=min(timeout_ms, 12_000))
        if changed is None:
            end = "stall"           # the pager says there is more, but nothing new arrived
            break
        if _signature(changed) in signatures:
            end = "repeat"          # back on a page already read — the list reset mid-walk
            break
        table = changed

    # The list must still be on screen, and not behind a login redirect, for any of this to count.
    still = _read(page)
    if not still or not still["rows"] or "/login" in (page.url or "").lower():
        end = "list_gone"
    final = _pager_state(page, still) if end != "list_gone" else None
    total = final.get("total") if final else None
    filtered = bool(final.get("filtered")) if final else False

    if end in ("list_gone", "eval_failed", "unsafe_pager", "click_failed", "stall", "repeat",
               "max_pages") or final is None or unreadable:
        complete = False
    elif isinstance(total, int):
        complete = (not filtered) and len(seen_keys) == total
    elif end == "last_page":
        complete = not filtered
    else:  # no_pager
        length = final.get("length")
        full = isinstance(length, int) and length > 0 and page_rows >= length
        complete = not full and not final.get("pagerPresent") and not filtered
    print(f"[gamename-check] walk end={end} rows={len(rows)} total={total} filtered={filtered} "
          f"complete={complete}", flush=True)
    return rows, "", complete


def _open_game_name_page(page, base: str, user: str, pw: str, *, timeout_ms: int) -> str:
    """Log in (same routine as the unset) and open the Game Name page. ``""`` or an error."""
    from prod_machine_batch import _login_egm_backend, _login_retries  # noqa: WPS433

    last = ""
    for attempt in range(1, min(_login_retries(), 2) + 1):
        try:
            _login_egm_backend(page, base, user, pw, timeout_ms=timeout_ms)
            last = ""
            break
        except Exception as e:  # noqa: BLE001
            last = f"login failed: {_short_err(e)}"
            logger.warning("gamename-check: %s login attempt %d: %s", base, attempt, _short_err(e))
    if last:
        return last

    url = f"{base.rstrip('/')}{GAME_NAME_LIST_PATH}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=min(60_000, timeout_ms))
    except Exception as e:  # noqa: BLE001
        return f"could not open {GAME_NAME_LIST_PATH}: {_short_err(e)}"
    page.wait_for_timeout(800)
    if "/login" in (page.url or "").lower():
        return "sent back to the login page — the duty account may not have access to Game Name"
    return ""


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------
def evaluate_games(games: dict[str, dict], rows: list[tuple[str, str]], *,
                   complete: bool) -> list[dict]:
    """Classify each planned game against the page rows. Pure — unit-tested without a browser."""
    out: list[dict] = []
    for key, g in games.items():
        hits = [(n, t) for n, t in rows
                if (t and norm_key(strip_test_mark(t)) == key) or norm_key(strip_test_mark(n)) == key]
        in_test = [strip_test_mark(n) or n for n, t in hits if has_test_mark(n) or has_test_mark(t)]
        odd = [n for n, t in hits
               if not (has_test_mark(n) or has_test_mark(t)) and _BARE_TEST_WORD_RE.search(_nfkc(n))]
        if in_test:
            status, reason = "test", ""
        elif odd:
            status = "unknown"
            reason = f"Name reads {odd[0]!r} — an unrecognised TEST marker"
        elif hits and complete:
            status, reason = "clear", ""
        elif hits:
            status = "unknown"
            reason = ("no TEST entry on the pages that could be read, but the Game Name list "
                      "could not be read to the end")
        else:
            status = "unknown"
            reason = ("no matching Game Name entry" if complete
                      else "no matching entry in the pages that could be read")
        tags = sorted({strip_test_mark(t) for _n, t in hits if t})
        out.append({"game_type": g["game_type"], "machines": g["machines"], "status": status,
                    "test_names": sorted(set(in_test)), "tags": tags, "reason": reason})
    return out


def _unknown(plan: dict, g: dict, reason: str) -> dict:
    return {"env": plan["env"], "game_type": g["game_type"], "machines": g["machines"],
            "status": "unknown", "test_names": [], "tags": [], "reason": reason}


def run_check(machines: Iterable[dict], *, game_type_hint: str = "",
              headless: bool | None = None) -> list[dict]:
    """Every game an unset touched, each classified ``test`` / ``clear`` / ``unknown``."""
    plans = plan_checks(machines, game_type_hint=game_type_hint)
    results: list[dict] = []
    todo = {b: p for b, p in plans.items() if p["games"] and not p["error"]}
    for plan in plans.values():
        for name in plan["unmapped"]:
            results.append({"env": plan["env"], "game_type": "", "machine": name,
                            "machines": 1, "status": "unknown", "test_names": [], "tags": [],
                            "reason": "game type not found for this machine"})
        if plan["error"]:
            results.extend(_unknown(plan, g, plan["error"]) for g in plan["games"].values())
    if not todo:
        return results

    from playwright.sync_api import sync_playwright  # noqa: WPS433
    from smmachine import _smachine_resolve_headless  # noqa: WPS433

    timeout_ms = _env_int("GAME_NAME_CHECK_TIMEOUT_MS", 60_000)
    hl = _smachine_resolve_headless(
        headless if headless is not None else
        os.environ.get("SMACHINE_HEADLESS", "1").strip().lower() not in ("0", "false", "no")
    )
    done: set[str] = set()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=hl)
            try:
                for base, plan in todo.items():
                    context = None
                    try:
                        # Inside the try: a browser that dies between backends must cost only the
                        # backends still to come, not the results already in hand.
                        context = browser.new_context(viewport={"width": 1600, "height": 1000},
                                                      ignore_https_errors=True)
                        page = context.new_page()
                        page.set_default_timeout(timeout_ms)
                        err = _open_game_name_page(page, base, plan["user"], plan["pw"],
                                                   timeout_ms=timeout_ms)
                        rows: list[tuple[str, str]] = []
                        complete = False
                        if not err:
                            rows, err, complete = read_all_game_rows(page, timeout_ms=timeout_ms)
                        print(f"[gamename-check] {base}: {len(rows)} row(s) complete={complete} "
                              f"err={err!r}", flush=True)
                        if err:
                            results.extend(_unknown(plan, g, err) for g in plan["games"].values())
                        else:
                            for r in evaluate_games(plan["games"], rows, complete=complete):
                                r["env"] = plan["env"]
                                results.append(r)
                    except Exception as e:  # noqa: BLE001
                        logger.exception("gamename-check: %s failed", base)
                        results.extend(_unknown(plan, g, _short_err(e))
                                       for g in plan["games"].values())
                    finally:
                        done.add(base)
                        if context is not None:
                            try:
                                context.close()
                            except Exception:  # noqa: BLE001
                                pass
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        # Launch failed, or the Playwright driver died: keep what was collected and report the rest.
        logger.exception("gamename-check: browser session failed")
        for base, plan in todo.items():
            if base not in done:
                results.extend(_unknown(plan, g, f"browser failed: {_short_err(e)}")
                               for g in plan["games"].values())
    return results


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def _budget(lines: list[str], extra_for: Callable[[int], str], total: int) -> str:
    """Join lines up to the card budget; on overflow end with ``extra_for(n_left)``."""
    out: list[str] = []
    used = 0
    for i, ln in enumerate(lines):
        if used + len(ln) + 1 > _CARD_BUDGET - 80:
            out.append(extra_for(total - i))
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out).strip()


def build_alert_card(results: list[dict]) -> dict | None:
    """
    The TEST alert, in the wording asked for — or ``None`` when no game is in test.

    The sentence is kept verbatim (``Detected {game name} is in test. Kindly inform SRE to unset
    the game name.``) with the game's Name as ``{game name}``, because that is what SRE looks for on
    the page. The tag, backend and count go on a line underneath for the operator.
    """
    entries: list[list[str]] = []
    for r in results:
        if r.get("status") != "test":
            continue
        for name in r.get("test_names") or [r.get("game_type") or "?"]:
            tag = ", ".join(r.get("tags") or []) or r.get("game_type") or ""
            entries.append([
                f"Detected **{name}** is in test. Kindly inform SRE to unset the game name.",
                f"• Game tag: {tag} · Backend: {r.get('env') or '?'} · "
                f"Machines unset: {r.get('machines') or 0}",
                "",
            ])
    if not entries:
        return None
    # Budget by whole entries so a sentence is never split from its detail line.
    body_lines: list[str] = []
    used = 0
    for i, e in enumerate(entries):
        size = sum(len(x) + 1 for x in e)
        if used + size > _CARD_BUDGET - 80:
            body_lines.append(f"… and {len(entries) - i} more game(s) in test — check the Game Name "
                              f"page for all of them.")
            break
        body_lines.extend(e)
        used += size
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "red",
                   "title": {"tag": "plain_text", "content": "⚠️ Game name still in TEST"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(body_lines).strip()}},
        ]},
    }


def build_incomplete_card(results: list[dict]) -> dict | None:
    """What could not be verified, and why — ``None`` when everything was checked."""
    unk = [r for r in results if r.get("status") == "unknown"]
    if not unk:
        return None
    head = ["Could not confirm whether these games are still in test on the Game Name page — "
            "kindly check them manually:", ""]
    items: list[str] = []
    for r in unk:
        what = r.get("game_type") or (f"machine `{r.get('machine')}`" if r.get("machine") else "?")
        reason = re.sub(r"\s+", " ", str(r.get("reason") or "unknown")).strip()[:220]
        items.append(f"• **{what}** ({r.get('env') or '?'}) — {reason}")
    shown = items[:30]
    hidden = len(items) - len(shown)        # cut by the 30-entry cap, before any length cut
    lines = head + shown
    body = _budget(lines, lambda n: f"… and {n + hidden} more", len(lines))
    if hidden and "… and" not in body:
        body += f"\n… and {hidden} more"
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {"template": "yellow",
                   "title": {"tag": "plain_text", "content": "ℹ️ Game name check incomplete"}},
        "body": {"elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]},
    }


def run_and_report(machines: list[dict], *, chat_id: str, send_message: Callable[..., Any],
                   game_type_hint: str = "") -> list[dict]:
    """Run the check and post its cards through ``send_message`` (the job's thread sender)."""
    if not check_enabled():
        print("[gamename-check] disabled (GAME_NAME_CHECK=0)", flush=True)
        return []
    try:
        results = run_check(machines, game_type_hint=game_type_hint)
    except Exception as e:  # noqa: BLE001
        logger.exception("gamename-check crashed")
        results = [{"env": "?", "game_type": "all games in this unset", "machines": len(machines),
                    "status": "unknown", "test_names": [], "tags": [],
                    "reason": f"check crashed: {_short_err(e)}"}]
    summary = {s: sum(1 for r in results if r.get("status") == s)
               for s in ("test", "clear", "unknown")}
    print(f"[gamename-check] results: {summary}", flush=True)
    for card in (build_alert_card(results), build_incomplete_card(results)):
        if card is None:
            continue
        try:
            send_message(chat_id, json.dumps(card, ensure_ascii=False), msg_type="interactive")
        except Exception as e:  # noqa: BLE001
            logger.warning("gamename-check: could not post card: %s", _short_err(e))
    return results
