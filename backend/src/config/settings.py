"""Application settings using Pydantic Settings.

Environment variables are loaded from .env file.
Use ENV_FILE environment variable to specify which file to load.
Database URLs are managed directly via os.getenv() in config/database.py.
"""

import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", ".env.dev"),  # Default: .env.dev
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="",  # No prefix
    )

    # OAuth2 - Google
    google_client_id: str = Field(default="", description="Google OAuth2 Client ID")
    google_client_secret: str = Field(default="", description="Google OAuth2 Client Secret")
    google_redirect_uri: str = Field(
        default="http://localhost:8080/auth/google/callback",
        description="OAuth2 Redirect URI",
    )

    # OAuth2 - GitHub (optional)
    github_client_id: str | None = Field(default=None, description="GitHub OAuth2 Client ID")
    github_client_secret: str | None = Field(
        default=None, description="GitHub OAuth2 Client Secret"
    )

    # OAuth2 - Azure (optional)
    azure_client_id: str | None = Field(default=None, description="Azure OAuth2 Client ID")
    azure_client_secret: str | None = Field(default=None, description="Azure OAuth2 Client Secret")
    azure_tenant_id: str | None = Field(default=None, description="Azure Tenant ID")

    # Security
    api_key_secret: str = Field(
        default="dev-secret-change-in-production",
        description="API Key encryption secret (32+ bytes)",
    )
    jwt_secret: str = Field(
        default="dev-secret-change-in-production", description="JWT signing secret (32+ bytes)"
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT algorithm")
    jwt_expire_minutes: int = Field(default=60, description="JWT expiration (minutes)")

    # Session
    session_ttl_seconds: int = Field(
        default=7 * 24 * 60 * 60, description="Session TTL (default: 7 days)"
    )

    # API Settings
    api_host: str = Field(default="0.0.0.0", description="API host")
    api_port: int = Field(default=8080, description="API port")
    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:8080",
        description="CORS allowed origins (comma-separated)",
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS origins from comma-separated string to list."""
        return [origin.strip() for origin in self.cors_origins.split(",")]

    # Logging
    log_level: str = Field(default="INFO", description="Log level")
    log_colorize: bool = Field(default=True, description="Enable colored logs")

    # Qdrant Security (Issue #273 Security Review)
    qdrant_api_key: str = Field(
        default="", description="Qdrant API key (REQUIRED in production, empty for local dev)"
    )

    # Embedding Configuration
    embedding_provider: str = Field(
        default="openai", description="Embedding model provider (openai/cohere/huggingface)"
    )
    embedding_model: str = Field(
        default="text-embedding-3-small", description="Embedding model name"
    )
    embedding_dimensions: int = Field(default=512, description="Embedding vector dimensions")

    # Ollama Configuration (Issue #44)
    ollama_base_url: str = Field(
        default="http://localhost:11434", description="Ollama API base URL"
    )

    # Search Configuration (Issue #105)
    enable_reranking: bool = Field(
        default=True,
        description="Enable reranking globally. Future: toggle external vs local reranker.",
    )

    # Feature Flags
    enable_neural_memory: bool = Field(default=False, description="Enable Neural Memory (Phase 3)")
    enable_research_tools: bool = Field(
        default=True, description="Enable research tools (Brave Search etc.)"
    )

    # Environment
    environment: str = Field(
        default="development", description="Environment (development/staging/production)"
    )

    # OAuth Provider Control (Issue #360)
    auth_providers: str = Field(
        default="auto",
        description="Comma-separated OAuth providers to enable: 'google', 'github', 'google,github', or 'auto' (enable all configured)",
    )

    # Registration Control (Issue #349)
    allow_registration: bool = Field(
        default=False,
        description="Allow new user registration via OAuth. "
        "When False, only invited users and the first admin can register.",
    )

    # Billing Plugin (OSS: disabled by default, SaaS: enabled with Stripe)
    billing_enabled: bool = Field(
        default=False,
        description="Enable billing plugin for self-service plan changes. "
        "When disabled, plan changes are admin-only.",
    )
    stripe_secret_key: str | None = Field(
        default=None, description="Stripe secret key (required when billing_enabled=True)"
    )
    stripe_webhook_secret: str | None = Field(
        default=None, description="Stripe webhook signing secret"
    )
    stripe_price_basic: str | None = Field(
        default=None, description="Stripe Price ID for Basic plan"
    )
    stripe_price_pro: str | None = Field(default=None, description="Stripe Price ID for Pro plan")

    # Plan Tier Overrides (environment variable customization for OSS deployments)
    plan_free_max_contexts: int | None = Field(
        default=None, description="Override FREE plan max contexts per workspace"
    )
    plan_free_memory_limit: int | None = Field(
        default=None, description="Override FREE plan memory limit"
    )
    plan_free_mcp_calls_per_day: int | None = Field(
        default=None, description="Override FREE plan MCP calls/day"
    )
    plan_basic_max_contexts: int | None = Field(
        default=None, description="Override BASIC plan max contexts per workspace"
    )
    plan_basic_memory_limit: int | None = Field(
        default=None, description="Override BASIC plan memory limit"
    )
    plan_basic_mcp_calls_per_day: int | None = Field(
        default=None, description="Override BASIC plan MCP calls/day"
    )
    plan_pro_max_contexts: int | None = Field(
        default=None, description="Override PRO plan max contexts per workspace"
    )
    plan_pro_memory_limit: int | None = Field(
        default=None, description="Override PRO plan memory limit"
    )
    plan_pro_mcp_calls_per_day: int | None = Field(
        default=None, description="Override PRO plan MCP calls/day"
    )

    # Plan Display Names (customizable for SaaS forks)
    plan_free_display_name: str | None = Field(
        default=None, description="Display name for FREE tier (default: S)"
    )
    plan_basic_display_name: str | None = Field(
        default=None, description="Display name for BASIC tier (default: M)"
    )
    plan_pro_display_name: str | None = Field(
        default=None, description="Display name for PRO tier (default: L)"
    )

    # Usage & Plan Limits (Issue #48)
    default_plan_memory_limit: int = Field(
        default=1000, description="Default FREE plan memory limit"
    )
    default_plan_daily_api_limit: int = Field(
        default=1000, description="Default FREE plan daily API call limit"
    )
    default_plan_weekly_api_limit: int = Field(
        default=5000, description="Default FREE plan weekly API call limit"
    )
    usage_warning_threshold: float = Field(
        default=0.80, description="Usage warning threshold (0.0-1.0)"
    )
    usage_critical_threshold: float = Field(
        default=0.95, description="Usage critical threshold (0.0-1.0)"
    )


# Global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get global settings instance.

    Returns:
        Settings instance

    Raises:
        ValueError: If required environment variables are missing
    """
    global _settings

    if _settings is None:
        _settings = Settings()

    return _settings
