# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""The step engine: one request, one site. Every step has a CHECK (is it already done on the site?)
and an APPLY; after a crash or a lost acknowledgement the engine re-reads the site and continues.
Backend-Core's recorded step is a display and audit trail, never the source of truth.

Recovery (PV0, RT-1355): a kill during new-site, install-erpnext or install-connector leaves a site
that is not repaired; it is dropped and recreated, which is safe until the credential is bound
(nothing can post before then). A kill at any later step resumes in place.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256

from ..connector.posting.compatibility_profile import SUPPORTED_MAJORS
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
from .feed_client import FeedClient, ProvisioningRequest
from .gateway import SiteGateway, SiteUnreadable
from .lease import AckSession
from .template import FISCAL_READINESS, TemplateSpec, locale_matches, spec_for

LOG = logging.getLogger("rt.provisioner")
_SITE_NAME = re.compile(r"^t[0-9a-f]{20}[a-z0-9.-]*$")  # the derived prefix, then Frappe-safe characters
_MAX_SITE_NAME = 63  # a DNS label; also keeps every path and argument short
_MAX_NAME = 140  # ERPNext Company and Warehouse name length
_WAREHOUSE_SUFFIX = " - RT"
_DIRTY_STEPS = frozenset({"install_erpnext", "install_connector"})
_CONNECTOR_STEP = "install_connector"
_PASSWORD_BYTES = 24

SUCCEEDED = "succeeded"
REJECTED = "rejected"
CONFLICT = "conflict"
TRANSIENT = "transient"
CAPACITY = "capacity"
SKIPPED = "skipped"
_PASSTHROUGH = (LeaseLost, LeaseTooShort, AuthRejected, FeedUnavailable, ProtocolError)


def db_name_for(prefix: str, site: str) -> str:
	"""Deterministic, so a replay or a recreate reuses the same database name under the scoped grant."""
	return f"{prefix}_{sha256(site.encode('utf-8')).hexdigest()[:16]}"


def _major(version: str | None) -> int | None:
	match = re.match(r"^(\d+)\.", version or "")
	return int(match.group(1)) if match else None


class ProvisioningEngine:
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
		self._secrets: list[str] = []  # values this run knows and must never echo (exact-match redaction)

	# --- one request -----------------------------------------------------------------------------
	def process(self, request: ProvisioningRequest) -> str:
		if request.kind != "tenant.provision":
			LOG.info("skipping request of kind %s: no handler in this provisioner", request.kind)
			return SKIPPED
		session = AckSession(self._feed, request, clock=self._clock, sleep=self._sleep)
		session.ack("claimed", "done")
		self._secrets = []
		try:
			result = self._run_steps(request, session)
		except _StepFailed as failed:
			return self._fail(session, failed.step, failed.error)
		finally:
			self._secrets = []
		session.complete("succeeded", result=result)
		return SUCCEEDED

	def _run_steps(self, request, session: AckSession) -> dict:
		spec = self._guard(request, "site_created", lambda: self._validate(request))
		steps = (
			("site_created", lambda: self._site_created(request, session, spec)),
			("erp_configured", lambda: self._erp_configured(request, session, spec)),
			("connector_installed", lambda: self._connector_installed(request, session, spec)),
			("credential_bound", lambda: self._credential_bound(request)),
			("verified", lambda: self._verified(request, spec)),
		)
		for name, run in steps:
			self._guard(request, name, run)
			session.ack(name, "done")
		return self._guard(request, "verified", lambda: self._result(request, spec))

	def _guard(self, request, step: str, run):
		try:
			return run()
		except _PASSTHROUGH:
			raise
		except CredentialRefused as exc:
			raise _StepFailed(
				step, ProvisioningError(ErrorClass.dependency, "credential refused", str(exc))
			) from exc
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

	def _fail(self, session: AckSession, step: str, err: ProvisioningError) -> str:
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

	# --- validation and gates --------------------------------------------------------------------
	def _validate(self, request: ProvisioningRequest) -> TemplateSpec:
		spec = spec_for(request.template_id, request.template_version)
		if spec is None:
			raise ProvisioningError(ErrorClass.permanently_rejected, "unknown template version")
		if not locale_matches(spec, request.country, request.currency, request.timezone):
			raise ProvisioningError(ErrorClass.permanently_rejected, "unsupported locale for this template")
		if not request.legal_name:
			raise ProvisioningError(ErrorClass.permanently_rejected, "legal name missing")
		self._validate_lengths(request)
		prefix = "t" + request.tenant_id.replace("-", "")[:20]
		name = request.site_name
		if not (len(name) <= _MAX_SITE_NAME and _SITE_NAME.fullmatch(name) and name.startswith(prefix)):
			raise ProvisioningError(ErrorClass.conflict, "site name is not derived from the tenant id")
		return spec

	@staticmethod
	def _validate_lengths(request: ProvisioningRequest) -> None:
		if len(request.legal_name or "") > _MAX_NAME:
			raise ProvisioningError(
				ErrorClass.permanently_rejected, "legal name longer than the company name limit"
			)
		if any(len(s.display_name) + len(_WAREHOUSE_SUFFIX) > _MAX_NAME for s in request.stores):
			raise ProvisioningError(
				ErrorClass.permanently_rejected, "store name longer than the warehouse name limit"
			)

	def _gates(self, site: str, exists: bool) -> None:
		if self._gw.queue_depth() >= self._c.queue_depth_threshold:
			raise ProvisioningError(ErrorClass.capacity, "queue depth at the threshold")
		if not exists and self._gw.count_sites() >= self._c.max_sites:
			raise ProvisioningError(ErrorClass.capacity, "site-count ceiling reached")

	def _budget(self, session: AckSession) -> None:
		if session.remaining_seconds() < self._c.step_budget_seconds:
			raise LeaseTooShort("lease shorter than the step budget")

	# --- step 1: the site ------------------------------------------------------------------------
	def _site_created(self, request, session: AckSession, spec: TemplateSpec) -> None:
		site = request.site_name
		exists = self._gw.site_exists(site)
		if exists and self._must_recreate(site, request):
			self._gw.drop_site(site)
			exists = False
		self._gates(site, exists)
		if not exists:
			self._budget(session)
			self._gw.write_intent(site, request.tenant_id, request.id)  # so a later drop is provably ours
			throwaway = secrets.token_urlsafe(_PASSWORD_BYTES)
			self._secrets.append(throwaway)
			self._gw.new_site(site, db_name_for(self._c.db_name_prefix, site), throwaway)
			self._gw.site_op(site, "reset_admin_password")  # the argv password is dead from here on
			self._gw.write_marker(site, self._marker(request, spec) | {"rt_admin_reset": True})
			self._gw.clear_intent(site)
			return
		self._refresh_marker(site, request, spec)

	def _marker(self, request, spec: TemplateSpec) -> dict:
		return {
			"rt_tenant_id": request.tenant_id,
			"rt_provisioning_request_id": request.id,
			"rt_template": spec.key,
		}

	def _must_recreate(self, site: str, request) -> bool:
		marker = self._gw.read_marker(site)
		if marker is None:
			intent = self._gw.read_intent(site)
			if intent is None or intent.get("tenant_id") != request.tenant_id:
				raise ProvisioningError(ErrorClass.conflict, "site exists without this tenant's marker")
			apps = self._apps_or_none(site)
			if apps is None or set(apps) <= {"frappe"}:
				return True  # killed during new-site, before the marker: nothing to adopt
			raise ProvisioningError(ErrorClass.conflict, "site exists without this tenant's marker")
		if marker.get("rt_tenant_id") != request.tenant_id:
			raise ProvisioningError(ErrorClass.conflict, "site name exists with another tenant marker")
		if marker.get("rt_step_in_progress") in _DIRTY_STEPS:
			if self._credential_present(site):
				raise ProvisioningError(
					ErrorClass.permanently_rejected,
					"site is dirty after the credential was bound: forward-fix only",
				)
			return True
		return False

	def _apps_or_none(self, site: str) -> list[str] | None:
		try:
			return self._gw.installed_apps(site)
		except SiteUnreadable:
			return None

	def _credential_present(self, site: str) -> bool:
		if (self._gw.read_marker(site) or {}).get("rt_credential_bound"):
			return True  # recorded in the marker file, so this never depends on the database
		try:
			return bool(self._gw.site_op(site, "inspect").get("credential_id"))
		except RuntimeError:  # includes SiteUnreadable
			return False

	def _refresh_marker(self, site: str, request, spec: TemplateSpec) -> None:
		marker = self._gw.read_marker(site) or {}
		if marker.get("rt_template") not in (None, spec.key):
			# A new template version never rewrites an existing site (RT-1352 section 4.6).
			raise ProvisioningError(
				ErrorClass.permanently_rejected, "site was provisioned with another template"
			)
		want = self._marker(request, spec)
		updates = {k: v for k, v in want.items() if marker.get(k) != v}
		if not marker.get("rt_admin_reset"):
			self._gw.site_op(site, "reset_admin_password")
			updates["rt_admin_reset"] = True
		if updates:
			self._gw.write_marker(site, updates)

	# --- step 2: ERPNext, wizard, template -------------------------------------------------------
	def _install(self, site: str, app: str, label: str) -> None:
		self._gw.write_marker(site, {"rt_step_in_progress": label})
		self._gw.install_app(site, app)
		self._gw.write_marker(site, {"rt_step_in_progress": None})

	def _check_versions(self, versions: dict) -> None:
		for name in ("frappe", "erpnext"):
			if _major(versions.get(name)) not in SUPPORTED_MAJORS:
				raise ProvisioningError(ErrorClass.compatibility, f"{name} outside the supported line")

	def _erp_configured(self, request, session: AckSession, spec: TemplateSpec) -> None:
		site = request.site_name
		if "erpnext" not in self._gw.installed_apps(site):
			self._budget(session)
			self._install(site, "erpnext", "install_erpnext")
		state = self._gw.site_op(site, "inspect")
		self._check_versions(state["versions"])
		if len(state["companies"]) > 1:
			raise ProvisioningError(ErrorClass.conflict, "second company on the site")
		if not state["setup_complete"]:
			self._budget(session)
			self._gw.site_op(site, "setup_wizard", self._wizard_args(request, spec))
		if state["template"] != {"id": spec.template_id, "version": spec.version}:
			self._gw.site_op(
				site, "apply_template", {"template_id": spec.template_id, "template_version": spec.version}
			)

	@staticmethod
	def _wizard_args(request, spec: TemplateSpec) -> dict:
		return {
			"country": spec.country,
			"currency": spec.currency,
			"timezone": spec.timezone,
			"language": request.language,
			"company_name": request.legal_name,
			"abbr": spec.abbr,
			"chart_of_accounts": spec.chart_of_accounts,
			"fiscal_year": spec.fiscal_year,
		}

	# --- step 3: Connector, scheduler, store maps ------------------------------------------------
	def _connector_installed(self, request, session: AckSession, spec: TemplateSpec) -> None:
		site = request.site_name
		if self._c.connector_app not in self._gw.installed_apps(site):
			self._budget(session)
			self._install(site, self._c.connector_app, _CONNECTOR_STEP)
		state = self._gw.site_op(site, "inspect")
		if not state["indexes_ok"]:
			raise ProvisioningError(ErrorClass.permanently_rejected, "exactly-once indexes are missing")
		if not state["scheduler_enabled"]:
			self._gw.site_op(site, "enable_scheduler")
		known = state["stores"]
		# Independent of the stores: a tenant unlocked before its first store exists still needs the
		# cash tender and the posting currency (posting stays paused without the currency).
		needs_site_maps = "cash" not in state.get("tender_methods", []) or not state.get("posting_currency")
		if needs_site_maps or any(s.store_id not in known for s in request.stores):
			stores = [{"storeId": s.store_id, "displayName": s.display_name} for s in request.stores]
			self._gw.site_op(
				site,
				"bootstrap_stores",
				{
					"stores": stores,
					"walk_in_customer": spec.walk_in_customer,
					"cash_mode_of_payment": spec.cash_mode_of_payment,
				},
			)

	# --- step 4: the credential ------------------------------------------------------------------
	def _credential_bound(self, request) -> None:
		site = request.site_name
		state = self._gw.site_op(site, "inspect")
		if state.get("credential_id") and state.get("registration_id") == request.registration_id:
			return  # a stored credential id of THIS registration means the step is done
		cred = self._feed.issue_credential(request.id)  # a repeat for an unpresented one is a rotation
		self._secrets.append(cred.secret)
		if cred.registration_id != request.registration_id:
			raise ProvisioningError(ErrorClass.dependency, "credential is for another registration")
		self._gw.site_op(
			site,
			"bind_credential",
			{
				"credential_id": cred.credential_id,
				"registration_id": cred.registration_id,
				"issued_at": cred.issued_at,
				"expires_at": cred.expires_at,
				"base_url": self._c.effective_connector_base_url,
			},
			secret=cred.secret,
		)
		self._gw.write_marker(site, {"rt_credential_bound": True})

	# --- step 5: verify (site-side only; Backend-Core judges the heartbeat) ----------------------
	def _verified(self, request, spec: TemplateSpec) -> None:
		state = self._gw.site_op(request.site_name, "inspect")
		self._check_versions(state["versions"])
		if len(state["companies"]) > 1:
			raise ProvisioningError(ErrorClass.conflict, "second company on the site")
		problems = self._verification_problems(state, request, spec)
		if problems:
			raise ProvisioningError(
				ErrorClass.failed_transient, "verification incomplete", ", ".join(problems)
			)

	@staticmethod
	def _verification_problems(state: dict, request, spec: TemplateSpec) -> list[str]:
		checks = {
			"company": len(state["companies"]) == 1 and bool(state["default_company"]),
			"template": state["template"] == {"id": spec.template_id, "version": spec.version},
			"indexes_ok": bool(state["indexes_ok"]),
			"scheduler_enabled": bool(state["scheduler_enabled"]),
			"credential_id": bool(state["credential_id"]),
			"registration": state.get("registration_id") == request.registration_id,
			"cash tender": "cash" in state.get("tender_methods", []),
			"posting currency": bool(state.get("posting_currency")),
			"store maps": all(s.store_id in state["stores"] for s in request.stores),
		}
		return [name for name, ok in checks.items() if not ok]

	def _result(self, request, spec: TemplateSpec) -> dict:
		state = self._gw.site_op(request.site_name, "inspect")
		versions = state["versions"]
		return {
			"siteName": request.site_name,
			"companyRef": state["default_company"],
			"templateVersion": spec.version,
			"erpnextVersion": versions.get("erpnext"),
			"frappeVersion": versions.get("frappe"),
			"connectorVersion": versions.get("connector"),
			"fiscalReadiness": FISCAL_READINESS,
			"stores": [
				{
					"storeId": s.store_id,
					"warehouseRef": state["stores"][s.store_id]["warehouse"],
					"customerRef": state["stores"][s.store_id]["customer"],
				}
				for s in request.stores
			],
		}


class _StepFailed(Exception):
	def __init__(self, step: str, error: ProvisioningError) -> None:
		self.step = step
		self.error = error
		super().__init__(str(error))
