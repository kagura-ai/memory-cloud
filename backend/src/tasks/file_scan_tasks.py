"""Scheduler entries of the upload malware scan (Issue #565).

Registered by ``tasks.file_tasks.schedule_file_tasks`` (leader only):

- ``file_scan_sweeper`` every 30 s: hands due scans (new uploads first, then
  the backfill of files uploaded before scanning was on) to worker tasks and
  returns at once; a no-op while ``FILE_SCAN_ENABLED`` is false.
- ``file_quarantine_expiry`` daily at 03:45 UTC: soft-deletes infected files
  past ``FILE_QUARANTINE_RETENTION_DAYS``; runs whether or not scanning is on.

Both return their counts and let unexpected exceptions propagate (the
scheduler records the run as failed), like the other file jobs.
"""

from __future__ import annotations

from services.file_scan_service import expire_quarantined_files, run_scan_sweep
from tasks.job_runs import JobFailed


async def sweep_file_scans() -> dict[str, int] | JobFailed:
    """``{"dispatched", "backfill_dispatched"}`` for this tick, or a failed run
    (``ClamdUnavailable``) while clamd does not answer."""
    return await run_scan_sweep()


async def expire_quarantine() -> dict[str, int]:
    """``{"expired", "released_bytes"}``."""
    return await expire_quarantined_files()
