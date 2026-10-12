# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""T050/T052 — local unit tests for the reason-category mapper + failure invariants.

Maps connector-internal failure kinds onto the 012 CLOSED RejectionReason.category set
(decision table rows 4-9); an unmapped internal kind RAISES rather than inventing a wire
code (FR-007). Failure messages carry no secrets (Principle V / Gate G4). No frappe.
"""

import time

import pytest

from retail_tower_erpnext_connector.connector.posting import contracts as c
from retail_tower_erpnext_connector.connector.posting import reasons as r


class TestReasonMapping:
    @pytest.mark.parametrize(
        "internal_kind, expected_category",
        [
            (r.FailureKind.CLOSED_PERIOD, "closed_period"),
            (r.FailureKind.VALIDATION, "validation"),
            (r.FailureKind.UNMAPPED_UNIT, "validation"),  # row 5: unmapped unit IS validation
            (r.FailureKind.UNMAPPED_ACCOUNT, "unmapped_account"),
            (r.FailureKind.MISSING_ITEM_REF, "validation"),  # row 8: upstream contract violation
            (r.FailureKind.DISABLED_ITEM, "validation"),  # row 7
            (r.FailureKind.OTHER, "other"),
        ],
    )
    def test_maps_internal_kind_to_closed_category(self, internal_kind, expected_category):
        reason = r.to_rejection_reason(internal_kind, message="detail")
        assert isinstance(reason, c.RejectionReason)
        assert reason.category == expected_category

    def test_unmapped_uom_is_never_its_own_category(self):
        # There is no `unmapped_uom` in the 012 closed set — it must resolve to validation.
        reason = r.to_rejection_reason(r.FailureKind.UNMAPPED_UNIT, message="unit 'box' unmapped")
        assert reason.category == "validation"
        assert reason.category in c.REJECTION_CATEGORIES

    def test_every_failure_kind_maps_into_the_closed_set(self):
        # T050: no FailureKind escapes the closed set; none invents a new wire code.
        for kind in r.FailureKind:
            reason = r.to_rejection_reason(kind, message="m")
            assert reason.category in c.REJECTION_CATEGORIES

    def test_unknown_kind_raises_not_invents(self):
        # An object that isn't a known FailureKind must raise — never a silent "other".
        with pytest.raises(r.UnmappedFailureKind):
            r.to_rejection_reason("totally-unknown", message="m")  # type: ignore[arg-type]


class TestFailureMessageHygiene:
    def test_scrubs_token_like_substrings(self):
        # T052: no secret/token/credential in the outcome message (Gate G4).
        dirty = "auth failed: Bearer sk-secret-abc123 token=deadbeef password=hunter2"
        clean = r.scrub_message(dirty)
        assert "sk-secret-abc123" not in clean
        assert "deadbeef" not in clean
        assert "hunter2" not in clean
        assert "[redacted]" in clean

    def test_preserves_safe_detail(self):
        clean = r.scrub_message("accounting period 2026-Q1 is closed")
        assert "accounting period 2026-Q1 is closed" == clean

    def test_reason_built_through_mapper_is_scrubbed(self):
        reason = r.to_rejection_reason(
            r.FailureKind.OTHER, message="db error token=abcd1234efgh5678"
        )
        assert "abcd1234efgh5678" not in reason.message

    def test_scrubs_json_serialized_secrets(self):
        # F-008: frappe often serializes exceptions as dicts/JSON — the scrubber must catch
        # quoted/JSON forms, not just `key=value`.
        dirty = '{"password": "hunter2", "api_key": "sk_live_abcd1234efgh"}'
        clean = r.scrub_message(dirty)
        assert "hunter2" not in clean
        assert "sk_live_abcd1234efgh" not in clean

    def test_scrubs_sk_underscore_variant(self):
        # F-008: sk_ (underscore) variant, not only sk-.
        clean = r.scrub_message("key leaked: sk_live_deadbeef1234")
        assert "sk_live_deadbeef1234" not in clean


class TestReasonMessageBounds:
    """RT-48: the DP2 ack contract bounds ``reason.message`` to 1..1000 chars (outcome-ack.dto.ts).

    ERPNext validation messages can be long HTML (the missing-valuation-rate error embeds a form
    link, <br>s and a <ul> of remedies). DP2 refuses an over-long message with a 400, which the
    transport surfaces as an unexpected-status error that escapes the page — the same item would be
    re-offered every tick. The reason must therefore be plain text and bounded.
    """

    def test_html_is_stripped_to_plain_text(self):
        msg = (
            'Valuation Rate for the Item <a href="/app/item/X">X</a>, is required.'
            "<br><br>Here are the options:<ul><li>Fix A</li><li>Fix B</li></ul>"
        )
        reason = r.to_rejection_reason(r.FailureKind.VALIDATION, message=msg)
        assert "<" not in reason.message
        assert ">" not in reason.message
        assert "Valuation Rate for the Item X, is required." in reason.message
        assert "Fix A" in reason.message
        assert "Fix B" in reason.message

    def test_message_is_bounded_to_the_ack_contract_limit(self):
        reason = r.to_rejection_reason(r.FailureKind.VALIDATION, message="x" * 5000)
        assert 1 <= len(reason.message) <= 1000

    def test_empty_message_still_satisfies_min_length(self):
        reason = r.to_rejection_reason(r.FailureKind.VALIDATION, message="<br>")
        assert len(reason.message) >= 1

    def test_secrets_are_still_redacted_after_normalisation(self):
        reason = r.to_rejection_reason(r.FailureKind.OTHER, message="<p>token=abc123secret</p>")
        assert "abc123secret" not in reason.message


class TestScrubberHardening:
    """RT-409 (Gate G4): the gaps probed at Connector ``3d03ea0``."""

    @pytest.mark.parametrize(
        "dirty, secret",
        [
            ("dp2_token=abc123", "abc123"),  # the Connector Settings field holding the Backend-Core token
            ("access_token: xyz", "xyz"),
            ("client_secret=s3cr3t", "s3cr3t"),
            ("pwd=hunter2", "hunter2"),
            ("passwd=hunter2", "hunter2"),
            ('{"dp2_token": "abc123"}', "abc123"),
            ("DP2_TOKEN: abc123", "abc123"),
            ("x-auth-token: abc123", "abc123"),
            # A quoted credential is redacted to its closing quote, spaces included (CodeRabbit, PR #68).
            ('{"dp2_token": "alpha beta"}', "beta"),
            ("password='my secret phrase'", "phrase"),
            ("got 'dp2_token=alpha beta'", "beta"),  # the repr a parse error echoes (got {value!r})
            ('got "pwd: a b c" here', "b c"),
            # ...through the MATCHING closing quote: the other quote character and escaped quotes stay
            # inside the credential (Codex, PR #69).
            ("got " + repr('dp2_token=alpha "beta gamma" omega'), "omega"),
            ("got " + repr("dp2_token=it's secret now"), "secret now"),
            ('{"dp2_token": "a \\"b c\\" d"}', "b c"),
            ("pwd='it\\'s secret'", "secret"),
        ],
    )
    def test_a_credential_key_ending_an_identifier_is_redacted(self, dirty, secret):
        clean = r.scrub_message(dirty)
        assert secret not in clean
        assert "[redacted]" in clean

    @pytest.mark.parametrize(
        "dirty",
        [
            'token="' + "a" * 257 + ' secret"',  # longer than the quoted cap (CodeRabbit, PR #69)
            "got 'dp2_token=" + "a" * 300 + " secret'",
            'password="abc secret',  # unterminated
        ],
    )
    def test_an_overlong_or_unterminated_quoted_value_never_leaks_its_tail(self, dirty):
        clean = r.scrub_message(dirty)
        assert "secret" not in clean
        assert clean.endswith("[redacted]")

    def test_a_closed_quoted_value_keeps_the_rest_of_the_message(self):
        assert r.scrub_message("got 'dp2_token=alpha beta' then more") == "got [redacted] then more"

    def test_an_unquoted_value_still_ends_at_whitespace(self):
        assert r.scrub_message("token=abc rest of message") == "[redacted] rest of message"

    def test_an_identifier_that_only_contains_a_key_word_is_kept(self):
        assert r.scrub_message("token_count: 5") == "token_count: 5"

    def test_entity_encoded_markup_is_stripped_after_unescaping(self):
        assert r.plain_bounded_message("&lt;b&gt;x&lt;/b&gt;") == "x"

    def test_an_unescaped_comparison_is_not_mistaken_for_a_tag(self):
        assert r.plain_bounded_message("qty 1 &lt; 2 &gt; 0") == "qty 1 < 2 > 0"

    @pytest.mark.parametrize(
        "dirty",
        [
            "a-" * 20000,
            "x_" * 20000,
            "_token" * 8000,
            "'token=" * 10000,
            '"token=' * 10000,
            "'" * 50000,
            "'token=\\" * 10000,  # an escape that eats every closing quote: the quoted body is capped
            'token="\\' * 10000,
        ],
    )
    def test_long_messages_scrub_in_linear_time(self, dirty):
        # Scrubbing runs before the 1000-char bound; a quadratic key pattern stalled for ~1 min here.
        start = time.monotonic()
        r.plain_bounded_message(dirty)
        assert time.monotonic() - start < 1.0
