"""Data retention policies for Kagura Memory Cloud.

Defines retention periods for Working and Persistent memory.

Issue #2 - Phase 1: データ保持期間要件定義
"""

from datetime import timedelta

# ============================================================================
# Working Memory Retention
# ============================================================================

# Working memory保持期間: 30日
WORKING_MEMORY_RETENTION_DAYS = 30
WORKING_MEMORY_RETENTION = timedelta(days=WORKING_MEMORY_RETENTION_DAYS)

# 自動クリーンアップスケジュール
CLEANUP_SCHEDULE = "daily"  # daily, hourly, weekly


# ============================================================================
# Persistent Memory Retention
# ============================================================================

# Persistent memory保持期間: 無期限（プラン別に将来変更可能）
PERSISTENT_MEMORY_RETENTION_DAYS = None  # None = 無期限

# プラン別保持期間（将来用）
RETENTION_BY_PLAN = {
    "free": timedelta(days=90),  # Free: 90日
    "pro": None,  # Pro: 無期限
    "enterprise": None,  # Enterprise: 無期限
}


# ============================================================================
# Working → Persistent 自動移行条件
# ============================================================================


def should_promote_to_persistent(
    access_count: int,
    age_days: int,
    importance: float,
    accessed_by_clients: list[str],
) -> bool:
    """Working → Persistent 移行判定.

    Issue #1 specification:
    - access_count >= 3
    - age_days >= 7
    - importance >= 0.7
    - len(set(accessed_by_clients)) >= 2

    Args:
        access_count: アクセス回数
        age_days: 作成からの日数
        importance: 重要度 (0.0-1.0)
        accessed_by_clients: アクセスしたクライアント一覧

    Returns:
        True if should promote

    Example:
        >>> should_promote_to_persistent(5, 10, 0.8, ["claude", "gpt"])
        True
    """
    # アクセス回数が多い
    if access_count >= 3:
        return True

    # 作成から7日経過
    if age_days >= 7:
        return True

    # 重要度が高い
    if importance >= 0.7:
        return True

    # 複数のクライアントからアクセス
    if len(set(accessed_by_clients)) >= 2:
        return True

    return False


# ============================================================================
# Cleanup Settings
# ============================================================================

# Working memoryの自動削除設定
AUTO_CLEANUP_ENABLED = True

# クリーンアップ対象の条件
CLEANUP_CRITERIA = {
    "scope": "working",  # Working memoryのみ
    "age_days": WORKING_MEMORY_RETENTION_DAYS,  # 30日以上
    "not_promoted": True,  # Persistentに移行されていない
}

# LRU削除時の優先順位
CLEANUP_PRIORITY = [
    "importance",  # 重要度が低い
    "last_used_at",  # 最終アクセスが古い
    "created_at",  # 作成日が古い
]
