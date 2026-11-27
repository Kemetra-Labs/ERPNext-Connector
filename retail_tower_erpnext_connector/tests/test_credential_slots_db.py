# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1362 S3 — the dual-slot credential writes on a real site (bench lane).

``tests/test_credential_slots.py`` proves the rules against a stand-in. This module runs the Frappe
calls themselves on a real v16 site: Frappe's password API for both slots, ``set_single_value`` with a
dict, the locking read of ``tabSingles``, the savepoint that makes a promotion one unit, and the new
Connector Settings fields after ``bench migrate``.

Isolation: every test runs inside the test transaction with ``frappe.flags.rt_provisioner_no_commit``
set (``credential_slots`` then never commits) and ``tearDown`` rolls it back, so no Connector Settings
value or ``__Auth`` row survives. Runs only on a bench (``bench run-tests``); without a site it skips.
"""

import unittest
from unittest import mock

try:
	import frappe
except ImportError:  # the frappe-free unit-test environment
	frappe = None

SETTINGS = "Connector Settings"
NO_COMMIT = "rt_provisioner_no_commit"
REGISTRATION = "3d2c7f60-91a4-4c0b-9f3e-6f1c2a8b4e55"
# Built at run time, low-entropy: a secret-shaped literal in a commit keeps gitleaks red.
OLD = "-".join(["rt1362", "current", "test", "value"])
NEW = "-".join(["rt1362", "pending", "test", "value"])
NEWER = "-".join(["rt1362", "newer", "test", "value"])
T1 = "2026-11-27T10:00:00.000001Z"
T2 = "2026-11-27T10:00:00.000002Z"


def _connected_site() -> bool:
	return frappe is not None and bool(getattr(getattr(frappe, "local", None), "site", None))


class TestCredentialSlotsOnASite(unittest.TestCase):
	def setUp(self):
		if not _connected_site():
			self.skipTest("needs a bench site with the app installed (bench run-tests)")
		from frappe.utils.password import get_decrypted_password, set_encrypted_password

		from retail_tower_erpnext_connector.connector import credential_slots

		self.slots = credential_slots
		self.secret_of = lambda field: get_decrypted_password(
			SETTINGS, SETTINGS, field, raise_exception=False
		)
		frappe.flags[NO_COMMIT] = True
		self.addCleanup(self._rollback)
		set_encrypted_password(SETTINGS, SETTINGS, OLD, "dp2_token")
		frappe.db.set_single_value(
			SETTINGS,
			{
				"dp2_token": "*" * len(OLD),
				"dp2_connector_registration_id": REGISTRATION,
				"dp2_credential_id": "cred-old",
				"dp2_current_credential_issued_at_utc": None,
				"dp2_pending_token": None,
				"dp2_pending_credential_id": None,
				"dp2_pending_credential_issued_at": None,
				"dp2_pending_credential_expires_at": None,
			},
		)

	def _rollback(self):
		frappe.db.rollback()
		frappe.flags.pop(NO_COMMIT, None)

	def _stage(self, credential_id="cred-new", issued_at=T1, secret=NEW):
		return self.slots.stage_pending(
			registration_id=REGISTRATION,
			credential_id=credential_id,
			issued_at=issued_at,
			expires_at="2027-02-25T10:00:00Z",
			secret=secret,
		)

	def _single(self, field):
		"""The stored value itself, past any per-request value cache."""
		rows = frappe.db.sql(
			"select value from `tabSingles` where doctype=%s and field=%s", (SETTINGS, field)
		)
		return rows[0][0] if rows else None

	def test_the_migrated_settings_carry_the_pending_slot(self):
		self.assertTrue(self.slots.has_pending_slot())

	def test_staging_writes_only_the_pending_slot(self):
		self.assertEqual(self._stage(), self.slots.WRITE)
		self.assertEqual(self.secret_of("dp2_pending_token"), NEW)
		self.assertEqual(self.secret_of("dp2_token"), OLD)
		self.assertEqual(self._single("dp2_pending_credential_id"), "cred-new")
		self.assertEqual(self._single("dp2_pending_credential_issued_at"), T1)
		self.assertEqual(self._single("dp2_credential_id"), "cred-old")
		pending = self.slots.read_pending(frappe.get_doc(SETTINGS))
		self.assertEqual((pending.credential_id, pending.secret), ("cred-new", NEW))

	def test_the_compare_and_set_on_a_real_site(self):
		self._stage(credential_id="cred-2", issued_at=T2, secret=NEWER)
		self.assertEqual(self._stage(), self.slots.STALE)  # older than the staged one
		self.assertEqual(self._stage(credential_id="cred-2", issued_at=T2, secret=NEWER), self.slots.NOOP)
		self.assertEqual(self.secret_of("dp2_pending_token"), NEWER)

	def test_promotion_moves_pending_to_current(self):
		self._stage()
		pending = self.slots.read_pending(frappe.get_doc(SETTINGS))
		self.assertTrue(self.slots.promote_pending(pending))
		self.assertEqual(self.secret_of("dp2_token"), NEW)
		self.assertIsNone(self.secret_of("dp2_pending_token"))
		self.assertEqual(self._single("dp2_credential_id"), "cred-new")
		self.assertEqual(self._single("dp2_current_credential_issued_at_utc"), T1)  # the exact fence
		self.assertFalse(self._single("dp2_pending_credential_id"))
		self.assertEqual(self.slots.reload_current_token(), NEW)

	def test_an_earlier_credential_is_never_staged_after_a_later_one_was_promoted(self):
		# The CURRENT fence is the exact UTC issue time the promotion wrote.
		self._stage(credential_id="cred-B", issued_at="2026-11-27T10:05:00Z", secret=NEWER)
		self.assertTrue(self.slots.promote_pending(self.slots.read_pending(frappe.get_doc(SETTINGS))))
		self.assertEqual(self._single("dp2_current_credential_issued_at_utc"), "2026-11-27T10:05:00Z")
		self.assertEqual(self._stage(credential_id="cred-A", issued_at=T1), self.slots.STALE)
		self.assertEqual(  # one second before CURRENT, by the exact fence
			self._stage(credential_id="cred-A2", issued_at="2026-11-27T10:04:59Z"), self.slots.STALE
		)
		self.assertIsNone(self.secret_of("dp2_pending_token"))
		self.assertEqual(
			self._stage(credential_id="cred-C", issued_at="2026-11-27T10:06:00Z"), self.slots.WRITE
		)

	def test_a_promotion_that_fails_part_way_rolls_back_to_both_slots(self):
		self._stage()
		pending = self.slots.read_pending(frappe.get_doc(SETTINGS))
		with mock.patch(
			"frappe.utils.password.remove_encrypted_password", side_effect=RuntimeError("injected")
		):
			with self.assertRaises(RuntimeError):
				self.slots.promote_pending(pending)
		self.assertEqual(self.secret_of("dp2_token"), OLD)
		self.assertEqual(self.secret_of("dp2_pending_token"), NEW)
		self.assertEqual(self._single("dp2_credential_id"), "cred-old")
		self.assertEqual(self._single("dp2_pending_credential_id"), "cred-new")
