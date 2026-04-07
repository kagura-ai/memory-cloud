"""Tests for redirect_uri wildcard matching (Issue #207)."""

import pytest

from utils.redirect_uri import (
    any_redirect_uri_matches,
    is_valid_redirect_uri_pattern,
    redirect_uri_matches,
)


class TestIsValidPattern:
    """Validate well-formedness of stored patterns."""

    @pytest.mark.parametrize(
        "pattern",
        [
            "https://example.com/cb",
            "https://example.com/cb/",
            "https://example.com/cb/*",
            "https://chatgpt.com/connector/oauth/*",
            "https://claude.ai/api/mcp/auth_callback",
            "http://localhost:3000/cb/*",
            "http://localhost/cb",
        ],
    )
    def test_valid(self, pattern):
        assert is_valid_redirect_uri_pattern(pattern) is True

    @pytest.mark.parametrize(
        "pattern",
        [
            "",
            "not-a-url",
            "ftp://example.com/cb",
            "javascript:alert(1)",
            "https://example.com/*/cb",  # wildcard in middle
            "https://example.com/cb*",  # wildcard not after slash
            "https://*.example.com/cb",  # wildcard in host
            "https://example.com/cb/**",  # double wildcard
            "https://example.com/cb/*/extra",  # extra after wildcard
            "https://example.com/cb?next=*",  # wildcard in query
            "https://example.com/cb/*?x=1",  # query on wildcard pattern
            "https://example.com/cb#frag/*",  # wildcard after fragment
            "https:///cb",  # missing host
        ],
    )
    def test_invalid(self, pattern):
        assert is_valid_redirect_uri_pattern(pattern) is False


class TestRedirectUriMatches:
    """Match incoming redirect_uri against a single stored pattern."""

    def test_exact_match(self):
        assert redirect_uri_matches("https://example.com/cb", "https://example.com/cb") is True

    def test_exact_mismatch(self):
        assert redirect_uri_matches("https://example.com/cb", "https://example.com/other") is False

    def test_wildcard_matches_one_segment(self):
        assert (
            redirect_uri_matches(
                "https://chatgpt.com/connector/oauth/*",
                "https://chatgpt.com/connector/oauth/4YtIB3v8sXsG",
            )
            is True
        )

    def test_wildcard_rejects_empty_segment(self):
        assert (
            redirect_uri_matches(
                "https://chatgpt.com/connector/oauth/*",
                "https://chatgpt.com/connector/oauth/",
            )
            is False
        )

    def test_wildcard_rejects_extra_segment(self):
        assert (
            redirect_uri_matches(
                "https://chatgpt.com/connector/oauth/*",
                "https://chatgpt.com/connector/oauth/abc/extra",
            )
            is False
        )

    def test_wildcard_rejects_path_traversal(self):
        assert (
            redirect_uri_matches(
                "https://chatgpt.com/connector/oauth/*",
                "https://chatgpt.com/connector/oauth/..",
            )
            is False
        )

    def test_scheme_mismatch_rejected(self):
        assert (
            redirect_uri_matches("https://example.com/cb/*", "http://example.com/cb/abc") is False
        )

    def test_host_mismatch_rejected(self):
        assert redirect_uri_matches("https://example.com/cb/*", "https://evil.com/cb/abc") is False

    def test_subdomain_mismatch_rejected(self):
        assert (
            redirect_uri_matches("https://example.com/cb/*", "https://api.example.com/cb/abc")
            is False
        )

    def test_port_mismatch_rejected(self):
        assert (
            redirect_uri_matches("https://example.com/cb/*", "https://example.com:8443/cb/abc")
            is False
        )

    def test_query_string_rejected_on_wildcard(self):
        # Open-redirect attempt via query params
        assert (
            redirect_uri_matches("https://example.com/cb/*", "https://example.com/cb/abc?next=evil")
            is False
        )

    def test_fragment_rejected_on_wildcard(self):
        assert (
            redirect_uri_matches("https://example.com/cb/*", "https://example.com/cb/abc#frag")
            is False
        )

    def test_path_prefix_pinned(self):
        # Stored pattern path prefix must match — cannot pivot to different endpoint
        assert (
            redirect_uri_matches("https://example.com/cb/*", "https://example.com/admin/abc")
            is False
        )

    def test_partial_path_segment_rejected(self):
        # /cb/* should not match /cba/abc (path must extend by full segment)
        assert (
            redirect_uri_matches("https://example.com/cb/*", "https://example.com/cba/abc") is False
        )

    def test_url_encoded_dot_dot_rejected(self):
        # %2E%2E decodes to ".." — must not bypass the traversal check
        assert (
            redirect_uri_matches("https://example.com/cb/*", "https://example.com/cb/%2E%2E")
            is False
        )

    def test_url_encoded_slash_rejected(self):
        # %2F decodes to "/" — must not bypass the single-segment check
        assert (
            redirect_uri_matches("https://example.com/cb/*", "https://example.com/cb/abc%2Fdef")
            is False
        )

    def test_url_encoded_single_dot_rejected(self):
        # %2E decodes to "." — current-directory reference, reject
        assert (
            redirect_uri_matches("https://example.com/cb/*", "https://example.com/cb/%2E") is False
        )

    def test_incoming_with_literal_asterisk_rejected(self):
        assert redirect_uri_matches("https://example.com/cb/*", "https://example.com/cb/*") is False

    def test_non_wildcard_pattern_rejects_anything_else(self):
        assert (
            redirect_uri_matches("https://example.com/cb", "https://example.com/cb/extra") is False
        )


class TestAnyMatch:
    """Test the list-of-patterns helper."""

    def test_empty_list(self):
        assert any_redirect_uri_matches([], "https://example.com/cb") is False

    def test_none(self):
        assert any_redirect_uri_matches(None, "https://example.com/cb") is False

    def test_first_matches(self):
        assert (
            any_redirect_uri_matches(
                ["https://example.com/cb", "https://other.com/cb/*"],
                "https://example.com/cb",
            )
            is True
        )

    def test_second_matches(self):
        assert (
            any_redirect_uri_matches(
                ["https://example.com/cb", "https://chatgpt.com/connector/oauth/*"],
                "https://chatgpt.com/connector/oauth/abc123",
            )
            is True
        )

    def test_none_match(self):
        assert (
            any_redirect_uri_matches(
                ["https://example.com/cb", "https://chatgpt.com/connector/oauth/*"],
                "https://evil.com/cb",
            )
            is False
        )
