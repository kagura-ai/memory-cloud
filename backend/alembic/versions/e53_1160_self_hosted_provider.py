"""Rename provider key ollama → self_hosted across persisted data (#1160).

Issue #1160: the self-hosted inference provider key was renamed
``ollama`` → ``self_hosted`` on all four surfaces (LLM registry, reranker
CHECK constraint, embedding provider, sleep LLM provider). This is a clean
cutover with NO read alias, so every persisted ``'ollama'`` value must be
rewritten here or code that now writes/reads ``'self_hosted'`` would silently
lose those rows (and the reranker CHECK constraint would reject the new value).

Also rewrites the ``OLLAMA_BASE_URL`` external-key ``key_name`` → ``SELF_HOSTED_BASE_URL``
(the env var was renamed) and the ``neural_config`` sleep-provider description.

The UPDATEs are idempotent (``WHERE ... = 'ollama'`` matches nothing on a fresh
DB) so this is safe whether or not any ``'ollama'`` rows exist.

Ordering note: the ``context_search_configs.reranker_provider`` CHECK
constraint is DROPPED before the data UPDATE and RECREATED after — updating a
row to ``'self_hosted'`` while the old CHECK (``... , 'ollama'``) is still
active would violate the constraint.

Revision ID: e53_1160_self_hosted_provider
Revises: e52_1136_file_context_acl
"""

from alembic import op

# revision identifiers
revision = "e53_1160_self_hosted_provider"
down_revision = "e52_1136_file_context_acl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Rewrite persisted 'ollama' provider values → 'self_hosted'."""
    # 1. context_search_configs.reranker_provider — drop CHECK, migrate data,
    #    recreate CHECK with the renamed value.
    op.drop_constraint("reranker_provider_check", "context_search_configs", type_="check")
    op.execute(
        "UPDATE context_search_configs SET reranker_provider = 'self_hosted' "
        "WHERE reranker_provider = 'ollama'"
    )
    op.create_check_constraint(
        "reranker_provider_check",
        "context_search_configs",
        "reranker_provider IN ('voyage', 'cohere', 'self_hosted')",
    )

    # 2. external_api_keys — provider key + the auto-generated key_name for the
    #    self-hosted base URL (env var renamed OLLAMA_BASE_URL → SELF_HOSTED_BASE_URL).
    #    'ollama_cloud' is a distinct hosted service and is intentionally untouched.
    op.execute("UPDATE external_api_keys SET provider = 'self_hosted' WHERE provider = 'ollama'")
    op.execute(
        "UPDATE external_api_keys SET key_name = 'SELF_HOSTED_BASE_URL' "
        "WHERE key_name = 'OLLAMA_BASE_URL'"
    )

    # 3. llm_pricing — $0 embedding rows seeded with provider='ollama' (c03 seed).
    op.execute("UPDATE llm_pricing SET provider = 'self_hosted' WHERE provider = 'ollama'")

    # 4. neural_config — sleep LLM provider value + its description.
    op.execute(
        "UPDATE neural_config SET value = 'self_hosted' "
        "WHERE key = 'sleep_llm_provider' AND value = 'ollama'"
    )
    op.execute(
        "UPDATE neural_config SET description = "
        "'LLM provider for sleep maintenance (openai/self_hosted)' "
        "WHERE key = 'sleep_llm_provider'"
    )

    # 5. Historical rows — keep (provider, model) joins against llm_pricing intact.
    op.execute(
        "UPDATE sleep_report_llm_usage SET provider = 'self_hosted' WHERE provider = 'ollama'"
    )
    op.execute(
        "UPDATE sleep_reports SET embedding_provider = 'self_hosted' "
        "WHERE embedding_provider = 'ollama'"
    )
    op.execute("UPDATE llm_call_log SET provider = 'self_hosted' WHERE provider = 'ollama'")


def downgrade() -> None:
    """Symmetric revert: 'self_hosted' → 'ollama'."""
    op.drop_constraint("reranker_provider_check", "context_search_configs", type_="check")
    op.execute(
        "UPDATE context_search_configs SET reranker_provider = 'ollama' "
        "WHERE reranker_provider = 'self_hosted'"
    )
    op.create_check_constraint(
        "reranker_provider_check",
        "context_search_configs",
        "reranker_provider IN ('voyage', 'cohere', 'ollama')",
    )

    op.execute("UPDATE external_api_keys SET provider = 'ollama' WHERE provider = 'self_hosted'")
    op.execute(
        "UPDATE external_api_keys SET key_name = 'OLLAMA_BASE_URL' "
        "WHERE key_name = 'SELF_HOSTED_BASE_URL'"
    )

    op.execute("UPDATE llm_pricing SET provider = 'ollama' WHERE provider = 'self_hosted'")

    op.execute(
        "UPDATE neural_config SET value = 'ollama' "
        "WHERE key = 'sleep_llm_provider' AND value = 'self_hosted'"
    )
    op.execute(
        "UPDATE neural_config SET description = "
        "'LLM provider for sleep maintenance (openai/ollama)' "
        "WHERE key = 'sleep_llm_provider'"
    )

    op.execute(
        "UPDATE sleep_report_llm_usage SET provider = 'ollama' WHERE provider = 'self_hosted'"
    )
    op.execute(
        "UPDATE sleep_reports SET embedding_provider = 'ollama' "
        "WHERE embedding_provider = 'self_hosted'"
    )
    op.execute("UPDATE llm_call_log SET provider = 'ollama' WHERE provider = 'self_hosted'")
