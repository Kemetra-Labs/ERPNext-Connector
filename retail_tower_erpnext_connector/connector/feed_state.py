# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-883 — the pollers' feed state, kept in Connector Settings with the cache as a fast path.

The posting and bin-view pollers used to keep their feed cursor (and the bin-view retry set) only
in ``frappe.cache()``. A Redis flush then sent the connector back to the start of each feed:
posting stayed exactly-once (the Posting Log unique index) but replayed the whole feed.

Each value now lives in a read-only Connector Settings field as well:

* ``save`` writes the field (``set_single_value``, without bumping ``modified``), commits, then
  sets the cache. The value is durable at the same checkpoint the poller already relies on, and
  a failed database write raises before the cache moves, so the cache is never ahead of a write
  that did not happen.
* ``load`` reads the cache first and, on a miss, the field; a value read from the field warms
  the cache again.

The commit is safe at every call site: a poller saves its cursor only once every outcome before
it is durable (the posting glue already commits each posted item before its ack, RT-458).
"""

from __future__ import annotations

import json

import frappe

_SETTINGS = "Connector Settings"


def load(cache_key: str, field: str):
	"""The cached value, or the persisted one after a cache miss (``None`` when neither is set)."""
	value = frappe.cache().get_value(cache_key)
	if value is not None:
		return value
	value = frappe.db.get_single_value(_SETTINGS, field) or None
	if value is not None:
		frappe.cache().set_value(cache_key, value)
	return value


def save(cache_key: str, field: str, value) -> None:
	"""Persist ``value`` in ``field`` and commit, then update the cache."""
	frappe.db.set_single_value(_SETTINGS, field, value, update_modified=False)
	frappe.db.commit()
	frappe.cache().set_value(cache_key, value)


def load_json(cache_key: str, field: str):
	"""``load`` for a structured value: the cache holds the object, the field its JSON text."""
	value = frappe.cache().get_value(cache_key)
	if value is not None:
		return value
	text = frappe.db.get_single_value(_SETTINGS, field)
	if not text:
		return None
	value = json.loads(text)
	frappe.cache().set_value(cache_key, value)
	return value


def save_json(cache_key: str, field: str, value) -> None:
	"""``save`` for a structured value. Key order is kept (the retry set is oldest first)."""
	frappe.db.set_single_value(_SETTINGS, field, json.dumps(value), update_modified=False)
	frappe.db.commit()
	frappe.cache().set_value(cache_key, value)
