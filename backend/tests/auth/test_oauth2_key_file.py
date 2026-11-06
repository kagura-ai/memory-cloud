"""The OAuth credential encryption key file is created once, atomically (#2673).

Every API worker builds an ``OAuth2Manager`` at startup, so on a fresh container
several processes reach ``_load_or_create_key`` at the same moment against an
empty config directory. They must all end up with the same complete key, and the
file must never be seen half-written.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import re
import stat
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

import auth.oauth2 as oauth2
from auth.oauth2 import _load_or_create_key

_WORKERS = 8
_ROUNDS = 5
_GENERATED_LOG = "Generated new encryption key"


def _race_worker(barrier: Any, dirs: list[str], results: Any) -> None:
    """Child process: for each round, wait for the others, then load or create.

    Module level so the spawn start method can import it in the child.
    """
    for directory in dirs:
        barrier.wait(timeout=30)
        try:
            key = _load_or_create_key(Path(directory) / ".key")
        except Exception as exc:  # reported to the parent, which fails the test
            results.put((directory, None, f"{type(exc).__name__}: {exc}"))
        else:
            results.put((directory, key, None))


def _leftovers(directory: Path) -> list[str]:
    """Names in ``directory`` other than the key file itself."""
    return sorted(p.name for p in directory.iterdir() if p.name != ".key")


class TestConcurrentFirstStart:
    def test_processes_racing_on_an_empty_dir_agree_on_one_key(self, tmp_path: Path) -> None:
        """Eight processes released together all get the key that is on disk."""
        ctx = multiprocessing.get_context("spawn")
        dirs = [tmp_path / f"round-{n}" for n in range(_ROUNDS)]
        for directory in dirs:
            directory.mkdir()

        barrier = ctx.Barrier(_WORKERS)
        results = ctx.Queue()
        procs = [
            ctx.Process(target=_race_worker, args=(barrier, [str(d) for d in dirs], results))
            for _ in range(_WORKERS)
        ]
        for proc in procs:
            proc.start()
        try:
            outcomes = [results.get(timeout=60) for _ in range(_WORKERS * _ROUNDS)]
        finally:
            for proc in procs:
                proc.join(timeout=10)
                if proc.is_alive():
                    proc.kill()

        errors = [err for _, _, err in outcomes if err is not None]
        assert errors == []

        for directory in dirs:
            keys = {key for d, key, _ in outcomes if d == str(directory)}
            assert len(keys) == 1, f"{directory.name}: workers got {len(keys)} different keys"
            (key,) = keys
            assert key is not None
            Fernet(key)
            key_file = directory / ".key"
            assert key_file.read_bytes() == key
            assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
            assert _leftovers(directory) == []


class TestLoadOrCreateKey:
    def test_creates_a_key_with_owner_only_mode(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """First use writes a valid key, 0o600, and logs that it generated one."""
        key_file = tmp_path / ".key"
        with caplog.at_level(logging.INFO, logger="auth.oauth2"):
            key = _load_or_create_key(key_file)

        Fernet(key)
        assert key_file.read_bytes() == key
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        assert _leftovers(tmp_path) == []
        assert [r.getMessage() for r in caplog.records].count(f"{_GENERATED_LOG}: {key_file}") == 1
        assert key.decode() not in caplog.text

    def test_existing_key_is_reused_unchanged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A key already on disk is returned as is and nothing else is written."""
        key_file = tmp_path / ".key"
        existing = Fernet.generate_key()
        key_file.write_bytes(existing)

        with caplog.at_level(logging.INFO, logger="auth.oauth2"):
            assert _load_or_create_key(key_file) == existing

        assert key_file.read_bytes() == existing
        assert _leftovers(tmp_path) == []
        assert _GENERATED_LOG not in caplog.text

    def test_losing_the_link_race_returns_the_winners_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Another process publishes first: its key is used, ours is discarded."""
        key_file = tmp_path / ".key"
        winner = Fernet.generate_key()
        real_link = os.link

        def publish_first(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            Path(dst).write_bytes(winner)
            real_link(src, dst)  # raises FileExistsError, as a real lost race does

        monkeypatch.setattr(oauth2.os, "link", publish_first)
        with caplog.at_level(logging.INFO, logger="auth.oauth2"):
            assert _load_or_create_key(key_file) == winner

        assert key_file.read_bytes() == winner
        assert _leftovers(tmp_path) == []
        assert _GENERATED_LOG not in caplog.text

    def test_without_hard_links_creates_the_file_exclusively(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A filesystem that refuses hard links falls back to an O_EXCL create."""
        key_file = tmp_path / ".key"

        def no_links(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            raise PermissionError("hard links not supported")

        monkeypatch.setattr(oauth2.os, "link", no_links)
        key = _load_or_create_key(key_file)

        Fernet(key)
        assert key_file.read_bytes() == key
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        assert _leftovers(tmp_path) == []

    def test_without_hard_links_a_lost_race_returns_the_winners_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In the fallback, the process whose O_EXCL create fails reads the file."""
        key_file = tmp_path / ".key"
        winner = Fernet.generate_key()

        def no_links_and_someone_else_wrote(
            src: str | os.PathLike[str], dst: str | os.PathLike[str]
        ) -> None:
            Path(dst).write_bytes(winner)
            raise PermissionError("hard links not supported")

        monkeypatch.setattr(oauth2.os, "link", no_links_and_someone_else_wrote)

        assert _load_or_create_key(key_file) == winner
        assert key_file.read_bytes() == winner
        assert _leftovers(tmp_path) == []

    def test_waits_for_a_key_file_that_is_still_being_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file shorter than a key is re-read until the writer has finished."""
        key_file = tmp_path / ".key"
        full = Fernet.generate_key()
        key_file.write_bytes(full[:10])
        sleeps: list[float] = []

        def finish_writing(seconds: float) -> None:
            sleeps.append(seconds)
            key_file.write_bytes(full)

        monkeypatch.setattr(oauth2.time, "sleep", finish_writing)

        assert _load_or_create_key(key_file) == full
        assert len(sleeps) == 1

    def test_a_key_file_that_stays_short_raises_naming_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wait is bounded: an empty file fails instead of hanging."""
        key_file = tmp_path / ".key"
        key_file.write_bytes(b"")
        monkeypatch.setattr(oauth2, "_KEY_WAIT_SECONDS", 0.05)

        with pytest.raises(ValueError, match=re.escape(str(key_file))):
            _load_or_create_key(key_file)
        assert key_file.read_bytes() == b""

    def test_an_invalid_existing_key_raises_naming_the_file(self, tmp_path: Path) -> None:
        """A corrupt key is not replaced: credentials encrypted with it depend on it."""
        key_file = tmp_path / ".key"
        corrupt = b"x" * 60
        key_file.write_bytes(corrupt)

        with pytest.raises(ValueError, match=re.escape(str(key_file))) as excinfo:
            _load_or_create_key(key_file)

        assert "x" * 10 not in str(excinfo.value)
        assert key_file.read_bytes() == corrupt
        assert _leftovers(tmp_path) == []
