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
import time
from collections.abc import Callable

from . import credential_slots
from .auth_circuit import AuthCircuit, AuthCircuitOpen, CircuitState
from .auth_circuit_status import AuthCircuitStatus, build_status
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
	key = f"{credential_id}:{fingerprint}"
	# RT-1362 S3: a staged PENDING credential is a credential change too. The transport presents it first,
	# so a circuit opened before it was staged must not hold it back. Its id alone names it (non-secret).
	pending_id = str(getattr(settings, credential_slots.PENDING_ID, "") or "").strip()
	return f"{key}|{pending_id}" if pending_id else key


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


def read_auth_circuit() -> AuthCircuitStatus:
	"""The circuit as an operator may see it (RT-1431): a READ of the shared cache, never a write.

	It loads the record and compares it with the configured credential itself; the breaker's own read
	(``AuthCircuit._state``) clears a record that belongs to another credential, which a view must not do.
	"""
	import frappe

	settings = frappe.get_doc("Connector Settings")
	return build_status(FrappeCircuitStore().load(), current_key=credential_key(settings), now=time.time())


def read_auth_circuit_status() -> dict:
	"""Scripted read: ``bench execute <this module>.read_auth_circuit_status`` prints the same values as the
	"Connector Auth Circuit" report (state, last status, failures, changed at, next probe, note)."""
	return read_auth_circuit().to_dict()


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
		on_promote=_on_promote(job_name, request_id),
		reload_token=credential_slots.reload_current_token,
		on_slot_error=_on_slot_error(job_name, request_id),
	)


def _on_promote(job_name: str, request_id: str) -> Callable[[credential_slots.PendingCredential], bool]:
	def promote(pending: credential_slots.PendingCredential) -> bool:
		promoted = credential_slots.promote_pending(pending)
		if promoted:
			# One log line per rotation (a recovery, not an error); ids only, never a secret.
			_logger().info(
				{
					"event": "connector.credential.promoted",
					"credential_id": pending.credential_id,
					"job_name": job_name,
					"request_id": request_id,
				}
			)
		return promoted

	return promote


def _on_slot_error(job_name: str, request_id: str) -> Callable[[str, Exception], None]:
	def failed(what: str, exc: Exception) -> None:
		# The class name only: a database error text could carry a value. The slot keeps the credential,
		# so the next accepted call promotes it again.
		_logger().warning(
			{
				"event": "connector.credential.slot_error",
				"step": what,
				"error": type(exc).__name__,
				"job_name": job_name,
				"request_id": request_id,
			}
		)

	return failed
