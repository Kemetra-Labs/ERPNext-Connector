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

A default Sales Taxes and Charges Template is NOT applied on a server-side insert in v15 or v16 (the
``is_default`` fallback in ``set_taxes`` is not reached), so it is a warning, not critical.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass

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
	pos = _pos_profile_templates(company) if company else {}
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
		tax_sources=_tax_sources(company, set((maps.get("customer") or {}).values()), pos),
		validate_selling_price=_flag("Selling Settings", "validate_selling_price"),
		selling_pricing_rules=int(frappe.db.count("Pricing Rule", {"selling": 1, "disable": 0}) or 0),
		unsupported=_unsupported(),
		blockers=_disabled_pos_templates(pos),
		default_tax_templates=_default_templates(company) if company else (),
		scheduler_tick_s=_scheduler_tick(),
	)


def _scheduler_tick() -> int | None:
	"""⏳ BENCH-VALIDATION. ``scheduler_tick_interval`` from the site config (common_site_config is
	merged into ``frappe.conf``), or None when unset (RT-494)."""
	conf = getattr(frappe, "conf", None) or {}
	value = int(conf.get("scheduler_tick_interval") or 0)
	return value or None


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


# Extra fields read per mapped doctype, besides its enabled/disabled flag.
_EXTRA_FIELDS = {"Warehouse": ["company", "is_group"], "Customer": ["is_frozen"]}


def _read_record(doctype: str, name: str, company: str | None) -> MappedRecord:
	fields = [_FLAGS[doctype][0], *_EXTRA_FIELDS.get(doctype, [])]
	row = frappe.db.get_value(doctype, name, fields, as_dict=True)
	record = _record_for(doctype, name, row)
	if doctype == "Mode of Payment" and record.exists:
		record = dataclasses.replace(record, has_company_account=_has_company_account(name, company))
	if doctype == "Customer" and record.exists:
		record = dataclasses.replace(record, frozen_blocked=_frozen_blocked(row))
	return record


def _frozen_blocked(row) -> bool:
	"""ERPNext v15 ``validate_party_frozen_disabled``: a frozen Customer throws unless the session user
	has the ``frozen_accounts_modifier`` role (always, when that role is not configured)."""
	if not int(row.get("is_frozen") or 0):
		return False
	modifier = frappe.db.get_single_value("Accounts Settings", "frozen_accounts_modifier")
	return modifier not in frappe.get_roles()


def _has_tax_rows(template: str | None) -> bool:
	"""An empty Sales Taxes and Charges Template adds no tax row (Codex PR #67)."""
	if not template:
		return False
	return bool(
		frappe.get_all(
			"Sales Taxes and Charges",
			filters={"parent": template, "parenttype": "Sales Taxes and Charges Template"},
			pluck="parent",
		)
	)


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
		is_group=bool(int(row.get("is_group") or 0)),
	)


@dataclass(frozen=True)
class _CustomerContext:
	"""What ERPNext v15 derives for a mapped Customer on insert (``party._get_party_details``)."""

	customer: str
	customer_group: str | None
	tax_category: str


def _customer_contexts(customers: set[str]) -> list[_CustomerContext]:
	contexts = []
	for customer in sorted(customers):
		party = (
			frappe.db.get_value("Customer", customer, ["customer_group", "tax_category"], as_dict=True) or {}
		)
		contexts.append(
			_CustomerContext(
				customer, party.get("customer_group"), _tax_category(customer, party.get("tax_category"))
			)
		)
	return contexts


def _tax_sources(company: str | None, customers: set[str], pos: dict[str, str]) -> tuple[str, ...]:
	"""Every source that would auto-apply a tax row to a Retail Tower invoice (D4)."""
	if not company:
		return ()
	# Address lookups only when a tax flag needs the customers' derived categories (re-check trivial).
	needs_contexts = any(_accounts_flag(field) for field in _ACCOUNTS_FLAGS)
	contexts = _customer_contexts(customers) if needs_contexts else []
	return (
		*_item_tax_templates(company, contexts),
		*_tax_rules(company, contexts),
		*_pos_profiles(pos),
	)


def _default_templates(company: str) -> tuple[str, ...]:
	"""Default Sales Taxes and Charges Templates (a warning: v15/v16 do not apply them on insert)."""
	return tuple(
		frappe.get_all(
			"Sales Taxes and Charges Template",
			filters={"is_default": 1, "disabled": 0, "company": company},
			pluck="name",
		)
	)


def _item_tax_templates(company: str, contexts: list[_CustomerContext]) -> list[str]:
	"""Item Tax assignments ERPNext v15 ``_get_item_tax_template`` can select today (Codex PR #67).

	A row is selected only for the invoice's derived tax category (``cstr`` equality, so a blank row
	category matches a blank customer category) and once ``valid_from`` has passed. The net-rate range
	depends on each line's rate, unknown before pulling, so it is treated as applicable.
	"""
	if not _accounts_flag("add_taxes_from_item_tax_template"):
		return []
	templates = frappe.get_all("Item Tax Template", filters={"disabled": 0, "company": company}, pluck="name")
	if not templates:
		return []
	rows = frappe.get_all(
		"Item Tax",
		filters={"item_tax_template": ["in", templates], "parenttype": ["in", ["Item", "Item Group"]]},
		fields=["item_tax_template", "tax_category", "valid_from"],
	)
	categories = {context.tax_category for context in contexts}
	today = frappe.utils.nowdate()
	assigned = sorted(
		{
			row["item_tax_template"]
			for row in rows
			if (row.get("tax_category") or "") in categories and _valid_by(row.get("valid_from"), today)
		}
	)
	return [
		f"Item Tax Template {name!r} on items (Accounts Settings add_taxes_from_item_tax_template)"
		for name in assigned
	]


def _valid_by(valid_from, today: str) -> bool:
	"""``valid_from`` (a date or ISO string, or unset) has been reached by ``today`` (ISO)."""
	return not valid_from or str(valid_from) <= today


def _tax_rules(company: str, contexts: list[_CustomerContext]) -> list[str]:
	"""Tax Rule templates ERPNext itself would set for a mapped Customer today (Codex PR #67).

	``party.set_taxes`` runs ERPNext's ``get_tax_template`` (customer, group, tax category, dates,
	disabled templates), so an expired rule or one restricted to another customer is not reported.
	"""
	if not _accounts_flag("add_taxes_from_taxes_and_charges_template"):
		return []
	from erpnext.accounts.party import set_taxes

	today = frappe.utils.nowdate()
	found = []
	for context in contexts:
		template = set_taxes(
			context.customer,
			"Customer",
			today,
			company,
			customer_group=context.customer_group,
			tax_category=context.tax_category,
		)
		if _has_tax_rows(template):
			found.append(
				f"Tax Rule template {template!r} applies to mapped Customer {context.customer!r} "
				"(Accounts Settings add_taxes_from_taxes_and_charges_template)"
			)
	return found


def _tax_category(customer: str, customer_category: str | None) -> str:
	"""The tax category ERPNext v15 derives on insert (``party._get_party_details``): the default
	billing (or, per Accounts Settings, shipping) Address's category overrides the Customer's."""
	from erpnext.accounts.party import get_address_tax_category, get_party_shipping_address
	from frappe.contacts.doctype.address.address import get_default_address

	# As v15 set_address_details does for a payload with no addresses: the default billing Address, and
	# get_party_shipping_address (address_type Shipping or is_shipping_address). get_default_address's
	# third argument is only a sort key, so it could return a billing address (re-check N1).
	billing = get_default_address("Customer", customer)
	shipping = get_party_shipping_address("Customer", customer)
	return get_address_tax_category(customer_category, billing, shipping)


def _pos_profile_templates(company: str) -> dict[str, str]:
	"""``{POS Profile: taxes template}`` for the profiles ``get_pos_profile`` can pick for a paid sale:
	any enabled one of the company, and the scheduler user's default one whatever its disabled flag
	(that query has no disabled filter)."""
	enabled = frappe.get_all(
		"POS Profile",
		filters={"disabled": 0, "company": company, "taxes_and_charges": ["is", "set"]},
		fields=["name", "taxes_and_charges"],
	)
	return {row["name"]: row["taxes_and_charges"] for row in (*enabled, *_user_default_pos_profiles(company))}


def _template_problem(template: str) -> str | None:
	"""Why ERPNext v15 would reject every paid sale taking ``template``, or ``None`` when it is usable.

	A deleted template fails link validation; a disabled one fails ``validate_enabled_taxes_and_charges``.
	"""
	disabled = frappe.db.get_value("Sales Taxes and Charges Template", template, "disabled")
	if disabled is None:
		return "does not exist"
	return "is disabled" if int(disabled or 0) else None


def _pos_profiles(pos: dict[str, str]) -> list[str]:
	"""Profiles whose usable template has tax rows: the template's taxes would be added."""
	return [
		f"POS Profile {name!r} taxes_and_charges (paid sales)"
		for name, template in sorted(pos.items())
		if _template_problem(template) is None and _has_tax_rows(template)
	]


def _disabled_pos_templates(pos: dict[str, str]) -> tuple[str, ...]:
	"""A POS Profile whose taxes template is disabled or deleted: ERPNext rejects every paid sale."""
	problems = {name: (template, _template_problem(template)) for name, template in pos.items()}
	return tuple(
		f"POS Profile {name!r} taxes template {template!r} {problem}: ERPNext would reject every paid sale"
		for name, (template, problem) in sorted(problems.items())
		if problem
	)


def _user_default_pos_profiles(company: str) -> list[dict]:
	users = sorted({"Administrator", frappe.session.user})
	parents = frappe.get_all(
		"POS Profile User", filters={"user": ["in", users], "default": 1}, pluck="parent"
	)
	if not parents:
		return []
	return frappe.get_all(
		"POS Profile",
		filters={"name": ["in", parents], "company": company, "taxes_and_charges": ["is", "set"]},
		fields=["name", "taxes_and_charges"],
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
