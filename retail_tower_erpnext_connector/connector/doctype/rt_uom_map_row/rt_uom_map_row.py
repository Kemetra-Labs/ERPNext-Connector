# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT Uom Map Row — child table of Connector Settings (spec 006, FR-008).

One unit→ERPNext-UOM mapping row. The connector reads these rows via
``connector.posting.config.parse_uom_map`` and applies them fail-closed (an unmapped unit →
permanently_rejected/validation). No behaviour on the row itself.
"""

from frappe.model.document import Document


class RTUomMapRow(Document):
	pass
