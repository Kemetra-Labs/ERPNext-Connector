# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-635 (OBS-6) — the Connector's Sentry scrubber conforms to the OBS-1 privacy baseline (§3).

Cases:
  - §3.1 allowlist: an outgoing event is REBUILT from the allowlisted fields only — a deliberately
    dirty event keeps exactly the allowed keys at every level (top level, tags, exception values,
    mechanism, stack frames); request/user/extra/contexts/breadcrumbs/frame vars/source lines go;
  - an event without an exception is dropped (errors only), and the input is never mutated;
  - §3.2 masking anywhere in a message: JWT → ``[jwt]``; URL userinfo/query/fragment removed (an
    unparsable URL → ``[url]``); email → ``[email]``; Postgres row detail from ``Key (`` /
    ``Failing row contains (`` to the end; the MariaDB ``Duplicate entry '<value>'`` row value (the
    Connector's own DB);
  - §3.3 secrets: ``Bearer <x>`` and credential ``key=value`` / ``"key": "value"`` forms;
  - input capped at 2,000 chars before scanning, each message truncated to 300;
  - linear-time: an adversarial 2,000-char input scrubs fast.

unittest TestCases (not pytest functions) so ``bench run-tests`` runs them too (AC1). Pure Python:
no frappe, no sentry_sdk.
"""

import copy
import json
import time
import unittest
from pathlib import Path

from retail_tower_erpnext_connector.connector.observability.scrubber import (
	MAX_MESSAGE_LENGTH,
	sanitize_message,
	scrub_event,
)

_REQUEST_ID = "5f0c6f7e-3c1a-4b8e-9d2f-1a2b3c4d5e6f"
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl"


def _dirty_event() -> dict:
	frame = {
		"filename": "retail_tower_erpnext_connector/connector/posting/poller.py",
		"abs_path": "/home/frappe/frappe-bench/apps/retail_tower_erpnext_connector/poller.py",
		"function": "run_posting_poll",
		"module": "retail_tower_erpnext_connector.connector.posting.poller",
		"lineno": 66,
		"colno": 4,
		"in_app": True,
		"vars": {"token": "secret-value", "sale": {"posTotal": "100.00"}},
		"pre_context": ["settings = frappe.get_doc('Connector Settings')"],
		"context_line": "token = settings.get_password('dp2_token')",
		"post_context": ["..."],
	}
	return {
		"event_id": "a" * 32,
		"timestamp": "2026-10-17T10:00:00.000000Z",
		"platform": "python",
		"level": "error",
		"environment": "staging",
		"release": "0123456789ab",
		"server_name": "rt-erp-worker",
		"logger": "retail_tower_posting",
		"logentry": {"message": "sale POS-9001 for jane@example.com"},
		"message": "sale POS-9001",
		"request": {"url": "https://erp/api/method/x?sid=abc", "data": {"sale": 1}, "cookies": "sid=abc"},
		"user": {"id": "Administrator", "email": "admin@example.com", "ip_address": "10.0.0.1"},
		"extra": {"sale": {"externalId": "POS-9001"}},
		"contexts": {"JSON Body": {"sale": 1}, "runtime": {"name": "CPython"}},
		"breadcrumbs": {"values": [{"message": "GET /api?token=x"}]},
		"modules": {"frappe": "16.52.0"},
		"sdk": {"name": "sentry.python", "version": "1.45.1"},
		"threads": {"values": []},
		"transaction": "/api/method/frappe.desk",
		"fingerprint": ["{{ default }}"],
		"tags": {
			"service": "erpnext-connector",
			"env": "staging",
			"version": "0123456789ab",
			"request_id": _REQUEST_ID,
			"job_name": "posting_poll",
			"error_class": "HTTPError",
			"tenant_id": "11111111-1111-4111-8111-111111111111",
			"frappe_user": "Administrator",
			"store_id": "33333333-3333-4333-8333-333333333333",
		},
		"exception": {
			"values": [
				{
					"type": "HTTPError",
					"value": "404 Client Error for url: https://u:p@dp2.example/v1/feed?token=abc#frag",
					"module": "requests.exceptions",
					"mechanism": {"type": "generic", "handled": False, "data": {"x": 1}, "meta": {}},
					"stacktrace": {"frames": [frame], "registers": {}},
				}
			]
		},
	}


class TestEventAllowlist(unittest.TestCase):
	"""OBS-1 §3.1 — the event is rebuilt from allowlisted fields; everything else is dropped."""

	def setUp(self):
		self.scrubbed = scrub_event(_dirty_event(), {})

	def test_top_level_keeps_only_the_allowlist(self):
		self.assertEqual(
			set(self.scrubbed),
			{
				"event_id",
				"timestamp",
				"platform",
				"level",
				"environment",
				"release",
				"server_name",
				"tags",
				"exception",
			},
		)

	def test_tags_keep_only_bounded_allowlisted_keys(self):
		self.assertEqual(
			self.scrubbed["tags"],
			{
				"service": "erpnext-connector",
				"env": "staging",
				"version": "0123456789ab",
				"request_id": _REQUEST_ID,
				"job_name": "posting_poll",
				"error_class": "HTTPError",
			},
		)

	def test_exception_value_keeps_type_value_mechanism_and_frames_only(self):
		(value,) = self.scrubbed["exception"]["values"]
		self.assertEqual(set(value), {"type", "value", "mechanism", "stacktrace"})
		self.assertEqual(value["mechanism"], {"type": "generic", "handled": False})
		self.assertEqual(set(value["stacktrace"]), {"frames"})

	def test_frames_keep_location_only(self):
		(frame,) = self.scrubbed["exception"]["values"][0]["stacktrace"]["frames"]
		self.assertEqual(
			frame,
			{
				"filename": "retail_tower_erpnext_connector/connector/posting/poller.py",
				"function": "run_posting_poll",
				"module": "retail_tower_erpnext_connector.connector.posting.poller",
				"lineno": 66,
				"colno": 4,
				"in_app": True,
			},
		)

	def test_exception_value_is_sanitized(self):
		(value,) = self.scrubbed["exception"]["values"]
		self.assertEqual(value["value"], "404 Client Error for url: https://dp2.example/v1/feed")

	def test_no_secret_or_business_value_survives_anywhere(self):
		text = repr(self.scrubbed)
		for leaked in ("secret-value", "POS-9001", "example.com", "sid=abc", "Administrator", "token=abc"):
			self.assertNotIn(leaked, text)

	def test_input_event_is_not_mutated(self):
		event = _dirty_event()
		before = copy.deepcopy(event)
		scrub_event(event, {})
		self.assertEqual(event, before)

	def test_event_without_exception_is_dropped(self):
		event = _dirty_event()
		del event["exception"]
		self.assertIsNone(scrub_event(event, {}))
		self.assertIsNone(scrub_event({"exception": {"values": []}}, {}))
		self.assertIsNone(scrub_event({"exception": "not-a-dict"}, {}))

	def test_malformed_request_id_tag_is_dropped(self):
		event = _dirty_event()
		event["tags"]["request_id"] = "not-a-uuid"
		self.assertNotIn("request_id", scrub_event(event, {})["tags"])

	def test_free_text_or_overlong_tag_values_are_dropped(self):
		event = _dirty_event()
		event["tags"]["outcome"] = "sale for jane doe"  # spaces: free text, never a tag
		event["tags"]["route"] = "/v1/" + "x" * 100  # unbounded
		event["tags"]["method"] = 42  # not a string
		tags = scrub_event(event, {})["tags"]
		for key in ("outcome", "route", "method"):
			self.assertNotIn(key, tags)

	def test_string_frame_and_type_fields_are_sanitized(self):
		event = _dirty_event()
		frame = event["exception"]["values"][0]["stacktrace"]["frames"][0]
		frame["filename"] = "/home/jane.doe@example.com/apps/poller.py"
		frame["function"] = f"handler_{_JWT}"
		frame["module"] = "pkg.mod?token=abc"
		(scrubbed,) = scrub_event(event, {})["exception"]["values"][0]["stacktrace"]["frames"]
		self.assertEqual(scrubbed["filename"], "poller.py")
		self.assertEqual(scrubbed["function"], "handler_[jwt]")
		self.assertNotIn("token=abc", repr(scrubbed))

	def test_absolute_frame_paths_keep_only_the_file_name(self):
		for path in ("/home/jane/apps/poller.py", "C:\\Users\\jane\\poller.py", "D:/jane/poller.py"):
			with self.subTest(path=path):
				event = _dirty_event()
				event["exception"]["values"][0]["stacktrace"]["frames"][0]["filename"] = path
				(frame,) = scrub_event(event, {})["exception"]["values"][0]["stacktrace"]["frames"]
				self.assertEqual(frame["filename"], "poller.py")

	def test_relative_frame_paths_are_kept(self):
		(frame,) = self.scrubbed["exception"]["values"][0]["stacktrace"]["frames"]
		self.assertEqual(frame["filename"], "retail_tower_erpnext_connector/connector/posting/poller.py")

	def test_frame_values_that_are_not_scalars_are_dropped(self):
		event = _dirty_event()
		event["exception"]["values"][0]["stacktrace"]["frames"][0]["function"] = {"nested": "x"}
		(frame,) = scrub_event(event, {})["exception"]["values"][0]["stacktrace"]["frames"]
		self.assertNotIn("function", frame)


class TestMessageMasking(unittest.TestCase):
	"""OBS-1 §3.2 / §3.3 — patterns are masked anywhere in the message."""

	def test_jwt_is_masked(self):
		self.assertEqual(sanitize_message(f"bad session {_JWT} rejected"), "bad session [jwt] rejected")

	def test_jwt_inside_a_token_is_masked(self):
		self.assertEqual(sanitize_message(f"auth=({_JWT})"), "auth=([jwt]")

	def test_url_userinfo_query_and_fragment_are_removed(self):
		self.assertEqual(
			sanitize_message("GET https://user:pw@dp2.example:8443/v1/feed?since=c-1&token=x#top failed"),
			"GET https://dp2.example:8443/v1/feed failed",
		)

	def test_quoted_url_keeps_its_trailing_punctuation(self):
		self.assertEqual(
			sanitize_message("url='https://dp2.example/v1/feed?since=c-1'."),
			"url='https://dp2.example/v1/feed'.",
		)

	def test_relative_url_query_is_removed(self):
		# requests.ConnectionError when Backend-Core is down: urllib3 prints a RELATIVE url.
		self.assertEqual(
			sanitize_message(
				"HTTPSConnectionPool(host='dp2.example', port=443): Max retries exceeded with url: "
				"/v1/connector/posting-feed?since=c-1&token=abc (Caused by NewConnectionError('refused'))"
			),
			"HTTPSConnectionPool(host='dp2.example', port=443): Max retries exceeded with url: "
			"/v1/connector/posting-feed (Caused by NewConnectionError('refused'))",
		)
		self.assertEqual(sanitize_message("path='/v1/feed?since=c-1#x'."), "path='/v1/feed'.")

	def test_question_without_a_path_is_kept(self):
		self.assertEqual(sanitize_message("why? because"), "why? because")

	def test_unparsable_url_becomes_placeholder(self):
		self.assertEqual(sanitize_message("bad http://[::1/x?token=y here"), "bad [url] here")
		self.assertEqual(sanitize_message("bad https://host:notaport/x end"), "bad [url] end")
		self.assertEqual(sanitize_message("bad https:///nohost?q=1 end"), "bad [url] end")

	def test_email_is_masked(self):
		self.assertEqual(sanitize_message("customer jane.doe@example.com missing"), "customer [email] missing")

	def test_postgres_key_detail_is_masked_to_the_end(self):
		self.assertEqual(
			sanitize_message('duplicate key value violates unique constraint "u" Key (email)=(a@b.c) already exists.'),
			'duplicate key value violates unique constraint "u" Key (...)=(...)',
		)

	def test_postgres_failing_row_is_masked_to_the_end(self):
		self.assertEqual(
			sanitize_message("null value in column violates not-null constraint Failing row contains (1, x, (y))."),
			"null value in column violates not-null constraint Failing row contains (...)",
		)

	def test_mariadb_duplicate_entry_value_is_masked_but_key_name_kept(self):
		self.assertEqual(
			sanitize_message(
				"(1062, \"Duplicate entry 'pos-pulse-POS-9001' for key 'unique_rt_si_provenance'\")"
			),
			"(1062, \"Duplicate entry '...' for key 'unique_rt_si_provenance'\")",
		)

	def test_mariadb_duplicate_entry_without_key_suffix_is_masked_to_the_end(self):
		self.assertEqual(sanitize_message("Duplicate entry 'POS-9001"), "Duplicate entry '...'")

	def test_bearer_token_is_masked(self):
		self.assertEqual(
			sanitize_message("sent Authorization: Bearer abc.def-123 to dp2"),
			"sent Authorization: [redacted] [redacted] to dp2",
		)

	def test_quoted_bearer_token_is_masked(self):
		# The posting transport's own header dict, as a repr in an exception message.
		self.assertEqual(
			sanitize_message("headers={'Authorization': 'Bearer abc123'} rejected"),
			"headers={'Authorization': [redacted] [redacted] rejected",
		)
		self.assertEqual(sanitize_message('sent "Bearer abc123" twice'), "sent [redacted] [redacted] twice")

	def test_any_authorization_scheme_value_is_masked(self):
		for scheme in ("Basic", "Token", "Digest", "ApiKey", "'Basic"):
			with self.subTest(scheme=scheme):
				self.assertEqual(
					sanitize_message(f"Authorization: {scheme} dXNlcjpwYXNz sent"),
					"Authorization: [redacted] [redacted] sent",
				)

	def test_compact_authorization_forms_mask_the_credential(self):
		for text in (
			"Authorization:Basic dXNlcjpwYXNz sent",
			"Authorization=Bearer abc123 sent",
			'{"Authorization":"Basic dXNlcjpwYXNz"} sent',
		):
			with self.subTest(text=text):
				out = sanitize_message(text)
				self.assertNotIn("dXNlcjpwYXNz", out)
				self.assertNotIn("abc123", out)
				self.assertTrue(out.endswith(" sent"))

	def test_basic_as_a_plain_word_is_kept(self):
		self.assertEqual(sanitize_message("basic validation failed"), "basic validation failed")

	def test_credential_in_a_token_that_also_holds_a_url_is_masked(self):
		self.assertEqual(
			sanitize_message("token=xyz,url=https://h.example/p?q=1"), "token=[redacted]"
		)
		self.assertEqual(sanitize_message("password=ab/cd?x=1"), "password=[redacted]")
		self.assertEqual(sanitize_message("dp2_token=abc/def#f"), "dp2_token=[redacted]")

	def test_credential_key_value_forms_are_masked(self):
		self.assertEqual(sanitize_message("dp2_token=abc123 api_key:xyz ok"), "dp2_token=[redacted] api_key:[redacted] ok")
		self.assertEqual(sanitize_message('{"password": "hunter2", "qty": 1}'), '{"password": [redacted] "qty": 1}')
		self.assertEqual(sanitize_message("headers={'dp2_token': 'abc'}"), "headers={'dp2_token': [redacted]")
		self.assertEqual(sanitize_message("cfg=x;client_secret=s3"), "cfg=x;client_secret=[redacted]")

	def test_non_credential_key_values_are_kept(self):
		self.assertEqual(sanitize_message("token_count=3 qty=1"), "token_count=3 qty=1")

	def test_plain_message_is_unchanged(self):
		self.assertEqual(sanitize_message("Connection refused"), "Connection refused")

	def test_whitespace_is_preserved(self):
		self.assertEqual(sanitize_message("a\n\tb  c"), "a\n\tb  c")

	def test_non_string_input_is_empty(self):
		self.assertEqual(sanitize_message(None), "")


class TestBoundsAndLinearity(unittest.TestCase):
	"""OBS-1 §3.2 — capped before scanning, truncated after, linear on adversarial input."""

	def test_message_is_truncated_to_the_contract_length(self):
		out = sanitize_message("x " * 1000)
		self.assertEqual(len(out), MAX_MESSAGE_LENGTH)
		self.assertTrue(out.endswith("..."))

	def test_secret_beyond_the_scan_cap_is_never_emitted(self):
		out = sanitize_message("a" * 2000 + " jane@example.com")
		self.assertNotIn("example.com", out)

	def test_token_cut_by_the_scan_cap_is_dropped(self):
		# A cap landing mid-email leaves "jane@e" (no dot after "@") past the email check; the long
		# query masked away first brings that fragment inside the 300-char output.
		out = sanitize_message("GET https://dp2.example/v1?" + "q" * 1966 + " jane@example.com")
		self.assertEqual(out, "GET https://dp2.example/v1")

	def test_adversarial_inputs_scrub_quickly(self):
		adversarial = [
			"a@" * 5000,
			"eyJ." * 5000,
			"a://" * 5000,
			"Key (" * 5000,
			"Duplicate entry '" * 5000,
			"token=" * 5000,
			"Bearer " * 5000,
			"@." * 5000,
			"Authorization : " * 5000,
			"password = " * 5000,
		]
		started = time.perf_counter()
		for text in adversarial:
			sanitize_message(text)
		self.assertLess(time.perf_counter() - started, 1.0)


_GOLDEN = json.loads((Path(__file__).parent / "obs1-golden-vectors.json").read_text(encoding="utf-8"))


class TestObs1GoldenVectors(unittest.TestCase):
	"""RT-686 — the OBS-1 §3.5 golden vectors (RT-682, RT-694), copied unchanged from Backend-Core.

	Every repository's scrubber gives exactly these outputs: Bearer/Authorization credentials and
	phone numbers (E.164, Egyptian mobiles) masked; UUIDs, SHA12, trace ids, timestamps, prices,
	sale numbers and barcodes kept.
	"""

	def test_every_vector_masks_exactly_as_the_contract_says(self):
		for vector in _GOLDEN["vectors"]:
			with self.subTest(vector["id"]):
				self.assertEqual(sanitize_message(vector["input"]), vector["expected"])

	def test_phone_rule_edges_follow_the_contract_text(self):
		cases = {
			# (a) fails (leading 0) but (b) still applies: the `+` stays, the digits go.
			"call +01012345678 now": "call +[phone] now",
			# (a) fails (letter before the `+`) but (c) still applies.
			"x+201000000638 kept": "x+[phone] kept",
			"missing Authorization:": "missing Authorization:",
		}
		for text, expected in cases.items():
			with self.subTest(text):
				self.assertEqual(sanitize_message(text), expected)

	def test_a_masked_word_keeps_its_own_next_word_signal(self):
		# A word masked because of the word before it, or read as a separator value, still passes on its
		# own Bearer/credential signal, so RT-694 never masks less than the RT-682 baseline.
		cases = {
			"Authorization :a,Authorization: S1": "Authorization :[redacted] [redacted]",
			"Authorization =x,Proxy-Authorization:Bearer S1": "Authorization =[redacted] [redacted]",
			"[Bearer] Authorization: S1": "[redacted] [redacted] [redacted]",
			"(Bearer) ?authorization= S1": "[redacted] [redacted] [redacted]",
			"Authorization: (Bearer) Authorization: S1": "Authorization: [redacted] [redacted] [redacted]",
			# The wider credential-key set must not mask less than Backend-Core does.
			"token :a,Authorization: S1": "token :[redacted] [redacted]",
		}
		for text, expected in cases.items():
			with self.subTest(text):
				self.assertEqual(sanitize_message(text), expected)

	def test_separator_word_edges_follow_the_contract_text(self):
		# RT-694: a bare Authorization key takes a following word's leading `:`/`=` as its separator.
		cases = {
			"Authorization\n:\nBearer x y": "Authorization\n:\n[redacted] [redacted] y",
			"headers={'Authorization' : 'Bearer x'} y": "headers={'Authorization' : [redacted] [redacted] y",
			"missing Authorization": "missing Authorization",
			# Known gaps, listed in §3.2: a key not directly before the separator word; a separator value.
			"Authorization ok : x": "Authorization ok : x",
			"Authorization : : x y": "Authorization : [redacted] x y",
			# Stricter than the contract (allowed): any credential key, as for `key=value`.
			"password = hunter2 ok": "password = [redacted] ok",
		}
		for text, expected in cases.items():
			with self.subTest(text):
				self.assertEqual(sanitize_message(text), expected)

	def test_contract_linear_time_inputs(self):
		linear = _GOLDEN["linearTime"]
		started = time.perf_counter()
		for item in linear["inputs"]:
			out = sanitize_message(item["pattern"] * item["repeat"])
			self.assertLessEqual(len(out), linear["maxOutputLength"])
		self.assertLess((time.perf_counter() - started) * 1000, linear["maxTotalMs"])

	def test_long_phone_scans_under_the_cap_stay_linear(self):
		# The contract's long inputs exceed MAX_SCAN_LENGTH with no whitespace, so the cap empties them
		# before the phone scan runs. These stay under the cap so the digit-run scan itself is timed.
		under_cap = [
			"1" * 1999,
			"+1" * 999,
			"01" * 999,
			"+201000000638 " * 140,
			"a01012345678" * 166,
		]
		started = time.perf_counter()
		for text in under_cap:
			self.assertLessEqual(len(sanitize_message(text)), MAX_MESSAGE_LENGTH)
		self.assertLess(time.perf_counter() - started, 0.5)
		self.assertTrue(sanitize_message("+201000000638 " * 140).startswith("[phone] [phone] "))
