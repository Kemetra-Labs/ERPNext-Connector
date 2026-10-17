# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-635 (OBS-6) — the Connector-only Sentry client for unhandled poller and transport errors.

Frappe ships its own Sentry integration (``frappe/utils/sentry.py``). It reports through the GLOBAL
hub and attaches request JSON and form bodies, so it stays off (``FRAPPE_SENTRY_DSN`` unset,
``enable_telemetry`` off). This module never calls ``sentry_sdk.init()``, never binds a client to the
global hub and never hooks Frappe's error handling. It owns a private ``Hub(Client(...))`` (the 1.x
API of the ``sentry-sdk~=1.45.1`` that Frappe v15/v16 already ship; no new dependency) and captures
only the errors the poller entrypoints hand it through :func:`report_unhandled`.

OBS-1 posture:
  - default-inert (§5): without ``RT_CONNECTOR_SENTRY_DSN`` nothing is imported or sent. A malformed
    DSN, or one outside the allowed egress (§6: ``*.ingest.de.sentry.io``), logs one warning that
    never echoes the DSN. The DSN name is the Connector's own, so it can never switch on Frappe's
    integration or the SDK's ``SENTRY_DSN`` default;
  - errors only (§4): no tracing, profiling, sessions, breadcrumbs, integrations or client reports;
  - privacy (§3): :func:`scrubber.scrub_event` is ``before_send``; PII, local variables and source
    context are off in the SDK as well;
  - tags (§1 / §2): ``service=erpnext-connector``, ``env`` (``DEPLOYMENT_ENVIRONMENT``), ``version``
    and ``release`` (the release SHA12 from ``RT_CONNECTOR_RELEASE``), the tick's ``request_id`` (the
    RT-39 ``X-Request-Id`` Backend-Core logs), ``job_name`` and ``error_class``. Tags are passed per
    event, so one tick's ``request_id`` never reaches another tick's event;
  - never blocking (§5): every failure returns ``False``; the only wait is a bounded flush on the
    error path, because the RQ work-horse exits right after the job.
"""

from __future__ import annotations

import logging
import os
import string
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from .scrubber import scrub_event

SERVICE = "erpnext-connector"
DSN_ENV = "RT_CONNECTOR_SENTRY_DSN"
RELEASE_ENV = "RT_CONNECTOR_RELEASE"
ENVIRONMENT_ENV = "DEPLOYMENT_ENVIRONMENT"
ALLOWED_INGEST_SUFFIX = ".ingest.de.sentry.io"
FLUSH_TIMEOUT_SECONDS = 2.0
UNKNOWN = "unknown"
_SHA12_LENGTH = 12
_MAX_SHA_LENGTH = 40
_MAX_ENV_LENGTH = 32
_HEX = frozenset(string.hexdigits.lower())
_ENV_CHARS = frozenset(string.ascii_lowercase + string.digits + "-_")

_logger = logging.getLogger("retail_tower_observability")
_WARNED: set[str] = set()
# One private hub per configuration, built on the first error of the process (never on a healthy tick).
_HUBS: dict[SentryConfig, object] = {}


@dataclass(frozen=True)
class SentryConfig:
    dsn: str
    environment: str
    release: str


def resolve_config(environ: Mapping[str, str]) -> SentryConfig | None:
    """The Connector's Sentry settings, or ``None`` (inert) when the DSN is unset, blank or not allowed."""
    dsn = (environ.get(DSN_ENV) or "").strip()
    if not dsn:
        return None
    if not _is_allowed_dsn(dsn):
        _warn_once(f"sentry disabled: {DSN_ENV} is not a valid https DSN for *{ALLOWED_INGEST_SUFFIX}")
        return None
    return SentryConfig(
        dsn=dsn,
        environment=_environment(environ.get(ENVIRONMENT_ENV)),
        release=_release(environ.get(RELEASE_ENV)),
    )


def _is_allowed_dsn(dsn: str) -> bool:
    try:
        parts = urlsplit(dsn)
        host = (parts.hostname or "").lower()
        project_id = parts.path.rpartition("/")[2]
        return (
            parts.scheme == "https"
            and bool(parts.username)
            and host.endswith(ALLOWED_INGEST_SUFFIX)
            and project_id.isdigit()
        )
    except ValueError:
        return False


def _environment(raw: str | None) -> str:
    value = (raw or "").strip()
    if 0 < len(value) <= _MAX_ENV_LENGTH and all(char in _ENV_CHARS for char in value):
        return value
    return UNKNOWN


def _release(raw: str | None) -> str:
    """The release SHA12 (OBS-1 §1); a full commit SHA is shortened, anything else is ``unknown``."""
    value = (raw or "").strip().lower()
    if _SHA12_LENGTH <= len(value) <= _MAX_SHA_LENGTH and all(char in _HEX for char in value):
        return value[:_SHA12_LENGTH]
    return UNKNOWN


def client_options(config: SentryConfig) -> dict:
    """``Client`` options: errors only, no PII, no integrations, scrubbed by the OBS-1 allowlist.

    ``release`` and ``environment`` are always explicit: a ``None`` release makes the 1.x SDK read
    environment variables and then shell out to ``git``.
    """
    return {
        "dsn": config.dsn,
        "environment": config.environment,
        "release": config.release,
        "before_send": scrub_event,
        "before_send_transaction": _drop,
        "before_breadcrumb": _drop,
        "max_breadcrumbs": 0,
        "send_default_pii": False,
        "include_local_variables": False,
        "include_source_context": False,
        "attach_stacktrace": False,
        "max_request_body_size": "never",
        "default_integrations": False,
        "auto_enabling_integrations": False,
        "integrations": [],
        "traces_sample_rate": None,
        "profiles_sample_rate": None,
        "auto_session_tracking": False,
        "send_client_reports": False,
        "enable_backpressure_handling": False,
        "propagate_traces": False,
        "trace_propagation_targets": [],
        "shutdown_timeout": FLUSH_TIMEOUT_SECONDS,
    }


def _drop(_item: object, _hint: object) -> None:
    return None


def capture_tick_error(
    exc: BaseException,
    *,
    request_id: str,
    job_name: str,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Report one unhandled tick error to the Connector's own Sentry project. Never raises.

    ``True`` when the event was handed to the SDK; ``False`` when inert or when anything failed.
    """
    try:
        config = resolve_config(os.environ if environ is None else environ)
        if config is None:
            return False
        hub = _hub_for(config)
        if hub is None:
            return False
        hub.capture_exception(exc, tags=_tags(config, request_id=request_id, job_name=job_name, exc=exc))
        hub.flush(timeout=FLUSH_TIMEOUT_SECONDS)
        return True
    except Exception:  # a telemetry failure never reaches a posting (OBS-1 §5)
        _warn_once("sentry capture failed; the tick error is still raised to the scheduler")
        return False


@contextmanager
def report_unhandled(*, job_name: str, request_id: str) -> Iterator[None]:
    """Report an exception escaping the block, then re-raise it unchanged (the scheduler still logs it)."""
    try:
        yield
    except Exception as exc:
        capture_tick_error(exc, request_id=request_id, job_name=job_name)
        raise


def _tags(config: SentryConfig, *, request_id: str, job_name: str, exc: BaseException) -> dict[str, str]:
    return {
        "service": SERVICE,
        "env": config.environment,
        "version": config.release,
        "request_id": request_id,
        "job_name": job_name,
        "error_class": type(exc).__name__,
    }


def _hub_for(config: SentryConfig):
    hub = _HUBS.get(config)
    if hub is None:
        sdk = _load_sdk()
        if sdk is None:
            return None
        hub = sdk.Hub(sdk.Client(**client_options(config)))
        _HUBS[config] = hub
    return hub


def _load_sdk():
    """``sentry_sdk``, or ``None`` when it is not installed (inert)."""
    try:
        import sentry_sdk
    except ImportError:
        return None
    return sentry_sdk


def _warn_once(message: str) -> None:
    if message not in _WARNED:
        _WARNED.add(message)
        _logger.warning(message)


def reset_cache() -> None:
    """Forget the built hubs and the warnings already logged (tests)."""
    _HUBS.clear()
    _WARNED.clear()
