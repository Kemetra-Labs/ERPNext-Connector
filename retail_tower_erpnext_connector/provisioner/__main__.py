# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""``python -m retail_tower_erpnext_connector.provisioner --config <file> [--once]``.

The only argument is the path of the config file; there is no argument, environment variable or
option that carries a secret. The process runs in the bench's Python environment on the ERP host
(its container and secret store are RT-1360)."""

from __future__ import annotations

import argparse
import logging
import sys

from .bench import SubprocessBench
from .config import ProvisionerConfig, load_config
from .engine import ProvisioningEngine
from .errors import AuthRejected, CredentialFileError, scrub
from .feed_client import FeedClient
from .gateway import BenchGateway
from .lock import FileLock
from .runner import ProvisionerRunner
from .transport import ProvisionerTransport

LOG = logging.getLogger("rt.provisioner")
_EXIT_AUTH = 3
_EXIT_FATAL = 4


def parse_args(argv: list[str]) -> argparse.Namespace:
	parser = argparse.ArgumentParser(prog="rt-provisioner", description="Retail Tower ERP tenant provisioner")
	parser.add_argument("--config", required=True, help="path of the JSON config file")
	parser.add_argument("--once", action="store_true", help="run one pull pass and exit")
	return parser.parse_args(argv)


class _ScrubFilter(logging.Filter):
	def filter(self, record: logging.LogRecord) -> bool:
		record.msg = scrub(record.getMessage(), 2000)
		record.args = ()
		return True


def install_log_scrubbing() -> None:
	logger = logging.getLogger("rt.provisioner")
	if not any(isinstance(f, _ScrubFilter) for f in logger.filters):
		logger.addFilter(_ScrubFilter())


def build_runner(config: ProvisionerConfig) -> ProvisionerRunner:
	feed = FeedClient(ProvisionerTransport(config.base_url, config.credential_file))
	gateway = BenchGateway(config, SubprocessBench())
	return ProvisionerRunner(
		ProvisioningEngine(gateway, feed, config), feed, FileLock(config.effective_lock_path)
	)


def main(argv: list[str] | None = None) -> int:
	args = parse_args(sys.argv[1:] if argv is None else argv)
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
	install_log_scrubbing()
	config = load_config(args.config)
	runner = build_runner(config)
	try:
		if args.once:
			LOG.info("pass finished: %s", runner.run_once())
		else:
			runner.run_forever(config.poll_interval_seconds)
	except AuthRejected:
		LOG.error("the provisioner credential was rejected: provisioning stopped (posting is unaffected)")
		return _EXIT_AUTH
	except CredentialFileError as exc:
		LOG.error("provisioning stopped: %s", exc)
		return _EXIT_FATAL
	except Exception as exc:
		LOG.error("provisioning stopped on an unexpected %s", type(exc).__name__)
		return _EXIT_FATAL
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
