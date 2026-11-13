# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""A one-at-a-time lock for the bench: an OS file lock, released by the OS if the process dies."""

from __future__ import annotations

import os
from pathlib import Path

try:  # POSIX
	import fcntl

	def _lock(fd: int) -> None:
		fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

	def _unlock(fd: int) -> None:
		fcntl.flock(fd, fcntl.LOCK_UN)

except ImportError:  # Windows (developer machines only; the provisioner runs on Linux)
	import msvcrt

	def _lock(fd: int) -> None:
		msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

	def _unlock(fd: int) -> None:
		msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class LockHeld(Exception):
	"""Another provisioner run holds the bench lock."""


class FileLock:
	def __init__(self, path: str | Path) -> None:
		self._path = str(path)
		self._fd: int | None = None

	def acquire(self) -> None:
		fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
		try:
			_lock(fd)
		except OSError as exc:
			os.close(fd)
			raise LockHeld(self._path) from exc
		self._fd = fd

	def release(self) -> None:
		if self._fd is not None:
			try:
				_unlock(self._fd)
			finally:
				os.close(self._fd)
				self._fd = None

	def __enter__(self) -> FileLock:
		self.acquire()
		return self

	def __exit__(self, *exc) -> None:
		self.release()
