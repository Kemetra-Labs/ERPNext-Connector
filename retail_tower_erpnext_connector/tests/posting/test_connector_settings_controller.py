# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-379 review finding 5 — an operator saving Connector Settings keeps the poller's status (no bench).

RT-883 — the same holds for the pollers' feed state (the cursors and the bin-view retry set).

The poller writes the read-only ``compatibility_*`` fields straight to the database. A form opened
before that write still carries the old values; saving it would roll the status or fingerprint back
(a stale fingerprint would also fake a ``compatibility.drift`` on the next tick). ``validate``
therefore re-reads the STORED values before the save.
"""

import importlib
import sys
import types

import pytest

_CONTROLLER = "retail_tower_erpnext_connector.connector.doctype.connector_settings.connector_settings"
_MISSING = object()
_STORED = {
	"compatibility_status": "CRITICAL",
	"compatibility_checked_at": "2026-10-11 20:05:00",
	"compatibility_fingerprint": "b" * 64,
	"compatibility_report": "CRITICAL\ncritical: Global Defaults default_company is not configured",
}
_FEED_STATE = {
	"posting_feed_cursor": "posting-c-9",
	"bin_view_feed_cursor": "bin-c-4",
	"bin_view_retry_state": '{"r-1": {"request": {}, "failures": 1}}',
}


class _Document:
	def get(self, field):
		return getattr(self, field, None)

	def set(self, field, value):
		setattr(self, field, value)


@pytest.fixture
def controller():
	names = ("frappe", "frappe.model", "frappe.model.document", _CONTROLLER)
	saved = [(n, sys.modules.get(n, _MISSING)) for n in names]
	fake = types.ModuleType("frappe")
	stored = {**_STORED, **_FEED_STATE}
	fake.db = types.SimpleNamespace(
		get_single_value=lambda doctype, field: stored[field] if doctype == "Connector Settings" else None
	)
	document = types.ModuleType("frappe.model.document")
	document.Document = _Document
	sys.modules.update(
		{"frappe": fake, "frappe.model": types.ModuleType("frappe.model"), "frappe.model.document": document}
	)
	sys.modules.pop(_CONTROLLER, None)
	yield importlib.import_module(_CONTROLLER)
	for name, value in saved:
		sys.modules.pop(name, None)
		if value is not _MISSING:
			sys.modules[name] = value


def test_a_form_save_keeps_the_stored_compatibility_status(controller):
	doc = controller.ConnectorSettings()
	doc.posting_currency = "EGP"  # the operator's own edit is kept
	doc.compatibility_status = "OK"  # stale values from a form opened before the poller's write
	doc.compatibility_checked_at = "2026-10-11 19:00:00"
	doc.compatibility_fingerprint = "a" * 64
	doc.compatibility_report = "OK"
	doc.validate()
	assert {field: doc.get(field) for field in _STORED} == _STORED
	assert doc.posting_currency == "EGP"


def test_a_form_save_never_moves_a_feed_cursor_back(controller):
	doc = controller.ConnectorSettings()
	doc.posting_feed_cursor = "posting-c-1"  # stale values from a form opened before the poller's write
	doc.bin_view_feed_cursor = "bin-c-1"
	doc.bin_view_retry_state = "{}"
	doc.validate()
	assert {field: doc.get(field) for field in _FEED_STATE} == _FEED_STATE
