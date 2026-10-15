# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-379 (RT-283 F4, design C) — the ERPNext Compatibility Profile (pure).

Pricing 665-674: an incompatible ERPNext site must be detected BEFORE a sale depends on it. The glue
reads a :class:`SiteSnapshot`; :func:`evaluate` returns critical findings, warnings and a drift
fingerprint. It is composed with the Gate G5 index guard (``index_guard``), never a replacement.

**Critical** (D2: the poller does not pull; nothing is rejected; the backlog stays in Backend-Core):

* a Frappe or ERPNext major version outside :data:`SUPPORTED_MAJORS` (D5, amended on RT-201/RT-494:
  v15 and v16), an unreadable version, or Frappe and ERPNext on different majors;
* a required Retail Tower custom field missing (the ``hooks.py`` fixture list);
* no posting currency configured, a posting currency with unknown ISO 4217 minor units, or an
  ERPNext currency precision below those minor units;
* company currency other than the posting currency;
* Global Defaults ``default_company`` missing (RT-378 posts every sale on it explicitly), or a mapped
  Warehouse belonging to another company (comment 11556);
* a mapped UOM, Warehouse, Customer or Mode of Payment that is missing or disabled, or a mapped Mode of
  Payment without an account for ``default_company`` (ERPNext v15 ``get_bank_cash_account`` would
  throw for every tender sale), or a mapped group Warehouse (ERPNext rejects it on the invoice);
* a tax source that would auto-apply a tax row to a Retail Tower invoice (D4);
* a check this ERPNext minor cannot support (e.g. v15 < 15.67 has no Accounts Settings
  ``add_taxes_from_taxes_and_charges_template``), with an explicit message.

**Warnings** (recorded and logged; posting continues): Selling Settings ``validate_selling_price``
enabled (input to Pricing Planning Gate 2), enabled selling Pricing Rules (neutralised per document
by RT-377; shown for visibility), and a default Sales Taxes and Charges Template (ERPNext v15 and
v16 do not apply it on a server-side insert: review finding 6, RT-201 probe P5).

The Connector never changes ERP settings to pass the profile (668). This module imports NO frappe.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

# D5: the Frappe/ERPNext majors the Connector is validated on. v16 added on RT-494 after the RT-201
# verification (validated pin: Frappe 16.52.0 / ERPNext 16.51.0); v15 stays while sites migrate.
SUPPORTED_MAJORS = frozenset({15, 16})
_SUPPORTED_LABEL = ", ".join(f"v{major}" for major in sorted(SUPPORTED_MAJORS))

# RT-494: the posting poller's cron is every minute (RT-45); Frappe v16's scheduler wakes every 240 s
# unless the site config sets scheduler_tick_interval.
_POSTING_TICK_S = 60
_V16_TICK_MAJOR = 16
_V16_DEFAULT_TICK_S = 240

# The custom fields the app ships (hooks.py fixtures); a test pins the two lists together.
REQUIRED_CUSTOM_FIELDS = (
	"Sales Invoice-rt_source_system",
	"Sales Invoice-rt_external_id",
	"Sales Invoice-rt_sale_ref",
	"Sales Invoice Item-rt_line_ref",
)

# ISO 4217 minor units for the currencies a Retail Tower tenant may post in. An unknown currency is
# critical (never guessed): add it here when a tenant needs it.
ISO_MINOR_UNITS = {
	"AED": 2,
	"BHD": 3,
	"EGP": 2,
	"EUR": 2,
	"GBP": 2,
	"JOD": 3,
	"JPY": 0,
	"KWD": 3,
	"OMR": 3,
	"QAR": 2,
	"SAR": 2,
	"USD": 2,
}

_MAJOR_RE = re.compile(r"^(\d+)\.")


@dataclass(frozen=True)
class MappedRecord:
	"""One record a Connector Settings map points at, as the site holds it."""

	doctype: str
	name: str
	exists: bool
	disabled: bool
	company: str | None = None
	# Mode of Payment only: has a Mode of Payment Account for default_company (None = not applicable).
	has_company_account: bool | None = None
	# Warehouse only: a group warehouse cannot hold stock (ERPNext rejects it on the invoice).
	is_group: bool = False
	# Customer only: frozen and the scheduler user lacks the frozen_accounts_modifier role.
	frozen_blocked: bool = False


@dataclass(frozen=True)
class SiteSnapshot:
	"""What the glue read from the site; every field is a value, never a frappe object."""

	frappe_version: str | None
	erpnext_version: str | None
	custom_fields: frozenset[str]
	posting_currency: str | None
	currency_precision: int | None
	default_company: str | None
	company_currency: str | None
	mapped: tuple[MappedRecord, ...]
	tax_sources: tuple[str, ...]
	validate_selling_price: bool
	selling_pricing_rules: int
	# Checks this ERPNext minor cannot support (critical, with their own message).
	unsupported: tuple[str, ...] = ()
	# Settings ERPNext would reject on every sale (critical, with their own message).
	blockers: tuple[str, ...] = ()
	# Default Sales Taxes and Charges Templates: visibility only, v15/v16 do not apply them on insert.
	default_tax_templates: tuple[str, ...] = ()
	# The site config ``scheduler_tick_interval`` in seconds, or None when unset (RT-494).
	scheduler_tick_s: int | None = None


@dataclass(frozen=True)
class ProfileResult:
	critical: tuple[str, ...]
	warnings: tuple[str, ...]
	fingerprint: str

	@property
	def ok(self) -> bool:
		"""Posting may proceed (warnings never block)."""
		return not self.critical

	@property
	def status(self) -> str:
		if self.critical:
			return "CRITICAL"
		return "WARNING" if self.warnings else "OK"

	def report(self) -> str:
		"""The read-only Connector Settings report: the status, then one line per finding."""
		lines = [self.status]
		lines += [f"critical: {line}" for line in self.critical]
		lines += [f"warning: {line}" for line in self.warnings]
		return "\n".join(lines)


def evaluate(snapshot: SiteSnapshot) -> ProfileResult:
	"""Critical findings, warnings and the drift fingerprint for ``snapshot``."""
	critical = (
		*snapshot.unsupported,
		*snapshot.blockers,
		*_version_findings(snapshot),
		*_custom_field_findings(snapshot),
		*_currency_findings(snapshot),
		*_company_findings(snapshot),
		*_mapped_findings(snapshot),
		*_tax_findings(snapshot),
	)
	return ProfileResult(critical=critical, warnings=_warnings(snapshot), fingerprint=fingerprint(snapshot))


def _major(version: str | None) -> int | None:
	match = _MAJOR_RE.match(version or "")
	return int(match.group(1)) if match else None


def _version_findings(snapshot: SiteSnapshot) -> list[str]:
	findings = []
	majors = []
	for app, version in (("Frappe", snapshot.frappe_version), ("ERPNext", snapshot.erpnext_version)):
		major = _major(version)
		majors.append(major)
		if major is None:
			findings.append(f"{app} version {version!r} cannot be read (supported: {_SUPPORTED_LABEL}, D5)")
		elif major not in SUPPORTED_MAJORS:
			findings.append(f"{app} {version} is outside the supported set {_SUPPORTED_LABEL} (D5)")
	if not findings and majors[0] != majors[1]:
		# Each major is supported on its own, but ERPNext runs only on its own Frappe major (RT-494).
		findings.append(
			f"Frappe {snapshot.frappe_version} and ERPNext {snapshot.erpnext_version} are on different "
			"majors: an unsupported combination (D5)"
		)
	return findings


def _custom_field_findings(snapshot: SiteSnapshot) -> list[str]:
	missing = [name for name in REQUIRED_CUSTOM_FIELDS if name not in snapshot.custom_fields]
	return [f"required custom field(s) missing: {', '.join(missing)}"] if missing else []


def _currency_findings(snapshot: SiteSnapshot) -> list[str]:
	currency = snapshot.posting_currency
	if not currency:
		return ["no posting currency configured (Connector Settings posting_currency)"]
	findings = []
	minor = ISO_MINOR_UNITS.get(currency)
	if minor is None:
		findings.append(f"posting currency {currency} has no known ISO 4217 minor units")
	elif snapshot.currency_precision is None or snapshot.currency_precision < minor:
		findings.append(
			f"ERPNext currency precision {snapshot.currency_precision!r} is below the {minor} minor "
			f"unit(s) of {currency}: Retail Tower amounts would be rounded"
		)
	if snapshot.company_currency != currency:
		findings.append(
			f"company currency {snapshot.company_currency!r} is not the posting currency {currency}"
		)
	return findings


def _company_findings(snapshot: SiteSnapshot) -> list[str]:
	company = snapshot.default_company
	if not company:
		return ["Global Defaults default_company is not configured: every sale would be rejected"]
	return [
		f"mapped Warehouse {record.name!r} belongs to company {record.company!r}, not {company!r}"
		for record in snapshot.mapped
		if record.doctype == "Warehouse" and record.exists and record.company != company
	]


def _mapped_findings(snapshot: SiteSnapshot) -> list[str]:
	findings = []
	for record in snapshot.mapped:
		if not record.exists:
			findings.append(f"mapped {record.doctype} {record.name!r} does not exist")
		elif record.disabled:
			findings.append(f"mapped {record.doctype} {record.name!r} is disabled")
		elif record.frozen_blocked:
			findings.append(
				f"mapped {record.doctype} {record.name!r} is frozen and the scheduler user lacks the frozen_accounts_modifier role"
			)
		elif record.is_group:
			findings.append(
				f"mapped {record.doctype} {record.name!r} is a group warehouse (cannot hold stock)"
			)
		elif record.has_company_account is False:
			findings.append(f"mapped {record.doctype} {record.name!r} has no account for the default company")
	return findings


def _tax_findings(snapshot: SiteSnapshot) -> list[str]:
	return [
		f"tax would auto-apply to Retail Tower invoices (VAT deferred, D4): {source}"
		for source in snapshot.tax_sources
	]


def _warnings(snapshot: SiteSnapshot) -> tuple[str, ...]:
	warnings = []
	if snapshot.validate_selling_price:
		warnings.append(
			"Selling Settings validate_selling_price is enabled (input to Pricing Planning Gate 2)"
		)
	warnings += [
		f"default Sales Taxes and Charges Template {name!r} is set: ERPNext v15/v16 does not apply it on a "
		"server-side insert (the is_default fallback is not reached), and RT-378 rejects any tax row"
		for name in snapshot.default_tax_templates
	]
	if snapshot.selling_pricing_rules:
		warnings.append(
			f"{snapshot.selling_pricing_rules} enabled selling Pricing Rule(s); neutralised per document "
			"by ignore_pricing_rule (RT-377)"
		)
	warnings += _scheduler_tick_warnings(snapshot)
	return tuple(warnings)


def _scheduler_tick_warnings(snapshot: SiteSnapshot) -> list[str]:
	"""Frappe v16 ticks every 240 s by default (v15: 60 s): the every-minute poller would run every
	~4 minutes and miss RT-45 (measured on RT-201). The fix is site config, so this only warns."""
	major = _major(snapshot.frappe_version)
	if major is None or major < _V16_TICK_MAJOR:
		return []
	tick = snapshot.scheduler_tick_s or _V16_DEFAULT_TICK_S
	if tick <= _POSTING_TICK_S:
		return []
	return [
		f"Frappe {snapshot.frappe_version} scheduler tick is {tick} s"
		f"{'' if snapshot.scheduler_tick_s else ' (the v16 default)'}: the every-minute posting poller "
		f"runs only every {tick} s and misses RT-45; set scheduler_tick_interval: {_POSTING_TICK_S} in "
		"common_site_config.json"
	]


def fingerprint(snapshot: SiteSnapshot) -> str:
	"""sha256 of the versions and checked settings, independent of collection order (672/673)."""
	canonical = {
		"frappe_version": snapshot.frappe_version,
		"erpnext_version": snapshot.erpnext_version,
		"custom_fields": sorted(snapshot.custom_fields),
		"posting_currency": snapshot.posting_currency,
		"currency_precision": snapshot.currency_precision,
		"default_company": snapshot.default_company,
		"company_currency": snapshot.company_currency,
		"mapped": sorted(
			[
				r.doctype,
				r.name,
				r.exists,
				r.disabled,
				r.company or "",
				str(r.has_company_account),
				r.is_group,
				r.frozen_blocked,
			]
			for r in snapshot.mapped
		),
		"tax_sources": sorted(snapshot.tax_sources),
		"validate_selling_price": snapshot.validate_selling_price,
		"selling_pricing_rules": snapshot.selling_pricing_rules,
		"unsupported": sorted(snapshot.unsupported),
		"blockers": sorted(snapshot.blockers),
		"default_tax_templates": sorted(snapshot.default_tax_templates),
	}
	if snapshot.scheduler_tick_s is not None:
		# Only when set (RT-494), so adding the field does not move every existing site's fingerprint.
		canonical["scheduler_tick_s"] = snapshot.scheduler_tick_s
	payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def drift_event(previous_fingerprint: str | None, result: ProfileResult) -> dict | None:
	"""The structured log event for this evaluation, or ``None`` when nothing changed.

	The first evaluation (no stored fingerprint) is a ``compatibility.baseline``; a changed
	fingerprint is ``compatibility.drift``.
	"""
	if previous_fingerprint == result.fingerprint:
		return None
	event = "compatibility.baseline" if not previous_fingerprint else "compatibility.drift"
	return {
		"event": event,
		"previous_fingerprint": previous_fingerprint,
		"fingerprint": result.fingerprint,
		"status": result.status,
		"critical": list(result.critical),
		"warnings": list(result.warnings),
	}
