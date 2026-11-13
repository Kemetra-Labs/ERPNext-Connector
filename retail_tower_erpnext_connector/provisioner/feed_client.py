# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""The pull client for ``provisioning-feed.yaml`` 1.0.0-draft (Backend-Core): pull, step ack, one-time
credential issue, final outcome.

It calls through the Connector's shared :class:`HttpTransport` (``self._transport``, module-level
``/api/`` path constants), so ``tools/contracts-check`` verifies every path and method it uses
against the pinned contract, and this module imports no HTTP library."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from ..connector.http_transport import HttpTransport
from .errors import (
	AttemptCollision,
	AuthRejected,
	CredentialFileError,
	CredentialRefused,
	FeedUnavailable,
	LeaseLost,
	ProtocolError,
)

_PULL_PATH = "/api/connector/v1/erpnext/provisioning/requests"
_ACK_PATH = "/api/connector/v1/erpnext/provisioning/requests/{request_id}/steps"
_CREDENTIAL_PATH = "/api/connector/v1/erpnext/provisioning/requests/{request_id}/credential"
_OUTCOME_PATH = "/api/connector/v1/erpnext/provisioning/requests/{request_id}/outcome"


@dataclass(frozen=True)
class Response:
	"""A POST response; the same shape as the shared transport's ``StatusResponse``."""

	status: int
	headers: dict
	body: dict


@dataclass(frozen=True)
class Store:
	store_id: str
	display_name: str


@dataclass(frozen=True)
class ProvisioningRequest:
	id: str
	kind: str
	tenant_id: str
	generation: int
	site_name: str
	template_id: str
	template_version: int
	country: str
	currency: str
	timezone: str
	language: str
	legal_name: str | None
	registration_id: str
	environment: str
	attempt_floor: int
	not_before: str | None
	stores: tuple[Store, ...]

	@classmethod
	def from_wire(cls, wire: dict) -> ProvisioningRequest:
		try:
			template, locale = wire["template"], wire["locale"]
			return cls(
				id=_uuid(wire["provisioningRequestId"]),
				kind=wire["kind"],
				tenant_id=_uuid(wire["tenantId"]),
				generation=int(wire["generation"]),
				site_name=wire["siteName"],
				template_id=template["templateId"],
				template_version=int(template["templateVersion"]),
				country=locale["country"],
				currency=locale["currency"],
				timezone=locale["timezone"],
				language=locale["language"],
				legal_name=wire.get("legalName"),
				registration_id=_uuid(wire["connectorRegistrationId"]),
				environment=wire["environment"],
				attempt_floor=int(wire["attemptFloor"]),
				not_before=wire.get("notBefore"),
				stores=tuple(Store(s["storeId"], s["displayName"]) for s in wire.get("stores", [])),
			)
		except (KeyError, TypeError, ValueError, AttributeError) as exc:
			raise ValueError(f"malformed provisioning request: missing or invalid {exc}") from exc


@dataclass(frozen=True)
class Page:
	items: list[ProvisioningRequest]
	cursor: str


@dataclass(frozen=True)
class IssuedCredential:
	credential_id: str
	registration_id: str
	issued_at: str
	expires_at: str
	rotated: bool
	secret: str

	def __repr__(self) -> str:  # the secret is shown nowhere
		return f"IssuedCredential(credential_id={self.credential_id!r}, secret=<redacted>)"

	__str__ = __repr__


def _uuid(value: object) -> str:
	"""A canonical lower-case UUID or ValueError: ids from the feed end up in URL paths and in
	the site marker, so nothing else is accepted."""
	text = str(value)
	if str(uuid.UUID(text)) != text.lower():
		raise ValueError("not a canonical UUID")
	return text.lower()


def parse_time(value: str) -> datetime:
	return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _failure(exc: Exception) -> Exception:
	"""Map a transport exception (a ``requests`` HTTP error carries ``.response``) to a run signal."""
	if isinstance(exc, CredentialFileError):
		return exc  # a fatal local problem, not an outage
	status = getattr(getattr(exc, "response", None), "status_code", None)
	if status in (401, 403):
		return AuthRejected(f"HTTP {status}")
	if status is not None and status < 500:
		return ProtocolError(f"HTTP {status}")
	return FeedUnavailable(type(exc).__name__)


def _lease_expiry(resp) -> datetime:
	"""``leaseExpiresAt`` of a 200 ack, as an aware datetime, or a protocol error."""
	try:
		expires = parse_time(resp.body["leaseExpiresAt"])
	except (KeyError, TypeError, ValueError, AttributeError) as exc:
		raise ProtocolError("ack: the response has no valid leaseExpiresAt") from exc
	if expires.tzinfo is None:
		raise ProtocolError("ack: leaseExpiresAt has no time zone")
	return expires


def _code(resp) -> str:
	error = resp.body.get("error") if isinstance(resp.body, dict) else None
	return error.get("code", "") if isinstance(error, dict) else ""


def _checked(resp) -> None:
	"""Run-level statuses every POST shares: bad credential, or Backend-Core unavailable."""
	if resp.status in (401, 403):
		raise AuthRejected(f"HTTP {resp.status}")
	if resp.status >= 500:
		raise FeedUnavailable(f"HTTP {resp.status}")


class FeedClient:
	def __init__(self, transport: HttpTransport) -> None:
		self._transport = transport

	def pull(self, cursor: str | None = None, limit: int = 20) -> Page:
		params: dict = {"limit": limit}
		if cursor:
			params["cursor"] = cursor
		try:
			body = self._transport.get(_PULL_PATH, params=params, headers={})
		except Exception as exc:
			raise _failure(exc) from exc
		if not isinstance(body, dict) or not isinstance(body.get("items", []), list):
			raise ProtocolError("pull: the response is not a page object")
		items = []
		for wire in body.get("items", []):
			if not isinstance(wire, dict):
				continue
			try:
				items.append(ProvisioningRequest.from_wire(wire))
			except ValueError:
				continue  # one malformed item must not block the others
		return Page(items, body.get("cursor", ""))

	def ack(
		self,
		request_id: str,
		step: str,
		outcome: str,
		attempt: int,
		*,
		reason: str | None = None,
		detail: str | None = None,
	) -> datetime:
		body: dict = {"step": step, "outcome": outcome, "attempt": attempt}
		if outcome != "done":
			body["errorClass"] = outcome
		if reason:
			body["reason"] = reason
		if detail:
			body["detail"] = detail
		headers = {"Idempotency-Key": f"{request_id}:{step}:{attempt}"}
		try:
			resp = self._transport.post(_ACK_PATH.format(request_id=request_id), json=body, headers=headers)
		except Exception as exc:
			raise _failure(exc) from exc
		_checked(resp)
		if resp.status == 200:
			return _lease_expiry(resp)
		if resp.status == 409 and _code(resp) == "idempotency_key_conflict":
			raise AttemptCollision(f"{step}:{attempt}")
		if resp.status in (404, 409):
			raise LeaseLost(request_id)
		raise ProtocolError(f"ack {step}: HTTP {resp.status} {_code(resp)}")

	def issue_credential(self, request_id: str) -> IssuedCredential:
		try:  # the call takes no body and no Idempotency-Key (a replay would have to store the secret)
			resp = self._transport.post(_CREDENTIAL_PATH.format(request_id=request_id), json=None, headers={})
		except Exception as exc:
			raise _failure(exc) from exc
		_checked(resp)
		if resp.status == 201:
			b = resp.body
			try:
				return IssuedCredential(
					b["credentialId"],
					b["registrationId"],
					b["issuedAt"],
					b["expiresAt"],
					bool(b["rotated"]),
					b["secret"],
				)
			except (KeyError, TypeError, AttributeError) as exc:
				raise ProtocolError(
					f"credential: the response is incomplete ({type(exc).__name__})"
				) from None
		if resp.status == 404:
			raise CredentialRefused(request_id)
		raise ProtocolError(f"credential: HTTP {resp.status}")

	def complete(
		self,
		request_id: str,
		outcome: str,
		attempt: int,
		*,
		reason: str | None = None,
		result: dict | None = None,
	) -> None:
		body: dict = {"outcome": outcome}
		if reason:
			body["reason"] = reason
		if result is not None:
			body["result"] = result
		headers = {"Idempotency-Key": f"{request_id}:outcome:{attempt}"}
		try:
			resp = self._transport.post(
				_OUTCOME_PATH.format(request_id=request_id), json=body, headers=headers
			)
		except Exception as exc:
			raise _failure(exc) from exc
		_checked(resp)
		if resp.status == 200:
			return
		if resp.status == 409 and _code(resp) == "idempotency_key_conflict":
			raise AttemptCollision(f"outcome:{attempt}")
		if resp.status in (404, 409):
			raise LeaseLost(request_id)
		raise ProtocolError(f"outcome: HTTP {resp.status}")
