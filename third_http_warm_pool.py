"""
Warm browser pool for Log Third Http Req (checkcredit / stuck credit / checkmachinelog).

One persistent logged-in Chromium per backend tag (NP, NCH, …). Disable with
``THIRD_HTTP_WARM_POOL=0``.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

_THIRD_HTTP_WARM_ALL_TAGS: tuple[str, ...] = ("NP", "NCH", "DHS", "CP", "TBP", "WF", "MDR", "TBR")
_THIRD_HTTP_WARM_KEEPALIVE_SEC = 240.0


def third_http_warm_pool_enabled() -> bool:
    return (os.environ.get("THIRD_HTTP_WARM_POOL", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def third_http_warm_prewarm_on_startup() -> bool:
    return (os.environ.get("THIRD_HTTP_WARM_PREWARM_ON_STARTUP", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def third_http_warm_envs() -> list[str]:
    raw = (os.environ.get("THIRD_HTTP_WARM_ENVS") or "").strip()
    if not raw:
        return list(_THIRD_HTTP_WARM_ALL_TAGS)
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        tag = part.strip().upper()
        if tag and tag in _THIRD_HTTP_WARM_ALL_TAGS and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out or list(_THIRD_HTTP_WARM_ALL_TAGS)


def _third_http_warm_headless() -> bool:
    import checkcredit as cc

    if cc._np_truthy_env("THIRD_HTTP_WARM_HEADLESS"):
        return True
    if cc._np_truthy_env("THIRD_HTTP_WARM_HEADED"):
        return False
    return cc._np_backend_playwright_headless()


def _warm_profile_dir(tag: str) -> Path:
    import checkcredit as cc

    safe = "".join(c if c.isalnum() else "_" for c in tag.lower())
    return Path(cc._ensure_writable_temp_dir()) / f"third_http_warm_{safe}"


class _ThirdHttpWarmWorker:
    """One browser thread per backend tag — stays on Log Third Http after login."""

    def __init__(self, tag: str) -> None:
        self.tag = tag.upper()
        self._tasks: queue.Queue[dict[str, Any]] = queue.Queue()
        self._p = None
        self._context = None
        self._page = None
        self._last_active = time.monotonic()
        threading.Thread(target=self._loop, name=f"third-http-warm-{self.tag}", daemon=True).start()
        threading.Thread(
            target=self._keepalive_loop, name=f"third-http-warm-ka-{self.tag}", daemon=True
        ).start()

    def submit_prewarm(self) -> None:
        self._tasks.put({"kind": "prewarm"})

    def screenshot(self, **kwargs: Any) -> str:
        done = threading.Event()
        box: dict[str, Any] = {}
        self._tasks.put({"kind": "screenshot", "kwargs": kwargs, "done": done, "box": box})
        done.wait()
        if box.get("error"):
            raise RuntimeError(str(box["error"]))
        path = str(box.get("path") or "").strip()
        if not path:
            raise RuntimeError(f"[third-http-warm:{self.tag}] screenshot returned no path")
        return path

    def _loop(self) -> None:
        while True:
            task = self._tasks.get()
            kind = task.get("kind")
            if kind == "prewarm":
                try:
                    self._ensure_ready(timeout_ms=120_000)
                    print(f"[third-http-warm:{self.tag}] pre-warmed (browser stays open).", flush=True)
                except Exception as ex:
                    print(f"[third-http-warm:{self.tag}] prewarm failed: {ex!r}", flush=True)
                    self._teardown()
                continue
            if kind == "keepalive":
                try:
                    if self._healthy():
                        # force=True so this idle refresh actually re-hits the server and
                        # re-logs-in an expired session HERE — not mid-screenshot. Without
                        # force, the login fast-path returns instantly and the session is
                        # never refreshed, so it silently dies and the next request goes cold.
                        self._ensure_ready(timeout_ms=120_000, force=True)
                except Exception:
                    self._teardown()
                continue
            if kind == "screenshot":
                self._handle_screenshot(task)
                continue

    def _handle_screenshot(self, task: dict[str, Any]) -> None:
        import checkcredit as cc
        from np_third_http_page import (
            np_third_http_dismiss_open_dialogs,
            np_third_http_run_search_and_screenshot,
        )

        box = task["box"]
        kw = task.get("kwargs") or {}
        timeout_ms = int(kw.get("timeout_ms") or 120_000)
        self._last_active = time.monotonic()
        out_path = cc._temp_png_path("np_third_http_")
        try:
            self._ensure_ready(timeout_ms=timeout_ms)
            headless = _third_http_warm_headless()
            np_third_http_dismiss_open_dialogs(self._page)
            np_third_http_run_search_and_screenshot(
                self._page,
                player_id=str(kw["player_id"]),
                date_iso=str(kw["date_iso"]),
                time_short=str(kw["time_short"]),
                time_short_candidates=kw.get("time_short_candidates"),
                machine_substr=kw.get("machine_substr"),
                expected_credit=kw.get("expected_credit"),
                machine_display=kw.get("machine_display"),
                timeout_ms=timeout_ms,
                headless=headless,
                out_path=out_path,
            )
            box["path"] = out_path
        except Exception as ex:
            box["error"] = ex
            try:
                os.remove(out_path)
            except OSError:
                pass
            self._teardown()
        finally:
            self._last_active = time.monotonic()
            task["done"].set()

    def _keepalive_loop(self) -> None:
        while True:
            time.sleep(_THIRD_HTTP_WARM_KEEPALIVE_SEC)
            if self._healthy() and (time.monotonic() - self._last_active) >= _THIRD_HTTP_WARM_KEEPALIVE_SEC:
                self._tasks.put({"kind": "keepalive"})

    def _healthy(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    def _ensure_ready(self, *, timeout_ms: int, force: bool = False) -> None:
        import checkcredit as cc
        from np_third_http_page import np_third_http_login_and_open_log_page

        if not self._healthy():
            self._launch()
        elif force:
            # Idle keepalive ONLY. Reload so the SPA re-validates its session against the
            # server: if the session is still good the page stays on the log URL and the
            # login call below fast-paths; if it expired the reload redirects to /login and
            # the login call re-authenticates — so an expired session is refreshed HERE,
            # during idle, instead of failing mid-screenshot. If the reload wedges the page,
            # rebuild it clean rather than logging in on a half-dead page.
            # The screenshot hot path never forces, so it keeps the original cheap login
            # fast-path behaviour unchanged (no regression).
            try:
                self._page.reload(wait_until="domcontentloaded", timeout=min(60_000, timeout_ms))
            except Exception:
                self._teardown()
                self._launch()

        base, user, pw = cc._np_resolve_backend_for_tag(self.tag)
        if not user or not pw:
            raise RuntimeError(
                f"Missing backend credentials for Third Http warm pool tag {self.tag}"
            )
        np_third_http_login_and_open_log_page(
            self._page,
            base=base,
            user=user,
            pw=pw,
            machine_display=cc._np_sample_machine_for_tag(self.tag),
            timeout_ms=timeout_ms,
        )

    def _launch(self) -> None:
        from playwright.sync_api import sync_playwright

        self._teardown()
        self._p = sync_playwright().start()
        profile = _warm_profile_dir(self.tag)
        profile.mkdir(parents=True, exist_ok=True)
        self._context = self._p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=_third_http_warm_headless(),
            viewport={"width": 1600, "height": 900},
            ignore_https_errors=True,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        print(f"[third-http-warm:{self.tag}] browser launched (kept open).", flush=True)

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


class _ThirdHttpWarmPool:
    def __init__(self) -> None:
        self._workers: dict[str, _ThirdHttpWarmWorker] = {}
        self._lock = threading.Lock()

    def _get(self, tag: str) -> _ThirdHttpWarmWorker:
        t = tag.strip().upper()
        with self._lock:
            if t not in self._workers:
                self._workers[t] = _ThirdHttpWarmWorker(t)
            return self._workers[t]

    def prewarm(self, tags: list[str]) -> None:
        for tag in tags:
            self._get(tag).submit_prewarm()

    def screenshot(self, tag: str, **kwargs: Any) -> str:
        return self._get(tag).screenshot(**kwargs)


_pool_singleton: _ThirdHttpWarmPool | None = None
_pool_lock = threading.Lock()


def third_http_warm_pool() -> _ThirdHttpWarmPool:
    global _pool_singleton
    with _pool_lock:
        if _pool_singleton is None:
            _pool_singleton = _ThirdHttpWarmPool()
        return _pool_singleton


def prewarm_third_http_pool_on_startup() -> None:
    """Launch + login one persistent browser per configured backend tag."""
    if not third_http_warm_pool_enabled():
        print("[third-http-warm] disabled (THIRD_HTTP_WARM_POOL=0).", flush=True)
        return
    if not third_http_warm_prewarm_on_startup():
        print(
            "[third-http-warm] startup pre-warm skipped (THIRD_HTTP_WARM_PREWARM_ON_STARTUP=0).",
            flush=True,
        )
        return
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print(
            "[third-http-warm] startup pre-warm skipped — playwright not installed "
            "(pip install playwright && playwright install chromium).",
            flush=True,
        )
        return
    import checkcredit as cc

    tags = [t for t in third_http_warm_envs() if cc._np_backend_has_credentials(t)]
    if not tags:
        print("[third-http-warm] no backends with credentials — nothing to pre-warm.", flush=True)
        return
    print(f"[third-http-warm] startup pre-warm ({len(tags)} browser(s)): {', '.join(tags)}", flush=True)
    try:
        third_http_warm_pool().prewarm(tags)
    except Exception as ex:
        print(f"[third-http-warm] startup pre-warm failed: {ex!r}", flush=True)
