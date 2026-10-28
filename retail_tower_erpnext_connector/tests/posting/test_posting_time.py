# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-49 — local unit tests for the posting date/time rule (no frappe).

Owner decision (RT-49 comment 10312, option a): ERPNext overwrites ``posting_date`` with "now"
unless ``set_posting_time=1``, so ``businessDate`` was silently discarded. The rule:

  - sale: ``posting_date = businessDate``; ``posting_time`` = ``occurredAt`` capped at now,
    converted to the ERPNext SITE timezone, clamped into businessDate's day if the zones disagree;
  - reversal: the same sale-derived stamp, raised to the original invoice's posting timestamp when
    that is later (ERPNext rejects a return EARLIER than its original; equal is allowed).
"""

from datetime import UTC, datetime, time, timedelta

import pytest

from retail_tower_erpnext_connector.connector.posting import posting_time as pt

_NOW = datetime(2026, 9, 28, 16, 0, 0, tzinfo=UTC)
_CAIRO = pt.PostingClock(site_tz="Africa/Cairo", now=_NOW)  # UTC+3 on this date (EEST)
_UTC = pt.PostingClock(site_tz="UTC", now=_NOW)


class TestSaleStamp:
	def test_same_zone_keeps_business_date_and_occurred_time(self):
		s = pt.stamp_for("2026-09-27T10:15:30.250Z", "2026-09-27", _UTC)
		assert (s.posting_date, s.posting_time, s.adjustments) == ("2026-09-27", "10:15:30.250000", ())

	def test_converts_occurred_at_to_the_site_timezone(self):
		# ERPNext reads posting_time as local time in the SITE timezone.
		s = pt.stamp_for("2026-09-27T10:15:30Z", "2026-09-27", _CAIRO)
		assert (s.posting_date, s.posting_time, s.adjustments) == ("2026-09-27", "13:15:30.000000", ())

	def test_accepts_an_explicit_offset(self):
		s = pt.stamp_for("2026-09-27T13:15:30+03:00", "2026-09-27", _UTC)
		assert s.posting_time == "10:15:30.000000"

	def test_local_date_after_business_date_clamps_to_end_of_business_day(self):
		# 22:30Z is 01:30 next day in Cairo while the store (UTC) business date is still the 27th.
		s = pt.stamp_for("2026-09-27T22:30:00Z", "2026-09-27", _CAIRO)
		assert (s.posting_date, s.posting_time) == ("2026-09-27", pt.DAY_END)
		assert s.adjustments == ("clamped_to_business_day_end",)

	def test_local_date_before_business_date_clamps_to_start_of_business_day(self):
		s = pt.stamp_for("2026-09-26T23:30:00Z", "2026-09-27", _UTC)
		assert (s.posting_date, s.posting_time) == ("2026-09-27", pt.DAY_START)
		assert s.adjustments == ("clamped_to_business_day_start",)

	def test_occurred_at_in_the_future_is_capped_at_now(self):
		# A POS clock ahead of the server must never produce a future stock timestamp.
		s = pt.stamp_for("2026-09-28T16:05:00Z", "2026-09-28", _UTC)
		assert (s.posting_date, s.posting_time) == ("2026-09-28", "16:00:00.000000")
		assert s.adjustments == ("capped_at_now",)

	def test_posting_date_is_always_the_business_date(self):
		for occurred in ("2026-09-20T00:00:00Z", "2026-09-27T12:00:00Z", "2026-09-29T00:00:00Z"):
			assert pt.stamp_for(occurred, "2026-09-27", _CAIRO).posting_date == "2026-09-27"

	def test_naive_occurred_at_is_rejected(self):
		# 012 occurredAt is an instant; a naive value has no defined zone — fail closed.
		with pytest.raises(ValueError):
			pt.stamp_for("2026-09-27T10:15:30", "2026-09-27", _UTC)

	def test_unknown_site_timezone_is_rejected(self):
		with pytest.raises(ValueError, match=r"unknown ERPNext site timezone 'Mars/Base'"):
			pt.stamp_for("2026-09-27T10:15:30Z", "2026-09-27", pt.PostingClock(site_tz="Mars/Base", now=_NOW))

	def test_clock_without_now_does_not_cap(self):
		s = pt.stamp_for("2030-01-01T08:00:00Z", "2030-01-01", pt.PostingClock(site_tz="UTC", now=None))
		assert (s.posting_time, s.adjustments) == ("08:00:00.000000", ())


class TestRaiseToOriginal:
	_SALE = pt.PostingStamp("2026-09-27", "13:15:30.000000")

	def test_original_earlier_keeps_the_stamp(self):
		assert pt.raise_to_original(self._SALE, "2026-09-27", "13:15:29.999999") == self._SALE

	def test_original_equal_keeps_the_stamp(self):
		# ERPNext throws only when the return is EARLIER than the original; equal submits.
		assert pt.raise_to_original(self._SALE, "2026-09-27", "13:15:30") == self._SALE

	def test_original_later_on_a_later_date_raises_to_the_original(self):
		# A pre-RT-49 original: ERPNext overwrote its posting_date with the post day.
		s = pt.raise_to_original(self._SALE, "2026-09-28", timedelta(hours=9, minutes=5, microseconds=12))
		assert (s.posting_date, s.posting_time) == ("2026-09-28", "09:05:00.000012")
		assert s.adjustments == ("raised_to_original",)

	def test_original_later_same_day_raises_time(self):
		s = pt.raise_to_original(self._SALE, "2026-09-27", time(14, 0, 0))
		assert (s.posting_date, s.posting_time) == ("2026-09-27", "14:00:00.000000")

	def test_keeps_earlier_adjustments(self):
		clamped = pt.PostingStamp("2026-09-27", pt.DAY_START, ("clamped_to_business_day_start",))
		s = pt.raise_to_original(clamped, "2026-09-27", "08:00:00")
		assert s.adjustments == ("clamped_to_business_day_start", "raised_to_original")

	def test_accepts_date_objects(self):
		from datetime import date

		s = pt.raise_to_original(self._SALE, date(2026, 9, 28), "00:00:01")
		assert s.posting_date == "2026-09-28"


class TestFormatTime:
	@pytest.mark.parametrize(
		("value", "expected"),
		[
			(timedelta(hours=19, minutes=27, seconds=48, microseconds=898550), "19:27:48.898550"),
			(timedelta(0), "00:00:00.000000"),
			("7:05:03", "07:05:03.000000"),
			("19:27:48.8985", "19:27:48.898500"),
			(time(1, 2, 3, 4), "01:02:03.000004"),
		],
	)
	def test_normalises_erpnext_time_values(self, value, expected):
		assert pt.format_time(value) == expected

	def test_rejects_a_day_or_more(self):
		with pytest.raises(ValueError, match=r"posting_time out of range"):
			pt.format_time(timedelta(days=1))


class TestApplyStamp:
	def test_sets_the_three_fields_on_a_copy(self):
		doc = {"doctype": "Sales Invoice", "posting_date": "old"}
		out = pt.apply_stamp(doc, pt.PostingStamp("2026-09-27", "13:15:30.000000"))
		assert out["set_posting_time"] == 1
		assert (out["posting_date"], out["posting_time"]) == ("2026-09-27", "13:15:30.000000")
		assert doc == {"doctype": "Sales Invoice", "posting_date": "old"}  # never mutated
