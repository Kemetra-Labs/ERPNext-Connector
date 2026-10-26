# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""T070/T071/T072 — local unit tests for UOM map, warehouse applier, money conformance.

UOM: signed Option A (connector-side unit→ERPNext-UOM map; unmapped → fail closed, FR-008).
Warehouse: apply the DP2-pre-resolved identity, never derive (FR-010, rider R5).
Money: every monetary field is exact-decimal string + ISO-4217 (FR-009). No frappe.
"""

import pytest

from retail_tower_erpnext_connector.connector.posting import builder as b
from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import uom as u


class TestUomMap:
    def test_maps_known_unit(self):
        resolver = u.UomMap({"each": "Nos", "kg": "Kg"})
        assert resolver.resolve("each") == "Nos"
        assert resolver.resolve("kg") == "Kg"

    def test_unmapped_unit_fails_closed(self):
        # FR-008: an unmapped unit raises UnmappedUnit (caller → validation), never defaults.
        resolver = u.UomMap({"each": "Nos"})
        with pytest.raises(b.UnmappedUnit):
            resolver.resolve("box")

    def test_resolve_is_the_builder_uom_callable(self):
        # The resolver plugs directly into build_sales_invoice's uom_for slot.
        resolver = u.UomMap({"each": "Nos"})
        assert callable(resolver.resolve)

    def test_map_is_case_sensitive_explicit(self):
        # No silent normalization — "Each" != "each" unless mapped (Principle VI, no guessing).
        resolver = u.UomMap({"each": "Nos"})
        with pytest.raises(b.UnmappedUnit):
            resolver.resolve("Each")


class TestWarehouseApplier:
    def test_applies_pre_resolved_identity(self):
        # FR-010 / rider R5: apply the DP2-pre-resolved warehouse generically; never derive.
        applier = u.PreResolvedWarehouse(
            {"33333333-3333-4333-8333-333333333333": {"doctype": "Warehouse", "name": "Main - RT"}}
        )
        wh = applier.for_store("33333333-3333-4333-8333-333333333333")
        assert wh == {"doctype": "Warehouse", "name": "Main - RT"}

    def test_unknown_store_is_upstream_violation(self):
        # A store with no pre-resolved warehouse should have DLQ'd in DP2 (R5); if it reaches
        # the connector, fail closed — never guess a warehouse.
        applier = u.PreResolvedWarehouse({})
        with pytest.raises(u.UnresolvedWarehouse):
            applier.for_store("unknown-store")


class TestStoreCustomerMap:
    # F-009 closure: the 012 work-item carries no customer identity, only Sale.store_id. The
    # operator maps each store to its ERPNext Customer (config, injected) — exactly parallel to
    # the warehouse map. A store with no mapping fails CLOSED (never fabricate a customer —
    # Principle VI), mapped by the caller to permanently_rejected / validation.
    def test_maps_known_store_to_customer(self):
        resolver = u.StoreCustomerMap(
            {"33333333-3333-4333-8333-333333333333": "Walk-in Customer - RT"}
        )
        assert resolver.for_store("33333333-3333-4333-8333-333333333333") == "Walk-in Customer - RT"

    def test_unmapped_store_fails_closed(self):
        # No default customer — an unmapped store raises UnmappedStore (caller → validation),
        # never silently posts to some fallback customer (would hide a config gap; Principle VI).
        resolver = u.StoreCustomerMap({})
        with pytest.raises(u.UnmappedStore):
            resolver.for_store("unknown-store")

    def test_for_store_is_the_builder_customer_callable(self):
        # The resolver plugs directly into build_sales_invoice's customer_for slot.
        resolver = u.StoreCustomerMap({"s1": "C1"})
        assert callable(resolver.for_store)

    def test_map_is_case_sensitive_explicit(self):
        # No silent normalization — store ids are opaque; a differently-cased id fails closed.
        resolver = u.StoreCustomerMap({"Store-A": "C1"})
        with pytest.raises(u.UnmappedStore):
            resolver.for_store("store-a")


class TestAsciiDigitsOnly:
    """RT-468 (S6353): the money/quantity patterns use a digit class with re.ASCII (ASCII only)."""

    @pytest.mark.parametrize(
        "pattern",
        [u._DECIMAL_RE, u._QUANTITY_RE, c._NON_NEGATIVE_DECIMAL_RE, c._RETURN_QUANTITY_RE],
    )
    def test_non_ascii_digits_rejected(self, pattern):
        assert pattern.match("12.50")
        assert not pattern.match(f"{chr(0x661)}{chr(0x662)}.{chr(0x665)}{chr(0x660)}")  # Arabic-Indic digits
        assert not pattern.match(f"{chr(0xFF11)}{chr(0xFF12)}")  # fullwidth digits


class TestMoneyConformance:
    def test_passes_clean_invoice(self):
        doc = {
            "currency": "EGP",
            "items": [{"rate": "100.00", "amount": "200.00", "qty": "2", "currency": "EGP"}],
        }
        # Should not raise.
        u.assert_money_conformance(doc)

    def test_rejects_float_amount(self):
        doc = {"currency": "EGP", "items": [{"rate": 100.0, "amount": "200.00", "qty": "2"}]}
        with pytest.raises(u.MoneyConformanceError):
            u.assert_money_conformance(doc)

    def test_rejects_missing_currency(self):
        doc = {"items": [{"rate": "100.00", "amount": "200.00", "qty": "2"}]}
        with pytest.raises(u.MoneyConformanceError):
            u.assert_money_conformance(doc)

    def test_rejects_bad_currency_code(self):
        doc = {"currency": "egp", "items": [{"rate": "100.00", "amount": "200.00", "qty": "2"}]}
        with pytest.raises(u.MoneyConformanceError):
            u.assert_money_conformance(doc)

    def test_rejects_more_than_four_fractional_digits(self):
        # F-006: 012 DecimalAmount allows at most 4 fractional digits; 5+ must be rejected
        # (the connector must not pass a value DP2's wire schema rejects).
        doc = {"currency": "EGP", "items": [{"rate": "100.12345", "amount": "200.00", "qty": "2"}]}
        with pytest.raises(u.MoneyConformanceError):
            u.assert_money_conformance(doc)

    def test_allows_exactly_four_fractional_digits(self):
        doc = {"currency": "EGP", "items": [{"rate": "100.1234", "amount": "200.0000", "qty": "2"}]}
        u.assert_money_conformance(doc)  # must not raise

    def test_allows_six_fractional_digits_on_quantity(self):
        # 012 SaleLine.quantity allows up to 6 fractional digits (weighed goods, e.g. kg).
        # qty must NOT be validated under the 4-digit money regex (that would false-reject a
        # valid sale — a Principle VI inversion).
        doc = {"currency": "EGP", "items": [{"rate": "10.00", "amount": "12.34", "qty": "1.234567"}]}
        u.assert_money_conformance(doc)  # must not raise

    def test_rejects_seven_fractional_digits_on_quantity(self):
        doc = {"currency": "EGP", "items": [{"rate": "10.00", "amount": "12.34", "qty": "1.2345678"}]}
        with pytest.raises(u.MoneyConformanceError):
            u.assert_money_conformance(doc)

    def test_rejects_float_quantity(self):
        doc = {"currency": "EGP", "items": [{"rate": "10.00", "amount": "12.34", "qty": 1.5}]}
        with pytest.raises(u.MoneyConformanceError):
            u.assert_money_conformance(doc)

    def test_rejects_float_price_list_rate(self):
        # RT-377: price_list_rate is money too (exact-decimal string, never float — FR-009).
        doc = {
            "currency": "EGP",
            "items": [{"rate": "10.00", "amount": "10.00", "qty": "1", "price_list_rate": 10.0}],
        }
        with pytest.raises(u.MoneyConformanceError):
            u.assert_money_conformance(doc)

    @pytest.mark.parametrize("value", [1.0, "1.00001", "abc"])
    def test_rejects_a_non_conformant_discount_amount(self, value):
        # RT-857: the per-unit discount_amount is money too (exact-decimal string, never float).
        doc = {
            "currency": "EGP",
            "items": [
                {"rate": "9.00", "amount": "9.00", "qty": "1", "price_list_rate": "10.00", "discount_amount": value}
            ],
        }
        with pytest.raises(u.MoneyConformanceError):
            u.assert_money_conformance(doc)

    def test_rejects_non_decimal_price_list_rate(self):
        doc = {
            "currency": "EGP",
            "items": [{"rate": "10.00", "amount": "10.00", "qty": "1", "price_list_rate": "10.12345"}],
        }
        with pytest.raises(u.MoneyConformanceError):
            u.assert_money_conformance(doc)

    def test_conforms_to_real_builder_output(self):
        # T072: the conformance check passes over an actual build_sales_invoice output —
        # it asserts, it does not re-build (no second money path).
        wi = _work_item()
        doc = b.build_sales_invoice(
            wi,
            uom_for=u.UomMap({"each": "Nos"}).resolve,
            warehouse_for=u.PreResolvedWarehouse(
                {"33333333-3333-4333-8333-333333333333": {"doctype": "Warehouse", "name": "Main - RT"}}
            ).for_store,
            customer_for=u.StoreCustomerMap(
                {"33333333-3333-4333-8333-333333333333": "Walk-in Customer - RT"}
            ).for_store,
        )
        u.assert_money_conformance(doc)


def _work_item():
    from retail_tower_erpnext_connector.connector.posting import contracts as c

    return c.PostingWorkItem.from_wire(
        {
            "workItemRef": "11111111-1111-4111-8111-111111111111",
            "kind": "sale_post",
            "sourceSystem": "pos-pulse",
            "externalId": "POS-9001",
            "payloadHash": "a" * 64,
            "businessDate": "2026-06-01",
            "itemCursor": "cursor-1",
            "sale": {
                "saleRef": "22222222-2222-4222-8222-222222222222",
                "storeId": "33333333-3333-4333-8333-333333333333",
                "currencyCode": "EGP",
                "posTotal": "200.00",
                "occurredAt": "2026-06-01T10:00:00Z",
                "businessDate": "2026-06-01",
                "sourceSystem": "pos-pulse",
                "externalId": "POS-9001",
                "lines": [
                    {
                        "lineName": "Item A",
                        "unitPrice": "100.00",
                        "currencyCode": "EGP",
                        "quantity": "2",
                        "lineAmount": "200.00",
                        "unit": "each",
                        "erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
                    }
                ],
            },
        }
    )
