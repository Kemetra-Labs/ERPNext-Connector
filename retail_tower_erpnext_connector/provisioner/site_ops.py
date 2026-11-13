# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Site-side provisioning operations, run INSIDE the site process by the registered bench command
``rt-provision-site-step`` (see ``commands.py``). The orchestrator never imports this module.

Every operation is idempotent (check, then apply) and uses ordinary Frappe mechanisms only:
``setup_complete`` for the base, document inserts (``get_doc().insert()``) so ERPNext's own validations
run, Connector Settings through the document API, and the site config for markers. No direct SQL
into ERPNext tables, no hook, no override. BENCH-VALIDATION: ``tests/test_provisioner_site_ops_db.py``
runs the template on a real site in the nightly bench lane.

Validation note: perpetual inventory and the FIFO setting are written with ``db.set_value`` /
``set_single_value``, which skip Company validation (safe on a brand-new Company, and the operation
is a no-op once they hold the template value); the Mode of Payment, the Customer and the Warehouses
go through document inserts, so ERPNext's own validations run for them.

The credential secret arrives in the spec read from stdin, is assigned to the Connector Settings
password field and is never logged, echoed or written to a file.
"""

from __future__ import annotations

import secrets
from typing import Any

from .template import TemplateSpec, spec_for

_SETTINGS = "Connector Settings"


class OpError(Exception):
	"""A site-side failure with a contract error class; printed as a structured result line."""

	def __init__(self, error_class: str, reason: str) -> None:
		self.error_class = error_class
		self.reason = reason
		super().__init__(reason)


def run(spec: dict) -> dict:
	handlers = {
		"inspect": lambda a, s: inspect(),
		"reset_admin_password": lambda a, s: reset_admin_password(),
		"setup_wizard": lambda a, s: setup_wizard(a),
		"apply_template": lambda a, s: apply_template(a),
		"enable_scheduler": lambda a, s: enable_scheduler(),
		"bootstrap_stores": lambda a, s: bootstrap_stores(a),
		"bind_credential": lambda a, s: bind_credential(a, s),
	}
	handler = handlers.get(spec.get("op"))
	if handler is None:
		raise OpError("permanently_rejected", "unknown site operation")
	return handler(spec.get("args") or {}, spec.get("secret"))


# --- reads ---------------------------------------------------------------------------------------


def _template_state(site_config: dict) -> dict | None:
	raw = site_config.get("rt_template_applied")
	if not raw or "@" not in raw:
		return None
	template_id, _, version = raw.rpartition("@")
	return {"id": template_id, "version": int(version)} if version.isdigit() else None


def _store_maps() -> dict:
	import frappe

	settings = frappe.get_single(_SETTINGS)
	warehouses = {r.store_id: r.warehouse for r in settings.warehouse_map or []}
	customers = {r.store_id: r.customer for r in settings.store_customer_map or []}
	return {
		store: {"warehouse": warehouses[store], "customer": customers[store]}
		for store in warehouses
		if store in customers
	}


def inspect() -> dict:
	import frappe
	from frappe.utils import cint
	from frappe.utils.scheduler import is_scheduler_disabled

	apps = frappe.get_installed_apps()
	out: dict[str, Any] = {
		"apps": apps,
		"setup_complete": bool(cint(frappe.db.get_single_value("System Settings", "setup_complete"))),
		"companies": [],
		"default_company": None,
		"template": _template_state(frappe.get_site_config()),
		"scheduler_enabled": not is_scheduler_disabled(verbose=False),
		"credential_id": None,
		"indexes_ok": False,
		"stores": {},
		"versions": _versions(apps),
	}
	if "erpnext" in apps:
		out["companies"] = frappe.get_all("Company", pluck="name")
		out["default_company"] = frappe.db.get_single_value("Global Defaults", "default_company")
	if "retail_tower_erpnext_connector" in apps:
		from ..connector import schema

		out["credential_id"] = frappe.db.get_single_value(_SETTINGS, "dp2_credential_id") or None
		out["registration_id"] = (
			frappe.db.get_single_value(_SETTINGS, "dp2_connector_registration_id") or None
		)
		out["indexes_ok"] = schema.index_present(
			"Posting Log", schema.POSTING_LOG_INDEX, schema.POSTING_LOG_COLUMNS
		) and schema.index_present("Sales Invoice", schema.SALES_INVOICE_INDEX, schema.SALES_INVOICE_COLUMNS)
		out["stores"] = _store_maps()
		out["posting_currency"] = frappe.db.get_single_value(_SETTINGS, "posting_currency") or None
		out["tender_methods"] = [r.tender_method for r in frappe.get_single(_SETTINGS).tender_mode_map or []]
	return out


def _versions(apps: list[str]) -> dict:
	import frappe

	out = {"frappe": frappe.__version__}
	if "erpnext" in apps:
		import erpnext

		out["erpnext"] = erpnext.__version__
	if "retail_tower_erpnext_connector" in apps:
		import retail_tower_erpnext_connector as connector

		out["connector"] = getattr(connector, "__version__", None)
	return out


# --- writes --------------------------------------------------------------------------------------


def reset_admin_password() -> dict:
	"""Replace the one-use admin password given to ``new-site`` with a value that is never stored
	(decision OD-9: the Site Administrator password is not stored; an operator resets it on demand)."""
	from frappe.utils.password import update_password

	update_password("Administrator", secrets.token_urlsafe(32), logout_all_sessions=True)
	_commit()
	return {"ok": True}


def _language_name(code: str) -> str:
	import frappe

	name = frappe.db.get_value("Language", code, "language_name")
	if not name:
		raise OpError("permanently_rejected", "language is not available on this site")
	return name


def setup_wizard(args: dict) -> dict:
	import frappe
	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
	from frappe.utils import cint, now_datetime

	if cint(frappe.db.get_single_value("System Settings", "setup_complete")):
		return {"ok": True, "already": True}
	year = str(now_datetime().year)  # calendar fiscal year (OD-4)
	setup_complete(
		{
			"language": _language_name(args["language"]),
			"country": args["country"],
			"timezone": args["timezone"],
			"currency": args["currency"],
			"company_name": args["company_name"],
			"company_abbr": args["abbr"],
			"chart_of_accounts": args["chart_of_accounts"],
			"fy_start_date": f"{year}-01-01",
			"fy_end_date": f"{year}-12-31",
		}
	)
	_commit()
	return {"ok": True}


def _commit() -> None:
	"""Commit, unless the caller runs the operation inside its own transaction (the bench-lane test
	sets ``frappe.flags.rt_provisioner_no_commit`` and rolls everything back, so it leaves no state)."""
	import frappe

	if not frappe.flags.get("rt_provisioner_no_commit"):
		frappe.db.commit()


def _one_company(args: dict | None = None) -> str:
	"""The site's single Company. ``args["company"]`` names it explicitly (the test runs on a site
	whose own Company it must not touch); it must exist. The engine never passes it."""
	import frappe

	named = (args or {}).get("company")
	if named:
		if not frappe.db.exists("Company", named):
			raise OpError("conflict", "named company does not exist")
		return named
	companies = frappe.get_all("Company", pluck="name")
	if len(companies) != 1:
		raise OpError("conflict", "expected exactly one company on the site")
	return companies[0]


def _ensure_cash_mode(spec: TemplateSpec, company: str) -> None:
	import frappe

	cash = frappe.db.get_value("Company", company, "default_cash_account") or frappe.db.get_value(
		"Account", {"company": company, "account_type": "Cash", "is_group": 0}
	)
	if not frappe.db.exists("Mode of Payment", spec.cash_mode_of_payment):
		frappe.get_doc(
			{"doctype": "Mode of Payment", "mode_of_payment": spec.cash_mode_of_payment, "type": "Cash"}
		).insert()
	mode = frappe.get_doc("Mode of Payment", spec.cash_mode_of_payment)
	if not any(row.company == company for row in mode.accounts):
		mode.append("accounts", {"company": company, "default_account": cash})
		mode.save()


def _ensure_walk_in(spec: TemplateSpec) -> None:
	import frappe

	if frappe.db.exists("Customer", {"customer_name": spec.walk_in_customer}):
		return
	frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": spec.walk_in_customer,
			"customer_type": "Individual",
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}),
			"territory": frappe.db.get_value("Territory", {"is_group": 0}),
		}
	).insert()


def apply_template(args: dict) -> dict:
	"""Template v1 get-or-create. Re-running it changes nothing that already matches."""
	import frappe

	spec = spec_for(args["template_id"], int(args["template_version"]))
	if spec is None:
		raise OpError("permanently_rejected", "unknown template version")
	company = _one_company(args)
	if spec.perpetual_inventory and not frappe.db.get_value("Company", company, "enable_perpetual_inventory"):
		frappe.db.set_value("Company", company, "enable_perpetual_inventory", 1)
	if frappe.db.get_single_value("Stock Settings", "valuation_method") != spec.valuation_method:
		frappe.db.set_single_value("Stock Settings", "valuation_method", spec.valuation_method)
	_ensure_cash_mode(spec, company)
	_ensure_walk_in(spec)
	_commit()
	if not frappe.flags.get("rt_provisioner_no_commit"):
		record_template(spec)  # the check reads this; written last
	return {"ok": True, "company": company}


def record_template(spec: TemplateSpec) -> None:
	"""Record the applied template in the site config (a file, not part of any transaction)."""
	from frappe.installer import update_site_config

	update_site_config("rt_template_applied", spec.key)


def enable_scheduler() -> dict:
	from frappe.utils.scheduler import enable_scheduler as enable

	enable()
	_commit()
	return {"ok": True}


def _walk_in_customer(customer_name: str) -> str:
	import frappe

	name = frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
	if not name:
		raise OpError("failed_transient", "walk-in customer missing: the template is not applied")
	return name


def _warehouse_candidates(store: dict) -> list[str]:
	"""The store's display name, then suffixed forms that get longer, ending with the full store id."""
	label, store_id = store["displayName"], store["storeId"]
	return [label, f"{label} ({store_id[:8]})", f"{label} ({store_id[:12]})", f"{label} ({store_id})"]


def _warehouse_for(store: dict, company: str, taken: dict[str, str]) -> str:
	"""Get-or-create a stock-holding warehouse for the store.

	A candidate is reused only if it is an enabled leaf warehouse not mapped to another store (a
	crashed earlier run may have created it); a group warehouse (ERPNext's own "All Warehouses"), a
	disabled one, or one owned by another store is skipped and the next, longer candidate is tried."""
	import frappe

	for label in _warehouse_candidates(store):
		rows = frappe.get_all(
			"Warehouse",
			filters={"warehouse_name": label, "company": company},
			fields=["name", "is_group", "disabled"],
			limit=1,
		)
		if not rows:
			return _insert_warehouse(label, company)
		row = rows[0]
		if row["is_group"] or row["disabled"] or taken.get(row["name"]) not in (None, store["storeId"]):
			continue
		return row["name"]
	raise OpError("conflict", "no free warehouse name for the store")


def _insert_warehouse(label: str, company: str) -> str:
	import frappe

	parent = frappe.db.get_value(
		"Warehouse", {"company": company, "is_group": 1, "parent_warehouse": ["is", "not set"]}
	)
	doc = frappe.get_doc(
		{"doctype": "Warehouse", "warehouse_name": label, "company": company, "parent_warehouse": parent}
	)
	doc.insert()
	return doc.name


def bootstrap_stores(args: dict) -> dict:
	"""One warehouse per store, the Connector Settings maps (warehouse, store customer), the cash
	tender, and the posting currency. Idempotent; it also runs with no stores (a tenant unlocked before
	its first store exists)."""
	import frappe

	company = _one_company(args)
	customer = _walk_in_customer(args["walk_in_customer"])
	settings = frappe.get_single(_SETTINGS)
	mapped = {r.store_id: r.warehouse for r in settings.warehouse_map or []}
	taken = {warehouse: store for store, warehouse in mapped.items()}
	customers = {r.store_id for r in settings.store_customer_map or []}
	if not settings.posting_currency:
		# Posting stays paused (a CRITICAL compatibility finding) while this is blank; it is the
		# currency the Company was created in, which the profile compares it with.
		settings.posting_currency = frappe.db.get_value("Company", company, "default_currency")
	cash_mode = args.get("cash_mode_of_payment")
	if cash_mode and not any(r.tender_method == "cash" for r in settings.tender_mode_map or []):
		# OD-4: Cash is the only launch tender. Get-or-create: an operator's row is never replaced.
		settings.append("tender_mode_map", {"tender_method": "cash", "mode_of_payment": cash_mode})
	for store in args["stores"]:
		if store["storeId"] not in mapped:
			warehouse = _warehouse_for(store, company, taken)
			settings.append("warehouse_map", {"store_id": store["storeId"], "warehouse": warehouse})
			taken[warehouse] = store["storeId"]
		if store["storeId"] not in customers:
			settings.append("store_customer_map", {"store_id": store["storeId"], "customer": customer})
	settings.save()
	_commit()
	return {"ok": True}


def _site_naive(iso: str):
	from frappe.utils import convert_utc_to_system_timezone, get_datetime

	return convert_utc_to_system_timezone(get_datetime(iso.replace("Z", "+00:00"))).replace(tzinfo=None)


def bind_credential(args: dict, secret: str | None) -> dict:
	"""Write the issued credential into Connector Settings, in-process. The secret is assigned to the
	Password field (encrypted on save) and goes nowhere else."""
	import frappe

	if not secret:
		raise OpError("permanently_rejected", "no credential supplied")
	settings = frappe.get_single(_SETTINGS)
	settings.dp2_base_url = args["base_url"]
	settings.dp2_token = secret
	settings.dp2_connector_registration_id = args["registration_id"]
	settings.dp2_credential_id = args["credential_id"]
	settings.dp2_credential_issued_at = _site_naive(args["issued_at"])
	settings.dp2_credential_expires_at = _site_naive(args["expires_at"])
	settings.save()
	_commit()
	return {"ok": True}


# --- bench-level (no site) ------------------------------------------------------------------------


def queue_depth() -> dict:
	"""The deepest background-job queue of this bench (Frappe refuses new jobs at its own ceiling)."""
	import frappe
	from frappe.utils.background_jobs import get_queues

	with frappe.init_site():
		return {"depth": max((int(q.count) for q in get_queues()), default=0)}
