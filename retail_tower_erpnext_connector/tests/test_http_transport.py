# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-1259 — ``build_http_transport`` refuses a non-HTTPS Backend-Core URL unless explicitly opted in.

The transport sends ``Authorization: Bearer <dp2_token>``; over plain ``http://`` an on-path observer
reads the token (CWE-319). The builder raises ``ValueError`` (the ticks already skip cleanly on it)
before any request object exists. ``RT_CONNECTOR_ALLOW_INSECURE_DP2=1`` is the development opt-in.
"""

import pytest

from retail_tower_erpnext_connector.connector.http_transport import (
	ALLOW_INSECURE_ENV,
	RequestsTransport,
	build_http_transport,
)


class _Settings:
	def __init__(self, base_url, token="tok"):
		self.dp2_base_url = base_url
		self._token = token

	def get_password(self, fieldname):
		assert fieldname == "dp2_token"
		return self._token


def test_https_builds_a_transport():
	t = build_http_transport(_Settings("https://dp2.example/"), environ={})
	assert isinstance(t, RequestsTransport)


def test_scheme_check_is_case_insensitive_and_trims():
	assert isinstance(
		build_http_transport(_Settings("  HTTPS://dp2.example  "), environ={}), RequestsTransport
	)


def test_http_without_opt_in_raises_before_a_transport_exists(monkeypatch):
	# Nothing is constructed, so there is no object that could send a request.
	built = []
	monkeypatch.setattr(RequestsTransport, "__init__", lambda self, *a, **k: built.append(a))
	with pytest.raises(ValueError, match="https"):
		build_http_transport(_Settings("http://172.20.0.1:3000"), environ={})
	assert built == []


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "2"])
def test_only_exactly_one_opts_in(value):
	with pytest.raises(ValueError, match="https"):
		build_http_transport(_Settings("http://dp2.example"), environ={ALLOW_INSECURE_ENV: value})


def test_http_with_opt_in_builds():
	t = build_http_transport(_Settings("http://172.20.0.1:3000"), environ={ALLOW_INSECURE_ENV: "1"})
	assert isinstance(t, RequestsTransport)


@pytest.mark.parametrize("url", ["ftp://dp2.example", "dp2.example", "//dp2.example", "file:///x"])
def test_other_schemes_are_refused_even_with_opt_in(url):
	with pytest.raises(ValueError):
		build_http_transport(_Settings(url), environ={ALLOW_INSECURE_ENV: "1"})


@pytest.mark.parametrize("url", ["", "   ", None])
def test_unset_url_still_raises(url):
	with pytest.raises(ValueError, match="required"):
		build_http_transport(_Settings(url), environ={})


@pytest.mark.parametrize("token", ["", None])
def test_unset_token_still_raises(token):
	with pytest.raises(ValueError, match="required"):
		build_http_transport(_Settings("https://dp2.example", token=token), environ={})


def test_error_does_not_leak_the_token():
	with pytest.raises(ValueError) as e:
		build_http_transport(_Settings("http://dp2.example", token="s3cret-token"), environ={})
	assert "s3cret-token" not in str(e.value)


def test_defaults_to_process_environment(monkeypatch):
	monkeypatch.delenv(ALLOW_INSECURE_ENV, raising=False)
	with pytest.raises(ValueError, match="https"):
		build_http_transport(_Settings("http://dp2.example"))
	monkeypatch.setenv(ALLOW_INSECURE_ENV, "1")
	assert isinstance(build_http_transport(_Settings("http://dp2.example")), RequestsTransport)
