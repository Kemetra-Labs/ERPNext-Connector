# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""The tenant provisioner (RT-1359, RT-1352 PC-A): a bench-level Connector component that PULLS
provisioning requests from Backend-Core and creates one ERPNext site per tenant.

It is a plain Python orchestrator. It imports no ``frappe``; site-side work runs inside the site
process through the registered bench command (``commands.py`` -> :mod:`.site_ops`), with a JSON spec
on stdin. It adds no whitelisted method, no document override and no hook. It is separate from the
per-site posting path.
"""
