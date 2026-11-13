# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Error classes of the provisioning contract and the control-flow exceptions of a run."""

from __future__ import annotations

import re
from enum import StrEnum

from ..connector.observability.scrubber import sanitize_message


class ErrorClass(StrEnum):
	"""The step outcomes other than ``done`` (``provisioning-feed.yaml`` ``ErrorClass``)."""

	failed_transient = "failed_transient"
	permanently_rejected = "permanently_rejected"
	conflict = "conflict"
	capacity = "capacity"
	compatibility = "compatibility"
	dependency = "dependency"


_ADMIN_PASSWORD_ARG = re.compile(r"(--admin-password\W+)[^\s'\"\]]+")
_LINE_BREAKS = re.compile(r"[\r\n]+")


def scrub(text: object, limit: int = 1000, known_secrets: tuple[str, ...] = ()) -> str:
	"""Detail or reason text that is safe to send and to log: no secret, token, email or phone.

	The scrubber masks labelled secrets only, so the values this process knows (the throwaway
	Administrator password, the issued credential) are removed by exact match first, a
	``--admin-password`` argument is masked wherever an argv list shows up, and line breaks are
	flattened so a Backend-Core string cannot forge a log line."""
	raw = text if isinstance(text, str) else str(text)
	for secret in known_secrets:
		if secret:
			raw = raw.replace(secret, "[redacted]")
	raw = _ADMIN_PASSWORD_ARG.sub(r"\1[redacted]", raw)
	return sanitize_message(_LINE_BREAKS.sub(" ", raw))[:limit]


class ProvisioningError(Exception):
	"""A step failed with a contract error class. ``reason`` and ``detail`` are scrubbed on creation."""

	def __init__(self, error_class: ErrorClass, reason: str, detail: str = "") -> None:
		self.error_class = ErrorClass(error_class)
		self.reason = scrub(reason, 200)
		self.detail = scrub(detail or reason, 1000)
		super().__init__(f"{self.error_class}: {self.reason}")


class LeaseLost(Exception):
	"""The lease is gone (``409 lease_lost``, or an unknown request): stop working it and re-pull."""


class LeaseTooShort(Exception):
	"""Not enough lease left to start a long step: stop and re-pull; nothing was started."""


class AuthRejected(Exception):
	"""``401``/``403`` on the feed: the provisioner credential is invalid. Fatal for the process."""


class FeedUnavailable(Exception):
	"""Backend-Core unreachable, or a 5xx: retry later."""


class AttemptCollision(Exception):
	"""``409 idempotency_key_conflict``: the attempt number was already used with another body."""


class CredentialRefused(Exception):
	"""The non-disclosing ``404`` on the credential call: reported as the ``dependency`` outcome."""


class CredentialFileError(Exception):
	"""The provisioner credential file is missing, empty, or not private to this process: fatal."""


class ProtocolError(Exception):
	"""A response or request that does not match the contract (a bug, never retried)."""
