# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""The bench as the engine sees it. :class:`BenchGateway` is the real one: ``bench`` subprocesses
through an argument-list wrapper, the site marker as keys in ``sites/<site>/site_config.json``, and
site-side work through the registered bench command with a JSON spec on stdin.

Database login: ``bench new-site`` and ``drop-site`` read ``root_login`` / ``root_password`` from the
bench's ``common_site_config.json`` when not given on the command line (Frappe v16.52.0
``frappe/database/mariadb/setup_db.py`` ``get_root_connection``, lines 124-140). RT-1360 provisions
that file for a dedicated, least-privilege MariaDB user (OD-3); nothing here carries or passes it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from .bench import BenchRunner
from .config import ProvisionerConfig
from .errors import ErrorClass, ProvisioningError

LOG = logging.getLogger("rt.provisioner")
SITE_COMMAND = "rt-provision-site-step"
QUEUE_COMMAND = "rt-provision-queue-depth"
RESULT_PREFIX = "RTPROV:"
MARKER_KEYS = (
	"rt_tenant_id",
	"rt_provisioning_request_id",
	"rt_template",
	"rt_template_applied",
	"rt_step_in_progress",
	"rt_admin_reset",
	"rt_credential_bound",
)


class SiteUnreadable(RuntimeError):
	"""The site exists but its database cannot be read (a half-created or half-installed site)."""


class SiteGateway(Protocol):
	def site_exists(self, site: str) -> bool: ...
	def read_marker(self, site: str) -> dict | None: ...
	def write_marker(self, site: str, updates: dict) -> None: ...
	def read_intent(self, site: str) -> dict | None: ...
	def write_intent(self, site: str, tenant_id: str, request_id: str) -> None: ...
	def clear_intent(self, site: str) -> None: ...
	def installed_apps(self, site: str) -> list[str]: ...
	def new_site(self, site: str, db_name: str, admin_password: str) -> None: ...
	def drop_site(self, site: str) -> None: ...
	def install_app(self, site: str, app: str) -> None: ...
	def site_op(self, site: str, op: str, args: dict | None = None, secret: str | None = None) -> dict: ...
	def count_sites(self) -> int: ...
	def queue_depth(self) -> int: ...


class BenchGateway:
	def __init__(self, config: ProvisionerConfig, runner: BenchRunner) -> None:
		self._c = config
		self._runner = runner
		self._sites = Path(config.bench_path) / "sites"

	# --- bench commands --------------------------------------------------------------------------
	def _bench(self, label: str, args: list[str], timeout: int, stdin: bytes | None = None):
		"""Run one bench command. ``label`` is a fixed name chosen by the caller, so nothing that
		reaches the log is derived from the argument list (new-site's carries the throwaway password).
		Neither the output nor the arguments are logged or put into the exception: the output can hold
		tenant business content (a Company or Warehouse name in an error) and the echo of the arguments."""
		argv = [self._c.bench_executable, *args]
		res = self._runner.run(argv, stdin=stdin, timeout=timeout, cwd=self._c.bench_path)
		if res.returncode != 0:
			LOG.warning("bench %s failed", label)
			raise RuntimeError(f"bench {label} failed (exit {res.returncode})")
		return res

	def new_site(self, site: str, db_name: str, admin_password: str) -> None:
		# The Administrator password is a one-use throwaway (owner decision 2026-11-13): `new-site`
		# accepts it only here, so the first site-side step replaces it with a value never stored.
		self._bench(
			"new-site",
			["new-site", "--db-name", db_name, "--admin-password", admin_password, site],
			self._c.new_site_timeout_seconds,
		)

	def drop_site(self, site: str) -> None:
		self._bench(
			"drop-site", ["drop-site", site, "--no-backup", "--force"], self._c.new_site_timeout_seconds
		)

	def install_app(self, site: str, app: str) -> None:
		self._bench("install-app", ["--site", site, "install-app", app], self._c.install_timeout_seconds)

	def _command_json(self, label: str, args: list[str], timeout: int, stdin: bytes | None = None) -> dict:
		res = self._bench(label, args, timeout, stdin)
		for line in reversed(res.stdout.splitlines()):
			if line.startswith(RESULT_PREFIX):
				return self._checked(json.loads(line[len(RESULT_PREFIX) :]))
		raise RuntimeError("bench command printed no result line")

	@staticmethod
	def _checked(result: dict) -> dict:
		error = result.get("error")
		if error:  # a structured failure from the site process: it carries a contract error class
			raise ProvisioningError(ErrorClass(error["class"]), str(error["reason"]))
		return result

	def site_op(self, site: str, op: str, args: dict | None = None, secret: str | None = None) -> dict:
		spec: dict = {"op": op, "args": args or {}}
		if secret is not None:
			spec["secret"] = secret  # the only channel: this pipe, into the site process
		timeout = self._c.short_timeout_seconds if op == "inspect" else self._c.site_step_timeout_seconds
		return self._command_json(
			SITE_COMMAND, ["--site", site, SITE_COMMAND], timeout, json.dumps(spec).encode("utf-8")
		)

	def installed_apps(self, site: str) -> list[str]:
		try:
			return list(self.site_op(site, "inspect")["apps"])
		except RuntimeError as exc:
			raise SiteUnreadable(str(exc)) from exc

	def queue_depth(self) -> int:
		return int(self._command_json(QUEUE_COMMAND, [QUEUE_COMMAND], self._c.short_timeout_seconds)["depth"])

	# --- the site marker and the site list (plain files; no frappe, no database) -----------------
	def _config_path(self, site: str) -> Path:
		return self._sites / site / "site_config.json"

	def site_exists(self, site: str) -> bool:
		return (self._sites / site).is_dir()

	def _read_config(self, site: str) -> dict:
		try:
			return json.loads(self._config_path(site).read_text(encoding="utf-8"))
		except FileNotFoundError:
			return {}

	def read_marker(self, site: str) -> dict | None:
		cfg = self._read_config(site)
		marker = {k: cfg[k] for k in MARKER_KEYS if k in cfg}
		return marker or None

	def write_marker(self, site: str, updates: dict) -> None:
		cfg = self._read_config(site)
		for key, value in updates.items():
			if key not in MARKER_KEYS:
				raise ValueError(f"not a marker key: {key}")
			if value is None:
				cfg.pop(key, None)
			else:
				cfg[key] = value
		self._replace_json(self._config_path(site), cfg)

	def _intent_path(self, site: str) -> Path:
		return Path(self._c.bench_path) / ".rt-provisioner" / f"{site}.json"

	def read_intent(self, site: str) -> dict | None:
		"""The journal entry written before ``new-site`` ran: the proof that an unmarked site was
		started by this provisioner, which is what allows dropping it (otherwise it is a conflict)."""
		try:
			return json.loads(self._intent_path(site).read_text(encoding="utf-8"))
		except FileNotFoundError:
			return None

	def write_intent(self, site: str, tenant_id: str, request_id: str) -> None:
		path = self._intent_path(site)
		path.parent.mkdir(parents=True, exist_ok=True)
		self._replace_json(path, {"tenant_id": tenant_id, "request_id": request_id, "site": site})

	def clear_intent(self, site: str) -> None:
		"""Remove the journal entry once the marker exists: from then on an intent with no marker
		means "started here and never finished", not a leftover."""
		self._intent_path(site).unlink(missing_ok=True)

	@staticmethod
	def _replace_json(path: Path, data: dict) -> None:
		fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
		try:
			with os.fdopen(fd, "w", encoding="utf-8") as handle:
				json.dump(data, handle, indent=1)
			if path.exists():
				shutil.copymode(path, tmp)  # mkstemp makes 0600; keep the file's own mode
				BenchGateway._keep_owner(path, tmp)
			os.replace(tmp, path)
		except BaseException:
			Path(tmp).unlink(missing_ok=True)
			raise

	@staticmethod
	def _keep_owner(original: Path, replacement: str) -> None:
		"""A site's workers may run as another user: keep the file's owner and group (best effort)."""
		chown = getattr(os, "chown", None)
		if chown is None:
			return
		stat = original.stat()
		try:
			chown(replacement, stat.st_uid, stat.st_gid)
		except PermissionError:
			pass  # not root and not the owner: the mode copy above is all that can be kept

	def _has_marker(self, name: str) -> bool:
		try:
			return "rt_tenant_id" in self._read_config(name)
		except ValueError:
			return False  # a corrupt site_config.json must not stop provisioning of other tenants

	def count_sites(self) -> int:
		if not self._sites.is_dir():
			return 0
		return sum(1 for d in self._sites.iterdir() if d.is_dir() and self._has_marker(d.name))
