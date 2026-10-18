# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-434 — the pure rule a feed-supplied ``sale.warehouseRef`` must meet before posting (no frappe).

The same leaf / same-company rules the compatibility preflight applies to the local map: a group
Warehouse or one of another company fails closed; so does a Warehouse that does not exist.
"""

import pytest

from retail_tower_erpnext_connector.connector.posting import warehouse_policy as wp


def test_a_leaf_warehouse_of_the_sale_company_passes():
	wp.assert_postable_warehouse("Frozen WH", {"company": "RT Company", "is_group": 0}, "RT Company")


def test_a_group_warehouse_is_rejected():
	with pytest.raises(wp.FrozenWarehouseRejected) as err:
		wp.assert_postable_warehouse("All WH", {"company": "RT Company", "is_group": 1}, "RT Company")
	assert str(err.value) == (
		"feed warehouseRef 'All WH' is a group warehouse (cannot hold stock) — rejected as validation "
		"before insert (RT-434)"
	)


def test_a_warehouse_of_another_company_is_rejected():
	with pytest.raises(wp.FrozenWarehouseRejected) as err:
		wp.assert_postable_warehouse("Other WH", {"company": "Other Co", "is_group": 0}, "RT Company")
	assert str(err.value) == (
		"feed warehouseRef 'Other WH' belongs to company 'Other Co', not the sale company 'RT Company' "
		"— rejected as validation before insert (RT-434)"
	)


def test_a_missing_warehouse_is_rejected():
	with pytest.raises(wp.FrozenWarehouseRejected) as err:
		wp.assert_postable_warehouse("Gone WH", None, "RT Company")
	assert str(err.value) == (
		"feed warehouseRef 'Gone WH' does not exist on this site — rejected as validation before insert "
		"(RT-434)"
	)
