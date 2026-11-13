# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Provisioner configuration: a JSON file whose path is the process's only argument.

The file never holds a secret. It names the *path* of the provisioner credential file (OD-3: an
owner-provisioned secret store on the host); the database root login comes from the bench's own
``common_site_config.json`` (Frappe's ``root_login`` / ``root_password`` fallback), never from here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

_SECRET_WORDS = ("password", "secret", "token", "api_key", "credential")
_PATH_KEYS = {"credential_file"}


@dataclass(frozen=True)
class ProvisionerConfig:
	base_url: str
	bench_path: str
	credential_file: str
	bench_executable: str = "bench"
	connector_app: str = "retail_tower_erpnext_connector"
	db_name_prefix: str = "rtp"
	max_sites: int = 20  # OD-10: 20 sites per bench
	queue_depth_threshold: int = 500  # Frappe refuses new jobs at 500 + 50 per site (background_jobs.py)
	step_budget_seconds: int = 300  # do not start a long step with less lease than this
	poll_interval_seconds: int = 30
	lock_path: str | None = None
	connector_base_url: str | None = None  # Connector Settings dp2_base_url; defaults to base_url
	new_site_timeout_seconds: int = 600
	install_timeout_seconds: int = 900
	site_step_timeout_seconds: int = 900
	short_timeout_seconds: int = 120

	@property
	def effective_connector_base_url(self) -> str:
		return self.connector_base_url or self.base_url

	@property
	def effective_lock_path(self) -> str:
		return self.lock_path or str(Path(self.bench_path) / ".rt-provisioner.lock")


_DB_PREFIX = re.compile(r"[a-z][a-z0-9_]{0,31}")  # the hash adds 17 characters to MariaDB's 64
_POSITIVE_INTS = (
	"max_sites",
	"queue_depth_threshold",
	"step_budget_seconds",
	"poll_interval_seconds",
	"new_site_timeout_seconds",
	"install_timeout_seconds",
	"site_step_timeout_seconds",
	"short_timeout_seconds",
)


def _validate_values(cfg: ProvisionerConfig) -> None:
	for name in _POSITIVE_INTS:
		value = getattr(cfg, name)
		if isinstance(value, bool) or not isinstance(value, int) or value < 1:
			raise ValueError(f"{name} must be a positive integer")
	if not _DB_PREFIX.fullmatch(cfg.db_name_prefix):
		raise ValueError("db_name_prefix must be 1 to 32 characters of [a-z0-9_], starting with a letter")
	for name in ("base_url", "bench_path", "credential_file", "bench_executable", "connector_app"):
		if not isinstance(getattr(cfg, name), str) or not getattr(cfg, name):
			raise ValueError(f"{name} must be a non-empty string")


def load_config(path: str | Path) -> ProvisionerConfig:
	raw = json.loads(Path(path).read_text(encoding="utf-8"))
	if not isinstance(raw, dict):
		raise ValueError("config must be a JSON object")
	for key in raw:
		if key not in _PATH_KEYS and any(word in key.lower() for word in _SECRET_WORDS):
			raise ValueError(f"config key {key!r} looks like a secret: the file may only name paths")
	known = {f.name for f in fields(ProvisionerConfig)}
	unknown = sorted(set(raw) - known)
	if unknown:
		raise ValueError(f"unknown config keys: {unknown}")
	cfg = ProvisionerConfig(**raw)
	_validate_values(cfg)
	for label, url in (("base_url", cfg.base_url), ("connector_base_url", cfg.connector_base_url)):
		if url is not None and urlsplit(url).scheme != "https":
			raise ValueError(f"{label} must be https")
	return cfg
