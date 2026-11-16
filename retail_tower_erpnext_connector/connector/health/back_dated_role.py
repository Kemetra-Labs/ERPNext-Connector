# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Back-dated stock role readiness (RT-1427) — the pure decision, NO frappe.

A count Stock Reconciliation is posted back-dated to the Count Cut time. ERPNext decides who may
create or edit a back-dated stock transaction in ``StockLedgerEntry.validate_with_last_transaction_posting_time``
(erpnext/stock/doctype/stock_ledger_entry/stock_ledger_entry.py, Stock Settings field
``role_allowed_to_create_edit_back_dated_transactions``): when a role is set and at least one user
holds it, only the users holding it may back-date; with no role set, or no holder, nothing restricts
the posting user. This module mirrors exactly that rule, so the answer matches what ERPNext will do.

The three results are the contract's ``backDatedRole.status``. ``unknown`` is what a check that
could not run reports; it is never promoted to ``verified``. The caller reads ERPNext (read-only) and
passes the values in, so this module touches nothing.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

VERIFIED = "verified"
MISSING = "missing"
UNKNOWN = "unknown"
STATUSES = (VERIFIED, MISSING, UNKNOWN)


@dataclass(frozen=True)
class BackDatedRole:
	"""One readiness result: ``status`` plus the Connector-clock time of the check (wire string)."""

	status: str
	checked_at: str

	def to_wire(self) -> dict:
		if self.status not in STATUSES:
			raise ValueError(f"backDatedRole.status must be one of {STATUSES}, got {self.status!r}")
		return {"status": self.status, "checkedAt": self.checked_at}


def assess_back_dated_role(
	*, allowed_role: str | None, role_holders: Collection[str], user: str | None
) -> str:
	"""The posting user's back-dated permission, by ERPNext's own rule.

	``allowed_role`` is Stock Settings' ``role_allowed_to_create_edit_back_dated_transactions``;
	``role_holders`` the users holding it (``Has Role`` rows); ``user`` the session user the
	Connector posts as. No user to judge is ``unknown``.
	"""
	if not user:
		return UNKNOWN
	if not allowed_role:
		return VERIFIED  # no role set: ERPNext restricts nobody
	if not role_holders:
		return VERIFIED  # role set but held by no one: ERPNext's check skips (authorized_users empty)
	return VERIFIED if user in role_holders else MISSING
