# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1431 — what an operator may see of the auth circuit: state, last status, opened at, next probe.

Pure: no frappe, no network, no clock of its own. It turns the RT-1429 cache record into a read-only view and
NEVER writes: it does not go through :class:`~.auth_circuit.AuthCircuit`, whose ``_state()`` clears a record
that belongs to another credential (a write). A view must not change the thing it shows.

The record's ``key`` (the credential id plus a one-way fingerprint of the token) is compared with the
configured credential and then dropped. It is a token-derived value, so it is never part of the view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .auth_circuit import CircuitState

STATE_CLOSED = "closed"
STATE_OPEN = "open"
NOTE_PROBE_DUE = "A probe is due: the next tick calls Backend-Core once."
NOTE_OTHER_CREDENTIAL = (
	"The recorded refusal belongs to a previous credential. The next tick clears it and calls Backend-Core."
)


@dataclass(frozen=True)
class AuthCircuitStatus:
	"""One read of the circuit. Times are timezone-aware UTC; a closed circuit has no last status or times."""

	state: str
	last_status: int | None = None
	failures: int | None = None
	opened_at: datetime | None = None
	next_probe_at: datetime | None = None
	note: str = ""

	def to_dict(self) -> dict:
		"""JSON-safe form for ``bench execute``: ISO-8601 UTC strings, no secret and no key."""
		return {
			"state": self.state,
			"last_status": self.last_status,
			"failures": self.failures,
			"opened_at": _iso(self.opened_at),
			"next_probe_at": _iso(self.next_probe_at),
			"note": self.note,
		}


def _iso(moment: datetime | None) -> str | None:
	return None if moment is None else moment.isoformat()


def _utc(epoch_s: float) -> datetime | None:
	try:
		return datetime.fromtimestamp(epoch_s, UTC)
	except OverflowError, OSError, ValueError:
		return None  # a damaged record shows no time, never an error


def build_status(state: CircuitState | None, *, current_key: str, now: float) -> AuthCircuitStatus:
	"""The view of ``state`` for the credential named by ``current_key`` at epoch second ``now``.

	Nothing recorded reads as closed (a recovery and a cache flush both clear the record, so a closed
	circuit has no last status or opened-at time to show). ``opened_at`` is the first refusal of the run: a
	later 403 after a 401 updates ``last_status`` but not ``opened_at``. A record for another credential also reads as
	closed, with a note: the tick that next runs clears it.
	"""
	if state is None:
		return AuthCircuitStatus(state=STATE_CLOSED)
	if state.key != current_key:
		return AuthCircuitStatus(state=STATE_CLOSED, note=NOTE_OTHER_CREDENTIAL)
	return AuthCircuitStatus(
		state=STATE_OPEN,
		last_status=state.status,
		failures=state.failures,
		opened_at=_utc(state.opened_at),
		next_probe_at=_utc(state.next_probe_at),
		note=NOTE_PROBE_DUE if now >= state.next_probe_at else "",
	)
