"""LLM service for Sleep Maintenance.

Issue #101: Multi-provider async LLM client for structured JSON completions.
Supports OpenAI and Ollama via OpenAI-compatible API.

API key retrieval follows the same priority pattern as EmbeddingService:
context-scoped → workspace-scoped → user-scoped → env fallback.
"""

from __future__ import annotations

import json
import os

from openai import AsyncOpenAI
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import ExternalAPIKey
from utils.encryption import get_encryptor
from utils.exceptions import ConfigurationError
from utils.logger import get_logger

logger = get_logger(__name__)


class LLMServiceError(Exception):
    """Error from LLM service (parse failure, API error, etc.)."""


class LLMService:
    """Multi-provider async LLM client for Sleep Maintenance.

    Provides structured JSON completions via OpenAI or Ollama.
    Tracks token usage for cost reporting.
    """

    def __init__(self, db: AsyncSession):
        """Initialize LLM service.

        Args:
            db: Database session (for API key retrieval)
        """
        self.db = db
        self._ollama_verified = False

    async def complete_json(
        self,
        user_id: str,
        prompt: str,
        system_prompt: str | None = None,
        *,
        context_id: str | None = None,
        workspace_id: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> tuple[dict, int]:
        """Call LLM with JSON mode and return parsed response.

        Args:
            user_id: User ID for API key retrieval
            prompt: User message prompt
            system_prompt: Optional system message
            context_id: Optional context ID for scoped API key
            workspace_id: Optional workspace ID for scoped API key
            model: LLM model name (default: from config)
            provider: LLM provider (default: from config)
            temperature: Sampling temperature (low for deterministic JSON)
            max_tokens: Max response tokens

        Returns:
            Tuple of (parsed_json_dict, tokens_used)

        Raises:
            LLMServiceError: On API error or JSON parse failure after retry
        """
        resolved_model = model or os.getenv("SLEEP_LLM_MODEL", "gpt-5-nano")
        resolved_provider = provider or os.getenv("SLEEP_LLM_PROVIDER", "openai")

        client = await self._get_client(user_id, resolved_provider, context_id, workspace_id)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # First attempt
        content = ""
        tokens_used = 0
        try:
            response = await client.chat.completions.create(
                model=resolved_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            tokens_used = response.usage.total_tokens if response.usage else 0

            parsed = json.loads(content)
            logger.debug(
                "llm_complete_json_success",
                model=resolved_model,
                tokens=tokens_used,
            )
            return parsed, tokens_used

        except json.JSONDecodeError:
            # Retry once with slightly higher temperature
            logger.warning(
                "llm_json_parse_failed_retrying",
                model=resolved_model,
                content_preview=content[:200],
            )

        except Exception as e:
            raise LLMServiceError(
                f"LLM API call failed ({resolved_provider}/{resolved_model}): {e}"
            ) from e

        # Retry with higher temperature
        try:
            response = await client.chat.completions.create(
                model=resolved_model,
                messages=messages,
                temperature=0.3,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            retry_tokens = response.usage.total_tokens if response.usage else 0

            parsed = json.loads(content)
            logger.info(
                "llm_complete_json_retry_success",
                model=resolved_model,
                tokens=tokens_used + retry_tokens,
            )
            return parsed, tokens_used + retry_tokens

        except json.JSONDecodeError as e:
            raise LLMServiceError(
                f"LLM JSON parse failed after retry ({resolved_provider}/{resolved_model}): {e}"
            ) from e
        except Exception as e:
            raise LLMServiceError(
                f"LLM API call failed on retry ({resolved_provider}/{resolved_model}): {e}"
            ) from e

    async def _get_client(
        self,
        user_id: str,
        provider: str,
        context_id: str | None = None,
        workspace_id: str | None = None,
    ) -> AsyncOpenAI:
        """Get OpenAI-compatible client for the specified provider.

        Args:
            user_id: User ID for API key lookup
            provider: "openai" or "ollama"
            context_id: Optional context scope
            workspace_id: Optional workspace scope

        Returns:
            AsyncOpenAI client configured for the provider
        """
        if provider == "ollama":
            from config.settings import get_settings

            ollama_base_url = get_settings().ollama_base_url

            if not self._ollama_verified:
                import httpx

                try:
                    async with httpx.AsyncClient(timeout=5.0) as http:
                        resp = await http.get(ollama_base_url)
                        if resp.status_code != 200:
                            raise ConfigurationError(
                                f"Ollama not responding at {ollama_base_url} "
                                f"(HTTP {resp.status_code})"
                            )
                except httpx.ConnectError as err:
                    raise ConfigurationError(
                        f"Cannot connect to Ollama at {ollama_base_url}. "
                        "Is Ollama running? Start with: ollama serve"
                    ) from err
                self._ollama_verified = True

            return AsyncOpenAI(
                base_url=f"{ollama_base_url}/v1",
                api_key="ollama",
            )

        # OpenAI (default)
        api_key = await self._get_user_api_key(user_id, context_id, workspace_id)
        return AsyncOpenAI(api_key=api_key)

    async def _get_user_api_key(
        self,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,
    ) -> str:
        """Get user's OpenAI API key from database or environment.

        Same priority as EmbeddingService._get_user_api_key():
        1. Context-scoped key (most specific)
        2. Workspace-scoped key (workspace-wide)
        3. User-scoped key (personal)
        4. Environment variable (OPENAI_API_KEY) fallback

        Args:
            user_id: User ID
            context_id: Optional context ID
            workspace_id: Optional workspace ID

        Returns:
            Decrypted API key

        Raises:
            ConfigurationError: If no API key found
        """
        from uuid import UUID

        from sqlalchemy import or_

        conditions = [
            ExternalAPIKey.user_id == user_id,
            ExternalAPIKey.provider == "openai",
            ExternalAPIKey.enabled.is_(True),
        ]

        scope_conditions = []
        if context_id:
            context_uuid = UUID(context_id) if isinstance(context_id, str) else context_id
            scope_conditions.append(ExternalAPIKey.context_id == context_uuid)
        if workspace_id:
            workspace_uuid = UUID(workspace_id) if isinstance(workspace_id, str) else workspace_id
            scope_conditions.append(ExternalAPIKey.workspace_id == workspace_uuid)

        if scope_conditions:
            conditions.append(
                or_(
                    *scope_conditions,
                    and_(
                        ExternalAPIKey.context_id.is_(None),
                        ExternalAPIKey.workspace_id.is_(None),
                    ),
                )
            )

        query = (
            select(ExternalAPIKey)
            .where(and_(*conditions))
            .order_by(
                ExternalAPIKey.context_id.desc().nulls_last(),
                ExternalAPIKey.workspace_id.desc().nulls_last(),
            )
            .limit(1)
        )

        result = await self.db.execute(query)
        api_key_entry = result.scalar_one_or_none()

        if api_key_entry:
            encryptor = get_encryptor()
            api_key = encryptor.decrypt(str(api_key_entry.encrypted_value))
            logger.debug(
                "llm_api_key_from_db",
                user_id=user_id,
                context_id=context_id,
            )
            return api_key

        env_key = os.getenv("OPENAI_API_KEY")
        if env_key:
            logger.debug("llm_api_key_from_env", user_id=user_id)
            return env_key

        raise ConfigurationError(
            f"OpenAI API key not configured for user {user_id}. "
            "Please set OPENAI_API_KEY environment variable or configure in settings."
        )
