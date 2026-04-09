"""Tests for URL redaction utilities (issue #272)."""

from __future__ import annotations

from utils.url_redact import redact_db_url, redact_generic_url


class TestRedactDbUrl:
    """Test redact_db_url for SQLAlchemy URLs."""

    def test_postgresql_asyncpg_with_password(self):
        url = "postgresql+asyncpg://kagura:s3cret@db:5432/app"
        result = redact_db_url(url)
        assert "s3cret" not in result
        assert "kagura" in result
        assert "db:5432" in result
        assert "/app" in result
        assert "postgresql+asyncpg" in result
        assert "***" in result

    def test_postgresql_plain_with_password(self):
        url = "postgresql://user:pw@host/db"
        result = redact_db_url(url)
        assert "pw" not in result
        assert "user" in result
        assert "host" in result
        assert "***" in result

    def test_no_password_does_not_fabricate_marker(self):
        """URLs without a password must not gain a fake `***` marker."""
        url = "postgresql://kagura@db/app"
        result = redact_db_url(url)
        assert "***" not in result
        assert "kagura" in result
        assert "db" in result

    def test_sqlite_file_url(self):
        """sqlite URLs have no netloc — should round-trip without change."""
        url = "sqlite:///./test.db"
        result = redact_db_url(url)
        assert "test.db" in result

    def test_empty_string_returns_placeholder(self):
        assert redact_db_url("") == "<redacted-url>"

    def test_malformed_returns_placeholder(self):
        """Garbage input must not crash and must not echo back unsafely."""
        result = redact_db_url("not a url at all")
        assert result == "<redacted-url>"

    def test_password_with_percent_encoded_chars(self):
        """Percent-encoded special chars in password must still be redacted."""
        url = "postgresql://user:p%40ss@host/db"
        result = redact_db_url(url)
        assert "p%40ss" not in result
        assert "p@ss" not in result
        assert "***" in result

    def test_preserves_query_params(self):
        url = "postgresql://user:pw@host/db?sslmode=require"
        result = redact_db_url(url)
        assert "pw" not in result
        assert "sslmode=require" in result


class TestRedactGenericUrl:
    """Test redact_generic_url for Redis / Qdrant / HTTP URLs."""

    def test_redis_password_no_user(self):
        """Redis convention: redis://:password@host."""
        url = "redis://:s3cret@redis:6379/0"
        result = redact_generic_url(url)
        assert "s3cret" not in result
        assert "redis:6379" in result
        assert "/0" in result
        assert "***" in result

    def test_redis_with_user_and_password(self):
        url = "redis://user:s3cret@redis:6379/0"
        result = redact_generic_url(url)
        assert "s3cret" not in result
        assert "user" in result
        assert "***" in result

    def test_redis_no_auth(self):
        """No credentials → pass through unchanged."""
        url = "redis://redis:6379/0"
        assert redact_generic_url(url) == url

    def test_qdrant_no_auth(self):
        url = "http://qdrant:6333"
        assert redact_generic_url(url) == url

    def test_qdrant_with_basic_auth(self):
        url = "https://admin:token@qdrant.example.com:6333"
        result = redact_generic_url(url)
        assert "token" not in result
        assert "admin" in result
        assert "***" in result

    def test_user_only_no_password_no_fake_marker(self):
        """user@host (no password) must not gain a fake `***` marker."""
        url = "http://user@host/path"
        result = redact_generic_url(url)
        assert "***" not in result
        assert "user@host" in result

    def test_empty_string_returns_placeholder(self):
        assert redact_generic_url("") == "<redacted-url>"

    def test_preserves_path_and_query(self):
        url = "https://user:pw@api.example.com/v1/search?q=foo&k=5"
        result = redact_generic_url(url)
        assert "pw" not in result
        assert "/v1/search" in result
        assert "q=foo" in result
        assert "k=5" in result
