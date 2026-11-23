# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Payer → ERPNext Customer resolution for a credit / third-party sale (RT-419, owner decision R-1).

``Sale.responsibilities[].payerExternalRef`` IS the payer's ERPNext Customer name: the posting-feed
contract calls it "the payer's ERPNext Customer link" (``settlement.yaml`` ``PayerAccount.externalRef``).
So the mapping is the identity, with no map-row doctype and no Connector Settings field (official-first:
reuse the Customer link Backend-Core already freezes with the work item's resolution, RT-332).

What the Connector adds is the fail-closed check that the link names a Customer it may post against: the
Customer exists under exactly that name (MariaDB compares names case-blind, and a case-folded hit would
later surface as a representation mismatch), and is neither disabled nor frozen. A failure is
``UnmappedPayer`` → ``validation`` for that one item (payers are not part of the compatibility profile, so
no global pause), and no store or default Customer is ever substituted.

This module imports NO frappe: the glue reads the Customer row and passes it in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from .contracts import SaleResponsibility
from .errors import UnmappedPayer


def resolve_payer_customer(ref: str, row: Mapping[str, object] | None) -> str:
	"""The Customer name for payer link ``ref``, given its ``Customer`` row (``None`` = no such Customer)."""
	if row is None or row.get("name") != ref:
		raise UnmappedPayer(ref, "no ERPNext Customer with exactly that name")
	if row.get("disabled"):
		raise UnmappedPayer(ref, "the Customer is disabled")
	if row.get("is_frozen"):
		raise UnmappedPayer(ref, "the Customer is frozen")
	return ref


def payer_customer(
	responsibilities: Sequence[SaleResponsibility], payer_customer_for: Callable[[str], str] | None
) -> str:
	"""The debtor Customer of a payer-bearing sale (the parse allows exactly one responsibility).

	A null or blank link, or no resolver wired, is :class:`UnmappedPayer`: a credit sale never posts
	against the store's Customer by omission.
	"""
	(responsibility,) = responsibilities
	ref = responsibility.payer_external_ref
	if ref is None or not ref.strip():
		raise UnmappedPayer(ref, "the payer has no ERPNext Customer link (null or blank)")
	if payer_customer_for is None:
		raise UnmappedPayer(ref, "no payer resolver is wired")
	return payer_customer_for(ref)
