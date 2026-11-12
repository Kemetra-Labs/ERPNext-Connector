"""RT-1340 / spec 048 OD-11: how a promotion clawback can post as an ERPNext v16 credit note.

Evidence class: LOCAL DISPOSABLE BENCH (Frappe v16.52.0 / ERPNext v16.51.0, the RT-494 pins of the
`connector/bench` lane), never the lab hosts or the rt9 tenant. Runs only on a throwaway site: it runs the
setup wizard (Egypt, EGP) when no Company exists and commits every document it posts.

Run from a bench: cd ~/frappe-bench/sites && ../env/bin/python <path>/od11_proof.py <site>
The site is required. The script refuses a site that has any Company other than "RT Proof Co".

Each case inserts and submits real Sales Invoices and credit notes in the shape the Connector builds
(ignore_pricing_rule, disable_rounded_total, update_stock, is_pos + payments, price_list_rate /
discount_amount / rate, return_against + sales_invoice_item), then prints totals, outstanding amount,
GL and Stock Ledger per voucher. A case ERPNext refuses is reported with ERPNext's own message and its
uncommitted document is rolled back. The last line is the whole result as RESULTS_JSON=<json>.

Cases: the E1 sale (reward line as an RT-857 visible discount); option A (the returned non-reward unit
at a reduced rate, with and without a qty-1 split); option B (a separate non-stock clawback row, with a
positive quantity or a negative rate); and golden sequence S3 (a clawback, then the reward unit at 200),
including a hybrid that gives the clawback back on a separate non-stock row.
"""

import json
import sys
import traceback

import frappe
from frappe.utils import flt, nowdate

PROOF_COMPANY = "RT Proof Co"
RESULTS = []


def log(case, **kw):
	RESULTS.append({"case": case, **kw})
	print(json.dumps({"case": case, **kw}, default=str, indent=1))


def setup():
	# Safety: this script commits real invoices, stock and GL. It runs only on a site that has no Company
	# yet (it then creates PROOF_COMPANY) or whose only Company is PROOF_COMPANY: never a lab or shared site.
	companies = frappe.get_all("Company", pluck="name")
	if companies and companies != [PROOF_COMPANY]:
		sys.exit(f"refusing to run: site has companies {companies}; use a fresh throwaway site")
	if not companies:
		from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

		year = nowdate()[:4]
		setup_complete(
			{
				"language": "English",
				"country": "Egypt",
				"timezone": "Africa/Cairo",
				"currency": "EGP",
				"company_name": PROOF_COMPANY,
				"company_abbr": "RTP",
				"chart_of_accounts": "Standard",
				"fy_start_date": f"{year}-01-01",
				"fy_end_date": f"{year}-12-31",
				"full_name": "Proof Admin",
				"email": "proof-admin@example.invalid",
			}
		)
		frappe.db.commit()
	company = frappe.get_all("Company", pluck="name", limit=1)[0]
	wh = (
		frappe.db.get_value("Warehouse", {"company": company, "is_group": 0, "warehouse_name": "Stores"})
		or frappe.get_all("Warehouse", filters={"company": company, "is_group": 0}, pluck="name", limit=1)[0]
	)
	frappe.db.set_value("Company", company, "enable_perpetual_inventory", 1)
	for code, stock in (("RT-ITEM-A", 1), ("RT-ITEM-B", 1), ("RT-ITEM-C", 1), ("RT-PROMO-CLAWBACK", 0)):
		if not frappe.db.exists("Item", code):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": code,
					"item_group": frappe.db.get_value("Item Group", {"is_group": 0}),
					"stock_uom": "Nos",
					"is_stock_item": stock,
					"is_sales_item": 1,
					"valuation_rate": 50 if stock else 0,
				}
			).insert()
	if not frappe.db.exists("Customer", "RT Proof Customer"):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "RT Proof Customer",
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}),
			}
		).insert()
	mop = frappe.get_doc("Mode of Payment", "Cash")
	cash_acc = frappe.db.get_value("Company", company, "default_cash_account") or frappe.db.get_value(
		"Account", {"company": company, "account_type": "Cash", "is_group": 0}
	)
	if not any(a.company == company for a in mop.accounts):
		mop.append("accounts", {"company": company, "default_account": cash_acc})
		mop.save()
	if not frappe.db.get_value("Bin", {"item_code": "RT-ITEM-A", "warehouse": wh}, "actual_qty"):
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": company,
				"items": [
					{
						"item_code": c,
						"qty": 100,
						"t_warehouse": wh,
						"basic_rate": 50,
						"conversion_factor": 1,
						"uom": "Nos",
					}
					for c in ("RT-ITEM-A", "RT-ITEM-B", "RT-ITEM-C")
				],
			}
		)
		se.insert()
		se.submit()
	frappe.db.commit()
	return company, wh


def row(code, qty, unit_price, rate=None, wh=None, sii=None):
	r = {
		"item_code": code,
		"qty": qty,
		"uom": "Nos",
		"conversion_factor": 1,
		"price_list_rate": unit_price,
		"rate": rate if rate is not None else unit_price,
		"warehouse": wh,
	}
	if rate is not None and flt(rate) != flt(unit_price):
		r["discount_amount"] = flt(unit_price) - flt(rate)
	if sii:
		r["sales_invoice_item"] = sii
	return r


def invoice(company, items, paid, *, return_against=None, stock=1):
	doc = {
		"doctype": "Sales Invoice",
		"company": company,
		"customer": "RT Proof Customer",
		"currency": frappe.db.get_value("Company", company, "default_currency"),
		"posting_date": nowdate(),
		"set_posting_time": 1,
		"update_stock": stock,
		"disable_rounded_total": 1,
		"ignore_pricing_rule": 1,
		"is_pos": 1,
		"items": items,
		"payments": [{"mode_of_payment": "Cash", "amount": paid}],
	}
	if return_against:
		doc.update({"is_return": 1, "return_against": return_against})
	si = frappe.get_doc(doc)
	si.insert()
	si.submit()
	frappe.db.commit()  # each posted document stands on its own (a later probe's rollback must not undo it)
	return si


def evidence(si):
	gl = frappe.db.sql(
		"""select account, sum(debit) d, sum(credit) c from `tabGL Entry`
		where voucher_no=%s and is_cancelled=0 group by account order by account""",
		si.name,
		as_dict=True,
	)
	sle = frappe.db.sql(
		"""select item_code, actual_qty, stock_value_difference from `tabStock Ledger Entry`
		where voucher_no=%s and is_cancelled=0 order by item_code""",
		si.name,
		as_dict=True,
	)
	return {
		"name": si.name,
		"items": [
			(
				i.item_code,
				flt(i.qty),
				flt(i.price_list_rate),
				flt(i.discount_amount),
				flt(i.rate),
				flt(i.amount),
			)
			for i in si.items
		],
		"net_total": flt(si.net_total),
		"grand_total": flt(si.grand_total),
		"rounded_total": flt(si.rounded_total),
		"paid_amount": flt(si.paid_amount),
		"outstanding_amount": flt(si.outstanding_amount),
		"status": si.status,
		"gl": [(g.account, flt(g.d), flt(g.c)) for g in gl],
		"gl_balanced": abs(sum(flt(g.d) - flt(g.c) for g in gl)) < 1e-9,
		"sle": [(s.item_code, flt(s.actual_qty), flt(s.stock_value_difference)) for s in sle],
	}


def run(case, fn):
	try:
		out = fn()
		frappe.db.commit()
		log(case, ok=True, **out)
	except Exception as e:  # the proof records ERPNext's refusal verbatim
		frappe.db.rollback()
		log(
			case,
			ok=False,
			error=f"{type(e).__name__}: {frappe.utils.strip_html(str(e))}",
			tb=traceback.format_exc().splitlines()[-3:],
		)


def sale(company, wh, lines, paid):
	return invoice(company, [row(c, q, p, r, wh) for c, q, p, r in lines], paid)


def rows_of(si):
	return {i.item_code: i.name for i in si.items}


def main():  # noqa: C901 - one flat list of proof cases, read top to bottom
	if len(sys.argv) != 2:
		sys.exit("usage: od11_proof.py <throwaway site>")
	frappe.init(site=sys.argv[1])
	frappe.connect()
	frappe.set_user("Administrator")
	company, wh = setup()
	print(
		"company",
		company,
		"warehouse",
		wh,
		"erpnext",
		frappe.get_attr("erpnext.__version__"),
		"frappe",
		frappe.__version__,
	)

	# ---- E1: 300 (trigger) + 200 (reward, 80 off) = 420 paid -----------------------------------------
	def e1_sale():
		return sale(company, wh, [("RT-ITEM-A", 1, 300, None), ("RT-ITEM-B", 1, 200, 120)], 420)

	state = {}

	def sale_case():
		si = e1_sale()
		state["e1"] = si
		return {"sale": evidence(si)}

	run("E1 sale (reward line as RT-857 visible discount)", sale_case)

	# Option A: return the 300 at rate 220 (pl 300, discount 80) -> credit note total = refund 220.
	def a1():
		s = state["e1"]
		r = rows_of(s)
		cn = invoice(
			company, [row("RT-ITEM-A", -1, 300, 220, wh, r["RT-ITEM-A"])], -220, return_against=s.name
		)
		state["a1"] = cn
		return {
			"credit_note": evidence(cn),
			"refund_expected": -220,
			"total_equals_refund": flt(cn.grand_total) == -220,
		}

	run("A1 option A: return the 300 with clawback 80 (rate 220)", a1)

	# S3 step 2 under A: the 200 reward unit, refund 200 > its sale-time net 120 (rate above original rate).
	def a1b():
		s = state["e1"]
		r = rows_of(s)
		cn = invoice(
			company, [row("RT-ITEM-B", -1, 200, 200, wh, r["RT-ITEM-B"])], -200, return_against=s.name
		)
		return {
			"credit_note": evidence(cn),
			"refund_expected": -200,
			"total_equals_refund": flt(cn.grand_total) == -200,
		}

	run("A1b S3 step 2: return the reward at 200 (above its net 120)", a1b)

	def a1c():
		s = state["e1"]
		r = rows_of(s)
		cn = invoice(
			company, [row("RT-ITEM-B", -1, 200, 120, wh, r["RT-ITEM-B"])], -120, return_against=s.name
		)
		return {
			"credit_note": evidence(cn),
			"note": "reward unit returned at its net 120 (RT-857 shape on a credit note)",
		}

	run("A1c return the reward at its net 120", a1c)

	# Option A with a split: 3 x 100 non-reward units returned with a clawback of 10.01 (not divisible by 3).
	def a2():
		s = sale(company, wh, [("RT-ITEM-C", 3, 100, None), ("RT-ITEM-B", 1, 50, 39.99)], 339.99)
		r = rows_of(s)
		cn = invoice(
			company,
			[
				row("RT-ITEM-C", -1, 100, 96.66, wh, r["RT-ITEM-C"]),
				row("RT-ITEM-C", -1, 100, 96.66, wh, r["RT-ITEM-C"]),
				row("RT-ITEM-C", -1, 100, 96.67, wh, r["RT-ITEM-C"]),
			],
			-289.99,
			return_against=s.name,
		)
		# then try to over-return one more unit of the same row (ERPNext's cumulative cap must still hold)
		over = None
		try:
			invoice(
				company, [row("RT-ITEM-C", -1, 100, 100, wh, r["RT-ITEM-C"])], -100, return_against=s.name
			)
			over = "ACCEPTED (cap broken!)"
		except Exception as e:
			frappe.db.rollback()
			over = f"refused: {frappe.utils.strip_html(str(e))[:200]}"
		return {
			"sale": evidence(s),
			"credit_note": evidence(cn),
			"refund_expected": -289.99,
			"total_equals_refund": abs(flt(cn.grand_total) + 289.99) < 1e-9,
			"over_return": over,
		}

	run("A2 option A split to qty 1: 3 x 100 with clawback 10.01", a2)

	# Option B: the 300 at its original net + a separate non-stock clawback line.
	def b_case(qty, rate, label):
		def f():
			s = e1_sale()
			r = rows_of(s)
			cn = invoice(
				company,
				[
					row("RT-ITEM-A", -1, 300, None, wh, r["RT-ITEM-A"]),
					{
						"item_code": "RT-PROMO-CLAWBACK",
						"qty": qty,
						"uom": "Nos",
						"conversion_factor": 1,
						"price_list_rate": rate,
						"rate": rate,
						"warehouse": wh,
					},
				],
				-220,
				return_against=s.name,
			)
			return {
				"credit_note": evidence(cn),
				"refund_expected": -220,
				"total_equals_refund": flt(cn.grand_total) == -220,
				"variant": label,
			}

		return f

	run("B1 option B: clawback line qty +1 rate 80", b_case(1, 80, "positive qty in a return"))
	run("B2 option B: clawback line qty -1 rate -80", b_case(-1, -80, "negative rate"))

	# Golden sequence S3 (E1): return 1 = the 300 with clawback 80 (refund 220); return 2 = the 200
	# reward unit, refund 200.00 (the earlier clawback is given back; total refunded 420 = paid).
	def s3_a():
		s = e1_sale()
		r = rows_of(s)
		cn1 = invoice(
			company, [row("RT-ITEM-A", -1, 300, 220, wh, r["RT-ITEM-A"])], -220, return_against=s.name
		)
		out = {"sale": s.name, "return_1": evidence(cn1)}
		try:
			cn2 = invoice(
				company, [row("RT-ITEM-B", -1, 200, 200, wh, r["RT-ITEM-B"])], -200, return_against=s.name
			)
			out["return_2"] = evidence(cn2)
		except Exception as e:
			frappe.db.rollback()
			out["return_2"] = f"REFUSED: {frappe.utils.strip_html(str(e))[:300]}"
		return out

	run("S3 under A: 300 at 220, then the reward at 200", s3_a)

	def s3_b():
		s = e1_sale()
		r = rows_of(s)

		def claw(qty):
			return {
				"item_code": "RT-PROMO-CLAWBACK",
				"qty": qty,
				"uom": "Nos",
				"conversion_factor": 1,
				"price_list_rate": 80,
				"rate": 80,
				"warehouse": wh,
			}

		cn1 = invoice(
			company,
			[row("RT-ITEM-A", -1, 300, None, wh, r["RT-ITEM-A"]), claw(1)],
			-220,
			return_against=s.name,
		)
		cn2 = invoice(
			company,
			[row("RT-ITEM-B", -1, 200, 120, wh, r["RT-ITEM-B"]), claw(-1)],
			-200,
			return_against=s.name,
		)
		total = flt(cn1.grand_total) + flt(cn2.grand_total)
		return {
			"sale": s.name,
			"return_1": evidence(cn1),
			"return_2": evidence(cn2),
			"refunded_total": total,
			"refunded_equals_paid": total == -420,
		}

	run("S3 under B: 300 + clawback line, then reward at net + clawback give-back", s3_b)

	# S3 step 2 as a hybrid: step 1 under A (the 300 at 220), then the reward at its net 120 plus a
	# non-stock give-back row (qty -1 x 80) that is not on the original invoice (review of PR for RT-1340).
	def s3_hybrid():
		s = e1_sale()
		r = rows_of(s)
		cn1 = invoice(
			company, [row("RT-ITEM-A", -1, 300, 220, wh, r["RT-ITEM-A"])], -220, return_against=s.name
		)
		frappe.message_log = []
		give_back = {
			"item_code": "RT-PROMO-CLAWBACK",
			"qty": -1,
			"uom": "Nos",
			"conversion_factor": 1,
			"price_list_rate": 80,
			"rate": 80,
			"warehouse": wh,
		}
		cn2 = invoice(
			company,
			[row("RT-ITEM-B", -1, 200, 120, wh, r["RT-ITEM-B"]), give_back],
			-200,
			return_against=s.name,
		)
		total = flt(cn1.grand_total) + flt(cn2.grand_total)
		return {
			"sale": s.name,
			"return_1": evidence(cn1),
			"return_2": evidence(cn2),
			"return_2_messages": [frappe.utils.strip_html(str(m)) for m in frappe.message_log],
			"refunded_total": total,
			"refunded_equals_paid": total == -420,
		}

	run("S3 hybrid: A at step 1, then the reward at its net + a give-back row qty -1 x 80", s3_hybrid)

	print("RESULTS_JSON=" + json.dumps(RESULTS, default=str))


main()
