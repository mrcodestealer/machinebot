"""Alias module: ``import webapp`` resolves to :mod:`webmachine` in this bot.

In osedutybot the maintained dashboard module is ``webapp.py`` (which also drags in
the whole duty-calendar/HRMS stack). This standalone machine bot ships the legacy
self-contained subset ``webmachine.py`` instead, but ``findmachine.py`` still does
``import webapp`` for its fresh-scrape-cache fast path (``_scrape_lock`` /
``_scrape_ts`` / ``_scrape_enabled`` / ``_run_scrape_once`` /
``_display_rows_and_provenance`` — all of which webmachine defines too).

Replacing this module object in ``sys.modules`` keeps attribute access live on
webmachine's real globals (a copied ``_scrape_ts`` would go stale on rebind).
"""

import sys

import webmachine

sys.modules[__name__] = webmachine
