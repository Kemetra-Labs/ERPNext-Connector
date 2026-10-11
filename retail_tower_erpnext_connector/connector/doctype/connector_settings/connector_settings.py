# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

# Connector Settings — Single DocType (spec 001; fields from specs 006/007, RT-379).
# No business logic beyond keeping the poller-owned status fields consistent (Principle VII).

import frappe
from frappe.model.document import Document

# RT-379: written by the poller (frappe.db.set_single_value), read-only on the form.
_PROFILE_STATUS_FIELDS = (
	"compatibility_status",
	"compatibility_checked_at",
	"compatibility_fingerprint",
	"compatibility_report",
)


class ConnectorSettings(Document):
	def validate(self):
		# A form opened before the poller's last write carries stale status values; saving it must not
		# roll the status or fingerprint back (a stale fingerprint would also fake a drift event).
		for field in _PROFILE_STATUS_FIELDS:
			self.set(field, frappe.db.get_single_value("Connector Settings", field))
