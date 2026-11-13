# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Bench commands of the tenant provisioner (RT-1359). Frappe loads this module's ``commands`` list
from every app in the bench; it is the supported extension point for a bench command (no hook).

* ``rt-provision-site-step`` runs one site-side operation inside the site process. The JSON spec
  (operation, arguments and, for the credential bind only, the secret) arrives on STDIN, so no
  secret ever appears in a command line or the environment. One ``RTPROV:`` JSON line comes out.
* ``rt-provision-queue-depth`` prints the bench's deepest background-job queue (no site needed).
"""

import json
import sys

import click
from frappe.commands import get_site, pass_context

RESULT_PREFIX = "RTPROV:"


def _emit(result: dict) -> None:
	click.echo(RESULT_PREFIX + json.dumps(result))


@click.command("rt-provision-site-step")
@pass_context
def rt_provision_site_step(context):
	"""Run one provisioning operation on --site; the JSON spec is read from stdin."""
	import frappe

	from retail_tower_erpnext_connector.provisioner import site_ops

	spec = json.loads(sys.stdin.read())
	frappe.init(site=get_site(context))
	frappe.connect()
	try:
		result = site_ops.run(spec)
	except site_ops.OpError as err:
		frappe.db.rollback()
		result = {"error": {"class": err.error_class, "reason": err.reason}}
	finally:
		spec.pop("secret", None)
		frappe.destroy()
	_emit(result)


@click.command("rt-provision-queue-depth")
def rt_provision_queue_depth():
	"""Print the deepest background-job queue of this bench."""
	from retail_tower_erpnext_connector.provisioner import site_ops

	_emit(site_ops.queue_depth())


commands = [rt_provision_site_step, rt_provision_queue_depth]
