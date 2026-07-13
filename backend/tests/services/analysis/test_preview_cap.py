"""#1244: run-size cap unit tests for ``assert_run_size_within_cap``.

Route-level enforcement (REST preview/start) is covered in
``tests/api/test_analyses_routes.py``; the vector_pull defense-in-depth
re-check in ``tests/services/analysis/test_vector_pull_coverage.py``.
"""

from __future__ import annotations

import pytest

from config.settings import get_settings
from services.analysis.preview import assert_run_size_within_cap
from utils.exceptions import ValidationError


class TestRunSizeCap:
    def test_at_cap_passes(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "analysis_max_memory_count", 50)
        assert_run_size_within_cap(50)  # must not raise

    def test_under_cap_passes(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "analysis_max_memory_count", 50)
        assert_run_size_within_cap(2)

    def test_over_cap_raises_naming_limit_and_count(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "analysis_max_memory_count", 50)
        with pytest.raises(ValidationError) as excinfo:
            assert_run_size_within_cap(51)
        message = str(excinfo.value)
        assert "51" in message, "actual count missing from the error"
        assert "50" in message, "configured limit missing from the error"

    def test_default_cap_matches_documented_value(self) -> None:
        """Pins the documented default (basic-tier memory_limit, see
        settings.py rationale) so a silent change shows up in review.
        """
        assert get_settings().analysis_max_memory_count == 10_000
