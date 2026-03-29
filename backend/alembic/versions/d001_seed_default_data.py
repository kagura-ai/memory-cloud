"""Seed default data (neural_config + mcp_tool_descriptions).

Revision ID: d001_seed
Revises: 157247e0df86
Create Date: 2026-03-27

Idempotent: uses ON CONFLICT DO NOTHING so safe to run on existing databases.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "d001_seed"
down_revision = "157247e0df86"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Insert default seed data."""

    # ================================================================
    # Neural Memory Configuration (27 parameters)
    # ================================================================
    op.execute("""
        INSERT INTO neural_config (key, value, value_type, category, description, min_value, max_value) VALUES
            -- Hebbian Learning
            ('top_m_edges', '8', 'int', 'hebbian', 'Maximum edges per node (sparsity control)', 1, 64),
            ('learning_rate', '0.05', 'float', 'hebbian', 'Hebbian learning rate', 0.0, 1.0),
            ('decay_lambda', '0.01', 'float', 'hebbian', 'L2 decay coefficient', 0.0, 1.0),
            ('weight_max', '3.0', 'float', 'hebbian', 'Maximum edge weight (clipping)', 0.1, 10.0),
            -- Activation Spreading
            ('spread_hops', '1', 'int', 'spreading', 'Activation spreading depth', 1, 3),
            ('spread_decay', '0.6', 'float', 'spreading', 'Decay factor per hop', 0.0, 1.0),
            ('spread_threshold', '0.01', 'float', 'spreading', 'Minimum activation to continue', 0.0, 0.5),
            -- Scoring Weights
            ('alpha', '0.55', 'float', 'scoring', 'Semantic similarity weight', 0.0, 1.0),
            ('beta', '0.20', 'float', 'scoring', 'Graph association weight', 0.0, 1.0),
            ('gamma', '0.10', 'float', 'scoring', 'Recency weight', 0.0, 1.0),
            ('delta', '0.10', 'float', 'scoring', 'Importance weight', 0.0, 1.0),
            ('epsilon', '0.05', 'float', 'scoring', 'Trust weight', 0.0, 1.0),
            ('zeta', '0.25', 'float', 'scoring', 'Redundancy penalty (MMR)', 0.0, 1.0),
            -- Temporal Parameters
            ('recency_tau_days', '14.0', 'float', 'temporal', 'Recency decay time constant (days)', 1.0, 365.0),
            ('importance_ema_alpha', '0.3', 'float', 'temporal', 'Importance EMA smoothing', 0.0, 1.0),
            -- Decay/Forgetting
            ('decay_rate', '0.001', 'float', 'decay', 'Edge weight decay rate', 0.0, 0.1),
            ('prune_threshold', '0.01', 'float', 'decay', 'Minimum edge weight before pruning', 0.0, 0.5),
            ('decay_background_interval', '3600', 'int', 'decay', 'Background decay interval (seconds)', 60, 86400),
            -- Co-Activation
            ('co_activation_window', '300', 'int', 'coactivation', 'Same-session time window (seconds)', 60, 3600),
            ('min_co_activation_count', '2', 'int', 'coactivation', 'Minimum count to strengthen edge', 1, 10),
            -- Consolidation
            ('consolidation_use_count_min', '3', 'int', 'consolidation', 'Min use count for promotion', 1, 20),
            ('consolidation_importance_min', '0.65', 'float', 'consolidation', 'Min importance for promotion', 0.0, 1.0),
            ('consolidation_diversity_min', '0.2', 'float', 'consolidation', 'Min diversity for promotion', 0.0, 1.0),
            -- Performance
            ('batch_update_size', '100', 'int', 'performance', 'Batch size for Hebbian updates', 1, 1000),
            ('max_candidates_k', '64', 'int', 'performance', 'Maximum retrieval candidates', 10, 500),
            -- Security
            ('gradient_clipping', '0.5', 'float', 'security', 'Maximum total weight change per node per update', 0.1, 2.0),
            ('async_update_delay_ms', '2000', 'int', 'performance', 'Debounce delay (ms) before batch DB writes', 100, 10000)
        ON CONFLICT (key) DO NOTHING;
    """)

    # ================================================================
    # MCP Tool Descriptions (6 tools x 2 locales = 12 entries)
    # ================================================================
    op.execute("""
        INSERT INTO mcp_tool_descriptions (tool_name, locale, description) VALUES
        ('remember', 'en', 'Store important information, decisions, code snippets, or context into long-term memory. Use this when you want to preserve information for future recall across conversations.

Supports 3-layer architecture:
- summary: Concise overview for search (10-500 chars)
- context_summary: Why this matters and how to use it
- details: Complete data, code, or structured information

Enrich memories with tags, importance (0.0-1.0), and type (code, note, decision, bug-fix, feature, learning).'),

        ('remember', 'ja', '重要な情報、決定事項、コードスニペット、文脈を長期メモリに保存します。

3層アーキテクチャ:
- summary: 検索用の簡潔な概要（10-500文字）
- context_summary: なぜ重要か、どう使うか
- details: 完全なデータ、コード、構造化情報

タグ、importance（0.0-1.0）、type（code, note, decision等）で検索品質を向上。'),

        ('recall', 'en', 'Search and retrieve memories using Hybrid Search (60%% semantic + 40%% full-text) with Neural Memory boosting.

Tips: expand queries with related terms, use tag filters for precision, try HyDE technique for question-style queries.
Returns summaries and context (Layers 1-2) optimized for quick understanding.'),

        ('recall', 'ja', 'ハイブリッド検索（セマンティック60%% + 全文検索40%%）とNeural Memoryブースティングでメモリを検索・取得します。

ヒント: 関連用語でクエリ拡張、タグフィルタで精度向上、質問形式にはHyDE技法を試す。
要約とコンテキスト（レイヤー1-2）を返します。'),

        ('forget', 'en', 'Delete memories that are no longer needed (soft delete, recoverable within retention period).

Safe workflow: recall() to preview → verify memory_id → forget().
Always verify before deleting, especially for high-importance memories.'),

        ('forget', 'ja', '不要になったメモリを削除します（ソフト削除、保持期間内は復元可能）。

安全なワークフロー: recall()でプレビュー → memory_idを確認 → forget()。
特にimportanceが高いメモリは削除前に必ず確認。'),

        ('reference', 'en', 'Retrieve complete details (Layer 3) of a specific memory by ID. Use after recall() when you need full content and metadata.

Workflow: recall() → identify interesting memories → reference() for full details.'),

        ('reference', 'ja', '特定のメモリの完全な詳細（レイヤー3）をIDで取得。recall()後に完全なコンテンツが必要な時に使用。

ワークフロー: recall() → 興味深いメモリを特定 → reference()で完全な詳細を取得。'),

        ('explore', 'en', 'Discover related memories through Neural Memory graph traversal using activation spreading.

Parameters: depth (1=direct, 2=recommended, 3+=broader), min_weight (0.0=all, 0.05=balanced, 0.1+=strong only).
Returns memories ranked by activation strength.'),

        ('explore', 'ja', 'Neural Memoryグラフ探索と活性化拡散で関連メモリを発見。

パラメータ: depth（1=直接、2=推奨、3+=広く）、min_weight（0.0=全て、0.05=バランス、0.1+=強いのみ）。
活性化強度でランク付けされたメモリを返します。'),

        ('get_context_info', 'en', 'Get current context information, usage guidelines, and optional memory statistics.

Call at session start, after switching contexts, or when you need project-specific memory guidelines.
Returns context ID, name, summary, usage guide, and optionally stats.'),

        ('get_context_info', 'ja', '現在のコンテキスト情報、使用ガイドライン、メモリ統計を取得。

セッション開始時、コンテキスト切り替え後、プロジェクト固有のガイドラインが必要な時に呼び出し。
コンテキストID、名前、要約、使用ガイド、統計を返します。')

        ON CONFLICT (tool_name, locale) DO NOTHING;
    """)


def downgrade() -> None:
    """Remove seed data (caution: destructive)."""
    op.execute(
        "DELETE FROM mcp_tool_descriptions WHERE tool_name IN ('remember','recall','forget','reference','explore','get_context_info')"
    )
    op.execute("DELETE FROM neural_config")
