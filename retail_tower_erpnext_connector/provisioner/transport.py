# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""The provisioner's HTTP transport to Backend-Core: the Connector's shared ``RequestsTransport``
(``connector/http_transport.py``, the one module that imports an HTTP library), built per request.

The platform-scope provisioner credential is read from a FILE at every request (OD-3: an
owner-provisioned secret store on the host), so rotation replaces the file and never needs a restart
or a changed argument. It lives in a local only for the duration of one call: never on this object,
never in a log, never in arguments or the environment."""

from __future__ import annotations

import os
from collections.abc import Callable

from ..connector.http_transport import HttpTransport, RequestsTransport
from .errors import CredentialFileError


def read_credential_file(path: str) -> str:
	"""The token in ``path``. On POSIX the file is opened without following a symlink, checked on the
	open descriptor (a regular file, owned by this user, no group or other access) and read from that
	same descriptor, so the checks and the read cannot be separated by a swap."""
	try:
		if os.name != "posix":
			with open(path, encoding="utf-8") as handle:
				token = handle.read().strip()
		else:
			fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
			try:
				info = os.fstat(fd)
				if (
					(info.st_mode & 0o170000) != 0o100000
					or info.st_mode & 0o077
					or info.st_uid != os.geteuid()
				):
					raise CredentialFileError(
						"the provisioner credential file must be a private file of this user"
					)
				with os.fdopen(fd, encoding="utf-8", closefd=False) as handle:
					token = handle.read().strip()
			finally:
				os.close(fd)
	except OSError as exc:
		raise CredentialFileError(
			f"the provisioner credential file cannot be read: {type(exc).__name__}"
		) from None
	if not token:
		raise CredentialFileError("the provisioner credential file is empty")
	return token


class ProvisionerTransport:
	def __init__(
		self,
		base_url: str,
		credential_file: str,
		*,
		factory: Callable[[str, str], HttpTransport] = RequestsTransport,
	) -> None:
		self._base = base_url
		self._credential_file = credential_file
		self._factory = factory

	def __repr__(self) -> str:
		return f"ProvisionerTransport(base_url={self._base!r})"

	def _build(self) -> HttpTransport:
		return self._factory(self._base, read_credential_file(self._credential_file))

	def get(self, path: str, *, params: dict, headers: dict) -> dict:
		return self._build().get(path, params=params, headers=headers)

	def post(self, path: str, *, json: dict | None, headers: dict) -> object:
		return self._build().post(path, json=json, headers=headers)
