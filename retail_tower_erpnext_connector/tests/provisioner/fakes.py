# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Test doubles for the tenant provisioner (RT-1359): a fake Backend-Core feed that follows
``provisioning-feed.yaml`` (pull, lease, ack idempotency, credential, outcome), a fake bench
(:class:`FakeGateway`) holding simulated site state, a fake clock and a crash plan that kills the
run at every mutating call. No bench, no network, no frappe."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from retail_tower_erpnext_connector.provisioner.feed_client import Response
from retail_tower_erpnext_connector.provisioner.gateway import SiteUnreadable

LEASE_S = 900
SECRET = "-".join(["S3cr3t", "credential", "value", "9f8e7d6c5b4a"])  # built, not a literal
TENANT = "0b9a3f0e-6c5d-4e2a-8d52-3a6f1b9c7d10"
SITE = "t0b9a3f0e6c5d4e2a8d52-pilot"
REQUEST_ID = "7b0f3c1e-3f1a-4a52-9c1c-0a8f6d2f6a11"
OTHER_TENANT = "11111111-2222-4333-8444-555555555555"
STORE_A = "5f1e2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5b"
STORE_B = "6a1e2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5c"


class SimulatedCrash(BaseException):
	"""A process kill: not an Exception, so no handler in the engine can swallow it."""


class FakeClock:
	def __init__(self) -> None:
		self.now = datetime(2026, 11, 14, 10, 0, 0, tzinfo=UTC)

	def __call__(self) -> datetime:
		return self.now

	def advance(self, seconds: float) -> None:
		self.now += timedelta(seconds=seconds)


class CrashPlan:
	"""Records every mutating event (feed ack, gateway change, mid-operation point) in order and
	raises :class:`SimulatedCrash` at event number ``crash_at``."""

	def __init__(self, crash_at: int | None = None) -> None:
		self.crash_at = crash_at
		self.trace: list[str] = []

	def hit(self, label: str, phase: str = "before") -> None:
		self.trace.append(f"{label}:{phase}")
		if self.crash_at is not None and len(self.trace) - 1 == self.crash_at:
			raise SimulatedCrash(f"{label}:{phase}")


@dataclass
class FakeRequestState:
	wire: dict
	status: str = "requested"
	lease_expires: datetime | None = None
	max_attempt: int = 0
	acks: dict[str, tuple[str, dict]] = field(default_factory=dict)
	steps: list[tuple[str, str]] = field(default_factory=list)
	terminal: str | None = None
	terminal_body: dict | None = None
	credentials_issued: int = 0
	credential_presented: bool = False


def make_wire(**over) -> dict:
	base = {
		"provisioningRequestId": REQUEST_ID,
		"kind": "tenant.provision",
		"tenantId": TENANT,
		"generation": 1,
		"siteName": SITE,
		"template": {"templateId": "egypt-retail", "templateVersion": 1},
		"locale": {"country": "EG", "currency": "EGP", "timezone": "Africa/Cairo", "language": "ar"},
		"legalName": "Example Trading Co",
		"connectorRegistrationId": "3d2c7f60-91a4-4c0b-9f3e-6f1c2a8b4e55",
		"environment": "pilot",
		"attemptFloor": 0,
		"stores": [
			{"storeId": STORE_A, "displayName": "Main Store"},
			{"storeId": STORE_B, "displayName": "Second Store"},
		],
	}
	base.update(over)
	return base


class HttpStatusError(OSError):
	"""What the shared transport's GET raises for an error status (``requests`` HTTPError shape)."""

	def __init__(self, status: int) -> None:
		super().__init__(f"HTTP {status}")

		class _Resp:
			status_code = status

		self.response = _Resp()


class FakeFeed:
	"""The contract's four feed operations, as a ``FeedTransport``."""

	def __init__(self, clock: FakeClock, plan: CrashPlan | None = None, token: str = "prov-token") -> None:
		self.clock = clock
		self.plan = plan or CrashPlan()
		self.token = token
		self.requests: dict[str, FakeRequestState] = {}
		self.calls: list[tuple[str, str, dict]] = []
		self.fail_next: list[object] = []  # statuses (int) or Exception to raise on the next calls
		self.credential_status: int | None = None  # force a status on the credential call
		self.bearer_in_use = token

	def add(self, **over) -> FakeRequestState:
		wire = make_wire(**over)
		state = FakeRequestState(wire=wire)
		self.requests[wire["provisioningRequestId"]] = state
		return state

	# --- the shared HttpTransport surface (GET returns the body, POST a status-aware response) ----
	def get(self, path, *, params, headers):
		resp = self.request("GET", path, params=params, headers=headers)
		if resp.status >= 400:
			raise HttpStatusError(resp.status)
		return resp.body

	def post(self, path, *, json, headers):
		return self.request("POST", path, json=json, headers=headers)

	# --- transport -------------------------------------------------------------------------------
	def request(self, method, path, *, params=None, json=None, headers=None) -> Response:
		headers = headers or {}
		self.calls.append((method, path, {"params": params, "json": json, "headers": dict(headers)}))
		if self.fail_next:
			nxt = self.fail_next.pop(0)
			if isinstance(nxt, BaseException):
				raise nxt
			return Response(int(nxt), {}, {"error": {"code": "internal_error", "message": "x"}})
		if self.bearer_in_use != self.token:
			return _err(401, "unauthorized")
		if path.endswith("/requests") and method == "GET":
			return self._pull(params or {})
		rid = path.split("/requests/")[1].split("/")[0]
		state = self.requests.get(rid)
		if state is None:
			return _err(404, "not_found")
		if path.endswith("/steps"):
			return self._ack(state, json or {}, headers)
		if path.endswith("/credential"):
			return self._credential(state)
		if path.endswith("/outcome"):
			return self._outcome(state, json or {}, headers)
		return _err(404, "not_found")

	# --- operations ------------------------------------------------------------------------------
	def _offerable(self, s: FakeRequestState) -> bool:
		if s.terminal:
			return False
		return s.lease_expires is None or s.lease_expires <= self.clock()

	def _live(self, s: FakeRequestState) -> bool:
		return s.lease_expires is not None and s.lease_expires > self.clock() and not s.terminal

	def _pull(self, params: dict) -> Response:
		items = [
			dict(s.wire, attemptFloor=s.max_attempt) for s in self.requests.values() if self._offerable(s)
		]
		cursor = str(params.get("cursor") or "0")
		start = int(cursor) if cursor.isdigit() else 0
		page = items[start : start + int(params.get("limit", 20))]
		return Response(200, {}, {"items": page, "cursor": str(start + len(page))})

	def _ack(self, s: FakeRequestState, body: dict, headers: dict) -> Response:
		self.plan.hit(f"feed.ack:{body.get('step')}")
		key = headers.get("Idempotency-Key", "")
		digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
		if key in s.acks:
			stored_digest, result = s.acks[key]
			if stored_digest != digest:
				return _err(409, "idempotency_key_conflict")
			return Response(200, {"Idempotent-Replayed": "true"}, result)
		attempt = body["attempt"]
		if attempt <= s.max_attempt:
			return _err(409, "idempotency_key_conflict")
		claiming = body["step"] == "claimed"
		if not self._live(s) and not (claiming and self._offerable(s)):
			return _err(409, "lease_lost")
		s.max_attempt = attempt
		s.lease_expires = self.clock() + timedelta(seconds=LEASE_S)
		s.steps.append((body["step"], body["outcome"]))
		result = {
			"requestId": s.wire["provisioningRequestId"],
			"step": body["step"],
			"leaseExpiresAt": s.lease_expires.isoformat().replace("+00:00", "Z"),
		}
		s.acks[key] = (digest, result)
		self.plan.hit(f"feed.ack:{body.get('step')}", "after")
		return Response(200, {}, result)

	def _credential(self, s: FakeRequestState) -> Response:
		self.plan.hit("feed.credential")
		if self.credential_status:
			return _err(self.credential_status, "not_found")
		if not self._live(s) or s.credential_presented:
			return _err(404, "not_found")
		rotated = s.credentials_issued > 0
		s.credentials_issued += 1
		self.plan.hit("feed.credential", "after")
		return Response(
			201,
			{"Cache-Control": "no-store"},
			{
				"credentialId": f"cred-{s.credentials_issued}",
				"registrationId": s.wire["connectorRegistrationId"],
				"secret": SECRET,
				"issuedAt": self.clock().isoformat().replace("+00:00", "Z"),
				"expiresAt": (self.clock() + timedelta(days=365)).isoformat().replace("+00:00", "Z"),
				"rotated": rotated,
			},
		)

	def _outcome(self, s: FakeRequestState, body: dict, headers: dict) -> Response:
		self.plan.hit("feed.outcome")
		if not self._live(s) and not s.terminal:
			return _err(409, "lease_lost")
		s.terminal = body["outcome"]
		s.terminal_body = body
		s.status = body["outcome"]
		self.plan.hit("feed.outcome", "after")
		return Response(200, {}, {"requestId": s.wire["provisioningRequestId"], "outcome": body["outcome"]})


def _err(status: int, code: str) -> Response:
	return Response(status, {}, {"error": {"code": code, "message": "x"}})


class FakeGateway:
	"""A bench: simulated sites. Records every call (argv-equivalents) for the secret assertions."""

	DIRTY_MID = ("new_site", "install_app")

	def __init__(self, plan: CrashPlan | None = None) -> None:
		self.plan = plan or CrashPlan()
		self.sites: dict[str, dict] = {}
		self.intents: dict[str, dict] = {}
		self.calls: list[tuple] = []
		self.stdin_payloads: list[dict] = []
		self.admin_passwords: list[str] = []
		self.queue = 0
		self.drop_fails = False
		self.fail_op: dict[str, Exception] = {}
		self.versions = {"frappe": "16.52.0", "erpnext": "16.51.0", "connector": "1.0.0"}
		self.site_limit_count: int | None = None

	# --- helpers ---------------------------------------------------------------------------------
	def _mut(self, label: str) -> None:
		self.plan.hit(label)

	def _mid(self, label: str) -> None:
		self.plan.hit(label, "mid")

	def seed_foreign(self, site: str, tenant: str = OTHER_TENANT, apps=("frappe", "erpnext")) -> None:
		self.sites[site] = {"marker": {"rt_tenant_id": tenant}, "apps": list(apps), **_blank_site()}

	# --- gateway protocol ------------------------------------------------------------------------
	def site_exists(self, site: str) -> bool:
		return site in self.sites

	def read_marker(self, site: str) -> dict | None:
		s = self.sites.get(site)
		return dict(s["marker"]) if s and s["marker"] is not None else None

	def write_marker(self, site: str, updates: dict) -> None:
		self._mut(f"write_marker:{sorted(updates)}")
		self.calls.append(("write_marker", site, dict(updates)))
		marker = self.sites[site].setdefault("marker", {}) or {}
		self.sites[site]["marker"] = marker
		for k, v in updates.items():
			if v is None:
				marker.pop(k, None)
			else:
				marker[k] = v

	def read_intent(self, site: str) -> dict | None:
		return self.intents.get(site)

	def clear_intent(self, site: str) -> None:
		self._mut("clear_intent")
		self.calls.append(("clear_intent", site))
		self.intents.pop(site, None)

	def write_intent(self, site: str, tenant_id: str, request_id: str) -> None:
		self._mut("write_intent")
		self.calls.append(("write_intent", site, tenant_id))
		self.intents[site] = {"tenant_id": tenant_id, "request_id": request_id}

	def installed_apps(self, site: str) -> list[str]:
		s = self.sites[site]
		if s.get("broken"):
			raise SiteUnreadable("database incomplete")
		return list(s["apps"])

	def new_site(self, site: str, db_name: str, admin_password: str) -> None:
		self._mut("new_site")
		self.calls.append(("new_site", site, db_name))
		self.admin_passwords.append(admin_password)
		if site in self.sites:
			raise RuntimeError("site exists")
		self.sites[site] = {"marker": None, "apps": ["frappe"], "db_name": db_name, **_blank_site()}
		self.sites[site]["broken"] = True  # a kill here leaves a half-created site, no marker
		self._mid("new_site")
		self.sites[site]["broken"] = False

	def drop_site(self, site: str) -> None:
		self._mut("drop_site")
		self.calls.append(("drop_site", site))
		if self.drop_fails:
			raise RuntimeError("bench drop-site failed (exit 1)")
		self.sites.pop(site, None)

	def install_app(self, site: str, app: str) -> None:
		self._mut(f"install_app:{app}")
		self.calls.append(("install_app", site, app))
		s = self.sites[site]
		s["broken"] = True  # a kill here leaves a half-installed app
		self._mid(f"install_app:{app}")
		s["broken"] = False
		s["apps"].append(app)
		if app == "retail_tower_erpnext_connector":
			s["indexes_ok"] = True
			s["has_settings"] = True

	def site_op(self, site: str, op: str, args: dict | None = None, secret: str | None = None) -> dict:
		args = args or {}
		self.calls.append(("site_op", site, op, dict(args)))
		self.stdin_payloads.append({"op": op, "args": dict(args), "secret": secret})
		if op in self.fail_op:
			raise self.fail_op[op]
		s = self.sites[site]
		if op == "inspect":
			return self._inspect(s)
		self._mut(f"site_op:{op}")
		self._apply_op(s, op, args, secret)
		return {"ok": True}

	@staticmethod
	def _apply_op(s: dict, op: str, args: dict, secret: str | None) -> None:
		if op == "reset_admin_password":
			s["admin_reset"] = True
		elif op == "setup_wizard":
			s.update(
				setup_complete=True, companies=[args["company_name"]], default_company=args["company_name"]
			)
		elif op == "apply_template":
			s["template"] = {"id": args["template_id"], "version": args["template_version"]}
		elif op == "enable_scheduler":
			s["scheduler"] = True
		elif op == "bootstrap_stores":
			assert args.get("cash_mode_of_payment") == "Cash"
			s["tenders"] = ["cash"]
			s["posting_currency"] = s.get("posting_currency") or "EGP"
			for st in args["stores"]:
				s["stores"][st["storeId"]] = {
					"warehouse": f"{st['displayName']} - RT",
					"customer": "Retail Walk-in",
				}
		elif op == "bind_credential":
			if not secret:
				raise RuntimeError("bind without secret")
			s.update(
				credential_id=args["credential_id"], registration_id=args["registration_id"], token_set=True
			)
		else:
			raise AssertionError(f"unknown op {op}")

	def _inspect(self, s: dict) -> dict:
		if s.get("broken"):
			raise SiteUnreadable("database incomplete")
		return {
			"setup_complete": s["setup_complete"],
			"companies": list(s["companies"]),
			"default_company": s["default_company"],
			"template": s["template"],
			"scheduler_enabled": s["scheduler"],
			"credential_id": s["credential_id"],
			"registration_id": s.get("registration_id"),
			"indexes_ok": s["indexes_ok"],
			"versions": dict(self.versions),
			"stores": dict(s["stores"]),
			"tender_methods": list(s.get("tenders", [])),
			"posting_currency": s.get("posting_currency"),
		}

	def count_sites(self) -> int:
		return self.site_limit_count if self.site_limit_count is not None else len(self.sites)

	def queue_depth(self) -> int:
		return self.queue


def _blank_site() -> dict:
	return {
		"setup_complete": False,
		"companies": [],
		"default_company": None,
		"template": None,
		"scheduler": False,
		"credential_id": None,
		"indexes_ok": False,
		"has_settings": False,
		"admin_reset": False,
		"token_set": False,
		"stores": {},
	}
