# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Shared text helpers for logged and raised strings (RT-1218).

Each call site passes its own limit and flags, so one helper keeps every site's output unchanged:

* :func:`scrub` — redact secret/token/credential substrings from a failure message, then
  optionally cut it to ``limit`` characters;
* :func:`error_code` — the ``code`` of a Backend-Core refusal body, else ``default``.

This module imports NO frappe.
"""

from __future__ import annotations

from .posting.reasons import scrub_message


def scrub(message: str, limit: int | None = None) -> str:
	"""``message`` with credentials redacted, cut to ``limit`` characters when one is given."""
	scrubbed = scrub_message(message)
	return scrubbed if limit is None else scrubbed[:limit]


def error_code(body: object, default: str, *, flat: bool, limit: int | None) -> str:
	"""The error ``code`` of DP2's canonical envelope ``{"error": {"code": ...}}``.

	``flat`` also accepts a flat ``{"code": ...}`` body (as older fakes/transports return) when the
	envelope has none. A found code is cut to ``limit`` characters when one is given; ``default``
	is returned as is when no code is set. Only the code is kept — never the message or the body.
	"""
	if isinstance(body, dict):
		envelope = body.get("error")
		if isinstance(envelope, dict) and envelope.get("code"):
			return _cut(str(envelope["code"]), limit)
		if flat and body.get("code"):
			return _cut(str(body["code"]), limit)
	return default


def _cut(text: str, limit: int | None) -> str:
	return text if limit is None else text[:limit]
