# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""ERP-origin stock import reader (RT-1287, Inventory Gate 8 P3) — the connector side of
Backend-Core's ``stock-import.yaml`` 1.0.0-draft (RT-1286).

The connector PULLS wanted Stock Ledger reads (``stockImportPullRequests``), reads the submitted
and cancelled Stock Ledger Entries at the requested mapped bucket warehouse after Backend-Core's
keyset ``after`` (one report row per Batch entry of a row's Serial and Batch Bundle), and REPORTS
the facts back (``stockImportReportRows``). FACTS ONLY (G8-7): nothing is classified or filtered
by business rule here; Backend-Core decides import versus finding (RT-1288).

Rollout order (contract): Backend-Core serves the routes first (RT-1288). The poller is therefore
off unless the site config switch ``rt_connector_stock_import_enabled`` is on, and it is not yet
registered in ``hooks.py`` (a gated surface; see ``poller``).
"""
