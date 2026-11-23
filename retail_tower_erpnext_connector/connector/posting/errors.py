# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Shared posting exception types, in a leaf module (RT-1460, audit A-21).

This module imports nothing from the package, so any posting module may import it at the top level
without a cycle. ``UnmappedUnit`` lived in ``builder`` while ``uom`` imported it from there, and
``builder`` hid the loop with a function-local import of ``uom``. ``builder.UnmappedUnit`` is still
the same class object (``builder`` re-exports it), so ``isinstance`` and ``except`` checks that name
either path keep working. No frappe import.
"""

from __future__ import annotations


class UnmappedUnit(Exception):
	"""A DP2 free-text ``unit`` has no entry in the signed unit→UOM map (FR-008).

	The caller maps this to a ``permanently_rejected`` / ``validation`` outcome — never a
	silent default (Principle VI). Raised by the injected UOM resolver.
	"""

	def __init__(self, unit: str) -> None:
		super().__init__(f"unit {unit!r} has no ERPNext UOM mapping (fail-closed → validation)")
		self.unit = unit


class UnmappedPayer(Exception):
	"""A sale's payer cannot be posted against: null, blank, unknown, disabled or frozen (RT-419, fail closed).

	``Sale.responsibilities[].payerExternalRef`` is the payer's ERPNext Customer link. The caller maps this
	to a ``permanently_rejected`` / ``validation`` outcome: the connector never falls back to the store's
	Customer or any default Customer (posting-feed ``SaleResponsibility.payerExternalRef``).
	"""

	def __init__(self, payer_external_ref: str | None, reason: str) -> None:
		super().__init__(
			f"payer {payer_external_ref!r} cannot be posted against: {reason} — fail-closed → validation "
			"(RT-419: never the store's Customer or a default Customer)"
		)
		self.payer_external_ref = payer_external_ref
