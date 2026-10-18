# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""The rule a feed-supplied ``sale.warehouseRef`` must meet before a sale is posted (RT-434).

Backend-Core's ``erpnext_warehouse_map`` is the warehouse mapping authority (RT-290 M-10): a frozen
work item carries its Warehouse on the feed, and the Connector posts to it. The compatibility
preflight checks only the Warehouses in the local fallback map, so a frozen Warehouse gets the same
leaf / same-company rules here, per item, before insert:

* a group Warehouse cannot hold stock (ERPNext rejects it on the invoice);
* a Warehouse of another company would post the sale's stock outside the sale's company;
* a Warehouse that does not exist cannot be posted to.

Each fails closed as ``validation``: nothing is inserted. This module imports NO frappe.
"""

from __future__ import annotations

from collections.abc import Mapping


class FrozenWarehouseRejected(Exception):
	"""The feed ``warehouseRef`` is not a postable Warehouse for the sale company (RT-434)."""


def assert_postable_warehouse(name: str, row: Mapping | None, company: str) -> None:
	"""Raise :class:`FrozenWarehouseRejected` unless ``row`` is a leaf Warehouse of ``company``.

	``row`` is the Warehouse's ``{company, is_group}`` as read from the site, or ``None`` when absent.
	"""
	suffix = "rejected as validation before insert (RT-434)"
	if row is None:
		raise FrozenWarehouseRejected(f"feed warehouseRef {name!r} does not exist on this site — {suffix}")
	if int(row.get("is_group") or 0):
		raise FrozenWarehouseRejected(
			f"feed warehouseRef {name!r} is a group warehouse (cannot hold stock) — {suffix}"
		)
	if row.get("company") != company:
		raise FrozenWarehouseRejected(
			f"feed warehouseRef {name!r} belongs to company {row.get('company')!r}, not the sale company "
			f"{company!r} — {suffix}"
		)
