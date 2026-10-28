# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Posting date/time rule for Retail Tower invoices (RT-49, owner decision 10312, option a).

ERPNext v15 ``TransactionBase.validate_posting_time`` overwrites ``posting_date`` and
``posting_time`` with "now" unless ``set_posting_time = 1``, so the ``businessDate`` the connector
sent was silently discarded. The 012 work-item carries no posting time, only ``sale.occurredAt``
(an instant), so the approved rule is Connector-side and needs no contract change:

  - **sale**: ``posting_date = businessDate``; ``posting_time`` = ``occurredAt`` capped at now,
    in the ERPNext SITE timezone (ERPNext reads ``posting_time`` as naive site-local time). If that
    local date is not ``businessDate`` (the store and site timezones disagree), the time is clamped
    into ``businessDate``'s day. ``posting_date`` is always ``businessDate``.
  - **reversal**: the same stamp from the reversal work-item's ``businessDate`` (today DP2 sends the
    ORIGINAL sale's), raised to the original invoice's posting timestamp when that is later.
    ERPNext ``validate_return_against`` rejects a return EARLIER than its original; equal submits.
    That covers invoices posted before RT-49, whose ``posting_date`` ERPNext overwrote.

Every adjustment is reported in :attr:`PostingStamp.adjustments` so the glue can log it at ERROR.
Precondition (10312): the pilot store timezone equals the ERPNext site timezone; the clamp is a
guard, not a feature. This module imports NO frappe.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAY_START = "00:00:00.000000"
DAY_END = "23:59:59.999999"



@dataclass(frozen=True)
class PostingClock:
	"""The ERPNext site timezone plus "now" (an aware instant; ``None`` = do not cap)."""

	site_tz: str
	now: datetime | None


@dataclass(frozen=True)
class PostingStamp:
	"""An ERPNext ``(posting_date, posting_time)`` pair and any rule adjustments applied to it."""

	posting_date: str
	posting_time: str
	adjustments: tuple[str, ...] = ()


# The pure builders' default when no site clock is injected (unit tests). The bench glue ALWAYS
# passes the ERPNext site timezone and the current instant (``frappe_glue._posting_clock``).
UTC_CLOCK = PostingClock(site_tz="UTC", now=None)


def stamp_for(occurred_at: str, business_date: str, clock: PostingClock) -> PostingStamp:
	"""The sale rule: ``businessDate`` + ``occurredAt`` (capped at now) in the site timezone."""
	instant = _parse_instant(occurred_at)
	adjustments: list[str] = []
	if clock.now is not None and instant > clock.now:
		instant = clock.now
		adjustments.append("capped_at_now")
	local = instant.astimezone(_zone(clock.site_tz))
	day = date.fromisoformat(business_date)
	if local.date() > day:
		return PostingStamp(business_date, DAY_END, (*adjustments, "clamped_to_business_day_end"))
	if local.date() < day:
		return PostingStamp(business_date, DAY_START, (*adjustments, "clamped_to_business_day_start"))
	return PostingStamp(business_date, format_time(local.time()), tuple(adjustments))


def raise_to_original(stamp: PostingStamp, original_date, original_time) -> PostingStamp:
	"""Never post a return earlier than its original invoice (ERPNext would reject it)."""
	original_day = original_date.isoformat() if isinstance(original_date, date) else str(original_date)
	original_clock = format_time(original_time)
	if _as_datetime(original_day, original_clock) <= _as_datetime(stamp.posting_date, stamp.posting_time):
		return stamp
	return PostingStamp(original_day, original_clock, (*stamp.adjustments, "raised_to_original"))


def apply_stamp(doc: Mapping, stamp: PostingStamp) -> dict:
	"""A copy of ``doc`` that ERPNext will post at ``stamp`` (``set_posting_time = 1``)."""
	return {
		**doc,
		"set_posting_time": 1,
		"posting_date": stamp.posting_date,
		"posting_time": stamp.posting_time,
	}


def format_time(value) -> str:
	"""Normalise an ERPNext time value (``timedelta`` from the DB, ``time`` or text) to ``HH:MM:SS.ffffff``."""
	if isinstance(value, timedelta):
		if not timedelta(0) <= value < timedelta(days=1):
			raise ValueError(f"posting_time out of range: {value!r}")
		value = (datetime.min + value).time()
	elif isinstance(value, str):
		value = time.fromisoformat(_pad_time(value))
	if not isinstance(value, time):
		raise ValueError(f"unsupported posting_time value: {value!r}")
	return value.strftime("%H:%M:%S.%f")


def _pad_time(text: str) -> str:
	"""``7:05:03`` → ``07:05:03`` and a short fraction → 6 digits, for ``time.fromisoformat`` on 3.10."""
	clock, _, fraction = text.strip().partition(".")
	hours, _, rest = clock.partition(":")
	padded = f"{int(hours):02d}:{rest}"
	return f"{padded}.{fraction.ljust(6, '0')[:6]}" if fraction else padded


def _parse_instant(text: str) -> datetime:
	"""Parse the 012 ``occurredAt`` instant (ISO-8601 with ``Z`` or an offset); naive is rejected."""
	value = text.strip()
	if value.endswith(("Z", "z")):
		value = value[:-1] + "+00:00"
	parsed = datetime.fromisoformat(value)
	if parsed.tzinfo is None:
		raise ValueError(f"occurredAt must carry a timezone offset, got {text!r}")
	return parsed.astimezone(UTC)


def _zone(name: str) -> ZoneInfo:
	try:
		return ZoneInfo(name)
	except (ZoneInfoNotFoundError, ValueError) as exc:
		raise ValueError(f"unknown ERPNext site timezone {name!r}") from exc


def _as_datetime(day: str, clock: str) -> datetime:
	return datetime.combine(date.fromisoformat(day), time.fromisoformat(clock))
