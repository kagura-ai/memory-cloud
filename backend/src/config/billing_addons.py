"""Canonical billing addon dimensions and storage bounds."""

from config.retention import OPERATION_HISTORY_MAX_RETENTION_BONUS_DAYS

# Friendly wire names are decoupled from ORM columns; callers never accept
# arbitrary column names. Both billing writers replace every listed column.
ADDON_COLUMNS: dict[str, str] = {
    "memory": "addon_memory_bonus",
    "mcp_quota": "addon_mcp_quota_bonus",
    "rest_quota": "addon_rest_quota_bonus",
    "public_quota": "addon_public_quota_bonus",
    "member": "addon_member_bonus",
    "context": "addon_context_bonus",
    "analysis": "addon_analysis_bonus",
    "storage_mb": "addon_storage_bonus_mb",
    "sleep_contexts": "addon_sleep_contexts_bonus",
    "connector": "addon_connector_bonus",
    "memories_per_day": "addon_memories_per_day_bonus",
    "history_retention_days": "addon_history_retention_days_bonus",
}
# Stay below signed PostgreSQL INTEGER's 2_147_483_647 ceiling so excessive
# bonuses fail validation instead of producing a database overflow/HTTP500.
ADDON_MAX_BONUS = 2_000_000_000
ADDON_MAX_BY_KEY: dict[str, int] = {
    "history_retention_days": OPERATION_HISTORY_MAX_RETENTION_BONUS_DAYS,
}
