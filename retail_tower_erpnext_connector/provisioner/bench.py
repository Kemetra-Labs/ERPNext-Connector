# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Bench invocations: an argument list, never a shell, never a prompt (stdin is closed unless the
caller pipes a JSON spec), always a timeout. The caller never puts a secret in the arguments or in
the environment this module hands on (the one exception is the throwaway Administrator password of
``new-site``, see ``gateway.py``); a timeout error therefore never carries the argument list."""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Protocol

_POSIX = os.name == "posix"


@dataclass(frozen=True)
class RunResult:
	returncode: int
	stdout: str
	stderr: str


class BenchRunner(Protocol):
	def run(self, argv: list[str], *, stdin: bytes | None = None, timeout: int, cwd: str) -> RunResult: ...


class SubprocessBench:
	def run(self, argv: list[str], *, stdin: bytes | None = None, timeout: int, cwd: str) -> RunResult:
		# A new session on POSIX, so a timeout can kill the whole process group: bench starts Frappe
		# as a child, and a surviving grandchild would keep running while the engine retries.
		proc = subprocess.Popen(
			argv,
			stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			cwd=cwd,
			shell=False,
			start_new_session=_POSIX,
		)
		try:
			out, err = proc.communicate(input=stdin, timeout=timeout)
		except subprocess.TimeoutExpired:
			self._kill(proc)
			proc.communicate()
			raise TimeoutError(
				f"bench command timed out after {timeout}s"
			) from None  # argv may hold a secret
		return RunResult(
			proc.returncode,
			out.decode("utf-8", errors="replace"),
			err.decode("utf-8", errors="replace"),
		)

	@staticmethod
	def _kill(proc: subprocess.Popen) -> None:
		if _POSIX:
			try:
				os.killpg(proc.pid, signal.SIGKILL)
				return
			except ProcessLookupError:
				return
		proc.kill()
