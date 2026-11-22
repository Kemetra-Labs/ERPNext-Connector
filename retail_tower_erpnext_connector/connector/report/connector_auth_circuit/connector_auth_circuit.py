# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1431 — Script Report "Connector Auth Circuit": whether Backend-Core refuses the Connector credential.

One row, read live from the shared cache the RT-1429 circuit keeps: state (closed or open), the last HTTP
status, how many refusals in a row, when the circuit opened and when the next probe is due. It stores
nothing, writes nothing (a poller tick is never touched) and shows no credential id, token or fingerprint.

Frappe reaches ``execute`` through ``frappe.get_attr`` (a standard report in the module folder), so no
whitelisted method is needed. The same values are available to scripts through
``bench execute retail_tower_erpnext_connector.connector.auth_circuit_store.read_auth_circuit_status``.
"""

from __future__ import annotations

from retail_tower_erpnext_connector.connector.auth_circuit_status import AuthCircuitStatus

COLUMNS = [
	{"label": "State", "fieldname": "state", "fieldtype": "Data", "width": 90},
	{"label": "Last Status", "fieldname": "last_status", "fieldtype": "Int", "width": 100},
	{"label": "Refusals In A Row", "fieldname": "failures", "fieldtype": "Int", "width": 140},
	{"label": "Opened At", "fieldname": "opened_at", "fieldtype": "Datetime", "width": 170},
	{"label": "Next Probe At", "fieldname": "next_probe_at", "fieldtype": "Datetime", "width": 170},
	{"label": "Note", "fieldname": "note", "fieldtype": "Data", "width": 420},
]


def execute(filters=None):
	from retail_tower_erpnext_connector.connector.auth_circuit_store import read_auth_circuit

	return COLUMNS, [to_row(read_auth_circuit())]


def to_row(status: AuthCircuitStatus) -> dict:
	"""The view as a report row, with the times in the site's timezone (naive, as Desk datetimes are)."""
	return {
		"state": status.state,
		"last_status": status.last_status,
		"failures": status.failures,
		"opened_at": _site_time(status.opened_at),
		"next_probe_at": _site_time(status.next_probe_at),
		"note": status.note,
	}


def _site_time(moment):
	if moment is None:
		return None
	from frappe.utils import convert_utc_to_system_timezone

	return convert_utc_to_system_timezone(moment).replace(tzinfo=None)
