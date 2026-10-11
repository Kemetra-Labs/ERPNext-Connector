# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""T030/T033/T034 — local unit tests for the work-item → Sales-Invoice builder.

Pure dict-transform: it APPLIES each line's pre-resolved erpnextItemRef (no resolution),
represents money as exact-decimal strings (never float), carries businessDate → posting_date,
and addresses every ERPNext doc generically as {doctype, name}. No frappe, no ERPNext call.
"""

import pytest

from retail_tower_erpnext_connector.connector.posting import builder as b
from retail_tower_erpnext_connector.connector.posting import contracts as c


def _work_item(**overrides) -> c.PostingWorkItem:
    line = {
        "lineName": "Item A",
        "unitPrice": "100.00",
        "currencyCode": "EGP",
        "quantity": "2",
        "lineAmount": "200.00",
        "taxAmount": "30.00",
        "unit": "each",
        "erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
    }
    wire = {
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
            "posTotal": "230.00",
            "occurredAt": "2026-06-01T10:00:00Z",
            "businessDate": "2026-06-01",
            "sourceSystem": "pos-pulse",
            "externalId": "POS-9001",
            "lines": [line],
        },
    }
    wire.update(overrides)
    return c.PostingWorkItem.from_wire(wire)


# A trivial UOM resolver for the builder under test: maps "each" → "Nos".
def _uom(unit: str) -> str:
    mapping = {"each": "Nos"}
    if unit not in mapping:
        raise b.UnmappedUnit(unit)
    return mapping[unit]


def _warehouse(store_id: str) -> dict:
    # The DP2-pre-resolved warehouse identity, applied generically (rider R5).
    return {"doctype": "Warehouse", "name": "Main - RT"}


def _customer(store_id: str) -> str:
    # F-009: the operator-configured store→customer map (the 012 work-item carries no customer).
    return "Walk-in Customer - RT"


class TestBuilderHappyPath:
    def test_builds_one_sales_invoice_doc(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert doc["doctype"] == "Sales Invoice"
        assert len(doc["items"]) == 1

    def test_applies_erpnext_item_ref_as_item_code(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        # T030: the line references the APPLIED Item by name; no resolution happened.
        assert doc["items"][0]["item_code"] == "ITEM-A"

    def test_carries_business_date_as_posting_date(self):
        # T033: businessDate → posting_date (never the connector's post-time).
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert doc["posting_date"] == "2026-06-01"

    def test_carries_provenance(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert doc["rt_source_system"] == "pos-pulse"
        assert doc["rt_external_id"] == "POS-9001"

    def test_applies_uom_and_warehouse(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert doc["items"][0]["uom"] == "Nos"
        assert doc["items"][0]["warehouse"] == "Main - RT"


class TestBuilderCustomer:
    # F-009 closure: build_sales_invoice now takes a customer_for(store_id)->str resolver and
    # emits doc['customer']. The customer comes from operator config (store→customer map), never
    # fabricated by the builder. A store with no mapping fails closed (UnmappedStore propagates).
    def test_emits_customer_from_resolver(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert doc["customer"] == "Walk-in Customer - RT"

    def test_customer_resolver_receives_store_id(self):
        seen = {}

        def _capture(store_id: str) -> str:
            seen["store_id"] = store_id
            return "C1"

        b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_capture
        )
        # The customer is resolved from the SALE's store_id (the only 012 identifier an operator
        # can map), not the line or work-item ref.
        assert seen["store_id"] == "33333333-3333-4333-8333-333333333333"

    def test_unmapped_store_propagates_unmapped_store(self):
        # A store with no customer mapping must fail closed — the builder does not invent a
        # customer (Principle VI). The caller maps UnmappedStore → permanently_rejected/validation.
        from retail_tower_erpnext_connector.connector.posting import uom as u

        def _unmapped(store_id: str) -> str:
            raise u.UnmappedStore(store_id)

        with pytest.raises(u.UnmappedStore):
            b.build_sales_invoice(
                _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_unmapped
            )


class TestBuilderMoneyFidelity:
    def test_money_is_exact_decimal_strings_not_float(self):
        # T033: every monetary field stays an exact-decimal string; no float anywhere.
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        item = doc["items"][0]
        assert item["rate"] == "100.00"
        assert item["amount"] == "200.00"
        assert item["qty"] == "2"
        for value in (item["rate"], item["amount"], item["qty"]):
            assert isinstance(value, str)
            assert not isinstance(value, float)

    def test_currency_code_present(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert doc["currency"] == "EGP"


class TestBuilderSelfValidatesMoney:
    def test_builder_output_is_money_conformant(self):
        # F-007: build_sales_invoice enforces money conformance on its OWN output before
        # returning — FR-009 is enforced in the path, not only by a separately-callable check.
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        # The builder must have validated; re-asserting here proves the contract holds.
        from retail_tower_erpnext_connector.connector.posting import uom as u

        u.assert_money_conformance(doc)

    def test_builder_rejects_a_non_decimal_money_value(self):
        # If a line's money arrives as a non-DecimalAmount string (e.g. upstream "100.0" is
        # fine, but "100.123456" is not), the builder must fail closed, not emit it.
        wi = _work_item(
            sale={
                "saleRef": "22222222-2222-4222-8222-222222222222",
                "storeId": "33333333-3333-4333-8333-333333333333",
                "currencyCode": "EGP",
                "posTotal": "230.00",
                "occurredAt": "2026-06-01T10:00:00Z",
                "businessDate": "2026-06-01",
                "sourceSystem": "pos-pulse",
                "externalId": "POS-9001",
                "lines": [
                    {
                        "lineName": "Item A",
                        "unitPrice": "100.123456",  # 6 fractional digits — not a DecimalAmount
                        "currencyCode": "EGP",
                        "quantity": "1",
                        "lineAmount": "100.123456",
                        "unit": "each",
                        "erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
                    }
                ],
            }
        )
        from retail_tower_erpnext_connector.connector.posting import uom as u

        with pytest.raises(u.MoneyConformanceError):
            b.build_sales_invoice(
                wi, uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
            )


class TestBuilderFailClosed:
    def test_missing_erpnext_item_ref_is_rejected_at_parse(self):
        # T034: an offered line lacking erpnextItemRef can't even parse into a SaleLine
        # (MissingErpnextItemRef) — it never reaches the builder as a partial doc.
        bad_line = {
            "lineName": "Item A",
            "unitPrice": "100.00",
            "currencyCode": "EGP",
            "quantity": "1",
            "lineAmount": "100.00",
            "unit": "each",
        }
        with pytest.raises(c.MissingErpnextItemRef):
            c.SaleLine.from_wire(bad_line)

    def test_unmapped_unit_raises_unmapped_unit(self):
        # T034/T070 seam: an unmapped unit fails closed (caller maps → validation).
        wi = _work_item()

        def _uom_unmapped(unit: str) -> str:
            raise b.UnmappedUnit(unit)

        with pytest.raises(b.UnmappedUnit):
            b.build_sales_invoice(
                wi, uom_for=_uom_unmapped, warehouse_for=_warehouse, customer_for=_customer
            )

    def test_builder_never_resolves_item_identity(self):
        # The builder takes erpnextItemRef as-is; there is no resolution hook to pass.
        import inspect

        sig = inspect.signature(b.build_sales_invoice)
        # Only uom_for + warehouse_for are injected — NO item-resolver parameter exists.
        assert "resolve" not in str(sig).lower()
        assert "item_for" not in sig.parameters


class TestStockEffect:
    """RT-48 (owner decision RT-47 D1): the POS sale Sales Invoice IS the stock-moving document.

    ``update_stock=1`` makes ERPNext write the Stock Ledger Entries inside the SAME submit as the
    invoice, so the existing SI idempotency (Posting Log + ``unique_rt_si_provenance``) is also the
    stock exactly-once guarantee. No Delivery Note (D1).
    """

    def test_sale_invoice_moves_stock(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert doc["update_stock"] == 1

    def test_every_line_carries_the_mapped_store_warehouse(self):
        # update_stock posts each Stock Ledger Entry against the line's warehouse — it must be the
        # DP2-pre-resolved store warehouse (never a guessed/default one).
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert all(item["warehouse"] == "Main - RT" for item in doc["items"])



class TestNoRounding:
    """RT-80 (F-R1, bench RT-75 T6a/T6b): ERPNext must not round a fractional POS total.

    With the site default (Global Defaults ``disable_rounded_total=0``) a 10.49 EGP sale posts
    ``rounded_total`` 10.00 and AR outstanding 10.00, with 0.49 booked to Round Off, so ERPNext AR
    no longer reconciles to DP2 ``posTotal``. The builder sets the flag per document instead of
    relying on a site setting (Principle VI: the posted total is the POS total, exactly).
    """

    def test_sale_invoice_disables_rounded_total(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert doc["disable_rounded_total"] == 1


class TestPostingTime:
    """RT-49 (decision 10312): ERPNext overwrites posting_date unless set_posting_time=1."""

    def test_sets_set_posting_time_so_business_date_is_kept(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert doc["set_posting_time"] == 1
        assert doc["posting_date"] == "2026-06-01"

    def test_default_stamp_uses_occurred_at_in_utc(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert doc["posting_time"] == "10:00:00.000000"

    def test_applies_the_injected_stamp(self):
        from retail_tower_erpnext_connector.connector.posting import posting_time as pt

        stamp = pt.PostingStamp("2026-06-01", "13:00:00.000000")
        doc = b.build_sales_invoice(
            _work_item(),
            uom_for=_uom,
            warehouse_for=_warehouse,
            customer_for=_customer,
            posting_stamp=stamp,
        )
        assert (doc["set_posting_time"], doc["posting_date"], doc["posting_time"]) == (
            1,
            "2026-06-01",
            "13:00:00.000000",
        )


def _mode(method: str) -> str:
    # The operator-configured tender → Mode of Payment map (RT-10 D5), fail closed on a miss.
    from retail_tower_erpnext_connector.connector.posting import tender as t

    return t.TenderModeMap({"cash": "Cash", "card_external": "Card Clearing"}).resolve(method)


def _tendered(tenders, *, tax_amount=None, pos_total="200.00") -> c.PostingWorkItem:
    """A sale whose line total is 200.00 (no tax unless given), carrying ``tenders``."""
    line = {
        "lineName": "Item A",
        "unitPrice": "100.00",
        "currencyCode": "EGP",
        "quantity": "2",
        "lineAmount": "200.00",
        "taxAmount": tax_amount,
        "unit": "each",
        "erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
    }
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
                "posTotal": pos_total,
                "occurredAt": "2026-06-01T10:00:00Z",
                "businessDate": "2026-06-01",
                "sourceSystem": "pos-pulse",
                "externalId": "POS-9001",
                "lines": [line],
                "tenders": tenders,
            },
        }
    )


def _settle(work_item, **kw):
    return b.build_sales_invoice(
        work_item,
        uom_for=_uom,
        warehouse_for=_warehouse,
        customer_for=_customer,
        mode_of_payment_for=kw.pop("mode_of_payment_for", _mode),
        **kw,
    )


class TestSettlement:
    """RT-78 / RT-10 D3(b): a tender-bearing sale posts as ONE paid Sales Invoice (is_pos=1).

    One ``payments`` row per tender, amount verbatim (exact-decimal string). No Payment Entry.
    """

    def test_cash_sale_is_a_paid_pos_invoice(self):
        doc = _settle(_tendered([{"method": "cash", "amount": "200.00"}]))
        assert doc["is_pos"] == 1
        assert doc["payments"] == [{"mode_of_payment": "Cash", "amount": "200.00"}]
        # RT-80 / amendment §3: never round a settled total (RT-75 T3a booked a false 0.49 change).
        assert doc["disable_rounded_total"] == 1

    def test_split_tender_keeps_one_row_per_tender_in_wire_order(self):
        doc = _settle(
            _tendered(
                [
                    {"method": "cash", "amount": "150.00"},
                    {"method": "card_external", "amount": "50.00", "reference": "AB12"},
                ]
            )
        )
        assert doc["payments"] == [
            {"mode_of_payment": "Cash", "amount": "150.00"},
            {"mode_of_payment": "Card Clearing", "amount": "50.00"},
        ]

    def test_tender_unknown_sale_builds_exactly_as_before(self):
        # RT-10 D8: no tenders → the unpaid invoice of R1's interim mode, no settlement keys.
        for tenders in (None, []):
            doc = _settle(_tendered(tenders))
            assert "is_pos" not in doc and "payments" not in doc

    def test_tender_unknown_sale_needs_no_tender_resolver(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert "payments" not in doc

    def test_unmapped_method_fails_closed(self):
        from retail_tower_erpnext_connector.connector.posting import tender as t

        only_cash = t.TenderModeMap({"cash": "Cash"}).resolve
        with pytest.raises(t.UnmappedTender, match="card_external"):
            _settle(
                _tendered([{"method": "card_external", "amount": "200.00"}]),
                mode_of_payment_for=only_cash,
            )

    def test_tenders_without_a_resolver_fail_closed(self):
        from retail_tower_erpnext_connector.connector.posting import tender as t

        with pytest.raises(t.UnmappedTender):
            b.build_sales_invoice(
                _tendered([{"method": "cash", "amount": "200.00"}]),
                uom_for=_uom,
                warehouse_for=_warehouse,
                customer_for=_customer,
            )

    def test_tender_total_must_equal_the_line_total(self):
        from retail_tower_erpnext_connector.connector.posting import tender as t

        with pytest.raises(t.TenderMismatch, match=r"199.99"):
            _settle(_tendered([{"method": "cash", "amount": "199.99"}]))

    def test_pos_total_that_differs_from_the_lines_is_rejected(self):
        # Amendment §4a: tenders sum to posTotal (230 incl. tax) but the invoice is built from the
        # lines (200). Never adjust a line, invent change or leave a partial balance to force a match.
        from retail_tower_erpnext_connector.connector.posting import tender as t

        with pytest.raises(t.TenderMismatch):
            _settle(
                _tendered([{"method": "cash", "amount": "230.00"}], tax_amount="30.00", pos_total="230.00")
            )

    def test_totals_compare_as_exact_decimals(self):
        doc = _settle(
            _tendered([{"method": "cash", "amount": "100"}, {"method": "cash", "amount": "100.0000"}])
        )
        assert [p["amount"] for p in doc["payments"]] == ["100", "100.0000"]

    def test_float_payment_amount_is_not_money_conformant(self):
        from retail_tower_erpnext_connector.connector.posting import uom as u

        doc = _settle(_tendered([{"method": "cash", "amount": "200.00"}]))
        doc["payments"][0]["amount"] = 200.0
        with pytest.raises(u.MoneyConformanceError, match="payments"):
            u.assert_money_conformance(doc)


class TestLineRef:
    """RT-16 / RT-14 D6: every Sales Invoice Item carries its sale line's ``lineRef``.

    A later partial return is matched to the ORIGINAL invoice row by this value (the
    ``Sales Invoice Item-rt_line_ref`` custom field), never by position.
    """

    def test_items_carry_the_sale_line_ref(self):
        line = {
            "lineName": "Item A",
            "unitPrice": "100.00",
            "currencyCode": "EGP",
            "quantity": "2",
            "lineAmount": "200.00",
            "unit": "each",
            "erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
            "lineRef": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        }
        sale = {
            "saleRef": "22222222-2222-4222-8222-222222222222",
            "storeId": "33333333-3333-4333-8333-333333333333",
            "currencyCode": "EGP",
            "posTotal": "200.00",
            "occurredAt": "2026-06-01T10:00:00Z",
            "businessDate": "2026-06-01",
            "sourceSystem": "pos-pulse",
            "externalId": "POS-9001",
            "lines": [line],
        }
        doc = b.build_sales_invoice(
            _work_item(sale=sale), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert [i["rt_line_ref"] for i in doc["items"]] == ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]

    def test_an_older_feed_without_line_ref_writes_none(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert "rt_line_ref" not in doc["items"][0]


class TestRepricingProtection:
    """RT-377 / RT-283 design A, decisions 657 + D6 (evidence RT-376).

    ERPNext must not reprice a Retail Tower sale. With no flag, an enabled selling Pricing Rule
    rewrote 9.50 to 9.00 on submit; with only the flag, ERPNext still filled ``price_list_rate``
    from its own price list and recorded an invented discount or margin. The builder therefore sends
    both: the per-document ``ignore_pricing_rule`` and ``price_list_rate`` equal to the line rate.
    """

    def test_sets_ignore_pricing_rule_on_the_document(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert doc["ignore_pricing_rule"] == 1

    def test_sends_price_list_rate_equal_to_rate_on_every_line(self):
        line_b = {
            "lineName": "Item B",
            "unitPrice": "11.25",
            "currencyCode": "EGP",
            "quantity": "3",
            "lineAmount": "33.75",
            "taxAmount": "0.00",
            "unit": "each",
            "erpnextItemRef": {"doctype": "Item", "name": "ITEM-B"},
        }
        item = _work_item()
        wire_lines = [
            {
                "lineName": "Item A",
                "unitPrice": "100.00",
                "currencyCode": "EGP",
                "quantity": "2",
                "lineAmount": "200.00",
                "taxAmount": "30.00",
                "unit": "each",
                "erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
            },
            line_b,
        ]
        sale = {
            "saleRef": item.sale.sale_ref,
            "storeId": item.sale.store_id,
            "currencyCode": "EGP",
            "posTotal": "263.75",
            "occurredAt": "2026-06-01T10:00:00Z",
            "businessDate": "2026-06-01",
            "sourceSystem": "pos-pulse",
            "externalId": "POS-9001",
            "lines": wire_lines,
        }
        doc = b.build_sales_invoice(
            _work_item(sale=sale), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
        )
        assert [(i["rate"], i["price_list_rate"]) for i in doc["items"]] == [
            ("100.00", "100.00"),
            ("11.25", "11.25"),
        ]
