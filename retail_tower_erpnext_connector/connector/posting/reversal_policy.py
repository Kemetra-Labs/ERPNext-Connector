# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Which reversal work-items the connector may post (RT-71; RT-14 F1, owner decision D8).

An amount-only refund cannot be posted correctly. Backend-Core never projects the refund amount
into the posting feed, so a ``reversalKind=refund`` work-item carries EVERY sale line at full
value, and the reversal builder would negate them all: any refund, even a partial one, became a
credit note for the whole sale (RT-48 bench: ACC-SINV-2026-00028 = -24.00 against a 24.00 sale).

Until line-aware returns land (RT-14 contract → RT-72/RT-73 in Backend-Core → RT-16 here), a refund
is rejected as ``permanently_rejected / validation``. Nothing is posted, so no stock or GL moves.
Once returns are supported, a rejected refund can be recovered with the Backend-Core ``re_post``
repair. A full void is unaffected. This module imports NO frappe.
"""

from __future__ import annotations

from .contracts import PostingWorkItem

_UNSUPPORTED_KINDS = frozenset({"refund"})


class UnsupportedReversal(Exception):
	"""A reversal the connector must not post yet (RT-71 fail-closed)."""


def assert_reversal_supported(work_item: PostingWorkItem) -> None:
	"""Raise :class:`UnsupportedReversal` for a reversal kind that cannot be posted correctly."""
	if work_item.kind != "reversal" or work_item.reversal_of is None:
		return
	if work_item.sale.responsibilities:
		# RT-419: the contract leaves a reversal of a payer-bearing sale undefined (posting-feed RT-417,
		# 035 OQ-4), and a credit note built on the store's Customer would credit the wrong debtor.
		raise UnsupportedReversal(
			"a reversal of a sale with payer responsibilities is not posted: the contract does not define "
			"it (RT-417), and it would credit the store's Customer instead of the payer"
		)
	kind = work_item.reversal_of.reversal_kind
	if kind in _UNSUPPORTED_KINDS:
		raise UnsupportedReversal(
			f"reversalKind {kind!r} is not posted: an amount-only refund carries no returned lines, "
			"so posting it would credit the whole sale. Line-aware returns are pending (RT-14 / RT-16); "
			"recover with the Backend-Core re_post repair once returns are supported."
		)
