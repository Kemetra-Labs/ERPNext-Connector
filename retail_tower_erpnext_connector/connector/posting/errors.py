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
