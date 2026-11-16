# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1429 — the auth circuit breaker: a refused Backend-Core credential is a state, not a per-tick error.

The breaker is pure (no frappe): the state sits behind an injected store and the clock is injected, so
every transition here is deterministic.
"""

import pytest

from retail_tower_erpnext_connector.connector.auth_circuit import (
	AuthCircuit,
	AuthCircuitOpen,
	AuthRefused,
	MemoryCircuitStore,
)


class _Clock:
	def __init__(self, now=1_000.0):
		self.now = now

	def __call__(self):
		return self.now


def _circuit(*, key="", base_s=60.0, cap_s=900.0, store=None, clock=None):
	clock = clock or _Clock()
	store = store if store is not None else MemoryCircuitStore()
	return AuthCircuit(store, clock=clock, credential_key=key, base_s=base_s, cap_s=cap_s), clock, store


def test_a_new_circuit_is_closed_and_lets_calls_through():
	circuit, _, _ = _circuit()
	assert circuit.blocked() is False
	circuit.acquire()  # does not raise


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_refusal_opens_the_circuit_and_reports_the_transition_once(status):
	circuit, _, _ = _circuit()
	assert circuit.record_refusal(status) is True
	assert circuit.blocked() is True
	with pytest.raises(AuthCircuitOpen):
		circuit.acquire()


def test_while_open_no_call_passes_before_the_backoff_ends():
	circuit, clock, _ = _circuit(base_s=60)
	circuit.record_refusal(401)
	for _ in range(10):
		clock.now += 5
		with pytest.raises(AuthCircuitOpen):
			circuit.acquire()
	assert circuit.blocked() is True


def test_after_the_backoff_exactly_one_caller_gets_the_probe():
	circuit, clock, _ = _circuit(base_s=60)
	circuit.record_refusal(401)
	clock.now += 61
	assert circuit.blocked() is False  # a probe is due
	circuit.acquire()  # this caller claims the probe
	assert circuit.blocked() is True  # the next caller is held back
	with pytest.raises(AuthCircuitOpen):
		circuit.acquire()


def test_a_failed_probe_doubles_the_backoff_up_to_the_cap_and_is_not_a_new_transition():
	circuit, clock, _ = _circuit(base_s=60, cap_s=900)
	assert circuit.record_refusal(401) is True
	waits = []
	for _ in range(7):
		before = clock.now
		clock.now = circuit.next_probe_at()
		circuit.acquire()
		assert circuit.record_refusal(401) is False  # still the same incident
		waits.append(circuit.next_probe_at() - clock.now)
		assert clock.now >= before
	assert waits == [120, 240, 480, 900, 900, 900, 900]


def test_a_success_closes_an_open_circuit_and_reports_the_transition_once():
	circuit, clock, _ = _circuit()
	circuit.record_refusal(401)
	clock.now += 61
	circuit.acquire()
	assert circuit.record_success() is True
	assert circuit.blocked() is False
	assert circuit.record_success() is False  # already closed


def test_a_success_on_a_closed_circuit_is_not_a_transition():
	circuit, _, _ = _circuit()
	assert circuit.record_success() is False


def test_reset_closes_the_circuit_at_once_even_inside_the_backoff():
	# The owner pastes a re-issued token: the next tick must call Backend-Core, not wait out the backoff.
	circuit, _, _ = _circuit(base_s=900)
	circuit.record_refusal(401)
	assert circuit.blocked() is True
	circuit.reset()
	assert circuit.blocked() is False
	circuit.acquire()


def test_100_refusals_are_one_incident():
	circuit, clock, _ = _circuit()
	opened = 0
	for _ in range(100):
		if circuit.record_refusal(401):
			opened += 1
		clock.now += 1
	assert opened == 1


def test_a_state_recorded_under_another_credential_is_ignored():
	store = MemoryCircuitStore()
	old, clock, _ = _circuit(key="cred-old", store=store)
	old.record_refusal(401)
	new, _, _ = _circuit(key="cred-new", store=store, clock=clock)
	assert new.blocked() is False
	new.acquire()


def test_the_state_is_shared_through_the_store():
	# Every tick is its own RQ job: a second circuit over the same store sees the first one's refusal.
	store = MemoryCircuitStore()
	first, clock, _ = _circuit(store=store)
	first.record_refusal(401)
	second, _, _ = _circuit(store=store, clock=clock)
	assert second.blocked() is True


def test_the_errors_never_carry_a_secret():
	refused = AuthRefused(401)
	assert str(refused) == "Backend-Core refused the Connector credential (HTTP 401)"
	assert refused.status == 401
	circuit, _, _ = _circuit()
	circuit.record_refusal(401)
	with pytest.raises(AuthCircuitOpen) as held:
		circuit.acquire()
	assert "Bearer" not in str(held.value)
