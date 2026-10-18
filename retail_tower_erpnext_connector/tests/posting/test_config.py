# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Local unit tests for the pure Connector Settings config parsers.

The poller's `_load_*` stubs read Connector Settings (a frappe-coupled, bench-only act) and then
transform the raw DocType rows into the dict shapes UomMap / PreResolvedWarehouse / StoreCustomerMap
consume. That TRANSFORM is split out as pure functions in `config.py` so it is unit-testable here
WITHOUT frappe — the only bench-only part left is the three-line `frappe.get_doc` read.

This pays down the connector's test-infra debt for THIS slice's own code: the parsing logic (the
part with real branching: empty rows, blank values, duplicate keys) is covered locally; only the
frappe read shell is bench-probe-guarded.
"""

import pytest

from retail_tower_erpnext_connector.connector.posting import config as cfg


class TestParseUomMap:
    def test_parses_child_rows_to_unit_uom_dict(self):
        rows = [
            {"dp2_unit": "each", "erpnext_uom": "Nos"},
            {"dp2_unit": "kg", "erpnext_uom": "Kg"},
        ]
        assert cfg.parse_uom_map(rows) == {"each": "Nos", "kg": "Kg"}

    def test_empty_rows_yield_empty_map(self):
        # An unconfigured map is a clean empty dict — the UomMap then fails closed per-unit
        # (not a crash here). None (single-doctype unset child table) is treated as empty.
        assert cfg.parse_uom_map([]) == {}
        assert cfg.parse_uom_map(None) == {}

    def test_blank_or_partial_row_is_rejected(self):
        # A half-filled row (a unit with no UOM, or vice versa) is a config error, not a silent
        # skip — surfacing it beats a mapping that silently lacks an entry (Principle VI).
        with pytest.raises(cfg.ConfigError):
            cfg.parse_uom_map([{"dp2_unit": "each", "erpnext_uom": ""}])
        with pytest.raises(cfg.ConfigError):
            cfg.parse_uom_map([{"dp2_unit": "", "erpnext_uom": "Nos"}])

    def test_duplicate_unit_is_rejected(self):
        # Two rows mapping the same unit to different UOMs is ambiguous — fail loud, never let the
        # last-wins silently pick one (a posting would depend on row order).
        with pytest.raises(cfg.ConfigError):
            cfg.parse_uom_map(
                [
                    {"dp2_unit": "each", "erpnext_uom": "Nos"},
                    {"dp2_unit": "each", "erpnext_uom": "Unit"},
                ]
            )


class TestParseWarehouseMap:
    def test_parses_rows_to_store_warehouse_identity(self):
        rows = [{"store_id": "s1", "warehouse": "Stores - E2E"}]
        assert cfg.parse_warehouse_map(rows) == {
            "s1": {"doctype": "Warehouse", "name": "Stores - E2E"}
        }

    def test_empty_rows_yield_empty_map(self):
        assert cfg.parse_warehouse_map([]) == {}
        assert cfg.parse_warehouse_map(None) == {}

    def test_blank_row_is_rejected(self):
        with pytest.raises(cfg.ConfigError):
            cfg.parse_warehouse_map([{"store_id": "s1", "warehouse": ""}])

    def test_duplicate_store_is_rejected(self):
        with pytest.raises(cfg.ConfigError):
            cfg.parse_warehouse_map(
                [
                    {"store_id": "s1", "warehouse": "WH-A"},
                    {"store_id": "s1", "warehouse": "WH-B"},
                ]
            )

    def test_one_warehouse_for_two_stores_is_rejected_naming_the_stores(self):
        # RT-434 / RT-290 M-2, M-3: a Warehouse belongs to at most one store.
        with pytest.raises(cfg.ConfigError) as err:
            cfg.parse_warehouse_map(
                [
                    {"store_id": "s2", "warehouse": "WH-A"},
                    {"store_id": "s3", "warehouse": "WH-B"},
                    {"store_id": "s1", "warehouse": "WH-A"},
                ]
            )
        assert str(err.value) == (
            "warehouse map has warehouse 'WH-A' for more than one store: ['s1', 's2'] — a Warehouse "
            "belongs to at most one store (RT-290 M-2/M-3); map each store to its own Warehouse"
        )

    def test_a_warehouse_differing_only_in_case_is_the_same_warehouse(self):
        # Frappe document names are case-insensitive on MariaDB: both rows address one Warehouse.
        with pytest.raises(cfg.ConfigError, match=r"for more than one store: \['s1', 's2'\]"):
            cfg.parse_warehouse_map(
                [
                    {"store_id": "s1", "warehouse": "Stores - RT"},
                    {"store_id": "s2", "warehouse": "stores - rt"},
                ]
            )

    def test_distinct_warehouses_for_several_stores_parse(self):
        rows = [
            {"store_id": "s1", "warehouse": "WH-A"},
            {"store_id": "s2", "warehouse": "WH-B"},
        ]
        assert cfg.parse_warehouse_map(rows) == {
            "s1": {"doctype": "Warehouse", "name": "WH-A"},
            "s2": {"doctype": "Warehouse", "name": "WH-B"},
        }


class TestRequireConfiguredMaps:
    # Codex PR #23 P1: a half-configured connector (creds saved, a required map still empty) must
    # SKIP the tick (run_posting_poll catches the raise → posting.poll.skipped, sales stay pending),
    # NOT activate and mass-reject every pending sale as terminal validation. Every work-item has a
    # store + >=1 line, so an empty uom/warehouse/customer map => 100% rejection — indistinguishable
    # from "not yet configured". So an EMPTY required map raises ConfigError (→ skip).
    def test_all_maps_populated_passes(self):
        cfg.require_configured_maps(
            uom={"each": "Nos"},
            warehouse={"s1": {"doctype": "Warehouse", "name": "WH"}},
            customer={"s1": "C1"},
        )  # must not raise

    def test_empty_uom_map_raises(self):
        with pytest.raises(cfg.ConfigError):
            cfg.require_configured_maps(uom={}, warehouse={"s1": {}}, customer={"s1": "C1"})

    def test_empty_warehouse_map_raises(self):
        with pytest.raises(cfg.ConfigError):
            cfg.require_configured_maps(uom={"each": "Nos"}, warehouse={}, customer={"s1": "C1"})

    def test_empty_customer_map_raises(self):
        with pytest.raises(cfg.ConfigError):
            cfg.require_configured_maps(
                uom={"each": "Nos"}, warehouse={"s1": {}}, customer={}
            )

    def test_error_names_every_empty_map(self):
        # The skip-reason log should say WHICH maps are unconfigured, not just "a map is empty".
        with pytest.raises(cfg.ConfigError) as exc:
            cfg.require_configured_maps(uom={}, warehouse={}, customer={"s1": "C1"})
        msg = str(exc.value)
        assert "uom" in msg.lower() and "warehouse" in msg.lower()

    def test_does_not_swallow_partial_miss(self):
        # REGRESSION GUARD against over-correcting: a NON-empty map with a specific key absent is a
        # genuine terminal rejection (decision-table rows 5/8), NOT a skip. require_configured_maps
        # only gates the EMPTY (not-configured) state — it must not inspect individual keys, so a
        # populated-but-incomplete map passes the gate (the per-item UnmappedUnit/UnmappedStore
        # reject still fires downstream in frappe_glue, unchanged).
        cfg.require_configured_maps(
            uom={"each": "Nos"},  # "kg" absent — store B unmapped — still must NOT skip
            warehouse={"s1": {"doctype": "Warehouse", "name": "WH"}},
            customer={"s1": "C1"},
        )  # must not raise — partial config is "live", not "unconfigured"


class TestParseStoreCustomerMap:
    def test_parses_rows_to_store_customer_dict(self):
        rows = [{"store_id": "s1", "customer": "Walk-in Customer - RT"}]
        assert cfg.parse_store_customer_map(rows) == {"s1": "Walk-in Customer - RT"}

    def test_empty_rows_yield_empty_map(self):
        assert cfg.parse_store_customer_map([]) == {}
        assert cfg.parse_store_customer_map(None) == {}

    def test_blank_row_is_rejected(self):
        with pytest.raises(cfg.ConfigError):
            cfg.parse_store_customer_map([{"store_id": "s1", "customer": ""}])

    def test_duplicate_store_is_rejected(self):
        with pytest.raises(cfg.ConfigError):
            cfg.parse_store_customer_map(
                [
                    {"store_id": "s1", "customer": "C-A"},
                    {"store_id": "s1", "customer": "C-B"},
                ]
            )


class TestParseTenderModeMap:
    """RT-78 / RT-10 D5: ``{tender_method, mode_of_payment}`` rows → ``{method: Mode of Payment}``."""

    def test_parses_rows_to_method_mode_dict(self):
        rows = [
            {"tender_method": "cash", "mode_of_payment": "Cash"},
            {"tender_method": "card_external", "mode_of_payment": "Card Clearing"},
        ]
        assert cfg.parse_tender_mode_map(rows) == {"cash": "Cash", "card_external": "Card Clearing"}

    def test_empty_rows_yield_empty_map(self):
        # Legitimate while sales are tender-unknown; a tender-bearing sale then fails closed per item.
        assert cfg.parse_tender_mode_map(None) == {}
        assert cfg.parse_tender_mode_map([]) == {}

    def test_blank_row_is_rejected(self):
        with pytest.raises(cfg.ConfigError, match="incomplete"):
            cfg.parse_tender_mode_map([{"tender_method": "cash", "mode_of_payment": ""}])

    def test_duplicate_method_is_rejected(self):
        rows = [
            {"tender_method": "cash", "mode_of_payment": "Cash"},
            {"tender_method": "cash", "mode_of_payment": "Petty Cash"},
        ]
        with pytest.raises(cfg.ConfigError, match="duplicate"):
            cfg.parse_tender_mode_map(rows)

    def test_method_outside_the_wire_enum_is_rejected(self):
        # The Select field restricts the value; a row that bypassed it is a config error, loud.
        with pytest.raises(cfg.ConfigError, match="voucher"):
            cfg.parse_tender_mode_map([{"tender_method": "voucher", "mode_of_payment": "Voucher"}])

    def test_tender_map_is_not_a_required_map(self):
        # An empty tender map must NOT skip the poll: tender-unknown sales still post unpaid.
        cfg.require_configured_maps(uom={"each": "Nos"}, warehouse={"s": {}}, customer={"s": "C"})
