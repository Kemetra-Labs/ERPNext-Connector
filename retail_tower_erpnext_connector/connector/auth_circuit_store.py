# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1429 — the live wiring of the auth circuit: shared Redis state and the transport every poller builds.

Every poller tick, the heartbeat and each long-queue job is its own RQ job, so the circuit's state cannot
live in a module global: it sits in the site's shared cache (``frappe.cache()``). A cache flush closes the
circuit; the next refused call reopens it, which costs one more event, never a lost posting.

:func:`guarded_transport` is what the entrypoints call in place of ``build_http_transport``. While the
circuit is open and no probe is due it raises :class:`AuthCircuitOpen` BEFORE any work is charged; every
caller already turns a build failure into a clean "skipped" log line, so a held-back tick does nothing.
The first refusal is reported once (Sentry + one log line); the recovery is one log line.

``frappe`` is imported on use, so the pure pieces stay testable without it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from .auth_circuit import AuthCircuit, AuthCircuitOpen, CircuitState
from .observability.sentry_client import capture_auth_circuit_open

CACHE_KEY = "rt_connector_auth_circuit"
_LOGGER = "retail_tower_connector_auth"
# A fixed, non-secret salt: the fingerprint only has to differ when the token differs.
_FINGERPRINT_SALT = b"rt-connector-auth-circuit-v1"
_FINGERPRINT_ROUNDS = 100_000


class FrappeCircuitStore:
	"""The circuit state in the site's shared Redis."""

	def load(self) -> CircuitState | None:
		import frappe

		return CircuitState.from_wire(frappe.cache().get_value(CACHE_KEY))

	def save(self, state: CircuitState) -> None:
		import frappe

		frappe.cache().set_value(CACHE_KEY, state.to_wire())

	def clear(self) -> None:
		import frappe

		frappe.cache().delete_value(CACHE_KEY)


def credential_key(settings) -> str:
	"""A key naming the CONFIGURED credential: its non-secret id plus a short one-way fingerprint of the token.

	The circuit is bound to this key, so replacing the token (pasting a re-issued one, a bench edit, the
	provisioner) is a different key: the next tick sees no refusal on record and calls Backend-Core at once,
	with no hook on Connector Settings (a DocType controller change would make the ERP release ``restore``
	class, RT-927). The fingerprint is the first 12 hex characters of a PBKDF2-HMAC-SHA256 of the bearer
	token (a change marker, not a stored password hash; PBKDF2 is the computationally expensive kind that
	code scanning accepts for sensitive data, about 20 ms once per tick). It cannot be reversed to the
	token, and it is never logged or sent anywhere.
	"""
	credential_id = str(getattr(settings, "dp2_credential_id", "") or "")
	try:
		token = settings.get_password("dp2_token") or ""
	except Exception:  # an unreadable token is the same as none: the transport builder refuses it anyway
		token = ""
	fingerprint = _fingerprint(token) if token else ""
	return f"{credential_id}:{fingerprint}"


def _fingerprint(token: str) -> str:
	digest = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), _FINGERPRINT_SALT, _FINGERPRINT_ROUNDS)
	return digest.hex()[:12]


def build_auth_circuit(settings) -> AuthCircuit:
	return AuthCircuit(FrappeCircuitStore(), credential_key=credential_key(settings))


def reset_auth_circuit() -> None:
	"""Close the circuit now (operator tool: ``bench execute <this module>.reset_auth_circuit``).

	Not needed after a token change, which is a new circuit key; useful to force an immediate probe.
	"""
	FrappeCircuitStore().clear()


def _logger():
	import frappe

	return frappe.logger(_LOGGER)


def _on_open(job_name: str, request_id: str) -> Callable[[int], None]:
	def opened(status: int) -> None:
		# One log line and one Sentry event per incident; the pollers then stay quiet until a probe.
		_logger().error(
			{
				"event": "connector.auth_circuit.opened",
				"status": status,
				"job_name": job_name,
				"request_id": request_id,
			}
		)
		capture_auth_circuit_open(status, request_id=request_id, job_name=job_name)

	return opened


def _on_close(job_name: str, request_id: str) -> Callable[[], None]:
	def closed() -> None:
		# A recovery is not an error (OBS-1 §4): it is logged, not sent to Sentry.
		_logger().info(
			{
				"event": "connector.auth_circuit.closed",
				"job_name": job_name,
				"request_id": request_id,
			}
		)

	return closed


def guarded_transport(settings, *, job_name: str, request_id: str):
	"""``build_http_transport`` plus the auth circuit. Raises :class:`AuthCircuitOpen` while held back."""
	from . import http_transport

	circuit = build_auth_circuit(settings)
	if circuit.blocked():
		raise AuthCircuitOpen(circuit.retry_in_s())
	return http_transport.build_http_transport(
		settings,
		circuit=circuit,
		on_open=_on_open(job_name, request_id),
		on_close=_on_close(job_name, request_id),
	)
