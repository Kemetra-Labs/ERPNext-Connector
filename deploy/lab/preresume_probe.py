"""RT-927: the pre-resume probe of the ERP lab deploy (design RT-926 v2, section 4 step 4).

Runs inside the backend container, from `sites/`, while the scheduler and the queues are still
stopped:

    ../env/bin/python /tmp/preresume_probe.py --site S --expected-version V

It answers one question before posting may resume: is this deployment able to post exactly once?
Four checks, each failing closed (an exception is a failed check, never a pass):

  compatibility  Connector Settings `compatibility_status` is exactly `OK`. WARNING also posts, but
                 a deploy must not start posting on a site that already warns; the owner decides.
  g5             the Gate G5 unique indexes exist (`index_guard.evaluate(schema.index_present)`).
  version        the installed Connector is the expected version.
  tick           `scheduler_tick_interval` is 60 s, the posting cadence the poller needs (RT-494).

Output contract: stdout carries only `RT_PROBE <name>=pass|fail` lines. Nothing else is printed
(no exception text: it may carry connection details). Exit 0 when every check passes, 1 when any
fails or the context cannot be built, 2 on bad arguments.

`frappe` is imported only inside the context factory, so the tests run without Frappe.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

POSTING_TICK_S = 60
SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class ProbeContext(Protocol):
    def compatibility_status(self) -> str | None: ...

    def index_guard_ok(self) -> bool: ...

    def connector_version(self) -> str: ...

    def scheduler_tick_interval(self) -> int | None: ...


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool


def _safe(check: Callable[[], bool]) -> bool:
    try:
        return bool(check())
    except Exception:
        return False


def evaluate(ctx: ProbeContext, expected_version: str) -> list[Check]:
    return [
        Check("compatibility", _safe(lambda: ctx.compatibility_status() == "OK")),
        Check("g5", _safe(ctx.index_guard_ok)),
        Check("version", _safe(lambda: ctx.connector_version() == expected_version)),
        Check("tick", _safe(lambda: ctx.scheduler_tick_interval() == POSTING_TICK_S)),
    ]


class FrappeContext:
    """The real context: a connected Frappe site. Imports only here."""

    def __init__(self, site: str) -> None:
        import frappe

        frappe.init(site=site, sites_path=".")
        frappe.connect()
        self._frappe = frappe

    def compatibility_status(self) -> str | None:
        return self._frappe.db.get_single_value("Connector Settings", "compatibility_status")

    def index_guard_ok(self) -> bool:
        from retail_tower_erpnext_connector.connector import schema
        from retail_tower_erpnext_connector.connector.posting import index_guard

        return index_guard.evaluate(schema.index_present).ok

    def connector_version(self) -> str:
        import retail_tower_erpnext_connector

        return retail_tower_erpnext_connector.__version__

    def scheduler_tick_interval(self) -> int | None:
        # common_site_config.json is merged into frappe.conf; parsed like the Connector's own
        # compatibility glue (a non-positive or unparseable value is "unset").
        try:
            value = int(float(self._frappe.conf.get("scheduler_tick_interval") or 0))
        except (TypeError, ValueError, OverflowError):
            return None
        return value if value > 0 else None

    def close(self) -> None:
        self._frappe.destroy()


def _frappe_context(site: str) -> ProbeContext:
    return FrappeContext(site)


def _site(value: str) -> str:
    if not SITE_RE.fullmatch(value) or ".." in value:
        raise argparse.ArgumentTypeError("bad site name")
    return value


def _version(value: str) -> str:
    if not VERSION_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("bad version")
    return value


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--site", required=True, type=_site)
    parser.add_argument("--expected-version", required=True, type=_version)
    args = parser.parse_args(argv)
    try:
        ctx = _frappe_context(args.site)
    except Exception as exc:
        print("RT_PROBE context=fail")
        print(f"preresume_probe: cannot build the context ({type(exc).__name__})", file=sys.stderr)
        return 1
    try:
        checks = evaluate(ctx, args.expected_version)
    finally:
        close = getattr(ctx, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass  # the verdict is already computed; a failed disconnect must not change it
    for check in checks:
        print(f"RT_PROBE {check.name}={'pass' if check.ok else 'fail'}")
    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
