# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-78 — the pure tender → ERPNext Mode of Payment resolver (RT-10 D5, fail closed)."""

import pytest

from retail_tower_erpnext_connector.connector.posting import tender as t
from retail_tower_erpnext_connector.connector.posting.contracts import SaleTender

_MODES = t.TenderModeMap({"cash": "Cash", "card_external": "Card Clearing"}).resolve


class TestPaymentRowOrder:
    """RT-361 / RT-264 D4: one row per tender (same method included), in ``sequence`` order."""

    def test_same_method_tenders_are_two_rows_never_merged(self):
        tenders = [
            SaleTender("cash", "15.00", tender_ref="r-1", sequence=1),
            SaleTender("cash", "5.00", tender_ref="r-2", sequence=2),
        ]
        rows = t.build_payments(tenders, line_amounts=["20.00"], mode_of_payment_for=_MODES)
        assert rows == [
            {"mode_of_payment": "Cash", "amount": "15.00"},
            {"mode_of_payment": "Cash", "amount": "5.00"},
        ]

    def test_rows_follow_sequence_not_wire_order(self):
        tenders = [
            SaleTender("cash", "5.00", tender_ref="r-2", sequence=2),
            SaleTender("card_external", "3.00", "AB12", tender_ref="r-3", sequence=3),
            SaleTender("cash", "15.00", tender_ref="r-1", sequence=1),
        ]
        rows = t.build_payments(tenders, line_amounts=["23.00"], mode_of_payment_for=_MODES)
        assert [r["amount"] for r in rows] == ["15.00", "5.00", "3.00"]
        assert t.tender_refs_in_row_order(tenders) == ["r-1", "r-2", "r-3"]

    def test_rows_carry_no_tender_identity(self):
        # D4: tenderRef / sequence never reach the ERPNext document.
        tenders = [SaleTender("cash", "20.00", tender_ref="r-1", sequence=1)]
        (row,) = t.build_payments(tenders, line_amounts=["20.00"], mode_of_payment_for=_MODES)
        assert set(row) == {"mode_of_payment", "amount"}

    def test_legacy_tenders_keep_wire_order(self):
        tenders = [SaleTender("card_external", "5.00"), SaleTender("cash", "15.00")]
        assert t.ordered_tenders(tenders) == tenders
        assert t.tender_refs_in_row_order(tenders) == [None, None]

    def test_tenders_without_a_sequence_follow_the_sequenced_ones_in_wire_order(self):
        # Backend-Core projects ORDER BY sequence NULLS LAST; the sort is stable for the rest.
        a, b, c = SaleTender("cash", "1.00"), SaleTender("cash", "2.00", sequence=2), SaleTender("cash", "3.00")
        d = SaleTender("card_external", "4.00", sequence=1)
        assert t.ordered_tenders([a, b, c, d]) == [d, b, a, c]


class TestTenderModeMap:
    def test_resolves_a_mapped_method(self):
        assert t.TenderModeMap({"cash": "Cash"}).resolve("cash") == "Cash"

    def test_unmapped_method_fails_closed(self):
        # No default Mode of Payment, ever (D5): the item is rejected as validation.
        with pytest.raises(t.UnmappedTender, match="card_external"):
            t.TenderModeMap({"cash": "Cash"}).resolve("card_external")

    def test_empty_map_fails_closed(self):
        with pytest.raises(t.UnmappedTender):
            t.TenderModeMap({}).resolve("cash")


_EXACT = {
    "grand_total": 10.49,
    "paid_amount": 10.49,
    "change_amount": 0.0,
    "write_off_amount": 0.0,
    "outstanding_amount": 0.0,
}


class TestAssertSettled:
    """RT-78: after ERPNext computes totals on insert, a settled invoice must balance EXACTLY.

    Amendment 4a: never invent change, leave a partial balance or write anything off.
    """

    def test_exactly_settled_invoice_passes(self):
        t.assert_settled(_EXACT)

    def test_exactly_settled_return_passes(self):
        t.assert_settled({**_EXACT, "grand_total": -10.49, "paid_amount": -10.49})

    @pytest.mark.parametrize(
        "drift",
        [
            {"paid_amount": 10.0},
            {"change_amount": 0.49},
            {"write_off_amount": 0.49},
            {"outstanding_amount": 0.49},
        ],
    )
    def test_any_residual_is_a_settlement_drift(self, drift):
        with pytest.raises(t.SettlementDrift):
            t.assert_settled({**_EXACT, **drift})

    def test_totals_rounded_by_erpnext_away_from_the_requested_total_drift(self):
        # Codex P2 PR #50 round 3: 3.3333 requested, ERPNext rounds item AND payment to 3.33 — the
        # invoice balances internally but no longer equals what Backend-Core recorded.
        rounded = {**_EXACT, "grand_total": 3.33, "paid_amount": 3.33}
        with pytest.raises(t.SettlementDrift, match=r"3.3333"):
            t.assert_settled(rounded, expected_total="3.3333")

    def test_expected_total_matches_exactly(self):
        t.assert_settled(_EXACT, expected_total="10.49")
        t.assert_settled({**_EXACT, "grand_total": -10.49, "paid_amount": -10.49}, expected_total="-10.49")

    def test_missing_computed_fields_count_as_zero(self):
        t.assert_settled({"grand_total": 5.0, "paid_amount": 5.0})
