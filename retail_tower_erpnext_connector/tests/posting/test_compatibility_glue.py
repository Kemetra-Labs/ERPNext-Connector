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
			("Customer", "Walk-in"): {"disabled": 0},
			("Mode of Payment", "Cash"): {"enabled": 1},
		},
		"lists": {
			"Custom Field": [{"name": n} for n in _CUSTOM_FIELDS],
			"Sales Taxes and Charges Template": [],
			"Item Tax Template": [],
			"Item Tax": [],
			"Tax Rule": [],
			"POS Profile": [],
		},
		"pricing_rules": 0,
		"precision": 2,
	}


class _Frappe(types.ModuleType):
	def __init__(self, site, *, unreadable=False):
		super().__init__("frappe")
		self.__version__ = "15.42.0"
		self._site, self.unreadable = site, unreadable
		self._logger = _Logger()
		self.logger = lambda name=None: self._logger
		self.writes = []
		self.db = types.SimpleNamespace(
			get_single_value=self._get_single_value,
			get_value=self._get_value,
			count=lambda doctype, filters=None: site["pricing_rules"] if doctype == "Pricing Rule" else 0,
			set_single_value=self._set_single_value,
			set_value=lambda *a, **k: pytest.fail("the profile must never write to ERPNext (668)"),
		)
		self.utils = types.SimpleNamespace(now_datetime=lambda: "2026-10-11 20:00:00", cint=int)

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
		return types.SimpleNamespace(get_field=lambda name: types.SimpleNamespace(fieldname=name))

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
	names = ("frappe", "frappe.model", "frappe.model.meta", "erpnext", _GLUE)
	saved = [(sys.modules, n, sys.modules.get(n, _MISSING)) for n in names]
	saved.append((vars(pkg), "compatibility_glue", vars(pkg).get("compatibility_glue", _MISSING)))

	def _load(fake):
		meta = types.ModuleType("frappe.model.meta")
		meta.get_field_precision = lambda df, doc=None, currency=None: fake._site["precision"]
		sys.modules.update(
			{
				"frappe": fake,
				"frappe.model": types.ModuleType("frappe.model"),
				"frappe.model.meta": meta,
				"erpnext": types.SimpleNamespace(__version__="15.38.1"),
			}
		)
		sys.modules.pop(_GLUE, None)
		return importlib.import_module(_GLUE)

	yield _load
	for namespace, name, value in saved:
		namespace.pop(name, None)
		if value is not _MISSING:
			namespace[name] = value


def _events(fake):
	return [record["event"] for level, record in fake._logger.records if level == "error"]


class TestReadSnapshot:
	def test_a_compatible_site_reads_as_compatible(self, load_glue):
		fake = _Frappe(_site())
		glue = load_glue(fake)
		snapshot = glue.read_snapshot(_Settings(), _MAPS)
		assert snapshot.frappe_version == "15.42.0" and snapshot.erpnext_version == "15.38.1"
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

	def test_a_default_sales_taxes_template_is_reported(self, load_glue):
		site = _site()
		site["lists"]["Sales Taxes and Charges Template"] = [
			{"name": "VAT 14%", "is_default": 1, "disabled": 0, "company": "RT Co"}
		]
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		assert any("VAT 14%" in s for s in snapshot.tax_sources)

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

	def test_a_sales_tax_rule_is_reported_while_template_taxes_are_added(self, load_glue):
		site = _site()
		site["singles"][("Accounts Settings", "add_taxes_from_taxes_and_charges_template")] = 1
		site["lists"]["Tax Rule"] = [{"name": "Rule-1", "tax_type": "Sales", "company": "RT Co"}]
		snapshot = load_glue(_Frappe(site)).read_snapshot(_Settings(), _MAPS)
		assert any("Rule-1" in s for s in snapshot.tax_sources)

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
