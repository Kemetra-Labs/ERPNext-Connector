# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Connector liveness heartbeat — the connector side of Backend-Core spec 020 (RT-564).

The connector POSTs a periodic heartbeat to Backend-Core's ``connectorReportHeartbeat``
(``erpnext-connector/connector-health.yaml``) over its existing machine credential, so the
operator read (``listConnectorHealth``) moves from ``never_seen`` to ``healthy`` / ``stale``.
Backend-Core owns the liveness clock and verdict; the connector only reports that it is alive
plus the contract's optional self-reported fields. The heartbeat is its own scheduled job and
fails quietly: it never blocks or alters posting.
"""
