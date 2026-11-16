# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1427 — the back-dated role decision (pure), against ERPNext's own rule.

ERPNext (StockLedgerEntry.validate_with_last_transaction_posting_time) restricts back-dating only
when a role is set AND some user holds it; then only holders may back-date. A check that cannot
judge reports ``unknown``, never ``verified``.
"""

import pytest

from retail_tower_erpnext_connector.connector.health.back_dated_role import (
	MISSING,
	UNKNOWN,
	VERIFIED,
	BackDatedRole,
	assess_back_dated_role,
)


@pytest.mark.parametrize(
	("role", "holders", "user", "expected"),
	[
		("Stock Backdater", ["rt-connector@x", "boss@x"], "rt-connector@x", VERIFIED),  # role present
		("Stock Backdater", ["boss@x"], "rt-connector@x", MISSING),  # role missing
		(None, ["boss@x"], "rt-connector@x", VERIFIED),  # no role set: nobody is restricted
		("", [], "rt-connector@x", VERIFIED),
		("Stock Backdater", [], "rt-connector@x", VERIFIED),  # role set, held by no one: ERPNext skips
		("Stock Backdater", ["boss@x"], None, UNKNOWN),  # no user to judge
		("Stock Backdater", ["boss@x"], "", UNKNOWN),
	],
)
def test_the_decision_mirrors_erpnext(role, holders, user, expected):
	assert assess_back_dated_role(allowed_role=role, role_holders=holders, user=user) == expected


def test_the_wire_form_carries_status_and_time():
	assert BackDatedRole(status=MISSING, checked_at="t").to_wire() == {"status": "missing", "checkedAt": "t"}


def test_a_status_outside_the_contract_cannot_be_built_for_the_wire():
	with pytest.raises(ValueError):
		BackDatedRole(status="ok", checked_at="t").to_wire()
