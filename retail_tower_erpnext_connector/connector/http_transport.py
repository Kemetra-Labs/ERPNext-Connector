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

This module imports NO frappe; ``requests`` is imported on use (bench-only, not installed in CI).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol
from urllib.parse import urlsplit

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

	def __init__(self, base: str, token: str) -> None:
		self._base = base.rstrip("/")
		self._auth = {"Authorization": "Bearer " + token}

	def get(self, path: str, *, params: dict, headers: dict) -> dict:
		import requests

		r = requests.get(
			self._base + path, params=params, headers={**self._auth, **headers}, timeout=_TIMEOUT_S
		)
		r.raise_for_status()
		return r.json()

	def post(self, path: str, *, json: dict, headers: dict) -> StatusResponse:
		import requests

		r = requests.post(self._base + path, json=json, headers={**self._auth, **headers}, timeout=_TIMEOUT_S)
		return StatusResponse(r)


def build_http_transport(settings, *, environ: Mapping[str, str] | None = None) -> RequestsTransport:
	"""Construct the auth-backed transport from Connector Settings (Gate G4 — token via get_password).

	Raises ``ValueError`` — which the ticks turn into a clean skip, before any request is made — if the
	base URL or token is unset, or if the URL is not ``https://``. The bearer token must not cross a
	cleartext link (CWE-319); ``RT_CONNECTOR_ALLOW_INSECURE_DP2=1`` allows ``http://`` for a bench.
	"""
	env = os.environ if environ is None else environ
	base_url = (settings.dp2_base_url or "").strip()
	token = settings.get_password("dp2_token")  # decrypted; never get_single_value (masks it)
	if not base_url or not token:
		raise ValueError("Connector Settings: dp2_base_url and dp2_token are required")
	scheme = urlsplit(base_url).scheme.lower()
	if scheme != "https" and not (scheme == "http" and env.get(ALLOW_INSECURE_ENV) == "1"):
		raise ValueError(
			"Connector Settings: dp2_base_url must be https:// "
			f"(set {ALLOW_INSECURE_ENV}=1 to allow http:// in development)"
		)
	return RequestsTransport(base_url, token)
