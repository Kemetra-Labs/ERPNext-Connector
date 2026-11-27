# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1362 S3 — the ``credential.rotate`` handler (``provisioning-feed.yaml`` 1.2.0-draft, "Credential
rotation", OVERLAP per owner decision RT-1362 c19107).

One completion sequence, on the site named by ``erpnextSiteRef`` (never ``siteName``, ``template`` or
``locale``, which are compatibility fields on this kind):

1. ``claimed``, keeping the returned ``leaseId``; every later call sends it as ``Provisioning-Lease-Id``.
2. SITE CHECK before anything is issued: the site exists on this host and its Connector Settings name
   ``connectorRegistrationId``. A missing site or another registration is ``conflict``; no credential
   call is made. The RT site marker is not required: an adopted site (RT-1557) has none.
3. The credential call: a PENDING credential; the current one keeps working.
4. A fenced compare-and-set write into the PENDING slot of Connector Settings (never CURRENT), after
   checking that the lease has time left. ``stale`` (a newer worker staged a later credential) and a
   lease too short write nothing and report nothing: stop and re-pull.
5. ``credential_bound``.
6. The Connector's next authenticated call, made now (one heartbeat): it presents PENDING, Backend-Core
   accepts it (promoting it and revoking the old one server-side), and the Connector promotes it locally.
7. ``verified``. No ``succeeded`` outcome is sent for this kind: Backend-Core completes the rotation when
   it accepts the pending credential.

Recovery: a lost credential response or a failed write is recovered by repeating the credential call
(a repeat revokes the unpresented pending credential and issues a new one), then writing again. On this
kind a ``404`` on the credential call and a ``409 lease_lost`` on a step mean "stop and re-pull", never
a ``dependency`` failure. A request missing from a pull is not proof that it completed.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime

from .config import ProvisionerConfig
from .errors import (
	AuthRejected,
	CredentialRefused,
	ErrorClass,
	FeedUnavailable,
	LeaseLost,
	LeaseTooShort,
	ProtocolError,
	ProvisioningError,
	scrub,
)
from .feed_client import FeedClient, IssuedCredential, ProvisioningRequest, parse_time
from .gateway import SiteGateway
from .lease import AckSession
from .outcomes import VERIFIED, report_failure

LOG = logging.getLogger("rt.provisioner")
ROTATE_KIND = "credential.rotate"
_BIND_STEP = "credential_bound"
_VERIFY_STEP = "verified"
# A Frappe site directory name: letters, digits, dot, hyphen, underscore; no path separator, no "..".
_SITE_REF = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,253}[A-Za-z0-9])?$")
_TRIES = 3
_BACKOFF_S = (1, 2, 4)
_STAGED = frozenset({"write", "noop", "stale", "expired", "deferred"})
_PASSTHROUGH = (LeaseLost, LeaseTooShort, AuthRejected, FeedUnavailable, ProtocolError)


class CredentialRotator:
	def __init__(
		self,
		gateway: SiteGateway,
		feed: FeedClient,
		config: ProvisionerConfig,
		*,
		clock: Callable[[], datetime] = lambda: datetime.now(UTC),
		sleep: Callable[[float], None] = time.sleep,
	) -> None:
		self._gw = gateway
		self._feed = feed
		self._c = config
		self._clock = clock
		self._sleep = sleep
		self._secrets: list[str] = []

	def process(self, request: ProvisioningRequest) -> str:
		session = AckSession(self._feed, request, clock=self._clock, sleep=self._sleep)
		session.ack("claimed", "done")
		if session.lease_id is None:
			# Without it every later call is refused (409/404): nothing can be fenced, so nothing is done.
			raise ProtocolError("claimed: no leaseId on a credential.rotate request")
		self._secrets = []
		try:
			site = self._guard(_BIND_STEP, lambda: self._site_check(request))
			cred = self._guard(_BIND_STEP, lambda: self._bind_pending(request, session, site))
			session.ack(_BIND_STEP, "done")
			self._guard(_VERIFY_STEP, lambda: self._confirm(site, cred))
			session.ack(_VERIFY_STEP, "done")
		except _StepFailed as failed:
			return report_failure(session, failed.step, failed.error)
		finally:
			self._secrets = []
		LOG.info("credential.rotate %s: the pending credential is accepted and promoted", request.id)
		return VERIFIED

	def _guard(self, step: str, run):
		try:
			return run()
		except _PASSTHROUGH:
			raise
		except CredentialRefused as exc:
			# On this kind the non-disclosing 404 means the lease or the request moved on (a superseded
			# claim, a finished rotation): stop and re-pull, never a dependency failure.
			raise LeaseLost("credential call refused") from exc
		except ProvisioningError as exc:
			raise _StepFailed(step, exc) from exc
		except Exception as exc:
			raise _StepFailed(
				step,
				ProvisioningError(
					ErrorClass.failed_transient,
					f"{step} failed",
					scrub(f"{type(exc).__name__}: {exc}", 1000, tuple(self._secrets)),
				),
			) from exc

	# --- step 2: the site check ------------------------------------------------------------------
	@staticmethod
	def _site_ref(request: ProvisioningRequest) -> str:
		ref = request.erpnext_site_ref
		if not isinstance(ref, str) or not _SITE_REF.fullmatch(ref) or ".." in ref:
			raise ProvisioningError(ErrorClass.permanently_rejected, "erpnextSiteRef is not a site name")
		return ref

	def _site_check(self, request: ProvisioningRequest) -> str:
		site = self._site_ref(request)
		if not self._gw.site_exists(site):
			raise ProvisioningError(
				ErrorClass.conflict, "the site named by erpnextSiteRef is not on this host"
			)
		state = self._gw.site_op(site, "inspect")  # no registration id without the Connector installed
		if state.get("registration_id") != request.registration_id:
			raise ProvisioningError(ErrorClass.conflict, "Connector Settings name another registration")
		if not state.get("pending_slot"):
			# A Connector release without the pending slot (or not migrated): issuing would only mint a
			# credential Backend-Core must then revoke. Checked before the credential call.
			raise ProvisioningError(
				ErrorClass.compatibility, "Connector Settings have no pending credential slot"
			)
		return site

	# --- steps 3 and 4: issue, then the fenced write (repeated after a lost response or a failed write)
	def _bind_pending(self, request: ProvisioningRequest, session: AckSession, site: str) -> IssuedCredential:
		for attempt in range(1, _TRIES + 1):
			last = attempt == _TRIES
			try:
				cred = self._issue(request, session)
			except FeedUnavailable:
				if last:
					raise
				self._sleep(_BACKOFF_S[attempt - 1])  # a lost response: the repeat replaces that credential
				continue
			try:
				staged = self._stage(site, request, session, cred)
			except RuntimeError:  # a failed write (bench, site): repeat the call, then write again
				if last:
					raise
				self._sleep(_BACKOFF_S[attempt - 1])
				continue
			if staged == "stale":
				raise LeaseLost("a later credential is already staged: a newer worker owns the rotation")
			if staged == "expired":
				raise LeaseTooShort("the lease ended before the pending-slot write: nothing written")
			if staged == "deferred":
				raise LeaseLost("the staged credential could not be settled: nothing written, re-pull")
			return cred
		raise AssertionError("unreachable")

	def _issue(self, request: ProvisioningRequest, session: AckSession) -> IssuedCredential:
		cred = session.issue_credential()
		self._secrets.append(cred.secret)
		if cred.registration_id != request.registration_id:
			raise ProvisioningError(ErrorClass.conflict, "credential is for another registration")
		try:
			issued = parse_time(cred.issued_at)
		except (TypeError, ValueError, AttributeError) as exc:
			raise ProtocolError("credential: issuedAt is not a time") from exc
		if issued.tzinfo is None:
			raise ProtocolError("credential: issuedAt has no time zone")
		return cred

	def _stage(self, site: str, request: ProvisioningRequest, session: AckSession, cred) -> str:
		"""The fenced write. The site process gets the lease's absolute end as ``deadline`` and checks it
		under the Connector Settings lock right before it changes anything (its lock wait is bounded by
		it), and the bench call is killed when the lease ends: a stalled write never lands after it."""
		left = session.remaining_seconds()
		if left < self._c.short_timeout_seconds or session.lease_expires_at is None:
			raise LeaseTooShort("lease too short for the pending-slot write")  # write nothing, re-pull
		deadline = session.lease_expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
		try:
			result = self._gw.site_op(
				site,
				"stage_pending_credential",
				{
					"registration_id": request.registration_id,
					"credential_id": cred.credential_id,
					"issued_at": cred.issued_at,
					"expires_at": cred.expires_at,
					"deadline": deadline,
				},
				secret=cred.secret,
				timeout=int(left),
			)
		except TimeoutError as exc:  # killed at the lease end: whatever it did not finish is not written
			raise LeaseTooShort("the pending-slot write ran into the lease end") from exc
		staged = result.get("staged")
		if staged not in _STAGED:
			raise RuntimeError("the pending-slot write returned no result")
		return staged

	# --- step 6: the Connector's next authenticated call -----------------------------------------
	def _confirm(self, site: str, cred: IssuedCredential) -> None:
		for attempt in range(1, _TRIES + 1):
			ids = self._gw.site_op(site, "confirm_credential", {"credential_id": cred.credential_id})
			current, pending = ids.get("current_credential_id"), ids.get("pending_credential_id")
			if current == cred.credential_id and pending != cred.credential_id:
				return
			if pending and pending != cred.credential_id:
				raise LeaseLost("another pending credential is staged: a newer worker owns the rotation")
			if attempt < _TRIES:
				self._sleep(_BACKOFF_S[attempt - 1])
		raise ProvisioningError(
			ErrorClass.failed_transient, "the pending credential was not accepted by Backend-Core yet"
		)


class _StepFailed(Exception):
	def __init__(self, step: str, error: ProvisioningError) -> None:
		self.step = step
		self.error = error
		super().__init__(str(error))
