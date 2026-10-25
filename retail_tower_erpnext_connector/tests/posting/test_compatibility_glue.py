# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt
"""RT-379 — the Compatibility Profile glue against a frappe stand-in (no bench).

* ``read_snapshot`` reads versions, custom fields, currency precision, company, the mapped records
  and the tax sources that would auto-apply, without writing anything to ERPNext (668).
* ``run_preflight`` evaluates the profile, records the status on Connector Settings (the only write,
  without bumping ``modified``) and logs critical / drift events at ERROR; it returns whether
  posting may proceed. An unreadable snapshot is critical (never assume the site is fine).
"""

import importlib
import sys
import types

import pytest

_GLUE = "retail_tower_erpnext_connector.connector.posting.compatibility_glue"
_PKG = "retail_tower_erpnext_connector.connector.posting"
_MISSING = object()
_CUSTOM_FIELDS = [
	"Sales Invoice-rt_source_system",
	"Sales Invoice-rt_external_id",
	"Sales Invoice-rt_sale_ref",
	"Sales Invoice Item-rt_line_ref",
]


class _Logger:
	def __init__(self):
		self.records = []

	def error(self, record):
		self.records.append(("error", record))

	def info(self, record):
		self.records.append(("info", record))

	warning = info


def _site():
	"""A compatible v15 site; a test edits the dicts before loading the glue."""
	return {
		"singles": {
			("Global Defaults", "default_company"): "RT Co",
			("Selling Settings", "validate_selling_price"): 0,
			("Accounts Settings", "add_taxes_from_item_tax_template"): 1,
			("Accounts Settings", "add_taxes_from_taxes_and_charges_template"): 0,
		},
		"records": {
			("Company", "RT Co"): {"default_currency": "EGP"},
			("UOM", "Nos"): {"enabled": 1},
			("Warehouse", "Stores - RT"): {"disabled": 0, "company": "RT Co"},
			("Customer", "Walk-in"): {"disabled": 0, "customer_group": "All", "tax_category": None},
			("Mode of Payment", "Cash"): {"enabled": 1},
			("Sales Taxes and Charges Template", "VAT 14%"): {"disabled": 0},
			("Sales Taxes and Charges Template", "Empty Tax"): {"disabled": 0},
		},
		"lists": {
			"Custom Field": [{"name": n} for n in _CUSTOM_FIELDS],
			"Sales Taxes and Charges Template": [],
			"Item Tax Template": [],
			"Item Tax": [],
			"Tax Rule": [],
			"POS Profile": [],
			"POS Profile User": [],
			# Template tax rows: "VAT 14%" has one; an empty template adds no tax row.
			"Sales Taxes and Charges": [
				{"parent": "VAT 14%", "parenttype": "Sales Taxes and Charges Template"}
			],
			"Mode of Payment Account": [
				{"parent": "Cash", "company": "RT Co", "default_account": "Cash - RT"}
			],
		},
		"pricing_rules": 0,
		# ERPNext's own Tax Rule evaluation (party.set_taxes): customer -> (template, required category).
		"tax_rule_templates": {},
		# Address tax categories and each customer's default (billing, shipping) addresses.
		"address_categories": {},
		# Each customer's Address rows (name, address_type, is_primary_address, is_shipping_address).
		"addresses": {},
		# Accounts Settings fields this ERPNext v15 minor has (the template flag only from 15.67).
		"accounts_fields": {"add_taxes_from_item_tax_template", "add_taxes_from_taxes_and_charges_template"},
		"precision": 2,
	}


class _Frappe(types.ModuleType):
	def __init__(self, site, *, unreadable=False):
		super().__init__("frappe")
		self.__version__ = "16.52.0"
		# A v16 site configured per the policy (scheduler_tick_interval: 60, RT-494).
		self.conf = {"scheduler_tick_interval": 60}
		self._site, self.unreadable = site, unreadable
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self.writes, self.set_taxes_calls, self.address_calls = [], [], 0
		self.session = types.SimpleNamespace(user="Administrator")
		self.get_roles = lambda user=None: list(site.get("roles", ["System Manager", "Accounts Manager"]))
		self.db = types.SimpleNamespace(
			get_single_value=self._get_single_value,
			get_value=self._get_value,
			count=lambda doctype, filters=None: site["pricing_rules"] if doctype == "Pricing Rule" else 0,
			set_single_value=self._set_single_value,
			set_value=lambda *a, **k: pytest.fail("the profile must never write to ERPNext (668)"),
		)
		self.utils = types.SimpleNamespace(
			now_datetime=lambda: "2026-10-11 20:00:00", nowdate=lambda: "2026-10-11", cint=int
		)

	def _get_single_value(self, doctype, field):
		if self.unreadable:
			raise TimeoutError("Lock wait timeout exceeded")
		return self._site["singles"].get((doctype, field))

	def _get_value(self, doctype, name, fields, as_dict=False):
		row = self._site["records"].get((doctype, name))
		if row is None:
			return None
		if isinstance(fields, str):
			return row.get(fields)
		return {f: row.get(f) for f in fields}

	def get_all(self, doctype, **kwargs):
		rows = [dict(r) for r in self._site["lists"].get(doctype, [])]
		for key, value in (kwargs.get("filters") or {}).items():
			rows = [r for r in rows if _matches(r.get(key), value)]
		pluck = kwargs.get("pluck")
		return [r[pluck] for r in rows] if pluck else rows

	def get_meta(self, doctype):
		fields = self._site["accounts_fields"] if doctype == "Accounts Settings" else None
		return types.SimpleNamespace(
			get_field=lambda name: types.SimpleNamespace(fieldname=name),
			has_field=lambda name: fields is None or name in fields,
		)

	def _set_single_value(self, doctype, values, **kwargs):
		self.writes.append((doctype, dict(values), kwargs.get("update_modified", True)))

	def get_doc(self, *args, **kwargs):
		pytest.fail("the profile must never load or save a document (668)")


def _matches(actual, wanted):
	"""The frappe filter forms the glue uses: a value, ``["in", [...]]`` and ``["is", "set"]``."""
	if not isinstance(wanted, list):
		return actual == wanted
	operator, operand = wanted
	return bool(actual) if operator == "is" else actual in operand


class _Settings:
	def __init__(self, **values):
		self._values = {"posting_currency": "EGP", "compatibility_fingerprint": None, **values}

	def get(self, field):
		return self._values.get(field)


_MAPS = {
	"uom": {"each": "Nos"},
	"warehouse": {"store-1": {"doctype": "Warehouse", "name": "Stores - RT"}},
	"customer": {"store-1": "Walk-in"},
	"tender": {"cash": "Cash"},
}


@pytest.fixture
def load_glue():
	pkg = importlib.import_module(_PKG)
	names = (
		"frappe",
		"frappe.model",
		"frappe.model.meta",
		"frappe.contacts",
		"frappe.contacts.doctype",
		"frappe.contacts.doctype.address",
		"frappe.contacts.doctype.address.address",
		"erpnext",
		"erpnext.accounts",
		"erpnext.accounts.party",
		_GLUE,
	)
	saved = [(sys.modules, n, sys.modules.get(n, _MISSING)) for n in names]
	saved.append((vars(pkg), "compatibility_glue", vars(pkg).get("compatibility_glue", _MISSING)))

	def _load(fake):
		meta = types.ModuleType("frappe.model.meta")
		meta.get_field_precision = lambda df, doc=None, currency=None: fake._site["precision"]
		party = types.ModuleType("erpnext.accounts.party")
		party.set_taxes = lambda *args, **kwargs: _set_taxes(fake, *args, **kwargs)
		party.get_address_tax_category = lambda *args: _address_tax_category(fake, *args)
		party.get_party_shipping_address = lambda doctype, name: _party_shipping_address(fake, name)
		address = types.ModuleType("frappe.contacts.doctype.address.address")
		address.get_default_address = lambda doctype, name, sort_key="is_primary_address": _default_address(
			fake, name, sort_key
		)
		sys.modules.update(
			{
				"frappe": fake,
				"frappe.model": types.ModuleType("frappe.model"),
				"frappe.model.meta": meta,
				"frappe.contacts": types.ModuleType("frappe.contacts"),
				"frappe.contacts.doctype": types.ModuleType("frappe.contacts.doctype"),
				"frappe.contacts.doctype.address": types.ModuleType("frappe.contacts.doctype.address"),
				"frappe.contacts.doctype.address.address": address,
				"erpnext": types.SimpleNamespace(__version__="16.51.0"),
				"erpnext.accounts": types.ModuleType("erpnext.accounts"),
				"erpnext.accounts.party": party,
			}
		)
		sys.modules.pop(_GLUE, None)
		return importlib.import_module(_GLUE)

	yield _load
	for namespace, name, value in saved:
		namespace.pop(name, None)
		if value is not _MISSING:
			namespace[name] = value


def _address_tax_category(fake, tax_category=None, billing_address=None, shipping_address=None):
	"""ERPNext v15 get_address_tax_category: the address chosen by Accounts Settings wins."""
	fake.address_calls += 1
	mode = fake._site["singles"].get(("Accounts Settings", "determine_address_tax_category_from"))
	chosen = shipping_address if mode == "Shipping Address" else billing_address
	return fake._site["address_categories"].get(chosen) or tax_category or ""


def _default_address(fake, customer, sort_key):
	"""Frappe v15 get_default_address: the third argument is only a SORT key (any address may come back)."""
	fake.address_calls += 1
	rows = sorted(fake._site["addresses"].get(customer, []), key=lambda r: r.get(sort_key, 0), reverse=True)
	return rows[0]["name"] if rows else None


def _party_shipping_address(fake, customer):
	"""ERPNext v15 get_party_shipping_address: address_type Shipping or is_shipping_address, else ""."""
	fake.address_calls += 1
	rows = [
		r
		for r in fake._site["addresses"].get(customer, [])
		if r.get("address_type") == "Shipping" or r.get("is_shipping_address")
	]
	rows.sort(key=lambda r: r.get("is_shipping_address", 0), reverse=True)
	return rows[0]["name"] if rows else ""


def _set_taxes(fake, *args, **kwargs):
	"""ERPNext's party.set_taxes stand-in: records the call; a rule may require a tax category.

	Positional args are ``(party, party_type, posting_date, company)``, as the glue passes them.
	"""
	customer, party_type, posting_date, company = args
	fake.set_taxes_calls.append({"customer": customer, "posting_date": posting_date, **kwargs})
	assert (party_type, company) == ("Customer", "RT Co")
	template, category = _rule(fake, customer)
	return template if category in (None, kwargs.get("tax_category")) else None


def _rule(fake, customer):
	rule = fake._site["tax_rule_templates"].get(customer)
	return rule if isinstance(rule, tuple) else (rule, None)


def _events(fake):
	return [record["event"] for level, record in fake._logger.records if level == "error"]


class TestReadSnapshot:
	def test_a_compatible_site_reads_as_compatible(self, load_glue):
		fake = _Frappe(_site())
		glue = load_glue(fake)
		snapshot = glue.read_snapshot(_Settings(), _MAPS)
		assert snapshot.frappe_version == "16.52.0"
		assert snapshot.erpnext_version == "16.51.0"
		assert snapshot.custom_fields == frozenset(_CUSTOM_FIELDS)
		assert (snapshot.default_company, snapshot.company_currency) == ("RT Co", "EGP")
		assert snapshot.currency_precision == 2 and snapshot.tax_sources == ()
		assert {(r.doctype, r.name, r.exists, r.disabled) for r in snapshot.mapped} == {
			("UOM", "Nos", True, False),
			("Warehouse", "Stores - RT", True, False),
			("Customer", "Walk-in", True, False),
			("Mode of Payment", "Cash", True, False),
		}

	def test_a_disabled_uom_and_a_missing_customer_are_read_as_such(self, load_glue):
		site = _site()
		site["records"][("UOM", "Nos")] = {"enabled": 0}
		del site["records"][("Customer", "Walk-in")]
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		by_type = {r.doctype: r for r in snapshot.mapped}
		assert by_type["UOM"].disabled and not by_type["Customer"].exists

	def test_the_warehouse_company_is_read(self, load_glue):
		site = _site()
		site["records"][("Warehouse", "Stores - RT")]["company"] = "Other Co"
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		(warehouse,) = [r for r in snapshot.mapped if r.doctype == "Warehouse"]
		assert warehouse.company == "Other Co"


class TestTaxSources:
	"""D4, per ERPNext v15 source: what can add a tax row on a server-side insert."""

	def test_a_default_sales_taxes_template_is_read_as_a_warning_source(self, load_glue):
		# Review finding 6: not an auto-applying source in v15; reported for visibility only.
		site = _site()
		site["lists"]["Sales Taxes and Charges Template"] = [
			{"name": "VAT 14%", "is_default": 1, "disabled": 0, "company": "RT Co"}
		]
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		assert snapshot.default_tax_templates == ("VAT 14%",) and snapshot.tax_sources == ()

	def test_an_assigned_item_tax_template_is_reported_while_auto_add_is_on(self, load_glue):
		site = _site()
		site["lists"]["Item Tax Template"] = [{"name": "Item VAT", "disabled": 0, "company": "RT Co"}]
		site["lists"]["Item Tax"] = [{"item_tax_template": "Item VAT", "parenttype": "Item"}]
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		assert any("Item VAT" in s for s in snapshot.tax_sources)

	def test_an_item_tax_template_is_not_reported_when_auto_add_is_off(self, load_glue):
		site = _site()
		site["singles"][("Accounts Settings", "add_taxes_from_item_tax_template")] = 0
		site["lists"]["Item Tax Template"] = [{"name": "Item VAT", "disabled": 0, "company": "RT Co"}]
		site["lists"]["Item Tax"] = [{"item_tax_template": "Item VAT", "parenttype": "Item"}]
		assert load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS).tax_sources == ()

	def test_a_tax_rule_that_applies_to_a_mapped_customer_is_reported(self, load_glue):
		# Codex PR #67: ERPNext's own get_tax_template (via party.set_taxes) decides applicability.
		site = _site()
		site["singles"][("Accounts Settings", "add_taxes_from_taxes_and_charges_template")] = 1
		site["tax_rule_templates"] = {"Walk-in": "VAT 14%"}
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		assert any("VAT 14%" in s and "Walk-in" in s for s in snapshot.tax_sources)

	def test_a_tax_rule_that_applies_to_no_mapped_customer_is_not_reported(self, load_glue):
		site = _site()
		site["singles"][("Accounts Settings", "add_taxes_from_taxes_and_charges_template")] = 1
		site["tax_rule_templates"] = {"Some Other Customer": "VAT 14%"}  # restricted / expired elsewhere
		assert load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS).tax_sources == ()

	def test_a_tax_rule_is_not_reported_while_template_taxes_are_not_added(self, load_glue):
		site = _site()
		site["tax_rule_templates"] = {"Walk-in": "VAT 14%"}
		assert load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS).tax_sources == ()

	def test_a_pos_profile_with_taxes_is_reported(self, load_glue):
		site = _site()
		site["lists"]["POS Profile"] = [
			{"name": "Main POS", "disabled": 0, "company": "RT Co", "taxes_and_charges": "VAT 14%"}
		]
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		assert any("Main POS" in s for s in snapshot.tax_sources)


class TestRunPreflight:
	def test_a_compatible_site_may_post_and_records_ok_without_bumping_modified(self, load_glue):
		fake = _Frappe(_site())
		assert load_glue(fake).run_preflight(_Settings(), _MAPS) is True
		((doctype, values, update_modified),) = fake.writes
		assert doctype == "Connector Settings" and update_modified is False
		assert values["compatibility_status"] == "OK" and len(values["compatibility_fingerprint"]) == 64
		assert "compatibility_checked_at" in values and values["compatibility_report"].startswith("OK")

	def test_the_first_evaluation_logs_a_baseline(self, load_glue):
		fake = _Frappe(_site())
		load_glue(fake).run_preflight(_Settings(), _MAPS)
		assert _events(fake) == ["compatibility.baseline"]

	def test_an_unchanged_site_logs_nothing(self, load_glue):
		fake = _Frappe(_site())
		glue = load_glue(fake)
		glue.run_preflight(_Settings(), _MAPS)
		fingerprint = fake.writes[0][1]["compatibility_fingerprint"]
		fake._logger.records.clear()
		assert glue.run_preflight(_Settings(compatibility_fingerprint=fingerprint), _MAPS) is True
		assert _events(fake) == []

	def test_a_changed_site_logs_drift(self, load_glue):
		fake = _Frappe(_site())
		assert load_glue(fake).run_preflight(_Settings(compatibility_fingerprint="0" * 64), _MAPS) is True
		assert _events(fake) == ["compatibility.drift"]

	def test_a_critical_site_pauses_posting_and_logs_it_every_tick(self, load_glue):
		site = _site()
		site["singles"][("Global Defaults", "default_company")] = ""
		fake = _Frappe(site)
		assert load_glue(fake).run_preflight(_Settings(), _MAPS) is False
		assert "compatibility.critical" in _events(fake)
		assert fake.writes[0][1]["compatibility_status"] == "CRITICAL"

	def test_warnings_do_not_pause_posting(self, load_glue):
		site = _site()
		site["pricing_rules"] = 2
		fake = _Frappe(site)
		assert load_glue(fake).run_preflight(_Settings(), _MAPS) is True
		assert fake.writes[0][1]["compatibility_status"] == "WARNING"

	def test_an_unreadable_site_pauses_posting(self, load_glue):
		fake = _Frappe(_site(), unreadable=True)
		assert load_glue(fake).run_preflight(_Settings(), _MAPS) is False
		assert "compatibility.unreadable" in _events(fake)

	def test_a_failed_status_write_is_logged_and_does_not_change_the_decision(self, load_glue):
		fake = _Frappe(_site())

		def _fail(*args, **kwargs):
			raise TimeoutError("Lock wait timeout exceeded")

		fake.db.set_single_value = _fail
		assert load_glue(fake).run_preflight(_Settings(), _MAPS) is True
		assert "compatibility.record_failed" in _events(fake)


class TestCodexFindings:
	def test_a_mode_of_payment_without_an_account_for_the_company_is_read_as_such(self, load_glue):
		site = _site()
		site["lists"]["Mode of Payment Account"] = [
			{"parent": "Cash", "company": "Other Co", "default_account": "Cash - OC"}
		]
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		(mop,) = [r for r in snapshot.mapped if r.doctype == "Mode of Payment"]
		assert mop.has_company_account is False

	def test_a_mode_of_payment_with_an_account_for_the_company_is_read_as_such(self, load_glue):
		snapshot = load_glue(_Frappe(_site())).read_snapshot(_Settings(), _MAPS)
		(mop,) = [r for r in snapshot.mapped if r.doctype == "Mode of Payment"]
		assert mop.has_company_account is True

	def test_an_unreadable_site_records_a_critical_status_and_keeps_the_fingerprint(self, load_glue):
		fake = _Frappe(_site(), unreadable=True)
		assert load_glue(fake).run_preflight(_Settings(compatibility_fingerprint="f" * 64), _MAPS) is False
		((doctype, values, update_modified),) = fake.writes
		assert doctype == "Connector Settings" and update_modified is False
		assert values["compatibility_status"] == "CRITICAL"
		assert (
			values["compatibility_report"].startswith("CRITICAL") and "read" in values["compatibility_report"]
		)
		assert "compatibility_fingerprint" not in values  # recovery is then compared with the last real check


class TestDeepReviewRound:
	def _flag_on(self, site):
		site["singles"][("Accounts Settings", "add_taxes_from_taxes_and_charges_template")] = 1
		return site

	def test_the_tax_category_comes_from_the_default_billing_address_as_erpnext_derives_it(self, load_glue):
		# Finding 1: customer without a category, billing address "VAT Registered", a matching rule.
		site = self._flag_on(_site())
		site["addresses"] = {
			"Walk-in": [
				{
					"name": "Walk-in-Billing",
					"address_type": "Billing",
					"is_primary_address": 1,
					"is_shipping_address": 0,
				}
			]
		}
		site["address_categories"] = {"Walk-in-Billing": "VAT Registered"}
		site["tax_rule_templates"] = {"Walk-in": ("VAT 14%", "VAT Registered")}
		fake = _Frappe(site)
		snapshot = load_glue(fake).read_snapshot(_Settings(), _MAPS)
		assert any("VAT 14%" in s for s in snapshot.tax_sources)
		(call,) = fake.set_taxes_calls
		assert call["tax_category"] == "VAT Registered" and call["customer_group"] == "All"
		result = load_glue(fake).run_preflight(_Settings(), _MAPS)
		assert result is False

	def test_without_the_address_category_the_restricted_rule_does_not_apply(self, load_glue):
		site = self._flag_on(_site())
		site["tax_rule_templates"] = {"Walk-in": ("VAT 14%", "VAT Registered")}
		fake = _Frappe(site)
		assert load_glue(fake).read_snapshot(_Settings(), _MAPS).tax_sources == ()
		assert fake.set_taxes_calls[0]["tax_category"] == ""

	def test_the_scheduler_tick_is_read_from_the_site_config(self, load_glue):
		# RT-494: frappe.conf merges common_site_config, where `bench set-config -g` writes the tick.
		fake = _Frappe(_site())
		fake.conf = {"scheduler_tick_interval": 60}
		assert load_glue(fake).read_snapshot(_Settings(), _MAPS).scheduler_tick_s == 60

	@pytest.mark.parametrize(
		("raw", "expected"),
		[("60", 60), ("60.0", 60), (90.0, 90), ("60s", None), ([60], None), (0, None), (-5, None),
		 ("inf", None), ("-Infinity", None), (float("inf"), None), ("nan", None)],
	)
	def test_a_malformed_scheduler_tick_reads_as_none_and_never_pauses_posting(self, load_glue, raw, expected):
		# Gitar review on PR #76: the tick only feeds a warning, so a value Frappe's cint() reads as 0 must
		# not make the site "unreadable" (which pauses posting). It reads as None, i.e. the v16 default.
		fake = _Frappe(_site())
		fake.conf = {"scheduler_tick_interval": raw}
		glue = load_glue(fake)
		assert glue.read_snapshot(_Settings(), _MAPS).scheduler_tick_s == expected
		assert glue.run_preflight(_Settings(), _MAPS) is True
		assert "compatibility.unreadable" not in _events(fake)

	def test_an_unset_scheduler_tick_reads_as_none(self, load_glue):
		fake = _Frappe(_site())
		fake.conf = {}
		assert load_glue(fake).read_snapshot(_Settings(), _MAPS).scheduler_tick_s is None

	def test_an_erpnext_before_15_67_is_critical_with_an_honest_message(self, load_glue):
		# Finding 2: the template flag does not exist yet; get_single_value would throw.
		site = _site()
		site["accounts_fields"] = {"add_taxes_from_item_tax_template"}
		del site["singles"][("Accounts Settings", "add_taxes_from_taxes_and_charges_template")]
		fake = _Frappe(site)
		glue = load_glue(fake)
		snapshot = glue.read_snapshot(_Settings(), _MAPS)
		assert any("15.67" in u for u in snapshot.unsupported)
		assert glue.run_preflight(_Settings(), _MAPS) is False
		assert "compatibility.unreadable" not in _events(fake)

	def test_a_disabled_pos_profile_that_is_the_users_default_is_reported(self, load_glue):
		# Finding 4: get_pos_profile's user-default query has no disabled filter.
		site = _site()
		site["lists"]["POS Profile"] = [
			{"name": "Old POS", "disabled": 1, "company": "RT Co", "taxes_and_charges": "VAT 14%"}
		]
		site["lists"]["POS Profile User"] = [{"parent": "Old POS", "user": "Administrator", "default": 1}]
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		assert any("Old POS" in s for s in snapshot.tax_sources)

	def test_a_disabled_pos_profile_that_is_nobodys_default_is_not_reported(self, load_glue):
		site = _site()
		site["lists"]["POS Profile"] = [
			{"name": "Old POS", "disabled": 1, "company": "RT Co", "taxes_and_charges": "VAT 14%"}
		]
		assert load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS).tax_sources == ()

	def test_an_evaluation_error_records_critical_and_never_pulls(self, load_glue, monkeypatch):
		# Trivial 7: never a stale OK.
		fake = _Frappe(_site())
		glue = load_glue(fake)

		def _boom(snapshot):
			raise RuntimeError("profile bug")

		monkeypatch.setattr(glue, "evaluate", _boom)
		assert glue.run_preflight(_Settings(), _MAPS) is False
		assert fake.writes[-1][1]["compatibility_status"] == "CRITICAL"
		assert "compatibility.failed" in _events(fake)


class TestGroupWarehouseRead:
	def test_a_group_warehouse_is_read_as_such(self, load_glue):
		site = _site()
		site["records"][("Warehouse", "Stores - RT")]["is_group"] = 1
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		(warehouse,) = [r for r in snapshot.mapped if r.doctype == "Warehouse"]
		assert warehouse.is_group is True


class TestItemTaxPredicates:
	"""Codex PR #67: v15 _get_item_tax_template selects a row only for the invoice's derived tax
	category, and only once valid_from has passed. Evaluated for the mapped customers, as of today."""

	def _site(self, **row):
		site = _site()
		site["lists"]["Item Tax Template"] = [{"name": "Item VAT", "disabled": 0, "company": "RT Co"}]
		site["lists"]["Item Tax"] = [{"item_tax_template": "Item VAT", "parenttype": "Item", **row}]
		return site

	def test_a_row_for_a_category_no_mapped_customer_has_is_not_reported(self, load_glue):
		site = self._site(tax_category="Export")
		assert load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS).tax_sources == ()

	def test_a_row_for_the_customers_derived_category_is_reported(self, load_glue):
		site = self._site(tax_category="VAT Registered")
		site["addresses"] = {
			"Walk-in": [
				{
					"name": "Walk-in-Billing",
					"address_type": "Billing",
					"is_primary_address": 1,
					"is_shipping_address": 0,
				}
			]
		}
		site["address_categories"] = {"Walk-in-Billing": "VAT Registered"}
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		assert any("Item VAT" in s for s in snapshot.tax_sources)

	def test_a_row_not_yet_valid_is_not_reported(self, load_glue):
		site = self._site(valid_from="2027-01-01")
		assert load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS).tax_sources == ()

	def test_a_row_already_valid_is_reported(self, load_glue):
		site = self._site(valid_from="2026-01-01")
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		assert any("Item VAT" in s for s in snapshot.tax_sources)


class TestFrozenCustomer:
	"""Codex PR #67: v15 validate_party_frozen_disabled throws for a frozen Customer unless the session
	user has the frozen_accounts_modifier role (always, when that role is unset)."""

	def _site(self, modifier, roles):
		site = _site()
		site["records"][("Customer", "Walk-in")]["is_frozen"] = 1
		site["singles"][("Accounts Settings", "frozen_accounts_modifier")] = modifier
		site["roles"] = roles
		return site

	def test_a_frozen_customer_without_the_modifier_role_is_read_as_blocked(self, load_glue):
		snapshot = load_glue(_Frappe(self._site("Accounts Manager", ["System Manager"]))).read_snapshot(
			_Settings(), _MAPS
		)
		(customer,) = [r for r in snapshot.mapped if r.doctype == "Customer"]
		assert customer.frozen_blocked is True

	def test_a_frozen_customer_with_no_modifier_role_configured_is_blocked(self, load_glue):
		snapshot = load_glue(_Frappe(self._site(None, ["System Manager"]))).read_snapshot(_Settings(), _MAPS)
		(customer,) = [r for r in snapshot.mapped if r.doctype == "Customer"]
		assert customer.frozen_blocked is True

	def test_a_frozen_customer_with_the_modifier_role_is_usable(self, load_glue):
		snapshot = load_glue(_Frappe(self._site("Accounts Manager", ["Accounts Manager"]))).read_snapshot(
			_Settings(), _MAPS
		)
		(customer,) = [r for r in snapshot.mapped if r.doctype == "Customer"]
		assert customer.frozen_blocked is False


class TestEmptyTemplates:
	"""Codex PR #67: an empty Sales Taxes and Charges Template adds no tax row; it must not pause."""

	def test_a_tax_rule_with_an_empty_template_is_not_reported(self, load_glue):
		site = _site()
		site["singles"][("Accounts Settings", "add_taxes_from_taxes_and_charges_template")] = 1
		site["tax_rule_templates"] = {"Walk-in": "Empty Tax"}
		assert load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS).tax_sources == ()

	def test_a_pos_profile_with_an_empty_template_is_not_reported(self, load_glue):
		site = _site()
		site["lists"]["POS Profile"] = [
			{"name": "Main POS", "disabled": 0, "company": "RT Co", "taxes_and_charges": "Empty Tax"}
		]
		assert load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS).tax_sources == ()


class TestShippingModeCategory:
	"""Re-check N1: in Shipping mode v15 uses get_party_shipping_address (address_type Shipping or
	is_shipping_address), not get_default_address's sort key, which can return a billing address."""

	def _site(self, addresses):
		site = _site()
		site["singles"][("Accounts Settings", "add_taxes_from_taxes_and_charges_template")] = 1
		site["singles"][("Accounts Settings", "determine_address_tax_category_from")] = "Shipping Address"
		site["records"][("Customer", "Walk-in")]["tax_category"] = "Retail"
		site["addresses"] = {"Walk-in": addresses}
		site["address_categories"] = {"Walk-in-Billing": "VAT Registered", "Walk-in-Ship": "Export"}
		return site

	def test_a_customer_with_only_a_billing_address_takes_the_customer_category(self, load_glue):
		fake = _Frappe(
			self._site([{"name": "Walk-in-Billing", "address_type": "Billing", "is_primary_address": 1}])
		)
		load_glue(fake).read_snapshot(_Settings(), _MAPS)
		assert fake.set_taxes_calls[0]["tax_category"] == "Retail"

	def test_a_lone_shipping_type_address_is_picked(self, load_glue):
		addresses = [{"name": "Walk-in-Ship", "address_type": "Shipping", "is_shipping_address": 0}]
		fake = _Frappe(self._site(addresses))
		load_glue(fake).read_snapshot(_Settings(), _MAPS)
		assert fake.set_taxes_calls[0]["tax_category"] == "Export"


class TestDisabledPosTemplate:
	def test_a_pos_profile_with_a_disabled_taxes_template_is_critical(self, load_glue):
		# Re-check trivial: v15 validate_enabled_taxes_and_charges throws "is disabled" on every paid sale.
		site = _site()
		site["lists"]["POS Profile"] = [
			{"name": "Main POS", "disabled": 0, "company": "RT Co", "taxes_and_charges": "Old Tax"}
		]
		site["records"][("Sales Taxes and Charges Template", "Old Tax")] = {"disabled": 1}
		fake = _Frappe(site)
		glue = load_glue(fake)
		snapshot = glue.read_snapshot(_Settings(), _MAPS)
		assert any("Old Tax" in b and "disabled" in b for b in snapshot.blockers)
		assert glue.run_preflight(_Settings(), _MAPS) is False


class TestLazyCustomerContexts:
	def test_no_address_lookups_when_no_tax_flag_needs_them(self, load_glue):
		site = _site()
		site["singles"][("Accounts Settings", "add_taxes_from_item_tax_template")] = 0
		fake = _Frappe(site)
		load_glue(fake).read_snapshot(_Settings(), _MAPS)
		assert fake.address_calls == 0


class TestMissingPosTemplate:
	def test_a_pos_profile_pointing_at_a_deleted_template_is_critical(self, load_glue):
		# Final check: v15 link validation rejects every paid sale whose taxes_and_charges is missing.
		site = _site()
		site["lists"]["POS Profile"] = [
			{"name": "Main POS", "disabled": 0, "company": "RT Co", "taxes_and_charges": "Deleted Tax"}
		]
		fake = _Frappe(site)
		glue = load_glue(fake)
		snapshot = glue.read_snapshot(_Settings(), _MAPS)
		assert any("Deleted Tax" in b and "does not exist" in b for b in snapshot.blockers)
		assert glue.run_preflight(_Settings(), _MAPS) is False
