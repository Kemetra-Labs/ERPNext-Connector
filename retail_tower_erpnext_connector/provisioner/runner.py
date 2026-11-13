# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""The pull loop: take the bench lock, pull once, work the supported requests one at a time.

A request whose lease is lost or too short is left alone (it is offered again when its lease
expires). An invalid provisioner credential (``AuthRejected``) is fatal: provisioning stops and
posting is unaffected. Pulling does not take a lease; the first acknowledgement does."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from .engine import ProvisioningEngine
from .errors import AttemptCollision, FeedUnavailable, LeaseLost, LeaseTooShort, ProtocolError
from .feed_client import FeedClient, parse_time
from .lock import FileLock, LockHeld

LOG = logging.getLogger("rt.provisioner")
_PAGE_SIZE = 50
_MAX_PAGES = 10


class ProvisionerRunner:
	def __init__(
		self,
		engine: ProvisioningEngine,
		feed: FeedClient,
		lock: FileLock,
		*,
		clock: Callable[[], datetime] = lambda: datetime.now(UTC),
		sleep: Callable[[float], None] = time.sleep,
	) -> None:
		self._engine = engine
		self._feed = feed
		self._lock = lock
		self._clock = clock
		self._sleep = sleep

	def run_once(self) -> dict:
		try:
			self._lock.acquire()
		except LockHeld:
			LOG.info("another provisioner run holds the bench lock")
			return {"lock": "held", "results": {}}
		try:
			try:
				items = self._pull_all()
			except (FeedUnavailable, ProtocolError) as exc:
				LOG.warning("pull failed: %s", type(exc).__name__)
				return {"lock": "acquired", "results": {}, "error": type(exc).__name__}
			return {"lock": "acquired", "results": self._work(items)}
		finally:
			self._lock.release()

	def _pull_all(self) -> list:
		"""Every offered request, following the cursor: requests this provisioner skips without an
		acknowledgement (other kinds) stay offered at the head, so page one alone could hide a
		tenant request behind them."""
		seen: dict[str, object] = {}
		cursor = None
		for _ in range(_MAX_PAGES):
			page = self._feed.pull(cursor=cursor, limit=_PAGE_SIZE)
			if not page.items:
				break
			for item in page.items:
				seen.setdefault(item.id, item)
			if not page.cursor or page.cursor == cursor:
				break  # no cursor to advance with: asking again would return the first page again
			cursor = page.cursor
		return list(seen.values())

	def _due(self, request) -> bool:
		if request.not_before is None:
			return True
		try:
			return parse_time(request.not_before) <= self._clock()
		except ValueError:
			return False  # an unreadable backoff time is treated as still backing off

	def _work(self, items) -> dict[str, str]:
		results: dict[str, str] = {}
		for request in filter(self._due, items):
			try:
				results[request.id] = self._engine.process(request)
			except (LeaseLost, LeaseTooShort) as exc:
				LOG.warning("request %s left for the next pull: %s", request.id, type(exc).__name__)
				results[request.id] = "released"
			except (ProtocolError, AttemptCollision) as exc:
				# One request the feed answers unexpectedly must not stop the others or the process.
				LOG.error("request %s skipped: %s", request.id, type(exc).__name__)
				results[request.id] = "error"
			except FeedUnavailable:
				LOG.warning("Backend-Core unavailable; stopping this pass")
				break
		return results

	def run_forever(self, poll_interval_seconds: int) -> None:
		while True:
			self.run_once()
			self._sleep(poll_interval_seconds)
