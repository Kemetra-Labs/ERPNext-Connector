# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""T010 — local unit tests for the 012 posting-feed work-item DTOs.

These assert the connector's read-models mirror the fixed, DP2-owned 012
`posting-feed.yaml` (1.1.0-draft) 1:1, and enforce the apply-only invariant
(every offered SaleLine carries a resolved `erpnextItemRef` — rider R2).

The module under test imports NO frappe (pure-Python core); these run locally.
"""

import dataclasses

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c


# A minimal-but-complete wire work-item per the 012 SaleLine/Sale/PostingWorkItem
# required sets (posting-feed.yaml).
def _wire_work_item() -> dict:
    return {
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
            "posTotal": "115.00",
            "occurredAt": "2026-06-01T10:00:00Z",
            "businessDate": "2026-06-01",
            "sourceSystem": "pos-pulse",
            "externalId": "POS-9001",
            "lines": [
                {
                    "lineName": "Item A",
                    "unitPrice": "100.00",
                    "currencyCode": "EGP",
                    "quantity": "1",
                    "lineAmount": "100.00",
                    "taxAmount": "15.00",
                    "unit": "each",
                    "erpnextItemRef": {"doctype": "Item", "name": "ITEM-A"},
                    "tenantProductRef": "44444444-4444-4444-8444-444444444444",
                }
            ],
        },
    }


class TestErpnextItemRef:
    def test_parses_doctype_and_name(self):
        ref = c.ErpnextItemRef.from_wire({"doctype": "Item", "name": "ITEM-A"})
        assert ref.doctype == "Item"
        assert ref.name == "ITEM-A"

    def test_is_frozen(self):
        ref = c.ErpnextItemRef(doctype="Item", name="ITEM-A")
        assert dataclasses.is_dataclass(ref)
        with pytest.raises(dataclasses.FrozenInstanceError):
            ref.name = "MUTATED"  # type: ignore[misc]

    def test_doctype_must_be_item(self):
        # 012: ErpnextItemRef.doctype is const "Item".
        with pytest.raises(ValueError):
            c.ErpnextItemRef.from_wire({"doctype": "Customer", "name": "X"})


class TestSaleLine:
    def test_parses_all_fields(self):
        line = c.SaleLine.from_wire(_wire_work_item()["sale"]["lines"][0])
        assert line.line_name == "Item A"
        assert line.unit_price == "100.00"
        assert line.quantity == "1"
        assert line.line_amount == "100.00"
        assert line.tax_amount == "15.00"
        assert line.unit == "each"
        assert line.erpnext_item_ref.name == "ITEM-A"
        assert line.tenant_product_ref == "44444444-4444-4444-8444-444444444444"

    def test_erpnext_item_ref_is_required(self):
        # rider R2: every OFFERED line carries a resolved erpnextItemRef.
        bad = dict(_wire_work_item()["sale"]["lines"][0])
        del bad["erpnextItemRef"]
        with pytest.raises(c.MissingErpnextItemRef):
            c.SaleLine.from_wire(bad)

    def test_tenant_product_ref_optional_for_ad_hoc(self):
        line_wire = dict(_wire_work_item()["sale"]["lines"][0])
        line_wire["tenantProductRef"] = None
        line = c.SaleLine.from_wire(line_wire)
        assert line.tenant_product_ref is None
        # ...but erpnextItemRef is still applied (R4: ad-hoc still needs a mapping).
        assert line.erpnext_item_ref.name == "ITEM-A"


class TestPostingWorkItem:
    def test_parses_header_and_sale(self):
        wi = c.PostingWorkItem.from_wire(_wire_work_item())
        assert wi.work_item_ref == "11111111-1111-4111-8111-111111111111"
        assert wi.kind == "sale_post"
        assert wi.source_system == "pos-pulse"
        assert wi.external_id == "POS-9001"
        assert wi.payload_hash == "a" * 64
        assert wi.business_date == "2026-06-01"
        assert wi.sale.currency_code == "EGP"
        assert len(wi.sale.lines) == 1

    def test_idempotency_key_is_source_system_and_external_id(self):
        wi = c.PostingWorkItem.from_wire(_wire_work_item())
        # O-3 wire idempotency anchor (forward sale_post — UNCHANGED).
        assert wi.idempotency_key == ("pos-pulse", "POS-9001")

    def test_idempotency_key_fails_closed_for_reversal(self):
        # #28 guard: a reversal's top-level externalId is the ORIGINAL sale's id, so
        # (sourceSystem, externalId) is the PRE-FIX buggy anchor. idempotency_key must REFUSE to
        # hand it back — reversals key via idempotency.key_for/provenance_id (on workItemRef).
        wire = _wire_work_item()
        wire["kind"] = "reversal"
        wire["reversalOf"] = {
            "sourceSystem": "pos-pulse",
            "externalId": "POS-9001",
            "reversalKind": "refund",
        }
        wi = c.PostingWorkItem.from_wire(wire)
        with pytest.raises(ValueError, match="forward sale_post anchor only"):
            _ = wi.idempotency_key

    def test_reversal_carries_reversal_ref(self):
        wire = _wire_work_item()
        wire["kind"] = "reversal"
        wire["reversalOf"] = {
            "sourceSystem": "pos-pulse",
            "externalId": "POS-9001",
            "reversalKind": "refund",
        }
        wi = c.PostingWorkItem.from_wire(wire)
        assert wi.kind == "reversal"
        assert wi.reversal_of is not None
        assert wi.reversal_of.reversal_kind == "refund"

    def test_sale_post_has_no_reversal_ref(self):
        wi = c.PostingWorkItem.from_wire(_wire_work_item())
        assert wi.reversal_of is None

    def test_empty_lines_is_rejected(self):
        # F-012: 012 Sale.lines is minItems: 1 — an empty lines array must raise.
        wire = _wire_work_item()
        wire["sale"]["lines"] = []
        with pytest.raises(ValueError):
            c.PostingWorkItem.from_wire(wire)


class TestOutcomeAndReason:
    def test_posted_outcome_carries_document_ref(self):
        ack = c.OutcomeAckRequest.posted(
            c.ErpnextDocumentRef(doctype="Sales Invoice", name="ACC-SINV-0001")
        )
        assert ack.outcome == "posted"
        assert ack.document_ref.name == "ACC-SINV-0001"
        assert ack.reason is None

    def test_permanently_rejected_carries_reason(self):
        ack = c.OutcomeAckRequest.permanently_rejected(
            c.RejectionReason(category="validation", message="unmapped unit")
        )
        assert ack.outcome == "permanently_rejected"
        assert ack.reason.category == "validation"
        assert ack.document_ref is None

    def test_failed_transient_has_neither(self):
        ack = c.OutcomeAckRequest.failed_transient()
        assert ack.outcome == "failed_transient"
        assert ack.document_ref is None
        assert ack.reason is None

    def test_rejection_category_must_be_in_closed_set(self):
        # 012 RejectionReason.category closed set — no new wire reason invented.
        with pytest.raises(ValueError):
            c.RejectionReason(category="unmapped_uom", message="x")

    def test_to_wire_round_trips_posted(self):
        ack = c.OutcomeAckRequest.posted(
            c.ErpnextDocumentRef(doctype="Sales Invoice", name="ACC-SINV-0001")
        )
        wire = ack.to_wire()
        assert wire["outcome"] == "posted"
        assert wire["documentRef"] == {"doctype": "Sales Invoice", "name": "ACC-SINV-0001"}
        assert "reason" not in wire or wire["reason"] is None


class TestSaleTenders:
    """RT-78 / RT-10 D1-D4: posting-feed 1.2 ``Sale.tenders`` (``SaleTender``), parsed strictly.

    A malformed tender raises ``ValueError`` so the transport isolates the item as
    ``malformed_work_item`` → ``permanently_rejected`` / ``validation`` (never a guessed tender).
    """

    def _with_tenders(self, tenders):
        wire = _wire_work_item()
        wire["sale"]["tenders"] = tenders
        return wire

    def test_absent_tenders_is_tender_unknown(self):
        wi = c.PostingWorkItem.from_wire(_wire_work_item())
        assert wi.sale.tenders == ()

    def test_null_or_empty_tenders_is_tender_unknown(self):
        for value in (None, []):
            wi = c.PostingWorkItem.from_wire(self._with_tenders(value))
            assert wi.sale.tenders == ()

    def test_parses_cash_and_card_in_wire_order(self):
        wi = c.PostingWorkItem.from_wire(
            self._with_tenders(
                [
                    {"method": "cash", "amount": "15.00"},
                    {"method": "card_external", "amount": "5.00", "reference": "AB12C"},
                ]
            )
        )
        assert wi.sale.tenders == (
            c.SaleTender(method="cash", amount="15.00", reference=None),
            c.SaleTender(method="card_external", amount="5.00", reference="AB12C"),
        )

    @pytest.mark.parametrize("method", ["voucher", "CASH", "card", ""])
    def test_unknown_method_raises(self, method):
        with pytest.raises(ValueError, match="method"):
            c.PostingWorkItem.from_wire(self._with_tenders([{"method": method, "amount": "1.00"}]))

    @pytest.mark.parametrize("amount", [1.5, "-1.00", "1.12345", "abc", "", None])
    def test_amount_must_be_a_non_negative_exact_decimal_string(self, amount):
        with pytest.raises(ValueError, match="amount"):
            c.PostingWorkItem.from_wire(self._with_tenders([{"method": "cash", "amount": amount}]))

    def test_cash_must_not_carry_a_reference(self):
        with pytest.raises(ValueError, match="reference"):
            c.PostingWorkItem.from_wire(
                self._with_tenders([{"method": "cash", "amount": "1.00", "reference": "AB12"}])
            )

    @pytest.mark.parametrize("reference", ["ab12", "ABCDEFG", "AB-12", "", 1234])
    def test_card_reference_must_match_the_short_terminal_pattern(self, reference):
        with pytest.raises(ValueError, match="reference"):
            c.PostingWorkItem.from_wire(
                self._with_tenders(
                    [{"method": "card_external", "amount": "1.00", "reference": reference}]
                )
            )

    @pytest.mark.parametrize("element", [None, "cash", 10, ["cash", "1.00"]])
    def test_non_object_tender_element_raises_value_error(self, element):
        # Greptile PR #48: `.get()` on a scalar raised AttributeError, which the transport does not
        # isolate, so one bad item aborted the whole page. It must be a ValueError.
        with pytest.raises(ValueError, match="object"):
            c.PostingWorkItem.from_wire(self._with_tenders([element]))

    def test_tenders_must_be_a_list(self):
        with pytest.raises(ValueError, match="tenders"):
            c.PostingWorkItem.from_wire(self._with_tenders({"method": "cash", "amount": "1.00"}))



class TestReversalTimestamps:
    """RT-16 / RT-63 (decision 10348): ``reversalOf.recordedAt`` + ``reversalOf.businessDate``.

    Both optional on parse (an older Backend-Core sends neither → the RT-49 fallback). When sent they
    must be well-formed, and a ``void`` / ``return`` that carries ``recordedAt`` must carry its own
    ``businessDate`` (posting-feed 1.3 schema) — a legacy ``refund`` has none (RT-63 P2).
    """

    def _reversal(self, **ref):
        wire = _wire_work_item()
        wire["kind"] = "reversal"
        wire["reversalOf"] = {"sourceSystem": "pos-pulse", "externalId": "POS-9001", "reversalKind": "void", **ref}
        return wire

    def test_parses_the_reversal_own_time_and_business_date(self):
        wi = c.PostingWorkItem.from_wire(
            self._reversal(recordedAt="2026-06-05T09:30:00Z", businessDate="2026-06-05")
        )
        assert (wi.reversal_of.recorded_at, wi.reversal_of.business_date) == (
            "2026-06-05T09:30:00Z",
            "2026-06-05",
        )

    def test_older_backend_core_sends_neither(self):
        wi = c.PostingWorkItem.from_wire(self._reversal())
        assert (wi.reversal_of.recorded_at, wi.reversal_of.business_date) == (None, None)

    @pytest.mark.parametrize(
        "ref",
        [
            {"recordedAt": None, "businessDate": None},
            {"recordedAt": None},
            {"businessDate": None},
            {"recordedAt": "2026-06-05T09:30:00Z", "businessDate": None},
        ],
    )
    def test_explicit_null_timestamps_are_malformed(self, ref):
        # Codex P2 PR #49 round 5: only an OMITTED pair is the older-Backend-Core fallback; a null
        # (the schema types both as strings) would otherwise fall back to the sale's day silently.
        with pytest.raises(ValueError, match="null"):
            c.PostingWorkItem.from_wire(self._reversal(**ref))

    def test_void_with_a_time_but_no_business_date_is_malformed(self):
        with pytest.raises(ValueError, match="businessDate"):
            c.PostingWorkItem.from_wire(self._reversal(recordedAt="2026-06-05T09:30:00Z"))

    def test_legacy_refund_carries_a_time_without_a_business_date(self):
        wi = c.PostingWorkItem.from_wire(
            self._reversal(reversalKind="refund", recordedAt="2026-06-05T09:30:00Z")
        )
        assert (wi.reversal_of.recorded_at, wi.reversal_of.business_date) == ("2026-06-05T09:30:00Z", None)

    @pytest.mark.parametrize("kind", ["void", "refund"])
    def test_a_business_date_without_its_time_is_malformed(self, kind):
        # Codex P2 / Greptile PR #49: a lone businessDate would be ignored by the stamp selector and
        # the reversal silently posted on the sale's day. Only BOTH-absent is the legacy fallback.
        with pytest.raises(ValueError, match="recordedAt"):
            c.PostingWorkItem.from_wire(self._reversal(reversalKind=kind, businessDate="2026-06-05"))

    @pytest.mark.parametrize("recorded_at", ["2026-06-05T09:30:00", "2026-06-05T09:30:00.123"])
    def test_recorded_at_without_a_timezone_offset_is_malformed(self, recorded_at):
        # Codex P2 / Greptile PR #49: a naive time parsed here but failed later as `other`.
        with pytest.raises(ValueError, match="offset"):
            c.PostingWorkItem.from_wire(self._reversal(recordedAt=recorded_at, businessDate="2026-06-05"))

    @pytest.mark.parametrize(
        "recorded_at",
        [
            "2026-06-05T09:30+03:00",
            "2026-06-05T09Z",
            "2026-06-05 09:30:00Z",
            "20260605T093000Z",
            # Codex P2 PR #49 round 3: fromisoformat normalizes 24:00 to the next midnight.
            "2026-06-05T24:00:00Z",
            "2026-06-05T09:60:00Z",
            "2026-06-05T09:30:61Z",
            "2026-06-05T09:30:00+24:00",
        ],
    )
    def test_recorded_at_must_follow_the_rfc3339_grammar(self, recorded_at):
        # Codex P2 PR #49 round 2: fromisoformat also accepts non-RFC 3339 forms (no seconds, a
        # space separator, basic format), which could then post at a guessed time.
        with pytest.raises(ValueError, match="RFC 3339"):
            c.PostingWorkItem.from_wire(self._reversal(recordedAt=recorded_at, businessDate="2026-06-05"))

    @pytest.mark.parametrize("recorded_at", ["2026-06-05T09:30:00.123Z", "2026-06-05T09:30:00.123456+03:00"])
    def test_recorded_at_with_fractional_seconds_is_accepted(self, recorded_at):
        wi = c.PostingWorkItem.from_wire(self._reversal(recordedAt=recorded_at, businessDate="2026-06-05"))
        assert wi.reversal_of.recorded_at == recorded_at

    def test_recorded_at_with_an_explicit_offset_is_accepted(self):
        wi = c.PostingWorkItem.from_wire(
            self._reversal(recordedAt="2026-06-05T12:30:00+03:00", businessDate="2026-06-05")
        )
        assert wi.reversal_of.recorded_at == "2026-06-05T12:30:00+03:00"

    @pytest.mark.parametrize("recorded_at", ["yesterday", "2026-06-05", 1717580000, ""])
    def test_malformed_recorded_at_raises(self, recorded_at):
        with pytest.raises(ValueError, match="recordedAt"):
            c.PostingWorkItem.from_wire(self._reversal(recordedAt=recorded_at, businessDate="2026-06-05"))

    @pytest.mark.parametrize(
        "business_date",
        # Codex P2 PR #49 round 4: 10-char ISO week / ordinal dates pass fromisoformat on 3.11+.
        ["2026-13-01", "05/06/2026", 20260605, "", "2026-W23-5", "2026-W23-5 ", "2026-156-1", "2026_06_05"],
    )
    def test_malformed_business_date_raises(self, business_date):
        with pytest.raises(ValueError, match="businessDate"):
            c.PostingWorkItem.from_wire(
                self._reversal(recordedAt="2026-06-05T09:30:00Z", businessDate=business_date)
            )



_LINE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_LINE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _return_wire(**ref):
    """A posting-feed 1.3 ``return`` work item: a two-line sale, returning part of line A."""
    wire = _wire_work_item()
    wire["kind"] = "reversal"
    wire["sale"]["lines"] = [
        {**wire["sale"]["lines"][0], "lineRef": _LINE_A, "quantity": "3", "lineAmount": "300.00", "taxAmount": None},
        {**wire["sale"]["lines"][0], "lineRef": _LINE_B, "lineName": "Item B"},
    ]
    wire["reversalOf"] = {
        "sourceSystem": "pos-pulse",
        "externalId": "POS-9001",
        "reversalKind": "return",
        "recordedAt": "2026-06-05T09:30:00Z",
        "businessDate": "2026-06-05",
        "returnLines": [{"lineRef": _LINE_A, "quantity": "1", "lineAmount": "100.00", "taxAmount": None}],
        "refundTenders": [{"method": "cash", "amount": "100.00"}],
        **ref,
    }
    return wire


class TestSaleLineRef:
    """RT-16 / RT-14 D6: every sale line carries ``lineRef`` (= sale_lines.id) from feed 1.3."""

    def test_parses_the_line_ref(self):
        wi = c.PostingWorkItem.from_wire(_return_wire())
        assert [line.line_ref for line in wi.sale.lines] == [_LINE_A, _LINE_B]

    def test_duplicate_line_refs_in_one_sale_are_malformed(self):
        # Greptile PR #50: lineRef = sale_lines.id is unique per sale; a duplicate would post two
        # invoice rows with one rt_line_ref and make every later return of that line unmatchable.
        wire = _return_wire()
        wire["sale"]["lines"][1]["lineRef"] = _LINE_A
        with pytest.raises(ValueError, match="duplicate lineRef"):
            c.PostingWorkItem.from_wire(wire)

    def test_an_older_feed_without_line_ref_still_parses(self):
        assert c.PostingWorkItem.from_wire(_wire_work_item()).sale.lines[0].line_ref is None


class TestReturnReversal:
    """RT-16 / RT-14 D1+D3: ``reversalKind: return`` with ``returnLines`` and cash ``refundTenders``."""

    def test_parses_return_lines_and_refund_tenders(self):
        ref = c.PostingWorkItem.from_wire(_return_wire()).reversal_of
        assert ref.reversal_kind == "return"
        assert ref.return_lines == (
            c.ReturnLine(line_ref=_LINE_A, quantity="1", line_amount="100.00", tax_amount=None),
        )
        assert ref.refund_tenders == (c.RefundTender(method="cash", amount="100.00"),)

    def test_a_return_without_return_lines_is_malformed(self):
        for lines in (None, []):
            with pytest.raises(ValueError, match="returnLines"):
                c.PostingWorkItem.from_wire(_return_wire(returnLines=lines))

    def test_a_return_needs_its_own_time_and_business_date(self):
        with pytest.raises(ValueError, match="recordedAt"):
            c.PostingWorkItem.from_wire(_return_wire(recordedAt=None, businessDate=None))

    @pytest.mark.parametrize("field", ["returnLines", "refundTenders"])
    def test_return_only_fields_are_forbidden_on_a_void(self, field):
        wire = _return_wire(reversalKind="void")
        if field == "returnLines":
            wire["reversalOf"].pop("refundTenders")
        else:
            wire["reversalOf"].pop("returnLines")
        with pytest.raises(ValueError, match=field):
            c.PostingWorkItem.from_wire(wire)

    @pytest.mark.parametrize("quantity", ["0", "0.000", "-1", "1.1234567", 1, ""])
    def test_return_quantity_must_be_a_positive_exact_decimal(self, quantity):
        lines = [{"lineRef": _LINE_A, "quantity": quantity, "lineAmount": "100.00", "taxAmount": None}]
        with pytest.raises(ValueError, match="quantity"):
            c.PostingWorkItem.from_wire(_return_wire(returnLines=lines))

    @pytest.mark.parametrize("amount", ["-1.00", "1.12345", 100.0, None])
    def test_return_line_amount_must_be_a_non_negative_exact_decimal(self, amount):
        lines = [{"lineRef": _LINE_A, "quantity": "1", "lineAmount": amount, "taxAmount": None}]
        with pytest.raises(ValueError, match="lineAmount"):
            c.PostingWorkItem.from_wire(_return_wire(returnLines=lines))

    def test_duplicate_line_ref_in_one_return_is_malformed(self):
        one = {"lineRef": _LINE_A, "quantity": "1", "lineAmount": "100.00", "taxAmount": None}
        with pytest.raises(ValueError, match="duplicate"):
            c.PostingWorkItem.from_wire(_return_wire(returnLines=[one, dict(one)]))

    def test_a_return_line_must_point_at_a_sale_line(self):
        stray = [{"lineRef": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "quantity": "1", "lineAmount": "1.00", "taxAmount": None}]
        with pytest.raises(ValueError, match="lineRef"):
            c.PostingWorkItem.from_wire(_return_wire(returnLines=stray))

    @pytest.mark.parametrize("element", [None, "x", ["cash"]])
    def test_non_object_return_line_or_refund_tender_is_malformed(self, element):
        with pytest.raises(ValueError, match="object"):
            c.PostingWorkItem.from_wire(_return_wire(returnLines=[element]))
        with pytest.raises(ValueError, match="object"):
            c.PostingWorkItem.from_wire(_return_wire(refundTenders=[element]))

    @pytest.mark.parametrize(
        "tender",
        [
            {"method": "card_external", "amount": "100.00"},  # RT-14 D3: cash only
            {"method": "cash", "amount": "-100.00"},
            {"method": "cash", "amount": "100.00", "reference": "AB12"},
        ],
    )
    def test_refund_tenders_are_cash_only_non_negative_and_unreferenced(self, tender):
        with pytest.raises(ValueError, match="RefundTender"):
            c.PostingWorkItem.from_wire(_return_wire(refundTenders=[tender]))

    def test_refund_tenders_are_optional_on_parse(self):
        # The builder rejects a return without them (validation); the wire field itself is optional.
        wire = _return_wire()
        wire["reversalOf"].pop("refundTenders")
        assert c.PostingWorkItem.from_wire(wire).reversal_of.refund_tenders == ()


class TestTrailingNewlineRejected:
    """RT-875: Python ``$`` under ``re.match`` also matches before a final "\\n"; the 012 patterns do not.

    One case per reachable call site of ``_NON_NEGATIVE_DECIMAL_RE``, ``_RETURN_QUANTITY_RE`` and
    ``_TENDER_REFERENCE_RE``.
    """

    def test_sale_tender_amount(self):
        wire = _wire_work_item()
        wire["sale"]["tenders"] = [{"method": "cash", "amount": "15.00\n"}]
        with pytest.raises(ValueError, match="amount"):
            c.PostingWorkItem.from_wire(wire)

    def test_sale_tender_reference(self):
        wire = _wire_work_item()
        wire["sale"]["tenders"] = [{"method": "card_external", "amount": "5.00", "reference": "AB12C\n"}]
        with pytest.raises(ValueError, match="reference"):
            c.PostingWorkItem.from_wire(wire)

    def test_return_line_quantity(self):
        lines = [{"lineRef": _LINE_A, "quantity": "1\n", "lineAmount": "100.00", "taxAmount": None}]
        with pytest.raises(ValueError, match="quantity"):
            c.PostingWorkItem.from_wire(_return_wire(returnLines=lines))

    @pytest.mark.parametrize("field", ["lineAmount", "taxAmount"])
    def test_return_line_amounts(self, field):
        line = {"lineRef": _LINE_A, "quantity": "1", "lineAmount": "100.00", "taxAmount": "0.00"}
        line[field] = line[field] + "\n"
        with pytest.raises(ValueError, match=field):
            c.PostingWorkItem.from_wire(_return_wire(returnLines=[line]))

    def test_refund_tender_amount(self):
        with pytest.raises(ValueError, match="RefundTender"):
            c.PostingWorkItem.from_wire(_return_wire(refundTenders=[{"method": "cash", "amount": "100.00\n"}]))
