# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1359 — the provisioner's constants against the pinned contract ``provisioning-feed.yaml``.

The fake feed in these tests is hand-written; this keeps the names the client sends (paths, step
names, error classes) tied to the contract file so a contract change fails here, not on a host.
Skips when the monorepo contract file is not in the checkout (a staged standalone subtree)."""

import re
from pathlib import Path

import pytest

from retail_tower_erpnext_connector.provisioner import feed_client
from retail_tower_erpnext_connector.provisioner.errors import ErrorClass

CONTRACT = next(
	(
		p / "services/backend-core/packages/contracts/openapi/erpnext-connector/provisioning-feed.yaml"
		for p in Path(__file__).resolve().parents
		if (
			p / "services/backend-core/packages/contracts/openapi/erpnext-connector/provisioning-feed.yaml"
		).exists()
	),
	None,
)
pytestmark = pytest.mark.skipif(CONTRACT is None, reason="the monorepo contract file is not in this checkout")

STEPS = ("claimed", "site_created", "erp_configured", "connector_installed", "credential_bound", "verified")


def _enum(text: str, schema: str) -> list[str]:
	match = re.search(rf"^    {schema}:\n(?:.*\n)*?      enum: \[([^\]]+)\]", text, re.M)
	assert match, f"enum of {schema} not found"
	return [v.strip() for v in match.group(1).split(",")]


@pytest.fixture(scope="module")
def text() -> str:
	return CONTRACT.read_text(encoding="utf-8")


def test_the_step_names_are_the_contracts(text):
	assert tuple(_enum(text, "StepName")) == STEPS


def test_the_error_classes_are_the_contracts(text):
	assert _enum(text, "ErrorClass") == [e.value for e in ErrorClass]
	assert _enum(text, "StepOutcome") == ["done", *[e.value for e in ErrorClass]]


def test_the_terminal_outcomes_are_the_contracts(text):
	assert re.search(r"enum: \[succeeded, permanently_rejected, conflict\]", text)


def test_the_paths_are_the_contracts(text):
	for const in ("_PULL_PATH", "_ACK_PATH", "_CREDENTIAL_PATH", "_OUTCOME_PATH"):
		path = getattr(feed_client, const).replace("{request_id}", "{requestId}")
		assert f"  {path}:" in text


def test_the_operation_ids_are_the_contracts(text):
	for op in (
		"connectorPullProvisioningRequests",
		"connectorAckProvisioningStep",
		"connectorIssueProvisionedCredential",
		"connectorCompleteProvisioning",
	):
		assert f"operationId: {op}" in text
