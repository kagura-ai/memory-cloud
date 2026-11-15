"""The ordinary deployment target must include every reconciled branch (#2819)."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_default_migration_target_has_one_head_with_both_parallel_histories():
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic upgrade head is ambiguous: {heads}"
    ancestors = {revision.revision for revision in script.iterate_revisions(heads[0], "base")}
    assert {
        "f45_2806_sync_credentials",
        "f46_2803_admission_record",
        "f48_2807_sync_feed_runs",
        "f49_2817_clear_allowlist_adders",
        "f50_2807_merge_sync_heads",
    } <= ancestors
