"""Tests for value masking utilities."""

from utils.masking import mask_email, mask_prefix_only, mask_secret


class TestMaskSecret:
    """Test mask_secret function."""

    def test_basic_masking(self):
        """Test basic masking with defaults."""
        result = mask_secret("sk-proj-abc123def456")
        assert result.startswith("sk-p")
        assert result.endswith("f456")
        assert "*" in result

    def test_empty_value(self):
        """Test empty string returns empty."""
        assert mask_secret("") == ""

    def test_short_value_fully_masked(self):
        """Short values should be fully masked."""
        result = mask_secret("short")
        assert result == "*****"

    def test_custom_show_chars(self):
        """Test custom show_start and show_end."""
        result = mask_secret("abc123", show_start=2, show_end=2)
        assert result.startswith("ab")
        assert result.endswith("23")

    def test_mask_length(self):
        """Test that mask meets minimum length."""
        result = mask_secret("1234567890", show_start=2, show_end=2, min_mask_length=4)
        assert result.startswith("12")
        assert result.endswith("90")
        assert "****" in result

    def test_fixed_length_mask(self):
        """Test that mask length is fixed, not proportional to value length (#132)."""
        short_result = mask_secret("sk-proj-abc123")
        long_result = mask_secret("sk-proj-abc123def456ghi789jkl012mno345pqr678stu901vwx")
        # Both should have same number of mask chars (default min_mask_length=4)
        short_stars = short_result.count("*")
        long_stars = long_result.count("*")
        assert short_stars == long_stars == 4
        # Output should never exceed input length
        assert len(short_result) <= len("sk-proj-abc123")
        assert len(long_result) <= len("sk-proj-abc123def456ghi789jkl012mno345pqr678stu901vwx")

    def test_boundary_near_total_shown(self):
        """Test value just barely longer than show_start + show_end (#132)."""
        # 9 chars, show 4+4=8, only 1 char available for mask
        result = mask_secret("abcdefghi")
        assert result.startswith("abcd")
        assert result.endswith("fghi")
        assert len(result) <= len("abcdefghi")
        assert result.count("*") == 1


class TestMaskPrefixOnly:
    """Test mask_prefix_only function."""

    def test_basic(self):
        """Test basic prefix masking."""
        result = mask_prefix_only("sk-proj-abc123def456")
        assert result.startswith("sk-proj-")
        assert result.endswith("***")

    def test_empty_value(self):
        """Test empty string returns empty."""
        assert mask_prefix_only("") == ""

    def test_short_value(self):
        """Short value returns only mask suffix."""
        assert mask_prefix_only("short", show_chars=10) == "***"


class TestMaskEmail:
    """Test mask_email function."""

    def test_basic_email(self):
        """Test basic email masking."""
        result = mask_email("user@example.com")
        assert result == "us***@example.com"
        assert "@example.com" in result

    def test_short_local_part(self):
        """Test email with short local part."""
        result = mask_email("a@test.com")
        assert "@test.com" in result

    def test_empty_email(self):
        """Test empty email."""
        assert mask_email("") == "***"

    def test_invalid_email(self):
        """Test invalid email format."""
        assert mask_email("not-an-email") == "***"

    def test_multiple_at_signs(self):
        """Test email with multiple @ signs."""
        assert mask_email("a@b@c") == "***"
