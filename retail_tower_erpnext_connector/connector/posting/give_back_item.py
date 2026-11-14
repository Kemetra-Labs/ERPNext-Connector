# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""The promotion give-back Item (OD-11 Hybrid, RT-1340). Pure: no frappe.

Owner decision (2026-11-14, RT-1340): the give-back posts on ONE fixed Item code that the connector
creates on install and every migrate (:func:`connector.schema.ensure_give_back_item`). A fixed code
keeps replay stable: the replay check compares ``item_code`` and the work item does not freeze it, so a
configurable Item changed later would turn every earlier give-back into a mismatch.

When the Item is missing, disabled or a stock Item, a give-back return is acked ``failed_transient``
(Backend-Core re-offers it within its retry budget) and the Compatibility Profile shows a WARNING, never
a critical finding: a critical one would pause all posting (owner decision, same date).
"""

from __future__ import annotations

from collections.abc import Mapping

from .return_builder import GiveBackItem, GiveBackItemNotConfigured

GIVE_BACK_ITEM_CODE = "RT-PROMO-GIVEBACK"
GIVE_BACK_ITEM_NAME = "Retail Tower promotion give-back"
GIVE_BACK_ITEM_FIELDS = ["disabled", "is_stock_item", "is_sales_item", "stock_uom"]


def give_back_item_problem(row: Mapping[str, object] | None) -> str | None:
	"""Why the Item row read back (``GIVE_BACK_ITEM_FIELDS``) cannot carry a give-back, or ``None``."""
	if not row:
		return "does not exist"
	if int(row.get("disabled") or 0):
		return "is disabled"
	if int(row.get("is_stock_item") or 0):
		return "is a stock Item (a give-back must not move stock)"
	if not int(row.get("is_sales_item") or 0):
		return "is not a sales Item (ERPNext refuses it on a Sales Invoice)"
	if not row.get("stock_uom"):
		return "has no stock UOM"
	return None


def resolve_give_back_item(row: Mapping[str, object] | None) -> GiveBackItem:
	"""The :class:`GiveBackItem` (its own stock UOM) or :class:`GiveBackItemNotConfigured` with the reason."""
	problem = give_back_item_problem(row)
	if problem is not None:
		raise GiveBackItemNotConfigured(f"Item {GIVE_BACK_ITEM_CODE!r} {problem}")
	return GiveBackItem(item_code=GIVE_BACK_ITEM_CODE, uom=str(row["stock_uom"]))
