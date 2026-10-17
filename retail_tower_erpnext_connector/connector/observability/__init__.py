# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-635 (OBS-6) — the Connector's own error reporting, per the OBS-1 contract (RT-629).

``scrubber`` holds the pure privacy baseline (OBS-1 §3); ``sentry_client`` the Connector-only Sentry
client used by the poller ticks. Frappe's built-in Sentry integration and its global hub are never
touched.
"""
