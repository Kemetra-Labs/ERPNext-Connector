# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1237 — the bin-view ``readAt`` clock reports the true UTC instant.

``frappe_glue`` imports frappe, so it is loaded against a minimal stand-in (the RT-176
pattern). The stand-in's ``frappe.utils.now_datetime`` returns Cairo wall time, the way a
site whose system time zone is not UTC does, so a clock that reads it is caught.
"""

import importlib
import re
import sys
import types
from datetime import UTC, datetime, timedelta

import pytest

_ROOT = "retail_tower_erpnext_connector"
_GLUE = f"{_ROOT}.connector.bin_view.frappe_glue"
_MISSING = object()

# 2026-11-05 07:15:30.123 UTC is 09:15:30.123 in Cairo (UTC+2 in November).
_UTC_INSTANT = datetime(2026, 11, 5, 7, 15, 30, 123456, tzinfo=UTC)
_CAIRO_WALL = datetime(2026, 11, 5, 9, 15, 30, 123456)  # naive, as now_datetime() returns


class _FixedDatetime(datetime):
	@classmethod
	def now(cls, tz=None):
		return _UTC_INSTANT if tz is None else _UTC_INSTANT.astimezone(tz)


@pytest.fixture
def glue():
	before = set(sys.modules)
	saved = {name: sys.modules.get(name, _MISSING) for name in ("frappe", "frappe.utils")}
	fake = types.ModuleType("frappe")
	utils = types.ModuleType("frappe.utils")
	utils.now_datetime = lambda: _CAIRO_WALL
	fake.utils = utils
	sys.modules["frappe"] = fake
	sys.modules["frappe.utils"] = utils
	yield importlib.import_module(_GLUE)
	for name in set(sys.modules) - before:
		if name.startswith(_ROOT):
			sys.modules.pop(name, None)
			parent, _, child = name.rpartition(".")
			if parent in sys.modules:
				vars(sys.modules[parent]).pop(child, None)
	for name, module in saved.items():
		if module is _MISSING:
			sys.modules.pop(name, None)
		else:
			sys.modules[name] = module


def test_read_at_is_the_utc_instant_not_site_local_time(glue, monkeypatch):
	monkeypatch.setattr(glue, "datetime", _FixedDatetime, raising=False)

	assert glue.UtcClock().now_iso() == "2026-11-05T07:15:30.123Z"


def test_read_at_keeps_real_milliseconds_and_the_contract_shape(glue):
	before = datetime.now(UTC)
	read_at = glue.UtcClock().now_iso()
	after = datetime.now(UTC)

	assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", read_at)
	parsed = datetime.strptime(read_at, "%Y-%m-%dT%H:%M:%S.%f%z")
	assert before - timedelta(milliseconds=1) <= parsed <= after
