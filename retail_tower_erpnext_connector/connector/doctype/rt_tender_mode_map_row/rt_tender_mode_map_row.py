# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT Tender Mode Map Row — child table of Connector Settings (RT-78, RT-10 D5).

One posting-feed tender method → ERPNext Mode of Payment row. Read via
``connector.posting.config.parse_tender_mode_map``; a tender-bearing sale whose method has no row
fails closed (UnmappedTender → validation) — the connector never guesses a payment mode or account.
No behaviour on the row itself.
"""

from frappe.model.document import Document


class RTTenderModeMapRow(Document):
	pass
