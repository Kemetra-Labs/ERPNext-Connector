# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT Store Customer Map Row — child table of Connector Settings (spec 006, F-009 closure).

One store→ERPNext-Customer mapping row. The 012 work-item carries no customer; the operator maps
each store here. Read via ``connector.posting.config.parse_store_customer_map``; a store with no
row fails closed (UnmappedStore → validation) — the connector never fabricates a customer
(Principle VI). No behaviour on the row itself.
"""

from frappe.model.document import Document


class RTStoreCustomerMapRow(Document):
	pass
