# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-379 — read the ERPNext site for the Compatibility Profile and record the result.

⏳ BENCH-VALIDATION: imports ``frappe``; covered locally only against a stand-in.

:func:`run_preflight` is called by the poller after the Gate G5 index guard and before any page is
pulled. It reads a :class:`compatibility_profile.SiteSnapshot`, evaluates it, records the status on
Connector Settings (D3: Connector-local visibility) and returns whether posting may proceed. A
critical finding pauses posting (D2); an unreadable site is treated as critical.

The profile only READS ERPNext. Its single write is the read-only status fields on Connector
Settings, via ``set_single_value`` without bumping ``modified`` (no timestamp clash with an operator
editing the form). It never changes an ERP setting to pass (668).

Tax sources (D4), per ERPNext v15 source (``accounts_controller.set_taxes_and_charges``,
``sales_invoice.set_pos_fields``, ``party.set_taxes``), for a server-side insert:

* a default Sales Taxes and Charges Template of the company (listed by the ratified scope);
* an Item Tax Template assigned to an Item or Item Group, while Accounts Settings
  ``add_taxes_from_item_tax_template`` is on (the v15 default);
* a Sales Tax Rule, while ``add_taxes_from_taxes_and_charges_template`` is on;
* a POS Profile of the company with ``taxes_and_charges`` (paid ``is_pos`` sales take it).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping

import frappe

from .compatibility_profile import REQUIRED_CUSTOM_FIELDS, MappedRecord, SiteSnapshot, drift_event, evaluate

_SETTINGS = "Connector Settings"
_LOGGER = "retail_tower_posting"

# doctype -> (flag field, flag means "enabled")
_FLAGS = {
	"UOM": ("enabled", True),
	"Warehouse": ("disabled", False),
	"Customer": ("disabled", False),
	"Mode of Payment": ("enabled", True),
}


def run_preflight(settings, maps: Mapping[str, Mapping]) -> bool:
	"""Evaluate the profile; record and log it. ``True`` when posting may proceed this tick."""
	try:
		snapshot = read_snapshot(settings, maps)
	except Exception as exc:  # an unreadable site must pause posting, never wave it through
		detail = _safe(f"{type(exc).__name__}: {exc}")
		_log({"event": "compatibility.unreadable", "detail": detail})
		# Best effort: show the pause on Connector Settings (Codex PR #67). The fingerprint is kept,
		# so a recovery is compared with the last real evaluation.
		_write(
			{
				"compatibility_status": "CRITICAL",
				"compatibility_report": f"CRITICAL\ncritical: the site could not be read: {detail}",
			}
		)
		return False
	result = evaluate(snapshot)
	event = drift_event(settings.get("compatibility_fingerprint"), result)
	if event is not None:
		_log(event)
	if not result.ok:
		_log({"event": "compatibility.critical", "critical": list(result.critical)})
	_record(result)
	return result.ok


def read_snapshot(settings, maps: Mapping[str, Mapping]) -> SiteSnapshot:
	"""⏳ BENCH-VALIDATION. Read what the profile checks; reads only."""
	company = frappe.db.get_single_value("Global Defaults", "default_company") or None
	currency = settings.get("posting_currency") or None
	return SiteSnapshot(
		frappe_version=getattr(frappe, "__version__", None),
		erpnext_version=_erpnext_version(),
		custom_fields=frozenset(
			frappe.get_all(
				"Custom Field", filters={"name": ["in", list(REQUIRED_CUSTOM_FIELDS)]}, pluck="name"
			)
		),
		posting_currency=currency,
		currency_precision=_currency_precision(currency),
		default_company=company,
		company_currency=frappe.db.get_value("Company", company, "default_currency") if company else None,
		mapped=_mapped_records(maps, company),
		tax_sources=_tax_sources(company),
		validate_selling_price=_flag("Selling Settings", "validate_selling_price"),
		selling_pricing_rules=int(frappe.db.count("Pricing Rule", {"selling": 1, "disable": 0}) or 0),
	)


def _erpnext_version() -> str | None:
	import erpnext

	return getattr(erpnext, "__version__", None)


def _currency_precision(currency: str | None) -> int | None:
	"""The precision ERPNext applies to a Sales Invoice Item rate in ``currency`` (Frappe's own rule)."""
	if not currency:
		return None
	from frappe.model.meta import get_field_precision

	df = frappe.get_meta("Sales Invoice Item").get_field("rate")
	return int(get_field_precision(df, currency=currency))


def _flag(doctype: str, field: str) -> bool:
	return bool(int(frappe.db.get_single_value(doctype, field) or 0))


def _mapped_names(maps: Mapping[str, Mapping]) -> dict[str, set[str]]:
	warehouses = {ref["name"] for ref in (maps.get("warehouse") or {}).values()}
	return {
		"UOM": set((maps.get("uom") or {}).values()),
		"Warehouse": warehouses,
		"Customer": set((maps.get("customer") or {}).values()),
		"Mode of Payment": set((maps.get("tender") or {}).values()),
	}


def _mapped_records(maps: Mapping[str, Mapping], company: str | None) -> tuple[MappedRecord, ...]:
	return tuple(
		_read_record(doctype, name, company)
		for doctype, names in _mapped_names(maps).items()
		for name in sorted(names)
	)


def _read_record(doctype: str, name: str, company: str | None) -> MappedRecord:
	flag = _FLAGS[doctype][0]
	fields = [flag, "company"] if doctype == "Warehouse" else [flag]
	record = _record_for(doctype, name, frappe.db.get_value(doctype, name, fields, as_dict=True))
	if doctype == "Mode of Payment" and record.exists:
		record = dataclasses.replace(record, has_company_account=_has_company_account(name, company))
	return record


def _has_company_account(mode_of_payment: str, company: str | None) -> bool:
	"""ERPNext v15 ``get_bank_cash_account`` needs a default account for the company on the mode."""
	accounts = frappe.get_all(
		"Mode of Payment Account",
		filters={"parent": mode_of_payment, "company": company},
		pluck="default_account",
	)
	return any(accounts)


def _record_for(doctype: str, name: str, row) -> MappedRecord:
	if not row:
		return MappedRecord(doctype, name, exists=False, disabled=False)
	flag, enabled_flag = _FLAGS[doctype]
	value = bool(int(row.get(flag) or 0))
	return MappedRecord(
		doctype,
		name,
		exists=True,
		disabled=(not value) if enabled_flag else value,
		company=row.get("company"),
	)


def _tax_sources(company: str | None) -> tuple[str, ...]:
	"""Every source that would auto-apply a tax row to a Retail Tower invoice (D4)."""
	if not company:
		return ()
	return (
		*_default_templates(company),
		*_item_tax_templates(company),
		*_tax_rules(company),
		*_pos_profiles(company),
	)


def _default_templates(company: str) -> list[str]:
	names = frappe.get_all(
		"Sales Taxes and Charges Template",
		filters={"is_default": 1, "disabled": 0, "company": company},
		pluck="name",
	)
	return [f"default Sales Taxes and Charges Template {name!r}" for name in names]


def _item_tax_templates(company: str) -> list[str]:
	if not _flag("Accounts Settings", "add_taxes_from_item_tax_template"):
		return []
	templates = frappe.get_all("Item Tax Template", filters={"disabled": 0, "company": company}, pluck="name")
	if not templates:
		return []
	rows = frappe.get_all(
		"Item Tax",
		filters={"item_tax_template": ["in", templates], "parenttype": ["in", ["Item", "Item Group"]]},
		fields=["item_tax_template"],
	)
	assigned = sorted({row["item_tax_template"] for row in rows})
	return [
		f"Item Tax Template {name!r} on items (Accounts Settings add_taxes_from_item_tax_template)"
		for name in assigned
	]


def _tax_rules(company: str) -> list[str]:
	if not _flag("Accounts Settings", "add_taxes_from_taxes_and_charges_template"):
		return []
	rules = frappe.get_all("Tax Rule", filters={"tax_type": "Sales"}, fields=["name", "company"])
	return [
		f"Sales Tax Rule {rule['name']!r} (Accounts Settings add_taxes_from_taxes_and_charges_template)"
		for rule in rules
		if rule.get("company") in (None, "", company)
	]


def _pos_profiles(company: str) -> list[str]:
	names = frappe.get_all(
		"POS Profile",
		filters={"disabled": 0, "company": company, "taxes_and_charges": ["is", "set"]},
		pluck="name",
	)
	return [f"POS Profile {name!r} taxes_and_charges (paid sales)" for name in names]


def _record(result) -> None:
	"""Write the read-only status fields (D3). A write failure is logged; the decision stands."""
	_write(
		{
			"compatibility_status": result.status,
			"compatibility_report": result.report(),
			"compatibility_fingerprint": result.fingerprint,
		}
	)


def _write(values: dict) -> None:
	"""Write status fields (plus the check time) without bumping ``modified``; a failure is logged."""
	try:
		frappe.db.set_single_value(
			_SETTINGS,
			{**values, "compatibility_checked_at": frappe.utils.now_datetime()},
			update_modified=False,
		)
	except Exception as exc:
		_log({"event": "compatibility.record_failed", "detail": _safe(f"{type(exc).__name__}: {exc}")})


def _log(record: dict) -> None:
	"""ERROR level: Frappe drops lower levels in production (the RT-49 convention)."""
	frappe.logger(_LOGGER).error(record)


def _safe(message: str) -> str:
	from .reasons import scrub_message

	return scrub_message(message)[:500]
