# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Per-request acknowledgement session: monotonic attempt numbers above the server's floor, safe
retry of a lost response, and the remaining lease. A replayed acknowledgement does not renew the
lease, so nothing here re-acks just to keep it alive."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from .errors import AttemptCollision, FeedUnavailable
from .feed_client import FeedClient, ProvisioningRequest

_RETRIES = 3
_BACKOFF_S = (1, 2, 4)


class AckSession:
	def __init__(
		self,
		client: FeedClient,
		request: ProvisioningRequest,
		*,
		clock: Callable[[], datetime] = lambda: datetime.now(UTC),
		sleep: Callable[[float], None] = time.sleep,
	) -> None:
		self._client = client
		self._request = request
		self._clock = clock
		self._sleep = sleep
		self._attempt = request.attempt_floor  # the next attempt must be greater than the floor
		self.lease_expires_at: datetime | None = None

	def _call(self, send: Callable[[int], object]):
		"""Send with a fresh attempt number; repeat the SAME attempt after a lost response, and move
		to a new one when the server says the number was used with another body."""
		self._attempt += 1
		collisions = 0
		lost = 0
		while True:
			try:
				return send(self._attempt)
			except FeedUnavailable:
				lost += 1
				if lost > _RETRIES:
					raise
				self._sleep(_BACKOFF_S[lost - 1])
			except AttemptCollision:
				collisions += 1
				if collisions > _RETRIES:
					raise
				self._attempt += 1

	def ack(self, step: str, outcome: str, *, reason: str | None = None, detail: str | None = None) -> None:
		self.lease_expires_at = self._call(
			lambda n: self._client.ack(self._request.id, step, outcome, n, reason=reason, detail=detail)
		)

	def complete(self, outcome: str, *, reason: str | None = None, result: dict | None = None) -> None:
		self._call(
			lambda n: self._client.complete(self._request.id, outcome, n, reason=reason, result=result)
		)

	def remaining_seconds(self) -> float:
		if self.lease_expires_at is None:
			return 0.0
		return (self.lease_expires_at - self._clock()).total_seconds()
