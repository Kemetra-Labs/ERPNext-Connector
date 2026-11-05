# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT Warehouse Map Row — child table of Connector Settings (spec 006, FR-010 rider R5).

One store→ERPNext-Warehouse mapping row applying the DP2-pre-resolved warehouse. Read via
``connector.posting.config.parse_warehouse_map``; a store with no row fails closed. No behaviour
on the row itself.
"""

from frappe.model.document import Document


class RTWarehouseMapRow(Document):
	pass
