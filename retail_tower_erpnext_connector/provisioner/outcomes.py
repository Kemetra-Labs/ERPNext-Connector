# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""What a worked request ends as, and how a failed step is reported (shared by every request kind)."""

from __future__ import annotations

import logging

from .errors import ErrorClass, ProvisioningError, scrub
from .lease import AckSession

LOG = logging.getLogger("rt.provisioner")

SUCCEEDED = "succeeded"
VERIFIED = "verified"  # credential.rotate: the provisioner's part is done; Backend-Core completes it
REJECTED = "rejected"
CONFLICT = "conflict"
TRANSIENT = "transient"
CAPACITY = "capacity"
SKIPPED = "skipped"


def report_failure(session: AckSession, step: str, err: ProvisioningError) -> str:
	"""Acknowledge ``step`` with the error class, then the outcome each class takes (contract
	"Error classes"): a retry for ``failed_transient`` and ``capacity``, a terminal outcome otherwise."""
	LOG.warning("step %s failed: %s", step, err)
	session.ack(step, err.error_class.value, reason=err.reason, detail=err.detail)
	cls = err.error_class
	if cls is ErrorClass.failed_transient:
		return TRANSIENT
	if cls is ErrorClass.capacity:
		return CAPACITY
	if cls is ErrorClass.conflict:
		session.complete("conflict", reason=err.reason)
		return CONFLICT
	reason = (
		err.reason if cls is ErrorClass.permanently_rejected else scrub(f"{cls.value}: {err.reason}", 200)
	)
	session.complete("permanently_rejected", reason=reason)
	return REJECTED
