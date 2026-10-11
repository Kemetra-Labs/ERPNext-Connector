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

* an Item Tax Template assigned to an Item or Item Group, while Accounts Settings
  ``add_taxes_from_item_tax_template`` is on (the v15 default);
* a Sales Tax Rule that ERPNext's own evaluation (``party.set_taxes`` → ``get_tax_template``:
  customer, group, dates, disabled template...) applies to a mapped Customer, while
  ``add_taxes_from_taxes_and_charges_template`` is on;
* a POS Profile of the company with ``taxes_and_charges`` (paid ``is_pos`` sales take it), including
  the scheduler user's default profile even when disabled.

A default Sales Taxes and Charges Template is NOT applied on a server-side insert in v15 (the
``is_default`` fallback in ``set_taxes`` is not reached), so it is a warning, not critical.
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
		# Best effort: show the pause on Connector Settings (Codex PR #67).
		_pause(
			"compatibility.unreadable", f"the site could not be read: {_safe(f'{type(exc).__name__}: {exc}')}"
		)
		return False
	try:
		result = evaluate(snapshot)
		event = drift_event(settings.get("compatibility_fingerprint"), result)
		if event is not None:
			_log(event)
		if not result.ok:
			_log({"event": "compatibility.critical", "critical": list(result.critical)})
	except Exception as exc:  # never a stale OK and never a pull on a profile error
		_pause(
			"compatibility.failed",
			f"the profile could not be evaluated: {_safe(f'{type(exc).__name__}: {exc}')}",
		)
		return False
	_record(result)
	return result.ok


def _pause(event: str, reason: str) -> None:
	"""Log ``event`` and record a CRITICAL status (best effort). The fingerprint is kept, so a
	recovery is compared with the last real evaluation."""
	try:
		_log({"event": event, "detail": reason})
	finally:
		_write({"compatibility_status": "CRITICAL", "compatibility_report": f"CRITICAL\ncritical: {reason}"})


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
		tax_sources=_tax_sources(company, set((maps.get("customer") or {}).values())),
		validate_selling_price=_flag("Selling Settings", "validate_selling_price"),
		selling_pricing_rules=int(frappe.db.count("Pricing Rule", {"selling": 1, "disable": 0}) or 0),
		unsupported=_unsupported(),
		default_tax_templates=_default_templates(company) if company else (),
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


# Accounts Settings flags the tax checks read, and the honest message when this v15 minor lacks one
# (``get_single_value`` throws on an unknown field, which would otherwise read as "unreadable").
_ACCOUNTS_FLAGS = {
	"add_taxes_from_taxes_and_charges_template": (
		"ERPNext v15 < 15.67: Tax Rule auto-apply semantics not supported by the profile; upgrade to >= 15.67"
	),
	"add_taxes_from_item_tax_template": (
		"ERPNext without Accounts Settings add_taxes_from_item_tax_template: Item Tax Template auto-apply "
		"semantics not supported by the profile; upgrade ERPNext v15"
	),
}


def _accounts_flag(field: str) -> bool | None:
	"""An Accounts Settings flag, or ``None`` when this ERPNext minor does not have the field."""
	if not frappe.get_meta("Accounts Settings").has_field(field):
		return None
	return _flag("Accounts Settings", field)


def _unsupported() -> tuple[str, ...]:
	return tuple(message for field, message in _ACCOUNTS_FLAGS.items() if _accounts_flag(field) is None)


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


def _tax_sources(company: str | None, customers: set[str]) -> tuple[str, ...]:
	"""Every source that would auto-apply a tax row to a Retail Tower invoice (D4)."""
	if not company:
		return ()
	return (
		*_item_tax_templates(company),
		*_tax_rules(company, customers),
		*_pos_profiles(company),
	)


def _default_templates(company: str) -> tuple[str, ...]:
	"""Default Sales Taxes and Charges Templates (a warning: v15 does not apply them on insert)."""
	return tuple(
		frappe.get_all(
			"Sales Taxes and Charges Template",
			filters={"is_default": 1, "disabled": 0, "company": company},
			pluck="name",
		)
	)


def _item_tax_templates(company: str) -> list[str]:
	if not _accounts_flag("add_taxes_from_item_tax_template"):
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


def _tax_rules(company: str, customers: set[str]) -> list[str]:
	"""Tax Rule templates ERPNext itself would set for a mapped Customer today (Codex PR #67).

	``party.set_taxes`` runs ERPNext's ``get_tax_template`` (customer, group, tax category, dates,
	disabled templates), so an expired rule or one restricted to another customer is not reported.
	"""
	if not _accounts_flag("add_taxes_from_taxes_and_charges_template"):
		return []
	from erpnext.accounts.party import set_taxes

	today = frappe.utils.nowdate()
	found = []
	for customer in sorted(customers):
		party = (
			frappe.db.get_value("Customer", customer, ["customer_group", "tax_category"], as_dict=True) or {}
		)
		template = set_taxes(
			customer,
			"Customer",
			today,
			company,
			customer_group=party.get("customer_group"),
			tax_category=_tax_category(customer, party.get("tax_category")),
		)
		if template:
			found.append(
				f"Tax Rule template {template!r} applies to mapped Customer {customer!r} "
				"(Accounts Settings add_taxes_from_taxes_and_charges_template)"
			)
	return found


def _tax_category(customer: str, customer_category: str | None) -> str:
	"""The tax category ERPNext v15 derives on insert (``party._get_party_details``): the default
	billing (or, per Accounts Settings, shipping) Address's category overrides the Customer's."""
	from erpnext.accounts.party import get_address_tax_category
	from frappe.contacts.doctype.address.address import get_default_address

	billing = get_default_address("Customer", customer)
	shipping = get_default_address("Customer", customer, "is_shipping_address")
	return get_address_tax_category(customer_category, billing, shipping)


def _pos_profiles(company: str) -> list[str]:
	"""POS Profiles ``get_pos_profile`` can pick for a paid sale: any enabled one of the company, and
	the scheduler user's default one whatever its disabled flag (that query has no disabled filter)."""
	enabled = frappe.get_all(
		"POS Profile",
		filters={"disabled": 0, "company": company, "taxes_and_charges": ["is", "set"]},
		pluck="name",
	)
	return [
		f"POS Profile {name!r} taxes_and_charges (paid sales)"
		for name in sorted({*enabled, *_user_default_pos_profiles(company)})
	]


def _user_default_pos_profiles(company: str) -> list[str]:
	users = sorted({"Administrator", frappe.session.user})
	parents = frappe.get_all(
		"POS Profile User", filters={"user": ["in", users], "default": 1}, pluck="parent"
	)
	if not parents:
		return []
	return frappe.get_all(
		"POS Profile",
		filters={"name": ["in", parents], "company": company, "taxes_and_charges": ["is", "set"]},
		pluck="name",
	)


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
