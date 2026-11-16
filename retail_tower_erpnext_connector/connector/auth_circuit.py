# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1429 — the auth circuit: a refused Backend-Core credential is a state, not a per-tick error.

When Backend-Core answers 401 or 403 to the Connector's bearer token, every poller sharing that token
fails the same way, every minute, until an operator fixes the credential (RT-1428: an expired token made
about 1,200 Sentry events in 20 hours). This module turns the repeated refusal into one incident:

* the first refusal OPENS the circuit (the caller reports that transition, once);
* while it is open no call leaves the process, except one PROBE per backoff window (60 s, doubling up to
  15 min). A probe that is refused again only extends the window; one that is accepted CLOSES it;
* saving Connector Settings (an operator pasting a re-issued token) resets it at once, so the fix is
  not held back by a long window.

Pure: no frappe, no network. The state sits behind :class:`CircuitStore` (the repo's Repository pattern;
the live store is the site's shared Redis because every tick is its own RQ job) and the clock is
injected, so every transition is unit-tested. Nothing here ever holds or prints a credential.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

AUTH_REFUSAL_STATUSES = frozenset({401, 403})
BASE_BACKOFF_S = 60.0
# Without an operator save (a token changed outside the form) a probe still runs within this window.
CAP_BACKOFF_S = 900.0


class AuthRefused(Exception):
	"""Backend-Core answered 401 or 403: the Connector credential is refused (not a per-item rule).

	``reported`` is True when an auth circuit saw the refusal, so the incident is reported through the
	circuit (once) and the tick must not report it again. The message carries the status only.
	"""

	def __init__(self, status: int, *, reported: bool = False) -> None:
		super().__init__(f"Backend-Core refused the Connector credential (HTTP {status})")
		self.status = status
		self.reported = reported


class AuthCircuitOpen(Exception):
	"""The circuit is open and no probe is due: the call was NOT made."""

	def __init__(self, retry_in_s: float = 0.0) -> None:
		super().__init__(f"Backend-Core credential refused; next probe in {max(0, round(retry_in_s))} s")
		self.retry_in_s = retry_in_s


@dataclass(frozen=True)
class CircuitState:
	key: str
	failures: int
	status: int
	opened_at: float
	next_probe_at: float

	def to_wire(self) -> dict:
		return {
			"key": self.key,
			"failures": self.failures,
			"status": self.status,
			"opened_at": self.opened_at,
			"next_probe_at": self.next_probe_at,
		}

	@staticmethod
	def from_wire(raw: object) -> CircuitState | None:
		if not isinstance(raw, dict):
			return None
		try:
			return CircuitState(
				key=str(raw["key"]),
				failures=int(raw["failures"]),
				status=int(raw["status"]),
				opened_at=float(raw["opened_at"]),
				next_probe_at=float(raw["next_probe_at"]),
			)
		except KeyError, TypeError, ValueError:
			return None  # a damaged record reads as "closed": never block posting on bad state


class CircuitStore(Protocol):
	def load(self) -> CircuitState | None: ...

	def save(self, state: CircuitState) -> None: ...

	def clear(self) -> None: ...


class MemoryCircuitStore:
	"""In-process store for tests and for a transport built without shared state."""

	def __init__(self) -> None:
		self._state: CircuitState | None = None

	def load(self) -> CircuitState | None:
		return self._state

	def save(self, state: CircuitState) -> None:
		self._state = state

	def clear(self) -> None:
		self._state = None


class AuthCircuit:
	"""The breaker over one store. ``credential_key`` is a NON-secret id of the configured credential."""

	def __init__(
		self,
		store: CircuitStore,
		*,
		clock: Callable[[], float] = time.time,
		credential_key: str = "",
		base_s: float = BASE_BACKOFF_S,
		cap_s: float = CAP_BACKOFF_S,
	) -> None:
		self._store = store
		self._clock = clock
		self._key = credential_key
		self._base_s = base_s
		self._cap_s = cap_s

	def _state(self) -> CircuitState | None:
		state = self._store.load()
		if state is not None and state.key != self._key:
			self._store.clear()  # recorded for a credential that is no longer the configured one
			return None
		return state

	def _backoff(self, failures: int) -> float:
		return min(self._cap_s, self._base_s * (2 ** max(0, failures - 1)))

	def blocked(self) -> bool:
		"""True while open and no probe is due. A read-only peek: it claims nothing."""
		state = self._state()
		return state is not None and self._clock() < state.next_probe_at

	def next_probe_at(self) -> float | None:
		state = self._state()
		return None if state is None else state.next_probe_at

	def retry_in_s(self) -> float:
		"""Seconds until the next probe is due; 0 when closed or when a probe is due now."""
		state = self._state()
		return 0.0 if state is None else max(0.0, state.next_probe_at - self._clock())

	def acquire(self) -> None:
		"""Let a call through, or raise :class:`AuthCircuitOpen` without it being made.

		When a probe is due the first caller claims it (the window restarts), so concurrent ticks stay
		held back. Not atomic across processes: a rare double probe costs one extra refused call.
		"""
		state = self._state()
		if state is None:
			return
		now = self._clock()
		if now < state.next_probe_at:
			raise AuthCircuitOpen(state.next_probe_at - now)
		self._store.save(
			CircuitState(
				key=state.key,
				failures=state.failures,
				status=state.status,
				opened_at=state.opened_at,
				next_probe_at=now + self._backoff(state.failures),
			)
		)

	def record_refusal(self, status: int) -> bool:
		"""Note a 401/403. True only when this call OPENED the circuit (the one transition to report)."""
		now = self._clock()
		state = self._state()
		failures = 1 if state is None else state.failures + 1
		self._store.save(
			CircuitState(
				key=self._key,
				failures=failures,
				status=status,
				opened_at=now if state is None else state.opened_at,
				next_probe_at=now + self._backoff(failures),
			)
		)
		return state is None

	def record_success(self) -> bool:
		"""Note an accepted call. True only when this call CLOSED an open circuit."""
		if self._state() is None:
			return False
		self._store.clear()
		return True

	def reset(self) -> None:
		"""Close at once (Connector Settings were saved: the credential may have changed)."""
		self._store.clear()
