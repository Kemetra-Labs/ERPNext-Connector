# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""The Connector's one HTTP transport to Backend-Core (DP2), shared by every client (RT-1218).

* :class:`HttpTransport` — the Protocol the 012 posting client, the 019 bin-view client and the
  heartbeat client call. Tests inject fakes that implement only what their client uses.
* :class:`RequestsTransport` — the live implementation over the spec-003 connectorBearer
  (``Authorization: Bearer``), 30 s timeout. GET raises for an HTTP error status; POST returns a
  status-aware :class:`StatusResponse` so a client can tell 201/200-replay/409/404 apart.
* :func:`build_http_transport` — builds it from Connector Settings (Gate G4: the token is read with
  ``get_password``) and refuses an unset base URL or token, so a tick skips rather than posts blind.
* Dual-slot credentials (RT-1362 S3, ``provisioning-feed.yaml`` 1.2.0-draft "Credential rotation"): when
  Connector Settings hold a PENDING credential, a call authenticates with it first; a refusal falls back
  to CURRENT for that call and clears nothing; the first accepted (2xx) call promotes PENDING to CURRENT
  (``on_promote``). A refusal of CURRENT is retried once with the CURRENT secret as last committed
  (``reload_token``: another job promoted). Only the final answer of a call feeds the RT-1429 auth
  circuit, so neither fallback counts as a refused credential.

This module imports NO frappe; ``requests`` is imported on use (bench-only, not installed in CI).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Protocol
from urllib.parse import urlsplit

from .auth_circuit import AUTH_REFUSAL_STATUSES, AuthCircuit, AuthRefused
from .credential_slots import PendingCredential, read_pending

_TIMEOUT_S = 30

# Development-only opt-in (RT-1259): exactly "1" lets ``dp2_base_url`` be plain ``http://``. Off by
# default. An env var, not a Connector Settings field: no DocType/migration change, and a site
# cannot be put on cleartext by editing a setting in the UI.
ALLOW_INSECURE_ENV = "RT_CONNECTOR_ALLOW_INSECURE_DP2"


class HttpTransport(Protocol):
	"""The minimal HTTP surface the clients need: a real auth-backed transport on a bench, a fake in
	unit tests.

	``get`` returns the body ``dict`` (and raises on an error status). ``post`` returns either a
	status-aware response exposing ``.status`` / ``.headers`` / ``.body``, or — for legacy/simple
	transports — a plain body ``dict`` that the clients treat as a success.
	"""

	def get(self, path: str, *, params: dict, headers: dict) -> dict: ...

	def post(self, path: str, *, json: dict, headers: dict) -> object: ...


class StatusResponse:
	"""Status-aware response wrapper so a client can read 201/200/409/404 off a POST."""

	def __init__(self, r) -> None:
		self.status = r.status_code
		self.headers = dict(r.headers)
		try:
			self.body = r.json()
		except Exception:
			self.body = {}


class RequestsTransport:
	"""Concrete :class:`HttpTransport` to DP2 over the spec-003 connectorBearer (Authorization: Bearer).

	Bench-only (imports ``requests``, carries the secret token). GET raises for HTTP errors; POST
	returns a :class:`StatusResponse`.
	"""

	def __init__(
		self,
		base: str,
		token: str,
		*,
		circuit: AuthCircuit | None = None,
		on_open: Callable[[int], None] | None = None,
		on_close: Callable[[], None] | None = None,
		pending: PendingCredential | None = None,
		on_promote: Callable[[PendingCredential], object] | None = None,
		reload_token: Callable[[], str | None] | None = None,
		on_slot_error: Callable[[str, Exception], None] | None = None,
	) -> None:
		self._base = base.rstrip("/")
		self._token = token
		self._circuit = circuit
		self._on_open = on_open
		self._on_close = on_close
		self._pending = pending
		self._on_promote = on_promote
		self._reload_token = reload_token
		self._on_slot_error = on_slot_error

	def get(self, path: str, *, params: dict, headers: dict) -> dict:
		import requests

		if self._circuit is not None:
			self._circuit.acquire()  # raises AuthCircuitOpen: no call leaves while the credential is refused
		r = self._send(
			lambda token: requests.get(
				self._base + path, params=params, headers={**_bearer(token), **headers}, timeout=_TIMEOUT_S
			)
		)
		self._observe(r.status_code, raise_on_refusal=True)
		r.raise_for_status()
		return r.json()

	def post(self, path: str, *, json: dict, headers: dict) -> StatusResponse:
		import requests

		if self._circuit is not None:
			self._circuit.acquire()
		r = self._send(
			lambda token: requests.post(
				self._base + path, json=json, headers={**_bearer(token), **headers}, timeout=_TIMEOUT_S
			)
		)
		# A POST client reads the status itself (heartbeat 401, ack 409/404), so the refusal is recorded
		# here and the response is returned unchanged.
		self._observe(r.status_code, raise_on_refusal=False)
		return StatusResponse(r)

	def _send(self, call: Callable[[str], object]):
		"""One logical call: PENDING first when staged, then CURRENT, then CURRENT as last committed.

		Backend-Core authenticates a request before it acts on it, so a refused attempt changed nothing
		and repeating it with another credential is safe for every method."""
		pending = self._pending
		if pending is not None:
			r = call(pending.secret)
			status = r.status_code
			if status not in AUTH_REFUSAL_STATUSES:
				# Only a 2xx proves Backend-Core authenticated it: an unmatched route, a body-parser 400/413
				# or an edge 429 answers before authentication, and promoting on it could replace CURRENT
				# with a revoked credential. Any other answer keeps PENDING staged for the next call.
				if 200 <= status < 300:
					self._promote(pending)
				return r
			# Refused: a newer repeat or a failed rotation revoked it, or a superseded worker staged it.
			# Fall back to CURRENT and clear nothing (the next rotation step replaces the slot); this
			# transport does not present it again.
			self._pending = None
		r = call(self._token)
		if r.status_code in AUTH_REFUSAL_STATUSES and self._reload_token is not None:
			fresh = self._reload()
			if fresh and fresh != self._token:
				self._token = fresh  # another job promoted the pending credential after this one read it
				r = call(fresh)
		return r

	def _promote(self, pending: PendingCredential) -> None:
		"""Backend-Core accepted PENDING: use it as CURRENT from now on and persist the promotion.

		A failed persist is reported and changes nothing else: the slot still holds the credential,
		which Backend-Core now treats as current, so the next accepted call promotes it again."""
		self._token = pending.secret
		self._pending = None
		if self._on_promote is not None:
			on_promote = self._on_promote
			self._slot_call("promote", lambda: on_promote(pending))

	def _reload(self) -> str | None:
		"""The CURRENT secret as last committed, or ``None`` when it cannot be read. A failed read is not
		reported here: the refusal it could not rule out stands and the auth circuit reports it once."""
		try:
			return self._reload_token()
		except Exception:
			return None

	def _slot_call(self, what: str, run: Callable[[], object]):
		try:
			return run()
		except Exception as exc:  # the bookkeeping of a credential slot never fails the call itself
			if self._on_slot_error is not None:
				self._on_slot_error(what, exc)
			return None

	def _observe(self, status: int, *, raise_on_refusal: bool) -> None:
		"""Feed the auth circuit: a 401/403 opens or extends it; any answer below 500 proves the credential
		was accepted and closes it; a 5xx proves nothing either way (the edge or origin may be down)."""
		circuit = self._circuit
		if status in AUTH_REFUSAL_STATUSES:
			if circuit is not None and circuit.record_refusal(status) and self._on_open is not None:
				self._on_open(status)
			if raise_on_refusal:
				raise AuthRefused(status, reported=circuit is not None)
		elif status < 500 and circuit is not None and circuit.record_success() and self._on_close is not None:
			self._on_close()


def build_http_transport(
	settings,
	*,
	environ: Mapping[str, str] | None = None,
	circuit: AuthCircuit | None = None,
	on_open: Callable[[int], None] | None = None,
	on_close: Callable[[], None] | None = None,
	on_promote: Callable[[PendingCredential], object] | None = None,
	reload_token: Callable[[], str | None] | None = None,
	on_slot_error: Callable[[str, Exception], None] | None = None,
) -> RequestsTransport:
	"""Construct the auth-backed transport from Connector Settings (Gate G4 — token via get_password).

	Raises ``ValueError`` — which the ticks turn into a clean skip, before any request is made — if the
	base URL or token is unset, or if the URL is not ``https://``. The bearer token must not cross a
	cleartext link (CWE-319); ``RT_CONNECTOR_ALLOW_INSECURE_DP2=1`` allows ``http://`` for a bench.
	A staged PENDING credential (RT-1362 S3) is read as well and tried first (:class:`RequestsTransport`).
	"""
	env = os.environ if environ is None else environ
	base_url = (settings.dp2_base_url or "").strip()
	token = settings.get_password("dp2_token")  # decrypted; never get_single_value (masks it)
	if not base_url or not token:
		raise ValueError("Connector Settings: dp2_base_url and dp2_token are required")
	_require_https(base_url, env)
	return RequestsTransport(
		base_url,
		token,
		circuit=circuit,
		on_open=on_open,
		on_close=on_close,
		pending=read_pending(settings),
		on_promote=on_promote,
		reload_token=reload_token,
		on_slot_error=on_slot_error,
	)


def build_probe_transport(
	settings, secret: str, *, environ: Mapping[str, str] | None = None
) -> RequestsTransport:
	"""RT-1362 S3: a transport that authenticates with ``secret`` ALONE: no auth circuit, no slot logic.

	The provisioner uses it to learn whether a credential still in the PENDING slot is Backend-Core's
	current one before it replaces that slot. The same URL rules as :func:`build_http_transport`."""
	env = os.environ if environ is None else environ
	base_url = (settings.dp2_base_url or "").strip()
	if not base_url or not secret:
		raise ValueError("Connector Settings: dp2_base_url and a credential are required")
	_require_https(base_url, env)
	return RequestsTransport(base_url, secret)


def _require_https(base_url: str, env: Mapping[str, str]) -> None:
	scheme = urlsplit(base_url).scheme.lower()
	if scheme != "https" and not (scheme == "http" and env.get(ALLOW_INSECURE_ENV) == "1"):
		raise ValueError(
			"Connector Settings: dp2_base_url must be https:// "
			f"(set {ALLOW_INSECURE_ENV}=1 to allow http:// in development)"
		)


def _bearer(token: str) -> dict:
	return {"Authorization": "Bearer " + token}
