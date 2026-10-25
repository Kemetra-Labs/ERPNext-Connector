# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-379 (RT-283 F4, design C) — the ERPNext Compatibility Profile (pure).

An incompatible site is detected before any sale depends on it. A **critical** finding pauses
posting (D2: the poller does not pull, the backlog stays in Backend-Core); a **warning** is
recorded and posting continues. The fingerprint drives the ``compatibility.drift`` event.
"""

import dataclasses
import re

import pytest

from retail_tower_erpnext_connector import hooks
from retail_tower_erpnext_connector.connector.posting import compatibility_profile as cp


def _snapshot(**overrides):
	base = cp.SiteSnapshot(
		frappe_version="16.52.0",
		erpnext_version="16.51.0",
		custom_fields=frozenset(cp.REQUIRED_CUSTOM_FIELDS),
		posting_currency="EGP",
		currency_precision=2,
		default_company="RT Co",
		company_currency="EGP",
		mapped=(
			cp.MappedRecord("UOM", "Nos", exists=True, disabled=False),
			cp.MappedRecord("Warehouse", "Stores - RT", exists=True, disabled=False, company="RT Co"),
			cp.MappedRecord("Customer", "Walk-in", exists=True, disabled=False),
			cp.MappedRecord("Mode of Payment", "Cash", exists=True, disabled=False, has_company_account=True),
		),
		tax_sources=(),
		validate_selling_price=False,
		selling_pricing_rules=0,
		# A v16 site configured per the policy (RT-494).
		scheduler_tick_s=60,
	)
	return dataclasses.replace(base, **overrides)


def _critical(**overrides):
	return cp.evaluate(_snapshot(**overrides)).critical


class TestCompatibleSite:
	def test_a_compatible_site_is_ok_with_no_findings(self):
		result = cp.evaluate(_snapshot())
		assert result.ok and result.status == "OK"
		assert result.critical == () and result.warnings == ()


class TestVersion:
	@pytest.mark.parametrize(
		("frappe_version", "erpnext_version"),
		[("14.80.0", "14.70.2"), ("15.110.0", "15.110.0"), ("17.0.0", "17.0.0")],
	)
	def test_a_major_outside_the_supported_set_is_critical(self, frappe_version, erpnext_version):
		critical = _critical(frappe_version=frappe_version, erpnext_version=erpnext_version)
		assert len(critical) == 2
		assert frappe_version in critical[0]
		assert erpnext_version in critical[1]
		assert all("v16" in finding and "v15" not in finding for finding in critical)

	def test_a_v16_site_is_ok(self):
		# RT-494 (RT-201 decision): v16 is supported; the validated pin is Frappe 16.52.0 / ERPNext 16.51.0.
		result = cp.evaluate(_snapshot(frappe_version="16.52.0", erpnext_version="16.51.0"))
		assert result.ok
		assert result.critical == ()

	@pytest.mark.parametrize(
		("frappe_version", "erpnext_version"), [("15.110.0", "16.51.0"), ("16.52.0", "15.110.0")]
	)
	def test_frappe_and_erpnext_on_different_majors_is_critical(self, frappe_version, erpnext_version):
		# With v16 the only supported major, the v15 side of a mismatch is outside the supported set.
		critical = _critical(frappe_version=frappe_version, erpnext_version=erpnext_version)
		assert len(critical) == 1
		assert "15.110.0" in critical[0]
		assert "outside the supported set v16" in critical[0]

	@pytest.mark.parametrize("value", [None, "", "develop", "version-15"])
	def test_an_unparseable_version_is_critical(self, value):
		assert _critical(frappe_version=value)

	def test_the_supported_set_is_v16_only(self):
		# D5 amended by the owner on 2026-10-25: v16 only; v15 dropped (no site runs it).
		assert frozenset({16}) == cp.SUPPORTED_MAJORS


class TestCustomFields:
	def test_the_required_list_is_the_hooks_fixture_list(self):
		# One source of truth: the custom fields the app ships are the ones the profile requires.
		(fixture,) = [f for f in hooks.fixtures if f["dt"] == "Custom Field"]
		(name_filter,) = [f for f in fixture["filters"] if f[0] == "name"]
		assert frozenset(name_filter[2]) == frozenset(cp.REQUIRED_CUSTOM_FIELDS)

	def test_a_missing_custom_field_is_critical(self):
		present = frozenset(cp.REQUIRED_CUSTOM_FIELDS) - {"Sales Invoice Item-rt_line_ref"}
		critical = _critical(custom_fields=present)
		assert len(critical) == 1 and "Sales Invoice Item-rt_line_ref" in critical[0]


class TestCurrency:
	def test_a_precision_below_the_minor_units_is_critical(self):
		critical = _critical(currency_precision=0)
		assert len(critical) == 1 and "precision" in critical[0]

	def test_a_higher_precision_is_fine(self):
		assert _critical(currency_precision=4) == ()

	def test_an_unknown_precision_is_critical(self):
		assert _critical(currency_precision=None)

	def test_a_currency_with_three_minor_units_needs_three_decimals(self):
		critical = _critical(posting_currency="KWD", company_currency="KWD", currency_precision=2)
		assert len(critical) == 1 and "KWD" in critical[0]

	def test_a_currency_without_known_minor_units_is_critical(self):
		critical = _critical(posting_currency="XYZ", company_currency="XYZ")
		assert critical and "XYZ" in critical[0]

	@pytest.mark.parametrize("value", [None, ""])
	def test_no_posting_currency_configured_is_critical(self, value):
		critical = _critical(posting_currency=value)
		assert critical and "posting currency" in critical[0]

	def test_a_company_currency_other_than_the_posting_currency_is_critical(self):
		critical = _critical(company_currency="USD")
		assert len(critical) == 1 and "USD" in critical[0] and "EGP" in critical[0]


class TestCompany:
	@pytest.mark.parametrize("value", [None, ""])
	def test_no_default_company_is_critical(self, value):
		critical = _critical(default_company=value)
		assert any("default_company" in c for c in critical)

	def test_a_mapped_warehouse_of_another_company_is_critical(self):
		mapped = (
			cp.MappedRecord("Warehouse", "Stores - X", exists=True, disabled=False, company="Other Co"),
		)
		critical = _critical(mapped=mapped)
		assert len(critical) == 1 and "Stores - X" in critical[0] and "Other Co" in critical[0]


class TestMappedRecords:
	@pytest.mark.parametrize("doctype", ["UOM", "Warehouse", "Customer", "Mode of Payment"])
	def test_a_missing_mapped_record_is_critical(self, doctype):
		mapped = (cp.MappedRecord(doctype, "Gone", exists=False, disabled=False, company="RT Co"),)
		critical = _critical(mapped=mapped)
		assert len(critical) == 1 and doctype in critical[0] and "Gone" in critical[0]

	@pytest.mark.parametrize("doctype", ["UOM", "Warehouse", "Customer", "Mode of Payment"])
	def test_a_disabled_mapped_record_is_critical(self, doctype):
		mapped = (cp.MappedRecord(doctype, "Off", exists=True, disabled=True, company="RT Co"),)
		critical = _critical(mapped=mapped)
		assert len(critical) == 1 and "disabled" in critical[0]


class TestTax:
	def test_an_auto_applying_tax_source_is_critical(self):
		# D4: while Egyptian VAT is deferred, an auto-applied tax row on a Retail Tower invoice fails closed.
		critical = _critical(tax_sources=("default Sales Taxes and Charges Template 'VAT 14%'",))
		assert len(critical) == 1 and "VAT 14%" in critical[0]


class TestWarnings:
	def test_validate_selling_price_is_a_warning_and_posting_continues(self):
		result = cp.evaluate(_snapshot(validate_selling_price=True))
		assert result.ok and result.status == "WARNING"
		assert len(result.warnings) == 1 and "validate_selling_price" in result.warnings[0]

	def test_enabled_selling_pricing_rules_are_a_warning(self):
		result = cp.evaluate(_snapshot(selling_pricing_rules=3))
		assert result.ok and "3" in result.warnings[0]

	def test_critical_wins_over_warning_for_the_status(self):
		result = cp.evaluate(_snapshot(validate_selling_price=True, tax_sources=("x",)))
		assert not result.ok and result.status == "CRITICAL" and result.warnings


class TestFingerprint:
	def test_the_same_site_has_the_same_fingerprint(self):
		# Two separate evaluations of two separately built (equal) snapshots must agree, and the
		# fingerprint must be a real sha256 digest (not empty / constant per call).
		first = cp.evaluate(_snapshot()).fingerprint
		second = cp.evaluate(_snapshot()).fingerprint
		assert first == second
		assert re.fullmatch(r"[0-9a-f]{64}", first)

	def test_it_does_not_depend_on_collection_order(self):
		snap = _snapshot()
		shuffled = dataclasses.replace(snap, mapped=tuple(reversed(snap.mapped)), tax_sources=("b", "a"))
		ordered = dataclasses.replace(snap, tax_sources=("a", "b"))
		assert cp.evaluate(shuffled).fingerprint == cp.evaluate(ordered).fingerprint

	@pytest.mark.parametrize(
		("field", "value"),
		[
			("erpnext_version", "15.39.0"),
			("currency_precision", 3),
			("validate_selling_price", True),
			("selling_pricing_rules", 1),
			("default_company", "Other Co"),
		],
	)
	def test_a_version_or_checked_setting_change_changes_it(self, field, value):
		assert cp.evaluate(_snapshot(**{field: value})).fingerprint != cp.evaluate(_snapshot()).fingerprint


class TestDriftEvent:
	def test_the_first_evaluation_is_a_baseline_not_drift(self):
		event = cp.drift_event(None, cp.evaluate(_snapshot()))
		assert event["event"] == "compatibility.baseline"

	def test_an_unchanged_fingerprint_emits_nothing(self):
		result = cp.evaluate(_snapshot())
		assert cp.drift_event(result.fingerprint, result) is None

	def test_a_changed_fingerprint_is_drift(self):
		before = cp.evaluate(_snapshot())
		after = cp.evaluate(_snapshot(erpnext_version="16.51.1"))
		event = cp.drift_event(before.fingerprint, after)
		assert event["event"] == "compatibility.drift"
		assert event["previous_fingerprint"] == before.fingerprint
		assert event["fingerprint"] == after.fingerprint
		assert event["status"] == "OK"


class TestReport:
	def test_the_report_lists_status_and_every_finding(self):
		result = cp.evaluate(_snapshot(validate_selling_price=True, tax_sources=("x",)))
		report = result.report()
		assert report.splitlines()[0].startswith("CRITICAL")
		assert all(line in report for line in (*result.critical, *result.warnings))


class TestModeOfPaymentAccount:
	"""Codex PR #67: ERPNext v15 get_bank_cash_account throws for a Mode of Payment without an account
	for the company, so every tender sale would be rejected one by one. Pause instead (D2)."""

	def test_a_mapped_mode_of_payment_without_a_company_account_is_critical(self):
		mapped = (
			cp.MappedRecord(
				"Mode of Payment", "Card", exists=True, disabled=False, has_company_account=False
			),
		)
		critical = _critical(mapped=mapped)
		assert len(critical) == 1 and "Card" in critical[0] and "account" in critical[0]

	def test_the_account_state_is_part_of_the_fingerprint(self):
		snap = _snapshot()
		without = dataclasses.replace(
			snap,
			mapped=(
				cp.MappedRecord(
					"Mode of Payment", "Cash", exists=True, disabled=False, has_company_account=False
				),
			),
		)
		with_account = dataclasses.replace(
			snap,
			mapped=(
				cp.MappedRecord(
					"Mode of Payment", "Cash", exists=True, disabled=False, has_company_account=True
				),
			),
		)
		assert cp.evaluate(without).fingerprint != cp.evaluate(with_account).fingerprint


class TestReviewRound:
	def test_an_unsupported_feature_is_critical_with_its_own_message(self):
		message = "ERPNext v15 < 15.67: Tax Rule auto-apply semantics not supported by the profile; upgrade to >= 15.67"
		critical = _critical(unsupported=(message,))
		assert critical == (message,)

	def test_a_default_sales_taxes_template_is_a_warning_not_critical(self):
		# Review finding 6: v15 does not reach the is_default fallback on a server-side insert; RT-201
		# probe P5 showed v16.51.0 behaves the same, so the warning names both supported lines.
		result = cp.evaluate(_snapshot(default_tax_templates=("VAT 14%",)))
		assert result.ok and result.status == "WARNING"
		assert "VAT 14%" in result.warnings[0] and "does not apply" in result.warnings[0]
		assert "v15/v16" in result.warnings[0]

	@pytest.mark.parametrize(
		("field", "value"), [("unsupported", ("x",)), ("default_tax_templates", ("VAT 14%",))]
	)
	def test_the_new_fields_are_part_of_the_fingerprint(self, field, value):
		assert cp.evaluate(_snapshot(**{field: value})).fingerprint != cp.evaluate(_snapshot()).fingerprint


class TestGroupWarehouse:
	"""Codex PR #67: ERPNext rejects a group warehouse on a stock-moving invoice; pause instead (D2)."""

	def test_a_mapped_group_warehouse_is_critical(self):
		mapped = (
			cp.MappedRecord(
				"Warehouse", "All Stores", exists=True, disabled=False, company="RT Co", is_group=True
			),
		)
		critical = _critical(mapped=mapped)
		assert len(critical) == 1 and "All Stores" in critical[0] and "group" in critical[0]

	def test_the_group_flag_is_part_of_the_fingerprint(self):
		def _snap(is_group):
			record = cp.MappedRecord(
				"Warehouse", "W", exists=True, disabled=False, company="RT Co", is_group=is_group
			)
			return cp.evaluate(_snapshot(mapped=(record,))).fingerprint

		assert _snap(True) != _snap(False)


class TestFrozenCustomerFinding:
	def test_a_blocked_frozen_customer_is_critical(self):
		mapped = (cp.MappedRecord("Customer", "Walk-in", exists=True, disabled=False, frozen_blocked=True),)
		critical = _critical(mapped=mapped)
		assert len(critical) == 1 and "frozen" in critical[0] and "Walk-in" in critical[0]

	def test_the_frozen_state_is_part_of_the_fingerprint(self):
		def _fp(blocked):
			record = cp.MappedRecord("Customer", "C", exists=True, disabled=False, frozen_blocked=blocked)
			return cp.evaluate(_snapshot(mapped=(record,))).fingerprint

		assert _fp(True) != _fp(False)


class TestBlockers:
	def test_a_blocker_is_critical_verbatim(self):
		message = "POS Profile 'Main POS' taxes template 'Old Tax' is disabled"
		assert _critical(blockers=(message,)) == (message,)

	def test_blockers_are_part_of_the_fingerprint(self):
		assert cp.evaluate(_snapshot(blockers=("x",))).fingerprint != cp.evaluate(_snapshot()).fingerprint


class TestSchedulerTick:
	"""RT-494: Frappe v16's default scheduler tick is 240 s (v15: 60 s). The every-minute posting poller
	then runs about every 4 minutes (measured on RT-201), missing RT-45. A warning, not critical:
	posting is still correct, only slower."""

	def _v16(self, **overrides):
		return _snapshot(frappe_version="16.52.0", erpnext_version="16.51.0", **overrides)

	@pytest.mark.parametrize("tick", [None, 240, 120])
	def test_v16_without_a_one_minute_tick_is_a_warning(self, tick):
		result = cp.evaluate(self._v16(scheduler_tick_s=tick))
		assert result.ok
		assert result.status == "WARNING"
		(warning,) = result.warnings
		assert "scheduler_tick_interval" in warning
		assert "RT-45" in warning

	@pytest.mark.parametrize("tick", [60, 30])
	def test_v16_with_a_tick_of_a_minute_or_less_is_ok(self, tick):
		result = cp.evaluate(self._v16(scheduler_tick_s=tick))
		assert result.ok
		assert result.warnings == ()

	def test_an_unset_tick_leaves_the_fingerprint_unchanged(self):
		# No drift event on every existing site just because this field was added.
		# An unset tick adds no key (compatibility_profile.fingerprint), so a set one moves it.
		unset = cp.evaluate(self._v16(scheduler_tick_s=None)).fingerprint
		assert cp.evaluate(self._v16(scheduler_tick_s=60)).fingerprint != unset
