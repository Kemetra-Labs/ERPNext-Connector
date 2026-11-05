# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Frappe-free checks that the shipped Custom Field fixtures are complete and exported (RT-16).

``fixtures/custom_field.json`` is only synced for the names the ``hooks.fixtures`` filter lists; a
field added to the JSON but missing from the filter silently never reaches a site (and the
connector would then write a field ERPNext drops). RT-16 / RT-14 D6 adds the Sales Invoice Item
``rt_line_ref`` custom field (authorized in decision 10343) that return rows are matched on.
"""

from __future__ import annotations

import json
from pathlib import Path

from retail_tower_erpnext_connector import hooks

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "custom_field.json"


def _fixture() -> list[dict]:
	return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _exported_names() -> set[str]:
	(entry,) = [f for f in hooks.fixtures if f["dt"] == "Custom Field"]
	((_field, _op, names),) = entry["filters"]
	return set(names)


def test_every_custom_field_in_the_fixture_is_exported_by_hooks():
	assert {f["name"] for f in _fixture()} == _exported_names()


def test_sales_invoice_item_line_ref_field_exists_read_only():
	by_name = {f["name"]: f for f in _fixture()}
	f = by_name["Sales Invoice Item-rt_line_ref"]
	assert (f["dt"], f["fieldname"], f["fieldtype"]) == ("Sales Invoice Item", "rt_line_ref", "Data")
	# Set only by the connector; never copied onto a new document by ERPNext's mapper.
	assert (f["read_only"], f["no_copy"]) == (1, 1)
