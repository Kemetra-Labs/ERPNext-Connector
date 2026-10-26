# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""T030/T033/T034 — local unit tests for the work-item → Sales-Invoice builder.

Pure dict-transform: it APPLIES each line's pre-resolved erpnextItemRef (no resolution),
represents money as exact-decimal strings (never float), carries businessDate → posting_date,
and addresses every ERPNext doc generically as {doctype, name}. No frappe, no ERPNext call.
"""

from decimal import Decimal

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


_REF_A = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
_REF_B = "16fd2706-8baf-433b-82eb-8c7fada847da"


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

    def test_same_method_split_tender_posts_one_row_per_tender_in_sequence_order(self):
        # RT-361 (G1): two cash tender lines stay two Cash rows; the wire order is not the row order.
        doc = _settle(
            _tendered(
                [
                    {"method": "cash", "amount": "50.00", "tenderRef": _REF_B, "sequence": 2},
                    {"method": "cash", "amount": "150.00", "tenderRef": _REF_A, "sequence": 1},
                ]
            )
        )
        assert doc["is_pos"] == 1
        assert doc["payments"] == [
            {"mode_of_payment": "Cash", "amount": "150.00"},
            {"mode_of_payment": "Cash", "amount": "50.00"},
        ]
        # RT-264 D4: the tender line identity stays off the ERPNext document.
        assert _REF_A not in str(doc) and _REF_B not in str(doc)

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


class TestExplicitCompany:
    """RT-378 review finding 2: the company is set explicitly from the site default (one source of
    truth with the representation check), never left to ERPNext's user-scoped default."""

    def test_sets_the_given_company(self):
        doc = b.build_sales_invoice(
            _work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer, company="RT Co"
        )
        assert doc["company"] == "RT Co"

    def test_without_a_company_the_document_carries_none(self):
        doc = b.build_sales_invoice(_work_item(), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer)
        assert "company" not in doc


def _line_sale(unit_price, quantity, line_amount, currency="EGP", tenders=None) -> dict:
    """A one-line sale wire; ``line_amount`` below ``unit_price x quantity`` carries a manual discount."""
    sale = {
        "saleRef": "22222222-2222-4222-8222-222222222222",
        "storeId": "33333333-3333-4333-8333-333333333333",
        "currencyCode": currency,
        "posTotal": line_amount,
        "occurredAt": "2026-06-01T10:00:00Z",
        "businessDate": "2026-06-01",
        "sourceSystem": "pos-pulse",
        "externalId": "POS-9001",
        "lines": [
            {
                "lineRef": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "lineName": "Item A",
                "unitPrice": unit_price,
                "currencyCode": currency,
                "quantity": quantity,
                "lineAmount": line_amount,
                "unit": "each",
                "erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
            }
        ],
    }
    if tenders is not None:
        sale["tenders"] = tenders
    return sale


def _build_line(*args, **kw) -> dict:
    doc = b.build_sales_invoice(
        _work_item(sale=_line_sale(*args, **kw)), uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer
    )
    return doc["items"][0]


def _erpnext_v16_amount(item: dict, minor: int = 2) -> Decimal:
    """The row amount ERPNext v16 would persist for this row (``taxes_and_totals`` emulation).

    ``calculate_item_rate``: no margin, so ``rate_with_margin = price_list_rate``; a non-zero rate equal
    to ``price_list_rate - discount_amount`` is kept (early exit) and a zero rate is replaced by it.
    ``calculate_item_values``: ``amount = flt(rate x qty, precision)``. Exact decimals stand in for
    Frappe's float + ``flt``: the builder only sends rates whose product is exact at the minor unit.
    """
    calculated = Decimal(item["price_list_rate"]) - Decimal(item.get("discount_amount", "0"))
    rate = Decimal(item["rate"]) or calculated
    assert rate == calculated, "ERPNext would reset the discount: rate != price_list_rate - discount_amount"
    return (rate * Decimal(item["qty"])).quantize(Decimal(1).scaleb(-minor))


class TestLineDiscount:
    """RT-857 / owner decision OD-28-13: a per-line manual discount is posted as a VISIBLE discount.

    The feed carries ``unitPrice``, ``quantity`` and the NET ``lineAmount``. A discounted line posts
    ``price_list_rate = unitPrice``, a per-unit ``discount_amount`` and ``rate`` = the net per-unit
    price, with ``amount = lineAmount``. When no exact per-unit price exists at the currency minor
    unit, the line is refused (fail closed): never a total off by a minor unit.
    """

    def test_an_undiscounted_line_builds_exactly_as_before(self):
        item = _build_line("100.00", "2", "200.00")
        assert item == {
            "item_code": "ITEM-A",
            "qty": "2",
            "uom": "Nos",
            "rate": "100.00",
            "price_list_rate": "100.00",
            "amount": "200.00",
            "warehouse": "Main - RT",
            "currency": "EGP",
            "rt_line_ref": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        }

    def test_an_amount_discount_on_quantity_one(self):
        item = _build_line("100.00", "1", "87.50")
        assert (item["price_list_rate"], item["discount_amount"], item["rate"], item["amount"]) == (
            "100.00",
            "12.50",
            "87.50",
            "87.50",
        )

    def test_a_percent_discount_on_an_evenly_divisible_quantity(self):
        # 10% of 3 x 9.99 = 2.997 -> 3.00 (half up on the line gross, OD-28-3): net 26.97 = 3 x 8.99.
        item = _build_line("9.99", "3", "26.97")
        assert (item["price_list_rate"], item["discount_amount"], item["rate"], item["amount"]) == (
            "9.99",
            "1.00",
            "8.99",
            "26.97",
        )

    def test_a_free_line_posts_a_full_discount_at_rate_zero(self):
        item = _build_line("25.00", "2", "0.00")
        assert (item["price_list_rate"], item["discount_amount"], item["rate"], item["amount"]) == (
            "25.00",
            "25.00",
            "0.00",
            "0.00",
        )

    @pytest.mark.parametrize(
        ("unit_price", "quantity", "line_amount"),
        [("100.00", "1", "87.50"), ("9.99", "3", "26.97"), ("100.00", "4", "360.00"), ("25.00", "2", "0.00")],
    )
    def test_erpnext_recomputes_exactly_the_captured_net(self, unit_price, quantity, line_amount):
        item = _build_line(unit_price, quantity, line_amount)
        assert _erpnext_v16_amount(item) == Decimal(line_amount) == Decimal(item["amount"])

    def test_the_invoice_total_equals_the_captured_net(self):
        doc = b.build_sales_invoice(
            _work_item(sale=_line_sale("9.99", "3", "26.97")),
            uom_for=_uom,
            warehouse_for=_warehouse,
            customer_for=_customer,
        )
        assert sum(Decimal(i["amount"]) for i in doc["items"]) == Decimal("26.97")

    def test_a_tendered_discounted_sale_is_settled_on_the_net(self):
        sale = _line_sale("100.00", "2", "180.00", tenders=[{"method": "cash", "amount": "180.00"}])
        doc = b.build_sales_invoice(
            _work_item(sale=sale),
            uom_for=_uom,
            warehouse_for=_warehouse,
            customer_for=_customer,
            mode_of_payment_for=lambda method: "Cash",
        )
        assert doc["payments"] == [{"mode_of_payment": "Cash", "amount": "180.00"}]

    @pytest.mark.parametrize(
        ("unit_price", "quantity", "line_amount"),
        [
            ("3.34", "3", "10.00"),  # 10.00 / 3: 3.33 x 3 = 9.99 and 3.34 x 3 = 10.02, neither is 10.00
            ("3.33", "3", "8.99"),  # 10% of 9.99 = 0.999 -> 1.00; net 8.99 / 3 = 2.9966...
            ("10.00", "7", "69.99"),  # a 0.01 discount over 7 units
        ],
    )
    def test_an_indivisible_net_is_refused(self, unit_price, quantity, line_amount):
        with pytest.raises(b.LineDiscountNotPostable, match=r"\[discount_not_divisible\]") as caught:
            _build_line(unit_price, quantity, line_amount)
        assert caught.value.code == "discount_not_divisible"
        assert line_amount in str(caught.value)

    def test_a_line_amount_above_the_gross_is_refused(self):
        with pytest.raises(b.LineDiscountNotPostable, match=r"\[discount_negative\]"):
            _build_line("100.00", "2", "200.01")

    def test_a_negative_line_amount_is_refused(self):
        # A discount above 100% of the line gross.
        with pytest.raises(b.LineDiscountNotPostable, match=r"\[discount_above_gross\]"):
            _build_line("100.00", "2", "-10.00")

    @pytest.mark.parametrize(("unit_price", "line_amount"), [("10.005", "9.00"), ("10.00", "9.005")])
    def test_a_discounted_amount_finer_than_the_minor_unit_is_refused(self, unit_price, line_amount):
        with pytest.raises(b.LineDiscountNotPostable, match=r"\[discount_off_minor_unit\]"):
            _build_line(unit_price, "1", line_amount)

    def test_a_discount_in_a_currency_without_a_known_minor_unit_is_refused(self):
        with pytest.raises(b.LineDiscountNotPostable, match=r"\[discount_currency_unknown\]"):
            _build_line("10.00", "1", "9.00", currency="XTS")

    def test_an_undiscounted_line_needs_no_known_minor_unit(self):
        # Unchanged behavior: the minor unit is consulted only for a discounted line.
        assert _build_line("10.00", "1", "10.00", currency="XTS")["rate"] == "10.00"

    def test_the_minor_unit_follows_the_line_currency(self):
        # JPY has no minor unit: 2700 / 3 = 900 is exact, 2701 / 3 is not.
        item = _build_line("1000", "3", "2700", currency="JPY")
        assert (item["rate"], item["discount_amount"]) == ("900", "100")
        assert _erpnext_v16_amount(item, minor=0) == Decimal("2700")
        with pytest.raises(b.LineDiscountNotPostable, match=r"\[discount_not_divisible\]"):
            _build_line("1000", "3", "2701", currency="JPY")

    def test_a_three_decimal_currency_keeps_its_minor_unit(self):
        item = _build_line("1.500", "4", "5.400", currency="KWD")
        assert (item["rate"], item["discount_amount"]) == ("1.350", "0.150")
        assert _erpnext_v16_amount(item, minor=3) == Decimal("5.400")

    def test_extra_zero_digits_do_not_change_the_representation(self):
        item = _build_line("100.0000", "2.000000", "180.0000")
        assert (item["rate"], item["discount_amount"]) == ("90.00", "10.00")
        assert (item["price_list_rate"], item["amount"]) == ("100.0000", "180.0000")

    def test_a_malformed_amount_is_still_a_money_conformance_error(self):
        # The discount is derived only from strings FR-009 has already accepted.
        from retail_tower_erpnext_connector.connector.posting.uom import MoneyConformanceError

        with pytest.raises(MoneyConformanceError):
            _build_line("100.00", "2", "1e2")


def _batch_work_item(ref, *, mode="batch"):
    line = {
        "lineName": "Item A",
        "unitPrice": "100.00",
        "currencyCode": "EGP",
        "quantity": "2",
        "lineAmount": "200.00",
        "unit": "each",
        "erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
        "trackingMode": mode,
        "erpnextBatchRef": ref,
    }
    wi = _work_item()
    return c.PostingWorkItem.from_wire(
        {
            "workItemRef": wi.work_item_ref,
            "kind": "sale_post",
            "sourceSystem": wi.source_system,
            "externalId": wi.external_id,
            "payloadHash": wi.payload_hash,
            "businessDate": wi.business_date,
            "itemCursor": wi.item_cursor,
            "sale": {
                "saleRef": wi.sale.sale_ref,
                "storeId": wi.sale.store_id,
                "currencyCode": "EGP",
                "posTotal": "200.00",
                "occurredAt": wi.sale.occurred_at,
                "businessDate": wi.sale.business_date,
                "sourceSystem": wi.source_system,
                "externalId": wi.external_id,
                "lines": [line],
            },
        }
    )


class TestBatchLine:
    """RT-452 / G7-11: a batch line posts with its supplied Batch, set explicitly."""

    def _build(self, work_item):
        return b.build_sales_invoice(work_item, uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer)

    def test_a_line_with_a_ref_sets_the_batch_and_the_row_flag(self):
        (item,) = self._build(_batch_work_item({"doctype": "Batch", "name": "LOT-B1"}))["items"]
        assert item["batch_no"] == "LOT-B1"
        assert item["use_serial_batch_fields"] == 1

    def test_a_line_without_a_ref_carries_no_batch_field(self):
        (item,) = self._build(_batch_work_item(None, mode="none"))["items"]
        assert "batch_no" not in item and "use_serial_batch_fields" not in item

    def test_a_legacy_line_without_the_fields_builds_as_before(self):
        (item,) = self._build(_work_item())["items"]
        assert "batch_no" not in item and "use_serial_batch_fields" not in item

    def test_a_reversal_never_carries_the_batch_of_its_sale_lines(self):
        # A credit note row carries no Batch of its own: ERPNext rebuilds the original's (G7-10).
        from retail_tower_erpnext_connector.connector.posting import reversal_builder as rb

        sale = _batch_work_item({"doctype": "Batch", "name": "LOT-B1"})
        void = c.PostingWorkItem.from_wire(
            {
                "workItemRef": "99999999-9999-4999-8999-999999999999",
                "kind": "reversal",
                "sourceSystem": sale.source_system,
                "externalId": sale.external_id,
                "payloadHash": "b" * 64,
                "businessDate": sale.business_date,
                "itemCursor": "cursor-2",
                "reversalOf": {
                    "sourceSystem": sale.source_system,
                    "externalId": sale.external_id,
                    "reversalKind": "void",
                },
                "sale": {
                    "saleRef": sale.sale.sale_ref,
                    "storeId": sale.sale.store_id,
                    "currencyCode": "EGP",
                    "posTotal": "200.00",
                    "occurredAt": sale.sale.occurred_at,
                    "businessDate": sale.sale.business_date,
                    "sourceSystem": sale.source_system,
                    "externalId": sale.external_id,
                    "lines": [
                        {
                            "lineName": "Item A",
                            "unitPrice": "100.00",
                            "currencyCode": "EGP",
                            "quantity": "2",
                            "lineAmount": "200.00",
                            "unit": "each",
                            "erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
                            "trackingMode": "batch",
                            "erpnextBatchRef": {"doctype": "Batch", "name": "LOT-B1"},
                        }
                    ],
                },
            }
        )
        doc = rb.build_reversing_invoice(void, uom_for=_uom, warehouse_for=_warehouse, customer_for=_customer)
        (item,) = doc["items"]
        assert "batch_no" not in item and "use_serial_batch_fields" not in item
