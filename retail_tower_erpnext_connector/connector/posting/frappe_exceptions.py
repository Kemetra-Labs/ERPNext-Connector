# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Which Frappe exceptions are retryable or a duplicate-provenance hit, in a leaf module (RT-1460, A-21).

Both ``frappe_glue`` and ``frappe_count_port`` classify ERPNext failures with these two sets. They
used to live in ``frappe_glue``, and ``frappe_count_port`` reached them with a function-local import
while ``frappe_glue`` did the same the other way round. This module imports nothing from the package,
so both import it at the top level without a cycle. ``frappe_glue`` imports the two names, so
``frappe_glue._transient_exceptions`` and ``frappe_glue._dup_provenance_exceptions`` still resolve.

``frappe`` is imported inside each function, as these sets were always resolved lazily so they track
the installed frappe (and the glue tests' per-test ``frappe`` stand-in). That is a third-party import,
not an in-package one, so it hides no cycle.
"""

from __future__ import annotations


def _transient_exceptions() -> tuple[type[BaseException], ...]:
	"""The ERPNext/Frappe exceptions that are genuinely retryable (F-005).

	Everything else (ValidationError, PermissionError, KeyError, …) is non-retryable and
	maps to ``permanently_rejected`` — never silently re-offered forever. Resolved lazily so
	the set tracks the installed frappe; unknown names degrade to the base where absent.
	"""
	# RT-1312 F3: Frappe v16 raises QueryDeadlockError(Exception) on a MariaDB deadlock
	# (frappe/database/database.py); a deadlock is retryable, never a permanent rejection.
	# RT-1351: TimestampMismatchError (frappe/exceptions.py, a ValidationError subclass) is raised by
	# Document.check_if_latest when the row's `modified` differs from the in-memory copy: a failure of
	# Frappe's optimistic-lock check, not a business rule (RT-317 A1c: no other posting was involved;
	# see the RT-1351 PR). It is listed here because every stage tries this set BEFORE the generic
	# ValidationError branch; the savepoint already rolled the insert back, so a re-offer is safe and
	# a committed twin is stopped by the unique provenance index (G5).
	import frappe

	names = (
		"TimedOutError",
		"RetryBackoffError",
		"QueryTimeoutError",
		"QueryDeadlockError",
		"TimestampMismatchError",
	)
	found = tuple(getattr(frappe, n) for n in names if hasattr(frappe, n))
	# TimeoutError is always a sane transient signal even if frappe names none of the above.
	return (*found, TimeoutError)


def _dup_provenance_exceptions() -> tuple[type[BaseException], ...]:
	"""Exceptions a duplicate Sales Invoice `(rt_source_system, rt_external_id)` insert raises (G5/F-002).

	The `unique_rt_si_provenance` index on Sales Invoice (patch sales_invoice_unique_provenance)
	makes a SECOND submit of an already-posted sale fail at the DB. On bench frappe v15 this surfaces
	as **UniqueValidationError** (a ValidationError subclass — verified on the bench, NOT guessed);
	DuplicateEntryError is included defensively (the Posting Log path raises that one). getattr-guarded
	so a frappe missing a name degrades rather than errors. This MUST be caught BEFORE the generic
	`frappe.ValidationError` handler, or a legitimate crash-recovery would be FALSELY rejected.
	"""
	import frappe

	names = ("UniqueValidationError", "DuplicateEntryError")
	return tuple(exc for exc in (getattr(frappe.exceptions, n, None) for n in names) if exc is not None) or (
		Exception,
	)
