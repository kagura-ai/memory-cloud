"""#3034: EMBEDDING_TASK_CONCURRENCY has a default and is checked at start-up."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("EMBEDDING_TASK_CONCURRENCY", raising=False)


def test_default_is_below_the_pool_default():
    s = Settings(_env_file=None)
    assert s.embedding_task_concurrency == 4
    assert s.embedding_task_concurrency < s.db_pool_size


def test_value_from_the_environment(monkeypatch):
    monkeypatch.setenv("EMBEDDING_TASK_CONCURRENCY", "8")
    assert Settings(_env_file=None).embedding_task_concurrency == 8


@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_values_below_one_are_refused(monkeypatch, value):
    monkeypatch.setenv("EMBEDDING_TASK_CONCURRENCY", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
