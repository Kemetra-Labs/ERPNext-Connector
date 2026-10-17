# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-635 (OBS-6) — the Connector-only Sentry client: default-inert, isolated, never blocking.

Cases:
  - configuration (OBS-1 §1 / §5): no or blank DSN → inert and the SDK is never imported; a
    malformed DSN, a non-https one or one outside the allowed ingest host (§6) → inert with ONE
    warning that never echoes the DSN; ``env`` from ``DEPLOYMENT_ENVIRONMENT``; ``release`` is the
    release SHA12 (a full SHA is shortened), anything else is ``unknown``;
  - failure behaviour (§5): no ``sentry_sdk`` installed, a failing SDK constructor or a failing
    capture all return ``False`` and never raise;
  - :func:`report_unhandled` re-raises the ORIGINAL exception after reporting it, even when the
    report itself fails;
  - with the real pinned SDK (``sentry-sdk~=1.45.1``, shipped with Frappe v15/v16):
      * one tick error → one event with ``service``/``env``/``version``/``request_id``/``job_name``
        tags, the release, a scrubbed exception and no request/user/breadcrumbs/frame vars;
      * the Connector client is never bound globally: ``Hub.current`` is unchanged, and an error
        reported through the GLOBAL hub (Frappe's request-error path) produces NO Connector event
        (RT-635 AC2);
      * one tick's ``request_id`` never leaks into the next tick's event;
      * a transport that cannot reach Sentry never surfaces to the caller (AC5).

unittest TestCases so ``bench run-tests`` runs them too; the real-SDK cases skip only where
``sentry_sdk`` is not installed.
"""

import builtins
import importlib.util
import sys
import types
import unittest
from unittest import mock

from retail_tower_erpnext_connector.connector.observability import sentry_client
from retail_tower_erpnext_connector.connector.observability.sentry_client import (
	DSN_ENV,
	ENVIRONMENT_ENV,
	RELEASE_ENV,
	SERVICE,
	capture_tick_error,
	report_unhandled,
	resolve_config,
)

_HAS_SDK = importlib.util.find_spec("sentry_sdk") is not None
_DSN = "https://0123456789abcdef0123456789abcdef@o123.ingest.de.sentry.io/4567"
_REQUEST_ID = "5f0c6f7e-3c1a-4b8e-9d2f-1a2b3c4d5e6f"
_OTHER_REQUEST_ID = "6a1d7f8e-4d2b-4c9f-8e3a-2b3c4d5e6f70"
_SHA = "0123456789abcdef0123456789abcdef01234567"
_ENV = {DSN_ENV: _DSN, ENVIRONMENT_ENV: "staging", RELEASE_ENV: _SHA}


def _tick_raising(exc: BaseException) -> None:
	"""A tick body that fails with ``exc`` inside :func:`report_unhandled`."""
	with report_unhandled(job_name="posting_poll", request_id=_REQUEST_ID):
		raise exc


def _raised(exc: BaseException) -> BaseException:
	"""``exc`` with a real traceback, as a poller tick would surface it."""
	try:
		raise exc
	except BaseException as caught:
		return caught


class _CacheReset(unittest.TestCase):
	def setUp(self):
		sentry_client.reset_cache()
		self.addCleanup(sentry_client.reset_cache)


class TestResolveConfig(_CacheReset):
	def test_no_dsn_is_inert(self):
		self.assertIsNone(resolve_config({}))
		self.assertIsNone(resolve_config({DSN_ENV: "   "}))

	def test_valid_dsn_yields_tags_and_release(self):
		config = resolve_config(_ENV)
		self.assertEqual(config.dsn, _DSN)
		self.assertEqual(config.environment, "staging")
		self.assertEqual(config.release, _SHA[:12])

	def test_sha12_release_is_kept_lower_case(self):
		self.assertEqual(resolve_config({**_ENV, RELEASE_ENV: "0123456789AB"}).release, "0123456789ab")

	def test_missing_or_malformed_release_and_env_are_unknown(self):
		for release in ("", "v1.2.3", "0123abc", "g123456789ab"):
			with self.subTest(release=release):
				self.assertEqual(resolve_config({**_ENV, RELEASE_ENV: release}).release, "unknown")
		config = resolve_config({DSN_ENV: _DSN})
		self.assertEqual((config.environment, config.release), ("unknown", "unknown"))
		self.assertEqual(resolve_config({**_ENV, ENVIRONMENT_ENV: "Stag ing!"}).environment, "unknown")

	def test_bad_dsns_are_inert_and_warn_once_without_echoing_the_dsn(self):
		bad = (
			"not a dsn",
			"http://key@o1.ingest.de.sentry.io/1",  # not https
			"https://o1.ingest.de.sentry.io/1",  # no public key
			"https://key@o1.ingest.de.sentry.io/project",  # non-numeric project id
			"https://key@o1.ingest.us.sentry.io/1",  # outside the allowed egress (D9)
			"https://key@evil.example/ingest.de.sentry.io/1",
			"https://key@[::1/1",
		)
		for dsn in bad:
			with self.subTest(dsn=dsn):
				sentry_client.reset_cache()
				with self.assertLogs("retail_tower_observability", level="WARNING") as logs:
					self.assertIsNone(resolve_config({DSN_ENV: dsn}))
					self.assertIsNone(resolve_config({DSN_ENV: dsn}))
				self.assertEqual(len(logs.records), 1)  # once per process, not per tick
				self.assertNotIn(dsn, logs.output[0])
				self.assertNotIn("key@", logs.output[0])


class TestFailureNeverPropagates(_CacheReset):
	def test_no_dsn_never_imports_the_sdk(self):
		with mock.patch.object(sentry_client, "_load_sdk", side_effect=AssertionError("imported")):
			self.assertFalse(capture_tick_error(_raised(ValueError("x")), request_id=_REQUEST_ID, job_name="posting_poll", environ={}))

	def test_missing_sdk_is_inert(self):
		real_import = builtins.__import__

		def _no_sentry(name, *args, **kwargs):
			if name == "sentry_sdk" or name.startswith("sentry_sdk."):
				raise ImportError(name)
			return real_import(name, *args, **kwargs)

		with mock.patch.dict(sys.modules, {"sentry_sdk": None}), mock.patch.object(builtins, "__import__", _no_sentry):
			self.assertIsNone(sentry_client._load_sdk())
			self.assertFalse(
				capture_tick_error(_raised(ValueError("x")), request_id=_REQUEST_ID, job_name="posting_poll", environ=_ENV)
			)

	def test_failing_sdk_constructor_returns_false(self):
		broken = types.SimpleNamespace(Client=mock.Mock(side_effect=TypeError("unknown option")), Hub=mock.Mock())
		with mock.patch.object(sentry_client, "_load_sdk", return_value=broken):
			self.assertFalse(
				capture_tick_error(_raised(ValueError("x")), request_id=_REQUEST_ID, job_name="posting_poll", environ=_ENV)
			)

	def test_failing_capture_returns_false(self):
		hub = mock.Mock()
		hub.capture_exception.side_effect = RuntimeError("sentry down")
		sdk = types.SimpleNamespace(Client=mock.Mock(), Hub=mock.Mock(return_value=hub))
		with mock.patch.object(sentry_client, "_load_sdk", return_value=sdk):
			self.assertFalse(
				capture_tick_error(_raised(ValueError("x")), request_id=_REQUEST_ID, job_name="posting_poll", environ=_ENV)
			)

	def test_report_unhandled_reraises_the_original_exception(self):
		original = ValueError("tick failed")
		with mock.patch.object(sentry_client, "capture_tick_error", return_value=True) as capture:
			with self.assertRaises(ValueError) as raised:
				_tick_raising(original)
		self.assertIs(raised.exception, original)
		capture.assert_called_once_with(original, request_id=_REQUEST_ID, job_name="posting_poll")

	def test_report_unhandled_reraises_even_when_reporting_fails(self):
		original = ValueError("tick failed")
		with mock.patch.object(sentry_client, "resolve_config", side_effect=RuntimeError("boom")):
			with self.assertRaises(ValueError) as raised:
				_tick_raising(original)
		self.assertIs(raised.exception, original)

	def test_report_unhandled_is_silent_on_success(self):
		with mock.patch.object(sentry_client, "capture_tick_error") as capture:
			with report_unhandled(job_name="posting_poll", request_id=_REQUEST_ID):
				pass
		capture.assert_not_called()


@unittest.skipUnless(_HAS_SDK, "needs sentry-sdk (shipped with Frappe; pinned in ci.yml)")
class TestRealSdk(_CacheReset):
	"""Against the real pinned SDK, with a recording transport instead of the network."""

	def setUp(self):
		super().setUp()
		import sentry_sdk
		from sentry_sdk.transport import Transport

		self.sentry_sdk = sentry_sdk
		events = self.events = []

		class _Recording(Transport):
			def capture_event(self, event):
				events.append(event)

			def capture_envelope(self, envelope):
				event = envelope.get_event()
				if event is not None:
					events.append(event)

			def flush(self, timeout, callback=None):
				return None

		self.transport_cls = _Recording
		real = sentry_sdk

		class _Sdk:
			Hub = real.Hub

			@staticmethod
			def Client(**options):
				return real.Client(transport=_Recording(options), **options)

		patcher = mock.patch.object(sentry_client, "_load_sdk", return_value=_Sdk)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _capture(self, exc, request_id=_REQUEST_ID):
		return capture_tick_error(exc, request_id=request_id, job_name="posting_poll", environ=_ENV)

	def test_tick_error_becomes_one_scrubbed_tagged_event(self):
		exc = _raised(ConnectionError("GET https://u:p@dp2.example/v1/feed?token=abc failed for jane@example.com"))
		self.assertTrue(self._capture(exc))

		(event,) = self.events
		self.assertEqual(
			event["tags"],
			{
				"service": SERVICE,
				"env": "staging",
				"version": _SHA[:12],
				"request_id": _REQUEST_ID,
				"job_name": "posting_poll",
				"error_class": "ConnectionError",
			},
		)
		self.assertEqual(event["release"], _SHA[:12])
		self.assertEqual(event["environment"], "staging")
		(value,) = event["exception"]["values"]
		self.assertEqual(value["type"], "ConnectionError")
		self.assertEqual(value["value"], "GET https://dp2.example/v1/feed failed for [email]")
		for dropped in ("request", "user", "extra", "contexts", "breadcrumbs", "modules", "sdk"):
			self.assertNotIn(dropped, event)
		for frame in value["stacktrace"]["frames"]:
			self.assertLessEqual(set(frame), {"filename", "function", "lineno", "colno", "module", "in_app"})

	def test_client_is_never_bound_to_the_global_hub(self):
		before = self.sentry_sdk.Hub.current.client
		self._capture(_raised(ValueError("x")))
		self.assertIs(self.sentry_sdk.Hub.current.client, before)

	def test_frappe_request_error_through_the_global_hub_produces_no_event(self):
		# RT-635 AC2: Frappe's own path (frappe.utils.sentry.capture_exception) reports through the
		# GLOBAL hub. The Connector client exists (one tick error was captured) yet sees nothing.
		self._capture(_raised(ValueError("tick")))
		self.events.clear()
		try:
			raise RuntimeError("frappe request error")
		except RuntimeError as exc:
			self.sentry_sdk.capture_exception(exc)
			self.sentry_sdk.Hub.current.capture_exception(exc)
		self.assertEqual(self.events, [])

	def test_request_id_does_not_leak_between_ticks(self):
		self._capture(_raised(ValueError("first")), request_id=_REQUEST_ID)
		self._capture(_raised(ValueError("second")), request_id=_OTHER_REQUEST_ID)
		self.assertEqual([e["tags"]["request_id"] for e in self.events], [_REQUEST_ID, _OTHER_REQUEST_ID])

	def test_client_options_keep_the_sdk_errors_only_and_pii_free(self):
		self._capture(_raised(ValueError("x")))
		(hub,) = sentry_client._HUBS.values()
		options = hub.client.options
		self.assertEqual(options["max_breadcrumbs"], 0)
		self.assertFalse(options["send_default_pii"])
		self.assertFalse(options["include_local_variables"])
		self.assertFalse(options["include_source_context"])
		self.assertFalse(options["default_integrations"])
		self.assertFalse(options["auto_enabling_integrations"])
		self.assertFalse(options["auto_session_tracking"])
		self.assertIsNone(options["traces_sample_rate"])
		self.assertEqual(options["max_request_body_size"], "never")
		self.assertEqual(hub.client.integrations, {})

	def test_unreachable_sentry_never_surfaces(self):
		# AC5 (unit level): a blocked network makes the transport raise; the caller only sees False
		# or True — never an exception — and the next capture still works.
		def _blocked(self, *_a, **_k):
			raise OSError("network unreachable")

		with mock.patch.object(self.transport_cls, "capture_envelope", _blocked), mock.patch.object(
			self.transport_cls, "capture_event", _blocked
		):
			self._capture(_raised(ValueError("blocked")))
		self.assertTrue(self._capture(_raised(ValueError("after"))))
