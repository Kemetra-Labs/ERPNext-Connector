# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-635 (OBS-6) — the OBS-1 privacy baseline (§3) for the Connector's Sentry events.

:func:`scrub_event` is the client's ``before_send``. It REBUILDS the outgoing event from an allowlist
(§3.1) instead of deleting known-bad keys, so anything Frappe, the SDK or a global event processor
adds (request, user, extra, contexts, breadcrumbs, frame vars, source lines) never leaves the process.

:func:`sanitize_message` masks, anywhere in an exception message (§3.2 / §3.3): JWTs, URL userinfo,
query and fragment, emails, Postgres row detail, the MariaDB ``Duplicate entry`` value (the
Connector's own database echoes the provenance key there), ``Bearer`` tokens, credential
``key=value`` pairs and phone numbers (RT-686, OBS-1 §3.2 as amended by RT-682). Input is capped before scanning and the result truncated. Only index scans and
whitespace splits run on the text — no regex — so the work stays linear on untrusted input.

Mirrors Backend-Core's reference scrubber (``packages/shared/src/observability/sentry.ts``, RT-609).
Pure Python: no frappe, no sentry_sdk.
"""

from __future__ import annotations

import string
import uuid
from collections.abc import Iterator, Mapping
from urllib.parse import urlsplit

MAX_SCAN_LENGTH = 2000
MAX_MESSAGE_LENGTH = 300
_ELLIPSIS = "..."

SAFE_TOP_LEVEL = ("event_id", "timestamp", "platform", "level", "environment", "release", "server_name")
FRAME_KEYS = ("filename", "function", "lineno", "colno", "module", "in_app")
MECHANISM_KEYS = ("type", "handled")
# OBS-1 §1 tags, the bounded extras, and §2's ``request_id``. Never tenant/store/user/sale ids.
ALLOWED_TAGS = frozenset(
    {
        "env",
        "service",
        "version",
        "request_id",
        "route",
        "method",
        "status",
        "status_class",
        "queue",
        "job_name",
        "error_class",
        "outcome",
    }
)
MAX_TAG_LENGTH = 64
_TAG_CHARS = frozenset(string.ascii_letters + string.digits + "._:/-")
_SCALARS = (str, int, float, bool)

# Database row detail. Postgres echoes row values after these markers and they may contain
# parentheses, so everything from the marker on is masked (str.find, no regex).
_ROW_MARKERS = (
    ("Key (", "Key (...)=(...)"),
    ("Failing row contains (", "Failing row contains (...)"),
)
# MariaDB 1062: ``Duplicate entry '<value>' for key '<index>'``. Only the value is masked.
_DUPLICATE_ENTRY = "Duplicate entry '"
_DUPLICATE_KEY = "' for key "

_SCHEME_CHARS = frozenset(string.ascii_letters + string.digits + "+.-")
_TRAILING_PUNCTUATION = frozenset("\"')],;.>")
_QUOTES = "\"'"
_WRAPPERS = "\"'{[(,:"
_IDENTIFIER_CHARS = frozenset(string.ascii_letters + string.digits + "_-")
# A credential key matches as the END of an identifier: ``dp2_token``, ``client_secret``, ``api-key``.
_CREDENTIAL_SUFFIXES = (
    "token",
    "password",
    "passwd",
    "pwd",
    "secret",
    "apikey",
    "api_key",
    "api-key",
    "authorization",
    "dsn",
)
_REDACTED = "[redacted]"
# HTTP auth schemes: after ``Authorization:``, the scheme AND the credential that follows are masked.
_AUTH_SCHEMES = frozenset({"bearer", "basic", "digest", "token", "apikey", "negotiate", "ntlm"})
# OBS-1 §3.2 phones: E.164 is ``+`` and 8-15 digits not starting with 0; an Egyptian mobile is
# ``1[0125]`` + 8 digits behind a national (``0``) or country (``20``, ``0020``) prefix.
_PHONE = "[phone]"
_DIGITS = frozenset(string.digits)
_ASCII_ALNUM = frozenset(string.ascii_letters + string.digits)
_E164_DIGITS = range(8, 16)
_EGYPT_MOBILE_PREFIX_BY_LENGTH = {11: "0", 12: "20", 14: "0020"}
_EGYPT_MOBILE_OPERATORS = frozenset({"10", "11", "12", "15"})


# --- §3.1 allowlist rebuild ------------------------------------------------------------------


def scrub_event(event: Mapping, _hint: Mapping | None = None) -> dict | None:
    """``before_send``: the event rebuilt from allowlisted fields, or ``None`` (drop) without an exception.

    Pure: never mutates ``event``.
    """
    exception = event.get("exception")
    values = exception.get("values") if isinstance(exception, Mapping) else None
    if not isinstance(values, list) or not values:
        return None
    out = {key: event[key] for key in SAFE_TOP_LEVEL if event.get(key) is not None}
    tags = _scrub_tags(event.get("tags"))
    if tags:
        out["tags"] = tags
    out["exception"] = {"values": [_scrub_exception(v) for v in values if isinstance(v, Mapping)]}
    return out


def _scrub_tags(tags: object) -> dict[str, str]:
    if not isinstance(tags, Mapping):
        return {}
    return {key: value for key, value in tags.items() if key in ALLOWED_TAGS and _is_safe_tag(key, value)}


def _is_safe_tag(key: str, value: object) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= MAX_TAG_LENGTH:
        return False
    if key == "request_id":
        return _is_canonical_uuid(value)
    return all(char in _TAG_CHARS for char in value)


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _scrub_exception(entry: Mapping) -> dict:
    out = _pick_scalars(entry, ("type",))
    if isinstance(entry.get("value"), str):
        out["value"] = sanitize_message(entry["value"])
    mechanism = entry.get("mechanism")
    if isinstance(mechanism, Mapping):
        out["mechanism"] = _pick_scalars(mechanism, MECHANISM_KEYS)
    frames = _frames(entry.get("stacktrace"))
    if frames is not None:
        out["stacktrace"] = {"frames": frames}
    return out


def _frames(stacktrace: object) -> list[dict] | None:
    """Each frame's location fields only; ``None`` when there is no frame list."""
    frames = stacktrace.get("frames") if isinstance(stacktrace, Mapping) else None
    if not isinstance(frames, list):
        return None
    return [_frame(frame) for frame in frames if isinstance(frame, Mapping)]


def _frame(frame: Mapping) -> dict:
    filename = frame.get("filename")
    if isinstance(filename, str):
        frame = {**frame, "filename": _without_directories_if_absolute(filename)}
    return _pick_scalars(frame, FRAME_KEYS)


def _without_directories_if_absolute(filename: str) -> str:
    """An absolute path (``/home/<user>/…``, ``C:\\Users\\<user>\\…``) → its file name only.

    Module-relative paths (the SDK's usual ``filename``) carry no user directory and are kept.
    """
    if not (filename.startswith(("/", "\\")) or filename[1:3] in (":\\", ":/")):
        return filename
    return filename.replace("\\", "/").rpartition("/")[2]


def _pick_scalars(source: Mapping, keys: tuple[str, ...]) -> dict:
    """The listed scalar fields; strings (type names, frame paths, functions) are sanitized too."""
    return {key: _sanitized(source[key]) for key in keys if isinstance(source.get(key), _SCALARS)}


def _sanitized(value: object) -> object:
    return sanitize_message(value) if isinstance(value, str) else value


# --- §3.2 / §3.3 message masking ---------------------------------------------------------------


def sanitize_message(message: object) -> str:
    """The message with every §3.2/§3.3 pattern masked, capped and truncated to the contract length."""
    if not isinstance(message, str):
        return ""
    text = _mask_duplicate_entry(_mask_postgres_rows(_capped(message)))
    cleaned = "".join(_scrub_tokens(text))
    if len(cleaned) > MAX_MESSAGE_LENGTH:
        cleaned = cleaned[: MAX_MESSAGE_LENGTH - len(_ELLIPSIS)] + _ELLIPSIS
    return cleaned


def _capped(message: str) -> str:
    """The first ``MAX_SCAN_LENGTH`` chars, cut back to whitespace so no token is split by the cap.

    A split token would dodge the checks (``jane@exa`` has no dot after its ``@``).
    """
    if len(message) <= MAX_SCAN_LENGTH:
        return message
    end = MAX_SCAN_LENGTH
    while end > 0 and not message[end].isspace():
        end -= 1
    return message[:end]


def _mask_postgres_rows(text: str) -> str:
    for marker, mask in _ROW_MARKERS:
        at = text.find(marker)
        if at != -1:
            text = text[:at] + mask
    return text


def _mask_duplicate_entry(text: str) -> str:
    """``Duplicate entry '<value>' for key 'x'`` → ``Duplicate entry '...' for key 'x'``."""
    at = text.find(_DUPLICATE_ENTRY)
    if at == -1:
        return text
    value_start = at + len(_DUPLICATE_ENTRY)
    key_at = text.find(_DUPLICATE_KEY, value_start)
    tail = text[key_at:] if key_at != -1 else "'"
    return text[:value_start] + "..." + tail


def _split_keeping_whitespace(text: str) -> Iterator[str]:
    """Alternating word / whitespace runs, so the joined output keeps the original spacing."""
    start = 0
    for index in range(1, len(text) + 1):
        if index == len(text) or text[index].isspace() != text[start].isspace():
            yield text[start:index]
            start = index


def _scrub_tokens(text: str) -> Iterator[str]:
    redact_next = False
    for token in _split_keeping_whitespace(text):
        if token.isspace():
            yield token
            continue
        if redact_next:
            redact_next = _is_auth_scheme(token)  # "Authorization: Basic <x>" → both masked
            yield _REDACTED
            continue
        scrubbed, redact_next = _scrub_token(token)
        yield scrubbed


def _scrub_token(token: str) -> tuple[str, bool]:
    """One whitespace-free token, masked; and whether the NEXT token is a secret value."""
    if _is_bearer(token):
        return _REDACTED, True
    masked, redact_next = _without_credential(_without_relative_query(_without_url(token)))
    return _without_phones(_without_jwt_or_email(masked)), redact_next


def _without_jwt_or_email(token: str) -> str:
    masked = _without_jwt(token)
    return "[email]" if _is_email_like(masked) else masked


def _without_phones(token: str) -> str:
    """Every phone-shaped digit run → ``[phone]`` (OBS-1 §3.2). One pass over the token."""
    out: list[str] = []
    at = 0
    while at < len(token):
        if token[at] not in _DIGITS:
            out.append(token[at])
            at += 1
            continue
        end = at
        while end < len(token) and token[end] in _DIGITS:
            end += 1
        kind = _phone_kind(token, at, end)
        if kind == "e164":
            out.pop()  # the "+" in front of the run
        out.append(token[at:end] if kind is None else _PHONE)
        at = end
    return "".join(out)


def _phone_kind(token: str, start: int, end: int) -> str | None:
    """``"e164"`` (the ``+`` is masked too), ``"national"`` or ``None`` for the run ``token[start:end]``.

    The run must have no ASCII letter or digit on either side.
    """
    if _char_at(token, end) in _ASCII_ALNUM:
        return None
    run = token[start:end]
    before = _char_at(token, start - 1)
    if before == "+" and _char_at(token, start - 2) not in _ASCII_ALNUM and _is_e164(run):
        return "e164"
    return "national" if before not in _ASCII_ALNUM and _is_egyptian_mobile(run) else None


def _char_at(text: str, index: int) -> str:
    """The character at ``index``, or ``""`` past either end (never a negative-index wrap)."""
    return text[index] if 0 <= index < len(text) else ""


def _is_e164(run: str) -> bool:
    return run[0] != "0" and len(run) in _E164_DIGITS


def _is_egyptian_mobile(run: str) -> bool:
    prefix = _EGYPT_MOBILE_PREFIX_BY_LENGTH.get(len(run))
    if prefix is None or not run.startswith(prefix):
        return False
    return run[len(prefix) : len(prefix) + 2] in _EGYPT_MOBILE_OPERATORS


def _is_bearer(token: str) -> bool:
    """``Bearer`` on its own, quoted or not (``'Bearer`` in a header-dict repr)."""
    return token.strip(_WRAPPERS).lower() == "bearer"


def _is_auth_scheme(token: str) -> bool:
    return token.strip(_WRAPPERS).lower() in _AUTH_SCHEMES


def _without_url(token: str) -> str:
    """Keep scheme + host + path; drop userinfo, query and fragment. Unparsable → ``[url]``."""
    sep = token.find("://")
    start = _scan_back(token, sep, _SCHEME_CHARS) if sep > 0 else sep
    if start < 0 or start == sep:
        return token
    end = _scan_back(token, len(token), _TRAILING_PUNCTUATION, floor=start)
    return token[:start] + _safe_url(token[start:end]) + token[end:]


def _scan_back(text: str, end: int, chars: frozenset[str] | str, floor: int = 0) -> int:
    """The start of the run of ``chars`` that ends at ``end`` (never below ``floor``)."""
    while end > floor and text[end - 1] in chars:
        end -= 1
    return end


def _safe_url(raw: str) -> str:
    try:
        parts = urlsplit(raw)
    except ValueError:  # e.g. an unbalanced IPv6 bracket
        return "[url]"
    if not _is_complete_url(parts):
        return "[url]"
    host = parts.netloc.rpartition("@")[2]  # drop userinfo
    return f"{parts.scheme}://{host}{parts.path}"


def _is_complete_url(parts) -> bool:
    return all((parts.scheme, parts.hostname, _has_valid_port(parts)))


def _has_valid_port(parts) -> bool:
    try:
        return parts.port is None or parts.port >= 0
    except ValueError:  # a non-numeric or out-of-range port
        return False


def _without_relative_query(token: str) -> str:
    """A path-shaped token (``/v1/feed?since=…``, as urllib3 prints it) without its query/fragment."""
    cut = min((at for at in (token.find("?"), token.find("#")) if at != -1), default=-1)
    if cut == -1 or token.find("/", 0, cut) == -1:
        return token
    end = _scan_back(token, len(token), _TRAILING_PUNCTUATION, floor=cut)
    return token[:cut] + token[end:]


def _without_credential(token: str) -> tuple[str, bool]:
    """``key=value`` / ``key:value`` with a credential key → the value masked (or the next token).

    Every separator is checked, so ``headers={'dp2_token':`` matches too. The backward scan only
    covers the identifier in front of each separator, so the work stays linear.
    """
    for sep, char in enumerate(token):
        if char in "=:" and _credential_key_before(token, sep):
            value = token[sep + 1 :]
            if not value:
                return token, True  # `"password":` — the value is the next token
            # `Authorization:Basic <x>` — the inline value is the scheme; the credential is next.
            return token[: sep + 1] + _REDACTED, _is_auth_scheme(value)
    return token, False


def _credential_key_before(token: str, sep: int) -> bool:
    end = _scan_back(token, sep, _QUOTES)
    start = _scan_back(token, end, _IDENTIFIER_CHARS)
    return token[start:end].lower().endswith(_CREDENTIAL_SUFFIXES)


def _without_jwt(token: str) -> str:
    """Mask a JWT (``eyJ…`` with three dot-separated segments) and everything after it."""
    at = token.find("eyJ")
    if at == -1 or token.count(".", at) < 2:
        return token
    return token[:at] + "[jwt]"


def _is_email_like(token: str) -> bool:
    at = token.find("@")
    return at > 0 and token.find(".", at) != -1
