"""RT-1449 (RT-1448 blueprint v2, Step 0): can a read-only role run the ERPNext v16 finance reports the way
the Connector will run them?

Evidence class: THROWAWAY BENCH ON GITHUB ACTIONS (Frappe v16.52.0 / ERPNext v16.51.0, the RT-494 pins of
the `connector/bench` lane), never a lab, tenant or production site. It commits real documents, so it
refuses to start unless RT_PROOF_THROWAWAY=1, RT_PROOF_SITE names the site, the database host is local and
every Company on the site is one of its own.

Run (the workflow does this): cd ~/frappe-bench/sites && PYTHONPATH=<this dir> RT_PROOF_THROWAWAY=1
	RT_PROOF_SITE=<site> ../env/bin/python -c "import finance_report_role_proof as m; m.main()" <site>
It is launched by IMPORTING the module, not with `python -m`: the report jobs are pickled by module and
name, and a function of `__main__` cannot be unpickled by the worker.
Two bench workers must be running with the same PYTHONPATH and without CI set (CI makes a worker switch
to test mode): `bench worker --queue short,default,long` and `bench worker --queue finance_report` (the
queue is declared under `workers` in common_site_config).

Every case is independent: a failure is recorded with ERPNext's own message and the first failing
boundary, and the run goes on. The whole result is written to RT_PROOF_OUT (default
finance-report-proof-results.json). Nothing here is product code; the Connector does not import it.

Cases (blueprint section 11.2): bench identity, V1 (nine reports as the service user, inline and as a
job on a separate queue), V2 (write refusal, a user without the role), the stock-role and Auditor
comparisons, V4 parity, V5 two Companies, V6 prepared reports and the General Ledger size curve, V7
cost-centre GL legs and filter isolation, V8 closed Accounting Period, V9 first load signal.
"""

import json
import os
import statistics
import sys
import time
import traceback

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

MODULE = "finance_report_role_proof"
COMPANY, ABBR = "RT Fin Co", "RFC"
OTHER_COMPANY, OTHER_ABBR = "RT Other Co", "ROC"
ROLE = "RT Finance Report Reader"
UNRELATED_ROLE = "RT Proof Unrelated Role"
QUEUE = "finance_report"
CUSTOMER, SUPPLIER = "RT Proof Customer", "RT Proof Supplier"
USERS = {
	"svc": "rt-finance-svc@example.invalid",
	"auditor": "rt-auditor-svc@example.invalid",
	"accounts": "rt-accounts-user@example.invalid",
	"norole": "rt-norole@example.invalid",
}
CUSTOM_DOCPERM_DOCTYPES = (
	"GL Entry",
	"Sales Invoice",
	"Purchase Invoice",
	"Journal Entry",
	"Stock Ledger Entry",
)
REPORTS = (
	"Trial Balance",
	"Profit and Loss Statement",
	"Balance Sheet",
	"Cash Flow",
	"General Ledger",
	"Accounts Receivable",
	"Accounts Payable",
	"Stock Balance",
	"Bank Reconciliation Statement",
)
WRITE_DOCTYPES = (
	"Journal Entry",
	"Payment Entry",
	"Sales Invoice",
	"Account",
	"Cost Center",
	"Period Closing Voucher",
)
WRITE_RIGHTS = ("create", "write", "submit", "cancel", "delete")
ITEM_PRICES = (("RT-ITEM-A", 100), ("RT-ITEM-B", 150), ("RT-ITEM-C", 200))
RESULTS = {"meta": {}, "cases": {}}
STATE = {}


# ---- recording --------------------------------------------------------------------------------------


def record(case, ok, **data):
	RESULTS["cases"][case] = {"ok": ok, **data}
	print(json.dumps({"case": case, "ok": ok, **data}, default=str)[:3000], flush=True)


def clean(message):
	return frappe.utils.strip_html(str(message))[:600]


def has_failure(value):
	"""True when any nested result says ok=False (timed(), report_job() and the job probe all do), so a
	case that swallowed nine failed reports is not recorded as passed."""
	if isinstance(value, dict):
		return value.get("ok") is False or any(has_failure(v) for v in value.values())
	if isinstance(value, list):
		return any(has_failure(v) for v in value)
	return False


def step(case, fn, informational=False):
	try:
		data = fn() or {}
		frappe.db.commit()
		record(case, informational or not has_failure(data), **data)
	except Exception as e:  # the proof records ERPNext's refusal verbatim
		frappe.db.rollback()
		frappe.set_user("Administrator")
		record(
			case, False, error=f"{type(e).__name__}: {clean(e)}", tb=traceback.format_exc().splitlines()[-3:]
		)


# ---- setup ------------------------------------------------------------------------------------------


def guard():
	if os.environ.get("RT_PROOF_THROWAWAY") != "1":
		sys.exit("refusing to run: set RT_PROOF_THROWAWAY=1, and only on a throwaway bench site")
	if frappe.local.site != os.environ.get("RT_PROOF_SITE"):
		sys.exit("refusing to run: RT_PROOF_SITE must name this exact site")
	if frappe.conf.get("db_host") not in (None, "", "127.0.0.1", "localhost"):
		sys.exit(f"refusing to run: database host {frappe.conf.get('db_host')!r} is not local")
	if frappe.db.count("GL Entry"):
		sys.exit("refusing to run: the site already has GL Entries; use a fresh throwaway site")
	companies = frappe.get_all("Company", pluck="name")
	if set(companies) - {COMPANY, OTHER_COMPANY}:
		sys.exit(f"refusing to run: site has companies {companies}; use a fresh throwaway site")


def init_company():
	if not frappe.get_all("Company"):
		from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

		year = nowdate()[:4]
		setup_complete(
			{
				"language": "English",
				"country": "Egypt",
				"timezone": "Africa/Cairo",
				"currency": "EGP",
				"company_name": COMPANY,
				"company_abbr": ABBR,
				"chart_of_accounts": "Standard",
				"fy_start_date": f"{year}-01-01",
				"fy_end_date": f"{year}-12-31",
				"full_name": "Proof Admin",
				"email": "proof-admin@example.invalid",
			}
		)
		frappe.db.commit()
	frappe.db.set_value("Company", COMPANY, "enable_perpetual_inventory", 1)
	STATE["wh"] = (
		frappe.db.get_value("Warehouse", {"company": COMPANY, "is_group": 0, "warehouse_name": "Stores"})
		or frappe.get_all("Warehouse", filters={"company": COMPANY, "is_group": 0}, pluck="name", limit=1)[0]
	)
	STATE["year"] = getdate(nowdate()).year
	STATE["year_start"] = f"{STATE['year']}-01-01"
	STATE["today"] = nowdate()
	from erpnext.accounts.utils import get_fiscal_year

	STATE["fy"] = get_fiscal_year(nowdate(), company=COMPANY)[0]


def first_account(company, **filters):
	return frappe.db.get_value("Account", {"company": company, "is_group": 0, **filters}, "name")


def ensure_cost_centre(name, company, abbr):
	full = f"{name} - {abbr}"
	if not frappe.db.exists("Cost Center", full):
		root = frappe.db.sql(
			"select name from `tabCost Center` where company=%s and ifnull(parent_cost_center,'')='' limit 1",
			company,
		)[0][0]
		frappe.get_doc(
			{
				"doctype": "Cost Center",
				"cost_center_name": name,
				"parent_cost_center": root,
				"company": company,
				"is_group": 0,
			}
		).insert()
	return full


def ensure_masters():
	item_group = frappe.db.get_value("Item Group", {"is_group": 0})
	for code, stock in (("RT-ITEM-A", 1), ("RT-ITEM-B", 1), ("RT-ITEM-C", 1), ("RT-SERVICE", 0)):
		if not frappe.db.exists("Item", code):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": code,
					"item_group": item_group,
					"stock_uom": "Nos",
					"is_stock_item": stock,
					"is_sales_item": 1,
					"is_purchase_item": 1,
					"valuation_rate": 50 if stock else 0,
				}
			).insert()
	if not frappe.db.exists("Customer", CUSTOMER):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": CUSTOMER,
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}),
			}
		).insert()
	if not frappe.db.exists("Supplier", SUPPLIER):
		frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": SUPPLIER,
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}),
			}
		).insert()
	mop = frappe.get_doc("Mode of Payment", "Cash")
	cash = frappe.db.get_value("Company", COMPANY, "default_cash_account") or first_account(
		COMPANY, account_type="Cash"
	)
	if not any(a.company == COMPANY for a in mop.accounts):
		mop.append("accounts", {"company": COMPANY, "default_account": cash})
		mop.save()
	frappe.db.commit()


def receive_stock():
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"company": COMPANY,
			"posting_date": STATE["year_start"],
			"set_posting_time": 1,
			"items": [
				{
					"item_code": code,
					"qty": 5000,
					"t_warehouse": STATE["wh"],
					"basic_rate": 50,
					"conversion_factor": 1,
					"uom": "Nos",
				}
				for code, _price in ITEM_PRICES
			],
		}
	)
	se.insert()
	se.submit()
	frappe.db.commit()


def sales_invoice(cost_centre, date, lines, *, ret=None, credit=False, due=None):
	"""A POS sale (cash paid, stock moves) or, with credit=True, an open credit sale; ret = a credit note."""
	items = []
	for code, qty, price in lines:
		row = {
			"item_code": code,
			"qty": qty,
			"uom": "Nos",
			"conversion_factor": 1,
			"price_list_rate": price,
			"rate": price,
			"warehouse": STATE["wh"],
			"cost_center": cost_centre,
		}
		if ret:
			row["sales_invoice_item"] = ret["rows"][code]
		items.append(row)
	total = sum(flt(q) * flt(p) for _c, q, p in lines)
	doc = {
		"doctype": "Sales Invoice",
		"company": COMPANY,
		"customer": CUSTOMER,
		"posting_date": date,
		"set_posting_time": 1,
		"update_stock": 0 if credit else 1,
		"disable_rounded_total": 1,
		"ignore_pricing_rule": 1,
		"is_pos": 0 if credit else 1,
		"items": items,
	}
	if frappe.get_meta("Sales Invoice").has_field("cost_center"):
		doc["cost_center"] = cost_centre
	if credit:
		doc["due_date"] = due
	else:
		doc["payments"] = [{"mode_of_payment": "Cash", "amount": total}]
	if ret:
		doc.update({"is_return": 1, "return_against": ret["name"]})
	si = frappe.get_doc(doc)
	si.insert()
	si.submit()
	frappe.db.commit()  # each posted document stands on its own
	return {"name": si.name, "rows": {i.item_code: i.name for i in si.items}, "total": flt(si.grand_total)}


def seed_sales(bulk):
	today = getdate(STATE["today"])
	centres = list(STATE["cc"].values())
	first, count = {}, 0
	# Ascending dates: out-of-order backdated stock postings would queue Repost Item Valuation work.
	plan = sorted(
		(min(getdate(f"{STATE['year']}-{1 + i % today.month:02d}-{1 + i % 27:02d}"), today), i)
		for i in range(bulk)
	)
	for date, i in plan:
		code, price = ITEM_PRICES[i % 3]
		cc = centres[i % 2]
		inv = sales_invoice(cc, str(date), [(code, 1 + i % 3, price)])
		first.setdefault(cc, {**inv, "code": code, "price": price})
		count += 1
	for cc, inv in first.items():  # one credit note per store, for the return legs
		STATE.setdefault("returns", {})[cc] = sales_invoice(
			cc, STATE["today"], [(inv["code"], -1, inv["price"])], ret=inv
		)
	STATE["first_sale"] = first
	for n, cc in enumerate(centres * 3):  # open credit sales, aged 40-100 days
		date = str(max(getdate(add_days(STATE["today"], -(40 + n * 12))), getdate(STATE["year_start"])))
		sales_invoice(
			cc,
			date,
			[(ITEM_PRICES[n % 3][0], 1, ITEM_PRICES[n % 3][1])],
			credit=True,
			due=add_days(date, 30),
		)
	return count


def seed_ap_bank():
	cc = next(iter(STATE["cc"].values()))
	expense = first_account(COMPANY, root_type="Expense", account_type="Expense Account") or first_account(
		COMPANY, root_type="Expense"
	)
	for n in range(3):
		date = str(max(getdate(add_days(STATE["today"], -(35 + n * 20))), getdate(STATE["year_start"])))
		frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": COMPANY,
				"supplier": SUPPLIER,
				"posting_date": date,
				"set_posting_time": 1,
				"bill_no": f"RT-BILL-{n}",
				"bill_date": date,
				"due_date": add_days(date, 30),
				"items": [
					{
						"item_code": "RT-SERVICE",
						"qty": 1,
						"rate": 500 + n * 100,
						"expense_account": expense,
						"cost_center": cc,
					}
				],
			}
		).insert().submit()
	parent = frappe.db.get_value("Account", {"company": COMPANY, "account_type": "Bank", "is_group": 1})
	if not frappe.db.exists("Account", f"RT Proof Bank - {ABBR}"):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "RT Proof Bank",
				"parent_account": parent,
				"company": COMPANY,
				"account_type": "Bank",
				"is_group": 0,
			}
		).insert()
	STATE["bank"] = f"RT Proof Bank - {ABBR}"
	journal(COMPANY, STATE["bank"], first_account(COMPANY, root_type="Income"), 1000, cc=cc, bank=True)
	frappe.db.commit()


def journal(company, debit_account, credit_account, amount, *, cc=None, bank=False, party=None):
	cc = cc or frappe.get_cached_value("Company", company, "cost_center")
	debit = {"account": debit_account, "debit_in_account_currency": amount, "cost_center": cc}
	credit = {"account": credit_account, "credit_in_account_currency": amount, "cost_center": cc}
	if party:
		(debit if party[0] == "Customer" else credit).update({"party_type": party[0], "party": party[1]})
	doc = {
		"doctype": "Journal Entry",
		"voucher_type": "Bank Entry" if bank else "Journal Entry",
		"company": company,
		"posting_date": STATE["today"],
		"accounts": [debit, credit],
	}
	if bank:
		doc.update({"cheque_no": "RT-CHQ-1", "cheque_date": STATE["today"]})
	frappe.get_doc(doc).insert().submit()


def seed_other_company():
	if not frappe.db.exists("Company", OTHER_COMPANY):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": OTHER_COMPANY,
				"abbr": OTHER_ABBR,
				"default_currency": "EGP",
				"country": "Egypt",
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": "Standard",
			}
		).insert()
	income = first_account(OTHER_COMPANY, root_type="Income")
	cash = first_account(OTHER_COMPANY, account_type="Cash")
	journal(OTHER_COMPANY, cash, income, 7000)
	journal(
		OTHER_COMPANY,
		first_account(OTHER_COMPANY, account_type="Receivable"),
		income,
		3000,
		party=("Customer", CUSTOMER),
	)
	journal(
		OTHER_COMPANY,
		first_account(OTHER_COMPANY, root_type="Expense", account_type="Expense Account")
		or first_account(OTHER_COMPANY, root_type="Expense"),
		first_account(OTHER_COMPANY, account_type="Payable"),
		2000,
		party=("Supplier", SUPPLIER),
	)
	frappe.db.commit()


# ---- identities -------------------------------------------------------------------------------------


def ensure_user(email, roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"user_type": "System User",
				"roles": [{"role": r} for r in roles],
			}
		).insert(ignore_permissions=True)


def configure_role():
	"""The planned read-only role. Custom DocPerm REPLACES a doctype's standard permission list (Meta.
	set_custom_permissions), so setup_custom_perms copies the standard rows first; whether that happened
	is recorded because it decides what the stock roles keep."""
	from frappe.permissions import setup_custom_perms

	if not frappe.db.exists("Role", ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 1}).insert()
	copied = {}
	for doctype in CUSTOM_DOCPERM_DOCTYPES:
		copied[doctype] = bool(setup_custom_perms(doctype))
		if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": ROLE, "permlevel": 0}):
			frappe.get_doc(
				{
					"doctype": "Custom DocPerm",
					"parent": doctype,
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": ROLE,
					"permlevel": 0,
					"read": 1,
					"report": 1,
				}
			).insert(ignore_permissions=True)
	attached = {}
	for report in REPORTS:
		stock = frappe.get_all("Has Role", filters={"parent": report, "parenttype": "Report"}, pluck="role")
		name = frappe.db.get_value("Custom Role", {"report": report})
		doc = frappe.get_doc("Custom Role", name) if name else frappe.new_doc("Custom Role")
		doc.report = report
		doc.set("roles", [{"role": r} for r in sorted({*stock, ROLE})])
		doc.save(ignore_permissions=True)
		attached[report] = sorted({*stock, ROLE})
	frappe.db.commit()
	frappe.clear_cache()
	return {"standard_perms_copied_first": copied, "report_roles": attached}


def ensure_role(name):
	if not frappe.db.exists("Role", name):
		frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": 1}).insert()


def ensure_identities():
	# The roles must exist before the users that hold them. The no-role user holds an unrelated desk role:
	# a user with no role at all becomes a Website User, which would make its refusal weak evidence.
	ensure_role(ROLE)
	ensure_role(UNRELATED_ROLE)
	ensure_user(USERS["svc"], [ROLE])
	ensure_user(USERS["auditor"], ["Auditor"])
	ensure_user(USERS["accounts"], ["Accounts User"])
	ensure_user(USERS["norole"], [UNRELATED_ROLE])
	frappe.db.commit()


# ---- running reports --------------------------------------------------------------------------------


def filters_for(report, *, company=COMPANY, start=None, end=None, cost_centre=None):
	start, end = start or STATE["year_start"], end or STATE["today"]
	if report == "Trial Balance":
		f = {"company": company, "fiscal_year": STATE["fy"], "from_date": start, "to_date": end}
	elif report in ("Profit and Loss Statement", "Balance Sheet", "Cash Flow"):
		f = {
			"company": company,
			"filter_based_on": "Date Range",
			"period_start_date": start,
			"period_end_date": end,
			"periodicity": "Monthly",
			"selected_view": "Report",
		}
	elif report == "General Ledger":
		f = {
			"company": company,
			"from_date": start,
			"to_date": end,
			"categorize_by": "Categorize by Voucher (Consolidated)",
		}
	elif report in ("Accounts Receivable", "Accounts Payable"):
		f = {
			"company": company,
			"report_date": end,
			"ageing_based_on": "Due Date",
			"range": "30, 60, 90, 120",
		}
	elif report == "Stock Balance":
		f = {"company": company, "from_date": start, "to_date": end}
	else:  # Bank Reconciliation Statement
		f = {"company": company, "account": STATE["bank"], "report_date": end}
	if cost_centre and report not in ("Accounts Receivable", "Accounts Payable", "Stock Balance"):
		f["cost_center"] = [cost_centre]
	return f


def summarize(res, seconds):
	return {
		"seconds": round(seconds, 3),
		"rows": len(res.get("result") or []),
		"columns": len(res.get("columns") or []),
		"bytes": len(json.dumps(res, default=str)),
		"prepared": bool(res.get("prepared_report")),
	}


def run_as(user, report, filters, **kwargs):
	"""Runs the report live. Stock Balance ships with prepared_report=1 (and any report that runs past 15 s
	is switched to it), and a prepared report returns a stub instead of rows, so every run passes
	ignore_prepared_report=True except V6's explicit prepared-report flow."""
	from frappe.desk.query_report import run

	kwargs.setdefault("ignore_prepared_report", True)
	frappe.set_user(user)
	try:
		return run(report, filters=filters, **kwargs)
	finally:
		frappe.set_user("Administrator")


def timed(user, report, filters):
	t0 = time.perf_counter()
	try:
		return {"ok": True, **summarize(run_as(user, report, filters), time.perf_counter() - t0)}
	except Exception as e:
		return {
			"ok": False,
			"error": f"{type(e).__name__}: {clean(e)}",
			"seconds": round(time.perf_counter() - t0, 3),
		}


def report_job(report_name, filters, run_user):  # runs inside a bench worker
	frappe.set_user(run_user)
	t0 = time.perf_counter()
	try:
		from frappe.desk.query_report import run

		res = run(report_name, filters=filters, ignore_prepared_report=True)
		return {"ok": True, **summarize(res, time.perf_counter() - t0)}
	except Exception as e:
		return {
			"ok": False,
			"error": f"{type(e).__name__}: {clean(e)}",
			"seconds": round(time.perf_counter() - t0, 3),
		}
	finally:
		frappe.set_user("Administrator")


def ping_job():
	return {"ok": True}


def tick_job():  # a small write, standing in for one posting-poll tick
	doc = frappe.get_doc({"doctype": "ToDo", "description": "rt-1449 tick"}).insert(ignore_permissions=True)
	frappe.delete_doc("ToDo", doc.name, ignore_permissions=True, force=True)
	frappe.db.commit()
	return {"ok": True}


def wait_job(job, timeout=900):
	t0, status = time.time(), None
	while time.time() - t0 < timeout:
		status = job.get_status(refresh=True)
		if status in ("finished", "failed", "stopped", "canceled"):
			break
		time.sleep(0.2)
	if status == "finished":
		value = job.return_value() if hasattr(job, "return_value") else job.result
		return {"status": status, "value": value, "waited": round(time.time() - t0, 3)}
	return {"status": status, "value": None, "exc": (job.exc_info or "")[-400:] if status else "timeout"}


def enqueue(method, queue, **kwargs):
	"""method is the FUNCTION OBJECT, not a dotted string: the worker's frappe.get_attr needs an installed
	app as the first path segment, whereas RQ unpickles a callable by module and name. That needs this
	module to be imported under its own name (see main's launch note), never run as __main__."""
	frappe.db.commit()  # the worker must see everything posted so far
	return frappe.enqueue(method, queue=queue, timeout=900, **kwargs)


def queue_available(queue):
	try:
		out = wait_job(enqueue(ping_job, queue), 60)
		return out["status"] == "finished", out
	except Exception as e:
		return False, {"error": f"{type(e).__name__}: {clean(e)}"}


def job_run(user, report, filters):
	out = wait_job(enqueue(report_job, QUEUE, report_name=report, filters=filters, run_user=user))
	return out["value"] if out["value"] else {"ok": False, "error": out.get("exc") or out["status"]}


def rows_of(res):
	return [r for r in (res.get("result") or []) if isinstance(r, dict)]


def find_row(res, label):
	for r in rows_of(res):
		if str(r.get("account") or r.get("account_name") or "").strip("'") == label:
			return r
	return None


def normalise(res):
	def rnd(v):
		if isinstance(v, float):
			return round(v, 4)
		if isinstance(v, dict):
			return {k: rnd(x) for k, x in v.items()}
		if isinstance(v, list):
			return [rnd(x) for x in v]
		return v

	return json.dumps(rnd(res.get("result")), sort_keys=True, default=str)


def has_money(res):
	keys = ("debit", "credit", "total", "balance", "outstanding", "invoiced", "paid")
	return any(abs(flt(r.get(k))) > 0 for r in rows_of(res) for k in keys)


def gl_rows_between(start, end):
	return frappe.db.sql(
		"select count(*) from `tabGL Entry` where is_cancelled=0 and company=%s and posting_date between %s and %s",
		(COMPANY, start, end),
	)[0][0]


def sql_totals(**where):
	cond = " and ".join(f"{k}=%({k})s" for k in where)
	row = frappe.db.sql(
		f"select ifnull(sum(debit),0), ifnull(sum(credit),0), count(*) from `tabGL Entry` "
		f"where is_cancelled=0 and {cond}",
		where,
	)[0]
	return {"debit": flt(row[0]), "credit": flt(row[1]), "rows": row[2]}


# ---- cases ------------------------------------------------------------------------------------------


def case_identity():
	apps = {}
	for app in frappe.get_installed_apps():
		try:
			apps[app] = frappe.get_attr(f"{app}.__version__")
		except Exception:
			apps[app] = "unknown"
	return {
		"site": frappe.local.site,
		"frappe": frappe.__version__,
		"apps": apps,
		"database": frappe.db.sql("select version()")[0][0],
		"python": sys.version.split()[0],
		"companies": frappe.get_all("Company", pluck="name"),
		"service_user_roles": frappe.get_roles(USERS["svc"]),
		# Trial Balance's own Company check needs select on Company, which the service user gets only from
		# the automatic Desk User role: recorded because the design depends on it.
		"service_user_company_select": bool(frappe.has_permission("Company", "select", user=USERS["svc"])),
		"seed": STATE.get("seed"),
		"note": "synthetic data only; no lab, tenant or production site",
	}


def case_v1_inline():
	return {r: timed(USERS["svc"], r, filters_for(r)) for r in REPORTS}


def case_v1_jobs():
	up, probe = queue_available(QUEUE)
	STATE["jobs"] = up
	if not up:
		return {
			"queue_available": False,
			"jobs_check": {"ok": False, **probe},
			"note": "no worker could run a job on the queue: the inline results stand, V3 is NOT verified",
		}
	return {
		"queue_available": True,
		"reports": {r: job_run(USERS["svc"], r, filters_for(r)) for r in REPORTS},
	}


def case_v2_write_refusal():
	frappe.set_user(USERS["svc"])
	try:
		rights = {dt: {p: bool(frappe.has_permission(dt, p)) for p in WRITE_RIGHTS} for dt in WRITE_DOCTYPES}
		attempts = {}
		for dt in ("Account", "Cost Center", "Journal Entry"):
			try:
				frappe.get_doc({"doctype": dt}).insert()
				attempts[dt] = "INSERTED (unexpected)"
			except frappe.PermissionError:
				attempts[dt] = "PermissionError"
			except Exception as e:
				attempts[dt] = f"{type(e).__name__} (not a permission refusal)"
	finally:
		frappe.set_user("Administrator")
	any_right = any(v for r in rights.values() for v in r.values())
	return {
		"has_permission": rights,
		"insert_attempts": attempts,
		"any_write_right": any_right,
		"expectation": {
			"ok": not any_right and all(v == "PermissionError" for v in attempts.values()),
			"meaning": "the service user holds no write right and every insert is a permission refusal",
		},
	}


def refused(user, report):
	try:
		run_as(user, report, filters_for(report))
		return "ALLOWED"
	except frappe.PermissionError:
		return "PermissionError"
	except Exception as e:
		return f"{type(e).__name__}: {clean(e)[:120]}"


def case_v2_norole():
	reports = {r: refused(USERS["norole"], r) for r in REPORTS}
	return {
		"reports": reports,
		"expectation": {
			"ok": "ALLOWED" not in reports.values(),
			"meaning": "a user without the role is refused every report",
		},
	}


def case_stock_role_comparison():
	return {
		"accounts_user_after_custom_docperm": {
			r: timed(USERS["accounts"], r, filters_for(r)) for r in REPORTS
		},
		"stock_auditor_only": {r: timed(USERS["auditor"], r, filters_for(r)) for r in REPORTS},
		"note": "informational: tests reuse of the stock Auditor role against the planned custom role",
	}


def case_v4_parity():
	out = {}
	for report in (
		"Trial Balance",
		"Profit and Loss Statement",
		"Balance Sheet",
		"General Ledger",
		"Accounts Receivable",
	):
		f = filters_for(report)
		admin, svc = run_as("Administrator", report, f), run_as(USERS["svc"], report, f)
		out[report] = {
			"service_user_equals_administrator": normalise(admin) == normalise(svc),
			"rows": len(rows_of(svc)),
		}
	tb = run_as(USERS["svc"], "Trial Balance", filters_for("Trial Balance"))
	total, sql = find_row(tb, "Total"), sql_totals(company=COMPANY)
	out["trial_balance_vs_sql"] = {
		"report_debit": flt(total.get("debit")) if total else None,
		"report_credit": flt(total.get("credit")) if total else None,
		"sql": sql,
		"matches": bool(total)
		and abs(flt(total.get("debit")) - sql["debit"]) < 0.01
		and abs(flt(total.get("credit")) - sql["credit"]) < 0.01,
	}
	pnl = run_as(USERS["svc"], "Profit and Loss Statement", filters_for("Profit and Loss Statement"))
	profit = find_row(pnl, "Profit for the year")
	income = frappe.db.sql(
		"select ifnull(sum(g.credit-g.debit),0) from `tabGL Entry` g join `tabAccount` a on a.name=g.account "
		"where g.is_cancelled=0 and g.company=%s and a.root_type in ('Income','Expense')",
		COMPANY,
	)[0][0]
	out["profit_vs_sql"] = {"report": flt(profit.get("total")) if profit else None, "sql": flt(income)}
	same_rows = [v["service_user_equals_administrator"] for v in out.values() if "rows" in v]
	profit_ok = out["profit_vs_sql"]["report"] is not None and (
		abs(out["profit_vs_sql"]["report"] - out["profit_vs_sql"]["sql"]) < 0.01
	)
	out["expectation"] = {
		"ok": bool(all(same_rows) and out["trial_balance_vs_sql"]["matches"] and profit_ok),
		"meaning": "service user equals Administrator, and Trial Balance and profit equal independent SQL; "
		"a profit row that was not found counts as not verified",
	}
	out["note"] = (
		"Desk is the same run endpoint, so the Administrator run stands in for it; SQL is independent"
	)
	return out


def case_v5_two_company():
	reports = [r for r in REPORTS if r != "Bank Reconciliation Statement"]

	def probe():
		out = {}
		for r in reports:
			try:
				res = run_as(USERS["svc"], r, filters_for(r, company=OTHER_COMPANY))
				out[r] = {"returned": True, "rows": len(rows_of(res)), "other_company_data": has_money(res)}
			except frappe.PermissionError:
				out[r] = {"returned": False, "refused": "PermissionError"}
			except Exception as e:
				out[r] = {"returned": False, "refused": f"{type(e).__name__}: {clean(e)[:120]}"}
		return out

	before = probe()
	perm = frappe.get_doc(
		{
			"doctype": "User Permission",
			"user": USERS["svc"],
			"allow": "Company",
			"for_value": COMPANY,
			"apply_to_all_doctypes": 1,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache()
	try:
		after = probe()
	finally:
		frappe.delete_doc("User Permission", perm.name, ignore_permissions=True, force=True)
		frappe.db.commit()
		frappe.clear_cache()
	return {"without_user_permission": before, "with_company_user_permission": after}


def case_v6_prepared_and_size():
	out = {"report_flag_before": {r: frappe.db.get_value("Report", r, "prepared_report") for r in REPORTS}}
	f = filters_for("Stock Balance")
	first = run_as(USERS["svc"], "Stock Balance", dict(f), ignore_prepared_report=False)
	out["stock_balance_first_run"] = {
		"prepared": bool(first.get("prepared_report")),
		"rows": len(rows_of(first)),
	}
	from frappe.core.doctype.prepared_report.prepared_report import make_prepared_report

	frappe.set_user(USERS["svc"])
	try:
		name = make_prepared_report("Stock Balance", dict(f))["name"]
	finally:
		frappe.set_user("Administrator")
	frappe.db.commit()
	t0 = time.time()
	while time.time() - t0 < 600:
		frappe.db.rollback()  # see the worker's commit
		doc = frappe.db.get_value(
			"Prepared Report",
			name,
			["status", "queued_at", "report_end_time", "creation", "peak_memory_usage"],
			as_dict=True,
		)
		if doc.status in ("Completed", "Error", "Cancelled"):
			break
		time.sleep(1)
	out["prepared_report_doc"] = doc
	second = run_as(USERS["svc"], "Stock Balance", dict(f), ignore_prepared_report=False)
	out["stock_balance_after_prepare"] = {
		"prepared": bool(second.get("prepared_report")),
		"rows": len(rows_of(second)),
	}
	out["completion_time_field"] = "Prepared Report.report_end_time"
	months = getdate(STATE["today"]).month
	curve = []
	for span in sorted({1, min(3, months), min(6, months), months}):
		start = f"{STATE['year']}-{months - span + 1:02d}-01"
		f_gl = filters_for("General Ledger", start=start)
		res = timed(USERS["svc"], "General Ledger", f_gl)
		curve.append({"from": start, "gl_entries": gl_rows_between(start, STATE["today"]), **res})
	out["general_ledger_size_curve"] = curve
	out["report_flag_after"] = {r: frappe.db.get_value("Report", r, "prepared_report") for r in REPORTS}
	return out


def case_v7_cost_centre():
	legs = {}
	samples = {
		"sale": next(iter(STATE["first_sale"].values()))["name"],
		"return": next(iter(STATE["returns"].values()))["name"],
	}
	for label, voucher in samples.items():
		rows = frappe.db.sql(
			"select g.account, a.root_type, a.account_type, g.cost_center, g.debit, g.credit from `tabGL Entry` g "
			"join `tabAccount` a on a.name=g.account where g.voucher_no=%s and g.is_cancelled=0",
			voucher,
			as_dict=True,
		)
		legs[label] = {"voucher": voucher, "legs": rows}
	a, b = STATE["cc"].values()
	isolation = {}
	for report in ("Trial Balance", "Profit and Loss Statement", "General Ledger"):
		res = {
			"all": run_as(USERS["svc"], report, filters_for(report)),
			"store_a": run_as(USERS["svc"], report, filters_for(report, cost_centre=a)),
			"store_b": run_as(USERS["svc"], report, filters_for(report, cost_centre=b)),
		}
		isolation[report] = {k: len(rows_of(v)) for k, v in res.items()}
	sql = {k: sql_totals(company=COMPANY, cost_center=v) for k, v in (("store_a", a), ("store_b", b))}
	by_store = {}
	for key, centre in (("store_a", a), ("store_b", b)):
		row = find_row(
			run_as(USERS["svc"], "Trial Balance", filters_for("Trial Balance", cost_centre=centre)), "Total"
		)
		by_store[key] = {
			"report_debit": flt(row.get("debit")) if row else None,
			"sql_debit": sql[key]["debit"],
		}
	isolates = all(
		v["report_debit"] is not None and abs(v["report_debit"] - v["sql_debit"]) < 0.01
		for v in by_store.values()
	)
	return {
		"gl_legs": legs,
		"report_rows_by_filter": isolation,
		"sql_by_cost_centre": sql,
		"trial_balance_by_store": by_store,
		"expectation": {
			"ok": bool(isolates),
			"meaning": "Trial Balance with a cost_center filter equals the SQL total for that cost centre",
		},
	}


def case_v8_closed_period():
	start = f"{STATE['year']}-01-01"
	end = str(min(getdate(f"{STATE['year']}-01-31"), getdate(add_days(STATE["today"], -1))))
	if getdate(end) < getdate(start):
		return {"skipped": {"ok": False, "reason": "no closed past period yet this year: V8 is NOT verified"}}
	period = frappe.get_doc(
		{
			"doctype": "Accounting Period",
			"period_name": "RT Proof Closed",
			"company": COMPANY,
			"start_date": start,
			"end_date": end,
			"closed_documents": [{"document_type": "Sales Invoice", "closed": 1}],
		}
	).insert()
	frappe.db.commit()
	try:
		inv = sales_invoice(next(iter(STATE["cc"].values())), start, [("RT-ITEM-A", 1, 100)])
		outcome = {"posted": True, "invoice": inv["name"]}
	except Exception as e:
		frappe.db.rollback()
		outcome = {"posted": False, "refusal": f"{type(e).__name__}: {clean(e)}"}
	finally:
		frappe.delete_doc("Accounting Period", period.name, ignore_permissions=True, force=True)
		frappe.db.commit()
	return {"period": [start, end], "sales_invoice_into_closed_period": outcome}


def case_v9_load_signal():
	if not STATE.get("jobs"):
		return {"skipped": {"ok": False, "reason": "no worker on the report queue: V9 is NOT verified"}}

	def ticks(n, until=None, cap=150):
		"""n ticks, then more while `until` says the load is still running; counts the ticks that started
		while it was, so a window that missed the load cannot pass for a result."""
		out, overlapped = [], 0
		while len(out) < n or (until is not None and until() and len(out) < cap):
			if until is not None and until():
				overlapped += 1
			t0 = time.time()
			wait_job(enqueue(tick_job, "default"), 60)
			out.append(round(time.time() - t0, 3))
			time.sleep(0.5)
		return out, overlapped

	baseline, _ = ticks(10)
	heavy = [
		enqueue(
			report_job,
			QUEUE,
			report_name="General Ledger",
			filters=filters_for("General Ledger"),
			run_user=USERS["svc"],
		)
		for _ in range(6)
	]

	def load_running():
		return not all(
			j.get_status(refresh=True) in ("finished", "failed", "stopped", "canceled") for j in heavy
		)

	loaded, overlapped = ticks(5, until=load_running)
	outcomes = [wait_job(j) for j in heavy]
	stat = lambda xs: {"median": statistics.median(xs), "max": max(xs)}  # noqa: E731
	return {
		"tick_seconds_baseline": stat(baseline),
		"tick_seconds_under_report_load": stat(loaded),
		"ticks_started_during_load": overlapped,
		"heavy_jobs": [o["status"] for o in outcomes],
		"heavy_job_seconds": [(o["value"] or {}).get("seconds") for o in outcomes],
		"expectation": {
			"ok": overlapped >= 3 and all(o["status"] == "finished" for o in outcomes),
			"meaning": "at least three ticks started while the report jobs were still running",
		},
		"note": "first signal on one shared site and DB; the lab proof stays separate",
	}


# ---- main -------------------------------------------------------------------------------------------


def main():
	if len(sys.argv) != 2:
		sys.exit(f'usage: python -c "import {MODULE} as m; m.main()" <throwaway site>')
	frappe.init(site=sys.argv[1])
	frappe.connect()
	frappe.set_user("Administrator")
	guard()
	bulk = int(os.environ.get("RT_PROOF_BULK", "240"))
	RESULTS["meta"] = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "bulk_sales": bulk}

	def setup():
		init_company()
		ensure_masters()
		STATE["cc"] = {n: ensure_cost_centre(n, COMPANY, ABBR) for n in ("Store A", "Store B")}
		receive_stock()
		STATE["seed"] = {"bulk_sales": seed_sales(bulk), "cost_centres": STATE["cc"]}
		seed_ap_bank()
		seed_other_company()
		ensure_identities()
		return STATE["seed"]

	step("setup_seed", setup)
	if not RESULTS["cases"]["setup_seed"]["ok"]:  # any seeding step failing leaves a half-built site
		write_results()
		sys.exit("seeding failed: nothing to prove on; see the setup_seed case")
	step("role_configuration", configure_role)
	for case, fn, *informational in (
		("bench_identity", case_identity),
		("v1_reports_inline", case_v1_inline),
		("v1_v3_reports_as_jobs", case_v1_jobs),
		("v2_write_refusal", case_v2_write_refusal),
		("v2_user_without_role", case_v2_norole),
		("stock_role_and_auditor_comparison", case_stock_role_comparison, True),
		("v4_parity", case_v4_parity),
		("v5_two_company", case_v5_two_company),
		("v6_prepared_and_size", case_v6_prepared_and_size),
		("v7_cost_centre", case_v7_cost_centre),
		("v9_load_signal", case_v9_load_signal),
		("v8_closed_period", case_v8_closed_period),
	):
		step(case, fn, bool(informational))
	RESULTS["meta"]["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
	write_results()


def write_results():
	path = os.environ.get("RT_PROOF_OUT", "finance-report-proof-results.json")
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(RESULTS, fh, indent=1, default=str)
	failed = [c for c, r in RESULTS["cases"].items() if not r["ok"]]
	print(f"results written to {path}; cases failed: {failed or 'none'}", flush=True)


if __name__ == "__main__":
	main()
